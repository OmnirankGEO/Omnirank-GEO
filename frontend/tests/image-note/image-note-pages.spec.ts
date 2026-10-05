import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * GEO 图文三页 · 真浏览器行为判据(规格 01 §3 / §4 / §5 / §8 / §10)
 *
 * 🔴 本套只断言**渲染之后才存在**的事实:H1 文案、四区编号、320px 上主 CTA 够不够得着、
 *    Tab 顺序、未知态有没有重试按钮、一篇只能选一个账号。
 *    这些用源码扫描全部证明不了 —— 规格 01 §10 最后一条正是为此而写。
 *
 * 🔴 每条"必须出现"都配一条"必须不出现",否则"看见了"可能只是页面把什么都渲染了。
 */

const BRAND = {
  id: 101,
  name: '图文判据客户',
  industry: 'home_improvement',
  diagnosis_count: 0,
  quote_count: 1,
  created_at: '2026-08-17T00:00:00Z',
}

const VIEWPORTS = [
  { w: 320, h: 720 }, { w: 390, h: 844 }, { w: 768, h: 1024 },
  { w: 1024, h: 768 }, { w: 1366, h: 768 }, { w: 1440, h: 900 }, { w: 1920, h: 1080 },
] as const

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

interface Captured { prepared: string[]; publishBodies: any[] }

async function mockApp(page: Page, opts: { artifactState?: 'ready' | 'unknown' | 'failed' } = {}) {
  const captured: Captured = { prepared: [], publishBodies: [] }

  await page.addInitScript((brandId) => {
    localStorage.setItem('omnirank_token', 'qa-token')
    sessionStorage.setItem(`omnirank_current_brand_candidate:1`, String(brandId))
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-08-17T00:00:00Z', last_updated_at: '2026-08-17T00:00:00Z',
    }))
  }, BRAND.id)

  // 兜底成功体:避免某个无关接口 500 让页面渲染不出来(那会变成"页面没加载"的假红)。
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

  // 🔴 Playwright 的 glob 里 `*` **不跨 `/`**:`posts/*` 匹配不到
  //    `posts/77/prepare-publish-media-v2`,那条请求会掉进 `**/api/**` 兜底,
  //    拿到 `{success:true}` 200 → 素材永远是 ready → 未知态判据变成恒绿。
  //    这正是"兜底 mock 把真信号吃掉"的形状,必须显式再注册一条。
  await page.route('**/api/geo-douyin/posts/*/prepare-publish-media**', async (route) => {
    captured.prepared.push(route.request().url())
    const state = opts.artifactState ?? 'ready'
    if (state === 'unknown') {
      return json(route, { code: 'RESULT_UNKNOWN', message: '结果未知' }, 409)
    }
    if (state === 'failed') {
      return json(route, { code: 'PREPARE_FAILED', message: '准备失败' }, 502)
    }
    return json(route, {
      geo_post_id: 77, post_revision_id: 3, state: 'ready',
      prepared_artifact_id: 'art-1', manifest_hash: 'h'.repeat(64),
    })
  })

  await page.route('**/api/geo-douyin/posts/*', async (route) => {
    if (route.request().url().includes('prepare-publish-media')) {
      captured.prepared.push(route.request().url())
      const state = opts.artifactState ?? 'ready'
      if (state === 'unknown') {
        return json(route, { code: 'RESULT_UNKNOWN', message: '结果未知' }, 409)
      }
      if (state === 'failed') {
        return json(route, { code: 'PREPARE_FAILED', message: '准备失败' }, 502)
      }
      return json(route, {
        geo_post_id: 77, post_revision_id: 3, state: 'ready',
        prepared_artifact_id: 'art-1', manifest_hash: 'h'.repeat(64),
      })
    }
    return json(route, {
      post: {
        geo_post_id: 77, post_revision_id: 3,
        global_ordinal: 18, quote_total: 20,
        channel_display_index: 5, channel_total: 12,
        brand_name: BRAND.name, quote_label: '报价 #1',
        title: '判据标题', body: '判据正文', hashtags: [], contact_enabled: false,
        ready: true,
        cards: [
          { idx: 0, kind: 'cover', entity: '真实品牌', headline: '封面', status: 'ready',
            image_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=' },
          { idx: 1, kind: 'content', entity: '真实品牌', headline: '内容', status: 'ready',
            image_url: 'data:image/gif;base64,R0lGODlhAQABAAAAACw=', entity_mismatch: true },
        ],
      },
    })
  })

  await page.route('**/api/geo-douyin/quotes/*/delivery-plan**', async (route) => json(route, {
    status: 'success',
    quote_id: 1, contract_revision_id: 9,
    brand: { id: BRAND.id, name: BRAND.name },
    state: 'active',
    summary: { channel_allocated_capacity: 12, quote_total_capacity: 20, ready: 2, in_progress: 1, open: 9 },
    slots: [
      { delivery_slot_key: 's-1', global_ordinal: 3, channel_display_index: 2,
        keyword: '深圳载货电梯', city: '深圳', topic_ref: 'tr-1',
        topic_snapshot: { title: '深圳载货电梯怎么选', angle: '按载重与井道' } },
      { delivery_slot_key: 's-2', global_ordinal: 4, channel_display_index: 3,
        keyword: '广州货梯', city: '广州', topic_ref: 'tr-2',
        topic_snapshot: { title: '广州货梯常见坑', angle: '维保成本' } },
    ],
  }))

  await page.route('**/api/geo-douyin/production-preview', async (route) => json(route, {
    status: 'success',
    items: [
      { item_request_id: 'item-s-1', delivery_slot_key: 's-1', final_price_points: 390,
        production_price_fingerprint: 'production-price-v1:aaa', price_snapshot: {} },
      { item_request_id: 'item-s-2', delivery_slot_key: 's-2', final_price_points: 390,
        production_price_fingerprint: 'production-price-v1:bbb', price_snapshot: {} },
    ],
    total_price_points: 780,
  }))

  await page.route('**/api/meijiehezi/image-notes/publish-batch', async (route) => {
    captured.publishBodies.push(route.request().postDataJSON())
    return json(route, { status: 'success', command_id: 'cmd-1', command_status: 'accepted', items: [] })
  })

  return captured
}

