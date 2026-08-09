"""
行业监控采集器 v3 — 按「行业方向 → 股票池」逐一采集：

  1. 股票池：每个行业方向在 weibo_config.json 的 industries[].stocks 中
     以「雪球个股主页 URL」配置（示例：https://xueqiu.com/S/SH688836），
     自动解析出 symbol（支持 A 股 SH/SZ、港股 5 位代码、美股原始代码）。
  2. 每个股票：导航其雪球主页（/S/{symbol}），拦截
     query/v1/symbol/search/status.json（SPA 自带 md5__1038 反爬参数），
     取出该股票近 N 天（默认 7 天）的讨论新帖。
  3. 行情快照：复用 quote.json 取价格/涨跌幅。
  4. 有价值的观点：跨股票按互动量选取 Top 帖子，再用 statuses/show.json
     （无需反爬参数）抓取「精彩评论」(excellent_comments) 作为评论样本。

说明：
  - 雪球旧版 stock_timeline.json / search.json 已失效（10020/空体），
    且裸 fetch 任何带 md5 的接口都会被反爬拦截；只有让浏览器打开对应
    SPA 页、由页面自身 JS 发起请求时才拿得到数据。评论完整列表同样被
    md5 拦截，仅 show.json 的 excellent_comments（精彩评论）可免参获取，
    故「评论」以精彩评论为最佳可得样本，余下以帖子互动量(回复数)表征讨论热度。
  - 与大V采集器共用同一套 Playwright 持久上下文 + cookie（data/xueqiu_profile）。

用法:
    from industry_collector import collect_industries
    result = asyncio.run(collect_industries(industries_cfg))
    # result[industry_id] = {
    #   "id","name","icon","days",
    #   "stocks": [{"symbol","name","note","quote":{...},"posts":[标准帖子...]}],
    #   "all_posts": [标准帖子(含 stock_symbol/stock_name), 跨股票聚合],
    #   "valuable_viewpoints": [{"post":标准帖子,"comments":[标准评论...]}]
    # }
"""
import asyncio
import json
import re
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

from xueqiu_collector import (
    launch_context,
    safe_fetch,
    strip_html,
    parse_ts,
    check_risk,
)

SEARCH_WAIT = 6           # 个股主页加载 + SPA 发请求的等待秒数
SLEEP_BETWEEN_STOCK = 1.5
SLEEP_BETWEEN_QUOTE = 1.0
VALUABLE_PER_INDUSTRY = 8  # 每个行业选取的「有价值观点」上限


# ============================ 数据解析 ============================

def extract_stock_post(status: dict, stock_name: str) -> dict:
    """把雪球搜索/时间线的 status 转为与微博统一的标准帖子（带作者）。

    stock_name 作为来源标签（行业方向名或代表股名）。
    """
    sid = str(status.get("id"))
    text = strip_html(status.get("text") or status.get("description") or "")

    user = status.get("user") or {}
    author = user.get("screen_name") or status.get("user_name") or "雪球用户"

    pics = []
    pic = status.get("pic")
    if pic:
        pics.append(pic if pic.startswith("http") else f"https:{pic}")

    rt = status.get("retweeted_status")
    retweet_text = ""
    if rt:
        rt_user = ((rt.get("user") or {}).get("screen_name")) or "已注销"
        retweet_text = f"@{rt_user}: {strip_html(rt.get('text') or rt.get('description') or '')}"

    target = status.get("target") or f"/statuses/{sid}"
    url = (f"https://xueqiu.com{target}"
           if str(target).startswith("/") else f"https://xueqiu.com/statuses/{sid}")

    return {
        "id": f"xqsv_{sid}",          # 前缀区分于大V自身帖子，避免 id 撞车
        "type": "post",
        "platform": "xueqiu",
        "uid": str(user.get("id") or author),
        "author": author,
        "text": text,
        "created_at": parse_ts(status.get("created_at")),
        "source": "雪球·" + stock_name,
        "source_symbol": "",
        "source_stock_name": stock_name,
        "reposts": status.get("retweet_count", 0),
        "comments": status.get("reply_count", 0),
        "likes": status.get("like_count", 0),
        "pics": pics,
        "is_retweet": bool(rt),
        "retweet_text": retweet_text,
        "url": url,
        "collected_at": datetime.now().isoformat(),
    }


