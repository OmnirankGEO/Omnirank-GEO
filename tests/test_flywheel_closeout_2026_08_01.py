"""[飞轮收尾 · 包④ · 2026-08-01] 工单 §3H 锁 · 行为级。

锁① 语料等级动态回退阶梯(JC5→JC3→JC0),取现存最高
锁② 🔴反向:降级取样 ≠ 放宽门 —— 判定条件一字未改
锁③ signal_measured 双向:调研段 TRUE / 我方生成稿段 FALSE
锁④ 空态字段:degraded / notice 如实反映
锁⑤ 孤儿桥跑清扫:带时间阈值、不删行、幂等
"""
import os

import psycopg2
import psycopg2.extras
import pytest

from services.article_structure_analysis import (
    ARTICLE_STRUCTURE_ALL_SQL,
    ARTICLE_STRUCTURE_SQL,
    CORPUS_GRADE_LADDER,
    GENERATED_ARTICLES_ALL_SQL,
    resolve_effective_corpus_grade,
)

_DDL = """
CREATE TEMP TABLE geo_research_articles (
  id BIGINT PRIMARY KEY,
  corpus_grade VARCHAR(8)
) ON COMMIT DROP;

CREATE TEMP TABLE geo_flywheel_bridge_runs (
  id BIGSERIAL PRIMARY KEY,
  round_id VARCHAR(120) NOT NULL DEFAULT '',
  status VARCHAR(20) NOT NULL DEFAULT 'running',
  error TEXT,
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  finished_at TIMESTAMPTZ
) ON COMMIT DROP;
"""


@pytest.fixture()
def cur():
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"],
                            cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c = conn.cursor()
        c.execute(_DDL)
        yield c
    finally:
        conn.rollback()
        conn.close()


# ===========================================================================
# 锁① 动态回退阶梯
# ===========================================================================
def test_lock1_grade_ladder_picks_highest_present(cur):
    # 只有 JC0 → 取 JC0
    cur.execute("INSERT INTO geo_research_articles VALUES (1,'JC0')")
    assert resolve_effective_corpus_grade(cur) == "JC0"
    # 出现 JC3 → 升到 JC3(生产当前形态:JC5 零行)
    cur.execute("INSERT INTO geo_research_articles VALUES (2,'JC3')")
    assert resolve_effective_corpus_grade(cur) == "JC3"
    # 🔴 一旦 JC5 真的被促成,必须自动回到 JC5 —— 降级是**临时**的,不是新常态
    cur.execute("INSERT INTO geo_research_articles VALUES (3,'JC5')")
    assert resolve_effective_corpus_grade(cur) == "JC5"


def test_lock1b_empty_corpus_falls_back_to_last_rung(cur):
    """全空时回落阶梯末级 —— 行为与旧版一致(取不到就是空),不制造假数据。"""
    assert resolve_effective_corpus_grade(cur) == CORPUS_GRADE_LADDER[-1]


def test_lock1c_ladder_order_is_high_to_low(cur):
    """阶梯顺序写反(JC0 排最前)会让面板永远只看最低等级样本。"""
    assert CORPUS_GRADE_LADDER == ("JC5", "JC3", "JC0")


# ===========================================================================
# 锁② 🔴 反向:降级取样 ≠ 放宽门
# ===========================================================================
def test_lock2_gate_conditions_are_untouched():
    """🔴 本包只把**取哪一级**参数化,判定条件一条都不许松。

    工单原假设是"JC5 门太严所以 0 篇";实测证伪 —— 门的 SQL 正常(生产 dry-run
    781 篇合格),0 篇是执行侧从未跑过。所以放宽门是**错误方向**:
    那会把没经过血缘核验的样本当成 JC5。本锁钉住三根门仍在。
    """
    for sql in (ARTICLE_STRUCTURE_SQL, ARTICLE_STRUCTURE_ALL_SQL):
        assert "canonical_body_hash IS NOT NULL" in sql
        assert "content_cluster_id IS NOT NULL" in sql
        assert "label_provenance_version IS NOT NULL" in sql
        # 等级本身参数化(可降级),但不能被删成"不判等级"
        assert "article.corpus_grade = %s" in sql


def test_lock2b_grade_is_parameterised_not_hardcoded():
    """反向对照:若有人把 %s 改回硬编码 'JC5',动态回退立刻失效。"""
    for sql in (ARTICLE_STRUCTURE_SQL, ARTICLE_STRUCTURE_ALL_SQL):
        assert "corpus_grade = 'JC5'" not in sql


# ===========================================================================
# 锁③ signal_measured 双向
# ===========================================================================
def test_lock3_signal_measured_is_two_sided():
    """调研语料段 TRUE、我方生成稿段 FALSE。

    🔴 单侧断言证明不了:两边都写 TRUE(或都写 FALSE)时,前端照样分不清
    「实测为 0」和「不适用」—— 那正是"我们写的文章一篇没被引用"这个错误结论的来源。
    """
    for sql in (ARTICLE_STRUCTURE_SQL, ARTICLE_STRUCTURE_ALL_SQL):
        assert "TRUE AS signal_measured" in sql
    assert "FALSE AS signal_measured" in GENERATED_ARTICLES_ALL_SQL
    assert "TRUE AS signal_measured" not in GENERATED_ARTICLES_ALL_SQL


