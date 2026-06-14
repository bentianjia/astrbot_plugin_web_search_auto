# Web Search Auto - 自动网络搜索

参考 Claude Code 的 WebSearch / WebFetch 工具设计。

LLM 无需命令前缀，无需 function calling。后台自动搜索，结果注入上下文，LLM 自然筛选回复。

默认爬取 Bing（cn.bing.com），国内直连，零部署。

## 工作原理

```
用户消息 → 插件拦截 → 后台自动搜 → 搜索结果注入 system prompt
        → LLM 基于上下文自然回复（自己筛选、总结、引用）
```

不需要 LLM 支持 function calling，弱模型也能用。

## 安装

1. 插件目录放入 `AstrBot/data/plugins/astrbot_plugin_web_search_auto/`
2. WebUI 重载插件，默认即可用（Bing 直连，无需任何配置）

## 搜索后端

| 后端 | 说明 |
|------|------|
| **bing**（默认） | 爬取 cn.bing.com，国内直连 |
| duckduckgo | ddgs 库，需 `pip install ddgs` |
| searxng | 自托管 SearXNG API |

## 配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `search_backend` | `bing` | bing / duckduckgo / searxng |
| `proxy` | `` | HTTP 代理，如 `http://127.0.0.1:7890` |
| `max_results` | `5` | 注入上下文的搜索结果数 |
| `cache_ttl` | `900` | 页面缓存秒数 (0=禁用) |
| `cache_max_size` | `100` | 缓存最大条目数 |
| `fetch_timeout` | `15` | 页面获取超时秒数 |
| `fetch_max_chars` | `10000` | 提取正文最大字符数 |

## 使用示例

```
用户: LuckPerms 怎么给玩家权限？
→ 插件后台搜 "LuckPerms 怎么给玩家权限"
→ 搜索结果注入 LLM 上下文
→ LLM: LuckPerms 通过 /lp user <玩家> permission set <权限> 来设置...

用户: Python 3.13 什么时候发布的？
→ 自动搜 → LLM 自然回答发布日期和相关特性
```

## 依赖

```
aiohttp beautifulsoup4 lxml
# duckduckgo 后端需额外: ddgs
```
