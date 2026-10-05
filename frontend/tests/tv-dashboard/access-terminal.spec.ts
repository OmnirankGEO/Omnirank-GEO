import { expect, test, type Page, type Route } from 'playwright/test';

const adminUser = {
  id: 101,
  username: 'qa-admin',
  display_name: '管理员',
  is_admin: true,
  is_active: 1,
  must_change_password: 0,
  agent_level: 0,
  roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
  permissions: ['users:write'],
  client_brand_ids: [],
};

const normalUser = {
  ...adminUser,
  id: 103,
  username: 'qa-user',
  display_name: '普通用户',
  is_admin: false,
  roles: [{ id: 3, name: 'user', display_name: '普通用户' }],
  permissions: [],
};

const dashboardData = {
  success: true,
  status: 'success',
  code: 'TV_DASHBOARD_READY',
  message: '大屏数据已加载',
  request_id: 'tv-dashboard-ui-ready',
  funnel: {
    total_users: 7,
    today_register: 1,
    online_users: 2,
    profile_rate: 70,
    interview_done: 4,
    lv3_count: 3,
    lv3_rate: 43,
    profiles_count: 5,
    interview_rate: 57,
    paid_count: 2,
    paid_rate: 29,
    corpus_count: 11,
    week_active_rate: 80,
  },
  content: { topics: 2, scripts: 3, articles: 4, published: 1 },
  finance: { today_revenue: 12, month_revenue: 120, month_cost: 20, month_profit: 100, profit_rate: 83 },
  geo_distribution: [],
  activity_heatmap: [],
  revenue_trend: [],
  top_features: [],
  live_feed: [],
  performance: { system: {}, db: {}, api: {}, scheduler: [] },
  publishing: { total: 0, published: 0, rejected: 0, pending: 0, queued: 0, today_published: 0 },
  online_users: [],
};

type TvMock = {
  sessionGranted: boolean;
  accessRequests: number;
  sessionRequests: number;
  exchangeRequests: number;
  dashboardRequests: number;
  dashboardMode: 'success' | 'failure';
  accessMode: 'password' | 'network';
  exchangeMode: 'grant' | 'network';
};

function freshMock(): TvMock {
  return {
    sessionGranted: false,
    accessRequests: 0,
    sessionRequests: 0,
    exchangeRequests: 0,
    dashboardRequests: 0,
    dashboardMode: 'success',
    accessMode: 'password',
    exchangeMode: 'grant',
  };
}

