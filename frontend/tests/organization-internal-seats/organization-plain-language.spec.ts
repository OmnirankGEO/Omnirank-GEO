/**
 * 「团队与席位」人话化判据（工单 2026-08-17 §5-1 / §5-2 / §5-3 / §5-4）。
 *
 * 判据打**真渲染 DOM**,不打源码写法 —— 源码里有映射函数不代表调用点用上了
 * (审计 B4 实锤:`statusLabel()` 就在同文件 393 行,「我的员工席位」那处没用)。
 *
 * 每个「必须命中」都配一个「必须不命中」:
 *   §5-1 零英文枚举   ←→ 反向对照:同一批断言在**注入英文的 DOM** 上必须报红
 *                        (见 `英文扫描器自身的判别力自证`)
 *   §5-2 未知值兜底   ←→ 塞一个后端没有过的枚举值,渲染必须是「未知…（xxx）」
 *   §5-3 422 人话     ←→ 真 5xx 必须仍然是「服务不可用」,不能被翻译成输入错误
 *   §5-4 overview 零 404 ←→ 断言 console 里确实**抓得到** 404(用一个人造 404 自证)
 */
import { expect, test, type ConsoleMessage, type Page, type Route } from 'playwright/test';

/* ── 禁止出现在渲染 DOM 里的英文枚举值 ───────────────────────────────────
 * 取自后端枚举定义(见 scripts/verify-organization-enum-labels.mjs 的机械 diff),
 * 这里挑的是**会被渲染到用户面**的那些列。 */
const FORBIDDEN_ENUM_TOKENS = [
  // 成员/邀请状态(B4/B9/B12)
  'active', 'suspended', 'leaving', 'removed', 'pending', 'accepted', 'revoked', 'expired',
  // 上限类型 + 功能代码(B1/B5/B6)
  'monthly_total', 'daily_total', 'daily_feature', 'monthly_feature',
  'monitor_single', 'geo_diagnosis', 'article_gen', 'topic_gen', 'quote_generate', 'media_publish',
  // 审批状态与动作(B2/B3)
  'approved', 'rejected', 'executing', 'executed',
  'billing.execute_high_cost', 'quote.send_external', 'publish.execute',
  'diagnosis_report.share_external', 'monitoring_report.share_external',
  // 计划状态(B5)
  'paused', 'cancelled', 'completed',
  // 审计三列(B10)
  'org.invite.create', 'invite.create', 'membership.assignment.replace', 'assignment.replace',
  'artifact.revoke_share', 'organization_membership', 'organization_artifact_share',
  'owner', 'member', 'system',
  // 工程词(A4/B11)
  'occurrence', 'idempotency', 'claim_token', 'worker', 'durable', 'provider',
];

/** 中文红线词。「额度」违反全站「算力」铁律;「排查用」是调试信息直给用户。 */
const FORBIDDEN_PHRASES = ['额度', '排查用', '幂等键', '跨域扩大', '产物', '组织服务暂不可用'];

/**
 * 扫描页面**可见文本**里的违禁串。
 * 用 innerText 而不是 innerHTML —— 只判用户真能读到的字,不判 class 名、data-testid、
 * title 属性(原始码有意保留在 hover 里给客服)。
 */
async function visibleText(page: Page): Promise<string> {
  return await page.evaluate(() => (document.body as HTMLElement).innerText || '');
}

function scanForbidden(text: string): string[] {
  const hits: string[] = [];
  for (const token of FORBIDDEN_ENUM_TOKENS) {
    // 词边界:避免 'owner' 命中「老板」的英文 aria-label 之外的正常中文,也避免
    // 'active' 命中 'inactive'。点号要转义。
    const pattern = new RegExp(`(^|[^A-Za-z0-9_.])${token.replace(/\./g, '\\.')}([^A-Za-z0-9_.]|$)`);
    if (pattern.test(text)) hits.push(token);
  }
  for (const phrase of FORBIDDEN_PHRASES) {
    if (text.includes(phrase)) hits.push(phrase);
  }
  return hits;
}

/* ── 固定数据 ─────────────────────────────────────────────────────────── */

const ownerUser = {
  id: 1, username: 'organization-owner', display_name: '老板验证账号',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 0,
  roles: [{ id: 1, name: 'user', display_name: '普通账号' }],
  permissions: ['settings:read'], client_brand_ids: [10, 18],
};

const salesCapabilities = [
  'clients.read_assigned', 'clients.profile_edit', 'materials.read_assigned',
  'diagnosis.read_own', 'diagnosis.run', 'diagnosis.export',
  'quote.read_own', 'quote.create', 'quote.submit_for_approval',
  'monitoring.read_assigned', 'monitoring.run', 'monitoring.retry', 'reports.read_own',
];

