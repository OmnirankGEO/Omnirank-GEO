import importlib
import sys


class _FakeInitCursor:
    rowcount = 0

    def execute(self, query, params=None):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakeInitConnection:
    autocommit = False

    def cursor(self):
        return _FakeInitCursor()

    def commit(self):
        return None

    def close(self):
        return None


class _FakeCursor:
    def __init__(self, row):
        self.row = row
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((query, params))

    def fetchone(self):
        return self.row


class _FakeConnection:
    def __init__(self, row):
        self.cursor_obj = _FakeCursor(row)
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def close(self):
        self.closed = True


def _load_modules(monkeypatch):
    import db.connection as connection

    monkeypatch.setattr(connection, "get_connection", lambda: _FakeInitConnection())
    sys.modules.pop("db.diagnosis_db", None)
    sys.modules.pop("api.m3_material_confirm_api", None)
    diagnosis_db = importlib.import_module("db.diagnosis_db")
    m3_api = importlib.import_module("api.m3_material_confirm_api")
    return diagnosis_db, m3_api


def test_sync_materials_creates_profile_from_brand_name_when_missing(monkeypatch):
    diagnosis_db, _ = _load_modules(monkeypatch)
    created = []
    updated = []
    profile_rows = []

    def fake_list_profiles(brand_ids=None):
        return list(profile_rows)

    def fake_create_profile(**kwargs):
        created.append(kwargs)
        profile_rows.append({"id": "p615abc1", "brand_id": kwargs["brand_id"]})
        return "p615abc1"

    def fake_update_profile(profile_id, **kwargs):
        updated.append((profile_id, kwargs))
        return True

    monkeypatch.setattr("db.profile_db.list_profiles", fake_list_profiles)
    monkeypatch.setattr("db.profile_db.create_profile", fake_create_profile)
    monkeypatch.setattr("db.profile_db.update_profile", fake_update_profile)
    monkeypatch.setattr(
        diagnosis_db,
        "get_connection",
        lambda: _FakeConnection({"name": "深圳栖舍设计装修有限公司", "industry": "装修设计"}),
    )

    diagnosis_db._sync_materials_to_profiles(615, {
        "company_intro": "栖舍是一家深圳装修设计公司。",
        "unique_value": "全案设计与施工协同",
        "core_selling_points": [{"point": "透明报价", "evidence": "合同明细"}],
    })

    assert created == [{
        "name": "深圳栖舍设计装修有限公司",
        "industry": "装修设计",
        "brand_id": 615,
        "company_intro": "栖舍是一家深圳装修设计公司。",
        "core_value": "全案设计与施工协同",
        "selling_points": "透明报价",
    }]
    assert updated == [(
        "p615abc1",
        {
            "_is_internal_sync": True,
            "company_intro": "栖舍是一家深圳装修设计公司。",
            "core_value": "全案设计与施工协同",
            "selling_points": "透明报价",
        },
    )]


def test_persist_cleaned_materials_creates_profile_before_structured_knowledge(monkeypatch):
    _, m3_api = _load_modules(monkeypatch)
    created = []
    structured_writes = []
    profile_rows = []

    def fake_list_profiles(brand_ids=None):
        return list(profile_rows)

    def fake_create_profile(**kwargs):
        created.append(kwargs)
        profile_rows.append({"id": "p615sk01", "brand_id": kwargs["brand_id"]})
        return "p615sk01"

    monkeypatch.setattr(m3_api, "save_client_materials", lambda diagnosis_id, materials, **_kwargs: 1)
    monkeypatch.setattr(m3_api, "_get_brand_info", lambda brand_id: {
        "name": "深圳栖舍设计装修有限公司",
        "industry": "装修设计",
    })
    monkeypatch.setattr("db.profile_db.list_profiles", fake_list_profiles)
    monkeypatch.setattr("db.profile_db.create_profile", fake_create_profile)
    monkeypatch.setattr(m3_api, "_persist_structured_knowledge", lambda profile_id, cleaned: structured_writes.append((profile_id, cleaned)))

    cleaned = {
        "company_intro": "栖舍资料已整理。",
        "structured_knowledge": {"products": ["办公室装修"], "customers": ["企业客户"]},
    }

    m3_api._persist_cleaned_materials(615, 9001, cleaned)

    assert created == [{
        "name": "深圳栖舍设计装修有限公司",
        "industry": "装修设计",
        "brand_id": 615,
    }]
    assert structured_writes == [("p615sk01", cleaned)]


def test_persist_cleaned_materials_uses_existing_profile_without_creating(monkeypatch):
    _, m3_api = _load_modules(monkeypatch)
    created = []
    structured_writes = []

    monkeypatch.setattr(m3_api, "save_client_materials", lambda diagnosis_id, materials, **_kwargs: 1)
    monkeypatch.setattr("db.profile_db.list_profiles", lambda brand_ids=None: [{"id": "existing1"}])
    monkeypatch.setattr("db.profile_db.create_profile", lambda **kwargs: created.append(kwargs))
    monkeypatch.setattr(m3_api, "_persist_structured_knowledge", lambda profile_id, cleaned: structured_writes.append((profile_id, cleaned)))

    cleaned = {"structured_knowledge": {"products": ["既有档案继续写"]}}

    m3_api._persist_cleaned_materials(615, 9001, cleaned)

    assert created == []
    assert structured_writes == [("existing1", cleaned)]


def test_persist_cleaned_materials_without_diagnosis_keeps_structured_knowledge(monkeypatch):
    _, m3_api = _load_modules(monkeypatch)
    created = []
    structured_writes = []
    profile_rows = []

    def fake_list_profiles(brand_ids=None):
        return list(profile_rows)

    def fake_create_profile(**kwargs):
        created.append(kwargs)
        profile_rows.append({"id": "p615only", "brand_id": kwargs["brand_id"]})
        return "p615only"

    monkeypatch.setattr(m3_api, "_get_brand_info", lambda brand_id: {
        "name": "深圳栖舍设计装修有限公司",
        "industry": "装修设计",
    })
    monkeypatch.setattr("db.profile_db.list_profiles", fake_list_profiles)
    monkeypatch.setattr("db.profile_db.create_profile", fake_create_profile)
    monkeypatch.setattr(m3_api, "_persist_structured_knowledge", lambda profile_id, cleaned: structured_writes.append((profile_id, cleaned)))

    cleaned = {"structured_knowledge": {"products": ["办公室装修"]}}

    m3_api._persist_cleaned_materials(615, None, cleaned)

    assert created == [{
        "name": "深圳栖舍设计装修有限公司",
        "industry": "装修设计",
        "brand_id": 615,
    }]
    assert structured_writes == [("p615only", cleaned)]
