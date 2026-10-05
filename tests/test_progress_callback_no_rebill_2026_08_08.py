"""进度回调不许触发重生成 · 判据锁(WO 回调重复计费 2026-08-08 · 资金)

缺陷:`generate_one` 里进度回调与「生成 + 保存」共用同一个 ``try:``,
而那个 ``except`` 打的是「🔄 主模型失败」—— 文章**已经生成好、已经存库成功**之后
回调抛一下,整条被判成模型失败 → 切兜底模型**从头再生成一遍** → 又一次真实 LLM 花费。

🔴 判据打在**钱**上:每一次 `_generate_validated_with_rewrite_once` 都是一次真实
LLM 往返,`llm_track` 当场落一行 `llm_call_log` 且**不会因为后来被判失败而冲正**。
所以本文件的替身生成函数**真的走一遍 `llm_track`**,断言数的是 `llm_call_log` 真表行数,
不是"函数被调了几次"这种间接量。

每条【必须命中】都配【必须不命中】。变异见
`tests/mutation_runner_progress_callback_2026_08_08.py`。
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest


LLM_CALL_LOG_DDL = """
CREATE TABLE IF NOT EXISTS llm_call_log (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    caller TEXT NOT NULL,
    platform TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    estimated_cost NUMERIC(10,6) DEFAULT 0,
    duration_ms INTEGER DEFAULT 0,
    brand_id INTEGER,
    quote_id INTEGER,
    user_id INTEGER,
    success BOOLEAN DEFAULT TRUE,
    error_msg TEXT,
    metadata JSONB
);
"""


@pytest.fixture
def billing_db():
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(LLM_CALL_LOG_DDL)
        cur.execute("DELETE FROM llm_call_log")
    yield
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM llm_call_log")


def _billing_rows() -> int:
    """`llm_call_log` 里写作链的真实计费行数 = 这一批到底打了几枪。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM llm_call_log WHERE caller = 'article_writing'")
        return int(cur.fetchone()["n"])


class _Cursor:
    rowcount = 1

    def execute(self, sql, params=()):
        self.last_sql = sql

    def fetchone(self):
        return {"writing_started_at": "lease-2"}


class _Connection:
    def __init__(self):
        self.cur = _Cursor()

    def cursor(self):
        return self.cur

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _service(monkeypatch, *, with_fallback: bool):
    """装一个能跑 `generate_articles` 的最小服务(harness 抄自
    tests/test_article_generation_task_closure.py 的既有形状)。"""
    import writing.article_generator_service as module
    import writing.llm_utils as llm_utils
    from writing.article_generator_service import ArticleGeneratorService

    fake_db = types.ModuleType("db.diagnosis_db")
    fake_db.get_connection = _Connection
    monkeypatch.setitem(sys.modules, "db.diagnosis_db", fake_db)
    monkeypatch.setattr(llm_utils, "get_api_key_for_provider", lambda provider: "test-key")
    # 🔴 兜底模型必须**真的配上** —— 没配的话"切兜底重生成"这条路根本走不到,
    #    整个用例就成了空操作(判据打偏的经典形状)。
    monkeypatch.setattr(
        module, "get_fallback_llm_config",
        (lambda: ("deepseek", "https://fb.example/v1", "fb-key", "deepseek-chat"))
        if with_fallback else (lambda: ("", "", "", "")),
    )

    service = ArticleGeneratorService(quote_id=1, brand_name="Brand", industry="Industry")

    async def _no_project_update():
        return None

    monkeypatch.setattr(service, "_update_project_status", _no_project_update)
    return service


def _install_generation(monkeypatch, service, calls):
    """替身生成函数:**真的走一遍 llm_track**,所以每次生成都会在
    `llm_call_log` 落一行 —— 与生产同一个计费出口。"""
    from tools.llm_call_tracker import llm_track

    async def _generate(*args, **kwargs):
        calls["generate"] += 1
        async with llm_track(
            "article_writing", "deepseek", model="deepseek-chat",
            metadata={"topic_id": 8, "generation_request_id": "req-1"},
        ) as tracker:
            tracker.record(success=True, input_tokens=10, output_tokens=10)
        return {"title": "标题", "content": "正文" * 150, "style": "buying_guide"}

    async def _save(*args, **kwargs):
        calls["save"] += 1
        return 4242

    monkeypatch.setattr(service, "_generate_validated_with_rewrite_once", _generate)
    monkeypatch.setattr(service, "_save_article", _save)


def _run(service):
    return asyncio.run(service.generate_articles(
        [{"id": 8, "title": "标题", "keyword": "关键词", "_writing_started_at": "lease-1"}],
        llm_override={"provider": "dashscope", "model": "model"},
    ))


# ══════════════════════════════════════════════════════════════════════════
# §1 回调炸 N 次,生成只计费 1 次(工单点名的那条判据)
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("n_raises", [1, 3, 10])
def test_callback_explosions_never_cause_a_second_generation(billing_db, monkeypatch, n_raises):
    """【必须命中 · 打在钱上】回调连炸 N 次,`llm_call_log` 仍然只有 1 行。"""
    service = _service(monkeypatch, with_fallback=True)
    calls = {"generate": 0, "save": 0, "cb": 0}
    _install_generation(monkeypatch, service, calls)

    def _boom(*args, **kwargs):
        calls["cb"] += 1
        if calls["cb"] <= n_raises:
            raise RuntimeError("SSE 客户端已断开")

    service.set_progress_callback(_boom)
    results = _run(service)

    assert calls["cb"] >= 1, "回调必须真的被调到,否则这条用例是空操作"
    assert calls["generate"] == 1, f"回调炸了不许重新生成(实际生成 {calls['generate']} 次)"
    assert _billing_rows() == 1, f"回调炸了不许多计费(llm_call_log {_billing_rows()} 行)"
    assert results and results[0].get("id") == 4242, "文章必须照常返回 —— 它本来就已经存好了"
    assert "error" not in (results[0] or {}), "回调炸了不许把这一篇标成失败"


