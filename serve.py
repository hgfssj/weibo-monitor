"""
微博监控前端服务 — 静态文件服务器 + 配置读写 API

用法:
    python serve.py             # 默认 8766 端口
    python serve.py --port 9000

额外接口:
    GET  /api/config          -> 返回 weibo_config.json 当前内容
    POST /api/save-config     -> 校验并原子写入 weibo_config.json（自动备份）
"""
import argparse
import http.server
import json
import os
import random
import re
import shutil
import socketserver
import threading

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")
CONFIG_PATH = os.path.join(BASE_DIR, "weibo_config.json")
CONFIG_BAK = os.path.join(BASE_DIR, "weibo_config.json.bak")
INDEX_FUTURES_PATH = os.path.join(BASE_DIR, "data", "index_futures_positions.json")
INDEX_FUTURES_BAK = os.path.join(BASE_DIR, "data", "index_futures_positions.json.bak")
INTRADAY_SNAP_PATH = os.path.join(BASE_DIR, "data", "intraday_daily_snapshot.json")

# ---------------- 盘中实时（腾讯分时 + 新浪行业实时，30秒服务端缓存） ----------------
_INTRADAY_CACHE = {"ts": 0.0, "data": None}
_INTRADAY_LOCK = threading.Lock()  # 多线程服务器下防重复抓取

# 申万二级热门行业（与日线图重点关注行业一致，另加高热度赛道）
SW_SECOND_FOCUS = [
    ("801081", "半导体"), ("801104", "软件开发"), ("801078", "自动化设备"),
    ("801193", "证券"), ("801194", "保险"), ("801125", "白酒"),
    ("801737", "电池"), ("801085", "消费电子"), ("801151", "化学制药"), ("801735", "光伏设备"),
]


def _fetch_sw_min(code, name):
    import akshare as ak
    import time as _t

    df = None
    for attempt in range(3):  # 含重试防限流
        try:
            df = ak.index_min_sw(symbol=code)
            if df is not None and len(df):
                break
        except Exception:
            df = None
        _t.sleep(1.0 + attempt)
    if df is None or not len(df):
        return {"key": "sw" + code, "name": name, "points": []}
    pts = []
    for i in range(0, len(df), 6):  # 10秒粒度降采样到约 1 分钟
        row = df.iloc[i]
        t = str(row["时间"])[:5].replace(":", "")
        pts.append([t, float(row["价格"])])
    return {"key": "sw" + code, "name": name, "points": pts}


def load_intraday():
    import time as _time

    now = _time.time()
    if _INTRADAY_CACHE["data"] is not None and now - _INTRADAY_CACHE["ts"] < 30:
        return _INTRADAY_CACHE["data"]
    with _INTRADAY_LOCK:
        # 拿到锁后再检查一次：可能已有其他线程完成抓取
        if _INTRADAY_CACHE["data"] is not None and _time.time() - _INTRADAY_CACHE["ts"] < 30:
            return _INTRADAY_CACHE["data"]
        return _build_intraday()


