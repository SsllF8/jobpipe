#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自荐信生成与合并的测试。

跑法：
    python -m pytest tests -q

测试四层：
  1. 提示词 —— 占位符填充、护栏条款是否仍在
  2. 缓存层 —— letters.json 的读取容错、按 id 合并
  3. 选岗逻辑 —— 只挑 S 级、跳过已生成、--force / --job 行为
  4. 构建层 —— 自荐信进不进产物（**可公开产物必须排除**）
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import build as builder  # noqa: E402
import gen_letter as gl  # noqa: E402


SAMPLE_JOB = {
    "id": "demo-job",
    "company": "示例科技有限公司",
    "title": "AI 应用开发工程师",
    "salary": "15-20k",
    "exp_req": "1-3年",
    "edu_req": "本科",
    "area": "上海 · 浦东新区",
    "channel": "全职招聘网",
    "why_match": ["做过 RAG", "做过工作流引擎"],
    "gaps": ["无生产级交付经验"],
}


def write_letters(path: Path, entries: dict) -> Path:
    path.write_text(
        json.dumps({"version": 1, "letters": entries}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def empty_state(path: Path) -> Path:
    """一份「没有真实数据」的 state，让构建走公开版分支。"""
    path.write_text(json.dumps({"patches": {}, "added": [], "removed": []}), encoding="utf-8")
    return path


def inlined_data(html: str) -> dict:
    match = re.search(r"window\.__DATA__ = (\{.*?\n\});", html, re.S)
    assert match, "产物里找不到内联的 __DATA__"
    return json.loads(match.group(1))


# ------------------------------------------------------------------ 1 提示词


def test_prompt_template_keeps_its_guardrails():
    """护栏条款任何一条被删掉，自荐信质量就会崩，这里当回归防线。"""
    template = gl.read_text(gl.PROMPT_FILE)
    for needle in ["不得编造", "Coze", "疑问句", "贵司", "450", "550"]:
        assert needle in template, f"提示词模板缺少约束：{needle}"


def test_build_prompt_fills_every_placeholder():
    template = gl.read_text(gl.PROMPT_FILE)
    out = gl.build_prompt(template, SAMPLE_JOB, "# 简历\n这是我的简历正文")

    assert "{{" not in out and "}}" not in out, "还有占位符没被替换"
    assert SAMPLE_JOB["company"] in out
    assert SAMPLE_JOB["title"] in out
    assert "15-20k" in out
    assert "这是我的简历正文" in out


def test_build_prompt_bulletizes_match_lists():
    template = gl.read_text(gl.PROMPT_FILE)
    out = gl.build_prompt(template, SAMPLE_JOB, "简历")
    assert "1. 做过 RAG" in out
    assert "2. 做过工作流引擎" in out


def test_build_prompt_tolerates_missing_optional_fields():
    template = gl.read_text(gl.PROMPT_FILE)
    bare = {"company": "只有公司名", "title": "岗位"}
    out = gl.build_prompt(template, bare, "简历")
    assert "{{" not in out
    assert "（未注明）" in out


def test_build_prompt_rejects_unknown_placeholder():
    with pytest.raises(SystemExit):
        gl.build_prompt("未知占位符 {{nope}}", SAMPLE_JOB, "简历")


def test_read_env_handles_comments_and_quotes(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# 注释行\n"
        "DEEPSEEK_API_KEY=sk-abc\n"
        "\n"
        'QUOTED="带引号的值"\n'
        "SPACED = 前后有空格 \n"
        "NOEQUALS\n",
        encoding="utf-8",
    )
    env = gl.read_env(env_file)
    assert env["DEEPSEEK_API_KEY"] == "sk-abc"
    assert env["QUOTED"] == "带引号的值"
    assert env["SPACED"] == "前后有空格"
    assert "NOEQUALS" not in env


def test_read_env_missing_file_is_empty(tmp_path):
    assert gl.read_env(tmp_path / "不存在") == {}


# ------------------------------------------------------------------ 2 缓存层


def test_load_letters_skips_empty_and_malformed(tmp_path):
    path = write_letters(
        tmp_path / "letters.json",
        {
            "ok": {"text": "正常正文"},
            "blank": {"text": "   "},
            "no-text": {"company": "没有正文字段"},
            "bad": "不是字典",
            "": {"text": "空 id"},
        },
    )
    assert builder.load_letters(path) == {"ok": "正常正文"}


def test_load_letters_missing_file(tmp_path):
    assert builder.load_letters(tmp_path / "没有这个文件") == {}


def test_load_letters_bad_root(tmp_path):
    path = tmp_path / "letters.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert builder.load_letters(path) == {}


def test_merge_letters_only_fills_matching_ids():
    records = [{"id": "a"}, {"id": "b"}]
    out, applied = builder.merge_letters(records, {"a": "正文 A", "zzz": "无主自荐信"})
    assert applied == 1
    assert out[0]["cover_letter"] == "正文 A"
    assert "cover_letter" not in out[1], "没匹配上的岗位不该被写入空字段"


# ------------------------------------------------------------------ 3 选岗逻辑


SAMPLE_POOL = [
    {"id": "s1", "tier": "S", "score": 88},
    {"id": "s2", "tier": "S", "score": 92},
    {"id": "a1", "tier": "A", "score": 70},
]


def test_pick_targets_defaults_to_s_tier_only():
    got = gl.pick_targets(SAMPLE_POOL, {}, None, "S", False, False)
    assert [j["id"] for j in got] == ["s2", "s1"], "应按分数倒序，且不包含 A 级"


def test_pick_targets_skips_already_generated():
    got = gl.pick_targets(SAMPLE_POOL, {"s1": {"text": "已有"}}, None, "S", False, False)
    assert [j["id"] for j in got] == ["s2"]


def test_pick_targets_force_includes_generated():
    got = gl.pick_targets(SAMPLE_POOL, {"s1": {"text": "已有"}}, None, "S", False, True)
    assert [j["id"] for j in got] == ["s2", "s1"]


def test_pick_targets_all_ignores_tier():
    got = gl.pick_targets(SAMPLE_POOL, {}, None, "S", True, False)
    assert {j["id"] for j in got} == {"s1", "s2", "a1"}


def test_pick_targets_explicit_job_still_respects_cache():
    """--job 是「指定范围」，不是「强制重做」；要去重跑得再加 --force。"""
    cached = {"a1": {"text": "已有"}}
    assert gl.pick_targets(SAMPLE_POOL, cached, ["a1"], "S", False, False) == []
    got = gl.pick_targets(SAMPLE_POOL, cached, ["a1"], "S", False, True)
    assert [j["id"] for j in got] == ["a1"]


def test_pick_targets_unknown_job_raises():
    with pytest.raises(SystemExit):
        gl.pick_targets(SAMPLE_POOL, {}, ["不存在"], "S", False, False)


def test_tier_of_prefers_manual_tier():
    assert gl.tier_of({"tier": "S", "tier_auto": "C"}) == "S"
    assert gl.tier_of({"tier_auto": "B"}) == "B"
    assert gl.tier_of({}) == "C"


def test_merge_usage_sums_tokens():
    got = gl.merge_usage({"prompt_tokens": 10, "completion_tokens": 5},
                         {"prompt_tokens": 3}, None)
    assert got == {"prompt_tokens": 13, "completion_tokens": 5}


# ------------------------------------------------------------------ 4 构建层


def test_demo_build_inlines_letters(tmp_path):
    letters = write_letters(tmp_path / "letters.json", {"liaoning-xinxi-ae": {"text": "演示用自荐信正文"}})
    out = tmp_path / "preview-demo.html"
    builder.build(demo=True, out=out, letters_path=letters)
    assert "演示用自荐信正文" in out.read_text(encoding="utf-8")


def test_publishable_build_excludes_letters(tmp_path):
    """可发布的 index.html 不能带自荐信 —— 正文点名了投递的公司。"""
    letters = write_letters(tmp_path / "letters.json", {"liaoning-xinxi-ae": {"text": "不该出现在公开产物里"}})
    out = tmp_path / "index.html"
    builder.build(demo=False, out=out, letters_path=letters,
                  state_path=empty_state(tmp_path / "state.json"))

    html = out.read_text(encoding="utf-8")
    assert "不该出现在公开产物里" not in html
    payload = inlined_data(html)
    assert len(payload["applications"]) == 17, "岗位数据本身必须还在"
    assert all(not (r.get("cover_letter") or "").strip() for r in payload["applications"])


def test_personal_build_inlines_letters(tmp_path):
    """个人版（local.html）要带上自荐信，手机上才复制得到。"""
    letters = write_letters(tmp_path / "letters.json", {"liaoning-xinxi-ae": {"text": "个人版自荐信"}})
    out = tmp_path / "local.html"
    builder.build(demo=False, out=out, letters_path=letters,
                  state_path=empty_state(tmp_path / "state.json"))
    assert "个人版自荐信" in out.read_text(encoding="utf-8")


def test_real_letters_never_reach_publishable_artifact(tmp_path):
    """回归防线：真实 letters.json 存在时，公开产物也不得泄漏。"""
    if not builder.LETTERS.exists():
        pytest.skip("还没生成过自荐信")
    real = builder.load_letters(builder.LETTERS)
    if not real:
        pytest.skip("letters.json 里没有有效正文")

    out = tmp_path / "index.html"
    builder.build(demo=False, out=out,
                  state_path=empty_state(tmp_path / "state.json"))
    html = out.read_text(encoding="utf-8")

    payload = inlined_data(html)
    assert all(not (r.get("cover_letter") or "").strip() for r in payload["applications"])
    for job_id, text in real.items():
        assert text[:20] not in html, f"{job_id} 的自荐信泄漏进了公开产物"


def test_build_warns_about_orphan_letters(tmp_path, capsys):
    """letters.json 里对不上岗位池的条目要被忽略并告警，而不是让构建失败。"""
    letters = write_letters(tmp_path / "letters.json", {
        "liaoning-xinxi-ae": {"text": "正常"},
        "早已删除的岗位": {"text": "孤儿自荐信"},
    })
    out = tmp_path / "preview-demo.html"
    builder.build(demo=True, out=out, letters_path=letters)

    printed = capsys.readouterr().out
    assert "对不上岗位池" in printed
    assert "孤儿自荐信" not in out.read_text(encoding="utf-8")
