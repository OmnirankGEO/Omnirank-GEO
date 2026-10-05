import { expect, test, type Page, type Route } from 'playwright/test';

const user = {
  id: 31,
  user_id: 31,
  username: 'quote-alert-qa',
  display_name: '报价验证用户',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [],
  permissions: ['quote:view', 'quote:write'],
  client_brand_ids: [10],
};

const sessions = [101, 202].map((quoteId, index) => ({
  token: `same-name-${quoteId}`,
  quote_id: quoteId,
  brand_name: '同名客户',
  industry: '软件服务',
  city: '上海',
  status: 'pricing_pending_review',
  selected_count: 1,
  created_at: `2026-07-${20 - index}T00:00:00Z`,
  updated_at: `2026-07-${20 - index}T00:00:00Z`,
}));

const pricingData = {
  generated_at: '2026-07-21T00:00:00Z',
  tiers: {
    entry: { label: '入门版', target_share: 0.10, ai_probability: '50%', stars: 3, total_price: 200, total_articles: 2 },
    standard: { label: '标准版', target_share: 0.20, ai_probability: '65%', stars: 4, total_price: 400, total_articles: 4 },
    flagship: { label: '旗舰版', target_share: 0.30, ai_probability: '75%', stars: 5, total_price: 600, total_articles: 6 },
  },
  keywords: [{
    id: 11,
    keyword: '同名关键词',
    category_label: '核心词',
    entry: { price: 200, articles: 2 },
    standard: { price: 400, articles: 4 },
    flagship: { price: 600, articles: 6 },
  }],
  unavailable_keywords: [{ keyword: '待补证关键词', reason: 'upstream_timeout' }],
};

interface MockOptions {
  memberCapabilities?: string[];
  onCoefficientSave?: (path: string, body: Record<string, unknown>) => void;
  onArchivePreview?: (quoteId: number) => void;
}

async function installMocks(page: Page, options: MockOptions = {}) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'quote-alert-token');
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'returning',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-21T00:00:00Z',
      last_updated_at: '2026-07-21T00:00:00Z',
    }));
  });

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path === '/api/auth/me') return route.fulfill({ json: { success: true, user } });
    if (path === '/api/organization/overview') {
      if (!options.memberCapabilities) return route.fulfill({ status: 404, json: { detail: { code: 'NOT_FOUND', message: 'no organization' } } });
      return route.fulfill({ json: {
        id: 7,
        owner_user_id: 1,
        name: '验证组织',
        status: 'active',
        version: 1,
        authority_version: 1,
        viewer_is_owner: false,
        entitlement: { id: 1, entitled_seats: 3, occupied_seats: 2, available_seats: 1, product_catalog_version: 'v1', source_sku: 'seat' },
        identity: { actor_kind: 'member', membership_id: 9, authority_version: '1', capabilities: options.memberCapabilities },
        assigned_brand_ids: [10],
      } });
    }
    if (path === '/api/keyword-selection/list') return route.fulfill({ json: { sessions } });
    if (path === '/api/quotes' && method === 'GET') return route.fulfill({ json: { items: [] } });
    if (path.startsWith('/api/s/same-name-')) {
      const quoteId = Number(path.split('-').at(-1));
      return route.fulfill({ json: {
        token: `same-name-${quoteId}`,
        quote_id: quoteId,
        brand_id: quoteId === 101 ? 10 : 20,
        brand_name: '同名客户',
        status: 'pricing_pending_review',
        selected_ids: [11],
        custom_keywords: [],
        pricing_data: pricingData,
        clusters_data: null,
      } });
    }
    const coefficientMatch = path.match(/^\/api\/quotes\/(\d+)\/coefficient-preview$/);
    if (coefficientMatch) {
      const coefficient = Number(url.searchParams.get('coefficient') || 1);
      return route.fulfill({ json: {
        quote_id: Number(coefficientMatch[1]),
        old_coefficient: 2,
        new_coefficient: coefficient,
        calculation_version: 'quote-coefficient-application-v1',
        summaries: {
          entry: { total_price: Math.round(100 * coefficient), total_articles: 2 },
          standard: { total_price: Math.round(200 * coefficient), total_articles: 4 },
          flagship: { total_price: Math.round(300 * coefficient), total_articles: 6 },
        },
        keywords: [{ id: 11, keyword: '同名关键词', entry: 100 * coefficient, standard: 200 * coefficient, flagship: 300 * coefficient }],
        keyword_count: 1,
        snapshot_hash: 'a'.repeat(64),
      } });
    }
    const coefficientSave = path.match(/^\/api\/quotes\/(\d+)\/coefficient$/);
    if (coefficientSave && method === 'POST') {
      options.onCoefficientSave?.(path, route.request().postDataJSON());
      return route.fulfill({ json: {
        success: true,
        quote_id: Number(coefficientSave[1]),
        old_coefficient: 2,
        new_coefficient: route.request().postDataJSON().coefficient,
        calculation_version: 'quote-coefficient-application-v1',
        summaries: {},
        keyword_count: 1,
        snapshot: { id: 501, version: 2 },
      } });
    }
    const archivePreview = path.match(/^\/api\/quotes\/(\d+)\/archive-preview$/);
    if (archivePreview) {
      const quoteId = Number(archivePreview[1]);
      options.onArchivePreview?.(quoteId);
      return route.fulfill({ json: {
        object: { type: 'quote', quote_id: quoteId, brand_id: quoteId === 101 ? 10 : 20, display_name: '同名客户', status: 'pricing_pending_review' },
        associations: { selection_sessions: 1, keywords: quoteId === 101 ? 3 : 9, topics: 2, articles: 4, interaction_events: 5 },
        impact: ['报价从列表归档', '公开链接失效'],
        protected: false,
        blocked_reason: null,
        recovery: { action: 'restore', target: `/api/quotes/${quoteId}/restore`, permission: 'team.output_handoff' },
      } });
    }
    if (path === '/api/auth/quote-markup-preference') return route.fulfill({ json: { effective_ratio: 2, source: 'user', can_edit: true } });
    if (path === '/api/agent/pricing/cost-per-article') return route.fulfill({ json: { cost_per_article: 60, system_default: 60 } });
    if (path === '/api/wallet') return route.fulfill({ json: { success: true, data: { paid_points: 1000, commission_points: 0, bonus_points: 0, frozen_points: 0 } } });
    if (path === '/api/user/notifications/unread-count') return route.fulfill({ json: { status: 'success', count: 0 } });
    if (path === '/api/user/notifications') return route.fulfill({ json: { status: 'success', notifications: [] } });
    if (path === '/api/client-context/list') return route.fulfill({ json: { success: true, clients: [] } });
    if (path === '/api/public/whitelabel') return route.fulfill({ json: { success: true, data: null } });
    return route.fulfill({ json: { success: true, status: 'success', data: {}, items: [], records: [], total: 0 } });
  });
}

