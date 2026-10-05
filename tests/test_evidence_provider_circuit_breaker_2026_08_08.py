"""证据检索供应商熔断 · 判据锁(WO 熔断单 2026-08-08 · 发车前置)

工单要的三件事,逐条配【必须命中】+【必须不命中】:
  §1 连续 N 次 ConnectError 后停止该供应商本批调用 —— 判据打在 **llm_call_log 真表行数**上,
     不是打在"函数返回了什么"上。花没花钱只有那张表说了算。
  §2 记告警 —— 判据打在 **ai_ops_alerts 真表行**上。
  §3 不影响普通写作主链,失败仍走「证据缺失允许写短」合同分支。

🔴 网络层用 `httpx.MockTransport` 注入,**保留真实的 httpx.AsyncClient 与真实的
   `_metaso_web_search_direct` / `doubao_web_search_raw` 代码路径** ——
   只把最底下那一层换掉。ConnectError 就是从传输层抛上来的,与生产同形。
   (锁必须打在接线上:光测 provider_circuit_breaker 这个模块本身,
    证明不了闸真的挂在了发 HTTP 之前。)
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tools.search import provider_circuit_breaker as cb
from tools.search.provider_circuit_breaker import (
    CONSECUTIVE_UNDELIVERED_THRESHOLD,
    COOLDOWN_SECONDS,
    PROVIDER_DOUBAO,
    PROVIDER_METASO,
    _ALERT_RULE_KEY,
)

# DDL 与 scripts/migration_ai_ops_center_2026_07_01.sql:212 逐字一致(去掉 ai_ops_tasks 外键,
# 本批不涉及任务面),口径同 tests/flywheel_close_loop/conftest.py。
AI_OPS_ALERTS_DDL = """
CREATE TABLE IF NOT EXISTS ai_ops_alerts (
  id SERIAL PRIMARY KEY,
  rule_key TEXT NOT NULL,
  fingerprint TEXT NOT NULL DEFAULT '',
  severity TEXT NOT NULL DEFAULT 'warn' CHECK (severity IN ('info', 'warn', 'critical')),
  title TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'firing' CHECK (status IN ('firing', 'resolved')),
  task_id INTEGER NULL,
  payload JSONB NOT NULL DEFAULT '{}'::jsonb,
  first_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
  last_seen_at TIMESTAMP NOT NULL DEFAULT NOW(),
  resolved_at TIMESTAMP NULL,
  resolved_by INTEGER NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_ops_alerts_firing
  ON ai_ops_alerts (rule_key, fingerprint) WHERE status = 'firing';
"""

# DDL 与 db/monitoring_db.py:1993 逐字一致。
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
def breaker_db(monkeypatch):
    """真库 + 干净熔断状态。每个用例前后都清,避免跨用例污染。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(AI_OPS_ALERTS_DDL)
        cur.execute(LLM_CALL_LOG_DDL)
        cur.execute("DELETE FROM ai_ops_alerts")
        cur.execute("DELETE FROM llm_call_log")
    cb.reset_provider_breakers()
    # [WO R2 2026-08-09 ①d] 原来是 setenv,而 METASO_MCP_CONFIG 当时在 import
    #   时就把 env 读死了 → 那一行是**死代码**,用例其实靠 .env 里恰好有 key
    #   才绿。②件已把 api_key 改成现取,setenv 现在也有效;但判据仍改打
    #   **官方接缝**(setitem)—— 它不依赖"env 什么时候被读"这个时序假设。
    import tools.search.metaso_mcp as _mm
    monkeypatch.setitem(_mm.METASO_MCP_CONFIG, "api_key", "test-key")
    monkeypatch.setenv("DOUBAO_SEARCH_API_KEY", "test-key")
    # 重试退避不在判据范围内(秘塔 3s、豆包 _RETRY_BACKOFF_S),但会把整套锁拖到
    # 100s+,变异 runner 要跑十几遍就不可用了。只压时间,不改任何重试**次数**
    # —— 计费笔数由次数决定,不由睡多久决定。
    _real_sleep = asyncio.sleep

    async def _no_wait(_delay, *a, **k):
        return await _real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _no_wait)
    yield
    cb.reset_provider_breakers()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM ai_ops_alerts")
        cur.execute("DELETE FROM llm_call_log")


def _billing_rows(platform: str) -> int:
    """`llm_call_log` 里该供应商的**真实**计费行数 —— 这就是"花了几笔钱"。"""
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT count(*) AS n FROM llm_call_log WHERE platform = %s", (platform,))
        return int(cur.fetchone()["n"])


