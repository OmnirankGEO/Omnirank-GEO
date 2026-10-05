#!/usr/bin/env node
/**
 * 判据 · 算价过期第三态 + 0 题回 idle(WO §3.2 第三态 · Review 2026-09-04 裁 (i))。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 * 「未评估」独立成一档 —— 它与「暂时还没跑」在退出码上必须分得开。
 *
 * 🔴 本脚本是**静态**检查(源码 + dist + git)。运行时命题(过期标记真的出现在
 *    浏览器里、点了按钮真的重新 preview)**不放这里** —— 放这里它永远只能是
 *    "未评估"。那些在生产 harness 的 `--repro=cached-rule` 三检查点里验。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const PAGE = readFileSync(join(ROOT, 'src/pages/Diagnosis/NewDiagnosis.tsx'), 'utf8');
const PANEL_RAW = readFileSync(join(ROOT, 'src/components/defensiveGeo/LaunchPanel.tsx'), 'utf8');
const stripComments = (src) => src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');
const PANEL = stripComments(PANEL_RAW);
const PAGE_CODE = stripComments(PAGE);

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, extra = '') => {
    console.log(`${cond ? '  ✅' : '  🔴'} ${label}${extra ? ' · ' + extra : ''}`);
    if (!cond) bad += 1;
};

// ── A 纯逻辑臂:题集签名 ────────────────────────────────────────────
const guard = await import(new URL('../src/pages/Diagnosis/launch/planSideGuard.ts', import.meta.url).href)
    .catch(() => null);
if (!guard) {
    notEvaluated += 1;
    console.log('  ⚠️ A0 **降级 ⇒ 未评估**:没能真 import .ts,纯逻辑臂一条都没执行。'
        + ' 解法:Node ≥22.6 的 --experimental-strip-types 或 tsx。');
} else {
    const sig = guard.questionSetSignature;
    const a = { modeSide: 'defensive', text: '甲' };
    const b = { modeSide: 'offensive', text: '乙' };
    ok(sig([a, b]) === sig([b, a]),
        'A1 签名**顺序无关**(拖动排序不该被判成"题单变了" ⇒ 否则是假过期)');
    ok(sig([a]) !== sig([a, b]),
        'A1 反臂:加了一道题签名必须变(否则过期永远判不出来)');
    ok(sig([a, { modeSide: 'offensive', text: '   ' }]) === sig([a]),
        'A2 空白题不计(一个空行不该制造一次假过期;后端也不认空题)');
    ok(sig([{ modeSide: 'defensive', text: '同' }]) !== sig([{ modeSide: 'offensive', text: '同' }]),
        'A3 侧别参与签名(同一句话换侧,身份与计价都变了)');
}

// ── B 源码结构臂 ────────────────────────────────────────────────────
ok(/liveQuestionCount === 0 && questionPlan/.test(PAGE_CODE) && /setQuestionPlan\(null\)/.test(PAGE_CODE),
    'B1 题数为 0 ⇒ 清 plan(裁定 (i):回 idle 显示缓存规则)');
ok(!/liveQuestionCount === 0[\s\S]{0,200}setPricingRule\(null\)/.test(PAGE_CODE),
    'B1 反臂:0 题时**不清 pricingRule** —— 要显示的正是那份缓存');
ok(/liveQuestionCount > 0[\s\S]{0,120}!==\s*pricedSignature/.test(PAGE_CODE),
    'B2 过期判定要求**题数 > 0**(0 题走清 plan 那条,不是过期那条)');
ok(/data-testid="launch-price-stale"/.test(PANEL) && /data-testid="launch-reprice"/.test(PANEL),
    'B3 过期时有**说明行 + 可点按钮**(:83 提示二选一:要么有修复动作,要么不显示)');
ok(/priceIsStale && 'opacity-45'/.test(PANEL) || /opacity-45/.test(PANEL),
    'B3 过期时价格块置灰(置灰是在说「别照这个数做决定」)');
ok(/onRepriceRequested\?\.\(\)/.test(PANEL),
    'B4 按钮走**回调**,由页面调现役 preview');
ok(!/useEffect[\s\S]{0,200}priceIsStale[\s\S]{0,120}runDefensivePlanPreview/.test(PAGE_CODE),
    'B4 反臂:**不自动重算** —— 题集一变就打后端会撞应用层限流,'
    + '且放大「她看到的那版 ≠ 她确认的那版」');
ok(!/priceIsStale[\s\S]{0,200}[*+]\s*(basePoints|extraPerQuestion)/.test(PANEL),
    'B5 反臂:过期态**不对价格做任何算术**(前端不算钱)');

// ── C 🔴 红臂:这些锚在现网版本里**都不存在** ───────────────────────
/**
 * 一条「加了个 testid 然后断言它在」的判据,证明不了这次改动有效 ——
 * 它在任何有这个 testid 的版本上都绿。**红臂要证的是:改之前它是红的。**
 * 这里直接取现网尖 51f59a8a5 的**同一批文件**来比。
 */
const PROD_TIP = '51f59a8a5';
let prodPanel = null;
let prodPage = null;
try {
    prodPanel = execFileSync('git', ['show', `${PROD_TIP}:frontend/src/components/defensiveGeo/LaunchPanel.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    prodPage = execFileSync('git', ['show', `${PROD_TIP}:frontend/src/pages/Diagnosis/NewDiagnosis.tsx`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1;
    console.log(`  ⚠️ C0 **未评估**:取不到现网尖 ${PROD_TIP} 的源码(${String(e.message).slice(0, 60)})`
        + ' ⇒ 红臂没跑,不能声称"改动有效"。');
}
if (prodPanel && prodPage) {
    ok(!/launch-price-stale/.test(prodPanel), 'C1 红臂:现网**没有** launch-price-stale');
    ok(!/launch-reprice/.test(prodPanel), 'C2 红臂:现网**没有** launch-reprice');
    ok(!/pricedSignature/.test(prodPage), 'C3 红臂:现网**没有** pricedSignature(过期判不出来)');
    ok(!/liveQuestionCount === 0 && questionPlan/.test(prodPage),
        'C4 红臂:现网 0 题时**不清 plan** ⇒ 面板停在 plan 分支 ⇒ 缓存规则永远显示不出来');
    // 配对臂:证明这个取法能看见东西(否则上面四个"不存在"可能只是取了个空文件)
    ok(/data-testid="launch-panel"/.test(prodPanel) && /questionPlan/.test(prodPage),
        'C5 配对臂:现网源码里那些**本来就该有**的锚确实在'
        + '(否则 C1-C4 的"没有"只是因为我取到了空内容)');
}

// ── D dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  ⚠️ D0 **未构建 ⇒ 未评估**:没有 dist,产物锚一条都没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    const d1 = js.includes('launch-price-stale');
    const d2 = js.includes('launch-reprice');
    const d3 = js.includes('题单变了');
    ok(d1, 'D1 过期标记锚在产物里(源码有 ≠ 产物有,tree-shaking 会摇没)');
    ok(d2, 'D2 重算按钮锚在产物里');
    ok(d3, 'D3 过期**文案**在产物里');
    if (d1 && d2 && d3) {
        ok(!js.includes('zzq-必然不存在的锚'), 'D4 反臂:编造的锚必须不命中');
    } else {
        notEvaluated += 1;
        console.log('  ⚠️ D4 未评估:D1-D3 未全成立 ⇒ 提取可能整体为空,'
            + '此时「编造锚不命中」不携带任何信息');
    }
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
