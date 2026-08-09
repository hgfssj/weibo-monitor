"""
股指期货大V复盘图片 OCR 识别模块

识别雪球大V（u/2411215032）每周复盘图片中的：
- 「本周中信净空单增减统计」
- 「本周其他主要玩家净空单增减统计」

提取每日 合计增减（净空单变化量），并累加为累计净空单曲线。

依赖：
- Python: Pillow, pytesseract
- 系统: Tesseract OCR 引擎（macOS: brew install tesseract tesseract-lang）
"""
import json
import os
import re
import shutil
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")
IMAGE_CACHE_DIR = os.path.join(DATA_DIR, "if_images")

DEFAULT_POSITIONS_PATH = os.path.join(DATA_DIR, "index_futures_positions.json")
DEFAULT_FRONTEND_PATH = os.path.join(FRONTEND_DATA_DIR, "index_futures_positions.json")
MACRO_DATA_PATH = os.path.join(DATA_DIR, "macro_data.json")

# 图片标题 -> 内部机构名
TITLE_TO_INST = {
    "中信": "中信期货",
    "其他主要玩家": "其他大机构",
}
INST_DISPLAY = {
    "中信期货": "中信期货",
    "其他大机构": "其他大机构",
}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def load_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return default


def extract_image_urls(post: dict) -> List[str]:
    """兼容 pics 为 ['url1,url2,...'] 或 ['url1','url2'] 两种存储格式。"""
    raw = post.get("pics") or []
    urls = []
    if isinstance(raw, str):
        raw = [raw]
    for item in raw:
        for u in str(item).split(","):
            u = u.strip()
            if u.startswith("http"):
                urls.append(u)
    return urls


def tesseract_available() -> bool:
    """检测系统是否已安装 tesseract 二进制。"""
    if shutil.which("tesseract"):
        return True
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _preprocess_image(image_path: str):
    """加载图片并做简单预处理（放大、灰度），提升 OCR 数字识别率。"""
    from PIL import Image
    img = Image.open(image_path)
    # 转为 RGB（兼容 PNG/JPG）
    if img.mode != "RGB":
        img = img.convert("RGB")
    # 放大 2 倍，表格数字更清晰
    w, h = img.size
    img = img.resize((w * 2, h * 2), Image.LANCZOS)
    # 灰度
    img = img.convert("L")
    return img


def ocr_image(image_path: str, lang: str = "chi_sim+eng") -> str:
    """对图片执行 OCR，返回识别到的原始文本。"""
    import pytesseract
    img = _preprocess_image(image_path)
    return pytesseract.image_to_string(img, lang=lang)


def _normalize_number(s: str) -> Optional[int]:
    """把 OCR 输出的数字串归一化并转 int；识别失败返回 None。"""
    if s is None:
        return None
    # 处理全角、Unicode 减号、中文负号、千分位逗号
    s = str(s).strip().replace(",", "").replace("，", "")
    s = s.replace("−", "-").replace("—", "-").replace("一", "-").replace("负", "-")
    s = s.replace("O", "0").replace("o", "0")
    # 去掉前后非数字字符（OCR 可能粘附文字）
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def detect_table_type(text: str) -> Optional[str]:
    """根据标题关键词判断是中信表还是其他大机构表。"""
    t = text.replace(" ", "").replace("\n", "")
    if "中信" in t and "净空单" in t:
        return "中信期货"
    if ("其他" in t or "其它" in t) and "净空单" in t:
        return "其他大机构"
    return None


def parse_net_short_change_table(text: str, institution: Optional[str] = None) -> List[dict]:
    """
    解析「净空单增减统计」表格文本。
    返回 [{"date": "2026-08-03", "value": 676, "institution": "中信期货"}, ...]
    """
    inst = institution or detect_table_type(text)
    if not inst:
        return []

    rows = []
    # 按行处理，找 20YYYYMMDD 的日期行
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        # 找日期
        dm = re.search(r"(20\d{2})(\d{2})(\d{2})", line)
        if not dm:
            continue
        date_str = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"
        # 去掉日期后，提取剩余数字；最后一个是「合计增减」
        after_date = line[dm.end():]
        nums = re.findall(r"-?\d[\d,]*", after_date)
        vals = [_normalize_number(n) for n in nums]
        vals = [v for v in vals if v is not None]
        if len(vals) < 2:
            continue
        # 最后一列为 合计增减
        rows.append({
            "date": date_str,
            "value": vals[-1],
            "institution": inst,
            "raw": line,
        })
    return rows


def download_image(url: str, path: str, timeout: int = 30) -> bool:
    """下载单张图片；已存在且非空则跳过。"""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return True
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=timeout)
        r.raise_for_status()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(r.content)
        return True
    except Exception as e:
        print(f"    [if_ocr] 图片下载失败 {url}: {e}", file=sys.stderr)
        return False


