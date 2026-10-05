import { expect, test, type Page, type Route } from './_fixtures';

const agentUser = {
  id: 102,
  username: 'identity-absence-reviewer',
  display_name: '监测运营人员',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '服务商' }],
  permissions: ['monitoring:read'],
  client_brand_ids: [77],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

type FixtureState = {
  pending: boolean;
  failNextDecision: boolean;
  decisionBodies: Array<Record<string, unknown>>;
  taskDetailCalls: number;
  keywordCalls: number;
};

type MountOptions = {
  failNextDecision?: boolean;
  // 卡片上真实存在的候选。[] = 整段没有可靠候选(原始死锁场景)。
  candidates?: string[];
  // 后端可信名守卫的回执:候选里含品牌可信名时 decision 接口返 409 + 可执行文案。
  guardedByTrustedName?: boolean;
};

async function mountMonitoring(page: Page, width: number, options: MountOptions = {}): Promise<FixtureState> {
  const {
    failNextDecision = false,
    candidates = [],
    guardedByTrustedName = false,
  } = options;
  const state: FixtureState = {
    pending: true,
    failNextDecision,
    decisionBodies: [],
    taskDetailCalls: 0,
    keywordCalls: 0,
  };

  await page.setViewportSize({ width, height: width <= 390 ? 844 : 1000 });
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-intercept-only');
    localStorage.setItem('omnirank_monitoring_default_view_v1', 'monitoring');
    localStorage.setItem('omnirank_monitoring_active_task_77', '5501');
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
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
    if (path === '/api/client-context/list') {
      return json(route, { success: true, clients: [{ id: 77, name: '测试品牌', industry: '测试行业' }] });
    }
    if (path === '/api/client-context/77') {
      return json(route, {
        success: true,
        context: {
          brand: { id: 77, name: '测试品牌' },
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
          paid_points: 1000,
          commission_points: 0,
          bonus_points: 0,
          frozen_points: 0,
          total_recharged: 0,
          customer_credit_status: 'ready',
        },
      });
    }
    if (path === '/api/monitoring/clients') {
      return json(route, {
        status: 'success',
        clients: [{
          quote_id: 700,
          brand_id: 77,
          brand_name: '测试品牌',
          industry: '测试行业',
          keyword_count: 1,
          tier: 'standard',
        }],
      });
    }
    if (path === '/api/monitoring/clients/700/keywords') {
      state.keywordCalls += 1;
      return json(route, { status: 'success', keywords: [], appearance_rate: null });
    }
    if (path === '/api/monitoring/tasks/5501') {
      state.taskDetailCalls += 1;
      return json(route, {
        status: 'success',
        task: { id: 5501, brand_id: 77, total_tests: 1, completed_tests: 1 },
        cells: [],
        results: [{
          tested_at: '2026-08-11T18:00:00Z',
          keyword: '南山区全屋定制哪家靠谱',
          platform: 'deepseek',
          identity_review_state: state.pending ? 'pending' : 'rejected',
          is_detected: false,
          response_snippet: '回答列出了多家同行，但没有出现测试品牌。',
          full_response: '回答列出了多家同行，但没有出现测试品牌。',
        }],
      });
    }
    if (path === '/api/monitoring/identity-reviews' && request.method() === 'GET') {
      return json(route, {
        status: 'success',
        count: state.pending ? 1 : 0,
        items: state.pending ? [{
          id: 9902,
          keyword: '南山区全屋定制哪家靠谱',
          platform: 'deepseek',
          response_snippet: '回答列出了同行甲、同行乙和同行丙。',
          identity_candidates: candidates,
          identity_evidence_snippet: '回答列出了同行甲、同行乙和同行丙。',
          identity_evidence_hash: 'c'.repeat(64),
          identity_decision_version: 0,
          tested_at: '2026-08-11T18:00:00Z',
        }] : [],
      });
    }
    if (path === '/api/monitoring/identity-reviews/9902/decision' && request.method() === 'POST') {
      state.decisionBodies.push(JSON.parse(request.postData() || '{}'));
      if (guardedByTrustedName) {
        // 后端守卫的真实回执(db/monitoring_db.py 的 absent 分支文案)
        return json(route, {
          detail: '候选中包含当前品牌的可信名称，不能直接记为未出现；请逐条判断该候选',
        }, 409);
      }
      if (state.failNextDecision) {
        state.failNextDecision = false;
        return json(route, { detail: '暂时保存失败，请重试' }, 500);
      }
      state.pending = false;
      return json(route, {
        status: 'success',
        data: { status: 'resolved', is_detected: false, mention_type: 'none' },
      });
    }
    return json(route, { status: 'success', success: true, data: {}, items: [] });
  });

  await page.goto('/monitoring?brand_id=77');
  return state;
}

