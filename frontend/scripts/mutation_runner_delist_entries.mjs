/**
 * mutation_runner_delist_entries.mjs — 证明 test-delist-entries.mjs 有判别力。
 *
 * 每条变异把一处真改动改回改前的样子,期望锁转红。全绿 = 锁是恒绿的,等于没锁。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { spawnSync } from 'node:child_process'
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs'

const root = process.cwd()
const MUTANTS = [
  ['F1', 'src/config/managedEntryGate.ts',
    'export const MANAGED_ENTRY_ENABLED = false;',
    'export const MANAGED_ENTRY_ENABLED = true;',
    '翻开托管入口总闸'],
  ['F2', 'src/pages/Pricing/PricingPage.tsx',
    "  'monitor_month_10',\n  'rank_alert',\n", '',
    '价目表不再隐藏下架商品'],
  // [开源 E3 · 前端 · 2026-10-01] F3(M3 工具注册表里的 managed_tool 条目)随 components/m3 整删、对应锁格退役,该毒退役
  ['F4', 'src/components/layout/AppSidebar.tsx',
    "...(MANAGED_ENTRY_ENABLED\n        ? [{ to: '/admin/managed', icon: Zap, label: 'GEO 托管管理' }]\n        : []),",
    "{ to: '/admin/managed', icon: Zap, label: 'GEO 托管管理' },",
    '侧边栏入口脱离闸控制'],
  ['F5', 'src/pages/Pricing/PricingPage.tsx',
    "  'managed_campaign_recharge',\n  'managed_brand_recharge',\n", '',
    '价目表不再隐藏托管条目'],
]

function runLock() {
  const r = spawnSync('node', ['scripts/test-delist-entries.mjs'],
    { cwd: root, encoding: 'utf8' })
  return r.status === 0
}

if (!runLock()) {
  console.error('❌ 基线不绿,变异结果无意义')
  process.exit(2)
}
console.log('[基线] 全绿')

let killed = 0
const survived = []
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(path.join(root, MUTANTS[0][1]), console.log);

for (const [code, rel, oldStr, newStr, why] of MUTANTS) {
  const p = path.join(root, rel)
  const orig = fs.readFileSync(p, 'utf8')
  if (!orig.includes(oldStr)) {
    console.log(`  ${code} ⚠️  锚点找不到 · ${rel}`)
    survived.push([code, '锚点缺失'])
    continue
  }
  try { assertRulerWorks(p) } catch (e) { console.log(`  ${e.message}`); process.exit(3) }
  try {
    fs.writeFileSync(p, orig.replace(oldStr, newStr), 'utf8')
    if (!syntaxOk(p)) {
      survived.push([code, NOT_LANDED_SYNTAX])
      console.log(`  ${code} ${NOT_LANDED_SYNTAX}`)
      continue
    }
    if (runLock()) { survived.push([code, why]); console.log(`  ${code} 🔴 存活 · ${why}`) }
    else { killed++; console.log(`  ${code} ✅ 被杀 · ${why}`) }
  } finally {
    fs.writeFileSync(p, orig, 'utf8')
  }
}

console.log(`\n变异 ${killed}/${MUTANTS.length} 被杀`)
if (survived.length) {
  console.error('❌ MUTATION_FAIL —— 存活:')
  survived.forEach(([c, w]) => console.error(`   ${c}: ${w}`))
  process.exit(1)
}
console.log('✅ MUTATION_OK —— 全杀')
