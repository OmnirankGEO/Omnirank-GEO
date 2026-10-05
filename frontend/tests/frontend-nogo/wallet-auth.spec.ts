import { expect, test, type Page, type Route } from './_fixtures';

const normalUser = {
  id: 103,
  username: 'qa-normal',
  display_name: '普通职员',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 0,
  roles: [{ id: 3, name: 'user', display_name: '普通用户' }],
  permissions: [],
  client_brand_ids: [],
};

const agentUser = {
  ...normalUser,
  id: 102,
  username: 'qa-agent',
  display_name: '服务商',
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '代理' }],
  permissions: ['quote:write', 'settings:read'],
};

const adminUser = {
  ...normalUser,
  id: 101,
  username: 'qa-admin',
  display_name: '管理员',
  is_admin: true,
  roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
  permissions: ['settings:write', 'users:write'],
};

const readyWalletData = {
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
};

async function installSession(page: Page, user = normalUser) {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'local-intercept-only'));
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user } });
      return;
    }
    if (path === '/api/auth/legal-agreements/accept') {
      await route.fulfill({ json: {
        success: true,
        acceptance: {
          acceptance_id: 'qa-purchase-acceptance',
          agreement_version: 'user-v2.0',
          content_hash: 'qa-content-hash',
        },
      } });
      return;
    }
    if (path === '/api/wallet/transactions') {
      await route.fulfill({ json: { items: [], total: 0, page: 1, limit: 20 } });
      return;
    }
    if (path === '/api/wallet/pricing') {
      await route.fulfill({ json: { success: true, data: [{
        feature_code: 'geo_diagnosis', feature_name: 'GEO专项诊断', cost_points: 650,
        cost_compute: 0, requires_paid_points: true, is_active: true,
      }] } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });
}

async function installRetailPricing(page: Page, displayName = '测试算力包') {
  await page.route('**/api/pricing/retail/catalog', route => route.fulfill({ json: {
    success: true,
    data: {
      seller_label: '平台统一收款',
      service_account_code: 'SV-QA',
      catalog_version: 'qa-retail-v1',
      items: [{
        product_code: 'qa-credit-pack',
        display_name: displayName,
        final_price_cents: 1000,
        points_granted: 1300,
        bonus_points: 0,
        usage_examples: ['诊断', '写作'],
      }],
    },
  } }));
  await page.route('**/api/pricing/retail/quote', route => route.fulfill({ json: {
    success: true,
    data: {
      quote_id: 'qa-retail-quote-1',
      final_price_cents: 1000,
      points_granted: 1300,
      bonus_points: 0,
      currency: 'CNY',
      price_valid_until: '2099-01-01T00:00:00Z',
      service_account_code: 'SV-QA',
      seller_label: '平台统一收款',
    },
  } }));
}

test('首次钱包失败不把未知余额显示成 0，并可重试', async ({ page }, testInfo) => {
  await installSession(page);
  await page.route('**/api/wallet', route => route.fulfill({ status: 500, json: { detail: { message: 'temporary' } } }));

  await page.goto('/wallet');

  await expect(page.getByRole('heading', { name: '钱包与交易记录' })).toBeVisible();
  await expect(page.getByText('余额暂时无法确认')).toBeVisible();
  await expect(page.getByText('这不代表余额为 0')).toBeVisible();
  await expect(page.getByRole('button', { name: '重新读取余额' })).toBeVisible();
  await expect(page.getByText('可用合计')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('wallet-first-error.png'), fullPage: true });
});

test('钱包 429 不会把服务商路由身份降级成普通用户', async ({ page }, testInfo) => {
  await installSession(page, agentUser);
  await page.route('**/api/wallet', route => route.fulfill({ status: 429, json: { detail: '请求太频繁' } }));

  await page.goto('/agent/inventory');

  await expect(page).toHaveURL(/\/agent\/inventory/);
  await expect(page).not.toHaveURL(/\/landing/);
  await page.screenshot({ path: testInfo.outputPath('agent-wallet-429.png'), fullPage: true });
});

