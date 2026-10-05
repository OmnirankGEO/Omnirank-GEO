#!/usr/bin/env node
/**
 * 变异检验 · 报价列表单据日期锁(WO_QUOTE_LIST_DATE 2026-08-05)。
 *
 * 每条变异 = 「有人把这个改回去/改坏」的一种具体写法。锁必须**当场转红**。
 * 存活的先分诊「锁写松了」还是「变异是空操作」,两者修法不同。
 *
 * 跑法:node scripts/mutation_runner_quote_order_date.mjs
 *
 * 自坏防线:
 *   · 锚点在源文件里出现次数 ≠ 1 → ANCHOR_BAD 并整体失败(抓不到 = 变异没落盘);
 *   · 写回用原始 buffer 逐字还原,不经任何行尾转换(CRLF 翻转会让后续锚点全失配);
 *   · 先跑一次基线,不绿就 abort(基线红时变异结果无意义)。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { spawnSync } from 'node:child_process'
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs'

const root = process.cwd()
const LOCK = 'scripts/test-quote-order-date.mjs'
const LIB = 'src/lib/quoteOrderDate.ts'
const PAGE = 'src/pages/Quote/OnlineQuoteFlow.tsx'

/** [编号, 说明, 文件, 锚点, 替换] */
const MUTATIONS = [
  ['M01', '口径回落到 updated_at(= 把工单要修的东西加回来)', LIB,
   `  const created = (session.created_at || '').trim();\n  if (created) return created;\n  return null;`,
   `  const created = (session.created_at || '').trim();\n  if (created) return created;\n  return ((session).updated_at || '').trim() || null;`],

  ['M02', '主显改成建单时间(提交词时间不再优先 · 会话 217 形态失真)', LIB,
   `  const submitted = (session.keywords_submitted_at || '').trim();\n  if (submitted) return submitted;`,
   `  const submitted = '';\n  if (submitted) return submitted;`],

  ['M03', '烂日期串不再挡(渲染出 Invalid Date)', LIB,
   `  if (Number.isNaN(d.getTime())) return '';`,
   `  if (false) return '';`],

  ['M04', '排序键退回 0 恒定(列表顺序与显示脱节)', LIB,
   `  const t = new Date(iso).getTime();\n  return Number.isNaN(t) ? 0 : t;`,
   `  return 0;`],

  ['M05', '空入参不再兜(锁里那条"不抛"变成真抛)', LIB,
   `  if (!session) return null;`,
   `  if (false) return null;`],

  ['M06', '列表卡片日期改回 updated_at(函数对、接线缺)', PAGE,
   `                        {formatQuoteOrderDate(s)}`,
   `                        {(s).updated_at ? new Date((s).updated_at).toLocaleDateString('zh-CN') : ''}`],

  ['M07', '详情头日期改回 updated_at', PAGE,
   `          {formatQuoteOrderDate(selectedSession) && (\n            <span>{formatQuoteOrderDate(selectedSession)}</span>\n          )}`,
   `          {(selectedSession).updated_at && <span>{new Date((selectedSession).updated_at).toLocaleDateString('zh-CN')}</span>}`],

  ['M08', '排序改回 updated_at(显示与排序脱节)', PAGE,
   `        (a, b) => quoteOrderDateSortKey(b) - quoteOrderDateSortKey(a),`,
   `        (a, b) => new Date((b).updated_at || 0).getTime() - new Date((a).updated_at || 0).getTime(),`],

  ['M09', 'Session 类型把 updated_at 放回去(闸门重新打开)', PAGE,
   `  keywords_submitted_at?: string;`,
   `  updated_at?: string;\n  keywords_submitted_at?: string;`],

  ['M10', 'limit 退回 50(截断集按 updated_at 选、页面按业务时间排 = 第一页 40% 错)', PAGE,
   `        api.get('/api/keyword-selection/list', { params: { limit: 500 } }),`,
   `        api.get('/api/keyword-selection/list', { params: { limit: 50 } }),`],
]

function runLock() {
  const r = spawnSync(process.execPath, [LOCK], { cwd: root, encoding: 'utf8' })
  return r.status === 0 ? 'SURVIVED' : 'KILLED'
}

console.log('── 基线自检:未变异时锁必须全绿 ──')
if (runLock() !== 'SURVIVED') {
  console.error('[ERR ] 基线就不是绿的 → 变异结果无意义,先修基线')
  process.exit(2)
}
console.log('   OK 基线全绿\n')

let killed = 0
let bad = 0
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(path.join(root, MUTATIONS[0][2]), console.log);

for (const [code, note, rel, anchor, replacement] of MUTATIONS) {
  const file = path.join(root, rel)
  const original = fs.readFileSync(file)          // Buffer,逐字还原用
  const text = original.toString('utf8')
  const hits = text.split(anchor).length - 1
  if (hits !== 1) {
    console.log(`${code}  [BAD ] ANCHOR_BAD  ${note}(命中 ${hits} 次,应为 1)`)
    bad++
    continue
  }
  let verdict
  try { assertRulerWorks(file) } catch (e) { console.log(`  ${e.message}`); process.exit(3) }
  try {
    fs.writeFileSync(file, text.replace(anchor, replacement), 'utf8')
    if (!syntaxOk(file)) {
      console.log(`${code}  [BAD ] ${NOT_LANDED_SYNTAX}`)
      bad++
      continue
    }
    verdict = runLock()
  } finally {
    fs.writeFileSync(file, original)              // 逐字还原,不经行尾转换
  }
  if (verdict === 'KILLED') killed++
  console.log(`${code}  [${verdict === 'KILLED' ? 'KILL' : 'LIVE'}] ${verdict.padEnd(9)} ${note}`)
}

const total = MUTATIONS.length
console.log(`\n变异 ${total} 条 · KILLED ${killed} · ANCHOR_BAD ${bad} · 其余 ${total - killed - bad}`)
if (killed !== total) {
  console.error('🔴 有变异没被杀死 —— 先分诊「锁写松了」还是「变异是空操作」')
  process.exit(1)
}
console.log('✅ 全部 KILLED')
