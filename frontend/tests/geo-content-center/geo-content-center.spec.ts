import { expect, test, type Page, type Route } from 'playwright/test'
import path from 'node:path'

const viewports = [320, 390, 768, 1024, 1440, 1920, 2560]
const pendingRequestKey = (userId: number, permissionVersion: number) =>
  `geo_content_center_pending_request_v2:u${userId}:p${permissionVersion}`
const pendingRetryKey = (userId: number, permissionVersion: number) =>
  `geo_content_center_pending_retry_v2:u${userId}:p${permissionVersion}`
const draftKey = (userId: number, permissionVersion: number) =>
  `geo_content_center_draft_v2:u${userId}:p${permissionVersion}`

async function rotateAuthoritativeSession(page: Page, token: string) {
  await page.evaluate(async (nextToken) => {
    const importer = new Function('return import("/src/lib/authoritativeSession.ts")') as () => Promise<any>
    const authority = await importer()
    const epoch = authority.stageAuthoritativeSessionToken(nextToken, 'refresh')
    const detail: { token: string; epoch: string; authoritativeReady?: Promise<void> } = {
      token: nextToken,
      epoch,
    }
    window.dispatchEvent(new CustomEvent('token-refreshed', { detail }))
    if (!detail.authoritativeReady) throw new Error('AuthContext did not accept refreshed authority')
    await detail.authoritativeReady
  }, token)
}

const strategy = {
  audience: '有客户资源、想增加 GEO 服务的服务商老板或运营',
  audience_status: '客户决策入口正在从传统搜索扩展到 AI 问答',
  action_resistance: '担心 GEO 讲不清，也担心交付没有真实证据',
  human_problem: '客户问 AI 时，答案里为什么没有他的品牌',
  core_angle: '先看 AI 的真实回答，再决定怎么做 GEO',
  single_value: '把诊断、内容和监测变成客户能看见的证据链',
  evidence_statement: '本内容不引用客户数字；没有证据就明确写无数据',
  single_action: '领取一次 GEO 诊断',
}

const teacher = {
  teacher_id: 'shu',
  name: '舒老师',
  version: '1.0.0',
  system_method: '受众现状→行动阻力→一句人话问题→唯一卖点→真实证据→低门槛唯一行动。',
  source: 'platform',
}

const copyPayload = {
  title: '客户问 AI 时，为什么答案里没有你的品牌？',
  body: '客户已经开始直接问 AI 选谁。先看真实诊断，再决定下一步；没有数据的地方明确写出来。',
  cta: '领取一次 GEO 诊断',
  tags: ['#GEO', '#AI搜索', '#服务商获客'],
}

const successJob = {
  job_id: 901,
  status: 'succeeded',
  error_summary: '',
  assets: [
    { id: 1, asset_kind: 'bundle_item', bundle_slot: 'professional_poster:image:professional_poster', download_url: '/api/marketing/materials/1/download' },
    { id: 2, asset_kind: 'copy', bundle_slot: 'professional_poster:copy', content_text: JSON.stringify(copyPayload) },
    { id: 3, asset_kind: 'bundle_item', bundle_slot: 'moments:image:moments', download_url: '/api/marketing/materials/3/download' },
    { id: 4, asset_kind: 'copy', bundle_slot: 'moments:copy', content_text: JSON.stringify({ ...copyPayload, tones: { restrained: '先看真实回答，再决定怎么做。', professional: '用诊断证据把下一步说清。', friendly: '先跑一份诊断看看，不急着定方案。' } }) },
    { id: 5, asset_kind: 'bundle_item', bundle_slot: 'xiaohongshu:image:xhs_cover', download_url: '/api/marketing/materials/5/download' },
    { id: 6, asset_kind: 'bundle_item', bundle_slot: 'xiaohongshu:image:xhs_card_1', download_url: '/api/marketing/materials/6/download' },
    { id: 7, asset_kind: 'bundle_item', bundle_slot: 'xiaohongshu:image:xhs_card_2', download_url: '/api/marketing/materials/7/download' },
    { id: 8, asset_kind: 'copy', bundle_slot: 'xiaohongshu:copy', content_text: JSON.stringify({ ...copyPayload, card_outline: ['客户正在怎么问 AI', '诊断里能看到什么', '下一步先做什么'] }) },
    { id: 9, asset_kind: 'bundle_item', bundle_slot: 'douyin:image:douyin_cover', download_url: '/api/marketing/materials/9/download' },
    { id: 10, asset_kind: 'copy', bundle_slot: 'douyin:copy', content_text: JSON.stringify({ title: '客户在 AI 里搜不到你，怎么办？', script: '别猜，先跑一次真实诊断，再决定怎么做。', shots: ['客户提问的画面', '诊断报告翻页', '评论区回复「诊断」'], tags: ['#GEO', '#AI搜索'] }) },
    { id: 11, asset_kind: 'bundle_item', bundle_slot: 'infographic:image:infographic', download_url: '/api/marketing/materials/11/download' },
    { id: 12, asset_kind: 'copy', bundle_slot: 'infographic:copy', content_text: JSON.stringify({ title: '一张图看懂 GEO 诊断能看见什么', body: '5 个平台逐一检测，12 项指标逐项打分。', tags: ['#信息图', '#GEO'] }) },
    { id: 13, asset_kind: 'bundle_item', bundle_slot: 'private_chat:image:private_chat_scene', download_url: '/api/marketing/materials/13/download' },
    { id: 14, asset_kind: 'copy', bundle_slot: 'private_chat:copy', content_text: JSON.stringify({ opening: '上次你说客户在问 AI 搜索的事，我这边能出一份真实诊断。', follow_up: '报告出来我发你一份，看完再决定要不要做优化。', objection_reply: '诊断不改动你的网站，只是如实检测可见度现状。' }) },
  ],
  components: [
    { component_id: 'professional_poster:copy', channel: 'professional_poster', kind: 'copy', status: 'succeeded' },
    { component_id: 'professional_poster:image:professional_poster', channel: 'professional_poster', kind: 'image', status: 'succeeded', slot: { slot: 'professional_poster', size: '3:4' } },
    { component_id: 'moments:copy', channel: 'moments', kind: 'copy', status: 'succeeded' },
    { component_id: 'moments:image:moments', channel: 'moments', kind: 'image', status: 'succeeded', slot: { slot: 'moments', size: '1:1' } },
    { component_id: 'xiaohongshu:copy', channel: 'xiaohongshu', kind: 'copy', status: 'succeeded' },
    { component_id: 'xiaohongshu:image:xhs_cover', channel: 'xiaohongshu', kind: 'image', status: 'succeeded', slot: { slot: 'xhs_cover', size: '3:4' } },
    { component_id: 'xiaohongshu:image:xhs_card_1', channel: 'xiaohongshu', kind: 'image', status: 'succeeded', slot: { slot: 'xhs_card_1', size: '3:4' } },
    { component_id: 'xiaohongshu:image:xhs_card_2', channel: 'xiaohongshu', kind: 'image', status: 'succeeded', slot: { slot: 'xhs_card_2', size: '3:4' } },
    { component_id: 'douyin:copy', channel: 'douyin', kind: 'copy', status: 'succeeded' },
    { component_id: 'douyin:image:douyin_cover', channel: 'douyin', kind: 'image', status: 'succeeded', slot: { slot: 'douyin_cover', size: '9:16' } },
    { component_id: 'infographic:copy', channel: 'infographic', kind: 'copy', status: 'succeeded' },
    { component_id: 'infographic:image:infographic', channel: 'infographic', kind: 'image', status: 'succeeded', slot: { slot: 'infographic', size: '3:4' } },
    { component_id: 'private_chat:copy', channel: 'private_chat', kind: 'copy', status: 'succeeded' },
    { component_id: 'private_chat:image:private_chat_scene', channel: 'private_chat', kind: 'image', status: 'succeeded', slot: { slot: 'private_chat_scene', size: '9:16' } },
  ],
  channels: ['professional_poster', 'moments', 'xiaohongshu', 'douyin', 'infographic', 'private_chat'],
  teacher,
  strategy,
  evidence: { facts: [], source_note: '本内容未引用客户诊断数字' },
  trend: { requested: false, used: false, reason: 'not_requested' },
  contact: { mode: 'none', text: '' },
  created_at: '2026-07-21T12:00:00Z',
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

async function mockApp(page: Page, options: {
  job?: typeof successJob
  qrInvalid?: boolean
  delayedInterpret?: boolean
  failFirstCreate?: boolean
  createRequestIds?: string[]
  createPayloads?: any[]
  interpretWarnings?: any[]
  serviceBrand?: { configured: boolean; name: string; has_logo: boolean }
} = {}) {
  const job = options.job || successJob
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'qa-token')
    localStorage.setItem('omnirank-theme', 'dark')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'never',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-21T12:00:00Z',
      last_updated_at: '2026-07-21T12:00:00Z',
    }))
  })
  await page.route('**/api/**', async (route) => json(route, { success: true }))
  await page.route('**/api/marketing/materials/*/download', async (route) => route.fulfill({
    status: 200,
    contentType: 'image/png',
    body: Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=', 'base64'),
  }))
  await page.route('**/api/auth/me', async (route) => json(route, {
    success: true,
    user: {
      id: 1, username: 'qa-owner', display_name: '质检服务商', is_admin: false, is_active: 1,
      must_change_password: 0, roles: [], permissions: [], client_brand_ids: [101], agent_level: 1,
      permission_version: 1,
    },
  }))
  await page.route('**/api/marketing/content-center/bootstrap', async (route) => json(route, {
    ok: true, teachers: [teacher, { ...teacher, teacher_id: 'b2b_sales_coach', name: 'B2B 销售教练' }],
    default_teacher: teacher, trend_provider_available: false,
    service_brand: options.serviceBrand || { configured: false, name: '', has_logo: false },
  }))
  await page.route('**/api/marketing/product-facts', async (route) => json(route, {
    ok: true,
    pack: { pack_id: 'product_facts', version: '1.0.0', fact_count: 19, facts: [] },
  }))
  await page.route('**/api/my-clients**', async (route) => json(route, {
    clients: [{ id: 101, name: '这是一家品牌名称特别特别长用于验证中文换行与选择器边界的客户公司' }],
    total: 1,
  }))
  await page.route('**/api/marketing/my-materials**', async (route) => json(route, { ok: true, materials: [] }))
  await page.route('**/api/marketing/interpret', async (route) => {
    if (options.delayedInterpret) await new Promise((resolve) => setTimeout(resolve, 700))
    await json(route, { ok: true, strategy, teacher, warnings: options.interpretWarnings || [] })
  })
  await page.route('**/api/marketing/content-packages', async (route) => {
    const requestBody = route.request().postDataJSON() as Record<string, any>
    const requestId = String(requestBody?.request_id || '')
    options.createRequestIds?.push(requestId)
    options.createPayloads?.push(requestBody)
    if (options.failFirstCreate && options.createRequestIds?.length === 1) {
      await route.abort('connectionrefused')
      return
    }
    await json(route, { ok: true, status: 'generating', job_id: job.job_id })
  })
  await page.route(`**/api/marketing/jobs/${job.job_id}`, async (route) => json(route, { ok: true, job, assets: job.assets }))
  await page.route('**/api/marketing/jobs/*/retry', async (route) => json(route, { ok: true, status: 'generating', job_id: job.job_id }))
  await page.route('**/api/marketing/qr-reference', async (route) => {
    if (options.qrInvalid) await json(route, { detail: 'qr_requires_exactly_one_decodable_code' }, 422)
    else await json(route, { ok: true, qr_reference: { reference_id: 'opaque-qr.png', payload_hash: 'a'.repeat(64), file_sha256: 'b'.repeat(64), payload_preview: 'https://example…', reference_token: 'signed', uploaded_at: '2026-07-21T12:00:00Z' } })
  })
}

