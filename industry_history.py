#!/usr/bin/env python3
"""行业讨论历史数据库 — 长期跟踪引擎。

解决「行业监控只有 7 天滚动窗口、看不到长期趋势」的问题：
  1. 按天聚合历史：帖数 / 互动量 / 多空情绪分 / 多空帖数比 / 热门关键词，
     每轮采集后幂等更新当日（并回填窗口内所有日期），保留 360 天。
  2. 高质量作者沉淀库：跨轮累计各作者发帖数、互动量，计算质量分，
     形成每个行业的「雪球高质量作者 Top 榜」。
  3. 每周行业讨论摘要：讨论焦点 / 多空论点 / 事件驱动 / 与上周变化，
     LLM 可用时自动生成，否则启发式兜底（LLM 优先级同 weibo_summary）。

输出：
  data/industry_history.json（+ frontend/data/ 副本），结构：
  {
    "updated_at": "...",
    "industries": {
      "<iid>": {
        "name": "...", "icon": "...", "sw_index": "801890",
        "days": {"2026-08-20": {"posts","interactions","sentiment",
                                 "bull","bear","neutral","keywords":{...}}, ...},
        "authors": {"<uid>": {"name","posts","likes","comments",
                              "reposts","quality","last_active"}, ...},
        "weekly_summaries": {"2026-W34": {"focus","bull_case","bear_case",
                                           "events","change","sig",...}}, ...
      }
    }
  }

用法：
    python industry_history.py --update     # 用现有 industry_data.json 更新历史
    python industry_history.py --weekly     # 生成/刷新本周摘要
"""
import json
import math
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")
HISTORY_FILE = os.path.join(DATA_DIR, "industry_history.json")
FRONTEND_HISTORY = os.path.join(FRONTEND_DATA_DIR, "industry_history.json")
INDUSTRY_DATA_FILE = os.path.join(DATA_DIR, "industry_data.json")

KEEP_DAYS = 360          # 历史保留天数
KEEP_TOP_AUTHORS = 50    # 每个行业沉淀的作者上限

# 行业方向 → 申万一级行业指数（用于前端与行业指数日K叠加）
SW_INDEX_MAP = {
    "humanoid_robot": "801890",   # 机械设备
    "ai_application": "801750",    # 计算机
    "ai_pharma": "801150",         # 医药生物
    "lithium_battery": "801730",   # 电力设备
    "ai_hardware": "801080",       # 电子
    "model_cloud": "801770",       # 通信
}

# ============================ 情绪词典 ============================
# 股市中文多空词典：命中看多词 +1，看空词 -1；帖子分 = 多空命中差归一化
BULL_WORDS = [
    "看多", "买入", "加仓", "满仓", "重仓", "抄底", "突破", "新高", "启动", "爆发",
    "上涨", "涨停", "大涨", "强势", "龙头", "机会", "利好", "牛市", "起飞", "增持",
    "翻倍", "低估", "底部", "反转", "放量", "拉升", "走强", "看好", "主升", "升势",
    "突破前高", "量价齐升", "站上", "回踩确认", "逢低", "布局", "建仓", "埋伏",
    "景气", "超预期", "订单饱满", "供不应求", "涨价", "提价", "扩产", "业绩爆表",
    "戴维斯双击", "高景气", "趋势向上", "新高不断", "资金流入", "主力进场", "抢筹",
]
BEAR_WORDS = [
    "看空", "卖出", "减仓", "清仓", "割肉", "止损", "见顶", "回落", "下跌", "跌停",
    "大跌", "破位", "风险", "利空", "熊市", "崩盘", "出货", "高估", "泡沫", "套牢",
    "踩雷", "爆仓", "走弱", "回调", "逃命", "离场", "观望", "泡沫化", "透支",
    "利好出尽", "不及预期", "业绩暴雷", "减持", "质押", "补跌", "阴跌", "缩量",
    "资金流出", "主力撤退", "高位站岗", "杀估值", "戴维斯双杀", "量价背离", "滞涨",
    "产能过剩", "价格战", "内卷", "需求疲软", "库存高企",
]

