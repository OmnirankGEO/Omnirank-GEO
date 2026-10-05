import { expect, test, type Page, type Route } from 'playwright/test';


type ScopeKind = 'legacy_unrestricted' | 'legacy_selected' | 'organization_assigned';
type SubjectKind = 'legacy_user' | 'organization_member' | 'organization_owner';

interface ScopeFixture {
  scope_kind: ScopeKind;
  subject_kind: SubjectKind;
  editable: boolean;
  brand_ids: number[];
  effective_brand_ids: number[];
  empty_semantics: string;
  version: number;
  permission_version: number;
  etag: string;
}

const adminUser = {
  id: 80,
  user_id: 80,
  username: 'platform-admin',
  display_name: '平台管理员',
  is_admin: true,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'admin', display_name: '管理员' }],
  permissions: ['users:read', 'users:admin'],
  client_brand_ids: [],
};

const longName = '华东超长客户品牌名称与全球增长交付有限公司（用于验证长中文和窄屏折行）';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

function detail(userId: number) {
  return {
    success: true,
    overview: {
      user_id: userId,
      username: `user${userId}`,
      display_name: `客户范围验证账号 ${userId}`,
      phone: '139****0070',
      company: '超长服务商公司名称用于自适应布局验证',
      is_active: true,
      business_identity: 'service_provider',
      business_identity_label: '服务商',
      platform_access: 'standard',
      platform_access_label: '普通账号',
      total_points: 1200,
      paid_points: 1200,
      bonus_points: 0,
      total_recharged_points: 1200,
      customer_count: 1,
      brand_count: 2,
      versions: {
        business_identity: 1,
        commercial_binding: 1,
        channel_relationship: 1,
        platform_access: 1,
        password_security: 1,
        wallet_adjustment: 1,
      },
    },
    relationships: {
      registration: { present: false, source: 'none', evidence: { status: 'not_required', label: '无邀请记录' } },
      commercial: { mode: 'platform_direct', binding_source_label: '平台直营', relationship_version: 1, evidence: { status: 'not_required', label: '不适用' } },
      channel: { mode: 'platform_root' },
      dual_relationships_present: false,
      notices: [],
    },
    pricing_and_settlement: {
      customer_pricing_route: '平台动态价目表',
      procurement_pricing_route: '平台直接供货',
      settlement_route: '平台结算',
      pricing_source: '版本化商品配置',
    },
    clients_and_brands: {
      clients: [{ customer_user_id: 71, username: 'customer71', display_name: '商业绑定客户', bound_at: '2026-07-20T00:00:00Z' }],
      brands: [{ brand_id: 70, name: longName, industry: '企业服务', status: 'active' }],
    },
    wallet_and_billing: {
      paid_points: 1200,
      bonus_points: 0,
      total_points: 1200,
      total_recharged_points: 1200,
      recent_orders: [],
      recent_transactions: [],
    },
    permissions_and_security: {
      platform_access: 'standard',
      account_status_label: '正常',
      must_change_password: false,
      permission_version: 3,
      legacy_roles: [],
    },
    operation_logs: [],
  };
}

function listItem(userId: number) {
  return {
    user_id: userId,
    username: `user${userId}`,
    display_name: `客户范围验证账号 ${userId}`,
    phone: '139****0070',
    is_active: true,
    business_identity: 'service_provider',
    platform_access: 'standard',
    total_points: 1200,
    customer_count: 1,
    brand_count: 2,
    service_mode: 'service_provider',
    needs_attention: false,
    versions: {
      business_identity: 1,
      commercial_binding: 1,
      channel_relationship: 1,
      platform_access: 1,
      password_security: 1,
      wallet_adjustment: 1,
    },
  };
}

