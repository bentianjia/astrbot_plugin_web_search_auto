# Web Search Auto - 自动网络搜索

LLM 自主决定何时搜索网络，**无需命令前缀**。默认爬取 Bing（cn.bing.com），国内直连，零部署。

参考 Claude Code 的 WebSearch / WebFetch 工具设计。

## 功能

| 工具 | 说明 |
|------|------|
| `web_search` | 搜索网络，返回标题、URL、简短摘要 |
| `web_fetch` | 获取网页全文，BeautifulSoup 提取正文 |

LLM 自动调用，结果由 LLM 消化总结后回复用户。

## 安装

1. 插件目录放入 `AstrBot/data/plugins/astrbot_plugin_web_search_auto/`
2. WebUI 重载插件，开箱即用（默认 Bing 后端，无需任何配置）

## 搜索后端

| 后端 | 说明 |
|------|------|
| **bing**（默认） | 爬取 cn.bing.com，国内直连，无需代理 |
| duckduckgo | ddgs 库，需 `pip install ddgs` |
| searxng | 自托管 SearXNG API |

## 配置

在 AstrBot WebUI 插件管理页配置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `search_backend` | `bing` | 搜索后端: bing / duckduckgo / searxng |
| `proxy` | `` | HTTP 代理，如 `http://127.0.0.1:7890` |
| `max_results` | `5` | 搜索结果数 (1-20) |
| `cache_ttl` | `900` | 页面缓存秒数 (0=禁用) |
| `cache_max_size` | `100` | 缓存最大条目数 |
| `fetch_timeout` | `15` | 页面获取超时秒数 |
| `fetch_max_chars` | `10000` | 提取正文最大字符数 |
| `enable_prompt_hint` | `true` | 注入系统提示指导 LLM 使用搜索结果 |

## 使用示例

```
用户: LuckPerms 插件怎么用？
LLM:  [自动调用 web_search("LuckPerms Minecraft 权限管理 教程")]
     → LLM 消化搜索结果 → 用自己的话总结回复

用户: 这个链接讲的什么？https://example.com/doc
LLM:  [自动调用 web_fetch("https://example.com/doc")]
     → LLM 读完全文 → 提炼要点回复
```

## 依赖

```
aiohttp beautifulsoup4 lxml
# duckduckgo 后端需要: ddgs
```
