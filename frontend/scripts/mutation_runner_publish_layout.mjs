/**
 * mutation_runner_publish_layout.mjs — 给发布中心布局那几条锁做变异自检。
 *
 * 工单 §6.1:「每件至少 1 个变异,变异后断言必须红」+「变异要过自检:
 * 这个变异在这个 fixture 下真的改变行为了吗」。
 *
 * 每条变异做三件事,少一件都不算数:
 *   1. **改到了** —— 改完文件字节必须变(否则是 no-op 变异,红不红都说明不了问题);
 *   2. **红了**   —— 指定的门禁脚本退出码必须非 0;
 *   3. **还原了** —— 还原后字节必须与原文**逐字节**一致。
 *
 * 🔴 全程用二进制读写(Buffer),不走 `readFileSync(…, 'utf8')` + `writeFileSync`。
 * Windows 上按文本写会把整包行尾翻成 CRLF —— 2026-08-03/04 连着栽过两次,
 * 一次让整包 CRLF 作废重做,一次把一个变异烤进了交付代码。
 *
 * 🔴 变异期间**不做任何 git 写操作**(不 stash 不 checkout)。08-04 踩过:
 * `git stash` 撞上后台跑着的变异 runner,改动被写回丢失 + 一个变异被烤进交付代码。
 * 还原只靠本进程内存里的原始 Buffer。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { spawnSync } from 'node:child_process'
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const root = process.cwd()
const P = {
  filter: path.join(root, 'src/components/publishing/ArticleStatusFilter.tsx'),
  drawer: path.join(root, 'src/components/publishing/MediaAdviceDrawer.tsx'),
  page: path.join(root, 'src/pages/Publishing/PublishCenter.tsx'),
  layout: path.join(root, 'src/components/layout/Layout.tsx'),
}
const GATE = {
  layoutLocks: 'scripts/test-publish-center-layout.mjs',
  wiring: 'scripts/verify-publish-center-scope-ui.mjs',
  scope: 'scripts/test-publish-center-scope.mjs',
}

/** 每条变异:改哪个文件、把什么换成什么、期望哪个门禁转红、对应工单哪一件。 */
const MUTATIONS = [
  {
    id: 'M1',
    item: 'L2 · T2 不回退',
    file: 'filter',
    from: '      {options.map(opt => {',
    to: '      {options.filter(o => o.count > 0).map(opt => {',
    gate: 'layoutLocks',
    why: '把空组重新变成"空即不渲染"(正是 2026-07-30 T2 修的病)→「两个空组的 0 都在」必须转红',
  },
  {
    id: 'M2',
    item: 'L2 · 四计数同时可见(工单点名的变异锚点)',
    file: 'filter',
    from: '      {options.map(opt => {',
    to: '      {options.filter(o => o.key === active).map(opt => {',
    gate: 'layoutLocks',
    why: '退回"一次只看得到一个状态"(等价于改回四段堆叠里只有一段展开)→「四个状态一个不少」必须转红',
  },
  {
    id: 'M3',
    item: 'L2 · 不许再打 minHeight 补丁',
    file: 'page',
    from: '              data-article-list-pane=""',
    to: '              data-article-list-pane="" style={{ minHeight: \'180px\' }}',
    gate: 'wiring',
    why: '把 2026-05-05 那个 minHeight 补丁塞回来 →「左栏不再有 minHeight 硬下限」必须转红',
  },
  {
    id: 'M4',
    item: 'L1 · 页面不许自己算高度',
    file: 'page',
    from: '    <div className="flex h-full min-h-0 max-w-full flex-col overflow-hidden p-2 sm:p-4 md:p-6">',
    to: '    <div className="flex min-h-[calc(100dvh-7rem)] max-w-full flex-col overflow-x-hidden p-2 pb-24 sm:p-4 md:h-[calc(100dvh-7rem)] md:min-h-0 md:overflow-hidden md:p-6 md:pb-6">',
    gate: 'wiring',
    why: '把写死的 7rem 换回来(就是老板看到的 64px 底部黑边)→ 接线3b 两条必须转红',
  },
  {
    id: 'M5',
    item: 'L1 · 外壳要给发布中心定高',
    file: 'layout',
    from: "className={cn(isFullHeightPage ? 'flex-1 min-h-0' : 'flex-1')}",
    to: "className={cn(isAgentPage ? 'flex-1 min-h-0' : 'flex-1')}",
    gate: 'layoutLocks',
    why: '<main> 不再给 min-h-0 → 它会被内容撑大,页面 h-full 继承到错的高度 → 锁9 必须转红',
  },
  {
    id: 'M6',
    item: 'L3 · 收起态只占一行',
    file: 'drawer',
    from: '      {open && (\n        <div data-drawer-body=""',
    to: '      {true && (\n        <div data-drawer-body=""',
    gate: 'layoutLocks',
    why: '收起时照样渲染内容体 →「收起态没有内容体 / 不渲染 T1T2 榜」必须转红',
  },
  {
    id: 'M7',
    item: 'L3 · 不许飘出左栏 / 盖住购物车条',
    file: 'drawer',
    from: "cn('flex shrink-0 flex-col border-t border-border', className)",
    to: "cn('fixed z-50 flex shrink-0 flex-col border-t border-border', className)",
    gate: 'layoutLocks',
    why: '给抽屉加 fixed+z-index(就能盖住页面级一键发布条)→ 锁7 三条必须转红',
  },
  {
    id: 'M8',
    item: 'L4 · 不许出现负高度',
    file: 'drawer',
    from: '  if (upper <= MIN_DRAWER_PX) return Math.max(0, upper);',
    to: '  if (upper <= MIN_DRAWER_PX) return upper;',
    gate: 'layoutLocks',
    why: '超矮容器下 upper 为负数且不再兜底 → 锁6「容器 0 / 100 不产生负高度」必须转红',
  },
  {
    id: 'M9',
    item: 'L4 · 必须记住上次高度',
    file: 'drawer',
    from: '    localStorage.setItem(storageKey, String(Math.round(height)));',
    to: '    void storageKey; void height;',
    gate: 'layoutLocks',
    why: '写入变成 no-op(= 每次进来重置,工单说"比没有更烦")→ 锁8 必须转红',
  },
]

