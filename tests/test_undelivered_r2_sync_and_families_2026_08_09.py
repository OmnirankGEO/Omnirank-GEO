"""未送达不计费 R2 · 同步路径 + 异常族扩面 · 判据锁(WO R2 2026-08-09 · 资金)

上一版(`abb13cd5`)只覆盖了异步 `llm_track` + httpx 三个连接类。复审点名三处漏面:
  (a) `_SyncLLMTrack.__exit__` 从不置位 —— sync 8 个调用点全裸奔;
  (b) aiohttp 链完全没覆盖 —— `competition_analyzer`(秘塔那条)与
      `api_5118`(`billable_units` **前置**,单次失败可记 ¥1.80)都是 aiohttp;
      `ImageConnectFailed`(connect 重试用尽)也不在集合里。

🔴 判据仍然打在**真表的行数与金额**上。每条【必须命中】配【必须不命中】。
"""
from __future__ import annotations

import asyncio

import aiohttp
import httpx
import pytest

from tools.llm_call_tracker import llm_track, llm_track_sync

LLM_CALL_LOG_DDL = """
CREATE TABLE IF NOT EXISTS llm_call_log (
    id SERIAL PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    caller TEXT NOT NULL, platform TEXT NOT NULL, model TEXT,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0, estimated_cost NUMERIC(10,6) DEFAULT 0,
    duration_ms INTEGER DEFAULT 0, brand_id INTEGER, quote_id INTEGER,
    user_id INTEGER, success BOOLEAN DEFAULT TRUE, error_msg TEXT, metadata JSONB
);
"""


@pytest.fixture
def cost_db():
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(LLM_CALL_LOG_DDL)
        cur.execute("DELETE FROM llm_call_log")
    yield
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM llm_call_log")


def _rows(platform: str) -> list:
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT estimated_cost, success, metadata FROM llm_call_log "
            "WHERE platform = %s ORDER BY id",
            (platform,),
        )
        return [dict(r) for r in cur.fetchall()]


def _aiohttp_connector_error() -> aiohttp.ClientConnectorError:
    """造一个真的 `ClientConnectorError`(它的构造要 connection_key + OSError)。"""
    from aiohttp.client_reqrep import ConnectionKey

    key = ConnectionKey("metaso.cn", 443, True, True, None, None, None)
    return aiohttp.ClientConnectorError(key, OSError(111, "Connection refused"))


# ══════════════════════════════════════════════════════════════════════════
# §1 (a) 同步路径 `_SyncLLMTrack`
# ══════════════════════════════════════════════════════════════════════════

def _sync_call(platform, model, exc, **kw):
    try:
        with llm_track_sync(caller="test_sync", platform=platform, model=model, **kw) as ctx:
            if exc is not None:
                raise exc
            ctx.record(success=True, input_tokens=1, output_tokens=1)
    except Exception:
        pass


def test_sync_path_zeroes_undelivered(cost_db):
    """【必须命中 · 打在钱上】sync 版未送达也必须记 0(上一版这里完全裸奔)。"""
    _sync_call("metaso", "search", httpx.ConnectError("refused"))
    rows = _rows("metaso")
    assert len(rows) == 1, "sync 版同样落行(方案 a 口径一致)"
    assert float(rows[0]["estimated_cost"]) == 0.0
    assert (rows[0]["metadata"] or {}).get("undelivered") is True
    assert rows[0]["success"] is False


def test_sync_path_still_bills_delivered(cost_db):
    """【必须不命中 · 反向对照】sync 版送达的照常计费(没把 sync 整条清零)。"""
    _sync_call("metaso", "search", None)
    rows = _rows("metaso")
    assert float(rows[0]["estimated_cost"]) == pytest.approx(0.013)
    assert rows[0]["success"] is True


def test_sync_path_still_bills_already_delivered_errors(cost_db):
    """【必须不命中】sync 版对 ReadTimeout / 业务异常照记(边界与 async 版一致)。"""
    _sync_call("metaso", "search", httpx.ReadTimeout("no response"))
    assert float(_rows("metaso")[0]["estimated_cost"]) == pytest.approx(0.013)


