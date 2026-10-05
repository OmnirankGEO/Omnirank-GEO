import { randomUUID } from 'node:crypto';
import { expect, test, type BrowserContext, type Page } from 'playwright/test';

type Role = 'customer' | 'agent';
type CatalogMode = 'ok' | 'slow' | '500' | 'offline' | '429-once';

type Controls = {
  role: Role;
  catalogMode: CatalogMode;
  catalogCalls: number;
  catalogInFlight: number;
  maxCatalogInFlight: number;
  retryAfterAt: number[];
  authDelayMs: number;
  auth401: boolean;
  permissionVersion: number;
  brandName: string;
  clientsEnabled: boolean;
  contextDelayMs: number;
  brandsMapDelayMs: number;
  testBrandIds: number[];
  permissionChangedPath: string;
  permissionChangedRemaining: number;
  unknownRequests: Array<{ method: string; path: string }>;
  requestLog: Array<{ method: string; path: string; at: number }>;
};

function baseURLFor(testInfo: { project: { use: { baseURL?: string } } }): string {
  const baseURL = String(testInfo.project.use.baseURL || '');
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(baseURL)) throw new Error(`invalid exclusive baseURL: ${baseURL}`);
  return baseURL;
}

async function patchControls(context: BrowserContext, baseURL: string, patch: Partial<Controls> & { resetCounters?: boolean }) {
  const response = await context.request.post(`${baseURL}/__perf/control`, { data: patch });
  expect(response.status()).toBe(200);
  return response.json() as Promise<Controls>;
}

async function readControls(context: BrowserContext, baseURL: string) {
  const response = await context.request.get(`${baseURL}/__perf/control`);
  expect(response.status()).toBe(200);
  return response.json() as Promise<Controls>;
}

function initialControls(role: Role): Partial<Controls> {
  return {
    role,
    catalogMode: 'ok',
    authDelayMs: 0,
    auth401: false,
    permissionVersion: 1,
    brandName: '性能客户',
    clientsEnabled: true,
    contextDelayMs: 10,
    brandsMapDelayMs: 10,
    testBrandIds: [],
    permissionChangedPath: '',
    permissionChangedRemaining: 0,
  };
}

async function installSyntheticSession(
  context: BrowserContext,
  baseURL: string,
  role: Role,
  clampIntervals = false,
) {
  const sessionId = randomUUID();
  await context.addCookies([{ name: 'omnirank_perf_session', value: sessionId, url: baseURL }]);
  await context.addInitScript(({ initialRole, owner, clamp }) => {
    const initKey = '__omnirank_failure_initialized';
    if (localStorage.getItem(initKey) !== owner) {
      localStorage.clear();
      sessionStorage.clear();
      localStorage.setItem(initKey, owner);
      localStorage.setItem('omnirank_token', `synthetic-failure-${initialRole}`);
      localStorage.setItem('omnirank_onboarding_done', 'true');
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
    }
    (window as any).__oldAccountDataSeen = false;
    if (clamp) {
      const nativeSetInterval = window.setInterval.bind(window);
      window.setInterval = ((handler: TimerHandler, timeout?: number, ...args: unknown[]) =>
        nativeSetInterval(handler, Math.min(timeout || 0, 50), ...args)) as typeof window.setInterval;
      let hidden = false;
      Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => hidden ? 'hidden' : 'visible' });
      Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
      (window as any).__setPerfHidden = (value: boolean) => {
        hidden = value;
        document.dispatchEvent(new Event('visibilitychange'));
      };
    }
    const observeAccountData = () => {
      if (!document.documentElement) return;
      new MutationObserver(() => {
        if (document.body?.innerText.includes('性能测试算力包')) (window as any).__oldAccountDataSeen = true;
      }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    };
    if (document.documentElement) observeAccountData();
    else document.addEventListener('DOMContentLoaded', observeAccountData, { once: true });
  }, { initialRole: role, owner: sessionId, clamp: clampIntervals });
  await patchControls(context, baseURL, { ...initialControls(role), resetCounters: true });
}

test.afterEach(async ({ context }, testInfo) => {
  const baseURL = baseURLFor(testInfo);
  const state = await readControls(context, baseURL);
  expect(state.unknownRequests, 'failure suite must consume every method+anchored-path 599').toEqual([]);
});

