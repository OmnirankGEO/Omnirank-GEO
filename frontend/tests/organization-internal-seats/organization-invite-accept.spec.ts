import { expect, test, type Page, type Route } from 'playwright/test';

const user = {
  id: 42, user_id: 42, username: 'invite-member', display_name: '待加入员工账号',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 0,
  roles: [{ id: 2, name: 'social_ops', display_name: '普通用户' }],
  permissions: ['settings:read'], client_brand_ids: [],
};

const publicStatus = {
  status: 'pending',
  organization_name: '华东品牌增长与客户成功团队（超长中文邀请验收）',
  role_name: '交付执行员工',
  target_kind: 'phone',
  account_mode: 'sign_in',
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

async function installAuthenticated(
  page: Page,
  writes: Array<{ path: string; body: Record<string, unknown> }>,
  blockerCode: string | null = null,
  publicGate?: Promise<void>,
  failFirstAccept = false,
) {
  let acceptCalls = 0;
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'organization-invite-ui-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [], first_seen_at: '2026-07-21T00:00:00Z',
      last_updated_at: '2026-07-21T00:00:00Z',
    }));
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'invite-refresh-token', user });
    if (path === '/api/public/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      if (publicGate) await publicGate;
      return json(route, publicStatus);
    }
    if (path === '/api/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, {
        status: blockerCode === 'ORG_INVITE_ACCEPTED' ? 'accepted' : 'pending',
        organization_id: 88,
        organization_name: publicStatus.organization_name,
        role_name: publicStatus.role_name,
        target_kind: 'phone', expires_at: publicStatus.expires_at,
        account_matched: true, target_verified: true,
        can_accept: blockerCode === null, blocker_code: blockerCode,
      });
    }
    if (path === '/api/organization/invites/accept') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      acceptCalls += 1;
      if (failFirstAccept && acceptCalls === 1) return route.abort('failed');
      return json(route, { membership_id: 901, organization_id: 88, status: 'active' });
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });
}

test('已有账号真实核验、清理 fragment、响应丢失后稳定重放接受', async ({ page }) => {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installAuthenticated(page, writes, null, undefined, true);
  const token = 'opaque-organization-invitation-token-1234567890';
  await page.goto(`/organization/invite#token=${token}`);
  await expect(page).toHaveURL(/\/organization\/invite$/);
  await expect(page.getByText(publicStatus.organization_name)).toBeVisible();
  await expect(page.getByText('当前账号与已验证联系方式匹配。')).toBeVisible();
  await expectNoOverflow(page);

  const accept = page.getByRole('button', { name: '接受团队邀请' });
  await accept.click();
  await expect(page.getByRole('alert')).toBeVisible();
  await expect(accept).toBeEnabled();
  await accept.click();
  await expect(page.getByText(/已成功加入/)).toBeVisible();

  const accepts = writes.filter(item => item.path.endsWith('/accept'));
  expect(accepts).toHaveLength(2);
  expect(accepts[0].body.request_id).toMatch(/^accept-invite:/);
  expect(accepts[1].body.request_id).toBe(accepts[0].body.request_id);
  expect(writes.every(item => item.body.token === token)).toBe(true);
});

test('尚无账号经验证码创建最小员工账号并直接加入，不写推荐或商业字段', async ({ page }) => {
  const token = 'opaque-new-operator-invitation-token-1234567890';
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/public/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { ...publicStatus, account_mode: 'create_operator' });
    }
    if (path === '/api/public/organization/invites/verification-challenges') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { challenge_id: 71, status: 'pending', expires_at: publicStatus.expires_at, delivery_queued: true, test_code: '246810' });
    }
    if (path === '/api/public/organization/invites/verification-challenges/71/verify') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { challenge_id: 71, status: 'verified', verification_receipt: 'opaque-verification-receipt-1234567890' });
    }
    if (path === '/api/public/organization/invites/onboard') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { user_id: 73, login_username: '+8613800138000', membership_id: 74, organization_id: 88, status: 'active', account_origin: 'organization_invite' });
    }
    if (path === '/api/auth/login') return json(route, { success: true, token: 'new-operator-session' });
    if (path === '/api/auth/me') return json(route, { success: true, user: { ...user, id: 73, user_id: 73, username: '+8613800138000' } });
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto(`/organization/invite#token=${token}`);
  await page.getByRole('button', { name: '验证联系方式并创建员工账号' }).click();
  await expect(page.getByTestId('local-verification-code')).toContainText('246810');
  await page.getByRole('button', { name: '确认验证码' }).click();
  await page.getByLabel('你的姓名').fill('新员工甲');
  await page.getByLabel('设置登录密码').fill('Local-only-password-2026');
  await page.getByText('我已阅读并同意 用户协议').click();
  await page.getByText('我已阅读并同意 隐私政策').click();
  await page.getByRole('button', { name: '创建员工账号并加入团队' }).click();
  await expect(page.getByText(/已成功加入/)).toBeVisible();
  await expectNoOverflow(page);

  const onboard = writes.find(item => item.path.endsWith('/invites/onboard'))?.body || {};
  expect(onboard.token).toBe(token);
  expect(onboard.challenge_id).toBe(71);
  for (const forbidden of ['referrer_id', 'agent_id', 'channel_id', 'wallet', 'commission', 'brand_ids']) {
    expect(onboard).not.toHaveProperty(forbidden);
  }
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toContain(token);
});

