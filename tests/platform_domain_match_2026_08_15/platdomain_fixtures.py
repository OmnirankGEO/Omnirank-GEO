"""[平台域口径 2026-08-15] 接线/存量用例的共享夹具**实现**。

🔴 为什么不叫 conftest:仓内多个测试目录都用 `from conftest import ...`,
   同一次收集里谁先进 sys.path 谁赢 —— 实测本目录与
   tests/flywheel_binding_liveness_2026_08_15 同跑时会 ImportError。
   所以助手放在唯一命名的模块里,conftest.py 只留 fixture。

沿用 `tests/flywheel_binding_liveness_2026_08_15/conftest.py` 的做法:种**真实体 + 真库存行**。
伪造实体在同源判据下会当场转红(那正是 6a87b271 那一包的教训)。
"""
from __future__ import annotations

from db.connection import get_connection
from db.media_entity_flywheel_db import (
    init_media_entity_flywheel_tables,
    upsert_media_binding_candidate,
)

#: 新纳入 A 桶的注册域,用它验「新口径生效」;子域用来验注册域折叠。
PLATFORM_ENTITY_KEY = "me_platdomain_csdn"
PLATFORM_ENTITY_DOMAIN = "blog.csdn.net"      # 注册域 = csdn.net(A 桶新增)
PLATFORM_ENTITY_NAME = "CSDN博客"
#: B 桶域,用它验「没被悄悄并进名单」——同样的形态在这里必须**不**打风险标。
BBUCKET_ENTITY_KEY = "me_platdomain_newscn"
BBUCKET_ENTITY_DOMAIN = "news.cn"
BBUCKET_ENTITY_NAME = "新华社"

INV_BASE = 992100


def _exec(sql: str, params: tuple = ()) -> None:
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def fetchone(sql: str, params: tuple = (), commit: bool = False):
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


ALL_KEYS = (PLATFORM_ENTITY_KEY, BBUCKET_ENTITY_KEY)


def cleanup() -> None:
    for key in ALL_KEYS:
        _exec("DELETE FROM geo_media_binding_audit_events WHERE entity_key = %s", (key,))
        _exec("DELETE FROM geo_media_binding_candidates WHERE entity_key = %s", (key,))
        _exec("DELETE FROM geo_media_inventory_mappings WHERE entity_id IN"
              " (SELECT id FROM geo_media_entities WHERE entity_key = %s)", (key,))
        _exec("DELETE FROM media_entity_score_snapshots WHERE entity_id IN"
              " (SELECT id FROM geo_media_entities WHERE entity_key = %s)", (key,))
        _exec("DELETE FROM geo_media_entities WHERE entity_key = %s", (key,))
    _exec("DELETE FROM mhz_media WHERE id >= %s AND id < %s", (INV_BASE, INV_BASE + 1000))


def seed_entity(entity_key: str, domain: str, name: str,
                industries=("education",), aliases: str = "[]") -> int:
    row = fetchone(
        """
        INSERT INTO geo_media_entities (entity_key, canonical_name, domain, aliases)
        VALUES (%s, %s, %s, %s::jsonb)
        ON CONFLICT (entity_key) DO UPDATE SET domain = EXCLUDED.domain,
            canonical_name = EXCLUDED.canonical_name, aliases = EXCLUDED.aliases
        RETURNING id
        """,
        (entity_key, name, domain, aliases), commit=True,
    )
    entity_id = int(row["id"])
    for industry in industries:
        _exec(
            """
            INSERT INTO media_entity_score_snapshots
                (entity_id, industry_key, score_version, shadow_score, reference_status, is_purchasable)
            VALUES (%s, %s, 'platdomain-v1', 70, 'purchasable', TRUE)
            ON CONFLICT (entity_id, industry_key, score_version) DO NOTHING
            """,
            (entity_id, industry),
        )
    return entity_id


def seed_inventory(inv_id: int, name: str, *, is_active: bool = True, price: float = 88.0,
                   link_domain: str = PLATFORM_ENTITY_DOMAIN) -> None:
    _exec(
        """
        INSERT INTO mhz_media (id, media_name, entrance_link, price, is_active)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET media_name = EXCLUDED.media_name,
            entrance_link = EXCLUDED.entrance_link, price = EXCLUDED.price,
            is_active = EXCLUDED.is_active
        """,
        (inv_id, name, f"https://{link_domain}/a/{inv_id}", price, is_active),
    )


def seed_candidate(entity_key: str, inv_id: int, name: str, *, industry: str = "education",
                   can_approve: bool = True, confidence: float = 0.98) -> int:
    saved = upsert_media_binding_candidate({
        "candidate_key": f"ck-{entity_key}-{inv_id}",
        "entity_key": entity_key,
        "industry_key": industry,
        "inventory": {"media_source": "mhz_media", "inventory_id": inv_id,
                      "media_name": name, "url": f"https://x/{inv_id}"},
        "match_method": "domain_exact",
        "match_confidence": confidence,
        "can_approve": can_approve,
        "risk_flags": [],
        "evidence": {},
    }, operator_id=None)
    return int(saved["id"])


def approved_total() -> int:
    return int(fetchone("SELECT COUNT(*) AS n FROM geo_media_binding_candidates WHERE status='approved'")["n"])


def candidate_row(cid: int) -> dict:
    return dict(fetchone("SELECT * FROM geo_media_binding_candidates WHERE id = %s", (cid,)))
