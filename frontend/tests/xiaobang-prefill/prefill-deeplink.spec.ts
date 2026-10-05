/**
 * XO-03 浏览器判据:业务页面解析 `xiaobang_intent` → 调预填端点 → **真实填表**。
 *
 * 🔴 为什么单独一份 config,不塞进 `playwright.xiaobang-unified.config.ts`:
 *    那份配置的用例总数被 `test_fab_accessibility.py` 钉死在 144 / 156,
 *    往里加 spec 会把那两条判据打红 —— 而它们钉的是**别的东西**(FAB 矩阵),
 *    不该被本轮改动牵连。分开跑,两个分母各自独立。
 *
 * 判据形状(三条,缺一条就能假绿):
 *   ① 页面**真的发了**预填请求(拦到请求 + URL 里是 opaque xint_,没有业务含义);
 *   ② 服务端给的字段**真的渲染出来了**(客户/对象/算力/原因/主按钮文案);
 *   ③ 表**真的被填了** —— 不是"条出现了就算填了"。这里打的是既有参数链:
 *      URL 上出现 brand_id / article_id,以及页面上那篇文章处于选中态。
 */
import { expect, test } from 'playwright/test'
import { installPrefillBackend, PREFILL_INTENT_ID, PREFILL_VARIANTS } from './prefill-harness'

test('deep link 带 xiaobang_intent → 调预填端点 → 字段上屏 + 表被真填', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls)

  await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)

  // ① 真的发了请求,且 URL 里只有 opaque id
  await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)
  expect(prefillCalls[0]).toContain(`/api/xiaobang/operations/intents/${PREFILL_INTENT_ID}/prefill`)
  // §12.2:URL 不放客户名、供应商、算力公式
  expect(prefillCalls[0]).not.toMatch(/甲品牌|brand_name|cost|算力/)

  // ② 服务端字段真的上屏
  const strip = page.getByTestId('xiaobang-prefill-strip')
  await expect(strip).toBeVisible()
  await expect(page.getByTestId('xiaobang-prefill-customer')).toContainText('甲品牌')
  await expect(page.getByTestId('xiaobang-prefill-object')).toContainText('文章 #4201')
  await expect(page.getByTestId('xiaobang-prefill-compute')).toContainText('390')
  await expect(page.getByTestId('xiaobang-prefill-reasons')).toContainText('还没被推荐过')
  // 🔴 主按钮文案来自服务端,前端一个字不拼
  await expect(page.getByTestId('xiaobang-prefill-primary-label'))
    .toHaveText('确认并执行 · 使用 390 算力')

  // ③ 表真的被填了 —— 打既有参数链 + 选中态,不是"条出现了就算填了"
  await expect.poll(() => new URL(page.url()).searchParams.get('brand_id'), { timeout: 15_000 })
    .toBe('101')
  expect(new URL(page.url()).searchParams.get('article_id')).toBe('4201')
  // intent 仍留在 URL 上:刷新/后退要能按 §12.2 恢复
  expect(new URL(page.url()).searchParams.get('xiaobang_intent')).toBe(PREFILL_INTENT_ID)
})

test('🔁 反向对照:没有 xiaobang_intent 时既不发请求也不显示预填条', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls)

  await page.goto('/publish')
  await expect(page.getByTestId('xiaobang-prefill-strip')).toHaveCount(0)
  // 等一会儿再断言"没发过" —— 立刻断言的话它对"还没来得及发"也成立。
  await page.waitForTimeout(1500)
  expect(prefillCalls).toEqual([])
})

test('🔁 反向对照:intent 过期返回 404 → 显示"重新准备"而不是空白页', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { prefillStatus: 404 })

  await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
  await expect(page.getByTestId('xiaobang-prefill-error')).toBeVisible()
  await expect(page.getByTestId('xiaobang-prefill-error')).toContainText('重新准备')
  await expect(page.getByTestId('xiaobang-prefill-strip')).toHaveCount(0)
  expect(prefillCalls.length).toBeGreaterThan(0)   // 请求发了,是服务端说过期
})

test('🔁 反向对照:形状不对的 intent 参数一律不发请求(不给猜 id 的反馈通道)', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls)

  await page.goto('/publish?xiaobang_intent=not-an-opaque-id')
  await page.waitForTimeout(1500)
  expect(prefillCalls).toEqual([])
  await expect(page.getByTestId('xiaobang-prefill-strip')).toHaveCount(0)
})

