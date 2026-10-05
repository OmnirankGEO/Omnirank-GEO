import json

import db.profile_db as profile_db


class FakeCursor:
    def __init__(self):
        self.executed = []
        self.rowcount = 1

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        return None


class FakeConnection:
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


def _json_param(params, needle):
    for value in params:
        if isinstance(value, str) and needle in value:
            return json.loads(value)
    raise AssertionError(f"JSON param containing {needle!r} not found in {params!r}")


def test_create_profile_serializes_structured_case_and_testimonial_payloads(monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(profile_db, "_ensure_columns", lambda: None)
    monkeypatch.setattr(profile_db, "get_connection", lambda: fake_conn)

    profile_id = profile_db.create_profile(
        name="深圳市栖舍设计装饰有限公司",
        success_cases=[{"client": "刘女士", "results": "满意并转介绍"}],
        testimonials=[{"name": "刘女士", "quote": "全程省心靠谱"}],
        structured_knowledge={"methodology": "兵将料分离"},
    )

    assert profile_id
    assert fake_conn.committed is True
    assert fake_conn.closed is True
    _, params = fake_conn.cursor_obj.executed[-1]
    assert all(not isinstance(value, (list, dict)) for value in params)
    assert _json_param(params, "满意并转介绍")[0]["client"] == "刘女士"
    assert _json_param(params, "全程省心靠谱")[0]["name"] == "刘女士"
    assert _json_param(params, "兵将料分离")["methodology"] == "兵将料分离"


def test_update_profile_serializes_testimonials_like_other_json_fields(monkeypatch):
    fake_conn = FakeConnection()
    monkeypatch.setattr(profile_db, "get_connection", lambda: fake_conn)
    monkeypatch.setattr(profile_db, "_sync_to_brand", lambda *args, **kwargs: None)

    ok = profile_db.update_profile(
        "profile-615",
        success_cases=[{"client": "企业高管", "results": "介绍 5 个新客户"}],
        testimonials=[{"name": "黄总", "quote": "验收彻底放心"}],
        structured_knowledge={"service_flow": "一周一结"},
    )

    assert ok is True
    assert fake_conn.committed is True
    assert fake_conn.closed is True
    _, params = fake_conn.cursor_obj.executed[-1]
    assert all(not isinstance(value, (list, dict)) for value in params)
    assert _json_param(params, "介绍 5 个新客户")[0]["client"] == "企业高管"
    assert _json_param(params, "验收彻底放心")[0]["name"] == "黄总"
    assert _json_param(params, "一周一结")["service_flow"] == "一周一结"
