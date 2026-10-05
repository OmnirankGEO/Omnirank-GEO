/**
 * xiaobang-unified 测试底座(mock 后端 + 身份夹具)。
 *
 * 🔴 从 `xiaobang-unified.spec.ts` 里**挪出来**(Codex R3 P2)。
 *    原先 `fab-collision.spec.ts` 直接 `import … from './xiaobang-unified.spec'`
 *    —— 从**测试文件**里 import 会把那个文件的 `test()` 一并求值,
 *    等于让两个 spec 互相牵连(收集顺序、重复注册、失败归因全乱)。
 *    底座属于底座,放这里,两个 spec 各自 import。
 */
import { type Page, type Route, type TestInfo } from 'playwright/test'

type Identity = 'admin' | 'agent' | 'normal' | 'member'

const memberCapabilities = [
  'clients.read_assigned',
  'diagnosis.read_own',
  'quote.read_own',
  'writing.read_own',
  'monitoring.read_assigned',
] as const

const clients = [
  {
    id: 101, name: '甲品牌', industry: '企业服务', diagnosis_count: 1,
    created_at: '2026-08-01T08:00:00Z', brand_status: 'active',
  },
  {
    id: 202, name: '乙品牌', industry: '消费服务', diagnosis_count: 1,
    created_at: '2026-08-02T08:00:00Z', brand_status: 'active',
  },
]

export function identityFrom(testInfo: TestInfo): Identity {
  return testInfo.project.name.split('-', 1)[0] as Identity
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

function userFor(identity: Identity) {
  const id = identity === 'admin' ? 301 : identity === 'agent' ? 302 : identity === 'normal' ? 303 : 304
  return {
    id,
    user_id: id,
    username: `xiaobang-${identity}`,
    display_name: identity,
    is_admin: identity === 'admin',
    is_active: 1,
    must_change_password: 0,
    agent_level: identity === 'agent' ? 1 : 0,
    roles: [],
    permissions: [
      'diagnosis:read', 'diagnosis:write', 'quote:read', 'quote:write',
      'writing:read', 'writing:write', 'monitoring:read', 'monitoring:write',
      'users:read', 'settings:read',
    ],
    client_brand_ids: [101, 202],
  }
}

function contextFor(brandId: number, currentPage: string) {
  const brand = clients.find((item) => item.id === brandId) || clients[0]
  const pageNames: Record<string, string> = {
    '/dashboard': '今日工作台',
    '/diagnosis/new': '品牌体检',
    '/writing': 'AI 创作中心',
  }
  const pathname = currentPage.split('?')[0]
  return {
    brand_id: brand.id,
    brand_name: brand.name,
    quote_id: brand.id === 101 ? 1101 : 1202,
    current_page: pathname,
    page_name: pageNames[pathname] || '当前页面',
    data_updated_at: '2026-08-10T10:32:00Z',
    has_customer: true,
  }
}

function sse(body: string, meta: unknown): string {
  return [
    ': ready',
    '',
    'event: text',
    `data: ${JSON.stringify({ delta: body })}`,
    '',
    'event: meta',
    `data: ${JSON.stringify(meta)}`,
    '',
    'event: done',
    'data: {}',
    '',
  ].join('\n')
}

// [xbvnext 2026-08-18] 导出给 fab-collision.spec.ts 复用。
// 复用而不是复制一份 mock:两份 mock 会各自漂,而"两边跑的不是同一个后端"
// 这种假绿本仓付过费。
export async function installBackend(page: Page, identity: Identity) {
  const user = userFor(identity)
  await page.addInitScript(({ id }) => {
    localStorage.setItem('omnirank_token', 'xiaobang-unified-browser-token')
    localStorage.setItem('sidebar-collapsed', 'false')
    sessionStorage.setItem(`omnirank_current_brand_candidate:${id}`, '101')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'returning',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-08-01T00:00:00Z',
      last_updated_at: '2026-08-10T00:00:00Z',
    }))
  }, { id: user.id })

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const path = url.pathname
    if (path === '/api/auth/me') return json(route, { success: true, user })
    if (path === '/api/organization/overview') {
      if (identity !== 'member') return json(route, { detail: 'not found' }, 404)
      return json(route, {
        id: 91,
        owner_user_id: 302,
        name: '交付团队',
        status: 'active',
        version: 1,
        authority_version: 1,
        viewer_is_owner: false,
        entitlement: {
          id: 1,
          entitled_seats: 5,
          occupied_seats: 2,
          available_seats: 3,
          product_catalog_version: 'catalog-v1',
          source_sku: 'team',
        },
        identity: {
          actor_kind: 'member',
          membership_id: 904,
          authority_version: '1:1:1:1',
          capabilities: memberCapabilities,
        },
        assigned_brand_ids: [101, 202],
      })
    }
    if (path === '/api/client-context/list') return json(route, { success: true, clients })
    if (/^\/api\/client-context\/(101|202)$/.test(path)) {
      const brandId = Number(path.split('/').pop())
      const brand = clients.find((item) => item.id === brandId)!
      return json(route, {
        success: true,
        context: {
          brand: { ...brand, brand_type: 'client' },
          profile: {},
          materials: {},
          relatedQuoteIds: [String(brandId === 101 ? 1101 : 1202)],
          socialProjects: [],
        },
      })
    }
    if (path === '/api/xiaobang/context') {
      const payload = request.postDataJSON() as {
        current_page?: string
        context_refs?: { brand_id?: number }
      }
      const context = contextFor(payload.context_refs?.brand_id || 101, payload.current_page || '/dashboard')
      return json(route, {
        success: true,
        context,
        operation_plan: {
          plan_id: `plan-${context.brand_id}`,
          plan_version: 'customer-operation-plan-v1',
          context,
          metrics: { writing: { pending: context.brand_id === 101 ? 2 : 1 } },
          primary_action: { operation_id: 'writing_center', reason: '按真实待写任务继续' },
        },
      })
    }
    if (path === '/api/xiaobang/chat') {
      const payload = request.postDataJSON() as {
        message: string
        current_page?: string
        context_refs?: { brand_id?: number }
      }
      const context = contextFor(payload.context_refs?.brand_id || 101, payload.current_page || '/dashboard')
      const diagnosis = payload.message.includes('品牌体检')
      const action = diagnosis
        ? {
            action_id: 'navigate:diagnosis_new', operation_id: 'diagnosis_new',
            label: '去品牌体检', confirmation: false, enabled: true,
            target_route: '/diagnosis/new', help_target: 'route-diagnosis-new',
            registry_version: 'operation-registry-v2', primary: true,
          }
        : {
            action_id: 'navigate:writing_center', operation_id: 'writing_center',
            label: '去AI 创作中心', confirmation: false, enabled: true,
            target_route: '/writing', help_target: 'route-writing',
            registry_version: 'operation-registry-v2', primary: true,
          }
      const meta = {
        sources: [],
        confidence: 'high',
        assistant_context: context,
        gap_assistant: {
          headline: diagnosis ? '在「主流程 → 品牌体检」。' : `${context.brand_name} 今天先处理待写任务。`,
          reasons: ['依据当前词包', '依据写作状态', '依据监测短板', '这条不应显示'],
          actions: [
            action,
            {
              action_id: 'navigate:admin_audit', operation_id: 'admin_audit',
              label: '去审计日志', confirmation: false, enabled: false,
              target_route: '/admin/audit', help_target: 'route-admin-audit',
              registry_version: 'operation-registry-v2', primary: false,
            },
          ],
          breadcrumb: diagnosis ? ['主流程', '品牌体检'] : ['AI 创作中心'],
          operation_map_version: 'operation-registry-v2',
          degraded: false,
          context,
          plan_id: `plan-${context.brand_id}`,
          evidence: [
            { label: '词包', value: '5/5 个已确认' },
            { label: '写作', value: context.brand_id === 101 ? '待写 2' : '待写 1' },
          ],
        },
      }
      return route.fulfill({
        status: 200,
        contentType: 'text/event-stream',
        body: sse(meta.gap_assistant.headline, meta),
      })
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 })
    if (path.includes('/agreement')) return json(route, { success: true, required: false })
    if (path === '/api/wallet') return json(route, { success: true, balance: 100, available_balance: 100 })
    return json(route, {
      success: true,
      data: {},
      items: [],
      clients: [],
      brands: [],
      records: [],
      total: 0,
    })
  })
}

