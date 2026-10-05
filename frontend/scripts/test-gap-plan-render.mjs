#!/usr/bin/env node
/**
 * 交付计划执行面 · **真渲染**判据(返工 WO_GAPPLAN_REWORK_2026-08-11 · 修 4)
 *
 * 🔴 这个文件存在的唯一理由:上一轮"可选链防白屏"是假修复,而当时**没有一条判据
 *    能把那个场景真跑一遍** —— 全靠正则扫写法,于是"改了写法 + 变异杀得掉"被我
 *    当成了"bug 已修"。裁定原话:那证明的是门禁认得出写法。
 *
 *    修 bug 的 commit 必须带一条**打在 bug 场景本身**的判据:构造触发条件、跑、断言。
 *
 * 做法:esbuild 把纯展示层 `GapPlanExecutionView` 连同它的依赖打成一个 ESM 包
 *      (React 走 external,由本进程提供),再用 `react-dom/server.renderToString`
 *      真渲染三种快照:
 *        A. **不含 execution_gate 键**(旧后端 / 蓝绿回滚窗口)→ 必须不抛,且渲染**执行面**
 *        B. execution_gate.open === false                    → 渲染提示、不渲染任务卡
 *        C. execution_gate.open === true                     → 渲染执行面
 *
 * 🔴 A 必须同时断言"不抛"**和**"渲染的是执行面":
 *    只断言不抛会放过"错退成一句提示"的实现 —— 那是拿功能倒退换不崩,
 *    对回滚窗口里的已付款客户是实打实的降级。
 *
 * 跑法:cd frontend && node scripts/test-gap-plan-render.mjs
 * 退出码:0=全绿 1=有红
 */
import { mkdtempSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);

let failed = 0;
const ok = (m) => console.log(`  ✅ ${m}`);
const bad = (m) => { console.log(`  🔴 ${m}`); failed++; };
const check = (cond, m) => (cond ? ok(m) : bad(m));

// ── 打包 ────────────────────────────────────────────────────────
const esbuild = require_('esbuild');
const React = require_('react');
const { renderToString } = require_('react-dom/server');

// 🔴 产物必须落在**项目内**(node_modules/.cache/):放系统临时目录的话,
//    bundle 里 external 出去的裸 `react` 导入解析不到 —— node 从产物所在目录
//    往上找 node_modules。第一版放 tmpdir,ERR_MODULE_NOT_FOUND。
const cacheRoot = join(ROOT, 'node_modules', '.cache');
mkdirSync(cacheRoot, { recursive: true });
const outDir = mkdtempSync(join(cacheRoot, 'gapplan-render-'));
const entry = join(outDir, 'entry.mjs');
const bundle = join(outDir, 'bundle.mjs');

writeFileSync(entry, `
export { GapPlanExecutionView, resolveExecutionMode }
  from ${JSON.stringify(join(ROOT, 'src/components/gapPlan/GapPlanExecutionView.tsx').replace(/\\/g, '/'))};
`, 'utf8');

try {
  esbuild.buildSync({
    entryPoints: [entry],
    bundle: true,
    outfile: bundle,
    format: 'esm',
    // 🔴 platform 必须是 browser:node 平台下 esbuild 会按 CJS 条件解析,
    //    lucide-react 会解到 dist/cjs 那一份,进 ESM 产物后 `require('react')`
    //    直接抛 "Dynamic require of react is not supported"。
    //    组件本来就是给浏览器写的,按浏览器条件解析才是同构的。
    platform: 'browser',
    define: { 'process.env.NODE_ENV': '"development"' },
    jsx: 'automatic',
    // React 由本进程注入,保证组件与 renderToString 用的是同一份 React
    external: ['react', 'react-dom', 'react/jsx-runtime', 'react/jsx-dev-runtime'],
    alias: { '@': join(ROOT, 'src') },
    loader: {
            '.tsx': 'tsx', '.ts': 'ts',
            // 🔴 Vite 自己懂 `import x from './a.jpg'`，esbuild 不懂 —— 不配 loader 就是
            //    「打包失败 ⇒ 判据不可用」(而不是静默少一张图)。用 dataurl 而不是置空：
            //    样图本身就是 #188 要验的东西(「风格这里必须让人看到成品是什么样」)，
            //    置空会让所有样图臂在真缺陷下也照样绿。
            '.jpg': 'dataurl', '.jpeg': 'dataurl', '.png': 'dataurl',
            '.webp': 'dataurl', '.svg': 'dataurl',
        },
    logLevel: 'silent',
  });
} catch (err) {
  console.log('🔴 打包失败(判据不可用,不当绿灯):\n' + String(err).slice(0, 2000));
  rmSync(outDir, { recursive: true, force: true });
  process.exit(1);
}

