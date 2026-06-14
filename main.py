"""
astrbot_plugin_web_search_auto
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

自动网络搜索插件 — LLM 自主决定何时调用 web_search / web_fetch，
无需命令前缀。

参考 Claude Code 的 WebSearch / WebFetch 设计：
- web_search: 直接搜搜索引擎（DuckDuckGo 类似 Claude Code 的 Bing scraping）
- web_fetch:  获取网页全文，提取正文
- 多后端适配器模式，默认零部署即可用
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

SYSTEM_PROMPT_HINT = (
    "## Web Search Capability\n"
    "You have two web tools available. Use them PROACTIVELY — "
    "if you are unsure about facts, dates, news, or any real-time information, "
    "search the web rather than guessing.\n\n"
    "1. **web_search(query, max_results=10, allowed_domains, blocked_domains)**\n"
    "   Search the web. Returns title / URL / snippet for each result.\n"
    "2. **web_fetch(url)**\n"
    "   Fetch the full content of a web page for detailed reading.\n\n"
    "Workflow: search first, then fetch specific pages if the snippets are insufficient.\n"
    "Always cite your sources when using information from the web."
)

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
    search_backend: str = "duckduckgo"   # duckduckgo | searxng
    searxng_url: str = "http://localhost:8080"
    proxy: str = ""                       # 代理地址，如 http://127.0.0.1:7890
    max_results: int = 10
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
            search_backend=str(cfg.get("search_backend", "duckduckgo")),
            searxng_url=str(cfg.get("searxng_url", "http://localhost:8080")).rstrip("/"),
            proxy=str(cfg.get("proxy", "")).strip(),
            max_results=_clamp(int(cfg.get("max_results", 10)), 1, 20),
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
# 搜索后端适配器（参考 Claude Code 的 Adapter Factory 模式）
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
    if config.search_backend == "searxng":
        if session is None:
            raise ValueError("SearXNG 后端需要 aiohttp.ClientSession")
        logger.info(f"Using SearXNG backend: {config.searxng_url}")
        return SearXNGSearcher(config.searxng_url, session)
    else:
        logger.info(f"Using DuckDuckGo backend (ddgs){' with proxy' if config.proxy else ''}")
        return DuckDuckGoSearcher(proxy=config.proxy)


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
# 插件主体
# ---------------------------------------------------------------------------

@register("web_search_auto", "bentianjia", "自动网络搜索（DuckDuckGo 零部署）", "1.1.0")
class WebSearchAuto(Star):
    """
    自动网络搜索插件 — 参考 Claude Code 的 WebSearch / WebFetch 设计。

    默认使用 DuckDuckGo 直接搜索，零部署，无需 API Key。
    也支持切换到 SearXNG 自托管。

    LLM 工具：
    - web_search: 搜索网络，返回标题/URL/摘要
    - web_fetch:  获取网页全文
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._config: Optional[PluginConfig] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._cache: Optional[PageCache] = None
        self._searcher: Optional[BaseSearcher] = None
        self._fetcher: Optional[PageFetcher] = None
        self._init_lock = asyncio.Lock()

    # ---- 生命周期 ----------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        """懒初始化。第一次工具调用时创建所有子组件。"""
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
            # 适配器工厂：按配置选择搜索后端
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

    # ---- 系统提示注入 --------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """每次 LLM 请求前注入搜索工具使用提示。"""
        await self._ensure_initialized()
        if self._config and self._config.enable_prompt_hint:
            if req.system_prompt:
                req.system_prompt += "\n\n" + SYSTEM_PROMPT_HINT
            else:
                req.system_prompt = SYSTEM_PROMPT_HINT

    # ---- LLM 工具: web_search -----------------------------------------------

    @filter.llm_tool(name="web_search")
    async def web_search(
        self,
        event: AstrMessageEvent,
        query: str,
        max_results: int = 10,
        allowed_domains: Optional[List[str]] = None,
        blocked_domains: Optional[List[str]] = None,
    ):
        """
        搜索网络获取最新信息。当不确定事实、日期、新闻时主动调用。

        Args:
            query(string): 搜索关键词，越精确越好。
            max_results(int): 返回结果数 1-20，默认 10。
            allowed_domains(list, optional): 仅返回指定域名结果，如 ["wikipedia.org"]。
            blocked_domains(list, optional): 排除指定域名结果。与 allowed_domains 互斥。
        """
        await self._ensure_initialized()

        if not query or not query.strip():
            yield event.plain_result("[web_search Error] query 不能为空。")
            return

        max_results = _clamp(max_results, 1, 20)

        if allowed_domains and blocked_domains:
            yield event.plain_result(
                "[web_search Error] allowed_domains 和 blocked_domains 不能同时指定。"
            )
            return

        logger.info(f"web_search: query='{query[:80]}', max_results={max_results}")

        try:
            results = await self._searcher.search(
                query=query.strip(),
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )
        except ImportError as exc:
            yield event.plain_result(f"[web_search Error] {exc}")
            return
        except Exception as exc:
            logger.error(f"web_search exception: {exc}", exc_info=True)
            yield event.plain_result(f"[web_search Error] 搜索失败：{exc}")
            return

        if not results:
            yield event.plain_result(f'未找到与 "{query}" 相关的结果。')
            return

        lines = [f"## Search Results for: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. **{r['title']}**")
            lines.append(f"   {r['url']}")
            if r.get("snippet"):
                snippet = r["snippet"].replace("\n", " ").strip()
                lines.append(f"   > {snippet[:300]}")
            lines.append("")

        yield event.plain_result("\n".join(lines))

    # ---- LLM 工具: web_fetch ------------------------------------------------

    @filter.llm_tool(name="web_fetch")
    async def web_fetch(self, event: AstrMessageEvent, url: str):
        """
        获取网页全文。当搜索结果摘要不够详细时使用。

        Args:
            url(string): 网页完整 URL（含 https://）。
        """
        await self._ensure_initialized()

        if not url or not url.strip():
            yield event.plain_result("[web_fetch Error] url 不能为空。")
            return

        url = url.strip()
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        logger.info(f"web_fetch: url='{url[:120]}'")

        try:
            content = await self._fetcher.fetch(url)
        except Exception as exc:
            logger.error(f"web_fetch exception: {exc}", exc_info=True)
            yield event.plain_result(f"[web_fetch Error] 获取失败：{exc}")
            return

        yield event.plain_result(f"## Content from: {url}\n\n{content}")
