from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool

from services import writing_competitor_verification as verification
from tools.api_source_pool import SourcePool


def _test_dsn() -> str:
    dsn = os.getenv("WRITING_VERIFICATION_TEST_DATABASE_URL") or os.getenv("TEST_DATABASE_URL") or ""
    if not dsn:
        pytest.skip("local throwaway PostgreSQL is required")
    parsed = urlparse(dsn)
    database = (parsed.path or "").lstrip("/").lower()
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("writing verification tests refuse non-local PostgreSQL")
    if "test" not in database and "throwaway" not in database:
        pytest.fail("writing verification tests require a database name containing test or throwaway")
    return dsn


def _connect(*, schema: str | None = None):
    connection = psycopg2.connect(_test_dsn(), cursor_factory=RealDictCursor)
    if schema:
        with connection.cursor() as cursor:
            cursor.execute(f'SET search_path TO "{schema}", public')
        connection.commit()
    return connection


class _Tracker:
    def record(self, **_kwargs):
        return None


@asynccontextmanager
async def _fake_track(*_args, **_kwargs):
    yield _Tracker()


class _Response:
    status_code = 200

    @staticmethod
    def json():
        return {
            "webpages": [
                {
                    "title": "并发候选品牌官网",
                    "snippet": "并发候选品牌",
                    "link": "https://example.com/evidence",
                }
            ]
        }


class _PoolLease:
    def __init__(self, raw, pool, state: dict[str, int], state_lock: threading.Lock):
        self._raw = raw
        self._pool = pool
        self._state = state
        self._state_lock = state_lock
        self._closed = False
        with self._state_lock:
            self._state["active"] += 1
            self._state["peak"] = max(self._state["peak"], self._state["active"])

    @property
    def autocommit(self):
        return self._raw.autocommit

    @autocommit.setter
    def autocommit(self, value):
        self._raw.autocommit = value

    def cursor(self, *args, **kwargs):
        return self._raw.cursor(*args, **kwargs)

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._pool.putconn(self._raw)
        with self._state_lock:
            self._state["active"] -= 1


def _local_pool(schema: str, *, application_name: str):
    return ThreadedConnectionPool(
        minconn=1,
        maxconn=25,
        dsn=_test_dsn(),
        cursor_factory=RealDictCursor,
        application_name=application_name,
        options=f"-c search_path={schema},public",
    )


