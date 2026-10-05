from __future__ import annotations

import json
from contextlib import asynccontextmanager
from copy import deepcopy

import pytest

from services import writing_competitor_verification as verification
from tools.api_source_pool import SourcePool


class _Response:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Tracker:
    def __init__(self, record: dict):
        self.record_data = record

    def record(self, **kwargs):
        self.record_data["result"] = kwargs


def _install_tracker(monkeypatch):
    tracked: list[dict] = []

    @asynccontextmanager
    async def fake_track(caller, platform, **kwargs):
        record = {"caller": caller, "platform": platform, **kwargs}
        tracked.append(record)
        yield _Tracker(record)

    monkeypatch.setattr(verification, "llm_track", fake_track)
    return tracked


def _install_client(monkeypatch, responder):
    requests: list[dict] = []

    class _Client:
        def __init__(self, *args, **kwargs):
            self.timeout = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, **kwargs):
            request = {"url": url, **kwargs}
            requests.append(request)
            key = kwargs["headers"]["Authorization"].removeprefix("Bearer ")
            return responder(key, kwargs["json"]["q"])

    monkeypatch.setattr(verification.httpx, "AsyncClient", _Client)
    return requests


def _pool(keys=("test-key",), qpm=200):
    return SourcePool("metaso-test", list(keys), qpm_per_source=qpm)


async def _verify(competitors, *, pool=None, **kwargs):
    return await verification.verify_persisted_competitor_names(
        competitors,
        industry=kwargs.get("industry", "家居制造业"),
        region=kwargs.get("region", "深圳"),
        quote_id=kwargs.get("quote_id", 77),
        request_id=kwargs.get("request_id", "req-test-77"),
        user_id=kwargs.get("user_id", 9),
        provider_pool=pool or _pool(),
        max_concurrency=kwargs.get("max_concurrency", 4),
    )


@pytest.mark.asyncio
async def test_verifies_only_pending_names_and_preserves_list(monkeypatch):
    _install_tracker(monkeypatch)

    def responder(_key: str, query: str):
        if "尚品宅配" in query:
            return _Response(
                200,
                {
                    "webpages": [
                        {
                            "title": "深圳尚品宅配南山区门店地址与服务介绍",
                            "snippet": "深圳尚品宅配南山区门店真实营业信息",
                            "link": "https://example.com/shangpin",
                        }
                    ]
                },
            )
        return _Response(
            200,
            {
                "webpages": [
                    {
                        "title": "全屋定制门店选购指南",
                        "snippet": "未出现目标企业全称",
                        "link": "https://example.com/guide",
                    }
                ]
            },
        )

    requests = _install_client(monkeypatch, responder)
    original = [
        {"name": "已核验品牌", "name_verified": True, "custom": "keep-a"},
        {"name": "深圳尚品宅配南山区门店", "custom": "keep-b", "selected": True},
        {"name": "欧派全屋定制南山区门店", "custom": "keep-c"},
        {"name": "人工核验品牌", "human_verified_name": True, "custom": "keep-d"},
        {"name": "已排除品牌", "excluded": True, "custom": "keep-e"},
    ]

    result = await _verify(original)

    assert len(requests) == 2
    assert result["attempted_count"] == 2
    assert result["newly_verified"] == 1
    assert result["verified_count"] == 3
    assert result["pending_names"] == ["欧派全屋定制南山区门店"]
    assert result["provider_error_count"] == 0
    assert [item["name"] for item in result["competitors"]] == [item["name"] for item in original]
    assert [item["custom"] for item in result["competitors"]] == [item["custom"] for item in original]
    assert result["competitors"][1]["selected"] is True
    assert result["competitors"][1]["name_verified"] is True
    assert result["competitors"][1]["name_verification_source_url"] == "https://example.com/shangpin"
    assert "name_verified" not in result["competitors"][2]
    assert original[1] == {"name": "深圳尚品宅配南山区门店", "custom": "keep-b", "selected": True}


@pytest.mark.asyncio
async def test_fully_verified_list_needs_no_provider_pool(monkeypatch):
    monkeypatch.setattr(
        verification,
        "get_metaso_pool",
        lambda: (_ for _ in ()).throw(AssertionError("pool must not be loaded")),
    )
    result = await verification.verify_persisted_competitor_names(
        [{"name": "品牌甲", "name_verified": True}],
        industry="",
        region="",
        quote_id=77,
        request_id="req-no-provider",
    )
    assert result["attempted_count"] == 0
    assert result["verified_count"] == 1


