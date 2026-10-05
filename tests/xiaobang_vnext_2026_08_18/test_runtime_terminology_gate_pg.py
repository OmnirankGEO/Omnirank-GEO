"""运行时术语门 · 真 HTTP + 真 PG16(R3-P7 ①)。

## 判据形状(工单原话)

「经 faq_api 发布含「积分」FAQ → **拒绝** 且 kb_chunks **零写入**」。

两半都要打,而且**零写入这半更要紧**:只验 4xx 的话,
「拒绝了但库里已经写进去一半」这种形态照样绿 —— 本仓 08-14 的
`response_model forbid` 事故就是「写库已提交、只有回包炸」。

## 为什么门要开在 writer 而不是只开在 API

``replace_chunks_transactionally`` 是 **clear-then-insert**:先 DELETE 整类
再逐行 INSERT。门若开在 API 层,任何绕过 API 的写入路径(部署期全量重建、
admin 重建端点、以后新增的路径)都不过闸;而门开在 writer 上,
**DELETE 之前**就全部扫完 —— 半份 release = 用户当场问不出答案,比拒绝坏得多。
:func:`test_poisoned_bulk_write_is_rejected_before_the_destructive_delete` 打的就是这条。
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.kb_terminology_gate import KbTerminologyViolation

ADMIN = {"id": 1, "is_admin": True, "username": "gate-admin"}

_POISON_ANSWER = "充进来的是**充值积分** · 可以花。扣 5% 手续费。"
_CLEAN_ANSWER = "充进来的是**充值算力** · 可以花。手续费以退款页显示为准。"


# ── 底座 ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def kb_ready():
    """真建 kb_chunks + faq_items 两张表(不手写精简 schema)。"""
    from api.faq_api import init_faq_tables
    from db.kb_db import init_kb_tables

    init_kb_tables()
    init_faq_tables()
    yield


@pytest.fixture()
def http(kb_ready):
    """真 HTTP。中间件在生产里负责注入 ``request.state.user``,这里等价替身。"""
    from api.faq_api import router as faq_router

    app = FastAPI()

    @app.middleware("http")
    async def _inject_user(request, call_next):
        request.state.user = ADMIN
        return await call_next(request)

    app.include_router(faq_router)
    with TestClient(app) as client:
        yield client


#: 🔴 本文件建的 FAQ 一律带这个标记,并在 fixture 里**无条件**清理。
#:    起因是实测踩到的:反向变异把前置校验拆掉后,这些用例会**真的**建出行来
#:    (判据正确转红),但变异 runner 只还原源码、不还原库 ⇒ 残留行把后面
#:    `test_built_faq_chunks_carry_no_legacy_terminology` 污染成假红。
#:    「判据失败时也要清理」不是洁癖 —— 不清理就会把下一条判据的结论弄脏。
_MARKER = "[门判据]"


@pytest.fixture(autouse=True)
def _purge_marked_rows():
    def purge():
        from db.connection import get_connection
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM faq_items WHERE question LIKE %s",
                        ("%" + _MARKER + "%",))
            conn.commit()
        finally:
            conn.close()

    purge()
    yield
    purge()


def _counts() -> tuple[int, int]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM kb_chunks")
        chunks = int(cur.fetchone()["n"])
        cur.execute("SELECT count(*) AS n FROM faq_items")
        faqs = int(cur.fetchone()["n"])
        return chunks, faqs
    finally:
        conn.close()


# ── ① 工单点名的那条 ─────────────────────────────────────────────────────
def test_publishing_a_faq_with_legacy_unit_is_rejected_with_zero_kb_writes(http):
    """🔴 工单判据本体:拒绝 + kb_chunks 零写入 + faq_items 也零写入。"""
    chunks_before, faqs_before = _counts()
    response = http.post("/api/admin/faq/items", json={
        "question": "余额不足时怎么充值" + _MARKER,
        "category": "billing",
        "answer_md": _POISON_ANSWER,
    })
    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "KB_TERMINOLOGY_REJECTED", detail
    chunks_after, faqs_after = _counts()
    assert chunks_after == chunks_before, "kb_chunks 被写了 —— 门没拦住写入"
    assert faqs_after == faqs_before, "faq_items 被写了 —— 拒绝发生在写库之后"


def test_the_rejection_tells_the_admin_how_to_fix_it(http):
    """报错必须带改法。拒绝而不给改法,下一个人只会把门关掉。"""
    response = http.post("/api/admin/faq/items", json={
        "question": "怎么充值改法" + _MARKER,
        "category": "billing",
        "answer_md": _POISON_ANSWER,
    })
    detail = response.json()["detail"]
    assert detail["problems"], detail
    assert detail["fixes"], "只说'被拒绝了'不说'改成什么' = 门迟早被关掉"
    blob = " ".join(detail["fixes"])
    assert "算力" in blob                     # 旧计价单位的改法
    assert "以页面显示为准" in blob            # 写死比率的改法
    # 逐条命中要指得出**是哪句话**,不是只报一个总数。
    assert any("积分" in problem for problem in detail["problems"]), detail


def test_a_clean_faq_still_publishes(http):
    """🔁 反向对照:干净的 FAQ 必须能发。

    没有这条的话,「门恒红」和「门正确」在上面两条里长得一模一样,
    而恒红的门第二天就会被人关掉。
    """
    _, faqs_before = _counts()
    response = http.post("/api/admin/faq/items", json={
        "question": "余额不足时怎么充值干净" + _MARKER,
        "category": "billing",
        "answer_md": _CLEAN_ANSWER,
    })
    assert response.status_code == 201, response.text
    _, faqs_after = _counts()
    assert faqs_after == faqs_before + 1

    http.delete("/api/admin/faq/items/{0}".format(response.json()["id"]))


def test_patch_gate_looks_at_the_merged_text_not_just_the_body(http):
    """PATCH 是部分更新:门要打**合并后**的文本。

    只打 body 的话,「标题干净、正文脏」这种改法会从缝里过去 ——
    因为 PATCH 允许只传 answer_md,而校验若只看 body 里传了什么就漏掉旧标题,
    反之亦然。这条构造「只传脏正文」的 PATCH。
    """
    created = http.post("/api/admin/faq/items", json={
        "question": "PATCH 合并校验" + _MARKER,
        "category": "billing",
        "answer_md": _CLEAN_ANSWER,
    })
    assert created.status_code == 201, created.text
    faq_id = created.json()["id"]
    try:
        chunks_before, _ = _counts()
        response = http.patch(
            "/api/admin/faq/items/{0}".format(faq_id),
            json={"answer_md": _POISON_ANSWER},
        )
        assert response.status_code == 400, response.text
        chunks_after, _ = _counts()
        assert chunks_after == chunks_before
        # 库里那条必须还是干净版本(拒绝不能留下半份)。
        fetched = http.get("/api/admin/faq/items/{0}".format(faq_id))
        assert "积分" not in (fetched.json().get("answer_md") or "")
    finally:
        http.delete("/api/admin/faq/items/{0}".format(faq_id))


# ── writer 层 fail-closed(收口点) ──────────────────────────────────────
def test_poisoned_bulk_write_is_rejected_before_the_destructive_delete(kb_ready):
    """🔴 门必须在 DELETE **之前**响。

    ``replace_chunks_transactionally`` 是 clear-then-insert。判据的做法:
    先塞一条已知的干净 preset chunk,再拿一批含旧词的 rows 去 replace ——
    要求 ① 抛异常,② **那条干净 chunk 还在**。
    门若开在 INSERT 循环里,DELETE 已经执行过,干净那条就没了。
    """
    from db.kb_db import insert_chunk, replace_chunks_transactionally
    from db.connection import get_connection

    marker_slug = "preset_gate_probe_r3p7"
    insert_chunk(
        source_type="preset", source_slug=marker_slug,
        source_title="门判据探针", content="这条是干净的算力说明。",
    )

    def _marker_exists() -> bool:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT count(*) AS n FROM kb_chunks WHERE source_slug = %s", (marker_slug,))
            return int(cur.fetchone()["n"]) > 0
        finally:
            conn.close()

    assert _marker_exists(), "探针没插进去 —— 后面的断言会空即通过"
    with pytest.raises(KbTerminologyViolation):
        replace_chunks_transactionally(source_types=("preset",), chunks=[
            {"source_type": "preset", "source_slug": "preset_poison",
             "source_title": "怎么充值", "content": "充进来的是充值积分。"},
        ])
    assert _marker_exists(), "DELETE 已经跑过了 —— 门开晚了,留下半份 release"

    # 🔁 反向对照:干净 rows 必须能替换成功(否则这道门是恒红的)。
    replace_chunks_transactionally(source_types=("preset",), chunks=[
        {"source_type": "preset", "source_slug": marker_slug,
         "source_title": "门判据探针", "content": "这条是干净的算力说明。"},
    ])
    assert _marker_exists()


def test_single_insert_writer_is_also_gated(kb_ready):
    """两个 writer 都要过闸。只关一个 = 另一个是敞开的门。

    [R3-P9 ⑤] 毒串换成 ② 域池名。原来用的是「设置额度」—— 那是 ③ 域裸词,
    已按 Review 裁定降为 advisory(warn 不 block),拿它做毒串会把
    「门还在不在」测成「③ 域降没降级」。② 域池名是硬门,不受降级影响。
    """
    from db.kb_db import insert_chunk

    with pytest.raises(KbTerminologyViolation):
        insert_chunk(
            source_type="sys_page", source_slug="sys_gate_probe",
            source_title="席位怎么设", content="老板的工具额度用完了就停。",
        )


def test_an_advisory_only_text_passes_the_writer(kb_ready):
    """🔁 配对反向:只含 ③ 域裸词的文本**能写进去**(advisory 不拦)。

    没有这条,上面那条"抛了"无法区分「门在拦池名」和「门在拦一切带额度的字」。
    """
    from db.kb_db import insert_chunk

    insert_chunk(
        source_type="sys_page", source_slug="sys_advisory_probe",
        source_title="席位怎么设", content="靠老板在团队管理里设置额度来控制。",
    )


def test_both_writers_are_covered_by_the_gate_not_just_the_ones_i_remembered():
    """🔴 覆盖面判据:``db/kb_db.py`` 里**每一个**往 kb_chunks 写的函数都要过闸。

    不数「我加了几处 assert」,而是用结构锚取 writer 全集,再要求每个 writer
    的函数体里出现门调用 —— 新增第三个 writer 而忘了加门,这条转红。
    """
    import ast
    import inspect

    import db.kb_db as kb_db
    # [R3-P9 ③] 包内相对导入 —— 绝对路径 ``tests.xiaobang...`` 只在
    # 「rootdir 恰好在 sys.path 上」时能解析,单文件独立跑会 ImportError。
    # 靠全量跑的 import 顺序续命 = 判据的可运行性依赖别的判据先跑过。
    from . import kb_index_sources as idx

    writers = idx.kb_chunks_writers()
    assert writers, "writer 全集为空 —— 分母没了"
    source = inspect.getsource(kb_db)
    tree = ast.parse(source)
    gated = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in writers:
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    name = getattr(inner.func, "attr", None) or getattr(inner.func, "id", None)
                    if name in ("assert_kb_text_clean", "assert_chunk_rows_clean"):
                        gated.add(node.name)
                        break
    assert gated == writers, "没过闸的 writer: {0}".format(sorted(writers - gated))
