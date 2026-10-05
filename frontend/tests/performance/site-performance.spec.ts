import { createHash, randomUUID } from 'node:crypto';
import { expect, test, type BrowserContext, type Locator, type Page } from 'playwright/test';

export type Role = 'anonymous' | 'customer' | 'agent' | 'admin';

const scenarios: Record<Role, string[]> = {
  anonymous: ['/login'],
  customer: ['/', '/customer/recharge'],
  agent: ['/', '/agent/pricing', '/agent/inventory', '/monitoring', '/publish', '/publish/history'],
  admin: ['/', '/admin/users', '/admin/pricing-center'],
};

const runs = Math.max(1, Number(process.env.PERF_RUNS || '1'));

type RouteContract = {
  heading?: string;
  label?: string;
  action: { kind: 'button'; name: string } | { kind: 'testId'; value: string } | { kind: 'placeholder'; name: string };
};

const routeContracts: Record<string, RouteContract> = {
  'anonymous:/login': { label: '手机号 / 用户名', action: { kind: 'placeholder', name: '请输入手机号或用户名' } },
  'customer:/': { heading: 'GEO 业务', action: { kind: 'testId', value: 'home-refresh' } },
  'customer:/customer/recharge': { heading: '购买算力', action: { kind: 'button', name: '刷新' } },
  'agent:/': { heading: 'GEO 业务', action: { kind: 'testId', value: 'home-refresh' } },
  'agent:/agent/pricing': { heading: '客户售价', action: { kind: 'button', name: '刷新' } },
  'agent:/agent/inventory': { heading: '算力库存', action: { kind: 'button', name: '刷新' } },
  'agent:/monitoring': { heading: '监测中心', action: { kind: 'button', name: '刷新' } },
  'agent:/publish': { heading: '发布中心', action: { kind: 'button', name: '代发' } },
  'agent:/publish/history': { heading: '发布记录', action: { kind: 'testId', value: 'publish-history-refresh' } },
  'admin:/': { heading: '运营控制台', action: { kind: 'button', name: '刷新' } },
  'admin:/admin/users': { heading: '用户管理', action: { kind: 'button', name: '刷新' } },
  'admin:/admin/pricing-center': { heading: '算力定价中心', action: { kind: 'button', name: '刷新' } },
};

function baseURLFor(testInfo: { project: { use: { baseURL?: string } } }): string {
  const baseURL = String(testInfo.project.use.baseURL || '');
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(baseURL)) throw new Error(`invalid exclusive baseURL: ${baseURL}`);
  return baseURL;
}

async function updateControls(context: BrowserContext, baseURL: string, patch: Record<string, unknown> = {}) {
  const response = await context.request.post(`${baseURL}/__perf/control`, { data: patch });
  expect(response.status(), 'performance fixture control response').toBe(200);
  return response.json() as Promise<Record<string, any>>;
}

async function readControls(context: BrowserContext, baseURL: string) {
  const response = await context.request.get(`${baseURL}/__perf/control`);
  expect(response.status(), 'performance fixture control read').toBe(200);
  return response.json() as Promise<Record<string, any>>;
}

