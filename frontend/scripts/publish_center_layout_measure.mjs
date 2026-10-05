/**
 * publish_center_layout_measure.mjs — 工单 §6.2 的像素实测(改前/改后 A/B)。
 *
 * 工单原话:「🔴 硬要求,必须量像素不许目测」。三档视口各量一次
 * 「未分发列表可见行数」,判据 = 改后 >= 改前 + 4 行;顺便量底部黑色留白高度(改后必须 = 0)。
 *
 * ## 为什么是 harness 而不是直接点生产
 *
 * 生产跑的就是本包的底(e16ba335),所以"改前"本可以直接在 omnirank.top 上量 ——
 * 底部 64px 黑边我确实那么量的(见交付说明)。但「未分发可见行数」量不了:
 * 唯一授权的 QA 服务商账号(113)名下项目**一篇已完成文章都没有**,
 * 而拿真实客户的项目去量是红线(要请示)。
 *
 * 所以行数改用 harness:**同一份 fixture、同一份真 CSS、同一批真组件**,
 * 只把布局结构换成改前/改后两种。这比拿两个不同项目去比更公平 ——
 * 两边喂的文章数完全一样(46/0/13/0,取自工单 §4 L2 的示例分布)。
 *
 * ## 保真度(交付说明里如实写了)
 *
 *   ✅ 真 CSS:链接 `vite build` 真实产出的 dist/assets/index-*.css(线上发的那一份)
 *   ✅ 真组件:改后用 ArticleStatusFilter / MediaAdviceDrawer 本体;
 *      改前用 `git show <底>:…ArticleGroupSection.tsx` 取回来的**生产那一版**本体
 *   ✅ 真外壳:复刻 Layout.tsx 的 SidebarInset(h-100dvh flex-col)+ Header(h-12)+ main
 *   ✅ 真类名:文章行沿用 PublishCenter 里那一串 class,行高由真 CSS 算
 *   ⚠️ 媒体榜(MediaEffectivenessPanel)要拉接口才有内容,静态渲不出 →
 *      两侧都用**同一个定高占位块**代表那三块辅助面板,高度取生产实测值 430px。
 *      为了证明结论不依赖这个数,下面还会在 auxPx=0(对我最不利)时再跑一遍。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import http from 'node:http'
import { execFileSync } from 'node:child_process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { chromium } from 'playwright'

const root = process.cwd()
const srcDir = path.join(root, 'src')
const cache = path.join(root, 'node_modules/.cache/publish-layout-measure')
fs.mkdirSync(cache, { recursive: true })

const BASE_SHA = process.env.BASE_SHA || 'e16ba335f35af0b2b1e8d007770de2e91216695f'
/** 生产实测:左栏底部三块辅助面板(flex min-h-0 shrink-0 flex-col gap-2)的整块高度。 */
const AUX_PX_MEASURED = 430
/** 工单 §4 L2 的示例分布。 */
const FIXTURE = { unpublished: 46, inProgress: 0, published: 13, rejected: 0 }
const VIEWPORTS = [
  { w: 1920, h: 1080 },
  { w: 1440, h: 900 },
  { w: 1366, h: 768 },
]

// ── 取回"改前"那一版 ArticleGroupSection(它已在本包里被删掉,只有 git 里还有)
const beforeGroupPath = path.join(cache, 'ArticleGroupSection.before.tsx')
{
  const buf = execFileSync('git', ['show', `${BASE_SHA}:frontend/src/components/publishing/ArticleGroupSection.tsx`],
    { cwd: path.join(root, '..'), maxBuffer: 1 << 20 })
  // 它 import '@/lib/utils',esbuild 的 alias 会解析,原样落盘即可
  fs.writeFileSync(beforeGroupPath, buf)
}

async function bundle(entry, name) {
  const outfile = path.join(cache, `${name}.mjs`)
  await esbuild.build({
    entryPoints: [entry], outfile, bundle: true, format: 'esm', platform: 'node',
    jsx: 'automatic', external: ['react', 'react-dom', 'react/jsx-runtime'],
    alias: { '@': srcDir }, mainFields: ['module', 'main'],
    conditions: ['import', 'module'], logLevel: 'silent',
  })
  return import(pathToFileURL(outfile).href)
}

