/**
 * [工单 M-1 ①③④⑤ 2026-07-28] 身份确认卡真渲染判别锁 + 截图证据。
 * ⑤ 容器错版:border-y 全宽段落补横向内边距,内容不得顶边、页面不得横向溢出;
 * ③ 候选称呼上下文片段高亮定位可见;
 * ④ 文案:仅 N 条待确认、其余已正常计入;
 * ① 确认成功后回写:面板消失 + 任务详情被重新拉取(回写链真的跑了)。
 */
import { expect, test, type Route } from './_fixtures';

const agentUser = {
  id: 102,
  username: 'm1-layout',
  display_name: '布局验证服务商',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '服务商' }],
  permissions: ['monitoring:read'],
  client_brand_ids: [77],
};

const EVIDENCE = '在国产隔离器厂商中，泰某生物的隔离器产品线覆盖细胞治疗与无菌制剂两类场景，交付周期约 45 天。';

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('身份确认卡:容器成形、候选上下文高亮可见、文案不暗示整轮作废、确认后回写(截图证据)', async ({ page }, testInfo) => {
  let pending = true;
  let taskDetailCalls = 0;
  let keywordCalls = 0;

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-intercept-only');
    localStorage.setItem('omnirank_monitoring_default_view_v1', 'monitoring');
    localStorage.setItem('omnirank_monitoring_active_task_77', '5501');
    const now = new Date().toISOString();
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [], first_seen_at: now, last_updated_at: now,
    }));
  });

  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
    if (path === '/api/client-context/list') {
      return json(route, { success: true, clients: [{ id: 77, name: '测试品牌', industry: '测试行业' }] });
    }
    if (path === '/api/client-context/77') {
      return json(route, {
        success: true,
        context: {
          brand: { id: 77, name: '测试品牌' }, profile: null, materials: null,
          relatedQuoteIds: ['700'], socialProjects: [],
        },
      });
    }
    if (path === '/api/wallet') {
      return json(route, {
        success: true,
        data: {
          paid_points: 1000, commission_points: 0, bonus_points: 0, frozen_points: 0,
          total_recharged: 0, customer_credit_status: 'ready',
        },
      });
    }
    if (path === '/api/monitoring/clients') {
      return json(route, {
        status: 'success',
        clients: [{ quote_id: 700, brand_id: 77, brand_name: '测试品牌', industry: '测试行业', keyword_count: 1, tier: 'standard' }],
      });
    }
    if (path === '/api/monitoring/clients/700/keywords') {
      keywordCalls += 1;
      return json(route, { status: 'success', keywords: [], appearance_rate: null });
    }
    if (path === '/api/monitoring/tasks/5501') {
      taskDetailCalls += 1;
      return json(route, {
        status: 'success',
        task: { id: 5501, brand_id: 77, total_tests: 8, completed_tests: 8 },
        cells: [],
        results: [
          {
            tested_at: '2026-07-28T02:00:00Z', keyword: '隔离器推荐', platform: 'doubao',
            identity_review_state: pending ? 'pending' : 'confirmed',
            is_detected: !pending, response_snippet: EVIDENCE, full_response: EVIDENCE,
          },
          {
            tested_at: '2026-07-28T02:01:00Z', keyword: '无菌制剂', platform: 'kimi',
            identity_review_state: 'not_required', is_detected: true,
            response_snippet: '另一条已正常计入。', full_response: '另一条已正常计入。',
          },
        ],
      });
    }
    if (path === '/api/monitoring/identity-reviews' && route.request().method() === 'GET') {
      return json(route, {
        status: 'success',
        count: pending ? 1 : 0,
        items: pending ? [{
          id: 9901,
          keyword: '细胞治疗药物研发和生产隔离器推荐',
          platform: '豆包',
          response_snippet: EVIDENCE,
          identity_candidates: ['泰某生物'],
          identity_evidence_snippet: EVIDENCE,
          identity_evidence_hash: 'b'.repeat(64),
          identity_decision_version: 0,
          tested_at: '2026-07-28T02:00:00Z',
        }] : [],
      });
    }
    if (path === '/api/monitoring/identity-reviews/9901/decision' && route.request().method() === 'POST') {
      pending = false;
      return json(route, { status: 'success', data: { status: 'resolved', is_detected: true, mention_type: 'mentioned' } });
    }
    return json(route, { status: 'success', success: true, data: {}, items: [] });
  });

  await page.goto('/monitoring?brand_id=77');

  const panel = page.getByTestId('identity-review-panel');
  await expect(panel).toBeVisible();

  // ④ 文案:仅 1 条待确认、其余已正常计入(不暗示整轮作废)
  await expect(panel.getByText(/仅这 1 条待确认/)).toBeVisible();
  await expect(panel.getByText(/其余监测结果已正常计入/)).toBeVisible();
  await expect(panel.getByText('确认后才会计入监测结果，其他平台结果不受影响。')).toHaveCount(0);

  // ③ 候选上下文片段可见且高亮命中该称呼
  const context = panel.getByTestId('candidate-context').first();
  await expect(context).toBeVisible();
  await expect(context.locator('mark')).toContainText('泰某生物');
  await expect(context).toContainText('隔离器产品线');   // 上下文确实带出了原文周边

  // ⑤ 容器成形:横向内边距生效(内容不顶边)+ 页面无横向溢出
  const padding = await panel.evaluate(el => getComputedStyle(el).paddingLeft);
  expect(parseFloat(padding)).toBeGreaterThan(0);
  const overflow = await page.evaluate(() => ({
    body: document.body.scrollWidth - document.body.clientWidth,
    root: document.documentElement.scrollWidth - document.documentElement.clientWidth,
  }));
  expect(overflow).toEqual({ body: 0, root: 0 });

  await testInfo.attach('identity-panel-before-decision', {
    body: await page.screenshot({ fullPage: true }),
    contentType: 'image/png',
  });

  // ① 确认后回写:面板消失 + 任务详情被重新拉取 + 关键词统计刷新
  const detailBefore = taskDetailCalls;
  const keywordBefore = keywordCalls;
  await panel.getByRole('button', { name: '是这个品牌' }).click();
  await expect(page.getByTestId('identity-review-panel')).toHaveCount(0);
  await expect.poll(() => taskDetailCalls).toBeGreaterThan(detailBefore);
  await expect.poll(() => keywordCalls).toBeGreaterThan(keywordBefore);

  await testInfo.attach('identity-panel-after-decision', {
    body: await page.screenshot({ fullPage: true }),
    contentType: 'image/png',
  });
});
