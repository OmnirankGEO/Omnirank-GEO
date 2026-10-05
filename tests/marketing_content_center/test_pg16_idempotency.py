import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import psycopg2

from db import marketing_db
from db.connection import get_connection
from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块
from services.marketing import evidence, geo_factory, strategy_teachers


def _create(request_id: str, request_hash: str, user_id: int = 78121):
    return marketing_db.create_or_get_material_job(
        user_id=user_id,
        request_id=request_id,
        request_hash=request_hash,
        material_kind="bundle",
        feature_code="mktg_bundle_std",
        input_fields={"_geo": {"channels": ["professional_poster"]}},
    )


def test_request_identity_db_primitive_replays_same_job():
    request_id = f"lost-response-{uuid4()}"
    request_hash = uuid4().hex
    first, created = _create(request_id, request_hash)
    assert created is True

    # This is intentionally only the DB primitive contract. The Review-CTO
    # suite separately covers HTTP -> freeze -> worker -> provider -> settle
    # with a lost response and a force-killed subprocess.
    replay, replay_created = _create(request_id, request_hash)
    assert replay_created is False
    assert replay["id"] == first["id"]
    assert replay["input_fields_jsonb"]["_geo"]["request_id"] == request_id
    assert replay["input_fields_jsonb"]["_geo"]["request_hash"] == request_hash


def test_concurrent_retries_create_exactly_one_job():
    request_id = f"concurrent-{uuid4()}"
    request_hash = uuid4().hex
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(lambda _: _create(request_id, request_hash), range(8)))
    assert len({int(job["id"]) for job, _created in results}) == 1
    assert sum(1 for _job, created in results if created) == 1


def test_reused_request_id_with_different_payload_fails_closed():
    request_id = f"conflict-{uuid4()}"
    _create(request_id, "a" * 64)
    try:
        _create(request_id, "b" * 64)
    except ValueError as exc:
        assert str(exc) == "material_request_id_conflict"
    else:
        raise AssertionError("request id conflict replayed a different payload")


def test_legacy_existing_freeze_rehydrates_execution_context():
    request_id = f"worker-kill-{uuid4()}"
    request_hash = uuid4().hex
    job, _created = _create(request_id, request_hash, user_id=78122)
    marketing_db.update_job(int(job["id"]), status="generating", freeze_id=90210, billing_ref="geo-test")
    geo = {
        "request_id": request_id,
        "request_hash": request_hash,
        "channels": ["professional_poster"],
    }
    replay = asyncio.run(geo_factory._prepare_legacy(
        user_id=78122,
        brand_id=None,
        geo=geo,
        request_hash=request_hash,
        feature_code="mktg_bundle_std",
        resolution="1k",
    ))
    assert replay["replayed"] is True
    assert replay["job_id"] == int(job["id"])
    assert replay["_ctx"] == {
        "job_id": int(job["id"]),
        "billing_kind": "legacy",
        "freeze_id": 90210,
        "payer_user_id": 78122,
    }


