/**
 * 标题生成失败可见性 · Playwright 真渲染断言(T5 · 2026-07-28 P0)
 *
 * 工单:docs/AI-CONTEXT/WORKORDER_ATTRIBUTION_FIX_2026-07-28.md §5.5-3 / §5.5-5
 *
 * 事故:QZQZ(quote 386)写 12 篇 → 标题阶段失败,topics 行从未创建,
 * 写作大厅一片空白。后端已改成"先落库再生成 + 失败就地标记 failed";
 * 本 spec 锁住**前端真的把它渲染出来了**:
 *
 *   锁 A 失败条目在"待写"列表里可见,显示关键词而不是空白行;
 *   锁 B 失败原因(错误码 + 人话)真渲染在 DOM 里,不是只挂 title 属性;
 *   锁 C 重试入口存在且是"重新生成标题"——**不走**正文那套退款闸门
 *         (标题阶段没扣过费,generation_refund_status 恒空;
 *          若沿用正文分支,退款闸门会把按钮永久藏掉 → 用户依然无路可走);
 *   锁 D 标题阶段失败不得出现"退款/未扣费"类资金话术(本次根本没扣钱)。
 *
 * 变异:后端不落 failed 行(列表回到空) / 前端不区分 title_ 阶段(按钮消失)
 *       → 锁 A / 锁 C 转红。
 */
import { expect, test, type Page, type Route } from 'playwright/test'

const agentUser = {
  id: 7101,
  user_id: 7101,
  username: 'title-failure-agent',
  display_name: '服务商运营',
  is_admin: true,
  is_active: 1,
  must_change_password: 0,
  agent_level: 2,
  roles: [{ id: 1, name: 'admin', display_name: '管理员' }],
  permissions: ['writing:read', 'writing:write', 'brands:read'],
  client_brand_ids: [601],
}

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
}

/** 后端 T5 落下来的两条"标题阶段失败"行:没有 optimized_title,没有退款状态。 */
const titleFailureTopics = [
  {
    id: 6301,
    keyword_id: 901,
    original_keyword: '净水器十大品牌',
    optimized_title: null,
    status: 'failed',
    article_id: null,
    generation_request_id: 'titles-qzqz0001',
    generation_operation: 'title_batch',
    generation_error_code: 'ARTICLE_PROVIDER_UNAVAILABLE',
    generation_error_message: '写作模型暂时不可用，本次未交付内容，可稍后安全重试。',
    generation_retryable: true,
    generation_failure_phase: 'title_generation',
    // 🔴 标题阶段没扣费 → 退款状态恒空。正文分支正是靠它放行重试按钮的。
    generation_refund_status: null,
  },
  {
    id: 6302,
    keyword_id: 902,
    original_keyword: '商用净水设备',
    optimized_title: null,
    status: 'failed',
    article_id: null,
    generation_request_id: 'titles-qzqz0001',
    generation_operation: 'title_batch',
    generation_error_code: 'TITLE_OUTPUT_EMPTY',
    generation_error_message: '本次没有生成可用标题，请稍后重试或先检查关键词。',
    generation_retryable: true,
    generation_failure_phase: 'title_output',
    generation_refund_status: null,
  },
]

/** 待写列表按关键词分组折叠;先展开再断言条目。 */
async function openPendingList(page: Page) {
  await page.goto('/writing?quote_id=386')
  await expect(page.getByRole('tab', { name: /待写 \(2\)/ })).toBeVisible()
  await page.getByRole('button', { name: '全部展开' }).click()
}

async function installWritingSession(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'local-title-failure-probe')
    localStorage.setItem('omnirank_m3_onboarded_at', 'local-ui-probe')
  })
  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/auth/me') return json(route, { success: true, user: agentUser })
    if (path === '/api/wallet') {
      return json(route, { success: true, data: { paid_points: 100000, total: 100000, frozen_points: 0 } })
    }
    if (path === '/api/writing/projects') {
      return json(route, {
        projects: [{
          id: 386,
          quote_ids: [386],
          brand_id: 601,
          brand_name: 'QZQZ',
          industry: '净水设备',
          keyword_count: 2,
          total_required_articles: 12,
          monthly_price: 0,
          writing_status: 'pending',
          confirmed_at: '2026-07-26T00:00:00Z',
        }],
      })
    }
    if (path === '/api/writing/projects/386') {
      return json(route, {
        quote: { id: 386, brand_id: 601, brand_name: 'QZQZ', industry: '净水设备' },
        keywords: [
          { id: 901, keyword: '净水器十大品牌', required_articles: 6, recommended_platforms: null, final_price: 0 },
          { id: 902, keyword: '商用净水设备', required_articles: 6, recommended_platforms: null, final_price: 0 },
        ],
        topics: titleFailureTopics,
      })
    }
    if (path === '/api/writing/projects/386/knowledge-status') {
      return json(route, { success: true, quote_id: 386, brand_id: 601, status: 'ready', materials_summary: {} })
    }
    if (path === '/api/writing/projects/386/structure-guidance') {
      return json(route, { success: true, can_apply: false, default_enabled: false })
    }
    if (path === '/api/writing/competitors/386') return json(route, { competitors: [], mode: 'evidence_only' })
    if (path === '/api/knowledge/client/601') return json(route, { success: true, documents: [] })
    if (path === '/api/brand-images/list/601') return json(route, { success: true, assets: [] })
    if (path === '/api/my-clients/601') {
      return json(route, { success: true, brand: { id: 601, name: 'QZQZ' }, profile: {} })
    }
    return json(route, { success: true, data: {}, items: [], total: 0 })
  })
}

test('锁 A/B · 标题阶段失败在写作大厅可见,并显示原因', async ({ page }, testInfo) => {
  await installWritingSession(page)
  await openPendingList(page)

  // 锁 A:不再是空白 —— 两条失败条目都在,且以关键词占位显示
  await expect(page.getByText('净水器十大品牌（待重新生成标题）')).toBeVisible()
  await expect(page.getByText('商用净水设备（待重新生成标题）')).toBeVisible()

  // 锁 B:失败原因真渲染在 DOM 文本里(不是只挂在 title 属性上)
  const bodyText = await page.evaluate(() => document.body.innerText)
  expect(bodyText).toContain('ARTICLE_PROVIDER_UNAVAILABLE')
  expect(bodyText).toContain('TITLE_OUTPUT_EMPTY')
  expect(bodyText).toContain('写作模型暂时不可用')

  await page.screenshot({ path: testInfo.outputPath('title-failure-list.png'), fullPage: true })
})

test('锁 C · 重试入口是"重新生成标题",不被正文退款闸门挡掉', async ({ page }) => {
  await installWritingSession(page)
  await openPendingList(page)

  const retry = page.locator('[data-testid="retry-title-generation"]')
  await expect(retry.first()).toBeVisible()
  await expect(retry.first()).toHaveText(/重新生成标题/)
  // generation_refund_status 为空时,正文那套"安全重试"必须不出现 ——
  // 出现即说明前端没按阶段分流,标题失败被塞进了资金语义的分支。
  await expect(page.getByRole('button', { name: '安全重试' })).toHaveCount(0)
})

test('锁 D · 标题阶段失败不出现退款类资金话术', async ({ page }) => {
  await installWritingSession(page)
  await openPendingList(page)
  await expect(page.getByText('净水器十大品牌（待重新生成标题）')).toBeVisible()

  const bodyText = await page.evaluate(() => document.body.innerText)
  for (const moneyWord of ['旧任务暂无退款记录', '退款已完成', '退款处理中', '已扣费']) {
    expect(bodyText).not.toContain(moneyWord)
  }
})
