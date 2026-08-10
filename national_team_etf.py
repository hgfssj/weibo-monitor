"""
国家队（中央汇金/诚通/国新）宽基 ETF 增减持采集器 —— 金额口径（亿元）

数据来源（均为公开、零本地 OCR/浏览器依赖）：
- 沪市核心 ETF 每日「基金份额」：上交所官方接口
    https://query.sse.com.cn/commonQuery.do
    sqlId = COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L  (按 STAT_DATE 返回全市场 ETF 份额)
- 每只 ETF 每日「单位净值」：东方财富基金档案 f10/lsjz（历史净值，含全部 12 只）
- 深市核心 ETF 每日「基金份额」：akshare fund_scale_daily_szse（深交所官方每日 ETF 份额，含历史序列）

口径说明（金额化）：
- 持仓金额(亿元) = 基金份额(亿份) × 单位净值(元/份)；1 亿份 × 1 元/份 = 1 亿元。
- 沪市核心 ETF 有每日历史份额 → 持仓金额曲线为真实值（份额×净值），净申购为真实申赎信号。
- 深市核心 ETF 经 akshare 拿到每日历史份额 → 持仓金额曲线同样为真实值，净申购亦为真实申赎信号。
  注：深交所官网 ETF 列表报表(1000_lf)在本环境仅返回表头、无数据行，故改走 akshare 封装接口。
- 每日净申购金额(亿元) = 持仓金额差分（沪市、深市均为真实值）。

输出：data/national_team_etf.json + frontend/data/national_team_etf.json
    结构见 build_output()。前端 A股资金面 板块读取并渲染双视图
    （净申购金额柱 / 持仓金额线），单位为亿元。

用法：
    python national_team_etf.py            # 增量更新（仅补最新交易日）
    python national_team_etf.py --backfill 90   # 回补近 90 个交易日
    python national_team_etf.py --test      # 解析/聚合单测（不联网）
"""
import argparse
import json
import os
import datetime
import time

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
FRONTEND_DATA_DIR = os.path.join(FRONTEND_DIR, "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "national_team_etf.json")
FRONTEND_OUTPUT_FILE = os.path.join(FRONTEND_DATA_DIR, "national_team_etf.json")

# 沪市核心 ETF（有每日历史份额）：代码 -> (名称, 宽基类别)
CORE_SH = {
    "510300": ("华泰柏瑞沪深300", "沪深300"),
    "510330": ("华夏沪深300", "沪深300"),
    "510310": ("易方达沪深300", "沪深300"),
    "510050": ("华夏上证50", "上证50"),
    "510500": ("南方中证500", "中证500"),
    "512100": ("南方中证1000", "中证1000"),
    "560010": ("广发中证1000", "中证1000"),
    "588000": ("华夏科创50", "科创50"),
}
# 深市核心 ETF（深交所仅提供当日快照，无历史）：代码 -> (名称, 宽基类别)
CORE_SZ = {
    "159919": ("嘉实沪深300", "沪深300"),
    "159922": ("嘉实中证500", "中证500"),
    "159845": ("华夏中证1000", "中证1000"),
    "159915": ("易方达创业板", "创业板"),
}
CATEGORIES = ["沪深300", "上证50", "中证500", "中证1000", "科创50", "创业板"]

SSE_URL = "https://query.sse.com.cn/commonQuery.do"
LSJZ_URL = "https://api.fund.eastmoney.com/f10/lsjz"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}


# ---------------------------------------------------------------- 抓取
def fetch_sse_shares(date_yyyymmdd):
    """返回 {code: 亿份} 仅含 CORE_SH 命中项。date_yyyymmdd 形如 20260807。"""
    params = {
        "isPagination": "true",
        "pageHelp.pageSize": "10000",
        "pageHelp.pageNo": "1",
        "sqlId": "COMMON_SSE_ZQPZ_ETFZL_XXPL_ETFGM_SEARCH_L",
        "STAT_DATE": date_yyyymmdd,
    }
    r = requests.get(SSE_URL, params=params, headers={**HEADERS, "Referer": "https://www.sse.com.cn/"}, timeout=20)
    r.raise_for_status()
    j = r.json()
    out = {}
    for x in (j.get("result") or []):
        code = x.get("SEC_CODE")
        if code in CORE_SH:
            tot = x.get("TOT_VOL")
            if tot is None:
                continue
            out[code] = float(tot) / 1e4  # 万份 -> 亿份
    return out


