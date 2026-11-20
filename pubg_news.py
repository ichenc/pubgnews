#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUBG 公告监控 → 飞书推送

数据源：
  1) 官网新闻  https://www.pubg.com/zh-cn/news?label=label_pc  （分页抓取，不遗漏）
  2) KRAFTON 官微  正式服停机/不停机维护公告（可选热补丁、维护结束等）
  3) pubg.plus  PC 服务器状态与在线人数

由 GitHub Actions + Cloudflare 定时触发，无需自建服务器。
"""

import base64
import hashlib
import hmac
import html as html_lib
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

import requests

# ============================================================
#  配置区（部署时全部通过 Secrets 环境变量传入）
# ============================================================

# 飞书自定义机器人 Webhook（必填）
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "").rstrip("/")
# 飞书签名校验密钥（可选）
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")

# 获取新闻语言列表（分别存档为 news_{lang}.json）
LANGUAGES = ["zh-cn", "zh-tw", "en", "ko"]
# 推送语言；空字符串表示不推送官网新闻
PUSH_LANG = os.getenv("PUSH_LANG", "zh-cn")

# 官网抓取：每页条数、最多翻几页、标签过滤
FETCH_SIZE = min(int(os.getenv("FETCH_SIZE", "50")), 100)
NEWS_MAX_PAGES = max(1, int(os.getenv("NEWS_MAX_PAGES", "4")))
NEWS_LABELS = os.getenv("NEWS_LABELS", "label_pc").strip()
# 本地缓存每个语言保留多少条历史（用于去重，过小会导致旧新闻被反复推送）
HISTORY_LIMIT = max(50, int(os.getenv("HISTORY_LIMIT", "300")))
# 单轮最多推送条数（超过则判定为缓存缺口的积压，只补历史不推送）
MAX_PUSH_PER_RUN = max(1, int(os.getenv("MAX_PUSH_PER_RUN", "20")))

# 版本标记：首次运行或逻辑升级后的第一轮只建立基线，不把历史存量当新闻推出去
STATE_FILE = "pubg_news_state.json"
SCHEMA_VERSION = 2

# 推送排除：标题命中以下关键词则不推送。
# 显式设置 EXCLUDE_KEYWORDS（可为空）即可覆盖默认值，做到「一条不漏」。
EXCLUDE_KEYWORDS = ["每周违规账号公示", "封禁公告", "Weekly Bans Notice", "Bans Notice"]
if "EXCLUDE_KEYWORDS" in os.environ:
    EXCLUDE_KEYWORDS = [k.strip() for k in os.environ["EXCLUDE_KEYWORDS"].split(",") if k.strip()]

# 飞书卡片标题；留空则按类别自动选择
PUSH_TITLE = os.getenv("PUSH_TITLE", "").strip()

# 脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 北京时间（Runner 默认 UTC，不统一会混淆状态时间与微博时间）
TZ_CN = timezone(timedelta(hours=8))


def now_cn():
    """当前北京时间（中国无夏令时，固定 UTC+8）"""
    return datetime.now(TZ_CN)


# 复用连接池
SESSION = requests.Session()

# 网络重试
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
HTTP_BACKOFF = 2

# PUBG 官网公开接口
PUBG_API = "https://api-foc.krafton.com/content/post/news"

# ============================================================
#  微博（KRAFTON_GAME 官微）
# ============================================================
ENABLE_WEIBO = os.getenv("ENABLE_WEIBO", "true").lower() in ("1", "true", "yes")
WEIBO_UID = os.getenv("WEIBO_UID", "6037906900")
WEIBO_MEDIA_URL = f"https://www.sina.cn/media/{WEIBO_UID}"
WEIBO_LANG = "weibo"

# 核心公告：必推
WEIBO_CORE_KEYWORDS = ["停机维护公告", "不停机维护公告"]
# 可选公告：热补丁、维护结束、开服等
WEIBO_OPTIONAL_KEYWORDS = ["热补丁", "维护结束", "维护已结束", "开服", "问题已修复", "修复通知"]
ENABLE_WEIBO_OPTIONAL = os.getenv("ENABLE_WEIBO_OPTIONAL", "true").lower() in ("1", "true", "yes")

# ============================================================
#  PUBG 服务器实时状态（pubg.plus）
# ============================================================
ENABLE_STATUS = os.getenv("ENABLE_STATUS", "true").lower() in ("1", "true", "yes")
PUBG_STATUS_API = "https://apiv1.pubg.plus/status/server"
PUBG_STATUS_SECRET = os.getenv("PUBG_STATUS_SECRET", "Bm4is8qQgJXdocrvobbFR7u2kh66Pu")
STATUS_CACHE_FILE = "pubg_status_state.json"

# 状态变化需连续确认的轮数：默认 8 轮。
# 定时器每 2 分钟一次，即“连续约 16 分钟都异常”才推送，
# 从而过滤掉「14:06 中断 → 14:15 恢复」这类瞬时抖动误报。
# 注意：轮数要跟着触发频率一起调，否则提高频率会重新引入误报。
STATUS_CONFIRM_POLLS = max(1, int(os.getenv("STATUS_CONFIRM_POLLS", "8")))
# 在线人数采样间隔（分钟）与保留点数（24 点 = 约 1 天）
ONLINE_SAMPLE_MINUTES = max(5, int(os.getenv("ONLINE_SAMPLE_MINUTES", "60")))
ONLINE_HISTORY_SIZE = 24

# ============================================================
#  工具函数
# ============================================================


def log(msg):
    """打印带北京时间戳的日志"""
    print(f"[{now_cn().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def http_get(url, retries=HTTP_RETRIES, backoff=HTTP_BACKOFF, **kwargs):
    """带指数退避重试的 GET；网络抖动不应导致整轮漏抓"""
    retries = max(1, int(retries))
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            resp = SESSION.get(url, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as e:
            last_exc = e
            if attempt < retries:
                wait = backoff ** attempt
                log(f"⚠️ 请求失败（第 {attempt}/{retries} 次），{wait}s 后重试：{e}")
                time.sleep(wait)
    raise last_exc


def gen_feishu_sign(secret, timestamp):
    """飞书机器人签名"""
    string_to_sign = f"{timestamp}\n{secret}"
    digest = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def _fmt_int(value):
    """整数千分位；非整数原样返回"""
    if isinstance(value, bool) or not isinstance(value, int):
        return str(value)
    return f"{value:,}"


# ============================================================
#  官网新闻
# ============================================================


def _news_headers(lang):
    return {
        "Origin": "https://pubg.com",
        "Referer": "https://pubg.com/",
        "Service-Game": "pubg",
        "Service-Lang": lang,
        "Service-Namespace": "PUBG_OFFICIAL",
        "Service-Url": f"https://pubg.com/{lang}/news",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    }


def _news_source(labels):
    """按 label 给出来源描述，用于卡片元信息"""
    has_pc = "label_pc" in labels
    has_console = "label_console" in labels
    if has_pc and has_console:
        return "官网 · PC / 主机"
    if has_pc:
        return "官网 · PC"
    if has_console:
        return "官网 · 主机"
    return "官网"


def _normalize_post(post, lang):
    images = post.get("images") or []
    first = images[0] if images else {}
    post_id = str(post.get("postId", ""))
    labels = post.get("labels") or []
    return {
        "title": (post.get("title") or "").strip(),
        "summary": post.get("summary") or "",
        "postId": post_id,
        "category": post.get("category", ""),
        "labels": labels,
        "createdAt": post.get("createdAt", ""),
        "displayTime": post.get("displayStartTime", ""),
        "imageUrl": first.get("imageUrl", ""),
        "thumbUrl": first.get("thumbUrl", ""),
        "source": _news_source(labels),
        "newsUrl": f"https://pubg.com/{lang}/news/{post_id}",
    }


def fetch_news(lang, known_ids=None):
    """
    抓取指定语言的官网新闻。

    为保证「一条都不漏」，这里会翻页：
      - 传入 known_ids（已知 postId）时，翻到出现已知条目为止，最多 NEWS_MAX_PAGES 页；
      - 不传则只取第一页（用于非推送语言的存档）。
    单页条数 FETCH_SIZE（默认 50），远大于 2 分钟内的发布量。
    """
    items = []
    seen = set()

    for page in range(1, NEWS_MAX_PAGES + 1):
        params = {
            "lang": lang,
            "displayLocationType": "NORMAL",
            "size": FETCH_SIZE,
            "page": page,
        }
        if NEWS_LABELS:
            params["label"] = NEWS_LABELS

        resp = http_get(PUBG_API, headers=_news_headers(lang), params=params, timeout=30)
        posts = resp.json().get("_embedded", {}).get("post", [])
        if not posts:
            break

        hit_known = False
        for post in posts:
            item = _normalize_post(post, lang)
            if not item["postId"] or item["postId"] in seen:
                continue
            seen.add(item["postId"])
            items.append(item)
            if known_ids and item["postId"] in known_ids:
                hit_known = True

        # 只抓一页 / 已翻到已知区域 / 已是最后一页
        if known_ids is None or hit_known or len(posts) < FETCH_SIZE:
            break

    items.sort(key=lambda x: x.get("displayTime", ""), reverse=True)
    return items


# ============================================================
#  KRAFTON 官微
# ============================================================


def _clean_html(text):
    """去标签、反转义、清掉零宽字符与多余空白"""
    text = html_lib.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    text = text.replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _extract_weibo_title(body):
    """标题：优先取【...】内容，否则取开头 30 字"""
    m = re.match(r"^[【\[](.+?)[】\]]", body)
    if m:
        return m.group(1).strip()
    return body[:30].strip()


def _parse_sina_time(raw, base=None):
    """
    新浪移动页时间文本 → "YYYY-MM-DD HH:MM:SS"（北京时间）。

    兼容：刚刚 / N分钟前 / N小时前 / N天前 / 今天·昨天·前天 HH:MM /
          裸 HH:MM / MM-DD HH:MM / YYYY-MM-DD HH:MM[:SS]。
    无法解析返回 None，由调用方兜底为当前时间。
    """
    if not raw:
        return None
    s = raw.strip()
    now = base or now_cn()

    m = re.match(r"^(\d+)\s*秒前$", s)
    if m:
        return (now - timedelta(seconds=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
    m = re.match(r"^(\d+)\s*分钟前$", s)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
    m = re.match(r"^(\d+)\s*小时前$", s)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
    m = re.match(r"^(\d+)\s*天前$", s)
    if m:
        return (now - timedelta(days=int(m.group(1)))).strftime("%Y-%m-%d %H:%M:%S")
    if s == "刚刚":
        return now.strftime("%Y-%m-%d %H:%M:%S")

    m = re.match(r"^(今天|昨天|前天)\s*(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", s)
    if m:
        days = {"今天": 0, "昨天": 1, "前天": 2}[m.group(1)]
        d = (now - timedelta(days=days)).replace(
            hour=int(m.group(2)), minute=int(m.group(3)),
            second=int(m.group(4)) if m.group(4) else 0, microsecond=0,
        )
        return d.strftime("%Y-%m-%d %H:%M:%S")

    m = re.match(r"^(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", s)
    if m:
        d = now.replace(
            hour=int(m.group(1)), minute=int(m.group(2)),
            second=int(m.group(3)) if m.group(3) else 0, microsecond=0,
        )
        return d.strftime("%Y-%m-%d %H:%M:%S")

    m = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", s)
    if m:
        d = now.replace(
            month=int(m.group(1)), day=int(m.group(2)),
            hour=int(m.group(3)), minute=int(m.group(4)),
            second=int(m.group(5)) if m.group(5) else 0, microsecond=0,
        )
        return d.strftime("%Y-%m-%d %H:%M:%S")

    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?$", s)
    if m:
        try:
            d = now.replace(
                year=int(m.group(1)), month=int(m.group(2)), day=int(m.group(3)),
                hour=int(m.group(4)), minute=int(m.group(5)),
                second=int(m.group(6)) if m.group(6) else 0, microsecond=0,
            )
        except ValueError:
            return None
        return d.strftime("%Y-%m-%d %H:%M:%S")

    return None


def _classify_weibo(body):
    """
    判定微博类型：core（正式服维护公告）/ optional（热补丁、维护结束等）/ None。
    只在正文开头 120 字里匹配，避免长正文里偶然出现关键词造成误判。
    """
    head = body[:120]
    if any(kw in head for kw in WEIBO_CORE_KEYWORDS):
        return "core"
    if ENABLE_WEIBO_OPTIONAL and any(kw in head for kw in WEIBO_OPTIONAL_KEYWORDS):
        return "optional"
    return None


def fetch_weibo():
    """抓取官微，只保留维护类公告，结构对齐官网新闻"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/15.0 Mobile/15E148 Safari/604.1"
        )
    }
    resp = http_get(WEIBO_MEDIA_URL, headers=headers, timeout=30)
    page = resp.content.decode("utf-8", errors="ignore")

    pattern = re.compile(
        r'<a class="post-link" href="(/news/detail/(\d+)\.html)"[^>]*>'
        r".*?<div class=\"time\">([^<]+)<span"
        r".*?<div class=\"post-text\">(.*?)</div>",
        re.S,
    )

    items = []
    seen = set()
    for m in pattern.finditer(page):
        _href, oid, time_str, raw_body = m.groups()
        oid = oid.strip()
        if oid in seen:
            continue
        seen.add(oid)

        body = _clean_html(raw_body)
        priority = _classify_weibo(body)
        if not priority:
            continue

        display_time = _parse_sina_time(time_str)
        if not display_time:
            display_time = now_cn().strftime("%Y-%m-%d %H:%M:%S")
            log(f"⚠️ 微博时间解析失败，使用当前时间兜底：{time_str!r} | {body[:30]}")

        items.append({
            "title": _extract_weibo_title(body),
            "summary": body,
            "postId": oid,
            "category": "weibo",
            "labels": ["weibo"],
            "priority": priority,
            "createdAt": display_time,
            "displayTime": display_time,
            "imageUrl": "",
            "thumbUrl": "",
            "source": "微博 · KRAFTON_GAME",
            "extra_lines": ["📌 正式服维护公告（重点）" if priority == "core" else "ℹ️ 维护相关通知"],
            "newsUrl": f"https://m.weibo.cn/status/{oid}",
        })

    items.sort(key=lambda x: x["displayTime"], reverse=True)
    return items


