import { expect, test, type Page, type Route } from 'playwright/test';
import fs from 'node:fs';
import path from 'node:path';

const adminUser = {
  id: 80, user_id: 80, username: 'platform-admin', display_name: '平台管理员',
  is_admin: true, is_active: 1, must_change_password: 0, agent_level: 1,
  roles: [{ id: 2, name: 'admin', display_name: '管理员' }],
  permissions: ['users:read', 'users:admin'], client_brand_ids: [],
};

const demoUser = {
  id: 132, user_id: 132, username: 'demo-132', display_name: '演示账号 132',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 0,
  roles: [{ id: 3, name: 'customer', display_name: '客户' }],
  permissions: ['dashboard:read'], client_brand_ids: [],
};

const cases = [
  { case_id: '56cc6f1e-bd50-584f-8702-2e044d20bce5', brand_id: 601, diagnosis_id: 9601, brand_name: '浙江岱林', industry: '生命科学', owner_user_id: 124, owner_label: '历史证据缺失客户 #124', diagnosis_created_at: '2026-07-20T10:00:00Z', frozen: false },
  { case_id: 'ee69c619-bfa2-56a8-84fc-b9db1f1cb9d9', brand_id: 602, diagnosis_id: 9602, brand_name: '浙江岱林', industry: '工业设备', owner_user_id: 129, owner_label: '双关系客户 #129', diagnosis_created_at: '2026-07-20T11:00:00Z', frozen: false },
];

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

function userListItem() {
  return {
    user_id: 132, username: 'demo_132', display_name: '明日演示账号一三二', phone: '139****0132',
    is_active: true, business_identity: 'customer', platform_access: 'standard', total_points: 1182,
    customer_count: 0, brand_count: 0, service_mode: 'customer', needs_attention: false,
    versions: { business_identity: 1, commercial_binding: 1, channel_relationship: 1, platform_access: 1, password_security: 1, wallet_adjustment: 1 },
  };
}

function userDetail() {
  return {
    success: true,
    overview: {
      user_id: 132, username: 'demo_132', display_name: '明日演示账号一三二', phone: '139****0132', company: '演示账号主体',
      is_active: true, business_identity: 'customer', business_identity_label: '客户', platform_access: 'standard', platform_access_label: '普通账号',
      total_points: 1182, paid_points: 1132, bonus_points: 50, total_recharged_points: 5132, customer_count: 0, brand_count: 0,
      versions: { business_identity: 1, commercial_binding: 1, channel_relationship: 1, platform_access: 1, password_security: 1, wallet_adjustment: 1 },
    },
    relationships: { registration: { present: false, source: 'none', evidence: { status: 'not_required', label: '无邀请记录' } }, commercial: { mode: 'platform_direct', binding_source_label: '平台直营', relationship_version: 1, evidence: { status: 'not_required', label: '不适用' } }, channel: { mode: 'platform_root' }, dual_relationships_present: false, notices: [] },
    pricing_and_settlement: { customer_pricing_route: '平台动态价目表', procurement_pricing_route: '平台直接供货', settlement_route: '平台结算', pricing_source: '版本化商品配置' },
    clients_and_brands: { clients: [], brands: [] },
    wallet_and_billing: { paid_points: 1132, bonus_points: 50, total_points: 1182, total_recharged_points: 5132, recent_orders: [], recent_transactions: [] },
    permissions_and_security: { platform_access: 'standard', account_status_label: '正常', must_change_password: false, permission_version: 3, legacy_roles: [] },
    operation_logs: [],
  };
}

async function seedSession(page: Page, token: string) {
  await page.addInitScript((value) => {
    localStorage.setItem('omnirank_token', value);
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({ version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [], dismissed_features: [], viewed_videos: [], first_seen_at: '2026-07-21T00:00:00.000Z', last_updated_at: '2026-07-21T00:00:00.000Z' }));
  }, token);
}

async function expectNoOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
}

test('管理员按不可变 ID 区分同名客户并批量授权、立即撤回，预览输入刷新即失', async ({ page }) => {
  await seedSession(page, 'admin-demo-customer-token');
  page.on('dialog', dialog => dialog.accept());
  const grantWrites: Array<Record<string, unknown>> = [];
  const revokeWrites: Array<Record<string, unknown>> = [];
  let grants: Array<Record<string, unknown>> = [];

  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: adminUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'admin-refresh', user: adminUser });
    if (path === '/api/organization/overview') return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND' } }, 404);
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/admin/user-governance/platform-direct-readiness') return json(route, { success: true, readiness: { configured: true, ready: true, status: 'ready', label: '已就绪', checks: [] } });
    if (path === '/api/admin/user-governance/users' && request.method() === 'GET') return json(route, { success: true, users: [userListItem()], total: 1, page: 1, page_size: 30, total_pages: 1 });
    if (path === '/api/admin/user-governance/users/132' && request.method() === 'GET') return json(route, userDetail());
    if (path === '/api/admin/cross-tenant-governance/demo-cases/catalog') return json(route, { success: true, cases, total: 2, page: 1, page_size: 100, total_pages: 1 });
    if (path === '/api/admin/cross-tenant-governance/demo-grants' && request.method() === 'GET') return json(route, { success: true, grants, total: grants.length, page: 1, page_size: 100, total_pages: 1 });
    if (path === '/api/admin/cross-tenant-governance/demo-grants' && request.method() === 'POST') {
      const payload = request.postDataJSON() as Record<string, unknown>;
      grantWrites.push(payload);
      grants = cases.map((item, index) => ({ id: 41 + index, ...item, grantee_kind: 'user', grantee_user_id: 132, grantee_label: '明日演示账号一三二', case_label: item.brand_name, capability: 'demo.customer.preview', status: 'active', expires_at: '2026-08-20T00:00:00Z', note: '明日销售演示', version: 1 }));
      return json(route, { success: true, grants });
    }
    if (path === '/api/admin/cross-tenant-governance/demo-grants/batch-revoke' && request.method() === 'POST') {
      const payload = request.postDataJSON() as Record<string, unknown>;
      revokeWrites.push(payload);
      grants = grants.map(grant => ({ ...grant, status: 'revoked', version: 2 }));
      return json(route, { success: true, grants });
    }
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto('/admin/users');
  await expect(page.getByTestId('admin-user-governance-page')).toBeVisible();
  await page.getByRole('button', { name: '演示客户' }).click();
  await expect(page.getByTestId('demo-grant-panel')).toBeVisible();
  await expect(page.getByText('浙江岱林')).toHaveCount(2);
  await expect(page.getByText(/owner 历史证据缺失客户 #124 · 生命科学/)).toBeVisible();
  await expect(page.getByText(/owner 双关系客户 #129 · 工业设备/)).toBeVisible();

  const catalog = page.getByTestId('demo-case-catalog');
  await catalog.getByRole('button').filter({ hasText: 'brand #601' }).click();
  await catalog.getByRole('button').filter({ hasText: 'brand #602' }).click();
  await page.getByPlaceholder('销售场景/审批单').fill('明日销售演示');
  await page.getByPlaceholder('例如：Owner 批准明日产品演示').fill('Owner 批准 user_id=132 演示');
  await page.getByRole('button', { name: '批量授权 2' }).click();
  await expect.poll(() => grantWrites.length).toBe(1);
  expect(grantWrites[0]).toEqual(expect.objectContaining({
    grantee_kind: 'user', grantee_user_id: 132, capability: 'demo.customer.preview', confirmation: 'GRANT_DEMO_CUSTOMER_PREVIEW',
    selections: [
      { case_id: cases[0].case_id, brand_id: 601, diagnosis_id: 9601 },
      { case_id: cases[1].case_id, brand_id: 602, diagnosis_id: 9602 },
    ],
  }));

  const grantSelectors = page.getByRole('button', { name: '选择授权' });
  await expect(grantSelectors).toHaveCount(2);
  await grantSelectors.nth(0).click();
  await grantSelectors.nth(1).click();
  await page.getByRole('button', { name: '批量撤回 2' }).click();
  await expect.poll(() => revokeWrites.length).toBe(1);
  expect(revokeWrites[0]).toEqual(expect.objectContaining({
    confirmation: 'REVOKE_DEMO_ACCESS',
    grants: [{ grant_id: 41, expected_version: 1 }, { grant_id: 42, expected_version: 1 }],
  }));

  await page.getByPlaceholder('销售场景/审批单').fill('仅用于本地流程讲解，刷新即失');
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('omnirank-demo-action-preview', { detail: {
    code: 'DEMO_ACTION_PREVIEW', message: '演示模式，不会保存、扣费或调用外部服务', access_mode: 'demo', action: 'writing.generate', blocked_reason: 'demo_side_effect_blocked', saved: false, charged: false, provider_called: false, external_service_called: false, job_created: false, refresh_discards_local_preview: true, real_mode_effects: ['校验参数与权限', '调用生成服务并保存文章'], request_id: 'pw-demo-preview',
  } })));
  await expect(page.getByTestId('demo-action-preview')).toContainText('演示模式，不会保存、扣费或调用外部服务');
  await expect(page.getByTestId('demo-action-preview')).toContainText('未保存、未扣费、未调用模型/搜索/外部服务');
  await page.getByRole('button', { name: '继续演示' }).click();
  await page.reload();
  await page.getByRole('button', { name: '演示客户' }).click();
  await expect(page.getByPlaceholder('销售场景/审批单')).toHaveValue('');
  await expectNoOverflow(page);
});