test('same-name quotes archive and coefficient writes remain quote-id bound', async ({ page }) => {
  const coefficientWrites: Array<{ path: string; body: Record<string, unknown> }> = [];
  const archivePreviews: number[] = [];
  await installMocks(page, {
    onCoefficientSave: (path, body) => coefficientWrites.push({ path, body }),
    onArchivePreview: quoteId => archivePreviews.push(quoteId),
  });
  await page.goto('/pricing');
  await page.getByText('同名客户').first().click();

  const coefficientCard = page.getByTestId('quote-coefficient-card');
  await expect(coefficientCard).toBeVisible();
  await expect(coefficientCard).toContainText('标准方案');
  await coefficientCard.getByLabel('本次报价系数').fill('2.5');
  await expect(coefficientCard).toContainText('¥500');
  await coefficientCard.getByLabel('系数调整原因').fill('扩大本单服务范围');
  await coefficientCard.getByRole('button', { name: '保存并冻结版本' }).click();
  await expect.poll(() => coefficientWrites.length).toBe(1);
  expect(coefficientWrites[0]).toEqual({
    path: '/api/quotes/101/coefficient',
    body: { coefficient: 2.5, reason: '扩大本单服务范围', expected_snapshot_hash: 'a'.repeat(64) },
  });

  const pricingAlert = page.locator('[data-alert-action="retry_pricing"]');
  await expect(pricingAlert).toHaveAttribute('data-alert-permission', 'quote.create');
  await expect(pricingAlert).toHaveAttribute('data-alert-recovery', 'retry_or_review_missing_keyword_evidence');

  await page.getByTitle('删除').first().click();
  await expect(page.getByText(/归档「同名客户」报价 #101/)).toBeVisible();
  await expect(page.getByText(/3 个关键词/)).toBeVisible();
  expect(archivePreviews).toEqual([101]);
  await page.getByRole('button', { name: '取消' }).click();
});

test('member without governance capabilities sees evidence but no governance buttons', async ({ page }) => {
  let coefficientRequests = 0;
  await installMocks(page, {
    memberCapabilities: ['quote.read_own'],
    onCoefficientSave: () => { coefficientRequests += 1; },
  });
  const organizationLoaded = page.waitForResponse(response => response.url().includes('/api/organization/overview'));
  await page.goto('/pricing');
  await organizationLoaded;
  await page.getByText('同名客户').first().click();

  await expect(page.getByTestId('quote-coefficient-card')).toHaveCount(0);
  await expect(page.getByTitle('删除')).toHaveCount(0);
  const pricingAlert = page.locator('[data-alert-action="retry_pricing"]');
  await expect(pricingAlert).toBeVisible();
  await expect(pricingAlert.getByRole('button')).toHaveCount(0);
  expect(coefficientRequests).toBe(0);
});
