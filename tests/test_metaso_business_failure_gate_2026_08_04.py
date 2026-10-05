"""秘塔业务级失败闸 · 止血包验收测试(2026-08-04)

背景:秘塔余额不足走 HTTP 200 通道返回 {"errCode":3000,"errMsg":"余额不足"},
原判据只看 status → 记假绿 success=True + failover 不触发 + 上层守卫判不出
→ effective_competition 静默落地板值 1 → 报价按「零竞争」出。

本文件的每一条正向断言都配了反向对照,防止判据自己恒真:
  - errCode 失败 → 必须兜底/告警   ←→  正常空结果 → 必须放行/不告警
  - 断血 → tracker 必须 success=False ←→ 正常 → 必须 success=True 且返回值逐字不变
"""

import asyncio
import json

import pytest

from tools import metaso_health
from tools.metaso_health import (
    METASO_CONSECUTIVE_ALERT_THRESHOLD,
    metaso_result_error,
    record_metaso_result,
    reset_metaso_health_state,
)

# 秘塔断血的真实响应体(2026-08-04 生产容器内实测原样抄录)
ERRCODE_BODY = {"errCode": 3000, "errMsg": "余额不足"}

# 正常响应的信封(2026-08-04 修复后实测形状)
def _normal_body(n_pages=2):
    return {
        "credits": 3,
        "searchParameters": {"q": "贵阳酸汤火锅", "scope": "webpage", "size": 100},
        "total": n_pages,
        "webpages": [
            {"title": f"t{i}", "link": f"https://example{i}.com/a", "snippet": "s"}
            for i in range(n_pages)
        ],
    }


# ============================================================
# 1. 判据真值表(含反向对照)
# ============================================================

@pytest.mark.parametrize("body,should_fail,why", [
    (ERRCODE_BODY, True, "余额不足 errCode=3000 走 200 通道 —— 本 bug 的真身"),
    ({"errCode": "3000", "errMsg": "余额不足"}, True, "errCode 是字符串也要判失败"),
    ({"errCode": "not-a-number"}, True, "非数字 errCode 宁可兜底不可假绿"),
    ({"error": "Status 500: boom"}, True, "上游已包成 error 键"),
    ({"credits": 3, "total": 0}, True, "缺 webpages 键 = 不是搜索结果形状"),
    ({"webpages": []}, True, "webpages 空且信封全无"),
    (None, True, "空响应"),
    ("not a dict", True, "响应不是 dict"),
    # ---- 反向对照:下面这些必须放行,判据不能恒真 ----
    (_normal_body(2), False, "正常有结果"),
    ({"errCode": 0, **_normal_body(1)}, False, "errCode=0 是成功码"),
    ({"errCode": "0", **_normal_body(1)}, False, "字符串 '0' 也是成功码"),
    ({"credits": 3, "searchParameters": {}, "total": 0, "webpages": []},
     False, "★正常空结果:冷门词真·零结果,合法业务事实,不判失败不告警"),
    ({"credits": 3, "webpages": []}, False, "只要有信封字段,空结果就合法"),
])
def test_metaso_result_error_truth_table(body, should_fail, why):
    err = metaso_result_error(body)
    assert (err is not None) is should_fail, f"{why} · 实际 err={err!r}"


def test_truth_table_has_both_polarities():
    """判据自检:真值表必须两极都覆盖,否则断言可能恒真/恒假。"""
    bodies = [ERRCODE_BODY, _normal_body(2)]
    verdicts = {metaso_result_error(b) is not None for b in bodies}
    assert verdicts == {True, False}, "判据没有判别力(两极没都出现)"


# ============================================================
# 2. 连续失败告警:够 N 次才响 · 只响一次 · 恢复再响一次
# ============================================================

@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    reset_metaso_health_state()
    fired = {"raise": 0, "resolve": 0, "details": []}
    monkeypatch.setattr(
        metaso_health, "_fire_alert",
        lambda n, reason, source: (fired.__setitem__("raise", fired["raise"] + 1),
                                   fired["details"].append((n, reason, source))),
    )
    monkeypatch.setattr(
        metaso_health, "_resolve_alert",
        lambda: fired.__setitem__("resolve", fired["resolve"] + 1),
    )
    yield fired
    reset_metaso_health_state()


def test_threshold_value_is_pinned():
    """钉死阈值数值本身。

    下面所有告警用例都写成 range(THRESHOLD-1),是**参数化在常量上**的 ——
    常量一改,用例跟着改,于是「阈值被人从 20 改成 1」这类改动一个都抓不到
    (变异自检 A5 当场证明了这一点)。所以数值必须单独钉一条。
    20 = 工单 2026-08-04 指定值,改它要走工单,不是随手调参。
    """
    assert METASO_CONSECUTIVE_ALERT_THRESHOLD == 20


def test_alert_fires_only_at_threshold(_clean_state):
    for i in range(METASO_CONSECUTIVE_ALERT_THRESHOLD - 1):
        assert record_metaso_result(False, "余额不足") is None, f"第 {i+1} 次不该告警"
    assert _clean_state["raise"] == 0, "未达阈值不许告警"

    assert record_metaso_result(False, "余额不足") == "raise", "第 N 次必须告警"
    assert _clean_state["raise"] == 1


