#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装入口端到端验证：应用内那个「装到手机」到底能不能用。

为什么要有这个脚本
------------------
真实反馈过一句话：「菜单中没有安装应用」。
可站点本身并没有问题 —— Chrome 的安装资格接口返回零错误、manifest 解析无误，
图标齐全、Service Worker 正常接管。问题在于**入口长在浏览器菜单里，而人翻不到**：
Chrome 要等它自己认定你确实在用这个站，才肯把「安装应用」放进菜单；
国产 ROM 的定制浏览器、微信内置浏览器则干脆没有这一项。

所以把入口搬进应用内部：接管 beforeinstallprompt，自己给提示条 + 顶栏常驻按钮。
本脚本验证的就是这套接管逻辑在真浏览器里确实成立，而不是「代码看起来写了」。

顺带向 Chrome 要一次安装资格结论（CDP Page.getInstallabilityErrors）——
线上排查这类问题时，这比逐条读 manifest 快得多。

覆盖的六个失败模式
------------------
1. 事件没接上（绑太晚 / 事件名写错）      → 断言提示条与顶栏按钮真的出现
2. 关掉之后反复骚扰                      → 断言不再弹提示条，但顶栏按钮保留
3. 点了安装却什么都没发生（deferred 丢了）→ 断言 prompt() 真被调用到
4. 用户拒绝后留一个点了没反应的按钮      → 断言入口全部收掉且状态落盘
5. 已经装成 App 了还在引导安装            → 断言 standalone 模式下入口完全不出现
6. 手动步骤文案串了平台                  → 按 Android / iPhone / 微信三种 UA 各验一次

第 6 条不是凑数：这三个平台的路径完全不同（Chrome 菜单 / Safari 分享 / 先跳出微信），
写错一个，等于对那个平台的人完全没写。

用法
----
    python build.py --publish        # 先产出 dist-publish
    python tools/e2e_install.py      # 完整跑一遍
