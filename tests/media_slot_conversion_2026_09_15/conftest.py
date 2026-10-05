# -*- coding: utf-8 -*-
"""WO_225-c1 判据底座:真库 + 迁移 061 自愈 + 每个场景一张干净单。

🔴 为什么每个场景**换一张报价单**而不是复用一张改快照:
   `quote_pricing_snapshots` 有 append-only 触发器
   (`reject_quote_pricing_snapshot_mutation`),删不掉也改不了。
   这是生产的真实约束;夹具绕过它就等于在测一个不存在的库。

🔴 为什么 conftest 自己跑一遍迁移 061:
   本包要能在**任何**一份 prod schema 测试库上跑。库里有没有那一列不该由
   「跑之前有没有人手动 psql 过」决定 —— 那正是「两边都以为对方在跑 ⇒ 谁都没跑」。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / "db" / "migration_061_media_slot_conversion_2026_09_15.sql"

BASE_QUOTE_ID = 991000


def _dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("TEST_DATABASE_URL 未设置")
    return dsn.split("?", 1)[0]


@pytest.fixture(scope="session", autouse=True)
def _migrated():
    """迁移 061 就地重放一次(全 IF NOT EXISTS,重放是空操作)。"""
    conn = psycopg2.connect(_dsn())
    conn.autocommit = True
    try:
        conn.cursor().execute(MIGRATION.read_text(encoding="utf-8"))
    finally:
        conn.close()
    from services.media_slot_conversion import ensure_seeded
    os.environ["DATABASE_URL"] = _dsn()
    import db.connection as dbc
    dbc.DATABASE_URL = _dsn()
    dbc._pool = None
    ensure_seeded()
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
def make_quote(cur):
    """建一张干净的报价单,返回 quote_id。

    `perspective=None` ⇒ 不写快照 = 老单(没有任何换算口径)。
    """
    made = []

    def _make(n: int, perspective=None, required=7, mix=None, posts_estimate=None,
              paid=True, conversion_version=1, keywords=1):
        # 🔴 每次跑都取**没被用过的** id,不复用固定 id。
        #    固定 id 要求每次先删干净,而 `quote_pricing_snapshots` 是 append-only
        #    (触发器拦 DELETE/UPDATE),`keyword_selection_sessions` 又有外键指着 quotes ——
        #    结果是**第一次跑绿(库里还没东西,删什么都成功),第二次起红**。
        #    实测踩过:注毒 harness 的基线检查第一时间把它照出来了。
        cur.execute("SELECT COALESCE(MAX(id), %s) + 1 AS nid FROM quotes WHERE id >= %s",
                    (BASE_QUOTE_ID, BASE_QUOTE_ID))
        qid = int(dict(cur.fetchone())["nid"])
        made.append(qid)
        cur.execute("INSERT INTO brands (id, name, owner_user_id) VALUES (%s,%s,1)",
                    (qid, "WO225 判据品牌 %s" % qid))
        cur.execute(
            "INSERT INTO quotes (id, brand_id, status, service_status, paid_amount, "
            "owner_user_id) VALUES (%s,%s,'paid',%s,%s,1)",
            (qid, qid, "active" if paid else None, 100 if paid else 0))
        cur.execute(
            "INSERT INTO keyword_selection_sessions (id, brand_id, quote_id, token, "
            "keywords_snapshot, expires_at) VALUES (%s,%s,%s,%s,'[]','2099-01-01T00:00:00Z')",
            (qid, qid, qid, "wo225lock%s" % qid))
        for _i in range(int(keywords)):
            # `required=None` ⇒ 真的写 NULL(不是 0)——
            # 生产里 `confirmed_keywords.required_articles` 是可空列,
            # 而 NULL 与 0 在本单里是**两种语义**(NULL ⇒ 1 / 显式 0 ⇒ 0)。
            cur.execute(
                "INSERT INTO confirmed_keywords (quote_id, keyword, required_articles, "
                "is_core) VALUES (%s,%s,%s,TRUE)",
                (qid, "判据词%d" % _i,
                 None if required is None else int(required)))
        if perspective is not None:
            mm = {"delivery_perspective": perspective,
                  "conversion_version": int(conversion_version)}
            if mix:
                mm["mix"] = mix
            if posts_estimate:
                mm["posts_estimate"] = posts_estimate
            cur.execute(
                "INSERT INTO quote_pricing_snapshots (quote_id, brand_id, "
                "selection_session_id, version, reason, calculation_version, "
                "pricing_snapshot, snapshot_hash) VALUES (%s,%s,%s,1,'wo225','t',%s,%s)",
                (qid, qid, qid, json.dumps({"media_mix": mm}), "0" * 64))
        return qid
    yield _make
    for qid in made:
        try:
            cur.execute("DELETE FROM topics WHERE quote_id=%s", (qid,))
        except Exception:  # noqa: BLE001
            pass


@pytest.fixture
def add_topics(cur):
    """给一张单加 n 条指定状态/桶的选题。"""
    def _add(quote_id: int, status: str, bucket, n: int):
        for _ in range(n):
            cur.execute(
                "INSERT INTO topics (quote_id, status, media_bucket, original_keyword) "
                "VALUES (%s,%s,%s,'k')", (quote_id, status, bucket))
    return _add