def test_generation_failure_still_falls_back_and_bills_twice(billing_db, monkeypatch):
    """【必须不命中 · 反向对照】**真的**生成失败时,兜底重试与它的第二笔计费一个字不动。

    没有这条,"永远不重试"这种错误实现会把上面那条拿满分 ——
    而"永不重试"会砍掉一条真实的可用性设计(兜底模型)。
    """
    service = _service(monkeypatch, with_fallback=True)
    calls = {"generate": 0, "save": 0}
    _install_generation(monkeypatch, service, calls)

    from tools.llm_call_tracker import llm_track

    async def _first_fails(*args, **kwargs):
        calls["generate"] += 1
        async with llm_track("article_writing", "deepseek", model="m",
                             metadata={"topic_id": 8}) as tracker:
            tracker.record(success=False, error_msg="boom")
        if calls["generate"] == 1:
            raise TimeoutError("provider timeout")
        return {"title": "标题", "content": "正文" * 150, "style": "buying_guide"}

    monkeypatch.setattr(service, "_generate_validated_with_rewrite_once", _first_fails)
    service.set_progress_callback(lambda *a, **k: None)
    results = _run(service)

    assert calls["generate"] == 2, "真失败必须切兜底再生成一次(这是设计,不是缺陷)"
    assert _billing_rows() == 2, "兜底那一次本来就该计费"
    assert results and results[0].get("id") == 4242


def test_save_failure_still_refuses_the_second_provider_call(billing_db, monkeypatch):
    """【必须不命中 · 反向对照】保存失败**不许**切兜底(既有设计:
    `ArticleSaveFailed` 被排除在兜底之外,不浪费第二次供应商调用)。

    这条保证我只吞了"回调"这一类异常,没有顺手把别的异常也吞掉。
    """
    from writing.article_generation_failure import ArticleSaveFailed

    service = _service(monkeypatch, with_fallback=True)
    calls = {"generate": 0, "save": 0}
    _install_generation(monkeypatch, service, calls)

    async def _save_boom(*args, **kwargs):
        raise ArticleSaveFailed()

    monkeypatch.setattr(service, "_save_article", _save_boom)
    service.set_progress_callback(lambda *a, **k: None)
    results = _run(service)

    assert calls["generate"] == 1, "保存失败不许再买一次供应商调用"
    assert _billing_rows() == 1
    assert results and results[0].get("error"), "保存失败仍然要落成失败态"


# ══════════════════════════════════════════════════════════════════════════
# §2 回调本身的契约
# ══════════════════════════════════════════════════════════════════════════

def test_all_three_callback_sites_go_through_the_single_helper(billing_db):
    """【必须命中 · 打在接线上】三个回调点必须都走 `_emit_progress` 单点。

    锁打在**源码接线**上而不是只打在 helper 上 —— 「函数对、接线缺」
    本周已经出现五次;改两处漏第三处也属于这一类。
    """
    import inspect

    from writing.article_generator_service import ArticleGeneratorService

    src = inspect.getsource(ArticleGeneratorService.generate_articles)
    code = "\n".join(
        line for line in src.split("\n") if not line.strip().startswith("#")
    )
    assert code.count("self._emit_progress(") == 3, (
        f"generate_articles 里应有 3 个 _emit_progress 调用,实际 {code.count('self._emit_progress(')}"
    )
    assert "self.progress_callback(" not in code, (
        "🔴 不许再有裸调用 —— 裸调用就是回到出事那天的形状"
    )


def test_emit_progress_swallows_and_reports(capsys):
    """【必须命中】回调异常被吞,但**留痕**(静默吞掉 = 下次没人查得出来)。"""
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService(quote_id=1, brand_name="B", industry="I")

    def _boom(*a, **k):
        raise RuntimeError("客户端断开")

    service.set_progress_callback(_boom)
    service._emit_progress(1, 1, "某标题", "completed")   # 不抛就是通过
    out = capsys.readouterr().out
    assert "进度回调异常" in out and "RuntimeError" in out, "必须留痕"


def test_emit_progress_passes_values_through(billing_db):
    """【必须不命中 · 反向对照】回调不炸时,四个参数逐字传过去。

    只有"吞异常"那条的话,`_emit_progress` 写成空函数也能满分。
    """
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService(quote_id=1, brand_name="B", industry="I")
    seen = []
    service.set_progress_callback(lambda *a: seen.append(a))
    service._emit_progress(3, 7, "标题X", "completed")
    assert seen == [(3, 7, "标题X", "completed")]


def test_no_callback_is_not_an_error(billing_db):
    """【必须不命中】没设回调时什么都不做,不抛(生产当前就是这个态)。"""
    from writing.article_generator_service import ArticleGeneratorService

    service = ArticleGeneratorService(quote_id=1, brand_name="B", industry="I")
    service._emit_progress(1, 1, "t", "completed")
