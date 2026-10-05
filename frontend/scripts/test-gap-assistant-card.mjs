#!/usr/bin/env node
/**
 * 小榜运营助手渲染层门禁(P4 · C1 接线补齐 · 2026-08-08)
 *
 * 🔴 这一层的缺失是本包自查抓到的:后端一直在 SSE meta 里下发 gap_assistant,
 *    hook 组装 meta 时只挑三个键把它丢了,前端也没有组件读它。
 *    11 条后端锁全绿 —— 因为它们全打在 build_answer / as_meta 上,没有一条打在浏览器这一端。
 *    所以这份门禁的每一条都刻意打在**接线**上,不打在函数上。
 *
 * 判别力约定:每条"必须命中"配一条"必须不命中"。
 * 变异证明见 scripts/mutation_runner_gap_assistant_card.mjs。
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { createRequire } from 'node:module'

const require_ = createRequire(import.meta.url)

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const read = (p) => readFileSync(resolve(ROOT, p), 'utf8')

const HOOK = 'src/hooks/useXiaobangChat.ts'
const LIST = 'src/components/xiaobang/XiaobangMessageList.tsx'
const CARD = 'src/components/xiaobang/XiaobangGapCard.tsx'

let failed = 0
const check = (name, fn) => {
  try {
    fn()
    console.log(`  ok   ${name}`)
  } catch (e) {
    failed++
    console.error(`  FAIL ${name}\n       ${e.message}`)
  }
}
const assert = (cond, msg) => { if (!cond) throw new Error(msg) }

/** 剥掉块注释与行注释 —— 否则"注释里提到了"会被当成"代码里做了"。
 *  🔴 只剥注释不剥字符串:本仓 2026-08-06 踩过"剥注释≠剥字符串"。 */
function stripComments(src) {
  return src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '')
}

console.log('小榜运营助手渲染层门禁')

// ── 1. hook:meta 组装必须把 gap_assistant 收进来 ───────────────────
check('hook 的 meta 组装带上 gap_assistant', () => {
  const src = stripComments(read(HOOK))
  const block = src.match(/const meta: XiaobangMeta = \{[\s\S]*?\n\s*\}/)
  assert(block, '找不到 meta 组装块 —— 锚点失配,不是通过')
  assert(/gap_assistant\s*:/.test(block[0]),
    'meta 组装块里没有 gap_assistant —— 后端下发的数据会被原地丢掉(这正是本次修的 bug)')
})

check('反向对照:meta 组装块确实是从 SSE data 取值的,不是写死', () => {
  const src = stripComments(read(HOOK))
  const block = src.match(/const meta: XiaobangMeta = \{[\s\S]*?\n\s*\}/)[0]
  assert(/data\.gap_assistant/.test(block),
    'gap_assistant 没有从 data 取 —— 写死成常量的话这条链等于没接')
})

