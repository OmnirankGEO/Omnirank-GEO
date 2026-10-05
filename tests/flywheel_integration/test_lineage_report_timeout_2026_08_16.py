"""锁:血缘三报「无超时 + 事务跨网络 I/O」P0(2026-08-16 把生产堵死过一次)。

事故链(工单 §0 · 实证不是推测):
    5 条血缘三报请求在生产 active,xact 3831–3865s(≈64 分钟)不结束
    → 第 30 班 prestart 重放迁移的 ALTER 排在它们后面(granted=f)
    → PG 锁队列 FIFO,ALTER 后面所有 SELECT 全排死
    → 客户页挂 + 飞轮全景 fail-soft 全零 + 面板显示「运行正常」

🔴 两个根因,必须分开锁,少锁一个都会复发:

  ① 事务跨应用侧网络 I/O(**本次 64 分钟的真形态**)
     `_load_rows` 原本在 `cur.execute()` 之后、`conn.close()` 之前调
     `_backfill_oss_bodies` —— 那是最多 1000 次未缓存 OSS 下载
     (corpus-export 传 limit=1000),全程扛着 ACCESS SHARE 锁。
     ⚠️ statement_timeout 对此**完全无效**:SQL 早就跑完了,慢的是事务不关。
        实证 .probe/lock_chain.sh:A 态(带 5s statement_timeout)ALTER 照堵。
     → 锁「接线」:OSS 回读必须发生在事务**之外**。

  ② 那条 SQL 本身跑 10.6s(生产形状库实测),且随 corpus_grade 命中数线性劣化。
     根因是 geo_research_articles 的 8 个 AND 谓词被估成 rows=1(实际 781),
     PG 选 Nested Loop 把整棵聚合子树**逐行重扫 781 次**
     (Rows Removed by Join Filter: 12,965,496)。
     → 锁「两级 MAX 折叠 + AS MATERIALIZED」两个结构不变量。

每条「必须命中」都配成对的「必须不命中」—— 单向断言证明不了判别力。
"""
from __future__ import annotations

import re

import psycopg2
import pytest

import services.article_structure_analysis as asa
from services.article_structure_analysis import (
    ARTICLE_STRUCTURE_ALL_SQL,
    ARTICLE_STRUCTURE_SQL,
    ReportComputeTimeout,
    _timeout_ms,
)

def _strip_sql_comments(sql: str) -> str:
    """剥掉 `--` 注释再做存在性断言。

    🔴 不剥的话,**解释这个改写的注释本身**会命中断言 —— 第一版就栽在这:
    注释里写了 `article_source_signal  GROUP BY (article_id, cite_url)`,
    于是「中间层必须消失」恒红。与工具箱 README 记的
    「交付文档描述这个扫描本身也会命中(一天踩三次)」同型。
    """
    return "\n".join(re.sub(r"--.*$", "", line) for line in sql.splitlines())


BOTH_SQL = (("ARTICLE_STRUCTURE_SQL", _strip_sql_comments(ARTICLE_STRUCTURE_SQL)),
            ("ARTICLE_STRUCTURE_ALL_SQL", _strip_sql_comments(ARTICLE_STRUCTURE_ALL_SQL)))


def test_comment_stripper_actually_strips():
    """必须不命中(反向对照):证明剥注释这一步真的在干活。

    若 _strip_sql_comments 变成恒等函数,上面所有结构断言都会被注释干扰,
    要么恒红要么恒绿 —— 先证明这把尺子是准的。
    """
    assert _strip_sql_comments("SELECT 1 -- article_source_signal 出现在注释里\n").strip() == "SELECT 1"
    assert "article_source_signal" in ARTICLE_STRUCTURE_SQL, \
        "注释里本来就该留有这段改写说明;它不在了说明锚点漂了,本对照失去意义"


# ─────────────────────────── ② SQL 结构不变量 ───────────────────────────

def test_article_signal_is_materialized():
    """必须命中:两条 SQL 的 article_signal 都必须是 AS MATERIALIZED。

    不是风格偏好 —— 去掉它,PG 会把这棵聚合子树当 Nested Loop 内表逐行重扫。
    """
    for name, sql in BOTH_SQL:
        assert re.search(r"article_signal\s+AS\s+MATERIALIZED\s*\(", sql), \
            f"{name}: article_signal 必须 AS MATERIALIZED,否则 nested-loop 重扫复发"


