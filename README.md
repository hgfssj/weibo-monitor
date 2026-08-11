# 📡 大V监控 + 行业监控 + A股资金面（微博 + 雪球）

监控**微博、雪球**两个平台的大V发言与评论，扩展**行业监控**（股票池近 7 天讨论、3 天内高价值观点、综合点评），并新增**A股资金面**看板（融资融券、投资者数量、平均维持担保比例、中美国债收益率、人民币汇率）。自带雪球风格白底蓝看板，多空红绿配色；支持前端「配置管理」页在线编辑参数并保存生效。

## 功能特性

### 大V监控（微博 + 雪球）
- **双平台采集**：微博（时间线 + 本人评论区留言，含对旧发言的补充）、雪球（时间线含评论回复）
- **股市相关性过滤**：关键词快筛 + LLM 语义判断（识别隐喻，如"村里"=监管、"五穷六绝七翻身"），按每用户独立策略开关
- **回复语境整合**：大V的短回复（如"很快"）会连同粉丝的前置提问一起判断和展示
- **每用户独立策略**：`filter` / `display_limit` / `fetch_pages` / `comment_scan_posts`
- **增量检测**：首次采集建基线不刷屏，之后只标记新增

### 行业监控（6 大方向）
- 方向：**人形机器人 / AI应用·软件 / AI医药·创新药 / 锂矿·电池·储能 / AI硬件 / 模型·云厂商**（股票池可在配置页编辑）
- 每个行业：股票池近 **7 天**帖子与评论 → 个股近期讨论
- **高价值观点（近 3 天）**：突出发布时间、所属个股中文名、作者，并附雪球链接可点击定位
- **综合点评**：多空共识、估值/景气判断、关键要点（LLM 研判，未配置大模型时走启发式兜底）

### A股资金面
- **融资融券**：融资余额 / 融券余额 / 融资买入额 / 融券卖出额（东方财富 `RPTA_WEB_MARGIN_DAILYTRADE`）
- **账户杠杆**：参与交易投资者数量 / 平均维持担保比例
- **宏观利率/汇率**：中国/美国 10 年期国债收益率、人民币汇率中间价（美元兑人民币）
- **近 1 年日线趋势图**：8 张 SVG 折线图卡片，自动跟随监控循环每日更新
- **国家队宽基ETF增减持**：上交所官方每日ETF份额（沪市核心ETF历史）× 东方财富历史单位净值 = **持仓金额(亿元)**；深市ETF份额锚定最新披露快照、历史金额按净值估算。双视图「每日净申购金额(增减持信号)」与「持仓金额(亿元)」趋势，按宽基类别(沪深300/上证50/中证500/中证1000/科创50/创业板)聚合

### 股指期货（公开渠道直连，无需 OCR）
- **数据源**：中国金融期货交易所(CFFEX)每日收盘后发布的「前 20 会员成交持仓排名」CSV
  （`http://www.cffex.com.cn/sj/ccpm/{YYYYMM}/{DD}/{VAR}_1.csv`，GBK），属公开权威数据，**不依赖任何大V、不需要 Tesseract、不需要浏览器**
- **计算口径**：跨 IF/IC/IM/IH 全部合约汇总
  - 中信期货净空单 = Σ(持卖单量 − 持买单量)
  - 其他主要玩家净空单 = 前 20 会员扣除中信期货后汇总
  - **直接得到真实绝对净空单水平**，无需 baseline 假设 / 从 0 累加
- **双视图**：「净空单水平（绝对）」与「每日增减」可切换；含近 30 天双轴趋势图（叠加上证指数）与自动多空信号分析
- **上证指数**：自动从 A股资金面宏观数据里取上证指数收盘填充
- **数据源切换**：`weibo_config.json` 的 `index_futures.source` 可设 `public`（默认，中金所直连）或 `ocr`（沿用雪球大V图片 OCR 旧路径，需 Tesseract）

### 看板与配置
- **雪球风格 UI**：白底 + 蓝主题，多空红绿（看多/涨=红，看空/跌=绿）
- **五级导航**：大V监控 / 行业监控 / 股指期货 / A股资金面 / 配置管理
- **配置管理页（⚙️）**：在线编辑后台刷新间隔、前台轮询间隔、微博/雪球大V列表、各行业股票池；「💾 保存生效」按钮即时写回 `weibo_config.json`（自动备份 `.bak`），后台下一轮自动加载

