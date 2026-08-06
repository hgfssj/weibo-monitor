"""
微博大V采集器 — Playwright 持久登录态版
- 首次运行：弹出浏览器窗口，扫码登录（登录态保存在 data/weibo_profile/）
- 后续运行：自动复用登录态，直接调用 m.weibo.cn API 拉取微博正文

用法:
    python weibo_collector.py          # 采集配置中所有大V
    python weibo_collector.py --login  # 仅登录/刷新登录态
"""
import asyncio
import html as html_mod
import json
import os
import re
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from playwright.async_api import async_playwright

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE_DIR = os.path.join(BASE_DIR, "data", "weibo_profile")
CONFIG_FILE = os.path.join(BASE_DIR, "weibo_config.json")

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

# 在页面上下文内 fetch API（自带登录 Cookie，绕开 requests 的 432 风控）
JS_FETCH_JSON = """
async (url) => {
    const resp = await fetch(url, {
        credentials: 'include',
        headers: {
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
            'MWeibo-Pwa': '1',
        }
    });
    return await resp.json();
}
"""


# ============================ 配置 ============================

def load_config() -> Dict:
    if not os.path.exists(CONFIG_FILE):
        return {"users": []}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg: Dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


# ============================ 文本解析 ============================

def strip_html(text: str) -> str:
    """去掉微博正文中的 HTML 标签，保留可读文本"""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html_mod.unescape(text).strip()


def parse_created_at(raw: str) -> str:
    """微博时间格式 → ISO 字符串。兼容绝对时间和相对时间（如 "5分钟前"）"""
    if not raw:
        return ""
    now = datetime.now()
    m = re.search(r"(\d+)秒前", raw)
    if m:
        return (now - timedelta(seconds=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)分钟前", raw)
    if m:
        return (now - timedelta(minutes=int(m.group(1)))).isoformat()
    m = re.search(r"(\d+)小时前", raw)
    if m:
        return (now - timedelta(hours=int(m.group(1)))).isoformat()
    m = re.match(r"今天\s*(\d{2}):(\d{2})", raw)
    if m:
        return now.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                           second=0, microsecond=0).isoformat()
    m = re.match(r"昨天\s*(\d{2}):(\d{2})", raw)
    if m:
        d = now - timedelta(days=1)
        return d.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                         second=0, microsecond=0).isoformat()
    m = re.match(r"(\d{1,2})月(\d{1,2})日", raw)
    if m:
        return now.replace(month=int(m.group(1)), day=int(m.group(2)),
                           hour=0, minute=0, second=0, microsecond=0).isoformat()
    # 绝对格式: "Wed Aug 05 12:00:00 +0800 2026"
    try:
        dt = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone().replace(tzinfo=None).isoformat()
    except ValueError:
        return raw


def extract_post(mblog: Dict, uid: str, author: str) -> Dict:
    """把 API 返回的 mblog 结构转为标准帖子"""
    pics = []
    for p in mblog.get("pics") or []:
        url = (p.get("large") or {}).get("url") or p.get("url")
        if url:
            pics.append(url)

    retweet = mblog.get("retweeted_status")
    retweet_text = ""
    if retweet:
        rt_user = (retweet.get("user") or {}).get("screen_name", "已注销")
        retweet_text = f"@{rt_user}: {strip_html(retweet.get('text', ''))}"

    mid = str(mblog.get("mid") or mblog.get("id"))
    return {
        "id": mid,
        "mid": mid,
        "uid": uid,
        "author": author,
        "text": strip_html(mblog.get("text", "")),
        "created_at": parse_created_at(mblog.get("created_at", "")),
        "source": strip_html(mblog.get("source", "")),
        "reposts": mblog.get("reposts_count", 0),
        "comments": mblog.get("comments_count", 0),
        "likes": mblog.get("attitudes_count", 0),
        "pics": pics,
        "is_retweet": bool(retweet),
        "retweet_text": retweet_text,
        "url": f"https://m.weibo.cn/detail/{mid}",
        "collected_at": datetime.now().isoformat(),
    }


# ============================ 浏览器 / 登录 ============================

async def launch_context(p, headless: bool):
    """启动持久化浏览器上下文（登录态存盘）"""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    context = await p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        user_agent=MOBILE_UA,
        viewport={"width": 414, "height": 896},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        device_scale_factor=1,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
            "--no-default-browser-check",
        ],
    )
    return context


async def is_logged_in(context) -> bool:
    cookies = await context.cookies()
    return any(c["name"] == "SUB" and "weibo" in c.get("domain", "")
               for c in cookies)


async def ensure_login(context, timeout_sec: int = 300) -> bool:
    """确保已登录。未登录时打开登录页等待扫码"""
    if await is_logged_in(context):
        return True

    page = context.pages[0] if context.pages else await context.new_page()
    await page.goto("https://passport.weibo.cn/signin/login?entry=mweibo"
                    "&url=https%3A%2F%2Fm.weibo.cn%2F",
                    wait_until="domcontentloaded", timeout=30000)
    print("\n" + "=" * 55)
    print("  📱 首次使用：请在弹出的浏览器窗口中扫码登录微博")
    print(f"     （等待最长 {timeout_sec} 秒...）")
    print("=" * 55 + "\n")

    deadline = asyncio.get_event_loop().time() + timeout_sec
    while asyncio.get_event_loop().time() < deadline:
        if await is_logged_in(context):
            print("  ✅ 登录成功，登录态已保存\n")
            return True
        await asyncio.sleep(2)

    print("  ❌ 登录超时")
    return False


