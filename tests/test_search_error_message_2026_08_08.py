"""检索供应商异常描述锁 —— 「永不为空」+「链上真消息要浮上来」。

背景见 `tools/search/_err.py` 抬头:2026-08-08 实测,秘塔与豆包的失败**双双被吞成空串**
(`{"error": ""}` / `ConnectError: `),导致任何「有没有 error / 抛没抛异常」的判据**恒绿**。

🔴 判据设计:
  · **正例**必须配 **反例** —— 只测"空异常能出非空描述",实现里 `return "x"` 也能满分,
    所以同时锁「真消息必须原样浮上来」与「类型链必须保留」;
  · 直接锁**消费方看到的那个字段**(秘塔 ToolResponse 里的 `error` 字符串),
    不是只锁 helper 的返回值 —— 「函数对、接线缺」本周已第五次。
"""
import json
import ssl

import pytest

from tools.search._err import describe_exc


# ── ① 核心:整条链都没有消息时,绝不返回空 ──────────────────────────────
def test_all_empty_chain_never_returns_empty():
    """无参异常的 str() 就是空串 —— 这是本次事故的原形。

    🔴 不用 `ssl.SSLEOFError()` 做这条的载体:实测它的 str() **随 Python 版本变** ——
    容器 py3.12 是 `''`(生产实测 `{"error": ""}` 就是它产的),
    本机 py3.13 是 `'()'`。用例前提不能挂在会变的东西上,所以用确定为空的构造。
    真实的 SSLEOFError 形态由下面 `test_deep_message_surfaces` 覆盖。
    """
    inner = ValueError()
    assert str(inner) == "", "前提变了:无参 ValueError 的 str() 不再是空串,本用例需重写"
    out = describe_exc(inner)
    assert out, "描述为空 = 本模块存在的理由没了"
    assert "ValueError" in out, f"至少要让人看见类型,实际={out!r}"


def test_nested_all_empty_never_returns_empty():
    try:
        try:
            raise ssl.SSLEOFError()
        except Exception as e:
            raise ConnectionError() from e
    except Exception as outer:
        out = describe_exc(outer)
    assert out.strip(), "整链无消息时仍不许返回空"
    assert "ConnectionError" in out and "SSLEOFError" in out, f"类型链丢了:{out!r}"


# ── ② 核心:真消息藏在链深处时,必须浮上来 ────────────────────────────
def test_deep_message_surfaces():
    """复刻当天真实链条:外两层 str() 全空,真消息在第 3 层。"""
    real = "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
    try:
        try:
            try:
                raise ssl.SSLEOFError(real)
            except Exception as e1:
                raise EOFError() from e1
        except Exception as e2:
            raise ConnectionError() from e2
    except Exception as outer:
        out = describe_exc(outer)
    assert "UNEXPECTED_EOF_WHILE_READING" in out, f"真消息没浮上来:{out!r}"
    assert "ConnectionError" in out, "外层类型不该丢(要知道是在哪一层炸的)"


# ── ③ 反例:本来就有消息的普通异常,不许被改写丢失 ────────────────────
def test_plain_message_preserved():
    out = describe_exc(ValueError("brand_id 必须是整数"))
    assert "brand_id 必须是整数" in out
    assert "ValueError" in out


# ── ④ 长度上限生效,但不许把消息截成空 ────────────────────────────────
def test_limit_truncates_but_never_empties():
    out = describe_exc(ValueError("x" * 500), limit=40)
    assert 0 < len(out) <= 40


def test_no_limit_keeps_full_message():
    msg = "y" * 300
    out = describe_exc(ValueError(msg), limit=0)
    assert msg in out


# ── ⑤ 自引用链不许死循环 ──────────────────────────────────────────────
def test_self_referential_chain_terminates():
    a = ValueError()
    try:
        raise a
    except Exception:
        pass
    a.__cause__ = a          # 人为构造自引用
    out = describe_exc(a)    # 不挂住就算过
    assert out


# ── ⑥ 🔴 接线锁:消费方真正看到的那个 error 字段必须非空 ──────────────
@pytest.mark.asyncio
async def test_metaso_response_error_field_is_never_empty(monkeypatch):
    """打在 `metaso_web_search` 返回的 ToolResponse 上,不是打在 helper 上。

    构造:让底层 httpx 抛一个 str() 为空的 SSLEOFError —— 修复前这里会产出
    `{"error": ""}`(当天生产实测原文)。
    """
    import tools.search.metaso_mcp as M

    # 🔴 不能只 setenv:key 在 import 时就被读进模块级 `METASO_MCP_CONFIG` 常量了,
    #    改环境变量盖不住(试过,直接短路成 "METASO_API_KEY not configured",
    #    等于没走到要锁的那条异常路径 —— 判据会变成测了个寂寞)。要打真目标。
    monkeypatch.setitem(M.METASO_MCP_CONFIG, "api_key", "dummy-for-test")

    class _BoomClient:
        # 无参 SSLEOFError:str() 为空(容器 py3.12 的真实形态)。
        # 即便某些版本 str() 非空,本用例断言的是「error 字段非空」,两种情况都必须成立。
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise ssl.SSLEOFError()

    monkeypatch.setattr(M.httpx, "AsyncClient", _BoomClient)
    # 🔴 不 monkeypatch 重试的 sleep:`_asyncio` 是**函数内局部 import**,不是模块属性,
    #    patch 不上(试过,AttributeError)。让它真睡一轮,用例多花约 3s,换判据不作假。

    resp = await M._metaso_web_search_direct("测试", "webpage", True, False, 1)
    payload = json.loads(resp.content[0]["text"])
    assert "error" in payload, "形态变了:不再返回 error 字段,本用例需重写"
    assert payload["error"].strip(), (
        "消费方拿到的 error 是空串 —— 这正是 2026-08-08 事故里"
        "「有响应、字段齐全、错误为空,看起来像成功」的形态"
    )
    assert "SSLEOFError" in payload["error"], f"至少要认出是什么错:{payload['error']!r}"


