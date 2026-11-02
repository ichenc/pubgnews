#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PUBG 官网新闻/活动公告监控 → 飞书推送
基于 yutangbb/pubg-news 改造，新增飞书 Webhook 推送支持
GitHub Actions 定时运行，无需自建服务器
"""

import requests
import json
import os
import re
import sys
import time
import html as html_lib
import hmac
import hashlib
import base64
from datetime import datetime, timedelta, timezone

# ============================================================
#  配置区（GitHub Actions 部署时全部通过 Secrets 环境变量传入）
# ============================================================

# 飞书自定义机器人 Webhook 地址（必填，通过环境变量 FEISHU_WEBHOOK_URL 传入）
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "").rstrip("/")

# 飞书机器人签名校验密钥（可选，没开签名就留空，通过环境变量 FEISHU_SECRET 传入）
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")

# 获取新闻语言列表（会分别保存为不同 json 文件）
LANGUAGES = ["zh-cn", "zh-tw", "en", "ko"]

# 推送通知使用的语言，设为空字符串则不推送
PUSH_LANG = os.getenv("PUSH_LANG", "zh-cn")

# 每次获取最新条数（上限 50）
SIZE = min(int(os.getenv("FETCH_SIZE", "10")), 50)

# 推送排除：标题包含以下任意关键词的新闻不推送
EXCLUDE_KEYWORDS = [
    "每周违规账号公示",
    "封禁公告",
    "Weekly Bans Notice",
    "Bans Notice"
]
# 支持通过环境变量覆盖（用英文逗号分隔）
_env_exclude = os.getenv("EXCLUDE_KEYWORDS", "")
if _env_exclude:
    EXCLUDE_KEYWORDS = [k.strip() for k in _env_exclude.split(",") if k.strip()]

# 飞书卡片头部标题
PUSH_TITLE = os.getenv("PUSH_TITLE", "🎮 PUBG 新公告")

# 脚本所在目录
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# 北京时间（GitHub Actions 运行环境默认是 UTC，
# 不统一时区会导致状态推送时间戳、日志时间与微博时间混用）
TZ_CN = timezone(timedelta(hours=8))


def now_cn():
    """当前北京时间（中国无夏令时，固定 UTC+8）"""
    return datetime.now(TZ_CN)


# HTTP 会话复用（同一进程内所有请求共享连接池，减少握手开销）
SESSION = requests.Session()

# 网络请求失败重试次数与退避基数（实际等待 backoff^attempt 秒）
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "3"))
HTTP_BACKOFF = 2

# PUBG 官网 API（无需 API Key，公开接口）
PUBG_API = "https://api-foc.krafton.com/content/post/news"

# ============================================================
#  微博（KRAFTON_GAME 官微）配置
# ============================================================
# 是否抓取微博停机维护公告（默认开启，设为 false 关闭）
ENABLE_WEIBO = os.getenv("ENABLE_WEIBO", "true").lower() in ("1", "true", "yes")

# KRAFTON_GAME 官微 UID（https://weibo.com/u/6037906900）
WEIBO_UID = os.getenv("WEIBO_UID", "6037906900")

# 新浪移动媒体页（服务端渲染，无需登录/cookie）
WEIBO_MEDIA_URL = f"https://www.sina.cn/media/{WEIBO_UID}"

# 仅推送正文包含以下任意关键词的微博（逗号分隔）
# 原则：凡是"服务不可用或即将不可用"的公告都必须命中，
#       包括提前预告（停机维护公告）和事后通知（维护结束/故障修复）
_weibo_kw_env = os.getenv("WEIBO_KEYWORDS",
    "维护公告,故障公告,连接故障,登录异常,服务器异常,服务器维护,停机,热补丁,"
    "紧急维护,临时维护,服务器波动,网络波动,无法登录,掉线,排队,维护结束,已恢复")
WEIBO_KEYWORDS = [k.strip() for k in _weibo_kw_env.split(",") if k.strip()]

# 微博数据在本地缓存里使用的虚拟语言标识（对应 news_weibo.json）
WEIBO_LANG = "weibo"

# ============================================================
#  PUBG 服务器实时状态（pubg.plus）配置
# ============================================================
# 是否监控服务器状态变化（默认开启，设为 false 关闭）
ENABLE_STATUS = os.getenv("ENABLE_STATUS", "true").lower() in ("1", "true", "yes")

# pubg.plus 状态 API（前端 HMAC-SHA256 签名，密钥逆向自其 JS）
PUBG_STATUS_API = "https://apiv1.pubg.plus/status/server"
# 签名密钥（pubg.plus 前端混淆拼接，若对方更新导致 401，可通过环境变量覆盖）
PUBG_STATUS_SECRET = os.getenv("PUBG_STATUS_SECRET", "Bm4is8qQgJXdocrvobbFR7u2kh66Pu")

# 状态缓存文件（保存上次状态，用于对比变化）
STATUS_CACHE_FILE = "pubg_status_state.json"

# ============================================================
#  工具函数
# ============================================================

def log(msg):
    """打印带时间戳的日志（北京时间）"""
    time_str = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{time_str}] {msg}", flush=True)


def http_get(url, retries=HTTP_RETRIES, backoff=HTTP_BACKOFF, **kwargs):
    """
    带重试的 GET 请求（指数退避）。
    GitHub Actions 网络偶发抖动，单次失败就跳过整轮会导致漏抓。
    """
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
    """生成飞书机器人签名（开启签名校验时使用）"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


