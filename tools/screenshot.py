#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""用真实浏览器打开构建产物，截图并收集 console 报错。

用途：每次改完界面跑一次，确认"构建通过"不等于"页面能跑"。
产物：docs/shots/*.png + 终端打印的 console 日志。

用法：
    python tools/screenshot.py                      # 默认截 dist/preview-demo.html
    python tools/screenshot.py dist/index.html
"""

from __future__ import annotations

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "shots"
VIEWPORT = {"width": 390, "height": 844}  # iPhone 14 逻辑分辨率


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "dist" / "preview-demo.html"
    target = (ROOT / target).resolve() if not target.is_absolute() else target
    if not target.exists():
        raise SystemExit(f"找不到构建产物：{target}（先跑 python build.py）")

    SHOTS.mkdir(parents=True, exist_ok=True)
    url = target.as_uri()

    logs: list[str] = []
    errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=2)

        page.on("console", lambda m: logs.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on("requestfailed", lambda r: errors.append(f"[requestfailed] {r.url}"))

        page.goto(url, wait_until="load")
        page.wait_for_timeout(400)

        tabs = [
            ("today", "01-今日"),
            ("jobs", "02-岗位"),
            ("pipeline", "03-投递"),
            ("letters", "04-自荐信"),
        ]
        for tab, name in tabs:
            page.click(f'.tabbar button[data-tab="{tab}"]')
            page.wait_for_timeout(220)
            page.screenshot(path=str(SHOTS / f"{name}.png"))

        # 岗位详情面板
        page.click('.tabbar button[data-tab="jobs"]')
        page.wait_for_timeout(220)
        page.click(".view .card")
        page.wait_for_timeout(400)
        page.screenshot(path=str(SHOTS / "05-岗位详情.png"))

        # 断言：详情面板确实打开了
        assert page.query_selector(".sheet"), "详情面板没有打开"
        assert page.query_selector(".sheet .meta-grid"), "详情面板缺少基本信息区"
        page.click(".sheet .close")
        page.wait_for_timeout(200)
        assert not page.query_selector(".mask"), "详情面板没有关闭"

        # 筛选 chip 是否生效
        page.click('.tabbar button[data-tab="jobs"]')
        page.wait_for_timeout(200)
        total_all = len(page.query_selector_all(".view .card"))
        page.click('.chip[data-tier="S"]')
        page.wait_for_timeout(200)
        total_s = len(page.query_selector_all(".view .card"))
        assert 0 < total_s < total_all, f"S 级筛选异常：全部 {total_all} 条，S 级 {total_s} 条"

        # S 级岗位详情：自荐信正文 + 复制入口
        page.click('.tabbar button[data-tab="letters"]')
        page.wait_for_timeout(220)
        page.click(".view .row")
        page.wait_for_timeout(420)
        assert page.query_selector(".sheet .letter"), "S 级岗位详情里没有渲染自荐信"
        page.locator(".sheet .letter").scroll_into_view_if_needed()
        page.wait_for_timeout(320)
        page.screenshot(path=str(SHOTS / "09-自荐信详情.png"))
        page.click(".sheet .close")
        page.wait_for_timeout(200)

        # 底栏角标
        badge = page.query_selector("#tab-badge")
        badge_text = badge.inner_text() if badge else ""

        browser.close()

    print(f"目标：{target.name}")
    print(f"页面规模：全部 {total_all} 张卡片，S 级筛选后 {total_s} 张")
    print(f"底栏逾期角标：{badge_text or '（未显示）'}")
    print(f"截图：{SHOTS}")
    print(f"console 消息 {len(logs)} 条，错误 {len(errors)} 条")
    for line in logs:
        print("  " + line)
    for line in errors:
        print("  " + line)
    if errors:
        raise SystemExit(f"存在 {len(errors)} 条页面错误，请修复")


if __name__ == "__main__":
    main()
