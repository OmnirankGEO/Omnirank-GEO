"""[BUG-P3] 遗留通用支付回调 /recharge/callback 无金额校验(免费入账后门)· 静态守护

根因:payment_callback 仅比对 X-Callback-Secret 后直接 complete_recharge 入账,无金额/订单态/
真实支付凭证校验(对比 /wechat-callback、/xunhupay-callback 经 _verify_callback_consistency
三重校验)。密钥泄露/误配即可把任意 pending 订单刷成已支付并凭空入账。
修:prod SECRET 空已恒 500 不可用 → 端点恒 410 下线,消除后门。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_callback_disabled():
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    s = src.find("async def payment_callback")
    assert s >= 0
    func = src[s: src.find("async def manual_recharge", s)]
    assert "410" in func, "遗留通用回调须恒拒绝下线(消除免费入账后门)"
    assert "complete_recharge(" not in func, "下线后不得再无金额校验调用 complete_recharge() 入账"
