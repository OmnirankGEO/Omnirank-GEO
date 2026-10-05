from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import uuid

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest


ROOT = Path(__file__).resolve().parents[1]
BASE_SQL = (ROOT / "scripts" / "fixtures" / "geo_article_closed_loop_pg16_base.sql").read_text(encoding="utf-8")
MIGRATION_SQL = (ROOT / "scripts" / "migration_geo_article_closed_loop_v1_2026_07_20.sql").read_text(encoding="utf-8")
TEST_DSN = os.getenv("ARTICLE_CLOSED_LOOP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not TEST_DSN, reason="ARTICLE_CLOSED_LOOP_TEST_DATABASE_URL not configured")


def _schema(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _connect(schema: str):
    return psycopg2.connect(TEST_DSN, options=f"-csearch_path={schema},public", cursor_factory=RealDictCursor)


def _fresh(schema: str) -> None:
    conn = psycopg2.connect(TEST_DSN)
    try:
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(BASE_SQL.replace("geo_article_closed_loop_test", schema))
        cur.execute(f"SET search_path TO {schema},public")
        cur.execute(MIGRATION_SQL)
    finally:
        conn.close()


def _seed_shadow(monkeypatch, schema: str, *, article_count: int = 2) -> tuple[int, int, int]:
    _fresh(schema)
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Tenant Brand',7) RETURNING id")
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO quotes(
                brand_id,owner_user_id,status,service_status,source_type,paid_amount,
                writing_status,confirmed_at,brand_name,industry
            ) VALUES (%s,7,'confirmed','active','agent_quote',100,'pending',NOW(),'Tenant Brand','医药')
            RETURNING id
            """,
            (brand_id,),
        )
        quote_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO confirmed_keywords(
                quote_id,keyword,required_articles,monitoring_query,is_core
            ) VALUES (%s,'隔离器推荐',%s,'客户购买的监测短句',TRUE) RETURNING id
            """,
            (quote_id, article_count),
        )
        keyword_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO keyword_selection_sessions(quote_id,status,confirmed_at,payment_received_at,updated_at) "
            "VALUES (%s,'active',NOW()::text,NOW()::text,NOW()::text)",
            (quote_id,),
        )
        conn.commit()
    finally:
        conn.close()

    import services.article_delivery_plan as plan

    monkeypatch.setenv("ARTICLE_PLAN_EVENT_OUTBOX_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_RECONCILER_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_PLAN_SHADOW_ENABLED", "true")
    monkeypatch.setattr(plan, "_get_connection", lambda: _connect(schema))
    assert plan.enqueue_quote_event_durable(quote_id, "quote_paid_standard")["enqueued"]
    assert plan.dispatch_plan_events(limit=10) == {"claimed": 1, "completed": 1, "failed": 0}
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE quotes SET article_plan_writing_mode='slot_aware_v1',article_plan_enrolled_at=NOW() WHERE id=%s",
            (quote_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return brand_id, quote_id, keyword_id


def test_slot_aware_save_is_sticky_non_destructive_and_uses_safe_style(monkeypatch):
    schema = _schema("geo_loop_sticky")
    _, quote_id, keyword_id = _seed_shadow(monkeypatch, schema)
    import db.diagnosis_db as diagnosis

    monkeypatch.setattr(diagnosis, "get_connection", lambda: _connect(schema))
    first = diagnosis.save_topics_batch(
        quote_id,
        [
            {"keyword_id": keyword_id, "original_keyword": "隔离器推荐", "optimized_title": "标题 A", "article_style": "ranking_v2"},
            {"keyword_id": keyword_id, "original_keyword": "隔离器推荐", "optimized_title": "标题 B", "article_style": "authority_ranking"},
        ],
    )
    assert len(set(first)) == 2
    second = diagnosis.save_topics_batch(
        quote_id,
        [
            {"keyword_id": keyword_id, "original_keyword": "隔离器推荐", "optimized_title": "新标题 A", "article_style": "ranking_v2"},
            {"keyword_id": keyword_id, "original_keyword": "隔离器推荐", "optimized_title": "新标题 B", "article_style": "authority_ranking"},
        ],
    )
    assert set(second) == set(first)

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("SELECT id,optimized_title,article_style,delivery_slot_key FROM topics ORDER BY id")
        rows = cur.fetchall()
        assert len(rows) == 2
        assert {row["optimized_title"] for row in rows} == {"新标题 A", "新标题 B"}
        assert {row["article_style"] for row in rows} == {"comparison_review"}
        assert all(row["delivery_slot_key"] for row in rows)
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_delivery_slots WHERE topic_id IS NOT NULL")
        assert cur.fetchone()["n"] == 2
    finally:
        conn.close()


def test_target_question_binding_fails_closed_on_quote_brand_tenant_mismatch(monkeypatch):
    schema = _schema("geo_loop_question_tenant")
    brand_id, quote_id, keyword_id = _seed_shadow(monkeypatch, schema, article_count=1)
    from services.article_closed_loop_metadata import SlotMetadataUnavailable, bind_topic_to_slot

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("UPDATE brands SET owner_user_id=8 WHERE id=%s", (brand_id,))
        cur.execute(
            "INSERT INTO topics(keyword_id,quote_id,optimized_title,status) "
            "VALUES (%s,%s,'不得绑定','draft') RETURNING id",
            (keyword_id, quote_id),
        )
        topic_id = int(cur.fetchone()["id"])
        with pytest.raises(SlotMetadataUnavailable, match="question_source_tenant_mismatch"):
            bind_topic_to_slot(
                cur,
                quote_id=quote_id,
                topic_id=topic_id,
                keyword_id=keyword_id,
                actor_user_id=7,
            )
        conn.rollback()
    finally:
        conn.close()


def test_canary_enrollment_requires_signed_new_quote_pass_and_is_sticky(monkeypatch):
    schema = _schema("geo_loop_canary_enroll")
    _, quote_id, _ = _seed_shadow(monkeypatch, schema, article_count=1)
    from services import article_closed_loop_contract as contract
    from services.article_closed_loop_metadata import enroll_new_quote_canary

    for flag in (
        "ARTICLE_PLAN_EVENT_OUTBOX_ENABLED",
        "ARTICLE_PLAN_RECONCILER_ENABLED",
        "ARTICLE_PLAN_SHADOW_ENABLED",
        "ARTICLE_PLAN_ASSISTED_METADATA_ENABLED",
        "ARTICLE_PLAN_CANARY_UPSERT_ENABLED",
    ):
        monkeypatch.setenv(flag, "true")
    monkeypatch.setenv("ARTICLE_PLAN_CANARY_ALLOWLIST", f"quote:{quote_id}")
    monkeypatch.setattr(contract, "CANARY_THRESHOLD_POLICY_VERSION", "signed-test-v1")
    monkeypatch.setattr(
        contract,
        "CANARY_NEW_QUOTE_NOT_BEFORE",
        datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE quotes SET article_plan_writing_mode=NULL,article_plan_enrolled_at=NULL,"
            "article_plan_contract_version=NULL,article_plan_enrolled_by=NULL,"
            "article_plan_enrollment_run_id=NULL WHERE id=%s",
            (quote_id,),
        )
        cur.execute(
            "UPDATE geo_article_plan_runs SET verdict='PASS', "
            "comparison_snapshot=%s::jsonb WHERE quote_id=%s",
            (json.dumps({"commercial_invariant_verdict": "PASS", "canary_verdict": "PASS"}), quote_id),
        )
        first = enroll_new_quote_canary(cur, quote_id=quote_id, actor_user_id=99)
        assert first["enrolled"] is True
        assert first["already_enrolled"] is False
        second = enroll_new_quote_canary(cur, quote_id=quote_id, actor_user_id=99)
        assert second == {"enrolled": True, "already_enrolled": True, "run_id": first["run_id"]}
        cur.execute(
            "SELECT article_plan_writing_mode,article_plan_contract_version,"
            "article_plan_enrolled_by,article_plan_enrollment_run_id "
            "FROM quotes WHERE id=%s",
            (quote_id,),
        )
        assert cur.fetchone() == {
            "article_plan_writing_mode": "slot_aware_v1",
            "article_plan_contract_version": contract.CONTRACT_VERSION,
            "article_plan_enrolled_by": 99,
            "article_plan_enrollment_run_id": first["run_id"],
        }
        conn.rollback()
    finally:
        conn.close()


def test_review_shadow_records_deduplicated_eligibility_without_enabling_hard_gate(monkeypatch):
    schema = _schema("geo_loop_review_shadow")
    _fresh(schema)
    content = "基于已保存证据的正常文章正文。"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    evidence_hash = "e" * 64
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO brands(name,owner_user_id) VALUES ('Shadow Brand',7) RETURNING id")
        brand_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO quotes(brand_id,owner_user_id,status,service_status,source_type,paid_amount,writing_status) "
            "VALUES (%s,7,'confirmed','active','agent_quote',100,'pending') RETURNING id",
            (brand_id,),
        )
        quote_id = int(cur.fetchone()["id"])
        cur.execute("INSERT INTO topics(quote_id,status) VALUES (%s,'completed') RETURNING id", (quote_id,))
        topic_id = int(cur.fetchone()["id"])
        cur.execute(
            """
            INSERT INTO articles(
                topic_id,quote_id,title,content,current_content_hash,evidence_manifest_hash,
                article_review_status,article_review,publication_profile
            ) VALUES (%s,%s,'Shadow Review',%s,%s,%s,'approved',%s::jsonb,'standard')
            RETURNING id
            """,
            (
                topic_id,
                quote_id,
                content,
                content_hash,
                evidence_hash,
                json.dumps(
                    {
                        "reviewed_content_hash": content_hash,
                        "reviewed_evidence_manifest_hash": evidence_hash,
                    }
                ),
            ),
        )
        article_id = int(cur.fetchone()["id"])
        conn.commit()
    finally:
        conn.close()

    import db.connection as db_connection
    from services.article_review_shadow import record_dispatch_review_shadow

    monkeypatch.setattr(db_connection, "get_connection", lambda: _connect(schema))
    monkeypatch.setenv("ARTICLE_REVIEW_SHADOW_ENABLED", "true")
    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "false")
    first = record_dispatch_review_shadow(
        article_id=article_id,
        dispatch_source="scheduler_retry",
        outgoing_content=content,
    )
    assert first["recorded"] is True
    assert first["eligible"] is True
    assert first["reason"] == "machine_rules_approved"
    second = record_dispatch_review_shadow(
        article_id=article_id,
        dispatch_source="scheduler_retry",
        outgoing_content=content,
    )
    assert second["recorded"] is False
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT eligible,reason,canonical_content_hash,outgoing_content_hash "
            "FROM geo_article_review_shadow_events"
        )
        row = cur.fetchone()
        assert row == {
            "eligible": True,
            "reason": "machine_rules_approved",
            "canonical_content_hash": content_hash,
            "outgoing_content_hash": content_hash,
        }
        cur.execute("UPDATE brands SET is_deleted=TRUE WHERE id=%s", (brand_id,))
        conn.commit()
    finally:
        conn.close()

    archived = record_dispatch_review_shadow(
        article_id=article_id,
        dispatch_source="legacy_batch_after_archive",
        outgoing_content=content,
    )
    assert archived == {"recorded": False, "reason": "article_or_active_brand_not_found"}
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS count FROM geo_article_review_shadow_events")
        assert cur.fetchone()["count"] == 1
    finally:
        conn.close()


