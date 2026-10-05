/**
 * verify-soft-findings-presentation.mjs — 误报治理工单 2026-07-30 · 锁 7(+T2 前端侧)
 *
 * 仓库没有前端单测框架(无 vitest/jest),verify-*.mjs 是既有的前端断言载体。
 *
 * 🔴 诚实说明能力边界:这是**结构断言**,不是渲染测试。它证明的是
 *    "JSX 里那颗按钮受哪个条件管辖 / 那个分支里有没有 <button>",
 *    不是"浏览器里真的没渲染出来"。真渲染只能靠上线后人工点一遍。
 *    (本仓踩过「量 DOM 属性≠量渲染」,所以这里不吹成行为锁。)
 *
 * 🔴 两条防假阳性的做法(本仓踩过「整文件串扫、不分注释与 JSX」导致长期红):
 *    ① 先剥注释(`{/* *\/}`、`/* *\/`、行尾 `//`),注释里写了什么都不算命中;
 *    ② 按**组件**大括号配对切块,soft 面板与 hard 面板分开判 ——
 *       整文件串扫会把两个面板混为一谈。
 *
 * 锁住:
 *   ① soft 面板(ArticleEvidenceAdvisory)里「一键修复本类」按钮**允许存在**,
 *      但必须配"不影响发布"明示,且按钮附近不得出现待办式字样。
 *      🔴 2026-08-01 Owner 拍板改的:原规则是"按钮必须被 isHardCard 管辖"(即不许出现)。
 *      改的是**手段**不是**用意** —— 用意始终是「别让用户以为不修完就不能发」。
 *      召回旧方案:revert 那一个 commit 即可。
 *   ② hard 面板(ArticleLegalFindings)的修复出口**仍在**(auto-repair-btn +
 *      span-ai-repair-btn)—— 防"砍 soft 时把 hard 一起砍了"
 *   ③ 修复失败三态齐全,且 needs_human 分支里**没有任何按钮**(不给重试)
 *   ④ soft 面板标题不再平铺"N 类共 M 条",改为"质量参考(不影响发布)"
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const FILE = path.resolve(process.cwd(), 'src/pages/Writing/WritingHall.tsx')
const failures = []
const fail = (m) => failures.push(m)

if (!fs.existsSync(FILE)) {
  console.error(`❌ 缺文件: ${FILE}`)
  process.exit(1)
}
const raw = fs.readFileSync(FILE, 'utf8')

/** 剥注释:块注释(含 JSX 的 {/* *\/})与行尾 //(避开 http:// 这类) */
function stripComments(src) {
  return src
    .replace(/\{\s*\/\*[\s\S]*?\*\/\s*\}/g, ' ')
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1')
}

/** 按大括号配对取出 `function NAME(...) {...}` 的函数体 */
function functionBody(src, name) {
  const head = src.indexOf(`function ${name}(`)
  if (head < 0) return null
  const open = src.indexOf('{', src.indexOf(')', head))
  if (open < 0) return null
  let depth = 0
  for (let i = open; i < src.length; i += 1) {
    if (src[i] === '{') depth += 1
    else if (src[i] === '}') {
      depth -= 1
      if (depth === 0) return src.slice(open, i + 1)
    }
  }
  return null
}

const clean = stripComments(raw)
const soft = functionBody(clean, 'ArticleEvidenceAdvisory')
const hard = functionBody(clean, 'ArticleLegalFindings')

if (!soft) fail('取不到 ArticleEvidenceAdvisory 函数体(组件被改名/删除?)')
if (!hard) fail('取不到 ArticleLegalFindings 函数体(组件被改名/删除?)')

// ① soft 面板的整类批量修复入口 —— 允许存在,但必须配"不影响发布"的明示
//
// 🔴 规则变更(2026-08-01 · Owner 拍板 · Deploy-CTO 执行):
//   原规则 = 按钮必须被 isHardCard(card) 管辖(即 soft 面板【不许】出现整类批量修复入口)。
//   新规则 = 按钮【允许】出现在 soft 面板,因为该修复免费且对用户有用;
//            但必须同时挂"不影响发布"的明示,且不得出现待办式字样。
//
//   为什么这不是"为了让门变绿而改判据":
//     原规则保护的**真实用意**是「别让用户以为不修完就不能发」——
//     那个用意由"不影响发布"文案直接承担,比"藏起按钮"更正面。
//     所以本条不是删除,是把同一个用意换成更准的断言(下面仍然会 fail)。
//
//   🔁 想召回旧方案:`git revert <本 commit>` 即可,前端代码那侧另见 P2-D。
if (soft) {
  const btn = soft.indexOf('data-testid="repair-class-btn"')
  if (btn >= 0) {
    // 有按钮 → 必须有"不影响发布"明示(标题或说明,二者其一即可,但至少一处)
    if (!/不影响发布/.test(soft)) {
      fail('① soft 面板有「一键修复本类」按钮,却没有任何"不影响发布"明示 '
        + '—— 会读成"不修完不让发"(2026-08-01 新规:按钮可留,明示不可少)')
    }
    // 有按钮 → 附近不得出现待办式字样(压迫感的真来源)
    const around = soft.slice(Math.max(0, btn - 600), Math.min(soft.length, btn + 600))
    const nag = around.match(/待办|待处理|待确认|需处理|必须修复/)
    if (nag) {
      fail(`① soft 面板批量修复按钮附近出现待办式字样「${nag[0]}」 `
        + '—— soft 不阻断发布,文案不得暗示必做')
    }
  }
}

// ② hard 面板的修复出口仍在(防止砍 soft 时误伤 hard)
if (hard) {
  for (const marker of ['data-testid="auto-repair-btn"', 'data-testid="span-ai-repair-btn"']) {
    if (!hard.includes(marker)) {
      fail(`② hard 面板缺少 ${marker} —— 法律硬门的 AI 修复出口不许被顺手砍掉`)
    }
  }
}

// ③ 三态齐全 + needs_human 不给重试
if (soft) {
  for (const tid of ['span-repair-needs-human', 'span-repair-unavailable', 'span-repair-failure']) {
    if (!soft.includes(`data-testid="${tid}"`)) {
      fail(`③ 修复出口三态缺 ${tid} —— 三种性质不同的结果不许再合并成一个红色"失败"`)
    }
  }
  const m = soft.match(/failure\.outcome === 'needs_human'[\s\S]*?\n\s*\)\}/)
  if (!m) {
    fail("③ 找不到 needs_human 分支")
  } else if (/<button|<Button/.test(m[0])) {
    fail('③ needs_human 分支里出现了按钮 —— 这一处 AI 已声明"不编造就改不好",'
      + '给重试等于诱导用户点两次把免费额度烧光,然后彻底没路')
  }
}

// ④ 标题不再平铺条数
if (soft) {
  if (!soft.includes('质量参考(不影响发布)')) {
    fail('④ soft 面板标题未改为「质量参考(不影响发布)」')
  }
  if (/质量记录\(仅供参考 · \{visibleCards\.length\} 类共 \{totalCount\} 条/.test(soft)) {
    fail('④ soft 面板标题仍在平铺"N 类共 M 条"(压迫感来源)')
  }
}

if (failures.length > 0) {
  console.error('❌ soft findings 呈现门禁未通过:')
  for (const f of failures) console.error('  · ' + f)
  process.exit(1)
}
console.log('✅ soft findings 呈现门禁通过(结构断言 · 4 组)')