### 工程
- **风控降级**：Playwright 持久浏览器、页面内 fetch、请求超时保护，触发风控自动跳过该轮
- **配置/代码分离**：所有可调参数在 `weibo_config.json`，无硬编码路径
- **空态友好**：无数据时前端显示「暂无数据」而非崩溃

## 目录结构

```
weibo-monitor/
├── weibo_collector.py     # 微博采集器（Playwright 持久登录态）
├── xueqiu_collector.py    # 雪球采集器（匿名 token，无需登录）
├── weibo_filter.py        # 股市相关性过滤（关键词 + LLM）
├── weibo_summary.py       # 大V研判（多空/仓位/加减仓）
├── industry_collector.py  # 行业采集（搜索 SPA + 行情接口）
├── industry_summary.py    # 行业研判（趋势/估值/共识/要点）
├── a_share_collector.py   # A股资金面数据采集（融资融券/投资者/利率/汇率）
├── national_team_etf.py   # 国家队宽基ETF增减持（沪深交易所官方份额，可回补2年）
├── industry_turnover.py   # 申万行业成交额占比趋势（360交易日，31一级+3二级+大消费聚合）
├── index_trend.py         # 主要指数走势（7个可选指数近一年日线，新浪+中证官网）
├── index_futures_public.py # 股指期货净空单（中金所直连：中信/其他大机构/头部机构合计）
├── if_ocr.py              # 股指期货图片 OCR 旧路径（source=ocr 时启用，需 Tesseract）
├── weibo_monitor.py       # 主编排：采集→增量→过滤→页面数据（含 --loop / --industries-only，单实例锁）
├── serve.py               # 前端服务（默认 8766）+ 配置读写 API
├── snapshot.py            # 长周期数据快照管理（--save / --restore / --status）
├── snapshot/              # 历史数据快照（入库，新部署自动恢复，免重抓）
├── weibo_config.json      # 监控账号、行业股票池、刷新间隔（可在线编辑）
├── frontend/weibo.html    # 看板页（五级导航：大V/行业/期指/资金面/配置）
├── start.sh               # 一键启动 serve + monitor
├── .env.example           # 可选环境变量模板（大模型研判）
├── data/                  # 运行时数据（登录态/采集历史/日志，不入库）
└── logs/                  # 运行日志（不入库）
```

## 快速开始

```bash
pip install -r requirements.txt
python -m playwright install chromium chromium-headless-shell

# 股指期货默认走中金所公开数据直连（纯 HTTP，无需 Tesseract / 无需浏览器）。
# 仅当 index_futures.source="ocr"（沿用大V图片识别旧路径）时才需要：
# macOS:  brew install tesseract tesseract-lang
# Ubuntu: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim

# 采集一次（生成页面数据到 data/ 与 frontend/data/）
python weibo_monitor.py            # 仅大V监控
python weibo_monitor.py --industries-only   # 仅行业监控（雪球匿名，无需登录）
python weibo_monitor.py --loop     # 后台循环（按配置间隔刷新）

# 单独回补股指期货近 60 天（中金所直连，可随时手动跑）
python index_futures_public.py --backfill=60

# 前端页面服务
python serve.py                    # http://localhost:8766/weibo.html
# 或一键启动两者：
./start.sh
```

> - 首次运行微博采集会弹出浏览器扫码登录（登录态持久保存到 `data/weibo_profile/`）；雪球采集无需登录。
> - **新部署零成本**：长周期历史数据（期指净空单/宏观资金面/国家队ETF/行业占比）已快照入库，首次启动自动从 `snapshot/` 恢复，无需重抓。
> - `--loop` 自带单实例锁：已有循环在运行时重复启动会被拒绝（避免双进程争抢浏览器登录态导致崩溃）。重启请 `kill $(cat data/weibo_monitor.pid)` 后再启动。

## 重新拉取部署（全新环境）

```bash
git clone <repo> && cd weibo-monitor
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt                    # pandas/numpy 版本已固定，勿升级
python -m playwright install chromium chromium-headless-shell
python snapshot.py --restore                       # 可选：启动时也会自动执行
python serve.py &                                  # 前端
python weibo_monitor.py --loop &                   # 后台循环（首次微博需扫码）
```