# ============================================================
#  PUBG 服务器状态（pubg.plus）
# ============================================================


def _status_sign(secret):
    """签名：HMAC-SHA256("status"+ts, secret) 取前 16 位 hex"""
    ts = str(int(time.time()))
    msg = ("status" + ts).encode("utf-8")
    return ts, hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]


def fetch_pubg_status():
    """拉取服务器状态，返回 {"pc": {...}, "console": {...}, "updated_at": ...}"""
    ts, sign = _status_sign(PUBG_STATUS_SECRET)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Origin": "https://pubg.plus",
        "Referer": "https://pubg.plus/",
    }
    resp = http_get(PUBG_STATUS_API, params={"ts": ts, "sign": sign}, headers=headers, timeout=20)
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"状态 API 返回错误：{body.get('message')}")
    return body.get("data", {})


def _online_lines(history, online):
    """把在线人数采样转成卡片附加行（当前 / 峰值均值 / 变化）"""
    if not isinstance(online, int):
        return ["👥 当前在线：未知"]
    lines = [f"👥 当前在线：{_fmt_int(online)} 人"]
    if len(history) >= 2:
        vals = [h["v"] for h in history if isinstance(h.get("v"), int)]
        if vals:
            lines.append(
                f"📈 近 {len(vals)} 次采样：峰值 {_fmt_int(max(vals))} · 均值 {_fmt_int(sum(vals) // len(vals))}"
            )
        prev = history[-2]["v"]
        delta = online - prev
        sign = "+" if delta >= 0 else "-"
        lines.append(f"↕️ 较上次采样：{sign}{_fmt_int(abs(delta))} 人")
    return lines


