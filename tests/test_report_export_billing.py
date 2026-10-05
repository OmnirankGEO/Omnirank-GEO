import ast
import asyncio
import copy
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException


def test_charge_report_export_skips_admin():
    from services.report_export_billing import charge_report_export

    calls = []

    async def deduct(user_id, feature_code):
        calls.append((user_id, feature_code))

    request = SimpleNamespace(state=SimpleNamespace(user={"user_id": 1, "is_admin": True}))
    charge = charge_report_export(request, diagnosis_id=10, deduct_func=deduct)

    assert charge.user_id is None
    assert charge.charged is False
    assert calls == []


def test_charge_report_export_charges_non_admin_and_refunds():
    from services.report_export_billing import charge_report_export

    calls = []

    async def deduct(user_id, feature_code):
        calls.append(("deduct", user_id, feature_code))
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": 1001,
            "channel": "v35",
        }

    async def refund(user_id, feature_code, reason, charge_tx_id, ledger_type):
        calls.append(
            ("refund", user_id, feature_code, reason, charge_tx_id, ledger_type)
        )
        return {"success": True, "refunded": 10}

    request = SimpleNamespace(state=SimpleNamespace(user={"user_id": 8, "is_admin": False}))
    charge = charge_report_export(request, diagnosis_id=10, deduct_func=deduct)
    assert charge.charged is True

    charge.refund("boom", refund_func=refund)

    assert charge.user_id == 8
    assert charge.charge_tx_id == 1001
    assert charge.ledger_type == "v35"
    assert charge.charged is False
    assert calls == [
        ("deduct", 8, "report_export"),
        ("refund", 8, "report_export", "boom", 1001, "v35"),
    ]


def test_client_pdf_not_ready_blocks_before_any_charge():
    from services.report_export_billing import (
        ClientReportNotReadyError,
        charge_client_report_export,
    )

    deduct_calls = []

    async def deduct(user_id, feature_code):
        deduct_calls.append((user_id, feature_code))
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": 901,
            "channel": "legacy",
        }

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 8, "is_admin": False})
    )
    internal_only = {
        "internal": {
            "modules": {"1": {"rendered_md": "INTERNAL_ONLY_MARGIN_37_PERCENT"}}
        }
    }

    with pytest.raises(ClientReportNotReadyError) as exc_info:
        charge_client_report_export(
            request,
            901,
            internal_only,
            deduct_func=deduct,
        )

    assert exc_info.value.code == "CLIENT_REPORT_NOT_READY"
    assert deduct_calls == []


def test_ready_client_pdf_guard_charges_non_admin_once():
    from services.report_export_billing import charge_client_report_export

    deduct_calls = []

    async def deduct(user_id, feature_code):
        deduct_calls.append((user_id, feature_code))
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": 902,
            "channel": "legacy",
        }

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 8, "is_admin": False})
    )
    ready = {"client": {"modules": {"1": {"insight": "真实客户结论"}}}}

    charge = charge_client_report_export(
        request,
        902,
        ready,
        deduct_func=deduct,
    )

    assert charge.charged is True
    assert charge.charge_tx_id == 902
    assert charge.ledger_type == "legacy"
    assert deduct_calls == [(8, "report_export")]


def test_report_export_attempt_key_is_bound_to_diagnosis_and_forwarded_to_billing():
    from services.report_export_billing import (
        charge_report_export,
        require_report_export_idempotency_key,
    )

    key = "report-export:902:stable-attempt-902"
    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 8, "is_admin": False}),
        headers={"X-Report-Export-Idempotency-Key": key},
    )
    assert require_report_export_idempotency_key(request, 902) == key
    with pytest.raises(HTTPException) as exc_info:
        require_report_export_idempotency_key(request, 903)
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail["code"] == "REPORT_EXPORT_IDEMPOTENCY_KEY_REQUIRED"

    calls = []

    async def deduct(user_id, feature_code, **kwargs):
        calls.append((user_id, feature_code, kwargs))
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": 1902,
            "channel": "legacy",
        }

    charge = charge_report_export(
        request,
        902,
        deduct_func=deduct,
        idempotency_key=key,
    )
    assert charge.idempotency_key == key
    assert calls == [(8, "report_export", {"idempotency_key": key})]


def test_report_export_attempt_key_is_not_required_for_admin_exemption():
    from services.report_export_billing import require_report_export_idempotency_key

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 1, "is_admin": True}),
        headers={},
    )
    assert require_report_export_idempotency_key(request, 902) is None


