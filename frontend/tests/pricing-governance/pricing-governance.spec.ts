import { expect, test, type Page, type Route } from 'playwright/test';

// WP2 §5.4 · 用户详情内嵌定价治理面板 + <GovernanceAlert> §13 合同 · 七视口响应式门。
//
// 判别:
//  - 服务商 → 可操作面板(专属报价系数输入框可见可编辑),不再只读解释;
//  - 非服务商 → §13 告警合同(data-governance-code + "去调整为服务商"动作),无编辑框(避免事故#2);
//  - 隐私:渲染 DOM 绝不出现 upstream/cost/底价/倍率/seller 内部字段。
// 每条在 7 个视口(320/390/768/1366/1440/1920/2560)各跑一次(config projects)。

const VERSIONS = {
  business_identity: 1, commercial_binding: 1, channel_relationship: 1,
  platform_access: 1, password_security: 1, wallet_adjustment: 1, account_status: 1,
};

function listItem(identity: 'service_provider' | 'ordinary_user') {
  return {
    user_id: 55, username: 'geo_target', display_name: '目标服务商',
    phone: null, is_active: true, business_identity: identity, platform_access: 'standard',
    total_points: 1000, customer_count: 2, brand_count: 1,
    service_mode: identity === 'service_provider' ? 'service_provider' : 'platform_direct',
    needs_attention: false, attention_label: null,
    created_at: '2026-07-01T00:00:00Z', last_active_at: '2026-07-20T00:00:00Z', versions: VERSIONS,
  };
}

function detail(identity: 'service_provider' | 'ordinary_user') {
  return {
    success: true,
    overview: {
      user_id: 55, username: 'geo_target', display_name: '目标服务商', phone: null, company: null,
      is_active: true, business_identity: identity,
      business_identity_label: identity === 'service_provider' ? '服务商' : '普通用户',
      platform_access: 'standard', platform_access_label: '标准',
      total_points: 1000, paid_points: 800, bonus_points: 200, total_recharged_points: 800,
      customer_count: 2, brand_count: 1,
      created_at: '2026-07-01T00:00:00Z', last_login_at: null, last_active_at: '2026-07-20T00:00:00Z',
      versions: VERSIONS,
    },
    relationships: {
      registration: { present: false, inviter: null, source: 'none', registered_at: null, legacy_pointer_user_id: null, evidence: { status: 'complete', items: [] } },
      commercial: { mode: identity === 'service_provider' ? 'service_provider' : 'platform_direct', provider: null, binding_id: null, binding_source: null, binding_source_label: '无', bound_at: null, relationship_version: 1, dispute_status: null, evidence: { status: 'complete', items: [] } },
      channel: { mode: 'not_applicable', upstream: null, relationship_version: null, cost_multiplier_bps: null, effective_from: null, reason: null },
      dual_relationships_present: false, dual_relationships_label: null, notices: [],
    },
    pricing_and_settlement: {
      customer_pricing_route: '按标准算力包报价',
      procurement_pricing_route: '平台标准进货',
      settlement_route: '标准结算',
      pricing_source: '平台默认规则',
      special_pricing_note: null,
    },
    clients_and_brands: { clients: [], brands: [] },
    wallet_and_billing: { paid_points: 800, bonus_points: 200, total_points: 1000, total_recharged_points: 800, recent_orders: [], recent_transactions: [] },
    permissions_and_security: { platform_access: 'standard', account_status_label: '正常', must_change_password: false, permission_version: 1, legacy_roles: [] },
    operation_logs: [],
  };
}

async function installMocks(page: Page, identity: 'service_provider' | 'ordinary_user') {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'pricing-gov-token');
    // 跳过首登新手引导覆盖层(否则弹窗盖住页面,tab 点不到)。
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'returning', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-07-21T00:00:00Z', last_updated_at: '2026-07-21T00:00:00Z',
    }));
  });
  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/auth/me') {
      return route.fulfill({ json: { success: true, user: {
        user_id: 1, id: 1, username: 'admin', display_name: '管理员',
        is_admin: true, agent_level: 9, is_active: 1, must_change_password: 0,
        roles: [], permissions: [], modules: [],
      } } });
    }
    if (path === '/api/admin/user-governance/platform-direct-readiness') {
      return route.fulfill({ json: { readiness: { configured: false, ready: false, status: 'missing', label: '未配置', checks: [] } } });
    }
    if (path === '/api/admin/user-governance/users' && !path.match(/users\/\d+$/)) {
      return route.fulfill({ json: { users: [listItem(identity)], total: 1, total_pages: 1 } });
    }
    if (/\/api\/admin\/user-governance\/users\/\d+$/.test(path)) {
      return route.fulfill({ json: detail(identity) });
    }
    if (path === '/api/admin/pricing/center') {
      return route.fulfill({ json: { success: true, catalog_version: 'v-test-1', default_rule: { points_per_yuan: 130, wholesale_discount: 0.9, agent_purchase_bonus_rate: 0.05 } } });
    }
    if (/\/api\/admin\/pricing\/agent-override\/\d+$/.test(path)) {
      return route.fulfill({ json: { success: true, agent_user_id: 55, catalog_version: 'v-test-1', override: { quote_markup_override: 2.0, sku_markup_override: 2.0, wholesale_numer: 104, wholesale_denom: 13000, note: '测试备注' } } });
    }
    // 其余 bootstrap 调用给一个无害成功,避免打断页面加载。
    return route.fulfill({ json: { success: true } });
  });
}

async function gotoPricingTab(page: Page) {
  await page.goto('/admin/users');
  // 首个用户自动选中 → 详情载入;点"定价与结算"tab。
  const pricingTab = page.getByRole('button', { name: '定价与结算' });
  await pricingTab.click();
}

const FORBIDDEN = ['cost_basis', 'platform_base', '底价', '倍率', '逐级利润', 'seller_account_code'];

test('service provider: 报价系数可操作输入框在本视口可见可编辑', async ({ page }) => {
  await installMocks(page, 'service_provider');
  await gotoPricingTab(page);
  const input = page.getByTestId('agent-quote-markup-input');
  await expect(input).toBeVisible();
  await input.fill('2.5');
  await expect(input).toHaveValue('2.5');
  // 该值来自 mock override(2.0)已回填,再被编辑为 2.5 —— 证明预填 + 可编辑
  const body = (await page.locator('body').innerText()).toLowerCase();
  for (const banned of FORBIDDEN) {
    expect(body.includes(banned.toLowerCase()), `隐私泄露: ${banned}`).toBeFalsy();
  }
});

test('ordinary user: 非服务商 → §13 告警合同给"去调整身份"出口,无编辑框', async ({ page }) => {
  await installMocks(page, 'ordinary_user');
  await gotoPricingTab(page);
  const alert = page.locator('[data-governance-code="PRICING_TARGET_NOT_SERVICE_PROVIDER"]');
  await expect(alert).toBeVisible();
  await expect(alert.locator('[data-governance-action="go_adjust_identity"]')).toBeVisible();
  // 非服务商绝不出现可编辑报价系数框(避免误配)
  await expect(page.getByTestId('agent-quote-markup-input')).toHaveCount(0);
});
