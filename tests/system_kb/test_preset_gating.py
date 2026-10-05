"""Task 4 · preset/canned 上下文门控测试

规则:带上下文(current_page 或 attachment_text)的提问不允许 preset/canned 直接短路,
必须走完整 RAG。无上下文的纯问答保持原有 canned/preset 兜底。
"""

from api.xiaobang_api import _should_short_circuit_canned


class _Req:
    def __init__(self, message, current_page="", attachment_text=None):
        self.message = message
        self.current_page = current_page
        self.attachment_text = attachment_text


def test_canned_short_circuits_without_context():
    assert _should_short_circuit_canned(_Req("怎么改密码")) is True


def test_canned_gated_with_current_page():
    assert _should_short_circuit_canned(_Req("这个怎么填", current_page="/pricing")) is False


def test_canned_gated_with_attachment():
    assert _should_short_circuit_canned(_Req("这是什么", attachment_text="报价页截图…")) is False