test('普通用户直达服务商路由时得到解释性阻断', async ({ page }, testInfo) => {
  await installSession(page, normalUser);
  await page.route('**/api/wallet', route => route.fulfill({ status: 503, json: { detail: 'temporary' } }));

  await page.goto('/agent/inventory');

  await expect(page).toHaveURL(/\/agent\/inventory/);
  await expect(page.getByRole('heading', { name: '当前账号没有服务商权限' })).toBeVisible();
  await expect(page.getByRole('link', { name: '返回主菜单' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('normal-user-agent-denied.png'), fullPage: true });
});

test('无业务 code 的 401 只刷新一次，恢复成功后保留当前任务', async ({ page }) => {
  let meCalls = 0;
  let refreshCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'local-intercept-only'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      meCalls += 1;
      if (meCalls === 1) {
        await route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
      } else {
        await route.fulfill({ json: { success: true, user: normalUser } });
      }
      return;
    }
    if (path === '/api/auth/refresh') {
      refreshCalls += 1;
      await route.fulfill({ json: { success: true, token: 'local-refreshed-only', user: normalUser } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');

  await expect(page.getByRole('heading', { name: '钱包与交易记录' })).toBeVisible();
  expect(refreshCalls).toBe(1);
  expect(meCalls).toBeGreaterThanOrEqual(2);
});

test('无业务 code 的 401 刷新后仍失败时才按会话过期处理', async ({ page }) => {
  let refreshCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'local-intercept-only'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
      return;
    }
    if (path === '/api/auth/refresh') {
      refreshCalls += 1;
      await route.fulfill({ json: { success: true, token: 'local-refreshed-only' } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');
  await expect(page).toHaveURL(/\/login/);
  expect(refreshCalls).toBe(1);
});

test('auth/me 429 只做一次自动重试并保留当前页面', async ({ page }) => {
  let meCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'local-intercept-only'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      meCalls += 1;
      await route.fulfill({ status: 429, json: { detail: 'too many' } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');
  await expect(page.getByText('暂时无法确认登录状态')).toBeVisible({ timeout: 6_000 });
  await expect(page).toHaveURL(/\/wallet/);
  expect(meCalls).toBe(2);
});

test('报告导出首次 500 后复用同一幂等键且不会再次模拟扣费', async ({ page }) => {
  const exportKeys: string[] = [];
  let exportCalls = 0;
  let simulatedChargeCount = 0;

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-intercept-only');
    const originalGetItem = Storage.prototype.getItem;
    const originalSetItem = Storage.prototype.setItem;
    const originalRemoveItem = Storage.prototype.removeItem;
    const isExportAttemptKey = (key: string) =>
      key.startsWith('omnirank:report-export-attempt:');
    Storage.prototype.getItem = function (key: string) {
      if (isExportAttemptKey(String(key))) throw new Error('storage blocked for test');
      return originalGetItem.call(this, key);
    };
    Storage.prototype.setItem = function (key: string, value: string) {
      if (isExportAttemptKey(String(key))) throw new Error('storage blocked for test');
      return originalSetItem.call(this, key, value);
    };
    Storage.prototype.removeItem = function (key: string) {
      if (isExportAttemptKey(String(key))) throw new Error('storage blocked for test');
      return originalRemoveItem.call(this, key);
    };
  });

  await page.route('**/api/**', async route => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: normalUser } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    if (path === '/api/diagnosis/901') {
      await route.fulfill({ json: {
        id: 901,
        brand_id: 77,
        brand_name: '幂等导出测试品牌',
        industry: '测试行业',
        level: '边缘级',
        total_score: 45,
        created_at: '2026-07-19T00:00:00Z',
      } });
      return;
    }
    if (path === '/api/diagnosis/901/content') {
      await route.fulfill({ json: { content: '# 客户安全报告', version: 'v2' } });
      return;
    }
    if (path === '/api/diagnosis/901/type') {
      await route.fulfill({ json: { scope: 'geo', is_legacy: false } });
      return;
    }
    if (path === '/api/brands/77/diagnosis-history') {
      await route.fulfill({ json: { success: true, items: [] } });
      return;
    }
    if (path === '/api/geo-observation/brands/77/summary') {
      await route.fulfill({
        status: 503,
        json: { detail: { code: 'OBSERVATION_NOT_READY', message: '观测数据尚未就绪' } },
      });
      return;
    }
    if (path === '/api/diagnosis/901/report-v2.pdf') {
      exportCalls += 1;
      const key = route.request().headers()['x-report-export-idempotency-key'];
      exportKeys.push(key || '');
      if (exportCalls === 1) {
        simulatedChargeCount += 1;
        await route.fulfill({
          status: 500,
          json: {
            detail: {
              code: 'REPORT_EXPORT_GENERATION_FAILED',
              message: '报告生成失败，请稍后重试。',
            },
          },
        });
      } else {
        await route.fulfill({
          status: 409,
          json: {
            detail: {
              code: 'IDEMPOTENCY_CHARGE_REFUND_PENDING',
              message: '该次扣费正在退款处理中，请稍后重试。',
            },
          },
        });
      }
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/diagnosis/report/901');
  await expect(page.getByRole('heading', { name: '幂等导出测试品牌' })).toBeVisible();
  await page.getByRole('button', { name: '更多操作' }).click();
  await page.getByRole('menuitem', { name: '导出报告' }).click();

  const generate = page.getByRole('button', { name: '生成并下载' });
  await generate.click();
  await expect(page.getByText('报告生成失败，请稍后重试。')).toBeVisible();
  await generate.click();
  await expect(page.getByText('该次扣费正在退款处理中，请稍后重试。')).toBeVisible();

  expect(exportCalls).toBe(2);
  expect(simulatedChargeCount).toBe(1);
  expect(exportKeys[0]).toMatch(/^report-export:901:/);
  expect(exportKeys[1]).toBe(exportKeys[0]);
});

test('旧账号 auth/me 响应不能覆盖在途登录的新账号身份', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'old-session'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      if (route.request().headers().authorization === 'Bearer old-session') {
        await new Promise(resolve => setTimeout(resolve, 700));
        await route.fulfill({ json: { success: true, user: agentUser } });
      } else {
        await route.fulfill({ json: { success: true, user: normalUser } });
      }
      return;
    }
    if (path === '/api/auth/login') {
      await route.fulfill({ json: { success: true, token: 'new-session', user: normalUser } });
      return;
    }
    if (path === '/api/c-end/settings/mode') {
      await route.fulfill({ json: { success: true, agent_level: 0, recommended_route: '/' } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ status: 503, json: { detail: 'temporary' } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/login');
  await page.getByPlaceholder('请输入手机号或用户名').fill('new-user');
  await page.getByPlaceholder('请输入密码').fill('local-test-only');
  await page.getByRole('button', { name: '登 录', exact: true }).click();
  await expect.poll(() => page.evaluate(() => localStorage.getItem('omnirank_token'))).toBe('new-session');
  await page.waitForTimeout(800);
  const returningUser = page.getByRole('button', { name: /我已经用过/ });
  if (await returningUser.isVisible().catch(() => false)) await returningUser.click();
  await page.evaluate(() => {
    window.history.pushState({}, '', '/agent/inventory');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });

  await expect(page.getByRole('heading', { name: '当前账号没有服务商权限' })).toBeVisible();
});

test('密码登录在业务路由前用 auth/me 补齐服务商身份', async ({ page }) => {
  let meCalls = 0;
  let meResolved = false;
  let modeCalledBeforeMe = false;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/login') {
      await route.fulfill({
        json: {
          success: true,
          token: 'password-agent-session',
          user: { ...normalUser, id: agentUser.id, username: agentUser.username, agent_level: undefined },
        },
      });
      return;
    }
    if (path === '/api/auth/me') {
      meCalls += 1;
      expect(route.request().headers().authorization).toBe('Bearer password-agent-session');
      meResolved = true;
      await route.fulfill({ json: { success: true, user: agentUser } });
      return;
    }
    if (path === '/api/c-end/settings/mode') {
      modeCalledBeforeMe = !meResolved;
      await route.fulfill({
        json: { success: true, agent_level: 1, preferred_mode: null, recommended_route: '/' },
      });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/login');
  await page.getByPlaceholder('请输入手机号或用户名').fill('13800000000');
  await page.getByPlaceholder('请输入密码').fill('local-test-only');
  await page.getByRole('button', { name: '登 录', exact: true }).click();

  await expect.poll(() => meCalls).toBe(1);
  expect(modeCalledBeforeMe).toBe(false);
  await expect(page.getByText('经营后台', { exact: true })).toBeVisible();
});

test('短信登录在业务路由前用 auth/me 补齐服务商身份', async ({ page }) => {
  let meCalls = 0;
  let meResolved = false;
  let modeCalledBeforeMe = false;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/login-sms') {
      await route.fulfill({
        json: {
          success: true,
          token: 'sms-agent-session',
          user: { id: agentUser.id, display_name: agentUser.display_name, phone: '138****0000' },
        },
      });
      return;
    }
    if (path === '/api/auth/me') {
      meCalls += 1;
      expect(route.request().headers().authorization).toBe('Bearer sms-agent-session');
      meResolved = true;
      await route.fulfill({ json: { success: true, user: agentUser } });
      return;
    }
    if (path === '/api/c-end/settings/mode') {
      modeCalledBeforeMe = !meResolved;
      await route.fulfill({
        json: { success: true, agent_level: 1, preferred_mode: null, recommended_route: '/' },
      });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/login');
  await page.getByRole('button', { name: '忘记密码？使用验证码登录' }).click();
  await page.getByPlaceholder('请输入手机号').fill('13800000000');
  await page.getByPlaceholder('请输入6位验证码').fill('123456');
  await page.getByRole('button', { name: '登 录', exact: true }).click();

  await expect.poll(() => meCalls).toBe(1);
  expect(modeCalledBeforeMe).toBe(false);
  await expect(page.getByText('经营后台', { exact: true })).toBeVisible();
});

test('authFetch 安全 GET 重放等待新 token 的 auth/me 权威身份完成', async ({ page }) => {
  let probeCalls = 0;
  let rotatedMeResolved = false;
  let replayBeforeMe = false;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'fetch-refresh-old'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      if (route.request().headers().authorization === 'Bearer fetch-refresh-new') {
        await new Promise(resolve => setTimeout(resolve, 250));
        rotatedMeResolved = true;
        await route.fulfill({ json: { success: true, user: agentUser } });
      } else {
        await route.fulfill({ json: { success: true, user: normalUser } });
      }
      return;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'fetch-refresh-new' } });
      return;
    }
    if (path === '/api/fetch-refresh-authority-probe') {
      probeCalls += 1;
      if (probeCalls === 1) {
        await route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      } else {
        replayBeforeMe = !rotatedMeResolved;
        await route.fulfill({ json: { success: true } });
      }
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');
  const status = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      authFetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    }>;
    return (await (await importer()).authFetch('/api/fetch-refresh-authority-probe')).status;
  });

  expect(status).toBe(200);
  expect(probeCalls).toBe(2);
  expect(replayBeforeMe).toBe(false);
});

test('并发旧 token 安全 GET 也必须等待同一个 auth/me 权威确认', async ({ page }) => {
  const probeCalls = { first: 0, delayed: 0 };
  let rotatedMeResolved = false;
  let replayBeforeMe = false;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'concurrent-old'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      if (route.request().headers().authorization === 'Bearer concurrent-new') {
        await new Promise(resolve => setTimeout(resolve, 250));
        rotatedMeResolved = true;
        await route.fulfill({ json: { success: true, user: agentUser } });
      } else {
        await route.fulfill({ json: { success: true, user: normalUser } });
      }
      return;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'concurrent-new' } });
      return;
    }
    if (path === '/api/concurrent-first-probe' || path === '/api/concurrent-delayed-probe') {
      const key = path.includes('delayed') ? 'delayed' : 'first';
      probeCalls[key] += 1;
      if (probeCalls[key] === 1) {
        if (key === 'delayed') await new Promise(resolve => setTimeout(resolve, 75));
        await route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      } else {
        replayBeforeMe = replayBeforeMe || !rotatedMeResolved;
        await route.fulfill({ json: { success: true } });
      }
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');
  const statuses = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      authFetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    }>;
    const { authFetch } = await importer();
    const responses = await Promise.all([
      authFetch('/api/concurrent-first-probe'),
      authFetch('/api/concurrent-delayed-probe'),
    ]);
    return responses.map(response => response.status);
  });

  expect(statuses).toEqual([200, 200]);
  expect(probeCalls).toEqual({ first: 2, delayed: 2 });
  expect(replayBeforeMe).toBe(false);
});

