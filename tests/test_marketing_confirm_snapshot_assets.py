import json
import importlib.util
import sys
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_marketing_confirm_api(monkeypatch):
    fake_diagnosis = types.ModuleType("db.diagnosis_db")
    fake_diagnosis.get_connection = lambda: None
    fake_notifications = types.ModuleType("db.notifications")
    fake_notifications.create_notification = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "db.diagnosis_db", fake_diagnosis)
    monkeypatch.setitem(sys.modules, "db.notifications", fake_notifications)

    spec = importlib.util.spec_from_file_location(
        "marketing_confirm_api_under_test",
        ROOT / "api" / "marketing_confirm_api.py",
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_material_snapshot_includes_contact_and_confirmable_images(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    monkeypatch.setattr(
        api,
        "_get_confirmable_images",
        lambda brand_id: [
            {
                "id": 7,
                "public_url": "/uploads/brand/7.jpg",
                "thumbnail_key": "/uploads/brand/7_thumb.jpg",
                "storage_key": "private-storage-key",
                "title": "施工现场",
                "caption": "工地保护",
                "image_type": "construction",
                "status": "active",
                "publish_allowed": True,
                "rights_confirmed": False,
            }
        ],
        raising=False,
    )

    snapshot = api._build_materials_snapshot(
        {"id": 615, "name": "栖舍设计", "company_name": "深圳市栖舍设计装饰有限公司", "industry": "家装"},
        {
            "contact_phone": "0755-123456",
            "contact_wechat": "qishe-design",
            "contact_website": "https://qishe.example",
            "contact_address": "深圳市罗湖区",
            "testimonials": [{"name": "刘女士", "quote": "真实原话"}],
        },
        {"testimonials": [{"name": "刘女士", "quote": "真实原话"}]},
    )

    assert snapshot["contact"] == {
        "phone": "0755-123456",
        "wechat": "qishe-design",
        "website": "https://qishe.example",
        "address": "深圳市罗湖区",
    }
    assert snapshot["images"] == [
        {
            "id": 7,
            "url": "/uploads/brand/7.jpg",
            "thumbnail_url": "/uploads/brand/7_thumb.jpg",
            "title": "施工现场",
            "caption": "工地保护",
            "image_type": "construction",
            "rights_confirmed": False,
        }
    ]
    assert "private-storage-key" not in json.dumps(snapshot, ensure_ascii=False)


def test_pending_or_feedback_session_uses_fresh_snapshot(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    stale_snapshot = {"testimonials": [{"name": "刘女士", "quote": "旧的泛化评价"}]}
    fresh_snapshot = {"testimonials": [{"name": "刘女士", "quote": "房子装修完整体效果超出我的期待"}]}

    monkeypatch.setattr(api, "_snapshot_from_current_brand", lambda brand_id: fresh_snapshot, raising=False)

    assert api._snapshot_for_public_session(
        {"status": "pending", "brand_id": 615, "materials_snapshot": json.dumps(stale_snapshot, ensure_ascii=False)}
    ) == fresh_snapshot
    assert api._snapshot_for_public_session(
        {"status": "feedback", "brand_id": 615, "materials_snapshot": json.dumps(stale_snapshot, ensure_ascii=False)}
    ) == fresh_snapshot
    assert api._snapshot_for_public_session(
        {"status": "confirmed", "brand_id": 615, "materials_snapshot": json.dumps(stale_snapshot, ensure_ascii=False)}
    ) == stale_snapshot


def test_confirmed_image_ids_are_extracted_safely(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    snapshot = {
        "images": [
            {"id": 12, "url": "/a.jpg"},
            {"id": "13", "url": "/b.jpg"},
            {"id": "bad", "url": "/c.jpg"},
            {"url": "/d.jpg"},
        ]
    }

    assert api._confirmed_image_ids_from_snapshot(snapshot) == [12, 13]
