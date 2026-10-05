"""[BUG-P3] per-agent 出厂折扣在线上 SKU 结算不生效 · 静态守护 + 公式验证

根因:V3.5 SKU 下单 snapshot 锁的是 sku_templates.wholesale_cents(全局出厂价);settlement_orchestrator
结算用该 locked 值算 factory_cents → margin。有 per-agent 出厂折扣 override(agent_pricing_overrides)
的服务商,其真实出厂成本应按 per-agent 折扣算,但全局值架空了 override → margin 算偏。
修:① 下单写 snapshot 时,有 per-agent override 才按 calc_factory_cents(points, *override) 重算锁价(无 override
   用模板全局值·零回归);② orchestrator 回落实时算分支同样:有 override 才按 per-agent 算(无则保持全局)。
prod 实证(2026-06-10 SSH):agent_pricing_overrides 0 行(per-agent 出厂折扣未配置)→ bug latent · 纯预防。
(agent_sku_overrides 46 行是零售价 override · 非出厂折扣 · 与本 bug 无关。)
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_locks_per_agent_wholesale():
    src = (ROOT / "api" / "wallet_api.py").read_text(encoding="utf-8")
    assert "get_agent_wholesale_override(bound_agent_id_for_v35)" in src, \
        "下单 snapshot 须按 per-agent 出厂折扣 override 锁价"
    assert "_locked_wholesale_cents = calc_factory_cents(base_points, *_ov_ratio)" in src, \
        "有 override 才按 per-agent calc_factory_cents 重算"
    assert '"wholesale_cents": _locked_wholesale_cents,' in src, \
        "snapshot 写锁定后的 per-agent wholesale_cents"


def test_orchestrator_fallback_uses_per_agent():
    src = (ROOT / "services" / "settlement_orchestrator.py").read_text(encoding="utf-8")
    # WORKERS=4 整合后，充值与争议裁决共用 core 结算器；回退断言必须锚到
    # SSOT 函数，不能继续截取旧调用点附近的固定 1400 字符窗口。
    s = src.find("def record_v35_core_settlement(")
    assert s >= 0
    e = src.find("\ndef ", s + 1)
    seg = src[s:e if e >= 0 else None]
    assert "get_agent_wholesale_override(agent_user_id)" in seg, \
        "回落实时算分支须先判 per-agent override(无则保持全局·零回归)"
    assert "calc_factory_cents(int(settle_points_granted), *get_agent_wholesale_ratio(agent_user_id))" in seg, \
        "有 override 才按 per-agent 出厂折扣实时算 factory"


def test_calc_factory_cents_per_agent_differs_from_global():
    """证伪:per-agent 折扣与全局不同 → 用全局必算偏 margin。"""
    from services.agent_pricing import calc_factory_cents
    pts = 13000
    global_factory = calc_factory_cents(pts, 225, 325)   # 全局(示例 9 折 225/325)
    better_factory = calc_factory_cents(pts, 150, 325)   # 某服务商更优进货折扣(150/325)
    assert better_factory < global_factory, "更优 per-agent 折扣的出厂成本应更低"
    # ceil 公式正确性
    assert global_factory == (pts * 225 + 324) // 325
    assert better_factory == (pts * 150 + 324) // 325
    # 若结算误用全局,有 override 服务商 margin 会被低估 (global_factory - better_factory) cents
    assert global_factory - better_factory > 0
