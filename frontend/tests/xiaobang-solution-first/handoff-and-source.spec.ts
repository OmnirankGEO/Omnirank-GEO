/**
 * 包 D①⑥ · 两个「后端发了、前端扔了 / 拼错了」的接线缺口。
 *
 * ① `handoff` / `should_escalate`(工单 §4 P1-4)
 *    后端低置信兜底那支**一直在发** `handoff: true`,正文里也写着
 *    「点下方把问题反馈给工作人员」—— 而 `XiaobangMeta` 与 SSE 解析器两处
 *    都不保留它。用户看到「点下方」,下方什么都没有。
 *
 * ② SourceCard 的 sys 来源链接(工单 §4 P1-7)
 *    系统知识库 chunk 用**路由本身**当 `source_slug`
 *    (`tools/xiaobang_system_kb.py`: `"source_slug": route`),
 *    原 `buildDocUrl` 无脑拼成 `/help/docs//monitoring` —— 双斜杠 + 不存在的页。
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

function sse(delta: string, meta: Record<string, unknown>): string {
  return [
    ': ready', '',
    'event: text', `data: ${JSON.stringify({ delta })}`, '',
    'event: meta', `data: ${JSON.stringify(meta)}`, '',
    'event: done', 'data: {}', '',
  ].join('\n')
}

async function drawerWith(page: import('playwright/test').Page,
                          meta: Record<string, unknown>, delta: string) {
  const active: { current: IsolationAccount } = { current: ACCOUNT_A }
  await installIsolationBackend(page, active)
  await page.route('**/api/xiaobang/chat', (route) => route.fulfill({
    status: 200, contentType: 'text/event-stream', body: sse(delta, meta),
  }))
  await seedBrowser(page, TOKEN)
  await page.goto('/dashboard')
  await openAssistant(page)
  await page.locator('textarea').fill('GEO 图文无法使用')
  await page.locator('button[title="发送"]').click()
  await expect(page.getByText(delta).first()).toBeVisible()
}

test('后端下发的 handoff/should_escalate 不再被解析器丢掉', async ({ page }) => {
  // 逐字照后端低置信兜底那支的 meta 形状(api/xiaobang_api.py)
  const meta = { sources: [], confidence: 'low', handoff: true }
  const delta = '我能看到你大概在这个页面，但这块的知识库说明还不够。'
  await drawerWith(page, meta, delta)

  // 从渲染树里把这条 assistant 消息的 meta 取出来验(不依赖某个还没做的按钮长啥样)
  const kept = await page.evaluate(() => {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (!key || !key.startsWith('omnirank_xiaobang_msgs_')) continue
      const raw = localStorage.getItem(key)
      if (!raw) continue
      const msgs = JSON.parse(raw) as Array<{ role: string; meta?: Record<string, unknown> }>
      const last = [...msgs].reverse().find((m) => m.role === 'assistant' && m.meta)
      if (last) return last.meta
    }
    return null
  })

  expect(kept, 'assistant 消息上必须有 meta').toBeTruthy()
  // 必须命中:接管信号留住了
  expect(kept!.handoff, `实得 meta=${JSON.stringify(kept)}`).toBe(true)
  // 必须命中:低置信也留住了(它决定 SourceCard 的降级样式)
  expect(kept!.confidence).toBe('low')
})

test('没有 handoff 时不许凭空变出接管信号', async ({ page }) => {
  const meta = { sources: [], confidence: 'high' }
  const delta = '监测每天跑四个引擎。'
  await drawerWith(page, meta, delta)

  const kept = await page.evaluate(() => {
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i)
      if (!key || !key.startsWith('omnirank_xiaobang_msgs_')) continue
      const raw = localStorage.getItem(key)
      if (!raw) continue
      const msgs = JSON.parse(raw) as Array<{ role: string; meta?: Record<string, unknown> }>
      const last = [...msgs].reverse().find((m) => m.role === 'assistant' && m.meta)
      if (last) return last.meta
    }
    return null
  })
  expect(kept).toBeTruthy()
  expect(kept!.handoff).toBe(false)
  expect(kept!.should_escalate).toBe(false)
})

test('系统知识库来源的链接不再拼成 /help/docs//route', async ({ page }) => {
  // 逐字照 tools/xiaobang_system_kb.py 的 source_slug 形状:路由本身(可带 #锚点)
  const meta = {
    sources: [{
      source_slug: '/monitoring#btn-start',
      source_title: '效果监测',
      section_title: '开始监测',
      source_type: 'doc',
    }],
    confidence: 'high',
  }
  await drawerWith(page, meta, '监测每天跑四个引擎。')

  const hrefs = await page.locator('a[href]').evaluateAll(
    (as) => as.map((a) => a.getAttribute('href') || ''),
  )
  // 必须不命中:双斜杠的假文档地址
  expect(hrefs.filter((h) => h.includes('/help/docs//')),
         `实得 ${JSON.stringify(hrefs)}`).toEqual([])
  // 必须命中:直接指向那个页面路由
  expect(hrefs.some((h) => h.startsWith('/monitoring')),
         `实得 ${JSON.stringify(hrefs)}`).toBe(true)
})

test('普通文档来源仍然走 /help/docs/{slug}(反向对照)', async ({ page }) => {
  const meta = {
    sources: [{
      source_slug: 'wallet', source_title: '我的钱包',
      section_title: '', source_type: 'doc',
    }],
    confidence: 'high',
  }
  await drawerWith(page, meta, '算力在钱包里看。')

  const hrefs = await page.locator('a[href]').evaluateAll(
    (as) => as.map((a) => a.getAttribute('href') || ''),
  )
  expect(hrefs.some((h) => h === '/help/docs/wallet'),
         `实得 ${JSON.stringify(hrefs)}`).toBe(true)
})
