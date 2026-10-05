import { expect, test, type Route } from './_fixtures';

import { fixture } from '../geo-observation-a/mock';

const agentUser = {
  id: 102,
  username: 'qa-agent',
  display_name: '服务商',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '服务商' }],
  permissions: ['monitoring:read'],
  client_brand_ids: [77],
};

const client = {
  quote_id: 700,
  brand_id: 77,
  brand_name: '测试品牌',
  industry: '测试行业',
  keyword_count: 1,
  tier: 'standard',
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('服务商进入监测中心默认执行监测，并可按需查看数据洞察', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-intercept-only');
    const now = new Date().toISOString();
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: now,
      last_updated_at: now,
    }));
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
    if (path === '/api/wallet') {
      return json(route, {
        success: true,
        data: {
          paid_points: 0,
          commission_points: 0,
          bonus_points: 0,
          frozen_points: 0,
          total_recharged: 0,
          customer_credit_status: 'ready',
          customer_credit: {
            tool_credit_points: 1000, publish_credit_points: 0, bonus_credit_points: 0,
            total_purchased_points: 1000, total_consumed_points: 0,
          },
        },
      });
    }
    if (path === '/api/wallet/pricing') return json(route, { data: [{
      feature_code: 'monitor_single', feature_name: '单次监测', cost_points: 130,
      cost_compute: 0, requires_paid_points: true, is_active: true,
    }] });
    if (path === '/api/monitoring/clients') return json(route, { status: 'success', clients: [client] });
    if (path === '/api/monitoring/schedule/status') return json(route, { status: 'success', enabled: false });
    if (path === '/api/monitoring/clients/700/keywords') {
      return json(route, { status: 'success', keywords: [], appearance_rate: null });
    }
    if (path === '/api/geo-observation/brands/77/summary') {
      return json(route, fixture.cases.brand_summary.response);
    }
    return json(route, { status: 'success', data: {}, items: [] });
  });

  await page.goto('/monitoring?brand_id=77');

  await expect(page.getByRole('heading', { name: '统一观测' })).toHaveCount(0);
  await expect(page.getByRole('tab', { name: '监测执行' })).toHaveAttribute('aria-selected', 'true');
  await expect(page.getByRole('tab', { name: '数据洞察' })).toBeVisible();

  await page.getByRole('tab', { name: '数据洞察' }).click();

  await expect(page.getByRole('heading', { name: '统一观测' })).toBeVisible();
  await expect(page.getByRole('tab', { name: '数据洞察' })).toHaveAttribute('aria-selected', 'true');

  await page.getByRole('tab', { name: '监测执行' }).click();
  await page.getByText('定时监测', { exact: true }).first().click();
  await expect(page.getByRole('heading', { name: '定时监测设置' })).toBeVisible();
  await page.keyboard.press('Escape');
});


test('监测低频弹窗按操作加载且沙盒客户门户 spotlight 保持可达', async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'monitoring-lazy-ui');
    localStorage.setItem('omnirank_sandbox_active', '1');
    localStorage.setItem('omnirank_sandbox_intro_shown', '1');
    localStorage.setItem('omnirank_sandbox_tutorial_stage', 'step4-token-card');
    const now = new Date().toISOString();
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'sandbox',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: now,
      last_updated_at: now,
    }));
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
    if (path === '/api/agent/agreement/v35-status') return json(route, { signed: true, required: false });
    if (path === '/api/client-context/list') {
      return json(route, { success: true, clients: [{
        id: 77, name: '测试品牌', diagnosis_count: 1, quote_count: 1, created_at: new Date().toISOString(),
      }] });
    }
    if (path === '/api/client-context/77') {
      return json(route, {
        success: true,
        context: {
          brand: { id: 77, name: '测试品牌', diagnosis_count: 1 },
          profile: null,
          materials: null,
          relatedQuoteIds: ['700'],
          socialProjects: [],
        },
      });
    }
    if (path === '/api/wallet') {
      return json(route, {
        success: true,
        data: {
          paid_points: 0,
          commission_points: 0,
          bonus_points: 0,
          frozen_points: 0,
          total_recharged: 0,
          customer_credit_status: 'ready',
          customer_credit: {
            tool_credit_points: 1000, publish_credit_points: 0, bonus_credit_points: 0,
            total_purchased_points: 1000, total_consumed_points: 0,
          },
        },
      });
    }
    if (path === '/api/wallet/pricing') return json(route, { data: [{
      feature_code: 'monitor_single', feature_name: '单次监测', cost_points: 130,
      cost_compute: 0, requires_paid_points: true, is_active: true,
    }] });
    if (path === '/api/monitoring/clients') return json(route, { status: 'success', clients: [client] });
    if (path === '/api/monitoring/schedule/status' || path === '/api/monitoring/schedule') {
      return json(route, { status: 'success', enabled: false, monitoring_enabled: false });
    }
    if (path === '/api/monitoring/clients/700/keywords') {
      return json(route, { status: 'success', keywords: [], appearance_rate: null });
    }
    return json(route, { status: 'success', success: true, data: {}, items: [] });
  });

  await page.goto('/monitoring?brand_id=77');
  await expect(page.getByRole('dialog', { name: '最后一步:让客户自己看实时数据' })).toBeVisible();
  await page.getByText('客户门户', { exact: true }).first().click();
  await expect(page.getByRole('heading', { name: '客户门户设置' })).toBeVisible();
});

