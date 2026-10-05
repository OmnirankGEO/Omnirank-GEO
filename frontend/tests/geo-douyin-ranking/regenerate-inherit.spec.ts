import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * 详情页「再次创作」· **继承冻结快照**的真实请求体判据(工单 §P1-3 / §P2-2)
 *
 * 🔴 这一套盯的是一条**正在烧钱**的 bug:
 *    改之前 `RegenerateRequest` 只收 `style_key` / `extra_hint`,服务端也没从
 *    库里取回榜单参数 —— 于是一条付费(260 算力)的「再次创作」会把榜单作品
 *    做成普通图文,全程不报错、无日志、用户只会觉得"这次做出来不一样了"。
 *
 * 判据全部落在**浏览器真发出去的 POST body** 上:
 *   ① 只改风格时 → body 里**没有** ranking_* 字段(= 服务端继承,不是前端重拼);
 *   ② 弹窗默认值来自快照(原母版、原家数、目标引擎都显示出来);
 *   ③ 显式换母版 → 只多出 ranking_template 一个字段;
 *   ④ 卡组型作品 → 弹窗里没有榜单那一块(反向对照)。
 */

const POST_ID = 77

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

type Captured = { bodies: any[] }

function detailPayload(ranking: unknown) {
  return {
    status: 'success',
    post: {
      id: POST_ID, brand_id: 101, title: '深圳载货电梯怎么选', body_text: '正文',
      hashtags: ['电梯'], cards: [], oss_keys: ['a', 'b', 'c', 'd', 'e', 'f'],
      status: 'ready', city: '深圳', keyword: '深圳载货电梯',
      style_key: 'design_text', aspect_ratio: '3:4', contact_enabled: false,
      closing_stale: false, redraw_count: 0, industry_key: 'auto',
    },
    preview_urls: [], siblings: [],
    style: { key: 'design_text', label: '设计文字卡' },
    ranking,
    redraw: { used: 0, limit: 10, remaining: 10 },
    contact: { configured: false, display: '', enabled: false, closing_stale: false },
  }
}

const RANKING_DTO = {
  content_form: 'ranking',
  template: 'tech_spec_matrix',
  template_label: '技术参数对比榜',
  entity_count: 5,
  entity_count_actual: 4,
  degraded: false,
  engine_label: '豆包',
  fallback_notice: null,
  gates: [],
}

async function mockApp(page: Page, ranking: unknown): Promise<Captured> {
  const captured: Captured = { bodies: [] }

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'qa-token')
    localStorage.setItem('omnirank-theme', 'dark')
    sessionStorage.setItem('omnirank_current_brand_candidate:1', '101')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-08-07T00:00:00Z', last_updated_at: '2026-08-07T00:00:00Z',
    }))
  })

  await page.route('**/api/**', async (route) => json(route, { success: true }))
  await page.route('**/api/auth/me', async (route) => json(route, {
    success: true,
    user: {
      id: 1, username: 'qa-owner', display_name: '质检服务商', is_admin: false,
      is_active: 1, must_change_password: 0, roles: [],
      permissions: ['writing:read', 'writing:write'],
      client_brand_ids: [101], agent_level: 1, permission_version: 1,
    },
  }))
  await page.route('**/api/client-context/list', async (route) => json(route, {
    success: true,
    clients: [{ id: 101, name: '端到端探针客户', industry: 'auto',
                diagnosis_count: 0, quote_count: 0, created_at: '2026-08-07T00:00:00Z' }],
  }))
  await page.route('**/api/client-context/101', async (route) => json(route, {
    success: true,
    context: { brand: { id: 101, name: '端到端探针客户', industry: 'auto', diagnosis_count: 0 },
               profile: null, materials: null, relatedQuoteIds: [], socialProjects: [] },
  }))
  await page.route('**/api/geo-douyin/pricing', async (route) => json(route, {
    status: 'success',
    first_generation: { feature_code: 'x', cost_points: 390, feature_name: '' },
    regenerate: { feature_code: 'y', cost_points: 260, feature_name: '' },
    redraw: { feature_code: 'z', cost_points: 100, feature_name: '' },
    extra_card: { feature_code: 'e', cost_points: 30, feature_name: '' },
    included_cards: 4, card_min: 1, card_max: 9, card_default: 4, redraw_limit: 10,
    aspect_ratios: [{ key: '3:4', label: '竖版 3:4', hint: '常规' }],
    aspect_ratio_default: '3:4',
    ranking_templates: [
      { key: 'industrial_overview', label: '工业设备综合榜', when: '多候选' },
      { key: 'tech_spec_matrix', label: '技术参数对比榜', when: '可比参数' },
    ],
    ranking_entity_count_min: 3, ranking_entity_count_max: 6, ranking_entity_count_default: 4,
  }))
  await page.route('**/api/geo-douyin/styles**', async (route) => json(route, {
    status: 'success',
    styles: [{ key: 'design_text', label: '设计文字卡', hint: '', disabled: false }],
  }))
  await page.route('**/api/geo-douyin/status', async (route) => json(route, {
    status: 'success', can_publish: true, publish_disabled_reason: '',
  }))
  await page.route('**/api/geo-douyin/posts?**', async (route) => json(route, {
    status: 'success',
    posts: [{ id: POST_ID, title: '深圳载货电梯怎么选', body_text: '正文', hashtags: [],
              cards: [], status: 'ready', city: '深圳', keyword: '深圳载货电梯',
              progress: null }],
  }))
  await page.route('**/api/geo-douyin/clients/*/keywords', async (route) => json(route, {
    status: 'success', keywords: [], cities: ['深圳'],
  }))
  // 🔴 知识卡端点必须真 mock:catch-all 返回 `{success:true}` 会让
  //    `knowledge.materials.total` 抛 —— 整页崩成"页面出错了",
  //    于是本套测的就不是弹窗而是错误边界。
  await page.route(`**/api/geo-douyin/posts/${POST_ID}/knowledge`, async (route) =>
    json(route, {
      status: 'success', has_brand: true, load_failed: false,
      materials: { filled: 6, total: 8, items: [] },
      images: { count: 0, thumbs: [] }, sources_used: [],
    }))
  await page.route(`**/api/geo-douyin/posts/${POST_ID}/consistency`, async (route) =>
    json(route, { status: 'success', checked: false }))
  await page.route(`**/api/geo-douyin/posts/${POST_ID}/task`, async (route) =>
    json(route, { status: 'success', progress: null }))
  await page.route(`**/api/geo-douyin/posts/${POST_ID}`, async (route) =>
    json(route, detailPayload(ranking)))
  await page.route(`**/api/geo-douyin/posts/${POST_ID}/regenerate`, async (route) => {
    captured.bodies.push(route.request().postDataJSON())
    return json(route, { status: 'accepted', post_id: POST_ID, cards_total: 6,
                         content_form: 'ranking', message: '开始重新创作了' })
  })
  return captured
}

