"""
conftest sanity 测试

验证:
1. pg_conn fixture 能拿数据库连接
2. sample_industry fixture 能建测试行业
3. sample_prompts fixture 能建 3 条测试 prompts
4. clean_research_tables autouse fixture 真的在测试间清表(连续两次跑都是空表)
"""

import pytest

pytestmark = pytest.mark.integration


def test_pg_conn_works(pg_conn):
    cur = pg_conn.cursor()
    cur.execute("SELECT 1 AS x")
    assert cur.fetchone()['x'] == 1


def test_sample_industry_works(clean_research_tables, sample_industry, pg_conn):
    cur = pg_conn.cursor()
    cur.execute("SELECT * FROM geo_research_industries WHERE id = %s", (sample_industry,))
    row = cur.fetchone()
    assert row is not None
    assert row['name'] == '测试行业'


def test_sample_prompts_works(clean_research_tables, sample_prompts):
    assert len(sample_prompts) == 3


def test_clean_tables_isolation(pg_conn, clean_research_tables):
    """
    申明 clean_research_tables 触发 TRUNCATE,
    然后插数据 + 断言,下次测试还是空(下个测试的 clean_research_tables 又 TRUNCATE 了)。
    """
    cur = pg_conn.cursor()

    # 测试开始时 industries 表应为空(clean_research_tables 的 TRUNCATE 跑过)
    cur.execute("SELECT COUNT(*) AS cnt FROM geo_research_industries")
    assert cur.fetchone()['cnt'] == 0, "测试开始 industries 应为空"

    # 插入一行
    cur.execute("INSERT INTO geo_research_industries (name, slug, sort_order, active) VALUES ('iso_test', 'iso_test', 999, TRUE)")
    pg_conn.commit()

    # 验证插入成功
    cur.execute("SELECT COUNT(*) AS cnt FROM geo_research_industries WHERE slug='iso_test'")
    assert cur.fetchone()['cnt'] == 1


def test_isolation_after_previous_insert(pg_conn, clean_research_tables):
    """
    上个测试插了 industry,这个测试应该看不到(clean_research_tables 在每个测试前 TRUNCATE)。
    """
    cur = pg_conn.cursor()
    cur.execute("SELECT COUNT(*) AS cnt FROM geo_research_industries WHERE slug='iso_test'")
    assert cur.fetchone()['cnt'] == 0, "isolation 失效: 上个测试的数据没被清"