async function openAndGenerate(page: Page) {
  await page.goto('/marketing-materials')
  await expect(page.getByTestId('geo-content-center')).toBeVisible()
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  await expect(page.getByAltText('专业海报最终成图')).toBeVisible()
  await expect(page.getByText('本版未请求热点，使用常青内容')).toBeVisible()
  await expect(page.getByText('联系方式未展示；历史重做沿用该冻结设置')).toBeVisible()
}

async function resetAllScrollContainers(page: Page) {
  await page.evaluate(() => {
    window.scrollTo(0, 0)
    document.scrollingElement?.scrollTo(0, 0)
    document.querySelectorAll<HTMLElement>('*').forEach((element) => {
      if (element.scrollTop) element.scrollTop = 0
      if (element.scrollLeft) element.scrollLeft = 0
    })
  })
}

/**
 * F4：「不溢出」断言不能只看 body.scrollWidth —— .gcc-shell 自带
 * overflow-x: hidden，子内容多宽都会被 clip，body 断言恒真（假绿）。
 * 改为对关键容器测 getBoundingClientRect().right <= innerWidth + 1；
 * body scrollWidth 保留作辅助。
 * N5：每个选择器先断言存在——缺失/改名立刻变红，不静默退化为仅 body 断言。
 */
async function expectNoHorizontalOverflow(page: Page, selectors: string[]) {
  expect(await page.locator('body').evaluate((body) => body.scrollWidth <= window.innerWidth + 1)).toBe(true)
  for (const selector of selectors) {
    const locator = page.locator(selector).first()
    expect(await locator.count(), `${selector} 未渲染，溢出断言不得静默跳过`).toBeGreaterThan(0)
    const metrics = await locator.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      return { right: rect.right, viewport: window.innerWidth }
    })
    expect(metrics.right, `${selector} 右缘 ${metrics.right.toFixed(1)}px 超出视口 ${metrics.viewport}px`).toBeLessThanOrEqual(metrics.viewport + 1)
  }
}

