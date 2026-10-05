import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { devices, expect, test, type Page, type Route } from 'playwright/test'

const viewports = [
  { width: 320, height: 720 },
  { width: 390, height: 844 },
  { width: 768, height: 1024 },
  { width: 1366, height: 768 },
  { width: 1920, height: 1080 },
] as const

test.beforeEach(async ({ page }) => {
  await page.goto('/login')
})

function fulfillJson(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

type SandboxBootstrapOptions = {
  stage?: string
  completedSteps?: string[]
}

async function installCanonicalSandboxBackend(
  page: Page,
  options: SandboxBootstrapOptions = {},
) {
  await page.addInitScript(({ stage, completedSteps }) => {
    localStorage.setItem('omnirank_token', 'sandbox-canonical-layout-token')
    localStorage.setItem('omnirank_sandbox_active', '1')
    localStorage.setItem('omnirank_sandbox_intro_shown', '1')
    if (!localStorage.getItem('omnirank_sandbox_tutorial_stage')) {
      localStorage.setItem('omnirank_sandbox_tutorial_stage', stage)
    }
    localStorage.setItem('sidebar-collapsed', 'true')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'start',
      completed_steps: completedSteps,
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-28T00:00:00.000Z',
      last_updated_at: '2026-07-28T00:00:00.000Z',
    }))
  }, {
    stage: options.stage || 'step4-sidebar',
    completedSteps: options.completedSteps || [],
  })

  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/auth/me') {
      return fulfillJson(route, {
        success: true,
        user: {
          id: 9301,
          user_id: 9301,
          username: 'sandbox-standard-user',
          display_name: '用户自定义名称',
          is_admin: false,
          is_active: 1,
          must_change_password: 0,
          agent_level: 0,
          roles: [],
          permissions: [],
          client_brand_ids: [],
        },
      })
    }
    if (path === '/api/organization/overview') {
      return fulfillJson(route, { detail: { code: 'NOT_FOUND', message: 'not found' } }, 404)
    }
    if (path === '/api/notifications/unread-count') return fulfillJson(route, { count: 0 })
    if (path === '/api/client-context/list') {
      return fulfillJson(route, { success: true, clients: [] })
    }
    if (path === '/api/wallet') {
      return fulfillJson(route, { success: true, balance: 1, available_balance: 1 })
    }
    if (path.includes('/agreement')) {
      return fulfillJson(route, { success: true, required: false })
    }
    return fulfillJson(route, {
      success: true,
      data: {},
      items: [],
      clients: [],
      brands: [],
      total: 0,
    })
  })
}

for (const viewport of viewports) {
  test(`真实交互目标在 ${viewport.width}x${viewport.height} 可定位且可点击`, async ({ page }) => {
    await page.setViewportSize(viewport)
    const result = await page.evaluate(async () => {
      const coach = await import('/src/components/onboarding/coachTarget.ts')
      document.body.innerHTML = `
        <div id="wrapper" style="position:fixed;right:12px;bottom:18px;padding:8px">
          <button id="real-button" data-coach-target="true"
            style="width:120px;height:44px;pointer-events:auto">继续</button>
        </div>`
      let clicks = 0
      const wrapper = document.querySelector('#wrapper') as HTMLElement
      const button = document.querySelector('#real-button') as HTMLButtonElement
      button.addEventListener('click', () => { clicks += 1 })
      const target = coach.resolveCoachTarget(wrapper)
      const rect = coach.measureCoachTarget(target)
      target?.click()
      return {
        isButton: target === button,
        actionable: coach.isCoachTargetActionable(target),
        visibleFraction: coach.coachRectVisibleFraction(rect),
        clicks,
        rect,
        viewport: coach.getCoachViewport(),
      }
    })
    expect(result.isButton).toBe(true)
    expect(result.actionable).toBe(true)
    expect(result.visibleFraction).toBeGreaterThan(0.99)
    expect(result.clicks).toBe(1)
    expect(result.rect?.left).toBeGreaterThanOrEqual(result.viewport.left)
    expect((result.rect?.left || 0) + (result.rect?.width || 0)).toBeLessThanOrEqual(result.viewport.right)
  })
}

