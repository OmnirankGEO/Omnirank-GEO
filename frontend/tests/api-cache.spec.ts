import { expect, test } from 'playwright/test';

test('GET single-flight never reuses settled wallet or permission state after a mutation', async ({ page }) => {
  await page.goto('/login');
  const result = await page.evaluate(async () => {
    const loadModule = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      default: any;
      clearApiDedupeCache: () => void;
    }>;
    const loadSession = new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<{
      stageAuthoritativeSessionToken: (token: string, source: 'bootstrap') => string;
      confirmAuthoritativeSessionToken: (token: string) => string | null;
    }>;
    const { default: api, clearApiDedupeCache } = await loadModule();
    const session = await loadSession();
    session.stageAuthoritativeSessionToken('synthetic-api-cache-token', 'bootstrap');
    session.confirmAuthoritativeSessionToken('synthetic-api-cache-token');
    clearApiDedupeCache();

    let version = 1;
    let getCalls = 0;
    api.defaults.adapter = async (config: any) => {
      const method = String(config.method || 'get').toLowerCase();
      if (method === 'get') {
        getCalls += 1;
        const capturedVersion = version;
        await new Promise<void>((resolve, reject) => {
          const timer = window.setTimeout(resolve, 40);
          const onAbort = () => {
            window.clearTimeout(timer);
            reject(new Error('aborted stale GET'));
          };
          if (config.signal?.aborted) onAbort();
          else config.signal?.addEventListener('abort', onAbort, { once: true });
        });
        return { data: { version: capturedVersion }, status: 200, statusText: 'OK', headers: {}, config };
      }
      version += 1;
      return { data: { version }, status: 200, statusText: 'OK', headers: {}, config };
    };

    const [first, joined] = await Promise.all([
      api.get('/api/wallet'),
      api.get('/api/wallet'),
    ]);
    const callsAfterSingleFlight = getCalls;
    const settledFollowUp = await api.get('/api/wallet');
    const callsAfterSettledFollowUp = getCalls;
    await api.post('/api/charge', {});
    const afterMutation = await api.get('/api/wallet');

    const stalePending = api.get('/api/permissions').catch((error: Error) => error.message);
    await new Promise((resolve) => window.setTimeout(resolve, 5));
    await api.patch('/api/admin/permissions', {});
    const freshPermission = await api.get('/api/permissions');
    const staleOutcome = await stalePending;

    return {
      first: first.data.version,
      joined: joined.data.version,
      callsAfterSingleFlight,
      settledFollowUp: settledFollowUp.data.version,
      callsAfterSettledFollowUp,
      afterMutation: afterMutation.data.version,
      freshPermission: freshPermission.data.version,
      staleOutcome,
    };
  });

  expect(result.first).toBe(1);
  expect(result.joined).toBe(1);
  expect(result.callsAfterSingleFlight).toBe(1);
  expect(result.settledFollowUp).toBe(1);
  expect(result.callsAfterSettledFollowUp).toBe(2);
  expect(result.afterMutation).toBe(2);
  expect(result.freshPermission).toBe(3);
  expect(result.staleOutcome).toMatch(/canceled|aborted stale GET/);
});

