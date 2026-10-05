"""C-2(a) · 结算分母 = **计费执行面**(Codex 终审 P1-9)。

被测的那一格:``diagnosis_sample_contract.evaluate_sample`` 算 coverage /
billable 比例时,分母里混进了 ``yuanbao`` —— 一个 Owner 2026-08-23 明令
「不进计价、不进对客承诺、不进交付链」的观测面。

方向是**双向**错的,所以判据也必须双向:
  · 元宝挂了、四个计费引擎全成功 ⇒ 旧口径 3/4 ⇒ **少收**;
  · 计费引擎挂了、元宝成功 ⇒ 旧口径 3/4,而客户真丢的是 1/3 ⇒ **多收**。
只验一个方向的锁抓不到另一半。
"""

from __future__ import annotations

import pytest

from config.ai_engines import (
    DEFENSIVE_GEO_BILLABLE_ENGINES,
    DIAGNOSIS_RUNTIME_ENGINES,
)
from services.diagnosis_sample_contract import (
    OUTCOME_DEGRADED,
    OUTCOME_SUFFICIENT,
    billable_engines_planned,
    billable_points,
    evaluate_sample,
)

QUESTIONS = 8


def _av(*, engines, per_engine_success: dict) -> dict:
    """按**真实产出方**的形状造 ai_visibility。

    🔴 键名逐字取自 ``workflows/diagnosis_workflow.py`` 那两处 ``ai_data`` 组装
       (``engines_tested`` / ``total_planned`` / ``total_tests`` / ``engine_stats``)。
       夹具用一个生产不会发的键,会让整片判据全绿而生产照错(本仓 2026-08-20 实录)。
    """
    planned = QUESTIONS * len(engines)
    tests = sum(per_engine_success.get(e, 0) for e in engines)
    return {
        "engines_tested": list(engines),
        "total_planned": planned,
        "total_tests": tests,
        "total_failed": planned - tests,
        "engine_stats": {
            e: {"detected": 0, "total": per_engine_success.get(e, 0), "rate": 0}
            for e in engines
        },
    }


# ══════════════════════════════════════════════════════════════════════════
# A. 分母的**来源**必须是机械的
# ══════════════════════════════════════════════════════════════════════════
def test_c2_00_billable_surface_comes_from_config_not_a_hand_written_list() -> None:
    """分母取自 ``config.ai_engines.DEFENSIVE_GEO_BILLABLE_ENGINES``,不手抄。

    这一条的判别力来自一个**今天就成立**的事实:运行集与计价集**不相等**
    (运行集含 yuanbao 不含 kimi)。两者相等的话,下面所有判据都会因为
    "收窄前后一样"而恒绿 —— 那时这条判据会先红,提醒我们分母口径变了。
    """
    assert set(DIAGNOSIS_RUNTIME_ENGINES) != set(DEFENSIVE_GEO_BILLABLE_ENGINES), (
        "运行集与计价集现在相等 —— 本组判据失去判别力,需要重新设计样本")
    assert "yuanbao" in DIAGNOSIS_RUNTIME_ENGINES
    assert "yuanbao" not in DEFENSIVE_GEO_BILLABLE_ENGINES

    picked = billable_engines_planned(list(DIAGNOSIS_RUNTIME_ENGINES))
    assert "yuanbao" not in picked, picked
    assert set(picked) == set(DIAGNOSIS_RUNTIME_ENGINES) & set(DEFENSIVE_GEO_BILLABLE_ENGINES)