test('监测客户首次读取失败显示明确故障且不伪装成空客户，重试后恢复', async ({ page }) => {
  let clientCalls = 0;
  let recover = false;
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'monitoring-client-recovery');
    localStorage.setItem('omnirank_onboarding_done', 'true');
    const now = new Date().toISOString();
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: now,
      last_updated_at: now,
    }));
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
    if (path === '/api/wallet') return json(route, { success: true, data: {
      paid_points: 0, commission_points: 0, bonus_points: 0, frozen_points: 0,
      customer_credit_status: 'ready', customer_credit: { tool_credit_points: 1000 },
    } });
    if (path === '/api/wallet/pricing') return json(route, { success: true, data: [{
      feature_code: 'monitor_single', feature_name: '单次监测', cost_points: 130,
      cost_compute: 0, requires_paid_points: true, is_active: true,
    }] });
    if (path === '/api/monitoring/clients') {
      clientCalls += 1;
      if (!recover) return json(route, { detail: 'temporary failure' }, 503);
      return json(route, { status: 'success', clients: [client] });
    }
    if (path === '/api/monitoring/clients/700/keywords') return json(route, { status: 'success', keywords: [] });
    if (path === '/api/monitoring/schedule/status') return json(route, { status: 'success', enabled: false });
    return json(route, { status: 'success', data: {}, items: [] });
  });

  await page.goto('/monitoring?brand_id=77');
  await expect(page.getByRole('alert')).toContainText('客户列表更新失败');
  await expect(page.getByText('还没有监测中的客户')).toHaveCount(0);
  recover = true;
  await page.getByRole('button', { name: '重新读取' }).click();
  await expect(page.getByText('测试品牌', { exact: true })).toBeVisible();
  expect(clientCalls).toBeGreaterThanOrEqual(2);
});