test('slow, 500, offline and 429 preserve the last catalog without overlap', async ({ context, page }, testInfo) => {
  const baseURL = baseURLFor(testInfo);
  await installSyntheticSession(context, baseURL, 'customer');

  const wrongMethod = await context.request.post(`${baseURL}/api/wallet`);
  expect(wrongMethod.status(), 'known path with wrong method must fail closed').toBe(599);
  expect((await readControls(context, baseURL)).unknownRequests).toEqual([{ method: 'POST', path: '/api/wallet' }]);
  await patchControls(context, baseURL, { resetCounters: true });

  await page.goto('/customer/recharge');
  const pack = page.getByText('标准算力包', { exact: true });
  const refresh = page.getByRole('button', { name: '刷新', exact: true });
  await expect(pack).toBeVisible();

  await patchControls(context, baseURL, { catalogMode: 'slow' });
  await refresh.evaluate((element: HTMLElement) => { element.click(); element.click(); });
  await expect(pack).toBeVisible();
  await expect.poll(async () => (await readControls(context, baseURL)).catalogInFlight).toBe(0);
  expect((await readControls(context, baseURL)).maxCatalogInFlight).toBe(1);

  await page.waitForTimeout(2100);
  await patchControls(context, baseURL, { catalogMode: '500' });
  await refresh.click();
  await expect(page.getByText(/保留上次成功结果/)).toBeVisible();
  await expect(pack).toBeVisible();

  await patchControls(context, baseURL, { catalogMode: 'offline' });
  await refresh.click();
  await expect(page.getByText(/网络.*保留上次成功结果/)).toBeVisible();
  await expect(pack).toBeVisible();

  await patchControls(context, baseURL, { catalogMode: '429-once' });
  const before429 = (await readControls(context, baseURL)).catalogCalls;
  const startedAt = Date.now();
  await refresh.click();
  await expect.poll(async () => (await readControls(context, baseURL)).catalogCalls).toBe(before429 + 2);
  const finalState = await readControls(context, baseURL);
  expect(finalState.retryAfterAt.at(-1)! - finalState.retryAfterAt.at(-2)!).toBeGreaterThanOrEqual(900);
  expect(Date.now() - startedAt).toBeGreaterThanOrEqual(900);
  await expect(pack).toBeVisible();
});

test('account switch and 401 never expose the previous account data', async ({ context, page }, testInfo) => {
  const baseURL = baseURLFor(testInfo);
  await installSyntheticSession(context, baseURL, 'agent');
  await page.goto('/agent/pricing');
  await expect(page.getByText('性能测试算力包', { exact: true })).toBeVisible();

  await patchControls(context, baseURL, { role: 'customer', authDelayMs: 800 });
  await page.evaluate(() => localStorage.setItem('omnirank_token', 'synthetic-failure-customer'));
  await page.goto('/customer/recharge', { waitUntil: 'domcontentloaded' });
  await expect(page.getByText('性能测试算力包', { exact: true })).toHaveCount(0);
  await expect(page.getByText('标准算力包', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => (window as any).__oldAccountDataSeen)).toBe(false);

  await patchControls(context, baseURL, { authDelayMs: 0, auth401: true });
  await page.evaluate(() => localStorage.setItem('omnirank_token', 'synthetic-expired'));
  await page.goto('/agent/pricing');
  await expect(page).toHaveURL(/\/login/);
  await expect(page.getByText('性能测试算力包', { exact: true })).toHaveCount(0);
});

test('same-user production refresh path clears revoked customer data and late writes', async ({ context, page }, testInfo) => {
  const baseURL = baseURLFor(testInfo);
  await installSyntheticSession(context, baseURL, 'agent');
  await patchControls(context, baseURL, {
    brandName: '撤权前客户',
    contextDelayMs: 1200,
    brandsMapDelayMs: 1200,
    testBrandIds: [101],
  });
  await page.goto('/agent/pricing', { waitUntil: 'domcontentloaded' });
  if ((page.viewportSize()?.width || 0) < 768) await page.getByRole('button', { name: '打开或收起主菜单' }).click();
  await expect(page.getByText('撤权前客户', { exact: true }).first()).toBeVisible();
  if ((page.viewportSize()?.width || 0) < 768) await page.getByRole('button', { name: '打开或收起主菜单' }).evaluate((element: HTMLElement) => element.click());

  await patchControls(context, baseURL, {
    permissionVersion: 2,
    clientsEnabled: false,
    contextDelayMs: 10,
    brandsMapDelayMs: 10,
    testBrandIds: [],
    permissionChangedPath: '/api/agent/pricing/skus',
    permissionChangedRemaining: 1,
  });
  await page.getByRole('button', { name: '刷新', exact: true }).click();
  await expect.poll(async () => (await readControls(context, baseURL)).requestLog
    .filter(item => item.method === 'POST' && item.path === '/api/auth/refresh').length).toBe(1);
  await expect(page.getByText('撤权前客户', { exact: true })).toHaveCount(0);
  await page.waitForTimeout(1400);
  await expect(page.getByText('撤权前客户', { exact: true })).toHaveCount(0);
  const leakedBrandIds = await page.evaluate(() => Object.keys(sessionStorage)
    .filter(key => key.startsWith('omnirank_brands_test_map_v2:'))
    .flatMap(key => {
      try { return JSON.parse(sessionStorage.getItem(key) || '{}').ids || []; }
      catch { return []; }
    }));
  expect(leakedBrandIds).not.toContain(101);
});

test('hidden pages stop non-essential polling', async ({ context, page }, testInfo) => {
  const baseURL = baseURLFor(testInfo);
  await installSyntheticSession(context, baseURL, 'customer', true);
  await page.goto('/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(500);
  await page.evaluate(() => (window as any).__setPerfHidden(true));
  await page.waitForTimeout(150);
  const afterHide = (await readControls(context, baseURL)).requestLog.filter(item => item.method === 'GET').length;
  await page.waitForTimeout(500);
  const finalCount = (await readControls(context, baseURL)).requestLog.filter(item => item.method === 'GET').length;
  expect(finalCount).toBe(afterHide);
});
