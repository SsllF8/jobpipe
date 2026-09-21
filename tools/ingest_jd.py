#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""JD 录入：把招聘网站上复制的一段文本，解析成看板能吃的岗位条目。

它在采集链路里的位置
--------------------
    tools/search_plan.py   生成检索清单（20+ 渠道的链接）
            ↓  你点开、挑岗位、复制 JD
    tools/ingest_jd.py     规则解析 + 打分 → 合并进 data/seed-YYYY-MM-DD.json
            ↓
    build.py               内联 seed → dist/index.html

为什么用规则解析而不是让 AI 读 JD
---------------------------------
1. 字段就那七八个（公司/岗位/薪资/门槛/地点/链接），正则完全够用，且**结果确定可测**；
2. 采集是每天都要做的动作，走 AI 意味着每天都产生 token 成本 —— 与「把 AI 用在刀刃上」
   的产品原则相反。真正的 AI 环节只留在自荐信（且只对 S 级岗位生成一次）。

解析不出来的字段会留空并**在报告里点名**，配合 --company/--title/--url 等参数手工补。
宁可空着，也不猜 —— 猜错的岗位信息会污染后面积累的所有投递记录。

用法
----
    # 单个岗位，字段从文本里能认出来
    python tools/ingest_jd.py --file jd.txt --channel 猎聘

    # 参数兜底（推荐：公司名和岗位名一律显式给，最省事也最准）
    python tools/ingest_jd.py --file jd.txt --company "某某科技有限公司" \
        --title "AI应用开发工程师" --channel BOSS直聘 --url "https://..." --salary "15-25k"

    # 直接贴一段文本
    python tools/ingest_jd.py --text "..." --company X --title Y

    # 一次录多个：用一行 --- 分隔
    python tools/ingest_jd.py --file many.txt

    # 只看解析结果，不写文件
    python tools/ingest_jd.py --file jd.txt --dry-run

    # 记不清渠道名怎么拼
    python tools/ingest_jd.py --list-channels
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SRC = ROOT / "src"
CHANNELS_FILE = ROOT / "tools" / "channels.json"
PROFILE_FILE = SRC / "profile.json"

sys.path.insert(0, str(SRC))
from score import score_job  # noqa: E402
from seed_store import latest_seed  # noqa: E402

SPLIT_RE = re.compile(r"^\s*(?:-{3,}|={3,})\s*$", re.MULTILINE)
URL_RE = re.compile(r"https?://[^\s，。、）)】\]\"'<>]+")
# 各站写法不一：15-25k / 12k-18K / 20~30千 / 15K-25K·13薪
# 所以前一个数字后的单位是**可选**的，后一个必需
SALARY_RE = re.compile(
    r"(\d{1,3}(?:\.\d)?)\s*[kK千]?\s*[-–~至]\s*(\d{1,3}(?:\.\d)?)\s*[kK千]"
)
EXP_RANGE_RE = re.compile(r"(\d{1,2})\s*[-–~至]\s*(\d{1,2})\s*年")
EXP_ABOVE_RE = re.compile(r"(\d{1,2})\s*年以上")
EDU_WORDS = ["学历不限", "本科", "硕士", "大专", "博士", "中专"]
# 公司名分两级匹配：优先取「…有限公司」全称，取不到再退回带「科技/集团」等字样的简称。
# 必须分两级：只用简称那级时，非贪婪量词会在「上海某某智能科技」处就停下，
# 把「有限公司」丢掉 —— 而公司名是回查岗位、判断重复的关键字段，不能残缺。
COMPANY_FULL_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?(?:股份有限公司|有限公司)"
)
COMPANY_SHORT_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9（）()·]{2,40}?(?:信息技术|科技|集团|研究院|软件|工作室)"
)
TITLE_HINT_RE = re.compile(r"(工程师|开发|架构师|专家|负责人|研究员|技术经理|算法)")

# JD 全文会被内联进单文件产物（看板的「原始 JD」层要用它），
# 从网页复制时容易带上导航栏等无关内容，截断防止产物体积失控。
# 打分的关键词匹配也只看这一段，太长没有额外收益。
MAX_JD_CHARS = 8000