def _build_intraday():
    import time as _time
    import urllib.request

    now = _time.time()
    try:
        from index_trend import INDEXES
    except Exception:
        INDEXES = []

    indexes = []
    for key, name, symbol, src in INDEXES:
        if src != "sina":
            continue  # 中证2000 无分时源
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/minute/query?code={symbol}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            d = json.loads(urllib.request.urlopen(req, timeout=8).read())
            inner = d["data"][symbol]["data"]
            pts = []
            for line in inner.get("data", []):
                p = line.split()
                if len(p) >= 2:
                    pts.append([p[0], float(p[1])])
            indexes.append({"key": key, "name": name, "date": inner.get("date", ""), "points": pts})
        except Exception as e:
            indexes.append({"key": key, "name": name, "points": [], "error": str(e)[:80]})

    industries, total = [], 0.0
    try:
        url = "https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn"})
        txt = urllib.request.urlopen(req, timeout=8).read().decode("gbk", errors="ignore")
        obj = json.loads(txt[txt.index("{"): txt.rindex("}") + 1])
        for v in obj.values():
            f = v.split(",")
            try:
                amount = float(f[7]) / 1e8  # 成交额(亿元)
                pct = float(f[5])           # 涨跌幅(%)
            except Exception:
                continue
            industries.append({"name": f[1], "amount": round(amount, 1), "change_pct": round(pct, 2)})
            total += amount
        for e in industries:
            e["share"] = round(e["amount"] / total * 100, 2) if total else 0
        industries.sort(key=lambda x: -x["amount"])
    except Exception:
        industries = []

    # 申万行业指数分时（一级全覆盖 + 二级热门，并行抓取，单个失败不影响其他）
    from concurrent.futures import ThreadPoolExecutor

    try:
        from industry_turnover import FIRST_LEVEL
    except Exception:
        FIRST_LEVEL = {}

    def _fetch_group(pairs):
        out = []
        # 并发降到 4，每个请求前随机间隔，避免申万接口限流
        def _worker(c, n):
            _time.sleep(random.random() * 0.5)
            return _fetch_sw_min(c, n)

        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(_worker, c, n): (c, n) for c, n in pairs}
            for fut, (c, n) in futs.items():
                try:
                    out.append(fut.result(timeout=45))
                except Exception:
                    out.append({"key": "sw" + c, "name": n, "points": []})
        return out

    sw_second, sw_first = [], []
    try:
        sw_second = _fetch_group(SW_SECOND_FOCUS)
        sw_first = _fetch_group([(c, n) for c, n in FIRST_LEVEL.items()])
    except Exception:
        pass

    data = {
        "time": _time.strftime("%Y-%m-%d %H:%M:%S"),
        "indexes": indexes,
        "sw_first": sw_first,
        "sw_second": sw_second,
        "industries": industries,
        "total_amount": round(total, 1),
    }

    # 每日行业快照（双槽 prev/today）：today 盘中持续覆盖（收盘后即为全天累计）；
    # 跨日时旧 today 自动转入 prev，保证次交易日全天都能对比“前一日”
    today_key = _time.strftime("%Y%m%d")
    prev_day = None
    try:
        store = {"prev": None, "today": None}
        if os.path.exists(INTRADAY_SNAP_PATH):
            with open(INTRADAY_SNAP_PATH, "r", encoding="utf-8") as f:
                old = json.load(f)
            if isinstance(old, dict) and ("today" in old or "prev" in old):
                store = {"prev": old.get("prev"), "today": old.get("today")}
            else:  # 旧版单快照格式兼容
                store["today"] = old
        # 跨日：旧 today 不是今天 → 它就是“前一日”数据，转入 prev
        if store["today"] and store["today"].get("date") != today_key:
            store["prev"] = store["today"]
        if store["prev"] and store["prev"].get("date") != today_key:
            prev_day = store["prev"]
        if industries:
            store["today"] = {
                "date": today_key,
                "time": data["time"],
                "total_amount": data["total_amount"],
                "industries": [
                    {"name": e["name"], "amount": e["amount"], "share": e["share"],
                     "change_pct": e.get("change_pct")}
                    for e in industries
                ],
            }
            tmp = INTRADAY_SNAP_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(store, f, ensure_ascii=False)
            os.replace(tmp, INTRADAY_SNAP_PATH)
    except Exception:
        pass
    data["prev_day"] = prev_day

    _INTRADAY_CACHE["ts"] = now
    _INTRADAY_CACHE["data"] = data
    return data


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/weibo.html")
            self.end_headers()
            return
        if path == "/api/config":
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self._send_json(cfg)
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        if path == "/api/index-futures":
            try:
                self._send_json(load_index_futures())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        if path == "/api/intraday":
            try:
                self._send_json(load_intraday())
            except Exception as e:
                self._send_json({"error": str(e)}, status=500)
            return
        super().do_GET()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/api/save-config":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send_json({"ok": False, "error": "请求体不是合法 JSON: " + str(e)}, status=400)
                return
            ok, err = validate_config(data)
            if not ok:
                self._send_json({"ok": False, "error": err}, status=400)
                return
            data = normalize_config(data)
            try:
                if os.path.exists(CONFIG_PATH):
                    shutil.copy2(CONFIG_PATH, CONFIG_BAK)
                tmp = CONFIG_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, CONFIG_PATH)  # 原子替换，避免读到半成品
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"ok": False, "error": "写入失败: " + str(e)}, status=500)
            return
        if path == "/api/index-futures":
            try:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                data = json.loads(raw.decode("utf-8"))
            except Exception as e:
                self._send_json({"ok": False, "error": "请求体不是合法 JSON: " + str(e)}, status=400)
                return
            ok, err = validate_index_futures(data)
            if not ok:
                self._send_json({"ok": False, "error": err}, status=400)
                return
            data = normalize_index_futures(data)
            try:
                if os.path.exists(INDEX_FUTURES_PATH):
                    shutil.copy2(INDEX_FUTURES_PATH, INDEX_FUTURES_BAK)
                tmp = INDEX_FUTURES_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, INDEX_FUTURES_PATH)  # 原子替换
                self._send_json({"ok": True, "data": data})
            except Exception as e:
                self._send_json({"ok": False, "error": "写入失败: " + str(e)}, status=500)
            return
        self.send_response(404)
        self.end_headers()

    def end_headers(self):
        # 禁止缓存，保证页面数据实时刷新
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class ReusableTCPServer(socketserver.ThreadingTCPServer):
    # 必须在构造(bind)前设置，否则端口处于 TIME_WAIT 时重启会报 Address already in use
    allow_reuse_address = True
    daemon_threads = True  # 请求各自一线程，避免长抓取阻塞其他请求