for (const width of viewports) {
  test(`visual result viewport ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width <= 390 ? 844 : width <= 768 ? 1024 : 900 })
    await mockApp(page)
    await openAndGenerate(page)
    await resetAllScrollContainers(page)
    const screenshot = path.resolve(testInfo.config.rootDir, '..', 'artifacts', 'geo-content-center', `${width}.png`)
    await page.screenshot({ path: screenshot, fullPage: true })
    await expectNoHorizontalOverflow(page, ['.gcc-channel-options', '.gcc-strategy-grid', '.gcc-channel-sections'])
    const typography = await page.evaluate(() => {
      const metric = (selector: string) => {
        const style = getComputedStyle(document.querySelector(selector) as Element)
        return { font: parseFloat(style.fontSize), line: parseFloat(style.lineHeight) }
      }
      return {
        brief: metric('.gcc-input-wrap textarea'),
        channel: metric('.gcc-channel-options button'),
        primary: metric('.gcc-primary'),
        copy: metric('.gcc-copy-content article p'),
      }
    })
    expect(typography.brief.font).toBeGreaterThanOrEqual(15)
    expect(typography.channel.font).toBeGreaterThanOrEqual(13)
    expect(typography.primary.font).toBeGreaterThanOrEqual(14)
    expect(typography.copy.font).toBeGreaterThanOrEqual(14)
    expect(typography.copy.line / typography.copy.font).toBeGreaterThanOrEqual(1.7)
  })
}

test('ordinary mode exposes one generation action and keeps advanced strategy collapsed', async ({ page }) => {
  await mockApp(page)
  await page.goto('/marketing-materials')
  await expect(page.getByRole('button', { name: '生成完整推广包' })).toHaveCount(1)
  await expect(page.getByRole('button', { name: /高级设置/ })).toHaveAttribute('aria-expanded', 'false')
  // F5：断言九格摘要的真实状态——未输入一句话前，8 个策略格为占位、渠道格展示默认勾选
  await expect(page.getByTestId('strategy-grid').locator('p.is-placeholder')).toHaveCount(8)
  await expect(page.locator('[data-strategy-key="平台渠道"]')).toContainText('海报')
  await expect(page.getByText('常青内容，不引用客户诊断数字')).toBeVisible()
})

test('brand axis: configured brand shows the company name, never a platform fallback', async ({ page }) => {
  await mockApp(page, { serviceBrand: { configured: true, name: '远山营销', has_logo: true } })
  await page.goto('/marketing-materials')
  // 已配置品牌:状态区展示公司名,高级设置里同口径,绝不出现平台兜底名
  const evidenceState = page.locator('.gcc-evidence-state')
  await expect(evidenceState.getByText('品牌：远山营销 · 成品展示该公司名与 Logo')).toBeVisible()
  await expect(evidenceState).not.toContainText(/OmniRank|全域上榜/)
  await page.getByRole('button', { name: /高级设置/ }).click()
  await expect(page.getByTestId('gcc-brand-status')).toContainText('已配置品牌：远山营销')
  await expect(page.getByRole('link', { name: '修改品牌信息' })).toHaveAttribute('href', '/agent/whitelabel')
  // 产品事实库状态行:只读,版本+条数,与后端 SSOT 包同口径
  await expect(page.getByTestId('gcc-facts-status')).toContainText('产品事实库 v1.0.0 · 19 条')
})

test('brand axis: unconfigured brand stays blank-honest with a settings link', async ({ page }) => {
  await mockApp(page)  // 默认未配置品牌
  await page.goto('/marketing-materials')
  // 未配置:成品留白,给品牌信息设置跳转;绝不暗示平台兜底
  const evidenceState = page.locator('.gcc-evidence-state')
  await expect(evidenceState.getByText('成品将不含品牌信息')).toBeVisible()
  await expect(evidenceState).not.toContainText(/OmniRank|全域上榜/)
  await expect(evidenceState.getByRole('link', { name: '去品牌信息设置' })).toHaveAttribute('href', '/agent/whitelabel')
  await page.getByRole('button', { name: /高级设置/ }).click()
  await expect(page.getByTestId('gcc-brand-status')).toContainText('未配置品牌：成品将不含品牌信息')
})

test('home shows the nine-element AI summary before generation and each cell is lightly editable', async ({ page }) => {
  await mockApp(page)
  await page.goto('/marketing-materials')
  // 九格摘要生成前已在首屏，且高级设置保持收起
  await expect(page.getByTestId('strategy-grid')).toBeVisible()
  await expect(page.locator('.gcc-strategy-cell')).toHaveCount(9)
  await expect(page.getByRole('button', { name: /高级设置/ })).toHaveAttribute('aria-expanded', 'false')

  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  // 一句话写定后自动完成 AI 理解，8 个策略格填充
  await expect(page.locator('[data-strategy-key="受众"]')).toContainText('有客户资源')
  await expect(page.locator('[data-strategy-key="真实痛点"]')).toContainText('为什么没有他的品牌')
  const platformCell = page.locator('[data-strategy-key="平台渠道"]')
  await expect(platformCell).toContainText('海报')
  await expect(platformCell).not.toContainText('信息图')

  // 轻量编辑：改核心卖点
  await page.locator('[data-strategy-key="核心卖点"]').getByRole('button', { name: '编辑核心卖点' }).click()
  // F7：暗色主题（mockApp 固定 omnirank-theme=dark）下浅色面不被全局 !important 染暗
  const cellEditor = page.locator('[data-strategy-key="核心卖点"] textarea')
  await expect(cellEditor).toHaveCSS('background-color', 'rgb(255, 255, 255)')
  // F10：格内编辑按钮触摸目标 ≥ 36px
  const editButtonBox = await page.locator('[data-strategy-key="受众"]').getByRole('button', { name: '编辑受众' }).boundingBox()
  expect(editButtonBox).toBeTruthy()
  expect(editButtonBox!.height).toBeGreaterThanOrEqual(36)
  await cellEditor.fill('一次诊断看清品牌在 AI 平台的真实可见度')
  await page.locator('[data-strategy-key="核心卖点"]').getByRole('button', { name: '保存' }).click()
  await expect(page.locator('[data-strategy-key="核心卖点"]')).toContainText('一次诊断看清品牌')

  // 生成 payload 带编辑后的策略
  const createRequest = page.waitForRequest('**/api/marketing/content-packages')
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  const payload = (await createRequest).postDataJSON()
  expect(payload.strategy.single_value).toBe('一次诊断看清品牌在 AI 平台的真实可见度')

  // 第 9 格与渠道勾选联动
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: '信息图' }).click()
  await expect(platformCell).toContainText('信息图')

  // F7：高级设置 select 的边框不被暗色主题泄漏染色
  await page.getByRole('button', { name: /高级设置/ }).click()
  await expect(page.locator('.gcc-settings-panel select').first()).toHaveCSS('border-color', 'rgb(219, 219, 219)')
})

test('channel picker includes infographic and feedback quick entry preselects private chat', async ({ page }) => {
  await mockApp(page)
  await page.goto('/marketing-materials')
  const infographicChip = page.getByRole('button', { name: '信息图' })
  await expect(infographicChip).toHaveAttribute('aria-pressed', 'false')
  await infographicChip.click()
  await expect(infographicChip).toHaveAttribute('aria-pressed', 'true')
  // 做反馈沟通素材：填入私域 brief 并预勾聊天素材渠道
  await page.getByRole('button', { name: '做反馈沟通素材' }).click()
  await expect(page.getByLabel('一句话说明你想推广什么')).toHaveValue(/反馈沟通素材/)
  await expect(page.getByRole('button', { name: '聊天素材' })).toHaveAttribute('aria-pressed', 'true')
})

test('showcase deal entry opens the DealStudio seam and returns', async ({ page }) => {
  await mockApp(page)
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: /晒成交/ }).click()
  await expect(page.getByTestId('deal-studio')).toBeVisible()
  await expect(page.getByRole('button', { name: '整理成交信息' })).toBeVisible()
  await page.getByRole('button', { name: /返回内容中心/ }).first().click()
  await expect(page.getByTestId('geo-content-center')).toBeVisible()
})

test('published evidence 422 is actionable, hides internal code, and recovers through evergreen', async ({ page }) => {
  await mockApp(page)
  const payloads: Array<Record<string, any>> = []
  await page.route('**/api/marketing/content-packages', async route => {
    const payload = route.request().postDataJSON() as Record<string, any>
    payloads.push(payload)
    if (payload.evidence?.source_type === 'latest_diagnosis') {
      // Exact production failure shape from the rejected release. The page
      // must translate it even while backend and frontend versions overlap.
      await json(route, { detail: 'published_diagnosis_evidence_not_found' }, 422)
      return
    }
    await json(route, { ok: true, status: 'generating', job_id: successJob.job_id })
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '用案例数据做内容' }).click()
  await page.getByRole('button', { name: /高级设置/ }).click()
  const evidenceGroup = page.getByRole('group', { name: '真实证据（可选）' })
  await evidenceGroup.getByRole('combobox').first().selectOption('latest_diagnosis')
  await evidenceGroup.getByRole('combobox').nth(1).selectOption('101')
  await page.getByRole('button', { name: /高级设置/ }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()

  const recovery = page.locator('.gcc-recovery')
  await expect(recovery).toHaveAttribute('data-error-code', 'PUBLISHED_DIAGNOSIS_EVIDENCE_NOT_FOUND')
  await expect(recovery.getByText('这个客户暂时没有可用于推广的已发布诊断。')).toBeVisible()
  await expect(recovery.getByRole('link', { name: '查看诊断记录' })).toHaveAttribute('href', '/history')
  await expect(page.getByText('published_diagnosis_evidence_not_found')).toHaveCount(0)
  expect(await page.evaluate(key => localStorage.getItem(key), pendingRequestKey(1, 1))).toBeNull()

  await recovery.getByRole('button', { name: '去选择已发布诊断证据' }).click()
  await expect(page.getByRole('button', { name: /高级设置/ })).toHaveAttribute('aria-expanded', 'true')
  await expect(page.getByLabel('证据来源')).toBeFocused()
  await recovery.getByRole('button', { name: '改为不依赖诊断证据的常青内容' }).click()
  await expect(page.getByText('常青内容，不引用客户诊断数字')).toBeVisible()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(payloads).toHaveLength(2)
  expect(payloads[0].evidence.source_type).toBe('latest_diagnosis')
  expect(payloads[0].brand_id).toBe(101)
  expect(payloads[1].evidence.source_type).toBe('none')
  expect(payloads[1].brand_id).toBeNull()
  expect(payloads[0].request_id).not.toBe(payloads[1].request_id)
})

test('organization overview 404 is a single expected empty-state request', async ({ page }) => {
  await mockApp(page)
  let overviewRequests = 0
  await page.route('**/api/organization/overview', async route => {
    overviewRequests += 1
    await json(route, { detail: 'organization_not_found' }, 404)
  })
  await page.goto('/marketing-materials')
  await expect(page.getByTestId('geo-content-center')).toBeVisible()
  await expect.poll(() => overviewRequests).toBe(1)
  await page.waitForTimeout(700)
  expect(overviewRequests).toBe(1)
  await expect(page.getByText(/组织信息加载失败|organization_not_found/)).toHaveCount(0)
})

test('reference comparison viewport 1487x1058', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1487, height: 1058 })
  await mockApp(page)
  await openAndGenerate(page)
  await resetAllScrollContainers(page)
  // F6：参考视口不再纯截屏——九格可见 + 渠道分段计数 > 0
  await expect(page.getByTestId('strategy-grid')).toBeVisible()
  expect(await page.locator('.gcc-channel-section').count()).toBeGreaterThan(0)
  const screenshot = path.resolve(testInfo.config.rootDir, '..', 'artifacts', 'geo-content-center', 'reference-1487x1058.png')
  await page.screenshot({ path: screenshot })
})

test('copy, tag copy, text download and image download are real browser actions', async ({ page, context }) => {
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: 'http://127.0.0.1:4175' })
  await mockApp(page)
  await openAndGenerate(page)
  const poster = page.locator('[data-channel-section="professional_poster"]')

  await poster.getByRole('article').filter({ hasText: '标题' }).getByRole('button', { name: '复制' }).click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toBe(copyPayload.title)

  await poster.getByRole('button', { name: '复制标签' }).click()
  await expect.poll(() => page.evaluate(() => navigator.clipboard.readText())).toContain('#GEO')

  const textDownload = page.waitForEvent('download')
  await poster.getByRole('button', { name: '下载文案' }).click()
  expect((await textDownload).suggestedFilename()).toContain('专业海报')

  const imageDownload = page.waitForEvent('download')
  await poster.getByRole('button', { name: '下载图片' }).click()
  expect((await imageDownload).suggestedFilename()).toContain('专业海报')
})

test('results render per-channel sections with platform mocks and fixed per-item actions', async ({ page }) => {
  await mockApp(page)
  await openAndGenerate(page)
  for (const id of ['professional_poster', 'moments', 'xiaohongshu', 'douyin', 'infographic', 'private_chat']) {
    await expect(page.locator(`[data-channel-section="${id}"]`)).toBeVisible()
  }
  // 聊天素材显著标「示例对话」
  await expect(page.locator('[data-channel-section="private_chat"]').getByText('示例对话 · 效果演示')).toBeVisible()
  // 信息图渠道分段渲染
  await expect(page.locator('[data-channel-section="infographic"]').getByAltText('信息图最终成图')).toBeVisible()
  // 每段固定操作：复制文案 / 下载图片 / 编辑 / 只重做这一项
  const poster = page.locator('[data-channel-section="professional_poster"]')
  await expect(poster.getByRole('button', { name: '复制文案' })).toBeVisible()
  await expect(poster.getByRole('button', { name: '下载图片' })).toBeVisible()
  await expect(poster.getByRole('button', { name: '编辑' })).toBeVisible()
  await expect(poster.getByRole('button', { name: '只重做这一项' })).toBeVisible()
})

test('copy edit patches the asset and refreshes locally without touching the image', async ({ page }) => {
  await mockApp(page)
  const patches: Array<Record<string, any>> = []
  await page.route('**/api/marketing/assets/*', async route => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, any>
    patches.push(body)
    const content = { ...copyPayload, title: String(body.updates?.title || '') }
    await json(route, { ok: true, asset_id: 2, channel: 'professional_poster', content, qa: { passed: true, errors: [], warnings: [] } })
  })
  await openAndGenerate(page)
  const poster = page.locator('[data-channel-section="professional_poster"]')
  await poster.getByRole('button', { name: '编辑' }).click()
  await poster.locator('.gcc-edit-field', { hasText: '标题' }).locator('textarea').first()
    .fill('改后的标题：先跑诊断再谈方案')
  await poster.getByRole('button', { name: '保存文案' }).click()
  await expect(poster.getByText('改后的标题：先跑诊断再谈方案')).toBeVisible()
  // 图片不动，仍然正常展示
  await expect(poster.getByAltText('专业海报最终成图')).toBeVisible()
  expect(patches).toHaveLength(1)
  expect(patches[0].updates).toEqual({ title: '改后的标题：先跑诊断再谈方案' })
})

test('advanced settings carry visual style and output specs into the payload', async ({ page }) => {
  await mockApp(page)
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: /高级设置/ }).click()
  await page.getByRole('button', { name: '科技深色' }).click()
  await page.getByLabel('清晰度').selectOption('2k')
  await page.getByLabel('朋友圈版式').selectOption('grid')
  await expect(page.getByRole('button', { name: /高级设置/ })).toContainText('科技深色')
  const createRequest = page.waitForRequest('**/api/marketing/content-packages')
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  const payload = (await createRequest).postDataJSON()
  expect(payload.visual_style).toBe('tech_dark')
  expect(payload.resolution).toBe('2k')
  expect(payload.moments_layout).toBe('grid')
})

test('partial success keeps completed output and retries only failed components', async ({ page }) => {
  const partial = structuredClone(successJob)
  partial.status = 'partial_success'
  partial.error_summary = 'partial_free_release'
  partial.components[1].status = 'failed'
  partial.assets = partial.assets.filter((asset) => asset.bundle_slot !== 'professional_poster:image:professional_poster')
  await mockApp(page, { job: partial })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByText('部分成功', { exact: true })).toBeVisible()
  const poster = page.locator('[data-channel-section="professional_poster"]')
  await expect(poster.getByText(copyPayload.title)).toBeVisible()
  const retryRequest = page.waitForRequest('**/api/marketing/jobs/*/retry')
  await page.getByRole('button', { name: '只重做失败项' }).click()
  const body = (await retryRequest).postDataJSON()
  expect(body.component_ids).toEqual(['professional_poster:image:professional_poster'])
})

test('failed channel shows a human reason with an actionable local retry and keeps succeeded sections', async ({ page }) => {
  const partial = structuredClone(successJob)
  partial.status = 'partial_success'
  partial.error_summary = 'partial_free_release'
  partial.components[1].status = 'failed'
  partial.assets = partial.assets.filter((asset) => asset.bundle_slot !== 'professional_poster:image:professional_poster')
  await mockApp(page, { job: partial })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByText('部分成功', { exact: true })).toBeVisible()
  const poster = page.locator('[data-channel-section="professional_poster"]')
  // 人话原因 + 可执行动作，不出现裸机器码
  await expect(poster.getByText('图片没通过视觉质检，不能发')).toBeVisible()
  await expect(poster.getByText('局部重试不重复扣费，其他渠道已生成的内容原样保留')).toBeVisible()
  await expect(poster.getByText('partial_free_release')).toHaveCount(0)
  // 成功组件保留
  await expect(page.locator('[data-channel-section="moments"]').getByAltText(/最终成图/)).toBeVisible()
  const retryRequest = page.waitForRequest('**/api/marketing/jobs/*/retry')
  await poster.getByRole('button', { name: '局部重试' }).click()
  expect((await retryRequest).postDataJSON().component_ids).toEqual(['professional_poster:image:professional_poster'])
})

test('invalid QR and unavailable trend fail honestly', async ({ page }) => {
  await mockApp(page, { qrInvalid: true })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: /高级设置/ }).click()
  await expect(page.getByText(/当前没有可靠热点源/)).toBeVisible()
  await page.getByRole('button', { name: '上传二维码', exact: true }).click()
  const fileInput = page.locator('.gcc-qr-upload input[type="file"]')
  await fileInput.setInputFiles({ name: 'bad.png', mimeType: 'image/png', buffer: Buffer.from('not-a-qr') })
  await expect(page.getByText('这个文件没有且仅有一个可扫码二维码，请换一张清晰原图。')).toBeVisible()
  await expect(page.getByText('qr_requires_exactly_one_decodable_code')).toHaveCount(0)
})

test('rapid task switch ignores late AI response', async ({ page }) => {
  await mockApp(page, { delayedInterpret: true })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await page.getByRole('button', { name: '做反馈沟通素材' }).click()
  await expect(page.getByLabel('一句话说明你想推广什么')).toHaveValue(/反馈沟通素材/)
  // F5+F3：切换任务后九格先失效为占位，随后由自动理解恢复（不再永久停留占位）
  await expect(page.locator('[data-strategy-key="受众"] p.is-placeholder')).toBeVisible()
  await expect(page.locator('[data-strategy-key="受众"]')).toContainText('有客户资源')
})

test('lost create response reuses the same idempotency key', async ({ page }) => {
  const requestIds: string[] = []
  await mockApp(page, { failFirstCreate: true, createRequestIds: requestIds })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByRole('button', { name: '生成完整推广包' })).toBeEnabled()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(requestIds).toHaveLength(2)
  expect(requestIds[0]).toBe(requestIds[1])
})

test('long Chinese and evidence selection never default to first client', async ({ page }) => {
  await mockApp(page)
  await page.goto('/marketing-materials')
  const textarea = page.getByLabel('一句话说明你想推广什么')
  await textarea.fill('向在多个城市服务制造业客户、团队很小但需要把复杂 GEO 方案讲成人话并且不愿意承诺虚假结果的服务商老板，推广一次可以追溯来源的诊断体验，希望他只做一个动作：先领取诊断。'.repeat(2))
  await page.getByRole('button', { name: /高级设置/ }).click()
  const evidenceGroup = page.getByRole('group', { name: '真实证据（可选）' })
  await evidenceGroup.getByRole('combobox').first().selectOption('latest_diagnosis')
  const clientSelect = evidenceGroup.getByRole('combobox').nth(1)
  await expect(clientSelect).toHaveValue('')
  await expect(clientSelect.locator('option')).toHaveCount(2)
  expect(await page.locator('body').evaluate((body) => body.scrollWidth <= window.innerWidth + 1)).toBe(true)
})

for (const width of [320, 390, 768, 1440, 2560]) {
  test(`slow-fast and unmount guards hold at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width < 700 ? 844 : 1000 })
    await mockApp(page)
    let uploads = 0
    await page.route('**/api/marketing/qr-reference', async (route) => {
      uploads += 1
      const current = uploads
      await new Promise(resolve => setTimeout(resolve, current === 2 ? 40 : 500))
      try {
        await json(route, { ok: true, qr_reference: {
          reference_id: `opaque-qr-${current}.png`, payload_hash: String(current).repeat(64), file_sha256: 'f'.repeat(64),
          payload_preview: current === 1 ? 'slow-A' : 'fast-B', reference_token: `signed-${current}`, uploaded_at: new Date().toISOString(),
        } })
      } catch { /* the first request is expected to be aborted */ }
    })
    await page.goto('/marketing-materials')
    await page.getByRole('button', { name: /高级设置/ }).click()
    await page.getByRole('button', { name: '上传二维码', exact: true }).click()
    const input = page.locator('.gcc-qr-upload input[type="file"]')
    await input.setInputFiles({ name: 'A.png', mimeType: 'image/png', buffer: Buffer.from('A') })
    await input.setInputFiles({ name: 'B.png', mimeType: 'image/png', buffer: Buffer.from('B') })
    await expect(page.getByText(/已验证：fast-B/)).toBeVisible()
    await page.waitForTimeout(600)
    await expect(page.getByText(/slow-A/)).toHaveCount(0)
    // Start one more slow request, then unmount the page. A late response may
    // arrive at the browser seam but must never write into the departed view.
    await input.setInputFiles({ name: 'unmount.png', mimeType: 'image/png', buffer: Buffer.from('C') })
    await page.goto('about:blank')
    await page.waitForTimeout(550)
    expect(page.url()).toBe('about:blank')
  })
}

