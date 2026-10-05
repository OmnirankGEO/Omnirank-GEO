import { expect, test, type BrowserContext, type Route } from './_fixtures';

const oldUser = {
  id: 7301,
  username: 'same-user',
  display_name: '同账号旧权限',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  roles: [],
  permissions: ['brands:view', 'monitoring:view'],
  client_brand_ids: [101],
  permission_version: 1,
  agent_level: 1,
};

const revokedUser = {
  ...oldUser,
  display_name: '同账号新权限',
  permissions: [],
  client_brand_ids: [],
  permission_version: 2,
};

const differentUser = {
  ...oldUser,
  id: 7302,
  username: 'different-user',
  display_name: '另一个账号',
  client_brand_ids: [202],
  permission_version: 1,
};

const wallet = {
  paid_points: 1000,
  commission_points: 0,
  bonus_points: 0,
  frozen_points: 0,
  total_recharged: 1000,
  deduction_preference: 'default',
  customer_credit_status: 'ready',
  customer_credit: {
    tool_credit_points: 0,
    publish_credit_points: 0,
    bonus_credit_points: 0,
    total_purchased_points: 0,
    total_consumed_points: 0,
  },
  charge_notify_level: 'quiet',
};

function authorization(route: Route): string {
  return route.request().headers().authorization || '';
}

async function installCommonRoutes(context: BrowserContext, handler?: (route: Route, path: string) => Promise<boolean>) {
  await context.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (handler && await handler(route, path)) return;
    if (path === '/api/wallet') return route.fulfill({ json: { success: true, data: wallet } });
    if (path === '/api/wallet/pricing') return route.fulfill({ json: { success: true, data: [] } });
    if (path === '/api/public/whitelabel') return route.fulfill({ json: { success: true, data: null } });
    if (path === '/api/user/notifications/unread-count') return route.fulfill({ json: { status: 'success', count: 0 } });
    if (path === '/api/user/notifications') return route.fulfill({ json: { status: 'success', notifications: [] } });
    if (path === '/api/m3/customers') return route.fulfill({ json: { success: true, customers: [] } });
    if (/^\/api\/m3\/material-confirm\/status\/\d+$/.test(path)) return route.fulfill({ json: {
      success: true,
      status: 'none',
      brand_id: 101,
      brand_name: '撤权前敏感客户',
      can_generate_link: false,
      has_session: false,
      materials_summary: { filled_count: 0, total_fields: 8, fields_missing: [] },
    } });
    return route.fulfill({ json: { success: true, data: {}, items: [], records: [], total: 0 } });
  });
}

