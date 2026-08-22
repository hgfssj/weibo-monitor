"""
行业历史数据回填 — 利用雪球 symbol/search/status.json 翻页接口，
为每个行业标的抓取尽量深的历史讨论帖（默认回看 90 天），重建历史库。

原理（实测验证）：
  - 该接口带 page 分页参数，每页最多 20 条；
  - 在已打开个股页的浏览器上下文内 fetch（携带 cookie），可正常翻页；
  - 每个标的雪球最多暴露约 1000 条（50 页），冷门标的可跨数月；
  - 雪球风控约限制单轮 100+ 次请求（约 4-5 只标的 × 25 页），需分轮跨时段执行。

用法:
    venv/bin/python industry_backfill.py            # 回填全部行业（幂等，每天只跑一次）
    venv/bin/python industry_backfill.py --force    # 强制重跑（忽略当日完成标记）
    venv/bin/python industry_backfill.py --days 120 # 指定回看天数（默认 90）
    venv/bin/python industry_backfill.py --only AI硬件

设计要点（v2，修复风控中断后误标完成的问题）:
  - 已达深度（回溯 >= 2/3 目标天数）的行业直接跳过，不重抓不重算；
  - 标的级持久缓存 data/backfill_cache.json：成功抓取的标的后不再重抓，
    跨轮/跨天累积（缓存只增不减，配合 update_history 的单调合并历史永不缩水）；
  - 风控中断时抓到一半的标的记为 partial，下轮优先重试；
  - 广度优先：跨行业轮转取标的，保证每轮让所有行业都有增量；
  - backfill_done 仅当「全部行业要么已达深度、要么标的全部抓完」才标记。
"""
import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from playwright.async_api import async_playwright

from xueqiu_collector import launch_context, check_risk
from industry_collector import extract_stock_post, parse_symbol, stock_display_name
import industry_history

PAGE_SIZE = 20          # 接口单页上限（实测）
MAX_PAGES = 25          # 每标的最多翻 25 页（500 条采样，控制总请求量）
SLEEP_PAGE = 2.0        # 翻页间隔（防风控）
SLEEP_STOCK = 5.0       # 标的间隔
COOLDOWN_SEC = 90       # 疑似风控时的冷却等待

TIMELINE_URL = ("https://xueqiu.com/query/v1/symbol/search/status.json"
                "?count=20&comment=0&symbol={sym}&hl=0&source=all&sort=time"
                "&page={page}&q=&type=82")


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


async def fetch_stock_history(page, symbol: str, days: int):
    """在个股页上下文中翻页抓历史帖。返回 (posts, blocked)。
    blocked=True 表示疑似风控，调用方应冷却等待。"""
    cutoff_ts = (datetime.now() - timedelta(days=days)).timestamp() * 1000
    seen, posts = set(), []
    import random

    async def fetch_page(pg):
        try:
            return await asyncio.wait_for(page.evaluate(
                """async (u) => {
                    const r = await fetch(u, {credentials: 'include'});
                    if (!r.ok) return null;
                    const j = await r.json();
                    return (j.list || []).map(x => ({id: x.id, ts: x.created_at, raw: x}));
                }""", TIMELINE_URL.format(sym=symbol, page=pg)), timeout=15)
        except Exception:
            return None

    for pg in range(1, MAX_PAGES + 1):
        ts_list = await fetch_page(pg)
        if ts_list is None:  # 失败：等 3s 重试一次
            await asyncio.sleep(3)
            ts_list = await fetch_page(pg)
        if ts_list is None:  # 仍失败：疑似风控
            print(f"      ⚠️ page={pg} 请求失败（疑似风控）")
            return posts, True
        if not ts_list:
            break
        new_items = [x for x in ts_list if x.get("id") and str(x["id"]) not in seen]
        if not new_items:
            break
        oldest = None
        for x in new_items:
            seen.add(str(x["id"]))
            raw = x.get("raw") or {}
            p = extract_stock_post(raw, symbol)
            p["stock_symbol"] = symbol
            posts.append(p)
            t = x.get("ts") or 0
            if t:
                oldest = t if oldest is None else min(oldest, t)
        # 最早帖子早于回看窗口 → 已翻够，停止
        if oldest is not None and oldest < cutoff_ts:
            break
        await asyncio.sleep(SLEEP_PAGE + random.uniform(0, 0.8))

    # 只保留窗口内帖子，按时间倒序
    cutoff_str = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    posts = [p for p in posts if (p.get("created_at") or "") >= cutoff_str]
    posts.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return posts, False


