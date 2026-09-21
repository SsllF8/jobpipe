#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集链路的测试：种子定位 / 检索清单 / JD 录入。

这三块是 M5 新增的「采集层」，全部是**纯规则**（零 AI 调用），
所以能像打分引擎一样被完整覆盖。

跑法：
    python -m pytest tests -q
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT))

import ingest_jd as ij  # noqa: E402
import search_plan as sp  # noqa: E402
from seed_store import latest_seed, seed_date  # noqa: E402

PROFILE = sp.load_json(ROOT / "src" / "profile.json")
CHANNELS = sp.load_json(ROOT / "tools" / "channels.json")["channels"]

SAMPLE_JD = """某科技有限公司
AI应用开发工程师（大模型方向）
15-25k
上海 · 浦东新区
1-3年经验 / 本科
https://www.liepin.com/job/1234567890

岗位职责：
1. 基于 LangChain / LlamaIndex 搭建 RAG 知识库问答系统，负责文档切片、向量检索与召回优化；
2. 使用 Chroma / Milvus 等向量数据库，结合 Python 与 FastAPI 提供接口服务；
3. 参与 Multi-Agent 编排与工作流设计，实践 Prompt 工程与 Function Calling。
"""


def run_cli(script: Path, *args: str, cwd: Path = ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(script), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=str(cwd),
    )


# ------------------------------------------------------------------ 1 种子定位


def test_latest_seed_picks_newest(tmp_path):
    for name in ("seed-2026-09-17.json", "seed-2026-09-21.json", "seed-2026-09-19.json"):
        (tmp_path / name).write_text("{}", encoding="utf-8")
    assert latest_seed(tmp_path).name == "seed-2026-09-21.json"


def test_latest_seed_falls_back_when_empty(tmp_path):
    """一份 seed 都没有时退回仓库自带的那份，不能抛异常。"""
    assert latest_seed(tmp_path).name == "seed-2026-09-17.json"


def test_latest_seed_ignores_unrelated_files(tmp_path):
    (tmp_path / "letters.json").write_text("{}", encoding="utf-8")
    (tmp_path / "seed-2026-09-18.json").write_text("{}", encoding="utf-8")
    assert latest_seed(tmp_path).name == "seed-2026-09-18.json"


def test_seed_date_parses_and_rejects():
    assert seed_date(Path("data/seed-2026-09-17.json")) == "2026-09-17"
    assert seed_date(Path("data/letters.json")) == ""
    assert seed_date(Path("data/applications.json")) == ""


# ------------------------------------------------------------------ 2 检索清单


def test_profile_and_channels_load():
    assert PROFILE["city"] == "上海"
    assert PROFILE["target_titles"], "画像里没有目标岗位名，清单会空"
    assert len(CHANNELS) >= 20, f"渠道只有 {len(CHANNELS)} 个，达不到「20+ 渠道」的产品承诺"


def test_every_channel_has_name_and_category():
    for channel in CHANNELS:
        assert channel.get("name"), "渠道缺名字"
        assert channel.get("category"), f"{channel.get('name')} 缺类别"


def test_build_keywords_dedup_and_order():
    words = sp.build_keywords(PROFILE)
    assert words == list(dict.fromkeys(words)), "关键词出现重复"
    assert "AI应用开发工程师" in words
    assert len(words) >= 4


def test_search_query_uses_site_operator():
    channel = {"name": "猎聘", "domain": "liepin.com"}
    query = sp.search_query(channel, "AI应用开发工程师", "上海")
    assert query == "site:liepin.com 上海 AI应用开发工程师"


def test_search_query_without_domain_falls_back_to_name():
    """没固定域名的渠道（如张江人才、目标公司官网）不能生成 site: 空查询。"""
    channel = {"name": "张江人才", "domain": ""}
    query = sp.search_query(channel, "AI应用开发工程师", "上海")
    assert query.startswith("张江人才 上海")
    assert "site:" not in query