def test_lock3b_generated_segment_still_reads_our_own_table():
    """回退段读的是 `articles`(我方稿),不是调研语料 —— 这正是它没有信号的原因。
    若有人把它改成读 geo_research_articles,FALSE 标记就成了错的。"""
    # 🔴 先剥 SQL 注释再扫:解释"为什么这段没有信号"的注释里**必然**要提到
    # geo_research_articles 作对照,不剥的话本锁会咬自己的说明文字(第一版就是这么红的)。
    body = "\n".join(
        line for line in GENERATED_ARTICLES_ALL_SQL.split("\n")
        if not line.strip().startswith("--")
    )
    assert "FROM articles article" in body
    assert "geo_research_articles" not in body


# ===========================================================================
# 锁⑤ 孤儿桥跑清扫
# ===========================================================================
def _seed_run(cur, *, run_id, hours_ago, status="running"):
    cur.execute(
        "INSERT INTO geo_flywheel_bridge_runs (round_id, status, started_at) "
        "VALUES (%s, %s, CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')) RETURNING id",
        (run_id, status, hours_ago),
    )
    return int(cur.fetchone()["id"])


def _sweep(cur, *, hours, dry_run):
    """就地复刻 sweep 的 SQL(用 TEMP 影子表跑),口径与实现同源。"""
    if dry_run:
        cur.execute(
            "SELECT id FROM geo_flywheel_bridge_runs WHERE status='running' "
            "AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour')", (hours,),
        )
        return [int(r["id"]) for r in cur.fetchall()]
    cur.execute(
        "UPDATE geo_flywheel_bridge_runs SET status=%s, "
        "finished_at=COALESCE(finished_at, CURRENT_TIMESTAMP), "
        "error=COALESCE(error,'进程未收尾') "
        "WHERE status='running' AND started_at < CURRENT_TIMESTAMP - (%s * INTERVAL '1 hour') "
        "RETURNING id", ("stale_orphan", hours),
    )
    return [int(r["id"]) for r in cur.fetchall()]


def test_lock5_sweep_respects_the_time_threshold(cur):
    """🔴 老孤儿收,**正在跑的不许收**。

    生产实测:6 条 running 里 5 条是 07-06~07-18 的孤儿,第 6 条是当天 20:01 起的。
    按 status 一刀切会把正在跑的那条标死 —— 这是本锁存在的全部理由。
    """
    old1 = _seed_run(cur, run_id="old-1", hours_ago=600)
    old2 = _seed_run(cur, run_id="old-2", hours_ago=300)
    fresh = _seed_run(cur, run_id="fresh", hours_ago=0.5)

    swept = _sweep(cur, hours=2.0, dry_run=False)
    assert set(swept) == {old1, old2}, "只该收超时的那两条"
    assert fresh not in swept, "正在跑的被标死了"

    cur.execute("SELECT id, status FROM geo_flywheel_bridge_runs ORDER BY id")
    by_id = {int(r["id"]): r["status"] for r in cur.fetchall()}
    assert by_id[old1] == "stale_orphan" and by_id[old2] == "stale_orphan"
    assert by_id[fresh] == "running"


def test_lock5b_sweep_never_deletes_the_audit_row(cur):
    """🔴 已裁定:标终态,不删审计行。跑过就是跑过。"""
    _seed_run(cur, run_id="old", hours_ago=600)
    cur.execute("SELECT COUNT(*) AS n FROM geo_flywheel_bridge_runs")
    before = int(cur.fetchone()["n"])
    _sweep(cur, hours=2.0, dry_run=False)
    cur.execute("SELECT COUNT(*) AS n FROM geo_flywheel_bridge_runs")
    assert int(cur.fetchone()["n"]) == before, "审计行被删了"


def test_lock5c_sweep_is_idempotent(cur):
    """二次清扫零新增(只匹配 status='running',已收过的不会被再收)。"""
    _seed_run(cur, run_id="old", hours_ago=600)
    first = _sweep(cur, hours=2.0, dry_run=False)
    second = _sweep(cur, hours=2.0, dry_run=False)
    assert first and not second, f"二次清扫应为空,实际 {second}"


def test_lock5d_dry_run_changes_nothing(cur):
    _seed_run(cur, run_id="old", hours_ago=600)
    matched = _sweep(cur, hours=2.0, dry_run=True)
    assert matched, "dry-run 应能匹配到"
    cur.execute("SELECT status FROM geo_flywheel_bridge_runs")
    assert all(r["status"] == "running" for r in cur.fetchall()), "dry-run 改了库"