async function installAdminAndTvApi(page: Page, state: TvMock) {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'tv-dashboard-ui-token'));
  await page.route('**/api/**', async (route: Route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: adminUser } });
      return;
    }
    if (path === '/api/tv/access/exchange' && request.method() === 'POST') {
      state.exchangeRequests += 1;
      if (state.exchangeMode === 'network') {
        await route.abort('connectionfailed');
        return;
      }
      const token = String(request.postDataJSON()?.token || '');
      if (token === 'used-exchange-code') {
        await route.fulfill({ status: 403, json: { success: false, status: 'error', code: 'TV_ACCESS_DENIED', message: '安全链接无效或已被使用，请让管理员重新生成或改用访问密码', request_id: 'tv-exchange-403-used' } });
        return;
      }
      if (token === 'expired-exchange-code') {
        await route.fulfill({ status: 403, json: { success: false, status: 'error', code: 'TV_ACCESS_DENIED', message: '安全链接已过期，请让管理员重新生成', request_id: 'tv-exchange-403-expired' } });
        return;
      }
      if (token === 'valid-exchange-code' || token === 'legacy-token-value') {
        state.sessionGranted = true;
        await route.fulfill({ json: { success: true, status: 'success', code: 'TV_ACCESS_GRANTED', message: '大屏访问已授权', request_id: 'tv-exchange-200' } });
        return;
      }
      await route.fulfill({ status: 403, json: { success: false, status: 'error', code: 'TV_ACCESS_DENIED', message: '安全链接无效或已被使用，请让管理员重新生成或改用访问密码', request_id: 'tv-exchange-403' } });
      return;
    }
    if (path === '/api/tv/access/session') {
      state.sessionRequests += 1;
      await route.fulfill(state.sessionGranted
        ? { json: { success: true, status: 'success', code: 'TV_ACCESS_SESSION_ACTIVE', message: '大屏访问会话有效', request_id: 'tv-session-ui', authenticated: true } }
        : { status: 401, json: { success: false, status: 'error', code: 'TV_ACCESS_SESSION_REQUIRED', message: '请输入大屏访问密码', request_id: 'tv-session-ui' } });
      return;
    }
    if (path === '/api/tv/access' && request.method() === 'POST') {
      state.accessRequests += 1;
      if (state.accessMode === 'network') {
        await route.abort('connectionfailed');
        return;
      }
      const password = request.postDataJSON().password;
      if (password === 'bad-input') {
        await route.fulfill({ status: 400, json: { success: false, status: 'error', code: 'TV_ACCESS_INPUT_INVALID', message: '请输入有效的大屏访问密码', request_id: 'tv-access-400' } });
        return;
      }
      if (password === 'server-down') {
        await route.fulfill({ status: 503, json: { success: false, status: 'error', code: 'TV_ACCESS_UNAVAILABLE', message: '大屏访问服务暂时不可用，请稍后重试', request_id: 'tv-access-503' } });
        return;
      }
      if (password === 'forbidden-password') {
        await route.fulfill({ status: 403, json: { success: false, status: 'error', code: 'TV_ADMIN_REQUIRED', message: '仅管理员可访问大屏', request_id: 'tv-access-403-admin' } });
        return;
      }
      if (password === 'rate-limited') {
        // 契约（2026-07-22 后端同步下发）：429 + TV_ACCESS_RATE_LIMITED + Retry-After
        await route.fulfill({
          status: 429,
          headers: { 'Retry-After': '30' },
          json: { success: false, status: 'error', code: 'TV_ACCESS_RATE_LIMITED', message: '尝试次数过多，请稍后重试', request_id: 'tv-access-429' },
        });
        return;
      }
      if (password === 'wrong-password') {
        await route.fulfill({ status: 403, json: { success: false, status: 'error', code: 'TV_ACCESS_DENIED', message: '访问密码不正确', request_id: 'tv-access-403' } });
        return;
      }
      state.sessionGranted = true;
      await route.fulfill({ json: { success: true, status: 'success', code: 'TV_ACCESS_GRANTED', message: '大屏访问已授权', request_id: 'tv-access-200' } });
      return;
    }
    if (path === '/api/tv/dashboard/auth') {
      state.dashboardRequests += 1;
      if (state.dashboardMode === 'failure') {
        await route.fulfill({ status: 503, json: { success: false, status: 'error', code: 'TV_DASHBOARD_UNAVAILABLE', message: '大屏数据暂时不可用，请稍后重试', request_id: 'tv-dashboard-503' } });
        return;
      }
      await route.fulfill({ json: dashboardData });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });
}

const viewports = [
  { width: 320, height: 740 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1440, height: 900 },
];

for (const viewport of viewports) {
  test(`password flow closes every normal state at ${viewport.width}px`, async ({ page }) => {
    const state = freshMock();
    await page.setViewportSize(viewport);
    await installAdminAndTvApi(page, state);
    await page.goto('/tv');

    const input = page.getByLabel('大屏访问密码');
    const submit = page.getByRole('button', { name: '进入监控中心' });
    await expect(input).toBeVisible();

    await input.fill('wrong-password');
    await submit.click();
    await expect(page.getByRole('alert')).toContainText('访问密码不正确');
    await expect(input).toHaveValue('');
    await expect(submit).toBeEnabled();

    await input.fill('correct-password');
    await submit.click();
    await expect(page.getByText('实时运营监控')).toBeVisible();
    expect(state.accessRequests).toBe(2);

    const privacy = await page.evaluate(() => ({
      href: window.location.href,
      local: Object.entries(localStorage),
      session: Object.entries(sessionStorage),
      body: document.body.textContent || '',
      overflow: document.documentElement.scrollWidth > window.innerWidth,
    }));
    expect(JSON.stringify(privacy)).not.toContain('correct-password');
    expect(privacy.overflow).toBe(false);

    await page.reload();
    await expect(page.getByText('实时运营监控')).toBeVisible();
    await expect(page.getByLabel('大屏访问密码')).toHaveCount(0);
    expect(state.accessRequests).toBe(2);
  });
}