test('two real pages rotate same-user authority before the replacement token can issue business reads', async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'same-user-old-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'returning', completed_steps: [], skipped_steps: [], started_at: Date.now(), updated_at: Date.now(),
    }));
  });
  let newMeStarted = false;
  let newMeResolved = false;
  let probeCalls = 0;
  const probeAuth: string[] = [];
  await installCommonRoutes(context, async (route, path) => {
    if (path === '/api/auth/me') {
      if (authorization(route) === 'Bearer same-user-new-token') {
        newMeStarted = true;
        await new Promise(resolve => setTimeout(resolve, 500));
        newMeResolved = true;
        await route.fulfill({ json: { success: true, user: revokedUser } });
      } else {
        await route.fulfill({ json: { success: true, user: oldUser } });
      }
      return true;
    }
    if (path === '/api/client-context/list') {
      const clients = authorization(route) === 'Bearer same-user-old-token'
        ? [{ id: 101, name: '撤权前敏感客户', diagnosis_count: 1, quote_count: 1, created_at: '2026-01-01' }]
        : [];
      await route.fulfill({ json: { success: true, clients } });
      return true;
    }
    if (path === '/api/client-context/101') {
      await route.fulfill({ json: { success: true, context: { brand: { id: 101, name: '撤权前敏感客户', diagnosis_count: 1 }, profile: null, materials: null, relatedQuoteIds: [], socialProjects: [] } } });
      return true;
    }
    if (path === '/api/my-clients/101') {
      if (authorization(route) !== 'Bearer same-user-old-token') {
        await route.fulfill({ status: 403, json: { detail: 'revoked' } });
      } else {
        await route.fulfill({ json: { brand: { id: 101, name: '撤权前敏感客户', industry: '敏感行业' }, profile: null } });
      }
      return true;
    }
    if (path === '/api/cross-tab-business-probe') {
      probeCalls += 1;
      probeAuth.push(authorization(route));
      await route.fulfill({ json: { success: true } });
      return true;
    }
    return false;
  });

  const oldPage = await context.newPage();
  const otherTab = await context.newPage();
  await oldPage.goto('/my-clients/101');
  await expect(oldPage.getByText('撤权前敏感客户').first()).toBeVisible();
  await expect(oldPage.getByRole('heading', { name: '撤权前敏感客户' })).toBeVisible();
  await oldPage.evaluate(() => {
    localStorage.setItem('interview_cache', JSON.stringify({ messages: [{ content: 'A账号旧对话' }] }));
    localStorage.setItem('interview_cache:7301:stale-epoch', JSON.stringify({ messages: [{ content: 'A账号旧对话' }] }));
    localStorage.setItem('currentProjectId', 'account-a-project');
    sessionStorage.setItem('account-a-sensitive', 'A账号旧状态');
  });
  await otherTab.goto('/login');

  // This is a real cross-tab localStorage write: the storage event is delivered to oldPage.
  await otherTab.evaluate(() => localStorage.setItem('omnirank_token', 'same-user-new-token'));
  await expect.poll(() => newMeStarted).toBe(true);
  expect(newMeResolved).toBe(false);
  await expect(oldPage.getByText('撤权前敏感客户')).toHaveCount(0);
  await expect.poll(() => oldPage.evaluate(() => ({
    interviewKeys: Object.keys(localStorage).filter(key => key.startsWith('interview_cache')).length,
    project: localStorage.getItem('currentProjectId'),
    session: sessionStorage.getItem('account-a-sensitive'),
  }))).toEqual({ interviewKeys: 0, project: null, session: null });

  const pendingOutcome = await oldPage.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string) => Promise<unknown> } }>;
    try {
      await (await importer()).default.get('/api/cross-tab-business-probe');
      return 'unexpected-success';
    } catch (error) {
      return (error as { code?: string; name?: string }).code || (error as Error).name;
    }
  });
  expect(pendingOutcome).toBe('AUTHORITATIVE_SESSION_PENDING');
  expect(probeCalls).toBe(0);

  await expect.poll(() => newMeResolved).toBe(true);
  const confirmedStatus = await oldPage.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string) => Promise<{ status: number }> } }>;
    return (await (await importer()).default.get('/api/cross-tab-business-probe')).status;
  });
  expect(confirmedStatus).toBe(200);
  expect(probeCalls).toBe(1);
  expect(probeAuth).toEqual(['Bearer same-user-new-token']);
  await expect(oldPage.getByText('撤权前敏感客户')).toHaveCount(0);
  await context.close();
});

test('api GET permission refresh replays with a fresh signal after scope abort', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'permission-old-token'));
  let probeCalls = 0;
  let refreshedMeResolved = false;
  const order: string[] = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      const next = authorization(route) === 'Bearer permission-new-token';
      order.push(next ? 'me:new:start' : 'me:old');
      if (next) {
        await new Promise(resolve => setTimeout(resolve, 120));
        refreshedMeResolved = true;
        order.push('me:new:done');
      }
      return route.fulfill({ json: { success: true, user: next ? revokedUser : oldUser } });
    }
    if (path === '/api/auth/refresh') {
      order.push('refresh');
      return route.fulfill({ json: { success: true, token: 'permission-new-token' } });
    }
    if (path === '/api/permission-replay-probe') {
      probeCalls += 1;
      order.push(`probe:${probeCalls}:${authorization(route)}`);
      if (probeCalls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      expect(refreshedMeResolved).toBe(true);
      return route.fulfill({ json: { success: true, version: 2 } });
    }
    if (path === '/api/wallet') return route.fulfill({ json: { success: true, data: wallet } });
    return route.fulfill({ json: { success: true, data: {}, items: [] } });
  });

  await page.goto('/login');
  await expect.poll(() => order.includes('me:old')).toBe(true);
  const result = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string) => Promise<{ status: number; data: { version: number } }> } }>;
    const response = await (await importer()).default.get('/api/permission-replay-probe');
    return { status: response.status, version: response.data.version };
  });

  expect(result).toEqual({ status: 200, version: 2 });
  expect(probeCalls).toBe(2);
  expect(order).toEqual([
    'me:old',
    'probe:1:Bearer permission-old-token',
    'refresh',
    'me:new:start',
    'me:new:done',
    'probe:2:Bearer permission-new-token',
  ]);
});

