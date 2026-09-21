#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自荐信生成：简历 + 岗位信息 → DeepSeek → ``data/letters.json``

为什么单独存 letters.json，而不是写回 seed-*.json
--------------------------------------------------
``seed`` 是**采集层**，每天被采集脚本整体重写。自荐信是**生成产物**，
写回 seed 会在下一次采集时被冲掉。所以数据分成三层：

    seed       data/seed-*.json     采集层，每天覆盖          公开
    letters    data/letters.json    生成层，按 job_id 累积     公开（无隐私）
    state      localStorage         用户层，投递进度与备注     私有

``build.py`` 在构建时把 letters 合并进 ``applications[].cover_letter``。

成本控制（"少用 AI"的落地）
---------------------------
1. 只对 **S 级**岗位生成（``--tier`` 可改，``--all`` 可放开）；
2. **已生成的一律跳过**，除非显式 ``--force``；
3. 每生成一封**立即落盘**，中途失败不会丢掉已完成的；
4. ``--dry-run`` 只打印提示词与预估消耗，一次 API 都不调。

用法
----
    python tools/gen_letter.py --list              # 看哪些还没生成
    python tools/gen_letter.py --dry-run           # 只预览提示词
    python tools/gen_letter.py                     # 给所有缺信的 S 级岗生成
    python tools/gen_letter.py --job liaoning-xinxi-ae --force   # 重做某一个
    python tools/gen_letter.py --show bailian-ai   # 打印已生成的自荐信
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # 04-job-board-app/
PROJECT = ROOT.parent                                   # job-hunting-2026/
DATA = ROOT / "data"
ENV_FILE = ROOT / ".env"
LETTERS_FILE = DATA / "letters.json"
PROMPT_FILE = ROOT / "tools" / "prompts" / "cover_letter.md"
SEED_FILE = DATA / "seed-2026-09-17.json"

RESUMES = {
    "A": PROJECT / "01-resume" / "src" / "刘展博-AI应用开发-A版-20260917.md",
    "B": PROJECT / "01-resume" / "src" / "刘展博-AI应用开发-B版-20260917.md",
}

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE = "https://api.deepseek.com"
TIMEOUT = 120

LETTERS_SCHEMA = 1


# ---------------------------------------------------------------- 基础工具


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_json(path: Path):
    return json.loads(read_text(path))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_env(path: Path) -> dict[str, str]:
    """极简 .env 解析：不引 dotenv，支持 KEY=VALUE 与 # 注释。"""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in read_text(path).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        # 去掉可能存在的引号
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        env[key.strip()] = value
    return env


# ---------------------------------------------------------------- 提示词


def bulletize(items) -> str:
    if not items:
        return "（无）"
    if isinstance(items, str):
        return items.strip() or "（无）"
    return "\n".join(f"{i + 1}. {str(x).strip()}" for i, x in enumerate(items))


def build_prompt(template: str, job: dict, resume_text: str) -> str:
    mapping = {
        "company": job.get("company") or "（未注明）",
        "title": job.get("title") or "",
        "salary": job.get("salary") or "（未注明）",
        "exp_req": job.get("exp_req") or "（未注明）",
        "edu_req": job.get("edu_req") or "（未注明）",
        "area": job.get("area") or "（未注明）",
        "channel": job.get("channel") or "（未注明）",
        "why_match": bulletize(job.get("why_match")),
        "gaps": bulletize(job.get("gaps")),
        "resume": resume_text.strip(),
    }
    out = template
    for key, value in mapping.items():
        out = out.replace("{{" + key + "}}", str(value))
    left = re.findall(r"\{\{(\w+)\}\}", out)
    if left:
        raise SystemExit(f"提示词模板里有未填充的占位符：{sorted(set(left))}")
    return out


# ---------------------------------------------------------------- API


class ApiError(RuntimeError):
    pass


