#!/usr/bin/env bash
# 一键启动微博+雪球监控看板：前端服务 + 后台循环采集
# 用法: ./start.sh
set -e
cd "$(dirname "$0")"
mkdir -p logs data

# 1) 前端页面服务 -> http://localhost:8766/weibo.html
nohup python -u serve.py > logs/serve.log 2>&1 &
echo "✅ serve 已启动 -> http://localhost:8766/weibo.html (pid $!)"

# 2) 后台循环采集（行业监控 + 大V监控，按配置间隔刷新）
nohup python -u weibo_monitor.py --loop > logs/weibo_monitor.log 2>&1 &
echo "✅ monitor 已启动 -> 按 weibo_config.json 间隔循环采集 (pid $!)"

echo
echo "查看日志:  tail -f logs/serve.log logs/weibo_monitor.log"
echo "停止服务:  kill $! 以及 serve 的 pid；或用 pkill -f 'weibo_monitor.py --loop' / pkill -f serve.py"
