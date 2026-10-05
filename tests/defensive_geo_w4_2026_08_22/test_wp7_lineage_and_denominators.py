"""WP7 纯域判据 —— §6.1 身份 / §6.3 分母 / attempt 账本 / §13.1 投影。

判据纪律(每一条都成对):每个「必须命中」配一个「必须不命中」;
锁都能拆红 —— 每条判据后面的注释写明**把哪一行改成什么**会让它转红。
"""

from __future__ import annotations

import pytest

from services.defensive_geo.monitoring import attempt_ledger as AL
from services.defensive_geo.monitoring import attribution as ATTR
from services.defensive_geo.monitoring import defensive_projection as DP
from services.defensive_geo.monitoring import denominators as DEN
from services.defensive_geo.monitoring import enrollment as ENR
from services.defensive_geo.monitoring import lineage as LIN
from services.defensive_geo.monitoring import renewal as REN
from services.defensive_geo.monitoring import scenario_coverage as SC
from services.defensive_geo.monitoring import snapshot as SNAP
from services.defensive_geo.monitoring import source_consistency as SRC



# ── 资金 sink 探测器(三种 import 形态全覆盖)─────────────────────────────
#: 顶层资金模块。判据按**模块路径前缀**判,不按整串相等 ——
#: ``from middleware import billing`` 的 AST 里 module 只有 ``middleware``,
#: 名字在 ``names`` 里,整串相等会漏掉它(MUT-13 实测存活)。
_FUNDING_SINK_PATHS: tuple[tuple[str, ...], ...] = (
    ("middleware", "billing"),
    ("db", "wallet_db"),
    ("services", "organization_billing"),
)


def _funding_sinks_in_source(tree) -> set[str]:
    import ast

    hits: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = tuple(alias.name.split("."))
                for sink in _FUNDING_SINK_PATHS:
                    if parts[:len(sink)] == sink:
                        hits.add(".".join(sink))
        elif isinstance(node, ast.ImportFrom):
            base = tuple((node.module or "").split(".")) if node.module else ()
            for alias in node.names:
                full = base + tuple(alias.name.split("."))
                for sink in _FUNDING_SINK_PATHS:
                    # 三种形态统一成"完整点分路径是否以 sink 开头"
                    if full[:len(sink)] == sink or base[:len(sink)] == sink:
                        hits.add(".".join(sink))
    return hits


def _funding_sinks_in(module) -> set[str]:
    import ast
    import inspect

    return _funding_sinks_in_source(ast.parse(inspect.getsource(module)))


# 一份合法的 plan cell 入参。判据从它出发做**单点变异**,
# 这样"是哪一格出的问题"永远唯一。
def _plan_fields(**over):
    base = dict(
        run_authority_id="mcr_001",
        tenant_owner_id=7,
        service_projection_id=None,
        service_projection_version=None,
        plan_item_key=None,
        question_set_revision="qs-v1",
        question_identity_key="q_" + "a" * 32,
        question_revision=1,
        family="identity_check",
        public_platform="doubao",
        planned_surface="ai_search",
        search_mode="ai_search",
        scheduled_window=None,
        run_index=1,
        route_plan_revision="rp-v1",
    )
    base.update(over)
    return base


def _attempt_fields(**over):
    base = dict(
        plan_cell_id=LIN.plan_cell_id(**_plan_fields()),
        attempt_ordinal=1,
        actual_provider="doubao",
        actual_model="doubao-pro",
        actual_model_revision="2026-08",
        actual_surface="ai_search",
        actual_search_mode="ai_search",
        request_hash="rh-1",
    )
    base.update(over)
    return base


# ══════════════════════════════════════════════════════════════════════
# §6.1 身份
# ══════════════════════════════════════════════════════════════════════

