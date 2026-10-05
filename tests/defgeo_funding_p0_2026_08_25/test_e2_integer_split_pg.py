"""【E2-1 = Codex 二审 P1-F1】分账一律整数,float 不参与算钱。

修之前的链(逐条亲手复现过,不是转述):
  · `workflows/diagnosis_workflow.py` 两处持久化点都写 `round(_v.billable_ratio, 4)`
    —— 比例落库时被截到**四位小数**;
  · `services/diagnosis_runs.py::_partial_commit_points` 再 `int(total * ratio)`;
  · `services/diagnosis_sample_contract.py::billable_points` 同形(用的是 dataclass
    上的原始 float,误差面小一些但**不是零**)。

亲手验算 Codex 那一例:planned=32 succeeded=11 total=20800
  原始 11/32 = 0.34375 → 持久化 0.3438 → int(20800 × 0.3438) = 7151
  整数精确 (20800 × 11) // 32 = 7150                        ⇒ **多收 1**

我自己另做了枚举复核(分母写明:p ≤ 64 × s < p × 三个总额 = 6048 组):
1545 组两式不等(25.5%),**两个方向都有** —— 多收 678、少收 867。
所以这不是「偏保守」也不是「偏激进」,是这个数**就是错的**,
与本函数一贯的「既不多收也不少收」直接冲突。

🔴 本文件所有 expected 都是**手算的字面量**,推导写在各自注释里。
   判据不许用被测代码的公式构造自己的期望 —— 那样实现算错了判据也跟着错,
   永远不会红。工单 E2-1 第 3 条逐字要求,也是本仓铁律。
   (既有 A1/A3/A4 那四臂原本正是这个形态:样本用可精确表示的 0.25、
    expected 写成 `int(COST * RATIO)`,两式恒等 ⇒ 对「用不用整数」零判别力。
    本单已把那四臂一并换成非终止小数 + 字面量期望。)
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# ═══════════════════════════════════════════════════════════════════════════
# 纯函数层:两个算钱站点
# ═══════════════════════════════════════════════════════════════════════════
def _degraded_verdict_dict(planned, succeeded):
    """形状照**生产持久化那两处**逐字来(含 4dp 截断的 ratio)。"""
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION
    ratio = succeeded / planned
    return {"version": SAMPLE_CONTRACT_VERSION, "outcome": OUTCOME_DEGRADED,
            "planned": planned, "succeeded": succeeded,
            "coverage_ratio": round(ratio, 4), "billable_ratio": round(ratio, 4)}


def _settle(planned, succeeded, total):
    from services.diagnosis_runs import _partial_commit_points
    run = {"final_snapshot_jsonb": None}
    snap = {"delivery_verdict": _degraded_verdict_dict(planned, succeeded)}
    return _partial_commit_points(run, snap, reserved_total_provider=lambda: total)


def test_e2_the_reported_case_charges_the_exact_integer_not_one_point_more():
    """Codex 报的那一例原样复现:planned=32 succeeded=11 total=20800。

    手算 expected:20800 × 11 = 228800;228800 ÷ 32 —— 32 × 7000 = 224000,
    余 4800;32 × 150 = 4800 ⇒ 商 **7150**,整除无余数。

    修之前:0.34375 落库成 0.3438 → int(20800 × 0.3438) = 7151 ⇒ 多收 1。
    """
    actual, err = _settle(32, 11, 20800)
    assert err is None, err
    assert actual == 7150, (
        "实扣 %r,精确应为 7150。7151 是四位小数截断的产物(0.34375 → 0.3438),"
        "客户为一次降级交付多付了 1 点" % actual)


def test_e2_a_seventy_percent_delivery_is_not_one_point_short():
    """另一个方向:planned=10 succeeded=7 total=650。

    手算:650 × 7 = 4550;4550 ÷ 10 = **455**(整除)。
    修之前:0.7 在二进制里无限循环,650 × 0.7 = 454.99999999999994 → int 得 454
    ⇒ **少收 1**。70% 覆盖率是最普通的一档,不是构造出来的边角料 ——
    所以这个缺陷的两个方向都不冷僻。
    """
    actual, err = _settle(10, 7, 650)
    assert err is None, err
    assert actual == 455, "实扣 %r,精确应为 455(650×7=4550,÷10)" % actual


@pytest.mark.parametrize("planned,succeeded,total,expected", [
    # 每一行的 expected 都是手算的,推导写在这里:
    (13, 9, 650, 450),        # 650×9 = 5850;13×450 = 5850 ⇒ 整除,450
    (13, 5, 650, 250),        # 650×5 = 3250;13×250 = 3250 ⇒ 整除,250
    (3, 2, 20800, 13866),     # 20800×2 = 41600;3×13866 = 41598,余 2 ⇒ 13866
    (6, 5, 20800, 17333),     # 20800×5 = 104000;6×17333 = 103998,余 2 ⇒ 17333
    (11, 6, 6000, 3272),      # 6000×6 = 36000;11×3272 = 35992,余 8 ⇒ 3272
    #  🔴 上一版这里写的是 (11, 3, 650, 177) —— 新旧两式在它上面**算出同一个数**，
    #     它继续待在这里只会让人以为覆盖面更宽。是下面那条
    #     「先量区分力」把它抳出来的。
])
def test_e2_non_terminating_samples_settle_on_the_exact_integer(
        planned, succeeded, total, expected):
    """一组**非终止小数**样本,expected 全部手算。

    分母刻意都不是 2 的幂(13/3/6/11)—— 2 的幂在二进制里可精确表示,
    用它做样本对「用不用整数」零判别力,那正是既有判据踩过的坑。
    """
    actual, err = _settle(planned, succeeded, total)
    assert err is None, err
    assert actual == expected, (
        "planned=%d succeeded=%d total=%d ⇒ 实扣 %r,手算精确值是 %r"
        % (planned, succeeded, total, actual, expected))


def test_e2_the_old_float_formula_really_would_disagree_on_these_samples():
    """判别力自证:上面那些样本在**修之前**的公式下确实算出不同的数。

    没有这一条,上面几条可能只是在验「两式恰好相等」的样本 ——
    那样它们全绿也证明不了整数修复在起作用(本仓「先量区分力」)。
    这里**不调用被测代码**,只把修前公式在判据里重演一遍做对照。
    """
    disagreements = 0
    for planned, succeeded, total, expected in (
            (32, 11, 20800, 7150), (10, 7, 650, 455), (13, 9, 650, 450),
            (13, 5, 650, 250), (3, 2, 20800, 13866), (6, 5, 20800, 17333),
            (11, 6, 6000, 3272)):
        old = max(1, min(total, int(total * round(succeeded / planned, 4))))
        if old != expected:
            disagreements += 1
    assert disagreements == 7, (
        "七个样本里只有 %d 个能区分新旧公式 —— 不能区分的那些是在守空气,换样本。"
        "(这条判据已经真的抓到过一次:初版的 (11,3,650) 两式同为 177。)"
        % disagreements)


# ═══════════════════════════════════════════════════════════════════════════
# 旧快照(只有 ratio、没有整数计数)⇒ 转人工,禁 float 回猜
# ═══════════════════════════════════════════════════════════════════════════
def test_e2_a_legacy_snapshot_without_counts_goes_to_manual_instead_of_guessing():
    """四位小数已经把信息丢了 —— 从 0.3438 反推不出 11/32,所以不许猜。

    (0.34375 与 0.34380 之间有无穷多个真分数;拿 ratio 回乘等于在这些里面
     随便挑一个替客户做主。这一档的口径一贯是「既不多收也不少收 → 转人工」。)
    """
    from services.diagnosis_runs import _partial_commit_points
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION

    legacy = {"version": SAMPLE_CONTRACT_VERSION, "outcome": OUTCOME_DEGRADED,
              "billable_ratio": 0.3438}            # ← 旧快照就长这样:只有 ratio
    actual, err = _partial_commit_points(
        {"final_snapshot_jsonb": None}, {"delivery_verdict": legacy},
        reserved_total_provider=lambda: 20800)
    assert actual is None, "旧快照被算出了一个数(%r)—— 那个数只能是猜的" % actual
    assert err == "delivery_verdict_counts_missing", err


@pytest.mark.parametrize("planned,succeeded", [
    (0, 5),        # planned 非正 —— 分母塌了
    (10, 0),       # succeeded=0 是 insufficient,不该走到分账
    (10, 10),      # succeeded=planned 是 sufficient,同上
    (10, 11),      # 超报:成功数大于计划数,数据自相矛盾
    (10, None),    # 缺键
    ("10", "7"),   # 字符串:形状对不上
])
def test_e2_incoherent_counts_go_to_manual_too(planned, succeeded):
    """计数不自洽同样转人工 —— 配对的必须命中,证明上一条不是只认「缺键」。

    `("10","7")` 那一格是有意的:它**能**被 int() 转成功,但形状不是生产会发的,
    收下它就等于允许一个"看起来像数"的脏值去算钱。
    """
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION
    from services.diagnosis_runs import _partial_commit_points

    v = {"version": SAMPLE_CONTRACT_VERSION, "outcome": OUTCOME_DEGRADED,
         "billable_ratio": 0.5, "planned": planned, "succeeded": succeeded}
    actual, err = _partial_commit_points(
        {"final_snapshot_jsonb": None}, {"delivery_verdict": v},
        reserved_total_provider=lambda: 650)
    assert actual is None and err == "delivery_verdict_counts_missing", (
        "planned=%r succeeded=%r 竟然算出了 %r(err=%r)" % (planned, succeeded, actual, err))


def test_e2_a_coherent_snapshot_is_still_settled_automatically():
    """🔴 配对的必须不命中:自洽的快照必须照常自动分账。

    少了它,一个「无条件返 counts_missing」的实现也能让上面那几条绿 ——
    而那样每一单降级交付都会堆进人工队列,比原来的 bug 更贵。
    """
    actual, err = _settle(13, 9, 650)
    assert err is None and actual == 450, (actual, err)


# ═══════════════════════════════════════════════════════════════════════════
# ratio × 计数 交叉校验
# ═══════════════════════════════════════════════════════════════════════════
def test_e2_a_ratio_that_contradicts_the_counts_goes_to_manual():
    """ratio 与整数计数对不上 ⇒ 快照被改过 ⇒ 转人工,不许挑一个信。

    这一条守的是:整数化之后 ratio 退居展示位,如果没人再看它,
    有人改了 ratio(用户可见的那个数)而不改计数,就再没有任何东西会红。
    """
    from services.diagnosis_runs import _partial_commit_points
    from services.diagnosis_sample_contract import OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION

    v = {"version": SAMPLE_CONTRACT_VERSION, "outcome": OUTCOME_DEGRADED,
         "planned": 13, "succeeded": 9,      # 真实比例 0.6923…
         "billable_ratio": 0.10}             # 展示给用户的却是 10%
    actual, err = _partial_commit_points(
        {"final_snapshot_jsonb": None}, {"delivery_verdict": v},
        reserved_total_provider=lambda: 650)
    assert actual is None, "ratio 与计数矛盾却仍然算出了 %r" % actual
    assert err == "delivery_verdict_ratio_counts_disagree", err


def test_e2_the_cross_check_tolerates_the_four_decimal_truncation_itself():
    """配对的必须不命中:**截断本身**造成的那点偏移不许被判成矛盾。

    生产落库的 ratio 就是 `round(x, 4)`,所以正常单里 ratio 与计数必然差一点点。
    容差要是给窄了,每一单降级交付都会转人工 —— 那是把 bug 换成了事故。
    """
    for planned, succeeded, total in ((32, 11, 20800), (13, 9, 650), (11, 3, 650),
                                      (3, 2, 20800), (997, 991, 20800)):
        actual, err = _settle(planned, succeeded, total)
        assert err is None, (
            "正常单(%d/%d @ %d)被交叉校验拦下了:%r —— 容差比截断误差还窄"
            % (succeeded, planned, total, err))


# ═══════════════════════════════════════════════════════════════════════════
# 站点二:contract.billable_points
# ═══════════════════════════════════════════════════════════════════════════
def test_e2_the_contract_helper_uses_integers_too():
    """`billable_points` 同样不许用 float 算钱。

    这一处**生产当前零调用方**(全仓 census:只有 woc 判据包在用),
    但它就摆在正确那份旁边,名字还叫 `billable_points` —— 下一个调用方会直接继承
    这个 bug。所以照样改,并配这一条钉住。

    手算:frozen 650,planned 10,succeeded 7 ⇒ 650×7 = 4550,÷10 = **455**。
    修之前 650 × 0.7 = 454.99999999999994 → int 得 454。
    """
    from services.diagnosis_sample_contract import billable_points, evaluate_sample

    v = evaluate_sample({"total_planned": 10, "total_tests": 7,
                         "engine_stats": {}, "engines": []})
    if v.outcome != "degraded":
        pytest.skip("evaluate_sample 对这组输入不判 degraded(%s)—— 换直构 verdict 那条" % v.outcome)
    assert billable_points(v, 650) == 455, billable_points(v, 650)


def test_e2_the_contract_helper_refuses_incoherent_counts():
    """配对的必须命中:计数不自洽时**抛**,不静默回落 float。

    直构 `SampleVerdict`(不经 `evaluate_sample`),因为要造的正是生产产出方
    不会产出的那种脏值 —— 而下一个调用方可能从别处拿到它。
    """
    from services.diagnosis_sample_contract import (
        OUTCOME_DEGRADED, SAMPLE_CONTRACT_VERSION, SampleVerdict, billable_points,
    )
    bad = SampleVerdict(SAMPLE_CONTRACT_VERSION, OUTCOME_DEGRADED,
                        0, 0, 0, 0.5, 0.5, (), (), "poisoned", "", {})
    with pytest.raises(ValueError):
        billable_points(bad, 650)
