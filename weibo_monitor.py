"""
大V监控 — 微博 + 雪球双平台，增量检测新发言 + 生成前端页面数据

用法:
    python weibo_monitor.py                 # 单次检测
    python weibo_monitor.py --loop          # 后台循环（间隔见 weibo_config.json）
    python weibo_monitor.py --loop --interval 600   # 自定义间隔(秒)

产出:
    data/weibo_posts.json          # 全量发言历史（按时间倒序，含平台标记）
    data/weibo_monitor_data.json   # 前端页面数据（platforms 两级结构）
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
from xueqiu_collector import collect_users as collect_xueqiu_users
from weibo_filter import is_stock_related

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

def qkey(platform: str, uid: str) -> str:
    """平台隔离的唯一键，避免微博/雪球 uid 数字撞车"""
    return uid if platform == "weibo" else f"xq:{uid}"


def run_once(cfg) -> dict:
    """执行一轮采集 + 增量检测，返回页面数据"""
    users = cfg.get("users", [])
    pages = cfg.get("pages_per_user", 2)
    weibo_users = [u for u in users if u.get("platform", "weibo") == "weibo"]
    xueqiu_users = [u for u in users if u.get("platform") == "xueqiu"]

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔍 开始检测 "
          f"微博 {len(weibo_users)} 个 + 雪球 {len(xueqiu_users)} 个账号...")

    # 分平台采集，结果按 平台:uid 归并
    collected = {}
    if weibo_users:
        for uid, data in asyncio.run(
                collect_users(weibo_users, pages=pages, headless=True, cfg=cfg)).items():
            collected[qkey("weibo", uid)] = dict(data, platform="weibo")
    if xueqiu_users:
        for uid, data in asyncio.run(
                collect_xueqiu_users(xueqiu_users, pages=pages,
                                     headless=True, cfg=cfg)).items():
            collected[qkey("xueqiu", uid)] = dict(data, platform="xueqiu")

    # 回填昵称到配置：以磁盘最新配置为基准合并写回，
    # 避免循环进程的旧内存副本覆盖手工修改的策略字段
    disk_cfg = load_config()
    if disk_cfg and disk_cfg.get("users"):
        name_map = {qkey(u.get("platform", "weibo"), str(u["uid"])): u.get("name")
                    for u in users if u.get("name")}
        for du in disk_cfg["users"]:
            k = qkey(du.get("platform", "weibo"), str(du.get("uid")))
            if k in name_map:
                du["name"] = name_map[k]
        save_config(disk_cfg)
    else:
        save_config(cfg)

    state = load_json(STATE_FILE, {"seen_ids": {}, "initialized": {}})
    all_posts = load_json(POSTS_FILE, {"posts": []})
    # 兼容历史数据：补充 type / platform 字段
    for p in all_posts["posts"]:
        p.setdefault("type", "post")
        p.setdefault("platform", "weibo")
    history_ids = {p["id"] for p in all_posts["posts"]}

    seen = state.setdefault("seen_ids", {})
    initialized = state.setdefault("initialized", {})
    users_info = {}
    new_posts_total = []

    for key, data in collected.items():
        platform = data.get("platform", "weibo")
        user = data.get("user")
        # 帖子 + 本人评论（含对旧发言的补充留言）统一进入时间线
        posts = data.get("posts", []) + data.get("own_comments", [])
        for p in posts:
            p["platform"] = platform
        if user:
            users_info[key] = user

        if not posts:
            continue

        if not initialized.get(key):
            # 首次采集：作为基线，不标记为新发言（避免刷屏）
            initialized[key] = True
            for p in posts:
                p["is_new"] = False
                if p["id"] not in history_ids:
                    all_posts["posts"].append(p)
            seen[key] = [p["id"] for p in posts]
            name = (user or {}).get("name", key)
            print(f"  [{name}] 首次采集基线: {len(posts)} 条（不计为新发言）")
            continue

        prev = set(seen.get(key, []))
        new_here = []
        for p in posts:
            p["is_new"] = p["id"] not in prev
            if p["is_new"]:
                new_here.append(p)
                if p["id"] not in history_ids:
                    all_posts["posts"].append(p)
        # 更新已见列表（保留最新100条ID）
        seen[key] = ([p["id"] for p in posts] + list(prev))[:100]

        name = (user or {}).get("name", key)
        if new_here:
            print(f"  [{name}] 🆕 发现 {len(new_here)} 条新发言/评论:")
            for p in new_here:
                tag = "💬评论" if p.get("type") == "comment" else (
                    "雪球" if platform == "xueqiu" else "微博")
                snippet = p["text"].replace("\n", " ")[:55]
                print(f"      - [{tag}] {snippet}")
                new_posts_total.append(p)
        else:
            print(f"  [{name}] 无新发言")

    # 历史按时间倒序
    all_posts["posts"].sort(key=lambda p: p.get("created_at", ""), reverse=True)

    # ===== 股市相关性过滤（仅对未分类且所属用户开启过滤的帖子）=====
    filter_flags = {qkey(u.get("platform", "weibo"), str(u["uid"])): u.get("filter", True)
                    for u in users}

    def post_key(p):
        return qkey(p.get("platform", "weibo"), str(p["uid"]))

    pending = [p for p in all_posts["posts"] if "stock_related" not in p]
    if pending:
        print(f"  🔎 对 {len(pending)} 条帖子做股市相关性过滤...")
        for p in pending:
            # 未开启过滤的用户：全量保留
            if not filter_flags.get(post_key(p), True):
                p["stock_related"] = True
                p["filter_method"] = "none"
                continue
            # 回复型评论：短回复（如"很快"）单独看无意义，
            # 需结合被回复的粉丝提问一起判断
            if p.get("type") == "comment" and p.get("reply_to"):
                ctx_text = (f"粉丝提问: {p['reply_to']}\n"
                            f"博主回复: {p.get('text', '')}")
                r = is_stock_related(ctx_text, p.get("original_post", ""))
            else:
                r = is_stock_related(p.get("text", ""), p.get("retweet_text", ""))
            p["stock_related"] = r["related"]
            p["filter_method"] = r["method"]
            mark = "✅" if r["related"] else "❌"
            print(f"    {mark} [{r['method']:7s}] {p['text'][:40]}")
    # 兼容历史：策略改为不过滤的用户，已分类的条目全部恢复展示
    for p in all_posts["posts"]:
        if not filter_flags.get(post_key(p), True):
            p["stock_related"] = True
            p["filter_method"] = "none"

    save_json(POSTS_FILE, all_posts)
    save_json(STATE_FILE, state)

    # ===== 生成页面数据（按平台分组 + 每用户策略）=====
    platforms = {"weibo": [], "xueqiu": []}
    total_filtered = 0
    for u in users:
        platform = u.get("platform", "weibo")
        uid = str(u["uid"])
        key = qkey(platform, uid)
        default_url = (f"https://xueqiu.com/u/{uid}" if platform == "xueqiu"
                       else f"https://weibo.com/u/{uid}")
        info = users_info.get(key, {"uid": uid, "name": u.get("name") or uid})
        limit = int(u.get("display_limit", 50))
        my_posts = [p for p in all_posts["posts"]
                    if p["uid"] == uid and p.get("platform", "weibo") == platform][:limit]
        related = [p for p in my_posts if p.get("stock_related")]
        total_filtered += len(my_posts) - len(related)
        platforms[platform].append({
            "uid": uid,
            "platform": platform,
            "name": info.get("name") or uid,
            "avatar": info.get("avatar", ""),
            "followers": info.get("followers", 0),
            "description": info.get("description", ""),
            "verified": info.get("verified", ""),
            "profile_url": info.get("profile_url", default_url),
            "filter": u.get("filter", True),
            "posts": related,
            "filtered_count": len(my_posts) - len(related),
        })

    page_data = {
        "updated_at": datetime.now().isoformat(),
        "new_count": len(new_posts_total),
        "filtered_count": total_filtered,
        "platforms": platforms,
    }
    save_json(PAGE_DATA_FILE, page_data)
    # 同步到 frontend/data/
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    shutil.copy2(PAGE_DATA_FILE, os.path.join(FRONTEND_DATA_DIR, "weibo_monitor_data.json"))

    print(f"\n  ✅ 本轮完成: 新增 {len(new_posts_total)} 条 | "
          f"历史共 {len(all_posts['posts'])} 条")
    return page_data


# ============================ CLI ============================

def current_interval(cfg) -> int:
    """动态间隔：周一至周五 8:00-16:00 活跃时段 8 分钟，其余时段 20 分钟"""
    now = datetime.now()
    if now.weekday() < 5 and 8 <= now.hour < 16:
        return int(cfg.get("peak_interval_sec", 480))
    return int(cfg.get("offpeak_interval_sec", 1200))


def main():
    parser = argparse.ArgumentParser(description="大V监控（微博+雪球）")
    parser.add_argument("--loop", action="store_true", help="循环监控模式")
    parser.add_argument("--interval", type=int, help="固定轮询间隔秒数（覆盖动态策略）")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.get("users"):
        print("❌ weibo_config.json 中没有配置监控账号")
        sys.exit(1)

    if args.loop:
        if args.interval:
            print(f"🔄 循环监控模式：固定每 {args.interval} 秒检测一次（Ctrl+C 退出）")
        else:
            print(f"🔄 循环监控模式：工作日 8:00-16:00 每 "
                  f"{int(cfg.get('peak_interval_sec', 480))//60} 分钟，"
                  f"其余时段每 {int(cfg.get('offpeak_interval_sec', 1200))//60} 分钟"
                  f"（Ctrl+C 退出）")
        while True:
            try:
                run_once(cfg)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                print(f"  ⚠️ 本轮失败: {e}")
            interval = args.interval or current_interval(cfg)
            print(f"  ⏱️ {datetime.now().strftime('%H:%M:%S')} "
                  f"下次检测在 {interval // 60} 分钟后")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n👋 监控已停止")
                break
    else:
        run_once(cfg)


if __name__ == "__main__":
    main()
