"""生图链三跳韧性锁(返工单 REWORK_ORDER_IMAGE_RETRY_2026-08-02)

背景:出海梯子握手成功率生产实测在 42%~75% 之间飘,而三跳原本**全都零重试**。
一条图文 N 张卡,全成功率 = p^N;按 42% 算 6 张卡只有 0.5%。

🔴🔴 本文件最重要的一条不是"重试要生效",而是 **"哪些绝不许重试"**:
    `submit_image` 的 docstring 把口径写死了 ——
    "Connect failures are the only safe no-send class."
    read timeout / 已建连后失败 / 收到任何 HTTP 响应 → provider 可能已受理,
    重发 = **第二次付费**。所以每条"会重试"的锁,都配一条"绝不重试"的反向对照。

三跳各自的性质不同,重试类也不同:
  submit   有计费风险 → 只重试 connect 类
  poll     已经计费了 → 瞬时错必须扛住(丢一拍继续),但绝不重新提交
  download 免费幂等   → connect + read 都可重试
"""
from __future__ import annotations

import asyncio
import io

import pytest

from services.marketing import image_client as ic


# ─────────────────────────────────────────────────────────────
# 测试脚手架
# ─────────────────────────────────────────────────────────────
class _FakeTracker:
    def __init__(self):
        self.records = []

    def record(self, **kw):
        self.records.append(kw)


class _FakeTrack:
    """替掉 llm_track 的异步上下文管理器(不打 DB)。"""

    def __init__(self, *a, **kw):
        self.tracker = _FakeTracker()

    async def __aenter__(self):
        return self.tracker

    async def __aexit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _no_real_sleep_and_no_db(monkeypatch):
    """退避不真等(否则一条用例要跑二十几秒),并把 llm_track 换成假的。"""
    async def fast_sleep(_s):
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(ic, "_get_key", lambda: "test-key")

    import tools.llm_call_tracker as tracker_mod
    monkeypatch.setattr(tracker_mod, "llm_track", _FakeTrack)


def _run(coro):
    return asyncio.run(coro)


def _tiny_png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 120, 40)).save(buf, "PNG")
    return buf.getvalue()


# ═════════════════════════════════════════════════════════════
# 一、submit 跳(计费红线所在)
# ═════════════════════════════════════════════════════════════
def _count_submit(monkeypatch, side_effects):
    """把 _submit 换成按脚本发作的假件,返回调用计数器。

    side_effects 里每项要么是异常实例(抛),要么是返回值。
    """
    calls = {"n": 0}

    async def fake_submit(*_a, **_kw):
        i = calls["n"]
        calls["n"] += 1
        eff = side_effects[min(i, len(side_effects) - 1)]
        if isinstance(eff, BaseException):
            raise eff
        return eff

    monkeypatch.setattr(ic, "_submit", fake_submit)
    return calls


def test_connect_error_four_times_then_success(monkeypatch):
    """前 4 次 ConnectError、第 5 次成功 → ok=True,且 _submit 恰好被调用 5 次。"""
    import httpx
    calls = _count_submit(monkeypatch, [httpx.ConnectError("x")] * 4 + ["task-1"])
    monkeypatch.setattr(ic, "_poll", lambda _t: _ok_url())

    out = _run(ic.generate_image("p"))
    assert out["ok"] is True, out
    assert calls["n"] == 5, f"_submit 调用次数应为 5,实际 {calls['n']}"


async def _ok_url():
    return "https://cdn.apimart.ai/x.png"


def test_single_success_does_not_retry(monkeypatch):
    """🔴 反向对照:一次就成功时 _submit 只能被调用 1 次。

    没有这条,"无条件重试 N 次"也能让上面那条通过 —— 那是重复付费。
    """
    calls = _count_submit(monkeypatch, ["task-1"])
    monkeypatch.setattr(ic, "_poll", lambda _t: _ok_url())

    out = _run(ic.generate_image("p"))
    assert out["ok"] is True
    assert calls["n"] == 1, f"没失败却重试了 {calls['n']} 次"