async function installSyntheticSession(context: BrowserContext, baseURL: string, role: Role) {
  const sessionId = randomUUID();
  await context.addCookies([{ name: 'omnirank_perf_session', value: sessionId, url: baseURL }]);
  await context.addInitScript(({ roleName, owner }) => {
    const initKey = '__omnirank_perf_initialized';
    if (localStorage.getItem(initKey) !== owner) {
      localStorage.clear();
      sessionStorage.clear();
      localStorage.setItem(initKey, owner);
      if (roleName !== 'anonymous') localStorage.setItem('omnirank_token', `synthetic-perf-${roleName}`);
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
    const perf = (window as any).__perf = {
      lcp: 0,
      cls: 0,
      longTasks: 0,
      inp: 0,
      inpObserved: false,
      clsSources: [],
      skeletonFirst: 0,
      skeletonEnd: 0,
      skeletonVisible: false,
    };
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries()) perf.lcp = Math.max(perf.lcp, entry.startTime);
      }).observe({ type: 'largest-contentful-paint', buffered: true });
    } catch {}
    try {
      new PerformanceObserver(list => {
        for (const entry of list.getEntries() as any) {
          if (entry.hadRecentInput) continue;
          perf.cls += entry.value;
          perf.clsSources.push({
            value: entry.value,
            sources: (entry.sources || []).slice(0, 5).map((source: any) => ({
              node: source.node ? `${source.node.tagName || ''}${source.node.id ? `#${source.node.id}` : ''}.${String(source.node.className || '').replace(/\s+/g, '.').slice(0, 120)}` : '',
              previousRect: source.previousRect,
              currentRect: source.currentRect,
            })),
          });
        }
      }).observe({ type: 'layout-shift', buffered: true });
    } catch {}
    try {
      new PerformanceObserver(list => { perf.longTasks += list.getEntries().length; }).observe({ type: 'longtask', buffered: true });
    } catch {}
    const observeInput = (type: string, options: Record<string, unknown>) => {
      try {
        new PerformanceObserver(list => {
          for (const entry of list.getEntries()) {
            perf.inpObserved = true;
            perf.inp = Math.max(perf.inp, entry.duration);
          }
        }).observe({ type, buffered: true, ...options } as any);
      } catch {}
    };
    observeInput('first-input', {});
    observeInput('event', { durationThreshold: 16 });
    const observeSkeletons = () => {
      const inspect = () => {
        const visible = !!document.querySelector('.animate-pulse, [data-testid*="skeleton"], [class*="skeleton"]');
        if (visible && !perf.skeletonVisible) {
          perf.skeletonVisible = true;
          if (!perf.skeletonFirst) perf.skeletonFirst = performance.now();
        } else if (!visible && perf.skeletonVisible) {
          perf.skeletonVisible = false;
          perf.skeletonEnd = performance.now();
        }
      };
      inspect();
      new MutationObserver(inspect).observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ['class'],
      });
    };
    document.addEventListener('DOMContentLoaded', observeSkeletons, { once: true });
  }, { roleName: role, owner: sessionId });
  await updateControls(context, baseURL, { role, resetCounters: true });
}

function mainFor(page: Page, role: Role) {
  return role === 'anonymous' ? page.getByTestId('login-page-main') : page.getByTestId('app-page-main');
}

async function assertRouteContract(page: Page, role: Role, path: string): Promise<Locator> {
  const contract = routeContracts[`${role}:${path}`];
  expect(contract, `missing route contract for ${role} ${path}`).toBeTruthy();
  await expect.poll(() => new URL(page.url()).pathname, { message: `${role} ${path} exact URL` }).toBe(path);
  const main = mainFor(page, role);
  await expect(main, `${role} ${path} page main`).toBeVisible();
  if (contract.heading) {
    await main.getByRole('heading', { name: contract.heading, exact: true }).first().waitFor({ state: 'visible' });
  }
  if (contract.label) {
    await main.getByText(contract.label, { exact: true }).waitFor({ state: 'visible' });
  }
  const action = contract.action.kind === 'button'
    ? main.getByRole('button', { name: contract.action.name, exact: true }).first()
    : contract.action.kind === 'testId'
      ? main.getByTestId(contract.action.value)
      : main.getByPlaceholder(contract.action.name, { exact: true });
  await action.waitFor({ state: 'visible' });
  await expect(action, `${role} ${path} page-specific primary operation enabled`).toBeEnabled();
  return action;
}

type BuildProvenance = {
  gitHead: string;
  gitTree: string;
  buildNonce: string;
  sourceClean: boolean;
  buildSha256: string;
  entrySha256: string;
  entryFile: string;
  css: string[];
  manifestEntries: number;
  preBuildStatusSha256: string;
  postBuildStatusSha256: string;
  viteEnvKeys: string[];
  viteEnvSha256: string;
  assetSha256: Record<string, string>;
};

