"""股指期货机构净空单 —— 公开渠道直连（中国金融期货交易所 CFFEX）

数据源：中金所每日收盘后发布的「前 20 会员成交持仓排名」CSV
URL 模式（AKShare 已验证）：
    http://www.cffex.com.cn/sj/ccpm/{YYYYMM}/{DD}/{VAR}_1.csv
  - VAR：品种 IF / IC / IM / IH
  - 编码：GBK
  - CSV 共 12 列，按「排名」对齐（同一行含该排名位的成交量/持多单/持空单会员）：
      0 交易日, 1 合约, 2 排名,
      3 成交量会员, 4 成交量, 5 成交量增减,
      6 持多单会员, 7 持买单量, 8 持多单增减,
      9 持空单会员, 10 持卖单量, 11 持空单增减
  - 因此统计某会员总多单，需遍历所有行，把该会员在「持多单会员」列的值累加（跨全部合约）。

计算口径（对应此前 OCR 方案的要求，且更准确）：
  - 中信期货净空单 = Σ(持卖单量 − 持买单量)，跨 IF/IC/IM/IH 全部合约汇总。
  - 其他主要玩家净空单 = 前 20 会员（即 CSV 全部会员）扣除中信期货后，同样跨品种汇总。
  - 绝对净空单水平直接可得，无需 baseline 假设 / 从 0 累加。
  - 每日增减 = 相邻交易日净空单差值。

依赖：仅标准库 urllib + csv（无 Tesseract / 无浏览器 / 不依赖任何大V）。
"""

import csv
import io
import json
import os
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")

CFFEX_URL_TMPL = "http://www.cffex.com.cn/sj/ccpm/{ym}/{dd}/{var}_1.csv"
PRODUCTS = ["IF", "IC", "IM", "IH"]
INSTITUTIONS = ["中信期货", "其他大机构"]

# 中信期货在「会员简称」列可能出现的写法（官方多为「中信期货」）
ZHONGXIN_MATCH = ("中信期货",)


def _is_zhongxin(name):
    if not name:
        return False
    return any(name.startswith(m) for m in ZHONGXIN_MATCH)


def fetch_cffex_csv_text(date_ymd, product):
    """抓取某交易日某品种的 CFFEX 持仓排名 CSV 文本；失败返回 None。"""
    ym = date_ymd[:6]
    dd = date_ymd[6:8]
    url = CFFEX_URL_TMPL.format(ym=ym, dd=dd, var=product)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/81.0.4044.138 Safari/537.36"
            )
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
        # CFFEX 文件为 GBK 编码
        return raw.decode("gbk", errors="replace")
    except Exception as e:  # 网络/解码/404 等均视为无数据
        print(f"[if_public] 抓取 {product} {date_ymd} 失败: {e}", file=sys.stderr)
        return None


def parse_cffex_csv(text):
    """解析 CFFEX 持仓排名 CSV 文本，返回行列表。

    每行：{contract, long_party, long_oi, short_party, short_oi}
    （只抽取与净空单计算相关的字段；按位置解析，跳过表头）。
    """
    if not text:
        return []
    rows = []
    reader = csv.reader(io.StringIO(text))
    for i, fields in enumerate(reader):
        # 跳过表头（含中文列名那一行的首列通常为「交易日」或空）
        if i == 0:
            continue
        if len(fields) < 12:
            continue
        contract = (fields[1] or "").strip()
        long_party = (fields[6] or "").strip()
        short_party = (fields[9] or "").strip()

        def _to_int(x):
            try:
                return int(str(x).strip().replace(",", ""))
            except Exception:
                return 0

        long_oi = _to_int(fields[7])
        short_oi = _to_int(fields[10])
        rows.append(
            {
                "contract": contract,
                "long_party": long_party,
                "long_oi": long_oi,
                "short_party": short_party,
                "short_oi": short_oi,
            }
        )
    return rows


