import { expect, test, type Page } from 'playwright/test'
import { installBackend, identityFrom } from './harness'

async function openAssistant(page: Page) {
  const firstEntry = page.getByRole('button', { name: '打开小榜 GEO 助手' })
  if (await firstEntry.count()) {
    await firstEntry.click()
  } else {
    await page.locator('button.fixed.touch-none.select-none').click()
  }
  await expect(page.getByRole('heading', { name: '小榜 · GEO 助手' })).toBeVisible()
}

async function openSidebarOnMobile(page: Page) {
  if ((page.viewportSize()?.width || 1000) >= 768) return
  const trigger = page.getByRole('button', { name: '打开或收起主菜单' })
  await trigger.click()
  await expect(page.getByRole('button', { name: '切换客户' })).toBeVisible()
}

async function closeSidebarOnMobileThroughVisibleNavigation(page: Page) {
  if ((page.viewportSize()?.width || 1000) >= 768) return
  // 页面内容本身也可能有 <aside>；只锁定包含客户切换器的真实导航侧栏。
  const navigationSidebar = page.getByRole('button', { name: '切换客户' }).locator('xpath=ancestor::aside')
  const firstVisibleRoute = navigationSidebar.locator('a[href^="/"]:visible').first()
  await firstVisibleRoute.click()
  await expect(navigationSidebar).toBeHidden()
}

