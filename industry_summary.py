"""
行业研判 — 在「股票池 + 近 7 天帖子/评论」基础上做两层推导：

  1. 个股推导 (analyze_stock)：基于单只股票近 7 天讨论，推断其多空倾向、
     核心主题、催化与风险。
  2. 行业聚合 (analyze_industry)：汇总各股票推导 + 近 7 天门面涨跌，
     形成趋势发展 / 估值潜力 / 多空共识 / 关键要点。
  3. 观点点评 (build_commentary)：对「有价值的观点」结合其评论（精彩评论）
     做一句综合点评。

LLM 优先级与 weibo_summary 一致（utils.qwen_utils 或 DASHSCOPE_API_KEY）；
两者都不可用则 LLM_AVAILABLE=False，上层用启发式兜底（关键词粗筛 +
互动热度统计 + 近 7 天门面涨跌）。
"""
from collections import Counter

from weibo_summary import LLM_AVAILABLE, _llm_chat, _extract_json

# ============================ 关键词 ============================

_BULL = ["看多", "看涨", "利好", "机会", "看好", "牛市", "上行", "超预期",
         "拐点", "爆发", "景气", "加仓", "布局", "重仓", "成长", "空间",
         "受益", "催化", "订单", "业绩", "反转", "放量", "突破", "量产", "装机"]
_BEAR = ["看空", "看跌", "利空", "风险", "熊市", "下行", "承压", "杀估值",
         "不及预期", "减仓", "降温", "过剩", "内卷", "亏损", "暴雷", "回调",
         "高估", "泡沫", "制裁", "降价", "竞争", "审批"]
_CATALYST = ["催化", "订单", "中标", "获批", "放量", "合作", "提价", "政策",
            "业绩", "拐点", "突破", "量产", "装机", "落地", "扩容", "签约"]
_RISK = ["风险", "利空", "承压", "过剩", "内卷", "亏损", "暴雷", "回调",
         "高估", "泡沫", "制裁", "竞争", "降价", "审批", "商誉", "解禁"]


def _count(words, text):
    return sum(text.count(w) for w in words)


def _stance_from_text(text):
    bull = _count(_BULL, text)
    bear = _count(_BEAR, text)
    if bull > bear * 1.3:
        return "bullish", bull, bear
    if bear > bull * 1.3:
        return "bearish", bull, bear
    if bull == 0 and bear == 0:
        return "neutral", bull, bear
    return "divided", bull, bear


# ============================ 个股推导 ============================

STOCK_PROMPT = """你是一个资深行业研究员。下面是雪球上关于个股【{stock}】近 7 天的讨论帖子（按时间倒序，含作者、日期）。请综合这些讨论，对该个股形成研判。

请重点输出：
1. 多空倾向（sentiment.stance）：看多 / 看空 / 中性 / 分歧，依据是什么（只依据给出的讨论）。
2. 核心主题（themes）：3-4 条市场最关心的点（如技术路线、订单、估值、竞争格局）。
3. 催化因素（catalysts）：2-3 条可能的正面催化。
4. 风险因素（risks）：2-3 条主要风险。

注意事项：
- 只依据给出的讨论推断，不要凭空编造具体数字或目标价。
- 样本很少时 confidence 标 "low" 并如实说明。
- 区分"观点"与"事实"。

讨论记录：
{vp_block}

请严格按照下面的 JSON 输出（只输出 JSON，不要多余文字、不要 markdown）：
{{
  "sentiment": {{"stance": "bullish|bearish|neutral|divided", "text": "多空倾向及依据"}},
  "themes": ["主题1", "主题2", "主题3"],
  "catalysts": ["催化1", "催化2"],
  "risks": ["风险1", "风险2"],
  "confidence": "high|medium|low"
}}"""


