/**
 * 客户线上购买门控 · Playwright 真渲染断言(2026-07-29)
 *
 * 工单 §2 判别锁 6:`can_purchase_online=false` 时充值页 DOM **无**自由金额输入区
 * ——断"不渲染",不是断 hidden 样式。变异(改为置灰仍渲染)必须转红。
 *
 * 同时锁住白标铁律:被禁页面全文零命中"服务商/代理/主账号/销售"等内部角色词。
 */
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { expect, test, type Page, type Route } from 'playwright/test'

const screenshotDir = resolve(
  process.cwd(),
  process.env.QA_PURCHASE_GATE_SCREENSHOT_DIR || '../_qa_purchase_gate_artifacts/screenshots',
)
mkdirSync(screenshotDir, { recursive: true })

/** 客户面前禁止出现的内部角色词(工单 §0 白标铁律) */
const FORBIDDEN_CUSTOMER_FACING = ['服务商', '代理', '主账号', '销售', '经销商', '分销']

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

function walletPayload(canPurchaseOnline: boolean) {
  return {
    success: true,
    data: {
      paid_points: 5000,
      commission_points: 0,
      bonus_points: 0,
      frozen_points: 0,
      total_recharged: 0,
      agent_level: 0,
      deduction_preference: 'default',
      charge_notify_level: 'off',
      customer_credit_status: 'ready',
      customer_credit: {
        tool_credit_points: 0,
        publish_credit_points: 0,
        bonus_credit_points: 0,
        total_purchased_points: 0,
        total_consumed_points: 0,
      },
      can_purchase_online: canPurchaseOnline,
    },
  }
}

const retailCatalog = {
  success: true,
  data: {
    items: [
      {
        product_code: 'CREDIT_1000',
        display_name: '入门算力包',
        subtitle: '适合首次体验',
        sales_pitch: null,
        scene: null,
        points_granted: 1000,
        bonus_points: 0,
        final_price_cents: 9900,
        usage_examples: [],
      },
    ],
    digital_goods_notice: null,
  },
}

async function openBuyCredit(page: Page, canPurchaseOnline: boolean) {
  const user = {
    id: 200,
    user_id: 200,
    username: 'gate-customer',
    display_name: '客户账号',
    is_admin: false,
    is_active: 1,
    must_change_password: 0,
    agent_level: 0,
    roles: [],
    permissions: [],
    client_brand_ids: [],
  }

  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'purchase-gate-test-token')
    localStorage.setItem('sidebar-collapsed', 'false')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1,
      welcome_choice: 'returning',
      completed_steps: [],
      skipped_steps: [],
      dismissed_features: [],
      viewed_videos: [],
      first_seen_at: '2026-07-29T00:00:00Z',
      last_updated_at: '2026-07-29T00:00:00Z',
    }))
  })

  await page.route('**/api/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (path === '/api/auth/me') return json(route, { success: true, user })
    if (path === '/api/wallet') return json(route, walletPayload(canPurchaseOnline))
    if (path.includes('/retail/catalog') || path.includes('/catalog')) {
      return json(route, retailCatalog)
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 })
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] })
    if (path.includes('/agreement')) return json(route, { success: true, required: false })
    return json(route, { success: true, data: {}, items: [], clients: [] })
  })

  // 判定落地点必须在 goto **之前**注册:/api/wallet 在等标题的过程中就已经响应完了,
  // 事后再 waitForResponse 等的是第二次请求 —— 永远等不到(踩过一次,30s 超时)。
  const walletVerdict = page.waitForResponse(
    (r) => new URL(r.url()).pathname === '/api/wallet',
    { timeout: 20_000 },
  )
  await page.goto('/customer/recharge')
  await expect(page.getByRole('heading', { name: '购买算力' })).toBeVisible({ timeout: 20_000 })
  await walletVerdict
}

test('开关开:自由充值金额输入区正常渲染(存量行为)', async ({ page }) => {
  await openBuyCredit(page, true)

  const amountInput = page.getByLabel('自由充值金额')
  await expect(amountInput).toBeVisible()
  await expect(amountInput).toBeEnabled()
  await expect(page.getByTestId('online-purchase-blocked-notice')).toHaveCount(0)

  await page.screenshot({
    path: resolve(screenshotDir, 'gate-on-free-amount-rendered.png'),
    fullPage: true,
  })
})

test('开关关:自由充值金额输入区在 DOM 里根本不存在(不是置灰)', async ({ page }) => {
  await openBuyCredit(page, false)

  // 先等判定落地再断缺失。canPurchaseOnline 的初值是 fail-open 的 true
  // (门控失灵不堵付款路),所以 /api/wallet 响应到达前自由充值区**本来就还在** DOM 里。
  // 不等这一步就断 count=0 会随 dev server 冷启动时快时慢地假绿/假红。
  await expect(page.getByTestId('online-purchase-blocked-notice')).toBeVisible()

  // 断"不渲染":count=0 而不是 toBeHidden() —— 置灰/隐藏都过不了这条
  await expect(page.getByLabel('自由充值金额')).toHaveCount(0)
  await expect(page.locator('input[aria-label="自由充值金额"]')).toHaveCount(0)
  await expect(page.getByRole('button', { name: '计算到账' })).toHaveCount(0)
  await expect(page.getByRole('button', { name: '按此金额充值' })).toHaveCount(0)

  // 支付发起区被提示卡替换
  const notice = page.getByTestId('online-purchase-blocked-notice')
  await expect(notice).toBeVisible()
  await expect(notice).toContainText('如需充值或购买，请联系您的推荐人办理')

  await page.screenshot({
    path: resolve(screenshotDir, 'gate-off-free-amount-unrendered.png'),
    fullPage: true,
  })
})

test('开关关:点算力包"立即购买"弹提示弹窗,文案零内部术语', async ({ page }) => {
  await openBuyCredit(page, false)

  // 同上:等判定落地。判定到达前 canPurchase 仍是 true,点"立即购买"会走下单链路而不是弹提示。
  await expect(page.getByTestId('online-purchase-blocked-notice')).toBeVisible()

  await page.getByRole('button', { name: '立即购买' }).first().click()
  const dialog = page.getByTestId('online-purchase-blocked-dialog')
  await expect(dialog).toBeVisible()
  await expect(dialog).toContainText('如需充值或购买，请联系您的推荐人办理')

  await page.screenshot({
    path: resolve(screenshotDir, 'gate-off-blocked-dialog.png'),
    fullPage: true,
  })

  // 白标铁律:整页可见文本零命中内部角色词
  const bodyText = (await page.locator('body').innerText()) || ''
  for (const term of FORBIDDEN_CUSTOMER_FACING) {
    expect(bodyText, `被禁页面出现内部术语「${term}」`).not.toContain(term)
  }
})
