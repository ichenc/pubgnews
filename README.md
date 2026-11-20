# PUBG 公告助手 📢

监控 PUBG 官网新闻、KRAFTON_GAME 官微维护公告、PC 服务器状态，第一时间用**飞书互动卡片**推送到群里。
由 GitHub Actions + Cloudflare 定时触发，无需自建服务器。

## 监控内容

| 来源 | 抓什么 | 抓法 |
|---|---|---|
| [官网新闻](https://www.pubg.com/zh-cn/news?label=label_pc) | `label_pc` 全部公告，**一条不漏** | 官方 API，每页 50 条、自动翻页到已知记录为止 |
| [KRAFTON_GAME 官微](https://weibo.com/u/6037906900) | 必推：正式服停机 / 不停机维护公告<br>可选：热补丁、维护结束、开服等 | 新浪移动页（无需 Cookie），按【】标题判定 |
| [pubg.plus](https://pubg.plus/zh-CN/status) | PC 服务器状态、在线人数 | 状态接口（HMAC 签名） |

**推送规则**

- 官网：新增即推，按 `postId` 去重，重复触发不会重复推；默认排除「每周违规账号公示」。
- 官微：只推维护类，重点公告橙色表头、通知类灰色表头。
- 状态：进入维护 🟡 / 维护结束 🟢 / 连接中断 🔴 / 连接恢复 🟢，并附带当前在线人数与近期趋势。
- 卡片配色：官网=蓝、官微重点=橙、官微通知=灰、异常=红、恢复=绿。
- 时间统一按**北京时间（UTC+8）**展示：官网接口返回的是 UTC，仅在展示层转换，缓存仍存原值以保证排序一致。

## 防误报（重要）

服务器状态**连续 8 轮**（每 2 分钟一轮，约 16 分钟）保持同一异常才推送，避免瞬时抖动被拆成一红一绿两条误报；
恢复通知只在之前推送过对应异常时发出；首次运行只记录基线、不推送。

## 数据文件

每 2 分钟更新一次，可直接引用：

| 内容 | 地址 |
|---|---|
| 简体中文 | `https://raw.githubusercontent.com/ichenc/pubgnews/main/news_zh-cn.json` |
| 繁体中文 | `https://raw.githubusercontent.com/ichenc/pubgnews/main/news_zh-tw.json` |
| 英语 | `https://raw.githubusercontent.com/ichenc/pubgnews/main/news_en.json` |
| 韩语 | `https://raw.githubusercontent.com/ichenc/pubgnews/main/news_ko.json` |
| 微博维护公告 | `https://raw.githubusercontent.com/ichenc/pubgnews/main/news_weibo.json` |

CDN 加速：把 `raw.githubusercontent.com/ichenc/pubgnews/main/` 换成 `cdn.jsdelivr.net/gh/ichenc/pubgnews@main/`（有额外缓存延迟）。

## 定时机制

GitHub 的 `schedule` 是「尽力而为」，实测会被限流到 3~10 小时一次。因此用 Cloudflare Worker
每 2 分钟调一次 `repository_dispatch`（事件 `pubg-cron`）作为准时层，工作流里的 `schedule` 仅兜底。
部署与排错见 [`scheduler/README.md`](./scheduler/README.md)。

## 配置项

全部通过环境变量传入，无需改代码。

| 变量 | 说明 | 默认 |
|---|---|---|
| `FEISHU_WEBHOOK_URL` | 飞书机器人 Webhook（必填） | — |
| `FEISHU_SECRET` | 签名校验密钥（未开启签名则留空） | 空 |
| `PUSH_LANG` | 推送语言，空表示不推官网 | `zh-cn` |
| `NEWS_LABELS` | 官网标签过滤，空表示全部 | `label_pc` |
| `FETCH_SIZE` | 每页条数（上限 100） | `50` |
| `NEWS_MAX_PAGES` | 最多翻页数 | `4` |
| `HISTORY_LIMIT` | 本地缓存条数（去重窗口） | `300` |
| `MAX_PUSH_PER_RUN` | 单轮最多推送条数，防刷屏 | `20` |
| `EXCLUDE_KEYWORDS` | 标题排除词，英文逗号分隔；设为空则一条不漏 | 每周违规账号公示等 |
| `ENABLE_WEIBO` | 是否抓官微 | `true` |
| `WEIBO_UID` | 官微 UID | `6037906900` |
| `ENABLE_WEIBO_OPTIONAL` | 是否推热补丁/维护结束等次要通知 | `true` |
| `ENABLE_STATUS` | 是否监控服务器状态 | `true` |
| `STATUS_CONFIRM_POLLS` | 状态需连续确认的轮数 | `3` |
| `ONLINE_SAMPLE_MINUTES` | 在线人数采样间隔（分钟） | `60` |
| `PUBG_STATUS_SECRET` | 状态接口密钥，仅在其更新导致 401 时覆盖 | 内置 |

## 二次开发

基于 [yutangbb/pubg-news](https://github.com/yutangbb/pubg-news) 改造，把 Bark 推送换成飞书。

```bash
pip install requests
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/open-apis/bot/v2/hook/xxx"
python pubg_news.py
```