class TestPlanCellIdentity:
    def test_same_inputs_same_id(self):
        assert LIN.plan_cell_id(**_plan_fields()) == LIN.plan_cell_id(**_plan_fields())

    @pytest.mark.parametrize("field", LIN.PLAN_CELL_FIELDS)
    def test_every_formula_field_changes_the_id(self, field):
        """分母 = ``PLAN_CELL_FIELDS`` **机械遍历**,不手抄。

        拆红方式:把 ``_digest`` 的 for 循环改成只遍历前 3 个字段 ——
        第 4 个起的每个 param 立刻红。
        """
        base = LIN.plan_cell_id(**_plan_fields())
        original = _plan_fields()[field]
        mutated = "MUTATED" if not isinstance(original, int) else original + 99
        if field == "question_identity_key":
            mutated = "q_" + "b" * 32
        assert LIN.plan_cell_id(**_plan_fields(**{field: mutated})) != base, (
            f"{field} 变了但 plan_cell_id 没变 —— 该字段没进 hash"
        )

    def test_null_and_empty_string_are_not_the_same(self):
        """§6.1「显式编码为 null」:空串**不是** null,而且空串直接拒。

        拆红:把 ``_require_fields`` 里 ``if not v: raise`` 那段删掉 ——
        本判据的 ``pytest.raises`` 立刻红。
        """
        with pytest.raises(LIN.LineageIdentityError, match="空串不是 null"):
            LIN.plan_cell_id(**_plan_fields(service_projection_id=""))

    def test_non_nullable_field_rejects_none(self):
        with pytest.raises(LIN.LineageIdentityError, match="不允许为 null"):
            LIN.plan_cell_id(**_plan_fields(public_platform=None))

    def test_missing_and_extra_fields_both_rejected(self):
        fields = _plan_fields()
        fields.pop("run_index")
        with pytest.raises(LIN.LineageIdentityError, match="缺字段"):
            LIN.plan_cell_id(**fields)
        with pytest.raises(LIN.LineageIdentityError, match="公式外字段"):
            LIN.plan_cell_id(**_plan_fields(), surprise="x")

    def test_legacy_question_key_shape_is_refused(self):
        """§0.5.1 第 7 条:现役 ``question_key``(裸 64 hex)传进来必须炸。

        这是「同名异义」的运行时门。拆红:删掉
        ``assert_not_legacy_question_key`` 里那个 ``_LEGACY_QUESTION_KEY_SHAPE`` 分支。
        """
        with pytest.raises(LIN.LineageIdentityError, match="同名异义|geo_article_target"):
            LIN.plan_cell_id(**_plan_fields(question_identity_key="f" * 64))

    def test_valid_identity_key_shape_is_accepted(self):
        """成对的正样本:合法形态必须**不**命中(证明上一条不是恒抛)。"""
        assert LIN.plan_cell_id(**_plan_fields(question_identity_key="q_" + "0" * 32))

    def test_no_delimiter_injection(self):
        """长度前缀 framing:``a|b`` 与 ``a`` + ``|b`` 必须是不同身份。

        拆红:把 ``_digest`` 改成用 ``"|".join(...)`` 拼接 —— 本判据立刻红。
        """
        left = LIN.plan_cell_id(**_plan_fields(family="ab", public_platform="cd"))
        right = LIN.plan_cell_id(**_plan_fields(family="a", public_platform="bcd"))
        assert left != right


class TestAttemptIdentity:
    def test_observation_cell_id_equals_attempt_id(self):
        """§6.1 逐字。写成别名而不是约定 —— 判据钉这一位。"""
        f = _attempt_fields()
        assert LIN.observation_cell_id(**f) == LIN.attempt_id(**f)

    def test_ordinal_starts_at_one(self):
        with pytest.raises(LIN.LineageIdentityError, match="attempt_ordinal"):
            LIN.attempt_id(**_attempt_fields(attempt_ordinal=0))

    @pytest.mark.parametrize("field", LIN.ATTEMPT_FIELDS)
    def test_every_attempt_field_changes_the_id(self, field):
        base = LIN.attempt_id(**_attempt_fields())
        original = _attempt_fields()[field]
        mutated = original + 7 if isinstance(original, int) else "MUTATED"
        assert LIN.attempt_id(**_attempt_fields(**{field: mutated})) != base

    def test_model_revision_null_is_allowed_and_distinct(self):
        """平台不给版本时 NULL 合法,且与任何字符串不同身份。"""
        null_id = LIN.attempt_id(**_attempt_fields(actual_model_revision=None))
        str_id = LIN.attempt_id(**_attempt_fields(actual_model_revision="unknown"))
        assert null_id != str_id

    def test_projection_id_changes_on_classifier_upgrade(self):
        """MON-04 的身份基础:分类器升级 ⇒ 新 projection id。"""
        common = dict(observation_cell_id="oc1", resolver_version="r1",
                      evidence_extractor_version="e1")
        assert (LIN.projection_id(classifier_version="c1", **common)
                != LIN.projection_id(classifier_version="c2", **common))


class TestEquivalentFallback:
    def test_same_platform_surface_is_equivalent(self):
        assert LIN.equivalent_fallback("ai_search", "doubao", "ai_search", "doubao")

    def test_changed_public_platform_needs_new_plan_revision(self):
        """§6.1:改了对用户有意义的公开平台 ⇒ **不是**等价 fallback。"""
        with pytest.raises(LIN.LineageIdentityError, match="不是等价 fallback"):
            LIN.assert_equivalent_fallback(
                planned_surface="ai_search", planned_platform="doubao",
                actual_surface="ai_search", actual_platform="kimi")


# ══════════════════════════════════════════════════════════════════════
# §6.3 分母
# ══════════════════════════════════════════════════════════════════════