async function installAdminSession(page: Page, initial: ScopeFixture) {
  const writes: Array<Record<string, unknown>> = [];
  let scope = {
    success: true,
    user_id: 70,
    available_brands: [
      { id: 70, name: longName, owner_user_id: 70 },
      { id: 71, name: '合法服务客户品牌', owner_user_id: 71 },
    ],
    stale_brand_ids: [],
    organization_id: initial.scope_kind === 'organization_assigned' ? 1 : null,
    membership_id: initial.scope_kind === 'organization_assigned' ? 2 : null,
    principal_user_id: initial.scope_kind === 'organization_assigned' ? 1 : 70,
    organization_authority_version: initial.scope_kind === 'organization_assigned' ? 11 : null,
    membership_status: initial.scope_kind === 'organization_assigned' ? 'active' : null,
    ...initial,
  };
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'admin-client-scope-playwright-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-20T00:00:00.000Z',
      last_updated_at: '2026-07-20T00:00:00.000Z',
    }));
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: adminUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'admin-refresh', user: adminUser });
    if (path === '/api/organization/overview') {
      return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND', message: '当前账号不属于组织' } }, 404);
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/admin/user-governance/platform-direct-readiness') {
      return json(route, { success: true, readiness: { configured: true, ready: true, status: 'ready', label: '已就绪', checks: [] } });
    }
    if (path === '/api/admin/user-governance/users' && request.method() === 'GET') {
      return json(route, { success: true, users: [listItem(70)], total: 1, page: 1, page_size: 100 });
    }
    if (path === '/api/admin/user-governance/users/70' && request.method() === 'GET') return json(route, detail(70));
    if (path === '/api/admin/users/70/clients' && request.method() === 'GET') return json(route, scope);
    if (path === '/api/admin/users/70/clients' && request.method() === 'PUT') {
      const payload = request.postDataJSON() as Record<string, unknown>;
      writes.push(payload);
      const brandIds = payload.brand_ids as number[];
      scope = {
        ...scope,
        scope_kind: payload.scope_kind as ScopeKind,
        brand_ids: brandIds,
        effective_brand_ids: brandIds,
        version: scope.version + 1,
        permission_version: scope.permission_version + 1,
        etag: `client-scope-v1-after-${writes.length}`,
      };
      return json(route, scope);
    }
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });
  return { writes, getScope: () => scope };
}

async function openClientsTab(page: Page) {
  await page.goto('/admin/users');
  await expect(page.getByTestId('admin-user-governance-page')).toBeVisible();
  await page.getByRole('button', { name: '客户与品牌' }).click();
  await expect(page.getByTestId('client-scope-editor')).toBeVisible();
}

async function expectNoOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => (
    document.documentElement.scrollWidth - document.documentElement.clientWidth
  ))).toBeLessThanOrEqual(1);
}

test('组织员工空分配显示零客户并以 organization_assigned CAS 保存', async ({ page }) => {
  const session = await installAdminSession(page, {
    scope_kind: 'organization_assigned',
    subject_kind: 'organization_member',
    editable: true,
    brand_ids: [],
    effective_brand_ids: [],
    empty_semantics: 'zero_clients',
    version: 5,
    permission_version: 8,
    etag: 'client-scope-v1-member-empty-baseline',
  });
  await openClientsTab(page);
  await expect(page.getByTestId('client-scope-empty-state')).toContainText('可访问 0 个客户');
  await page.getByLabel(`分配客户 ${longName}`).check();
  await page.getByTestId('client-scope-reason').fill('管理员按交付责任分配客户');
  await page.getByTestId('client-scope-save').click();
  await expect.poll(() => session.writes.length).toBe(1);
  expect(session.writes[0]).toEqual(expect.objectContaining({
    scope_kind: 'organization_assigned',
    expected_scope_kind: 'organization_assigned',
    expected_version: 5,
    etag: 'client-scope-v1-member-empty-baseline',
    brand_ids: [70],
  }));
  await expectNoOverflow(page);
});

test('旧账号空分配明确为兼容回退而非全部客户', async ({ page }) => {
  const session = await installAdminSession(page, {
    scope_kind: 'legacy_unrestricted',
    subject_kind: 'legacy_user',
    editable: true,
    brand_ids: [],
    effective_brand_ids: [70],
    empty_semantics: 'legacy_owner_role_fallback',
    version: 3,
    permission_version: 3,
    etag: 'client-scope-v1-legacy-unrestricted',
  });
  await openClientsTab(page);
  const empty = page.getByTestId('client-scope-empty-state');
  await expect(empty).toContainText('不代表全部客户');
  await page.getByLabel('分配客户 合法服务客户品牌').check();
  await page.getByTestId('client-scope-reason').fill('旧账号保留指定服务客户');
  await page.getByTestId('client-scope-save').click();
  await expect.poll(() => session.writes.length).toBe(1);
  expect(session.writes[0]).toEqual(expect.objectContaining({
    scope_kind: 'legacy_selected',
    expected_scope_kind: 'legacy_unrestricted',
    brand_ids: [71],
  }));
  await expectNoOverflow(page);
});

test('组织老板客户范围来自 owner 身份且编辑器只读', async ({ page }) => {
  const session = await installAdminSession(page, {
    scope_kind: 'organization_assigned',
    subject_kind: 'organization_owner',
    editable: false,
    brand_ids: [],
    effective_brand_ids: [70, 71],
    empty_semantics: 'owner_identity_all',
    version: 9,
    permission_version: 12,
    etag: 'client-scope-v1-owner-readonly',
  });
  await openClientsTab(page);
  await expect(page.getByTestId('client-scope-empty-state')).toContainText('来自 owner 身份');
  await expect(page.getByTestId('client-scope-save')).toHaveCount(0);
  await expect(page.getByLabel(`分配客户 ${longName}`)).toBeDisabled();
  await expect(page.getByLabel('分配客户 合法服务客户品牌')).toBeDisabled();
  expect(session.writes).toHaveLength(0);
  await expectNoOverflow(page);
});


