from pathlib import Path


def test_media_binding_tables_are_idempotent_and_shadow_only():
    text = Path("db/media_entity_flywheel_db.py").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS geo_media_binding_candidates" in text
    assert "CREATE TABLE IF NOT EXISTS geo_media_binding_audit_events" in text
    assert "UNIQUE (entity_key, industry_key, media_source, inventory_id)" in text
    assert "DROP TABLE" not in text
    assert "TRUNCATE" not in text


def test_media_binding_api_is_admin_dry_run_and_no_raw_purchasable_bypass():
    text = Path("api/media_entity_flywheel_api.py").read_text(encoding="utf-8")

    assert '"/media/binding-candidates"' in text
    assert '"/media/binding-candidates/rebuild"' in text
    assert '"/media/binding-candidates/{candidate_id}/review"' in text
    assert "class MediaBindingCandidateRebuildRequest" in text
    assert "dry_run: bool = True" in text
    assert "verify_candidate_for_approval" in text
    assert "match_inventory_to_entity" in text

    review_pos = text.index('"/media/binding-candidates/{candidate_id}/review"')
    review_body = text[review_pos: review_pos + 1600]
    assert "_require_admin(request)" in review_body
    assert "production_takeover" in review_body
    assert "False" in review_body
    assert "is_purchasable=True" not in review_body
    assert "matched_inventory or req.inventory_matches" not in text


def test_media_binding_frontend_has_operator_labels_not_engineering_words():
    text = Path("frontend/src/pages/Admin/GeoPlacementFlywheel.tsx").read_text(encoding="utf-8")

    assert "媒体绑定候选" in text
    assert "预览绑定候选" in text
    assert "写入待审核候选" in text
    assert "通过绑定" in text
    assert "驳回绑定" in text
    assert "不会接管线上投放" in text
    assert "binding_candidates" not in text