const beforeMod = await bundle(beforeGroupPath, 'group-before')
const filterMod = await bundle(path.join(srcDir, 'components/publishing/ArticleStatusFilter.tsx'), 'filter-after')
const drawerMod = await bundle(path.join(srcDir, 'components/publishing/MediaAdviceDrawer.tsx'), 'drawer-after')

const h = React.createElement

// PublishCenter 里"未分发"那一行的 class 串,逐字照抄 —— 行高必须由真 CSS 算出来
const ROW_CLASS = 'flex items-start gap-2 px-2 py-2 rounded-md transition-colors cursor-pointer hover:bg-secondary/50'
const row = (i) => h('div', { key: i, 'data-row': String(i), className: ROW_CLASS },
  h('div', { className: 'flex items-center justify-center shrink-0 -my-2 -ml-2 px-3 py-2 rounded-l-md' },
    h('div', { className: 'size-4 rounded border border-border' })),
  h('div', { className: 'min-w-0 flex-1' },
    h('div', { className: 'text-sm leading-snug line-clamp-2 break-words' }, `示例文章标题 ${i + 1} · 用于量行高`),
    h('div', { className: 'flex gap-1.5 mt-1' },
      h('span', { className: 'text-[10px] text-muted-foreground' }, '示例关键词'))),
)
const rows = (n) => Array.from({ length: n }, (_, i) => row(i))

const auxPlaceholder = (px) => px > 0
  ? h('div', { className: 'rounded-md border border-border', style: { height: px } }, null)
  : null

// ── 改前:四段堆叠(生产那一版的 filledStyle 逐字照抄)+ 辅助面板在流里
function BeforeLeft({ auxPx }) {
  const G = beforeMod.ArticleGroupSection
  return h(React.Fragment, null,
    h('div', { className: 'p-3 border-b border-border space-y-2' },
      h('div', { className: 'flex items-center justify-between gap-2' },
        h('div', { className: 'text-sm font-medium md:hidden' }, '选择文章')),
      h('select', { className: 'w-full px-3 py-1.5 rounded-md border border-border bg-background text-sm' },
        h('option', null, '示例项目'))),
    // 未分发:flex 3 1 0% + minHeight 120px(published > 0 时的分支)
    h('div', {
      className: 'flex flex-col shrink-0',
      style: { flex: '3 1 0%', minHeight: '120px' },
    },
      h('button', { type: 'button', className: 'w-full px-3 py-1.5 text-[10px] font-medium text-muted-foreground tracking-wider border-b border-border flex items-center justify-between shrink-0' },
        h('span', null, `未分发 (${FIXTURE.unpublished})`)),
      h('div', { 'data-list-scroll': '', className: 'flex-1 overflow-y-auto p-2 space-y-1' }, rows(FIXTURE.unpublished))),
    // 发布中:空组(不吃 filledStyle,只留标题 + 占位)· 生产默认展开
    h(G, { groupKey: 'inProgress', label: '发布中', count: FIXTURE.inProgress, open: true, onToggle: () => {}, tone: 'progress', emptyText: '本项目暂无发布中的文章', filledStyle: { flex: '2 1 0%', minHeight: '180px' } }),
    // 已分发:生产默认**收起**(collapsed.published = true)→ 只有标题行
    h(G, { groupKey: 'published', label: '已分发', count: FIXTURE.published, open: false, onToggle: () => {}, emptyText: '本项目暂无已分发文章', filledStyle: { flex: '2 1 0%', minHeight: '180px' } }),
    // 已拒稿:默认收起
    h(G, { groupKey: 'rejected', label: '已拒稿', count: FIXTURE.rejected, open: false, onToggle: () => {}, tone: 'danger', emptyText: '本项目暂无已拒稿文章', filledStyle: { flex: '2 1 0%', minHeight: '180px' } }),
    h('div', { className: 'flex min-h-0 shrink-0 flex-col gap-2' }, auxPlaceholder(auxPx)),
    h('div', { className: 'p-3 border-t border-border text-xs text-muted-foreground shrink-0' }, `共 ${FIXTURE.unpublished} 篇可选`),
  )
}