test('切换用户后迟到保存响应不得污染新用户范围或下一次请求', async ({ page }) => {
  let releaseUserA!: () => void;
  const userAGate = new Promise<void>((resolve) => { releaseUserA = resolve; });
  let userAResponseCompleted = false;
  const writes: Array<{ userId: number; payload: Record<string, unknown> }> = [];
  const scopeFor = (userId: number) => userId === 70 ? {
    success: true,
    user_id: 70,
    scope_kind: 'organization_assigned' as const,
    subject_kind: 'organization_member' as const,
    editable: true,
    brand_ids: [],
    effective_brand_ids: [],
    available_brands: [{ id: 70, name: 'A 用户客户', owner_user_id: 1 }],
    stale_brand_ids: [],
    empty_semantics: 'zero_clients',
    version: 5,
    permission_version: 8,
    etag: 'client-scope-user-a-v5',
    organization_id: 1,
    membership_id: 2,
    principal_user_id: 1,
    organization_authority_version: 11,
    membership_status: 'active',
  } : {
    success: true,
    user_id: 71,
    scope_kind: 'legacy_selected' as const,
    subject_kind: 'legacy_user' as const,
    editable: true,
    brand_ids: [171],
    effective_brand_ids: [171],
    available_brands: [
      { id: 170, name: 'B 待新增客户', owner_user_id: 71 },
      { id: 171, name: 'B 原有客户', owner_user_id: 71 },
    ],
    stale_brand_ids: [],
    empty_semantics: 'legacy_selected',
    version: 22,
    permission_version: 30,
    etag: 'client-scope-user-b-v22',
    organization_id: null,
    membership_id: null,
    principal_user_id: 71,
    organization_authority_version: null,
    membership_status: null,
  };

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'admin-client-scope-race-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-20T00:00:00.000Z',
      last_updated_at: '2026-07-20T00:00:00.000Z',
    }));
  });
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const scopeMatch = path.match(/^\/api\/admin\/users\/(70|71)\/clients$/);
    const detailMatch = path.match(/^\/api\/admin\/user-governance\/users\/(70|71)$/);
    if (path === '/api/auth/me') return json(route, { success: true, user: adminUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'admin-refresh', user: adminUser });
    if (path === '/api/organization/overview') return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND' } }, 404);
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/admin/user-governance/platform-direct-readiness') {
      return json(route, { success: true, readiness: { configured: true, ready: true, status: 'ready', label: '已就绪', checks: [] } });
    }
    if (path === '/api/admin/user-governance/users' && request.method() === 'GET') {
      const requestedPage = Number(new URL(request.url()).searchParams.get('page') || '1');
      return json(route, {
        success: true,
        users: [listItem(requestedPage === 1 ? 70 : 71)],
        total: 2,
        page: requestedPage,
        page_size: 30,
        total_pages: 2,
      });
    }
    if (detailMatch && request.method() === 'GET') return json(route, detail(Number(detailMatch[1])));
    if (scopeMatch && request.method() === 'GET') return json(route, scopeFor(Number(scopeMatch[1])));
    if (scopeMatch && request.method() === 'PUT') {
      const userId = Number(scopeMatch[1]);
      const payload = request.postDataJSON() as Record<string, unknown>;
      writes.push({ userId, payload });
      if (userId === 70) {
        await userAGate;
        userAResponseCompleted = true;
        return json(route, { ...scopeFor(70), brand_ids: [70], effective_brand_ids: [70], version: 6, permission_version: 9, etag: 'client-scope-user-a-v6' });
      }
      const brandIds = payload.brand_ids as number[];
      return json(route, { ...scopeFor(71), brand_ids: brandIds, effective_brand_ids: brandIds, version: 23, permission_version: 31, etag: 'client-scope-user-b-v23' });
    }
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await openClientsTab(page);
  await page.getByLabel('分配客户 A 用户客户').check();
  await page.getByTestId('client-scope-reason').fill('A 用户保存故意延迟');
  await page.getByTestId('client-scope-save').click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0]).toEqual(expect.objectContaining({ userId: 70 }));

  const backButton = page.getByRole('button', { name: '返回用户列表' });
  if (await backButton.isVisible()) await backButton.click();
  await page.getByRole('button', { name: '下一页' }).click();
  await expect(page.getByText('user71 · 用户 ID 71')).toBeVisible();
  // Switching accounts/pages preserves the selected tab while the old account
  // detail/editor is cleared before the new response arrives.
  await expect(page.getByTestId('tab-clients')).toBeVisible();
  const editor = page.getByTestId('client-scope-editor');
  await expect(editor).toHaveAttribute('data-scope-kind', 'legacy_selected');
  await expect(editor).toContainText('范围版本 22');

  releaseUserA();
  await expect.poll(() => userAResponseCompleted).toBe(true);
  await expect(editor).toContainText('范围版本 22');
  await expect(page.getByLabel('分配客户 B 原有客户')).toBeChecked();
  await expect(page.getByLabel('分配客户 B 待新增客户')).not.toBeChecked();

  await page.getByLabel('分配客户 B 待新增客户').check();
  await page.getByTestId('client-scope-reason').fill('B 用户独立保存');
  await page.getByTestId('client-scope-save').click();
  await expect.poll(() => writes.length).toBe(2);
  expect(writes[1]).toEqual({
    userId: 71,
    payload: expect.objectContaining({
      expected_scope_kind: 'legacy_selected',
      expected_version: 22,
      etag: 'client-scope-user-b-v22',
      brand_ids: [170, 171],
      reason: 'B 用户独立保存',
    }),
  });
  await expect(editor).toContainText('范围版本 23');
  await expectNoOverflow(page);
});

