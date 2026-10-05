/**
 * [工单 M-2 2026-07-28] 效果监测页闪断与反复刷新 · 第二条清空路径判别锁。
 *
 * 根因(git 实证 cff43fb6):Monitoring 页 URL 水合 effect 把"URL 没带 brand_id"
 * 当成"选择应清空",从侧栏点进裸 /monitoring 必然 switchClient(null) 清掉已选客户,
 * 随后默认重选 + 上下文异步加载让 accessScopeKey 连续翻转,整页反复清空重拉(闪断)。
 * 叠加 ClientContext 403 分支连带清 state + sessionStorage(grant 瞬时校验失败也清)。
 *
 * 修复原则(与 08b12d68 一致):选择是 UI 状态不是凭证 —— 403/权限刷新绝不清除
 * 已选客户;加载中(pending)不渲染依赖客户上下文的卡片、零身份确认拉取。
 */
import { expect, test, type Page, type Route } from './_fixtures';

const agentUser = {
  id: 102,
  username: 'm2-agent',
  display_name: '闪断测试服务商',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '服务商' }],
  permissions: ['quote:view', 'monitoring:read', 'settings:read'],
  client_brand_ids: [],
  permission_version: 1,
};

type Counters = {
  contextByBrand: Record<string, number>;
  identityReviews: number;
  monitoringClients: number;
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

async function installRoutes(
  page: Page,
  counters: Counters,
  options: { context403For?: number } = {},
) {
  await page.route('**/api/**', async route => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser });
    if (path === '/api/client-context/list') {
      return json(route, {
        success: true,
        clients: [
          { id: 77, name: '甲品牌', industry: '制造业' },
          { id: 88, name: '乙品牌', industry: '服务业' },
        ],
      });
    }
    const contextMatch = path.match(/^\/api\/client-context\/(\d+)$/);
    if (contextMatch) {
      const brandId = Number(contextMatch[1]);
      counters.contextByBrand[brandId] = (counters.contextByBrand[brandId] || 0) + 1;
      if (options.context403For === brandId) {
        return json(route, { detail: 'grant check failed' }, 403);
      }
      return json(route, {
        success: true,
        context: {
          brand: { id: brandId, name: brandId === 88 ? '乙品牌' : '甲品牌' },
          profile: null,
          materials: null,
          relatedQuoteIds: [String(brandId * 10)],
          socialProjects: [],
        },
      });
    }
    if (path === '/api/monitoring/identity-reviews') {
      counters.identityReviews += 1;
      return json(route, { items: [] });
    }
    if (path === '/api/monitoring/clients') {
      counters.monitoringClients += 1;
      return json(route, {
        status: 'success',
        clients: [
          { quote_id: 880, brand_id: 88, brand_name: '乙品牌', industry: '服务业', keyword_count: 1, tier: 'standard' },
        ],
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
    if (path.startsWith('/api/monitoring/clients/') && path.endsWith('/keywords')) {
      return json(route, { status: 'success', keywords: [], appearance_rate: null });
    }
    return json(route, { status: 'success', success: true, data: {}, items: [], records: [], total: 0 });
  });
}

async function dismissOnboarding(page: Page) {
  const experienced = page.getByRole('button', { name: /我已经用过/ });
  if (await experienced.isVisible().catch(() => false)) {
    await experienced.click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  }
}

test('从侧栏点效果监测:已选客户不清空、URL 吸附选择、无清空重拉循环', async ({ page }) => {
  const counters: Counters = { contextByBrand: {}, identityReviews: 0, monitoringClients: 0 };
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'm2-token-a');
    localStorage.setItem('omnirank_onboarding_done', 'true');
    localStorage.setItem('omnirank_screenshot_mode', '1');
  });
  await installRoutes(page, counters);

  await page.goto('/agent/pricing');
  await dismissOnboarding(page);
  await expect(page.getByRole('button', { name: '切换客户' })).toBeEnabled();
  await page.getByRole('button', { name: '切换客户' }).click();
  await page.getByText('乙品牌', { exact: true }).last().click();
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('乙品牌');

  // 真实动线:点左侧栏"效果监测"(SPA 导航,URL 不带 brand_id)
  await page.getByRole('link', { name: '效果监测' }).click();

  // ① 已选客户不丢:选择 UI 与 tab 级持久化都还在
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('乙品牌');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('omnirank_current_brand_candidate:102'))).toBe('88');

  // ② URL 吸附当前选择(裸 URL 不是"清空指令")
  await expect(page).toHaveURL(/\/monitoring\?.*brand_id=88/);

  // ③ 页面成形:直达 live,不停留在反复刷新的骨架
  await expect(page.locator('[data-access-mode="live"]')).toBeVisible();

  // ④ 无清空重拉循环:上下文/监测客户请求都是有限次(StrictMode 双跑容忍 ≤4)
  await page.waitForTimeout(1500);
  expect(counters.contextByBrand[88] || 0).toBeLessThanOrEqual(4);
  expect(counters.contextByBrand[77] || 0).toBe(0); // 绝不能被"默认第一个客户"顶掉
  expect(counters.monitoringClients).toBeLessThanOrEqual(4);

  // 选择保持稳定(无振荡)
  await page.waitForTimeout(1000);
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('乙品牌');
});

