#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
无需 Node，直接用 Cloudflare API 部署 pubgnews-scheduler 定时触发器。

用法：
    export CLOUDFLARE_API_TOKEN=xxxx        # 需 Workers Scripts: Edit 权限
    export CLOUDFLARE_ACCOUNT_ID=xxxx       # 可选，留空则自动读取第一个账户
    export GH_TOKEN=ghp_xxxx                # GitHub 令牌（repo + workflow）
    python deploy.py

会依次完成：校验令牌 → 上传 Worker → 写入 GH_TOKEN 密钥 → 设置每 5 分钟 Cron。
"""

import json
import os
import sys

try:
    import requests
except ImportError:
    sys.exit("缺少依赖：请先 pip install requests")

CF_API = "https://api.cloudflare.com/client/v4"
SCRIPT_NAME = "pubgnews-scheduler"
CRON = os.getenv("CRON", "*/5 * * * *")
HERE = os.path.dirname(os.path.abspath(__file__))


def die(msg):
    print(f"\n❌ {msg}")
    sys.exit(1)


def cf(method, path, token, **kwargs):
    resp = requests.request(method, f"{CF_API}{path}", timeout=60,
                            headers={"Authorization": f"Bearer {token}"}, **kwargs)
    try:
        data = resp.json()
    except Exception:
        die(f"Cloudflare 返回非 JSON（HTTP {resp.status_code}）：{resp.text[:300]}")
    if not data.get("success"):
        errs = data.get("errors") or []
        detail = "; ".join(f"[{e.get('code')}] {e.get('message')}" for e in errs) or resp.text[:300]
        die(f"Cloudflare 接口失败（HTTP {resp.status_code}）：{detail}")
    return data.get("result")


def main():
    token = os.getenv("CLOUDFLARE_API_TOKEN", "").strip()
    gh = os.getenv("GH_TOKEN", "").strip()
    account = os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip()

    if not token:
        die("缺少 CLOUDFLARE_API_TOKEN")
    if not gh:
        die("缺少 GH_TOKEN（GitHub 令牌）")

    # 1) 校验令牌
    print("→ 校验 Cloudflare 令牌 ...")
    cf("GET", "/user/tokens/verify", token)
    print("  ✅ 令牌有效")

    # 2) 账户 ID
    if not account:
        print("→ 自动读取账户 ...")
        accounts = cf("GET", "/accounts", token) or []
        if not accounts:
            die("无法读取账户，请显式设置 CLOUDFLARE_ACCOUNT_ID")
        account = accounts[0]["id"]
        print(f"  使用账户：{accounts[0].get('name', '')} ({account})")

    base = f"/accounts/{account}/workers/scripts/{SCRIPT_NAME}"

    # 3) 上传 Worker（ES module）
    print("→ 上传 Worker 脚本 ...")
    script_path = os.path.join(HERE, "worker.js")
    if not os.path.exists(script_path):
        die(f"找不到 {script_path}")
    metadata = {
        "main_module": "worker.js",
        "compatibility_date": "2026-01-01",
        "observability": {"enabled": True},
    }
    with open(script_path, "rb") as fh:
        files = {
            "metadata": (None, json.dumps(metadata), "application/json"),
            "worker.js": ("worker.js", fh, "application/javascript+module"),
        }
        cf("PUT", base, token, files=files)
    print("  ✅ 脚本已上传")

    # 4) 写入 GH_TOKEN 密钥
    print("→ 写入 GH_TOKEN 密钥 ...")
    cf("PUT", f"{base}/secrets", token,
       json={"name": "GH_TOKEN", "text": gh, "type": "secret_text"})
    print("  ✅ 密钥已写入")

    # 5) 设置 Cron
    print(f"→ 设置 Cron：{CRON} ...")
    cf("PUT", f"{base}/schedules", token, json={"crons": [CRON]})
    print("  ✅ Cron 已设置")

    print("\n🎉 部署完成。")
    print(f"   工作流触发事件：repository_dispatch / pubg-cron")
    print(f"   触发频率：{CRON}（UTC 计时）")
    print("   可在 Cloudflare 控制台 Workers & Pages → %s → Triggers 查看。" % SCRIPT_NAME)


if __name__ == "__main__":
    main()