test('authFetch GET permission refresh uses a fresh transport while an active caller remains subscribed', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'fetch-old-token'));
  let probeCalls = 0;
  const order: string[] = [];
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      const refreshed = authorization(route) === 'Bearer fetch-new-token';
      order.push(refreshed ? 'me:new' : 'me:old');
      return route.fulfill({ json: { success: true, user: refreshed ? revokedUser : oldUser } });
    }
    if (path === '/api/auth/refresh') {
      order.push('refresh');
      return route.fulfill({ json: { success: true, token: 'fetch-new-token' } });
    }
    if (path === '/api/fetch-permission-replay-probe') {
      probeCalls += 1;
      order.push(`probe:${probeCalls}:${authorization(route)}`);
      if (probeCalls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return route.fulfill({ json: { success: true, version: 2 } });
    }
    return route.fulfill({ json: { success: true, data: {} } });
  });

  await page.goto('/login');
  await expect.poll(() => order.includes('me:old')).toBe(true);
  const result = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      authFetch: (url: string, init?: RequestInit) => Promise<Response>;
    }>;
    const { authFetch } = await importer();
    const response = await authFetch('/api/fetch-permission-replay-probe');
    return { status: response.status, body: await response.json() };
  });

  expect(result).toEqual({ status: 200, body: { success: true, version: 2 } });
  expect(probeCalls).toBe(2);
  expect(order).toEqual([
    'me:old',
    'probe:1:Bearer fetch-old-token',
    'refresh',
    'me:new',
    'probe:2:Bearer fetch-new-token',
  ]);
});

test('a delayed 403 from session A is never replayed or resolved with session B data', async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => localStorage.setItem('omnirank_token', 'session-a-token'));
  let release403!: () => void;
  const waitForRelease = new Promise<void>(resolve => { release403 = resolve; });
  let probeCalls = 0;
  let refreshCalls = 0;
  let sessionBConfirmed = false;
  await installCommonRoutes(context, async (route, path) => {
    if (path === '/api/auth/me') {
      const isB = authorization(route) === 'Bearer session-b-token';
      if (isB) sessionBConfirmed = true;
      await route.fulfill({ json: { success: true, user: isB ? differentUser : oldUser } });
      return true;
    }
    if (path === '/api/auth/refresh') {
      refreshCalls += 1;
      await route.fulfill({ json: { success: true, token: 'must-not-be-used' } });
      return true;
    }
    if (path === '/api/cross-session-403-probe') {
      probeCalls += 1;
      if (probeCalls === 1) {
        await waitForRelease;
        await route.fulfill({ status: 403, json: { detail: 'BRAND_ACCESS: 无权访问 brand_id=101' } });
      } else {
        await route.fulfill({ json: { success: true, owner: authorization(route) } });
      }
      return true;
    }
    return false;
  });

  const oldPage = await context.newPage();
  const switcher = await context.newPage();
  await oldPage.goto('/login');
  await expect(oldPage.getByRole('button', { name: '登 录' })).toBeVisible();
  const pending = oldPage.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string) => Promise<unknown> } }>;
    try {
      const response = await (await importer()).default.get('/api/cross-session-403-probe') as { data?: unknown };
      return { resolved: true, data: response.data };
    } catch (error) {
      const candidate = error as { response?: { status?: number; data?: unknown }; code?: string };
      return { resolved: false, status: candidate.response?.status, data: candidate.response?.data, code: candidate.code };
    }
  });
  await expect.poll(() => probeCalls).toBe(1);

  await switcher.goto('/login');
  await switcher.evaluate(() => localStorage.setItem('omnirank_token', 'session-b-token'));
  await expect.poll(() => sessionBConfirmed).toBe(true);
  release403();

  const outcome = await pending;
  expect(outcome.resolved).toBe(false);
  expect(outcome.status).not.toBe(200);
  expect(JSON.stringify(outcome.data || '')).not.toContain('session-b-token');
  expect(probeCalls).toBe(1);
  expect(refreshCalls).toBe(0);
  await context.close();
});