@pytest.mark.parametrize(
    ("name", "modules_jsonb", "expected_charge_count"),
    [
        (
            "raw_appendix_only",
            {"client": {"modules": {"3_raw": {"tests": [{"answer": "raw"}]}}}},
            0,
        ),
        (
            "legacy_rich_narrative_only",
            {
                "client": {
                    "modules": {
                        "rich_narrative": {"executive_summary": "legacy summary"}
                    }
                }
            },
            0,
        ),
        ("empty_numeric_module_one", {"client": {"modules": {"1": {}}}}, 0),
        (
            "real_numeric_module_one",
            {"client": {"modules": {"1": {"rendered_md": "真实客户报告正文"}}}},
            1,
        ),
    ],
)
def test_client_pdf_charge_uses_strict_customer_artifact_readiness(
    name, modules_jsonb, expected_charge_count
):
    from services.report_export_billing import (
        ClientReportNotReadyError,
        charge_client_report_export,
    )
    from services.report_html_renderer import is_client_report_ready

    deduct_calls = []

    async def deduct(user_id, feature_code):
        deduct_calls.append((user_id, feature_code))
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": 903,
            "channel": "legacy",
        }

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 8, "is_admin": False})
    )

    if expected_charge_count:
        charge = charge_client_report_export(
            request,
            903,
            modules_jsonb,
            deduct_func=deduct,
        )
        assert charge.charged is True, name
    else:
        with pytest.raises(ClientReportNotReadyError):
            charge_client_report_export(
                request,
                903,
                modules_jsonb,
                deduct_func=deduct,
            )

    assert is_client_report_ready(modules_jsonb) is bool(expected_charge_count), name
    assert len(deduct_calls) == expected_charge_count, name


def test_same_user_concurrent_exports_refund_their_own_exact_charge():
    from services.report_export_billing import charge_report_export

    id_lock = Lock()
    refund_lock = Lock()
    next_id = iter((2001, 2002))
    refund_calls = []

    async def deduct(_user_id, _feature_code):
        with id_lock:
            charge_tx_id = next(next_id)
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": charge_tx_id,
            "channel": "legacy",
        }

    async def refund(
        user_id, feature_code, reason, charge_tx_id, ledger_type
    ):
        with refund_lock:
            refund_calls.append(
                (user_id, feature_code, reason, charge_tx_id, ledger_type)
            )
        return {"success": True, "refunded": 10}

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 8, "is_admin": False})
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        charges = list(
            pool.map(
                lambda diagnosis_id: charge_report_export(
                    request, diagnosis_id, deduct_func=deduct
                ),
                (101, 102),
            )
        )
        list(
            pool.map(
                lambda charge: charge.refund("并发导出失败", refund_func=refund),
                charges,
            )
        )

    assert {charge.charge_tx_id for charge in charges} == {2001, 2002}
    assert all(charge.charged is False for charge in charges)
    assert {call[3] for call in refund_calls} == {2001, 2002}
    assert all(call[4] == "legacy" for call in refund_calls)


def test_failed_exact_refund_persists_exact_durable_recovery_identity():
    from services.report_export_billing import (
        ReportExportRefundError,
        charge_report_export,
    )

    async def deduct(_user_id, _feature_code):
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": 3001,
            "channel": "v35",
        }

    refund_calls = []
    recovery_calls = []

    async def refund(
        _user_id, _feature_code, reason, charge_tx_id, ledger_type
    ):
        refund_calls.append((reason, charge_tx_id, ledger_type))
        return {"success": False, "reason": "injected transient failure"}

    def recover(source, kind, **kwargs):
        recovery_calls.append((source, kind, kwargs))
        return 7001

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 8, "is_admin": False})
    )
    charge = charge_report_export(request, 103, deduct_func=deduct)

    with pytest.raises(ReportExportRefundError) as exc_info:
        charge.refund("第一次", refund_func=refund, recovery_func=recover)
    assert charge.charged is True
    assert charge.charge_tx_id == 3001
    assert charge.ledger_type == "v35"
    assert charge.deducted == 10
    assert charge.recovery_order_id == 7001
    assert exc_info.value.recovery_order_id == 7001
    assert refund_calls == [("第一次", 3001, "v35")]
    assert recovery_calls == [
        (
            "report_export",
            "refund",
            {
                "ref_key": "diagnosis:103",
                "user_id": 8,
                "feature_code": "report_export",
                "charge_tx_id": 3001,
                "amount_points": 10,
                "reason": "客户报告导出失败后的精确退款未确认",
                "last_error": "injected transient failure",
                "payload": {
                    "diagnosis_id": 103,
                    "user_id": 8,
                    "charge_tx_id": 3001,
                    "ledger_type": "v35",
                    "deducted": 10,
                    "export_failure": "第一次",
                },
                "ledger_type": "v35",
            },
        )
    ]


