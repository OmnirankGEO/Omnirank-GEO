from __future__ import annotations

import ast
from contextlib import contextmanager
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load_diagnosis_db_helper(name: str):
    source = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    module = ast.parse(source)
    node = next(
        item for item in module.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == name
    )
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), "diagnosis_db_helper", "exec"), namespace)
    return namespace[name]


class _Cursor:
    def __init__(self, row):
        self.row = row
        self.sql = ""
        self.params = None

    def execute(self, sql, params=None):
        self.sql = " ".join(sql.split())
        self.params = params

    def fetchone(self):
        return self.row


class _SequenceCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        self.executed.append((" ".join(sql.split()), params))
        self.rowcount = 1

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor


def _get_db(cursor):
    @contextmanager
    def factory():
        yield _Connection(cursor)

    return factory


def test_complete_product_requires_exact_identity_and_nonempty_score(monkeypatch):
    from db import connection
    from services import diagnosis_runs

    cursor = _Cursor({
        "id": 464,
        "brand_id": 592,
        "report_v2_modules_jsonb": {
            "client": {"modules": {"1": {"insight": "客户结论"}}}
        },
    })
    monkeypatch.setattr(connection, "get_db", _get_db(cursor))

    row = diagnosis_runs.require_complete_product("session-1", 464)

    assert row["id"] == 464
    assert row["brand_id"] == 592
    assert cursor.params == (464, "session-1")
    assert "id=%s AND session_id=%s AND total_score IS NOT NULL" in cursor.sql


def test_empty_placeholder_cannot_satisfy_completion_gate(monkeypatch):
    from db import connection
    from services import diagnosis_runs

    cursor = _Cursor(None)
    monkeypatch.setattr(connection, "get_db", _get_db(cursor))

    with pytest.raises(RuntimeError, match="incomplete or identity mismatch"):
        diagnosis_runs.require_complete_product("session-shell", 464)


def test_score_only_product_cannot_satisfy_customer_readiness(monkeypatch):
    from db import connection
    from services import diagnosis_runs

    cursor = _Cursor({"id": 464, "brand_id": 592, "report_v2_modules_jsonb": {}})
    monkeypatch.setattr(connection, "get_db", _get_db(cursor))

    with pytest.raises(RuntimeError, match="customer report not ready"):
        diagnosis_runs.require_complete_product("session-shell", 464)


def test_missing_completion_identity_fails_before_database_lookup(monkeypatch):
    from db import connection
    from services import diagnosis_runs

    monkeypatch.setattr(
        connection,
        "get_db",
        lambda: (_ for _ in ()).throw(AssertionError("missing identity must fail first")),
    )
    with pytest.raises(RuntimeError, match="identity missing"):
        diagnosis_runs.require_complete_product("session-shell", None)


def test_production_smallint_contract_and_workflow_failure_propagation():
    db_source = (ROOT / "db" / "diagnosis_db.py").read_text(encoding="utf-8")
    workflow_source = (ROOT / "workflows" / "diagnosis_workflow.py").read_text(encoding="utf-8")

    assert "has_brand_presence SMALLINT" in db_source
    assert "data_anomaly SMALLINT" in db_source
    assert 'data_type == "boolean"' in db_source
    assert 'data_type == "smallint"' in db_source
    assert "has_brand_presence BOOLEAN" not in db_source
    assert "data_anomaly BOOLEAN" not in db_source
    assert workflow_source.count('print(f"⚠️ 数据库保存失败: {e}")\n            raise') == 1
    assert workflow_source.count('print(f"  ⚠️ 数据库保存失败: {e}")\n        raise') == 1


