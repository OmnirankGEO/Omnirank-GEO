import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * 「同行榜单」创建入口 · **从页面操作发出的请求体**判别测试
 *
 * 🔴 这一套的存在理由(返工单 §1):
 *    以后不许再用「直调后端函数」冒充产品端到端。判据必须落在
 *    **浏览器真的发出去的那个 POST body** 上 —— 只有它能证明用户点得出来。
 *
 * 覆盖:
 *   ① 默认(不点榜单)→ content_form 为空,与接入之前逐字相同(反向对照);
 *   ② 点「同行榜单」→ content_form === 'ranking' 真的进了请求体;
 *   ③ 家数按钮 → ranking_entity_count 跟着变(不是摆设);
 *   ④ 版式下拉 → ranking_template + ranking_force 一起带上(手动 = 明示覆盖);
 *   ⑤ 切回卡组 → 榜单三个字段一起归零(不许残留)。
 */

const BRAND = {
  id: 101,
  name: '端到端探针客户',
  industry: 'home_improvement',
  diagnosis_count: 0,
  quote_count: 0,
  created_at: '2026-08-06T00:00:00Z',
}

async function json(route: Route, body: unknown, status = 200) {
  await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

/** 记录页面真正发出去的创建请求体。**这就是本套的唯一判据来源。** */
type Captured = { bodies: any[] }

async function mockApp(page: Page): Promise<Captured> {
  const captured: Captured = { bodies: [] }

  await page.addInitScript((brandId) => {
    localStorage.setItem('omnirank_token', 'qa-token')
    localStorage.setItem('omnirank-theme', 'dark')
    // 客户选择存在 sessionStorage,键按 userId 分片(ClientContext.selectionStorageKey)
    sessionStorage.setItem(`omnirank_current_brand_candidate:1`, String(brandId))
    // 新手引导弹窗会盖住整页 —— 不关掉的话本套测的就是那个弹窗。
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-08-06T00:00:00Z', last_updated_at: '2026-08-06T00:00:00Z',
    }))
  }, BRAND.id)

  // 兜底:没显式 mock 的接口一律返回一个无害的成功体,避免页面因为某个
  // 无关请求 500 而渲染不出来(那会让本套变成"页面没加载"的假红)。
  await page.route('**/api/**', async (route) => json(route, { success: true }))

  await page.route('**/api/auth/me', async (route) => json(route, {
    success: true,
    user: {
      id: 1, username: 'qa-owner', display_name: '质检服务商', is_admin: false,
      is_active: 1, must_change_password: 0, roles: [],
      // 🔴 `/writing` 挂 requiredModule="writing",而 hasModule 判的是
      //    `permissions` 里有没有 `writing:` 前缀的条目。给空数组会落到
      //    「当前账号没有此页面权限」那一屏 —— 那时测的就不是本页了。
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
      profile: null, materials: null, relatedQuoteIds: [], socialProjects: [],
    },
  }))

  // /pricing 是选项 SSOT:画幅、榜单母版、家数区间都从这里来。
  await page.route('**/api/geo-douyin/pricing', async (route) => json(route, {
    status: 'success',
    first_generation: { feature_code: 'x', cost_points: 390, feature_name: '' },
    extra_card: { feature_code: 'y', cost_points: 30, feature_name: '' },
    included_cards: 4, card_min: 1, card_max: 9, card_default: 4,
    aspect_ratios: [
      { key: '3:4', label: '竖版 3:4', hint: '常规' },
      { key: '9:16', label: '全屏 9:16', hint: '沉浸' },
    ],
    aspect_ratio_default: '3:4',
    ranking_templates: [
      { key: 'industrial_overview', label: '工业设备综合榜', when: '多候选' },
      { key: 'tech_spec_matrix', label: '技术参数对比榜', when: '可比参数' },
    ],
    ranking_entity_count_min: 3,
    ranking_entity_count_max: 6,
    ranking_entity_count_default: 4,
  }))

  await page.route('**/api/geo-douyin/styles**', async (route) => json(route, {
    status: 'success',
    styles: [{ key: 'design_text', label: '设计文字卡', hint: '', disabled: false }],
  }))
  await page.route('**/api/geo-douyin/posts?**', async (route) => json(route, {
    status: 'success', posts: [],
  }))
  await page.route('**/api/geo-douyin/clients/*/keywords', async (route) => json(route, {
    status: 'success',
    keywords: [{ keyword: '深圳载货电梯', required_articles: 3, quote_id: 1 }],
    cities: ['深圳'],
  }))
  await page.route('**/api/geo-douyin/clients/*/plan', async (route) => json(route, {
    status: 'success', items: [],
  }))
  await page.route('**/api/geo-douyin/clients/*/latest-topics', async (route) => json(route, {
    status: 'success', topics: [],
  }))

  // 🔴 创建请求:抓 body,再返回一个 accepted。
  await page.route('**/api/geo-douyin/posts', async (route) => {
    if (route.request().method() !== 'POST') return json(route, { status: 'success', posts: [] })
    captured.bodies.push(route.request().postDataJSON())
    return json(route, { status: 'accepted', post_id: 1, message: '开始制作了', cards_total: 4 })
  })

  return captured
}