def test_refund_exception_keeps_exact_charge_identity_for_retry():
    from services.report_export_billing import (
        ReportExportRefundError,
        charge_report_export,
    )

    async def deduct(_user_id, _feature_code):
        return {
            "success": True,
            "deducted": 10,
            "charge_tx_id": 3002,
            "channel": "legacy",
        }

    async def refund(*_args, **_kwargs):
        raise TimeoutError("injected refund timeout")

    recovery_calls = []

    def recover(source, kind, **kwargs):
        recovery_calls.append((source, kind, kwargs))
        return 7002

    request = SimpleNamespace(
        state=SimpleNamespace(user={"user_id": 8, "is_admin": False})
    )
    charge = charge_report_export(request, 104, deduct_func=deduct)

    with pytest.raises(ReportExportRefundError) as exc_info:
        charge.refund("超时", refund_func=refund, recovery_func=recover)

    assert exc_info.value.result == {
        "success": False,
        "error_type": "TimeoutError",
    }
    assert charge.charged is True
    assert charge.charge_tx_id == 3002
    assert charge.ledger_type == "legacy"
    assert charge.recovery_order_id == 7002
    assert recovery_calls[0][2]["amount_points"] == 10
    assert recovery_calls[0][2]["payload"] == {
        "diagnosis_id": 104,
        "user_id": 8,
        "charge_tx_id": 3002,
        "ledger_type": "legacy",
        "deducted": 10,
        "export_failure": "超时",
    }


def test_recovery_registration_failure_is_not_reported_as_durable_success():
    from services.report_export_billing import (
        ReportExportCharge,
        ReportExportRecoveryPersistenceError,
    )

    async def refund_failed(*_args, **_kwargs):
        return {"success": False, "reason": "temporary refund failure"}

    charge = ReportExportCharge(
        user_id=8,
        diagnosis_id=105,
        charged=True,
        charge_tx_id=3003,
        ledger_type="legacy",
        deducted=10,
    )

    with pytest.raises(ReportExportRecoveryPersistenceError):
        charge.refund(
            "生成失败",
            refund_func=refund_failed,
            recovery_func=lambda *_args, **_kwargs: None,
        )

    assert charge.charged is True
    assert charge.recovery_order_id is None


