"""[V3.5 v7 批1D · 2026-06-08] 提现 fees 透明三段单测(mock · 不依赖真 DB)

覆盖:
- calc_withdrawal_fees 守恒(net = gross − 总扣费 · platform = gateway + settlement)
- W2 铁律:返回【绝不含 *_bps】(Agent 路径禁暴露费率)
- gross<=0 短路全 0(不调费率)
- fee 整数 floor(对服务商友好 · 差 <1 分)
- 税率走 per-agent(get_agent_tax_rate_bps · agent_tax_profiles override)
- create_settlement_request 写 fees 审计列 + 返回透明三段
- net<=0(费率配置异常)raise 让主事务 rollback

真 DB 集成(实际申请落库 + admin 按 net 打款)由 Deploy 在 staging 验证。
"""
import pytest


def _patch_fee_rates(monkeypatch, gateway=90, settlement=190, tax=600, tax_capture=None):
    import services.agent_pricing as ap
    monkeypatch.setattr(ap, "get_gateway_fee_bps", lambda pm="wechat_pay": gateway)
    monkeypatch.setattr(ap, "get_settlement_service_fee_bps", lambda: settlement)

    def _tax(cursor, agent_user_id):
        if tax_capture is not None:
            tax_capture["agent"] = agent_user_id
        return tax
    monkeypatch.setattr(ap, "get_agent_tax_rate_bps", _tax)
    return ap


# ---------- calc_withdrawal_fees(纯函数) ----------

def test_withdrawal_fees_conservation(monkeypatch):
    ap = _patch_fee_rates(monkeypatch)
    fees = ap.calc_withdrawal_fees(None, 28, 10000)  # ¥100
    assert fees["gateway_fee_cents"] == 90
    assert fees["settlement_fee_cents"] == 190
    assert fees["platform_fee_cents"] == 280
    assert fees["tax_cents"] == 600
    assert fees["total_fee_cents"] == 880
    assert fees["net_cents"] == 9120              # 提 ¥100 · 扣 ¥8.8 · 到账 ¥91.2(对齐 directive 例子)
    # 守恒 + 合并
    assert fees["net_cents"] == fees["gross_cents"] - fees["total_fee_cents"]
    assert fees["platform_fee_cents"] == fees["gateway_fee_cents"] + fees["settlement_fee_cents"]
    assert fees["total_fee_cents"] == fees["platform_fee_cents"] + fees["tax_cents"]


def test_withdrawal_fees_no_bps_leak(monkeypatch):
    """W2 铁律:返回 dict 绝不含任何 *_bps key(Agent 路径禁暴露费率结构)。"""
    ap = _patch_fee_rates(monkeypatch)
    fees = ap.calc_withdrawal_fees(None, 28, 10000)
    assert not any("bps" in k for k in fees.keys()), f"泄露 bps key: {list(fees.keys())}"


def test_withdrawal_fees_zero_and_negative(monkeypatch):
    ap = _patch_fee_rates(monkeypatch)
    assert all(v == 0 for v in ap.calc_withdrawal_fees(None, 28, 0).values())
    assert all(v == 0 for v in ap.calc_withdrawal_fees(None, 28, -100).values())


def test_withdrawal_fees_floor(monkeypatch):
    """fee 整数 floor · net = gross − 三项 floor(对服务商友好 · 平台少收 <1 分)。"""
    ap = _patch_fee_rates(monkeypatch)
    # gross=333(¥3.33):gateway 333*90//10000=2 · settlement 333*190//10000=6 · tax 333*600//10000=19
    fees = ap.calc_withdrawal_fees(None, 28, 333)
    assert fees["gateway_fee_cents"] == 2
    assert fees["settlement_fee_cents"] == 6
    assert fees["tax_cents"] == 19
    assert fees["net_cents"] == 333 - 2 - 6 - 19   # 306