def test_engine_url_encodes_query():
    url = sp.engine_url("site:a.com 上海 AI应用开发", "bing")
    assert url.startswith("https://www.bing.com/search?q=")
    assert " " not in url, "查询串没被编码，浏览器会截断"
    assert "%20" in url


def test_engine_url_rejects_unknown_engine():
    with pytest.raises(ValueError):
        sp.engine_url("x", "yandex")


def test_site_search_url_handles_missing_and_template():
    assert sp.site_search_url({"site_search": ""}, "AI") is None
    url = sp.site_search_url({"site_search": "https://x.com/s?q={q}"}, "AI 应用")
    assert url == "https://x.com/s?q=AI%20%E5%BA%94%E7%94%A8"


def test_channel_links_one_search_per_keyword():
    channel = {"name": "猎聘", "domain": "liepin.com", "site_search": ""}
    keywords = ["A", "B", "C"]
    links = sp.channel_links(channel, keywords, "上海")
    search = [link for link in links if link["kind"] == "search"]
    assert len(search) == len(keywords)
    assert not [link for link in links if link["kind"] == "direct"]


def test_channel_links_adds_at_most_one_direct():
    channel = {
        "name": "猎聘",
        "domain": "liepin.com",
        "site_search": "https://www.liepin.com/zhaopin/?key={q}",
    }
    links = sp.channel_links(channel, ["A", "B", "C"], "上海")
    assert len([link for link in links if link["kind"] == "direct"]) == 1


def test_build_plan_shape():
    plan = sp.build_plan(PROFILE, CHANNELS, "bing", "上海")
    assert plan["city"] == "上海"
    assert len(plan["groups"]) == len(CHANNELS)
    counts = sp.count_links(plan)
    assert counts["channels"] == len(CHANNELS)
    assert counts["search"] == len(CHANNELS) * len(plan["keywords"])


def test_html_renders_every_channel_and_no_external_asset():
    plan = sp.build_plan(PROFILE, CHANNELS, "bing", "上海")
    page = sp.render_html(plan, "2026-09-21")
    for channel in CHANNELS:
        assert channel["name"] in page, f"{channel['name']} 没出现在清单里"
    low = page.lower()
    assert "<script src" not in low
    assert "@import" not in low
    assert not [m for m in ("cdn.", "unpkg.com", "jsdelivr") if m in low], "清单页引入了外部 CDN"
    assert "fetch(" not in low, "清单页不该有运行时请求"


def test_html_escapes_channel_fields():
    plan = {
        "city": "上海",
        "engine": "bing",
        "keywords": ["AI"],
        "groups": [
            {
                "name": 'X"><script>alert(1)</script>',
                "category": "测试",
                "note": "备注",
                "domain": "",
                "links": [{"keyword": "AI", "kind": "search", "url": "https://x.com/?q=1"}],
            }
        ],
    }
    page = sp.render_html(plan, "2026-09-21")
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_markdown_has_checkboxes():
    plan = sp.build_plan(PROFILE, CHANNELS, "bing", "上海")
    md = sp.render_markdown(plan, "2026-09-21")
    assert md.count("- [ ] ") >= len(CHANNELS)


# ------------------------------------------------------------------ 3 JD 解析


def test_split_records_by_separator():
    chunks = ij.split_records("岗位一\n---\n岗位二\n=====\n岗位三")
    assert chunks == ["岗位一", "岗位二", "岗位三"]


def test_split_records_single():
    assert ij.split_records("只有一个岗位") == ["只有一个岗位"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("薪资 15-25k", "15-25k"),
        ("12k-18K", "12-18k"),
        ("月薪 20K 起", "20k"),
        ("薪资面议", ""),
    ],
)
def test_extract_salary(text, expected):
    assert ij.extract_salary(text) == expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("要求 1-3年经验", "1-3年"),
        ("应届生可投", "应届可投"),
        ("经验不限", "经验不限"),
        ("5年以上", "5年以上"),
        ("没有提经验", ""),
    ],
)
def test_extract_exp(text, expected):
    assert ij.extract_exp(text) == expected


