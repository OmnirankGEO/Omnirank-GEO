import { expect, test, type Page, type Route } from 'playwright/test'
import path from 'node:path'

/**
 * 晒成交（Deal Studio）主链路：
 * 三输入（图片/语音/表单）→ analyze 确认单（tentative 标）→ 打码
 * （auto/restore/add/strength/confirm）→ showcase_deal 生成 → 渠道分段结果
 * （复制/下载/编辑/局部重试/调整打码/更换风格/历史版本）。
 * mock API 与 geo-content-center.spec.ts 的 page.route 模式一致。
 */

const draftAnchorKey = (userId: number, permissionVersion: number) =>
  `geo_deal_studio_draft_v1:u${userId}:p${permissionVersion}`
const pendingRequestKey = (userId: number, permissionVersion: number) =>
  `geo_deal_studio_pending_request_v1:u${userId}:p${permissionVersion}`
const pendingRetryKey = (userId: number, permissionVersion: number) =>
  `geo_deal_studio_pending_retry_v1:u${userId}:p${permissionVersion}`

/* 与 geo-content-center.spec.ts 相同的权威会话轮换助手：暂存新 token 并派发
   token-refreshed，等待 AuthContext 完成 /me 确认。 */
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

const teacher = {
  teacher_id: 'shu',
  name: '舒老师',
  version: '1.0.0',
  system_method: '受众现状→行动阻力→一句人话问题→唯一卖点→真实证据→低门槛唯一行动。',
  source: 'platform',
}

const SHEET_KEYS = [
  'what_happened', 'deal_amount', 'deal_time', 'customer_industry', 'service_content',
  'deal_reason', 'customer_praise', 'quotable_lines', 'image_materials',
  'privacy_redaction_items', 'contact_and_qr', 'target_channels',
]

function sheetFixture(): Record<string, { value: string; status: string; provenance: string }> {
  const sheet: Record<string, { value: string; status: string; provenance: string }> = {}
  for (const key of SHEET_KEYS) sheet[key] = { value: '', status: 'tentative', provenance: 'customer_asserted' }
  sheet.what_happened = { value: '签约一家全屋定制客户，提供 GEO 品牌可见度优化服务。', status: 'confirmed', provenance: 'customer_asserted' }
  sheet.deal_amount = { value: '约 3 万元', status: 'tentative', provenance: 'customer_asserted' }
  sheet.deal_time = { value: '2026 年 6 月', status: 'confirmed', provenance: 'customer_asserted' }
  sheet.customer_industry = { value: '全屋定制（家装）', status: 'confirmed', provenance: 'customer_asserted' }
  sheet.service_content = { value: 'GEO 品牌可见度诊断 + 3 个月优化', status: 'confirmed', provenance: 'customer_asserted' }
  sheet.deal_reason = { value: '客户被 AI 搜索曝光差距触动，想先看真实诊断。', status: 'tentative', provenance: 'customer_asserted' }
  sheet.customer_praise = { value: '诊断报告讲得清楚，先看到数据再决定', status: 'confirmed', provenance: 'customer_asserted' }
  sheet.quotable_lines = { value: '「人家看了诊断报告，直接约的面谈。」', status: 'confirmed', provenance: 'customer_asserted' }
  return sheet
}

function emptyRedaction() {
  return {
    auto_regions: [] as any[],
    manual_regions: [] as any[],
    restored_ids: [] as string[],
    strength: 'medium',
    status: 'none',
    detection: '',
    has_preview: false,
    has_output: false,
  }
}

function materialFixture(id = 'mat001') {
  return {
    material_id: id,
    width: 800,
    height: 1000,
    size_bytes: 120000,
    original_filename: 'chat.png',
    ocr_text: '',
    extracted: false,
    redaction: emptyRedaction(),
  }
}

function makeDraft(overrides: Record<string, any> = {}) {
  return {
    id: 42,
    brand_id: null,
    request_id: 'deal-test-request-id',
    form: {},
    materials: [],
    sheet: {},
    status: 'draft',
    created_at: '2026-07-21T12:00:00Z',
    updated_at: '2026-07-21T12:00:00Z',
    ...overrides,
  }
}

const dealCopy = {
  title: '又一位全屋定制客户签约了',
  body: '客户先看了诊断报告，一次面谈就定下了 3 个月优化。',
  tags: ['#真实成交', '#GEO'],
}

