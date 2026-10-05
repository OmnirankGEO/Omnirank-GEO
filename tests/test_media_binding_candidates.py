from __future__ import annotations

import pytest


def test_shared_platform_domain_requires_name_evidence_before_approval():
    from services.media_binding_candidates import build_binding_candidates

    entity = {
        "entity_key": "media_sohu",
        "canonical_name": "搜狐网",
        "domain": "sohu.com",
        "aliases": ["搜狐"],
        "industry_key": "tourism_hotel",
    }
    inventory = [{
        "media_source": "mhz_media",
        "inventory_id": 1001,
        "media_name": "随机娱乐号",
        "entrance_link": "https://www.sohu.com/a/123456_999999",
        "price_yuan": 120,
        "is_active": True,
    }]

    candidates = build_binding_candidates(entity, inventory)

    assert len(candidates) == 1
    assert candidates[0]["can_approve"] is False
    assert "共享平台域名需要名称证据" in candidates[0]["risk_flags"]


def test_exact_name_and_domain_candidate_is_approvable():
    from services.media_binding_candidates import build_binding_candidates

    entity = {
        "entity_key": "media_ctrip",
        "canonical_name": "携程旅行",
        "domain": "ctrip.com",
        "aliases": ["携程"],
        "industry_key": "tourism_hotel",
    }
    inventory = [{
        "media_source": "mhz_media",
        "inventory_id": 2001,
        "media_name": "携程旅行",
        "entrance_link": "https://www.ctrip.com/",
        "price_yuan": 280,
        "is_active": True,
    }]

    candidates = build_binding_candidates(entity, inventory)

    assert len(candidates) == 1
    assert candidates[0]["can_approve"] is True
    assert candidates[0]["match_method"] in {"domain_exact", "name_alias"}
    assert candidates[0]["inventory"]["inventory_id"] == 2001


def test_inactive_or_free_inventory_cannot_be_approved_even_with_name_match():
    from services.media_binding_candidates import build_binding_candidates

    entity = {
        "entity_key": "media_mafengwo",
        "canonical_name": "马蜂窝",
        "domain": "mafengwo.cn",
        "aliases": ["马蜂窝旅游"],
        "industry_key": "tourism_hotel",
    }
    inventory = [
        {
            "media_source": "mhz_media",
            "inventory_id": 3001,
            "media_name": "马蜂窝旅游",
            "entrance_link": "https://www.mafengwo.cn/",
            "price_yuan": 0,
            "price_points": 0,
            "is_active": True,
        },
        {
            "media_source": "mhz_media",
            "inventory_id": 3002,
            "media_name": "马蜂窝旅游",
            "entrance_link": "https://www.mafengwo.cn/",
            "price_yuan": 180,
            "is_active": False,
        },
    ]

    candidates = build_binding_candidates(entity, inventory)

    assert {c["inventory"]["inventory_id"] for c in candidates} == {3001, 3002}
    assert all(c["can_approve"] is False for c in candidates)
    assert all("库存不可采购" in c["risk_flags"] for c in candidates)


def test_approval_revalidates_loaded_inventory_and_rejects_raw_payload_claims():
    from services.media_binding_candidates import verify_candidate_for_approval

    entity = {
        "entity_key": "media_ctrip",
        "canonical_name": "携程旅行",
        "domain": "ctrip.com",
        "aliases": ["携程"],
        "industry_key": "tourism_hotel",
    }
    raw_candidate_claim = {
        "inventory": {
            "media_source": "mhz_media",
            "inventory_id": 2001,
            "media_name": "携程旅行",
            "entrance_link": "https://www.ctrip.com/",
            "is_purchasable": True,
            "price_yuan": 999,
            "is_active": True,
        }
    }
    reloaded_inventory = {
        "media_source": "mhz_media",
        "inventory_id": 2001,
        "media_name": "携程旅行",
        "entrance_link": "https://www.ctrip.com/",
        "price_yuan": 0,
        "price_points": 0,
        "is_active": True,
    }

    with pytest.raises(ValueError, match="库存不可采购"):
        verify_candidate_for_approval(entity, reloaded_inventory, raw_candidate_claim)