def test_question_snapshot_article_revision_blocking_and_correction_are_scoped(monkeypatch):
    schema = _schema("geo_loop_metadata")
    _, quote_id, keyword_id = _seed_shadow(monkeypatch, schema, article_count=1)
    monkeypatch.setenv("ARTICLE_FLYWHEEL_CANDIDATE_ENABLED", "true")
    from services.article_closed_loop_metadata import (
        SlotMetadataUnavailable,
        bind_article_to_topic,
        bind_topic_to_slot,
        record_correction_signal,
        record_correction_signal_in_transaction_if_enabled,
        set_slot_blocked,
        unblock_slot,
    )

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO topics(keyword_id,quote_id,optimized_title,article_style,status) "
            "VALUES (%s,%s,'证据型标题','qa_recommendation','draft') RETURNING id",
            (keyword_id, quote_id),
        )
        topic_id = int(cur.fetchone()["id"])
        # The purchased phrase is frozen in the contract revision.  Editing the
        # mutable current keyword before binding must not rewrite that history.
        cur.execute(
            "UPDATE confirmed_keywords SET monitoring_query='后来被编辑的文本' WHERE id=%s",
            (keyword_id,),
        )
        bound = bind_topic_to_slot(cur, quote_id=quote_id, topic_id=topic_id, keyword_id=keyword_id, actor_user_id=7)
        slot_key = bound["delivery_slot_key"]
        cur.execute(
            "SELECT source_text_snapshot,question_source_type,resolution_status FROM geo_article_target_question_snapshots WHERE id=%s",
            (bound["question_snapshot_id"],),
        )
        question = cur.fetchone()
        assert question == {
            "source_text_snapshot": "客户购买的监测短句",
            "question_source_type": "purchased_monitoring_exact",
            "resolution_status": "exact",
        }

        cur.execute(
            "INSERT INTO articles(topic_id,quote_id,title,content) VALUES (%s,%s,'证据型标题','正文 v1') RETURNING id",
            (topic_id, quote_id),
        )
        article_id = int(cur.fetchone()["id"])
        cur.execute("UPDATE topics SET article_id=%s,status='completed' WHERE id=%s", (article_id, topic_id))
        assert bind_article_to_topic(cur, topic_id=topic_id, article_id=article_id, actor_user_id=7)["bound"]

        signal = record_correction_signal(
            cur,
            quote_id=quote_id,
            topic_id=topic_id,
            article_id=article_id,
            before_text="正文 v1",
            after_text="正文 v2",
            correction_type="factual_correction",
            reason="代理指出资质表述错误",
            actor_user_id=7,
        )
        assert signal == {"recorded": True, "candidate_status": "candidate"}
        with pytest.raises(SlotMetadataUnavailable, match="correction_article_scope_mismatch"):
            record_correction_signal(
                cur,
                quote_id=quote_id,
                topic_id=topic_id,
                article_id=article_id + 9999,
                before_text="正文 v1",
                after_text="伪跨作用域正文",
                correction_type="factual_correction",
                reason="不得写入",
                actor_user_id=7,
            )
        cur.execute("SELECT before_hash,after_hash,candidate_status FROM geo_article_correction_signals")
        correction = cur.fetchone()
        assert correction["before_hash"] == hashlib.sha256("正文 v1".encode()).hexdigest()
        assert correction["after_hash"] == hashlib.sha256("正文 v2".encode()).hexdigest()
        assert correction["candidate_status"] == "candidate"

        assert set_slot_blocked(
            cur,
            delivery_slot_key=slot_key,
            reason_code="missing_customer_material",
            user_message="请补充产品资质材料",
            owner_kind="agent",
            next_action="上传资质文件",
            target_resolution_at="2026-08-01T00:00:00+00:00",
            actor_user_id=7,
        ) == {"blocked": True}
        assert unblock_slot(
            cur,
            delivery_slot_key=slot_key,
            resolution_evidence="已补充并由运营核验",
            actor_user_id=7,
        ) == {"unblocked": True}
        cur.execute("DROP TABLE geo_article_correction_signals")
        cur.execute("UPDATE articles SET content='正文 v3' WHERE id=%s", (article_id,))
        contained = record_correction_signal_in_transaction_if_enabled(
            cur,
            quote_id=quote_id,
            topic_id=topic_id,
            article_id=article_id,
            before_text="正文 v2",
            after_text="正文 v3",
            correction_type="factual_correction",
            reason="故障隔离测试",
            actor_user_id=7,
        )
        assert contained == {"recorded": False, "reason": "UndefinedTable"}
        cur.execute("SELECT content FROM articles WHERE id=%s", (article_id,))
        assert cur.fetchone()["content"] == "正文 v3"
        conn.commit()

        cur.execute(
            "SELECT current_state,projection_version,resolution_evidence FROM geo_article_delivery_slots WHERE delivery_slot_key=%s",
            (slot_key,),
        )
        slot = cur.fetchone()
        assert slot["current_state"] == "active"
        assert slot["projection_version"] == 5
        assert slot["resolution_evidence"] == "已补充并由运营核验"
        cur.execute("SELECT COUNT(*) AS n FROM geo_article_delivery_slot_events WHERE delivery_slot_key=%s", (slot_key,))
        assert cur.fetchone()["n"] == 5
    finally:
        conn.close()