def test_extract_edu():
    assert ij.extract_edu("本科及以上学历") == "本科"
    assert ij.extract_edu("大专以上") == "大专"
    assert ij.extract_edu("硕士优先") == "硕士"
    # EDU_WORDS 有序，"学历不限"排最前 —— 含它就该返回它
    assert ij.extract_edu("学历不限，本科优先") == "学历不限"
    assert ij.extract_edu("无要求") == ""


def test_extract_area_uses_districts():
    districts = PROFILE["districts"]
    assert ij.extract_area("上海 · 浦东新区", districts) == "上海 · 浦东"
    assert ij.extract_area("工作地点在上海", districts) == "上海"
    assert ij.extract_area("北京朝阳", districts) == ""


def test_extract_company_keeps_full_name():
    """回归：非贪婪量词曾让「…科技有限公司」被截成「…科技」。"""
    assert ij.extract_company("上海某某智能科技有限公司 招聘") == "上海某某智能科技有限公司"
    assert ij.extract_company("某某科技 在招人") == "某某科技"
    assert ij.extract_company("一段没有公司名的文本") == ""


def test_extract_url_strips_punctuation():
    text = "详情见 https://www.liepin.com/job/123，谢谢"
    assert ij.extract_url(text) == "https://www.liepin.com/job/123"


def test_parse_jd_full_sample():
    job = ij.parse_jd(SAMPLE_JD, PROFILE, {"channel": "猎聘"})
    assert job["company"] == "某科技有限公司"
    assert "AI应用开发工程师" in job["title"]
    assert job["salary"] == "15-25k"
    assert job["area"] == "上海 · 浦东"
    assert job["exp_req"] == "1-3年"
    assert job["edu_req"] == "本科"
    assert job["url"] == "https://www.liepin.com/job/1234567890"
    assert job["status"] == "wishlist"
    assert job["id"].startswith("auto-")
    assert job["jd_text"].strip()


def test_parse_jd_overrides_beat_parsing():
    job = ij.parse_jd(SAMPLE_JD, PROFILE, {
        "company": "手工指定的公司",
        "title": "手工指定的岗位",
        "salary": "99k",
        "url": "https://example.com/x",
    })
    assert job["company"] == "手工指定的公司"
    assert job["title"] == "手工指定的岗位"
    assert job["salary"] == "99k"
    assert job["url"] == "https://example.com/x"


def test_parse_jd_leaves_unknown_fields_empty():
    """认不出来就留空 —— 猜错会污染后面积累的所有投递记录。"""
    job = ij.parse_jd("一段没有任何结构化信息的文本", PROFILE, {})
    assert job["salary"] == ""
    assert job["url"] == ""
    assert job["area"] == ""


def test_make_id_is_stable_and_distinct():
    assert ij.make_id("A公司", "B岗位") == ij.make_id("A公司", "B岗位")
    assert ij.make_id("A公司", "B岗位") != ij.make_id("A公司", "C岗位")


def test_find_duplicates_by_url():
    old = [{"id": "x", "url": "https://a.com/1", "company": "A公司", "title": "AI工程师"}]
    job = {"id": "auto-1", "url": "https://a.com/1", "company": "别的", "title": "别的"}
    assert ij.find_duplicates(job, old)


def test_find_duplicates_by_company_title():
    old = [{"id": "x", "url": "", "company": "A公司", "title": "AI工程师"}]
    job = {"id": "auto-2", "url": "https://new.com", "company": "A公司", "title": "AI工程师"}
    assert ij.find_duplicates(job, old)


def test_find_duplicates_returns_none_for_new():
    old = [{"id": "x", "url": "https://a.com/1", "company": "A公司", "title": "AI工程师"}]
    job = {"id": "auto-3", "url": "https://b.com/9", "company": "B公司", "title": "大模型工程师"}
    assert ij.find_duplicates(job, old) is None


