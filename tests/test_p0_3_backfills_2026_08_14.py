# -*- coding: utf-8 -*-
"""P0-3 · 观测回填 · 判别锁(2026-08-14)。

变异点:
  M1 拆幂等护栏(UPDATE 去掉 target_outcome IS NULL)→ test_outcome_sql_idempotent_guard 红;
  M2 拆歧义拒绝(ambiguous 也硬选一篇)→ test_ambiguous_never_guessed 红;
  M3 拆 link 幂等护栏(UPDATE 去掉 article_id IS NULL)→ test_link_sql_idempotent_guard 红;
  M4 拆 cron 时序(回填挪到 04:05 之后)→ test_cron_registered_before_ledger_sync 红。
"""
from __future__ import annotations

import inspect
import os
import re

from scripts.backfill_monitoring_outcome_2026_08_14 import (
    BACKFILL_RESOLVER_VERSION,
    classify_legacy_row,
)
from scripts.backfill_publication_article_links_2026_08_14 import resolve_article_for_hashes


# ------------------------------------------------------------------ outcome 回填
def test_legacy_row_with_response_classified() -> None:
    row = {"target_brand_snapshot": "", "full_response": "关于电梯选购,推荐关注甲品牌。",
           "response_status": None, "is_detected": True, "mention_type": "none",
           "search_citations": ""}
    verdict = classify_legacy_row(row, "甲品牌")
    assert verdict is not None
    outcome, confidence = verdict
    assert outcome == "mentioned_only" and 0 < confidence <= 1


def test_legacy_row_not_mentioned() -> None:
    row = {"target_brand_snapshot": "", "full_response": "这是一段完全不提该品牌的回答。",
           "response_status": None, "is_detected": False, "mention_type": "none",
           "search_citations": ""}
    outcome, _ = classify_legacy_row(row, "甲品牌")
    assert outcome == "not_mentioned"


def test_legacy_row_empty_response_no_answer_state() -> None:
    """🔴 翻转锁(R2-2 条件 4,返修单 §3-3 单独说明):
    旧锁把「空答案 → engine_error」锁成正确 —— 那是把"没答案"编成"引擎错",
    63,594 行会被大面积写成错误态实质结论。新锁:空答案 → **no_answer_unjudgeable**
    (无答案不可判),不塞进任何业务分类。"""
    from scripts.backfill_monitoring_outcome_2026_08_14 import NO_ANSWER_OUTCOME

    row = {"target_brand_snapshot": "", "full_response": "", "response_status": None,
           "is_detected": False, "mention_type": "none", "search_citations": ""}
    outcome, confidence = classify_legacy_row(row, "甲品牌")
    assert outcome == NO_ANSWER_OUTCOME and confidence == 1.0
    # 反向对照①:纯空白也算空答案
    row2 = dict(row, full_response="   \n  ")
    assert classify_legacy_row(row2, "甲品牌")[0] == NO_ANSWER_OUTCOME
    # 反向对照②:该态不在八业务分类白名单里(D6-B 分母不吃它)
    import services.reco_outcome_feedback as rof
    assert NO_ANSWER_OUTCOME not in rof._VALID_OUTCOMES


def test_no_brand_skipped_not_guessed() -> None:
    # 反向对照:拿不到品牌名 → None(诚实跳过),绝不猜
    row = {"target_brand_snapshot": "", "full_response": "内容", "response_status": None,
           "is_detected": False, "mention_type": "none", "search_citations": ""}
    assert classify_legacy_row(row, "") is None
    assert classify_legacy_row(row, "甲品牌") is not None  # 有品牌就必须给结论


def test_backfill_resolver_version_distinct() -> None:
    """回填行必须可按版本区分/整体反转(口径纪律)。"""
    from services.monitoring_lineage import OUTCOME_RESOLVER_VERSION

    assert BACKFILL_RESOLVER_VERSION != OUTCOME_RESOLVER_VERSION
    assert "backfill" in BACKFILL_RESOLVER_VERSION