def test_read_timeout_is_never_retried(monkeypatch):
    """🔴🔴 计费红线:ReadTimeout = outcome-unknown,provider 可能已受理。

    重发 = 第二次付费。所以 _submit **只能**被调用 1 次。
    """
    import httpx
    calls = _count_submit(monkeypatch, [httpx.ReadTimeout("x")])

    out = _run(ic.generate_image("p"))
    assert out["ok"] is False
    assert calls["n"] == 1, f"ReadTimeout 被重试了 {calls['n']} 次 = 重复付费"
    assert "ReadTimeout" in out["error"]


def test_protocol_error_is_never_retried(monkeypatch):
    """同上:已建连后的协议错也属 outcome-unknown。"""
    import httpx
    calls = _count_submit(monkeypatch, [httpx.RemoteProtocolError("x")])

    out = _run(ic.generate_image("p"))
    assert out["ok"] is False
    assert calls["n"] == 1, f"RemoteProtocolError 被重试了 {calls['n']} 次"


def test_submit_failed_response_is_never_retried(monkeypatch):
    """🔴 §6.3 澄清:拿到 HTTP 响应(非 200)= 请求已送达,不属"连接失败"。

    `_submit` 此时返回 None。若把 None 也当成可重试,就是把"响应错误"混进
    "连接失败" —— 同样是重复付费。
    """
    calls = _count_submit(monkeypatch, [None])

    out = _run(ic.generate_image("p"))
    assert out["ok"] is False
    assert out["error"] == "submit_failed"
    assert calls["n"] == 1, f"submit_failed 被重试了 {calls['n']} 次"


def test_all_connect_attempts_failed_reports_type_and_proxy(monkeypatch):
    """全失败 → ok=False,且 error 串里必须同时含异常类型与代理主机。

    httpx.ConnectError 的 str() 天生是空串;只打 str(e) 等于什么都没说
    (生产实测就是这么"失声"的)。
    """
    import httpx
    monkeypatch.setenv("JINA_HTTP_PROXY", "socks5h://203.0.113.10:8022")
    monkeypatch.delenv("APIMART_HTTP_PROXY", raising=False)
    calls = _count_submit(monkeypatch, [httpx.ConnectError("")])

    out = _run(ic.generate_image("p"))
    assert out["ok"] is False
    assert calls["n"] == ic.connect_retries(), "重试次数与配置不一致"
    assert "ConnectError" in out["error"], f"error 里没有异常类型:{out['error']}"
    assert "203.0.113.10" in out["error"], f"error 里没有代理主机:{out['error']}"
    assert "8022" in out["error"], "error 里没有代理端口"


def test_proxy_credentials_never_leak(monkeypatch):
    """🔴🔴 正向对照:代理带凭据时,error 与日志都**不得**出现 user/pass。

    现役配置形如 `socks5h://user:pass@host:port`,原样打出去就是把凭据
    写进日志和 API 响应。
    """
    import httpx
    monkeypatch.setenv("JINA_HTTP_PROXY",
                       "socks5h://s3cretUser:sup3rSecret@203.0.113.10:8022")
    monkeypatch.delenv("APIMART_HTTP_PROXY", raising=False)
    _count_submit(monkeypatch, [httpx.ConnectError("")])

    out = _run(ic.generate_image("p"))
    blob = out["error"] + ic._proxy_label()
    assert "s3cretUser" not in blob, f"用户名泄漏:{blob}"
    assert "sup3rSecret" not in blob, f"密码泄漏:{blob}"
    # 但可运维信息仍在(证明不是靠"整段不打"来避免泄漏的)
    assert "203.0.113.10" in blob and "socks5h" in blob


def test_proxy_label_shape():
    """代理标识形状:协议://主机:端口;没配代理时是 direct。"""
    import os
    old = os.environ.get("JINA_HTTP_PROXY")
    try:
        os.environ["JINA_HTTP_PROXY"] = "socks5h://u:p@1.2.3.4:9999"
        assert ic._proxy_label() == "socks5h://1.2.3.4:9999"
        os.environ.pop("JINA_HTTP_PROXY")
        os.environ.pop("APIMART_HTTP_PROXY", None)
        assert ic._proxy_label() == "direct"
    finally:
        if old is not None:
            os.environ["JINA_HTTP_PROXY"] = old
        else:
            os.environ.pop("JINA_HTTP_PROXY", None)