/**
 * 把 `NotificationBanner` 喂到可见态(它是 Layout 里**唯一**能与小榜 FAB
 * 真正同屏的常驻底部浮层 —— 见 fab-collision.spec.ts 顶部的互斥说明)。
 *
 * 🔴 R3-P3(Review ②):共存判据以前靠注入一条合成 `fixed bottom-0` 撑分母,
 *    那等于自己造一个对象来验"不与对象相交",分母恒非空,分母门是摆设。
 *    这里改成走组件**自己的**渲染条件:它只在拿到未读 gentle/important 通知时
 *    渲染,且挂载时不会立刻拉 —— 只在 30s 轮询或 `visibilitychange` 时拉。
 */
export async function armNotificationBanner(page: Page) {
  await page.addInitScript(() => localStorage.removeItem('notification_mute_until'))
  // Playwright 后注册的 route 优先,所以这条盖得住 installBackend 的兜底。
  await page.route('**/api/user/notifications**', (route) =>
    route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        status: 'success',
        notifications: [{
          id: 9001,
          title: '有 1 篇稿件等你确认',
          link: '',
          level: 'gentle',
          is_read: false,
          metadata: {},
        }],
      }),
    }),
  )
}

/** 走 NotificationBanner 自己的 visibilitychange handler 触发一次真拉取。 */
export async function triggerNotificationPoll(page: Page) {
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
}

/**
 * 进沙盒态(`OnboardingChecklist` 圆球的**唯一**渲染条件)。
 *
 * 🔴 用它是为了**证明互斥**,不是为了凑共存分母:`Layout.tsx:295/321` 用
 *    `!sandboxUI` 关掉小榜 FAB 与抽屉,而 `Layout.tsx:336` 用 `sandboxUI`
 *    打开圆球 —— 两者永不同屏。
 */
export async function armSandboxChecklist(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_sandbox_active', '1')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'start',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-08-01T00:00:00Z',
      last_updated_at: '2026-08-10T00:00:00Z',
    }))
  })
}
