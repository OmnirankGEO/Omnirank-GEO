"""[统一 R3 · 2026-07-23 §五] 法律禁止清单版本血缘传播断言。

传播链(全部 additive · 旧文章/旧调用方无版本保持兼容、不回填):

    evidence_first_policy.LEGAL_PROHIBITION_CATALOG_VERSION (SSOT 常量, v8)
      ├─ 生成期: article_lineage.generation_request_snapshot
      │    .legal_prohibition_catalog_version            (B 包已落地,此处钉死)
      ├─ 失败投影: topics.generation_legal_catalog_version
      │    (mark_failure_on_cursor + 重写失败内联 UPDATE;full_reset 时清空)
      └─ 发布冻结快照:
           · ArticleDispatchSnapshot.legal_prohibition_catalog_version
             (发布动作时用于判定外发内容的清单版本 = 当前常量)
           · mhz_publish_order_items.submitted_legal_catalog_version
             (snapshot_writer 链持久化;缺省不写该列)
           · articles.publication_snapshot.legal_prohibition_catalog_version
             (来自文章 generation_request_snapshot 的生成期版本;历史文章=None)
"""
from __future__ import annotations

import os

import psycopg2
import pytest

from writing.evidence_first_policy import LEGAL_PROHIBITION_CATALOG_VERSION

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 🔴 [V6 单点化 2026-08-10 · 最终接管工单 §7] 版本不再钉死本文件字面量 ——
# 旧值 "ad-law-art9-absolute-v9" 是第二份未签发目录的版本号,已废。
# 现版本单点来自 Owner 签发包(config/legal_prohibited_pack.json),
# 本测试与运行时同源取值:改包版本,两边一起变,不再有第二份可漂移。
from services.marketing.guards import legal_pack_version

CURRENT_CATALOG_VERSION = legal_pack_version()


def test_catalog_version_is_signed_pack_version() -> None:
    """单点化判别锁:运行时版本 == 签发包版本,且非空。"""
    assert str(LEGAL_PROHIBITION_CATALOG_VERSION) == CURRENT_CATALOG_VERSION
    assert CURRENT_CATALOG_VERSION, "签发包版本为空 —— 包没读到"
    assert CURRENT_CATALOG_VERSION != "ad-law-art9-absolute-v9", (
        "版本又回到了第二份未签发目录的字面量"
    )


# ============================================================
# 1. 生成期 lineage(B 包已落地 · 钉死防回归)
# ============================================================
def test_lineage_freezes_catalog_version() -> None:
    assert LEGAL_PROHIBITION_CATALOG_VERSION == CURRENT_CATALOG_VERSION
    from writing.article_lineage import build_article_lineage

    lineage = build_article_lineage(
        topic={
            "id": 10,
            "title": "隔离器如何选型？",
            "style_code": "buying_guide",
            "_evidence_pack": {"items": []},
        },
        article={
            "title": "隔离器如何选型？",
            "content": "先核对工艺边界，再核对公开标准与验收步骤。" * 100,
            "style": "buying_guide",
        },
        quote_id=20,
        industry="制药装备",
        client_brand="测试品牌",
    )
    snapshot = lineage["generation_request_snapshot"]
    assert snapshot["legal_prohibition_catalog_version"] == LEGAL_PROHIBITION_CATALOG_VERSION


# ============================================================
# 2. 发布冻结快照(代码对象): ArticleDispatchSnapshot
# ============================================================
def test_dispatch_snapshot_carries_catalog_version(monkeypatch) -> None:
    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "false")
    import services.article_publish_dispatch as dispatch
    import services.article_review_shadow as shadow

    monkeypatch.setattr(dispatch, "_load_contact_consent_context", lambda article_id: (None, None))
    monkeypatch.setattr(
        shadow,
        "record_dispatch_review_shadow",
        lambda **kwargs: {"recorded": False, "reason": "simulated"},
    )
    snapshot = dispatch.prepare_article_dispatch_snapshot(
        article_id=0,
        source_title="隔离器选型方法指南",
        source_content="本文介绍隔离器选型时需要核对的材料和步骤。",
        outgoing_title="隔离器选型方法指南",
        outgoing_content="本文介绍隔离器选型时需要核对的材料和步骤。",
        source="unit_test",
    )
    assert snapshot.review_reason == "publication_review_gate_disabled"
    assert snapshot.legal_prohibition_catalog_version is None
    assert snapshot.payload()["legal_prohibition_catalog_version"] is None


def test_dispatch_snapshot_field_defaults_to_none_for_legacy_callers() -> None:
    """additive 兼容:旧调用方不传新字段 → None,不破坏既有构造。"""
    from services.article_publish_dispatch import ArticleDispatchSnapshot

    legacy = ArticleDispatchSnapshot(
        article_id=1,
        source="legacy",
        title="t",
        content="c",
        content_hash="h",
        canonical_content_hash=None,
        evidence_manifest_hash=None,
        publication_profile=None,
        review_reason="legacy",
    )
    assert legacy.legal_prohibition_catalog_version is None


