# -*- coding: utf-8 -*-
"""WO_233-c3 · 分范围交付:覆盖太薄就**不出全局总分**(Owner 2026-09-17 拍板)。

根因(Review + A 实证):防御模式的题单只出品牌题,`offensive_count` 生产 30/30 例
全为 0 ⇒ 每次防御型诊断都触发权重重归一、必然虚高,命中率 100%。
c1 已经让封顶活到读取口;c3 再进一步:**单层覆盖时那个 0-100 的数本身没有分母意义**,
不出总分、不扣零分、不放大补满,防御结果照常交付,增长位置写「本次未测」。

🔴 判据取的是**实际观测到的层覆盖**,不是 `diagnosis_mode`、不是题单来源。
   A 查出:用户手动删光两道增长题一样得到 off=0 —— 前端保证不了某层非空,
   「题单合法但某层无题」是正常输入不是异常。
"""
from __future__ import annotations

import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from services.report_v2_score import resolve_canonical_score          # noqa: E402
from tools.scoring.funnel_score import calculate_funnel_score          # noqa: E402


def _row(calc: dict) -> dict:
    return {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "funnel": {
                "total_score": calc["total_score"],
                "level": calc["level"],
                "level_meta": calc["level_meta"],
                "layers": calc["layers"],
            }
        },
    }


SINGLE_LAYER = [
    ("防御题单 · 品牌 10/10(满分那种)", dict(brand_detected=10, brand_total=10)),
    ("防御题单 · 品牌 1/3(低分那种)", dict(brand_detected=1, brand_total=3)),
    ("手工删光增长题后只剩品牌 2/3", dict(brand_detected=2, brand_total=3)),
    ("只剩决策层 3/6", dict(local_detected=3, local_total=6)),
]


@pytest.mark.parametrize("name,kw", SINGLE_LAYER)
def test_a_single_layer_sample_yields_no_global_score(name, kw):
    """🔴 主判据:单层覆盖 ⇒ 全局总分与等级**都不给**。

    🔴 高分低分两种都要跑。只跑满分那种的话,有人把规则写成
       「只在分数高时不出分」我也看不出来 —— 那会变成"报喜不报忧"的反面:
       低分照出、高分藏起来,同样是拿口径迁就结论。
    """
    calc = calculate_funnel_score(**kw)
    assert calc["level_meta"]["scope_limited"] is True, (
        "%s 在评分器那侧就没被判成覆盖过薄 —— 样本没落在缺口上" % name)
    got = resolve_canonical_score(_row(calc))
    assert got["total_score"] is None, "%s 仍给出了总分 %r" % (name, got["total_score"])
    assert got["level"] is None, "%s 仍给出了等级 %r" % (name, got["level"])
    assert got["source"] == "scope_limited", got


@pytest.mark.parametrize("name,kw", SINGLE_LAYER)
def test_the_measured_layer_is_still_reported(name, kw):
    """🔴 不出总分 ≠ 什么都不给。

    「分范围交付」的另一半是**防御结果照常交付** —— 读取口要说清哪一层测了,
    否则客户只看到一片空白,分不清"没测"和"测了为 0"。
    """
    calc = calculate_funnel_score(**kw)
    got = resolve_canonical_score(_row(calc))
    assert got.get("measured_layers"), "没有交出任何已测层:%r" % got
    assert len(got["measured_layers"]) == 1, got["measured_layers"]
    assert got["partial_sample"] is True, got


def test_no_zero_is_handed_out_instead_of_none():
    """🔴 不许拿 0 顶替 None。

    回 0 会被读成「AI 一次都没提到你」—— 那是**另一个**错误陈述,
    而且比原来的虚高更难发现(它看起来像个谦虚的数)。
    """
    calc = calculate_funnel_score(brand_detected=10, brand_total=10)
    got = resolve_canonical_score(_row(calc))
    assert got["total_score"] is None, got
    assert got["total_score"] != 0, got     # None != 0,写出来是为了让这条断言的意图可读


# ── 反向对照 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,kw", [
    ("双层齐", dict(brand_detected=5, brand_total=5, local_detected=5, local_total=5)),
    ("三层齐(全满)", dict(brand_detected=4, brand_total=4, local_detected=4,
                          local_total=4, scenario_detected=4, scenario_total=4)),
    ("三层齐(中等)", dict(brand_detected=2, brand_total=4, local_detected=2,
                          local_total=4, scenario_detected=1, scenario_total=4)),
])
def test_a_multi_layer_sample_still_gets_its_score(name, kw):
    """反向对照:双层/三层**照常出分**。没有这一条,把总分整个停掉也会全绿。"""
    calc = calculate_funnel_score(**kw)
    assert calc["level_meta"]["scope_limited"] is False, name
    got = resolve_canonical_score(_row(calc))
    assert got["total_score"] == calc["total_score"], (name, got)
    assert got["level"] == calc["level"], (name, got)
    assert got["source"] == "v2_funnel", got


