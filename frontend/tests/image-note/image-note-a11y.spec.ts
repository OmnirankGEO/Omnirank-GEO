import AxeBuilder from '@axe-core/playwright'
import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * GEO 图文三页 · axe 无障碍基线(规格 01 §8「加 axe 基线;关键违规为 0」)
 *
 * 🔴 判据是 `critical` + `serious` 两档**逐条列出**后断言为空数组,
 *    不是"违规数 <= N"。后者会随页面变大悄悄放宽。
 *
 * 🔴 本套单独成文件的理由:axe 注入需要页面完全稳定,与交互测试混在一起
 *    会被中途的路由/状态切换污染,变成随机红。
 */

const BRAND = {
  id: 101, name: '无障碍判据客户', industry: 'home_improvement',
  diagnosis_count: 0, quote_count: 1, created_at: '2026-08-17T00:00:00Z',
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function mockApp(page: Page) {
  await page.addInitScript((brandId) => {
    localStorage.setItem('omnirank_token', 'qa-token')
    sessionStorage.setItem(`omnirank_current_brand_candidate:1`, String(brandId))
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-08-17T00:00:00Z', last_updated_at: '2026-08-17T00:00:00Z',
    }))
  }, BRAND.id)

  await page.route('**/api/**', async (route) => json(route, { success: true }))
  await page.route('**/api/auth/me', async (route) => json(route, {
    success: true,
    user: {
      id: 1, username: 'qa-owner', display_name: '质检服务商', is_admin: false,
      is_active: 1, must_change_password: 0, roles: [],
      permissions: ['writing:read', 'writing:write'],
      client_brand_ids: [BRAND.id], agent_level: 1, permission_version: 1,
    },
  }))
  await page.route('**/api/client-context/list', async (route) => json(route, {
    success: true, clients: [BRAND],
  }))
  await page.route(`**/api/client-context/${BRAND.id}`, async (route) => json(route, {
    success: true,
    context: {
      brand: { id: BRAND.id, name: BRAND.name, industry: BRAND.industry, diagnosis_count: 0 },
      profile: null, materials: null, relatedQuoteIds: [1], socialProjects: [],
    },
  }))
  await page.route('**/api/writing/projects', async (route) => json(route, {
    success: true,
    projects: [{ id: 1, name: BRAND.name, brand_name: BRAND.name, brand_id: BRAND.id,
                 industry: BRAND.industry, quote_ids: [1] }],
  }))
  await page.route('**/api/geo-douyin/posts/*', async (route) => json(route, {
    post: {
      geo_post_id: 77, post_revision_id: 3, global_ordinal: 18, quote_total: 20,
      channel_display_index: 5, channel_total: 12,
      brand_name: BRAND.name, quote_label: '报价 #1',
      title: '判据标题', body: '判据正文', hashtags: [], contact_enabled: false,
      ready: true,
      cards: [{ idx: 0, kind: 'cover', entity: '真实品牌', headline: '封面', status: 'ready',
                image_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=' }],
    },
  }))
}

/** critical + serious 两档,逐条列出违规 id + 命中的元素,失败时看得懂。
 *
 * 🔴 `selector` 把扫描**限定在本包拥有的页面容器**内。
 *    第一版扫全 document,三条全红,而三条红的是同一个东西:
 *    全局侧栏 `a[data-guide="today-board"]` 与 `.text-muted-foreground/50 .text-[10px]`
 *    的 color-contrast —— 那是既有 chrome,不是本包写的,改它会动到别人的 UI。
 *    把别人的存量红算进本包的基线,基线就永远不可能绿,而**长期红的判据等于没有判据**。
 *    存量那条不隐藏:见下方 `既有全局 chrome 的存量违规` 那一条,它把数字打印出来。
 */
async function criticalViolations(page: Page, selector: string) {
  const results = await new AxeBuilder({ page })
    .include(selector)
    .withTags(['wcag2a', 'wcag2aa'])
    .analyze()
  return results.violations
    .filter(v => v.impact === 'critical' || v.impact === 'serious')
    .map(v => `${v.impact}:${v.id} @ ${v.nodes.slice(0, 2).map(n => n.target.join(' ')).join(' | ')}`)
}

test('第一页:axe 关键违规为 0', async ({ page }) => {
  await mockApp(page)
  await page.goto('/writing')
  await page.getByRole('tab', { name: /制作 GEO 图文/ }).click()
  await expect(page.getByTestId('image-note-batch')).toBeVisible()
  expect(await criticalViolations(page, '[data-testid="image-note-batch"]')).toEqual([])
})

test('第二页:axe 关键违规为 0', async ({ page }) => {
  await mockApp(page)
  await page.goto('/writing/image-note/77')
  await expect(page.getByTestId('image-note-proof')).toBeVisible()
  expect(await criticalViolations(page, '[data-testid="image-note-proof"]')).toEqual([])
})

test('发布页:axe 关键违规为 0', async ({ page }) => {
  await mockApp(page)
  await page.goto('/publish?media_type=imagenote')
  await expect(page.getByTestId('image-note-panel')).toBeVisible()
  expect(await criticalViolations(page, '[data-testid="image-note-panel"]')).toEqual([])
})

test('axe 探针本身有判别力(注入一个已知违规必须被抓到)', async ({ page }) => {
  /* 🔴 三条全绿也可能是 axe 根本没跑起来。注入一个教科书级违规
     (无可读名称的按钮 + 对比度极低的文字),必须至少报一条 —— 否则
     上面的 `toEqual([])` 就是恒真。 */
  await mockApp(page)
  await page.goto('/writing')
  await page.getByRole('tab', { name: /制作 GEO 图文/ }).click()
  await expect(page.getByTestId('image-note-batch')).toBeVisible()
  // 🔴 注入点必须在**被扫描的那个容器内**。第一版注入 document.body,
  //    而扫描已经 `.include('[data-testid=image-note-batch]')` —— 探针根本
  //    没落进样本,于是"抓不到"被误读成"探针没判别力"。
  //    本仓刚为「驳回反驳前先证明探针打得进正样本」交过学费,同一形状。
  await page.evaluate(() => {
    const host = document.querySelector('[data-testid="image-note-batch"]')!
    const btn = document.createElement('button')
    btn.setAttribute('id', '__axe_probe__')
    host.appendChild(btn)                   // 无可读名称
    const img = document.createElement('img')
    img.src = 'data:image/gif;base64,R0lGODlhAQABAAAAACw='
    host.appendChild(img)                   // 无 alt
  })
  const found = await criticalViolations(page, '[data-testid="image-note-batch"]')
  expect(found.length, 'axe 没有抓到注入的已知违规 = 它根本没在跑').toBeGreaterThan(0)
})

test('既有全局 chrome 的存量违规:如实打印,不并入本包基线', async ({ page }) => {
  /* 🔴 存量红不是"不存在",是"不属于本包"。把它打印出来而不是静默过滤 ——
     静默过滤和从来没测过在结果上长得一模一样。
     当前实测:全局侧栏 color-contrast(`a[data-guide="today-board"]`、
     `.text-muted-foreground/50 .text-[10px]`),3 个页面各命中一次同一族。
     归属:全局 chrome,不在 GEO 图文包的改动面内,已如实上报。 */
  await mockApp(page)
  await page.goto('/writing')
  await page.getByRole('tab', { name: /制作 GEO 图文/ }).click()
  await expect(page.getByTestId('image-note-batch')).toBeVisible()
  const wholePage = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa']).analyze()
  const legacy = wholePage.violations
    .filter(v => v.impact === 'critical' || v.impact === 'serious')
    .map(v => `${v.impact}:${v.id}`)
  console.log('[a11y] 全页(含既有 chrome)关键违规:', legacy.length ? legacy : '无')
  // 只断言"探针跑起来了",不把别人的存量红变成本包的门禁
  expect(Array.isArray(wholePage.violations)).toBe(true)
})