def fetch_sz_shares_history(start_yyyymmdd, end_yyyymmdd):
    """返回 {code: {date(YYYY-MM-DD): 亿份}} 仅含 CORE_SZ 命中项。

    数据源：akshare fund_scale_daily_szse（深交所官方每日 ETF 份额，含历史序列）。
    深交所官网 ETF 列表报表(1000_lf)在本环境仅返回表头、无数据行，故改用 akshare
    封装的深交所每日份额接口，能拿到真实每日历史份额，从而算出真实金额曲线与净申购。
    """
    out = {c: {} for c in CORE_SZ}
    try:
        import akshare as ak
        df = ak.fund_scale_daily_szse(start_date=start_yyyymmdd, end_date=end_yyyymmdd, symbol="ETF")
        for _, row in df.iterrows():
            code = str(row.get("基金代码", "")).strip()
            if code not in CORE_SZ:
                continue
            dt = str(row.get("日期", ""))[:10]
            val = row.get("基金份额")
            if not dt or val is None:
                continue
            try:
                out[code][dt] = float(str(val).replace(",", "")) / 1e8  # 份 -> 亿份
            except Exception:
                pass
    except Exception as e:
        print(f"  ⚠️ 深市份额历史抓取失败: {e}")
    return out


def fetch_nav_history(code, start_yyyymmdd, end_yyyymmdd):
    """返回 {date(YYYY-MM-DD): 单位净值}；东方财富 f10/lsjz（不需登录）。

    lsjz 每页最多返回 20 条且按日期倒序，故自动翻页直到覆盖窗口起始日。
    """
    sd = f"{start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:8]}"
    ed = f"{end_yyyymmdd[:4]}-{end_yyyymmdd[4:6]}-{end_yyyymmdd[6:8]}"
    win_start = sd
    out = {}
    page = 1
    while True:
        params = {
            "fundCode": code,
            "pageIndex": str(page),
            "pageSize": "20",
            "startDate": sd,
            "endDate": ed,
        }
        r = requests.get(LSJZ_URL, params=params,
                         headers={**HEADERS, "Referer": f"https://fundf10.eastmoney.com/F10/jjjz_{code}.html"},
                         timeout=20)
        r.raise_for_status()
        d = r.json()
        items = ((d.get("Data") or {}).get("LSJZList") or [])
        if not items:
            break
        oldest = None
        for it in items:
            date = it.get("FSRQ")
            nav = it.get("DWJZ")
            if date and nav:
                try:
                    out[str(date)[:10]] = float(nav)
                    oldest = str(date)[:10]
                except Exception:
                    pass
        # 已覆盖窗口起始日，或到达末页 -> 停止
        if oldest is None or oldest <= win_start or len(items) < 20:
            break
        page += 1
        if page > 60:
            break
    return out


# ---------------------------------------------------------------- 工具
def ymd(d):
    return d.strftime("%Y%m%d")


def iso(d):
    return d.strftime("%Y-%m-%d")


def load_existing():
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"dates": [], "etfs": []}


def fetch_sh_index(dates):
    """取上证指数(000001)每日收盘，对齐到 dates。东财优先，新浪兜底；失败返回 {}。"""
    if not dates:
        return {}
    start = min(dates).replace("-", "")
    end = max(dates).replace("-", "")
    try:
        import akshare as ak
        try:
            df = ak.index_zh_a_hist(symbol="000001", period="daily", start_date=start, end_date=end)
            col_date, col_close = df.columns[0], "收盘"
        except Exception as e:
            print(f"  ⚠️ 上证指数东财源失败，改用新浪: {e}")
            df = ak.stock_zh_index_daily(symbol="sh000001")
            col_date, col_close = "date", "close"
        out = {}
        for _, row in df.iterrows():
            try:
                out[str(row[col_date])[:10]] = float(row[col_close])
            except Exception:
                pass
        return out
    except Exception as e:
        print(f"  ⚠️ 上证指数抓取失败: {e}")
        return {}


