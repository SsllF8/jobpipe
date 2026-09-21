#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""种子文件的定位规则 —— 采集层与消费层共用的唯一约定。

采集脚本（``tools/ingest_jd.py``）每天产出 ``data/seed-YYYY-MM-DD.json``；
构建（``build.py``）与自荐信生成（``tools/gen_letter.py``）一律消费**最新一份**。

为什么按「文件名」而不是 json 里的 ``updated_at`` 排序：
- 文件名是零填充日期，字典序天然等于时间序，不必解析内容即可定位；
- 采集脚本中途失败可能写出不完整文件，此时不该拿内容时间戳去猜谁新谁旧；
- 少一次 IO，且 build.py 会在 CI 里跑，行为必须完全确定。

显式传入的 ``--seed``（无论 build 还是 gen_letter）优先级永远高于自动发现。
"""

from __future__ import annotations

from pathlib import Path

# 一份 seed 都没有时的兜底：仓库里随代码提交的初始岗位池
FALLBACK = "seed-2026-09-17.json"

PREFIX = "seed"


def latest_seed(data_dir: Path) -> Path:
    """返回 data_dir 下最新的 ``seed-*.json``；一份都没有时退回内置种子。"""
    directory = Path(data_dir)
    files = sorted(directory.glob(f"{PREFIX}-*.json"))
    return files[-1] if files else directory / FALLBACK


def seed_date(path: Path) -> str:
    """从 ``seed-YYYY-MM-DD.json`` 里取出日期串；命名不符时返回空串。"""
    stem = Path(path).stem                      # seed-2026-09-17
    head, _, tail = stem.partition("-")
    return tail if head == PREFIX else ""