def _build_status_events(old, new, pc, history, announced):
    """
    由「已确认的旧状态」与「新状态」生成要推送的事件。
    返回 (events, announced_dims)。
    """
    events, dims = [], set()
    now_str = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    ts = int(time.time())
    online = pc.get("online")
    extra = _online_lines(history, online)

    old_m, new_m = old["maintenance"], new["maintenance"]
    old_s, new_s = old["server_status"], new["server_status"]

    # ---- 维护状态：仅在 1=正常 ↔ 2=维护中 之间切换时推送 ----
    if old_m != new_m and {old_m, new_m} == {1, 2}:
        if new_m == 2:
            events.append({
                "title": "🟡 PUBG PC 服务器进入维护",
                "summary": "PUBG PC 服务器已开始维护，维护期间可能无法登录或匹配。",
                "postId": f"status_pc_maint_{new_m}_{ts}",
                "category": "status",
                "severity": _CARD_TEMPLATE_ORANGE,
                "source": "服务器状态 · pubg.plus",
                "displayTime": now_str,
                "newsUrl": "https://pubg.plus/zh-CN/status",
                "extra_lines": list(extra),
            })
            dims.add("maintenance")
        elif announced.get("maintenance") == old_m:
            # 只有之前推送过「开始维护」，才推送「维护结束」，避免无来由的恢复通知
            events.append({
                "title": "🟢 PUBG PC 服务器维护结束",
                "summary": "PUBG PC 服务器维护已结束，可以重新启动客户端进入游戏。",
                "postId": f"status_pc_maint_{new_m}_{ts}",
                "category": "status",
                "severity": _CARD_TEMPLATE_GREEN,
                "source": "服务器状态 · pubg.plus",
                "displayTime": now_str,
                "newsUrl": "https://pubg.plus/zh-CN/status",
                "extra_lines": list(extra),
            })
            dims.add("maintenance")

    # ---- 连接状态：仅在 1=正常 ↔ 0=离线 之间切换时推送 ----
    # 维护期间 server_status=2 属正常现象，故不匹配 {0,1}，不会误报
    if old_s != new_s and {old_s, new_s} == {0, 1}:
        if new_s == 0:
            events.append({
                "title": "🔴 PUBG PC 服务器连接中断",
                "summary": "PUBG PC 服务器连接状态异常（可能崩溃或网络波动），请稍后重试。",
                "postId": f"status_pc_conn_{new_s}_{ts}",
                "category": "status",
                "severity": _CARD_TEMPLATE_RED,
                "source": "服务器状态 · pubg.plus",
                "displayTime": now_str,
                "newsUrl": "https://pubg.plus/zh-CN/status",
                "extra_lines": list(extra),
            })
            dims.add("server_status")
        elif announced.get("server_status") == old_s:
            events.append({
                "title": "🟢 PUBG PC 服务器连接恢复",
                "summary": "PUBG PC 服务器连接已恢复。",
                "postId": f"status_pc_conn_{new_s}_{ts}",
                "category": "status",
                "severity": _CARD_TEMPLATE_GREEN,
                "source": "服务器状态 · pubg.plus",
                "displayTime": now_str,
                "newsUrl": "https://pubg.plus/zh-CN/status",
                "extra_lines": list(extra),
            })
            dims.add("server_status")

    return events, dims


