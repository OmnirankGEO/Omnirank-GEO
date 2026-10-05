from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg2
import pytest
from fastapi import FastAPI, HTTPException, Request as FastAPIRequest
from fastapi.testclient import TestClient
from starlette.requests import Request

from api.admin_cross_tenant_governance_api import demo_router
from db.refund_work_order_db import _lock_active_refund_provider
from auth.brand_access import get_user_brand_filter, require_brand_access
from auth.module_mapping import resolve_permission
from services.admin_cross_tenant_governance import (
    CrossTenantConflict,
    CrossTenantNotFound,
    change_account_status,
    confirm_provider_downgrade_plan,
    create_demo_case_grant,
    create_demo_case_grants_batch,
    create_provider_downgrade_plan,
    get_authorized_demo_case,
    has_authorized_demo_case,
    list_authorized_demo_cases,
    list_demo_case_catalog,
    provider_downgrade_snapshot,
    record_admin_read,
    revoke_demo_case_grant,
    revoke_demo_case_grants_batch,
)
from services.admin_cross_tenant_schema import verify_admin_cross_tenant_schema
from services.demo_access import (
    DemoAccessContext,
    DemoSafeResponseMiddleware,
    DemoSideEffectBlocked,
    assert_side_effects_allowed,
    demo_portal_entry,
    is_demo_portal_entry,
    project_safe_snapshot,
    resolve_demo_access,
    resolve_demo_portal_entry,
    resolve_demo_route_contract,
    snapshot_transport_payload,
    stable_demo_case_id,
)
from services.portal_token_authority import require_portal_token_authority


DB_URL = os.environ["TEST_DATABASE_URL"]
ROOT = Path(__file__).resolve().parents[2]


def sql_one(sql: str, params=()):
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def demo_request(method: str, path: str, request_id: str, *, user_id: int = 132, client_brand_ids=None) -> Request:
    scope = {
        "type": "http", "http_version": "1.1", "method": method, "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "headers": [(b"x-request-id", request_id.encode())],
        "client": ("127.0.0.77", 12345), "server": ("testserver", 80),
    }
    request = Request(scope)
    request.state.user = {
        "user_id": user_id, "id": user_id, "username": f"user_{user_id}",
        "is_admin": False, "client_brand_ids": client_brand_ids or [],
    }
    return request


def grant_zhejiang_dailin(*, request_id: str = "dailin-grant-132") -> dict:
    case_id = stable_demo_case_id(601, 9601)
    return create_demo_case_grant(
        grantee_kind="user", grantee_user_id=132, grantee_organization_id=None,
        brand_id=601, diagnosis_id=9601, case_id=case_id,
        capability="demo.customer.preview",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        note="Owner 明日产品演示", reason="Owner 批准浙江岱林演示",
        actor_user_id=1, actor_username="admin_one", request_id=request_id,
        ip_address="127.0.0.9",
    )["grant"]


def test_schema_contract_and_account_status_are_cas_audited():
    assert resolve_permission("/api/demo-cases") is None
    assert resolve_permission("/api/demo-cases/56cc6f1e-bd50-584f-8702-2e044d20bce5") is None
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        verify_admin_cross_tenant_schema(cur)
    result = change_account_status(
        124, active=False, expected_version=1, reason="风控核验后暂停账号",
        actor_user_id=1, actor_username="admin_one", request_id="status-disable-124",
        ip_address="127.0.0.8",
    )
    assert result["after"] == {"active": False}
    assert result["version"] == 2
    assert sql_one(
        """SELECT actor_user_id,reason,ip_address,before_snapshot->>'active',
                  after_snapshot->>'active',request_id
           FROM admin_cross_tenant_audits WHERE request_id='status-disable-124'"""
    ) == (1, "风控核验后暂停账号", "127.0.0.8", "true", "false", "status-disable-124")


def test_last_admin_census_is_serialized_across_two_connections():
    barrier = threading.Barrier(2)

    def disable(target: int):
        barrier.wait(timeout=5)
        try:
            return change_account_status(
                target, active=False, expected_version=1, reason="并发停用管理员",
                actor_user_id=124, actor_username="governance_actor",
                request_id=f"disable-admin-{target}", ip_address="127.0.0.20",
            )
        except CrossTenantConflict as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(disable, (1, 2)))
    assert sum(isinstance(item, dict) for item in results) == 1
    assert results.count("LAST_ACTIVE_ADMIN") == 1
    assert sql_one(
        """SELECT COUNT(*) FROM users u WHERE COALESCE(u.is_active,0)<>0 AND EXISTS(
             SELECT 1 FROM user_roles ur JOIN roles r ON r.id=ur.role_id
             WHERE ur.user_id=u.id AND r.name='admin')"""
    ) == (1,)


def test_audit_request_id_collision_rolls_back_business_mutation():
    record_admin_read(
        actor_user_id=1, actor_username="admin_one", action="user.read",
        subject_kind="user", subject_id=124, request_id="audit-collision-124",
        reason="先记录只读审计", before={}, after={"read": True}, ip_address="127.0.0.21",
    )
    with pytest.raises(CrossTenantConflict) as collision:
        change_account_status(
            124, active=False, expected_version=1, reason="碰撞请求不得提交业务",
            actor_user_id=1, actor_username="admin_one", request_id="audit-collision-124",
            ip_address="127.0.0.21",
        )
    assert collision.value.code == "AUDIT_REQUEST_ID_COLLISION"
    assert sql_one("SELECT is_active,permission_version FROM users WHERE id=124") == (1, 1)
    assert sql_one(
        "SELECT action FROM admin_cross_tenant_audits WHERE request_id='audit-collision-124'"
    ) == ("user.read",)


def test_schema_contract_rejects_weakened_demo_event_idempotency_key():
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        try:
            cur.execute(
                "ALTER TABLE admin_demo_access_events "
                "DROP CONSTRAINT admin_demo_access_events_idempotency_key"
            )
            cur.execute(
                "ALTER TABLE admin_demo_access_events ADD CONSTRAINT "
                "admin_demo_access_events_idempotency_key "
                "UNIQUE(grant_id,viewer_user_id,request_id)"
            )
            with pytest.raises(RuntimeError, match="columns .*action"):
                verify_admin_cross_tenant_schema(cur)
        finally:
            cur.execute(
                "ALTER TABLE admin_demo_access_events "
                "DROP CONSTRAINT IF EXISTS admin_demo_access_events_idempotency_key"
            )
            cur.execute(
                "ALTER TABLE admin_demo_access_events ADD CONSTRAINT "
                "admin_demo_access_events_idempotency_key "
                "UNIQUE(grant_id,viewer_user_id,request_id,action)"
            )


def test_migration_repairs_dropped_subject_version_primary_key():
    migration = (ROOT / "scripts/migration_admin_cross_tenant_governance_2026_07_21.sql").read_text(encoding="utf-8")
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE admin_governance_subject_versions DROP CONSTRAINT admin_governance_subject_versions_pkey")
        cur.execute(migration)
        cur.execute(migration)
        verify_admin_cross_tenant_schema(cur)


@pytest.mark.parametrize(
    "tamper,expected",
    [
        (
            """ALTER TABLE admin_cross_tenant_audits DROP CONSTRAINT admin_cross_tenant_audits_reason_check;
               ALTER TABLE admin_cross_tenant_audits ADD CONSTRAINT admin_cross_tenant_audits_reason_check CHECK(TRUE)""",
            "literal contract|check columns|tautological|expression contract",
        ),
        (
            """DROP INDEX idx_admin_cross_tenant_audits_actor;
               CREATE INDEX idx_admin_cross_tenant_audits_actor ON users(id)""",
            "wrong table/schema",
        ),
        (
            """ALTER TABLE admin_demo_access_events DROP CONSTRAINT admin_demo_access_events_grant_id_fkey;
               ALTER TABLE admin_demo_access_events ADD CONSTRAINT admin_demo_access_events_grant_id_fkey
                 FOREIGN KEY(grant_id) REFERENCES admin_demo_case_grants(id) NOT VALID""",
            "not validated",
        ),
        (
            """ALTER TABLE admin_governance_subject_versions RENAME TO admin_governance_subject_versions_real;
               CREATE VIEW admin_governance_subject_versions AS SELECT * FROM admin_governance_subject_versions_real""",
            "relation kind",
        ),
    ],
)
def test_exact_schema_contract_rejects_catalog_decoys(tamper: str, expected: str):
    conn = psycopg2.connect(DB_URL)
    try:
        cur = conn.cursor()
        cur.execute(tamper)
        with pytest.raises(RuntimeError, match=expected):
            verify_admin_cross_tenant_schema(cur)
    finally:
        conn.rollback()
        conn.close()