test('禁用目标不冒充可点击按钮，布局变化后重新测量', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const coach = await import('/src/components/onboarding/coachTarget.ts')
    document.body.innerHTML = `
      <div id="wrapper" style="position:fixed;left:20px;top:20px">
        <button id="real-button" disabled style="width:100px;height:40px">继续</button>
      </div>`
    const wrapper = document.querySelector('#wrapper') as HTMLElement
    const button = document.querySelector('#real-button') as HTMLButtonElement
    const disabled = coach.isCoachTargetActionable(button)
    button.disabled = false
    const first = coach.measureCoachTarget(coach.resolveCoachTarget(wrapper))
    wrapper.style.left = '160px'
    const second = coach.measureCoachTarget(coach.resolveCoachTarget(wrapper))
    return {
      disabled,
      moved: !!first && !!second && !coach.sameCoachRect(first, second),
      second,
    }
  })
  expect(result.disabled).toBe(false)
  expect(result.moved).toBe(true)
  expect(result.second?.left).toBe(160)
})

test('响应式页面存在隐藏副本时选择可见的真实目标', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const coach = await import('/src/components/onboarding/coachTarget.ts')
    document.body.innerHTML = `
      <button class="shared-target" style="display:none">隐藏桌面副本</button>
      <button class="shared-target" style="width:140px;height:42px">当前视口目标</button>`
    const targets = Array.from(document.querySelectorAll('.shared-target'))
    const selected = coach.resolveCoachTarget(null, '.shared-target')
    return {
      selectedVisibleTarget: selected === targets[1],
      actionable: coach.isCoachTargetActionable(selected),
      rect: coach.measureCoachTarget(selected),
    }
  })

  expect(result.selectedVisibleTarget).toBe(true)
  expect(result.actionable).toBe(true)
  expect(result.rect?.width).toBe(140)
})

test('沙盒四步导航与用户身份、折叠历史和当前客户无关', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const contract = await import('/src/sandbox/tutorialLayoutContract.ts')
    const stages = ['sidebar', 'step2-sidebar', 'step3-sidebar', 'step4-sidebar'] as const
    return {
      items: contract.SANDBOX_TUTORIAL_NAV_ITEMS.map((item) => ({
        to: item.to,
        label: item.label,
        stepNumber: item.stepNumber,
        stage: item.stage,
      })),
      resolved: stages.map((stage) => contract.getSandboxTutorialNavItem(stage)?.to),
    }
  })

  expect(result.items).toEqual([
    { to: '/diagnosis/new', label: '品牌体检', stepNumber: 1, stage: 'sidebar' },
    { to: '/pricing', label: '报价方案', stepNumber: 2, stage: 'step2-sidebar' },
    { to: '/writing', label: 'AI 写文章与发布', stepNumber: 3, stage: 'step3-sidebar' },
    { to: '/monitoring', label: '效果监测', stepNumber: 4, stage: 'step4-sidebar' },
  ])
  expect(result.resolved).toEqual(['/diagnosis/new', '/pricing', '/writing', '/monitoring'])
})

test('显式教程锚点不会被用户侧栏分组或相邻栏目抢走', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const coach = await import('/src/components/onboarding/coachTarget.ts')
    document.body.innerHTML = `
      <aside>
        <button data-user-section="customer">客户与资料</button>
        <button data-user-section="admin">经营后台</button>
        <a
          href="/monitoring"
          data-sandbox-coach-anchor="sandbox_sidebar_monitoring"
          style="display:block;width:180px;height:42px"
        >效果监测</a>
      </aside>`
    const anchor = '[data-sandbox-coach-anchor="sandbox_sidebar_monitoring"]'
    const before = coach.resolveCoachTarget(null, anchor)
    document.querySelector('[data-user-section="customer"]')?.remove()
    document.querySelector('aside')?.prepend(document.querySelector('[data-user-section="admin"]') as Node)
    const after = coach.resolveCoachTarget(null, anchor)
    return {
      beforeText: before?.textContent,
      afterText: after?.textContent,
      same: before === after,
      actionable: coach.isCoachTargetActionable(after),
    }
  })

  expect(result).toEqual({
    beforeText: '效果监测',
    afterText: '效果监测',
    same: true,
    actionable: true,
  })
})

