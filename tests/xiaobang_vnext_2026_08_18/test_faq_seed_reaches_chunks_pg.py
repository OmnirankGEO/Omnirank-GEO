"""真库判据:改 ``db/faq_db.py`` 的 seed **确实**会变成 ``kb_chunks`` 里的 faq 正文。

## 为什么必须有这一条

R3-P6 的 ①②修的是文本;但「改了文本」和「用户看到的答案变了」之间隔着两跳:

1. ``init_faq_tables()`` 把 ``_INITIAL_SEED`` 同步进 ``faq_items`` 表 ——
   **只对 ``updated_by IS NULL`` 的行**(管理员手改过的行保留);
2. ``build_faq_chunks()`` 从 ``faq_items`` 表 SELECT,才产出 faq chunk。

任何一跳断了,文本改了也白改,而且**失败形态是静默的**:锁全绿、包发车、
线上照旧答旧词 —— 34 班就是这么过去的。所以判据打在这条链上,不是打在文件内容上。

🔴 生产实测(2026-08-19):10 条 ``faq_items`` 全部 ``updated_by IS NULL``,
即第 1 跳对全部 10 条都会真的覆盖。

## 这条测不了什么(如实写)

它证明**链路通**,不证明**部署会跑**。

🔴 [WO-A ① · 2026-08-20 订正] 上一版这里写的是「索引重建**不在任何自动步骤里**:
``start.sh`` / ``scripts/prestart.py`` / ``go.sh`` 三处 ``kb_indexer`` 命中数均为 0」。
前半句**不成立** —— 那三个文件确实零命中,但部署链跑的是
``scripts/deploy-blue-green.sh``(``go.sh`` 起飞时执行的就是它),
而那个脚本 2026-05-25 起就有一段 Post-Deploy 重建。分母是手写的三个文件名,
漏掉的那一个不会让任何判据变红。

真因是**判成功的方式**坏了(``|| true`` 吞退出码 + ``tail -8 | grep 索引完成``,
而生产 8 类 source_type 会把那一行挤出末 8 行)⇒ 成功也报失败 ⇒ 没人再信它。
已在 ``test_kb_release_step_pg.py`` 立锁,这里只留订正记录。
"""

from __future__ import annotations

import pytest

from . import terminology_domains as dom

pytestmark = pytest.mark.usefixtures("_faq_table_ready")


@pytest.fixture()
def _faq_table_ready():
    """在测试库里真建表 + 真跑 seed 同步(不 mock)。"""
    from api.faq_api import init_faq_tables

    init_faq_tables()
    yield


def _live_faq_rows() -> list[dict]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, question, answer_md, updated_by FROM faq_items "
            "WHERE is_published = TRUE ORDER BY id"
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def test_seed_actually_lands_in_the_table():
    """第 1 跳:``_INITIAL_SEED`` 的正文真的进了 ``faq_items``。"""
    from db.faq_db import _INITIAL_SEED

    rows = _live_faq_rows()
    assert rows, "faq_items 空 —— 后面所有断言都会变成空即通过"
    by_q = {r["question"]: r["answer_md"] for r in rows}
    for question, _cat, _sort, _up, _down, answer in _INITIAL_SEED:
        assert question in by_q, question
        assert by_q[question] == answer, question


def test_seed_sync_overwrites_a_stale_row_that_nobody_edited():
    """🔴 活性自证:把某行改脏 → 再跑一次 seed 同步 → 必须被覆盖回来。

    不做这一步的话,上一条在「表里本来就是新值」时也会绿 —— 那证明不了
    同步路径还活着(它可能已经被改成只在表为空时 insert)。
    """
    from api.faq_api import init_faq_tables
    from db.connection import get_connection
    from db.faq_db import _INITIAL_SEED

    question, _cat, _s, _u, _d, good_answer = _INITIAL_SEED[1]   # 「余额不足时怎么充值」
    poisoned = "充进来的是充值积分 · 扣 5% 手续费"

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE faq_items SET answer_md = %s, updated_by = NULL WHERE question = %s",
            (poisoned, question),
        )
        conn.commit()
    finally:
        conn.close()

    assert {r["question"]: r["answer_md"] for r in _live_faq_rows()}[question] == poisoned
    init_faq_tables()
    assert {r["question"]: r["answer_md"] for r in _live_faq_rows()}[question] == good_answer


def test_admin_edited_rows_are_not_overwritten():
    """反向对照:``updated_by`` 非 NULL 的行**不该**被 seed 覆盖。

    没有这条,上一条可以靠「无条件全表覆盖」通过 —— 那会把管理员改过的
    答案在每次部署时悄悄抹掉。
    """
    from api.faq_api import init_faq_tables
    from db.connection import get_connection
    from db.faq_db import _INITIAL_SEED

    question = _INITIAL_SEED[3][0]
    mine = "管理员手改过的答案,部署不许动它。"
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE faq_items SET answer_md = %s, updated_by = 1 WHERE question = %s",
            (mine, question),
        )
        conn.commit()
    finally:
        conn.close()

    init_faq_tables()
    assert {r["question"]: r["answer_md"] for r in _live_faq_rows()}[question] == mine

    # 还原,免得污染后续用例
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE faq_items SET updated_by = NULL WHERE question = %s", (question,)
        )
        conn.commit()
    finally:
        conn.close()
    init_faq_tables()


def test_built_faq_chunks_carry_no_legacy_terminology():
    """第 2 跳 + 验收线:真从表构建 chunk,逐条过残留门。

    这就是 WO 的线上判据「``kb_chunks`` 含『积分』=0 且含『额度』=0」的
    本机同构版 —— 打的是 ``build_faq_chunks()`` 的**真实产物**,不是文件内容。
    """
    from tools.xiaobang_kb_indexer import build_faq_chunks

    chunks = build_faq_chunks()
    assert chunks, "faq chunk 为空 —— 分母没了,断言会空即通过"
    problems = []
    for chunk in chunks:
        text = "{0}\n{1}".format(chunk.get("source_title") or "", chunk.get("content") or "")
        problems.extend(
            "{0}: {1}".format(chunk["source_slug"], p) for p in dom.kb_index_residue(text)
        )
        problems.extend(
            "{0}: {1}".format(chunk["source_slug"], p) for p in dom.dead_scheme_violations(text)
        )
        problems.extend(
            "{0}: {1}".format(chunk["source_slug"], p)
            for p in dom.cash_semantic_violations(text)
        )
    assert not problems, problems[:6]


def test_the_chunk_gate_would_catch_a_poisoned_row():
    """🔴 上一条的活性自证:往表里灌一条旧词答案 → 同一套判据必须命中。

    「全绿」得先证明不是因为判据打不到 chunk 正文。
    """
    from db.connection import get_connection
    from tools.xiaobang_kb_indexer import build_faq_chunks

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE faq_items SET answer_md = %s WHERE id = ("
            "  SELECT min(id) FROM faq_items WHERE is_published = TRUE)",
            ("充进来的是充值积分,老板给你设团队额度。",),
        )
        conn.commit()
    finally:
        conn.close()

    hits = []
    for chunk in build_faq_chunks():
        text = "{0}\n{1}".format(chunk.get("source_title") or "", chunk.get("content") or "")
        hits.extend(dom.kb_index_residue(text))
    assert hits, "判据打不到 chunk 正文 —— 上一条的绿是假绿"

    from api.faq_api import init_faq_tables
    init_faq_tables()          # 还原成 seed 版本