async function assertBuildProvenance(
  context: BrowserContext,
  baseURL: string,
  expected: { gitHead: string; gitTree: string; buildNonce: string },
): Promise<BuildProvenance> {
  const markerResponse = await context.request.get(`${baseURL}/performance-build.json`, { headers: { 'Cache-Control': 'no-cache' } });
  expect(markerResponse.status()).toBe(200);
  const marker = await markerResponse.json() as BuildProvenance;
  const liveResponse = await context.request.get(`${baseURL}/__perf/provenance`, { headers: { 'Cache-Control': 'no-cache' } });
  expect(liveResponse.status()).toBe(200);
  const live = await liveResponse.json() as Record<string, any>;
  expect(marker.gitHead).toBe(expected.gitHead);
  expect(marker.gitTree).toBe(expected.gitTree);
  expect(marker.buildNonce).toBe(expected.buildNonce);
  expect(live.gitHead).toBe(expected.gitHead);
  expect(live.gitTree).toBe(expected.gitTree);
  expect(live.buildNonce).toBe(expected.buildNonce);
  expect(marker.preBuildStatusSha256).toBe(marker.postBuildStatusSha256);
  expect(live.statusSha256).toBe(marker.postBuildStatusSha256);
  if (process.env.PERF_ALLOW_DIRTY !== '1') {
    expect(marker.sourceClean, 'formal performance evidence requires a clean pre/post build tree').toBe(true);
    expect(live.sourceClean, 'formal performance evidence requires a clean live tree').toBe(true);
  }
  expect(marker.buildSha256).toMatch(/^[a-f0-9]{64}$/);
  expect(marker.entrySha256).toMatch(/^[a-f0-9]{64}$/);
  expect(marker.viteEnvSha256).toMatch(/^[a-f0-9]{64}$/);
  expect(marker.entryFile).toMatch(/^assets\/.*\.js$/);
  expect(marker.manifestEntries).toBeGreaterThan(0);
  expect(Object.keys(marker.assetSha256).length).toBeGreaterThan(0);
  for (const [file, expectedHash] of Object.entries(marker.assetSha256)) {
    const response = await context.request.get(`${baseURL}/${file}`, { headers: { 'Cache-Control': 'no-cache' } });
    expect(response.status(), `manifest asset ${file}`).toBe(200);
    expect(createHash('sha256').update(await response.body()).digest('hex'), `manifest asset hash ${file}`).toBe(expectedHash);
  }
  return marker;
}

async function enableFast4G(page: Page) {
  const session = await page.context().newCDPSession(page);
  await session.send('Network.enable');
  await session.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: 150,
    downloadThroughput: (1.6 * 1024 * 1024) / 8,
    uploadThroughput: (750 * 1024) / 8,
    connectionType: 'cellular4g',
  });
  return session;
}

