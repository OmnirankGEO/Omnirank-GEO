# -*- coding: utf-8 -*-
"""WO_221-c1' · 回显锁的**行为**,不是它的存在。

🔴 为什么另起一个文件:c1 里那条 `test_both_official_call_sites_actually_call_the_echo_assert`
   是 **AST「调用存在」锁**。Review 的三发毒从它下面走了过去:
     Pa 把回显锁挪到 `record(success=True)` **之后**  -> 仍绿
     Pb 用 `try/except: pass` 把它**吞掉**            -> 仍绿
     Pc ai_tester 回显不符分支**不返 engine_error**   -> 仍绿
   「调用存在」既看不见**谁接住了异常**,也看不见**它在 record 前还是后**。
   同一个病本单已经犯过两次(WO_220 的 platform、ocr_qa)——
   **返回值对不对、有没有被接上、接住之后做了什么,是三件事。**

🔴 两条端点行为不同(2026-09-15 各打一发实测):
     /v1/chat/completions   请求旧名 -> 回显 `deepseek-flash`(厂商归一)
     /anthropic/v1/messages 请求旧名 -> 回显 `deepseek-v4-flash`(**原样**)
   监测线走 Anthropic 那条 ⇒ 回显锁在那儿是同义反复,**真正的防线是发出前归一**。
   所以本文件既锁回显行为,也锁归一入口。
"""
from __future__ import annotations

import asyncio
import json

import pytest


class _Resp:
    def __init__(self, payload, status=200):
        self._p = payload
        self.status_code = status
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


