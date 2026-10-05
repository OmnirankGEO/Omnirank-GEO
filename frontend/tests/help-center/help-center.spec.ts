import { mkdirSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type Page, type Route } from 'playwright/test'

type ViewerKind = 'agent' | 'normal'

type HelpDoc = {
  title: string
  body: string
}

const docs = JSON.parse(
  readFileSync(resolve(process.cwd(), '..', 'api', 'help_docs_content.json'), 'utf8'),
) as Record<string, HelpDoc>

const screenshotDir = resolve(
  process.cwd(),
  process.env.QA_HELP_CENTER_SCREENSHOT_DIR || '../_qa_help_center_artifacts/screenshots',
)
mkdirSync(screenshotDir, { recursive: true })

const viewports = [
  { name: '320', width: 320, height: 720 },
  { name: '390', width: 390, height: 844 },
  { name: '768', width: 768, height: 1024 },
  { name: '1366', width: 1366, height: 768 },
  { name: '1920', width: 1920, height: 1080 },
] as const

const agentOnly = [
  'provider-guide',
  'leads',
  'stock-up',
  'set-pricing',
  'profit',
  'agent-wallet',
  'wallet-bank-cards',
  'agent-settlement',
  'agent-promotion',
  'agent-agreement',
]
const normalOnly = ['standard-guide', 'customer-wallet', 'customer-recharge']
const l2Only = ['wallet-service-fee-history']
const adminOnly = ['team-members', 'roles', 'audit-log']

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

async function installBackend(page: Page, viewer: ViewerKind) {
  const agent = viewer === 'agent'
  const user = {
    id: agent ? 210 : 211,
    user_id: agent ? 210 : 211,
    username: agent ? 'help-agent' : 'help-normal',
    display_name: agent ? '服务商运营' : '普通用户',
    is_admin: false,
    is_active: 1,
    must_change_password: 0,
    agent_level: agent ? 1 : 0,
    roles: [],
    permissions: [],
    client_brand_ids: [],
  }
  const hiddenSlugs = agent
    ? [...normalOnly, ...l2Only, ...adminOnly]
    : [...agentOnly, ...l2Only, ...adminOnly]

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'help-center-test-token')
    localStorage.setItem('sidebar-collapsed', 'false')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'returning',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-26T00:00:00Z',
      last_updated_at: '2026-07-26T00:00:00Z',
    }))
  })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/auth/me') return json(route, { success: true, user })
    if (path === '/api/help/docs/acl') {
      return json(route, {
        role: viewer,
        hidden_slugs: hiddenSlugs,
        agent_only_slugs: agentOnly,
        normal_only_slugs: normalOnly,
        l2_only_slugs: l2Only,
        admin_only_slugs: adminOnly,
      })
    }
    if (path.startsWith('/api/help/docs/')) {
      const slug = decodeURIComponent(path.slice('/api/help/docs/'.length))
      if (hiddenSlugs.includes(slug)) {
        return json(route, { detail: '无权访问该帮助文档' }, 403)
      }
      const doc = docs[slug]
      return doc
        ? json(route, { slug, title: doc.title, body: doc.body })
        : json(route, { detail: '帮助文档不存在' }, 404)
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 })
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] })
    if (path === '/api/wallet') return json(route, { success: true, balance: 0, available_balance: 0 })
    if (path.includes('/agreement')) return json(route, { success: true, required: false })
    return json(route, {
      success: true,
      data: {},
      items: [],
      clients: [],
      brands: [],
      total: 0,
    })
  })
}

async function expectNoHorizontalOverflow(page: Page) {
  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    body: document.body.scrollWidth - document.body.clientWidth,
  }))
  expect(overflow.document, `document horizontal overflow: ${overflow.document}px`).toBeLessThanOrEqual(1)
  expect(overflow.body, `body horizontal overflow: ${overflow.body}px`).toBeLessThanOrEqual(1)
}

