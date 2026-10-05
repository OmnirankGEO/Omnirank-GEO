"""ACT-12 / §19 变异 74:新增 runtime H0 硬门必须有 Owner-signed 完整登记(§0.5.3 G-1)。

这一组判据打的是**能力**不是清单:
证明「未签发的规则拿不到硬门资格」,而不是证明「某份清单里写了几条」。
所以就算表是空的,判据也有判别力 —— 空表时任何 ``enforce_h0`` 都必须拒绝。
"""

from __future__ import annotations

import pytest

from services.defensive_geo.h0_rule_catalog import (
    CATALOG_VERSION,
    H0Rule,
    H0RuleNotSigned,
    _completeness_gaps,
    census,
    derived_candidate_ids,
    derived_rule_ids,
    enforce_h0,
    pending_rule_ids,
    signed_rule_ids,
    statically_signed_rule_ids,
)


def test_catalog_version_is_pinned():
    """改任一条必须升版,否则旧 sidecar 会按新语义执行(§19 变异 181)。"""
    assert CATALOG_VERSION == "defensive-geo-h0-catalog-v1"
    assert census()["catalog_version"] == CATALOG_VERSION


def test_unknown_rule_can_never_be_enforced():
    """🔴 主锁:不在目录里的规则,拿不到硬门资格。"""
    with pytest.raises(H0RuleNotSigned) as exc:
        enforce_h0("defgeo.some.rule.nobody.signed")
    assert "A1" in str(exc.value), "拒绝信息必须指出唯一合法出路是降级,不能只说『不行』"


def test_pending_candidates_are_listed_but_not_enforceable():
    """待签候选可机读(不散在文档里),但**一条都不许**当硬门用。

    这条同时是 §21.1「Owner 待签队列」的对账口:
    候选非空 = 有东西在等签;候选可 enforce = 有人绕过了签发。
    """
    pending = pending_rule_ids()
    assert pending, "待签清单为空 —— 要么真没有,要么 census 抽取面坏了"
    for rule_id in sorted(pending):
        with pytest.raises(H0RuleNotSigned):
            enforce_h0(rule_id)


def test_signed_and_pending_never_overlap():
    """必须不命中:一条规则不能既『已签』又『待签』—— 那样对账永远说不清。"""
    assert not (signed_rule_ids() & pending_rule_ids())


def test_implementation_can_never_sign_a_h0_gate_for_itself():
    """🔴 真正的分母守卫:**实现方自己写死**的签发集合恒为空。

    语义变更说明(窗C / WP5+WP6 · 2026-08-21):
    本判据的前身是 ``test_this_window_introduces_no_new_runtime_h0_gate``,
    断言 ``signed_rule_ids() == frozenset()``(「本窗一条硬门都不引入」)。
    窗C 确实引入了一条 —— ``defgeo.publish.legal_catalog``(H0-LEGAL,
    §11.1/§11.3 第五步:发布 worker 外调前对冻结正文再验一次广告法)。

    但**不是**把断言删掉换成「允许有一条」就完事。真正要守的东西是
    §1.1「本规格不能自我签名」:危险的不是「多了一条硬门」,
    而是「实现方在自己的代码里给自己盖章」。所以判据下移到那一格 ——
    ``_SIGNED``(源码里手写 ``owner_signed=True`` 的静态字面量)必须恒空。
    新硬门只能走派生签发,签名来自别处 Owner 真签过的载体。

    ⚠️ 这条恒空不因窗C 的新增而放松:窗C 的那一条走的是 ``_DERIVED``。
    """
    assert statically_signed_rule_ids() == frozenset(), (
        f"源码里出现了静态已签硬门 {sorted(statically_signed_rule_ids())} —— "
        "那是实现方给自己盖章(§1.1)。硬门的签名必须来自 Owner 真签过的载体,"
        "走 _DERIVED 派生解析器,不是往 _SIGNED 里手写一个 True。"
    )
    assert signed_rule_ids() == derived_rule_ids(), (
        "当前生效的签发集合应当全部来自派生签发;出现别的来源必须先说清是谁签的。"
    )