# ============================================================
#  PUBG 官网 API 拉取
# ============================================================

def fetch_news(lang):
    """
    调用 PUBG 官网 API 拉取指定语言的新闻列表
    返回标准化后的新闻字典列表
    """
    headers = {
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
        )
    }
    params = {
        "lang": lang,
        "displayLocationType": "NORMAL",
        "size": SIZE,
        "page": 1
    }

    resp = http_get(PUBG_API, headers=headers, params=params, timeout=30)

    posts = resp.json().get("_embedded", {}).get("post", [])
    news_items = []

    for post in posts:
        images = post.get("images") or []
        image_url = images[0].get("imageUrl", "") if images else ""
        thumb_url = images[0].get("thumbUrl", "") if images else ""
        post_id = str(post.get("postId", ""))

        news_items.append({
            "title": post.get("title", ""),
            "summary": post.get("summary", ""),
            "postId": post_id,
            "category": post.get("category", ""),
            "labels": post.get("labels", []),
            "createdAt": post.get("createdAt", ""),
            "displayTime": post.get("displayStartTime", ""),
            "imageUrl": image_url,
            "thumbUrl": thumb_url,
            "newsUrl": f"https://pubg.com/{lang}/news/{post_id}"
        })

    return news_items[:SIZE]


# ============================================================
#  KRAFTON 官微（sina.cn 移动页）拉取
# ============================================================

def _clean_html(text):
    """去掉 HTML 标签、反转义实体、微博里的零宽字符 / 多余空白"""
    text = html_lib.unescape(text)  # &quot; &amp; &nbsp; 等
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\u200b", "").replace("\u200c", "").replace("\u200d", "")
    text = text.replace("\xa0", " ").strip()
    return re.sub(r"\s+", " ", text)


def _extract_weibo_title(body):
    """
    从微博正文里提取标题：
    优先取【...】里的内容，否则取前 30 个字符
    """
    m = re.match(r"^[【\[](.+?)[】\]]", body)
    if m:
        return m.group(1).strip()
    return body[:30].strip()