def _firing_alerts(fingerprint: str) -> list:
    from db.connection import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT rule_key, fingerprint, severity, status, payload FROM ai_ops_alerts "
            "WHERE rule_key = %s AND fingerprint = %s",
            (_ALERT_RULE_KEY, fingerprint),
        )
        return [dict(r) for r in cur.fetchall()]


# ── 传输层替身:保留真 httpx.AsyncClient,只换 transport ────────────────────

def _install_transport(monkeypatch, module, handler):
    """把 module 里的 `httpx.AsyncClient` 换成挂了 MockTransport 的真 client。"""
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_client(transport=httpx.MockTransport(handler), timeout=5.0)

    monkeypatch.setattr(module.httpx, "AsyncClient", _factory)


def _connect_error_handler(request):
    raise httpx.ConnectError("connection refused", request=request)


def _metaso_ok_handler(request):
    body = {"content": [{"type": "text", "text": json.dumps(
        {"credits": 1, "total": 1,
         "webpages": [{"title": "t", "link": "https://e.com/a", "snippet": "s"}]}
    )}]}
    return httpx.Response(200, json=body)


def _doubao_ok_handler(request):
    return httpx.Response(200, json={"Result": {"WebResults": [
        {"Url": "https://e.com/a", "Title": "t", "Content": "c"}
    ]}})


# ══════════════════════════════════════════════════════════════════════════
# §1 熔断:连续 N 次未送达之后,**不再产生计费行**
# ══════════════════════════════════════════════════════════════════════════

def test_metaso_stops_billing_after_threshold(breaker_db, monkeypatch):
    """【必须命中 · 打在钱上】秘塔连续未送达到阈值后,`llm_call_log` **不再新增行**。"""
    import tools.search.metaso_mcp as mm

    _install_transport(monkeypatch, mm, _connect_error_handler)

    async def _drive(n):
        for _ in range(n):
            await mm._metaso_web_search_direct("q", size=5)

    asyncio.run(_drive(40))
    rows = _billing_rows("metaso")
    # 阈值 5、每次逻辑调用重试 2 次 → 最多在第 3 次逻辑调用中途跳闸。
    # 关键不是精确值,而是**远小于 40×2=80**,且之后彻底停住。
    assert rows <= CONSECUTIVE_UNDELIVERED_THRESHOLD + 2, (
        f"跳闸后仍在计费:{rows} 笔(阈值 {CONSECUTIVE_UNDELIVERED_THRESHOLD})"
    )

    before = _billing_rows("metaso")
    asyncio.run(_drive(30))
    assert _billing_rows("metaso") == before, "跳闸之后再打 30 次,一笔都不许多"


def test_without_the_breaker_every_call_would_bill(breaker_db, monkeypatch):
    """【反向对照】把闸拆掉(should_skip 恒 False)→ 每次调用都计费。

    没有这条,上面那条会被"根本没发生调用"这种错误实现拿满分 ——
    它证明的是:计费行数少下来是**闸**干的,不是链路本来就不调。
    """
    import tools.search.metaso_mcp as mm

    _install_transport(monkeypatch, mm, _connect_error_handler)
    monkeypatch.setattr(cb, "should_skip", lambda provider: (False, None))

    async def _drive(n):
        for _ in range(n):
            await mm._metaso_web_search_direct("q", size=5)

    asyncio.run(_drive(10))
    assert _billing_rows("metaso") == 20, (
        "无闸时 10 次逻辑调用 × 2 次重试 = 20 笔 —— 这就是 454 笔的成因形状"
    )


def test_doubao_stops_billing_after_threshold(breaker_db, monkeypatch):
    """【必须命中 · 打在钱上】豆包侧同样收敛。"""
    import tools.search.doubao_search as ds

    _install_transport(monkeypatch, ds, _connect_error_handler)

    async def _drive(n):
        for _ in range(n):
            try:
                await ds.doubao_web_search_raw("q", count=5)
            except ds.DoubaoSearchError:
                pass

    asyncio.run(_drive(40))
    rows = _billing_rows("doubao_search")
    assert rows <= CONSECUTIVE_UNDELIVERED_THRESHOLD + 2, f"跳闸后仍在计费:{rows} 笔"


