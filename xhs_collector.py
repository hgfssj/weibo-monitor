"""
小红书采集器 — Playwright 持久浏览器 + 用户主页滚动 + 接口响应拦截

原理:
1. launch_persistent_context 持久化 cookie（首次若需登录，运行
   `python xhs_collector.py --login` 在可见窗口扫码，登录态会保存复用）
2. 打开用户主页 https://www.xiaohongshu.com/user/profile/{uid}，
   页面自身 JS 会发带签名的 /api/sns/web/v1/user_posted 请求，
   采集器拦截这些响应获取笔记列表（无需自行伪造 x-s 签名）
3. 滚动触发分页加载，连续多轮无新笔记或达到上限即停止
4. 用户信息从 window.__INITIAL_STATE__ 解析

用法:
    from xhs_collector import collect_users
    result = asyncio.run(collect_users([{"uid": "95302462712"}]))

    python xhs_collector.py --login            # 可见窗口扫码登录
    python xhs_collector.py --uid 95302462712  # 单用户试采集
"""
import asyncio
import json
import os
import random
import time
from datetime import datetime
from typing import Dict, List, Optional

from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "data", "xhs_profile")

DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

PROFILE_URL = "https://www.xiaohongshu.com/user/profile/{uid}"

MAX_NOTES = 300          # 单用户单次采集笔记上限（“全部内容”的保险丝）
NO_NEW_STOP = 3          # 连续无新笔记的滚动次数 → 视为到底

# 隐身脚本：修复 headless 指纹漏洞（小红书反爬敏感，不隐身会被强弹登录）
STEALTH_SCRIPTS = [
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});",
    "window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}, app: {}};",
    """Object.defineProperty(navigator, 'plugins', {get: () => [
        {name:'Chrome PDF Plugin', filename:'internal-pdf-viewer'},
        {name:'Chrome PDF Viewer', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},
        {name:'Native Client', filename:'internal-nacl-plugin'}
    ]});""",
    "Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN','zh','en-US','en']});",
    "Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 4});",
    "Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});",
]

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-popup-blocking",
    "--disable-sync",
    "--no-default-browser-check",
    "--mute-audio",
]


# ============================ 数据解析 ============================

def parse_ts(sec) -> str:
    """user_posted 的 time 为秒级时间戳字符串"""
    try:
        return datetime.fromtimestamp(int(sec)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def _cover_url(note: Dict) -> str:
    cover = note.get("cover") or {}
    url = cover.get("url") or ""
    if not url:
        infos = cover.get("info_list") or []
        url = infos[0].get("url", "") if infos else ""
    if url and url.startswith("//"):
        url = "https:" + url
    return url


def note_to_post(note: Dict, uid: str, author: str) -> Dict:
    """把 user_posted 的笔记结构转为与微博/雪球统一的标准帖子"""
    nid = note.get("note_id") or ""
    text = (note.get("title") or note.get("display_title")
            or note.get("desc") or "").strip()
    return {
        "id": f"xhs_{nid}",
        "type": "post",
        "platform": "xhs",
        "uid": uid,
        "author": author,
        "text": text or "[小红书笔记]",
        "created_at": parse_ts(note.get("time") or note.get("timestamp") or 0),
        "source": "小红书",
        "reposts": 0,
        "comments": 0,
        "likes": int(note.get("likes") or note.get("liked_count") or 0),
        "pics": ([_cover_url(note)] if _cover_url(note) else []),
        "is_retweet": False,
        "retweet_text": "",
        "url": f"https://www.xiaohongshu.com/user/profile/{uid}/{nid}",
        "note_type": note.get("type") or "",   # normal=图文 / video=视频
        "collected_at": datetime.now().isoformat(),
    }


JS_USER_INFO = """() => {
  // 小红书 SSR 状态被包成 Vue ref，需递归取 _value
  const unref = v => { for (let i = 0; i < 3 && v && typeof v === "object" && v.__v_isRef; i++) v = v._value; return v; };
  const s = window.__INITIAL_STATE__;
  const u = unref(s && s.user);
  if (!u) return null;
  const pd = unref(u.userPageData) || {};
  const b = unref(pd.basicInfo) || pd;
  const inter = unref(pd.interactions) || [];
  const fansIt = inter.find(i => String((i && i.name) || (i && i.type) || "").includes("粉丝"));
  const fans = fansIt ? unref(fansIt.count) : null;
  return {
    name: unref(b.nickname || b.nick) || "",
    avatar: unref(b.imageb || b.image) || "",
    desc: unref(b.desc) || "",
    red_id: unref(b.redId || b.red_id) || "",
    fans: fans != null ? String(fans) : "0",
  };
}"""

JS_DOM_NOTES = """() => {
  // DOM 兵底：SSR 渲染的笔记卡片（无 time 字段）
  return [...document.querySelectorAll('section.note-item')].map(el => {
    const a = el.querySelector('a[href]');
    const href = a ? a.getAttribute('href') : '';
    const m = href.match(/([0-9a-f]{24})/);
    const like = (el.querySelector('.like-wrapper span, .count') || {}).textContent || '0';
    return {
      note_id: m ? m[1] : '',
      title: ((el.querySelector('.title') || {}).textContent || '').trim(),
      likes: like.replace(/[^0-9]/g, '') || '0',
      href,
    };
  }).filter(x => x.note_id);
}"""


# ============================ 采集 ============================

async def launch_context(p, headless: bool = True):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    context = await p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        user_agent=DESKTOP_UA,
        viewport={"width": 1280, "height": 800},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=LAUNCH_ARGS,
    )
    for script in STEALTH_SCRIPTS:
        await context.add_init_script(script)
    return context