test('未登录已有账号在邀请页原地登录，凭证不进入 storage', async ({ page }) => {
  const token = 'opaque-existing-login-invitation-token-1234567890';
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/public/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, publicStatus);
    }
    if (path === '/api/auth/login') return json(route, { success: true, token: 'existing-inline-login-session' });
    if (path === '/api/auth/me') return json(route, { success: true, user });
    if (path === '/api/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { status: 'pending', organization_id: 88, organization_name: publicStatus.organization_name, role_name: publicStatus.role_name, target_kind: 'phone', expires_at: publicStatus.expires_at, account_matched: true, target_verified: true, can_accept: true, blocker_code: null });
    }
    if (path === '/api/organization/invites/accept') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { membership_id: 91, organization_id: 88, status: 'active' });
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto(`/organization/invite#token=${token}`);
  await page.getByLabel('已有账号').fill('invite-member');
  await page.getByLabel('密码').fill('local-test-only');
  await page.getByRole('button', { name: '登录并核验邀请' }).click();
  await expect(page.getByText('当前账号与已验证联系方式匹配。')).toBeVisible();
  await page.getByRole('button', { name: '接受团队邀请' }).click();
  await expect(page.getByText(/已成功加入/)).toBeVisible();
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toContain(token);
});

test('跨团队状态硬禁用接受，迟到响应离页后不回填', async ({ page }) => {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installAuthenticated(page, writes, 'ORG_USER_ALREADY_MEMBER_OTHER_ORG');
  await page.goto('/organization/invite#token=opaque-cross-organization-invite-token');
  await expect(page.getByText('当前账号已属于其他团队，不能跨团队接受邀请。')).toBeVisible();
  await expect(page.getByRole('button', { name: '接受团队邀请' })).toBeDisabled();
  await expectNoOverflow(page);

  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  const latePage = await page.context().newPage();
  const lateWrites: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installAuthenticated(latePage, lateWrites, null, gate);
  await latePage.goto('/organization/invite#token=opaque-late-invitation-response-token');
  await expect(latePage.getByTestId('organization-invite-accept')).toBeVisible();
  await latePage.goto('/landing');
  release();
  await expect(latePage.getByTestId('organization-invite-accept')).toHaveCount(0);
  await expect(latePage.getByText(publicStatus.organization_name)).toHaveCount(0);
});
test('[WP6] 用户名式邀请:跳过短信,凭 token 直接自设密码开户,不带验证凭证/商业字段', async ({ page }) => {
  const token = 'opaque-username-invitation-token-1234567890';
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/public/organization/invites/inspect') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { ...publicStatus, target_kind: 'username', account_mode: 'create_operator' });
    }
    if (path === '/api/public/organization/invites/onboard-credential') {
      writes.push({ path, body: request.postDataJSON() as Record<string, unknown> });
      return json(route, { user_id: 77, login_username: 'geo_operator_1', membership_id: 78, organization_id: 88, status: 'active', account_origin: 'organization_invite', auto_login: true, token: 'username-operator-session' });
    }
    if (path === '/api/auth/me') return json(route, { success: true, user: { ...user, id: 77, user_id: 77, username: 'geo_operator_1' } });
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto(`/organization/invite#token=${token}`);
  // 用户名式:直接出现设密码表单,绝不出现短信取码入口
  await expect(page.getByTestId('username-onboard-form')).toBeVisible();
  await expect(page.getByRole('button', { name: '验证联系方式并创建员工账号' })).toHaveCount(0);
  await expectNoOverflow(page);

  await page.getByLabel('你的姓名').fill('用户名员工甲');
  await page.getByLabel('设置登录密码').fill('Username-only-password-2026');
  await page.getByText('我已阅读并同意 用户协议').click();
  await page.getByText('我已阅读并同意 隐私政策').click();
  await page.getByTestId('username-onboard-submit').click();
  await expect(page.getByText(/已成功加入/)).toBeVisible();

  const onboard = writes.find(item => item.path.endsWith('/invites/onboard-credential'))?.body || {};
  expect(onboard.token).toBe(token);
  // 凭证式:不带 challenge_id/verification_receipt,也不带任何推荐/商业/钱包字段
  for (const forbidden of ['challenge_id', 'verification_receipt', 'referrer_id', 'agent_id', 'channel_id', 'wallet', 'commission', 'brand_ids']) {
    expect(onboard).not.toHaveProperty(forbidden);
  }
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toContain(token);
});

test('query 参数中的邀请凭证被拒绝并立即从地址栏清理', async ({ page }) => {
  const writes: Array<{ path: string; body: Record<string, unknown> }> = [];
  await installAuthenticated(page, writes);
  const queryToken = 'query-token-must-never-be-accepted-or-forwarded';
  await page.goto(`/organization/invite?token=${queryToken}`);
  await expect(page).toHaveURL(/\/organization\/invite$/);
  await expect(page.getByText('邀请链接不完整，请重新打开老板发给你的完整链接。')).toBeVisible();
  expect(writes).toHaveLength(0);
  const storage = await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }));
  expect(storage).not.toContain(queryToken);
});