test('global axios never refreshes or replays a delayed session A 401 under session B', async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => localStorage.setItem('omnirank_token', 'global-session-a'));
  let release401!: () => void;
  const responseGate = new Promise<void>(resolve => { release401 = resolve; });
  let probeCalls = 0;
  let refreshCalls = 0;
  let sessionBConfirmed = false;
  await installCommonRoutes(context, async (route, path) => {
    if (path === '/api/auth/me') {
      const isB = authorization(route) === 'Bearer global-session-b';
      if (isB) sessionBConfirmed = true;
      await route.fulfill({ json: { success: true, user: isB ? differentUser : oldUser } });
      return true;
    }
    if (path === '/api/auth/refresh') {
      refreshCalls += 1;
      await route.fulfill({ json: { success: true, token: 'global-refresh-must-not-run' } });
      return true;
    }
    if (path === '/api/global-cross-session-probe') {
      probeCalls += 1;
      await responseGate;
      await route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return true;
    }
    return false;
  });

  const oldPage = await context.newPage();
  const switcher = await context.newPage();
  await oldPage.goto('/login');
  const pending = oldPage.evaluate(async () => {
    const importer = new Function('return import("/@id/axios")') as () => Promise<{ default: any }>;
    const axios = (await importer()).default;
    try {
      const response = await axios.get('/api/global-cross-session-probe');
      return { resolved: true, status: response.status };
    } catch (error) {
      return { resolved: false, status: (error as { response?: { status?: number } }).response?.status };
    }
  });
  await expect.poll(() => probeCalls).toBe(1);

  await switcher.goto('/login');
  await switcher.evaluate(() => localStorage.setItem('omnirank_token', 'global-session-b'));
  await expect.poll(() => sessionBConfirmed).toBe(true);
  release401();

  expect(await pending).toEqual({ resolved: false, status: 401 });
  expect(probeCalls).toBe(1);
  expect(refreshCalls).toBe(0);
  await context.close();
});

test('axios permission replay preserves caller cancellation while replacing only its internal scope signal', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'axios-cancel-old'));
  let probeCalls = 0;
  let refreshStarted = false;
  let oldMeConfirmed = false;
  let releaseNewMe!: () => void;
  const newMeGate = new Promise<void>(resolve => { releaseNewMe = resolve; });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      const isNew = authorization(route) === 'Bearer axios-cancel-new';
      if (isNew) await newMeGate;
      else oldMeConfirmed = true;
      return route.fulfill({ json: { success: true, user: isNew ? revokedUser : oldUser } });
    }
    if (path === '/api/auth/refresh') {
      refreshStarted = true;
      return route.fulfill({ json: { success: true, token: 'axios-cancel-new' } });
    }
    if (path === '/api/axios-cancel-probe') {
      probeCalls += 1;
      if (probeCalls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return route.fulfill({ json: { success: true, leaked: true } });
    }
    return route.fulfill({ json: { success: true, data: {} } });
  });

  await page.goto('/login');
  await expect.poll(() => oldMeConfirmed).toBe(true);
  const pending = page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string, config: { signal: AbortSignal }) => Promise<unknown> } }>;
    const controller = new AbortController();
    (window as any).__cancelAuthorityProbe = () => controller.abort();
    try {
      await (await importer()).default.get('/api/axios-cancel-probe', { signal: controller.signal });
      return 'unexpected-success';
    } catch (error) {
      return (error as { code?: string; name?: string }).code || (error as Error).name;
    }
  });
  await expect.poll(() => refreshStarted).toBe(true);
  await page.evaluate(() => (window as any).__cancelAuthorityProbe());
  releaseNewMe();
  expect(await pending).toMatch(/ERR_CANCELED|Canceled|Abort/);
  expect(probeCalls).toBe(1);
});

