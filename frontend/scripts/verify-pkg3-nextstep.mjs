#!/usr/bin/env node
/**
 * 判据 · 包三 §G 创作/发布/监测「下一步」条。三态退出码:0 / 1 / 3。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const rd = (p) => strip(readFileSync(join(ROOT, p), 'utf8'));

let bad = 0; let notEvaluated = 0; const skipped = [];
const ENV_SKIPS = ['E0', 'F0'];
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

const PAGES = {
    writing: 'src/pages/Writing/WritingCenter.tsx',
    publish: 'src/pages/Publishing/PublishCenter.tsx',
    monitoring: 'src/pages/Monitoring/index.tsx',
};

// ── A 🔴 三页共用同一个组件与同一份映射 ─────────────────────────────
/**
 * 🔴 每页各写一条"下一步",措辞迟早各走各的,而她会以为那是三个不同的流程。
 *    所以锁**唯一性**:三页都挂同一个组件,映射只有一份。
 */
for (const [key, rel] of Object.entries(PAGES)) {
    const src = rd(rel);
    ok(new RegExp(`<NextStepBar page="${key}"`).test(src), `A1[${key}] 挂了「下一步」条且 page 标对`);
    ok((src.match(/<NextStepBar/g) || []).length === 1, `A1[${key}] 恰挂一次`);
}
const bar = rd('src/components/nav/NextStepBar.tsx');
ok(/mainChainNextStep\(page\)/.test(bar), 'A2 🔴 组件从**唯一映射**取值,不在组件里写死三段文案');
ok(/if \(!step\) return null;/.test(bar),
    'A3 🔴 未知页**不渲染**,不猜一个下一步 —— 错的具体会把她带去跟她无关的页面,'
    + '而她会以为那是流程要求的');

// ── B 🔴 纯函数行为臂 + 路由必须真实存在 ────────────────────────────
const m = await import(new URL('../src/components/nav/mainChainNextStep.ts', import.meta.url).href)
    .catch(() => null);
if (!m) {
    notEvaluated += 1; skipped.push('B0');
    console.log('  ⚠️ B0 **未评估**:没能 import mainChainNextStep.ts。');
} else {
    ok(m.MAIN_CHAIN_PAGES.length === 3, `B1 正样本臂:映射里有 ${m.MAIN_CHAIN_PAGES.length} 页`);
    ok(m.mainChainNextStep('没这一页') === null && m.mainChainNextStep('') === null
        && m.mainChainNextStep(null) === null, 'B2 🔴 未知 / 空 / null 一律返回 null');
    /**
     * 🔴 B3 沿用 nextActionRoute.ts 立的纪律:**只放确认存在的站内路由**。
     *    给一颗点了 404 的按钮比不给更糟。这里逐条回 App.tsx 核,
     *    **分母机械枚举**(遍历 MAIN_CHAIN_PAGES,不手写清单)。
     */
    const app = readFileSync(join(ROOT, 'src/App.tsx'), 'utf8');
    const missing = m.MAIN_CHAIN_PAGES
        .map((p) => m.mainChainNextStep(p).to)
        .filter((to) => !new RegExp(`path="${to.replace(/^\//, '')}"`).test(app));
    ok(missing.length === 0, `B3 🔴 每个「下一步」的落点在 App.tsx 里都是真实路由(实得缺失 ${JSON.stringify(missing)})`);
    ok(!new RegExp('path="zzq-必然不存在"').test(app), 'B3 反向对照:编造的路由必须查不到');
    const steps = m.MAIN_CHAIN_PAGES.map((p) => m.mainChainNextStep(p));
    ok(steps.every((s) => s.doneLabel && s.nextLabel && s.why && s.to),
        'B4 每一页四个字段都齐(缺 why 就等于只给按钮不给理由)');
    ok(new Set(steps.map((s) => s.to)).size === steps.length,
        'B5 三页的下一步各不相同(全指同一处说明映射写塌了)');
    const JARGON = /token|draft|stage|SOV|模块|接口|端点/;
    ok(!steps.some((s) => JARGON.test(s.doneLabel + s.nextLabel + s.why)),
        'B6 🔴 用她的话不用模块名');
    ok(JARGON.test('去 writing 模块'), 'B6 反向对照:词表对样例串确实命中');
}

// ── E 🔴 红臂 ───────────────────────────────────────────────────────
const BASE = '7c578e5e0';
try {
    const baseWriting = execFileSync('git', ['show', `${BASE}:frontend/${PAGES.writing}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
    ok(!/NextStepBar/.test(baseWriting), 'E1 红臂:改动前创作页没有「下一步」条');
    ok(/WritingCenter/.test(baseWriting), 'E2 配对臂:基线里本来就有的锚确实在');
} catch (e) {
    notEvaluated += 1; skipped.push('E0');
    console.log(`  ⚠️ E0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1; skipped.push('F0');
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('main-chain-next-step'), 'F1 「下一步」条进了产物');
}

const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t));
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
