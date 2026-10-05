from types import SimpleNamespace


class _Settings:
    report_v3_enabled = False
    report_v3_whitelist_user_ids = [42]


def _request(user=None, preview=False):
    return SimpleNamespace(
        state=SimpleNamespace(user=user),
        query_params={"preview_v3": "1"} if preview else {},
    )


def test_should_render_report_v3_allows_admin_preview():
    from services.report_v3_gating import should_render_report_v3

    assert should_render_report_v3(_Settings(), None, True) is True


def test_should_render_report_v3_uses_whitelist_when_global_off():
    from services.report_v3_gating import should_render_report_v3

    assert should_render_report_v3(_Settings(), 42, False) is True
    assert should_render_report_v3(_Settings(), 7, False) is False


def test_resolve_report_v3_subject_prefers_shared_by_then_brand_owner():
    from services.report_v3_gating import resolve_report_v3_subject_user_id

    req = _request(user={"user_id": 3})
    assert resolve_report_v3_subject_user_id(
        request=req,
        diagnosis_record={"brand_owner_user_id": 9},
        shared_by=88,
    ) == 88
    assert resolve_report_v3_subject_user_id(
        request=req,
        diagnosis_record={"brand_owner_user_id": 9},
        shared_by=None,
    ) == 9


def test_resolve_report_v3_subject_falls_back_to_brand_lookup(monkeypatch):
    from services import report_v3_gating

    class Cursor:
        def execute(self, query, params):
            self.params = params

        def fetchone(self):
            return {"owner_user_id": 77}

    class Conn:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(report_v3_gating, "get_connection", lambda: Conn())

    assert report_v3_gating.resolve_report_v3_subject_user_id(
        request=_request(),
        diagnosis_record={"brand_id": 12},
        shared_by=None,
    ) == 77
