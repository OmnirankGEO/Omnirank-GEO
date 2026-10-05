/**
 * test-publish-center-scope.mjs — 发布中心「作用域 / 空组可见 / 严审出口」八条锁。
 *
 * 仓库没有前端单测框架(无 vitest/jest),沿用 test-publication-contract-logic.mjs
 * 的做法:esbuild 现场把 TS/TSX 打成 JS 再 import,**真的跑一遍行为**。
 * 呈现件用 react-dom/server 渲成静态 HTML 后断言 —— 断的是渲染结果,
 * 不是源码字符串(源码串断言换皮即绕过、重构就误伤,双向脆)。
 *
 * 对应工单(2026-07-30 发布中心三处)§4.1 的八条锁:
 *   锁1 T1 同源      锁2 T1 反向     锁3 T1 文案      锁4 T2 空组可见
 *   锁5 T2 隐藏提示  锁6 T3 面板渲染 锁7 T3 反向      锁8 T3 换档
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
const outDir = path.join(root, 'node_modules/.cache/publish-center-scope')
fs.mkdirSync(outDir, { recursive: true })

/**
 * GovernanceAlert 的 `api` 类动作里有三处 `await import(...)`(AuthContext / api / lazyToast)。
 * 本锁只渲染,永远走不到那个分支;但 esbuild 仍会去解析它们,顺带把 axios/sonner/
 * demoReport.md?raw 整条依赖链拖进来。这里把它们换成空壳 —— 换掉的是**渲染路径之外**
 * 的东西,渲染结果一字不差。
 */
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

/** 把一个入口连同它的相对/别名依赖打成单文件 ESM。React 走 external,复用本进程的实例。 */
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
    // lucide-react 等包同时出 CJS/ESM;不指定就会拿到 CJS,打进 ESM 后
    // `require("react")` 直接炸("Dynamic require is not supported")。
    mainFields: ['module', 'main'],
    conditions: ['import', 'module'],
    logLevel: 'silent',
  })
  return import(pathToFileURL(outfile).href)
}

const scope = await load('pages/Publishing/publishCenterScopeLogic.ts', 'scope')
const strict = await load('contracts/strictMediaPresubmit.ts', 'strict')
// [WO-PUBCENTER-LAYOUT 2026-08-04] 锁4 的承载体由 ArticleGroupSection 换成分段筛选器
// (前者布局重构后已无调用方并删除)。锁的语义不变:空组的计数不许消失。
const filterMod = await load('components/publishing/ArticleStatusFilter.tsx', 'statusfilter')
const noticeMod = await load('components/publishing/HiddenArticlesNotice.tsx', 'notice')
const panelMod = await load('components/publishing/StrictPresubmitPanel.tsx', 'panel')

let failed = 0
/**
 * 每条锁实际跑了几条断言。
 * 🔴 「0 skipped」必须是**算出来的**,不是打印一句话:某条锁的代码块若被注释掉、
 *    或在第一条断言前就抛异常,它的计数就是 0 —— 收尾时按 skipped 处理并判失败。
 *    (SKIP 不是 PASS;而"整块没跑"正是最容易被当成 PASS 的一种 SKIP。)
 */
const DECLARED_LOCKS = ['锁1', '锁2', '锁3', '锁4', '锁5', '锁6', '锁7', '锁8', '锁9', '锁10']
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
const ok = (cond, what) => eq(Boolean(cond), true, what)
const html = (el) => renderToStaticMarkup(el)

