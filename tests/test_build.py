#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建产物与打分引擎的测试。

跑法：
    python -m pytest tests -q

测试分三层：
  1. 数据层 —— 种子数据的完整性与一致性
  2. 算法层 —— 打分的确定性、值域、分档映射、薪资解析
  3. 构建层 —— 单文件产物是否真的零外链、占位符是否全部替换、内联 JSON 是否合法
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

import build as builder  # noqa: E402
from score import (  # noqa: E402
    load_json,
    parse_salary_lower_k,
    score_all,
    score_job,
    tier_of,
)

VALID_STATUS = {
    "wishlist", "applied", "screening", "interview",
    "offer", "rejected", "dropped", "ghosted",
}
VALID_TIER = {"S", "A", "B", "C"}
REQUIRED_FIELDS = ["id", "company", "title", "status", "tier", "score", "channel", "url"]


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def profile():
    return load_json(ROOT / "src" / "profile.json")


@pytest.fixture(scope="module")
def seed():
    return load_json(ROOT / "data" / "seed-2026-09-17.json")["applications"]


@pytest.fixture(scope="module")
def scored(seed, profile):
    return score_all(seed, profile)


# ------------------------------------------------------------------ 1 数据层


def test_seed_has_17_records(seed):
    assert len(seed) == 17


def test_ids_are_unique(seed):
    ids = [r["id"] for r in seed]
    assert len(ids) == len(set(ids)), "存在重复的岗位 id"


def test_required_fields_present(seed):
    for rec in seed:
        for field in REQUIRED_FIELDS:
            assert field in rec, f"{rec.get('id')} 缺少字段 {field}"


def test_status_and_tier_are_legal(seed):
    for rec in seed:
        assert rec["status"] in VALID_STATUS, f"{rec['id']} 状态非法：{rec['status']}"
        assert rec["tier"] in VALID_TIER, f"{rec['id']} 分档非法：{rec['tier']}"


def test_scores_are_within_range(seed):
    for rec in seed:
        assert 0 <= rec["score"] <= 100, f"{rec['id']} 分数越界：{rec['score']}"


def test_urls_are_http(seed):
    for rec in seed:
        assert re.match(r"https?://", rec["url"]), f"{rec['id']} 的 url 不合法"


def test_every_record_has_match_evidence(seed):
    """红线：每个岗位都要有可解释的匹配理由，不能只有分数。"""
    for rec in seed:
        assert rec["why_match"], f"{rec['id']} 没有匹配理由"
        assert rec["gaps"], f"{rec['id']} 没有能力缺口说明"


# ------------------------------------------------------------------ 2 算法层


def test_scoring_is_deterministic(seed, profile):
    a = [r["score_auto"] for r in score_all(seed, profile)]
    b = [r["score_auto"] for r in score_all(seed, profile)]
    assert a == b, "同样的输入必须得到同样的分数"


def test_auto_score_within_range(scored):
    for rec in scored:
        assert 0 <= rec["score_auto"] <= 100, f"{rec['id']} 算法分越界：{rec['score_auto']}"


def test_breakdown_sums_to_total(scored):
    for rec in scored:
        parts = rec["breakdown"]
        total = parts["skill"] + parts["barrier"] + parts["direction"] + parts["compensation"]
        assert abs(total - rec["score_auto"]) < 0.15, (
            f"{rec['id']} 维度之和 {total} 与总分 {rec['score_auto']} 不一致"
        )


def test_each_dimension_respects_its_weight(scored, profile):
    weights = profile["weights"]
    for rec in scored:
        b = rec["breakdown"]
        assert 0 <= b["skill"] <= weights["skill"]
        assert 0 <= b["barrier"] <= weights["barrier"]
        assert 0 <= b["direction"] <= weights["direction"]
        assert 0 <= b["compensation"] <= weights["compensation"]


def test_tier_mapping_follows_thresholds(profile):
    t = profile["tiers"]
    assert tier_of(t["S"] + 5, profile) == "S"
    assert tier_of(t["S"], profile) == "S"
    assert tier_of(t["A"], profile) == "A"
    assert tier_of(t["B"], profile) == "B"
    assert tier_of(0, profile) == "C"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("12-16k · 13薪", 12),
        ("15-20k", 15),
        ("面议（同级约 15-25k）", 15),
        ("8-18k", 8),
        ("20000元", 20),
        ("面议", None),
        ("未公示", None),
        ("", None),
    ],
)
def test_salary_parser(text, expected):
    assert parse_salary_lower_k(text) == expected


def test_scorer_survives_minimal_record(profile):
    """采集层将来可能只给到很稀疏的字段，打分器不能因此崩掉。"""
    tiny = {"id": "x", "company": "某公司", "title": "Python 开发"}
    result = score_job(tiny, profile)
    assert 0 <= result["score_auto"] <= 100
    assert result["tier_auto"] in VALID_TIER