test('authFetch 的 auth/me 失败时业务 GET 零重放', async ({ page }) => {
  let probeCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'me-failure-old'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      if (route.request().headers().authorization === 'Bearer me-failure-new') {
        await route.fulfill({ status: 503, json: { detail: 'temporary' } });
      } else {
        await route.fulfill({ json: { success: true, user: normalUser } });
      }
      return;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'me-failure-new' } });
      return;
    }
    if (path === '/api/me-failure-replay-probe') {
      probeCalls += 1;
      await route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');
  const status = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      authFetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    }>;
    return (await (await importer()).authFetch('/api/me-failure-replay-probe')).status;
  });

  expect(status).toBe(401);
  expect(probeCalls).toBe(1);
});

test('token-refreshed 监听器缺失时业务 GET 零重放', async ({ page }) => {
  let probeCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'listener-old'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: normalUser } });
      return;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'listener-new' } });
      return;
    }
    if (path === '/api/listener-missing-replay-probe') {
      probeCalls += 1;
      await route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');
  const status = await page.evaluate(async () => {
    const originalDispatch = window.dispatchEvent.bind(window);
    window.dispatchEvent = (() => true) as typeof window.dispatchEvent;
    try {
      const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
        authFetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
      }>;
      return (await (await importer()).authFetch('/api/listener-missing-replay-probe')).status;
    } finally {
      window.dispatchEvent = originalDispatch;
    }
  });

  expect(status).toBe(401);
  expect(probeCalls).toBe(1);
});

