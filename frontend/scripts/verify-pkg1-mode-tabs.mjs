#!/usr/bin/env node
/**
 * 判据 · 包一阶段① 三标签(订正三 / 订正七②)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 本阶段**只改 IA 与呈现**:`campaignMode` 三值沿用现有 state,提交链一个字不动。
 * 所以这里不钉任何与提交体 / 计价相关的东西 —— 那些在阶段③⑤,钉在这会永远"未评估"。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label) => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}`);
    if (!cond) bad += 1;
};

const TABS_RAW = rd('src/components/defensiveGeo/ModeTabs.tsx');
const TABS = strip(TABS_RAW);
const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));
const CARDS = rd('src/components/defensiveGeo/ModeRadioCards.tsx');

// ── A 装配与形态 ────────────────────────────────────────────────────
ok(/<ModeTabs/.test(PAGE), 'A1 标签选择器真的挂在发起页上');
ok(!/<ModeRadioCards/.test(PAGE), 'A1 反臂:三张卡已不再挂载(订正七② 删)');
ok(!/这次体检想解决什么/.test(PAGE),
    'A2 「这次体检想解决什么」标题已删 —— 标签名 + 说明已经把它说了两遍中的一遍');
ok(/role="tablist"/.test(TABS) && /role="tab"/.test(TABS),
    'A3 是真 tablist 语义(不是三个看起来像标签的按钮)');
ok(/ArrowRight|ArrowLeft/.test(TABS),
    'A3 配套:左右箭头可切 —— tablist 的键盘契约,漏了就是键盘用户只能停在第一个');
ok(/data-testid="launch-mode-tabs"/.test(TABS)
    && /data-testid="launch-mode-tab"/.test(TABS)
    && /data-mode=\{o\.mode\}/.test(TABS),
    'A4 稳定锚齐全:容器 / 每个标签 / data-mode(B 的锁钉这些,不锚文案)');
ok(/aria-selected=\{selected\}/.test(TABS), 'A5 选中态可被读屏与判据读到');

// 🔴 A6 机械答「同义输入位 = 1」(订正七):数**页面里真被渲染的模式选择器**,
//    不靠"我看过了"。类型/函数 import 不算(它们不渲染任何东西)。
const MOUNTS = (PAGE.match(/<(ModeTabs|ModeRadioCards|ModeChooser|ModeSelect[A-Za-z]*)(?![A-Za-z])/g) || []);
ok(MOUNTS.length === 1,
    `A6 页内模式选择器恰 1 个(实得 ${MOUNTS.length}: ${MOUNTS.join(',') || '无'})`
    + ' —— 0 个 = 用户选不到;2 个 = 同一概念两个输入位');

// ── B 🔴 文案单源:说明不许在标签组件里重打一遍 ──────────────────────
/**
 * 订正三要求「模式说明文案一字不删」。最容易的写法是把三句话抄进新组件 ——
 * 那样同一段话就有两个源,改一处另一处不跟,而**两处各自看起来都对**。
 */
const EXPLAINERS = [...CARDS.matchAll(/explainer:\s*\n?\s*'([^']+)'/g)].map((m) => m[1]);
ok(EXPLAINERS.length === 3,
    `B0 正样本臂:从 ModeRadioCards 解析出 3 句说明(实得 ${EXPLAINERS.length})`
    + ' —— 解析不出来的话,下面"没抄"的判定不携带信息');
if (EXPLAINERS.length === 3) {
    const copied = EXPLAINERS.filter((t) => TABS_RAW.includes(t));
    ok(copied.length === 0,
        `B1 🔴 说明文案**没有**被抄进 ModeTabs(抄了 ${copied.length} 句)`);
}
ok(/modeOptions\(\)/.test(TABS),
    'B2 说明取自 modeOptions() —— 单一源');
ok(/防守/.test(TABS) && /增长/.test(TABS) && /全面测试/.test(TABS),
    'B3 三个标签短名按订正三命名');

// ── C 默认标签 = 增长(offensive)────────────────────────────────────
ok(/return 'offensive';/.test(CARDS),
    'C1 无参进入时确定性回 offensive(增长)—— 旧链接旧书签行为不变');
ok(!/useState<CampaignMode>\(\s*'defensive'/.test(PAGE),
    'C1 反臂:页面没有把默认硬写成 defensive');

// ── D 🔴 红臂:这些在本支的底(e19fe1496)上都不成立 ──────────────────
const BASE = 'e19fe1496';
let basePage = null;
try {
    basePage = execFileSync('git', ['show', `${BASE}:frontend/src/pages/Diagnosis/NewDiagnosis.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ D0 **未评估**:取不到基线 ${BASE} 源码(${String(e.message).slice(0, 50)})`);
}
if (basePage) {
    ok(/<ModeRadioCards/.test(basePage), 'D1 红臂:改动前挂的确实是三张卡');
    ok(/这次体检想解决什么/.test(basePage), 'D2 红臂:改动前确实有那个标题');
    ok(!/<ModeTabs/.test(basePage), 'D3 红臂:改动前没有标签组件');
    ok(/campaignMode/.test(basePage),
        'D4 配对臂:基线里本来就该有的锚确实在(否则 D3 的"没有"只是取到空内容)');
}

// ── E dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ E0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const e1 = js.includes('launch-mode-tabs');
    ok(e1, 'E1 标签锚进了产物(源码有 ≠ 产物有,tree-shaking 会摇没)');
    if (e1) ok(!js.includes('zzq-必然不存在的锚'), 'E2 反臂:编造的锚必须不命中');
    else { notEvaluated += 1; console.log('  ⚠️ E2 未评估:E1 未成立 ⇒ 提取可能整体为空'); }
}

if (bad > 0) {
    console.log(`\n🔴 ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exit(1);
}
if (notEvaluated > 0) {
    console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`);
    process.exit(3);
}
console.log('\n✅ 全部通过');
process.exit(0);
