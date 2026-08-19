# -*- coding: utf-8 -*-
"""验证假设：证券指数在周五/节假日前最后交易日是否更易下跌且跌幅更大"""
import akshare as ak
import pandas as pd
import numpy as np

# ---------- 数据 ----------
df = ak.stock_zh_index_daily(symbol="sz399975")  # 证券公司指数
df["date"] = pd.to_datetime(df["date"])
df = df.sort_values("date").reset_index(drop=True)
df["pct"] = df["close"].pct_change() * 100

cal = ak.tool_trade_date_hist_sina()
tds = sorted(pd.to_datetime(cal["trade_date"]).tolist())
next_td = {tds[i]: tds[i + 1] for i in range(len(tds) - 1)}

# 对照：沪深300
df300 = ak.stock_zh_index_daily(symbol="sh000300")
df300["date"] = pd.to_datetime(df300["date"])
df300 = df300.sort_values("date").reset_index(drop=True)
df300["pct"] = df300["close"].pct_change() * 100

# ---------- 分组 ----------
def group_of(d):
    if d not in next_td:
        return None
    gap = (next_td[d] - d).days
    if gap > 3:  # 距下一交易日超过正常周末 → 节假日前最后交易日
        return "pre_holiday"
    if d.dayofweek == 4:
        return "friday"
    return "other"

def build(frame):
    frame = frame.dropna(subset=["pct"]).copy()
    frame["grp"] = frame["date"].map(group_of)
    return frame.dropna(subset=["grp"])

sec = build(df)
hs = build(df300)

# 统计周期：仅今年
START = "2026-01-01"
sec = sec[sec["date"] >= START]
hs = hs[hs["date"] >= START]

# ---------- 统计 ----------
LABEL = {"friday": "普通周五", "pre_holiday": "节假日前最后交易日", "other": "其他交易日"}

def stats(frame, tag):
    print(f"\n===== {tag}  样本 {frame['date'].min().date()} ~ {frame['date'].max().date()} =====")
    print(f"{'分组':<12}{'天数':>6}{'下跌概率':>9}{'平均涨跌':>9}{'中位数':>8}{'跌日均跌幅':>11}{'跌>1%':>8}{'跌>2%':>8}")
    for g in ["friday", "pre_holiday", "other"]:
        s = frame.loc[frame["grp"] == g, "pct"]
        n = len(s)
        down = (s < 0).mean() * 100
        mean, med = s.mean(), s.median()
        dmean = s[s < 0].mean() if (s < 0).any() else 0
        b1 = (s < -1).mean() * 100
        b2 = (s < -2).mean() * 100
        print(f"{LABEL[g]:<12}{n:>6}{down:>8.1f}%{mean:>8.2f}%{med:>8.2f}%{dmean:>10.2f}%{b1:>7.1f}%{b2:>7.1f}%")

stats(sec, "证券公司指数 sz399975")
stats(hs, "沪深300（对照） sh000300")

# ---------- 证券指数：合并「周五+节前」 vs 其他 ----------
print("\n===== 证券指数：周五∪节前 vs 其他 =====")
a = sec.loc[sec["grp"] != "other", "pct"]
b = sec.loc[sec["grp"] == "other", "pct"]
print(f"周五/节前: n={len(a)}, 下跌概率={(a<0).mean()*100:.1f}%, 均值={a.mean():.3f}%, 跌>2%概率={(a<-2).mean()*100:.1f}%")
print(f"其他日    : n={len(b)}, 下跌概率={(b<0).mean()*100:.1f}%, 均值={b.mean():.3f}%, 跌>2%概率={(b<-2).mean()*100:.1f}%")

# 简单显著性：两比例 z 检验（下跌概率）
p1, n1 = (a < 0).mean(), len(a)
p2, n2 = (b < 0).mean(), len(b)
p = (p1 * n1 + p2 * n2) / (n1 + n2)
z = (p1 - p2) / np.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
print(f"下跌概率差异 z 值 = {z:.2f}  (|z|>1.96 即 5% 显著)")

# ---------- 分年度稳健性（证券指数，周五∪节前 下跌概率 - 其他） ----------
sec["year"] = sec["date"].dt.year
print("\n===== 分年度：周五/节前下跌概率 vs 其他日 =====")
print(f"{'年份':<6}{'周五/节前n':>10}{'下跌%':>7}{'其他n':>7}{'下跌%':>7}{'差值':>7}")
for y, g in sec.groupby("year"):
    if y < 2010 or len(g) < 100:
        continue
    ga, gb = g[g["grp"] != "other"]["pct"], g[g["grp"] == "other"]["pct"]
    if len(ga) < 10:
        continue
    da, db = (ga < 0).mean() * 100, (gb < 0).mean() * 100
    print(f"{y:<6}{len(ga):>10}{da:>6.1f}%{len(gb):>7}{db:>6.1f}%{da-db:>6.1f}")

# ---------- 节前细分 ----------
pre = sec[sec["grp"] == "pre_holiday"]
print(f"\n节前最后交易日自身: 平均 {pre['pct'].mean():.2f}%, 下跌概率 {(pre['pct']<0).mean()*100:.1f}%")

# ---------- 跨指数对比：周五∪节前 vs 其他 ----------
dfcy = ak.stock_zh_index_daily(symbol="sz399006")  # 创业板指
dfcy["date"] = pd.to_datetime(dfcy["date"])
dfcy = dfcy.sort_values("date").reset_index(drop=True)
dfcy["pct"] = dfcy["close"].pct_change() * 100
cy = build(dfcy)
cy = cy[cy["date"] >= START]

print("\n===== 2026 跨指数对比：周五/节前 vs 其他日 =====")
print(f"{'指数':<10}|{'周五 n/下跌%/均值':<22}|{'节前 n/下跌%/均值':<22}|{'周五∪节前 n/下跌%/均值/跌>2%':<32}|{'其他 n/下跌%/均值':<22}")
for tag, fr in [("证券", sec), ("沪深300", hs), ("创业板", cy)]:
    f = fr[fr["grp"] == "friday"]["pct"]
    p = fr[fr["grp"] == "pre_holiday"]["pct"]
    a = fr[fr["grp"] != "other"]["pct"]
    b = fr[fr["grp"] == "other"]["pct"]
    print(f"{tag:<10}|{len(f):>3}/{(f<0).mean()*100:>5.1f}%/{f.mean():>6.2f}%   |{len(p):>3}/{(p<0).mean()*100:>5.1f}%/{p.mean():>6.2f}%   |"
          f"{len(a):>3}/{(a<0).mean()*100:>5.1f}%/{a.mean():>6.2f}%/{(a<-2).mean()*100:>5.1f}%        |"
          f"{len(b):>4}/{(b<0).mean()*100:>5.1f}%/{b.mean():>6.2f}%")
    # 差值
    print(f"{'':<10} 周五-其他: 下跌概率 {(f<0).mean()*100-(b<0).mean()*100:+.1f}pp, 均值 {f.mean()-b.mean():+.2f}pp;  "
          f"周五∪节前-其他: {(a<0).mean()*100-(b<0).mean()*100:+.1f}pp, 均值 {a.mean()-b.mean():+.2f}pp")
