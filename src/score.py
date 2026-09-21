#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯规则匹配打分引擎 —— 零 AI 调用。

设计原则
--------
1. **不调用任何 AI**。全部是确定性字符串匹配与规则判断，可重复、可测试、零成本。
2. **口径全部在 ``profile.json``**。技能词表、方向词表、权重、分档阈值都外置，改口径不用改代码。
3. **输入文本的取法**：优先用岗位原始 JD（``jd_text``）。种子数据没有原始 JD，
   退化为 ``title + why_match`` 作为代理文本 —— ``why_match`` 是人工从 JD 提炼的
   要求摘要，可当作关键词代理。
   ⚠️ 接入采集层后应改用真实 ``jd_text``，并让 ``why_match`` 退出打分文本，
   否则会形成"人工判断 → 关键词 → 打分"的循环论证。

四个维度（权重来自 profile.json 的 weights）
--------------------------------------------
    技能重合   0-40   画像技能词在文本中的加权命中量
    门槛可达   0-30   经验 / 学历门槛与「0-2 年经验 + 本科」的差距
    方向契合   0-20   AI 应用方向的正面关键词，减去硬性不匹配的负面关键词
    薪资通勤   0-10   薪资下限 + 通勤区域
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 基础工具


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_json(obj: Any, path: str | Path) -> None:
    Path(path).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def build_haystack(job: dict) -> str:
    """把岗位里可用于关键词匹配的文本拼成一个可搜索字符串。"""
    parts = [str(job.get("title", "")), str(job.get("jd_text", ""))]
    if not job.get("jd_text"):
        parts.append(" ".join(job.get("why_match") or []))
    return " ".join(parts).lower()


# ---------------------------------------------------------------- 维度一：技能重合


SKILL_TARGET = 8.0  # 命中 8 个「核心技能当量」才算满分，避免轻易触顶


def score_skill(job: dict, profile: dict, haystack: str) -> tuple[float, dict]:
    max_score = float(profile["weights"]["skill"])
    skills = profile["skills"]

    weight = 0.0
    hits: list[str] = []
    for kw in skills["core"]:
        if kw.lower() in haystack:
            weight += 1.0
            hits.append(kw)
    for kw in skills["familiar"]:
        if kw.lower() in haystack:
            weight += 0.6
            hits.append(kw)
    for kw in skills["plus"]:
        if kw.lower() in haystack:
            weight += 0.4
            hits.append(kw)

    value = round(clamp(weight / SKILL_TARGET, 0.0, 1.0) * max_score, 1)
    return value, {"hits": hits, "weighted": round(weight, 2)}


# ---------------------------------------------------------------- 维度二：门槛可达

# 顺序敏感：更具体的模式必须排在更宽泛的前面
EXP_RULES: list[tuple[str, float]] = [
    (r"20(2[6-9]|3\d)\s*届", 8.0),          # 明确限定未来届次，往届基本无门
    (r"校招|校园招聘", 12.0),                # 校招通道，通常卡届次
    (r"经验不限|不限经验|经验不设限", 30.0),
    (r"应届生可投|学生可投|应届毕业生|应届生", 28.0),
    (r"应届|毕业生", 24.0),                  # 只写"应届"的，口径模糊，折中
    (r"1\s*[-~—至]\s*3\s*年", 27.0),
    (r"3\s*[-~—至]\s*5\s*年", 15.0),
    (r"6\s*年|8\s*年|10\s*年", 4.0),
    (r"5\s*年", 8.0),
    (r"4\s*年", 12.0),
    (r"3\s*年", 18.0),
    (r"2\s*年", 24.0),
    (r"1\s*年", 26.0),
]

EDU_ADJUST: list[tuple[str, float]] = [
    (r"硕士|研究生|博士", -8.0),
    (r"大专|专科", 2.0),
]

NEUTRAL_BARRIER = 20.0  # 无法解析门槛时给中位分，不奖励也不惩罚


def score_barrier(job: dict, profile: dict) -> tuple[float, dict]:
    max_score = float(profile["weights"]["barrier"])
    text = f"{job.get('exp_req', '')} {job.get('edu_req', '')}"
    text = text.replace(" ", "") if "年" not in text else text

    base = NEUTRAL_BARRIER
    matched_exp = None
    for pattern, value in EXP_RULES:
        if re.search(pattern, text):
            base = value
            matched_exp = pattern
            break

    adjust = 0.0
    matched_edu = None
    for pattern, delta in EDU_ADJUST:
        if re.search(pattern, text):
            adjust += delta
            matched_edu = pattern
            break

    value = round(clamp(base + adjust, 0.0, max_score), 1)
    return value, {"exp_rule": matched_exp, "edu_rule": matched_edu, "base": base, "adjust": adjust}


# ---------------------------------------------------------------- 维度三：方向契合


def score_direction(profile: dict, haystack: str) -> tuple[float, dict]:
    max_score = float(profile["weights"]["direction"])
    positive: dict[str, list[str]] = profile["direction"]["positive"]
    negative: dict[str, list[str]] = profile["direction"]["negative"]

    hit_groups = [
        group for group, kws in positive.items() if any(k.lower() in haystack for k in kws)
    ]
    block_groups = [
        group for group, kws in negative.items() if any(k.lower() in haystack for k in kws)
    ]

    step = max_score / len(positive)
    value = len(hit_groups) * step - len(block_groups) * 6.0
    value = round(clamp(value, 0.0, max_score), 1)
    return value, {"hit_groups": hit_groups, "block_groups": block_groups}


