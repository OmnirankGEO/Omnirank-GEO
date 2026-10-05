"""C-2(c) · 总超时**降级交付**,不整批丢弃(Codex 终审 P1-9)。

被测的那一格逐字:``tools/ai_visibility/ai_tester.py`` 的
``detailed_ai_visibility_test`` 在 4 分钟总超时那一支写着::

    except asyncio.TimeoutError:
        print("⚠️ AI可见度测试总超时(4分钟)，使用已完成的结果")
        results_list = []          # ← 与上一行**正好相反**

已经成功返回的平台结果被全部扔掉,``total_tests`` 归零。后果落在**钱**上:
``evaluate_sample`` 第 ② 档 ``zero_usable_result`` ⇒ INSUFFICIENT ⇒ 整单退款、
报告不交付 —— 而真相可能是 32 格里 30 格已经拿到了答案。

🔴 这里**不打真 provider**:被测的是"超时那一刻怎么处置已完成的 Task",
   与外调无关。注入走参数(``engines`` + monkeypatch 单引擎查询函数),
   不走 ``is_test`` 分支 —— 生产链上零测试短路是本仓铁律。
"""

from __future__ import annotations

import asyncio

import pytest

from tools.ai_visibility import ai_tester


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def _fast_timeout(monkeypatch):
    """把 4 分钟总超时压到可测的量级。

    🔴 改的是 ``asyncio.wait_for`` 的入参而不是被测代码里的常量 ——
       让被测那一行(``timeout=240``)原样跑,判据不依赖改源码。
    """
    real_wait_for = asyncio.wait_for

    async def _short(aw, timeout=None):                   # noqa: ANN001
        return await real_wait_for(aw, timeout=0.35)

    monkeypatch.setattr(ai_tester.asyncio, "wait_for", _short)
    return _short


def _install_engines(monkeypatch, *, slow_engines: set[str], on_cancel=None):  # noqa: ANN001
    """每个引擎一个假实现:``slow_engines`` 里的那几个永远不返回。

    返回体形状逐字照 ``query_dashscope_search`` 的真实返回(``ToolResponse``
    带一个 JSON 文本),因为 ``query_single`` 会 ``json.loads`` 它。

    🔴 [外选 EXTC-05] ``on_cancel`` 是**在飞任务被取消**的观测点:
       慢引擎那一支把 ``CancelledError`` 接住、记一笔、再原样抛回去
       (不吞 —— 吞掉会把"被取消"变成"正常返回",两种行为在外面又分不开了)。
    """
    import json

    class _Resp:
        def __init__(self, payload: dict) -> None:
            self.content = [{"text": json.dumps(payload, ensure_ascii=False)}]

    def _payload(engine: str) -> dict:
        return {
            "engine": engine,
            "response": f"{engine} 的回答正文",
            "brand_detected": True,
            "brand_verdict": "YES",
            "engine_error": False,
            "web_search_enabled": True,
            "mentioned_brands_inline": [],
            "platform_key": engine,
            "surface_key": "ai_search",
        }

    async def _make(engine: str, *_a, **_kw):             # noqa: ANN001
        if engine in slow_engines:
            try:
                await asyncio.sleep(30)                   # 永远等不到
            except asyncio.CancelledError:
                if on_cancel is not None:
                    on_cancel(engine)
                raise
        return _Resp(_payload(engine))

    for engine, fname in (("dashscope", "query_dashscope_search"),
                          ("deepseek", "query_deepseek_official"),
                          ("doubao", "query_doubao_search"),
                          ("kimi", "query_kimi_search")):
        monkeypatch.setattr(
            ai_tester, fname,
            (lambda e: (lambda *a, **k: _make(e, *a, **k)))(engine))