def compute_net_short_for_date(date_ymd):
    """计算某交易日 中信期货 / 其他主要玩家 的净空单（绝对水平）。

    返回 dict：{
      "rows": 全部解析行,
      "total_long", "total_short",
      "zhongxin_long", "zhongxin_short",
      "zhongxin_net", "others_net",
    }
    若当日任一品种无数据，返回 None。
    """
    all_rows = []
    for product in PRODUCTS:
        text = fetch_cffex_csv_text(date_ymd, product)
        rows = parse_cffex_csv(text)
        all_rows.extend(rows)
        time.sleep(0.15)  # 对中金所公开接口的礼貌间隔

    if not all_rows:
        return None

    total_long = sum(r["long_oi"] for r in all_rows)
    total_short = sum(r["short_oi"] for r in all_rows)

    zhongxin_long = sum(
        r["long_oi"] for r in all_rows if _is_zhongxin(r["long_party"])
    )
    zhongxin_short = sum(
        r["short_oi"] for r in all_rows if _is_zhongxin(r["short_party"])
    )
    zhongxin_net = zhongxin_short - zhongxin_long
    total_net = total_short - total_long
    others_net = total_net - zhongxin_net

    return {
        "rows": all_rows,
        "total_long": total_long,
        "total_short": total_short,
        "zhongxin_long": zhongxin_long,
        "zhongxin_short": zhongxin_short,
        "zhongxin_net": zhongxin_net,
        "others_net": others_net,
    }


def _load_sh_index(date_ymd):
    """从已采集的 macro_data.json 取上证指数收盘点位（与现有采集复用，避免额外网络）。"""
    macro_path = os.path.join(DATA_DIR, "macro_data.json")
    if not os.path.exists(macro_path):
        return None
    try:
        with open(macro_path, "r", encoding="utf-8") as f:
            macro = json.load(f)
        # macro_data.json 结构为 {series: [{key: sh_close, values: [{date, value}]}]}
        sh = {}
        for s in macro.get("series", []):
            if s.get("key") == "sh_close":
                sh = {v["date"]: v["value"] for v in s.get("values", [])}
                break
        # 兼容旧版顶层 dict 格式
        if not sh and isinstance(macro.get("sh_close"), dict):
            sh = macro["sh_close"]
        # sh_close 的 key 可能是 "YYYY-MM-DD" 或 "YYYYMMDD"
        return sh.get(date_ymd) or sh.get(
            f"{date_ymd[:4]}-{date_ymd[4:6]}-{date_ymd[6:8]}"
        )
    except Exception:
        return None


def collect_range(end_date=None, backfill_days=60):
    """回补最近 backfill_days 个日历日内的交易日数据，返回按日期升序的 records 列表。

    每个 record：
      {
        "date": "YYYY-MM-DD",
        "net_short_change": {"中信期货": int|None, "其他大机构": int|None},
        "net_short_cumulative": {"中信期货": int, "其他大机构": int},  # 真实绝对水平
        "sh_index": number|None,
        "source": "cffex",
        "note": ""
      }
    """
    if end_date is None:
        end_date = date.today()
    elif isinstance(end_date, str):
        end_date = date(int(end_date[:4]), int(end_date[4:6]), int(end_date[6:8]))

    daily = {}  # ymd -> {zhongxin_net, others_net}
    cur = end_date
    scanned = 0
    while scanned < backfill_days:
        ymd = cur.strftime("%Y%m%d")
        try:
            res = compute_net_short_for_date(ymd)
        except Exception as e:
            print(f"[if_public] 计算 {ymd} 异常: {e}", file=sys.stderr)
            res = None
        if res is not None:
            daily[ymd] = {
                "zhongxin_net": res["zhongxin_net"],
                "others_net": res["others_net"],
            }
        cur -= timedelta(days=1)
        scanned += 1

    if not daily:
        return []

    # 按日期升序
    ordered = sorted(daily.keys())
    records = []
    prev = None
    for ymd in ordered:
        zx = daily[ymd]["zhongxin_net"]
        ot = daily[ymd]["others_net"]
        change = {
            "中信期货": (zx - prev["zhongxin_net"]) if prev else None,
            "其他大机构": (ot - prev["others_net"]) if prev else None,
        }
        iso = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
        records.append(
            {
                "date": iso,
                "positions": {},
                "net_short_change": change,
                "net_short_cumulative": {"中信期货": zx, "其他大机构": ot},
                "sh_index": _load_sh_index(ymd),
                "source": "cffex",
                "note": "",
            }
        )
        prev = daily[ymd]
    return records


