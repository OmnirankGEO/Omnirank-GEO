import { expect, test, type Page, type Route } from 'playwright/test';

const token = 'opaque-new-operator-invitation-token-abcdef1234567890';

const employeeUser = {
  id: 73, user_id: 73, username: '+8613800138000', display_name: '新员工甲',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 0,
  roles: [], permissions: [], client_brand_ids: [],
};

const publicStatus = {
  status: 'pending',
  organization_name: '华东品牌增长与客户成功团队（超长中文邀请验收）',
  role_name: '交付执行员工',
  target_kind: 'phone',
  account_mode: 'create_operator',
  account_available: true,
  expires_at: '2026-08-21T12:00:00Z',
  agreements: { user_terms_version: 'v2', privacy_version: 'v2' },
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function expectNoOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
}

async function installBase(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [], first_seen_at: '2026-07-23T00:00:00Z',
      last_updated_at: '2026-07-23T00:00:00Z',
    }));
  });
}

interface DeliveryMock {
  state: string;
  failureCode: string | null;
}

async function installPublicRoutes(
  page: Page,
  writes: Array<{ path: string; body: Record<string, unknown> }>,
  delivery: DeliveryMock,
  options: { challengeStatus?: number; challengeError?: unknown } = {},
) {
  let challengeCalls = 0;
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/public/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, publicStatus);
    }
    if (path === '/api/public/organization/invites/verification-challenges') {
      challengeCalls += 1;
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      if (options.challengeError) {
        return json(route, { detail: options.challengeError }, options.challengeStatus || 503);
      }
      // 重发（第 2 次起）人为延迟，给 single-flight 断言一个真实的并发窗口
      if (challengeCalls >= 2) await new Promise(resolve => setTimeout(resolve, 400));
      return json(route, {
        challenge_id: 70 + challengeCalls, status: 'pending',
        expires_at: publicStatus.expires_at, delivery_queued: true, replayed: false,
      });
    }
    if (path.endsWith('/delivery-status')) {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      const challengeId = Number(path.split('/').at(-2));
      return json(route, {
        challenge_id: challengeId, challenge_status: 'pending',
        delivery_state: delivery.state, failure_code: delivery.failureCode,
        sent_at: delivery.state === 'sent' ? '2026-07-23T10:00:00Z' : null,
        expires_at: publicStatus.expires_at,
      });
    }
    if (path.endsWith('/verify')) {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { challenge_id: 71, status: 'verified', verification_receipt: 'opaque-verification-receipt-abcdef1234567890', replayed: false });
    }
    if (path === '/api/public/organization/invites/onboard') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, {
        success: true, user_id: 73, login_username: '+8613800138000', membership_id: 74,
        organization_id: 88, status: 'active', account_origin: 'organization_invite',
        replayed: false, auto_login: true, token: 'w1-operator-jwt', user: employeeUser,
      });
    }
    if (path === '/api/auth/me') return json(route, { success: true, user: employeeUser });
    if (path === '/api/auth/login') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { success: true, token: 'should-not-be-used', user: employeeUser });
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });
  return () => challengeCalls;
}

test('新员工：无需预先注册文案 + 送达状态流 + 验证码开户后 JWT 自动登录', async ({ page }) => {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  const delivery: DeliveryMock = { state: 'queued', failureCode: null };
  await installBase(page);
  await installPublicRoutes(page, writes, delivery);

  await page.goto(`/organization/invite#token=${token}`);
  await expect(page.getByText(/无需预先注册/)).toBeVisible();
  await expect(page.getByText(publicStatus.organization_name)).toBeVisible();

  await page.getByRole('button', { name: '验证联系方式并创建员工账号' }).click();
  await expect(page.getByTestId('invite-delivery-badge')).toHaveText(/排队中/);

  // 服务端送达后，重新核验 → 已发送
  delivery.state = 'sent';
  await page.getByTestId('invite-delivery-recheck').click();
  await expect(page.getByTestId('invite-delivery-badge')).toHaveText(/已发送/);

  await page.getByLabel('6 位验证码').fill('246810');
  await page.getByRole('button', { name: '确认验证码' }).click();
  await page.getByLabel('你的姓名').fill('新员工甲');
  await page.getByLabel('设置登录密码').fill('Local-only-password-2026');
  await page.getByText('我已阅读并同意 用户协议').click();
  await page.getByText('我已阅读并同意 隐私政策').click();
  await page.getByRole('button', { name: '创建员工账号并加入团队' }).click();

  await expect(page.getByText(/已成功加入/)).toBeVisible();
  // JWT 直发：不走密码登录
  expect(writes.filter(item => item.path === '/api/auth/login')).toHaveLength(0);
  const onboard = writes.find(item => item.path.endsWith('/invites/onboard'))?.body || {};
  expect(onboard.token).toBe(token);
  expect(onboard.referral_code).toBeUndefined();
  await expectNoOverflow(page);
});