test('演示授权切换账号到组织时丢弃迟到列表与保存 continuation', async ({ page }) => {
  await seedSession(page, 'admin-demo-grant-race-token');
  let releaseUserList!: () => void;
  let releaseCreate!: () => void;
  const userListGate = new Promise<void>(resolve => { releaseUserList = resolve; });
  const createGate = new Promise<void>(resolve => { releaseCreate = resolve; });
  let delayUserList = true;
  let delayCreate = false;
  const requestedAuthorities: string[] = [];
  const userGrant = { id: 51, ...cases[0], grantee_kind: 'user', grantee_user_id: 132, grantee_label: '账号 132', case_label: '账号迟到授权客户', capability: 'demo.customer.preview', status: 'active', expires_at: '2026-08-20T00:00:00Z', note: '', version: 1 };
  const organizationGrant = { id: 61, ...cases[1], grantee_kind: 'organization', grantee_organization_id: 77, grantee_label: '组织 77', case_label: '组织当前授权客户', capability: 'demo.customer.preview', status: 'active', expires_at: '2026-08-20T00:00:00Z', note: '', version: 1 };

  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: adminUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'admin-refresh', user: adminUser });
    if (path === '/api/organization/overview') return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND' } }, 404);
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/admin/user-governance/platform-direct-readiness') return json(route, { success: true, readiness: { configured: true, ready: true, status: 'ready', label: '已就绪', checks: [] } });
    if (path === '/api/admin/user-governance/users' && request.method() === 'GET') return json(route, { success: true, users: [userListItem()], total: 1, page: 1, page_size: 30, total_pages: 1 });
    if (path === '/api/admin/user-governance/users/132' && request.method() === 'GET') return json(route, userDetail());
    if (path === '/api/admin/cross-tenant-governance/demo-cases/catalog') return json(route, { success: true, cases, total: 2, page: 1, page_size: 100, total_pages: 1 });
    if (path === '/api/admin/cross-tenant-governance/demo-grants' && request.method() === 'GET') {
      const current = `${url.searchParams.get('grantee_kind')}:${url.searchParams.get('grantee_id')}`;
      requestedAuthorities.push(current);
      if (current === 'user:132' && delayUserList) {
        await userListGate;
        delayUserList = false;
        return json(route, { success: true, grants: [userGrant], total: 1, page: 1, page_size: 100, total_pages: 1 });
      }
      return json(route, { success: true, grants: current === 'organization:77' ? [organizationGrant] : [], total: current === 'organization:77' ? 1 : 0, page: 1, page_size: 100, total_pages: 1 });
    }
    if (path === '/api/admin/cross-tenant-governance/demo-grants' && request.method() === 'POST') {
      if (delayCreate) await createGate;
      return json(route, { success: true, grants: [userGrant] });
    }
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto('/admin/users');
  await page.getByRole('button', { name: '演示客户' }).click();
  const panel = page.getByTestId('demo-grant-panel');
  await expect.poll(() => requestedAuthorities).toContain('user:132');
  await panel.getByRole('combobox').click();
  await page.getByRole('option', { name: '指定组织' }).click();
  await panel.getByLabel('组织 ID').fill('77');
  await expect(panel.getByText('组织当前授权客户')).toBeVisible();
  releaseUserList();
  await expect(panel.getByText('账号迟到授权客户')).toHaveCount(0);

  await panel.getByRole('combobox').click();
  await page.getByRole('option', { name: /当前账号 #132/ }).click();
  await expect(panel.getByText('当前对象没有演示客户授权。')).toBeVisible();
  await panel.getByTestId('demo-case-catalog').getByRole('button').filter({ hasText: 'brand #601' }).click();
  await panel.getByPlaceholder('例如：Owner 批准明日产品演示').fill('验证保存期间授权对象切换');
  delayCreate = true;
  await panel.getByRole('button', { name: '批量授权 1' }).click();
  await expect(panel.getByRole('button', { name: /批量授权/ })).toBeDisabled();
  await panel.getByRole('combobox').click();
  await page.getByRole('option', { name: '指定组织' }).click();
  await panel.getByLabel('组织 ID').fill('77');
  await expect(panel.getByText('组织当前授权客户')).toBeVisible();
  releaseCreate();
  await expect(panel.getByText('账号迟到授权客户')).toHaveCount(0);
  await expect(panel.getByRole('button', { name: /批量授权/ })).toBeEnabled();
  await expectNoOverflow(page);
});