const roles = [
  { id: 1, organization_id: 1, code: 'owner', name: '老板', is_owner_role: true, version: 1, capabilities: [] },
  { id: 2, organization_id: 1, code: 'sales', name: '销售', is_owner_role: false, version: 2, capabilities: salesCapabilities },
  { id: 3, organization_id: 1, code: 'delivery', name: '交付', is_owner_role: false, version: 2, capabilities: [] },
  { id: 4, organization_id: 1, code: 'readonly', name: '只读协作', is_owner_role: false, version: 2, capabilities: [] },
];

const now = new Date('2026-08-17T12:00:00Z');
const later = new Date('2026-08-20T12:00:00Z');

const ownerOverview = {
  has_organization: true,
  id: 1, owner_user_id: 1, name: 'QA测试团队-勿动', status: 'active',
  version: 7, authority_version: 11, viewer_is_owner: true,
  entitlement: {
    id: 1, entitled_seats: 20, occupied_seats: 2, available_seats: 18,
    product_catalog_version: 'organization-basic-team-v1', source_sku: 'organization-basic-team',
  },
  identity: { actor_kind: 'owner', membership_id: 1, authority_version: '11:1:1:1', capabilities: [] },
  assigned_brand_ids: [10, 18],
  assigned_brands: [{ id: 10, name: '浙江岱林生物技术股份有限公司' }, { id: 18, name: '另一家客户品牌' }],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

interface SessionOptions {
  /** §5-2 拆锁:往响应里塞后端从未出现过的枚举值。 */
  unknownEnums?: boolean;
}

async function installOwnerSession(page: Page, options: SessionOptions = {}) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'organization-plain-language-intercept-only');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-08-17T00:00:00.000Z', last_updated_at: '2026-08-17T00:00:00.000Z',
    }));
  });
  const unknown = options.unknownEnums === true;
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const method = request.method();
    if (path === '/api/auth/me') return json(route, { success: true, user: ownerUser });
    if (path === '/api/auth/refresh') return json(route, { success: true, token: 'local-refresh-only', user: ownerUser });
    if (path === '/api/organization/overview') return json(route, ownerOverview);
    if (path.endsWith('/link')) return json(route, { delivery_token: 'local-derived-token-0123456789' });
    if (method !== 'GET' && path.startsWith('/api/organization')) {
      if (path === '/api/organization/invites/delivery-states') {
        return json(route, { items: [{ invite_id: 31, deliveries: { invite_link: { state: 'superseded', failure_code: null, sent_at: null, updated_at: now.toISOString(), attempt_count: 1 } } }] });
      }
      return json(route, { success: true, status: 'active', version: 9, delivery_token: null });
    }
    if (path === '/api/organization/members') {
      return json(route, [
        { id: 1, user_id: 1, status: 'active', is_owner: true, version: 1, capability_version: 1, assignment_version: 1, display_name: '老板验证账号', role_id: 1, role_name: '老板', role_code: 'owner', assigned_brand_ids: [10, 18], effective_capabilities: [], capability_overrides: {} },
        // 员工状态故意用 `leaving`:它在 DB CHECK 里,但旧 statusLabel 的手挑 5 项里**没有**
        //   —— 正是 B9「fallback `|| status` 就是英文出口」的原样复现。
        //   unknownEnums 模式下换成后端从未出现过的值,验「未知（xxx）」兜底形态。
        { id: 2, user_id: 2, status: unknown ? 'zombie_state' : 'leaving', is_owner: false, version: 3, capability_version: 4, assignment_version: 5, display_name: '员工验证账号', role_id: 2, role_name: '销售', role_code: 'sales', assigned_brand_ids: [10], effective_capabilities: salesCapabilities, capability_overrides: {} },
      ]);
    }
    if (path === '/api/organization/roles') return json(route, roles);
    if (path === '/api/organization/invites') return json(route, [{
      id: 16, organization_id: 1, target_kind: 'username',
      login_name: 'jcfemw-qaseat01',
      role_id: 2, status: 'pending', expires_at: later.toISOString(), resend_count: 0,
      version: 1, created_at: now.toISOString(), access_policy_hash: 'local-only',
      access_policy: { version: 'invite-access-policy-v1', brand_ids: [10], capability_overrides: {}, artifact_scope: 'own', daily_limit_points: 200, monthly_limit_points: 3000, feature_limits: {}, high_risk_expansions: [] },
    }]);
    if (path === '/api/organization/limits') return json(route, [{
      id: 41, membership_id: 2,
      limit_kind: unknown ? 'weekly_total' : 'monthly_total',
      feature_code: unknown ? 'teleport_beam' : 'monitor_single',
      limit_points: 3000, reserved_points: 130, consumed_points: 500, refunded_points: 30,
      period_start: now.toISOString(), period_end: later.toISOString(),
      status: unknown ? 'melting' : 'open', policy_version: 2,
    }]);
    if (path === '/api/organization/approvals') return json(route, [{
      id: 51, requested_by_membership_id: 2, requested_by_user_id: 7,
      action_type: unknown ? 'wormhole.open' : 'billing.execute_high_cost',
      estimated_points: 400, status: unknown ? 'levitating' : 'pending',
      policy_version: 3, expires_at: later.toISOString(), version: 1, created_at: now.toISOString(),
    }]);
    if (path === '/api/organization/approval-policies') return json(route, [{ id: 61, version: 3, status: 'active', action_type: 'billing.execute_high_cost', threshold_points: 1000, always_require_approval: false }]);
    if (path === '/api/organization/automatic-plans') return json(route, [{
      id: 71, feature_code: unknown ? 'teleport_beam' : 'monitor_single', work_kind: 'monitoring.run', brand_id: 10,
      cadence_seconds: 86400, max_occurrences: 30, scheduled_occurrences: 1, settled_occurrences: 2,
      total_budget_points: 3900, max_occurrence_points: 130, reserved_budget_points: 130,
      consumed_budget_points: 260, refunded_budget_points: 0,
      next_occurrence_at: later.toISOString(), ends_at: later.toISOString(),
      status: unknown ? 'levitating' : 'active', version: 4,
    }]);
    if (path === '/api/organization/audit') return json(route, [{
      id: 81, action: unknown ? 'wormhole.open' : 'assignment.replace',
      actor_kind: unknown ? 'poltergeist' : 'owner', actor_user_id: 1,
      entity_type: unknown ? 'ectoplasm' : 'organization_membership', entity_id: '2',
      reason: '老板更新客户分配', created_at: now.toISOString(),
    }]);
    if (path === '/api/organization/short-code') return json(route, { short_code: 'jcfemw' });
    if (path === '/api/organization/payer-policy') return json(route, {
      viewer: 'owner', global_shared_payer_flag: true,
      policy: { organization_id: 1, shared_payer_enabled: false, overage_enabled: false, per_action_limit_points: null, daily_limit_points: null, monthly_limit_points: null, policy_version: 2 },
      usage: { daily_overage_used_points: 0, monthly_overage_used_points: 0, daily_period_start: now.toISOString(), daily_period_end: later.toISOString(), monthly_period_start: now.toISOString(), monthly_period_end: later.toISOString(), daily_overage_remaining_points: null, monthly_overage_remaining_points: null },
    });
    if (path === '/api/my-clients') return json(route, { success: true, clients: [{ id: 10, name: '浙江岱林生物技术股份有限公司' }, { id: 18, name: '另一家客户品牌' }] });
    if (path === '/api/wallet') return json(route, { success: true, data: { paid_points: 100000, commission_points: 0, bonus_points: 0, frozen_points: 0, total_recharged: 100000, customer_credit_status: 'ready' } });
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    return json(route, { success: true, status: 'success', data: {}, items: [], total: 0 });
  });
}

