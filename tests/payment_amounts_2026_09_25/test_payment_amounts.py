# -*- coding: utf-8 -*-
"""WO_299 · 支付渠道元 → 分精确换算(微信 Native / H5 / JSAPI / 退款 + 虎皮椒)。

事故形态:`int(total_yuan * 100)` 在浮点下截断,`1.15 → 114` 分;回调核「实付 == 应付」⇒ 付了钱不入账。
判据分三层:
  ① 助手本身:Review 点名的六个金额经 float / Decimal / str 三种入口都精确;0–1000 元每一分做往返;非法金额一律拒绝;
  ② 五个下单 / 退款函数真正发给渠道的金额(打桩截获请求体,不连网、不需要密钥);
  ③ 源码里不许再出现 `* 100` 的就地换算(防止下一个人又写回去)。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
from decimal import Decimal

import pytest

from services.payment_amounts import fen_to_yuan_str, yuan_to_fen

REPO = pathlib.Path(__file__).resolve().parents[2]
CASES = [("0.29", 29), ("1.15", 115), ("2.01", 201), ("9.95", 995), ("19.99", 1999), ("100", 10000)]


@pytest.mark.parametrize("text,fen", CASES)
def test_named_amounts_are_exact_from_every_input_type(text, fen):
    assert yuan_to_fen(float(text)) == fen
    assert yuan_to_fen(Decimal(text)) == fen
    assert yuan_to_fen(text) == fen
    assert yuan_to_fen(fen / 100) == fen                      # 调用方常见写法:cents / 100
    assert yuan_to_fen(Decimal(fen) / Decimal(100)) == fen    # 另一种:Decimal(cents) / 100


def test_every_cent_round_trips_up_to_1000_yuan():
    bad = [c for c in range(0, 100001) if yuan_to_fen(c / 100) != c or yuan_to_fen(fen_to_yuan_str(c)) != c]
    assert bad == [], bad[:10]


def test_the_old_truncation_really_was_wrong():
    """对照:不是凭空修 —— 旧写法在这些金额上确实少一分。"""
    wrong = [t for t, f in CASES if int(float(t) * 100) != f]
    assert {"0.29", "1.15"} <= set(wrong), wrong


@pytest.mark.parametrize("bad", [0.001, "1.005", -1, "-0.01", float("nan"), float("inf"), True, "abc", None, 0.1 + 0.2])
def test_non_two_decimal_amounts_are_rejected(bad):
    with pytest.raises(ValueError):
        yuan_to_fen(bad)


def test_fen_to_yuan_str():
    assert [fen_to_yuan_str(x) for x in (0, 1, 29, 115, 10000)] == ["0.00", "0.01", "0.29", "1.15", "100.00"]
    for bad in (-1, 1.5, True, "1"):
        with pytest.raises(ValueError):
            fen_to_yuan_str(bad)


# ---------------------------------------------------------------- ② 渠道真正收到的金额

class _Resp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"prepay_id": "wx-prepay-test", "code_url": "weixin://test", "h5_url": "https://test"}


def _stub_wechat(monkeypatch):
    import services.wechat_pay as wp

    sent = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, content=None, headers=None):
            sent.append(json.loads(content))
            return _Resp()

    monkeypatch.setattr(wp, "_build_auth_header", lambda *a, **k: "stub")
    monkeypatch.setattr(wp.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(wp, "APPID", "wx-test")
    return wp, sent


@pytest.mark.parametrize("text,fen", CASES)
def test_wechat_native_h5_jsapi_send_exact_fen(monkeypatch, text, fen):
    wp, sent = _stub_wechat(monkeypatch)
    y = float(text)
    asyncio.run(wp.create_native_order(out_trade_no="t1", total_yuan=y, description="d"))
    h5 = wp.create_h5_order
    import inspect
    h5_kwargs = {"out_trade_no": "t2", "total_yuan": y, "description": "d"}
    if "payer_ip" in inspect.signature(h5).parameters:
        h5_kwargs["payer_ip"] = "127.0.0.1"
    asyncio.run(h5(**h5_kwargs))
    asyncio.run(wp.create_jsapi_order(out_trade_no="t3", total_yuan=y, openid="o", description="d"))
    assert [s["amount"]["total"] for s in sent] == [fen, fen, fen]


@pytest.mark.parametrize("text,fen", CASES)
def test_wechat_refund_sends_exact_fen(monkeypatch, text, fen):
    wp, sent = _stub_wechat(monkeypatch)
    asyncio.run(wp.refund_order(out_trade_no="t", out_refund_no="r", refund_yuan=float(text), total_yuan=float(text)))
    assert sent[0]["amount"]["refund"] == fen and sent[0]["amount"]["total"] == fen


@pytest.mark.parametrize("text,fen", CASES)
def test_xunhupay_sends_exact_two_decimal_string(monkeypatch, text, fen):
    import services.xunhupay as xp

    sent = []

    class _R:
        async def text(self):
            return '{"errcode": 0}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class _S:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, data=None):
            sent.append(dict(data))
            return _R()

    monkeypatch.setattr(xp, "XUNHUPAY_APPID", "a")
    monkeypatch.setattr(xp, "XUNHUPAY_APPSECRET", "s")
    monkeypatch.setattr(xp, "verify_xunhupay_callback", lambda d: True)
    monkeypatch.setattr(xp.aiohttp, "ClientSession", _S)
    asyncio.run(xp.create_xunhupay_order(order_id="o", amount_yuan=float(text), title="t"))
    assert sent[0]["total_fee"] == fen_to_yuan_str(fen)


def test_channel_rejects_a_three_decimal_amount_before_calling_out(monkeypatch):
    wp, sent = _stub_wechat(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(wp.create_native_order(out_trade_no="t", total_yuan=1.005, description="d"))
    assert sent == [], "金额不合法时不许发出任何请求"


# ---------------------------------------------------------------- ③ 不许再写回就地换算

_INLINE = re.compile(r"(total_yuan|refund_yuan|amount_yuan)\s*\*\s*100|:\.2f}")


def inline_conversion_sites(source: str) -> list:
    return [i for i, line in enumerate(source.splitlines(), 1)
            if _INLINE.search(line.split("#", 1)[0])]


def test_no_inline_yuan_to_fen_left_in_the_channel_modules():
    for rel in ("services/wechat_pay.py", "services/xunhupay.py"):
        assert inline_conversion_sites((REPO / rel).read_text(encoding="utf-8")) == [], rel


def test_inline_scanner_has_teeth():
    assert inline_conversion_sites("    total_fen = int(total_yuan * 100)\n") == [1]
    assert inline_conversion_sites('    "total_fee": f"{amount_yuan:.2f}",\n') == [1]
    assert inline_conversion_sites("    total_fen = yuan_to_fen(total_yuan)  # 原来是 total_yuan * 100\n") == []   # 注释不算


# ---------------------------------------------------------------- WO_299b · 另两处同型截断

_FLOAT_TRUNC = re.compile(r"int\(\s*float\([^()]*(\([^()]*\))?[^()]*\)\s*\*\s*100\s*\)")
WO_299B_FILES = ("services/service_fee_calculator.py", "db/refund_work_order_db.py")


def float_truncation_sites(source: str) -> list:
    return [i for i, line in enumerate(source.splitlines(), 1)
            if _FLOAT_TRUNC.search(line.split("#", 1)[0])]


def test_no_float_truncation_left_in_the_two_money_paths():
    for rel in WO_299B_FILES:
        assert float_truncation_sites((REPO / rel).read_text(encoding="utf-8")) == [], rel


def test_float_truncation_scanner_has_teeth():
    assert float_truncation_sites('    x = int(float(order.get("paid_amount_yuan") or 0) * 100),\n') == [1]
    assert float_truncation_sites('    y = int(float(pc.get("amount_yuan") or 0) * 100)\n') == [1]
    assert float_truncation_sites("    z = int(round(float(v) * 100))\n") == []          # 对照:round 那几处 Review 定不动
    assert float_truncation_sites("    # 原来是 int(float(v) * 100)\n") == []             # 注释不算


def test_service_fee_paid_cents_prefers_integer_cents_and_converts_yuan_exactly():
    from services.service_fee_calculator import _order_paid_cents

    assert _order_paid_cents({"paid_amount_cents": 115, "paid_amount_yuan": 1.15}) == 115
    assert _order_paid_cents({"paid_amount_yuan": 1.15}) == 115           # 旧写法这里是 114
    assert _order_paid_cents({"paid_amount_yuan": 0.29}) == 29
    assert _order_paid_cents({}) == 0
    for bad in ({"paid_amount_cents": True}, {"paid_amount_cents": -1}, {"paid_amount_cents": 1.5},
                {"paid_amount_yuan": 1.005}):
        with pytest.raises(ValueError):
            _order_paid_cents(bad)


class _FakeCur:
    """按 SQL 关键字应答;agent_revenue_ledger 那条抛(模拟第一条来源不可用),pending_commissions 返给定金额。"""

    def __init__(self, pc_amount):
        self.pc_amount = pc_amount
        self.last = ""

    def execute(self, sql, params=None):
        self.last = " ".join(str(sql).split())
        if "FROM agent_revenue_ledger" in self.last:
            raise RuntimeError("ledger unavailable")

    def fetchone(self):
        from datetime import datetime
        s = self.last
        if "FROM recharge_orders" in s:
            return {"id": "o1", "user_id": 7, "amount_cents": 11500, "base_points": 100, "bonus_points": 0,
                    "paid_at": None, "refund_status": None, "payment_status": "paid"}
        if "FROM pending_commissions" in s:
            return {"amount_yuan": self.pc_amount, "has_settled": False}
        if "now()" in s:
            return {"now": datetime(2026, 9, 25)}
        return {"credit_total": 0, "paid_points": 0, "bonus_points": 0}


def _preview_with(monkeypatch, pc_amount):
    import db.refund_work_order_db as m

    class _Conn:
        def cursor(self):
            return _FakeCur(pc_amount)

        def close(self):
            pass

    monkeypatch.setattr(m, "get_connection", lambda: _Conn())
    return m.build_refund_order_preview("o1")


def test_refund_preview_fallback_converts_commission_exactly(monkeypatch):
    from decimal import Decimal as D

    assert _preview_with(monkeypatch, D("1.15"))["impact"]["agent_revenue_reversal_cents"] == 115   # 旧写法 114
    assert _preview_with(monkeypatch, D("0.29"))["impact"]["agent_revenue_reversal_cents"] == 29


def test_refund_preview_fallback_does_not_swallow_a_bad_amount_as_zero(monkeypatch):
    from decimal import Decimal as D

    with pytest.raises(ValueError):
        _preview_with(monkeypatch, D("1.155"))