const dealJob = {
  job_id: 955,
  status: 'succeeded',
  error_summary: '',
  quick_task: 'showcase_deal',
  assets: [
    { id: 101, asset_kind: 'bundle_item', bundle_slot: 'deal_poster:image:deal_poster', download_url: '/api/marketing/materials/101/download' },
    { id: 102, asset_kind: 'copy', bundle_slot: 'deal_poster:copy', content_text: JSON.stringify(dealCopy) },
    { id: 103, asset_kind: 'bundle_item', bundle_slot: 'deal_chat:image:deal_chat_scene', download_url: '/api/marketing/materials/103/download' },
    { id: 104, asset_kind: 'copy', bundle_slot: 'deal_chat:copy', content_text: JSON.stringify({ title: '客户当场拍板的对话', body: '看完诊断报告样例，客户直接约了面谈。', tags: ['#聊天晒单'] }) },
    { id: 105, asset_kind: 'bundle_item', bundle_slot: 'deal_data_card:image:deal_data_card', download_url: '/api/marketing/materials/105/download' },
    { id: 106, asset_kind: 'copy', bundle_slot: 'deal_data_card:copy', content_text: JSON.stringify({ title: '一笔真实成交的数据', facts: [{ label: '成交金额', value: '3 万元' }, { label: '成交时间', value: '2026 年 6 月' }], body: '客户自述成交事实，生成时已冻结。', tags: ['#数据卡片'] }) },
    { id: 107, asset_kind: 'bundle_item', bundle_slot: 'deal_story:image:deal_story', download_url: '/api/marketing/materials/107/download' },
    { id: 108, asset_kind: 'copy', bundle_slot: 'deal_story:copy', content_text: JSON.stringify({ title: '从诊断到签约的 12 天', body: '先看真实数据，再谈优化方案。', tags: ['#签约故事'] }) },
    { id: 109, asset_kind: 'bundle_item', bundle_slot: 'moments:image:moments', download_url: '/api/marketing/materials/109/download' },
    { id: 110, asset_kind: 'copy', bundle_slot: 'moments:copy', content_text: JSON.stringify({ title: '晒一笔真实成交', tones: { restrained: '又一单签约，过程如实记录。', professional: '用诊断证据说话，客户自己做了决定。', friendly: '感谢老客户介绍，又成一单。' }, tags: ['#晒成交'] }) },
  ],
  components: [
    { component_id: 'deal_poster:copy', channel: 'deal_poster', kind: 'copy', status: 'succeeded' },
    { component_id: 'deal_poster:image:deal_poster', channel: 'deal_poster', kind: 'image', status: 'succeeded', slot: { slot: 'deal_poster', size: '3:4' } },
    { component_id: 'deal_chat:copy', channel: 'deal_chat', kind: 'copy', status: 'succeeded' },
    { component_id: 'deal_chat:image:deal_chat_scene', channel: 'deal_chat', kind: 'image', status: 'succeeded', slot: { slot: 'deal_chat_scene', size: '9:16' } },
    { component_id: 'deal_data_card:copy', channel: 'deal_data_card', kind: 'copy', status: 'succeeded' },
    { component_id: 'deal_data_card:image:deal_data_card', channel: 'deal_data_card', kind: 'image', status: 'succeeded', slot: { slot: 'deal_data_card', size: '3:4' } },
    { component_id: 'deal_story:copy', channel: 'deal_story', kind: 'copy', status: 'succeeded' },
    { component_id: 'deal_story:image:deal_story', channel: 'deal_story', kind: 'image', status: 'succeeded', slot: { slot: 'deal_story', size: '9:16' } },
    { component_id: 'moments:copy', channel: 'moments', kind: 'copy', status: 'succeeded' },
    { component_id: 'moments:image:moments', channel: 'moments', kind: 'image', status: 'succeeded', slot: { slot: 'moments', size: '1:1' } },
  ],
  channels: ['deal_poster', 'deal_chat', 'deal_data_card', 'deal_story', 'moments'],
  teacher,
  strategy: { core_angle: '用一笔真实成交的完整过程代替夸张宣传' },
  evidence: { facts: [], source_note: '成交事实来自客户确认单，未经平台核验' },
  trend: { requested: false, used: false, reason: 'not_requested' },
  contact: { mode: 'none', text: '' },
  created_at: '2026-07-21T12:00:00Z',
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

type DealMockOptions = {
  draft?: ReturnType<typeof makeDraft>
  analyzeDelayMs?: number
  job?: typeof dealJob
  historyJobs?: any[]
}

async function mockDealApp(page: Page, options: DealMockOptions = {}) {
  const calls = {
    creates: [] as any[],
    analyze: 0,
    patches: [] as any[],
    redacts: [] as any[],
    generates: [] as any[],
    retries: [] as any[],
    assetPatches: [] as any[],
    qrUploads: 0,
  }
  const job = options.job || dealJob
  let sheetState: Record<string, any> = { ...(options.draft?.sheet || {}) }
  let materialsState: any[] = (options.draft?.materials || []).map((material) => ({
    ...material,
    redaction: { ...emptyRedaction(), ...(material.redaction || {}) },
  }))
  let formState: Record<string, string> = { ...(options.draft?.form || {}) }
  let redactionState = { ...emptyRedaction(), ...(options.draft?.materials?.[0]?.redaction || {}) }
  const currentDraft = () => makeDraft({
    ...(options.draft || {}),
    form: { ...formState },
    materials: materialsState.map((material) => ({
      ...material,
      redaction: material.material_id === 'mat001' ? { ...redactionState } : { ...material.redaction },
    })),
    sheet: sheetState,
  })

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
  await page.route('**/api/marketing/deal-drafts/*/materials/*/file**', async (route) => route.fulfill({
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
    ok: true, teachers: [teacher], default_teacher: teacher, trend_provider_available: false,
    service_brand: { configured: true, name: '质检服务商', has_logo: false },
  }))
  await page.route('**/api/my-clients**', async (route) => json(route, { clients: [{ id: 101, name: '全屋定制客户公司' }], total: 1 }))
  await page.route('**/api/marketing/my-materials**', async (route) => json(route, { ok: true, materials: options.historyJobs || [] }))

  await page.route('**/api/marketing/deal-drafts', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, any>
    calls.creates.push(body)
    formState = Object.fromEntries(Object.entries(body?.form || {}).map(([key, value]) => [key, String(value)]))
    const draft = currentDraft()
    draft.request_id = String(body?.request_id || 'deal-test-request-id')
    await json(route, { ok: true, created: true, draft })
  })
  await page.route('**/api/marketing/deal-drafts/42', async (route) => {
    if (route.request().method() === 'GET') return json(route, { ok: true, draft: options.draft ? { ...options.draft } : currentDraft() })
    if (route.request().method() === 'PATCH') {
      const body = route.request().postDataJSON() as { sheet?: Record<string, any> }
      calls.patches.push(body)
      for (const [key, raw] of Object.entries(body?.sheet || {})) {
        const value = typeof raw === 'object' && raw !== null ? String(raw.value ?? '') : String(raw ?? '')
        sheetState[key] = { value, status: 'confirmed', provenance: 'customer_asserted' }
      }
      return json(route, { ok: true, sheet: sheetState, draft: currentDraft() })
    }
    return route.fallback()
  })
  await page.route('**/api/marketing/deal-drafts/42/materials', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const added = [materialFixture()]
    materialsState = [...materialsState, ...added]
    await json(route, { ok: true, added, draft: currentDraft() })
  })
  await page.route('**/api/marketing/deal-drafts/42/voice', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    formState.voice_transcript = '上个月签了个做全屋定制的客户，三万的单子。'
    await json(route, { ok: true, transcript: formState.voice_transcript, provider: 'mock', draft: currentDraft() })
  })
  await page.route('**/api/marketing/deal-drafts/42/analyze', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    calls.analyze += 1
    if (options.analyzeDelayMs) await new Promise((resolve) => setTimeout(resolve, options.analyzeDelayMs))
    sheetState = sheetFixture()
    await json(route, { ok: true, sheet: sheetState, draft: currentDraft() })
  })
  await page.route('**/api/marketing/deal-drafts/42/redact', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, any>
    calls.redacts.push(body)
    const action = String(body?.action || '')
    if (action === 'auto') {
      redactionState.auto_regions = [
        { id: 'r1', kind: 'phone', box: [100, 200, 220, 40], source: 'auto', active: true },
        { id: 'r2', kind: 'name', box: [100, 300, 180, 36], source: 'auto', active: true },
      ]
      redactionState.detection = 'ocr'
      redactionState.status = 'previewed'
      redactionState.has_preview = true
    } else if (action === 'add') {
      redactionState.manual_regions = [
        ...redactionState.manual_regions,
        { id: `m${redactionState.manual_regions.length + 1}`, kind: String(body?.kind || 'other'), box: body?.box, source: 'manual', active: true },
      ]
      redactionState.status = 'previewed'
      redactionState.has_preview = true
    } else if (action === 'restore') {
      const regionId = String(body?.region_id || '')
      redactionState.restored_ids = [...redactionState.restored_ids, regionId]
      redactionState.auto_regions = redactionState.auto_regions.map((region) => region.id === regionId ? { ...region, active: false } : region)
      redactionState.status = 'previewed'
      redactionState.has_preview = true
    } else if (action === 'strength') {
      redactionState.strength = String(body?.strength || 'medium')
      redactionState.status = 'previewed'
      redactionState.has_preview = true
    } else if (action === 'confirm') {
      redactionState.status = 'confirmed'
      redactionState.has_output = true
    }
    await json(route, {
      ok: true,
      material_id: 'mat001',
      redaction: redactionState,
      preview_url: '/api/marketing/deal-drafts/42/materials/mat001/file?variant=preview',
      draft: currentDraft(),
    })
  })

  await page.route('**/api/marketing/qr-reference', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    calls.qrUploads += 1
    await json(route, {
      ok: true,
      qr_reference: {
        reference_id: 'qr-deal-test-001.png',
        payload_hash: 'b'.repeat(64),
        file_sha256: 'c'.repeat(64),
        payload_preview: 'https://example.test/contact',
        reference_token: 'qr-deal-token-test',
        organization_id: null,
        uploaded_at: '2026-07-23T00:00:00Z',
      },
    })
  })
  await page.route('**/api/marketing/content-packages', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, any>
    calls.generates.push(body)
    await json(route, { ok: true, status: 'generating', job_id: job.job_id })
  })
  await page.route(`**/api/marketing/jobs/${job.job_id}`, async (route) => json(route, { ok: true, job, assets: job.assets }))
  await page.route('**/api/marketing/jobs/956', async (route) => {
    const child = { ...job, job_id: 956, is_revision: true, revision_no: 2 }
    await json(route, { ok: true, job: child, assets: child.assets })
  })
  await page.route('**/api/marketing/jobs/*/retry', async (route) => {
    const body = route.request().postDataJSON() as Record<string, any>
    calls.retries.push(body)
    await json(route, { ok: true, status: 'generating', job_id: 956 })
  })
  await page.route('**/api/marketing/assets/*', async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    const body = route.request().postDataJSON() as Record<string, any>
    calls.assetPatches.push(body)
    const content = { ...dealCopy, title: String(body?.updates?.title || dealCopy.title) }
    await json(route, { ok: true, asset_id: 102, channel: 'deal_poster', content, qa: { passed: true, errors: [], warnings: [] } })
  })
  return calls
}

