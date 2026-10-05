import { expect, test, type Page, type Route } from 'playwright/test';

const ownerUser = {
  id: 1,
  username: 'organization-owner',
  display_name: '老板验证账号',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 0,
  roles: [{ id: 1, name: 'user', display_name: '普通账号' }],
  permissions: ['settings:read'],
  client_brand_ids: [10, 18],
};

const memberUser = {
  ...ownerUser,
  id: 2,
  username: 'organization-member',
  display_name: '员工验证账号',
  client_brand_ids: [10],
};

const salesCapabilities = [
  'clients.read_assigned', 'clients.profile_edit', 'materials.read_assigned',
  'diagnosis.read_own', 'diagnosis.run', 'diagnosis.export',
  'quote.read_own', 'quote.create', 'quote.submit_for_approval',
  'monitoring.read_assigned', 'monitoring.run', 'monitoring.retry', 'reports.read_own',
];
const deliveryCapabilities = [
  'clients.read_assigned', 'materials.read_assigned', 'materials.write',
  'writing.read_own', 'writing.generate', 'writing.review', 'writing.share_internal',
  'publish.plan', 'publish.submit_for_approval', 'publish.execute',
  'monitoring.read_assigned', 'monitoring.run', 'monitoring.retry',
  'reports.read_own', 'reports.generate', 'reports.export',
];
const readonlyCapabilities = [
  'clients.read_assigned', 'materials.read_assigned', 'diagnosis.read_own',
  'quote.read_own', 'writing.read_own', 'monitoring.read_assigned',
  'reports.read_own', 'team.output_read',
];

const entitlement = {
  id: 1,
  entitled_seats: 20,
  occupied_seats: 2,
  available_seats: 18,
  product_catalog_version: 'organization-basic-team-v1',
  source_sku: 'organization-basic-team',
};

const ownerOverview = {
  id: 1,
  owner_user_id: 1,
  name: '华东超长品牌增长交付与客户成功服务中心（移动端长中文验证）',
  status: 'active',
  version: 7,
  authority_version: 11,
  viewer_is_owner: true,
  entitlement,
  identity: {
    actor_kind: 'owner',
    membership_id: 1,
    authority_version: '11:7:8:9',
    capabilities: [],
  },
  assigned_brand_ids: [10, 18],
};

function memberOverview(role: 'sales' | 'delivery') {
  return {
    ...ownerOverview,
    viewer_is_owner: false,
    identity: {
      actor_kind: 'member',
      membership_id: 2,
      authority_version: role === 'sales' ? '11:3:4:5' : '11:8:9:10',
      capabilities: role === 'sales' ? salesCapabilities : deliveryCapabilities,
    },
    assigned_brand_ids: [10],
  };
}

const roles = [
  { id: 1, organization_id: 1, code: 'owner', name: '老板', is_owner_role: true, version: 1, capabilities: [] },
  { id: 2, organization_id: 1, code: 'sales', name: '销售', is_owner_role: false, version: 2, capabilities: salesCapabilities },
  { id: 3, organization_id: 1, code: 'delivery', name: '交付', is_owner_role: false, version: 2, capabilities: deliveryCapabilities },
  { id: 4, organization_id: 1, code: 'readonly', name: '只读协作', is_owner_role: false, version: 2, capabilities: readonlyCapabilities },
];

function membersFor(role: 'sales' | 'delivery' = 'sales') {
  const selected = roles.find(item => item.code === role)!;
  return [
    { id: 1, user_id: 1, status: 'active', is_owner: true, version: 1, capability_version: 1, assignment_version: 1, display_name: '老板验证账号', role_id: 1, role_name: '老板', role_code: 'owner', assigned_brand_ids: [10, 18], effective_capabilities: [], capability_overrides: {} },
    { id: 2, user_id: 2, status: 'active', is_owner: false, version: 3, capability_version: 4, assignment_version: 5, display_name: '员工姓名也可以非常非常长用于验证折行而不会泄漏或撑破布局', role_id: selected.id, role_name: selected.name, role_code: selected.code, assigned_brand_ids: [10], effective_capabilities: selected.capabilities, capability_overrides: {} },
  ];
}

const now = new Date('2026-07-22T12:00:00Z');
const later = new Date('2026-07-23T12:00:00Z');

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

interface MutationRecord {
  method: string;
  path: string;
  body: Record<string, unknown>;
}