test('真实沙盒页面强制固定展开侧栏并把第四步锚定到效果监测', async ({ page }) => {
  await installCanonicalSandboxBackend(page)
  await page.setViewportSize({ width: 1366, height: 768 })
  await page.goto('/monitoring', { waitUntil: 'domcontentloaded' })

  const canonicalClient = page.getByTestId('sandbox-canonical-client')
  await expect(canonicalClient).toContainText('标准教程客户')
  await expect(canonicalClient).toContainText('一路顺风出行服务')
  await expect(canonicalClient).toContainText('固定演示数据 · 零扣费')
  await expect(canonicalClient).not.toContainText('用户自定义名称')

  const sidebar = page.locator('aside').first()
  await expect(sidebar).toHaveAttribute('data-state', 'expanded')
  for (const label of ['品牌体检', '报价方案', 'AI 写文章与发布', '效果监测']) {
    await expect(sidebar.getByText(label, { exact: true })).toBeVisible()
  }
  await expect(sidebar.getByText('客户与资料', { exact: true })).toHaveCount(0)
  await expect(sidebar.getByText('经营后台', { exact: true })).toHaveCount(0)

  const monitoringAnchor = sidebar.locator(
    '[data-sandbox-coach-anchor="sandbox_sidebar_monitoring"]',
  )
  await expect(monitoringAnchor).toBeVisible()
  await expect(monitoringAnchor).toHaveAttribute('aria-label', '效果监测')
  await expect(monitoringAnchor).toContainText('效果监测')
  await expect(page.locator('[data-sandbox-coach-anchor]')).toHaveCount(1)
})

for (const deviceName of ['iPhone 13', 'Pixel 5', 'iPhone 13 landscape'] as const) {
  test(`真实 ${deviceName} 触控环境可打开菜单并进入效果监测`, async ({ browser, baseURL }) => {
    const context = await browser.newContext({
      ...devices[deviceName],
      baseURL,
    })
    const page = await context.newPage()
    try {
      await installCanonicalSandboxBackend(page)
      await page.goto('/monitoring', { waitUntil: 'domcontentloaded' })

      const hamburger = page.locator('[data-mobile-coach="hamburger"]')
      await expect(hamburger).toBeVisible()
      await expect(page.getByText('第 4 步 · 打开菜单', { exact: true })).toBeVisible()

      const hamburgerBox = await hamburger.boundingBox()
      const viewport = page.viewportSize()
      expect(hamburgerBox).not.toBeNull()
      expect(viewport).not.toBeNull()
      expect(hamburgerBox!.x).toBeGreaterThanOrEqual(0)
      expect(hamburgerBox!.y).toBeGreaterThanOrEqual(0)
      expect(hamburgerBox!.x + hamburgerBox!.width).toBeLessThanOrEqual(viewport!.width)
      expect(hamburgerBox!.y + hamburgerBox!.height).toBeLessThanOrEqual(viewport!.height)

      await hamburger.click()

      const sidebar = page.locator('aside[data-state="expanded"]')
      await expect(sidebar).toBeVisible()
      await expect.poll(() => page.evaluate(
        () => localStorage.getItem('omnirank_sandbox_tutorial_stage'),
      )).toBe('step4-sidebar')
      await expect(sidebar.getByTestId('sandbox-canonical-client')).toContainText('一路顺风出行服务')
      for (const label of ['品牌体检', '报价方案', 'AI 写文章与发布', '效果监测']) {
        await expect(sidebar.getByText(label, { exact: true })).toBeVisible()
      }

      // Radix renders the mobile sheet outside the desktop sidebar subtree.
      const monitoringAnchor = page.locator('[data-mobile-coach="sidebar-first_monitoring"]')
      await expect(monitoringAnchor).toBeVisible()
      await expect(page.getByText('第 4 步 · 点这里去效果监测', { exact: true })).toBeVisible()
      const anchorBox = await monitoringAnchor.boundingBox()
      expect(anchorBox).not.toBeNull()
      expect(anchorBox!.x).toBeGreaterThanOrEqual(0)
      expect(anchorBox!.y).toBeGreaterThanOrEqual(0)
      expect(anchorBox!.x + anchorBox!.width).toBeLessThanOrEqual(viewport!.width)
      expect(anchorBox!.y + anchorBox!.height).toBeLessThanOrEqual(viewport!.height)

      await monitoringAnchor.click()
      await expect.poll(() => new URL(page.url()).pathname).toBe('/monitoring')
      await expect(sidebar).toBeHidden()
      await expect(page.getByText('当前账号没有此页面权限', { exact: true })).toHaveCount(0)
      await expect(page.getByText('监测词条 (3)', { exact: true })).toBeVisible()
      await expect.poll(() => page.evaluate(
        () => localStorage.getItem('omnirank_sandbox_tutorial_stage'),
      )).toBe('step4-intro')
      await expect(page.getByText('第四步:看效果 · 盯出现率', { exact: true })).toBeVisible()
    } finally {
      await context.close()
    }
  })
}

