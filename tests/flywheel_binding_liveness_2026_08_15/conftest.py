"""[P0-1/P0-2 2026-08-15] 飞轮绑定「陈旧候选死锁」用例的共享夹具。

🔴 夹具必须种**真实体 + 真库存行**:旧夹具(tests/flywheel_integration/test_t6_binding_approve_all.py)
   伪造 entity_key 与不存在的 inventory_id,在快照口径下照样全绿 —— 那正是本 bug 能活到生产的原因之一。
   判据同源之后,伪造夹具会当场转红,所以这里把真实链路种齐:
   geo_media_entities → media_entity_score_snapshots(按行业)→ mhz_media / mhz_wemedia → 候选行。
"""
from __future__ import annotations

import pytest

from db.connection import get_connection
from db.media_entity_flywheel_db import (
    init_media_entity_flywheel_tables,
    upsert_media_binding_candidate,
)

ENTITY_KEY = "me_livetest_flywheel"
ENTITY_DOMAIN = "livetest-media.example.com"
ENTITY_NAME = "实时判据测试媒体"
#: 库存 id 取高位段,避开生产/其他夹具占用的低位 id。
INV_BASE = 990100


def _exec(sql: str, params: tuple = ()) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _fetchone(sql: str, params: tuple = (), commit: bool = False):
    """commit=True 供 `INSERT ... RETURNING` 用 —— 不提交的话整条 INSERT 会随 close 回滚,
    表现为「拿到了 id 但外键说这行不存在」。"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        row = cur.fetchone()
        if commit:
            conn.commit()
        return row
    finally:
        conn.close()


def cleanup() -> None:
    _exec("DELETE FROM geo_media_binding_audit_events WHERE entity_key = %s", (ENTITY_KEY,))
    _exec("DELETE FROM geo_media_binding_candidates WHERE entity_key = %s", (ENTITY_KEY,))
    _exec(
        "DELETE FROM geo_media_inventory_mappings WHERE entity_id IN"
        " (SELECT id FROM geo_media_entities WHERE entity_key = %s)", (ENTITY_KEY,),
    )
    _exec(
        "DELETE FROM media_entity_score_snapshots WHERE entity_id IN"
        " (SELECT id FROM geo_media_entities WHERE entity_key = %s)", (ENTITY_KEY,),
    )
    _exec("DELETE FROM geo_media_entities WHERE entity_key = %s", (ENTITY_KEY,))
    _exec("DELETE FROM mhz_media WHERE id >= %s AND id < %s", (INV_BASE, INV_BASE + 1000))
    _exec("DELETE FROM mhz_wemedia WHERE id >= %s AND id < %s", (INV_BASE, INV_BASE + 1000))


def seed_entity(industries=("education", "auto", "finance")) -> int:
    """种一个媒体实体 + 每个行业一条影子评分快照。

    🔴 每个行业都要有 snapshot:`get_media_entity_for_binding` 是
       `LEFT JOIN ... WHERE s.industry_key = %s`,缺快照 = 整个实体取不到。
    """
    row = _fetchone(
        """
        INSERT INTO geo_media_entities (entity_key, canonical_name, domain, aliases)
        VALUES (%s, %s, %s, '[]'::jsonb)
        ON CONFLICT (entity_key) DO UPDATE SET canonical_name = EXCLUDED.canonical_name
        RETURNING id
        """,
        (ENTITY_KEY, ENTITY_NAME, ENTITY_DOMAIN),
        commit=True,
    )
    entity_id = int(row["id"])
    for industry in industries:
        _exec(
            """
            INSERT INTO media_entity_score_snapshots
                (entity_id, industry_key, score_version, shadow_score, reference_status, is_purchasable)
            VALUES (%s, %s, 'test-v1', 70, 'purchasable', TRUE)
            ON CONFLICT (entity_id, industry_key, score_version) DO NOTHING
            """,
            (entity_id, industry),
        )
    return entity_id


def seed_inventory(inv_id: int, *, media_source: str = "mhz_media",
                   name: str = "", is_active: bool = True, price: float = 88.0) -> None:
    """种一条库存行。🔴 两张表列名不同:mhz_media=media_name / mhz_wemedia=toutiao_name。"""
    name = name or f"{ENTITY_NAME}-{inv_id}"
    if media_source == "mhz_wemedia":
        _exec(
            """
            INSERT INTO mhz_wemedia (id, toutiao_name, entrance_link, price, is_active)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET toutiao_name = EXCLUDED.toutiao_name,
                entrance_link = EXCLUDED.entrance_link, price = EXCLUDED.price,
                is_active = EXCLUDED.is_active
            """,
            (inv_id, name, f"https://{ENTITY_DOMAIN}/a/{inv_id}", price, is_active),
        )
    else:
        _exec(
            """
            INSERT INTO mhz_media (id, media_name, entrance_link, price, is_active)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET media_name = EXCLUDED.media_name,
                entrance_link = EXCLUDED.entrance_link, price = EXCLUDED.price,
                is_active = EXCLUDED.is_active
            """,
            (inv_id, name, f"https://{ENTITY_DOMAIN}/a/{inv_id}", price, is_active),
        )


def set_inventory_active(inv_id: int, active: bool, media_source: str = "mhz_media") -> None:
    table = "mhz_wemedia" if media_source == "mhz_wemedia" else "mhz_media"
    _exec(f"UPDATE {table} SET is_active = %s WHERE id = %s", (active, inv_id))


def seed_candidate(inv_id: int, *, industry: str = "education", media_source: str = "mhz_media",
                   confidence: float = 0.98, can_approve: bool = True,
                   risk_flags=None, name: str = "") -> int:
    """写一条候选行。can_approve / risk_flags 是**写入时快照** —— 用例故意让它与实时状态打架。"""
    name = name or f"{ENTITY_NAME}-{inv_id}"
    saved = upsert_media_binding_candidate({
        "candidate_key": f"ck-{ENTITY_KEY}-{industry}-{media_source}-{inv_id}",
        "entity_key": ENTITY_KEY,
        "industry_key": industry,
        "inventory": {
            "media_source": media_source,
            "inventory_id": inv_id,
            "media_name": name,
            "url": f"https://{ENTITY_DOMAIN}/a/{inv_id}",
        },
        "match_method": "domain_exact",
        "match_confidence": confidence,
        "can_approve": can_approve,
        "risk_flags": risk_flags or [],
        "evidence": {},
    }, operator_id=None)
    return int(saved["id"])


def snapshot_can_approve(candidate_id: int) -> bool:
    row = _fetchone(
        "SELECT can_approve FROM geo_media_binding_candidates WHERE id = %s", (candidate_id,))
    return bool(row["can_approve"])


@pytest.fixture(autouse=True)
def _flywheel_tables():
    init_media_entity_flywheel_tables()
    cleanup()
    yield
    cleanup()
