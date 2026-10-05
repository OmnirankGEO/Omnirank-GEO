from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


ROOT = Path(__file__).resolve().parents[2]


def test_frontend_uses_one_bulk_endpoint_not_per_asset_patch_loop():
    source = (ROOT / "frontend/src/components/brand/BrandImageGallery.tsx").read_text(encoding="utf-8")
    assert "/api/brand-images/assets/article-usage" in source
    assert "assetIds.map((assetId) => authApi.patch" not in source


def test_image_name_has_touch_and_keyboard_full_text_disclosure():
    source = (ROOT / "frontend/src/components/brand/BrandImageGallery.tsx").read_text(encoding="utf-8")
    assert "data-testid={`image-name-disclosure-${assetId}`}" in source
    assert "<details" in source
    assert "title={displayName}" in source
    assert "break-all" in source


def test_single_image_disable_also_preserves_confirmed_rights():
    source = (ROOT / "frontend/src/components/brand/BrandImageGallery.tsx").read_text(encoding="utf-8")
    toggle = source[source.index("const handleToggle"):source.index("const handleDelete")]
    assert "? { publish_allowed: 1, rights_confirmed: 1 }" in toggle
    assert ": { publish_allowed: 0 }" in toggle
    assert "rights_confirmed: on ? 1 : 0" not in toggle


def test_writing_image_hard_rule_is_unchanged():
    source = (ROOT / "db/brand_image_assets_db.py").read_text(encoding="utf-8")
    assert "status='active' AND publish_allowed=1 AND rights_confirmed=1" in source


class _FakeCursor:
    def __init__(self, rows: dict[int, dict]):
        self.rows = rows
        self.rowcount = 0

    def execute(self, sql, params):
        if sql.lstrip().startswith("SELECT"):
            self.selected_ids = list(params[0])
            return
        if "rights_confirmed=%s" in sql:
            publish_allowed, rights_confirmed, asset_id = params
        else:
            publish_allowed, asset_id = params
            rights_confirmed = self.rows[int(asset_id)]["rights_confirmed"]
        row = self.rows[int(asset_id)]
        row["publish_allowed"] = publish_allowed
        row["rights_confirmed"] = rights_confirmed
        self.rowcount = 1

    def fetchall(self):
        return [dict(self.rows[asset_id]) for asset_id in self.selected_ids if asset_id in self.rows]


class _FakeConnection:
    def __init__(self, rows: dict[int, dict]):
        self._cursor = _FakeCursor(rows)
        self.committed = False

    def cursor(self, **_kwargs):
        return self._cursor

    def commit(self):
        self.committed = True

    def rollback(self):
        pass

    def close(self):
        pass


def _asset(asset_id: int, *, risk_flags=None, status="active", publish=0, rights=0, brand_id=7):
    return {
        "id": asset_id,
        "brand_id": brand_id,
        "status": status,
        "risk_flags": risk_flags or [],
        "publish_allowed": publish,
        "rights_confirmed": rights,
    }


def test_bulk_image_success_risk_skip_and_repeat_idempotency(monkeypatch):
    from db import brand_image_assets_db as image_db

    rows = {1: _asset(1), 2: _asset(2, risk_flags=["qrcode"])}
    monkeypatch.setattr(image_db, "get_connection", lambda: _FakeConnection(rows))

    first = image_db.batch_set_article_usage([1, 2], enabled=True)
    second = image_db.batch_set_article_usage([1, 2], enabled=True)

    assert first["updated"] == 1
    assert first["skipped"] == 1
    assert "qrcode" in first["items"][1]["reason"]
    assert rows[1]["publish_allowed"] == rows[1]["rights_confirmed"] == 1
    assert second["updated"] == 0
    assert second["skipped"] == 2


def test_cancel_article_usage_preserves_confirmed_rights(monkeypatch):
    from db import brand_image_assets_db as image_db

    rows = {1: _asset(1, publish=1, rights=1)}
    monkeypatch.setattr(image_db, "get_connection", lambda: _FakeConnection(rows))

    first = image_db.batch_set_article_usage([1], enabled=False)
    repeat = image_db.batch_set_article_usage([1], enabled=False)

    assert first["updated"] == 1
    assert rows[1]["publish_allowed"] == 0
    assert rows[1]["rights_confirmed"] == 1
    assert repeat["updated"] == 0
    assert repeat["skipped"] == 1


@pytest.mark.asyncio
async def test_bulk_image_cross_brand_idor_rejected_before_write(monkeypatch):
    from api import image_asset_api

    called = False
    monkeypatch.setattr(
        image_asset_api,
        "get_image_assets_by_ids",
        lambda _ids: [_asset(1, brand_id=999)],
    )

    def reject(_request, brand_id):
        assert brand_id == 999
        raise HTTPException(status_code=403, detail="无权访问")

    def should_not_write(*_args, **_kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(image_asset_api, "require_brand_access", reject)
    monkeypatch.setattr(image_asset_api, "batch_set_article_usage", should_not_write)

    with pytest.raises(HTTPException) as exc:
        await image_asset_api.batch_update_article_usage(
            image_asset_api.BulkArticleUsageRequest(
                asset_ids=[1], enabled=True, rights_confirmed=True
            ),
            SimpleNamespace(state=SimpleNamespace()),
        )
    assert exc.value.status_code == 403
    assert called is False