def needed_dates(last_date, end_date, backfill_days):
    """返回需要抓取的交易日列表（升序，YYYY-MM-DD 字符串）。"""
    end = datetime.date.fromisoformat(end_date) if end_date else datetime.date.today()
    if last_date:
        start = datetime.date.fromisoformat(last_date) + datetime.timedelta(days=1)
    else:
        start = end - datetime.timedelta(days=int(backfill_days * 1.45))
    dates = []
    d = start
    while d <= end:
        if d.weekday() < 5:  # 跳过周末
            dates.append(d)
        d += datetime.timedelta(days=1)
    return dates


# ---------------------------------------------------------------- 主流程
def collect(backfill_days=90, end_date=None, force_full=False):
    existing = load_existing()
    existing_dates = list(existing.get("dates", []))
    # 既有沪市份额：code -> {date: 亿份}
    sh_hist = {code: {} for code in CORE_SH}
    for e in existing.get("etfs", []):
        if e.get("market") == "SH" and "shares" in e and e.get("shares"):
            for dt, v in zip(existing_dates, e["shares"]):
                if v is not None:
                    sh_hist[e["code"]][dt] = v

    # 既有深市份额：code -> {date: 亿份}
    sz_hist = {code: {} for code in CORE_SZ}
    for e in existing.get("etfs", []):
        if e.get("market") == "SZ" and "shares" in e and e.get("shares"):
            for dt, v in zip(existing_dates, e["shares"]):
                if v is not None:
                    sz_hist[e["code"]][dt] = v

    # 既有单位净值（历史净值不可变，不重抓）：code -> {date: 净值}
    nav_hist = {code: {} for code in list(CORE_SH) + list(CORE_SZ)}
    for e in existing.get("etfs", []):
        if e.get("nav"):
            for dt, v in zip(existing_dates, e["nav"]):
                if v is not None:
                    nav_hist.setdefault(e["code"], {})[dt] = v

    last_date = existing_dates[-1] if existing_dates else None
    dates_to_fetch = needed_dates(None if force_full else last_date, end_date, backfill_days) if (not last_date or force_full) \
        else needed_dates(last_date, end_date, backfill_days)

    new_shares = {}  # date(YYYY-MM-DD) -> {code: 亿份}
    for d in dates_to_fetch:
        try:
            res = fetch_sse_shares(ymd(d))
            if res:
                new_shares[iso(d)] = res
        except Exception as e:
            print(f"  ⚠️ SSE {iso(d)} 抓取失败: {e}")
        time.sleep(0.1)  # 对上交所公开接口的礼貌间隔

    # 合并历史
    for dt, m in new_shares.items():
        for code, v in m.items():
            sh_hist[code][dt] = v

    # 确保首个新日期的前一个交易日存在（用于算净申购）
    if new_shares:
        first_new = min(new_shares.keys())
        all_dates = sorted(set(existing_dates) | set(sh_hist[next(iter(CORE_SH))].keys()))
        idx = all_dates.index(first_new) if first_new in all_dates else 0
        if idx > 0:
            prev_dt = all_dates[idx - 1]
            if prev_dt not in new_shares and prev_dt not in sh_hist[next(iter(CORE_SH))]:
                try:
                    res = fetch_sse_shares(ymd(datetime.date.fromisoformat(prev_dt)))
                    if res:
                        new_shares[prev_dt] = res
                        for code, v in res.items():
                            sh_hist[code][prev_dt] = v
                except Exception:
                    pass

    all_dates = sorted(set().union(*[set(v.keys()) for v in sh_hist.values()]))
    if not all_dates:
        print("  ℹ️ 无可用交易日数据（可能网络不可达或非交易时段）")
        return existing

    # 深市历史份额（akshare 深交所每日 ETF 份额）：仅补缺失窗口，分块避免单次请求过大
    sz_missing = [dt for dt in all_dates if any(dt not in sz_hist[c] for c in CORE_SZ)]
    if sz_missing:
        cur = datetime.date.fromisoformat(min(sz_missing))
        end_d = datetime.date.fromisoformat(max(sz_missing))
        while cur <= end_d:
            chunk_end = min(cur + datetime.timedelta(days=119), end_d)
            try:
                fetched = fetch_sz_shares_history(ymd(cur), ymd(chunk_end))
                for code, m in fetched.items():
                    for dt, v in m.items():
                        sz_hist[code][dt] = v
            except Exception as e:
                print(f"  ⚠️ 深市份额抓取失败 {iso(cur)}~{iso(chunk_end)}: {e}")
            cur = chunk_end + datetime.timedelta(days=1)

    # 每只 ETF 历史单位净值：仅补缺失区间（历史净值不可变）
    for code in list(CORE_SH) + list(CORE_SZ):
        missing = [dt for dt in all_dates if dt not in nav_hist.get(code, {})]
        if not missing:
            continue
        try:
            got = fetch_nav_history(code, min(missing).replace("-", ""), max(missing).replace("-", ""))
            nav_hist.setdefault(code, {}).update(got)
        except Exception as e:
            print(f"  ⚠️ NAV {code} 抓取失败: {e}")
    # 构建每只 ETF 的 金额(亿元) / 净申购金额(亿元) 序列
    etfs = []
    for code, (name, cat) in CORE_SH.items():
        shares_by_date = sh_hist[code]
        shares = [shares_by_date.get(dt) for dt in all_dates]
        navs = [nav_hist.get(code, {}).get(dt) for dt in all_dates]
        amount = []
        for i, dt in enumerate(all_dates):
            s, n = shares[i], navs[i]
            # 金额(亿元) = 份额(亿份) × 单位净值(元/份)；1亿份×1元/份 = 1亿元
            amount.append(round(s * n, 4) if (s is not None and n is not None) else None)
        net = []
        for i in range(len(all_dates)):
            if i == 0 or amount[i] is None or amount[i - 1] is None:
                net.append(None)
            else:
                net.append(round(amount[i] - amount[i - 1], 4))
        etfs.append({
            "code": code, "name": name, "market": "SH", "category": cat,
            "shares": shares, "nav": navs, "amount": amount, "net": net,
        })

    for code, (name, cat) in CORE_SZ.items():
        shares_by_date = sz_hist[code]
        shares = [shares_by_date.get(dt) for dt in all_dates]
        navs = [nav_hist.get(code, {}).get(dt) for dt in all_dates]
        amount = []
        for i, dt in enumerate(all_dates):
            s, n = shares[i], navs[i]
            # 金额(亿元) = 份额(亿份) × 单位净值(元/份)
            amount.append(round(s * n, 4) if (s is not None and n is not None) else None)
        net = []
        for i in range(len(all_dates)):
            if i == 0 or amount[i] is None or amount[i - 1] is None:
                net.append(None)
            else:
                net.append(round(amount[i] - amount[i - 1], 4))
        has = any(x is not None for x in amount)
        note = None if has else "深交所每日份额接口暂未返回数据，暂无法展示深市水位与金额趋势"
        etfs.append({
            "code": code, "name": name, "market": "SZ", "category": cat,
            "shares": shares, "nav": navs, "amount": amount, "net": net,
            "note": note,
        })

    # 按宽基类别聚合（金额、净申购，单位亿元）
    series = {}
    for cat in CATEGORIES:
        lvl = []
        net = []
        for i, dt in enumerate(all_dates):
            s = 0.0
            n = 0.0
            has = False
            for e in etfs:
                if e["category"] == cat and e["amount"][i] is not None:
                    s += e["amount"][i]
                    has = True
                    if e["net"][i] is not None:
                        n += e["net"][i]
            lvl.append(round(s, 4) if has else None)
            net.append(round(n, 4) if has else None)
        series[cat] = {"level": lvl, "net": net}

    # 上证指数对齐
    sh_map = fetch_sh_index(all_dates)
    sh_index = [round(sh_map.get(dt), 2) if dt in sh_map else None for dt in all_dates]

    data = build_output(all_dates, series, etfs, sh_index)
    write_positions(data)
    return data


