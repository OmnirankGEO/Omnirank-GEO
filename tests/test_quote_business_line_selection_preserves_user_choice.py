"""Customer-selected quote scope must not be reduced by an audit heuristic."""

import ast
from pathlib import Path


SELECTION_API = Path(__file__).parents[1] / "api" / "selection_api.py"


def _load_selector():
    module = ast.parse(SELECTION_API.read_text(encoding="utf-8"))
    dependencies = [
        node
        for node in module.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef))
        and node.name in {
            "KeywordSelectionContractError",
            "_normalize_unique_keyword_snapshot_ids",
            "_select_keyword_ids_for_business_lines",
        }
    ]
    isolated = ast.Module(body=dependencies, type_ignores=[])
    namespace: dict = {"ValueError": ValueError}
    exec(compile(ast.fix_missing_locations(isolated), str(SELECTION_API), "exec"), namespace)
    return namespace["_select_keyword_ids_for_business_lines"]


_select_keyword_ids_for_business_lines = _load_selector()


def _load_override_recorder():
    module = ast.parse(SELECTION_API.read_text(encoding="utf-8"))
    functions = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"_same_review_event", "_record_keyword_review_overrides"}
    ]
    isolated = ast.Module(body=functions, type_ignores=[])
    namespace = {
        "_is_informational_keyword": lambda item: (
            item.get("intent") == "informational",
            "intent=informational" if item.get("intent") == "informational" else "",
        )
    }
    exec(compile(ast.fix_missing_locations(isolated), str(SELECTION_API), "exec"), namespace)
    return namespace["_record_keyword_review_overrides"]


_record_keyword_review_overrides = _load_override_recorder()


def _load_delivery_partitioner():
    module = ast.parse(SELECTION_API.read_text(encoding="utf-8"))
    functions = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_partition_delivery_exclusions"
    ]
    isolated = ast.Module(body=functions, type_ignores=[])
    namespace = {
        "_is_informational_keyword": lambda item: (
            item.get("intent") == "informational",
            "intent=informational" if item.get("intent") == "informational" else "",
        ),
        "_COMMERCIAL_POLICY_VERSION": "geo-commercial-intent-governance-v1.0",
    }
    exec(compile(ast.fix_missing_locations(isolated), str(SELECTION_API), "exec"), namespace)
    return namespace["_partition_delivery_exclusions"]


_partition_delivery_exclusions = _load_delivery_partitioner()


def test_all_keywords_from_all_selected_business_lines_are_preserved():
    keywords = [
        {
            "id": keyword_id,
            "keyword": f"候选词{keyword_id}",
            "business_line_id": ((keyword_id - 1) % 6) + 1,
            "intent": "informational" if keyword_id % 3 == 0 else "commercial",
        }
        for keyword_id in range(1, 21)
    ]

    selected = _select_keyword_ids_for_business_lines(
        keywords,
        {1, 2, 3, 4, 5, 6},
    )

    assert selected == list(range(1, 21))


def test_only_keywords_from_customer_selected_lines_are_preserved():
    keywords = [
        {"id": 1, "keyword": "业务一知识题", "business_line_id": 1},
        {"id": 2, "keyword": "业务一商业题", "business_line_id": 1},
        {"id": 3, "keyword": "业务二商业题", "business_line_id": 2},
    ]

    assert _select_keyword_ids_for_business_lines(keywords, {1}) == [1, 2]


def test_old_sessions_without_lineage_keep_all_visible_keywords():
    keywords = [
        {"id": 11, "keyword": "旧词一"},
        {"id": 12, "keyword": "旧词二"},
    ]

    assert _select_keyword_ids_for_business_lines(keywords, {1}) == [11, 12]


def test_mixed_lineage_preserves_all_when_customer_selects_every_line():
    keywords = [
        {"id": 1, "keyword": "新词", "business_line_id": 1},
        {"id": 2, "keyword": "旧词一"},
        {"id": 3, "keyword": "旧词二"},
    ]

    assert _select_keyword_ids_for_business_lines(
        keywords,
        {1, 2},
        all_business_line_ids={1, 2},
    ) == [1, 2, 3]


def test_mixed_lineage_partial_selection_fails_instead_of_dropping_old_words():
    keywords = [
        {"id": 1, "keyword": "新词", "business_line_id": 1},
        {"id": 2, "keyword": "旧词"},
    ]

    try:
        _select_keyword_ids_for_business_lines(
            keywords,
            {1},
            all_business_line_ids={1, 2},
        )
    except Exception as error:
        assert getattr(error, "code", "") == "KEYWORD_BUSINESS_LINE_LINEAGE_INCOMPLETE"
    else:
        raise AssertionError("mixed lineage must not silently drop unassigned keywords")


