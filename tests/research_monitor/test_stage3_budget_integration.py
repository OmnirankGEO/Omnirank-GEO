"""
Stage3 URL 预算 真 PG 集成判别测试(2026-07-16 P1 · 禁假绿)

覆盖(真 PG · 需 TEST_DATABASE_URL):
- db 层: persist 幂等(ON CONFLICT 不覆盖已处理状态)/ load_pending / mark_status
  终态守护(done 不被覆盖)/ 计数
- ensure_stage3_url_budget: 首次计算+持久化 / 续跑读回 pending 不重算(幂等)
- stage3_crawl_articles: 处理完更新 budget status(耐久断点)/ 取消保持 pending /
  30s 伴飞心跳 ticker
- seed migration 幂等 2×
"""
import asyncio
import hashlib
import inspect
from unittest.mock import patch, MagicMock

import pytest
import psycopg2

from services.research_monitor import round_runner
from db import research_url_budget_db as bdb


def _uh(s: str) -> str:
    return hashlib.sha1(s.encode()).hexdigest()


@pytest.fixture(scope='session', autouse=True)
def _ensure_budget_schema(setup_test_db):
    """确保测试库有 url_budget 表 + raw.is_answer_cited 列(conftest migration 列表
    不含 placement_flywheel/本批 migration)。独立 autocommit 连接, 幂等。"""
    import os
    import psycopg2
    url = os.environ.get('DATABASE_URL') or os.environ.get('TEST_DATABASE_URL')
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("ALTER TABLE geo_research_raw ADD COLUMN IF NOT EXISTS is_answer_cited BOOLEAN DEFAULT FALSE")
    with open('scripts/migration_stage3_url_budget_2026_07_16.sql', encoding='utf-8') as f:
        cur.execute(f.read())
    with open(
        'scripts/migration_stage3_url_budget_industry_name_2026_07_18.sql',
        encoding='utf-8',
    ) as f:
        cur.execute(f.read())
    conn.close()
    yield