# ══════════════════════════════════════════════════════════════════════════
# B. 双向:少收 / 多收 各一发
# ══════════════════════════════════════════════════════════════════════════
def test_c2_01_non_billable_failure_must_not_reduce_the_bill() -> None:
    """🔴 **少收方向**:元宝挂了,而计费的三个全成功 ⇒ 全额。

    拆红:把 ``_billable_counts`` 的收窄摘掉 ⇒ 比例回到 3/4 ⇒ 本条红。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)          # dashscope/deepseek/doubao/yuanbao
    billable = [e for e in engines if e in DEFENSIVE_GEO_BILLABLE_ENGINES]
    av = _av(engines=engines,
             per_engine_success={e: QUESTIONS for e in billable} | {"yuanbao": 0})

    v = evaluate_sample(av)
    assert v.outcome == OUTCOME_SUFFICIENT, (v.outcome, v.planned, v.succeeded)
    assert v.billable_ratio == 1.0, v
    assert billable_points(v, 8000) == 8000, "元宝挂了却少收了钱"
    assert list(v.evidence.get("non_billable_engines_excluded") or []) == ["yuanbao"], v.evidence


def test_c2_02_billable_failure_must_not_be_diluted_by_a_non_billable_success() -> None:
    """🔴 **多收方向**:豆包挂了、元宝成功 ⇒ 客户真丢的是 1/3,不是 1/4。

    旧口径给 0.75(4 格里坏 1 格),按 8000 冻结算 6000;
    正确口径是 2/3,算 5333。差的那 667 是**多收**的。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    av = _av(engines=engines, per_engine_success={
        "dashscope": QUESTIONS, "deepseek": QUESTIONS,
        "doubao": 0,                      # 计费引擎挂了
        "yuanbao": QUESTIONS,             # 非计费面成功
    })

    v = evaluate_sample(av)
    assert v.outcome == OUTCOME_DEGRADED, v
    assert v.planned == QUESTIONS * 3, v          # 三个计费引擎
    assert v.succeeded == QUESTIONS * 2, v
    assert round(v.billable_ratio, 4) == round(2 / 3, 4), v
    assert billable_points(v, 8000) == int(8000 * (2 / 3)), v
    # 反向对照:旧口径(4 格分母)会给 0.75 —— 明确写出来,免得日后有人
    # 以为 2/3 与 3/4 差不多。
    assert round(v.billable_ratio, 4) != 0.75


def test_c2_03_all_billable_engines_dead_is_still_a_full_refund() -> None:
    """三个计费引擎全挂、只有元宝成功 ⇒ **零可用结果**,整单退。

    这一格是收窄之后才成立的:旧口径把元宝那 8 次算进 ``total_tests``,
    于是 succeeded=8 > 0 ⇒ DEGRADED ⇒ 还要收 25% 的钱,
    而客户买的四个引擎**一个都没测成**。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    av = _av(engines=engines, per_engine_success={"yuanbao": QUESTIONS})

    v = evaluate_sample(av)
    assert v.outcome == "insufficient", v
    assert billable_points(v, 8000) == 0, "客户买的引擎一个都没测成,却还在收钱"


# ══════════════════════════════════════════════════════════════════════════
# C. 不猜:算不出来就**保持原口径**
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("mutate", [
    pytest.param(lambda av: av.pop("engines_tested"), id="no_engines_tested"),
    pytest.param(lambda av: av.pop("engine_stats"), id="no_engine_stats"),
    pytest.param(lambda av: av.update({"engine_stats": {"dashscope": "bad"}}),
                 id="engine_stats_shape_broken"),
    pytest.param(lambda av: av.update({"total_planned": 31}), id="planned_not_divisible"),
])
def test_c2_10_unresolvable_narrowing_keeps_the_old_reading(mutate) -> None:  # noqa: ANN001
    """收窄的四个前置任何一个不成立 ⇒ 分母**逐位回到改动前**。

    少收一格远好过按一个编出来的比例扣款。这一组同时是「旧入参旧行为不变」
    的证明:老结构(没有 engines_tested / engine_stats)的调用方一点没变。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    av = _av(engines=engines, per_engine_success={
        "dashscope": QUESTIONS, "deepseek": QUESTIONS,
        "doubao": QUESTIONS, "yuanbao": 0})
    mutate(av)

    v = evaluate_sample(av)
    # 回到旧口径 = 4 格分母 ⇒ 0.75(planned 被改成 31 的那一臂另算)
    assert list(v.evidence.get("non_billable_engines_excluded") or []) == [], v.evidence