def test_withdrawal_fees_per_agent_tax(monkeypatch):
    """税率走 per-agent(get_agent_tax_rate_bps 被以正确 agent 调用)。"""
    cap = {}
    ap = _patch_fee_rates(monkeypatch, tax=300, tax_capture=cap)
    fees = ap.calc_withdrawal_fees(None, 99, 10000)
    assert cap["agent"] == 99
    assert fees["tax_cents"] == 300                 # 该 agent 特殊税率 3%


# ---------- create_settlement_request 写 fees ----------

class _SettleCursor:
    def __init__(self, ledger_rows):
        self._ledger_rows = ledger_rows
        self._mode = None
        self.insert_request_params = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if "INSERT INTO agent_settlement_requests" in s:
            self.insert_request_params = params
            self._mode = "new_req"
        elif "FROM agent_revenue_ledger" in s and "FOR UPDATE" in s:
            self._mode = "ledger"
        else:
            self._mode = None

    def fetchone(self):
        return {"id": 555} if self._mode == "new_req" else None

    def fetchall(self):
        return self._ledger_rows if self._mode == "ledger" else []


def _patch_create_deps(monkeypatch, available_cents, fees):
    import services.agent_revenue as ar
    import services.agent_pricing as ap
    monkeypatch.setattr(ar, "get_agent_balance", lambda c, a: {"available_cents": available_cents})
    monkeypatch.setattr(ap, "calc_withdrawal_fees", lambda c, a, g: fees)


def test_create_settlement_writes_fees(monkeypatch):
    from services.agent_revenue import create_settlement_request
    fees = {
        "gross_cents": 10000, "gateway_fee_cents": 90, "settlement_fee_cents": 190,
        "platform_fee_cents": 280, "tax_cents": 600, "total_fee_cents": 880, "net_cents": 9120,
    }
    _patch_create_deps(monkeypatch, available_cents=100000, fees=fees)
    cur = _SettleCursor([{"id": 1, "agent_settlement_cents": 100000, "already_locked": 0}])
    result = create_settlement_request(cur, 28, 10000, "招行", "6222000011112222", "张三")

    # INSERT request 末 4 参 = gateway / settlement / tax / net(审计列)
    params = cur.insert_request_params
    assert params[-4] == 90
    assert params[-3] == 190
    assert params[-2] == 600
    assert params[-1] == 9120
    # 返回透明三段(金额 · 不露 bps)
    assert result["net_cents"] == 9120
    assert result["total_fee_cents"] == 880
    assert result["platform_fee_cents"] == 280
    assert result["tax_cents"] == 600
    assert "bps" not in str(result.keys())
    # W2 铁律:分项 gateway/settlement 金额【绝不进 Agent 返回】(只进 DB 审计列)
    # 显式断言分项缺席(bps 子串检查挡不住分项 key 回归 · 分项 key 不含 "bps")
    assert "gateway_fee_cents" not in result
    assert "settlement_fee_cents" not in result


def test_create_settlement_net_zero_raises(monkeypatch):
    """费率配置异常(总扣费 >= 提现额 · net<=0)→ raise · 不留半截申请。"""
    from services.agent_revenue import create_settlement_request
    fees = {
        "gross_cents": 10000, "gateway_fee_cents": 0, "settlement_fee_cents": 0,
        "platform_fee_cents": 0, "tax_cents": 10000, "total_fee_cents": 10000, "net_cents": 0,
    }
    _patch_create_deps(monkeypatch, available_cents=100000, fees=fees)
    with pytest.raises(ValueError, match="配置异常"):
        create_settlement_request(_SettleCursor([]), 28, 10000, "招行", "6222", "张三")


def test_create_settlement_insufficient_available_raises(monkeypatch):
    """available 不足(get_agent_balance · 已含换算力锁 SSOT)→ raise(防双花前置)。"""
    from services.agent_revenue import create_settlement_request
    _patch_create_deps(monkeypatch, available_cents=5000, fees={})
    with pytest.raises(ValueError, match="不足"):
        create_settlement_request(_SettleCursor([]), 28, 10000, "招行", "6222", "张三")