test('authFetch permission replay stops when its caller cancels during authoritative refresh', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'fetch-cancel-old'));
  let probeCalls = 0;
  let refreshStarted = false;
  let oldMeConfirmed = false;
  let releaseNewMe!: () => void;
  const newMeGate = new Promise<void>(resolve => { releaseNewMe = resolve; });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      const isNew = authorization(route) === 'Bearer fetch-cancel-new';
      if (isNew) await newMeGate;
      else oldMeConfirmed = true;
      return route.fulfill({ json: { success: true, user: isNew ? revokedUser : oldUser } });
    }
    if (path === '/api/auth/refresh') {
      refreshStarted = true;
      return route.fulfill({ json: { success: true, token: 'fetch-cancel-new' } });
    }
    if (path === '/api/fetch-cancel-probe') {
      probeCalls += 1;
      if (probeCalls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return route.fulfill({ json: { success: true, leaked: true } });
    }
    return route.fulfill({ json: { success: true, data: {} } });
  });

  await page.goto('/login');
  await expect.poll(() => oldMeConfirmed).toBe(true);
  const pending = page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ authFetch: (url: string, init: RequestInit) => Promise<Response> }>;
    const controller = new AbortController();
    (window as any).__cancelFetchAuthorityProbe = () => controller.abort();
    try {
      await (await importer()).authFetch('/api/fetch-cancel-probe', { signal: controller.signal });
      return 'unexpected-success';
    } catch (error) {
      return (error as Error).name || String((error as { code?: unknown }).code || 'canceled');
    }
  });
  await expect.poll(() => refreshStarted).toBe(true);
  await page.evaluate(() => (window as any).__cancelFetchAuthorityProbe());
  releaseNewMe();
  expect(await pending).toMatch(/ERR_CANCELED|Canceled|Abort/);
  expect(probeCalls).toBe(1);
});


for (const staleStatus of [401, 404, 500]) {
  test(`stale refresh and hard-invalid responses cannot clear a superseding session (${staleStatus})`, async ({ browser }) => {
    const context = await browser.newContext();
    await context.addInitScript(() => localStorage.setItem('omnirank_token', 'cas-session-a'));
    let refreshStarted = false;
    let sessionAConfirmed = false;
    let sessionBConfirmed = false;
    await installCommonRoutes(context, async (route, path) => {
      if (path === '/api/auth/me') {
        const isB = authorization(route) === 'Bearer cas-session-b';
        if (isB) sessionBConfirmed = true;
        else sessionAConfirmed = true;
        await route.fulfill({ json: { success: true, user: isB ? differentUser : oldUser } });
        return true;
      }
      if (path === '/api/auth/refresh') {
        refreshStarted = true;
        await new Promise(resolve => setTimeout(resolve, 750));
        await route.fulfill({ status: staleStatus, json: staleStatus >= 500
          ? { detail: 'temporary auth failure' }
          : { code: 'INVALID_TOKEN' } });
        return true;
      }
      if (path === '/api/cas-refresh-probe') {
        await route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
        return true;
      }
      return false;
    });

    const oldPage = await context.newPage();
    const switcher = await context.newPage();
    await oldPage.goto('/login');
    await expect.poll(() => sessionAConfirmed).toBe(true);
    const pending = oldPage.evaluate(async () => {
      const request = (async () => {
        const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string) => Promise<unknown> } }>;
        try {
          await (await importer()).default.get('/api/cas-refresh-probe');
          return 'unexpected-success';
        } catch (error) {
          return (error as { response?: { status?: number } }).response?.status || 'rejected';
        }
      })();
      return Promise.race([
        request,
        new Promise<string>(resolve => setTimeout(() => resolve('probe-timeout'), 5000)),
      ]);
    });
    await expect.poll(() => refreshStarted).toBe(true);

    await switcher.goto('/login');
    await switcher.evaluate(() => localStorage.setItem('omnirank_token', 'cas-session-b'));
    await expect.poll(() => sessionBConfirmed).toBe(true);
    expect(await pending).toBe(401);

    expect(await oldPage.evaluate(() => localStorage.getItem('omnirank_token'))).toBe('cas-session-b');
    expect(await switcher.evaluate(() => localStorage.getItem('omnirank_token'))).toBe('cas-session-b');
    await context.close();
  });
}

