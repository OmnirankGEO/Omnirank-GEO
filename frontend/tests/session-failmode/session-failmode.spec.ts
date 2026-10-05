import { expect, test, type Page, type Route } from 'playwright/test';

// [BUG-3 2026-07-27] 会话确认失败 → 整页白屏 的真渲染判别。
//
// 事故：2026-07-27 生产 /api/auth/me 变慢（新建连接 +2.0~2.7s），
// `requireConfirmedSessionToken()` 在 token 未被 /me 确认时直接 throw
// → 冒泡到 ErrorBoundary → 大量客户与 Owner 看到整页「页面出错了」；
// 客户门户（PortalDashboard）表现为「数据全丢失」。
//
// 这里模拟两种情况，断言用户看到的是 loading / 登录引导，**不是白屏 + stack trace**：
//   A. /me 慢 5s   → 等待期间页面必须活着
//   B. /me 返回 401 → 走登录引导
//
// 🔴 本文件【不】负责 fail-open 安全锁。我写过一条"确认前不得带 Authorization 发业务请求"，
//    实测把 awaitConfirmedSessionToken 改成 fail-open 后它仍然全绿 —— 因为 /me 在途期间
//    React 组件根本还没开始发业务请求，那条断言恒真、零区分度，属于假绿，已删。
//    fail-open 由 scripts/test-authoritative-session-failmode.mjs 守（该变异下红 5 条）。

const user = {
  id: 149, user_id: 149, username: 'session-failmode', display_name: '会话测试用户',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 1,
  roles: [], permissions: [], client_brand_ids: [],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

type MeMode = 'slow5s' | 'unauthorized';

async function install(page: Page, mode: MeMode) {

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'session-failmode-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'returning', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-07-27T00:00:00Z', last_updated_at: '2026-07-27T00:00:00Z',
    }));
  });

  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;

    if (path === '/api/auth/me') {
      if (mode === 'unauthorized') {
        return json(route, { success: false, code: 'INVALID_TOKEN', detail: 'token 无效' }, 401);
      }
      await new Promise(resolve => setTimeout(resolve, 5000));   // 事故复现：慢 5s
      return json(route, { success: true, user });
    }
    if (path === '/api/auth/refresh') {
      return json(route, { success: false }, 401);
    }

    return json(route, { success: true, items: [], data: {}, clients: [], total: 0, count: 0 });
  });
}

/** ErrorBoundary 的致命形态：标题「页面出错了」+ stack trace 展开器。 */
async function expectNoFatalErrorScreen(page: Page) {
  await expect(page.getByText('页面出错了')).toHaveCount(0);
  await expect(page.getByText('展开 stack trace')).toHaveCount(0);
  await expect(page.getByText('AuthoritativeSessionPendingError')).toHaveCount(0);
}

test.describe('A · /api/auth/me 慢 5 秒', () => {
  for (const [label, path] of [['主应用', '/'], ['客户门户', '/portal/dashboard']] as const) {
    test(`${label}：等待期间不白屏，落定后正常渲染`, async ({ page }) => {
      await install(page, 'slow5s');
      await page.goto(path);

      // 等待中：页面活着，没有 ErrorBoundary
      await page.waitForTimeout(2000);
      await expectNoFatalErrorScreen(page);
      expect(await page.locator('body').count()).toBe(1);

      // /me 落定后：仍然不是错误页，且页面渲染出了真内容
      await page.waitForTimeout(6000);
      await expectNoFatalErrorScreen(page);
      expect((await page.locator('body').innerText()).trim().length).toBeGreaterThan(0);
    });
  }
});

test.describe('B · /api/auth/me 返回 401', () => {
  test('主应用：走登录引导，不白屏', async ({ page }) => {
    await install(page, 'unauthorized');
    await page.goto('/');
    await page.waitForTimeout(3000);

    await expectNoFatalErrorScreen(page);
    // 401 是真未登录：token 必须被清掉（不得把无效 token 继续带出去）
    const token = await page.evaluate(() => localStorage.getItem('omnirank_token'));
    expect(token).toBeNull();
  });

  test('客户门户：不白屏（事故重灾区）', async ({ page }) => {
    await install(page, 'unauthorized');
    await page.goto('/portal/dashboard');
    await page.waitForTimeout(3000);
    await expectNoFatalErrorScreen(page);
  });
});