def test_dailin_user_132_fixture_is_frozen_redacted_and_independent():
    before_owner = sql_one("SELECT owner_user_id FROM brands WHERE id=601")
    before_commercial = sql_one("SELECT user_id FROM user_clients WHERE brand_id=601 ORDER BY user_id")
    before_wallet = sql_one("SELECT paid_points,bonus_points FROM user_wallets WHERE user_id=132")
    before_binding = sql_one("SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=124")
    catalog = list_demo_case_catalog(page=1, page_size=30, search="浙江岱林")
    assert catalog["total"] == 2
    by_brand = {item["brand_id"]: item for item in catalog["cases"]}
    assert by_brand[601]["case_id"] != by_brand[602]["case_id"]
    assert by_brand[601]["owner_user_id"] == 124 and by_brand[602]["owner_user_id"] == 129

    grant = grant_zhejiang_dailin()
    case_id = grant["case_id"]
    assert grant["brand_id"] == 601 and grant["capability"] == "demo.customer.preview"
    assert has_authorized_demo_case(132, case_id) is True
    assert has_authorized_demo_case(132, stable_demo_case_id(602, 9602)) is False
    assert resolve_demo_access(132, 601) is not None
    assert resolve_demo_access(132, 602) is None

    listed = list_authorized_demo_cases(
        132, page=1, page_size=30, search="生命科学",
        request_id="dailin-list-132", ip_address="127.0.0.10",
    )
    assert listed["total"] == 1 and listed["cases"][0]["case_id"] == case_id
    detail = get_authorized_demo_case(
        132, case_id, action="demo_case.detail",
        request_id="dailin-detail-132", ip_address="127.0.0.10",
    )
    assert detail["customer_overview"]["brand_name"] == "浙江岱林"
    assert detail["diagnosis_snapshot"]["total_score"] == 88
    assert detail["quote_snapshots"][0]["status"] == "confirmed"
    assert detail["writing_snapshots"]["articles"][0]["title"] == "浙江岱林行业洞察"
    assert detail["monitoring_snapshots"][0]["status"] == "completed"
    assert detail["report_snapshots"][0]["overall_score"] == 86
    serialized = json.dumps(detail, ensure_ascii=False, default=str).lower()
    for forbidden in ("never-expose-token", "provider-secret", "upstream-secret", "monitor-secret", "internal_cost", "profit"):
        assert forbidden not in serialized

    # Frozen snapshot does not drift when the commercial record changes later.
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE diagnosis_records SET total_score=1 WHERE id=9601")
    assert get_authorized_demo_case(
        132, case_id, action="demo_case.export",
        request_id="dailin-watermarked-export-132", ip_address="127.0.0.10",
    )["diagnosis_snapshot"]["total_score"] == 88

    assert sql_one("SELECT owner_user_id FROM brands WHERE id=601") == before_owner
    assert sql_one("SELECT user_id FROM user_clients WHERE brand_id=601 ORDER BY user_id") == before_commercial
    assert sql_one("SELECT paid_points,bonus_points FROM user_wallets WHERE user_id=132") == before_wallet
    assert sql_one("SELECT agent_user_id FROM customer_agent_bindings WHERE customer_user_id=124") == before_binding
    assert sql_one("SELECT COUNT(*) FROM user_clients WHERE user_id=132 AND brand_id=601") == (0,)
    # Demo reads are audited through structured logs and never mutate PostgreSQL.
    assert sql_one(
        "SELECT COUNT(*) FROM admin_demo_access_events WHERE grant_id=%s AND viewer_user_id=132",
        (grant["id"],),
    ) == (0,)


def test_demo_direct_api_side_effects_fail_before_write_provider_funds_or_jobs():
    grant_zhejiang_dailin()
    before = {
        "wallet": sql_one("SELECT paid_points,bonus_points FROM user_wallets WHERE user_id=132"),
        "tasks": sql_one("SELECT COUNT(*) FROM monitoring_tasks WHERE brand_id=601"),
        "articles": sql_one("SELECT COUNT(*) FROM articles WHERE brand_id=601"),
        "quotes": sql_one("SELECT COUNT(*) FROM quotes WHERE brand_id=601"),
        "publishes": sql_one("SELECT COUNT(*) FROM media_publications WHERE brand_id=601"),
    }
    attempts = (
        ("POST", "/api/monitoring/keyword/9831/disable", "demo-block-monitor"),
        ("POST", "/api/writing/generate-titles", "demo-block-article"),
        ("POST", "/api/quotes/9701/confirm", "demo-block-quote"),
        ("POST", "/api/meijiehezi/publish", "demo-block-publish"),
        ("GET", "/api/portal/tokens/by-brand/601", "demo-block-token"),
        ("GET", "/api/m3/export/monitoring.xlsx", "demo-block-raw-export"),
    )
    for method, path, request_id in attempts:
        with pytest.raises(HTTPException) as captured:
            require_brand_access(demo_request(method, path, request_id), 601)
        assert captured.value.status_code == 404
    # Demo is not ordinary query authority, even for an otherwise valid grant.
    assert 601 not in get_user_brand_filter(demo_request("GET", "/api/client-context/list", "filter-no-demo"))
    for boundary in ("database.write", "provider.call", "billing.freeze", "job.enqueue"):
        with pytest.raises(DemoSideEffectBlocked):
            assert_side_effects_allowed("demo", boundary)
    after = {
        "wallet": sql_one("SELECT paid_points,bonus_points FROM user_wallets WHERE user_id=132"),
        "tasks": sql_one("SELECT COUNT(*) FROM monitoring_tasks WHERE brand_id=601"),
        "articles": sql_one("SELECT COUNT(*) FROM articles WHERE brand_id=601"),
        "quotes": sql_one("SELECT COUNT(*) FROM quotes WHERE brand_id=601"),
        "publishes": sql_one("SELECT COUNT(*) FROM media_publications WHERE brand_id=601"),
    }
    assert after == before
    assert sql_one("SELECT COUNT(*) FROM admin_demo_access_events WHERE viewer_user_id=132 AND blocked_reason='DEMO_SIDE_EFFECT_BOUNDARY'") == (0,)