def _build_vp_block(vps, limit: int = 30) -> str:
    lines = []
    for i, v in enumerate(vps[:limit], 1):
        tag = f"@{v.get('author', '')} {str(v.get('created_at', ''))[:10]}"
        lines.append(f"[{i}] ({tag}) {v.get('text', '')}")
    return "\n".join(lines)


def analyze_stock(name: str, posts: list, quotes_map: dict = None) -> dict:
    """基于单只股票近 7 天帖子推导个股研判。LLM 优先，否则启发式。"""
    quotes_map = quotes_map or {}
    if LLM_AVAILABLE and posts:
        prompt = STOCK_PROMPT.format(stock=name or "", vp_block=_build_vp_block(posts))
        try:
            raw = _llm_chat(prompt)
            data = _extract_json(raw)
            if isinstance(data, dict):
                data.setdefault("sentiment", {"stance": "neutral", "text": ""})
                data.setdefault("themes", [])
                data.setdefault("catalysts", [])
                data.setdefault("risks", [])
                data.setdefault("confidence", "low")
                data["source"] = "llm"
                eng = _engagement(posts)
                data["engagement"] = eng
                return data
        except Exception as e:
            print(f"    ⚠️ 个股《{name}》LLM 失败: {e}")
    return heuristic_stock_summary(posts, name)


def _engagement(posts):
    likes = sum(int(p.get("likes") or 0) for p in posts)
    comments = sum(int(p.get("comments") or 0) for p in posts)
    return {"posts": len(posts), "likes": likes, "comments": comments}


def heuristic_stock_summary(posts: list, name: str = "") -> dict:
    """无大模型时的个股关键词启发式（低置信度占位/兜底）。"""
    if not posts:
        return {
            "sentiment": {"stance": "neutral", "text": "近 7 天暂无讨论样本，无法判断"},
            "themes": [], "catalysts": [], "risks": [],
            "engagement": {"posts": 0, "likes": 0, "comments": 0},
            "confidence": "low", "source": "heuristic",
        }
    text = "\n".join(p.get("text", "") for p in posts)
    stance, bull, bear = _stance_from_text(text)
    stance_text = {
        "bullish": f"讨论整体偏多（看多类 {bull} 次 / 看空类 {bear} 次）",
        "bearish": f"讨论整体偏空（看空类 {bear} 次 / 看多类 {bull} 次）",
        "divided": f"多空分歧（看多 {bull} 次 / 看空 {bear} 次，势均）",
        "neutral": "多空信号不明显或样本不足",
    }[stance]

    catalysts = [w for w in _CATALYST if w in text]
    risks = [w for w in _RISK if w in text]
    # 主题：出现频率较高的催化/风险词，去重保留前 4
    themes = []
    for w in (catalysts + risks):
        if w not in themes:
            themes.append(w)
    themes = themes[:4]

    return {
        "sentiment": {"stance": stance, "text": stance_text},
        "themes": themes,
        "catalysts": catalysts[:3],
        "risks": risks[:3],
        "engagement": _engagement(posts),
        "confidence": "low",
        "source": "heuristic",
    }


# ============================ 行业聚合 ============================

INDUSTRY_PROMPT = """你是一个资深行业研究员。下面是针对【{industry}】板块，按股票池逐只拆解后的个股研判（含多空倾向、主题、催化、风险），以及少量代表性讨论原文。请综合形成该行业方向的研判。

请重点输出：
1. 趋势发展研判（trend）：当前处于什么阶段——景气度上行 / 下行 / 震荡 / 主题轮动，核心驱动与压制因素。
2. 估值潜力判断（valuation）：便宜 / 合理 / 偏贵 / 难判断，结合近 7 天门面表现与市场情绪。
3. 多空共识（sentiment）：整体偏看多 / 偏看空 / 分歧较大 / 中性。
4. 关键要点（key_points）：3-5 条核心结论。

注意事项：
- 只依据给出的研判/讨论推断，不编造具体数字或目标价。
- 样本很少时 confidence 标 "low"。

个股研判：
{stock_block}

代表性讨论（节选）：
{vp_block}

请严格按照下面的 JSON 输出（只输出 JSON，不要多余文字、不要 markdown）：
{{
  "trend": {{"direction": "upward|downward|volatile|rotating|neutral", "text": "趋势研判及核心驱动/压制"}},
  "valuation": {{"level": "cheap|fair|expensive|uncertain", "text": "估值判断及依据"}},
  "sentiment": {{"stance": "bullish|bearish|neutral|divided", "text": "多空共识及依据"}},
  "key_points": ["要点1", "要点2", "要点3"],
  "confidence": "high|medium|low"
}}"""


