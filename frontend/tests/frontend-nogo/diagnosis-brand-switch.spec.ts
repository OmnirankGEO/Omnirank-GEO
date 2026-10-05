import { expect, test, type Page, type Route } from './_fixtures';


async function installSession(page: Page) {
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'brand-switch-test'));
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({
        json: {
          success: true,
          user: {
            id: 101,
            username: 'qa-admin',
            display_name: '管理员',
            is_admin: true,
            agent_level: 0,
            roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
            permissions: ['settings:write', 'users:write'],
          },
        },
      });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({
        json: {
          success: true,
          data: {
            paid_points: 100000,
            commission_points: 0,
            bonus_points: 0,
            frozen_points: 0,
            customer_credit_status: 'ready',
          },
        },
      });
      return;
    }
    if (path === '/api/my-clients') {
      await route.fulfill({
        json: {
          success: true,
          clients: [
            { id: 7001, name: '慢品牌 A', industry: '制造业', owner_name: '测试 A' },
            { id: 7002, name: '快品牌 B', industry: '科技服务', owner_name: '测试 B' },
          ],
        },
      });
      return;
    }
    if (path === '/api/brands/7001/latest-diagnosis-params') {
      await new Promise(resolve => setTimeout(resolve, 600));
      await route.fulfill({
        json: {
          has_diagnosis: true,
          industry: '制造业',
          brand_display_names: ['A 的旧别名'],
          keywords: ['A 关键词'],
        },
      }).catch(() => undefined);
      return;
    }
    if (path === '/api/brands/7002/latest-diagnosis-params') {
      await route.fulfill({
        json: {
          has_diagnosis: true,
          industry: '科技服务',
          brand_display_names: ['B 的正确别名'],
          keywords: ['B 关键词'],
        },
      });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [], total: 0 } });
  });
}


test('快速切换品牌时迟到响应不得把 A 的别名写进 B', async ({ page }) => {
  await installSession(page);
  await page.goto('/diagnosis/new');

  const input = page.getByPlaceholder('输入品牌名搜索或创建新品牌');
  await expect(input).toBeVisible();
  await input.focus();
  await page.getByRole('button', { name: '选择品牌 慢品牌 A' }).click();

  await input.fill('');
  await page.getByRole('button', { name: '选择品牌 快品牌 B' }).click();

  await page.getByText('高级选项', { exact: true }).click();
  const aliases = page.locator('#brandDisplayNames');
  await expect(input).toHaveValue('快品牌 B');
  await expect(aliases).toHaveValue('B 的正确别名');
  await page.waitForTimeout(800);
  await expect(input).toHaveValue('快品牌 B');
  await expect(aliases).toHaveValue('B 的正确别名');
  await expect(aliases).not.toHaveValue(/A 的旧别名/);
});