async function openDouyinTab(page: Page) {
  await page.goto('/writing')
  await page.getByRole('tab', { name: /制作 GEO 图文/ }).click()
  // 选项到位 = /pricing 回来了。用形态选择器本身做就绪信号,不用 sleep。
  await expect(page.getByTestId('content-form-picker')).toBeVisible()
}

async function submit(page: Page) {
  await page.getByTestId('produce-batch').click()
}

test('默认不点榜单 → content_form 为空（现有行为逐字不变）', async ({ page }) => {
  const cap = await mockApp(page)
  await openDouyinTab(page)
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(1)
  const body = cap.bodies[0]
  expect(body.content_form).toBe('')
  expect(body.ranking_entity_count).toBe(0)
  expect(body.ranking_template).toBe('')
  expect(body.ranking_force).toBe(false)
})

test('点「同行榜单」→ 请求体里真的带上 content_form=ranking', async ({ page }) => {
  const cap = await mockApp(page)
  await openDouyinTab(page)
  await page.getByTestId('content-form-ranking').click()
  await expect(page.getByTestId('ranking-options')).toBeVisible()
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(1)
  // 🔴 这一行就是"产品端到端"与"后端函数测试"的分界线。
  expect(cap.bodies[0].content_form).toBe('ranking')
})

test('家数按钮不是摆设 → ranking_entity_count 跟着变', async ({ page }) => {
  const cap = await mockApp(page)
  await openDouyinTab(page)
  await page.getByTestId('content-form-ranking').click()
  // 张数要够摆下家数(封面+收尾各占一张),否则按钮是灰的
  await page.getByTestId('card-count').fill('8')
  // 默认取后端给的 default=4
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(1)
  expect(cap.bodies[0].ranking_entity_count).toBe(4)

  await page.getByTestId('ranking-count-6').click()
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(2)
  expect(cap.bodies[1].ranking_entity_count).toBe(6)
})

test('手动选版式 → ranking_template 与 ranking_force 一起带上', async ({ page }) => {
  const cap = await mockApp(page)
  await openDouyinTab(page)
  await page.getByTestId('content-form-ranking').click()

  // 自动(默认)时不覆盖
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(1)
  expect(cap.bodies[0].ranking_template).toBe('')
  expect(cap.bodies[0].ranking_force).toBe(false)

  await page.getByTestId('ranking-template-picker').selectOption('tech_spec_matrix')
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(2)
  expect(cap.bodies[1].ranking_template).toBe('tech_spec_matrix')
  expect(cap.bodies[1].ranking_force).toBe(true)
})