@pytest.mark.parametrize(
    ("data_type", "present", "expected", "expected_type"),
    [
        ("boolean", True, True, bool),
        ("boolean", False, False, bool),
        ("smallint", True, 1, int),
        ("smallint", False, 0, int),
    ],
)
def test_brand_presence_binding_supports_legacy_and_canonical_schema(
    data_type, present, expected, expected_type
):
    _coerce_brand_presence_for_schema = _load_diagnosis_db_helper(
        "_coerce_brand_presence_for_schema"
    )

    cursor = _Cursor({"data_type": data_type})
    value = _coerce_brand_presence_for_schema(cursor, present)

    assert value == expected
    assert type(value) is expected_type
    assert "to_regclass('diagnosis_records')" in cursor.sql


def test_brand_presence_binding_rejects_unknown_schema():
    _coerce_brand_presence_for_schema = _load_diagnosis_db_helper(
        "_coerce_brand_presence_for_schema"
    )

    with pytest.raises(RuntimeError, match="schema mismatch"):
        _coerce_brand_presence_for_schema(_Cursor({"data_type": "integer"}), True)


@pytest.mark.parametrize(
    ("data_type", "anomalous", "expected", "expected_type"),
    [
        ("boolean", True, True, bool),
        ("boolean", False, False, bool),
        ("smallint", True, 1, int),
        ("smallint", False, 0, int),
    ],
)
def test_data_anomaly_binding_supports_legacy_and_canonical_schema(
    data_type, anomalous, expected, expected_type
):
    coerce = _load_diagnosis_db_helper("_coerce_data_anomaly_for_schema")

    cursor = _Cursor({"data_type": data_type})
    value = coerce(cursor, anomalous)

    assert value == expected
    assert type(value) is expected_type
    assert "a.attname = 'data_anomaly'" in cursor.sql


def test_data_anomaly_binding_rejects_unknown_schema():
    coerce = _load_diagnosis_db_helper("_coerce_data_anomaly_for_schema")

    with pytest.raises(RuntimeError, match="data_anomaly schema mismatch"):
        coerce(_Cursor({"data_type": "integer"}), True)


def test_server_checks_durable_product_before_any_settlement():
    source = (ROOT / "server.py").read_text(encoding="utf-8")
    start = source.index("async def run_diagnosis_task(")
    end = source.index("async def _run_diagnosis_impl(", start)
    block = source[start:end]

    complete_gate = block.index("_dr.require_complete_product")
    pending_stamp = block.index("_dr.mark_product_pending")
    dispatch = block.index("_dr.dispatch_success_settlement")
    assert complete_gate < pending_stamp < dispatch
    assert "SELECT id FROM diagnosis_records WHERE session_id=%s LIMIT 1" not in block

    # [P0-3c 2026-08-25] 这条锁原来在**同一个 block 里**依次找
    #   require_complete_product < mark_product_pending < _settle_org_charge < commit_run。
    #   件2 把 org/非 org 的结算分派抽进了 services/diagnosis_runs.py
    #   (`dispatch_success_settlement`),后两个锚点因此离开了 run_diagnosis_task ——
    #   **锁的分段边界被搬家移动了**,原写法直接 ValueError。
    #
    #   它保护的安全属性没变:**任何结算动作之前都必须先过产物证明**。
    #   所以这里让锁跟着代码走,把顺序保证在新宿主里继续钉住,
    #   而不是把断言删成"能过就行"。
    #   (P0-3c 配了一发变异:把 org 分支的 require_complete_product 挪到
    #    settle_charge 之后 —— 下面这段必须红。)
    runs = (ROOT / "services" / "diagnosis_runs.py").read_text(encoding="utf-8")
    d_start = runs.index("async def dispatch_success_settlement(")
    d_end = runs.index("# sweeper(§3.4)", d_start)
    d_block = runs[d_start:d_end]
    org_gate = d_block.index("await asyncio.to_thread(require_complete_product")
    organization_commit = d_block.index("await _settle_org_charge(")
    legacy_commit = d_block.index("return await commit_run(")
    assert org_gate < organization_commit < legacy_commit


