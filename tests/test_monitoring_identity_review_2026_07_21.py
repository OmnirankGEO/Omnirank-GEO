from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pending_identity_contract_helpers_are_fail_closed():
    from services.monitoring_identity_review import (
        aggregate_eligible_sql,
        bounded_identity_candidates,
        build_evidence_hash,
        is_pending_identity_result,
    )

    assert is_pending_identity_result({"identity_review_state": "pending"})
    assert is_pending_identity_result({"response_status": "brand_identity_unresolved"})
    assert is_pending_identity_result({"mention_type": "pending_identity"})
    assert not is_pending_identity_result({"response_status": "success"})
    assert "identity_review_state" in aggregate_eligible_sql("mr")
    assert "brand_identity_unresolved" in aggregate_eligible_sql("mr")
    assert "pending_identity" in aggregate_eligible_sql("mr")
    assert bounded_identity_candidates([" 候选甲 ", "候选甲", "候选乙", ""]) == [
        "候选甲",
        "候选乙",
    ]
    first = build_evidence_hash(
        brand_id=7,
        platform="deepseek",
        keyword="测试词",
        candidates=["候选甲"],
        evidence_snippet="证据",
        full_response="完整回答",
    )
    second = build_evidence_hash(
        brand_id=7,
        platform="deepseek",
        keyword="测试词",
        candidates=["候选甲"],
        evidence_snippet="证据",
        full_response="完整回答",
    )
    assert first == second and len(first) == 64


def test_rejudge_is_provider_free_and_frontend_is_durable():
    db_source = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    rejudge = db_source[db_source.index("async def reverify_keyword_results") :]
    assert "resolver.resolve_local(text)" in rejudge
    assert "await resolver.resolve(" not in rejudge

    panel = (
        ROOT
        / "frontend"
        / "src"
        / "pages"
        / "Monitoring"
        / "components"
        / "IdentityReviewPanel.tsx"
    ).read_text(encoding="utf-8")
    assert "/api/monitoring/identity-reviews" in panel
    assert "AbortController" in panel
    assert "setTimeout(poll, 30_000)" in panel
    assert "setInterval" not in panel
    assert "decisionRequestIds" in panel
    assert "break-words" in panel
    assert "都不是这些品牌" in panel
    assert "不是这个品牌" not in panel
    assert '<X className="mr-1 h-4 w-4" />没有出现' in panel
    for internal_term in (
        "brand_identity_unresolved",
        "pending_identity",
        "identity_review_state",
    ):
        assert internal_term not in panel


def test_identity_decision_request_accepts_absence_without_weakening_other_actions():
    """API 允许 no+空名称；yes/custom 的空名称仍由事务层拒绝。"""
    server_tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    request_class = next(
        node
        for node in server_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "MonitoringIdentityDecisionRequest"
    )
    namespace: dict[str, object] = {
        "BaseModel": __import__("pydantic").BaseModel,
        "Field": __import__("pydantic").Field,
        "Literal": __import__("typing").Literal,
        "uuid": __import__("uuid"),
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[request_class], type_ignores=[])),
            "server.py:MonitoringIdentityDecisionRequest",
            "exec",
            dont_inherit=True,
        ),
        namespace,
    )
    model = namespace["MonitoringIdentityDecisionRequest"]
    payload = model(
        brand_id=7,
        action="no",
        selected_name="",
        expected_version=0,
        evidence_hash="a" * 64,
        request_id=__import__("uuid").uuid4(),
    )
    assert payload.selected_name == ""

    db_source = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    decision_body = db_source[db_source.index("def decide_monitoring_identity_review"):]
    decision_body = decision_body[: decision_body.index("\ndef ", 10)]
    assert 'answer_absent = normalized_action == "no" and not normalized_name' in decision_body
    assert '"full_answer_absent"' in decision_body