def test_lost_refund_response_persists_and_processor_closes_from_real_ledger_evidence(
    monkeypatch,
):
    """A committed refund with a lost response survives the request and closes by evidence."""
    from db.auth_db import init_auth_db
    from db.connection import get_connection
    from db.fund_recovery_db import get_recovery_order, init_fund_recovery_tables
    from db.wallet_db import init_wallet_tables
    from services.report_export_billing import ReportExportCharge, ReportExportRefundError

    # Keep this PostgreSQL test independent from suite order.  These are the
    # production idempotent initializers, so the fixture exercises real FK and
    # ledger contracts instead of relying on tables seeded by an earlier test.
    init_auth_db()
    init_wallet_tables()

    conn = get_connection()
    cur = conn.cursor()
    init_fund_recovery_tables(cur)
    cur.execute("SELECT id FROM users ORDER BY id LIMIT 1")
    user = cur.fetchone()
    assert user, "throwaway PostgreSQL must contain a test user"
    user_id = int(user["id"] if isinstance(user, dict) else user[0])
    cur.execute(
        """
        INSERT INTO point_transactions
            (user_id, type, point_type, amount, balance_after, feature_code,
             description, order_id)
        VALUES (%s, 'consume', 'paid', -10, 0, 'report_export',
                'report-export-lost-response-test', %s)
        RETURNING id
        """,
        (user_id, "report-export-lost-response-test"),
    )
    consume_row = cur.fetchone()
    charge_tx_id = int(
        consume_row["id"] if isinstance(consume_row, dict) else consume_row[0]
    )
    conn.commit()
    conn.close()

    async def committed_then_response_lost(
        refund_user_id,
        feature_code,
        reason,
        charge_tx_id,
        ledger_type,
    ):
        assert (refund_user_id, feature_code, ledger_type) == (
            user_id,
            "report_export",
            "legacy",
        )
        evidence_conn = get_connection()
        evidence_cur = evidence_conn.cursor()
        evidence_cur.execute(
            """
            INSERT INTO point_transactions
                (user_id, type, point_type, amount, balance_after, feature_code,
                 description, order_id)
            VALUES (%s, 'refund', 'paid', 10, 0, 'report_export', %s, %s)
            """,
            (refund_user_id, reason, str(charge_tx_id)),
        )
        evidence_conn.commit()
        evidence_conn.close()
        raise TimeoutError("refund committed but response was lost")

    charge = ReportExportCharge(
        user_id=user_id,
        diagnosis_id=99104,
        charged=True,
        charge_tx_id=charge_tx_id,
        ledger_type="legacy",
        deducted=10,
    )
    recovery_order_id = None
    try:
        with pytest.raises(ReportExportRefundError) as exc_info:
            charge.refund(
                "PDF 生成失败",
                refund_func=committed_then_response_lost,
            )
        recovery_order_id = exc_info.value.recovery_order_id
        assert recovery_order_id is not None
        work_order = get_recovery_order(recovery_order_id)
        assert work_order["source"] == "report_export"
        assert work_order["ref_key"] == "diagnosis:99104"
        assert work_order["user_id"] == user_id
        assert work_order["charge_tx_id"] == charge_tx_id
        assert work_order["ledger_type"] == "legacy"
        assert work_order["amount_points"] == 10
        assert work_order["payload"] == {
            "diagnosis_id": 99104,
            "user_id": user_id,
            "charge_tx_id": charge_tx_id,
            "ledger_type": "legacy",
            "deducted": 10,
            "export_failure": "PDF 生成失败",
        }

        processing_conn = get_connection()
        processing_cur = processing_conn.cursor()
        processing_cur.execute(
            """
            UPDATE fund_recovery_orders
               SET status='processing', claim_token='report-export-test-token',
                   worker_id='report-export-test', claimed_at=NOW()
             WHERE id=%s
            """,
            (recovery_order_id,),
        )
        processing_conn.commit()
        processing_conn.close()
        work_order = get_recovery_order(recovery_order_id)

        import middleware.billing as billing
        import services.fund_recovery_processor as processor

        claim_results = iter((work_order, None))
        monkeypatch.setattr(
            processor,
            "claim_next_recovery_order",
            lambda _worker_id: next(claim_results),
        )

        async def retry_without_notification(*_args, **_kwargs):
            # This is the real production shape after the first refund already
            # committed: without a notification context, refund_points returns
            # failure and the processor must use immutable ledger evidence.
            return {"success": False, "reason": "未找到扣费记录"}

        monkeypatch.setattr(billing, "refund_points", retry_without_notification)
        stats = asyncio.run(processor.process_pending(limit=1))
        assert stats["idempotent"] == 1
        assert stats["resolved"] == 1
        assert get_recovery_order(recovery_order_id)["status"] == "resolved"
    finally:
        cleanup_conn = get_connection()
        cleanup_cur = cleanup_conn.cursor()
        if recovery_order_id is not None:
            cleanup_cur.execute(
                "DELETE FROM fund_recovery_orders WHERE id=%s",
                (recovery_order_id,),
            )
        cleanup_cur.execute(
            "DELETE FROM point_transactions WHERE user_id=%s AND "
            "(id=%s OR order_id=%s OR description='report-export-lost-response-test')",
            (user_id, charge_tx_id, str(charge_tx_id)),
        )
        cleanup_conn.commit()
        cleanup_conn.close()


@pytest.mark.parametrize(
    "record",
    [
        {"report_v2_version": "v1", "report": "INTERNAL_ONLY"},
        {
            "report_v2_version": "v2",
            "report_v2_modules_jsonb": {
                "client": {"modules": {"3_raw": {"tests": []}}}
            },
        },
    ],
)
def test_legacy_export_record_guard_rejects_internal_or_partial_artifacts(record):
    from services.report_export_billing import (
        ClientReportNotReadyError,
        require_client_v2_export_ready,
    )

    with pytest.raises(ClientReportNotReadyError) as exc_info:
        require_client_v2_export_ready(record)
    assert exc_info.value.code == "CLIENT_REPORT_NOT_READY"


