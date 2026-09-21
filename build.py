#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建：把 data + src 内联成**单文件** HTML。

为什么要内联
------------
1. 单文件 = 双击即用、离线可用、可直接丢到 GitHub Pages，不需要任何服务器或 CDN。
2. 符合本项目「不依赖外部 CDN」的环境约定。
3. 数据在构建期写死进 HTML，运行时零网络请求。

三种构建模式（**输出文件名刻意不同，避免个人数据被误发布**）
------------------------------------------------------------
    python build.py                              # 公开版 → dist/index.html      不含任何个人数据
    python build.py --demo                       # 演示版 → dist/preview-demo.html（虚构状态 + 自荐信）
    python build.py --state data/state.json      # 个人版 → dist/local.html      内嵌你的真实数据
    python build.py --publish                    # 只产出可发布版本 → dist-publish/（CI 用）

`--state` 会把你导出的真实投递记录写进 HTML，**产物只能本地/私密使用，绝不能部署到公开地址**。
因此它默认输出到 `local.html`，与可发布的 `index.html` 物理隔离。

`--publish` 与前三种的关键区别：它先把目标目录**整个清空**再构建，且只产出 index.html
与它的 PWA 附属文件。为什么需要它 —— `dist/` 是本地开发目录，里面会残留
`preview-demo.html`（内联了自荐信正文，点名了你在投的公司）。
部署时若整目录上传，这些文件会一起公开。用「每次重建的干净目录」替代「靠人记得删」，
再用 tools/check_public.py 按白名单校验，两道闸互为兜底。

PWA 资产为什么是独立文件
------------------------
Service Worker 必须与页面同源、且**不能被内联**（浏览器强制），
所以「单文件 HTML」与「PWA」不是二选一：HTML 依然自包含、file:// 双击即用，
仅在托管（http/https）时额外挂上 manifest.webmanifest 与 sw.js 变成可安装应用。
这两样只在可发布的 index.html 旁产出 —— 若 local.html 也带一套，
它会和 index.html 争抢同一个目录的 SW scope，导航回退可能把公开版当成个人版。

数据分层
--------
    seed      ← data/seed-*.json      岗位池，由采集脚本重写（每天覆盖）
    letters   ← data/letters.json     自荐信正文，由 tools/gen_letter.py 生成（累积）
    state     ← 浏览器 localStorage   你的操作记录（私有）
    merge(seed, letters, state) → 页面数据

**自荐信为什么不进公开版**：正文里点名了投递的公司（"应聘辽宁鑫锡的……岗位"），
属于个人投递行为，会暴露你在投哪些公司。所以只有演示版与个人版会内联 letters，
`index.html` 一律不含——文件名叫 index.html 就自动排除，不需要你记得加参数。

公开版内联的 state 为空，页面启动后用 localStorage 里的数据；
个人版内联 state 作为**首次打开时的初始值**（仅当本地为空才采纳），
之后一切仍以 localStorage 为准——否则每次重新部署都会回滚你的进度。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import datetime as dt
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
DATA = ROOT / "data"
DIST = ROOT / "dist"
PUBLISH = ROOT / "dist-publish"

sys.path.insert(0, str(SRC))
from score import load_json, score_all  # noqa: E402


TOKENS = {
    "style": "/*__STYLE__*/",
    "data": "/*__DATA__*/",
    "meta": "/*__META__*/",
    "state": "/*__STATE__*/",
    "store": "/*__STORE__*/",
    "app": "/*__APP__*/",
    "pwa": "<!--__PWA_HEAD__-->",
}

SEED = DATA / "seed-2026-09-17.json"
DEMO_STATE = DATA / "demo-state.json"
LOCAL_STATE = DATA / "state.json"
LETTERS = DATA / "letters.json"

SCHEMA = 1

# PWA 附属文件。图标用 src/icons/ 里提交好的 PNG，构建时只拷不生成，
# 这样 CI 和别人的机器上不需要装 Playwright 也能构建出完整产物。
ICON_FILES = [
    "icon-192.png",
    "icon-512.png",
    "icon-maskable-512.png",
    "apple-touch-icon.png",
]

PWA_HEAD = """<link rel="manifest" href="./manifest.webmanifest">
<link rel="icon" type="image/png" sizes="192x192" href="./icons/icon-192.png">
<link rel="apple-touch-icon" href="./icons/apple-touch-icon.png">"""