test('4xx, 5xx, forbidden, network failure and dashboard failure are retryable terminal states', async ({ page }) => {
  const state = freshMock();
  await page.setViewportSize({ width: 390, height: 844 });
  await installAdminAndTvApi(page, state);
  await page.goto('/tv');
  const input = page.getByLabel('大屏访问密码');

  await input.fill('bad-input');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByRole('alert')).toContainText('请输入有效的大屏访问密码');
  await expect(page.getByText('请求 tv-access-400')).toBeVisible();

  await input.fill('forbidden-password');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByRole('alert')).toContainText('仅管理员可访问大屏');
  await expect(page.getByText('请求 tv-access-403-admin')).toBeVisible();
  await expect(page.getByRole('button', { name: '进入监控中心' })).toBeEnabled();

  await input.fill('server-down');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByRole('alert')).toContainText('大屏访问服务暂时不可用');
  await expect(page.getByRole('button', { name: '进入监控中心' })).toBeEnabled();

  state.accessMode = 'network';
  await input.fill('network-failure');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByRole('alert')).toContainText('无法连接大屏访问服务');
  await expect(page.getByRole('button', { name: '进入监控中心' })).toBeEnabled();

  state.accessMode = 'password';
  state.dashboardMode = 'failure';
  await input.fill('correct-password');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByRole('alert')).toContainText('大屏数据暂时不可用');
  await expect(page.getByText('请求 tv-dashboard-503')).toBeVisible();

  state.dashboardMode = 'success';
  await page.getByRole('button', { name: '重试加载' }).click();
  await expect(page.getByText('实时运营监控')).toBeVisible();
  expect(state.dashboardRequests).toBe(2);
});

test('429 rate limit shows the server message and stays retryable', async ({ page }) => {
  const state = freshMock();
  await page.setViewportSize({ width: 390, height: 844 });
  await installAdminAndTvApi(page, state);
  await page.goto('/tv');
  const input = page.getByLabel('大屏访问密码');

  // 限流：forbidden 类 → 展示服务端 message + request_id，密码门保持可重试
  await input.fill('rate-limited');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByRole('alert')).toContainText('尝试次数过多，请稍后重试');
  await expect(page.getByText('请求 tv-access-429')).toBeVisible();
  await expect(page.getByRole('button', { name: '进入监控中心' })).toBeEnabled();
  await expect(page.getByLabel('大屏访问密码')).toBeVisible();

  // 限流解除后同一密码门可直接进入
  await input.fill('correct-password');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByText('实时运营监控')).toBeVisible();
  expect(state.accessRequests).toBe(2);
});

test('timeout, duplicate submission and unmount cannot leave a live spinner or stale update', async ({ page }) => {
  const pageErrors: string[] = [];
  page.on('pageerror', error => pageErrors.push(error.message));
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'tv-dashboard-ui-token'));
  let accessRequests = 0;
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: adminUser } });
      return;
    }
    if (path === '/api/tv/access/session') {
      await route.fulfill({ status: 401, json: { success: false, status: 'error', code: 'TV_ACCESS_SESSION_REQUIRED', message: '请输入大屏访问密码', request_id: 'tv-session-ui' } });
      return;
    }
    if (path === '/api/tv/access') {
      accessRequests += 1;
      await new Promise(resolve => setTimeout(resolve, 15_000));
      await route.fulfill({ json: { success: true } }).catch(() => undefined);
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/tv');
  const input = page.getByLabel('大屏访问密码');
  const submit = page.getByRole('button', { name: '进入监控中心' });
  await input.fill('slow-password');
  await submit.click();
  await expect(page.getByRole('button', { name: '验证中…' })).toBeDisabled();
  await page.locator('form').evaluate((form: HTMLFormElement) => {
    form.requestSubmit();
    form.requestSubmit();
  });
  await expect.poll(() => accessRequests).toBe(1);
  // 锚定「提交态结束」这一确定性后置条件（按钮复位与错误提示同属一次状态提交），
  // 不再用裸 wall-clock 窗口等 8s 超时——满载 worker 上浏览器定时器可任意延迟。
  await expect(page.getByRole('button', { name: '进入监控中心' })).toBeEnabled({ timeout: 30_000 });
  await expect(page.getByRole('alert')).toContainText('验证超时');
  await expect(page.getByRole('button', { name: '进入监控中心' })).toBeEnabled();

  await input.fill('unmount-password');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await page.getByRole('link', { name: '返回管理后台' }).click();
  await expect(page).not.toHaveURL(/\/tv(?:\?|$)/);
  await page.waitForTimeout(250);
  expect(pageErrors).toEqual([]);
});