async function installSession(
  page: Page,
  actor: 'owner' | 'member',
  mutations: MutationRecord[],
  memberRole: 'sales' | 'delivery' = 'sales',
) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'organization-ui-local-intercept-only');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-22T00:00:00.000Z',
      last_updated_at: '2026-07-22T00:00:00.000Z',
    }));
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (path === '/api/auth/me') return json(route, { success: true, user: actor === 'owner' ? ownerUser : memberUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'local-refresh-only', user: actor === 'owner' ? ownerUser : memberUser });
    if (path === '/api/organization/overview') return json(route, actor === 'owner' ? ownerOverview : memberOverview(memberRole));
    if (method !== 'GET' && path.startsWith('/api/organization')) {
      const body = (request.postDataJSON() || {}) as Record<string, unknown>;
      mutations.push({ method, path, body });
      if (path === '/api/organization/invites') return json(route, { id: 33, delivery_token: null });
      if (path.endsWith('/assignments')) return json(route, { success: true, status: 'active', version: 9 });
      return json(route, { success: true, status: 'active', version: 9, delivery_token: null });
    }
    if (path === '/api/organization/members') return json(route, actor === 'owner' ? membersFor(memberRole) : [membersFor(memberRole)[1]]);
    if (path === '/api/organization/roles') return json(route, roles);
    if (path === '/api/organization/invites') return json(route, [{
      id: 31,
      organization_id: 1,
      target_kind: 'phone',
      role_id: 2,
      status: 'pending',
      expires_at: later.toISOString(),
      resend_count: 0,
      version: 1,
      created_at: now.toISOString(),
      access_policy_hash: 'local-only',
      access_policy: {
        version: 'invite-access-policy-v1',
        brand_ids: [10],
        capability_overrides: {},
        artifact_scope: 'own',
        daily_limit_points: 200,
        monthly_limit_points: 3000,
        feature_limits: {},
        high_risk_expansions: [],
      },
    }]);
    if (path === '/api/organization/limits') return json(route, [{ id: 41, membership_id: 2, limit_kind: 'monthly_total', feature_code: null, limit_points: 3000, reserved_points: 130, consumed_points: 500, refunded_points: 30, period_start: now.toISOString(), period_end: later.toISOString(), status: 'active', policy_version: 2 }]);
    if (path === '/api/organization/approvals') return json(route, [
      { id: 51, requested_by_membership_id: 2, requested_by_user_id: 2, action_type: 'quote.send_external', estimated_points: 40, status: 'pending', policy_version: 3, expires_at: later.toISOString(), version: 1, created_at: now.toISOString() },
      { id: 52, requested_by_membership_id: 2, requested_by_user_id: 2, action_type: 'report.share_external', estimated_points: 0, status: 'pending', policy_version: 3, expires_at: later.toISOString(), version: 1, created_at: now.toISOString() },
    ]);
    if (path === '/api/organization/approval-policies') return json(route, [{ id: 61, version: 3, status: 'active', action_type: 'billing.execute_high_cost', threshold_points: 1000, always_require_approval: false }]);
    if (path === '/api/organization/automatic-plans') return json(route, [{ id: 71, feature_code: 'monitor_single', work_kind: 'monitoring.run', brand_id: 10, cadence_seconds: 86400, max_occurrences: 30, scheduled_occurrences: 1, settled_occurrences: 2, total_budget_points: 3900, max_occurrence_points: 130, reserved_budget_points: 130, consumed_budget_points: 260, refunded_budget_points: 0, next_occurrence_at: later.toISOString(), ends_at: '2026-08-20T12:00:00.000Z', status: 'active', version: 4 }]);
    if (path === '/api/organization/audit') return json(route, [{ id: 81, action: 'assignment.replace', actor_kind: 'owner', actor_user_id: 1, entity_type: 'organization_membership', entity_id: '2', reason: '老板更新客户分配，超长原因文案用于验证单元格折行', created_at: now.toISOString() }]);
    if (path === '/api/my-clients') return json(route, { success: true, clients: [{ id: 10, name: '浙江岱林生物技术股份有限公司' }, { id: 18, name: '另一家超长客户品牌名称用于视觉折行验证' }] });
    if (path === '/api/wallet') return json(route, { success: true, data: { paid_points: 100000, commission_points: 0, bonus_points: 0, frozen_points: 0, total_recharged: 100000, customer_credit_status: 'ready' } });
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    return json(route, { success: true, status: 'success', data: {}, items: [], total: 0 });
  });
}

async function expectMutation(mutations: MutationRecord[], method: string, path: string) {
  await expect.poll(() => mutations.some(item => item.method === method && item.path === path)).toBe(true);
}