def test_demo_transport_exposes_only_frozen_existing_portal_link(monkeypatch):
    """✅ [2026-08-20 · 已定案 · Master SSOT v2.6 修订 §8.2] 本条期望不再变动。

    Owner 裁决:**原则① 全面生效**(停用的门户 token 必须立刻停止解析,无缓冲期),
    而它在现状下**已经成立** —— 解析每次实时查该 quote 的 token 活性,无缓存无 TTL。
    承重的是那条不变式「一个 quote 同时最多一张 active token」,由同文件的
    `test_one_active_portal_token_per_quote_is_the_load_bearing_invariant` 单独立锁。

    所以本条打的是**不变式**(该 quote 的 active token 全部停用 ⇒ 句柄当场失效)+
    正向对照,不打实现细节;T2(2026-07-29)的「多张并存时停掉其中一张不影响解析」
    作为现役语义一并锁住,免得有人悄悄改回去。
    依据:`docs/DECISION/OMNIRANK_BUSINESS_GOVERNANCE_MASTER_SSOT_AND_CLAUDE_EXECUTION.md` §8.2 ·
    过程见 `docs/AI-CONTEXT/TRIAGE_DEMO_ISOLATION_2RED_2026-08-20.md`。
    """
    monkeypatch.setenv("DEMO_PORTAL_ENTRY_SECRET", "test-only-demo-portal-entry-secret-0123456789")
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """INSERT INTO client_access_tokens(quote_id,brand_name,token,is_active,expires_at)
               VALUES (9701,'浙江岱林','DAILINPORTALOLDER',1,'2026-12-31 00:00:00+00')"""
        )
        cur.execute(
            """INSERT INTO quotes(id,brand_id,brand_name,status)
               VALUES (9702,601,'浙江岱林','confirmed')"""
        )
        cur.execute(
            """INSERT INTO client_access_tokens(quote_id,brand_name,token,is_active,expires_at)
               VALUES (9702,'浙江岱林','DAILINPORTALB2026',1,'2027-06-30 00:00:00+00')"""
        )
    grant_zhejiang_dailin(request_id="demo-transport-grant")
    context = resolve_demo_access(132, 601)
    assert context is not None
    raw_snapshot = sql_one(
        "SELECT safe_snapshot::text FROM admin_demo_cases WHERE case_id=%s",
        (context.case_id,),
    )[0]
    assert "DAILINPORTAL2026" not in raw_snapshot
    assert "DAILINPORTALOLDER" not in raw_snapshot
    assert "DAILINPORTALB2026" not in raw_snapshot
    assert "token_fingerprint" in raw_snapshot

    status, clients = snapshot_transport_payload(
        context, method="GET", path="/api/monitoring/clients", query={},
    )
    assert status == 200 and clients["status"] == "success"
    assert {row["quote_id"] for row in clients["clients"]} == {9701, 9702}
    assert "surface" not in clients
    status, keywords = snapshot_transport_payload(
        context, method="GET", path="/api/monitoring/clients/9701/keywords", query={},
    )
    assert status == 200 and keywords["snapshot_missing"] is False
    assert keywords["count"] == 0

    legacy_context = DemoAccessContext(
        access_mode="demo", viewer_user_id=132, grant_id=999, brand_id=601,
        case_id="legacy-case", diagnosis_id=9601, expires_at=context.expires_at,
        snapshot={
            "snapshot_contract": "demo-customer-safe-v3",
            "customer_overview": {"brand_id": 601, "brand_name": "浙江岱林"},
            "quote_snapshots": [{"id": 9701, "brand_id": 601}],
        },
    )
    status, missing_keywords = snapshot_transport_payload(
        legacy_context, method="GET", path="/api/monitoring/clients/9701/keywords", query={},
    )
    assert status == 200 and missing_keywords["snapshot_missing"] is True
    assert missing_keywords["snapshot_message"] == "快照未包含" and "count" not in missing_keywords
    status, missing_diagnosis = snapshot_transport_payload(
        legacy_context, method="GET", path="/api/diagnosis/9601", query={},
    )
    assert status == 200 and missing_diagnosis["snapshot_missing"] is True
    assert missing_diagnosis["snapshot_message"] == "快照未包含"
    assert "total" not in missing_diagnosis

    entry = demo_portal_entry(context, quote_id=9701)
    entry_b = demo_portal_entry(context, quote_id=9702)
    assert entry and entry_b and entry != entry_b
    assert entry.startswith("D") and "never-expose-token" not in entry.lower()
    assert resolve_demo_portal_entry(entry).context.case_id == context.case_id
    assert resolve_demo_portal_entry(entry).quote_id == 9701
    assert resolve_demo_portal_entry(entry_b).quote_id == 9702
    status, token = snapshot_transport_payload(
        context, method="GET", path="/api/portal/tokens/9701", query={},
    )
    assert status == 200 and token["token"]["token"] == entry
    assert token["token"]["demo_transport_entry"] == entry
    # 🔴 [WO-D-R2 ② 2026-08-20] 原为 `json.dumps(token)` —— 裸 dumps。
    #   `client_access_tokens.expires_at` 生产是 DATE,payload 里就是个 `date` 对象,
    #   裸 dumps 直接 `TypeError: Object of type date is not JSON serializable`。
    #   这是**测试侧**的问题不是生产侧:生产走 FastAPI 的 JSON 编码器,date 会变字符串。
    #   加 default=str 后这条泄漏检查反而更严(日期也被展平成字符串一并扫)。
    serialized_token = json.dumps(token, default=str).lower()
    assert "dailinportal2026" not in serialized_token
    assert "dailinportalolder" not in serialized_token
    assert "dailinportalb2026" not in serialized_token
    assert "never-expose-token" not in serialized_token
    status, token_b = snapshot_transport_payload(
        context, method="GET", path="/api/portal/tokens/9702", query={},
    )
    assert status == 200 and token_b["token"]["token"] == entry_b

    # ------------------------------------------------------------------
    # 🔴 [triage 2026-08-20 · 判据期望订正] 本段原来打在 `_portal_link_is_live` 上:
    #   `monkeypatch.setattr(demo_access_module, "_portal_link_is_live", …)`
    #   + `assert live_link_checks == 1`,并在只停**一张** token 之后要求解析返回 None。
    #   两条都是 **T2 之前**的语义,现役生产已经不是那样了:
    #
    #   · `services/demo_access.py:886 resolve_demo_portal_entry` → `:878 resolve_portal_link`
    #     → `:851 live_portal_link` → `:821 _current_portal_token_row`(实时取该 quote
    #     **当前** active token);`_portal_link_is_live`(`:971`)如今只在
    #     `live_snapshot_portal_link`(`:1021`)这条**回落**路径上用。
    #     所以主路命中时它的调用数天然是 0 —— 判据数的是一个已经不在主路上的私有函数。
    #   · `:824-828` 与 `tests/portal_notify_2026_07_29/test_t2_demo_portal_live.py::
    #     test_lockA_portal_entry_resolves_even_without_frozen_portal_links` 写明:
    #     旧口径「句柄绑定那一版 token 的指纹」导致 token 一轮换演示门户就永久打不开
    #     (Owner 报的"客户门户卡片点不开"),T2 才刻意改成按 quote 取当前 active token。
    #     于是「同一 quote 还有别的 active token 时,停用其中一张仍能解析」是**预期**,
    #     不是缺口 —— 客户那边本来也还开着。
    #
    #   安全不变式没有放宽,而且实测成立:该 quote **没有任何** active token 时,
    #   句柄解析返回 None(下面 P1 那段)。判据改成打这条不变式,不打实现细节。
    # ------------------------------------------------------------------
    import services.demo_access as demo_access_module
    live_link_checks = 0
    original_live_check = demo_access_module.resolve_portal_link

    def count_live_check(*args, **kwargs):
        nonlocal live_link_checks
        live_link_checks += 1
        return original_live_check(*args, **kwargs)

    # 打**收口点** `resolve_portal_link`:实时口径与快照回落口径都从它下去,
    # 换实现不换收口点时判据不该跟着红,而「压根不查库就放行」必须红。
    monkeypatch.setattr(demo_access_module, "resolve_portal_link", count_live_check)
    assert resolve_demo_portal_entry("D" + "A" * 32) is None
    assert live_link_checks == 0, "HMAC 都没对上就去查库了"
    assert resolve_demo_portal_entry(entry).quote_id == 9701
    assert live_link_checks == 1, "解析成功却没做过任何门户活性复核"

    # T2 语义(显式锁住,免得有人悄悄改回去):轮换/多张 token 并存时,
    # 停掉其中一张不影响解析 —— 该 quote 的客户门户仍然是开着的。
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE client_access_tokens SET is_active=0 WHERE token='DAILINPORTAL2026'")
    assert resolve_demo_portal_entry(entry).quote_id == 9701
    status, rotated = snapshot_transport_payload(
        context, method="GET", path="/api/portal/tokens/9701", query={},
    )
    assert status == 200 and rotated["token"]["token"] == entry

    # 🔴 安全不变式:该 quote 的 active token **全部**停用(= 客户门户真被收回)
    #    ⇒ 演示句柄当场失效。这是「停用 token 必须停止解析」在现役语义下的准确形态。
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE client_access_tokens SET is_active=0 WHERE quote_id=9701")
    assert resolve_demo_portal_entry(entry) is None
    # 正向对照:9702 的 token 没动,它必须仍然解析得出来 ——
    # 否则上面那个 None 可能只是「解析整个坏掉了」。
    assert resolve_demo_portal_entry(entry_b).quote_id == 9702
    status, revoked = snapshot_transport_payload(
        context, method="GET", path="/api/portal/tokens/9701", query={},
    )
    assert status == 200 and revoked["token"] is None
    status, still_live_b = snapshot_transport_payload(
        context, method="GET", path="/api/portal/tokens/9702", query={},
    )
    assert status == 200 and still_live_b["token"]["token"] == entry_b


