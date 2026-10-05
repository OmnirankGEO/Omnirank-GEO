from test_marketing_confirm_snapshot_assets import load_marketing_confirm_api
from fastapi import HTTPException
import asyncio


def test_public_asset_urls_are_rooted_for_customer_confirm_pages(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)

    assert api._normalize_public_asset_url("uploads/article-images/a.webp") == "/uploads/article-images/a.webp"
    assert api._normalize_public_asset_url("/uploads/article-images/a.webp") == "/uploads/article-images/a.webp"
    assert api._normalize_public_asset_url("article-images/a.webp") == "/uploads/article-images/a.webp"
    assert api._normalize_public_asset_url("https://cdn.example.com/a.webp") == "https://cdn.example.com/a.webp"


def test_snapshot_prefers_real_profile_testimonials_over_generic_material_summary(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    monkeypatch.setattr(api, "_get_confirmable_images", lambda brand_id: [], raising=False)

    brand = {"id": 615, "name": "深圳市栖舍设计装修有限公司", "industry": "家装"}
    profile = {
        "company_intro": "公司简介",
        "testimonials": (
            '[{"name":"刘女士","quote":"房子装修完整体效果超出我的期待。"},'
            '{"评价人称呼":"黄总","评价原话":"今天验收完彻底放心。"}]'
        ),
    }
    materials = {
        "testimonials": [
            {
                "name": "刘",
                "quote": "对设计方案、施工手艺、问题响应速度及细节处理给予高度评价，认为全程沟通舒服。",
            }
        ]
    }

    snapshot = api._build_materials_snapshot(brand, profile, materials)
    quotes = [item.get("quote") for item in snapshot["testimonials"]]

    assert "房子装修完整体效果超出我的期待。" in quotes
    assert "今天验收完彻底放心。" in quotes
    assert "对设计方案、施工手艺、问题响应速度及细节处理给予高度评价，认为全程沟通舒服。" not in quotes[:2]


def test_customer_material_patch_applies_allowed_paths_without_mutating_original(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    snapshot = {
        "company": {"intro": "旧简介", "core_value": "旧价值"},
        "selling_points": {
            "items": [{"point": "旧卖点", "evidence": "旧依据"}],
            "usp": "旧主张",
        },
        "methodology": "旧方法论",
        "images": [{"url": "/uploads/a.webp", "title": "旧图片名", "caption": "旧说明"}],
    }

    patched, audit = api._apply_material_patch(snapshot, [
        {"path": "company.intro", "value": "新简介"},
        {"path": "selling_points.items.0.evidence", "value": "新依据"},
        {"path": "methodology", "value": "新方法论"},
        {"path": "images.0.title", "value": "新图片名"},
    ])

    assert patched["company"]["intro"] == "新简介"
    assert patched["selling_points"]["items"][0]["evidence"] == "新依据"
    assert patched["methodology"] == "新方法论"
    assert patched["images"][0]["title"] == "新图片名"
    assert snapshot["company"]["intro"] == "旧简介"
    assert snapshot["selling_points"]["items"][0]["evidence"] == "旧依据"
    assert snapshot["images"][0]["title"] == "旧图片名"
    assert [item["path"] for item in audit] == [
        "company.intro",
        "selling_points.items.0.evidence",
        "methodology",
        "images.0.title",
    ]


def test_customer_material_patch_rejects_unsafe_or_unowned_paths(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    snapshot = {
        "company": {"intro": "简介"},
        "selling_points": {"items": [{"point": "卖点", "evidence": "依据"}]},
        "images": [{"url": "/uploads/a.webp"}],
    }

    for edit in [
        {"path": "images.0.url", "value": "https://evil.example/x.png"},
        {"path": "__proto__.polluted", "value": "1"},
        {"path": "selling_points.items.5.point", "value": "越界"},
    ]:
        try:
            api._apply_material_patch(snapshot, [edit])
        except HTTPException as exc:
            assert exc.status_code == 400
        else:
            raise AssertionError(f"unsafe edit unexpectedly accepted: {edit}")


def test_snapshot_patch_materials_maps_back_to_profile_and_material_payloads(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    snapshot = {
        "company": {
            "name": "深圳市栖舍设计装饰有限公司",
            "industry": "家装/室内设计",
            "intro": "客户确认后的公司简介",
            "core_value": "客户确认后的价值主张",
            "target_users": "中高端自住业主",
            "service_area": "深圳及周边城市",
        },
        "selling_points": {
            "items": [{"point": "兵将料分离", "evidence": "自有产业工人"}],
            "usp": "设计施工一体化全案服务",
        },
        "products": {"name": "全案家装", "features": ["设计", "施工"], "scenarios": ["平层", "别墅"]},
        "customers": {"needs": ["靠谱落地"], "concerns": ["恶意增项"]},
        "cases": [{"client": "私企老板", "results": "满意并转介绍"}],
        "testimonials": [{"name": "刘女士", "quote": "全程省心靠谱"}],
        "credentials": [{"type": "协会", "name": "家装协会理事单位"}],
        "methodology": "工人、项目管家、辅材商分离管理",
        "contact": {"phone": "0755-00000000", "wechat": "qishe", "website": "https://example.com", "address": "深圳"},
    }

    profile_updates, material_updates = api._snapshot_to_profile_and_material_updates(snapshot)

    assert profile_updates["company_intro"] == "客户确认后的公司简介"
    assert profile_updates["core_value"] == "客户确认后的价值主张"
    assert profile_updates["target_users"] == "中高端自住业主"
    assert profile_updates["contact_phone"] == "0755-00000000"
    assert profile_updates["contact_wechat"] == "qishe"
    assert profile_updates["success_cases"][0]["results"] == "满意并转介绍"
    assert profile_updates["testimonials"][0]["quote"] == "全程省心靠谱"
    assert material_updates["company_intro"] == "客户确认后的公司简介"
    assert material_updates["unique_value"] == "客户确认后的价值主张"
    assert material_updates["service_area"] == "深圳及周边城市"
    assert material_updates["core_selling_points"][0]["point"] == "兵将料分离"
    assert material_updates["methodology"] == "工人、项目管家、辅材商分离管理"


def test_snapshot_image_metadata_syncs_to_brand_image_assets(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    executed = []

    class FakeCursor:
        def execute(self, query, params=None):
            executed.append((query, params))

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(api, "get_connection", lambda: FakeConn())
    snapshot = {
        "images": [
            {"id": 12, "title": "客户确认后的图片名", "caption": "客厅案例", "image_type": "case"},
            {"id": "bad", "title": "应跳过"},
            {"url": "/uploads/no-id.webp", "title": "无 id 跳过"},
        ]
    }

    api._write_image_metadata_for_brand(615, snapshot)

    assert len(executed) == 1
    query, params = executed[0]
    assert "UPDATE brand_image_assets" in query
    assert "title = %s" in query
    assert "caption = %s" in query
    assert "image_type = %s" in query
    assert "public_url" not in query
    assert "storage_key" not in query
    assert "publish_allowed" not in query
    assert "rights_confirmed" not in query
    assert params[-2:] == (12, 615)
    assert "客户确认后的图片名" in params


def test_material_patch_feedback_syncs_customer_edits_to_knowledge(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    session = {
        "id": 7,
        "brand_id": 615,
        "status": "pending",
        "expires_at": None,
    }
    snapshot = {
        "company": {"name": "深圳市栖舍设计装饰有限公司", "intro": "旧简介"},
        "selling_points": {"items": []},
    }
    synced = []

    class FakeCursor:
        def execute(self, query, params=None):
            self.last_query = query

        def fetchone(self):
            return session

    class FakeConn:
        def __init__(self):
            self.cursor_obj = FakeCursor()
            self.committed = False
            self.closed = False

        def cursor(self):
            return self.cursor_obj

        def commit(self):
            self.committed = True

        def close(self):
            self.closed = True

    fake_conn = FakeConn()
    monkeypatch.setattr(api, "get_connection", lambda: fake_conn)
    monkeypatch.setattr(api, "_ensure_table", lambda: None)
    monkeypatch.setattr(api, "_snapshot_from_current_brand", lambda brand_id: snapshot)
    monkeypatch.setattr(api, "create_notification", lambda **kwargs: None)
    monkeypatch.setattr(
        api,
        "_write_snapshot_back_to_knowledge",
        lambda brand_id, patched: synced.append((brand_id, patched)) or True,
    )

    req = api.UpdateMaterialsRequest(
        action="feedback",
        note="",
        edits=[api.MaterialPatchEdit(path="company.intro", value="客户直接改好的简介")],
    )
    result = asyncio.run(api.update_materials_from_customer("token-1", req))

    assert result["status"] == "feedback"
    assert result["knowledge_synced"] is True
    assert synced[0][0] == 615
    assert synced[0][1]["company"]["intro"] == "客户直接改好的简介"


def test_material_patch_preserves_append_only_customer_edit_history(monkeypatch):
    api = load_marketing_confirm_api(monkeypatch)
    first_patch = {
        "edits": [{"path": "company.intro", "old": "旧简介", "new": "第一次客户修改"}],
        "note": "第一次备注",
        "action": "feedback",
        "updated_at": "2026-06-25T10:00:00",
    }
    session = {
        "id": 8,
        "brand_id": 615,
        "status": "feedback",
        "expires_at": None,
        "materials_snapshot": '{"company":{"intro":"第一次客户修改"},"selling_points":{"items":[]}}',
        "customer_patch_json": api.json.dumps(first_patch, ensure_ascii=False),
    }
    written_patch_payloads = []

    class FakeCursor:
        def execute(self, query, params=None):
            if "UPDATE marketing_confirm_sessions" in query and params:
                written_patch_payloads.append(api.json.loads(params[2]))

        def fetchone(self):
            return session

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(api, "get_connection", lambda: FakeConn())
    monkeypatch.setattr(api, "_ensure_table", lambda: None)
    monkeypatch.setattr(api, "create_notification", lambda **kwargs: None)
    monkeypatch.setattr(api, "_write_snapshot_back_to_knowledge", lambda brand_id, patched: True)

    req = api.UpdateMaterialsRequest(
        action="feedback",
        note="第二次备注",
        edits=[api.MaterialPatchEdit(path="company.intro", value="第二次客户修改")],
    )
    result = asyncio.run(api.update_materials_from_customer("token-2", req))

    assert result["status"] == "feedback"
    written_patch = written_patch_payloads[-1]
    assert written_patch["note"] == "第二次备注"
    assert len(written_patch["history"]) == 2
    assert written_patch["history"][0]["edits"][0]["new"] == "第一次客户修改"
    assert written_patch["history"][1]["edits"][0]["old"] == "第一次客户修改"
    assert written_patch["history"][1]["edits"][0]["new"] == "第二次客户修改"