# ═════════════════════════════════════════════════════════════
# 二、poll 跳(走到这里已经计费了)
# ═════════════════════════════════════════════════════════════
class _FakePollClient:
    """假 httpx.AsyncClient:按脚本在 get() 上抛异常或返回响应。"""

    script: list = []
    calls = {"n": 0}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, *_a, **_kw):
        i = type(self).calls["n"]
        type(self).calls["n"] += 1
        eff = type(self).script[min(i, len(type(self).script) - 1)]
        if isinstance(eff, BaseException):
            raise eff
        return eff


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _done_payload(url="https://cdn.apimart.ai/x.png"):
    return {"data": {"status": "completed", "result": {"images": [{"url": [url]}]}}}


def test_poll_survives_a_transient_connect_error(monkeypatch):
    """🔴 追加锁 ①:轮询中途一次 ConnectError → 最终 ok=True,且 _submit 仍只 1 次。

    走到轮询时 submit 已成功 = **已计费**。因一次瞬时网络错就放弃,
    等于平台白花钱 + 用户还要退款。
    """
    import httpx
    calls = _count_submit(monkeypatch, ["task-1"])

    _FakePollClient.calls = {"n": 0}
    _FakePollClient.script = [httpx.ConnectError(""), _Resp(200, _done_payload())]
    monkeypatch.setattr(httpx, "AsyncClient", _FakePollClient)

    out = _run(ic.generate_image("p"))
    assert out["ok"] is True, out
    assert calls["n"] == 1, f"轮询抖动导致重新提交了({calls['n']} 次)= 重复付费"
    assert _FakePollClient.calls["n"] == 2, "没有丢一拍后继续轮询"


def test_poll_never_resubmits(monkeypatch):
    """🔴 反向对照:轮询一路失败到超时,也绝不能再发一次 POST。"""
    import httpx
    calls = _count_submit(monkeypatch, ["task-1"])

    _FakePollClient.calls = {"n": 0}
    _FakePollClient.script = [httpx.ConnectError("")]
    monkeypatch.setattr(httpx, "AsyncClient", _FakePollClient)

    out = _run(ic.generate_image("p"))
    assert out["ok"] is False
    assert out["error"] == "poll_failed"
    assert calls["n"] == 1, f"轮询失败后重新提交了 = 重复付费({calls['n']} 次)"
    # 任务号必须留着(已计费任务要能人工捞回)
    assert out.get("provider_task_id") == "task-1"


def test_poll_misses_count_against_the_budget(monkeypatch):
    """丢拍必须计入 _POLL_MAX_WAIT_S 预算 —— 否则网络一直抖就是无限循环。"""
    import httpx
    _count_submit(monkeypatch, ["task-1"])
    _FakePollClient.calls = {"n": 0}
    _FakePollClient.script = [httpx.ConnectError("")]
    monkeypatch.setattr(httpx, "AsyncClient", _FakePollClient)

    _run(ic.generate_image("p"))
    expected = int(ic._POLL_MAX_WAIT_S / ic._POLL_INTERVAL_S)
    assert _FakePollClient.calls["n"] == expected, (
        f"丢拍没计入预算:轮询了 {_FakePollClient.calls['n']} 次,"
        f"按预算应为 {expected} 次")


def test_poll_budget_covers_observed_slow_tasks():
    """🔴 上限必须盖得住实测慢档。

    实测同一 prompt 快的 49s、慢的 >240s。原来的 150s 直接把慢的判死 ——
    A/B 实验 6 张里 2 张(33%)就是这么废的,且丢拍 0 次(纯超时不是网络)。
    """
    assert ic._POLL_MAX_WAIT_S >= 300, (
        f"轮询上限 {ic._POLL_MAX_WAIT_S}s 盖不住实测 240s+ 的慢档")


def test_first_poll_delay_follows_provider_estimate():
    """provider 自己给 estimated_time,就别从第 5 秒起盲问。"""
    assert ic._first_poll_delay(100) == 60.0, "没按 estimated_time 的六成延迟"
    assert ic._first_poll_delay(240) == ic._POLL_FIRST_DELAY_MAX_S, "延迟没有上限"
    # 取六成而不是十成:实测 actual 可能只有 estimated 的一半(49 vs 100)
    assert ic._first_poll_delay(100) < 100, "睡满预估会白等"


def test_first_poll_delay_falls_back_without_estimate():
    """反向面:provider 没给预估时必须退回下限,不能不问或长睡。"""
    for bad in (None, 0, -5, "x"):
        assert ic._first_poll_delay(bad) == ic._POLL_FIRST_DELAY_MIN_S, bad


