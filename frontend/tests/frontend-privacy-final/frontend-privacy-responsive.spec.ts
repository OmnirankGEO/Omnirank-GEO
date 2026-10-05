import { expect, test, type Page, type Route } from 'playwright/test';

const longBrand = '浙江岱林生物技术股份有限公司超长品牌展示名称用于响应式验证';
const longIndustryBrand = '雅栖酒店集团旗下超长品牌名称用于行业展示验证';
const longOwner = '华东区域超级长归属服务运营负责人姓名用于管理员审计验证';
const longIndustry = '生物制药与实验室自动化设备综合解决方案超长行业名称';
const longImageName = '岱林生物实验室全自动培养检测设备与生产环境超长图片文件名称用于触摸键盘完整查看.png';

const forbiddenTokens = [
  'agent_user_id', 'upstream_user_id', 'resolved_user_id',
  'service_account_code', 'channel_account_code', 'relationship_version',
  'cost_multiplier', 'bound_agent_user_id', 'agent_referral_code',
  '直属渠道', '绑定服务方', '上级服务商', '总部账号',
  '尚未绑定服务方', '价格和包装由服务方设置',
];

const adminUser = {
  id: 99101,
  user_id: 99101,
  username: 'local-admin-probe',
  display_name: '本地管理员探针',
  is_admin: true,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
  permissions: ['diagnosis:read', 'diagnosis:write', 'brands:read'],
  client_brand_ids: [7001, 7002, 7003],
};

const customerUser = {
  id: 99201,
  user_id: 99201,
  username: 'local-customer-probe',
  display_name: '客户探针',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 0,
  roles: [{ id: 2, name: 'geo_writer', display_name: '普通用户' }],
  permissions: ['wallet:read', 'diagnosis:read'],
  client_brand_ids: [7001],
};

const agentUser = {
  ...customerUser,
  id: 99301,
  user_id: 99301,
  username: 'local-agent-probe',
  display_name: '服务商探针',
  agent_level: 2,
  roles: [{ id: 3, name: 'agent', display_name: '服务商' }],
  permissions: ['wallet:read', 'agent:read', 'brands:read'],
};

const publicWallet = {
  paid_points: 100000,
  commission_points: 0,
  bonus_points: 0,
  frozen_points: 0,
  total: 100000,
  customer_credit_status: 'ready',
  customer_credit: {
    tool_credit_points: 100000,
    publish_credit_points: 0,
    bonus_credit_points: 0,
    total_purchased_points: 100000,
    total_consumed_points: 0,
  },
};

