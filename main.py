"""
astrbot_plugin_web_search_auto
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

自动网络搜索插件 — on_llm_request 拦截用户消息，后台自动搜索，
结果注入 system prompt 作为上下文，LLM 自然回复。

不依赖 function calling，弱模型也能用。

参考 Claude Code 的 WebSearch / WebFetch 工具设计模式。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from html import unescape
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

REMOVE_TAGS = ["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

@dataclass
class PluginConfig:
    """插件配置。默认用 DuckDuckGo，零部署。"""
    search_backend: str = "bing"          # bing | duckduckgo | searxng
    searxng_url: str = "http://localhost:8080"
    proxy: str = ""                       # 代理地址，如 http://127.0.0.1:7890
    max_results: int = 5
    cache_ttl: int = 900
    cache_max_size: int = 100
    fetch_timeout: int = 15
    fetch_max_chars: int = 10000
    enable_prompt_hint: bool = True

    @classmethod
    def from_context(cls, context: Context) -> "PluginConfig":
        cfg: Optional[dict] = getattr(context, "_config", None)
        if not isinstance(cfg, dict) or not cfg:
            return cls()
        return cls(
            search_backend=str(cfg.get("search_backend", "bing")),
            searxng_url=str(cfg.get("searxng_url", "http://localhost:8080")).rstrip("/"),
            proxy=str(cfg.get("proxy", "")).strip(),
            max_results=_clamp(int(cfg.get("max_results", 5)), 1, 20),
            cache_ttl=max(int(cfg.get("cache_ttl", 900)), 0),
            cache_max_size=max(int(cfg.get("cache_max_size", 100)), 1),
            fetch_timeout=_clamp(int(cfg.get("fetch_timeout", 15)), 3, 60),
            fetch_max_chars=_clamp(int(cfg.get("fetch_max_chars", 10000)), 500, 50000),
            enable_prompt_hint=bool(cfg.get("enable_prompt_hint", True)),
        )


# ---------------------------------------------------------------------------
# 页面缓存（类似 Claude Code 的 15 分钟缓存策略）
# ---------------------------------------------------------------------------

class PageCache:
    """FIFO + TTL 缓存。TTL=0 时禁用缓存。"""

    def __init__(self, ttl: int = 900, max_size: int = 100) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()

    @property
    def enabled(self) -> bool:
        return self._ttl > 0

    async def get(self, key: str) -> Optional[str]:
        if not self.enabled:
            return None
        entry = self._data.get(key)
        if entry is None:
            return None
        content, ts = entry
        if time.monotonic() - ts > self._ttl:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return content

    async def set(self, key: str, content: str) -> None:
        if not self.enabled:
            return
        if key in self._data:
            del self._data[key]
        elif len(self._data) >= self._max_size:
            self._data.popitem(last=False)
        self._data[key] = (content, time.monotonic())


# ---------------------------------------------------------------------------
# 搜索后端适配器（工厂模式，可扩展多个后端）
# ---------------------------------------------------------------------------

class BaseSearcher(ABC):
    """搜索后端抽象基类。"""

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int = 10,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        """返回 [{title, url, snippet}, ...]。"""
        ...


class DuckDuckGoSearcher(BaseSearcher):
    """
    DuckDuckGo 搜索后端。
    使用 ddgs 库直接搜多个搜索引擎（Brave/Yahoo/Bing 等），
    类似 Claude Code 的 scraping 方式，无需 API Key，无需部署服务。
    """

    def __init__(self, proxy: str = "") -> None:
        self._proxy = proxy or None
        self._ddgs = None
        self._import_error: Optional[str] = None

    def _get_ddgs(self):
        """惰性导入 ddgs 库。"""
        if self._ddgs is not None:
            return self._ddgs
        if self._import_error:
            raise ImportError(self._import_error)
        try:
            from ddgs import DDGS
            self._ddgs = DDGS
        except ImportError as e:
            self._import_error = (
                f"需要安装 ddgs 库: pip install ddgs\n"
                f"原始错误: {e}"
            )
            raise ImportError(self._import_error) from e
        return self._ddgs

    async def search(
        self,
        query: str,
        max_results: int = 10,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        DDGS = self._get_ddgs()

        loop = asyncio.get_event_loop()

        def _do_search() -> List[Dict[str, str]]:
            results: List[Dict[str, str]] = []
            kwargs = {}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            with DDGS(**kwargs) as ddgs:
                for item in ddgs.text(query, max_results=max_results):
                    url_str = item.get("href", "")
                    if not url_str:
                        continue
                    hostname = _hostname(url_str)
                    if allowed_domains and hostname not in allowed_domains:
                        continue
                    if blocked_domains and hostname in blocked_domains:
                        continue
                    results.append({
                        "title": item.get("title", ""),
                        "url": url_str,
                        "snippet": item.get("body", ""),
                    })
            return results

        return await loop.run_in_executor(None, _do_search)


class BingSearcher(BaseSearcher):
    """
    Bing 搜索后端 — 直接爬 cn.bing.com 的 HTML 搜索结果。
    类似 Claude Code 的 BingSearchAdapter，无需 API Key，无需代理。
    cn.bing.com 国内可直接访问。
    """

    def __init__(self, session: aiohttp.ClientSession, proxy: str = "") -> None:
        self._session = session
        self._proxy = proxy

    async def search(
        self,
        query: str,
        max_results: int = 10,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        url = "https://cn.bing.com/search"
        params = {"q": query, "count": max_results}
        headers = {"User-Agent": DEFAULT_USER_AGENT}

        try:
            async with self._session.get(
                url,
                params=params,
                headers=headers,
                proxy=self._proxy or None,
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Bing returned {resp.status}")
                    return []
                html = await resp.text(encoding="utf-8", errors="replace")
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error(f"Bing request failed: {exc}")
            return []

        return self._parse_results(html, max_results, allowed_domains, blocked_domains)

    def _parse_results(
        self,
        html: str,
        max_results: int,
        allowed_domains: Optional[List[str]],
        blocked_domains: Optional[List[str]],
    ) -> List[Dict[str, str]]:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            return []

        results: List[Dict[str, str]] = []
        for li in soup.select("li.b_algo"):
            h2 = li.find("h2")
            a_tag = h2.find("a") if h2 else None
            if not a_tag:
                continue

            url_str = a_tag.get("href", "")
            title = a_tag.get_text(strip=True)
            if not url_str or not title:
                continue

            # 域名过滤
            hostname = _hostname(url_str)
            if allowed_domains and hostname not in allowed_domains:
                continue
            if blocked_domains and hostname in blocked_domains:
                continue

            # 摘要
            snippet = ""
            cap_p = li.select_one(".b_caption p") or li.select_one("p")
            if cap_p:
                snippet = cap_p.get_text(strip=True)

            results.append({"title": title, "url": url_str, "snippet": snippet})
            if len(results) >= max_results:
                break

        return results


class SearXNGSearcher(BaseSearcher):
    """SearXNG JSON API 后端 — 可选，适合自托管场景。"""

    def __init__(self, base_url: str, session: aiohttp.ClientSession) -> None:
        self._base_url = base_url
        self._session = session

    async def search(
        self,
        query: str,
        max_results: int = 10,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ) -> List[Dict[str, str]]:
        params: Dict[str, Any] = {
            "format": "json",
            "q": query,
            "categories": "general",
            "pageno": 1,
        }
        url = f"{self._base_url}/search"

        try:
            async with self._session.get(
                url, params=params, headers={"User-Agent": DEFAULT_USER_AGENT}
            ) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"SearXNG returned {resp.status}: {text[:300]}")
                    return []
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.error(f"SearXNG request failed: {exc}")
            return []

        raw: List[dict] = data.get("results", [])
        if not raw:
            return []

        results: List[Dict[str, str]] = []
        for item in raw:
            url_str = item.get("url", "")
            if not url_str:
                continue
            hostname = _hostname(url_str)
            if allowed_domains and hostname not in allowed_domains:
                continue
            if blocked_domains and hostname in blocked_domains:
                continue
            results.append({
                "title": item.get("title", ""),
                "url": url_str,
                "snippet": item.get("content", item.get("snippet", "")),
            })
            if len(results) >= max_results:
                break
        return results


def create_searcher(config: PluginConfig, session: Optional[aiohttp.ClientSession] = None) -> BaseSearcher:
    """搜索后端工厂函数 — 类似 Claude Code 的 createAdapter()。"""
    if config.search_backend == "duckduckgo":
        logger.info(f"Using DuckDuckGo backend (ddgs){' with proxy' if config.proxy else ''}")
        return DuckDuckGoSearcher(proxy=config.proxy)
    elif config.search_backend == "searxng":
        if session is None:
            raise ValueError("SearXNG 后端需要 aiohttp.ClientSession")
        logger.info(f"Using SearXNG backend: {config.searxng_url}")
        return SearXNGSearcher(config.searxng_url, session)
    else:
        # 默认 bing — 国内直连，类似 Claude Code 的 BingSearchAdapter
        if session is None:
            raise ValueError("Bing 后端需要 aiohttp.ClientSession")
        logger.info("Using Bing backend (cn.bing.com, no proxy needed)")
        return BingSearcher(session, proxy=config.proxy)


# ---------------------------------------------------------------------------
# 页面获取（类似 Claude Code 的 WebFetch — 本地 HTTP + 正文提取）
# ---------------------------------------------------------------------------

class PageFetcher:
    """异步网页获取 + BeautifulSoup 正文提取。"""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cache: PageCache,
        timeout: int = 15,
        max_chars: int = 10000,
    ) -> None:
        self._session = session
        self._cache = cache
        self._timeout = timeout
        self._max_chars = max_chars

    async def fetch(self, url: str) -> str:
        cache_key = _cache_key(url)
        if self._cache.enabled:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        try:
            async with self._session.get(
                url,
                headers={"User-Agent": DEFAULT_USER_AGENT},
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                allow_redirects=True,
                ssl=False,
            ) as resp:
                if resp.status != 200:
                    return f"[Error] HTTP {resp.status}"
                content_type = resp.headers.get("Content-Type", "")
                html = await resp.text(encoding="utf-8", errors="replace")
        except asyncio.TimeoutError:
            return f"[Error] Timeout after {self._timeout}s"
        except aiohttp.ClientError as exc:
            return f"[Error] {exc}"

        if "text/html" in content_type or not content_type:
            text = _html_to_text(html)
        else:
            text = html.strip()

        if not text:
            return "[Error] No readable content extracted"

        if len(text) > self._max_chars:
            text = text[:self._max_chars] + "\n\n[... content truncated]"

        if self._cache.enabled:
            await self._cache.set(cache_key, text)

        return text


# ---------------------------------------------------------------------------
# HTML 工具
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    """BeautifulSoup + lxml 提取可读文本。"""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(REMOVE_TAGS):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return unescape(text)


def _hostname(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(val, hi))


# ---------------------------------------------------------------------------
# 插件主体 — 自动搜索模式
#
# 策略：不依赖 LLM 的 function calling（弱模型不会用），
# 而是 on_llm_request 拦截用户消息 → 后台自动搜 → 结果注入 system prompt
# → LLM 像平常一样回复，自然用上搜索上下文。
# ---------------------------------------------------------------------------

@register("web_search_auto", "bentianjia", "自动网络搜索（Bing 直连零部署）", "2.0.0")
class WebSearchAuto(Star):
    """
    自动网络搜索插件 — LLM 无需命令前缀，无需 function calling。

    在每次 LLM 请求前自动搜索用户消息，将搜索结果作为上下文注入，
    LLM 根据语境自然筛选和回复。
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._config: Optional[PluginConfig] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._cache: Optional[PageCache] = None
        self._searcher: Optional[BaseSearcher] = None
        self._fetcher: Optional[PageFetcher] = None
        self._init_lock = asyncio.Lock()
        # 防重复搜：同一会话的上一轮 query
        self._last_query: str = ""

    # ---- 生命周期 ----------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        if self._config is not None:
            return
        async with self._init_lock:
            if self._config is not None:
                return
            self._config = PluginConfig.from_context(self.context)
            self._cache = PageCache(
                ttl=self._config.cache_ttl,
                max_size=self._config.cache_max_size,
            )
            self._http = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30),
            )
            self._searcher = create_searcher(self._config, self._http)
            self._fetcher = PageFetcher(
                session=self._http,
                cache=self._cache,
                timeout=self._config.fetch_timeout,
                max_chars=self._config.fetch_max_chars,
            )
            logger.info(
                f"web_search_auto initialized: "
                f"backend={self._config.search_backend}, "
                f"cache_ttl={self._config.cache_ttl}s"
            )

    async def terminate(self) -> None:
        if self._http:
            await self._http.close()
            self._http = None
        self._config = None
        self._searcher = None
        self._fetcher = None
        self._cache = None
        logger.info("web_search_auto plugin unloaded.")

    # ---- 核心：自动搜索 + 注入上下文 ----------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """
        拦截 LLM 请求 → 用用户消息自动搜索 → 结果注入 system prompt。
        LLM 无需 function calling，直接基于上下文回复。
        """
        await self._ensure_initialized()
        if not self._config:
            return

        # 获取用户消息文本
        user_msg = _get_event_text(event)
        if not user_msg or len(user_msg) < 4:
            return

        # 避免重复搜同一句
        if user_msg == self._last_query:
            return
        self._last_query = user_msg

        logger.info(f"auto-search triggered: '{user_msg[:60]}'")

        # 后台搜索
        try:
            results = await self._searcher.search(
                query=user_msg,
                max_results=self._config.max_results,
            )
        except Exception as exc:
            logger.error(f"auto-search failed: {exc}")
            return

        if not results:
            return

        # 构建搜索上下文
        ctx_lines = [
            "## [Web Search Context — use this to answer accurately, cite sources briefly]",
            "",
        ]
        for i, r in enumerate(results, 1):
            snippet = r.get("snippet", "").replace("\n", " ").strip()
            ctx_lines.append(f"{i}. {r['title']} - {r['url']}")
            if snippet:
                ctx_lines.append(f"   {snippet[:200]}")
            ctx_lines.append("")

        ctx_lines.append("[End of search context]")
        context_block = "\n".join(ctx_lines)

        # 注入 system prompt
        if req.system_prompt:
            req.system_prompt = context_block + "\n\n" + req.system_prompt
        else:
            req.system_prompt = context_block


def _get_event_text(event: AstrMessageEvent) -> str:
    """从事件中提取用户消息文本。"""
    try:
        msg = event.message_str
        if msg:
            return msg.strip()
    except Exception:
        pass

    try:
        for seg in event.message_obj.message:
            text = getattr(seg, "text", None)
            if text:
                return text.strip()
    except Exception:
        pass

    return ""