test('pending GET key recursively distinguishes nested params and fails open on unsafe values', async ({ page }) => {
  await page.goto('/login');
  const result = await page.evaluate(async () => {
    const loadModule = new Function('return import("/src/lib/api.ts")') as () => Promise<{
      default: any;
      clearApiDedupeCache: () => void;
    }>;
    const loadSession = new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<{
      stageAuthoritativeSessionToken: (token: string, source: 'bootstrap') => string;
      confirmAuthoritativeSessionToken: (token: string) => string | null;
    }>;
    const [{ default: api, clearApiDedupeCache }, session] = await Promise.all([loadModule(), loadSession()]);
    session.stageAuthoritativeSessionToken('nested-params-token', 'bootstrap');
    session.confirmAuthoritativeSessionToken('nested-params-token');
    clearApiDedupeCache();

    let calls = 0;
    api.defaults.adapter = async (config: any) => {
      const call = ++calls;
      await new Promise(resolve => window.setTimeout(resolve, 40));
      return { data: { call }, status: 200, statusText: 'OK', headers: {}, config };
    };

    const beforeNestedDifferent = calls;
    const nestedDifferent = await Promise.all([
      api.get('/api/nested-different', { params: { filter: { brand_id: 1 } } }),
      api.get('/api/nested-different', { params: { filter: { brand_id: 2 } } }),
    ]);
    const nestedDifferentCalls = calls - beforeNestedDifferent;

    const beforeNestedOrder = calls;
    const nestedOrder = await Promise.all([
      api.get('/api/nested-order', { params: { filter: { brand_id: 1, city: 'A' }, page: 1 } }),
      api.get('/api/nested-order', { params: { page: 1, filter: { city: 'A', brand_id: 1 } } }),
    ]);
    const nestedOrderCalls = calls - beforeNestedOrder;

    const beforeArrayOrder = calls;
    const arrayOrder = await Promise.all([
      api.get('/api/array-order', { params: { ids: [1, 2] } }),
      api.get('/api/array-order', { params: { ids: [2, 1] } }),
    ]);
    const arrayOrderCalls = calls - beforeArrayOrder;

    const circular: Record<string, unknown> = { filter: { brand_id: 1 } };
    circular.self = circular;
    const beforeCircular = calls;
    const circularResults = await Promise.allSettled([
      Promise.resolve().then(() => api.get('/api/circular', { params: circular })),
      Promise.resolve().then(() => api.get('/api/circular', { params: circular })),
    ]);
    const circularCalls = calls - beforeCircular;

    const beforeFunction = calls;
    const functionResults = await Promise.all([
      api.get('/api/unserializable', { params: { filter: () => 1 } }),
      api.get('/api/unserializable', { params: { filter: () => 1 } }),
    ]);
    const functionCalls = calls - beforeFunction;

    return {
      nestedDifferentCalls,
      nestedDifferentResultCalls: nestedDifferent.map((response: any) => response.data.call),
      nestedOrderCalls,
      nestedOrderResultCalls: nestedOrder.map((response: any) => response.data.call),
      arrayOrderCalls,
      arrayOrderResultCalls: arrayOrder.map((response: any) => response.data.call),
      circularCalls,
      circularOutcomes: circularResults.map(result => result.status),
      functionCalls,
      functionResultCalls: functionResults.map((response: any) => response.data.call),
    };
  });

  expect(result.nestedDifferentCalls).toBe(2);
  expect(new Set(result.nestedDifferentResultCalls).size).toBe(2);
  expect(result.nestedOrderCalls).toBe(1);
  expect(new Set(result.nestedOrderResultCalls).size).toBe(1);
  expect(result.arrayOrderCalls).toBe(2);
  expect(new Set(result.arrayOrderResultCalls).size).toBe(2);
  expect(result.circularCalls).toBe(0);
  expect(result.circularOutcomes).toEqual(['rejected', 'rejected']);
  expect(result.functionCalls).toBe(2);
  expect(new Set(result.functionResultCalls).size).toBe(2);
});

test('v35 pending read is aborted before a post-mutation refresh can join it', async ({ page }) => {
  let pricingCalls = 0;
  await page.route('**/api/agent/pricing/skus', async (route) => {
    pricingCalls += 1;
    const capturedVersion = pricingCalls;
    await new Promise((resolve) => setTimeout(resolve, 80));
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ items: [{ version: capturedVersion }] }),
    });
  });
  await page.goto('/login');

  const result = await page.evaluate(async () => {
    const loadApi = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: any }>;
    const loadV35 = new Function('return import("/src/lib/v35w2Api.ts")') as () => Promise<{
      agentApi: { pricingSKUs: () => Promise<{ items: Array<{ version: number }> }> };
    }>;
    const loadSession = new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<{
      stageAuthoritativeSessionToken: (token: string, source: 'bootstrap') => string;
      confirmAuthoritativeSessionToken: (token: string) => string | null;
    }>;
    const [{ default: api }, { agentApi }, session] = await Promise.all([loadApi(), loadV35(), loadSession()]);
    session.stageAuthoritativeSessionToken('synthetic-v35-cache-token', 'bootstrap');
    session.confirmAuthoritativeSessionToken('synthetic-v35-cache-token');
    api.defaults.adapter = async (config: any) => ({
      data: { success: true }, status: 200, statusText: 'OK', headers: {}, config,
    });

    const stale = agentApi.pricingSKUs().then(
      (value) => `resolved:${value.items[0]?.version}`,
      (error: Error) => `${error.name}:${error.message}`,
    );
    await new Promise((resolve) => window.setTimeout(resolve, 10));
    await api.post('/api/agent/pricing/mutation', {});
    const fresh = await agentApi.pricingSKUs();
    return { stale: await stale, freshVersion: fresh.items[0]?.version };
  });

  expect(pricingCalls).toBe(2);
  expect(result.stale).toMatch(/Abort/);
  expect(result.freshVersion).toBe(2);
});