def _anthropic_payload(echoed_model):
    """官方 Anthropic Messages 形状(形状自检要过,才轮得到回显锁)。"""
    return {
        "type": "message", "role": "assistant", "model": echoed_model,
        "stop_reason": "end_turn",
        "content": [{"type": "text", "text": "答案"}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


class _ClientStub:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    def __call__(self, *a, **kw):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return _Resp(self._payload)


class _TrackStub:
    """记下 `record` 被怎么调的 —— 判据要看的是**成功与否**,不是调没调。"""

    def __init__(self):
        self.records = []

    def __call__(self, *a, **kw):
        self.meta = {"args": a, "kw": kw}
        outer = self

        class _T:
            async def __aenter__(self_i):
                return self_i

            async def __aexit__(self_i, *exc):
                return False

            def record(self_i, **kw2):
                outer.records.append(kw2)

        return _T()


# ══════════════════════════════════════════════════════════════════
# 1. platforms 官方通道:抛出 + **没有以 success=True 记账**
# ══════════════════════════════════════════════════════════════════
def test_platforms_raises_and_does_not_record_success_on_echo_mismatch(monkeypatch):
    """🔴 [钉 Pa+Pb] 回显不符必须**抛**,而且那一次**不许以 success=True 落账**。

    Pa(挪到 record 之后)与 Pb(try/except 吞掉)都能让「调用存在」读绿,
    但它们的后果完全不同:一次供应商静默换模型的调用会被记成成功,
    成本表与 WO_215 的心跳判据都看不出异常。
    """
    from config.deepseek_models import OfficialModelEchoMismatch
    import services.research_monitor.platforms as P

    stub = _ClientStub(_anthropic_payload("deepseek-someone-else"))
    track = _TrackStub()
    monkeypatch.setattr(P.httpx, "AsyncClient", stub)
    monkeypatch.setattr(P, "llm_track", track)
    monkeypatch.setattr(P, "_load_model_from_config",
                        lambda k, d=None: "deepseek-flash", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-fake")

    with pytest.raises(OfficialModelEchoMismatch):
        asyncio.run(P.query_deepseek(1, "问题"))

    good = [r for r in track.records if r.get("success") is True]
    assert not good, (
        "回显不符的那一次仍以 success=True 落账:%s —— "
        "成本表上它看起来是一次正常调用" % good)


def test_platforms_still_records_success_when_the_echo_agrees(monkeypatch):
    """反向臂:回显一致时**必须**照常落账成功。

    只钉「不符时不记成功」的话,一个**永远不记成功**的实现也能满足它。
    """
    import services.research_monitor.platforms as P
    stub = _ClientStub(_anthropic_payload("deepseek-flash"))
    track = _TrackStub()
    monkeypatch.setattr(P.httpx, "AsyncClient", stub)
    monkeypatch.setattr(P, "llm_track", track)
    monkeypatch.setattr(P, "_load_model_from_config",
                        lambda k, d=None: "deepseek-flash", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-fake")
    asyncio.run(P.query_deepseek(1, "问题"))
    assert [r for r in track.records if r.get("success") is True], (
        "回显一致却没记成功 —— 这条通道等于永远不落账")


# ══════════════════════════════════════════════════════════════════
# 2. 归一入口:配置里写旧名,发出去的必须是新名
# ══════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("configured", ["deepseek-v4-flash", "deepseek-chat"])
def test_a_stale_name_in_runtime_config_is_normalised_before_emit(monkeypatch, configured):
    """🔴 这是 Anthropic 那条端点上的**真防线**。

    回显锁在那儿抓不到旧名(服务端原样回显),所以如果运行期配置
    `geo_research_config.model_deepseek_official` 还写着旧名,
    不归一就会**静默照发**,而锁会放行 —— 不是我原先说的「100% 报错」。
    """
    import services.research_monitor.platforms as P
    stub = _ClientStub(_anthropic_payload("deepseek-flash"))
    track = _TrackStub()
    monkeypatch.setattr(P.httpx, "AsyncClient", stub)
    monkeypatch.setattr(P, "llm_track", track)
    monkeypatch.setattr(P, "_load_model_from_config",
                        lambda k, d=None: configured, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-fake")
    asyncio.run(P.query_deepseek(1, "问题"))
    sent = stub.calls[0]["json"]["model"]
    assert sent == "deepseek-flash", (
        "配置里写着 %s,发出去的还是 %s —— 归一没接上" % (configured, sent))


def test_the_dashscope_key_is_not_normalised():
    """🔴 反向钉:`model_deepseek_via_dashscope` **不许**归一。

    `normalize_deepseek_model` 只认官方线的历史名;百炼上 `deepseek-v4-flash`
    是另一家的**活**模型 ID,归一会把它改成百炼不认识的名字。
    上面那条归一、这条不归一,是**有意的不对称** —— 后来人「顺手统一」就坏了。
    """
    import io
    import pathlib
    src = io.open(pathlib.Path(__file__).resolve().parents[2]
                  / "services" / "research_monitor" / "platforms.py",
                  encoding="utf-8").read()
    i = src.index("model_deepseek_via_dashscope")
    line_start = src.rfind("\n", 0, i) + 1
    line_end = src.find("\n", i)
    line = src[line_start:line_end]
    assert "normalize_deepseek_model" not in line, (
        "百炼那条键被归一了:%s" % line.strip())


# ══════════════════════════════════════════════════════════════════
# 3. ai_tester:回显不符 -> engine_error,且两个名字都在
# ══════════════════════════════════════════════════════════════════
def _run_official(monkeypatch, echoed_model, *, content_text="答案正文"):
    """真跑 `query_deepseek_official`,只桩掉出网与下游分析。

    🔴 c1'' 返工:上一版是**源码切片**检查
    (`blk = src[i:i+900]; assert '"engine_error": True' in blk`)——
    它**不执行分支**。把那个 `return ToolResponse(...)` 塞进 `if False:`,
    文本还在,判据照样绿(Review 的毒 Pc 正是这么过去的)。

    🔴 更该记的是:我在**本文件抬头里写了「Pc 仍绿」**,命名了这个洞,
    然后给它配了一条文本锚。**知道有洞** 和 **守得住那个洞** 是两件事 ——
    和本单 ③「改了代码没写锁」是同一个形状,只是这次连「知道」都有了。
    """
    import json as _json
    import asyncio as _asyncio
    import tools.ai_visibility.ai_tester as T

    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-fake-official")

    payload = {
        "id": "t-1", "type": "message", "role": "assistant",
        "model": echoed_model, "stop_reason": "end_turn",
        "content": [{"type": "text", "text": content_text}],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }

    class _R:
        status_code = 200
        text = _json.dumps(payload, ensure_ascii=False)

        def json(self):
            return payload

    async def _fake_post(client, url, **kw):
        return _R()

    class _C:
        def __call__(self, *a, **kw):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(T, "_tracked_post", _fake_post)
    monkeypatch.setattr(T.httpx, "AsyncClient", _C())

    async def _fake_analyze(ai_response, query, check_brand, *a, **kw):
        return {"mentioned_brands": [], "brand_detected": False,
                "answer_summary": ai_response[:40]}

    monkeypatch.setattr(T, "_call_analyze_visibility", _fake_analyze, raising=False)

    async def _no_sleep(*a, **kw):
        return None

    monkeypatch.setattr(_asyncio, "sleep", _no_sleep)

    resp = _asyncio.run(T.query_deepseek_official("问题", check_brand="某品牌"))
    blocks = getattr(resp, "content", None) or []
    assert blocks, "ToolResponse 里没有 content"
    return _json.loads(blocks[0]["text"])


def test_ai_tester_returns_engine_error_naming_both_models(monkeypatch):
    """🔴 [钉 Pc · c1''] 回显不符时**真的走到**那条分支并返 engine_error。

    「抛/拒」与「返回一个下游认得出来的失败」是两件事:
    不返 engine_error 的话,上游会把一次**身份不明**的回答当成正常结果记进监测。
    两个名字都要在 —— 没有它们,看到告警的人不知道被换成了什么。
    """
    out = _run_official(monkeypatch, "deepseek-someone-else")
    assert out.get("engine_error") is True, (
        "回显不符却没标 engine_error:%s" % {k: out.get(k) for k in list(out)[:6]})
    summary = str(out.get("answer_summary") or "")
    assert "deepseek-someone-else" in summary and "deepseek-flash" in summary, (
        "answer_summary 没同时写清「回显的」与「请求的」:%r" % summary)


def test_the_official_path_still_works_when_the_echo_agrees(monkeypatch):
    """反向臂:回显一致时走**正常路径**,不许恒返 engine_error。

    只钉「不符时红」的话,一个**永远返 engine_error** 的实现也能满足它。
    """
    out = _run_official(monkeypatch, "deepseek-flash", content_text="正常答案")
    assert not out.get("engine_error"), (
        "回显一致却标了 engine_error —— 这条通道等于永远不通:%s" % out)