def test_duplicate_keyword_ids_fail_before_selection_or_pricing():
    keywords = [
        {"id": 1, "keyword": "业务一", "business_line_id": 1},
        {"id": 1, "keyword": "业务二", "business_line_id": 2},
    ]

    try:
        _select_keyword_ids_for_business_lines(
            keywords,
            {1},
            all_business_line_ids={1, 2},
        )
    except Exception as error:
        assert getattr(error, "code", "") == "KEYWORD_ID_DUPLICATED"
    else:
        raise AssertionError("duplicate IDs must fail before downstream expansion")


def test_orphan_business_line_reference_fails_instead_of_dropping_keyword():
    keywords = [
        {"id": 1, "keyword": "正常词", "business_line_id": 1},
        {"id": 2, "keyword": "孤儿词", "business_line_id": 99},
    ]

    try:
        _select_keyword_ids_for_business_lines(
            keywords,
            {1},
            all_business_line_ids={1},
        )
    except Exception as error:
        assert getattr(error, "code", "") == "KEYWORD_BUSINESS_LINE_ORPHANED"
    else:
        raise AssertionError("orphan lineage must not be silently removed")


def test_business_line_submit_no_longer_uses_audit_as_destructive_filter():
    source = (
        Path(__file__).parents[1] / "api" / "selection_api.py"
    ).read_text(encoding="utf-8")
    block = source[
        source.index("async def submit_business_lines"):
        source.index('@router.post("/s/{token}/withdraw-keywords")')
    ]

    assert "_select_keyword_ids_for_business_lines(" in block
    assert "info_skipped" not in block
    assert "建议人工复核，不做静默删除" in block
    assert '"keywords_review_suggested"' in block
    assert "_record_keyword_review_overrides(" in block
    assert "keywords_snapshot = %s" in block


def test_knowledge_terms_are_excluded_by_default_without_operator_release():
    """[报价纠偏工单 2026-07-26 P0-4 修订 · 裁决 D8「放行权归用户」]

    当前契约(取代 2026-07-23 的"物理隔离不可改选回"):
      - 知识词**默认**不进付费交付,逐条盖章 + 返回原因(绝不静默消失);
      - 没有操作员放行理由时,行为与旧版完全一致(下面这条用例锁的就是这个默认态);
      - 有放行理由时才回到交付 —— 见
        `test_operator_release_puts_knowledge_term_back_into_delivery`。
      - 合格商业词照常保留,不受影响。
    """
    # [Review-CTO 2026-07-23 P1] 用真实查询词:唯一引擎按文本判定,
    # 占位假词不代表商业意图。
    keywords = [
        {
            "id": 1,
            "keyword": "GEO和SEO有什么区别",
            "intent": "informational",
            "rejection_reason": "系统建议复核",
        },
        {
            "id": 2,
            "keyword": "深圳装修公司哪家好",
            "intent": "commercial",
        },
    ]

    stamped, exclusions = _partition_delivery_exclusions(
        keywords,
        [1, 2],
        reviewed_at="2026-07-23 12:00:00",
    )

    # 知识词:逐条排除(可见、有原因、有策略版本),物理离开付费交付
    assert [item["id"] for item in exclusions] == [1]
    assert exclusions[0]["reason"]
    assert exclusions[0]["policy_version"]
    assert stamped[0]["commercial_delivery_eligible"] is False
    assert stamped[0]["delivery_exclusion"]["decision"] == "knowledge_term_not_deliverable"
    # 商业词:不受影响
    assert "delivery_exclusion" not in stamped[1]

    # 知识词不再产生 human_continue(无人工改选回付费交付的入口)
    updated, override_count = _record_keyword_review_overrides(
        stamped,
        [2],
        actor_kind="customer_business_line_selection",
        actor_id=99,
        reason="客户明确选择包含该关键词的业务方向",
        reviewed_at="2026-07-23 12:00:00",
    )
    assert override_count == 0
    assert "review_advisory" not in updated[0]
    assert "review_advisory" not in updated[1]

    # 排除章上必须写清楚"这条是可以人工放行的"(不是四条硬边界)
    assert stamped[0]["delivery_exclusion"]["human_override_allowed"] is True
    assert stamped[0]["delivery_exclusion"]["hard_block"] is False


