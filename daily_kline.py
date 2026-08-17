#!/usr/bin/env python3
"""日K线数据采集：主要指数 + 申万行业指数日线 OHLC。

数据源：
  - 新浪（ak.stock_zh_index_daily）：8 个主要指数日线 OHLC
  - 申万行业指数（ak.index_hist_sw）：31 个一级行业 + 10 个二级热门行业日线 OHLC

输出 data/daily_kline.json 与 frontend/data/ 副本，保留近 250 个交易日。
用法：
    python daily_kline.py                     # 全量采集
    python daily_kline.py --backfill 250      # 指定保留天数
"""

import json
import os
import sys
import time
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(SCRIPT_DIR, "frontend", "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "daily_kline.json")
FRONTEND_OUTPUT = os.path.join(FRONTEND_DATA_DIR, "daily_kline.json")

KEEP_DAYS = 360

# 主要指数
INDEXES = [
    ("sh_index", "上证指数", "sh000001", "sina"),
    ("sse50", "上证50", "sh000016", "sina"),
    ("csi300", "沪深300", "sh000300", "sina"),
    ("csi300_growth", "300成长", "000918", "csindex"),
    ("csi300_div", "300红利", "000821", "csindex"),
    ("csi500", "中证500", "sh000905", "sina"),
    ("csi1000", "中证1000", "sh000852", "sina"),
    ("csi2000", "中证2000", "932000", "csindex"),
    ("chinext", "创业板指", "sz399006", "sina"),
    ("star50", "科创50", "sh000688", "sina"),
]

# 申万一级行业（与 industry_turnover.py FIRST_LEVEL 一致）
FIRST_LEVEL = {
    "801010": "农林牧渔", "801030": "基础化工", "801040": "钢铁", "801050": "有色金属",
    "801080": "电子", "801880": "汽车", "801110": "家用电器", "801120": "食品饮料",
    "801130": "纺织服饰", "801140": "轻工制造", "801150": "医药生物", "801160": "公用事业",
    "801170": "交通运输", "801180": "房地产", "801200": "商贸零售", "801210": "社会服务",
    "801780": "银行", "801790": "非银金融", "801230": "综合", "801710": "建筑材料",
    "801720": "建筑装饰", "801730": "电力设备", "801890": "机械设备", "801740": "国防军工",
    "801750": "计算机", "801760": "传媒", "801770": "通信", "801950": "煤炭",
    "801960": "石油石化", "801970": "环保", "801980": "美容护理",
}

# 申万二级热门（与 serve.py SW_SECOND_FOCUS 一致）
SW_SECOND_FOCUS = [
    ("801081", "半导体"), ("801104", "软件开发"), ("801078", "自动化设备"),
    ("801193", "证券"), ("801194", "保险"), ("801125", "白酒"),
    ("801737", "电池"), ("801085", "消费电子"), ("801151", "化学制药"), ("801735", "光伏设备"),
]


def _fetch_sina_index(symbol):
    """新浪指数日线，返回 [(date, open, high, low, close), ...] 升序。"""
    import akshare as ak

    df = ak.stock_zh_index_daily(symbol=symbol)
    out = []
    for _, row in df.iterrows():
        d = str(row["date"])[:10]
        try:
            out.append((d, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
        except Exception:
            continue
    return out


def _fetch_csindex(code):
    """中证指数官网（中证2000），返回 [(date, open, high, low, close), ...] 升序。"""
    import akshare as ak

    start = (date.today() - timedelta(days=int(KEEP_DAYS * 2.5))).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    df = ak.stock_zh_index_hist_csindex(symbol=code, start_date=start, end_date=end)
    out = []
    for _, row in df.iterrows():
        d = str(row["日期"])[:10]
        try:
            out.append((d, float(row["开盘"]), float(row["最高"]),
                        float(row["最低"]), float(row["收盘"])))
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out


def _fetch_sw_index(code):
    """申万行业指数日线，返回 [(date, open, high, low, close), ...] 升序。"""
    import akshare as ak

    df = ak.index_hist_sw(symbol=code, period="day")
    out = []
    for _, row in df.iterrows():
        d = str(row["日期"])[:10]
        try:
            out.append((d, float(row["开盘"]), float(row["最高"]),
                        float(row["最低"]), float(row["收盘"])))
        except Exception:
            continue
    return out


def _build_kline_series(rows, backfill_days):
    """将 [(date, o, h, l, c), ...] 转换为按日期排序的 OHLC 数组，保留最近 backfill_days 天。"""
    rows.sort(key=lambda x: x[0])
    rows = rows[-backfill_days:]
    return {
        "dates": [r[0] for r in rows],
        "open": [round(r[1], 2) for r in rows],
        "high": [round(r[2], 2) for r in rows],
        "low": [round(r[3], 2) for r in rows],
        "close": [round(r[4], 2) for r in rows],
    }


def collect(backfill_days=KEEP_DAYS):
    """采集并写入 daily_kline.json。返回输出 dict。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)

    # 主要指数日K
    index_kline = []
    for key, name, symbol, src in INDEXES:
        try:
            rows = _fetch_csindex(symbol) if src == "csindex" else _fetch_sina_index(symbol)
            series = _build_kline_series(rows, backfill_days)
            series["key"] = key
            series["name"] = name
            series["symbol"] = symbol
            index_kline.append(series)
            print(f"  📈 {name}({symbol}): {len(series['dates'])} 天")
        except Exception as e:
            print(f"  ⚠️ {name}({symbol}) 抓取失败: {e}")
            index_kline.append({"key": key, "name": name, "symbol": symbol,
                                "dates": [], "open": [], "high": [], "low": [], "close": [],
                                "error": str(e)[:80]})
        time.sleep(0.2)

    # 申万行业日K（一级 31 + 二级热门 10）
    industry_kline = []
    all_sw = list(FIRST_LEVEL.items()) + [(c, n) for c, n in SW_SECOND_FOCUS]
    for code, name in all_sw:
        try:
            rows = _fetch_sw_index(code)
            series = _build_kline_series(rows, backfill_days)
            series["key"] = code
            series["name"] = name
            industry_kline.append(series)
            print(f"  🏭 {name}({code}): {len(series['dates'])} 天")
        except Exception as e:
            print(f"  ⚠️ {name}({code}) 抓取失败: {e}")
            industry_kline.append({"key": code, "name": name,
                                    "dates": [], "open": [], "high": [], "low": [], "close": [],
                                    "error": str(e)[:80]})
        time.sleep(0.1)

    data = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "index_kline": index_kline,
        "industry_kline": industry_kline,
    }
    for p in (OUTPUT_FILE, FRONTEND_OUTPUT):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    return data


if __name__ == "__main__":
    days = KEEP_DAYS
    for arg in sys.argv[1:]:
        if arg.startswith("--backfill="):
            try:
                days = int(arg.split("=", 1)[1])
            except ValueError:
                pass
    collect(backfill_days=days)