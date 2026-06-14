# Web Search Auto - 自动网络搜索

基于 [SearXNG](https://github.com/searxng/searxng) 的 AstrBot 插件，LLM 自主决定何时搜索网络，**无需命令前缀**。

参考 Claude Code 的 WebSearch / WebFetch 工具设计。

## 功能

| 工具 | 说明 |
|------|------|
| `web_search` | 搜索网络，返回标题、URL、摘要 |
| `web_fetch` | 获取网页全文，提取正文内容 |

LLM 会自动判断何时需要搜索——用户只需正常聊天，无需打 `/search` 之类的命令。

## 安装

1. 确保有运行中的 SearXNG 实例
2. 将插件目录放入 `AstrBot/data/plugins/astrbot_plugin_web_search_auto/`
3. 重启 AstrBot 或在 WebUI 中重载插件

## 配置

在 AstrBot WebUI 的插件管理页面配置：

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `searxng_url` | `http://localhost:8080` | SearXNG 实例地址 |
| `max_results` | `10` | 搜索返回的最大结果数 (1-20) |
| `cache_ttl` | `900` | 页面缓存有效期（秒，0=禁用） |
| `cache_max_size` | `100` | 缓存最大条目数 |
| `fetch_timeout` | `15` | 页面获取超时（秒） |
| `fetch_max_chars` | `10000` | 提取正文最大字符数 |
| `enable_prompt_hint` | `true` | 注入系统提示帮助 LLM 理解何时搜索 |

## 使用示例

```
用户: 最近有什么 AI 新闻？
LLM:  [自动调用 web_search("latest AI news 2026")]
     → 基于搜索结果回答

用户: 这个链接讲了什么？https://example.com/article
LLM:  [自动调用 web_fetch("https://example.com/article")]
     → 总结文章内容
```

## 依赖

- `aiohttp` — 异步 HTTP
- `beautifulsoup4` + `lxml` — HTML 正文提取
- SearXNG — 搜索后端（自行部署）