test('账号 132 的客户切换器显示浙江岱林演示标识并为详情请求附统一 demo 上下文', async ({ page }) => {
  await seedSession(page, 'user-132-demo-token');
  const contextHeaders: Array<Record<string, string>> = [];
  const listHeaders: Array<Record<string, string>> = [];
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: demoUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'demo-refresh', user: demoUser });
    if (path === '/api/client-context/list') {
      listHeaders.push(request.headers());
      return json(route, { success: true, clients: [{ id: 601, name: '浙江岱林', industry: '生命科学', diagnosis_count: 1, latest_diagnosis_id: 9601, quote_count: 1, brand_status: 'active', owner_user_id: 124, owner_name: '已脱敏原客户', created_at: '2026-07-20T10:00:00Z', access_mode: 'demo', is_demo: true, demo_case_id: cases[0].case_id, demo_expires_at: '2026-08-20T00:00:00Z' }] });
    }
    if (path === `/api/demo-cases/${cases[0].case_id}`) {
      contextHeaders.push(request.headers());
      return json(route, { success: true, case: { case_id: cases[0].case_id, brand_id: 601, diagnosis_id: 9601, expires_at: '2026-08-20T00:00:00Z', customer_overview: { name: '浙江岱林', industry: '生命科学' }, diagnosis_snapshot: { score: 88 }, quote_snapshots: [{ id: 9701 }] } });
    }
    if (path === '/api/organization/overview') return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND' } }, 404);
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto('/dashboard');
  await expect.poll(() => contextHeaders.length).toBeGreaterThan(0);
  if (!(await page.getByText('浙江岱林').first().isVisible())) {
    await page.getByRole('button', { name: '打开或收起主菜单' }).click();
  }
  await expect(page.getByText('浙江岱林').first()).toBeVisible();
  await expect(page.getByText('演示案例').first()).toBeVisible();
  expect(contextHeaders[0]['x-demo-brand-id']).toBe('601');
  expect(contextHeaders[0]['x-demo-case-id']).toBe(cases[0].case_id);
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('omnirank-api-mutated', { detail: { tags: ['clients'] } })));
  await expect.poll(() => listHeaders.length).toBeGreaterThan(1);
  expect(listHeaders.at(-1)?.['x-demo-brand-id']).toBeUndefined();
  expect(listHeaders.at(-1)?.['x-demo-case-id']).toBeUndefined();
  await expectNoOverflow(page);
});

const monitoringViewportMatrix = [
  { width: 320, height: 720 }, { width: 390, height: 844 }, { width: 768, height: 1024 },
  { width: 1366, height: 768 }, { width: 1440, height: 900 }, { width: 1920, height: 1080 },
  { width: 2560, height: 1440 },
];

async function installMonitoringFixture(
  page: Page,
  mode: 'live' | 'demo',
  options: {
    failDemoContext?: boolean;
    identityItems?: Array<Record<string, unknown>>;
    identityReviewStatus?: number;
  } = {},
) {
  const demoCaseId = '4ed3e12e-fcac-5589-b2be-07db2585ba92';
  const demoPortalEntry = 'DAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
  const ordinaryReads: string[] = [];
  const mutations: string[] = [];
  const requestCounts = new Map<string, number>();
  await seedSession(page, `dailin-${mode}-monitoring-token`);
  await page.addInitScript(() => sessionStorage.setItem('omnirank_current_brand_candidate:46', '592'));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const requestPath = url.pathname;
    const requestKey = `${request.method()} ${requestPath}`;
    requestCounts.set(requestKey, (requestCounts.get(requestKey) || 0) + 1);
    const user = {
      ...demoUser, id: 46, user_id: 46, username: 'fixture-operator', agent_level: 1, permission_version: 7,
      permissions: ['dashboard:read', 'clients:read', 'diagnosis:read', 'quote:read', 'writing:read', 'publish:read', 'monitoring:read', 'reports:read'],
    };
    if (requestPath === '/api/auth/me') return json(route, { success: true, user });
    if (requestPath === '/api/auth/refresh') return json(route, { success: true, token: `dailin-${mode}-refresh`, user });
    if (requestPath === '/api/organization/overview') return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND' } }, 404);
    if (requestPath === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (requestPath === '/api/client-context/list') return json(route, { success: true, clients: [{
      id: 592, name: '浙江岱林生物技术股份有限公司', industry: '制造业', diagnosis_count: 1,
      latest_diagnosis_id: 9592, quote_count: 1, brand_status: 'active', owner_user_id: 28,
      owner_name: '已脱敏原客户', created_at: '2026-07-20T10:00:00Z', access_mode: mode === 'demo' ? 'demo' : 'real',
      is_demo: mode === 'demo', demo_case_id: mode === 'demo' ? demoCaseId : undefined,
      demo_expires_at: mode === 'demo' ? '2026-08-21T00:00:00Z' : undefined,
    }] });
    if (requestPath === `/api/demo-cases/${demoCaseId}`) {
      if (options.failDemoContext) return json(route, { detail: '资源不存在' }, 404);
      return json(route, { success: true, case: {
      case_id: demoCaseId, brand_id: 592, diagnosis_id: 9592, expires_at: '2026-08-21T00:00:00Z',
      frozen_at: '2026-07-21T12:00:00Z', customer_overview: { brand_name: '浙江岱林生物技术股份有限公司', industry: '制造业' },
      diagnosis_snapshot: { total_score: 72, created_at: '2026-07-20T10:00:00Z' }, quote_snapshots: [{ id: 9701 }],
      } });
    }
    if (requestPath === '/api/client-context/592') return json(route, { success: true, context: {
      brand: { id: 592, name: '浙江岱林生物技术股份有限公司', industry: '制造业', diagnosis_count: 1, brand_type: 'client' },
      profile: { industry: '制造业' }, materials: null, relatedQuoteIds: ['9701'], socialProjects: [], access_mode: 'real',
    } });

    if (requestPath.startsWith('/api/monitoring') || requestPath.startsWith('/api/publications') || requestPath === '/api/logs' || requestPath.startsWith('/api/portal/tokens')) {
      ordinaryReads.push(`${request.method()} ${requestPath}`);
      if (mode === 'demo' && request.method() === 'GET') {
        expect(request.headers()['x-demo-brand-id']).toBe('592');
        expect(request.headers()['x-demo-case-id']).toBe(demoCaseId);
      }
      if (request.method() !== 'GET') {
        mutations.push(`${request.method()} ${requestPath}`);
        return json(route, { detail: {
          code: 'DEMO_ACTION_PREVIEW', message: '演示案例为只读', access_mode: 'demo', action: 'portal.token.regenerate',
          blocked_reason: 'DEMO_SIDE_EFFECT_BOUNDARY', saved: false, charged: false, provider_called: false,
          external_service_called: false, job_created: false, refresh_discards_local_preview: true,
          real_mode_effects: ['签发新的真实客户门户凭证', '使旧凭证失效'], request_id: 'pw-demo-readonly',
        } }, 409);
      }
      if (requestPath === '/api/monitoring/clients') return json(route, { status: 'success', clients: [{ quote_id: 9701, brand_id: 592, brand_name: '浙江岱林生物技术股份有限公司', industry: '制造业', keyword_count: 1, status: 'paid', service_days: 30, service_start_date: '2026-07-01' }], access_mode: mode });
      if (requestPath === '/api/monitoring/clients/9701/keywords') return json(route, { status: 'success', keywords: [{ id: 9831, keyword: '生物安全柜', target_brand: '浙江岱林', source: 'confirmed', lifecycle: 'monitoring', detection_rate: 75, effective_rate: 75, target_rate: 65, is_compliant: true, compliant_days: 12, remaining_days: 18, service_days: 30 }], super_red_ocean_keywords: [], access_mode: mode });
      if (requestPath === '/api/monitoring/trend') return json(route, { status: 'success', trend: [{ date: '2026-07-20', rate: 68 }, { date: '2026-07-21', rate: 75 }], access_mode: mode });
      if (requestPath === '/api/monitoring/schedule') return json(route, { status: 'success', monitoring_enabled: false, jobs: [], access_mode: mode });
      if (requestPath === '/api/monitoring/client/9701/monitoring-config') return json(route, { status: 'success', monitoring_enabled: false, monitoring_interval_hours: 24, monitoring_start_hour: 8, access_mode: mode });
      if (requestPath === '/api/monitoring/archives') return json(route, { status: 'success', archives: [], access_mode: mode });
      if (requestPath === '/api/monitoring/rollback/tasks' || requestPath === '/api/monitoring/tasks') return json(route, { status: 'success', tasks: [], access_mode: mode });
      if (requestPath === '/api/monitoring/identity-reviews') {
        if (options.identityReviewStatus) {
          return json(route, { detail: '当前账号无权访问 brand_id' }, options.identityReviewStatus);
        }
        return json(route, { items: options.identityItems || [], access_mode: mode });
      }
      if (requestPath === '/api/portal/tokens/9701') return json(route, { status: 'success', token: {
        token: mode === 'demo' ? demoPortalEntry : 'LIVEPORTAL12', is_active: true, expires_at: '2026-08-21', access_mode: mode,
        demo_transport_entry: mode === 'demo' ? demoPortalEntry : undefined,
      }, access_mode: mode });
      return json(route, { status: 'success', items: [], access_mode: mode });
    }
    return json(route, { success: true, status: 'success', items: [], data: {}, total: 0 });
  });
  return { ordinaryReads, mutations, requestCounts };
}

