/**
 * WO-B2 ① 浏览器判据:**producer 端到端一条真链** —— 卡片 → URL → 页面预填渲染。
 *
 * WO-B 把三页的消费方接通了,但那一单 §8-2 自己写着:全仓没有任何地方构造
 * `?xiaobang_intent=`,消费方只能靠手拼 URL 到达。也就是说那条链只有下半截。
 *
 * 本文件打的就是**上半截接没接上**,四跳缺一不可:
 *
 *   ① 小榜答案里的链接卡带着服务端签发的 `operation_id`(不是前端猜的);
 *   ② 点它 → 真的 `POST …/{operation_id}/prepare`(不是直接 navigate);
 *   ③ URL 上出现 `?xiaobang_intent=xint_…`,而且**只有**这一个 opaque id
 *      (§12.2:不放客户名 / 供应商 / 算力公式);
 *   ④ 目标页真的把预填**渲染出来**(不是"跳到了就算")。
 *
 * 🔴 只断言前三跳会漏掉最贵的那种失败:URL 拼对了、页面却因为参数名/路由对不上
 *    而打开一张空白默认表单 —— 那正是 §12.3 要消灭的形态。所以第 ④ 跳必须打。
 */
import { expect, test, type Page } from 'playwright/test'
import { prefillFixture, PREFILL_INTENT_ID } from './prefill-harness'

const CHAT_USER = {
  id: 301, username: 'pw-agent', display_name: '深链测试代理',
  is_admin: false, agent_level: 1,
  permissions: ['writing:read', 'writing:write', 'publish:read', 'publish:write',
                'materials:read', 'materials:write'],
}

/** 把 meta 包成一段真 SSE —— 与生产 `/api/xiaobang/chat` 的帧序一致。 */
function sse(body: string, meta: unknown): string {
  return [
    ': ready', '',
    'event: text', `data: ${JSON.stringify({ delta: body })}`, '',
    'event: meta', `data: ${JSON.stringify(meta)}`, '',
    'event: done', 'data: {}', '',
  ].join('\n')
}

type Recorded = { prepareCalls: string[]; prepareBodies: unknown[]; prefillCalls: string[] }

/**
 * @param operationId 服务端给这张卡带的 operation_id;传 `null` 模拟**没有合同**的路由
 *                    (老行为:裸跳转,不 prepare)。
 */
async function installProducerBackend(
  page: Page, rec: Recorded,
  opts: { operationId: string | null; route: string; targetRoute?: string },
) {
  await page.addInitScript(() => {
    localStorage.setItem('omnirank_token', 'xiaobang-deeplink-browser-token')
    localStorage.setItem('sidebar-collapsed', 'false')
    // 🔴 **显式退出沙盒态**。`Layout.tsx:295/321` 用 `!sandboxUI` 关掉小榜 FAB
    //    与抽屉 —— 沙盒里根本没有卡片可点,判据会以"找不到元素"红,
    //    而那不是产品缺陷,是夹具没跑起来(「跑没跑起来」必须先跟「过没过」分开)。
    localStorage.removeItem('omnirank_sandbox_active')
    localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
      version: 1, welcome_choice: 'skip', completed_steps: [], skipped_steps: [],
      dismissed_features: [], viewed_videos: [],
      first_seen_at: '2026-08-01T00:00:00Z', last_updated_at: '2026-08-10T00:00:00Z',
    }))
  })

  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    const json = (body: unknown, status = 200) =>
      route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

    if (path === '/api/xiaobang/chat') {
      const link: Record<string, unknown> = { route: opts.route, label: '去这一页' }
      if (opts.operationId) link.operation_id = opts.operationId
      return route.fulfill({
        status: 200, contentType: 'text/event-stream',
        body: sse('这件事在这一页做。', { sources: [], confidence: 'high', link }),
      })
    }
    // 🔴 producer 的那一跳:prepare。判据要看到它**真的**被调了。
    if (path.endsWith('/prepare') && path.includes('/api/xiaobang/operations/')) {
      rec.prepareCalls.push(path)
      rec.prepareBodies.push(route.request().postDataJSON())
      return json({
        intent_id: PREFILL_INTENT_ID,
        intent_state: 'prepared',
        intent_revision: 1,
        replayed: false,
        deep_link: { target_route: opts.targetRoute ?? opts.route, help_target: 'x' },
      })
    }
    if (path.includes('/api/xiaobang/operations/intents/') && path.endsWith('/prefill')) {
      rec.prefillCalls.push(url.pathname + url.search)
      return json(prefillFixture('prepared', 'geo_post'))
    }

    if (path === '/api/auth/me') return json({ success: true, user: CHAT_USER })
    if (path === '/api/organization/overview') return json({ detail: 'not found' }, 404)
    if (path === '/api/my-clients') {
      return json({ success: true, clients: [{ id: 101, name: '甲品牌', industry: '企业服务' }] })
    }
    if (path === '/api/notifications/unread-count') return json({ count: 0 })
    return json({ success: true, items: [], clients: [], brands: [], data: null })
  })
}

