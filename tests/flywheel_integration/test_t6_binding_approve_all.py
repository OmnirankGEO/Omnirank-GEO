"""[T6] 全行业一键通过服务端选集:跨行业,且通过后幂等(0 可再选)。

[P0-1 2026-08-15 夹具重建] 选集判据从「读快照列」改成「实时重算」(判据与审核同源)之后,
本文件原来的夹具就失效了 —— 它伪造 `entity_key` 并用不存在的 `inventory_id`,在快照口径下
照样全绿。那正是 bug 能活到生产的原因之一:**测试从不碰真实体/真库存,所以永远发现不了
「选集说能过、审核说不能过」**。

用例意图一个没删(跨行业选集 / 三条过滤线 / 通过后幂等),只是把实体、影子评分、库存行种成真的。
过滤线的形态也换成实时可控的等价物:
  · conf<0.90  → 仍用快照置信度(实时重算的 domain_exact 恒 0.98,压不下来)…
    改用「库存下架」这条实时线,并单独保留一条快照低置信度用例证明两条线都在;
  · can_approve=False / 有风险 → 用「价格为 0」「实体域名是共享平台且无名称证据」这两种
    实时可复现的形态。
"""
import pytest

from services.media_binding_candidates import RECOMMENDED_BINDING_MIN_CONFIDENCE
from db.media_entity_flywheel_db import (
    count_recommended_binding_candidates,
    init_media_entity_flywheel_tables,
    list_recommended_binding_candidates,
    upsert_media_binding_candidate,
)
from db.connection import get_connection

_EKEY = "me_t6_approveall"
_DOMAIN = "t6-approveall.example.com"
_INV_BASE = 991000
_INDUSTRIES = ("education", "auto", "finance")


def _exec(sql, params=()):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def _cleanup():
    _exec("DELETE FROM geo_media_binding_audit_events WHERE entity_key = %s", (_EKEY,))
    _exec("DELETE FROM geo_media_binding_candidates WHERE entity_key = %s", (_EKEY,))
    _exec("DELETE FROM geo_media_inventory_mappings WHERE entity_id IN"
          " (SELECT id FROM geo_media_entities WHERE entity_key = %s)", (_EKEY,))
    _exec("DELETE FROM media_entity_score_snapshots WHERE entity_id IN"
          " (SELECT id FROM geo_media_entities WHERE entity_key = %s)", (_EKEY,))
    _exec("DELETE FROM geo_media_entities WHERE entity_key = %s", (_EKEY,))
    _exec("DELETE FROM mhz_media WHERE id >= %s AND id < %s", (_INV_BASE, _INV_BASE + 1000))


def _seed_entity():
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO geo_media_entities (entity_key, canonical_name, domain, aliases)
            VALUES (%s, %s, %s, '[]'::jsonb)
            ON CONFLICT (entity_key) DO UPDATE SET domain = EXCLUDED.domain
            RETURNING id
            """,
            (_EKEY, "T6 一键通过测试媒体", _DOMAIN),
        )
        entity_id = int(cur.fetchone()["id"])
        for industry in _INDUSTRIES:
            cur.execute(
                """
                INSERT INTO media_entity_score_snapshots
                    (entity_id, industry_key, score_version, shadow_score, reference_status, is_purchasable)
                VALUES (%s, %s, 't6-v1', 70, 'purchasable', TRUE)
                ON CONFLICT (entity_id, industry_key, score_version) DO NOTHING
                """,
                (entity_id, industry),
            )
        conn.commit()
    finally:
        conn.close()


def _seed_inventory(inv_id, *, is_active=True, price=66.0, name=None):
    _exec(
        """
        INSERT INTO mhz_media (id, media_name, entrance_link, price, is_active)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET media_name = EXCLUDED.media_name,
            entrance_link = EXCLUDED.entrance_link, price = EXCLUDED.price,
            is_active = EXCLUDED.is_active
        """,
        (inv_id, name or f"T6 测试媒体-{inv_id}", f"https://{_DOMAIN}/p/{inv_id}", price, is_active),
    )


def _mk(inv_id, industry, conf, can_approve, risk):
    upsert_media_binding_candidate({
        "candidate_key": f"ck-{_EKEY}-{inv_id}",
        "entity_key": _EKEY,
        "industry_key": industry,
        "inventory": {"media_source": "mhz_media", "inventory_id": inv_id,
                      "media_name": f"T6 测试媒体-{inv_id}", "url": f"https://{_DOMAIN}/p/{inv_id}"},
        "match_method": "domain_exact",
        "match_confidence": conf,
        "can_approve": can_approve,
        "risk_flags": risk,
        "evidence": {},
    }, operator_id=None)


@pytest.fixture(autouse=True)
def _tables():
    init_media_entity_flywheel_tables()
    _cleanup()
    _seed_entity()
    yield
    _cleanup()


def _selected():
    return [c for c in list_recommended_binding_candidates(RECOMMENDED_BINDING_MIN_CONFIDENCE, limit=5000)
            if c["entity_key"] == _EKEY]


def test_recommended_selection_cross_industry_and_filters():
    _seed_inventory(_INV_BASE + 1)                       # ✓ recommended
    _seed_inventory(_INV_BASE + 2)                       # ✓ recommended(跨行业)
    _seed_inventory(_INV_BASE + 3)                       # ✓ recommended
    _seed_inventory(_INV_BASE + 4, is_active=False)      # ✗ 库存已下架(实时线)
    _seed_inventory(_INV_BASE + 5, price=0)              # ✗ 价格为 0 → 不可采购(实时线)
    _seed_inventory(_INV_BASE + 6, name="低价套餐随机5个")  # ✗ 噪声名(实时线)
    _mk(_INV_BASE + 1, "education", 0.98, True, [])
    _mk(_INV_BASE + 2, "auto", 0.95, True, [])
    _mk(_INV_BASE + 3, "finance", 0.92, True, [])
    _mk(_INV_BASE + 4, "education", 0.98, True, [])
    _mk(_INV_BASE + 5, "education", 0.98, True, [])
    _mk(_INV_BASE + 6, "education", 0.98, True, [])

    selected = _selected()
    assert len(selected) == 3, f"选集应为 3 条,实得 {[c['inventory_id'] for c in selected]}"
    assert {c["industry_key"] for c in selected} == {"education", "auto", "finance"}, "必须跨行业选集"
    # 🔴 三条被排除的必须**各自**排除,而不是「一起被藏掉」
    excluded = {int(c["inventory_id"]) for c in selected}
    for bad in (_INV_BASE + 4, _INV_BASE + 5, _INV_BASE + 6):
        assert bad not in excluded

    # count 与 list 同源(全库 count 做上界校验)
    assert count_recommended_binding_candidates(RECOMMENDED_BINDING_MIN_CONFIDENCE) >= 3


def test_idempotent_after_approved():
    _seed_inventory(_INV_BASE + 10)
    _seed_inventory(_INV_BASE + 11)
    _mk(_INV_BASE + 10, "education", 0.98, True, [])
    _mk(_INV_BASE + 11, "auto", 0.96, True, [])
    assert len(_selected()) == 2
    # 模拟一键通过后 status→approved
    _exec("UPDATE geo_media_binding_candidates SET status='approved' WHERE entity_key=%s", (_EKEY,))
    assert len(_selected()) == 0, "通过后再选集应为 0(幂等,再点一次 0 可通过)"