def test_outcome_sql_idempotent_guard() -> None:
    import scripts.backfill_monitoring_outcome_2026_08_14 as mod

    src = inspect.getsource(mod)
    assert re.search(
        r"WHERE id = %s\s+AND \(target_outcome IS NULL OR target_outcome = 'legacy_unknown'\)",
        src,
    ), "UPDATE 少了未解析幂等护栏 —— 重跑/并发会覆盖已解析值(只加注不删被破)"


def test_outcome_scope_covers_sentinel() -> None:
    """🔴 构造用例实证(2026-08-15 测试库):legacy 行的 target_outcome 是 DDL
    哨兵字符串 'legacy_unknown',**不是 NULL** —— 只认 IS NULL 的回填在生产
    恒 0 行(死回填)。批扫与 UPDATE 两处都必须认哨兵。"""
    import scripts.backfill_monitoring_outcome_2026_08_14 as mod

    src = inspect.getsource(mod)
    assert src.count("r.target_outcome = 'legacy_unknown'") == 1, "批扫没认哨兵值"
    # 反向:真实 outcome(resolver 已写)绝不在扫描范围 —— 不存在无护栏的全表 UPDATE
    assert "SET target_outcome" in src and "WHERE id = %s" in src


def test_legacy_sentinel_response_status_treated_missing() -> None:
    """response_status 同样有 'legacy_unknown' 哨兵默认:必须视同缺失走
    「非空回答=成功」推断,否则全部 legacy 行被误判 engine_error。"""
    row = {"target_brand_snapshot": "", "full_response": "推荐关注甲品牌。",
           "response_status": "legacy_unknown", "is_detected": 1,
           "mention_type": "none", "search_citations": ""}
    outcome, _ = classify_legacy_row(row, "甲品牌")
    assert outcome == "mentioned_only", f"哨兵 response_status 没被视同缺失: {outcome}"


# ------------------------------------------------------------------ 发布桥回填
_HASHES = {"h1": {101}, "h2": {102}, "h3": {101, 103}}


def test_single_match_resolved() -> None:
    assert resolve_article_for_hashes(_HASHES, ["h1"]) == (101, "matched")


def test_no_match_left_null() -> None:
    assert resolve_article_for_hashes(_HASHES, ["nope"]) == (None, "no_match")


def test_ambiguous_never_guessed() -> None:
    # 同 hash 命中两篇 → 不猜(publication_url_article_ambiguous 同款纪律)
    assert resolve_article_for_hashes(_HASHES, ["h3"]) == (None, "ambiguous")
    # 一单两条目命中不同文章 → 同样 ambiguous
    assert resolve_article_for_hashes(_HASHES, ["h1", "h2"]) == (None, "ambiguous")
    # 反向对照:两条目命中同一篇 → 正常 matched(歧义判定不许扩大化)
    assert resolve_article_for_hashes({"a": {7}, "b": {7}}, ["a", "b"]) == (7, "matched")


def test_link_sql_idempotent_guard() -> None:
    import scripts.backfill_publication_article_links_2026_08_14 as mod

    src = inspect.getsource(mod)
    assert "AND article_id IS NULL" in src, "只补 NULL 的幂等护栏被拆"


# ------------------------------------------------------------------ cron 接线
def test_cron_registered_before_ledger_sync() -> None:
    import api.scheduler as sched

    src = inspect.getsource(sched)
    assert 'id="monitoring_outcome_backfill"' in src, "存量解析 cron 没注册(死功能)"
    m_backfill = re.search(
        r'_run_monitoring_outcome_backfill,\s*trigger=CronTrigger\(hour=(\d+), minute=(\d+)', src,
    )
    assert m_backfill, "回填 job 的 CronTrigger 找不到"
    hour, minute = int(m_backfill.group(1)), int(m_backfill.group(2))
    # 时序纪律:必须早于 04:05 的归因账本同步(同 04:05→04:20 那条既有纪律)
    assert (hour, minute) < (4, 5), f"回填排在归因同步之后({hour}:{minute}),当天新解析要多等一天"
    # 反向对照:归因同步自身仍在 04:05(别把别人的表挪了)
    assert re.search(
        r'_run_article_attribution_sync,\s*trigger=CronTrigger\(hour=4, minute=5', src,
    )