check('normalizeGapAssistant 存在且被 meta 组装使用', () => {
  const src = stripComments(read(HOOK))
  assert(/export function normalizeGapAssistant\(/.test(src), '规范化函数不存在')
  const block = src.match(/const meta: XiaobangMeta = \{[\s\S]*?\n\s*\}/)[0]
  assert(/normalizeGapAssistant\(/.test(block), '规范化函数定义了但没在 meta 组装里调用')
})

check('headline 为空的载荷会被判成"没有建议"', () => {
  const src = stripComments(read(HOOK))
  const fn = src.match(/export function normalizeGapAssistant\([\s\S]*?\n\}/)[0]
  assert(/if \(!headline\) return undefined/.test(fn),
    '没有 headline 还返回对象 → 会渲染出一张空卡片')
})

// ── 2. XiaobangMessageList:必须真的渲染 ──────────────────────────────────
check('XiaobangMessageList 渲染 XiaobangGapCard', () => {
  const src = stripComments(read(LIST))
  assert(/import \{ XiaobangGapCard \}/.test(src), '没有 import')
  assert(/<XiaobangGapCard/.test(src), '导入了但没渲染 —— 死导入')
})

check('渲染以 meta.gap_assistant 存在为条件', () => {
  const src = stripComments(read(LIST))
  assert(/message\.meta\?\.gap_assistant\s*&&\s*\(\s*<XiaobangGapCard/.test(src.replace(/\s+/g, ' ')),
    '没有以 gap_assistant 存在为渲染条件 —— 无建议时会渲染空卡')
})

check('反向对照:卡片拿到的是 meta 里那一份,不是另造的对象', () => {
  const src = stripComments(read(LIST))
  assert(/assistant=\{message\.meta\.gap_assistant\}/.test(src.replace(/\s+/g, ' ')),
    'assistant prop 不是直接来自 message.meta.gap_assistant')
})

// ── 3. 卡片:路由只认服务端签发的 target_route ──────────────────────
check('跳转只用 action.target_route', () => {
  const src = stripComments(read(CARD))
  /*
   * 🔴 [2026-09-20] 原正则是 `navigate\(action\.target_route as string\)` ——
   *    要求 `as string` 后**紧跟右括号**。产品后来给 navigate 传了第二个参数
   *    (`navigate(action.target_route as string, { … })`)⇒ 这一格红,
   *    而跳转用的**就是** target_route,一点没错。锁钉了调用的形状,不是行为。
   *    ⇒ 只钉「navigate 的第一个实参是 action.target_route」。
   */
  assert(/navigate\(\s*action\.target_route\b/.test(src),
    '跳转没有用 action.target_route')
})

check('🔴 卡片源码里不得出现任何路由字面量(前端不许拼/猜/兜底路径)', () => {
  const src = stripComments(read(CARD))
  const hits = src.match(/['"`]\/(pricing|publish|writing|monitoring|my-clients|quotes?)[^'"`]*['"`]/g)
  assert(!hits, `出现了硬编码路由:${hits && hits.join(', ')}`)
})

check('只渲染 enabled 且带 target_route 的动作(两个条件都要)', () => {
  const src = stripComments(read(CARD))
  const fn = src.match(/export function navigableActions\([\s\S]*?\n\}/)[0]
  assert(/a\.enabled === true/.test(fn), '没有判 enabled')
  /*
   * 🔴 [2026-09-20] 原来钉的是字面量 `!!a.target_route`。产品后来把它换成
   *    `typeof a.target_route === 'string' && a.target_route.startsWith('/')
   *     && !a.target_route.startsWith('//')` —— **严格更强**(还挡了相对路径与
   *    协议相对 URL),而这一格因为钉形状反而红了:**产品变强,锁报警**。
   *    ⇒ 改成钉「这个过滤器确实看了 target_route」,不规定它用哪种写法;
   *      「过滤不是恒真」由紧邻的那条反向对照守着。
   */
  assert(/a\.target_route/.test(fn), '没有判 target_route')
})

check('反向对照:过滤是 filter 不是恒真', () => {
  const src = stripComments(read(CARD))
  const fn = src.match(/export function navigableActions\([\s\S]*?\n\}/)[0]
  assert(/\.filter\(/.test(fn), 'navigableActions 没有真过滤')
  assert(!/return\s+actions\s*$/m.test(fn), '直接把入参原样返回 = 没过滤')
})

check('跳转前先关 drawer(否则抽屉盖住目标页)', () => {
  const src = stripComments(read(CARD))
  const handler = src.match(/onClick=\{\(\) => \{[\s\S]*?\}\}/)[0]
  assert(handler.indexOf('onNavigate?.()') < handler.indexOf('navigate('),
    'onNavigate 必须在 navigate 之前调用')
})

check('降级态有渲染且措辞不像故障', () => {
  const src = stripComments(read(CARD))
  const block = src.match(/assistant\.degraded\s*&&[\s\S]*?<\/p>/)
  assert(block, '降级态没有任何渲染 —— 取不到建议时用户会以为页面坏了')
  assert(/不受影响/.test(block[0]), '降级文案没有说明"页面数据不受影响" → 用户会以为出故障了')
  assert(!/(错误|失败|异常|error)/i.test(block[0]), '降级文案用了报错措辞 —— 它不是故障')
})

// ── 4. 设计系统 ──────────────────────────────────────────────────
check('禁硬编码色值(必须走主题 token)', () => {
  const src = stripComments(read(CARD))
  const hits = src.match(/#[0-9a-fA-F]{3,8}\b|rgba?\(/g)
  assert(!hits, `出现硬编码色值:${hits && hits.join(', ')}`)
})

check('禁 emoji(前端约定)', () => {
  const src = read(CARD)
  const hits = src.match(/\p{Extended_Pictographic}/gu)
  const allowed = new Set(['🔴'])   // 注释里的红标是本仓约定,允许
  const bad = (hits || []).filter((h) => !allowed.has(h))
  assert(bad.length === 0, `出现 emoji:${bad.join(' ')}`)
})

check('反向对照:emoji 判据本身不是恒真', () => {
  const probe = 'const x = "去发布 🚀"'
  assert(/\p{Extended_Pictographic}/u.test(probe), 'emoji 正则失效了,上一条等于没判')
})

/* ── AST 锁:过滤器不只是「在」,它得**决定返回值** ─────────────────── */
/*
 * 🔴 来源(2026-09-20,A 窗重锚 F8 时照出来的):
 *    把 `navigableActions` 改成开头就 `return actions;`,原过滤器**沦为死代码** ——
 *    上面那几条 grep 判据**全绿**,因为那段 filter 还老老实实躺在源码里。
 *    「某串出现过」证明不了它还执行。
 *
 * 🔴 本仓 `an-ast-lock-that-finds-the-call-cannot-see-that-nothing-uses-it`:
 *    找得到调用 ≠ 有人用它的结果。三件**一起**钉才算数:
 *      ① 调用在        —— 函数体里确有 `.filter(...)`;
 *      ② 结果被接住    —— 它不是一句被丢掉的表达式语句;
 *      ③ 结果控制返回值 —— **每一条** return 的表达式都由它派生。
 *    ③ 是关键:少了它,一个提前 return 就能把 ① ② 全部架空,而读数全绿。
 *
 * 用 AST 不用 grep:提前 return、死代码、换行重排,grep 一律看不出来。
 */
check('🔴 navigableActions 的过滤器**决定返回值**(不是躺在源码里的死代码)', () => {
  const ts = require_('typescript')
  const sf = ts.createSourceFile(CARD, read(CARD), ts.ScriptTarget.ES2020, true, ts.ScriptKind.TSX)

  let fn = null
  sf.forEachChild((n) => {
    if (ts.isFunctionDeclaration(n) && n.name && n.name.text === 'navigableActions') fn = n
  })
  assert(fn && fn.body, '找不到 navigableActions 的函数声明 —— 判据够不着,不当绿灯')

  /** 这棵子树里有没有 `.filter(...)` 调用。 */
  const hasFilterCall = (node) => {
    let hit = false
    const walk = (n) => {
      if (hit) return
      if (ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression)
        && n.expression.name.text === 'filter') { hit = true; return }
      n.forEachChild(walk)
    }
    walk(node)
    return hit
  }

  /* ① 调用在 */
  assert(hasFilterCall(fn.body), '函数体里根本没有 .filter(...) 调用')

  /* ③ 每一条 return 都由它派生(② 随之成立:被 return 用掉就不是丢弃的表达式)。
       🔴 数的是**全部** return,不是"有一条是就行" —— 提前 return 正是那个漏洞。 */
  const returns = []
  const collect = (n) => {
    /* 不下钻嵌套函数:内层箭头函数(比如 filter 的谓词)的 return 不算这一层的出口。 */
    if (n !== fn && (ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n)
      || ts.isArrowFunction(n))) return
    if (ts.isReturnStatement(n)) returns.push(n)
    n.forEachChild(collect)
  }
  fn.body.forEachChild(collect)

  assert(returns.length > 0, '函数体里一条 return 都没有 —— 这不是本函数该有的形状')
  const bare = returns.filter((r) => !r.expression || !hasFilterCall(r.expression))
  assert(bare.length === 0,
    `有 ${bare.length} 条 return **绕过了过滤器**(共 ${returns.length} 条)`
    + ' —— 提前 return / 死代码型改动就是这样把过滤静默架空的')
})

check('反向对照:上一格的 AST 读法确实找得到东西(不是恒绿)', () => {
  const ts = require_('typescript')
  /* 🔴 正控:喂一段**已知含 filter 且被 return**的源码,必须判成通过;
        再喂一段**提前 return** 的,必须判成不通过。
        两边都验,否则"没找到绕过"与"我的遍历坏了"读数同形。 */
  const hasFilter = (node) => {
    let hit = false
    const walk = (n) => {
      if (hit) return
      if (ts.isCallExpression(n) && ts.isPropertyAccessExpression(n.expression)
        && n.expression.name.text === 'filter') { hit = true; return }
      n.forEachChild(walk)
    }
    walk(node)
    return hit
  }
  const bareReturns = (code) => {
    const sf = ts.createSourceFile('t.tsx', code, ts.ScriptTarget.ES2020, true, ts.ScriptKind.TSX)
    let fn = null
    sf.forEachChild((n) => { if (ts.isFunctionDeclaration(n) && n.name.text === 'f') fn = n })
    const rs = []
    const collect = (n) => {
      if (n !== fn && (ts.isFunctionDeclaration(n) || ts.isFunctionExpression(n)
        || ts.isArrowFunction(n))) return
      if (ts.isReturnStatement(n)) rs.push(n)
      n.forEachChild(collect)
    }
    fn.body.forEachChild(collect)
    return rs.filter((r) => !r.expression || !hasFilter(r.expression)).length
  }
  assert(bareReturns('function f(a){ return (a||[]).filter((x)=>!!x) }') === 0,
    '正控失败:一段正常的 filter-return 被判成绕过了')
  assert(bareReturns('function f(a){ return a; return (a||[]).filter((x)=>!!x) }') === 1,
    '负控失败:提前 return(原过滤器成死代码)没被判出来 —— 上一格等于没判')
})

console.log(failed === 0 ? '\n全部通过' : `\n${failed} 条失败`)
process.exit(failed === 0 ? 0 : 1)