def inject(html: str, token: str, content: str) -> str:
    """占位符替换。

    注意：Python 的 str.replace 是字面替换，不会像 JS 那样把 `$&` / `$'`
    当成分组引用 —— 所以内联进来的 app.js 里带 `${...}` 的模板字符串是安全的。
    """
    if token not in html:
        raise SystemExit(f"模板里找不到占位符 {token}")
    return html.replace(token, content)


def safe_json(obj) -> str:
    """防止 JSON 里出现 </script> 提前闭合脚本块。"""
    return json.dumps(obj, ensure_ascii=False, indent=2).replace("<", "\\u003c")


def normalize_state(raw: dict | None) -> dict:
    """把任意来源的 state 规范成页面认识的结构，坏字段丢弃而不是让页面崩掉。"""
    empty = {"schema": SCHEMA, "updated_at": None, "patches": {}, "added": [], "removed": []}
    if not raw or not isinstance(raw, dict):
        return empty

    patches = {
        k: v
        for k, v in (raw.get("patches") or {}).items()
        if isinstance(k, str) and k and isinstance(v, dict)
    }
    added = [j for j in (raw.get("added") or []) if isinstance(j, dict) and j.get("id")]
    removed = sorted({x for x in (raw.get("removed") or []) if isinstance(x, str)})

    return {
        "schema": SCHEMA,
        "updated_at": raw.get("updated_at") if isinstance(raw.get("updated_at"), str) else None,
        "patches": patches,
        "added": added,
        "removed": removed,
    }


def resolve_state(demo: bool, state_path: Path | None) -> tuple[dict, str]:
    """决定内联哪份 state，返回 (state, 来源标签)。"""
    if demo:
        if not DEMO_STATE.exists():
            raise SystemExit(f"--demo 需要 {DEMO_STATE}，但它不存在")
        return normalize_state(load_json(DEMO_STATE)), "demo"
    if state_path:
        if not state_path.exists():
            raise SystemExit(f"找不到 state 文件：{state_path}")
        return normalize_state(load_json(state_path)), f"file:{state_path.name}"
    if LOCAL_STATE.exists():
        # 自动读取 data/state.json，省得每次都带 --state
        return normalize_state(load_json(LOCAL_STATE)), f"file:{LOCAL_STATE.name}"
    return normalize_state(None), "browser"


def load_letters(path: Path) -> dict[str, str]:
    """读自荐信缓存 data/letters.json，返回 {job_id: 正文}。

    自荐信是「生成层」，独立于 seed（采集层，每天被重写）与 state（用户层，私有）。
    只取有非空正文的条目，坏数据直接跳过而不是让构建失败。
    """
    if not path.exists():
        return {}
    raw = load_json(path)
    entries = raw.get("letters") if isinstance(raw, dict) else None
    if not isinstance(entries, dict):
        return {}
    out: dict[str, str] = {}
    for job_id, rec in entries.items():
        if isinstance(job_id, str) and job_id and isinstance(rec, dict):
            text = (rec.get("text") or "").strip()
            if text:
                out[job_id] = text
    return out


def merge_letters(scored: list[dict], letters: dict[str, str]) -> tuple[list[dict], int]:
    """把自荐信正文填进对应岗位的 cover_letter 字段。

    letters.json 是自荐信的权威来源 —— seed 里的 cover_letter 一律是空占位，
    采集脚本不产出自荐信，所以这里是「有则以 letters 为准」而不是「只填空白」。
    """
    applied = 0
    for rec in scored:
        text = letters.get(rec.get("id", ""))
        if text:
            rec["cover_letter"] = text
            applied += 1
    return scored, applied


def content_version(*parts: str) -> str:
    """按产物内容算版本号（sha1 前 10 位）。

    为什么要算而不是用时间戳：版本号决定 Service Worker 的缓存名。
    用时间戳的话，每次构建（哪怕内容一字未改）都会产生新缓存名，
    用户每次打开都收到「有新版本」提示 —— 提示一旦变噪音就会被忽略。
    用内容哈希则「内容变才提示」，升级提示重新变得可信。

    为什么要把 sw.js 与 manifest 也纳入哈希：它们虽然不进 HTML，
    却直接决定缓存里存什么。只哈希 HTML 的话，改了预缓存清单
    会复用旧缓存名，新旧条目混在同一个缓存里。
    """
    h = hashlib.sha1()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:10]


