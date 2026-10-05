"""
回归判别锁 · services/research_monitor/cost_estimator.py

修复 finding:
- GEO-R1-CAN-090 (P3 · pricing-l3-per-engine-underestimate):
  L3 答案实体抽取按 answer-group 计费,一题被 4 引擎回答产生最多 4 个 answer-group
  → 每题 L3 调用数 ∝ 引擎数,不是 1。原 `SELFSERVE_L3_YUAN_PER_PROMPT * n` 低估 4x。
  修复:乘 SELFSERVE_L3_ENGINE_COUNT(=4)。

主形态 = source-inspection 判别锁(回退修复则断言失败),另加纯函数行为单测。
不依赖 DB / 不 import server.py。
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "services" / "research_monitor" / "cost_estimator.py"


def _read_src() -> str:
    return SRC_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# GEO-R1-CAN-090 · source-inspection 判别锁
# ---------------------------------------------------------------------------
def test_geo_r1_can_090_engine_count_constant_defined():
    """修复标志:引入 SELFSERVE_L3_ENGINE_COUNT 常量(回退则无此常量)。"""
    src = _read_src()
    assert "SELFSERVE_L3_ENGINE_COUNT" in src, (
        "缺 SELFSERVE_L3_ENGINE_COUNT — L3 按 answer-group/引擎数计费的修复被回退"
    )
    assert "[GEO-R1-CAN-090]" in src


def test_geo_r1_can_090_l3_multiplies_engine_count():
    """修复标志:l3_yuan 计算乘上 SELFSERVE_L3_ENGINE_COUNT(不再是纯 *n)。"""
    src = _read_src()
    # 定位 l3_yuan 赋值行,必须含引擎数因子
    l3_lines = [
        ln for ln in src.splitlines()
        if "l3_yuan =" in ln and "SELFSERVE_L3_YUAN_PER_PROMPT" in ln
    ]
    assert l3_lines, "找不到 l3_yuan 计算行"
    assert any("SELFSERVE_L3_ENGINE_COUNT" in ln for ln in l3_lines), (
        "l3_yuan 未乘引擎数 — 每题仍只算 1 次 L3 抽取(低估)"
    )


# ---------------------------------------------------------------------------
# GEO-R1-CAN-090 · 纯函数行为单测(不需 DB/app)
# ---------------------------------------------------------------------------
def test_geo_r1_can_090_l3_covers_per_engine_extraction():
    """
    行为:modeled l3_yuan 必须 >= 每题 worst-case 4 个 answer-group 的实际抽取成本。
    """
    from services.research_monitor.cost_estimator import (
        estimate_selfserve_round_cost,
        SELFSERVE_L3_YUAN_PER_PROMPT,
        SELFSERVE_L3_ENGINE_COUNT,
    )

    n = 6
    result = estimate_selfserve_round_cost(prompt_count=n)
    l3_yuan = result["breakdown"]["l3_yuan"]

    # worst-case:每题 4 引擎各产生 1 个独立 answer-group → 4 次 L3 抽取
    actual_worst_case = round(SELFSERVE_L3_YUAN_PER_PROMPT * n * 4, 2)
    assert l3_yuan >= actual_worst_case, (
        f"l3_yuan={l3_yuan} 低于 worst-case 实际成本 {actual_worst_case}(每题 4 引擎抽取)"
    )
    assert SELFSERVE_L3_ENGINE_COUNT == 4


def test_geo_r1_can_090_l3_not_underestimated_single_prompt():
    """单题也不能低估:l3_yuan 应等于 单价 × 引擎数。"""
    from services.research_monitor.cost_estimator import (
        estimate_selfserve_round_cost,
        SELFSERVE_L3_YUAN_PER_PROMPT,
        SELFSERVE_L3_ENGINE_COUNT,
    )

    result = estimate_selfserve_round_cost(prompt_count=1)
    expected = round(SELFSERVE_L3_YUAN_PER_PROMPT * 1 * SELFSERVE_L3_ENGINE_COUNT, 2)
    assert result["breakdown"]["l3_yuan"] == expected


if __name__ == "__main__":
    import pytest

    sys.exit(pytest.main([__file__, "-q"]))
