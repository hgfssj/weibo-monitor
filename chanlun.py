#!/usr/bin/env python3
"""缠论标注：基于日K线 OHLC 自动识别笔、线段、中枢、一/二/三买卖点。

核心流程：
  1. 包含处理 → 合并被包含的 K 线
  2. 顶底分型 → 识别局部极值点
  3. 笔 → 连接相邻的顶底分型（至少 5 根合并 K 线）
  4. 线段 → 由至少 3 笔构成
  5. 中枢 → 三段连续线段的重叠区间
  6. 买卖点 → 基于背驰 + 中枢位置关系判定

用法：
    python chanlun.py                     # 读取 daily_kline.json 并输出 chanlun.json
    python chanlun.py --symbol sh_index   # 仅计算指定指数
"""

import json
import os
import sys
import time
from collections import namedtuple

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(SCRIPT_DIR, "frontend", "data")
KLINE_PATH = os.path.join(DATA_DIR, "daily_kline.json")
OUTPUT_PATH = os.path.join(DATA_DIR, "chanlun.json")
FRONTEND_OUTPUT = os.path.join(FRONTEND_DATA_DIR, "chanlun.json")

# ---------------------------------------------------------------
#  辅助工具
# ---------------------------------------------------------------

KLine = namedtuple("KLine", ["idx", "date", "o", "h", "l", "c"])
Fractal = namedtuple("Fractal", ["idx", "type", "price"])  # type: "top" / "bottom"
Stroke = namedtuple("Stroke", ["start", "end", "direction"])  # direction: 1=up, -1=down
Segment = namedtuple("Segment", ["start", "end", "direction"])
Pivot = namedtuple("Pivot", ["start_idx", "end_idx", "zg", "zd"])
Signal = namedtuple("Signal", ["idx", "type", "price", "date"])