for (const width of [320, 390]) {
  test(`品牌判定安全重试耗尽在 ${width}px 下如实说明且不横向溢出`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: 844 });
    await page.addInitScript(() => {
      localStorage.setItem('omnirank_token', 'local-intercept-only');
      localStorage.setItem('omnirank_monitoring_default_view_v1', 'monitoring');
      const now = new Date().toISOString();
      localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
        version: 1,
        welcome_choice: 'never',
        completed_steps: [],
        skipped_steps: [],
        dismissed_features: [],
        viewed_videos: [],
        first_seen_at: now,
        last_updated_at: now,
      }));
    });
    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/auth/me') {
        return json(route, {
          success: true,
          user: {
            ...agentUser,
            is_admin: false,
            agent_level: 0,
            roles: [{ id: 3, name: 'customer', display_name: '客户' }],
          },
        });
      }
      if (path === '/api/wallet') {
        return json(route, {
          success: true,
          data: {
            paid_points: 0,
            commission_points: 0,
            bonus_points: 0,
            frozen_points: 0,
            total_recharged: 0,
            customer_credit_status: 'ready',
            customer_credit: {
              tool_credit_points: 1000, publish_credit_points: 0, bonus_credit_points: 0,
              total_purchased_points: 1000, total_consumed_points: 0,
            },
          },
        });
      }
      if (path === '/api/wallet/pricing') return json(route, { data: [{
        feature_code: 'monitor_single', feature_name: '单次监测', cost_points: 130,
        cost_compute: 0, requires_paid_points: true, is_active: true,
      }] });
      if (path === '/api/monitoring/clients') {
        return json(route, { status: 'success', clients: [client] });
      }
      if (path === '/api/monitoring/schedule/status') {
        return json(route, { status: 'success', enabled: false });
      }
      if (path === '/api/monitoring/clients/700/keywords') {
        return json(route, {
          status: 'success',
          appearance_rate: null,
          keywords: [{
            id: 9001,
            keyword: '细胞治疗药物研发和生产隔离器推荐',
            target_brand: '浙江岱林生物技术股份有限公司',
            source: 'contract',
            lifecycle: 'monitoring',
            is_monitored: true,
            monitoring_status: 'active',
          }],
        });
      }
      if (path === '/api/monitoring/run-stream') {
        const body = [
          'data: {"type":"start","total":1}',
          '',
          'data: {"type":"detail","completed":1,"keyword":"细胞治疗药物研发和生产隔离器推荐","platform":"豆包","status":"error","error":"品牌身份重判仍未完成","error_code":"brand_identity_retry_exhausted","detected":false}',
          '',
          'data: {"type":"complete","task_id":9901}',
          '',
        ].join('\n');
        return route.fulfill({ status: 200, contentType: 'text/event-stream', body });
      }
      return json(route, { status: 'success', data: {}, items: [] });
    });

    await page.goto('/monitoring?brand_id=77');
    await expect(page.getByRole('tab', { name: '数据洞察' })).toBeVisible();

    const keywordRow = page.locator('tr').filter({ hasText: '细胞治疗药物研发和生产隔离器推荐' });
    await keywordRow.getByRole('checkbox').check();
    await page.getByRole('button', { name: /开始监测/ }).click();
    const excluded = page.getByText(/本次未计入/).first();
    await expect(excluded).toBeVisible();
    await excluded.click();
    await expect(page.getByText(/已对同一条 AI 回答重新判定/)).toBeVisible();
    await expect(page.getByText(/后续监测会继续使用已确认的品牌常用名/)).toBeVisible();

    const overflow = await page.evaluate(() => ({
      body: document.body.scrollWidth - document.body.clientWidth,
      root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(overflow).toEqual({ body: 0, root: 0 });
    await page.screenshot({ path: testInfo.outputPath(`identity-recovery-${width}.png`), fullPage: true });
  });
}

for (const width of [320, 390, 768, 1440, 2560]) {
  test(`待确认品牌名称在 ${width}px 下可跨会话恢复并完成确认`, async ({ page }) => {
    await page.setViewportSize({ width, height: width <= 390 ? 844 : 1000 });
    await page.addInitScript(() => {
      localStorage.setItem('omnirank_token', 'local-intercept-only');
      localStorage.setItem('omnirank_monitoring_default_view_v1', 'monitoring');
      const now = new Date().toISOString();
      localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
        version: 1,
        welcome_choice: 'never',
        completed_steps: [],
        skipped_steps: [],
        dismissed_features: [],
        viewed_videos: [],
        first_seen_at: now,
        last_updated_at: now,
      }));
    });

    let pending = true;
    let postedDecision: Record<string, unknown> | null = null;
    const decisionRequestIds: string[] = [];
    await page.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
      if (path === '/api/wallet') {
        return json(route, {
          success: true,
          data: {
            paid_points: 0,
            commission_points: 0,
            bonus_points: 0,
            frozen_points: 0,
            total_recharged: 0,
            customer_credit_status: 'ready',
          },
        });
      }
      if (path === '/api/monitoring/clients') return json(route, { status: 'success', clients: [client] });
      if (path === '/api/monitoring/schedule/status') return json(route, { status: 'success', enabled: false });
      if (path === '/api/monitoring/clients/700/keywords') {
        return json(route, { status: 'success', keywords: [], appearance_rate: null });
      }
      if (path === '/api/monitoring/identity-reviews' && route.request().method() === 'GET') {
        return json(route, {
          status: 'success',
          count: pending ? 1 : 0,
          items: pending ? [{
            id: 8801,
            keyword: '细胞治疗药物研发和生产隔离器推荐',
            platform: '豆包',
            response_snippet: '回答中出现了一个相近品牌名称。',
            identity_candidates: ['岱林生物'],
            identity_evidence_snippet: '回答提到岱林生物及其隔离器产品，需要确认是否为当前品牌。',
            identity_evidence_hash: 'a'.repeat(64),
            identity_decision_version: 0,
            tested_at: '2026-07-21T10:00:00Z',
          }] : [],
        });
      }
      if (path === '/api/monitoring/identity-reviews/8801/decision' && route.request().method() === 'POST') {
        postedDecision = route.request().postDataJSON();
        decisionRequestIds.push(String(postedDecision?.request_id || ''));
        if (width === 1440 && decisionRequestIds.length === 1) {
          return json(route, { detail: '临时网络错误' }, 500);
        }
        pending = false;
        return json(route, { status: 'success', data: { status: 'resolved' } });
      }
      return json(route, { status: 'success', data: {}, items: [] });
    });

    await page.goto('/monitoring?brand_id=77');
    await expect(page.getByRole('heading', { name: '需要确认的品牌名称' })).toBeVisible();
    await expect(page.getByText('岱林生物', { exact: true })).toBeVisible();

    // The pending row is server-backed rather than a one-page toast: a fresh
    // page load must recover it until a durable decision succeeds.
    await page.reload();
    await expect(page.getByRole('heading', { name: '需要确认的品牌名称' })).toBeVisible();
    await page.getByRole('button', { name: '是这个品牌' }).click();
    if (width === 1440) {
      await expect(page.getByRole('alert').filter({ hasText: '临时网络错误' })).toBeVisible();
      await page.getByRole('button', { name: '是这个品牌' }).click();
      expect(decisionRequestIds).toHaveLength(2);
      expect(decisionRequestIds[1]).toBe(decisionRequestIds[0]);
    }
    await expect(page.getByRole('heading', { name: '需要确认的品牌名称' })).toHaveCount(0);
    expect(postedDecision).toMatchObject({
      brand_id: 77,
      action: 'yes',
      selected_name: '岱林生物',
      expected_version: 0,
      evidence_hash: 'a'.repeat(64),
    });

    await page.reload();
    await expect(page.getByRole('heading', { name: '需要确认的品牌名称' })).toHaveCount(0);
    for (const term of ['brand_identity_unresolved', 'pending_identity', 'identity_review_state']) {
      await expect(page.getByText(term, { exact: false })).toHaveCount(0);
    }
    const overflow = await page.evaluate(() => ({
      body: document.body.scrollWidth - document.body.clientWidth,
      root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(overflow).toEqual({ body: 0, root: 0 });
  });
}

