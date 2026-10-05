import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §3 · 出口闸 §7-3] 计费主体 —— **真浏览器渲染锁**。
 *
 * 病史(2026-08-16 生产实证):Owner 用管理员账户给岱林开监测,界面完全不显示计费主体
 *   ⇒ 在不知情的情况下花掉了服务商 4,290 算力。
 *
 * 🔴 元素存在不算。每条都验:渲染内容正确 + 可点 + 点了真发出正确的请求。
 *
 * 锁1  管理员开通 → 出现「记谁的账」二选一,且**默认是记服务商账**
 * 锁2  选服务商账 → 最终确认**显示「记【服务商账】」**,请求**不带** billing_mode
 * 锁3  选平台账   → 最终确认**显示「记【平台账】」**,请求带 billing_mode=platform
 * 锁4  🔴 反向(**已移到后端**,理由见文件末尾):非管理员传 platform 一律 403
 */

const BRAND = 4242
const QUOTE = 900

const KEYWORDS = [
  { id: 6001, keyword: '合同词-未开', target_brand: 'A 品牌', source: 'confirmed',
    lifecycle: 'pending', is_monitored: false, monitoring_status: 'active',
    detection_rate: 0, effective_rate: 0, appearance_rate: 0, compliant_days: 0, service_days: 180 },
]

interface Seen { enables: string[] }

async function mockApp(page: Page, seen: Seen, opts: { isAdmin: boolean }) {
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
    const url = new URL(route.request().url())
    const p = url.pathname
    const full = url.pathname + url.search

    if (p === '/api/auth/me') {
      return json(route, { success: true, user: {
        id: 1, username: 'qa', display_name: 'QA',
        is_admin: opts.isAdmin, agent_level: 1, permission_version: 1, modules: ['monitoring'] } })
    }
    if (p === '/api/client-context/list') {
      return json(route, { success: true, clients: [{ id: BRAND, name: 'A 品牌', brand_type: 'client' }] })
    }
    if (p.startsWith('/api/client-context/')) {
      return json(route, { success: true, context: {
        brand: { id: BRAND, name: 'A 品牌', industry: '测试行业', diagnosis_count: 0, brand_type: 'client' },
        profile: null, materials: null, relatedQuoteIds: [String(QUOTE)],
        socialProjects: [], access_mode: 'real' } })
    }
    if (p === '/api/monitoring/clients') {
      return json(route, { status: 'success', clients: [
        { quote_id: QUOTE, brand_id: BRAND, brand_name: 'A 品牌', keyword_count: KEYWORDS.length } ] })
    }
    if (/\/api\/monitoring\/clients\/\d+\/keywords$/.test(p)) {
      return json(route, { status: 'success', keywords: KEYWORDS, super_red_ocean_keywords: [],
        service_days: 180, service_start_date: '2026-05-10', contract_end_date: '2026-12-10' })
    }
    if (p.includes('archived-keywords')) {
      return json(route, { status: 'success', success: true, keywords: [], items: [] })
    }
    if (/\/(enable|disable)$/.test(p)) {
      seen.enables.push(full)
      return json(route, { success: true })
    }
    return json(route, { success: true, status: 'success', items: [], clients: [], keywords: [], data: {} })
  })
}

async function openMonitoring(page: Page) {
  await page.goto(`/monitoring?brand_id=${BRAND}`)
  await expect(page.getByTestId('monitoring-keyword-row-6001')).toBeVisible({ timeout: 20_000 })
}

const bodyText = async (page: Page) => (await page.locator('body').innerText()).replace(/\s+/g, '')

/** 点开关 → 返回二选一弹窗是否出现 */
async function clickToggle(page: Page) {
  const sw = page.getByTestId('monitoring-keyword-row-6001').locator('button[role="switch"]').first()
  await expect(sw).toBeVisible()
  await sw.click()
}

test('锁1+锁2 管理员:出现二选一且默认记服务商账;选默认 → 显示【服务商账】· 请求不带 billing_mode', async ({ page }) => {
  const seen: Seen = { enables: [] }
  await mockApp(page, seen, { isAdmin: true })
  await openMonitoring(page)
  await clickToggle(page)

  // 锁1:二选一真的出现,且**默认那一侧**(cancel 位)写着记服务商账
  const choiceText = await bodyText(page)
  expect(choiceText, '管理员没看到「记谁的账」二选一').toContain('费用记谁的账')
  const defaultBtn = page.getByRole('button', { name: /记服务商账（默认）|记服务商账\(默认\)/ })
  await expect(defaultBtn, '🔴 默认选项不是「记服务商账」—— 默认成平台承担 = 平台成本敞口')
    .toBeVisible()
  await expect(page.getByRole('button', { name: /^记平台账$/ })).toBeVisible()

  // 选默认(记服务商账)
  await defaultBtn.click()

  // 锁2:最终确认必须**显示**扣谁的钱
  const confirmText = await bodyText(page)
  expect(confirmText, '最终确认没明示计费主体 —— Owner 这次就是"点完了也不知道花的是谁的钱"')
    .toContain('本次记【服务商账】')
  expect(confirmText).not.toContain('本次记【平台账】')

  await page.getByRole('button', { name: /^开通$/ }).click()
  await expect.poll(() => seen.enables.length, { timeout: 10_000 }).toBeGreaterThan(0)
  expect(seen.enables.some(u => u.includes('billing_mode')),
    `选了服务商账却带了 billing_mode:${JSON.stringify(seen.enables)}`).toBeFalsy()
})

test('锁3 管理员选平台账 → 显示【平台账】· 请求带 billing_mode=platform', async ({ page }) => {
  const seen: Seen = { enables: [] }
  await mockApp(page, seen, { isAdmin: true })
  await openMonitoring(page)
  await clickToggle(page)

  await page.getByRole('button', { name: /^记平台账$/ }).click()

  const confirmText = await bodyText(page)
  expect(confirmText, '选了平台账,最终确认却没显示【平台账】').toContain('本次记【平台账】')
  expect(confirmText, '同时还显示着【服务商账】—— 两个都显示等于没显示')
    .not.toContain('本次记【服务商账】')

  await page.getByRole('button', { name: /^开通$/ }).click()
  await expect.poll(() => seen.enables.length, { timeout: 10_000 }).toBeGreaterThan(0)
  expect(seen.enables.some(u => u.includes('billing_mode=platform')),
    `请求没带 billing_mode=platform:${JSON.stringify(seen.enables)}`).toBeTruthy()
})

/**
 * 🔴 锁4/锁5(非管理员看不到二选一)**在真渲染里做不了,已移走** —— 说明而不是省略:
 *
 * 实测:把 `is_admin` 翻成 false 之后,`/monitoring?brand_id=...` 会路由到 M3 工作台
 * (页面文本是「今日工作台 / 客户交付 · 按顺序做」),监测词条表**根本不渲染** ——
 * 非管理员在这个 build 里到不了这张表,所以"他看不看得到二选一"没有可观测的落点。
 *
 * ⇒ 这条反向对照移到它真正生效的那一层:**后端**。
 *   tests/platform_covered_2026_08_16/test_p3_admin_only_billing_mode.py
 *   断言非管理员传 billing_mode=platform 一律 403(工单 §3「后端不许信前端 · 并有用例」)。
 *   前端少一个入口不是安全边界,后端那一条才是。
 */