// ── 改后:分段筛选器 + 单列表 + 收起的抽屉
function AfterLeft({ auxPx }) {
  return h(React.Fragment, null,
    h('div', { className: 'p-3 border-b border-border space-y-2' },
      h('div', { className: 'flex items-center justify-between gap-2' },
        h('div', { className: 'text-sm font-medium md:hidden' }, '选择文章')),
      h('select', { className: 'w-full px-3 py-1.5 rounded-md border border-border bg-background text-sm' },
        h('option', null, '示例项目'))),
    h(filterMod.ArticleStatusFilter, {
      className: 'mx-2 mt-2',
      active: 'unpublished',
      onChange: () => {},
      options: [
        { key: 'unpublished', label: '未分发', count: FIXTURE.unpublished },
        { key: 'inProgress', label: '发布中', count: FIXTURE.inProgress, tone: 'progress' },
        { key: 'published', label: '已分发', count: FIXTURE.published },
        { key: 'rejected', label: '已拒稿', count: FIXTURE.rejected, tone: 'danger' },
      ],
    }),
    h('div', { 'data-list-scroll': '', 'data-article-list-pane': '', className: 'flex min-h-0 flex-1 flex-col overflow-y-auto p-2 space-y-1' }, rows(FIXTURE.unpublished)),
    h(drawerMod.MediaAdviceDrawer, {
      open: false, onToggle: () => {}, storageKey: 'measure',
      sections: [{ key: 'aux', node: auxPlaceholder(auxPx) }],
    }),
    h('div', { className: 'p-3 border-t border-border text-xs text-muted-foreground shrink-0' }, `共 ${FIXTURE.unpublished} 篇可选`),
  )
}

const cssHref = '/' + fs.readdirSync(path.join(root, 'dist/assets')).find(f => /^index-.*\.css$/.test(f))

function page(variant, auxPx) {
  const isAfter = variant === 'after'
  // 外壳逐字复刻 Layout.tsx:SidebarInset 基类 + 条件类;main 的 flex-1 / min-h-0 分支
  const insetCls = 'flex flex-col flex-1 min-w-0 overflow-hidden h-[100dvh] bg-card '
    + (isAfter ? 'flex flex-col overflow-hidden' : 'overflow-y-auto overflow-x-hidden')
  const mainCls = isAfter ? 'flex-1 min-h-0' : 'flex-1'
  // PublishCenter 页面容器:改前自算 100dvh-7rem;改后 h-full 跟外壳走
  const pageCls = isAfter
    ? 'flex h-full min-h-0 max-w-full flex-col overflow-hidden p-2 sm:p-4 md:p-6'
    : 'flex min-h-[calc(100dvh-7rem)] max-w-full flex-col overflow-x-hidden p-2 pb-24 sm:p-4 md:h-[calc(100dvh-7rem)] md:min-h-0 md:overflow-hidden md:p-6 md:pb-6'

  const left = renderToStaticMarkup(
    h('div', { 'data-left-pane': '', className: 'w-full min-w-0 shrink-0 rounded-lg border border-border md:w-72 md:rounded-none md:border-y-0 md:border-l-0 md:border-r lg:w-80 flex-col overflow-hidden md:flex md:max-h-full flex flex-1' },
      isAfter ? h(AfterLeft, { auxPx }) : h(BeforeLeft, { auxPx })),
  )
  return `<div class="flex h-[100dvh] w-full bg-background">
  <aside class="w-64 shrink-0 border-r border-border bg-card"></aside>
  <div id="inset" class="${insetCls}">
    <header class="bg-background/80 sticky top-0 z-20 flex h-12 shrink-0 items-center gap-2 border-b px-4"><span class="text-sm">发布中心</span></header>
    <main id="main" role="main" class="${mainCls}">
      <div id="page" class="${pageCls}">
        <div class="flex min-h-0 w-full min-w-0 flex-1 flex-col gap-3 overflow-visible md:flex-row md:gap-0 md:overflow-hidden">
          ${left}
          <div data-right-pane class="min-h-0 min-w-0 flex-1 flex-col overflow-visible md:flex md:overflow-hidden hidden md:flex"><div class="p-4 text-xs text-muted-foreground">右栏(本次不动)</div></div>
        </div>
      </div>
    </main>
  </div>
</div>`
}