// ─────────────────────────────────────────────────────────────── 共用夹具
// 四组各造一条 + 购物车占一条 + 一条"未分发但没过审"。
const A = (id, extra = {}) => ({ id, article_id: id, title: `文章${id}`, publication_eligible: true, ...extra })
const ARTICLES = [
  A(101),                       // 未分发 · 过审
  A(102),                       // 未分发 · 过审
  A(103, { publication_eligible: false }), // 未分发 · **没过审**
  A(201),                       // 已发布
  A(301),                       // 发布中
  A(401),                       // 已拒稿
  A(501),                       // 在购物车里
  A(601, { is_optimize: true }), // 未分发 · 优化文
]
const STATS = new Map([
  [201, { status: 'published' }],
  [301, { status: 'in_progress' }],
  [401, { status: 'rejected' }],
  [101, { status: 'none' }], [102, { status: 'none' }], [103, { status: 'none' }],
  [501, { status: 'none' }], [601, { status: 'none' }],
])

// ───────────────────────────────────────────── 锁 1 · T1 全选与「未分发」同源
{
  const r = scope.partitionArticles({
    articles: ARTICLES, cartArticleIds: [501], stats: STATS,
  })
  const target = scope.selectAllUnpublishedTarget(r)
  const picked = [...scope.toggleSelectAllUnpublished(new Set(), target)].sort((a, b) => a - b)

  eq(r.unpublished.map(a => a.id).sort((a, b) => a - b), [101, 102, 103, 601],
    '锁1 未分发分组 = 4 篇(购物车那篇已排除)')
  eq(picked, [101, 102, 601], '锁1 全选结果 = 「未分发且已过审」的 id 集合')
  eq(target.groupCount, r.unpublished.length, '锁1 groupCount == 未分发分组里的篇数')
  // 分布断言:选中/不选中两种结果都必须出现,不能全选或全不选。
  ok(picked.length > 0 && picked.length < ARTICLES.length, '锁1 分布:既有被选中的也有没被选中的')
}

// ─────────────────────────────────── 锁 2 · T1 反向:四类都不许被全选带上
{
  const r = scope.partitionArticles({ articles: ARTICLES, cartArticleIds: [501], stats: STATS })
  const target = scope.selectAllUnpublishedTarget(r)
  const picked = new Set(scope.toggleSelectAllUnpublished(new Set(), target))
  eq(picked.has(201), false, '锁2 已发布(201)不在全选结果里')
  eq(picked.has(301), false, '锁2 发布中(301)不在全选结果里')
  eq(picked.has(401), false, '锁2 已拒稿(401)不在全选结果里')
  eq(picked.has(501), false, '锁2 购物车里那篇(501)不在全选结果里')
  eq(picked.has(103), false, '锁2 未分发但没过审(103)不在全选结果里')

  // 手点其它分组的勾选**不被全选清掉**(「已发布的可以单独勾」是保留能力)
  const withManual = scope.toggleSelectAllUnpublished(new Set([201]), target)
  eq(withManual.has(201), true, '锁2 手点的已发布勾选不被全选覆盖')
  // 再点一次 = 反选,只摘候选集,手点的仍在
  const off = scope.toggleSelectAllUnpublished(withManual, target)
  eq([...off].sort((a, b) => a - b), [201], '锁2 反选只摘候选集,手点的保留')
}