async function monitoringDomShape(page: Page) {
  return page.locator('[data-access-mode]').evaluate((root) => {
    const shape = (node: Element): unknown => ({
      tag: node.tagName,
      cls: node.getAttribute('class'),
      role: node.getAttribute('role'),
      children: Array.from(node.children).map(shape),
    });
    return shape(root);
  });
}

for (const viewport of monitoringViewportMatrix) {
  test(`监测 live/demo 共用 DOM 与布局 ${viewport.width}`, async ({ browser }) => {
    const live = await browser.newPage({ viewport });
    const demo = await browser.newPage({ viewport });
    const liveTraffic = await installMonitoringFixture(live, 'live');
    const demoTraffic = await installMonitoringFixture(demo, 'demo');
    await Promise.all([live.goto('/monitoring?brand_id=592'), demo.goto('/monitoring?brand_id=592')]);
    await Promise.all([
      expect(live.getByRole('heading', { name: '监测中心' })).toBeVisible(),
      expect(demo.getByRole('heading', { name: '监测中心' })).toBeVisible(),
    ]);
    await Promise.all([
      expect(live.getByText('生物安全柜', { exact: true })).toBeVisible(),
      expect(demo.getByText('生物安全柜', { exact: true })).toBeVisible(),
    ]);
    expect(await monitoringDomShape(demo)).toEqual(await monitoringDomShape(live));
    await expectNoOverflow(live);
    await expectNoOverflow(demo);
    expect(liveTraffic.ordinaryReads).toContain('GET /api/monitoring/clients');
    expect(demoTraffic.ordinaryReads).toContain('GET /api/monitoring/clients');
    expect(demoTraffic.mutations).toEqual([]);
    const output = path.resolve('output/playwright/demo-wysiwyg');
    fs.mkdirSync(output, { recursive: true });
    await live.screenshot({ path: path.join(output, `monitoring-live-${viewport.width}.png`), fullPage: true });
    await demo.screenshot({ path: path.join(output, `monitoring-demo-${viewport.width}.png`), fullPage: true });
    await live.close();
    await demo.close();
  });
}

test('客户门户设置 live/demo 同组件且 demo regenerate 零副作用', async ({ browser }) => {
  const viewport = { width: 1440, height: 900 };
  const live = await browser.newPage({ viewport });
  const demo = await browser.newPage({ viewport });
  await installMonitoringFixture(live, 'live');
  const demoTraffic = await installMonitoringFixture(demo, 'demo');
  await Promise.all([live.goto('/monitoring?brand_id=592'), demo.goto('/monitoring?brand_id=592')]);
  await Promise.all([
    expect(live.getByText('生物安全柜', { exact: true })).toBeVisible(),
    expect(demo.getByText('生物安全柜', { exact: true })).toBeVisible(),
  ]);
  await live.getByText('客户门户', { exact: true }).last().click();
  await demo.getByText('客户门户', { exact: true }).last().click();
  const liveDialog = live.getByRole('dialog', { name: '客户门户设置' });
  const demoDialog = demo.getByRole('dialog', { name: '客户门户设置' });
  await Promise.all([expect(liveDialog).toBeVisible(), expect(demoDialog).toBeVisible()]);
  await expect(demoDialog).not.toContainText('LIVEPORTAL12');
  const dialogShape = (page: Page) => page.getByRole('dialog', { name: '客户门户设置' }).evaluate((root) => {
    const shape = (node: Element): unknown => ({
      tag: node.tagName,
      cls: node.getAttribute('class'),
      children: Array.from(node.children).map(shape),
    });
    return shape(root);
  });
  expect(await dialogShape(demo)).toEqual(await dialogShape(live));
  const output = path.resolve('output/playwright/demo-wysiwyg');
  fs.mkdirSync(output, { recursive: true });
  await live.screenshot({ path: path.join(output, 'portal-settings-live-1440.png'), fullPage: true });
  await demo.screenshot({ path: path.join(output, 'portal-settings-demo-1440.png'), fullPage: true });
  await demoDialog.getByRole('button', { name: '重新生成' }).click();
  await expect(demo.getByTestId('demo-action-preview')).toContainText('演示案例为只读');
  expect(demoTraffic.mutations).toEqual(['POST /api/portal/tokens']);
  await demo.screenshot({ path: path.join(output, 'portal-settings-demo-readonly-preview-1440.png'), fullPage: true });
  await live.close();
  await demo.close();
});