test('a delayed hard-invalid global axios response from session A cannot logout session B', async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => localStorage.setItem('omnirank_token', 'hard-session-a'));
  let probeStarted = false;
  let releaseProbe!: () => void;
  const probeGate = new Promise<void>(resolve => { releaseProbe = resolve; });
  let sessionAConfirmed = false;
  let sessionBConfirmed = false;
  await installCommonRoutes(context, async (route, path) => {
    if (path === '/api/auth/me') {
      const isB = authorization(route) === 'Bearer hard-session-b';
      if (isB) sessionBConfirmed = true;
      else sessionAConfirmed = true;
      await route.fulfill({ json: { success: true, user: isB ? differentUser : oldUser } });
      return true;
    }
    if (path === '/api/hard-invalid-probe') {
      probeStarted = true;
      await probeGate;
      await route.fulfill({ status: 401, json: { code: 'INVALID_TOKEN' } });
      return true;
    }
    return false;
  });

  const oldPage = await context.newPage();
  const switcher = await context.newPage();
  await oldPage.goto('/login');
  await expect.poll(() => sessionAConfirmed).toBe(true);
  const pending = oldPage.evaluate(async () => {
    const importer = new Function('return import("/@id/axios")') as () => Promise<{ default: any }>;
    try {
      await (await importer()).default.get('/api/hard-invalid-probe');
      return 'unexpected-success';
    } catch (error) {
      return (error as { response?: { status?: number } }).response?.status || 'rejected';
    }
  });
  await expect.poll(() => probeStarted).toBe(true);
  await switcher.goto('/login');
  await switcher.evaluate(() => localStorage.setItem('omnirank_token', 'hard-session-b'));
  await expect.poll(() => sessionBConfirmed).toBe(true);
  releaseProbe();
  expect(await pending).toBe(401);
  expect(await oldPage.evaluate(() => localStorage.getItem('omnirank_token'))).toBe('hard-session-b');
  await context.close();
});

test('same-document hard expiry on a public route clears AuthContext memory as well as storage', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'public-expiry-token');
    localStorage.setItem('interview_cache', JSON.stringify({ messages: [{ content: '公开页旧账号消息' }] }));
    localStorage.setItem('interview_cache:7301:old', JSON.stringify({ messages: [{ content: '公开页旧账号消息' }] }));
    localStorage.setItem('currentProjectId', 'old-public-project');
    sessionStorage.setItem('old-public-session', 'sensitive');
  });
  let probeStarted = false;
  let releaseProbe!: () => void;
  const probeGate = new Promise<void>(resolve => { releaseProbe = resolve; });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return route.fulfill({ json: { success: true, user: oldUser } });
    if (path === '/api/public-expiry-private-probe') {
      probeStarted = true;
      await probeGate;
      return route.fulfill({ status: 401, json: { code: 'INVALID_TOKEN' } });
    }
    return route.fulfill({ json: { success: true, data: {}, items: [], records: [], total: 0 } });
  });

  await page.goto('/');
  const pending = page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ authFetch: (url: string) => Promise<Response> }>;
    return (await (await importer()).authFetch('/api/public-expiry-private-probe')).status;
  });
  await expect.poll(() => probeStarted).toBe(true);
  await page.evaluate(() => {
    history.pushState({}, '', '/login');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page).toHaveURL(/\/login$/);
  releaseProbe();
  expect(await pending).toBe(401);
  expect(await page.evaluate(() => localStorage.getItem('omnirank_token'))).toBeNull();
  expect(await page.evaluate(() => ({
    interviewKeys: Object.keys(localStorage).filter(key => key.startsWith('interview_cache')).length,
    project: localStorage.getItem('currentProjectId'),
    session: sessionStorage.getItem('old-public-session'),
  }))).toEqual({ interviewKeys: 0, project: null, session: null });
  await page.evaluate(() => {
    history.pushState({}, '', '/');
    window.dispatchEvent(new PopStateEvent('popstate'));
  });
  await expect(page).toHaveURL(/\/login$/);
});

test('v35 managed transport survives PERMISSION_CHANGED rotation and replays with the confirmed token', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'v35-replay-old'));
  let calls = 0;
  let oldMeConfirmed = false;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      const isNew = authorization(route) === 'Bearer v35-replay-new';
      if (!isNew) oldMeConfirmed = true;
      return route.fulfill({ json: { success: true, user: isNew ? revokedUser : oldUser } });
    }
    if (path === '/api/auth/refresh') return route.fulfill({ json: { success: true, token: 'v35-replay-new' } });
    if (path === '/api/agent/inventory/balance') {
      calls += 1;
      if (calls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return route.fulfill({ json: {
        paid_inventory_points: 77, bonus_inventory_points: 0, frozen_inventory_points: 0,
        total_purchased_points: 77, total_allocated_points: 0, alert_level: 'healthy',
      } });
    }
    return route.fulfill({ json: { success: true, data: {} } });
  });
  await page.goto('/login');
  await expect.poll(() => oldMeConfirmed).toBe(true);
  const result = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/v35w2Api.ts")') as () => Promise<{ agentApi: { inventoryBalance: () => Promise<{ paid_inventory_points: number }> } }>;
    return (await importer()).agentApi.inventoryBalance();
  });
  expect(result.paid_inventory_points).toBe(77);
  expect(calls).toBe(2);
});