class TestDenominators:
    def test_registry_units_are_all_registered(self):
        """G-2:分母 = registry 机械遍历。任何 key 的单位必须在 UNITS 里。"""
        for key in DEN.REGISTRY_KEYS:
            assert DEN.unit_of(key) in DEN.UNITS, key

    def test_zero_denominator_is_never_zero_percent(self):
        """§6.3 / MET-13 承重点。

        拆红:把 ``ratio`` 里 ``denominator == 0`` 那支改成
        ``return ... 0.0, "measured"`` —— 本判据立刻红。
        """
        r = DEN.ratio(numerator_label="x", denominator_key="valid_samples",
                      numerator=0, denominator=0)
        assert r.value is None
        assert r.status == "no_denominator"

    def test_nonzero_denominator_produces_a_value(self):
        """成对正样本:证明上一条不是"永远返 None"。"""
        r = DEN.ratio(numerator_label="x", denominator_key="valid_samples",
                      numerator=3, denominator=4)
        assert r.value == pytest.approx(0.75)
        assert r.status == "measured"

    def test_numerator_cannot_exceed_denominator(self):
        with pytest.raises(DEN.DenominatorError, match="分子"):
            DEN.ratio(numerator_label="x", denominator_key="valid_samples",
                      numerator=5, denominator=4)

    def test_attempt_records_cannot_be_a_rate_denominator(self):
        """§6.3 逐字「不直接作为品牌率分母」——重试越多品牌率越低。"""
        with pytest.raises(DEN.DenominatorError, match="attempt"):
            DEN.ratio(numerator_label="x", denominator_key="attempt_records",
                      numerator=1, denominator=2)

    def test_cross_unit_cohort_ratio_is_refused(self):
        """唯一能抓住"claim 数除以 cell 数"的门。两边都是整数,别的检查全绿。"""
        with pytest.raises(DEN.DenominatorError, match="计量单位混算"):
            DEN.cohort_ratio(numerator_key="answer_claims",
                             denominator_key="valid_samples",
                             numerator=1, denominator=2)

    def test_same_unit_cohort_ratio_is_accepted(self):
        r = DEN.cohort_ratio(numerator_key="covered_families",
                             denominator_key="measured_families",
                             numerator=1, denominator=2)
        assert r.unit == DEN.UNIT_QUESTION_FAMILY

    def test_attempted_conservation_catches_a_swallowed_cell(self):
        """MON-01/02:失败格被删 / ambiguous 被吞 ⇒ 等式当场不成立。"""
        DEN.assert_attempted_conservation(
            attempted_cells=10, canonical_outcome_cells=7,
            ambiguous_cells=2, final_engine_error_cells=1)
        with pytest.raises(DEN.DenominatorError, match="守恒破裂"):
            DEN.assert_attempted_conservation(
                attempted_cells=10, canonical_outcome_cells=7,
                ambiguous_cells=0, final_engine_error_cells=1)


# ══════════════════════════════════════════════════════════════════════
# attempt 账本(纯逻辑部分;库层三条承重约束在 PG16 判据里)
# ══════════════════════════════════════════════════════════════════════

def _rec(ordinal, state, *, error=None, provider_called=True):
    return AL.AttemptRecord(
        attempt_id=f"a{ordinal}", plan_cell_id="pc", attempt_ordinal=ordinal,
        parent_attempt_id=None, terminal_state=state, error_code=error,
        provider_called=provider_called, monitoring_result_id=None,
        actual_provider="p", actual_model="m", actual_model_revision=None,
        actual_surface="s", actual_search_mode="sm")


class TestAttemptLedger:
    def test_engine_error_cannot_be_recorded_as_not_mentioned(self):
        """MON-02 承重点:401/429/超时不得被压成「未提及」。

        拆红:删掉 ``assert_error_not_absent`` 里第一段 if。
        """
        with pytest.raises(AL.AttemptLedgerError, match="不能被压成"):
            AL.assert_error_not_absent("not_mentioned", "RATE_LIMITED")

    def test_engine_error_requires_a_code(self):
        with pytest.raises(AL.AttemptLedgerError, match="必须带 error_code"):
            AL.assert_error_not_absent("engine_error", None)

    def test_legitimate_answered_without_error_passes(self):
        """成对正样本。"""
        AL.assert_error_not_absent("answered", None)

    def test_fallback_success_keeps_the_first_attempt_error(self):
        """MON-10 逐字:首个 attempt engine-error、fallback 成功 ⇒
        final cell 可 answered,但 **attempt error 必须还在**。

        拆红:把 ``cell_denominator_facts`` 的 ``attemptErrors`` 改成
        只收 canonical attempt 的 error —— 本判据立刻红。
        """
        attempts = [_rec(1, "engine_error", error="TIMEOUT"), _rec(2, "answered")]
        facts = AL.cell_denominator_facts(attempts)
        assert facts["canonicalState"] == "answered"
        assert facts["attemptRecords"] == 2
        assert facts["attemptErrors"] == [
            {"attemptOrdinal": 1, "errorCode": "TIMEOUT"}]

    def test_policy_skipped_is_terminal_but_not_attempted(self):
        """MON-11 逐字:skipped 无 provider attempt、计 terminal 不计 attempted。"""
        facts = AL.cell_denominator_facts(
            [_rec(1, "policy_skipped", provider_called=False)])
        assert facts["isTerminal"] is True
        assert facts["isAttempted"] is False
        assert facts["providerAttempts"] == 0

    def test_canonical_selection_prefers_answer_then_lowest_ordinal(self):
        attempts = [_rec(1, "engine_error", error="TIMEOUT"),
                    _rec(2, "answered"), _rec(3, "answered")]
        assert AL.canonical_attempt(attempts).attempt_ordinal == 2

    def test_no_terminal_attempt_yields_none(self):
        assert AL.canonical_attempt([_rec(1, None)]) is None

    def test_unknown_terminal_state_is_refused(self):
        with pytest.raises(AL.AttemptLedgerError, match="未知终态"):
            AL._assert_terminal("mostly_fine")


