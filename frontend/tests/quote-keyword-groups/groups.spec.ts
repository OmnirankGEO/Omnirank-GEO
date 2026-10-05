import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * 报价关键词「地域与商业意图双向反转」· T5 **真浏览器行为锁**(工单 §6 前端六条)
 *
 * 事故现场:报价页把知识题、需澄清、业务不匹配、地域过宽、外地地域全部塞进同一块,
 * 并使用同一句「该问法通常不会让 AI 推荐具体品牌、服务商、产品或方案」——
 * 操作员看不出应该放行、改词还是删除(R5)。
 *
 * 六条按工单 §6「前端」逐条落:
 *   锁1 混合响应渲染成**不同原因区**,不再统一显示"不会推荐"
 *   锁2 六个龙岗精准词默认勾选
 *   锁3 `商场推荐` 对本地客户默认不选,但可点一次放行并进入选中集合
 *   锁4 业务不匹配与地域过宽显示**不同文案**
 *   锁5 提交 payload 保留三轴决策与人工 override 审计原因
 *   锁6 320/390/768/1440 视口无溢出、按钮可点击(由 config 的四个 project 驱动)
 *
 * 🔴 后端响应在这里是**受控 mock**:本文件锁的是"前端拿到三轴之后怎么渲染",
 *    "后端算得对不对"由 tests/quotegeo_2026_08_10/ 端到端锁负责。两边不重复。
 */

const BRAND_ID = 7777

/** 六条应默认进入交付的龙岗精准词(工单 §5.1 逐字) */
const DELIVERED = [
  '深圳龙岗买家具建材去哪里好',
  '深圳龙岗租商铺做餐饮哪里合适',
  '深圳龙岗办公家具批发市场在哪',
  '深圳龙岗建材市场有哪些',
  '深圳龙岗商场招商电话',
  '深圳龙岗哪个商场适合租商铺开店',
]

const kw = (
  keyword: string,
  commercial_intent: string,
  business_scope: string,
  geo_scope: string,
  default_selected: boolean,
  reason_code: string,
  reason_group: string,
  reason_text: string,
) => ({
  keyword, source: 'llm', category: '行业核心',
  commercial_intent, business_scope, geo_scope, default_selected,
  reason_code, reason_group, reason_text,
  policy_version: 'geo-commercial-intent-governance-v1.0',
  delivery_policy_version: 'quote-keyword-delivery-decision-v1',
  geo_recommend: commercial_intent === 'commercial',
  scope_match: business_scope !== 'mismatched' && geo_scope === 'matched',
  rejection_reason: default_selected ? '' : reason_text,
  hard_block: false,
  human_override_allowed: true,
  commercial_delivery_eligible: commercial_intent === 'commercial',
})

const EXPAND_BODY = {
  success: true,
  keywords: DELIVERED.map(k =>
    kw(k, 'commercial', 'matched', 'matched', true, 'DELIVERY_OK', 'delivered', '本地强意图问法,已进入交付。')),
  rejected_keywords: [
    kw('商场推荐', 'commercial', 'matched', 'too_broad', false,
       'GEO_SCOPE_TOO_BROAD', 'geo_too_broad',
       '商业意图成立,但客户只服务本地市场,这个词地域过宽,默认不计入交付。'),
    kw('商业广场推荐', 'commercial', 'matched', 'too_broad', false,
       'GEO_SCOPE_TOO_BROAD', 'geo_too_broad',
       '商业意图成立,但客户只服务本地市场,这个词地域过宽,默认不计入交付。'),
    kw('商业综合体设计公司', 'commercial', 'mismatched', 'too_broad', false,
       'BUSINESS_SCOPE_MISMATCH', 'scope_mismatch',
       '这是真实的商业问法,但该问法求的是「设计」服务,不在客户已确认的业务范围内,默认不计入交付。'),
    kw('上海商场推荐', 'commercial', 'matched', 'outside_market', false,
       'GEO_SCOPE_OUTSIDE_MARKET', 'geo_outside',
       '商业意图成立,但这个词的地域超出了客户当前的服务市场。'),
    kw('商场是什么', 'knowledge', 'matched', 'too_broad', false,
       'COMMERCIAL_INTENT_KNOWLEDGE', 'knowledge',
       '这是知识/教程类问法,AI 回答时通常只讲概念,不会点名商家,默认不计入交付。'),
    kw('商场洗手间在哪', 'uncertain', 'matched', 'too_broad', false,
       'COMMERCIAL_INTENT_UNCERTAIN', 'needs_confirm',
       '看不出这是客户会拿去问 AI 的成交问法,先放这里等你确认。'),
  ],
  scope_lock: {
    version: 'quote-scope-lock-v1',
    service_market: ['深圳'], market_level: 'district', business_type: 'B2C',
    sub_regions: ['龙岗'], provinces: ['广东'], source: 'manual',
    real_query_seeds: [], buyer_persona: '本地商业项目招商负责人',
  },
  summary: { market_level: 'district', total: DELIVERED.length },
}