def test_c2_11_all_billable_engines_means_no_change_at_all() -> None:
    """计划集**全是**计费面 ⇒ 收窄不发生,行为逐位不变。

    这一条把"收窄"限制在它该发生的地方:客户买什么就跑什么的 defgeo 链
    (run_executor 显式传计费集)不该因为本包多出任何一格变化。
    """
    engines = list(DEFENSIVE_GEO_BILLABLE_ENGINES)
    av = _av(engines=engines, per_engine_success={
        e: (0 if e == "kimi" else QUESTIONS) for e in engines})

    v = evaluate_sample(av)
    assert v.planned == QUESTIONS * len(engines), v
    assert round(v.billable_ratio, 4) == round(3 / 4, 4), v
    assert list(v.evidence.get("non_billable_engines_excluded") or []) == [], v.evidence


def test_c2_12_probe_is_alive_narrowing_really_changes_the_number() -> None:
    """判别力自证:同一份样本在**收窄前后**必须给出不同的数。

    没有这一条,上面那些 ``== 1.0`` / ``== 2/3`` 有可能只是因为收窄从来没发生过
    (例如 import 失败让 ``billable_engines_planned`` 恒返空)。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    success = {"dashscope": QUESTIONS, "deepseek": QUESTIONS,
               "doubao": QUESTIONS, "yuanbao": 0}
    narrowed = evaluate_sample(_av(engines=engines, per_engine_success=success))

    naive = _av(engines=engines, per_engine_success=success)
    naive.pop("engine_stats")             # 逼它回到旧口径
    old = evaluate_sample(naive)

    assert narrowed.billable_ratio != old.billable_ratio, (
        "收窄前后给出同一个数 —— 收窄根本没发生,上面的断言全是恒真")
    assert narrowed.billable_ratio == 1.0 and round(old.billable_ratio, 4) == 0.75


# ══════════════════════════════════════════════════════════════════════════
# C. [外选 EXTC-04] 分子的**溢出**方向 —— 改动前零夹具
# ══════════════════════════════════════════════════════════════════════════
#
# 🔴 先把外选草单的说法**证伪一半**,免得后来的人照着它去改代码:
#
#   草单说摘掉 ``min(...)`` 封顶会让「分子溢出盖过另一个计费引擎的真失败
#   ⇒ full_coverage ⇒ 按 100% 扣款」。**这半句在两边都成立,不是封顶挡住的**:
#   封顶把分子夹到 ``planned``,而 ``succeeded >= planned`` 正是
#   ``full_coverage`` 的入口条件 —— 封顶不封顶,那一单一样判 SUFFICIENT、
#   一样按 100% 计费(实测:dashscope 超报 16 / deepseek 8 / doubao 0,
#   封顶后 succeeded=24=planned,照样 full_coverage)。
#
#   总量封顶守住的是**另一格**:``planned`` 与 ``succeeded`` 会原样写进
#   ``workflows/diagnosis_workflow.py:1197`` 那份耐久 ``delivery_verdict``
#   快照(结算侧与退款争议读的就是它)。分子大于分母的一行审计记录说的是
#   「32 题里跑成了 34 次」—— 它不是一个可以对账的数。
#
#   而「超报能盖住一个整死的计费引擎」是**另一个、更贵的**洞,总量封顶挡不住
#   (封顶把 24 夹成 24,``succeeded >= planned`` 正是 full_coverage 的入口)。
#   Review 2026-08-26 裁定按「按真实履约扣费」口径修:**逐引擎封顶**,
#   只影响未来结算不追溯。那一格由下面 ``test_c2_15/16`` 守。
def test_c2_13_the_numerator_can_never_exceed_the_denominator() -> None:
    """🔴 [外选 EXTC-04] 分子任何时候都不许大于分母。

    超报是真实存在的形态:``engine_stats[e]["total"]`` 由采集侧逐格累加,
    重试/双报会让单引擎的成功数超过它自己的计划题数。

    🔴 逐引擎封顶落地之后,本条**单发杀不动**了 —— 如实写明,不装。
       两道封顶现在是冗余的:循环体里 ``min(per_engine, ...)`` 保证每项 ≤ per_engine,
       共 len(billable) 项 ⇒ 和必 ≤ 分母 ⇒ 结尾那个
       ``min(succeeded, per_engine * len(billable))`` 在数学上**恒等于** ``succeeded``。
       所以:
         · 摘外层 ``min``(= 原 MUT-EXTC-04)⇒ 无可观测差,本条绿;
         · 摘逐引擎 ``min``(= 继任者 MUT-EXTC-04b)⇒ 26 被外层夹回 24,本条**也**绿
           —— 那一发由 ``test_c2_15`` 的覆盖率判据杀;
         · 两处**同时**摘 ⇒ 26 > 24 ⇒ 本条红(判别力实测见交付文)。
       本条今天的角色是**不变式**:不管封顶写在哪一层、以后怎么重构,
       写进耐久 ``delivery_verdict`` 快照的分子不许越过分母。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    over = {"dashscope": QUESTIONS + 10, "deepseek": QUESTIONS,
            "doubao": 0, "yuanbao": QUESTIONS}
    v = evaluate_sample(_av(engines=engines, per_engine_success=over))

    billable = billable_engines_planned(engines)
    assert v.planned == QUESTIONS * len(billable), v
    assert v.succeeded <= v.planned, (
        f"分子({v.succeeded})大于分母({v.planned})—— 落进耐久 delivery_verdict "
        "快照的是一行没法对账的审计记录")
    assert v.failed >= 0, v
    assert 0.0 <= v.coverage_ratio <= 1.0, v
    assert 0.0 <= v.billable_ratio <= 1.0, v
    assert billable_points(v, 6000) <= 6000, "超报把可计费点数抬到了冻结额之上"


