import { expect, test, type Page, type Route } from 'playwright/test';

// [F-2 存量收敛] detail 归一装在真实 axios 实例上的判别。
//
// 事故形态:全仓约 217 处(88 文件)写 `toast.error(x?.response?.data?.detail || '失败')`。
// 后端 §13 合同 / Pydantic 422 会让 detail 变成对象或数组 —— 直接丢给 toast 渲染不出来。
// 仓里本来有一份归一,但挂在 **axios 全局默认实例** 上,而全站用的 authApi / api 都是
// axios.create() 出来的独立实例,**不继承全局拦截器** —— 那层保护一直空转。
//
// 这里不改任何调用点,只验证:装到真实实例上之后,对象/数组 detail 都能渲染成人话,
// 且原始结构仍保留在 detail_contract(供少数按 code 分支的逻辑使用)。

const user = {
  id: 151, user_id: 151, username: 'operator-normalize', display_name: '组织操作员',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 0,
  roles: [], permissions: [], client_brand_ids: [],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

type Mode = 'object_detail' | 'pydantic_array';

async function install(page: Page, mode: Mode) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'normalize-token');
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
      if (mode === 'object_detail') {
        return json(route, {
          detail: {
            code: 'ORG_CAPABILITY_DENIED',
            message: '当前员工席位未获此操作授权。',
            request_id: 'req-1',
          },
        }, 403);
      }
      // Pydantic 422:detail 是数组 —— 同样渲染不出来
      return json(route, {
        detail: [
          { loc: ['body', 'name'], msg: '客户名称不能为空', type: 'value_error' },
          { loc: ['body', 'industry'], msg: '行业不能为空', type: 'value_error' },
        ],
      }, 422);
    }
    if (path === '/api/my-clients' && method === 'GET') return json(route, { success: true, clients: [] });
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, clients: [], total: 0 });
  });
}

async function submit(page: Page) {
  await page.goto('/my-clients?add=1');
  await page.getByPlaceholder('品牌名或公司名').fill('测试客户乙');
  await page.getByRole('button', { name: '添加', exact: true }).click();
}

test('[F-2] 对象形态 detail 经归一后渲染成人话', async ({ page }) => {
  await install(page, 'object_detail');
  await submit(page);
  await expect(page.getByText('当前员工席位未获此操作授权。')).toBeVisible({ timeout: 8000 });
  await expect(page.getByText('[object Object]')).toHaveCount(0);
});

test('[F-2] Pydantic 数组 detail 经归一后逐条可读', async ({ page }) => {
  await install(page, 'pydantic_array');
  await submit(page);
  await expect(page.getByText(/客户名称不能为空/)).toBeVisible({ timeout: 8000 });
});

test('[F-2] 归一后原始结构仍保留在 detail_contract(按 code 分支的逻辑不失效)', async ({ page }) => {
  await install(page, 'object_detail');
  await page.goto('/my-clients');
  const result = await page.evaluate(async () => {
    const mod = await import('/src/lib/api.ts');
    const bag: Record<string, unknown> = {
      detail: { code: 'ORG_CAPABILITY_DENIED', message: '当前员工席位未获此操作授权。' },
    };
    (mod as { normalizeResponseDetail: (d: unknown) => void }).normalizeResponseDetail(bag);
    return {
      detail: bag.detail,
      contractCode: (bag.detail_contract as { code?: string } | undefined)?.code,
    };
  });
  expect(result.detail).toBe('当前员工席位未获此操作授权。');
  expect(result.contractCode).toBe('ORG_CAPABILITY_DENIED');
});
