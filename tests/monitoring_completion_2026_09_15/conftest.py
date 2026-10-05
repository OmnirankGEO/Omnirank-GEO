# -*- coding: utf-8 -*-
"""WO_224-c1 判据底座:真库 + 每个场景一张干净 task。

🔴 用真库不用桩:本单要钉的是「**完成信号取自被服务方**」,
   而被服务方就是 `monitoring_results` / `monitoring_run_cells` 两张表。
   桩掉它们等于把要测的那一层换成我自己 —— WO_225 那次
   「桩掉的那层正是坏的那层」已经踩过。
"""
from __future__ import annotations

import json
import os
import uuid

import psycopg2
import psycopg2.extras
import pytest

BASE_TASK_ID = 970000


def _dsn() -> str:
    raw = os.environ.get("TEST_DATABASE_URL") or os.environ["DATABASE_URL"]
    return raw.split("?", 1)[0]


@pytest.fixture(scope="session", autouse=True)
def _db_env():
    os.environ["DATABASE_URL"] = _dsn()
    import db.connection as dbc
    dbc.DATABASE_URL = _dsn()
    dbc._pool = None
    yield


@pytest.fixture
def cur():
    conn = psycopg2.connect(_dsn(), cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    try:
        yield conn.cursor()
    finally:
        conn.close()


@pytest.fixture
def make_task(cur):
    """建一张干净的监测 task,返回 task_id。

    🔴 取 `MAX(id)+1` 的新 id,不复用固定 id ——
       固定 id + 先删后建在外键/不可变表面前只有第一次能过(WO_225 实测过)。
    """
    made = []

    def _make(platform_count: int = 4, status: str = "running"):
        cur.execute("SELECT COALESCE(MAX(id), %s) + 1 AS nid FROM monitoring_tasks WHERE id >= %s",
                    (BASE_TASK_ID, BASE_TASK_ID))
        tid = int(dict(cur.fetchone())["nid"])
        made.append(tid)
        # 🔴 `monitoring_run_cells` 有 fk_monitoring_run_cells_brand 指着 brands ——
        #    不建品牌行的话格子插不进去,而那会被误读成「这条路径没格子」,
        #    集合比对那几条锁就变成走了计数分支的假绿。
        cur.execute(
            "INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,1) "
            "ON CONFLICT (id) DO NOTHING", (tid, "WO224 判据品牌 %s" % tid))
        cur.execute(
            "INSERT INTO monitoring_tasks (id, client_id, brand_id, task_name, "
            "keyword_count, platform_count, total_tests, status, trigger_type) "
            "VALUES (%s,%s,%s,%s,1,%s,%s,%s,'manual_user')",
            (tid, str(tid), tid, "WO224 判据 %s" % tid, platform_count,
             max(platform_count, 1), status))
        return tid

    yield _make
    for tid in made:
        for tbl in ("monitoring_run_cells", "monitoring_results", "monitoring_tasks",
                    "brands"):
            try:
                key = "task_id"
                if tbl in ("monitoring_tasks", "brands"):
                    key = "id"
                cur.execute("DELETE FROM %s WHERE %s = %%s" % (tbl, key), (tid,))
            except Exception:  # noqa: BLE001
                pass


@pytest.fixture
def add_results(cur):
    """给 task 落结果行(被服务方)。"""
    def _add(task_id: int, platforms):
        for p in platforms:
            cur.execute(
                "INSERT INTO monitoring_results (task_id, keyword, platform, round_number) "
                "VALUES (%s,'判据词',%s,1)", (task_id, p))
    return _add


@pytest.fixture
def add_cells(cur):
    """给 task 建派发台账(`monitoring_run_cells`)。

    这张表 14 列 NOT NULL 且无默认 —— 逐列给值,不靠默认,
    否则夹具建不出来的那一刻会被误读成「这条路径没格子」。
    """
    def _add(task_id: int, platforms, is_planned: bool = True):
        for i, p in enumerate(platforms):
            cur.execute(
                "INSERT INTO monitoring_run_cells (task_id, brand_id, keyword_id, "
                "keyword_source, keyword_snapshot, question_snapshot, "
                "target_brand_snapshot, platform, is_planned, entitlement_snapshot, "
                "order_snapshot, fulfillment_credential, plan_hash) "
                "VALUES (%s,%s,%s,'confirmed','判据词','判据问法','判据品牌',%s,%s,%s,%s,%s,%s)",
                (task_id, task_id, task_id * 10 + i, p, is_planned,
                 json.dumps({"platforms": list(platforms)}),
                 json.dumps({"source": "wo224-lock"}),
                 str(uuid.uuid4()), "0" * 64))
    return _add