def fetch_weibo():
    """
    抓取 KRAFTON_GAME 官微最新微博，过滤出含维护类关键词的条目。
    返回与官网 fetch_news 相同结构的列表，postId 用新浪详情页 oid。
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/15.0 Mobile/15E148 Safari/604.1"
        )
    }
    resp = http_get(WEIBO_MEDIA_URL, headers=headers, timeout=30)
    # 新浪移动页返回 utf-8
    html = resp.content.decode("utf-8", errors="ignore")

    # 每条微博结构：
    # <a class="post-link" href="/news/detail/{oid}.html">
    #   <article class="post"> ...
    #     <div class="time">2026-09-22 15:00<span ...>来自 ...</span></div>
    #     <div class="post-text">正文...</div>
    #   </article>
    # </a>
    pattern = re.compile(
        r'<a class="post-link" href="(/news/detail/(\d+)\.html)"[^>]*>'
        r".*?<div class=\"time\">([^<]+)<span"
        r".*?<div class=\"post-text\">(.*?)</div>",
        re.S,
    )

    news_items = []
    seen_oids = set()
    for m in pattern.finditer(html):
        _href, oid, time_str, raw_body = m.groups()
        oid = oid.strip()
        if oid in seen_oids:
            continue
        seen_oids.add(oid)

        body = _clean_html(raw_body)
        # 关键词过滤：只保留维护类公告
        if not any(kw in body for kw in WEIBO_KEYWORDS):
            continue

        # 时间统一成 "YYYY-MM-DD HH:MM:SS"。
        # 防御：若页面返回相对时间（如 "3分钟前"）等无法解析的格式，
        # 直接丢弃该条目，否则字符串排序会错乱，导致推送逻辑误判
        display_time = time_str.strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", display_time):
            log(f"⏭️ 微博时间格式异常，跳过该条：{display_time} | {body[:30]}")
            continue
        display_time += ":00"

        news_items.append({
            "title": _extract_weibo_title(body),
            "summary": body,
            "postId": oid,
            "category": "weibo",
            "labels": ["weibo"],
            "createdAt": display_time,
            "displayTime": display_time,
            "imageUrl": "",
            "thumbUrl": "",
            # 微博原文（移动端分享页），而不是抓取源新浪聚合页
            "newsUrl": f"https://m.weibo.cn/status/{oid}",
            "source": "微博 · KRAFTON_GAME",
        })

    # 按时间倒序，最多保留 SIZE 条
    news_items.sort(key=lambda x: x["displayTime"], reverse=True)
    return news_items[:SIZE]


# ============================================================
#  PUBG 服务器实时状态（pubg.plus）
# ============================================================

def _status_sign(secret):
    """生成 pubg.plus 状态 API 的签名：HMAC-SHA256("status"+ts, secret) 前 16 hex"""
    ts = str(int(time.time()))
    msg = ("status" + ts).encode("utf-8")
    sign = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()[:16]
    return ts, sign


def fetch_pubg_status():
    """
    拉取 PUBG PC / 主机服务器实时状态。
    返回 dict: {"pc": {...}, "console": {...}, "updated_at": "..."}
    """
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
    resp = http_get(
        PUBG_STATUS_API,
        params={"ts": ts, "sign": sign},
        headers=headers,
        timeout=20,
    )
    body = resp.json()
    if body.get("code") != 0:
        raise RuntimeError(f"状态 API 返回错误：{body.get('message')}")
    return body.get("data", {})


def check_status_change(old, new):
    """
    对比新旧 PC 服务器状态，返回需要推送的消息列表。
    old 为 None/空表示首次运行，不推送任何消息（只记录基线）。

    pubg.plus 状态枚举（从实际返回反推）：
      maintenance / server_status:
        0 = 离线/异常
        1 = 正常在线
        2 = 维护中
    """
    if not old:
        return []

    messages = []
    old_p = old.get("pc") or {}
    new_p = new.get("pc") or {}
    if not new_p:
        return []

    old_m = old_p.get("maintenance")
    new_m = new_p.get("maintenance")
    old_s = old_p.get("server_status")
    new_s = new_p.get("server_status")
    now_str = now_cn().strftime("%Y-%m-%d %H:%M:%S")
    ts = int(time.time())

    # 只有已知枚举值才参与对比，None/异常值不触发
    valid = (0, 1, 2)

    # ---- 维护状态变化（仅在 1=正常 ↔ 2=维护中 之间切换时推送）----
    if (old_m in valid and new_m in valid
            and old_m != new_m
            and {old_m, new_m} == {1, 2}):
        if new_m == 2:
            title = "🟡 PUBG PC 服务器进入维护"
            summary = "PUBG PC 服务器已开始维护，预计维护期间无法登录/匹配。"
        else:  # new_m == 1，从维护中恢复
            title = "🟢 PUBG PC 服务器维护结束"
            summary = "PUBG PC 服务器维护已结束，可以重新启动客户端进入游戏。"
        if new_p.get("online"):
            summary += f" 当前在线人数：{new_p['online']}。"
        messages.append({
            "title": title,
            "summary": summary,
            "postId": f"status_pc_maint_{new_m}_{ts}",
            "category": "status",
            "severity": _CARD_TEMPLATE_ORANGE if new_m == 2 else _CARD_TEMPLATE_GREEN,
            "source": "服务器状态 · pubg.plus",
            "displayTime": now_str,
            "newsUrl": "https://pubg.plus/zh-CN/status",
        })

    # ---- 连接状态变化（仅在 1=正常 ↔ 0=离线 之间切换时推送；
    #      维护期间 server_status=2 属于正常现象，不单独推送）----
    if (old_s in valid and new_s in valid
            and old_s != new_s
            and {old_s, new_s} == {0, 1}):
        if new_s == 0:
            title = "🔴 PUBG PC 服务器连接中断"
            summary = "PUBG PC 服务器连接状态异常（可能崩溃或网络波动），请稍后重试。"
        else:
            title = "🟢 PUBG PC 服务器连接恢复"
            summary = "PUBG PC 服务器连接已恢复。"
        messages.append({
            "title": title,
            "summary": summary,
            "postId": f"status_pc_conn_{new_s}_{ts}",
            "category": "status",
            "severity": _CARD_TEMPLATE_RED if new_s == 0 else _CARD_TEMPLATE_GREEN,
            "source": "服务器状态 · pubg.plus",
            "displayTime": now_str,
            "newsUrl": "https://pubg.plus/zh-CN/status",
        })

    return messages


def load_status_state():
    """读取上次保存的服务器状态"""
    filename = os.path.join(SCRIPT_DIR, STATUS_CACHE_FILE)
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def save_status_state(state):
    """保存当前 PC 服务器状态（只存对比需要的字段）"""
    filename = os.path.join(SCRIPT_DIR, STATUS_CACHE_FILE)
    pc = state.get("pc") or {}
    slim = {
        "pc": {
            "maintenance": pc.get("maintenance"),
            "server_status": pc.get("server_status"),
            "client_version": pc.get("client_version"),
            "online": pc.get("online"),
        },
        "updated_at": state.get("updated_at", ""),
        "saved_at": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(slim, f, ensure_ascii=False, indent=2)


# ============================================================
#  本地缓存（json 文件，跨运行保留已推送记录）
# ============================================================

def load_existing(lang):
    """读取已保存的新闻列表（用于对比增量），并做旧格式兼容迁移"""
    filename = os.path.join(SCRIPT_DIR, f"news_{lang}.json")
    items = []
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                items = json.load(f)
        except Exception:
            return []

    for item in items:
        # 历史缓存中官网 postId 是 int，统一为字符串，避免去重时类型不一致
        item["postId"] = str(item.get("postId", ""))
        # 旧数据微博链接指向抓取源（新浪聚合页），迁移为微博原文链接
        if item.get("category") == "weibo" and "sina.cn" in item.get("newsUrl", ""):
            item["newsUrl"] = f"https://m.weibo.cn/status/{item['postId']}"
    return items


def save_news(lang, news_items):
    """保存新闻列表到 json 文件"""
    filename = os.path.join(SCRIPT_DIR, f"news_{lang}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(news_items, f, ensure_ascii=False, indent=4)
    log(f"💾 保存 news_{lang}.json，共 {len(news_items)} 条")


def merge_news(existing, new):
    """合并新旧新闻，去重（postId 统一按字符串比较），按时间倒序，保留最新 SIZE 条"""
    existing_map = {str(item["postId"]): item for item in existing}
    for item in new:
        existing_map[str(item["postId"])] = item
    merged = list(existing_map.values())
    merged.sort(key=lambda x: x.get("displayTime", ""), reverse=True)
    return merged[:SIZE]


# ============================================================
#  飞书推送
# ============================================================

# ============================================================
#  飞书互动卡片（interactive card）构建
# ============================================================

# 卡片表头主题色（飞书 header template 取值），用于视觉区分与一致性
_CARD_TEMPLATE_BLUE = "blue"      # 官网新闻 / 默认
_CARD_TEMPLATE_ORANGE = "orange"  # 微博维护公告 / 维护中
_CARD_TEMPLATE_RED = "red"        # 服务器异常 / 连接中断
_CARD_TEMPLATE_GREEN = "green"    # 恢复 / 正常
_VALID_TEMPLATES = frozenset([
    "blue", "orange", "red", "green", "wathet",
    "grey", "violet", "indigo", "turquoise",
])


def _escape_markdown(text):
    """转义 lark_md 中的特殊字符，避免标题/摘要里的 * _ ` [ ] 破坏卡片排版"""
    return (
        text.replace("\\", "\\\\")
        .replace("*", "\\*")
        .replace("_", "\\_")
        .replace("`", "\\`")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _truncate(text, limit):
    """超长文本截断并追加省略号"""
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _card_template(news):
    """
    根据消息类别 / 严重程度选择卡片表头主题色。
    优先使用 status 消息自带的 severity 字段，保证视觉一致且有区分度。
    """
    severity = news.get("severity")
    if severity in _VALID_TEMPLATES:
        return severity
    if news.get("category") == "weibo":
        return _CARD_TEMPLATE_ORANGE
    return _CARD_TEMPLATE_BLUE


def _link_text(news):
    """根据来源给出统一的跳转按钮文案"""
    category = news.get("category", "")
    if category == "weibo":
        return "查看微博原文"
    if category == "status":
        return "查看状态面板"
    return "查看官网详情"


def build_interactive_card(news):
    """
    将一条新闻 / 公告组装为飞书互动卡片（interactive card）。

    相比原 post 富文本，卡片具备：彩色表头、封面图、摘要、
    来源 / 时间元信息、跳转按钮与统一页脚，视觉更美观一致。
    """
    title = (news.get("title") or "").strip()
    summary = (news.get("summary") or "").strip()
    source = news.get("source") or news.get("category") or ""
    display_time = news.get("displayTime") or ""
    news_url = news.get("newsUrl") or ""
    image_url = news.get("imageUrl") or news.get("thumbUrl") or ""

    elements = []

    # 标题（加粗突出）
    if title:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": f"**{_escape_markdown(title)}**"},
        })

    # 来源 + 时间（两行元信息）
    meta_bits = []
    if source:
        meta_bits.append(f"🏷️ 来源：{_escape_markdown(source)}")
    if display_time:
        meta_bits.append(f"🕒 时间：{display_time}")
    if meta_bits:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": "  \n".join(meta_bits)},
        })

    # 封面图（仅当有图时展示，避免空白占位）
    if image_url:
        elements.append({
            "tag": "img",
            "img_path": image_url,
            "alt": {"tag": "plain_text", "content": title or "cover"},
            "mode": "fit_horizontal",
            "corner_radius": 8,
        })

    # 摘要
    if summary:
        elements.append({
            "tag": "div",
            "text": {"tag": "lark_md", "content": _truncate(_escape_markdown(summary), 500)},
        })

    # 分隔线 + 跳转按钮
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

    # 统一页脚
    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": "🤖 PUBG 公告助手 · 自动推送"}],
    })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": PUSH_TITLE},
            "template": _card_template(news),
        },
        "elements": elements,
    }
    return {"msg_type": "interactive", "card": card}


def send_feishu(news):
    """推送飞书互动卡片，失败自动重试（指数退避），提高送达率"""
    if not FEISHU_WEBHOOK_URL:
        log("⚠️ 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    payload = build_interactive_card(news)

    # 签名校验（开启时）
    if FEISHU_SECRET:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = gen_feishu_sign(FEISHU_SECRET, timestamp)

    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            resp = SESSION.post(FEISHU_WEBHOOK_URL, json=payload, timeout=15)
            result = resp.json()
        except Exception as e:
            # 网络抖动 / 解析失败属于可重试的瞬态错误
            log(f"⚠️ 推送请求异常（第 {attempt}/{HTTP_RETRIES} 次）：{e}")
        else:
            # 飞书自定义机器人成功时 code == 0
            code = result.get("code", result.get("StatusCode"))
            if code == 0:
                log(f"✅ 飞书推送成功：{news.get('title')}")
                return True
            # 业务错误（签名错误 / 被限流 / 关键词屏蔽等）重试无意义，直接放弃
            log(f"❌ 飞书推送失败（业务错误）：{json.dumps(result, ensure_ascii=False)}")
            return False

        if attempt < HTTP_RETRIES:
            wait = HTTP_BACKOFF ** attempt
            log(f"⚠️ {wait}s 后重试")
            time.sleep(wait)

    log(f"❌ 飞书推送最终失败（已重试 {HTTP_RETRIES} 次）")
    return False


# ============================================================
#  增量推送逻辑
# ============================================================

def push_new_news(existing, new, lang):
    """
    对比新旧新闻，只推送真正新增的
    existing: 已保存的旧新闻列表
    new: 刚拉取的新新闻列表
    lang: 当前语言
    """
    if not FEISHU_WEBHOOK_URL:
        log("⚠️ 未配置飞书 Webhook，跳过推送环节")
        return 0

    # 官网语言按 PUSH_LANG 过滤；微博（weibo）只要开关开启就推送
    if lang == WEIBO_LANG:
        if not ENABLE_WEIBO:
            return 0
    elif lang != PUSH_LANG:
        return 0

    # 已保存的 postId 集合（统一按字符串比较，兼容旧缓存中的 int 类型）
    existing_ids = {str(item["postId"]) for item in existing}
    # 已保存的最新时间（用于过滤更早的新闻）
    latest_time = existing[0].get("displayTime", "") if existing else ""

    # 按时间从旧到新排序，保证推送顺序正确
    new_sorted = sorted(new, key=lambda x: x.get("displayTime", ""))

    pushed_count = 0
    for item in new_sorted:
        post_id = str(item["postId"])
        title = item["title"]

        # 已推送过的跳过
        if post_id in existing_ids:
            continue

        # 时间比已保存最新的还早，跳过（防止历史新闻倒灌）
        if latest_time and item.get("displayTime", "") < latest_time:
            continue

        # 关键词过滤
        if any(kw in title for kw in EXCLUDE_KEYWORDS):
            log(f"🔇 关键词过滤，跳过：{title}")
            continue

        # 推送
        if send_feishu(item):
            pushed_count += 1
        # 无论成功失败都标记为已处理，防止失败时反复轰炸
        existing_ids.add(post_id)

    return pushed_count


# ============================================================
#  主入口
# ============================================================

def main():
    log("=" * 50)
    log("🚀 PUBG 新闻公告监控脚本启动")
    log(f"📡 推送语言：{PUSH_LANG or '（未设置，不推送）'}")
    log(f"🔑 飞书 Webhook：{'已配置' if FEISHU_WEBHOOK_URL else '未配置'}")
    log(f"📝 排除关键词：{EXCLUDE_KEYWORDS}")
    log(f"📱 微博抓取：{'已开启（UID ' + WEIBO_UID + '，关键词=' + str(WEIBO_KEYWORDS) + '）' if ENABLE_WEIBO else '已关闭'}")
    log(f"📡 服务器状态监控：{'已开启（pubg.plus）' if ENABLE_STATUS else '已关闭'}")
    log("=" * 50)

    total_pushed = 0

    for lang in LANGUAGES:
        log(f"\n--- 处理语言：{lang} ---")

        # 1. 读取本地缓存
        existing_news = load_existing(lang)
        log(f"📂 本地缓存 {len(existing_news)} 条")

        # 2. 拉取最新新闻
        try:
            new_news = fetch_news(lang)
            log(f"📡 官网拉取 {len(new_news)} 条")
        except Exception as e:
            log(f"❌ 拉取失败（{lang}）：{e}")
            continue

        if not new_news:
            log("⚠️ 未获取到新闻，跳过")
            continue

        # 3. 增量推送（仅对 PUSH_LANG 生效）
        pushed = push_new_news(existing_news, new_news, lang)
        total_pushed += pushed

        # 4. 合并并保存（无论是否推送都更新缓存）
        merged_news = merge_news(existing_news, new_news)
        save_news(lang, merged_news)

    # ---------------- 微博维护公告 ----------------
    if ENABLE_WEIBO:
        log(f"\n--- 处理微博：KRAFTON_GAME（{WEIBO_UID}）---")
        existing_weibo = load_existing(WEIBO_LANG)
        log(f"📂 微博缓存 {len(existing_weibo)} 条")

        try:
            new_weibo = fetch_weibo()
            log(f"📡 微博拉取 {len(new_weibo)} 条维护类公告")
        except Exception as e:
            # 微博失败不影响官网主流程
            log(f"❌ 微博拉取失败（已跳过，不影响官网）：{e}")
            new_weibo = []

        if new_weibo:
            pushed = push_new_news(existing_weibo, new_weibo, WEIBO_LANG)
            total_pushed += pushed
            merged_weibo = merge_news(existing_weibo, new_weibo)
            save_news(WEIBO_LANG, merged_weibo)

    # ---------------- 服务器实时状态（仅 PC） ----------------
    if ENABLE_STATUS:
        log(f"\n--- 处理 PC 服务器状态（pubg.plus）---")
        old_state = load_status_state()
        try:
            new_state = fetch_pubg_status()
            pc = new_state.get("pc") or {}
            log(
                f"📡 PC: maintenance={pc.get('maintenance')} "
                f"conn={pc.get('server_status')} online={pc.get('online')} "
                f"ver={pc.get('client_version')}"
            )
        except Exception as e:
            log(f"❌ 服务器状态拉取失败（已跳过）：{e}")
            new_state = None

        if new_state:
            # 首次运行只记录基线，不推送
            if not old_state:
                log("ℹ️ 首次运行，记录当前状态作为基线（不推送）")
            else:
                events = check_status_change(old_state, new_state)
                for ev in events:
                    if send_feishu(ev):
                        total_pushed += 1
            save_status_state(new_state)

    log("\n" + "=" * 50)
    log(f"🏁 执行完成，本次共推送 {total_pushed} 条新公告")
    log("=" * 50)


if __name__ == "__main__":
    main()