def test_one_active_portal_token_per_quote_is_the_load_bearing_invariant():
    """🔴 [2026-08-20 · Owner 原则①] 「停用的门户 token 必须立刻停止解析,无缓冲期」
    在生产上成立,**靠的是这条不变式**:一个 quote 同时最多一张 active token。

    上一条判据之所以红,是因为它的夹具用裸 INSERT 造了**两张同时 active** 的 token
    —— 而生产没有任何路径会产生那个状态:

      · 全仓**只有一处** `INSERT INTO client_access_tokens`
        (`db/monitoring_db.py:7449`),而它前面紧跟着同一事务里的
        `UPDATE … SET is_active=0 WHERE quote_id=%s AND is_active=1`(`:7437-7441`);
      · 其余写只有 `expires_at` 续期(`:7718` / `server.py:18124`)、
        `last_access_at`(`:7783`)、过期扫描置 0(`:8127`);
      · **没有任何一处**把 `is_active` 写回 1 —— 停用是单向的。

    所以「停用那张 token」= 该 quote 再无 active token = 演示句柄当场解析不出来
    (上一条尾部那段实测)。这条不变式一旦破(有人新增第二个 writer 而不失效旧的),
    原则① 就在无人察觉的情况下失守 —— 故单独立锁,按**结构**取全集,不数「我记得几处」。
    """
    import ast

    root = Path(__file__).resolve().parents[2]
    inserts: list[tuple[str, str]] = []
    reactivations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        parts = set(path.parts)
        if parts & {"tests", "node_modules", ".git", "_archive", "__pycache__"}:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "client_access_tokens" not in source:
            continue
        lowered = source.lower()
        if "insert into client_access_tokens" in lowered:
            inserts.append((str(path.relative_to(root)).replace("\\", "/"), source))
        for marker in ("set is_active = 1", "set is_active=1"):
            if marker in lowered:
                reactivations.append(str(path.relative_to(root)).replace("\\", "/"))

    assert inserts, "一处 INSERT 都没扫到 —— 分母塌了,不是没有 writer"
    assert [name for name, _ in inserts] == ["db/monitoring_db.py"], [n for n, _ in inserts]
    assert not reactivations, ("有路径把门户 token 重新激活,停用不再是单向的", reactivations)

    # 那唯一一处 INSERT 必须与「失效旧 token」在**同一个函数体**里。
    source = inserts[0][1]
    holder = None
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = ast.unparse(node).lower()
        if "insert into client_access_tokens" in body:
            holder = (node.name, body)
    assert holder, "找不到承载 INSERT 的函数"
    name, body = holder
    assert "update\n            client_access_tokens" in body or "update client_access_tokens" in body, name
    assert "is_active = 0" in body, (name, "签发新 token 之前没有失效旧 token")


def test_portal_token_authority_audits_admin_ignores_demo_and_accepts_owner(monkeypatch):
    grant_zhejiang_dailin(request_id="portal-authority-demo-grant")
    token_writes = 0
    governance_events = []

    import services.admin_cross_tenant_governance as cross_tenant_governance
    monkeypatch.setattr(
        cross_tenant_governance,
        "record_admin_read",
        lambda **event: governance_events.append(event),
    )

    def regenerate(request):
        nonlocal token_writes
        authority = require_portal_token_authority(
            request,
            quote_id=9701,
            action="portal.token.generate",
        )
        token_writes += 1
        return authority

    owner_request = demo_request("POST", "/api/portal/tokens", "portal-owner", user_id=124)
    assert regenerate(owner_request)["brand_id"] == 601
    assert token_writes == 1

    demo_request_only = demo_request("POST", "/api/portal/tokens", "portal-demo-only", user_id=132)
    with pytest.raises(HTTPException) as denied:
        regenerate(demo_request_only)
    assert denied.value.status_code == 403
    assert denied.value.detail["code"] == "PORTAL_TOKEN_AUTHORITY_REQUIRED"
    assert token_writes == 1

    admin_only = demo_request("POST", "/api/portal/tokens", "portal-admin-only", user_id=1)
    admin_only.state.user["is_admin"] = True
    assert regenerate(admin_only)["brand_id"] == 601
    assert token_writes == 2
    assert len(governance_events) == 1
    assert governance_events[0]["action"] == "portal.token.generate"
    assert governance_events[0]["subject_kind"] == "customer_portal_credential"
    assert governance_events[0]["subject_id"] == 9701
    assert governance_events[0]["before"]["brand_id"] == 601
    assert governance_events[0]["after"]["authority"] == "platform_admin_governance"

    legacy_assigned = demo_request(
        "POST", "/api/portal/tokens", "portal-legacy-assigned",
        user_id=132, client_brand_ids=[601],
    )
    assert regenerate(legacy_assigned)["brand_id"] == 601
    assert token_writes == 3

    same_name_wrong_owner = demo_request(
        "POST", "/api/portal/tokens", "portal-same-name-wrong-brand", user_id=129,
    )
    with pytest.raises(HTTPException):
        regenerate(same_name_wrong_owner)
    assert token_writes == 3

    class AssignedOperator:
        is_member = True
        organization_status = "active"
        membership_status = "active"
        principal_user_id = 124
        capabilities = frozenset({"reports.share_external"})

    import db.organization_db as organization_db
    operator_request = demo_request("POST", "/api/portal/tokens", "portal-operator", user_id=132)
    operator_request.state.organization_identity = AssignedOperator()
    monkeypatch.setattr(organization_db, "assigned_brand_ids", lambda identity, cursor=None: [601])
    assert regenerate(operator_request)["brand_id"] == 601
    assert token_writes == 4

    monkeypatch.setattr(organization_db, "assigned_brand_ids", lambda identity, cursor=None: [])
    with pytest.raises(HTTPException) as revoked_operator:
        regenerate(operator_request)
    assert revoked_operator.value.detail["code"] == "PORTAL_TOKEN_ASSIGNMENT_REQUIRED"
    assert token_writes == 4


def test_real_commercial_access_wins_and_demo_never_downgrades_it():
    grant_zhejiang_dailin()
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO user_clients(user_id,brand_id) VALUES (132,601)")
    request = demo_request("POST", "/api/writing/generate-titles", "real-wins", client_brand_ids=[601])
    require_brand_access(request, 601)
    assert getattr(request.state, "demo_access_context", None) is None
    filtered = get_user_brand_filter(request)
    assert 601 in filtered and not getattr(request.state, "demo_brand_ids", set())


def test_stale_demo_headers_do_not_downgrade_real_commercial_middleware_access():
    grant = grant_zhejiang_dailin(request_id="commercial-real-wins-grant")
    app = FastAPI()
    calls = {"live": 0}

    @app.post("/api/writing/generate-titles")
    async def live_handler():
        calls["live"] += 1
        return {"success": True, "access_mode": "real"}

    app.add_middleware(DemoSafeResponseMiddleware)

    @app.middleware("http")
    async def fake_auth(request: FastAPIRequest, call_next):
        request.state.user = {
            "user_id": 132, "id": 132, "username": "commercial_132",
            "is_admin": False, "client_brand_ids": [601],
        }
        return await call_next(request)

    response = TestClient(app).post(
        "/api/writing/generate-titles",
        headers={
            "X-Demo-Brand-ID": "601", "X-Demo-Case-ID": grant["case_id"],
            "X-Request-ID": "commercial-real-wins-request",
        },
    )
    assert response.status_code == 200
    assert response.json()["access_mode"] == "real" and calls["live"] == 1


