# 📡 大V监控（微博 + 雪球）

监控微博、雪球两个平台的大V发言与评论，增量检测新内容，按每用户独立策略过滤（股市相关性 LLM 识别），白底看板页两级 Tab 展示。

## 功能特性

- **双平台采集**：微博（时间线 + 本人评论区留言，含对旧发言的补充）、雪球（时间线含评论回复）
- **股市相关性过滤**：关键词快筛 + qwen LLM 语义判断（识别隐喻，如"村里"=监管、"五穷六绝七翻身"）
- **回复语境整合**：大V的短回复（如"很快"）会连同粉丝的前置提问一起判断和展示
- **每用户独立策略**：`filter`（是否过滤）/ `display_limit`（展示条数）/ `fetch_pages`（采集深度）
- **增量检测**：首次采集建基线不刷屏，之后只标记新增
- **分时段轮询**：工作日 8:00-16:00 每 8 分钟，其余时段每 20 分钟（可配）
- **风控防护**：Playwright 持久浏览器、页面内 fetch、请求超时保护、触发风控自动降级跳过

## 目录结构

```
weibo-monitor/
├── weibo_collector.py     # 微博采集器（Playwright 持久登录态）
├── xueqiu_collector.py    # 雪球采集器（匿名 token，无需登录）
├── weibo_filter.py        # 股市相关性过滤（关键词 + LLM）
├── weibo_monitor.py       # 主编排：采集→增量→过滤→页面数据
├── weibo_config.json      # 监控账号与策略配置
├── serve.py               # 静态页面服务（默认 8766 端口）
├── frontend/weibo.html    # 看板页（两级 Tab：平台→大V）
└── data/                  # 运行时数据（登录态/历史/日志，不入库）
```

## 快速开始

```bash
pip install -r requirements.txt
python -m playwright install chromium chromium-headless-shell

# 首次运行：微博会弹出浏览器扫码登录（登录态持久保存到 data/weibo_profile/）
python weibo_monitor.py          # 单次检测
python weibo_monitor.py --loop   # 后台循环（分时段间隔）
python serve.py                  # 页面服务 http://localhost:8766
```

后台运行：

```bash
nohup python -u weibo_monitor.py --loop > data/weibo_monitor.log 2>&1 &
nohup python -u serve.py > data/serve.log 2>&1 &
```

## 配置示例

`weibo_config.json` 中每个账号独立策略：

```json
{
  "platform": "weibo",        // weibo / xueqiu
  "uid": "1593163950",
  "name": "周思CIO",
  "filter": true,             // true=仅股市相关, false=全量展示
  "display_limit": 50,        // 页面展示条数上限
  "fetch_pages": 3,           // 时间线采集页数
  "comment_scan_posts": 12    // 评论区扫描范围（仅微博）
}
```

轮询节奏：`peak_interval_sec`（工作日 8-16 点）/ `offpeak_interval_sec`（其余时段）。

## 依赖说明

- LLM 过滤依赖上级目录 `utils/qwen_utils.py` 的 `chat()` 与 `config.yaml` 中的 DASHSCOPE_API_KEY；不可用时自动降级为纯关键词模式
- 雪球采集无需登录；微博需要扫码登录一次（真实账号登录态，注意控制采集频率）