async function openDealStudio(page: Page) {
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: /晒成交/ }).click()
  await expect(page.getByTestId('deal-studio')).toBeVisible()
}

test('deal studio mocked-API studio flow: intake → sheet tentative → redact → generate → results with edit and local retry', async ({ page }) => {
  // 名称诚实化(2026-07-23 P2-4):本用例 API 全部由 page.route mock,是前端工作
  // 室流程测试,不得声称「全链路」;真实后端链路见 pytest test_review_cto_regressions
  // 的 test_real_fastapi_to_freeze_worker_fake_provider_and_settlement_full_chain。
  const calls = await mockDealApp(page)
  await openDealStudio(page)

  /* 第一步：上传图片 + 表单 */
  await page.getByTestId('deal-image-input').setInputFiles({ name: 'chat.png', mimeType: 'image/png', buffer: Buffer.from('png-bytes') })
  await expect(page.getByAltText('成交素材第 1 张')).toBeVisible()
  expect(calls.creates).toHaveLength(1)
  await page.getByRole('button', { name: /补充成交细节/ }).click()
  await page.getByLabel('成交金额').fill('3 万元')

  /* 整理成交信息（重复点击防护） */
  const analyzeButton = page.getByRole('button', { name: '整理成交信息' })
  await analyzeButton.click()
  await expect(page.getByText('成交信息确认单')).toBeVisible()
  expect(calls.analyze).toBe(1)

  /* 第二步：确认单 —— 表单覆盖 confirmed，剩余 tentative 标 amber */
  await expect(page.getByText('不会替你编造客户名、订单号或金额')).toBeVisible()
  await expect(page.locator('.gcc-badge.amber', { hasText: '待确认' })).toHaveCount(1)
  const reasonField = page.locator('.gcc-deal-sheet-field', { hasText: '成交原因' }).first()
  await expect(reasonField.locator('.gcc-badge.amber')).toBeVisible()
  await expect(page.getByLabel('成交金额')).toHaveValue('3 万元')
  await reasonField.locator('textarea').fill('客户看过诊断报告样例后一次面谈成交。')
  await page.getByRole('button', { name: '下一步：隐私打码' }).click()
  expect(calls.patches.length).toBeGreaterThanOrEqual(2)
  expect(calls.patches.at(-1)?.sheet?.deal_reason?.value).toBe('客户看过诊断报告样例后一次面谈成交。')

  /* 第三步：打码 auto → restore → strength → confirm */
  await expect(page.getByText('隐私打码', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: /一键全打码/ }).click()
  await expect(page.locator('.gcc-deal-region-row')).toHaveCount(2)
  await expect(page.locator('.gcc-deal-region-row', { hasText: '手机号' })).toHaveCount(1)
  await page.locator('.gcc-deal-region-row').first().getByRole('button', { name: /恢复/ }).click()
  await expect(page.locator('.gcc-deal-region-row.is-restored')).toHaveCount(1)
  expect(calls.redacts.map((call) => call.action)).toEqual(['auto', 'restore'])
  await page.getByLabel('打码强度').press('End')
  await expect.poll(() => calls.redacts.at(-1)?.action).toBe('strength')
  expect(calls.redacts.at(-1)?.strength).toBe('heavy')
  await page.getByRole('button', { name: '预览确认：这张可以用了' }).click()
  await expect(page.getByText('这张素材的打码已确认')).toBeVisible()
  expect(calls.redacts.at(-1)?.action).toBe('confirm')
  await page.getByRole('button', { name: /下一步：生成整套内容/ }).click()

  /* 第四步：生成 payload 合同 */
  const generateButton = page.getByRole('button', { name: '确认并生成整套内容' })
  await generateButton.click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(calls.generates).toHaveLength(1)
  const payload = calls.generates[0]
  expect(payload.quick_task).toBe('showcase_deal')
  expect(payload.deal_draft_id).toBe(42)
  expect(payload.channels).toContain('deal_poster')
  expect(payload.channels).toContain('moments')
  expect(payload.contact.mode).toBe('none')
  expect(payload.visual_style).toBe('festive')
  expect(payload.resolution).toBe('1k')
  expect(String(payload.request_id).length).toBeGreaterThan(7)

  /* 结果：渠道分段 + 固定操作 + deal 专用操作 */
  for (const id of ['deal_poster', 'deal_chat', 'deal_data_card', 'deal_story', 'moments']) {
    await expect(page.locator(`[data-channel-section="${id}"]`)).toBeVisible()
  }
  await expect(page.getByAltText('喜报海报最终成图')).toBeVisible()
  await expect(page.locator('[data-channel-section="deal_chat"]').getByText('示例对话图按规范带「示例对话」标识')).toBeVisible()
  await expect(page.locator('[data-channel-section="deal_data_card"]').getByText('成交金额：3 万元')).toBeVisible()
  await expect(page.getByRole('button', { name: '调整打码' })).toBeVisible()
  await expect(page.getByRole('button', { name: '下载全部文案' })).toBeVisible()

  /* 编辑文案：只改文字不动图 */
  const poster = page.locator('[data-channel-section="deal_poster"]')
  await poster.getByRole('button', { name: '编辑' }).click()
  await poster.locator('.gcc-edit-field', { hasText: '标题' }).locator('textarea').first().fill('改后的喜报标题')
  await poster.getByRole('button', { name: '保存文案' }).click()
  await expect(poster.getByText('改后的喜报标题')).toBeVisible()
  expect(calls.assetPatches).toHaveLength(1)
  await expect(poster.getByAltText('喜报海报最终成图')).toBeVisible()

  /* 局部重试：只重做聊天晒单这一项 */
  await page.locator('[data-channel-section="deal_chat"]').getByRole('button', { name: '只重做这一项' }).click()
  await expect(page.getByText('修订版本 2')).toBeVisible()
  expect(calls.retries).toHaveLength(1)
  expect(calls.retries[0].component_ids).toEqual(['deal_chat:copy', 'deal_chat:image:deal_chat_scene'])
})

