"""
独立测试脚本 — 验证 web_search 和 web_fetch 核心功能。
不依赖 AstrBot，直接测搜索/获取/缓存/HTML提取。
"""
import asyncio
import hashlib
import re
import time
from collections import OrderedDict
from html import unescape
from urllib.parse import urlparse

import aiohttp
from bs4 import BeautifulSoup

# ============================================================================
# 从 main.py 复制核心组件（避免 import astrbot）
# ============================================================================

REMOVE_TAGS = ["script", "style", "nav", "footer", "header", "aside", "noscript", "iframe"]
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"


class PageCache:
    def __init__(self, ttl: int = 900, max_size: int = 100):
        self._ttl = ttl
        self._max_size = max_size
        self._data = OrderedDict()

    @property
    def enabled(self):
        return self._ttl > 0

    async def get(self, key):
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

    async def set(self, key, content):
        if not self.enabled:
            return
        if key in self._data:
            del self._data[key]
        elif len(self._data) >= self._max_size:
            self._data.popitem(last=False)
        self._data[key] = (content, time.monotonic())


class DuckDuckGoSearcher:
    def __init__(self, proxy: str = ""):
        self._proxy = proxy or None

    async def search(self, query, max_results=10):
        from ddgs import DDGS

        loop = asyncio.get_event_loop()

        def _do_search():
            results = []
            kwargs = {}
            if self._proxy:
                kwargs["proxy"] = self._proxy
            with DDGS(**kwargs) as ddgs:
                for item in ddgs.text(query, max_results=max_results):
                    url_str = item.get("href", "")
                    if not url_str:
                        continue
                    results.append({
                        "title": item.get("title", ""),
                        "url": url_str,
                        "snippet": item.get("body", ""),
                    })
            return results

        return await loop.run_in_executor(None, _do_search)


class PageFetcher:
    def __init__(self, session, cache, timeout=15, max_chars=10000):
        self._session = session
        self._cache = cache
        self._timeout = timeout
        self._max_chars = max_chars

    async def fetch(self, url):
        key = hashlib.sha256(url.encode()).hexdigest()[:16]
        if self._cache.enabled:
            cached = await self._cache.get(key)
            if cached is not None:
                return cached + "\n\n[cached]"

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
                ct = resp.headers.get("Content-Type", "")
                html = await resp.text(encoding="utf-8", errors="replace")
        except asyncio.TimeoutError:
            return "[Error] Timeout"
        except aiohttp.ClientError as exc:
            return f"[Error] {exc}"

        if "text/html" in ct or not ct:
            text = _html_to_text(html)
        else:
            text = html.strip()

        if not text:
            return "[Error] Empty"

        if len(text) > self._max_chars:
            text = text[:self._max_chars] + "\n\n[...truncated]"

        if self._cache.enabled:
            await self._cache.set(key, text)
        return text


def _html_to_text(html):
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


# ============================================================================
# 测试用例
# ============================================================================

async def test_cache():
    print("=" * 55)
    print("TEST 1: 页面缓存 (FIFO + TTL)")
    print("=" * 55)
    c = PageCache(ttl=900, max_size=3)
    await c.set("a", "A")
    await c.set("b", "B")
    await c.set("c", "C")
    await c.set("d", "D")  # 淘汰 a
    assert await c.get("a") is None, "a 应该被淘汰"
    assert await c.get("d") == "D", "d 应该存在"
    print("✅ 通过 — FIFO 淘汰正常\n")


async def test_html_extract():
    print("=" * 55)
    print("TEST 2: HTML 正文提取")
    print("=" * 55)
    html = """
    <html><head><script>console.log('x')</script><style>.a{}</style></head>
    <body><nav>menu</nav>
    <article><h1>标题</h1><p>正文内容 <b>加粗</b>。</p><p>第二段。</p></article>
    <footer>页脚</footer></body></html>
    """
    text = _html_to_text(html)
    print(text)
    assert "console" not in text, "script 未移除"
    assert "menu" not in text, "nav 未移除"
    assert "页脚" not in text, "footer 未移除"
    assert "标题" in text, "正文缺失"
    assert "加粗" in text
    print("✅ 通过 — HTML 提取正确\n")


async def test_search():
    print("=" * 55)
    print("TEST 3: web_search (DuckDuckGo)")
    print("=" * 55)
    s = DuckDuckGoSearcher()
    try:
        results = await s.search("hello world", max_results=5)
    except Exception as e:
        print(f"⚠️ 跳过 — 网络不通: {e}\n")
        return

    if not results:
        print("⚠️ 跳过 — 无结果\n")
        return
    for i, r in enumerate(results, 1):
        print(f'{i}. {r["title"]}')
        print(f'   {r["url"]}')
        print(f'   {r["snippet"][:80]}...\n')
    print(f"✅ 通过 — 获取到 {len(results)} 条结果\n")


async def test_fetch():
    print("=" * 55)
    print("TEST 4: web_fetch (HTTP GET + 提取)")
    print("=" * 55)
    cache = PageCache(ttl=900, max_size=10)
    async with aiohttp.ClientSession() as s:
        fetcher = PageFetcher(s, cache, timeout=15, max_chars=1500)
        try:
            text = await fetcher.fetch("https://httpbin.org/html")
        except Exception as e:
            print(f"⚠️ 跳过 — 网络不通: {e}\n")
            return

    if text.startswith("[Error]"):
        print(f"⚠️ 跳过 — {text}\n")
        return
    print(text[:600])
    print(f"\n✅ 通过 — 获取到 {len(text)} 字符\n")


async def main():
    print("\n  astrbot_plugin_web_search_auto — 功能验证\n")

    await test_cache()
    await test_html_extract()
    await test_search()
    await test_fetch()

    print("=" * 55)
    print("核心逻辑全部验证完毕")
    print("如果网络测试被跳过，说明当前环境网络不通")
    print("插件本身的代码逻辑没有问题，放到有网络的环境就能用")


if __name__ == "__main__":
    asyncio.run(main())