# ============================================================
# 3. 失败投影: mark_failure_on_cursor SQL 带版本
# ============================================================
class _RecordingCursor:
    def __init__(self, rowcount: int = 1):
        self.rowcount = rowcount
        self.calls: list = []

    def execute(self, sql, params=None):
        self.calls.append((sql, params))


def test_failure_projection_writes_catalog_version() -> None:
    from services.article_generation_status import mark_failure_on_cursor
    from writing.article_generation_failure import ArticleProviderUnavailable

    cur = _RecordingCursor(rowcount=1)
    ok = mark_failure_on_cursor(cur, topic_id=123, failure=ArticleProviderUnavailable().failure)
    assert ok is True
    sql, params = cur.calls[0]
    assert "generation_legal_catalog_version=%s" in sql
    assert LEGAL_PROHIBITION_CATALOG_VERSION in params


def test_rewrite_failure_inline_update_carries_catalog_version() -> None:
    """重写失败的内联 UPDATE(第二失败投影写点)同样冻结版本。"""
    src = open(os.path.join(ROOT, "writing/article_generator_service.py"), encoding="utf-8").read()
    anchor = src.index("generation_failure_phase=%s,generation_legal_catalog_version=%s")
    assert "LEGAL_PROHIBITION_CATALOG_VERSION" in src[: anchor + 4000]


def test_full_reset_clears_failure_projection_version() -> None:
    """full_reset 清空失败字段时同步清空版本,投影语义保持一致。"""
    src = open(os.path.join(ROOT, "services/article_generation_reset.py"), encoding="utf-8").read()
    assert "generation_retryable=NULL,generation_failure_phase=NULL," in src
    assert "generation_legal_catalog_version=NULL" in src


def test_bootstrap_self_heal_registers_failure_projection_column() -> None:
    """启动自检(_safe_add_column)兜底注册新列,防 migration 未跑环境 SELECT 报错。"""
    src = open(os.path.join(ROOT, "db/diagnosis_db.py"), encoding="utf-8").read()
    assert '_safe_add_column(cursor, "topics", "generation_legal_catalog_version", "VARCHAR(64)")' in src


# ============================================================
# 4. 渠道提交冻结快照: set_item_submission_snapshot
# ============================================================
class _FakeConn:
    def __init__(self):
        self.cursor_obj = _RecordingCursor(rowcount=0)
        self.committed = False
        self.closed = False

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.committed = True

    def close(self):
        self.closed = True


def test_mhz_submission_snapshot_freezes_catalog_version(monkeypatch) -> None:
    import db.meijiehezi_db as mhz_db

    fake = _FakeConn()
    monkeypatch.setattr(mhz_db, "_get_conn", lambda: fake)
    changed = mhz_db.set_item_submission_snapshot(
        [5], title="t", content="c", source="unit_test",
        legal_catalog_version=LEGAL_PROHIBITION_CATALOG_VERSION,
    )
    assert changed == 0  # 假 cursor rowcount=0,只验证 SQL 形状
    sql, params = fake.cursor_obj.calls[0]
    assert "submitted_legal_catalog_version=%s" in sql
    assert LEGAL_PROHIBITION_CATALOG_VERSION in params


def test_mhz_submission_snapshot_omits_version_column_for_legacy_callers(monkeypatch) -> None:
    """缺省(旧调用方)不写版本列 → 历史行保持 NULL,不回填。"""
    import db.meijiehezi_db as mhz_db

    fake = _FakeConn()
    monkeypatch.setattr(mhz_db, "_get_conn", lambda: fake)
    mhz_db.set_item_submission_snapshot([5], title="t", content="c", source="unit_test")
    sql, _params = fake.cursor_obj.calls[0]
    assert "submitted_legal_catalog_version" not in sql


def test_mhz_bootstrap_self_heal_registers_snapshot_column() -> None:
    src = open(os.path.join(ROOT, "db/meijiehezi_db.py"), encoding="utf-8").read()
    assert (
        "ALTER TABLE mhz_publish_order_items ADD COLUMN IF NOT EXISTS "
        "submitted_legal_catalog_version VARCHAR(64)"
    ) in src


def test_all_dispatch_snapshot_writers_thread_catalog_version() -> None:
    """6 个 snapshot_writer 调用点全部把 snap.legal_prohibition_catalog_version
    传入 set_item_submission_snapshot(不漏一个写点)。"""
    expected = {
        "api/meijiehezi_api.py": 4,
        "api/scheduler.py": 2,
    }
    for rel, count in expected.items():
        src = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        needle = "legal_catalog_version=snap.legal_prohibition_catalog_version"
        assert src.count(needle) == count, f"{rel} 只有 {src.count(needle)}/{count} 个写点传版本"


# ============================================================
# 5. 文章级不可变发布快照: articles.publication_snapshot
# ============================================================
class _PublicationCursor:
    """模拟 capture_publication_snapshot_with_cursor 的 SELECT→UPDATE 序列。"""

    def __init__(self, select_row):
        self._select_row = select_row
        self.calls: list = []
        self._fetches = [select_row, {"id": select_row["id"]}]

    def execute(self, sql, params=None):
        self.calls.append((sql, params))

    def fetchone(self):
        return self._fetches.pop(0) if self._fetches else None