"""

from __future__ import annotations

import functools
import http.server
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / "dist-publish"
VIEWPORT = {"width": 390, "height": 844}   # 手机尺寸：桌面宽度下顶栏按钮的排布不是真实场景

UA_ANDROID = ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36")
UA_IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1")
UA_WECHAT = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49(0x18003128)")

# 浏览器自己派发的 beforeinstallprompt 只在 https + 满足一堆条件时才来，
# 本地跑不到。这里照它的接口手造一个：我们的代码只用到 preventDefault /
# prompt / userChoice 三个成员，事件对象上挂得出来就能真跑通整条路径。
DISPATCH = """() => {
  const ev = new Event('beforeinstallprompt', { cancelable: true });
  ev.prompt = () => { window.__PROMPTED__ = (window.__PROMPTED__ || 0) + 1; };
  ev.userChoice = Promise.resolve({ outcome: window.__CHOICE__ || 'accepted' });
  window.dispatchEvent(ev);
}"""

# 每个文档都先跑它：让 chosen 分支不受 reload 影响
SET_CHOICE = "window.__CHOICE__ = '%s';"


class Report:
    def __init__(self) -> None:
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


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:      # noqa: D102 —— 静音，输出留给断言
        pass


def serve(directory: Path) -> tuple[socketserver.ThreadingTCPServer, str]:
    handler = functools.partial(QuietHandler, directory=str(directory))
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/index.html"


def pwa_state(page) -> dict:
    return page.evaluate("() => window.__APP__.pwa()")


def main() -> int:
    if not (PUBLISH / "index.html").exists():
        print("找不到 dist-publish/index.html —— 先跑 python build.py --publish")
        return 2

    rep = Report()
    srv, url = serve(PUBLISH)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT)
        page = ctx.new_page()
        errors: list[str] = []
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"PAGEERROR: {e}"))

        try:
            # ---------------------------------------------- 0 站点安装资格
            print("\n[0] Chrome 认为这个站能装吗")
            page.goto(url, wait_until="load")
            page.wait_for_timeout(1200)
            cdp = ctx.new_cdp_session(page)
            cdp.send("Page.enable")
            errs = cdp.send("Page.getInstallabilityErrors").get("installabilityErrors", [])
            man = cdp.send("Page.getAppManifest")
            rep.check("安装资格零错误（Chrome 官方判定）", not errs, f"errors={errs}")
            rep.check("manifest 解析无错误", not man.get("errors"), f"url={man.get('url')}")

            # ---------------------------------------------- 1 入口出现
            print("\n[1] 浏览器交出安装权之后，入口出现")
            st = pwa_state(page)
            rep.check("初始不显示任何安装入口",
                      not st["installBarShown"] and not st["installButtonShown"])

            page.evaluate(DISPATCH)
            page.wait_for_timeout(250)
            st = pwa_state(page)
            rep.check("提示条出现", st["installBarShown"])
            rep.check("顶栏按钮出现", st["installButtonShown"])
            rep.check("deferred 事件已保存", st["installAvailable"])
            bar_text = page.inner_text(".installbar span")
            rep.check("提示条文案说清了目的", "装到手机" in bar_text, bar_text)

            # ---------------------------------------------- 2 关掉不再骚扰
            print("\n[2] 点「以后再说」之后不该反复弹")
            page.click(".installbar [data-install-x]")
            page.wait_for_timeout(250)
            st = pwa_state(page)
            rep.check("提示条收掉", not st["installBarShown"])
            rep.check("顶栏按钮保留（想装还能装）", st["installButtonShown"])
            rep.check("忽略状态已落盘", st["installDismissed"])

            page.reload(wait_until="load")
            page.wait_for_timeout(500)
            page.evaluate(DISPATCH)
            page.wait_for_timeout(250)
            st = pwa_state(page)
            rep.check("重载后不再弹提示条", not st["installBarShown"])
            rep.check("重载后顶栏按钮仍在", st["installButtonShown"])

            # ---------------------------------------------- 3 接受安装
            print("\n[3] 点「安装」要真的调用系统安装框")
            page.evaluate(SET_CHOICE % "accepted")
            page.evaluate("() => localStorage.removeItem('jobpipe.install.dismissed')")
            page.reload(wait_until="load")
            page.wait_for_timeout(500)
            page.evaluate(DISPATCH)
            page.wait_for_timeout(250)
            page.click(".installbar [data-install-go]")
            page.wait_for_timeout(400)
            prompted = page.evaluate("() => window.__PROMPTED__ || 0")
            st = pwa_state(page)
            rep.check("prompt() 被调用了一次", prompted == 1, f"次数={prompted}")
            rep.check("接受后入口全部收掉",
                      not st["installBarShown"] and not st["installButtonShown"])
            rep.check("接受后有反馈（toast）", page.query_selector(".toast") is not None)

            # ---------------------------------------------- 4 拒绝安装
            print("\n[4] 用户拒绝后不该留一个点了没反应的按钮")
            page.evaluate("() => localStorage.removeItem('jobpipe.install.dismissed')")
            page.add_init_script(SET_CHOICE % "dismissed")
            page.reload(wait_until="load")
            page.wait_for_timeout(500)
            page.evaluate(DISPATCH)
            page.wait_for_timeout(250)
            page.click(".installbar [data-install-go]")
            page.wait_for_timeout(400)
            st = pwa_state(page)
            rep.check("拒绝后入口全部收掉",
                      not st["installBarShown"] and not st["installButtonShown"])
            rep.check("拒绝状态已落盘", st["installDismissed"])
            ctx.close()

            # ---------------------------------------------- 5 已装成 App
            print("\n[5] 已经装成 App 了就不该再引导安装")
            ctx2 = browser.new_context(viewport=VIEWPORT)
            # 注意：init script 是按原样执行的语句，不能包成 `() => {...}` ——
            # 那样只是一个没被调用的函数字面量，什么都不会发生。
            ctx2.add_init_script("""
              const origMatchMedia = window.matchMedia;
              window.matchMedia = function (q) {
                if (String(q).indexOf('display-mode') >= 0)
                  return { matches: true, media: q, addEventListener() {}, removeEventListener() {} };
                return origMatchMedia.call(window, q);
              };
            """)
            page2 = ctx2.new_page()
            page2.goto(url, wait_until="load")
            page2.wait_for_timeout(600)
            page2.evaluate(DISPATCH)
            page2.wait_for_timeout(250)
            st2 = pwa_state(page2)
            rep.check("standalone 模式被识别", st2["standalone"])
            rep.check("提示条不出现", not st2["installBarShown"])
            rep.check("顶栏按钮不出现", not st2["installButtonShown"])
            page2.click("#btn-more")
            page2.wait_for_timeout(300)
            rep.check("设置面板改说「已在以 App 方式运行」",
                      "以 App 方式运行" in page2.inner_text(".sheet"))
            ctx2.close()

            # ---------------------------------------------- 6 平台文案
            print("\n[6] 手动步骤文案必须按平台给对")
            platforms = [
                ("Android", UA_ANDROID, "安装应用",  "Chrome 菜单里的名字"),
                ("iPhone",  UA_IPHONE,  "添加到主屏幕", "Safari 分享菜单里的名字"),
                ("微信",     UA_WECHAT,  "在浏览器中打开", "微信里得先跳出去"),
            ]
            for label, ua, keyword, why in platforms:
                c = browser.new_context(viewport=VIEWPORT, user_agent=ua)
                pg = c.new_page()
                pg.goto(url, wait_until="load")
                pg.wait_for_timeout(500)
                pg.click("#btn-more")
                pg.wait_for_timeout(300)
                txt = pg.inner_text(".sheet")
                rep.check(f"{label} 面板给对了路径（含「{keyword}」）", keyword in txt, why)
                c.close()

            print("\n[7] 全程 console 应当干净")
            rep.check("无 console 错误", not errors, str(errors[:3]) if errors else "")
        finally:
            browser.close()
            srv.shutdown()

    total = len(rep.passed) + len(rep.failed)
    print("\n" + "─" * 60)
    if rep.failed:
        print(f"失败 {len(rep.failed)}/{total}：")
        for name in rep.failed:
            print(f"  ✗ {name}")
        return 1
    print(f"全部通过：{total} 项断言")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