test('auth/me 确认期间 token 被替换时业务 GET 零重放', async ({ page }) => {
  let probeCalls = 0;
  let refreshedMeStarted = false;
  let releaseRefreshedMe!: () => void;
  const refreshedMeGate = new Promise<void>(resolve => { releaseRefreshedMe = resolve; });
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'superseded-old'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      if (route.request().headers().authorization === 'Bearer superseded-refresh') {
        refreshedMeStarted = true;
        await refreshedMeGate;
        await route.fulfill({ json: { success: true, user: agentUser } });
      } else {
        await route.fulfill({ json: { success: true, user: normalUser } });
      }
      return;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'superseded-refresh' } });
      return;
    }
    if (path === '/api/superseded-replay-probe') {
      probeCalls += 1;
      await route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: readyWalletData } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/wallet');
  await page.evaluate(() => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      authFetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    }>;
    (window as typeof window & { __supersededProbe?: Promise<number> }).__supersededProbe = (async () =>
      (await (await importer()).authFetch('/api/superseded-replay-probe')).status
    )();
  });
  await expect.poll(() => refreshedMeStarted).toBe(true);
  await page.evaluate(() => localStorage.setItem('omnirank_token', 'newer-session'));
  releaseRefreshedMe();
  const status = await page.evaluate(() =>
    (window as typeof window & { __supersededProbe: Promise<number> }).__supersededProbe
  );

  expect(status).toBe(401);
  expect(probeCalls).toBe(1);
  expect(await page.evaluate(() => localStorage.getItem('omnirank_token'))).toBe('newer-session');
});

test('修改密码换发 token 必须等待新 token 的 auth/me 后才离开页面', async ({ page }) => {
  let rotatedMeStarted = false;
  let releaseRotatedMe: (() => void) | undefined;
  const rotatedMeGate = new Promise<void>(resolve => {
    releaseRotatedMe = resolve;
  });
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'password-old-session'));
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    const authorization = route.request().headers().authorization;
    if (path === '/api/auth/me') {
      if (authorization === 'Bearer password-rotated-session') {
        rotatedMeStarted = true;
        await rotatedMeGate;
        await route.fulfill({ json: { success: true, user: agentUser } });
      } else {
        await route.fulfill({ json: { success: true, user: normalUser } });
      }
      return;
    }
    if (path === '/api/auth/change-password') {
      await route.fulfill({
        json: { success: true, token: 'password-rotated-session' },
      });
      return;
    }
    if (path === '/api/c-end/settings/mode') {
      await route.fulfill({
        json: { success: true, agent_level: 1, preferred_mode: null, recommended_route: '/' },
      });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/change-password');
  await page.getByPlaceholder('原密码').fill('old-password');
  await page.getByPlaceholder('至少 6 位').fill('new-password');
  await page.getByPlaceholder('再次输入新密码').fill('new-password');
  await page.getByRole('button', { name: '确认修改' }).click();

  await expect.poll(() => rotatedMeStarted).toBe(true);
  await expect(page).toHaveURL(/\/change-password/);
  expect(await page.evaluate(() => localStorage.getItem('omnirank_token')))
    .toBe('password-rotated-session');
  releaseRotatedMe?.();
  await expect(page).toHaveURL(/\/$/);
});

test('代管额度查询失败不能把钱包提交为 ready 或伪装成 0', async ({ page }) => {
  await installSession(page);
  await page.route('**/api/wallet', route => route.fulfill({
    json: {
      success: true,
      data: {
        ...readyWalletData,
        customer_credit_status: 'unavailable',
        customer_credit: null,
      },
    },
  }));

  await page.goto('/wallet');

  await expect(page.getByRole('heading', { name: '余额暂时无法确认' })).toBeVisible();
  await expect(page.getByText('可用合计')).toHaveCount(0);
});

test('非幂等 POST 裸 401 只刷新身份但绝不自动重放', async ({ page }) => {
  let writeCalls = 0;
  let refreshCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'write-probe-session'));
  await page.route('**/api/nogo-write-probe', async route => {
    writeCalls += 1;
    await route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
  });
  await page.route('**/api/auth/refresh', async route => {
    refreshCalls += 1;
    await route.fulfill({ json: { success: true, token: 'write-probe-refreshed' } });
  });
  await page.route('**/api/auth/me', route => route.fulfill({ json: { success: true, user: normalUser } }));
  await page.route('**/api/wallet', route => route.fulfill({ json: { success: true, data: readyWalletData } }));

  await page.goto('/login');
  const status = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      authFetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
    }>;
    const { authFetch } = await importer();
    const response = await authFetch('/api/nogo-write-probe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'probe' }),
    });
    return response.status;
  });

  expect(status).toBe(401);
  expect(writeCalls).toBe(1);
  expect(refreshCalls).toBe(1);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('omnirank_token')))
    .toBe('write-probe-refreshed');
});

test('AuthContext 全局 axios 对非幂等 POST 也不自动重放', async ({ page }) => {
  let writeCalls = 0;
  let refreshCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'axios-write-session'));
  await page.route('**/api/**', route => route.fulfill({ json: { success: true, data: {}, items: [] } }));
  await page.route('**/api/auth/me', route => route.fulfill({ json: { success: true, user: normalUser } }));
  await page.route('**/api/wallet', route => route.fulfill({ json: { success: true, data: readyWalletData } }));
  await page.route('**/api/nogo-axios-write-probe', route => {
    writeCalls += 1;
    return route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
  });
  await page.route('**/api/auth/refresh', route => {
    refreshCalls += 1;
    return route.fulfill({ json: { success: true, token: 'axios-write-refreshed', user: normalUser } });
  });

  await page.goto('/wallet');
  await expect(page.getByRole('heading', { name: '钱包与交易记录' })).toBeVisible();
  const status = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      default: { post: (url: string, body: unknown) => Promise<unknown> };
    }>;
    const axios = (await importer()).default;
    try {
      await axios.post('/api/nogo-axios-write-probe', { action: 'probe' });
      return 200;
    } catch (error: unknown) {
      return (error as { response?: { status?: number } }).response?.status ?? 0;
    }
  });

  expect(status).toBe(401);
  expect(writeCalls).toBe(1);
  expect(refreshCalls).toBe(1);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('omnirank_token')))
    .toBe('axios-write-refreshed');
});

