/**
 * 板块 C · 白标作用域真实路由回归。
 *
 * 走生产 App/Router/AuthContext/Layout/useBranding/PublicQuote，仅 mock HTTP 边界；
 * 不使用测试专用页面，不连接后端或数据库。
 */
import { expect, test, type Page, type Route } from 'playwright/test';

const PLATFORM_NAME = 'OmniRank · 全域上榜';
const AGENT_132 = '代理商132品牌';
const AGENT_456 = '服务商456品牌';
const AGENT_789 = '服务商789品牌';
const VIEWPORTS = [
  { width: 320, height: 720 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 900 },
];

type AccountKey = 'agent132' | 'agent456' | 'agent789';

const users = {
  agent132: {
    id: 132,
    username: 'agent132',
    display_name: AGENT_132,
    is_admin: true,
    is_active: 1,
    must_change_password: 0,
    roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
    permissions: ['users:read', 'settings:read'],
    client_brand_ids: [],
    agent_level: 2,
  },
  agent456: {
    id: 456,
    username: 'agent456',
    display_name: AGENT_456,
    is_admin: false,
    is_active: 1,
    must_change_password: 0,
    roles: [{ id: 2, name: 'agent', display_name: '服务商' }],
    permissions: [],
    client_brand_ids: [],
    agent_level: 1,
  },
  agent789: {
    id: 789,
    username: 'agent789',
    display_name: AGENT_789,
    is_admin: false,
    is_active: 1,
    must_change_password: 0,
    roles: [{ id: 2, name: 'agent', display_name: '服务商' }],
    permissions: [],
    client_brand_ids: [],
    agent_level: 2,
  },
};

const branding = {
  agent132: {
    success: true,
    data: {
      company_name: AGENT_132,
      product_name: '132营销云',
      logo_url: 'https://cdn.example/132.png',
      configuration_status: 'approved',
      display_scope: 'platform',
      customer_branding_active: true,
      backoffice_branding_active: false,
      backoffice_brand_allowed: false,
      backoffice_brand_unlocked: false,
      brand_version: 8,
    },
  },
  agent456: {
    success: true,
    data: {
      company_name: AGENT_456,
      product_name: '456营销云',
      logo_url: 'https://cdn.example/456.png',
      configuration_status: 'approved',
      display_scope: 'platform',
      customer_branding_active: true,
      backoffice_branding_active: false,
      backoffice_brand_allowed: false,
      backoffice_brand_unlocked: false,
      brand_version: 4,
    },
  },
  agent789: {
    success: true,
    data: {
      company_name: AGENT_789,
      product_name: '789营销云',
      logo_url: 'https://cdn.example/789.png',
      configuration_status: 'approved',
      display_scope: 'approved_whitelabel',
      customer_branding_active: true,
      backoffice_branding_active: true,
      backoffice_brand_allowed: true,
      backoffice_brand_unlocked: true,
      brand_version: 6,
    },
  },
};

function quotePayload(companyName: string, logoUrl: string) {
  return {
    status: 'success',
    quote: {
      services: [{ name: 'GEO 商业意图优化', quantity: 1, unit_price: 6800, subtotal: 6800 }],
      total_price: 6800,
      whitelabel: {
        company_name: companyName,
        logo_url: logoUrl,
        slogan: companyName === PLATFORM_NAME ? undefined : '本地增长服务商',
      },
      created_at: '2026-07-23T12:00:00+08:00',
    },
  };
}

async function fulfillJson(route: Route, body: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  });
}

async function installApiMocks(page: Page, initialAccount: AccountKey = 'agent132') {
  let currentAccount = initialAccount;
  let suspendedQuote = false;

  await page.route('https://cdn.example/**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'image/png',
      body: Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
        'base64',
      ),
    }),
  );

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (request.method() === 'OPTIONS') {
      await route.fulfill({ status: 204 });
      return;
    }
    if (path === '/api/auth/me') {
      await fulfillJson(route, { success: true, user: users[currentAccount] });
      return;
    }
    if (path === '/api/auth/refresh') {
      await fulfillJson(route, { success: true, token: `token-${currentAccount}` });
      return;
    }
    if (path === '/api/referral/whitelabel') {
      await fulfillJson(route, branding[currentAccount]);
      return;
    }
    if (path.startsWith('/api/public/quote/')) {
      await fulfillJson(
        route,
        suspendedQuote
          ? quotePayload(PLATFORM_NAME, '/logo-192.png')
          : quotePayload(AGENT_132, 'https://cdn.example/132.png'),
      );
      return;
    }
    if (path.includes('/agreement')) {
      await fulfillJson(route, { success: true, required: false, agreements: [] });
      return;
    }
    if (path.includes('/wallet')) {
      await fulfillJson(route, {
        success: true,
        data: { paid_points: 0, bonus_points: 0, frozen_points: 0, total_points: 0 },
      });
      return;
    }
    await fulfillJson(route, {
      success: true,
      status: 'success',
      data: {},
      items: [],
      total: 0,
      notifications: [],
    });
  });

  return {
    setAccount(account: AccountKey) {
      currentAccount = account;
    },
    setSuspendedQuote(value: boolean) {
      suspendedQuote = value;
    },
  };
}