def test_twenty_workers_call_provider_once_per_candidate(monkeypatch):
    monkeypatch.setattr(verification, "llm_track", _fake_track)
    post_queries: list[str] = []
    post_lock = threading.Lock()

    class _Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **kwargs):
            with post_lock:
                post_queries.append(kwargs["json"]["q"])
            await asyncio.sleep(0.01)
            return _Response()

    monkeypatch.setattr(verification.httpx, "AsyncClient", _Client)
    worker_count = 20
    quote_id = 810000 + (uuid.uuid4().int % 100000)
    start = threading.Barrier(worker_count)
    attempted = threading.Event()
    attempt_count = 0
    attempt_lock = threading.Lock()
    winners: list[int] = []
    errors: list[BaseException] = []

    def worker(index: int):
        nonlocal attempt_count
        connection = _connect()
        connection.autocommit = True
        acquired = False
        try:
            start.wait(timeout=10)
            acquired = verification.try_acquire_quote_verification_lock(connection, quote_id)
            with attempt_lock:
                attempt_count += 1
                if acquired:
                    winners.append(index)
                if attempt_count == worker_count:
                    attempted.set()
            if not acquired:
                return
            assert attempted.wait(timeout=10)
            result = asyncio.run(
                verification.verify_persisted_competitor_names(
                    [{"name": "并发候选品牌"}, {"name": "第二候选品牌"}],
                    industry="制造业",
                    region="深圳",
                    quote_id=quote_id,
                    request_id="req-pg-20",
                    user_id=7,
                    provider_pool=SourcePool("metaso-pg", ["key-one"], qpm_per_source=20),
                )
            )
            assert result["attempted_count"] == 2
        except BaseException as exc:
            errors.append(exc)
        finally:
            if acquired:
                verification.release_quote_verification_lock(connection, quote_id)
            connection.close()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(worker_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert len(winners) == 1
    assert len(post_queries) == 2
    assert sum("并发候选品牌" in query for query in post_queries) == 1
    assert sum("第二候选品牌" in query for query in post_queries) == 1


@pytest.fixture
def verification_schema():
    schema = f"writing_verify_{uuid.uuid4().hex[:12]}"
    admin = _connect()
    admin.autocommit = True
    with admin.cursor() as cursor:
        cursor.execute(f'CREATE SCHEMA "{schema}"')
        cursor.execute(
            f"""
            CREATE TABLE "{schema}".brands (
                id BIGINT PRIMARY KEY,
                owner_user_id BIGINT NOT NULL,
                is_deleted BOOLEAN DEFAULT FALSE
            );
            CREATE TABLE "{schema}".user_clients (
                user_id BIGINT NOT NULL,
                brand_id BIGINT NOT NULL,
                PRIMARY KEY (user_id, brand_id)
            );
            CREATE TABLE "{schema}".quotes (
                id BIGINT PRIMARY KEY,
                competitor_list TEXT NOT NULL,
                competitor_mode TEXT NOT NULL,
                industry TEXT,
                city TEXT,
                brand_id BIGINT
            );
            """
        )
    try:
        yield schema
    finally:
        with admin.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()


def _seed_scope(schema: str, *, assigned: bool = True):
    connection = _connect(schema=schema)
    with connection.cursor() as cursor:
        cursor.execute("INSERT INTO brands (id, owner_user_id) VALUES (5, 99)")
        if assigned:
            cursor.execute("INSERT INTO user_clients (user_id, brand_id) VALUES (7, 5)")
        cursor.execute(
            """
            INSERT INTO quotes (id, competitor_list, competitor_mode, industry, city, brand_id)
            VALUES (10, %s, 'semi', '旧行业', '旧城市', 5)
            """,
            ('[{"name":"候选甲"}]',),
        )
    connection.commit()
    connection.close()


def _seed_many_quotes(schema: str, quote_ids: list[int]):
    connection = _connect(schema=schema)
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO quotes (id, competitor_list, competitor_mode, industry, city, brand_id)
            VALUES (%s, %s, 'semi', '制造业', '深圳', NULL)
            """,
            [(quote_id, '[{"name":"并发候选品牌"}]') for quote_id in quote_ids],
        )
    connection.commit()
    connection.close()


def _current_fingerprint(schema: str) -> str:
    connection = _connect(schema=schema)
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT competitor_list, competitor_mode, industry, city, brand_id FROM quotes WHERE id = 10"
        )
        row = cursor.fetchone()
    connection.close()
    return verification.competitor_verification_fingerprint(
        row["competitor_list"], row["competitor_mode"], row["industry"], row["city"], row["brand_id"]
    )


def _assert_quote_unchanged(schema: str):
    connection = _connect(schema=schema)
    with connection.cursor() as cursor:
        cursor.execute("SELECT competitor_list, competitor_mode FROM quotes WHERE id = 10")
        row = cursor.fetchone()
    connection.close()
    assert json.loads(row["competitor_list"]) == [{"name": "候选甲"}]
    assert row["competitor_mode"] == "semi"


def _exercise_provider_barrier(
    monkeypatch,
    schema: str,
    *,
    mutation_sql: str,
    mutation_params: tuple,
    expected_exception: type[BaseException],
):
    provider_entered = threading.Event()
    provider_release = threading.Event()
    outcomes: list[BaseException | None] = []
    expected = _current_fingerprint(schema)
    monkeypatch.setattr(verification, "llm_track", _fake_track)

    class _BarrierResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "webpages": [
                    {
                        "title": "候选甲官方网站",
                        "snippet": "候选甲真实资料",
                        "link": "https://example.com/candidate-a",
                    }
                ]
            }

    class _BarrierClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            provider_entered.set()
            released = await asyncio.to_thread(provider_release.wait, 10)
            assert released
            return _BarrierResponse()

    monkeypatch.setattr(verification.httpx, "AsyncClient", _BarrierClient)

    def worker():
        writer = None
        try:
            result = asyncio.run(
                verification.verify_persisted_competitor_names(
                    [{"name": "候选甲"}],
                    industry="旧行业",
                    region="旧城市",
                    quote_id=10,
                    request_id="req-provider-barrier",
                    user_id=7,
                    provider_pool=SourcePool("metaso-barrier", ["key-one"], qpm_per_source=10),
                )
            )
            assert result["newly_verified"] == 1
            writer = _connect(schema=schema)
            verification.persist_competitor_verification_result(
                writer,
                quote_id=10,
                expected_fingerprint=expected,
                competitors=result["competitors"],
                result_mode="real",
                user={"user_id": 7, "is_admin": False, "client_brand_ids": [5]},
            )
            writer.commit()
            outcomes.append(None)
        except BaseException as exc:
            if writer is not None:
                writer.rollback()
            outcomes.append(exc)
        finally:
            if writer is not None:
                writer.close()

    thread = threading.Thread(target=worker)
    thread.start()
    assert provider_entered.wait(timeout=10)
    mutator = _connect(schema=schema)
    with mutator.cursor() as cursor:
        cursor.execute(mutation_sql, mutation_params)
    mutator.commit()
    mutator.close()
    provider_release.set()
    thread.join(timeout=15)

    assert not thread.is_alive()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], expected_exception)
    _assert_quote_unchanged(schema)


def test_access_revoked_during_provider_barrier_cannot_write(monkeypatch, verification_schema):
    _seed_scope(verification_schema)
    _exercise_provider_barrier(
        monkeypatch,
        verification_schema,
        mutation_sql="DELETE FROM user_clients WHERE user_id = %s AND brand_id = %s",
        mutation_params=(7, 5),
        expected_exception=verification.CompetitorVerificationForbidden,
    )


@pytest.mark.parametrize("column,value", [("industry", "新行业"), ("city", "新城市")])
def test_scope_changed_during_provider_barrier_is_conflict_and_zero_write(
    monkeypatch, verification_schema, column, value
):
    _seed_scope(verification_schema)
    _exercise_provider_barrier(
        monkeypatch,
        verification_schema,
        mutation_sql=f"UPDATE quotes SET {column} = %s WHERE id = 10",
        mutation_params=(value,),
        expected_exception=verification.CompetitorVerificationConflict,
    )


def test_capacity_gate_prevents_pool_exhaustion_and_releases_for_followup(
    monkeypatch, verification_schema
):
    monkeypatch.setattr(verification, "llm_track", _fake_track)
    quote_ids = list(range(910001, 910032))
    _seed_many_quotes(verification_schema, quote_ids)

    gate_limit = 8
    gate = verification.VerificationAdmissionGate(gate_limit)
    application_name = f"writing_verify_capacity_{uuid.uuid4().hex[:8]}"
    pool = _local_pool(verification_schema, application_name=application_name)
    lease_state = {"active": 0, "peak": 0}
    lease_lock = threading.Lock()
    provider_release = threading.Event()
    all_admitted_in_provider = threading.Event()
    provider_calls = 0
    provider_lock = threading.Lock()

    class _BlockingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            nonlocal provider_calls
            with provider_lock:
                provider_calls += 1
                if provider_calls == gate_limit:
                    all_admitted_in_provider.set()
            released = await asyncio.to_thread(provider_release.wait, 15)
            assert released
            return _Response()

    monkeypatch.setattr(verification.httpx, "AsyncClient", _BlockingClient)

    def connection_factory():
        raw = pool.getconn()
        return _PoolLease(raw, pool, lease_state, lease_lock)

    start = threading.Barrier(30)
    outcomes: list[tuple[int, str]] = []
    outcome_lock = threading.Lock()

    def worker(quote_id: int):
        try:
            start.wait(timeout=10)
            asyncio.run(
                verification.execute_competitor_verification_request(
                    quote_id=quote_id,
                    request_id=f"req-capacity-{quote_id}",
                    user={"user_id": 1, "is_admin": True},
                    user_id=1,
                    initial_access_check=lambda: None,
                    connection_factory=connection_factory,
                    provider_pool=SourcePool(
                        f"metaso-capacity-{quote_id}", ["key-one"], qpm_per_source=1000
                    ),
                    admission_gate=gate,
                )
            )
            outcome = "success"
        except verification.CompetitorVerificationCapacityExceeded:
            outcome = "capacity"
        except BaseException as exc:
            outcome = f"error:{type(exc).__name__}:{exc}"
        with outcome_lock:
            outcomes.append((quote_id, outcome))

    threads = [threading.Thread(target=worker, args=(quote_id,)) for quote_id in quote_ids[:30]]
    try:
        for thread in threads:
            thread.start()
        assert all_admitted_in_provider.wait(timeout=15)

        observer = _connect(schema=verification_schema)
        with observer.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS sessions,
                       COUNT(*) FILTER (WHERE xact_start IS NOT NULL) AS open_transactions
                  FROM pg_stat_activity
                 WHERE application_name = %s
                """,
                (application_name,),
            )
            activity = cursor.fetchone()
        observer.close()
        assert activity["sessions"] == gate_limit
        assert activity["open_transactions"] == 0
        with lease_lock:
            assert lease_state == {"active": gate_limit, "peak": gate_limit}

        provider_release.set()
        for thread in threads:
            thread.join(timeout=20)
        assert not any(thread.is_alive() for thread in threads)
        assert sum(outcome == "success" for _, outcome in outcomes) == gate_limit
        assert sum(outcome == "capacity" for _, outcome in outcomes) == 30 - gate_limit
        assert not [outcome for _, outcome in outcomes if outcome.startswith("error:")]
        assert provider_calls == gate_limit
        with lease_lock:
            assert lease_state["active"] == 0
            assert lease_state["peak"] == gate_limit

        followup = asyncio.run(
            verification.execute_competitor_verification_request(
                quote_id=quote_ids[30],
                request_id="req-capacity-followup",
                user={"user_id": 1, "is_admin": True},
                user_id=1,
                initial_access_check=lambda: None,
                connection_factory=connection_factory,
                provider_pool=SourcePool("metaso-capacity-followup", ["key-one"], qpm_per_source=10),
                admission_gate=gate,
            )
        )
        assert followup["newly_verified"] == 1
        assert provider_calls == gate_limit + 1
        with lease_lock:
            assert lease_state["active"] == 0
    finally:
        provider_release.set()
        for thread in threads:
            thread.join(timeout=5)
        pool.closeall()