test('AuthContext 全局 axios 的 refresh 503 保留登录态', async ({ page }) => {
  let readCalls = 0;
  let refreshCalls = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'axios-soft-session'));
  await page.route('**/api/**', route => route.fulfill({ json: { success: true, data: {}, items: [] } }));
  await page.route('**/api/auth/me', route => route.fulfill({ json: { success: true, user: normalUser } }));
  await page.route('**/api/wallet', route => route.fulfill({ json: { success: true, data: readyWalletData } }));
  await page.route('**/api/nogo-axios-read-probe', route => {
    readCalls += 1;
    return route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
  });
  await page.route('**/api/auth/refresh', route => {
    refreshCalls += 1;
    return route.fulfill({ status: 503, json: { detail: 'temporary' } });
  });

  await page.goto('/wallet');
  await expect(page.getByRole('heading', { name: '钱包与交易记录' })).toBeVisible();
  const status = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      default: { get: (url: string) => Promise<unknown> };
    }>;
    const axios = (await importer()).default;
    try {
      await axios.get('/api/nogo-axios-read-probe');
      return 200;
    } catch (error: unknown) {
      return (error as { response?: { status?: number } }).response?.status ?? 0;
    }
  });

  expect(status).toBe(401);
  expect(readCalls).toBe(1);
  expect(refreshCalls).toBe(1);
  await expect.poll(() => page.evaluate(() => localStorage.getItem('omnirank_token')))
    .toBe('axios-soft-session');
});

for (const refreshStatus of [429, 503]) {
  test(`业务裸 401 后 refresh ${refreshStatus} 保留登录态且不重放`, async ({ page }) => {
    let readCalls = 0;
    let refreshCalls = 0;
    await page.addInitScript(() => localStorage.setItem('omnirank_token', 'soft-refresh-session'));
    await page.route('**/api/nogo-read-probe', async route => {
      readCalls += 1;
      await route.fulfill({ status: 401, json: { detail: 'Unauthorized' } });
    });
    await page.route('**/api/auth/refresh', async route => {
      refreshCalls += 1;
      await route.fulfill({ status: refreshStatus, json: { detail: 'temporary' } });
    });
    await page.route('**/api/auth/me', route => route.fulfill({ json: { success: true, user: normalUser } }));
    await page.route('**/api/wallet', route => route.fulfill({ json: { success: true, data: readyWalletData } }));

    await page.goto('/login');
    const status = await page.evaluate(async () => {
      const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
        authFetch: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
      }>;
      const { authFetch } = await importer();
      return (await authFetch('/api/nogo-read-probe')).status;
    });

    expect(status).toBe(401);
    expect(readCalls).toBe(1);
    expect(refreshCalls).toBe(1);
    await expect.poll(() => page.evaluate(() => localStorage.getItem('omnirank_token')))
      .toBe('soft-refresh-session');
  });
}

test('余额读取失败仍可进入购买算力并创建充值订单', async ({ page }) => {
  let orderCalls = 0;
  await installSession(page);
  await installRetailPricing(page, '故障恢复算力包');
  await page.route('**/api/wallet', route => route.fulfill({ status: 503, json: { detail: 'temporary' } }));
  await page.route('**/api/wallet/recharge', route => {
    orderCalls += 1;
    expect(route.request().postDataJSON()).toMatchObject({ price_quote_id: 'qa-retail-quote-1' });
    return route.fulfill({ json: { success: true, data: {
      order_id: 'recharge-despite-wallet-read-failure', amount_yuan: 10, base_points: 1300,
      bonus_points: 0, total_points: 1300, tier_label: '测试', actual_channel: 'wechat_native', status: 'pending',
    } } });
  });
  await page.route('**/api/wallet/order-status/**', route => route.fulfill({ json: { status: 'pending' } }));

  await page.goto('/wallet');
  await page.getByRole('button', { name: '充值', exact: true }).click();
  await expect(page).toHaveURL(/\/customer\/recharge/);
  await page.getByRole('checkbox', { name: '确认用户服务协议中的购买与退款规则' }).check();
  await page.getByRole('button', { name: '立即购买' }).click();

  await expect(page.getByRole('dialog')).toBeVisible();
  expect(orderCalls).toBe(1);
});

test('渠道等级接口挂起不阻塞服务商钱包 ready', async ({ page }) => {
  await installSession(page, agentUser);
  await page.route('**/api/wallet', route => route.fulfill({
    json: { success: true, data: { ...readyWalletData, paid_points: 456, total_recharged: 456 } },
  }));
  await page.route('**/api/agent/channel-tier/me', async route => {
    await new Promise(resolve => setTimeout(resolve, 20_000));
    await route.fulfill({ json: { success: true, channel_tier: { enabled: true } } });
  });

  await page.goto('/wallet');

  await expect(page.getByText('456', { exact: true }).first()).toBeVisible({ timeout: 2_000 });
  await expect(page.getByRole('heading', { name: '余额暂时无法确认' })).toHaveCount(0);
});

test('真正成功返回 0 时仍显示 0', async ({ page }, testInfo) => {
  await installSession(page);
  await page.route('**/api/wallet', route => route.fulfill({
    // 故意不返回 agent_level：钱包是资金 SSOT，不是身份 SSOT。
    json: { success: true, data: readyWalletData },
  }));

  await page.goto('/wallet');

  await expect(page.getByText('可用合计').locator('..')).toContainText('0');
  await expect(page.getByText('余额暂时无法确认')).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath('wallet-real-zero.png'), fullPage: true });
});