test('transcript edits merge into the sheet and re-analyze asks before overwriting manual edits', async ({ page }) => {
  const calls = await mockDealApp(page, {
    draft: makeDraft({
      form: { voice_transcript: '上个月签了个做全屋定制的客户，三万的单子。' },
      sheet: sheetFixture(),
      status: 'analyzed',
    }),
  })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 2, maxStep: 2, anonymous: true, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  await openDealStudio(page)

  /* 恢复到第二步（确认单已存在） */
  await expect(page.getByText('成交信息确认单')).toBeVisible()
  /* 回到第一步：转写可编辑 */
  await page.getByRole('button', { name: /提供素材/ }).click()
  const transcript = page.getByLabel('语音转写文字')
  await expect(transcript).toHaveValue(/全屋定制/)
  await transcript.fill('上个月签了个做全屋定制的客户，三万二的单子，客户先看诊断再定。')
  await page.getByRole('button', { name: '整理成交信息' }).click()
  await expect(page.getByText('重新整理会用最新素材重出确认单')).toBeVisible()
  await page.getByRole('button', { name: '确认重新整理' }).click()
  await expect(page.getByText('成交信息确认单')).toBeVisible()
  expect(calls.analyze).toBe(1)
  const mergePatch = calls.patches.find((body) => body?.sheet?.what_happened)
  expect(mergePatch?.sheet?.what_happened?.value).toContain('三万二')
})