const run = (script) => {
  const r = spawnSync(process.execPath, [script], { cwd: root, encoding: 'utf8' })
  return { code: r.status, out: (r.stdout || '') + (r.stderr || '') }
}

// ── 0. 基线:所有门禁在没变异时必须全绿。基线本来就红的话,后面"变异后红"毫无意义。
console.log('── 基线(未变异)──')
let failed = 0
const baseline = {}
for (const [name, script] of Object.entries(GATE)) {
  const r = run(script)
  baseline[name] = r.code
  if (r.code === 0) console.log(`✅ 基线 ${name} 绿`)
  else { console.error(`❌ 基线 ${name} 就是红的(exit ${r.code}) —— 变异自检失去意义`); failed++ }
}
if (failed) {
  console.error('\n🔴 基线不绿,停止')
  process.exit(1)
}

// ── 1. 逐条变异
console.log('\n── 变异 ──')
const ORIG = Object.fromEntries(Object.entries(P).map(([k, v]) => [k, fs.readFileSync(v)]))
let mutFailed = 0

/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(P[MUTATIONS[0].file]);

for (const m of MUTATIONS) {
  const file = P[m.file]
  const orig = ORIG[m.file]
  const text = orig.toString('utf8')

  // 自检 a:锚点必须命中,且**唯一**。命中 0 次 = 变异根本没落地(no-op);
  // 命中多次 = 改到了不该改的地方,红了也说明不了是哪一处。
  const hits = text.split(m.from).length - 1
  if (hits !== 1) {
    console.error(`❌ ${m.id} 锚点命中 ${hits} 次(要求恰好 1 次)—— 变异没落地或打散了`)
    mutFailed++
    continue
  }

  const mutated = Buffer.from(text.replace(m.from, m.to), 'utf8')
  // 自检 b:改完字节必须真的变了
  if (mutated.equals(orig)) {
    console.error(`❌ ${m.id} 变异后文件字节没变 —— no-op 变异`)
    mutFailed++
    continue
  }

  try { assertRulerWorks(file) } catch (err) { console.log(`  ${err.message}`); process.exit(3) }
  fs.writeFileSync(file, mutated)
  if (!syntaxOk(file)) {
    console.log(`  ${NOT_LANDED_SYNTAX}`)
    fs.writeFileSync(file, orig)
    continue
  }
  const r = run(GATE[m.gate])
  fs.writeFileSync(file, orig)

  // 自检 c:还原必须逐字节一致(防 CRLF 翻转 / 变异被烤进交付代码)
  const restored = fs.readFileSync(file)
  if (!restored.equals(orig)) {
    console.error(`🔴🔴 ${m.id} 还原后字节与原文不一致 —— 立刻人工检查 ${m.file}`)
    mutFailed++
    continue
  }

  if (r.code !== 0) {
    console.log(`✅ ${m.id} [${m.item}] 变异后 ${m.gate} 转红(exit ${r.code}) · ${m.why}`)
  } else {
    console.error(`❌ ${m.id} [${m.item}] 变异后 ${m.gate} 仍然全绿 —— 这条锁抓不到本变异`)
    console.error(`   ${m.why}`)
    mutFailed++
  }
}

// ── 2. 收尾:还原后基线必须重新全绿
console.log('\n── 还原后复跑基线 ──')
for (const [name, script] of Object.entries(GATE)) {
  const r = run(script)
  if (r.code === 0) console.log(`✅ 还原后 ${name} 仍绿`)
  else { console.error(`🔴🔴 还原后 ${name} 变红了(exit ${r.code}) —— 有变异没还原干净`); mutFailed++ }
}

const covered = new Set(MUTATIONS.map(m => m.item.split(' · ')[0]))
console.log(`\n变异 ${MUTATIONS.length} 条 / 覆盖 ${[...covered].sort().join(' ')} / 未通过 ${mutFailed}`)
console.log(mutFailed ? '\n🔴 变异自检未通过' : '\n✅ 变异自检全通过:每条锁都能抓到对应的回退')
process.exit(mutFailed ? 1 : 0)