test('rapid history A to B keeps only the newest version', async ({ page }) => {
  await mockApp(page)
  const jobA = structuredClone(successJob)
  jobA.job_id = 1101
  jobA.strategy.core_angle = '历史版本 A（慢）'
  const jobB = structuredClone(successJob)
  jobB.job_id = 1102
  jobB.strategy.core_angle = '历史版本 B（快）'
  await page.route('**/api/marketing/my-materials**', route => json(route, { ok: true, materials: [jobA, jobB] }))
  await page.route('**/api/marketing/jobs/*', async route => {
    const isA = route.request().url().endsWith('/1101')
    await new Promise(resolve => setTimeout(resolve, isA ? 500 : 30))
    try { await json(route, { ok: true, job: isA ? jobA : jobB }) } catch { /* aborted A */ }
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '历史版本' }).click()
  await page.getByRole('button', { name: /历史版本 A/ }).click()
  await page.getByRole('button', { name: /历史版本 B/ }).click()
  await expect(page.getByText('历史版本 B（快）')).toBeVisible()
  await page.waitForTimeout(600)
  await expect(page.getByText('历史版本 A（慢）')).toHaveCount(0)
})

test('teacher preference response is bound to the selected teacher', async ({ page }) => {
  await mockApp(page)
  const saved: string[] = []
  await page.route('**/api/marketing/teacher-preference', async route => {
    const body = route.request().postDataJSON() as { teacher_id: string; version: string }
    saved.push(body.teacher_id)
    await new Promise(resolve => setTimeout(resolve, body.teacher_id === 'b2b_sales_coach' ? 450 : 30))
    try { await json(route, { ok: true, teacher: { ...teacher, teacher_id: body.teacher_id, version: body.version } }) } catch { /* superseded save */ }
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: /高级设置/ }).click()
  const select = page.locator('.gcc-settings-panel select').first()
  await select.selectOption('b2b_sales_coach@1.0.0')
  await page.getByRole('button', { name: '设为默认' }).click()
  await select.selectOption('shu@1.0.0')
  await page.getByRole('button', { name: '设为默认' }).click()
  await page.waitForTimeout(550)
  expect(saved).toEqual(['b2b_sales_coach', 'shu'])
  await expect(select).toHaveValue('shu@1.0.0')
  await expect(page.getByText('默认导师保存失败')).toHaveCount(0)
})