def test_this_window_introduces_exactly_one_derived_h0_gate_with_owner_evidence():
    """窗C 新增的 H0 硬门**恰好**是这一条,且 Owner 签发证据当场可查。

    多一条 = 有人又悄悄加了个硬门;少一条 = 发布链的法律门掉了(降级 advisory,
    §11.1 的「外调前再验一次」形同虚设)。两向都要红。
    """
    expected = {"defgeo.publish.legal_catalog"}
    assert set(derived_candidate_ids()) == expected, (
        f"派生解析器登记面实得 {sorted(derived_candidate_ids())},期望 {sorted(expected)}"
    )
    assert set(derived_rule_ids()) == expected, (
        f"当前派生成立的硬门实得 {sorted(derived_rule_ids())},期望 {sorted(expected)}。"
        "少了 = 法律包未签/回落内嵌兜底;多了 = 新增硬门没同步本判据。"
    )

    # ── Owner 签发证据(不是「我说签了」,是把签名那一行读出来钉住)──────────
    from services.marketing.guards import legal_pack_info

    info = legal_pack_info()
    assert info["source"] == "config", (
        f"法律包来源实得 {info['source']!r} —— 只有 config 那份带 Owner 签名,"
        "embedded_fallback 是未签的内嵌兜底,无权硬拦"
    )
    signed = info["owner_signed"]
    assert signed.get("signed_by") == "Owner"
    assert signed.get("signed_at") == "2026-07-23"
    assert signed.get("source") == "GEO_COMMERCIAL_INTENT_GOVERNANCE_SSOT_2026-07-23.md"

    # 该规则被 enforce 时必须完整(六项体检),且类别确实是 H0-LEGAL。
    rule = enforce_h0("defgeo.publish.legal_catalog")
    assert rule.h0_class == "H0-LEGAL"
    assert rule.rule_version == info["version"], (
        "规则版本必须逐值等于签发载体的版本 —— 否则升级法律包时旧 sidecar 会按新语义执行"
    )
    assert set(rule.test_ids) == {"MED-11", "MED-18"}
    assert rule.exits == ("repair_legal_passage",), "硬门必须有出口,否则是死门"

    # census 必须把「这条是派生来的」如实标出来,不能混进 static 里对不上账。
    data = census()
    assert data["statically_signed"] == []
    assert data["derived_signed"] == ["defgeo.publish.legal_catalog"]
    assert data["signed"]["defgeo.publish.legal_catalog"]["signature_origin"] == "derived"


def test_unsigning_the_pack_revokes_the_gate_and_falls_back_to_advisory(monkeypatch):
    """🔴 反向锁:把签发载体的 ``owner_signed`` 打空,硬门必须**当场失效**。

    没有这一条,上面那条「派生签发成立」可能只是因为 ``_resolve_*`` 恒返回一个
    对象 —— 那样签名就还是个摆设,只是从 ``_SIGNED`` 挪到了函数体里。
    这条证明签发状态**真的**被每次读出来:签名没了,``enforce_h0`` 必抛,
    且发布链的法律门自动回落 ``advisory``(扫但不拦),而不是继续硬拦。
    """
    import services.marketing.guards as guards
    from services.defensive_geo.publish import legal_gate

    # 先取正向对照:未打桩时确实是 blocking。
    assert legal_gate.gate_mode() == "blocking"

    real = guards.legal_pack_info()

    def _unsigned() -> dict:
        return {**real, "owner_signed": {}}

    monkeypatch.setattr(guards, "legal_pack_info", _unsigned)

    assert derived_rule_ids() == frozenset(), "签名打空后仍有派生签发 —— 签发状态不是实时算的"
    with pytest.raises(H0RuleNotSigned) as exc:
        enforce_h0("defgeo.publish.legal_catalog")
    assert "派生解析器" in str(exc.value), "拒绝信息要说清是签发载体没签,不是规则不存在"
    assert legal_gate.gate_mode() == "advisory", (
        "签发载体未签时法律门必须回落 advisory(§1.1 未签目录无权硬拦),不得继续 blocking"
    )