async def check_risk(page) -> bool:
    """检测风控/登录拦截页"""
    try:
        html = (await page.content())[:3000]
        return ("安全限制" in html or "300012" in html
                or ("登录" in html and "手机号" in html))
    except Exception:
        return False


async def resolve_user_id(page, red_id: str) -> Optional[str]:
    """小红书号(red_id) → 主页 URL 所需的 hex user_id。
    通过搜索页输入小红书号，拦截 usersearch 接口响应拿到 user_id；
    若 uid 本身已是 hex(24位16进制)则直接返回原值。"""
    if len(red_id) == 24 and all(c in "0123456789abcdef" for c in red_id.lower()):
        return red_id

    found: Dict[str, str] = {}

    async def on_response(resp):
        if "usersearch" not in resp.url and "user/search" not in resp.url:
            return
        try:
            body = await resp.json()
        except Exception:
            return
        for it in ((body.get("data") or {}).get("user_list") or []):
            info = (it.get("user_info") or it.get("userinfo") or it) or {}
            uid_hex = info.get("user_id") or info.get("id") or ""
            rid = str(info.get("red_id") or "")
            nick = info.get("nickname") or ""
            # 严格匹配 red_id，避免误取搜索结果第一条（可能是登录账号本人）
            if uid_hex and rid == red_id:
                found[uid_hex] = nick

    page.on("response", on_response)
    try:
        await page.goto("https://www.xiaohongshu.com/search_result?keyword="
                        + red_id + "&type=54",
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)
        # 部分版本需点击「用户」tab 才触发 usersearch
        try:
            await page.click("text=用户", timeout=3000)
            await asyncio.sleep(4)
        except Exception:
            pass
        if not found:
            # 兜底：从 SSR/DOM 的用户卡片链接提取 hex user_id
            m = await page.evaluate("""() => {
                const a = document.querySelector('a[href*="/user/profile/"]');
                const href = a ? a.getAttribute('href') : '';
                const mm = href.match(/([0-9a-f]{24})/);
                return mm ? mm[1] : '';
            }""")
            if m:
                return m
        return next(iter(found)) if found else None
    finally:
        page.remove_listener("response", on_response)


