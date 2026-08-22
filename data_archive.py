#!/usr/bin/env python3
"""原始帖子档案层 — 多机数据同步的基础。

设计：
  - 采集到的每条帖子以 JSONL 追加写入 data/archive/<iid>/<日期>_<机器>.jsonl；
  - 文件按机器命名，多台电脑同时使用也不会产生 git 冲突；
  - git 仅跟踪 data/archive/（见 .gitignore），cookie/缓存/历史库仍在本地不入库；
  - 历史库 data/industry_history.json 由档案在本机重建：
    update_history 的「按天单调合并 + 帖子ID去重」保证多机档案并集的正确性
    （他机抓到的帖子合并后，本机历史只会更全、永不缩水）。

用法（一般由 data_sync / 监控循环自动调用）：
    python data_archive.py --append    # 用当前 industry_data.json 追加档案
    python data_archive.py --rebuild   # 由档案重建历史库
"""
import json
import os
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

ARCHIVE_DIR = os.path.join(BASE_DIR, "data", "archive")
MACHINE_FILE = os.path.join(BASE_DIR, "data", ".machine_id")

# 档案保留的标准帖子字段（够历史聚合/情绪/关键词/作者统计用）
_FIELDS = ("id", "created_at", "author", "uid", "text",
           "likes", "comments", "reposts", "stock_name", "stock_symbol")


def machine_id() -> str:
    """本机标识：主机名 + 随机后缀（避免同名机器冲突），存 data/.machine_id。"""
    if os.path.exists(MACHINE_FILE):
        with open(MACHINE_FILE, encoding="utf-8") as f:
            mid = f.read().strip()
        if mid:
            return mid
    import platform
    import uuid
    mid = f"{platform.node().replace(' ', '-')}-{uuid.uuid4().hex[:6]}"
    os.makedirs(os.path.dirname(MACHINE_FILE), exist_ok=True)
    with open(MACHINE_FILE, "w", encoding="utf-8") as f:
        f.write(mid)
    return mid


def _norm_post(p: dict) -> dict:
    """归一化帖子：只保留标准字段，互动量转安全类型（id 保持原字符串）。"""
    out = {}
    for k in _FIELDS:
        v = p.get(k)
        if v is None:
            continue
        if k in ("likes", "comments", "reposts") and v != "":
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = 0
        out[k] = v
    return out


def append_posts(iid: str, posts: list):
    """把一批帖子追加到本机档案文件（同日同机追加写）。"""
    if not posts:
        return 0
    d_dir = os.path.join(ARCHIVE_DIR, iid)
    os.makedirs(d_dir, exist_ok=True)
    fname = f"{datetime.now().strftime('%Y-%m-%d')}_{machine_id()}.jsonl"
    n = 0
    with open(os.path.join(d_dir, fname), "a", encoding="utf-8") as f:
        for p in posts:
            np = _norm_post(p)
            if np.get("id"):
                f.write(json.dumps(np, ensure_ascii=False) + "\n")
                n += 1
    return n


def append_round(industry_data: dict) -> int:
    """采集一轮后调用：把 industry_data 各行业 viewpoints 追加进档案。"""
    total = 0
    for iid, ind in (industry_data or {}).get("industries", {}).items():
        total += append_posts(iid, ind.get("viewpoints") or [])
    return total


def load_all() -> dict:
    """读取全部档案（含他机 pull 下来的文件），按帖子 id 去重。"""
    out = {}
    if not os.path.isdir(ARCHIVE_DIR):
        return out
    for iid in sorted(os.listdir(ARCHIVE_DIR)):
        d_dir = os.path.join(ARCHIVE_DIR, iid)
        if not os.path.isdir(d_dir):
            continue
        posts = {}
        for fn in sorted(os.listdir(d_dir)):
            if not fn.endswith(".jsonl"):
                continue
            try:
                with open(os.path.join(d_dir, fn), encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        p = json.loads(line)
                        pid = str(p.get("id") or "")
                        if pid and pid not in posts:
                            posts[pid] = p
            except (json.JSONDecodeError, OSError):
                continue  # 单文件损坏不影响整体
        if posts:
            out[iid] = list(posts.values())
    return out


def rebuild_into_history(cfg: dict = None) -> dict:
    """由档案重建/增厚历史库。

    增量过滤（避免每轮对全量档案重跑 jieba/情绪计算）：
      - 已在历史库 seen_pids 中、且所属日期观测已不小于档案计数的帖子 → 跳过；
      - 其余（新帖子 / 他机补充了更全的日期）→ 送 update_history 重算，
        单调合并规则保证历史只会更全。
    """
    import industry_history

    archive = load_all()
    if not archive:
        return {}

    hist = industry_history._load_history()
    industries_in = {}
    n_sel = 0
    for iid, posts in archive.items():
        h = hist["industries"].get(iid) or {}
        seen = {str(x) for x in (h.get("seen_pids") or [])}
        day_stats = h.get("days") or {}
        # 档案中每个日期的帖子数
        arch_by_day = {}
        for p in posts:
            d = str(p.get("created_at") or "")[:10]
            if d:
                arch_by_day.setdefault(d, []).append(p)
        selected = []
        for d, plist in arch_by_day.items():
            old_n = (day_stats.get(d) or {}).get("posts", 0)
            if len(plist) > old_n:
                selected.extend(plist)      # 档案更全 → 整天重算
                continue
            selected.extend(p for p in plist
                            if str(p.get("id") or "") not in seen)
        if not selected:
            continue
        industries_in[iid] = {
            "id": iid,
            "name": h.get("name") or iid,
            "icon": h.get("icon", "🏭"),
            "viewpoints": selected,
        }
        n_sel += len(selected)

    if not industries_in:
        print("  🗃️ 档案与历史库一致，无需重建")
        return {}
    print(f"  🗃️ 档案重建: {len(industries_in)} 个方向 · "
          f"{n_sel} 条增量帖子 → 更新历史库")
    return industry_history.update_history(
        {"industries": industries_in,
         "updated_at": datetime.now().isoformat()}, cfg)


if __name__ == "__main__":
    if "--append" in sys.argv:
        p = os.path.join(BASE_DIR, "data", "industry_data.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                n = append_round(json.load(f))
            print(f"✅ 已追加 {n} 条帖子到档案（机器 {machine_id()}）")
        else:
            print("⚠️ 无 industry_data.json")
    if "--rebuild" in sys.argv:
        cfg_p = os.path.join(BASE_DIR, "weibo_config.json")
        cfg = {}
        if os.path.exists(cfg_p):
            with open(cfg_p, encoding="utf-8") as f:
                cfg = json.load(f)
        rebuild_into_history(cfg)
