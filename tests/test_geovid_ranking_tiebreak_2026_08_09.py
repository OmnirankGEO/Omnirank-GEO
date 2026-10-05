# -*- coding: utf-8 -*-
"""榜单取数的两处并列歧义 · 锁(2026-08-09 · **r2:按 Review 裁定拆分后**)

## 两处并列,性质完全不同 —— 这是本文件存在的主要原因

| 位置 | 决定什么 | 性质 | 本包处置 |
|---|---|---|---|
| `best` CTE 的 `DISTINCT ON` 末位 | 同一家企业**引哪一条证据行**(哪个引擎的哪句回答) | 数据稳定性 | ✅ 补 `e.id DESC` |
| 最终 `ORDER BY ... LIMIT 40` | **哪几家企业进候选池** | **商业排名策略** | 🔴 **撤销**,需 Owner 签发 |

我上一版把两者打包成"一件技术去随机"提交。Review 拆开后指出:
后者用 `entity_key ASC`(字符序)决定谁上榜,实测换掉了 `business_service` 榜上的
一家公司(**华为云 → Worktile**)—— 那不是去随机,是未签发的排名策略。**裁定正确,已撤。**

## 🔴 撤销之后仍然存在的缺陷,如实写着

第 40 名边界的并列**回到"SQL 未规定"**:生产快照实测 17 个行业里 **14 个**存在并列,
`food` 有 **11 家**同 `(engine_count, mention_count)` 争最后一个坑。
**这是已知未修缺陷,不是修好了。**

实证边界:强制换执行计划(`enable_hashagg=off` + `enable_seqscan=off`)两次结果一致 ——
**没有观测到漂移**,歧义是规范层面的,不是已出事。

## 🔴 关于"能不能锁住"的一条实测结论(留着,别再试一遍)

**"没有 tiebreaker 就必红"的行为锁,原理上造不出来** —— 并列顺序按定义未规定,
任何观测到的顺序都是合法输出。我造过插入序 = 字典序倒序的 5 家全并列 fixture,
去掉 tiebreaker 的 SQL 变体**仍返回同一组**(PG 这份数据上的聚合顺序碰巧就是字典序)。
所以剩下的是**结构锁**:一条要求 `DISTINCT ON` 末位唯一(变异实测能转红),
一条把最终 ORDER BY 钉成**与已签发合同逐字相等**(白名单,不是黑名单 ——
黑名单被 Review 实测用「换行加排序键」一击穿透,验尸报告见
`test_the_guard_catches_all_four_sneak_shapes` 的四类变异)。
"""
from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = REPO / "services" / "geo_douyin" / "ranking_source.py"


def _strip_comments(src: str) -> str:
    """🔴 剥掉 `--` 注释再判 —— 我在这个仓里已经因为"判据命中自己写的注释"
    翻过三次车(一次是 grep 命中注释里的常量名,一次是我自己注释里提到被禁的符号)。"""
    return "\n".join(re.sub(r"--.*$", "", ln) for ln in src.splitlines())


# ===========================================================================
# 结构:两处 ORDER BY 的末位必须是唯一列
# ===========================================================================

#: 🔴 **被签发过的排序合同原文**。改它 = 改"谁进候选池",需要 Owner 签发。
#:    判据断言的是**这一整句**,不是"没出现某几个词" —— 见下面那段。
_SIGNED_FINAL_ORDER_BY = "a.engine_count DESC, a.mention_count DESC"


def _final_order_by_clause(sql: str) -> str:
    """抽出最终 `ORDER BY ... LIMIT` 之间的**完整**子句,归一空白。

    🔴 必须跨行抽 —— 上一版写的是 `ORDER BY a\\.engine_count DESC[^\\n]*`,
       只看第一行。Review 实测:换行再加一个排序键就完全绕过,守卫照样绿:

           ORDER BY a.engine_count DESC, a.mention_count DESC,
                    b.best_rank_raw ASC
    """
    i = sql.rfind("ORDER BY a.engine_count DESC")
    assert i >= 0, "找不到最终 ORDER BY(SQL 结构被改过?这条锁要跟着改)"
    j = sql.find("LIMIT", i)
    assert j > i, "最终 ORDER BY 后面找不到 LIMIT"
    return " ".join(sql[i + len("ORDER BY"):j].split())