def analyze_industry(name: str, stocks_summaries: list, all_posts: list,
                     stocks_meta: list = None) -> dict or None:
    """基于各股票推导聚合行业研判。LLM 优先，否则启发式。"""
    stocks_meta = stocks_meta or []
    if LLM_AVAILABLE and (stocks_summaries or all_posts):
        stock_block = "\n".join(
            f"- {s.get('name','')}：{s.get('summary',{}).get('sentiment',{}).get('stance','')}"
            f"｜主题：{', '.join(s.get('summary',{}).get('themes',[]) or [])}"
            f"｜催化：{', '.join(s.get('summary',{}).get('catalysts',[]) or [])}"
            f"｜风险：{', '.join(s.get('summary',{}).get('risks',[]) or [])}"
            for s in stocks_summaries)
        prompt = INDUSTRY_PROMPT.format(
            industry=name or "", stock_block=stock_block or "（无个股研判）",
            vp_block=_build_vp_block(all_posts, 30))
        try:
            raw = _llm_chat(prompt)
            data = _extract_json(raw)
            if isinstance(data, dict):
                data.setdefault("trend", {"direction": "neutral", "text": ""})
                data.setdefault("valuation", {"level": "uncertain", "text": ""})
                data.setdefault("sentiment", {"stance": "neutral", "text": ""})
                data.setdefault("key_points", [])
                data.setdefault("confidence", "low")
                data["source"] = "llm"
                return data
        except Exception as e:
            print(f"    ⚠️ 行业《{name}》LLM 失败: {e}")
    return heuristic_industry_summary(stocks_summaries, all_posts, stocks_meta, name)