def test_explain_reads_real_score_fields():
    """防止字段名漂移：explain 必须真的从 score.breakdown 里取到东西。"""
    from score import score_job

    job = ij.parse_jd(SAMPLE_JD, PROFILE, {"channel": "猎聘"})
    scored = score_job(job, PROFILE)
    ij.explain(job, scored, PROFILE)

    assert job["why_match"], "why_match 是空的 —— 打分明细字段名对不上了"
    joined = " ".join(job["why_match"])
    assert "命中核心技能" in joined
    assert any(word in joined for word in ("langchain", "rag", "python", "chroma", "fastapi"))
    assert scored["tier_auto"] in {"S", "A", "B", "C"}
    assert job["advice"]


def test_explain_flags_missing_fields():
    from score import score_job

    job = ij.parse_jd("只有一句话的文本", PROFILE, {})
    scored = score_job(job, PROFILE)
    ij.explain(job, scored, PROFILE)
    assert any("字段待补" in gap for gap in job["gaps"])


def test_channel_type_comes_from_channels_json():
    table = ij.load_channels()
    assert table["猎聘"]["category"] == "综合平台"
    assert table["V2EX 酷工作"]["category"] == "AI 技术垂直"
    assert len(table) >= 20


def test_merge_into_seed_appends(tmp_path):
    seed_path = tmp_path / "seed-2026-09-17.json"
    seed_path.write_text(
        json.dumps({"version": 1, "applications": [{"id": "old"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    out = tmp_path / "seed-2026-09-21.json"
    payload = ij.merge_into_seed([{"id": "new1"}, {"id": "new2"}], out, seed_path)
    assert [r["id"] for r in payload["applications"]] == ["old", "new1", "new2"]
    assert json.loads(out.read_text(encoding="utf-8"))["applications"][-1]["id"] == "new2"
    assert payload["updated_at"]


def test_detail_renders_inlined_jd(tmp_path):
    """详情面板必须渲染 jd_text —— 采集进来的 JD 要能回看。

    这里只做静态断言（产物里存在渲染分支与样式），真浏览器的点击验证放在
    tools/e2e.py —— pytest 套件保持零浏览器依赖，CI 才跑得动（CI 只装 pytest）。
    """
    import build as builder
    from score import score_job

    job = ij.parse_jd(SAMPLE_JD, PROFILE, {"channel": "猎聘"})
    ij.explain(job, score_job(job, PROFILE), PROFILE)
    seed_path = tmp_path / "seed-2026-09-21.json"
    ij.merge_into_seed([job], seed_path, ROOT / "data" / "seed-2026-09-17.json")
    out = tmp_path / "board.html"
    builder.build(seed_path=seed_path, out=out)
    html = out.read_text(encoding="utf-8")

    assert 'details class="jd"' in html, "详情面板没有渲染原始 JD"
    assert "原始 JD" in html
    assert "details.jd" in html, "缺 .jd 样式，折叠区在深色页面上会很难看"


def test_detail_shows_inlined_jd_in_browser(tmp_path):
    """真浏览器验证：点开岗位 → 展开「原始 JD」→ 看得到录入的原文。

    CI 只装 pytest，所以这里 importorskip 优雅跳过；本地跑则有完整回归保护。
    """
    pytest.importorskip("playwright.sync_api", reason="需要本地 Playwright")
    from playwright.sync_api import sync_playwright

    import build as builder
    from score import score_job

    job = ij.parse_jd(SAMPLE_JD, PROFILE, {"channel": "猎聘"})
    ij.explain(job, score_job(job, PROFILE), PROFILE)
    seed_path = tmp_path / "seed-2026-09-21.json"
    ij.merge_into_seed([job], seed_path, ROOT / "data" / "seed-2026-09-17.json")
    out = tmp_path / "board.html"
    builder.build(seed_path=seed_path, out=out)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 390, "height": 844})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(out.resolve().as_uri())
        page.wait_for_timeout(500)
        page.click("text=岗位", timeout=8000)
        page.wait_for_timeout(300)
        page.locator("article.card", has_text="AI应用开发工程师").first.click()
        page.wait_for_timeout(400)
        assert "原始 JD" in page.inner_text("body"), "详情里没有「原始 JD」区"

        page.locator("details.jd summary").first.click()
        page.wait_for_timeout(250)
        text = page.inner_text("body")
        browser.close()

    assert "LangChain" in text, "展开折叠区后看不到 JD 原文"
    assert not errors, f"页面报错：{errors}"


# ------------------------------------------------------------------ 4 CLI 端到端


def test_cli_search_plan_dry_run():
    result = run_cli(ROOT / "tools" / "search_plan.py", "--dry-run")
    assert result.returncode == 0, result.stderr
    assert f"渠道 {len(CHANNELS)} 个" in result.stdout


def test_cli_search_plan_writes_both_products(tmp_path):
    html = tmp_path / "plan.html"
    md = tmp_path / "plan.md"
    result = run_cli(
        ROOT / "tools" / "search_plan.py",
        "--date", "2026-09-21",
        "--out-html", str(html),
        "--out-md", str(md),
    )
    assert result.returncode == 0, result.stderr
    assert html.exists() and md.exists()
    assert "岗位检索清单" in html.read_text(encoding="utf-8")


def test_cli_ingest_writes_seed(tmp_path):
    jd = tmp_path / "jd.txt"
    jd.write_text(SAMPLE_JD, encoding="utf-8")
    out = tmp_path / "seed-2026-09-21.json"
    result = run_cli(
        ROOT / "tools" / "ingest_jd.py",
        "--file", str(jd),
        "--channel", "猎聘",
        "--seed", str(ROOT / "data" / "seed-2026-09-17.json"),
        "--out", str(out),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert len(payload["applications"]) == 18
    added = payload["applications"][-1]
    assert added["channel"] == "猎聘"
    assert added["channel_type"] == "综合平台"
    assert added["why_match"], "录入的岗位没有任何匹配理由"
    assert "新增 1 条" in result.stdout


def test_cli_ingest_skips_duplicate(tmp_path):
    jd = tmp_path / "jd.txt"
    jd.write_text(SAMPLE_JD, encoding="utf-8")
    first = tmp_path / "seed-2026-09-21.json"
    base = ["--file", str(jd), "--channel", "猎聘", "--out", str(first)]
    run_cli(ROOT / "tools" / "ingest_jd.py", *base,
            "--seed", str(ROOT / "data" / "seed-2026-09-17.json"))
    second = run_cli(ROOT / "tools" / "ingest_jd.py", *base, "--seed", str(first))
    assert second.returncode == 0, second.stderr
    assert "跳过 1 条重复" in second.stdout
    payload = json.loads(first.read_text(encoding="utf-8"))
    assert len(payload["applications"]) == 18, "重复录入把岗位池写重复了"


def test_cli_ingest_dry_run_does_not_write(tmp_path):
    jd = tmp_path / "jd.txt"
    jd.write_text(SAMPLE_JD, encoding="utf-8")
    out = tmp_path / "should-not-exist.json"
    result = run_cli(
        ROOT / "tools" / "ingest_jd.py",
        "--file", str(jd), "--out", str(out), "--dry-run",
        "--seed", str(ROOT / "data" / "seed-2026-09-17.json"),
    )
    assert result.returncode == 0, result.stderr
    assert not out.exists(), "--dry-run 竟然写了文件"
    assert "--dry-run" in result.stdout


def test_cli_ingest_requires_source():
    result = run_cli(ROOT / "tools" / "ingest_jd.py")
    assert result.returncode != 0
    assert "--text" in result.stderr or "--text" in result.stdout


def test_cli_ingest_list_channels():
    result = run_cli(ROOT / "tools" / "ingest_jd.py", "--list-channels")
    assert result.returncode == 0, result.stderr
    assert "猎聘" in result.stdout
    assert "综合平台" in result.stdout
