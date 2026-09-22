#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布闸门：审计产物目录，确认里面没有任何个人数据、也没有多余文件。

为什么需要一个独立脚本，而不是把断言写进测试
--------------------------------------------
「构建产物能不能公开」这件事有两个入口：本地手动部署、CI 自动部署。
写在测试里只能挡住 CI；做成独立脚本则两边都能用，
而且本地跑一次就能立刻知道，不用等 CI 失败回来。

三道闸的分工
------------
    build.py --publish    只产出可发布版本，且先清空目标目录（结构上排除）
    tools/check_public.py 按白名单校验文件与内容（本次运行前排除）
    .github/workflows     构建前后各跑一次，任一失败就不部署

具体查什么
----------
1. 目录里只允许出现白名单文件 —— `preview-demo.html`（含虚构投递记录）与
   `local.html`（含真实投递记录）一旦混进来立刻报警。
2. 页面不得内联任何 state 补丁（`state_inlined` 必须为 false）。
3. 不得出现密钥、本机绝对路径。
4. PWA 资产必须齐备且 manifest 合法 —— 顺手把「部署上去发现装不了」也挡在前面。

注意：自荐信**不**在这个清单里。2026-09-22 起它是**主动公开**的内容
（为了手机上能直接复制正文去投递），所以产物里带自荐信不算泄漏、不拦。
真正不可挽回的是投递记录（state）与密钥 —— 闸门只为这两样兜底。

用法
----
    python tools/check_public.py                 # 默认审计 dist-publish/
    python tools/check_public.py dist-publish    # 指定目录
    python tools/check_public.py --quiet         # 只在失败时输出
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

ALLOWED_FILES = {
    "index.html",
    "manifest.webmanifest",
    "sw.js",
    ".nojekyll",
    "icons/icon-192.png",
    "icons/icon-512.png",
    "icons/icon-maskable-512.png",
    "icons/apple-touch-icon.png",
}

# 出现在产物里就说明有东西泄漏了。
# 注意不要写 "data/state.json" 这类文件名 —— 界面的帮助文案里本来就会提到它，
# 那不是泄漏。真正的泄漏信号是「数据真的被内联了」，所以下面改为查 state_source 与正文。
FORBIDDEN_STRINGS = [
    "DEEPSEEK_API_KEY",
]

# 本机绝对路径的特征（产物里不该出现任何磁盘路径）
ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:\\\\|[A-Za-z]:\\|/Users/|/home/[a-z])")

DATA_RE = re.compile(r"window\.__DATA__ = (\{.*?\n\});", re.S)
META_RE = re.compile(r"window\.__META__ = (\{.*?\n\});", re.S)


def extract(html: str, pattern: re.Pattern) -> dict | None:
    m = pattern.search(html)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except json.JSONDecodeError:
        return None


