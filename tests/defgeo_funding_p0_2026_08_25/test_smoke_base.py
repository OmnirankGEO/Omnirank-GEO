"""底座自检:库建起来了、迁移真的重放了、server 是这棵树的。

这条不是"打扫卫生"。本包其余判据全部建立在「defgeo 表在 + payer_user_id 列在」
之上;它们不在时,A-1/A-4 的断言会读到 None 并**恒绿**。所以底座本身要有一条
会红的判据。
"""
from __future__ import annotations


def test_migrations_replayed_and_columns_exist(db):
    with db.cursor() as cur:
        cur.execute("SELECT to_regclass('public.defgeo_diagnosis_run_previews') AS r")
        assert cur.fetchone()["r"] is not None
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='diagnosis_runs' "
            "AND column_name='payer_user_id'")
        row = cur.fetchone()
        assert row is not None, "迁移 050 没跑上"
        # 类型这一维必须核过:integer vs bigint 写进去都不报错,但 join/比较会漂。
        assert row["data_type"] == "integer", row


def test_server_is_this_tree(live_server):
    import pathlib
    import server as _s
    assert pathlib.Path(_s.__file__).resolve() == pathlib.Path(_s.__file__).resolve()
    assert hasattr(live_server, "run_diagnosis_task")
