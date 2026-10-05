import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * [WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11] 报价系数顶栏 + 演示隐私模式 · **真浏览器行为锁**
 *
 * 事故现场:
 *   ① 「审核与发送」正文里一整块「本次报价系数」卡 —— 服务商当面演示时,客户直接看到
 *      售价系数、调整原因、三档换算、关键词标准价、冻结说明;
 *   ② 顶部眼睛只把**数值**换成 `••`,「默认报价系数」「毛利」「单篇成本」「平台设定」
 *      这些**字段名**照旧渲染;
 *   ③ 显示态存 `localStorage: omnirank_pricing_reveal` → 上次点过显示,刷新/重开仍是公开态。
 *
 * 🔴 判据设计口径:
 *   · 每条"必须不出现"都配一条**同场景的"必须出现"**(显示态下同一串必须能被看到),
 *     否则"页面没渲染出来"也会让隐藏断言全绿(零判别力)。
 *   · DOM 级判据用 count()/innerText,不看 CSS —— 工单 §6 明令不许用 opacity/blur/visibility 遮挡。
 *   · 网络级判据看真实请求列表 —— "不预加载本次系数预览"只能这么证。
 */

const QUOTE_REVIEW = 9001        // status = pricing_pending_review(可改系数)
const QUOTE_SENT = 9002          // status = quoted(已发送,不可改)
const TOKEN_REVIEW = 'qa-token-review'
const TOKEN_SENT = 'qa-token-sent'

/** 只在本套件出现的指纹值 —— 避免与页面其它数字撞车导致断言恒真/恒假 */
const MARKUP_RATIO = 1.8
const MARKUP_MARGIN_PCT = 44
const COST_PER_ARTICLE = 66
const PREVIEW_ENTRY = 88888
const PREVIEW_STANDARD = 99999
const PREVIEW_FLAGSHIP = 111111
const PREVIEW_KEYWORD = '隐私核验词'
const SNAPSHOT_HASH = 'a'.repeat(64)

/**
 * 工单 §6「隐藏范围」逐条落成串。隐藏态一个都不许出现在可见 DOM。
 *
 * 🔴 这里刻意**不**放「本次报价系数」:工单 §3/§6 明确允许两个功能标签常驻显示,
 *    而标签文案「修改本次报价系数」把它整个包住 —— 拿它当敏感串会把合法标签判成泄露
 *    (第一轮实测就是这么红的)。编辑器本体改用它独有的「仅影响报价 #」「售价系数」锁。
 */
const SECRET_LABELS = [
  '默认报价系数',
  '毛利',
  '单篇内容成本',
  '你设定的',
  '售价系数',
  '调整原因',
  '保存并冻结版本',
  '仅影响报价',
]
/** 🔴 带上单位/千分位再断言:裸 `66`、裸 `1.8` 会跟页面别处的价格、比例撞车(恒红) */
const SECRET_VALUES = [
  `${MARKUP_RATIO} 倍`,
  `${MARKUP_MARGIN_PCT}%`,
  `¥${COST_PER_ARTICLE} / 篇`,
  `¥${PREVIEW_ENTRY.toLocaleString('en-US')}`,
  `¥${PREVIEW_STANDARD.toLocaleString('en-US')}`,
  `¥${PREVIEW_FLAGSHIP.toLocaleString('en-US')}`,
  PREVIEW_KEYWORD,
]

const tier = (label: string, price: number, articles: number) => ({
  label, target_share: 0.5, ai_probability: '问 3 次约出现 2 次', stars: 3,
  total_price: price, total_articles: articles,
})

const pricingKeyword = (id: number, keyword: string) => ({
  id, keyword, category_label: '行业核心',
  entry: { price: 1000, articles: 2 },
  standard: { price: 2000, articles: 3 },
  flagship: { price: 3000, articles: 4 },
  price_locked_until: null, price_lock_note: null,
})

const PRICING_DATA = {
  generated_at: '2026-08-11T02:00:00Z',
  tiers: {
    entry: tier('入门版', 12000, 20),
    standard: tier('标准版', 24000, 36),
    flagship: tier('旗舰版', 36000, 52),
  },
  // 🔴 词数要多到**连 4K(2160 高)也撑出滚动条** —— R3 那条「滚下去顶栏还在不在」的判据
  //    靠的是页面真的能滚;只放 2 个词时 4K 下根本不溢出,反向对照立不住 = 零判别力。
  keywords: [
    pricingKeyword(1, '深圳办公家具批发'),
    pricingKeyword(2, '龙岗建材市场'),
    ...Array.from({ length: 40 }, (_, i) => pricingKeyword(100 + i, `批发渠道关键词${i + 1}`)),
  ],
}