def test_redundant_two_level_aggregation_is_gone():
    """必须不命中:中间层 article_source_signal 必须已被折叠掉。

    它存在就说明又变回两级聚合(每层都会被重扫一次)。
    """
    for name, sql in BOTH_SQL:
        assert "article_source_signal" not in sql, \
            f"{name}: 两级聚合已折叠成一级,不该再出现 article_source_signal"


def test_collapsed_aggregate_groups_by_article_id_only():
    """必须命中:折叠后按 article_id 单列分组(不再带 cite_url)。

    MAX(MAX(x)) ≡ MAX(x) 只在**去掉细分组**时才成立;若还留着
    `GROUP BY citation.article_id, raw.cite_url` 就是没折叠干净。
    """
    for name, sql in BOTH_SQL:
        assert "GROUP BY citation.article_id\n" in sql, f"{name}: 应按 article_id 一级分组"
        assert "GROUP BY citation.article_id, raw.cite_url" not in sql, \
            f"{name}: 仍在按 (article_id, cite_url) 细分组 —— 没折叠"


def test_all_collapsed_aggregates_are_max():
    """必须命中:折叠后的 8 个聚合**全部**是 MAX。

    折叠的等价性依据就是「全 MAX」(MAX∘MAX ≡ MAX)。任何一个变成
    MIN/SUM/AVG,恒等式立刻不成立、报表口径静默改变
    (.probe/sql_equivalence.py 已在构造夹具上实证这 5 种毒各自会改结果)。
    """
    cols = ("is_adopted", "is_cited", "is_search_only", "source_weight", "engine_count",
            "is_adopted_legacy", "is_cited_legacy", "is_search_only_legacy")
    for name, sql in BOTH_SQL:
        for col in cols:
            assert f"MAX(COALESCE(sig.{col}, 0)) AS {col}" in sql, \
                f"{name}: {col} 必须是 MAX(折叠等价性依赖全 MAX)"
            for bad in ("MIN", "SUM", "AVG"):
                assert f"{bad}(COALESCE(sig.{col}, 0)) AS {col}" not in sql, \
                    f"{name}: {col} 用了 {bad} —— 折叠不再等价"


# ────────────────────── ① 接线锁:事务不许跨网络 I/O ──────────────────────

class _FakeCursor:
    def __init__(self, log, rows_by_call):
        self._log = log
        self._rows_by_call = rows_by_call
        self._calls = 0
        self._last: list = []

    def execute(self, sql, params=None):
        self._log.append(("execute", " ".join(str(sql).split())[:60]))
        self._last = self._rows_by_call(self._calls, sql)
        self._calls += 1

    def fetchall(self):
        return self._last

    def fetchone(self):
        return self._last[0] if self._last else None


class _FakeConn:
    def __init__(self, log, rows_by_call):
        self._log = log
        self._cur = _FakeCursor(log, rows_by_call)
        self.closed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self._log.append(("commit", ""))

    def rollback(self):
        self._log.append(("rollback", ""))

    def close(self):
        self.closed = True
        self._log.append(("close", ""))


def _install_fake_db(monkeypatch, rows_by_call):
    log: list[tuple[str, str]] = []
    conn = _FakeConn(log, rows_by_call)
    monkeypatch.setattr(asa, "get_connection", lambda: conn)
    return log, conn


def _rows_for(_call_idx, sql):
    """resolve_effective_corpus_grade 要一行 grade;主查询给一行带 oss_key 的文章。"""
    text = str(sql)
    if "corpus_grade, COUNT(*)" in text or "GROUP BY corpus_grade" in text:
        return [{"corpus_grade": "JC5", "n": 1}]
    if "set_config" in text:
        return [{"set_config": "30000"}]
    return [{
        "id": 1, "url": "https://x.test/a", "domain": "x.test", "title": "t",
        "oss_key_cleaned": "oss/a.md", "inline_cleaned_content": "",
        "cleaned_char_count": 900, "source_weight": 1, "engine_count": 1,
        "is_adopted": 1, "is_cited": 0, "is_search_only": 0,
        "is_adopted_legacy": 0, "is_cited_legacy": 0, "is_search_only_legacy": 0,
        "group_rank": 1, "signal_measured": True,
    }]


