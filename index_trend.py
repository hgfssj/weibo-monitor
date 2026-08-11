#!/usr/bin/env python3
"""主要指数走势采集器：近一年日线收盘价（7 个可选指数）。

数据源（本环境验证）：
  - 新浪（ak.stock_zh_index_daily）：上证50/沪深300/中证500/中证1000/创业板指/科创50
  - 中证指数官网（ak.stock_zh_index_hist_csindex）：中证2000（新浪/腾讯源无此代码）

输出 data/index_trend.json 与 frontend/data/ 副本：
  {
    "updated_at": "...",
    "indexes": [ {"key","name","symbol","dates":[...],"closes":[...]} ... ]
  }
历史收盘价不可变，每轮仅追加新交易日；与已有数据做增量合并。
"""

import json
import os
import sys
import time
from datetime import date, timedelta

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(SCRIPT_DIR, "frontend", "data")

# 保留的交易日数（覆盖「近一年」视图，略多留余量）
KEEP_DAYS = 270

INDEXES = [
    # key, 名称, 数据源代码, 渠道
    ("sh_index", "上证指数", "sh000001", "sina"),
    ("sse50", "上证50", "sh000016", "sina"),
    ("csi300", "沪深300", "sh000300", "sina"),
    ("csi500", "中证500", "sh000905", "sina"),
    ("csi1000", "中证1000", "sh000852", "sina"),
    ("csi2000", "中证2000", "932000", "csindex"),
    ("chinext", "创业板指", "sz399006", "sina"),
    ("star50", "科创50", "sh000688", "sina"),
]


def _fetch_sina(symbol):
    """新浪全历史日线，返回 [(date, close), ...] 升序。"""
    import akshare as ak

    df = ak.stock_zh_index_daily(symbol=symbol)
    out = []
    for _, row in df.iterrows():
        d = str(row["date"])[:10]
        try:
            out.append((d, float(row["close"])))
        except Exception:
            continue
    return out


def _fetch_csindex(code):
    """中证指数官网，返回 [(date, close), ...] 升序。"""
    import akshare as ak

    start = (date.today() - timedelta(days=int(KEEP_DAYS * 1.8))).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    df = ak.stock_zh_index_hist_csindex(symbol=code, start_date=start, end_date=end)
    out = []
    for _, row in df.iterrows():
        d = str(row["日期"])[:10]
        try:
            out.append((d, float(row["收盘"])))
        except Exception:
            continue
    out.sort(key=lambda x: x[0])
    return out


def collect(backfill_days=KEEP_DAYS):
    """采集并写入 index_trend.json（增量合并）。返回输出 dict。"""
    out_path = os.path.join(DATA_DIR, "index_trend.json")
    front_path = os.path.join(FRONTEND_DATA_DIR, "index_trend.json")

    existing = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                for e in json.load(f).get("indexes", []):
                    existing[e["key"]] = dict(zip(e["dates"], e["closes"]))
        except Exception:
            existing = {}

    indexes = []
    for key, name, symbol, src in INDEXES:
        hist = dict(existing.get(key, {}))
        last_known = max(hist.keys()) if hist else None
        try:
            rows = _fetch_csindex(symbol) if src == "csindex" else _fetch_sina(symbol)
            added = 0
            for d, c in rows:
                if last_known is None or d > last_known:
                    hist[d] = c
                    added += 1
            print(f"  📈 {name}({symbol}): +{added} 天，共 {len(hist)} 天")
        except Exception as e:
            print(f"  ⚠️ {name}({symbol}) 抓取失败: {e}")
        dates = sorted(hist.keys())[-backfill_days:]
        indexes.append(
            {
                "key": key,
                "name": name,
                "symbol": symbol,
                "dates": dates,
                "closes": [round(hist[d], 2) for d in dates],
            }
        )
        time.sleep(0.2)

    data = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "indexes": indexes,
    }
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    for p in (out_path, front_path):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    return data


if __name__ == "__main__":
    days = KEEP_DAYS
    for arg in sys.argv[1:]:
        if arg.startswith("--days="):
            try:
                days = int(arg.split("=", 1)[1])
            except ValueError:
                pass
    collect(backfill_days=days)