const SESSIONS = [
  {
    token: TOKEN_REVIEW, quote_id: QUOTE_REVIEW, brand_name: '待审核客户甲',
    industry: '商业地产运营', city: '深圳', status: 'pricing_pending_review',
    selected_count: 2, created_at: '2026-08-10T02:00:00Z',
  },
  {
    token: TOKEN_SENT, quote_id: QUOTE_SENT, brand_name: '已发送客户乙',
    industry: '商业地产运营', city: '深圳', status: 'quoted',
    selected_count: 2, created_at: '2026-08-09T02:00:00Z',
  },
]

/**
 * [Owner 2026-08-11 亲裁 · R5] 关键词行展开区「为什么是这个价」里
 * **两种状态下都永不许出现**的措辞 —— 处置是「删掉」,不是「藏起来」。
 *   · 单篇成本 / 基础成本公式 = 服务商进货价;
 *   · 报价系数 / 你账号的报价系数 = 内部加价倍率;
 *   · 内部定价说明 / ******** = 此地无银三百两的占位行(等于宣告"这里瞒着你")。
 * 剩下的「成本构成」一句只讲成本大致由什么决定,常显、不受眼睛控制。
 */
const RATIONALE_NEVER = [
  '单篇成本',
  '基础成本 = 建议篇数 × 单篇成本',
  '你账号的报价系数',
  '报价系数',
  '内部定价说明',
  '********',
]
/**
 * 整页级断言只能用**不会撞合法标签**的串。
 * 🔴 `报价系数` 被顶栏标签「修改本次报价系数」整个包住 —— 拿它扫全页 = 把合法标签
 *    判成泄露(这个坑我在本包里踩了两次,与首轮「本次报价系数」同型)。
 */
const RATIONALE_SECRETS_PAGEWIDE = [
  '单篇成本',
  '基础成本 = 建议篇数 × 单篇成本',
  '你账号的报价系数',
]
/** 同一块里**允许**给客户看的内容 —— 用作正面对照,防"整块没渲染"也算通过。
 *  [WO_QUOTE_MEDIA_MIX 2026-08-12] 「发布篇数」已升级为「投放组合」+「投放节奏」两行,
 *  两者都是客户可见的价值说明,不受隐私眼睛控制。 */
const RATIONALE_PUBLIC = ['为什么是这个价', '投放组合', '重点媒体锚点', '行业与平台覆盖', '商业价值', '成本构成']
/** 客户可见区里**不许**出现的内部字段名(工单 §6 末句) */
const MIX_INTERNAL_NAMES = ['anchor_n', 'unclassified', 'ratio_source', 'coverage_to_anchor', 'cell_n']

interface Capture {
  requests: { method: string; path: string; query: string }[]
  coefficientPost: { url: string; body: any } | null
  profilePut: any | null
  /** 挂住 POST /coefficient 的闸门 —— 用来构造"请求在途时用户隐藏面板" */
  coefficientPostGate?: Promise<void>
}

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

  const json = (route: Route, body: unknown, status = 200) => route.fulfill({
    status, contentType: 'application/json', body: JSON.stringify(body),
  })

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    const p = url.pathname
    const method = route.request().method()
    capture.requests.push({ method, path: p, query: url.search })

    if (p === '/api/auth/me') {
      return json(route, {
        success: true,
        user: { id: 1, username: 'qa', display_name: 'QA', is_admin: true,
                agent_level: 1, permission_version: 1, modules: ['quote', 'keyword', 'brand'] },
      })
    }
    if (p === '/api/auth/quote-markup-preference') {
      return json(route, {
        effective_ratio: MARKUP_RATIO, own_ratio: MARKUP_RATIO, admin_override: null,
        source: 'self', can_edit: true, needs_pricing_disclaimer: false,
        margin_pct: MARKUP_MARGIN_PCT,
      })
    }
    if (p === '/api/agent/pricing/cost-per-article') {
      if (method === 'PUT') return json(route, { success: true })
      return json(route, { cost_per_article: COST_PER_ARTICLE, system_default: 60 })
    }
    if (p === '/api/auth/profile' && method === 'PUT') {
      capture.profilePut = route.request().postDataJSON()
      return json(route, { success: true })
    }
    if (p === '/api/keyword-selection/list') {
      return json(route, { success: true, sessions: SESSIONS })
    }
    if (p === '/api/quotes' && method === 'GET') {
      return json(route, { items: [] })
    }
    if (p === `/api/s/${TOKEN_REVIEW}` || p === `/api/s/${TOKEN_SENT}`) {
      const session = SESSIONS.find(s => p.endsWith(s.token))!
      return json(route, {
        token: session.token, status: session.status, brand_name: session.brand_name,
        industry: session.industry, city: session.city,
        selected_ids: [], custom_keywords: [], business_lines: [],
        keywords: [], pricing_data: PRICING_DATA, clusters_data: null,
      })
    }
    if (/^\/api\/quotes\/\d+\/coefficient-preview$/.test(p)) {
      const quoteId = Number(p.split('/')[3])
      return json(route, {
        quote_id: quoteId, old_coefficient: 1.2, new_coefficient: 1.2,
        calculation_version: 'qa-v1',
        summaries: {
          entry: { total_price: PREVIEW_ENTRY, total_articles: 20 },
          standard: { total_price: PREVIEW_STANDARD, total_articles: 36 },
          flagship: { total_price: PREVIEW_FLAGSHIP, total_articles: 52 },
        },
        keywords: [{ id: 1, keyword: PREVIEW_KEYWORD, entry: 100, standard: 200, flagship: 300 }],
        keyword_count: 1,
        snapshot_hash: SNAPSHOT_HASH,
      })
    }
    if (/^\/api\/quotes\/\d+\/coefficient$/.test(p) && method === 'POST') {
      capture.coefficientPost = { url: p, body: route.request().postDataJSON() }
      if (capture.coefficientPostGate) await capture.coefficientPostGate
      return json(route, {
        success: true, quote_id: Number(p.split('/')[3]),
        old_coefficient: 1.2, new_coefficient: 1.5,
        calculation_version: 'qa-v2', summaries: {}, keyword_count: 1, snapshot: {},
      })
    }
    return json(route, { success: true, status: 'success', items: [], clients: [], data: {}, quotes: [] })
  })
}