async function businessMainShape(page: Page) {
  return page.getByTestId('app-page-main').evaluate((root) => {
    const shape = (node: Element): unknown => ({
      tag: node.tagName,
      cls: node.getAttribute('class'),
      role: node.getAttribute('role'),
      children: Array.from(node.children).map(shape),
    });
    return shape(root);
  });
}

test('全 surface live/demo 共用业务页面结构', async ({ browser }) => {
  const surfaces = [
    '/my-clients',
    '/diagnosis/report/9592',
    '/pricing?brand_id=592',
    '/writing?brand_id=592',
    '/publish?brand_id=592',
    '/reports?brand_id=592',
  ];
  for (const surface of surfaces) {
    const live = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const demo = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    await installMonitoringFixture(live, 'live');
    await installMonitoringFixture(demo, 'demo');
    await Promise.all([
      live.goto(surface, { waitUntil: 'networkidle' }),
      demo.goto(surface, { waitUntil: 'networkidle' }),
    ]);
    await Promise.all([
      expect(live.getByTestId('app-page-main')).toBeVisible(),
      expect(demo.getByTestId('app-page-main')).toBeVisible(),
    ]);
    await expect(live.getByText('当前账号没有此页面权限')).toHaveCount(0);
    await expect(demo.getByText('当前账号没有此页面权限')).toHaveCount(0);
    expect(await demo.getByTestId('app-page-main').locator('*').count()).toBeGreaterThan(2);
    expect(await businessMainShape(demo)).toEqual(await businessMainShape(live));
    await expectNoOverflow(live);
    await expectNoOverflow(demo);
    await live.close();
    await demo.close();
  }
});

