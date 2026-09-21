#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PWA 端到端验证：Service Worker 真的接管了吗？断网还能打开吗？升级提示会弹吗？

为什么不能只靠单元测试
----------------------
单元测试能证明 `sw.js` 里写着 `addEventListener('install')`，
但证明不了浏览器**真的装上了它、真的接管了页面、真的能在断网时把页面端出来**。
过去两轮的教训一致：`构建通过` / `字段正确` 和 `浏览器里真的能用` 是两件事
（M1 的详情面板关不掉、M2 的归档后彻底消失，都是跑起来才发现的）。

四个真实的失败模式，正好对应下面四组断言
----------------------------------------
1. SW 注册被静默拒绝（非 https / 作用域不对 / 脚本 404）→ 断言注册成功且已控制页面
2. 预缓存清单写错（相对路径写成绝对路径，部署在子目录下就 404）
   → 断言缓存里 7 个条目的 URL 全部真实存在
3. 只"注册了"却没有可用缓存 → 断言断网后整页仍能渲染，而不是白屏
4. 升级链条断掉（装上了新 SW 但页面不提示、点了不重载）
   → 真改一次产物，断言提示条出现、点击后拿到新内容

用法
----
    python tools/e2e_pwa.py            # 完整跑一遍
    python tools/e2e_pwa.py --keep     # 保留临时服务目录便于排查