def test_real_organization_assignment_wins_before_organization_guard_middleware(monkeypatch):
    grant = grant_zhejiang_dailin(request_id="org-explicit-demo-grant")
    import db.organization_db as organization_db

    identity = object()
    monkeypatch.setattr(organization_db, "resolve_identity", lambda user_id, request_id: identity)
    monkeypatch.setattr(organization_db, "assigned_brand_ids", lambda value: [601] if value is identity else [])
    app = FastAPI()
    calls = {"live": 0}

    @app.post("/api/quotes/9701/confirm")
    async def live_quote_handler():
        calls["live"] += 1
        return {"success": True, "access_mode": "real"}

    app.add_middleware(DemoSafeResponseMiddleware)

    @app.middleware("http")
    async def fake_auth(request: FastAPIRequest, call_next):
        request.state.user = {
            "user_id": 132, "id": 132, "username": "org_member_132",
            "is_admin": False, "client_brand_ids": [],
        }
        return await call_next(request)

    response = TestClient(app).post(
        "/api/quotes/9701/confirm",
        headers={
            "X-Demo-Brand-ID": "601", "X-Demo-Case-ID": grant["case_id"],
            "X-Request-ID": "org-real-wins-request",
        },
    )
    assert response.status_code == 200
    assert response.json()["access_mode"] == "real"
    assert calls["live"] == 1