def test_final_order_by_matches_the_signed_contract_exactly():
    """🔴 **守卫,不是修复**:最终 ORDER BY 必须**逐字**等于已签发的合同。

    我上一版在这里加过 `b.entity_key ASC`。Review 裁定它不是"技术去随机",
    而是**用字符序决定哪家企业进榜** —— 实测换掉了 `business_service` 榜上的
    一家公司(华为云 → Worktile)。同分时谁上榜是商业排名策略,**必须有数据依据
    并由 Owner 签发**。

    🔴 而且这条锁上一版是**黑名单**(禁三个字段名)—— 黑名单天然漏:
       换行、换字段(`b.best_rank_raw`)、表达式、别名,随便哪种都能绕过去。
       正确写法是**白名单 + 精确相等**:任何改动都必须先来改这条常量,
       而改这条常量的人会读到上面那句"需要 Owner 签发"。
    """
    got = _final_order_by_clause(_strip_comments(SOURCE.read_text(encoding="utf-8")))
    assert got == _SIGNED_FINAL_ORDER_BY, (
        f"最终 ORDER BY 与已签发合同不符。\n  已签发:{_SIGNED_FINAL_ORDER_BY}\n"
        f"  现  在:{got}\n"
        "同分时谁进候选池属于商业排名策略,先拿签发再改这条常量。")


@pytest.mark.parametrize("sneaky", [
    # ① 换行加排序键 —— 上一版守卫**完全看不见**(Review 实测的那个)
    "ORDER BY a.engine_count DESC, a.mention_count DESC,\n"
    "         b.best_rank_raw ASC\n         LIMIT %s",
    # ② 同行加字段名(上一版黑名单只挡这一类)
    "ORDER BY a.engine_count DESC, a.mention_count DESC, b.entity_key ASC\n"
    "         LIMIT %s",
    # ③ 表达式排序(没有任何字段名可禁)
    "ORDER BY a.engine_count DESC, a.mention_count DESC,\n"
    "         (b.confidence * 100)::int DESC\n         LIMIT %s",
    # ④ 别名排序(SELECT 里起的别名,黑名单更抓不到)
    "ORDER BY a.engine_count DESC, a.mention_count DESC, best_rank_raw NULLS LAST\n"
    "         LIMIT %s",
])
def test_the_guard_catches_all_four_sneak_shapes(sneaky):
    """🔴 判别力自证:四类"偷偷加同分策略"的写法必须**全部**被抓。

    这条用例本身就是上一版守卫的**验尸报告** —— 它当时四类里只抓第 ②。
    """
    got = _final_order_by_clause(sneaky)
    assert got != _SIGNED_FINAL_ORDER_BY, f"这种写法溜过去了:{got!r}"


def test_the_guard_accepts_the_contract_across_reformatting():
    """成对反向:合同**本身**换行/多空格不该被判红(否则守卫会因为格式化恒红)。"""
    reformatted = ("ORDER BY a.engine_count DESC,\n"
                   "         a.mention_count DESC\n         LIMIT %s")
    assert _final_order_by_clause(reformatted) == _SIGNED_FINAL_ORDER_BY


def test_distinct_on_order_by_ends_with_a_unique_column():
    sql = _strip_comments(SOURCE.read_text(encoding="utf-8"))
    i = sql.find("DISTINCT ON (e.entity_key)")
    assert i > 0
    end = sql.find("), agg AS", i)
    assert end > i, "找不到 best 段的结束边界(SQL 结构被改过?)"
    block = sql[i:end]
    j = block.find("ORDER BY e.entity_key")
    assert j > 0, "找不到 DISTINCT ON 的 ORDER BY"
    seg = block[j: block.find(")", block.find("e.updated_at DESC", j))]
    assert "e.id" in seg, (
        f"DISTINCT ON 的 ORDER BY 末位不唯一,同 updated_at 时选哪行未规定:{seg}")


