import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * 客户反馈⑤ · 监测中心"一直刷新" —— **真浏览器行为锁**(Review 2026-08-09 P1-3 要求)
 *
 * 此前 ⑤ 只有源码接线锁 + 纯函数锁,证明不了 React 生命周期里的真实行为。
 * 本文件跑的是**真 app**:真路由、真 Provider、真 ClientContext、真 lib/api 失效广播。
 *
 * 复现的那条链(修复前):
 *   lib/api.ts 任一命中 brands/customers/my-clients 的**写操作成功** → 广播失效
 *   → ClientContext.loadClientContext(force=true) 旧写法 `clientContextCache.delete(key)`
 *     后紧跟 `setClientContext(cached)`,而 delete 之后 cached 必为 null → 上下文被**同步置空**
 *   → Monitoring 的 useLayoutEffect(旧判据含 activeAccessMode)看到 live→pending 翻转
 *   → 关键词/发布/趋势/结果 state 全部清空 → 数据回来再填 = 页面"跳一下"。
 *
 * 六条断言按 Review 点名逐条落:
 *   锁1 词条区可见且滚到视口
 *   锁2 触发一次**成功的**品牌资料写操作(真走 authFetch → 真触发失效广播)
 *   锁3 人为把 client-context 刷新响应拖慢(拖慢期间才是原 bug 的窗口)
 *   锁4 旧词条**持续可见**:不空态、不骨架、不跳位
 *   锁5 响应回来后只更新一次(不重复刷)
 *   锁6 **真切品牌**时旧数据必须清掉(反向对照 —— 否则锁4 可能只是"永远不清")
 */

const BRAND_A = 4242
const BRAND_B = 4343
const QUOTE_A = 900
const QUOTE_B = 901

/** 关键词行数够多才能撑出滚动,滚动位移才有意义 */
const KEYWORDS_A = Array.from({ length: 24 }, (_, i) => ({
  id: 1000 + i,
  keyword: `甲方关键词-${i + 1}`,
  target_brand: 'A 品牌',
  lifecycle: 'monitoring',
  is_monitored: true,
  monitoring_status: 'active',
  appearance_rate: 40 + (i % 30),
  compliant_days: 3,
  service_days: 180,
  source: 'confirmed',
}))
const KEYWORDS_B = [{
  id: 2001, keyword: '乙方关键词-唯一', target_brand: 'B 品牌', lifecycle: 'monitoring',
  is_monitored: true, monitoring_status: 'active', appearance_rate: 11,
  compliant_days: 1, service_days: 180, source: 'confirmed',
}]

const brandRow = (id: number, name: string) => ({
  id, name, brand_name: name, access_mode: 'real', is_test: false,
})

/** 形状按 `lib/api.ts:ClientContextDetail` 逐字来 —— 少一个键前端会判"响应不完整"并停加载 */
const contextBody = (id: number, name: string) => ({
  success: true,
  context: {
    brand: { id, name, industry: '测试行业', diagnosis_count: 0, brand_type: 'client' },
    profile: null,
    materials: null,
    relatedQuoteIds: [String(id === BRAND_A ? QUOTE_A : QUOTE_B)],
    socialProjects: [],
    access_mode: 'real',
  },
})

interface Knobs {
  /** client-context 详情接口的延迟(毫秒)—— 原 bug 的窗口就在这段时间里 */
  contextDelayMs: number
  /** 计数:词条接口被打了几次 */
  keywordHits: { n: number }
  /** 计数:client-context 详情被打了几次 */
  contextHits: { n: number }
}

async function mockApp(page: Page, knobs: Knobs) {
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
    const method = route.request().method()

    if (p === '/api/auth/me') {
      return json(route, {
        success: true,
        user: { id: 1, username: 'qa', display_name: 'QA', is_admin: true,
                agent_level: 1, permission_version: 1, modules: ['monitoring'] },
      })
    }
    if (p === '/api/client-context/list') {
      return json(route, { success: true, clients: [brandRow(BRAND_A, 'A 品牌'), brandRow(BRAND_B, 'B 品牌')] })
    }
    if (p.startsWith('/api/client-context/')) {
      knobs.contextHits.n += 1
      const id = Number(p.split('/').pop())
      if (knobs.contextDelayMs > 0) await new Promise(r => setTimeout(r, knobs.contextDelayMs))
      return json(route, contextBody(id, id === BRAND_A ? 'A 品牌' : 'B 品牌'))
    }
    if (p === '/api/monitoring/clients') {
      return json(route, {
        status: 'success',
        clients: [
          { quote_id: QUOTE_A, brand_id: BRAND_A, brand_name: 'A 品牌', keyword_count: KEYWORDS_A.length },
          { quote_id: QUOTE_B, brand_id: BRAND_B, brand_name: 'B 品牌', keyword_count: KEYWORDS_B.length },
        ],
      })
    }
    if (/\/api\/monitoring\/clients\/\d+\/keywords$/.test(p)) {
      knobs.keywordHits.n += 1
      const qid = Number(p.split('/')[4])
      return json(route, {
        status: 'success',
        keywords: qid === QUOTE_A ? KEYWORDS_A : KEYWORDS_B,
        super_red_ocean_keywords: [],
        service_days: 180,
        service_start_date: '2026-05-10',
        contract_end_date: '2026-06-10',
        service_remaining_days_natural_signed: -60,
        // ⑥ 的真实排班资格(顺带保证横幅在真浏览器里也读的是后端结论)
        auto_monitoring: { active: true, code: 'compliance_driven',
                           message: '服务期日历已过,自动监测仍在按达标天数继续跑' },
      })
    }
    // 🔴 锁2 的关键:这是一次**成功的写操作**,且路径命中 brands —— lib/api 会广播失效。
    if (/\/api\/brands\/\d+$/.test(p) && method === 'PUT') {
      return json(route, { success: true })
    }
    return json(route, { success: true, status: 'success', items: [], clients: [], data: {} })
  })
}

