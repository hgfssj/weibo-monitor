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

# 加载本地 .env（不入库）：DASHSCOPE_API_KEY 等，供子模块启用大模型研判
_env_path = os.path.join(BASE_DIR, ".env")
if os.path.exists(_env_path):
    with open(_env_path, encoding="utf-8") as f:
        for _line in f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

from weibo_collector import collect_users, load_config, save_config
from xueqiu_collector import collect_users as collect_xueqiu_users
from weibo_filter import is_stock_related
from weibo_summary import analyze_bigv, heuristic_summary, LLM_AVAILABLE
from industry_collector import collect_industries
from industry_summary import (analyze_industry, heuristic_industry_summary,
                                analyze_stock, build_commentary)
from a_share_collector import collect_macro_data
from if_ocr import update_index_futures_positions as update_if_ocr
from index_futures_public import update_index_futures_positions as update_if_public
from national_team_etf import collect as collect_national_team_etf
from industry_turnover import collect as collect_industry_turnover

DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")

POSTS_FILE = os.path.join(DATA_DIR, "weibo_posts.json")
STATE_FILE = os.path.join(DATA_DIR, "weibo_state.json")
PAGE_DATA_FILE = os.path.join(DATA_DIR, "weibo_monitor_data.json")
SUMMARY_CACHE_FILE = os.path.join(DATA_DIR, "weibo_summary_cache.json")
INDUSTRY_DATA_FILE = os.path.join(DATA_DIR, "industry_data.json")
INDUSTRY_CACHE_FILE = os.path.join(DATA_DIR, "industry_summary_cache.json")
MACRO_DATA_FILE = os.path.join(DATA_DIR, "macro_data.json")
FRONTEND_MACRO_DATA_FILE = os.path.join(FRONTEND_DATA_DIR, "macro_data.json")


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