def test_sync_and_async_agree_on_the_same_exception(cost_db):
    """【必须命中 · 元判据】同一个异常,两条路径的判定必须一致。

    两条路径各写一份判据 = 迟早分叉;这条锁把它们绑在一起。
    """
    async def _async_call(exc):
        try:
            async with llm_track("test_async", "metaso", model="search"):
                raise exc
        except Exception:
            pass

    for exc in (httpx.ConnectError("x"), _aiohttp_connector_error(), httpx.ReadTimeout("x")):
        from db.connection import get_db

        with get_db() as conn:
            conn.cursor().execute("DELETE FROM llm_call_log")
        _sync_call("metaso", "search", exc)
        sync_cost = float(_rows("metaso")[0]["estimated_cost"])

        with get_db() as conn:
            conn.cursor().execute("DELETE FROM llm_call_log")
        asyncio.run(_async_call(exc))
        async_cost = float(_rows("metaso")[0]["estimated_cost"])

        assert sync_cost == async_cost, f"{type(exc).__name__}: sync {sync_cost} vs async {async_cost}"


# ══════════════════════════════════════════════════════════════════════════
# §2 (b) 异常族扩面 —— 真值表
# ══════════════════════════════════════════════════════════════════════════

def _image_connect_failed():
    from services.marketing.image_client import ImageConnectFailed

    return ImageConnectFailed("connect_failed(attempts=5)")


@pytest.mark.parametrize("make_exc, billed, why", [
    (lambda: httpx.ConnectError("refused"), False, "httpx 连接失败"),
    (lambda: httpx.ConnectTimeout("t"), False, "httpx 连接超时"),
    (lambda: httpx.ProxyError("p"), False, "httpx 代理失败"),
    (_aiohttp_connector_error, False, "aiohttp 连接失败(competition_analyzer / api_5118 走这条)"),
    (_image_connect_failed, False, "connect 重试用尽标记"),
    # ── 下面都算**已送达**,必须照记 ──
    (lambda: httpx.ReadTimeout("t"), True, "已建连、没等到回应 —— 可能已受理"),
    (lambda: aiohttp.ServerDisconnectedError(), True, "连上了又断 —— 可能已受理"),
    (lambda: aiohttp.ServerTimeoutError(), True, "超时,不是未送达"),
    (lambda: RuntimeError("metaso 业务级失败: errCode=3000"), True, "业务级失败(HTTP 200 通道)"),
    (lambda: ValueError("bad json"), True, "解析失败 —— 响应已经拿到了"),
])
def test_no_send_family_truth_table(cost_db, make_exc, billed, why):
    """【真值表】扩面之后的边界:一个不多、一个不少。"""
    _sync_call("metaso", "search", make_exc())
    cost = float(_rows("metaso")[0]["estimated_cost"])
    if billed:
        assert cost == pytest.approx(0.013), f"{why} 属已送达,必须照记"
    else:
        assert cost == 0.0, f"{why} 属未送达,不许计费"


def test_aiohttp_parent_classes_are_deliberately_excluded():
    """【必须不命中】只收 `ClientConnectorError`,不收它那些更宽的父类。

    `ClientConnectionError` / `ClientOSError` 是 `ServerDisconnectedError` 的
    共同祖先,收进来会把"连上了又断"算成未送达 → 少记成本。
    """
    from tools.search.provider_circuit_breaker import undelivered_error_types

    types = undelivered_error_types()
    assert aiohttp.ClientConnectorError in types
    assert aiohttp.ClientConnectionError not in types
    assert aiohttp.ClientOSError not in types
    assert aiohttp.ServerDisconnectedError not in types
    # 子类靠 isinstance 自动覆盖,不必逐个列
    assert issubclass(aiohttp.ClientProxyConnectionError, aiohttp.ClientConnectorError)


