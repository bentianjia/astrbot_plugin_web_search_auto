"""
astrbot_plugin_web_search_auto
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

基于 SearXNG 的自动网络搜索插件。
LLM 自主决定何时调用 web_search / web_fetch，无需命令前缀。

参考 Claude Code 的 WebSearch / WebFetch 工具设计：
- web_search: 搜索网络，返回标题 / URL / 摘要
- web_fetch:  获取网页全文，提取正文内容
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
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
    '## Web Search Capability\n'
    'You have two web tools available. Use them PROACTIVELY — '
    'if you are unsure about facts, dates, news, or any real-time information, '
    'search the web rather than guessing.\n\n'
    '1. **web_search(query, max_results=10, allowed_domains, blocked_domains)**\n'
    '   Search the web. Returns title / URL / snippet for each result.\n'
    '2. **web_fetch(url)**\n'
    '   Fetch the full content of a web page for detailed reading.\n\n'
    'Workflow: search first, then fetch specific pages if the snippets are insufficient.\n'
    'Always cite your sources when using information from the web.'
)

DEFAULT_SEARXNG_URL = "http://localhost:8080"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# HTML 标签：提取正文时移除
REMOVE_TAGS = ["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]


# ---------------------------------------------------------------------------
# 配置数据类
# ---------------------------------------------------------------------------

@dataclass
class PluginConfig:
    """插件配置，从 _conf_schema.json / WebUI 加载。"""
    searxng_url: str = DEFAULT_SEARXNG_URL
    max_results: int = 10
    cache_ttl: int = 900          # 秒，0 = 禁用
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
            searxng_url=str(cfg.get("searxng_url", DEFAULT_SEARXNG_URL)).rstrip("/"),
            max_results=_clamp(int(cfg.get("max_results", 10)), 1, 20),
            cache_ttl=max(int(cfg.get("cache_ttl", 900)), 0),
            cache_max_size=max(int(cfg.get("cache_max_size", 100)), 1),
            fetch_timeout=_clamp(int(cfg.get("fetch_timeout", 15)), 3, 60),
            fetch_max_chars=_clamp(int(cfg.get("fetch_max_chars", 10000)), 500, 50000),
            enable_prompt_hint=bool(cfg.get("enable_prompt_hint", True)),
        )


# ---------------------------------------------------------------------------
# 页面缓存
# ---------------------------------------------------------------------------

class PageCache:
    """基于 OrderedDict 的 FIFO 缓存，TTL 到期自动淘汰。"""

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
            self._data.popitem(last=False)  # 淘汰最旧
        self._data[key] = (content, time.monotonic())


# ---------------------------------------------------------------------------
# 搜索 & 获取
# ---------------------------------------------------------------------------

class SearXNGSearcher:
    """SearXNG JSON API 封装。"""

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
        """执行 SearXNG 搜索，返回 [{title, url, snippet}, ...]。"""
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
        except aiohttp.ClientError as exc:
            logger.error(f"SearXNG request failed: {exc}")
            return []
        except asyncio.TimeoutError:
            logger.error("SearXNG request timed out")
            return []

        raw: List[dict] = data.get("results", [])
        if not raw:
            return []

        results: List[Dict[str, str]] = []
        for item in raw:
            url_str = item.get("url", "")
            if not url_str:
                continue

            # 域名过滤
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
        # 1. 查缓存
        cache_key = _cache_key(url)
        if self._cache.enabled:
            cached = await self._cache.get(cache_key)
            if cached is not None:
                return cached

        # 2. HTTP GET
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

        # 3. 提取正文
        if "text/html" in content_type or not content_type:
            text = _html_to_text(html)
        else:
            # 非 HTML：直接返回截断的原文
            text = html.strip()

        if not text:
            return "[Error] No readable content extracted"

        # 4. 截断
        if len(text) > self._max_chars:
            text = text[:self._max_chars] + "\n\n[... content truncated]"

        # 5. 写缓存
        if self._cache.enabled:
            await self._cache.set(cache_key, text)

        return text


# ---------------------------------------------------------------------------
# HTML 工具
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    """用 BeautifulSoup + lxml 提取可读文本。"""
    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        # lxml 失败时回退到 html.parser
        soup = BeautifulSoup(html, "html.parser")

    for tag in soup(REMOVE_TAGS):
        tag.decompose()

    text = soup.get_text(separator="\n", strip=True)
    # 合并多余空行
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return unescape(text)


def _hostname(url: str) -> str:
    """提取 URL 的 hostname，如 https://docs.example.com/path → docs.example.com"""
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def _cache_key(url: str) -> str:
    """生成缓存键（URL 的 SHA256 前 16 位）。"""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(val, hi))


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