@pytest.mark.asyncio
async def test_pending_list_without_key_is_unavailable(monkeypatch):
    _install_tracker(monkeypatch)
    with pytest.raises(verification.CompetitorVerificationUnavailable):
        await _verify([{"name": "待核验品牌"}], pool=_pool(keys=()))


@pytest.mark.asyncio
async def test_first_key_failure_fails_over_and_tracks_each_real_post(monkeypatch):
    tracked = _install_tracker(monkeypatch)

    def responder(key: str, _query: str):
        if key == "key-one":
            return _Response(503, {"error": "unavailable"})
        return _Response(
            200,
            {
                "webpages": [
                    {"title": "待核验品牌官网", "snippet": "待核验品牌", "link": "https://example.com/ok"}
                ]
            },
        )

    requests = _install_client(monkeypatch, responder)
    result = await _verify([{"name": "待核验品牌"}], pool=_pool(("key-one", "key-two")))

    assert [r["headers"]["Authorization"] for r in requests] == ["Bearer key-one", "Bearer key-two"]
    assert result["newly_verified"] == 1
    assert len(tracked) == 2
    assert tracked[0]["result"] == {"success": False, "error_msg": "metaso_http_503"}
    assert tracked[1]["result"] == {"success": True}


@pytest.mark.asyncio
async def test_malformed_success_payload_fails_over_before_tracking_success(monkeypatch):
    tracked = _install_tracker(monkeypatch)

    def responder(key: str, _query: str):
        if key == "key-one":
            return _Response(200, {"webpages": {"not": "a list"}})
        return _Response(200, {"webpages": []})

    requests = _install_client(monkeypatch, responder)
    result = await _verify([{"name": "待核验品牌"}], pool=_pool(("key-one", "key-two")))

    assert len(requests) == 2
    assert result["provider_error_count"] == 0
    assert tracked[0]["result"] == {"success": False, "error_msg": "metaso_invalid_payload"}
    assert tracked[1]["result"] == {"success": True}


@pytest.mark.asyncio
async def test_all_provider_failures_preserve_input_and_are_not_no_evidence(monkeypatch):
    _install_tracker(monkeypatch)
    requests = _install_client(monkeypatch, lambda _key, _query: _Response(429, {"error": "rate limited"}))
    competitors = [{"name": "品牌甲"}, {"name": "品牌乙"}]
    before = deepcopy(competitors)

    with pytest.raises(verification.CompetitorVerificationUnavailable):
        await _verify(competitors, pool=_pool(("key-one", "key-two")))

    assert len(requests) == 4
    assert competitors == before


@pytest.mark.asyncio
async def test_pool_qpm_is_a_hard_upper_bound(monkeypatch):
    _install_tracker(monkeypatch)
    requests = _install_client(
        monkeypatch,
        lambda _key, _query: _Response(
            200,
            {"webpages": [{"title": "其他品牌", "snippet": "无匹配", "link": "https://example.com/no"}]},
        ),
    )
    result = await _verify(
        [{"name": "候选甲"}, {"name": "候选乙"}, {"name": "候选丙"}],
        pool=_pool(("key-one", "key-two"), qpm=1),
    )
    assert len(requests) == 2
    assert result["provider_error_count"] == 1
    assert result["newly_verified"] == 0


@pytest.mark.asyncio
async def test_tracking_metadata_has_business_ids_without_name_or_key(monkeypatch):
    tracked = _install_tracker(monkeypatch)
    _install_client(
        monkeypatch,
        lambda _key, _query: _Response(200, {"webpages": []}),
    )
    await _verify(
        [{"name": "绝不进入追踪的候选名称"}],
        pool=_pool(("super-secret-key",)),
        quote_id=314,
        request_id="req-safe-314",
        user_id=271,
    )

    assert len(tracked) == 1
    record = tracked[0]
    assert record["quote_id"] == 314
    assert record["user_id"] == 271
    assert record["metadata"] == {
        "candidate_index": 0,
        "purpose": "writing_competitor_name_verify",
        "quote_id": 314,
        "request_id": "req-safe-314",
    }
    serialized = json.dumps(record, ensure_ascii=False)
    assert "绝不进入追踪的候选名称" not in serialized
    assert "super-secret-key" not in serialized