def test_c2_20_total_timeout_keeps_the_platforms_that_already_answered(
        monkeypatch, _fast_timeout) -> None:
    """🔴 本项的**本体判据**:超时之后已完成的平台结果必须还在。

    拆红:把那一支改回 ``results_list = []`` ⇒ ``total_tests`` 归 0 ⇒ 本条红。
    """
    _install_engines(monkeypatch, slow_engines={"doubao"})

    out = _run(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一", "问题二"],
        engines=["dashscope", "deepseek", "doubao"],
    ))
    bds = out["brand_detection_summary"]

    assert bds["total_planned"] == 2 * 3, bds
    # 两个快引擎 × 两题 = 4 格成功;豆包那两格超时丢掉。
    assert bds["total_tests"] == 4, (
        f"超时把已完成的结果也扔了(total_tests={bds['total_tests']}) —— "
        "那正是「使用已完成的结果」这句话说了没做的事")
    assert bds["total_failed"] == 2, bds
    assert bds["by_engine"]["dashscope"]["total"] == 2, bds["by_engine"]
    assert bds["by_engine"]["deepseek"]["total"] == 2, bds["by_engine"]
    assert bds["by_engine"].get("doubao", {}).get("total", 0) == 0, bds["by_engine"]


def test_c2_21_degraded_result_settles_partially_not_as_a_full_refund(
        monkeypatch, _fast_timeout) -> None:
    """端到端到**资金口径**:超时降级 ⇒ 部分计费,不是整单退。

    这一条把 C-2(c) 与结算合同接起来 —— 只断言 ``total_tests`` 的话,
    「保留了结果」与「保留了结果但结算侧还是按零算」分不开。
    """
    from services.diagnosis_sample_contract import (
        OUTCOME_DEGRADED, billable_points, evaluate_sample,
    )

    _install_engines(monkeypatch, slow_engines={"doubao"})
    out = _run(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一", "问题二"],
        engines=["dashscope", "deepseek", "doubao"],
    ))
    bds = out["brand_detection_summary"]

    # 组装成生产那一侧的 ai_visibility 形状(键名逐字同源)
    av = {
        "engines_tested": out["engines"],
        "total_planned": bds["total_planned"],
        "total_tests": bds["total_tests"],
        "total_failed": bds["total_failed"],
        "engine_stats": {k: {"detected": v["detected"], "total": v["total"], "rate": v["rate"]}
                         for k, v in bds["by_engine"].items()},
    }
    verdict = evaluate_sample(av)
    assert verdict.outcome == OUTCOME_DEGRADED, verdict
    charged = billable_points(verdict, 6000)
    assert 0 < charged < 6000, (charged, verdict)
    assert charged == int(6000 * (4 / 6)), (charged, verdict)


def test_c2_22_everything_timed_out_is_still_a_full_refund(
        monkeypatch, _fast_timeout) -> None:
    """判别力对照:**全都**超时时仍然必须是零成功(不能"保留"出假结果)。

    没有这一条,上一条的"保留了 4 格"有可能是因为收割逻辑把未完成的 Task
    也算成了成功 —— 那比整批丢弃更糟(按没测成的结果收钱)。
    """
    _install_engines(monkeypatch, slow_engines={"dashscope", "deepseek", "doubao"})
    out = _run(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一"],
        engines=["dashscope", "deepseek", "doubao"],
    ))
    bds = out["brand_detection_summary"]
    assert bds["total_tests"] == 0, bds
    assert bds["total_planned"] == 3, bds


async def _drive_and_settle(coro):
    """跑完被测函数,再让出几轮 loop —— ``cancel()`` 只是**请求**。

    取消要等被取消的协程被重新调度才真正投递。收尾之后回一份"还在飞"的清单:
    真被取消的 Task 这时已经 ``done()``,没被取消的还挂在 loop 上。
    """
    out = await coro
    for _ in range(20):
        await asyncio.sleep(0)
    current = asyncio.current_task()
    leftover = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
    return out, [repr(t)[:120] for t in leftover]


