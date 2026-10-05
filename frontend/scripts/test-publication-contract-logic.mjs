/**
 * test-publication-contract-logic.mjs — §13 合同渲染器纯逻辑的**可执行**断言。
 *
 * 仓库没有前端单测框架(无 vitest/jest),这里用 esbuild 现场把
 * src/components/publishing/pendingUserActionsLogic.ts 转成 JS 再 import,
 * 真的跑一遍行为 —— 与 verify-publication-contract-ui.mjs(静态门禁)互补:
 * 那个管"代码里不许有什么",这个管"喂进真数据出来对不对"。
 *
 * 喂的两份合同是从生产真实数据与后端构造函数原样抄下来的两个不同 reject_code,
 * 用来证明**一套逻辑通吃两条出口**;第三份是残缺样本,证明不会渲染出空白。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const root = process.cwd()
const src = path.join(root, 'src/components/publishing/pendingUserActionsLogic.ts')
const outDir = path.join(root, 'node_modules/.cache/contract-logic-test')
fs.mkdirSync(outDir, { recursive: true })
const outFile = path.join(outDir, 'logic.mjs')

esbuild.buildSync({
  entryPoints: [src],
  outfile: outFile,
  format: 'esm',
  platform: 'node',
  // 只有 type-only import(@/contracts/governanceAlert),转译后会被抹掉,无需 alias
  bundle: false,
  loader: { '.ts': 'ts' },
})

const logic = await import(pathToFileURL(outFile).href)

let failed = 0
const eq = (actual, expected, what) => {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a !== e) { console.error(`❌ ${what}\n   期望 ${e}\n   实际 ${a}`); failed++ }
  else console.log(`✅ ${what}`)
}
const ok = (cond, what) => eq(Boolean(cond), true, what)

// ---------------------------------------------------------------- 两条出口的真合同

const STUCK = {
  reject_code: 'PUBLISH_AWAITING_SYNC_UNRESOLVED',
  reject_user_message: '平台一直没拿到发布回执。',
  reject_contract: {
    code: 'PUBLISH_AWAITING_SYNC_UNRESOLVED',
    message: '这条发到「咸宁新闻网」的稿件, 平台一直没拿到发布回执, 需要你确认一下。',
    reason: '媒体方当时回执了「已接收」, 但没有返回可追踪的订单号。',
    impact: '这条发布任务停在这里, 没有再重复提交、也没有再扣新的费用。',
    repair_hint: '没找到就点「确认未发布 · 退还算力」。',
    rule_version: 'publish-awaiting-sync-exit-v1',
    actions: [
      { id: 'report_not_published', type: 'api', method: 'POST', label: '确认未发布 · 退还算力', target: '/api/meijiehezi/items/339/report-not-published' },
      { id: 'record_publish_url', type: 'api', method: 'POST', label: '已发布 · 补录链接', target: '/api/meijiehezi/items/339/record-publish-url', fields: [{ name: 'publish_url', type: 'url', label: '稿件链接', required: true }] },
      { id: 'view_orders', type: 'nav', label: '查看发布任务', target: '/publishing' },
    ],
  },
}

const DRIFT = {
  reject_code: 'PUBLISH_CONTENT_DRIFT',
  reject_user_message: '系统在准备期间优化了这篇稿件。',
  reject_contract: {
    code: 'PUBLISH_CONTENT_DRIFT',
    message: '系统在准备期间优化了这篇稿件, 需要重新确认一次即可发布。',
    reason: '稿件的正文与下单时冻结的版本不一致。',
    impact: '本次没有发布, 也没有扣新的费用。',
    repair_hint: '点「重新准备并审核」。',
    rule_version: 'publish-content-drift-v1',
    actions: [
      { id: 'reprepare_and_review', type: 'api', method: 'POST', label: '重新准备并审核', target: '/api/meijiehezi/orders/351/re-prepare' },
      { id: 'view_article', type: 'nav', label: '先看看稿件', target: '/writing?article_id=1204' },
    ],
  },
}

// 验收①:两份不同 reject_code 的合同喂同一套逻辑,动作原样保留、不被改写、不被补兜底
for (const [name, item] of [['代发卡单', STUCK], ['内容漂移', DRIFT]]) {
  const c = logic.normalizeContract(item)
  eq(c.code, item.reject_contract.code, `${name}:code 原样透传`)
  eq(c.actions.map(a => a.id), item.reject_contract.actions.map(a => a.id), `${name}:动作原样保留(未被补兜底/未被裁剪)`)
  ok(c.message && c.reason && c.impact && c.repair_hint, `${name}:四个人话字段都在`)
  ok(c.actions.filter(a => a.type === 'api').length >= 1, `${name}:至少一个可执行动作`)
}

// 验收④:合同残缺 / 动作全是认不出的 type → 仍有人话 + 兜底出口,绝不空白
{
  const weird = logic.normalizeContract({
    reject_code: 'PUBLISH_UNKNOWN_FUTURE_CODE',
    reject_user_message: '这条发布任务遇到了平台还没归类的情况。',
    reject_contract: { code: 'X', message: '还没归类的情况。', reason: '', impact: '', actions: [{ id: 'weird', type: 'telepathy', label: '心灵感应' }] },
  })
  ok(weird.message.length > 0, '未知动作类型:仍渲染出 message')
  ok(weird.actions.some(a => a.id === 'contact_support'), '未知动作类型:补出了「联系客服」兜底')

  const empty = logic.normalizeContract({ reject_code: 'C', reject_user_message: '需要你确认一下。', reject_contract: null })
  eq(empty.message, '需要你确认一下。', '合同缺失:回落到后端写好的人话')
  ok(empty.actions.some(a => a.id === 'contact_support'), '合同缺失:仍有下一步')

  const blank = logic.normalizeContract({ reject_code: null, reject_user_message: null, reject_contract: null })
  ok(blank.message.length > 0, '什么都没有:仍不渲染空白')
  ok(blank.actions.length > 0, '什么都没有:仍有下一步')
}

// 验收②:带 fields 的动作 —— 必填校验 + url 前缀 + 提交体键来自 fields[].name
{
  const field = STUCK.reject_contract.actions[1].fields[0]
  ok(logic.fieldIsMissing(field, '') !== '', '必填为空 → 拦下')
  ok(logic.fieldIsMissing(field, 'www.example.com') !== '', 'url 缺 http(s) 前缀 → 拦下')
  eq(logic.fieldIsMissing(field, 'https://a.com/x'), '', '合法 https 链接 → 放行')
  eq(logic.fieldIsMissing(field, 'http://a.com/x'), '', '合法 http 链接 → 放行')
  ok(logic.fieldIsMissing(field, 'https://a.com/' + 'x'.repeat(2100)) !== '', '超 2000 字 → 拦下(与后端 400 同判据)')

  const body = logic.buildSubmitBody(STUCK.reject_contract.actions[1].fields, { publish_url: '  https://a.com/x  ', note: ' 补充 ' })
  eq(body, { note: '补充', publish_url: 'https://a.com/x' }, '提交体 = {publish_url, note} 且去空白')

  const noFields = logic.buildSubmitBody([], {})
  eq(noFields, { note: '' }, '无 fields 的动作提交体只有 note')
}

// 验收③:target 驱动 —— 换掉 mock 里的 target,规整后的动作就指向新路径
{
  const moved = JSON.parse(JSON.stringify(STUCK))
  moved.reject_contract.actions[0].target = '/api/meijiehezi/items/339/some-new-path'
  const c = logic.normalizeContract(moved)
  eq(c.actions[0].target, '/api/meijiehezi/items/339/some-new-path', 'target 由合同驱动(改 mock 即改请求路径)')
}

// 验收⑥:409/404/400 各有收场,且只有前两者要刷新列表
{
  eq(logic.messageForStatus(409, ''), { text: '这条已经处理过了，列表帮你刷新了一下。', stale: true }, '409 → 提示 + 刷新')
  eq(logic.messageForStatus(404, ''), { text: '这条发布任务已经不在待确认里了。', stale: true }, '404 → 提示 + 刷新(后端故意反枚举,不当 bug)')
  eq(logic.messageForStatus(400, '链接要以 http:// 开头'), { text: '链接要以 http:// 开头', stale: false }, '400 → 显示后端 detail,不刷新')
  eq(logic.messageForStatus(500, ''), { text: '操作没成功，请稍后重试。', stale: false }, '其他状态 → 通用兜底')
}

// isExecutable:只有 api/nav 且带 target 才算出口
{
  ok(logic.isExecutable({ id: 'a', label: 'a', type: 'api', target: '/x' }), 'api + target = 出口')
  ok(logic.isExecutable({ id: 'a', label: 'a', type: 'nav', target: '/x' }), 'nav + target = 出口')
  ok(!logic.isExecutable({ id: 'a', label: 'a', type: 'api' }), 'api 缺 target ≠ 出口')
  ok(!logic.isExecutable({ id: 'a', label: 'a', type: 'telepathy', target: '/x' }), '未知 type ≠ 出口')
  ok(!logic.isExecutable(null), 'null ≠ 出口')
}

if (failed > 0) {
  console.error(`\n❌ §13 合同逻辑断言失败 ${failed} 条`)
  process.exit(1)
}
console.log('\n✅ §13 合同逻辑断言全部通过')