async function expectNoPageOverflow(page: Page) {
  await expect.poll(() => page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }))).toEqual(expect.objectContaining({ clientWidth: page.viewportSize()?.width }));
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
}

async function openInviteWizard(page: Page) {
  const wizard = page.locator('[data-testid="owner-invite-wizard"]:visible');
  if (!(await wizard.count())) {
    await page.locator('button:visible').filter({ hasText: '邀请员工' }).last().click();
  }
  await expect(wizard).toBeVisible();
  return wizard;
}

async function openMemberDrawer(page: Page) {
  if ((page.viewportSize()?.width ?? 0) >= 1024) {
    const desktopManage = page.getByRole('button', { name: /管理 员工姓名/ }).first();
    await expect(desktopManage).toBeVisible();
    await desktopManage.click();
  } else {
    const mobileEdit = page.getByRole('button', { name: '编辑成员' });
    await expect(mobileEdit).toBeVisible();
    await mobileEdit.click();
  }
  await expect(page.getByRole('dialog', { name: '编辑成员' })).toBeVisible();
}

async function exposeSidebar(page: Page) {
  const diagnosis = page.getByRole('link', { name: /品牌体检/ });
  if (!(await diagnosis.first().isVisible().catch(() => false))) {
    const menu = page.getByLabel('打开或收起主菜单');
    if (await menu.isVisible().catch(() => false)) await menu.click();
  }
}

async function closeMobileSidebar(page: Page) {
  const overlay = page.getByRole('button', { name: '关闭主菜单' });
  if (!(await overlay.isVisible().catch(() => false))) return;
  const viewport = page.viewportSize();
  if (!viewport) throw new Error('viewport is required to close the mobile sidebar');
  await page.mouse.click(viewport.width - 4, Math.floor(viewport.height / 2));
  await expect(overlay).toBeHidden();
}

test('V2 老板操作台完成邀请、成员治理和全部高级治理按钮，七档视口无溢出', async ({ page }) => {
  const mutations: MutationRecord[] = [];
  await installSession(page, 'owner', mutations);
  page.on('dialog', dialog => void dialog.accept());
  await page.goto('/organization/team');
  await expect(page.getByTestId('organization-center')).toBeVisible();
  await expect(page.getByText(ownerOverview.name, { exact: true })).toBeVisible();
  if ((page.viewportSize()?.width ?? 0) >= 768) {
    await expect(page.locator('p:visible').filter({ hasText: '销售和交付各管各的，单个员工不能一个人从谈单做到交付。' })).toBeVisible();
  }
  await expectNoPageOverflow(page);

  if (page.viewportSize()?.width === 1440) {
    await page.screenshot({ path: '../_qa_organization_internal_seats/owner-operations-v2-1440.png', fullPage: true });
  }

  const wizard = await openInviteWizard(page);
  await expect(wizard.getByTestId('invite-role-sales')).toContainText('我的客户 · 诊断 · 报价 · 监测');
  await expect(wizard.getByTestId('invite-role-delivery')).toContainText('我的客户 · 写作 · 发布 · 监测');
  await expect(wizard.getByTestId('invite-role-readonly')).toContainText('只看已分配客户和授权结果');
  await wizard.locator('input[placeholder="输入员工手机号"]').fill('13800138000');
  await wizard.getByTestId('invite-role-sales').click();
  await wizard.getByLabel('浙江岱林生物技术股份有限公司').check();
  await wizard.getByTestId('invite-daily-limit').fill('200');
  await wizard.getByTestId('invite-monthly-limit').fill('3000');
  await wizard.getByRole('button', { name: '生成邀请并复制链接' }).click();
  await expectMutation(mutations, 'POST', '/api/organization/invites');
  const invite = mutations.find(item => item.path === '/api/organization/invites')!;
  expect(invite.body).toMatchObject({ target_kind: 'phone', target: '13800138000', role_id: 2, brand_ids: [10], daily_limit_points: 200, monthly_limit_points: 3000, capability_overrides: {} });

  await page.locator('button:visible').filter({ hasText: '重发' }).first().click();
  await page.locator('button:visible').filter({ hasText: '撤销' }).first().click();
  await expectMutation(mutations, 'POST', '/api/organization/invites/31/resend');
  await expectMutation(mutations, 'POST', '/api/organization/invites/31/revoke');

  await openMemberDrawer(page);
  const drawer = page.getByRole('dialog', { name: '编辑成员' });
  await drawer.getByLabel('另一家超长客户品牌名称用于视觉折行验证').check();
  await drawer.getByRole('button', { name: '保存变更' }).click();
  await expectMutation(mutations, 'PUT', '/api/organization/members/2/assignments');
  await openMemberDrawer(page);
  await page.getByRole('dialog', { name: '编辑成员' }).getByRole('button', { name: '暂停席位' }).click();
  await expectMutation(mutations, 'POST', '/api/organization/members/2/status');
  await openMemberDrawer(page);
  await page.getByRole('dialog', { name: '编辑成员' }).getByRole('button', { name: '移除员工' }).click();

  await page.getByRole('button', { name: '员工费用与算力上限' }).click();
  await page.getByLabel('每日上限').fill('2000');
  await page.getByLabel('每月上限').fill('30000');
  await page.getByRole('button', { name: '保存上限' }).click();
  await page.getByRole('button', { name: '审批队列' }).click();
  await page.getByLabel('需要审批的算力阈值').fill('1200');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('button', { name: '通过' }).first().click();
  await page.getByRole('button', { name: '拒绝' }).last().click();
  await page.getByRole('button', { name: '自动计划' }).click();
  await page.getByLabel('客户', { exact: true }).selectOption('10');
  await page.getByRole('button', { name: '创建自动计划' }).click();
  await page.getByRole('button', { name: '暂停', exact: true }).click();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await page.getByRole('button', { name: '操作记录' }).click();
  // [B10] 审计三列已全部走中文映射;mock 里的 action 换成代码真会写的 `assignment.replace`
  await expect(page.getByText('更新客户分配')).toBeVisible();
  await expectNoPageOverflow(page);
});