@pytest.mark.parametrize("industry", ["general", "装修建材"])
def test_oss_backfill_runs_only_after_transaction_is_closed(monkeypatch, industry):
    """🔴 必须命中(接线锁):`_backfill_oss_bodies` 被调用时,DB 连接必须**已经归还**。

    这是本次 P0 的核心。OSS 回读是上千次未缓存网络下载;只要它还在事务里,
    事务就会老到几十分钟,ACCESS SHARE 锁把 prestart 的 ALTER 卡住,
    PG FIFO 再把全站 SELECT 排死 —— 与 SQL 快不快无关。

    两个分支(general / 具体行业)各锁一次:2026-08-16 之前**两条路径都在事务里**回读。
    """
    log, conn = _install_fake_db(monkeypatch, _rows_for)
    seen: dict = {}

    def _fake_backfill(rows, cap=None):
        seen["closed_when_called"] = conn.closed
        seen["log_at_call"] = list(log)
        return 0

    monkeypatch.setattr(asa, "_backfill_oss_bodies", _fake_backfill)
    asa._load_rows(industry, limit=10, min_chars=500)

    assert seen, "_backfill_oss_bodies 没被调用 —— 判据打空(不是通过)"
    assert seen["closed_when_called"] is True, (
        "OSS 回读发生在事务/连接还开着的时候 —— P0 复发:"
        f"调用时的连接日志={seen['log_at_call']}"
    )


def test_backfill_is_actually_wired(monkeypatch):
    """必须不命中(反向对照):证明上面那条锁打的是**真的会跑的**那行代码。

    若哪天 `_backfill_oss_bodies` 被从 `_load_rows` 摘掉,上面的断言会因为
    `seen` 为空而报「判据打空」,而不是悄悄变成恒真。这里直接正面证明它被接着线。
    """
    log, _conn = _install_fake_db(monkeypatch, _rows_for)
    calls: list[int] = []
    monkeypatch.setattr(asa, "_backfill_oss_bodies", lambda rows, cap=None: calls.append(1) or 0)
    asa._load_rows("general", limit=10, min_chars=500)
    assert calls, "_load_rows 没调用 _backfill_oss_bodies —— 接线断了"


def test_transaction_sets_both_timeouts(monkeypatch):
    """必须命中:事务开头必须同时下两条闸。

    只下 statement_timeout 是工单原方案 —— 实证拦不住本次事故形态
    (.probe/lock_chain.sh A 态:带 5s statement_timeout,ALTER 照堵)。
    """
    log, _conn = _install_fake_db(monkeypatch, _rows_for)
    monkeypatch.setattr(asa, "_backfill_oss_bodies", lambda rows, cap=None: 0)
    asa._load_rows("general", limit=10, min_chars=500)
    executed = " | ".join(sql for kind, sql in log if kind == "execute")
    assert "statement_timeout" in executed, "没下 statement_timeout"
    assert "idle_in_transaction_session_timeout" in executed, (
        "没下 idle_in_transaction_session_timeout —— 只有它能掐住"
        "「SQL 跑完了但事务不关」这个真形态")


# ───────────────────── 超时必须冒泡成 degraded,不许吞成 0 ─────────────────────

def test_query_canceled_becomes_report_compute_timeout(monkeypatch):
    """必须命中:statement_timeout 触发 → 抛 ReportComputeTimeout。

    🔴 绝不能返回空 rows —— 那会让上层算出「0 篇 / 0.00 倍」当成真实结论,
    正是 2026-08-16 事故被伪装成「运行正常」的机制。
    """
    def _boom(_call_idx, sql):
        if "set_config" in str(sql):
            return [{"set_config": "30000"}]
        raise psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")

    _install_fake_db(monkeypatch, _boom)
    monkeypatch.setattr(asa, "_backfill_oss_bodies", lambda rows, cap=None: 0)
    with pytest.raises(ReportComputeTimeout):
        asa._load_rows("general", limit=10, min_chars=500)


def test_non_timeout_errors_still_propagate(monkeypatch):
    """必须不命中:普通 SQL 错误不许被错标成「超时」(否则真 bug 被伪装成慢)。"""
    def _boom(_call_idx, sql):
        if "set_config" in str(sql):
            return [{"set_config": "30000"}]
        raise psycopg2.errors.UndefinedColumn('column "nope" does not exist')

    _install_fake_db(monkeypatch, _boom)
    monkeypatch.setattr(asa, "_backfill_oss_bodies", lambda rows, cap=None: 0)
    with pytest.raises(psycopg2.errors.UndefinedColumn):
        asa._load_rows("general", limit=10, min_chars=500)


# ───────────────────────────── 超时时长可配 ─────────────────────────────