def test_article_rewrite_advances_projection_without_deleting_prior_revision(monkeypatch):
    schema = _schema("geo_loop_revision")
    _, quote_id, keyword_id = _seed_shadow(monkeypatch, schema, article_count=1)
    from services.article_closed_loop_metadata import bind_article_to_topic, bind_topic_to_slot

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO topics(keyword_id,quote_id,optimized_title,status) "
            "VALUES (%s,%s,'改写测试','draft') RETURNING id",
            (keyword_id, quote_id),
        )
        topic_id = int(cur.fetchone()["id"])
        bound = bind_topic_to_slot(
            cur,
            quote_id=quote_id,
            topic_id=topic_id,
            keyword_id=keyword_id,
            actor_user_id=7,
        )
        article_ids: list[int] = []
        revision_keys: list[str] = []
        for version in (1, 2):
            cur.execute(
                "INSERT INTO articles(topic_id,quote_id,title,content) "
                "VALUES (%s,%s,'改写测试',%s) RETURNING id",
                (topic_id, quote_id, f"正文 v{version}"),
            )
            article_id = int(cur.fetchone()["id"])
            article_ids.append(article_id)
            cur.execute(
                "UPDATE topics SET article_id=%s,status='completed' WHERE id=%s",
                (article_id, topic_id),
            )
            result = bind_article_to_topic(
                cur,
                topic_id=topic_id,
                article_id=article_id,
                actor_user_id=7,
            )
            assert result["bound"] is True
            revision_keys.append(result["revision_key"])
        conn.commit()

        cur.execute(
            "SELECT id,delivery_slot_key,article_revision_key FROM articles WHERE id=ANY(%s) ORDER BY id",
            (article_ids,),
        )
        rows = cur.fetchall()
        assert [row["id"] for row in rows] == article_ids
        assert {str(row["delivery_slot_key"]) for row in rows} == {bound["delivery_slot_key"]}
        assert [row["article_revision_key"].strip() for row in rows] == revision_keys
        assert revision_keys[0] != revision_keys[1]
        cur.execute(
            "SELECT article_id,projection_version FROM geo_article_delivery_slots WHERE delivery_slot_key=%s",
            (bound["delivery_slot_key"],),
        )
        slot = cur.fetchone()
        assert slot["article_id"] == article_ids[-1]
        assert slot["projection_version"] == 4
    finally:
        conn.close()