def test_legacy_export_record_guard_returns_only_ready_client_modules():
    from services.report_export_billing import require_client_v2_export_ready

    modules_jsonb = {
        "client": {"modules": {"1": {"rendered_md": "客户安全报告"}}},
        "internal": {"modules": {"1": {"rendered_md": "INTERNAL_ONLY"}}},
    }
    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": modules_jsonb,
        "report": "INTERNAL_LEGACY_MARKDOWN",
    }

    assert require_client_v2_export_ready(record) is modules_jsonb


@pytest.mark.parametrize("field", ["insight", "conclusion_text", "rendered_md"])
def test_each_canonical_module_one_text_field_can_prove_readiness(field):
    from services.report_html_renderer import is_client_report_ready

    assert is_client_report_ready(
        {"client": {"modules": {"1": {field: " 真实客户内容 "}}}}
    )
    assert not is_client_report_ready(
        {"client": {"modules": {"1": {field: "   "}}}}
    )


def test_pdf_endpoint_checks_readiness_before_renderer_and_charge_writer():
    source = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    start = source.index("def get_diagnosis_v2_pdf(")
    end = source.index("\n\n@app.get(\"/api/diagnosis/{id}/report-v2.html\")", start)
    block = source[start:end]

    idempotency_index = block.index("require_report_export_idempotency_key(request, id)")
    readiness_index = block.index("require_client_v2_export_ready(record)")
    renderer_index = block.index("render_report_v3_html(")
    temp_file_index = block.index("tempfile.NamedTemporaryFile(")
    writer_index = block.index("charge_client_report_export(")
    assert readiness_index < idempotency_index < renderer_index < temp_file_index < writer_index