test('canvas box select adds a manual redaction region with image pixel coordinates', async ({ page }) => {
  const redactedMaterial = materialFixture()
  redactedMaterial.redaction = {
    ...emptyRedaction(),
    auto_regions: [{ id: 'r1', kind: 'phone', box: [100, 200, 220, 40], source: 'auto', active: true }],
    status: 'previewed',
    has_preview: true,
    detection: 'ocr',
  }
  const calls = await mockDealApp(page, {
    draft: makeDraft({ materials: [redactedMaterial], sheet: sheetFixture(), status: 'redacted' }),
  })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 3, maxStep: 3, anonymous: true, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  await openDealStudio(page)

  await expect(page.getByText('隐私打码', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: '框选新增打码区' }).click()
  await expect(page.getByText('在图上按住拖出要遮住的区域')).toBeVisible()
  const stage = page.locator('.gcc-deal-stage-img')
  const box = await stage.boundingBox()
  expect(box).toBeTruthy()
  await page.mouse.move(box!.x + box!.width * 0.2, box!.y + box!.height * 0.3)
  await page.mouse.down()
  await page.mouse.move(box!.x + box!.width * 0.6, box!.y + box!.height * 0.45, { steps: 4 })
  await page.mouse.up()
  await expect.poll(() => calls.redacts.at(-1)?.action).toBe('add')
  const addCall = calls.redacts.at(-1)
  expect(addCall?.kind).toBe('other')
  const [x, y, w, h] = addCall?.box || []
  expect(x).toBeGreaterThanOrEqual(0)
  expect(y).toBeGreaterThanOrEqual(0)
  expect(w).toBeGreaterThan(100)
  expect(h).toBeGreaterThan(50)
  /* 区域列表出现手动新增行，可点恢复 */
  await expect(page.locator('.gcc-deal-region-row', { hasText: '手动新增' })).toHaveCount(1)
  /* F10：恢复/重打按钮触摸目标 ≥ 36px */
  const undoBox = await page.locator('.gcc-deal-r-undo').first().boundingBox()
  expect(undoBox).toBeTruthy()
  expect(undoBox!.height).toBeGreaterThanOrEqual(36)
})

test('lost generate response recovers by durable request id after refresh without another POST', async ({ page }) => {
  const calls = await mockDealApp(page)
  await page.addInitScript(({ anchorKey, pendingKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 4, maxStep: 4, anonymous: true, updatedAt: Date.now(),
    }))
    localStorage.setItem(pendingKey, JSON.stringify({
      requestId: 'deal-stable-pending', payloadHash: 'a'.repeat(64), createdAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1), pendingKey: pendingRequestKey(1, 1) })
  await page.route('**/api/marketing/content-packages/by-request/*', async (route) => json(route, { ok: true, job: dealJob }))
  await openDealStudio(page)
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  await expect(page.locator('[data-channel-section="deal_poster"]')).toBeVisible()
  expect(calls.generates).toHaveLength(0)
})

test('unconfirmed redaction keeps generation unblocked and warns about chat component skip', async ({ page }) => {
  await mockDealApp(page, {
    draft: makeDraft({ materials: [materialFixture()], sheet: sheetFixture(), status: 'analyzed' }),
  })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 4, maxStep: 4, anonymous: true, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  await openDealStudio(page)
  await expect(page.getByText(/聊天晒单需要至少一张已确认打码的素材/)).toBeVisible()
  const generateButton = page.getByRole('button', { name: '确认并生成整套内容' })
  await expect(generateButton).toBeEnabled()
})

test('deal studio is reachable from home and returns with the same account scope', async ({ page }) => {
  await mockDealApp(page)
  await page.goto('/marketing-materials')
  await page.getByRole('button', { name: /晒成交/ }).click()
  await expect(page.getByTestId('deal-studio')).toBeVisible()
  await expect(page.getByRole('button', { name: '整理成交信息' })).toBeVisible()
  await expect(page.getByText('把一次真实成交，变成一整套获客内容')).toBeVisible()
  await page.getByRole('button', { name: /返回内容中心/ }).first().click()
  await expect(page.getByTestId('geo-content-center')).toBeVisible()
})

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