def load_json(path: Path):
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle)


def load_channels(path: Path = CHANNELS_FILE) -> dict[str, dict]:
    """渠道名 → 渠道定义，用于把 channel 自动映射到 channel_type。"""
    channels = load_json(path)["channels"]
    table: dict[str, dict] = {}
    for channel in channels:
        table[channel["name"]] = channel
    return table


def split_records(text: str) -> list[str]:
    """一段文本里可能贴了多个岗位，用 `---` 分隔。"""
    parts = [chunk.strip() for chunk in SPLIT_RE.split(text)]
    return [chunk for chunk in parts if chunk]


def first_match(pattern: re.Pattern, text: str) -> str | None:
    found = pattern.search(text)
    return found.group(0).strip() if found else None


def extract_url(text: str) -> str:
    found = URL_RE.search(text)
    return found.group(0) if found else ""


def extract_salary(text: str) -> str:
    hit = SALARY_RE.search(text)
    if hit:
        low, high = hit.group(1), hit.group(2)
        return f"{low}-{high}k"
    # 只写了一个数字的情况（如「月薪 20K 起」）
    single = re.search(r"(\d{2,3})\s*[kK]", text)
    return f"{single.group(1)}k" if single else ""


def extract_exp(text: str) -> str:
    if "应届" in text:
        return "应届可投"
    if "经验不限" in text or "不限经验" in text:
        return "经验不限"
    hit = EXP_RANGE_RE.search(text)
    if hit:
        return f"{hit.group(1)}-{hit.group(2)}年"
    above = EXP_ABOVE_RE.search(text)
    if above:
        return f"{above.group(1)}年以上"
    return ""


def extract_edu(text: str) -> str:
    for word in EDU_WORDS:
        if word in text:
            return word
    return ""


def extract_area(text: str, districts: list[str]) -> str:
    for district in districts:
        if district in text:
            return f"上海 · {district}"
    return "上海" if "上海" in text else ""


def extract_company(text: str) -> str:
    for pattern in (COMPANY_FULL_RE, COMPANY_SHORT_RE):
        hit = pattern.search(text)
        if hit:
            return hit.group(0)
    return ""


def extract_title(text: str) -> str:
    """岗位名优先取带「工程师/开发/架构师」等字样的短行。"""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = [line for line in lines if TITLE_HINT_RE.search(line) and len(line) <= 40]
    if candidates:
        return min(candidates, key=len)
    return lines[0][:40] if lines else ""


def make_id(company: str, title: str) -> str:
    key = f"{company}|{title}".encode("utf-8")
    return "auto-" + hashlib.sha1(key).hexdigest()[:8]


def parse_jd(text: str, profile: dict, overrides: dict | None = None) -> dict:
    """把一段 JD 文本解析成岗位条目（字段缺失就留空，不做猜测）。"""
    overrides = overrides or {}
    districts = profile.get("districts") or []

    company = (overrides.get("company") or "").strip() or extract_company(text)
    title = (overrides.get("title") or "").strip() or extract_title(text)
    salary = (overrides.get("salary") or "").strip() or extract_salary(text)

    job = {
        "id": make_id(company, title),
        "company": company,
        "title": title,
        "tier": None,
        "score": None,
        "score_source": "auto",
        "salary": salary,
        "area": (overrides.get("area") or "").strip() or extract_area(text, districts),
        "exp_req": (overrides.get("exp") or "").strip() or extract_exp(text),
        "edu_req": (overrides.get("edu") or "").strip() or extract_edu(text),
        "channel": (overrides.get("channel") or "").strip(),
        "channel_type": "",
        "url": (overrides.get("url") or "").strip() or extract_url(text),
        "status": "wishlist",
        "applied_at": None,
        "resume_version": None,
        "next_action": "评估",
        "next_action_at": None,
        "cover_letter": "",
        "contacts": [],
        "timeline": [],
        "found_at": dt.date.today().isoformat(),
        "advice": "",
        "why_match": [],
        "gaps": [],
        "notes": "",
        "jd_text": text.strip()[:MAX_JD_CHARS],
    }
    return job