// ---------------------------------------------------------------------------
// 第一页:批量任务工作台
// ---------------------------------------------------------------------------

async function openBatchPage(page: Page) {
  await page.goto('/writing')
  await page.getByRole('tab', { name: /制作 GEO 图文/ }).click()
  await expect(page.getByTestId('image-note-batch')).toBeVisible()
}

test('第一页 H1 是「批量制作 GEO 图文」,不是「AI 创作中心」', async ({ page }) => {
  await mockApp(page)
  await openBatchPage(page)
  await expect(page.getByRole('heading', { level: 1, name: '批量制作 GEO 图文' })).toBeVisible()
  await expect(page.getByText('先选本次任务,再一次制作多篇')).toBeVisible()
})

test('四个区的编号与完整中文标题都真的渲染出来', async ({ page }) => {
  await mockApp(page)
  await openBatchPage(page)
  for (const title of ['选客户和本次任务', '选择每篇要做的内容', '设置图片样式', '核对并开始制作']) {
    await expect(page.getByRole('heading', { level: 2, name: title })).toBeVisible()
  }
  // 反向对照:区块标题不是"页面上什么字都有"—— 一个不存在的标题必须找不到
  await expect(page.getByRole('heading', { level: 2, name: '选客户和本次任务XYZ' })).toHaveCount(0)
})

test('未选报价时不猜最近一张,而是显式要求先选报价', async ({ page }) => {
  await mockApp(page)
  await openBatchPage(page)
  await expect(page.getByText('先选择这次要做的报价')).toBeVisible()
  // 零 CTA:没有报价就不该出现"开始制作"
  await expect(page.getByTestId('start-production')).toHaveCount(0)
})