def test_operator_release_puts_knowledge_term_back_into_delivery():
    """[工单 P0-4] 操作员显式放行(review_override_reason)→ 进付费交付 + 留审计。

    生产实证:纯 regex 引擎把"…培训哪里正规"这类真问法判成知识词。
    操作员明知是真词却救不回来 = 与裁决 D8「放行权归用户」直接冲突。
    """
    keywords = [
        {
            "id": 1,
            "keyword": "GEO和SEO有什么区别",
            "intent": "informational",
            "review_override_reason": "客户原话就是这么问的,销售确认要做",
        },
        {
            # 没填放行理由 → 仍然默认排除(放行必须是显式动作)
            "id": 2,
            "keyword": "电梯保养流程是怎样的",
            "intent": "informational",
        },
    ]

    stamped, exclusions = _partition_delivery_exclusions(
        keywords, [1, 2], reviewed_at="2026-07-26 12:00:00",
    )
    by_id = {item["id"]: item for item in stamped}

    assert by_id[1]["commercial_delivery_eligible"] is True
    assert "delivery_exclusion" not in by_id[1]
    assert by_id[1]["human_release"]["decision"] == "human_release_into_delivery"
    assert by_id[1]["human_release"]["reason"] == "客户原话就是这么问的,销售确认要做"
    assert by_id[1]["human_release"]["engine_intent_type"]  # 引擎结论仍留档
    assert by_id[1]["human_release"]["released_at"] == "2026-07-26 12:00:00"

    assert [e["id"] for e in exclusions] == [2]
    assert by_id[2]["commercial_delivery_eligible"] is False


def test_hard_boundary_terms_cannot_be_released_even_by_operator():
    """[工单 P0-4] 物理禁选只剩四条硬边界(法律/资金/越权/数据完整性),人工也放不行。"""
    keywords = [
        {"id": 1, "keyword": "   ", "review_override_reason": "我就要这条"},
        {"id": 2, "keyword": "深" * 60, "review_override_reason": "我就要这条"},
    ]
    stamped, exclusions = _partition_delivery_exclusions(
        keywords, [1, 2], reviewed_at="2026-07-26 12:00:00",
    )
    assert [e["id"] for e in exclusions] == [1, 2]
    for item in stamped:
        assert item["commercial_delivery_eligible"] is False
        assert "human_release" not in item
        assert item["delivery_exclusion"]["hard_block"] is True
        assert item["delivery_exclusion"]["human_override_allowed"] is False


def test_snapshot_freezes_operator_release_fields():
    """建链接时快照必须冻住放行理由与硬边界标记(提交侧据此复核)。"""
    source = SELECTION_API.read_text(encoding="utf-8")
    block = source[
        source.index("class KeywordSnapshotItem"):
        source.index('@router.post("/keyword-selection/{token}/extend")')
    ]
    assert '"review_override_reason": kw.review_override_reason or ""' in block
    assert '"human_override_allowed": not _hard_block_code' in block
    assert '"hard_block": bool(_hard_block_code)' in block


def test_twenty_commercial_selections_commit_as_twenty():
    """[指令 §六.3 / SSOT §3.3] 已通过商业资格的 20 条全选 → 20 条保留,
    零静默缩减(排除只发生在知识词上,且必须逐条给原因)。"""
    keywords = [
        {"id": i, "keyword": f"深圳行业服务商推荐{i}", "intent": "commercial"}
        for i in range(1, 21)
    ]
    stamped, exclusions = _partition_delivery_exclusions(
        keywords,
        list(range(1, 21)),
        reviewed_at="2026-07-23 12:00:00",
    )
    assert exclusions == []
    assert len(stamped) == 20
    assert all("delivery_exclusion" not in item for item in stamped)