test('切换客户后丢弃上一客户迟到的监测任务响应', async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'monitoring-brand-race');
    localStorage.setItem('omnirank_monitoring_default_view_v1', 'monitoring');
    localStorage.setItem('omnirank_monitoring_active_task_77', '9901');
    localStorage.setItem('omnirank_monitoring_active_task_88', '9902');
    localStorage.setItem('omnirank_onboarding_done', 'true');
    const now = new Date().toISOString();
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: now,
      last_updated_at: now,
    }));
  });

  let releaseBrandA!: () => void;
  const brandAGate = new Promise<void>(resolve => { releaseBrandA = resolve; });
  const contextClients = [
    { id: 77, name: '品牌甲', industry: '制造业', diagnosis_count: 1, quote_count: 1 },
    { id: 88, name: '品牌乙', industry: '服务业', diagnosis_count: 1, quote_count: 1 },
  ];
  const monitoringClients = [
    { ...client, brand_id: 77, quote_id: 700, brand_name: '品牌甲' },
    { ...client, brand_id: 88, quote_id: 800, brand_name: '品牌乙' },
  ];
  const delayedCell = {
    id: 101, task_id: 9901, brand_id: 77, keyword_id: 1,
    keyword_source: 'confirmed', keyword_snapshot: '甲方迟到词', platform: 'dashscope',
    is_planned: true, state: 'failed', plan_hash: 'a'.repeat(64),
    fulfillment_state: 'covered', retry_coverage: 'included', retry_count: 0,
    retry_max_attempts: 1,
  };
  const currentCell = {
    id: 102, task_id: 9902, brand_id: 88, keyword_id: 2,
    keyword_source: 'confirmed', keyword_snapshot: '乙方当前词', platform: 'dashscope',
    is_planned: true, state: 'succeeded', plan_hash: 'b'.repeat(64),
    fulfillment_state: 'covered', result_id: 9002,
  };

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: { ...agentUser, client_brand_ids: [77, 88] } });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: contextClients });
    if (path === '/api/client-context/77' || path === '/api/client-context/88') {
      const id = path.endsWith('/88') ? 88 : 77;
      return json(route, { success: true, context: {
        brand: { id, name: id === 88 ? '品牌乙' : '品牌甲', diagnosis_count: 1 },
        profile: null, materials: null, relatedQuoteIds: [id === 88 ? '800' : '700'], socialProjects: [],
      } });
    }
    if (path === '/api/wallet') return json(route, { success: true, data: {
      paid_points: 0, commission_points: 0, bonus_points: 0, frozen_points: 0,
      customer_credit_status: 'ready', customer_credit: { tool_credit_points: 1000 },
    } });
    if (path === '/api/wallet/pricing') return json(route, { data: [{
      feature_code: 'monitor_single', feature_name: '单次监测', cost_points: 130,
      cost_compute: 0, requires_paid_points: true, is_active: true,
    }] });
    if (path === '/api/monitoring/clients') return json(route, { status: 'success', clients: monitoringClients });
    if (path === '/api/monitoring/schedule/status') return json(route, { status: 'success', enabled: false });
    if (path === '/api/monitoring/clients/700/keywords' || path === '/api/monitoring/clients/800/keywords') {
      return json(route, { status: 'success', keywords: [] });
    }
    if (path === '/api/monitoring/tasks/9901') {
      await brandAGate;
      return json(route, { status: 'success', task: { id: 9901, brand_id: 77 }, cells: [delayedCell] });
    }
    if (path === '/api/monitoring/tasks/9902') {
      return json(route, { status: 'success', task: { id: 9902, brand_id: 88 }, cells: [currentCell] });
    }
    return json(route, { status: 'success', success: true, data: {}, items: [] });
  });

  await page.goto('/monitoring?brand_id=77');
  await page.getByRole('button', { name: '切换客户' }).click();
  await page.getByRole('button', { name: /品牌乙/ }).click();
  await expect(page.getByTestId('monitoring-planned-matrix').getByText('乙方当前词', { exact: true })).toBeVisible();
  releaseBrandA();
  await page.waitForTimeout(100);
  await expect(page.getByText('甲方迟到词', { exact: true })).toHaveCount(0);
  await expect(page.getByText('乙方当前词', { exact: true })).toBeVisible();
});

