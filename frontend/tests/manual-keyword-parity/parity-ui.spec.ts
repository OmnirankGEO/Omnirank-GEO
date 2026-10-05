import { expect, test, type Page, type Route } from 'playwright/test'

/**
 * [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-8] 手动词 UI 与合同词同权 —— **真浏览器渲染锁**。
 *
 * 🔴 元素存在不算。每条都要求:渲染内容正确 + 可点 + 点了真发出正确的请求。
 *
 * 锁1  手动词行有监测开关(不再是 source==='confirmed' 才画)
 * 锁2  点手动词开关,请求带 source=extra(不带的话后端走 confirmed 分支查订阅 → 404)
 * 锁2R 反向对照:点合同词开关**不带** source=extra(证明锁2 不是"给所有请求都拼上了")
 * 锁3  开关关闭的词不再并排显示进行时 —— 渲染的是「已停」
 * 锁3R 反向对照:开着的词仍显示进行时(证明锁3 不是"把所有词都写成已停")
 * 锁4  归档列表里手动词的「续费」按钮**可点**(不是 disabled)
 * 锁5  点它真发出 renew 请求且带 source=extra
 */

const BRAND = 4242
const QUOTE = 900

const KEYWORDS = [
  { id: 5001, keyword: '合同词-在跑', target_brand: 'A 品牌', source: 'confirmed',
    lifecycle: 'monitoring', is_monitored: true, monitoring_status: 'active',
    detection_rate: 60, effective_rate: 60, appearance_rate: 60, compliant_days: 3, service_days: 180 },
  { id: 5002, keyword: '手动词-在跑', target_brand: 'A 品牌', source: 'extra',
    lifecycle: 'monitoring', is_monitored: true, monitoring_status: 'active',
    detection_rate: 55, effective_rate: 55, appearance_rate: 55, compliant_days: 2, service_days: 180 },
  { id: 5003, keyword: '手动词-已关', target_brand: 'A 品牌', source: 'extra',
    lifecycle: 'stopped_detected', is_monitored: false, monitoring_status: 'active',
    detection_rate: 0, effective_rate: 0, appearance_rate: 0, compliant_days: 1, service_days: 180 },
]

const ARCHIVED = [
  { id: 5101, keyword: '归档手动词', source: 'extra', quote_id: QUOTE,
    archived_at: '2026-07-01T00:00:00Z', archive_reason: 'expired', target_brand: 'A 品牌' },
  { id: 5102, keyword: '归档合同词', source: 'confirmed', quote_id: QUOTE,
    archived_at: '2026-07-01T00:00:00Z', archive_reason: 'expired', target_brand: 'A 品牌' },
]

interface Seen { toggles: string[]; renews: string[] }

async function mockApp(page: Page, seen: Seen) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'qa-token')
    localStorage.setItem('omnirank-theme', 'dark')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'never', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-07-21T12:00:00Z', last_updated_at: '2026-07-21T12:00:00Z',
    }))
  })

  const json = (route: Route, body: unknown) => route.fulfill({
    status: 200, contentType: 'application/json', body: JSON.stringify(body),
  })

  await page.route('**/api/**', async (route: Route) => {
    const url = new URL(route.request().url())
    const p = url.pathname
    const full = url.pathname + url.search

    if (p === '/api/auth/me') {
      return json(route, { success: true, user: { id: 1, username: 'qa', display_name: 'QA',
        is_admin: true, agent_level: 1, permission_version: 1, modules: ['monitoring'] } })
    }
    if (p === '/api/client-context/list') {
      return json(route, { success: true, clients: [{ id: BRAND, name: 'A 品牌', brand_type: 'client' }] })
    }
    if (p.startsWith('/api/client-context/')) {
      return json(route, { success: true, context: {
        brand: { id: BRAND, name: 'A 品牌', industry: '测试行业', diagnosis_count: 0, brand_type: 'client' },
        profile: null, materials: null, relatedQuoteIds: [String(QUOTE)],
        socialProjects: [], access_mode: 'real' } })
    }
    if (p === '/api/monitoring/clients') {
      return json(route, { status: 'success', clients: [
        { quote_id: QUOTE, brand_id: BRAND, brand_name: 'A 品牌', keyword_count: KEYWORDS.length } ] })
    }
    if (/\/api\/monitoring\/clients\/\d+\/keywords$/.test(p)) {
      return json(route, { status: 'success', keywords: KEYWORDS, super_red_ocean_keywords: [],
        service_days: 180, service_start_date: '2026-05-10', contract_end_date: '2026-12-10' })
    }
    if (p.includes('archived-keywords')) {
      return json(route, { status: 'success', success: true, keywords: ARCHIVED, items: ARCHIVED })
    }
    if (/\/(enable|disable)$/.test(p)) {
      seen.toggles.push(full)
      return json(route, { success: true })
    }
    if (/renew$/.test(p)) {
      seen.renews.push(full)
      return json(route, { success: true, keyword_id: 5101, keyword: '归档手动词', quote_id: QUOTE, message: 'ok' })
    }
    return json(route, { success: true, status: 'success', items: [], clients: [], keywords: [], data: {} })
  })
}