def test_c2_14_the_overflow_sample_really_overflows() -> None:
    """前提自证:上一条用的样本**真的**触发了封顶那一位。

    没有这一条,``succeeded <= planned`` 可能只是因为样本压根没超报 ——
    那时封顶那一位从没被执行过,判据却全绿(本仓记过:样本要打在会短路的那一位之后)。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    over = {"dashscope": QUESTIONS + 10, "deepseek": QUESTIONS,
            "doubao": 0, "yuanbao": QUESTIONS}
    billable = billable_engines_planned(engines)
    raw = sum(over[e] for e in billable)
    assert raw > QUESTIONS * len(billable), (
        f"样本没超报(raw={raw} ≤ planned={QUESTIONS * len(billable)})—— "
        "封顶那一位在这条样本下不会被执行")
    # 反向:不超报的样本必须**不**被夹(否则封顶会顺手改掉正常单的数)
    normal = {"dashscope": QUESTIONS, "deepseek": QUESTIONS - 3,
              "doubao": 0, "yuanbao": QUESTIONS}
    v = evaluate_sample(_av(engines=engines, per_engine_success=normal))
    assert v.succeeded == sum(normal[e] for e in billable), (
        f"没超报的样本也被改了数:{v}")


# ══════════════════════════════════════════════════════════════════════════
# D. [外选 EXTC-04 真洞 · Review 2026-08-26 裁定] 超报不许盖住整死的引擎
# ══════════════════════════════════════════════════════════════════════════
def test_c2_15_an_over_reporting_engine_cannot_mask_a_dead_billable_one() -> None:
    """🔴 **本项的本体判据**:一个引擎多报,不能替另一个整死的引擎履约。

    修复前实测(2026-08-26,`per_engine=8`):
    dashscope 16 / deepseek 8 / doubao **0** ⇒ 分子 16+8+0 = 24 = 分母 24
    ⇒ `full_coverage` ⇒ SUFFICIENT ⇒ 冻结 6000 **全额扣 6000**,
    而客户买的三面里**豆包那一面一次都没测成**。

    「按真实履约扣费」在分子上的形态:一个引擎最多只能履约它自己那一份
    ``per_engine``,多报的不算履约。所以正确结论是
    succeeded=16 / planned=24 / **degraded** / ratio=2/3 / 扣 4000。

    拆红:把 ``_billable_counts`` 循环体里的 ``min(per_engine, ...)`` 摘掉
    ⇒ 回到 full_coverage / 全额 ⇒ 本条红。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    billable = billable_engines_planned(engines)
    assert "doubao" in billable and "yuanbao" not in billable, billable

    over = {"dashscope": QUESTIONS * 2, "deepseek": QUESTIONS,
            "doubao": 0, "yuanbao": QUESTIONS}
    v = evaluate_sample(_av(engines=engines, per_engine_success=over))

    assert v.planned == QUESTIONS * len(billable), v
    assert v.succeeded == QUESTIONS * (len(billable) - 1), (
        f"分子不是「每个引擎最多算它自己那一份」:{v}")
    assert v.outcome == OUTCOME_DEGRADED, (
        f"一整面没测成却判成 {v.outcome!r} —— 超报把它盖住了")
    assert v.reason_code == "partial_provider_failure", v
    assert v.failed == QUESTIONS, v
    assert round(v.billable_ratio, 4) == round(
        (len(billable) - 1) / len(billable), 4), v
    # 钱那一位:未履约的那一面**不计费**
    assert billable_points(v, 6000) == int(6000 * (len(billable) - 1) / len(billable)), (
        f"扣款没按真实履约走:{billable_points(v, 6000)} / {v}")