test('成功后失败保留上次余额并标记 stale', async ({ page }, testInfo) => {
  let shouldFail = false;
  await installSession(page);
  await page.route('**/api/wallet', route => shouldFail
    ? route.fulfill({ status: 503, json: { detail: 'temporary' } })
    : route.fulfill({ json: { success: true, data: { ...readyWalletData, paid_points: 777, total_recharged: 777 } } }));

  await page.goto('/wallet');
  await expect(page.getByText('777', { exact: true }).first()).toBeVisible();
  shouldFail = true;
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('wallet:refresh')));

  await expect(page.getByRole('heading', { name: '余额数据可能已过期' })).toBeVisible();
  await expect(page.getByText('777', { exact: true }).first()).toBeVisible();
  await expect(page.getByText(/最后更新/)).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('wallet-stale.png'), fullPage: true });
});

test('交易记录失败与余额失败独立建模', async ({ page }) => {
  await installSession(page);
  await page.route('**/api/wallet', route => route.fulfill({ status: 500, json: { detail: 'temporary' } }));
  await page.route('**/api/wallet/transactions**', route => route.fulfill({
    json: {
      items: [{ id: 'tx-1', created_at: '2026-07-14T12:00:00Z', type: 'recharge', feature_name: '历史充值', amount: 130, balance_after: 130 }],
      total: 1,
      page: 1,
      limit: 20,
    },
  }));

  await page.goto('/wallet');

  await expect(page.getByText('余额暂时无法确认')).toBeVisible();
  await expect(page.getByText('历史充值').first()).toBeVisible();
  await expect(page.getByText('交易记录暂时无法读取')).toHaveCount(0);
});

test('慢钱包请求保持单飞，完成后刷新读取最新余额', async ({ page }) => {
  let walletCalls = 0;
  let releaseFirst!: () => void;
  let secondSeen!: () => void;
  const firstGate = new Promise<void>(resolve => { releaseFirst = resolve; });
  const secondGate = new Promise<void>(resolve => { secondSeen = resolve; });
  await installSession(page);
  await page.route('**/api/wallet', async route => {
    walletCalls += 1;
    if (walletCalls === 1) {
      await firstGate;
      await route.fulfill({ json: { success: true, data: { ...readyWalletData, paid_points: 111, total_recharged: 111 } } });
      return;
    }
    await route.fulfill({ json: { success: true, data: { ...readyWalletData, paid_points: 222, total_recharged: 222 } } });
    secondSeen();
  });

  await page.goto('/wallet');
  await expect(page.getByRole('heading', { name: '钱包与交易记录' })).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('wallet:refresh')));
  await page.waitForTimeout(100);
  expect(walletCalls).toBe(1);
  releaseFirst();

  await expect(page.getByText('111', { exact: true }).first()).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('wallet:refresh')));
  await secondGate;

  await expect(page.getByText('222', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('111', { exact: true })).toHaveCount(0);
});

test('detail 对象被转成人类文案且不触发 React 页面错误', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await installSession(page);
  await page.route('**/api/wallet', route => route.fulfill({ status: 400, json: { detail: { message: '余额资料需要重新确认' } } }));

  await page.goto('/wallet');

  await expect(page.getByText(/余额请求未被接受/)).toBeVisible();
  await expect(page.getByText('[object Object]')).toHaveCount(0);
  expect(pageErrors).toEqual([]);
});

test('个人设置失败提供分类说明、重试和返回', async ({ page }) => {
  await installSession(page);
  await page.route('**/api/auth/profile', route => route.fulfill({ status: 429, json: { detail: 'too many' } }));

  await page.goto('/account/profile');

  await expect(page.getByRole('heading', { name: '个人设置暂时无法读取' })).toBeVisible();
  await expect(page.getByText(/请求太频繁/)).toBeVisible();
  await expect(page.getByRole('button', { name: '重试读取资料' })).toBeVisible();
  await expect(page.getByRole('button', { name: '返回主菜单' })).toBeVisible();
});

for (const profileFailure of [
  { status: 401, text: '登录状态已过期' },
  { status: 403, text: '没有查看个人设置的权限' },
  { status: 500, text: '个人设置服务暂时不可用' },
]) {
  test(`个人设置 HTTP ${profileFailure.status} 有分类恢复说明`, async ({ page }) => {
    await installSession(page);
    await page.route('**/api/auth/profile', route => route.fulfill({
      status: profileFailure.status,
      json: profileFailure.status === 401
        ? { code: 'PROFILE_AUTH_REQUIRED', detail: 'injected' }
        : { detail: 'injected' },
    }));
    await page.goto('/account/profile');
    await expect(page.getByText(new RegExp(profileFailure.text))).toBeVisible();
    await expect(page.getByRole('button', { name: '重试读取资料' })).toBeVisible();
  });
}

test('管理员 FAQ 反馈列表前端恢复并保留筛选', async ({ page }, testInfo) => {
  await installSession(page, adminUser);
  await page.route('**/api/admin/faq/feedback/counts**', route => route.fulfill({ json: { pending: 1, read: 0, done: 0, closed: 0 } }));
  await page.route('**/api/admin/faq/feedback?**', route => route.fulfill({ json: { items: [{
    id: 101,
    client_id: 'intercepted-feedback',
    faq_id: null,
    message: '反馈列表已恢复',
    urgency: 'high',
    kind: 'bug',
    screenshot_url: '',
    ai_answer: '',
    submitter_identity: 'normal_user',
    submitter_agent_level: 0,
    contact: '',
    user_id: 103,
    status: 'pending',
    admin_note: '',
    created_at: '2026-07-14T12:00:00Z',
    handled_at: null,
    handled_by: null,
    faq_question: null,
    user_username: 'qa-normal',
    user_display_name: '普通职员',
  }] } }));

  await page.goto('/admin/help-center');
  await expect(page.getByRole('heading', { name: '帮助中心管理' })).toBeVisible();
  await expect(page.getByText('反馈列表已恢复')).toBeVisible();
  await expect(page.getByText('卡死')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('admin-faq-feedback-recovered.png'), fullPage: true });
});