/** 开关有二次确认弹窗(元指令 #2 按钮级确认扣费)—— 点掉它,否则请求根本不发。 */
async function confirmDialog(page: Page) {
  const btn = page.getByRole('button', { name: /^(开通|关闭|续费|确认|确定)$/ }).last()
  await expect(btn).toBeVisible({ timeout: 10_000 })
  await btn.click()
}

async function openMonitoring(page: Page) {
  await page.goto(`/monitoring?brand_id=${BRAND}`)
  await expect(page.getByTestId('monitoring-keyword-row-5001')).toBeVisible({ timeout: 20_000 })
}

test('锁1+锁2+锁2R 手动词有开关,点了带 source=extra;合同词不带', async ({ page }) => {
  const seen: Seen = { toggles: [], renews: [] }
  await mockApp(page, seen)
  await openMonitoring(page)

  const manualRow = page.getByTestId('monitoring-keyword-row-5002')
  await expect(manualRow).toBeVisible()
  const manualSwitch = manualRow.locator('button[role="switch"]').first()
  await expect(manualSwitch, '手动词行没有监测开关 —— 逐行开关仍限制 source===confirmed').toBeVisible()
  await expect(manualSwitch).toBeEnabled()

  await manualSwitch.click()
  await confirmDialog(page)
  await expect.poll(() => seen.toggles.length, { timeout: 10_000 }).toBeGreaterThan(0)
  expect(seen.toggles.some(u => u.includes('source=extra')),
    `手动词开关请求没带 source=extra:${JSON.stringify(seen.toggles)}`).toBeTruthy()

  const before = seen.toggles.length
  const contractSwitch = page.getByTestId('monitoring-keyword-row-5001').locator('button[role="switch"]').first()
  await contractSwitch.click()
  await confirmDialog(page)
  await expect.poll(() => seen.toggles.length, { timeout: 10_000 }).toBeGreaterThan(before)
  expect(seen.toggles.slice(before).some(u => u.includes('source=extra')),
    '合同词也被拼上了 source=extra —— 锁2 零判别力(是给所有请求都拼上了)').toBeFalsy()
})

test('锁3+锁3R 关掉的词渲染「已停」而不是并排进行时', async ({ page }) => {
  const seen: Seen = { toggles: [], renews: [] }
  await mockApp(page, seen)
  await openMonitoring(page)

  const offRow = page.getByTestId('monitoring-keyword-row-5003')
  await expect(offRow).toBeVisible()
  const offText = (await offRow.innerText()).replace(/\s+/g, '')
  expect(offText, `关掉的词这一行没渲染「已停」:${offText}`).toContain('已停')
  expect(offText, `关掉的词仍并排显示进行时字样:${offText}`).not.toContain('铺量中')

  const onText = (await page.getByTestId('monitoring-keyword-row-5002').innerText()).replace(/\s+/g, '')
  expect(onText, `在跑的词也被写成「已停」→ 锁3 零判别力:${onText}`).not.toContain('已停')
})

test('锁4+锁5 归档手动词的续费按钮可点,且点了带 source=extra', async ({ page }) => {
  const seen: Seen = { toggles: [], renews: [] }
  await mockApp(page, seen)
  await openMonitoring(page)

  // 归档列表是常驻分区(默认折叠),标题里带「归档」;点它展开
  const opener = page.getByText(/已归档/).first()
  await opener.click()

  const row = page.locator('tr').filter({ hasText: '归档手动词' }).last()
  await expect(row).toBeVisible({ timeout: 15_000 })
  const renewBtn = row.getByRole('button', { name: /续费/ }).first()
  await expect(renewBtn, '归档手动词没有续费按钮').toBeVisible()
  await expect(renewBtn, '归档手动词的续费按钮仍是 disabled —— 前端没解禁').toBeEnabled()

  await renewBtn.click()
  // 🔴 续费也有二次确认。顺带验文案按来源分:手动词不许说"服务期从今天重算"(K3)
  const confirmBtn = page.getByRole('button', { name: /^确定$/ }).last()
  await expect(confirmBtn, '续费没有二次确认(元指令 #2 按钮级确认扣费)').toBeVisible({ timeout: 10_000 })
  const dlgText = (await page.locator('body').innerText()).replace(/\s+/g, '')
  expect(dlgText, '手动词续费弹窗说了"服务期重算" —— K3 下这是假话').not.toContain('服务期将从今天重新计算')
  expect(dlgText, '手动词续费弹窗没说明不影响服务期').toContain('不影响这张报价单的服务期')
  await confirmDialog(page)
  await expect.poll(() => seen.renews.length, { timeout: 10_000 }).toBeGreaterThan(0)
  expect(seen.renews.some(u => u.includes('source=extra')),
    `续费请求没带 source=extra → 后端会去改同 id 的合同词:${JSON.stringify(seen.renews)}`).toBeTruthy()
})
