import { expect, test, type Page } from 'playwright/test';

// [R2-5 2026-08-15] 「下一步」CTA 真渲染判据。
// 返修单 §0② 反例:CTA 曾是带箭头的死 <span>(无 onClick / 无 Link / 无 button)。
// 本 spec 打的是**真渲染 + 真点击 + 真跳转**:publish 档 CTA 点击后 URL 必须
// 真的切到 /publish?quote_id=…。「元素存在」不算判据,「可点且有效」才算。

const agentUser = {
  id: 102,
  username: 'qa-agent',
  display_name: '服务商',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '代理' }],
  permissions: ['quote:write', 'settings:read', 'writing:read', 'writing:write'],
  client_brand_ids: [100],
};

const project = {
  id: 501,
  quote_ids: [501],
  brand_id: 100,
  brand_name: '演练品牌',
  industry: '家居定制',
  keyword_count: 2,
  total_required_articles: 6,
  monthly_price: 0,
  writing_status: 'writing',
  confirmed_at: '2026-08-01T00:00:00Z',
};

async function installRoutes(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-intercept-only');
    // 关掉新手引导弹窗:走 LEGACY_KEYS 老用户判定(schema 校验最稳的路径)
    localStorage.setItem('onboarding_state', JSON.stringify({ social_step: 3, geo_step: 2 }));
    localStorage.setItem('omnirank_current_brand_id', '100');
  });
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: agentUser } });
      return;
    }
    if (path === '/api/auth/refresh') {
      await route.fulfill({ json: { success: true, token: 'local-refreshed-only', user: agentUser } });
      return;
    }
    if (path === '/api/writing/projects') {
      await route.fulfill({ json: { success: true, projects: [project] } });
      return;
    }
    if (path === '/api/client-context/list') {
      await route.fulfill({ json: { success: true, clients: [
        { id: 100, brand_name: '演练品牌', industry: '家居定制' },
      ] } });
      return;
    }
    if (path === '/api/client-context/100') {
      await route.fulfill({ json: { success: true, client: {
        id: 100, brand_name: '演练品牌', industry: '家居定制',
      } } });
      return;
    }
    if (path === '/api/writing/projects/501/next-step') {
      await route.fulfill({ json: {
        success: true,
        available: true,
        version: 'writing-next-step-v1.0',
        facts: { eligible_unpublished: 3, advisory_open: 2 },
        next_step: {
          stage: 'publish',
          title: '去发布',
          why: '有 3 篇可发布、还没发布。(另有 2 篇带可优化提示,不影响发布,可稍后逐条处理或忽略)',
          cta: '去发布中心',
        },
      } });
      return;
    }
    if (path === '/api/writing/projects/501') {
      await route.fulfill({ json: {
        success: true,
        quote: { id: 501, brand_id: 100, industry: '家居定制', target_engine: null },
        keywords: [],
        topics: [],
      } });
      return;
    }
    if (path === '/api/writing/closed-loop/projects/501/summary') {
      await route.fulfill({ json: { enabled: false, available: false } });
      return;
    }
    // 其余接口一律温和空响应:本 spec 只判 CTA 行为,不摆全站数据。
    await route.fulfill({ json: { success: true, data: {}, projects: [], clients: [], items: [] } });
  });
}

test('publish 档 CTA 可点且真的跳转到发布中心', async ({ page }) => {
  await installRoutes(page);
  await page.goto('/writing?quote_id=501');

  const card = page.getByTestId('next-step-card');
  await expect(card).toBeVisible({ timeout: 20_000 });
  await expect(card).toContainText('去发布');

  const cta = page.getByTestId('next-step-cta');
  await expect(cta).toBeVisible();
  // 「可点」的硬判据:是 button 元素(role=button),不是死文本
  expect(await cta.evaluate(el => el.tagName.toLowerCase())).toBe('button');

  await cta.click();
  // 「有效」的硬判据:点击后发生真实导航(URL 切到发布中心并携带 quote_id)
  await expect(page).toHaveURL(/\/publish\?quote_id=501/, { timeout: 15_000 });
});

test('反向对照:next-step 不可用时卡片与 CTA 都不渲染(不给点了没反应的东西)', async ({ page }) => {
  await installRoutes(page);
  await page.route('**/api/writing/projects/501/next-step', async route => {
    await route.fulfill({ json: { success: true, available: false } });
  });
  await page.goto('/writing?quote_id=501');
  // 等详情面板出现(项目已选中)再断言卡片缺席
  await expect(page.getByText('演练品牌').first()).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId('next-step-card')).toHaveCount(0);
});