test('电视大屏二次门禁说明用途、获取对象和返回路径', async ({ page }) => {
  await installSession(page, adminUser);
  // 板块 D：/tv 已收紧为仅管理员，且进入页面前先探测服务端会话——
  // 无会话返回 401 SESSION_REQUIRED，页面落在密码门（本用例断言的对象）。
  await page.route('**/api/tv/access/session', route => route.fulfill({
    status: 401,
    json: {
      success: false,
      status: 'error',
      code: 'TV_ACCESS_SESSION_REQUIRED',
      message: '请输入大屏访问密码',
      request_id: 'tv-nogo-session-required',
    },
  }));
  await page.goto('/tv');

  await expect(page.getByRole('heading', { name: '运营监控中心' })).toBeVisible();
  await expect(page.getByText(/会议室和公共屏幕/)).toBeVisible();
  await expect(page.getByText(/系统管理员或运营负责人/)).toBeVisible();
  await expect(page.getByRole('link', { name: '返回管理后台' })).toBeVisible();
  await expect(page.getByLabel('大屏访问密码')).toBeVisible();
});

test('移动端支付 Dialog 有中文名称、描述且关闭目标至少 44px', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const dialogWarnings: string[] = [];
  page.on('console', message => {
    if (/Missing `Description`|aria-describedby/i.test(message.text())) dialogWarnings.push(message.text());
  });
  await installSession(page);
  await installRetailPricing(page);
  await page.route('**/api/wallet', route => route.fulfill({ json: { success: true, data: { ...readyWalletData, paid_points: 1000, total_recharged: 1000 } } }));
  await page.route('**/api/wallet/recharge', route => {
    expect(route.request().postDataJSON()).toMatchObject({ price_quote_id: 'qa-retail-quote-1' });
    return route.fulfill({ json: { success: true, data: {
    order_id: 'intercepted-order', amount_yuan: 10, base_points: 1300, bonus_points: 0, total_points: 1300,
    tier_label: '测试', actual_channel: 'wechat_native', status: 'pending',
    } } });
  });
  await page.route('**/api/wallet/order-status/**', route => route.fulfill({ json: { status: 'pending' } }));

  await page.goto('/customer/recharge');
  await page.getByRole('checkbox', { name: '确认用户服务协议中的购买与退款规则' }).check();
  await page.getByRole('button', { name: '立即购买' }).click();

  await expect(page.getByRole('dialog')).toBeVisible();
  await expect(page.getByText(/核对金额和到账算力/)).toBeVisible();
  const close = page.getByRole('button', { name: '关闭弹窗' });
  const box = await close.boundingBox();
  expect(box?.width).toBeGreaterThanOrEqual(44);
  expect(box?.height).toBeGreaterThanOrEqual(44);
  expect(dialogWarnings).toEqual([]);
  await page.screenshot({ path: testInfo.outputPath('mobile-payment-dialog.png'), fullPage: true });
});

test('普通客户自由充值固定实付金额并按账户价格展示到账算力', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await installSession(page);
  await installRetailPricing(page);
  await page.route('**/api/wallet', route => route.fulfill({ json: {
    success: true,
    data: { ...readyWalletData, paid_points: 1000, total_recharged: 1000 },
  } }));
  await page.route('**/api/pricing/retail/custom-amount-quote', route => {
    expect(route.request().postDataJSON()).toMatchObject({
      amount_cents: 100,
      digital_goods_acknowledged: true,
      terms_acceptance_id: 'qa-purchase-acceptance',
    });
    return route.fulfill({ json: { success: true, data: {
      quote_id: 'qa-retail-custom-cash-1',
      final_price_cents: 100,
      points_granted: 100,
      bonus_points: 0,
      currency: 'CNY',
      price_valid_until: '2099-01-01T00:00:00Z',
      amount_source: 'customer_entered_cash',
      seller_label: 'OmniRank 平台',
    } } });
  });
  await page.route('**/api/wallet/recharge', route => {
    expect(route.request().postDataJSON()).toMatchObject({
      price_quote_id: 'qa-retail-custom-cash-1',
      terms_acceptance_id: 'qa-purchase-acceptance',
    });
    return route.fulfill({ json: { success: true, data: {
      order_id: 'custom-cash-order',
      amount_yuan: 1,
      base_points: 100,
      bonus_points: 0,
      total_points: 100,
      tier_label: '自由充值',
      actual_channel: 'wechat_native',
      status: 'pending',
    } } });
  });
  await page.route('**/api/wallet/order-status/**', route => route.fulfill({ json: { status: 'pending' } }));

  await page.goto('/customer/recharge');
  await page.getByRole('checkbox', { name: '确认用户服务协议中的购买与退款规则' }).check();
  await page.getByLabel('自由充值金额').fill('1');
  await page.getByRole('button', { name: '计算到账' }).click();

  await expect(page.getByText('100 算力', { exact: true })).toBeVisible();
  await expect(page.getByText('实付 ¥1.00', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '按此金额充值' }).click();
  await expect(page.getByRole('dialog')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('customer-custom-cash-recharge.png'), fullPage: true });
});

test('服务商购买页只引导进货且不展示客户自由充值', async ({ page }) => {
  await installSession(page, agentUser);
  await page.addInitScript(() => {
    const now = new Date().toISOString();
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: now,
      last_updated_at: now,
    }));
  });
  await installRetailPricing(page);
  await page.route('**/api/wallet', route => route.fulfill({ json: {
    success: true,
    data: { ...readyWalletData, paid_points: 1000, total_recharged: 1000 },
  } }));

  await page.goto('/customer/recharge');

  await expect(page.getByRole('heading', { name: '服务商进货请前往算力库存' })).toBeVisible();
  await expect(page.getByRole('link', { name: /前往算力库存/ })).toHaveAttribute('href', '/agent/inventory');
  await expect(page.getByLabel('自由充值金额')).toHaveCount(0);
});