test('监测 URL 直达回退与迟到响应保持 demo/live 租户隔离', async ({ page }) => {
  const demoCaseId = 'demo-case-593-wysiwyg';
  let holdDemoKeywords = false;
  let releaseDemoKeywords: (() => void) | null = null;
  let demoKeywordStarted = 0;
  const seenReads: Array<{ path: string; demoBrand?: string }> = [];
  await seedSession(page, 'monitoring-navigation-token');
  await page.route('**/api/**', async route => {
    const request = route.request();
    const requestPath = new URL(request.url()).pathname;
    const user = { ...demoUser, id: 46, user_id: 46, agent_level: 1, permissions: ['monitoring:read'] };
    if (requestPath === '/api/auth/me') return json(route, { success: true, user });
    if (requestPath === '/api/auth/refresh') return json(route, { success: true, token: 'navigation-refresh', user });
    if (requestPath === '/api/organization/overview') return json(route, { detail: { code: 'ORGANIZATION_NOT_FOUND' } }, 404);
    if (requestPath === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (requestPath === '/api/client-context/list') return json(route, { success: true, clients: [
      { id: 592, name: '正式客户甲', industry: '制造业', diagnosis_count: 1, quote_count: 1, brand_status: 'active', access_mode: 'real' },
      { id: 593, name: '冻结演示乙', industry: '生命科学', diagnosis_count: 1, quote_count: 1, brand_status: 'active', access_mode: 'demo', is_demo: true, demo_case_id: demoCaseId },
    ] });
    if (requestPath === '/api/client-context/592') return json(route, { success: true, context: {
      brand: { id: 592, name: '正式客户甲', industry: '制造业', diagnosis_count: 1 },
      profile: { industry: '制造业' }, materials: null, relatedQuoteIds: ['9701'], socialProjects: [], access_mode: 'real',
    } });
    if (requestPath === `/api/demo-cases/${demoCaseId}`) return json(route, { success: true, case: {
      case_id: demoCaseId, brand_id: 593, diagnosis_id: 9593,
      customer_overview: { brand_name: '冻结演示乙', industry: '生命科学' },
      diagnosis_snapshot: { total_score: 70 }, quote_snapshots: [{ id: 9702 }],
    } });
    if (requestPath.startsWith('/api/monitoring') || requestPath.startsWith('/api/publications') || requestPath === '/api/logs' || requestPath.startsWith('/api/portal/tokens')) {
      const demoBrand = request.headers()['x-demo-brand-id'];
      seenReads.push({ path: requestPath, demoBrand });
      if (requestPath === '/api/monitoring/clients') return json(route, { status: 'success', clients: demoBrand === '593'
        ? [{ quote_id: 9702, brand_id: 593, brand_name: '冻结演示乙', keyword_count: 1 }]
        : [{ quote_id: 9701, brand_id: 592, brand_name: '正式客户甲', keyword_count: 1 }],
      });
      if (requestPath === '/api/monitoring/clients/9702/keywords') {
        demoKeywordStarted += 1;
        if (holdDemoKeywords) await new Promise<void>(resolve => { releaseDemoKeywords = resolve; });
        return json(route, { status: 'success', keywords: [{ id: 2, keyword: '演示乙冻结词', target_brand: '冻结演示乙', source: 'confirmed' }], super_red_ocean_keywords: [] });
      }
      if (requestPath === '/api/monitoring/clients/9701/keywords') return json(route, { status: 'success', keywords: [{ id: 1, keyword: '正式甲实时词', target_brand: '正式客户甲', source: 'confirmed' }], super_red_ocean_keywords: [] });
      if (requestPath === '/api/monitoring/schedule') return json(route, { status: 'success', monitoring_enabled: false, jobs: [] });
      if (requestPath.includes('monitoring-config')) return json(route, { status: 'success', monitoring_enabled: false });
      return json(route, { status: 'success', tasks: [], archives: [], trend: [], items: [] });
    }
    return json(route, { success: true, status: 'success', items: [] });
  });

  await page.goto('/monitoring?brand_id=593');
  await expect(page.getByText('演示乙冻结词', { exact: true })).toBeVisible();
  expect(seenReads.some(item => item.path === '/api/monitoring/clients/9702/keywords' && item.demoBrand === '593')).toBe(true);

  const clientSwitcher = page.getByRole('button', { name: '切换客户' });
  if (!await clientSwitcher.isVisible()) {
    await page.getByRole('button', { name: '打开或收起主菜单' }).click();
  }
  await expect(clientSwitcher).toBeVisible();
  await clientSwitcher.click();
  await page.getByText('正式客户甲', { exact: true }).last().click();
  await expect(page).toHaveURL(/brand_id=592/);
  await expect(page.getByText('正式甲实时词', { exact: true })).toBeVisible();
  expect(seenReads.some(item => item.path === '/api/monitoring/clients/9701/keywords' && item.demoBrand === undefined)).toBe(true);

  holdDemoKeywords = true;
  await page.goBack();
  await expect(page).toHaveURL(/brand_id=593/);
  await expect(page.getByText('冻结演示乙', { exact: true }).first()).toBeVisible();
  await page.goForward();
  await expect(page.getByText('正式甲实时词', { exact: true })).toBeVisible();
  releaseDemoKeywords?.();
  await page.waitForTimeout(100);
  await expect(page.getByText('演示乙冻结词', { exact: true })).toHaveCount(0);
});

test('演示详情失效时停止自动重选与整页请求风暴', async ({ page }) => {
  const fixture = await installMonitoringFixture(page, 'demo', { failDemoContext: true });
  await page.goto('/monitoring?brand_id=592');
  await expect(page.getByRole('alert').getByText('演示案例暂时无法加载')).toBeVisible();
  const contextKey = 'GET /api/demo-cases/4ed3e12e-fcac-5589-b2be-07db2585ba92';
  const readsAfterFailure = fixture.requestCounts.get(contextKey) || 0;
  const monitoringReadsAfterFailure = fixture.requestCounts.get('GET /api/monitoring/clients') || 0;
  expect(readsAfterFailure).toBeLessThanOrEqual(2);
  expect(monitoringReadsAfterFailure).toBe(0);
  await page.waitForTimeout(1_000);
  expect(fixture.requestCounts.get(contextKey) || 0).toBe(readsAfterFailure);
  expect(fixture.requestCounts.get('GET /api/monitoring/clients') || 0).toBe(monitoringReadsAfterFailure);

  await page.getByRole('button', { name: '重新验证访问权限' }).click();
  await expect.poll(() => fixture.requestCounts.get(contextKey) || 0).toBeGreaterThan(readsAfterFailure);
  await expect(page.getByRole('alert').getByText('演示案例暂时无法加载')).toBeVisible();
});

test('演示监测 403 不刷新 JWT、不重放、不重建客户上下文', async ({ page }) => {
  const fixture = await installMonitoringFixture(page, 'demo', { identityReviewStatus: 403 });
  await page.goto('/monitoring?brand_id=592');
  await expect(page.getByText('浙江岱林生物技术股份有限公司').first()).toBeVisible();
  const contextKey = 'GET /api/demo-cases/4ed3e12e-fcac-5589-b2be-07db2585ba92';
  await expect.poll(() => fixture.requestCounts.get('GET /api/monitoring/identity-reviews') || 0).toBe(1);
  const contextReads = fixture.requestCounts.get(contextKey) || 0;
  const clientReads = fixture.requestCounts.get('GET /api/monitoring/clients') || 0;
  await page.waitForTimeout(1_000);
  expect(fixture.requestCounts.get('POST /api/auth/refresh') || 0).toBe(0);
  expect(fixture.requestCounts.get('GET /api/monitoring/identity-reviews') || 0).toBe(1);
  expect(fixture.requestCounts.get(contextKey) || 0).toBe(contextReads);
  expect(fixture.requestCounts.get('GET /api/monitoring/clients') || 0).toBe(clientReads);
  expect(fixture.requestCounts.get('GET /api/client-context/592') || 0).toBe(0);
});

test('品牌确认原文渲染 GFM 且卡片内部可滚动不穿模', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const longEvidence = [
    '## 商务酒店推荐',
    '',
    '**直接结论：** 优先核验交通、会议空间和真实住客反馈。',
    '',
    '| 维度 | 核验动作 |',
    '| --- | --- |',
    '| 交通 | 核对地图距离 |',
    '| 会议 | 询问可用时段 |',
    '',
    ...Array.from({ length: 14 }, (_, index) => `${index + 1}. 第 ${index + 1} 条核验说明，需要保留完整上下文供操作员判断。`),
  ].join('\n');
  await installMonitoringFixture(page, 'live', {
    identityItems: [{
      id: 8801,
      keyword: '揭阳商务酒店哪家好',
      platform: 'kimi',
      response_snippet: longEvidence,
      identity_candidates: [],
      identity_evidence_hash: 'evidence-hash-8801',
      identity_decision_version: 1,
      tested_at: '2026-07-22T10:00:00Z',
    }],
  });
  await page.goto('/monitoring?brand_id=592');
  const evidence = page.getByTestId('identity-review-evidence-8801');
  await expect(evidence).toBeVisible();
  await expect(evidence.getByRole('heading', { name: '商务酒店推荐' })).toBeVisible();
  expect(await evidence.textContent()).not.toContain('## 商务酒店推荐');
  expect(await evidence.locator('table').count()).toBe(1);
  expect(await evidence.evaluate(element => element.scrollHeight > element.clientHeight)).toBe(true);
  await evidence.evaluate(element => { element.scrollTop = element.scrollHeight; });
  expect(await evidence.evaluate(element => element.scrollTop)).toBeGreaterThan(0);
  await expect(page.getByPlaceholder('填写正确的品牌名称')).toBeVisible();
  await expectNoOverflow(page);
});