def write_positions(records, positions_path=None, frontend_path=None):
    """写入 data/index_futures_positions.json 与 frontend/data/ 副本。"""
    if positions_path is None:
        positions_path = os.path.join(DATA_DIR, "index_futures_positions.json")
    if frontend_path is None:
        frontend_path = os.path.join(
            FRONTEND_DATA_DIR, "index_futures_positions.json"
        )
    data = {
        "institutions": INSTITUTIONS,
        "records": records,
        "note": "数据源：中国金融期货交易所(CFFEX)每日前20会员持仓排名（公开渠道直连，非大V图片OCR）",
    }
    os.makedirs(os.path.dirname(positions_path), exist_ok=True)
    with open(positions_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.makedirs(os.path.dirname(frontend_path), exist_ok=True)
    with open(frontend_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return data


def update_index_futures_positions(cfg=None, backfill_days=60, end_date=None):
    """主入口：回补并写入股指期货净空单数据。返回写入的 data dict。

    与本地已有记录做增量合并：新窗口数据覆盖同日期旧值，
    窗口之外的历史保留（支持一次性长周期回补后，每轮只刷新近期）。
    """
    if cfg:
        if_cfg = cfg.get("index_futures") or {}
        try:
            backfill_days = int(if_cfg.get("backfill_days", backfill_days) or backfill_days)
        except Exception:
            pass
    records = collect_range(end_date=end_date, backfill_days=backfill_days)
    if not records:
        print("[if_public] 未获取到任何交易日数据（可能网络不可达或非交易日）", file=sys.stderr)
        return None

    existing_path = os.path.join(DATA_DIR, "index_futures_positions.json")
    merged = {r["date"]: r for r in records}
    if os.path.exists(existing_path):
        try:
            with open(existing_path, "r", encoding="utf-8") as f:
                old = json.load(f)
            for r in old.get("records", []):
                merged.setdefault(r["date"], r)
        except Exception:
            pass
    all_records = sorted(merged.values(), key=lambda r: r["date"])
    data = write_positions(all_records)
    print(
        f"[if_public] 已写入 {len(all_records)} 条（{all_records[0]['date']} ~ {all_records[-1]['date']}，"
        f"本轮刷新 {len(records)} 条）",
        file=sys.stderr,
    )
    return data


def main_test():
    """解析器单测：用模拟 CSV 验证中信净空单与前20除中信汇总自洽。"""
    sample = """交易日,合约,排名,会员简称,成交量,增减,会员简称,持买单量,增减,会员简称,持卖单量,增减
20260805,IF2609,1,国泰君安,25399,100,中信期货,17401,200,中信期货,17082,300
20260805,IF2609,2,中信期货,22850,50,国泰君安,24777,150,国泰君安,18063,250
20260805,IF2609,3,海通期货,6304,40,银河期货,7612,30,东证期货,12579,20
20260805,IF2612,1,中信期货,1000,10,中信期货,500,5,中信期货,800,6
20260805,IF2612,2,国泰君安,900,9,海通期货,400,4,国泰君安,700,7
"""
    rows = parse_cffex_csv(sample)
    assert len(rows) == 5, f"期望 5 行，实际 {len(rows)}"

    total_long = sum(r["long_oi"] for r in rows)
    total_short = sum(r["short_oi"] for r in rows)
    zhongxin_long = sum(r["long_oi"] for r in rows if _is_zhongxin(r["long_party"]))
    zhongxin_short = sum(r["short_oi"] for r in rows if _is_zhongxin(r["short_party"]))
    zhongxin_net = zhongxin_short - zhongxin_long
    total_net = total_short - total_long
    others_net = total_net - zhongxin_net

    # 手算基准
    exp_zx_long = 17401 + 500
    exp_zx_short = 17082 + 800
    exp_zx_net = exp_zx_short - exp_zx_long  # -19
    exp_total_long = 17401 + 24777 + 7612 + 500 + 400  # 50690
    exp_total_short = 17082 + 18063 + 12579 + 800 + 700  # 49224
    exp_total_net = exp_total_short - exp_total_long  # -1466
    exp_others_net = exp_total_net - exp_zx_net  # -1447

    assert zhongxin_long == exp_zx_long, (zhongxin_long, exp_zx_long)
    assert zhongxin_short == exp_zx_short, (zhongxin_short, exp_zx_short)
    assert zhongxin_net == exp_zx_net, (zhongxin_net, exp_zx_net)
    assert total_long == exp_total_long, (total_long, exp_total_long)
    assert total_short == exp_total_short, (total_short, exp_total_short)
    assert total_net == exp_total_net, (total_net, exp_total_net)
    assert others_net == exp_others_net, (others_net, exp_others_net)
    print("parse+compute test OK")
    print(f"  中信 net={zhongxin_net}  其他 net={others_net}  总量 net={total_net}")


if __name__ == "__main__":
    if "--test" in sys.argv:
        main_test()
    else:
        # 默认：回补最近 60 天并写入
        bd = 60
        for a in sys.argv[1:]:
            if a.startswith("--backfill="):
                bd = int(a.split("=")[1])
        update_index_futures_positions(backfill_days=bd)