/** 打开抽屉 → 发一句话 → 等链接卡出现。 */
async function askAndGetCard(page: Page) {
  await page.getByRole('button', { name: '打开小榜 GEO 助手' }).click()
  const input = page.getByRole('textbox').first()
  await input.fill('这件事在哪做')
  await input.press('Enter')
  const card = page.getByTestId('xiaobang-link-card')
  await expect(card).toBeVisible({ timeout: 20_000 })
  return card
}

test('卡片 → prepare → URL 带 intent → 目标页预填真的渲染出来', async ({ page }) => {
  const rec: Recorded = { prepareCalls: [], prepareBodies: [], prefillCalls: [] }
  await installProducerBackend(page, rec, { operationId: 'publish_center', route: '/publish' })
  await page.goto('/dashboard/today')

  // ① 卡片带着服务端签发的 operation_id
  const card = await askAndGetCard(page)
  await expect(card).toHaveAttribute('data-operation-id', 'publish_center')

  await card.click()

  // ② 真的调了 prepare(不是直接 navigate)
  await expect.poll(() => rec.prepareCalls.length, { timeout: 20_000 }).toBeGreaterThan(0)
  expect(rec.prepareCalls[0]).toBe('/api/xiaobang/operations/publish_center/prepare')

  // ③ URL 上出现 opaque intent,且**只有**它(§12.2)
  await expect.poll(() => new URL(page.url()).searchParams.get('xiaobang_intent'),
                    { timeout: 20_000 }).toBe(PREFILL_INTENT_ID)
  expect(new URL(page.url()).pathname).toBe('/publish')
  expect(page.url()).not.toMatch(/甲品牌|brand_name|cost|算力/)

  // ④ 目标页真的把预填渲染出来了 —— 不是"跳到了就算"
  await expect.poll(() => rec.prefillCalls.length, { timeout: 20_000 }).toBeGreaterThan(0)
  await expect(page.getByTestId('xiaobang-prefill-strip')).toBeVisible()
  await expect(page.getByTestId('xiaobang-prefill-customer')).toContainText('甲品牌')
  // 表也被真填了(WO-B 的消费方仍在工作)
  await expect.poll(() => new URL(page.url()).searchParams.get('geo_post_id'),
                    { timeout: 20_000 }).toBe('5150')
})

test('🔁 反向对照:没有 operation_id 的卡片不 prepare,裸跳转(老行为)', async ({ page }) => {
  const rec: Recorded = { prepareCalls: [], prepareBodies: [], prefillCalls: [] }
  await installProducerBackend(page, rec, { operationId: null, route: '/pricing' })
  await page.goto('/dashboard/today')

  const card = await askAndGetCard(page)
  await expect(card).toHaveAttribute('data-operation-id', '')
  await card.click()

  await expect.poll(() => new URL(page.url()).pathname, { timeout: 20_000 }).toBe('/pricing')
  // 等一会儿再断言"没调过" —— 立刻断言对"还没来得及调"也成立
  await page.waitForTimeout(1500)
  expect(rec.prepareCalls).toEqual([])
  expect(new URL(page.url()).searchParams.get('xiaobang_intent')).toBeNull()
})

test('🔁 反向对照:prepare 失败也要让用户去得了那一页(降级裸跳,不拦路)', async ({ page }) => {
  const rec: Recorded = { prepareCalls: [], prepareBodies: [], prefillCalls: [] }
  await installProducerBackend(page, rec, { operationId: 'publish_center', route: '/publish' })
  // prepare 打 500:预填是锦上添花,不能因为它没准备好就不让用户去
  await page.route('**/api/xiaobang/operations/*/prepare', (route) =>
    route.fulfill({ status: 500, contentType: 'application/json', body: '{}' }))
  await page.goto('/dashboard/today')

  const card = await askAndGetCard(page)
  await card.click()
  await expect.poll(() => new URL(page.url()).pathname, { timeout: 20_000 }).toBe('/publish')
  expect(new URL(page.url()).searchParams.get('xiaobang_intent')).toBeNull()
})

test('目标路由用服务端返回的那个,不是卡片上的 route', async ({ page }) => {
  // 🔴 §12.1:路由由版本化操作地图签发,前端不拼、不猜、不兜底旧路径。
  //    卡片说 /publish,服务端 prepare 说 /marketing-materials —— 必须听服务端的。
  const rec: Recorded = { prepareCalls: [], prepareBodies: [], prefillCalls: [] }
  await installProducerBackend(page, rec, {
    operationId: 'geo_content_center', route: '/publish',
    targetRoute: '/marketing-materials',
  })
  await page.goto('/dashboard/today')

  const card = await askAndGetCard(page)
  await card.click()
  await expect.poll(() => new URL(page.url()).pathname, { timeout: 20_000 })
    .toBe('/marketing-materials')
  expect(new URL(page.url()).searchParams.get('xiaobang_intent')).toBe(PREFILL_INTENT_ID)
})
