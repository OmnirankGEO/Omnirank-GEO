"""[BUG-P2] compute_unspent_from_order 跨订单扣减消费 → 多订单客户退后单时少撤额度(双拿)· 静态守护

根因:算某订单未消费时,把"该订单首笔 allocate 时间之后的所有 consume(不论属哪一单)"整体从本订单
划拨量里扣。退较晚订单 Y 时实际可能扣的是更早订单 X 的消费(FIFO 下应先扣 X),导致 Y 的 unspent
被低估 → revoke 少撤 → 客户保留本应撤回的额度,同时仍拿现金退款=双拿。单订单无此问题。
修:真 FIFO 分账 — 本订单 allocate 之前的其他订单先吸收 consume,剩余 consume 才落到本订单,clamp 到本订单 allocate。
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_compute_unspent_fifo_prior_alloc():
    src = (ROOT / "services" / "customer_credit.py").read_text(encoding="utf-8")
    s = src.find("def compute_unspent_from_order")
    assert s >= 0
    e = src.find("\ndef ", s + 10)
    func = src[s: e if e > 0 else s + 3000]
    assert "prior_alloc" in func, "须 FIFO:本订单之前的 allocate 先吸收 consume"
    assert "created_at < %s" in func, "prior_alloc 须限定本订单 allocate 时间之前"
    # 不再用"first_at 之后所有 consume 全算本订单"的旧口径
    assert "created_at >= %s" not in func, "仍用旧跨订单口径 → 退较晚订单少撤双拿"
