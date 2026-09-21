#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检索清单生成器：把「关键词 × 城市 × 渠道」展开成**可直接点的链接**。

为什么要这个而不是爬虫
----------------------
逐个爬招聘站会有三重麻烦：反爬（BOSS/猎聘有登录墙与风控）、合规（多数站点
robots 与服务条款禁止批量抓取）、维护（页面一改版解析就失效）。
本脚本干脆**不抓页面**——只按各站公开的搜索 URL 规则拼链接，剩下的交给浏览器。
于是：零反爬风险、零维护成本、搜索结果永远和真人看到的一致。

顺带解决了「来源面广」：一次产出覆盖 20+ 渠道的检索入口，比单站点翻页找到的岗位多得多。

产物
----
    dist/search-plan.html            手机浏览器直接用（纯静态、无外链资源）
    data/search-plan-YYYY-MM-DD.md   纯文本版，便于备份与在别处看

用法
----
    python tools/search_plan.py                          # 默认：目标岗位名 × 全部渠道
    python tools/search_plan.py --keywords "AI应用开发,大模型应用"
    python tools/search_plan.py --engine baidu           # 用百度做 site: 检索
    python tools/search_plan.py --open                   # 生成后自动打开 HTML

拿到岗位后怎么录入看板：见 tools/ingest_jd.py（把 JD 文本粘回去即可）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import sys
import webbrowser
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DIST = ROOT / "dist"
PROFILE_FILE = ROOT / "src" / "profile.json"
CHANNELS_FILE = ROOT / "tools" / "channels.json"

ENGINES = {
    "bing": "https://www.bing.com/search?q={q}",
    "baidu": "https://www.baidu.com/s?wd={q}",
    "google": "https://www.google.com/search?q={q}",
}

# 在 profile.target_titles 之外补充的搜索词。
# 岗位名各公司叫法不一，只搜「AI应用开发工程师」会漏掉大量同义岗位。
# 想改搜索面就改这里，或命令行 --keywords 整体覆盖。
EXTRA_KEYWORDS = [
    "大模型应用开发",
    "LLM 应用开发",
    "RAG 工程师",
    "AI Agent 开发",
]