def test_unreachable_barrier_scores_low(profile):
    """6 年经验的硬门槛必须显著压分。"""
    good = {"id": "a", "title": "AI应用开发工程师", "exp_req": "经验不限", "edu_req": "本科"}
    bad = {"id": "b", "title": "AI应用开发工程师", "exp_req": "8年以上", "edu_req": "硕士"}
    assert score_job(bad, profile)["score_auto"] < score_job(good, profile)["score_auto"]


def test_manual_s_tier_lands_in_auto_top_tiers(seed, scored):
    """人工判定的 S 级岗位，算法也必须给到 S 或 A —— 保证初筛不把好岗位筛掉。"""
    manual_s = {r["id"] for r in seed if r["tier"] == "S"}
    auto_of = {r["id"]: r["tier_auto"] for r in scored}
    for jid in manual_s:
        assert auto_of[jid] in {"S", "A"}, f"{jid} 人工 S 级，算法只给了 {auto_of[jid]}"


def test_manual_c_tier_never_reaches_auto_s(seed, scored):
    """人工判定的 C 级（学历/年限硬不符）不得被算法捧成 S 级。"""
    manual_c = {r["id"] for r in seed if r["tier"] == "C"}
    auto_of = {r["id"]: r["tier_auto"] for r in scored}
    for jid in manual_c:
        assert auto_of[jid] != "S"


def test_auto_score_is_calibrated_against_manual(seed, scored):
    """算法分只做初筛，不必和人工分一致，但平均绝对偏差要控制在可接受范围。

    种子数据没有原始 JD，打分文本用的是人工提炼的 why_match 作代理，
    所以这里要求的是"大致同量级"，不是"完全吻合"。
    """
    manual = {r["id"]: r["score"] for r in seed}
    diffs = [abs(r["score_auto"] - manual[r["id"]]) for r in scored if r["id"] in manual]
    mean_abs = sum(diffs) / len(diffs)
    assert mean_abs <= 12, f"平均绝对偏差 {mean_abs:.1f} 过大，打分口径需要校准"


# ------------------------------------------------------------------ 3 构建层


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist") / "index.html"
    builder.build(demo=False, out=out)
    return out.read_text(encoding="utf-8")


def test_all_placeholders_are_replaced(built):
    for token in ("/*__STYLE__*/", "/*__DATA__*/", "/*__META__*/",
                  "/*__STATE__*/", "/*__STORE__*/", "/*__APP__*/",
                  "<!--__PWA_HEAD__-->"):
        assert token not in built, f"占位符 {token} 没有被替换"


def test_store_layer_is_inlined(built):
    """存储层必须真的进了产物 —— 没有它页面就只能看不能记。"""
    assert "jobpipe.state.v1" in built, "localStorage 键名不在产物里，store.js 没被内联"
    assert "setStatus" in built and "importState" in built


def test_output_has_no_external_resources(built):
    """产物不得依赖任何外部域的资源 —— 这是「双击即用、断网可用」的前提。

    为什么允许 <link rel="manifest"> 这类标签：它们是**同源相对路径**的 PWA 附属文件，
    属于可选增强（托管在 http/https 上才生效），file:// 下压根不会被请求，
    单文件离线能力不受影响。真正要禁的是指向外部域的东西 —— 那会让离线失效，
    还会把访问请求漏给第三方。

    PWA 与单文件不冲突：Service Worker 无法内联（浏览器强制要求同源独立文件），
    所以「HTML 自包含」与「可安装」通过相对路径引用并存。
    """
    low = built.lower()
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', low), "存在指向外部域的引用"
    assert not re.search(r'(?:src|href)\s*=\s*["\']//', low), "存在协议相对的外部引用"
    assert "<script src" not in low, "存在外部脚本引用"
    assert "@import" not in low, "存在 CSS @import"
    assert "fetch(" not in low, "存在运行时网络请求"
    assert "xmlhttprequest" not in low, "存在运行时网络请求"


def test_pwa_references_are_relative(built):
    """PWA 引用必须是相对路径。

    部署在 GitHub Pages 的子路径下（https://user.github.io/jobpipe/），
    以 / 开头的绝对路径会指向域名根而 404 —— 本地测不出来，上线才发现装不了。
    """
    hrefs = re.findall(r'<link[^>]+href="([^"]+)"', built)
    assert hrefs, "产物里没有任何 <link>，PWA head 没注入"
    for href in hrefs:
        assert not href.startswith("/"), f"发现绝对路径引用：{href}"
        assert href.startswith("./"), f"引用不是显式相对路径：{href}"


def test_inlined_data_is_valid_json(built):
    match = re.search(r"window\.__DATA__ = (\{.*?\n\});", built, re.S)
    assert match, "产物里找不到内联的 __DATA__"
    payload = json.loads(match.group(1))
    assert len(payload["applications"]) == 17
    for rec in payload["applications"]:
        assert "score_auto" in rec, "构建时没有跑打分"


def test_inlined_meta_is_valid_json(built):
    match = re.search(r"window\.__META__ = (\{.*?\n\});", built, re.S)
    assert match, "产物里找不到内联的 __META__"
    meta = json.loads(match.group(1))
    assert meta["total"] == 17
    assert set(meta["tiers"]) <= VALID_TIER
    assert meta["schema"] == 1