def test_demo_middleware_uses_only_snapshot_and_blocks_indirect_side_effect_routes(monkeypatch):
    """✅ [2026-08-20 · 已定案 · Master SSOT v2.6 修订 §8.2] 本条期望不再变动。

    Owner 裁决:**原则②「只从冻结快照出数据」作用域 = 凭证面**
    (门户 token / 导出下载 / 分享与公开链接类 GET);**业务只读面继续按 v2.0 D10**
    走受控实时只读投影 + 出站脱敏。理由:业务面全域切冻结快照会当场违反 §8.1
    「用长期陈旧且缺字段的快照假装实时数据」「因快照不全返回空页面」两条禁令。

    所以 `/api/clients/**`(`customer_overview.read` · disposition=`live` ·
    `services/demo_access.py:512-515`)出实时数据是**合同内**行为:
    本条打「数据源标注存在且取值在合同枚举内」+「该路由 disposition 未被悄悄改」,
    真正的安全面由紧随其后的泄漏断言守住(实测:phone / internal_cost / profit /
    快照注入的 secret-owner 一个都不出网)。

    ⚠️ 函数名里的 "uses_only_snapshot" 是 D10 之前留下的旧名,**没改**:
    改名会让引用它的部署/复审记录对不上号。以本 docstring 为准。
    凭证面「只从快照出」由同文件
    `test_demo_transport_exposes_only_frozen_existing_portal_link` 与
    `tests/test_d10_demo_live_projection.py::test_portal_token_surface_stays_frozen` 守。
    """
    monkeypatch.setenv("DEMO_PORTAL_ENTRY_SECRET", "test-only-demo-portal-entry-secret-0123456789")
    grant = grant_zhejiang_dailin(request_id="demo-middleware-grant")
    app = FastAPI()
    calls = {"mutation": 0, "export": 0, "token": 0, "portal_verify": 0}

    @app.post("/api/writing/generate")
    async def mutation(request: FastAPIRequest):
        calls["mutation"] += 1
        return {"success": True, "received": await request.json()}

    @app.post("/api/portal/tokens")
    async def token_mutation():
        calls["token"] += 1
        return {"status": "success"}

    @app.post("/api/portal/verify")
    async def portal_verify(request: FastAPIRequest):
        calls["portal_verify"] += 1
        body = await request.json()
        candidate = body.get("token")
        if is_demo_portal_entry(candidate):
            context = resolve_demo_portal_entry(candidate)
            if not context:
                raise HTTPException(status_code=404, detail="资源不存在")
            return {
                "status": "success", "valid": True, "quote_id": 9701,
                "access_mode": "demo", "demo_transport_entry": candidate,
            }
        return {"status": "success", "valid": True, "quote_id": 9701, "access_mode": "live"}

    @app.get("/api/clients/601")
    async def safe_read(brand_id: int):
        return {
            "brand_id": brand_id, "name": "浙江岱林", "industry": "生命科学",
            "phone": "13900000124", "internal_cost": 1200, "profit": 3800,
            "status": "published",
        }

    @app.get("/api/customer/export.xlsx")
    async def raw_export(brand_id: int):
        calls["export"] += 1
        return {"brand_id": brand_id, "raw": True}

    app.add_middleware(DemoSafeResponseMiddleware)

    @app.middleware("http")
    async def fake_auth(request: FastAPIRequest, call_next):
        request.state.user = {
            "user_id": 132, "id": 132, "username": "demo_132",
            "is_admin": False,
            "client_brand_ids": [601] if request.headers.get("X-Real-Access") == "1" else [],
        }
        return await call_next(request)

    client = TestClient(app)
    demo_headers = {
        "X-Demo-Brand-ID": "601", "X-Demo-Case-ID": grant["case_id"],
        "X-Request-ID": "direct-with-demo-contract",
    }
    for path in (
        "/api/writing/generate", "/api/quotes/tasks/9701/confirm",
        "/api/monitoring/keywords/9831/disable", "/api/portal/tokens",
    ):
        blocked = client.post(path, json={"prompt": "不得记录正文"}, headers=demo_headers)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "DEMO_ACTION_PREVIEW"
    assert calls["mutation"] == 0 and calls["token"] == 0

    before_last_access = sql_one(
        "SELECT last_access_at FROM client_access_tokens WHERE token='DAILINPORTAL2026'"
    )
    portal_entry = demo_portal_entry(resolve_demo_access(132, 601), quote_id=9701)
    assert portal_entry
    verified_without_context = client.post(
        "/api/portal/verify", json={"token": portal_entry},
        headers={"X-Request-ID": "demo-handle-new-browser"},
    )
    assert verified_without_context.status_code == 200
    assert verified_without_context.json()["access_mode"] == "demo"
    assert verified_without_context.json()["demo_transport_entry"] == portal_entry
    live_customer = client.post(
        "/api/portal/verify", json={"token": "DAILINPORTAL2026"},
        headers={"X-Request-ID": "live-customer-token-control"},
    )
    assert live_customer.status_code == 200
    assert live_customer.json()["access_mode"] == "live"
    assert calls["portal_verify"] == 2
    assert sql_one(
        "SELECT last_access_at FROM client_access_tokens WHERE token='DAILINPORTAL2026'"
    ) == before_last_access

    # Even a malicious/legacy unknown field stored in the snapshot is excluded
    # by the read-time allow-list, and the live route handler is never called.
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE admin_demo_cases SET safe_snapshot=jsonb_set(
                 safe_snapshot,'{customer_overview,owner_name}',to_jsonb('secret-owner'::text),true)
               WHERE case_id=%s""",
            (grant["case_id"],),
        )
    safe = client.get(
        "/api/clients/601?brand_id=601",
        headers={**demo_headers, "X-Request-ID": "demo-safe-snapshot"},
    )
    assert safe.status_code == 200 and safe.headers["X-Demo-Mode"] == "demo"
    # 🔴 [triage 2026-08-20 · 判据期望订正] 原为
    #   `assert safe.json()["data_source"] == "admin_demo_cases.safe_snapshot"`。
    #   `data_source` 这个**响应体键**只由 `services/demo_access.py:1025 _snapshot_meta`
    #   产出,而它只喂冻结快照那条路(`:1708 _snapshot_response`)。
    #   `/api/clients/601` 在现役契约表里是 `services/demo_access.py:512-515`
    #   `customer_overview.read` · disposition=**live** —— D10 明写(`:1704-1706`)
    #   「其余读面默认走真实 handler 的实时投影;快照降级为数据源不可用时的 fallback」,
    #   理由在 `:1667-1672`:v1.x 白名单默认拒 → 演示用户打不开未登记的功能面。
    #   所以这一条断言等于要求这条路由退回 D10 之前的形态,不是发现了缺口。
    #
    #   数据源标注**没有丢**,它在响应头上(`:1719` / `:1760` / `_live_projection`
    #   出站标 live)。判据改成打「标注存在且取值在合同枚举内」+ 路由 disposition 本身,
    #   再由紧随其后的泄漏断言守住真正的安全面。
    #   实测(triage 探针):真 handler 被调 1 次,响应体只剩
    #   `{brand_id, name, industry, status}`,phone / internal_cost / profit /
    #   快照里注入的 secret-owner **一个都不在** —— 出站脱敏是真的在做事。
    assert safe.headers["X-Demo-Data-Source"] in (
        "live", "frozen_snapshot", "frozen_snapshot_fallback",
    ), safe.headers.get("X-Demo-Data-Source")
    assert resolve_demo_route_contract("GET", "/api/clients/601").disposition == "live", (
        "这条路由的 disposition 变了 —— 判据的期望要跟着重定,不许默认沿用")
    serialized = json.dumps(safe.json(), ensure_ascii=False)
    assert "secret-owner" not in serialized and "phone" not in serialized and "internal_cost" not in serialized

    for path in ("/api/customer/export.xlsx", "/api/portal/tokens/download", "/api/reports/raw-export"):
        export = client.get(path, headers={**demo_headers, "X-Request-ID": f"block-{path}"})
        assert export.status_code == 409
    assert calls["export"] == 0

    # A request without demo selection remains a normal commercial request.
    real = client.post(
        "/api/writing/generate", json={"brand_id": 601, "prompt": "真实客户输入"},
        headers={"X-Real-Access": "1", "X-Request-ID": "real-body-survives"},
    )
    assert real.status_code == 200 and real.json()["received"]["brand_id"] == 601
    assert calls["mutation"] == 1

    revoke_demo_case_grant(
        grant["id"], expected_version=grant["version"], reason="立即撤回后防迟到请求",
        actor_user_id=1, actor_username="admin_one",
        request_id="demo-middleware-revoke", ip_address="127.0.0.17",
    )
    late = client.post(
        "/api/writing/generate", json={"brand_id": 601},
        headers={**demo_headers, "X-Request-ID": "late-after-revoke"},
    )
    assert late.status_code == 404 and calls["mutation"] == 1
    revoked_entry = client.post(
        "/api/portal/verify", json={"token": portal_entry},
        headers={"X-Request-ID": "revoked-demo-handle-new-browser"},
    )
    assert revoked_entry.status_code == 404
    assert sql_one(
        """SELECT COUNT(*) FROM admin_demo_access_events
           WHERE viewer_user_id=132 AND request_id='late-after-revoke'"""
    ) == (0,)


def test_snapshot_projection_drops_unknown_fields_by_allow_list():
    projected = project_safe_snapshot({
        "customer_overview": {
            "brand_id": 601, "brand_name": "浙江岱林", "industry": "生命科学",
            "owner_name": "不得泄漏", "phone": "13900000124", "future_sensitive_field": "secret",
        },
        "report_snapshots": [{
            "id": 81,
            "content": "联系 13900000124 或 owner@example.com; token=production-secret",
            "summary_data": {
                "total_keywords": 3,
                "keyword_stats": [{"keyword": "生物安全柜", "avg_rate": 75, "private_cost": 500}],
                "internal_margin": 900,
            },
        }],
        "monitoring_snapshot": {
            "keywords": [{
                "id": 9831,
                "keyword": "生物安全柜",
                "note": "联系 owner@example.com",
                "detection_details": [{
                    "platform": "AI 搜索",
                    "snippet": "联系 13900000124，token=snippet-secret",
                    "citations": [{
                        "title": "公开证据 owner@example.com",
                        "url": "https://viewer:password@example.com/evidence?utm_source=demo&token=secret#private",
                    }],
                }],
            }],
            "results": [{
                "id": 101, "task_id": 71,
                "identity_evidence_snippet": "联系 13900000124，secret=evidence-secret",
            }],
            "logs": [{
                "id": 91, "brand_id": 601, "action": "add_keyword",
                "details": {"keyword": "生物安全柜", "password": "secret", "cost": 500},
            }],
        },
        "unknown_top_level": {"profit": 999},
    })
    assert projected["customer_overview"] == {
        "brand_id": 601, "brand_name": "浙江岱林", "industry": "生命科学",
    }
    assert "unknown_top_level" not in projected
    serialized = json.dumps(projected, ensure_ascii=False)
    assert "13900000124" not in serialized and "owner@example.com" not in serialized
    assert "production-secret" not in serialized and "private_cost" not in serialized
    assert "snippet-secret" not in serialized and "evidence-secret" not in serialized
    assert "internal_margin" not in serialized and '"password"' not in serialized
    assert "viewer" not in serialized and "token=secret" not in serialized and "#private" not in serialized
    assert "utm_source=demo" in serialized
    assert projected["report_snapshots"][0]["summary_data"]["total_keywords"] == 3
    assert project_safe_snapshot(projected)["monitoring_snapshot"]["included"] == [
        "keywords", "results", "logs",
    ]
    assert project_safe_snapshot(projected)["included"] == projected["included"]


def test_revoke_expiry_soft_delete_and_cross_tenant_all_become_404_predicate():
    grant = grant_zhejiang_dailin()
    case_id = grant["case_id"]
    with pytest.raises(CrossTenantNotFound):
        get_authorized_demo_case(129, case_id, action="demo_case.detail", request_id="cross-tenant-129", ip_address="127.0.0.11")
    revoked = revoke_demo_case_grant(
        grant["id"], expected_version=grant["version"], reason="演示结束立即撤回",
        actor_user_id=1, actor_username="admin_one", request_id="dailin-revoke-132",
        ip_address="127.0.0.12",
    )
    assert revoked["grant"]["status"] == "revoked"
    assert has_authorized_demo_case(132, case_id) is False
    assert list_authorized_demo_cases(132, page=1, page_size=30, search="浙江岱林", request_id="after-revoke-list", ip_address="127.0.0.12")["total"] == 0
    with pytest.raises(CrossTenantNotFound):
        get_authorized_demo_case(132, case_id, action="demo_case.detail", request_id="after-revoke-url", ip_address="127.0.0.12")

    expiring = grant_zhejiang_dailin(request_id="dailin-grant-expiring")
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE admin_demo_case_grants
               SET valid_from=NOW()-INTERVAL '2 seconds',
                   expires_at=NOW()-INTERVAL '1 second'
               WHERE id=%s""",
            (expiring["id"],),
        )
    assert resolve_demo_access(132, 601) is None
    with pytest.raises(CrossTenantNotFound):
        get_authorized_demo_case(132, case_id, action="demo_case.detail", request_id="after-expiry-url", ip_address="127.0.0.12")

    # A fresh grant also fails immediately when the source customer is soft deleted.
    fresh = grant_zhejiang_dailin(request_id="dailin-grant-soft-delete")
    assert fresh["status"] == "active"
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET is_active=0 WHERE id=132")
    assert resolve_demo_access(132, 601) is None
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET is_active=1 WHERE id=132")
    assert resolve_demo_access(132, 601) is not None
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE brands SET is_deleted=TRUE WHERE id=601")
    assert resolve_demo_access(132, 601) is None
    assert has_authorized_demo_case(132, case_id) is False