test('四身份（含员工）× 桌面/4K/手机：动作可达、DOM target、上下文切换与权限一致', async ({ page }, testInfo) => {
  const identity = identityFrom(testInfo)
  await installBackend(page, identity)
  await page.goto('/dashboard')
  await openAssistant(page)

  const contextBar = page.getByTestId('xiaobang-context-bar')
  await expect(contextBar).toContainText('甲品牌')
  await expect(contextBar).toContainText('今日工作台')
  await expect(contextBar).toContainText('更新于')
  await expect(page.getByTestId('xiaobang-plan-status')).toContainText('待写 2')
  for (const quick of ['今天先做什么', '这个页面怎么用', '下一步去哪', '为什么这样安排']) {
    await expect(page.getByRole('button', { name: quick })).toBeVisible()
  }

  // Drawer 挂载后，当前身份实际渲染的全部 sidebar 链接都获得稳定 DOM target。
  const isMobile = (page.viewportSize()?.width || 1000) < 768
  if (isMobile) {
    await page.locator('button[title="关闭"]').last().click()
  }
  await openSidebarOnMobile(page)
  const sidebarLinks = page.locator('aside a[href^="/"]')
  await expect.poll(() => sidebarLinks.count()).toBeGreaterThan(5)
  const parity = await sidebarLinks.evaluateAll((links) => links.map((link) => ({
    href: link.getAttribute('href'),
    target: link.getAttribute('data-help-target'),
  })))
  for (const item of parity) {
    const expected = `route-${(item.href || '/').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'root'}`
    expect(item.target, item.href || '').toBe(expected)
  }

  if (identity === 'admin') {
    expect(await page.getByText('审计日志', { exact: true }).count()).toBeGreaterThan(0)
  } else {
    await expect(page.getByText('审计日志', { exact: true })).toHaveCount(0)
  }
  if (identity === 'normal') {
    expect(await page.getByText('购买算力', { exact: true }).count()).toBeGreaterThan(0)
    await expect(page.getByText('经营总览', { exact: true })).toHaveCount(0)
  }
  if (identity === 'member') {
    // 员工能力组合允许诊断/报价/写作/监测只读，但不含团队治理、钱包或发布。
    expect(await page.getByText('品牌体检', { exact: true }).count()).toBeGreaterThan(0)
    expect(await page.getByText('报价方案', { exact: true }).count()).toBeGreaterThan(0)
    expect(await page.getByText('AI 创作中心', { exact: true }).count()).toBeGreaterThan(0)
    expect(await page.getByText('效果监测', { exact: true }).count()).toBeGreaterThan(0)
    await expect(page.getByText('发布投放', { exact: true })).toHaveCount(0)
    await expect(page.getByText('团队与席位', { exact: true })).toHaveCount(0)
    await expect(page.getByText('我的钱包', { exact: true })).toHaveCount(0)
    await expect(page.getByText('经营总览', { exact: true })).toHaveCount(0)
  }

  if (isMobile) {
    await closeSidebarOnMobileThroughVisibleNavigation(page)
    await openAssistant(page)
  }
  const input = page.getByPlaceholder('输入你的问题...')
  await input.fill('品牌体检在哪里')
  await input.press('Enter')
  const gapCard = page.getByTestId('xiaobang-gap-card')
  await expect(gapCard).toBeVisible()
  expect(await gapCard.locator('li').count()).toBeLessThanOrEqual(3)
  await expect(gapCard.locator('details')).not.toHaveAttribute('open', '')
  await expect(gapCard).toContainText('待写 2')
  await expect(gapCard.getByRole('button', { name: '去审计日志' })).toHaveCount(0)
  await gapCard.getByRole('button', { name: '去品牌体检' }).click()
  await expect(page).toHaveURL(/\/diagnosis\/new$/)
  await expect(page.locator('[data-xiaobang-highlighted="true"]')).toBeVisible()

  // 通过现役客户切换器切换；旧客户消息立即离开当前会话，再按新客户重取同一计划。
  await openSidebarOnMobile(page)
  await page.getByRole('button', { name: '切换客户' }).click()
  await page.getByText('乙品牌', { exact: true }).last().click()
  if (isMobile) {
    await closeSidebarOnMobileThroughVisibleNavigation(page)
  }
  await openAssistant(page)
  await expect(contextBar).toContainText('乙品牌')
  await expect(page.getByText('品牌体检在哪里', { exact: true })).toHaveCount(0)
  await expect(page.getByTestId('xiaobang-gap-card')).toHaveCount(0)
  await page.getByRole('button', { name: '今天先做什么' }).click()
  await expect(page.getByTestId('xiaobang-gap-card')).toContainText('乙品牌')
  await expect(page.getByTestId('xiaobang-gap-card')).toContainText('待写 1')
  await expect(page.getByTestId('xiaobang-plan-status')).toContainText('待写 1')
  await expect(contextBar).toContainText('乙品牌')

  // 历史记录按服务端已确认的客户上下文隔离；乙客户下看不到甲客户会话。
  await page.getByTitle('历史记录').click()
  await expect(page.getByText('今天先做什么', { exact: true })).toBeVisible()
  await expect(page.getByText('品牌体检在哪里', { exact: true })).toHaveCount(0)
  await page.getByTitle('关闭').last().click()

  // 切回甲客户后只恢复甲客户历史；选中它时上下文与消息同时回到甲客户。
  await openSidebarOnMobile(page)
  await page.getByRole('button', { name: '切换客户' }).click()
  await page.getByText('甲品牌', { exact: true }).last().click()
  if (isMobile) {
    await closeSidebarOnMobileThroughVisibleNavigation(page)
  }
  await openAssistant(page)
  await expect(contextBar).toContainText('甲品牌')
  await page.getByTitle('历史记录').click()
  await expect(page.getByText('品牌体检在哪里', { exact: true })).toBeVisible()
  await expect(page.getByText('今天先做什么', { exact: true })).toHaveCount(0)
  await page.getByText('品牌体检在哪里', { exact: true }).click()
  await expect(page.getByText('品牌体检在哪里', { exact: true })).toBeVisible()
  await expect(contextBar).toContainText('甲品牌')

  const bodyText = await page.locator('body').innerText()
  expect(bodyText).not.toContain('销售分组')
  expect(bodyText).not.toContain('运营分组')
  const overflow = await page.evaluate(() => ({
    document: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    body: document.body.scrollWidth - document.body.clientWidth,
  }))
  expect(overflow.document).toBeLessThanOrEqual(1)
  expect(overflow.body).toBeLessThanOrEqual(1)
})
