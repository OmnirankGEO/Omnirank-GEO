"""【P0-3c · R1-C】把 P0-3b 那发 NameError 的场景**钉死**。

## 钉的是什么

2026-08-25 生产实证:P0-3b 把 `_dr.settlement_signals_from_result(result)` 加进
`_run_diagnosis_impl` 的完成 payload,而**那个函数从来不 import `_dr`** ⇒
每一单走到完成 payload 的诊断都 `NameError: name '_dr' is not defined`,
四引擎跑完、报告已入库,却在 99% 处整单转失败并退款。
92 条判据 + 27 发变异全绿放行,因为**没有一条真的执行过 server.py**。

所以这一条判据必须:

1. 真的 `await server._run_diagnosis_impl(...)`(不是调 services 里那个纯函数);
2. 断言两条结算信号**带着真值**进了 payload(不是断言键名出现过);
3. 断言这一单**没有**因为 NameError 掉进 except 分支变成失败态。

## 夹具形状不是我手写的

`result` 里的两条信号由**真生产者**产出:
`evaluate_delivery_verdict`(workflows.diagnosis_workflow)与
`assess_identity_suspicion`(services.diagnosis_identity_suspicion)——
生产者改形状,夹具跟着改,不会出现"夹具供了生产不会供的形状"。
"""
from __future__ import annotations

import asyncio

import pytest


def production_shaped_result(*, brand_name="P03C客户", planned=32, succeeded=32):
    """照 workflow 的真实装配路径造 `result`。

    workflow 把两条信号挂在:
      `results["data"]["ai_visibility"]["identity_suspicion"]`  (diagnosis_workflow.py:2164)
      `results["data"]["delivery_verdict"]`                     (diagnosis_workflow.py:2317)
    —— 而 `settlement_signals_from_result` 正是从这两处读。写错任一层包装,
    两条信号就都是 None,判据会因为**错误的原因**"看起来没问题"。
    """
    from services.diagnosis_identity_suspicion import assess_identity_suspicion
    from workflows.diagnosis_workflow import evaluate_delivery_verdict

    ai_visibility = {
        "engines_tested": ["qwen", "deepseek", "kimi", "doubao"],
        "total_tests": succeeded,
        "total_planned": planned,
        "total_failed": planned - succeeded,
        "summary": {"total_planned": planned, "total_tests": succeeded,
                    "total_failed": planned - succeeded},
        "detected_count": 12,
        "engine_stats": {},
    }
    suspicion = assess_identity_suspicion(
        brand_name=brand_name,
        dimension_stats={"brand": {"detected": 12, "total": 16}},
        detail_table=None)
    ai_visibility["identity_suspicion"] = suspicion

    _v = evaluate_delivery_verdict(ai_visibility)
    verdict = {
        "version": _v.version, "outcome": _v.outcome,
        "planned": _v.planned, "succeeded": _v.succeeded,
        "coverage_ratio": round(_v.coverage_ratio, 4),
        "billable_ratio": round(_v.billable_ratio, 4),
        "failed_platforms": list(_v.failed_platforms),
        "message": _v.message,
    }
    return {
        "data": {"ai_visibility": ai_visibility, "delivery_verdict": verdict},
        "scores": {"total_score": 61, "level": "成长级"},
        "report": {"markdown": "# 报告\n正文"},
    }, verdict, suspicion


class _Silent:
    def __init__(self, real):
        self._real = real
        self.sent = []

    async def send_message(self, sid, payload):
        self.sent.append(payload)

    def clear_task(self, sid):
        pass

    def _set_status(self, sid, payload):
        pass

    def __getattr__(self, k):
        return getattr(self._real, k)


def _run_impl(server, monkeypatch, result):
    """真跑 `_run_diagnosis_impl`,只把上游那一步(四引擎 workflow · 真跑要花钱)换掉。

    `run_diagnosis_workflow` 是 server.py:27 的**模块级** import,
    所以换模块属性就能在 :3138 的调用点生效 —— 被测的 payload 构造段一行没被替。
    """
    async def _fake_workflow(**kw):
        return result

    monkeypatch.setattr(server, "run_diagnosis_workflow", _fake_workflow)
    monkeypatch.setattr(server, "manager", _Silent(server.manager))
    req = server.DiagnosisRequest(brand_name="P03C客户", industry="测试行业", keywords=["词"])
    sid = "regr_" + __import__("uuid").uuid4().hex[:10]
    return asyncio.run(server._run_diagnosis_impl(req, sid, creator_user_id=None))


def test_completion_payload_really_runs_and_carries_both_signal_values(live_server, monkeypatch):
    """必须命中:payload 真的构造出来,两条信号**带真值**进去。

    「符号改成未定义名」的执行毒(把 `_run_diagnosis_impl` 里那句 `from services import
    diagnosis_runs as _dr` 删掉 / 把 `_dr.` 改成未定义名)会让这一条直接红 ——
    那正是 2026-08-25 上线炸的那一发。
    """
    result, verdict, suspicion = production_shaped_result()
    payload = _run_impl(live_server, monkeypatch, result)

    assert isinstance(payload, dict), payload
    # ① 没掉进 except:NameError 会让 impl 返回 error payload 而不是 complete
    assert payload.get("type") == "complete", (
        "完成 payload 没跑出来(多半是调用点抛了异常被 except 兜成 error):%r" % payload)
    assert payload.get("error") in (None, False), payload

    # ② 两条信号**带真值**,不是"键在值 None"
    assert payload["delivery_verdict"] == verdict, payload.get("delivery_verdict")
    assert payload["identity_suspicion"] == suspicion, payload.get("identity_suspicion")
    assert payload["delivery_verdict"] is not None
    assert payload["identity_suspicion"] is not None


def test_paired_must_not_hit_absent_signals_stay_none_but_keys_remain(live_server, monkeypatch):
    """配对的必须不命中:workflow 没算这两条时,键仍在、值为 None,且**照样不炸**。

    没有这一条,上面那条可能是靠"任何 result 都能出值"混绿的。
    """
    payload = _run_impl(live_server, monkeypatch, {"data": {}, "scores": {}})
    assert payload.get("type") == "complete", payload
    assert "delivery_verdict" in payload and payload["delivery_verdict"] is None
    assert "identity_suspicion" in payload and payload["identity_suspicion"] is None


def test_the_impl_really_binds_dr_at_runtime_not_just_in_source(live_server):
    """直指 2026-08-25 那个真因:`_dr` 必须在 `_run_diagnosis_impl` 的**运行时**可绑定。

    P0-3b 的锁只证明了 `_dr.settlement_signals_from_result(result)` 这行文本在源码里,
    证明不了 `_dr` 在那个函数里绑得上。这里直接查函数体内有没有那次 import ——
    并且上面两条判据是**真执行**过的,所以即使这条被绕过,执行毒也逃不掉。
    """
    import ast
    import inspect
    src = inspect.getsource(live_server._run_diagnosis_impl)
    tree = ast.parse(src.lstrip())
    bound = [n for n in ast.walk(tree)
             if isinstance(n, ast.ImportFrom) and n.module == "services"
             and any(a.asname == "_dr" for a in n.names)]
    assert bound, (
        "`_run_diagnosis_impl` 函数体内没有 `from services import diagnosis_runs as _dr` —— "
        "server.py 模块级也从来没绑过 `_dr`(只有 run_diagnosis_task / start_diagnosis "
        "两个函数内部有),所以完成 payload 一跑就是 NameError,每单诊断必挂")