test('钱包失败后重试恢复为可信余额', async ({ page }) => {
  let shouldFail = true;
  await installSession(page);
  await page.route('**/api/wallet', route => shouldFail
    ? route.fulfill({ status: 503, json: { detail: 'temporary' } })
    : route.fulfill({ json: { success: true, data: { ...readyWalletData, paid_points: 456, total_recharged: 456 } } }));

  await page.goto('/wallet');
  await expect(page.getByText('余额暂时无法确认')).toBeVisible();
  shouldFail = false;
  await page.getByRole('button', { name: '重新读取余额' }).click();
  await expect(page.getByText('456', { exact: true }).first()).toBeVisible();
  await expect(page.getByText('余额暂时无法确认')).toHaveCount(0);
});

test('经典诊断页安全展示结构化算力不足错误且不触发 React 崩溃', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await installSession(page, adminUser);
  await page.route('**/api/diagnosis/start', route => route.fulfill({
    status: 402,
    json: {
      detail: {
        code: 'INSUFFICIENT_POINTS',
        message: '积分不足，需要 650',
        required: 650,
        available: 0,
      },
    },
  }));

  await page.goto('/diagnosis/new');
  await page.getByLabel('品牌名称 *').fill('结构化错误测试品牌');
  await page.getByLabel('所属行业 *').fill('科技服务');
  await page.getByLabel('核心关键词 *').fill('测试品牌哪家好');
  await page.getByRole('button', { name: '开始 GEO 诊断' }).click();

  await expect(page.getByText(/积分不足|算力不足/).first()).toBeVisible();
  await expect(page.getByText('[object Object]')).toHaveCount(0);
  await expect(page.getByText('页面出错了')).toHaveCount(0);
  await expect(page).toHaveURL(/\/diagnosis\/new/);
  expect(pageErrors).toEqual([]);
});

test('诊断报告页安全展示结构化加载错误且不触发 React 崩溃', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await installSession(page, adminUser);
  await page.route('**/api/diagnosis/901', route => route.fulfill({
    status: 503,
    json: {
      detail: {
        code: 'REPORT_TEMPORARILY_UNAVAILABLE',
        message: '报告暂时无法读取',
        required: false,
        available: false,
      },
    },
  }));

  await page.goto('/diagnosis/report/901');

  await expect(page.getByText(/报告暂时无法读取|加载报告失败/).first()).toBeVisible();
  await expect(page.getByText('[object Object]')).toHaveCount(0);
  await expect(page.getByText('页面出错了')).toHaveCount(0);
  expect(pageErrors).toEqual([]);
});

const walletFailures: Array<{
  name: string;
  fulfill?: Parameters<Route['fulfill']>[0];
  abort?: 'timedout' | 'internetdisconnected';
}> = [
  ...[400, 401, 402, 403, 404, 409, 422, 429, 500, 502, 503].map(status => ({
    name: `HTTP ${status}`,
    fulfill: {
      status,
      json: status === 401
        ? { code: 'WALLET_UNAVAILABLE', detail: { message: 'injected failure' } }
        : { detail: { message: 'injected failure' } },
    },
  })),
  { name: '超时', abort: 'timedout' },
  { name: '断网', abort: 'internetdisconnected' },
  { name: '空响应', fulfill: { status: 200, body: '' } },
  { name: '非法 JSON', fulfill: { status: 200, contentType: 'application/json', body: '{broken' } },
  { name: 'success=false', fulfill: { status: 200, json: { success: false, data: {} } } },
  { name: '字段缺失', fulfill: { status: 200, json: { success: true, data: { paid_points: 8 } } } },
  { name: 'detail 对象', fulfill: { status: 418, json: { detail: { message: '余额暂时不可用' } } } },
];

for (const failure of walletFailures) {
  test(`钱包异常矩阵：${failure.name}`, async ({ page }) => {
    const pageErrors: string[] = [];
    let walletCalls = 0;
    page.on('pageerror', error => pageErrors.push(error.message));
    await installSession(page);
    await page.route('**/api/wallet', async route => {
      walletCalls += 1;
      if (failure.abort) {
        await route.abort(failure.abort);
      } else {
        await route.fulfill(failure.fulfill!);
      }
    });

    await page.goto('/wallet');
    await expect(page.getByRole('heading', { name: '余额暂时无法确认' })).toBeVisible();
    await expect(page.getByText('可用合计')).toHaveCount(0);
    await expect(page.getByText('[object Object]')).toHaveCount(0);
    expect(pageErrors).toEqual([]);
    expect(walletCalls).toBeLessThanOrEqual(2);
  });
}

for (const status of [400, 401, 403, 409, 429, 500, 503]) {
  test(`服务商钱包 HTTP ${status} 仍保留服务商身份`, async ({ page }) => {
    await installSession(page, agentUser);
    await page.route('**/api/wallet', route => route.fulfill({
      status,
      json: status === 401 ? { code: 'WALLET_UNAVAILABLE', detail: 'injected' } : { detail: 'injected' },
    }));

    await page.goto('/agent/inventory');
    await expect(page).toHaveURL(/\/agent\/inventory/);
    await expect(page).not.toHaveURL(/\/landing/);
  });
}

for (const viewport of [
  { width: 1440, height: 900 },
  { width: 1366, height: 768 },
  { width: 1280, height: 700 },
  { width: 768, height: 1024 },
  { width: 390, height: 844 },
  { width: 375, height: 667 },
]) {
  test(`钱包错误态响应式 ${viewport.width}x${viewport.height}`, async ({ page }, testInfo) => {
    await page.setViewportSize(viewport);
    await installSession(page);
    await page.route('**/api/wallet', route => route.fulfill({ status: 503, json: { detail: 'temporary' } }));
    await page.goto('/wallet');
    await expect(page.getByText('余额暂时无法确认')).toBeVisible();
    const horizontalOverflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1);
    expect(horizontalOverflow).toBe(false);
    await page.screenshot({ path: testInfo.outputPath(`wallet-error-${viewport.width}x${viewport.height}.png`), fullPage: true });
  });
}
