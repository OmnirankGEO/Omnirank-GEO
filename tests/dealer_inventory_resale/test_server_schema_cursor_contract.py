import ast
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


#   [守卫单一来源 2026-07-30] 守卫实现自 server.py 迁到 services/startup_schema_guards.py
#   (server.py 与 scripts/prestart.py 现在共用同一份清单)。本锁跟着搬,守的性质不变:
#   转售 schema 守卫必须用 mapping cursor,并按列名读 flag。
GUARD_MODULE = ROOT / "services" / "startup_schema_guards.py"


def _load_schema_guard():
    tree = ast.parse(GUARD_MODULE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef)
        and item.name == "verify_dealer_resale_schema_fail_closed"
    )
    namespace = {"logger": types.SimpleNamespace(info=lambda *args, **kwargs: None)}
    exec(
        compile(ast.Module(body=[node], type_ignores=[]), str(GUARD_MODULE), "exec"),
        namespace,
    )
    return namespace[node.name]


def test_web_schema_guard_uses_mapping_cursor_and_reads_flag_by_name(monkeypatch):
    from services import dealer_inventory_resale

    cursor_factory = object()
    connect_calls = []

    class Cursor:
        def execute(self, sql, params=None):
            self.sql = sql

        def fetchone(self):
            assert "DEALER_INVENTORY_RESALE_ENABLED" in self.sql
            return {"value": "false"}

    class Connection:
        autocommit = False

        def cursor(self):
            return Cursor()

        def close(self):
            pass

    psycopg2 = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = cursor_factory

    def connect(database_url, **kwargs):
        connect_calls.append((database_url, kwargs))
        return Connection()

    psycopg2.connect = connect
    psycopg2.extras = extras
    monkeypatch.setitem(sys.modules, "psycopg2", psycopg2)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    monkeypatch.setenv("DATABASE_URL", "postgresql://schema-contract-test")

    monkeypatch.setattr(
        dealer_inventory_resale,
        "purchase_agreement_schema_status",
        lambda cur: {"ready": True, "blockers": []},
    )

    _load_schema_guard()()

    assert connect_calls == [
        (
            "postgresql://schema-contract-test",
            {"cursor_factory": cursor_factory},
        )
    ]