/** 把五个页签逐个打开,收集全程渲染出的可见文本。 */
async function walkAllTabs(page: Page): Promise<string> {
  let collected = await visibleText(page);
  const tabs = ['员工费用与算力上限', '审批队列', '自动计划', '操作记录'];
  for (const label of tabs) {
    const button = page.getByRole('button', { name: label, exact: true });
    await expect(button).toBeVisible();
    await button.click();
    await page.waitForTimeout(150);
    collected += '\n' + await visibleText(page);
    await button.click();
  }
  // 邀请面板里的高级权限(29 个勾选框)也要展开
  // 窄视口下邀请面板是底部抽屉,要先点固定按钮把它拉起来。
  const wizard = page.locator('[data-testid="owner-invite-wizard"]:visible');
  if (!(await wizard.count())) {
    await page.locator('button:visible').filter({ hasText: '邀请员工' }).last().click();
    await expect(wizard).toBeVisible();
  }
  await wizard.getByTestId('invite-advanced-permissions').locator('> summary').click();
  for (const group of await wizard.locator('details[data-testid^="capability-group-"] > summary').all()) {
    await group.click();
  }
  collected += '\n' + await visibleText(page);
  return collected;
}

/* ── §5-1 · 全流程零英文枚举 / 零「额度」 / 零「排查用」 ─────────────────── */

