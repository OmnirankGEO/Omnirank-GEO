from __future__ import annotations

import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
PG_URL = os.environ.get("MONITORING_SCHEDULER_PG_TEST_URL")


@pytest.mark.skipif(
    not PG_URL,
    reason="set MONITORING_SCHEDULER_PG_TEST_URL for the real PostgreSQL contract",
)
def test_real_cold_start_product_matrix_migration_and_rollback(monkeypatch):
    import psycopg2
    from psycopg2 import sql
    from psycopg2.extras import RealDictCursor

    database_name = f"monitor_product_{uuid.uuid4().hex[:12]}"
    admin = psycopg2.connect(PG_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cursor:
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database_name)))

        parsed = urlsplit(PG_URL)
        target_url = urlunsplit(
            (parsed.scheme, parsed.netloc, f"/{database_name}", parsed.query, parsed.fragment)
        )
        monkeypatch.setenv("DATABASE_URL", target_url)
        from db import connection as connection_db

        monkeypatch.setattr(connection_db, "DATABASE_URL", target_url)
        monkeypatch.setattr(connection_db, "_pool", None)
        from db import diagnosis_db, monitoring_db

        def connect():
            return psycopg2.connect(
                target_url,
                cursor_factory=RealDictCursor,
            )

        monkeypatch.setattr(diagnosis_db, "get_connection", connect)
        monkeypatch.setattr(monitoring_db, "get_connection", connect)

        # Use the repository's real cold-start owners. The test does not invent
        # confirmed_keywords.platforms or any production-only shortcut column.
        diagnosis_db.init_db()
        monitoring_db.init_monitoring_tables()

        conn = connect()
        with conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                 FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'confirmed_keywords'
                   AND column_name = 'platforms'
                """
            )
            assert cursor.fetchone()["count"] == 0

            cursor.execute(
                "INSERT INTO brands (name) VALUES ('matrix contract brand') RETURNING id"
            )
            brand_id = cursor.fetchone()["id"]
            cursor.execute(
                """
                INSERT INTO quotes (brand_id, brand_name, status, paid_at)
                VALUES (%s, 'matrix contract brand', 'paid', NOW())
                RETURNING id
                """,
                (brand_id,),
            )
            quote_id = cursor.fetchone()["id"]
            cursor.execute(
                """
                INSERT INTO confirmed_keywords (quote_id, keyword)
                VALUES (%s, 'historical paid phrase')
                RETURNING id
                """,
                (quote_id,),
            )
            confirmed_id = cursor.fetchone()["id"]
            cursor.execute(
                """
                INSERT INTO client_keywords
                    (client_id, brand_id, keyword, target_brand, platforms)
                VALUES
                    ('default-row', %s, 'default client', 'matrix contract brand',
                     'dashscope,deepseek,kimi,doubao'),
                    ('custom-row', %s, 'custom client', 'matrix contract brand',
                     'deepseek')
                """,
                (brand_id, brand_id),
            )
            cursor.execute(
                """
                INSERT INTO extra_keywords
                    (client_id, brand_id, keyword, target_brand, platforms)
                VALUES
                    ('default-extra', %s, 'default extra', 'matrix contract brand',
                     'dashscope,deepseek,kimi,doubao'),
                    ('custom-extra', %s, 'custom extra', 'matrix contract brand',
                     'doubao')
                """,
                (brand_id, brand_id),
            )
            cursor.execute(
                """
                INSERT INTO monitoring_config (client_id, brand_id, default_platforms)
                VALUES
                    ('default-config', %s, 'dashscope,deepseek,kimi,doubao'),
                    ('custom-config', %s, 'kimi,deepseek')
                """,
                (brand_id, brand_id),
            )

            old_sql = (
                ROOT / "scripts" / "migration_monitoring_yuanbao_default_2026_07_20.sql"
            ).read_text(encoding="utf-8")
            new_sql = (
                ROOT / "scripts" / "migration_monitoring_product_matrix_2026_07_21.sql"
            ).read_text(encoding="utf-8")
            rollback_sql = (
                ROOT / "scripts" / "rollback_monitoring_product_matrix_2026_07_21.sql"
            ).read_text(encoding="utf-8")

            cursor.execute(old_sql)
            # Same value without the retired migration's row-level backup is
            # ambiguous customer data. The new migration must not rewrite it.
            cursor.execute(
                """
                INSERT INTO client_keywords
                    (client_id, brand_id, keyword, target_brand, platforms)
                VALUES ('ambiguous-row', %s, 'ambiguous client',
                        'matrix contract brand', 'dashscope,deepseek,doubao,yuanbao');
                INSERT INTO extra_keywords
                    (client_id, brand_id, keyword, target_brand, platforms)
                VALUES ('ambiguous-extra', %s, 'ambiguous extra',
                        'matrix contract brand', 'dashscope,deepseek,doubao,yuanbao');
                INSERT INTO monitoring_config
                    (client_id, brand_id, default_platforms)
                VALUES ('ambiguous-config', %s,
                        'dashscope,deepseek,doubao,yuanbao');
                """,
                (brand_id, brand_id, brand_id),
            )
            cursor.execute(new_sql)
            cursor.execute(new_sql)  # migration 2x

            with pytest.raises(RuntimeError, match="ambiguous retired monitoring defaults"):
                monitoring_db.assert_monitoring_product_matrix_ready(cursor)

            cursor.execute(
                """
                UPDATE client_keywords SET platforms = 'yuanbao'
                 WHERE client_id = 'ambiguous-row';
                UPDATE extra_keywords SET platforms = 'yuanbao'
                 WHERE client_id = 'ambiguous-extra';
                UPDATE monitoring_config SET default_platforms = 'yuanbao'
                 WHERE client_id = 'ambiguous-config';
                INSERT INTO client_keywords
                    (client_id, brand_id, keyword, target_brand)
                VALUES ('new-default-row', %s, 'new default client',
                        'matrix contract brand');
                INSERT INTO extra_keywords
                    (client_id, brand_id, keyword, target_brand)
                VALUES ('new-default-extra', %s, 'new default extra',
                        'matrix contract brand');
                INSERT INTO monitoring_config (client_id, brand_id)
                VALUES ('new-default-config', %s);
                """,
                (brand_id, brand_id, brand_id),
            )
            monitoring_db.assert_monitoring_product_matrix_ready(cursor)

            # Same-name objects earlier in search_path, including pg_temp,
            # must not redirect migration or runtime entitlement reads.
            cursor.execute("CREATE SCHEMA monitoring_contract_lure")
            cursor.execute(
                """
                CREATE TABLE monitoring_contract_lure.monitoring_product_platform_matrices (
                    version TEXT PRIMARY KEY,
                    platforms TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                INSERT INTO monitoring_contract_lure.monitoring_product_platform_matrices
                    (version, platforms)
                VALUES ('monitoring-classic4-v1', 'yuanbao');
                CREATE OR REPLACE FUNCTION
                    monitoring_contract_lure.reject_monitoring_product_matrix_mutation()
                RETURNS TRIGGER LANGUAGE plpgsql AS $$
                BEGIN
                    RETURN NEW;
                END;
                $$;
                CREATE TEMP TABLE monitoring_product_platform_matrices (
                    version TEXT PRIMARY KEY,
                    platforms TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                INSERT INTO pg_temp.monitoring_product_platform_matrices
                    (version, platforms)
                VALUES ('monitoring-classic4-v1', 'yuanbao');
                SET LOCAL search_path = monitoring_contract_lure, pg_temp, public;
                """
            )
            cursor.execute(new_sql)
            monitoring_db.assert_monitoring_product_matrix_ready(cursor)

            cursor.execute("SAVEPOINT early_return_function_probe")
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION
                    public.reject_confirmed_monitoring_product_rebind()
                RETURNS TRIGGER LANGUAGE plpgsql AS $$
                BEGIN
                    RETURN NEW;
                    IF OLD.monitoring_product_version IS DISTINCT FROM
                       NEW.monitoring_product_version THEN
                        RAISE EXCEPTION
                            'confirmed monitoring product version is immutable';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
            with pytest.raises(RuntimeError, match="function drift"):
                monitoring_db.assert_monitoring_product_matrix_ready(cursor)
            cursor.execute("ROLLBACK TO SAVEPOINT early_return_function_probe")
            cursor.execute("RELEASE SAVEPOINT early_return_function_probe")

            cursor.execute("SAVEPOINT wrong_column_type_probe")
            cursor.execute(
                """
                ALTER TABLE public.monitoring_product_platform_matrices
                    ALTER COLUMN platforms TYPE VARCHAR(200)
                """
            )
            with pytest.raises(RuntimeError, match="columns drifted"):
                monitoring_db.assert_monitoring_product_matrix_ready(cursor)
            cursor.execute("ROLLBACK TO SAVEPOINT wrong_column_type_probe")
            cursor.execute("RELEASE SAVEPOINT wrong_column_type_probe")

            cursor.execute("SAVEPOINT weak_primary_key_probe")
            cursor.execute(
                """
                ALTER TABLE public.confirmed_keywords
                    DROP CONSTRAINT fk_confirmed_keywords_monitoring_product_version;
                ALTER TABLE public.monitoring_product_platform_matrices
                    DROP CONSTRAINT monitoring_product_platform_matrices_pkey;
                ALTER TABLE public.monitoring_product_platform_matrices
                    ADD CONSTRAINT monitoring_product_platform_matrices_pkey
                    PRIMARY KEY (platforms);
                """
            )
            with pytest.raises(RuntimeError, match="PK drifted"):
                monitoring_db.assert_monitoring_product_matrix_ready(cursor)
            cursor.execute("ROLLBACK TO SAVEPOINT weak_primary_key_probe")
            cursor.execute("RELEASE SAVEPOINT weak_primary_key_probe")

            cursor.execute(
                """
                SELECT monitoring_product_version
                  FROM confirmed_keywords
                 WHERE id = %s
                """,
                (confirmed_id,),
            )
            assert cursor.fetchone()["monitoring_product_version"] == (
                monitoring_db.DEFAULT_MONITORING_PRODUCT_VERSION
            )

            cursor.execute(
                "SELECT client_id, platforms FROM client_keywords ORDER BY client_id"
            )
            assert {row["client_id"]: row["platforms"] for row in cursor.fetchall()} == {
                "ambiguous-row": "yuanbao",
                "custom-row": "deepseek",
                "default-row": "dashscope,deepseek,kimi,doubao",
                "new-default-row": "dashscope,deepseek,kimi,doubao",
            }
            cursor.execute(
                "SELECT client_id, platforms FROM extra_keywords ORDER BY client_id"
            )
            assert {row["client_id"]: row["platforms"] for row in cursor.fetchall()} == {
                "ambiguous-extra": "yuanbao",
                "custom-extra": "doubao",
                "default-extra": "dashscope,deepseek,kimi,doubao",
                "new-default-extra": "dashscope,deepseek,kimi,doubao",
            }
            cursor.execute(
                "SELECT client_id, default_platforms FROM monitoring_config "
                "WHERE client_id IN ('default-config', 'custom-config', 'new-default-config') "
                "ORDER BY client_id"
            )
            assert {
                row["client_id"]: row["default_platforms"]
                for row in cursor.fetchall()
            } == {
                "custom-config": "kimi,deepseek",
                "default-config": "dashscope,deepseek,kimi,doubao",
                "new-default-config": "dashscope,deepseek,kimi,doubao",
            }

            cursor.execute("SAVEPOINT matrix_mutation_probe")
            with pytest.raises(psycopg2.Error, match="append-only"):
                cursor.execute(
                    """
                    UPDATE public.monitoring_product_platform_matrices
                       SET platforms = 'deepseek'
                     WHERE version = 'monitoring-classic4-v1'
                    """
                )
            cursor.execute("ROLLBACK TO SAVEPOINT matrix_mutation_probe")
            cursor.execute("RELEASE SAVEPOINT matrix_mutation_probe")

            cursor.execute("SAVEPOINT confirmed_rebind_probe")
            with pytest.raises(psycopg2.Error, match="immutable"):
                cursor.execute(
                    """
                    UPDATE public.confirmed_keywords
                       SET monitoring_product_version = 'not-a-product'
                     WHERE id = %s
                    """,
                    (confirmed_id,),
                )
            cursor.execute("ROLLBACK TO SAVEPOINT confirmed_rebind_probe")
            cursor.execute("RELEASE SAVEPOINT confirmed_rebind_probe")

            cursor.execute(rollback_sql)
            cursor.execute(new_sql)  # rollback -> forward
            monitoring_db.assert_monitoring_product_matrix_ready(cursor)

            cursor.execute(
                """
                SELECT column_default
                 FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = 'monitoring_tasks'
                   AND column_name = 'platform_count'
                """
            )
            assert cursor.fetchone()["column_default"] == "0"

            # Commit the migrated contract before the business helper opens its
            # own production-style connection.
            conn.commit()

            task_id = monitoring_db.create_monitoring_task(
                brand_id=brand_id,
                keyword_ids=[confirmed_id, confirmed_id + 1],
                planned_test_count=2,
                planned_platform_count=2,
                trigger_type="scheduled",
            )
            cursor.execute(
                """
                SELECT keyword_count, platform_count, total_tests
                  FROM monitoring_tasks
                 WHERE id = %s
                """,
                (task_id,),
            )
            assert dict(cursor.fetchone()) == {
                "keyword_count": 2,
                "platform_count": 2,
                "total_tests": 2,
            }

            assert monitoring_db.update_task_status(
                task_id,
                "completed",
                completed_tests=2,
            )
            cursor.execute(
                "SELECT status, completed_tests, total_tests FROM monitoring_tasks WHERE id = %s",
                (task_id,),
            )
            assert dict(cursor.fetchone()) == {
                "status": "completed",
                "completed_tests": 2,
                "total_tests": 2,
            }
    finally:
        try:
            with admin.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT pg_terminate_backend(pid)
                      FROM pg_stat_activity
                     WHERE datname = %s
                       AND pid <> pg_backend_pid()
                    """,
                    (database_name,),
                )
                cursor.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(
                        sql.Identifier(database_name)
                    )
                )
        finally:
            admin.close()