def test_poll_reports_real_progress(monkeypatch):
    """🔴 前端进度用 provider 的真字段,不需要模拟。

    实测响应里带 progress / estimated_time / actual_time —— 这条锁钉住"真的透出去了"。
    """
    import httpx
    _count_submit(monkeypatch, ["task-1"])
    seen = []
    _FakePollClient.calls = {"n": 0}
    _FakePollClient.script = [
        _Resp(200, {"data": {"status": "processing", "progress": 50,
                             "estimated_time": 100}}),
        _Resp(200, {"data": {"status": "completed", "progress": 100,
                             "estimated_time": 100, "actual_time": 49,
                             "result": {"images": [{"url": ["https://cdn.apimart.ai/x.png"]}]}}}),
    ]
    monkeypatch.setattr(httpx, "AsyncClient", _FakePollClient)

    url = _run(ic._poll("task-1", on_progress=seen.append))
    assert url
    assert seen, "一次进度都没回调"
    assert seen[0]["progress"] == 50 and seen[0]["estimated_time"] == 100
    assert seen[-1]["progress"] == 100 and seen[-1]["actual_time"] == 49


def test_progress_callback_failure_does_not_break_generation(monkeypatch):
    """必须不命中面:进度回调抛异常不该把已计费的生图拖垮。"""
    import httpx
    _FakePollClient.calls = {"n": 0}
    _FakePollClient.script = [_Resp(200, _done_payload())]
    monkeypatch.setattr(httpx, "AsyncClient", _FakePollClient)

    def boom(_p):
        raise RuntimeError("回调炸了")

    assert _run(ic._poll("task-1", on_progress=boom)), "回调异常把生图带崩了"


def test_first_delay_happens_at_most_once(monkeypatch):
    """长睡只做一次 —— 每轮都按预估长睡会在预估不准时越睡越久。"""
    import httpx
    _FakePollClient.calls = {"n": 0}
    # 一直 processing,直到预算耗尽
    _FakePollClient.script = [_Resp(200, {"data": {"status": "processing",
                                                   "progress": 10,
                                                   "estimated_time": 100}})]
    monkeypatch.setattr(httpx, "AsyncClient", _FakePollClient)
    _run(ic._poll("task-1"))
    # 首轮长睡 60s + 其余按 5s:总轮询次数应约 (420-60)/5 + 1
    expected = int((ic._POLL_MAX_WAIT_S - 60.0) / ic._POLL_INTERVAL_S) + 1
    assert abs(_FakePollClient.calls["n"] - expected) <= 1, (
        f"轮询次数 {_FakePollClient.calls['n']} 与'长睡只做一次'不符(应约 {expected})")


def test_poll_429_branch_untouched(monkeypatch):
    """429 分支既有逻辑不动:退避后继续,不算丢拍、不放弃。"""
    import httpx
    _count_submit(monkeypatch, ["task-1"])
    _FakePollClient.calls = {"n": 0}
    _FakePollClient.script = [_Resp(429), _Resp(200, _done_payload())]
    monkeypatch.setattr(httpx, "AsyncClient", _FakePollClient)

    out = _run(ic.generate_image("p"))
    assert out["ok"] is True, "429 之后没有继续轮询"


# ═════════════════════════════════════════════════════════════
# 三、download 跳(免费幂等 GET)
# ═════════════════════════════════════════════════════════════
class _FakeStream:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _StreamResp:
    def __init__(self, body: bytes, status=200, ctype="image/png"):
        self.status_code = status
        self.headers = {"content-type": ctype, "content-length": str(len(body))}
        self._body = body

    async def aiter_bytes(self):
        yield self._body


class _FakeDownloadClient:
    script: list = []
    calls = {"n": 0}

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, _method, _url):
        i = type(self).calls["n"]
        type(self).calls["n"] += 1
        eff = type(self).script[min(i, len(type(self).script) - 1)]
        if isinstance(eff, BaseException):
            raise eff
        return _FakeStream(eff)