async function measure(page: Page, role: Role, path: string, baseURL: string) {
  const controlsBefore = await readControls(page.context(), baseURL);
  const unknownStart = controlsBefore.unknownRequests.length;
  const requests = new Map<string, number>();
  const statuses: number[] = [];
  const errors: string[] = [];
  const pendingApis = new Set<any>();
  let lastApiActivityAt = Date.now();
  let startedAt = Date.now();
  const onRequest = (request: any) => {
    if (request.method() === 'GET') requests.set(request.url(), (requests.get(request.url()) || 0) + 1);
    if (request.url().includes('/api/')) {
      pendingApis.add(request);
      lastApiActivityAt = Date.now();
    }
  };
  const onResponse = (response: any) => { statuses.push(response.status()); };
  const onRequestFinished = (request: any) => {
    if (!request.url().includes('/api/')) return;
    pendingApis.delete(request);
    lastApiActivityAt = Date.now();
  };
  const onConsole = (message: any) => { if (message.type() === 'error') errors.push(message.text()); };
  const onPageError = (error: Error) => { errors.push(`pageerror:${error.message}`); };
  page.on('request', onRequest);
  page.on('response', onResponse);
  page.on('requestfinished', onRequestFinished);
  page.on('requestfailed', onRequestFinished);
  page.on('console', onConsole);
  page.on('pageerror', onPageError);
  startedAt = Date.now();
  await page.goto(path, { waitUntil: 'domcontentloaded' });
  const action = await assertRouteContract(page, role, path);
  await page.waitForFunction(() => {
    const fcp = performance.getEntriesByName('first-contentful-paint')[0]?.startTime || 0;
    return fcp > 0 && ((window as any).__perf?.lcp || 0) > 0;
  }, undefined, { timeout: 15_000, polling: 50 });
  const interactiveMs = await page.evaluate(() => performance.now());
  const startupRequests = new Map(requests);
  await action.click();
  await page.waitForFunction(() => (window as any).__perf?.inpObserved === true, undefined, { timeout: 5_000, polling: 25 });
  const minimumStableWindowEnd = Date.now() + 2_000;
  const quietDeadline = Date.now() + 6_000;
  while (Date.now() < quietDeadline) {
    if (Date.now() >= minimumStableWindowEnd && pendingApis.size === 0 && Date.now() - lastApiActivityAt >= 750) break;
    await page.waitForTimeout(50);
  }
  const metrics = await page.evaluate(interactive => {
    const navigation = performance.getEntriesByType('navigation')[0] as PerformanceNavigationTiming;
    const resources = performance.getEntriesByType('resource') as PerformanceResourceTiming[];
    const paints = Object.fromEntries(performance.getEntriesByType('paint').map(entry => [entry.name, entry.startTime]));
    const apiResources = resources.filter(entry => entry.name.includes('/api/'));
    const assetResources = resources.filter(entry => new URL(entry.name).pathname.startsWith('/assets/'));
    const bytes = (extension: RegExp, decoded = false) => resources
      .filter(entry => extension.test(new URL(entry.name).pathname))
      .reduce((sum, entry) => sum + (decoded ? entry.decodedBodySize : entry.transferSize || 0), 0);
    const perf = (window as any).__perf || {};
    const skeletonEnd = perf.skeletonEnd || (perf.skeletonVisible ? performance.now() : perf.skeletonFirst);
    return {
      dns: navigation.domainLookupEnd - navigation.domainLookupStart,
      tcp: navigation.connectEnd - navigation.connectStart,
      ttfb: navigation.responseStart - navigation.requestStart,
      fcp: paints['first-contentful-paint'] || 0,
      lcp: perf.lcp || 0,
      cls: perf.cls || 0,
      clsSources: perf.clsSources || [],
      inp: perf.inp || 0,
      inpObserved: perf.inpObserved === true,
      longTasks: perf.longTasks || 0,
      skeletonMs: perf.skeletonFirst ? Math.max(0, skeletonEnd - perf.skeletonFirst) : 0,
      transferBytes: resources.reduce((sum, entry) => sum + (entry.transferSize || 0), 0),
      decodedBytes: resources.reduce((sum, entry) => sum + (entry.decodedBodySize || 0), 0),
      resourceBytes: { js: bytes(/\.js$/i), css: bytes(/\.css$/i), fonts: bytes(/\.(woff2?|ttf|otf)$/i), images: bytes(/\.(png|jpe?g|gif|webp|svg|avif)$/i) },
      decodedResourceBytes: { js: bytes(/\.js$/i, true), css: bytes(/\.css$/i, true), fonts: bytes(/\.(woff2?|ttf|otf)$/i, true), images: bytes(/\.(png|jpe?g|gif|webp|svg|avif)$/i, true) },
      requestCount: resources.length + 1,
      interactiveMs: interactive,
      staticAssetCount: assetResources.length,
      staticCacheHitRate: assetResources.length ? assetResources.filter(entry => entry.transferSize === 0).length / assetResources.length : 0,
      apiDurations: apiResources.map(entry => entry.duration),
      apiWaterfall: apiResources.map(entry => ({ path: new URL(entry.name).pathname, startMs: entry.startTime, durationMs: entry.duration, transferBytes: entry.transferSize || 0 })),
      assetWaterfall: assetResources.map(entry => ({ path: new URL(entry.name).pathname, initiatorType: entry.initiatorType, startMs: entry.startTime, durationMs: entry.duration, transferBytes: entry.transferSize || 0, decodedBytes: entry.decodedBodySize || 0 })),
    };
  }, interactiveMs);
  const controlsAfter = await readControls(page.context(), baseURL);
  page.off('request', onRequest);
  page.off('response', onResponse);
  page.off('requestfinished', onRequestFinished);
  page.off('requestfailed', onRequestFinished);
  page.off('console', onConsole);
  page.off('pageerror', onPageError);
  const duplicateGets = [...startupRequests.entries()].filter(([, count]) => count > 1);
  const duplicateApiGets = duplicateGets.filter(([url]) => url.includes('/api/'));
  return {
    ...metrics,
    path,
    observationMs: Date.now() - startedAt,
    duplicateGets,
    duplicateApiGets,
    statuses,
    errors,
    unknownApis: controlsAfter.unknownRequests.slice(unknownStart),
  };
}