test('refresh recovers an unconfirmed create by durable request id without another POST', async ({ page }) => {
  await mockApp(page)
  let posts = 0
  let requestIdentity = ''
  await page.route('**/api/marketing/content-packages', async route => {
    posts += 1
    requestIdentity = String(route.request().postDataJSON()?.request_id || '')
    await route.abort('connectionrefused')
  })
  await page.route('**/api/marketing/content-packages/by-request/*', route => json(route, { ok: true, job: successJob }))
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByRole('button', { name: '生成完整推广包' })).toBeEnabled()
  expect(requestIdentity.length).toBeGreaterThan(7)
  await page.reload()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(posts).toBe(1)
})

test('create recovery keeps the stable request id across an initial 404 grace refresh', async ({ page }) => {
  await mockApp(page)
  const stable = 'geo-stable-404-create'
  await page.addInitScript(({ stableId }) => {
    localStorage.setItem('geo_content_center_pending_request_v2:u1:p1', JSON.stringify({
      requestId: stableId, payloadHash: 'a'.repeat(64), createdAt: Date.now() - 10 * 60_000,
    }))
  }, { stableId: stable })
  const recoveredUrls: string[] = []
  await page.route('**/api/marketing/content-packages/by-request/*', async route => {
    recoveredUrls.push(route.request().url())
    if (recoveredUrls.length === 1) await json(route, { detail: '任务不存在' }, 404)
    else await json(route, { ok: true, job: successJob })
  })
  await page.goto('/marketing-materials')
  await expect.poll(() => recoveredUrls.length).toBe(1)
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('geo_content_center_pending_request_v2:u1:p1') || '{}').requestId)).toBe(stable)
  await page.reload()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(recoveredUrls).toHaveLength(2)
  expect(recoveredUrls.every(url => url.endsWith(`/${stable}`))).toBe(true)
})

test('refresh recovers an unconfirmed component retry without a second retry POST', async ({ page }) => {
  const partial = structuredClone(successJob)
  partial.status = 'partial_success'
  partial.components[1].status = 'failed'
  partial.assets = partial.assets.filter(asset => asset.bundle_slot !== 'professional_poster:image:professional_poster')
  await mockApp(page, { job: partial })
  let retryPosts = 0
  let retryIdentity = ''
  await page.route('**/api/marketing/jobs/*/retry', async route => {
    retryPosts += 1
    retryIdentity = String(route.request().postDataJSON()?.request_id || '')
    await route.abort('connectionrefused')
  })
  const recovered = structuredClone(successJob)
  recovered.job_id = 1450
  recovered.is_revision = true
  recovered.revision_no = 2
  await page.route('**/api/marketing/content-packages/by-request/*', route => json(route, { ok: true, job: recovered }))
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await page.getByRole('button', { name: '只重做失败项' }).click()
  await expect(page.getByRole('button', { name: '只重做失败项' })).toBeEnabled()
  expect(retryIdentity.length).toBeGreaterThan(7)
  await page.reload()
  await expect(page.getByText('修订版本 2')).toBeVisible()
  expect(retryPosts).toBe(1)
})