test('notification acknowledgement does not cancel unrelated inventory or monitoring reads', async ({ page }) => {
  let inventoryCalls = 0;
  await page.route('**/api/agent/inventory/balance', async route => {
    inventoryCalls += 1;
    await new Promise(resolve => setTimeout(resolve, 100));
    await route.fulfill({ json: {
      paid_inventory_points: 77, bonus_inventory_points: 0, frozen_inventory_points: 0,
      total_purchased_points: 77, total_allocated_points: 0, alert_level: 'healthy',
    } });
  });
  await page.goto('/login');

  const result = await page.evaluate(async () => {
    const loadApi = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: any }>;
    const loadV35 = new Function('return import("/src/lib/v35w2Api.ts")') as () => Promise<{
      agentApi: { inventoryBalance: () => Promise<{ paid_inventory_points: number }> };
    }>;
    const loadSession = new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<{
      stageAuthoritativeSessionToken: (token: string, source: 'bootstrap') => string;
      confirmAuthoritativeSessionToken: (token: string) => string | null;
    }>;
    const [{ default: api }, { agentApi }, session] = await Promise.all([loadApi(), loadV35(), loadSession()]);
    session.stageAuthoritativeSessionToken('resource-tag-token', 'bootstrap');
    session.confirmAuthoritativeSessionToken('resource-tag-token');

    let monitoringAborted = false;
    let monitoringCalls = 0;
    api.defaults.adapter = async (config: any) => {
      const method = String(config.method || 'get').toLowerCase();
      if (method === 'get' && String(config.url).includes('/api/monitoring/')) {
        monitoringCalls += 1;
        await new Promise<void>((resolve, reject) => {
          const timer = window.setTimeout(resolve, 90);
          const onAbort = () => {
            monitoringAborted = true;
            window.clearTimeout(timer);
            reject(new Error('monitoring read aborted'));
          };
          if (config.signal?.aborted) onAbort();
          else config.signal?.addEventListener('abort', onAbort, { once: true });
        });
        return { data: { status: 'success' }, status: 200, statusText: 'OK', headers: {}, config };
      }
      return { data: { success: true }, status: 200, statusText: 'OK', headers: {}, config };
    };

    const inventory = agentApi.inventoryBalance();
    const monitoring = api.get('/api/monitoring/read-probe');
    await new Promise(resolve => window.setTimeout(resolve, 10));
    await api.post('/api/user/notifications/42/read', {});
    const [inventoryValue, monitoringValue] = await Promise.all([inventory, monitoring]);
    return {
      inventoryPoints: inventoryValue.paid_inventory_points,
      monitoringStatus: monitoringValue.data.status,
      monitoringCalls,
      monitoringAborted,
    };
  });

  expect(inventoryCalls).toBe(1);
  expect(result).toEqual({
    inventoryPoints: 77,
    monitoringStatus: 'success',
    monitoringCalls: 1,
    monitoringAborted: false,
  });
});

