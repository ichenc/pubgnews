// Cloudflare Worker：每 5 分钟通过 repository_dispatch 触发 pubgnews 工作流。
//
// 为什么需要它：
//   GitHub Actions 的 schedule 是「尽力而为」排队，本仓库实测被限流到每 3~10 小时
//   才运行一次（cron 写的是每 15 分钟），导致推送延迟数小时。
//   repository_dispatch 不经过那套排队，通常秒级入队，因此用外部定时器兜住准时性。
//
// 部署：见同目录 README.md。
// GH_TOKEN 用 `wrangler secret put GH_TOKEN` 写入，切勿写进代码或提交到仓库。

const OWNER = 'ichenc';
const REPO = 'pubgnews';
const EVENT_TYPE = 'pubg-cron';

async function dispatch(env, reason) {
  const res = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}/dispatches`, {
    method: 'POST',
    headers: {
      'Accept': 'application/vnd.github+json',
      'Authorization': `Bearer ${env.GH_TOKEN}`,
      'X-GitHub-Api-Version': '2022-11-28',
      'Content-Type': 'application/json',
      'User-Agent': 'pubgnews-scheduler',
    },
    body: JSON.stringify({
      event_type: EVENT_TYPE,
      client_payload: { source: reason },
    }),
  });
  // 204 = 触发成功
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`GitHub dispatch 失败: HTTP ${res.status} ${text.slice(0, 300)}`);
  }
  return res.status;
}

export default {
  // Cron Trigger 入口
  async scheduled(event, env, ctx) {
    const at = new Date(event.scheduledTime).toISOString();
    try {
      const status = await dispatch(env, 'cloudflare-cron');
      console.log(`[pubgnews] ${at} 触发成功 HTTP ${status}`);
    } catch (e) {
      console.error(`[pubgnews] ${at} 首次触发失败: ${e.message}`);
      // 一次瞬时失败重试；仍失败也没关系，仓库里的 schedule 兜底会补
      try {
        await new Promise((r) => setTimeout(r, 3000));
        const status = await dispatch(env, 'cloudflare-cron-retry');
        console.log(`[pubgnews] ${at} 重试成功 HTTP ${status}`);
      } catch (e2) {
        console.error(`[pubgnews] ${at} 重试仍失败: ${e2.message}`);
      }
    }
  },

  // 手动验证：浏览器访问 Worker 域名即可立刻触发一次
  async fetch(request, env) {
    try {
      const status = await dispatch(env, 'manual');
      return new Response(`dispatched HTTP ${status}\n`, { status: 200 });
    } catch (e) {
      return new Response(`error: ${e.message}\n`, { status: 502 });
    }
  },
};