async def fetch_notes_via_api(page, context, uid: str, max_notes: int) -> List[Dict]:
    """直接调 user_posted 接口：借用页面自带签名函数 window._webmsxyw
    生成 x-s/x-t 头（无需自行实现签名），cursor 翻页拉全量。
    适用场景：headless 下主页 SPA hydration 失败、滚动不触发请求时。"""
    notes: Dict[str, Dict] = {}
    cursor = ""
    for _ in range(30):
        req_data = {"num": 30, "cursor": cursor, "user_id": uid,
                    "image_formats": ["jpg", "webp", "avif"]}
        # 签名与请求均在页面内完成：签名原文(JSON.stringify)与发送 body
        # 必须完全一致，且页面 fetch 会被站点拦截器补全 x-s-common 等头
        try:
            body = await page.evaluate("""async (d) => {
                if (!window._webmsxyw) return {err: 'no_sign'};
                const bodyStr = JSON.stringify(d);
                const s = await window._webmsxyw('/api/sns/web/v1/user_posted', bodyStr);
                const r = await fetch('/api/sns/web/v1/user_posted', {
                    method: 'POST',
                    headers: {'content-type': 'application/json;charset=UTF-8',
                              'x-s': s['X-s'], 'x-t': String(s['X-t'])},
                    body: bodyStr,
                });
                if (!r.ok) return {err: 'http_' + r.status};
                return await r.json();
            }""", req_data)
        except Exception as e:
            print(f"    ⚠️ user_posted 调用异常: {e}")
            break
        if not body or body.get("err"):
            print(f"    ⚠️ user_posted 失败: {body.get('err') if body else 'empty'}")
            break
        data = body.get("data") or {}
        batch = data.get("notes") or []
        if not batch:
            break
        for n in batch:
            nid = n.get("note_id")
            if nid:
                notes[nid] = n
        if not data.get("has_more") or len(notes) >= max_notes:
            break
        cursor = data.get("cursor") or ""
        await asyncio.sleep(random.uniform(1.5, 3.0))
    return sorted(notes.values(),
                  key=lambda n: int(n.get("time") or 0), reverse=True)


