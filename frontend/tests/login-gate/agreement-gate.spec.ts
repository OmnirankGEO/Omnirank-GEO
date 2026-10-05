import { expect, test, type Page, type Route } from 'playwright/test';

/**
 * 工单 2026-07-29 §5 · 锁 1 / 锁 3(协议门禁死循环)
 *
 * 事故复盘(生产 L2 实证):
 *   `installDetailNormalizer` 把对象 `detail` 归一成字符串、原对象挪到 `detail_contract`,
 *   而 `agreementRequirementFromError` 仍按对象读 `detail.code` → 六项校验第一项即失败
 *   → 返回 null → 428 被降级成一句红字 → 补签页永远到不了。
 *   07-24 20:25 前 35 次 428 有 33 次数秒内补签成功;归一化上线后 2 次 428 转化为 0。
 *
 * 🔴 这里刻意**只 mock 后端原始响应体**,归一化由页面自己的拦截器真跑一遍 ——
 *    否则就绕过了事故本体,变成一个永远绿的假锁。
 */

const BACKEND_428_DETAIL = {
  code: 'REGISTRATION_AGREEMENTS_REQUIRED',
  message: '为继续使用，请确认当前《用户服务协议》和《隐私政策》',
  requires_agreement: true,
  agreement_session_token: 'ags1:eyJ0b2tlbl91c2UiOiJyZWdpc3RyYXRpb25fYWdyZWVtZW50X3N1cHBsZW1lbnQifQ:deadbeefdeadbeefdeadbeefdeadbeef',
  expires_at: Math.floor(Date.now() / 1000) + 600,
  agreements: [
    { agreement_type: 'user_terms', agreement_version: 'user-v2.0', content_hash: '0c63', title: '用户服务协议', url: '/terms?embedded=true' },
    { agreement_type: 'privacy', agreement_version: 'privacy-v1.0', content_hash: '991a', title: '隐私政策', url: '/privacy?embedded=true' },
  ],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

type Shape = 'healthy' | 'payload_drift';

async function install(page: Page, shape: Shape) {
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();

    if (path === '/api/auth/login' && method === 'POST') {
      if (shape === 'healthy') return json(route, { detail: BACKEND_428_DETAIL }, 428);
      // payload 漂移:门禁确实触发(428),但六项校验里的 code 变了 —— 解析必然失败。
      // 锁 3 要的就是"这种时候也必须有出口"。
      return json(route, { detail: { ...BACKEND_428_DETAIL, code: 'SOMETHING_ELSE' } }, 428);
    }
    if (path === '/api/auth/registration-agreements/session' && method === 'POST') {
      return json(route, { success: true, ...BACKEND_428_DETAIL });
    }
    if (path === '/api/auth/me') return json(route, { success: false }, 401);
    return json(route, { success: true, items: [], data: {} });
  });
}

async function passwordLogin(page: Page) {
  await page.goto('/login');
  await page.getByPlaceholder(/手机号/).first().fill('13800000001');
  await page.getByPlaceholder(/密码/).first().fill('correct-horse');
  await page.getByRole('button', { name: '登 录' }).click();
}

test('锁1 · 缺协议账号登录 → 真的进入补签页并看到两个可勾选项(不是红字)', async ({ page }) => {
  await install(page, 'healthy');
  await passwordLogin(page);

  await expect(page).toHaveURL(/\/agreement-update/, { timeout: 10_000 });
  await expect(page.getByRole('heading', { name: '确认最新服务协议' })).toBeVisible();
  await expect(page.locator('#supplement-terms')).toBeVisible();
  await expect(page.locator('#supplement-privacy')).toBeVisible();
  // 事故形态的反向断言:不能停在登录页只给一句红字。
  await expect(page.getByRole('button', { name: '同意并继续' })).toBeVisible();
});

test('锁1b · 勾选两份协议后「同意并继续」可点击(补签动作真的可达)', async ({ page }) => {
  await install(page, 'healthy');
  await passwordLogin(page);
  await expect(page).toHaveURL(/\/agreement-update/, { timeout: 10_000 });

  const submit = page.getByRole('button', { name: '同意并继续' });
  await expect(submit).toBeDisabled();
  await page.locator('#supplement-terms').check();
  await page.locator('#supplement-privacy').check();
  await expect(submit).toBeEnabled();
});

test('锁3 · payload 六项校验失败时,登录页仍给出可点击的补签入口', async ({ page }) => {
  await install(page, 'payload_drift');
  await passwordLogin(page);

  // 仍停在登录页是允许的,但**必须**有出口。
  const exit = page.getByTestId('agreement-gate-exit');
  await expect(exit).toBeVisible({ timeout: 10_000 });
  await exit.getByRole('button', { name: '去确认协议' }).click();
  await expect(page).toHaveURL(/\/agreement-update/, { timeout: 10_000 });
});

test('锁3b · 没有 session 进补签页 → 自助重新验证身份,不是白屏也不是弹回登录', async ({ page }) => {
  await install(page, 'payload_drift');
  await page.goto('/agreement-update');

  await expect(page.getByTestId('agreement-session-recovery')).toBeVisible({ timeout: 10_000 });
  await expect(page).toHaveURL(/\/agreement-update/);
  await page.getByPlaceholder('登录手机号').fill('13800000001');
  await page.getByPlaceholder('密码').fill('correct-horse');
  await page.getByRole('button', { name: '验证身份并继续' }).click();

  // 自助取回凭证后必须真的进到勾选界面。
  await expect(page.locator('#supplement-terms')).toBeVisible({ timeout: 10_000 });
});