# 关键词提取：停用词 + 单字过滤
STOP_WORDS = set("""的 了 和 是 就 都 而 及 与 跟 或 一个 我们 你们 他们 这个 那个
这些 那些 什么 怎么 为什么 可以 会 要 不 没 有 在 为 对 从 被 把 让 使
现在 目前 今天 昨天 明天 今年 去年 时候 起来 下来 出来 上去 过去 以后 之前
就是 还是 但是 因为 所以 如果 虽然 然后 于是 不过 其实 真的 感觉 应该 可能
公司 股票 股价 市场行情 大盘 A股 老铁 兄弟 大家 分享 觉得 个人 认为 一直
已经 还有 只有 还有 只是 不是 没有 这种 这样 那样 一样 还是 而且 并且 以及
第一 第二 上个 下个 一些 有点 比较 非常 特别 十分 更加 最 很 太 挺 蛮 略
""".split())


def score_text(text: str):
    """对单条文本打情绪分。返回 (score, bull_hits, bear_hits)。

    score ∈ [-1, 1]：多空命中差 / 总命中（无命中返回 None 交由上层处理）。
    """
    if not text:
        return None, 0, 0
    bull = sum(1 for w in BULL_WORDS if w in text)
    bear = sum(1 for w in BEAR_WORDS if w in text)
    total = bull + bear
    if total == 0:
        return None, 0, 0
    return (bull - bear) / total, bull, bear


def extract_keywords(texts: list, top_n: int = 30) -> dict:
    """jieba 分词统计高频关键词（名词性 2 字以上，去停用词）。"""
    counter = Counter()
    try:
        import jieba
        jieba.setLogLevel(60)  # 关闭初始化日志
        for t in texts:
            if not t:
                continue
            for w in jieba.lcut(re.sub(r"#[^#]{1,30}#|\$[^$]{1,20}\$|https?://\S+|[\s\n]+", " ", t)):
                w = w.strip()
                if len(w) < 2 or w in STOP_WORDS or w.isdigit():
                    continue
                if re.fullmatch(r"[^\u4e00-\u9fa5a-zA-Z]+", w):
                    continue
                counter[w] += 1
    except ImportError:
        # 无 jieba 时退化为 2-gram 统计（同样过滤停用词）
        for t in texts:
            if not t:
                continue
            clean = re.sub(r"#[^#]{1,30}#|\$[^$]{1,20}\$|https?://\S+", " ", t)
            han = re.findall(r"[\u4e00-\u9fa5]{2,6}", clean)
            for seg in han:
                for i in range(len(seg) - 1):
                    g = seg[i:i + 2]
                    if g in STOP_WORDS:
                        continue
                    counter[g] += 1
    return dict(counter.most_common(top_n))


# ============================ 历史聚合 ============================

def _interaction(p: dict) -> int:
    try:
        return int(p.get("likes") or 0) + int(p.get("comments") or 0) + int(p.get("reposts") or 0)
    except (TypeError, ValueError):
        return 0


def _date_of(p: dict) -> str:
    return str(p.get("created_at") or "")[:10]