def test_review_override_history_is_append_only_and_idempotent():
    # [SSOT v1.0 §2.2] advisory + 人工确认继续只适用于合格商业词的
    # 范围/表达争议(scope_match=False);知识词(geo_recommend=False)
    # 已改走 _partition_delivery_exclusions,不再产生 human_continue。
    # 合格商业词(唯一引擎判 eligible)+ scope_match=False → 走 advisory
    keywords = [
        {
            "id": 1,
            "keyword": "深圳装修公司哪家好",
            "scope_match": False,
            "review_advisory": {
                "policy_version": "buyer-intent-review-v1",
                "decision": "human_continue",
                "actor_kind": "sales_operator_keyword_selection",
                "actor_id": 7,
                "reason": "销售决定继续",
                "original_issue": "系统建议复核",
                "reviewed_at": "2026-07-23 09:00:00",
            },
        }
    ]

    first, first_count = _record_keyword_review_overrides(
        keywords,
        [1],
        actor_kind="customer_business_line_selection",
        actor_id=99,
        reason="客户明确选择包含该关键词的业务方向",
        reviewed_at="2026-07-23 12:00:00",
    )
    second, second_count = _record_keyword_review_overrides(
        first,
        [1],
        actor_kind="customer_business_line_selection",
        actor_id=99,
        reason="客户明确选择包含该关键词的业务方向",
        reviewed_at="2026-07-23 12:01:00",
    )

    assert first_count == 1
    assert second_count == 0
    assert len(second[0]["review_advisory_history"]) == 2
    assert second[0]["review_advisory_history"][0]["actor_kind"] == "sales_operator_keyword_selection"
    assert second[0]["review_advisory_history"][1]["actor_kind"] == "customer_business_line_selection"


def test_create_link_freezes_sales_override_metadata_in_existing_snapshot():
    source = SELECTION_API.read_text(encoding="utf-8")
    block = source[
        source.index("class KeywordSnapshotItem"):
        source.index('@router.post("/keyword-selection/{token}/extend")')
    ]

    for field in (
        "geo_recommend",
        "scope_match",
        "default_selected",
        "rejection_reason",
        "review_override_reason",
        "review_version",
    ):
        assert field in block
    assert '"decision": "human_continue"' in block
    assert '"actor_kind": "sales_operator_keyword_selection"' in block
    assert '"original_issue":' in block
    assert '"reviewed_at":' in block
    assert 'snapshot_item["review_advisory_history"] = [dict(review_event)]' in block
    assert "_normalize_unique_keyword_snapshot_ids(" in source[
        source.index("async def confirm_quote"):
        source.index('@router.post("/s/{token}/events")')
    ]
    assert "_normalize_unique_keyword_snapshot_ids(" in source[
        source.index("async def _generate_quote_impl"):
        source.index("async def _generate_quote_cluster_mode")
    ]


def test_partition_uses_single_engine_reviewer_counterexamples():
    """[Review-CTO 2026-07-23 P1] 真实提交分区必须走唯一引擎:
    实测 4 反例(哪个好/更适合=商业保留·靠谱吗=商业·预算怎么做=知识排除),
    覆盖旧 _is_informational 的 哪个好/哪个更/和 误排。"""
    keywords = [
        {"id": 1, "keyword": "国产电梯和进口电梯哪个好"},
        {"id": 2, "keyword": "A品牌和B品牌哪个更适合医院"},
        {"id": 3, "keyword": "深圳装修公司靠谱吗"},
        {"id": 4, "keyword": "预算怎么做"},
    ]
    stamped, exclusions = _partition_delivery_exclusions(
        keywords, [1, 2, 3, 4], reviewed_at="2026-07-23 12:00:00",
    )
    by_id = {it["id"]: it for it in stamped}
    # 商业比较/信任问法保留可计价
    for kid in (1, 2, 3):
        assert by_id[kid]["commercial_delivery_eligible"] is True, kid
        assert "delivery_exclusion" not in by_id[kid], kid
    # 只有知识题被排除
    assert [e["id"] for e in exclusions] == [4]
    assert by_id[4]["commercial_delivery_eligible"] is False


def test_partition_twenty_commercial_stay_twenty_via_engine():
    """20 条商业比较词全选 → 20 条保留(不被旧 哪个好/和 模式二次砍)。"""
    kws = [
        {"id": i, "keyword": f"深圳{i}区装修公司和全屋定制哪个更划算推荐几家"}
        for i in range(1, 21)
    ]
    stamped, exclusions = _partition_delivery_exclusions(
        kws, list(range(1, 21)), reviewed_at="2026-07-23 12:00:00",
    )
    assert exclusions == []
    assert all(it["commercial_delivery_eligible"] is True for it in stamped)


def test_partition_uncertain_goes_to_clarification_zone():
    """待澄清词排除但归需澄清区(decision=needs_clarification),非知识永久排除。"""
    kws = [{"id": 1, "keyword": "行业白皮书发布月历"}]
    stamped, exclusions = _partition_delivery_exclusions(
        kws, [1], reviewed_at="2026-07-23 12:00:00",
    )
    assert exclusions[0]["kind"] == "needs_clarification"
    assert stamped[0]["delivery_exclusion"]["decision"] == "needs_clarification"
