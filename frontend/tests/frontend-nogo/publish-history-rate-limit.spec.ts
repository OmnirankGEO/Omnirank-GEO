import { expect, test, type Page, type Route } from './_fixtures';
import fs from 'node:fs';
import path from 'node:path';

const publisherUser = {
  id: 24,
  user_id: 24,
  username: 'qa-publisher',
  display_name: '发布测试账号',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 2,
  roles: [{ id: 2, name: 'geo_writer', display_name: '服务商' }],
  permissions: ['writing:read', 'writing:write', 'publish:read', 'publish:write'],
  client_brand_ids: [],
};

const readyWallet = {
  success: true,
  data: {
    paid_points: 1,
    commission_points: 0,
    bonus_points: 0,
    frozen_points: 0,
    total_recharged: 1,
    customer_credit_status: 'ready',
    customer_credit: { tool_credit_points: 0, publish_credit_points: 0, bonus_credit_points: 0, total_purchased_points: 0, total_consumed_points: 0 },
  },
};

const record = {
  record_key: 'publish:proxy:201',
  source: 'proxy',
  action_id: 'SYNC-201',
  order_sn: 'ORDER-201',
  article_id: 501,
  article_title: '发布后立即可见的真实记录',
  brand_id: 10,
  brand_name: '超长测试客户主体名称有限公司华东运营中心',
  channel_name: '测试发布渠道',
  media_type: 'mhz',
  status_key: 'submitted',
  status_label: '已提交，等待平台同步',
  status_family: 'in_progress',
  points: 1500,
  created_at: '2026-07-19T10:00:00Z',
  updated_at: '2026-07-19T10:00:00Z',
  can_withdraw: false,
  can_refund: false,
  can_republish: false,
};

const successPayload = {
  status: 'success',
  source: 'proxy',
  view: 'order',
  total: 1,
  page: 1,
  pages: 1,
  stats: { total: 1, completed: 0, in_progress: 1, pending: 0, rejected: 0, withdrawn: 0, refunded: 0 },
  filters: { brands: [{ id: 10, name: record.brand_name }], media_types: ['mhz'] },
  records: [record],
};

type HistoryReply = 'success' | 'empty' | '429' | '500' | 'slow';

async function installMocks(page: Page) {
  const paths: string[] = [];
  let historyReply: HistoryReply = 'success';
  let slowResolver: (() => void) | null = null;
  let activeUser = publisherUser;

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'publish-test-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [], first_seen_at: '2026-07-19T00:00:00Z', last_updated_at: '2026-07-19T00:00:00Z',
    }));
  });

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    paths.push(url.pathname);
    if (url.pathname === '/api/auth/me') return route.fulfill({ json: { success: true, user: activeUser } });
    if (url.pathname === '/api/wallet') return route.fulfill({ json: readyWallet });
    if (url.pathname === '/api/meijiehezi/publish-history') {
      if (historyReply === 'slow') {
        await new Promise<void>(resolve => { slowResolver = resolve; });
      }
      if (historyReply === '429') return route.fulfill({ status: 429, headers: { 'Retry-After': '2' }, json: { detail: 'rate limited', code: 'RATE_LIMITED' } });
      if (historyReply === '500') return route.fulfill({ status: 500, json: { detail: 'internal' } });
      if (historyReply === 'empty') return route.fulfill({ json: { ...successPayload, total: 0, pages: 0, records: [], stats: { total: 0, completed: 0, in_progress: 0, pending: 0, rejected: 0, withdrawn: 0, refunded: 0 }, filters: { brands: [], media_types: [] } } });
      if (activeUser.id === 25) {
        return route.fulfill({ json: {
          ...successPayload,
          records: [{ ...record, record_key: 'publish:proxy:202', article_title: '第二账号独立记录' }],
        } });
      }
      return route.fulfill({ json: successPayload });
    }
    if (url.pathname === '/api/meijiehezi/awaiting-confirmations') return route.fulfill({ json: { status: 'success', items: [] } });
    if (url.pathname === '/api/writing/projects') return route.fulfill({ json: { projects: [] } });
    if (url.pathname === '/api/publish/check-first-order') return route.fulfill({ json: { status: 'success', is_first_order: false, discount: 1 } });
    return route.fulfill({ json: { success: true, status: 'success', data: {}, items: [], profiles: [], counts: {} } });
  });

  return {
    paths,
    setReply(value: HistoryReply) { historyReply = value; },
    setUser(id: number) { activeUser = { ...publisherUser, id, user_id: id, username: `qa-publisher-${id}` }; },
    resolveSlow() { slowResolver?.(); slowResolver = null; },
    count(pathname: string) { return paths.filter(value => value === pathname).length; },
  };
}

