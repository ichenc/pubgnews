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
    """
    发送飞书交互式卡片消息
    news: 单条新闻字典
    返回 True/False
    """
    if not FEISHU_WEBHOOK_URL:
        log("⚠️ 未配置 FEISHU_WEBHOOK_URL，跳过推送")
        return False

    # 根据分类选择卡片头部颜色
    category = (news.get("category") or "").lower()
    color_map = {
        "event": "orange",
        "notice": "red",
        "update": "blue",
        "announcement": "turquoise",
        "news": "wathet"
    }
    header_color = color_map.get(category, "orange")

    # 构建卡片元素
    elements = []

    # 分类标签
    if news.get("category"):
        elements.append({
            "tag": "lark_md",
            "content": f"**分类：** {news['category']}"
        })

    # 摘要
    summary = (news.get("summary") or "").strip()
    if summary:
        elements.append({"tag": "divider"})
        elements.append({
            "tag": "lark_md",
            "content": summary[:500]
        })

    # 发布时间
    display_time = news.get("displayTime", "")
    if display_time:
        elements.append({"tag": "divider"})
        elements.append({
            "tag": "lark_md",
            "content": f"📅 **发布时间：** {display_time}"
        })

    # 封面图
    image_url = news.get("imageUrl", "")
    if image_url:
        elements.append({"tag": "divider"})
        elements.append({
            "tag": "lark_md",
            "content": f"![封面]({image_url})"
        })

    # 查看详情按钮
    elements.append({"tag": "divider"})
    elements.append({
        "tag": "action",
        "actions": [{
            "tag": "button",
            "text": {"tag": "plain_text", "content": "👉 查看官网详情"},
            "url": news["newsUrl"],
            "type": "primary"
        }]
    })

    # 组装卡片
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"{PUSH_TITLE}：{news['title']}"
                },
                "template": header_color
            },
            "elements": elements
        }
    }

    # 签名校验
    if FEISHU_SECRET:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = gen_feishu_sign(FEISHU_SECRET, timestamp)

    # 发送
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

    if lang != PUSH_LANG:
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

    log("\n" + "=" * 50)
    log(f"🏁 执行完成，本次共推送 {total_pushed} 条新公告")
    log("=" * 50)


if __name__ == "__main__":
    main()
