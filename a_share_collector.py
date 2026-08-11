"""
A 股资金面宏观数据采集器
覆盖：融资融券汇总、参与交易投资者数量、平均维持担保比例、
      中美 10 年期国债收益率、人民币汇率中间价

数据源：
- 东方财富 datacenter-web.eastmoney.com (RPTA_WEB_MARGIN_DAILYTRADE)
- akshare: bond_zh_us_rate / currency_boc_safe

用法：
    python a_share_collector.py           # 抓取并保存
    python a_share_collector.py --days 365
"""
import argparse
import json
import os
import sys
from datetime import date, datetime, timedelta

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "macro_data.json")
FRONTEND_OUTPUT_FILE = os.path.join(FRONTEND_DATA_DIR, "macro_data.json")


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def fetch_margin_daily(limit_days=500):
    """
    东方财富融资融券账户/交易每日统计（含融资余额、融券余额、融资买入额、
    融券卖出额、参与交易投资者数量、平均维持担保比例、上证指数收盘）
    """
    url = "https://datacenter-web.eastmoney.com/api/data/v1/get"
    params = {
        "sortColumns": "STATISTICS_DATE",
        "sortTypes": "-1",
        "pageSize": min(500, limit_days),
        "pageNumber": 1,
        "reportName": "RPTA_WEB_MARGIN_DAILYTRADE",
        "columns": "ALL",
        "source": "WEB",
    }
    r = requests.get(url, params=params, timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    data = r.json()
    rows = data.get("result", {}).get("data", [])

    field_map = {
        "fin_balance": ("FIN_BALANCE", "融资余额", "亿元"),
        "loan_balance": ("LOAN_BALANCE", "融券余额", "亿元"),
        "fin_buy": ("FIN_BUY_AMT", "融资买入额", "亿元"),
        "loan_sell": ("LOAN_SELL_AMT", "融券卖出额", "亿元"),
        "investor_num": ("INVESTOR_NUM", "参与交易投资者数量", "名"),
        "avg_guarantee_ratio": ("AVG_GUARANTEE_RATIO", "平均维持担保比例", "%"),
        "sh_close": ("SCI_CLOSE_PRICE", "上证指数", "点"),
    }
    series = {}
    for key, (col, label, unit) in field_map.items():
        series[key] = {"key": key, "label": label, "unit": unit, "values": []}

    for row in rows:
        dt = row.get("STATISTICS_DATE")
        if not dt:
            continue
        ds = str(dt)[:10]
        for key, (col, label, unit) in field_map.items():
            val = row.get(col)
            if val is None or (isinstance(val, float) and val != val):  # NaN
                continue
            try:
                v = float(val)
            except (ValueError, TypeError):
                continue
            series[key]["values"].append({"date": ds, "value": round(v, 4)})

    # 日期升序
    for s in series.values():
        s["values"] = sorted(s["values"], key=lambda x: x["date"])

    return list(series.values())


def fetch_bond_rates(limit_days=365):
    try:
        import akshare as ak
    except ImportError:
        print("[a_share_collector] akshare not installed, skip bond/fx", file=sys.stderr)
        return []

    df = ak.bond_zh_us_rate()
    df = df[["日期", "中国国债收益率10年", "美国国债收益率10年"]].copy()
    df = df.dropna(subset=["日期"])
    df = df.tail(limit_days)

    cn_values, us_values = [], []
    for _, row in df.iterrows():
        ds = str(row["日期"])[:10]
        cn = row["中国国债收益率10年"]
        us = row["美国国债收益率10年"]
        if isinstance(cn, (int, float)) and cn == cn:
            cn_values.append({"date": ds, "value": round(float(cn), 4)})
        if isinstance(us, (int, float)) and us == us:
            us_values.append({"date": ds, "value": round(float(us), 4)})

    return [
        {"key": "cn_bond_10y", "label": "中国10年期国债收益率", "unit": "%", "values": cn_values},
        {"key": "us_bond_10y", "label": "美国10年期国债收益率", "unit": "%", "values": us_values},
    ]


def fetch_fx(limit_days=365):
    try:
        import akshare as ak
    except ImportError:
        return []

    df = ak.currency_boc_safe()
    df = df[["日期", "美元"]].copy()
    df = df.dropna(subset=["日期"])
    df = df.tail(limit_days)

    values = []
    for _, row in df.iterrows():
        ds = str(row["日期"])[:10]
        val = row["美元"]
        if isinstance(val, (int, float)) and val == val:
            # 数据源为 100 美元兑人民币，转为 1 美元兑人民币
            values.append({"date": ds, "value": round(float(val) / 100, 4)})

    return [
        {"key": "usdcny", "label": "人民币汇率中间价", "unit": "元/美元", "values": values},
    ]


def fetch_today_sh_close():
    """新浪/乐咕日线通常当夜或次日才更新；收盘后用腾讯分时末点作为上证指数当日收盘。

    返回 float 或 None（未到收盘/非当日/接口异常）。
    """
    now = datetime.now()
    if now.hour < 15:  # 收盘前不补，避免把盘中价当收盘
        return None
    try:
        import urllib.request
        url = "https://web.ifzq.gtimg.cn/appstock/app/minute/query?code=sh000001"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        d = json.loads(urllib.request.urlopen(req, timeout=8).read())
        inner = d["data"]["sh000001"]["data"]
        if inner.get("date") != now.strftime("%Y%m%d"):
            return None
        lines = inner.get("data", [])
        if len(lines) < 240:  # 未收盘（点数不足全天 242 点）
            return None
        return round(float(lines[-1].split()[1]), 4)
    except Exception as e:
        print(f"[a_share_collector] 上证当日收盘补齐失败: {e}", file=sys.stderr)
        return None


def append_today_sh_close(series):
    """sh_close 统计源 T+1，收盘后用腾讯分时末点补当日值。"""
    today = datetime.now().strftime("%Y-%m-%d")
    for s in series:
        if s.get("key") != "sh_close":
            continue
        vals = s.get("values", [])
        if vals and vals[-1]["date"] >= today:
            return series
        close = fetch_today_sh_close()
        if close is not None:
            vals.append({"date": today, "value": close})
            print(f"[a_share_collector] sh_close 补当日收盘 {today} = {close}（腾讯分时末点）")
    return series


def collect_macro_data(days=365):
    series = []
    series.extend(fetch_margin_daily(limit_days=days + 30))
    series.extend(fetch_bond_rates(limit_days=days))
    series.extend(fetch_fx(limit_days=days))
    series = append_today_sh_close(series)

    # 统一裁剪到最近 days 个交易日/日历日
    start_dt = (date.today() - timedelta(days=days)).isoformat()
    for s in series:
        s["values"] = [v for v in s["values"] if v["date"] >= start_dt]
        s["values"] = s["values"][-days:]

    # 提取日期范围
    all_dates = []
    for s in series:
        all_dates.extend([v["date"] for v in s["values"]])

    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source_days": days,
        "date_range": {
            "start": min(all_dates) if all_dates else None,
            "end": max(all_dates) if all_dates else None,
        },
        "series": series,
    }
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=365,
                        help="采集最近 N 天的数据")
    parser.add_argument("--silent", action="store_true")
    args = parser.parse_args()

    data = collect_macro_data(days=args.days)
    save_json(OUTPUT_FILE, data)
    save_json(FRONTEND_OUTPUT_FILE, data)

    if not args.silent:
        print(f"[a_share_collector] saved {len(data['series'])} series, "
              f"date range {data['date_range']['start']} ~ {data['date_range']['end']}")


if __name__ == "__main__":
    main()