async def fetch_user_notes(page, uid: str, max_scrolls: int) -> List[Dict]:
    """打开主页 + 滚动分页，拦截 user_posted 响应收集全部笔记"""
    captured: Dict[str, Dict] = {}

    async def on_response(resp):
        if "user_posted" not in resp.url:
            return
        try:
            body = await resp.json()
        except Exception:
            return
        for n in ((body.get("data") or {}).get("notes") or []):
            nid = n.get("note_id")
            if nid:
                captured[nid] = n

    page.on("response", on_response)
    try:
        await page.goto(PROFILE_URL.format(uid=uid),
                        wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(4)

        no_new, scrolls = 0, 0
        while no_new < NO_NEW_STOP and len(captured) < MAX_NOTES \
                and scrolls < max_scrolls:
            prev = len(captured)
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(random.uniform(2.0, 3.5))  # 人类节奏，防风控
            scrolls += 1
            no_new = no_new + 1 if len(captured) == prev else 0

        # DOM 兵底：接口拦截为空但 SSR 渲染了卡片时（如登录态下分页被限）
        if not captured:
            try:
                for n in await page.evaluate(JS_DOM_NOTES):
                    captured.setdefault(n["note_id"], n)
            except Exception:
                pass
    finally:
        page.remove_listener("response", on_response)

    notes = sorted(captured.values(),
                   key=lambda n: int(n.get("time") or 0), reverse=True)
    return notes


async def collect_users(users: List[Dict], pages: int = 2,
                        headless: bool = True,
                        cfg: Optional[Dict] = None) -> Dict[str, Dict]:
    """
    采集多个小红书用户。
    返回: {uid: {"user": 用户信息, "posts": [帖子...], "own_comments": []}}
    """
    result = {}
    async with async_playwright() as p:
        context = await launch_context(p, headless=headless)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            # 先访问首页种 cookie
            await page.goto("https://www.xiaohongshu.com/explore",
                            wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(3)

            for u in users:
                uid = str(u["uid"])
                print(f"  [小红书] 采集 {u.get('name') or uid} ...")
                # 配置里的可能是小红书号(red_id)，先解析成 hex user_id
                real_uid = uid
                try:
                    resolved = await resolve_user_id(page, uid)
                    if resolved and resolved != uid:
                        print(f"    🔎 小红书号 {uid} → user_id {resolved}")
                        real_uid = resolved
                    elif not resolved:
                        print(f"    ⚠️ 未解析到 user_id，按原始 uid 尝试")
                except Exception as e:
                    print(f"    ⚠️ user_id 解析异常: {e}")
                # 打开主页 + 滚动分页（__INITIAL_STATE__ 需先进主页才能解析）
                max_scrolls = int(u.get("fetch_pages") or pages) * 10
                notes = await fetch_user_notes(page, real_uid, max_scrolls)
                # 兜底：headless 下 SPA hydration 失败时滚动不触发请求，
                # 改用页面自带签名函数直接调 user_posted 接口拉全量
                if not notes:
                    try:
                        notes = await fetch_notes_via_api(
                            page, context, real_uid, MAX_NOTES)
                        if notes:
                            print(f"    🔧 接口直连兜底取到 {len(notes)} 条")
                    except Exception as e:
                        print(f"    ⚠️ 接口兜底失败: {e}")
                info_raw = None
                try:
                    info_raw = await page.evaluate(JS_USER_INFO)
                except Exception as e:
                    print(f"    ⚠️ 用户信息解析失败: {e}")

                if not notes and not info_raw:
                    if await check_risk(page):
                        print("    ⚠️ 小红书触发风控/需登录，本轮降级跳过"
                              "（可运行 python xhs_collector.py --login 扫码）")
                    else:
                        print("    ❌ 跳过（未取到任何数据）")
                    result[uid] = {"user": None, "posts": [], "own_comments": []}
                    continue

                info = {
                    "uid": uid,
                    "name": (info_raw or {}).get("name") or u.get("name") or uid,
                    "avatar": (info_raw or {}).get("avatar", ""),
                    "followers": int((info_raw or {}).get("fans") or 0),
                    "description": (info_raw or {}).get("desc", ""),
                    "verified": "",
                    "profile_url": PROFILE_URL.format(uid=real_uid),
                }
                if not u.get("name") and info["name"]:
                    u["name"] = info["name"]
                posts = [note_to_post(n, real_uid, info["name"]) for n in notes]
                result[uid] = {"user": info, "posts": posts, "own_comments": []}
                print(f"    ✅ {info['name']} 粉丝{info['followers']} "
                      f"最新 {len(posts)} 条笔记")
                await asyncio.sleep(3)
        finally:
            await context.close()
    return result


# ============================ CLI ============================

async def login_flow(timeout: int = 180):
    """可见窗口打开小红书，轮询 cookie 等待用户扫码登录，登录态存入 PROFILE_DIR"""
    async with async_playwright() as p:
        context = await launch_context(p, headless=False)
        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto("https://www.xiaohongshu.com/explore",
                        wait_until="domcontentloaded", timeout=30000)
        print("🔐 请在弹出的浏览器窗口用小红书 App 扫码登录，"
              f"登录成功后自动关闭（最多等待 {timeout} 秒）...")
        ok = False
        for _ in range(timeout // 2):
            await asyncio.sleep(2)
            # 注意：小红书游客态也下发 web_session cookie，不能用它判断；
            # 以 SSR 状态 userInfo.guest === false 为准
            try:
                logged = await page.evaluate("""() => {
                  const s = window.__INITIAL_STATE__;
                  const unref = v => { for (let i=0;i<3&&v&&typeof v==='object'&&v.__v_isRef;i++) v=v._value; return v; };
                  const ui = unref(s && s.user && s.user.userInfo) || {};
                  return !!ui.userId && ui.guest === false;
                }""")
            except Exception:
                logged = False
            if logged:
                ok = True
                await asyncio.sleep(3)  # 等 cookie 落盘
                break
        await context.close()
    if ok:
        print("✅ 登录态已保存，后续 headless 采集将复用")
    else:
        print("❌ 超时未检测到登录，可重跑 --login")


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="小红书采集器")
    parser.add_argument("--login", action="store_true", help="可见窗口扫码登录")
    parser.add_argument("--uid", type=str, help="单用户试采集")
    args = parser.parse_args()

    if args.login:
        asyncio.run(login_flow())
    elif args.uid:
        out = asyncio.run(collect_users([{"uid": args.uid, "fetch_pages": 5}],
                                        headless=True))
        for uid, data in out.items():
            print(json.dumps({"user": data["user"],
                              "post_count": len(data["posts"]),
                              "first_posts": data["posts"][:5]},
                             ensure_ascii=False, indent=2))
    else:
        parser.print_help()