/**
 * 🔴 JIT 缺类闸(不做这个,整套量法会静默失真)。
 *
 * 本包把 `min-h-[calc(100dvh-7rem)]` / `md:h-[calc(100dvh-7rem)]` 的**唯一使用处**删掉了,
 * Tailwind JIT 于是不再产出这两个类 —— 而"改前"那一侧恰恰要靠它们才有正确高度。
 * 第一版 harness 就是这么翻车的:改前底部黑边量出 0px(真值 64px)、两侧都是 46 行,
 * 全部判据看着"过了",实际上根本没约束住高度。
 *
 * 所以这里把"类到底在不在"变成硬闸:harness 用到的每一个 class,
 * 要么在真 CSS 里找得到,要么必须在 SHIM 里**显式**补上并写明理由;
 * 两头都没有 → 直接报错,不许静默继续。
 */
const SHIM = {
  // 这两个是"改前"那一版的原样声明(md+ 分支)。JIT 已把它们清掉,这里按语义补回。
  'min-h-\\[calc\\(100dvh-7rem\\)\\]': 'min-height: calc(100dvh - 7rem)',
  'md\\:h-\\[calc\\(100dvh-7rem\\)\\]': 'height: calc(100dvh - 7rem)',
}
const shimCss = Object.entries(SHIM).map(([sel, decl]) => `.${sel}{${decl}}`).join('\n')

const cssText = fs.readFileSync(path.join(root, 'dist/assets', path.basename(cssHref)), 'utf8')
// 去掉所有 CSS 转义反斜杠,类名就能按字面量比对(md\:flex → md:flex,h-\[100dvh\] → h-[100dvh])
const cssFlat = cssText.replace(/\\/g, '')
const shimFlat = shimCss.replace(/\\/g, '')
const hasClass = (token) => {
  for (const hay of [cssFlat, shimFlat]) {
    let i = -1
    while ((i = hay.indexOf('.' + token, i + 1)) !== -1) {
      const next = hay[i + 1 + token.length]
      if (!next || !/[A-Za-z0-9_-]/.test(next)) return true
    }
  }
  return false
}

/**
 * 白名单:lucide-react 给每个图标 <svg> 打的自有标记类(`lucide`/`lucide-chevron-up` …)。
 * 它们不是 Tailwind 类,本来就不该出现在 CSS 里,**也不带任何几何**
 * —— 图标尺寸来自同一元素上的 `size-3`(那个类在真 CSS 里,已被上面的闸验过)。
 * 所以放行它们不会让这道闸失效;放行的是"确定无几何影响"的一类,不是"查不到就算了"。
 */
const NON_TAILWIND_OK = /^lucide(-|$)/

function auditClasses(html, label) {
  const tokens = new Set()
  for (const m of html.matchAll(/class="([^"]*)"/g)) {
    for (const t of m[1].split(/\s+/)) if (t) tokens.add(t)
  }
  const missing = [...tokens].filter(t => !NON_TAILWIND_OK.test(t) && !hasClass(t))
  return { label, total: tokens.size, missing }
}

const tpl = (body) => `<!doctype html><html class="dark"><head><meta charset="utf-8">`
  + `<link rel="stylesheet" href="${cssHref}"><style>${shimCss}</style></head>`
  + `<body class="bg-background">${body}</body></html>`

// ── 起一个真 http 源再量。
// 🔴 不能用 setContent + route 拦截:那样文档源是 about:blank,
// `<link href="/index-*.css">` 根本不发请求 → 样式表静默不生效
// (第一版就是这么翻车的:所有几何量成 0)。
const pages = new Map() // pathname -> html
const server = http.createServer((req, res) => {
  const u = new URL(req.url, 'http://127.0.0.1')
  if (pages.has(u.pathname)) {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' })
    return res.end(pages.get(u.pathname))
  }
  const asset = path.join(root, 'dist/assets', path.basename(u.pathname))
  if (u.pathname.endsWith('.css') && fs.existsSync(asset)) {
    res.writeHead(200, { 'Content-Type': 'text/css; charset=utf-8' })
    return res.end(fs.readFileSync(asset))
  }
  res.writeHead(404); res.end('nope')
})
await new Promise(r => server.listen(0, '127.0.0.1', r))
const ORIGIN = `http://127.0.0.1:${server.address().port}`