def test_provider_exception_releases_session_lock_connection_and_capacity(
    monkeypatch, verification_schema
):
    monkeypatch.setattr(verification, "llm_track", _fake_track)
    quote_id = 920001
    _seed_many_quotes(verification_schema, [quote_id])
    gate = verification.VerificationAdmissionGate(1)
    application_name = f"writing_verify_error_{uuid.uuid4().hex[:8]}"
    pool = _local_pool(verification_schema, application_name=application_name)
    lease_state = {"active": 0, "peak": 0}
    lease_lock = threading.Lock()

    class _FailedResponse:
        status_code = 503

        @staticmethod
        def json():
            return {}

    class _FailingClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            return _FailedResponse()

    monkeypatch.setattr(verification.httpx, "AsyncClient", _FailingClient)

    def connection_factory():
        return _PoolLease(pool.getconn(), pool, lease_state, lease_lock)

    try:
        with pytest.raises(verification.CompetitorVerificationUnavailable):
            asyncio.run(
                verification.execute_competitor_verification_request(
                    quote_id=quote_id,
                    request_id="req-provider-error",
                    user={"user_id": 1, "is_admin": True},
                    user_id=1,
                    initial_access_check=lambda: None,
                    connection_factory=connection_factory,
                    provider_pool=SourcePool("metaso-error", ["key-one"], qpm_per_source=10),
                    admission_gate=gate,
                )
            )

        with lease_lock:
            assert lease_state["active"] == 0

        probe = _connect(schema=verification_schema)
        probe.autocommit = True
        try:
            assert verification.try_acquire_quote_verification_lock(probe, quote_id) is True
            verification.release_quote_verification_lock(probe, quote_id)
        finally:
            probe.close()

        class _RecoveredClient:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def post(self, _url, **_kwargs):
                return _Response()

        monkeypatch.setattr(verification.httpx, "AsyncClient", _RecoveredClient)
        recovered = asyncio.run(
            verification.execute_competitor_verification_request(
                quote_id=quote_id,
                request_id="req-provider-recovered",
                user={"user_id": 1, "is_admin": True},
                user_id=1,
                initial_access_check=lambda: None,
                connection_factory=connection_factory,
                provider_pool=SourcePool("metaso-recovered", ["key-two"], qpm_per_source=10),
                admission_gate=gate,
            )
        )
        assert recovered["newly_verified"] == 1
        with lease_lock:
            assert lease_state["active"] == 0
    finally:
        pool.closeall()


