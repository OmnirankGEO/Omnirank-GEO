"""判据 · WO-A ④:``preset`` chunk 计数与 manifest 对账(**集合式**,不是计数式)。

## 诊断结论(多的那条是什么、谁写进去的)

生产长期显示 ``preset: 16``,而同一次重建写下的
``manifest["chunk_counts"]["preset"]`` 是 15。多出来的那一条不是知识,是
``__kb_release_manifest__`` —— manifest 自己。它由
``db.kb_db.replace_chunks_transactionally`` 写入,为了「不新增表/列」
**借用** ``source_type='preset'`` 存放。

错的是计数器不是 manifest:检索路径 ``load_all_chunks`` 早就把这一行排除了,
只有 ``count_chunks`` 把内部记账行当知识数。已按检索口径对齐。

## 为什么判据是集合式

计数式判据("preset 必须等于 15")会被正常业务写过期:
以后加一条 preset,判据转红而代码没错 —— 转红没有信息量的判据会被人调数字,
调着调着就没人看了。集合式("库里那批 slug 必须**就是** builder 产出的那批")
永远跟着真值走,而且漏一条 / 多一条都指得出是**哪一条**。
"""

from __future__ import annotations

import pytest

RELEASE_SHA = "f" * 40


def drop_release_rows() -> None:
    """🔴 收尾必须把这一版 release 删干净。

    实测(本包 A/B):不删的话,``tests/system_kb`` 的检索排序判据会被这
    140+ 条 doc/faq/preset chunk 顶红 —— 而那三条红**跟被测代码毫无关系**,
    下一个人得先花半天证明「不是我的锅」。同底基线上这条序列是绿的,
    所以红是本包的残留,不是存量。
    """
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM kb_chunks WHERE source_type IN ('doc','faq','preset')")
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module", autouse=True)
def _release_built():
    """在测试库里真跑一次 release 重建(不 mock)—— 判据要打真行,不打返回值。"""
    from api.faq_api import init_faq_tables
    from db.kb_db import init_kb_tables
    from tools.xiaobang_kb_indexer import rebuild_release

    init_kb_tables()
    init_faq_tables()
    try:
        yield rebuild_release(RELEASE_SHA)
    finally:
        drop_release_rows()


def _slugs_in_db(source_type: str) -> set[str]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT source_slug FROM kb_chunks WHERE source_type = %s",
                    (source_type,))
        return {r["source_slug"] for r in cur.fetchall()}
    finally:
        conn.close()


def test_preset_slugs_in_db_are_exactly_what_the_builder_produced():
    """集合式对账:库里那批 preset slug **就是** builder 产出的那批。"""
    from db.kb_db import KB_RELEASE_MANIFEST_SLUG, count_chunks
    from tools.xiaobang_kb_indexer import build_preset_chunks

    built = {row["source_slug"] for row in build_preset_chunks()}
    assert built, "builder 产出为空 —— 分母塌了,下面全是空即通过"

    live = _slugs_in_db("preset")
    assert KB_RELEASE_MANIFEST_SLUG in live, (
        "manifest 行不在库里 —— 那这条判据没在验它该验的东西")

    # 计数口径 = 检索口径:manifest 行不算知识。
    assert live - {KB_RELEASE_MANIFEST_SLUG} == built, {
        "库里多出来的": sorted(live - {KB_RELEASE_MANIFEST_SLUG} - built),
        "库里少掉的": sorted(built - live),
    }
    assert count_chunks()["preset"] == len(built)


def test_count_chunks_agrees_with_the_manifest_it_wrote(_release_built):
    """三方对账:``count_chunks`` == manifest.chunk_counts == builder 产出。

    三个数字里任意两个对上都可能是巧合;三个一起对上,而且分母各自独立
    (库 / manifest / builder),才说明这一版 release 是完整的。
    """
    from db.kb_db import count_chunks, load_release_manifest

    manifest = load_release_manifest()
    assert manifest is not None, "manifest 读不回来 —— 对账没有第二个信源"
    assert manifest["release_sha"] == RELEASE_SHA

    counts = count_chunks()
    for source_type, expected in manifest["chunk_counts"].items():
        assert counts.get(source_type) == expected, (source_type, counts, manifest)


def test_the_extra_row_is_exactly_one_and_it_is_the_manifest():
    """反向对照:``include_internal=True`` 必须比默认口径**恰好多一条**。

    没有这一条,上面的绿也可能来自「计数函数把 preset 整类漏掉了」——
    那同样会让两边"对上"(0 == 0)。这条把差额钉死成 1,并指名那一条是谁。
    """
    from db.kb_db import KB_RELEASE_MANIFEST_SLUG, count_chunks

    lean = count_chunks()
    raw = count_chunks(include_internal=True)
    assert raw["preset"] - lean["preset"] == 1, (lean, raw)
    for source_type, value in lean.items():
        if source_type != "preset":
            assert raw[source_type] == value, (source_type, lean, raw)
    assert KB_RELEASE_MANIFEST_SLUG in _slugs_in_db("preset")


def test_the_manifest_row_never_reaches_retrieval():
    """内部记账行不许进召回 —— 用户问一句就答一串 JSON 那种事故。"""
    from db.kb_db import KB_RELEASE_MANIFEST_SLUG, load_all_chunks

    slugs = {c["source_slug"] for c in load_all_chunks(include_admin=True)}
    assert KB_RELEASE_MANIFEST_SLUG not in slugs
