"""§13 告警合同 rollout —— D 组剩余三处低价值面的七字段合同判别。

Master SSOT §13 / 手册 §3.6:任何用户可见告警必须带机器合同(7 字段)+ 至少一个
合法下一步动作。只有红色错误码没有下一步一律 NO-GO(事故#8)。

本文件锁死本轮补齐的三处:

| 处 | 旧形态 | 新形态 |
|---|---|---|
| `advisor_api` 非流式对话(空回复 / 兜底异常) | `{"success":False,"error":str(e)}` 裸串 | `advisor_chat_failed_alert` 七字段 + 重试/联系客服 |
| `content_api` /chat 流(corpus/写稿)兜底异常 | `{'type':'error','text':str(e)}` 裸串 | `content_stream_failed_alert("write")` 七字段 |
| `content_api` 扣费兜底 402 ×2 | `HTTPException(402, detail=str(e))` | `_content_billing_error_alert()` 七字段 |

判别口径:`is_alert_contract(payload)` 为真 **且** 至少一个 action;并断言异常原文
(`str(e)`/异常类名)不出现在用户可见文案(feedback_no_supplier_names_to_users)。
本轮只给已经失败的路径补出口,**不新增任何硬阻断**(§1.6 第一解释原则)。
"""
import inspect

import pytest

from services.governance_contract import is_alert_contract, CONTRACT_KEYS


LEAKY = "ConnectionResetError: upstream dashscope qwen3-max 502 at 10.0.0.7"


def _assert_governed(payload: dict, *, leaked: str = LEAKY):
    assert is_alert_contract(payload), f"不是 §13 合同: {payload}"
    for k in CONTRACT_KEYS:
        assert k in payload, f"缺字段 {k}"
    assert payload["actions"], "红码无下一步(事故#8)"
    for a in payload["actions"]:
        assert a.get("id") and a.get("label")
    # 异常原文/供应商名不得进用户可见文案。
    for field in ("message", "reason", "impact", "repair_hint"):
        val = str(payload.get(field) or "")
        assert leaked not in val
        assert "dashscope" not in val.lower()
        assert "Error" not in val


class TestContentBillingAlert:
    def test_content_billing_error_alert_is_governed(self):
        from api.content_api import _content_billing_error_alert
        _assert_governed(_content_billing_error_alert())

    def test_states_no_charge_taken(self):
        """扣费失败必须明确告知"未扣费",否则用户以为钱没了(事故#8 变体)。"""
        from api.content_api import _content_billing_error_alert
        a = _content_billing_error_alert()
        assert "未扣费" in a["impact"]

    def test_offers_retry_and_support(self):
        from api.content_api import _content_billing_error_alert
        ids = {x["id"] for x in _content_billing_error_alert()["actions"]}
        assert "retry" in ids
        assert "contact_support" in ids

    def test_both_402_sites_use_contract_not_str_e(self):
        """两处 402 兜底不得再把 str(e) 塞给用户。"""
        import api.content_api as capi
        src = inspect.getsource(capi)
        assert "raise HTTPException(status_code=402, detail=str(e))" not in src, \
            "仍有 402 直接暴露 str(e)(裸错误码无出口)"
        assert src.count("detail=_content_billing_error_alert()") == 2


class TestAdvisorNonStreamAlert:
    def test_chat_failed_alert_is_governed(self):
        from services.governance_alerts import advisor_chat_failed_alert
        _assert_governed(advisor_chat_failed_alert(LEAKY))

class TestContentStreamAlert:
    def test_write_stream_alert_is_governed(self):
        from services.governance_alerts import content_stream_failed_alert
        _assert_governed(content_stream_failed_alert("write", LEAKY))

    def test_chat_stream_no_longer_yields_bare_str_e(self):
        import api.content_api as capi
        src = inspect.getsource(capi)
        assert "'type': 'error', 'text': str(e)" not in src, \
            "/chat 流兜底仍把 str(e) 直接吐给用户"

    def test_all_three_kinds_governed(self):
        from services.governance_alerts import content_stream_failed_alert
        for kind in ("write", "topics", "corpus"):
            _assert_governed(content_stream_failed_alert(kind, LEAKY))


class TestNoNewHardBlock:
    """本轮只补出口,不得引入新的 fail-closed(§1.6 / 红线)。"""

    @pytest.mark.parametrize("builder_name", [
        "_content_billing_error_alert",
    ])
    def test_builders_are_pure_and_side_effect_free(self, builder_name):
        import api.content_api as capi
        fn = getattr(capi, builder_name)
        a, b = fn(), fn()
        assert a == b, "告警 builder 必须是纯函数(可重复构造)"

    def test_dead_end_alert_is_rejected_at_construction(self):
        """无动作的告警必须构造即拒(死胡同不可能上线)。"""
        from services.governance_contract import build_alert, AlertContractError
        with pytest.raises(AlertContractError):
            build_alert("X", "m", reason="r", impact="i", actions=[])