# ══════════════════════════════════════════════════════════════════════
# §13.1 防御投影
# ══════════════════════════════════════════════════════════════════════

class TestDefensiveProjection:
    def test_corroborated_with_conflict_is_legal(self):
        """MET-31 逐字:``corroborated + hasConflict=true`` **合法并存**。

        拆红:在 ``assert_orthogonal`` 里加一句
        ``if has_conflict: support_level = 'unsupported'`` —— 本判据立刻红。
        """
        DP.assert_orthogonal("corroborated", True)

    @pytest.mark.parametrize("merged", sorted(DP.FORBIDDEN_MERGED_SUPPORT_LEVELS))
    def test_merged_support_level_is_refused(self, merged):
        """§13.1「禁止复活 unsupported_or_conflicting 合并态」。分母机械遍历。"""
        with pytest.raises(DP.DefensiveProjectionError, match="合并态"):
            DP.assert_orthogonal(merged, False)

    def test_conflict_flag_and_facts_must_agree(self):
        with pytest.raises(DP.DefensiveProjectionError, match="conflictingFacts 为空"):
            DP.assert_conflict_facts_consistent(has_conflict=True, conflicting_facts=[])
        with pytest.raises(DP.DefensiveProjectionError, match="两者必须一致"):
            DP.assert_conflict_facts_consistent(has_conflict=False,
                                                conflicting_facts=["x"])
        DP.assert_conflict_facts_consistent(has_conflict=True, conflicting_facts=["x"])

    def test_ai_adjudication_requires_model_and_version(self):
        """R-1 留痕:没有版本号的留痕在模型换代后无法复现。"""
        DP.assert_adjudication_traceable("ai_adjudicated", "ai:qwen3-max@2026-08")
        for bad in ("ai", "auto", "ai:qwen3-max", "human:abc"):
            with pytest.raises(DP.DefensiveProjectionError):
                DP.assert_adjudication_traceable("ai_adjudicated", bad)

    def test_content_conflict_defaults_to_ai_not_human(self):
        """R-1(§0.5.2):内容/判断类**默认交 AI**,不默认转人工。

        拆红:把 ``route_low_confidence`` 的最后一行改成
        ``return "pending_review"`` —— 本判据立刻红。
        """
        assert DP.route_low_confidence(conflict_kind="content", ai_available=True) \
            == "ai_adjudicated"

    def test_funding_and_identity_conflicts_stay_human(self):
        for kind in DP.HUMAN_ONLY_CONFLICT_KINDS:
            assert DP.route_low_confidence(conflict_kind=kind, ai_available=True) \
                == "pending_review"

    def test_ai_unavailable_fails_closed_not_open(self):
        """fail-closed 的方向是「停在待审」,不是「猜一个」。"""
        assert DP.route_low_confidence(conflict_kind="content", ai_available=False) \
            == "pending_review"


# ══════════════════════════════════════════════════════════════════════
# §7.3 场景覆盖
# ══════════════════════════════════════════════════════════════════════