def test_timeout_is_configurable_and_clamped(monkeypatch):
    """必须命中:可配置;必须不命中:0/负/垃圾值不许变成「无限等待」。"""
    monkeypatch.setenv("X_T", "5000")
    assert _timeout_ms("X_T", 30_000) == 5_000
    monkeypatch.delenv("X_T", raising=False)
    assert _timeout_ms("X_T", 30_000) == 30_000          # 未配 → 默认 30s
    monkeypatch.setenv("X_T", "0")
    assert _timeout_ms("X_T", 30_000) == 1_000           # 🔴 0 在 PG 里是"不限",必须夹住
    monkeypatch.setenv("X_T", "-1")
    assert _timeout_ms("X_T", 30_000) == 1_000
    monkeypatch.setenv("X_T", "not-a-number")
    assert _timeout_ms("X_T", 30_000) == 30_000
    monkeypatch.setenv("X_T", "99999999")
    assert _timeout_ms("X_T", 30_000) == 600_000         # 上限 10min,防手滑写成永远


# ─────────── panorama fail-soft 必须留痕(事故当天靠它伪装成「运行正常」)───────────

def test_panorama_marks_degraded_when_queries_fail(monkeypatch):
    """必须命中:取数失败被 fail-soft 吞成 0 时,必须置 degraded=True。

    2026-08-16 全站被锁排死,本面板每一项都静静吞成 0,照常渲染
    「采集 0 / 学习 0 · 运行正常」—— 事故被伪装成正常态,排查方按
    「真没有数据」查了两天。0 本身保留(下游按 int 消费),但必须留痕。
    """
    import services.flywheel_panorama as fp

    class _FailingConn:
        """取到连接了,但**查询**失败 —— 这才是被 fail-soft 吞成 0 的那条路径。

        注意 `get_connection()` 本身在 try 之外(连接池耗尽会直接 500,是**响亮**失败,
        故意不改成 fail-soft:把它也吞掉才是更坏的病)。
        """

        def cursor(self):
            return self

        def execute(self, *a, **k):
            raise RuntimeError("canceling statement due to lock timeout")

        def close(self):
            return None

    monkeypatch.setattr(fp, "get_connection", lambda: _FailingConn())
    monkeypatch.setattr(fp, "_active_writing_versions", lambda: 0)
    monkeypatch.setattr(fp, "_pending_reviews", lambda: {"binding_candidates": 0,
                                                         "writing_drafts": 0,
                                                         "engine_weight_candidates": 0})
    out = fp.get_flywheel_panorama()
    assert out["degraded"] is True, "取数全失败却没标 degraded —— 事故会再次被伪装成正常"
    assert out["degraded_reasons"], "degraded=True 却没说是哪一项失败,排查方无从下手"
    # 🔴 只断言「degraded 为真」不够:_scalar 与 _scalar_opt 各自留痕,
    #    任一条掉了,另一条仍会把 degraded 顶成 True → 判据看不见那次回归。
    #    (实测:M7 摘掉 _scalar 的留痕,第一版断言照样全绿。)
    #    所以必须逐个来源核对,而不是只看总开关。
    prefixes = {r.split(":")[0] for r in out["degraded_reasons"]}
    assert "scalar" in prefixes, "_scalar 的失败没留痕(3 个计数查询失败却查不到是谁)"
    assert "scalar_opt" in prefixes, "_scalar_opt 的失败没留痕"
    assert len(out["degraded_reasons"]) >= 4, (
        f"4 个查询全失败,却只记了 {len(out['degraded_reasons'])} 条:{out['degraded_reasons']}")
    # 数值仍是 0(不改下游契约),但现在有标记能把两者分开
    assert out["nodes"][0]["value"] == 0


def test_panorama_not_degraded_when_healthy(monkeypatch):
    """必须不命中(反向对照):一切正常时 degraded 必须是 False。

    没有这条,上面那条可能只是「degraded 恒为 True」—— 恒真和恒假一样废。
    """
    import services.flywheel_panorama as fp

    class _C:
        def cursor(self):
            return self

        def execute(self, *a, **k):
            return None

        def fetchone(self):
            return {"count": 7}

        def close(self):
            return None

    monkeypatch.setattr(fp, "get_connection", lambda: _C())
    monkeypatch.setattr(fp, "_active_writing_versions", lambda: 1)
    monkeypatch.setattr(fp, "_pending_reviews", lambda: {"binding_candidates": 0,
                                                         "writing_drafts": 0,
                                                         "engine_weight_candidates": 0})
    out = fp.get_flywheel_panorama()
    assert out["degraded"] is False, "健康时也报 degraded → 标记恒真,零判别力"
    assert out["degraded_reasons"] == []
