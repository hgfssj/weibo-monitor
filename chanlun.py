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
Pivot = namedtuple("Pivot", ["start_idx", "end_idx", "zg", "zd", "level"])  # level: stroke/segment
Signal = namedtuple("Signal", ["idx", "type", "price", "date", "div"])  # div: 背驰连线信息或None


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
#  4. 线段识别（特征序列分型法）
# ---------------------------------------------------------------

def _stroke_range(u):
    """笔/线段作为特征序列元素的高低价。"""
    return max(u.start.price, u.end.price), min(u.start.price, u.end.price)


def _merge_chars(chars, direction):
    """特征序列包含处理：反向笔视作 K 线，向上取高高高低、向下取低低低高。"""
    merged = []
    for c in chars:
        h, l = _stroke_range(c)
        if merged:
            ph, pl, _ = merged[-1]
            if (h <= ph and l >= pl) or (h >= ph and l <= pl):
                if direction == 1:
                    merged[-1] = (max(ph, h), max(pl, l), merged[-1][2])
                else:
                    merged[-1] = (min(ph, h), min(pl, l), merged[-1][2])
                continue
        merged.append((h, l, c))
    return merged


def find_segments(strokes):
    """
    线段识别（标准特征序列分型法）：
    - 向上线段：特征序列 = 段内向下笔；出现顶分型 → 线段终结于中元素起点
    - 向下线段：对称（特征序列 = 向上笔，底分型终结）
    线段至少 3 笔；最后未终结段取极值点收尾。
    """
    if len(strokes) < 3:
        return []

    segments = []
    seg_start = strokes[0].start
    seg_dir = strokes[0].direction
    cur = [strokes[0]]
    i = 1
    while i < len(strokes):
        cur.append(strokes[i])
        i += 1
        chars = [st for st in cur if st.direction == -seg_dir]
        if len(chars) < 3:
            continue
        m = _merge_chars(chars, seg_dir)
        broken_end, mid_stroke = None, None
        for j in range(1, len(m) - 1):
            h1, l1, _ = m[j - 1]
            h2, l2, mid = m[j]
            h3, l3, _ = m[j + 1]
            if seg_dir == 1 and h2 > h1 and h2 > h3:
                broken_end, mid_stroke = mid.start, mid
                break
            if seg_dir == -1 and l2 < l1 and l2 < l3:
                broken_end, mid_stroke = mid.start, mid
                break
        if broken_end is not None:
            segments.append(Segment(seg_start, broken_end, seg_dir))
            seg_start = broken_end
            seg_dir = -seg_dir
            cur = cur[cur.index(mid_stroke):]

    # 最后未终结段：取同向笔极值点收尾
    same = [st for st in cur if st.direction == seg_dir]
    if same:
        end_st = max(same, key=lambda st: st.end.price) if seg_dir == 1 \
            else min(same, key=lambda st: st.end.price)
        if end_st.end != seg_start:
            segments.append(Segment(seg_start, end_st.end, seg_dir))
    return segments


# ---------------------------------------------------------------
#  5. 中枢识别（笔级 + 线段级，含延伸）
# ---------------------------------------------------------------

def _build_pivots(units, level):
    """三段连续单元（笔或线段）重叠区间构成中枢，后续重叠单元纳入延伸。"""
    pivots = []
    i, n = 0, len(units)
    while i < n - 2:
        s1, s2, s3 = units[i], units[i + 1], units[i + 2]
        highs = [max(u.start.price, u.end.price) for u in (s1, s2, s3)]
        lows = [min(u.start.price, u.end.price) for u in (s1, s2, s3)]
        zg, zd = min(highs), max(lows)
        if zg > zd:
            end_idx = s3.end.idx
            j = i + 3
            while j < n:  # 延伸
                uh, ul = _stroke_range(units[j])
                if ul < zg and uh > zd:
                    end_idx = units[j].end.idx
                    j += 1
                else:
                    break
            pivots.append(Pivot(s1.start.idx, end_idx, zg, zd, level))
            i = j
        else:
            i += 1
    return pivots


# ---------------------------------------------------------------
#  6. 背驰检测（MACD 面积法：同向两笔/两线段对比）
# ---------------------------------------------------------------

def _macd_area(bar, i1, i2):
    return sum(abs(v) for v in bar[max(0, i1):i2 + 1])


def _unit_divergence(units, i, bar, dif):
    """units[i] 相对前一同向单元 units[i-2] 是否背驰。
    底背驰：价格新低 + MACD 柱面积缩小或 DIF 不新低；顶背驰对称。
    返回 (是否背驰, 前一单元)。"""
    if i < 2:
        return False, None
    cur, prev = units[i], units[i - 2]
    if cur.direction != prev.direction:
        return False, None
    ca = _macd_area(bar, cur.start.idx, cur.end.idx)
    pa = _macd_area(bar, prev.start.idx, prev.end.idx)
    if cur.direction == -1:
        if cur.end.price >= prev.end.price:
            return False, None
        dif_shallow = min(dif[cur.start.idx:cur.end.idx + 1]) > min(dif[prev.start.idx:prev.end.idx + 1])
        return (ca < pa or dif_shallow), prev
    if cur.end.price <= prev.end.price:
        return False, None
    dif_shallow = max(dif[cur.start.idx:cur.end.idx + 1]) < max(dif[prev.start.idx:prev.end.idx + 1])
    return (ca < pa or dif_shallow), prev


# ---------------------------------------------------------------
#  7. 买卖点判定
# ---------------------------------------------------------------