def test_terminal_publish_targets_only_complete_product_row():
    source = (ROOT / "services" / "diagnosis_runs.py").read_text(encoding="utf-8")
    start = source.index("def _terminal_local_txn(")
    end = source.index("async def _do_settlement(", start)
    block = source[start:end]

    assert "WHERE id=%s AND session_id=%s FOR UPDATE" in block
    assert "is_client_report_ready" in block
    assert "UPDATE diagnosis_records SET result_visibility=%s WHERE id=%s" in block
    assert "if visibility == \"published\" and vis_rows == 0" in block
    assert "ORDER BY id DESC" not in block


def test_pending_stamp_and_retry_keep_the_frozen_product_identity():
    source = (ROOT / "services" / "diagnosis_runs.py").read_text(encoding="utf-8")
    mark_start = source.index("def mark_product_pending(")
    mark_end = source.index("def require_complete_product(", mark_start)
    mark_block = source[mark_start:mark_end]
    sweep_start = source.index("async def run_diagnosis_sweep(")
    sweep_block = source[sweep_start:]

    assert "WHERE id=%s AND session_id=%s" in mark_block
    assert "final_snapshot_jsonb=%s" in mark_block
    assert "diagnosis run product anchor conflict" in mark_block
    assert "不影响产物" not in mark_block
    assert "SELECT run_token, run_status, final_snapshot_jsonb" in sweep_block
    assert 'd.get("final_snapshot_jsonb") if intent == "commit"' in sweep_block


def test_release_snapshot_preserves_the_durable_product_identity():
    from services import diagnosis_runs

    merged = diagnosis_runs._merge_terminal_snapshot(
        {"diagnosis_id": 464, "type": "complete", "message": "ready"},
        {"type": "error", "message": "refunded"},
    )

    assert merged["diagnosis_id"] == 464
    assert merged["type"] == "error"
    assert merged["message"] == "refunded"
    with pytest.raises(RuntimeError, match="identity conflict"):
        diagnosis_runs._merge_terminal_snapshot(
            {"diagnosis_id": 464},
            {"diagnosis_id": 999},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "durable_snapshot",
    [None, "not-json", {}, {"diagnosis_id": 464}],
)
async def test_commit_retry_revalidates_product_before_billing_commit(
    monkeypatch, durable_snapshot
):
    from middleware import billing
    from services import diagnosis_runs

    state = {"status": "commit_pending"}
    calls = {"commit": 0, "release": 0, "validated": []}

    def get_run(_run_token):
        return {
            "run_token": "run-1",
            "session_id": "session-1",
            "run_status": state["status"],
            "owner_user_id": 132,
            "freeze_id": 156,
            "freeze_backend": "legacy",
            "freeze_task_ref": "diag_run-1",
            "final_snapshot_jsonb": durable_snapshot,
        }

    def require_complete(session_id, diagnosis_id, run_token=None):
        calls["validated"].append((session_id, diagnosis_id, run_token))
        raise RuntimeError("customer report not ready")

    def cas(_token, from_states, to_state, _extra=None, finish=False):
        assert state["status"] in from_states
        state["status"] = to_state
        return True

    async def commit_freeze(**_kwargs):
        calls["commit"] += 1
        return {"success": True}

    async def release_freeze(**_kwargs):
        calls["release"] += 1
        return {"success": True}

    monkeypatch.setattr(diagnosis_runs, "get_run", get_run)
    monkeypatch.setattr(diagnosis_runs, "require_complete_product", require_complete)
    monkeypatch.setattr(diagnosis_runs, "_cas", cas)
    monkeypatch.setattr(
        diagnosis_runs,
        "_terminal_local_txn",
        lambda *_args, **_kwargs: (True, "released"),
    )
    monkeypatch.setattr(billing, "commit_freeze", commit_freeze)
    monkeypatch.setattr(billing, "release_freeze", release_freeze)

    result = await diagnosis_runs._do_settlement(
        "run-1", "commit", {"type": "complete", "diagnosis_id": 464}
    )

    assert result == {"ok": True, "terminal": "released"}
    assert calls["commit"] == 0
    assert calls["release"] == 1
    if durable_snapshot == "not-json":
        assert calls["validated"] == []
    elif durable_snapshot == {"diagnosis_id": 464}:
        assert calls["validated"] == [("session-1", 464, "run-1")]
    else:
        assert calls["validated"] == [("session-1", None, "run-1")]