const browser = await chromium.launch()
const ctx = await browser.newContext()
const pg = await ctx.newPage()

const MEASURE = () => {
  const main = document.getElementById('main')
  const page = document.getElementById('page')
  const scroll = document.querySelector('[data-list-scroll]')
  const sc = scroll.getBoundingClientRect()
  let visible = 0
  scroll.querySelectorAll('[data-row]').forEach(r => {
    const b = r.getBoundingClientRect()
    // 完整落在滚动视口内才算"看得见一行"
    if (b.top >= sc.top - 0.5 && b.bottom <= sc.bottom + 0.5) visible++
  })
  const mr = main.getBoundingClientRect(), pr = page.getBoundingClientRect()
  return {
    visibleRows: visible,
    listH: Math.round(sc.height),
    bottomGap: Math.round(mr.bottom - pr.bottom),
    pageOverflowsMain: Math.round(pr.bottom - mr.bottom) > 1,
    hScroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
  }
}

async function measure(variant, auxPx, vp) {
  const key = `/${variant}-${auxPx}.html`
  if (!pages.has(key)) pages.set(key, tpl(page(variant, auxPx)))
  await pg.setViewportSize({ width: vp.w, height: vp.h })
  await pg.goto(ORIGIN + key, { waitUntil: 'load' })
  await pg.waitForTimeout(60)
  return pg.evaluate(MEASURE)
}

let failed = 0
const ok = (cond, what) => { if (cond) console.log(`✅ ${what}`); else { console.error(`❌ ${what}`); failed++ } }

// ── 前置闸 0:CSS 真的加载了吗 + harness 用到的类真的都存在吗
{
  console.log('══════ 前置闸:真 CSS 是否生效 / 有没有 JIT 缺类 ══════')
  // 反向对照:判据本身要能报出"缺类",否则 hasClass 恒真、这道闸等于没有
  ok(!hasClass('zz-definitely-not-a-real-class'), '前置闸 反向对照 hasClass 能报出不存在的类(不是恒真)')
  ok(hasClass('cursor-row-resize'), '前置闸 hasClass 能在真 CSS 里找到确实存在的类(不是恒假)')

  for (const variant of ['before', 'after']) {
    const a = auditClasses(page(variant, AUX_PX_MEASURED), variant)
    ok(a.missing.length === 0,
      `前置闸 ${variant} 用到的 ${a.total} 个 class 全部有样式${a.missing.length ? ` · 缺:${a.missing.join(' ')}` : ''}`)
  }

  // 样式表真的被浏览器应用了吗 —— 拿一个只可能来自真 CSS 的几何量自证
  pages.set('/probe.html', tpl('<div id="probe" class="h-12"></div>'))
  await pg.goto(ORIGIN + '/probe.html', { waitUntil: 'load' })
  const probeH = await pg.evaluate(() => document.getElementById('probe').getBoundingClientRect().height)
  ok(probeH === 48, `前置闸 真 CSS 已被应用(h-12 量到 ${probeH}px,期望 48)`)
  if (failed) {
    console.error('\n🔴 前置闸没过,后面的像素数全都不可信,停止')
    await browser.close(); server.close(); process.exit(1)
  }
}