def update_status(old_state, new_state):
    """
    状态机：只有「连续 STATUS_CONFIRM_POLLS 轮」都保持的新状态才被确认并推送。

    这样能过滤掉瞬时抖动（例如截图里的 14:06 中断 → 14:15 恢复），
    避免一条真实故障被拆成一红一绿两条误报。
    返回 (events, new_state)。
    """
    pc = new_state.get("pc") or {}
    cur = {"maintenance": pc.get("maintenance"), "server_status": pc.get("server_status")}
    valid = (0, 1, 2)
    state = dict(old_state or {})

    # ---- 在线人数采样（固定间隔，避免每轮都写盘）----
    history = list(state.get("online_history") or [])
    online = pc.get("online")
    if isinstance(online, int):
        now = now_cn()
        due = True
        if history:
            try:
                last_t = datetime.strptime(history[-1]["t"], "%Y-%m-%d %H:%M").replace(tzinfo=TZ_CN)
                due = (now - last_t) >= timedelta(minutes=ONLINE_SAMPLE_MINUTES)
            except Exception:
                due = True
        if due:
            history.append({"t": now.strftime("%Y-%m-%d %H:%M"), "v": online})
            history = history[-ONLINE_HISTORY_SIZE:]
    state["online_history"] = history
    state["pc"] = {
        "maintenance": cur["maintenance"],
        "server_status": cur["server_status"],
        "client_version": pc.get("client_version"),
    }
    state["updated_at"] = new_state.get("updated_at", "")

    # ---- 兼容旧格式：从 pc 派生已确认基线 ----
    stable = state.get("stable")
    if not stable:
        old_pc = state.get("pc") or {}
        if old_pc.get("maintenance") in valid and old_pc.get("server_status") in valid:
            stable = {"maintenance": old_pc["maintenance"], "server_status": old_pc["server_status"]}

    # ---- 状态值异常：本轮不参与判定 ----
    if cur["maintenance"] not in valid or cur["server_status"] not in valid:
        log(f"⚠️ 状态值异常，本轮跳过：{cur}")
        if stable:
            state["stable"] = stable
        return [], state

    # ---- 首次运行：只记录基线 ----
    if not stable:
        state["stable"] = cur
        log(f"ℹ️ 首次运行，记录基线（不推送）：{cur}")
        return [], state

    # ---- 与已确认状态一致：清空待确认计数 ----
    if cur == stable:
        state["stable"] = stable
        state.pop("pending", None)
        return [], state

    # ---- 有差异：累计连续轮数 ----
    pending = state.get("pending") or {}
    count = int(pending.get("count", 0)) + 1 if pending.get("candidate") == cur else 1
    if count < STATUS_CONFIRM_POLLS:
        state["stable"] = stable
        state["pending"] = {
            "candidate": cur,
            "count": count,
            "since": pending.get("since") or now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        }
        log(f"⏳ 状态变化待确认（{count}/{STATUS_CONFIRM_POLLS}）：{stable} → {cur}")
        return [], state

    # ---- 连续多轮确认：生成推送 ----
    announced = dict(state.get("last_announced") or {})
    events, dims = _build_status_events(stable, cur, pc, history, announced)
    for dim in dims:
        announced[dim] = cur[dim]
    state["last_announced"] = announced
    state["stable"] = cur
    state.pop("pending", None)

    if not dims:
        log(f"ℹ️ 状态已确认但无需推送：{stable} → {cur}")
    return events, state


