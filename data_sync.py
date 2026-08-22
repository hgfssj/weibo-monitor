#!/usr/bin/env python3
"""GitHub 数据同步 — 让多台电脑共享同一份行业讨论历史。

流程（after_collect，每轮采集后自动执行）：
  1. 新帖子追加到本机档案 data/archive/（按机器分文件，无 git 冲突）
  2. git commit + push（推送本机新档案）
  3. git pull --rebase（拉取他机档案；冲突仅可能来自手工改动的文件，
     档案文件按机器命名不会冲突）
  4. 由档案并集重建历史库（data_archive.rebuild_into_history，
     单调合并 → 历史只会更全）+ 刷新周摘要

安全边界：
  - 只 add data/archive/，cookie（data/xueqiu_profile）、缓存、历史库
    均在 .gitignore 中，不会上传；
  - 所有 git 失败只打印警告，绝不中断采集主流程。

用法：
    python data_sync.py --after-collect   # 采集后：追加+推送+拉取+重建
    python data_sync.py --pull            # 仅拉取+重建（开机/手动）
"""
import os
import subprocess
import sys
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import data_archive


def _git(*args, timeout=180) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", BASE_DIR, *args],
                          capture_output=True, text=True, timeout=timeout)


def _ok(r: subprocess.CompletedProcess) -> bool:
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip().splitlines()
        print(f"    ⚠️ git {r.args[2] if len(r.args) > 2 else ''} 失败: "
              f"{err[-1] if err else '未知'}")
    return r.returncode == 0


def push_archive() -> bool:
    """提交并推送档案变更（无变更自动跳过）。"""
    r = _git("add", "data/archive")
    if not _ok(r):
        return False
    r = _git("diff", "--cached", "--quiet")
    if r.returncode == 0:
        return True  # 无变更
    msg = f"data: {data_archive.machine_id()} {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    if not _ok(_git("commit", "-m", msg)):
        return False
    r = _git("push")
    if not _ok(r):
        # 远端有新提交 → 拉取合并后重试一次
        if _ok(_git("pull", "--rebase", "--autostash")):
            return _ok(_git("push"))
        _git("rebase", "--abort")
        print("    ⚠️ 推送失败（rebase 冲突），档案保留本地待下次重试")
        return False
    return True


def pull_archive() -> bool:
    """拉取远端档案（他机数据）。"""
    r = _git("pull", "--rebase", "--autostash")
    if not _ok(r):
        _git("rebase", "--abort")
        print("    ⚠️ 拉取失败，本次使用本地数据")
        return False
    return True


def after_collect(industry_data: dict, cfg: dict = None):
    """采集/回填一轮后调用：追加档案 → 推送 → 拉取 → 重建历史库。"""
    n = data_archive.append_round(industry_data)
    pushed = push_archive()
    pulled = pull_archive()
    if n or pulled:
        data_archive.rebuild_into_history(cfg)
        try:
            import industry_history
            arch = data_archive.load_all()
            industry_history.generate_weekly_summaries(
                {"industries": {iid: {"viewpoints": posts}
                                for iid, posts in arch.items()}})
        except Exception as e:
            print(f"    ⚠️ 周摘要刷新跳过: {e}")
    print(f"  🔄 GitHub 同步完成: 新增档案 {n} 条 · "
          f"推送{'✓' if pushed else '✗'} · 拉取{'✓' if pulled else '✗'}")


def pull_and_rebuild(cfg: dict = None):
    """开机/运行前调用：仅拉取 + 重建（不推送）。"""
    pulled = pull_archive()
    if pulled:
        data_archive.rebuild_into_history(cfg)


if __name__ == "__main__":
    cfg_p = os.path.join(BASE_DIR, "weibo_config.json")
    cfg = {}
    if os.path.exists(cfg_p):
        import json
        with open(cfg_p, encoding="utf-8") as f:
            cfg = json.load(f)
    if "--after-collect" in sys.argv:
        p = os.path.join(BASE_DIR, "data", "industry_data.json")
        with open(p, encoding="utf-8") as f:
            after_collect(json.load(f), cfg)
    elif "--pull" in sys.argv:
        pull_and_rebuild(cfg)