test('服务端总页数收缩后自动 clamp 到仍存在的第一页', async ({ page }) => {
  const requestedPages: number[] = [];
  let pageTwoReads = 0;
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'admin-pagination-clamp-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({ version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [], dismissed_features: [], viewed_videos: [], first_seen_at: '2026-07-20T00:00:00Z', last_updated_at: '2026-07-20T00:00:00Z' }));
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: adminUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'admin-refresh', user: adminUser });
    if (path === '/api/organization/overview') return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND' } }, 404);
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/admin/user-governance/platform-direct-readiness') return json(route, { success: true, readiness: { configured: true, ready: true, status: 'ready', label: '已就绪', checks: [] } });
    if (path === '/api/admin/user-governance/users' && request.method() === 'GET') {
      const requestedPage = Number(url.searchParams.get('page') || '1');
      requestedPages.push(requestedPage);
      if (requestedPage === 2) {
        pageTwoReads += 1;
        if (pageTwoReads > 1) return json(route, { success: true, users: [], total: 1, page: 2, page_size: 30, total_pages: 1 });
        return json(route, { success: true, users: [listItem(71)], total: 60, page: 2, page_size: 30, total_pages: 2 });
      }
      return json(route, { success: true, users: [listItem(70)], total: pageTwoReads > 1 ? 1 : 60, page: 1, page_size: 30, total_pages: pageTwoReads > 1 ? 1 : 2 });
    }
    const detailMatch = path.match(/^\/api\/admin\/user-governance\/users\/(70|71)$/);
    if (detailMatch) return json(route, detail(Number(detailMatch[1])));
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto('/admin/users');
  await expect(page.getByTestId('admin-user-governance-page')).toBeVisible();
  const back = page.getByRole('button', { name: '返回用户列表' });
  if ((page.viewportSize()?.width || 0) < 1024) {
    await expect(back).toBeVisible();
    await back.click();
  }
  await page.getByRole('button', { name: '下一页' }).click();
  await expect.poll(() => requestedPages.filter(value => value === 2).length).toBe(1);
  await page.locator('header').getByRole('button', { name: '刷新' }).click();
  await expect.poll(() => requestedPages.slice(-2)).toEqual([2, 1]);
  if ((page.viewportSize()?.width || 0) < 1024) {
    await expect(back).toBeVisible();
    await back.click();
  }
  await expect(page.getByTestId('user-row-70')).toBeVisible();
  await expect(page.getByText('1 / 2')).toHaveCount(0);
  await expectNoOverflow(page);
});

test('桌面收起用户导航后缩到移动端会恢复可用列表', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await installAdminSession(page, {
    scope_kind: 'legacy_selected', subject_kind: 'legacy_user', editable: true,
    brand_ids: [70], effective_brand_ids: [70], empty_semantics: 'legacy_selected',
    version: 1, permission_version: 1, etag: 'responsive-collapse-v1',
  });
  await page.goto('/admin/users');
  await expect(page.getByTestId('admin-user-governance-page')).toBeVisible();
  await page.getByRole('button', { name: '收起用户导航' }).click();
  await expect(page.getByTestId('user-list')).toHaveCount(0);
  await page.setViewportSize({ width: 390, height: 844 });
  const back = page.getByRole('button', { name: '返回用户列表' });
  await expect(back).toBeVisible();
  await back.click();
  await expect(page.getByTestId('user-list')).toBeVisible();
  await expect(page.getByTestId('user-row-70')).toBeVisible();
  await expectNoOverflow(page);
});
