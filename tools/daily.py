#!/usr/bin/env python3
"""每日一键流程：生成检索清单 → 本地自检 → 推送上线 → 打开清单。

桌面快捷方式（由 tools/make_shortcut.py 生成）双击跑的就是这个脚本。

为什么是「生成清单」而不是「自动抓岗位」：
    招聘站点的反爬与合规成本很高，且站点改版就得跟着修。
    这里只按各站公开的 URL 规则拼检索链接，零反爬风险、零维护，
    代价是每天约 10 分钟人工挑岗位 —— 这是刻意的取舍，不是没做完。

用法：
    python tools/daily.py              # 完整流程
    python tools/daily.py --no-push    # 只生成 + 自检，不推远端
    python tools/daily.py --no-open    # 不自动打开浏览器
    python tools/daily.py --keywords "AI应用开发,大模型开发"   # 临时改搜索词
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
DATA = ROOT / "data"
PY = sys.executable
BRANCH = "main"

# 已登录的 GitHub 凭据在 Git Credential Manager 里；这里只做「读出来用」，
# 不落盘、不打印。清空 GCM 或换机器时需要重新 git push 一次让 GCM 记住。
GCM_CANDIDATES = [
    Path(r"C:\Program Files\Git\mingw64\bin\git-credential-manager.exe"),
    Path(r"C:\Program Files\Git\mingw64\libexec\git-core\git-credential-manager.exe"),
]
GIT_CANDIDATES = [
    Path(r"C:\Program Files\Git\cmd\git.exe"),
    Path(r"C:\Program Files (x86)\Git\cmd\git.exe"),
]


# ----------------------------------------------------------------- 小工具

def rule(title: str) -> None:
    print()
    print("─" * 62)
    print(f"  {title}")
    print("─" * 62)
    sys.stdout.flush()


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """跑子进程并把输出实时透出来（编码固定 utf-8，避免中文乱码）。"""
    # 子进程直接写终端 fd，绕过了 Python 的缓冲；不先 flush 的话
    # 我们的标题会跑到子进程输出后面去，看着像顺序错乱。
    sys.stdout.flush()
    return subprocess.run(
        cmd, cwd=str(ROOT), encoding="utf-8", errors="replace", **kw
    )


def find_git() -> str | None:
    hit = shutil.which("git")
    if hit:
        return hit
    for cand in GIT_CANDIDATES:
        if cand.exists():
            return str(cand)
    return None


def gcm_token() -> str:
    """从 Git Credential Manager 取已登录的 GitHub token（不落盘）。"""
    gcm = next((p for p in GCM_CANDIDATES if p.exists()), None)
    if gcm is None:
        return ""
    try:
        proc = subprocess.run(
            [str(gcm), "get"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception:
        return ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


# ----------------------------------------------------------------- 四步

def step_plan(args) -> dict:
    """① 生成检索清单（手机可点的 HTML + 仓库留档的 MD）。"""
    rule("① 生成今日检索清单")
    cmd = [PY, "tools/search_plan.py", "--date", args.date]
    if args.keywords:
        cmd += ["--keywords", args.keywords]
    if not args.no_open:
        cmd.append("--open")
    proc = run(cmd)
    if proc.returncode != 0:
        return {"ok": False, "why": "检索清单生成失败"}

    md = DATA / f"search-plan-{args.date}.md"
    links = 0
    if md.exists():
        links = sum(1 for line in md.read_text(encoding="utf-8").splitlines()
                    if line.startswith("- [ ]"))
    return {"ok": True, "links": links, "html": DIST / "search-plan.html"}


def step_selfcheck() -> dict:
    """② 本地先跑一遍发布闸门 —— 有问题就别推上去让 CI 红。"""
    rule("② 本地自检（构建 + 发布审计）")
    build = run([PY, "build.py", "--publish"])
    if build.returncode != 0:
        return {"ok": False, "why": "构建失败"}
    audit = run([PY, "tools/check_public.py"])
    if audit.returncode != 0:
        return {"ok": False, "why": "发布审计不通过（产物可能含个人数据）"}
    return {"ok": True}


def git_push(git: str) -> tuple[bool, str]:
    """把当前分支推上远端。返回 (是否成功, 输出)。

    用 http.extraheader 传凭据，而不是临时凭据文件：
    临时文件依赖 $HOME 正常，环境异常时会静默失败并挂起等输入。
    另外必须带 GIT_TERMINAL_PROMPT=0 —— 宁可报错也不能卡住等人的输入。
    """
    token = gcm_token()
    if not token:
        return False, "取不到 GitHub 凭据（Git Credential Manager 里没有 github.com）"
    auth = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        push = subprocess.run(
            [git, "-c", f"http.extraheader=Authorization: Basic {auth}",
             "push", "origin", BRANCH],
            cwd=str(ROOT), capture_output=True, encoding="utf-8",
            errors="replace", timeout=180, env=env,
        )
    except subprocess.TimeoutExpired:
        return False, "推送超时（网络问题？）"
    out = ((push.stdout or "") + (push.stderr or "")).replace(token, "***")
    return push.returncode == 0, out


def step_push(date: str) -> dict:
    """③ 提交并推送，CI 会自动测试 → 审计 → 上线。"""
    rule("③ 同步到线上")
    git = find_git()
    if git is None:
        return {"ok": False, "why": "找不到 git"}

    run([git, "add", "-A"])
    staged = run([git, "diff", "--cached", "--quiet"])
    if staged.returncode == 0:
        print("没有新变更，跳过提交（线上保持原样）。")
        return {"ok": True, "committed": False}

    files = run([git, "diff", "--cached", "--name-only"], capture_output=True)
    changed = len([x for x in (files.stdout or "").splitlines() if x.strip()])
    commit = run([git, "commit", "-q", "-m", f"chore(daily): 检索清单 {date}"])
    if commit.returncode != 0:
        return {"ok": False, "why": "提交失败"}
    print(f"已提交 {changed} 个文件的变更。")

    ok, out = git_push(git)
    for line in out.splitlines():
        if line.strip():
            print("  " + line.strip())
    if not ok:
        return {"ok": False, "why": "推送失败（提交已在本地，重跑本脚本即可重试）"}
    return {"ok": True, "committed": True}


def step_hint(args) -> None:
    """④ 收尾：告诉他今天下一步干什么。"""
    rule("④ 接下来")
    print("手机打开 https://ssllf8.github.io/jobpipe/ 挑岗位")
    print("（已加到主屏幕的话，下拉刷新一次即可）")
    print()
    print("挑中岗位后，把 JD 正文存成 jd.txt，然后回来敲：")
    print()
    print('  python tools/ingest_jd.py --file jd.txt --channel 猎聘 \\')
    print('      --company "某某科技" --title "AI应用开发工程师" --url "https://…"')
    print()
    print("再跑一次本快捷方式，岗位就上线了。")


# ----------------------------------------------------------------- 入口

def main() -> int:
    # 输出会经过子进程（git / build），行缓冲能保证顺序不错乱
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(
        description="每日一键：检索清单 → 自检 → 推送上线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="日期，默认今天")
    parser.add_argument("--keywords", help="逗号分隔，整体覆盖默认搜索词")
    parser.add_argument("--no-push", action="store_true", help="不推送远端，只本地生成")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    print()
    print("═" * 62)
    print(f"  jobpipe 每日推送 · {args.date}")
    print("═" * 62)

    plan = step_plan(args)
    if not plan["ok"]:
        print(f"\n[中断] {plan['why']}")
        return 1

    check = step_selfcheck()
    if not check["ok"]:
        print(f"\n[中断] {check['why']} —— 没有推送，线上保持可用状态。")
        return 1

    if args.no_push:
        rule("③ 同步到线上")
        print("已跳过（--no-push）。")
        result = {"ok": True, "committed": False}
    else:
        result = step_push(args.date)
    if not result["ok"]:
        print(f"\n[中断] {result['why']}")
        return 1

    step_hint(args)

    print()
    print("═" * 62)
    print(f"  完成：{plan['links']} 条检索链接已备好"
          + ("，线上已更新（CI 跑完约 1 分钟生效）" if result.get("committed") else ""))
    print("═" * 62)
    return 0


def pause() -> None:
    """双击快捷方式时把窗口留住，否则出错信息一闪就没了；
    在管道 / CI 里跑（stdin 不是终端）就不拦。"""
    try:
        if sys.stdin is not None and sys.stdin.isatty():
            input("\n按回车关闭窗口 …")
    except (EOFError, KeyboardInterrupt, OSError):
        pass


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        code = 130
    except Exception as exc:                      # noqa: BLE001 —— 双击场景要看得见报错
        import traceback
        traceback.print_exc()
        print(f"\n[出错] {exc}")
        code = 1
    pause()
    raise SystemExit(code)
