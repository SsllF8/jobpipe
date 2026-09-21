#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""测试全局夹具：把「默认种子」钉在仓库自带的那份上。

为什么需要这一步
----------------
岗位池是**采集层** —— ``tools/ingest_jd.py`` 每天会往 ``data/`` 写一份新的
``seed-YYYY-MM-DD.json``，``build.py`` 默认取最新的一份（这是生产行为，不能改）。

但测试里有多处断言「17 个岗位」。如果测试跟着 ``data/`` 的目录内容漂移，
「今天采集过」就会让一批老测试变红 —— 那是假故障，会训练人忽略红色。

所以：生产走「最新」，测试走「固定」。
要验证「取最新」这个行为本身，见 ``tests/test_collect.py`` 里的专项用例。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import build as builder  # noqa: E402

# 仓库自带、随代码提交的初始岗位池
builder.SEED = ROOT / "data" / "seed-2026-09-17.json"
