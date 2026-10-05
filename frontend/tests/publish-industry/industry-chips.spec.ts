/**
 * 行业筛选 chip 的**真渲染**判据。
 * WO_PUBLISH_DISPATCH_STATUS_AND_INDUSTRY_2026-08-17 Part② 判据 1 + 4。
 *
 * 判据打在 DOM 上而不是源码写法上:换个渲染写法就绕过的正则扫描证明不了
 * 「用户看到的是 20 个大类还是 243 个斜杠原文」。
 *
 * 🔴 mock 的 `/wemedia/filters` **按真实后端形状返回**(`[{key,count}]` · 已隐藏 0 家),
 *    并且会**回声当前 query**,所以「切平台重算」这条能被真判到:
 *    切了平台之后前端没带上参数重拉,回声就不会变,断言必红。
 */
import { expect, test, type Page, type Route } from 'playwright/test';

const publisherUser = {
  id: 24, user_id: 24, username: 'qa-publisher', display_name: '发布测试账号',
  is_admin: false, is_active: 1, must_change_password: 0, agent_level: 2,
  roles: [{ id: 2, name: 'geo_writer', display_name: '服务商' }],
  permissions: ['writing:read', 'writing:write', 'publish:read', 'publish:write'],
  client_brand_ids: [],
};

const wallet = {
  success: true,
  data: {
    paid_points: 100000, commission_points: 0, bonus_points: 0, frozen_points: 0,
    total_recharged: 100000, customer_credit_status: 'ready',
    customer_credit: { tool_credit_points: 0, publish_credit_points: 0, bonus_credit_points: 0, total_purchased_points: 0, total_consumed_points: 0 },
  },
};

/** 服务端 facet 的形状:key 恒为 L1 大类,count 恒 > 0(0 家的后端就不下发)。 */
const FACETS_ALL = [
  { key: '汽车', count: 33746 }, { key: '科技数码', count: 13430 },
  { key: '新闻资讯', count: 11282 }, { key: '财经投资', count: 11097 },
  { key: '法律商务', count: 204 }, { key: '家居', count: 2747 },
  { key: '母婴亲子', count: 1531 }, { key: '教育培训', count: 2145 },
  { key: '健康医疗', count: 3514 }, { key: '美食', count: 3043 },
  { key: '旅游', count: 3442 }, { key: '体育', count: 1349 },
  { key: '影视娱乐', count: 4223 }, { key: '游戏', count: 2704 },
  { key: '时尚', count: 3030 }, { key: '美妆', count: 152 },
  { key: '艺术文化', count: 1121 }, { key: '情感', count: 360 },
  { key: '萌宠', count: 35 }, { key: '三农', count: 501 },
  { key: '生活', count: 19559 }, { key: '综合', count: 16055 },
  { key: '其他', count: 7 },
];

/** 切到「微信公众号」之后的结果集:只剩三个大类有家 —— 其余必须从 DOM 消失。 */
const FACETS_WECHAT = [
  { key: '科技数码', count: 812 },
  { key: '财经投资', count: 655 },
  { key: '综合', count: 1204 },
];

async function installMocks(page: Page) {
  const seen: string[] = [];
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'pubind-test-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [], first_seen_at: '2026-08-17T00:00:00Z',
      last_updated_at: '2026-08-17T00:00:00Z',
    }));
  });

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    seen.push(url.pathname + (url.search || ''));
    if (url.pathname === '/api/auth/me') return route.fulfill({ json: { success: true, user: publisherUser } });
    if (url.pathname === '/api/wallet') return route.fulfill({ json: wallet });
    if (url.pathname === '/api/meijiehezi/wemedia/filters') {
      const platform = url.searchParams.get('platform') || '';
      return route.fulfill({
        json: {
          status: 'success',
          platforms: ['今日头条', '百家号', '微信公众号'],
          provinces: ['广东', '北京'],
          geo_platforms: [],
          industries: platform === '微信公众号' ? FACETS_WECHAT : FACETS_ALL,
        },
      });
    }
    if (url.pathname === '/api/meijiehezi/wemedia') {
      return route.fulfill({ json: { status: 'success', media: [], total: 0, page: 1, pages: 0 } });
    }
    if (url.pathname === '/api/meijiehezi/awaiting-confirmations') return route.fulfill({ json: { status: 'success', items: [] } });
    if (url.pathname === '/api/writing/projects') return route.fulfill({ json: { projects: [] } });
    if (url.pathname === '/api/publish/check-first-order') return route.fulfill({ json: { status: 'success', is_first_order: false, discount: 1 } });
    return route.fulfill({ json: { success: true, status: 'success', data: {}, items: [], profiles: [], counts: {}, stats: {}, article_ids: [] } });
  });
  return { seen, has: (frag: string) => seen.some(s => s.includes(frag)) };
}