def audit(publish_dir: Path) -> tuple[list[str], list[str]]:
    """返回 (致命问题, 警告)。致命问题非空 = 不该发布。"""
    errors: list[str] = []
    warnings: list[str] = []

    if not publish_dir.is_dir():
        return [f"目录不存在：{publish_dir}（先运行 python build.py --publish）"], []

    # ---------- 1 文件白名单 ----------
    found = sorted(
        str(p.relative_to(publish_dir)).replace("\\", "/")
        for p in publish_dir.rglob("*")
        if p.is_file()
    )
    extra = [f for f in found if f not in ALLOWED_FILES]
    missing = [f for f in sorted(ALLOWED_FILES) if f not in found]
    if extra:
        errors.append(
            "目录里出现了不该发布的内容：" + "、".join(extra)
            + "（含个人投递数据或自荐信正文，请只上传 build.py --publish 的产物）"
        )
    if missing:
        errors.append("缺少必需文件：" + "、".join(missing))

    index = publish_dir / "index.html"
    if not index.exists():
        return errors, warnings

    html = index.read_text(encoding="utf-8")

    # ---------- 2 内联数据 ----------
    data = extract(html, DATA_RE)
    if data is None:
        errors.append("index.html 里找不到合法的内联 __DATA__")
    else:
        apps = data.get("applications") or []
        if not apps:
            errors.append("内联岗位池为空")
        # 自荐信不查：2026-09-22 起它是主动公开的内容（见文件头说明）。

    meta = extract(html, META_RE)
    if meta is None:
        errors.append("index.html 里找不到合法的内联 __META__")
    else:
        if meta.get("state_inlined"):
            errors.append("产物内联了真实投递记录（state_inlined=true）")
        source = str(meta.get("state_source") or "")
        if source.startswith("file:"):
            errors.append(
                f"构建时读取了本地 state 文件（state_source={source}）—— "
                "说明这份产物是用 --state 或 data/state.json 构建的，不能发布"
            )
        if meta.get("demo"):
            errors.append("产物是演示版，不该发布（演示状态是虚构的投递记录）")

    # ---------- 3 敏感字符串 ----------
    for needle in FORBIDDEN_STRINGS:
        if needle in html:
            errors.append(f"产物里出现了 {needle}")
    if re.search(r"sk-[A-Za-z0-9]{16,}", html):
        errors.append("产物里出现了疑似 API key 的字符串")

    hit = ABSOLUTE_PATH_RE.search(html)
    if hit:
        warnings.append(f"产物里出现了疑似本机绝对路径：{hit.group(0)!r}")

    if "fetch(" in html or "XMLHttpRequest" in html:
        warnings.append("产物里出现了网络请求代码，请确认不是向外发送数据")

    # ---------- 4 PWA 资产 ----------
    if '<link rel="manifest"' not in html:
        errors.append("index.html 没有引用 manifest，装不成 PWA")
    if "apple-touch-icon" not in html:
        warnings.append("缺少 apple-touch-icon，iOS 添加到主屏幕会没有图标")

    manifest_path = publish_dir / "manifest.webmanifest"
    if manifest_path.exists():
        try:
            mf = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            errors.append(f"manifest.webmanifest 不是合法 JSON：{exc}")
        else:
            for key in ("name", "short_name", "start_url", "scope", "display", "icons"):
                if key not in mf:
                    errors.append(f"manifest 缺少字段 {key}")
            icons = mf.get("icons") or []
            purposes = {i.get("purpose", "any") for i in icons}
            if "maskable" not in purposes:
                warnings.append("manifest 没有 maskable 图标，Android 自适应图标会留白边")
            for icon in icons:
                rel = str(icon.get("src", "")).lstrip("./")
                if rel and not (publish_dir / rel).exists():
                    errors.append(f"manifest 引用的图标不存在：{icon.get('src')}")
            for key in ("start_url", "scope"):
                val = str(mf.get(key, ""))
                if val.startswith("/"):
                    errors.append(
                        f"manifest 的 {key} 是绝对路径（{val}）——部署在 GitHub Pages "
                        "子路径下会 404，必须用 ./ 相对写法"
                    )

    sw_path = publish_dir / "sw.js"
    if sw_path.exists():
        sw = sw_path.read_text(encoding="utf-8")
        if "__VERSION__" in sw:
            errors.append("sw.js 里的 __VERSION__ 没有被替换成版本号")
        if "skipWaiting" in sw and "install" in sw:
            # install 阶段直接 skipWaiting 会导致「旧页面 + 新缓存」错配
            m = re.search(r"addEventListener\('install'.*?\}\)", sw, re.S)
            if m and "skipWaiting()" in m.group(0):
                warnings.append("install 阶段直接 skipWaiting()，升级时可能出现页面与缓存版本错配")

    return errors, warnings


def main() -> None:
    ap = argparse.ArgumentParser(description="审计发布产物，确认不含个人数据")
    ap.add_argument("dir", nargs="?", default=str(ROOT / "dist-publish"),
                    help="要审计的目录，默认 dist-publish/")
    ap.add_argument("--quiet", action="store_true", help="只在失败时输出")
    args = ap.parse_args()

    target = Path(args.dir)
    errors, warnings = audit(target)

    if not args.quiet:
        print(f"审计 {target}")
        files = sorted(
            str(p.relative_to(target)).replace("\\", "/")
            for p in target.rglob("*") if p.is_file()
        ) if target.is_dir() else []
        print(f"  文件 {len(files)} 个：{', '.join(files)}")

    for w in warnings:
        print(f"  ⚠ {w}")

    if errors:
        print(f"\n✗ 审计未通过（{len(errors)} 项）")
        for e in errors:
            print(f"  · {e}")
        sys.exit(1)

    print("✓ 审计通过：产物不含个人数据，PWA 资产齐备")


if __name__ == "__main__":
    main()