def explain(job: dict, scored: dict, profile: dict) -> None:
    """按打分明细生成 why_match 与 gaps —— 只罗列**事实命中**，不编故事。

    字段名严格对齐 src/score.py 的 breakdown.detail：
        skill        {hits, weighted}
        barrier      {exp_rule, edu_rule, base, adjust}
        direction    {hit_groups, block_groups}
        compensation {salary_lower_k, salary_part, area_part}
    """
    breakdown = scored["breakdown"]
    detail = breakdown["detail"]
    why: list[str] = []
    gaps: list[str] = []

    hits = [h for h in (detail["skill"].get("hits") or []) if h]
    if hits:
        why.append("JD 命中核心技能：" + " / ".join(hits[:5]))

    dir_groups = detail["direction"].get("hit_groups") or []
    if dir_groups:
        why.append("方向对口：" + " / ".join(dir_groups[:2]))

    if job.get("exp_req"):
        if breakdown["barrier"] >= 24:
            why.append(f"门槛友好：{job['exp_req']}")
        else:
            why.append(f"门槛要求：{job['exp_req']}（按画像这一项会拉低总分）")

    comp = detail["compensation"]
    if comp.get("salary_part", 0.0) >= 4.5:
        why.append(f"薪资达标：{job['salary']}")
    if comp.get("area_part", 0.0) >= 5.0:
        why.append(f"通勤落在优先区域：{job['area']}")

    job["why_match"] = why

    for group in detail["direction"].get("block_groups") or []:
        gaps.append(f"JD 涉及「{group}」，画像里标为弱方向，面试前想好怎么答")
    missing = [name for name in ("salary", "area", "exp_req", "edu_req") if not job.get(name)]
    if missing:
        gaps.append("字段待补：" + " / ".join(missing) + "（补全后分数才准）")
    job["gaps"] = gaps

    tier = scored["tier_auto"]
    advice = {
        "S": "命中度高，建议优先投递。",
        "A": "值得投，投前把 JD 里的硬性要求逐条对一遍。",
        "B": "命中一般，时间够再投。",
        "C": "重合度低，除非有特别理由，建议跳过。",
    }[tier]
    if missing:
        advice += "（待补字段：" + "/".join(missing) + "）"
    job["advice"] = advice
    job["score_source"] = "auto"


def find_duplicates(job: dict, records: list[dict]) -> str | None:
    """返回重复原因，没重复返回 None。"""
    url = (job.get("url") or "").strip()
    for old in records:
        if url and (old.get("url") or "").strip() == url:
            return f"url 与已有岗位 {old.get('id')} 相同"
    key = (job.get("company"), job.get("title"))
    for old in records:
        if (old.get("company"), old.get("title")) == key and all(key):
            return f"公司+岗位与已有岗位 {old.get('id')} 相同"
    if job.get("id") in {old.get("id") for old in records}:
        return "生成的 id 与已有岗位相同"
    return None