def validate_config(data):
    """校验前端提交的整体配置；返回 (ok, error_msg)。"""
    if not isinstance(data, dict):
        return False, "配置必须是 JSON 对象"
    users = data.get("users")
    if not isinstance(users, list):
        return False, "users 必须是数组"
    for i, u in enumerate(users):
        if not isinstance(u, dict):
            return False, f"users[{i}] 必须是对象"
        if u.get("platform") not in ("weibo", "xueqiu"):
            return False, f"users[{i}].platform 必须是 weibo 或 xueqiu"
        if not str(u.get("uid", "")).strip():
            return False, f"users[{i}].uid 不能为空"
        if not str(u.get("name", "")).strip():
            return False, f"users[{i}].name 不能为空"
    industries = data.get("industries")
    if not isinstance(industries, list):
        return False, "industries 必须是数组"
    for i, ind in enumerate(industries):
        if not isinstance(ind, dict):
            return False, f"industries[{i}] 必须是对象"
        if not str(ind.get("name", "")).strip():
            return False, f"industries[{i}].name 不能为空"
        stocks = ind.get("stocks")
        if not isinstance(stocks, list):
            return False, f"industries[{i}].stocks 必须是数组"
        for j, s in enumerate(stocks):
            if not isinstance(s, dict):
                return False, f"industries[{i}].stocks[{j}] 必须是对象"
            if not str(s.get("url", "")).strip():
                return False, f"industries[{i}].stocks[{j}].url 不能为空"
            if not str(s.get("name", "")).strip():
                return False, f"industries[{i}].stocks[{j}].name 不能为空"
    for k in ("peak_interval_sec", "offpeak_interval_sec", "frontend_poll_interval_sec"):
        v = data.get(k)
        if v is not None and (not isinstance(v, (int, float)) or v <= 0):
            return False, f"{k} 必须为正数"
    return True, ""


def normalize_config(data):
    """补齐监控脚本依赖的默认字段，避免保存后出现缺字段导致崩溃。"""
    data.setdefault("pages_per_user", 2)
    data.setdefault("frontend_poll_interval_sec", 30)
    for u in (data.get("users") or []):
        u.setdefault("filter", False)
        u.setdefault("display_limit", 30)
        u.setdefault("fetch_pages", 2)
        if u.get("platform") == "weibo":
            u.setdefault("comment_scan_posts", 8)
    for ind in (data.get("industries") or []):
        ind.setdefault("icon", "🏭")
        ind.setdefault("days", 7)
        if not ind.get("id"):
            ind["id"] = re.sub(r"[^a-z0-9_]", "_", str(ind.get("name", "ind")).lower())
        for s in (ind.get("stocks") or []):
            s.setdefault("note", "")
    return data