test('已开始执行的 intent:页面不再出第二个确认区(§12.1 唯一 CTA)', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { intentState: 'execution_linked' })

  await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
  await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)
  // 预填仍取到了(所以不是"请求没发"),但确认区不渲染。
  await expect(page.getByTestId('xiaobang-prefill-strip')).toHaveCount(0)
})

// ══════════════════════════════════════════════════════════════════════════
// [R3-P11 ①] 三对象矩阵 —— 上一轮只有文章 #4201 = 单对象分母
// ══════════════════════════════════════════════════════════════════════════

/**
 * 🔴 单对象分母的失效方式是沉默的:geo 图文与报价这两条预填链**完全没接线**时,
 * 只测文章的判据照样全绿(它压根没打到那两个对象)。
 *
 * 每个变体各打三件事:对象上屏 / URL 参数被真填 / 渠道那一栏有字。
 */
for (const variant of ['article', 'geo_post', 'quote'] as const) {
  test(`三对象矩阵 · ${variant}:对象上屏 + URL 参数被真填`, async ({ page }) => {
    const shape = PREFILL_VARIANTS[variant]
    const prefillCalls: string[] = []
    await installPrefillBackend(page, prefillCalls, { variant })

    await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
    await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)

    // ① 对象真的上屏(不是"条出现了"就算)
    await expect(page.getByTestId('xiaobang-prefill-object'))
      .toContainText(shape.objectLabel.split(' · ')[1])

    // ② 这个变体该落的参数**逐个**落进 URL —— 分母来自变体表,不是我记得几个
    const keys = Object.keys(shape.expectedParams)
    expect(keys.length).toBeGreaterThan(0)          // 空分母自证
    for (const key of keys) {
      await expect
        .poll(() => new URL(page.url()).searchParams.get(key), { timeout: 15_000 })
        .toBe(shape.expectedParams[key])
    }
    // intent 仍在 URL 上(§12.2 刷新恢复)
    expect(new URL(page.url()).searchParams.get('xiaobang_intent')).toBe(PREFILL_INTENT_ID)
  })
}

// ══════════════════════════════════════════════════════════════════════════
// [R3-P11 ②] 目标渠道/账号 —— 三档文案逐字钉死
// ══════════════════════════════════════════════════════════════════════════

/**
 * 🔴 工单原话:「channel 缺失时的降级文案也要钉(别让 undefined 渲染成空串假绿)」。
 *
 * 所以这里**不**用 `toBeVisible()` 了事 —— 一个渲染成空串的 `<dd>` 同样"可见"。
 * 三档各自断言**实际文字**,并且额外断言这一栏非空。
 */
const CHANNEL_EXPECTATIONS: Record<string, string> = {
  // 选过渠道项 + 资格未核
  geo_post: '图文 · douyin_main · 账号资格还没核',
  // 只知道载体类型
  article: '文章 · 具体账号在下一步选 · 账号资格还没核',
  // 什么都没有 —— 降级档
  quote: '还没选目标渠道',
}

for (const variant of ['article', 'geo_post', 'quote'] as const) {
  test(`目标渠道/账号 · ${variant} 档文案逐字钉死`, async ({ page }) => {
    const prefillCalls: string[] = []
    await installPrefillBackend(page, prefillCalls, { variant })
    await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
    await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)

    const cell = page.getByTestId('xiaobang-prefill-channel')
    await expect(cell).toBeVisible()
    await expect(cell).toHaveText(CHANNEL_EXPECTATIONS[variant])
    // 🔴 空串/undefined 防线:上面 toHaveText 已经钉了字,这一条防的是
    //    "以后有人把文案改空" —— 空串仍然 toBeVisible,但长度会掉。
    const text = (await cell.textContent()) || ''
    expect(text.trim().length).toBeGreaterThan(3)
    expect(text).not.toContain('undefined')
    expect(text).not.toContain('null')
  })
}

test('目标渠道/账号:这一栏在主按钮上方(§12.3 同屏可见)', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { variant: 'geo_post' })
  await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
  await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)

  const channelBox = await page.getByTestId('xiaobang-prefill-channel').boundingBox()
  const primaryBox = await page.getByTestId('xiaobang-prefill-primary-label').boundingBox()
  expect(channelBox).not.toBeNull()
  expect(primaryBox).not.toBeNull()
  // "上方" = 渠道那一栏的顶边在主按钮文案顶边之上。规格要的是同屏可见且在其上方,
  // 不是"页面上某处有这个字"。
  expect(channelBox!.y).toBeLessThan(primaryBox!.y)
})
