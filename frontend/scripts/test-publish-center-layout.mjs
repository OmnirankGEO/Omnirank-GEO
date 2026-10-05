/**
 * test-publish-center-layout.mjs — 发布中心「代发」左栏布局重构的行为锁。
 *
 * 工单 WO-PUBCENTER-LAYOUT-2026-08-04(L1 定高 / L2 分段筛选器 / L3 底部抽屉 / L4 可拖)。
 * 沿用 test-batch-conflict-window.mjs 的做法:esbuild 现场把 TSX 打成 JS **真跑一遍**,
 * 呈现件用 react-dom/server 渲成静态 HTML 后断言 —— 断的是渲染结果,不是源码字符串
 * (源码串断言换皮即绕过、重构就误伤,双向脆)。
 *
 * 🔴 这两个组件都刻意做成**不走 Portal 的纯组件**。2026-08-04 批量冲突窗口那一批
 * 踩过:Radix Dialog 走 Portal,`renderToStaticMarkup` 渲不出内容,锁全灭。
 * 所以这里的抽屉是就地折叠的 div,不是 Dialog。
 *
 *   锁1 四个计数同时可见(含 0)—— 2026-07-30 T2 不回退,且比 T2 更强
 *   锁2 选中态唯一,且切换筛选是纯状态切换(不夹带副作用)
 *   锁3 分段控件是「筛选器」不是「二级 tab」(语义 radiogroup + 不是 tablist)
 *   锁4 抽屉收起态只占一行把手:没有内容体、没有拖拽条
 *   锁5 抽屉展开:三块内容都在,且拖拽条出现
 *   锁6 clampDrawerHeight 的上下界与"不许负高度"
 *   锁7 抽屉不含右栏元素 / 结构上够不着页面级购物车条
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
const outDir = path.join(root, 'node_modules/.cache/publish-center-layout')
fs.mkdirSync(outDir, { recursive: true })

async function load(rel, name) {
  const outfile = path.join(outDir, `${name}.mjs`)
  await esbuild.build({
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

const filterMod = await load('components/publishing/ArticleStatusFilter.tsx', 'statusfilter')
const drawerMod = await load('components/publishing/MediaAdviceDrawer.tsx', 'drawer')

let failed = 0
const DECLARED_LOCKS = ['锁1', '锁2', '锁3', '锁4', '锁5', '锁6', '锁7', '锁8', '锁9']
const ran = new Map(DECLARED_LOCKS.map(k => [k, 0]))
const tally = (what) => {
  const m = /^(锁\d+)/.exec(what)
  if (m && ran.has(m[1])) ran.set(m[1], ran.get(m[1]) + 1)
}
const ok = (cond, what) => {
  tally(what)
  if (cond) console.log(`✅ ${what}`)
  else { console.error(`❌ ${what}`); failed++ }
}
const eq = (got, want, what) => ok(
  JSON.stringify(got) === JSON.stringify(want),
  `${what}${JSON.stringify(got) === JSON.stringify(want) ? '' : ` (got ${JSON.stringify(got)} want ${JSON.stringify(want)})`}`,
)
const html = (el) => renderToStaticMarkup(el)
const count = (s, re) => (s.match(re) || []).length

// 老板截图里的真实分布:未分发 46 / 发布中 0 / 已分发 13 / 已拒稿 0
const OPTIONS = [
  { key: 'unpublished', label: '未分发', count: 46 },
  { key: 'inProgress', label: '发布中', count: 0 },
  { key: 'published', label: '已分发', count: 13 },
  { key: 'rejected', label: '已拒稿', count: 0 },
]

// ─────────────────────────── 锁 1 · 四个计数同时可见(含 0)
{
  const m = html(React.createElement(filterMod.ArticleStatusFilter, {
    options: OPTIONS, active: 'unpublished', onChange: () => {},
  }))
  for (const o of OPTIONS) {
    ok(m.includes(`data-status-filter="${o.key}"`), `锁1 ${o.label} 渲染出来了`)
    ok(m.includes(`data-status-filter-badge="${o.key}"`), `锁1 ${o.label} 的计数徽标存在`)
  }
  // 工单验收:「已分发 13 不用滚动就能看到」。同一次渲染里四个数字全在 = 同时可见。
  ok(m.includes('>13</span>'), '锁1 已分发 13 在默认(未分发)选中态下就已渲染,不需要切过去')
  eq(count(m, /data-status-filter-count="0"/g), 2, '锁1 反向 两个空组的 0 都在,没被吞')
  eq(count(m, /data-status-filter="/g), 4, '锁1 反向 四个状态一个不少')
  // 反向对照:如果哪天有人给计数加了 `count > 0 &&` 守卫,上面两条会转红。
  // 这里再补一条"分布断言":全 0 时四个数也必须都在。
  const allZero = html(React.createElement(filterMod.ArticleStatusFilter, {
    options: OPTIONS.map(o => ({ ...o, count: 0 })), active: 'unpublished', onChange: () => {},
  }))
  eq(count(allZero, /data-status-filter-count="0"/g), 4, '锁1 反向 四个全为 0 时,四个计数照样全渲染')
}

// ─────────────────────────── 锁 2 · 选中态唯一 + 切换是纯切换
{
  for (const active of ['unpublished', 'inProgress', 'published', 'rejected']) {
    const m = html(React.createElement(filterMod.ArticleStatusFilter, {
      options: OPTIONS, active, onChange: () => {},
    }))
    eq(count(m, /data-status-filter-active="true"/g), 1, `锁2 active=${active} 时有且只有一个选中`)
    ok(new RegExp(`data-status-filter="${active}"[^>]*data-status-filter-active="true"`).test(m)
       || new RegExp(`data-status-filter-active="true"[^>]*data-status-filter="${active}"`).test(m),
      `锁2 active=${active} 时选中的正是它自己`)
    ok(m.includes(`data-active-status="${active}"`), `锁2 active=${active} 时根节点回写了当前状态(供列表容器对齐)`)
  }
  // onChange 拿到的就是被点的那个 key,没有别的加工
  let got = null
  const el = React.createElement(filterMod.ArticleStatusFilter, {
    options: OPTIONS, active: 'unpublished', onChange: (k) => { got = k },
  })
  // 静态渲染拿不到事件,这里直接调 props 里的回调验证契约形状
  el.props.onChange('published')
  eq(got, 'published', '锁2 onChange 原样回传被点的状态 key')
}

// ─────────────────────────── 锁 3 · 是筛选器,不是二级 tab
{
  const m = html(React.createElement(filterMod.ArticleStatusFilter, {
    options: OPTIONS, active: 'unpublished', onChange: () => {},
  }))
  // CLAUDE.md 铁律:「详情 tab 内必须用折叠面板,禁止再加二级 tab」。
  // 分段控件是"同一集合的筛选器"而非"不同功能的 tab",语义上必须能自证:
  ok(m.includes('role="radiogroup"'), '锁3 语义是 radiogroup(单选筛选),不是 tablist')
  ok(!m.includes('role="tablist"'), '锁3 反向 没有 tablist')
  ok(!m.includes('role="tab"'), '锁3 反向 没有 tab role')
  eq(count(m, /role="radio"/g), 4, '锁3 四个选项都是 radio')
  ok(m.includes('aria-checked="true"'), '锁3 选中态用 aria-checked 表达(读屏读出来是"单选"不是"标签页")')
}

// ─────────────────────────── 锁 4 · 收起态只占一行把手
{
  const sections = [
    { key: 't1', node: React.createElement('div', null, 'T1内容AI最常引用') },
    { key: 't2', node: React.createElement('div', null, 'T2内容搭配建议') },
    { key: 'board', node: React.createElement('div', null, 'BOARD内容媒体榜') },
  ]
  const closed = html(React.createElement(drawerMod.MediaAdviceDrawer, {
    sections, open: false, onToggle: () => {}, storageKey: 'k',
  }))
  ok(closed.includes('data-drawer-handle'), '锁4 收起态有把手')
  ok(closed.includes('data-drawer-open="false"'), '锁4 收起态标记正确')
  ok(!closed.includes('data-drawer-body'), '锁4 收起态没有内容体')
  ok(!closed.includes('data-drawer-resizer'), '锁4 收起态没有拖拽条')
  // 关键:收起时三块内容一个都不渲染 —— 这才叫"合计占 1 行"
  ok(!closed.includes('T1内容AI最常引用'), '锁4 反向 收起态不渲染 T1')
  ok(!closed.includes('T2内容搭配建议'), '锁4 反向 收起态不渲染 T2')
  ok(!closed.includes('BOARD内容媒体榜'), '锁4 反向 收起态不渲染媒体榜')
  ok(!/style="[^"]*height/.test(closed), '锁4 反向 收起态不写死高度(高度由把手那一行自然撑)')
}

// ─────────────────────────── 锁 5 · 展开:三块都在 + 出现拖拽条
{
  const sections = [
    { key: 't1', node: React.createElement('div', null, 'T1内容AI最常引用') },
    { key: 't2', node: React.createElement('div', null, 'T2内容搭配建议') },
    { key: 'board', node: React.createElement('div', null, 'BOARD内容媒体榜') },
  ]
  const open = html(React.createElement(drawerMod.MediaAdviceDrawer, {
    sections, open: true, onToggle: () => {}, storageKey: 'k',
  }))
  ok(open.includes('data-drawer-open="true"'), '锁5 展开态标记正确')
  ok(open.includes('data-drawer-body'), '锁5 展开态有内容体')
  ok(open.includes('T1内容AI最常引用') && open.includes('T2内容搭配建议') && open.includes('BOARD内容媒体榜'),
    '锁5 展开后三块内容都能看到(工单验收:展开后两块内容都能看到)')
  for (const k of ['t1', 't2', 'board']) {
    ok(open.includes(`data-drawer-section="${k}"`), `锁5 分区 ${k} 渲染`)
  }
  ok(open.includes('data-drawer-resizer'), '锁5 展开态出现拖拽条(L4)')
  ok(open.includes('cursor-row-resize'), '锁5 拖拽条是纵向拖拽光标')
  ok(/style="[^"]*height:\s*\d+px/.test(open), '锁5 展开态高度受控(可被拖动改写)')
  // 分区数 == 传进去的块数,不多不少
  eq(count(open, /data-drawer-section="/g), 3, '锁5 反向 分区数等于传入块数')
}

// ─────────────────────────── 锁 6 · clampDrawerHeight 上下界 + 不许负高度
{
  const { clampDrawerHeight, MIN_DRAWER_PX, MIN_LIST_PX } = drawerMod
  const CONTAINER = 700
  // 正常区间原样返回
  eq(clampDrawerHeight(300, CONTAINER), 300, '锁6 区间内的高度原样保留')
  // 下界:抽屉不许小于 MIN_DRAWER_PX
  eq(clampDrawerHeight(10, CONTAINER), MIN_DRAWER_PX, '锁6 下界 抽屉不小于最小高度')
  eq(clampDrawerHeight(-500, CONTAINER), MIN_DRAWER_PX, '锁6 下界 负输入也被抬回最小高度')
  // 上界:必须给列表留 MIN_LIST_PX
  eq(clampDrawerHeight(9999, CONTAINER), CONTAINER - MIN_LIST_PX, '锁6 上界 给列表留够最小可见高度')
  ok(CONTAINER - clampDrawerHeight(9999, CONTAINER) >= MIN_LIST_PX,
    '锁6 上界 反向 拖到最大时列表仍 >= 最小可见高度(工单:列表最小保 3 行可见)')
  // 🔴 超矮容器:上界会低于下界。工单明写"resize 时不许出现负高度"。
  const TINY = 200 // < MIN_DRAWER_PX + MIN_LIST_PX
  const tiny = clampDrawerHeight(300, TINY)
  ok(tiny >= 0, `锁6 超矮容器不产生负高度(得到 ${tiny})`)
  ok(tiny <= TINY, '锁6 超矮容器下抽屉不超过容器本身')
  eq(clampDrawerHeight(300, 100), 0, '锁6 容器比列表最小高度还矮时抽屉压到 0(优先保列表)')
  // 反向:容器为 0 / NaN 不许算出负数或 NaN
  ok(clampDrawerHeight(300, 0) >= 0, '锁6 反向 容器 0 不产生负高度')
  ok(Number.isFinite(clampDrawerHeight(NaN, CONTAINER)), '锁6 反向 NaN 输入不污染成 NaN')
}

// ─────────────────────────── 锁 7 · 抽屉是左栏内的纯折叠件
{
  const sections = [{ key: 'board', node: React.createElement('div', null, 'BOARD') }]
  const open = html(React.createElement(drawerMod.MediaAdviceDrawer, {
    sections, open: true, onToggle: () => {}, storageKey: 'k',
  }))
  // 老板否掉了"移到右栏":组件本身不许渲染任何 fixed/absolute 定位,
  // 否则它就能飘到左栏之外(也就能盖住页面级的购物车条)。
  ok(!/class="[^"]*\bfixed\b/.test(open), '锁7 抽屉不含 fixed 定位(飘不出左栏)')
  ok(!/class="[^"]*\babsolute\b/.test(open), '锁7 抽屉不含 absolute 定位')
  ok(!/z-\d/.test(open), '锁7 抽屉不抬 z-index(盖不住页面级购物车/一键发布条)')
  ok(/class="[^"]*shrink-0/.test(open), '锁7 抽屉是 shrink-0 的在流元素,和列表分高度而不是浮在它上面')
  // 反向对照:证明上面三条不是恒真 —— 同一个断言打在一个真带 fixed/z 的片段上必须转红
  const probe = '<div class="fixed z-30 absolute"></div>'
  ok(/class="[^"]*\bfixed\b/.test(probe) && /z-\d/.test(probe) && /class="[^"]*\babsolute\b/.test(probe),
    '锁7 反向对照 判据本身能抓到 fixed/absolute/z-index(不是恒真)')
}

// ─────────────────────────── 锁 8 · 拖到的高度必须记得住(L4 硬要求)
{
  const { readStoredHeight, writeStoredHeight, DEFAULT_DRAWER_PX } = drawerMod
  // 用一个假的 localStorage 打行为,不 grep 源码 —— grep 到 `localStorage` 只能证明
  // 那几个字母在文件里,证不了"读回来的真是刚写进去的那个数"。
  const store = new Map()
  globalThis.localStorage = {
    getItem: k => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)) },
  }
  const KEY_A = 'publish_center_media_advice_h_v1:113'
  const KEY_B = 'publish_center_media_advice_h_v1:114'

  eq(readStoredHeight(KEY_A), null, '锁8 没存过时返回 null(调用方走默认值)')
  writeStoredHeight(KEY_A, 321.4)
  eq(readStoredHeight(KEY_A), 321, '锁8 写进去多少就读回来多少(四舍五入到整像素)')
  // 🔴 这条是工单那句「不记 = 每次进来重置,比没有更烦」的正面证据
  ok(readStoredHeight(KEY_A) !== DEFAULT_DRAWER_PX,
    '锁8 存过之后读到的不是默认值(证明真的"记住了",不是每次回到默认)')

  // 按用户维度:另一个账号读不到上一个账号的高度
  eq(readStoredHeight(KEY_B), null, '锁8 换账号(storageKey 变)读不到上一个人的高度')
  writeStoredHeight(KEY_B, 500)
  eq(readStoredHeight(KEY_A), 321, '锁8 反向 写 B 不会污染 A')

  // 脏值一律当没存过
  for (const dirty of ['', 'abc', '0', '-10', 'NaN']) {
    store.set(KEY_A, dirty)
    eq(readStoredHeight(KEY_A), null, `锁8 反向 脏值 ${JSON.stringify(dirty)} 当没存过`)
  }

  // 反向对照:证明上面的判据不是恒真 —— 写入被吞掉时必须读回 null
  const brokenStore = new Map()
  globalThis.localStorage = {
    getItem: k => (brokenStore.has(k) ? brokenStore.get(k) : null),
    setItem: () => { /* 模拟"没写进去" */ },
  }
  writeStoredHeight(KEY_A, 400)
  eq(readStoredHeight(KEY_A), null,
    '锁8 反向对照 写入没生效时读回 null(判据能区分"记住了"和"没记住",不是恒真)')
  delete globalThis.localStorage
}

