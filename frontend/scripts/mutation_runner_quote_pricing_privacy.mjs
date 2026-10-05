#!/usr/bin/env node
/**
 * 变异检验 · 报价系数顶栏 + 演示隐私模式(WO_QUOTE_COEFFICIENT_PRIVATE_TOPBAR 2026-08-11)。
 *
 * 工单 §12 逐条落:「以下任一回退必须让测试转红」。
 * 每条变异 = 「有人把这个改回去/改坏」的一种**具体写法**,锁必须当场转红。
 * 存活的先分诊「锁写松了」还是「变异是空操作」,两者修法不同。
 *
 * 跑法:node scripts/mutation_runner_quote_pricing_privacy.mjs
 *
 * 🔴 锁 = 真浏览器 Playwright 套件(只跑 desktop1440 一个 project 控时长;
 *    三视口全量由 `npx playwright test --config=playwright.quote-pricing-privacy.config.ts` 单独跑)。
 *
 * 自坏防线:
 *   · 锚点在源文件里出现次数 ≠ 1 → ANCHOR_BAD 并整体失败(抓不到 = 变异没落盘);
 *   · 写回用原始 Buffer 逐字还原,不经任何行尾转换(CRLF 翻转会让后续锚点全失配);
 *   · 先跑一次基线,不绿就 abort(基线红时变异结果无意义)。
 */
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { spawnSync } from 'node:child_process'
import { landPoisons, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs'

const root = process.cwd()
const CONFIG = 'playwright.quote-pricing-privacy.config.ts'
const BAR = 'src/components/pricing/QuotePricingControlBar.tsx'
const EDITOR = 'src/components/pricing/QuoteCoefficientEditor.tsx'
const PRIVACY = 'src/components/pricing/pricingPrivacy.tsx'
const RATIONALE = 'src/pages/Quote/utils/priceRationale.ts'
const WHY = 'src/pages/Quote/components/WhyThisPrice.tsx'
const FLOW = 'src/pages/Quote/OnlineQuoteFlow.tsx'

/** [编号, 条目, [[文件, 锚点, 替换], ...]] · 一条变异可跨文件(隐私态是跨组件的) */
const MUTATIONS = [
  ['M01', '§12 恢复 localStorage=true 自动公开', [
    [PRIVACY, `  const [revealed, setRevealedState] = useState(false);`,
     `  const [revealed, setRevealedState] = useState<boolean>(() => { try { return localStorage.getItem('omnirank_pricing_reveal') === 'true' } catch { return false } });`],
    [PRIVACY, `  const toggleRevealed = useCallback(() => setRevealedState(v => !v), []);`,
     `  const toggleRevealed = useCallback(() => setRevealedState(v => { const n = !v; try { localStorage.setItem('omnirank_pricing_reveal', n ? 'true' : 'false') } catch { /* ignore */ } return n }), []);`],
  ]],

  ['M02', '§12 只遮数值、保留敏感标签', [
    [BAR, `              <span className="font-mono text-sm tracking-widest text-muted-foreground">{PRICING_MASK}</span>`,
     `              <span className="font-mono text-sm tracking-widest text-muted-foreground">默认报价系数 •• · 毛利 •• · 单篇内容成本 ••</span>`],
  ]],

  ['M03', '§12 用 CSS 模糊代替删除敏感 DOM', [
    [BAR, `        <div className="border-t border-border px-3 py-3" data-testid="quote-pricing-panel">`,
     `        <div className={\`border-t border-border px-3 py-3 \${revealed ? '' : 'blur-sm select-none'}\`} data-testid="quote-pricing-panel">`],
    [BAR, `          {!revealed ? (`, `          {false ? (`],
  ]],

  ['M04', '§12 保留旧 Step4 系数卡', [
    [FLOW, `      {/* 客户追加词提示 */}`,
     `      {canRecalculateQuote && <Card data-testid="quote-coefficient-card"><CardHeader><CardTitle>本次报价系数</CardTitle></CardHeader><CardContent><span>售价系数（1.0–5.0）</span><Input aria-label="本次报价系数" /></CardContent></Card>}\n      {/* 客户追加词提示 */}`],
  ]],

  ['M05', '§12 切换报价后复用旧 quote_id(不回隐藏 + 不换 key)', [
    [PRIVACY, `  if (seenQuoteId !== quoteId) {`, `  if (false && seenQuoteId !== quoteId) {`],
    [BAR, `              key={context.quoteId ?? 'none'}`, `              key="quote-coefficient-editor"`],
  ]],

  ['M06', '§12 删除 adjustment reason', [
    [EDITOR, `        reason: coefficientReason.trim(),\n`, ``],
  ]],

  ['M07', '§12 删除 expected_snapshot_hash', [
    [EDITOR, `        expected_snapshot_hash: coefficientPreview.snapshot_hash,\n`, ``],
  ]],

  ['M08', '§12 默认系数误作用到当前报价(默认标签改成编辑当前报价)', [
    [BAR, `          ) : activeTab === 'default' ? (`, `          ) : activeTab === 'default' && false ? (`],
  ]],

  ['M09', '§12 本次系数误作用到其他报价', [
    [EDITOR, 'api.post(`/api/quotes/${quoteId}/coefficient`, {', 'api.post(`/api/quotes/${quoteId + 1}/coefficient`, {'],
  ]],

  /* ── 返修 R1/R2 专项:把 Review 指出的两个 P1 原样改回去,锁必须转红 ── */
  ['M10', 'R1 恢复「编辑器卸载即无条件解除发送锁」(POST 在途也解锁)', [
    [EDITOR, `  useEffect(() => () => { onBusyChange?.('preview', false); }, [onBusyChange]);`,
     `  useEffect(() => () => { onBusyChange?.('preview', false); onBusyChange?.('save', false); }, [onBusyChange]);`],
  ]],

  ['M11', 'R5 把「基础成本 = 建议篇数 × 单篇成本」公式塞回展开区', [
    [RATIONALE, `      body: '内容成本按当前媒体 / 写作行情估算,最终报价还会结合这个词的商业价值。',`,
     `      body: '内容成本按当前媒体 / 写作行情估算 · 基础成本 = 建议篇数 × 单篇成本。',`],
  ]],

  ['M12', 'R5 把「你账号的报价系数」塞回展开区', [
    [RATIONALE, `      title: '成本构成',`, `      title: '成本构成(按你账号的报价系数换算)',`],
  ]],

  /* ── R3/R4 专项:老板 2026-08-11 两条口头改动被改回去,锁必须转红 ── */
  ['M13', 'R3 报价栏挪回 OnlineQuoteFlow 滚动区内(往下翻就看不见了)', [
    [FLOW, `    {topBarSlot ? createPortal(pricingControlBar, topBarSlot) : pricingControlBar}\n`, ``],
    [FLOW, `            {isMobile && (\n              <MobileSessionSwitcher`,
     `            {pricingControlBar}\n            {isMobile && (\n              <MobileSessionSwitcher`],
  ]],

  ['M14', 'R4 又留回「内部定价说明:********」占位行(此地无银三百两)', [
    [RATIONALE, `      title: '成本构成',\n      body: '内容成本按当前媒体 / 写作行情估算,最终报价还会结合这个词的商业价值。',`,
     `      title: '内部定价说明',\n      body: '********',`],
  ]],

  ['M15', 'R4/R5 把删掉的「报价系数」整行放回来', [
    [RATIONALE, `      body: '内容成本按当前媒体 / 写作行情估算,最终报价还会结合这个词的商业价值。',\n    },\n  ]`,
     `      body: '内容成本按当前媒体 / 写作行情估算,最终报价还会结合这个词的商业价值。',\n    },\n    {\n      kind: 'markup' as const,\n      title: '报价系数',\n      body: '给客户的售价已按你账号的报价系数自动算好。',\n    },\n  ]`],
  ]],
]

function runLock() {
  const r = spawnSync(
    process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['playwright', 'test', `--config=${CONFIG}`, '--project=desktop1440'],
    { cwd: root, encoding: 'utf8', shell: process.platform === 'win32' },
  )
  return r.status === 0 ? 'SURVIVED' : 'KILLED'
}

console.log('── 基线自检:未变异时锁必须全绿 ──')
if (runLock() !== 'SURVIVED') {
  console.error('[ERR ] 基线就不是绿的 → 变异结果无意义,先修基线')
  process.exit(2)
}
console.log('   OK 基线全绿\n')

let killed = 0
let bad = 0
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(path.join(root, MUTATIONS[0][2][0][0]), console.log);

for (const [code, note, edits] of MUTATIONS) {
  // 一条变异可能跨多个文件 —— 先全部读进来(Buffer,逐字还原用),再整体应用
  const originals = new Map()
  for (const [rel] of edits) {
    if (!originals.has(rel)) originals.set(rel, fs.readFileSync(path.join(root, rel)))
  }
  const mutated = new Map()
  for (const [rel] of edits) {
    if (!mutated.has(rel)) mutated.set(rel, originals.get(rel).toString('utf8'))
  }

  let anchorBad = false
  for (const [rel, anchor, replacement] of edits) {
    const text = mutated.get(rel)
    const hits = text.split(anchor).length - 1
    if (hits !== 1) {
      console.log(`${code}  [BAD ] ANCHOR_BAD  ${note}(${rel} 锚点命中 ${hits} 次,应为 1)`)
      anchorBad = true
      break
    }
    mutated.set(rel, text.replace(anchor, replacement))
  }
  if (anchorBad) { bad++; continue }

  let verdict
  try {
    const landing = landPoisons([...mutated].map(([rel, text]) => [path.join(root, rel), text]))
    if (!landing.ok) {
      console.log(`${code}  [BAD ] ${NOT_LANDED_SYNTAX} · ${landing.bad.join(', ')}`)
      bad++
      continue
    }
    verdict = runLock()
  } finally {
    // 逐字还原,不经行尾转换
    for (const [rel, buf] of originals) fs.writeFileSync(path.join(root, rel), buf)
  }
  if (verdict === 'KILLED') killed++
  console.log(`${code}  [${verdict === 'KILLED' ? 'KILL' : 'LIVE'}] ${verdict.padEnd(9)} ${note}`)
}

const total = MUTATIONS.length
console.log(`\n变异 ${total} 条 · KILLED ${killed} · ANCHOR_BAD ${bad} · 其余 ${total - killed - bad}`)
if (killed !== total) {
  console.error('🔴 有变异没被杀死 —— 先分诊「锁写松了」还是「变异是空操作」')
  process.exit(1)
}
console.log('✅ 全部 KILLED')