# ===========================================================================
# 真库:🔴 这一段原本锁"并列时取字典序最小的那几家" —— **已随 `entity_key ASC` 一起撤销**
# ===========================================================================
#
# 撤销理由见上面 `test_final_order_by_matches_the_signed_contract_exactly`:
# 那是未签发的商业排名策略。锁跟着实现一起撤,**不留一条断言已撤销行为的用例** ——
# 留着它只会恒红,而长期红的判据等于没有判据。
#
# 🔴 同时如实记下:撤销之后,第 40 名边界的并列**又回到"SQL 未规定"**。
#    这是**已知未修缺陷**,不是"修好了"。它需要一条签发过的同分策略才能真正关闭,
#    见交付单 §3。此处**故意不放锁** —— 放一条"必须未规定"的锁是荒唐的。


# ===========================================================================
# 🔴 真数据零触发 → 构造用例(这是本仓的规矩,不是可选项)
# ===========================================================================
#
# 实测(`QNARROW_TIEBREAK_IMPACT_2026-08-09.py`,A=生产尖 B=本包):
#     候选顺序 0/17 · 候选成员 0/17 · 最终 6 家 0/17
# 也就是说 `e.id DESC` 在**生产快照上一次都没触发** —— 因为没有 `updated_at`
# 完全相同的两行。好消息是它对商业输出零影响(Review 批准保留的理由);
# 但"零影响"与"这段代码是死的"在真数据上**长得一模一样**。
# 上一班车我刚吃过这个亏(新门槛真数据全绿但从未触发 = 等于没验),所以造一个。

_ID_IND = "idtie_probe_ind"
_ID_KEY = "idtie_e1"


@pytest.fixture(scope="module")
def id_tie_seeded():
    """同一实体两条证据行,**engine / rank / updated_at 全部相同**,只有 id 不同。

    这正是 `DISTINCT ON` 末位没有唯一列时"选哪行未规定"的最小复现。
    """
    from db.connection import get_connection
    conn = get_connection()
    try:
        cur = conn.cursor()
        for n, fid in enumerate((990500, 990501)):
            cur.execute("""
                INSERT INTO geo_research_answer_facts
                    (id, raw_id, industry_key, query, engine, batch_id,
                     answer_hash, extractor_version)
                VALUES (%s, 1, %s, %s, 'A', 'idtie_probe', %s, 'v1')
                ON CONFLICT DO NOTHING
            """, (fid, _ID_IND, "同 updated_at 探针问题 %d" % n,
                  ("%06d" % fid) + "f" * 26))
            cur.execute("""
                INSERT INTO geo_research_answer_entities
                    (answer_fact_id, raw_id, industry_key, engine, entity_name,
                     entity_key, entity_type, recommendation_rank, evidence_phrases,
                     recommendation_reasons, confidence, llm_model, extractor_version,
                     updated_at)
                VALUES (%s, 1, %s, 'A', 'id并列探针', %s, 'brand', 3, '[]', '[]',
                        1.0, 'm', 'v1', TIMESTAMPTZ '2026-08-09 00:00:00+00')
                ON CONFLICT (answer_fact_id, entity_key) DO NOTHING
            """, (fid, _ID_IND, _ID_KEY))
        conn.commit()
    finally:
        conn.close()
    yield


def test_distinct_on_id_tiebreak_is_live_and_deterministic(id_tie_seeded):
    """🔴 证明 `e.id DESC` **这段代码是活的**:同 updated_at 时选 id 最大的那条。

    判据打在**对外那句话引的是谁**上 —— `source.row_id` 与 `source.query`
    必须来自 id 较大的那一行,且两次调用逐字相同。

    ⚠️ 如实说明这条锁**不能**证明什么:去掉 `e.id DESC` 之后 PG 选哪行同样未规定,
       所以"没有它必红"依然造不出(与文件头那段同理)。它证明的是
       ① 这段代码确实被执行到、② 方向没写反、③ 同入参两次一致。
    """
    from services.geo_douyin.ranking_source import fetch_ranking_candidates
    a = fetch_ranking_candidates(_ID_IND, limit=5)
    b = fetch_ranking_candidates(_ID_IND, limit=5)
    c = next(x for x in a if x["entity_key"] == _ID_KEY)
    assert c["source"]["row_id"] is not None
    assert "探针问题 1" in c["source"]["query"], (
        f"没有取到 id 较大的那一行(取到的是:{c['source']['query']})")
    assert [x["source"]["row_id"] for x in a] == [x["source"]["row_id"] for x in b], \
        "同入参两次取到的证据行不同"