function assertMeasurement(result: Record<string, any>, role: Role, path: string, phase: 'cold' | 'warm') {
  expect(result.duplicateApiGets, `${role} ${path} ${phase} duplicate startup API GETs`).toEqual([]);
  expect(result.unknownApis, `${role} ${path} ${phase} unknown method+path API contracts`).toEqual([]);
  expect(result.errors, `${role} ${path} ${phase} console errors`).toEqual([]);
  expect(result.fcp, `${role} ${path} ${phase} FCP observed`).toBeGreaterThan(0);
  expect(result.lcp, `${role} ${path} ${phase} LCP observed`).toBeGreaterThan(0);
  expect(result.inpObserved, `${role} ${path} ${phase} INP interaction observed`).toBe(true);
  expect.soft(result.interactiveMs, `${role} ${path} ${phase} interactive gate`).toBeLessThanOrEqual(phase === 'cold' ? 3_000 : 1_500);
  expect.soft(result.lcp, `${role} ${path} ${phase} LCP gate`).toBeLessThanOrEqual(2_500);
  expect.soft(result.cls, `${role} ${path} ${phase} CLS gate`).toBeLessThanOrEqual(0.1);
  expect.soft(result.inp, `${role} ${path} ${phase} INP gate`).toBeLessThanOrEqual(200);
  expect(result.statuses.some((status: number) => status === 401 || status === 403 || status === 429 || status >= 500), `${role} ${path} ${phase} HTTP failures`).toBe(false);
}

for (const [role, paths] of Object.entries(scenarios) as [Role, string[]][]) {
  for (const path of paths) {
    test(`${role} ${path} same-network cold/warm performance`, async ({ browser }, testInfo) => {
      const baseURL = baseURLFor(testInfo);
      const results: Array<Record<string, unknown>> = [];
      for (let index = 0; index < runs; index += 1) {
        const context = await browser.newContext({ viewport: testInfo.project.use.viewport });
        await installSyntheticSession(context, baseURL, role);
        const page = await context.newPage();
        const networkSession = await enableFast4G(page);
        const cold = await measure(page, role, path, baseURL);
        results.push({ phase: 'cold', run: index + 1, ...cold });
        assertMeasurement(cold, role, path, 'cold');
        expect(await page.locator('body').innerText()).not.toMatch(/页面出错了|加载错误|暂时无法确认登录状态/);
        if (index === 0 && role === 'anonymous' && path === '/login') {
          const provenance = await assertBuildProvenance(context, baseURL, {
            gitHead: String(testInfo.config.metadata.gitHead),
            gitTree: String(testInfo.config.metadata.gitTree),
            buildNonce: String(testInfo.config.metadata.buildNonce),
          });
          await testInfo.attach(`${testInfo.project.name}-build-provenance.json`, {
            body: Buffer.from(JSON.stringify(provenance, null, 2)),
            contentType: 'application/json',
          });
        }
        const warm = await measure(page, role, path, baseURL);
        results.push({ phase: 'warm', run: index + 1, ...warm });
        assertMeasurement(warm, role, path, 'warm');
        expect(warm.staticAssetCount, `${role} ${path} warm hash asset evidence`).toBeGreaterThan(0);
        expect(warm.staticCacheHitRate, `${role} ${path} warm hash static cache hit rate`).toBeGreaterThan(0.95);
        expect(warm.transferBytes, `${role} ${path} warm transfer must improve on cold under identical Fast 4G`).toBeLessThan(cold.transferBytes);
        await networkSession.detach();
        await context.close();
      }
      await testInfo.attach(`${role}-${path.replace(/[^a-z0-9]+/gi, '-')}-${testInfo.project.name}-metrics.json`, {
        body: Buffer.from(JSON.stringify(results, null, 2)),
        contentType: 'application/json',
      });
    });
  }
}

test('measurement observes the full stable window and catches a late unknown API', async ({ browser }, testInfo) => {
  const baseURL = baseURLFor(testInfo);
  const context = await browser.newContext({ viewport: testInfo.project.use.viewport });
  await installSyntheticSession(context, baseURL, 'customer');
  await context.addInitScript(() => {
    window.addEventListener('DOMContentLoaded', () => {
      window.setTimeout(() => { void fetch('/api/performance-late-unknown').catch(() => undefined); }, 1_200);
    }, { once: true });
  });
  const page = await context.newPage();
  const session = await enableFast4G(page);
  const result = await measure(page, 'customer', '/', baseURL);
  expect(result.unknownApis).toEqual([{ method: 'GET', path: '/api/performance-late-unknown' }]);
  expect(result.observationMs).toBeGreaterThanOrEqual(2_000);
  await updateControls(context, baseURL, { resetCounters: true });
  await session.detach();
  await context.close();
});