def update_history(industry_data: dict, cfg: dict = None) -> dict:
    """主入口：用一轮 industry_data.json 的内容幂等更新历史库。

    - 窗口内每个日期重新聚合（当日数据随轮次增长，重算保证准确）
    - 作者库增量合并（posts/likes 累计，quality 重算）
    - LLM 周摘要不在此函数（见 generate_weekly_summaries）
    """
    hist = _load_history()
    inds_in = (industry_data or {}).get("industries", {})
    cfg_inds = {i.get("id") or i.get("name"): i for i in (cfg or {}).get("industries", [])}

    for iid, ind in inds_in.items():
        h = hist["industries"].setdefault(iid, {
            "name": ind.get("name", iid),
            "icon": ind.get("icon", "🏭"),
            "days": {}, "authors": {}, "weekly_summaries": {},
        })
        h["name"] = ind.get("name", h.get("name", iid))
        h["icon"] = ind.get("icon", h.get("icon", "🏭"))
        h["sw_index"] = SW_INDEX_MAP.get(iid, h.get("sw_index", ""))

        posts = ind.get("viewpoints") or ind.get("all_posts") or []
        # 1. 按天聚合（重算涉及到的日期，其他日期保留）。
        #    单调合并：浅层采集（日常监控每标的仅约20帖）不覆盖深层回填
        #    （每标的可达500帖）已写入的更全观测。
        by_date = {}
        for p in posts:
            d = _date_of(p)
            if d:
                by_date.setdefault(d, []).append(p)
        for d, plist in by_date.items():
            texts, bull_n, bear_n, neut_n, senti_sum, senti_n = [], 0, 0, 0, 0.0, 0
            inter = 0
            for p in plist:
                inter += _interaction(p)
                texts.append(p.get("text") or "")
                sc, b, r = score_text(p.get("text") or "")
                if sc is None:
                    neut_n += 1
                else:
                    senti_sum += sc
                    senti_n += 1
                    if sc > 0:
                        bull_n += 1
                    elif sc < 0:
                        bear_n += 1
                    else:
                        neut_n += 1
            sentiment = round(senti_sum / senti_n * 100) if senti_n else None
            old = h["days"].get(d)
            if old and old.get("posts", 0) >= len(plist):
                continue  # 已有更全观测，保留
            h["days"][d] = {
                "posts": len(plist),
                "interactions": inter,
                "sentiment": sentiment,
                "bull": bull_n, "bear": bear_n, "neutral": neut_n,
                "keywords": extract_keywords(texts, top_n=20),
            }

        # 2. 作者沉淀（按帖子 id 去重后增量合并，避免跨轮重复计数）
        seen_ids = h.get("seen_pids") or []
        seen = set(seen_ids)
        for p in posts:
            pid = str(p.get("id") or "")
            if pid and pid in seen:
                continue  # 已统计过的帖子
            if pid:
                seen.add(pid)
                seen_ids.append(pid)
            uid = str(p.get("uid") or p.get("author") or "")
            if not uid or uid == "None":
                continue
            a = h["authors"].setdefault(uid, {
                "name": p.get("author") or uid, "posts": 0, "likes": 0,
                "comments": 0, "reposts": 0, "quality": 0,
                "last_active": p.get("created_at") or "",
            })
            a["posts"] += 1
            try:
                a["likes"] += int(p.get("likes") or 0)
                a["comments"] += int(p.get("comments") or 0)
                a["reposts"] += int(p.get("reposts") or 0)
            except (TypeError, ValueError):
                pass
            if (p.get("created_at") or "") > (a.get("last_active") or ""):
                a["last_active"] = p.get("created_at") or ""
        # 质量分：log10(总互动+1) * min(发帖数,10) 权重，作者保留 Top N
        for uid, a in h["authors"].items():
            total_inter = a.get("likes", 0) + a.get("comments", 0) + a.get("reposts", 0)
            a["quality"] = round(math.log10(total_inter + 1) * min(a.get("posts", 1), 10), 1)
        top_uids = sorted(h["authors"].items(),
                          key=lambda kv: kv[1].get("quality", 0), reverse=True)[:KEEP_TOP_AUTHORS]
        h["authors"] = dict(top_uids)
        # 已统计帖子 id 归档（封顶保最近 5000 条，防止内存/文件膨胀）
        h["seen_pids"] = seen_ids[-5000:]

        # 3. 裁剪历史
        cutoff = (datetime.now() - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        h["days"] = {d: v for d, v in sorted(h["days"].items()) if d >= cutoff}
        if not h["sw_index"]:
            # 尝试从配置 stocks 推断（不强求）
            pass

    hist["updated_at"] = datetime.now().isoformat()
    _save_history(hist)
    n_days = sum(len(v.get("days", {})) for v in hist["industries"].values())
    n_authors = sum(len(v.get("authors", {})) for v in hist["industries"].values())
    print(f"  📚 行业历史库已更新: {len(hist['industries'])} 个方向 · "
          f"累计 {n_days} 天记录 · 沉淀 {n_authors} 位作者")
    return hist


# ============================ 周摘要（LLM 优先 / 启发式兜底） ============================

try:
    from weibo_summary import LLM_AVAILABLE, _llm_chat
except Exception:
    LLM_AVAILABLE = False
    _llm_chat = None


def _iso_week(d: datetime):
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def _week_range(week_str: str):
    """'2026-W34' → (起始日, 结束日) date 对象。"""
    year, wk = week_str.split("-W")
    monday = datetime.strptime(f"{year}-W{int(wk):02d}-1", "%G-W%V-%u").date()
    return monday, monday + timedelta(days=6)


def _week_posts(hist_ind: dict, week_str: str) -> list:
    """从历史库提取某周内的帖子无法实现（历史库只存聚合），此处返回该周日聚合。"""
    start, end = _week_range(week_str)
    return {d: v for d, v in hist_ind.get("days", {}).items()
            if start.strftime("%Y-%m-%d") <= d <= end.strftime("%Y-%m-%d")}


def _heuristic_weekly(ind_name: str, week_days: dict, prev_week_days: dict) -> dict:
    """启发式周摘要：基于聚合指标 + 热词 + 环比变化。"""
    posts = sum(v.get("posts", 0) for v in week_days.values())
    inter = sum(v.get("interactions", 0) for v in week_days.values())
    sentiments = [v["sentiment"] for v in week_days.values() if v.get("sentiment") is not None]
    senti_avg = round(sum(sentiments) / len(sentiments)) if sentiments else None
    bull = sum(v.get("bull", 0) for v in week_days.values())
    bear = sum(v.get("bear", 0) for v in week_days.values())
    kw = Counter()
    for v in week_days.values():
        for w, c in (v.get("keywords") or {}).items():
            kw[w] += c
    top_kw = [w for w, _ in kw.most_common(10)]
    prev_posts = sum(v.get("posts", 0) for v in prev_week_days.values())
    prev_senti = [v["sentiment"] for v in prev_week_days.values() if v.get("sentiment") is not None]
    prev_avg = round(sum(prev_senti) / len(prev_senti)) if prev_senti else None

    if posts == 0:
        return {"focus": "本周无讨论记录", "bull_case": "—", "bear_case": "—",
                "events": "—", "change": "—", "source": "heuristic"}

    focus = (f"本周《{ind_name}》雪球讨论共 {posts} 帖 / 互动 {inter} 次，"
             f"热门关键词：{('、'.join(top_kw[:8])) or '—'}。")
    if senti_avg is None:
        stance = "无明显多空倾向"
    elif senti_avg >= 20:
        stance = "讨论情绪明显偏多"
    elif senti_avg > -20:
        stance = "讨论情绪中性略偏多" if senti_avg >= 0 else "讨论情绪中性略偏空"
    else:
        stance = "讨论情绪明显偏空"
    bull_case = (f"本周看多表述帖子 {bull} 条"
                 + (f"，情绪均值 {senti_avg:+d}" if senti_avg is not None else "")
                 + f"，{stance}。")
    bear_case = f"本周看空表述帖子 {bear} 条" + (f"（多空比 {bull}:{bear}）。" if bear else "。")
    events = f"高频事件词：{('、'.join(top_kw[:5])) or '无显著事件线索'}（启发式词频，建议结合帖子明细人工确认）。"
    if prev_posts:
        pct = (posts - prev_posts) / prev_posts * 100
        chg = f"讨论量环比上周 {pct:+.0f}%（{prev_posts} → {posts} 帖）"
        if prev_avg is not None and senti_avg is not None:
            chg += f"，情绪分 {prev_avg:+d} → {senti_avg:+d}"
        change = chg + "。"
    else:
        change = "上周无记录（历史积累初期），环比不可用。"
    return {"focus": focus, "bull_case": bull_case, "bear_case": bear_case,
            "events": events, "change": change, "source": "heuristic"}


WEEKLY_PROMPT = """你是一名资深行业研究员。以下是雪球社区某行业方向本周的讨论数据聚合（按天：帖数、互动量、多空情绪分、高频关键词），以及该行业代表性股票的讨论摘要。请生成本周讨论摘要，严格输出 JSON（不要 markdown 代码块）：
{{
  "focus": "本周讨论焦点（2-3句：市场在关注什么、核心分歧是什么）",
  "bull_case": "多头核心论点（1-2句）",
  "bear_case": "空头核心论点（1-2句）",
  "events": "事件驱动（政策/订单/财报/技术发布等，无则写'本周无显著事件'）",
  "change": "与上周的变化（讨论热度与情绪的变化方向）"
}}

行业方向：{name}

本周按天数据：
{days_block}

上周按天数据（对比用）：
{prev_block}

注意：只依据给定数据归纳，不要编造具体数字；关键词已按频次排序。"""


def generate_weekly_summaries(industry_data: dict = None) -> dict:
    """为每个行业生成本周摘要。本周 sig 变化才重算（LLM），历史周固定。"""
    hist = _load_history()
    ind_data = industry_data or _load_json(INDUSTRY_DATA_FILE, {})
    inds_in = ind_data.get("industries", {})
    this_week = _iso_week(datetime.now())
    prev_week = _iso_week(datetime.now() - timedelta(days=7))

    for iid, h in hist["industries"].items():
        week_days = _week_posts(h, this_week)
        prev_days = _week_posts(h, prev_week)
        if not week_days:
            continue
        sig = "|".join(f"{d}:{v['posts']}" for d, v in sorted(week_days.items()))
        ws = h.setdefault("weekly_summaries", {})
        # LLM 结果按 sig 缓存；启发式结果每轮重算（代价低，且规则会迭代）
        if (LLM_AVAILABLE and _llm_chat
                and ws.get(this_week, {}).get("sig") == sig
                and ws[this_week].get("source") == "llm"):
            continue  # 本周内容未变化，跳过

        # 帖子明细（供 LLM 更好归纳，截断）
        posts_txt = ""
        ind = inds_in.get(iid) or {}
        for p in (ind.get("viewpoints") or [])[:40]:
            posts_txt += f"- {str(p.get('created_at','')[:10])} @{p.get('author','')}: {str(p.get('text',''))[:120]}\n"

        if LLM_AVAILABLE and _llm_chat:
            days_block = "\n".join(
                f"{d}: 帖{v['posts']} 互动{v['interactions']} 情绪{v.get('sentiment')} "
                f"热词{('、'.join(list((v.get('keywords') or {}).keys())[:8]))}"
                for d, v in sorted(week_days.items()))
            prev_block = "\n".join(
                f"{d}: 帖{v['posts']} 情绪{v.get('sentiment')}"
                for d, v in sorted(prev_days.items())) or "（无记录）"
            prompt = WEEKLY_PROMPT.format(name=h.get("name", iid), days_block=days_block,
                                          prev_block=prev_block)
            if posts_txt:
                prompt += "\n本周代表性帖子摘录：\n" + posts_txt[:6000]
            try:
                raw = _llm_chat(prompt, temperature=0.2)
                m = re.search(r"\{[\s\S]*\}", raw)
                obj = json.loads(m.group(0)) if m else {}
                if obj.get("focus"):
                    ws[this_week] = {"sig": sig, "generated_at": datetime.now().isoformat(),
                                     "source": "llm", **obj}
                    continue
            except Exception as e:
                print(f"    ⚠️ 周摘要 LLM 调用失败({iid}): {e}")
        # 启发式兜底（sig 记录便于 LLM 恢复后重算：source 标 heuristic 但带 sig）
        ws[this_week] = {"sig": sig, "generated_at": datetime.now().isoformat(),
                         **_heuristic_weekly(h.get("name", iid), week_days, prev_days)}

    hist["updated_at"] = datetime.now().isoformat()
    _save_history(hist)
    print(f"  📋 周摘要已更新（{this_week}）")
    return hist


# ============================ IO ============================

def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _load_history() -> dict:
    hist = _load_json(HISTORY_FILE, None)
    if not isinstance(hist, dict) or "industries" not in hist:
        hist = {"updated_at": datetime.now().isoformat(), "industries": {}}
    hist.setdefault("industries", {})
    return hist


def _save_history(hist: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)
    os.makedirs(FRONTEND_DATA_DIR, exist_ok=True)
    tmp2 = FRONTEND_HISTORY + ".tmp"
    with open(tmp2, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False)
    os.replace(tmp2, FRONTEND_HISTORY)


# ============================ CLI ============================

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--update" in args or not args:
        ind = _load_json(INDUSTRY_DATA_FILE, {})
        if not ind.get("industries"):
            print("⚠️ 无 industry_data.json，先运行行业采集")
        else:
            cfg = _load_json(os.path.join(BASE_DIR, "weibo_config.json"), {})
            update_history(ind, cfg)
    if "--weekly" in args or not args:
        generate_weekly_summaries()