test('§5-1 老板五页签全程:渲染 DOM 零英文枚举、零「额度」、零「排查用」', async ({ page }) => {
  await installOwnerSession(page);
  await page.goto('/organization/team');
  await expect(page.getByTestId('organization-center')).toBeVisible();

  const text = await walkAllTabs(page);
  const hits = scanForbidden(text);
  expect(hits, `渲染 DOM 里出现了这些不该给用户看的串：${hits.join(', ')}`).toEqual([]);

  // 正向:关键中文标签确实渲染出来了(证明上面的「零命中」不是因为页面根本没渲染)
  expect(text).toContain('每月总上限');
  expect(text).toContain('效果监测');
  expect(text).toContain('待审批');
  expect(text).toContain('高额算力消费');
  expect(text).toContain('运行中');
  expect(text).toContain('退出处理中');   // leaving —— 旧 statusLabel 漏网的那一个
  expect(text).toContain('更新客户分配');  // 审计 action 映射
  expect(text).toContain('老板');          // actor_kind 映射
});

test('§5-1 反向对照:扫描器对人造英文必须报红(证明它不是恒真)', async ({ page }) => {
  await installOwnerSession(page);
  await page.goto('/organization/team');
  await expect(page.getByTestId('organization-center')).toBeVisible();
  await page.evaluate(() => {
    const probe = document.createElement('div');
    probe.textContent = 'monthly_total · monitor_single · pending · 额度 · 排查用';
    document.body.appendChild(probe);
  });
  const hits = scanForbidden(await visibleText(page));
  expect(hits).toEqual(expect.arrayContaining(['monthly_total', 'monitor_single', 'pending', '额度', '排查用']));
});

/* ── §5-2 · fallback 拆锁:未知枚举值渲染成「未知…（xxx）」而不是裸英文 ──── */

test('§5-2 拆锁:塞入后端从未有过的枚举值 → 渲染「未知…（原值）」而不是裸英文', async ({ page }) => {
  await installOwnerSession(page, { unknownEnums: true });
  await page.goto('/organization/team');
  await expect(page.getByTestId('organization-center')).toBeVisible();

  const text = await walkAllTabs(page);
  // 每一个人造值都必须被「未知…（值）」包住,不能单独裸奔。
  for (const injected of ['zombie_state', 'weekly_total', 'teleport_beam', 'levitating', 'wormhole.open', 'poltergeist', 'ectoplasm']) {
    const wrapped = new RegExp(`未知[^（]*（${injected.replace(/\./g, '\\.')}）`);
    expect(wrapped.test(text), `未知值 ${injected} 没有被「未知…（${injected}）」包住 —— fallback 退化成裸值了`).toBe(true);
  }
  // 反向:同一批值不允许以「裸标签」形态出现(前后没有中文包裹)
  expect(/(^|\n)\s*zombie_state\s*($|\n)/.test(text), '未知状态以裸值形态单独成行渲染了').toBe(false);
});

/* ── §5-3 · 422 成对判据 ────────────────────────────────────────────────── */

const publicInvite = {
  status: 'pending', organization_name: 'QA测试团队-勿动', role_name: '销售',
  target_kind: 'phone', account_mode: 'create_operator', account_available: true,
  expires_at: later.toISOString(),
  agreements: { user_terms_version: 'v1', privacy_version: 'v1' },
  request_id: 'local-inspect',
};

async function installInviteAccept(page: Page, verifyResponder: (route: Route) => Promise<void>) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: false }, 401);
    if (path === '/api/public/organization/invites/inspect') return json(route, publicInvite);
    if (path === '/api/public/organization/invites/verification-challenges') {
      return json(route, { challenge_id: 9, status: 'pending', expires_at: later.toISOString(), delivery_queued: true, replayed: false });
    }
    if (path.endsWith('/delivery-status')) {
      return json(route, { challenge_id: 9, challenge_status: 'pending', delivery_state: 'sent', failure_code: null, sent_at: now.toISOString(), expires_at: later.toISOString(), request_id: 'local' });
    }
    if (path.endsWith('/verify')) return await verifyResponder(route);
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    return json(route, { success: true, data: {}, items: [], total: 0 });
  });
  await page.goto('/organization/invite#token=local-invite-token-0123456789');
  await expect(page.getByTestId('organization-invite-accept')).toBeVisible();
  await page.getByRole('button', { name: '验证联系方式并创建员工账号' }).click();
  await page.getByLabel('6 位验证码').fill('123456');
  await page.getByRole('button', { name: '确认验证码' }).click();
}