def _find_quote_obj(o):
    """递归找到含 current 字段的行情对象。"""
    if isinstance(o, dict):
        if "current" in o:
            return o
        for v in o.values():
            r = _find_quote_obj(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_quote_obj(v)
            if r:
                return r
    return None


async def parse_v5_quote(caps, symbol: str) -> dict:
    """从主页 SPA 拦截到的 v5/stock/quote.json 响应解析行情。

    该接口由页面自带 md5__1038 触发，覆盖 A/港/美全市场（含科创板），
    比旧的 stock/quote.json?code= 可靠（旧接口对 688 科创板返回 21911）。
    """
    for fut in caps:
        try:
            d = json.loads(await fut)
        except Exception:
            continue
        q = _find_quote_obj(d)
        if not isinstance(q, dict):
            continue
        if q.get("symbol") not in (None, "", symbol) and q.get("symbol") != symbol:
            # 命中其它标的的行情（批量响应），跳过
            if q.get("current") is not None:
                # 仍可能是本标的（symbol 字段偶缺），价格有效即采用
                pass
        price = q.get("current")
        if price is None:
            continue
        return {
            "price": price,
            "change_pct": q.get("percent"),
            "market_cap": q.get("market_capital"),
            "pe_ttm": q.get("pe_ttm"),
        }
    return {}


# ============================ 配置解析 ============================

def parse_symbol(stock_cfg: dict) -> str:
    """从配置解析雪球 symbol。优先 url（/S/{symbol}），其次 symbol 字段。"""
    url = (stock_cfg.get("url") or "").strip().rstrip("/")
    if url:
        m = re.search(r"/S/([^/?#]+)", url)
        if m:
            return m.group(1).upper()
    sym = (stock_cfg.get("symbol") or "").strip().upper()
    return sym


def stock_display_name(stock_cfg: dict, symbol: str) -> str:
    return (stock_cfg.get("name") or symbol or "").strip()


# ============================ 评论解析 ============================

def extract_comment(c: dict, post_id: str) -> dict:
    """把 show.json 的 excellent_comments 项转为标准评论结构。"""
    sid = str(c.get("id") or c.get("status_id") or "")
    text = strip_html(c.get("text") or c.get("description") or "")
    user = c.get("user") or {}
    author = user.get("screen_name") or c.get("user_name") or "雪球用户"
    return {
        "id": f"xqc_{sid}" if sid else f"xqc_{post_id}_{id(c)}",
        "type": "comment",
        "platform": "xueqiu",
        "uid": str(user.get("id") or author),
        "author": author,
        "text": text,
        "created_at": parse_ts(c.get("created_at")),
        "source": "雪球·精彩评论",
        "likes": c.get("like_count", 0),
        "reply_count": c.get("reply_count", 0),
        "post_id": post_id,
        "url": f"https://xueqiu.com/statuses/{post_id}",
        "collected_at": datetime.now().isoformat(),
    }


async def fetch_post_detail(page, post_id: str) -> dict:
    """用 show.json（免反爬参数）取帖子详情，抽取精彩评论。"""
    data = await safe_fetch(page, f"https://xueqiu.com/statuses/show.json?id={post_id}")
    if not isinstance(data, dict) or data.get("error"):
        return {"comments": [], "reply_count": 0}
    ec = data.get("excellent_comments") or []
    comments = []
    for c in ec:
        if isinstance(c, dict) and (c.get("text") or c.get("description")):
            comments.append(extract_comment(c, str(post_id)))
    return {
        "comments": comments,
        "reply_count": data.get("reply_count", 0),
        "talk_count": data.get("talk_count", 0),
    }


# ============================ 时间过滤 ============================

def within_days(created_at: str, days: int) -> bool:
    """created_at 为本地时间字符串 %Y-%m-%d %H:%M:%S；判断是否在 days 天内。"""
    if not created_at:
        return False
    try:
        dt = datetime.strptime(created_at, "%Y-%m-%d %H:%M:%S")
    except Exception:
        return False
    return dt >= (datetime.now() - timedelta(days=days))


# ============================ 数据采集 ============================

async def fetch_stock_timeline(page, symbol: str, days: int):
    """导航个股主页，拦截 symbol/search/status.json（近 days 天讨论帖）
    与 v5/stock/quote.json（行情快照）。返回 (posts, quote)。"""
    captured = []
    quote_caps = []

    def on_response(resp):
        try:
            u = resp.url
            if "symbol/search/status.json" in u and resp.status == 200:
                captured.append(asyncio.ensure_future(resp.text()))
            elif "v5/stock/quote.json" in u and resp.status == 200:
                quote_caps.append(asyncio.ensure_future(resp.text()))
        except Exception:
            pass

    page.on("response", on_response)
    try:
        await page.goto(f"https://xueqiu.com/S/{symbol}",
                        wait_until="domcontentloaded", timeout=30000)
        # 滚动数次，尝试触发讨论区可能的分页（多数仅第 1 页 ~10 条）
        for _ in range(4):
            await page.mouse.wheel(0, 900)
            await asyncio.sleep(1.2)
        await asyncio.sleep(1.5)
    except Exception as e:
        print(f"    ⚠️ 个股 {symbol} 主页跳转失败: {e}")
    finally:
        page.remove_listener("response", on_response)

    items = []
    for fut in captured:
        try:
            txt = await fut
            if not txt:
                continue
            data = json.loads(txt)
            for it in (data.get("list") or []):
                if isinstance(it, dict) and it.get("id"):
                    items.append(it)
        except Exception:
            continue

    # 去重 + 解析 + 7 天过滤
    seen, posts = set(), []
    for it in items:
        sid = str(it.get("id"))
        if sid in seen:
            continue
        seen.add(sid)
        p = extract_stock_post(it, symbol)
        p["stock_symbol"] = symbol
        if within_days(p.get("created_at", ""), days):
            posts.append(p)
    posts.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    quote = await parse_v5_quote(quote_caps, symbol)
    return posts, quote


async def collect_industries(industries: list, headless: bool = True,
                             cfg=None) -> dict:
    """采集多个行业方向。返回 {industry_id: {...}}，结构见模块 docstring。"""
    result = {}
    async with async_playwright() as p:
        context = await launch_context(p, headless=headless)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://xueqiu.com/", wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(4)
            if await check_risk(page):
                print("  ⚠️ 雪球触发滑块风控，行业监控本轮降级跳过")
                return result

            for ind in industries:
                iid = ind.get("id") or ind.get("name")
                name = ind.get("name", iid)
                days = int(ind.get("days", 7))
                stocks_cfg = ind.get("stocks", [])
                print(f"  [行业] 《{name}》 近 {days} 天 · {len(stocks_cfg)} 只标的 ...")

                stock_entries = []
                all_posts = []
                for s in stocks_cfg:
                    sym = parse_symbol(s)
                    sname = stock_display_name(s, sym)
                    if not sym:
                        continue
                    posts, quote = await fetch_stock_timeline(page, sym, days)
                    price = quote.get("price")
                    chg = quote.get("change_pct")
                    try:
                        chg_f = float(chg)
                        chg_str = f" ({chg_f:+.2f}%)"
                    except (TypeError, ValueError):
                        chg_str = ""
                    print(f"    · {sname}({sym}) 近{days}天 {len(posts)} 帖"
                          + (f" | {price}{chg_str}"
                             if price is not None else " | 行情缺失"))
                    for p in posts:
                        p["stock_name"] = sname
                        all_posts.append(p)
                    stock_entries.append({
                        "symbol": sym, "name": sname,
                        "note": s.get("note", ""), "quote": quote,
                        "posts": posts,
                    })
                    await asyncio.sleep(SLEEP_BETWEEN_STOCK)
                    if await check_risk(page):
                        print("    ⚠️ 触发滑块风控，终止后续行业采集")
                        break

                # 选取有价值的观点：仅取近 3 天，跨股票按互动量 Top
                recent_posts = [p for p in all_posts
                                if within_days(p.get("created_at", ""), 3)]
                valuable = select_valuable(recent_posts, VALUABLE_PER_INDUSTRY)
                vp_out = []
                for v in valuable:
                    pid = v["id"].replace("xqsv_", "")
                    detail = await fetch_post_detail(page, pid)
                    vp_out.append({
                        "post": v,
                        "comments": detail.get("comments", []),
                        "reply_count": detail.get("reply_count", 0),
                    })
                    await asyncio.sleep(0.8)

                result[iid] = {
                    "id": iid,
                    "name": name,
                    "icon": ind.get("icon", "🏭"),
                    "days": days,
                    "stocks": stock_entries,
                    "all_posts": all_posts,
                    "valuable_viewpoints": vp_out,
                }
        finally:
            await context.close()
    return result


def select_valuable(posts: list, limit: int) -> list:
    """按互动量（赞 + 3×回复）排序取 Top，作为「有价值的观点」候选。"""
    def score(p):
        return (int(p.get("likes") or 0) + 3 * int(p.get("comments") or 0))
    ranked = sorted(posts, key=score, reverse=True)
    return ranked[:limit]


if __name__ == "__main__":
    import shutil
    sample = [{
        "id": "ai_hardware", "name": "AI硬件", "icon": "🔧", "days": 7,
        "stocks": [
            {"url": "https://xueqiu.com/S/SH688256", "name": "寒武纪"},
            {"url": "https://xueqiu.com/S/NVDA", "name": "英伟达"},
        ],
    }]
    out = asyncio.run(collect_industries(sample, headless=True))
    for k, v in out.items():
        print(k, v["name"], "stocks:", [(s["name"], len(s["posts"])) for s in v["stocks"]])
        print("  valuable:", [(x["post"]["author"], len(x["comments"]))
                              for x in v["valuable_viewpoints"]])