const mod = await import(pathToFileURL(bundle).href);
const { GapPlanExecutionView, resolveExecutionMode } = mod;

// ── 夹具 ────────────────────────────────────────────────────────
/**
 * 一份最小但**结构真实**的快照。字段形态照抄 services/gap_operation_plan.present_snapshot
 * 的 agent 分支出参,不是随手编的:少一个字段就可能测的是"渲染提前 return 了"。
 */
const baseSnapshot = () => ({
  audience: 'agent',
  snapshot_id: 'S-TEST',
  snapshot_version: 'gap-plan-v1',
  rule_version: 'gap-plan-rules-v1',
  authority_generation: 1,
  quote_id: 900,
  query_family: { display_query: '深圳哪家装修公司靠谱', observed_at: null },
  summary: {
    headline: '同行已经在被推荐。',
    next_step: '先补齐可信信息。',
    customer_present: false,
    total_candidates: 3,
    capacity_notice: '',
  },
  capacity: {
    contract_version: 'article-capacity-v1',
    authorized_articles: 15, reserved_articles: 0, consumed_articles: 0,
    available_articles: 15, over_delivered_articles: 0,
    display_status: 'capacity_reserved', semantics: 'upper_bound_0_to_capacity',
    capacity_source: 'services.article_capacity_contract@article-capacity-v1',
    label: '额度已保留', explanation: '', tone: 'neutral',
  },
  shortfall: { shortfall_articles: 0, counts_as_failure: false, reasons: [] },
  items: [{
    plan_item_id: 'Q900-P01',
    ordinal: 1,
    title: '第 1 篇 · 对比文 → 发到cnblogs.com',
    target_question: '深圳哪家装修公司靠谱',
    content_form: '对比文',
    target_domain: 'cnblogs.com',
    status: { code: 'ready_to_execute', label: '可开工', explanation: '', tone: 'green' },
    gap: { code: 'attack_absence', label: '客户还没进入推荐名单', explanation: '', tone: 'amber' },
    access: { code: 'self_service_publishable', label: '可以直接发布', explanation: '', tone: 'green' },
    duplicate_of_item_id: null,
    rationale: { why: '为什么写', what: '写什么', how: '怎么发' },
    actions: [{ action_id: 'open_writing_task', label: '去写这篇', confirmation: false, enabled: true }],
  }],
  evidence_platforms: [],
  evidence: {},
  labels_version: 'gap-operation-labels-v1',
  generated_at: null,
});

const withGate = (open) => {
  const s = baseSnapshot();
  s.execution_gate = open
    ? { open: true, code: '', label: '', hint: '' }
    : { open: false, code: 'execution_not_open_yet', label: '执行计划待开放',
        hint: '客户确认报价并完成付款后，这里会自动展开可执行的交付计划。' };
  return s;
};

/** 🔴 旧后端形态:**整个键都不存在**,不是 `execution_gate: undefined`。 */
const withoutGate = () => {
  const s = baseSnapshot();
  delete s.execution_gate;
  return s;
};

const render = (snapshot) => renderToString(
  React.createElement(GapPlanExecutionView, {
    snapshot, error: null, loading: false, busy: false,
    onReload: () => {}, onAction: () => {},
  }),
);

