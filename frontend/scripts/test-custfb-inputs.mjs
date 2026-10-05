/**
 * test-custfb-inputs.mjs — 客户反馈 ①②⑤ 的前端纯逻辑锁。
 *
 * 仓库没有前端单测框架(无 vitest/jest),沿用 test-publish-center-scope.mjs 的做法:
 * esbuild 现场把 TS 打成 JS 再 import,**真跑一遍行为**,不做源码字符串断言。
 *
 *   ① filterPublishProjects        品牌选择器搜索过滤
 *   ② mediaNeedsRegionRemark       地区备注媒体信号(夹具用生产真实两行)
 *   ② cartMediaNeedsRegionRemark   旧购物车(无字段)按名称兜底
 *   ② normalizeRegionRemark        提交前收敛
 *   ⑤ identityItemsEqual           轮询浅比较(机制 A 的判别力所在)
 *
 * 每条正向断言都配一条反向对照 —— 单向断言证明不了判别力。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { pathToFileURL } from 'node:url'
import esbuild from 'esbuild'

const root = process.cwd()
const srcDir = path.join(root, 'src')
const outDir = path.join(root, 'node_modules/.cache/custfb-inputs')
fs.mkdirSync(outDir, { recursive: true })

/**
 * IdentityReviewPanel 会把 api / lazyToast / SafeMarkdown 整条依赖链拖进来
 * (axios / sonner / react-markdown / demoReport.md?raw)。本文件只测它导出的
 * 一个**纯函数**,永远走不到那些分支 —— 换成空壳,被测函数一字不差。
 */
const stubHeavyDeps = {
  name: 'stub-heavy-deps',
  setup(build) {
    const STUBBED = /^@\/(lib\/api|lib\/lazyToast|components\/SafeMarkdown|components\/ui\/button|components\/ui\/input)$/
    build.onResolve({ filter: STUBBED }, args => ({ path: args.path, namespace: 'stub' }))
    build.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
      contents: 'export const authFetch = () => {}; export const lazyToast = {};'
              + ' export const Button = () => null; export const Input = () => null;'
              + ' export default () => null;',
      loader: 'js',
    }))
  },
}

async function load(rel, name) {
  const outfile = path.join(outDir, `${name}.mjs`)
  await esbuild.build({
    plugins: [stubHeavyDeps],
    entryPoints: [path.join(srcDir, rel)],
    outfile,
    bundle: true,
    format: 'esm',
    platform: 'neutral',
    external: ['react', 'react-dom', 'react-dom/server', 'react/jsx-runtime',
               'sonner', 'axios', 'lucide-react'],
    alias: { '@': srcDir },
    logLevel: 'silent',
  })
  return import(pathToFileURL(outfile).href)
}

let passed = 0
const failures = []
function check(name, fn) {
  try {
    fn()
    passed += 1
    console.log(`  ✓ ${name}`)
  } catch (err) {
    failures.push(`${name}: ${err.message}`)
    console.log(`  ✗ ${name} — ${err.message}`)
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg || 'assertion failed')
}

const inputs = await load('pages/Publishing/publishCenterInputs.ts', 'inputs')
const identity = await load('pages/Monitoring/components/IdentityReviewPanel.tsx', 'identity')

const {
  filterPublishProjects, mediaNeedsRegionRemark, mediaNeedingRegionRemark,
  cartMediaNeedsRegionRemark, normalizeRegionRemark, REGION_REMARK_MAX_LEN,
} = inputs
const { identityItemsEqual } = identity

// ── ① 品牌选择器搜索 ────────────────────────────────────────────────────
const PROJECTS = [
  { id: 1, brand_name: '揭阳滨江南路雅栖酒店', industry: '酒店', keyword_count: 3 },
  { id: 2, brand_name: 'QZQZ 美学定制', industry: '美业', keyword_count: 5 },
  { id: 3, brand_name: '浙江岱林生物技术股份有限公司', industry: '生物医药', keyword_count: 8 },
]

console.log('① 品牌选择器搜索')
check('空查询原样返回(默认行为不变)', () => {
  assert(filterPublishProjects(PROJECTS, '').length === 3)
  assert(filterPublishProjects(PROJECTS, '   ').length === 3)
})
check('打三个字能定位(按名称)', () => {
  const got = filterPublishProjects(PROJECTS, '雅栖酒')
  assert(got.length === 1 && got[0].id === 1, JSON.stringify(got))
})
check('按行业也能搜', () => {
  const got = filterPublishProjects(PROJECTS, '生物医药')
  assert(got.length === 1 && got[0].id === 3)
})
check('大小写不敏感', () => {
  assert(filterPublishProjects(PROJECTS, 'qzqz').length === 1)
})
check('反向对照:搜不存在的词返回空(不是恒返回全部)', () => {
  assert(filterPublishProjects(PROJECTS, '不存在的品牌xyz').length === 0)
})

// ── ② 地区备注信号(夹具 = 生产真实两行) ────────────────────────────────
console.log('② 地区备注媒体信号')
const LIEJU = { media_name: '列举网(可指定地区)', remark: '图片不包敏感词修改不通知，geo可发 可指定地区下单备注' }
const CHEZHU = { media_name: '车主之家随机(可指定地区)', remark: '所有的地方站都可以指定发' }
const GENERIC = { media_name: '半岛网财经', remark: '好出，审核不严，出稿稳，有要求的提前备注清楚，联系方式不带' }