def test_cause_walk_would_have_missed_image_connect_failed():
    """【选型佐证】这条钉死"为什么不选递归 `__cause__`"。

    `ImageConnectFailed` 是**裸 raise**、且在 for 循环之外 ——
    `__cause__` 与 `__context__` 都是 None,递归方案对它完全无效。
    这不是偏好,是构造决定的。
    """
    from services.marketing import image_client

    exc = None
    try:
        try:
            raise httpx.ConnectError("attempt failed")
        except httpx.ConnectError:
            pass                       # 与 _submit_with_retry 同形:except 里只记录
        raise image_client.ImageConnectFailed("connect_failed(attempts=5)")
    except image_client.ImageConnectFailed as e:
        exc = e
    assert exc.__cause__ is None, "裸 raise → 没有 __cause__"
    assert exc.__context__ is None, "在 except 之外 raise → 也没有 __context__"


# ══════════════════════════════════════════════════════════════════════════
# §3 两条被点名的链:真值表落在它们各自的计费形态上
# ══════════════════════════════════════════════════════════════════════════

def test_competition_analyzer_chain_shape_is_covered(cost_db):
    """【必须命中 · 秘塔那条链】aiohttp + flat_rate 按次计。

    `tools/competition_analyzer` 用 `llm_track("metaso_competition_search",
    "metaso", model="search")` 包 aiohttp 调用 —— 连不上时抛
    `ClientConnectorError`,穿过 `llm_track` 被外层 `except Exception` 接住。
    """
    async def _run():
        try:
            async with llm_track("metaso_competition_search", "metaso", model="search",
                                 metadata={"size": 100}):
                raise _aiohttp_connector_error()
        except Exception:
            pass

    asyncio.run(_run())
    rows = _rows("metaso")
    assert len(rows) == 1 and float(rows[0]["estimated_cost"]) == 0.0


def test_api_5118_prepaid_billable_units_are_zeroed(cost_db):
    """【必须命中 · ¥1.80 那条】`billable_units` 是**前置**的,未送达时必须一起归零。

    `api_5118` 在进 `llm_track` 时就把 `billable_units = len(keywords)` 写进
    metadata(按词计价)。300 个词一次连不上照 300 个词记。
    """
    async def _run(n_keywords):
        try:
            async with llm_track("5118_search_volume_submit", "5118", model="search_volume",
                                 metadata={"keyword_count": n_keywords,
                                           "billable_units": n_keywords}):
                raise _aiohttp_connector_error()
        except Exception:
            pass

    asyncio.run(_run(300))
    rows = _rows("5118")
    assert len(rows) == 1
    assert float(rows[0]["estimated_cost"]) == 0.0, "前置的 billable_units 也要被归零"


def test_api_5118_delivered_still_bills_by_units(cost_db):
    """【必须不命中 · 反向对照】送达时 `billable_units` 照常放大计费。

    没有这条,"把 billable_units 一律当 0"会把 5118 整条链的成本清零还满分。
    """
    async def _run(n_keywords):
        async with llm_track("5118_search_volume_submit", "5118", model="search_volume",
                             metadata={"keyword_count": n_keywords,
                                       "billable_units": n_keywords}) as tracker:
            tracker.record(success=True)

    asyncio.run(_run(300))
    rows = _rows("5118")
    assert float(rows[0]["estimated_cost"]) == pytest.approx(0.013 * 300), (
        "送达时 300 词必须记 ¥1.80"
    )


def test_missing_aiohttp_degrades_to_empty_tuple(monkeypatch):
    """【必须不命中】环境没装 aiohttp 时退化成空元组,**不抛**。

    🔴 分诊记档:变异 M07 第一版存活 —— 因为本机装了 aiohttp,那个 except
    分支根本执行不到,**变异是空操作**。要隔离它必须真的让 import 失败。
    这条与"熔断/计费设施自己坏掉不许把主链带下水"是同一条方向。
    """
    import builtins

    from tools.search import provider_circuit_breaker as cb

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "aiohttp":
            raise ImportError("simulated: aiohttp not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    assert cb._aiohttp_no_send_types() == (), "取不到 aiohttp 时必须返回空元组"
    # 正向对照:装着的时候必须真的返回那一族(否则上面那条恒真)
    monkeypatch.undo()
    import aiohttp as _ah

    assert cb._aiohttp_no_send_types() == (_ah.ClientConnectorError,)