test('真实 iPhone 写作项目可进入、生成标题并在刷新后恢复到可执行入口', async ({ browser, baseURL }) => {
  const context = await browser.newContext({
    ...devices['iPhone 13'],
    baseURL,
  })
  const page = await context.newPage()
  try {
    await installCanonicalSandboxBackend(page, {
      stage: 'step3-page',
      completedSteps: ['first_quote'],
    })
    await page.goto('/writing', { waitUntil: 'domcontentloaded' })

    const startWriting = page.getByRole('button', { name: /创作/ })
    await expect(startWriting).toBeVisible()
    await startWriting.click()
    await expect(page.getByText('深圳租埃尔法配司机的公司', { exact: true })).toBeVisible()

    await page.evaluate(async () => {
      const tutorial = await import('/src/sandbox/tutorialStage.ts')
      tutorial.setTutorialStage('step3-gen-titles')
    })

    const generateTitles = page.getByTestId('sandbox-generate-titles')
    await expect(generateTitles).toBeVisible()
    await expect(page.getByText('第五步: AI 批量生成标题', { exact: true })).toBeVisible()
    const buttonBox = await generateTitles.boundingBox()
    const viewport = page.viewportSize()
    expect(buttonBox).not.toBeNull()
    expect(viewport).not.toBeNull()
    expect(buttonBox!.width).toBeGreaterThanOrEqual(120)
    expect(buttonBox!.height).toBeGreaterThanOrEqual(32)
    expect(buttonBox!.x).toBeGreaterThanOrEqual(0)
    expect(buttonBox!.y).toBeGreaterThanOrEqual(0)
    expect(buttonBox!.x + buttonBox!.width).toBeLessThanOrEqual(viewport!.width)
    expect(buttonBox!.y + buttonBox!.height).toBeLessThanOrEqual(viewport!.height)

    await generateTitles.click()
    await expect.poll(() => page.evaluate(
      () => localStorage.getItem('omnirank_sandbox_tutorial_stage'),
    ), { timeout: 10_000 }).toBe('step3-expand-titles')

    const expandTitles = page.locator(
      '[data-sandbox-coach-anchor="writing-expand-titles"]',
    )
    await expect(expandTitles).toBeVisible()
    await expect(page.getByText('第六步: 点这里展开看 AI 写出的标题', {
      exact: true,
    })).toBeVisible()
    const expandBox = await expandTitles.boundingBox()
    expect(expandBox).not.toBeNull()
    expect(expandBox!.width).toBeGreaterThanOrEqual(240)
    expect(expandBox!.height).toBeGreaterThanOrEqual(44)
    expect(expandBox!.x).toBeGreaterThanOrEqual(0)
    expect(expandBox!.y).toBeGreaterThanOrEqual(0)
    expect(expandBox!.x + expandBox!.width).toBeLessThanOrEqual(viewport!.width)
    expect(expandBox!.y + expandBox!.height).toBeLessThanOrEqual(viewport!.height)
    await expandTitles.click()

    await expect(page.getByText('深圳埃尔法配司机价格全解析·按天/月/项目租分别多少钱', {
      exact: true,
    })).toBeVisible()
    await expect.poll(() => page.evaluate(
      () => localStorage.getItem('omnirank_sandbox_tutorial_stage'),
    )).toBe('step3-show-titles')

    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(startWriting).toBeVisible()
    await expect.poll(() => page.evaluate(
      () => localStorage.getItem('omnirank_sandbox_tutorial_stage'),
    )).toBe('step3-page')
  } finally {
    await context.close()
  }
})

