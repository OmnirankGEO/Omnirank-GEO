"""[存量救治]稿件正文清洗脚本的判别。

Owner 点名三条:
1. 清洗前后**合法内容零丢失**(先红后绿 —— 这条判别在 sanitizer 有吞正文缺陷时必须红);
2. **已发布稿零触碰**(快照不可变是底线);
3. 清洗后**能正常过发布口**(哈希变了要重走审核,否则就是自己再踩一遍 P0 的坑)。

外加幂等与在途订单避让 —— 后者正是 P0 的根因,清洗脚本自己绝不能制造同一个坑。
"""
import inspect

import pytest

from scripts import sanitize_legacy_article_bodies as sanitizer
from writing.body_internal_marker_sanitizer import sanitize_article_body


# ---------------------------------------------------------------- 1. 零丢失

def test_legitimate_sentences_survive_sanitization():
    """同一行里既有内部记号又有正经内容时,正经内容必须留下。

    这条是"先红后绿"的那条:整行删的实现会把首句一起吞掉。
    """
    body = "本品采用低温萃取工艺。待核验:该数据尚未取得第三方报告。"
    cleaned, _ = sanitize_article_body(body)
    assert "本品采用低温萃取工艺。" in cleaned, "合法首句被连带删除 = 吞正文"


def test_sanitizer_never_returns_empty_body():
    """洗光了宁可留记号也不能丢稿。"""
    body = "待核验:全文都是待核验句式。"
    cleaned, _ = sanitize_article_body(body)
    assert cleaned.strip(), "清洗把全文洗光 = 丢稿"


def test_clean_body_is_unchanged_idempotent():
    body = "这是一段完全干净的正文,没有任何内部记号。"
    once, _ = sanitize_article_body(body)
    twice, _ = sanitize_article_body(once)
    assert once == body
    assert twice == once


# ---------------------------------------------------------------- 2. 硬边界

@pytest.mark.parametrize("row,expected", [
    ({"first_published_at": "2026-07-01", "status": "draft", "content": "x"}, "already_published"),
    ({"status": "published", "content": "x"}, "status_not_eligible:published"),
    ({"status": "draft", "content": "x", "has_immutable_snapshot": True}, "immutable_snapshot"),
    ({"status": "draft", "content": "x", "has_active_publication": True}, "active_publication_order"),
    ({"status": "draft", "content": "   "}, "empty_body"),
    ({"status": "draft", "content": "正文"}, None),
])
def test_hard_boundaries(row, expected):
    assert sanitizer._is_skippable(row) == expected


def test_published_articles_are_never_touched():
    """快照不可变 —— 清洗一篇已发布的稿子等于篡改已交付物。"""
    assert sanitizer._is_skippable(
        {"first_published_at": "2026-07-01", "status": "draft", "content": "有内部记号"}
    ) == "already_published"


def test_active_publication_orders_are_skipped():
    """这正是 P0 的根因:窗口内改稿 → 订单快照失配 → 客户被拖进死锁。"""
    assert sanitizer._is_skippable(
        {"status": "draft", "content": "x", "has_active_publication": True}
    ) == "active_publication_order"


def test_candidate_query_computes_both_publication_flags():
    """两个硬边界必须在**同一次查询**里算出来,不能逐篇再问(N+1 且易漏)。"""
    src = inspect.getsource(sanitizer._select_candidates)
    assert "has_active_publication" in src and "has_immutable_snapshot" in src
    assert "awaiting_action" in src, "挂起态也算在途,漏了会在挂起期间改稿"


# ---------------------------------------------------------------- 3. 清洗后能过发布口

def test_hash_change_triggers_review_refresh():
    """哈希变了却不重走审核 = 发布口 hash 失配,等于自己再造一次 P0。"""
    src = inspect.getsource(sanitizer.run_sanitize_legacy_bodies)
    assert "refresh_article_review" in src
    assert "article_review_status" in src, "只对有审核记录的刷新,避免给无审核的凭空造记录"


def test_backup_is_written_for_rollback():
    src = inspect.getsource(sanitizer.run_sanitize_legacy_bodies)
    assert "original_content" in src and "original_hash" in src
    assert sanitizer.BACKUP_KEY == "sanitize_backup"


# ---------------------------------------------------------------- 幂等与运行方式

def test_dry_run_writes_nothing():
    src = inspect.getsource(sanitizer.run_sanitize_legacy_bodies)
    body = src[src.index("if dry_run:"):]
    assert "continue" in body[:80], "dry-run 必须在写库之前就返回"


def test_unchanged_articles_are_not_rewritten():
    """幂等的关键:已经干净的稿子不写库,重复跑不产生新版本。"""
    src = inspect.getsource(sanitizer.run_sanitize_legacy_bodies)
    assert "if cleaned == original:" in src


