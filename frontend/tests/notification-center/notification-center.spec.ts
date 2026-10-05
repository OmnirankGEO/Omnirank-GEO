import { expect, test, type Page, type Route } from 'playwright/test';

const user = {
  id: 10,
  user_id: 10,
  username: 'notification-qa',
  display_name: '通知测试用户',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 0,
  roles: [{ id: 3, name: 'user', display_name: '普通用户' }],
  permissions: [],
  client_brand_ids: [],
};

const importantNotification = {
  id: 701,
  type: 'system',
  title: '充值已到账',
  content: '业务单号 ORDER-701；金额 100.00 元；到账算力 10,000；最终状态：已到账。',
  link: '/customer/wallet',
  level: 'important',
  is_read: false,
  created_at: '2026-07-17T12:00:00Z',
  metadata: { event_type: 'recharge.credited' },
};

type MockOptions = {
  listFailures?: number;
  readFailure?: boolean;
  muted?: boolean;
};

async function installMocks(page: Page, options: MockOptions = {}) {
  let listFailures = options.listFailures || 0;
  await page.addInitScript(({ muted }) => {
    localStorage.setItem('omnirank_token', 'notification-test-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-17T00:00:00Z',
      last_updated_at: '2026-07-17T00:00:00Z',
    }));
    if (muted) localStorage.setItem('notification_mute_until', '-1');
  }, { muted: Boolean(options.muted) });

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: {
        success: true,
        data: {
          paid_points: 10000,
          commission_points: 0,
          bonus_points: 0,
          frozen_points: 0,
          total_recharged: 10000,
          customer_credit_status: 'ready',
          customer_credit: {
            tool_credit_points: 10000,
            publish_credit_points: 0,
            bonus_credit_points: 0,
            total_purchased_points: 10000,
            total_consumed_points: 0,
          },
        },
      } });
      return;
    }
    if (path === '/api/user/notifications/unread-count') {
      await route.fulfill({ json: {
        status: 'success',
        count: 1,
        by_level: { silent: 0, light: 0, gentle: 0, important: 1, total: 1 },
      } });
      return;
    }
    if (path === '/api/user/notifications' && route.request().method() === 'GET') {
      if (listFailures > 0) {
        listFailures -= 1;
        await route.fulfill({ status: 503, json: { detail: '通知暂时无法加载' } });
      } else {
        await route.fulfill({ json: { status: 'success', notifications: [importantNotification] } });
      }
      return;
    }
    if (path === '/api/user/notifications/701/read') {
      await route.fulfill(options.readFailure
        ? { status: 503, json: { detail: '通知状态暂时无法更新' } }
        : { json: { status: 'success', marked: true } });
      return;
    }
    if (path === '/api/user/notifications/read-all') {
      await route.fulfill({ json: { status: 'success', marked: 1 } });
      return;
    }
    await route.fulfill({ json: { success: true, status: 'success', data: {}, items: [] } });
  });
}

function overlaps(a: { x: number; y: number; width: number; height: number }, b: { x: number; y: number; width: number; height: number }) {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}

test('重要未读在静音和手机端仍可见，标题余额铃铛不重叠', async ({ page }, testInfo) => {
  await installMocks(page, { muted: true });
  await page.goto('/notifications');
  const bell = page.getByRole('button', { name: '打开通知中心' });
  await expect(bell).toBeVisible();
  // NotificationBell intentionally yields its first auxiliary request for 10s.
  await expect(bell.locator('span').filter({ hasText: '1' })).toBeVisible({ timeout: 15_000 });
  await bell.click();
  await expect(page.getByText('充值已到账').first()).toBeVisible();
  await expect(page.getByText(/业务单号 ORDER-701/).first()).toBeVisible();

  const bellBox = await bell.boundingBox();
  const titleBox = await page.getByRole('heading', { name: '通知中心' }).boundingBox();
  const balanceBox = await page.getByRole('button', { name: /当前余额/ }).boundingBox();
  const notificationPanel = page.getByText('业务单号 ORDER-701').first().locator('xpath=ancestor::div[contains(@class,"fixed")][1]');
  const panelBox = await notificationPanel.boundingBox();
  expect(bellBox).not.toBeNull();
  expect(titleBox).not.toBeNull();
  expect(balanceBox).not.toBeNull();
  expect(panelBox).not.toBeNull();
  expect(overlaps(bellBox!, titleBox!)).toBe(false);
  expect(overlaps(bellBox!, balanceBox!)).toBe(false);
  expect(overlaps(titleBox!, balanceBox!)).toBe(false);
  expect(panelBox!.x).toBeGreaterThanOrEqual(0);
  expect(panelBox!.x + panelBox!.width).toBeLessThanOrEqual(testInfo.project.use.viewport!.width);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('notification-mobile-visible.png'), fullPage: true });
});

test('通知列表 503 显示失败并可重试，不伪装为空', async ({ page }) => {
  await installMocks(page, { listFailures: 1 });
  await page.goto('/notifications');

  await expect(page.getByText('通知暂时无法加载').last()).toBeVisible();
  await expect(page.getByText('暂无通知')).toHaveCount(0);
  const retryAlert = page.locator('[data-alert-action="reload_notifications"]');
  await expect(retryAlert).toHaveAttribute('data-alert-target', '/api/user/notifications');
  await expect(retryAlert).toHaveAttribute('data-alert-permission', 'authenticated');
  await expect(retryAlert).toHaveAttribute('data-alert-recovery', 'retry_after_network_recovers');
  await page.getByRole('button', { name: '重新加载' }).click();
  await expect(page.getByText('充值已到账').last()).toBeVisible();
});

test('标记已读失败保留未读并显示可理解错误', async ({ page }) => {
  await installMocks(page, { readFailure: true });
  await page.goto('/notifications');
  await page.getByText('充值已到账').last().click();
  const statusAlert = page.getByRole('alert');
  await expect(statusAlert).toContainText('通知状态更新失败，请重试');
  await expect(statusAlert).toHaveAttribute('data-alert-status', '/api/user/notifications/{notification_id}/read');
  await expect(page.getByText('1 未读')).toBeVisible();
});

test('点击通知链接同时标记已读并进入现役路由', async ({ page }) => {
  await installMocks(page);
  await page.goto('/notifications');

  const readRequest = page.waitForRequest(request =>
    request.method() === 'POST' && request.url().includes('/api/user/notifications/701/read')
  );
  await page.getByRole('button', { name: '打开通知链接: 充值已到账' }).last().click();
  await readRequest;
  await expect(page).toHaveURL(/\/customer\/wallet$/);
});

test('通知下拉整行可跳转且箭头触控区不少于 44px', async ({ page }) => {
  await installMocks(page);
  await page.goto('/notifications');
  await page.getByRole('button', { name: '打开通知中心' }).click();

  const row = page.getByRole('link', { name: '打开通知: 充值已到账' });
  const arrow = page.getByRole('button', { name: '打开通知链接: 充值已到账' }).first();
  await expect(row).toBeVisible();
  const arrowBox = await arrow.boundingBox();
  expect(arrowBox).not.toBeNull();
  expect(arrowBox!.width).toBeGreaterThanOrEqual(44);
  expect(arrowBox!.height).toBeGreaterThanOrEqual(44);

  const readRequest = page.waitForRequest(request =>
    request.method() === 'POST' && request.url().includes('/api/user/notifications/701/read')
  );
  await row.click();
  await readRequest;
  await expect(page).toHaveURL(/\/customer\/wallet$/);
});
