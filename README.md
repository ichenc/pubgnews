# PUBG 新闻 📢

基于 [yutangbb/pubg-news](https://github.com/yutangbb/pubg-news) 二次开发，将 iOS Bark 推送替换为飞书群机器人推送。
获取 [PUBG 官网](https://pubg.com/news) 与 [KRAFTON_GAME 官微](https://weibo.com/u/6037906900) 最新的新闻公告与停机维护通知，支持飞书群机器人发送通知。

---

## ✨ 主要功能

- 🔗 使用官网 API 获取新闻公告列表
- 📱 抓取 KRAFTON_GAME 官微的停机维护公告（通过新浪移动页，无需登录/Cookie）
- 🌐 支持多语言获取并保存到多个 `.json` 文件
- 🔢 自定义获取条数
- 📲 指定语言有更新时通过飞书群机器人推送通知，支持富文本、链接跳转
- 🔍 推送通知支持排除特定关键词，例如"每周违规账号公示"
- 🎯 微博侧仅推送维护类公告，关键词可配置
- ⏲️ GitHub Actions 定时执行，无需自建服务器

---

## 📢 获取新闻公告

此仓库每隔 15 分钟自动更新 4 种语言的新闻公告与微博维护公告，你可以直接通过下面的 URL 获取最新列表。

> ℹ️ 可能无法按照期望的时间获取数据从而导致延迟更新，这取决于 GitHub Actions 任务执行情况。

| 语言/来源 | URL                                                                       |
| ---- | ------------------------------------------------------------------------- |
| 简体中文 | https://raw.githubusercontent.com/ichenc/pubgnews/main/news_zh-cn.json |
| 繁体中文 | https://raw.githubusercontent.com/ichenc/pubgnews/main/news_zh-tw.json |
| 英语   | https://raw.githubusercontent.com/ichenc/pubgnews/main/news_en.json    |
| 韩语   | https://raw.githubusercontent.com/ichenc/pubgnews/main/news_ko.json    |
| 微博维护公告 | https://raw.githubusercontent.com/ichenc/pubgnews/main/news_weibo.json |

### 通过 jsDelivr CDN 访问

| 语言/来源 | URL                                                                 |
| ---- | ------------------------------------------------------------------- |
| 简体中文 | https://cdn.jsdelivr.net/gh/ichenc/pubgnews@main/news_zh-cn.json |
| 繁体中文 | https://cdn.jsdelivr.net/gh/ichenc/pubgnews@main/news_zh-tw.json |
| 英语   | https://cdn.jsdelivr.net/gh/ichenc/pubgnews@main/news_en.json    |
| 韩语   | https://cdn.jsdelivr.net/gh/ichenc/pubgnews@main/news_ko.json    |
| 微博维护公告 | https://cdn.jsdelivr.net/gh/ichenc/pubgnews@main/news_weibo.json |

> ℹ️ 通过 jsDelivr CDN 访问内容有更长时间的更新延迟。

---

## 📱 微博停机维护公告

脚本会额外抓取 [KRAFTON_GAME 官微](https://weibo.com/u/6037906900)（PUBG 官方微博）发布的维护类公告，包括：

- 【正式服停机维护公告】
- 【正式服不停机维护公告】
- （可选）【热补丁修复通知】、维护结束通知等

实现方式：抓取新浪移动媒体页 `https://www.sina.cn/media/{uid}`（服务端渲染，无需登录或 Cookie），按关键词过滤后存入 `news_weibo.json`，有新公告时同样通过飞书机器人推送，卡片会标注"来源：微博 · KRAFTON_GAME"。

---

## ⚙️ 可配置项

所有配置通过 GitHub Actions 环境变量（Secrets）传入，无需修改代码。

| 环境变量 | 说明 | 默认值 |
| --- | --- | --- |
| `FEISHU_WEBHOOK_URL` | 飞书群机器人 Webhook 地址（必填） | 空 |
| `FEISHU_SECRET` | 飞书机器人签名校验密钥（没开签名就留空） | 空 |
| `PUSH_LANG` | 推送通知使用的语言，设为空则不推送 | `zh-cn` |
| `FETCH_SIZE` | 每次获取最新条数，上限 50 | `10` |
| `EXCLUDE_KEYWORDS` | 排除关键词，英文逗号分隔 | `每周违规账号公示,封禁公告,Weekly Bans Notice,Bans Notice` |
| `PUSH_TITLE` | 飞书消息标题 | `🎮 PUBG 新公告` |
| `ENABLE_WEIBO` | 是否抓取微博维护公告（`true`/`false`） | `true` |
| `WEIBO_UID` | KRAFTON 官微 UID | `6037906900` |
| `WEIBO_KEYWORDS` | 微博仅推送含这些关键词的内容，逗号分隔。想连热补丁/维护结束通知一起推，可设为 `维护公告,热补丁,停机维护已结束` | `维护公告` |

### 分多种语言获取

```python
LANGUAGES = ["zh-cn", "zh-tw", "en", "ko"]
