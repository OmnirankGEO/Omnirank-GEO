from __future__ import annotations

import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("MONITORING_SCHEDULER_PG_TEST_URL")


@pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")
def test_diagnosis_identity_rollback_fresh_database_safe_skip():
    """[R4 · P1-2] fresh 空库（events 表不存在）诊断 rollback 安全跳过，可连跑两次。"""
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    database_name = f"diag_rollback_fresh_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))
        parsed = urlsplit(PG_URL)
        target_url = urlunsplit(
            (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
        )
        rollback_sql = (
            ROOT / "scripts" / "rollback_diagnosis_identity_review_2026_07_22.sql"
        ).read_text(encoding="utf-8")
        conn = psycopg2.connect(target_url, cursor_factory=RealDictCursor)
        try:
            with conn, conn.cursor() as cursor:
                cursor.execute(rollback_sql)  # 表不存在 → 跳过
                cursor.execute(rollback_sql)  # 幂等第二次
        finally:
            conn.close()
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
        finally:
            admin.close()


@pytest.mark.skipif(not PG_URL, reason="real PostgreSQL URL is required")
def test_monitoring_identity_review_real_postgres_contract(monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    from db import connection as connection_db

    database_name = f"monitor_identity_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

        parsed = urlsplit(PG_URL)
        target_url = urlunsplit(
            (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
        )

        def connect():
            return psycopg2.connect(target_url, cursor_factory=RealDictCursor)

        monkeypatch.setenv("DATABASE_URL", target_url)
        monkeypatch.setenv("TEST_DATABASE_URL", target_url)
        monkeypatch.setattr(connection_db, "DATABASE_URL", target_url)
        monkeypatch.setattr(connection_db, "_pool", None)

        # Import schema owners only after the isolated database is authoritative.
        # diagnosis_db initializes tables at import time.
        from db import diagnosis_db, monitoring_db

        monkeypatch.setattr(diagnosis_db, "get_connection", connect)
        monkeypatch.setattr(monitoring_db, "get_connection", connect)

        diagnosis_db.init_db()
        monitoring_db.init_monitoring_tables()

        yuanbao_default_sql = (
            ROOT / "scripts" / "migration_monitoring_yuanbao_default_2026_07_20.sql"
        ).read_text(encoding="utf-8")
        product_sql = (
            ROOT / "scripts" / "migration_monitoring_product_matrix_2026_07_21.sql"
        ).read_text(encoding="utf-8")
        migration_sql = (
            ROOT / "scripts" / "migration_monitoring_identity_review_2026_07_21.sql"
        ).read_text(encoding="utf-8")
        # [2026-07-22 板块A D6] 诊断侧泛化 migration · 启动自检契约已含新列,
        # 本测试必须在旧 migration 之后接跑(含连跑两次幂等证明)
        diagnosis_migration_sql = (
            ROOT / "scripts" / "migration_diagnosis_identity_review_2026_07_22.sql"
        ).read_text(encoding="utf-8")
        rollback_sql = (
            ROOT / "scripts" / "rollback_monitoring_identity_review_2026_07_21.sql"
        ).read_text(encoding="utf-8")
        # [R4 · P1-2] 诊断泛化回滚：fail-closed（durable diagnosis 事件阻断）
        diagnosis_rollback_sql = (
            ROOT / "scripts" / "rollback_diagnosis_identity_review_2026_07_22.sql"
        ).read_text(encoding="utf-8")

        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN DEFAULT FALSE;
                ALTER TABLE public.brands ADD COLUMN IF NOT EXISTS brand_display_names TEXT;
                CREATE TABLE IF NOT EXISTS public.users (
                    id BIGINT PRIMARY KEY, username TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS public.roles (
                    id BIGINT PRIMARY KEY, name TEXT NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS public.user_roles (
                    user_id BIGINT NOT NULL, role_id BIGINT NOT NULL,
                    PRIMARY KEY (user_id, role_id)
                );
                CREATE TABLE IF NOT EXISTS public.user_clients (
                    user_id BIGINT NOT NULL, brand_id INTEGER NOT NULL,
                    PRIMARY KEY (user_id, brand_id)
                );
                CREATE TABLE IF NOT EXISTS public.client_profiles (
                    id BIGSERIAL PRIMARY KEY, brand_id INTEGER NOT NULL,
                    brand_display_names TEXT, is_deleted INTEGER DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS public.brand_aliases (
                    id BIGSERIAL PRIMARY KEY, canonical_name TEXT NOT NULL,
                    alias TEXT NOT NULL, brand_id INTEGER, source TEXT DEFAULT 'manual',
                    UNIQUE (canonical_name, alias)
                );
                """
            )
            cursor.execute(yuanbao_default_sql)
            cursor.execute(product_sql)
            cursor.execute(migration_sql)
            cursor.execute(migration_sql)
            cursor.execute(diagnosis_migration_sql)
            cursor.execute(diagnosis_migration_sql)
            monitoring_db.assert_monitoring_product_matrix_ready(cursor)
            monitoring_db.assert_monitoring_identity_review_ready(cursor)

            # The rollback is reversible before durable decisions exist.
            cursor.execute(rollback_sql)
            cursor.execute(migration_sql)
            cursor.execute(diagnosis_migration_sql)
            monitoring_db.assert_monitoring_identity_review_ready(cursor)

            # Same-name lures cannot redirect public-qualified migration/readiness.
            cursor.execute("CREATE SCHEMA identity_lure")
            cursor.execute(
                """
                CREATE TABLE identity_lure.monitoring_identity_decision_events (
                    event_id BIGINT PRIMARY KEY
                );
                CREATE TEMP TABLE monitoring_identity_name_decisions (
                    brand_id INTEGER, normalized_name TEXT
                );
                SET LOCAL search_path = identity_lure, pg_temp, public;
                """
            )
            cursor.execute(migration_sql)
            cursor.execute(diagnosis_migration_sql)
            monitoring_db.assert_monitoring_identity_review_ready(cursor)

            cursor.execute("SAVEPOINT weak_function")
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION public.reject_monitoring_identity_event_mutation()
                RETURNS TRIGGER LANGUAGE plpgsql AS $$
                BEGIN
                    RETURN NEW;
                    RAISE EXCEPTION 'monitoring identity decision events are append-only';
                END;
                $$
                """
            )
            with pytest.raises(RuntimeError, match="function drift"):
                monitoring_db.assert_monitoring_identity_review_ready(cursor)
            cursor.execute("ROLLBACK TO SAVEPOINT weak_function")
            cursor.execute("RELEASE SAVEPOINT weak_function")

            cursor.execute("SAVEPOINT weak_check")
            cursor.execute(
                """
                ALTER TABLE public.monitoring_results
                    DROP CONSTRAINT chk_monitoring_results_identity_markers;
                ALTER TABLE public.monitoring_results
                    ADD CONSTRAINT chk_monitoring_results_identity_markers CHECK (TRUE);
                """
            )
            with pytest.raises(RuntimeError, match="CHECK contract drift"):
                monitoring_db.assert_monitoring_identity_review_ready(cursor)
            cursor.execute("ROLLBACK TO SAVEPOINT weak_check")
            cursor.execute("RELEASE SAVEPOINT weak_check")

            cursor.execute(
                """
                INSERT INTO public.users (id, username) VALUES
                    (7001, 'owner'), (7002, 'assigned'), (7003, 'revoked'), (7004, 'admin');
                INSERT INTO public.roles (id, name) VALUES (1, 'admin');
                INSERT INTO public.user_roles (user_id, role_id) VALUES (7004, 1);
                INSERT INTO public.brands
                    (name, company_name, owner_user_id, brand_display_names, is_deleted)
                VALUES ('测试主品牌', '测试主品牌有限公司', 7001,
                        '["测试主品牌"]', FALSE)
                RETURNING id;
                """
            )
            brand_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO public.user_clients (user_id, brand_id)
                VALUES (7002, %s), (7003, %s);
                INSERT INTO public.client_profiles (brand_id, brand_display_names)
                VALUES (%s, '["测试主品牌"]');
                INSERT INTO public.monitoring_tasks
                    (client_id, brand_id, task_name, keyword_count, platform_count,
                     total_tests, completed_tests, status, trigger_type)
                VALUES ('identity-review', %s, 'identity review', 4, 4, 4, 4,
                        'completed', 'manual')
                RETURNING id;
                """,
                (brand_id, brand_id, brand_id, brand_id),
            )
            task_id = int(cursor.fetchone()["id"])
        conn.close()

        def result(platform: str, *, pending: bool = False, candidate: str = "候选甲"):
            return {
                "task_id": task_id,
                "keyword": f"{platform} 测试词",
                "platform": platform,
                "brand_id": brand_id,
                "identity_brand_id": brand_id,
                "identity_review_state": "pending" if pending else "not_required",
                "identity_candidates": [candidate] if pending else [],
                "identity_evidence_snippet": "回答中出现相近名称，需要人工确认" if pending else "",
                "is_detected": not pending,
                "mention_type": "pending_identity" if pending else "direct",
                "response_status": "brand_identity_unresolved" if pending else "success",
                "full_response": f"{candidate} 的完整回答证据，" + "有效正文" * 30,
                "response_snippet": f"{candidate} 的回答",
                "sent_question_snapshot": f"{platform} 测试词",
                "provider": platform,
                "model": f"{platform}-model",
                "surface": "monitoring",
                "target_brand": "测试主品牌",
            }

        assert monitoring_db.batch_save_results(
            [
                result("deepseek", pending=True),
                result("dashscope"),
                result("kimi"),
                result("doubao"),
            ]
        ) == 4
        all_rows = monitoring_db.get_task_results(task_id)
        eligible_rows = monitoring_db.get_aggregate_task_results(task_id)
        assert len(all_rows) == 4
        assert len(eligible_rows) == 3
        assert {row["platform"] for row in eligible_rows} == {"dashscope", "kimi", "doubao"}

        pending = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)
        assert len(pending) == 1
        pending_row = pending[0]
        request_id = str(uuid.uuid4())
        resolved = monitoring_db.decide_monitoring_identity_review(
            result_id=pending_row["id"],
            brand_id=brand_id,
            actor_user_id=7001,
            action="yes",
            selected_name="候选甲",
            expected_version=0,
            evidence_hash=pending_row["identity_evidence_hash"],
            request_id=request_id,
        )
        assert resolved["status"] == "resolved"
        replay = monitoring_db.decide_monitoring_identity_review(
            result_id=pending_row["id"],
            brand_id=brand_id,
            actor_user_id=7001,
            action="yes",
            selected_name="候选甲",
            expected_version=0,
            evidence_hash=pending_row["identity_evidence_hash"],
            request_id=request_id,
        )
        assert replay["status"] == "idempotent"
        assert len(monitoring_db.get_aggregate_task_results(task_id)) == 4

        # A failed batch cannot leave the first pending row without its evidence.
        before = len(monitoring_db.get_task_results(task_id))
        with pytest.raises(ValueError, match="identity_brand_id"):
            monitoring_db.batch_save_results(
                [result("deepseek", pending=True, candidate="候选乙"), {
                    **result("kimi", pending=True, candidate="候选丙"),
                    "brand_id": None,
                    "identity_brand_id": None,
                }]
            )
        assert len(monitoring_db.get_task_results(task_id)) == before

        # Live RBAC is checked at commit time; stale request claims cannot write.
        assert monitoring_db.batch_save_results(
            [result("deepseek", pending=True, candidate="候选乙")]
        ) == 1
        revoked_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "DELETE FROM public.user_clients WHERE user_id = 7003 AND brand_id = %s",
                (brand_id,),
            )
        conn.close()
        with pytest.raises(PermissionError):
            monitoring_db.decide_monitoring_identity_review(
                result_id=revoked_row["id"], brand_id=brand_id, actor_user_id=7003,
                action="no", selected_name="候选乙", expected_version=0,
                evidence_hash=revoked_row["identity_evidence_hash"],
                request_id=str(uuid.uuid4()),
            )

        # Twenty concurrent confirmations produce one event and one version step.
        def concurrent_decision(index: int):
            try:
                return monitoring_db.decide_monitoring_identity_review(
                    result_id=revoked_row["id"], brand_id=brand_id, actor_user_id=7004,
                    action="no", selected_name="候选乙", expected_version=0,
                    evidence_hash=revoked_row["identity_evidence_hash"],
                    request_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"identity-{index}")),
                )["status"]
            except monitoring_db.MonitoringIdentityReviewConflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=20) as executor:
            statuses = list(executor.map(concurrent_decision, range(20)))
        assert statuses.count("resolved") == 1
        assert statuses.count("conflict") == 19

        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS count FROM public.monitoring_identity_decision_events "
                "WHERE result_id = %s",
                (revoked_row["id"],),
            )
            assert cursor.fetchone()["count"] == 1
            cursor.execute(
                "SELECT identity_review_state, identity_decision_version "
                "FROM public.monitoring_results WHERE id = %s",
                (revoked_row["id"],),
            )
            final_row = cursor.fetchone()
            assert final_row == {"identity_review_state": "rejected", "identity_decision_version": 1}
            with pytest.raises(psycopg2.Error, match="append-only"):
                cursor.execute(
                    "UPDATE public.monitoring_identity_decision_events SET action = 'yes' "
                    "WHERE result_id = %s",
                    (revoked_row["id"],),
                )
            conn.rollback()
        conn.close()

        # A separate pending-only task cannot be archived and deleted out from
        # under the durable human-review outlet.
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.brands
                    (name, company_name, owner_user_id, brand_display_names, is_deleted)
                VALUES ('待核验清理守卫品牌', '待核验清理守卫品牌有限公司', 7001,
                        '["待核验清理守卫品牌"]', FALSE)
                RETURNING id
                """
            )
            pending_only_brand_id = int(cursor.fetchone()["id"])
            cursor.execute(
                """
                INSERT INTO public.monitoring_tasks
                    (client_id, brand_id, task_name, keyword_count, platform_count,
                     total_tests, completed_tests, status, trigger_type)
                VALUES ('pending-clear-guard', %s, 'pending clear guard', 1, 1, 1, 1,
                        'completed', 'manual')
                RETURNING id
                """,
                (pending_only_brand_id,),
            )
            pending_only_task_id = int(cursor.fetchone()["id"])
        conn.close()
        pending_only = result("deepseek", pending=True, candidate="不可清除候选")
        pending_only["task_id"] = pending_only_task_id
        pending_only["brand_id"] = pending_only_brand_id
        pending_only["identity_brand_id"] = pending_only_brand_id
        assert monitoring_db.batch_save_results([pending_only]) == 1
        clear_result = monitoring_db.clear_monitoring_data(
            brand_id=pending_only_brand_id,
            quote_id=0,
            operator="pg-test",
            reason="must preserve pending review",
        )
        assert clear_result["success"] is False
        assert "身份记录" in clear_result["error"]
        rollback_result = monitoring_db.rollback_task(pending_only_task_id)
        assert rollback_result["success"] is False
        assert "身份记录" in rollback_result["error"]
        assert len(
            monitoring_db.list_pending_monitoring_identity_reviews(pending_only_brand_id)
        ) == 1

        # Persisted negative decisions feed only the deterministic resolver.
        monkeypatch.setattr(connection_db, "get_connection", connect)
        from services.brand_identity_resolver import BrandIdentityResolver, BrandVerdict

        local = BrandIdentityResolver.for_brand(brand_id).resolve_local(
            "候选乙被列入回答，但这不是目标品牌。"
        )
        assert local.verdict is BrandVerdict.NO
        assert local.reason == "human_rejected_name"

        # Admin is live-authorized, and custom confirmation appends instead of replacing.
        assert monitoring_db.batch_save_results(
            [result("deepseek", pending=True, candidate="待改正确名称")]
        ) == 1
        custom_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        monitoring_db.decide_monitoring_identity_review(
            result_id=custom_row["id"], brand_id=brand_id, actor_user_id=7004,
            action="custom", selected_name="测试主品牌旗舰店", expected_version=0,
            evidence_hash=custom_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute("SELECT brand_display_names FROM public.brands WHERE id = %s", (brand_id,))
            names = json.loads(cursor.fetchone()["brand_display_names"])
            assert names == ["测试主品牌", "候选甲", "测试主品牌旗舰店"]
        conn.close()

        # "None of these" rejects every persisted candidate atomically rather
        # than letting one per-candidate button incorrectly decide the row.
        multi_candidate = result("doubao", pending=True, candidate="错误候选甲")
        multi_candidate["identity_candidates"] = ["错误候选甲", "错误候选乙"]
        assert monitoring_db.batch_save_results([multi_candidate]) == 1
        multi_row = monitoring_db.list_pending_monitoring_identity_reviews(brand_id)[0]
        monitoring_db.decide_monitoring_identity_review(
            result_id=multi_row["id"], brand_id=brand_id, actor_user_id=7004,
            action="no", selected_name="错误候选甲", expected_version=0,
            evidence_hash=multi_row["identity_evidence_hash"], request_id=str(uuid.uuid4()),
        )
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT normalized_name, decision
                  FROM public.monitoring_identity_name_decisions
                 WHERE brand_id = %s AND display_name = ANY(%s)
                 ORDER BY display_name
                """,
                (brand_id, ["错误候选甲", "错误候选乙"]),
            )
            assert [row["decision"] for row in cursor.fetchall()] == ["negative", "negative"]
            cursor.execute(
                """
                SELECT metadata
                  FROM public.monitoring_identity_decision_events
                 WHERE result_id = %s
                """,
                (multi_row["id"],),
            )
            # [工单 M-1 ② 2026-07-28 语义扩展] 事件 metadata 增加本地重判审计面
            # (rejudge_* + counted_is_detected)。本条 'no' 决策把两个候选写入否定表,
            # 重判据此命中 human_rejected_name → 按未提及计入。
            assert cursor.fetchone()["metadata"] == {
                "source": "monitoring_human_review",
                "decision_scope": "candidate_set_rejected",
                "decision_name_count": 2,
                "decision_names": ["错误候选甲", "错误候选乙"],
                "rejudge_verdict": "NO",
                "rejudge_reason": "human_rejected_name",
                "rejudge_method": "human_negative_exact",
                "counted_is_detected": False,
            }
            cursor.execute(
                "SELECT identity_review_state FROM public.monitoring_results WHERE id = %s",
                (multi_row["id"],),
            )
            assert cursor.fetchone()["identity_review_state"] == "rejected"
        conn.close()

        # Rollback may remove unused additive objects, but never durable review evidence.
        conn = connect()
        try:
            with conn.cursor() as cursor:
                with pytest.raises(psycopg2.Error, match="rollback blocked"):
                    cursor.execute(rollback_sql)
            conn.rollback()
            with conn, conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM public.monitoring_identity_decision_events"
                )
                assert cursor.fetchone()["count"] == 4
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM public.monitoring_results "
                    "WHERE identity_review_state <> 'not_required'"
                )
                assert cursor.fetchone()["count"] == 5
        finally:
            conn.close()

        # ------------------------------------------------------------------
        # [R4 · P1-2] 诊断泛化 rollback fail-closed 矩阵（真实 PG16）
        # ------------------------------------------------------------------
        # ⑤ 零 diagnosis 耐久事件 → 逆 additive 回滚成功（monitoring 事件不受影响）
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(diagnosis_rollback_sql)
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                  FROM pg_catalog.pg_attribute
                 WHERE attrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
                   AND attname IN ('source_kind', 'source_result_id', 'tenant_id', 'ip', 'reason')
                   AND NOT attisdropped
                """
            )
            assert cursor.fetchone()["count"] == 0, "回滚后泛化列必须已移除"
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                  FROM pg_catalog.pg_constraint
                 WHERE conrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
                   AND conname IN ('chk_monitoring_identity_event_source',
                                   'chk_monitoring_identity_event_source_kind')
                """
            )
            assert cursor.fetchone()["count"] == 0, "回滚后泛化 CHECK 必须已移除"
            cursor.execute(
                "SELECT COUNT(*) AS count FROM public.monitoring_identity_decision_events"
            )
            assert cursor.fetchone()["count"] == 4, "monitoring 旧事件不得受影响"
            # [R5 · 复审 P1] 回滚必须恢复 result_id NOT NULL——否则下一次 prestart
            # 按 manifest 顺序先重放旧监测迁移时会因 result_id 契约漂移 abort
            cursor.execute(
                """
                SELECT a.attnotnull
                  FROM pg_catalog.pg_attribute a
                 WHERE a.attrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
                   AND a.attname = 'result_id' AND NOT a.attisdropped
                """
            )
            assert cursor.fetchone()["attnotnull"] is True, (
                "回滚后 result_id 必须恢复 NOT NULL（prestart 重放旧监测迁移的前置）"
            )
        conn.close()

        # ⑤b 回滚幂等：连续第二次执行同样安全（列/约束已缺 → 跳过）
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(diagnosis_rollback_sql)
        conn.close()

        # ⑥ [R5 · 复审 P1] 按真实 manifest 顺序重放：先旧监测迁移（其 result_id
        #    NOT NULL 契约依赖回滚已恢复非空），再诊断迁移，最后 readiness
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(migration_sql)
            cursor.execute(diagnosis_migration_sql)
            monitoring_db.assert_monitoring_identity_review_ready(cursor)
        conn.close()

        # ⑦ 写入一条 source_kind='diagnosis' 耐久事件（人工确认审计血缘）
        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO public.monitoring_identity_decision_events
                    (result_id, brand_id, action, selected_name, normalized_name,
                     evidence_hash, result_version_before, result_version_after,
                     actor_user_id, request_id, metadata,
                     source_kind, source_result_id, tenant_id, ip, reason)
                VALUES (NULL, %s, 'yes', '诊断候选名', '诊断候选名',
                        'dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd',
                        0, 1, 7001, %s, '{"source": "diagnosis_human_review"}'::jsonb,
                        'diagnosis', 424242, 7001, '203.0.113.7', '人工确认诊断品牌')
                """,
                (brand_id, str(uuid.uuid4())),
            )
            cursor.execute(
                "SELECT COUNT(*) AS count FROM public.monitoring_identity_name_decisions "
                "WHERE brand_id = %s",
                (brand_id,),
            )
            name_decisions_before = cursor.fetchone()["count"]
        conn.close()

        # ⑧ rollback 必须稳定 fail-closed（稳定错误码；整体回滚）
        conn = connect()
        try:
            with conn.cursor() as cursor:
                with pytest.raises(
                    psycopg2.Error,
                    match="GEO_DIAGNOSIS_IDENTITY_ROLLBACK_BLOCKED_DURABLE_EVENTS",
                ):
                    cursor.execute(diagnosis_rollback_sql)
            conn.rollback()
            # ⑨ 失败后事件行、列、约束完整保留；readiness 仍通过
            with conn, conn.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM public.monitoring_identity_decision_events "
                    "WHERE source_kind = 'diagnosis'"
                )
                assert cursor.fetchone()["count"] == 1, "diagnosis 耐久事件必须完整保留"
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                      FROM pg_catalog.pg_attribute
                     WHERE attrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
                       AND attname IN ('source_kind', 'source_result_id', 'tenant_id', 'ip', 'reason')
                       AND NOT attisdropped
                    """
                )
                assert cursor.fetchone()["count"] == 5, "泛化列必须完整保留"
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                      FROM pg_catalog.pg_constraint
                     WHERE conrelid = 'public.monitoring_identity_decision_events'::pg_catalog.regclass
                       AND conname IN ('chk_monitoring_identity_event_source',
                                       'chk_monitoring_identity_event_source_kind')
                    """
                )
                assert cursor.fetchone()["count"] == 2, "泛化 CHECK 必须完整保留"
                # ⑩ monitoring 旧事件与 alias SSOT 不受影响
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM public.monitoring_identity_decision_events "
                    "WHERE source_kind = 'monitoring'"
                )
                assert cursor.fetchone()["count"] == 4, "monitoring 旧事件不得受影响"
                cursor.execute(
                    "SELECT COUNT(*) AS count FROM public.monitoring_identity_name_decisions "
                    "WHERE brand_id = %s",
                    (brand_id,),
                )
                assert cursor.fetchone()["count"] == name_decisions_before, "alias SSOT 不得受影响"
                monitoring_db.assert_monitoring_identity_review_ready(cursor)
        finally:
            conn.close()
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (database_name,),
                )
                cursor.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(database_name)))
        finally:
            admin.close()