def find_buy_sell_points(strokes, segments, pivots, bar, dif):
    """
    完整三类买卖点判定：
    - 一买/一卖：笔级或线段级背驰 + 之前存在中枢（趋势背驰）
    - 二买/二卖：一买/一卖后首次回踩不破前低/前高
    - 三买/三卖：突破中枢后回抽不回中枢区间
    """
    buys, sells = [], []
    pivots_stroke = [p for p in pivots if p.level == "stroke"]

    def has_pivot_before(idx):
        return any(p.start_idx < idx for p in pivots_stroke)

    def div_info(prev, cur):
        return {"a_idx": prev.end.idx, "a_price": prev.end.price,
                "b_idx": cur.end.idx, "b_price": cur.end.price}

    # 一买/一卖：笔级背驰
    for i in range(2, len(strokes)):
        ok, prev = _unit_divergence(strokes, i, bar, dif)
        if not ok:
            continue
        cur = strokes[i]
        if not has_pivot_before(cur.start.idx):
            continue
        if cur.direction == -1:
            buys.append(Signal(cur.end.idx, "B1", cur.end.price, None, div_info(prev, cur)))
        else:
            sells.append(Signal(cur.end.idx, "S1", cur.end.price, None, div_info(prev, cur)))

    # 一买/一卖：线段级背驰（级别更高，优先展示）
    for i in range(2, len(segments)):
        ok, prev = _unit_divergence(segments, i, bar, dif)
        if not ok:
            continue
        cur = segments[i]
        if cur.direction == -1:
            buys.append(Signal(cur.end.idx, "B1", cur.end.price, None,
                               dict(div_info(prev, cur), level="segment")))
        else:
            sells.append(Signal(cur.end.idx, "S1", cur.end.price, None,
                                dict(div_info(prev, cur), level="segment")))

    # 二买：一买之后首个下降笔终点不破前低
    for sig in [s for s in buys if s.type == "B1"]:
        for s in strokes:
            if s.end.idx <= sig.idx:
                continue
            if s.direction == -1:
                if s.end.price > sig.price:
                    buys.append(Signal(s.end.idx, "B2", s.end.price, None, None))
                break

    # 二卖：一卖之后首个上升笔终点不破前高
    for sig in [s for s in sells if s.type == "S1"]:
        for s in strokes:
            if s.end.idx <= sig.idx:
                continue
            if s.direction == 1:
                if s.end.price < sig.price:
                    sells.append(Signal(s.end.idx, "S2", s.end.price, None, None))
                break

    # 三买：向上突破中枢 ZG 后，首个回踩笔低点不回落入中枢
    for p in pivots_stroke:
        broke = False
        for s in strokes:
            if s.end.idx <= p.end_idx:
                continue
            if not broke:
                if s.direction == 1 and s.end.price > p.zg:
                    broke = True
            elif s.direction == -1:
                if s.end.price > p.zg:
                    buys.append(Signal(s.end.idx, "B3", s.end.price, None, None))
                break

    # 三卖：向下跌破中枢 ZD 后，首个反弹笔高点不回升入中枢
    for p in pivots_stroke:
        broke = False
        for s in strokes:
            if s.end.idx <= p.end_idx:
                continue
            if not broke:
                if s.direction == -1 and s.end.price < p.zd:
                    broke = True
            elif s.direction == 1:
                if s.end.price < p.zd:
                    sells.append(Signal(s.end.idx, "S3", s.end.price, None, None))
                break

    # 去重（同 idx+type 保留首个；线段级一买优先于笔级）
    def dedup(signals):
        signals = sorted(signals, key=lambda s: (s.idx, 0 if (s.div or {}).get("level") == "segment" else 1))
        seen = set()
        out = []
        for s in signals:
            key = (s.idx, s.type)
            if key not in seen:
                seen.add(key)
                out.append(s)
        return sorted(out, key=lambda s: s.idx)

    return dedup(buys), dedup(sells)


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
        return {"strokes": [], "segments": [], "pivots": [], "buy_points": [], "sell_points": []}

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

    # 4. 线段（特征序列分型法）
    segments = find_segments(strokes)

    # 5. 中枢（笔级 + 线段级）
    pivots = _build_pivots(strokes, "stroke") + _build_pivots(segments, "segment")

    # 6. MACD
    dif, dea, bar = macd(closes)

    # 7. 买卖点
    buy_pts, sell_pts = find_buy_sell_points(strokes, segments, pivots, bar, dif)

    # 格式化输出
    def fmt_unit(s):
        return {"start_idx": s.start.idx, "end_idx": s.end.idx,
                "start_price": s.start.price, "end_price": s.end.price,
                "direction": s.direction}

    def fmt_pivot(p):
        return {"start_idx": p.start_idx, "end_idx": p.end_idx,
                "zg": round(p.zg, 2), "zd": round(p.zd, 2), "level": p.level}

    def fmt_signal(sg):
        return {"idx": sg.idx, "type": sg.type, "price": sg.price,
                "date": dates[sg.idx] if sg.idx < len(dates) else "",
                "div": sg.div}

    return {
        "strokes": [fmt_unit(s) for s in strokes],
        "segments": [fmt_unit(s) for s in segments],
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
            results[key] = {"strokes": [], "segments": [], "pivots": [], "buy_points": [], "sell_points": []}
            continue

        try:
            result = analyze_chanlun(dates, opens, highs, lows, closes, name)
            result["name"] = name
            results[key] = result
            b = len(result["buy_points"])
            s = len(result["sell_points"])
            print(f"  📐 {name}({key}): {len(result['strokes'])}笔 {len(result['segments'])}线段 "
                  f"{len(result['pivots'])}中枢 B×{b} S×{s}")
        except Exception as e:
            print(f"  ⚠️ {name}({key}) 计算失败: {e}")
            results[key] = {"strokes": [], "segments": [], "pivots": [], "buy_points": [], "sell_points": [], "error": str(e)[:80]}

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