def test_script_is_manual_only_not_cron():
    """Owner 要求:挂 admin 手动动作,不进 cron。"""
    from pathlib import Path

    root = Path(__file__).parent.parent
    src = (root / "scripts" / "sanitize_legacy_article_bodies.py").read_text(encoding="utf-8")
    assert "--dry-run" in src and "--apply" in src
    assert "不进 cron" in src

    # 全仓不得有调度器引用它
    scheduler = (root / "api" / "scheduler.py").read_text(encoding="utf-8")
    assert "sanitize_legacy_article_bodies" not in scheduler, "被挂进 cron 了"


def test_apply_and_dry_run_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        sanitizer.main(["--dry-run", "--apply"])
    with pytest.raises(SystemExit):
        sanitizer.main([])


# ---------------------------------------------------------------- 原子性与真实计数
#
# 实测踩到的坑:refresh_article_review 抛异常后 psycopg2 事务进入 aborted 态,
# 随后的 commit 实际等于 ROLLBACK —— 已改的正文被一起撤掉,报表却还在报
# "已清洗 N 篇"。**假成功**。下面两条锁死修复。

class _RecordingCursor:
    """记录 SQL 的受控游标(不碰真库,避免把判别绑到 schema 完整度上)。"""

    def __init__(self, rows):
        self._rows = rows
        self.sql: list[str] = []

    def execute(self, sql, params=None):
        self.sql.append(" ".join(str(sql).split()))

    def fetchall(self):
        return self._rows


def _dirty_row(article_id=1, review="passed"):
    return {
        "id": article_id, "title": "带记号稿", "status": "draft",
        "content": "本品采用低温萃取工艺。待核验:该数据尚未取得第三方报告。",
        "first_published_at": None, "article_review_status": review,
        "has_active_publication": False, "has_immutable_snapshot": False,
    }


def test_review_refresh_failure_rolls_back_the_whole_article(monkeypatch):
    """审核刷不了 → 连正文一起回退。留下"内容变了、审核还是旧的"就是 P0 那个坑。"""
    import services.article_review_gate as gate

    def boom(*a, **k):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(gate, "refresh_article_review", boom)
    cursor = _RecordingCursor([_dirty_row()])
    report = sanitizer.run_sanitize_legacy_bodies(dry_run=False, cursor=cursor)

    assert report["would_change"] == 1
    assert report["changed"] == 0, "审核失败还计入已清洗 = 假成功"
    assert report["failed"] == 1
    assert any("ROLLBACK TO SAVEPOINT" in q for q in cursor.sql), "没回退 = 留下危险中间态"
    assert not any("RELEASE SAVEPOINT" in q for q in cursor.sql)


def test_successful_article_is_released_and_counted(monkeypatch):
    import services.article_review_gate as gate

    monkeypatch.setattr(gate, "refresh_article_review", lambda *a, **k: None)
    cursor = _RecordingCursor([_dirty_row()])
    report = sanitizer.run_sanitize_legacy_bodies(dry_run=False, cursor=cursor)

    assert report["changed"] == 1
    assert report["review_refreshed"] == 1
    assert report.get("failed", 0) == 0
    assert any("RELEASE SAVEPOINT" in q for q in cursor.sql)
    assert any("UPDATE articles" in q for q in cursor.sql)


def test_each_article_gets_its_own_savepoint(monkeypatch):
    """一篇出错不得毒化整批。"""
    import services.article_review_gate as gate

    monkeypatch.setattr(gate, "refresh_article_review", lambda *a, **k: None)
    cursor = _RecordingCursor([_dirty_row(1), _dirty_row(2)])
    sanitizer.run_sanitize_legacy_bodies(dry_run=False, cursor=cursor)
    assert len([q for q in cursor.sql if q.startswith("SAVEPOINT")]) == 2


def test_apply_summary_prints_actual_changed_not_candidates(capsys, monkeypatch):
    """汇总行必须打印**真实清洗数**,不能拿命中数冒充(我第一版就是这么骗了自己)。"""
    import services.article_review_gate as gate

    def boom(*a, **k):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(gate, "refresh_article_review", boom)
    monkeypatch.setattr(
        sanitizer, "run_sanitize_legacy_bodies",
        lambda **k: {"dry_run": False, "scanned": 4, "would_change": 1, "changed": 0,
                     "review_refreshed": 0, "failed": 1, "skipped": {},
                     "failures": [{"article_id": 2, "error": "boom"}], "items": []},
    )
    sanitizer.main(["--apply"])
    out = capsys.readouterr().out
    assert "实际清洗 0 篇" in out
    assert "回退 1 篇" in out