test('写作教程阶段即使旧项目没有待生成词也必须显示真实标题按钮', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const contract = await import('/src/sandbox/tutorialLayoutContract.ts')
    return {
      tutorialCompletedProject: contract.shouldRenderTitleGenerationAction(
        'completed',
        false,
        'step3-gen-titles',
        true,
      ),
      productionCompletedProject: contract.shouldRenderTitleGenerationAction(
        'completed',
        false,
        'step3-gen-titles',
        false,
      ),
      productionPendingProject: contract.shouldRenderTitleGenerationAction(
        'pending',
        false,
        'step3-page',
        false,
      ),
    }
  })

  expect(result).toEqual({
    tutorialCompletedProject: true,
    productionCompletedProject: false,
    productionPendingProject: true,
  })
})

test('全选 20 个商业词不会被沙盒静默缩成 3 个', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const sandbox = await import('/src/sandbox/sandboxInterceptor.ts')
    const selectedIds = Array.from({ length: 20 }, (_, index) => index + 1)
    const submit = await sandbox.tryFetchSandboxMock(
      'POST',
      '/api/s/sandbox-selection/submit-keywords',
      true,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ selected_ids: selectedIds, custom_keywords: [] }),
      },
    )
    const originalNow = Date.now
    const future = originalNow() + 4000
    Date.now = () => future
    const session = await sandbox.tryFetchSandboxMock(
      'GET',
      '/api/s/sandbox-selection',
      true,
    )
    Date.now = originalNow
    return {
      submitStatus: submit?.status,
      session: await session?.json(),
    }
  })
  expect(result.submitStatus).toBe(200)
  expect(result.session.selected_count).toBe(20)
  expect(result.session.selected_ids).toEqual(Array.from({ length: 20 }, (_, index) => index + 1))
  expect(result.session.pricing_data.keywords).toHaveLength(20)
})

test('未知业务接口显式失败，监测流返回真实 SSE 合同', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const sandbox = await import('/src/sandbox/sandboxInterceptor.ts')
    const unknown = await sandbox.tryFetchSandboxMock(
      'POST',
      '/api/sandbox/not-implemented',
      true,
      { method: 'POST', body: '{}' },
    )
    const stream = await sandbox.tryFetchSandboxMock(
      'POST',
      '/api/monitoring/run-stream',
      true,
      { method: 'POST', body: JSON.stringify({ keyword_ids: [1, 2] }) },
    )
    return {
      unknownStatus: unknown?.status,
      unknownBody: await unknown?.json(),
      streamStatus: stream?.status,
      streamType: stream?.headers.get('content-type'),
      streamBody: await stream?.text(),
    }
  })
  expect(result.unknownStatus).toBe(501)
  expect(result.unknownBody.code).toBe('SANDBOX_ROUTE_NOT_IMPLEMENTED')
  expect(result.unknownBody.actions.map((action: { id: string }) => action.id)).toEqual([
    'retry',
    'continue_tutorial',
    'exit_sandbox',
  ])
  expect(result.streamStatus).toBe(200)
  expect(result.streamType).toContain('text/event-stream')
  expect(result.streamBody).toContain('"type":"complete"')
})