def load_index_futures():
    """读取股指期货多空单存储；文件缺失/损坏时返回默认结构。"""
    if os.path.exists(INDEX_FUTURES_PATH):
        try:
            with open(INDEX_FUTURES_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"institutions": ["中信期货", "其他大机构"], "records": []}


def validate_index_futures(data):
    """校验前端提交的股指期货存储；返回 (ok, error_msg)。"""
    if not isinstance(data, dict):
        return False, "必须是 JSON 对象"
    insts = data.get("institutions")
    if not isinstance(insts, list) or not insts:
        return False, "institutions 必须是非空数组"
    for n in insts:
        if not str(n).strip():
            return False, "机构名称不能为空"
    recs = data.get("records")
    if not isinstance(recs, list):
        return False, "records 必须是数组"
    date_re = re.compile(r"^\d{4}-\d{2}-\d{2}$")
    for i, r in enumerate(recs):
        if not isinstance(r, dict):
            return False, f"records[{i}] 必须是对象"
        if not date_re.match(str(r.get("date", ""))):
            return False, f"records[{i}].date 格式应为 YYYY-MM-DD"
        pos = r.get("positions")
        if not isinstance(pos, dict):
            return False, f"records[{i}].positions 必须是对象"
        for k, v in pos.items():
            if not isinstance(v, dict):
                return False, f"records[{i}].positions[{k}] 必须是对象"
            for fld in ("long", "short"):
                val = v.get(fld)
                if val is not None and (not isinstance(val, int) or isinstance(val, bool) or val < 0):
                    return False, f"records[{i}].positions[{k}].{fld} 必须为非负整数"
        sh = r.get("sh_index")
        if sh is not None and (not isinstance(sh, (int, float)) or isinstance(sh, bool) or sh <= 0):
            return False, f"records[{i}].sh_index 必须为正数（上证指数收盘点位）"
        for fld in ("net_short_change", "net_short_cumulative"):
            d = r.get(fld)
            if d is not None and not isinstance(d, dict):
                return False, f"records[{i}].{fld} 必须是对象"
            if isinstance(d, dict):
                for k, v in d.items():
                    if v is not None and not isinstance(v, int):
                        return False, f"records[{i}].{fld}[{k}] 必须是整数"
    return True, ""


def normalize_index_futures(data):
    """补齐默认字段并清洗 positions / net_short 字段：仅保留已知机构，缺失补 null。"""
    data.setdefault("institutions", ["中信期货", "其他大机构"])
    data.setdefault("records", [])
    insts = data["institutions"]
    for r in data["records"]:
        r.setdefault("source_url", "")
        r.setdefault("note", "")
        sh = r.get("sh_index")
        r["sh_index"] = sh if isinstance(sh, (int, float)) and not isinstance(sh, bool) else None
        pos = r.get("positions") or {}
        cleaned = {}
        for n in insts:
            p = pos.get(n)
            if isinstance(p, dict):
                cleaned[n] = {"long": p.get("long"), "short": p.get("short")}
            else:
                cleaned[n] = {"long": None, "short": None}
        r["positions"] = cleaned
        # 同步 positions 到 net_short_cumulative（手动录入优先）
        r.setdefault("net_short_change", {})
        r.setdefault("net_short_cumulative", {})
        for n in insts:
            p = cleaned.get(n) or {}
            if p.get("long") is not None and p.get("short") is not None:
                r["net_short_cumulative"][n] = int(p["long"]) - int(p["short"])
    # 记录按日期升序，便于前端与趋势计算
    data["records"].sort(key=lambda r: str(r.get("date", "")))
    return data


def main():
    parser = argparse.ArgumentParser(description="微博监控前端服务")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    # 新部署时从 snapshot/ 恢复长周期历史数据（仅补缺失，不覆盖）
    try:
        import snapshot
        snapshot.restore()
    except Exception as e:
        print(f"⚠️ 快照恢复跳过: {e}")

    os.chdir(FRONTEND_DIR)
    with ReusableTCPServer(("", args.port), Handler) as httpd:
        print(f"📡 微博监控页面: http://localhost:{args.port}/weibo.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 服务已停止")


if __name__ == "__main__":
    main()
