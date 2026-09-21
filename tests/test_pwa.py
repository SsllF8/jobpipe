#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""PWA 与发布闸门的测试。

跑法：
    python -m pytest tests -q

覆盖五块：
  1. manifest  —— 字段合法、路径相对、图标尺寸与真实文件一致
  2. sw.js     —— 缓存以版本号为名、预缓存路径相对、不在 install 阶段抢跑
  3. 构建层    —— 只有可发布的 index.html 才带 PWA 资产；版本号由内容决定
  4. 发布闸门  —— 干净产物通过；混入个人数据必须失败
  5. 安装入口  —— 应用内接管 beforeinstallprompt 的那套逻辑还在（行为由
                  tools/e2e_install.py 在真浏览器里验）

为什么值得单独一个文件：M4 的风险大多是「本地看着对、上线才发现不对」——
绝对路径在本地文件系统下能打开，部署到子目录就 404；
图标声明 512 实际是 256，本地预览正常，装到手机才糊。
这些都能在纯 Python 侧查出来，不必等到浏览器。
"""

from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build as builder  # noqa: E402
import check_public as gate  # noqa: E402

MANIFEST = ROOT / "src" / "manifest.webmanifest"
SW = ROOT / "src" / "sw.js"
ICONS = ROOT / "src" / "icons"

ICON_NAMES = [
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-512.png",
    "apple-touch-icon.png",
]


def png_size(path: Path) -> tuple[int, int]:
    """从 PNG 的 IHDR 块读出真实宽高。

    为什么要真读文件：manifest 声明的 sizes 是给人看的字符串，
    和 PNG 实际像素没有强制绑定关系。只校验声明值等于校验一个注释。
    """
    raw = path.read_bytes()
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} 不是合法 PNG"
    assert raw[12:16] == b"IHDR", f"{path.name} 缺少 IHDR 块"
    width, height = struct.unpack(">II", raw[16:24])
    return width, height


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sw_source() -> str:
    return SW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def public_build(tmp_path_factory) -> Path:
    """构建一份可发布产物，返回 index.html 路径。"""
    out_dir = tmp_path_factory.mktemp("publish")
    builder.build(demo=False, out_dir=out_dir)
    return out_dir / "index.html"


# ============================================================ 1 manifest


def test_manifest_is_valid_json(manifest):
    assert manifest["name"]
    assert manifest["short_name"]
    assert manifest["lang"] == "zh-CN"


def test_manifest_uses_relative_urls(manifest):
    """start_url / scope 必须是相对写法。

    部署在 https://ssllf8.github.io/jobpipe/ 时，
    "start_url": "/" 会指向域名根 —— 打开桌面图标进的是别人的站点。
    """
    for key in ("start_url", "scope", "id"):
        if key in manifest:
            assert not str(manifest[key]).startswith("/"), f"{key} 是绝对路径"


def test_manifest_display_is_standalone(manifest):
    assert manifest["display"] == "standalone"


def test_manifest_icon_sizes_match_real_files(manifest):
    """声明的尺寸必须与 PNG 实际像素一致，否则手机会拉伸变形。"""
    checked = 0
    for icon in manifest["icons"]:
        rel = str(icon["src"]).lstrip("./")
        path = ROOT / "src" / rel
        assert path.exists(), f"manifest 引用的图标不存在：{icon['src']}"
        assert icon["type"] == "image/png"
        width, height = png_size(path)
        assert f"{width}x{height}" == icon["sizes"], (
            f"{path.name} 实际 {width}x{height}，manifest 声明 {icon['sizes']}"
        )
        checked += 1
    assert checked >= 3, "manifest 里的图标太少，安装体验会缺图"


def test_manifest_has_maskable_icon(manifest):
    """没有 maskable 图标时，Android 会把普通图标裁成圆角，四周留白边。"""
    purposes = {i.get("purpose", "any") for i in manifest["icons"]}
    assert "maskable" in purposes


def test_all_icon_files_exist_and_square():
    for name in ICON_NAMES:
        path = ICONS / name
        assert path.exists(), f"缺少图标 {name}（跑 python tools/make_icons.py 生成）"
        width, height = png_size(path)
        assert width == height, f"{name} 不是正方形（{width}x{height}）"
        assert width >= 180, f"{name} 边长只有 {width}，手机上会糊"


# ============================================================ 2 sw.js


def test_sw_has_required_handlers(sw_source):
    for event in ("install", "activate", "fetch", "message"):
        assert f"addEventListener('{event}'" in sw_source, f"sw.js 缺少 {event} 处理"


def test_sw_version_placeholder_present(sw_source):
    assert "__VERSION__" in sw_source, "sw.js 里没有 __VERSION__ 占位符"


def test_sw_does_not_skip_waiting_during_install(sw_source):
    """install 阶段不能直接 skipWaiting()。

    那样新 SW 会立刻接管，但用户看到的还是旧页面 —— 页面与缓存版本错配，
    表现是「刷新了还是旧数据」或「点了按钮行为对不上」。
    正确做法是等页面发消息，用户点「刷新」再接管。
    """
    install_block = re.search(r"addEventListener\('install'.*?\n\}\);", sw_source, re.S)
    assert install_block, "找不到 install 处理块"
    assert "skipWaiting()" not in install_block.group(0)


def test_sw_precache_paths_are_relative(sw_source):
    """预缓存清单里不能有以 / 开头的路径（子目录部署会 404）。"""
    block = re.search(r"var PRECACHE = \[(.*?)\];", sw_source, re.S)
    assert block, "找不到 PRECACHE 清单"
    urls = re.findall(r"'([^']+)'", block.group(1))
    assert len(urls) >= 5, f"预缓存条目太少：{urls}"
    for url in urls:
        assert not url.startswith("/"), f"预缓存用了绝对路径：{url}（子目录部署会 404）"
        assert url.startswith("./"), f"预缓存路径不是显式相对写法：{url}"


def test_sw_navigation_uses_network_first(sw_source):
    """导航必须 network-first。

    这是个每天更新岗位的看板：若导航走 cache-first，部署新版本后
    手机上会永远停在旧数据，而且用户完全无法察觉。
    """
    nav_block = re.search(r"request\.mode === 'navigate'.*?\n    return;", sw_source, re.S)
    assert nav_block, "找不到导航请求的分支"
    block = nav_block.group(0)
    assert "fetch(request)" in block, "导航分支没有先请求网络"
    assert "caches.match('./index.html')" in block, "导航分支没有缓存兜底"
    assert block.index("fetch(request)") < block.index("caches.match"), "导航分支顺序反了"


def test_sw_ignores_cross_origin(sw_source):
    """跨域请求直接放行，不缓存不拦截 —— 否则会把别人的资源缓存进来。"""
    assert "self.location.origin" in sw_source
    assert "response.type === 'basic'" in sw_source, "缺少跨域响应过滤"


def test_sw_cache_name_is_versioned(sw_source):
    assert re.search(r"var CACHE = 'jobpipe-' \+ VERSION", sw_source)


# ============================================================ 3 构建层


def test_version_is_deterministic():
    """同样内容必须得到同一个版本号 —— 否则每次构建都弹「有新版本」，提示会变噪音。"""
    a = builder.content_version("<html>x</html>", "sw", "mf")
    b = builder.content_version("<html>x</html>", "sw", "mf")
    assert a == b
    assert len(a) == 10


def test_version_changes_with_content():
    base = builder.content_version("<html>x</html>", "sw", "mf")
    assert base != builder.content_version("<html>y</html>", "sw", "mf")
    # sw.js 与 manifest 也要参与哈希：它们决定缓存内容
    assert base != builder.content_version("<html>x</html>", "sw2", "mf")
    assert base != builder.content_version("<html>x</html>", "sw", "mf2")


def test_public_build_emits_pwa_assets(public_build):
    out_dir = public_build.parent
    for name in ["index.html", "manifest.webmanifest", "sw.js", ".nojekyll"] + \
                [f"icons/{n}" for n in ICON_NAMES]:
        assert (out_dir / name).exists(), f"可发布产物缺少 {name}"


def test_public_index_links_pwa(public_build):
    html = public_build.read_text(encoding="utf-8")
    assert '<link rel="manifest" href="./manifest.webmanifest">' in html
    assert 'rel="apple-touch-icon"' in html


def test_emitted_sw_has_real_version(public_build):
    """产物里的 sw.js 必须已经替换掉占位符，且与缓存名一致。"""
    sw = (public_build.parent / "sw.js").read_text(encoding="utf-8")
    assert "__VERSION__" not in sw
    m = re.search(r"var VERSION = '([0-9a-f]{10})'", sw)
    assert m, "sw.js 里的版本号不是 10 位十六进制"
    assert f"jobpipe-' + VERSION" in sw or f"jobpipe-{m.group(1)}" in sw


def test_rebuild_gives_same_version(tmp_path):
    """内容不变 → 版本不变 → 用户不会收到无意义的升级提示。"""
    a, b = tmp_path / "a", tmp_path / "b"
    builder.build(demo=False, out_dir=a)
    builder.build(demo=False, out_dir=b)
    va = re.search(r"var VERSION = '([^']+)'", (a / "sw.js").read_text(encoding="utf-8")).group(1)
    vb = re.search(r"var VERSION = '([^']+)'", (b / "sw.js").read_text(encoding="utf-8")).group(1)
    assert va == vb, "内容没改版本却变了，用户会收到假升级提示"


def test_demo_build_has_no_pwa_assets(tmp_path):
    """演示版/个人版不产出 PWA 资产。

    否则 local.html 会和 index.html 争抢同一目录的 SW scope，
    导航回退时可能把公开版当成个人版（或反过来），行为难以预测。
    """
    out_dir = tmp_path / "demo"
    builder.build(demo=True, out_dir=out_dir)
    assert (out_dir / "preview-demo.html").exists()
    for name in ("manifest.webmanifest", "sw.js", "icons"):
        assert not (out_dir / name).exists(), f"演示版不该产出 {name}"


def test_demo_build_has_no_manifest_link(tmp_path):
    out_dir = tmp_path / "demo"
    builder.build(demo=True, out_dir=out_dir)
    html = (out_dir / "preview-demo.html").read_text(encoding="utf-8")
    # 断言必须带上 '<'：app.js 里那句守卫 querySelector('link[rel="manifest"]')
    # 也含 rel="manifest" 字面量，不带 '<' 会把它误判成注入的标签
    assert '<link rel="manifest"' not in html
    assert '<link rel="apple-touch-icon"' not in html
    assert "<!--__PWA_HEAD__-->" not in html, "占位符没被替换（空注入也要替换掉）"


def test_publish_mode_refuses_personal_data():
    """--publish 与 --state 互斥：可发布版本绝不能内嵌个人数据。"""
    with pytest.raises(SystemExit):
        builder.build(publish=True, state_path=ROOT / "data" / "demo-state.json")


def test_publish_mode_wipes_target_dir(tmp_path):
    """--publish 必须清空目标目录。

    否则上次遗留的 preview-demo.html（含自荐信正文）会被一起部署出去。
    """
    target = tmp_path / "publish"
    target.mkdir()
    (target / "preview-demo.html").write_text("旧残留", encoding="utf-8")
    builder.build(publish=True, out_dir=target)
    assert not (target / "preview-demo.html").exists(), "旧文件没被清掉"


def test_publish_ignores_local_state_file(tmp_path, monkeypatch):
    """即使本机存在 data/state.json，--publish 也不能把它读进来。

    这条最容易被忽略：靠「记得别加 --state」防泄漏是不可靠的，
    本机一旦有了 state.json，默认路径就会读到它。所以要在代码里直接关掉。
    """
    fake = tmp_path / "state.json"
    fake.write_text(json.dumps({
        "schema": 1,
        "patches": {"liaoning-xinxi-ae": {"status": "applied"}},
        "added": [],
        "removed": [],
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(builder, "LOCAL_STATE", fake)

    target = tmp_path / "pub"
    builder.build(publish=True, out_dir=target)

    html = (target / "index.html").read_text(encoding="utf-8")
    m = re.search(r"window\.__META__ = (\{.*?\n\});", html, re.S)
    meta = json.loads(m.group(1))
    assert meta["state_inlined"] is False, "发布构建内联了本地投递记录"
    assert meta["state_source"] == "browser"
    errors, _ = gate.audit(target)
    assert not errors, errors


# ============================================================ 4 发布闸门


@pytest.fixture(scope="module")
def clean_publish(tmp_path_factory) -> Path:
    target = tmp_path_factory.mktemp("gate")
    builder.build(publish=True, out_dir=target)
    return target


def test_gate_passes_on_clean_build(clean_publish):
    errors, _ = gate.audit(clean_publish)
    assert not errors, f"干净的发布产物不该有问题：{errors}"


def test_gate_rejects_extra_files(clean_publish, tmp_path):
    """混入演示版必须被拦下 —— 它内联了点名投递公司的自荐信正文。"""
    target = tmp_path / "dirty"
    target.mkdir()
    for p in clean_publish.rglob("*"):
        if p.is_file():
            dest = target / p.relative_to(clean_publish)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())
    (target / "preview-demo.html").write_text("<html></html>", encoding="utf-8")

    errors, _ = gate.audit(target)
    assert any("不该发布的内容" in e for e in errors), errors


def test_gate_rejects_inlined_letters(clean_publish, tmp_path):
    """产物里若出现自荐信正文，闸门必须报错。"""
    target = tmp_path / "leak"
    target.mkdir()
    for p in clean_publish.rglob("*"):
        if p.is_file():
            dest = target / p.relative_to(clean_publish)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())

    html = (target / "index.html").read_text(encoding="utf-8")
    html = html.replace('"cover_letter": ""', '"cover_letter": "尊敬的招聘负责人，我是刘展博…"', 1)
    (target / "index.html").write_text(html, encoding="utf-8")

    errors, _ = gate.audit(target)
    assert any("自荐信正文" in e for e in errors), errors


def test_gate_rejects_local_state(clean_publish, tmp_path):
    """state_source=file: 说明构建时读了本地记录，绝不能发布。"""
    target = tmp_path / "state"
    target.mkdir()
    for p in clean_publish.rglob("*"):
        if p.is_file():
            dest = target / p.relative_to(clean_publish)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())

    html = (target / "index.html").read_text(encoding="utf-8")
    html = html.replace('"state_source": "browser"', '"state_source": "file:state.json"')
    (target / "index.html").write_text(html, encoding="utf-8")

    errors, _ = gate.audit(target)
    assert any("state 文件" in e for e in errors), errors


def test_gate_rejects_absolute_manifest_urls(clean_publish, tmp_path):
    """manifest 用绝对路径在子目录部署下会 404，闸门要提前拦住。"""
    target = tmp_path / "abs"
    target.mkdir()
    for p in clean_publish.rglob("*"):
        if p.is_file():
            dest = target / p.relative_to(clean_publish)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())

    mf = json.loads((target / "manifest.webmanifest").read_text(encoding="utf-8"))
    mf["start_url"] = "/"
    (target / "manifest.webmanifest").write_text(
        json.dumps(mf, ensure_ascii=False), encoding="utf-8")

    errors, _ = gate.audit(target)
    assert any("绝对路径" in e for e in errors), errors


def test_gate_rejects_unreplaced_sw_version(clean_publish, tmp_path):
    target = tmp_path / "sw"
    target.mkdir()
    for p in clean_publish.rglob("*"):
        if p.is_file():
            dest = target / p.relative_to(clean_publish)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(p.read_bytes())

    (target / "sw.js").write_text("var VERSION = '__VERSION__';", encoding="utf-8")
    errors, _ = gate.audit(target)
    assert any("__VERSION__" in e for e in errors), errors


def test_gate_reports_missing_dir(tmp_path):
    errors, _ = gate.audit(tmp_path / "nope")
    assert errors and "目录不存在" in errors[0]


# ---------------------------------------------------------------------------
# 5. 安装入口
#
# 「菜单里没有安装应用」那次问题的防回归。
# 站点可安装 ≠ 人装得上：入口长在浏览器菜单里，Chrome 要等它自己认定
# 用户确实在用这个站才肯放出来，微信内置浏览器则根本没有这一项。
# 所以改成应用内接管 beforeinstallprompt、自己给入口。
# 下面这些断言守的就是那套接管逻辑还在，别被哪次重构悄悄删掉。
# 真浏览器里的行为由 tools/e2e_install.py 验（25 项断言）。
# ---------------------------------------------------------------------------

APP_JS = ROOT / "src" / "app.js"
STYLE = ROOT / "src" / "style.css"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css() -> str:
    return STYLE.read_text(encoding="utf-8")


def test_app_takes_over_beforeinstallprompt(app_js):
    """必须接管事件：不 preventDefault 就是让浏览器弹它自己那条迷你提示，
    位置和时机都不受控 —— 而那个入口正是用户翻不到的东西。"""
    assert "addEventListener('beforeinstallprompt'" in app_js
    handler = re.search(r"addEventListener\('beforeinstallprompt'.*?\n\s*\}\);",
                        app_js, re.S)
    assert handler, "找不到 beforeinstallprompt 的处理体"
    body = handler.group(0)
    assert "preventDefault()" in body
    assert "deferredInstall = e" in body


def test_app_calls_prompt_and_reads_user_choice(app_js):
    assert ".prompt()" in app_js
    assert "userChoice" in app_js
    # 两种结果都要收尾：接受就撤掉入口，拒绝也不能留一个点了没反应的按钮
    assert "outcome === 'accepted'" in app_js


def test_install_entry_skipped_when_already_installed(app_js):
    """已经装成 App 了还引导安装，等于让用户点一个必然失败的东西。"""
    fn = re.search(r"function showInstallEntry\(\) \{(.*?)\n  \}", app_js, re.S)
    assert fn, "找不到 showInstallEntry"
    first_line = fn.group(1).strip().splitlines()[0]
    assert "isStandalone()" in first_line, first_line
    assert "display-mode: standalone" in app_js
    assert "navigator.standalone" in app_js      # iOS Safari 不实现那套媒体查询


def test_install_dismiss_is_persisted(app_js):
    """关掉之后下次不许再弹 —— 否则每条入口都会变成骚扰。"""
    assert "jobpipe.install.dismissed" in app_js
    assert "localStorage.setItem(INSTALL_KEY" in app_js


def test_manual_steps_cover_three_platforms(app_js):
    """Android / iPhone / 微信 的路径完全不同，串一个就等于对那个平台没写。"""
    words = re.search(r"function installWords\(\) \{(.*?)\n  \}", app_js, re.S)
    assert words, "找不到 installWords"
    body = words.group(1)
    assert "MicroMessenger" in body and "在浏览器中打开" in body
    assert "iPhone|iPad|iPod" in body and "添加到主屏幕" in body
    assert "Android" in body and "安装应用" in body


def test_install_state_exposed_to_e2e(app_js):
    """tools/e2e_install.py 靠这些字段做断言；改名会让那个脚本静默失去意义。"""
    for key in ("installAvailable", "installBarShown", "installButtonShown",
                "installDismissed", "standalone"):
        assert key + ":" in app_js, key


def test_install_bar_has_styles(style_css):
    assert ".installbar" in style_css
    assert ".ibtn.install" in style_css


def test_public_build_carries_install_entry(public_build):
    html = public_build.read_text(encoding="utf-8")     # fixture 给的直接是 index.html
    assert "beforeinstallprompt" in html
    assert ".installbar" in html


def test_install_e2e_script_exists():
    """这套逻辑只有真浏览器跑得出来，脚本必须在，且要问浏览器要资格结论。"""
    script = ROOT / "tools" / "e2e_install.py"
    assert script.exists()
    assert "getInstallabilityErrors" in script.read_text(encoding="utf-8")
