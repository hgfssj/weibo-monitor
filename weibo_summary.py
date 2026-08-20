"""
大V观点总结 — 基于大V近期发言 + 本人评论，用大模型推断：
  1. 对当前行情的多空观点 (market_view)
  2. 自身仓位的水位     (position_level)
  3. 可能持有的个股/板块 (holdings)
  4. 近期加减仓情况       (add_reduce)

LLM 优先级（任一可用即可）：
  1. 仓库约定:  from utils.qwen_utils import chat
                （需上级目录 utils/ + config.yaml 中的 DASHSCOPE_API_KEY）
  2. 内置 DashScope REST：环境变量 DASHSCOPE_API_KEY
                （无需外部 utils，使用 urllib 直连，默认模型 qwen-plus，
                  可用 DASHSCOPE_MODEL 覆盖）
两者都不可用时 LLM_AVAILABLE=False，上层用缓存/启发式兜底。
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

# ============================ LLM 可用性探测 ============================

try:
    from utils.qwen_utils import chat as _repo_chat
    REPO_LLM = True
except Exception:
    _repo_chat = None
    REPO_LLM = False

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_MODEL = os.environ.get("DASHSCOPE_MODEL", "qwen-plus")
BUILTIN_LLM = bool(DASHSCOPE_API_KEY)

LLM_AVAILABLE = REPO_LLM or BUILTIN_LLM

# ============================ LLM 调用 ============================

def _call_dashscope(prompt: str, temperature: float = 0.0) -> str:
    """内置 DashScope 文本生成 REST 调用（urllib，无第三方依赖）"""
    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/text-generation/generation"
    body = json.dumps({
        "model": DASHSCOPE_MODEL,
        "input": {"messages": [{"role": "user", "content": prompt}]},
        "parameters": {"temperature": temperature, "result_format": "message"},
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + DASHSCOPE_API_KEY,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "ignore")[:300]
        raise RuntimeError(f"DashScope HTTP {e.code}: {detail}")
    try:
        return data["output"]["choices"][0]["message"]["content"]
    except Exception:
        raise RuntimeError("DashScope 返回格式异常: " + str(data)[:200])


def _llm_chat(prompt: str, temperature: float = 0.0):
    if REPO_LLM:
        resp = _repo_chat(prompt, model="qwen3.5-plus",
                          enable_thinking=False, temperature=temperature)
        # 兼容仓库 chat 返回 dict 或 str
        if isinstance(resp, dict):
            try:
                return resp["choices"][0]["message"]["content"]
            except Exception:
                return str(resp)
        return str(resp)
    return _call_dashscope(prompt, temperature)


# ============================ Prompt ============================

PROMPT_TEMPLATE = """你是一个资深证券分析师助理。下面是一位财经大V（{platform_label}账号：{name}）近期的发言与评论记录（按时间倒序，含日期，含其本人评论）。请基于这些内容，推断该大V当前的投资观点。

请重点从内容中推断：
1. 对当前大盘/行情的多空观点（看多 / 看空 / 中性，以及理由）
2. 自身仓位的水位（高仓位 / 中等 / 低仓位 / 空仓，结合其表述推断）
3. 可能持有的个股或板块（列出名称，并简要分析其看法）
4. 近期加减仓动作（加仓 / 减仓 / 维持 / 不明，结合表述）

注意事项：
- 只依据给出的发言/评论推断，不要凭空编造具体个股或仓位数字。
- 若发言中财经相关内容很少、不足以判断，请在 confidence 标为 "low" 并如实说明"暂不足以判断"。
- 仓位、加减仓等敏感信息，若大V未明确提及，标注为"未明确披露"而非猜测。

发言/评论记录：
{posts_block}

