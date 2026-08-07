"""
股市相关性过滤 — 关键词快筛 + LLM 语义兜底

两层策略:
1. 关键词命中（60+ 股市术语，零成本快速通过）
2. 未命中时交给 LLM 判断（识别隐喻/类比，如"村里开会"、"大A"、"上车下车"）

LLM 依赖上层项目的 utils/qwen_utils.py（读取 config.yaml 的 DASHSCOPE_API_KEY），
不可用时自动降级为纯关键词过滤。
"""
import os
import re
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PARENT_DIR = os.path.dirname(BASE_DIR)
if PARENT_DIR not in sys.path:
    sys.path.insert(0, PARENT_DIR)

# ============================ LLM 可用性 ============================

try:
    from utils.qwen_utils import chat as _qwen_chat
    LLM_AVAILABLE = True
except Exception:
    _qwen_chat = None
    LLM_AVAILABLE = False

LLM_MODEL = "qwen3.5-plus"

# ============================ 关键词快筛 ============================

KEYWORDS = [
    # 市场/大盘
    "股市", "A股", "港股", "美股", "大盘", "上证", "深证", "创业板", "科创板",
    "沪指", "深指", "纳指", "道指", "恒生指数", "日经指数", "上证指数", "股民",
    # 交易行为
    "涨停", "跌停", "做多", "做空", "多头", "空头", "牛市", "熊市",
    "加仓", "减仓", "清仓", "满仓", "空仓", "持仓", "建仓", "补仓",
    "割肉", "止损", "止盈", "套牢", "抄底", "追高", "梭哈", "上车", "下车",
    "韭菜", "镰刀", "收割",
    # 工具/品种
    "ETF", "股票", "炒股", "券商", "主力", "散户", "K线", "龙虎榜",
    "北向资金", "南向资金", "融资", "融券", "期权", "股指期货", "国债逆回购",
    "龙头股", "妖股", "题材股",
    # 政策/宏观（与市场强相关）
    "降准", "降息", "加息", "LPR", "MLF", "逆回购", "证监会", "央妈",
    "IPO", "退市", "印花税", "救市", "国家队",
    # 常见隐喻/黑话
    "大A", "红盘", "绿盘", "翻红", "翻绿", "放量", "缩量", "回调", "企稳",
    "反弹行情", "护盘", "砸盘", "洗盘", "高低切换", "情绪周期",
    "黄金坑", "价值投资", "长期主义", "定投",
]

# 去掉短词误伤：短于2字的不用
_keywords = [k for k in KEYWORDS if len(k) >= 2]
_pattern = re.compile("|".join(re.escape(k) for k in _keywords), re.IGNORECASE)


def keyword_hit(text: str) -> bool:
    return bool(_pattern.search(text or ""))


# ============================ LLM 语义判断 ============================

LLM_PROMPT = """你是一个财经内容审核员。请判断下面这条微博内容是否与"股市/证券投资"相关。

注意：博主可能使用隐喻、类比、黑话或暗指，例如：
- "村里""上面"指监管层，"开会""发文"指政策
- "大A""村里发红包""收割""韭菜"指A股市场
- "上车/下车""仓位""子弹"指交易行为
- 讨论个股、板块、指数、宏观政策对投资的影响也算相关

以下情况不算相关：纯生活记录、娱乐八卦、与证券市场无关的新闻评论、纯情感内容。

微博正文：
\"\"\"{text}\"\"\"

只输出一个词：相关 或 无关"""


def llm_classify(text: str) -> bool:
    """LLM 判断股市相关性；异常时抛给上层降级"""
    prompt = LLM_PROMPT.format(text=text[:800])
    resp = _qwen_chat(prompt, model=LLM_MODEL, enable_thinking=False, temperature=0)
    # resp 可能是 str 或 dict，统一取文本
    if isinstance(resp, dict):
        try:
            answer = resp["choices"][0]["message"]["content"]
        except Exception:
            answer = str(resp)
    else:
        answer = str(resp)
    answer = answer.strip()
    # "无关"包含"相关"子串，必须先判"无关"
    if "无关" in answer:
        return False
    return True


# ============================ 对外接口 ============================

def is_stock_related(text: str, retweet_text: str = "") -> dict:
    """
    判断一条微博是否与股市相关。
    返回 {"related": bool, "method": "keyword"|"llm"|"fallback"}
    """
    full_text = f"{text or ''}\n{retweet_text or ''}".strip()
    if not full_text:
        return {"related": False, "method": "fallback"}

    # 第1层：关键词
    if keyword_hit(full_text):
        return {"related": True, "method": "keyword"}

    # 第2层：LLM 语义判断（含隐喻识别）
    if LLM_AVAILABLE:
        try:
            related = llm_classify(full_text)
            return {"related": related, "method": "llm"}
        except Exception as e:
            print(f"    ⚠️ LLM 过滤失败，降级为关键词模式: {e}")
    return {"related": False, "method": "fallback"}


if __name__ == "__main__":
    # 自测
    cases = [
        "今天大盘跳水，兄弟们仓位控制好",
        "中午吃了碗兰州拉面，真香",
        "村里要开会了，懂的都懂，先埋伏一波",
        "周末带娃去公园玩",
        "38岁的教授，绝对人才呀，可惜了",
    ]
    print(f"LLM 可用: {LLM_AVAILABLE}")
    for c in cases:
        r = is_stock_related(c)
        print(f"[{'✅' if r['related'] else '❌'} {r['method']:8s}] {c[:40]}")