async function seedSession(page: Page, token = 'token-agent132') {
  await page.addInitScript((value) => {
    localStorage.setItem('omnirank_token', value);
  }, token);
}

function collectTerminalErrors(page: Page) {
  const errors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console:${message.text()}`);
  });
  page.on('pageerror', (error) => errors.push(`pageerror:${error.message}`));
  return errors;
}

test.describe('板块 C · 白标作用域真实路由', () => {
  test('客户公开报价在四个视口显示服务商品牌', async ({ page }) => {
    await installApiMocks(page);
    const errors = collectTerminalErrors(page);

    for (const viewport of VIEWPORTS) {
      await page.setViewportSize(viewport);
      await page.goto(`/q/quote-132?v=${viewport.width}`);
      await expect(page.getByText(AGENT_132).first()).toBeVisible();
      await expect(page.getByText('GEO 商业意图优化')).toBeVisible();
      await expect(page.locator('img[alt="代理商132品牌"]').first()).toBeVisible();
    }

    expect(errors).toEqual([]);
  });

  test('未获后台换肤授权的服务商在真实工作台只显示平台品牌', async ({ page }) => {
    await seedSession(page, 'token-agent456');
    await installApiMocks(page, 'agent456');
    const errors = collectTerminalErrors(page);

    await page.setViewportSize({ width: 1440, height: 900 });
    const brandingResponse = page.waitForResponse(
      (response) => response.url().includes('/api/referral/whitelabel') && response.status() === 200,
    );
    await page.goto('/agent/whitelabel');
    await brandingResponse;

    const sidebarLogo = page.locator('aside img').first();
    await expect(sidebarLogo).toHaveAttribute('alt', PLATFORM_NAME);
    await expect(sidebarLogo).not.toHaveAttribute('alt', AGENT_456);
    await expect(page).not.toHaveTitle(new RegExp(AGENT_456));
    expect(errors).toEqual([]);
  });

  test('已授权 agent → admin 真实 SPA 路由切换立即清除后台品牌', async ({ page }) => {
    await seedSession(page, 'token-agent789');
    await installApiMocks(page, 'agent789');
    const errors = collectTerminalErrors(page);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/agent/whitelabel');
    const sidebarLogo = page.locator('aside img').first();
    await expect(sidebarLogo).toHaveAttribute('alt', AGENT_789);

    await page.evaluate(() => {
      window.history.pushState({}, '', '/admin/users');
      window.dispatchEvent(new PopStateEvent('popstate'));
    });
    await expect(page).toHaveURL(/\/admin\/users$/);
    await expect(sidebarLogo).toHaveAttribute('alt', PLATFORM_NAME);
    await expect(page).not.toHaveTitle(new RegExp(AGENT_789));
    expect(errors).toEqual([]);
  });

  test('同一 SPA 换账号轮换 authority 后不复用上一账号品牌缓存', async ({ page }) => {
    await seedSession(page, 'token-agent789');
    const api = await installApiMocks(page, 'agent789');
    const errors = collectTerminalErrors(page);

    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto('/agent/whitelabel');
    const sidebarLogo = page.locator('aside img').first();
    await expect(sidebarLogo).toHaveAttribute('alt', AGENT_789);

    api.setAccount('agent456');
    await page.evaluate(() => {
      const oldValue = localStorage.getItem('omnirank_token');
      const newValue = 'token-agent456';
      localStorage.setItem('omnirank_token', newValue);
      window.dispatchEvent(new StorageEvent('storage', {
        key: 'omnirank_token',
        oldValue,
        newValue,
        storageArea: localStorage,
      }));
    });

    await expect(sidebarLogo).toHaveAttribute('alt', PLATFORM_NAME);
    await expect(sidebarLogo).not.toHaveAttribute('alt', AGENT_789);
    await expect(sidebarLogo).not.toHaveAttribute('alt', AGENT_456);
    expect(errors).toEqual([]);
  });

  test('暂停态公开报价回落平台且不残留服务商品牌', async ({ page }) => {
    const api = await installApiMocks(page);
    api.setSuspendedQuote(true);
    const errors = collectTerminalErrors(page);

    await page.goto('/q/suspended-132');
    await expect(page.getByText(PLATFORM_NAME).first()).toBeVisible();
    await expect(page.getByText(AGENT_132)).toHaveCount(0);
    await expect(page.locator('img[alt="OmniRank · 全域上榜"]').first()).toBeVisible();
    expect(errors).toEqual([]);
  });
});
