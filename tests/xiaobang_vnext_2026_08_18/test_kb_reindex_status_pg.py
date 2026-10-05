"""判据 · WO-A ②:fire-and-forget 重建的收口 —— 失败告警 + 前台状态位。

## 工单原话与这里的对应

「重建失败触发 ai_ops 告警(复用 raise_wiring_alert 产线),前台可见
『索引更新中/失败』状态位;判据=注入必败重建→告警行落库。」

* **注入必败重建 → 告警行落库** = :func:`test_a_failing_reindex_lands_an_alert_row`
  (打真库真行,不是断言"调过 alert 函数了");
* **前台可见** = 两条:后端 :func:`reindex_status` 的状态机,
  以及前端**真的在消费**这个端点的结构锚 ——
  端点建好没人接的死元素,本仓 8 月已经出过一次(灯不灭真因=端点没接前端)。

## 反向对照

* 成功一次要把告警收掉(否则状态位永久停在失败);
* 告警表读不到时状态位必须是 ``unknown`` 而**不是** ``idle`` ——
  「读不到」和「一切正常」在返回值上同形,是本仓反复付费的那种假绿。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from . import conftest as vnext_conftest

ROOT = Path(__file__).resolve().parents[2]
AI_OPS_MIGRATION = ROOT / "scripts" / "migration_ai_ops_center_2026_07_01.sql"
ADMIN_PAGE = ROOT / "frontend" / "src" / "pages" / "Admin" / "HelpCenterAdmin" / "index.tsx"
ADMIN_API = ROOT / "frontend" / "src" / "pages" / "Admin" / "HelpCenterAdmin" / "api.ts"


@pytest.fixture(scope="module", autouse=True)
def _tables():
    import psycopg2

    conn = psycopg2.connect(vnext_conftest.EXACT_THROWAWAY_URL)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(AI_OPS_MIGRATION.read_text(encoding="utf-8"))
    finally:
        conn.close()

    from api.faq_api import init_faq_tables
    from db.kb_db import init_kb_tables

    init_kb_tables()
    init_faq_tables()
    try:
        yield
    finally:
        # 本模块会真跑 ``reindex_faq``(写 faq chunk)—— 不清掉会顶红
        # ``tests/system_kb`` 的检索排序判据。理由详见 parity 那份的同名函数。
        from .test_preset_manifest_parity_pg import drop_release_rows

        drop_release_rows()


@pytest.fixture()
def clean_alerts():
    from services.kb_release_ops import KB_STATUS_ALERT_RULES

    def _wipe():
        from db.connection import get_connection

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM ai_ops_alerts WHERE rule_key IN %s",
                        (tuple(KB_STATUS_ALERT_RULES),))
            conn.commit()
        finally:
            conn.close()

    _wipe()
    yield _wipe
    _wipe()


def _firing(rule_key: str) -> list[dict]:
    from db.connection import get_connection

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT rule_key, fingerprint, title, detail, payload FROM ai_ops_alerts "
            "WHERE rule_key = %s AND status = 'firing' ORDER BY id", (rule_key,))
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════════════
# 注入必败重建 → 告警行落库
# ══════════════════════════════════════════════════════════════════════════

def test_a_failing_reindex_lands_an_alert_row(clean_alerts, monkeypatch):
    """把 ``reindex_faq`` 注成必败 ⇒ ``ai_ops_alerts`` 里出现一条 firing。

    注入点选在 indexer 那一层而不是 stub 掉 ``trigger_reindex`` 自己 ——
    stub 掉被测函数的话,判据验的是 stub。
    """
    import api.xiaobang_api as xb
    import tools.xiaobang_kb_indexer as indexer
    from services.kb_release_ops import KB_REINDEX_ALERT_RULE

    def _boom():
        raise RuntimeError("注入:reindex_faq 必败")

    monkeypatch.setattr(indexer, "reindex_faq", _boom)
    with pytest.raises(RuntimeError):
        asyncio.run(xb.trigger_reindex(scope="faq"))

    rows = _firing(KB_REINDEX_ALERT_RULE)
    assert len(rows) == 1, rows
    assert rows[0]["fingerprint"] == "faq"
    assert "注入:reindex_faq 必败" in rows[0]["detail"], rows[0]["detail"]


def test_the_status_bit_turns_failed_after_that(clean_alerts, monkeypatch):
    """前台状态位读的是同一条告警(跨进程:部署期那次重建是另一个进程)。"""
    import api.xiaobang_api as xb
    import tools.xiaobang_kb_indexer as indexer
    from services.kb_release_ops import reindex_status

    monkeypatch.setattr(indexer, "reindex_faq",
                        lambda: (_ for _ in ()).throw(RuntimeError("注入必败")))
    with pytest.raises(RuntimeError):
        asyncio.run(xb.trigger_reindex(scope="faq"))

    status = reindex_status()
    assert status["state"] == "failed", status
    assert status["failures"], status


def test_a_successful_reindex_clears_the_failed_bit(clean_alerts, monkeypatch):
    """反向对照:一次成功要把状态位收回 idle。

    只会拉响不会恢复 = 状态位永久红 = 下一个人直接无视它。
    """
    import api.xiaobang_api as xb
    import tools.xiaobang_kb_indexer as indexer
    from services.kb_release_ops import reindex_status

    monkeypatch.setattr(indexer, "reindex_faq",
                        lambda: (_ for _ in ()).throw(RuntimeError("注入必败")))
    with pytest.raises(RuntimeError):
        asyncio.run(xb.trigger_reindex(scope="faq"))
    assert reindex_status()["state"] == "failed"

    monkeypatch.undo()
    asyncio.run(xb.trigger_reindex(scope="faq"))          # 真重建,真写库
    status = reindex_status()
    assert status["state"] == "idle", status


def test_the_status_bit_is_running_while_a_rebuild_is_in_flight(clean_alerts):
    from services.kb_release_ops import reindex_status, track_reindex

    assert reindex_status()["state"] == "idle"
    with track_reindex("all"):
        inflight = reindex_status()
    after = reindex_status()

    assert inflight["state"] == "running", inflight
    assert inflight["running"][0]["scope"] == "all"
    assert after["state"] == "idle", after


def test_unreadable_alerts_report_unknown_not_idle(monkeypatch):
    """🔴 读不到告警表时不许谎报干净。

    ``unknown`` 与 ``idle`` 在前台是两句不同的话;把前者说成后者,
    等于告诉管理员「一切正常」而其实我们什么都不知道。
    """
    import db.ai_ops_db as ai_ops
    from services.kb_release_ops import reindex_status

    def _boom(*_a, **_kw):
        raise RuntimeError("注入:告警表不可读")

    monkeypatch.setattr(ai_ops, "list_alerts", _boom)
    status = reindex_status()
    assert status["state"] == "unknown", status
    assert status["alerts_readable"] is False


# ══════════════════════════════════════════════════════════════════════════
# 接线:一个收口点 + 前端真的在消费
# ══════════════════════════════════════════════════════════════════════════

def test_track_reindex_sits_on_the_single_chokepoint():
    """状态位套在 ``trigger_reindex`` 里 —— admin 手动与 FAQ CRUD 钩子的共同收口点。

    套在调用点上 = 同一个谓词写两处,必有一处漏(漏掉那次失败又变回静默)。
    把 ``with track_reindex(...)`` 删掉 ⇒ 本条转红。
    """
    import ast
    import inspect

    import api.xiaobang_api as xb

    body = ast.unparse(ast.parse(inspect.getsource(xb.trigger_reindex)))
    assert "track_reindex" in body, "重建的唯一收口点没有状态位"

    import api.faq_api as faq_api

    hook = ast.unparse(ast.parse(inspect.getsource(faq_api._run_faq_reindex_with_retry)))
    assert "trigger_reindex" in hook, (
        "fire-and-forget 钩子绕开了收口点 —— 它的失败不会进状态位")


def test_the_frontend_actually_consumes_the_status_endpoint():
    """结构锚:端点建好了没人接 = 死元素(本仓 8 月刚踩过)。"""
    api_src = ADMIN_API.read_text(encoding="utf-8")
    page_src = ADMIN_PAGE.read_text(encoding="utf-8")

    assert "/api/admin/xiaobang/reindex-status" in api_src, "前端没有这个端点的客户端"
    # 🔴 不能只搜名字:import 那一行也含它,把调用整条换掉判据照样绿
    #    (变异 N19 存活实测)。要的是**带括号的那次调用**。
    assert "getReindexStatus()" in page_src, "页面 import 了但没调 —— 那是死元素"
    assert "索引更新中" in page_src and "索引更新失败" in page_src, (
        "两个状态位文案缺一 —— 工单要的是「更新中/失败」两个")


def test_the_status_endpoint_is_admin_only():
    """状态位带着告警明细(含内部报错文本),不许给非 admin。"""
    import ast
    import inspect

    import api.xiaobang_api as xb

    body = ast.unparse(ast.parse(inspect.getsource(xb.admin_xiaobang_reindex_status)))
    assert "is_admin" in body and "403" in body, body