def test_c2_24_in_flight_tasks_are_cancelled_not_left_burning(
        monkeypatch, _fast_timeout) -> None:
    """🔴 [外选 EXTC-05] 总超时之后,在飞的 provider 调用必须**真的停下来**。

    这半句是修复说明里自己写下的承诺:「仍在飞的那几格显式 cancel 止损
    (否则它们会在后台继续烧钱,而结果没人消费)」。改动前的四条判据
    只断言 ``total_tests / total_planned / by_engine`` 这几个**计数**,
    而计数在"停了"与"没停、任它在后台跑"两种行为下**完全一样** ——
    止损这半句承诺零判据。

    两条独立的锚,互为纵深:
      ① 被取消的协程自己看到了 ``CancelledError``(它是真的被取消了);
      ② 收尾之后 loop 上**没有**还在飞的 Task(不是"取消请求发了但没生效")。

    ═══════════════════════════════════════════════════════════════════
    🔴 本条**杀不动** MUT-EXTC-05(把 ``t.cancel()`` 换成 ``pass``)—— 如实写明
    ═══════════════════════════════════════════════════════════════════
    不是判据够不到,是那一发**没有可观测差**:止损由**两道冗余**机制完成,
    ``t.cancel()`` 只是第二道。第一道是 ``asyncio.wait_for`` 本身 ——
    它超时时会取消被 await 的那个 gather,并**等取消完成**才抛
    ``TimeoutError``。所以走到 ``except`` 那一支时:

        done       = [True, True]        ← 全部终态
        cancelled()= [True, False]       ← 在飞那个已经是 cancelled
        t.cancel() = [False, False]      ← 返回 False:已终态,无事可做

    (2026-08-26 用同形状的独立小程序实测,不是推断。)

    所以摘掉 ``t.cancel()`` 之后行为逐位不变,任何判据都不可能红 ——
    「补一条判据把它杀掉」在这一发上是做不到的事,只能如实报回。

    那这条判据守什么?守**承诺本身**,而不是守某一行代码:两道机制
    **同时**失效时它才该红,而那正是真实回归的形状(有人把 gather 包进
    ``asyncio.shield``、或换成不取消的 ``asyncio.wait``,再顺手删掉这行
    "看起来没用"的 cancel)。判别力已实测:shield + ``pass`` 双改
    ⇒ 本条红(leftover 里躺着两个 pending 的 ``query_single``),
    而其余五条照绿。
    """
    cancelled: list[str] = []
    _install_engines(monkeypatch, slow_engines={"doubao"},
                     on_cancel=cancelled.append)

    out, leftover = _run(_drive_and_settle(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一", "问题二"],
        engines=["dashscope", "deepseek", "doubao"],
    )))

    bds = out["brand_detection_summary"]
    # 前提:这一跑真的走了超时那一支(否则下面两条守的是另一条路径)
    assert bds["total_tests"] == 4 and bds["total_planned"] == 6, bds

    assert cancelled == ["doubao", "doubao"], (
        f"在飞的 provider 调用没被取消(实得 {cancelled})—— "
        "它们会在后台继续跑、继续产生 API 费用,而结果没有任何消费者")
    assert leftover == [], (
        f"收尾之后 loop 上还挂着在飞的任务:{leftover}")


def test_c2_25_the_cancel_probe_is_alive_not_always_firing(
        monkeypatch, _fast_timeout) -> None:
    """判别力反臂:**没有**在飞任务时,一次取消都不许发生。

    没有这一条,上一条的 ``cancelled == ["doubao","doubao"]`` 有可能只是因为
    收尾时整批被无差别取消(那会连已经成功的格一起打掉)。
    """
    cancelled: list[str] = []
    _install_engines(monkeypatch, slow_engines=set(), on_cancel=cancelled.append)

    out, leftover = _run(_drive_and_settle(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一", "问题二"],
        engines=["dashscope", "deepseek"],
    )))
    assert out["brand_detection_summary"]["total_tests"] == 4, out
    assert cancelled == [], f"没有超时却取消了 {cancelled}"
    assert leftover == [], leftover