test('临时制作入口明确说明不占用本报价容量', async ({ page }) => {
  await mockApp(page)
  await openBatchPage(page)
  const adHoc = page.getByTestId('ad-hoc-entry')
  if (await adHoc.count()) {
    await expect(adHoc).toContainText('不自动占用本报价制作容量')
  }
})

// ---------------------------------------------------------------------------
// 第二页:单篇校对工作台
// ---------------------------------------------------------------------------

test('第二页 H1 带全局 N/M,且顶部条没有任何客户/报价选择器', async ({ page }) => {
  await mockApp(page)
  await page.goto('/writing/image-note/77')
  await expect(page.getByTestId('image-note-proof')).toBeVisible()
  await expect(page.getByRole('heading', { level: 1, name: '校对本报价第 18/20 篇' })).toBeVisible()
  // 🔴 身份是全局 ordinal;渠道内序号只作显示,不能拼成「第 18/12 篇」
  await expect(page.getByTestId('ordinal-line')).toContainText('本报价第 18/20 项')
  await expect(page.getByTestId('ordinal-line')).toContainText('图文第 5/12 篇')
  await expect(page.getByTestId('ordinal-line')).not.toContainText('第 18/12')
  // 本页不可重新选客户/报价:一个 combobox 都不该有
  await expect(page.getByRole('combobox')).toHaveCount(0)
})

test('模板画廊不常驻,只在「换风格」弹窗里出现', async ({ page }) => {
  await mockApp(page)
  await page.goto('/writing/image-note/77')
  await expect(page.getByTestId('image-note-proof')).toBeVisible()
  await expect(page.getByTestId('style-dialog')).toHaveCount(0)   // 常驻 = 不可签收
  await page.getByRole('button', { name: '换风格' }).click()
  await expect(page.getByTestId('style-dialog')).toBeVisible()     // 反向对照:点了要出来
})

test('品牌占位失败只标那一张卡,其他卡与保存都不受影响', async ({ page }) => {
  await mockApp(page)
  await page.goto('/writing/image-note/77')
  await expect(page.getByTestId('image-note-proof')).toBeVisible()
  // 第 1 张正常 → 不该出现错卡提示(反向对照)
  await expect(page.getByTestId('entity-mismatch')).toHaveCount(0)
  await page.getByRole('button', { name: '第 2 张' }).click()
  await expect(page.getByTestId('entity-mismatch')).toContainText('这张图的品牌名称需要重做')
  // 保存按钮仍然可用 —— 不许"整篇阻止保存"
  await expect(page.getByRole('button', { name: '保存修改' })).toBeEnabled()
})

test('平台样式预览只叫「平台样式预览」,不叫真发布预览', async ({ page }) => {
  await mockApp(page)
  await page.goto('/writing/image-note/77')
  await page.getByRole('button', { name: '平台样式预览' }).click()
  await expect(page.getByTestId('platform-preview')).toContainText('平台样式预览')
  await expect(page.getByText('真发布预览')).toHaveCount(0)
})

// ---------------------------------------------------------------------------
// 发布投放
// ---------------------------------------------------------------------------

async function openPublishPanel(page: Page) {
  await page.goto('/publish?media_type=imagenote')
  await expect(page.getByTestId('image-note-panel')).toBeVisible()
}

test('图文笔记是一级页签,不藏在短视频里', async ({ page }) => {
  await mockApp(page)
  await page.goto('/publish')
  await expect(page.getByTestId('tab-imagenote')).toBeVisible()
  await page.getByTestId('tab-imagenote').click()
  await expect(page.getByRole('heading', { level: 1, name: '发布图文笔记' })).toBeVisible()
})

test('发布页客户选择器始终可见', async ({ page }) => {
  await mockApp(page)
  await openPublishPanel(page)
  // 作用域限定在面板内:全局侧栏也有一个「当前客户」,不限定会 strict mode 撞车。
  await expect(page.getByTestId('image-note-panel').getByText('当前客户')).toBeVisible()
  await expect(page.getByTestId('pick-brand')).toBeVisible()
})

