"""
雪球采集器 — Playwright 持久浏览器 + 页面上下文内 fetch API

原理与微博采集器一致:
1. launch_persistent_context 持久化 cookie（雪球匿名访问首页即可获得
   xq_a_token，通常无需扫码登录）
2. page.evaluate(fetch) 在页面上下文调用雪球 JSON 接口，天然携带 cookie
3. 雪球接口响应可能为 {body, error, status} 包装结构，需解包取 body
4. 所有请求带超时保护：雪球风控敏感，触发滑块/限流时降级跳过而非挂死

用法:
    from xueqiu_collector import collect_users
    result = asyncio.run(collect_users([{"uid": "9089343523"}]))
"""
import asyncio
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "data", "xueqiu_profile")

DESKTOP_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")

FETCH_TIMEOUT = 20  # 单次接口请求超时（秒）

# 页面上下文内 fetch（带 cookie），失败返回 {error} 而非抛异常
JS_FETCH_JSON = """async (url) => {
  try {
    const r = await fetch(url, {credentials: "include",
      headers: {"X-Requested-With": "XMLHttpRequest"}});
    const t = await r.text();
    return JSON.parse(t);
  } catch (e) {
    return {error: String(e)};
  }
}"""


# ============================ 基础工具 ============================

def strip_html(text: str) -> str:
    """去HTML标签，保留纯文本"""
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return text.strip()


def parse_ts(ms) -> str:
    """雪球 created_at 为毫秒时间戳，转为本地时间字符串"""
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def unwrap(data):
    """雪球接口可能返回 {body, error, status} 包装结构，解包取真实数据"""
    if isinstance(data, dict) and "body" in data and (
            "status" in data or "error" in data):
        return data.get("body")
    return data


async def safe_fetch(page, url: str):
    """带超时保护的接口请求，超时/异常返回 None"""
    try:
        return await asyncio.wait_for(page.evaluate(JS_FETCH_JSON, url),
                                      timeout=FETCH_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"    ⚠️ 请求超时(可能被风控): {url.split('?')[0]}")
        return None
    except Exception as e:
        print(f"    ⚠️ 请求异常: {e}")
        return None


# ============================ 数据解析 ============================

def extract_status(status: Dict, uid: str, author: str) -> Dict:
    """把雪球 status 结构转为与微博统一的标准帖子"""
    sid = str(status.get("id"))
    text = strip_html(status.get("text") or status.get("description") or "")

    pics = []
    pic = status.get("pic")
    if pic:
        pics.append(pic if pic.startswith("http") else f"https:{pic}")

    rt = status.get("retweeted_status")
    retweet_text = ""
    if rt:
        rt_user = ((rt.get("user") or {}).get("screen_name")) or "已注销"
        retweet_text = f"@{rt_user}: {strip_html(rt.get('text') or rt.get('description') or '')}"

    target = status.get("target") or ""
    url = (f"https://xueqiu.com{target}" if target
           else f"https://xueqiu.com/statuses/{sid}")

    return {
        "id": f"xq_{sid}",  # 加前缀避免与微博 mid 冲突
        "type": "post",
        "platform": "xueqiu",
        "uid": uid,
        "author": author,
        "text": text,
        "created_at": parse_ts(status.get("created_at")),
        "source": "雪球",
        "reposts": status.get("retweet_count", 0),
        "comments": status.get("reply_count", 0),
        "likes": status.get("like_count", 0),
        "pics": pics,
        "is_retweet": bool(rt),
        "retweet_text": retweet_text,
        "url": url,
        "collected_at": datetime.now().isoformat(),
    }


async def fetch_user_info(page, uid: str) -> Optional[Dict]:
    """获取雪球用户信息"""
    data = await safe_fetch(page, f"https://xueqiu.com/users/show.json?id={uid}")
    body = unwrap(data)
    if isinstance(body, list):  # 接口有时返回数组
        body = body[0] if body else None
    if not isinstance(body, dict) or not body.get("id"):
        return None
    name = body.get("screen_name") or body.get("name") or uid
    return {
        "uid": uid,
        "name": name,
        "avatar": body.get("profile_image_url", ""),
        "followers": body.get("followers_count", 0),
        "description": body.get("description", ""),
        "verified": "认证" if body.get("verified") else "",
        "profile_url": f"https://xueqiu.com/u/{uid}",
    }


async def fetch_user_posts(page, uid: str, author: str,
                           pages: int = 2) -> List[Dict]:
    """拉取用户最新发言（每页约10-20条）"""
    posts = []
    for page_no in range(1, pages + 1):
        url = (f"https://xueqiu.com/v4/statuses/user_timeline.json"
               f"?page={page_no}&user_id={uid}")
        data = await safe_fetch(page, url)
        if data is None:
            break
        if isinstance(data, dict) and data.get("error"):
            print(f"    ⚠️ 第{page_no}页返回错误: {str(data.get('error_description') or data.get('error'))[:80]}")
            break
        body = unwrap(data)
        if isinstance(body, dict):
            items = body.get("statuses") or body.get("list") or []
        elif isinstance(body, list):
            items = body
        else:
            items = []
        if not items:
            break
        for it in items:
            posts.append(extract_status(it, uid, author))
        await asyncio.sleep(3)  # 雪球风控敏感，间隔放宽
    return posts


# ============================ 数据采集 ============================

async def launch_context(p, headless: bool = True):
    os.makedirs(PROFILE_DIR, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        user_agent=DESKTOP_UA,
        viewport={"width": 1280, "height": 800},
        args=["--disable-blink-features=AutomationControlled"],
    )


async def check_risk(page) -> bool:
    """检测是否触发滑块验证页"""
    try:
        title = await page.title()
        return "Verification" in title or "验证" in title
    except Exception:
        return False


async def collect_users(users: List[Dict], pages: int = 2,
                        headless: bool = True,
                        cfg: Optional[Dict] = None) -> Dict[str, Dict]:
    """
    采集多个雪球用户。
    返回: {uid: {"user": 用户信息, "posts": [帖子...], "own_comments": []}}
    """
    result = {}
    async with async_playwright() as p:
        context = await launch_context(p, headless=headless)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://xueqiu.com/", wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(4)
            if await check_risk(page):
                print("  ⚠️ 雪球触发滑块风控，本轮降级跳过")
                return result

            for u in users:
                uid = str(u["uid"])
                print(f"  [雪球] 采集 {u.get('name') or uid} ...")
                info = await fetch_user_info(page, uid)
                await asyncio.sleep(2)
                if not info:
                    print(f"    ❌ 跳过（用户信息获取失败，可能被限流）")
                    result[uid] = {"user": None, "posts": [], "own_comments": []}
                    continue
                if not u.get("name") and info["name"]:
                    u["name"] = info["name"]
                posts = await fetch_user_posts(
                    page, uid, info["name"],
                    pages=int(u.get("fetch_pages") or pages))
                result[uid] = {"user": info, "posts": posts, "own_comments": []}
                print(f"    ✅ {info['name']} 粉丝{info['followers']} "
                      f"最新 {len(posts)} 条")
                await asyncio.sleep(3)
        finally:
            await context.close()
    return result