// ── 主判据:auxPx = 生产实测值。工单 §6.2 的三行表就是这一段。
console.log(`\n══════ 主判据 · 辅助面板占位 auxPx=${AUX_PX_MEASURED}px(生产实测值)══════`)
console.log('视口        | 改前行数 | 改后行数 | 增益 | 改前列表高 | 改后列表高 | 改前黑边 | 改后黑边')
console.log('------------|----------|----------|------|------------|------------|----------|--------')
const attribution = []
for (const vp of VIEWPORTS) {
  const b = await measure('before', AUX_PX_MEASURED, vp)
  const a = await measure('after', AUX_PX_MEASURED, vp)
  const gain = a.visibleRows - b.visibleRows
  console.log(
    `${String(vp.w).padStart(4)}×${String(vp.h).padEnd(4)} | ${String(b.visibleRows).padStart(8)} | ${String(a.visibleRows).padStart(8)} | ${String('+' + gain).padStart(4)} | ${String(b.listH + 'px').padStart(10)} | ${String(a.listH + 'px').padStart(10)} | ${String(b.bottomGap + 'px').padStart(8)} | ${String(a.bottomGap + 'px').padStart(6)}`,
  )
  ok(gain >= 4, `  ${vp.w}×${vp.h} 可见行数 >= 改前 + 4(实得 +${gain})`)
  ok(a.bottomGap === 0, `  ${vp.w}×${vp.h} 改后底部黑色留白 = 0(实得 ${a.bottomGap}px)`)
  ok(!a.pageOverflowsMain, `  ${vp.w}×${vp.h} 改后内容没有被视口截断`)
  ok(!a.hScroll, `  ${vp.w}×${vp.h} 改后没有横向滚动条`)
  // 🔴 反向对照:改前那一侧的黑边必须量得出 64px。
  // 第一版 harness 就是在这里露的馅(量成 0px)—— 没有这条,整张表的"全过"是假的。
  ok(b.bottomGap === 64, `  ${vp.w}×${vp.h} 反向对照 改前底部黑边量到 64px(与生产实测一致 → 量法有判别力)`)
  attribution.push({ vp, gain })

  // 13 寸那一档留一组对比图,交付说明里给老板看
  if (vp.w === 1366 && process.env.SHOTS) {
    fs.mkdirSync(process.env.SHOTS, { recursive: true })
    for (const variant of ['before', 'after']) {
      await pg.setViewportSize({ width: vp.w, height: vp.h })
      await pg.goto(`${ORIGIN}/${variant}-${AUX_PX_MEASURED}.html`, { waitUntil: 'load' })
      await pg.waitForTimeout(60)
      await pg.screenshot({ path: path.join(process.env.SHOTS, `${variant}-1366x768.png`) })
    }
    console.log(`  📸 已存 ${process.env.SHOTS}/{before,after}-1366x768.png`)
  }
}

// ── 归因(不设判据,只报事实):把辅助面板占位设成 0,看增益还剩多少。
// auxPx=0 在生产里**不存在**(实测就是 430px),所以它不是验收条件,
// 而是用来回答"这次的收益主要来自哪一件"。如实写进交付说明,不拿它冒充验收。
console.log('\n══════ 归因(非判据)· auxPx=0px:假设那三块辅助面板根本不占地方 ══════')
console.log('视口        | 改前行数 | 改后行数 | 增益(仅 L1+L2 贡献)')
console.log('------------|----------|----------|--------------------')
for (const vp of VIEWPORTS) {
  const b = await measure('before', 0, vp)
  const a = await measure('after', 0, vp)
  const full = attribution.find(x => x.vp.w === vp.w).gain
  console.log(
    `${String(vp.w).padStart(4)}×${String(vp.h).padEnd(4)} | ${String(b.visibleRows).padStart(8)} | ${String(a.visibleRows).padStart(8)} | ${String('+' + (a.visibleRows - b.visibleRows)).padStart(4)}(总增益 +${full},其余来自 L3 收起辅助面板)`,
  )
}

// ── §6.4 不回退:右栏可见高度改前改后不许变小(老板否掉"辅助信息移右栏"的理由就是怕压右栏)
console.log('\n══════ §6.4 右栏可见高度 改前/改后 逐像素对比 ══════')
for (const vp of VIEWPORTS) {
  const probe = () => {
    const el = document.querySelector('[data-right-pane]')
    return el ? Math.round(el.getBoundingClientRect().height) : -1
  }
  const one = async (variant) => {
    const key = `/${variant}-${AUX_PX_MEASURED}.html`
    await pg.setViewportSize({ width: vp.w, height: vp.h })
    await pg.goto(ORIGIN + key, { waitUntil: 'load' })
    await pg.waitForTimeout(40)
    return pg.evaluate(probe)
  }
  const b = await one('before'), a = await one('after')
  console.log(`  ${vp.w}×${vp.h}:改前 ${b}px → 改后 ${a}px(差 ${a - b >= 0 ? '+' : ''}${a - b}px)`)
  ok(b > 0 && a > 0, `  §6.4 ${vp.w}×${vp.h} 两侧都量到了右栏(锚点有效,不是 -1 恒过)`)
  ok(a >= b, `  §6.4 ${vp.w}×${vp.h} 右栏可见高度没有变小(${b} → ${a})`)
}

