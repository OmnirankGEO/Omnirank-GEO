import { expect, test, type Route } from 'playwright/test';

const admin = {
  id: 1, user_id: 1, username: 'platform-admin', display_name: '平台管理员',
  is_admin: true, is_active: 1, must_change_password: 0, agent_level: 0,
  roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
  permissions: ['users:read', 'users:write'], client_brand_ids: [],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
}

test('管理员审阅并以 CAS 更新默认零价基础席位，迟到响应不覆盖新页面', async ({ page }) => {
  const writes: Array<Record<string, unknown>> = [];
  await page.addInitScript(() => localStorage.setItem('omnirank_token', 'admin-session'));
  await page.route('**/api/**', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === '/api/auth/me') return json(route, { success: true, user: admin });
    if (path === '/api/admin/organization-product-config' && request.method() === 'GET') {
      return json(route, {
        publication_version: 1, version_code: 'organization-basic-team-v1',
        included_seats: 8, extra_seat_price_cents: 0,
        paid_extra_seats_enabled: false, operational: true,
        invite_ttl_hours: 72, verification_ttl_minutes: 10,
        verification_max_attempts: 5, config_hash: 'a'.repeat(64),
        created_at: '2026-07-22T00:00:00Z',
      });
    }
    if (path === '/api/admin/organization-product-config/publish') {
      writes.push(request.postDataJSON() as Record<string, unknown>);
      return json(route, {
        publication_version: 2, version_code: 'organization-seats-v2',
        included_seats: 3, extra_seat_price_cents: 0,
        paid_extra_seats_enabled: false, operational: true,
        invite_ttl_hours: 72, verification_ttl_minutes: 10,
        verification_max_attempts: 5, config_hash: 'b'.repeat(64),
        created_at: '2026-07-22T01:00:00Z',
      });
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 });
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] });
    return json(route, { success: true, items: [], data: {}, total: 0 });
  });

  await page.goto('/admin/organization-product-config');
  await expect(page.getByText('已发布可用基础席位')).toBeVisible();
  await page.getByLabel('每个团队的基础员工席位').fill('3');
  await page.getByLabel('发布原因').fill('Owner 批准三个免费基础员工席位');
  await page.getByRole('button', { name: '发布基础席位策略' }).click();
  await expect(page.getByText('已发布可用基础席位')).toBeVisible();
  expect(writes).toHaveLength(1);
  expect(writes[0]).toMatchObject({
    expected_version: 1,
    included_seats: 3,
    reason: 'Owner 批准三个免费基础员工席位',
  });
  expect(writes[0]).toMatchObject({ verification_ttl_minutes: 10, verification_max_attempts: 5 });
  expect(writes[0]).not.toHaveProperty('extra_seat_price_cents');
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(1);
});
