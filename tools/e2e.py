#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端行为验证：真实点击 → 真实存储 → 刷新页面 → 数据还在吗。

为什么必须单独有这么一个脚本
--------------------------
`python build.py` 只证明"能生成 HTML"，不证明"点得动、记得住"。
M1 那次就是靠浏览器验证抓到"详情面板关不掉"的 bug。
M2 的核心风险更高：状态改了没存住、刷新就回滚、备注写丢 —— 这些只跑构建是发现不了的。

两种环境都跑
------------
1. file://  —— 双击打开的本地场景（个人版就是这种用法）
2. http://  —— 部署到 GitHub Pages 后的场景

这两个环境下 localStorage 的行为不一样，必须分别验证，不能想当然。

用法
----
    python tools/e2e.py            # 两种环境都测
    python tools/e2e.py file       # 只测 file://
    python tools/e2e.py http       # 只测 http://
"""

from __future__ import annotations

import datetime as dt
import functools
import http.server
import socketserver
import sys
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
SHOTS = ROOT / "docs" / "shots"
TARGET = DIST / "preview-demo.html"
VIEWPORT = {"width": 390, "height": 844}
TODAY = dt.date.today().isoformat()

S_JOB = "liaoning-xinxi-ae"  # 种子里的 S 级岗位，用来做状态流转测试
A_JOB = "pingan-health-ai"   # 种子里的 A 级岗位（演示状态里是「沟通中」）


class Report:
    def __init__(self, env: str):
        self.env = env
        self.passed: list[str] = []
        self.failed: list[str] = []

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed.append(name)
            print(f"  ✓ {name}" + (f"　{detail}" if detail else ""))
        else:
            self.failed.append(name)
            print(f"  ✗ {name}" + (f"　{detail}" if detail else ""))
        return cond


def serve_dist() -> tuple[str, socketserver.TCPServer]:
    """在后台线程起一个只读静态服务，模拟部署后的 http 环境。"""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(DIST))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}/preview-demo.html", httpd


def run_suite(page, r: Report, tag: str) -> None:
    """一套行为断言，file:// 和 http:// 各跑一遍。tag 用于区分截图文件名。"""

    # ---------------------------------------------------------- 0 首屏
    todos = len(page.query_selector_all(".todos .todo"))
    kpis = len(page.query_selector_all(".kpi"))
    r.check("首屏「今天要做」有内容", todos > 0, f"{todos} 项待办")
    r.check("总览 KPI 四项齐全", kpis == 4)

    # ---------------------------------------------------------- 1 存储可用性
    ls_ok = page.evaluate("(() => { try { localStorage.setItem('__t__','1');"
                          "localStorage.removeItem('__t__'); return true; } catch (e) { return false; } })()")
    r.check("localStorage 可写", ls_ok)
    r.check("应用脚本已挂载", page.evaluate("Boolean(window.__APP__)"))

    # ---------------------------------------------------------- 2 状态流转
    page.click(f'.card[data-id="{S_JOB}"]')
    page.wait_for_timeout(260)
    r.check("详情面板能打开", bool(page.query_selector(".sheet")))

    before = page.inner_text(".stchip.on")
    page.click('.stchip[data-set-status="applied"]')
    page.wait_for_timeout(260)
    after = page.inner_text(".stchip.on")
    r.check("状态流转生效", before == "待投递" and after == "已投递", f"{before} → {after}")

    tl_count = len(page.query_selector_all(".tl li"))
    r.check("操作记录自动写入", tl_count >= 1, f"{tl_count} 条")

    applied_at = page.evaluate(
        "(() => { const j = window.__APP__.jobs().find(x => x.id === '%s');"
        "return j ? (j.applied_at || '') : ''; })()" % S_JOB
    )
    r.check("自动记录投递日期为今天", applied_at == TODAY, f"applied_at={applied_at}")

    # ---------------------------------------------------------- 3 备注与跟进
    page.fill('[data-field="notes"]', "e2e 写入的备注：" + tag)
    page.fill('[data-field="next_action"]', "等 HR 回复")
    page.fill('[data-field="next_action_at"]', "2026-09-25")
    page.click(".sheet h2")          # 触发 blur → 保存
    page.wait_for_timeout(200)

    page.click(".sheet .close")
    page.wait_for_timeout(200)

    # ---------------------------------------------------------- 4 刷新后仍在（M2 的核心）
    page.reload(wait_until="load")
    page.wait_for_timeout(400)
    page.click('.tabbar button[data-tab="jobs"]')
    page.wait_for_timeout(250)
    card_status = page.inner_text(f'.card[data-id="{S_JOB}"] .tag.st')
    r.check("刷新后状态仍是「已投递」", card_status == "已投递", f"实际 {card_status}")

    page.click(f'.card[data-id="{S_JOB}"]')
    page.wait_for_timeout(260)
    notes = page.input_value('[data-field="notes"]')
    r.check("刷新后备注没丢", notes == "e2e 写入的备注：" + tag)
    na = page.input_value('[data-field="next_action"]')
    r.check("刷新后跟进动作没丢", na == "等 HR 回复")

    # ---------------------------------------------------------- 5 撤销
    page.click('[data-undo]')
    page.wait_for_timeout(260)
    undone = page.inner_text(".stchip.on")
    r.check("撤销上一步回到「待投递」", undone == "待投递", f"实际 {undone}")

    # 撤回来再推上去，恢复测试现场
    page.click('.stchip[data-set-status="applied"]')
    page.wait_for_timeout(240)
    page.click(".sheet .close")
    page.wait_for_timeout(200)

    # ---------------------------------------------------------- 6 手动新增岗位
    page.click("#btn-add")
    page.wait_for_timeout(260)
    r.check("新增岗位面板打开", bool(page.query_selector("#f-company")))
    page.fill("#f-company", f"端到端测试公司{tag}")
    page.fill("#f-title", "AI 应用开发工程师")
    page.fill("#f-salary", "15-20k")
    page.click("[data-add-submit]")
    page.wait_for_timeout(320)
    added = page.evaluate("window.__APP__.state().added.map(j => j.company).join(',')")
    r.check("手动岗位已写入本地", f"端到端测试公司{tag}" in added, added[:60])

    if page.query_selector(".sheet"):        # 新增后面板会自动打开该岗位
        page.click(".sheet .close")
        page.wait_for_timeout(200)

    # ---------------------------------------------------------- 7 归档与恢复
    page.click('.tabbar button[data-tab="jobs"]')
    page.wait_for_timeout(220)
    page.click('.chip[data-tier="—"]')
    page.wait_for_timeout(240)
    manual_cards = page.query_selector_all(".view .card")
    r.check("手动岗位显示在「待评估」档", len(manual_cards) >= 1, f"{len(manual_cards)} 条")

    page.click(".view .card")
    page.wait_for_timeout(260)
    page.click("[data-archive]")
    page.wait_for_timeout(320)
    removed_n = page.evaluate("window.__APP__.state().removed.length")
    r.check("归档写入 removed", removed_n >= 1, f"{removed_n} 条")
    r.check("归档后不再出现在岗位池",
            page.evaluate("window.__APP__.jobs().some(j => j._manual)" ) is False)

    page.click('.tabbar button[data-tab="pipeline"]')
    page.wait_for_timeout(240)
    page.click('.chip[data-pipe="archived"]')
    page.wait_for_timeout(240)
    arch_rows = page.query_selector_all(".view .row")
    r.check("「已归档」档能看到归档项", len(arch_rows) >= 1, f"{len(arch_rows)} 条")

    page.click(".view .row")
    page.wait_for_timeout(260)
    page.click("[data-restore]")
    page.wait_for_timeout(320)
    removed_after = page.evaluate("window.__APP__.state().removed.length")
    r.check("从归档恢复", removed_after == removed_n - 1, f"removed {removed_n} → {removed_after}")

    # ---------------------------------------------------------- 8 导出
    page.click("#btn-more")
    page.wait_for_timeout(260)
    r.check("数据面板打开", bool(page.query_selector("[data-export-copy]")))
    payload = page.evaluate("JSON.parse(window.__APP__.exportText())")
    r.check("导出结构合法", payload.get("schema") == 1 and "patches" in payload)
    r.check("导出含状态变更", S_JOB in (payload.get("patches") or {}),
            f"patches={len(payload.get('patches') or {})}")
    r.check("导出含手动岗位", len(payload.get("added") or []) >= 1)

    if tag == "file":
        page.screenshot(path=str(SHOTS / "06-数据面板.png"))

    # ---------------------------------------------------------- 9 清空
    page.once("dialog", lambda d: d.accept())
    page.click("[data-reset]")
    page.wait_for_timeout(400)
    cleared = page.evaluate("Object.keys(window.__APP__.state().patches).length")
    r.check("清空本地记录", cleared == 0, f"剩余 {cleared} 条补丁")

    # ---------------------------------------------------------- 10 PWA 隔离
    # 演示版没有 manifest 引用，因此不该注册 Service Worker。
    # 否则同一目录下的 preview-demo.html 会与 index.html 争抢 SW scope，
    # 导航回退时互相顶掉对方的页面。
    pwa = page.evaluate("window.__APP__.pwa()")
    if tag == "http":
        r.check("演示版不注册 Service Worker（与 index.html 隔离）",
                pwa.get("supported") is True and pwa.get("controlled") is False,
                f"controlled={pwa.get('controlled')}")
    else:
        r.check("file:// 下不尝试注册 Service Worker",
                pwa.get("protocol") == "file:" and pwa.get("controlled") is False)


