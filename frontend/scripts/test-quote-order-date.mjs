#!/usr/bin/env node
/**
 * 门禁 · 报价列表「单据日期」锁(可执行行为断言)
 * [WO_QUOTE_LIST_DATE 2026-08-05]
 *
 * 两半:
 *   §1 **真跑** `src/lib/quoteOrderDate.ts`(esbuild 转译后 import),喂生产实测数据 ——
 *      纯文本判据抓不到"实现写反了但字面都在"那类退化。
 *   §2 **接线**判据:`OnlineQuoteFlow.tsx` 的三个日期位必须真的换掉了 ——
 *      口径函数写对了但页面还在渲染 `updated_at` = 修了个寂寞
 *      (「函数对、接线缺」这形状本周已经出现四次)。
 *
 * 跑法:node scripts/test-quote-order-date.mjs
 * ⚠️ 本锁**未接进 package.json 的 build 链** —— frontend/package.json 现有
 *    natdlg-20260806 + publayout-20260804 双声明,刻意避让。接线交 Deploy 后补。
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const root = process.cwd()
const srcLib = path.join(root, 'src/lib/quoteOrderDate.ts')
const pageFile = path.join(root, 'src/pages/Quote/OnlineQuoteFlow.tsx')

const outDir = fs.mkdtempSync(path.join(os.tmpdir(), 'quotedate-'))
const outFile = path.join(outDir, 'quoteOrderDate.mjs')
esbuild.buildSync({
  entryPoints: [srcLib],
  outfile: outFile,
  format: 'esm',
  platform: 'node',
  bundle: false,
  loader: { '.ts': 'ts' },
})
const lib = await import(pathToFileURL(outFile).href)

let failed = 0
const eq = (actual, expected, what) => {
  if (actual !== expected) {
    console.error(`[FAIL] ${what}\n   期望 ${JSON.stringify(expected)}  实际 ${JSON.stringify(actual)}`)
    failed++
  } else {
    console.log(`[ OK ] ${what}`)
  }
}
const ok = (cond, what) => eq(Boolean(cond), true, what)

// ─────────────────────────────────────────────────────────────────────────
// §1 口径本体 · 生产实测数据回放
// ─────────────────────────────────────────────────────────────────────────
console.log('── §1 单据日期口径(真跑转译产物) ──')

// 会话 194 逐字取自生产(2026-08-08 只读取证):
//   created 07-23 15:08 / 提交词 07-23 15:19 / updated_at 08-04 16:08(打开 141 次)
const S194 = {
  keywords_submitted_at: '2026-07-23 15:19:45',
  created_at: '2026-07-23 15:08:22.970385+08',
}
eq(lib.formatQuoteOrderDate(S194), new Date('2026-07-23 15:19:45').toLocaleDateString('zh-CN'),
   '【必须命中】会话 194 显示 07-23,而不是 08-04')
ok(!lib.formatQuoteOrderDate(S194).includes('8/4'), '【必须不命中】194 不许再显示 8/4')

// 打开 N 次不变:updated_at 变了,单据日期一个字不动(工单验收第 3 条)
const S194_OPENED = { ...S194, updated_at: '2026-08-08 23:59:59' }
eq(lib.formatQuoteOrderDate(S194_OPENED), lib.formatQuoteOrderDate(S194),
   '【必须命中】updated_at 被顶到今天,单据日期逐字不变')
eq(lib.quoteOrderDateSortKey(S194_OPENED), lib.quoteOrderDateSortKey(S194),
   '【必须命中】排序键也不许被 updated_at 顶动')

// 新建报价显示当日(工单验收第 2 条)
const today = new Date()
const iso = today.toISOString()
eq(lib.formatQuoteOrderDate({ keywords_submitted_at: iso, created_at: iso }),
   today.toLocaleDateString('zh-CN'), '【必须命中】新建报价显示当日')

// 回落:没有提交词时间 → 建单时间(生产 168 里 37 个是这种)
eq(lib.formatQuoteOrderDate({ created_at: '2026-07-30 09:00:00' }),
   new Date('2026-07-30 09:00:00').toLocaleDateString('zh-CN'),
   '【必须命中】无提交词时间 → 回落建单时间')

// 🔴 反向对照:两个业务时间都没有 → 什么都不显示,**不许**回落 updated_at
eq(lib.formatQuoteOrderDate({ updated_at: '2026-08-08 12:00:00' }), '',
   '【必须不命中】只有 updated_at 时不许编一个日期出来')
eq(lib.quoteOrderDateIso({ updated_at: '2026-08-08 12:00:00' }), null,
   '【必须不命中】quoteOrderDateIso 不许回落 updated_at')
eq(lib.formatQuoteOrderDate(null), '', '【必须不命中】空入参返回空串,不抛')
eq(lib.formatQuoteOrderDate({ created_at: '不是日期' }), '',
   '【必须不命中】烂日期串返回空串,不渲染 Invalid Date')

// 主显优先级:提交词时间**优先于**建单时间(会话 217 形态:建单 08-03、提交词 08-05)
eq(lib.formatQuoteOrderDate({ keywords_submitted_at: '2026-08-05 15:45:27',
                              created_at: '2026-08-03 10:00:00' }),
   new Date('2026-08-05 15:45:27').toLocaleDateString('zh-CN'),
   '【必须命中】提交词时间优先(会话 217 形态)')

// 排序键与显示同源
const older = { keywords_submitted_at: '2026-07-23 15:19:45' }
const newer = { keywords_submitted_at: '2026-08-06 10:55:02' }
ok(lib.quoteOrderDateSortKey(newer) > lib.quoteOrderDateSortKey(older),
   '【必须命中】排序键单调:新单排前面')
eq(lib.quoteOrderDateSortKey({}), 0, '【必须不命中】取不到业务时间的行排最后')

// ─────────────────────────────────────────────────────────────────────────
// §2 接线 · 页面三个日期位必须真的换掉了
// ─────────────────────────────────────────────────────────────────────────
console.log('\n── §2 接线(判据打在真渲染点上,不是打在函数上) ──')
const page = fs.readFileSync(pageFile, 'utf8')
// 剥注释,否则本包自己写的说明文字会把判据变成恒真
const code = page
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .split('\n').filter(l => !l.trim().startsWith('//')).join('\n')

ok(code.includes("from '@/lib/quoteOrderDate'"), '【必须命中】页面 import 了口径单点')
ok(/formatQuoteOrderDate\(s\)/.test(code), '【必须命中】列表卡片日期走 formatQuoteOrderDate')
ok(/formatQuoteOrderDate\(selectedSession\)/.test(code), '【必须命中】详情头日期走 formatQuoteOrderDate')
ok(/quoteOrderDateSortKey\(b\)\s*-\s*quoteOrderDateSortKey\(a\)/.test(code),
   '【必须命中】列表排序走同一口径')

// 🔴 最硬的一条:页面代码里**不许**再出现拿 updated_at 造日期的写法
const badDate = /new Date\(\s*[A-Za-z_$][\w$.]*\.updated_at/.test(code)
   || /updated_at[^\n]*toLocaleDateString/.test(code)
eq(badDate, false, '【必须不命中】页面里不许再有 new Date(x.updated_at) / updated_at→toLocaleDateString')
eq(/updated_at\?:\s*string/.test(code), false,
   '【必须不命中】Session 类型里不许再有 updated_at(放回去 = 又能被当日期用)')

// 截断集:limit 必须覆盖全表,否则"按 updated_at 选 50 条、按业务时间排"= 第一页 40% 是错的
//（生产实测 133 条可见集合,两种口径的前 50 差 20 条)
ok(/keyword-selection\/list[\s\S]{0,80}limit:\s*(\d+)/.test(code)
   && Number(code.match(/keyword-selection\/list[\s\S]{0,80}limit:\s*(\d+)/)[1]) >= 300,
   '【必须命中】选词会话列表 limit ≥ 300(覆盖全表 · 截断集不再与显示口径打架)')
eq(/keyword-selection\/list[\s\S]{0,80}limit:\s*50\s*\}/.test(code), false,
   '【必须不命中】limit 不许退回 50')

// 反向对照:判据不是恒真 —— 同样的两条模式在**原始**(未修)源码上必须命中
const original = `
  updated_at?: string;
  <p>{s.updated_at ? new Date(s.updated_at).toLocaleDateString('zh-CN') : ''}</p>
`
ok(/new Date\(\s*[A-Za-z_$][\w$.]*\.updated_at/.test(original),
   '【反向对照】同一条正则在未修源码上必须命中(证明它不是恒假)')
ok(/updated_at\?:\s*string/.test(original),
   '【反向对照】类型判据在未修源码上必须命中')

fs.rmSync(outDir, { recursive: true, force: true })
console.log(`\n${failed === 0 ? '✅ 全绿' : `🔴 ${failed} 条失败`}`)
process.exit(failed === 0 ? 0 : 1)