test('智能补足预览与确认在沙盒内闭环且零真实副作用', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const sandbox = await import('/src/sandbox/sandboxInterceptor.ts')
    const previewResponse = await sandbox.tryFetchSandboxMock(
      'GET',
      '/api/writing/optimize-preview/72006',
      true,
    )
    const previewBody = await previewResponse?.json()
    const preview = previewBody?.preview
    const generateResponse = await sandbox.tryFetchSandboxMock(
      'POST',
      '/api/writing/optimize-generate',
      true,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          keyword_id: preview?.keyword_id,
          plan_version: preview?.plan_version,
          plan_hash: preview?.plan_hash,
          client_request_id: 'sandboxsupplementrequest01',
        }),
      },
    )
    return {
      previewStatus: previewResponse?.status,
      previewMock: previewResponse?.headers.get('x-sandbox-mock'),
      previewBody,
      generateStatus: generateResponse?.status,
      generateBody: await generateResponse?.json(),
    }
  })

  expect(result.previewStatus).toBe(200)
  expect(result.previewMock).toBe('1')
  expect(result.previewBody.status).toBe('success')
  expect(result.previewBody.preview).toMatchObject({
    keyword_id: 72006,
    keyword: '深圳埃尔法长租包月',
    suggested_articles: 2,
    reason_code: 'rate_gap',
    style_plan: { guide: 1, comparison: 1 },
    estimated_points: 0,
    target_rate: 50,
    recent_rate: 35,
  })
  expect(result.previewBody.preview.plan_hash).toMatch(/^[0-9a-f]{64}$/)
  expect(result.previewBody.provider_calls).toBe(0)
  expect(result.previewBody.billed_points).toBe(0)

  expect(result.generateStatus).toBe(200)
  expect(result.generateBody).toMatchObject({
    status: 'success',
    keyword_id: 72006,
    created: 2,
    provider_calls: 0,
    billed_points: 0,
  })
  expect(result.generateBody.plan_hash).toBe(result.previewBody.preview.plan_hash)
})

test('监测与发布首屏后台读取均有显式本地合同', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const sandbox = await import('/src/sandbox/sandboxInterceptor.ts')
    const urls = [
      '/api/monitoring/schedule',
      '/api/monitoring/client/990001/monitoring-config',
      '/api/monitoring/archives?brand_id=990001',
      '/api/monitoring/rollback/tasks?brand_id=990001&limit=5',
      '/api/publications/990001',
      '/api/logs?brand_id=990001&limit=50',
      '/api/publish/research/active-task?industry=business',
      '/api/meijiehezi/published-articles',
      '/api/meijiehezi/rejected-articles',
      '/api/meijiehezi/article-publish-stats',
    ]
    return Promise.all(urls.map(async (url) => {
      const response = await sandbox.tryFetchSandboxMock('GET', url, true)
      return {
        url,
        status: response?.status,
        body: await response?.json(),
      }
    }))
  })

  expect(result).toHaveLength(10)
  expect(result.every((item) => item.status === 200)).toBe(true)
  expect(result.every((item) => item.body?.status === 'success')).toBe(true)
  expect(result.find((item) => item.url === '/api/monitoring/schedule')?.body.monitoring_enabled).toBe(false)
  expect(result.find((item) => item.url.includes('active-task'))?.body.active_task).toBeNull()
})