test('§5-3 必须命中:验证码字段 422 → 「验证码是 6 位数字」,不是「组织服务暂不可用」', async ({ page }) => {
  // 原样复现旧后端行为:FastAPI 默认 422,detail 是**英文错误数组**。
  await installInviteAccept(page, route => json(route, {
    detail: [{ type: 'string_pattern_mismatch', loc: ['body', 'code'], msg: "String should match pattern '^[0-9]{6}$'", input: '12345' }],
  }, 422));
  const alert = page.getByRole('alert').first();
  await expect(alert).toContainText('验证码是 6 位数字');
  await expect(alert).not.toContainText('组织服务暂不可用');
});

test('§5-3 必须不命中:真 5xx 仍显示服务不可用,不能翻译成输入错误', async ({ page }) => {
  await installInviteAccept(page, route => json(route, { detail: 'Internal Server Error' }, 500));
  const alert = page.getByRole('alert').first();
  await expect(alert).toContainText('服务暂时不可用');
  await expect(alert).not.toContainText('验证码是 6 位数字');
  await expect(alert).not.toContainText('Internal Server Error');
});

/* ── §5-4 · overview 零 404 ─────────────────────────────────────────────── */

async function collect404(page: Page, hits: string[]) {
  page.on('console', (message: ConsoleMessage) => {
    const text = message.text();
    if (message.type() === 'error' && text.includes('404')) hits.push(text);
  });
  page.on('response', response => {
    if (response.status() === 404) hits.push(`${response.status()} ${new URL(response.url()).pathname}`);
  });
}

test('§5-4 有团队的账号:全程 console/network 零 404', async ({ page }) => {
  const hits: string[] = [];
  await collect404(page, hits);
  await installOwnerSession(page);
  await page.goto('/organization/team');
  await expect(page.getByTestId('organization-center')).toBeVisible();
  await walkAllTabs(page);
  expect(hits, `出现了 404：${hits.join(' | ')}`).toEqual([]);
});

test('§5-4 没有团队的账号:overview 走 200 + has_organization:false,零 404', async ({ page }) => {
  const hits: string[] = [];
  await collect404(page, hits);
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'organization-no-org-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({ version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [], dismissed_features: [], viewed_videos: [], first_seen_at: '2026-08-17T00:00:00.000Z', last_updated_at: '2026-08-17T00:00:00.000Z' }));
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: ownerUser });
    // 新契约:没有团队 = 200,不是 404
    if (path === '/api/organization/overview') return json(route, { has_organization: false });
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    return json(route, { success: true, data: {}, items: [], total: 0 });
  });
  await page.goto('/organization/team');
  await expect(page.getByLabel('团队名称')).toBeVisible();
  expect(hits, `出现了 404：${hits.join(' | ')}`).toEqual([]);
});

test('§5-4 反向对照:404 探针确实抓得到(证明上面的「零 404」不是瞎子)', async ({ page }) => {
  const hits: string[] = [];
  await collect404(page, hits);
  await installOwnerSession(page);
  await page.route('**/api/organization/overview', route => json(route, { detail: { code: 'ORG_MEMBERSHIP_REQUIRED', message: '你还没有加入任何团队' } }, 404));
  await page.goto('/organization/team');
  await expect(page.getByLabel('团队名称')).toBeVisible();
  expect(hits.length, '探针没抓到人造 404 —— 说明「零 404」那两条是恒真的假绿').toBeGreaterThan(0);
});

/* ── §1-2 / §1-3 · 邀请链接找回 + 登录名可见 ───────────────────────────── */

test('§1-2/§1-3 邀请行显示最终登录名,且能只读找回邀请链接', async ({ page }) => {
  await installOwnerSession(page);
  await page.goto('/organization/team');
  await expect(page.getByTestId('organization-center')).toBeVisible();

  // §1-3:显示 jcfemw-qaseat01,而不是「用户名邀请 #16」
  const title = page.locator('[data-testid="invite-title-16"]:visible').first();
  await expect(title).toContainText('jcfemw-qaseat01');
  await expect(title).not.toContainText('#16');

  // §1-2:点「查看链接」→ 链接面板出现且带上 token(不用重发、不作废旧链接)
  await page.locator('[data-testid="invite-reveal-16"]:visible').first().click();
  const linkInput = page.locator('[data-testid="invite-link-input"]:visible').first();
  await expect(linkInput).toBeVisible();
  await expect(linkInput).toHaveValue(/#token=local-derived-token-0123456789$/);
});