def test_absent_guard_message_is_the_same_string_the_frontend_lock_mocks():
    """[返修单 R1] 防"测量仪器与被测实现口径漂移"。

    守卫本身的判据是真调用(`tests/test_m1_identity_confirm_ux_2026_07_28.py` 里
    对着真 PG 断言抛出的异常文案),前端那条是真渲染(spec 里 mock 409 的 detail)。
    但 mock 是**手抄**的字符串:后端改文案时真调用那条会转红,前端那条会**假绿**
    ——它照样渲染自己抄的旧文案。这里把两边钉成同一个字面量。
    """
    db_source = (ROOT / "db" / "monitoring_db.py").read_text(encoding="utf-8")
    decision_body = db_source[db_source.index("def decide_monitoring_identity_review"):]
    decision_body = decision_body[: decision_body.index("\ndef ", 10)]

    absent_message = "候选中包含当前品牌的可信名称，不能直接记为未出现；请逐条判断该候选"
    named_message = "候选中包含当前品牌的可信名称，不能整组标记为不是"
    # 后端两条文案必须都在,且互不相同 —— 同一个字符串就等于没区分
    assert absent_message in decision_body
    assert named_message in decision_body
    assert absent_message != named_message

    # 守卫的判别位:absent 分支必须读**卡上真实存在的候选**,不读被置空的 decision_names
    assert "candidate_map.keys()" in decision_body
    guard_line = next(
        line for line in decision_body.splitlines()
        if "name_key in trusted_names" in line
    )
    assert "guarded_name_keys" in guard_line, (
        "守卫仍在读 decision_names —— absent 分支把它置空,any() 对空集恒 False"
    )

    spec = (
        ROOT / "frontend" / "tests" / "frontend-nogo" / "monitoring-identity-absent.spec.ts"
    ).read_text(encoding="utf-8")
    assert absent_message in spec, "前端锁 mock 的 409 文案与后端实际抛出的已经不是同一句"

    # 链路第三环:前端 mock 的是 409,API 层就必须真把这个异常映射成 409。
    # 映射改成 500 时,前端锁照样绿(它 mock 的是自己写的 409),真 UX 却会走
    # 到另一条错误分支 —— 这条钉把三环焊死。
    server_tree = ast.parse((ROOT / "server.py").read_text(encoding="utf-8"))
    handlers = [
        node for node in ast.walk(server_tree)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Name)
        and node.type.id == "MonitoringIdentityReviewConflict"
    ]
    assert handlers, "server.py 不再捕获 MonitoringIdentityReviewConflict"
    for handler in handlers:
        codes = {
            keyword.value.value
            for raise_node in ast.walk(handler)
            if isinstance(raise_node, ast.Call)
            for keyword in raise_node.keywords
            if keyword.arg == "status_code" and isinstance(keyword.value, ast.Constant)
        }
        assert codes == {409}, f"守卫冲突的 HTTP 映射漂了:{codes}"


def test_all_changed_monitoring_aggregate_surfaces_use_shared_predicate():
    expected = (
        "api/distillation_api.py",
        "api/m3_api.py",
        "api/monitoring_api.py",
        "api/scheduler.py",
        "db/monitoring_db.py",
        "db/publish_db.py",
        "scheduler.py",
        "services/ai_ops/report_metrics/monitoring.py",
        "services/article_data_health.py",
        "services/article_experiment_registry.py",
        "services/article_expert_calibration.py",
        "services/geo_observation/reconciler.py",
        "services/geo_observation_analytics/evidence.py",
        "services/publish_recommendation.py",
        "services/report_evidence.py",
        "services/source_authority_analyzer.py",
        "services/strict_article_outcomes.py",
        "tools/distillation/backfill.py",
    )
    missing = []
    for relative in expected:
        source = (ROOT / relative).read_text(encoding="utf-8")
        if "aggregate_eligible_sql" not in source:
            missing.append(relative)
        tree = ast.parse(source, filename=relative)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            assert "{aggregate_eligible_sql" not in node.value, (
                f"{relative} leaves the aggregate predicate in a literal SQL string"
            )
    assert missing == []

    source_hooks = (ROOT / "services/geo_observation/source_hooks.py").read_text(
        encoding="utf-8"
    )
    assert 'row["identity_review_state"] == "pending"' in source_hooks
    assert 'row["response_status"] == "brand_identity_unresolved"' in source_hooks
    assert "return SourceGate(\n            False,\n            False" in source_hooks

    lineage = (ROOT / "services/monitoring_lineage.py").read_text(encoding="utf-8")
    assert 'status == "brand_identity_unresolved"' in lineage
    assert 'mention == "pending_identity"' in lineage
    assert 'return "entity_ambiguous", 1.0' in lineage

    trigger = (ROOT / "tools/distillation/trigger.py").read_text(encoding="utf-8")
    assert "from db.monitoring_db import get_aggregate_task_results" in trigger
    assert "results = get_aggregate_task_results(task_id)" in trigger


def test_identity_review_migration_is_in_prestart_manifest():
    manifest = (ROOT / "db" / "migration_manifest.py").read_text(encoding="utf-8")
    product = "scripts/migration_monitoring_product_matrix_2026_07_21.sql"
    identity = "scripts/migration_monitoring_identity_review_2026_07_21.sql"
    assert product in manifest and identity in manifest
    assert manifest.index(product) < manifest.index(identity)


def test_completion_event_separates_attempted_and_eligible_samples():
    source = (ROOT / "tools" / "monitoring" / "batch_monitor.py").read_text(
        encoding="utf-8"
    )
    assert '"attempted_tests": attempted_tests' in source
    assert '"total_tests": total_tests' in source
    assert '"pending_identity_count": pending_identity_count' in source
    assert '"total_tests": len(results)' not in source
