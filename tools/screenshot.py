#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 README 用的展示截图：全套、一键复现。

为什么要收敛成一个独立脚本
--------------------------
展示截图是仓库的门面，必须来自**干净的演示状态**。之前这批图是
`e2e.py` / `e2e_pwa.py` 的副产品 —— 跑一次测试，带测试痕迹的中间态就把
展示图覆盖了（`06-数据面板.png` 里还飘着「端到端测试公司 file 已恢复到
岗位池」的提示条）。现在只由这一处产出，测试脚本的截图落到 `dist/_shots/`
当作调试留档，两者不再互相踩。

例外：`12-升级提示.png` 仍由 `e2e_pwa.py` 产出 —— 那条提示只在"改 sw.js
触发真升级"的过程中出现，静态截图复现不出来，由测试留档是合理的。
`10-图标.png` 由 `make_icons.py --preview` 产出。

为什么走本地 http 而不是 file://
--------------------------------
安装入口由 `beforeinstallprompt` 驱动，只在 http(s) 下由浏览器派发；
`file://` 拿不到事件，就截不出「装到手机」。所以这里临时起一个只读服务。

用法
----
    python build.py --demo         # 先备好演示产物（含演示投递记录）
    python tools/screenshot.py     # → docs/shots/*.png，并汇报页面报错
    python tools/screenshot.py dist/index.html    # 换产物
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SHOTS = ROOT / "docs" / "shots"
DEFAULT_TARGET = ROOT / "dist" / "preview-demo.html"
VIEWPORT = {"width": 390, "height": 844}  # iPhone 14 逻辑分辨率
SCALE = 2  # 2 倍图，GitHub 上放大不糊
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36"
)

# 手动派发安装事件：真实浏览器要"用户确实在用这个站"之后才给，
# 截图不能等它，只能自己造一个（字段与真实事件一致）。
DISPATCH_INSTALL = """() => {
  const ev = new Event('beforeinstallprompt', { cancelable: true });
  ev.prompt = () => {};
  ev.userChoice = Promise.resolve({ outcome: 'accepted' });
  window.dispatchEvent(ev);
}"""

SCROLL_TO_SLAB = """el => {
  const slab = Array.from(el.querySelectorAll('.slab'))
    .find(s => s.textContent.indexOf('操作记录') === 0);
  if (slab) slab.scrollIntoView({ block: 'start' });
}"""


class _Quiet(http.server.SimpleHTTPRequestHandler):
    """截图不需要访问日志，静音掉，免得把结果淹掉。"""

    def log_message(self, *args):  # noqa: D102
        pass


def serve(target: Path) -> tuple[str, socketserver.TCPServer]:
    """在后台线程起一个只读静态服务，让页面跑在 http 下。"""
    handler = functools.partial(_Quiet, directory=str(target.parent))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/{target.name}", httpd


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    target = (ROOT / arg).resolve() if arg else DEFAULT_TARGET
    if not target.exists():
        raise SystemExit(f"找不到 {target}（先跑 python build.py --demo）")

    SHOTS.mkdir(parents=True, exist_ok=True)
    url, httpd = serve(target)
    taken: list[str] = []
    errors: list[str] = []

    def shoot(page, name: str) -> None:
        page.screenshot(path=str(SHOTS / f"{name}.png"))
        taken.append(name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(
            viewport=VIEWPORT, device_scale_factor=SCALE, user_agent=ANDROID_UA
        )
        page = ctx.new_page()
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        page.on(
            "console",
            lambda m: errors.append(f"[console.{m.type}] {m.text}")
            if m.type == "error"
            else None,
        )
        page.on("requestfailed", lambda r: errors.append(f"[requestfailed] {r.url}"))

        page.goto(url, wait_until="load")
        page.wait_for_timeout(500)

        # ---------------------------------------------------- 01-04 四个 Tab
        for tab, name in (
            ("today", "01-今日"),
            ("jobs", "02-岗位"),
            ("pipeline", "03-投递"),
            ("letters", "04-自荐信"),
        ):
            page.click(f'.tabbar button[data-tab="{tab}"]')
            page.wait_for_timeout(280)
            shoot(page, name)

        # ------------------------------------------- 05/07 岗位详情（两屏）
        page.click('.tabbar button[data-tab="jobs"]')
        page.wait_for_timeout(260)
        page.click(".view .card")
        page.wait_for_timeout(460)
        assert page.query_selector(".sheet"), "岗位详情面板没打开"
        shoot(page, "05-岗位详情")

        page.eval_on_selector(".sheet", SCROLL_TO_SLAB)
        page.wait_for_timeout(380)
        shoot(page, "07-详情操作区")
        page.click(".sheet .close")
        page.wait_for_timeout(260)

        # -------------------------------------------------- 08 手动新增岗位
        page.click("#btn-add")
        page.wait_for_timeout(400)
        assert page.query_selector(".sheet"), "新增岗位面板没打开"
        shoot(page, "08-新增岗位")
        page.click(".sheet .close")
        page.wait_for_timeout(260)

        # ------------------------------- 06 数据与设置（含「装到手机」段）
        page.click("#btn-more")
        page.wait_for_timeout(400)
        assert page.query_selector("[data-export-copy]"), "数据面板没打开"
        shoot(page, "06-数据面板")
        page.click(".sheet .close")
        page.wait_for_timeout(260)

        # -------------------------------------------- 09 S 级自荐信正文
        page.click('.tabbar button[data-tab="letters"]')
        page.wait_for_timeout(280)
        page.click(".view .row")
        page.wait_for_timeout(480)
        assert page.query_selector(".sheet .letter"), "自荐信正文没渲染"
        page.locator(".sheet .letter").scroll_into_view_if_needed()
        page.wait_for_timeout(340)
        shoot(page, "09-自荐信详情")
        page.click(".sheet .close")
        page.wait_for_timeout(260)

        # ------------------------------------------ 11 装到手机（应用内入口）
        page.click('.tabbar button[data-tab="today"]')
        page.wait_for_timeout(260)
        page.evaluate(DISPATCH_INSTALL)
        page.wait_for_timeout(320)
        assert page.query_selector(".installbar"), "安装提示条没出现"
        assert page.query_selector("#btn-install"), "顶栏安装按钮没出现"
        page.evaluate("() => window.scrollTo(0, 0)")
        page.wait_for_timeout(220)
        shoot(page, "11-装到手机")

        badge = page.query_selector("#tab-badge")
        badge_text = badge.inner_text() if badge else ""
        browser.close()

    httpd.shutdown()

    print(f"目标：{target.name}")
    print(f"截图 {len(taken)} 张 → {SHOTS}")
    print("   " + "、".join(n + ".png" for n in taken))
    print(f"底栏逾期角标：{badge_text or '（未显示）'}")
    print(f"console / pageerror：{len(errors)} 条")
    for line in errors:
        print("   " + line)
    if errors:
        raise SystemExit(f"存在 {len(errors)} 条页面报错，先修再截图")


if __name__ == "__main__":
    main()