// ────────────────────────────────────────────────────── 锁 3 · T1 按钮文案
// 🔴 口径已订正(2026-07-30 复审):按钮主数字答的是「**点了会勾几篇**」,
//    **不再**与分组标签比 —— 强求相等就是让按钮说谎,正是本单要消灭的病。
{
  const r = scope.partitionArticles({ articles: ARTICLES, cartArticleIds: [501], stats: STATS })
  const target = scope.selectAllUnpublishedTarget(r)
  ok(target.label.includes('未分发'), '锁3 按钮文案含「未分发」字样')
  const n = /\((\d+)/.exec(target.label)
  ok(n, '锁3 按钮文案带数字')
  const picked = scope.toggleSelectAllUnpublished(new Set(), target)
  // n 可能为 null（文案里根本没数字）—— 这里**不能**直接 n[1]:
  // 抛异常会让后面几条锁整块不跑，在变异 runner 眼里就成了"判别力不成立"，
  // 把「文案没数字」这个真信号盖成一个假问题。
  eq(n ? Number(n[1]) : null, picked.size, '锁3 按钮主数字 == 点下去真正被勾中的篇数')
}

// ── 锁 9 · T1 三个数各自正确(新增 · 复审 §3.2 第 3 件)
// 🔴 这条锁的价值在于**分别核三个数**,而不是核"它们相等"。
//    写成两两比对就退回旧错误了 —— 期望值是三个写死的常量,彼此无关。
{
  // 未分发 5 篇,其中 2 篇未过审 → 按钮 3 · 实际勾 3 · 分组标签 5
  const FIVE = [
    A(701), A(702), A(703),
    A(704, { publication_eligible: false }),
    A(705, { publication_eligible: false }),
  ]
  const st = new Map(FIVE.map(a => [a.id, { status: 'none' }]))
  const r = scope.partitionArticles({ articles: FIVE, stats: st })
  const target = scope.selectAllUnpublishedTarget(r)
  const picked = scope.toggleSelectAllUnpublished(new Set(), target)

  eq(target.selectableCount, 3, '锁9 按钮数字(点了会勾几篇) = 3')
  eq(picked.size, 3, '锁9 实际勾选 = 3')
  eq(r.unpublished.length, 5, '锁9 分组标签(这组有几篇) = 5')
  eq([...picked].sort((a, b) => a - b), [701, 702, 703], '锁9 勾中的正是那 3 篇已过审的')
  eq(target.label, '全选未分发 (3/5)', '锁9 文案把两个数都摆出来,用户看得出另 2 篇是未过审')
  // 反向:全部过审时不啰嗦,只显一个数;此时"点了会勾几篇"仍然独立成立
  const allOk = scope.partitionArticles({ articles: [A(801), A(802)], stats: new Map([[801, { status: 'none' }], [802, { status: 'none' }]]) })
  const t2 = scope.selectAllUnpublishedTarget(allOk)
  eq(t2.label, '全选未分发 (2)', '锁9 反向:无未过审时只显一个数')
  eq(t2.selectableCount, 2, '锁9 反向:selectableCount 仍独立正确')
  eq(t2.groupCount, 2, '锁9 反向:groupCount 仍独立正确')
}

// ──────────────────────────────────────────── 锁 4 · T2 空组的计数照样可见
//
// [WO-PUBCENTER-LAYOUT 2026-08-04] 承载体换了,锁的东西没换。
// 原来 T2 的保证是"分组标题永远渲染(带 0)",承载体是 ArticleGroupSection。
// 布局重构后四个状态改成顶部分段筛选器,ArticleGroupSection 已无调用方并被删除 ——
// 锁必须跟着搬到**现在真的在跑的那条路径**上,否则就是在给死代码上锁(零调用零风险,
// 全绿也证明不了线上那条链没坏)。
// 分段控件把 T2 的保证加强了:四个计数常驻同一行、同时可见,不用展开也不用滚动。
{
  const OPTIONS = [
    { key: 'unpublished', label: '未分发', count: 46 },
    { key: 'inProgress', label: '发布中', count: 0 },
    { key: 'published', label: '已分发', count: 13 },
    { key: 'rejected', label: '已拒稿', count: 0 },
  ]
  const m = html(React.createElement(filterMod.ArticleStatusFilter, {
    options: OPTIONS, active: 'unpublished', onChange: () => {},
  }))
  for (const o of OPTIONS) {
    ok(m.includes(`data-status-filter="${o.key}"`), `锁4 ${o.label}:计数按钮渲染出来了(含空组)`)
    ok(m.includes(`data-status-filter-count="${o.count}"`), `锁4 ${o.label}:计数 ${o.count} 可见`)
  }
  // 反向:空组的 0 不许被吞。这正是 2026-07-30 T2 修的那个病
  //(空即不渲染 → 老板看到"历史记录不见了")。
  ok((m.match(/data-status-filter-count="0"/g) || []).length === 2,
    '锁4 反向:两个空组的 0 都在,没有被 length>0 之类的守卫吞掉')
  // 反向:未选中的状态照样渲染(筛选器不是"只显示选中项")
  ok(m.includes('data-status-filter-active="false"'),
    '锁4 反向:未选中的状态仍然渲染,四个数是同时可见而不是切一个显一个')
  ok((m.match(/data-status-filter-active="true"/g) || []).length === 1,
    '锁4 反向:有且只有一个选中态')
}

// ─────────────────────────────────────────── 锁 5 · T2 隐藏提示(正反向)
{
  // 条件① 购物车
  const cartOnly = scope.partitionArticles({ articles: ARTICLES, cartArticleIds: [501, 201], stats: STATS })
  eq(cartOnly.hidden, { byQuote: 0, byPreselect: 0, byOptimize: 0, byCart: 2, total: 2 }, '锁5① 购物车隐藏数 == 真实条数')
  // 条件② preselected
  const preOnly = scope.partitionArticles({ articles: ARTICLES, preselected: [101, 102], stats: STATS })
  eq(preOnly.hidden, { byQuote: 0, byPreselect: 6, byOptimize: 0, byCart: 0, total: 6 }, '锁5② 预选隐藏数 == 真实条数')
  // 条件③ onlyOptimize
  const optOnly = scope.partitionArticles({ articles: ARTICLES, onlyOptimize: true, stats: STATS })
  eq(optOnly.hidden, { byQuote: 0, byPreselect: 0, byOptimize: 7, byCart: 0, total: 7 }, '锁5③ 只看优化隐藏数 == 真实条数')

  /*
   * 条件④ 报价筛选(#210 新增的那一道)。
   * 🔴 上面三条用的是**整体深等** —— 加一道过滤而不在账里申报,它们会当场红。
   *    这正是想要的行为:`hidden` 是"我的文章去哪了"的唯一答案,
   *    多一道没人记账的过滤,那句话就开始撒谎。
   */
  const Q = ARTICLES.map((a, i) => ({ ...a, quoteId: i < 3 ? 94 : 378 }))
  const qOnly = scope.partitionArticles({ articles: Q, quoteFilter: 94, stats: STATS })
  eq(qOnly.hidden, { byQuote: Q.length - 3, byPreselect: 0, byOptimize: 0, byCart: 0, total: Q.length - 3 },
    '锁5④ 报价筛选隐藏数 == 真实条数(#210)')
  ok(qOnly.allArticles.length === Q.length,
    '锁5④ 反向:allArticles 仍是全集 —— 筛选收窄的是看到的,不是有的')
  /* 🔴 默认必须是「全部」:不传 quoteFilter ⇒ 一篇都不藏。
     真客户那次的病根就是默认只看一份报价。 */
  const qAll = scope.partitionArticles({ articles: Q, stats: STATS })
  ok(qAll.hidden.byQuote === 0 && qAll.filteredArticles.length === Q.length,
    '锁5④ 🔴 不传报价筛选 ⇒ 全部报价都在(默认不许收窄)')

  const m = html(React.createElement(noticeMod.HiddenArticlesNotice, { hidden: cartOnly.hidden }))
  ok(m.includes('data-hidden-total="2"'), '锁5 提示行出现且数字与真实条数一致')
  ok(m.includes('购物车 2 篇'), '锁5 提示行点名是哪一道过滤器')
  // 购物车不可清 → 不给「显示全部」按钮(点了也没用的出口不如不给)
  ok(!m.includes('hidden-articles-show-all'), '锁5 仅购物车隐藏时不给「显示全部」')
  const mPre = html(React.createElement(noticeMod.HiddenArticlesNotice, { hidden: preOnly.hidden }))
  ok(mPre.includes('hidden-articles-show-all'), '锁5 预选可清 → 给「显示全部」')

  // 🔴 反向:三条件全假 → 这一行**不出现**
  const none = scope.partitionArticles({ articles: ARTICLES, stats: STATS })
  eq(none.hidden.total, 0, '锁5 反向:无过滤时隐藏数为 0')
  eq(html(React.createElement(noticeMod.HiddenArticlesNotice, { hidden: none.hidden })), '',
    '锁5 反向:无过滤时提示行不渲染')
}

// ───────────────────── 锁 6 · T3 面板渲染(喂后端真实 payload 形状)
// 载荷逐字对齐 services/strict_media_presubmit.py::_blocked_payload
const REAL_PAYLOAD = {
  detail: {
    eligible: false,
    reason: 'strict_media_presubmit_failed',
    reason_class: 'platform_profile_hard',
    overridable: false,
    article_id: 1052,
    message: '搜狐网、新浪网 属严审媒体，本稿未通过发布前预审，已在扣费前拦下（未产生费用）。',
    repair_hint: '按提示修正后重新下单；这些媒体历史拒稿率高，先修比先发划算。',
    presubmit: {
      version: 'strict-media-presubmit-v1.0',
      checked: 3, blocked: true,
      blocked_media: [{
        media_id: 771, media_name: '搜狐网', family_key: 'sohu', family_label: '搜狐系',
        observed_reject_rate: 0.522,
        hard_failures: [
          { code: 'ad_law_absolute_term_in_title', detail: '最佳', evidence: '广告法第9条绝对化用语' },
          { code: 'strict_media_body_external_link_detected', evidence: '被引语料站外链接出现率 0/1053' },
        ],
        warnings: [],
      }],
      passed_media: [{ media_id: 802, media_name: '凤凰网' }],
    },
    hard_failure_codes: ['ad_law_absolute_term_in_title', 'strict_media_body_external_link_detected'],
    actions: [
      { id: 'ai_fix_this_span', label: 'AI 修复违规表述', type: 'retry' },
      { id: 'view_findings', label: '查看定位', type: 'nav' },
      { id: 'edit_manually', label: '自己手动改', type: 'nav' },
    ],
    rule_version: 'strict-media-presubmit-v1.0',
  },
}
{
  const block = strict.parseStrictPresubmitBlock(REAL_PAYLOAD)
  ok(block, '锁6 真实 payload 能解析出拦截块')
  const m = html(React.createElement(panelMod.StrictPresubmitPanel, { block, canSwitchMedia: true }))
  for (const id of ['ai_fix_this_span', 'view_findings', 'edit_manually']) {
    ok(m.includes(`data-governance-action="${id}"`), `锁6 渲染出后端动作按钮 ${id}`)
  }
  ok(m.includes('data-governance-action="switch_to_non_strict_media"'), '锁6 渲染出「换成非严审媒体」')
  ok(m.includes('标题里有广告法禁止的绝对化用语'), '锁6 失败原因① 是人话不是码')
  ok(m.includes('正文里带了站外链接'), '锁6 失败原因② 是人话不是码')
  ok(!m.includes('ad_law_absolute_term_in_title<'), '锁6 主视线不直接吐机器码')
  ok(m.includes('先修比先发划算'), '锁6 渲染 repair_hint')
  ok(m.includes('未产生任何费用'), '锁6 明示"没扣钱"(拦在扣费之前)')
  ok(m.includes('搜狐网'), '锁6 逐媒体列出被拦的是哪家')
  ok(m.includes('52%'), '锁6 给出该媒体历史拒稿率')
}

// ────────────────────── 锁 7 · T3 反向:缺 actions 不崩 + 永不出现放行出口
const FORBIDDEN = /忽略继续投放|忽略并继续|人工审核放行|强制发布|跳过预审|仍然投放|继续投放/
{
  // ① 老响应 / 其它拦截:没有 actions
  const legacy = { detail: { ...REAL_PAYLOAD.detail, actions: undefined } }
  const block = strict.parseStrictPresubmitBlock(legacy)
  ok(block, '锁7 缺 actions 仍能解析')
  eq(block.actions, [], '锁7 缺 actions 时退化成空动作表')
  let m = ''
  try {
    m = html(React.createElement(panelMod.StrictPresubmitPanel, { block, canSwitchMedia: false }))
  } catch (e) { console.error(`❌ 锁7 缺 actions 时渲染崩了: ${e.message}`); failed++ }
  ok(m.includes('strict-presubmit-panel'), '锁7 缺 actions 时不崩,退化成纯提示')
  ok(!FORBIDDEN.test(m), '锁7 缺 actions 时也不出现放行类按钮')

  // ② 敌意 payload:后端硬塞一个覆盖动作 → 白名单必须把它挡在渲染之外
  const hostile = strict.parseStrictPresubmitBlock({
    detail: {
      ...REAL_PAYLOAD.detail,
      actions: [
        ...REAL_PAYLOAD.detail.actions,
        { id: 'override_and_publish', label: '忽略继续投放', type: 'retry' },
      ],
    },
  })
  const mh = html(React.createElement(panelMod.StrictPresubmitPanel, { block: hostile, canSwitchMedia: true }))
  ok(!mh.includes('data-governance-action="override_and_publish"'), '锁7 白名单挡掉覆盖类动作 id')
  ok(!FORBIDDEN.test(mh), '锁7 任何情况下都不渲染「忽略继续投放」类按钮')
  // 分布断言:同一次渲染里合法动作**在**、覆盖动作**不在**
  ok(mh.includes('data-governance-action="ai_fix_this_span"'), '锁7 合法动作仍在(证明不是整片没渲染)')
}

// ──────────────────────────────────────────────── 锁 8 · T3 换成非严审媒体
{
  const block = strict.parseStrictPresubmitBlock(REAL_PAYLOAD)
  eq(strict.blockedMediaIds(block), [771], '锁8 被拦媒体 id 解析正确')
  const cart = [
    { articleId: 1052, mediaList: [{ id: 771, name: '搜狐网' }, { id: 802, name: '凤凰网' }] },
    { articleId: 1099, mediaList: [{ id: 771, name: '搜狐网' }] }, // 别的文章,不该被动
  ]
  const next = strict.applyStrictMediaSwitch(cart, block.article_id, strict.blockedMediaIds(block))
  eq(next.find(c => c.articleId === 1052).mediaList.map(m => m.id), [802],
    '锁8 被拦媒体从该文章的选择里移除,其余媒体保留')
  eq(next.find(c => c.articleId === 1099).mediaList.map(m => m.id), [771],
    '锁8 反向:同一家媒体在别的文章上不被误伤')
  // 摘光媒体的条目要整条去掉(留空条目提交必报错)
  const emptied = strict.applyStrictMediaSwitch(
    [{ articleId: 1052, mediaList: [{ id: 771 }] }], 1052, [771])
  eq(emptied, [], '锁8 媒体被摘光的条目整条移除')
}


// ─────────────────────────── 锁 10 · fallback 那条路也必须过核实位(R6 补充①)
//
// 🔴 这一条守的是**默认路径**,不是边角:分组优先读 stats,而 stats 未到货时
//    (首屏 / `/article-publish-stats` 请求失败 / 切客户空窗)走的是 id 集合那条
//    fallback。`publishedArticleIds` 来自 `/published-articles` 的 `article_ids`,
//    那个字段里 UNION 了自助发布的**回执**成功行(R2 §③ 保留的防重复发布占位),
//    所以拿它当"已发布"= 未核实的自报直接进已发布桶,而且是**静默**的:
//    stats 一到货又变回来,肉眼几乎抓不到。
{
  const arts = [A(9001), A(9002), A(9003)]
  const r = scope.partitionArticles({
    articles: arts,
    // 占位集(端点 article_ids):三篇都在
    publishedArticleIds: [9001, 9002, 9003],
    // 核实位(端点从 R2 §② 起就返回,前端此前从没接过)
    verifiedPublishedArticleIds: [9002],
    reportedUnverifiedArticleIds: [9001],
  })
  ok(!r.published.some(a => a.id === 9001), '锁10 stats 未到货:未核实的自报不进已发布桶')
  ok(r.reportedUnverified.some(a => a.id === 9001), '锁10 它落在第五桶(不许从所有 tab 消失)')
  ok(!r.unpublished.some(a => a.id === 9001), '锁10 也不许掉进未分发(占位仍然算数)')
  ok(r.published.some(a => a.id === 9002), '锁10 反向:已核实的自报照常算已发布')
  ok(r.published.some(a => a.id === 9003), '锁10 反向:代发订单照常算已发布')

  // 兼容反向对照:不传核实位时行为与旧版逐字一致(既有调用方不许被静默改掉)
  const legacy = scope.partitionArticles({ articles: arts, publishedArticleIds: [9001, 9002, 9003] })
  eq(legacy.published.length, 3, '锁10 不传核实位时维持旧行为')

  // 重投明示弹窗:第三态,不冒充已发布。
  // 🔴 stats **到货**那条路要单独打一遍 —— 第一版只测了 fallback 那条,
  //    于是"把 stats 路的 reported 并回 published"这个变异**存活**了:
  //    同一个命题有两条实现路径,只测一条等于只守了一半。
  const distFromStats = scope.cartArticlesAlreadyDistributed(
    [{ articleId: 9001, articleTitle: '未核实' }, { articleId: 9002, articleTitle: '已核实' }],
    {
      stats: new Map([
        [9001, { status: 'reported_success_unverified' }],
        [9002, { status: 'published' }],
      ]),
    })
  eq(distFromStats.find(d => d.articleId === 9001).state, 'reported_unverified',
    '锁10 重投弹窗(stats 路):未核实的自报是第三态')
  eq(distFromStats.find(d => d.articleId === 9002).state, 'published',
    '锁10 反向(stats 路):已核实的仍是已发布')

  const dist = scope.cartArticlesAlreadyDistributed(
    [{ articleId: 9001, articleTitle: '未核实' }, { articleId: 9002, articleTitle: '已核实' }],
    {
      publishedArticleIds: [9001, 9002],
      verifiedPublishedArticleIds: [9002],
      reportedUnverifiedArticleIds: [9001],
    })
  eq(dist.find(d => d.articleId === 9001).state, 'reported_unverified',
    '锁10 重投弹窗:未核实的自报是第三态')
  eq(dist.find(d => d.articleId === 9002).state, 'published',
    '锁10 反向:已核实的仍是已发布')
}

// ───────────────────────────────────────────── 收尾:0 skipped 必须算出来
const skipped = DECLARED_LOCKS.filter(k => ran.get(k) === 0)
const total = [...ran.values()].reduce((a, b) => a + b, 0)
console.log('\n── 覆盖统计 ──')
console.log(DECLARED_LOCKS.map(k => `${k}:${ran.get(k)}`).join('  '))
console.log(`断言 ${total} 条 / 声明 ${DECLARED_LOCKS.length} 条锁 / passed ${total - failed} / failed ${failed} / skipped ${skipped.length}`)
// 用 ❌ 前缀报,是为了跟变异 runner 的三层分流对上:
// 「退出码非 0 但零条 ❌」被判为假红;整块被跳过是**真信号**,必须落成一条 ❌。
if (skipped.length) console.error(`❌ 以下锁一条断言都没跑(= SKIP,不算 PASS): ${skipped.join(', ')}`)

const bad = failed || skipped.length
console.log(bad ? `
🔴 未通过(failed=${failed} skipped=${skipped.length})` : `
✅ 发布中心作用域 ${DECLARED_LOCKS.length} 条锁全绿 · 0 skipped`)
process.exit(bad ? 1 : 0)
