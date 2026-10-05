/**
 * WO-B ① 浏览器判据:**另外两页**也真的解析 intent(规格 §12.3)。
 *
 * §12.3 原话:
 *   · PublishCenter 已有部分 brand/quote/article/geo post 选择能力;
 *   · WritingHall 已能接收 quote;
 *   · **GEO 图文页需补真实 intent 解析,不能继续打开空白默认表单。**
 *
 * R3-P11 只接了 PublishCenter 一处(那一单 §未做 ② 自己写着)。本文件把另外两页
 * 的判据补上,形状与 `prefill-deeplink.spec.ts` 一致:
 *
 *   ① 页面**真的发了**预填请求(URL 里只有 opaque `xint_`);
 *   ② 服务端字段**真的渲染出来了**(客户/对象/算力/渠道那一栏);
 *   ③ 表**真的被填了** —— 打的是各页**既有**的参数链,不是"条出现了就算填了";
 *   ④ 反向对照:**没有** intent 时既不发请求也不显示预填条(空 intent 降级)。
 *
 * 🔴 ④ 不是凑数:三页共用同一个 hook 之后,「无 intent 也发请求」这种回归会
 *    一次坏三页。每页各打一次,才知道是哪一页坏了。
 */
import { expect, test } from 'playwright/test'
import { installPrefillBackend, prefillFixture, PREFILL_INTENT_ID, PREFILL_VARIANTS } from './prefill-harness'

/** 页面 → (路由, 该页要落的参数, 用哪个对象变体)。分母写成表,加页面时不会漏判据。 */
const PAGES = [
  {
    name: 'WritingHall(/writing 壳)',
    route: '/writing',
    variant: 'quote' as const,
    // 写作侧接的是报价 —— §12.3「WritingHall 已能接收 quote」
    params: ['brand_id', 'quote_id'],
  },
  {
    name: 'GEO 图文页(/marketing-materials)',
    route: '/marketing-materials',
    variant: 'geo_post' as const,
    params: ['brand_id', 'geo_post_id'],
  },
]

for (const page_ of PAGES) {
  test(`${page_.name}:深链 → 调预填端点 → 字段上屏 + 参数被真填`, async ({ page }) => {
    const prefillCalls: string[] = []
    await installPrefillBackend(page, prefillCalls, { variant: page_.variant })

    await page.goto(`${page_.route}?xiaobang_intent=${PREFILL_INTENT_ID}`)

    // ① 真的发了请求,且 URL 里只有 opaque id(§12.2:不放客户名/供应商/算力公式)
    await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)
    expect(prefillCalls[0]).toContain(
      `/api/xiaobang/operations/intents/${PREFILL_INTENT_ID}/prefill`)
    expect(prefillCalls[0]).not.toMatch(/甲品牌|brand_name|cost|算力/)

    // ② 服务端字段真的上屏(不是"页面没崩"就算)
    const strip = page.getByTestId('xiaobang-prefill-strip')
    await expect(strip).toBeVisible()
    await expect(page.getByTestId('xiaobang-prefill-customer')).toContainText('甲品牌')
    const shape = PREFILL_VARIANTS[page_.variant]
    await expect(page.getByTestId('xiaobang-prefill-object'))
      .toContainText(shape.objectLabel.split(' · ')[1])
    // 渠道那一栏在**每一页**都必须有字(缺它就是 §12.3 的同屏清单少一项)
    const channel = page.getByTestId('xiaobang-prefill-channel')
    await expect(channel).toBeVisible()
    expect(((await channel.textContent()) || '').trim().length).toBeGreaterThan(3)

    // ③ 表真的被填了 —— 逐个参数断言,分母来自上面那张表(不是我记得几个)
    expect(page_.params.length).toBeGreaterThan(0)          // 空分母自证
    for (const key of page_.params) {
      await expect
        .poll(() => new URL(page.url()).searchParams.get(key), { timeout: 15_000 })
        .toBe(shape.expectedParams[key])
    }
    // intent 留在 URL 上:刷新/后退要能按 §12.2 恢复
    expect(new URL(page.url()).searchParams.get('xiaobang_intent')).toBe(PREFILL_INTENT_ID)
  })

  test(`🔁 ${page_.name}:没有 intent 时既不发请求也不显示预填条`, async ({ page }) => {
    const prefillCalls: string[] = []
    await installPrefillBackend(page, prefillCalls, { variant: page_.variant })

    await page.goto(page_.route)
    await expect(page.getByTestId('xiaobang-prefill-strip')).toHaveCount(0)
    // 等一会儿再断言"没发过" —— 立刻断言的话它对"还没来得及发"也成立。
    await page.waitForTimeout(1500)
    expect(prefillCalls).toEqual([])
  })

  test(`🔁 ${page_.name}:intent 过期 → 显示"重新准备"而不是空白页`, async ({ page }) => {
    const prefillCalls: string[] = []
    await installPrefillBackend(page, prefillCalls,
                               { variant: page_.variant, prefillStatus: 404 })

    await page.goto(`${page_.route}?xiaobang_intent=${PREFILL_INTENT_ID}`)
    await expect(page.getByTestId('xiaobang-prefill-error')).toBeVisible()
    await expect(page.getByTestId('xiaobang-prefill-error')).toContainText('重新准备')
    await expect(page.getByTestId('xiaobang-prefill-strip')).toHaveCount(0)
    expect(prefillCalls.length).toBeGreaterThan(0)   // 请求发了,是服务端说过期
  })
}

