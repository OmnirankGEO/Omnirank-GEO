"""投放结果事实表的锁(Review NO-GO 2026-08-09 返工)。

纪律:**每条「必须命中」都配一条成对的「必须不命中」**。
只验「命中」的锁,在正反两侧同时失效时会全绿 —— 那是零判别力的假绿。

这里的用例全部是**构造**的:生产真数据跑不出「引用早于发布」「search_citations 坏 JSON」
「窗口刚好边界」这几个分支(实测生产 0 行 '[]'、0 行坏 JSON),只跑真数据必然全绿。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from services.publication_outcome_facts import (
    FACTS_METRIC_VERSION,
    OUTCOME_TABLE,
    citation_rank_for,
    _outcome_rows_for,
)
from services.strict_article_outcomes import URL_NORMALIZATION_VERSION, parse_citation_urls

PUB_AT = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)
OUR_URL = "https://www.example.com/our-article"
OTHER_URL = "https://www.example.com/somebody-else"

# _outcome_rows_for 返回的 tuple 列序(与 _OUTCOME_UPSERT 的 VALUES 一一对应)
I_WINDOW_COMPLETE = 13
I_TESTS_TOTAL = 14
I_TESTS_OBSERVABLE = 15
I_UNPARSABLE = 16
I_GATE1 = 17
I_FIRST_CITED = 18
I_BEST_RANK = 19


def _pub():
    return {"id": 1, "brand_id": 42, "media_id": 7, "media_name": "示例媒体",
            "_url": OUR_URL, "_published_at": PUB_AT}


def _citations(*urls, ranks=None):
    items = []
    for idx, url in enumerate(urls):
        item = {"url": url, "title": "t"}
        if ranks is not None:
            item["rank"] = ranks[idx]
        items.append(item)
    return json.dumps(items)


def _mr(*, offset_hours=1, citations=None, keyword="关键词A", platform="deepseek"):
    return {
        "id": 100 + offset_hours, "brand_id": 42, "keyword": keyword, "platform": platform,
        "tested_at": PUB_AT + timedelta(hours=offset_hours),
        "search_citations": citations,
    }


def _build(rows, window_days=30, clock=None):
    summary = {k: 0 for k in (
        "tests_total", "tests_observable", "citations_unparsable", "gate1_url_cited")}
    out = _outcome_rows_for(_pub(), rows, window_days,
                            clock or (PUB_AT + timedelta(days=365)), summary)
    return out, summary


# ══════════════════════════════════════════════════════════════════
# P0-2 观察窗:发布之前的命中不算这篇文章的战果
# ══════════════════════════════════════════════════════════════════

def test_citation_before_publish_is_excluded():
    """必须不命中:引用发生在发布**之前** → 整行不产出(窗口内零监测)。"""
    rows, _ = _build([_mr(offset_hours=-1, citations=_citations(OUR_URL))])
    assert rows == [], "发布前的监测被算进了窗口 —— 这正是 Review P0-2 抓的那个洞"


def test_citation_after_publish_is_counted():
    """成对的必须命中:同样一条引用,挪到发布**之后** → 必须计入。"""
    rows, _ = _build([_mr(offset_hours=+1, citations=_citations(OUR_URL))])
    assert len(rows) == 1
    assert rows[0][I_GATE1] == 1


def test_window_is_half_open_interval():
    """边界:published_at 那一刻算进来,published_at+window 那一刻算出去。"""
    at_start = _mr(offset_hours=0, citations=_citations(OUR_URL))
    at_end = _mr(offset_hours=7 * 24, citations=_citations(OUR_URL))
    rows_start, _ = _build([at_start], window_days=7)
    rows_end, _ = _build([at_end], window_days=7)
    assert rows_start and rows_start[0][I_GATE1] == 1, "窗口起点必须是闭的"
    assert rows_end == [], "窗口终点必须是开的,否则 7 天窗和 14 天窗会重叠计数"


# ══════════════════════════════════════════════════════════════════
# 🔴 可观察性:看不见 ≠ 失败(gate1 的分母是 tests_observable)
# ══════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("empty_value", [None, ""])
def test_unrecorded_citations_are_not_observable(empty_value):
    """必须不命中:search_citations 没记录 → 进 tests_total,**不进** tests_observable。

    把它算进 gate1 的分母 = 把「我们没看见」当成「没被引」。
    """
    rows, _ = _build([_mr(citations=empty_value)])
    assert len(rows) == 1
    assert rows[0][I_TESTS_TOTAL] == 1
    assert rows[0][I_TESTS_OBSERVABLE] == 0, "未记录的监测被当成了可观察样本"
    assert rows[0][I_GATE1] == 0


def test_recorded_but_uncited_is_observable():
    """成对的必须命中:记录了引用列表、但里面没有我们 → 这**是**一次可观察的失败。"""
    rows, _ = _build([_mr(citations=_citations(OTHER_URL))])
    assert rows[0][I_TESTS_OBSERVABLE] == 1, "引用列表有内容却没算成可观察"
    assert rows[0][I_GATE1] == 0


def test_broken_json_goes_to_unparsable_not_observable():
    """坏 JSON 单独记账,既不算可观察也不算失败 —— 混进任何一边都会污染分母。"""
    rows, _ = _build([_mr(citations="{不是数组")])
    assert rows[0][I_UNPARSABLE] == 1
    assert rows[0][I_TESTS_OBSERVABLE] == 0
    assert rows[0][I_GATE1] == 0


def test_empty_array_is_observable_but_uncited():
    """`[]` = AI 检索了、什么都没引 → 可观察的失败,和「没记录」不是一回事。"""
    rows, _ = _build([_mr(citations="[]")])
    assert rows[0][I_TESTS_OBSERVABLE] == 1
    assert rows[0][I_GATE1] == 0


# ══════════════════════════════════════════════════════════════════
# gate1:URL 必须归一化后严格相等
# ══════════════════════════════════════════════════════════════════

def test_gate1_matches_after_tracking_params_stripped():
    """必须命中:同一篇文章带了 utm 参数 → 归一化后仍算命中。"""
    rows, _ = _build([_mr(citations=_citations(OUR_URL + "?utm_source=x&utm_medium=y"))])
    assert rows[0][I_GATE1] == 1


def test_gate1_does_not_match_same_domain_other_article():
    """成对的必须不命中:同域名的**别人的**文章不算我们的战果。

    这正是严格账本注释里警告过的「域级近似会把别人在同域发的文章算成我们的」。
    """
    rows, _ = _build([_mr(citations=_citations(OTHER_URL))])
    assert rows[0][I_GATE1] == 0


def test_citation_rank_agrees_with_parse_citation_urls():
    """rank 与 parse 必须用同一套归一化,否则会「parse 说命中、rank 说没命中」。"""
    raw = _citations(OTHER_URL, OUR_URL + "?utm_source=x", ranks=[1, 2])
    assert OUR_URL in set(parse_citation_urls(raw)["urls"])
    assert citation_rank_for(raw, OUR_URL) == 2, "两处归一化口径不一致"


def test_citation_rank_none_when_absent():
    """成对的必须不命中:没被引 → rank 是 None,不是 0(0 会被误读成「第 0 名」)。"""
    assert citation_rank_for(_citations(OTHER_URL), OUR_URL) is None


def test_citation_rank_prefers_rank_field_over_array_position():
    """rank 字段与数组下标不一致时,以 **rank 字段** 为准。

    变异 runner 抓到的缺口:原用例里 rank 恰好等于下标,两者分不出来 ——
    「正反两侧同时失效」的经典形状。这里让 rank 与下标故意错开。
    """
    raw = _citations(OTHER_URL, OUR_URL, ranks=[5, 9])   # 我们在下标 2、rank 9
    assert citation_rank_for(raw, OUR_URL) == 9, "拿数组下标冒充了真实排位"


def test_citation_rank_falls_back_to_position_when_field_missing():
    """成对:没有 rank 字段时才退回下标(退回是兜底,不是常态)。"""
    raw = _citations(OTHER_URL, OUR_URL)                  # 不带 rank 字段
    assert citation_rank_for(raw, OUR_URL) == 2


# ══════════════════════════════════════════════════════════════════
# 🔴 本表不记「品牌被提及/被推荐」—— 粒度不同,放进来必被文章数重复加权
# ══════════════════════════════════════════════════════════════════

def test_schema_has_no_brand_mention_columns():
    """🔴 gate2/gate3 已从本表移除(Codex 2026-08-09 复审)。

    它们是「词 × 日」粒度,`keyword_compliance_log` 已经在做。放进文章粒度后,
    同品牌两篇文章的 30 天窗口重叠时,一次唯一监测事件会被数成两次。
    谁想加回来,这条先红。
    """
    sql = _migration_sql()
    for banned in ("gate2_brand_detected", "gate3_brand_recommended", "gate2_backfilled"):
        assert banned not in sql, f"品牌层面的 {banned} 又被塞回文章粒度表了"


def test_builder_does_not_read_mention_fields():
    """成对:builder 也不许再去**读**这两个字段。

    ⚠️ 打在真实读取模式上,不是原始文本匹配 —— docstring 里会正当地提到这两个字段名
    (解释「为什么第三关没有聚合层」),文本匹配会把注释误判成读取。
    """
    import inspect
    from services import publication_outcome_facts as mod
    src = inspect.getsource(mod)
    for pattern in ('row.get("is_detected")', 'row.get("mention_type")',
                    'mr.is_detected', 'mr.mention_type'):
        assert pattern not in src, f"builder 仍在读 {pattern}"


def test_two_articles_sharing_one_monitoring_each_get_own_row():
    """🔴 Codex 点名用例:两篇文章 + 同一条监测。

    每篇文章各自一行、各自判自己的 URL 有没有被引 —— 这是**正确的按文章记账**,
    不是重复计数。被重复加权的是品牌层面的事实,所以那些列已经删了。
    """
    shared = _mr(offset_hours=1, citations=_citations(OUR_URL))

    pub_a = _pub()
    rows_a, _ = _build([shared])
    assert rows_a[0][I_TESTS_TOTAL] == 1
    assert rows_a[0][I_GATE1] == 1, "A 文章的 URL 确实被引了"

    # B 文章是同品牌另一篇,URL 不同 → 同一条监测里它没被引
    summary = {k: 0 for k in ("tests_total", "tests_observable", "citations_unparsable",
                              "gate1_url_cited")}
    pub_b = {**pub_a, "id": 2, "_url": OTHER_URL}
    rows_b = _outcome_rows_for(pub_b, [shared], 30, PUB_AT + timedelta(days=365), summary)
    assert rows_b[0][I_TESTS_TOTAL] == 1
    assert rows_b[0][I_GATE1] == 0, "B 文章的 URL 没被引,却被算成命中了"


# ══════════════════════════════════════════════════════════════════
# 分组与窗口完整性
# ══════════════════════════════════════════════════════════════════

def test_rows_split_by_keyword_and_platform():
    """粒度锁:(词 × 引擎) 各自一行,不许合并成一行 —— 合并会丢掉引擎差异。"""
    rows, _ = _build([
        _mr(offset_hours=1, keyword="词A", platform="deepseek"),
        _mr(offset_hours=2, keyword="词A", platform="kimi"),
        _mr(offset_hours=3, keyword="词B", platform="deepseek"),
    ])
    assert len(rows) == 3


def test_incomplete_window_is_flagged():
    """窗口没走完必须如实标记,否则会拿偏小的分母算率。"""
    rows, _ = _build([_mr(citations=_citations(OUR_URL))],
                     window_days=30, clock=PUB_AT + timedelta(days=3))
    assert rows[0][I_WINDOW_COMPLETE] is False


def test_complete_window_is_flagged():
    """成对的必须命中。"""
    rows, _ = _build([_mr(citations=_citations(OUR_URL))],
                     window_days=30, clock=PUB_AT + timedelta(days=31))
    assert rows[0][I_WINDOW_COMPLETE] is True


def test_first_cited_at_is_earliest_hit():
    """多次命中取最早那次,不是最后一次。"""
    rows, _ = _build([
        _mr(offset_hours=10, citations=_citations(OUR_URL)),
        _mr(offset_hours=2, citations=_citations(OUR_URL)),
    ])
    assert rows[0][I_FIRST_CITED] == PUB_AT + timedelta(hours=2)


# ══════════════════════════════════════════════════════════════════
# 设计锁:表里不许出现任何比率列
# ══════════════════════════════════════════════════════════════════

def _migration_sql() -> str:
    root = Path(__file__).resolve().parents[1]
    return (root / "db" / "migration_032_publication_outcome_facts_2026_08_09.sql").read_text(
        encoding="utf-8")


def test_migration_stores_no_ratio_columns():
    """🔴 比率一旦落库就固化了一个分母选择,下游再也看不见它。

    只扫**列定义区**,不扫注释 —— 注释里出现「率」字是正常的(整份迁移都在解释为什么不存率)。
    """
    sql = _migration_sql()
    column_lines = [
        line.split("--")[0]
        for line in sql.splitlines()
        if line.startswith("    ") and not line.strip().startswith("--")
    ]
    body = "\n".join(column_lines).lower()
    for banned in ("_rate", "_ratio", "_pct", "_percent", "effectiveness"):
        assert banned not in body, f"事实表出现了比率列 {banned} —— 率必须在查询侧现算"


def test_migration_has_invariant_checks():
    """五条 CHECK 是「分子大于分母」的最后一道闸,不许被删。"""
    sql = _migration_sql()
    for check in ("ck_pub_outcome_gate1_le_observable",
                  "ck_pub_outcome_observable_le_total"):
        assert check in sql, f"缺少不变式约束 {check}"


def test_metric_version_is_pinned():
    """口径变更必须 bump 版本写新行。版本号被改动时这条会红,提醒同步下游。"""
    assert FACTS_METRIC_VERSION == "publication-outcome-facts-v1.0"
    assert URL_NORMALIZATION_VERSION == "strict-url-v1.0", \
        "URL 归一化口径变了 —— 事实表与严格账本会长出两个身份,必须同步 bump"


def test_outcome_table_name_matches_migration():
    """接线锁:模块里的表名必须和迁移里建的那张一致(打在接线上,不是打在常量上)。"""
    assert f"CREATE TABLE IF NOT EXISTS {OUTCOME_TABLE}" in _migration_sql()


# ══════════════════════════════════════════════════════════════════
# 读取侧:分母不许选错(本次 NO-GO 五条 P0 里三条是分母选错)
# ══════════════════════════════════════════════════════════════════

def test_gate_denominators_are_pinned():
    """gate1 的分母必须是 tests_observable,gate2/gate3 必须是 tests_total。

    这不是风格选择:search_citations 没记录时无从判断 URL 有没有被检索到;
    而品牌有没有被提及是从回答正文判的,与引用列表是否记录无关。
    """
    from services.publication_outcome_facts import _GATE_DENOMINATORS
    assert _GATE_DENOMINATORS["gate1_url_cited"][0] == "tests_observable"


def test_gate1_denominator_is_not_tests_total():
    """成对的必须不命中:gate1 一旦用 tests_total 当分母,就是把「没看见」当「失败」。"""
    from services.publication_outcome_facts import _GATE_DENOMINATORS
    assert _GATE_DENOMINATORS["gate1_url_cited"][0] != "tests_total"


def test_article_level_sql_filters_incomplete_windows():
    """文章级查询必须只算窗口已走完的行,否则拿偏小的分母算率。"""
    from services.publication_outcome_facts import _ARTICLE_LEVEL_SQL
    assert "window_complete" in _ARTICLE_LEVEL_SQL


def test_article_level_sql_denominator_excludes_unobservable():
    """分母必须是 observable > 0 的文章,不是全部文章。"""
    from services.publication_outcome_facts import _ARTICLE_LEVEL_SQL
    assert "FILTER (WHERE observable > 0)" in _ARTICLE_LEVEL_SQL
    assert "FILTER (WHERE cited > 0)" in _ARTICLE_LEVEL_SQL


# ══════════════════════════════════════════════════════════════════
# 🔴🔴🔴 接线锁 —— Codex 2026-08-09 P0:迁移与 builder 都没接进部署链,整包惰性
# ══════════════════════════════════════════════════════════════════

def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def test_migration_is_wired_into_deploy_manifest():
    """🔴 迁移必须在 `db/migration_manifest.py` 的清单里,否则表永远不会被建出来。

    第一版 32 条锁全绿、13/13 变异全杀 —— 但没有一条打在「这张表会被建出来」上,
    所以整包上线即惰性。这就是「判据要打在接线上,不是函数上」的第六例。
    """
    manifest = (_repo_root() / "db" / "migration_manifest.py").read_text(encoding="utf-8")
    assert "migration_032_publication_outcome_facts_2026_08_09.sql" in manifest, \
        "迁移 032 没进部署清单 —— prestart 只跑 manifest,表建不出来"


def test_builder_is_wired_into_scheduler():
    """🔴 成对:builder 必须有真实调用方,否则表建出来也永远是空的。"""
    sched = (_repo_root() / "api" / "scheduler.py").read_text(encoding="utf-8")
    assert "build_publication_facts" in sched, "builder 没有任何调度调用方"
    assert 'scheduler.add_job(' in sched and "publication_outcome_facts" in sched, \
        "builder 没有注册成 job"


def test_scheduler_job_runs_after_attribution_sync():
    """两者读同一批监测数据,排在归因同步之前只会重复算一遍旧账本。"""
    sched = (_repo_root() / "api" / "scheduler.py").read_text(encoding="utf-8")
    attr_pos = sched.index('id="article_attribution_sync"')
    facts_pos = sched.index('id="publication_outcome_facts"')
    assert facts_pos > attr_pos, "事实表重建排到了归因同步前面"


# ══════════════════════════════════════════════════════════════════
# 🔴 Codex P1:重跑必须清掉过期事实(published→rejected / URL 被订正)
# ══════════════════════════════════════════════════════════════════

class _RecordingCursor:
    """记录真实执行的 SQL —— 比源码文本匹配强:改了写法但行为对,锁不该红;
    行为错了(比如先插后删),源码匹配可能照样绿。"""

    def __init__(self, sink, publications):
        self.sink = sink
        self._pubs = publications
        self._last = ""

    def execute(self, sql, params=None):
        self.sink.append((" ".join(str(sql).split()), params))
        self._last = str(sql)

    def fetchall(self):
        return self._pubs if "mhz_publish_order_items" in self._last else []

    @property
    def rowcount(self):
        return 3


def _fake_db(sink, publications):
    class _Conn:
        def cursor(self): return _RecordingCursor(sink, publications)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return lambda: _Conn()


def _one_publication():
    return [{
        "id": 1, "brand_id": 42, "media_id": 7, "media_name": "示例媒体",
        "status": "published", "publish_url": OUR_URL, "cost_yuan": 10,
        "submitted_at": PUB_AT, "published_at": PUB_AT, "created_at": PUB_AT,
    }]


def test_rebuild_deletes_stale_rows_before_insert(monkeypatch):
    """🔴 行为锁:发布订单会从 published 改成 rejected,URL/品牌也会被订正。

    只 upsert 不删 → 一篇后来被判 rejected 的文章,它的 gate1 命中会永远留在表里
    被下游当成真战果。必须在同一个事务里、**在插入之前**删掉本口径的旧行。
    """
    from services import publication_outcome_facts as mod
    sink: list = []
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_db", _fake_db(sink, _one_publication()))

    mod.build_publication_facts(dry_run=False)

    statements = [sql for sql, _ in sink]
    deletes = [i for i, sql in enumerate(statements) if sql.startswith("DELETE FROM")]
    inserts = [i for i, sql in enumerate(statements) if sql.startswith("INSERT INTO")]
    assert deletes, "重跑没有清理旧行"
    assert inserts, "重跑没有写入"
    assert max(deletes) < min(inserts), "先插后删,顺序反了,等于没删"

    deleted_tables = {sql.split()[2] for sql in statements if sql.startswith("DELETE FROM")}
    assert mod.OUTCOME_TABLE in deleted_tables, "没清 outcome 旧行"
    assert mod.ATTEMPT_TABLE in deleted_tables, "没清 attempt 旧行"


def test_stale_deletion_is_scoped_to_metric_version(monkeypatch):
    """🔴 成对的必须不命中:清理只能删本口径的行,不许把别的 metric_version 一起端了。

    口径变更走新 metric_version 写新行是既定纪律(与严格账本同规),
    无差别 DELETE 会把历史口径的行也删掉。
    """
    from services import publication_outcome_facts as mod
    sink: list = []
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_db", _fake_db(sink, _one_publication()))

    mod.build_publication_facts(dry_run=False)

    for sql, params in sink:
        if not sql.startswith("DELETE FROM"):
            continue
        assert "WHERE metric_version = %s" in sql, f"无差别 DELETE:{sql}"
        assert params == (mod.FACTS_METRIC_VERSION,), "DELETE 没有按本口径限定"


# ══════════════════════════════════════════════════════════════════
# 🔴 Codex P1:零监测的文章不许从人口里消失
# ══════════════════════════════════════════════════════════════════

def test_article_level_denominator_starts_from_publication_population():
    """🔴 分母必须从投放人口(attempt 表)起底,再 LEFT JOIN outcome。

    窗口内一次监测都没有的文章,在 outcome 表里根本不产行 ——
    只从 outcome 数 articles_total,分母会悄悄变成「至少被测过一次的文章」,
    `articles_unobservable` 恒为 0,零监测的文章整个人间蒸发。
    """
    from services.publication_outcome_facts import _ARTICLE_LEVEL_SQL, ATTEMPT_TABLE
    assert f"FROM {ATTEMPT_TABLE}" in _ARTICLE_LEVEL_SQL, "分母没有从投放人口起底"
    assert "LEFT JOIN" in _ARTICLE_LEVEL_SQL, "用了内连接 → 零监测文章会被 JOIN 掉"
    assert "is_published" in _ARTICLE_LEVEL_SQL, "人口没有限定在已发布文章上"


def test_article_level_population_filters_incomplete_windows():
    """成对:人口也要按窗口是否走完过滤,否则刚发的文章会拉低分母。"""
    from services.publication_outcome_facts import _ARTICLE_LEVEL_SQL
    assert "published_at + make_interval(days => %s) <= NOW()" in _ARTICLE_LEVEL_SQL


def test_unobservable_articles_stay_in_denominator():
    """行为锁:零监测的文章必须能被 `articles_unobservable` 数出来。"""
    from services.publication_outcome_facts import _ARTICLE_LEVEL_SQL
    # observable 用 COALESCE(SUM(...),0),LEFT JOIN 没命中时是 0 而不是 NULL,
    # 才能被 `FILTER (WHERE observable > 0)` 正确排除出分子而留在分母里
    assert "COALESCE(SUM(o.tests_observable), 0)" in _ARTICLE_LEVEL_SQL


def test_mutation_runner_preserves_line_endings():
    """🔴 变异 runner 读写必须带 newline="" —— 否则跑一次就污染整仓行尾。

    本仓 `core.autocrlf=false` 且仓库存 LF。`Path.write_text()` 在 Windows 上默认把
    `\n` 写成 `\r\n`,于是 runner 每次「还原」都把被它碰过的文件从 LF 改成 CRLF ——
    第一版就是这么把 `api/scheduler.py` 的 diff 从 42 行撑成 9,986 行的。
    """
    src = (_repo_root() / "tests" / "mutation_runner_pubfacts_2026_08_09.py").read_text(
        encoding="utf-8")
    for lineno, line in enumerate(src.splitlines(), 1):
        if "write_text(" in line or "read_text(" in line:
            assert 'newline=""' in line, \
                f"runner 第 {lineno} 行读写文件没带 newline=\"\" —— 跑一次就污染行尾:{line.strip()}"


def test_does_not_claim_compliance_log_covers_mention_and_recommend():
    """🔴 Review 2026-08-09:不许再声称 `keyword_compliance_log` 已承担第二、三关。

    实测该表只有 detection_rate / target_rate / is_compliant:
      · 被提及 → 只有日聚合率,不是事实层
      · 被推荐 → **压根没有聚合层**(原始信号只在 monitoring_results.mention_type)
    把 detection_rate 当推荐率用,是把两个不同的量混成一个。
    """
    for text in (_migration_sql(),
                 (_repo_root() / "services" / "publication_outcome_facts.py").read_text(
                     encoding="utf-8")):
        assert "已由 `keyword_compliance_log`" not in text, "又写回了那句假声明"
        assert "detection_rate 当推荐率用" in text, "缺少「不许把 detection_rate 当推荐率用」的禁令"
        assert "没有任何聚合层" in text, "没有写明第三关(被推荐)当前没有聚合层"