test('[Help] /help 直接进入服务商说明书，目录可搜索和跳转', async ({ page }) => {
  await installBackend(page, 'agent')
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.goto('/help')

  await expect(page.getByRole('heading', { name: '服务商完整操作说明书' })).toBeVisible()
  const docTree = page.locator('aside').filter({ hasText: '服务商工作路径' })
  await expect(docTree.getByText('全域上榜GEO交付系统')).toBeVisible()
  await expect(docTree.getByText('服务商工作路径')).toBeVisible()
  await expect(page.getByText('普通用户完整操作说明书')).toHaveCount(0)
  await expect(docTree.getByRole('link', { name: '打开视频教程主页' })).toBeVisible()

  const search = docTree.getByPlaceholder('搜索操作说明')
  await search.fill('品牌体检')
  await expect(docTree.getByRole('link', { name: '品牌体检' })).toBeVisible()
  await docTree.getByRole('link', { name: '品牌体检' }).click()
  await expect(page).toHaveURL(/\/help\/docs\/diagnosis$/)
  await expect(page.getByRole('heading', { name: '1. 品牌体检', exact: true })).toBeVisible()
  await expect(page.getByText('品牌疑似命中')).toBeVisible()
  await expectNoHorizontalOverflow(page)
})

test('[Help] 两个关键教程入口高对比并尊重减弱动效设置', async ({ page }) => {
  await installBackend(page, 'agent')
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.goto('/help')

  const videoHomeCta = page.locator('[data-help-cta="video-home"]')
  await expect(videoHomeCta).toBeVisible()
  await expect(videoHomeCta).toHaveClass(/bg-emerald-400/)
  await expect.poll(
    () => videoHomeCta.evaluate((element) => getComputedStyle(element).animationName),
  ).toContain('pulse-subtle')

  await page.goto('/help/home')
  const restartCta = page.locator('[data-help-cta="restart-tutorial"]')
  await expect(restartCta).toBeVisible()
  await expect(restartCta).toHaveClass(/bg-emerald-400/)
  await expect.poll(
    () => restartCta.evaluate((element) => getComputedStyle(element).animationName),
  ).toContain('pulse-subtle')

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await expect.poll(
    () => restartCta.evaluate((element) => getComputedStyle(element).animationName),
  ).toBe('none')
})

test('[Help] 普通用户默认看到自己的操作说明书，代理经营文档不下发', async ({ page }) => {
  await installBackend(page, 'normal')
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.goto('/help')

  await expect(page.getByRole('heading', { name: '普通用户完整操作说明书' })).toBeVisible()
  const docTree = page.locator('aside').filter({ hasText: '普通用户工作路径' })
  await expect(docTree.getByText('普通用户工作路径')).toBeVisible()
  await expect(page.getByText('服务商完整操作说明书')).toHaveCount(0)
  await expect(docTree.getByText('算力库存')).toHaveCount(0)
  await expectNoHorizontalOverflow(page)
})

test('[Help] /m3/help 仍兼容重定向到说明书', async ({ page }) => {
  await installBackend(page, 'agent')
  await page.goto('/m3/help')

  await expect(page).toHaveURL(/\/help$/)
  await expect(page.getByRole('heading', { name: '服务商完整操作说明书' })).toBeVisible()
})

test('[Help] 服务商视频主页恢复完整四步并可返回说明书', async ({ page }) => {
  await installBackend(page, 'agent')
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.goto('/help/home')

  await expect(page.getByRole('heading', { name: '视频教程主页' })).toBeVisible()
  await expect(page.getByRole('heading', { name: /5 分钟看懂.*怎么用/ })).toBeVisible()
  await expect(page.getByRole('heading', { name: '重走新手教程' })).toBeVisible()
  await expect(page.getByRole('button', { name: '开始教程' })).toBeVisible()
  for (const step of ['做品牌诊断', '出报价方案', 'AI 写文章并发布', '持续监测效果']) {
    await expect(page.getByRole('heading', { name: step, exact: true })).toBeVisible()
  }
  await expect(page.getByRole('link', { name: '打开操作说明书' })).toHaveAttribute('href', '/help')
  await expectNoHorizontalOverflow(page)
  await page.screenshot({
    path: resolve(screenshotDir, 'help-home-agent-1366.png'),
    fullPage: true,
  })
})