test('authApi brand mutation invalidates client context only and forces an immediate fresh read', async ({ page }) => {
  let inventoryCalls = 0;
  await page.route('**/api/agent/inventory/balance', async route => {
    inventoryCalls += 1;
    await new Promise(resolve => setTimeout(resolve, 90));
    await route.fulfill({ json: {
      paid_inventory_points: 88, bonus_inventory_points: 0, frozen_inventory_points: 0,
      total_purchased_points: 88, total_allocated_points: 0, alert_level: 'healthy',
    } });
  });
  await page.goto('/login');

  const result = await page.evaluate(async () => {
    const loadApi = new Function('return import("/src/lib/api.ts")') as () => Promise<{ default: any }>;
    const loadAuth = new Function('return import("/src/context/AuthContext.tsx")') as () => Promise<{ authApi: any }>;
    const loadV35 = new Function('return import("/src/lib/v35w2Api.ts")') as () => Promise<{
      agentApi: { inventoryBalance: () => Promise<{ paid_inventory_points: number }> };
    }>;
    const loadSession = new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<{
      stageAuthoritativeSessionToken: (token: string, source: 'bootstrap') => string;
      confirmAuthoritativeSessionToken: (token: string) => string | null;
    }>;
    const [{ default: api }, { authApi }, { agentApi }, session] = await Promise.all([
      loadApi(), loadAuth(), loadV35(), loadSession(),
    ]);
    session.stageAuthoritativeSessionToken('auth-api-resource-token', 'bootstrap');
    session.confirmAuthoritativeSessionToken('auth-api-resource-token');

    let clientVersion = 1;
    let clientCalls = 0;
    let monitoringAborted = false;
    const mutationEvents: string[][] = [];
    window.addEventListener('omnirank-api-mutated', ((event: CustomEvent<{ tags?: string[] }>) => {
      mutationEvents.push(event.detail?.tags || []);
    }) as EventListener);
    api.defaults.adapter = async (config: any) => {
      const url = String(config.url || '');
      if (url.includes('/api/client-context/101')) {
        clientCalls += 1;
        const captured = clientVersion;
        await new Promise<void>((resolve, reject) => {
          const timer = window.setTimeout(resolve, 80);
          const onAbort = () => { window.clearTimeout(timer); reject(new Error('client read canceled')); };
          if (config.signal?.aborted) onAbort();
          else config.signal?.addEventListener('abort', onAbort, { once: true });
        });
        return { data: { success: true, context: { brand: { industry: captured === 1 ? '旧行业' : '新行业' } } }, status: 200, statusText: 'OK', headers: {}, config };
      }
      if (url.includes('/api/monitoring/read-probe')) {
        await new Promise<void>((resolve, reject) => {
          const timer = window.setTimeout(resolve, 100);
          const onAbort = () => { monitoringAborted = true; window.clearTimeout(timer); reject(new Error('monitoring canceled')); };
          if (config.signal?.aborted) onAbort();
          else config.signal?.addEventListener('abort', onAbort, { once: true });
        });
        return { data: { status: 'success' }, status: 200, statusText: 'OK', headers: {}, config };
      }
      return { data: { success: true }, status: 200, statusText: 'OK', headers: {}, config };
    };
    authApi.defaults.adapter = async (config: any) => {
      clientVersion = 2;
      return { data: { success: true }, status: 200, statusText: 'OK', headers: {}, config };
    };

    const staleClient = api.get('/api/client-context/101').then(
      (response: any) => `resolved:${response.data.context.brand.industry}`,
      (error: Error) => error.message,
    );
    const monitoring = api.get('/api/monitoring/read-probe');
    const inventory = agentApi.inventoryBalance();
    await new Promise(resolve => window.setTimeout(resolve, 10));
    await authApi.put('/api/my-brand', { industry: '新行业', cities: ['上海'] });
    const freshClient = await api.get('/api/client-context/101');
    const [staleOutcome, monitoringResponse, inventoryResponse] = await Promise.all([staleClient, monitoring, inventory]);
    return {
      staleOutcome,
      freshIndustry: freshClient.data.context.brand.industry,
      clientCalls,
      monitoringStatus: monitoringResponse.data.status,
      monitoringAborted,
      inventoryPoints: inventoryResponse.paid_inventory_points,
      mutationEvents,
    };
  });

  expect(inventoryCalls).toBe(1);
  expect(result.staleOutcome).toMatch(/cancel/i);
  expect(result.freshIndustry).toBe('新行业');
  expect(result.clientCalls).toBe(2);
  expect(result.monitoringStatus).toBe('success');
  expect(result.monitoringAborted).toBe(false);
  expect(result.inventoryPoints).toBe(88);
  expect(result.mutationEvents).toContainEqual(['clients']);
});

test('brand material mutations rely on resource-scoped invalidation, never the global identity abort', async ({ page }) => {
  await page.goto('/login');
  const source = await page.evaluate(async () => {
    const response = await fetch('/src/pages/Brand/BrandDetailPage.tsx');
    return response.text();
  });

  expect(source).not.toContain('clearApiDedupeCache');
  expect(source).toContain('materialConfirmApi.clean');
  expect(source).toContain('materialConfirmApi.generateLink');
});