def test_journal_first_and_gated_cron() -> None:
    """R2-2:①账本先行(回填源码里 journal INSERT 在 UPDATE 之前,同一事务);
    ②cron 自动 apply 默认关(签发闸);③恢复脚本只动仍带回填版本戳的行。"""
    import scripts.backfill_monitoring_outcome_2026_08_14 as bmod

    src = inspect.getsource(bmod)
    j_pos = src.index("INSERT INTO monitoring_outcome_backfill_journal")
    u_pos = src.index("UPDATE monitoring_results")
    assert j_pos < u_pos, "账本写入没有先于历史改写(改了历史才记旧值 = 白记)"
    import api.scheduler as sched

    ssrc = inspect.getsource(sched._run_monitoring_outcome_backfill)
    # 🔴 判据打 getenv **代码形态**,不打裸字符串 —— docstring 里也写了闸名,
    # 裸字符串判据会被注释撑成恒真(变异 R2-2/Mg 实测穿透过一次)。
    assert '_os.getenv("GEO_OUTCOME_BACKFILL_AUTO_APPLY"' in ssrc, "cron 没有签发闸(自动改历史)"
    assert '"gated": True' in ssrc, "闸关时必须显式报 gated,不许静默"
    import scripts.restore_monitoring_outcome_backfill_2026_08_15 as rmod

    rsrc = inspect.getsource(rmod)
    assert "AND outcome_resolver_version = %s" in rsrc, (
        "恢复没有版本戳护栏 —— 会反过来毁掉回填后被现役 resolver 重写的行"
    )