async function openWemediaTab(page: Page) {
  await page.goto('/publish?media_type=wemedia');
  await expect(page.getByTestId('wm-industry-chip').first()).toBeVisible({ timeout: 30_000 });
}

test('行业 chips ≤ 25 个 · DOM 里没有斜杠组合原始标签 · 每个 chip 带非 0 计数', async ({ page }) => {
  await installMocks(page);
  await openWemediaTab(page);

  const chips = page.getByTestId('wm-industry-chip');
  const n = await chips.count();
  // 反向对照:分母不能是 0,否则下面三条全是空转
  expect(n).toBeGreaterThan(0);
  expect(n).toBeLessThanOrEqual(25);

  for (let i = 0; i < n; i++) {
    const chip = chips.nth(i);
    const key = await chip.getAttribute('data-industry-key');
    const count = Number(await chip.getAttribute('data-industry-count'));
    expect(key, 'chip 的 key 不该是目录原始组合串').not.toContain('/');
    expect(count, `chip「${key}」计数为 0 却被渲染`).toBeGreaterThan(0);
    // 数量必须真的写在用户看得见的文字里,不只藏在 data-* 上
    await expect(chip).toContainText(String(count));
  }

  // 病态样本正对照:目录里真实存在的这几条原文,渲染后一处都不该出现
  const html = await page.content();
  for (const raw of ['母婴亲子/亲子/教育培训/知识/健康医疗', '汽车/生活/旅行/旅游/综合/电商物流/商业', '生活/综合']) {
    expect(html, `原始组合标签漏回 UI:${raw}`).not.toContain(raw);
  }
});

test('切平台后 chip 重算 · 0 家的大类从 DOM 消失(成对:有家的仍在)', async ({ page }) => {
  await installMocks(page);
  await openWemediaTab(page);

  await expect(page.locator('[data-testid="wm-industry-chip"][data-industry-key="萌宠"]')).toHaveCount(1);
  await page.getByText('微信公众号', { exact: true }).first().click();

  // 微信公众号下 0 家的大类消失
  await expect(page.locator('[data-testid="wm-industry-chip"][data-industry-key="萌宠"]')).toHaveCount(0);
  await expect(page.locator('[data-testid="wm-industry-chip"][data-industry-key="汽车"]')).toHaveCount(0);
  // 成对:有家的仍在,且计数换成了该平台下的数(证明不是"整排消失")
  const tech = page.locator('[data-testid="wm-industry-chip"][data-industry-key="科技数码"]');
  await expect(tech).toHaveCount(1);
  await expect(tech).toHaveAttribute('data-industry-count', '812');
  await expect(page.getByTestId('wm-industry-chip')).toHaveCount(3);
});

test('切平台时 filters 请求确实带上了 platform 参数(否则上一条是靠 mock 撞对的)', async ({ page }) => {
  const calls = await installMocks(page);
  await openWemediaTab(page);
  expect(calls.has('/api/meijiehezi/wemedia/filters')).toBe(true);
  expect(calls.has('platform=%E5%BE%AE%E4%BF%A1%E5%85%AC%E4%BC%97%E5%8F%B7')).toBe(false);

  await page.getByText('微信公众号', { exact: true }).first().click();
  await expect(page.getByTestId('wm-industry-chip')).toHaveCount(3);
  expect(
    calls.seen.some(s => s.startsWith('/api/meijiehezi/wemedia/filters') && s.includes('platform=')),
    'facet 没有跟着平台重拉 —— chip 计数会停在全目录口径上说谎',
  ).toBe(true);
});
