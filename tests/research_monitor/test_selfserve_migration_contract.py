"""自助调研 prestart schema 判别：三值老库升级、真实 round 写入与四维目录反查。"""

from pathlib import Path

import psycopg2
import pytest

from services.research_monitor.round_state import create_round_with_snapshot


ROOT = Path(__file__).resolve().parents[2]
BASE_MIGRATION = ROOT / 'scripts/migration_geo_research_monitor.sql'
SELFSERVE_MIGRATION = ROOT / 'scripts/migration_geo_research_selfserve_2026_07_05.sql'


def _apply_selfserve_twice(pg_conn) -> None:
    sql = SELFSERVE_MIGRATION.read_text(encoding='utf-8')
    pg_conn.rollback()
    pg_conn.autocommit = True
    cur = pg_conn.cursor()
    cur.execute(sql)
    cur.execute(sql)


def _create_selfserve_round() -> str:
    return create_round_with_snapshot(
        'selfserve',
        [{'id': 1, 'name': '测试行业', 'slug': 'test-industry'}],
        {'1': [{'id': 1, 'text': '测试问题'}]},
        triggered_user_id=9001,
    )


def test_fresh_create_and_manifest_contract_are_selfserve_ready():
    base_sql = BASE_MIGRATION.read_text(encoding='utf-8')
    migration_sql = SELFSERVE_MIGRATION.read_text(encoding='utf-8')
    assert "('cron', 'manual', 'missed_cron_recovery', 'selfserve')" in base_sql
    assert '已验证 blob 45973f82c3262b20e9e4ceb40cde0d3f4216960b' in migration_sql
    assert 'c.convalidated = TRUE' in migration_sql

    from db.migration_manifest import MIGRATIONS

    expected = [
        'scripts/migration_geo_research_monitor.sql',
        'scripts/migration_geo_research_selfserve_2026_07_05.sql',
        'scripts/migration_research_round_concurrency_autoresume_2026_07_16.sql',
    ]
    assert [item for item in MIGRATIONS if item in expected] == expected