test('non-admin is denied before the password or dashboard APIs are called', async ({ page }) => {
  let tvRequests = 0;
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'tv-dashboard-normal-user-token'));
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: normalUser } });
      return;
    }
    if (path.startsWith('/api/tv/')) tvRequests += 1;
    await route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/tv');
  await expect(page.getByRole('heading', { name: '当前账号没有此页面权限' })).toBeVisible();
  await expect(page.getByLabel('大屏访问密码')).toHaveCount(0);
  expect(tvRequests).toBe(0);
});

// ========== D4 · URL token 一次性交换 ==========

test('URL exchange code trades for a session, strips the token and skips the password gate', async ({ page }) => {
  const state = freshMock();
  await page.setViewportSize({ width: 1440, height: 900 });
  await installAdminAndTvApi(page, state);
  await page.goto('/tv?token=valid-exchange-code');

  // 交换成功 → 直接进大屏，不调会话探测、不出密码门
  await expect(page.getByText('实时运营监控')).toBeVisible();
  expect(state.exchangeRequests).toBe(1);
  expect(state.sessionRequests).toBe(0);
  expect(state.accessRequests).toBe(0);
  await expect(page.getByLabel('大屏访问密码')).toHaveCount(0);

  // 地址栏 token 已抹除；交换码不落任何存储
  await expect(page).not.toHaveURL(/token=/);
  const storageDump = await page.evaluate(() => JSON.stringify({
    href: window.location.href,
    local: Object.entries(localStorage),
    session: Object.entries(sessionStorage),
  }));
  expect(storageDump).not.toContain('valid-exchange-code');

  // 刷新后会话仍在（不再交换）
  await page.reload();
  await expect(page.getByText('实时运营监控')).toBeVisible();
  expect(state.exchangeRequests).toBe(1);
});

test('legacy URL token still exchanges to a session during the compat window', async ({ page }) => {
  const state = freshMock();
  await installAdminAndTvApi(page, state);
  await page.goto('/tv?token=legacy-token-value');

  await expect(page.getByText('实时运营监控')).toBeVisible();
  expect(state.exchangeRequests).toBe(1);
  await expect(page).not.toHaveURL(/token=/);
});

test('used or expired URL token falls back to the password gate with a human message', async ({ page }) => {
  const state = freshMock();
  await installAdminAndTvApi(page, state);
  await page.goto('/tv?token=used-exchange-code');

  // 链接失效 → 落密码门 + 人话提示 + 地址栏已抹 token
  await expect(page.getByRole('alert')).toContainText('安全链接无效或已被使用');
  await expect(page.getByText('请求 tv-exchange-403-used')).toBeVisible();
  await expect(page.getByLabel('大屏访问密码')).toBeVisible();
  await expect(page).not.toHaveURL(/token=/);

  // 密码门兜底仍可用
  await page.getByLabel('大屏访问密码').fill('correct-password');
  await page.getByRole('button', { name: '进入监控中心' }).click();
  await expect(page.getByText('实时运营监控')).toBeVisible();
  expect(state.accessRequests).toBe(1);

  // 过期链接同样落密码门
  const state2 = freshMock();
  await installAdminAndTvApi(page, state2);
  await page.goto('/tv?token=expired-exchange-code');
  await expect(page.getByRole('alert')).toContainText('安全链接已过期');
  await expect(page.getByLabel('大屏访问密码')).toBeVisible();
});

test('exchange network failure keeps the token for an explicit retry', async ({ page }) => {
  const state = freshMock();
  state.exchangeMode = 'network';
  await installAdminAndTvApi(page, state);
  await page.goto('/tv?token=valid-exchange-code');

  // 断网 → 会话错误页（保留 token），点击重试后交换成功
  await expect(page.getByRole('alert')).toContainText('无法连接大屏访问服务');
  state.exchangeMode = 'grant';
  await page.getByRole('button', { name: '重试检查' }).click();
  await expect(page.getByText('实时运营监控')).toBeVisible();
  expect(state.exchangeRequests).toBe(2);
  await expect(page).not.toHaveURL(/token=/);
});