@pytest.mark.asyncio
async def test_private_or_credentialed_source_urls_never_verify(monkeypatch):
    _install_tracker(monkeypatch)
    urls = iter(["http://127.0.0.1/internal", "https://user:pass@example.com/private"])

    def responder(_key: str, _query: str):
        return _Response(
            200,
            {
                "webpages": [
                    {"title": "待核验品牌真实资料", "snippet": "待核验品牌", "link": next(urls)}
                ]
            },
        )

    _install_client(monkeypatch, responder)
    result = await _verify([{"name": "待核验品牌"}, {"name": "待核验品牌"}], pool=_pool(("a", "b")))
    assert result["newly_verified"] == 0
    assert result["pending_names"] == ["待核验品牌", "待核验品牌"]
    assert result["provider_error_count"] == 0


class _StateCursor:
    def __init__(self, connection):
        self.connection = connection
        self.row = None

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        state = self.connection.state
        if normalized.startswith("SELECT competitor_list") and "FROM quotes" in normalized:
            self.row = dict(state)
        elif normalized.startswith("SELECT owner_user_id"):
            self.row = {"owner_user_id": self.connection.owner_user_id} if self.connection.brand_exists else None
        elif normalized.startswith("SELECT 1") and "FROM user_clients" in normalized:
            self.row = {"?column?": 1} if params in self.connection.assignments else None
        elif normalized.startswith("UPDATE quotes"):
            state["competitor_list"] = params[0]
            state["competitor_mode"] = params[1]
            self.connection.writes += 1
            self.row = None
        else:
            raise AssertionError(normalized)

    def fetchone(self):
        return self.row

    def close(self):
        pass


class _StateConnection:
    def __init__(self, state, *, owner_user_id=99, assignments=()):
        self.state = state
        self.owner_user_id = owner_user_id
        self.brand_exists = True
        self.assignments = set(assignments)
        self.writes = 0

    def cursor(self):
        return _StateCursor(self)


def _fingerprint(state):
    return verification.competitor_verification_fingerprint(
        state["competitor_list"],
        state["competitor_mode"],
        state["industry"],
        state["city"],
        state["brand_id"],
    )


@pytest.mark.parametrize("field,new_value", [("industry", "新行业"), ("city", "新城市")])
def test_scope_change_after_provider_is_409_equivalent_and_zero_write(field, new_value):
    state = {
        "competitor_list": '[{"name":"候选甲"}]',
        "competitor_mode": "semi",
        "industry": "旧行业",
        "city": "旧城市",
        "brand_id": 5,
    }
    expected = _fingerprint(state)
    state[field] = new_value
    connection = _StateConnection(state, owner_user_id=7)

    with pytest.raises(verification.CompetitorVerificationConflict):
        verification.persist_competitor_verification_result(
            connection,
            quote_id=10,
            expected_fingerprint=expected,
            competitors=[{"name": "候选甲", "name_verified": True}],
            result_mode="real",
            user={"user_id": 7, "is_admin": False},
        )
    assert connection.writes == 0
    assert "name_verified" not in state["competitor_list"]


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("competitor_list", '[{"name":"另一页面新候选"}]'),
        ("competitor_mode", "evidence_only"),
    ],
)
def test_candidate_state_change_after_provider_is_not_overwritten(field, new_value):
    state = {
        "competitor_list": '[{"name":"候选甲"}]',
        "competitor_mode": "semi",
        "industry": "行业",
        "city": "城市",
        "brand_id": 5,
    }
    expected = _fingerprint(state)
    state[field] = new_value
    connection = _StateConnection(state, owner_user_id=7)

    with pytest.raises(verification.CompetitorVerificationConflict):
        verification.persist_competitor_verification_result(
            connection,
            quote_id=10,
            expected_fingerprint=expected,
            competitors=[{"name": "候选甲", "name_verified": True}],
            result_mode="real",
            user={"user_id": 7, "is_admin": False},
        )
    assert connection.writes == 0
    assert state[field] == new_value


def test_revoked_assignment_after_provider_is_forbidden_and_zero_write():
    state = {
        "competitor_list": '[{"name":"候选甲"}]',
        "competitor_mode": "semi",
        "industry": "行业",
        "city": "城市",
        "brand_id": 5,
    }
    connection = _StateConnection(state, owner_user_id=99, assignments=())
    with pytest.raises(verification.CompetitorVerificationForbidden):
        verification.persist_competitor_verification_result(
            connection,
            quote_id=10,
            expected_fingerprint=_fingerprint(state),
            competitors=[{"name": "候选甲", "name_verified": True}],
            result_mode="real",
            user={"user_id": 7, "is_admin": False, "client_brand_ids": [5]},
        )
    assert connection.writes == 0
    assert "name_verified" not in state["competitor_list"]


