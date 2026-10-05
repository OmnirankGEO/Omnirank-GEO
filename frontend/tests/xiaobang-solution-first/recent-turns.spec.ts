/**
 * 包 A③④ · 浏览器这半截的**接线锁**。
 *
 * 🔴 后端那半截(sanitize → messages)已经有真 HTTP 锁,但那锁**看不见浏览器**:
 *    把 `recent_turns: recentTurns.length ? recentTurns : undefined` 这一行从
 *    请求体里删掉,后端 34 条判据一条都不会红 —— 因为后端永远收得到一个合法请求,
 *    只是里面没有历史。用户看到的是「小榜又忘了上文」,而 CI 全绿。
 *
 *    所以这里直接截真实的 `POST /api/xiaobang/chat`,逐项核对请求体。
 */
import { expect, test } from 'playwright/test'
import {
  ACCOUNT_A,
  installIsolationBackend,
  mintProductionShapedToken,
  openAssistant,
  seedBrowser,
  type IsolationAccount,
} from '../xiaobang-session-isolation/isolation-harness'

const TOKEN = mintProductionShapedToken(ACCOUNT_A)

type ChatBody = {
  message?: string
  session_id?: string
  recent_turns?: Array<Record<string, unknown>>
}

test('第二轮请求带上有界最近对话;第一轮不带;历史里只有 role/content', async ({ page }) => {
  const bodies: ChatBody[] = []
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)
  // 装在 mock 后端**之后**:Playwright 后注册的 route 先匹配,这样才截得到。
  await page.route('**/api/xiaobang/chat', async (route) => {
    bodies.push(route.request().postDataJSON() as ChatBody)
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: [
        ': ready', '',
        'event: text', `data: ${JSON.stringify({ delta: ACCOUNT_A.answer })}`, '',
        'event: meta', `data: ${JSON.stringify({ sources: [], confidence: 'high' })}`, '',
        'event: done', 'data: {}', '',
      ].join('\n'),
    })
  })
  await seedBrowser(page, TOKEN)

  await page.goto('/dashboard')
  await openAssistant(page)

  // ── 第一轮 ────────────────────────────────────────────────
  await page.locator('textarea').fill('员工席位怎么分配')
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText(ACCOUNT_A.answer).first()).toBeVisible()

  expect(bodies.length, '第一轮请求必须真的发出去了').toBe(1)
  // 必须不命中:第一轮之前没有任何上文
  expect(bodies[0].recent_turns, '第一轮不该带历史').toBeUndefined()
  expect(bodies[0].message).toBe('员工席位怎么分配')

  // ── 第二轮 ────────────────────────────────────────────────
  await page.locator('textarea').fill('这个按钮点不了')
  await page.locator('button[title="发送"]').click()
  await expect.poll(() => bodies.length).toBe(2)

  const second = bodies[1]
  expect(second.message).toBe('这个按钮点不了')
  const turns = second.recent_turns
  expect(turns, '第二轮必须带上一轮的对话').toBeTruthy()
  expect(turns!.length).toBe(2)

  // 必须命中:上一问 + 上一答,role 正确、正序
  expect(turns![0]).toEqual({ role: 'user', content: '员工席位怎么分配' })
  expect(turns![1]).toEqual({ role: 'assistant', content: ACCOUNT_A.answer })

  // 必须不命中:当前这句不许被重复塞进历史(否则模型会看到两遍)
  expect(turns!.some((t) => t.content === '这个按钮点不了')).toBe(false)

  // 必须不命中:历史条目**只有** role/content 两个键
  // (attachment/meta/isStreaming/timestamp/id 一个都不许进 —— 包 A④)
  for (const t of turns!) {
    expect(Object.keys(t).sort(), `历史条目多带了字段:${JSON.stringify(t)}`)
      .toEqual(['content', 'role'])
  }
})

/**
 * 有界:第 5 轮请求最多带 6 条,且是**最近**的 6 条。
 *
 * ⚠️ 顺带交代一个**没有锁**的地方:`toRecentTurns` 里的 `!m.isStreaming` 过滤
 *    驱动不到 —— 流式期间输入框 disabled、`sendMessage` 也早退
 *    (`if (... || isStreaming) return`),用户根本发不出第二句。
 *    ⇒ 那一条过滤是**纵深防御·非承重**,本套判据不声称验过它。
 */
