/**
 * test-delist-entries.mjs — 打包商品下架 + 托管入口隐藏 的前端锁(2026-08-10)。
 *
 * 覆盖 python 变异 runner 里声明为"预期存活"的 F1 / F2 —— **声明存活不等于免检**。
 *
 * 🔴 本锁**只读 frontend/ 下的文件**,不 readFileSync 任何后端源码 ——
 *    遵守本仓铁律「引用后端文件的锁不许进 frontend build 链」
 *    (Dockerfile 的 frontend-builder 阶段只 COPY frontend/,够不到后端 → 镜像构建 100% 失败)。
 *
 * 判据形态:esbuild 现场把 TS 打成 JS 再 import,**真读常量值**;
 * 只有 JSX/映射表这类拿不到运行时值的,才退到源码断言,并各配一条反向对照。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const root = process.cwd()
const srcDir = path.join(root, 'src')
const outDir = path.join(root, 'node_modules/.cache/delist-entries')
fs.mkdirSync(outDir, { recursive: true })

let passed = 0
const failures = []
function check(name, fn) {
  try { fn(); passed++; console.log(`  ✓ ${name}`) }
  catch (e) { failures.push(`${name} — ${e.message}`); console.error(`  ✗ ${name}: ${e.message}`) }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || '断言失败') }

async function load(rel, name) {
  const outfile = path.join(outDir, `${name}.mjs`)
  await esbuild.build({
    entryPoints: [path.join(srcDir, rel)],
    bundle: true, format: 'esm', platform: 'neutral', outfile, logLevel: 'silent',
  })
  return import(pathToFileURL(outfile).href + `?t=${Date.now()}`)
}

const gate = await load('config/managedEntryGate.ts', 'gate')
const pricing = fs.readFileSync(path.join(srcDir, 'pages/Pricing/PricingPage.tsx'), 'utf8')
const sidebar = fs.readFileSync(path.join(srcDir, 'components/layout/AppSidebar.tsx'), 'utf8')
const drawer = fs.readFileSync(path.join(srcDir, 'components/c_end/CEndDrawer.tsx'), 'utf8')
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 旧 C 端嵌入面板与 M3 工具注册表随宿主整删,守它们的两格退役(见下方「四个入口」段)。
//   托管产品本身的页面 / 组件一个没删,末段照查。

/**
 * 🔴 剥注释再扫 —— 变异 F4 抓出来的:我写在入口上方的说明注释里**本身就含
 * `MANAGED_ENTRY_ENABLED`**,于是"闸有没有包住这个入口"的正则被自己的注释喂绿了。
 * 把入口改成不受闸控制,锁照样过。本仓同型前科:扫描器把"写下来的教训"判成了犯错。
 */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split(/\r?\n/).map(l => l.replace(/\/\/.*$/, '')).join('\n')
}

console.log('— 托管入口总闸 —')
check('F1 · MANAGED_ENTRY_ENABLED 出厂必须是 false', () => {
  assert(gate.MANAGED_ENTRY_ENABLED === false, `实得 ${gate.MANAGED_ENTRY_ENABLED}`)
})
check('反向对照 · 该常量真被导出(不是 undefined 恒等于 false)', () => {
  assert('MANAGED_ENTRY_ENABLED' in gate)
  assert(typeof gate.MANAGED_ENTRY_ENABLED === 'boolean')
})

console.log('— 四个入口都挂在闸上 —')
check('侧边栏 /admin/managed 受闸控制(剥注释后再判)', () => {
  const code = stripComments(sidebar)
  assert(code.includes('MANAGED_ENTRY_ENABLED'), '侧边栏没引用闸')
  assert(/MANAGED_ENTRY_ENABLED[\s\S]{0,120}\/admin\/managed/.test(code), '闸没包住该菜单项')
})
check('C 端抽屉「托管套餐」按钮受闸控制(剥注释后再判)', () => {
  const code = stripComments(drawer)
  assert(code.includes('MANAGED_ENTRY_ENABLED'), '抽屉没引用闸')
  assert(/MANAGED_ENTRY_ENABLED[\s\S]{0,200}托管套餐/.test(code), '闸没包住该按钮')
})
// [开源 E3 · 前端 · 2026-10-01] 原「嵌入面板不再映射 /managed」「M3 工具位 managed_tool 被隐藏(条目保留)」两格退役:
//   宿主(旧 C 端嵌入面板、M3 工具注册表)随 E3 整删 —— 入口连同宿主一起没了,不是被放开。
//   「托管页面 / 组件一个都没删」(Owner 禁做项)那一格照旧在下面查。

console.log('— 价目表隐藏 —')
check('F2 · 两个下架商品 code 在 HIDDEN_FEATURE_CODES 里', () => {
  for (const c of ['monitor_month_10', 'rank_alert']) {
    assert(new RegExp(`HIDDEN_FEATURE_CODES[\\s\\S]{0,600}'${c}'`).test(pricing), `缺 ${c}`)
  }
})
check('两个托管 code 也在(隐藏,可恢复)', () => {
  for (const c of ['managed_campaign_recharge', 'managed_brand_recharge']) {
    assert(new RegExp(`HIDDEN_FEATURE_CODES[\\s\\S]{0,600}'${c}'`).test(pricing), `缺 ${c}`)
  }
})
check('反向对照 · 在售 code 不许被误加进隐藏集合', () => {
  const block = pricing.split('HIDDEN_FEATURE_CODES')[1].split(']')[0]
  for (const c of ['monitoring_keyword_daily', 'monitor_single', 'scheduled_monitoring', 'geo_diagnosis']) {
    assert(!block.includes(`'${c}'`), `误伤在售 code: ${c}`)
  }
})

console.log('— 禁做项:隐藏 ≠ 删除 —')
check('托管页面/组件一个都没被删', () => {
  for (const p of [
    'pages/Admin/ManagedCampaignsAdmin.tsx',
    'pages/Managed/ManagedListPage.tsx',
    'components/managed/api.ts',
    'components/c_end/ManagedSheet.tsx',
  ]) {
    assert(fs.existsSync(path.join(srcDir, p)), `被删了(Owner 禁做项): ${p}`)
  }
})

console.log('')
if (failures.length) {
  console.error(`✗ ${failures.length} 条锁未通过:`)
  failures.forEach(f => console.error(`   - ${f}`))
  process.exit(1)
}
console.log(`✓ 全部 ${passed} 条锁通过`)