class TestScenarioCoverage:
    def _policy(self):
        return SC.signed_policy("recommendation", required_platforms=["doubao"],
                                family_valid_minimum=2, minimum_positive_count=1,
                                minimum_positive_rate=0.5)

    def test_recommendation_positive_set_excludes_mentioned_only(self):
        """MET-02 / MET-38:「只是提到」不是推荐。"""
        assert "mentioned_only" not in self._policy().positive_outcomes

    def test_smuggling_mentioned_only_into_recommendation_is_refused(self):
        """拆红:把 ``assert_positive_set_signed`` 的比较改成 ``issubset`` ——
        本判据立刻红。"""
        with pytest.raises(SC.CoveragePolicyError, match="与签发值不符"):
            SC.assert_positive_set_signed(
                "recommendation",
                frozenset({"recommended", "conditionally_recommended", "mentioned_only"}))

    def test_planned_denominator_must_come_from_frozen_plan(self):
        """§7.3:用实采数当分母 ⇒ 平台挂了覆盖率反而涨。"""
        with pytest.raises(SC.CoveragePolicyError, match="计划分母"):
            SC.assert_planned_is_frozen(2, ["f1", "f2", "f3"])

    def test_three_denominators_are_monotonic(self):
        result = SC.evaluate(
            policy=self._policy(),
            frozen_family_keys=["f1", "f2", "f3"],
            per_family_cells={
                "f1": [{"platform": "doubao", "outcome": "recommended", "valid": True},
                       {"platform": "kimi", "outcome": "recommended", "valid": True}],
                "f2": [{"platform": "doubao", "outcome": "mentioned_only", "valid": True},
                       {"platform": "kimi", "outcome": "not_mentioned", "valid": True}],
                # f3 一格都没采到 ⇒ 不进 measured,但**仍在 planned 里**
            })
        assert (result.planned_families, result.measured_families,
                result.covered_families) == (3, 2, 1)

    def test_zero_measured_gives_not_measured_not_zero_percent(self):
        result = SC.evaluate(policy=self._policy(),
                             frozen_family_keys=["f1"], per_family_cells={})
        assert result.status == "not_measured"
        assert SC.as_ratio(result).value is None

    def test_empty_required_platforms_is_refused(self):
        """空 required set 会让平台条件恒真 —— 恒真的条件不是条件。"""
        with pytest.raises(SC.CoveragePolicyError, match="required_platforms 为空"):
            SC.signed_policy("recommendation", required_platforms=[],
                             family_valid_minimum=2, minimum_positive_count=1,
                             minimum_positive_rate=0.5)


# ══════════════════════════════════════════════════════════════════════
# §7.5 归因 —— MET-32 的 10/2/1 夹具
# ══════════════════════════════════════════════════════════════════════

def _claim(key, *, observable=True, mapped=False, url=False, body=False,
           support="unknown", sources=0):
    return ATTR.ClaimAttribution(
        claim_key=key, evidence_cell_ref="ec1", is_source_observable=observable,
        has_explicit_mapping=mapped, has_url_proof=url, has_body_proof=body,
        support_level=support, has_conflict=False, support_source_count=sources)


def _fixture_10_2_1():
    claims = [_claim(f"c{i}") for i in range(1, 9)]          # 8 条无映射
    claims.append(_claim("c9", mapped=True))                  # 有映射无 proof
    claims.append(_claim("c10", mapped=True, url=True, body=True))  # 有映射有 proof
    return claims


class TestAttribution:
    def test_met32_fixture_gives_one_over_ten_and_one_over_two(self):
        """MET-32 / §20.5 逐字。

        拆红:把 ``coverage`` 的分子从 ``len(verified)`` 改成 ``len(mapped)``
        —— 主覆盖率变成 2/10,本判据立刻红。
        """
        m = ATTR.attribution_metrics(_fixture_10_2_1())
        ATTR.assert_met32_fixture(m)
        assert (m.coverage.numerator, m.coverage.denominator) == (1, 10)
        assert (m.proof_rate.numerator, m.proof_rate.denominator) == (1, 2)

    def test_deleting_the_eight_unmapped_claims_is_caught(self):
        """MET-32 逐字点名的作弊:「删除 8 个无映射 claims」。"""
        m = ATTR.attribution_metrics(_fixture_10_2_1()[-2:])
        with pytest.raises(ATTR.AttributionError, match="不是 10/2/1 夹具"):
            ATTR.assert_met32_fixture(m)

    def test_mapped_without_proof_is_not_explicit_attribution(self):
        """MET-11:无 URL/body proof 不得标 explicit。"""
        assert _claim("x", mapped=True).attribution_state == "unknown"
        assert _claim("x", mapped=True, url=True, body=True).attribution_state \
            == "explicit_citation"

    def test_unobservable_claims_leave_the_denominator(self):
        """``source_observable_samples``:平台本身不暴露来源时才 not-applicable。"""
        m = ATTR.attribution_metrics([_claim("x", observable=False)])
        assert m.eligible_claims == 0
        assert m.coverage.value is None      # 零分母 ⇒ null,不是 0%

    def test_corroborated_requires_a_source(self):
        with pytest.raises(ATTR.AttributionError, match="corroborated"):
            ATTR.attribution_metrics([_claim("x", support="corroborated", sources=0)])

    def test_customer_copy_cannot_claim_explicit_citation_without_proof(self):
        """§7.5 对客文案门 —— 这是我们**控制得了**的那一类风险。"""
        claim = _claim("x", mapped=True)      # 有映射,无 proof
        with pytest.raises(ATTR.AttributionError, match="明确引用"):
            ATTR.assert_no_explicit_claim_without_proof(claim, "AI 明确引用了你们官网")
        # 成对正样本:有 proof 时同一句话放行
        ATTR.assert_no_explicit_claim_without_proof(
            _claim("y", mapped=True, url=True, body=True), "AI 明确引用了你们官网")

    def test_no_causal_claim_without_source(self):
        """POR-08:无显式来源时只说观察变化,不说由发布导致。"""
        with pytest.raises(ATTR.AttributionError, match="因果"):
            ATTR.assert_no_causal_claim_without_source(
                has_explicit_source=False, customer_copy="这次提升是由这篇文章带来的")
        ATTR.assert_no_causal_claim_without_source(
            has_explicit_source=False, customer_copy="本期观察到提及次数增加")


