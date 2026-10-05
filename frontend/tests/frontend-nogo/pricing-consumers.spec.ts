import { expect, test, type Page, type Route } from './_fixtures';

const user = {
  id: 7401,
  username: 'pricing-consumer',
  display_name: '动态价测试用户',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  roles: [],
  permissions: ['diagnosis:view', 'monitoring:view', 'brands:view', 'writing:view'],
  client_brand_ids: [101],
  permission_version: 8,
  agent_level: 1,
};

const priceList = [
  { feature_code: 'geo_diagnosis', feature_name: 'GEO专项诊断', cost_points: 987, cost_compute: 0, requires_paid_points: true, is_active: true },
  { feature_code: 'monitor_single', feature_name: '单次监测', cost_points: 431, cost_compute: 0, requires_paid_points: true, is_active: true },
  { feature_code: 'deep_analyze', feature_name: '深度行业解析', cost_points: 777, cost_compute: 0, requires_paid_points: true, is_active: true },
  { feature_code: 'article_gen', feature_name: '文章生成', cost_points: 333, cost_compute: 0, requires_paid_points: true, is_active: true },
  { feature_code: 'article_rewrite', feature_name: '文章重写', cost_points: 222, cost_compute: 0, requires_paid_points: true, is_active: true },
];

async function installSession(
  page: Page,
  options: {
    waitForPricing?: Promise<void>;
    onPricing?: () => void;
    onDiagnosis?: () => void;
    pricingResponse?: () => { status?: number; data?: typeof priceList };
  } = {},
) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'pricing-consumer-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'returning', completed_steps: [], skipped_steps: [], started_at: Date.now(), updated_at: Date.now(),
    }));
  });
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return route.fulfill({ json: { success: true, user } });
    if (path === '/api/wallet') return route.fulfill({ json: { success: true, data: {
      paid_points: 800, commission_points: 0, bonus_points: 0, frozen_points: 0, total_recharged: 800,
      deduction_preference: 'default', customer_credit_status: 'ready', customer_credit: {
        tool_credit_points: 0, publish_credit_points: 0, bonus_credit_points: 0,
        total_purchased_points: 0, total_consumed_points: 0,
      }, charge_notify_level: 'quiet',
    } } });
    if (path === '/api/wallet/pricing') {
      options.onPricing?.();
      if (options.waitForPricing) await options.waitForPricing;
      const configured = options.pricingResponse?.();
      if (configured?.status && configured.status >= 400) {
        return route.fulfill({ status: configured.status, json: { detail: 'pricing refresh failed' } });
      }
      return route.fulfill({ json: { success: true, data: configured?.data || priceList } });
    }
    if (path === '/api/diagnosis/start') {
      options.onDiagnosis?.();
      return route.fulfill({ json: { success: true, session_id: 'must-not-run' } });
    }
    if (path === '/api/client-context/list') return route.fulfill({ json: { success: true, clients: [{ id: 101, name: '动态价客户', diagnosis_count: 1, quote_count: 1, created_at: '2026-01-01' }] } });
    if (path === '/api/client-context/101') return route.fulfill({ json: { success: true, context: { brand: { id: 101, name: '动态价客户', diagnosis_count: 1 }, profile: null, materials: null, relatedQuoteIds: ['501'], socialProjects: [] } } });
    if (path === '/api/my-brand' || path === '/api/my-clients/101') return route.fulfill({ json: { brand: { id: 101, name: '动态价客户', industry: '软件服务' }, profile: { id: 901 } } });
    if (path === '/api/monitoring/clients') return route.fulfill({ json: { status: 'success', clients: [{ quote_id: 501, brand_id: 101, brand_name: '动态价客户', keyword_count: 1, tier: 'standard' }] } });
    if (path === '/api/monitoring/schedule') return route.fulfill({ json: { status: 'success', monitoring_enabled: false, jobs: [] } });
    if (/\/api\/monitoring\/clients\/\d+\/keywords/.test(path)) return route.fulfill({ json: { status: 'success', keywords: [{ keyword: '动态价关键词', keyword_key: 'dynamic-price', enabled: true }], client_appearance_rate: null, service_days: 1 } });
    if (path === '/api/monitoring/trend') return route.fulfill({ json: { status: 'success', trend: [] } });
    if (path.includes('monitoring-config')) return route.fulfill({ json: { status: 'success', monitoring_enabled: false } });
    if (path === '/api/public/whitelabel') return route.fulfill({ json: { success: true, data: null } });
    if (path === '/api/user/notifications/unread-count') return route.fulfill({ json: { status: 'success', count: 0 } });
    if (path === '/api/user/notifications') return route.fulfill({ json: { status: 'success', notifications: [] } });
    if (path === '/api/m3/customers') return route.fulfill({ json: { success: true, customers: [] } });
    if (/^\/api\/m3\/material-confirm\/status\/\d+$/.test(path)) return route.fulfill({ json: {
      success: true,
      status: 'none',
      brand_id: 101,
      brand_name: '动态价客户',
      can_generate_link: false,
      has_session: false,
      materials_summary: { filled_count: 0, total_fields: 8, fields_missing: [] },
    } });
    return route.fulfill({ json: { success: true, status: 'success', data: {}, items: [], records: [], total: 0 } });
  });
}

