/**
 * verify-publication-contract-ui.mjs — §13 合同用户可达面构建门禁
 *
 * 背景:后端两条出口(内容漂移 / 代发卡单)的合同写得很完整,但前端一行都没渲染,
 * 合同只写不读 → 出口在后端存在、在用户那里不存在(违反「严禁死胡同」铁律)。
 * 本门禁锁住修复后**最容易悄悄退化**的几条不变量。任一违规即 exit 1。
 *
 * 仓库没有前端单测框架(无 vitest/jest),verify-*.mjs 是既有的前端断言载体,
 * 本脚本沿用同一形态并挂进 npm run build。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'

const root = path.resolve(process.cwd(), 'src')
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8')
const failures = []
const fail = (msg) => failures.push(msg)

/**
 * 只看**代码**,不看注释与 JSX 文案。
 * 否则"渲染器里不许出现机器码"这条会被文件顶部解释根因的注释误伤 ——
 * 而那段注释恰恰是让后人别再加 reject_code 分支的关键说明,不该为了过门禁删掉。
 */
function stripComments(src) {
  return src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => {
      const t = line.trim()
      return !t.startsWith('//') && !t.startsWith('*')
    })
    .join('\n')
}

const RENDERER = 'components/publishing/PendingUserActions.tsx'
const rendererRaw = read(RENDERER)
const renderer = stripComments(rendererRaw)
const LOGIC = 'components/publishing/pendingUserActionsLogic.ts'
const logicRaw = read(LOGIC)
const logic = stripComments(logicRaw)
const contractTypes = read('contracts/governanceAlert.ts')
const alertRaw = read('components/ui/governance-alert.tsx')
const alert = stripComments(alertRaw)
const app = read('App.tsx')
const mocks = read('sandbox/mockData.ts')

// ① 通用渲染器:组件内不得有任何 reject_code 分支。
//    有一条就意味着下一条出口又得改前端,回到"写了合同没人渲染"的原点。
for (const [name, src] of [[RENDERER, renderer], [LOGIC, logic]]) {
  for (const code of ['PUBLISH_CONTENT_DRIFT', 'PUBLISH_AWAITING_SYNC_UNRESOLVED']) {
    if (src.includes(code)) fail(`${name} 出现了具体机器码 ${code} —— 通用渲染器不得按 reject_code 分派`)
  }
  if (/reject_code\s*===/.test(src)) fail(`${name} 出现了 reject_code === 分支 —— 只能按 action.type 分派`)
}