for (const width of [390, 1440]) {
  test(`deal results viewport ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width <= 390 ? 844 : 900 })
    await mockDealApp(page)
    await page.addInitScript(({ anchorKey, pendingKey }) => {
      localStorage.setItem(anchorKey, JSON.stringify({
        requestId: 'deal-test-request-id', draftId: 42, step: 4, maxStep: 4, anonymous: true, updatedAt: Date.now(),
      }))
      localStorage.setItem(pendingKey, JSON.stringify({
        requestId: 'deal-stable-pending', payloadHash: 'a'.repeat(64), createdAt: Date.now(),
      }))
    }, { anchorKey: draftAnchorKey(1, 1), pendingKey: pendingRequestKey(1, 1) })
    await page.route('**/api/marketing/content-packages/by-request/*', async (route) => json(route, { ok: true, job: dealJob }))
    await openDealStudio(page)
    await expect(page.locator('[data-channel-section="deal_poster"]')).toBeVisible()
    await resetAllScrollContainers(page)
    const screenshot = path.resolve(testInfo.config.rootDir, '..', 'artifacts', 'deal-studio', `deal-results-${width}.png`)
    await page.screenshot({ path: screenshot, fullPage: true })
    await expectNoHorizontalOverflow(page, ['.gcc-deal-generate-grid', '.gcc-channel-sections'])
  })

  test(`deal redaction viewport ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width <= 390 ? 844 : 900 })
    const redactedMaterial = materialFixture()
    redactedMaterial.redaction = {
      ...emptyRedaction(),
      auto_regions: [
        { id: 'r1', kind: 'phone', box: [100, 200, 220, 40], source: 'auto', active: true },
        { id: 'r2', kind: 'name', box: [100, 300, 180, 36], source: 'auto', active: true },
      ],
      status: 'previewed',
      has_preview: true,
      detection: 'ocr',
    }
    await mockDealApp(page, {
      draft: makeDraft({ materials: [redactedMaterial], sheet: sheetFixture(), status: 'redacted' }),
    })
    await page.addInitScript(({ anchorKey }) => {
      localStorage.setItem(anchorKey, JSON.stringify({
        requestId: 'deal-test-request-id', draftId: 42, step: 3, maxStep: 3, anonymous: true, updatedAt: Date.now(),
      }))
    }, { anchorKey: draftAnchorKey(1, 1) })
    await openDealStudio(page)
    await expect(page.locator('.gcc-deal-region-row')).toHaveCount(2)
    await expect(page.locator('.gcc-deal-stage-img img')).toBeVisible()
    await resetAllScrollContainers(page)
    const screenshot = path.resolve(testInfo.config.rootDir, '..', 'artifacts', 'deal-studio', `deal-redact-${width}.png`)
    await page.screenshot({ path: screenshot, fullPage: true })
    await expectNoHorizontalOverflow(page, ['.gcc-deal-stage-img', '.gcc-deal-redact-grid'])
  })

  test(`deal intake viewport ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width <= 390 ? 844 : 900 })
    await mockDealApp(page, {
      draft: makeDraft({ materials: [materialFixture()], form: { voice_transcript: '上个月签了个做全屋定制的客户，三万的单子。' } }),
    })
    await page.addInitScript(({ anchorKey }) => {
      localStorage.setItem(anchorKey, JSON.stringify({
        requestId: 'deal-test-request-id', draftId: 42, step: 1, maxStep: 1, anonymous: true, updatedAt: Date.now(),
      }))
    }, { anchorKey: draftAnchorKey(1, 1) })
    await openDealStudio(page)
    await expect(page.getByAltText('成交素材第 1 张')).toBeVisible()
    await page.getByRole('button', { name: /补充成交细节/ }).click()
    await expect(page.getByLabel('成交金额')).toBeVisible()
    await resetAllScrollContainers(page)
    const screenshot = path.resolve(testInfo.config.rootDir, '..', 'artifacts', 'deal-studio', `deal-intake-${width}.png`)
    await page.screenshot({ path: screenshot, fullPage: true })
    await expectNoHorizontalOverflow(page, ['.gcc-deal-intake-grid'])
  })

  test(`deal sheet viewport ${width}`, async ({ page }, testInfo) => {
    await page.setViewportSize({ width, height: width <= 390 ? 844 : 900 })
    await mockDealApp(page, {
      draft: makeDraft({ materials: [materialFixture()], sheet: sheetFixture(), status: 'analyzed' }),
    })
    await page.addInitScript(({ anchorKey }) => {
      localStorage.setItem(anchorKey, JSON.stringify({
        requestId: 'deal-test-request-id', draftId: 42, step: 2, maxStep: 2, anonymous: true, updatedAt: Date.now(),
      }))
    }, { anchorKey: draftAnchorKey(1, 1) })
    await openDealStudio(page)
    await expect(page.getByText('成交信息确认单')).toBeVisible()
    await resetAllScrollContainers(page)
    const screenshot = path.resolve(testInfo.config.rootDir, '..', 'artifacts', 'deal-studio', `deal-sheet-${width}.png`)
    await page.screenshot({ path: screenshot, fullPage: true })
    await expectNoHorizontalOverflow(page, ['.gcc-deal-sheet-grid'])
  })
}

test('sheet edit after a lost generate response mints a new request id, no edit reuses it (F2)', async ({ page }) => {
  await mockDealApp(page, {
    draft: makeDraft({ sheet: sheetFixture(), status: 'analyzed' }),
  })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 4, maxStep: 4, anonymous: true, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  // 生成请求全部丢响应（锚点保留），捕获每次 POST 的 request_id
  const requestIds: string[] = []
  await page.route('**/api/marketing/content-packages', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    requestIds.push(String(route.request().postDataJSON()?.request_id || ''))
    await route.abort('connectionrefused')
  })
  await openDealStudio(page)
  const generateButton = page.getByRole('button', { name: '确认并生成整套内容' })
  await generateButton.click()
  await expect(generateButton).toBeEnabled()
  // 生成前会先落确认单 contact_and_qr 联动 PATCH,POST 在其后发出;用 poll 等
  // 真实网络动作,不做时序脆弱的同步断言(P2-4 真断言)。
  await expect.poll(() => requestIds.length).toBe(1)

  /* 回第二步改成交金额（PATCH 落库），再生成：幂等键必须换新 */
  await page.locator('.gcc-deal-step', { hasText: '确认成交信息' }).click()
  await expect(page.getByText('成交信息确认单')).toBeVisible()
  await page.getByLabel('成交金额').fill('4 万元')
  await page.getByRole('button', { name: '下一步：隐私打码' }).click()
  await page.locator('.gcc-deal-step', { hasText: '生成整套内容' }).click()
  await generateButton.click()
  await expect(generateButton).toBeEnabled()
  await expect.poll(() => requestIds.length).toBe(2)
  expect(requestIds[1]).not.toBe(requestIds[0])

  /* 不再改动：继续丢响应时仍复用同一幂等键，不会重复计费 */
  await generateButton.click()
  await expect.poll(() => requestIds.length).toBe(3)
  expect(requestIds[2]).toBe(requestIds[1])
})

test('deal history shows showcase packages even without deal channels and hides normal packages (F14)', async ({ page }) => {
  const momentsOnlyShowcase = {
    ...dealJob,
    job_id: 971,
    channels: ['moments'],
    strategy: { core_angle: '只发朋友圈的晒单包' },
  }
  const normalPromote = {
    ...dealJob,
    job_id: 972,
    quick_task: 'promote_geo',
    channels: ['professional_poster'],
    strategy: { core_angle: '普通推广包' },
  }
  await mockDealApp(page, { historyJobs: [momentsOnlyShowcase, normalPromote, dealJob] })
  await openDealStudio(page)
  await page.getByRole('button', { name: '历史版本' }).click()
  // 只勾朋友圈的晒成交包（无 deal_* 渠道）按 quick_task=showcase_deal 口径仍显示
  await expect(page.getByText('只发朋友圈的晒单包')).toBeVisible()
  await expect(page.getByText('用一笔真实成交的完整过程代替夸张宣传')).toBeVisible()
  // 普通推广包不混进晒成交历史
  await expect(page.getByText('普通推广包')).toHaveCount(0)
})

test('same-user safe token refresh keeps the user inside DealStudio with draft state restored (F9)', async ({ page }) => {
  const calls = await mockDealApp(page)
  await openDealStudio(page)
  /* 先上传一张素材：服务端建草稿 + 锚点落 draftId */
  await page.getByTestId('deal-image-input').setInputFiles({ name: 'chat.png', mimeType: 'image/png', buffer: Buffer.from('png-bytes') })
  await expect(page.getByAltText('成交素材第 1 张')).toBeVisible()
  expect(calls.creates).toHaveLength(1)

  await rotateAuthoritativeSession(page, 'geo-safe-refresh-successor')

  /* owner 代际（u+permVer）未变：安全刷新按 candidate-token 设计卸载重挂整页，
     但用户不被踢出晒成交——视图随草稿偏好恢复，服务端草稿状态随锚点恢复。 */
  await expect(page.getByTestId('deal-studio')).toBeVisible()
  await expect(page.getByAltText('成交素材第 1 张')).toBeVisible()
  await expect(page.getByRole('button', { name: '整理成交信息' })).toBeVisible()
})

test('AI-extraction sheet warnings render under fields without blocking edits', async ({ page }) => {
  // Owner 2026-07-22 防编造改标注:AI 提取超出材料的字段保留 + 字段下方琥珀提醒。
  const sheet = sheetFixture() as Record<string, any>
  sheet._meta = {
    extraction: 'ai',
    warnings: [
      { code: 'ai_extraction_beyond_materials', field: 'deal_amount', message: 'AI 提取的「成交金额」超出了你提供的材料，请核对' },
    ],
  }
  await mockDealApp(page, { draft: makeDraft({ sheet, status: 'analyzed' }) })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 2, maxStep: 2, anonymous: true, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  await openDealStudio(page)

  await expect(page.getByText('成交信息确认单')).toBeVisible()
  // 字段值保留(不剔除)+ 提醒显示在字段下方
  await expect(page.getByLabel('成交金额')).toHaveValue('约 3 万元')
  const warning = page.locator('[data-sheet-warning="deal_amount"]')
  await expect(warning).toBeVisible()
  await expect(warning).toContainText('超出了你提供的材料')
  // 不阻断:字段可编辑,下一步照常可走
  await page.getByLabel('成交金额').fill('3.2 万元')
  await expect(page.getByRole('button', { name: '下一步：隐私打码' })).toBeEnabled()
})

test('redaction manual-review warning shows as amber note after confirm, never a 422', async ({ page }) => {
  // Owner 2026-07-22:redaction_manual_review_required 422 → 放行 + warning。
  const material = materialFixture()
  material.redaction = {
    ...emptyRedaction(),
    status: 'previewed',
    has_preview: true,
    detection: 'risk_flagged_manual_required',
    warnings: [{ code: 'redaction_manual_review_suggested', message: '素材疑似有隐私内容但未能自动定位，建议人工复核一遍打码' }],
  }
  const calls = await mockDealApp(page, {
    draft: makeDraft({ materials: [material], sheet: sheetFixture(), status: 'redacted' }),
  })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 3, maxStep: 3, anonymous: true, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  await openDealStudio(page)

  await expect(page.getByText('隐私打码', { exact: true }).first()).toBeVisible()
  // 提醒条可见(琥珀色);confirm 照常可走,不会被 422 拦下
  const note = page.locator('[data-redaction-warnings="mat001"]')
  await expect(note).toBeVisible()
  await expect(note).toContainText('建议人工复核')
  await page.getByRole('button', { name: '预览确认：这张可以用了' }).click()
  await expect(page.getByText('这张素材的打码已确认')).toBeVisible()
  expect(calls.redacts.at(-1)?.action).toBe('confirm')
  await expect(note).toBeVisible()
})

test('deal job warnings aggregate into the results status bar', async ({ page }) => {
  const warnedJob = structuredClone(dealJob) as any
  warnedJob.warnings = [
    { code: 'contact_forbidden_when_none', message: '联系方式已关闭，但文案里出现了联系方式类内容，请确认是否需要', channel: 'moments', component_id: 'moments:copy' },
  ]
  await mockDealApp(page, { job: warnedJob })
  await page.addInitScript(({ anchorKey, pendingKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 4, maxStep: 4, anonymous: true, updatedAt: Date.now(),
    }))
    localStorage.setItem(pendingKey, JSON.stringify({
      requestId: 'deal-stable-pending', payloadHash: 'a'.repeat(64), createdAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1), pendingKey: pendingRequestKey(1, 1) })
  await page.route('**/api/marketing/content-packages/by-request/*', async (route) => json(route, { ok: true, job: warnedJob }))
  await openDealStudio(page)

  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  const jobBar = page.locator('.gcc-warnings[data-warnings-scope="job"]')
  await expect(jobBar).toBeVisible()
  await jobBar.getByRole('button').click()
  await expect(jobBar.getByText('联系方式已关闭，但文案里出现了联系方式类内容，请确认是否需要')).toBeVisible()
  // 渠道分段旁同款提醒;不阻断任何操作
  const moments = page.locator('[data-channel-section="moments"]')
  await expect(moments.locator('.gcc-warnings[data-warnings-scope="channel"]')).toBeVisible()
  await expect(moments.getByRole('button', { name: '复制文案' })).toBeEnabled()
})

test('public showcase supports QR contact upload, sheet sync and qr payload (P2-2)', async ({ page }) => {
  // 二维码上传闭环真实断言:三态联系方式 → 上传 → 服务端扫码校验 →
  // 确认单 contact_and_qr 联动 PATCH → 生成 payload 带 qr_reference。
  const calls = await mockDealApp(page, {
    draft: makeDraft({ sheet: sheetFixture(), status: 'confirmed' }),
  })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 4, maxStep: 4,
      anonymous: false, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  await openDealStudio(page)

  /* 三态联系方式:不展示 / 展示文字 / 展示二维码 */
  await expect(page.getByRole('button', { name: '不展示联系方式' })).toBeVisible()
  await expect(page.getByRole('button', { name: '展示文字' })).toBeVisible()
  const qrModeButton = page.getByRole('button', { name: '展示二维码' })
  await qrModeButton.click()
  const qrInput = page.getByLabel('上传二维码图片')
  await qrInput.setInputFiles({ name: 'qr.png', mimeType: 'image/png', buffer: Buffer.from('qr-bytes') })
  await expect(page.getByText(/已验证：https:\/\/example\.test\/contact/)).toBeVisible()
  expect(calls.qrUploads).toBe(1)

  /* 生成:确认单 contact_and_qr 联动 PATCH + payload 带 qr_reference */
  await page.getByRole('button', { name: '确认并生成整套内容' }).click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(calls.generates).toHaveLength(1)
  const payload = calls.generates[0]
  expect(payload.contact.mode).toBe('qr')
  expect(payload.contact.qr_reference?.reference_id).toBe('qr-deal-test-001.png')
  expect(payload.contact.qr_reference?.reference_token).toBe('qr-deal-token-test')
  const contactPatch = calls.patches.find((patch) => String(patch?.sheet?.contact_and_qr?.value || '').includes('二维码联系方式'))
  expect(contactPatch, '生成前必须把二维码联系方式写回确认单 contact_and_qr').toBeTruthy()
})

test('anonymous showcase forces contact off even after QR upload', async ({ page }) => {
  // 匿名晒单:联系方式强制不出现,QR 选择不得泄漏进 payload(与后端 anonymize 同口径)。
  const calls = await mockDealApp(page, {
    draft: makeDraft({ sheet: sheetFixture(), status: 'confirmed' }),
  })
  await page.addInitScript(({ anchorKey }) => {
    localStorage.setItem(anchorKey, JSON.stringify({
      requestId: 'deal-test-request-id', draftId: 42, step: 4, maxStep: 4,
      anonymous: false, updatedAt: Date.now(),
    }))
  }, { anchorKey: draftAnchorKey(1, 1) })
  await openDealStudio(page)
  await page.getByRole('button', { name: '展示二维码' }).click()
  await page.getByLabel('上传二维码图片').setInputFiles({ name: 'qr.png', mimeType: 'image/png', buffer: Buffer.from('qr-bytes') })
  await expect(page.getByText(/已验证/)).toBeVisible()
  // 切回匿名:联系方式控件整体隐藏,payload 必须 mode=none
  await page.getByText('公开晒单').click()
  await expect(page.getByText('匿名晒单')).toBeVisible()
  await page.getByRole('button', { name: '确认并生成整套内容' }).click()
  await expect(page.getByText('已完成', { exact: true }).first()).toBeVisible()
  expect(calls.generates).toHaveLength(1)
  expect(calls.generates[0].contact.mode).toBe('none')
  expect(calls.generates[0].contact.qr_reference).toBeNull()
})