def test_c2_16_honest_full_delivery_is_still_billed_in_full() -> None:
    """判别力反臂:每个计费引擎都**正好**跑满时,仍然是全额。

    没有这一条,上一条的 degraded 有可能只是因为封顶把正常单也夹小了
    —— 那会让每一单都少收,方向反了一样是资金事故。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    billable = billable_engines_planned(engines)
    full = {e: (QUESTIONS if e in billable else 0) for e in engines}
    v = evaluate_sample(_av(engines=engines, per_engine_success=full))

    assert v.outcome == OUTCOME_SUFFICIENT, v
    assert v.reason_code == "full_coverage", v
    assert v.succeeded == v.planned == QUESTIONS * len(billable), v
    assert billable_points(v, 6000) == 6000, v


def test_c2_17_an_honest_partial_failure_is_unchanged_by_the_cap() -> None:
    """封顶只删「多报的那部分」,不动老实样本 —— 逐位对照。

    同一场"豆包整死",一次是**老实报数**(8/8/0)、一次是**超报**(16/8/0):
    修好之后两者的结算结论必须**完全一样**。不一样就说明封顶改的不只是超报。
    """
    engines = list(DIAGNOSIS_RUNTIME_ENGINES)
    honest = evaluate_sample(_av(engines=engines, per_engine_success={
        "dashscope": QUESTIONS, "deepseek": QUESTIONS,
        "doubao": 0, "yuanbao": QUESTIONS}))
    over = evaluate_sample(_av(engines=engines, per_engine_success={
        "dashscope": QUESTIONS * 2, "deepseek": QUESTIONS,
        "doubao": 0, "yuanbao": QUESTIONS}))

    for field in ("outcome", "planned", "succeeded", "failed",
                  "coverage_ratio", "billable_ratio", "reason_code"):
        assert getattr(honest, field) == getattr(over, field), (
            f"老实报数与超报给出不同结论(字段 {field}):"
            f"{getattr(honest, field)!r} vs {getattr(over, field)!r}")
    assert honest.outcome == OUTCOME_DEGRADED, honest
    # 活性:这两份样本**确实不同**(否则上面的逐位相等是恒真)
    assert (_av(engines=engines, per_engine_success={
        "dashscope": QUESTIONS, "deepseek": QUESTIONS, "doubao": 0,
        "yuanbao": QUESTIONS})["total_tests"]
        != _av(engines=engines, per_engine_success={
            "dashscope": QUESTIONS * 2, "deepseek": QUESTIONS, "doubao": 0,
            "yuanbao": QUESTIONS})["total_tests"]), "两份样本其实是同一份"