def test_alert_deduped_no_spam(_clean_state):
    for _ in range(METASO_CONSECUTIVE_ALERT_THRESHOLD + 200):
        record_metaso_result(False, "余额不足")
    assert _clean_state["raise"] == 1, "告警必须去重,220 次失败只能响 1 次"


def test_recovery_alerts_once_then_silent(_clean_state):
    for _ in range(METASO_CONSECUTIVE_ALERT_THRESHOLD):
        record_metaso_result(False, "余额不足")
    assert _clean_state["raise"] == 1

    assert record_metaso_result(True) == "resolve", "恢复必须告一次"
    assert _clean_state["resolve"] == 1
    for _ in range(50):
        assert record_metaso_result(True) is None
    assert _clean_state["resolve"] == 1, "恢复告警也要去重"


def test_success_resets_consecutive_counter(_clean_state):
    """★反向对照:间歇性失败(中间夹成功)不该攒够阈值 —— 防抖动刷屏。"""
    for _ in range(10):
        for _ in range(METASO_CONSECUTIVE_ALERT_THRESHOLD - 1):
            record_metaso_result(False, "抖动")
        record_metaso_result(True)
    assert _clean_state["raise"] == 0, "连续计数必须被成功清零"


def test_normal_empty_results_never_alert(_clean_state):
    """★验收要求的反向对照:正常空结果连续 100 次也不许告警。"""
    empty_ok = {"credits": 3, "searchParameters": {}, "total": 0, "webpages": []}
    for _ in range(100):
        err = metaso_result_error(empty_ok)
        record_metaso_result(err is None, str(err or ""))
    assert _clean_state["raise"] == 0, "正常空结果不是失败,不许告警"


# ============================================================
# 3. search_metaso:200+errCode 必须转失败 + 触发 failover + 记 success=False
# ============================================================

class _FakeResp:
    def __init__(self, status, payload=None, text=""):
        self.status = status
        self._payload = payload
        self._text = text

    async def json(self):
        return self._payload

    async def text(self):
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, resp_factory):
        self._resp_factory = resp_factory

    def post(self, *a, **k):
        return self._resp_factory()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeTracker:
    def __init__(self, sink):
        self.sink = sink

    def record(self, success=None, error_msg=None, **kw):
        self.sink.append({"success": success, "error_msg": error_msg})


class _FakeTrack:
    def __init__(self, sink):
        self.sink = sink

    def __call__(self, *a, **k):
        return self

    async def __aenter__(self):
        return _FakeTracker(self.sink)

    async def __aexit__(self, *a):
        return False


def _patch_metaso(monkeypatch, body, status=200, n_keys=2):
    """把 competition_analyzer 的网络层与 key 池换成可控假件。"""
    import tools.api_source_pool as pool_mod
    import tools.competition_analyzer as ca

    calls = {"n": 0}
    records = []

    def _factory():
        calls["n"] += 1
        return _FakeResp(status, body, text=json.dumps(body or {}))

    monkeypatch.setattr(ca.aiohttp, "ClientSession", lambda *a, **k: _FakeSession(_factory))
    monkeypatch.setattr(ca, "llm_track", _FakeTrack(records))
    real_pool = pool_mod.SourcePool(
        name="metaso-test", keys=[f"k{i}" for i in range(n_keys)], qpm_per_source=10_000
    )
    monkeypatch.setattr(pool_mod, "get_metaso_pool", lambda: real_pool)
    return calls, records


async def test_errcode_body_becomes_error_and_exhausts_failover(monkeypatch, _clean_state):
    import tools.competition_analyzer as ca
    calls, records = _patch_metaso(monkeypatch, ERRCODE_BODY, n_keys=2)

    result = await ca.search_metaso("贵阳酸汤火锅哪家味道最地道", size=100)

    assert "error" in result, "断血必须被包成 error 键,而不是把 errCode body 交给上层"
    assert "3000" in str(result["error"]), f"错误原因要能看出是 errCode:{result}"
    assert calls["n"] == 2, "★两个账号都要试过 —— 证明 failover 被重新激活(旧行为只试 1 次就当成功返回)"
    assert records and all(r["success"] is False for r in records), \
        "★断血不许再记 success=True"


async def test_normal_body_unchanged_and_success_true(monkeypatch, _clean_state):
    """★逐字不变对照:秘塔正常时返回值与 tracker 行为必须和修复前一致。"""
    import tools.competition_analyzer as ca
    body = _normal_body(3)
    calls, records = _patch_metaso(monkeypatch, body, n_keys=2)

    result = await ca.search_metaso("成都火锅", size=100)

    assert result == body, "正常响应必须原样返回,一个字节都不许改"
    assert calls["n"] == 1, "正常时只调 1 次(不该触发 failover)"
    assert records == [{"success": True, "error_msg": None}], "正常时必须记 success=True"


