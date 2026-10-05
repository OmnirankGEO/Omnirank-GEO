"""v1.7 真 C 完整版 SSE Test 51-58

锁 SSOT SOCIAL_V1.7_SSOT_2026-05-22.md §1.8.1(后端 Test 51-57)+ §1.8.2(前端 safe rule Test 58)·
共 8 个 test 跟 50 个 v1.6 capability test 续接(Test 1-50 原在 v1.6 能力清单测试包;该包末批格守的社媒工作台页面随开源 E3 整删,包已退役)。

本文件覆盖:
- Test 51:select_backbone signal 分发 + step 数 4/5 混存 + render_steps 返 status='pending'
- Test 52:SSE Queue mux 架构(52a/52b/52c · 老板七审 P1 关键)
- Test 53-57:5 层 LLM fallback(timeout / 长度 / 禁词 / 空 label / 超长)
- Test 58:前端 safe rule(用 Python 模拟 React reducer · helper 函数 mirror)
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

# [开源 E3 · B2] SSE 进度三件模块已删,原顶层 import 随之删除


# ============================================================
# Test 52 · SSE Queue mux 架构(老板七审 P1 关键 · STEP 1.1 锁)
# ============================================================


def _run(coro):
    return asyncio.run(coro)


# [开源 E3 · B2 · 2026-09-28] SSE 进度三件(事件复用器 / 进度骨架 / 动态文案)与社媒内容工坊、能力处理器随 E3 删除,
#   守它们的格退役;下面只剩守前端镜像规则 / content_api / nginx 的格。


# ============================================================
# Test 51 · select_backbone signal 分发 + step 数 4/5 混存 + render_steps status='pending'
# ============================================================


# ============================================================
# Test 53-57 · 5 层 LLM fallback(SSOT §1.6)
# ============================================================


# ============================================================
# Test 58 · 前端 safe rule(Python mirror React reducer)
# ============================================================


# [开源 E3 · B2 · 2026-09-28] Test 58 三格(前端 safe rule 镜像)的输入是已删进度骨架的 render_backbone_steps,
#   随骨架模块退役;只为它们服务的三个 _mirror_* helper 一并删。


# ============================================================
# Test 81 / 82 · v1.7.2 真 C 完整版 scope 扩展(SSOT §1.3.13 全 11 类覆盖)
# ============================================================


# ============================================================
# Test 83 · v1.7.3 BUG-F · 主流 _quick_progress_signals 覆盖完整 11 类 backbone signal
# ============================================================


# ============================================================
# Test 84 · v1.7.3 BUG-G · _quick_progress_title 4 个 backbone 分支补齐
# ============================================================


# ============================================================
# Test 85 · v1.7.3 BUG-I · chat_research helper fail · 发 status 提示 · 防双 progress_plan 视觉突变
# ============================================================


# ============================================================
# Test 86 · v1.7.4 P0 · 长链路对话失忆修复(老板 2026-05-22 真机)
# 根因:写稿 LLM 收到 topic=user_input · 但"这段话/这条/上面"指代词无上下文 → LLM 跑题
# 修法:_build_recent_chat_context helper + business_context 拼入最近 N 轮 chat history
# ============================================================


# ============================================================
# v1.7.6 · Test 87-89(P0-3 / P0-4 / P1-3 防回归)
# ============================================================


# [开源 E3 · B2 · 2026-09-28] 限流轻量桶原登记的聊天附件 / 会话端点随 E3 删除,名单清空;守它们的 89a-c 三格退役。


# ============================================================
# v1.7.6 · Round 3 · Test 90-95(P0-2 / P0-6 / P1-1 / P1-4 防回归)
# ============================================================


def test_91a_v17_6_p0_2_resolve_billing_brand_id_helper():
    """91a · v1.7.6 P0-2 FIN-002 · _resolve_billing_brand_id helper 存在 + profile 优先 fallback JWT"""
    from pathlib import Path
    src = Path("api/content_api.py").read_text(encoding="utf-8")
    assert "def _resolve_billing_brand_id(" in src, (
        "P0-2 · 必有 _resolve_billing_brand_id helper"
    )
    assert "profile.brand_id 优先" in src, "P0-2 · helper docstring 必说明 profile 优先"
    assert "fallback JWT" in src.lower() or "fallback brand_ids" in src.lower(), (
        "P0-2 · 必有 JWT fallback 兜底"
    )
    # 越权检测 · profile.brand_id 不在 user.client_brand_ids 时 fallback + log warning
    assert "NOT in user.client_brand_ids" in src, (
        "P0-2 · 越权时必 log warning fallback(防 admin 之外 user 写错单)"
    )


def test_91b_v17_6_p0_2_bill_accepts_profile_id_kwarg():
    """91b · v1.7.6 P0-2 · _bill / _bill_ctx 加 profile_id 可选参数(兼容老 caller)"""
    from api.content_api import _bill, _bill_ctx
    import inspect

    sig_bill = inspect.signature(_bill)
    assert "profile_id" in sig_bill.parameters, "P0-2 · _bill 必有 profile_id 参数"
    assert sig_bill.parameters["profile_id"].default is None, "P0-2 · profile_id 默认 None(老 caller 兼容)"

    sig_ctx = inspect.signature(_bill_ctx)
    assert "profile_id" in sig_ctx.parameters, "P0-2 · _bill_ctx 必有 profile_id 参数"


# ============================================================
# v1.7.6.1 · Test 94-98(P0-3a/b/c/d charge-before-expose · SSE 断连扣费灾难线)
#
# 老板 2026-05-23 深度诊断:v1.7.6 P0-3 修的是 CancelledError 来时退费 · 但真机里
# 浏览器 abort 不一定让 starlette 抛 CancelledError · LLM 继续跑完 + 钱已扣 + 0 refund
# 根因:扣费时机错位(charge-before-LLM 而不是 charge-before-expose) + 断连检测缺失
# 修法 7 条:test first · 预检查不真扣 · _abortable_await 包长 await · final alive check
#         · alive 过才 _bill · emit 失败才 refund · log 三标识(disconnect/skip_charge/charge-before-expose)
# ============================================================


def test_94_v17_6_1_p0_3b_abortable_await_helper_exists():
    """94 · v1.7.6.1 P0-3b · _abortable_await helper 必存在 · 接受 (request, coro_or_task, phase)"""
    from api.content_api import _abortable_await
    import inspect

    sig = inspect.signature(_abortable_await)
    params = list(sig.parameters.keys())
    assert "request" in params, "P0-3b · 第一参数必为 request(用 request.is_disconnected)"
    # 必有 phase keyword 用于 log
    assert "phase" in params, "P0-3b · 必有 phase keyword 用于 log"


@pytest.mark.asyncio
async def test_94a_v17_6_1_p0_3b_abortable_await_returns_result_when_task_finishes_first():
    """94a · v1.7.6.1 · task 先完成 · watcher 未触发 → 返 task.result · 不抛"""
    from api.content_api import _abortable_await

    class _FakeReq:
        async def is_disconnected(self):
            return False  # 永远连着

    async def _fast_task():
        await asyncio.sleep(0.05)
        return "done-fast"

    result = await _abortable_await(_FakeReq(), _fast_task(), phase="test_fast")
    assert result == "done-fast", "P0-3b · task 先完成必返 task.result"


@pytest.mark.asyncio
async def test_94b_v17_6_1_p0_3b_abortable_await_raises_cancel_when_disconnect():
    """94b · v1.7.6.1 P0-3b · disconnect 期间触发 → cancel task + raise CancelledError

    防回归:这是 charge-before-expose 架构的入口 · watcher 必须能抢先 cancel · 让 caller skip_charge
    """
    from api.content_api import _abortable_await

    disconnect_after = 0.6  # 600ms 后断
    task_was_cancelled = {"flag": False}

    class _FakeReq:
        def __init__(self):
            self._start = asyncio.get_event_loop().time()

        async def is_disconnected(self):
            elapsed = asyncio.get_event_loop().time() - self._start
            return elapsed >= disconnect_after

    async def _long_task():
        try:
            await asyncio.sleep(3.0)  # 长 LLM 模拟
            return "should-not-reach"
        except asyncio.CancelledError:
            task_was_cancelled["flag"] = True
            raise

    with pytest.raises(asyncio.CancelledError):
        await _abortable_await(_FakeReq(), _long_task(), phase="test_disconnect")

    assert task_was_cancelled["flag"] is True, (
        "P0-3b · disconnect 必触发 task.cancel · 让 LLM 终止"
    )


def test_95b_v17_6_1_p0_3a_main_stream_bills_after_alive_check():
    """95b · v1.7.6.1 P0-3a · _bill 必在 generation_task 完成 + final alive check 之后"""
    from pathlib import Path
    src = Path("api/content_api.py").read_text(encoding="utf-8")
    # 必有 final alive check log
    assert "final alive check" in src, (
        "P0-3a · 暴露正文前必有 final alive check"
    )
    # 必有 charge-before-expose log
    assert "charge-before-expose" in src, (
        "P0-3a · 扣费成功必 log 'charge-before-expose' · Deploy-CTO 可 grep"
    )
    # 必有 skip_charge log(disconnect 时)
    assert "skip_charge" in src, (
        "P0-3a · disconnect 时必 log 'skip_charge' · 便于对账"
    )


def test_97_v17_6_1_p0_3a_bill_ctx_final_alive_check():
    """97 · v1.7.6.1 P0-3a · _bill_ctx yield 后扣费前必有 final alive check

    防回归:_bill_ctx 是 contextmanager · yield 后真扣 · 浏览器 abort 不触发 CancelledError 时同样会扣
    覆盖:script_gen / topic_gen / web_search / hook_gen / comment_gen 6 处 caller 全部受益
    """
    from pathlib import Path
    src = Path("api/content_api.py").read_text(encoding="utf-8")
    # 找 _bill_ctx 函数体
    bill_ctx_idx = src.find("async def _bill_ctx(")
    assert bill_ctx_idx > 0, "测试自检:必能找到 _bill_ctx"
    body = src[bill_ctx_idx : bill_ctx_idx + 5000]
    # yield 后必有 is_disconnected check
    yield_idx = body.find("yield\n")
    assert yield_idx > 0
    post_yield = body[yield_idx:]
    assert "request.is_disconnected()" in post_yield, (
        "P0-3a · _bill_ctx yield 后必有 final alive check"
    )
    # 必有 P0-3a · skip_charge log
    assert "P0-3a" in post_yield, "P0-3a · 必标 v1.7.6.1 修复点"
    assert "skip_charge" in post_yield, "P0-3a · 必 log skip_charge 便于 Deploy-CTO grep"


# ============================================================
# v1.7.6.2 · Test 99-103(prod 实证后真根治 · 老板 2026-05-24 全权拍 A)
#
# 真因(Deploy-CTO 2026-05-24 prod 实证):
#   v1.7.6.1 charge-before-expose 在 prod 失效 · is_disconnected() 永 False
#   · 4 笔 script_gen + 1 笔 topic_gen abort 后全扣 · 0 退
#   · prod log:5 条 'charge-before-expose · billed=True' · 0 条 'disconnect detected' · drainer cancelled=0
#   · X-Accel-Buffering: no 已在 _SSE_HEADERS · 不是修法
#   · 真根因:nginx + Aliyun ESA 不关 upstream connection · uvicorn 收不到 EOF
# 修法 4 层:
#   1. nginx line 200 补齐 SSE 完整配置(对齐 line 306)· 让 nginx 正确关 upstream
#   2. Cache-Control no-transform · 防 Aliyun ESA buffer/transform
#   3. _with_heartbeat catch write-fail · client 断 → write 抛 → 关 generator(双保险)
#   4. topic_gen 漏接 P0-2 _resolve_billing_brand_id(id 1104 brand_id=477 不是 476)
# 顺修 Codex test_98b:topic-stream LLM 期间也加 disconnect watcher(不光 final alive)
# ============================================================


def test_99_v17_6_2_nginx_line_200_complete_sse_config():
    """99 · v1.7.6.2 · nginx /api/content/quick-generate-stream 必补齐 SSE 完整配置

    真因(2026-05-24 Deploy-CTO 实证):line 200 exact match 缺 proxy_http_version 1.1
    · nginx 用 HTTP/1.0 到 upstream · client disconnect 不传到 backend · uvicorn 收不到 EOF
    修法:对齐 line 306 regex catch-all 的完整 SSE 配置
    """
    from pathlib import Path
    src = Path("nginx.conf").read_text(encoding="utf-8")
    block_idx = src.find('location = /api/content/quick-generate-stream {')
    assert block_idx > 0, "测试自检:必能找到 nginx line 200 exact match block"
    block_body = src[block_idx : block_idx + 800]
    assert "proxy_http_version 1.1" in block_body, (
        "v1.7.6.2 · nginx line 200 必加 proxy_http_version 1.1(主嫌疑 · upstream disconnect 传播)"
    )
    assert "proxy_request_buffering off" in block_body, (
        "v1.7.6.2 · nginx line 200 必加 proxy_request_buffering off"
    )
    assert "chunked_transfer_encoding on" in block_body, (
        "v1.7.6.2 · nginx line 200 必加 chunked_transfer_encoding on"
    )
    assert "Connection \"\"" in block_body or "Connection ''" in block_body or 'Connection ""' in block_body, (
        "v1.7.6.2 · nginx line 200 必加 proxy_set_header Connection '' 清空"
    )


def test_101_v17_6_2_with_heartbeat_catches_write_fail():
    """101 · v1.7.6.2 · _with_heartbeat 必 catch write-fail(双保险 disconnect 路径)

    真因:starlette 的 listen_for_disconnect 依赖 uvicorn 发 http.disconnect
    · uvicorn 收不到 EOF(nginx/ESA 不关 upstream)→ disconnect 永不发
    · 但 backend 每 10s 写 keepalive · client 断时 write 应失败抛 ConnectionResetError/BrokenPipeError/anyio.BrokenResourceError
    · catch 后转 generator 取消 · 是 is_disconnected 失败的备用路径
    """
    from pathlib import Path
    src = Path("api/content_api.py").read_text(encoding="utf-8")
    hb_idx = src.find("async def _with_heartbeat(")
    assert hb_idx > 0
    body = src[hb_idx : hb_idx + 2500]
    has_connection_catch = (
        "ConnectionError" in body
        or "BrokenPipeError" in body
        or "BrokenResourceError" in body
    )
    assert has_connection_catch, (
        "v1.7.6.2 · _with_heartbeat 必 catch ConnectionError/BrokenPipeError/BrokenResourceError 等 write-fail 异常"
    )
    assert "v1.7.6.2" in body or "write-fail" in body, (
        "v1.7.6.2 · _with_heartbeat 必注释标记修复点"
    )