def test_live_assignment_can_persist_and_preserves_admin_semantics():
    for user, assignments in [
        ({"user_id": 7, "is_admin": False}, {(7, 5)}),
        ({"user_id": 1, "is_admin": True}, set()),
    ]:
        state = {
            "competitor_list": '[{"name":"候选甲"}]',
            "competitor_mode": "semi",
            "industry": "行业",
            "city": "城市",
            "brand_id": 5,
        }
        connection = _StateConnection(state, owner_user_id=99, assignments=assignments)
        verification.persist_competitor_verification_result(
            connection,
            quote_id=10,
            expected_fingerprint=_fingerprint(state),
            competitors=[{"name": "候选甲", "name_verified": True}],
            result_mode="real",
            user=user,
        )
        assert connection.writes == 1
        assert json.loads(state["competitor_list"])[0]["name_verified"] is True


def test_owner_with_numeric_string_user_id_is_authorized():
    state = {
        "competitor_list": '[{"name":"候选甲"}]',
        "competitor_mode": "semi",
        "industry": "行业",
        "city": "城市",
        "brand_id": "5",
    }
    connection = _StateConnection(state, owner_user_id=7)
    verification.persist_competitor_verification_result(
        connection,
        quote_id=10,
        expected_fingerprint=_fingerprint(state),
        competitors=[{"name": "候选甲", "name_verified": True}],
        result_mode="real",
        user={"user_id": "7", "is_admin": False},
    )
    assert connection.writes == 1
    assert json.loads(state["competitor_list"])[0]["name_verified"] is True


@pytest.mark.asyncio
async def test_capacity_rejection_happens_before_access_check_or_db_checkout():
    gate = verification.VerificationAdmissionGate(1)
    assert gate.try_acquire() is True
    calls: list[str] = []

    with pytest.raises(verification.CompetitorVerificationCapacityExceeded):
        await verification.execute_competitor_verification_request(
            quote_id=10,
            request_id="req-capacity-reject",
            user={"user_id": 7, "is_admin": False},
            user_id=7,
            initial_access_check=lambda: calls.append("access"),
            connection_factory=lambda: calls.append("db"),
            provider_pool=_pool(),
            admission_gate=gate,
        )

    assert calls == []
    gate.release()


def test_ui_and_api_use_pool_singleflight_live_auth_and_scope_cas():
    server = open("server.py", encoding="utf-8").read()
    service = open("services/writing_competitor_verification.py", encoding="utf-8").read()
    frontend = open("frontend/src/pages/Writing/WritingHall.tsx", encoding="utf-8").read()

    route = '@app.post("/api/writing/competitors/{quote_id}/verify")'
    assert route in server
    route_body = server[server.index(route) : server.index('@app.put("/api/writing/competitors/{quote_id}/mode")')]
    assert route_body.count("require_quote_access(request, quote_id)") == 1
    assert route_body.count("execute_competitor_verification_request(") == 1
    assert "connection_factory=get_connection" in route_body
    assert "read_conn" not in route_body
    assert "live_access_conn" not in route_body
    assert "write_conn" not in route_body
    assert "METASO_API_KEY" not in route_body

    execute_start = service.index("async def execute_competitor_verification_request(")
    execute_body = service[execute_start:]
    assert execute_body.index("gate.try_acquire()") < execute_body.index("initial_access_check()")
    assert execute_body.index("initial_access_check()") < execute_body.index("connection_factory()")
    assert execute_body.count("connection_factory()") == 1

    session_start = service.index("async def verify_quote_competitors_with_session_lock(")
    session_body = service[session_start:execute_start]
    assert session_body.index("try_acquire_quote_verification_lock") < session_body.index(
        "load_competitor_verification_inputs("
    )
    assert session_body.index("load_competitor_verification_inputs(") < session_body.index(
        "verify_persisted_competitor_names("
    )
    assert session_body.index("verify_persisted_competitor_names(") < session_body.index(
        "persist_competitor_verification_result("
    )
    assert "connection.autocommit = True" in session_body
    assert "`/api/writing/competitors/${selectedProject.id}/verify`" in frontend
    assert "await verifyCurrentCompetitors()" in frontend
