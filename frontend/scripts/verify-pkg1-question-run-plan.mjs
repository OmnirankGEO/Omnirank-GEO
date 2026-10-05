#!/usr/bin/env node
/**
 * 判据 · 包一阶段③ 题单三态(订正十二)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 本阶段**不碰按钮上的价**。原因写在这里,免得下一个人以为漏做了:
 *    订正七④ 要求「按钮上的价为唯一价」。服务端只读价预览
 *    (`GET /api/pricing/diagnosis-preview`,#84 契约)尚未实现,而前端计价镜像
 *    (`NewDiagnosis.tsx` 的 `customExtraCost`)要到阶段⑤ 才删。
 *    此刻把服务端价接上 = **同屏两个价源**,正是订正七④ 要消灭的东西。
 *    ⇒ 价那半与阶段⑤ 合并(删镜像 + 接服务端价 + 「正在重新算」禁用态,同一笔)。
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

const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));
const EDITOR = strip(rd('src/components/defensiveGeo/QuestionPlanEditor.tsx'));

// ── A 纯逻辑臂:订正十二的三分支 ─────────────────────────────────────
const m = await import(new URL('../src/pages/Diagnosis/launch/questionRunPlan.ts', import.meta.url).href)
    .catch(() => null);
if (!m) {
    notEvaluated += 1;
    console.log('  ⚠️ A0 **未评估**:没能 import questionRunPlan.ts,三分支一条都没执行。');
} else {
    ok(m.questionRunMode(0, false) === 'ai_only', 'A1 她没出题 ⇒ 跑 AI 出的');
    ok(m.questionRunMode(3, false) === 'custom_only', 'A2 🔴 她出了题 ⇒ **只**跑她的(AI 那批不跑)');
    ok(m.questionRunMode(3, true) === 'dual', 'A3 开了双轨 ⇒ 两边都跑');
    ok(m.questionRunMode(0, true) === 'ai_only',
        'A4 边界:她没出题时开关无意义 —— 仍是 ai_only,不是 dual(dual 会把 AI 那批数两遍)');

    // 计价基数 = 实际要跑的题数(订正十二逐字)
    ok(m.effectiveQuestionCount(8, 0, false) === 8, 'A5 计价基数:没出题 ⇒ 8');
    ok(m.effectiveQuestionCount(8, 3, false) === 3, 'A5 🔴 出了 3 道 ⇒ **3**,不是 11(不为没跑的题付钱)');
    ok(m.effectiveQuestionCount(8, 9, false) === 9, 'A5 出了 9 道 ⇒ 9(第 9 道起加价由服务端算)');
    ok(m.effectiveQuestionCount(8, 3, true) === 11, 'A5 双轨 ⇒ 8+3=11');
    ok(m.effectiveQuestionCount(8, 0, true) === 8, 'A5 反臂:没出题时开关不改基数');

    ok(m.aiQuestionsMuted(3, false) === true && m.aiQuestionsMuted(3, true) === false
        && m.aiQuestionsMuted(0, false) === false,
        'A6 折叠置灰的判定与三分支同源(只在 custom_only 折叠)');
    ok(/只跑你出的 3 道/.test(m.mutedNotice(3)),
        'A7 折叠文案里的 N 取**她的题数** —— 不取手边恰好有的别的数');
}

// ── B 结构臂 ────────────────────────────────────────────────────────
ok(/aiQuestionsMuted\(ownQuestionCount, alsoRunAi\)/.test(PAGE),
    'B1 页面从单源取 aiMuted,不自己判三分支');
/**
 * 🔴 B2 原来写成 `精确形 || 宽松形` —— 那样它**只等于宽松的那半**:
 *    页面里随便哪儿出现一次调用就绿,包括三个实参传错。
 *    「或」把一条严判据降级成宽判据,而降级本身不产生任何红。
 *    改成只留精确形,允许跨行(格式化会换行),但实参顺序不许变。
 */
const B2_RE = new RegExp(
    'effectiveQuestionCount\\([\\s\\S]{0,80}?suggestedWithFiller\\.length,'
    + '[\\s\\S]{0,40}?ownQuestionCount,[\\s\\S]{0,40}?alsoRunAi');
ok(B2_RE.test(PAGE),
    'B2 实际要跑的题数从单源算,且三个实参顺序正确(AI 数 / 她的数 / 双轨开关)');
ok(/runningCount=\{effectiveRunCount\}/.test(PAGE),
    'B3 🔴 题单上那个数**下传**,不让编辑器自己数 —— 各算各的就会「显示 5 道、按 8 道收钱」');
ok(/runningCount \?\? running\.length/.test(EDITOR),
    'B3 配套:编辑器优先用传进来的数');
ok(/data-testid="plan-ai-muted"/.test(EDITOR) && /data-testid="plan-ai-muted-note"/.test(EDITOR),
    'B4 折叠置灰块 + 说明行有稳定锚');
ok(/aiMuted \? manual : all/.test(EDITOR),
    'B5 折叠时分组只按她的题算(否则两批混在同一 family 里,分不出哪些会跑)');
ok(/也跑 AI 出的题（双轨）/.test(PAGE) || /也跑 AI 出的题\(双轨\)/.test(PAGE),
    'B6 开关改名为它真正做的事(订正十二)');
ok(!/AI 主动优化我的题/.test(PAGE),
    'B6 反臂:旧文案消失 —— 它描述的是 05-21 被替换掉的语义,留着就是说反话');
ok(!/主分用 baseline/.test(PAGE),
    'B6 反臂二:旧描述里那句"主分用 baseline"也必须走');

// ── C 🔴 红臂:基线 e19fe1496 上都不成立 ─────────────────────────────
const BASE = 'e19fe1496';
let baseEditor = null; let basePage = null;
try {
    const show = (p) => execFileSync('git', ['show', `${BASE}:${p}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    baseEditor = show('frontend/src/components/defensiveGeo/QuestionPlanEditor.tsx');
    basePage = show('frontend/src/pages/Diagnosis/NewDiagnosis.tsx');
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ C0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (baseEditor && basePage) {
    ok(!/plan-ai-muted/.test(baseEditor), 'C1 红臂:改动前没有折叠置灰');
    ok(/共 \{all\.length\} 道/.test(baseEditor),
        'C2 🔴 红臂:改动前那个数是 **all.length** —— 题单里躺着的总数,不是会跑的数');
    ok(/AI 主动优化我的题/.test(basePage), 'C3 红臂:改动前是旧开关文案');
    ok(/QuestionPlanEditor/.test(basePage),
        'C4 配对臂:基线里本来就有的锚确实在(否则 C1 的"没有"只是取到空内容)');
}

// ── D dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ D0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const d1 = js.includes('plan-ai-muted');
    ok(d1, 'D1 折叠块锚进了产物');
    ok(js.includes('也跑 AI 出的题'), 'D2 新开关文案进了产物');
    if (d1) ok(!js.includes('zzq-必然不存在的锚'), 'D3 反臂:编造的锚必须不命中');
    else { notEvaluated += 1; console.log('  ⚠️ D3 未评估:D1 未成立 ⇒ 提取可能整体为空'); }
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