test('last-good price remains display-only while failed refresh blocks payment until a new trusted price arrives', async ({ page }) => {
  let pricingCalls = 0;
  let diagnosisCalls = 0;
  let mode: 'initial' | 'failed' | 'recovered' = 'initial';
  const recoveredPrices = priceList.map(item => item.feature_code === 'geo_diagnosis'
    ? { ...item, cost_points: 1111 }
    : item);
  await installSession(page, {
    onPricing: () => { pricingCalls += 1; },
    onDiagnosis: () => { diagnosisCalls += 1; },
    pricingResponse: () => mode === 'failed'
      ? { status: 503 }
      : { data: mode === 'recovered' ? recoveredPrices : priceList },
  });

  await page.goto('/diagnosis/new');
  await page.getByLabel('品牌名称 *').fill('动态价刷新品牌');
  await page.getByLabel('所属行业 *').fill('科技服务');
  await page.getByLabel('核心关键词 *').fill('动态价刷新关键词');
  const submit = page.getByRole('button', { name: /开始 GEO 诊断|价目读取中|价目不可用/ });
  await expect(submit).toBeEnabled();
  expect(pricingCalls).toBe(1);

  mode = 'failed';
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('omnirank-api-mutated', {
    detail: { url: '/api/admin/feature-pricing/geo_diagnosis', tags: ['pricing'] },
  })));
  await expect.poll(() => pricingCalls).toBe(2);
  await expect(submit).toBeDisabled();
  await expect(submit).toContainText(/价目不可用|价目读取中/);
  await submit.click({ force: true });
  expect(diagnosisCalls).toBe(0);

  mode = 'recovered';
  await page.evaluate(() => window.dispatchEvent(new CustomEvent('omnirank-api-mutated', {
    detail: { url: '/api/admin/feature-pricing/geo_diagnosis', tags: ['pricing'] },
  })));
  await expect.poll(() => pricingCalls).toBe(3);
  await expect(submit).toBeEnabled();
  await submit.click();
  await expect(page.getByRole('dialog')).toContainText('1,111 算力');
  expect(diagnosisCalls).toBe(0);
});

test('diagnosis freezes on unknown price and uses the exact dynamic DB price for insufficiency confirmation', async ({ page }) => {
  let releasePricing!: () => void;
  const pricingGate = new Promise<void>(resolve => { releasePricing = resolve; });
  let pricingStarted = false;
  let diagnosisCalls = 0;
  await installSession(page, {
    waitForPricing: pricingGate,
    onPricing: () => { pricingStarted = true; },
    onDiagnosis: () => { diagnosisCalls += 1; },
  });
  await page.goto('/diagnosis/new');
  await expect.poll(() => pricingStarted).toBe(true);
  const submit = page.getByRole('button', { name: /价目读取中|开始 GEO 诊断/ });
  await expect(submit).toBeDisabled();
  await expect(submit).toContainText('价目读取中');

  releasePricing();
  await page.getByPlaceholder('输入品牌名搜索或创建新品牌').fill('动态价品牌');
  await page.getByPlaceholder('选择或输入').fill('软件服务');
  await page.getByPlaceholder(/每行一个关键词/).fill('动态价关键词');
  const readySubmit = page.getByRole('button', { name: '开始 GEO 诊断' });
  await expect(readySubmit).toBeEnabled();
  await readySubmit.click();

  await expect(page.getByRole('dialog')).toContainText('987 算力');
  await expect(page.getByRole('dialog')).toContainText('还差 187');
  expect(diagnosisCalls).toBe(0);
});

for (const [path, landmark] of [
  ['/monitoring', /监测/],
  ['/my-brand', /动态价客户|我的品牌|品牌资料/],
  ['/my-clients/101', /动态价客户/],
  ['/writing', /写作|内容/],
  ['/feature-pricing', /算力价格表|价目/],
] as const) {
  test(`${path} mounts the consumer-driven dynamic pricing source`, async ({ page }) => {
    let pricingCalls = 0;
    await installSession(page, { onPricing: () => { pricingCalls += 1; } });
    await page.goto(path);
    await expect.poll(() => pricingCalls).toBe(1);
    await expect(page.locator('body')).toContainText(landmark);
    await expect(page.getByText('PricingProvider 未挂载')).toHaveCount(0);
  });
}
