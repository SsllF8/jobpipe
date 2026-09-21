#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日一键流程与桌面快捷方式的测试。

这里只测**不联网、不建快捷方式**的部分 —— 真去 push、真去写桌面，
都不该发生在单元测试里。真正联网的那一段（取凭据 + push）在
``tools/daily.py`` 里单独可跑，属于手工验证的范畴。
"""

from __future__ import annotations

import struct
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import daily  # noqa: E402
import make_shortcut as ms  # noqa: E402


# ------------------------------------------------------- 图标（PNG → ICO）

def test_png_to_ico_writes_valid_ico(tmp_path):
    out = tmp_path / "x.ico"
    w, h = ms.png_to_ico(ROOT / "src" / "icons" / "icon-192.png", out)
    data = out.read_bytes()

    reserved, kind, count = struct.unpack("<HHH", data[:6])
    assert (reserved, kind, count) == (0, 1, 1)          # ICO 头
    assert (w, h) == (192, 192)

    bw, bh, colors, rsv, planes, bits, size, offset = struct.unpack("<BBBBHHII", data[6:22])
    assert (bw, bh) == (192, 192)                        # 256 才会记作 0
    assert (planes, bits) == (1, 32)
    assert offset == 22                                  # 数据紧跟在目录项后
    assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"
    assert size == len(data) - 22                        # 声明的长度和实际一致


def test_png_to_ico_rejects_non_png(tmp_path):
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not a png at all")
    with pytest.raises(ValueError):
        ms.png_to_ico(bad, tmp_path / "out.ico")


# ------------------------------------------------------- PowerShell 转义

def test_ps_quote_doubles_single_quote():
    assert ms.q(r"C:\a b\c") == r"'C:\a b\c'"
    assert ms.q("it's") == "'it''s'"


def test_shortcut_points_at_daily_script():
    # 快捷方式一旦指向别的脚本就白搭了，钉住它
    assert ms.SCRIPT == ROOT / "tools" / "daily.py"
    assert ms.ICO.name == "jobpipe.ico"


# ------------------------------------------------------- 推送前的护栏

def test_git_push_refuses_without_credentials(monkeypatch):
    """取不到凭据时必须**直接返回失败**，绝不能落到交互式等密码。"""
    monkeypatch.setattr(daily, "gcm_token", lambda: "")
    ok, msg = daily.git_push("git")
    assert ok is False
    assert "凭据" in msg


def test_git_push_never_echoes_token(monkeypatch, tmp_path):
    """推送输出里出现 token 属于泄漏，必须已经被 *** 覆盖。"""
    secret = "ghp_" + "x" * 30
    monkeypatch.setattr(daily, "gcm_token", lambda: secret)

    class FakeProc:
        returncode = 0
        stdout = f"remote: someone tried {secret}\n"
        stderr = ""

    def fake_run(cmd, **kw):
        assert kw.get("env", {}).get("GIT_TERMINAL_PROMPT") == "0"   # 禁止交互
        assert any("http.extraheader=Authorization: Basic " in c for c in cmd)
        return FakeProc()

    monkeypatch.setattr(daily.subprocess, "run", fake_run)
    ok, out = daily.git_push("git")
    assert ok is True
    assert secret not in out and "***" in out


# ------------------------------------------------------- CLI 本身能跑

@pytest.mark.parametrize("script", ["daily.py", "make_shortcut.py"])
def test_cli_help_exits_zero(script):
    proc = subprocess.run(
        [sys.executable, str(TOOLS / script), "--help"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert proc.returncode == 0
    assert "usage" in proc.stdout.lower()


def test_daily_dry_run_does_not_touch_repo(tmp_path):
    """--no-push --no-open 只应产出检索清单，不该动 git、也不该留下垃圾文件。"""
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=str(ROOT),
        capture_output=True, encoding="utf-8", errors="replace",
    )
    if before.returncode != 0:
        pytest.skip("当前环境没有 git")

    # 用一个专用日期，免得覆盖掉当天的真实清单
    date = "1970-01-01"
    md = ROOT / "data" / f"search-plan-{date}.md"
    assert not md.exists(), "上个测试没清干净"

    try:
        proc = subprocess.run(
            [sys.executable, str(TOOLS / "daily.py"), "--no-push", "--no-open",
             "--date", date],
            cwd=str(ROOT), capture_output=True, encoding="utf-8", errors="replace",
            timeout=300,
        )
        assert proc.returncode == 0, proc.stdout
        # 只生成文件、只自检，不做任何 stage / commit
        assert "已跳过（--no-push）" in proc.stdout
        after = subprocess.run(
            ["git", "diff", "--cached", "--name-only"], cwd=str(ROOT),
            capture_output=True, encoding="utf-8", errors="replace",
        )
        assert after.stdout.strip() == "", "daily.py 不该把任何改动 stage 起来"
    finally:
        md.unlink(missing_ok=True)      # 测试产物不留在仓库里