def test_falling_back_to_embedded_pack_also_revokes_the_gate(monkeypatch):
    """反向锁之二:签名还在、但包回落 ``embedded_fallback`` 时同样失效。

    分开写是因为这是**另一个**失效原因:内嵌兜底那份集合从来没经 Owner 签发,
    拿它硬拦等于用一份没人签过的词表挡住客户发布。
    """
    import services.marketing.guards as guards
    from services.defensive_geo.publish import legal_gate

    real = guards.legal_pack_info()
    monkeypatch.setattr(
        guards, "legal_pack_info", lambda: {**real, "source": "embedded_fallback"}
    )

    assert derived_rule_ids() == frozenset()
    with pytest.raises(H0RuleNotSigned):
        enforce_h0("defgeo.publish.legal_catalog")
    assert legal_gate.gate_mode() == "advisory"


def test_every_signed_rule_is_complete_or_refused():
    """已签条目逐条体检;不完整的必须被 enforce 拒绝,而不是带病放行。

    表为空时本条零圈通过 —— 所以它**不是**分母守卫,
    分母守卫是上面 `test_this_window_introduces_no_new_runtime_h0_gate`。
    """
    for rule_id in sorted(signed_rule_ids()):
        gaps = _completeness_gaps(enforce_h0(rule_id))
        assert gaps == [], f"{rule_id} 缺 {gaps} 却被放行"


# ── 判别力自证:构造带病规则,证明六项体检真的会挑出毛病 ──────────────────
def _rule(**overrides) -> H0Rule:
    base = dict(
        rule_id="defgeo.test.probe",
        rule_version="v1",
        h0_class="H0-DATA",
        intercepts="拦截:对已冻结报价快照的原地 UPDATE",
        exits=("创建新快照并重新让客户确认",),
        test_ids=("REV-01",),
        owner_signed=True,
    )
    base.update(overrides)
    return H0Rule(**base)


def test_a_fully_formed_rule_has_no_gaps():
    """反向对照 —— 没有这条,下面的『缺项被抓出』可能只是因为体检恒报错。"""
    assert _completeness_gaps(_rule()) == []


@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("rule_id", "   "),
        ("rule_version", ""),
        ("h0_class", "H1"),          # 非 H0 类别不许混进 H0 目录
        ("h0_class", "H0-MADEUP"),
        ("intercepts", ""),
        ("exits", ()),               # 没有出口 = 死门
        ("exits", ("",)),            # 只有空 label 也算没出口
        ("test_ids", ()),            # 没判据的硬门 = 没人能证明它会拦
        ("test_ids", ("",)),
        ("owner_signed", False),     # 🔴 本规格不能自我签名
    ],
)
def test_each_missing_field_is_detected(field, bad_value):
    """ACT-12 六项逐项:缺任一项都必须被挑出来。"""
    gaps = _completeness_gaps(_rule(**{field: bad_value}))
    assert gaps, f"{field}={bad_value!r} 没被体检挑出 —— 带病规则会被当硬门放行"
    assert field in gaps, f"体检报了 {gaps},但没点名 {field}"


def test_census_export_is_machine_readable_and_self_consistent():
    """G-1:census 是分母来源,必须自洽 —— 计数与集合不许对不上。"""
    data = census()
    assert data["signed_count"] == len(data["signed"]) == len(signed_rule_ids())
    assert data["pending_count"] == len(data["pending"]) == len(pending_rule_ids())
    for item in data["pending"]:
        assert item["rule_id"] and item["note"], "待签条目必须写清是什么,否则对账时没人看得懂"
