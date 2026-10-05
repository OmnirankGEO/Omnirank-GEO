import { expect, test, type Page, type Route } from './_fixtures';

const agentUser = {
  id: 102,
  username: 'qa-agent',
  display_name: '服务商',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '代理' }],
  permissions: ['quote:write', 'settings:read'],
  client_brand_ids: [100, 101],
};

async function seedStaleClientSelection(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-intercept-only');
    localStorage.setItem('omnirank_current_brand_id', '545');
  });
}

async function installCommonRoutes(
  page: Page,
  contextReply: (route: Route) => Promise<void>,
  includeStoredBrand = false,
) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: agentUser } });
      return;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'local-refreshed-only', user: agentUser } });
      return;
    }
    if (path === '/api/client-context/list') {
      await route.fulfill({ json: {
        success: true,
        clients: includeStoredBrand
          ? [{ id: 545, brand_name: '暂时读取失败的品牌' }]
          : [
              { id: 100, brand_name: '可访问品牌甲' },
              { id: 101, brand_name: '可访问品牌乙' },
            ],
      } });
      return;
    }
    if (path === '/api/client-context/545') {
      await contextReply(route);
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: {
        success: true,
        data: {
          paid_points: 0,
          commission_points: 0,
          bonus_points: 0,
          frozen_points: 0,
          total_recharged: 0,
          customer_credit_status: 'ready',
          customer_credit: {
            tool_credit_points: 0,
            publish_credit_points: 0,
            bonus_credit_points: 0,
            total_purchased_points: 0,
            total_consumed_points: 0,
            agent_user_id: null,
          },
        },
      } });
      return;
    }
    if (path === '/api/wallet/transactions') {
      await route.fulfill({ json: { items: [], total: 0, page: 1, limit: 20 } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });
}

test('已撤权品牌不在权威列表时清除旧选择且不请求客户详情', async ({ page }) => {
  let contextCalls = 0;
  let refreshCalls = 0;
  await seedStaleClientSelection(page);
  await installCommonRoutes(page, async route => {
    contextCalls += 1;
    await route.fulfill({ status: 403, json: { detail: '无权访问 brand_id' } });
  });
  page.on('request', request => {
    if (new URL(request.url()).pathname === '/api/auth/refresh') refreshCalls += 1;
  });

  await page.goto('/agent/pricing');
  await expect(page).toHaveURL(/\/agent\/pricing/);
  await expect(page.getByTestId('app-page-main')).toBeVisible();
  await page.waitForTimeout(1_000);

  expect(contextCalls).toBe(0);
  expect(refreshCalls).toBe(0);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('omnirank_current_brand_id'))).toBeNull();
  const stableCount = contextCalls;
  await page.waitForTimeout(500);
  expect(contextCalls).toBe(stableCount);
});

test('客户上下文 429 不会被 loading 状态反复触发', async ({ page }) => {
  let contextCalls = 0;
  await seedStaleClientSelection(page);
  await installCommonRoutes(page, async route => {
    contextCalls += 1;
    await route.fulfill({ status: 429, headers: { 'Retry-After': '60' }, json: { detail: '请求太频繁' } });
  }, true);

  await page.goto('/wallet');
  await expect(page).toHaveURL(/\/wallet/);
  await expect(page.getByTestId('app-page-main')).toBeVisible();
  await expect.poll(() => contextCalls).toBe(1);
  await page.waitForTimeout(1_000);

  expect(contextCalls).toBe(1);
  expect(await page.evaluate(() => localStorage.getItem('omnirank_current_brand_id'))).toBeNull();
});

test('客户上下文 500 只尝试一次并保持页面可用', async ({ page }) => {
  let contextCalls = 0;
  await seedStaleClientSelection(page);
  await installCommonRoutes(page, async route => {
    contextCalls += 1;
    await route.fulfill({ status: 500, json: { detail: 'temporary' } });
  }, true);

  await page.goto('/wallet');
  await expect(page).toHaveURL(/\/wallet/);
  await expect(page.getByTestId('app-page-main')).toBeVisible();
  await expect.poll(() => contextCalls).toBe(1);
  await page.waitForTimeout(1_000);
  expect(contextCalls).toBe(1);
});
