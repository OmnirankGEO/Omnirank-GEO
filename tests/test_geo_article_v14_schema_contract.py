from services.geo_article_v14_schema_contract import (
    CONSTRAINTS,
    _constraint_definition_matches,
)
from services.article_closed_loop_schema_contract import (
    CHECK_DEFINITIONS as CLOSED_LOOP_CHECK_DEFINITIONS,
    _check_definition_matches as _closed_loop_check_definition_matches,
)


def test_review_decision_accepts_both_postgresql_16_equivalent_renderings():
    # [WP9-P0-7 ④ · Owner 裁决 D8]合法决策域 = {approved, rejected, skipped}
    # (skipped = 品牌故事/企业档案推荐人审的"明示跳过",审计留痕)。
    contract = CONSTRAINTS["ck_geo_article_review_decision"]

    assert _constraint_definition_matches(
        contract,
        "CHECK (decision::text = ANY (ARRAY['approved'::character varying, "
        "'rejected'::character varying, 'skipped'::character varying]::text[]))",
    )
    assert _constraint_definition_matches(
        contract,
        "CHECK (decision::text = ANY (ARRAY['approved'::character varying::text, "
        "'rejected'::character varying::text, 'skipped'::character varying::text]))",
    )


def test_review_decision_rejects_expanded_or_bypassed_constraint():
    # 反漂移意图不变:只认 D8 签发的这三个值 —— 第四个值(如 pending)或 OR TRUE 绕过仍必须被拒;
    # 收窄回旧的两值域(丢掉 skipped)同样属漂移,也要被拒。
    contract = CONSTRAINTS["ck_geo_article_review_decision"]

    assert not _constraint_definition_matches(
        contract,
        "CHECK (decision::text = ANY (ARRAY['approved'::character varying::text, "
        "'rejected'::character varying::text, 'skipped'::character varying::text, "
        "'pending'::character varying::text]))",
    )
    assert not _constraint_definition_matches(
        contract,
        "CHECK ((decision::text = ANY (ARRAY['approved'::character varying::text, "
        "'rejected'::character varying::text, 'skipped'::character varying::text])) OR TRUE)",
    )
    assert not _constraint_definition_matches(
        contract,
        "CHECK (decision::text = ANY (ARRAY['approved'::character varying::text, "
        "'rejected'::character varying::text]))",
    )


def test_assignment_arm_accepts_production_rendering_but_rejects_extra_arm():
    contract = CONSTRAINTS["ck_geo_article_assignment_arm"]

    assert _constraint_definition_matches(
        contract,
        "CHECK (arm::text = ANY (ARRAY['control'::character varying::text, "
        "'candidate'::character varying::text]))",
    )
    assert not _constraint_definition_matches(
        contract,
        "CHECK (arm::text = ANY (ARRAY['control'::character varying::text, "
        "'candidate'::character varying::text, 'shadow'::character varying::text]))",
    )


def test_closed_loop_check_accepts_pg16_dump_restore_cast_rendering():
    expected = CLOSED_LOOP_CHECK_DEFINITIONS["ck_geo_article_plan_run_mode"]
    restored = (
        "CHECK (run_mode::text = ANY (ARRAY['shadow'::character varying::text, "
        "'assisted'::character varying::text, 'canary'::character varying::text]))"
    )
    assert _closed_loop_check_definition_matches(expected, restored)


def test_closed_loop_check_rejects_extra_value_or_bypass():
    expected = CLOSED_LOOP_CHECK_DEFINITIONS["ck_geo_article_plan_run_mode"]
    expanded = (
        "CHECK (run_mode::text = ANY (ARRAY['shadow'::character varying::text, "
        "'assisted'::character varying::text, 'canary'::character varying::text, "
        "'unsafe'::character varying::text]))"
    )
    bypassed = (
        "CHECK ((run_mode::text = ANY (ARRAY['shadow'::character varying::text, "
        "'assisted'::character varying::text, 'canary'::character varying::text])) OR TRUE)"
    )
    assert not _closed_loop_check_definition_matches(expected, expanded)
    assert not _closed_loop_check_definition_matches(expected, bypassed)
