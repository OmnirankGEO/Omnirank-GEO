"""
tests/system_kb/test_kb_migration.py

Task 1：验证 kb_chunks 支持 sys_* source_type + origin 列 + clear_system_chunks()
"""
from db.kb_db import init_kb_tables, insert_chunk, load_all_chunks, clear_system_chunks, _get_conn


def test_sys_chunk_can_insert_and_has_origin():
    init_kb_tables()
    clear_system_chunks()
    cid = insert_chunk(
        source_type="sys_page",
        source_slug="/pricing",
        source_title="报价方案",
        content="给客户生成三档报价。",
        route="/pricing",
        route_label="去报价",
        category="system",
        is_admin_only=False,
        token_keywords=["报价", "客户"],
        origin="manual",
    )
    assert cid
    rows = [c for c in load_all_chunks(include_admin=True) if c["source_type"] == "sys_page"]
    assert any(r["source_slug"] == "/pricing" and r.get("origin") == "manual" for r in rows)
    clear_system_chunks()


def test_init_kb_tables_is_idempotent():
    """init_kb_tables 连调两次不报错"""
    init_kb_tables()
    init_kb_tables()