test('上下文读取 403(grant 校验失败):已选客户保持、无请求循环、骨架期零身份确认拉取', async ({ page }) => {
  const counters: Counters = { contextByBrand: {}, identityReviews: 0, monitoringClients: 0 };
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'm2-token-b');
    localStorage.setItem('omnirank_onboarding_done', 'true');
    localStorage.setItem('omnirank_screenshot_mode', '1');
    sessionStorage.setItem('omnirank_current_brand_candidate:102', '88');
  });
  await installRoutes(page, counters, { context403For: 88 });

  await page.goto('/monitoring');

  // ① 403 绝不清除已选客户:选择 UI 与持久化保持
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('乙品牌');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('omnirank_current_brand_candidate:102'))).toBe('88');

  // ② 明确故障提示 + 已停止自动重试(不是伪装成"没有客户"),且**不能**把真实客户
  //    的权限抖动说成"演示案例"(403 不再清选择后这条路径暴露度大增 · 自审补)
  const errorBox = page.getByTestId('client-access-error');
  await expect(errorBox).toBeVisible();
  await expect(errorBox.getByText(/已停止自动重试/)).toBeVisible();
  await expect(errorBox.getByText('这个客户的资料暂时读取不到')).toBeVisible();
  await expect(errorBox.getByText('演示案例暂时无法加载')).toHaveCount(0);
  await expect(errorBox.getByText(/你选中的客户没有被清空/)).toBeVisible();
  // 可重试:选择被保留 → 同步 effect 已把 brand_id 吸附进 URL → 重试按钮必然可用
  await expect(page).toHaveURL(/brand_id=88/);
  await expect(errorBox.getByRole('button', { name: /重新验证访问权限/ })).toBeVisible();

  // ③ 无请求循环:上下文 403 只试有限次(自动加载对同一品牌只试一次 · StrictMode 容忍)
  await page.waitForTimeout(2500);
  const first = counters.contextByBrand[88] || 0;
  expect(first).toBeGreaterThanOrEqual(1);
  expect(first).toBeLessThanOrEqual(4);
  await page.waitForTimeout(1500);
  expect(counters.contextByBrand[88] || 0).toBe(first); // 停住了,不是循环

  // ④ 骨架/无客户上下文阶段:依赖客户上下文的身份确认卡零拉取、零轮询
  expect(counters.identityReviews).toBe(0);

  // ⑤ 选择在等待期间始终没被清空(可手动重选恢复,选择是 UI 状态不是凭证)
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('乙品牌');
});