def test_legacy_delivery_repair_requires_explicit_exact_product_binding(monkeypatch):
    from db import connection
    from services import diagnosis_runs

    cursor = _SequenceCursor([
        {
            "session_id": "session-1",
            "run_status": "delivery_repair_pending",
            "last_settlement_error": None,
            "owner_user_id": 132,
            "billing_mode": "paid",
            "freeze_id": 156,
            "freeze_backend": "legacy",
            "freeze_task_ref": "diag_run-1",
            "final_snapshot_jsonb": None,
        },
        {
            "id": 464,
            "report_v2_modules_jsonb": {
                "client": {"modules": {"1": {"insight": "客户结论"}}}
            },
            "run_token": None,
        },
    ])
    monkeypatch.setattr(connection, "get_db", _get_db(cursor))
    monkeypatch.setattr(diagnosis_runs, "_write_settlement_audit", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_runs, "_alert", lambda *a, **k: None)

    out = diagnosis_runs.repair_delivery(
        "run-1", "admin(uid=1)", "republish", "核验不可变 JSON 后恢复", diagnosis_id=464
    )

    assert out["ok"] is True
    assert out["decision"] == "republish"
    sql = "\n".join(statement for statement, _ in cursor.executed)
    assert "UPDATE diagnosis_runs SET final_snapshot_jsonb=%s" in sql
    assert "UPDATE diagnosis_records SET run_token=%s" in sql
    assert "UPDATE diagnosis_records SET result_visibility='published' WHERE id=%s" in sql


def test_legacy_delivery_repair_never_guesses_by_session(monkeypatch):
    from db import connection
    from services import diagnosis_runs

    cursor = _SequenceCursor([{
        "session_id": "session-1",
        "run_status": "delivery_repair_pending",
        "last_settlement_error": None,
        "owner_user_id": 132,
        "billing_mode": "paid",
        "freeze_id": 156,
        "freeze_backend": "legacy",
        "freeze_task_ref": "diag_run-1",
        "final_snapshot_jsonb": None,
    }])
    monkeypatch.setattr(connection, "get_db", _get_db(cursor))

    out = diagnosis_runs.repair_delivery(
        "run-1", "admin(uid=1)", "republish", "缺少明确对象"
    )

    assert out["ok"] is False
    assert "显式提交" in out["error"]
    assert all("ORDER BY" not in statement for statement, _ in cursor.executed)


def test_release_with_missing_anchored_row_never_withholds_replacement_session_row(monkeypatch):
    from db import connection
    from services import diagnosis_runs

    cursor = _SequenceCursor([
        {
            "session_id": "session-1",
            "run_status": "released",
            "owner_user_id": 132,
            "final_snapshot_jsonb": {"diagnosis_id": 464},
        },
        None,
    ])
    monkeypatch.setattr(connection, "get_db", _get_db(cursor))
    monkeypatch.setattr(diagnosis_runs, "_enqueue_diagnosis_run_terminal", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_runs, "_alert", lambda *a, **k: None)
    monkeypatch.setattr(diagnosis_runs, "_write_settlement_audit", lambda *a, **k: None)

    ok, terminal = diagnosis_runs._terminal_local_txn(
        "run-1",
        ["release_pending"],
        "released",
        "withheld",
        {"type": "error", "message": "费用已退"},
    )

    assert ok is True
    assert terminal == "released"
    statements = [statement for statement, _ in cursor.executed]
    assert not any(
        statement.startswith("UPDATE diagnosis_records SET result_visibility=%s WHERE session_id=%s")
        for statement in statements
    )