check('命中:列举网(名称+备注都带)', () => assert(mediaNeedsRegionRemark(LIEJU) === true))
check('命中:车主之家(只有名称带 —— 工单原口径漏的正是这一行)', () => {
  assert(mediaNeedsRegionRemark(CHEZHU) === true)
})
check('反向对照:通用"提前备注清楚"不命中(748 行同类,收进来就是噪音)', () => {
  assert(mediaNeedsRegionRemark(GENERIC) === false)
})
check('反向对照:空/缺字段不命中', () => {
  assert(mediaNeedsRegionRemark(null) === false)
  assert(mediaNeedsRegionRemark({}) === false)
})
check('批量筛选只挑出命中的那些', () => {
  const got = mediaNeedingRegionRemark([LIEJU, GENERIC, CHEZHU])
  assert(got.length === 2, `期望 2 行,实际 ${got.length}`)
})

console.log('② 购物车条目(含旧车兜底)')
check('新车:字段为 true 直接命中', () => {
  assert(cartMediaNeedsRegionRemark({ name: '随便什么', needsRegionRemark: true }) === true)
})
check('旧车(无字段):按名称兜底仍命中', () => {
  assert(cartMediaNeedsRegionRemark({ name: '列举网(可指定地区)' }) === true)
})
check('反向对照:旧车里的普通媒体不命中', () => {
  assert(cartMediaNeedsRegionRemark({ name: '半岛网财经' }) === false)
})

console.log('② 备注收敛')
check('去首尾空白', () => assert(normalizeRegionRemark('  发广东省 ') === '发广东省'))
check('空串语义(不上送)', () => {
  assert(normalizeRegionRemark('   ') === '')
  assert(normalizeRegionRemark(null) === '')
})
check('截断到上限', () => {
  assert(normalizeRegionRemark('地'.repeat(REGION_REMARK_MAX_LEN + 50)).length === REGION_REMARK_MAX_LEN)
})

// ── ⑤ 机制 A:轮询浅比较 ────────────────────────────────────────────────
console.log('⑤ 监测中心轮询浅比较')
const A = [
  { id: 1, identity_evidence_hash: 'h1', identity_decision_version: 1 },
  { id: 2, identity_evidence_hash: 'h2', identity_decision_version: 1 },
]
check('内容未变 → 判等(不再换引用 = 不再每 30 秒闪)', () => {
  const B = A.map(x => ({ ...x }))   // 不同引用、同内容
  assert(identityItemsEqual(A, B) === true)
})
check('反向对照:证据 hash 变了 → 判不等', () => {
  const B = A.map(x => ({ ...x }))
  B[1].identity_evidence_hash = 'h2-new'
  assert(identityItemsEqual(A, B) === false)
})
check('反向对照:决策版本变了 → 判不等', () => {
  const B = A.map(x => ({ ...x }))
  B[0].identity_decision_version = 2
  assert(identityItemsEqual(A, B) === false)
})
check('反向对照:少一条 / 顺序变了 → 判不等', () => {
  assert(identityItemsEqual(A, [A[0]]) === false)
  assert(identityItemsEqual(A, [A[1], A[0]]) === false)
})
check('空对空判等(首次加载不制造多余渲染)', () => {
  assert(identityItemsEqual([], []) === true)
})

/* ══════════════════════════════════════════════════════════════════
 * [下单备注 P0 · 2026-08-10] 地区备注停用闸 —— 变异 M3 的覆盖物
 *
 * 变异 runner(python 侧)把 REGION_REMARK_ENABLED 翻成 true 时,后端锁够不到,
 * 于是那条变异会"存活"。存活不许当免检 —— 这几条把前端那一半钉上。
 *
 * 停用理由(生产真单 A/B):订单 479/480 填备注 → failed;481 空备注 → submitted。
 * 发布通道回包逐字相同:「备注字段内容不合法」。
 * ══════════════════════════════════════════════════════════════════ */
check('地区备注闸出厂必须是关的', () => {
  assert(inputs.REGION_REMARK_ENABLED === false)
})
check('反向对照:该常量确实被导出(不是 undefined 恒等于 false)', () => {
  assert('REGION_REMARK_ENABLED' in inputs)
  assert(typeof inputs.REGION_REMARK_ENABLED === 'boolean')
})
check('闸关时提交侧不许上送备注(PublishCenter 用 REGION_REMARK_ENABLED && … 收口)', () => {
  const src = fs.readFileSync(path.join(srcDir, 'pages/Publishing/PublishCenter.tsx'), 'utf8')
  const line = src.split('\n').find(l => l.includes('order_remark:'))
  assert(line && line.includes('REGION_REMARK_ENABLED'))
})
check('闸关时输入框不渲染 / 有替代提示(两个 testid 各自被闸门控)', () => {
  const src = fs.readFileSync(path.join(srcDir, 'pages/Publishing/PublishCenter.tsx'), 'utf8')
  assert(src.includes('REGION_REMARK_ENABLED && (') || src.includes('&& REGION_REMARK_ENABLED'))
  assert(src.includes('region-remark-disabled-notice'))
})

console.log('')
if (failures.length) {
  console.error(`✗ ${failures.length} 条锁未通过:`)
  failures.forEach(f => console.error(`   - ${f}`))
  process.exit(1)
}
console.log(`✓ 全部 ${passed} 条锁通过(含下单备注 P0 四条)`)
