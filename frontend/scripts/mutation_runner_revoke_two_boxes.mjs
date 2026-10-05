/**
 * mutation_runner_revoke_two_boxes.mjs
 *   —— 证明 invrel-inventory.spec.cjs 里「残留1」那三条撤回锁**有判别力**。
 *
 * Review-CTO 增量授权 2026-08-17 要求「拆锁转红」。这里逐条把改动改回改前的样子,
 * 期望锁转红;**存活 = 锁是恒绿的,等于没锁**。
 *
 * 🔴 为什么要有这个文件:素树 A/B 只能证明"旧代码上这些锁是红的",
 *    它证明不了"锁盯的是我以为的那一位"。变异是逐位拆 —— 少一位就少一条证据。
 *
 * 跑法(需要 chromium):
 *   cd frontend && npm run verify:revoke-two-boxes
 *
 * 不进 npm run build 链:它要拉真浏览器 + Vite dev server,与 build 的定位不同。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { spawnSync } from 'node:child_process'
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs'

const root = process.cwd()
const PAGE = 'src/pages/Agent/InventoryCenter.tsx'

/** [编号, 文件, 原文, 替换, 说明, 期望被杀的锁(人话)] */
const MUTANTS = [
  ['M1', PAGE,
    `          <div>
            <Label htmlFor="revoke-bonus" className="text-xs">赠送算力</Label>`,
    `          <div>
            <Label htmlFor="revoke-publish" className="text-xs">发布算力</Label>
            <Input id="revoke-publish" type="number" min={0} defaultValue="" />
          </div>
          <div>
            <Label htmlFor="revoke-bonus" className="text-xs">赠送算力</Label>`,
    '把第三格「发布算力」加回撤回弹窗',
    '恰两框 / 渲染文本无「发布算力」'],

  ['M2', PAGE,
    `        tool_points: parseInt(recharge || '0') || 0,
        publish_points: 0,`,
    `        tool_points: 0,
        publish_points: 0,`,
    '充值格不再进 tool_points(撤回额度静默变 0)',
    '撤回请求体 tool_points=充值'],

  ['M3', PAGE,
    `        tool_points: parseInt(recharge || '0') || 0,
        publish_points: 0,
        bonus_points: parseInt(bonus || '0') || 0,`,
    `        tool_points: parseInt(recharge || '0') || 0,
        publish_points: 9,
        bonus_points: parseInt(bonus || '0') || 0,`,
    'publish_points 不再恒 0(废字段又开始产生非 0 值)',
    '撤回请求体 publish_points 恒 0'],

  ['M4', PAGE,
    `  const customerTitle = (c: AgentCustomerLookupItem) =>
    isProvider(c)
      ? (c.display_name || c.phone_masked || \`服务商 \${c.customer_user_id}\`)
      : (c.brand_name || c.display_name || \`客户 \${c.customer_user_id}\`);`,
    `  const customerTitle = (c: AgentCustomerLookupItem) =>
    c.brand_name || c.display_name || \`客户 \${c.customer_user_id}\`;`,
    '标题退回"优先品牌名"(§R1 修复前的写法)',
    '撤回弹窗双向标题(服务商侧泄露下线的客户品牌名)'],
]

function runLock() {
  // 🔴 直接调 playwright 的 CLI 入口,不经 npx:Windows 上 `npx`/`npx.cmd` 不加
  //    shell:true 会 ENOENT,而 spawnSync 失败时 status 是 null —— 那会被
  //    `status === 0` 判成"不绿",于是**每条变异都像被杀了**,是最坏的假阳性。
  //    走 node + 本地 cli.js,没有 shell 解析、没有 PATH 依赖。
  const cli = path.join(root, 'node_modules', 'playwright', 'cli.js')
  if (!fs.existsSync(cli)) {
    return { green: false, out: `playwright CLI 不存在:${cli}(先装依赖)`, launchFailed: true }
  }
  const r = spawnSync(
    process.execPath,
    [cli, 'test', '--config', 'playwright.invrel.config.cjs', '-g', '残留1'],
    { cwd: root, encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } },
  )
  if (r.error || r.status === null) {
    return { green: false, out: `进程没起来:${r.error || 'status=null'}`, launchFailed: true }
  }
  return { green: r.status === 0, out: `${r.stdout || ''}${r.stderr || ''}` }
}

// 🔴 基线不绿的话,后面每条变异都会"被杀",那是假阳性。
const base = runLock()
if (!base.green) {
  // 🔴 分清"没跑起来"和"跑了但红" —— 本仓踩过把前者当后者的亏。
  console.error(base.launchFailed
    ? '❌ 锁根本没跑起来(不是红),变异结果无意义'
    : '❌ 基线不绿,变异结果无意义')
  console.error(base.out.slice(-2000))
  process.exit(2)
}
// 反向对照:基线确实**跑到了** 3 条,不是 0 条也算绿(空集合恒绿是本仓踩过的坑)
const baseCount = /(\d+) passed/.exec(base.out)
if (!baseCount || Number(baseCount[1]) !== 3) {
  console.error(`❌ 基线跑到的用例数 = ${baseCount ? baseCount[1] : '未知'},期望 3 —— 分母不对,判据不可信`)
  process.exit(2)
}
console.log('[基线] 3 passed')

let killed = 0
const survived = []
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(path.join(root, MUTANTS[0][1]), console.log);

for (const [code, rel, oldStr, newStr, why, lock] of MUTANTS) {
  const p = path.join(root, rel)
  // 🔴 Node 的 readFileSync/writeFileSync 不翻译换行(不像 Python 的默认 newline=None),
  //    所以这里不会把整份文件的 LF 改成 CRLF。别换成会翻译换行的写法。
  const orig = fs.readFileSync(p, 'utf8')
  if (!orig.includes(oldStr)) {
    console.log(`  ${code} ⚠️  锚点找不到 · ${rel} —— 按存活计`)
    survived.push([code, '锚点缺失(代码已漂移,变异根本没打进去)'])
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
    const r = runLock()
    if (r.launchFailed) {
      // 变异期间进程起不来 ≠ 锁转红,不许算"被杀"
      survived.push([code, `锁没跑起来:${r.out}`])
      console.log(`  ${code} ⚠️  锁没跑起来 —— 按存活计`)
      continue
    }
    if (r.green) {
      survived.push([code, `${why} → 锁「${lock}」没转红`])
      console.log(`  ${code} 🔴 存活 · ${why}`)
    } else {
      killed++
      console.log(`  ${code} ✅ 被杀 · ${why}  ⟵ 锁「${lock}」`)
    }
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