for (const width of [390, 1440, 3840]) {
  test(`没有候选时可确认客户品牌未出现并正常计入 (${width}px)`, async ({ page }) => {
    const state = await mountMonitoring(page, width);
    const panel = page.getByTestId('identity-review-panel');
    const absent = panel.getByRole('button', { name: '没有出现', exact: true });

    await expect(panel).toBeVisible();
    await expect(absent).toBeVisible();
    await expect(panel.getByPlaceholder('填写正确的品牌名称')).toBeVisible();
    await expect(panel.getByText('直接记为未出现，本条会正常计入监测结果。')).toBeVisible();

    const taskCallsBefore = state.taskDetailCalls;
    const keywordCallsBefore = state.keywordCalls;
    await absent.click();

    await expect(panel).toHaveCount(0);
    await expect.poll(() => state.taskDetailCalls).toBeGreaterThan(taskCallsBefore);
    await expect.poll(() => state.keywordCalls).toBeGreaterThan(keywordCallsBefore);
    expect(state.decisionBodies).toHaveLength(1);
    expect(state.decisionBodies[0]).toMatchObject({
      brand_id: 77,
      action: 'no',
      selected_name: '',
      expected_version: 0,
      evidence_hash: 'c'.repeat(64),
    });
    expect(typeof state.decisionBodies[0].request_id).toBe('string');

    const overflow = await page.evaluate(() => ({
      body: document.body.scrollWidth - document.body.clientWidth,
      root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(overflow).toEqual({ body: 0, root: 0 });
  });
}

// [返修单 R1 · 2026-08-12] 候选非空时的两向对照。两条都用**同一个按钮**、同一段渲染,
// 只有后端回执不同 —— 判据打在真渲染 + 真请求体上,不扫源码写法。
for (const width of [390, 1440, 3840]) {
  test(`候选是同行名时「没有出现」仍是可用出口 (${width}px)`, async ({ page }) => {
    const state = await mountMonitoring(page, width, { candidates: ['同行甲', '同行乙'] });
    const panel = page.getByTestId('identity-review-panel');
    const absent = panel.getByRole('button', { name: '没有出现', exact: true });

    // 候选非空的卡上「都不是这些品牌」与「没有出现」并存,后者必须可点
    await expect(panel.getByRole('button', { name: '都不是这些品牌' })).toBeVisible();
    await expect(absent).toBeVisible();
    await expect(absent).toBeEnabled();

    await absent.click();
    await expect(panel).toHaveCount(0);
    expect(state.decisionBodies).toHaveLength(1);
    expect(state.decisionBodies[0]).toMatchObject({ action: 'no', selected_name: '' });

    const overflow = await page.evaluate(() => ({
      body: document.body.scrollWidth - document.body.clientWidth,
      root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(overflow).toEqual({ body: 0, root: 0 });
  });

  test(`候选含品牌可信名时被后端守卫拦下且给出可执行下一步 (${width}px)`, async ({ page }) => {
    const state = await mountMonitoring(page, width, {
      candidates: ['测试品牌'],
      guardedByTrustedName: true,
    });
    const panel = page.getByTestId('identity-review-panel');
    const absent = panel.getByRole('button', { name: '没有出现', exact: true });

    await absent.click();

    // 卡片必须留下(没被误关),错误必须告诉运营下一步怎么走 ——
    // 只回「不能整组标记为不是」时界面上并没有"整组"这个动作,运营会不知道该干嘛。
    await expect(panel).toBeVisible();
    await expect(panel.getByRole('alert')).toContainText('不能直接记为未出现');
    await expect(panel.getByRole('alert')).toContainText('请逐条判断该候选');
    await expect(absent).toBeEnabled();
    // 逐条判断的出口就在同一张卡上
    await expect(panel.getByRole('button', { name: '是这个品牌' })).toBeVisible();
    expect(state.decisionBodies).toHaveLength(1);
    expect(state.decisionBodies[0]).toMatchObject({ action: 'no', selected_name: '' });

    const overflow = await page.evaluate(() => ({
      body: document.body.scrollWidth - document.body.clientWidth,
      root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    }));
    expect(overflow).toEqual({ body: 0, root: 0 });
  });
}

test('保存失败保留卡片和重试出口，重试沿用同一请求编号', async ({ page }) => {
  const state = await mountMonitoring(page, 1440, { failNextDecision: true });
  const panel = page.getByTestId('identity-review-panel');
  const absent = panel.getByRole('button', { name: '没有出现', exact: true });

  await absent.click();
  await expect(panel).toBeVisible();
  await expect(panel.getByRole('alert')).toContainText('暂时保存失败，请重试');
  await expect(absent).toBeEnabled();

  await absent.click();
  await expect(panel).toHaveCount(0);
  expect(state.decisionBodies).toHaveLength(2);
  expect(state.decisionBodies[1]).toMatchObject({ action: 'no', selected_name: '' });
  expect(state.decisionBodies[1].request_id).toBe(state.decisionBodies[0].request_id);
});
