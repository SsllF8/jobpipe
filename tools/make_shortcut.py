#!/usr/bin/env python3
"""在桌面生成「每日推送」快捷方式（Windows）。

做的事情只有两件：
    1. 把 PWA 图标 src/icons/icon-192.png 包成 Windows 能认的 .ico
       （ICO 从 Vista 起允许直接内嵌 PNG，所以不需要 Pillow 之类的绘图库）
    2. 用 WScript.Shell 建一个直接指向 python.exe + tools/daily.py 的桌面快捷方式

快捷方式用 PowerShell 的 -EncodedCommand（UTF-16LE base64）下发，
不拼命令行字符串 —— 中文路径 / 空格 / 引号都不会被转义搞坏。

用法：
    python tools/make_shortcut.py                 # 生成/覆盖桌面快捷方式
    python tools/make_shortcut.py --name 求职推送  # 换个名字
    python tools/make_shortcut.py --python <路径>  # 指定解释器
    python tools/make_shortcut.py --remove        # 删掉它
"""

from __future__ import annotations

import argparse
import base64
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PNG = ROOT / "src" / "icons" / "icon-192.png"
ICO = ROOT / "assets" / "jobpipe.ico"
SCRIPT = ROOT / "tools" / "daily.py"
APP_URL = "https://ssllf8.github.io/jobpipe/"
DEFAULT_NAME = "每日求职推送"

# 路径变了就重跑本脚本即可覆盖旧快捷方式。
MANAGED_PYTHON = Path.home() / ".workbuddy" / "binaries" / "python" / "versions" / "3.13.12" / "python.exe"


def find_python(override: str | None = None) -> Path:
    """挑一个解释器：显式指定 > 托管解释器 > PATH 里的 python > 当前解释器。"""
    if override:
        path = Path(override)
        if not path.exists():
            raise FileNotFoundError(f"指定的解释器不存在：{path}")
        return path
    for cand in (MANAGED_PYTHON, ):
        if cand.exists():
            return cand
    hit = shutil.which("python")
    if hit:
        return Path(hit)
    return Path(sys.executable)


def png_to_ico(png: Path, out: Path) -> tuple[int, int]:
    """把单张 PNG 塞进 ICO 容器（PNG 压缩的 ICO，Windows Vista+ 支持）。"""
    data = png.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{png} 不是合法 PNG")
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")

    header = struct.pack("<HHH", 0, 1, 1)              # reserved / type=icon / count=1
    entry = struct.pack(
        "<BBBBHHII",
        width % 256, height % 256,                     # 256 记作 0，其余就是本身
        0, 0, 1, 32,                                   # 调色板 / 保留 / 平面 / 位深
        len(data), 6 + 16,                             # 数据长度 / 数据偏移
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(header + entry + data)
    return width, height


def powershell() -> str:
    hit = shutil.which("powershell")
    if hit:
        return hit
    root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    for rel in ("System32/WindowsPowerShell/v1.0/powershell.exe",
                "SysWOW64/WindowsPowerShell/v1.0/powershell.exe"):
        cand = root / rel
        if cand.exists():
            return str(cand)
    raise RuntimeError("找不到 powershell，无法创建快捷方式")


def run_ps(script: str) -> str:
    # PowerShell 默认按控制台代码页（中文 Windows 是 936）写 stdout，
    # 我们在 Python 侧按 utf-8 解，中文路径会变成乱码 —— 强制它输出 utf-8。
    script = "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\n" + script
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        [powershell(), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True, encoding="utf-8", errors="replace", timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip() or "PowerShell 执行失败")
    return (proc.stdout or "").strip()


def q(path: str | Path) -> str:
    """PowerShell 单引号字面量：内部单引号翻倍。"""
    return "'" + str(path).replace("'", "''") + "'"


def create(name: str, pyexe: str | None = None) -> Path:
    if not SCRIPT.exists():
        raise FileNotFoundError(f"找不到主脚本 {SCRIPT}")

    python = find_python(pyexe)
    w, h = png_to_ico(PNG, ICO)
    print(f"图标已生成：assets/jobpipe.ico（{w}×{h}，内嵌 PNG）")
    print(f"解释器：{python}")

    script = f"""
$ErrorActionPreference = 'Stop'
$desktop = [Environment]::GetFolderPath('Desktop')
$target = Join-Path $desktop {q(name + '.lnk')}
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($target)
$lnk.TargetPath = {q(python)}
$lnk.Arguments = {q('"' + str(SCRIPT) + '"')}
$lnk.WorkingDirectory = {q(ROOT)}
$lnk.IconLocation = {q(str(ICO) + ',0')}
$lnk.Description = 'jobpipe 每日推送：生成岗位检索清单 + 推送到线上'
$lnk.WindowStyle = 1
$lnk.Save()
Write-Output $target
"""
    target = Path(run_ps(script))
    print(f"快捷方式已创建：{target}")

    # 立刻读回来核对 —— 建快捷方式最容易出的问题是参数没带上或路径编码坏了
    got = run_ps(f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$t = Join-Path $desktop {q(name + '.lnk')}
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($t)
'TARGET=' + $s.TargetPath
'ARGS=' + $s.Arguments
'EXISTS=' + (Test-Path (($s.Arguments -replace '"','')))
""")
    print("\n".join("  " + line for line in got.splitlines()))
    return target


def remove(name: str) -> None:
    script = f"""
$desktop = [Environment]::GetFolderPath('Desktop')
$target = Join-Path $desktop {q(name + '.lnk')}
if (Test-Path $target) {{ Remove-Item -LiteralPath $target -Force; Write-Output 'REMOVED' }} else {{ Write-Output 'NOT_FOUND' }}
"""
    print({"REMOVED": "快捷方式已删除。", "NOT_FOUND": "桌面没有这个快捷方式。"}[run_ps(script)])


def main() -> int:
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(description="生成桌面「每日推送」快捷方式")
    parser.add_argument("--name", default=DEFAULT_NAME, help=f"快捷方式显示名，默认「{DEFAULT_NAME}」")
    parser.add_argument("--python", help="指定 python.exe 路径（换了解释器时用）")
    parser.add_argument("--remove", action="store_true", help="删除桌面快捷方式")
    args = parser.parse_args()

    try:
        if args.remove:
            remove(args.name)
        else:
            create(args.name, args.python)
            print()
            print("双击它就会依次做四件事：")
            print("  ① 生成今日检索清单并打开（170 条可点链接）")
            print("  ② 本地构建 + 发布审计自检")
            print("  ③ 提交推送到 GitHub，CI 自动上线")
            print("  ④ 打印今天的下一步操作")
            print()
            print(f"线上地址：{APP_URL}")
    except Exception as exc:          # noqa: BLE001 —— 个人脚本，报错说人话就够了
        print(f"[失败] {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