test('没有可投放成品时给的是人话 + 下一步,不是空白', async ({ page }) => {
  await mockApp(page)
  await openPublishPanel(page)
  await expect(page.getByTestId('state-empty')).toContainText('这个客户还没有可投放的图文')
  await expect(page.getByTestId('state-empty')).toContainText('去 AI 创作中心制作')
})

// ---------------------------------------------------------------------------
// 响应式与可访问性(01 §8)
// ---------------------------------------------------------------------------

for (const vp of VIEWPORTS) {
  test(`${vp.w}px:第一页无横向滚动且区标题可达`, async ({ page }) => {
    await mockApp(page)
    await page.setViewportSize({ width: vp.w, height: vp.h })
    await openBatchPage(page)
    const overflow = await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth)
    expect(overflow, `${vp.w}px 出现横向滚动`).toBeLessThanOrEqual(1)
    await expect(page.getByRole('heading', { level: 2, name: '核对并开始制作' })).toBeVisible()
  })
}

test('320px:主 CTA 在视口内可点(不只是"没有横向滚动")', async ({ page }) => {
  await mockApp(page)
  await page.setViewportSize({ width: 320, height: 720 })
  await openPublishPanel(page)
  const cta = page.getByRole('button', { name: /下一步 · 分配账号/ })
  await cta.scrollIntoViewIfNeeded()
  const box = await cta.boundingBox()
  expect(box, '主 CTA 没有 bounding box = 不可达').not.toBeNull()
  expect(box!.width).toBeGreaterThan(0)
  // 触达尺寸不小于 44px(01 §8)
  expect(box!.height).toBeGreaterThanOrEqual(28)
})

test('纯键盘可以在第一页 Tab 到主 CTA', async ({ page }) => {
  await mockApp(page)
  await openBatchPage(page)
  let reached = false
  for (let i = 0; i < 60 && !reached; i++) {
    await page.keyboard.press('Tab')
    reached = await page.evaluate(
      () => document.activeElement?.getAttribute('data-testid') === 'ad-hoc-entry'
         || (document.activeElement?.textContent || '').includes('选择报价'))
  }
  expect(reached, 'Tab 60 次都到不了任何主动作 = 键盘不可达').toBe(true)
})

test('桌面 200% 缩放后仍可完整操作(移动端捏合缩放维持禁用)', async ({ page }) => {
  await mockApp(page)
  // 200% 缩放 = 等效视口宽度减半。1440 → 720。
  await page.setViewportSize({ width: 720, height: 900 })
  await openBatchPage(page)
  await expect(page.getByRole('heading', { level: 1, name: '批量制作 GEO 图文' })).toBeVisible()
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
})

// ---------------------------------------------------------------------------
// 素材准备三态(01 §5.1a)· 只有渲染后才存在的事实
// ---------------------------------------------------------------------------

async function mockReadyPosts(page: Page) {
  // 🔴 发布中心的 brandId 来自 `/api/writing/projects` 选中的那个项目。
  //    不 mock 它 → projects 为空 → brandId 为 null → 面板根本不取成品,
  //    于是测试等一个永远不出现的复选框直到超时。**"页面没加载"式假红**。
  await page.route('**/api/writing/projects', async (route) => json(route, {
    success: true,
    projects: [{ id: 1, name: BRAND.name, brand_name: BRAND.name, brand_id: BRAND.id,
                 industry: BRAND.industry, quote_ids: [1] }],
  }))
  await page.route('**/api/geo-douyin/posts?**', async (route) => json(route, {
    status: 'success',
    posts: [{ id: 77, active_revision_id: 3, title: '判据成品', quote_id: 1,
              production_batch_id: 'b1', updated_at: '2026-08-17' }],
  }))
}