async function installPortalFixture(page: Page, mode: 'live' | 'demo') {
  const liveToken = 'LIVEPORTAL12';
  const demoTransportEntry = 'DAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
  const entry = mode === 'demo' ? demoTransportEntry : liveToken;
  const requests: string[] = [];
  let revoked = false;
  let optionalReportsMissing = false;
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    requests.push(`${request.method()} ${url.pathname}`);
    if (url.pathname === '/api/portal/verify') {
      expect(request.headers()['x-demo-brand-id']).toBeUndefined();
      expect(request.headers()['x-demo-case-id']).toBeUndefined();
      expect(request.postDataJSON()).toEqual({ token: entry });
      return json(route, {
        status: 'success', valid: true, quote_id: 9701, brand_id: 592,
        brand_name: '浙江岱林生物技术股份有限公司', access_mode: mode,
        demo_transport_entry: mode === 'demo' ? demoTransportEntry : undefined,
      });
    }
    let target = `${url.pathname}${url.search}`;
    if (url.pathname.startsWith('/api/portal/demo/')) {
      expect(mode).toBe('demo');
      expect(request.method()).toBe('GET');
      if (revoked) return route.fulfill({
        status: 404,
        contentType: 'application/json',
        headers: { 'X-Error-Code': 'DEMO_PORTAL_AUTHORITY_LOST' },
        body: JSON.stringify({ detail: { code: 'DEMO_PORTAL_AUTHORITY_LOST', message: '演示授权已失效' } }),
      });
      target = url.searchParams.get('target') || '';
    }
    const targetUrl = new URL(target, 'http://fixture.local');
    if (targetUrl.pathname === '/api/monitoring/clients/9701/keywords') return json(route, {
      status: 'success', keywords: [{
        id: 9831, keyword: '生物安全柜', target_brand: '浙江岱林', source: 'confirmed',
        lifecycle: 'monitoring', detection_rate: 75, effective_rate: 75, display_rate: 75,
        target_rate: 65, is_compliant: true, is_stable: true, compliant_days: 12,
        remaining_compliant: 18, service_days: 30,
      }], covered_keywords: [], service_days: 30, service_start_date: '2026-07-01',
    });
    if (targetUrl.pathname === '/api/publications/9701') return json(route, {
      status: 'success', publications: [{ id: 44, platform_name: '人民网', article_title: '实验室生物安全实践', publish_date: '2026-07-18', status: 'published' }],
    });
    if (targetUrl.pathname === '/api/monitoring/trend') return json(route, {
      status: 'success', trend: [{ date: '2026-07-20', rate: 68 }, { date: '2026-07-21', rate: 75 }],
    });
    if (targetUrl.pathname === '/api/reports' && optionalReportsMissing) {
      return json(route, { detail: { code: 'DEMO_ROUTE_UNMAPPED', message: '冻结快照没有此可选模块' } }, 404);
    }
    if (targetUrl.pathname === '/api/reports') return json(route, { status: 'success', reports: [{
      id: 71, title: '客户周报', report_type: 'weekly', status: 'sent', created_at: '2026-07-21T00:00:00Z', content: '冻结周报正文',
    }] });
    if (targetUrl.pathname === '/api/insights/9701') return json(route, { status: 'success', insights: [{ text: '出现率保持稳定', type: 'positive' }] });
    if (targetUrl.pathname === '/api/public/whitelabel') return json(route, { status: 'success', whitelabel: null });
    return json(route, { status: 'success', items: [] });
  });
  return {
    entry, liveToken, demoTransportEntry, requests,
    missOptionalReports: () => { optionalReportsMissing = true; },
    revoke: () => { revoked = true; },
  };
}

async function portalMainShape(page: Page) {
  return page.locator('main').evaluate((root) => {
    const shape = (node: Element): unknown => {
      if (node.getAttribute('role') === 'status') return null;
      return {
        tag: node.tagName,
        cls: node.getAttribute('class'),
        children: Array.from(node.children).map(shape).filter(Boolean),
      };
    };
    return shape(root);
  });
}

test('demo 打开现有有效链接但同一门户组件只读取冻结 transport', async ({ browser }) => {
  const live = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const demo = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const liveFixture = await installPortalFixture(live, 'live');
  const demoFixture = await installPortalFixture(demo, 'demo');
  const [liveResponse, demoResponse] = await Promise.all([
    live.goto(`/portal/${liveFixture.entry}`),
    demo.goto(`/portal/${demoFixture.entry}`),
  ]);
  expect(liveResponse?.status()).toBe(200);
  expect(demoResponse?.status()).toBe(200);
  await Promise.all([
    live.waitForURL('**/portal/dashboard'),
    demo.waitForURL('**/portal/dashboard'),
  ]);
  await Promise.all([
    expect(live.getByText('生物安全柜', { exact: true })).toBeVisible(),
    expect(demo.getByText('生物安全柜', { exact: true })).toBeVisible(),
  ]);
  expect(await portalMainShape(demo)).toEqual(await portalMainShape(live));
  await expect(demo.getByText('演示案例 · 只读')).toBeVisible();
  await expect(demo.getByRole('button', { name: '导出 Excel' })).toBeDisabled();
  expect(demoFixture.requests.some(item => item.includes('customer-events'))).toBe(false);
  expect(demoFixture.requests.some(item => item.includes(`/api/portal/demo/${demoFixture.demoTransportEntry}/transport`))).toBe(true);
  expect(demoFixture.requests.filter(item => item.startsWith('POST '))).toEqual(['POST /api/portal/verify']);
  expect(await live.evaluate(() => localStorage.getItem('portal_token'))).toBe(liveFixture.liveToken);
  expect(await demo.evaluate(() => localStorage.getItem('portal_token'))).toBe(demoFixture.demoTransportEntry);
  expect(await demo.evaluate(() => localStorage.getItem('portal_demo_transport_entry'))).toBe(demoFixture.demoTransportEntry);
  expect(await demo.evaluate(() => JSON.stringify(localStorage))).not.toContain(liveFixture.liveToken);
  await expect(demo.locator('body')).not.toContainText(liveFixture.liveToken);
  await expectNoOverflow(live);
  await expectNoOverflow(demo);
  const output = path.resolve('output/playwright/demo-wysiwyg');
  fs.mkdirSync(output, { recursive: true });
  await live.screenshot({ path: path.join(output, 'portal-live-1440.png'), fullPage: true });
  await demo.screenshot({ path: path.join(output, 'portal-demo-1440.png'), fullPage: true });
  await live.close();
  await demo.close();
});

function portalVerificationSuccess(entry: string, quoteId: number) {
  return {
    status: 'success', valid: true, quote_id: quoteId, brand_id: 592,
    brand_name: '浙江岱林生物技术股份有限公司', access_mode: 'demo',
    demo_transport_entry: entry,
  };
}

async function installPortalRaceFixture(
  page: Page,
  verify: (route: Route, token: string) => Promise<void>,
) {
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === '/api/portal/verify') {
      expect(request.headers()['x-demo-brand-id']).toBeUndefined();
      expect(request.headers()['x-demo-case-id']).toBeUndefined();
      await verify(route, String(request.postDataJSON().token));
      return;
    }
    if (url.pathname.startsWith('/api/portal/demo/')) {
      const target = new URL(url.searchParams.get('target') || '/', 'http://fixture.local').pathname;
      if (target.includes('/keywords')) return json(route, { status: 'success', keywords: [], covered_keywords: [] });
      if (target.includes('/publications/')) return json(route, { status: 'success', publications: [] });
      if (target.includes('/reports')) return json(route, { status: 'success', reports: [] });
      if (target.includes('/insights/')) return json(route, { status: 'success', insights: [] });
      if (target.includes('/trend')) return json(route, { status: 'success', trend: [] });
    }
    return json(route, { status: 'success', items: [], whitelabel: null });
  });
}

