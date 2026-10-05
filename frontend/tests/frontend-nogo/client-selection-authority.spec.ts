import { expect, test, type Page, type Route } from './_fixtures';

type SessionState = {
  userId: number;
  token: string;
  clients: Array<{ id: number; name: string; industry: string }>;
  holdNextList: boolean;
  releaseList: (() => void) | null;
};

const agent = (id: number) => ({
  id,
  username: `selection-agent-${id}`,
  display_name: `选择测试-${id}`,
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [{ id: 2, name: 'geo_agent', display_name: '代理' }],
  permissions: ['quote:view', 'settings:read'],
  client_brand_ids: [],
  permission_version: 1,
});

async function installSession(page: Page, state: SessionState) {
  await page.addInitScript(() => {
    if (!localStorage.getItem('omnirank_token')) localStorage.setItem('omnirank_token', 'selection-token-a');
    localStorage.setItem('omnirank_onboarding_done', 'true');
    localStorage.setItem('omnirank_screenshot_mode', '1');
  });
  await page.route('**/api/**', async (route: Route) => {
    const path = new URL(route.request().url()).pathname;
    if (path === '/api/auth/me') {
      await route.fulfill({ json: { success: true, user: agent(state.userId) } });
      return;
    }
    if (path === '/api/client-context/list') {
      if (state.holdNextList) {
        state.holdNextList = false;
        await new Promise<void>(resolve => { state.releaseList = resolve; });
        state.releaseList = null;
      }
      await route.fulfill({ json: { success: true, clients: state.clients } });
      return;
    }
    const contextMatch = path.match(/^\/api\/client-context\/(\d+)$/);
    if (contextMatch) {
      const brandId = Number(contextMatch[1]);
      const brand = state.clients.find(client => client.id === brandId);
      if (!brand) {
        await route.fulfill({ status: 403, json: { detail: 'revoked' } });
        return;
      }
      await route.fulfill({ json: {
        success: true,
        context: { brand, profile: null, materials: null, relatedQuoteIds: [], socialProjects: [] },
      } });
      return;
    }
    if (path === '/api/wallet') {
      await route.fulfill({ json: { success: true, data: { paid_points: 1000, bonus_points: 0, commission_points: 0, frozen_points: 0, customer_credit_status: 'ready' } } });
      return;
    }
    await route.fulfill({ json: { success: true, data: {}, items: [], records: [], total: 0 } });
  });
}

async function chooseClient(page: Page, name: string) {
  const experienced = page.getByRole('button', { name: /我已经用过/ });
  if (await experienced.isVisible().catch(() => false)) {
    await experienced.click();
    await expect(page.getByRole('dialog')).toHaveCount(0);
  }
  await expect(page.getByRole('button', { name: '切换客户' })).toBeEnabled();
  await page.getByRole('button', { name: '切换客户' }).click();
  await page.getByText(name, { exact: true }).last().click();
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText(name);
}

test('same-user reload restores a non-sensitive brand id only after the new authority list validates it', async ({ page }) => {
  const state: SessionState = {
    userId: 102,
    token: 'selection-token-a',
    clients: [
      { id: 101, name: '客户甲', industry: '制造业' },
      { id: 102, name: '客户乙', industry: '服务业' },
    ],
    holdNextList: false,
    releaseList: null,
  };
  await installSession(page, state);
  await page.goto('/agent/pricing');
  await chooseClient(page, '客户乙');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('omnirank_current_brand_candidate:102'))).toBe('102');

  state.holdNextList = true;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(() => state.releaseList !== null).toBe(true);
  await expect(page.getByRole('button', { name: '切换客户' })).not.toContainText('客户乙');
  state.releaseList?.();
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('客户乙');
});

test('same-user permission refresh removes a revoked persisted selection without exposing its name', async ({ page }) => {
  const state: SessionState = {
    userId: 102,
    token: 'selection-token-a',
    clients: [
      { id: 101, name: '保留客户', industry: '制造业' },
      { id: 102, name: '已撤权客户', industry: '服务业' },
    ],
    holdNextList: false,
    releaseList: null,
  };
  await installSession(page, state);
  await page.goto('/agent/pricing');
  await chooseClient(page, '已撤权客户');

  state.clients = [{ id: 101, name: '保留客户', industry: '制造业' }];
  state.holdNextList = true;
  await page.reload({ waitUntil: 'domcontentloaded' });
  await expect.poll(() => state.releaseList !== null).toBe(true);
  await expect(page.getByText('已撤权客户', { exact: true })).toHaveCount(0);
  state.releaseList?.();
  await expect(page.getByText('已撤权客户', { exact: true })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('omnirank_current_brand_candidate:102'))).toBe('101');
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('保留客户');
});

test('cross-user token adoption immediately clears the previous user selection and candidate key', async ({ page }) => {
  const state: SessionState = {
    userId: 102,
    token: 'selection-token-a',
    clients: [
      { id: 101, name: 'A 客户', industry: '制造业' },
      { id: 102, name: 'A 私有客户', industry: '服务业' },
    ],
    holdNextList: false,
    releaseList: null,
  };
  await installSession(page, state);
  await page.goto('/agent/pricing');
  await chooseClient(page, 'A 私有客户');

  state.userId = 203;
  state.token = 'selection-token-b';
  state.clients = [{ id: 203, name: 'B 客户', industry: '科技业' }];
  await page.evaluate(() => {
    const oldValue = localStorage.getItem('omnirank_token');
    localStorage.setItem('omnirank_token', 'selection-token-b');
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'omnirank_token',
      oldValue,
      newValue: 'selection-token-b',
      storageArea: localStorage,
    }));
  });

  await expect(page.getByText('A 私有客户', { exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: '切换客户' })).toContainText('B 客户');
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('omnirank_current_brand_candidate:102'))).toBeNull();
  expect(await page.evaluate(() => Object.keys(sessionStorage).filter(key => key.startsWith('omnirank_current_brand_candidate:')))).toEqual(['omnirank_current_brand_candidate:203']);
});

test('cross-tab logout clears the persisted brand candidate and sensitive client DOM immediately', async ({ browser }) => {
  const context = await browser.newContext();
  const state: SessionState = {
    userId: 102,
    token: 'selection-token-a',
    clients: [{ id: 102, name: '登出前私有客户', industry: '服务业' }],
    holdNextList: false,
    releaseList: null,
  };
  const page = await context.newPage();
  await installSession(page, state);
  await page.goto('/agent/pricing');
  await chooseClient(page, '登出前私有客户');

  const logoutPage = await context.newPage();
  await installSession(logoutPage, state);
  await logoutPage.goto('/login');
  await logoutPage.evaluate(() => localStorage.removeItem('omnirank_token'));

  await expect(page.getByText('登出前私有客户', { exact: true })).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => sessionStorage.getItem('omnirank_current_brand_candidate:102'))).toBeNull();
  await context.close();
});