# ══════════════════════════════════════════════════════════════════════
# §7.4 信源一致性
# ══════════════════════════════════════════════════════════════════════

def _obs(field, value, fingerprint):
    return SRC.FactObservation(
        fact_field=field, value=value, source_url=f"https://x/{fingerprint}",
        captured_snapshot_ref="snap1", as_of="2026-08-01T00:00:00Z",
        normalizer_version="n1", extractor_version="e1",
        syndication_fingerprint=fingerprint)


class TestSourceConsistency:
    def test_syndicated_copies_are_not_independent_sources(self):
        """§7.4 / MET-16:同一软文转载不算多个独立来源。

        拆红:把 ``independent_sources`` 的去重 set 删掉 —— 本判据立刻红。
        """
        obs = [_obs("address", "深圳", "fp1"), _obs("address", "深圳", "fp1"),
               _obs("address", "深圳", "fp1")]
        assert len(SRC.independent_sources(obs)) == 1

    def test_missing_is_unknown_not_conflict(self):
        """§7.4 / MET-10 逐字:「缺失不等于冲突」「unknown 不等于错误」。"""
        r = SRC.consistency_for_field(fact_field="phone", accepted_value="0755-1",
                                      observations=[])
        assert r["state"] == "unknown"
        assert r["conflictingSources"] == []

    def test_accepted_fact_survives_a_conflict(self):
        """R-4(§0.5.2):客户确认过的事实在 hasConflict 时**仍然可用**。

        拆红:在 ``consistency_for_field`` 冲突分支里把 ``acceptedValue``
        置 None —— 本判据立刻红。
        """
        r = SRC.consistency_for_field(
            fact_field="service_regions", accepted_value="深圳",
            observations=[_obs("service_regions", "广州", "fp2")])
        assert r["state"] == "conflicting"
        assert r["acceptedValue"] == "深圳"     # 没有消失

    def test_comparable_needs_two_independent_sources(self):
        one = SRC.consistency_for_field(fact_field="official_site", accepted_value="a",
                                        observations=[_obs("official_site", "a", "f1")])
        two = SRC.consistency_for_field(
            fact_field="official_site", accepted_value="a",
            observations=[_obs("official_site", "a", "f1"),
                          _obs("official_site", "a", "f2")])
        assert one["isComparable"] is False
        assert two["isComparable"] is True

    def test_evidence_gate_cannot_be_used_by_the_writing_chain(self):
        """R-3:证据门**仅限**信源一致性测量轴,不得外溢到写作链。"""
        with pytest.raises(SRC.SourceConsistencyError, match="不设"):
            SRC.assert_not_used_as_writing_gate("writing/ranking_prompt_v9.py")
        SRC.assert_not_used_as_writing_gate("services/defensive_geo/monitoring/x.py")

    def test_misidentified_is_not_collapsed_into_engine_error(self):
        """MET-07 / MET-26。"""
        with pytest.raises(SRC.SourceConsistencyError, match="不是引擎故障"):
            SRC.assert_entity_state_not_collapsed(
                entity_state="misidentified", projected_outcome="engine_error")
        # 成对正样本:落 not_mentioned 是 §7.4 明说可以的
        SRC.assert_entity_state_not_collapsed(
            entity_state="misidentified", projected_outcome="not_mentioned")

    def test_provenance_five_fields_are_all_required(self):
        bad = SRC.FactObservation(
            fact_field="phone", value="1", source_url="", captured_snapshot_ref="s",
            as_of="2026-08-01T00:00:00Z", normalizer_version="n",
            extractor_version="e", syndication_fingerprint="f")
        with pytest.raises(SRC.SourceConsistencyError, match="溯源字段"):
            SRC.assert_provenance_complete(bad)


# ══════════════════════════════════════════════════════════════════════
# §13.3 快照 / §13.4 续费 / §15.9.3 admission
# ══════════════════════════════════════════════════════════════════════