def merge_into_seed(new_jobs: list[dict], out_path: Path, seed_path: Path) -> dict:
    seed = load_json(seed_path) if Path(seed_path).exists() else {"version": 1, "applications": []}
    records = list(seed.get("applications") or [])
    records.extend(new_jobs)
    payload = {
        "version": seed.get("version", 1),
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "source": "上游人工核实岗位 + tools/ingest_jd.py 录入",
        "applications": records,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def report(parsed: list[tuple[dict, dict]], dups: list[tuple[dict, str]], total: int) -> None:
    if dups:
        print(f"\n跳过 {len(dups)} 条重复：")
        for job, reason in dups:
            print(f"  - {job['company'] or '(公司未识别)'} · {job['title'] or '(岗位未识别)'} —— {reason}")
    if not parsed:
        print("\n没有新岗位需要写入。")
        return
    print(f"\n新增 {len(parsed)} 条（岗位池共 {total} 条）：")
    for job, scored in parsed:
        print(f"  · {job['company'] or '(公司未识别)'} · {job['title'] or '(岗位未识别)'}")
        print(
            f"    {scored['score_auto']} 分 / {scored['tier_auto']} 级"
            f" | {job['salary'] or '薪资待补'}"
            f" | {job['area'] or '地点待补'}"
            f" | {job['exp_req'] or '门槛待补'}"
        )
        for line in job["why_match"]:
            print(f"    - {line}")
        print(f"    建议：{job['advice']}")
        if not job["company"] or not job["title"]:
            print("    ⚠ 公司或岗位名没认出来，建议用 --company / --title 显式指定后重录")
        if not job["url"]:
            print("    ⚠ 没解析到链接，建议用 --url 补上（去重靠它）")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 JD 文本解析成看板岗位条目（规则解析，零 AI 调用）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text", help="直接传一段 JD 文本")
    source.add_argument("--file", help="从文件读取 JD 文本（多个岗位用 --- 分隔）")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取")

    parser.add_argument("--company", help="公司名（推荐显式给，解析最准）")
    parser.add_argument("--title", help="岗位名（推荐显式给）")
    parser.add_argument("--channel", help="来源渠道，如 猎聘 / BOSS直聘 / V2EX 酷工作")
    parser.add_argument("--url", help="岗位链接（去重与回查靠它）")
    parser.add_argument("--salary", help="薪资，如 15-25k")
    parser.add_argument("--area", help="地点，如 上海 · 浦东新区")
    parser.add_argument("--exp", help="经验要求，如 1-3年 / 应届可投")
    parser.add_argument("--edu", help="学历要求")

    parser.add_argument("--seed", help="读取哪份岗位池做去重，默认 data/ 里最新的")
    parser.add_argument("--out", help="写到哪个 seed 文件，默认 data/seed-<今天>.json")
    parser.add_argument("--profile", default=str(PROFILE_FILE), help="画像文件")
    parser.add_argument("--dry-run", action="store_true", help="只打印结果，不写文件")
    parser.add_argument("--list-channels", action="store_true", help="列出渠道名（填 --channel 用）")
    args = parser.parse_args()

    channels = load_channels()

    if args.list_channels:
        print("可用渠道名（按类别）：")
        current = None
        for channel in channels.values():
            if channel["category"] != current:
                current = channel["category"]
                print(f"\n[{current}]")
            print(f"  {channel['name']}")
        return

    if args.text:
        raw = args.text
    elif args.file:
        raw = Path(args.file).read_text(encoding="utf-8")
    elif args.stdin:
        raw = sys.stdin.read()
    else:
        parser.error("要从哪里读 JD？请用 --text / --file / --stdin 之一（--help 有示例）")

    chunks = split_records(raw)
    if not chunks:
        parser.error("没读到任何内容")

    profile = load_json(Path(args.profile))
    seed_path = Path(args.seed) if args.seed else latest_seed(DATA)
    out_path = Path(args.out) if args.out else DATA / f"seed-{dt.date.today().isoformat()}.json"

    existing = load_json(seed_path).get("applications", []) if seed_path.exists() else []
    overrides = {
        "company": args.company,
        "title": args.title,
        "channel": args.channel,
        "url": args.url,
        "salary": args.salary,
        "area": args.area,
        "exp": args.exp,
        "edu": args.edu,
    }

    parsed: list[tuple[dict, dict]] = []
    dups: list[tuple[dict, str]] = []
    seen: list[dict] = list(existing)

    print(f"读取岗位池：{seed_path.name}（{len(existing)} 条）")
    for chunk in chunks:
        job = parse_jd(chunk, profile, overrides)
        # 渠道 → 来源类别（用于统计「哪个渠道出精品多」）
        channel_def = channels.get(job["channel"])
        if channel_def:
            job["channel_type"] = channel_def.get("category", "")

        reason = find_duplicates(job, seen)
        if reason:
            dups.append((job, reason))
            continue

        scored = score_job(job, profile)
        explain(job, scored, profile)
        parsed.append((job, scored))
        seen.append(job)

    report(parsed, dups, len(existing) + len(parsed))

    if args.dry_run:
        print("\n--dry-run：未写入文件。")
        return
    if not parsed:
        return

    payload = merge_into_seed([job for job, _ in parsed], out_path, seed_path)
    print(f"\n已写入 {out_path}")
    print(f"岗位池合计 {len(payload['applications'])} 条")
    print("下一步：python build.py   （重新构建看板）")


if __name__ == "__main__":
    sys.exit(main())