test('权威身份切换立即清除上一账号快照并按新身份重读', async ({ page }) => {
  const calls = await installMocks(page);
  await page.goto('/publish/history');
  await expect(page.getByText(record.article_title)).toBeVisible();
  calls.setUser(25);

  await page.evaluate(async () => {
    const detail: { token: string; authoritativeReady?: Promise<void> } = { token: 'publish-test-token-2' };
    window.dispatchEvent(new CustomEvent('token-refreshed', { detail }));
    await detail.authoritativeReady;
  });

  await expect(page.getByText('第二账号独立记录')).toBeVisible();
  await expect(page.getByText(record.article_title)).toHaveCount(0);
});

test('记录模式严格懒加载，60 秒内 PublishCenter 无无关请求和轮询', async ({ page }) => {
  test.setTimeout(80_000);
  const calls = await installMocks(page);
  await page.goto('/publish?mode=history');
  await expect(page.getByText(record.article_title)).toBeVisible();

  const forbidden = [
    '/api/writing/projects', '/api/publish/check-first-order', '/api/publish/media/filters',
    '/api/meijiehezi/awaiting-confirmations', '/api/meijiehezi/published-articles',
    '/api/meijiehezi/rejected-articles', '/api/meijiehezi/article-publish-stats',
    '/api/meijiehezi/markup', '/api/meijiehezi/media/filters', '/api/meijiehezi/wemedia/filters',
  ];
  for (const endpoint of forbidden) expect(calls.count(endpoint), endpoint).toBe(0);

  const initialHistoryCalls = calls.count('/api/meijiehezi/publish-history');
  expect(initialHistoryCalls).toBeGreaterThanOrEqual(1);
  expect(initialHistoryCalls).toBeLessThanOrEqual(2);
  await page.waitForTimeout(60_000);
  expect(calls.count('/api/meijiehezi/publish-history')).toBe(initialHistoryCalls);
  for (const endpoint of forbidden) expect(calls.count(endpoint), endpoint).toBe(0);
});

test('隐藏、切到记录模式和卸载后待确认轮询归零且慢轮询不重叠', async ({ page }) => {
  test.setTimeout(35_000);
  const calls = await installMocks(page);
  await page.goto('/publish');
  await expect.poll(() => calls.count('/api/meijiehezi/awaiting-confirmations')).toBeGreaterThanOrEqual(1);

  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => true });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  const hiddenCount = calls.count('/api/meijiehezi/awaiting-confirmations');
  await page.waitForTimeout(5_500);
  expect(calls.count('/api/meijiehezi/awaiting-confirmations')).toBe(hiddenCount);

  await page.evaluate(() => {
    Object.defineProperty(document, 'hidden', { configurable: true, get: () => false });
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await expect.poll(() => calls.count('/api/meijiehezi/awaiting-confirmations')).toBeGreaterThan(hiddenCount);
  await page.goto('/publish?mode=history');
  await expect(page.getByText(record.article_title)).toBeVisible();
  const historyModeCount = calls.count('/api/meijiehezi/awaiting-confirmations');
  await page.waitForTimeout(5_500);
  expect(calls.count('/api/meijiehezi/awaiting-confirmations')).toBe(historyModeCount);

  await page.goto('/');
  const unmountedCount = calls.count('/api/meijiehezi/awaiting-confirmations');
  await page.waitForTimeout(5_500);
  expect(calls.count('/api/meijiehezi/awaiting-confirmations')).toBe(unmountedCount);
});

// [WO_273 · 2026-09-23] 原格「发布结果轮询在两秒等待期间切到隐藏态不会多发最后一次 GET」肯定式退役:
//   它测的是浏览器插件自助发布的 publish-result 轮询;自助发布整档退役、那组接口由后端同单删除,
//   被测物已不存在。接替者 = scripts/test-self-publish-retired.mjs(S2:frontend/src 一处 /api/extension 都没有)
//   + scripts/test-self-publish-retired-render.mjs(N 段:全程零 /api/extension 请求)—— 没有轮询可以多发了。