const freshCapture = (): Capture => ({ requests: [], coefficientPost: null, profilePut: null })

const bar = (page: Page) => page.getByTestId('quote-pricing-control-bar')

/** 侧边栏(桌面)与下拉(移动)两条路径都走真 UI */
async function selectSession(page: Page, token: string, brandName: string) {
  const width = page.viewportSize()?.width ?? 1440
  if (width < 768) {
    await page.locator('select').first().selectOption(token)
  } else {
    await page.locator('p', { hasText: brandName }).first().click()
  }
  await expect(bar(page)).toBeVisible()
}

async function openPricingPage(page: Page, capture: Capture) {
  await mockApp(page, capture)
  await page.goto('/pricing')
  await expect(bar(page)).toBeVisible({ timeout: 60_000 })
}

/** 可见 DOM 全文(含所有已渲染文本) */
const bodyText = (page: Page) => page.locator('body').innerText()

async function expectSecretsHidden(page: Page) {
  const text = await bodyText(page)
  for (const s of [...SECRET_LABELS, ...SECRET_VALUES]) {
    expect(text, `隐藏态泄露了「${s}」`).not.toContain(s)
  }
}

test.describe('报价系数顶栏 · 演示隐私模式', () => {
  test('§11.1/§11.2 · 首次进入默认隐藏,刷新后仍默认隐藏', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    await expect(bar(page).getByTestId('quote-pricing-mask-inline')).toBeVisible()
    await expect(bar(page).getByTestId('quote-pricing-reveal-toggle'))
      .toHaveAttribute('aria-label', '显示报价隐私信息')
    await expectSecretsHidden(page)

    // 🔴 反向对照:旧实现把显示态写进 localStorage。这里先真的点开(证明能显示),
    //    再刷新 —— 回退成 localStorage 持久化的话,刷新后就会是公开态。
    await bar(page).getByTestId('quote-pricing-tab-default').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByText(`默认报价系数 · ${MARKUP_RATIO} 倍`)).toBeVisible()

    await page.reload()
    await expect(bar(page)).toBeVisible({ timeout: 60_000 })
    await expect(bar(page).getByTestId('quote-pricing-mask-inline')).toBeVisible()
    await expectSecretsHidden(page)

    // 持久化痕迹也一并锁死(改回 localStorage 会在这里红)
    const persisted = await page.evaluate(() => ({
      local: localStorage.getItem('omnirank_pricing_reveal'),
      session: sessionStorage.getItem('omnirank_pricing_reveal'),
    }))
    expect(persisted.local).toBeNull()
    expect(persisted.session).toBeNull()
  })

  test('§11.3/§11.4 · 隐藏时字段名与数值都不在可见 DOM,敏感区统一渲染 ********', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    for (const tab of ['quote-pricing-tab-default', 'quote-pricing-tab-current']) {
      await bar(page).getByTestId(tab).click()
      const masked = bar(page).getByTestId('quote-pricing-masked')
      await expect(masked).toBeVisible()
      // 恰好是八颗星,不是"遮住倍数、留着字段名"
      expect((await masked.innerText()).replace(/\s+/g, '')).toContain('********')
      await expectSecretsHidden(page)
      // 隐藏态整条栏内不许有任何输入框(输入框 = 可聚焦、可复制的敏感值)
      await expect(bar(page).locator('input')).toHaveCount(0)
      await bar(page).getByTestId(tab).click()   // 收起,准备下一个标签
    }

    // 旧遮罩形态 `••` 必须绝迹(否则"换个遮罩符号"也能算通过)
    expect(await bodyText(page)).not.toContain('••')
  })

  test('§11.5/§11.6 · 点眼睛后完整内容一次性出现;再次隐藏后输入框与敏感文字从 DOM 消失', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    // ── 默认报价设置
    await bar(page).getByTestId('quote-pricing-tab-default').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    // 先等面板真的加载完(/api/auth/quote-markup-preference 在途时显示"加载报价定价设置…"),
    // 否则读到的是 loading 态,正面对照会假红
    await expect(bar(page).getByTestId('quote-markup-panel')).toBeVisible({ timeout: 30_000 })
    const shown = await bodyText(page)
    for (const s of ['默认报价系数', '毛利', '单篇内容成本', '你设定的',
                     `${MARKUP_RATIO} 倍`, `${MARKUP_MARGIN_PCT}%`, `¥${COST_PER_ARTICLE} / 篇`]) {
      expect(shown, `显示态反而看不到「${s}」= 隐藏断言零判别力`).toContain(s)
    }
    expect(await bar(page).locator('input').count()).toBeGreaterThan(0)

    // ── 本次报价系数
    await bar(page).getByTestId('quote-pricing-tab-current').click()
    await expect(bar(page).getByTestId('quote-coefficient-editor')).toBeVisible()
    await expect(bar(page).getByText(PREVIEW_KEYWORD)).toBeVisible({ timeout: 30_000 })
    const shown2 = await bodyText(page)
    for (const s of ['仅影响报价', '售价系数', '调整原因', '保存并冻结版本', `¥${PREVIEW_STANDARD.toLocaleString('en-US')}`]) {
      expect(shown2, `显示态反而看不到「${s}」`).toContain(s)
    }

    // ── 再次隐藏
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByTestId('quote-coefficient-editor')).toHaveCount(0)
    await expect(bar(page).locator('input')).toHaveCount(0)
    await expectSecretsHidden(page)
  })

  test('§11.7/§11.8 · 顶部有「修改本次报价系数」标签,审核步骤里的大块系数卡已不存在', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    await expect(bar(page).getByTestId('quote-pricing-tab-current')).toBeVisible()
    await expect(bar(page).getByTestId('quote-pricing-tab-default')).toBeVisible()

    // 旧卡的锚点在全页彻底消失
    await expect(page.getByTestId('quote-coefficient-card')).toHaveCount(0)

    // 正面对照:Step4 正文该有的东西还在(证明我们确实站在"审核与发送"这一步,
    // 而不是页面根本没渲染出来 —— 否则上面的 count(0) 恒真)。
    // 🔴 用「发送报价给客户」按钮而不是关键词文本:关键词在响应式表格/卡片里同时存在
    //    多份副本,`.first()` 会命中当前视口下被隐藏的那一份(实测 9 个匹配,第一个 hidden)。
    await expect(page.getByRole('button', { name: /确认无误，发送报价给客户/ })).toBeVisible({ timeout: 30_000 })

    // 展开顶栏后,全页仍只有**一套**本次系数编辑器
    await bar(page).getByTestId('quote-pricing-tab-current').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(page.getByTestId('quote-coefficient-editor')).toHaveCount(1)
  })

  test('§11.9 · 切换报价立刻回隐藏,不会闪出上一份报价的数据,也不预取新报价预览', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    await bar(page).getByTestId('quote-pricing-tab-current').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByText(PREVIEW_KEYWORD)).toBeVisible({ timeout: 30_000 })
    expect(capture.requests.some(r => r.path === `/api/quotes/${QUOTE_REVIEW}/coefficient-preview`)).toBe(true)

    capture.requests.length = 0
    await selectSession(page, TOKEN_SENT, '已发送客户乙')

    // 切过去立刻是隐藏态,编辑器与上一份的预览值全部不在 DOM
    await expect(bar(page).getByTestId('quote-pricing-mask-inline')).toBeVisible()
    await expect(bar(page).getByTestId('quote-coefficient-editor')).toHaveCount(0)
    await expectSecretsHidden(page)

    // 隐藏态**不预加载**本次系数预览(工单 §6:不预加载并展示本次系数预览)
    await page.waitForTimeout(1200)
    expect(
      capture.requests.filter(r => r.path.includes('/coefficient-preview')),
      '切换报价后隐藏态仍在预取系数预览',
    ).toHaveLength(0)
  })

  test('§11.10/§11.12 · 保存本次系数走既有 API(CAS + 原因),保存成功后回隐藏并刷新报价', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    await bar(page).getByTestId('quote-pricing-tab-current').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByText(PREVIEW_KEYWORD)).toBeVisible({ timeout: 30_000 })

    await bar(page).getByLabel('本次报价系数').fill('1.5')
    await bar(page).getByLabel('系数调整原因').fill('本单服务范围扩大')
    capture.requests.length = 0
    await bar(page).getByRole('button', { name: '保存并冻结版本' }).click()

    await expect.poll(() => capture.coefficientPost, { timeout: 30_000 }).not.toBeNull()
    // §11.12 只作用于当前这一份报价
    expect(capture.coefficientPost!.url).toBe(`/api/quotes/${QUOTE_REVIEW}/coefficient`)
    expect(capture.coefficientPost!.body.coefficient).toBe(1.5)
    // 调整原因 + CAS 快照哈希都必须在 payload 里(§12 反向:删掉任一条这里就红)
    expect(capture.coefficientPost!.body.reason).toBe('本单服务范围扩大')
    expect(capture.coefficientPost!.body.expected_snapshot_hash).toBe(SNAPSHOT_HASH)
    expect(capture.coefficientPost!.body.expected_snapshot_hash).toMatch(/^[0-9a-f]{64}$/)
    // 本次系数不许顺手改默认系数
    expect(capture.profilePut).toBeNull()

    // §11.10 保存后走既有会话刷新链路,套餐/关键词报价立即重拉
    await expect.poll(
      () => capture.requests.filter(r => r.path === `/api/s/${TOKEN_REVIEW}`).length,
      { timeout: 30_000 },
    ).toBeGreaterThan(0)

    // 保存成功 → 自动恢复隐藏
    await expect(bar(page).getByTestId('quote-pricing-mask-inline')).toBeVisible()
    await expect(bar(page).getByTestId('quote-coefficient-editor')).toHaveCount(0)
  })

  test('§11.11 · 默认报价设置只打 profile 链路,绝不落到当前报价上', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    await bar(page).getByTestId('quote-pricing-tab-default').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByText(`默认报价系数 · ${MARKUP_RATIO} 倍`)).toBeVisible()

    await bar(page).locator('input[type="number"]').first().fill('2.5')
    await bar(page).getByRole('button', { name: '保存' }).first().click()

    await expect.poll(() => capture.profilePut, { timeout: 30_000 }).not.toBeNull()
    expect(capture.profilePut.quote_markup_ratio).toBe('2.5')
    // 🔴 默认系数误作用到当前报价 = 工单 §12 必须转红的回退
    expect(capture.coefficientPost, '默认报价设置打到了当前报价的 coefficient 端点').toBeNull()
  })

  test('§11.13 · 已发送报价不能从前端绕过状态限制', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_SENT, '已发送客户乙')

    await bar(page).getByTestId('quote-pricing-tab-current').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()

    await expect(bar(page).getByTestId('quote-coefficient-blocked'))
      .toHaveText('报价已发送，如需调整请先按现有流程撤回')
    await expect(bar(page).getByTestId('quote-coefficient-editor')).toHaveCount(0)
    await expect(bar(page).locator('input')).toHaveCount(0)
    expect(capture.requests.filter(r => r.path.includes('/coefficient'))).toHaveLength(0)

    // 正面对照:同一个显示态下,默认报价设置照常可编辑(证明"看不到编辑器"是状态闸,不是整栏坏了)
    await bar(page).getByTestId('quote-pricing-tab-default').click()
    await expect(bar(page).getByText(`默认报价系数 · ${MARKUP_RATIO} 倍`)).toBeVisible()
  })

  test('§11.14 · 客户公开链接 /s/:token 不出现系数、成本、毛利、来源与调整原因', async ({ page }) => {
    const capture = freshCapture()
    await mockApp(page, capture)
    await page.goto(`/s/${TOKEN_SENT}`)

    // 正面对照:客户页确实渲染出来了(否则下面的"不含"恒真)
    await expect(page.getByText('已发送客户乙').first()).toBeVisible({ timeout: 60_000 })

    const text = await bodyText(page)
    for (const s of ['报价系数', '售价系数', '毛利', '单篇内容成本', '调整原因', '你设定的', '平台设定', '系统默认']) {
      expect(text, `客户公开页泄露了「${s}」`).not.toContain(s)
    }
    // 客户该看到的套餐报价仍在(工单 §6:不要把整张客户报价单遮住)
    expect(text).toContain('标准版')
  })

  /* ────────────────────────────────────────────────────────────────
     返修 R1(Review P1):保存在途时隐藏面板,不许提前解除「发送报价」锁。
     首轮实现里编辑器卸载会无条件上报 busy=false —— 用户保存后立刻点眼睛,
     POST 还没落库发送按钮就恢复可用,旧价格可能被发给客户。
     ──────────────────────────────────────────────────────────────── */
  test('R1 · coefficient POST 在途时隐藏面板,发送按钮必须持续禁用到请求真正结束', async ({ page }) => {
    const capture = freshCapture()
    let releasePost!: () => void
    capture.coefficientPostGate = new Promise<void>(resolve => { releasePost = resolve })

    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    const sendBtn = page.getByRole('button', { name: /确认无误，发送报价给客户/ })
    await expect(sendBtn).toBeVisible({ timeout: 30_000 })

    await bar(page).getByTestId('quote-pricing-tab-current').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByText(PREVIEW_KEYWORD)).toBeVisible({ timeout: 30_000 })

    await bar(page).getByLabel('本次报价系数').fill('1.5')
    await bar(page).getByLabel('系数调整原因').fill('本单服务范围扩大')
    // 正面对照:预览落定后按钮本来是可用的 —— 否则下面的 toBeDisabled 恒真
    await expect(bar(page).getByText(PREVIEW_KEYWORD)).toBeVisible()
    await expect(sendBtn).toBeEnabled({ timeout: 30_000 })

    await bar(page).getByRole('button', { name: '保存并冻结版本' }).click()
    await expect.poll(() => capture.coefficientPost, { timeout: 30_000 }).not.toBeNull()

    // 用户立刻点眼睛隐藏 → 编辑器卸载,但 POST 还挂在闸门上
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByTestId('quote-coefficient-editor')).toHaveCount(0)
    await expect(sendBtn, '编辑器卸载就把发送锁松开了 —— 旧价格会被发给客户').toBeDisabled()

    // 切标签同样不许解锁(另一条会卸载编辑器的路径)
    await bar(page).getByTestId('quote-pricing-tab-default').click()
    await expect(sendBtn).toBeDisabled()

    // 反向对照:请求真正结束后必须恢复可用(否则"永远禁用"也能让上面全绿)
    releasePost()
    await expect(sendBtn).toBeEnabled({ timeout: 30_000 })
  })

  /* ────────────────────────────────────────────────────────────────
     返修 R2(Review P1):关键词行展开后的「为什么是这个价」也归同一只眼睛管。
     首轮 revealed 关在顶栏局部 state 里,这块完全不受控 —— 当面演示的真实路径。
     ──────────────────────────────────────────────────────────────── */
  test('R2/R5 · 展开关键词区永不出现进货价与加价倍率(两态皆然),基本成本构成常显', async ({ page }) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    // 展开一行关键词(移动卡与桌面行同锚点,响应式下只有一个可见)。
    // 🔴 点关键词文本而不是整行中心:行里勾选框/审计/删除等单元格带 stopPropagation,
    //    点中心会落在它们身上,行根本不展开(实测第一轮就是这么假红的)。
    const kwRow = page.locator('[data-testid="quote-kw-expand"]:visible').first()
    await kwRow.getByText('深圳办公家具批发', { exact: true }).first().click()
    const why = page.locator('[data-testid="why-this-price"]:visible').first()
    await expect(why).toBeVisible({ timeout: 30_000 })

    // ① 眼睛处于隐藏态:进货价/加价倍率一个字都不许在,连占位星号也不许留
    const hidden = await why.innerText()
    for (const s of RATIONALE_NEVER) {
      expect(hidden, `隐藏态展开区出现了「${s}」`).not.toContain(s)
    }
    // 正面对照:这块内容本身**照常渲染**(R5 起它不再受眼睛控制)——
    // 否则"整块没渲染"也能让上面的 not.toContain 全绿 = 零判别力
    for (const s of RATIONALE_PUBLIC) {
      expect(hidden, `「${s}」没渲染出来 —— 上面的"不含"断言等于没跑`).toContain(s)
    }
    // 工单 §6:内部字段名一个都不许露在客户可见区
    for (const s of MIX_INTERNAL_NAMES) {
      expect(hidden, `展开区露了内部字段名「${s}」`).not.toContain(s)
    }
    /* 三类数量之和必须等于本次交付的槽数(前端不许出现对不上的三行)。
     *
     * 🔴 [2026-09-20] 单位由「篇/条」改为「槽」,并去掉「额度」二字。
     *    产品自 e9e1ddfa7(#225-a1 ②)起渲染
     *      `重点媒体锚点 N 槽 · 行业与平台覆盖 N 槽(· 抖音图文 N 组) · 本次交付 N 槽`
     *    ——「槽」是合同分配,「条」是按发布口径估算的量,同屏不混
     *    (SSOT:`@/pages/Quote/utils/priceRationale.ts` 的 mixParts / 本次交付 一段)。
     *    本次改的**只是单位词**:验的东西没变,仍是「内部数不外露 + 三桶加起来等于总数」。
     *
     * 🔴 期望串是从**真页面 innerText 里抓出来的**,不是照着源码推的
     *    (实测渲染:`重点媒体锚点 2 槽 · 行业与平台覆盖 1 槽 · 本次交付 3 槽`)。
     *    照源码推会漏掉 `本次交付额度`→`本次交付` 这种只在文案层发生的删字。
     *
     * ⚠️ 已知盲区(留给后续,不在本单):当前 mock 里 douyinShare=0 且没有
     *    deliveryPerspective,所以 `抖音图文 N 组` 这个可选段与「本单按…约需 N 条
     *    左右(运营口径)」那段**从未被这条判据走到过** —— 它们写错了这里也不会红。
     */
    const m = hidden.match(/重点媒体锚点 (\d+) 槽 · 行业与平台覆盖 (\d+) 槽(?: · 抖音图文 (\d+) 组)? · 本次交付 (\d+) 槽/)
    expect(m, '投放组合三行没渲染成约定形态').not.toBeNull()
    expect(Number(m![1]) + Number(m![2]) + Number(m![3] ?? 0)).toBe(Number(m![4]))
    // 整页级复核:隐藏态下这些句子在任何地方都不许出现
    const pageHidden = await bodyText(page)
    for (const s of RATIONALE_SECRETS_PAGEWIDE) {
      expect(pageHidden, `隐藏态整页泄露了「${s}」`).not.toContain(s)
    }

    // ② 点眼睛显示 —— 这几个词是**删掉**的不是藏起来的,显示态同样不许回来(R5)
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByTestId('quote-pricing-mask-inline')).toHaveCount(0)  // 确认真的进了显示态
    const shown = await page.locator('[data-testid="why-this-price"]:visible').first().innerText()
    for (const s of RATIONALE_NEVER) {
      expect(shown, `显示态展开区把「${s}」放回来了 —— 它应当是删掉的,不是藏起来的`).not.toContain(s)
    }
    // 常显内容在显示态同样在(证明这块不是被眼睛切走了)
    for (const s of RATIONALE_PUBLIC) {
      expect(shown, `显示态「${s}」也没了`).toContain(s)
    }
  })

  /* ────────────────────────────────────────────────────────────────
     R3(老板 2026-08-11):报价设置栏要回到「之前的地方」—— 滚动区**外面**的固定槽位,
     往下翻关键词时它还在,不用滚回顶部才能找到。
     R1/R2 那版把它挪进了 OnlineQuoteFlow 的 overflow-y-auto,一往下翻就没了。
     ──────────────────────────────────────────────────────────────── */
  test('R3 · 报价设置栏挂在滚动区外,内容滚到底部它仍然可见', async ({ page }, testInfo) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')

    // DOM 归属:必须落在 PricingCenter 的固定槽位里,不在 OnlineQuoteFlow 的滚动容器里
    const inSlot = await page.evaluate(() => {
      const slot = document.querySelector('[data-testid="quote-pricing-bar-slot"]')
      const el = document.querySelector('[data-testid="quote-pricing-control-bar"]')
      return Boolean(slot && el && slot.contains(el))
    })
    expect(inSlot, '报价设置栏不在固定槽位里 —— 它会跟着内容一起滚走').toBe(true)

    // 底部锚点用**唯一可见**的发送按钮,不用关键词文本:
    // 🔴 关键词在响应式表格/卡片里有隐藏副本,而隐藏元素**永远**满足 not.toBeInViewport()
    //    —— 拿它当反向对照等于没有对照(第一轮实测就是这么假绿又假红的)。
    const bottomMarker = page.getByRole('button', { name: /确认无误，发送报价给客户/ })
    await expect(bottomMarker).toBeAttached()
    // 反向对照:滚动前页面底部**不在**视口内 —— 证明这一页真的能滚,
    // 否则下面"滚到底后顶栏还在"是废话(4K 视口尤其容易变成空判据)
    await expect(bottomMarker).not.toBeInViewport()

    // 位置:顶栏必须在内容**之上**(不是塞在流程条/关键词表后面 = 等于没移回来)。
    // 🔴 用 `:visible` 锚点:响应式表格/卡片有隐藏副本,隐藏元素 boundingBox() 返 null
    //    —— 这个坑我在本包里已经踩到第三次了。
    const barBox = await bar(page).boundingBox()
    const firstKwBox = await page.locator('[data-testid="quote-kw-expand"]:visible').first().boundingBox()
    expect(barBox, '顶栏没有可见区域').not.toBeNull()
    expect(firstKwBox, '关键词行没有可见区域(锚点选错了)').not.toBeNull()
    expect(barBox!.y, '顶栏跑到关键词内容下面去了').toBeLessThan(firstKwBox!.y)

    await page.evaluate(() => {
      const scrollables = Array.from(document.querySelectorAll<HTMLElement>('*')).filter(el => {
        const oy = getComputedStyle(el).overflowY
        return (oy === 'auto' || oy === 'scroll') && el.scrollHeight > el.clientHeight + 40
      })
      for (const el of scrollables) el.scrollTop = el.scrollHeight
      window.scrollTo(0, document.body.scrollHeight)
    })
    // 滚到底了(正面对照成立 —— 证明上面/下面的判断不是空跑)
    await expect(bottomMarker).toBeInViewport({ timeout: 15_000 })

    /* 🔴 「滚到底顶栏还在不在」按视口分支 —— 这不是把判据改成能过,是两种布局本来就不同,
       且**都不是本包引入的**(改造前的 QuoteMarkupCard 挂在同一个槽位,行为一模一样):
         · 桌面/4K:滚的是 OnlineQuoteFlow 内层 pane,槽位在它外面 → 顶栏被钉住;
         · mobile390:实测(2026-08-11 探针)`<main>` 自身是滚动容器(body overflow:hidden),
           槽位在 `<main>` 里 → 顶栏随整页走。
       老板这次提的是桌面(「鼠标滚一下」),移动端按既有行为如实锁,不顺手改 sticky
       —— 仓内有 iOS sticky 缩放坑,超出本次改动范围。两支各自都有断言,不存在空过。 */
    const PINNED_PROJECTS = ['desktop1440', 'uhd3840']
    if (PINNED_PROJECTS.includes(testInfo.project.name)) {
      await expect(
        bar(page),
        `${testInfo.project.name}: 滚到底后报价设置栏被滚走了`,
      ).toBeInViewport()
      await expect(bar(page).getByTestId('quote-pricing-tab-current')).toBeVisible()
    } else {
      testInfo.annotations.push({
        type: 'known-layout',
        description: `${testInfo.project.name}: <main> 自身是滚动容器,顶栏随整页滚动(改造前同址的 QuoteMarkupCard 亦然)`,
      })
      // 这一支的牙:回到顶部,顶栏必须立刻可见 —— 它就在内容最上方,不需要到处找。
      await page.evaluate(() => {
        document.querySelectorAll<HTMLElement>('*').forEach(el => { if (el.scrollTop > 0) el.scrollTop = 0 })
        window.scrollTo(0, 0)
      })
      await expect(bar(page)).toBeInViewport()
      await expect(bar(page).getByTestId('quote-pricing-tab-current')).toBeVisible()
    }
  })

  test('§11.15 · 当前视口无横向溢出,顶栏两个标签与眼睛都可点', async ({ page }, testInfo) => {
    const capture = freshCapture()
    await openPricingPage(page, capture)
    await selectSession(page, TOKEN_REVIEW, '待审核客户甲')
    await bar(page).getByTestId('quote-pricing-tab-current').click()
    await bar(page).getByTestId('quote-pricing-reveal-toggle').click()
    await expect(bar(page).getByTestId('quote-coefficient-editor')).toBeVisible()

    const overflow = await page.evaluate(() => ({
      scrollWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    }))
    expect(
      overflow.scrollWidth,
      `${testInfo.project.name}: 页面横向溢出 ${overflow.scrollWidth} > ${overflow.clientWidth}`,
    ).toBeLessThanOrEqual(overflow.clientWidth + 1)

    for (const id of ['quote-pricing-tab-default', 'quote-pricing-tab-current', 'quote-pricing-reveal-toggle']) {
      const el = bar(page).getByTestId(id)
      await expect(el).toBeVisible()
      const box = await el.boundingBox()
      expect(box, `${id} 没有可点区域`).not.toBeNull()
      expect(box!.width).toBeGreaterThan(0)
      expect(box!.height).toBeGreaterThan(0)
    }
  })
})
