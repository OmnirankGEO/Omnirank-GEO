/**
 * XO-03 预填判据的 mock 后端。
 *
 * 🔴 预填 DTO **照抄服务端 `build_prefill()` 的形状**,并由 Python 侧判据
 *    `test_prefill_dto_shape_matches_the_browser_fixture` 逐键核对 ——
 *    否则前端判据会对着一个服务端根本不会返回的形状全绿(本仓付过这个费)。
 */
import { type Page, type Route } from 'playwright/test'

export const PREFILL_INTENT_ID = 'xint_pw0000000001'

/**
 * [R3-P11 ①] 对象变体。
 *
 * 🔴 上一轮这里只有文章 #4201 —— **单对象分母**:geo 图文与报价这两条链
 * 就算完全没接线,浏览器判据也全绿(它压根没打到那两个对象)。
 *
 * 每个变体的 `form_prefill` 键必须是**生产端 `_frozen_form_prefill` 真会写**的那些
 * (Python 侧 `test_browser_fixture_form_prefill_fields_match_the_producer_allowlist`
 * 逐键对账);`channel` 同理对 `_frozen_channel` 的产出形状。
 */
export type PrefillVariant = 'article' | 'geo_post' | 'quote' | 'geo_post_verified'

export const PREFILL_VARIANTS: Record<PrefillVariant, {
  objectLabel: string
  items: Array<{ resource_kind: string; label: string }>
  form_prefill: Record<string, number>
  channel: Record<string, unknown>
  /** 这个变体应该落进 URL 的参数(判据据此断言"表被真填了")。 */
  expectedParams: Record<string, string>
}> = {
  article: {
    objectLabel: '甲品牌 · 文章 #4201',
    items: [
      { resource_kind: 'brand', label: '甲品牌' },
      { resource_kind: 'article', label: '文章 #4201' },
    ],
    form_prefill: { brand_id: 101, quote_id: 77, article_id: 4201 },
    channel: { verified: false, resource_kind: 'article' },
    expectedParams: { brand_id: '101', quote_id: '77', article_id: '4201' },
  },
  geo_post: {
    objectLabel: '甲品牌 · 图文 #5150',
    items: [
      { resource_kind: 'brand', label: '甲品牌' },
      { resource_kind: 'geo_image_post', label: '图文 #5150' },
    ],
    form_prefill: { brand_id: 101, geo_post_id: 5150 },
    // 渠道项选过 → 这一档能说出是哪一项(资格仍未核)
    channel: { verified: false, resource_kind: 'geo_image_post', channel_option_id: 'douyin_main' },
    expectedParams: { brand_id: '101', geo_post_id: '5150' },
  },
  // [WO-B ③ 2026-08-20] 第四档:渠道**已核**。
  // ③ 之前 `verified` 恒 false ⇒ 上面三档打的全是"还没核"那一支,
  // 「核过了但这个号不能用」这条路**一个判据都没有**。
  // 这一档专门打它:verified=true + eligible=false + 服务端给的人话结论。
  geo_post_verified: {
    objectLabel: '甲品牌 · 图文 #5150',
    items: [
      { resource_kind: 'brand', label: '甲品牌' },
      { resource_kind: 'geo_image_post', label: '图文 #5150' },
    ],
    form_prefill: { brand_id: 101, geo_post_id: 5150 },
    channel: {
      verified: true, eligible: false, resource_kind: 'geo_image_post',
      channel_option_id: 'svideo:12',
      eligibility_note: '这个账号发不了图文,换一个能发图文的',
    },
    expectedParams: { brand_id: '101', geo_post_id: '5150' },
  },
  quote: {
    objectLabel: '甲品牌 · 报价 #77',
    items: [
      { resource_kind: 'brand', label: '甲品牌' },
      { resource_kind: 'quote', label: '报价 #77' },
    ],
    form_prefill: { brand_id: 101, quote_id: 77 },
    // 什么渠道信息都没有 → 前端必须走"还没选目标渠道"那一档
    channel: {},
    expectedParams: { brand_id: '101', quote_id: '77' },
  },
}

