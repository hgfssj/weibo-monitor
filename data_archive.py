#!/usr/bin/env python3
"""
原始帖子档案层 — 多机数据同步的基础，git 仓库内的唯一数据真相源。

目录结构：
  data/archive/
    _meta.json                     # 跨机元数据（git 跟踪）：回填完成标记、
    │                              #   标的完成清单、schema 版本。任何机器
    │                              #   clone 后据此跳过已完成工作（幂等）。
    └── <行业id>/<日期>_<机器>.jsonl   # 原始帖子，按机器分文件（git 无冲突）

幂等性设计：
  - machine_id 绑定 hostname（两行存储）：目录被 cp/rsync 到别的机器时
    自动重新生成，杜绝两台机器写同一个档案文件造成 git 冲突；
  - append_posts 按帖子 id 对整个行业目录去重后追加：
    同一批数据重复处理 → 第二次写入 0 条 → git diff 干净；
  - 历史库 data/industry_history.json 只是本地衍生缓存，
    删除后可由档案确定性重建（rebuild_into_history）。

用法（一般由 data_sync / 监控循环自动调用）：
    python data_archive.py --append    # 用当前 industry_data.json 追加档案
    python data_archive.py --rebuild   # 由档案重建历史库
"""
import json
import os
import sys
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

ARCHIVE_DIR = os.path.join(BASE_DIR, "data", "archive")
MACHINE_FILE = os.path.join(BASE_DIR, "data", ".machine_id")
META_PATH = os.path.join(ARCHIVE_DIR, "_meta.json")

# 档案保留的标准帖子字段（够历史聚合/情绪/关键词/作者统计用）
_FIELDS = ("id", "created_at", "author", "uid", "text",
           "likes", "comments", "reposts", "stock_name", "stock_symbol")


def machine_id() -> str:
    """本机标识：<hostname>\\n<随机id> 两行存储，绑定主机名。

    目录被整体拷贝到另一台机器时（cp -r / rsync / 网盘同步），
    hostname 不匹配会触发重新生成 → 两台机器写不同文件，永不冲突。
    v1 格式（单行、无 hostname）无法验证归属，一律视为不可信并重生成。
    """
    import platform
    import uuid
    host = platform.node()
    if os.path.exists(MACHINE_FILE):
        try:
            with open(MACHINE_FILE, encoding="utf-8") as f:
                parts = [x.strip() for x in f.read().split("\n") if x.strip()]
            if len(parts) == 2 and parts[0] == host and parts[1]:
                return parts[1]
        except OSError:
            pass
    mid = uuid.uuid4().hex[:8]
    os.makedirs(os.path.dirname(MACHINE_FILE), exist_ok=True)
    with open(MACHINE_FILE, "w", encoding="utf-8") as f:
        f.write(f"{host}\n{mid}\n")
    return mid


# ---------------- 跨机元数据（git 跟踪，幂等协同的状态源） ----------------

def read_meta() -> dict:
    """读取档案元数据。结构:
    {"schema_version": 2, "backfill": {"done_date": "", "days": 90, "symbols": {iid: [...]}}}"""
    try:
        with open(META_PATH, encoding="utf-8") as f:
            meta = json.load(f)
        if isinstance(meta.get("backfill"), dict):
            return meta
    except Exception:
        pass
    return {"schema_version": 2,
            "backfill": {"done_date": "", "days": 90, "symbols": {}}}


def write_meta(meta: dict):
    """原子写入元数据。sort_keys 保证跨机器产生一致的 git diff。"""
    meta.setdefault("schema_version", 2)
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    tmp = META_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, META_PATH)


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