async function openDetail(page: Page) {
  await page.goto('/writing')
  await page.getByRole('tab', { name: /制作 GEO 图文/ }).click()
  await page.getByTestId(`post-card-${POST_ID}`).getByRole('button', { name: '看看' }).click()
  await expect(page.getByRole('button', { name: /再次创作/ })).toBeVisible()
}

test('弹窗默认值来自原作品快照（原母版 / 原家数 / 目标引擎）', async ({ page }) => {
  await mockApp(page, RANKING_DTO)
  await openDetail(page)
  await page.getByRole('button', { name: /再次创作/ }).click()
  await expect(page.getByTestId('regen-ranking')).toBeVisible()
  await expect(page.getByTestId('regen-ranking-inherit'))
    .toContainText('点名 5 家')
  await expect(page.getByTestId('regen-ranking-inherit'))
    .toContainText('上次实际做出 4 家')
  await expect(page.getByTestId('regen-ranking-inherit')).toContainText('豆包')
  await expect(page.getByTestId('regen-ranking-template'))
    .toHaveValue('tech_spec_matrix')
})

test('只改风格 → 请求体里不出现榜单字段（由服务端继承，不是前端重拼）', async ({ page }) => {
  const cap = await mockApp(page, RANKING_DTO)
  await openDetail(page)
  await page.getByRole('button', { name: /再次创作/ }).click()
  await page.getByTestId('regen-hint').fill('多讲工期')
  await page.getByTestId('regen-submit').click()
  await expect.poll(() => cap.bodies.length).toBe(1)
  const b = cap.bodies[0]
  expect(b.extra_hint).toBe('多讲工期')
  // 🔴 这就是"继承"与"重拼"的分界:前端**不发**这些字段。
  expect(b).not.toHaveProperty('ranking_template')
  expect(b).not.toHaveProperty('ranking_entity_count')
  expect(b).not.toHaveProperty('content_form')
})

test('显式换母版 → 只多出 ranking_template 一个字段', async ({ page }) => {
  const cap = await mockApp(page, RANKING_DTO)
  await openDetail(page)
  await page.getByRole('button', { name: /再次创作/ }).click()
  await page.getByTestId('regen-ranking-template').selectOption('industrial_overview')
  await page.getByTestId('regen-submit').click()
  await expect.poll(() => cap.bodies.length).toBe(1)
  expect(cap.bodies[0].ranking_template).toBe('industrial_overview')
  expect(cap.bodies[0]).not.toHaveProperty('ranking_entity_count')
})

test('卡组型作品的弹窗里没有榜单那一块（反向对照）', async ({ page }) => {
  const cap = await mockApp(page, null)
  await openDetail(page)
  await page.getByRole('button', { name: /再次创作/ }).click()
  await expect(page.getByTestId('regen-ranking')).toHaveCount(0)
  await page.getByTestId('regen-submit').click()
  await expect.poll(() => cap.bodies.length).toBe(1)
  expect(cap.bodies[0]).not.toHaveProperty('ranking_template')
})

test('普通用户看不到内部 ID / 供应商 / 内部表名', async ({ page }) => {
  await mockApp(page, RANKING_DTO)
  await openDetail(page)
  await page.getByRole('button', { name: /再次创作/ }).click()
  const text = await page.locator('body').innerText()
  for (const leak of ['contract_hash', 'entity_key', 'geo_research_answer',
                      'deepseek', 'qwen3', 'llm_model', 'extractor_version',
                      'generation_meta']) {
    expect(text.toLowerCase()).not.toContain(leak.toLowerCase())
  }
})

for (const width of [320, 390, 768, 1440, 2560]) {
  test(`再次创作弹窗在 ${width}px 无横向溢出`, async ({ page }) => {
    await mockApp(page, RANKING_DTO)
    await page.setViewportSize({ width, height: 900 })
    await openDetail(page)
    await page.getByRole('button', { name: /再次创作/ }).click()
    await expect(page.getByTestId('regen-ranking')).toBeVisible()
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow).toBeLessThanOrEqual(1)
    await expect(page.getByTestId('regen-submit')).toBeVisible()
  })
}