def call_deepseek(api_key: str, base_url: str, model: str, prompt: str) -> tuple[str, dict]:
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.6,   # 偏低：自荐信要守格式规则，不要发散
        "stream": False,
    }
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    hints = {
        400: "请求格式有误（多半是模型名不对）",
        401: "key 无效或已失效 —— 检查 .env 里的 DEEPSEEK_API_KEY",
        402: "账户余额不足",
        422: "参数不被接受",
        429: "触发限流，稍后再试",
        500: "服务端错误，稍后重试",
        503: "服务暂时不可用，稍后重试",
    }
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = hints.get(exc.code, "未知错误")
        raise ApiError(f"HTTP {exc.code} · {hint}\n{detail}") from None
    except urllib.error.URLError as exc:
        raise ApiError(f"网络不可达：{exc.reason}") from None

    try:
        text = body["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError):
        raise ApiError(f"返回结构异常：{json.dumps(body, ensure_ascii=False)[:300]}") from None

    if not text:
        raise ApiError("模型返回了空文本")
    return text, body.get("usage") or {}


COMPRESS_PROMPT = """下面这封中文求职自荐信写得太长了（{n} 字），请压缩到 {target} 字以内。

压缩要求：

1. 保留全部具体证据：项目名称、技术名称、算法名、数字，一个都不能删。
2. 保留联系方式、作品集链接，保留"可随时到岗面聊"的收尾（两句话以内）。
3. 缺口仍然只保留一句，不要新增事实，也不要删掉已有的诚实说明。
4. 删掉重复表述、铺垫词、形容词堆砌。
5. 仍然是纯文本，段落之间空一行；不要疑问句；不要出现「贵司」「贵公司」；不要 Markdown 标记。
6. 输出前自己数一遍字数，确认在 {target} 字以内再输出。

原文：

---
{text}
---

只输出压缩后的自荐信正文，不要任何说明。"""


def merge_usage(*usages: dict) -> dict:
    """把多次调用的 token 用量累加起来，便于统计真实成本。"""
    total: dict[str, int] = {}
    for usage in usages:
        for key, value in (usage or {}).items():
            if isinstance(value, int):
                total[key] = total.get(key, 0) + value
    return total


def compress_letter(api_key: str, base_url: str, model: str, text: str,
                    target: int) -> tuple[str, dict]:
    """让模型把超长自荐信压到目标字数内。

    提示词里的长度约束模型执行得并不可靠（实测普遍超出 20%~35%），
    所以在程序侧做一次确定性兜底：超长就再要求压一轮。
    """
    prompt = COMPRESS_PROMPT.replace("{n}", str(len(text))).replace("{target}", str(target))
    prompt = prompt.replace("{text}", text)
    return call_deepseek(api_key, base_url, model, prompt)



# ---------------------------------------------------------------- 目标筛选


def tier_of(job: dict) -> str:
    """展示分档：人工分优先（与看板口径一致）。"""
    return job.get("tier") or job.get("tier_auto") or "C"


def pick_targets(records: list[dict], existing: dict, jobs: list[str] | None,
                 tier: str, take_all: bool, force: bool) -> list[dict]:
    if jobs:
        wanted = set(jobs)
        found = [j for j in records if j.get("id") in wanted]
        missing = wanted - {j.get("id") for j in found}
        if missing:
            raise SystemExit(f"找不到这些 job id：{sorted(missing)}")
        targets = found
    elif take_all:
        targets = list(records)
    else:
        targets = [j for j in records if tier_of(j) == tier]

    if not force:
        targets = [j for j in targets if not (existing.get(j["id"], {}).get("text") or "").strip()]
    return sorted(targets, key=lambda j: -(float(j.get("score") or 0)))


# ---------------------------------------------------------------- CLI 子命令


def cmd_list(records: list[dict], letters: dict, tier: str) -> None:
    rows = [j for j in records if tier_of(j) == tier]
    rows.sort(key=lambda j: -(float(j.get("score") or 0)))
    done = sum(1 for j in rows if (letters.get(j["id"], {}).get("text") or "").strip())

    print(f"{tier} 级岗位自荐信状态（{done}/{len(rows)} 已生成）\n")
    print(f"{'状态':<6}{'分':>5}  {'岗位':<44}{'生成时间':<18}")
    print("-" * 78)
    for job in rows:
        rec = letters.get(job["id"], {})
        ok = bool((rec.get("text") or "").strip())
        title = f"{job.get('company', '')[:16]} · {job.get('title', '')}"[:42]
        print(f"{'✅ 有' if ok else '· 缺':<6}{float(job.get('score') or 0):>5.0f}  {title:<44}{rec.get('generated_at', '—')[:16]:<18}")
    if done < len(rows):
        print(f"\n还有 {len(rows) - done} 封没生成 → python tools/gen_letter.py")
    else:
        print("\n全部已生成。")


def cmd_show(letters: dict, job_id: str) -> None:
    rec = letters.get(job_id)
    if not rec:
        raise SystemExit(f"letters.json 里没有 {job_id}")
    print(f"=== {job_id}（{rec.get('company', '')} / {rec.get('title', '')}）===")
    print(f"model {rec.get('model')} ｜ 生成于 {rec.get('generated_at')} ｜ 字数 {len(rec.get('text', ''))}\n")
    print(rec["text"])


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(description="生成 S 级岗位自荐信（DeepSeek）")
    parser.add_argument("--seed", default=str(SEED_FILE), help="岗位数据，默认 seed-2026-09-17.json")
    parser.add_argument("--letters", default=str(LETTERS_FILE), help="自荐信缓存文件")
    parser.add_argument("--job", action="append", help="只处理指定 id，可重复传")
    parser.add_argument("--tier", default="S", help="自动挑选的分档，默认 S")
    parser.add_argument("--all", action="store_true", help="不按分档筛选，处理全部岗位")
    parser.add_argument("--force", action="store_true", help="覆盖已有自荐信")
    parser.add_argument("--resume", choices=["A", "B"], default="A", help="用哪版简历做素材，默认 A")
    parser.add_argument("--model", default=None, help="覆盖 .env 里的模型名")
    parser.add_argument("--max-chars", type=int, default=560,
                        help="正文超过此字数就自动再压缩一轮，默认 560；传 0 关闭")
    parser.add_argument("--target-chars", type=int, default=500,
                        help="压缩的目标字数，默认 500")
    parser.add_argument("--dry-run", action="store_true", help="只预览提示词，不调用 API")
    parser.add_argument("--list", action="store_true", help="只列出各岗位自荐信状态")
    parser.add_argument("--show", metavar="JOB_ID", help="打印某个岗位已生成的自荐信")
    args = parser.parse_args()

    payload = load_json(Path(args.seed))
    records = payload["applications"] if isinstance(payload, dict) else payload
    letters = load_json(Path(args.letters)) if Path(args.letters).exists() else {
        "version": LETTERS_SCHEMA, "updated_at": None, "model": None, "letters": {}
    }
    store = letters.setdefault("letters", {})

    if args.list:
        cmd_list(records, store, args.tier)
        return
    if args.show:
        cmd_show(store, args.show)
        return

    targets = pick_targets(records, store, args.job, args.tier, args.all, args.force)
    if not targets:
        print("没有需要生成的自荐信（都已完成，或用 --force 覆盖）。")
        return

    env = read_env(ENV_FILE)
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    base_url = env.get("DEEPSEEK_BASE_URL", DEFAULT_BASE)
    model = args.model or env.get("DEEPSEEK_MODEL", DEFAULT_MODEL)

    resume_path = RESUMES[args.resume]
    if not resume_path.exists():
        raise SystemExit(f"找不到简历素材：{resume_path}")
    resume_text = read_text(resume_path)

    template = read_text(PROMPT_FILE)

    print(f"准备生成 {len(targets)} 封自荐信 ｜ 模型 {model} ｜ 简历 {args.resume} 版 ｜ 提示词 {len(template)} 字\n")

    if args.dry_run:
        first = targets[0]
        prompt = build_prompt(template, first, resume_text)
        print("=" * 70)
        print(f"【示例】{first.get('company')} · {first.get('title')}")
        print("=" * 70)
        print(prompt)
        print("=" * 70)
        est_in = (len(prompt) + len(resume_text)) // 2      # 中文粗估：1 字 ≈ 1 token（保守）
        print(f"\n单封提示词约 {len(prompt)} 字。--dry-run 未调用 API，未产生任何费用。")
        print(f"待生成清单：{[j['id'] for j in targets]}")
        return

    if not api_key:
        raise SystemExit(
            "没有读到 DEEPSEEK_API_KEY。\n"
            f"请在 {ENV_FILE} 里写一行：DEEPSEEK_API_KEY=sk-xxxx\n"
            "（该文件已在 .gitignore 中，不会进 Git）"
        )

    ok, failed = 0, []
    for i, job in enumerate(targets, 1):
        tag = f"[{i}/{len(targets)}] {job.get('company', '')[:14]} · {job.get('title', '')[:24]}"
        print(f"{tag} 生成中…", end=" ", flush=True)

        compressed = False
        note = ""
        try:
            prompt = build_prompt(template, job, resume_text)
            text, usage = call_deepseek(api_key, base_url, model, prompt)

            # 长度兜底：模型写超是常态，超阈值就再要一轮压缩
            if args.max_chars and len(text) > args.max_chars:
                try:
                    shorter, usage2 = compress_letter(
                        api_key, base_url, model, text, args.target_chars)
                    if 0 < len(shorter) < len(text):
                        note = f"，{len(text)}→{len(shorter)} 字已压缩"
                        text = shorter
                        compressed = True
                    usage = merge_usage(usage, usage2)
                except ApiError as exc:
                    note = f"，压缩失败保留原稿（{str(exc).splitlines()[0]}）"
        except ApiError as exc:
            print("失败")
            failed.append((job["id"], str(exc)))
            print(f"    {exc}")
            continue

        store[job["id"]] = {
            "text": text,
            "company": job.get("company", ""),
            "title": job.get("title", ""),
            "model": model,
            "resume": args.resume,
            "generated_at": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "chars": len(text),
            "compressed": compressed,
            "usage": {
                "prompt_tokens": usage.get("prompt_tokens"),
                "completion_tokens": usage.get("completion_tokens"),
            },
        }
        letters["updated_at"] = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        letters["model"] = model
        write_json(Path(args.letters), letters)      # 每封立即落盘
        ok += 1
        print(f"完成（{len(text)} 字{note}）")

    print(f"\n成功 {ok} 封 ｜ 失败 {len(failed)} 封 ｜ 缓存 → {args.letters}")
    for job_id, err in failed:
        print(f"  ✗ {job_id}: {err.splitlines()[0]}")
    if ok:
        print("\n下一步：python build.py --demo 重新构建看板，自荐信会出现在「自荐信」页与岗位详情里。")


if __name__ == "__main__":
    main()