test('默认销售与交付互斥，跨域扩大必须二次确认并写入邀请审计载荷', async ({ page }) => {
  const mutations: MutationRecord[] = [];
  await installSession(page, 'owner', mutations);
  await page.goto('/organization/team');
  const wizard = await openInviteWizard(page);
  await wizard.locator('input[placeholder="输入员工手机号"]').fill('13900139000');
  await wizard.getByTestId('invite-advanced-permissions').locator('summary').click();
  // [C1 2026-08-17] 29 个勾选框改成按模块分组折叠,勾选前要先展开对应分组。
  const openGroup = async (title: string) => {
    const group = wizard.locator(`details[data-testid="capability-group-${title}"]`);
    if (!(await group.evaluate((node: HTMLDetailsElement) => node.open))) {
      await group.locator('summary').click();
    }
  };
  await openGroup('诊断');
  await openGroup('文章');
  const diagnosis = wizard.locator('label').filter({ hasText: '发起诊断' }).getByRole('checkbox');
  const writing = wizard.locator('label').filter({ hasText: '生成文章' }).getByRole('checkbox');
  await expect(diagnosis).toBeChecked();
  await expect(writing).not.toBeChecked();

  await wizard.getByTestId('invite-role-delivery').click();
  await openGroup('诊断');
  await openGroup('文章');
  await expect(diagnosis).not.toBeChecked();
  await expect(writing).toBeChecked();
  await wizard.getByTestId('invite-role-sales').click();
  await openGroup('文章');
  await writing.check();
  const submit = wizard.getByRole('button', { name: '生成邀请并复制链接' });
  await expect(submit).toBeDisabled();
  await wizard.getByLabel('我确认要额外给他这些权限，此操作会被记录。').check();
  await wizard.locator('input[placeholder="说明为什么要给他这些额外权限"]').fill('客户交接期需要销售临时生成文章');
  await submit.click();
  await expectMutation(mutations, 'POST', '/api/organization/invites');
  expect(mutations.find(item => item.path === '/api/organization/invites')?.body).toMatchObject({
    role_id: 2,
    capability_overrides: { 'writing.generate': 'allow' },
    high_risk_confirmed: true,
    high_risk_reason: '客户交接期需要销售临时生成文章',
  });
});