@pytest.fixture
def _allow_test_host(monkeypatch):
    """让 SSRF 校验放行测试域(仍然真的**跑**校验,只是把域加进白名单)。"""
    monkeypatch.setenv("MARKETING_IMAGE_DOWNLOAD_HOSTS", "test.invalid")
    monkeypatch.setenv("MARKETING_IMAGE_ALLOW_PRIVATE_HOSTS", "test.invalid")


def test_download_retries_transient_connect_error(monkeypatch, _allow_test_host):
    """🔴 追加锁 ②:下载第一次 ConnectError、第二次成功 → 拿到字节。

    下载是免费幂等 GET,一次抖动就让整张卡死掉(image_download_failed)是浪费。
    """
    import httpx
    png = _tiny_png()
    _FakeDownloadClient.calls = {"n": 0}
    _FakeDownloadClient.script = [httpx.ConnectError(""), _StreamResp(png)]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDownloadClient)

    got = _run(ic.download_image("https://test.invalid/a.png"))
    assert got == png
    assert _FakeDownloadClient.calls["n"] == 2, "没有重试或重试过头"


def test_download_retries_read_timeout(monkeypatch, _allow_test_host):
    """下载跳的重试类比 submit 宽:read 类也可以重(免费幂等,无重复计费风险)。"""
    import httpx
    png = _tiny_png()
    _FakeDownloadClient.calls = {"n": 0}
    _FakeDownloadClient.script = [httpx.ReadTimeout(""), _StreamResp(png)]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDownloadClient)

    assert _run(ic.download_image("https://test.invalid/a.png")) == png


def test_download_success_does_not_retry(monkeypatch, _allow_test_host):
    """反向对照:一次就成功时不得重复请求。"""
    import httpx
    png = _tiny_png()
    _FakeDownloadClient.calls = {"n": 0}
    _FakeDownloadClient.script = [_StreamResp(png)]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDownloadClient)

    _run(ic.download_image("https://test.invalid/a.png"))
    assert _FakeDownloadClient.calls["n"] == 1


def test_download_revalidates_ssrf_on_every_attempt(monkeypatch, _allow_test_host):
    """🔴 每次重试都要重跑 SSRF 校验,不得因为"第一次过了"就免检。"""
    import httpx
    png = _tiny_png()
    seen = {"n": 0}
    real = ic._validate_download_url

    def counting(u):
        seen["n"] += 1
        return real(u)

    monkeypatch.setattr(ic, "_validate_download_url", counting)
    _FakeDownloadClient.calls = {"n": 0}
    _FakeDownloadClient.script = [httpx.ConnectError(""), _StreamResp(png)]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDownloadClient)

    _run(ic.download_image("https://test.invalid/a.png"))
    assert seen["n"] >= 2, f"SSRF 校验只跑了 {seen['n']} 次,重试绕过了校验"


def test_download_does_not_retry_ssrf_rejection(monkeypatch):
    """必须不命中面:域不在白名单是**判定结果**,不是网络抖动,不许重试。

    重试只会重复失败,还会把一个被拒的地址反复打出去。
    """
    import httpx
    monkeypatch.setenv("MARKETING_IMAGE_DOWNLOAD_HOSTS", "only.allowed")
    _FakeDownloadClient.calls = {"n": 0}
    _FakeDownloadClient.script = [_StreamResp(_tiny_png())]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDownloadClient)

    assert _run(ic.download_image("https://evil.example/a.png")) is None
    assert _FakeDownloadClient.calls["n"] == 0, "被 SSRF 拒的地址还发了请求"


def test_download_does_not_retry_bad_content_type(monkeypatch, _allow_test_host):
    """同上:content-type 不合法是判定结果,不重试。"""
    import httpx
    _FakeDownloadClient.calls = {"n": 0}
    _FakeDownloadClient.script = [_StreamResp(b"<html>", ctype="text/html")]
    monkeypatch.setattr(httpx, "AsyncClient", _FakeDownloadClient)

    assert _run(ic.download_image("https://test.invalid/a.png")) is None
    assert _FakeDownloadClient.calls["n"] == 1, "非网络类失败被重试了"