test('切回卡组 → 榜单三个字段一起归零（不许残留）', async ({ page }) => {
  const cap = await mockApp(page)
  await openDouyinTab(page)
  await page.getByTestId('content-form-ranking').click()
  await page.getByTestId('card-count').fill('8')
  await page.getByTestId('ranking-count-5').click()
  await page.getByTestId('ranking-template-picker').selectOption('industrial_overview')
  await page.getByTestId('content-form-cards').click()
  await expect(page.getByTestId('ranking-options')).toHaveCount(0)
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(1)
  const body = cap.bodies[0]
  expect(body.content_form).toBe('')
  expect(body.ranking_entity_count).toBe(0)
  expect(body.ranking_template).toBe('')
  expect(body.ranking_force).toBe(false)
})

test('企业数量与卡片数量互不混淆（工单 §六-前端-1）', async ({ page }) => {
  const cap = await mockApp(page)
  await openDouyinTab(page)
  await page.getByTestId('content-form-ranking').click()
  // 张数 6、家数 4 —— 两个旋钮各走各的
  await page.getByTestId('card-count').fill('6')
  await page.getByTestId('ranking-count-4').click()
  await submit(page)
  await expect.poll(() => cap.bodies.length).toBe(1)
  const b = cap.bodies[0]
  expect(b.card_count).toBe(6)
  expect(b.ranking_entity_count).toBe(4)
  expect(b.card_count).not.toBe(b.ranking_entity_count)
})

test('张数装不下家数时按钮变灰并说明为什么（付费前就看得见）', async ({ page }) => {
  await mockApp(page)
  await openDouyinTab(page)
  await page.getByTestId('content-form-ranking').click()
  await page.getByTestId('card-count').fill('5')       // 5 张 → 只剩 3 个企业卡位
  await expect(page.getByTestId('ranking-count-3')).toBeEnabled()
  await expect(page.getByTestId('ranking-count-6')).toBeDisabled()
  // 默认家数是 4(后端给的 default),已经超过 3 个卡位 → 命中"装不下"那一支,
  // 而且必须**直接给出怎么办**,不是只说"不行"。
  await expect(page.getByTestId('ranking-count-hint'))
    .toContainText('把张数加到 6 张才能做 4 家')
  // 反向面:降到卡位以内时换成另一句,不再报警
  await page.getByTestId('ranking-count-3').click()
  await expect(page.getByTestId('ranking-count-hint')).toContainText('最多摆 3 家')
  await expect(page.getByTestId('ranking-count-hint')).not.toContainText('把张数加到')
})

test('降级告知渲染并给两个出口', async ({ page }) => {
  const cap = await mockApp(page)
  await page.route('**/api/geo-douyin/posts?**', async (route) => json(route, {
    status: 'success',
    posts: [{
      id: 7, title: '深圳载货电梯怎么选', body_text: '正文', hashtags: [], cards: [],
      status: 'ready', city: '深圳', keyword: '深圳载货电梯', progress: null,
      generation_meta: {
        ranking: {
          requested_form: 'ranking', effective_form: 'card_group',
          fallback_reason: 'no_candidates', degraded: true,
          fallback_notice: {
            reason: 'no_candidates',
            message: '这个行业还没攒够可以点名的同行数据，这次做成了卡组版式。',
            actions: [
              { id: 'supplement_candidates', label: '去跑监测攒候选', type: 'nav', href: '/monitoring' },
              { id: 'keep_current', label: '就用这版', type: 'action' },
            ],
          },
        },
        ranking_gates: [
          { gate: 'R1', level: 'A1', message: '有 1 张卡讲的公司不在这次的名单里。', card_indices: [2] },
        ],
      },
    }],
  }))
  await openDouyinTab(page)
  await expect(page.getByTestId('ranking-fallback-7')).toBeVisible()
  await expect(page.getByTestId('ranking-fallback-msg-7'))
    .toContainText('这次做成了卡组版式')
  await expect(page.getByTestId('ranking-fallback-nav-7')).toHaveAttribute('href', '/monitoring')
  await expect(page.getByTestId('ranking-gate-7-R1')).toContainText('第 2 张')
  // 「就用这版」= 真出口,点了要真的关掉
  await page.getByTestId('ranking-fallback-keep-7').click()
  await expect(page.getByTestId('ranking-fallback-7')).toHaveCount(0)
  expect(cap.bodies.length).toBe(0)
})