async function openMonitoring(page: Page) {
  await page.goto(`/monitoring?brand_id=${BRAND_A}`)
  await expect(page.getByTestId(`monitoring-keyword-row-${KEYWORDS_A[0].id}`)).toBeVisible({ timeout: 20_000 })
}

/** 触发一次成功写操作 —— 走页面自己的 authFetch,不绕过 lib/api 的失效广播 */
async function triggerSuccessfulBrandWrite(page: Page, brandId: number) {
  return page.evaluate(async (id) => {
    const importer = new Function('return import("/src/lib/api.ts")') as () => Promise<any>
    const api = await importer()
    const res = await api.authFetch(`/api/brands/${id}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ industry: '改一下行业' }),
    })
    return res.ok
  }, brandId)
}

test('锁1-5 · 成功写操作期间,旧词条持续可见、不空态、不跳位,响应回来只更新一次', async ({ page }) => {
  const knobs: Knobs = { contextDelayMs: 0, keywordHits: { n: 0 }, contextHits: { n: 0 } }
  await mockApp(page, knobs)
  await openMonitoring(page)

  // 锁1:滚到词条区,记住滚动位置(位移是"跳一下"最直观的判据)
  const lastRow = page.getByTestId(`monitoring-keyword-row-${KEYWORDS_A[KEYWORDS_A.length - 1].id}`)
  await lastRow.scrollIntoViewIfNeeded()
  const scrollBefore = await page.evaluate(() => window.scrollY)
  const keywordHitsBefore = knobs.keywordHits.n

  // 锁3:把刷新响应拖慢 —— 修复前正是这段时间里整页被清空
  knobs.contextDelayMs = 1500

  // 🔴 锁4 的判据形状是本文件最重要的一条经验:
  //   **不能用 `expect(locator).toBeVisible()`** —— Playwright 的断言自动重试,
  //   "词条短暂消失又回来"正好会被重试等没了,锁在修复前的代码上照样绿(实测过,零判别力)。
  //   改成在页面内装一个采样记录器,用 rAF 连续记录**观测到的最小行数**与滚动位移极值;
  //   闪一帧就会被记下来。
  await page.evaluate(() => {
    const w = window as any
    w.__flicker = { minRows: Number.POSITIVE_INFINITY, sawEmptyState: false,
                    maxScrollDelta: 0, baseScroll: window.scrollY, samples: 0 }
    const tick = () => {
      const rows = document.querySelectorAll('[data-testid^="monitoring-keyword-row-"]').length
      w.__flicker.minRows = Math.min(w.__flicker.minRows, rows)
      if (document.querySelector('[data-testid="monitoring-keyword-empty"]')) w.__flicker.sawEmptyState = true
      w.__flicker.maxScrollDelta = Math.max(
        w.__flicker.maxScrollDelta, Math.abs(window.scrollY - w.__flicker.baseScroll))
      w.__flicker.samples += 1
      w.__flicker.raf = requestAnimationFrame(tick)
    }
    tick()
  })

  // 锁2:成功写操作(记录器已就位,整段刷新窗口都在采样)
  expect(await triggerSuccessfulBrandWrite(page, BRAND_A)).toBe(true)
  await page.waitForTimeout(2200)          // 覆盖 1500ms 延迟 + 落地渲染
  knobs.contextDelayMs = 0
  await page.waitForTimeout(600)

  const flicker = await page.evaluate(() => {
    const w = window as any
    cancelAnimationFrame(w.__flicker.raf)
    return w.__flicker as { minRows: number; sawEmptyState: boolean; maxScrollDelta: number; samples: number }
  })

  // 元判据:采样器真的跑起来了(样本太少的话下面三条都是"没人反对"式的假绿)
  expect(flicker.samples).toBeGreaterThan(50)
  // 锁4:整段窗口里词条一行都没少过
  expect(flicker.minRows).toBe(KEYWORDS_A.length)
  expect(flicker.sawEmptyState).toBe(false)
  // 不跳位(允许 2px 取整误差)
  expect(flicker.maxScrollDelta).toBeLessThanOrEqual(2)

  // 锁5:刷新落地后,词条接口最多被重打一次(不是反复刷)
  await expect(page.getByTestId(`monitoring-keyword-row-${KEYWORDS_A[0].id}`)).toBeVisible()
  expect(knobs.keywordHits.n - keywordHitsBefore).toBeLessThanOrEqual(1)
})

test('锁6 · 反向对照:真切品牌时旧品牌的词必须清掉(证明锁4 不是"永远不清")', async ({ page }) => {
  const knobs: Knobs = { contextDelayMs: 0, keywordHits: { n: 0 }, contextHits: { n: 0 } }
  await mockApp(page, knobs)
  await openMonitoring(page)

  await page.goto(`/monitoring?brand_id=${BRAND_B}`)
  await expect(page.getByTestId(`monitoring-keyword-row-${KEYWORDS_B[0].id}`)).toBeVisible({ timeout: 20_000 })
  // A 品牌的词一条都不许留 —— 留了就是跨品牌串数据,比闪一下严重得多
  await expect(page.getByTestId(`monitoring-keyword-row-${KEYWORDS_A[0].id}`)).toHaveCount(0)
})