def test_blocked_slot_cannot_receive_a_late_article_generation(monkeypatch):
    schema = _schema("geo_loop_blocked_generation")
    _, quote_id, keyword_id = _seed_shadow(monkeypatch, schema, article_count=1)
    from services.article_closed_loop_metadata import (
        SlotMetadataUnavailable,
        bind_article_to_topic,
        bind_topic_to_slot,
        set_slot_blocked,
    )

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO topics(keyword_id,quote_id,optimized_title,status) "
            "VALUES (%s,%s,'阻断并发测试','writing') RETURNING id",
            (keyword_id, quote_id),
        )
        topic_id = int(cur.fetchone()["id"])
        bound = bind_topic_to_slot(
            cur,
            quote_id=quote_id,
            topic_id=topic_id,
            keyword_id=keyword_id,
            actor_user_id=7,
        )
        set_slot_blocked(
            cur,
            delivery_slot_key=bound["delivery_slot_key"],
            reason_code="evidence_missing",
            user_message="缺少必要证据",
            owner_kind="agent",
            next_action="补充证据",
            target_resolution_at="2026-08-01T00:00:00+00:00",
            actor_user_id=7,
        )
        cur.execute(
            "INSERT INTO articles(topic_id,quote_id,title,content) "
            "VALUES (%s,%s,'迟到正文','不得继承') RETURNING id",
            (topic_id, quote_id),
        )
        article_id = int(cur.fetchone()["id"])
        cur.execute("UPDATE topics SET article_id=%s,status='completed' WHERE id=%s", (article_id, topic_id))
        with pytest.raises(SlotMetadataUnavailable, match="slot_aware_article_binding_not_active"):
            bind_article_to_topic(cur, topic_id=topic_id, article_id=article_id, actor_user_id=7)
        conn.rollback()
    finally:
        conn.close()