test('销售员工只出现诊断、报价、监测入口，写作与发布入口不存在', async ({ page }) => {
  const mutations: MutationRecord[] = [];
  await installSession(page, 'member', mutations, 'sales');
  page.on('dialog', dialog => void dialog.accept());
  await page.goto('/organization/team');
  await exposeSidebar(page);
  await expect(page.getByRole('link', { name: /品牌体检/ }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: /报价方案/ }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: /效果监测/ }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: /AI 写文章/ })).toHaveCount(0);
  await expect(page.getByRole('link', { name: /发布投放/ })).toHaveCount(0);
  await expect(page.getByText('我的员工席位')).toBeVisible();
  for (const forbidden of ['邀请员工', '自动计划', '审计记录']) {
    await expect(page.getByRole('button', { name: forbidden, exact: true })).toHaveCount(0);
  }
  await closeMobileSidebar(page);
  await page.getByRole('button', { name: '员工费用与算力上限' }).click();
  // [B1] 卡片标题原来直出 `monthly_total`,现在必须是中文;顺带断言裸值已消失。
  await expect(page.getByText('每月总上限')).toBeVisible();
  await expect(page.getByText('monthly_total')).toHaveCount(0);
  await page.getByRole('button', { name: '员工费用与算力上限' }).click();
  await page.getByRole('button', { name: '退出团队' }).click();
  await expectMutation(mutations, 'POST', '/api/organization/leave');
  await expectNoPageOverflow(page);
});

test('交付员工只出现写作、发布、监测入口，诊断与报价入口不存在', async ({ page }) => {
  const mutations: MutationRecord[] = [];
  await installSession(page, 'member', mutations, 'delivery');
  await page.goto('/organization/team');
  const writing = page.getByRole('link', { name: /AI 写文章/ });
  if (!(await writing.first().isVisible().catch(() => false))) {
    const menu = page.getByLabel('打开或收起主菜单');
    if (await menu.isVisible().catch(() => false)) await menu.click();
  }
  await expect(writing.first()).toBeVisible();
  await expect(page.getByRole('link', { name: /发布投放/ }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: /效果监测/ }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: /品牌体检/ })).toHaveCount(0);
  await expect(page.getByRole('link', { name: /报价方案/ })).toHaveCount(0);
  await expect(page.getByText('交付', { exact: true })).toBeVisible();
  await expectNoPageOverflow(page);
});

test('登出后延迟的组织响应不得回填上一账号信息', async ({ page }) => {
  let releaseOverview!: () => void;
  const overviewGate = new Promise<void>(resolve => { releaseOverview = resolve; });
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'organization-stale-response-test-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({ version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [], dismissed_features: [], viewed_videos: [], first_seen_at: '2026-07-22T00:00:00.000Z', last_updated_at: '2026-07-22T00:00:00.000Z' }));
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: ownerUser });
    if (path === '/api/organization/overview') { await overviewGate; return json(route, ownerOverview); }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    return json(route, { success: true, data: {}, items: [], total: 0 });
  });
  page.on('dialog', dialog => void dialog.accept());
  await page.goto('/organization/team');
  const logout = page.getByLabel('退出登录');
  if (await logout.count() === 0) await page.getByLabel('打开或收起主菜单').click();
  await logout.evaluate((button: HTMLButtonElement) => button.click());
  await expect(page).toHaveURL(/\/login/);
  releaseOverview();
  await page.waitForTimeout(100);
  await expect(page.getByText(ownerOverview.name, { exact: true })).toHaveCount(0);
});

test('创建团队失败持久展示行动原因且重试复用幂等键', async ({ page }) => {
  const payloads: Array<{ name: string; request_id: string }> = [];
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'organization-create-feedback-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({ version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [], dismissed_features: [], viewed_videos: [], first_seen_at: '2026-07-22T00:00:00.000Z', last_updated_at: '2026-07-22T00:00:00.000Z' }));
  });
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: ownerUser });
    if (path === '/api/organization/overview') return json(route, { detail: { code: 'ORG_MEMBERSHIP_REQUIRED', message: '当前账号尚未加入组织', request_id: 'overview-no-organization', retryable: false } }, 404);
    if (path === '/api/organization' && request.method() === 'POST') {
      payloads.push(request.postDataJSON());
      return json(route, { detail: { code: 'ORG_SEATS_DISABLED', message: '团队功能暂未开放，请联系管理员检查团队席位策略', request_id: 'create-disabled', retryable: false } }, 503);
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    return json(route, { success: true, data: {}, items: [], total: 0 });
  });
  await page.goto('/organization/team');
  await page.getByLabel('团队名称').fill('华东品牌增长服务中心');
  const create = page.getByRole('button', { name: '创建团队' });
  await create.click();
  await expect(page.getByTestId('organization-action-error')).toHaveText('团队功能暂未开放，请联系管理员检查团队席位策略');
  await expect(create).toBeEnabled();
  await create.click();
  await expect.poll(() => payloads.length).toBe(2);
  expect(payloads[0].request_id).toBe(payloads[1].request_id);
  expect(payloads[0].name).toBe('华东品牌增长服务中心');
});