def _save_cache(path: str, cache: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except Exception:
        pass


def _industry_reach_days(h_ind: dict) -> int:
    """历史库中该行业最早记录距今天数（无记录返回 0）。"""
    ds = sorted((h_ind or {}).get("days", {}).keys())
    if not ds:
        return 0
    try:
        earliest = datetime.strptime(ds[0], "%Y-%m-%d")
    except ValueError:
        return 0
    return max(0, (datetime.now() - earliest).days)


def _industry_complete(ind: dict, h_ind: dict, cache: dict, days: int) -> tuple:
    """判断行业是否回填完成：已达深度 或 全部标的已抓取。返回 (done, reason)。"""
    reach = _industry_reach_days(h_ind)
    if reach >= int(days * 2 / 3):
        return True, f"历史已回溯 {reach} 天"
    iid = ind.get("id") or ind.get("name")
    need = {parse_symbol(s) for s in ind.get("stocks", []) if parse_symbol(s)}
    cached = set(((cache.get(iid) or {}).get("stocks") or {}).keys())
    if need and need <= cached:
        return True, f"{len(need)} 只标的均已抓取（深度受接口上限）"
    return False, ""


async def backfill(days: int, force: bool, only: str = None):
    cfg = load_json(os.path.join(BASE_DIR, "weibo_config.json"), {})
    industries = cfg.get("industries", [])
    if only:
        industries = [i for i in industries
                      if i.get("id") == only or i.get("name") == only]
        if not industries:
            print(f"⚠️ 未找到行业: {only}")
            return
    if not industries:
        print("⚠️ 配置中无行业，先编辑 weibo_config.json")
        return

    hist = industry_history._load_history()
    done_mark = hist.get("backfill_done")
    today = datetime.now().strftime("%Y-%m-%d")
    if done_mark == today and not force:
        print(f"✅ 今日已回填过（{done_mark}），跳过。如需重跑加 --force")
        return

    # 标的级持久缓存（跨轮/跨天累积，只增不减）
    cache_path = os.path.join(BASE_DIR, "data", "backfill_cache.json")
    cache = load_json(cache_path, {})

    # 多机协同：先拉取他机档案并入历史库（他机已抓的深度可直接免抓）
    try:
        import data_sync
        data_sync.pull_and_rebuild(cfg)
    except Exception as e:
        print(f"  ⚠️ 拉取他机档案跳过: {e}")

    # ---- 判定待办：跳过已完成的行业 ----
    todo = []   # [(ind, [待抓标的cfg...]), ...]
    for ind in industries:
        iid = ind.get("id") or ind.get("name")
        name = ind.get("name", iid)
        done, reason = _industry_complete(
            ind, hist.get("industries", {}).get(iid), cache, days)
        if done:
            print(f"[行业] 《{name}》 {reason}，跳过")
            continue
        cached = set(((cache.get(iid) or {}).get("stocks") or {}).keys())
        missing = [s for s in ind.get("stocks", [])
                   if parse_symbol(s) and parse_symbol(s) not in cached]
        if not missing:
            # 无有效标的配置，视为完成避免死循环
            continue
        todo.append((ind, missing))

    if not todo:
        hist["backfill_done"] = today
        industry_history._save_history(hist)
        print("✅ 全部行业均已完成回填（或已达深度），无需抓取")
        return

    total_missing = sum(len(m) for _, m in todo)
    print(f"🚀 行业历史回填：{len(todo)}/{len(industries)} 个方向待补 · "
          f"共 {total_missing} 只标的 · 回看 {days} 天"
          + (f" · 仅 {only}" if only else ""))

    # ---- 广度优先队列：跨行业轮转，每轮让所有行业都有增量 ----
    stock_queue = []
    max_len = max(len(m) for _, m in todo)
    for k in range(max_len):
        for ind, missing in todo:
            if k < len(missing):
                stock_queue.append((ind, missing[k]))

    abort = False
    async with async_playwright() as pw:
        context = await launch_context(pw, headless=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://xueqiu.com/", wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(4)
            if await check_risk(page):
                print("  ⚠️ 雪球风控拦截，本轮无操作")
                return

            fail_streak = 0          # 连续风控失败的标的数
            cooldown = COOLDOWN_SEC  # 指数退避冷却
            for ind, s in stock_queue:
                if abort:
                    break
                iid = ind.get("id") or ind.get("name")
                iname = ind.get("name", iid)
                scache = cache.setdefault(iid, {"name": iname, "stocks": {}})
                scache["name"] = iname
                scache["icon"] = ind.get("icon", "🏭")
                scache.setdefault("partial", {})
                sym = parse_symbol(s)
                if not sym or sym in scache.get("stocks", {}):
                    continue
                sname = stock_display_name(s, sym)
                await page.goto(f"https://xueqiu.com/S/{sym}",
                                wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(3)
                posts, blocked = await fetch_stock_history(page, sym, days)
                for p in posts:
                    p["stock_name"] = sname
                dmin = min((p.get("created_at", "")[:10] for p in posts),
                           default="-")
                dmax = max((p.get("created_at", "")[:10] for p in posts),
                           default="-")
                print(f"  · [{iname}] {sname}({sym}): {len(posts)} 帖  {dmin} ~ {dmax}")
                if blocked:
                    if posts:
                        # 风控中断，部分数据暂存 partial，下轮重试补全
                        scache["partial"][sym] = posts
                        _save_cache(cache_path, cache)
                    fail_streak += 1
                    # 指数退避：90 → 180 → 360s；连续 4 个标的全失败则本轮收尾
                    wait = min(cooldown, 360)
                    cooldown = min(cooldown * 2, 360)
                    print(f"    ⏳ 风控冷却 {wait}s (连续失败 {fail_streak}) ...")
                    await asyncio.sleep(wait)
                    if fail_streak >= 4:
                        print("  🛑 连续风控失败，本轮提前收尾（已有数据写库）")
                        abort = True
                else:
                    # 完整抓取（含 0 帖标的）记入 stocks，此后不再重抓
                    scache["stocks"][sym] = posts
                    scache["partial"].pop(sym, None)
                    _save_cache(cache_path, cache)
                    try:
                        import data_archive
                        data_archive.append_posts(iid, posts)
                    except Exception:
                        pass
                    fail_streak = 0
                    cooldown = COOLDOWN_SEC
                    await asyncio.sleep(SLEEP_STOCK)
                if await check_risk(page):
                    print("  ⚠️ 触发滑块风控，本轮收尾")
                    abort = True
        finally:
            await context.close()

    # ---- 由缓存组装本轮涉及行业的 viewpoints，单调合并进历史库 ----
    result = {}
    for ind, _ in todo:
        iid = ind.get("id") or ind.get("name")
        sc = cache.get(iid) or {}
        posts = []
        for sym, plist in (sc.get("stocks") or {}).items():
            posts.extend(plist or [])
        for sym, plist in (sc.get("partial") or {}).items():
            posts.extend(plist or [])
        if not posts:
            continue
        result[iid] = {
            "id": iid, "name": sc.get("name") or ind.get("name", iid),
            "icon": ind.get("icon", "🏭"), "viewpoints": posts,
        }

    if not result:
        print("❌ 本轮未取到任何帖子（风控?），历史库未做修改")
        return

    # 合并日常采集的最新帖子（industry_data.json），保证近期数据完整
    recent_data = load_json(os.path.join(BASE_DIR, "data", "industry_data.json"), {})
    for iid, ind in result.items():
        recent_posts = ((recent_data.get("industries") or {}).get(iid) or {}) \
            .get("viewpoints") or []
        if recent_posts:
            seen_ids = {str(p.get("id")) for p in ind.get("viewpoints", [])}
            extra = [p for p in recent_posts
                     if str(p.get("id")) not in seen_ids]
            ind["viewpoints"] = list(ind.get("viewpoints", [])) + extra

    industry_data = {"industries": result, "updated_at": datetime.now().isoformat()}
    industry_history.update_history(industry_data, cfg)

    # 为缺失周补摘要
    try:
        industry_history.generate_weekly_summaries(industry_data)
    except Exception as e:
        print(f"  ⚠️ 周摘要生成失败(不影响回填): {e}")

    # ---- done 判定：全部行业「已达深度 或 标的全抓完」才标记 ----
    hist = industry_history._load_history()
    pending = []
    for ind in industries:
        iid = ind.get("id") or ind.get("name")
        done, _ = _industry_complete(
            ind, hist.get("industries", {}).get(iid), cache, days)
        if not done:
            pending.append(ind.get("name", iid))
    if not pending:
        hist["backfill_done"] = today
        print("\n🎉 全部行业回填完成")
    else:
        print(f"\n⏳ 待续（下轮继续）: {', '.join(pending)}")
    industry_history._save_history(hist)

    # 回填档案推送 GitHub（供其他机器共享，失败不影响本地）
    try:
        import data_sync
        data_sync.push_archive()
    except Exception as e:
        print(f"  ⚠️ 档案推送跳过: {e}")

    print("\n📊 当前历史库:")
    for iid, ind in hist["industries"].items():
        ds = sorted(ind.get("days", {}).keys())
        rng = f"{ds[0]} ~ {ds[-1]}" if ds else "无"
        print(f"  {ind.get('name')}: {len(ds)} 天 ({rng}) · "
              f"{len(ind.get('authors', {}))} 位作者")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="回看天数（默认 90）")
    ap.add_argument("--force", action="store_true", help="忽略当日已完成标记强制重跑")
    ap.add_argument("--only", default=None, help="仅回填指定行业 id 或名称")
    args = ap.parse_args()
    asyncio.run(backfill(args.days, args.force, args.only))