def _load_v2_pdf_endpoint(monkeypatch, record):
    """Execute only the real safe PDF endpoint without importing server startup."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "get_diagnosis_v2_pdf"
    )
    cloned = copy.deepcopy(node)
    cloned.decorator_list = []
    module = ast.Module(body=[cloned], type_ignores=[])
    ast.fix_missing_locations(module)

    logger = SimpleNamespace(
        warning=lambda *_args, **_kwargs: None,
        error=lambda *_args, **_kwargs: None,
        exception=lambda *_args, **_kwargs: None,
    )
    modules_jsonb = record.get("report_v2_modules_jsonb", {})
    namespace = {
        "__file__": str(root / "server.py"),
        "HTTPException": HTTPException,
        "Request": object,
        "BackgroundTasks": object,
        "get_diagnosis_by_id": lambda _diagnosis_id: record,
        "_gate_diagnosis_visibility": lambda _record: None,
        "_build_report_html_context": lambda _record: ({}, modules_jsonb, {}),
        "_build_v2_report_html": lambda _record, audience: "<html>safe</html>",
        "_resolve_report_branding": lambda *_args, **_kwargs: {},
        "logger": logger,
        "os": os,
    }
    exec(compile(module, str(root / "server.py"), "exec"), namespace)

    import auth.brand_access as brand_access
    import config.settings_manager as settings_manager
    import services.report_v3_gating as report_v3_gating

    monkeypatch.setattr(
        brand_access,
        "require_diagnosis_access",
        lambda _request, _diagnosis_id: None,
    )
    monkeypatch.setattr(settings_manager, "load_settings", lambda: {})
    monkeypatch.setattr(
        report_v3_gating,
        "resolve_report_v3_subject_user_id",
        lambda **_kwargs: 8,
    )
    monkeypatch.setattr(
        report_v3_gating,
        "is_admin_preview_request",
        lambda _request: False,
    )
    monkeypatch.setattr(
        report_v3_gating,
        "should_render_report_v3",
        lambda *_args, **_kwargs: False,
    )
    return namespace["get_diagnosis_v2_pdf"]


def test_temp_html_failure_happens_before_charge_and_returns_retryable_503(monkeypatch):
    import tempfile
    import services.report_export_billing as report_export_billing

    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "client": {"modules": {"1": {"rendered_md": "客户安全报告"}}}
        },
    }
    endpoint = _load_v2_pdf_endpoint(monkeypatch, record)
    charge_calls = []

    def fail_file_creation(*_args, **_kwargs):
        raise OSError("injected disk full")

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", fail_file_creation)
    monkeypatch.setattr(
        report_export_billing,
        "charge_client_report_export",
        lambda *_args, **_kwargs: charge_calls.append("client"),
    )
    monkeypatch.setattr(
        report_export_billing,
        "charge_report_export",
        lambda *_args, **_kwargs: charge_calls.append("other"),
    )

    with pytest.raises(HTTPException) as exc_info:
        endpoint(
            881,
            SimpleNamespace(
                state=SimpleNamespace(user={"user_id": 8, "is_admin": False}),
                headers={
                    "X-Report-Export-Idempotency-Key": "report-export:881:attempt-test-881"
                },
            ),
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail["code"] == "REPORT_EXPORT_FILE_PREPARATION_FAILED"
    assert exc_info.value.detail["message"] == "报告文件暂时无法创建，请稍后重试。"
    assert exc_info.value.detail["request_id"].startswith("report-export-")
    assert charge_calls == []


def test_v1_client_pdf_is_rejected_before_v3_gate_renderer_or_charge(monkeypatch):
    import services.report_export_billing as report_export_billing
    import services.report_v3_gating as report_v3_gating

    record = {
        "report_v2_version": "v1",
        "report_v2_modules_jsonb": {},
        "report": "INTERNAL_LEGACY_MARKDOWN",
    }
    endpoint = _load_v2_pdf_endpoint(monkeypatch, record)
    calls = []

    monkeypatch.setattr(
        report_v3_gating,
        "should_render_report_v3",
        lambda *_args, **_kwargs: calls.append("v3_gate") or True,
    )
    monkeypatch.setattr(
        report_export_billing,
        "charge_client_report_export",
        lambda *_args, **_kwargs: calls.append("client_charge"),
    )
    monkeypatch.setattr(
        report_export_billing,
        "charge_report_export",
        lambda *_args, **_kwargs: calls.append("other_charge"),
    )

    with pytest.raises(HTTPException) as exc_info:
        endpoint(
            880,
            SimpleNamespace(
                state=SimpleNamespace(user={"user_id": 8, "is_admin": False}),
                headers={},
            ),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "CLIENT_REPORT_NOT_READY"
    assert calls == []


def test_real_pdf_endpoint_hands_unconfirmed_post_charge_refund_to_durable_queue(
    monkeypatch,
):
    import subprocess
    import db.fund_recovery_db as fund_recovery_db
    import middleware.billing as billing
    import services.report_export_billing as report_export_billing
    from services.report_export_billing import ReportExportCharge

    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "client": {"modules": {"1": {"rendered_md": "客户安全报告"}}}
        },
    }
    endpoint = _load_v2_pdf_endpoint(monkeypatch, record)
    recovery_calls = []
    charge = ReportExportCharge(
        user_id=8,
        diagnosis_id=882,
        charged=True,
        charge_tx_id=3882,
        ledger_type="legacy",
        deducted=10,
    )

    monkeypatch.setattr(
        report_export_billing,
        "charge_client_report_export",
        lambda *_args, **_kwargs: charge,
    )

    async def refund_unconfirmed(*_args, **_kwargs):
        return {"success": False, "reason": "injected refund outage"}

    monkeypatch.setattr(billing, "refund_points", refund_unconfirmed)
    monkeypatch.setattr(
        fund_recovery_db,
        "create_recovery_order",
        lambda source, kind, **kwargs: (
            recovery_calls.append((source, kind, kwargs)) or 7882
        ),
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1,
            stderr="injected node failure",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        endpoint(
            882,
            SimpleNamespace(
                state=SimpleNamespace(user={"user_id": 8, "is_admin": False}),
                headers={
                    "X-Report-Export-Idempotency-Key": "report-export:882:attempt-test-882"
                },
            ),
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["code"] == "REPORT_EXPORT_GENERATION_FAILED"
    assert "injected node failure" not in str(exc_info.value.detail)
    assert charge.charged is True
    assert charge.recovery_order_id == 7882
    assert recovery_calls[0][0:2] == ("report_export", "refund")
    assert recovery_calls[0][2]["ref_key"] == "diagnosis:882"
    assert recovery_calls[0][2]["charge_tx_id"] == 3882
    assert recovery_calls[0][2]["ledger_type"] == "legacy"
    assert recovery_calls[0][2]["amount_points"] == 10


def test_real_pdf_endpoint_never_exposes_subprocess_or_exception_details(monkeypatch):
    import subprocess
    import services.report_export_billing as report_export_billing
    from services.report_export_billing import ReportExportCharge

    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "client": {"modules": {"1": {"rendered_md": "客户安全报告"}}}
        },
    }
    endpoint = _load_v2_pdf_endpoint(monkeypatch, record)
    monkeypatch.setattr(
        report_export_billing,
        "charge_client_report_export",
        lambda *_args, **_kwargs: ReportExportCharge(
            user_id=8,
            diagnosis_id=883,
            charged=False,
        ),
    )
    secret = r"C:\\internal\\private-script.js TOKEN_INTERNAL_ONLY"
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stderr=secret),
    )

    with pytest.raises(HTTPException) as exc_info:
        endpoint(
            883,
            SimpleNamespace(
                state=SimpleNamespace(user={"user_id": 8, "is_admin": False}),
                headers={
                    "X-Report-Export-Idempotency-Key": "report-export:883:attempt-test-883"
                },
            ),
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["code"] == "REPORT_EXPORT_GENERATION_FAILED"
    assert exc_info.value.detail["message"] == "报告生成失败，请稍后重试。"
    assert exc_info.value.detail["request_id"].startswith("report-export-")
    assert secret not in str(exc_info.value.detail)


def test_real_pdf_endpoint_never_exposes_billing_contract_details(monkeypatch):
    import services.report_export_billing as report_export_billing

    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "client": {"modules": {"1": {"rendered_md": "客户安全报告"}}}
        },
    }
    endpoint = _load_v2_pdf_endpoint(monkeypatch, record)
    secret = "BILLING_INTERNAL_TABLE_AND_PATH_ONLY"

    def fail_billing(*_args, **_kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(
        report_export_billing,
        "charge_client_report_export",
        fail_billing,
    )

    with pytest.raises(HTTPException) as exc_info:
        endpoint(
            884,
            SimpleNamespace(
                state=SimpleNamespace(user={"user_id": 8, "is_admin": False}),
                headers={
                    "X-Report-Export-Idempotency-Key": "report-export:884:attempt-test-884"
                },
            ),
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail["code"] == "REPORT_EXPORT_BILLING_FAILED"
    assert exc_info.value.detail["message"] == "导出扣费状态暂时无法确认，请稍后重试。"
    assert exc_info.value.detail["request_id"].startswith("report-export-")
    assert secret not in str(exc_info.value.detail)


def test_active_frontend_legacy_export_urls_are_guarded_before_side_effects():
    root = Path(__file__).resolve().parents[1]
    frontend = (
        root / "frontend/src/pages/Diagnosis/ExportReportModal.tsx"
    ).read_text(encoding="utf-8")
    server = (root / "server.py").read_text(encoding="utf-8")

    assert "/api/diagnosis/${diagnosisId}/export/pptx" not in frontend
    assert "/api/diagnosis/${diagnosisId}/export/pdf?theme=${selectedTheme}" not in frontend
    assert "/api/diagnosis/${diagnosisId}/report-v2.pdf?theme=${selectedTheme}" in frontend
    assert "PPTX（即将开放）" in frontend
    assert "10 页专业演示文稿" not in frontend
    assert "X-Report-Export-Idempotency-Key" in frontend

    pptx_start = server.index("def export_diagnosis_pptx(")
    pdf_start = server.index("def export_diagnosis_pdf(", pptx_start)
    pptx_block = server[pptx_start:pdf_start]
    pdf_end = server.index("\n\n# ==========================================", pdf_start)
    pdf_block = server[pdf_start:pdf_end]

    pptx_guard = pptx_block.index("_require_ready_client_v2_export(record)")
    pptx_terminal = pptx_block.index('"code": CLIENT_REPORT_EXPORT_UNSUPPORTED_CODE')
    assert pptx_guard < pptx_terminal
    for forbidden in ("deduct_points", "_get_diagnosis_md_content", "extract_report_data"):
        assert forbidden not in pptx_block

    pdf_guard = pdf_block.index("_require_ready_client_v2_export(record)")
    pdf_redirect = pdf_block.index("return RedirectResponse(")
    assert pdf_guard < pdf_redirect
    for forbidden in ("deduct_points", "_get_diagnosis_md_content", "extract_report_data"):
        assert forbidden not in pdf_block
    assert "/report-v2.pdf?theme={theme}" in pdf_block


def _load_active_export_functions(monkeypatch, record):
    """Execute the real endpoint functions without importing startup-heavy server."""
    root = Path(__file__).resolve().parents[1]
    source = (root / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {
        "_require_ready_client_v2_export",
        "export_diagnosis_pptx",
        "export_diagnosis_pdf",
    }
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            cloned = copy.deepcopy(node)
            cloned.decorator_list = []
            nodes.append(cloned)
    assert {node.name for node in nodes} == wanted

    calls = []
    namespace = {
        "HTTPException": HTTPException,
        "Request": object,
        "BackgroundTasks": object,
        "get_diagnosis_by_id": lambda diagnosis_id: (
            calls.append(("load", diagnosis_id)) or record
        ),
        "_gate_diagnosis_visibility": lambda loaded_record: calls.append(
            ("visibility", loaded_record)
        ),
    }
    module = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(root / "server.py"), "exec"), namespace)

    import auth.brand_access as brand_access

    monkeypatch.setattr(
        brand_access,
        "require_diagnosis_access",
        lambda _request, diagnosis_id: calls.append(("access", diagnosis_id)),
    )
    return namespace, calls


@pytest.mark.parametrize("endpoint_name", ["export_diagnosis_pdf", "export_diagnosis_pptx"])
def test_active_legacy_export_endpoints_reject_partial_customer_report(
    monkeypatch, endpoint_name
):
    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "client": {"modules": {"3_raw": {"tests": []}}},
            "internal": {
                "modules": {"1": {"rendered_md": "INTERNAL_ONLY_MARKDOWN"}}
            },
        },
        "report": "INTERNAL_LEGACY_MARKDOWN",
    }
    namespace, calls = _load_active_export_functions(monkeypatch, record)

    with pytest.raises(HTTPException) as exc_info:
        namespace[endpoint_name](
            77,
            SimpleNamespace(state=SimpleNamespace(user={"user_id": 8})),
            SimpleNamespace(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail["code"] == "CLIENT_REPORT_NOT_READY"
    assert calls[:3] == [("access", 77), ("load", 77), ("visibility", record)]


def test_active_pdf_endpoint_redirects_ready_customer_report_to_safe_writer(monkeypatch):
    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "client": {"modules": {"1": {"rendered_md": "客户安全报告"}}},
            "internal": {
                "modules": {"1": {"rendered_md": "INTERNAL_ONLY_MARKDOWN"}}
            },
        },
        "report": "INTERNAL_LEGACY_MARKDOWN",
    }
    namespace, calls = _load_active_export_functions(monkeypatch, record)

    response = namespace["export_diagnosis_pdf"](
        78,
        SimpleNamespace(state=SimpleNamespace(user={"user_id": 8})),
        SimpleNamespace(),
        theme="light_corporate",
    )

    assert response.status_code == 307
    assert response.headers["location"] == (
        "/api/diagnosis/78/report-v2.pdf?theme=light_corporate"
    )
    assert calls[:3] == [("access", 78), ("load", 78), ("visibility", record)]


def test_active_pptx_endpoint_returns_structured_unsupported_after_readiness(monkeypatch):
    record = {
        "report_v2_version": "v2",
        "report_v2_modules_jsonb": {
            "client": {"modules": {"1": {"rendered_md": "客户安全报告"}}}
        },
    }
    namespace, _calls = _load_active_export_functions(monkeypatch, record)

    with pytest.raises(HTTPException) as exc_info:
        namespace["export_diagnosis_pptx"](
            79,
            SimpleNamespace(state=SimpleNamespace(user={"user_id": 8})),
            SimpleNamespace(),
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == {
        "code": "CLIENT_REPORT_EXPORT_UNSUPPORTED",
        "message": "PPTX 客户报告暂不支持安全导出，请先使用 PDF。",
    }


def test_export_modal_reads_structured_detail_code_and_message():
    source = (
        Path(__file__).resolve().parents[1]
        / "frontend/src/pages/Diagnosis/ExportReportModal.tsx"
    ).read_text(encoding="utf-8")

    assert 'CLIENT_REPORT_NOT_READY: "客户报告尚未就绪，请稍后重试。"' in source
    assert (
        'CLIENT_REPORT_EXPORT_UNSUPPORTED: "PPTX 客户报告暂不支持安全导出，请先使用 PDF。"'
        in source
    )
    assert 'typeof detail?.code === "string"' in source
    assert 'typeof detail?.message === "string"' in source
    assert "getExportErrorMessage(errorData, response.status)" in source
    assert "inMemoryExportAttemptKeys" in source
    assert "shouldClearExportAttemptAfterError(errorData)" in source
    assert 'code === "IDEMPOTENCY_CHARGE_REFUNDED"' in source
    assert '"IDEMPOTENCY_CHARGE_REFUND_PENDING"' not in source.split(
        "CLEARABLE_UNCHARGED_EXPORT_CODES", 1
    )[1].split("]);", 1)[0]