test('慢记录请求单飞，连续点击不会启动第二个请求', async ({ page }) => {
  const calls = await installMocks(page);
  await page.goto('/publish/history');
  await expect(page.getByText(record.article_title)).toBeVisible();
  calls.setReply('slow');

  const before = calls.count('/api/meijiehezi/publish-history');
  await page.getByTestId('publish-history-refresh').click();
  await expect(page.getByTestId('publish-history-refresh')).toBeDisabled();
  await page.getByTestId('publish-history-refresh').click({ force: true });
  await page.waitForTimeout(500);
  expect(calls.count('/api/meijiehezi/publish-history')).toBe(before + 1);
  calls.resolveSlow();
  await expect(page.getByTestId('publish-history-refresh')).toBeEnabled();
});

test('429 遵守 Retry-After 并保留上次记录、统计和筛选', async ({ page }) => {
  const calls = await installMocks(page);
  await page.goto('/publish/history');
  await expect(page.getByText(record.article_title)).toBeVisible();
  await expect(page.getByRole('option', { name: record.brand_name })).toHaveCount(1);
  calls.setReply('429');

  const before = calls.count('/api/meijiehezi/publish-history');
  await page.getByTestId('publish-history-refresh').click();
  await expect(page.getByRole('alert')).toContainText('暂时无法更新，当前显示上次结果');
  await expect(page.getByText(record.article_title)).toBeVisible();
  await expect(page.getByTestId('history-stat-总记录')).toHaveText('1');
  await expect(page.getByTestId('publish-history-refresh')).toBeDisabled();
  await page.getByTestId('publish-history-refresh').click({ force: true });
  expect(calls.count('/api/meijiehezi/publish-history')).toBe(before + 1);
  await expect(page.getByTestId('publish-history-refresh')).toBeEnabled({ timeout: 4_000 });
});

test('切换筛选后失败不会把上一查询记录伪装成新结果', async ({ page }) => {
  const calls = await installMocks(page);
  await page.goto('/publish/history');
  await expect(page.getByText(record.article_title)).toBeVisible();
  calls.setReply('429');

  await page.getByRole('button', { name: '已完成', exact: true }).click();
  await expect(page.getByRole('alert')).toContainText('暂时无法读取发布记录');
  await expect(page.getByText(record.article_title)).toHaveCount(0);
  await expect(page.getByTestId('history-stat-总记录')).toHaveText('—');
  await expect(page.getByRole('button', { name: '已完成', exact: true })).toBeDisabled();
  await expect(page.getByPlaceholder('搜索内容或发布渠道')).toBeDisabled();
});

test('真实空数据显示零，首次 5xx 不显示假零或裸错误', async ({ page }) => {
  const calls = await installMocks(page);
  calls.setReply('empty');
  await page.goto('/publish/history');
  await expect(page.getByTestId('history-stat-总记录')).toHaveText('0');
  await expect(page.getByText('暂无发布记录')).toBeVisible();

  const secondPage = await page.context().newPage();
  const secondCalls = await installMocks(secondPage);
  secondCalls.setReply('500');
  await secondPage.goto('/publish/history');
  await expect(secondPage.getByRole('alert')).toContainText('暂时无法读取发布记录');
  await expect(secondPage.getByTestId('history-stat-总记录')).toHaveText('—');
  await expect(secondPage.getByText('暂无发布记录')).toHaveCount(0);
  await expect(secondPage.getByText(/HTTP 500|500 Internal|internal/i)).toHaveCount(0);
});

for (const viewport of [
  { width: 320, height: 760 },
  { width: 390, height: 844 },
  { width: 768, height: 900 },
  { width: 1440, height: 900 },
]) {
  test(`${viewport.width}px 发布记录无横向溢出并保存验收截图`, async ({ page }) => {
    await page.setViewportSize(viewport);
    await installMocks(page);
    await page.goto('/publish/history');
    await expect(page.getByText(record.article_title)).toBeVisible();
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);
    await expect(page.getByTestId('publish-history-refresh')).toBeVisible();

    const outputDir = path.resolve(process.cwd(), 'output/playwright');
    fs.mkdirSync(outputDir, { recursive: true });
    await page.screenshot({ path: path.join(outputDir, `publish-history-${viewport.width}.png`), fullPage: true });
  });
}