test('child retry recovery keeps the same request id across an initial 404 grace refresh', async ({ page }) => {
  await mockApp(page)
  const stable = 'geo-stable-404-child'
  await page.addInitScript(({ stableId }) => {
    localStorage.setItem('geo_content_center_pending_retry_v2:u1:p1', JSON.stringify({
      retryKey: '901:professional_poster:image:professional_poster',
      requestId: stableId, createdAt: Date.now() - 10 * 60_000,
    }))
  }, { stableId: stable })
  const recoveredUrls: string[] = []
  const recovered = structuredClone(successJob)
  recovered.job_id = 1901
  recovered.is_revision = true
  recovered.revision_no = 2
  await page.route('**/api/marketing/content-packages/by-request/*', async route => {
    recoveredUrls.push(route.request().url())
    if (recoveredUrls.length === 1) await json(route, { detail: '任务不存在' }, 404)
    else await json(route, { ok: true, job: recovered })
  })
  await page.goto('/marketing-materials')
  await expect.poll(() => recoveredUrls.length).toBe(1)
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem('geo_content_center_pending_retry_v2:u1:p1') || '{}').requestId)).toBe(stable)
  await page.reload()
  await expect(page.getByText('修订版本 2')).toBeVisible()
  expect(recoveredUrls).toHaveLength(2)
  expect(recoveredUrls.every(url => url.endsWith(`/${stable}`))).toBe(true)
})

test('same-tab account A to B clears A and reloads only B clients and history', async ({ page }) => {
  await mockApp(page)
  const historyA = structuredClone(successJob)
  historyA.job_id = 2101
  historyA.strategy = { ...strategy, core_angle: '账号 A 独有历史' }
  const historyB = structuredClone(successJob)
  historyB.job_id = 2201
  historyB.strategy = { ...strategy, core_angle: '账号 B 独有历史' }
  await page.route('**/api/auth/me', route => {
    const token = route.request().headers()['authorization'] || ''
    const isB = token === 'Bearer geo-account-b'
    return json(route, {
      success: true,
      user: {
        id: isB ? 2 : 1, username: isB ? 'owner-b' : 'owner-a', display_name: isB ? '账号 B' : '账号 A',
        is_admin: false, is_active: 1, must_change_password: 0, roles: [], permissions: [],
        client_brand_ids: [isB ? 202 : 101], agent_level: 1, permission_version: isB ? 4 : 1,
      },
    })
  })
  await page.route('**/api/my-clients**', route => {
    const isB = route.request().headers()['authorization'] === 'Bearer geo-account-b'
    return json(route, { clients: [{ id: isB ? 202 : 101, name: isB ? '账号 B 独有客户' : '账号 A 独有客户' }], total: 1 })
  })
  await page.route('**/api/marketing/my-materials**', route => {
    const isB = route.request().headers()['authorization'] === 'Bearer geo-account-b'
    return json(route, { ok: true, materials: [isB ? historyB : historyA] })
  })
  await page.goto('/marketing-materials')
  await page.getByLabel('一句话说明你想推广什么').fill('账号 A 的私密推广 brief 和联系方式')
  await page.getByRole('button', { name: /高级设置/ }).click()
  const evidenceA = page.getByRole('group', { name: '真实证据（可选）' })
  await evidenceA.getByRole('combobox').first().selectOption('latest_diagnosis')
  await expect(evidenceA.locator('option', { hasText: '账号 A 独有客户' })).toHaveCount(1)
  await page.getByRole('button', { name: '历史版本' }).click()
  await expect(page.getByText('账号 A 独有历史')).toBeVisible()

  await rotateAuthoritativeSession(page, 'geo-account-b')
  await expect(page.getByLabel('一句话说明你想推广什么')).toHaveValue('')
  await page.getByRole('button', { name: /高级设置/ }).click()
  const evidenceB = page.getByRole('group', { name: '真实证据（可选）' })
  await evidenceB.getByRole('combobox').first().selectOption('latest_diagnosis')
  await expect(evidenceB.locator('option', { hasText: '账号 B 独有客户' })).toHaveCount(1)
  await expect(page.getByText('账号 A 独有客户')).toHaveCount(0)
  await page.getByRole('button', { name: '历史版本' }).click()
  await expect(page.getByText('账号 B 独有历史')).toBeVisible()
  await expect(page.getByText('账号 A 独有历史')).toHaveCount(0)
})

test('same-user safe token refresh preserves and recovers the pending request anchor', async ({ page }) => {
  await mockApp(page)
  await page.route('**/api/auth/me', route => json(route, {
    success: true,
    user: {
      id: 1, username: 'qa-owner', display_name: '质检服务商', is_admin: false, is_active: 1,
      must_change_password: 0, roles: [], permissions: [], client_brand_ids: [101], agent_level: 1,
      permission_version: 1,
    },
  }))
  const stable = 'same-user-refresh-pending'
  const recovered: string[] = []
  await page.route('**/api/marketing/content-packages/by-request/*', route => {
    recovered.push(route.request().url())
    return json(route, { detail: '任务提交中' }, 404)
  })
  await page.goto('/marketing-materials')
  await page.evaluate(({ key, stableId }) => {
    localStorage.setItem(key, JSON.stringify({
      requestId: stableId, payloadHash: 'd'.repeat(64), createdAt: Date.now(),
    }))
  }, { key: pendingRequestKey(1, 1), stableId: stable })
  await rotateAuthoritativeSession(page, 'geo-safe-refresh-successor')
  await expect.poll(() => recovered.length).toBeGreaterThan(0)
  expect(recovered.every(url => url.endsWith(`/${stable}`))).toBe(true)
  expect(await page.evaluate(key => JSON.parse(localStorage.getItem(key) || '{}').requestId, pendingRequestKey(1, 1))).toBe(stable)
})

test('permission version change removes old durable anchors and never recovers them', async ({ page }) => {
  await mockApp(page)
  await page.route('**/api/auth/me', route => {
    const upgraded = route.request().headers()['authorization'] === 'Bearer geo-permission-v2'
    return json(route, {
      success: true,
      user: {
        id: 1, username: 'qa-owner', display_name: '质检服务商', is_admin: false, is_active: 1,
        must_change_password: 0, roles: [], permissions: [], client_brand_ids: [upgraded ? [] : 101],
        agent_level: 1, permission_version: upgraded ? 2 : 1,
      },
    })
  })
  const recoveryUrls: string[] = []
  await page.route('**/api/marketing/content-packages/by-request/*', route => {
    recoveryUrls.push(route.request().url())
    return json(route, { detail: 'must not recover prior generation' }, 404)
  })
  await page.goto('/marketing-materials')
  await page.getByLabel('一句话说明你想推广什么').fill('旧权限代际私密 brief')
  await page.evaluate(({ draft, pending }) => {
    localStorage.setItem(draft, JSON.stringify({ quickTask: 'publish_case', channels: ['douyin'], trend: true, contactMode: 'text' }))
    localStorage.setItem(pending, JSON.stringify({ requestId: 'old-permission-request', payloadHash: 'e'.repeat(64), createdAt: Date.now() }))
  }, { draft: draftKey(1, 1), pending: pendingRequestKey(1, 1) })
  await rotateAuthoritativeSession(page, 'geo-permission-v2')
  await expect(page.getByLabel('一句话说明你想推广什么')).toHaveValue('')
  await expect.poll(() => page.evaluate(key => localStorage.getItem(key), pendingRequestKey(1, 1))).toBeNull()
  await expect.poll(() => page.evaluate(key => localStorage.getItem(key), draftKey(1, 1))).toBeNull()
  expect(await page.evaluate(key => localStorage.getItem(key), pendingRequestKey(1, 2))).toBeNull()
  expect(recoveryUrls).toHaveLength(0)
})