def load_status_state():
    filename = os.path.join(SCRIPT_DIR, STATUS_CACHE_FILE)
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_status_state(state):
    """仅在内容实质变化时写文件，避免高频触发产生无意义提交"""
    filename = os.path.join(SCRIPT_DIR, STATUS_CACHE_FILE)
    payload = {k: v for k, v in state.items() if k != "saved_at"}

    old = None
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                old = {k: v for k, v in json.load(f).items() if k != "saved_at"}
        except Exception:
            old = None

    if old == payload:
        return

    payload["saved_at"] = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# ============================================================
#  本地缓存
# ============================================================


def load_existing(lang):
    """读取已保存列表（用于增量去重），并做旧格式迁移"""
    filename = os.path.join(SCRIPT_DIR, f"news_{lang}.json")
    if not os.path.exists(filename):
        return []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            items = json.load(f)
    except Exception:
        return []

    for item in items:
        item["postId"] = str(item.get("postId", ""))
        if item.get("category") == "weibo" and "sina.cn" in item.get("newsUrl", ""):
            item["newsUrl"] = f"https://m.weibo.cn/status/{item['postId']}"
    return items


def save_news(lang, news_items):
    filename = os.path.join(SCRIPT_DIR, f"news_{lang}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(news_items, f, ensure_ascii=False, indent=4)
    log(f"💾 保存 news_{lang}.json，共 {len(news_items)} 条")


def merge_news(existing, new):
    """合并去重（postId 按字符串比较），按时间倒序，保留 HISTORY_LIMIT 条"""
    merged_map = {str(item["postId"]): item for item in existing}
    for item in new:
        merged_map[str(item["postId"])] = item
    merged = list(merged_map.values())
    merged.sort(key=lambda x: x.get("displayTime", ""), reverse=True)
    return merged[:HISTORY_LIMIT]


def load_bot_state():
    """读取运行状态（版本标记等）"""
    filename = os.path.join(SCRIPT_DIR, STATE_FILE)
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_bot_state(state):
    filename = os.path.join(SCRIPT_DIR, STATE_FILE)
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ============================================================
#  飞书互动卡片
# ============================================================

_CARD_TEMPLATE_BLUE = "blue"
_CARD_TEMPLATE_ORANGE = "orange"
_CARD_TEMPLATE_RED = "red"
_CARD_TEMPLATE_GREEN = "green"
_CARD_TEMPLATE_GREY = "grey"
_VALID_TEMPLATES = frozenset([
    "blue", "orange", "red", "green", "wathet",
    "grey", "violet", "indigo", "turquoise",
])

_CATEGORY_TITLES = {
    "weibo": "🛠️ PUBG 维护公告",
    "status": "📡 PUBG 服务器状态",
}


def _escape_markdown(text):
    """转义 lark_md 特殊字符，避免标题里的符号破坏排版"""
    return (
        text.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _truncate(text, limit):
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _card_template(news):
    """表头配色：状态消息优先用 severity；微博重点公告橙色，通知类灰色"""
    severity = news.get("severity")
    if severity in _VALID_TEMPLATES:
        return severity
    if news.get("category") == "weibo":
        return _CARD_TEMPLATE_ORANGE if news.get("priority") == "core" else _CARD_TEMPLATE_GREY
    return _CARD_TEMPLATE_BLUE


def _header_title(news):
    if PUSH_TITLE:
        return PUSH_TITLE
    return _CATEGORY_TITLES.get(news.get("category", ""), "🎮 PUBG 新公告")


def _link_text(news):
    category = news.get("category", "")
    if category == "weibo":
        return "查看微博原文"
    if category == "status":
        return "查看状态面板"
    return "查看官网详情"


def _utc_to_cn(text):
    """
    官网接口返回的 displayStartTime 是 UTC（不带时区标记），
    直接展示会比实际时间早 8 小时，看起来像「延迟了 8 小时」。
    这里统一转成北京时间用于卡片展示；缓存中仍保留 UTC 原值，保证排序一致。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    try:
        dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return raw
    return (dt + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def build_interactive_card(news):
    """把一条公告组装成飞书互动卡片"""
    title = (news.get("title") or "").strip()
    summary = (news.get("summary") or "").strip()
    source = news.get("source") or news.get("category") or ""
    display_time = news.get("displayTime") or ""
    # 官网新闻存的是 UTC；微博/服务器状态本身已是北京时间
    if display_time and news.get("category") not in ("weibo", "status"):
        display_time = _utc_to_cn(display_time)
    news_url = news.get("newsUrl") or ""
    image_url = news.get("imageUrl") or news.get("thumbUrl") or ""

    elements = []

    if title:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{_escape_markdown(title)}**"},
        })

    meta = []
    if source:
        meta.append(f"🏷️ 来源：{_escape_markdown(source)}")
    if display_time:
        meta.append(f"🕒 时间：{display_time}")
    if meta:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "  \n".join(meta)},
        })

    if image_url:
        elements.append({
            "tag": "img",
            "img_path": image_url,
            "alt": {"tag": "plain_text", "content": title or "cover"},
            "mode": "fit_horizontal",
            "corner_radius": 8,
        })

    if summary:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": _truncate(_escape_markdown(summary), 500)},
        })

    extra_lines = news.get("extra_lines") or []
    if extra_lines:
        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "  \n".join(_escape_markdown(str(x)) for x in extra_lines),
            },
        })

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": f"👉 {_link_text(news)}"},
            "type": "primary",
            "url": news_url,
        }],
    })
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "🤖 PUBG 公告助手 · 自动推送"}],
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": _header_title(news)},
            "template": _card_template(news),
        },
        "elements": elements,
    }
    return {"msg_type": "interactive", "card": card}


def send_feishu(news):
    """推送飞书卡片；网络异常重试，业务错误直接放弃"""
    if not FEISHU_WEBHOOK_URL:
        log("⚠️ 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    payload = build_interactive_card(news)
    if FEISHU_SECRET:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = gen_feishu_sign(FEISHU_SECRET, timestamp)

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            result = SESSION.post(FEISHU_WEBHOOK_URL, json=payload, timeout=15).json()
        except Exception as e:
            log(f"⚠️ 推送请求异常（第 {attempt}/{HTTP_RETRIES} 次）：{e}")
        else:
            if result.get("code", result.get("StatusCode")) == 0:
                log(f"✅ 飞书推送成功：{news.get('title')}")
                return True
            log(f"❌ 飞书推送失败（业务错误）：{json.dumps(result, ensure_ascii=False)}")
            return False

        if attempt < HTTP_RETRIES:
            wait = HTTP_BACKOFF ** attempt
            log(f"⚠️ {wait}s 后重试")
            time.sleep(wait)

    log(f"❌ 飞书推送最终失败（已重试 {HTTP_RETRIES} 次）")
    return False


# ============================================================
#  增量推送
# ============================================================


def push_new_news(existing, new, lang, allow_push=True):
    """
    只推送未推送过的条目（按 postId 去重）。

    注意：这里刻意不再按「比缓存最新时间更早」过滤。
    旧逻辑会把发布时间早于缓存最新项的公告永久丢弃（补发/定时发布时会造成漏推），
    去重只依赖 postId 即可。
    """
    if not FEISHU_WEBHOOK_URL:
        log("⚠️ 未配置飞书 Webhook，跳过推送环节")
        return 0

    if lang == WEIBO_LANG:
        if not ENABLE_WEIBO:
            return 0
    elif lang != PUSH_LANG:
        return 0

    # 冷启动保护：缓存为空时只建立基线，不推送历史存量，避免一次刷出上百条
    if not existing:
        log("ℹ️ 本地缓存为空（首次运行），建立基线，本轮不推送")
        return 0

    existing_ids = {str(item["postId"]) for item in existing}
    # 按时间从旧到新推送，保证群里顺序正确
    ordered = sorted(new, key=lambda x: x.get("displayTime", ""))

    pending = []
    for item in ordered:
        post_id = str(item["postId"])
        if post_id in existing_ids:
            continue
        if any(kw in item.get("title", "") for kw in EXCLUDE_KEYWORDS):
            log(f"🔇 关键词过滤，跳过：{item.get('title')}")
            continue
        pending.append(item)

    # 积压判定：一次冒出太多“新增”，说明缓存有缺口（首次升级 / 长期停跑）。
    # 此时只补齐历史、不推送，避免把几个月前的旧公告一次性刷进群里。
    if len(pending) > MAX_PUSH_PER_RUN:
        log(f"⚠️ 本轮新增 {len(pending)} 条，判定为积压（缓存缺口），仅补齐历史、不推送")
        return 0

    if not allow_push:
        log(f"🔄 版本升级后的首轮只建立基线：本轮 {len(pending)} 条新记录仅入库、不推送")
        return 0

    pushed = 0
    for item in pending:
        if send_feishu(item):
            pushed += 1

    return pushed


# ============================================================
#  主入口
# ============================================================


def main():
    log("=" * 50)
    log("🚀 PUBG 公告监控启动")
    log(f"📡 官网：{','.join(LANGUAGES)}｜推送语言 {PUSH_LANG or '（不推送）'}｜标签 {NEWS_LABELS or '（全部）'}")
    log(f"📱 官微：{'开启' if ENABLE_WEIBO else '关闭'}（UID {WEIBO_UID}）")
    log(f"🟢 状态：{'开启' if ENABLE_STATUS else '关闭'}（确认轮数 {STATUS_CONFIRM_POLLS}）")
    log(f"🔑 飞书 Webhook：{'已配置' if FEISHU_WEBHOOK_URL else '未配置'}")
    log("=" * 50)

    total = 0

    # 首次运行 / 逻辑升级后的第一轮：只建立基线，不把历史存量当新闻推出去
    bot_state = load_bot_state()
    migrating = bot_state.get("schema") != SCHEMA_VERSION
    if migrating:
        log("🔄 首次运行或版本升级：本轮只建立基线，不推送历史存量")

    # ---------------- 官网新闻 ----------------
    for lang in LANGUAGES:
        log(f"\n--- 官网：{lang} ---")
        existing = load_existing(lang)
        known = {str(i["postId"]) for i in existing} if lang == PUSH_LANG else None
        log(f"📂 缓存 {len(existing)} 条")

        try:
            new_news = fetch_news(lang, known)
            log(f"📡 拉取 {len(new_news)} 条")
        except Exception as e:
            log(f"❌ 拉取失败（{lang}）：{e}")
            continue

        if not new_news:
            log("⚠️ 未获取到新闻，跳过")
            continue

        total += push_new_news(existing, new_news, lang, allow_push=not migrating)
        save_news(lang, merge_news(existing, new_news))

    # ---------------- 官微 ----------------
    if ENABLE_WEIBO:
        log(f"\n--- 官微：KRAFTON_GAME（{WEIBO_UID}）---")
        existing = load_existing(WEIBO_LANG)
        log(f"📂 缓存 {len(existing)} 条")
        try:
            new_weibo = fetch_weibo()
            log(f"📡 拉取 {len(new_weibo)} 条维护类公告")
        except Exception as e:
            log(f"❌ 微博拉取失败（已跳过，不影响官网）：{e}")
            new_weibo = []

        if new_weibo:
            total += push_new_news(existing, new_weibo, WEIBO_LANG, allow_push=not migrating)
            save_news(WEIBO_LANG, merge_news(existing, new_weibo))

    # ---------------- 服务器状态 ----------------
    if ENABLE_STATUS:
        log("\n--- PC 服务器状态（pubg.plus）---")
        old_state = load_status_state()
        try:
            new_state = fetch_pubg_status()
            pc = new_state.get("pc") or {}
            log(
                f"📡 maintenance={pc.get('maintenance')} conn={pc.get('server_status')} "
                f"online={pc.get('online')} ver={pc.get('client_version')}"
            )
        except Exception as e:
            log(f"❌ 状态拉取失败（已跳过）：{e}")
            new_state = None

        if new_state:
            events, merged_state = update_status(old_state, new_state)
            for ev in events:
                if send_feishu(ev):
                    total += 1
            save_status_state(merged_state)

    log("\n" + "=" * 50)
    log(f"🏁 完成，本次共推送 {total} 条")
    log("=" * 50)

    if migrating:
        save_bot_state({
            "schema": SCHEMA_VERSION,
            "migrated_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        })
        log("✅ 基线建立完成，后续运行将正常推送新增内容")


if __name__ == "__main__":
    main()