# ============================ 数据采集 ============================

async def fetch_user_info(page, uid: str) -> Optional[Dict]:
    """获取用户基本信息（昵称/粉丝数/简介）"""
    url = f"https://m.weibo.cn/api/container/getIndex?type=uid&value={uid}"
    try:
        data = await page.evaluate(JS_FETCH_JSON, url)
    except Exception as e:
        print(f"    ⚠️ 用户信息请求失败: {e}")
        return None
    if data.get("ok") != 1:
        print(f"    ⚠️ 用户信息接口返回异常: {str(data)[:120]}")
        return None
    info = data["data"].get("userInfo", {})
    return {
        "uid": uid,
        "name": info.get("screen_name", ""),
        "description": strip_html(info.get("description", "")),
        "followers": info.get("followers_count", 0),
        "statuses_count": info.get("statuses_count", 0),
        "avatar": info.get("profile_image_url", ""),
        "profile_url": f"https://weibo.com/u/{uid}",
        "verified": info.get("verified_reason", ""),
    }


async def fetch_user_posts(page, uid: str, author: str, pages: int = 2) -> List[Dict]:
    """拉取用户最新微博（默认前2页 ≈ 20条）"""
    posts = []
    for page_no in range(1, pages + 1):
        url = (f"https://m.weibo.cn/api/container/getIndex"
               f"?type=uid&value={uid}&containerid=107603{uid}&page={page_no}")
        try:
            data = await page.evaluate(JS_FETCH_JSON, url)
        except Exception as e:
            print(f"    ⚠️ 第{page_no}页请求失败: {e}")
            break
        if data.get("ok") != 1:
            print(f"    ⚠️ 第{page_no}页返回异常: {str(data)[:120]}")
            break
        cards = data["data"].get("cards", [])
        for card in cards:
            mblog = card.get("mblog")
            if not mblog:
                continue
            posts.append(extract_post(mblog, uid, author))
        await asyncio.sleep(2)  # 翻页间隔，防风控
    return posts


async def collect_users(users: List[Dict], pages: int = 2,
                        headless: bool = True) -> Dict[str, Dict]:
    """
    采集多个用户。
    返回: {uid: {"user": 用户信息, "posts": [帖子...]}}
    """
    result = {}
    async with async_playwright() as p:
        # 先检查登录态：未登录时必须 headed 模式扫码
        context = await launch_context(p, headless=headless)
        try:
            if not await is_logged_in(context):
                await context.close()
                context = await launch_context(p, headless=False)
            if not await ensure_login(context):
                return result

            page = context.pages[0] if context.pages else await context.new_page()
            # 先访问一次移动版首页，建立会话
            await page.goto("https://m.weibo.cn/", wait_until="domcontentloaded",
                            timeout=30000)
            await asyncio.sleep(2)

            for u in users:
                uid = str(u["uid"])
                print(f"  [微博] 采集 {u.get('name') or uid} ...")
                info = await fetch_user_info(page, uid)
                if not info:
                    print(f"    ❌ 跳过（可能是账号不存在或被限流）")
                    result[uid] = {"user": None, "posts": []}
                    continue
                # 回填昵称到配置
                if not u.get("name") and info["name"]:
                    u["name"] = info["name"]
                posts = await fetch_user_posts(page, uid, info["name"], pages=pages)
                result[uid] = {"user": info, "posts": posts}
                print(f"    ✅ {info['name']} 粉丝{info['followers']} "
                      f"最新 {len(posts)} 条")
                await asyncio.sleep(2)
        finally:
            await context.close()
    return result


# ============================ CLI ============================

def main():
    cfg = load_config()
    users = cfg.get("users", [])
    if not users:
        print("❌ 配置为空，请先编辑 weibo_config.json 添加要监控的微博账号")
        sys.exit(1)

    login_only = "--login" in sys.argv
    pages = 2
    for arg in sys.argv[1:]:
        if arg.startswith("--pages="):
            pages = int(arg.split("=")[1])

    result = asyncio.run(collect_users(users, pages=pages, headless=True))

    if login_only:
        return

    # 输出结果（供 monitor 或直接调试用）
    total = sum(len(v["posts"]) for v in result.values())
    print(f"\n共采集 {total} 条微博")
    out = os.path.join(BASE_DIR, "data", "weibo_latest.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"已保存: {out}")

    # 预览每个用户最新一条
    for uid, v in result.items():
        if v["posts"]:
            post = v["posts"][0]
            print(f"\n[{post['author']}] {post['created_at']}")
            print(f"  {post['text'][:100]}")


if __name__ == "__main__":
    main()
