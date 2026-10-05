# -*- coding: utf-8 -*-
"""C6 🔴 子集出题只清本次出题那几个词的旧草稿(真 PG · 整段在一个事务里,结束回滚)。

本机真浏览器实测(Review 09-28):键名修好后点「为新词生成标题」,新词 35 条成功,
但老词原有的 pending 标题被 save_topics_batch 的整单 DELETE 删掉 —— 服务商已付费、还没写的标题没了。
修法:save_topics_batch(only_keyword_ids=...) 只清给定词;generate-titles 带 per_keyword_plan 时传 plannable 的词。

  C6a 行为臂:A、B 各有一条 pending 旧标题,只给 B 存新标题(only_keyword_ids=[B])⇒ A 那条还在、B 的旧那条被换掉
  C6b 牙证:同样数据不传 only_keyword_ids(= 整表重生成,老行为)⇒ A 那条被删 —— 证明 C6a 的数据真能看见这个病
  C6c 接线:generate-titles 里两处 save_topics_batch 都传 only_keyword_ids=_only_kw_ids,且它只在带 per_keyword_plan 时非 None
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


@pytest.fixture
def cur():
    # 先导入 db.diagnosis_db(首次导入会用自己的连接 ALTER brands;晚于本事务插 brands 就互等挂死)
    import db.diagnosis_db  # noqa: F401
    from db.connection import get_connection
    conn = get_connection()
    conn.autocommit = False
    c = conn.cursor()
    try:
        yield c
    finally:
        conn.rollback()
        try:
            from db.connection import release_connection
            release_connection(conn)
        except Exception:  # noqa: BLE001
            conn.close()


def _seed(c):
    c.execute("INSERT INTO brands (name, is_test) VALUES ('子集存题判据_test', true) RETURNING id")
    bid = c.fetchone()["id"]
    c.execute("INSERT INTO quotes (brand_id, brand_name, status, writing_status) "
              "VALUES (%s, '子集存题判据_test', 'paid', 'in_progress') RETURNING id", (bid,))
    qid = c.fetchone()["id"]
    ids = {}
    for kw in ("老词A", "新词B"):
        c.execute("INSERT INTO confirmed_keywords (quote_id, brand_id, keyword, required_articles, is_core) "
                  "VALUES (%s, %s, %s, 1, true) RETURNING id", (qid, bid, kw))
        ids[kw] = c.fetchone()["id"]
        c.execute("INSERT INTO topics (quote_id, keyword_id, original_keyword, optimized_title, status) "
                  "VALUES (%s, %s, %s, %s, 'pending')", (qid, ids[kw], kw, kw + " 旧标题"))
    return qid, ids


def _titles(c, qid):
    c.execute("SELECT original_keyword, optimized_title FROM topics WHERE quote_id=%s ORDER BY id", (qid,))
    return [(r["original_keyword"], r["optimized_title"]) for r in c.fetchall()]


def _new_b(ids):
    return [{"keyword_id": ids["新词B"], "original_keyword": "新词B", "optimized_title": "新词B 新标题",
             "article_style": "", "status": "draft"}]


def test_c6a_subset_save_keeps_other_keywords_titles(cur):
    from db.diagnosis_db import save_topics_batch
    qid, ids = _seed(cur)
    assert save_topics_batch(qid, _new_b(ids), cursor=cur, only_keyword_ids=[ids["新词B"]])
    got = _titles(cur, qid)
    assert ("老词A", "老词A 旧标题") in got
    assert ("新词B", "新词B 新标题") in got and ("新词B", "新词B 旧标题") not in got


def test_c6b_whole_table_save_still_replaces_everything(cur):
    from db.diagnosis_db import save_topics_batch
    qid, ids = _seed(cur)
    assert save_topics_batch(qid, _new_b(ids), cursor=cur)
    assert ("老词A", "老词A 旧标题") not in _titles(cur, qid), "整表路径没删 —— C6a 的数据看不见这个病"


def test_c6c_generate_titles_passes_the_subset():
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    fn_node = next(n for n in ast.parse(src).body
                   if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_generate_titles")
    fn = ast.get_source_segment(src, fn_node)
    calls = [n for n in ast.walk(fn_node) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "save_topics_batch"]
    assert len(calls) == 2
    for c in calls:
        kw = {k.arg: k.value for k in c.keywords}
        assert getattr(kw.get("only_keyword_ids"), "id", None) == "_only_kw_ids"
    assert "if request.per_keyword_plan else None)" in fn