def test_normal_build_carries_no_personal_data(tmp_path):
    """公开产物的红线：既不能有演示状态，也不能有真实记录。"""
    out = tmp_path / "real.html"
    builder.build(demo=False, out=out)
    html = out.read_text(encoding="utf-8")

    meta = json.loads(re.search(r"window\.__META__ = (\{.*?\n\});", html, re.S).group(1))
    assert meta["demo"] is False
    assert meta["state_inlined"] is False, f"公开产物里内联了个人数据：{meta['state_source']}"
    assert re.search(r"window\.__STATE__ = null;", html), "空状态应当内联成 null"

    payload = json.loads(re.search(r"window\.__DATA__ = (\{.*?\n\});", html, re.S).group(1))
    statuses = {r["status"] for r in payload["applications"]}
    assert statuses == {"wishlist"}, f"正式产物里出现了非初始状态：{statuses}"


def test_demo_build_inlines_fictional_state(tmp_path):
    out = tmp_path / "demo.html"
    builder.build(demo=True, out=out)
    html = out.read_text(encoding="utf-8")

    meta = json.loads(re.search(r"window\.__META__ = (\{.*?\n\});", html, re.S).group(1))
    assert meta["demo"] is True
    assert meta["state_inlined"] is True

    state = json.loads(re.search(r"window\.__STATE__ = (\{.*?\n\});", html, re.S).group(1))
    assert state["patches"], "演示状态没有内联进去"
    assert state["schema"] == 1


def test_personal_build_refuses_publishable_filename(tmp_path):
    """内嵌真实数据的产物不允许叫 index.html —— 防止被误发布。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "schema": 1, "patches": {"liaoning-xinxi-ae": {"status": "applied"}},
        "added": [], "removed": [],
    }, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(SystemExit):
        builder.build(out=tmp_path / "index.html", state_path=state)


def test_personal_build_inlines_exported_state(tmp_path):
    """导出 → 构建 → 页面带着数据，这条回路必须通。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "schema": 1,
        "updated_at": "2026-09-21T19:00",
        "patches": {
            "liaoning-xinxi-ae": {"status": "applied", "applied_at": "2026-09-21",
                                  "notes": "本地测试备注"},
        },
        "added": [{"id": "manual-x", "company": "某创业公司", "title": "AI 工程师"}],
        "removed": ["migu-algorithm"],
    }, ensure_ascii=False), encoding="utf-8")

    out = builder.build(out=tmp_path / "local.html", state_path=state)
    html = out.read_text(encoding="utf-8")
    inlined = json.loads(re.search(r"window\.__STATE__ = (\{.*?\n\});", html, re.S).group(1))

    assert inlined["patches"]["liaoning-xinxi-ae"]["notes"] == "本地测试备注"
    assert inlined["added"][0]["company"] == "某创业公司"
    assert inlined["removed"] == ["migu-algorithm"]


def test_missing_placeholder_raises(profile):
    with pytest.raises(SystemExit):
        builder.inject("<html></html>", "/*__DATA__*/", "{}")


# ------------------------------------------------------------------ 4 状态层


def test_normalize_state_handles_empty_input():
    empty = {"schema": 1, "updated_at": None, "patches": {}, "added": [], "removed": []}
    assert builder.normalize_state(None) == empty
    assert builder.normalize_state({}) == empty
    assert builder.normalize_state("垃圾数据") == empty


def test_normalize_state_drops_malformed_entries():
    """外部导入的 JSON 不可信，坏字段要丢弃而不是让页面崩掉。"""
    s = builder.normalize_state({
        "patches": {"ok": {"status": "applied"}, "bad": "不是对象", "": {"x": 1}},
        "added": [{"id": "x", "company": "有 id"}, {"company": "没有 id"}, "字符串"],
        "removed": ["z", 5, None],
    })
    assert set(s["patches"]) == {"ok"}
    assert [j["id"] for j in s["added"]] == ["x"]
    assert s["removed"] == ["z"]


def test_demo_state_only_references_real_jobs(seed):
    """演示状态里的岗位 id 必须都真实存在，否则演示会静默少几条。"""
    ids = {r["id"] for r in seed}
    demo = builder.normalize_state(load_json(ROOT / "data" / "demo-state.json"))
    for jid in demo["patches"]:
        assert jid in ids, f"演示状态引用了不存在的岗位：{jid}"
    for jid, p in demo["patches"].items():
        assert p["status"] in VALID_STATUS, f"{jid} 演示状态非法：{p['status']}"


def test_makefile_like_script_targets_exist():
    """文档里让人跑的脚本必须真的存在，不然 README 就是骗人的。"""
    for rel in ("build.py", "src/score.py", "src/store.js", "tools/screenshot.py",
                "tools/e2e.py", "data/seed-2026-09-17.json", "data/demo-state.json"):
        assert (ROOT / rel).exists(), f"缺少 {rel}"
