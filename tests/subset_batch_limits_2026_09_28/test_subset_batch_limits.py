# -*- coding: utf-8 -*-
"""WO_317 第三笔 · 子集出题修通后才走得到的四件(Review 09-28 裁定)。

  D1 🔴 防御型超 14 条:上限按条 14,超出按最大余数分给服务商选的其他方向;只选了防御型 ⇒ 出满 14 条,
        回包写明「防御型不重复的题最多 14 条,本次出 14 条」,不许静默少给、也不许报错不出
  D2 🔴 子集批次出防御题:同一单别的词已有(未删)的防御题作排除集,0 重复;排除后不够就少出并写明
  D3 🔴 混合口径子集批次:按「整单配额 − 已有未删选题已占」钳本批条数(扣费前,每词至少 1 条),
        保存后各桶合计不超整单配额、本批没有落到 NULL 桶的行(真 PG)
  D4 🔴 0 槽词是有意不出题:份数不设下限 1、没有要出题的词卡片 None;generate-titles 空批在冻结前 400;
        前端「没标题的新词」只看 plannedPosts > 0 的词 ⇒ 核心 0 槽词在,整表按钮照常出现
"""
from __future__ import annotations

import ast
import functools
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from writing.defensive_questions import defensive_capacity, defensive_titles, plan_defensive_titles  # noqa: E402
from writing.direction_distribution import scale_distribution_to_posts, trim_posts_to_total  # noqa: E402

BRAND = "测试品牌"


@functools.lru_cache(maxsize=1)
def _fn() -> str:
    src = (ROOT / "server.py").read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_generate_titles")
    return ast.get_source_segment(src, node)


# ─────────────────────────── D1 防御型超 14 ───────────────────────────

def test_d1_only_defensive_is_capped_and_the_batch_is_trimmed():
    cap = defensive_capacity()
    assert cap == 14
    scaled = scale_distribution_to_posts({"defensive_company": 7}, 35)        # 7 槽 ⇒ 35 条,只选防御型
    assert scaled == {"defensive_company": 14}
    trimmed = trim_posts_to_total({1: 35}, sum(scaled.values()))
    assert trimmed == {1: 14}
    two = trim_posts_to_total({1: 35, 2: 15}, 14)                              # 按比例,每词至少 1
    assert sum(two.values()) == 14 and min(two.values()) >= 1
    assert trim_posts_to_total({1: 5, 2: 5, 3: 5}, 2) is None                  # 不够每词 1 条 ⇒ 调用方拒
    fn = _fn()
    assert "防御型不重复的题最多 {_def_capacity()} 条{_def_used_note}," in fn
    assert "_kw[\"planned_count\"] = _posts_for_gen[_kw.get(\"id\")]" in fn        # 真收小生成条数
    assert '"notes": list(_batch_notes),' in fn and '_message += f"·{_note}"' in fn


def test_d1_with_other_directions_the_excess_moves_there():
    s = scale_distribution_to_posts({"defensive_company": 4, "evidence_qa": 3}, 35)
    assert s == {"defensive_company": 14, "evidence_qa": 21}


# ─────────────────────────── D2 排除已有防御题 ───────────────────────────

def test_d2_subset_batch_never_repeats_an_existing_defensive_title():
    all_titles = [d["title"] for d in defensive_titles(BRAND, 14)]
    assert len(set(all_titles)) == 14                                           # 标题唯一,才能按标题排除
    assert [d["title"] for d in defensive_titles(BRAND, 14, exclude_titles=())] == all_titles  # 不排除 = 老顺序
    existing = set(all_titles[:5])
    applied, unconverted = plan_defensive_titles(BRAND, [{"id": i} for i in range(9)], exclude_titles=existing)
    assert len(applied) == 9 and not ({a["title"] for a in applied} & existing)
    applied, unconverted = plan_defensive_titles(BRAND, [{"id": i} for i in range(10)], exclude_titles=existing)
    assert len(applied) == 9 and [u["reason"] for u in unconverted] == ["capacity_exceeded"]
    # 牙证:不传排除集就会重复
    applied_bad, _ = plan_defensive_titles(BRAND, [{"id": i} for i in range(9)])
    assert {a["title"] for a in applied_bad} & existing


def test_d2_endpoint_wires_the_exclusion_and_the_reduced_cap():
    fn = _fn()
    assert "exclude_titles=_def_existing," in fn
    assert "_def_cap_now = _def_capacity() - len(_def_existing)" in fn
    assert "defensive_cap=_def_cap_now" in fn
    assert re.search(r"_def_existing = \{t\.get\(\"optimized_title\"\) for t in _surviving_topics", fn)


# ─────────────────────────── D3 混合口径配额(真 PG) ───────────────────────────

@pytest.fixture
def cur():
    # 🔴 先导入 db.diagnosis_db:它首次导入会跑 init_db → ensure_brands_schema,用**自己的连接**
    #    ALTER brands;若在本事务插了 brands 之后才首次导入,两边互等 ⇒ 整格挂死(本机实测)。
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
            from db.connection import put_connection
            put_connection(conn)
        except Exception:  # noqa: BLE001
            conn.close()


QUOTA = {"focus_media_anchor": 2, "industry_platform_coverage": 5, "douyin_doubao_only": 0}