test('沙盒模拟发布零外发零扣费且不创建审批任务', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const fixtures = await import('/src/sandbox/mockData.ts')
    const publishedEvent = new Promise<Record<string, number>>((resolve) => {
      window.addEventListener(
        'sandbox:step3-published',
        (event) => resolve((event as CustomEvent<Record<string, number>>).detail),
        { once: true },
      )
    })
    const batch = fixtures.getSandboxMeijieheziPublishBatch()
    const event = await publishedEvent
    const awaiting = fixtures.getSandboxAwaitingConfirmations()
    const pending = fixtures.getSandboxPendingUserActions()
    return { batch, event, awaiting, pending }
  })

  expect(result.batch).toMatchObject({
    status: 'success',
    charged_points: 0,
    billed_points: 0,
    provider_calls: 0,
    approval_tasks: 0,
    tutorial: true,
  })
  expect(result.event).toMatchObject({
    provider_calls: 0,
    billed_points: 0,
    approval_tasks: 0,
  })
  expect(result.awaiting).toEqual({ status: 'success', items: [], total: 0 })
  expect(result.pending).toEqual({ status: 'success', success: true, items: [], total: 0 })
})

test('沙盒发布引导按真实账号身份显示且普通用户无治理入口', async ({ page }) => {
  const result = await page.evaluate(async () => {
    const contract = await import('/src/sandbox/publishTutorialContract.ts')
    const standard = contract.getSandboxPublishGuide(
      'step3-pub-batch-send',
      { is_admin: false, agent_level: 0 },
    )
    const provider = contract.getSandboxPublishGuide(
      'step3-pub-batch-send',
      { is_admin: false, agent_level: 1 },
    )
    return {
      standard,
      provider,
      standardGovernance: contract.canShowSandboxManualGovernance(
        true,
        { is_admin: false, agent_level: 0 },
      ),
      providerGovernance: contract.canShowSandboxManualGovernance(
        true,
        { is_admin: false, agent_level: 1 },
      ),
      productionGovernance: contract.canShowSandboxManualGovernance(
        false,
        { is_admin: false, agent_level: 0 },
      ),
    }
  })

  expect(result.standard).toMatchObject({
    audience: 'standard',
    title: '发布演练 · 普通用户视角',
    activeStep: 4,
  })
  expect(result.standard.description).toContain('不需要扮演管理员或处理审批')
  expect(result.standard.nextAction).toContain('模拟发布')
  expect(result.provider).toMatchObject({
    audience: 'provider',
    title: '发布演练 · 服务商工作流',
  })
  expect(result.standardGovernance).toBe(false)
  expect(result.providerGovernance).toBe(true)
  expect(result.productionGovernance).toBe(true)
})

test('发布页面源码把真实治理、待确认和充值弹窗隔离在教程之外', async () => {
  const root = process.cwd()
  const publishCenter = readFileSync(resolve(root, 'src/pages/Publishing/PublishCenter.tsx'), 'utf8')
  const mockData = readFileSync(resolve(root, 'src/sandbox/mockData.ts'), 'utf8')

  expect(publishCenter).toContain('!sandboxActive && <PendingUserActions')
  expect(publishCenter).toContain('awaitingDialogOpen && !sandboxActive')
  expect(publishCenter).toContain('showInsufficientDialog && !sandboxActive')
  expect(publishCenter).toContain('data-testid="sandbox-publish-guide"')
  expect(publishCenter).toContain("mode === 'manual' && showManualGovernance")
  expect(publishCenter).not.toContain('含违规词的会被退稿')
  expect(mockData).toContain('approval_tasks: 0')
  expect(mockData).toContain("return { status: 'success', items: [], total: 0 }")
})

test('源码合同不再创建透明点击拦截层或宣传旧 7 步流程', async () => {
  const root = process.cwd()
  const spotlight = readFileSync(resolve(root, 'src/components/onboarding/OnboardingSpotlight.tsx'), 'utf8')
  const desktop = readFileSync(resolve(root, 'src/components/onboarding/FeatureTooltip.tsx'), 'utf8')
  const mobile = readFileSync(resolve(root, 'src/components/onboarding/mobile/MobileCoachMark.tsx'), 'utf8')
  const welcome = readFileSync(resolve(root, 'src/components/onboarding/OnboardingWelcomeModal.tsx'), 'utf8')
  const state = readFileSync(resolve(root, 'src/sandbox/sandboxState.ts'), 'utf8')

  expect(spotlight).not.toContain('pointer-events-auto')
  expect(mobile).not.toContain('pointer-events-auto')
  expect(spotlight).toContain('pointer-events-none')
  expect(mobile).toContain('pointer-events-none')
  expect(desktop).toContain('visualViewport')
  expect(desktop).toContain('MutationObserver')
  expect(mobile).toContain('visualViewport')
  expect(welcome).toContain('完整 4 步流程')
  expect(state).toContain('走完 4 步教程')
})

