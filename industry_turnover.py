"""
A股行业成交额占比趋势采集器 —— 申万行业分类（亿元口径）

数据来源（akshare index_hist_sw，申万行业指数日历史，单次调用返回全历史）：
- 31 个申万一级行业（覆盖全部沪深A股，合计 ≈ 两市总成交额）
- 3 个重点二级行业：半导体(801081) / 软件开发(801104) / 自动化设备·机器人(801078)
- 1 个虚拟「大消费」聚合 = 食品饮料+家用电器+商贸零售+美容护理+纺织服饰+社会服务

占比口径：行业成交额 ÷ 31个一级行业成交额合计 × 100（%），观察行业交易拥挤度/资金集中度。

输出：data/industry_turnover.json + frontend/data/industry_turnover.json
用法：
    python industry_turnover.py                         # 增量更新（补新交易日）
    python industry_turnover.py --backfill 360 --force-full  # 全量回补 360 交易日
"""
import argparse
import datetime
import json
import os
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")
OUTPUT_FILE = os.path.join(DATA_DIR, "industry_turnover.json")
FRONTEND_OUTPUT_FILE = os.path.join(FRONTEND_DATA_DIR, "industry_turnover.json")

# 申万一级行业（2021版，31个）：代码 -> 名称
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
# 重点关注二级行业：代码 -> (名称, 显示名)
SECOND_EXTRA = {
    "801081": ("半导体", "半导体(芯片)"),
    "801104": ("软件开发", "软件开发"),
    "801078": ("自动化设备", "机器人(自动化设备)"),
}
# 虚拟「大消费」聚合成分（一级行业）
CONSUME_CODES = ["801120", "801110", "801200", "801980", "801130", "801210"]
# 默认重点展示顺序
FOCUS_ORDER = ["801081", "801790", "801104", "consume", "801078", "801150"]


def fetch_index_history(code):
    """返回 {date(YYYY-MM-DD): 成交额(亿元)}，申万行业指数全历史。"""
    import akshare as ak
    df = ak.index_hist_sw(symbol=code, period="day")
    out = {}
    for _, row in df.iterrows():
        try:
            out[str(row["日期"])[:10]] = float(row["成交额"])
        except Exception:
            pass
    return out


def load_existing():
    if os.path.exists(OUTPUT_FILE):
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return None


def collect(backfill_days=360, force_full=False):
    existing = load_existing()

    # 抓取 31 个一级行业 + 3 个重点二级行业全历史
    amount_hist = {}
    for code in list(FIRST_LEVEL) + list(SECOND_EXTRA):
        try:
            amount_hist[code] = fetch_index_history(code)
        except Exception as e:
            print(f"  ⚠️ 申万行业 {code} 抓取失败: {e}")
            amount_hist[code] = {}
        time.sleep(0.1)

    # 日期全集：以数据最全的一级行业为基准
    base_dates = max((v.keys() for v in amount_hist.values() if v),
                     key=lambda s: len(s), default=[])
    all_dates = sorted(base_dates)
    if not all_dates:
        print("  ℹ️ 无可用行业数据")
        return existing or {"dates": [], "industries": []}

    # 窗口：max(回补深度, 已有历史长度)，向前保留已有快照数据
    keep = backfill_days
    if existing and not force_full:
        keep = max(keep, len(existing.get("dates", [])))
    all_dates = all_dates[-keep:]
    today = datetime.date.today().isoformat()

    # 每日两市总成交额（31 个一级行业合计）
    total_amount = []
    for dt in all_dates:
        s = 0.0
        ok = False
        for code in FIRST_LEVEL:
            v = amount_hist.get(code, {}).get(dt)
            if v is not None:
                s += v
                ok = True
        total_amount.append(round(s, 2) if ok else None)

    def series_of(code):
        return [amount_hist.get(code, {}).get(dt) for dt in all_dates]

    def shares_of(amounts):
        return [round(a / t * 100, 4) if (a is not None and t) else None
                for a, t in zip(amounts, total_amount)]

    industries = []
    # 一级行业
    for code, name in FIRST_LEVEL.items():
        amounts = series_of(code)
        industries.append({
            "key": code, "name": name, "level": "一级",
            "amount": [round(a, 2) if a is not None else None for a in amounts],
            "share": shares_of(amounts),
            "focus": code in FOCUS_ORDER,
        })
    # 重点二级行业
    for code, (name, disp) in SECOND_EXTRA.items():
        amounts = series_of(code)
        industries.append({
            "key": code, "name": disp, "level": "二级",
            "amount": [round(a, 2) if a is not None else None for a in amounts],
            "share": shares_of(amounts),
            "focus": True,
        })
    # 虚拟「大消费」聚合
    cons_amounts = []
    for i in range(len(all_dates)):
        s, ok = 0.0, False
        for c in CONSUME_CODES:
            v = amount_hist.get(c, {}).get(all_dates[i])
            if v is not None:
                s += v
                ok = True
        cons_amounts.append(round(s, 2) if ok else None)
    industries.append({
        "key": "consume", "name": "大消费", "level": "聚合",
        "note": "食品饮料+家电+商贸零售+美容护理+纺织服饰+社会服务",
        "amount": cons_amounts, "share": shares_of(cons_amounts), "focus": True,
    })

    # 按重点顺序 + 占比降序排列
    order = {k: i for i, k in enumerate(FOCUS_ORDER)}
    industries.sort(key=lambda e: (order.get(e["key"], 99),
                                   -(e["share"][-1] or 0) if order.get(e["key"]) is None else 0))

    data = {
        "updated": today,
        "source": "申万行业指数日历史(akshare index_hist_sw)；占比 = 行业成交额 ÷ 31个一级行业成交额合计",
        "note": "二级行业(半导体/软件开发/自动化设备)与虚拟大消费聚合为子集口径，占比相对同一分母可比",
        "dates": all_dates,
        "total_amount": total_amount,
        "focus_order": FOCUS_ORDER,
        "industries": industries,
    }
    write_output(data)
    return data


def write_output(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    for p in (OUTPUT_FILE, FRONTEND_OUTPUT_FILE):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=360, help="回补交易日天数")
    ap.add_argument("--force-full", action="store_true", help="按 backfill 全窗口回补（不保留更长历史）")
    args = ap.parse_args()
    data = collect(backfill_days=args.backfill, force_full=args.force_full)
    n = len(data.get("dates", []))
    nf = sum(1 for e in data.get("industries", []) if e.get("focus"))
    print(f"✓ 行业成交占比已更新：{n} 个交易日，{len(data.get('industries', []))} 个行业（重点 {nf} 个）")


if __name__ == "__main__":
    main()