def save_if_posts(data, name=""):
    """写出股指期货大V原文（供前端「股指期货」页展示），不进入主大V视图。"""
    out = {
        "updated_at": datetime.now().isoformat(),
        "user": data.get("user"),
        "posts": data.get("posts", []),
    }
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    save_json(os.path.join(FRONTEND_DATA_DIR, "if_posts.json"), out)
    print(f"  📊 股指期货大V原文已更新({name}): {len(out['posts'])} 条")


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

    # 股指期货大V（机构多空单复盘）：单独采集，不进入主大V视图
    if_cfg = cfg.get("index_futures") or {}
    if_uid = str(if_cfg.get("monitor_uid")) if if_cfg.get("monitor_uid") else None
    if_name = if_cfg.get("monitor_name") or "股指期货机构持仓复盘"

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔍 开始检测 "
          f"微博 {len(weibo_users)} 个 + 雪球 {len(xueqiu_users)} 个账号...")

    # 分平台采集，结果按 平台:uid 归并
    collected = {}
    if weibo_users:
        for uid, data in asyncio.run(
                collect_users(weibo_users, pages=pages, headless=True, cfg=cfg)).items():
            collected[qkey("weibo", uid)] = dict(data, platform="weibo")
    # 雪球采集：主大V + 股指期货大V（并入同一批次，复用浏览器会话，只采集一次）
    xueqiu_to_collect = list(xueqiu_users)
    if if_uid and not any(str(u.get("uid")) == if_uid for u in xueqiu_users):
        xueqiu_to_collect.append({"uid": if_uid, "name": if_name, "fetch_pages": 2})
    if xueqiu_to_collect:
        for uid, data in asyncio.run(
                collect_xueqiu_users(xueqiu_to_collect, pages=pages,
                                     headless=True, cfg=cfg)).items():
            collected[qkey("xueqiu", uid)] = dict(data, platform="xueqiu")
    # 股指期货数据更新
    if_source = (if_cfg.get("source") or "public").lower()
    # 公开渠道直连（中金所每日前20会员持仓）：每轮更新，
    # 不依赖大V发图、不依赖 Tesseract，纯 HTTP 抓取 CSV。
    if if_source == "public":
        try:
            update_if_public(cfg=cfg)
        except Exception as e:
            print(f"  ⚠️ 股指期货公开数据(CFFEX)更新失败(不影响主流程): {e}")

    # 大V原文复盘 feed（可选）+ 可选 OCR 路径
    if if_uid:
        if_data = collected.pop(qkey("xueqiu", if_uid), None)
        if if_data and if_data.get("posts"):
            save_if_posts(if_data, if_name)
            if if_source == "ocr":
                # 对复盘图片做 OCR，自动识别中信/其他大机构净空单变化
                try:
                    update_if_ocr(if_data, cfg=cfg)
                except Exception as e:
                    print(f"  ⚠️ 股指期货图片 OCR 失败(不影响主流程): {e}")
        else:
            print(f"  📊 股指期货大V本轮未采集到新帖（可能风控），沿用既有 if_posts.json")

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
    summary_cache = load_json(SUMMARY_CACHE_FILE, {})
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

        # ---- 大V观点总结（基于近期发言 + 本人评论推断）----
        recent = sorted(
            [p for p in all_posts["posts"]
             if p["uid"] == uid and p.get("platform", "weibo") == platform],
            key=lambda p: p.get("created_at", ""), reverse=True)[:25]
        latest_id = recent[0]["id"] if recent else ""
        s_entry = summary_cache.get(uid, {})
        if LLM_AVAILABLE:
            # 仅当帖子发生变化（或出现/缺失）时才调用大模型，节省额度
            if s_entry.get("last_post_id") != latest_id or "summary" not in s_entry:
                s = analyze_bigv(info.get("name") or uid, platform, recent)
                if s:
                    s_entry = {"last_post_id": latest_id, "summary": s,
                               "generated_at": datetime.now().isoformat(),
                               "source": "llm"}
                    summary_cache[uid] = s_entry
            summary = s_entry.get("summary")
        else:
            # 无大模型：优先保留已生成的缓存（含人工/初始 seed），否则用启发式兜底
            summary = s_entry.get("summary") or heuristic_summary(
                recent, info.get("name") or uid)

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
            "summary": summary,
        })

    page_data = {
        "updated_at": datetime.now().isoformat(),
        "new_count": len(new_posts_total),
        "filtered_count": total_filtered,
        "platforms": platforms,
    }
    save_json(PAGE_DATA_FILE, page_data)
    save_json(SUMMARY_CACHE_FILE, summary_cache)
    # 同步到 frontend/data/
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    shutil.copy2(PAGE_DATA_FILE, os.path.join(FRONTEND_DATA_DIR, "weibo_monitor_data.json"))

    # 行业监控（同一循环、同一间隔；失败不影响大V监控）
    try:
        run_industries(cfg)
    except Exception as e:
        print(f"  ⚠️ 行业监控本轮失败(不影响大V): {e}")

    # A股资金面宏观数据（无浏览器依赖，失败不影响主流程）
    try:
        print("\n  💰 A股资金面: 更新融资融券/投资者/利率/汇率...")
        macro_data = collect_macro_data(days=365)
        os.makedirs(os.path.dirname(MACRO_DATA_FILE), exist_ok=True)
        save_json(MACRO_DATA_FILE, macro_data)
        save_json(FRONTEND_MACRO_DATA_FILE, macro_data)
        print(f"    ✅ 已更新 {len(macro_data['series'])} 个序列，"
              f"区间 {macro_data['date_range']['start']} ~ {macro_data['date_range']['end']}")
    except Exception as e:
        print(f"  ⚠️ A股资金面采集失败(不影响主流程): {e}")

    # 国家队宽基ETF增减持（上交所官方每日ETF份额，无浏览器依赖）
    try:
        print("\n  🏦 国家队宽基ETF: 更新沪市核心ETF份额/净申购...")
        collect_national_team_etf()
        print("    ✅ 国家队ETF数据已更新")
    except Exception as e:
        print(f"  ⚠️ 国家队ETF采集失败(不影响主流程): {e}")

    # 行业成交额占比（申万行业日历史，无浏览器依赖）
    try:
        print("\n  🏭 行业成交占比: 更新申万行业成交额占比趋势...")
        collect_industry_turnover()
        print("    ✅ 行业成交占比数据已更新")
    except Exception as e:
        print(f"  ⚠️ 行业成交占比采集失败(不影响主流程): {e}")

    print(f"\n  ✅ 本轮完成: 新增 {len(new_posts_total)} 条 | "
          f"历史共 {len(all_posts['posts'])} 条")
    return page_data


