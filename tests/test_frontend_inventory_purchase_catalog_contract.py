"""Focused source-contract locks for the inventory purchase catalog frontend."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_pricing_center_has_four_business_sections_and_shared_channel_panel():
    page = _read("frontend/src/pages/Admin/PricingCenter.tsx")
    for title in ("默认进货规则", "进货价目表", "渠道奖励", "普通客户算力包"):
        assert title in page
    assert "<ChannelTierPanel embedded />" in page
    assert "<InventoryPurchaseCatalogPanel />" in page
    assert page.count("<TabsTrigger value=\"") >= 4
    for value in ("default-rules", "purchase-catalog", "channel-rewards", "customer-packages"):
        assert f'<TabsTrigger value="{value}">' in page
        assert f'<TabsContent value="{value}"' in page
    assert "href={href}" not in page


def test_legacy_channel_route_is_only_a_shared_panel_wrapper():
    wrapper = _read("frontend/src/pages/Admin/ChannelTierAdmin.tsx")
    panel = _read("frontend/src/pages/Admin/ChannelTierPanel.tsx")
    assert "<ChannelTierPanel />" in wrapper
    assert "agent_purchase_options" not in panel
    assert "服务商进货档" not in panel


def test_catalog_editor_uses_typed_occ_contract_and_business_warnings():
    api = _read("frontend/src/lib/v35w2Api.ts")
    editor = _read("frontend/src/pages/Admin/InventoryPurchaseCatalogPanel.tsx")
    assert "/api/admin/pricing/inventory-purchase-catalog" in api
    assert "InventoryPurchaseCatalogPutRequest" in api
    assert "expected_catalog_version: catalogVersion" in editor
    assert "environment_override.active" in editor
    assert "环境配置覆盖中" in editor
    assert "配置已被其他管理员更新" in editor
    assert "本地草稿已保留" in editor
    assert "只影响保存后的新订单，历史及待支付订单不变" in editor


def test_catalog_editor_can_activate_procurement_without_retail_blockers():
    editor = _read("frontend/src/pages/Admin/InventoryPurchaseCatalogPanel.tsx")
    assert "pricingPublicationDryRun([])" in editor
    assert "reviewed_target_mode: current.review_contract.target_mode" in editor
    assert "reviewed_service_user_ids: current.review_contract.service_user_ids" in editor
    assert "reviewed_scopes: current.review_contract.scopes" in editor
    assert "进货价目可以单独生效" in editor
    assert "让新价目生效" in editor


def test_service_provider_buy_page_routes_to_inventory_and_previews_own_packages():
    page = _read("frontend/src/pages/Customer/BuyCredit.tsx")
    provider_branch = page.index("if (isServiceProvider) {")
    customer_catalog = page.index("const catalogResult = await getRetailCatalog();")
    assert provider_branch < customer_catalog
    assert "const { user, isLoading: authLoading } = useAuth();" in page
    assert "Number(user?.agent_level ?? 0) >= 1" in page
    assert "const response = await agentApi.pricingSKUs();" in page
    assert ".filter((item: ProviderRetailPreview) => item.is_active)" in page
    assert 'to="/agent/inventory"' in page
    assert 'to="/agent/pricing"' in page
    assert "当前页面是普通客户购买算力的入口" in page
    assert "你的客户当前看到的算力包" in page
    provider_preview = page.split("{!authLoading && isServiceProvider && (", 1)[1].split(
        "{!isServiceProvider && loading", 1
    )[0]
    assert "wholesale_cents" not in provider_preview
    assert "立即购买" not in provider_preview


def test_admin_publication_primary_copy_hides_engineering_terms():
    panel = _read("frontend/src/pages/Admin/PricingPublicationPanel.tsx")
    visible = panel.split("return (", 1)[1]
    technical = visible.split("查看技术详情（排障时使用）", 1)
    assert "让新价目生效" in visible
    assert "以下问题处理后才能生效" in visible
    assert "保存只是保留草稿" in visible
    assert len(technical) == 2
    primary_copy = technical[0]
    for jargon in ("运行时价目", "配置纪元", "开闸阻断", "PRICING_DUAL_SSOT_ENABLED"):
        assert jargon not in primary_copy


def test_channel_panel_has_occ_per_tier_switch_and_description():
    api = _read("frontend/src/lib/v35w2Api.ts")
    panel = _read("frontend/src/pages/Admin/ChannelTierPanel.tsx")
    assert "expected_catalog_version: expectedCatalogVersion" in api
    assert "configRes.catalog_version" in panel
    assert "is_enabled" in panel
    assert "description" in panel
    assert "配置已被其他管理员更新" in panel
    assert 'to="/admin/channel-tier"' in panel
    assert "仅管理认证、优选、战略三档" in panel
    assert "!embedded && <Panel title=\"创始席位与奖励有效期\">" in panel
    pricing_center = _read("frontend/src/pages/Admin/PricingCenter.tsx")
    assert "centerTab === 'customer-packages'" in pricing_center


def test_money_conversion_is_integer_only_and_catalog_has_client_validation():
    money = _read("frontend/src/lib/inventoryPurchaseCatalog.ts")
    assert "whole * 100 + fraction" in money
    assert "parseFloat" not in money
    assert "Number.isSafeInteger" in money
    assert "MAX_AGENT_PURCHASE_AMOUNT_CENTS" in money
    assert "至少保留一个启用档位" in money
    assert "所有档位的进货金额不能重复" in money
    assert "排序必须是大于或等于 0 的整数" in money
    assert "a.sort_order - b.sort_order" in money
    assert "a.amount_cents - b.amount_cents" in money


def test_agent_purchase_sends_option_version_and_quote_fingerprint_but_never_points():
    api = _read("frontend/src/lib/v35w2Api.ts")
    inventory = _read("frontend/src/pages/Agent/InventoryCenter.tsx")
    purchase_body = api.split("createPurchase:", 1)[1].split("// [1C", 1)[0]
    assert "option_id" in purchase_body
    assert "expected_catalog_version" in purchase_body
    assert "expected_quote_fingerprint" in purchase_body
    assert "previewPurchase" in api
    assert "previewPurchase" in inventory
    assert "quote_fingerprint" in inventory
    assert "base_points" not in purchase_body
    assert "bonus_points" not in purchase_body
    assert "total_points" not in purchase_body
    assert "e?.response?.status === 409" in inventory
    assert "await reload()" in inventory
    assert "自由金额进货（元）" in inventory


def test_admin_inventory_uses_platform_warehouse_mode_without_self_procurement():
    inventory = _read("frontend/src/pages/Agent/InventoryCenter.tsx")
    assert "const isPlatformWarehouseMode = Boolean(user?.is_admin)" in inventory
    assert "isPlatformWarehouseMode\n        ? Promise.resolve(null)\n        : getProcurementCatalog()" in inventory
    assert "if (catalogResult === null)" in inventory
    assert "平台仓库不向自身进货" in inventory
    assert "平台库存发行只记仓库数量和发行流水，不计入平台经营收入" in inventory
    assert 'to="/admin/pricing-center"' in inventory
    assert 'to="/admin/users"' in inventory


def test_sidebar_does_not_gain_an_inventory_catalog_entry():
    sidebar = _read("frontend/src/components/layout/AppSidebar.tsx")
    assert "/admin/inventory-purchase" not in sidebar
    assert "/admin/inventory-pricing" not in sidebar