# ═════════════════════════════════════════════════════════════
# 四、配置与成本口径
# ═════════════════════════════════════════════════════════════
def test_retry_counts_are_single_sourced_and_env_overridable(monkeypatch):
    """次数走单一常量 + env,禁止多处写死。"""
    assert ic.connect_retries() == 5
    assert 3 <= ic.download_retries() <= 5
    monkeypatch.setenv("MARKETING_IMAGE_CONNECT_RETRIES", "9")
    assert ic.connect_retries() == 9, "env 覆盖不生效"
    monkeypatch.setenv("MARKETING_IMAGE_CONNECT_RETRIES", "垃圾")
    assert ic.connect_retries() == 5, "非法值没有回落到默认"


def test_backoff_has_jitter_and_spans_seconds_to_tens():
    """🔴 退避必须带 jitter 且跨度秒~十秒级。

    梯子抖动是**突发相关**的:5 连击若都落在同一个抖动窗口里,等于没重试。
    """
    first = {round(ic._backoff_delay(1), 6) for _ in range(30)}
    assert len(first) > 1, "同一 attempt 每次都同值 = 没有 jitter"
    assert min(ic._backoff_delay(1) for _ in range(30)) >= 0.5
    late = max(ic._backoff_delay(5) for _ in range(30))
    assert late >= 6.0, f"退避跨度没拉到十秒级(最大 {late:.1f}s)"
    assert late <= ic._RETRY_MAX_S + 0.01, "退避没有上限"


def test_connect_class_is_exactly_the_no_send_set():
    """🔴 可重试集合 = connect 三类,一个不多一个不少。

    多一个(如 ReadTimeout)就是重复付费;少一个就是白白放弃可救的请求。
    """
    import httpx
    assert set(ic._connect_error_types()) == {
        httpx.ConnectError, httpx.ConnectTimeout, httpx.ProxyError}


def test_download_retry_set_is_wider_but_excludes_value_errors():
    """下载跳更宽(含 read 类),但 ValueError 类判定结果不在其中。"""
    import httpx
    types = set(ic._download_retry_types())
    assert httpx.ReadTimeout in types and httpx.ReadError in types
    assert set(ic._connect_error_types()) <= types
    assert ValueError not in types, "SSRF/内容判定失败被算成可重试"


def test_cost_1k_is_owner_confirmed_rate():
    """1k = $0.01(示例值),不是旧的 0.006。"""
    assert ic.IMAGE_COST_USD_1K == 0.01
    assert ic.IMAGE_COST_USD["1k"] == 0.01


def test_header_comment_matches_the_constant():
    """🔴 常量改了注释不改,下一个读者继续信错注释(本仓成灾教训)。"""
    import pathlib
    src = pathlib.Path(ic.__file__).read_text(encoding="utf-8")
    head = src.split('"""')[1]
    assert "$0.01" in head, "头注释还写着旧价"
    assert "$0.006/" not in head, "头注释里仍残留 0.006"


def test_tracker_prices_image_per_picture():
    """价目表必须有 apimart/gpt-image-2,且按【张】计价而不是按 token。"""
    from tools.llm_call_tracker import (APIMART_IMAGE_CALL_CNY, PRICING_TABLE,
                                        USD_TO_CNY, estimate_cost)
    row = PRICING_TABLE[("apimart", "gpt-image-2")]
    assert row.get("flat_rate_per_call") == APIMART_IMAGE_CALL_CNY
    assert row["input"] == 0 and row["output"] == 0, "图片不该有 token 单价"
    # 与 image_client 的美元费率同源
    assert abs(APIMART_IMAGE_CALL_CNY - ic.IMAGE_COST_USD_1K * USD_TO_CNY) < 1e-6
    assert abs(estimate_cost("apimart", "gpt-image-2", 0, 0)
               - APIMART_IMAGE_CALL_CNY) < 1e-9
    # 多张按 billable_units 线性
    assert abs(estimate_cost("apimart", "gpt-image-2", 0, 0,
                             metadata={"billable_units": 3})
               - APIMART_IMAGE_CALL_CNY * 3) < 1e-9


def test_tracker_no_longer_falls_back_to_default_pricing():
    """必须不命中面:命中前的行为是落 DEFAULT_PRICING(按 token,图片记≈0)。"""
    from tools.llm_call_tracker import DEFAULT_PRICING, PRICING_TABLE
    row = PRICING_TABLE[("apimart", "gpt-image-2")]
    assert row != DEFAULT_PRICING
    assert DEFAULT_PRICING.get("flat_rate_per_call") is None, (
        "前提变了:DEFAULT 现在也按张计价,这条锁要重写")