function installErrorCapture(page: Page) {
  const errors: string[] = [];
  page.on('pageerror', (error) => errors.push(`pageerror:${error.message}`));
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console:${message.text()}`);
  });
  return errors;
}

function installPublicApiCapture(page: Page) {
  const bodies: Promise<string>[] = [];
  page.on('response', (response) => {
    if (!new URL(response.url()).pathname.startsWith('/api/')) return;
    bodies.push(response.text().catch(() => ''));
  });
  return async () => {
    await page.waitForLoadState('networkidle');
    const serialized = (await Promise.all(bodies)).join('\n');
    for (const token of forbiddenTokens) expect(serialized).not.toContain(token);
  };
}

async function installSession(
  page: Page,
  user: Record<string, unknown>,
  handler: (path: string, route: Route) => Promise<boolean>,
) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-intercept-only');
    localStorage.setItem('omnirank_m3_onboarded_at', 'local-ui-probe');
    localStorage.removeItem('omnirank_locked_ref');
    localStorage.removeItem('portal_owner_user_id');
  });
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: publicWallet } });
      return;
    }
    if (await handler(path, route)) return;
    await route.fulfill({ json: { success: true, data: {}, items: [], total: 0 } });
  });
}

async function expectNoPrivacyResidue(page: Page) {
  const snapshot = await page.evaluate(() => ({
    text: document.body.innerText,
    url: window.location.href,
    local: Object.fromEntries(Object.keys(localStorage).map((key) => [key, localStorage.getItem(key)])),
    session: Object.fromEntries(Object.keys(sessionStorage).map((key) => [key, sessionStorage.getItem(key)])),
    overflow: document.documentElement.scrollWidth - window.innerWidth,
  }));
  const serialized = JSON.stringify(snapshot);
  for (const token of forbiddenTokens) expect(serialized).not.toContain(token);
  expect(snapshot.overflow).toBeLessThanOrEqual(1);
}

test('诊断品牌全称支持键盘、鼠标和触摸查看且六档无溢出', async ({ page }, testInfo) => {
  const errors = installErrorCapture(page);
  await installSession(page, adminUser, async (path, route) => {
    if (path === '/api/my-clients') {
      await route.fulfill({
        json: {
          success: true,
          clients: [
            { id: 7001, name: longBrand, industry: longIndustry, owner_name: longOwner },
            { id: 7002, name: '短品牌', industry: '科技服务', owner_name: '短归属' },
            { id: 7003, name: longIndustryBrand, industry: longIndustry },
          ],
        },
      });
      return true;
    }
    if (/^\/api\/brands\/\d+\/latest-diagnosis-params$/.test(path)) {
      await route.fulfill({ json: { has_diagnosis: false, has_profile: false } });
      return true;
    }
    return false;
  });

  await page.goto('/diagnosis/new');
  const brandInput = page.getByPlaceholder('输入品牌名搜索或创建新品牌');
  await expect(brandInput).toBeVisible();
  await brandInput.locator('xpath=following-sibling::button[1]').click();

  const row = page.getByTestId('brand-option-7001');
  await expect(row).toBeVisible();
  const metrics = await row.evaluate((element) => ({
    rowWidth: element.getBoundingClientRect().width,
    rowScrollWidth: element.scrollWidth,
    viewportWidth: window.innerWidth,
    documentOverflow: document.documentElement.scrollWidth - window.innerWidth,
  }));
  expect(metrics.rowScrollWidth).toBeLessThanOrEqual(Math.ceil(metrics.rowWidth) + 1);
  expect(metrics.documentOverflow).toBeLessThanOrEqual(1);
  if (testInfo.project.use.viewport!.width < 640) {
    expect(metrics.rowWidth).toBeGreaterThan(metrics.viewportWidth * 0.75);
  }

  const disclosure = page.getByRole('button', { name: `查看${longBrand}完整信息` });
  await disclosure.focus();
  await expect(page.getByTestId('brand-option-full-7001')).toContainText(longBrand);
  await expect(page.getByTestId('brand-option-full-7001')).toContainText(longOwner);
  await page.getByRole('button', { name: `收起${longBrand}完整信息` }).click();
  await disclosure.click();
  await expect(page.getByTestId('brand-option-full-7001')).toBeVisible();

  await page.screenshot({ path: testInfo.outputPath('new-diagnosis-brand-dropdown.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('客户购买页只显示平台化价格结果且缓存、URL、DOM无上游痕迹', async ({ page }, testInfo) => {
  const errors = installErrorCapture(page);
  const expectPublicApiClean = installPublicApiCapture(page);
  await installSession(page, customerUser, async (path, route) => {
    if (path === '/api/pricing/retail/catalog') {
      await route.fulfill({
        json: {
          success: true,
          data: {
            seller_label: 'OmniRank 平台',
            service: { service_status: 'platform_managed', configuration_status: 'ready', account_configured: true, dispute_pending: false },
            catalog_version: 'public-v1',
            items: [{
              product_code: 'credit_basic',
              display_name: 'OmniRank 企业增长算力包超长名称完整展示验证',
              final_price_cents: 180000,
              points_granted: 195000,
              bonus_points: 0,
              usage_examples: ['约可生成 500 篇基础文章'],
            }],
          },
        },
      });
      return true;
    }
    if (path === '/api/agent/channel-tier/me') {
      await route.fulfill({ json: { success: true, channel_tier: {} } });
      return true;
    }
    return false;
  });

  await page.goto('/customer/recharge');
  await expect(page.getByRole('heading', { name: '购买算力' })).toBeVisible();
  await expect(page.getByText('OmniRank 企业增长算力包超长名称完整展示验证')).toBeVisible();
  await expectPublicApiClean();
  await expectNoPrivacyResidue(page);
  await page.screenshot({ path: testInfo.outputPath('customer-recharge-public-contract.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('客户额度钱包使用平台服务中性契约且响应、DOM、storage无上游痕迹', async ({ page }, testInfo) => {
  const errors = installErrorCapture(page);
  const expectPublicApiClean = installPublicApiCapture(page);
  await installSession(page, customerUser, async (path, route) => {
    if (path === '/api/customer/credit/summary') {
      await route.fulfill({
        json: {
          tool_credit_points: 120000,
          publish_credit_points: 10000,
          bonus_credit_points: 5000,
          total_purchased_points: 135000,
          service: {
            service_status: 'platform_managed',
            configuration_status: 'ready',
            account_configured: true,
            dispute_pending: false,
          },
          recent_transactions: [],
        },
      });
      return true;
    }
    return false;
  });

  await page.goto('/customer/wallet');
  await expect(page.getByRole('heading', { name: '当前服务可用额度' })).toBeVisible();
  await expect(page.getByText('由 OmniRank 平台提供服务')).toBeVisible();
  await expectNoPrivacyResidue(page);
  await expectPublicApiClean();
  await page.screenshot({ path: testInfo.outputPath('customer-credit-wallet.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('服务商库存页只显示自己的进货与库存且无上游痕迹', async ({ page }, testInfo) => {
  const errors = installErrorCapture(page);
  const expectPublicApiClean = installPublicApiCapture(page);
  await installSession(page, agentUser, async (path, route) => {
    if (path === '/api/agent/agreement/v35-status') {
      await route.fulfill({ json: { status: 'signed', version: 'v3.5' } });
      return true;
    }
    if (path === '/api/agent/inventory/balance') {
      await route.fulfill({ json: {
        paid_inventory_points: 500000,
        bonus_inventory_points: 20000,
        frozen_inventory_points: 0,
        total_purchased_points: 520000,
        total_allocated_points: 100000,
        alert_level: 'normal',
      } });
      return true;
    }
    if (path === '/api/agent/inventory/transactions') {
      await route.fulfill({ json: { items: [], total: 0 } });
      return true;
    }
    if (path === '/api/pricing/procurement/catalog') {
      await route.fulfill({ json: {
        success: true,
        data: {
          seller_label: 'OmniRank 平台',
          service: { service_status: 'platform_managed', configuration_status: 'ready' },
          catalog_version: 'procurement-v1',
          items: [{
            product_code: 'inventory_basic',
            display_name: 'OmniRank 服务商进货算力包',
            cash_price_cents: 120000,
            paid_inventory_points: 195000,
            bonus_inventory_points: 10000,
          }],
        },
      } });
      return true;
    }
    if (path === '/api/agent/channel-tier/me') {
      await route.fulfill({ json: { success: true, channel_tier: { tier: 'certified' } } });
      return true;
    }
    return false;
  });

  await page.goto('/agent/inventory');
  await expect(page.getByText('OmniRank 服务商进货算力包')).toBeVisible();
  await expectPublicApiClean();
  await expectNoPrivacyResidue(page);
  await page.screenshot({ path: testInfo.outputPath('agent-inventory-platform-supply.png'), fullPage: true });
  expect(errors).toEqual([]);
});

test('图片超长名称可聚焦、点击展开且移动端不截断为不可恢复', async ({ page }, testInfo) => {
  const errors = installErrorCapture(page);
  await installSession(page, agentUser, async (path, route) => {
    if (path === '/api/my-clients/7001') {
      await route.fulfill({ json: {
        success: true,
        brand: { id: 7001, name: longBrand, industry: longIndustry, brand_type: 'client' },
        profile: null,
      } });
      return true;
    }
    if (path === '/api/brand-images/list/7001') {
      await route.fulfill({ json: { assets: [{
        id: 8101,
        brand_id: 7001,
        title: longImageName,
        file_name: longImageName,
        public_url: '/logo-192.png',
        thumbnail_key: '/logo-192.png',
        alt_text: '实验室设备',
        status: 'active',
        publish_allowed: 1,
        rights_confirmed: 1,
        risk_flags: [],
        usage_scenarios: ['product_desc'],
        suggested_placement: 'product_desc',
        image_type: 'product',
      }] } });
      return true;
    }
    if (path === '/api/m3/material-confirm/status/7001') {
      await route.fulfill({ json: {
        status: 'none',
        has_session: false,
        token: null,
        token_url: null,
        expires_at: null,
        confirmed_at: null,
        customer_notes: '',
        materials_summary: {
          company_name: '', industry: longIndustry, intro_excerpt: '', usp_excerpt: '',
          fields_filled: [], fields_missing: [], filled_count: 0, total_fields: 8,
          selling_points_count: 0, cases_count: 0, testimonials_count: 0,
        },
        last_session_id: null,
        can_generate_link: false,
        brand_id: 7001,
        brand_name: longBrand,
      } });
      return true;
    }
    return false;
  });

  await page.goto('/my-clients/7001?tab=files');
  const imageSectionButton = page.locator('button:visible').filter({ hasText: /^图片素材$/ }).first();
  if (await imageSectionButton.count()) await imageSectionButton.click();
  const disclosure = page.locator('[data-testid="image-name-disclosure-8101"]:visible');
  await expect(disclosure).toBeVisible();
  const summary = disclosure.locator('summary');
  await summary.focus();
  await summary.press('Enter');
  await expect(disclosure.locator('p')).toHaveText(longImageName);
  const overflow = await disclosure.evaluate((element) => element.scrollWidth - element.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath('brand-image-full-name.png'), fullPage: true });
  expect(errors).toEqual([]);
});
