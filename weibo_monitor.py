"""
微博大V监控 — 增量检测新发言 + 生成前端页面数据

用法:
    python weibo_monitor.py                 # 单次检测
    python weibo_monitor.py --loop          # 后台循环（间隔见 weibo_config.json）
    python weibo_monitor.py --loop --interval 600   # 自定义间隔(秒)

产出:
    data/weibo_posts.json          # 全量发言历史（按时间倒序）
    data/weibo_monitor_data.json   # 前端页面数据（含 is_new 标记）
    frontend/data/weibo_monitor_data.json  # 同步到前端目录
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import time
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from weibo_collector import collect_users, load_config, save_config

DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")

POSTS_FILE = os.path.join(DATA_DIR, "weibo_posts.json")
STATE_FILE = os.path.join(DATA_DIR, "weibo_state.json")
PAGE_DATA_FILE = os.path.join(DATA_DIR, "weibo_monitor_data.json")


# ============================ 持久化 ============================

def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ============================ 核心逻辑 ============================

def run_once(cfg) -> dict:
    """执行一轮采集 + 增量检测，返回页面数据"""
    users = cfg.get("users", [])
    pages = cfg.get("pages_per_user", 2)

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔍 开始检测 {len(users)} 个微博账号...")
    collected = asyncio.run(collect_users(users, pages=pages, headless=True))

    # 回填昵称到配置
    save_config(cfg)

    state = load_json(STATE_FILE, {"seen_ids": {}, "initialized": {}})
    all_posts = load_json(POSTS_FILE, {"posts": []})
    history_ids = {p["id"] for p in all_posts["posts"]}

    seen = state.setdefault("seen_ids", {})
    initialized = state.setdefault("initialized", {})
    users_info = {}
    new_posts_total = []

    for uid, data in collected.items():
        user = data.get("user")
        posts = data.get("posts", [])
        if user:
            users_info[uid] = user

        if not posts:
            continue

        if not initialized.get(uid):
            # 首次采集：作为基线，不标记为新发言（避免刷屏）
            initialized[uid] = True
            for p in posts:
                p["is_new"] = False
                if p["id"] not in history_ids:
                    all_posts["posts"].append(p)
            seen[uid] = [p["id"] for p in posts]
            name = (user or {}).get("name", uid)
            print(f"  [{name}] 首次采集基线: {len(posts)} 条（不计为新发言）")
            continue

        prev = set(seen.get(uid, []))
        new_here = []
        for p in posts:
            p["is_new"] = p["id"] not in prev
            if p["is_new"]:
                new_here.append(p)
                if p["id"] not in history_ids:
                    all_posts["posts"].append(p)
        # 更新已见列表（保留最新100条ID）
        seen[uid] = ([p["id"] for p in posts] + list(prev))[:100]

        name = (user or {}).get("name", uid)
        if new_here:
            print(f"  [{name}] 🆕 发现 {len(new_here)} 条新发言:")
            for p in new_here:
                snippet = p["text"].replace("\n", " ")[:60]
                print(f"      - {snippet}")
                new_posts_total.append(p)
        else:
            print(f"  [{name}] 无新发言")

    # 历史按时间倒序
    all_posts["posts"].sort(key=lambda p: p.get("created_at", ""), reverse=True)
    save_json(POSTS_FILE, all_posts)
    save_json(STATE_FILE, state)

    # ===== 生成页面数据 =====
    page_users = []
    for u in users:
        uid = str(u["uid"])
        info = users_info.get(uid, {"uid": uid, "name": u.get("name") or uid})
        my_posts = [p for p in all_posts["posts"] if p["uid"] == uid][:50]
        page_users.append({
            "uid": uid,
            "name": info.get("name") or uid,
            "avatar": info.get("avatar", ""),
            "followers": info.get("followers", 0),
            "description": info.get("description", ""),
            "verified": info.get("verified", ""),
            "profile_url": info.get("profile_url", f"https://weibo.com/u/{uid}"),
            "posts": my_posts,
        })

    page_data = {
        "updated_at": datetime.now().isoformat(),
        "new_count": len(new_posts_total),
        "users": page_users,
    }
    save_json(PAGE_DATA_FILE, page_data)
    # 同步到 frontend/data/
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    shutil.copy2(PAGE_DATA_FILE, os.path.join(FRONTEND_DATA_DIR, "weibo_monitor_data.json"))

    print(f"\n  ✅ 本轮完成: 新增 {len(new_posts_total)} 条 | "
          f"历史共 {len(all_posts['posts'])} 条")
    return page_data


# ============================ CLI ============================

def main():
    parser = argparse.ArgumentParser(description="微博大V监控")
    parser.add_argument("--loop", action="store_true", help="循环监控模式")
    parser.add_argument("--interval", type=int, help="轮询间隔秒数（覆盖配置）")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.get("users"):
        print("❌ weibo_config.json 中没有配置监控账号")
        sys.exit(1)

    interval = args.interval or cfg.get("poll_interval_sec", 300)

    if args.loop:
        print(f"🔄 循环监控模式：每 {interval} 秒检测一次（Ctrl+C 退出）")
        while True:
            try:
                run_once(cfg)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  ⚠️ 本轮失败: {e}")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n👋 监控已停止")
                break
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
