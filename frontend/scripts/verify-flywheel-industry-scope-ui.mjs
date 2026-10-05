/**
 * verify-flywheel-industry-scope-ui.mjs — 飞轮全景「行业口径」的**接线**门禁。
 *
 * 存在的理由(工单 WO_FLYWHEEL_PANORAMA_INDUSTRY_SCOPE_2026-08-16):
 * 后端把行业过滤做对了、单测也全绿,但只要前端少传一个 `industry_key`,
 * 页面看到的仍然是全站数 —— **后端锁一条都不会红**。这就是"死接线"。
 * 2026-08-13 invrel 的 P0 正是同一形态(端点做好了但没接前端 → 灯永远不灭)。
 *
 * 断的是**结构性质**(哪个值被传到哪个位置 / 角标由哪个字段驱动),
 * 不是文案串 —— 换措辞不该转红,换掉数据来源必须转红。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const read = (p) => fs.readFileSync(path.join(process.cwd(), p), 'utf8')

let failed = 0
const ok = (cond, what) => {
  if (cond) console.log(`✅ ${what}`)
  else { console.error(`❌ ${what}`); failed++ }
}

// 只剥块注释与独占一行的 `//`,不碰行内 `//`(免得误伤 https://)。
const stripComments = (s) => s
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/^[ \t]*\/\/.*$/gm, '')

// ── 剥注释器已知答案自检:不做这个,下面反向断言的判别力全是"我觉得"。
{
  const probe = "const a = 1 // KEEPME_INLINE\n// KEEPME_OWNLINE\n/* KEEPME_BLOCK */\nconst b = 'KEEPME_CODE'\n"
  const out = stripComments(probe)
  ok(!out.includes('KEEPME_OWNLINE'), '[自检] 独占行注释被剥掉(否则反向断言恒红)')
  ok(!out.includes('KEEPME_BLOCK'), '[自检] 块注释被剥掉')
  ok(out.includes('KEEPME_CODE'), '[自检] 代码里的同类字符串必须留下(否则反向断言恒真 = 真正危险的一侧)')
}

const panorama = stripComments(read('src/components/flywheel/PanoramaTab.tsx'))
const health = stripComments(read('src/components/flywheel/DataHealthTab.tsx'))
const ring = stripComments(read('src/components/flywheel/FlywheelRing.tsx'))
const page = stripComments(read('src/pages/Admin/GeoPlacementFlywheel.tsx'))

console.log('飞轮全景 · 行业口径接线门禁')

// ── 锁 1:两个消费 panorama 的 tab 都必须把行业带上 ──────────────────
// 少任何一个,那个 tab 就退回全站口径 —— 本工单要修的 bug 原地复活。
for (const [name, src] of [['PanoramaTab', panorama], ['DataHealthTab', health]]) {
  ok(
    /apiGetCached<PanoramaResponse>\(\s*`\/flywheel-panorama\?industry_key=\$\{enc\(industry\)\}`/.test(src),
    `锁1 ${name} 调 /flywheel-panorama 时带 industry_key`,
  )
  // 反向:不许再存在"裸调不带行业"的写法(那是改动前的形态)
  ok(
    !/apiGetCached<PanoramaResponse>\(\s*['"]\/flywheel-panorama['"]\s*\)/.test(src),
    `锁1-反向 ${name} 不残留裸调 '/flywheel-panorama'(不带行业)`,
  )
}

// ── 锁 2:角标必须由后端字段驱动,不能前端自己猜 ──────────────────────
ok(
  /siteScope:\s*industryScoped\s*&&\s*n\?\.industry_scope\s*===\s*'site'/.test(ring),
  '锁2 「全站」角标由后端 industry_scope + industryScoped 两个条件共同驱动',
)
ok(
  /industryScoped=\{!!panorama\?\.industry_scoped\}/.test(panorama),
  '锁2 PanoramaTab 把后端的 industry_scoped 传给 FlywheelRing',
)
// 反向:角标不许由「前端自己判断 industry 是不是 general」得出 —— 那会跟后端口径打架
ok(
  !/siteScope[^\n]*industry\s*(===|!==)\s*['"]general['"]/.test(ring),
  '锁2-反向 角标不由前端自行判断行业名得出(必须以后端口径为准)',
)

// ── 锁 3:角标真的被渲染出来(PC + 移动端两处都要,否则一半用户看不到)──
const badges = ring.match(/nv\.siteScope\s*&&\s*\(/g) || []
ok(badges.length >= 2, `锁3 「全站」角标在 PC 卡与移动端列表两处都渲染(实测 ${badges.length} 处)`)
ok(/全站/.test(ring), '锁3 角标文案存在')
// 反向:角标不能无条件渲染(否则全站口径下每张卡都挂「全站」= 新的噪声)
ok(
  !/>\s*全站\s*<\/span>\s*\)?\s*\}?[\s\S]{0,40}$/.test(ring.replace(/nv\.siteScope\s*&&\s*\([\s\S]*?\)\}/g, '')),
  '锁3-反向 角标不是无条件渲染(必须挂在 nv.siteScope 上)',
)

// ── 锁 4:页面级口径说明只在"选了具体行业"时出现 ──────────────────────
ok(
  /panorama\?\.industry_scoped\s*&&\s*\(panorama\.site_scope_nodes\s*\|\|\s*\[\]\)\.length\s*>\s*0/.test(panorama),
  '锁4 页面级「哪些环是全站口径」说明,以 industry_scoped + site_scope_nodes 非空为条件',
)

// ── 锁 5:行业展示名真的传下来了(否则提示里出现的是英文 key)──────────
ok(
  /<PanoramaTab[^>]*industryLabel=\{geoIndustryLabel\(industry\)\s*\|\|\s*industry\}/.test(page),
  '锁5 页面把行业中文展示名传给 PanoramaTab',
)
ok(/\{industryLabel\}/.test(panorama), '锁5 PanoramaTab 真的把 industryLabel 渲染出来(不是收了不用)')

console.log(failed === 0 ? '\n✅ 飞轮行业口径接线门禁通过' : `\n❌ ${failed} 条未通过`)
process.exit(failed === 0 ? 0 : 1)