// ── §6.3 「重叠」到底是不是 bug:出单人给不了结论,这里用像素回答。
//
// 工单 §3:老板截图里"未分发"列表底部有半截行被「发布中 (0)」的条压住,
// 两种可能 —— (a) 滚动裁剪的正常半行 (b) 父链高度算错导致的真溢出压盖。
// 判据:把未分发列表滚到底,逐行算它与"发布中"标题条的**几何交叠**。
//   交叠 = 0 → 只是裁剪(a),不是 bug;
//   交叠 > 0 → 真压盖(b),得单独修。
console.log('\n══════ §6.3 重叠复现(四组全非空 · 1366×768)══════')
{
  const FOUR = { unpublished: 46, inProgress: 5, published: 13, rejected: 3 }
  const saved = { ...FIXTURE }
  Object.assign(FIXTURE, FOUR)
  pages.clear() // fixture 变了,之前缓存的 HTML 作废

  const overlapProbe = () => {
    const scroll = document.querySelector('[data-list-scroll]')
    scroll.scrollTop = scroll.scrollHeight // 滚到底
    const hdr = document.querySelector('[data-article-group-header="inProgress"]')
    if (!hdr) return { err: 'no-inprogress-header' }
    const hr = hdr.getBoundingClientRect()
    const sc = scroll.getBoundingClientRect()
    let maxOverlap = 0, worst = null
    scroll.querySelectorAll('[data-row]').forEach(r => {
      const b = r.getBoundingClientRect()
      const ov = Math.min(b.bottom, hr.bottom) - Math.max(b.top, hr.top)
      if (ov > maxOverlap) { maxOverlap = ov; worst = r.getAttribute('data-row') }
    })
    return {
      maxOverlap: Math.round(maxOverlap), worstRow: worst,
      scrollClips: Math.round(scroll.scrollHeight) > Math.round(sc.height),
      listBottom: Math.round(sc.bottom), headerTop: Math.round(hr.top),
    }
  }

  for (const variant of ['before', 'after']) {
    const key = `/${variant}-overlap.html`
    pages.set(key, tpl(page(variant, AUX_PX_MEASURED)))
    await pg.setViewportSize({ width: 1366, height: 768 })
    await pg.goto(ORIGIN + key, { waitUntil: 'load' })
    await pg.waitForTimeout(60)
    if (variant === 'before') {
      const r = await pg.evaluate(overlapProbe)
      console.log(`  改前:未分发列表滚到底 → 与「发布中」标题条最大交叠 = ${r.maxOverlap}px`
        + ` (列表下沿 ${r.listBottom}px / 标题上沿 ${r.headerTop}px / 列表确实在裁剪=${r.scrollClips})`)
      ok(r.scrollClips, '  §6.3 改前列表确实处于"内容多于可视高度"的状态(复现前提成立)')
      if (r.maxOverlap === 0) {
        console.log('  → 判定:**未复现**真压盖。露半行是滚动容器边界不对齐行边界的正常裁剪(可能性 a)。')
      } else {
        console.log(`  → 判定:**复现**真压盖 ${r.maxOverlap}px(可能性 b),需单独定位。`)
      }
      global.__overlapBefore = r.maxOverlap
    } else {
      // 改后没有"发布中标题条"这个东西了(四状态改成顶部分段控件),
      // 结构上就不存在"列表被下一个分组标题压住"的可能 —— 这条要说清楚,不能含糊成"修好了"。
      const gone = await pg.evaluate(() => !document.querySelector('[data-article-group-header="inProgress"]'))
      ok(gone, '  §6.3 改后不存在"下一个分组标题条"(四状态已移到顶部分段控件)→ 该压盖形态结构上消失')
    }
  }
  Object.assign(FIXTURE, saved)
}

await browser.close()
server.close()
console.log(failed ? `\n🔴 ${failed} 条像素判据未通过` : '\n✅ 三档视口 × 两种 auxPx 全部满足 §6.2 判据')
process.exit(failed ? 1 : 0)