@register("web_search_auto", "AstrBot Community", "自动网络搜索（基于 SearXNG）", "1.0.0")
class WebSearchAuto(Star):
    """
    自动网络搜索插件。

    暴露两个 LLM 工具：
    - web_search: 搜索网络
    - web_fetch:  获取网页内容

    LLM 自主判断何时需要搜索，无需用户输入命令前缀。
    参考 Claude Code 的 WebSearch / WebFetch 实现。
    """

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self._config: Optional[PluginConfig] = None
        self._http: Optional[aiohttp.ClientSession] = None
        self._cache: Optional[PageCache] = None
        self._searcher: Optional[SearXNGSearcher] = None
        self._fetcher: Optional[PageFetcher] = None
        self._init_lock = asyncio.Lock()

    # ---- 生命周期 ----------------------------------------------------------

    async def _ensure_initialized(self) -> None:
        """懒初始化：第一次工具调用时创建 session 和子组件。"""
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
            self._searcher = SearXNGSearcher(
                base_url=self._config.searxng_url,
                session=self._http,
            )
            self._fetcher = PageFetcher(
                session=self._http,
                cache=self._cache,
                timeout=self._config.fetch_timeout,
                max_chars=self._config.fetch_max_chars,
            )
            logger.info(
                f"web_search_auto initialized: "
                f"searxng={self._config.searxng_url}, "
                f"cache_ttl={self._config.cache_ttl}s"
            )

    async def terminate(self) -> None:
        """插件卸载时清理。"""
        if self._http:
            await self._http.close()
            self._http = None
        self._config = None
        self._searcher = None
        self._fetcher = None
        self._cache = None
        logger.info("web_search_auto plugin unloaded.")

    # ---- LLM 系统提示注入 --------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req):
        """在每次 LLM 请求前注入搜索工具的使用提示。"""
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
        通过网络搜索获取最新信息。当你不确定某个事实、日期、新闻事件，
        或需要实时数据时，应主动调用此工具。

        Args:
            query(string): 搜索查询词。使用精确、关键词丰富的查询以获得最佳结果。
            max_results(int): 返回的最大结果数量，范围 1-20。默认为 10。
            allowed_domains(list, optional): 返回的结果限定的域名列表，如 ["wikipedia.org"]。
            blocked_domains(list, optional): 排除的域名列表。与 allowed_domains 不可同时使用。
        """
        await self._ensure_initialized()

        if not query or not query.strip():
            yield event.plain_result("[web_search Error] query 不能为空。")
            return

        max_results = _clamp(max_results, 1, 20)

        # allowed_domains 和 blocked_domains 互斥
        if allowed_domains and blocked_domains:
            yield event.plain_result(
                "[web_search Error] allowed_domains 和 blocked_domains 不能同时指定。"
            )
            return

        logger.info(
            f"web_search: query='{query[:80]}', max_results={max_results}"
        )

        try:
            results = await self._searcher.search(
                query=query.strip(),
                max_results=max_results,
                allowed_domains=allowed_domains,
                blocked_domains=blocked_domains,
            )
        except Exception as exc:
            logger.error(f"web_search exception: {exc}", exc_info=True)
            yield event.plain_result(f"[web_search Error] 搜索失败：{exc}")
            return

        if not results:
            yield event.plain_result(f'未找到与 "{query}" 相关的结果。')
            return

        # 格式化为 Markdown
        lines = [f"## Search Results for: {query}", ""]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. **{r['title']}**")
            lines.append(f"   {r['url']}")
            if r.get("snippet"):
                snippet = r["snippet"].replace("\n", " ").strip()
                lines.append(f"   > {snippet[:300]}")
            lines.append("")

        output = "\n".join(lines)
        yield event.plain_result(output)

    # ---- LLM 工具: web_fetch ------------------------------------------------

    @filter.llm_tool(name="web_fetch")
    async def web_fetch(self, event: AstrMessageEvent, url: str):
        """
        获取并阅读一个网页的完整内容。当搜索结果的摘要不够详细、
        需要查看完整文章 / 文档 / 新闻时使用。

        Args:
            url(string): 要获取的网页完整 URL（含 https://）。
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

        output = f"## Content from: {url}\n\n{content}"
        yield event.plain_result(output)