# ============================ 行业监控 ============================

def run_industries(cfg) -> dict:
    """采集 + 研判各行业（股票池近 7 天帖子/评论），写出 industry_data.json。

    流程：collect_industries（逐股主页抓近 7 天讨论 + 行情 + 精选有价值
    观点及其精彩评论）→ 个股推导 + 行业聚合（LLM/缓存/启发式）→ 有价值
    观点综合点评。复用 STATE_FILE.industry_seen 做新增检测；通过
    industry_summary_cache.json 缓存研判（仅 LLM 可用且内容变化时重算）。
    """
    industries = cfg.get("industries", [])
    if not industries:
        return {}
    print(f"\n  🏭 行业监控: 采集 {len(industries)} 个方向...")

    ind_raw = {}
    try:
        ind_raw = asyncio.run(
            collect_industries(industries, headless=True, cfg=cfg))
    except Exception as e:
        print(f"    ⚠️ 行业采集异常: {e}")

    ind_cache = load_json(INDUSTRY_CACHE_FILE, {})
    state = load_json(STATE_FILE, {"seen_ids": {}, "initialized": {}, "industry_seen": {}})
    industry_seen = state.setdefault("industry_seen", {})
    # 上一轮已生成的行业数据，用于本轮采集为空（雪球瞬时风控）时兜底沿用
    prev_ind = load_json(INDUSTRY_DATA_FILE, {"industries": {}}).get("industries", {})

    industries_out = {}
    total_ind_new = 0
    for ind in industries:
        iid = ind.get("id") or ind.get("name")
        days = int(ind.get("days", 7))
        raw = ind_raw.get(iid, {
            "id": iid, "name": ind.get("name", iid), "icon": ind.get("icon", "🏭"),
            "days": days, "stocks": [], "all_posts": [], "valuable_viewpoints": []})
        # 本轮某行业采集为空时，沿用上一轮的标的/观点，避免看板瞬间空白
        if not raw.get("stocks") and (prev_ind.get(iid) or {}).get("stocks"):
            raw["stocks"] = prev_ind[iid]["stocks"]
        if not raw.get("all_posts") and (prev_ind.get(iid) or {}).get("all_posts"):
            raw["all_posts"] = prev_ind[iid]["all_posts"]
        if not raw.get("valuable_viewpoints") and (prev_ind.get(iid) or {}).get("valuable_viewpoints"):
            raw["valuable_viewpoints"] = prev_ind[iid]["valuable_viewpoints"]

        stocks_raw = raw.get("stocks", [])
        all_posts = raw.get("all_posts", [])
        vp_raw = raw.get("valuable_viewpoints", [])

        # 个股推导（LLM 优先，否则启发式）
        stocks_out = []
        stock_summaries = []
        for s in stocks_raw:
            per = analyze_stock(s.get("name", ""), s.get("posts", []))
            stocks_out.append({
                "symbol": s.get("symbol"), "name": s.get("name"),
                "note": s.get("note", ""), "quote": s.get("quote", {}),
                "posts": s.get("posts", []), "summary": per,
            })
            stock_summaries.append({"name": s.get("name", ""), "summary": per})

        # 行业聚合（LLM / 缓存 / 启发式）
        sig = f"{len(all_posts)}|{len(stocks_out)}|{(all_posts[0]['id'] if all_posts else '')}"
        c = ind_cache.get(iid, {})
        if LLM_AVAILABLE:
            if c.get("sig") != sig or "summary" not in c:
                s = analyze_industry(ind.get("name", iid), stock_summaries,
                                     all_posts, stocks_out)
                if s:
                    c = {"sig": sig, "summary": s,
                         "generated_at": datetime.now().isoformat(),
                         "source": "llm"}
                    ind_cache[iid] = c
            summary = c.get("summary")
        else:
            summary = c.get("summary") or heuristic_industry_summary(
                stock_summaries, all_posts, stocks_out, ind.get("name", iid))

        # 有价值观点 + 综合点评
        valuable = []
        for vp in vp_raw:
            post = vp.get("post", {})
            comments = vp.get("comments", [])
            reply_total = vp.get("reply_count", 0)
            commentary = build_commentary(post, comments, reply_total)
            valuable.append({
                "post": post, "comments": comments,
                "reply_count": reply_total, "commentary": commentary,
            })

        # 新增检测（基于全部帖子 id）
        prev = set(industry_seen.get(iid, []))
        for p in all_posts:
            p["is_new"] = p["id"] not in prev
            if p["is_new"]:
                total_ind_new += 1
        industry_seen[iid] = ([p["id"] for p in all_posts] + list(prev))[:300]

        industries_out[iid] = {
            "id": iid,
            "name": ind.get("name", iid),
            "icon": ind.get("icon", "🏭"),
            "days": days,
            "summary": summary,
            "stocks": stocks_out,
            "viewpoints": all_posts,                 # 全部近 7 天帖子（兼容旧字段）
            "valuable_viewpoints": valuable,
            "posts_count": len(all_posts),
        }

    industry_data = {
        "updated_at": datetime.now().isoformat(),
        "new_count": total_ind_new,
        "industries": industries_out,
    }
    save_json(INDUSTRY_DATA_FILE, industry_data)
    save_json(INDUSTRY_CACHE_FILE, ind_cache)
    save_json(STATE_FILE, state)
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    shutil.copy2(INDUSTRY_DATA_FILE,
                 os.path.join(FRONTEND_DATA_DIR, "industry_data.json"))
    print(f"  ✅ 行业数据已生成: {len(industries_out)} 个方向，"
          f"新增帖子 {total_ind_new} 条")
    return industry_data


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
    parser.add_argument("--industries-only", action="store_true",
                        help="仅运行行业监控（采集+研判并写出 industry_data.json）")
    args = parser.parse_args()

    cfg = load_config()
    if not cfg.get("users"):
        print("❌ weibo_config.json 中没有配置监控账号")
        sys.exit(1)

    # 新部署时从 snapshot/ 恢复长周期历史数据（仅补缺失，不覆盖）
    try:
        import snapshot
        snapshot.restore()
    except Exception as e:
        print(f"  ⚠️ 快照恢复跳过: {e}")

    if args.industries_only:
        if not cfg.get("industries"):
            print("❌ weibo_config.json 中没有配置 industries（行业监控）")
            sys.exit(1)
        run_industries(cfg)
        return

    if args.loop:
        # 单实例锁：两个循环进程会争抢同一 Chromium 登录态目录导致双双 SEGV 崩溃
        pid_file = os.path.join(DATA_DIR, "weibo_monitor.pid")
        if os.path.exists(pid_file):
            try:
                old_pid = int(open(pid_file).read().strip())
                os.kill(old_pid, 0)  # 不发信号，仅检查进程存活
                print(f"❌ 已有监控循环在运行(pid={old_pid})，拒绝重复启动。"
                      f"如需重启请先 kill {old_pid}")
                sys.exit(1)
            except (ProcessLookupError, ValueError):
                pass  # 旧进程已不存在，可接管
            except PermissionError:
                print(f"⚠️ 无法确认旧进程(pid={old_pid})状态，谨慎起见拒绝启动")
                sys.exit(1)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))

        if args.interval:
            print(f"🔄 循环监控模式：固定每 {args.interval} 秒检测一次（Ctrl+C 退出）")
        else:
            print(f"🔄 循环监控模式：工作日 8:00-16:00 每 "
                  f"{int(cfg.get('peak_interval_sec', 480))//60} 分钟，"
                  f"其余时段每 {int(cfg.get('offpeak_interval_sec', 1200))//60} 分钟"
                  f"（Ctrl+C 退出）")
        while True:
            try:
                # 每轮重新读取配置，配置管理页保存后下一轮自动生效
                cfg = load_config()
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
