#!/bin/bash
# 行业历史回填分轮循环：每轮抓取额度内标的，间隔30分钟，完成或15轮后结束。
# 回填期间暂停日常监控（共用雪球请求配额，避免互相触发风控）。
cd "/Users/ssj/Library/Application Support/TRAE SOLO CN/ModularData/ai-agent/work-mode-projects/6a81ab3c54e810eb12556836/weibo-monitor"

if pkill -f "weibo_monitor.py --loop" 2>/dev/null; then
  echo "=== 日常监控已暂停（回填期间避免雪球配额竞争） $(date '+%m-%d %H:%M') ==="
  sleep 5
fi

for i in $(seq 1 15); do
  echo "=== 回填第 $i 轮 $(date '+%m-%d %H:%M') ==="
  venv/bin/python -u industry_backfill.py --days 90
  if venv/bin/python -c "import json;h=json.load(open('data/industry_history.json'));exit(0 if h.get('backfill_done') else 1)" 2>/dev/null; then
    echo "=== 全部行业回填完成 $(date '+%m-%d %H:%M') ==="
    break
  fi
  echo "=== 本轮未全部完成，30 分钟后继续 ==="
  sleep 1800
done

# 回填结束后重启日常监控循环
nohup venv/bin/python -u weibo_monitor.py --loop > /tmp/weibo_monitor.log 2>&1 &
echo "=== 监控循环已重启 pid=$! ==="
