"""
微博监控前端服务 — 静态文件服务器

用法:
    python serve.py             # 默认 8766 端口
    python serve.py --port 9000
"""
import argparse
import http.server
import os
import socketserver

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=FRONTEND_DIR, **kwargs)

    def do_GET(self):
        # 根路径直接跳转到看板页
        if self.path in ("/", ""):
            self.send_response(302)
            self.send_header("Location", "/weibo.html")
            self.end_headers()
            return
        super().do_GET()

    def end_headers(self):
        # 禁止缓存，保证页面数据实时刷新
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description="微博监控前端服务")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()

    os.chdir(FRONTEND_DIR)
    with socketserver.TCPServer(("", args.port), Handler) as httpd:
        httpd.allow_reuse_address = True
        print(f"📡 微博监控页面: http://localhost:{args.port}/weibo.html")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n👋 服务已停止")


if __name__ == "__main__":
    main()