def emit_pwa(out_dir: Path, version: str, manifest: str, sw: str) -> list[str]:
    """写出 PWA 附属文件，返回相对输出目录的文件名列表。

    只与可发布的 index.html 配套产出，理由见模块 docstring。
    """
    written: list[str] = []

    (out_dir / "manifest.webmanifest").write_text(manifest, encoding="utf-8")
    written.append("manifest.webmanifest")

    if "__VERSION__" not in sw:
        raise SystemExit("src/sw.js 里找不到 __VERSION__ 占位符，版本号无法写入")
    (out_dir / "sw.js").write_text(sw.replace("__VERSION__", version), encoding="utf-8")
    written.append("sw.js")

    icons_dir = out_dir / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    missing = []
    for name in ICON_FILES:
        src = SRC / "icons" / name
        if not src.exists():
            missing.append(name)
            continue
        shutil.copyfile(src, icons_dir / name)
        written.append(f"icons/{name}")
    if missing:
        raise SystemExit(
            f"缺少图标文件：{missing}\n请先运行 python tools/make_icons.py 生成。"
        )

    # GitHub Pages 在产物式部署下不跑 Jekyll，但留着这个文件可以彻底排除
    # 「下划线开头的目录被吞掉」这类经典事故，代价为零。
    (out_dir / ".nojekyll").write_text("", encoding="utf-8")
    written.append(".nojekyll")

    return written