@pytest.mark.usefixtures('clean_research_tables')
class TestSelfserveMigrationPostgres:

    def test_migration_twice_allows_runtime_selfserve_round(self, pg_conn):
        _apply_selfserve_twice(pg_conn)
        round_id = _create_selfserve_round()

        cur = pg_conn.cursor()
        cur.execute(
            "SELECT triggered_by, triggered_user_id FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        assert cur.fetchone() == {'triggered_by': 'selfserve', 'triggered_user_id': 9001}

    def test_old_three_value_check_upgrades_without_half_write(self, pg_conn):
        pg_conn.rollback()
        pg_conn.autocommit = True
        cur = pg_conn.cursor()
        cur.execute(
            "ALTER TABLE geo_research_round "
            "DROP CONSTRAINT IF EXISTS geo_research_round_triggered_by_check"
        )
        cur.execute(
            "ALTER TABLE geo_research_round "
            "ADD CONSTRAINT geo_research_round_triggered_by_check "
            "CHECK (triggered_by IN ('cron', 'manual', 'missed_cron_recovery'))"
        )
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO geo_research_round "
                "(round_id, batch_id, triggered_by, status) "
                "VALUES ('pre_upgrade_selfserve', 'batch_pre_upgrade', 'selfserve', 'pending')"
            )

        _apply_selfserve_twice(pg_conn)
        round_id = _create_selfserve_round()
        cur.execute(
            "SELECT triggered_by FROM geo_research_round WHERE round_id = %s",
            (round_id,),
        )
        assert cur.fetchone()['triggered_by'] == 'selfserve'

    def test_queue_and_alias_schema_four_dimensions(self, pg_conn):
        _apply_selfserve_twice(pg_conn)
        cur = pg_conn.cursor()

        cur.execute(
            """
            SELECT table_name, column_name, udt_name, is_nullable, column_default
              FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name IN (
                   'geo_research_selfserve_queue', 'geo_research_industry_aliases'
               )
            """
        )
        columns = {
            (row['table_name'], row['column_name']): row
            for row in cur.fetchall()
        }
        expected_columns = {
            'geo_research_selfserve_queue': {
                'id': ('int8', 'NO'), 'user_id': ('int8', 'NO'),
                'brand_id': ('int8', 'YES'), 'industry_raw': ('text', 'NO'),
                'industry_id': ('int8', 'YES'), 'industry_key': ('text', 'YES'),
                'round_id': ('varchar', 'YES'), 'freeze_id': ('int8', 'YES'),
                'freeze_table': ('varchar', 'YES'), 'billing_exempt': ('bool', 'YES'),
                'price_points': ('int4', 'NO'), 'prompt_snapshot': ('jsonb', 'YES'),
                'status': ('varchar', 'NO'), 'failed_reason': ('text', 'YES'),
                'idempotency_key': ('text', 'NO'), 'created_at': ('timestamptz', 'YES'),
                'started_at': ('timestamptz', 'YES'), 'finished_at': ('timestamptz', 'YES'),
            },
            'geo_research_industry_aliases': {
                'id': ('int8', 'NO'), 'normalized_alias': ('text', 'NO'),
                'industry_id': ('int8', 'NO'), 'confidence': ('numeric', 'YES'),
                'resolved_by': ('varchar', 'NO'), 'reviewed_by': ('int8', 'YES'),
                'active': ('bool', 'YES'), 'created_at': ('timestamptz', 'YES'),
                'updated_at': ('timestamptz', 'YES'),
            },
        }
        for table, expected in expected_columns.items():
            actual_names = {
                column for (actual_table, column) in columns if actual_table == table
            }
            assert actual_names == set(expected)
            for column, (udt_name, nullable) in expected.items():
                row = columns[(table, column)]
                assert (row['udt_name'], row['is_nullable']) == (udt_name, nullable)

        assert columns[('geo_research_selfserve_queue', 'status')]['column_default'] == "'pending'::character varying"
        assert columns[('geo_research_selfserve_queue', 'billing_exempt')]['column_default'] == 'false'
        assert columns[('geo_research_industry_aliases', 'active')]['column_default'] == 'true'

        cur.execute(
            """
            SELECT c.conname, c.contype, c.convalidated, pg_get_constraintdef(c.oid) AS definition
              FROM pg_constraint c
             WHERE c.conrelid IN (
                 'geo_research_round'::regclass,
                 'geo_research_selfserve_queue'::regclass,
                 'geo_research_industry_aliases'::regclass
             )
            """
        )
        constraints = {row['conname']: row for row in cur.fetchall()}
        for name in (
            'geo_research_round_triggered_by_check',
            'geo_research_selfserve_queue_status_check',
            'geo_research_industry_aliases_resolved_by_check',
        ):
            assert constraints[name]['contype'] == 'c'
            assert constraints[name]['convalidated'] is True
        assert constraints['geo_research_selfserve_queue_pkey']['contype'] == 'p'
        assert constraints['geo_research_industry_aliases_pkey']['contype'] == 'p'
        assert constraints['geo_research_industry_aliases_normalized_alias_key']['contype'] == 'u'
        assert constraints['geo_research_industry_aliases_industry_id_fkey']['contype'] == 'f'

        cur.execute(
            """
            SELECT indexname, indexdef
              FROM pg_indexes
             WHERE schemaname = current_schema()
               AND tablename IN (
                   'geo_research_selfserve_queue', 'geo_research_industry_aliases'
               )
            """
        )
        indexes = {row['indexname']: row['indexdef'] for row in cur.fetchall()}
        for name in (
            'uq_selfserve_active_idem', 'idx_selfserve_user',
            'idx_selfserve_industry_key', 'idx_selfserve_status',
            'idx_selfserve_alias_industry', 'idx_selfserve_alias_active',
        ):
            assert name in indexes
        assert 'UNIQUE' in indexes['uq_selfserve_active_idem']
        assert "status)::text = ANY" in indexes['uq_selfserve_active_idem']
        assert 'WHERE active' in indexes['idx_selfserve_alias_active']