## 配置说明（`weibo_config.json`）

```jsonc
{
  "users": [                      // 大V列表（微博/雪球）
    {
      "platform": "weibo",        // weibo / xueqiu
      "uid": "1593163950",
      "name": "周思CIO",
      "filter": true,             // true=仅股市相关, false=全量展示
      "display_limit": 50,
      "fetch_pages": 3,
      "comment_scan_posts": 12    // 仅微博：评论区扫描范围
    }
  ],
  "industries": [                 // 行业监控股票池
    {
      "id": "humanoid_robot",
      "name": "人形机器人",
      "icon": "🤖",
      "days": 7,                  // 个股讨论回溯天数
      "stocks": [
        { "url": "https://xueqiu.com/S/SH688836", "name": "宇树科技", "note": "..." }
      ]
    }
  ],
  "index_futures": {              // 股指期货配置
    "source": "public",           // public=中金所公开数据直连（默认）；ocr=雪球大V图片 OCR 旧路径
    "backfill_days": 60,          // 每次回补的日历日窗口（只落地其中的交易日）
    "monitor_uid": "2411215032",  // 仅当保留「大V原文复盘」feed 时需要；设为空可彻底去掉浏览器依赖
    "monitor_name": "股指期货机构持仓复盘",
    "contracts": ["IF", "IH", "IC", "IM"]
  },
  "peak_interval_sec": 1800,      // 高峰时段后台刷新间隔（秒）
  "offpeak_interval_sec": 1800,   // 非高峰时段后台刷新间隔（秒）
  "frontend_poll_interval_sec": 30 // 前端页面轮询间隔（秒）
}
```

- 也可在页面右上「⚙️ 配置管理」中在线编辑上述所有项，点「💾 保存生效」。
- 后台刷新间隔可通过 `--interval <秒>` 单次运行覆盖。

## 依赖说明

- **必需**：`playwright`（浏览器采集大V/行业）、`akshare`/`requests`（宏观/ETF/行业数据）、`pandas==1.5.3`/`numpy==1.24.3`（版本已固定，勿升级，详见 requirements.txt 注释）。
- **股指期货**：默认 `source=public` 走中金所公开数据直连，**仅用标准库 urllib+csv，无任何额外依赖、不需要 Tesseract、不需要浏览器**。仅当 `source=ocr` 时才需要 `Pillow` + `pytesseract` + 系统 `tesseract`（见上文），未安装则跳过。
- **可选**：大模型研判（`weibo_summary.py` / `weibo_filter.py`）在设置环境变量 `DASHSCOPE_API_KEY` 后即可启用——代码内置 `urllib` 直连 DashScope（默认模型 `qwen-plus`，可用 `DASHSCOPE_MODEL` 覆盖），**无需任何第三方依赖、也无需 `utils/` 模块**。未设置该变量时自动降级为纯关键词 / 启发式模式，不影响主流程与数据采集。
- 雪球采集无需登录；微博需要扫码登录一次（真实账号登录态，注意控制采集频率）。

## 部署建议

- 开发/临时：`python serve.py` + `python weibo_monitor.py --loop`（或 `./start.sh`）。
- 常驻（macOS）：用 `launchd` plist（`KeepAlive` + `RunAtLoad`）让两个进程开机自启、崩溃自愈；注意 plist 内的路径需改为实际部署路径，且**不要**把含本机绝对路径的 plist 提交到仓库。

## 历史数据快照（重新部署免重抓）

长周期历史数据（股指期货 360 天净空单 / 宏观资金面 365 天 / 国家队ETF约2年 / 申万行业占比 360 交易日 / 主要指数近一年日线）不可变，
以快照形式入库（`snapshot/`，共约 1.3 MB）；`data/` 与 `frontend/data/` 仍不入库。

- **新部署自动恢复**：`serve.py` / `weibo_monitor.py` 启动时检测运行数据缺失则从快照补齐，无需调用外部接口
- **更新快照**：数据回补范围扩大或长周期修正后，提交前跑一次 `python snapshot.py --save`
- 其他命令：`--restore`（仅补缺失）/ `--force`（快照覆盖运行数据）/ `--status`（对比）