test('有界:第 5 轮最多带 6 条,且是最近的 6 条', async ({ page }) => {
  const bodies: ChatBody[] = []
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)

  let call = 0
  await page.route('**/api/xiaobang/chat', async (route) => {
    bodies.push(route.request().postDataJSON() as ChatBody)
    call += 1
    await route.fulfill({
      status: 200,
      contentType: 'text/event-stream',
      body: [
        ': ready', '',
        'event: text', `data: ${JSON.stringify({ delta: `答复${call}` })}`, '',
        'event: done', 'data: {}', '',
      ].join('\n'),
    })
  })
  await seedBrowser(page, TOKEN)

  await page.goto('/dashboard')
  await openAssistant(page)

  for (let i = 1; i <= 5; i += 1) {
    await page.locator('textarea').fill(`第${i}问`)
    await page.locator('button[title="发送"]').click()
    await expect(page.getByText(`答复${i}`).first()).toBeVisible()
    await expect.poll(() => bodies.length).toBe(i)
  }

  const turns = bodies[4].recent_turns || []
  // 必须命中:恰好 6 条(4 轮问答 = 8 条,超了要砍到 6)
  expect(turns.length, `实得 ${JSON.stringify(turns)}`).toBe(6)
  // 必须命中:留的是**最近**的 —— 最后一条是第 4 轮的答复
  expect(turns[turns.length - 1]).toEqual({ role: 'assistant', content: '答复4' })
  // 必须不命中:第 1 问已经被挤出去了(否则「留最近」没被验到)
  expect(turns.some((t) => t.content === '第1问'), '第1问该被挤出').toBe(false)
  // 分母自证:确实发生了裁剪(没裁剪的话上面两条是空转)
  expect(turns.length).toBeLessThan(8)
})

/**
 * 补验 §8 弱点 2:`toRecentTurns` 的 `!m.isStreaming` 过滤驱动不到。
 *
 * 我之前只是**声称**「流式期间发不出第二句,所以那条过滤非承重」。
 * 声称不算验过 —— 这条把那个**前提**本身锁住:
 * 流式进行中,发送按钮不可用、输入框 disabled。
 * 哪天有人放开了它(比如做「打断并追问」),这条会红,
 * 那时 `!isStreaming` 过滤就从纵深防御升为承重,必须先补锁。
 */
test('[前提锁] 流式进行中发不出第二句 —— 这是 !isStreaming 过滤非承重的依据', async ({ page }) => {
  const bodies: ChatBody[] = []
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)

  let release: (() => void) | null = null
  const held = new Promise<void>((r) => { release = r })

  await page.route('**/api/xiaobang/chat', async (route) => {
    bodies.push(route.request().postDataJSON() as ChatBody)
    // 挂住不回,让前端停在 isStreaming=true
    await held
    await route.fulfill({
      status: 200, contentType: 'text/event-stream',
      body: [': ready', '', 'event: done', 'data: {}', ''].join('\n'),
    })
  })
  await seedBrowser(page, TOKEN)
  await page.goto('/dashboard')
  await openAssistant(page)

  await page.locator('textarea').fill('第一句')
  await page.locator('button[title="发送"]').click()
  await expect.poll(() => bodies.length).toBe(1)

  // ⚠️ 实测订正:输入框在流式期间**是可以打字的**
  //    (`XiaobangChatInput`: `disabled={disabled && !isStreaming}` —— 刻意允许
  //     用户先把下一句敲好)。所以「发不出去」靠的不是 disabled,
  //    而是 `sendMessage` 开头那句 `if (... || isStreaming) return`。
  //    第一版我断言 textarea disabled,**实跑当场红** —— 前提又写错了一次,已订正。
  //    承重的是下面这条:**敲了、按了回车,也不许产生第二个请求**。
  await expect(page.locator('button[title="停止"]')).toBeVisible()
  await expect(page.locator('button[title="发送"]')).toHaveCount(0)

  await page.locator('textarea').fill('流式期间硬插的第二句')
  await page.locator('textarea').press('Enter')
  await page.waitForTimeout(600)
  expect(bodies.length, `流式期间发出了第二个请求:${JSON.stringify(bodies)}`).toBe(1)

  release?.()
})