test('GEO 图文对象从深链进来时落在图文 tab,不是默认的写文章 tab', async ({ page }) => {
  // 🔴 落错 tab 与「打开空白默认表单」在用户眼里没有区别 —— 他要的那一页仍然没出现。
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { variant: 'geo_post' })

  await page.goto(`/writing?xiaobang_intent=${PREFILL_INTENT_ID}`)
  await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)
  // 断言 aria-selected(可访问性契约),抓手用 testid ——
  // 两个 trigger 的可访问名由文本 + Badge 拼出,快照里算不出稳定 name。
  await expect(page.getByTestId('writing-tab-douyin'))
    .toHaveAttribute('aria-selected', 'true', { timeout: 15_000 })
  await expect(page.getByTestId('writing-tab-article'))
    .toHaveAttribute('aria-selected', 'false')
})

test('🔁 反向对照:报价对象不会把 tab 切到图文', async ({ page }) => {
  // 没有这一条,上面那条可能只是因为"这一页永远默认图文 tab"。
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { variant: 'quote' })

  await page.goto(`/writing?xiaobang_intent=${PREFILL_INTENT_ID}`)
  await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)
  await page.waitForTimeout(1000)
  await expect(page.getByTestId('writing-tab-article'))
    .toHaveAttribute('aria-selected', 'true')
  await expect(page.getByTestId('writing-tab-douyin'))
    .toHaveAttribute('aria-selected', 'false')
})

// ══════════════════════════════════════════════════════════════════════════
// [WO-B ③] 渠道**已核**那一档 —— ③ 之前 verified 恒 false,这条路一个判据都没有
// ══════════════════════════════════════════════════════════════════════════

test('目标渠道/账号 · 已核但不可用:说出服务端给的结论,不再说"还没核"', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { variant: 'geo_post_verified' })

  await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
  await expect.poll(() => prefillCalls.length, { timeout: 15_000 }).toBeGreaterThan(0)

  const cell = page.getByTestId('xiaobang-prefill-channel')
  await expect(cell).toHaveText('图文 · svideo:12 · 这个账号发不了图文,换一个能发图文的')
  // 🔴 成对:核过之后**不许**再显示"还没核" —— 那是把一个已知结论说成未知。
  await expect(cell).not.toContainText('账号资格还没核')
  const text = (await cell.textContent()) || ''
  expect(text).not.toContain('undefined')
  expect(text).not.toContain('null')
})

// ══════════════════════════════════════════════════════════════════════════
// [微单 2026-08-20] `primary_action.enabled=false` —— 不许死按钮
// ══════════════════════════════════════════════════════════════════════════

test('执行链没开放时:主按钮那一行视觉塌下去 + 把原因说出来', async ({ page }) => {
  // 🔴 服务端对 executable=False 的 operation 返回 enabled=false + note。
  //    前端还渲染成一行自信的「确认并执行 · 使用 X 算力」= 死按钮换了个材质。
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { variant: 'geo_post' })
  // 覆盖预填响应:只改主按钮那一格,其余保持真实形状
  await page.route('**/api/xiaobang/operations/intents/*/prefill', async (route) => {
    const body = prefillFixture('prepared', 'geo_post')
    body.primary_action = {
      next_action_id: 'open_page',
      label: '在这一页继续做',
      enabled: false,
      note: '这一步暂时不能在小榜里直接执行,已经准备好的内容都在,不会重复。',
    }
    await route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify(body),
    })
  })

  await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
  const label = page.getByTestId('xiaobang-prefill-primary-label')
  await expect(label).toBeVisible()
  await expect(label).toHaveAttribute('data-enabled', 'false')
  await expect(label).toHaveAttribute('aria-disabled', 'true')
  // 🔴 承诺必须消失:不许再出现「确认并执行」或算力数字
  await expect(label).not.toContainText('确认并执行')
  await expect(label).not.toContainText('算力')
  // 原因说出来,而且不吓人
  await expect(page.getByTestId('xiaobang-prefill-primary-note')).toContainText('不会重复')
})

test('🔁 反向对照:执行链开放时那一行仍是正常的确认文案', async ({ page }) => {
  const prefillCalls: string[] = []
  await installPrefillBackend(page, prefillCalls, { variant: 'geo_post' })
  await page.goto(`/publish?xiaobang_intent=${PREFILL_INTENT_ID}`)
  const label = page.getByTestId('xiaobang-prefill-primary-label')
  await expect(label).toHaveAttribute('data-enabled', 'true')
  await expect(label).toContainText('确认并执行')
  await expect(page.getByTestId('xiaobang-prefill-primary-note')).toHaveCount(0)
})