def main() -> None:
    if not TARGET.exists():
        raise SystemExit(f"找不到 {TARGET}，先跑 python build.py --demo")

    only = sys.argv[1] if len(sys.argv) > 1 else "both"
    SHOTS.mkdir(parents=True, exist_ok=True)

    envs: list[tuple[str, str]] = []
    if only in ("file", "both"):
        envs.append(("file", TARGET.as_uri()))
    httpd = None
    if only in ("http", "both"):
        url, httpd = serve_dist()
        envs.append(("http", url))

    all_failed = 0
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for tag, url in envs:
            print(f"\n=== 环境：{tag}  ({url}) ===")
            ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
            page.on("console", lambda m: errors.append(f"[console.{m.type}] {m.text}")
                    if m.type == "error" else None)
            page.on("requestfailed", lambda rq: errors.append(f"[requestfailed] {rq.url}"))

            r = Report(tag)
            try:
                page.goto(url, wait_until="load")
                page.wait_for_timeout(400)
                run_suite(page, r, tag)
            except Exception as exc:  # noqa: BLE001
                r.failed.append(f"脚本异常：{exc}")
                print(f"  ✗ 脚本异常：{exc}")

            if errors:
                r.failed.append("页面报错")
                for line in errors:
                    print(f"  ✗ {line}")
            else:
                r.passed.append("console / pageerror 零报错")
                print("  ✓ console / pageerror 零报错")

            print(f"  ── {tag}：{len(r.passed)} 通过，{len(r.failed)} 失败")
            all_failed += len(r.failed)
            ctx.close()

        # 视觉截图（干净的演示状态，专门给「操作区」和「新增岗位」留档）
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = ctx.new_page()
        page.goto(TARGET.as_uri(), wait_until="load")
        page.wait_for_timeout(400)

        page.click(f'.tabbar button[data-tab="jobs"]')
        page.wait_for_timeout(250)
        page.click(f'.card[data-id="{A_JOB}"]')
        page.wait_for_timeout(320)
        page.screenshot(path=str(SHOTS / "07-详情操作区.png"))
        page.click(".sheet .close")
        page.wait_for_timeout(220)

        page.click("#btn-add")
        page.wait_for_timeout(320)
        page.screenshot(path=str(SHOTS / "08-新增岗位.png"))
        ctx.close()
        browser.close()

    if httpd:
        httpd.shutdown()

    print(f"\n{'=' * 46}")
    if all_failed:
        raise SystemExit(f"端到端验证失败：{all_failed} 项未通过")
    print("端到端验证全部通过")


if __name__ == "__main__":
    main()