def test_organization_demo_grant_requires_live_membership(monkeypatch):
    monkeypatch.setenv("DEMO_PORTAL_ENTRY_SECRET", "test-only-demo-portal-entry-secret-0123456789")
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO organizations(id,owner_user_id,name,status) VALUES (77,28,'演示组织','active')")
        cur.execute("INSERT INTO organization_memberships(organization_id,user_id,status) VALUES (77,129,'active')")
        cur.execute(
            "INSERT INTO quotes(id,brand_id,brand_name,status) VALUES (9702,602,'浙江岱林','confirmed')"
        )
        cur.execute(
            """INSERT INTO client_access_tokens(quote_id,brand_name,token,is_active,expires_at)
               VALUES (9702,'浙江岱林','ORGDAILINPORTAL',1,'2027-07-21 00:00:00+00')"""
        )
    case_id = stable_demo_case_id(602, 9602)
    create_demo_case_grant(
        grantee_kind="organization", grantee_user_id=None, grantee_organization_id=77,
        brand_id=602, diagnosis_id=9602, case_id=case_id,
        capability="demo.customer.preview", expires_at=datetime.now(timezone.utc) + timedelta(days=7),
        reason="组织统一演示", actor_user_id=1, actor_username="admin_one",
        request_id="demo-grant-org-77", ip_address="127.0.0.13",
    )
    context = resolve_demo_access(129, 602)
    assert context is not None
    entry = demo_portal_entry(context, quote_id=9702)
    assert entry and resolve_demo_portal_entry(entry).context.viewer_user_id == 129
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute("UPDATE organization_memberships SET status='suspended' WHERE organization_id=77 AND user_id=129")
    assert has_authorized_demo_case(129, case_id) is False
    assert resolve_demo_portal_entry(entry) is None


def test_batch_revoke_is_atomic_when_any_selected_version_is_stale():
    first = grant_zhejiang_dailin(request_id="demo-batch-atomic-first")
    second_case_id = stable_demo_case_id(602, 9602)
    second = create_demo_case_grant(
        grantee_kind="user", grantee_user_id=132, grantee_organization_id=None,
        brand_id=602, diagnosis_id=9602, case_id=second_case_id,
        capability="demo.customer.preview",
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        reason="同一批次的第二个案例", actor_user_id=1, actor_username="admin_one",
        request_id="demo-batch-atomic-second", ip_address="127.0.0.16",
    )["grant"]
    with pytest.raises(CrossTenantConflict):
        revoke_demo_case_grants_batch(
            grants=[
                {"grant_id": first["id"], "expected_version": first["version"]},
                {"grant_id": second["id"], "expected_version": second["version"] + 1},
            ],
            reason="第二条版本故意过期", actor_user_id=1,
            actor_username="admin_one", request_id="demo-batch-atomic-revoke",
            ip_address="127.0.0.16",
        )
    assert sql_one(
        "SELECT COUNT(*) FROM admin_demo_case_grants WHERE id IN (%s,%s) AND status='active'",
        (first["id"], second["id"]),
    ) == (2,)


def test_demo_grant_batch_idempotency_binds_full_normalized_payload():
    # 🔴 [WO-D-R2 ③ 2026-08-20] 原为写死的 `datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)`。
    #   WO-D ④ 当时把它判成「潜伏」—— **判轻了**。挂死解开、这条判据第一次真的跑起来之后
    #   立刻实测到它**已经引爆**:
    #     psycopg2.errors.CheckViolation: violates check constraint
    #     "admin_demo_case_grants_window_check"   (CHECK expires_at > valid_from)
    #   valid_from 吃列默认 NOW(),而这个写死时刻在 2026-08-20T12:00Z —— 当前 UTC 已过它,
    #   于是 expires_at > valid_from 恒假。**正是 wp7 那个「固定锚 + 列默认 NOW」形态。**
    #   上一轮判轻的原因也记在这:我只查了「有没有断言读 status」,没查**库端约束**
    #   —— 判据能不能过,不只取决于断言,还取决于写得进不进去。
    #   改成相对锚,与同文件另外 3 处(85 / 854 / 875 行)写法一致。
    expires = datetime.now(timezone.utc) + timedelta(days=1)
    selections = [
        {"case_id": stable_demo_case_id(602, 9602), "brand_id": 602, "diagnosis_id": 9602},
        {"case_id": stable_demo_case_id(601, 9601), "brand_id": 601, "diagnosis_id": 9601},
    ]
    first = create_demo_case_grants_batch(
        grantee_kind="user", grantee_user_id=132, grantee_organization_id=None,
        selections=selections, expires_at=expires, note="明日演示", reason="Owner 批准批量案例",
        actor_user_id=1, actor_username="admin_one", request_id="grant-batch-full-hash",
        ip_address="127.0.0.22",
    )
    replay = create_demo_case_grants_batch(
        grantee_kind="user", grantee_user_id=132, grantee_organization_id=None,
        selections=list(reversed(selections)), expires_at=expires, note="明日演示", reason="Owner 批准批量案例",
        actor_user_id=1, actor_username="admin_one", request_id="grant-batch-full-hash",
        ip_address="127.0.0.22",
    )
    assert [item["id"] for item in replay["grants"]] == [item["id"] for item in first["grants"]]
    with pytest.raises(CrossTenantConflict) as conflict:
        create_demo_case_grants_batch(
            grantee_kind="user", grantee_user_id=132, grantee_organization_id=None,
            selections=selections, expires_at=expires, note="被篡改备注", reason="Owner 批准批量案例",
            actor_user_id=1, actor_username="admin_one", request_id="grant-batch-full-hash",
            ip_address="127.0.0.22",
        )
    assert conflict.value.code == "IDEMPOTENCY_CONFLICT"


@pytest.mark.parametrize(
    "statements,expected_category,expected_key",
    [
        (["INSERT INTO refund_work_orders(agent_user_id,status) VALUES (102,'submitted')"], "responsibilities", "refund_work_orders"),
        (["INSERT INTO dealer_resale_fulfillment_plans VALUES ('plan-102',102,132,'reserved')"], "orders", "jit_fulfillment_plans"),
        (["INSERT INTO dealer_resale_fulfillment_hops VALUES ('hop-plan',0,102,132,'reserved')"], "orders", "jit_fulfillment_hops"),
        ([
            "INSERT INTO dealer_resale_fulfillment_hops VALUES ('allocation-plan',0,102,132,'reserved')",
            "INSERT INTO dealer_resale_fulfillment_allocations(plan_id,hop_seq,status) VALUES ('allocation-plan',0,'reserved')",
        ], "inventory", "jit_reserved_allocations"),
        (["INSERT INTO dealer_resale_hop_profit_ledger(plan_id,hop_seq,seller_user_id,agent_payable_cents,status) VALUES ('profit-plan',0,102,900,'pending')"], "earnings", "jit_hop_payable_cents"),
        (["INSERT INTO agent_revenue_ledger(agent_user_id,agent_settlement_cents,status) VALUES (102,777,'frozen')"], "earnings", "agent_revenue_cents"),
    ],
)
def test_downgrade_census_includes_refund_and_jit_obligations(statements, expected_category, expected_key):
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        for statement in statements:
            cur.execute(statement)
    snapshot = provider_downgrade_snapshot(102)
    category = next(item for item in snapshot["categories"] if item["key"] == expected_category)
    assert expected_key in {item["key"] for item in category["outstanding"]}
    assert category["resolved"] is False and snapshot["ready"] is False


def test_downgrade_readiness_uses_same_platform_direct_terminal_protection(monkeypatch):
    monkeypatch.setenv("PLATFORM_DIRECT_SERVICE_USER_ID", "102")
    snapshot = provider_downgrade_snapshot(102)
    assert snapshot["ready"] is False
    category = next(item for item in snapshot["categories"] if item["key"] == "platform_manufacturer")
    assert any(
        item["key"] == "platform_direct_service_identity" and item["value"] == 1
        for item in category["outstanding"]
    )


