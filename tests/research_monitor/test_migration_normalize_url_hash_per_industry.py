"""
P14.2 C2 · migration_normalize_url_hash_per_industry.sql 测试

锁两件事:
  1. Migration DDL 部分用 IF EXISTS / IF NOT EXISTS 兜底 idempotent (inspect SQL 文件)
  2. Backfill 部分: 跨行业 citation 全部被迁移到对应行业 dup article 上 · 重跑无副作用

注: 测试库由 conftest.setup_test_db 已切换到 "post-migration" 复合 UNIQUE 状态 ·
    本测试只验 backfill SQL · DDL 部分用 inspect 锁

不动 prod · 不动 .env · 跑在 TEST_DATABASE_URL 上
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MIGRATION_PATH = ROOT / "scripts" / "migration_normalize_url_hash_per_industry.sql"


# ============================================================
# Part 1: DDL inspect · 锁 idempotent 关键字
# ============================================================

class TestMigrationDDLIdempotent:

    def test_migration_file_exists(self):
        assert MIGRATION_PATH.exists(), \
            f"P14.2 C2 migration 文件必须存在: {MIGRATION_PATH}"

    def test_migration_wrapped_in_transaction(self):
        sql = MIGRATION_PATH.read_text(encoding='utf-8')
        assert "BEGIN;" in sql and "COMMIT;" in sql, \
            "Migration 必须用 BEGIN/COMMIT 事务包裹 · 失败可回滚"

    def test_migration_drops_old_uniques_idempotently(self):
        sql = MIGRATION_PATH.read_text(encoding='utf-8')
        # 必须用 IF EXISTS 兜底重跑
        assert "IF EXISTS" in sql, "DROP CONSTRAINT 必须 IF EXISTS 兜底重跑"
        for old_name in [
            'geo_research_articles_url_hash_key',
            'geo_research_articles_url_key',
        ]:
            assert old_name in sql, f"必须 DROP 旧约束 {old_name}"

    def test_migration_adds_new_composite_uniques_idempotently(self):
        sql = MIGRATION_PATH.read_text(encoding='utf-8')
        assert "IF NOT EXISTS" in sql, "ADD CONSTRAINT 必须 IF NOT EXISTS 兜底重跑"
        for new_name in [
            'geo_research_articles_url_hash_industry_key',
            'geo_research_articles_url_industry_key',
        ]:
            assert new_name in sql, f"必须 ADD 新复合约束 {new_name}"

    def test_migration_handles_null_primary_industry_first(self):
        sql = MIGRATION_PATH.read_text(encoding='utf-8')
        # UPDATE 必须在 ADD UNIQUE 之前 (NULL 行 UNIQUE 行为不可预期 · 先填 'unknown')
        upd_idx = sql.find("UPDATE geo_research_articles\n   SET primary_industry = 'unknown'")
        add_uniq_idx = sql.find("ADD CONSTRAINT geo_research_articles_url_hash_industry_key")
        assert upd_idx != -1, "必须先 UPDATE NULL/空 primary_industry 兜底 'unknown'"
        assert upd_idx < add_uniq_idx, \
            "NULL primary_industry UPDATE 必须在 ADD UNIQUE 之前"

    def test_migration_uses_distinct_on_in_backfill(self):
        sql = MIGRATION_PATH.read_text(encoding='utf-8')
        # 老板规格 B: backfill 必须通用 SQL · 不能按 5 条写死
        assert "DISTINCT ON (a.url_hash, i.name)" in sql, \
            "backfill INSERT 必须 DISTINCT ON 防同 (url_hash, industry) 多 cite 行重复 INSERT"
        assert "NOT EXISTS" in sql, \
            "backfill INSERT 必须 NOT EXISTS 兜底重跑 idempotent"

    def test_migration_recomputes_total_citation_count(self):
        sql = MIGRATION_PATH.read_text(encoding='utf-8')
        assert "total_citation_count" in sql, \
            "backfill 后必须重算 total_citation_count (citation 迁移导致老 article 计数减)"


# ============================================================
# Part 2: Backfill 真跑 · 锁数据效果 + idempotent
# ============================================================

class TestMigrationBackfillFunctional:
    """真在 test DB 上跑 backfill SQL · 验证迁移效果"""

    @pytest.fixture
    def seeded_cross_industry_state(self, pg_conn, clean_research_tables):
        """造 pre-migration 状态: 行业A 抓了 1 篇 · 行业B 引用了该篇 · citation industry != article.primary"""
        cur = pg_conn.cursor()

        # 行业 A B
        cur.execute("""
            INSERT INTO geo_research_industries (name, slug, sort_order, active)
            VALUES ('行业A', 'ind_a', 1, TRUE), ('行业B', 'ind_b', 2, TRUE)
            RETURNING id, name
        """)
        rows = cur.fetchall()
        ind_a_id = next(r['id'] for r in rows if r['name'] == '行业A')
        ind_b_id = next(r['id'] for r in rows if r['name'] == '行业B')

        # prompts
        cur.execute("""
            INSERT INTO geo_research_prompts (industry_id, prompt_text, sort_order, active)
            VALUES (%s, %s, 0, TRUE), (%s, %s, 0, TRUE)
            RETURNING id
        """, (ind_a_id, '行业A prompt', ind_b_id, '行业B prompt'))
        prompts = cur.fetchall()
        prompt_a_id = prompts[0]['id']
        prompt_b_id = prompts[1]['id']

        # 2 个 round: 行业A 的 (首抓) 和 行业B 的 (跨引 · 用于验证 first_seen_round_id)
        round_a = 'round_test_backfill_a'
        round_b = 'round_test_backfill_b'
        cur.execute("""
            INSERT INTO geo_research_round (round_id, status, triggered_by, snapshot_json,
                                            started_at, finished_at, batch_id)
            VALUES (%s, 'completed', 'manual', '{}'::jsonb, NOW(), NOW(), %s),
                   (%s, 'completed', 'manual', '{}'::jsonb, NOW(), NOW(), %s)
        """, (round_a, f'batch_{round_a}', round_b, f'batch_{round_b}'))

        # article (primary=行业A · 1 篇)
        cur.execute("""
            INSERT INTO geo_research_articles
                (url, url_hash, domain, title,
                 oss_key_raw, raw_char_count, content_hash,
                 domain_tier, content_type, clean_status, review_status,
                 primary_industry, is_duplicate, total_citation_count,
                 first_seen_round_id, last_seen_at, fetched_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'pending', 'crawled',
                    '行业A', FALSE, 2, %s, NOW(), NOW())
            RETURNING id
        """, ('https://x.com/backfill', 'urlhash_backfill_001', 'x.com', 'T',
              'k.md', 2000, 'ch001', 'whitelist', 'article', round_a))
        article_a_id = cur.fetchone()['id']

        # 2 条 citation: 行业A 自引 (round_a) + 行业B 跨引 (round_b · 老板修后 backfill 用此 round)
        cur.execute("""
            INSERT INTO geo_research_article_citations
                (article_id, round_id, industry_id, prompt_id, platform, cited_at)
            VALUES (%s, %s, %s, %s, 'doubao', NOW() - INTERVAL '1 hour'),
                   (%s, %s, %s, %s, 'kimi', NOW())
        """, (
            article_a_id, round_a, ind_a_id, prompt_a_id,
            article_a_id, round_b, ind_b_id, prompt_b_id,  # round_b · 测 backfill 用 c.round_id
        ))
        pg_conn.commit()
        return {
            'ind_a_id': ind_a_id, 'ind_b_id': ind_b_id,
            'article_a_id': article_a_id,
            'round_a': round_a, 'round_b': round_b,
            'url_hash': 'urlhash_backfill_001',
        }

    def _run_backfill_only(self, pg_conn):
        """跑 migration 的 backfill 部分 (DDL 部分 setup_test_db 已搞 · 这里只跑数据迁移)"""
        sql = MIGRATION_PATH.read_text(encoding='utf-8')
        # 抽 backfill 段: INSERT geo_research_articles ... + UPDATE citations + UPDATE total_citation_count
        # 简化: 整 SQL 跑 (DDL 部分 IF EXISTS / IF NOT EXISTS 兜底 · 重跑不报错)
        cur = pg_conn.cursor()
        cur.execute(sql)
        pg_conn.commit()

    def test_backfill_creates_dup_article_for_cross_industry_citation(
        self, pg_conn, seeded_cross_industry_state,
    ):
        """跑 migration 后 · 必须为"行业B"创建 dup article + citation 已迁移"""
        s = seeded_cross_industry_state
        self._run_backfill_only(pg_conn)

        cur = pg_conn.cursor()
        # 1. 现在应有 2 个 article (行业A primary + 行业B dup)
        cur.execute("""
            SELECT id, primary_industry, is_duplicate, primary_article_id,
                   total_citation_count, first_seen_round_id
              FROM geo_research_articles WHERE url_hash = %s
             ORDER BY id ASC
        """, (s['url_hash'],))
        arts = cur.fetchall()
        assert len(arts) == 2, f"backfill 后应 2 行 (行业A + 行业B) · 实际 {len(arts)}"

        a = next(a for a in arts if a['primary_industry'] == '行业A')
        b = next(a for a in arts if a['primary_industry'] == '行业B')
        assert a['is_duplicate'] is False
        assert b['is_duplicate'] is True
        assert b['primary_article_id'] == a['id']
        # 老板复核 fix · backfill dup article 的 first_seen_round_id 必须用 c.round_id (行业B 首次引用 round)
        # 不是沿用 a.first_seen_round_id (会让 dup 在"行业A 那一轮"视图里露 · 视觉错位)
        assert a['first_seen_round_id'] == s['round_a'], \
            "首篇 article first_seen_round_id 仍是 round_a (没被 backfill 改)"
        assert b['first_seen_round_id'] == s['round_b'], \
            f"dup article first_seen_round_id 必须 = 行业B 首次 citation 的 round_b ·" \
            f" 实际 {b['first_seen_round_id']!r} (期望 {s['round_b']!r})"

        # 2. 行业B 的 citation 已迁移到 dup article
        cur.execute("""
            SELECT c.article_id, i.name AS industry_name
              FROM geo_research_article_citations c
              JOIN geo_research_industries i ON i.id = c.industry_id
        """)
        cites = cur.fetchall()
        assert len(cites) == 2
        for c in cites:
            if c['industry_name'] == '行业A':
                assert c['article_id'] == a['id'], "行业A 自引仍指向 primary article"
            else:
                assert c['article_id'] == b['id'], "行业B 跨引必须迁到 dup article"

        # 3. total_citation_count 已重算 · 各 1 条
        assert a['total_citation_count'] == 1
        assert b['total_citation_count'] == 1

    def test_backfill_is_idempotent(self, pg_conn, seeded_cross_industry_state):
        """重跑 migration 不应再新增 dup / 不应再迁 citation / 计数不变"""
        s = seeded_cross_industry_state
        self._run_backfill_only(pg_conn)

        cur = pg_conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM geo_research_articles WHERE url_hash=%s",
                    (s['url_hash'],))
        cnt_after_first = cur.fetchone()['c']
        cur.execute("SELECT array_agg(article_id ORDER BY id) AS ids FROM geo_research_article_citations")
        cite_ids_after_first = cur.fetchone()['ids']

        # 第二次跑
        self._run_backfill_only(pg_conn)

        cur.execute("SELECT COUNT(*) AS c FROM geo_research_articles WHERE url_hash=%s",
                    (s['url_hash'],))
        cnt_after_second = cur.fetchone()['c']
        cur.execute("SELECT array_agg(article_id ORDER BY id) AS ids FROM geo_research_article_citations")
        cite_ids_after_second = cur.fetchone()['ids']

        assert cnt_after_second == cnt_after_first, \
            f"重跑 migration 不应新增 article · 第一次 {cnt_after_first} 第二次 {cnt_after_second}"
        assert cite_ids_after_second == cite_ids_after_first, \
            "重跑 migration 不应改 citation article_id 映射"

    def test_backfill_handles_zero_cross_industry_citations(
        self, pg_conn, clean_research_tables,
    ):
        """没有跨行业 citation 时 · migration 必须空跑过 · 不报错"""
        # 不造 seed · 直接跑
        self._run_backfill_only(pg_conn)
        cur = pg_conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM geo_research_articles WHERE is_duplicate=TRUE")
        # 没数据时 · 应 0 dup
        assert cur.fetchone()['c'] == 0

    def test_backfill_recomputes_total_citation_count_correctly(
        self, pg_conn, seeded_cross_industry_state,
    ):
        """seed 故意把 article.total_citation_count 设成 99 (错值) · backfill 必须重算"""
        s = seeded_cross_industry_state
        cur = pg_conn.cursor()
        cur.execute("UPDATE geo_research_articles SET total_citation_count = 99 WHERE id = %s",
                    (s['article_a_id'],))
        pg_conn.commit()

        self._run_backfill_only(pg_conn)

        cur.execute("""
            SELECT primary_industry, total_citation_count
              FROM geo_research_articles WHERE url_hash = %s
             ORDER BY primary_industry
        """, (s['url_hash'],))
        rows = cur.fetchall()
        assert all(r['total_citation_count'] == 1 for r in rows), \
            f"backfill 必须按真实 citation count 重算 · 实际 {rows}"