test('送达失败：状态徽章 + 失败原因人话 + 三动作 single-flight', async ({ page }) => {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  const delivery: DeliveryMock = { state: 'queued', failureCode: null };
  await installBase(page);
  const challengeCalls = await installPublicRoutes(page, writes, delivery);

  await page.goto(`/organization/invite#token=${token}`);
  await page.getByRole('button', { name: '验证联系方式并创建员工账号' }).click();

  // 确定性失败（手机号无效）→ failed 徽章 + 人话 + 三动作
  delivery.state = 'failed';
  delivery.failureCode = 'isv.MOBILE_NUMBER_ILLEGAL';
  await page.getByTestId('invite-delivery-recheck').click();
  await expect(page.getByTestId('invite-delivery-badge')).toHaveText(/发送失败/);
  await expect(page.getByTestId('invite-delivery-failure')).toContainText('手机号无效');
  await expect(page.getByTestId('invite-delivery-resend')).toBeVisible();
  await expect(page.getByTestId('invite-delivery-recheck')).toBeVisible();
  await expect(page.getByTestId('invite-delivery-contact-owner')).toBeVisible();

  // 重新发送 single-flight：连击只产生一个新 challenge
  delivery.state = 'queued';
  delivery.failureCode = null;
  const resend = page.getByTestId('invite-delivery-resend');
  await resend.click();
  await resend.click({ force: true }).catch(() => undefined);
  await expect.poll(() => challengeCalls()).toBe(2);
  await page.waitForTimeout(300);
  expect(challengeCalls()).toBe(2);
  await expect(page.getByTestId('invite-delivery-badge')).toHaveText(/排队中/);

  // 联系团队负责人 → 提示
  await page.getByTestId('invite-delivery-contact-owner').click();
  await expect(page.getByTestId('contact-owner-notice')).toBeVisible();
  await expectNoOverflow(page);
});

test('邮箱邀请显式不可用：无发送入口，人话引导换手机号', async ({ page }) => {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installBase(page);
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/public/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { ...publicStatus, target_kind: 'email' });
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto(`/organization/invite#token=${token}`);
  await expect(page.getByTestId('email-invite-unavailable')).toContainText('邮箱邀请暂不可用');
  await expect(page.getByRole('button', { name: '验证联系方式并创建员工账号' })).toHaveCount(0);
  // 绝不发起 verification-challenges（假装发送）
  expect(writes.filter(item => item.path.endsWith('/verification-challenges'))).toHaveLength(0);
  await expectNoOverflow(page);
});

test('短信通道未配置：503 人话报错，绝不显示已发送', async ({ page }) => {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  const delivery: DeliveryMock = { state: 'queued', failureCode: null };
  await installBase(page);
  await installPublicRoutes(page, writes, delivery, {
    challengeStatus: 503,
    challengeError: { code: 'ORG_INVITE_DELIVERY_NOT_CONFIGURED', message: '邀请验证通道尚未配置，请联系管理员' },
  });

  await page.goto(`/organization/invite#token=${token}`);
  await page.getByRole('button', { name: '验证联系方式并创建员工账号' }).click();
  await expect(page.getByRole('alert')).toContainText('邀请短信通道尚未配置');
  await expect(page.getByTestId('invite-delivery-status')).toHaveCount(0);
  await expect(page.getByText(/已发送/)).toHaveCount(0);
  await expectNoOverflow(page);
});