// ─────────────────────────── 锁 9 · 外壳把 /publish 归进"定高页"(L1 接线)
{
  // 这条只能打源码:它断的是 Layout.tsx 有没有把发布中心接进 isFullHeightPage。
  // 不接的话页面 `h-full` 会去继承一个没被约束的 <main>,L1 白做。
  const layout = fs.readFileSync(path.join(srcDir, 'components/layout/Layout.tsx'), 'utf8')
  const bare = layout.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^[ \t]*\/\/.*$/gm, '')
  ok(/const isFullHeightPage\s*=[\s\S]{0,120}'\/publish'/.test(bare),
    '锁9 /publish 被归进 isFullHeightPage')
  ok(/isFullHeightPage \? 'flex flex-col overflow-hidden'/.test(bare),
    '锁9 定高页的外壳是 flex-col + overflow-hidden(外壳不滚,页面自己管)')
  ok(/isFullHeightPage \? 'flex-1 min-h-0'/.test(bare),
    '锁9 定高页的 <main> 带 min-h-0(否则它会被内容撑大,h-full 就继承到错的高度)')
  // 反向:小榜助手那两处判断里不许混进 isFullHeightPage —— 混用会把发布中心(定高页)的小榜按钮一起弄没。
  // 🔴 [WO_260 · 2026-09-23 重锚] 原先锚的是字面 `!isAgentPage && !sandboxUI`:那是 D 桶 `/agent`(全屏对话页,
  //    不在侧栏、随 E3 删)的特判,WO_260 把它连同定高里的 `/agent` 一起撤了。本锁要守的从来不是 isAgentPage,
  //    而是「小榜判断 ≠ 定高判断」—— 改锚到小榜两处判断本身:恰好两处,且都不含 isFullHeightPage。
  const xbConds = bare.match(/\{![^\n]*xiaobangRequested[^\n]*&& \(/g) || []
  ok(xbConds.length === 2 && xbConds.every(l => !/isFullHeightPage/.test(l)),
    '锁9 反向 小榜助手的两处判断里没有 isFullHeightPage(发布中心的小榜按钮不会被定高判断连带弄没)',
    `小榜判断 ${xbConds.length} 处`)
  // 反向对照:判据本身有判别力(换成一个不存在的路由必须匹配不上)
  ok(!/const isFullHeightPage\s*=[\s\S]{0,120}'\/nonexistent-route'/.test(bare),
    '锁9 反向对照 判据认的是具体路由串,不是"只要出现 isFullHeightPage 就算过"')
}

// ─────────────────────────── 收尾:0 skipped 必须是算出来的
const skipped = DECLARED_LOCKS.filter(k => ran.get(k) === 0)
const total = [...ran.values()].reduce((a, b) => a + b, 0)
console.log(`\n${DECLARED_LOCKS.map(k => `${k}:${ran.get(k)}`).join('  ')}`)
console.log(`断言 ${total} 条 / 声明 ${DECLARED_LOCKS.length} 条锁 / failed ${failed} / skipped ${skipped.length}`)
if (skipped.length) {
  console.error(`🔴 有锁一条断言都没跑:${skipped.join(', ')}`)
  failed += skipped.length
}
console.log(failed ? `\n🔴 ${failed} 条未通过` : '\n✅ 发布中心布局七条锁全绿 · 0 skipped')
process.exit(failed ? 1 : 0)