def test_evidence_freeze_rejects_brand_or_diagnosis_revocation_and_mutation():
    conn = get_connection()
    try:
        cur = conn.cursor()
        ensure_brands_schema(cur)   # [R5 ⑤ 批2] 生产 SSOT 出口，必须排在下面那块之前
        cur.execute("""
            -- [R5 ⑤ 批2] brands 由 ensure_brands_schema()（生产 SSOT 出口）在本块之前建。
            --   手搓版 8 列且 owner_user_id NOT NULL（生产 nullable），比生产又窄又严。
            CREATE TABLE IF NOT EXISTS diagnosis_records (
              id BIGSERIAL PRIMARY KEY,brand_id BIGINT NOT NULL,total_score INTEGER,level TEXT,
              is_deleted BOOLEAN DEFAULT FALSE,result_visibility TEXT,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE brands ADD COLUMN IF NOT EXISTS company_name TEXT;
            ALTER TABLE brands ADD COLUMN IF NOT EXISTS industry TEXT;
            ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
            ALTER TABLE brands ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS total_score INTEGER;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS level TEXT;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS result_visibility TEXT;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
            INSERT INTO users(id,username,display_name,phone,email)
            VALUES (78123,'geo-evidence-78123','证据用户','1390078123','geo78123@example.test')
            ON CONFLICT (id) DO NOTHING;
        """)
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('brands','id'),GREATEST(COALESCE((SELECT MAX(id) FROM brands),0),1),TRUE)"
        )
        cur.execute(
            "INSERT INTO brands(name,company_name,industry,owner_user_id) VALUES (%s,%s,%s,%s) RETURNING id",
            (f"evidence-{uuid4()}", "证据测试品牌", "B2B", 78123),
        )
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO diagnosis_records(brand_id,total_score,level,result_visibility) VALUES (%s,71,'B','published') RETURNING id",
            (brand_id,),
        )
        diagnosis_id = int(cur.fetchone()["id"])
        conn.commit()
    finally:
        conn.close()

    snapshot = evidence.freeze_evidence(brand_id=brand_id, source_type="diagnosis", diagnosis_id=diagnosis_id)
    evidence.require_frozen_evidence_live(snapshot)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE diagnosis_records SET total_score=72 WHERE id=%s", (diagnosis_id,))
        conn.commit()
    finally:
        conn.close()
    try:
        evidence.require_frozen_evidence_live(snapshot)
    except ValueError as exc:
        assert str(exc) == "evidence_changed_since_freeze"
    else:
        raise AssertionError("mutated evidence remained readable")

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE brands SET is_deleted=TRUE WHERE id=%s", (brand_id,))
        conn.commit()
    finally:
        conn.close()
    try:
        evidence.require_frozen_evidence_live(snapshot)
    except ValueError as exc:
        assert str(exc) == "evidence_revoked_or_deleted"
    else:
        raise AssertionError("soft-deleted brand evidence remained readable")