def test_downgrade_allows_resale_profit_after_existing_payout_primitives_settle_it():
    with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_revenue_ledger(agent_user_id,agent_settlement_cents,status) "
            "VALUES (102,900,'settled') RETURNING id"
        )
        ledger_id = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO agent_settlement_requests(agent_user_id,request_amount_cents,status) "
            "VALUES (102,900,'paid') RETURNING id"
        )
        settlement_id = int(cur.fetchone()[0])
        cur.execute(
            "INSERT INTO agent_settlement_request_items(settlement_request_id,ledger_id,locked_amount_cents) "
            "VALUES (%s,%s,900)",
            (settlement_id, ledger_id),
        )
        cur.execute(
            """INSERT INTO dealer_resale_hop_profit_ledger(
                   plan_id,hop_seq,seller_user_id,agent_payable_cents,status,revenue_ledger_id)
               VALUES ('settled-profit-plan',0,102,900,'available',%s)""",
            (ledger_id,),
        )
    assert provider_downgrade_snapshot(102)["ready"] is True


def test_downgrade_plan_surfaces_every_category_and_never_confiscates():
    blocked = provider_downgrade_snapshot(28)
    assert blocked["ready"] is False and "clients" in blocked["blocker_categories"]
    plan = create_provider_downgrade_plan(
        28, strategy="settle_then_downgrade", target_provider_user_id=None,
        expected_identity_version=1, reason="建立逐项结清方案", actor_user_id=1,
        actor_username="admin_one", request_id="downgrade-plan-28", ip_address="127.0.0.14",
    )["plan"]
    result = confirm_provider_downgrade_plan(
        28, plan["id"], expected_plan_version=plan["version"], reason="再次核验仍有客户",
        actor_user_id=1, actor_username="admin_one", request_id="downgrade-confirm-blocked-28",
        ip_address="127.0.0.14",
    )
    assert result["blocked"] is True
    assert sql_one("SELECT agent_level FROM user_wallets WHERE user_id=28") == (1,)
    assert sql_one("SELECT COUNT(*) FROM customer_agent_bindings WHERE agent_user_id=28") == (3,)


def test_clean_provider_downgrade_reuses_existing_identity_primitive():
    assert provider_downgrade_snapshot(102)["ready"] is True
    created = create_provider_downgrade_plan(
        102, strategy="settle_then_downgrade", target_provider_user_id=None,
        expected_identity_version=1, reason="无依赖服务商完成结清", actor_user_id=1,
        actor_username="admin_one", request_id="downgrade-plan-102", ip_address="127.0.0.15",
    )
    result = confirm_provider_downgrade_plan(
        102, created["plan"]["id"], expected_plan_version=created["plan"]["version"],
        reason="逐项均为零后确认降级", actor_user_id=1, actor_username="admin_one",
        request_id="downgrade-confirm-102", ip_address="127.0.0.15",
    )
    assert result["plan"]["status"] == "completed"
    assert sql_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (0,)
    assert sql_one(
        "SELECT after_snapshot->>'inventory_confiscated',after_snapshot->>'funds_overwritten' FROM admin_cross_tenant_audits WHERE request_id='downgrade-confirm-102'"
    ) == ("false", "false")


def test_refund_responsibility_cannot_appear_after_provider_downgrade_census(monkeypatch):
    import services.admin_cross_tenant_governance as governance

    created = create_provider_downgrade_plan(
        102, strategy="settle_then_downgrade", target_provider_user_id=None,
        expected_identity_version=1, reason="验证退款责任并发互斥", actor_user_id=1,
        actor_username="admin_one", request_id="downgrade-refund-race-plan-102",
        ip_address="127.0.0.31",
    )
    census_complete = threading.Event()
    allow_downgrade_commit = threading.Event()
    refund_started = threading.Event()
    original_snapshot = governance.provider_downgrade_snapshot

    def paused_snapshot(provider_user_id, *, cur=None, lock=False):
        snapshot = original_snapshot(provider_user_id, cur=cur, lock=lock)
        if lock and int(provider_user_id) == 102:
            census_complete.set()
            assert allow_downgrade_commit.wait(timeout=10)
        return snapshot

    monkeypatch.setattr(governance, "provider_downgrade_snapshot", paused_snapshot)

    def downgrade():
        return governance.confirm_provider_downgrade_plan(
            102, created["plan"]["id"],
            expected_plan_version=created["plan"]["version"],
            reason="并发退款责任出现前完成降级", actor_user_id=1,
            actor_username="admin_one", request_id="downgrade-refund-race-confirm-102",
            ip_address="127.0.0.31",
        )

    def create_responsibility():
        with psycopg2.connect(DB_URL) as conn, conn.cursor() as cur:
            refund_started.set()
            _lock_active_refund_provider(cur, 102)
            cur.execute(
                "INSERT INTO refund_work_orders(agent_user_id,status) VALUES (102,'submitted')"
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        downgrade_future = pool.submit(downgrade)
        assert census_complete.wait(timeout=10)
        refund_future = pool.submit(create_responsibility)
        assert refund_started.wait(timeout=10)
        allow_downgrade_commit.set()
        assert downgrade_future.result(timeout=10)["plan"]["status"] == "completed"
        with pytest.raises(ValueError, match="服务商状态已变化"):
            refund_future.result(timeout=10)

    assert sql_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (0,)
    assert sql_one(
        "SELECT COUNT(*) FROM refund_work_orders WHERE agent_user_id=102"
    ) == (0,)


def test_downgrade_plan_and_identity_rollback_together_on_final_audit_collision():
    created = create_provider_downgrade_plan(
        102, strategy="settle_then_downgrade", target_provider_user_id=None,
        expected_identity_version=1, reason="验证原子降级事务", actor_user_id=1,
        actor_username="admin_one", request_id="downgrade-atomic-plan-102",
        ip_address="127.0.0.18",
    )
    record_admin_read(
        actor_user_id=1, actor_username="admin_one", action="provider.read",
        subject_kind="user", subject_id=102, request_id="downgrade-atomic-confirm-102",
        reason="制造最终审计碰撞", before={}, after={"read": True},
        ip_address="127.0.0.18",
    )
    with pytest.raises(CrossTenantConflict) as collision:
        confirm_provider_downgrade_plan(
            102, created["plan"]["id"], expected_plan_version=created["plan"]["version"],
            reason="身份与计划必须同回滚", actor_user_id=1,
            actor_username="admin_one", request_id="downgrade-atomic-confirm-102",
            ip_address="127.0.0.18",
        )
    assert collision.value.code == "AUDIT_REQUEST_ID_COLLISION"
    assert sql_one("SELECT agent_level FROM user_wallets WHERE user_id=102") == (1,)
    assert sql_one(
        "SELECT status,version,confirmed_at FROM admin_provider_downgrade_plans WHERE id=%s",
        (created["plan"]["id"],),
    ) == (created["plan"]["status"], created["plan"]["version"], None)


def test_demo_websocket_connects_then_revocation_closes_live_session(monkeypatch):
    import auth.jwt_utils

    grant = grant_zhejiang_dailin(request_id="demo-ws-grant-132")
    case_id = stable_demo_case_id(601, 9601)
    monkeypatch.setattr(
        auth.jwt_utils, "decode_jwt",
        lambda _token: {"user_id": 132, "perm_version": 1},
    )
    app = FastAPI()
    app.include_router(demo_router)

    with TestClient(app) as client:
        with client.websocket_connect(
            f"/api/demo-cases/{case_id}/ws",
            subprotocols=["omnirank-auth", "x" * 24],
        ) as websocket:
            assert websocket.receive_json() == {
                "type": "heartbeat", "access_mode": "demo",
                "presentation_preview": True,
            }
            revoke_demo_case_grant(
                grant["id"], expected_version=grant["version"],
                reason="验证已连接 WS 即时撤权", actor_user_id=1,
                actor_username="admin_one", request_id="demo-ws-revoke-132",
                ip_address="127.0.0.19",
            )
            websocket.send_text("continue")
            assert websocket.receive_json() == {"type": "permission-revoked"}
