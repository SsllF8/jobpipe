#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 PWA 图标（PNG）。

为什么用浏览器渲染而不是绘图库
------------------------------
系统里没有 Pillow，也不该为了四张图标引一个新依赖。
Playwright 本来就是这个项目的验证工具，用它把 SVG 渲染成精确尺寸的 PNG，
既零新增依赖，又能保证图标和界面出自同一套设计 token。

为什么图标要提交进仓库
----------------------
构建（`build.py`）只负责把 src/icons/*.png 拷进 dist，
这样 CI 和别人的机器上不需要 Playwright 也能构建出完整产物。

尺寸与用途
----------
    icon-192.png            192  Android 主屏 / 通用
    icon-512.png            512  Android 启动画面 / 商店
    icon-maskable-512.png   512  Android 自适应图标（内容缩到安全区内，满幅底色）
    apple-touch-icon.png    180  iOS「添加到主屏幕」（必须是方角、不透明）

用法
----
    python tools/make_icons.py            # 生成全部
    python tools/make_icons.py --preview  # 额外导出一张拼版预览图便于肉眼检查
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "src" / "icons"

# 与 src/style.css 的 :root token 保持一致
BG = "#0a0a0a"
PANEL = "#111111"
LINE = "#262626"
ACCENT = "#c41e3a"

FONT_STACK = (
    "Microsoft YaHei, Microsoft YaHei UI, DengXian, PingFang SC, "
    "Noto Sans SC, Source Han Sans SC, SimHei, sans-serif"
)

# 四张图的规格：文件名 / 边长 / 是否圆角 / 字形占比（相对边长）
SPECS: list[tuple[str, int, bool, float]] = [
    ("icon-192.png", 192, True, 0.60),
    ("icon-512.png", 512, True, 0.60),
    # maskable：内容必须落在中心 80% 安全区内，且底色铺满（裁切后才不会露白边）
    ("icon-maskable-512.png", 512, False, 0.44),
    # iOS 自己会加圆角，所以这里给方角原图
    ("apple-touch-icon.png", 180, False, 0.58),
]


def build_svg(size: int, rounded: bool, glyph_ratio: float) -> str:
    """按尺寸生成一张图标 SVG。

    构图只有三样：纯黑底、一圈深灰细边框、居中的红色「职」。
    曾经在字下加过一道红杠，但和「职」的末横糊成一团、放大看像脏点，
    按「删减元素、聚焦核心」砍掉了 —— 一个字足够，多一笔都是噪音。
    """
    r = size * 0.22 if rounded else 0
    inset = size * 0.05
    glyph = size * glyph_ratio
    # CJK 字形在 em 框里视觉重心偏上，dominant-baseline=central 后再微调下移
    cy = size * 0.535

    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"
     viewBox="0 0 {size} {size}" shape-rendering="geometricPrecision">
  <rect x="0" y="0" width="{size}" height="{size}" rx="{r:.2f}" fill="{BG}"/>
  <rect x="{inset:.2f}" y="{inset:.2f}"
        width="{size - inset * 2:.2f}" height="{size - inset * 2:.2f}"
        rx="{max(0.0, r - inset * 0.5):.2f}"
        fill="{PANEL}" stroke="{LINE}" stroke-width="{max(1.0, size / 128):.2f}"/>
  <text x="{size / 2:.2f}" y="{cy:.2f}" font-family="{FONT_STACK}"
        font-size="{glyph:.2f}" font-weight="700" fill="{ACCENT}"
        text-anchor="middle" dominant-baseline="central">{'职'}</text>
</svg>"""


def render(sizes: list[tuple[str, int, bool, float]], out_dir: Path) -> list[Path]:
    from playwright.sync_api import sync_playwright

    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, size, rounded, ratio in sizes:
            ctx = browser.new_context(
                viewport={"width": size, "height": size},
                device_scale_factor=1,
            )
            page = ctx.new_page()
            page.set_content(
                '<html><head><meta charset="utf-8">'
                f'<style>html,body{{margin:0;padding:0;width:{size}px;height:{size}px;'
                "overflow:hidden;background:transparent}}</style></head>"
                f"<body>{build_svg(size, rounded, ratio)}</body></html>",
                wait_until="load",
            )
            # 等字体真正就位，否则可能截到 fallback 字形
            page.wait_for_timeout(180)
            target = out_dir / name
            page.screenshot(path=str(target))  # 视口即画布，尺寸精确
            written.append(target)
            print(f"  {name:<26} {size}×{size}  {target.stat().st_size / 1024:.1f} KB")
            ctx.close()
        browser.close()

    return written


def render_preview(out: Path) -> Path:
    """把四张图拼成一张预览图，方便一眼看出是否糊、是否居中。

    图片必须以 base64 内联：`set_content` 出来的页面是 about:blank，
    用 file:// 引本地图片会被浏览器拦掉，只剩一排碎图。
    """
    import base64
    from playwright.sync_api import sync_playwright

    def data_uri(p: Path) -> str:
        return "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode("ascii")

    cells = "".join(
        f'<figure><img src="{data_uri(OUT_DIR / name)}" width="192" height="192" alt="{name}">'
        f"<figcaption>{name}<span>{size}×{size}</span></figcaption></figure>"
        for name, size, *_ in SPECS
    )
    html = f"""<html><head><meta charset="utf-8"><style>
    body{{margin:0;padding:26px;background:#151515;display:flex;gap:22px;
         font:600 12px {FONT_STACK};color:#cfcfcf}}
    figure{{margin:0;text-align:center}}
    img{{display:block;border-radius:6px}}
    figcaption{{margin-top:9px;display:flex;flex-direction:column;gap:3px}}
    figcaption span{{font-weight:400;color:#7a7a7a}}
    </style></head><body>{cells}</body></html>"""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1000, "height": 300})
        page.set_content(html, wait_until="load")
        page.wait_for_timeout(300)
        page.screenshot(path=str(out), full_page=True)
        browser.close()
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="生成 PWA 图标 PNG")
    ap.add_argument("--out", default=str(OUT_DIR), help="输出目录，默认 src/icons")
    ap.add_argument("--preview", action="store_true", help="额外导出拼版预览图")
    args = ap.parse_args()

    out_dir = Path(args.out)
    print(f"渲染图标 → {out_dir}")
    written = render(SPECS, out_dir)
    if len(written) != len(SPECS):
        sys.exit("图标数量不符，渲染可能中途失败")

    if args.preview:
        prev = render_preview(ROOT / "docs" / "shots" / "10-图标.png")
        print(f"预览图 → {prev}")

    print(f"完成，共 {len(written)} 张")


if __name__ == "__main__":
    main()