@pytest.fixture
def clean_budget(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("TRUNCATE TABLE geo_research_url_budget RESTART IDENTITY")
    cur.execute("DELETE FROM geo_research_raw WHERE batch_id LIKE 'batch_s3b%'")
    pg_conn.commit()
    yield
    cur.execute("TRUNCATE TABLE geo_research_url_budget RESTART IDENTITY")
    cur.execute("DELETE FROM geo_research_raw WHERE batch_id LIKE 'batch_s3b%'")
    pg_conn.commit()


def _sample_selected(n=5, round_id='r_s3b'):
    # url_hash 必须 = compute_url_hash(normalized_url)(与生产 crawl_one 算法一致,
    # 否则 mark_budget_status 的 WHERE url_hash 不命中)。
    from services.research_monitor.url_normalizer import compute_url_hash
    out = []
    for i in range(n):
        norm = f'https://d{i}.com/a'
        out.append({
            'url_hash': compute_url_hash(norm),
            'normalized_url': norm,
            'url': norm,
            'domain': f'd{i}.com',
            'industry_id': 1, 'industry_name': '行业0',
            'prompt_id': 10, 'platform': 'qwen',
            'raw_id': 100 + i, 'rank_in_response': 1,
            'score': 5.0 - i, 'budget_rank': i + 1,
        })
    return out


# ==================== db 层 ====================

@pytest.mark.usefixtures('clean_budget')
class TestBudgetDb:
    def test_null_industry_is_normalized_and_idempotent(self):
        rid = 'r_s3b_null_industry'
        sel = _sample_selected(1, rid)
        sel[0]['industry_name'] = None
        assert bdb.persist_url_budget(rid, sel) == 1
        assert bdb.persist_url_budget(rid, sel) == 0
        assert bdb.mark_budget_status(
            rid, sel[0]['url_hash'], None, 'done', 'reused_existing'
        )
        assert bdb.get_budget_status_counts(rid).get('done') == 1

    def test_persist_idempotent_no_overwrite_status(self):
        rid = 'r_s3b_idem'
        sel = _sample_selected(5, rid)
        assert bdb.persist_url_budget(rid, sel) == 5
        assert bdb.count_budget_rows(rid) == 5
        # 处理其中一个 → done
        assert bdb.mark_budget_status(rid, sel[0]['url_hash'], '行业0', 'done', 'crawled_new')
        # 幂等重 persist(续跑重算)→ 0 新增, done 状态不被覆盖回 pending
        assert bdb.persist_url_budget(rid, sel) == 0
        counts = bdb.get_budget_status_counts(rid)
        assert counts.get('done') == 1
        assert counts.get('pending') == 4

    def test_load_pending_excludes_terminal(self):
        rid = 'r_s3b_pend'
        sel = _sample_selected(5, rid)
        bdb.persist_url_budget(rid, sel)
        bdb.mark_budget_status(rid, sel[0]['url_hash'], '行业0', 'done', 'reused_existing')
        bdb.mark_budget_status(rid, sel[1]['url_hash'], '行业0', 'skipped', 'skipped_short')
        bdb.mark_budget_status(rid, sel[2]['url_hash'], '行业0', 'failed', 'failed_unknown')
        pending = bdb.load_pending_budget(rid)
        assert len(pending) == 2
        # 按 budget_rank 升序(质量优先)
        assert [p['budget_rank'] for p in pending] == [4, 5]
        # 字段结构对齐 crawl_one 所需
        assert pending[0]['normalized_url'] and pending[0]['url_hash']

    def test_done_terminal_guard(self):
        rid = 'r_s3b_guard'
        sel = _sample_selected(1, rid)
        bdb.persist_url_budget(rid, sel)
        assert bdb.mark_budget_status(rid, sel[0]['url_hash'], '行业0', 'done', 'crawled_new')
        # 迟到 worker 想改回 failed → WHERE status<>'done' 挡下, 0 行
        changed = bdb.mark_budget_status(rid, sel[0]['url_hash'], '行业0', 'failed', 'failed_unknown')
        assert changed is False
        assert bdb.get_budget_status_counts(rid).get('done') == 1

    def test_seed_migration_idempotent(self, pg_conn):
        with open('scripts/migration_stage3_url_budget_2026_07_16.sql', encoding='utf-8') as f:
            sql = f.read()
        with open(
            'scripts/migration_stage3_url_budget_industry_name_2026_07_18.sql',
            encoding='utf-8',
        ) as f:
            upgrade_sql = f.read()
        cur = pg_conn.cursor()
        cur.execute(sql)
        cur.execute(sql)  # 幂等 2×
        cur.execute(upgrade_sql)
        cur.execute(upgrade_sql)  # 独立增量同样必须幂等
        pg_conn.commit()
        cur.execute(
            "SELECT value_json FROM geo_research_config WHERE key = 'stage3_url_budget_max'")
        assert cur.fetchone()['value_json'] == 1500

    def test_nullable_legacy_schema_upgrades_without_data_loss(self, pg_conn):
        cur = pg_conn.cursor()
        cur.execute("ALTER TABLE geo_research_url_budget ALTER COLUMN industry_name DROP NOT NULL")
        cur.execute(
            """
            INSERT INTO geo_research_url_budget
                (round_id,url_hash,normalized_url,url,industry_name)
            VALUES ('legacy_null', %s, 'https://legacy.test/a',
                    'https://legacy.test/a', NULL)
            """,
            ('a' * 40,),
        )
        pg_conn.commit()
        with open(
            'scripts/migration_stage3_url_budget_industry_name_2026_07_18.sql',
            encoding='utf-8',
        ) as f:
            sql = f.read()
        cur.execute(sql)
        cur.execute(sql)
        pg_conn.commit()
        cur.execute(
            "SELECT industry_name FROM geo_research_url_budget WHERE round_id='legacy_null'"
        )
        assert cur.fetchone()['industry_name'] == ''
        cur.execute(
            """
            SELECT attnotnull
              FROM pg_attribute
             WHERE attrelid='geo_research_url_budget'::regclass
               AND attname='industry_name'
            """
        )
        assert cur.fetchone()['attnotnull'] is True

    def test_nullable_collision_aborts_without_deleting_rows(self, pg_conn):
        cur = pg_conn.cursor()
        cur.execute("TRUNCATE geo_research_url_budget")
        cur.execute("ALTER TABLE geo_research_url_budget ALTER COLUMN industry_name DROP NOT NULL")
        cur.execute(
            """
            INSERT INTO geo_research_url_budget
                (round_id,url_hash,normalized_url,url,industry_name)
            VALUES
                ('legacy_collision', %s, 'https://legacy.test/a',
                 'https://legacy.test/a', NULL),
                ('legacy_collision', %s, 'https://legacy.test/b',
                 'https://legacy.test/b', '')
            """,
            ('b' * 40, 'b' * 40),
        )
        pg_conn.commit()
        with open(
            'scripts/migration_stage3_url_budget_industry_name_2026_07_18.sql',
            encoding='utf-8',
        ) as f:
            sql = f.read()
        try:
            with pytest.raises(psycopg2.Error, match='NULL/empty collision'):
                cur.execute(sql)
            pg_conn.rollback()
            cur.execute(
                """
                SELECT COUNT(*) AS n, COUNT(*) FILTER (WHERE industry_name IS NULL) AS nulls
                  FROM geo_research_url_budget
                 WHERE round_id='legacy_collision'
                """
            )
            row = cur.fetchone()
            assert row['n'] == 2
            assert row['nulls'] == 1
        finally:
            pg_conn.rollback()
            cur.execute("TRUNCATE geo_research_url_budget")
            cur.execute("ALTER TABLE geo_research_url_budget ALTER COLUMN industry_name SET NOT NULL")
            pg_conn.commit()


# ==================== ensure_stage3_url_budget 幂等 ====================

@pytest.mark.usefixtures('clean_budget')
class TestEnsureBudget:
    def _seed_raw(self, pg_conn, batch_id, urls_meta):
        cur = pg_conn.cursor()
        for m in urls_meta:
            cur.execute(
                """
                INSERT INTO geo_research_raw
                    (industry, query, engine, cited_platform, cite_position,
                     cite_url, is_answer_cited, batch_id, researcher)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '')
                """,
                (m['industry'], 'q', m['engine'], 'x.com', m['pos'],
                 m['cite_url'], m['answer_cited'], batch_id),
            )
        pg_conn.commit()

    def test_first_compute_then_resume_reads_back(self, pg_conn):
        rid = 'r_s3b_ensure'
        batch = 'batch_s3b_ensure'
        # 10 candidate URL, 各 1 raw 行
        raw_meta = [{'industry': '行业0', 'engine': 'qwen', 'pos': 1,
                     'cite_url': f'https://e{i}.com/a', 'answer_cited': (i < 3)}
                    for i in range(10)]
        self._seed_raw(pg_conn, batch, raw_meta)
        kept = [{'url': m['cite_url'], 'normalized_url': m['cite_url'],
                 'industry_id': 1, 'industry_name': '行业0', 'prompt_id': 10,
                 'platform': 'qwen', 'raw_id': None, 'rank_in_response': 1}
                for m in raw_meta]

        with patch.object(round_runner, '_read_config_value',
                          side_effect=lambda k, d, c: {'stage3_url_budget_max': 5}.get(k, d)):
            pending1 = round_runner.ensure_stage3_url_budget(rid, batch, kept)
        # 首次: budget_max=5 → 选中 5, 全 pending
        assert len(pending1) == 5
        assert bdb.count_budget_rows(rid) == 5
        # 答案引用的(前 3 个)应因高分被选中
        sel_urls = {p['normalized_url'] for p in pending1}
        assert sum(1 for i in range(3) if f'https://e{i}.com/a' in sel_urls) >= 2

        # 处理 2 个 → done
        for p in pending1[:2]:
            bdb.mark_budget_status(rid, p['url_hash'], '行业0', 'done', 'crawled_new')

        # 续跑: 预算表已有行 → 不重算, 只读回剩余 pending(耐久断点)
        with patch.object(round_runner, '_read_config_value',
                          side_effect=lambda k, d, c: {'stage3_url_budget_max': 5}.get(k, d)):
            pending2 = round_runner.ensure_stage3_url_budget(rid, batch, kept)
        assert len(pending2) == 3, "续跑应只读回未完成 3 个(幂等断点)"
        assert bdb.count_budget_rows(rid) == 5, "续跑不得新增预算行(不重算)"

    def test_db_hit_free_urls_exempt_from_budget(self, pg_conn):
        """[轮1审核 must_fix] DB 命中(articles 已有 url_hash)的免费项(reused/dup·零 Jina)
        豁免出 budget_max, 全保留写 citation — 不被预算裁掉(覆盖率不随 DB 成熟下降)。"""
        from services.research_monitor.url_normalizer import compute_url_hash
        rid = 'r_s3b_free'
        batch = 'batch_s3b_free'
        # 30 个 candidate; 前 10 个预建 article(DB 命中=免费), budget_max=5
        raw_meta = [{'industry': '行业0', 'engine': 'qwen', 'pos': 9,
                     'cite_url': f'https://f{i}.com/a', 'answer_cited': False}
                    for i in range(30)]
        self._seed_raw(pg_conn, batch, raw_meta)
        cur = pg_conn.cursor()
        for i in range(10):
            norm = f'https://f{i}.com/a'
            cur.execute(
                """
                INSERT INTO geo_research_articles
                    (url, url_hash, domain, primary_industry, clean_status, review_status,
                     first_seen_round_id, fetched_at)
                VALUES (%s, %s, %s, %s, 'pending', 'crawled', 'old_round', NOW())
                ON CONFLICT DO NOTHING
                """,
                (norm, compute_url_hash(norm), f'f{i}.com', '行业0'),
            )
        pg_conn.commit()
        kept = [{'url': m['cite_url'], 'normalized_url': m['cite_url'],
                 'industry_id': 1, 'industry_name': '行业0', 'prompt_id': 10,
                 'platform': 'qwen', 'raw_id': None, 'rank_in_response': 9}
                for m in raw_meta]

        with patch.object(round_runner, '_read_config_value',
                          side_effect=lambda k, d, c: {'stage3_url_budget_max': 5}.get(k, d)), \
             patch.object(round_runner, '_get_int_config',
                          side_effect=lambda k, d: {'stage3_min_urls_per_industry': 0,
                                                    'stage3_min_urls_per_platform': 0,
                                                    'stage3_max_urls_per_domain': 100}.get(k, d)):
            pending = round_runner.ensure_stage3_url_budget(rid, batch, kept)
        # 免费 10(全保留) + 付费选中 ≤5 = 落库 ≤15, 且 ≥10(免费全在)
        assert bdb.count_budget_rows(rid) >= 10, "DB 命中免费项被裁掉了"
        assert bdb.count_budget_rows(rid) <= 15, "付费项超上限"
        # 10 个预建 URL 全部在预算里(免费全保留)
        pend_urls = {p['normalized_url'] for p in pending}
        for i in range(10):
            assert f'https://f{i}.com/a' in pend_urls, f"免费项 f{i} 被裁"

    def test_budget_max_zero_takes_all(self, pg_conn):
        rid = 'r_s3b_unlim'
        batch = 'batch_s3b_unlim'
        raw_meta = [{'industry': '行业0', 'engine': 'qwen', 'pos': 1,
                     'cite_url': f'https://u{i}.com/a', 'answer_cited': False}
                    for i in range(8)]
        self._seed_raw(pg_conn, batch, raw_meta)
        kept = [{'url': m['cite_url'], 'normalized_url': m['cite_url'],
                 'industry_id': 1, 'industry_name': '行业0', 'prompt_id': 10,
                 'platform': 'qwen', 'raw_id': None, 'rank_in_response': 1}
                for m in raw_meta]
        with patch.object(round_runner, '_read_config_value',
                          side_effect=lambda k, d, c: {'stage3_url_budget_max': 0}.get(k, d)):
            pending = round_runner.ensure_stage3_url_budget(rid, batch, kept)
        assert len(pending) == 8, "budget_max=0 应不限(全量)"

    def test_stage3_cost_estimate_excludes_free_library_hits(self):
        """Free DB hits remain available for citations but consume no Jina budget."""
        rows = [
            {'url_hash': 'free-a'},
            {'url_hash': 'paid-a'},
            {'url_hash': 'free-b'},
            {'url_hash': 'paid-b'},
        ]
        with patch(
            'db.research_url_budget_db.load_existing_article_url_hashes',
            return_value={'free-a', 'free-b'},
        ):
            assert round_runner._count_stage3_paid_urls(rows) == 2

        pipeline_source = inspect.getsource(round_runner._run_pipeline)
        assert 'paid_budget_count = _count_stage3_paid_urls(budget_urls)' in pipeline_source
        assert 'estimated_increment_yuan=paid_budget_count * COST_PER_JINA_CRAWL' in pipeline_source


# ==================== stage3 断点/取消/心跳 ====================

@pytest.mark.usefixtures('clean_budget')
class TestStage3Runtime:
    def _persist_pending(self, rid, n):
        sel = _sample_selected(n, rid)
        bdb.persist_url_budget(rid, sel)
        return sel

    def test_crawl_updates_budget_status_durable(self, pg_conn):
        rid = 'r_s3b_rt'
        sel = self._persist_pending(rid, 3)
        pending = bdb.load_pending_budget(rid)

        # mock crawl: 让每个 URL 走 reused_existing(articles 已有)→ done
        async def fake_flush(*a, **k):
            pass

        # 直接驱动真实 stage3_crawl_articles, mock 掉 DB article 查询让走 reused_existing
        class _Cur:
            def __init__(self):
                self._one = {'id': 1}
                self.rowcount = 1
            def execute(self, sql, params=()):
                s = ' '.join(sql.split())
                # 本行业已有 article → reused_existing
                if 'SELECT id FROM geo_research_articles' in s and 'primary_industry' in s:
                    self._one = {'id': 1}
                else:
                    self._one = None
                self.rowcount = 1
            def fetchone(self):
                return self._one
            def close(self):
                pass

        class _Conn:
            def cursor(self):
                return _Cur()
            def commit(self):
                pass
            def close(self):
                pass

        with patch.object(round_runner, 'get_connection', side_effect=lambda: _Conn()), \
             patch.object(round_runner, 'update_heartbeat', MagicMock()), \
             patch.object(round_runner, 'update_round_progress', MagicMock()), \
             patch.object(round_runner, 'get_round_status', return_value={'status': 'running'}), \
             patch.object(round_runner, '_log_cost', MagicMock()), \
             patch.object(round_runner, 'STAGE3_HEARTBEAT_INTERVAL_SECONDS', 0.05):
            asyncio.run(round_runner.stage3_crawl_articles(rid, pending))

        # 全部处理完 → budget status done(耐久断点落库)
        counts = bdb.get_budget_status_counts(rid)
        assert counts.get('done') == 3, f"耐久断点未落库: {counts}"
        assert bdb.load_pending_budget(rid) == []

    def test_cancel_keeps_pending_for_resume(self, pg_conn):
        """取消: 在跑 URL 不被标终态, 保持 pending → 续跑重爬(要求4)。"""
        rid = 'r_s3b_cancel'
        self._persist_pending(rid, 4)
        pending = bdb.load_pending_budget(rid)

        # get_round_status 返回 cancelled → crawl_one 首行 _raise_if_round_cancelled 抛
        with patch.object(round_runner, 'get_connection', side_effect=lambda: MagicMock()), \
             patch.object(round_runner, 'update_heartbeat', MagicMock()), \
             patch.object(round_runner, 'update_round_progress', MagicMock()), \
             patch.object(round_runner, 'get_round_status', return_value={'status': 'cancelled'}), \
             patch.object(round_runner, '_log_cost', MagicMock()), \
             patch.object(round_runner, 'STAGE3_HEARTBEAT_INTERVAL_SECONDS', 0.05):
            with pytest.raises(round_runner.RoundCancelledError):
                asyncio.run(round_runner.stage3_crawl_articles(rid, pending))

        # 取消后全部保持 pending(未被误标 failed → 续跑可重爬)
        counts = bdb.get_budget_status_counts(rid)
        assert counts.get('pending') == 4, f"取消误标了终态: {counts}"
        assert 'failed' not in counts

    def test_heartbeat_ticker_beats_during_slow_crawl(self, pg_conn):
        """30s 伴飞心跳: 全部 worker 卡在 Jina 时仍时间驱动刷心跳(要求6)。"""
        rid = 'r_s3b_hb'
        self._persist_pending(rid, 2)
        pending = bdb.load_pending_budget(rid)
        hb = MagicMock()

        # crawl 卡住(mock query_with_retry sleep), ticker 应照打心跳
        async def slow_jina(*a, **k):
            await asyncio.sleep(0.4)
            return {'title': 't', 'content': 'x' * 100, 'char_count': 100,
                    'ok': True, 'markdown': 'x' * 100}

        class _AnyCur:
            def execute(self, *a, **k): pass
            def fetchone(self): return None
            def close(self): pass
        class _AnyConn:
            def cursor(self): return _AnyCur()
            def commit(self): pass
            def close(self): pass

        with patch.object(round_runner, 'get_connection', side_effect=lambda: _AnyConn()), \
             patch.object(round_runner, 'update_heartbeat', hb), \
             patch.object(round_runner, 'update_round_progress', MagicMock()), \
             patch.object(round_runner, 'get_round_status', return_value={'status': 'running'}), \
             patch.object(round_runner, '_log_cost', MagicMock()), \
             patch.object(round_runner, 'crawl_article', side_effect=slow_jina), \
             patch.object(round_runner, 'STAGE3_HEARTBEAT_INTERVAL_SECONDS', 0.1):
            asyncio.run(round_runner.stage3_crawl_articles(rid, pending))

        # 0.4s crawl / 0.1s ticker → 伴飞心跳至少 2 拍
        assert hb.call_count >= 2, f"伴飞心跳未生效: {hb.call_count}"


# Production web/cron never run migrations. Keep the Stage3 schema wired into
# the shared prestart manifest instead of relying on test-local SQL execution.
def test_stage3_budget_migration_is_in_prestart_manifest():
    from db.migration_manifest import MIGRATIONS

    base = "scripts/migration_geo_research_monitor.sql"
    concurrency = "scripts/migration_research_round_concurrency_autoresume_2026_07_16.sql"
    budget = "scripts/migration_stage3_url_budget_2026_07_16.sql"

    assert budget in MIGRATIONS
    assert MIGRATIONS.index(base) < MIGRATIONS.index(concurrency) < MIGRATIONS.index(budget)
