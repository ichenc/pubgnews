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
import hmac
import hashlib
import base64
from datetime import datetime

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
# 默认聚焦"维护公告"，需要热补丁/维护结束通知可追加：热补丁,停机维护已结束
_weibo_kw_env = os.getenv("WEIBO_KEYWORDS", "维护公告")
WEIBO_KEYWORDS = [k.strip() for k in _weibo_kw_env.split(",") if k.strip()]

# 微博数据在本地缓存里使用的虚拟语言标识（对应 news_weibo.json）
WEIBO_LANG = "weibo"

# ============================================================
#  工具函数
# ============================================================

def log(msg):
    """打印带时间戳的日志"""
    time_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{time_str}] {msg}", flush=True)


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

    resp = requests.get(PUBG_API, headers=headers, params=params, timeout=30)
    resp.raise_for_status()

    posts = resp.json().get("_embedded", {}).get("post", [])
    news_items = []

    for post in posts:
        images = post.get("images") or []
        image_url = images[0].get("imageUrl", "") if images else ""
        thumb_url = images[0].get("thumbUrl", "") if images else ""

        news_items.append({
            "title": post.get("title", ""),
            "summary": post.get("summary", ""),
            "postId": post.get("postId", ""),
            "category": post.get("category", ""),
            "labels": post.get("labels", []),
            "createdAt": post.get("createdAt", ""),
            "displayTime": post.get("displayStartTime", ""),
            "imageUrl": image_url,
            "thumbUrl": thumb_url,
            "newsUrl": f"https://pubg.com/{lang}/news/{post.get('postId', '')}"
        })

    return news_items[:SIZE]


# ============================================================
#  KRAFTON 官微（sina.cn 移动页）拉取
# ============================================================

def _clean_html(text):
    """去掉 HTML 标签和微博里的零宽字符 / 多余空白"""
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
    resp = requests.get(WEIBO_MEDIA_URL, headers=headers, timeout=30)
    resp.raise_for_status()
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
        href, oid, time_str, raw_body = m.groups()
        oid = oid.strip()
        if oid in seen_oids:
            continue
        seen_oids.add(oid)

        body = _clean_html(raw_body)
        # 关键词过滤：只保留维护类公告
        if not any(kw in body for kw in WEIBO_KEYWORDS):
            continue

        # 时间统一成 "YYYY-MM-DD HH:MM:SS"
        display_time = time_str.strip()
        if len(display_time) == 16:  # "2026-09-22 15:00"
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
            "newsUrl": f"https://www.sina.cn{href}",
            "source": "微博 · KRAFTON_GAME",
        })

    # 按时间倒序，最多保留 SIZE 条
    news_items.sort(key=lambda x: x["displayTime"], reverse=True)
    return news_items[:SIZE]


# ============================================================
#  本地缓存（json 文件，跨运行保留已推送记录）
# ============================================================

def load_existing(lang):
    """读取已保存的新闻列表（用于对比增量）"""
    filename = os.path.join(SCRIPT_DIR, f"news_{lang}.json")
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []


def save_news(lang, news_items):
    """保存新闻列表到 json 文件"""
    filename = os.path.join(SCRIPT_DIR, f"news_{lang}.json")
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(news_items, f, ensure_ascii=False, indent=4)
    log(f"💾 保存 news_{lang}.json，共 {len(news_items)} 条")


def merge_news(existing, new):
    """合并新旧新闻，去重，按时间倒序，保留最新 SIZE 条"""
    existing_map = {item["postId"]: item for item in existing}
    for item in new:
        existing_map[item["postId"]] = item
    merged = list(existing_map.values())
    merged.sort(key=lambda x: x.get("displayTime", ""), reverse=True)
    return merged[:SIZE]


# ============================================================
#  飞书推送
# ============================================================

def send_feishu(news):
    if not FEISHU_WEBHOOK_URL:
        log("⚠️ 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    # 组装富文本内容（每行一个数组）
    content_lines = []

    # 标题行
    content_lines.append([
        {"tag": "text", "text": f"标题：{news['title']}"}
    ])

    # 分类 / 来源
    source = news.get("source") or news.get("category")
    if source:
        content_lines.append([
            {"tag": "text", "text": f"来源：{source}"}
        ])

    # 摘要
    summary = (news.get("summary") or "").strip()
    if summary:
        content_lines.append([
            {"tag": "text", "text": f"摘要：{summary[:300]}"}
        ])

    # 发布时间
    if news.get("displayTime"):
        content_lines.append([
            {"tag": "text", "text": f"时间：{news['displayTime']}"}
        ])

    # 跳转链接（官网/微博文案区分）
    link_text = "👉 查看微博原文" if news.get("source") else "👉 点击查看官网详情"
    content_lines.append([
        {"tag": "a", "text": link_text, "href": news["newsUrl"]}
    ])

    payload = {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": PUSH_TITLE,
                    "content": content_lines
                }
            }
        }
    }

    # 签名校验
    if FEISHU_SECRET:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = gen_feishu_sign(FEISHU_SECRET, timestamp)

    try:
        resp = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=15)
        result = resp.json()
        if result.get("code") == 0 or result.get("StatusCode") == 0:
            log(f"✅ 飞书推送成功：{news['title']}")
            return True
        else:
            log(f"❌ 飞书推送失败：{json.dumps(result, ensure_ascii=False)}")
            return False
    except Exception as e:
        log(f"❌ 飞书推送异常：{e}")
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

    # 已保存的 postId 集合
    existing_ids = {item["postId"] for item in existing}
    # 已保存的最新时间（用于过滤更早的新闻）
    latest_time = existing[0].get("displayTime", "") if existing else ""

    # 按时间从旧到新排序，保证推送顺序正确
    new_sorted = sorted(new, key=lambda x: x.get("displayTime", ""))

    pushed_count = 0
    for item in new_sorted:
        post_id = item["postId"]
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

    log("\n" + "=" * 50)
    log(f"🏁 执行完成，本次共推送 {total_pushed} 条新公告")
    log("=" * 50)


if __name__ == "__main__":
    main()