interface Capture { selectionBody: any }

async function mockApp(page: Page, capture: Capture) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'qa-token')
    localStorage.setItem('omnirank-theme', 'dark')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-07-21T12:00:00Z', last_updated_at: '2026-07-21T12:00:00Z',
    }))
  })

  const json = (route: Route, body: unknown) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(body),
  })

  await page.route('**/api/**', async (route: Route) => {
    const p = new URL(route.request().url()).pathname
    const method = route.request().method()

    if (p === '/api/auth/me') {
      return json(route, {
        success: true,
        user: { id: 1, username: 'qa', display_name: 'QA', is_admin: true,
                agent_level: 1, permission_version: 1, modules: ['quote', 'keyword', 'brand'] },
      })
    }
    if (p === '/api/client-context/list') {
      return json(route, {
        success: true,
        clients: [{ id: BRAND_ID, name: '龙岗某商业中心', brand_name: '龙岗某商业中心',
                    industry: '商业地产运营', access_mode: 'real', is_test: false }],
      })
    }
    if (p === '/api/keywords/expand' && method === 'POST') {
      return json(route, EXPAND_BODY)
    }
    if (p === '/api/keyword-selection/create' && method === 'POST') {
      capture.selectionBody = route.request().postDataJSON()
      return json(route, { success: true, token: 'qa-selection-token' })
    }
    return json(route, { success: true, status: 'success', items: [], clients: [], data: {}, quotes: [] })
  })
}

/** 走到候选词已渲染的状态(全程走真 UI:URL 选客户 → 填核心词 → 点扩词) */
async function openAndExpand(page: Page) {
  await page.goto(`/pricing?brand_id=${BRAND_ID}`)
  const core = page.getByTestId('quote-core-keywords')
  await expect(core).toBeVisible({ timeout: 30_000 })
  await core.fill('商场招商')
  await page.getByTestId('quote-expand-btn').click()
  await expect(page.getByTestId('kwgroup-delivered')).toBeVisible({ timeout: 30_000 })
}