// ② target 驱动:动作路径必须来自合同,前端不得另写一份 URL 常量。
//    硬编码会在后端换路径时**静默失效** —— 按钮还在、点了 404,比没按钮更糟。
//    判据取"渲染器里不许出现任何 /api/ 字面量" —— 比逐个列端点更严且不会漏新增的。
const apiLiterals = renderer.match(/['"`]\/api\/[^'"`]*/g) || []
if (apiLiterals.length > 0) {
  fail(`${RENDERER} 硬编码了接口路径 ${apiLiterals.join(', ')} —— 必须只用 action.target`)
}
const logicApiLiterals = logic.match(/['"`]\/api\/[^'"`]*/g) || []
if (logicApiLiterals.length > 0) fail(`${LOGIC} 硬编码了接口路径 ${logicApiLiterals.join(', ')}`)
if (!/action\.target/.test(renderer)) fail(`${RENDERER} 没有从 action.target 取路径`)
if (!/action\.method/.test(renderer)) fail(`${RENDERER} 没有尊重 action.method`)

// ③ fields 驱动的表单:提交体的键必须来自 fields[].name,不做映射表。
if (!/action\.fields/.test(renderer)) fail(`${RENDERER} 没有消费 action.fields —— 带表单的动作会提交空 body 被 400 挡回`)
if (!/body\[f\.name\]/.test(logic)) fail(`${LOGIC} 提交体的键不是来自 fields[].name`)
if (!/buildSubmitBody/.test(renderer)) fail(`${RENDERER} 没有走 buildSubmitBody 组装提交体`)

// ④ 空/未知动作仍要有下一步 —— 绝不渲染空白卡片(把死胡同从后端搬到前端)。
if (!/CONTACT_ACTION/.test(logic)) fail(`${LOGIC} 缺少兜底出口(联系客服)`)
if (!/normalizeContract/.test(renderer)) fail(`${RENDERER} 没有走合同规整,合同残缺时会渲染出空白`)

// ⑤ 已表态 → 禁止重复提交。
if (!/user_exit_claim/.test(renderer)) fail(`${RENDERER} 没有处理 user_exit_claim(已表态仍可重复提交)`)
if (!/disabledActionIds/.test(renderer)) fail(`${RENDERER} 没有把已表态的动作置灰`)

// ⑥ 错误码各有收场,不报红崩溃。404 是后端故意的反枚举,不能当 bug 弹。
for (const status of ['409', '404', '400']) {
  if (!logic.includes(status)) fail(`${LOGIC} 没有针对 HTTP ${status} 的处置`)
}
if (!/messageForStatus/.test(renderer)) fail(`${RENDERER} 没有按状态码分流,错误会一律报红`)

// ⑦ 设计系统:禁 inline style / <style> 块。
for (const [name, src] of [[RENDERER, rendererRaw], [LOGIC, logicRaw], ['components/ui/governance-alert.tsx', alertRaw]]) {
  if (/style=\{\{/.test(src)) fail(`${name} 出现 inline style,违反设计系统约束`)
  if (/<style[\s>]/.test(src)) fail(`${name} 出现 <style> 块,违反设计系统约束`)
}

// ⑧ 合同类型必须能表达 api 动作的 target/method/fields,否则渲染器无从类型安全地消费。
for (const key of ['target', 'method', 'fields', "'api'"]) {
  if (!contractTypes.includes(key)) fail(`contracts/governanceAlert.ts 缺少 ${key},api 动作契约不完整`)
}

// ⑨ [崩溃回归] GovernanceAlert 的 useState 之间不得插入 early return。
//    原代码 `if (dismissed) return null` 夹在两个 useState 中间:dismissed 一翻 true
//    这次 render 只跑 1 个 hook → React 抛 "Rendered fewer hooks than expected" 白屏,
//    而触发路径正是本组件的主路径(dismiss / api 成功)。
{
  const firstHook = alert.indexOf('useState(false)')
  const lastHook = alert.indexOf('useState<string | null>(null)')
  if (firstHook < 0 || lastHook < 0) {
    fail('governance-alert.tsx 的 useState 声明形态变了,无法校验 hook 顺序')
  } else {
    const between = alert.slice(Math.min(firstHook, lastHook), Math.max(firstHook, lastHook))
    if (/\breturn\b/.test(between)) {
      fail('governance-alert.tsx 在两个 useState 之间出现 return —— 条件 hook 会导致整棵子树白屏')
    }
  }
}

// ⑩ 可达性:合同里 view_orders 的 target 是 '/publishing',而站内真实路由是 '/publish'。
//    生产已有的合同 JSON 冻在 DB 里,改后端构造函数不回填历史行 → 别名路由不能少。
if (!/path="publishing"/.test(app)) {
  fail("App.tsx 缺少 '/publishing' 别名路由 —— 已冻结的历史合同点「查看发布任务」会落到不存在的路由")
}

// ⑪ mock 必须覆盖两个不同的 reject_code(证明一套渲染器真的通吃)。
const mockCodes = ['PUBLISH_AWAITING_SYNC_UNRESOLVED', 'PUBLISH_CONTENT_DRIFT']
for (const code of mockCodes) {
  if (!mocks.includes(code)) fail(`sandbox/mockData.ts 缺少 ${code} 样本,无法验证"两条出口共用一套渲染"`)
}
if (!/telepathy/.test(mocks)) fail('sandbox/mockData.ts 缺少"认不出的 action type"兜底样本')

if (failures.length > 0) {
  console.error('❌ §13 合同用户可达面门禁未通过:')
  for (const f of failures) console.error('   · ' + f)
  process.exit(1)
}
console.log('✅ §13 合同用户可达面门禁通过')