def _existing_ids(iid: str) -> set:
    """扫描行业目录下全部档案文件（含他机 pull 下来的），收集已有帖子 id。"""
    d_dir = os.path.join(ARCHIVE_DIR, iid)
    ids = set()
    if not os.path.isdir(d_dir):
        return ids
    for fn in os.listdir(d_dir):
        if not fn.endswith(".jsonl") or fn.startswith("_"):
            continue
        try:
            with open(os.path.join(d_dir, fn), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        pid = str(json.loads(line).get("id") or "")
                    except json.JSONDecodeError:
                        continue
                    if pid:
                        ids.add(pid)
        except OSError:
            continue
    return ids


def append_posts(iid: str, posts: list) -> int:
    """幂等追加帖子到本机档案文件。

    以「该行业目录下所有文件的帖子 id 并集」判重（含他机档案），
    同一批数据重复调用只会写入新帖 → 重复运行产生干净的 git diff。
    返回实际新增条数。
    """
    if not posts:
        return 0
    existing = _existing_ids(iid)
    new = []
    for p in posts:
        np = _norm_post(p)
        pid = str(np.get("id") or "")
        if pid and pid not in existing:
            existing.add(pid)
            new.append(np)
    if not new:
        return 0
    d_dir = os.path.join(ARCHIVE_DIR, iid)
    os.makedirs(d_dir, exist_ok=True)
    fname = f"{datetime.now().strftime('%Y-%m-%d')}_{machine_id()}.jsonl"
    with open(os.path.join(d_dir, fname), "a", encoding="utf-8") as f:
        for np in new:
            f.write(json.dumps(np, ensure_ascii=False) + "\n")
    return len(new)


def append_round(industry_data: dict) -> int:
    """采集一轮后调用：把 industry_data 各行业 viewpoints 追加进档案。"""
    total = 0
    for iid, ind in (industry_data or {}).get("industries", {}).items():
        total += append_posts(iid, ind.get("viewpoints") or [])
    return total


def _check_post(p: dict, today: datetime) -> bool:
    """单条帖子合法性：日期在窗口内、有内容、互动量在合理范围。

    防他机代码 bug 污染（如时间解析错乱成 1970、字段错位成天文数字）。
    """
    d = str(p.get("created_at") or "")[:10]
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
    except ValueError:
        return False
    if not (today - timedelta(days=370)) <= dt <= today + timedelta(days=1):
        return False  # 时钟错乱 / 超期档案
    if not (p.get("text") or "").strip():
        return False  # 无文本且无价值的空帖
    for k in ("likes", "comments", "reposts"):
        try:
            v = int(p.get(k) or 0)  # 容忍字符串型（旧版档案）
        except (TypeError, ValueError):
            return False
        if v < 0 or v > 10_000_000:
            return False  # 字段错位产生异常值
    return True


# 单文件坏行率超过此阈值 → 整文件丢弃（说明那台机器代码版本有问题）
FILE_BAD_RATE_LIMIT = 0.5


def load_all() -> dict:
    """读取全部档案（含他机 pull 下来的文件），按帖子 id 去重。

    三层校验：
      1. 结构归一化（写入端 _norm_post 白名单）；
      2. 逐条合法性（_check_post：日期窗口/文本/互动量范围）；
      3. 文件级健康度：坏行率 > 50% 的整文件丢弃并告警（文件名含机器名，
         可追溯到具体哪台机器的采集代码有问题）。
    """
    out = {}
    if not os.path.isdir(ARCHIVE_DIR):
        return out
    today = datetime.now()
    bad_files = []
    for iid in sorted(os.listdir(ARCHIVE_DIR)):
        d_dir = os.path.join(ARCHIVE_DIR, iid)
        if not os.path.isdir(d_dir):
            continue
        posts = {}
        for fn in sorted(os.listdir(d_dir)):
            if not fn.endswith(".jsonl") or fn.startswith("_"):
                continue
            fpath = os.path.join(d_dir, fn)
            good, bad = [], 0
            try:
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            p = json.loads(line)
                        except json.JSONDecodeError:
                            bad += 1
                            continue
                        pid = str(p.get("id") or "")
                        if pid and pid not in posts and _check_post(p, today):
                            good.append(p)
                        elif pid and pid not in posts:
                            bad += 1
            except OSError:
                continue
            total = len(good) + bad
            if total and bad / total > FILE_BAD_RATE_LIMIT:
                bad_files.append(f"{iid}/{fn} ({bad}/{total} 坏行)")
                continue  # 整文件丢弃
            for p in good:
                posts[str(p["id"])] = p
        if posts:
            out[iid] = list(posts.values())
    if bad_files:
        print(f"  ⚠️ 以下他机档案坏行率过高已整文件丢弃（疑似该机采集代码异常）:")
        for b in bad_files:
            print(f"      · {b}")
    return out


# ---------------- 聚合备份（pre-archive 天级记录的兜底保存） ----------------
# 背景：档案机制上线前的历史库只有天级聚合（原始帖子已不可再得，如人形机器人
# 5-8月数据）。这些聚合导出为 _pre_<machine>.jsonl 随 git 保存，
# 重建时作「下限」合并（取 posts 更多的记录），保证任何机器都能得到全量历史。

def load_prearchive(iid: str) -> dict:
    """读取该行业全部聚合备份文件，按天取 posts 更多者。返回 {date: record}。"""
    d_dir = os.path.join(ARCHIVE_DIR, iid)
    out = {}
    if not os.path.isdir(d_dir):
        return out
    for fn in os.listdir(d_dir):
        if not (fn.startswith("_pre_") and fn.endswith(".jsonl")):
            continue
        try:
            with open(os.path.join(d_dir, fn), encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    d = str(r.get("date") or "")[:10]
                    if not d:
                        continue
                    if d not in out or (r.get("posts") or 0) > (out[d].get("posts") or 0):
                        out[d] = r
        except OSError:
            continue
    return out


def export_prearchive(archive: dict = None) -> int:
    """把历史库中「档案无法重建」的天级聚合导出为备份文件（git 跟踪）。

    幂等：档案可重建的天不导出；已有同等或更全备份的天不重写。
    正常运行时新数据都流经档案，本函数只会一次性导出 pre-archive 时代的天。
    """
    import industry_history
    if archive is None:
        archive = load_all()
    hist = industry_history._load_history()
    n = 0
    for iid, h in (hist.get("industries") or {}).items():
        days = h.get("days") or {}
        if not days:
            continue
        # 档案按天可重建的帖子数
        arch_counts = {}
        for p in archive.get(iid, []):
            d = str(p.get("created_at") or "")[:10]
            if d:
                arch_counts[d] = arch_counts.get(d, 0) + 1
        exported = load_prearchive(iid)
        lines = []
        for d, rec in sorted(days.items()):
            if arch_counts.get(d, 0) >= (rec.get("posts") or 0):
                continue  # 档案可重建，无需备份
            ex = exported.get(d)
            if ex and (ex.get("posts") or 0) >= (rec.get("posts") or 0):
                continue  # 已备份过
            lines.append(json.dumps({"date": d, **rec}, ensure_ascii=False))
            n += 1
        if lines:
            d_dir = os.path.join(ARCHIVE_DIR, iid)
            os.makedirs(d_dir, exist_ok=True)
            fpath = os.path.join(d_dir, f"_pre_{machine_id()}.jsonl")
            with open(fpath, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
    return n


def _merge_prearchive(cfg: dict = None) -> bool:
    """把聚合备份按天下限合并进历史库（取 posts 更多的记录）。"""
    import industry_history
    hist = industry_history._load_history()
    cfg_inds = {i.get("id") or i.get("name"): i
                for i in (cfg or {}).get("industries", [])}
    changed = False
    iids = set(hist.get("industries", {}).keys())
    for iid in os.listdir(ARCHIVE_DIR) if os.path.isdir(ARCHIVE_DIR) else []:
        d_dir = os.path.join(ARCHIVE_DIR, iid)
        if os.path.isdir(d_dir) and any(f.startswith("_pre_") for f in os.listdir(d_dir)):
            iids.add(iid)
    for iid in iids:
        pre = load_prearchive(iid)
        if not pre:
            continue
        h = hist["industries"].setdefault(iid, {
            "name": (cfg_inds.get(iid) or {}).get("name", iid),
            "icon": (cfg_inds.get(iid) or {}).get("icon", "🏭"),
            "days": {}, "authors": {}, "weekly_summaries": {},
        })
        for d, rec in pre.items():
            cur = (h.get("days") or {}).get(d)
            if cur is None or (rec.get("posts") or 0) > (cur.get("posts") or 0):
                h.setdefault("days", {})[d] = {k: v for k, v in rec.items() if k != "date"}
                changed = True
    if changed:
        industry_history._save_history(hist)
        print("  🗄️ 聚合备份已合并（pre-archive 天级记录，档案无法重建的部分）")
    return changed


def rebuild_into_history(cfg: dict = None) -> dict:
    """由档案重建/增厚历史库。

    增量过滤（避免每轮对全量档案重跑 jieba/情绪计算）：
      - 已在历史库 seen_pids 中、且所属日期观测已不小于档案计数的帖子 → 跳过；
      - 其余（新帖子 / 他机补充了更全的日期）→ 送 update_history 重算，
        单调合并规则保证历史只会更全。
    最后合并聚合备份下限，并导出本机独有的聚合（git 备份）。
    """
    import industry_history

    archive = load_all()
    result = {}
    if archive:
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

        if industries_in:
            print(f"  🗃️ 档案重建: {len(industries_in)} 个方向 · "
                  f"{n_sel} 条增量帖子 → 更新历史库")
            result = industry_history.update_history(
                {"industries": industries_in,
                 "updated_at": datetime.now().isoformat()}, cfg)
        else:
            print("  🗃️ 档案与历史库一致，无需重算")

    # 聚合备份：合并他机导出的下限 + 导出本机独有的天
    _merge_prearchive(cfg)
    n_exp = export_prearchive(archive)
    if n_exp:
        print(f"  💾 聚合备份导出: {n_exp} 天（档案无法重建的历史记录）")
    return result


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