"""

from __future__ import annotations

import argparse
import functools
import http.server
import re
import shutil
import socketserver
import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
PUBLISH = ROOT / "dist-publish"
SERVE = ROOT / ".pwa_tmp"
SHOTS = ROOT / "docs" / "shots"
VIEWPORT = {"width": 390, "height": 844}

PRECACHE_EXPECT = [
    "/index.html",
    "/manifest.webmanifest",
    "/icons/icon-192.png",
    "/icons/icon-512.png",
    "/icons/icon-maskable-512.png",
    "/icons/apple-touch-icon.png",
]


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


class ManifestHandler(http.server.SimpleHTTPRequestHandler):
    """静态服务，按 GitHub Pages 的行为给出 .webmanifest 的 MIME 类型。

    必须定义成子类：extensions_map 是**类**属性。设到 functools.partial 上
    会直接 AttributeError，而且就算能设也只影响这一个实例，测出来的
    Content-Type 会和线上不一致。

    另外要丢掉 If-Modified-Since —— 这个覆盖不是为了让测试好看，是为了让测试
    说真话：SimpleHTTPRequestHandler 的 Last-Modified 只有**秒**精度，
    而升级测试需要在同一秒内连改两次 sw.js。第二次请求会命中
    If-Modified-Since 拿到 304，浏览器据此认定「脚本没变」而跳过安装，
    于是失败信息变成「没检测到新版本」—— 一个纯粹由测试台造出来的假故障。
    真实的 GitHub Pages 用内容哈希做 ETag，内容一变必然返回新字节，
    所以这里模拟的是「永不 304」，反而更贴近线上。
    """
    extensions_map = {
        **http.server.SimpleHTTPRequestHandler.extensions_map,
        ".webmanifest": "application/manifest+json",
    }

    def send_head(self):
        if "If-Modified-Since" in self.headers:
            del self.headers["If-Modified-Since"]
        return super().send_head()

    def log_message(self, *args):  # 静音访问日志，输出只留断言结果
        pass


def serve(directory: Path) -> tuple[str, socketserver.TCPServer]:
    """在后台线程起静态服务，模拟 GitHub Pages。端口随机，避免和别的服务撞。"""
    handler = functools.partial(ManifestHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{httpd.server_address[1]}", httpd


def sw_version(serve_dir: Path) -> str:
    m = re.search(r"var VERSION = '([^']+)'", (serve_dir / "sw.js").read_text(encoding="utf-8"))
    return m.group(1) if m else ""


def wait_for_controller(page, timeout_ms: int = 12000) -> bool:
    """等 SW 真正接管页面（controller 非空）。

    注意区分两件事：注册成功（registration 存在）≠ 已控制页面。
    首次注册时还要等 install → activate → clients.claim() 走完。
    """
    try:
        page.wait_for_function(
            "() => navigator.serviceWorker && navigator.serviceWorker.controller",
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


def run(r: Report, serve_dir: Path, keep: bool) -> None:
    base, httpd = serve(serve_dir)
    version = sw_version(serve_dir)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2)
        page = ctx.new_page()

        console_errors: list[str] = []
        page.on("pageerror", lambda e: console_errors.append(f"[pageerror] {e}"))
        page.on("console", lambda m: console_errors.append(f"[console.error] {m.text}")
                if m.type == "error" else None)

        served_by_sw: list[str] = []
        page.on("response", lambda resp: served_by_sw.append(resp.url)
                if resp.from_service_worker else None)

        # ------------------------------------------------------ 1 首次加载
        page.goto(base + "/", wait_until="load")
        page.wait_for_timeout(500)

        r.check("页面能打开", bool(page.query_selector(".tabbar")))
        r.check("base 指向了 index.html（不是目录列表）",
                page.query_selector(".brand") is not None,
                page.title())

        # ------------------------------------------------------ 2 manifest
        mf_resp = page.request.get(base + "/manifest.webmanifest")
        r.check("manifest 可获取", mf_resp.status == 200, f"HTTP {mf_resp.status}")
        mf_ok = False
        try:
            mf = mf_resp.json()
            mf_ok = (mf.get("display") == "standalone"
                     and mf.get("start_url") == "./"
                     and len(mf.get("icons") or []) >= 3)
        except Exception as exc:  # noqa: BLE001
            print(f"    manifest 解析失败：{exc}")
        r.check("manifest 字段合法（standalone / 相对 start_url / 图标齐全）", mf_ok)

        icon_resp = page.request.get(base + "/icons/icon-512.png")
        r.check("图标可获取", icon_resp.status == 200 and len(icon_resp.body()) > 1000,
                f"HTTP {icon_resp.status}, {len(icon_resp.body()) // 1024} KB")

        # ------------------------------------------------------ 3 SW 注册与接管
        r.check("Service Worker 已注册",
                bool(page.evaluate("navigator.serviceWorker.getRegistration()"
                                   ".then(r => Boolean(r))")))
        r.check("Service Worker 已接管页面", wait_for_controller(page))

        st = page.evaluate("window.__APP__.pwa()")
        r.check("应用层确认已受控", st.get("controlled") is True, f"version={st.get('version')}")
        r.check("SW 回报的版本号与产物一致", st.get("version") == version,
                f"页面 {st.get('version')} / 文件 {version}")

        # ------------------------------------------------------ 4 缓存内容
        info = page.evaluate(
            """async () => {
                 const names = await caches.keys();
                 const name = names.find(n => n.startsWith('jobpipe-'));
                 if (!name) return { names, name: null, urls: [] };
                 const cache = await caches.open(name);
                 const reqs = await cache.keys();
                 return { names, name, urls: reqs.map(r => new URL(r.url).pathname) };
               }"""
        )
        r.check("缓存已建立", bool(info.get("name")), f"缓存名 {info.get('name')}")
        r.check("缓存名带版本号（换版本即失效）", str(info.get("name")).endswith(version),
                str(info.get("name")))

        cached = set(info.get("urls") or [])
        r.check("首页已预缓存", "/index.html" in cached, f"{len(cached)} 个条目")
        missing = [u for u in PRECACHE_EXPECT if u not in cached]
        r.check("预缓存清单全部命中（相对路径没写错）", not missing, f"缺 {missing}" if missing else "")

        # ------------------------------------------------------ 5 断网仍可用（核心）
        ctx.set_offline(True)
        offline_ok = False
        offline_cards = 0
        try:
            page.reload(wait_until="load")
            page.wait_for_timeout(600)
            offline_ok = bool(page.query_selector(".tabbar"))
            offline_cards = len(page.query_selector_all(".view *"))
        except Exception as exc:  # noqa: BLE001
            print(f"    断网重载异常：{exc}")
        r.check("断网后整页仍能打开", offline_ok)
        r.check("断网后内容有渲染（不是白屏）", offline_cards > 20, f"{offline_cards} 个节点")
        r.check("离线内容由 SW 提供",
                any(u.endswith("/") or u.endswith("index.html") for u in served_by_sw),
                f"{len(served_by_sw)} 个响应来自 SW")
        ctx.set_offline(False)

        # ------------------------------------------------------ 6 联网时拿到的是最新内容
        # 只改页面、不改 sw.js：导航必须是 network-first，否则用户会永远停在旧数据
        html = (serve_dir / "index.html").read_text(encoding="utf-8")
        (serve_dir / "index.html").write_text(
            html.replace("</head>", '<meta name="e2e-marker" content="fresh"></head>'),
            encoding="utf-8",
        )
        page.reload(wait_until="load")
        page.wait_for_timeout(500)
        r.check("联网重载拿到最新页面（导航走 network-first）",
                page.evaluate("Boolean(document.querySelector('meta[name=e2e-marker]'))"))

        # ------------------------------------------------------ 7 升级链条
        # 真改一次 sw.js（版本号变），看提示条会不会弹、点了会不会重载。
        # 走两轮：第一轮验证「稍后」能关掉，第二轮验证「刷新」真的换版本 ——
        # 一条提示条的两个出口都要验，只验一个的话另一个坏了也发现不了。
        def bump(tag_suffix: str, marker: str) -> None:
            sw_path = serve_dir / "sw.js"
            sw_path.write_text(
                sw_path.read_text(encoding="utf-8").replace(
                    "var VERSION = '", "var VERSION = '" + tag_suffix, 1),
                encoding="utf-8",
            )
            (serve_dir / "index.html").write_text(
                html.replace("</head>", f'<meta name="e2e-marker" content="{marker}"></head>'),
                encoding="utf-8",
            )
            page.evaluate("navigator.serviceWorker.getRegistration().then(r => r.update())")

        def wait_bar(timeout: int = 10000) -> bool:
            try:
                page.wait_for_selector(".updatebar", timeout=timeout)
                return True
            except Exception:
                return False

        def sw_state() -> dict:
            """把注册的三个 worker 状态全捞出来 —— 排查升级问题全靠它。"""
            return page.evaluate(
                """async () => {
                     const r = await navigator.serviceWorker.getRegistration();
                     if (!r) return { none: true };
                     return {
                       active:    r.active    ? r.active.state    : null,
                       waiting:   r.waiting   ? r.waiting.state   : null,
                       installing:r.installing? r.installing.state: null,
                       controlled: Boolean(navigator.serviceWorker.controller),
                       bar: Boolean(document.querySelector('.updatebar')),
                     };
                   }"""
            )

        bump("a", "upgraded")
        first_bar = wait_bar()
        r.check("检测到新版本并弹出刷新提示", first_bar, str(sw_state()))

        if first_bar:
            page.screenshot(path=str(SHOTS / "12-升级提示.png"))
            has_dismiss = bool(page.query_selector("[data-sw-dismiss]"))
            padded = page.evaluate("document.body.classList.contains('has-update')")
            r.check("提示条带「稍后」按钮", has_dismiss)
            r.check("提示条出现时给页面补了底部留白（不挡住最后一张卡）", padded)

            if has_dismiss:
                page.click("[data-sw-dismiss]")
                page.wait_for_timeout(250)
                r.check("点「稍后」能关掉提示条",
                        not page.query_selector(".updatebar")
                        and not page.evaluate("document.body.classList.contains('has-update')"))

            # 第二轮：再来一个新版本，走「刷新」出口。
            # 先挂一个 updatefound 计数器：万一提示没弹，能立刻分辨是
            # 「压根没触发更新」还是「更新了但提示环节坏了」。
            page.evaluate(
                """async () => {
                     const r = await navigator.serviceWorker.getRegistration();
                     window.__diag = { updatefound: 0, states: [] };
                     r.addEventListener('updatefound', () => { window.__diag.updatefound += 1; });
                     if (r.installing) {
                       r.installing.addEventListener('statechange',
                         e => window.__diag.states.push(e.target.state));
                     }
                   }"""
            )
            bump("b", "upgraded")
            second_bar = wait_bar()
            diag = page.evaluate("window.__diag")
            r.check("再次检测到新版本（关掉后仍会重新提示）", second_bar,
                    f"{sw_state()} diag={diag}")

            if second_bar:
                page.click("[data-sw-reload]")
                page.wait_for_timeout(1800)
                marker = page.evaluate(
                    "(() => { const m = document.querySelector('meta[name=e2e-marker]');"
                    "return m ? m.content : ''; })()"
                )
                r.check("点「刷新」后真的切换到新版本", marker == "upgraded", f"标记 {marker!r}")

                names = page.evaluate("caches.keys()")
                r.check("旧缓存已被清理（只剩当前版本）",
                        all(not n.startswith("jobpipe-") or version in n for n in names),
                        f"{names}")

        # ------------------------------------------------------ 8 控制台
        r.check("console 无报错", not console_errors, "; ".join(console_errors[:3]))

        ctx.close()
        browser.close()

    httpd.shutdown()


def main() -> None:
    ap = argparse.ArgumentParser(description="PWA 端到端验证")
    ap.add_argument("--keep", action="store_true", help="保留临时服务目录")
    args = ap.parse_args()

    if not PUBLISH.is_dir():
        raise SystemExit(f"缺少 {PUBLISH}，先运行：python build.py --publish")

    # 复制一份到可写的临时目录：测试要模拟"部署了新版本"，不能污染真实产物
    if SERVE.exists():
        shutil.rmtree(SERVE)
    shutil.copytree(PUBLISH, SERVE)

    print(f"服务目录 {SERVE}（拷贝自 {PUBLISH.name}）")
    r = Report()
    try:
        run(r, SERVE, args.keep)
    finally:
        if not args.keep:
            shutil.rmtree(SERVE, ignore_errors=True)
        else:
            print(f"临时目录保留：{SERVE}")

    total = len(r.passed) + len(r.failed)
    print(f"\n── PWA 端到端 ──  {len(r.passed)}/{total} 通过")
    if r.failed:
        for f in r.failed:
            print(f"  ✗ {f}")
        raise SystemExit(1)
    print("全部通过")


if __name__ == "__main__":
    main()
