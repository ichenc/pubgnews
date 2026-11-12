# 准时触发器（Cloudflare Worker）

## 为什么需要它

GitHub Actions 的 `schedule` 是**尽力而为**：高峰期会整批延迟，甚至被限流。
本项目实测（2026-11-05 ~ 11-12）虽然 `cron` 写的是每 15 分钟，实际**每 3~10 小时才跑一次**，
这才是「推送延迟太高」的根本原因——不是脚本抓取慢，而是任务根本没被及时调度。

本目录的 Worker 用 Cloudflare 的 Cron Trigger **每 5 分钟**调用一次 GitHub 的
`repository_dispatch` 接口。`dispatch` 不经过 GitHub 的 cron 排队，通常**秒级入队**，
于是工作流能在分钟级被准时拉起；仓库里的 `schedule` 只作为「外部定时器失效」时的兜底。

> 依赖：本仓库的工作流已监听 `repository_dispatch` 的 `pubg-cron` 事件。

---

## 方式 A：用 wrangler 部署（推荐）

需要 Node.js 18+。

```bash
cd scheduler

# 1) 登录（或改用环境变量 CLOUDFLARE_API_TOKEN）
npx wrangler login

# 2) 写入 GitHub 令牌（粘贴你的 PAT；不会进入代码库）
npx wrangler secret put GH_TOKEN

# 3) 部署（自动按 wrangler.toml 里的 crons 设置每 5 分钟触发）
npx wrangler deploy
```

部署成功后，控制台会给出 Worker 地址；用浏览器访问该地址可**立即手动触发一次**做验证。

---

## 方式 B：用 API 部署（无需 Node）

只需要一个 Python 环境和三样东西：

- `CLOUDFLARE_API_TOKEN`：Cloudflare 令牌，需含 **Workers Scripts: Edit** 权限
- `CLOUDFLARE_ACCOUNT_ID`：Cloudflare 账户 ID（控制台右侧可复制）
- `GH_TOKEN`：GitHub 令牌（classic PAT 需 `repo` + `workflow`）

```bash
cd scheduler
export CLOUDFLARE_API_TOKEN=xxxx
export CLOUDFLARE_ACCOUNT_ID=xxxx
export GH_TOKEN=ghp_xxxx
python deploy.py
```

脚本会依次完成：校验令牌 → 上传 Worker → 写入 `GH_TOKEN` 密钥 → 设置每 5 分钟 Cron。

---

## 验证

1. 部署后在 Cloudflare 控制台 **Workers & Pages → pubgnews-scheduler → Triggers** 能看到
   `*/5 * * * *`。
2. 打开仓库 **Actions → PUBG News**，应能看到由 `repository_dispatch` 触发的运行，
   且频率变为约每 5 分钟一次。
3. 想调整频率：改 `wrangler.toml` 的 `crons` 后重新部署；或用方式 B 时改 `deploy.py`
   顶部 `CRON` 常量。

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| `Invalid API Token`（错误码 1000/9109） | 令牌无效或复制不全。请到 Cloudflare「我的个人资料 → API 令牌」重新创建，权限选 **Workers Scripts: Edit**；账户级令牌请确认账户 ID 正确。 |
| 触发返回 401/403 | GitHub 令牌权限不足。classic PAT 需勾选 `repo` 与 `workflow`。 |
| 部署后仍不触发 | 检查 Cron 是否生效（Trigger 页）；或手动访问 Worker 域名看返回。 |
| 推送仍偶发变慢 | 属 GitHub Actions 入队抖动；可把 `crons` 调到每 2~3 分钟，或在仓库加更多兜底 `schedule`。 |

> 安全：`GH_TOKEN` 只通过 `wrangler secret` 或 API 写入 Cloudflare，**不要**写进
> `worker.js` / `wrangler.toml`，也不要提交到仓库。