# ---------------------------------------------------------------- 维度四：薪资通勤


def parse_salary_lower_k(text: str) -> int | None:
    """从薪资文本里抽出下限（单位 k）。'12-16k' -> 12，'面议' -> None。"""
    if not text:
        return None
    match = re.search(r"(\d{1,3})\s*[-~—至]\s*(\d{1,3})\s*k", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d{1,3})\s*k", text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    match = re.search(r"(\d{4,6})\s*元", text)
    if match:
        return int(int(match.group(1)) / 1000)
    return None


def score_compensation(job: dict, profile: dict) -> tuple[float, dict]:
    text = str(job.get("salary", ""))
    area = str(job.get("area", ""))
    lower = parse_salary_lower_k(text)

    if lower is None:
        salary_part = 3.0
    elif lower >= 15:
        salary_part = 5.0
    elif lower >= 12:
        salary_part = 4.5
    elif lower >= 10:
        salary_part = 3.0
    elif lower >= 8:
        salary_part = 2.0
    else:
        salary_part = 1.0

    if any(d in area for d in profile["commute_good"]):
        area_part = 5.0
    elif any(d in area for d in profile["districts"]):
        area_part = 3.5
    else:
        area_part = 1.0

    value = round(clamp(salary_part + area_part, 0.0, 10.0), 1)
    return value, {"salary_lower_k": lower, "salary_part": salary_part, "area_part": area_part}


# ---------------------------------------------------------------- 汇总


def tier_of(score: float, profile: dict) -> str:
    tiers = profile["tiers"]
    if score >= tiers["S"]:
        return "S"
    if score >= tiers["A"]:
        return "A"
    if score >= tiers["B"]:
        return "B"
    return "C"


def score_job(job: dict, profile: dict) -> dict:
    haystack = build_haystack(job)

    skill, skill_detail = score_skill(job, profile, haystack)
    barrier, barrier_detail = score_barrier(job, profile)
    direction, direction_detail = score_direction(profile, haystack)
    comp, comp_detail = score_compensation(job, profile)

    total = round(clamp(skill + barrier + direction + comp, 0.0, 100.0), 1)
    return {
        "score_auto": total,
        "tier_auto": tier_of(total, profile),
        "breakdown": {
            "skill": skill,
            "barrier": barrier,
            "direction": direction,
            "compensation": comp,
            "detail": {
                "skill": skill_detail,
                "barrier": barrier_detail,
                "direction": direction_detail,
                "compensation": comp_detail,
            },
        },
    }


def score_all(records: list[dict], profile: dict) -> list[dict]:
    """给每条记录补上 score_auto / tier_auto / score_breakdown，不改动其它字段。"""
    out = []
    for job in records:
        enriched = dict(job)
        enriched.update(score_job(job, profile))
        out.append(enriched)
    return out


def resolve_display_score(job: dict) -> tuple[float, str]:
    """对外展示分数：人工分优先，没有人工分才用算法分。"""
    if job.get("score") is not None:
        return float(job["score"]), job.get("score_source", "manual")
    if job.get("score_auto") is not None:
        return float(job["score_auto"]), "auto"
    return 0.0, "none"


# ---------------------------------------------------------------- CLI


def calibrate(records: list[dict]) -> str:
    lines = [
        "人工分 vs 算法分 对照（用于调参，不参与展示）",
        f"{'岗位':<34}{'人工':>6}{'算法':>8}{'差值':>8}  维度明细",
        "-" * 92,
    ]
    diffs = []
    for job in records:
        manual = job.get("score")
        auto = job.get("score_auto")
        if manual is None or auto is None:
            continue
        diff = auto - float(manual)
        diffs.append(diff)
        b = job["breakdown"]
        lines.append(
            f"{job['title'][:32]:<34}{float(manual):>6.1f}{auto:>8.1f}{diff:>+8.1f}"
            f"  技{b['skill']:>4.1f} 槛{b['barrier']:>4.1f} 向{b['direction']:>4.1f} 薪{b['compensation']:>4.1f}"
        )
    if diffs:
        lines.append("-" * 92)
        lines.append(
            f"平均差值 {sum(diffs) / len(diffs):+.1f} ｜ 最大偏差 {max(diffs, key=abs):+.1f}"
            f" ｜ 样本 {len(diffs)}"
        )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="纯规则岗位匹配打分（零 AI）")
    parser.add_argument("--data", required=True, help="岗位数据 JSON 路径")
    parser.add_argument("--profile", required=True, help="画像 JSON 路径")
    parser.add_argument("--out", help="把打分结果写到此文件（不传则只打印）")
    parser.add_argument("--calibrate", action="store_true", help="打印人工分与算法分对照表")
    args = parser.parse_args()

    profile = load_json(args.profile)
    payload = load_json(args.data)
    records = payload["applications"] if isinstance(payload, dict) else payload
    scored = score_all(records, profile)

    if isinstance(payload, dict):
        payload = {**payload, "applications": scored}
    else:
        payload = scored

    if args.calibrate:
        print(calibrate(scored))

    if args.out:
        dump_json(payload, args.out)
        print(f"\n已写入 {args.out}（{len(scored)} 条）")


if __name__ == "__main__":
    main()