const user = {
  id: 301, username: 'pw-agent', display_name: '预填测试代理',
  is_admin: false, agent_level: 1,
  // 🔴 `/publish` 的路由守卫是 `requiredModule="writing"`,而 `hasModule` 判的是
  //    `permissions` 里有没有 `writing:` 前缀(AuthContext:868)。第一版夹具只给了
  //    `publish:*` → 页面被权限门挡在门外,`prefillCalls` 恒空。
  //    那**不是**产品缺陷,是夹具没跑起来 —— 「跑没跑起来」必须先跟「过没过」分开。
  permissions: ['writing:read', 'writing:write', 'publish:read', 'publish:write'],
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

export function prefillFixture(intentState = 'prepared',
                              variant: PrefillVariant = 'article') {
  const shape = PREFILL_VARIANTS[variant]
  return {
    contract_version: 'xiaobang-page-prefill-v1',
    intent_id: PREFILL_INTENT_ID,
    intent_state: intentState,
    intent_revision: 1,
    operation_id: 'publish_center',
    display_name: '发布管理',
    side_effect: 'external',
    target_route: '/publish',
    help_target: 'publish-panel',
    customer: { label: '甲品牌' },
    object: {
      // 🔴 服务端**不下发** scope(内部工程标记 + DLP 判红形态);夹具同形。
      label: shape.objectLabel,
      items: shape.items,
      pending_checks: ['投放账号资格还没核'],
    },
    recommendation: {
      reasons: [{ reason_code: 'low_coverage', explanation: '这个词还没被推荐过,先补一篇' }],
    },
    external_actions: [{ state: 'pending_domain_adapter', label: '对外动作要在核对页选定投放账号后才能逐条列出' }],
    payer: { label: '你的钱包', role: 'self' },
    visibility: '发布后公开可见',
    channel: shape.channel,
    compute: { state: 'quoted', amount: 390, unit: '算力', expires_at: null },
    primary_action: { next_action_id: 'confirm_then_run', label: '确认并执行 · 使用 390 算力', enabled: true },
    cancel_action: { next_action_id: 'cancel_free', label: '取消本次操作 · 不使用算力', enabled: true },
    secondary_actions: [
      { next_action_id: 'back_to_assistant', label: '返回小榜' },
      { next_action_id: 'reprepare', label: '重新准备' },
    ],
    // 🔴 [R3-P8 ①] 这三个字段是**生产端 `_frozen_form_prefill` 真产出**的形状
    //    (Python 侧判据 `test_browser_fixture_form_prefill_fields_match_the_producer_allowlist`
    //     与 `test_prefill_fixture_matches_a_real_prepare_output` 逐字对账)。
    //    R3-P7 这里是夹具自己造的 —— 生产端一个字没写,于是"表被真填了"
    //    打的是夹具自己造的字段。
    form_prefill: shape.form_prefill,
    rebind_hint: {
      label: '内容已改,算力需要更新',
      primary_label: '重新计算并核对',
      watched_fields: ['article_id', 'brand_id'],
    },
    settlement_state: 'none',
    status: {
      intent_state: intentState,
      intent_revision: 1,
      domain_projection: { state: 'not_started', reference: null },
      settlement_projection: { state: 'none', amount: 390, unit: '算力' },
      external_projection: { state: 'not_started' },
    },
  }
}

export async function installPrefillBackend(
  page: Page,
  prefillCalls: string[],
  opts: { prefillStatus?: number; intentState?: string; variant?: PrefillVariant } = {},
) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'xiaobang-prefill-browser-token')
    localStorage.setItem('sidebar-collapsed', 'false')
  })

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname

    if (path.includes('/api/xiaobang/operations/intents/') && path.endsWith('/prefill')) {
      prefillCalls.push(url.pathname + url.search)
      if (opts.prefillStatus && opts.prefillStatus !== 200) {
        return json(route, { detail: { error_code: 'INTENT_NOT_FOUND' } }, opts.prefillStatus)
      }
      return json(route, prefillFixture(opts.intentState || 'prepared',
                                       opts.variant || 'article'))
    }

    if (path === '/api/auth/me') return json(route, { success: true, user })
    if (path === '/api/organization/overview') return json(route, { detail: 'not found' }, 404)
    if (path === '/api/my-clients') {
      return json(route, { success: true, clients: [{ id: 101, name: '甲品牌', industry: '企业服务' }] })
    }
    if (path.startsWith('/api/publish/projects')) {
      return json(route, {
        success: true,
        projects: [{ brand_id: 101, brand_name: '甲品牌', quote_id: 77, project_name: '甲品牌' }],
      })
    }
    if (path.includes('/geo/douyin/posts') || path.includes('/geo-posts')) {
      return json(route, {
        success: true,
        posts: [{ id: 5150, title: '甲品牌图文', brand_id: 101, status: 'draft' }],
      })
    }
    if (path.startsWith('/api/publish/articles') || path.includes('/articles')) {
      return json(route, {
        success: true,
        articles: [{
          id: 4201, article_id: 4201, title: '甲品牌怎么选', status: 'draft',
          publication_eligible: true, brand_id: 101,
        }],
      })
    }
    // 其余 API 一律给一个不含业务含义的空成功体:判据不该因为某个无关轮询 500 而红。
    return json(route, { success: true, items: [], data: null })
  })
}
