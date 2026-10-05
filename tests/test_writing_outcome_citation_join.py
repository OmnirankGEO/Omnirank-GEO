"""[WP9-P0-3] 写作效果闭环 · 被引诚实只读 join 判别。

锁定 Owner D-P0-3 铁律:
- 写作文章→mhz publish_url(published)→geo_research_articles.url→citations 真计数;
- **排除 tombstone/禁用行**(domain_tier='blacklist' / clean_status≠'cleaned');
- 仅 status='published' 的发布;
- 无发布/无匹配/查询异常 → 诚实 insufficient(citations=0·不造假);
- **research 域一行不写**(citation_fn 只读)。

用 TEMP 表影子真表,自建最小 schema,ON COMMIT DROP,不碰持久数据。
"""
import os

import psycopg2
import psycopg2.extras
import pytest

import services.writing_outcome_backfill as wob
from services.writing_outcome_backfill import _citation_count_for_articles

_DDL = """
CREATE TEMP TABLE mhz_publish_order_items (
  id BIGSERIAL PRIMARY KEY, article_id INTEGER, status TEXT, publish_url TEXT
) ON COMMIT DROP;
CREATE TEMP TABLE geo_research_articles (
  id BIGSERIAL PRIMARY KEY, url TEXT UNIQUE, domain_tier TEXT DEFAULT 'gray',
  clean_status TEXT DEFAULT 'cleaned'
) ON COMMIT DROP;
CREATE TEMP TABLE geo_research_article_citations (
  id BIGSERIAL PRIMARY KEY, article_id BIGINT, cited_at TIMESTAMPTZ DEFAULT NOW()
) ON COMMIT DROP;
"""


@pytest.fixture()
def conn(monkeypatch):
    c = psycopg2.connect(os.environ["TEST_DATABASE_URL"],
                         cursor_factory=psycopg2.extras.RealDictCursor)
    c.autocommit = False
    cur = c.cursor()
    cur.execute(_DDL)

    class _CtxNoClose:
        def __enter__(self_):
            return c
        def __exit__(self_, *a):
            return False
    # citation_fn 用模块级 get_db(import 时绑定)→ patch 其模块命名空间的名字
    monkeypatch.setattr(wob, "get_db", lambda: _CtxNoClose())
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _seed(cur, *, article_id, url, status='published', tier='gray', clean='cleaned', cites=0):
    cur.execute("INSERT INTO mhz_publish_order_items(article_id,status,publish_url) VALUES(%s,%s,%s)",
                (article_id, status, url))
    cur.execute("INSERT INTO geo_research_articles(url,domain_tier,clean_status) VALUES(%s,%s,%s) RETURNING id",
                (url, tier, clean))
    gra_id = cur.fetchone()["id"]
    for _ in range(cites):
        cur.execute("INSERT INTO geo_research_article_citations(article_id) VALUES(%s)", (gra_id,))
    return gra_id


def test_empty_ids_insufficient():
    assert _citation_count_for_articles([])["insufficient_data"] is True
    assert _citation_count_for_articles([0])["insufficient_data"] is True


def test_honest_join_counts_real_citations(conn):
    cur = conn.cursor()
    _seed(cur, article_id=11, url="https://a.example/1", cites=3)
    r = _citation_count_for_articles([11])
    assert r["insufficient_data"] is False
    assert r["matched_articles"] == 1
    assert r["citations"] == 3


def test_excludes_blacklist_and_uncleaned_tombstone(conn):
    cur = conn.cursor()
    _seed(cur, article_id=21, url="https://b.example/1", tier="blacklist", cites=5)
    _seed(cur, article_id=22, url="https://b.example/2", clean="failed", cites=5)
    # 两篇都被 tombstone/禁用规则排除 → 无匹配 → 诚实 insufficient,不计入被引
    r = _citation_count_for_articles([21, 22])
    assert r["matched_articles"] == 0
    assert r["citations"] == 0
    assert r["insufficient_data"] is True


def test_only_published_orders_count(conn):
    cur = conn.cursor()
    _seed(cur, article_id=31, url="https://c.example/1", status="pending", cites=4)
    r = _citation_count_for_articles([31])
    assert r["matched_articles"] == 0 and r["insufficient_data"] is True


def test_no_publish_url_match_is_honest_insufficient(conn):
    cur = conn.cursor()
    # 文章已发布但 publish_url 未被爬取(geo_research_articles 无此 url)→ 无匹配
    cur.execute("INSERT INTO mhz_publish_order_items(article_id,status,publish_url) VALUES(41,'published','https://d.example/x')")
    r = _citation_count_for_articles([41])
    assert r["matched_articles"] == 0 and r["citations"] == 0 and r["insufficient_data"] is True


def test_citation_fn_writes_nothing_to_research_domain(conn):
    cur = conn.cursor()
    _seed(cur, article_id=51, url="https://e.example/1", cites=2)
    before = {}
    for t in ("geo_research_articles", "geo_research_article_citations", "mhz_publish_order_items"):
        cur.execute(f"SELECT COUNT(*) AS c FROM {t}")
        before[t] = cur.fetchone()["c"]
    _citation_count_for_articles([51])  # 只读
    for t, n in before.items():
        cur.execute(f"SELECT COUNT(*) AS c FROM {t}")
        assert cur.fetchone()["c"] == n, f"{t} 行数被 citation_fn 改动(应只读)"