def test_breaker_is_per_provider(breaker_db, monkeypatch):
    """【必须不命中】秘塔跳闸不许把豆包一起停掉(工单明写"按供应商")。"""
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")
    assert cb.should_skip(PROVIDER_METASO)[0] is True
    assert cb.should_skip(PROVIDER_DOUBAO)[0] is False, "另一个供应商必须照常可用"


def test_business_failure_does_not_trip_the_breaker(breaker_db, monkeypatch):
    """【必须不命中】HTTP 打通了但业务失败(500 / errCode / 空结果)**不算**未送达。

    那类失败是送达的、该计费的,也该继续调(可能下一条 query 就有结果)。
    拿它跳闸 = 用错判据停供应商。
    """
    import tools.search.metaso_mcp as mm

    def _http_500(request):
        return httpx.Response(500, text="boom")

    _install_transport(monkeypatch, mm, _http_500)

    async def _drive(n):
        for _ in range(n):
            await mm._metaso_web_search_direct("q", size=5)

    asyncio.run(_drive(20))
    assert cb.should_skip(PROVIDER_METASO)[0] is False, "业务级失败不许跳闸"
    assert _firing_alerts(PROVIDER_METASO) == [], "业务级失败不许拉本熔断的告警"


def test_read_timeout_is_not_counted_as_undelivered(breaker_db, monkeypatch):
    """【必须不命中】`ReadTimeout` = 请求**已经发出去了**,只是没等到回应 ——
    钱可能已经花了,它不属于「确定未送达」,不许进熔断计数。

    🔴 这条是 `image_client._connect_error_types()` 那份"唯一的没送出去集合"的
    边界本身(它只收 ConnectError/ConnectTimeout/ProxyError)。
    用 HTTP 500 隔离不了这一条 —— 500 在秘塔侧根本不抛异常、进不到 except
    (变异 M08/M16 第一版因此存活,是我的用例打偏了,不是实现稳)。
    """
    import tools.search.metaso_mcp as mm

    def _read_timeout(request):
        raise httpx.ReadTimeout("no response", request=request)

    _install_transport(monkeypatch, mm, _read_timeout)

    async def _drive(n):
        for _ in range(n):
            await mm._metaso_web_search_direct("q", size=5)

    asyncio.run(_drive(20))
    assert cb.should_skip(PROVIDER_METASO)[0] is False, (
        "ReadTimeout 不是「确定未送达」,不许拿它跳闸"
    )
    assert _firing_alerts(PROVIDER_METASO) == [], "更不许为它拉告警"


def test_connect_error_is_counted_as_undelivered(breaker_db, monkeypatch):
    """【必须命中 · 与上一条成对】ConnectError 就是要跳闸。

    只有上一条会让"永远不记未送达"这种实现拿满分;成对才有判别力。
    """
    import tools.search.metaso_mcp as mm

    _install_transport(monkeypatch, mm, _connect_error_handler)

    async def _drive(n):
        for _ in range(n):
            await mm._metaso_web_search_direct("q", size=5)

    asyncio.run(_drive(20))
    assert cb.should_skip(PROVIDER_METASO)[0] is True


def test_success_closes_the_breaker(breaker_db, monkeypatch):
    """【必须命中】送达一次就清零 —— 抖动一下不该把供应商停到进程结束。"""
    import tools.search.metaso_mcp as mm

    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD - 1):
        cb.record_undelivered(PROVIDER_METASO, "boom")
    assert cb.should_skip(PROVIDER_METASO)[0] is False, "差一次不许跳"

    _install_transport(monkeypatch, mm, _metaso_ok_handler)
    asyncio.run(mm._metaso_web_search_direct("q", size=5))
    assert cb.should_skip(PROVIDER_METASO)[0] is False
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD - 1):
        cb.record_undelivered(PROVIDER_METASO, "boom")
    assert cb.should_skip(PROVIDER_METASO)[0] is False, "成功后计数必须真的从 0 重新数"


def test_cooldown_lets_exactly_one_probe_through(breaker_db, monkeypatch):
    """【必须命中】静默期满后**只**放一个探针,并发不许一次漏一批。"""
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")
    assert cb.should_skip(PROVIDER_METASO)[0] is True

    # 把开闸时刻往前拨过静默期(不 sleep 300s)
    with cb._lock:
        cb._state[PROVIDER_METASO]["opened_at"] -= (COOLDOWN_SECONDS + 1)

    first = cb.should_skip(PROVIDER_METASO)
    others = [cb.should_skip(PROVIDER_METASO) for _ in range(9)]
    assert first[0] is False, "静默期满,第一个必须放行"
    assert all(s[0] is True for s in others), "其余并发必须仍被拦(否则一批漏 10 笔)"