for (const width of [320, 390, 768, 1440, 2560]) {
  test(`generation locks the composer until the tracked job lands at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: width < 700 ? 844 : 1000 })
    await mockApp(page)
    let polls = 0
    await page.route(`**/api/marketing/jobs/${successJob.job_id}`, async route => {
      polls += 1
      if (polls === 1) {
        // 第一轮轮询慢一点：任务仍在后台生成中
        await new Promise(resolve => setTimeout(resolve, 700))
        await json(route, {
          ok: true,
          job: {
            ...successJob,
            status: 'generating',
            assets: [],
            components: successJob.components.map((component) => ({ ...component, status: 'pending' })),
          },
        })
        return
      }
      await json(route, { ok: true, job: successJob, assets: successJob.assets })
    })
    await page.goto('/marketing-materials')
    await page.getByRole('button', { name: '推广 GEO 服务' }).click()
    await expect(page.locator('[data-strategy-key="受众"]')).toContainText('有客户资源')
    await page.getByRole('button', { name: '生成完整推广包' }).click()
    // F1：生成进行期锁定 composer 输入区与高级设置触发器，任务在 UI 始终可见
    await expect(page.getByLabel('一句话说明你想推广什么')).toBeDisabled()
    await expect(page.getByRole('button', { name: /高级设置/ })).toBeDisabled()
    await expect(page.getByRole('button', { name: '信息图' })).toBeDisabled()
    await expect(page.getByRole('button', { name: '编辑受众' })).toBeDisabled()
    await expect(page.getByText('任务正在后台生成，完成后才能修改设置')).toBeVisible()
    await expect(page.locator('.gcc-status', { hasText: '生成中' })).toBeVisible()
    // 任务落地后解锁
    await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
    await expect(page.getByLabel('一句话说明你想推广什么')).toBeEnabled()
    await expect(page.getByRole('button', { name: /高级设置/ })).toBeEnabled()
    // F3：解锁后改 brief，九格失效为占位并由自动理解重新填充
    await page.getByLabel('一句话说明你想推广什么').fill('向连锁餐饮服务商推广 GEO 诊断，邀请先领取一次真实诊断')
    await expect(page.locator('[data-strategy-key="受众"] p.is-placeholder')).toBeVisible()
    await expect(page.locator('[data-strategy-key="受众"]')).toContainText('有客户资源')
  })
}

test('an already-open strategy cell editor stays locked while generation is in flight (N1)', async ({ page }) => {
  await mockApp(page)
  let polls = 0
  await page.route(`**/api/marketing/jobs/${successJob.job_id}`, async route => {
    polls += 1
    if (polls === 1) {
      // 第一轮轮询慢一点：任务仍在后台生成中
      await new Promise(resolve => setTimeout(resolve, 900))
      await json(route, {
        ok: true,
        job: {
          ...successJob,
          status: 'generating',
          assets: [],
          components: successJob.components.map((component) => ({ ...component, status: 'pending' })),
        },
      })
      return
    }
    await json(route, { ok: true, job: successJob, assets: successJob.assets })
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  const cell = page.locator('[data-strategy-key="核心卖点"]')
  await expect(cell).toContainText('把诊断、内容和监测')
  // N1 复现路径：打开编辑器不保存，直接点生成（编辑器不收起）
  await cell.getByRole('button', { name: '编辑核心卖点' }).click()
  await cell.locator('textarea').fill('锁定期间不得保存的新卖点')
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  // 生成进行期：已打开的编辑器三件套全部锁定，保存不生效
  await expect(cell.locator('textarea')).toBeDisabled()
  const saveButton = cell.getByRole('button', { name: '保存' })
  await expect(saveButton).toBeDisabled()
  await saveButton.click({ force: true })
  // 任务不消失：跟踪视图仍在，轮询继续直到落地
  await expect(page.locator('.gcc-status', { hasText: '生成中' })).toBeVisible()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  // 锁定期的编辑没有落进九格（按钮禁用 + saveStrategyCell 守卫双保险）
  await cell.getByRole('button', { name: '取消' }).click()
  await expect(cell).toContainText('把诊断、内容和监测变成客户能看见的证据链')
  await expect(cell).not.toContainText('锁定期间不得保存的新卖点')
})

test('history entries are locked while a job is generating (N3)', async ({ page }) => {
  await mockApp(page)
  await page.route('**/api/marketing/my-materials**', route => json(route, { ok: true, materials: [successJob] }))
  let polls = 0
  await page.route(`**/api/marketing/jobs/${successJob.job_id}`, async route => {
    polls += 1
    if (polls === 1) {
      // 第一轮轮询慢一点：任务仍在后台生成中
      await new Promise(resolve => setTimeout(resolve, 900))
      await json(route, {
        ok: true,
        job: {
          ...successJob,
          status: 'generating',
          assets: [],
          components: successJob.components.map((component) => ({ ...component, status: 'pending' })),
        },
      })
      return
    }
    await json(route, { ok: true, job: successJob, assets: successJob.assets })
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.locator('.gcc-status', { hasText: '生成中' })).toBeVisible()
  // N3：生成进行期历史条目锁定，点开历史不能把跟踪中的任务顶出结果区
  await page.getByRole('button', { name: '历史版本' }).click()
  const entry = page.locator('.gcc-history > button', { hasText: '先看 AI 的真实回答' })
  await expect(entry).toBeDisabled()
  await expect(page.getByText('任务生成中，落地后可查看历史版本。')).toBeVisible()
  // 跟踪视图不被顶掉；任务落地后历史条目解锁
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  await expect(entry).toBeEnabled()
})

test('terminal failed child keeps parent assets and next explicit retry gets a new identity', async ({ page }) => {
  const partial = structuredClone(successJob)
  partial.status = 'partial_success'
  partial.components[1].status = 'failed'
  partial.assets = partial.assets.filter(asset => asset.bundle_slot !== 'professional_poster:image:professional_poster')
  await mockApp(page, { job: partial })
  const ids: string[] = []
  let retry = 0
  await page.route('**/api/marketing/jobs/*/retry', async route => {
    ids.push(String(route.request().postDataJSON()?.request_id || ''))
    retry += 1
    await json(route, { ok: true, status: 'generating', job_id: 1200 + retry })
  })
  await page.route('**/api/marketing/jobs/120*', async route => {
    const child = structuredClone(partial)
    child.job_id = Number(route.request().url().split('/').pop())
    child.status = 'failed'
    await json(route, { ok: true, job: child })
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await page.getByRole('button', { name: '只重做失败项' }).click()
  await expect(page.locator('[data-channel-section="professional_poster"]').getByText(copyPayload.title)).toBeVisible()
  await page.getByRole('button', { name: '只重做失败项' }).click()
  expect(ids).toHaveLength(2)
  expect(ids[0]).not.toBe(ids[1])
})

test('failed QR upload allows re-selecting the same file (F8)', async ({ page }) => {
  await mockApp(page, { qrInvalid: true })
  let uploads = 0
  await page.route('**/api/marketing/qr-reference', async (route) => {
    uploads += 1
    await json(route, { detail: 'qr_requires_exactly_one_decodable_code' }, 422)
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: /高级设置/ }).click()
  await page.getByRole('button', { name: '上传二维码', exact: true }).click()
  const input = page.locator('.gcc-qr-upload input[type="file"]')
  await input.setInputFiles({ name: 'same-qr.png', mimeType: 'image/png', buffer: Buffer.from('bad-qr') })
  await expect(page.getByText('这个文件没有且仅有一个可扫码二维码，请换一张清晰原图。')).toBeVisible()
  expect(uploads).toBe(1)
  // 同名文件重选必须再次触发上传（input.value 已在 onChange 首行清空）
  await input.setInputFiles({ name: 'same-qr.png', mimeType: 'image/png', buffer: Buffer.from('bad-qr') })
  await expect.poll(() => uploads).toBe(2)
})

test('copy edit survives a slower retry poll that returns the pre-edit snapshot (F11)', async ({ page }) => {
  const partial = structuredClone(successJob)
  partial.status = 'partial_success'
  partial.error_summary = 'partial_free_release'
  partial.components[1].status = 'failed'
  partial.assets = partial.assets.filter((asset) => asset.bundle_slot !== 'professional_poster:image:professional_poster')
  await mockApp(page, { job: partial })
  // 轮询回包比 PATCH 慢，且带的是编辑前的文案快照（F11 竞态窗口）
  await page.route(`**/api/marketing/jobs/${partial.job_id}`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500))
    await json(route, { ok: true, job: partial, assets: partial.assets })
  })
  await page.route('**/api/marketing/assets/*', async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, any>
    const content = { ...copyPayload, title: String(body.updates?.title || '') }
    await json(route, { ok: true, asset_id: 2, channel: 'professional_poster', content, qa: { passed: true, errors: [], warnings: [] } })
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await page.getByRole('button', { name: '生成完整推广包' }).click()
  await expect(page.getByText('部分成功', { exact: true })).toBeVisible()
  const poster = page.locator('[data-channel-section="professional_poster"]')
  await poster.getByRole('button', { name: '编辑' }).click()
  await poster.locator('.gcc-edit-field', { hasText: '标题' }).locator('textarea').first().fill('改后的标题：竞态下也保留')
  await poster.getByRole('button', { name: '保存文案' }).click()
  await expect(poster.getByText('改后的标题：竞态下也保留')).toBeVisible()
  // 局部重试：轮询回包慢且带旧快照，落地后 UI 不得回退到旧文案
  await poster.getByRole('button', { name: '局部重试' }).click()
  await page.waitForTimeout(900)
  await expect(poster.getByText('改后的标题：竞态下也保留')).toBeVisible()
  await expect(poster.getByText(copyPayload.title)).toHaveCount(0)
})

test('generate falls back to a non-crypto hash outside secure contexts (F13)', async ({ page }) => {
  await mockApp(page)
  await page.addInitScript(() => {
    // 模拟非 secure-context（http 非 localhost）：crypto.subtle 不可用
    try {
      Object.defineProperty(window.crypto, 'subtle', { value: undefined, configurable: true })
    } catch { /* 环境不允许覆盖时本用例退化为冒烟 */ }
  })
  await openAndGenerate(page)
})

test('normal history marks showcase-deal packages with a type badge (F14)', async ({ page }) => {
  await mockApp(page)
  const showcaseMomentsOnly = structuredClone(successJob)
  showcaseMomentsOnly.job_id = 3101
  ;(showcaseMomentsOnly as Record<string, unknown>).quick_task = 'showcase_deal'
  showcaseMomentsOnly.channels = ['moments']
  showcaseMomentsOnly.strategy = { ...strategy, core_angle: '晒成交朋友圈包' }
  await page.route('**/api/marketing/my-materials**', (route) => json(route, { ok: true, materials: [showcaseMomentsOnly] }))
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '历史版本' }).click()
  const entry = page.locator('.gcc-history > button', { hasText: '晒成交朋友圈包' })
  await expect(entry).toBeVisible()
  await expect(entry.getByText('晒成交', { exact: true })).toBeVisible()
})

test('QA warnings render as amber collapsible reminders and never block output', async ({ page }) => {
  // Owner 2026-07-22 分层:warnings 琥珀色提醒条(渠道分段 + job 总状态条),
  // 不出现红色失败样式,成品与操作不受任何阻断。
  // SSOT 2026-07-23 五问合同:reason/repair_hint/actions/rule_version 随条渲染。
  const warned = structuredClone(successJob)
  ;(warned as any).warnings = [
    {
      code: 'number_without_evidence', message: '文案里的数字没有对应证据来源，发布前请核对',
      reason: '没有证据来源的数字发布出去无法自证，客户质疑时内容站不住',
      repair_hint: 'AI 可以把没有依据的数字删掉或换成有证据的表述',
      actions: [{ id: 'edit_copy', label: '修改文案' }, { id: 'confirm_continue', label: '确认无误，继续' }],
      rule_version: 'geo-content-qa/1.0.0',
      channel: 'professional_poster', component_id: 'professional_poster:copy',
    },
    { code: 'diagnosis_case_requires_frozen_evidence', message: '案例卡引用的诊断数据建议先发布诊断报告；本版已按常青口径生成', channel: '', component_id: '' },
  ]
  await mockApp(page, { job: warned })
  await openAndGenerate(page)

  // 渠道分段旁:可折叠琥珀提醒条,人话 message
  const poster = page.locator('[data-channel-section="professional_poster"]')
  const bar = poster.locator('.gcc-warnings[data-warnings-scope="channel"]')
  await expect(bar).toBeVisible()
  await expect(bar.getByRole('button')).toContainText('1 条提醒')
  await bar.getByRole('button').click()
  await expect(bar.getByText('文案里的数字没有对应证据来源，发布前请核对')).toBeVisible()
  // 五问合同渲染:为什么影响交付 + AI 可修提示 + 人如何继续 + 规则版本
  await expect(bar.getByText('没有证据来源的数字发布出去无法自证，客户质疑时内容站不住')).toBeVisible()
  await expect(bar.locator('.gcc-warning-repair')).toContainText('AI 可修')
  await expect(bar.locator('.gcc-warning-actions')).toContainText('确认无误，继续')
  await expect(bar.getByText('规则版本 geo-content-qa/1.0.0')).toBeVisible()
  // 不阻断:成品照常渲染、操作按钮可用、状态徽标仍是「已完成」(非红色失败)
  await expect(poster.getByAltText('专业海报最终成图')).toBeVisible()
  await expect(poster.getByRole('button', { name: '复制文案' })).toBeEnabled()
  await expect(poster.locator('.gcc-badge.green', { hasText: '已完成' })).toBeVisible()
  await expect(poster.locator('.gcc-fail-panel')).toHaveCount(0)
  // 无提醒的渠道不出现提醒条
  await expect(page.locator('[data-channel-section="douyin"] .gcc-warnings')).toHaveCount(0)

  // job 级提醒汇总进总状态条
  const jobBar = page.locator('.gcc-warnings[data-warnings-scope="job"]')
  await expect(jobBar).toBeVisible()
  await expect(jobBar.getByRole('button')).toContainText('2 条发布前提醒')
  await jobBar.getByRole('button').click()
  await expect(jobBar.getByText('案例卡引用的诊断数据建议先发布诊断报告；本版已按常青口径生成')).toBeVisible()
})

test('warnings preview turns generate into an explicit confirm action carrying warnings_acknowledged', async ({ page }) => {
  // 诚实化确认流(2026-07-23 P1-2):interpret 预览有提醒时,生成按钮变为显式
  // 确认动作「知道了，继续生成」,点击后请求必须带 warnings_acknowledged=true——
  // 后端仅凭该标记记「用户确认继续」审计,不再自动声称。
  const createPayloads: any[] = []
  await mockApp(page, {
    interpretWarnings: [
      {
        code: 'strategy_promise_claim', message: '策略里有承诺/保证类表述，请确认都能兑现',
        reason: '策略里的承诺/保证或措辞偏强表述会传导到所有渠道文案',
        repair_hint: 'AI 可以只改写策略中含承诺词的格子', rule_version: 'geo-content-qa/1.0.0',
      },
    ],
    createPayloads,
  })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  // 提醒预览真实渲染(可折叠琥珀条),按钮文案变为显式确认
  const jobBar = page.locator('.gcc-generate-row .gcc-warnings[data-warnings-scope="job"]')
  await expect(jobBar).toBeVisible()
  await expect(jobBar.getByRole('button')).toContainText('1 条发布前提醒')
  const confirmButton = page.getByRole('button', { name: '知道了，继续生成' })
  await expect(confirmButton).toBeVisible()
  await expect(page.getByRole('button', { name: '生成完整推广包' })).toHaveCount(0)
  await confirmButton.click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(createPayloads).toHaveLength(1)
  expect(createPayloads[0].warnings_acknowledged).toBe(true)
})

test('no warnings keeps the plain generate action and sends warnings_acknowledged=false', async ({ page }) => {
  // 反向对照:无提醒时按钮是普通生成,不做伪确认,标记必须为 false。
  const createPayloads: any[] = []
  await mockApp(page, { createPayloads })
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: '推广 GEO 服务' }).click()
  await expect(page.locator('.gcc-generate-row .gcc-warnings')).toHaveCount(0)
  const plainButton = page.getByRole('button', { name: '生成完整推广包' })
  await expect(plainButton).toBeVisible()
  await plainButton.click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(createPayloads).toHaveLength(1)
  expect(createPayloads[0].warnings_acknowledged).toBe(false)
})