// 执行面的两个指纹:任务卡的 data 属性 + 折叠抽屉按钮文案。
// 提示态两个都不该有(它只有一段 <p>)。
const EXECUTION_FINGERPRINTS = ['data-plan-item-id="Q900-P01"', '为什么这样安排'];
const hasExecutionUI = (html) => EXECUTION_FINGERPRINTS.every((f) => html.includes(f));

// ══════════════════════════════════════════════════════════════
console.log('── 0. 判据可用性(渲染不出东西时后面全是"空即通过") ──');
let baselineHtml = '';
try {
  baselineHtml = render(withGate(true));
} catch (err) {
  bad(`闸开态就渲染不出来:${String(err).slice(0, 300)}`);
}
check(baselineHtml.length > 200, `闸开态渲染出 ${baselineHtml.length} 字符(判据有东西可查)`);
check(hasExecutionUI(baselineHtml), '闸开态渲染的是执行面(任务卡 + 依据抽屉都在)');

// ══════════════════════════════════════════════════════════════
console.log('\n── 1. 🔴 bug 场景本身:旧后端不下发 execution_gate ──');
// 这一段就是 963f7b27 声称防住、实际没防住的那个组合。
let missingHtml = null;
let thrown = null;
try {
  missingHtml = render(withoutGate());
} catch (err) {
  thrown = err;
}
check(thrown === null,
  thrown ? `渲染抛异常(= 报价详情页崩到 GlobalErrorBoundary):${String(thrown).slice(0, 200)}`
         : 'execution_gate 键缺失时渲染不抛异常');
check(missingHtml !== null && hasExecutionUI(missingHtml),
  '而且渲染的是**执行面**(旧后端 = 旧行为),不是退化成一句提示');
// 反向对照:证明"渲染的是执行面"这条断言真的分得开两种形态
check(!hasExecutionUI(render(withGate(false))),
  '反向对照:闸关态**不**满足执行面指纹(所以上面那条不是恒真)');

// ══════════════════════════════════════════════════════════════
console.log('\n── 2. 闸关态:只有提示,没有任务卡 ──');
const hintHtml = render(withGate(false));
check(hintHtml.includes('客户确认报价并完成付款后'),
  '渲染了服务端下发的 hint 原文(前端不写第二份中文串)');
check(!hintHtml.includes('data-plan-item-id'), '闸关态不渲染任何任务卡');
check(hintHtml.includes('data-help-target="gap-plan-section"'),
  '闸关态保留小榜锚点');

// ══════════════════════════════════════════════════════════════
console.log('\n── 3. 分档决策函数三态(语义,不是写法) ──');
const S = { open: (s) => s };
void S;
check(resolveExecutionMode(withoutGate(), null, false) === 'execution',
  'gate 缺失 → execution(旧行为)');
check(resolveExecutionMode(withGate(false), null, false) === 'gate_hint',
  'gate.open=false → gate_hint');
check(resolveExecutionMode(withGate(true), null, false) === 'execution',
  'gate.open=true → execution');
// 三个入参组合各自到位,证明这三条不是同一条恒真
check(resolveExecutionMode(null, null, true) === 'loading', '无快照 + loading → loading');
check(resolveExecutionMode(null, { code: 'x' }, false) === 'error_only', '无快照 + 有错 → error_only');

// ══════════════════════════════════════════════════════════════
console.log('\n── 4. 加载态也带锚点(返工订正 2) ──');
const loadingHtml = renderToString(React.createElement(GapPlanExecutionView, {
  snapshot: null, error: null, loading: true, busy: false,
  onReload: () => {}, onAction: () => {},
}));
check(loadingHtml.includes('data-help-target="gap-plan-section"'),
  '加载态带锚点(否则小榜在加载那一瞬间高亮不到任何元素)');

rmSync(outDir, { recursive: true, force: true });

console.log('\n' + '='.repeat(60));
if (failed) { console.log(`🔴 ${failed} 条未通过`); process.exit(1); }
console.log('✅ 交付计划执行面真渲染判据全绿');