def _frozen(**over):
    base = {
        "plan_snapshot_id": "ps1", "plan_snapshot_hash": "h" * 64,
        "sampling_window_start": "2026-08-01T00:00:00Z",
        "sampling_window_end": "2026-08-02T00:00:00Z",
        "cutoff_at": "2026-08-02T00:00:00Z", "input_watermark": "wm1",
        "raw_result_ids": [1, 2, 3], "entity_resolver_version": "r1",
        "outcome_classifier_version": "c1", "evidence_extractor_version": "e1",
        "metric_definition_version": "m1",
    }
    base.update(over)
    return base


class TestSnapshot:
    @pytest.mark.parametrize("field", SNAP.FROZEN_FIELDS)
    def test_every_frozen_field_changes_the_hash(self, field):
        """MON-06 / POR-05:改任一冻结叶子必改 hash。分母机械遍历。"""
        base = SNAP.content_hash(_frozen())
        original = _frozen()[field]
        mutated = original + [99] if isinstance(original, list) else str(original) + "X"
        assert SNAP.content_hash(_frozen(**{field: mutated})) != base, field

    def test_missing_frozen_field_is_refused(self):
        f = _frozen()
        f.pop("input_watermark")
        with pytest.raises(SNAP.SnapshotError, match="缺冻结项"):
            SNAP.content_hash(f)

    def test_processing_snapshot_cannot_be_bound_to_customer(self):
        """POR-15:非终态不许签客户 token / 出 PDF。"""
        snap = SNAP.build(report_snapshot_id="rs1", revision=1, state="processing",
                          frozen=_frozen())
        with pytest.raises(SNAP.SnapshotError, match="不可绑定"):
            SNAP.assert_customer_bindable(snap)
        SNAP.assert_customer_bindable(
            SNAP.build(report_snapshot_id="rs1", revision=2, state="partial",
                       frozen=_frozen()))

    def test_in_place_state_change_on_same_hash_is_refused(self):
        with pytest.raises(SNAP.SnapshotError, match="原地"):
            SNAP.assert_no_in_place_state_change(
                existing_state="processing", existing_hash="h1",
                new_state="ready", new_hash="h1")
        # 成对:换了 hash(= 新 revision 的内容)则放行
        SNAP.assert_no_in_place_state_change(
            existing_state="processing", existing_hash="h1",
            new_state="ready", new_hash="h2")

    def test_live_card_must_carry_as_of(self):
        """§13.3 点名的病灶:实时卡没有清楚标时间。"""
        with pytest.raises(SNAP.SnapshotError, match="as_of"):
            SNAP.live_view(as_of="", updated_at="2026-08-22T00:00:00Z", aggregate={})

    def test_stale_live_card_needs_a_reason(self):
        """Z-2.1:不白屏、不显示 0 —— 保留上次快照就必须说清为什么是旧的。"""
        with pytest.raises(SNAP.SnapshotError, match="reason"):
            SNAP.live_view(as_of="a", updated_at="b", aggregate={}, is_stale=True)

    def test_live_fields_cannot_leak_into_the_frozen_surface(self):
        with pytest.raises(SNAP.SnapshotError, match="实时监测字段"):
            SNAP.assert_live_not_in_frozen_surface(
                {"cards": [{"liveMonitoring": {"count": 3}}]})
        SNAP.assert_live_not_in_frozen_surface({"cards": [{"identity": {}}]})

    def test_late_result_cannot_backwrite_a_signed_snapshot(self):
        with pytest.raises(SNAP.SnapshotError, match="迟到结果"):
            SNAP.assert_late_result_not_backwritten(
                snapshot_revision=2, late_result_revision=2)
        SNAP.assert_late_result_not_backwritten(
            snapshot_revision=2, late_result_revision=3)


def _signals(direction="improved"):
    return [REN.SignalReading(signal=s, direction=direction,
                              evidence_refs=("ev1",), explanation_key=f"k_{s}")
            for s in REN.RENEWAL_SIGNALS]