def test_failed_probe_reopens_the_cooldown(breaker_db):
    """【必须不命中】探针又挂了 → 立刻重新开闸,不许变成不限速重试。"""
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")
    with cb._lock:
        cb._state[PROVIDER_METASO]["opened_at"] -= (COOLDOWN_SECONDS + 1)
    assert cb.should_skip(PROVIDER_METASO)[0] is False  # 探针放行
    cb.record_undelivered(PROVIDER_METASO, "boom again")
    assert cb.should_skip(PROVIDER_METASO)[0] is True, "探针失败必须重新静默"


def test_rate_arm_is_unreachable(breaker_db):
    """【必须不命中】复用的 `CircuitBreaker` 失败率那条臂**不许**参与跳闸。

    业务级失败(空结果/余额不足)会进它的分母分子,让它跳闸 = 用错判据停供应商。
    """
    counter = cb._new_counter()
    # 🔴 失败率必须**明确超过** rate_threshold(0.5),否则 `rate > threshold` 恒假,
    #    这条反向对照就成了空操作(第一版我写 1:1 交替 = 恰好 0.5,变异 M07 因此存活)。
    #    2 失败 1 成功 → 失败率 2/3;consecutive 峰值 2 < 阈值 5,不会从连续那条臂跳。
    for _ in range(10_000):
        counter.record_failure()
        counter.record_failure()
        counter.record_success()
    assert counter.failure_count / (counter.failure_count + counter.success_count) > 0.5
    assert counter.consecutive_failures < CONSECUTIVE_UNDELIVERED_THRESHOLD
    tripped, reason = counter.is_tripped()
    assert tripped is False and reason is None, (
        f"失败率 67% 跑三万次都不许跳闸(业务级失败不是停供应商的判据),实际 {reason}"
    )


# ══════════════════════════════════════════════════════════════════════════
# §2 告警 —— 判据打在 ai_ops_alerts 真表行上
# ══════════════════════════════════════════════════════════════════════════

def test_alert_row_lands_in_ai_ops_alerts(breaker_db):
    """【必须命中】跳闸落一条 firing 告警,fingerprint = 供应商。"""
    for i in range(CONSECUTIVE_UNDELIVERED_THRESHOLD - 1):
        cb.record_undelivered(PROVIDER_METASO, "connect refused")
    assert _firing_alerts(PROVIDER_METASO) == [], "未到阈值不许告警"

    cb.record_undelivered(PROVIDER_METASO, "connect refused")
    rows = _firing_alerts(PROVIDER_METASO)
    assert len(rows) == 1, "跳闸必须落一条告警行"
    assert rows[0]["status"] == "firing"
    assert rows[0]["severity"] == "critical"
    assert rows[0]["payload"]["provider"] == PROVIDER_METASO
    assert rows[0]["payload"]["consecutive"] == CONSECUTIVE_UNDELIVERED_THRESHOLD


def test_alert_is_deduped_not_spammed(breaker_db):
    """【必须不命中】继续失败不许刷屏 —— 仍然只有一行。"""
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD + 50):
        cb.record_undelivered(PROVIDER_METASO, "boom")
    assert len(_firing_alerts(PROVIDER_METASO)) == 1


def test_alert_resolves_on_recovery(breaker_db):
    """【必须命中】恢复送达后告警被收掉(开/合闭环)。"""
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")
    assert len(_firing_alerts(PROVIDER_METASO)) == 1
    cb.record_delivered(PROVIDER_METASO)
    rows = _firing_alerts(PROVIDER_METASO)
    assert rows and rows[0]["status"] == "resolved", "恢复后必须收告警"


def test_alert_channel_failure_never_breaks_the_caller(breaker_db, monkeypatch):
    """【必须不命中】告警通道自己挂了,不许把主链带下水(熔断是省钱设施,不是主链依赖)。"""
    import db.ai_ops_db as ai_ops_db

    def _boom(*a, **k):
        raise RuntimeError("alert channel down")

    monkeypatch.setattr(ai_ops_db, "upsert_alert", _boom)
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")   # 不抛就是通过
    assert cb.should_skip(PROVIDER_METASO)[0] is True, "告警挂了,熔断本身照样生效"