def process_post(post: dict, cache_root: str = IMAGE_CACHE_DIR,
                 force_reocr: bool = False) -> Dict[str, List[dict]]:
    """
    处理单条帖子中的所有图片，返回识别到的净空单变化记录。
    结果按机构名分组：{"中信期货": [...], "其他大机构": [...]}
    """
    post_id = str(post.get("id", "unknown"))
    urls = extract_image_urls(post)
    results: Dict[str, List[dict]] = {}
    if not urls:
        return results

    for idx, url in enumerate(urls):
        ext = os.path.splitext(url.split("?")[0])[1] or ".jpg"
        img_path = os.path.join(cache_root, post_id, f"img{idx}{ext}")
        ocr_path = img_path + ".ocr.txt"

        if not download_image(url, img_path):
            continue

        # 读取缓存 OCR 文本，避免重复识别
        text = None
        if not force_reocr and os.path.exists(ocr_path):
            try:
                with open(ocr_path, "r", encoding="utf-8") as f:
                    text = f.read()
            except Exception:
                pass

        if text is None:
            try:
                text = ocr_image(img_path)
                with open(ocr_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception as e:
                print(f"    [if_ocr] OCR 失败 {img_path}: {e}", file=sys.stderr)
                continue

        rows = parse_net_short_change_table(text)
        if rows:
            print(f"    [if_ocr] 从 {post_id}/img{idx} 识别到 {len(rows)} 行净空单变化")
            for r in rows:
                results.setdefault(r["institution"], []).append(r)
    return results


def merge_ocr_records(all_results: Dict[str, List[dict]]) -> Dict[str, Dict[str, int]]:
    """
    合并多个帖子/图片识别结果。
    同一日期同一机构若冲突，取最新图片的值（后出现的覆盖先出现的）。
    返回：{institution: {date: value}}
    """
    merged: Dict[str, Dict[str, int]] = {}
    for inst, rows in all_results.items():
        m = {}
        for r in rows:
            m[r["date"]] = r["value"]
        merged[inst] = m
    return merged


def compute_cumulative(changes: Dict[str, Dict[str, int]],
                       institutions: List[str],
                       baseline_date: Optional[str] = None,
                       baseline_values: Optional[Dict[str, int]] = None) -> Dict[str, Dict[str, int]]:
    """
    把每日净空单变化量累加为累计净空单。
    默认从 0 开始；可通过 baseline_date + baseline_values 指定某天真实净空单作为基准。
    返回：{institution: {date: cumulative_value}}
    """
    cumulative: Dict[str, Dict[str, int]] = {}
    for inst in institutions:
        base = (baseline_values or {}).get(inst, 0)
        inst_changes = changes.get(inst, {})
        dates = sorted(inst_changes.keys())
        running = base
        cum = {}
        # 若指定了基准日期，基准日之前无数据；从基准日期开始累加
        for d in dates:
            if baseline_date and d < baseline_date:
                continue
            running += inst_changes[d]
            cum[d] = running
        cumulative[inst] = cum
    return cumulative


def _migrate_record(r: dict, institutions: List[str]) -> dict:
    """兼容旧数据：从 positions 计算 net_short_cumulative / net_short_change。"""
    r.setdefault("net_short_change", {})
    r.setdefault("net_short_cumulative", {})
    positions = r.get("positions") or {}
    for inst in institutions:
        p = positions.get(inst) or {}
        if p.get("long") is not None and p.get("short") is not None:
            r["net_short_cumulative"][inst] = int(p["long"]) - int(p["short"])
    return r


def _load_sh_index_map() -> Dict[str, float]:
    """从宏观数据里取上证指数收盘，用于填充股指期货每日上证点位。"""
    macro = load_json(MACRO_DATA_PATH, {})
    for s in macro.get("series", []):
        if s.get("key") == "sh_close":
            return {v["date"]: v["value"] for v in s.get("values", [])}
    return {}


def update_index_futures_positions(posts_data: dict,
                                   positions_path: str = DEFAULT_POSITIONS_PATH,
                                   frontend_path: str = DEFAULT_FRONTEND_PATH,
                                   cfg: Optional[dict] = None):
    """
    主入口：根据大V帖子 OCR 更新 index_futures_positions.json。
    失败时打印警告，不影响主监控流程。
    """
    if not tesseract_available():
        print("  [if_ocr] ⚠️ 系统未安装 Tesseract，跳过图片识别。"
              "请在终端执行: brew install tesseract tesseract-lang")
        return

    posts = (posts_data or {}).get("posts", [])
    if not posts:
        print("  [if_ocr] 无股指期货大V帖子，跳过 OCR")
        return

    print(f"\n  📊 股指期货: 对 {len(posts)} 条帖子进行图片 OCR 识别...")

    all_results: Dict[str, List[dict]] = {}
    for post in posts:
        res = process_post(post)
        for inst, rows in res.items():
            all_results.setdefault(inst, []).extend(rows)

    if not all_results:
        print("  [if_ocr] 未识别到任何净空单表格（可能图片未命中或 OCR 失败）")
        return

    merged = merge_ocr_records(all_results)
    institutions = sorted(merged.keys())

    # 基准设置
    if_cfg = (cfg or {}).get("index_futures") or {}
    baseline_date = if_cfg.get("baseline_date")
    baseline_values = if_cfg.get("baseline_net_short")

    cumulative = compute_cumulative(merged, institutions,
                                     baseline_date=baseline_date,
                                     baseline_values=baseline_values)

    # 加载已有记录，按日期索引
    data = load_json(positions_path, {
        "institutions": institutions,
        "records": [],
    })
    data.setdefault("institutions", institutions)
    data.setdefault("records", [])

    # 合并机构列表
    for inst in institutions:
        if inst not in data["institutions"]:
            data["institutions"].append(inst)

    # 旧记录迁移
    for r in data["records"]:
        _migrate_record(r, data["institutions"])

    records_by_date: Dict[str, dict] = {r["date"]: r for r in data["records"]}

    all_dates = set()
    for inst in institutions:
        all_dates.update(merged.get(inst, {}).keys())
        all_dates.update(cumulative.get(inst, {}).keys())

    sh_map = _load_sh_index_map()

    for d in sorted(all_dates):
        rec = records_by_date.get(d, {
            "date": d,
            "source_url": "https://xueqiu.com/u/2411215032",
            "note": "OCR 自动识别",
            "positions": {},
            "net_short_change": {},
            "net_short_cumulative": {},
            "sh_index": None,
        })
        for inst in institutions:
            ch = merged.get(inst, {}).get(d)
            cu = cumulative.get(inst, {}).get(d)
            if ch is not None:
                rec["net_short_change"][inst] = ch
            if cu is not None:
                rec["net_short_cumulative"][inst] = cu
        # 如果存在 positions，也同步 net_short_cumulative（手动录入优先）
        for inst in data["institutions"]:
            p = (rec.get("positions") or {}).get(inst) or {}
            if p.get("long") is not None and p.get("short") is not None:
                rec["net_short_cumulative"][inst] = int(p["long"]) - int(p["short"])
        # 填充上证指数（优先宏观数据）
        if rec.get("sh_index") is None and d in sh_map:
            rec["sh_index"] = sh_map[d]
        records_by_date[d] = rec

    data["records"] = sorted(records_by_date.values(), key=lambda x: x["date"])
    data["updated_at"] = datetime.now().isoformat()
    data["ocr_engine"] = "tesseract"
    data["ocr_status"] = f"已识别 {sum(len(v) for v in merged.values())} 条日度变化"

    save_json(positions_path, data)
    save_json(frontend_path, data)
    print(f"    ✅ 已更新 {len(data['records'])} 条记录（机构: {', '.join(institutions)}）")


def main_test():
    """用合成的 OCR 文本测试表格解析。"""
    sample_zhongxin = """20260807 本周中信净空单增减统计
日期 IM增减 IC增减 IF增减 IH增减 合计增减
20260803 309 462 -120 25 676
20260804 1957 -128 442 -297 1974
20260805 -311 1538 237 506 1970
20260806 1409 -688 651 183 1555
20260807 -3058 358 -191 168 -2723
合计增减 306 1542 1019 585 3452
"""
    sample_other = """20260807 本周其他主要玩家净空单增减统计
日期 IM增减 IC增减 IF增减 IH增减 合计增减
20260803 -1932 -920 1300 -40 -1592
20260804 -445 1624 -849 331 661
20260805 -27 -2246 -308 -907 -3488
20260806 -3286 -2310 -762 -255 -6613
20260807 -2222 -490 -884 -1025 -4621
合计增减 -7912 -4342 -1503 -1896 -15653
"""
    rows1 = parse_net_short_change_table(sample_zhongxin)
    rows2 = parse_net_short_change_table(sample_other)
    print("中信 rows:", rows1)
    print("其他 rows:", rows2)
    assert len(rows1) == 5 and rows1[0]["value"] == 676 and rows1[-1]["value"] == -2723
    assert len(rows2) == 5 and rows2[0]["value"] == -1592 and rows2[-1]["value"] == -4621
    print("parse test OK")


if __name__ == "__main__":
    main_test()