test('v35 replay retains the real caller abort while replacing its managed authority signal', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'v35-cancel-old'));
  let calls = 0;
  let refreshStarted = false;
  let oldMeConfirmed = false;
  let releaseNewMe!: () => void;
  const newMeGate = new Promise<void>(resolve => { releaseNewMe = resolve; });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      const isNew = authorization(route) === 'Bearer v35-cancel-new';
      if (isNew) await newMeGate;
      else oldMeConfirmed = true;
      return route.fulfill({ json: { success: true, user: isNew ? revokedUser : oldUser } });
    }
    if (path === '/api/auth/refresh') {
      refreshStarted = true;
      return route.fulfill({ json: { success: true, token: 'v35-cancel-new' } });
    }
    if (path === '/api/agent/inventory/balance') {
      calls += 1;
      if (calls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return route.fulfill({ json: { paid_inventory_points: 99 } });
    }
    return route.fulfill({ json: { success: true, data: {} } });
  });
  await page.goto('/login');
  await expect.poll(() => oldMeConfirmed).toBe(true);
  const pending = page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/v35w2Api.ts")') as () => Promise<{ agentApi: { inventoryBalance: (signal: AbortSignal) => Promise<unknown> } }>;
    const controller = new AbortController();
    (window as any).__cancelV35Replay = () => controller.abort();
    try {
      await (await importer()).agentApi.inventoryBalance(controller.signal);
      return 'unexpected-success';
    } catch (error) {
      return (error as Error).name || String((error as { code?: unknown }).code || 'canceled');
    }
  });
  await expect.poll(() => refreshStarted).toBe(true);
  await page.evaluate(() => (window as any).__cancelV35Replay());
  releaseNewMe();
  expect(await pending).toMatch(/Abort|Cancel/i);
  expect(calls).toBe(1);
});

test('same textual JWT cannot let an E1 refresh cross logout into confirmed E2', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'same-jwt-token'));
  let meCalls = 0;
  let refreshStarted = false;
  let releaseRefresh!: () => void;
  const refreshGate = new Promise<void>(resolve => { releaseRefresh = resolve; });
  let probeCalls = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      meCalls += 1;
      return route.fulfill({ json: { success: true, user: oldUser } });
    }
    if (path === '/api/auth/refresh') {
      refreshStarted = true;
      await refreshGate;
      return route.fulfill({ json: { success: true, token: 'same-jwt-token' } });
    }
    if (path === '/api/same-jwt-epoch-probe') {
      probeCalls += 1;
      if (probeCalls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      return route.fulfill({ json: { leaked: 'E1 replay crossed into E2' } });
    }
    return route.fulfill({ json: { success: true, data: {} } });
  });

  await page.goto('/login');
  await expect.poll(() => meCalls).toBeGreaterThanOrEqual(1);
  const pending = page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string) => Promise<{ data: unknown }> } }>;
    try {
      return { resolved: true, value: (await (await importer()).default.get('/api/same-jwt-epoch-probe')).data };
    } catch (error) {
      return { resolved: false, value: (error as { code?: string; name?: string }).code || (error as Error).name };
    }
  });
  await expect.poll(() => refreshStarted).toBe(true);

  const e2 = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<any>;
    const authority = await importer();
    const e1 = authority.getAuthorizationEpoch();
    if (!authority.clearAuthoritativeSessionToken('same-jwt-token', e1)) throw new Error('E1 CAS clear failed');
    const epoch = authority.stageAuthoritativeSessionToken('same-jwt-token', 'login');
    const detail: { token: string; epoch: string; authoritativeReady?: Promise<void> } = {
      token: 'same-jwt-token',
      epoch,
    };
    window.dispatchEvent(new CustomEvent('token-refreshed', { detail }));
    if (!detail.authoritativeReady) throw new Error('AuthContext did not accept staged E2');
    await detail.authoritativeReady;
    return epoch;
  });
  await expect.poll(() => meCalls).toBeGreaterThanOrEqual(2);
  releaseRefresh();
  const result = await pending;

  expect(result.resolved).toBe(false);
  expect(probeCalls).toBe(1);
  expect(await page.evaluate(() => localStorage.getItem('omnirank_token'))).toBe('same-jwt-token');
  expect(await page.evaluate(async () => {
    const authority = await (new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<any>)();
    return authority.getAuthorizationEpoch();
  })).toBe(e2);
});