def ema(series, n):
    """指数移动平均。"""
    k = 2.0 / (n + 1)
    out = [series[0]]
    for v in series[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def macd(closes, fast=12, slow=26, signal=9):
    """返回 (dif, dea, bar)。"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [a - b for a, b in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal)
    bar = [2 * (d - e) for d, e in zip(dif, dea)]
    return dif, dea, bar


# ---------------------------------------------------------------
#  1. 包含处理（K 线合并）
# ---------------------------------------------------------------

def inclusion_merge(klines):
    """
    将存在包含关系的相邻 K 线合并。
    方向向上时取高高、高低；方向向下时取低低、低高。
    返回合并后的 K 线列表。
    """
    if len(klines) < 2:
        return klines[:]

    merged = [klines[0]]
    direction = 0  # 0=未知, 1=向上, -1=向下

    for cur in klines[1:]:
        prev = merged[-1]
        # 判断方向
        if len(merged) >= 2:
            if prev.h > merged[-2].h:
                direction = 1
            elif prev.h < merged[-2].h:
                direction = -1

        # 判断包含关系
        contained = (cur.h <= prev.h and cur.l >= prev.l) or \
                    (cur.h >= prev.h and cur.l <= prev.l)

        if not contained:
            merged.append(cur)
            continue

        # 包含处理
        if direction == 1:
            # 向上：取高高、高低
            new_h = max(prev.h, cur.h)
            new_l = max(prev.l, cur.l)
        elif direction == -1:
            # 向下：取低低、低高
            new_h = min(prev.h, cur.h)
            new_l = min(prev.l, cur.l)
        else:
            # 方向未知：取高低范围
            new_h = max(prev.h, cur.h)
            new_l = min(prev.l, cur.l)
            direction = 1 if cur.h > prev.h else (-1 if cur.h < prev.h else 0)

        merged[-1] = KLine(prev.idx, prev.date, prev.o, new_h, new_l, prev.c)

    return merged


# ---------------------------------------------------------------
#  2. 顶底分型识别
# ---------------------------------------------------------------

def find_fractals(klines_merged):
    """
    在合并后的 K 线中找顶底分型。
    顶分型：中间 K 线最高价 > 左右 K 线最高价
    底分型：中间 K 线最低价 < 左右 K 线最低价
    """
    if len(klines_merged) < 3:
        return []

    fractals = []
    for i in range(1, len(klines_merged) - 1):
        a, b, c = klines_merged[i - 1], klines_merged[i], klines_merged[i + 1]
        if b.h > a.h and b.h > c.h:
            fractals.append(Fractal(b.idx, "top", b.h))
        elif b.l < a.l and b.l < c.l:
            fractals.append(Fractal(b.idx, "bottom", b.l))
    return fractals


# ---------------------------------------------------------------
#  3. 笔识别
# ---------------------------------------------------------------

def find_strokes(klines_merged, fractals, prices):
    """
    连接相邻的顶底分型形成笔。
    要求：相邻顶底之间至少 4 根 K 线（含端点共 5 根），且不能共享 K 线。
    返回 [Stroke] 列表。
    """
    if len(fractals) < 2:
        return []

    # 过滤：相邻的顶底分型之间必须有足够间距
    strokes = []
    prev = fractals[0]
    for cur in fractals[1:]:
        if prev.type == cur.type:
            # 同类型分型：保留更极端的
            if cur.type == "top" and cur.price > prev.price:
                prev = cur
            elif cur.type == "bottom" and cur.price < prev.price:
                prev = cur
            continue

        # 检查间距：相邻顶底间至少 4 根 K 线
        if abs(cur.idx - prev.idx) < 4:
            continue

        # 顶底之间不能有包含关系的 K 线隔断
        # 简化：检查顶的高点是否高于底和底之间的低点
        if prev.type == "bottom":
            # 底→顶：上升笔
            mid_low = min(prices[prev.idx:cur.idx + 1])
            if prev.price > mid_low:
                continue
            strokes.append(Stroke(prev, cur, 1))
        else:
            # 顶→底：下降笔
            mid_high = max(prices[prev.idx:cur.idx + 1])
            if prev.price < mid_high:
                continue
            strokes.append(Stroke(prev, cur, -1))

        prev = cur

    return strokes


# ---------------------------------------------------------------
#  4. 线段识别
# ---------------------------------------------------------------

def find_segments(strokes):
    """
    由至少 3 笔构成线段。
    第一笔的方向决定线段方向。
    简化实现：遍历笔序列，同向笔合并为线段。
    """
    if len(strokes) < 3:
        return []

    segments = []
    start = strokes[0].start
    direction = strokes[0].direction
    prev_end = strokes[0].end

    for i in range(1, len(strokes)):
        s = strokes[i]
        if s.direction == direction:
            # 同向：延伸线段
            prev_end = s.end
        else:
            # 反向：可能结束当前线段
            # 至少需要 3 笔才构成线段
            stroke_count = sum(1 for st in strokes[:i + 1] if st.direction == direction)
            if stroke_count >= 2:
                # 有足够的同向笔，可以结束线段
                segments.append(Segment(start, prev_end, direction))
                start = prev_end
                direction = s.direction
                prev_end = s.end
            else:
                prev_end = s.end

    # 最后一段
    if start != prev_end:
        segments.append(Segment(start, prev_end, direction))

    return segments


# ---------------------------------------------------------------
#  5. 中枢识别
# ---------------------------------------------------------------

def find_pivots(segments, prices):
    """
    三段连续线段的重叠区间构成中枢。
    ZG = 三段区间高点的最小值
    ZD = 三段区间低点的最大值
    若 ZG > ZD，视为有效中枢。
    """
    if len(segments) < 3:
        return []

    pivots = []
    for i in range(len(segments) - 2):
        s1, s2, s3 = segments[i], segments[i + 1], segments[i + 2]

        # 三段的高点
        highs = []
        lows = []
        for s in (s1, s2, s3):
            seg_high = max(prices[s.start.idx:s.end.idx + 1])
            seg_low = min(prices[s.start.idx:s.end.idx + 1])
            highs.append(seg_high)
            lows.append(seg_low)

        zg = min(highs)
        zd = max(lows)

        if zg > zd:
            pivots.append(Pivot(s1.start.idx, s3.end.idx, zg, zd))

    # 合并重叠的中枢
    merged = []
    for p in pivots:
        if not merged:
            merged.append(p)
            continue
        prev = merged[-1]
        # 如果重叠，合并
        if p.start_idx <= prev.end_idx and p.zg > prev.zd and prev.zg > p.zd:
            merged[-1] = Pivot(prev.start_idx, max(p.end_idx, prev.end_idx),
                               min(p.zg, prev.zg), max(p.zd, prev.zd))
        else:
            merged.append(p)

    return merged


# ---------------------------------------------------------------
#  6. 背驰检测（MACD 辅助）
# ---------------------------------------------------------------

def detect_divergence(prices, dif, dea, start_idx, end_idx, direction):
    """
    检测背驰。
    direction=1: 上涨背驰（价格新高，但 DIF 未新高）
    direction=-1: 下跌背驰（价格新低，但 DIF 未新低）
    返回是否有背驰。
    """
    length = end_idx - start_idx
    if length < 10:
        return False

    half = length // 2
    mid = start_idx + half

    if direction == 1:
        # 上涨背驰：后半段价格创新高，但 DIF 未创新高
        price_first = max(prices[start_idx:mid])
        price_second = max(prices[mid:end_idx])
        dif_first = max(dif[start_idx:mid])
        dif_second = max(dif[mid:end_idx])
        return price_second > price_first and dif_second < dif_first
    else:
        # 下跌背驰：后半段价格创新低，但 DIF 未创新低
        price_first = min(prices[start_idx:mid])
        price_second = min(prices[mid:end_idx])
        dif_first = min(dif[start_idx:mid])
        dif_second = min(dif[mid:end_idx])
        return price_second < price_first and dif_second > dif_first


# ---------------------------------------------------------------
#  7. 买卖点判定
# ---------------------------------------------------------------

def find_buy_sell_points(strokes, segments, pivots, prices, dif, dea, closes):
    """
    基于缠论规则判定一/二/三买卖点。
    """
    buy_points = []
    sell_points = []

    if not strokes or len(strokes) < 3:
        return buy_points, sell_points

    # 找到最近的笔的端点作为候选
    for i in range(1, len(strokes) - 1):
        prev_s = strokes[i - 1]
        cur_s = strokes[i]
        next_s = strokes[i + 1]

        # 一买：下降笔结束 + 底分型 + 背驰
        if cur_s.direction == -1 and cur_s.end.type == "bottom":
            start_i = cur_s.start.idx
            end_i = cur_s.end.idx
            if detect_divergence(prices, dif, dea, start_i, end_i, -1):
                pt = cur_s.end
                buy_points.append(Signal(pt.idx, "B1", pt.price, None))

        # 一卖：上升笔结束 + 顶分型 + 背驰
        if cur_s.direction == 1 and cur_s.end.type == "top":
            start_i = cur_s.start.idx
            end_i = cur_s.end.idx
            if detect_divergence(prices, dif, dea, start_i, end_i, 1):
                pt = cur_s.end
                sell_points.append(Signal(pt.idx, "S1", pt.price, None))

    # 二买：一买之后回踩不破前低
    for bp in buy_points:
        for s in strokes:
            if s.start.idx > bp.idx and s.direction == -1 and s.end.type == "bottom":
                if s.end.price > bp.price:
                    buy_points.append(Signal(s.end.idx, "B2", s.end.price, None))
                    break
                break

    # 二卖：一卖之后反弹不破前高
    for sp in sell_points:
        for s in strokes:
            if s.start.idx > sp.idx and s.direction == 1 and s.end.type == "top":
                if s.end.price < sp.price:
                    sell_points.append(Signal(s.end.idx, "S2", s.end.price, None))
                    break
                break

    # 三买：向上突破中枢后回踩不破中枢上沿
    for p in pivots:
        for s in strokes:
            if s.start.idx > p.end_idx and s.direction == -1 and s.end.type == "bottom":
                if s.end.price > p.zg:
                    buy_points.append(Signal(s.end.idx, "B3", s.end.price, None))
                    break
                break

    # 三卖：向下跌破中枢后反弹不破中枢下沿
    for p in pivots:
        for s in strokes:
            if s.start.idx > p.end_idx and s.direction == 1 and s.end.type == "top":
                if s.end.price < p.zd:
                    sell_points.append(Signal(s.end.idx, "S3", s.end.price, None))
                    break
                break

    # 去重（按 idx 去重，保留每组第一个）
    def dedup(signals):
        seen = set()
        out = []
        for s in signals:
            key = (s.idx, s.type)
            if key not in seen:
                seen.add(key)
                out.append(s)
        return out

    return dedup(buy_points), dedup(sell_points)


# ---------------------------------------------------------------
#  主流程：对单个指数计算缠论标注
# ---------------------------------------------------------------

def analyze_chanlun(dates, opens, highs, lows, closes, name=""):
    """
    对一组 OHLC 数据执行完整缠论分析。
    返回 dict：{strokes, pivots, buy_points, sell_points, merged_klines}
    """
    n = len(dates)
    if n < 30:
        return {"strokes": [], "pivots": [], "buy_points": [], "sell_points": []}

    # 构建 K 线列表
    klines = [KLine(i, dates[i], opens[i], highs[i], lows[i], closes[i]) for i in range(n)]

    # 1. 包含处理
    merged = inclusion_merge(klines)

    # 构建价格数组（基于原始索引）
    prices = closes[:]

    # 2. 顶底分型
    fractals = find_fractals(merged)

    # 3. 笔
    strokes = find_strokes(merged, fractals, prices)

    # 4. 线段
    segments = find_segments(strokes)

    # 5. 中枢
    pivots = find_pivots(segments, prices)

    # 6. MACD
    dif, dea, bar = macd(closes)

    # 7. 买卖点
    buy_pts, sell_pts = find_buy_sell_points(strokes, segments, pivots, prices, dif, dea, closes)

    # 格式化输出
    def fmt_stroke(s):
        return {"start_idx": s.start.idx, "end_idx": s.end.idx,
                "start_price": s.start.price, "end_price": s.end.price,
                "start_type": s.start.type, "end_type": s.end.type,
                "direction": s.direction}

    def fmt_pivot(p):
        return {"start_idx": p.start_idx, "end_idx": p.end_idx,
                "zg": round(p.zg, 2), "zd": round(p.zd, 2)}

    def fmt_signal(s):
        return {"idx": s.idx, "type": s.type, "price": s.price,
                "date": dates[s.idx] if s.idx < len(dates) else ""}

    return {
        "strokes": [fmt_stroke(s) for s in strokes],
        "pivots": [fmt_pivot(p) for p in pivots],
        "buy_points": [fmt_signal(b) for b in buy_pts],
        "sell_points": [fmt_signal(s) for s in sell_pts],
        "merged_count": len(merged),
    }


# ---------------------------------------------------------------
#  批量采集
# ---------------------------------------------------------------

def collect(target_symbol=None):
    """读取 daily_kline.json，对每个品种计算缠论标注，输出 chanlun.json。"""
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)

    with open(KLINE_PATH, "r", encoding="utf-8") as f:
        kline_data = json.load(f)

    results = {}

    for series in kline_data.get("index_kline", []) + kline_data.get("industry_kline", []):
        key = series.get("key", "")
        if target_symbol and key != target_symbol:
            continue
        name = series.get("name", key)
        dates = series.get("dates", [])
        opens = series.get("open", [])
        highs = series.get("high", [])
        lows = series.get("low", [])
        closes = series.get("close", [])

        if len(dates) < 30:
            results[key] = {"strokes": [], "pivots": [], "buy_points": [], "sell_points": []}
            continue

        try:
            result = analyze_chanlun(dates, opens, highs, lows, closes, name)
            result["name"] = name
            results[key] = result
            b = len(result["buy_points"])
            s = len(result["sell_points"])
            print(f"  📐 {name}({key}): {len(result['strokes'])}笔 {len(result['pivots'])}中枢 B×{b} S×{s}")
        except Exception as e:
            print(f"  ⚠️ {name}({key}) 计算失败: {e}")
            results[key] = {"strokes": [], "pivots": [], "buy_points": [], "sell_points": [], "error": str(e)[:80]}

    output = {
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "annotations": results,
    }

    for p in (OUTPUT_PATH, FRONTEND_OUTPUT):
        with open(p, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False)

    print(f"\n✅ 缠论标注已写入 {OUTPUT_PATH}")
    return output


if __name__ == "__main__":
    target = None
    for arg in sys.argv[1:]:
        if arg.startswith("--symbol="):
            target = arg.split("=", 1)[1]
    collect(target_symbol=target)