from services.media_takeover_gate import evaluate_takeover_gate


def _green_health():
    return {
        "level": "green",
        "can_activate": True,
        "reasons": [],
        "metrics": {
            "answer_adoption_count": 42,
            "explicit_citation_count": 31,
        },
    }


def _adoption_summary():
    return {
        "answer_adopted_rows": 42,
        "explicit_cited_rows": 31,
        "unique_sources": 12,
        "engine_count": 3,
    }


def test_takeover_gate_fails_closed_without_approved_media_binding():
    result = evaluate_takeover_gate(
        industry_key="tourism_hotel",
        health=_green_health(),
        approved_bindings=[],
        adoption_summary=_adoption_summary(),
        whitelist={"customer_ids": [1001]},
    )

    assert result["ready"] is False
    assert result["production_takeover"] is False
    assert "至少需要 1 个已通过审核的真实媒体资源" in result["blockers"]
    assert result["next_action"] == "先审核通过媒体绑定候选"


def test_takeover_gate_fails_closed_without_whitelist_scope():
    result = evaluate_takeover_gate(
        industry_key="tourism_hotel",
        health=_green_health(),
        approved_bindings=[{"id": 7, "entity_key": "ctrip_com"}],
        adoption_summary=_adoption_summary(),
        whitelist={},
    )

    assert result["ready"] is False
    assert result["production_takeover"] is False
    assert "必须先限定行业、客户或品牌白名单" in result["blockers"]
    assert result["next_action"] == "补充白名单范围后再保存接管准备"


def test_takeover_gate_can_be_ready_but_never_takes_over_production():
    result = evaluate_takeover_gate(
        industry_key="tourism_hotel",
        health=_green_health(),
        approved_bindings=[
            {"id": 7, "entity_key": "ctrip_com"},
            {"id": 8, "entity_key": "qunar_com"},
        ],
        adoption_summary=_adoption_summary(),
        whitelist={"customer_ids": [1001], "brand_ids": ["BRD-1"]},
    )

    assert result["ready"] is True
    assert result["production_takeover"] is False
    assert result["status_label"] == "具备接管准备条件"
    assert result["next_action"] == "等待老板单独授权 live 接线"