def test_teacher_default_is_tenant_scoped_audited_and_rollbackable():
    principal_a = 79000 + (uuid4().int % 1000)
    principal_b = principal_a + 2000
    selected = strategy_teachers.set_default_preference(
        principal_user_id=principal_a,
        teacher_id="b2b_sales_coach",
        version="1.0.0",
        actor_user_id=principal_a,
        reason="pg16 tenant isolation test",
    )
    assert selected["teacher_id"] == "b2b_sales_coach"
    assert strategy_teachers.resolve_teacher(principal_user_id=principal_a)["teacher_id"] == "b2b_sales_coach"
    assert strategy_teachers.resolve_teacher(principal_user_id=principal_b)["teacher_id"] == "shu"

    rolled_back = strategy_teachers.set_default_preference(
        principal_user_id=principal_a,
        teacher_id="shu",
        version="1.0.0",
        actor_user_id=principal_a,
        reason="rollback",
    )
    assert rolled_back["teacher_id"] == "shu"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT COUNT(*)::int AS count
            FROM marketing_events
            WHERE event_type='strategy_teacher_preference_changed'
              AND payload_jsonb->>'principal_user_id'=%s
            """,
            (str(principal_a),),
        )
        assert int(cur.fetchone()["count"]) == 2
    finally:
        conn.close()


def test_execution_session_advisory_lock_is_explicitly_released(monkeypatch):
    job_id = 800000 + (uuid4().int % 100000)
    monkeypatch.setattr(
        geo_factory.marketing_db,
        "get_job",
        lambda _job_id: {"id": job_id, "status": "succeeded"},
    )
    monkeypatch.setattr(geo_factory.marketing_db, "list_assets", lambda _job_id: [])
    result = asyncio.run(geo_factory.execute_geo_package_job({"job_id": job_id}))
    assert result["status"] == "succeeded"

    fresh = psycopg2.connect(os.environ["TEST_DATABASE_URL"])
    try:
        with fresh.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0))",
                (f"geo-content-execute:{job_id}",),
            )
            assert cur.fetchone()[0] is True
            cur.execute(
                "SELECT pg_advisory_unlock(hashtextextended(%s,0))",
                (f"geo-content-execute:{job_id}",),
            )
    finally:
        fresh.close()


def test_employee_evidence_is_scoped_to_current_organization_membership():
    owner_user_id = 200000 + (uuid4().int % 300000)
    other_user_id = owner_user_id + 500000
    conn = get_connection()
    try:
        cur = conn.cursor()
        ensure_brands_schema(cur)   # [R5 ⑤ 批2] 生产 SSOT 出口，必须排在下面那块之前
        cur.execute("""
            -- [R5 ⑤ 批2] brands 由 ensure_brands_schema()（生产 SSOT 出口）在本块之前建。
            --   手搓版 8 列且 owner_user_id NOT NULL（生产 nullable），比生产又窄又严。
            CREATE TABLE IF NOT EXISTS diagnosis_records (
              id BIGSERIAL PRIMARY KEY,brand_id BIGINT NOT NULL,total_score INTEGER,level TEXT,
              is_deleted BOOLEAN DEFAULT FALSE,result_visibility TEXT,
              organization_id BIGINT,created_by_membership_id BIGINT,
              created_at TIMESTAMPTZ DEFAULT NOW()
            );
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS organization_id BIGINT;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS created_by_membership_id BIGINT;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS total_score INTEGER;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS level TEXT;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS result_visibility TEXT;
            ALTER TABLE diagnosis_records ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
            ALTER TABLE brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
        """)
        cur.executemany(
            "INSERT INTO users(id,username,display_name,phone,email) VALUES (%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
            [
                (owner_user_id, f"geo-evidence-{owner_user_id}", "租户证据用户", f"phone-{owner_user_id}", f"geo{owner_user_id}@example.test"),
                (other_user_id, f"geo-evidence-{other_user_id}", "其他证据用户", f"phone-{other_user_id}", f"geo{other_user_id}@example.test"),
            ],
        )
        cur.execute(
            "INSERT INTO organizations(owner_user_id,creation_request_id,name) VALUES (%s,%s,'证据测试组织') RETURNING id",
            (owner_user_id, f"evidence-org-{uuid4()}"),
        )
        organization_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO organization_roles(organization_id,code,name,created_by_user_id) VALUES (%s,'member','成员',%s) RETURNING id",
            (organization_id, owner_user_id),
        )
        role_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO organization_memberships(organization_id,user_id,role_id,status) VALUES (%s,%s,%s,'active') RETURNING id",
            (organization_id, owner_user_id, role_id),
        )
        own_membership_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO organization_memberships(organization_id,user_id,role_id,status) VALUES (%s,%s,%s,'active') RETURNING id",
            (organization_id, other_user_id, role_id),
        )
        other_membership_id = int(cur.fetchone()["id"])
        cur.execute(
            "SELECT setval(pg_get_serial_sequence('brands','id'),GREATEST(COALESCE((SELECT MAX(id) FROM brands),0),1),TRUE)"
        )
        cur.execute(
            "INSERT INTO brands(name,owner_user_id) VALUES (%s,%s) RETURNING id",
            (f"tenant-evidence-{uuid4()}", owner_user_id),
        )
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO diagnosis_records(
              brand_id,total_score,level,result_visibility,organization_id,created_by_membership_id,
              created_by_user_id,created_by_actor_kind,responsible_user_id,artifact_visibility
            ) VALUES (%s,61,'A','published',%s,%s,%s,'member',%s,'private') RETURNING id
            """,
            (brand_id, organization_id, own_membership_id, owner_user_id, owner_user_id),
        )
        own_diagnosis_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO diagnosis_records(
              brand_id,total_score,level,result_visibility,organization_id,created_by_membership_id,
              created_by_user_id,created_by_actor_kind,responsible_user_id,artifact_visibility
            ) VALUES (%s,99,'S','published',%s,%s,%s,'member',%s,'private')
            """,
            (brand_id, organization_id, other_membership_id, other_user_id, other_user_id),
        )
        conn.commit()
    finally:
        conn.close()

    snapshot = evidence.freeze_evidence(
        brand_id=brand_id,
        source_type="latest_diagnosis",
        organization_id=organization_id,
        membership_id=own_membership_id,
    )
    assert snapshot["source_id"] == own_diagnosis_id
    assert snapshot["facts"][0]["value"] == 61
    evidence.require_frozen_evidence_live(snapshot)

    try:
        evidence.freeze_evidence(
            brand_id=brand_id,
            source_type="latest_diagnosis",
            organization_id=organization_id,
            membership_id=other_membership_id + 99999,
        )
    except ValueError as exc:
        assert str(exc) == "published_diagnosis_evidence_not_found"
    else:
        raise AssertionError("employee could freeze another membership's diagnosis")