def load_json(path: Path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def build_keywords(profile: dict, extra: list[str] | None = None) -> list[str]:
    """岗位关键词 = 画像里的目标岗位名 + 同义补充词（去重、保序）。"""
    words: list[str] = []
    for word in list(profile.get("target_titles") or []) + list(EXTRA_KEYWORDS if extra is None else extra):
        word = (word or "").strip()
        if word and word not in words:
            words.append(word)
    return words


def search_query(channel: dict, keyword: str, city: str) -> str:
    """拼出检索式：有域名就限定站点，没有就用「渠道名 + 招聘」定位。"""
    domain = (channel.get("domain") or "").strip()
    if domain:
        return f"site:{domain} {city} {keyword}"
    return f"{channel['name']} {city} {keyword} 招聘"


def engine_url(query: str, engine: str = "bing") -> str:
    template = ENGINES.get(engine)
    if template is None:
        raise ValueError(f"未知搜索引擎：{engine}（可选：{'/'.join(ENGINES)}）")
    return template.format(q=quote(query, safe=""))


def site_search_url(channel: dict, keyword: str) -> str | None:
    """站内搜索直达链接；该渠道没配置模板时返回 None（用全网检索兜底）。"""
    template = (channel.get("site_search") or "").strip()
    if not template:
        return None
    if "{q}" in template:
        return template.replace("{q}", quote(keyword, safe=""))
    return template


def channel_links(channel: dict, keywords: list[str], city: str, engine: str = "bing") -> list[dict]:
    """展开单个渠道的全部链接。"""
    links = []
    for keyword in keywords:
        links.append(
            {
                "keyword": keyword,
                "kind": "search",
                "url": engine_url(search_query(channel, keyword, city), engine),
            }
        )
    for keyword in keywords:
        direct = site_search_url(channel, keyword)
        if direct:
            links.append({"keyword": keyword, "kind": "direct", "url": direct})
            break  # 站内搜索每渠道只给一个入口，避免清单过长
    return links


def build_plan(profile: dict, channels: list[dict], engine: str = "bing", city: str | None = None) -> dict:
    city = city or profile.get("city") or "上海"
    keywords = build_keywords(profile)
    groups = []
    for channel in channels:
        groups.append(
            {
                "name": channel["name"],
                "category": channel.get("category", ""),
                "note": channel.get("note", ""),
                "domain": channel.get("domain", ""),
                "links": channel_links(channel, keywords, city, engine),
            }
        )
    return {"city": city, "engine": engine, "keywords": keywords, "groups": groups}


def count_links(plan: dict) -> dict:
    search = sum(1 for g in plan["groups"] for link in g["links"] if link["kind"] == "search")
    direct = sum(1 for g in plan["groups"] for link in g["links"] if link["kind"] == "direct")
    return {"search": search, "direct": direct, "channels": len(plan["groups"]), "keywords": len(plan["keywords"])}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>岗位检索清单 · __DATE__</title>
<style>
:root { --bg:#0b0b0b; --card:#141414; --line:#2a2a2a; --fg:#e8e8e8; --dim:#8a8a8a; --hot:#c41e3a; }
* { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
body { margin:0; padding:16px 14px 48px; background:var(--bg); color:var(--fg);
  font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
h1 { font-size:17px; font-weight:500; margin:0 0 4px; }
.sub { color:var(--dim); font-size:12.5px; margin-bottom:14px; }
.how { background:var(--card); border:1px solid var(--line); border-left:2px solid var(--hot);
  border-radius:8px; padding:10px 12px; margin-bottom:16px; font-size:12.5px; color:var(--dim); }
.how b { color:var(--fg); font-weight:500; }
.how ol { margin:6px 0 0; padding-left:18px; }
.how code { color:var(--hot); font-size:12px; }
.cat { font-size:12px; color:var(--hot); margin:20px 0 8px; letter-spacing:.5px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:12px; margin-bottom:10px; }
.card h2 { font-size:14px; font-weight:500; margin:0; display:inline; }
.badge { font-size:11px; color:var(--dim); border:1px solid var(--line);
  border-radius:999px; padding:1px 7px; margin-left:6px; }
.note { color:var(--dim); font-size:12px; margin:5px 0 9px; }
.chips { display:flex; flex-wrap:wrap; gap:6px; }
.chip { display:inline-block; padding:7px 11px; border:1px solid var(--line); border-radius:8px;
  color:var(--fg); text-decoration:none; font-size:13px; }
.chip:active { background:#1e1e1e; border-color:var(--hot); }
.chip.direct { color:var(--hot); border-color:#3a2028; }
footer { margin-top:22px; padding-top:14px; border-top:1px solid var(--line);
  color:var(--dim); font-size:12px; }
footer code { color:var(--fg); background:#1a1a1a; border-radius:4px; padding:2px 5px;
  display:inline-block; margin-top:4px; }
</style>
</head>
<body>
<h1>岗位检索清单</h1>
<div class="sub">__DATE__ · __CITY__ · __NCH__ 个渠道 × __NKW__ 个关键词 · 共 __NSEARCH__ 条检索</div>

<div class="how">
  <b>怎么用</b>
  <ol>
    <li>点开任意链接，浏览器里就是该渠道的真实搜索结果</li>
    <li>挑中岗位后，把 JD 全文复制下来</li>
    <li>粘回终端录入看板：<code>python tools/ingest_jd.py --file jd.txt --channel 猎聘</code></li>
  </ol>
</div>
__BODY__
<footer>
  本页由 <code>tools/search_plan.py</code> 生成，只是链接清单 —— 不抓取任何页面，不发送任何请求。<br>
  站内搜索链接基于各站公开 URL 规则；若某站改版失效，用同行的检索链接兜底即可。
</footer>
</body>
</html>
"""


def render_html(plan: dict, date: str) -> str:
    counts = count_links(plan)
    blocks: list[str] = []
    current_category = None
    for group in plan["groups"]:
        if group["category"] != current_category:
            current_category = group["category"]
            blocks.append(f'<div class="cat">{html.escape(current_category)}</div>')

        chips = []
        for link in group["links"]:
            cls = "chip direct" if link["kind"] == "direct" else "chip"
            label = "站内搜索" if link["kind"] == "direct" else html.escape(link["keyword"])
            chips.append(
                f'<a class="{cls}" href="{html.escape(link["url"], quote=True)}" '
                f'target="_blank" rel="noopener noreferrer">{label}</a>'
            )
        domain = group["domain"] or "无固定域名"
        blocks.append(
            '<div class="card">'
            f'<h2>{html.escape(group["name"])}</h2>'
            f'<span class="badge">{html.escape(domain)}</span>'
            f'<div class="note">{html.escape(group["note"])}</div>'
            f'<div class="chips">{"".join(chips)}</div>'
            "</div>"
        )

    return (
        HTML_TEMPLATE.replace("__DATE__", date)
        .replace("__CITY__", html.escape(plan["city"]))
        .replace("__NCH__", str(counts["channels"]))
        .replace("__NKW__", str(counts["keywords"]))
        .replace("__NSEARCH__", str(counts["search"]))
        .replace("__BODY__", "\n".join(blocks))
    )


def render_markdown(plan: dict, date: str) -> str:
    counts = count_links(plan)
    lines = [
        f"# 岗位检索清单 · {date}",
        "",
        f"- 城市：{plan['city']}　渠道：{counts['channels']} 个　关键词：{counts['keywords']} 个",
        f"- 检索链接：{counts['search']} 条（全网 site: 检索）＋ {counts['direct']} 条站内直达",
        f"- 关键词：{' / '.join(plan['keywords'])}",
        "",
        "> 本文件由 `tools/search_plan.py` 生成，只是一份链接清单，不抓取任何页面。",
        "",
    ]
    current_category = None
    for group in plan["groups"]:
        if group["category"] != current_category:
            current_category = group["category"]
            lines += [f"## {current_category}", ""]
        lines.append(f"### {group['name']}")
        if group["note"]:
            lines.append(f"_{group['note']}_")
        lines.append("")
        for link in group["links"]:
            label = "站内搜索" if link["kind"] == "direct" else link["keyword"]
            lines.append(f"- [ ] {label} → {link['url']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def default_out(html_path: str | None, md_path: str | None, date: str) -> tuple[Path, Path]:
    return (
        Path(html_path) if html_path else DIST / "search-plan.html",
        Path(md_path) if md_path else DATA / f"search-plan-{date}.md",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="生成岗位检索清单（20+ 渠道 × 岗位关键词），不抓取任何页面",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="录入岗位见：python tools/ingest_jd.py --help",
    )
    parser.add_argument("--profile", default=str(PROFILE_FILE), help="画像文件，默认 src/profile.json")
    parser.add_argument("--channels", default=str(CHANNELS_FILE), help="渠道清单，默认 tools/channels.json")
    parser.add_argument("--keywords", help="逗号分隔，整体覆盖默认关键词")
    parser.add_argument("--city", help="覆盖画像里的城市")
    parser.add_argument("--engine", default="bing", choices=sorted(ENGINES), help="用哪个搜索引擎做限定检索")
    parser.add_argument("--date", default=dt.date.today().isoformat(), help="产物里的日期（默认今天）")
    parser.add_argument("--out-html", help="HTML 输出路径，默认 dist/search-plan.html")
    parser.add_argument("--out-md", help="Markdown 输出路径，默认 data/search-plan-<date>.md")
    parser.add_argument("--open", action="store_true", help="生成后自动打开 HTML")
    parser.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = parser.parse_args()

    profile = load_json(Path(args.profile))
    channels = load_json(Path(args.channels))["channels"]

    global EXTRA_KEYWORDS
    if args.keywords:
        EXTRA_KEYWORDS = []
        profile["target_titles"] = [w.strip() for w in args.keywords.split(",") if w.strip()]

    plan = build_plan(profile, channels, args.engine, args.city)
    counts = count_links(plan)
    print(
        f"渠道 {counts['channels']} 个 × 关键词 {counts['keywords']} 个 "
        f"→ 检索链接 {counts['search']} 条，站内直达 {counts['direct']} 条"
    )
    print("关键词：" + " / ".join(plan["keywords"]))

    if args.dry_run:
        return

    html_path, md_path = default_out(args.out_html, args.out_md, args.date)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(plan, args.date), encoding="utf-8")
    md_path.write_text(render_markdown(plan, args.date), encoding="utf-8")
    print(f"已写入 {html_path.relative_to(ROOT) if html_path.is_relative_to(ROOT) else html_path}")
    print(f"已写入 {md_path.relative_to(ROOT) if md_path.is_relative_to(ROOT) else md_path}")

    if args.open:
        webbrowser.open(html_path.resolve().as_uri())


if __name__ == "__main__":
    sys.exit(main())