async function spaNavigate(page: Page, target: string) {
  await page.evaluate((path) => {
    history.pushState({}, '', path);
    window.dispatchEvent(new PopStateEvent('popstate'));
  }, target);
}

test('PortalLogin A 慢响应不会覆盖 B 快响应', async ({ page }) => {
  const a = 'DAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
  const b = 'DBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB';
  const calls: string[] = [];
  await installPortalRaceFixture(page, async (route, token) => {
    calls.push(token);
    await new Promise(resolve => setTimeout(resolve, token === a ? 250 : 10));
    try { await json(route, portalVerificationSuccess(token, token === a ? 9701 : 9702)); } catch { /* aborted stale request */ }
  });
  await page.goto(`/portal/${a}`);
  await expect.poll(() => calls).toContain(a);
  await spaNavigate(page, `/portal/${b}`);
  await page.waitForURL('**/portal/dashboard');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('portal_token'))).toBe(b);
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => localStorage.getItem('portal_token'))).toBe(b);
  expect(await page.evaluate(() => localStorage.getItem('portal_quote_id'))).toBe('9702');
});

test('PortalLogin A→B→A 只提交最后一代响应', async ({ page }) => {
  const a = 'DAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
  const b = 'DBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB';
  const finalA = 'DCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC';
  let aCalls = 0;
  const calls: string[] = [];
  await installPortalRaceFixture(page, async (route, token) => {
    calls.push(token);
    if (token === a) aCalls += 1;
    const responseEntry = token === a && aCalls === 2 ? finalA : token;
    const delay = token === a && aCalls === 1 ? 250 : token === b ? 180 : 10;
    await new Promise(resolve => setTimeout(resolve, delay));
    try { await json(route, portalVerificationSuccess(responseEntry, responseEntry === finalA ? 9703 : 9701)); } catch { /* aborted stale request */ }
  });
  await page.goto(`/portal/${a}`);
  await expect.poll(() => calls.length).toBe(1);
  await spaNavigate(page, `/portal/${b}`);
  await expect.poll(() => calls.length).toBe(2);
  await spaNavigate(page, `/portal/${a}`);
  await page.waitForURL('**/portal/dashboard');
  await expect.poll(() => page.evaluate(() => localStorage.getItem('portal_token'))).toBe(finalA);
  await page.waitForTimeout(300);
  expect(await page.evaluate(() => localStorage.getItem('portal_token'))).toBe(finalA);
  expect(await page.evaluate(() => localStorage.getItem('portal_quote_id'))).toBe('9703');
});

test('PortalLogin 卸载后迟到响应不写 storage 或跳转', async ({ page }) => {
  const a = 'DAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
  let started = false;
  await installPortalRaceFixture(page, async (route, token) => {
    started = true;
    await new Promise(resolve => setTimeout(resolve, 200));
    try { await json(route, portalVerificationSuccess(token, 9701)); } catch { /* aborted stale request */ }
  });
  await page.goto(`/portal/${a}`);
  await expect.poll(() => started).toBe(true);
  await spaNavigate(page, '/login');
  await page.waitForTimeout(300);
  await expect(page).toHaveURL(/\/login$/);
  expect(await page.evaluate(() => localStorage.getItem('portal_token'))).toBeNull();
  expect(await page.evaluate(() => localStorage.getItem('portal_quote_id'))).toBeNull();
});

test('demo portal 缺内部 transport 立即 fail-closed 且不发送 live Bearer', async ({ page }) => {
  const handle = 'DAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA';
  const authorizations: Array<string | undefined> = [];
  await page.addInitScript((entry) => {
    localStorage.setItem('portal_token', entry);
    localStorage.setItem('portal_quote_id', '9701');
    localStorage.setItem('portal_brand_id', '592');
    localStorage.setItem('portal_brand_name', '不得残留客户');
    localStorage.setItem('portal_access_mode', 'demo');
  }, handle);
  await page.route('**/api/**', route => {
    authorizations.push(route.request().headers().authorization);
    return json(route, { status: 'success', items: [] });
  });
  await page.goto('/portal/dashboard');
  await page.waitForURL('**/portal?reason=demo-authority-lost');
  await expect(page.getByText('演示授权已失效')).toBeVisible();
  expect(authorizations.filter(Boolean)).toEqual([]);
  expect(await page.evaluate(() => localStorage.getItem('portal_token'))).toBeNull();
  expect(await page.evaluate(() => localStorage.getItem('portal_quote_id'))).toBeNull();
});

test('demo portal 普通资源 404 保留会话，authority code 才立即清屏', async ({ page }) => {
  const fixture = await installPortalFixture(page, 'demo');
  await page.goto(`/portal/${fixture.entry}`);
  await page.waitForURL('**/portal/dashboard');
  await expect(page.getByText('生物安全柜', { exact: true })).toBeVisible();
  fixture.missOptionalReports();
  await page.reload();
  await expect(page).toHaveURL(/\/portal\/dashboard$/);
  await expect(page.getByText('生物安全柜', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => localStorage.getItem('portal_token'))).toBe(fixture.demoTransportEntry);
  expect(await page.evaluate(() => localStorage.getItem('portal_demo_transport_entry'))).toBe(fixture.demoTransportEntry);
  fixture.revoke();
  await page.reload();
  await page.waitForURL('**/portal?reason=demo-authority-lost');
  await expect(page.getByText('演示授权已失效')).toBeVisible();
  await expect(page.getByText('生物安全柜', { exact: true })).toHaveCount(0);
  expect(await page.evaluate(() => localStorage.getItem('portal_token'))).toBeNull();
  expect(await page.evaluate(() => localStorage.getItem('portal_demo_transport_entry'))).toBeNull();
});

test('live portal 普通 5xx 不清除合法客户凭证', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('portal_token', 'LIVEPORTAL12');
    localStorage.setItem('portal_quote_id', '9701');
    localStorage.setItem('portal_brand_id', '592');
    localStorage.setItem('portal_brand_name', '浙江岱林');
    localStorage.setItem('portal_access_mode', 'live');
  });
  await page.route('**/api/**', route => json(route, { status: 'error', error: 'temporary' }, 500));
  await page.goto('/portal/dashboard');
  await page.waitForTimeout(150);
  await expect(page).toHaveURL(/\/portal\/dashboard$/);
  expect(await page.evaluate(() => localStorage.getItem('portal_token'))).toBe('LIVEPORTAL12');
  expect(await page.evaluate(() => localStorage.getItem('portal_access_mode'))).toBe('live');
});