def _article_row(generation_snapshot):
    return {
        "id": 7,
        "title": "隔离器选型指南",
        "content": "正文",
        "version": 3,
        "style": "buying_guide",
        "style_code": "buying_guide",
        "style_family": "implementation_guide",
        "style_contract_version": "v1",
        "style_version": "v1",
        "evidence_manifest_hash": None,
        "brand_snapshot_hash": None,
        "current_content_hash": None,
        "publication_profile": "standard",
        "article_review_status": "approved",
        "platform_review": None,
        "generation_request_snapshot": generation_snapshot,
        "publication_snapshot_at": None,
        "publication_snapshot_hash": None,
    }


def _captured_snapshot(cur: _PublicationCursor) -> dict:
    update_sql, update_params = cur.calls[-1]
    assert "publication_snapshot = %s" in update_sql
    json_param = update_params[0]
    return json_param.adapted  # psycopg2.extras.Json 包装前的原始 dict


def test_publication_snapshot_propagates_lineage_catalog_version() -> None:
    from services.article_publication_snapshot import (
        capture_publication_snapshot_with_cursor,
    )

    cur = _PublicationCursor(_article_row({
        "legal_prohibition_catalog_version": CURRENT_CATALOG_VERSION,
    }))
    result = capture_publication_snapshot_with_cursor(
        cur, article_id=7, source="unit_test", source_id=1, success_state="published",
    )
    assert result["captured"] is True
    snapshot = _captured_snapshot(cur)
    assert snapshot["legal_prohibition_catalog_version"] == CURRENT_CATALOG_VERSION


def test_publication_snapshot_keeps_none_for_legacy_article_without_backfill() -> None:
    """历史文章 generation_request_snapshot 无版本 → 快照版本=None,不以当前版本回填。"""
    from services.article_publication_snapshot import (
        capture_publication_snapshot_with_cursor,
    )

    cur = _PublicationCursor(_article_row(None))
    result = capture_publication_snapshot_with_cursor(
        cur, article_id=7, source="unit_test", source_id=1, success_state="published",
    )
    assert result["captured"] is True
    snapshot = _captured_snapshot(cur)
    assert snapshot["legal_prohibition_catalog_version"] is None


def test_publication_snapshot_tolerates_stringified_generation_snapshot() -> None:
    """generation_request_snapshot 以 JSON 字符串落库的旧驱动行为也能解析。"""
    from services.article_publication_snapshot import (
        capture_publication_snapshot_with_cursor,
    )

    cur = _PublicationCursor(_article_row('{"legal_prohibition_catalog_version": "ad-law-art9-absolute-v7"}'))
    result = capture_publication_snapshot_with_cursor(
        cur, article_id=7, source="unit_test", source_id=1, success_state="published",
    )
    assert result["captured"] is True
    assert _captured_snapshot(cur)["legal_prohibition_catalog_version"] == "ad-law-art9-absolute-v7"


# ============================================================
# 6. PG16 真库冒烟: 新列存在 + 两条 UPDATE 语句在真库可执行
#    (id=-1 匹配 0 行,不改动任何数据;语句失败说明列缺失/类型错)
# ============================================================
def test_pg16_columns_exist_and_projection_statements_execute() -> None:
    from db.diagnosis_db import _safe_add_column
    from db.meijiehezi_db import init_mhz_tables

    init_mhz_tables()

    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = True
    try:
        cur = conn.cursor()
        # 与启动自检相同的幂等自愈(测试库一次性,可反复跑)
        _safe_add_column(cur, "topics", "generation_legal_catalog_version", "VARCHAR(64)")
        cur.execute(
            """
            SELECT data_type FROM information_schema.columns
             WHERE table_name='topics' AND column_name='generation_legal_catalog_version'
            """
        )
        assert cur.fetchone()[0] == "character varying"
        cur.execute(
            """
            SELECT data_type FROM information_schema.columns
             WHERE table_name='mhz_publish_order_items'
               AND column_name='submitted_legal_catalog_version'
            """
        )
        assert cur.fetchone()[0] == "character varying"

        # 失败投影语句真库可执行(0 行命中 → False,不抛异常即通过)
        from services.article_generation_status import mark_failure_on_cursor
        from writing.article_generation_failure import ArticleProviderUnavailable

        assert mark_failure_on_cursor(
            cur, topic_id=-1, failure=ArticleProviderUnavailable().failure
        ) is False

        # 渠道快照语句真库可执行(0 行命中 → 0)
        import db.meijiehezi_db as mhz_db

        monkey_conn = _FakeConn()
        real_execute = cur.execute
        monkey_conn.cursor_obj.execute = lambda sql, params=None: real_execute(sql, params)
        assert (
            mhz_db.set_item_submission_snapshot(
                [-1], title="t", content="c", source="unit_test",
                legal_catalog_version=LEGAL_PROHIBITION_CATALOG_VERSION,
            )
            == 0
        )
    finally:
        conn.close()