# ══════════════════════════════════════════════════════════════════════════
# §3 不影响写作主链:失败仍走「证据缺失允许写短」
# ══════════════════════════════════════════════════════════════════════════

def test_writing_chain_still_produces_a_compact_plan(breaker_db, monkeypatch):
    """【必须命中 · 打在合同分支上】两个供应商全熔断时,篇幅合同走
    `verified_evidence_absent_allow_shorter`,主链**不中断**。
    """
    from writing.article_length_contract import build_article_length_plan

    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")
        cb.record_undelivered(PROVIDER_DOUBAO, "boom")
    assert cb.should_skip(PROVIDER_METASO)[0] is True
    assert cb.should_skip(PROVIDER_DOUBAO)[0] is True

    # 熔断态下证据必然是 0 条 —— 篇幅合同据此走"允许写短",不是报错、不是补白。
    plan = build_article_length_plan("implementation_guide", evidence_pack=None)
    assert "verified_evidence_absent_allow_shorter" in (plan.get("reasons") or []), plan
    # 反向对照:有 8 条已核验证据时**不许**再走这条压缩分支
    # 🔴 fixture 必须是**生产真实形态**:`_verified_provenance_complete` 对
    #    `official_record` 要求 official_record_id + 公网 http url,少一个就不算已核验
    #    (第一版我按想当然写 {"verification_status": "verified"},结果反向对照恒真 ——
    #     "写判据前先读实现"这条又踩了一次,记档)。
    rich = build_article_length_plan("implementation_guide", evidence_pack={
        "items": [
            {
                "evidence_id": f"ev{i}",
                "verification_status": "official_record",
                "official_record_id": f"rec-{i}",
                "url": f"https://e{i}.example.com/a",
                "claim": "某条可核验事实",
                "publisher": f"p{i}",
                "relationship": "support",
            }
            for i in range(8)
        ],
    })
    assert "verified_evidence_absent_allow_shorter" not in (rich.get("reasons") or []), rich


def test_evidence_pack_does_not_raise_when_both_providers_are_open(breaker_db, monkeypatch):
    """【必须命中 · 打在接线上】熔断状态下 `collect_evidence_pack` 仍**正常返回**,不抛。

    抛了就会被 `article_generator_service` 的 except 兜成 `failed_closed`,
    那是另一种状态;工单要的是"照旧走证据缺失分支"。
    """
    import tools.search.metaso_mcp as mm
    import tools.search.doubao_search as ds
    from writing.evidence_research import collect_evidence_pack

    _install_transport(monkeypatch, mm, _connect_error_handler)
    _install_transport(monkeypatch, ds, _connect_error_handler)
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")
        cb.record_undelivered(PROVIDER_DOUBAO, "boom")

    pack = asyncio.run(collect_evidence_pack(
        title="测试标题", keyword="测试词", industry="测试行业",
        client_brand="某品牌", force=True,
    ))
    assert isinstance(pack, dict), "必须正常返回一个 pack,不许抛"
    assert not (pack.get("items") or []), "熔断期间不该凭空产出证据"


def test_open_breaker_costs_nothing_in_the_writing_chain(breaker_db, monkeypatch):
    """【必须命中 · 打在钱上】熔断态下跑一整篇的证据检索,`llm_call_log` 零新增。"""
    import tools.search.metaso_mcp as mm
    import tools.search.doubao_search as ds
    from writing.evidence_research import collect_evidence_pack

    _install_transport(monkeypatch, mm, _connect_error_handler)
    _install_transport(monkeypatch, ds, _connect_error_handler)
    for _ in range(CONSECUTIVE_UNDELIVERED_THRESHOLD):
        cb.record_undelivered(PROVIDER_METASO, "boom")
        cb.record_undelivered(PROVIDER_DOUBAO, "boom")

    from db.connection import get_db
    with get_db() as conn:
        conn.cursor().execute("DELETE FROM llm_call_log")

    asyncio.run(collect_evidence_pack(
        title="测试标题", keyword="测试词", industry="测试行业",
        client_brand="某品牌", force=True,
    ))
    assert _billing_rows("metaso") == 0, "熔断态下秘塔不许再产生任何一笔"
    assert _billing_rows("doubao_search") == 0, "熔断态下豆包不许再产生任何一笔"


def test_threshold_value_is_pinned(breaker_db):
    """【必须命中】阈值钉死 —— 改它要走工单,不是随手调参(同 metaso_health 惯例)。"""
    assert CONSECUTIVE_UNDELIVERED_THRESHOLD == 5