def test_publication_fact_requires_explicit_url_and_completes_the_existing_slot(monkeypatch):
    schema = _schema("geo_loop_publish")
    _, quote_id, keyword_id = _seed_shadow(monkeypatch, schema, article_count=1)
    from services.article_closed_loop_metadata import bind_article_to_topic, bind_topic_to_slot
    import services.article_delivery_plan as plan

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO topics(keyword_id,quote_id,optimized_title,status) VALUES (%s,%s,'发布标题','draft') RETURNING id",
            (keyword_id, quote_id),
        )
        topic_id = int(cur.fetchone()["id"])
        bound = bind_topic_to_slot(cur, quote_id=quote_id, topic_id=topic_id, keyword_id=keyword_id, actor_user_id=7)
        cur.execute(
            "INSERT INTO articles(topic_id,quote_id,title,content) VALUES (%s,%s,'发布标题','发布正文') RETURNING id",
            (topic_id, quote_id),
        )
        article_id = int(cur.fetchone()["id"])
        cur.execute("UPDATE topics SET article_id=%s,status='completed' WHERE id=%s", (article_id, topic_id))
        bind_article_to_topic(cur, topic_id=topic_id, article_id=article_id, actor_user_id=7)
        published_article_id = article_id
        cur.execute(
            "INSERT INTO articles(topic_id,quote_id,title,content) "
            "VALUES (%s,%s,'发布标题（重写）','尚未发布的新正文') RETURNING id",
            (topic_id, quote_id),
        )
        current_article_id = int(cur.fetchone()["id"])
        cur.execute(
            "UPDATE topics SET article_id=%s,status='completed' WHERE id=%s",
            (current_article_id, topic_id),
        )
        bind_article_to_topic(cur, topic_id=topic_id, article_id=current_article_id, actor_user_id=7)
        cur.execute("CREATE TABLE publish_orders(id SERIAL PRIMARY KEY,article_id INTEGER NOT NULL)")
        cur.execute(
            "CREATE TABLE publish_order_items(id SERIAL PRIMARY KEY,order_id INTEGER NOT NULL,status TEXT,publish_url TEXT,published_at TIMESTAMPTZ DEFAULT NOW())"
        )
        cur.execute("INSERT INTO publish_orders(article_id) VALUES (%s) RETURNING id", (published_article_id,))
        order_id = int(cur.fetchone()["id"])
        cur.execute(
            "INSERT INTO publish_order_items(order_id,status,publish_url) VALUES (%s,'published','https://example.com/a') RETURNING id,published_at",
            (order_id,),
        )
        item = cur.fetchone()
        item_id = int(item["id"])
        incomplete = plan.enqueue_publication_locked_in_transaction_if_enabled(
            cur,
            publication_source="publish_order_items",
            publication_source_id=item_id,
            article_id=published_article_id,
            platform="搜狐",
            provider_receipt="receipt-1",
            public_url="",
            submitted_content_hash=hashlib.sha256("发布正文".encode()).hexdigest(),
        )
        assert incomplete == {"enqueued": False, "reason": "strict_publication_fact_incomplete"}
        strict = plan.enqueue_publication_locked_in_transaction_if_enabled(
            cur,
            publication_source="publish_order_items",
            publication_source_id=item_id,
            article_id=published_article_id,
            platform="搜狐",
            provider_receipt="receipt-1",
            public_url="https://example.com/a",
            submitted_content_hash=hashlib.sha256("发布正文".encode()).hexdigest(),
            published_at=item["published_at"],
        )
        assert strict["enqueued"] is True
        conn.commit()
    finally:
        conn.close()

    assert plan.dispatch_plan_events(limit=10) == {"claimed": 1, "completed": 1, "failed": 0}
    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT article_id,completion_evidence,projection_version FROM geo_article_delivery_slots WHERE delivery_slot_key=%s",
            (bound["delivery_slot_key"],),
        )
        slot = cur.fetchone()
        assert slot["article_id"] == current_article_id
        assert slot["completion_evidence"] == f"publish_order_items:{item_id}"
        assert slot["projection_version"] == 5
    finally:
        conn.close()