def _dedupe(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _avg_change(stocks_meta):
    vals = []
    for s in stocks_meta:
        q = s.get("quote") or {}
        chg = q.get("change_pct")
        try:
            vals.append(float(chg))
        except (TypeError, ValueError):
            pass
    return sum(vals) / len(vals) if vals else None


def heuristic_industry_summary(stocks_summaries, all_posts, stocks_meta, name=""):
    """无大模型时的行业聚合启发式。"""
    # 多空共识：统计个股 stance
    stances = [s.get("summary", {}).get("sentiment", {}).get("stance", "neutral")
               for s in stocks_summaries]
    if not stances:
        stances = ["neutral"]
    bull = stances.count("bullish")
    bear = stances.count("bearish")
    divided = stances.count("divided")
    if bull > bear + divided:
        sent = ("bullish", f"多数个股讨论偏多（{bull}/{len(stances)} 偏多）")
    elif bear > bull + divided:
        sent = ("bearish", f"多数个股讨论偏空（{bear}/{len(stances)} 偏空）")
    elif divided >= max(bull, bear):
        sent = ("divided", "个股间多空分歧较大")
    else:
        sent = ("neutral", "多空信号不明显或样本不足")

    # 趋势：结合近 7 天门面平均涨跌
    avg = _avg_change(stocks_meta)
    if avg is not None:
        if avg > 3:
            trend = ("upward", f"近 7 天门面平均涨幅 {avg:+.1f}%，板块情绪偏强、景气度上行")
        elif avg < -3:
            trend = ("downward", f"近 7 天门面平均跌幅 {avg:+.1f}%，板块承压、景气度下行")
        else:
            trend = ("volatile", f"近 7 天门面平均 {avg:+.1f}%，板块震荡整理")
    else:
        trend = ("neutral", "样本有限，无法判断趋势阶段")

    # 关键要点：汇总个股催化/风险 + 热度主题
    cats, risks, themes = [], [], []
    for s in stocks_summaries:
        sm = s.get("summary", {})
        cats.extend(sm.get("catalysts", []))
        risks.extend(sm.get("risks", []))
        themes.extend(sm.get("themes", []))
    key_points = []
    if cats:
        key_points.append("正向催化：" + "、".join(_dedupe(cats)[:4]))
    if risks:
        key_points.append("主要风险：" + "、".join(_dedupe(risks)[:4]))
    if not key_points:
        key_points.append("近 7 天讨论样本有限，以上为关键词粗筛，置信度低")
    key_points.append("（未接入大模型，研判为启发式，仅供参考）")

    return {
        "trend": {"direction": trend[0], "text": trend[1]},
        "valuation": {"level": "uncertain",
                      "text": "未接入大模型，估值难判断（可结合门面涨跌与业绩兑现度自行评估）"},
        "sentiment": {"stance": sent[0], "text": sent[1]},
        "key_points": key_points,
        "confidence": "low",
        "source": "heuristic",
    }


# ============================ 观点点评 ============================

COMMENTARY_PROMPT = """你是一个资深行业研究员。下面是一条雪球上关于某行业/个股的「有价值观点」原文，以及它的部分精彩评论。请结合原文与评论，写一段 2-3 句的中文「综合点评」：概括该观点核心、评论区的主流反应（看多/看空/分歧）、并给出一句谨慎的 independent 提醒（观点有偏，需自行判断）。

观点原文：
{post_text}

精彩评论：
{comments_block}

请直接输出点评文字（不要标题、不要 markdown）："""


def build_commentary(post: dict, comments: list, reply_count: int = 0) -> str:
    """对单条有价值观点生成综合点评（LLM 优先，否则启发式）。"""
    text = post.get("text", "")
    if LLM_AVAILABLE:
        cb = "\n".join(f"- {c.get('author','')}：{c.get('text','')}"
                        for c in comments[:5]) or "（无精彩评论）"
        prompt = COMMENTARY_PROMPT.format(post_text=text, comments_block=cb)
        try:
            out = _llm_chat(prompt)
            if isinstance(out, str) and out.strip():
                return out.strip()
        except Exception:
            pass
    return heuristic_commentary(post, comments, reply_count)


def heuristic_commentary(post: dict, comments: list, reply_count: int = 0) -> str:
    text = post.get("text", "")
    stance, bull, bear = _stance_from_text(text)
    stance_cn = {"bullish": "偏多", "bearish": "偏空", "divided": "多空分歧",
                 "neutral": "中性"}[stance]
    likes = int(post.get("likes") or 0)
    heat = "高" if (reply_count >= 5 or likes >= 30) else ("中" if (reply_count >= 1 or likes >= 5) else "低")
    parts = [f"该观点整体{stance_cn}，获 {likes} 赞、{reply_count} 条评论，讨论热度{heat}。"]
    if comments:
        ctext = "\n".join(c.get("text", "") for c in comments)
        cb, cbull, cbear = _stance_from_text(ctext)
        if cb == "bullish":
            parts.append("评论区多数呼应看多。")
        elif cb == "bearish":
            parts.append("评论区偏谨慎/看空。")
        elif cb == "divided":
            parts.append("评论区分歧明显。")
        else:
            parts.append("评论区未形成明显倾向。")
    else:
        parts.append("暂无精彩评论样本（雪球评论完整列表受反爬限制，仅取精彩评论）。")
    parts.append("以上为大模型/关键词粗筛，观点有偏，仅供参考、需独立判断。")
    return "".join(parts)


if __name__ == "__main__":
    print(f"LLM_AVAILABLE={LLM_AVAILABLE}")