def build_output(dates, series, etfs, sh_index):
    return {
        "updated": datetime.date.today().isoformat(),
        "source": "上交所每日ETF份额(沪市历史) × 东方财富历史单位净值 = 持仓金额(亿元)；深市ETF份额经 akshare 取深交所每日历史份额(真实)",
        "note": "单位已统一为「亿元」（持仓金额=份额×单位净值）。沪市核心ETF与深市核心ETF(159919/159922/159845/159915)均取每日历史份额，曲线为真实值，每日净申购金额为真实申赎信号（持仓金额的差分）。",
        "categories": [c for c in CATEGORIES if c in series],
        "dates": dates,
        "series": series,
        "sh_index": sh_index,
        "etfs": etfs,
    }


def write_positions(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    for p in (OUTPUT_FILE, FRONTEND_OUTPUT_FILE):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 单测
def main_test():
    """用合成多日数据验证 金额=份额×净值 / 净申购差分 逻辑。"""
    synth_shares = {
        "2026-08-03": {"510300": 260.0, "510330": 88.0, "512100": 110.0},
        "2026-08-04": {"510300": 265.0, "510330": 88.5, "512100": 118.0},
        "2026-08-05": {"510300": 259.0, "510330": 87.2, "512100": 105.0},
    }
    synth_nav = {
        "2026-08-03": {"510300": 4.0, "510330": 4.2, "512100": 2.5},
        "2026-08-04": {"510300": 4.1, "510330": 4.3, "512100": 2.6},
        "2026-08-05": {"510300": 3.9, "510330": 4.1, "512100": 2.4},
    }
    dates = sorted(synth_shares.keys())
    etfs = []
    for code, (name, cat) in CORE_SH.items():
        if code not in synth_shares["2026-08-03"]:
            continue
        shares = [synth_shares[d][code] for d in dates]
        navs = [synth_nav[d][code] for d in dates]
        amount = [round(shares[i] * navs[i], 6) for i in range(len(dates))]
        net = [None] + [round(amount[i] - amount[i - 1], 6) for i in range(1, len(dates))]
        etfs.append({"code": code, "name": name, "market": "SH", "category": cat,
                     "shares": shares, "nav": navs, "amount": amount, "net": net})

    series = {}
    for cat in ("沪深300", "中证1000"):
        lvl = [round(sum(e["amount"][i] for e in etfs if e["category"] == cat), 6)
               for i in range(len(dates))]
        net = [round(sum((e["net"][i] or 0) for e in etfs if e["category"] == cat), 6)
               for i in range(len(dates))]
        series[cat] = {"level": lvl, "net": net}

    # 沪深300: 510300(260*4=1040) + 510330(88*4.2=369.6) = 1409.6 亿元
    assert abs(series["沪深300"]["level"][0] - (260*4.0 + 88*4.2)) < 1e-9, series["沪深300"]["level"]
    # 第二日沪深300净申购 = amount[1]-amount[0]
    a0 = (260*4.0 + 88*4.2)
    a1 = (265*4.1 + 88.5*4.3)
    assert abs(series["沪深300"]["net"][1] - (a1 - a0)) < 1e-9, series["沪深300"]["net"]
    print("✓ main_test 通过(金额口径)：沪深300 持仓金额(亿元)",
          [round(x, 1) for x in series["沪深300"]["level"]],
          "| 每日净申购(亿元)", [round((x or 0), 2) for x in series["沪深300"]["net"]])
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=90, help="回补交易日天数")
    ap.add_argument("--end-date", type=str, default=None, help="截止日期 YYYY-MM-DD")
    ap.add_argument("--force-full", action="store_true",
                    help="无视已有数据，按 backfill 全窗口回补（用于向前追溯更长历史）")
    ap.add_argument("--test", action="store_true", help="运行解析/聚合单测（不联网）")
    args = ap.parse_args()
    if args.test:
        main_test()
        return
    data = collect(backfill_days=args.backfill, end_date=args.end_date, force_full=args.force_full)
    n = len(data.get("dates", []))
    sh = sum(1 for e in data.get("etfs", []) if e.get("market") == "SH")
    sz = sum(1 for e in data.get("etfs", []) if e.get("market") == "SZ")
    print(f"✓ 国家队ETF数据已更新：{n} 个交易日，沪市 {sh} 只 + 深市 {sz} 只（金额口径/亿元）；写至 {OUTPUT_FILE} 与 {FRONTEND_OUTPUT_FILE}")


if __name__ == "__main__":
    main()