def test_agent_summary_uses_plain_language_and_tenant_task_filter(monkeypatch):
    schema = _schema("geo_loop_summary")
    _, quote_id, _ = _seed_shadow(monkeypatch, schema, article_count=1)
    monkeypatch.setenv("ARTICLE_PLAN_READ_SUMMARY_ENABLED", "true")
    monkeypatch.setenv("ARTICLE_WRITING_SIMPLE_UI_ENABLED", "true")
    import services.article_closed_loop_queries as queries

    monkeypatch.setattr(queries, "_conn", lambda: _connect(schema))
    summary = queries.get_project_summary(quote_id)
    assert summary["available"] is True
    assert summary["delivery"] == {
        "contract_total": 1,
        "due": 1,
        "completed": 0,
        "pending": 1,
        "blocked": 0,
        "published": 0,
    }
    assert summary["next_action"] == {"label": "生成标题", "kind": "generate_titles"}
    ordinary_payload = json.dumps(summary, ensure_ascii=False)
    for internal_term in ("delivery_slot_key", "source_version", "experiment_arm", "evidence_manifest_hash"):
        assert internal_term not in ordinary_payload

    conn = _connect(schema)
    try:
        cur = conn.cursor()
        cur.execute("SELECT delivery_slot_key FROM geo_article_delivery_slots WHERE quote_id=%s", (quote_id,))
        slot_key = str(cur.fetchone()["delivery_slot_key"])
        from services.article_closed_loop_metadata import set_slot_blocked

        set_slot_blocked(
            cur,
            delivery_slot_key=slot_key,
            reason_code="missing_customer_material",
            user_message="需要补充客户资料",
            owner_kind="agent",
            next_action="上传资料",
            target_resolution_at="2026-08-01T00:00:00+00:00",
            actor_user_id=7,
        )
        conn.commit()
    finally:
        conn.close()
    assert queries.list_agent_tasks([999])["items"] == []
    tasks = queries.list_agent_tasks([1])
    assert tasks["items"][0]["project_name"] == "Tenant Brand"
    assert tasks["items"][0]["next_action"] == "上传资料"
