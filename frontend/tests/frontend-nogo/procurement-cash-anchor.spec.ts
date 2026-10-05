import { expect, test, type Page, type Route } from './_fixtures';

const agentUser = {
  id: 9021,
  username: 'cash-anchor-agent',
  display_name: '测试服务商',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '服务商' }],
  permissions: ['quote:write', 'settings:read'],
  client_brand_ids: [],
};

const forbiddenPublicTerms = [
  /platform_base/i,
  /upstream/i,
  /multiplier/i,
  /cost_basis/i,
  /\bB2B\b/i,
  /平台库存转售|倍率|上游|底价|逐级利润/,
];

async function installCashAnchorSession(page: Page) {
  const publicResponses: string[] = [];
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'cash-anchor-test-token');
    localStorage.setItem('onboarding_state', 'cash-anchor-existing-provider');
    localStorage.setItem('omnirank_onboarding_done', 'true');
  });
  page.on('response', async response => {
    const path = new URL(response.url()).pathname;
    if (path.startsWith('/api/agent/inventory') || path.startsWith('/api/pricing/procurement')) {
      publicResponses.push(await response.text().catch(() => ''));
    }
  });
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: agentUser } });
      return;
    }
    if (path === '/api/agent/agreement/v35-status') {
      await route.fulfill({ json: { status: 'signed', version: 'v2.4' } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: {
        paid_points: 0,
        commission_points: 0,
        bonus_points: 0,
        frozen_points: 0,
        agent_level: 1,
      } });
      return;
    }
    if (path === '/api/agent/channel-tier/me') {
      await route.fulfill({ json: { channel_tier: null } });
      return;
    }
    if (path === '/api/agent/inventory/balance') {
      await route.fulfill({ json: {
        paid_inventory_points: 0,
        bonus_inventory_points: 0,
        frozen_inventory_points: 0,
        total_purchased_points: 0,
        total_allocated_points: 0,
        alert_level: 'empty',
      } });
      return;
    }
    if (path === '/api/agent/inventory/transactions') {
      await route.fulfill({ json: { items: [], total: 0 } });
      return;
    }
    if (path === '/api/pricing/procurement/catalog') {
      await route.fulfill({ json: { success: true, data: {
        seller_label: 'OmniRank 平台',
        service: { service_status: 'platform_managed', configuration_status: 'ready' },
        catalog_version: 'cash-anchor-ui-v1',
        items: [{
          product_code: 'apo_6464f92d12b6',
          display_name: 'apo_6464f92d12b6',
          cash_price_cents: 50000,
          paid_inventory_points: 50000,
          bonus_inventory_points: 0,
          total_inventory_points: 50000,
        }],
      } } });
      return;
    }
    if (path === '/api/agent/inventory/purchase-preview') {
      const request = route.request().postDataJSON() as { amount_cents?: number };
      expect(request.amount_cents).toBe(100);
      await route.fulfill({ json: {
        price_quote_id: 'cash-anchor-ui-quote',
        amount_cents: 100,
        base_points: 100,
        bonus_points: 0,
        total_points: 100,
        catalog_version: 'cash-anchor-ui-v1',
      } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });
  return publicResponses;
}

for (const viewport of [
  { width: 320, height: 720 },
  { width: 375, height: 667 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1366, height: 768 },
  { width: 1920, height: 1080 },
]) {
  test(`进货金额锚定与隐私 ${viewport.width}x${viewport.height}`, async ({ page }) => {
    await page.setViewportSize(viewport);
    const publicResponses = await installCashAnchorSession(page);
    await page.goto('/agent/inventory');

    await expect(page.getByText('输入金额就是本次应付金额，系统按当前采购规则换算到账算力。')).toBeVisible();
    await expect(page.getByRole('link', { name: '《服务商协议》' })).toBeVisible();
    await expect(page.getByText('apo_6464f92d12b6', { exact: true })).toHaveCount(0);
    await page.getByLabel('自由金额进货（元）').fill('1');
    await page.getByRole('button', { name: '获取并确认报价' }).click();
    await expect(page.getByRole('heading', { name: '确认本次自由金额进货' })).toBeVisible();
    await expect(page.getByText('¥1.00', { exact: true })).toBeVisible();

    const visibleText = await page.locator('body').innerText();
    const networkText = publicResponses.join('\n');
    for (const pattern of forbiddenPublicTerms) {
      expect(visibleText).not.toMatch(pattern);
      expect(networkText).not.toMatch(pattern);
    }
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
    );
    expect(overflow).toBe(false);
  });
}