def test_twenty_same_quote_requests_use_one_provider_call_through_orchestrator(
    monkeypatch, verification_schema
):
    monkeypatch.setattr(verification, "llm_track", _fake_track)
    quote_id = 930001
    _seed_many_quotes(verification_schema, [quote_id])
    gate = verification.VerificationAdmissionGate(16)
    pool = _local_pool(
        verification_schema,
        application_name=f"writing_verify_singleflight_{uuid.uuid4().hex[:8]}",
    )
    lease_state = {"active": 0, "peak": 0}
    lease_lock = threading.Lock()
    provider_entered = threading.Event()
    provider_release = threading.Event()
    provider_calls = 0
    provider_lock = threading.Lock()

    class _SingleflightClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, _url, **_kwargs):
            nonlocal provider_calls
            with provider_lock:
                provider_calls += 1
                provider_entered.set()
            released = await asyncio.to_thread(provider_release.wait, 15)
            assert released
            return _Response()

    monkeypatch.setattr(verification.httpx, "AsyncClient", _SingleflightClient)

    def connection_factory():
        return _PoolLease(pool.getconn(), pool, lease_state, lease_lock)

    start = threading.Barrier(20)
    secondary_finished = threading.Event()
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def worker(index: int):
        try:
            start.wait(timeout=10)
            asyncio.run(
                verification.execute_competitor_verification_request(
                    quote_id=quote_id,
                    request_id=f"req-singleflight-{index}",
                    user={"user_id": 1, "is_admin": True},
                    user_id=1,
                    initial_access_check=lambda: None,
                    connection_factory=connection_factory,
                    provider_pool=SourcePool(
                        f"metaso-singleflight-{index}", ["key-one"], qpm_per_source=1000
                    ),
                    admission_gate=gate,
                )
            )
            outcome = "success"
        except verification.CompetitorVerificationInProgress:
            outcome = "in_progress"
        except verification.CompetitorVerificationCapacityExceeded:
            outcome = "capacity"
        except BaseException as exc:
            outcome = f"error:{type(exc).__name__}:{exc}"
        with outcomes_lock:
            outcomes.append(outcome)
            if len(outcomes) == 19:
                secondary_finished.set()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    try:
        for thread in threads:
            thread.start()
        assert provider_entered.wait(timeout=15)
        assert secondary_finished.wait(timeout=15)
        assert provider_calls == 1
        provider_release.set()
        for thread in threads:
            thread.join(timeout=20)
        assert not any(thread.is_alive() for thread in threads)
        assert outcomes.count("success") == 1
        assert outcomes.count("in_progress") + outcomes.count("capacity") == 19
        assert not [outcome for outcome in outcomes if outcome.startswith("error:")]
        assert provider_calls == 1
        with lease_lock:
            assert lease_state["active"] == 0
            assert lease_state["peak"] <= gate.limit
    finally:
        provider_release.set()
        for thread in threads:
            thread.join(timeout=5)
        pool.closeall()