请严格按照下面的 JSON 格式输出（只输出 JSON，不要多余文字、不要 markdown 代码块）：
{{
  "market_view": {{"stance": "bullish|bearish|neutral", "text": "一句话概括多空观点及理由"}},
  "position_level": {{"level": "high|medium|low|empty", "text": "仓位水位判断及依据"}},
  "holdings": [{{"name": "个股或板块名", "analysis": "对其看法/分析"}}],
  "add_reduce": {{"action": "add|reduce|hold|unknown", "text": "加减仓动作及依据"}},
  "confidence": "high|medium|low"
}}"""


def _build_posts_block(posts, limit: int = 25) -> str:
    lines = []
    for i, p in enumerate(posts[:limit], 1):
        tag = "评论" if p.get("type") == "comment" else "发言"
        lines.append(f"[{i}] ({tag} {str(p.get('created_at', ''))[:10]}) {p.get('text', '')}")
    return "\n".join(lines)


def _extract_json(text: str):
    """从 LLM 返回中稳健提取 JSON 对象"""
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def _normalize(data: dict) -> dict:
    data = data or {}
    data.setdefault("market_view", {"stance": "neutral", "text": ""})
    data.setdefault("position_level", {"level": "medium", "text": ""})
    data.setdefault("holdings", [])
    data.setdefault("add_reduce", {"action": "unknown", "text": ""})
    data.setdefault("confidence", "low")
    if not isinstance(data.get("holdings"), list):
        data["holdings"] = []
    return data


# ============================ 对外接口 ============================

def analyze_bigv(name: str, platform: str, posts: list) -> dict or None:
    """
    基于大V近期发言/评论生成观点总结。
    返回标准化 dict；LLM 不可用或解析失败返回 None（由上层用缓存/启发式兜底）。
    """
    if not LLM_AVAILABLE:
        return None
    if not posts:
        return None
    platform_label = {"xueqiu": "雪球", "xhs": "小红书"}.get(platform, "微博")
    prompt = PROMPT_TEMPLATE.format(
        platform_label=platform_label, name=name or "",
        posts_block=_build_posts_block(posts))
    try:
        raw = _llm_chat(prompt)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            print(f"    ⚠️ 总结 LLM 返回无法解析为 JSON，跳过")
            return None
        return _normalize(data)
    except Exception as e:
        print(f"    ⚠️ 总结 LLM 调用失败: {e}")
        return None


# ============================ 启发式兜底（无 LLM 时） ============================

_BULL = ["看多", "牛市", "加仓", "满仓", "抄底", "看好转", "上涨", "机会",
         "进场", "上车", "做多", "乐观", "重仓", "利好", "坚定持有"]
_BEAR = ["看空", "熊市", "减仓", "清仓", "空仓", "割肉", "下跌", "风险",
         "离场", "下车", "做空", "悲观", "轻仓", "避险", "利空"]
_ADD = ["加仓", "补仓", "建仓", "进场", "上车", "抄底", "加杠杆"]
_REDUCE = ["减仓", "清仓", "离场", "下车", "减磅", "出局", "降仓"]
_LEVEL_HIGH = ["满仓", "重仓", "满杠杆", "九成仓", "八成仓"]
_LEVEL_LOW = ["空仓", "轻仓", "清仓", "空仓观望", "Almost空仓", "低仓位"]
_LEVEL_MED = ["半仓", "五成仓", "中等仓位", "六成仓", "七成仓"]


def _count(words, text):
    return sum(text.count(w) for w in words)


def heuristic_summary(posts: list, name: str = "") -> dict:
    """
    无大模型时的关键词启发式总结（低置信度，仅作占位/兜底）。
    仅依据显性词汇做最粗糙判断，绝不对仓位/个股做具体猜测。
    """
    text = "\n".join(p.get("text", "") for p in posts)
    bull = _count(_BULL, text)
    bear = _count(_BEAR, text)

    if bull > bear:
        stance, mv_text = "bullish", f"近期发言偏多（命中看多/加仓类词汇 {bull} 次），倾向乐观"
    elif bear > bull:
        stance, mv_text = "bearish", f"近期发言偏空（命中看空/减仓类词汇 {bear} 次），倾向谨慎"
    else:
        stance, mv_text = "neutral", "多空信号不明显或样本不足，无法判断"

    if _count(_LEVEL_HIGH, text):
        level, pl_text = "high", "文中出现满仓/重仓等表述"
    elif _count(_LEVEL_LOW, text):
        level, pl_text = "low", "文中出现空仓/轻仓等表述"
    elif _count(_LEVEL_MED, text):
        level, pl_text = "medium", "文中出现半仓/中等仓位等表述"
    else:
        level, pl_text = "medium", "未明确披露仓位，无法判断"

    if _count(_ADD, text) > _count(_REDUCE, text):
        action, ar_text = "add", "文中加仓类表述多于减仓类"
    elif _count(_REDUCE, text) > _count(_ADD, text):
        action, ar_text = "reduce", "文中减仓类表述多于加仓类"
    else:
        action, ar_text = "unknown", "加减仓信号不明显"

    return _normalize({
        "market_view": {"stance": stance, "text": mv_text},
        "position_level": {"level": level, "text": pl_text},
        "holdings": [],
        "add_reduce": {"action": action, "text": ar_text},
        "confidence": "low",
    })


if __name__ == "__main__":
    print(f"REPO_LLM={REPO_LLM} BUILTIN_LLM={BUILTIN_LLM} LLM_AVAILABLE={LLM_AVAILABLE}")
