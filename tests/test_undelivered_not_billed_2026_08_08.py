"""未送达调用不产生成本账 · 判据锁(WO 2026-08-08 · 资金 · 随 21 班)

缺陷:`tools/llm_call_tracker.llm_track()` 的 ``finally`` **无条件**写
`llm_call_log`,`flat_rate_per_call` 型(秘塔 / 豆包 按次计)的
`estimate_cost()` 不看 `success` —— 于是**请求根本没送出去**(ConnectError 一族)
也照记一笔成本。熔断包(`439ebb0b`)只是止血(把量收住),根治是这一件。

方案 **(a)**:未送达**仍落行**,但 `estimated_cost = 0`(理由与对账影响见交付说明)。

🔴 判据打在**真表的行数与金额**上,不是打在 `estimate_cost()` 的返回值上。
每条【必须命中】配【必须不命中】("送达照常计费"就是那个反向对照)。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from tools.llm_call_tracker import llm_track

# DDL 与 db/monitoring_db.py 的建表逐字一致。
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
            "SELECT platform, model, estimated_cost, success, error_msg, metadata "
            "FROM llm_call_log WHERE platform = %s ORDER BY id",
            (platform,),
        )
        return [dict(r) for r in cur.fetchall()]


async def _call(platform: str, model: str, exc: BaseException | None):
    """跑一次真实的 `llm_track` 上下文;`exc` 非空时在块内抛出。"""
    try:
        async with llm_track("test_caller", platform, model=model) as tracker:
            if exc is not None:
                raise exc
            tracker.record(success=True, input_tokens=1, output_tokens=1)
    except Exception:
        pass


# ══════════════════════════════════════════════════════════════════════════
# §1 未送达 → 落行,但金额为 0
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("platform, model, unit_price", [
    ("metaso", "search", 0.013),
    ("doubao_search", "web_search", 0.013),
])
def test_undelivered_lands_a_row_with_zero_cost(cost_db, platform, model, unit_price):
    """【必须命中 · 打在钱上】ConnectError → 行还在(可观测),`estimated_cost = 0`。"""
    asyncio.run(_call(platform, model, httpx.ConnectError("connection refused")))
    rows = _rows(platform)
    assert len(rows) == 1, "方案 (a):行数口径不变 —— 未送达也要留一行"
    assert float(rows[0]["estimated_cost"]) == 0.0, (
        f"未送达不许计费,实际 {rows[0]['estimated_cost']}(单价 {unit_price})"
    )
    assert rows[0]["success"] is False
    assert "ConnectError" in (rows[0]["error_msg"] or ""), "异常类型要留在行里,便于对账"
    assert (rows[0]["metadata"] or {}).get("undelivered") is True, (
        "行要自描述 —— 对账时能一条 SQL 把未送达筛出来"
    )


@pytest.mark.parametrize("platform, model, unit_price", [
    ("metaso", "search", 0.013),
    ("doubao_search", "web_search", 0.013),
])
def test_delivered_call_is_still_billed(cost_db, platform, model, unit_price):
    """【必须不命中 · 反向对照】送达的调用**照常计费**。

    没有这条,"把 estimate_cost 恒返 0"这种实现会把上面那条拿满分 ——
    那等于把整张成本表清零。
    """
    asyncio.run(_call(platform, model, None))
    rows = _rows(platform)
    assert len(rows) == 1
    assert float(rows[0]["estimated_cost"]) == pytest.approx(unit_price), (
        "送达的 flat_rate 调用必须照单价记"
    )
    assert rows[0]["success"] is True


@pytest.mark.parametrize("exc, billed", [
    (httpx.ConnectError("refused"), False),
    (httpx.ConnectTimeout("timeout"), False),
    (httpx.ProxyError("proxy"), False),
    # 🔴 下面这些**不算**未送达:请求可能已经到了对方、可能已经被计费。
    (httpx.ReadTimeout("no response"), True),
    (httpx.RemoteProtocolError("half-closed"), True),
    (ValueError("business error"), True),
    (RuntimeError("boom"), True),
])
def test_only_the_no_send_class_is_free(cost_db, exc, billed):
    """【真值表】边界就是 image_client 那份「唯一的没送出去集合」,一个不多一个不少。"""
    asyncio.run(_call("metaso", "search", exc))
    rows = _rows("metaso")
    assert len(rows) == 1, "任何异常都要留一行(可观测性不变)"
    cost = float(rows[0]["estimated_cost"])
    if billed:
        assert cost == pytest.approx(0.013), f"{type(exc).__name__} 属已送达,必须照记"
    else:
        assert cost == 0.0, f"{type(exc).__name__} 属未送达,不许计费"


def test_criterion_reuses_the_single_definition(cost_db):
    """【必须命中 · 禁第二套】判据必须来自熔断包那份唯一定义。

    判据:把**那一份**换掉(monkeypatch),本模块的判定必须跟着变。
    不跟着变 = 这里私藏了第二份异常清单。
    """
    import tools.llm_call_tracker as tracker_mod
    from tools.search import provider_circuit_breaker as cb

    assert tracker_mod._is_undelivered_exception(httpx.ConnectError("x")) is True
    assert tracker_mod._is_undelivered_exception(httpx.ReadTimeout("x")) is False

    original = cb.undelivered_error_types
    try:
        cb.undelivered_error_types = lambda: (httpx.ReadTimeout,)
        assert tracker_mod._is_undelivered_exception(httpx.ReadTimeout("x")) is True, (
            "换掉唯一定义后判定没变 —— 说明这里私藏了第二份清单"
        )
        assert tracker_mod._is_undelivered_exception(httpx.ConnectError("x")) is False
    finally:
        cb.undelivered_error_types = original


def test_broken_criterion_falls_back_to_billing(cost_db, monkeypatch):
    """【必须不命中】判据取不到时**照旧计费**,不许静默少算。

    `llm_call_log` 是成本核算 SSOT:判据坏掉时宁可多记(看得见、能对账),
    也不要少记(账对不上还查不出为什么)。
    """
    import tools.search.provider_circuit_breaker as cb

    def _boom():
        raise RuntimeError("criterion unavailable")

    monkeypatch.setattr(cb, "undelivered_error_types", _boom)
    asyncio.run(_call("metaso", "search", httpx.ConnectError("refused")))
    rows = _rows("metaso")
    assert float(rows[0]["estimated_cost"]) == pytest.approx(0.013), (
        "判据不可用时必须回落到照旧计费"
    )


# ══════════════════════════════════════════════════════════════════════════
# §2 与熔断包(439ebb0b)的边界
# ══════════════════════════════════════════════════════════════════════════

def test_breaker_skipped_calls_produce_no_row_at_all(cost_db, monkeypatch):
    """【必须命中 · 边界】闸拦下的调用**根本不进 llm_track** → 一行都不落。

    两件事职责不同,不能混:
      · 熔断(439ebb0b):连续未送达到阈值后**不再发起**调用 → **无行**;
      · 本包:已经发起、但确定没送出去的那几次 → **有行、金额 0**。
    这条锁把边界钉死,防止后来的人把两者合并成"未送达就别落行"。
    """
    import tools.search.metaso_mcp as mm
    from tools.search import provider_circuit_breaker as cb

    cb.reset_provider_breakers()
    # [WO R2 2026-08-09 ①d] 原来是 setenv,而 METASO_MCP_CONFIG 当时在 import
    #   时就把 env 读死了 → 那一行是**死代码**,用例其实靠 .env 里恰好有 key
    #   才绿。②件已把 api_key 改成现取,setenv 现在也有效;但判据仍改打
    #   **官方接缝**(setitem)—— 它不依赖"env 什么时候被读"这个时序假设。
    import tools.search.metaso_mcp as _mm
    monkeypatch.setitem(_mm.METASO_MCP_CONFIG, "api_key", "test-key")

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)

        def _handler(request):
            raise httpx.ConnectError("connection refused", request=request)

        return real_client(transport=httpx.MockTransport(_handler), timeout=5.0)

    monkeypatch.setattr(mm.httpx, "AsyncClient", _factory)
    _real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _real_sleep(0))

    async def _drive(n):
        for _ in range(n):
            await mm._metaso_web_search_direct("q", size=5)

    asyncio.run(_drive(30))
    rows = _rows("metaso")

    # 闸把量收住了 → 行数远小于 30×2
    assert 0 < len(rows) <= 8, f"熔断应把行数收住,实际 {len(rows)} 行"
    # 而落下的这几行,金额必须全是 0(本包的贡献)
    assert all(float(r["estimated_cost"]) == 0.0 for r in rows), (
        "未送达那几行的金额必须全为 0 —— 熔断收量,本包收钱"
    )
    assert all(r["success"] is False for r in rows)
    cb.reset_provider_breakers()


def test_total_undelivered_cost_is_exactly_zero(cost_db):
    """【必须命中 · 对账口径】未送达行的金额合计恰好 0,而行数不为 0。

    这正是交付说明里给对账的那条 SQL 的形状:
      `SELECT count(*), sum(estimated_cost) FROM llm_call_log
        WHERE metadata->>'undelivered' = 'true'`
    """
    from db.connection import get_db

    for _ in range(7):
        asyncio.run(_call("metaso", "search", httpx.ConnectError("refused")))
    asyncio.run(_call("metaso", "search", None))  # 一次成功的,做分母

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*) AS n, coalesce(sum(estimated_cost), 0) AS total "
            "FROM llm_call_log WHERE metadata->>'undelivered' = 'true'"
        )
        row = dict(cur.fetchone())
    assert int(row["n"]) == 7, "未送达行必须还在(方案 a 的可观测性)"
    assert float(row["total"]) == 0.0, "未送达合计金额必须是 0"

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT coalesce(sum(estimated_cost), 0) AS total FROM llm_call_log "
            "WHERE metadata->>'undelivered' IS NULL"
        )
        delivered_total = float(dict(cur.fetchone())["total"])
    assert delivered_total == pytest.approx(0.013), "送达那一笔照常计入总成本"
