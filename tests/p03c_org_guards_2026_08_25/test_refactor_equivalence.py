"""【P0-3c 件2 · 行为零变化的**比对式**自证】

抽函数化最危险的地方是"看起来一样"。所以这里不靠肉眼看 diff:
**重构前**把两条臂真跑一遍、把可观察终态逐字冻进 `_equivalence_baseline.json`;
**重构后**再跑,逐字比。任何一个字不同 → 红。

冻的都是确定性可观察量(run 终态 / 产物可见性 / 冻结状态 / 钱包三池 / charge 状态),
**不冻时间列、不冻自增 id** —— 时间进判据 = 定时炸弹(本仓踩过)。

重刷基线:`P03C_WRITE_BASELINE=1 pytest ...`(只在**确认**行为该变时才刷,
而本包的行为**不该变**,所以这个开关在交付时不该被用过)。
"""
from __future__ import annotations

import io
import json
import os
import pathlib

import pytest

from tests.p03c_org_guards_2026_08_25._world import legacy_world, org_world
from tests.p03c_org_guards_2026_08_25.test_settlement_dispatch_runtime import drive

BASELINE = pathlib.Path(__file__).resolve().parent / "_equivalence_baseline.json"


def _observables(server, dsn, monkeypatch, world):
    sent, after = drive(server, world, monkeypatch)
    # SSE 只留"发了什么类型的终态",不留 diagnosis_id / share_token 这类每次都变的值。
    kinds = [(p.get("type"), p.get("stage"), bool(p.get("done"))) for p in sent]
    return {"db": after, "sse": kinds}


def test_both_arms_observables_match_the_frozen_pre_refactor_baseline(
        live_server, live_dsn, monkeypatch):
    current = {
        "legacy": _observables(live_server, live_dsn, monkeypatch, legacy_world(live_dsn)),
        "org": _observables(live_server, live_dsn, monkeypatch, org_world(live_dsn)),
    }
    if os.getenv("P03C_WRITE_BASELINE") == "1":
        io.open(BASELINE, "w", encoding="utf-8", newline="").write(
            json.dumps(current, ensure_ascii=False, indent=2, sort_keys=True))
        pytest.skip("已写入基线 —— 这一跑不算判据")

    assert BASELINE.is_file(), "基线文件不在,重构等价性没有任何东西在守"
    expected = json.loads(io.open(BASELINE, encoding="utf-8").read())
    # 两边都过一次 JSON 归一:直接比会永远不等(tuple 出去、list 回来),
    # 那种"恒红"和"恒绿"一样没有判别力。
    current = json.loads(json.dumps(current, ensure_ascii=False))
    assert current == expected, (
        "抽函数化改变了可观察行为。左=现在 右=重构前基线\n现在: %s\n基线: %s"
        % (json.dumps(current, ensure_ascii=False, sort_keys=True),
           json.dumps(expected, ensure_ascii=False, sort_keys=True)))
