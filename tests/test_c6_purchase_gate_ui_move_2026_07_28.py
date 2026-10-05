"""微单 C-6 判别锁(2026-07-28)· 门控开关 UI 迁移:推广中心 → 客户售价页。

单一入口:总开关+客户级三态只在 PricingCenter(经 ClientPurchaseGatePanel);
PromotionCenter 旧入口撤除。后端 API 与门控判定零改动(既有 26 锁另行回归)。
变异:恢复旧入口 / 移除新入口 → 转红。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend" / "src"

_PANEL = FE / "components" / "agent" / "ClientPurchaseGatePanel.tsx"
_PRICING = FE / "pages" / "Agent" / "PricingCenter.tsx"
_PROMOTION = FE / "pages" / "Agent" / "PromotionCenter.tsx"


def test_panel_component_carries_master_switch_and_tri_state():
    src = _PANEL.read_text(encoding="utf-8")
    assert 'data-testid="client-purchase-gate-panel"' in src
    assert 'data-testid="client-purchase-gate-master"' in src
    assert "允许名下客户线上购买" in src
    # 三态齐全
    for opt in ('"inherit"', '"allow"', '"offline_only"'):
        assert opt in src, f"三态缺 {opt}"
    # 只调既有三端点(API 零改动):读列表 + 总开关 + 客户覆盖
    assert "agentApi.promotionCustomers(" in src
    assert "agentApi.setClientPurchaseSettings(" in src
    assert "agentApi.setCustomerPurchaseOverride(" in src


def test_pricing_center_hosts_panel_on_top():
    """售价页是唯一入口且置顶:面板渲染必须在算力包经营区(SummaryTile)之前。

    变异(移除新入口)→ 本锁转红。
    """
    src = _PRICING.read_text(encoding="utf-8")
    assert "ClientPurchaseGatePanel" in src
    assert src.index("<ClientPurchaseGatePanel />") < src.index("SummaryTile"), (
        "门控面板必须置顶(在经营汇总/SKU 区之前)"
    )


def test_promotion_center_old_entry_fully_removed():
    """推广中心零门控残留(单一入口)。变异(恢复旧入口)→ 本锁转红。"""
    src = _PROMOTION.read_text(encoding="utf-8")
    assert "允许名下客户线上购买" not in src
    assert "setClientPurchaseSettings" not in src
    assert "setCustomerPurchaseOverride" not in src
    assert "online_purchase_override ||" not in src  # 三态 select 绝迹
    assert "offline_only" not in src
    # 表格列数同步(9→8),空态 colSpan 不撒谎
    assert "colSpan={8}" in src and "colSpan={9}" not in src
    # v35 UI 审计口径保持(错误文案收口函数仍在用)
    assert "formatApiErrorForDisplay" in src


def test_playwright_location_regression_exists():
    """真渲染位置回归资产入库(截图规,含置顶断言与旧入口撤除断言)。"""
    spec = (ROOT / "frontend" / "tests" / "online-purchase-gate"
            / "provider-gate-ui-location.spec.ts").read_text(encoding="utf-8")
    assert "client-purchase-gate-panel" in spec
    assert "panelBox.y < skuBox.y" in spec  # 置顶是行为断言不是口头承诺
    assert "provider-gate-on-pricing-page.png" in spec
    assert "provider-gate-removed-from-promotion.png" in spec
