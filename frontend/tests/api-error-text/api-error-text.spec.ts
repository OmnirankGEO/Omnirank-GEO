import { expect, test, type Page, type Route } from 'playwright/test';

// [F-2] 静默失败 + dict detail 渲染不出 的判别。
//
// 事故形态:后端返回 §13 合同对象作为 detail(如 UPGRADE_REQUIRED 结构化 payload),
// 前端 `toast.error(e.response.data.detail || '失败')` 把**对象**丢给 toast → 渲染不出来;
// 再加上 success=false 分支根本没有 else → 用户点了完全没反应。
// 这里在真实页面上跑:两种形态都必须给出**可见的人话反馈**。

const user = {
  id: 151, user_id: 151, username: 'operator-f2', display_name: '组织操作员',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 0,
  roles: [], permissions: [], client_brand_ids: [],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function install(page: Page, mode: 'dict_detail_402' | 'success_false') {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'f2-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'returning', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-07-21T00:00:00Z', last_updated_at: '2026-07-21T00:00:00Z',
    }));
  });
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    const method = route.request().method();
    if (path === '/api/auth/me') return json(route, { success: true, user });
    if (path === '/api/my-clients' && method === 'POST') {
      if (mode === 'dict_detail_402') {
        // 结构化 §13 合同作为 detail —— 旧代码会把对象丢给 toast,渲染不出
        return json(route, {
          detail: {
            code: 'UPGRADE_REQUIRED',
            message: '当前账号最多只能管理 1 个客户。',
            reason: '普通账号的客户额度为 1 个。',
            impact: '本次没有创建新客户。',
            repair_hint: '升级为服务商后可管理多个客户。',
            actions: [{ id: 'upgrade', label: '了解服务商', type: 'nav' }],
          },
        }, 402);
      }
      // success=false 且 HTTP 200 —— 旧代码这里**完全没有 else**,静默无反馈
      return json(route, { success: false, detail: '客户名称与已有客户重复' });
    }
    if (path === '/api/my-clients' && method === 'GET') return json(route, { success: true, clients: [] });
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, clients: [], total: 0 });
  });
}

async function submitNewClient(page: Page) {
  // 页面支持 ?add=1 直接打开"添加客户"弹窗(原 M3 快捷操作条联动用;那一端随开源 E3 删,参数本身仍在役)
  await page.goto('/my-clients?add=1');
  await page.getByPlaceholder('品牌名或公司名').fill('测试客户甲');
  await page.getByRole('button', { name: '添加', exact: true }).click();
}

test('[F-2] 结构化 detail(§13 合同对象)必须渲染成人话,不能是空白/[object Object]', async ({ page }) => {
  await install(page, 'dict_detail_402');
  await submitNewClient(page);
  // 必须出现合同里的 message,而不是 [object Object]
  await expect(page.getByText('当前账号最多只能管理 1 个客户。')).toBeVisible({ timeout: 8000 });
  await expect(page.getByText('[object Object]')).toHaveCount(0);
});

test('[F-2] success=false 必须给可见反馈,不能静默"点了没用"', async ({ page }) => {
  await install(page, 'success_false');
  await submitNewClient(page);
  await expect(page.getByText('客户名称与已有客户重复')).toBeVisible({ timeout: 8000 });
});