def test_zero_observation_is_deliberately_left_alone():
    """🔴 **完全没有观测**那一格没动:仍是 0 · 隐形级。

    我第一版把它一起归进了「分范围交付」,读数看着很顺 —— 但那是另一件事:
    零观测该不该对客户说「隐形级」(等于从零证据做一个负面陈述)是个口径问题,
    Owner 这次拍的是「防御模式分范围交付」,**没拍它**。
    拿一次授权去动两件事,是本仓反复吃亏的形状。已单独报 Review 定。
    这条判据把"没动"钉住 —— 哪天要动,得有人先把它改红。
    """
    calc = calculate_funnel_score()
    assert calc["level_meta"]["scope_limited"] is False, calc["level_meta"]
    got = resolve_canonical_score(_row(calc))
    assert got["total_score"] == 0 and got["level"] == "隐形级", got
    assert got["source"] == "v2_funnel", got


def test_legacy_reports_without_the_flag_behave_exactly_as_before():
    """存量报告不静默重写:老 level_meta 没有 scope_limited 键 ⇒ 行为与改前逐字一致。"""
    row = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "funnel": {"total_score": 100, "level": "成长级",
                       "level_meta": {"level_capped": True, "partial_sample": True}}
        },
    }
    got = resolve_canonical_score(row)
    assert got["total_score"] == 100, got
    assert got["level"] == "成长级", got      # c1 的封顶仍生效
    assert got["source"] == "v2_funnel", got


def test_level_capped_is_unreachable_on_the_live_path():
    """🔴 钉住 c1 与 c3 的**耦合本身**。

    封顶只在 `effective_weight_sum < 60`(= 单层覆盖)时触发,
    而单层覆盖现在一律回 `scope_limited` ⇒ **`level_capped=True` 在活路径上不可达**。
    也就是说 c1 的主判据对新报告已经完全不承重,只剩存量面。

    为什么要把"不可达"钉成判据:
    将来谁放松 c3(比如决定单层也该出分),**这一格会先红**,把他指回 c1 ——
    否则他会看到 c1 那组绿判据,以为封顶还在保护活路径,而那时它已经不在了。
    一组判据变成摆设的时刻是**悄悄**发生的:没有任何一条会因此变红。
    """
    for kw in (dict(brand_detected=10, brand_total=10),
               dict(brand_detected=8, brand_total=10),
               dict(local_detected=6, local_total=6)):
        calc = calculate_funnel_score(**kw)
        assert calc["level_meta"]["level_capped"] is True, kw
        got = resolve_canonical_score(_row(calc))
        assert got["source"] == "scope_limited", (
            "封顶样本在活路径上竟然还走到了出分那条路(%r)—— c3 被放松了?"
            "那 c1 的活路径判据要重新指回来,别让它继续当摆设" % got)
        assert got["level"] is None and got["total_score"] is None, got


# ── hybrid 顶层状态(本单只核不改)──────────────────────────────────

def test_hybrid_top_state_takes_the_worse_side():
    """hybrid 顶层状态取更差一侧 —— 现役 `aggregate_hybrid` 的原则已经是对的,本单**不改**。

    钉住它,是因为 c3 的口径(hybrid 不出合并分、两侧并排)**依赖**这个聚合行为;
    它哪天被改成"取更好一侧",c3 的交付口径会跟着悄悄失真。
    """
    from services.defensive_geo.presentation.comparability import aggregate_hybrid

    class _P:
        def __init__(self, state):
            self._state = state

    import services.defensive_geo.presentation.comparability as comp
    original = comp.section_state_for
    comp.section_state_for = lambda p: p._state
    try:
        assert aggregate_hybrid({"d": _P("ready"), "o": _P("no_conclusion")}) == "partial_observation"
        assert aggregate_hybrid({"d": _P("ready"), "o": _P("ready")}) == "ready"
        assert aggregate_hybrid({"d": _P("no_conclusion"), "o": _P("no_conclusion")}) == "no_conclusion"
    finally:
        comp.section_state_for = original