test('an A-prime replay in flight is aborted before session B can consume its response', async ({ browser }) => {
  const context = await browser.newContext();
  await context.addInitScript(() => localStorage.setItem('omnirank_token', 'replay-owner-a'));
  let replayStarted = false;
  let releaseReplay!: () => void;
  const replayGate = new Promise<void>(resolve => { releaseReplay = resolve; });
  let sessionBConfirmed = false;
  let probeCalls = 0;
  await installCommonRoutes(context, async (route, path) => {
    if (path === '/api/auth/me') {
      const token = authorization(route);
      const isB = token === 'Bearer replay-owner-b';
      if (isB) sessionBConfirmed = true;
      await route.fulfill({ json: { success: true, user: isB ? differentUser : oldUser } });
      return true;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'replay-owner-a-prime' } });
      return true;
    }
    if (path === '/api/replay-switch-probe') {
      probeCalls += 1;
      if (probeCalls === 1) {
        await route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      } else {
        replayStarted = true;
        await replayGate;
        await route.fulfill({ json: { leaked: 'A-prime sensitive payload' } }).catch(() => {});
      }
      return true;
    }
    return false;
  });

  const oldPage = await context.newPage();
  const switcher = await context.newPage();
  await oldPage.goto('/login');
  const pending = oldPage.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string) => Promise<{ data: unknown }> } }>;
    try {
      return { resolved: true, value: (await (await importer()).default.get('/api/replay-switch-probe')).data };
    } catch (error) {
      return { resolved: false, value: (error as { code?: string; name?: string }).code || (error as Error).name };
    }
  });
  await expect.poll(() => replayStarted).toBe(true);

  await switcher.goto('/login');
  await switcher.evaluate(() => localStorage.setItem('omnirank_token', 'replay-owner-b'));
  await expect.poll(() => sessionBConfirmed).toBe(true);
  releaseReplay();
  const result = await pending;

  expect(result.resolved).toBe(false);
  expect(JSON.stringify(result.value)).not.toContain('A-prime sensitive payload');
  expect(probeCalls).toBe(2);
  expect(await oldPage.evaluate(() => localStorage.getItem('omnirank_token'))).toBe('replay-owner-b');
  await context.close();
});

test('authority replay respects the original remaining axios deadline', async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'deadline-owner-a'));
  let oldMeConfirmed = false;
  let replayStarted = false;
  let probeCalls = 0;
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      if (authorization(route) === 'Bearer deadline-owner-a') oldMeConfirmed = true;
      return route.fulfill({ json: { success: true, user: oldUser } });
    }
    if (path === '/api/auth/refresh') {
      return route.fulfill({ json: { success: true, token: 'deadline-owner-a-prime' } });
    }
    if (path === '/api/replay-deadline-probe') {
      probeCalls += 1;
      if (probeCalls === 1) return route.fulfill({ status: 401, json: { code: 'PERMISSION_CHANGED' } });
      replayStarted = true;
      await new Promise(resolve => setTimeout(resolve, 400));
      await route.fulfill({ json: { leaked: 'late payload' } }).catch(() => {});
      return;
    }
    return route.fulfill({ json: { success: true, data: {} } });
  });

  await page.goto('/login');
  await expect.poll(() => oldMeConfirmed).toBe(true);
  const result = await page.evaluate(async () => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: { get: (url: string, config: { timeout: number }) => Promise<unknown> } }>;
    const startedAt = performance.now();
    try {
      await (await importer()).default.get('/api/replay-deadline-probe', { timeout: 150 });
      return { resolved: true, elapsed: performance.now() - startedAt, outcome: 'unexpected-success' };
    } catch (error) {
      return {
        resolved: false,
        elapsed: performance.now() - startedAt,
        outcome: (error as { code?: string; name?: string }).code || (error as Error).name,
      };
    }
  });
  await expect.poll(() => replayStarted).toBe(true);
  await page.waitForTimeout(450);

  expect(result.resolved).toBe(false);
  expect(result.elapsed).toBeLessThan(1000);
  expect(result.outcome).toMatch(/Cancel|Abort|ECONNABORTED/i);
  expect(probeCalls).toBe(2);
});
