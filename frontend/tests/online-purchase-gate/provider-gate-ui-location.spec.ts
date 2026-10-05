/**
 * [微单 C-6 2026-07-28] 门控开关 UI 位置回归(真渲染截图)。
 *
 * - /agent/pricing(客户售价):门控面板在场且置顶(总开关 + 客户级三态同页);
 * - /agent/promotion(推广中心):旧入口已撤(单一入口);
 * - 截图交付:_qa_purchase_gate_artifacts/screenshots/provider-gate-*.png
 */
import { expect, test, type Page, type Route } from 'playwright/test'
import { mkdirSync } from 'node:fs'
import { resolve } from 'node:path'

const screenshotDir = resolve(
  process.env.QA_PURCHASE_GATE_SCREENSHOT_DIR || '../_qa_purchase_gate_artifacts/screenshots',
)
mkdirSync(screenshotDir, { recursive: true })

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    body: JSON.stringify(body),
  })
}

const providerUser = {
  id: 300,
  user_id: 300,
  username: 'gate-provider',
  display_name: '服务商账号',
  is_admin: false,
  is_active: 1,
  must_change_password: 0,
  agent_level: 1,
  roles: [],
  permissions: [],
  client_brand_ids: [],
}

const gateCustomers = {
  success: true,
  items: [
    {
      customer_user_id: 501,
      display_name: '张三',
      phone_masked: '138****0001',
      binding_source: 'referral_link',
      bound_at: '2026-07-01T00:00:00Z',
      tool_credit: 1000,
      publish_credit: 0,
      bonus_credit: 100,
      online_purchase_override: 'inherit',
      can_purchase_online: true,
    },
    {
      customer_user_id: 502,
      display_name: '李四',
      phone_masked: '139****0002',
      binding_source: 'invite_code',
      bound_at: '2026-07-02T00:00:00Z',
      tool_credit: 0,
      publish_credit: 0,
      bonus_credit: 0,
      online_purchase_override: 'offline_only',
      can_purchase_online: false,
    },
  ],
  total: 2,
  default_allow_client_online_purchase: true,
}

async function installProviderSession(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'purchase-gate-provider-token')
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
    if (path === '/api/auth/me') return json(route, { success: true, user: providerUser })
    if (path === '/api/agent/promotion/customers') return json(route, gateCustomers)
    if (path === '/api/agent/client-purchase/settings') {
      return json(route, { success: true, allow_client_online_purchase: true })
    }
    if (path === '/api/agent/promotion/qr') {
      return json(route, {
        success: true, ref_code: 'REF300', invite_code: 'INV300',
        ref_link: 'https://omnirank.top/r/REF300',
      })
    }
    if (path === '/api/notifications/unread-count') return json(route, { count: 0 })
    if (path === '/api/client-context/list') return json(route, { success: true, clients: [] })
    if (path.includes('/agreement')) {
      return json(route, { success: true, status: 'signed' })
    }
    return json(route, { success: true, data: {}, items: [], clients: [], total: 0 })
  })
}

test('[C-6] 客户售价页:门控面板在场且置顶(总开关+客户三态),截图回报位置', async ({ page }) => {
  await installProviderSession(page)
  await page.goto('/agent/pricing')

  const panel = page.locator('[data-testid="client-purchase-gate-panel"]')
  await expect(panel).toBeVisible({ timeout: 15000 })
  await expect(panel.locator('[data-testid="client-purchase-gate-master"]')).toBeVisible()
  await expect(panel.getByLabel('允许名下客户线上购买')).toBeVisible()
  // 客户级三态在同页客户行呈现
  await expect(panel.getByText('张三')).toBeVisible()
  await expect(panel.locator('select')).toHaveCount(2)
  // 置顶:面板必须出现在"新增算力包"经营区块(SKU 卡)之前
  const panelBox = await panel.boundingBox()
  const skuHeading = page.getByText('算力包加价系数').first()
  await expect(skuHeading).toBeVisible()
  const skuBox = await skuHeading.boundingBox()
  expect(panelBox && skuBox && panelBox.y < skuBox.y).toBeTruthy()

  await page.screenshot({
    path: resolve(screenshotDir, 'provider-gate-on-pricing-page.png'),
    fullPage: true,
  })
})

test('[C-6] 推广中心:旧门控入口已撤(单一入口)', async ({ page }) => {
  await installProviderSession(page)
  await page.goto('/agent/promotion')

  // 页面主体加载完成(已绑定客户表在场)
  await expect(page.getByText('已绑定客户', { exact: false })).toBeVisible({ timeout: 15000 })
  // 旧门控卡与三态列绝迹
  await expect(page.locator('[data-testid="client-purchase-gate-panel"]')).toHaveCount(0)
  await expect(page.getByLabel('允许名下客户线上购买')).toHaveCount(0)
  await expect(page.getByText('允许线上购买', { exact: true })).toHaveCount(0)

  await page.screenshot({
    path: resolve(screenshotDir, 'provider-gate-removed-from-promotion.png'),
    fullPage: true,
  })
})