test.describe('T5 · 报价候选词按真实原因分组', () => {
  test('锁1/锁4 · 混合响应渲染成不同原因区,各区文案不同,不再统一显示"不会推荐"', async ({ page }) => {
    const capture: Capture = { selectionBody: null }
    await mockApp(page, capture)
    await openAndExpand(page)

    // 锁1:四个不同原因区都在
    for (const group of ['geo_too_broad', 'scope_mismatch', 'geo_outside', 'knowledge', 'needs_confirm']) {
      await expect(page.getByTestId(`kwgroup-${group}`)).toBeVisible()
    }

    // 锁4:业务不匹配 与 地域过宽 的文案必须**不同**
    const geoText = await page.getByTestId('kwgroup-geo_too_broad').innerText()
    const scopeText = await page.getByTestId('kwgroup-scope_mismatch').innerText()
    expect(geoText).toContain('地域过宽')
    expect(scopeText).toContain('设计')
    expect(geoText).not.toEqual(scopeText)

    // 反向对照:事故现场那句话在整页里**一次都不许出现**
    const body = await page.locator('body').innerText()
    expect(body).not.toContain('通常不会让 AI 推荐具体品牌')
  })

  test('锁2 · 六个龙岗精准词默认勾选', async ({ page }) => {
    const capture: Capture = { selectionBody: null }
    await mockApp(page, capture)
    await openAndExpand(page)

    const delivered = page.getByTestId('kwgroup-delivered')
    for (const keyword of DELIVERED) {
      const label = delivered.locator('label', { hasText: keyword })
      await expect(label).toHaveCount(1)
      await expect(label.locator('button[role="checkbox"]')).toHaveAttribute('data-state', 'checked')
    }
  })

  test('锁3 · 商场推荐默认不选,点一次放行后进入选中集合', async ({ page }) => {
    const capture: Capture = { selectionBody: null }
    await mockApp(page, capture)
    await openAndExpand(page)

    // 默认不在交付区
    await expect(page.getByTestId('kwgroup-delivered').locator('label', { hasText: '商场推荐' }))
      .toHaveCount(0)

    const row = page.getByTestId('kwrow-geo_too_broad').filter({ hasText: '商场推荐' }).first()
    await expect(row).toBeVisible()
    await row.getByTestId('kwrelease-geo_too_broad').click()

    // 放行后进入交付区且被勾选
    const label = page.getByTestId('kwgroup-delivered').locator('label', { hasText: '商场推荐' }).first()
    await expect(label).toBeVisible()
    await expect(label.locator('button[role="checkbox"]')).toHaveAttribute('data-state', 'checked')
    // 反向对照:没被放行的那条仍留在原区(否则"全放行"也能让上面变绿)
    await expect(page.getByTestId('kwrow-geo_outside').filter({ hasText: '上海商场推荐' }))
      .toHaveCount(1)
  })

  test('锁5 · 提交 payload 保留三轴决策与人工放行审计原因', async ({ page }) => {
    const capture: Capture = { selectionBody: null }
    await mockApp(page, capture)
    await openAndExpand(page)

    await page.getByTestId('kwrow-geo_too_broad').filter({ hasText: '商场推荐' }).first()
      .getByTestId('kwrelease-geo_too_broad').click()

    await page.getByTestId('quote-create-selection').click()
    await expect.poll(() => capture.selectionBody, { timeout: 20_000 }).not.toBeNull()

    const rows: any[] = capture.selectionBody.keywords
    const released = rows.find(r => r.keyword === '商场推荐')
    expect(released).toBeTruthy()
    expect(released.commercial_intent).toBe('commercial')
    expect(released.geo_scope).toBe('too_broad')
    expect(released.reason_code).toBe('GEO_SCOPE_TOO_BROAD')
    expect(released.delivery_policy_version).toBe('quote-keyword-delivery-decision-v1')
    // 审计要说清放行了**哪一类**判定,只写"操作员放行"复盘时等于没写
    expect(released.review_override_reason).toContain('地域范围过宽')
    expect(released.review_override_reason).toContain('GEO_SCOPE_TOO_BROAD')

    // 反向对照:本来就该进交付的词不许被标成"人工放行"
    const natural = rows.find(r => r.keyword === DELIVERED[0])
    expect(natural.reason_code).toBe('DELIVERY_OK')
    expect(natural.review_override_reason).toBe('')
  })

  test('锁6 · 当前视口下无横向溢出,且每组的放行按钮真的可点', async ({ page }, testInfo) => {
    const capture: Capture = { selectionBody: null }
    await mockApp(page, capture)
    await openAndExpand(page)

    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }))
    expect(
      overflow.scrollWidth,
      `${testInfo.project.name}: 页面横向溢出 ${overflow.scrollWidth} > ${overflow.clientWidth}`,
    ).toBeLessThanOrEqual(overflow.clientWidth + 1)

    for (const group of ['geo_too_broad', 'scope_mismatch', 'geo_outside', 'needs_confirm']) {
      const button = page.getByTestId(`kwrelease-${group}`).first()
      await expect(button).toBeVisible()
      const box = await button.boundingBox()
      expect(box, `${group} 放行按钮没有可点区域`).not.toBeNull()
      expect(box!.width).toBeGreaterThan(0)
      expect(box!.height).toBeGreaterThan(0)
    }
  })
})