def build(demo: bool = False, out: Path | None = None, state_path: Path | None = None,
          seed_path: Path = SEED, letters_path: Path = LETTERS,
          out_dir: Path | None = None, publish: bool = False) -> Path:
    profile = load_json(SRC / "profile.json")
    payload = load_json(seed_path)
    records = payload["applications"]

    if publish:
        if demo or state_path:
            raise SystemExit("--publish 与 --demo / --state 互斥：可发布版本不含任何个人数据")
        # 先整个清空，再构建。不让上一次遗留的演示版/个人版混进部署目录。
        if out_dir is None:
            out_dir = PUBLISH
        if out_dir.exists():
            shutil.rmtree(out_dir)

    if publish:
        # 发布构建永远不读本地 state。否则本机一旦有了 data/state.json，
        # 一条 --publish 就会自己把个人投递记录塞进可发布产物 ——
        # 靠「记得别加 --state」防这种事是不可靠的，得从代码上关掉这条路。
        state, state_source = normalize_state(None), "browser"
    else:
        state, state_source = resolve_state(demo, state_path)
    has_state = bool(state["patches"] or state["added"] or state["removed"])

    # 先定输出文件名 —— 自荐信的内联策略取决于产物是否可公开
    if out is None:
        if demo:
            name = "preview-demo.html"
        elif state_path or has_state:
            # 内嵌了真实数据的产物换个文件名，防止被误当成可发布版本传上去
            name = "local.html"
        else:
            name = "index.html"
        out = (out_dir / name) if out_dir else (DIST / name)

    # 打分（纯规则，零 AI）
    scored = score_all(records, profile)

    # 合并自荐信：生成层（letters.json）→ 展示字段 cover_letter
    # 自荐信正文点名了投递的公司，属于个人投递行为，因此不进可公开的 index.html
    expose_letters = demo or has_state or out.name != "index.html"
    letters = load_letters(letters_path) if expose_letters else {}
    seed_ids = {rec.get("id") for rec in records}
    orphans = sorted(set(letters) - seed_ids)
    scored, letters_applied = merge_letters(scored, letters)

    tier_count: dict[str, int] = {}
    for rec in scored:
        tier = rec.get("tier") or rec.get("tier_auto") or "C"
        tier_count[tier] = tier_count.get(tier, 0) + 1

    now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")

    meta = {
        "built_at": now,
        "updated_at": payload.get("updated_at", "")[:10] or now[:10],
        "source": payload.get("source", ""),
        "total": len(scored),
        "tiers": tier_count,
        "demo": demo,
        "state_source": state_source,
        "state_inlined": has_state,
        "letters": letters_applied,
        "letters_orphans": len(orphans),
        "schema": SCHEMA,
    }

    data_out = {
        "version": payload.get("version", 1),
        "updated_at": now,
        "applications": scored,
    }

    html = (SRC / "index.html").read_text(encoding="utf-8")
    html = inject(html, TOKENS["style"], (SRC / "style.css").read_text(encoding="utf-8"))
    html = inject(html, TOKENS["data"], safe_json(data_out))
    html = inject(html, TOKENS["meta"], safe_json(meta))
    html = inject(html, TOKENS["state"], safe_json(state) if has_state else "null")
    html = inject(html, TOKENS["store"], (SRC / "store.js").read_text(encoding="utf-8"))
    html = inject(html, TOKENS["app"], (SRC / "app.js").read_text(encoding="utf-8"))

    # --- PWA ---
    # 版本号在注入 PWA head **之前**算：head 是常量，把它算进哈希没有任何信息量，
    # 反而会让「先算哈希再注入」这个顺序变得说不清。纳入 sw.js 与 manifest 是因为
    # 它们决定缓存内容（理由见 content_version 的注释）。
    manifest = (SRC / "manifest.webmanifest").read_text(encoding="utf-8")
    sw_src = (SRC / "sw.js").read_text(encoding="utf-8")
    version = content_version(html, sw_src, manifest)
    is_public = out.name == "index.html"
    html = inject(html, TOKENS["pwa"], PWA_HEAD if is_public else "")

    if has_state and not demo and out.name == "index.html":
        raise SystemExit(
            "拒绝构建：这份产物内嵌了真实投递数据，文件名却是可发布的 index.html。\n"
            "个人版请用 --out dist/local.html，或用默认输出。"
        )

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    pwa_files: list[str] = []
    if is_public:
        pwa_files = emit_pwa(out.parent, version, manifest, sw_src)

    size_kb = out.stat().st_size / 1024
    label = {"demo": "演示数据", "browser": "不含个人数据"}.get(state_source, "内嵌真实数据")
    print(f"构建完成 → {out}")
    if expose_letters:
        print(f"  岗位 {len(scored)} 条 ｜ 分档 {tier_count} ｜ 自荐信 {letters_applied} 封 ｜ 产物 {size_kb:.1f} KB")
    else:
        print(f"  岗位 {len(scored)} 条 ｜ 分档 {tier_count} ｜ 产物 {size_kb:.1f} KB")
        if letters_path.exists():
            print("  自荐信未内联（可发布产物不含个人投递信息；个人版会自动带上）")
    if orphans:
        print(f"  ⚠ letters.json 有 {len(orphans)} 条对不上岗位池（已忽略）：{orphans[:5]}")
    print(f"  状态来源：{label}（{state_source}）"
          + ("　⚠ 个人版，勿部署到公开地址" if has_state and not demo else ""))
    if pwa_files:
        print(f"  PWA v{version} ｜ {len(pwa_files)} 个附属文件："
              f"{', '.join(pwa_files[:3])}{' …' if len(pwa_files) > 3 else ''}")
        print("    托管到 https 后即可「添加到主屏幕」，断网也能打开")
    elif not demo and not has_state:
        print("  未产出 PWA 附属文件（只有可发布的 index.html 才带）")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="内联构建单文件看板")
    parser.add_argument("--demo", action="store_true", help="叠加虚构示例状态，用于演示/截图")
    parser.add_argument("--state", help="要内联的用户 state.json（个人版）")
    parser.add_argument("--seed", help="岗位种子数据，默认 data/seed-2026-09-17.json")
    parser.add_argument("--letters", help="自荐信缓存，默认 data/letters.json")
    parser.add_argument("--out", help="输出路径")
    parser.add_argument("--publish", action="store_true",
                        help=f"只产出可发布版本（清空并重建 {PUBLISH.name}/），供 CI 部署")
    parser.add_argument("--out-dir", help="输出目录，默认 dist/；与 --out 二选一")
    args = parser.parse_args()

    if args.publish and args.out:
        parser.error("--publish 不能与 --out 同时使用")

    build(
        demo=args.demo,
        out=Path(args.out) if args.out else None,
        state_path=Path(args.state) if args.state else None,
        seed_path=Path(args.seed) if args.seed else SEED,
        letters_path=Path(args.letters) if args.letters else LETTERS,
        out_dir=Path(args.out_dir) if args.out_dir else None,
        publish=args.publish,
    )


if __name__ == "__main__":
    main()
