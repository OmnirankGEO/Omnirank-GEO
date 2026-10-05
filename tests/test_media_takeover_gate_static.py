from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_takeover_gate_tables_are_idempotent_shadow_only():
    text = (ROOT / "db" / "media_entity_flywheel_db.py").read_text(encoding="utf-8")

    assert "CREATE TABLE IF NOT EXISTS geo_media_takeover_policies" in text
    assert "CREATE TABLE IF NOT EXISTS geo_media_takeover_audit_events" in text
    assert "idx_geo_media_takeover_one_active_shadow" in text
    assert "COALESCE(active, TRUE) = TRUE" in text
    assert "DROP TABLE" not in text
    assert "TRUNCATE" not in text


def test_takeover_gate_api_is_admin_only_and_does_not_import_live_placement():
    text = (ROOT / "api" / "media_entity_flywheel_api.py").read_text(encoding="utf-8")

    assert '"/media/takeover-gate"' in text
    assert '"/media/takeover-gate/preview"' in text
    assert '"/media/takeover-gate/save"' in text
    assert '"/media/takeover-gate/disable"' in text
    assert "class MediaTakeoverGateSaveRequest" in text
    assert "production_takeover" in text
    assert "False" in text
    assert "placement_service" not in text

    save_pos = text.index('"/media/takeover-gate/save"')
    save_body = text[save_pos: save_pos + 1800]
    assert "_require_admin(request)" in save_body
    assert "review_note" in save_body


def test_takeover_gate_frontend_is_operator_readable_chinese():
    text = (ROOT / "frontend" / "src" / "pages" / "Admin" / "GeoPlacementFlywheel.tsx").read_text(encoding="utf-8")

    assert "接管前闸门" in text
    assert "当前不接管线上" in text
    assert "预览接管条件" in text
    assert "保存接管准备" in text
    assert "停用接管准备" in text
    assert "等待老板单独授权" in text