def _seed_mixed(c):
    c.execute("INSERT INTO brands (name, is_test) VALUES ('混合口径钳量判据_test', true) RETURNING id")
    bid = c.fetchone()["id"]
    c.execute("INSERT INTO quotes (brand_id, brand_name, status, writing_status) "
              "VALUES (%s, '混合口径钳量判据_test', 'paid', 'in_progress') RETURNING id", (bid,))
    qid = c.fetchone()["id"]
    c.execute("INSERT INTO keyword_selection_sessions (token, quote_id, brand_id, keywords_snapshot, expires_at) "
              "VALUES (%s, %s, %s, '[]', '2099-01-01') RETURNING id", ("d3-%s" % qid, qid, bid))
    sid = c.fetchone()["id"]
    body = json.dumps({"media_mix": {"delivery_perspective": "mixed", "posts_estimate": QUOTA}}, sort_keys=True)
    c.execute("INSERT INTO quote_pricing_snapshots (quote_id, brand_id, selection_session_id, version, reason, "
              "calculation_version, pricing_snapshot, snapshot_hash) VALUES (%s,%s,%s,1,'判据','d3',%s::jsonb,%s)",
              (qid, bid, sid, body, hashlib.sha256(body.encode()).hexdigest()))
    ids = {}
    for kw in ("老词", "新词"):
        c.execute("INSERT INTO confirmed_keywords (quote_id, brand_id, keyword, required_articles, is_core) "
                  "VALUES (%s, %s, %s, 3, true) RETURNING id", (qid, bid, kw))
        ids[kw] = c.fetchone()["id"]
    for i, b in enumerate(["focus_media_anchor", "industry_platform_coverage", "industry_platform_coverage"]):
        c.execute("INSERT INTO topics (quote_id, keyword_id, original_keyword, optimized_title, status, media_bucket) "
                  "VALUES (%s, %s, '老词', %s, 'pending', %s)", (qid, ids["老词"], "老词 已有 %d" % i, b))
    return qid, ids


def _surviving(c, qid, batch_ids):
    c.execute("SELECT keyword_id, status, media_bucket FROM topics WHERE quote_id=%s", (qid,))
    return [dict(r) for r in c.fetchall()
            if not (r["keyword_id"] in batch_ids and r["status"] in ("draft", "pending", "regenerating"))]


def _save_new(c, qid, kid, n):
    from db.diagnosis_db import save_topics_batch
    topics = [{"keyword_id": kid, "original_keyword": "新词", "optimized_title": "新词 第%d条" % i,
               "article_style": "", "status": "draft"} for i in range(n)]
    return save_topics_batch(qid, topics, cursor=c, only_keyword_ids=[kid])


def _bucket_totals(c, qid):
    c.execute("SELECT media_bucket, count(*) AS n FROM topics WHERE quote_id=%s GROUP BY 1", (qid,))
    return {r["media_bucket"]: int(r["n"]) for r in c.fetchall()}


def test_d3_subset_batch_is_clamped_to_the_remaining_mixed_quota(cur):
    from services.media_slot_conversion import mixed_remaining_posts
    qid, ids = _seed_mixed(cur)
    left = mixed_remaining_posts(qid, _surviving(cur, qid, {ids["新词"]}), cur)
    assert left == (2 - 1) + (5 - 2) + 0 == 4
    trimmed = trim_posts_to_total({ids["新词"]: 15}, left)                 # 3 槽自媒体 15 条 ⇒ 钳到 4
    assert trimmed == {ids["新词"]: 4}
    assert _save_new(cur, qid, ids["新词"], trimmed[ids["新词"]])
    totals = _bucket_totals(cur, qid)
    assert None not in totals, "有行落到 NULL 桶 = 超出整单配额"
    assert all(totals.get(b, 0) <= q for b, q in QUOTA.items()), totals


def test_d3_tooth_without_the_clamp_rows_overflow_into_null_buckets(cur):
    qid, ids = _seed_mixed(cur)
    assert _save_new(cur, qid, ids["新词"], 15)                            # 不钳(= 改前)
    assert _bucket_totals(cur, qid).get(None, 0) == 11                    # 4 条进桶,11 条溢出到 NULL


def test_d3_the_clamp_sits_before_any_charge():
    fn = _fn()
    i = fn.index("_mixed_left = mixed_remaining_posts(request.quote_id, _surviving_topics)")
    assert i < fn.index("await deduct_points(") and i < fn.index("await reserve_charge(")


# ─────────────────────────── D4 0 槽词 ───────────────────────────

def test_d4_empty_batch_is_rejected_before_anything_is_frozen():
    from services.topic_gen_charge import charge_card, keyword_count
    zero = [{"id": 1, "required_articles": 0, "planned_posts_default": 0}]
    assert keyword_count(zero) == 0
    assert charge_card(zero, billable=True, lookup=lambda _c: {"cost_points": 80}) is None
    one = zero + [{"id": 2, "required_articles": 1, "planned_posts_default": 5}]
    assert keyword_count(one) == 1
    fn = _fn()
    i = fn.index('raise HTTPException(status_code=400, detail="没有需要出题的关键词")')
    for charging in ("await deduct_points(", "await reserve_charge(", "await check_balance_only(",
                     'update_writing_status(request.quote_id, "titles_generating")'):
        assert i < fn.index(charging), charging


def test_d4_front_end_ignores_zero_slot_keywords_when_looking_for_new_ones():
    tsx = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(encoding="utf-8")
    assert "keywords.some(kw => plannedPosts(kw) > 0 && !topics.some(t => t.keyword_id === kw.id))" in tsx
    assert "keywords.filter((kw) => plannedPosts(kw) > 0).map((kw) => kw.id), topics));" in tsx
    assert "const canGenerateTitlesNow = kwFace === 'new-keywords' && plannedPosts(kw) > 0;" in tsx