class TestRenewal:
    def test_all_six_signals_are_required(self):
        """§13.4 六维。分母 = ``RENEWAL_SIGNALS`` 机械遍历。"""
        with pytest.raises(REN.RenewalError, match="缺维度"):
            REN.build(signals=_signals()[:5], basis_report_snapshot_id="rs1",
                      comparability_level="full")

    def test_direction_claims_need_evidence(self):
        bare = [REN.SignalReading(signal=s, direction="improved", evidence_refs=(),
                                  explanation_key="k") for s in REN.RENEWAL_SIGNALS]
        with pytest.raises(REN.RenewalError, match="没有证据"):
            REN.build(signals=bare, basis_report_snapshot_id="rs1",
                      comparability_level="full")

    def test_non_comparable_cannot_conclude_improvement(self):
        """§13.2「不可比时不算涨跌」的下游。

        拆红:把 ``build`` 里 ``comparability_level == "none"`` 那支删掉 ——
        本判据立刻红。
        """
        rec = REN.build(signals=_signals(), basis_report_snapshot_id="rs1",
                        comparability_level="none")
        assert rec.level == "insufficient_basis"

    def test_next_action_is_always_a_new_customer_snapshot(self):
        """§13.4 末句:续费仍是**新商业快照**,不自动扣费。"""
        rec = REN.build(signals=_signals(), basis_report_snapshot_id="rs1",
                        comparability_level="full")
        assert rec.next_action_kind == "new_customer_snapshot"

    @pytest.mark.parametrize("marker", REN.FORBIDDEN_AUTO_CHARGE_MARKERS)
    def test_auto_charge_markers_are_refused(self, marker):
        with pytest.raises(REN.RenewalError, match="自动扣费"):
            REN.assert_no_auto_charge({"summary": f"下期将 {marker} 继续"})

    def test_renewal_module_imports_no_funding_sink(self):
        """「不自动扣费」的**接线**证明,不是"我们没写"。

        🔴 第一版这条锁**只认** ``from middleware.billing import X`` 一种形态,
           MUT-13 用 ``from middleware import billing`` 当场存活。
           同一种病本仓记过(census 裸符号名 / 三种 import 形态)。
           现在三种形态全覆盖,并由
           ``test_funding_sink_detector_catches_all_three_import_forms``
           做**活性自证** —— 没有那条自证,这条锁下次还会静默漏。
        """
        found = _funding_sinks_in(REN)
        assert not found, f"续费模块引入了资金 sink:{sorted(found)}"

    def test_funding_sink_detector_catches_all_three_import_forms(self):
        """探测器活性自证:三种写法都必须被抓到。

        没有这一条,上面那条锁"全绿"既可能是真没引入,
        也可能是探测器压根看不见 —— 两者长得一模一样。
        """
        import ast

        forms = (
            "import middleware.billing",
            "from middleware.billing import freeze_points",
            "from middleware import billing",
            "from db import wallet_db",
            "import db.wallet_db as w",
        )
        for src in forms:
            assert _funding_sinks_in_source(ast.parse(src)), f"漏检:{src}"
        # 成对负样本:无关 import 必须**不**命中(证明它不是恒真)
        assert not _funding_sinks_in_source(
            ast.parse("from typing import Any\nimport hashlib"))


class TestAdmission:
    def test_legacy_is_never_blocked_by_the_new_gate(self):
        """§15.9.3 方向②:新门不得阻断 legacy(§19 变异 140 的另一半)。

        拆红:把 ``admit`` 里 ``if not is_v2_enrolled`` 那段删掉 —— 本判据立刻红。
        """
        for path in ENR.LEGACY_PATHS:
            d = ENR.admit(is_v2_enrolled=False, legacy_path=path)
            assert d.admitted is True
            assert d.reason_code is None

    def test_v2_without_activation_is_rejected_with_zero_side_effects(self):
        """§15.9.3 方向①:未激活 ⇒ 零 freeze/outbox/provider。"""
        d = ENR.admit(is_v2_enrolled=True, service_activated=False)
        assert d.admitted is False
        assert d.reason_code == "service_not_active"
        assert (d.freeze_count_expected, d.outbox_count_expected,
                d.provider_call_count_expected) == (0, 0, 0)
        ENR.assert_zero_side_effects(d, observed_freezes=0, observed_outbox=0,
                                     observed_provider_calls=0)

    def test_side_effects_after_rejection_are_caught(self):
        d = ENR.admit(is_v2_enrolled=True, service_activated=False)
        with pytest.raises(ENR.AdmissionError, match="却产生了副作用"):
            ENR.assert_zero_side_effects(d, observed_freezes=1, observed_outbox=0,
                                         observed_provider_calls=0)

    @pytest.mark.parametrize("reason", ENR.ADMISSION_REASONS)
    def test_every_reason_has_an_action(self, reason):
        """§0.5.6 铁律:任何阻塞必须自带解决方案。分母机械遍历。"""
        assert ENR.REASON_TO_ACTION.get(reason), f"{reason} 没有下一步 = 死路"

    def test_funding_pending_never_carries_a_command_id(self):
        """§19 变异 142:资金不足时创建空 command 后再标 pending。"""
        d = ENR.admit(is_v2_enrolled=True, service_activated=False)
        proj = ENR.funding_pending_projection(
            d, work_kind="continuous_monitoring", work_item_ref="w1",
            preview_or_snapshot_ref="p1", status_url="/s")
        assert proj["commandId"] is None and proj["freezeId"] is None

    def test_admitted_decision_has_no_funding_pending_projection(self):
        d = ENR.admit(is_v2_enrolled=True, service_activated=True,
                      monitoring_budget_available=True)
        with pytest.raises(ENR.AdmissionError, match="已放行"):
            ENR.funding_pending_projection(
                d, work_kind="continuous_monitoring", work_item_ref="w",
                preview_or_snapshot_ref="p", status_url="/s")
