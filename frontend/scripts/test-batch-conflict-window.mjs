/**
 * test-batch-conflict-window.mjs — 批量投放「冲突处理窗口」七条锁。
 *
 * 工单 WO-BATCH-CONFLICT-2026-08-04。沿用 test-publish-center-scope.mjs 的做法:
 * esbuild 现场把 TS/TSX 打成 JS **真跑一遍行为**;呈现件用 react-dom/server 渲成
 * 静态 HTML 后断言 —— 断的是渲染结果,不是源码字符串(源码串断言换皮即绕过、
 * 重构就误伤,双向脆)。
 *
 *   锁1 解析:结构化冲突能解析出来(带 article_id/media_id)
 *   锁2 解析反向:脏/缺字段一律不进清单,解析不出就回落 null
 *   锁3 分类文案:already_published ≠ in_flight,且绝不说成"进行中"
 *   锁4 剔除:按 (article,media) 成对摘,不误伤同媒体别的文章
 *   锁5 剩余计数:M 从剔除后真算,不是减法
 *   锁6 面板渲染:列冲突 + 一键剔除按钮 + 明示没扣钱
 *   锁7 空单:剔除后剩 0 时禁止提交,且不渲染剔除按钮
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const root = process.cwd()
const srcDir = path.join(root, 'src')
const outDir = path.join(root, 'node_modules/.cache/batch-conflict-window')
fs.mkdirSync(outDir, { recursive: true })

const stubDynamicImports = {
  name: 'stub-dynamic-imports',
  setup(build) {
    const STUBBED = /^@\/(context\/AuthContext|lib\/api|lib\/lazyToast)$/
    build.onResolve({ filter: STUBBED }, args => ({ path: args.path, namespace: 'stub' }))
    build.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
      contents: 'export const authApi = {}; export const apiErrorText = () => ""; export const lazyToast = {};',
      loader: 'js',
    }))
  },
}

async function load(rel, name) {
  const outfile = path.join(outDir, `${name}.mjs`)
  await esbuild.build({
    plugins: [stubDynamicImports],
    entryPoints: [path.join(srcDir, rel)],
    outfile,
    bundle: true,
    format: 'esm',
    platform: 'node',
    jsx: 'automatic',
    external: ['react', 'react-dom', 'react/jsx-runtime'],
    alias: { '@': srcDir },
    mainFields: ['module', 'main'],
    conditions: ['import', 'module'],
    logLevel: 'silent',
  })
  return import(pathToFileURL(outfile).href)
}

const C = await load('contracts/duplicateOrderConflict.ts', 'conflict')
const panelMod = await load('components/publishing/DuplicateConflictPanel.tsx', 'panel')

let failed = 0
const DECLARED_LOCKS = ['锁1', '锁2', '锁3', '锁4', '锁5', '锁6', '锁7']
const ran = new Map(DECLARED_LOCKS.map(k => [k, 0]))
const tally = (what) => {
  const m = /^(锁\d+)/.exec(what)
  if (m && ran.has(m[1])) ran.set(m[1], ran.get(m[1]) + 1)
}
const eq = (actual, expected, what) => {
  tally(what)
  const a = JSON.stringify(actual), e = JSON.stringify(expected)
  if (a !== e) { console.error(`❌ ${what}\n   期望 ${e}\n   实际 ${a}`); failed++ }
  else console.log(`✅ ${what}`)
}
const ok = (cond, what) => eq(!!cond, true, what)

// 生产真实形状:item 414 = article 1393 × 博客园(125918),published + 真实单号。
const REAL_409 = {
  detail: {
    code: 'DUPLICATE_ORDER',
    message: '这几篇已经发过对应媒体了：东莞乘客电梯安全吗…判断依据×博客园',
    conflicts: [
      { article_id: 1393, media_id: 125918, article_title: '惠州乘客电梯安全吗？定期检验不合格项与风险判断依据',
        media_name: '博客园', reason_code: 'ALREADY_PUBLISHED', label: 'x' },
      { article_id: 1400, media_id: 125918, article_title: '另一篇在发的',
        media_name: '博客园', reason_code: 'IN_FLIGHT', label: 'y' },
    ],
  },
}

// ───────────────────────────── 锁1 解析
{
  const b = C.parseDuplicateOrderBlock(REAL_409)
  ok(b !== null, '锁1 真实 409 载荷能解析出冲突块')
  eq(b.conflicts.length, 2, '锁1 两条冲突都在')
  eq(b.conflicts[0].articleId, 1393, '锁1 article_id 解析正确(能回到具体组合)')
  eq(b.conflicts[0].mediaId, 125918, '锁1 media_id 解析正确')
  eq(b.conflicts[0].reason, 'already_published', '锁1 大写理由码规范化成小写')
  eq(b.conflicts[1].reason, 'in_flight', '锁1 第二条是 in_flight')
  // 也接受直接传 payload 本身(不套 detail)
  ok(C.parseDuplicateOrderBlock(REAL_409.detail) !== null, '锁1 直接传 payload 也能解析')
}

// ───────────────────────────── 锁2 解析反向
{
  eq(C.parseDuplicateOrderBlock(null), null, '锁2 null 不炸,返回 null')
  eq(C.parseDuplicateOrderBlock({ detail: { code: 'OTHER_CODE', conflicts: [] } }), null,
    '锁2 别的 code 不认领')
  eq(C.parseDuplicateOrderBlock({ detail: { code: 'DUPLICATE_ORDER' } }), null,
    '锁2 没有 conflicts 字段 → null(回落老 toast)')
  // 🔴 缺 article_id / media_id 的条目必须丢弃:补 0 会让"剔除"去摘不存在的组合
  const partial = C.parseDuplicateOrderBlock({
    detail: { code: 'DUPLICATE_ORDER', conflicts: [
      { media_id: 1, reason_code: 'IN_FLIGHT' },
      { article_id: 2, reason_code: 'IN_FLIGHT' },
      { article_id: 3, media_id: 4, reason_code: 'IN_FLIGHT' },
    ] },
  })
  eq(partial.conflicts.length, 1, '锁2 缺 id 的条目被丢弃,只留完整那条')
  eq(partial.conflicts[0].articleId, 3, '锁2 留下的是完整那条(反向:不是全丢)')
  eq(C.parseDuplicateOrderBlock({ detail: { code: 'DUPLICATE_ORDER', conflicts: [
    { media_id: 1, reason_code: 'IN_FLIGHT' } ] } }), null,
    '锁2 全部条目都残缺 → null,不给一个点不动的空窗口')
  eq(C.normalizeConflictReason('SOMETHING_NEW'), 'other', '锁2 没登记的码落 other,不猜')
}

// ───────────────────────────── 锁3 分类文案
{
  const L = C.CONFLICT_REASON_LABELS
  ok(L.already_published !== L.in_flight, '锁3 两类文案必须不同')
  ok(/发布过/.test(L.already_published), '锁3 已发布说的是"发布过"')
  // 🔴 本单核心:published 绝不许说成"进行中"
  ok(!/进行中/.test(L.already_published), '锁3 已发布**不**说成"进行中"')
  ok(!/进行中/.test(C.CONFLICT_REASON_HINTS.already_published), '锁3 已发布的解释里也没有"进行中"')
  ok(/进行中/.test(L.in_flight), '锁3 反向:真在途的才说"进行中"(证明判据不是恒真)')
  const groups = C.groupConflictsByReason(C.parseDuplicateOrderBlock(REAL_409).conflicts)
  eq(groups.map(g => g.reason), ['already_published', 'in_flight'], '锁3 按类型分组且顺序稳定')
  eq(groups.map(g => g.items.length), [1, 1], '锁3 两组各一条')
  eq(C.groupConflictsByReason([]).length, 0, '锁3 反向:空冲突不产生空分组')
}

// ───────────────────────────── 锁4 剔除
{
  const cart = [
    { articleId: 1393, mediaList: [{ id: 125918 }, { id: 777 }] },  // 冲突 + 一个正常
    { articleId: 1400, mediaList: [{ id: 125918 }] },               // 整条都冲突
    { articleId: 9999, mediaList: [{ id: 125918 }] },               // 同媒体但**别的**文章
  ]
  const conflicts = C.parseDuplicateOrderBlock(REAL_409).conflicts
  const next = C.applyConflictRemoval(cart, conflicts)
  eq(next.length, 2, '锁4 整条冲突的条目被移除,其余保留')
  eq(next[0], { articleId: 1393, mediaList: [{ id: 777 }] },
    '锁4 只摘冲突那个媒体,同文章其余媒体保留')
  // 🔴 反向:同一家媒体在别的文章上不能被误伤
  eq(next[1], { articleId: 9999, mediaList: [{ id: 125918 }] },
    '锁4 反向:媒体 125918 对文章 9999 不是冲突,不许一刀切删掉')
  eq(C.applyConflictRemoval(cart, []), cart, '锁4 反向:无冲突时购物车逐位不变')
}

// ───────────────────────────── 锁5 剩余计数
{
  const cart = [
    { articleId: 1393, mediaList: [{ id: 125918 }, { id: 777 }] },
    { articleId: 1400, mediaList: [{ id: 125918 }] },
  ]
  const conflicts = C.parseDuplicateOrderBlock(REAL_409).conflicts
  eq(C.countCombinations(cart), 3, '锁5 剔除前 3 个组合')
  eq(C.remainingAfterRemoval(cart, conflicts), 1, '锁5 剔除后真算得 1')
  // 🔴 反向:后端对同一组合返回重复条目时,减法会算成 3-3=0,真算仍是 1
  const dupConflicts = [...conflicts, ...conflicts]
  eq(C.remainingAfterRemoval(cart, dupConflicts), 1,
    '锁5 反向:冲突条目重复时 M 仍是 1(证明不是用减法)')
  eq(C.countCombinations([]), 0, '锁5 空购物车 0 个组合')
}

// ───────────────────────────── 锁6 面板渲染
{
  const block = C.parseDuplicateOrderBlock(REAL_409)
  const html = renderToStaticMarkup(React.createElement(panelMod.DuplicateConflictPanel, {
    block, remainingAfterRemoval: 1, onRemoveConflicts() {},
  }))
  ok(html.includes('duplicate-conflict-panel'), '锁6 冲突面板渲染出来了')
  ok(html.includes('data-conflict-count="2"'), '锁6 冲突条数落到 DOM 上')
  ok(html.includes(C.CONFLICT_REASON_LABELS.already_published), '锁6 渲染"已发布过"分类')
  ok(html.includes(C.CONFLICT_REASON_LABELS.in_flight), '锁6 渲染"进行中"分类')
  ok(html.includes('博客园'), '锁6 逐条列出是哪个媒体')
  ok(html.includes(REAL_409.detail.conflicts[0].article_title), '锁6 逐条列出是哪篇文章')
  ok(html.includes('未产生任何费用'), '锁6 明示预检没扣钱')
  ok(html.includes('remove-conflicts-button'), '锁6 给出一键剔除按钮')
  ok(/去掉这\s*2\s*个冲突组合/.test(html.replace(/<!--[^>]*-->/g, '')), '锁6 按钮说清去掉几个')
  ok(/继续投放其余\s*1\s*个/.test(html.replace(/<!--[^>]*-->/g, '')), '锁6 按钮说清还剩几个')
  // 🔴 冲突未处理前必须禁提交。断在纯判定上(它是资金闸,按下去就扣费)。
  ok(C.isSubmitBlockedByConflicts(block), '锁6 冲突未处理时禁止直接提交')
  ok(!C.isSubmitBlockedByConflicts(null), '锁6 反向:无冲突块时不禁提交')
  ok(!C.isSubmitBlockedByConflicts({ code: 'DUPLICATE_ORDER', message: '', conflicts: [] }),
    '锁6 反向:冲突已清空时不禁提交(证明判据不是恒真)')
  // 反向:无冲突时面板不该出现、提交按钮不该被本条禁用
  const clean = renderToStaticMarkup(React.createElement(panelMod.DuplicateConflictPanel, {
    block: { code: 'DUPLICATE_ORDER', message: '', conflicts: [] },
    remainingAfterRemoval: 0,
  })) || ''
  ok(!clean.includes('duplicate-conflict-panel'), '锁6 反向:无冲突时不渲染冲突面板')
  ok(!clean.includes('remove-conflicts-button'), '锁6 反向:无冲突时没有剔除按钮')
}

// ───────────────────────────── 锁7 空单
{
  const allConflict = C.parseDuplicateOrderBlock(REAL_409)
  const html = renderToStaticMarkup(React.createElement(panelMod.DuplicateConflictPanel, {
    block: allConflict, remainingAfterRemoval: 0, onRemoveConflicts() {},
  }))
  // 🔴 剔完剩 0 = 空单:不给剔除按钮(点了就是提交 0 项)
  ok(!html.includes('remove-conflicts-button'), '锁7 剩 0 时不渲染剔除按钮')
  ok(html.includes('没有可以继续投放的内容'), '锁7 剩 0 时明确告诉用户没得投')
  ok(C.isSubmitBlockedByConflicts(allConflict), '锁7 剩 0 时仍禁提交(空单不许提交)')
  ok(html.includes('data-remaining-after-removal="0"'), '锁7 剩余数落到 DOM 上')
  // 反向:剩 >0 时按钮回来(证明上面三条不是恒真)
  const some = renderToStaticMarkup(React.createElement(panelMod.DuplicateConflictPanel, {
    block: allConflict, remainingAfterRemoval: 3, onRemoveConflicts() {},
  }))
  ok(some.includes('remove-conflicts-button'), '锁7 反向:剩 3 时剔除按钮存在')
  ok(!some.includes('没有可以继续投放的内容'), '锁7 反向:剩 3 时不说"没得投"')
}

// ───────────────────────────── 收尾
const skipped = DECLARED_LOCKS.filter(k => ran.get(k) === 0)
const total = [...ran.values()].reduce((a, b) => a + b, 0)
console.log('\n── 覆盖统计 ──')
console.log(DECLARED_LOCKS.map(k => `${k}:${ran.get(k)}`).join('  '))
console.log(`断言 ${total} 条 / 声明 ${DECLARED_LOCKS.length} 条锁 / passed ${total - failed} / failed ${failed} / skipped ${skipped.length}`)
if (skipped.length) console.error(`❌ 以下锁一条断言都没跑(= SKIP,不算 PASS): ${skipped.join(', ')}`)

const bad = failed || skipped.length
console.log(bad ? `\n🔴 未通过(failed=${failed} skipped=${skipped.length})` : '\n✅ 批量冲突处理窗口七条锁全绿 · 0 skipped')
process.exit(bad ? 1 : 0)
