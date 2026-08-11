"""
长周期历史数据快照管理

历史数据（股指期货净空单 / 宏观资金面 / 国家队ETF）一旦生成就不会变化，
存入 snapshot/ 目录并提交 git；重新部署时自动从快照恢复，无需重跑外部接口。

用法:
    python snapshot.py --save      # 运行数据 → 快照（提交代码前执行一次）
    python snapshot.py --restore   # 快照 → data/ 与 frontend/data/（仅补缺失文件）
    python snapshot.py --force     # 强制用快照覆盖运行数据（慎用）
    python snapshot.py --status    # 查看快照与运行数据对比

运行时目录 data/ 与 frontend/data/ 仍被 .gitignore 排除（含登录态等敏感内容），
只有 snapshot/ 入库。
"""
import argparse
import os
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
FRONTEND_DATA_DIR = os.path.join(BASE_DIR, "frontend", "data")
SNAPSHOT_DIR = os.path.join(BASE_DIR, "snapshot")

# 长周期、不可变的历史数据集（重新抓取成本高：中金所千次请求 / akshare 长序列）
DATASETS = [
    "index_futures_positions.json",  # 股指期货机构净空单（CFFEX 公开数据，360 天）
    "macro_data.json",               # A股资金面宏观序列（融资融券/投资者/利率/汇率，365 天）
    "national_team_etf.json",        # 国家队宽基ETF份额与持仓金额序列
    "industry_turnover.json",        # 申万行业成交额占比趋势（360 交易日）
    "index_trend.json",              # 主要指数走势（近一年日线，7 个可选指数）
]


def save():
    """运行数据 → 快照（提交 git 前执行）。"""
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    saved = 0
    for name in DATASETS:
        src = os.path.join(DATA_DIR, name)
        if not os.path.exists(src):
            print(f"  ⚠️ 运行数据缺失，跳过: {name}")
            continue
        shutil.copy2(src, os.path.join(SNAPSHOT_DIR, name))
        print(f"  💾 已存快照: {name} ({os.path.getsize(src) / 1024:.0f} KB)")
        saved += 1
    print(f"[snapshot] 共保存 {saved}/{len(DATASETS)} 份快照，可提交 git")


def restore(force=False, quiet=False):
    """快照 → 运行目录；默认仅补缺失文件。服务启动时自动调用。"""
    restored = []
    for name in DATASETS:
        snap = os.path.join(SNAPSHOT_DIR, name)
        if not os.path.exists(snap):
            continue
        for dst_dir in (DATA_DIR, FRONTEND_DATA_DIR):
            dst = os.path.join(dst_dir, name)
            if force or not os.path.exists(dst):
                os.makedirs(dst_dir, exist_ok=True)
                shutil.copy2(snap, dst)
                restored.append(os.path.relpath(dst, BASE_DIR))
    if restored and not quiet:
        for p in restored:
            print(f"  ♻️ 已从快照恢复: {p}")
    return restored


def status():
    print(f"{'数据集':32s} {'快照':>10s} {'运行数据':>10s}")
    for name in DATASETS:
        snap = os.path.join(SNAPSHOT_DIR, name)
        run = os.path.join(DATA_DIR, name)
        s = f"{os.path.getsize(snap) / 1024:.0f} KB" if os.path.exists(snap) else "—"
        r = f"{os.path.getsize(run) / 1024:.0f} KB" if os.path.exists(run) else "—"
        print(f"{name:32s} {s:>10s} {r:>10s}")


def main():
    parser = argparse.ArgumentParser(description="长周期历史数据快照管理")
    parser.add_argument("--save", action="store_true", help="运行数据 → 快照")
    parser.add_argument("--restore", action="store_true", help="快照 → 运行目录（仅补缺失）")
    parser.add_argument("--force", action="store_true", help="快照强制覆盖运行数据")
    parser.add_argument("--status", action="store_true", help="查看对比")
    args = parser.parse_args()

    if args.save:
        save()
    elif args.force:
        restore(force=True)
    elif args.restore:
        restore()
    else:
        status()


if __name__ == "__main__":
    main()
