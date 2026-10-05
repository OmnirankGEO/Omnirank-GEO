/**
 * 包 D①③④ · 抽屉内的出口闭环。
 *
 * 三件必须成立的事(工单 §5.3 / §6 包 D③④):
 *   1. 后端签发的 `exits` 渲染成**可点的东西** —— 不再是「点下方」而下方空无一物;
 *   2. 转人工**前**把「会提交什么」摊开给用户看,不悄悄外发;
 *   3. 提交成功后显示**工单 id 与下一步**,不是弹个 toast 就完事。
 *
 * 🔴 提交打的是 `POST /api/faq/feedback` —— **现有那条链路**,没有第二套反馈系统。
 *    判据直接截这个请求,逐字核对它的形状与 `FeedbackPage` 一致。
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

const FALLBACK_TEXT =
  '我能看到你大概在「效果监测」这个页面，但这块的知识库说明还不够，没法给你准确解答。'

/** 逐字照后端 `build_exits(confidence="low", handoff=True)` 的产出形状。 */
const EXITS = [
  {
    exit_id: 'handoff',
    label: '还没解决,提交给工作人员',
    submits: ['你这条问题原文', '当前页面', '最近几轮对话', '我刚才的回答'],
  },
  { exit_id: 'resolved', label: '解决了' },
]

function sse(delta: string, meta: Record<string, unknown>): string {
  return [
    ': ready', '',
    'event: text', `data: ${JSON.stringify({ delta })}`, '',
    'event: meta', `data: ${JSON.stringify(meta)}`, '',
    'event: done', 'data: {}', '',
  ].join('\n')
}

async function drawerWithExits(
  page: import('playwright/test').Page,
  feedbackCalls: Array<Record<string, unknown>>,
  opts: { feedbackStatus?: number; existing?: boolean } = {},
) {
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)

  await page.route('**/api/xiaobang/chat', (route) => route.fulfill({
    status: 200,
    contentType: 'text/event-stream',
    body: sse(FALLBACK_TEXT, {
      sources: [], confidence: 'low', handoff: true, exits: EXITS,
    }),
  }))
  await page.route('**/api/faq/feedback', async (route) => {
    feedbackCalls.push(route.request().postDataJSON() as Record<string, unknown>)
    if (opts.feedbackStatus && opts.feedbackStatus !== 200) {
      return route.fulfill({
        status: opts.feedbackStatus, contentType: 'application/json',
        body: JSON.stringify({ detail: '后端暂时不可用' }),
      })
    }
    return route.fulfill({
      status: 200, contentType: 'application/json',
      body: JSON.stringify({ id: 4321, status: opts.existing ? 'existing' : 'new' }),
    })
  })

  await seedBrowser(page, TOKEN)
  await page.goto('/dashboard')
  await openAssistant(page)
  await page.locator('textarea').fill('GEO 图文无法使用')
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText(FALLBACK_TEXT).first()).toBeVisible()
}

test('后端签发的出口渲染成可点按钮(「点下方」下方真的有东西)', async ({ page }) => {
  await drawerWithExits(page, [])
  await expect(page.getByTestId('xiaobang-exit-bar')).toBeVisible()
  await expect(page.getByTestId('xiaobang-exit-handoff')).toBeVisible()
  await expect(page.getByTestId('xiaobang-exit-resolved')).toBeVisible()
  // 必须不命中:前端不许自己造出服务端没签发的出口
  await expect(page.getByTestId('xiaobang-exit-retry')).toHaveCount(0)
  await expect(page.getByTestId('xiaobang-exit-clarify')).toHaveCount(0)
})

test('转人工前先摊开「会提交什么」,用户确认后才发', async ({ page }) => {
  const calls: Array<Record<string, unknown>> = []
  await drawerWithExits(page, calls)

  await page.getByTestId('xiaobang-exit-handoff').click()
  const preview = page.getByTestId('xiaobang-handoff-preview')
  await expect(preview).toBeVisible()
  for (const item of EXITS[0].submits!) {
    await expect(preview).toContainText(item)
  }
  // 必须不命中:还没确认之前**一个请求都不许发**(不许悄悄外发)
  expect(calls, `未确认就发了:${JSON.stringify(calls)}`).toEqual([])

  await page.getByRole('button', { name: '确认提交' }).click()
  await expect.poll(() => calls.length).toBe(1)

  // 请求形状必须与 FeedbackPage 那条一致
  const body = calls[0]
  expect(body.kind).toBe('bug')
  expect(String(body.client_id)).toMatch(/^bug_\d+_[a-z0-9]+$/)
  expect(String(body.ai_answer)).toContain('知识库说明还不够')
  const message = String(body.message)
  expect(message).toContain('GEO 图文无法使用')
  expect(message).toContain('当前页面')
  expect(message).toContain('最近对话')
})

test('提交成功后显示工单 id 与下一步,而不是只弹一个 toast', async ({ page }) => {
  await drawerWithExits(page, [])
  await page.getByTestId('xiaobang-exit-handoff').click()
  await page.getByRole('button', { name: '确认提交' }).click()

  const result = page.getByTestId('xiaobang-handoff-result')
  await expect(result).toBeVisible()
  await expect(result).toContainText('#4321')
  await expect(result).toContainText('会通知你')
})

test('重放拿到同一张工单时,说清楚是并单不是重复排队', async ({ page }) => {
  await drawerWithExits(page, [], { existing: true })
  await page.getByTestId('xiaobang-exit-handoff').click()
  await page.getByRole('button', { name: '确认提交' }).click()
  await expect(page.getByTestId('xiaobang-handoff-result')).toContainText('并到同一张工单')
})

test('接管 API 挂了:保留用户已填内容 + 给重试,不显示裸错误码', async ({ page }) => {
  // 工单 §8 S15
  const calls: Array<Record<string, unknown>> = []
  await drawerWithExits(page, calls, { feedbackStatus: 503 })

  await page.getByTestId('xiaobang-exit-handoff').click()
  await page.getByRole('button', { name: '确认提交' }).click()

  const err = page.getByTestId('xiaobang-handoff-error')
  await expect(err).toBeVisible()
  await expect(err).toContainText('你填的内容还在')
  // 必须命中:重试入口还在,且「会提交什么」也还摊着(没被清空)
  await expect(page.getByRole('button', { name: '再试一次' })).toBeVisible()
  await expect(page.getByTestId('xiaobang-handoff-preview')).toBeVisible()
  // 必须不命中:没有伪造出一个成功的工单
  await expect(page.getByTestId('xiaobang-handoff-result')).toHaveCount(0)
})

test('点「解决了」只记录闭环,不提交任何工单', async ({ page }) => {
  const calls: Array<Record<string, unknown>> = []
  await drawerWithExits(page, calls)
  await page.getByTestId('xiaobang-exit-resolved').click()
  await expect(page.getByTestId('xiaobang-resolved-ack')).toBeVisible()
  expect(calls, '「解决了」不该产生任何提交').toEqual([])
})

test('没有 exits 的正常回答不渲染出口条(反向对照)', async ({ page }) => {
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)
  await page.route('**/api/xiaobang/chat', (route) => route.fulfill({
    status: 200, contentType: 'text/event-stream',
    body: sse('监测每天跑四个引擎。', { sources: [], confidence: 'high' }),
  }))
  await seedBrowser(page, TOKEN)
  await page.goto('/dashboard')
  await openAssistant(page)
  await page.locator('textarea').fill('监测怎么跑')
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText('监测每天跑四个引擎。').first()).toBeVisible()
  await expect(page.getByTestId('xiaobang-exit-bar')).toHaveCount(0)
})