test('源码合同:沙盒使用固定教程外壳且写作标题按钮有独立锚点', async () => {
  const root = process.cwd()
  const sidebar = readFileSync(resolve(root, 'src/components/layout/AppSidebar.tsx'), 'utf8')
  const writing = readFileSync(resolve(root, 'src/pages/Writing/WritingHall.tsx'), 'utf8')
  const mobile = readFileSync(resolve(root, 'src/sandbox/MobileSidebarCoach.tsx'), 'utf8')

  expect(sidebar).toContain('isSandbox')
  expect(sidebar).toContain('SANDBOX_TUTORIAL_SECTIONS')
  expect(sidebar).toContain('sandbox-canonical-client')
  expect(sidebar).toContain('全域上榜GEO交付系统')
  expect(sidebar).toContain('if (isSandbox && !isMobile) setOpen(true)')
  expect(sidebar).toContain('if (isSandbox) return true')
  expect(sidebar).toContain('targetSelector={`[data-sandbox-coach-anchor="${sandboxSidebarConfig.featureId}"]`}')
  expect(mobile).toContain('getSandboxTutorialNavItem(tutorialStage)')

  expect(writing).toContain('shouldRenderTitleGenerationAction(')
  expect(writing).toContain('data-testid="sandbox-generate-titles"')
  expect(writing).toContain('data-sandbox-coach-anchor="writing-generate-titles"')
  expect(writing).not.toContain("if (selectedProject.writing_status !== 'pending' && !hasAnyKwWithoutTopics) return null")
})

test('源码合同:教程模式不做余额预检(沙盒不产生计费,不得被真实余额挡住)', async () => {
  const root = process.cwd()
  const deduct = readFileSync(resolve(root, 'src/components/DeductDialog.tsx'), 'utf8')

  // 2026-07-27 生产事故:真实余额 638 < 需要 650 → 代理被自己的余额挡在新手教程外。
  // 沙盒的扣费 mutation 全被 sandboxInterceptor 拦下,永远不会真扣,预检必须放行。
  expect(deduct).toContain('useSandboxState')
  expect(deduct).toContain('!isAdmin && !isSandbox && availableForFeature < cost')

  // 反向锁:不允许退回成只判 isAdmin 的老写法
  expect(deduct).not.toContain('!isAdmin && availableForFeature < cost')
})

test('源码合同:业务流程文案不出现媒介供应商名', async () => {
  const root = process.cwd()
  // 2026-07-27 生产事故:沙盒教程写着「敏感词被媒介盒子拦下了」,把转售供应商名露给服务商。
  // 铁律「不给用户看供应商名」· 对用户统一叫「发布通道」。
  const files = [
    'src/components/publishing/AwaitingConfirmDialog.tsx',
    'src/pages/Publishing/PublishCenter.tsx',
    'src/sandbox/mockData.ts',
  ]
  for (const rel of files) {
    const src = readFileSync(resolve(root, rel), 'utf8')
    const leaked = src
      .split(/\r?\n/)
      .map((line, i) => ({ line, no: i + 1 }))
      // 注释不渲染,不算泄漏
      .filter(({ line }) => {
        const s = line.trimStart()
        if (s.startsWith('//') || s.startsWith('*') || s.startsWith('/*')) return false
        const idx = line.indexOf('//')
        return (idx >= 0 ? line.slice(0, idx) : line).includes('媒介盒子')
      })
      .map(({ no, line }) => `${rel}:${no} ${line.trim().slice(0, 70)}`)
    expect(leaked, `渲染层出现供应商名:\n${leaked.join('\n')}`).toEqual([])
  }
})