for (const width of [320, 390, 768, 1440, 2560]) {
  test(`完整监测矩阵在 ${width}px 下可恢复且只重试失败单格`, async ({ page }) => {
    await page.setViewportSize({ width, height: width <= 390 ? 844 : 1000 });
    await page.addInitScript(() => {
      localStorage.setItem('omnirank_token', 'monitoring-cell-retry');
      localStorage.setItem('omnirank_monitoring_default_view_v1', 'monitoring');
      localStorage.setItem('omnirank_monitoring_active_task_77', '9901');
      const now = new Date().toISOString();
      localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
        version: 1,
        welcome_choice: 'never',
        completed_steps: [],
        skipped_steps: [],
        dismissed_features: [],
        viewed_videos: [],
        first_seen_at: now,
        last_updated_at: now,
      }));
    });

    const planHash = (id: number) => id.toString(16).padStart(64, '0');
    const cells = [
      { id: 1, task_id: 9901, keyword_id: 9101, keyword_source: 'confirmed', keyword_snapshot: '工业隔离器推荐', platform: 'dashscope', is_planned: true, state: 'queued', plan_hash: planHash(1), fulfillment_state: 'covered' },
      { id: 2, task_id: 9901, keyword_id: 9101, keyword_source: 'confirmed', keyword_snapshot: '工业隔离器推荐', platform: 'deepseek', is_planned: true, state: 'running', plan_hash: planHash(2), fulfillment_state: 'covered' },
      { id: 3, task_id: 9901, keyword_id: 9101, keyword_source: 'confirmed', keyword_snapshot: '工业隔离器推荐', platform: 'kimi', is_planned: true, state: 'succeeded', plan_hash: planHash(3), fulfillment_state: 'covered', result_id: 7003 },
      { id: 4, task_id: 9901, keyword_id: 9101, keyword_source: 'confirmed', keyword_snapshot: '工业隔离器推荐', platform: 'doubao', is_planned: true, state: 'failed', plan_hash: planHash(4), fulfillment_state: 'covered', retry_coverage: 'included', retry_count: 0, retry_max_attempts: 1, error_code: 'provider_error', error_message: '暂时失败' },
      { id: 5, task_id: 9901, keyword_id: 9102, keyword_source: 'confirmed', keyword_snapshot: '无菌生产设备', platform: 'dashscope', is_planned: true, state: 'pending_identity', plan_hash: planHash(5), fulfillment_state: 'covered', result_id: 7005 },
      { id: 6, task_id: 9901, keyword_id: 9102, keyword_source: 'confirmed', keyword_snapshot: '无菌生产设备', platform: 'deepseek', is_planned: false, state: 'unavailable', plan_hash: planHash(6), fulfillment_state: 'covered' },
      { id: 7, task_id: 9901, keyword_id: 9102, keyword_source: 'confirmed', keyword_snapshot: '无菌生产设备', platform: 'kimi', is_planned: true, state: 'pending_provider_confirmation', plan_hash: planHash(7), fulfillment_state: 'covered' },
      { id: 8, task_id: 9901, keyword_id: 9102, keyword_source: 'confirmed', keyword_snapshot: '无菌生产设备', platform: 'doubao', is_planned: true, state: 'succeeded', plan_hash: planHash(8), fulfillment_state: 'covered', result_id: 7008 },
    ];
    const retryBodies: Array<Record<string, unknown>> = [];

    await page.route('**/api/**', async route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
      if (path === '/api/wallet') return json(route, { success: true, data: {
        paid_points: 0, commission_points: 0, bonus_points: 0, frozen_points: 0,
        customer_credit_status: 'ready', customer_credit: { tool_credit_points: 1000 },
      } });
      if (path === '/api/wallet/pricing') return json(route, { data: [{
        feature_code: 'monitor_single', feature_name: '单次监测', cost_points: 130,
        cost_compute: 0, requires_paid_points: true, is_active: true,
      }] });
      if (path === '/api/monitoring/clients') return json(route, { status: 'success', clients: [client] });
      if (path === '/api/monitoring/schedule/status') return json(route, { status: 'success', enabled: false });
      if (path === '/api/monitoring/clients/700/keywords') return json(route, { status: 'success', keywords: [] });
      if (path === '/api/monitoring/tasks/9901' && route.request().method() === 'GET') {
        return json(route, {
          status: 'success',
          task: { id: 9901, brand_id: 77, total_tests: 7, completed_tests: 3 },
          cells,
        });
      }
      if (path === '/api/monitoring/tasks/9901/cells/4/retry' && route.request().method() === 'POST') {
        retryBodies.push(route.request().postDataJSON());
        if (retryBodies.length === 1) {
          return json(route, { detail: 'gateway lost the upstream response' }, 500);
        }
        Object.assign(cells[3], { state: 'succeeded', result_id: 7014, error_code: null, error_message: null });
        return json(route, { status: 'success', cell: cells[3], data: { state: 'succeeded' } });
      }
      return json(route, { status: 'success', data: {}, items: [] });
    });

    await page.goto('/monitoring?brand_id=77');
    const matrix = page.getByTestId('monitoring-planned-matrix');
    await expect(matrix).toBeVisible();
    await expect(matrix.getByText('工业隔离器推荐', { exact: true })).toBeVisible();
    await expect(matrix.getByText('无菌生产设备', { exact: true })).toBeVisible();
    for (const label of ['排队', '执行', '成功', '失败', '待身份确认']) {
      await expect(matrix.getByText(label, { exact: true }).first()).toBeVisible();
    }
    await expect(matrix.getByText('不可用', { exact: true })).toHaveCount(1);
    await expect(matrix.getByText('待人工核对', { exact: true })).toHaveCount(1);
    await expect(matrix.getByRole('button', { name: '重试 工业隔离器推荐 豆包' })).toBeVisible();
    await expect(matrix.getByRole('button', { name: /重试/ })).toHaveCount(1);

    await matrix.getByRole('button', { name: '重试 工业隔离器推荐 豆包' }).click();
    await expect(matrix.getByRole('button', { name: '重试 工业隔离器推荐 豆包' })).toBeVisible();
    expect(retryBodies).toHaveLength(1);
    expect(retryBodies[0]).toMatchObject({
      brand_id: 77,
      expected_plan_hash: planHash(4),
    });
    expect(String(retryBodies[0].request_id)).toMatch(/^[0-9a-f-]{36}$/i);
    await matrix.getByRole('button', { name: '重试 工业隔离器推荐 豆包' }).click();
    await expect(matrix.getByRole('button', { name: /重试/ })).toHaveCount(0);
    expect(retryBodies).toHaveLength(2);
    expect(retryBodies[1].request_id).toBe(retryBodies[0].request_id);
    expect(cells[2].result_id).toBe(7003);

    await page.reload();
    await expect(page.getByTestId('monitoring-planned-matrix')).toBeVisible();
    await expect(page.getByTestId('monitoring-planned-matrix').getByRole('button', { name: /重试/ })).toHaveCount(0);
    const overflow = await page.evaluate(() => ({
      body: document.body.scrollWidth - document.body.clientWidth,
      root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(overflow).toEqual({ body: 0, root: 0 });
  });
}