def test_c2_23_no_timeout_path_is_unchanged(monkeypatch) -> None:
    """不超时那条路**逐位不变** —— 本包只动 except 那一支。

    🔴 刻意**不**用 ``_fast_timeout``:这一条要跑的是正常路径。
    """
    _install_engines(monkeypatch, slow_engines=set())
    out = _run(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一", "问题二"],
        engines=["dashscope", "deepseek"],
    ))
    bds = out["brand_detection_summary"]
    assert bds["total_planned"] == 4 and bds["total_tests"] == 4, bds
    assert bds["total_failed"] == 0, bds


# ══════════════════════════════════════════════════════════════════════════
# [工单 E3-5 · Codex 二审 P2] 总超时那条日志报的必须是**真留下来的格数**
# ══════════════════════════════════════════════════════════════════════════

def test_e5_01_the_timeout_log_reports_kept_cells_not_terminal_tasks(
        monkeypatch, _fast_timeout, capsys) -> None:
    """显示 4/6,不是 6/6。

    🔴 旧写法 ``done_count = sum(1 for t in tasks if t.done())`` 不是"有时多数
       几格",是**永远**报 N/N:``asyncio.wait_for`` 超时时先 cancel 掉被等的
       gather 并等这次取消落定再抛 TimeoutError,所以走到这里每个 task 都已经
       是终态 —— 而 asyncio 里**被取消的 task 也是 done()**。
       于是这条日志在结构上永远说不出"降级了几格"。
       (本文件上面 ``test_c2_24`` 那段 docstring 里已经用独立小程序实测过:
        done=[True,True] / cancelled()=[True,False]。)

    🔴 这一条只打**计数口径**。结算另有其路(``brand_stats`` →
       total_tests/total_planned/total_failed → ``evaluate_sample``),
       由 c2_20 / c2_21 守,本条一个字都不碰它们。
    """
    _install_engines(monkeypatch, slow_engines={"doubao"})

    out = _run(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一", "问题二"],
        engines=["dashscope", "deepseek", "doubao"],
    ))
    printed = capsys.readouterr().out

    # 前提自证:这一跑真的走了超时那一支(否则下面在对一句没打印的话断言)。
    assert "总超时" in printed, f"没有走到总超时分支,判据在对空气说话:\n{printed}"
    # 前提自证②:真的**有**格被丢掉 —— 6/6 与 4/6 只有在有降级时才分得开。
    bds = out["brand_detection_summary"]
    assert bds["total_planned"] == 6 and bds["total_tests"] == 4, bds

    assert "保留已完成的 4/6 格结果" in printed, (
        "总超时日志报的不是真正留下来的格数。\n"
        f"实际打印:{printed.strip()}\n"
        "(被取消的 task 也是 done() —— 数 done() 会永远报满格)")
    assert "保留已完成的 6/6 格结果" not in printed, (
        "还在按 done() 计数 —— 显示满格而实际只留下 4 格")


def test_e5_02_the_kept_count_matches_the_settlement_count(
        monkeypatch, _fast_timeout, capsys) -> None:
    """判别力自证:日志里那个数必须**跟着**真实履约数走,不是写死的 4。

    换一个慢引擎组合(两个引擎慢 ⇒ 只留 2 格),日志必须跟着变成 2/6。
    没有这一条,把那句话硬编码成 "4/6" 也能让上一条全绿。
    """
    _install_engines(monkeypatch, slow_engines={"doubao", "deepseek"})

    out = _run(ai_tester.detailed_ai_visibility_test(
        check_brand="工单C判据品牌",
        questions=["问题一", "问题二"],
        engines=["dashscope", "deepseek", "doubao"],
    ))
    printed = capsys.readouterr().out
    bds = out["brand_detection_summary"]

    assert "总超时" in printed, printed
    assert bds["total_tests"] == 2, bds
    assert f"保留已完成的 {bds['total_tests']}/6 格结果" in printed, (
        f"日志数与结算数对不上:结算 {bds['total_tests']},日志 {printed.strip()}")