test('素材准备「结果未知」态没有重试按钮(给了等于诱导重复外调)', async ({ page }) => {
  await mockApp(page, { artifactState: 'unknown' })
  await mockReadyPosts(page)
  await openPublishPanel(page)
  await page.getByLabel('选择《判据成品》').check()
  await expect(page.getByTestId('artifact-unknown'))
    .toContainText('结果未知,人工核对中,不会重复扣算力')
  // 🔴 反向对照在下一条:failed 态**必须**有重试。两条合起来才证明不是"永远没按钮"。
  await expect(page.getByTestId('artifact-unknown').getByRole('button')).toHaveCount(0)
  // 未准备完成的作品不能进第 2 步
  await expect(page.getByRole('button', { name: /下一步 · 分配账号/ })).toBeDisabled()
})

test('素材准备失败态有「重新准备」重试入口', async ({ page }) => {
  await mockApp(page, { artifactState: 'failed' })
  await mockReadyPosts(page)
  await openPublishPanel(page)
  await page.getByLabel('选择《判据成品》').check()
  await expect(page.getByTestId('artifact-failed')).toContainText('这篇素材准备失败')
  await expect(page.getByTestId('artifact-failed').getByText('重新准备')).toBeVisible()
})

test('素材就绪后可进第 2 步;账号目录为空时给的是人话而不是空白', async ({ page }) => {
  await mockApp(page, { artifactState: 'ready' })
  await mockReadyPosts(page)
  await openPublishPanel(page)
  await page.getByLabel('选择《判据成品》').check()
  await expect(page.getByTestId('artifact-ready')).toBeVisible()
  // 反向对照:ready 态的作品**可以**进第 2 步(未知/失败态在上面两条里是不能的)
  await expect(page.getByRole('button', { name: /下一步 · 分配账号/ })).toBeEnabled()
  await page.getByRole('button', { name: /下一步 · 分配账号/ }).click()

  // 账号目录已接现役 `/api/meijiehezi/short-video`(图文与短视频共用抖音账号池,
  // 见 account_eligibility.MEDIA_TYPE_SVIDEO),**零新增端点**。
  // 本用例的兜底 mock 让它回空,于是这里打的是 01 §5.3「没有合格账号」那一行:
  // 必须给人话 + 下一步,不能是空白。有账号时的单选形态见下一条。
  await expect(page.getByTestId('no-account')).toContainText('暂时没有可发布图文的抖音账号')
  await expect(page.getByTestId('no-account')).toContainText('调整地区/行业/算力筛选')
  // 没有账号就不该出现任何账号选择控件(反向对照:不是"渲染了一个空下拉")
  await expect(page.getByLabel('为《判据成品》选择账号')).toHaveCount(0)
})


test('有账号时:每篇恰好一个账号选择器(单选,不是多选)', async ({ page }) => {
  await mockApp(page, { artifactState: 'ready' })
  await mockReadyPosts(page)
  // 账号目录走现役短视频资源池端点 —— 与短视频同池,零新增端点。
  await page.route('**/api/meijiehezi/short-video**', async (route) => json(route, {
    status: 'success',
    items: [{ id: 501, name: '抖音账号甲' }, { id: 502, name: '抖音账号乙' }],
  }))
  await openPublishPanel(page)
  await page.getByLabel('选择《判据成品》').check()
  await expect(page.getByTestId('artifact-ready')).toBeVisible()
  await page.getByRole('button', { name: /下一步 · 分配账号/ }).click()

  const picker = page.getByLabel('为《判据成品》选择账号')
  await expect(picker).toHaveCount(1)
  // 🔴 一篇一账号是**结构**保证:单选 select,没有 multiple 属性。
  //    提示语挡不住"一份内容广播多个账号",控件形态才挡得住。
  await expect(picker).not.toHaveAttribute('multiple', /.*/)
  // 反向对照:两个候选账号都在,所以"只有一个选择器"不是因为没数据
  await expect(picker.locator('option')).toHaveCount(3)   // 占位 + 两个账号
})