test('[Help] 普通用户视频主页只展示自己的任务', async ({ page }) => {
  await installBackend(page, 'normal')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/help/home')

  await expect(page.getByRole('heading', { name: '从 AI 写作到效果复盘' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'AI 写文章并发布', exact: true }).first()).toBeVisible()
  await expect(page.getByRole('heading', { name: '查看内容效果', exact: true }).first()).toBeVisible()
  await expect(page.getByRole('heading', { name: '重走新手教程' })).toHaveCount(0)
  for (const forbidden of ['报价', '客户线索', '提现结算', '算力库存', '推广获客']) {
    await expect(page.getByText(forbidden, { exact: false })).toHaveCount(0)
  }
  await expectNoHorizontalOverflow(page)
  await page.screenshot({
    path: resolve(screenshotDir, 'help-home-normal-390.png'),
    fullPage: true,
  })
})

test('[Help] 重走教程会清理旧进度并真实进入教程模式', async ({ page }) => {
  await installBackend(page, 'agent')
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_sandbox_intro_shown', '1')
    localStorage.setItem('omnirank_sandbox_quote_paths', 'old-path')
    localStorage.setItem('omnirank_sandbox_tutorial_stage', '3')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'returning',
      completed_steps: ['diagnosis'],
      skipped_steps: ['quote'],
      dismissed_features: ['feature-tour'],
      viewed_videos: [],
      first_seen_at: '2026-07-26T00:00:00Z',
      last_updated_at: '2026-07-26T00:00:00Z',
    }))
    window.addEventListener('sandbox:change', () => {
      sessionStorage.setItem('qa-sandbox-change-observed', '1')
    })
  })
  await page.goto('/help/home')
  await page.getByRole('button', { name: '开始教程' }).click()

  await expect.poll(() => page.evaluate(() => localStorage.getItem('omnirank_sandbox_active'))).toBe('1')
  await expect.poll(() => page.evaluate(
    () => sessionStorage.getItem('qa-sandbox-change-observed'),
  )).toBe('1')

  const tutorialState = await page.evaluate(() => ({
    intro: localStorage.getItem('omnirank_sandbox_intro_shown'),
    quotePaths: localStorage.getItem('omnirank_sandbox_quote_paths'),
    stage: localStorage.getItem('omnirank_sandbox_tutorial_stage'),
    onboarding: JSON.parse(localStorage.getItem('omnirank_onboarding_state') || '{}'),
  }))
  expect(tutorialState.intro).toBeNull()
  expect(tutorialState.quotePaths).toBeNull()
  expect(tutorialState.stage).toBeNull()
  expect(tutorialState.onboarding.completed_steps).toEqual([])
  expect(tutorialState.onboarding.skipped_steps).toEqual([])
  expect(tutorialState.onboarding.dismissed_features).toEqual([])
})

for (const viewport of viewports) {
  test(`[Help] ${viewport.name}px 角色手册布局稳定`, async ({ page }) => {
    await installBackend(page, 'agent')
    await page.setViewportSize({ width: viewport.width, height: viewport.height })
    await page.goto('/help')

    await expect(page.getByRole('heading', { name: '服务商完整操作说明书' })).toBeVisible()
    if (viewport.width < 1024) {
      const treeButton = page.getByRole('button', { name: '打开目录' })
      await expect(treeButton).toBeVisible()
      await treeButton.click()
      const mobileTree = page.locator('#mobile-help-tree')
      await expect(mobileTree.getByPlaceholder('搜索操作说明')).toBeVisible()
      await expect(mobileTree.getByRole('link', { name: '品牌体检' })).toBeVisible()
    } else {
      await expect(page.getByText('服务商工作路径')).toBeVisible()
    }
    await expectNoHorizontalOverflow(page)
    await page.screenshot({
      path: resolve(screenshotDir, `help-center-agent-${viewport.name}.png`),
      fullPage: true,
    })
  })
}