async def test_http_500_still_failovers(monkeypatch, _clean_state):
    """回归护栏:原本就该 failover 的 HTTP 失败路径不许被我改坏。"""
    import tools.competition_analyzer as ca
    calls, records = _patch_metaso(monkeypatch, {"m": "boom"}, status=500, n_keys=2)

    result = await ca.search_metaso("x", size=100)

    assert "error" in result
    assert calls["n"] == 2
    assert all(r["success"] is False for r in records)


# ============================================================
# 4. 守卫 ②:兜底必须接管 · 正常时不许接管
# ============================================================

async def _run_search_one(monkeypatch, fake_result, keyword="贵阳酸汤火锅哪家味道最地道"):
    import tools.competition_analyzer as ca
    import tools.keyword_value_scorer as kvs

    async def _fake_search_metaso(kw, size=100):
        return fake_result

    monkeypatch.setattr(ca, "search_metaso", _fake_search_metaso)

    async def _fake_batch_classify(keyword_distilled):
        return {k: v for k, v in keyword_distilled.items()}

    monkeypatch.setattr(ca, "batch_classify_results_with_llm", _fake_batch_classify)
    return await kvs.fetch_metaso_batch([keyword], concurrency=2)


async def test_guard_falls_back_on_errcode_body(monkeypatch, _clean_state):
    kw = "贵阳酸汤火锅哪家味道最地道"
    out = await _run_search_one(monkeypatch, ERRCODE_BODY, kw)

    from tools.keyword_value_scorer import estimate_competition_from_keyword
    expected = estimate_competition_from_keyword(kw)

    assert out[kw]["source"] == "fallback", "★errCode 响应必须走兜底,不许静默算成零竞争"
    assert out[kw]["effective_competition"] == expected, \
        "兜底值必须来自 estimate_competition_from_keyword"
    assert out[kw]["effective_competition"] > 1, \
        "★本 bug 的核心:兜底后不许再是地板值 1"
    assert "3000" in out[kw].get("fallback_reason", ""), "必须留痕原因"


async def test_guard_does_not_fall_back_on_normal_empty(monkeypatch, _clean_state):
    """★反向对照:信封完整的零结果是合法业务事实,不许被兜底顶掉。"""
    kw = "某个没人搜的冷门词"
    empty_ok = {"credits": 3, "searchParameters": {}, "total": 0, "webpages": []}
    out = await _run_search_one(monkeypatch, empty_ok, kw)

    # 成功路径根本不写 source 键 —— 它的缺席本身就是「没走兜底」的证据
    assert out[kw].get("source") != "fallback", "正常空结果不该走兜底"
    assert "fallback_reason" not in out[kw], "正常空结果不该留失败痕"
    assert out[kw]["effective_competition"] == 1, "真·零竞争就应该是 1"


async def test_guard_normal_body_unchanged(monkeypatch, _clean_state):
    kw = "成都火锅"
    out = await _run_search_one(monkeypatch, _normal_body(4), kw)

    assert out[kw].get("source") != "fallback", "正常响应不许走兜底"
    assert out[kw]["content_count"] == 4, "正常路径的统计口径不许变"


# ============================================================
# 5. metaso_search(ToolResponse 出口 · 工单点名的 :113/:117)
# ============================================================

class _FakeHttpxResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class _FakeHttpxClient:
    def __init__(self, resp):
        self._resp = resp

    async def post(self, *a, **k):
        return self._resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


async def _run_metaso_search(monkeypatch, payload, status=200):
    # tools/search/__init__.py 用同名**函数** metaso_search 遮蔽了同名子**模块**,
    # `import tools.search.metaso_search as ms` 拿到的是函数 → 只能从 sys.modules 取模块。
    import sys
    import tools.search.metaso_search  # noqa: F401  (触发子模块注册)
    import tools.llm_call_tracker as tracker_mod

    ms = sys.modules["tools.search.metaso_search"]
    records = []
    monkeypatch.setattr(ms.httpx, "AsyncClient",
                        lambda *a, **k: _FakeHttpxClient(_FakeHttpxResp(status, payload)))
    monkeypatch.setattr(tracker_mod, "llm_track", _FakeTrack(records))
    resp = await ms.metaso_search("贵阳酸汤火锅", size=20)
    return json.loads(json.dumps(resp.content[0]["text"])), records


async def test_metaso_search_errcode_returns_error(monkeypatch, _clean_state):
    text, records = await _run_metaso_search(monkeypatch, ERRCODE_BODY)
    assert text.startswith("Error:"), f"★断血必须返回 Error,不许把 errCode body 当结果:{text[:120]}"
    assert "3000" in text
    assert records and records[-1]["success"] is False, "★断血不许记 success=True"


async def test_metaso_search_normal_unchanged(monkeypatch, _clean_state):
    body = _normal_body(2)
    text, records = await _run_metaso_search(monkeypatch, body)
    assert json.loads(text) == body, "正常时输出必须逐字不变"
    assert records and records[-1]["success"] is True