def test_backfill_restore_roundtrip_db() -> None:
    """R2-2 判据(DB 真跑):回填→账本有旧值→恢复→复算回原值;二遍恢复 0;
    🔴 反向:**拆掉账本写入 → 恢复必然 0 行**(账本是真依赖不是装饰)。"""
    import psycopg2

    from scripts.backfill_monitoring_outcome_2026_08_14 import run_backfill
    from scripts.restore_monitoring_outcome_backfill_2026_08_15 import run_restore

    url = os.environ["TEST_DATABASE_URL"]
    conn = psycopg2.connect(url)
    conn.autocommit = True
    cur = conn.cursor()
    # 自举(缺才建、建过必拆 —— 同 P2 测试与 p3a 先例;A/B 臂库是裸 template0)。
    # journal 表用**真迁移文件**建:量具与实现同口径,不手写第二份 DDL。
    from pathlib import Path as _Path

    _created: list[str] = []
    _boot = (
        ("users", "CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT, "
                  "password_hash TEXT, display_name TEXT)"),
        # [R5 ⑤ 批2] brands 不在这里手搓了（原来 3 列，生产 32 列），
        #   改由下面的 ensure_brands_schema()（生产 SSOT 出口）建；
        #   仍登记进 _created，保持本文件「建过必拆」的清理契约不变。
        ("monitoring_tasks", "CREATE TABLE monitoring_tasks (id SERIAL PRIMARY KEY, "
                             "client_id INTEGER, brand_id INTEGER, status TEXT)"),
        ("monitoring_results", "CREATE TABLE monitoring_results (id SERIAL PRIMARY KEY, "
                               "task_id INTEGER, keyword TEXT, platform TEXT, is_detected SMALLINT, "
                               "mention_type TEXT, full_response TEXT, tested_at TIMESTAMPTZ, "
                               "response_status VARCHAR(32) DEFAULT 'legacy_unknown', "
                               "search_citations TEXT, target_brand_snapshot TEXT, "
                               "target_outcome VARCHAR(40) DEFAULT 'legacy_unknown', "
                               "outcome_resolver_confidence NUMERIC(5,4), "
                               "outcome_resolver_version VARCHAR(80) DEFAULT 'legacy_unknown')"),
        ("monitoring_outcome_backfill_journal",
         (_Path(__file__).resolve().parents[1]
          / "scripts" / "migration_monitoring_outcome_backfill_journal_2026_08_15.sql"
          ).read_text(encoding="utf-8")),
    )
    cur.execute("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='brands')")
    _brands_preexisting = cur.fetchone()[0]
    from db.brands_schema import ensure_brands_schema  # 零副作用叶子模块
    ensure_brands_schema(cur)
    if not _brands_preexisting:
        _created.append("brands")
    for name, ddl in _boot:
        cur.execute("SELECT EXISTS(SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename=%s)", (name,))
        if not cur.fetchone()[0]:
            cur.execute(ddl)
            _created.append(name)
    try:
        from tests.test_p2_asset_and_next_step_2026_08_14 import _seed_user_adaptive

        _seed_user_adaptive(cur, 96001, "r2bf")
        cur.execute("INSERT INTO brands (id, name, owner_user_id) "
                    "VALUES (96001,'回填测试品牌',96001) ON CONFLICT DO NOTHING")
        cur.execute("INSERT INTO monitoring_tasks (id, client_id, brand_id, status) "
                    "VALUES (96001, 96001, 96001, 'active') ON CONFLICT DO NOTHING")
        cur.execute("DELETE FROM monitoring_outcome_backfill_journal WHERE monitoring_result_id=96001")
        cur.execute("DELETE FROM monitoring_results WHERE id=96001")
        cur.execute("INSERT INTO monitoring_results (id, task_id, keyword, platform, is_detected, "
                    "mention_type, full_response, tested_at, target_outcome) "
                    "VALUES (96001, 96001, 'kw', 'doubao', 1, 'none', '推荐关注回填测试品牌。', NOW(), 'legacy_unknown')")
        totals = run_backfill(apply=True, batch=10000, max_batches=2, batch_id="t-rt")
        assert totals["journaled"] >= 1 and totals["written"] >= 1
        cur.execute("SELECT target_outcome FROM monitoring_results WHERE id=96001")
        assert cur.fetchone()[0] == "mentioned_only"
        cur.execute("SELECT old_target_outcome FROM monitoring_outcome_backfill_journal "
                    "WHERE monitoring_result_id=96001 AND batch_id='t-rt'")
        assert cur.fetchone()[0] == "legacy_unknown", "账本没记旧值"
        restored = run_restore(batch_id="t-rt", apply=True)
        assert restored["restored"] >= 1
        cur.execute("SELECT target_outcome FROM monitoring_results WHERE id=96001")
        assert cur.fetchone()[0] == "legacy_unknown", "恢复没有回到原值"
        second = run_restore(batch_id="t-rt", apply=True)
        assert second["restored"] == 0, "二遍恢复不幂等"
        # 🔴 反向:没有账本行的批次 → 恢复 0(账本是真依赖)
        empty = run_restore(batch_id="no-such-batch", apply=True)
        assert empty["journal_rows"] == 0 and empty["restored"] == 0
    finally:
        cur.execute("DELETE FROM monitoring_outcome_backfill_journal WHERE monitoring_result_id=96001")
        cur.execute("DELETE FROM monitoring_results WHERE id=96001")
        for name in reversed(_created):
            cur.execute(f"DROP TABLE IF EXISTS {name} CASCADE")
        conn.close()


def test_bridge_rates_carry_subsample_qualifier() -> None:
    """红线桥限定语(工单红线 6):比率产出必须带「已配对子样本」限定。"""
    import services.article_attribution_ledger as ledger

    src = inspect.getsource(ledger.bridge_pairing_rates)
    assert "已配对子样本" in src
    import services.flywheel_heartbeat as hb

    hb_src = inspect.getsource(hb)
    assert "已配对子样本" in hb_src, "心跳展示端把限定语剥了"
