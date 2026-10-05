#!/usr/bin/env node
/**
 * 判据 · 包三 §G「报告页(内部)」收口。三态退出码:0 全过 / 1 有失败 / 3 有未评估。
 */
import { readFileSync, existsSync, readdirSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const REL = 'src/pages/Diagnosis/DiagnosisReport.tsx';
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const SRC = strip(readFileSync(join(ROOT, REL), 'utf8'));
const VIEW = strip(readFileSync(join(ROOT, 'src/components/defensiveGeo/DefensiveReportView.tsx'), 'utf8'));
const copyStrings = (s) => [
    ...(s.match(/['"`][^'"`\n]*[一-龥][^'"`\n]*['"`]/g) || []),
    ...(s.replace(/\{[^{}]*\}/g, '~').match(/(?<![=-])>[^<>;]*[一-龥][^<>;]*</g) || []),
];

let bad = 0; let notEvaluated = 0; const skipped = [];
const ENV_SKIPS = ['D0', 'F0'];
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

// ── A 🔴 顶部一句结论(纯函数行为臂)────────────────────────────────
/**
 * 🔴 顶部原来只有分数和等级徽标 —— 那是**读数**不是结论。
 *    她得自己把「62 分 + 成长」翻成一个动作,这就是「3 秒不知该点哪」。
 */
const m = await import(new URL('../src/pages/Diagnosis/report/reportHeadline.ts', import.meta.url).href)
    .catch(() => null);
if (!m) {
    notEvaluated += 1; skipped.push('A0');
    console.log('  ⚠️ A0 **未评估**:没能 import reportHeadline.ts。');
} else {
    const cases = ['领先', '成熟', '成长', '起步', '待提升', '空白', '这是个没见过的档', '', null];
    const outs = cases.map((c) => m.reportHeadline(c, 62));
    ok(outs.every((o) => o && o.conclusion && o.nextStep && o.tone),
        'A1 🔴 **总覆盖**:每一档(含未见过的 / 空 / null)都给得出结论与下一步');
    ok(new Set(outs.map((o) => o.conclusion)).size >= 4,
        `A2 正样本臂:不同档给出不同结论(实得 ${new Set(outs.map((o) => o.conclusion)).size} 种;`
        + '若全一样,A1 的"都给得出"就没有信息量)');
    const unknown = m.reportHeadline('这是个没见过的档', 62);
    ok(unknown.tone === 'neutral',
        'A3 🔴 未知档落**笼统但为真**的一档 —— 给一个具体但可能错的判断更糟:'
        + '错的具体会让她停止寻找并走错方向');
    /**
     * 🔴 A4 第一版没红:毒把未知档改成「已经稳定上榜,**保证**下次也被推荐」,
     *    而我的词表里只有「保证上榜」这个**整词** —— 「保证下次」「稳定上榜」都不命中。
     *    驱动是全的,坏的是**词表**:我按脑子里想到的完整短语列,而承诺是拆开说的。
     *    改法三条:①词表按**单词**列不按短语;②驱动扩到「每个档位 × 有分/无分」;
     *    ③加**注入正样本** —— 拿同一个谓词去测一个必然违规的串,证明尺子有牙。
     */
    const PROMISE = /保证|一定|绝对|必然|稳定上榜|保排名|几乎每次|100%|90%\+?/;
    const allOuts = [];
    for (const lv of cases) { allOuts.push(m.reportHeadline(lv, 62), m.reportHeadline(lv, null)); }
    ok(allOuts.length === cases.length * 2,
        `A4.0 正样本臂:驱到了每个档位 × 有分/无分(实得 ${allOuts.length} 条输出)`);
    const promiseHits = allOuts.filter((o) => PROMISE.test(o.conclusion + o.nextStep));
    ok(promiseHits.length === 0,
        `A4 🔴 零绝对化承诺(元指令)——只描述现状与下一步,不预测结果`
        + `(实得 ${JSON.stringify(promiseHits.map((o) => o.conclusion).slice(0, 3))})`);
    ok(PROMISE.test('已经稳定上榜,保证下次也被推荐'),
        'A4a 🔴 注入正样本:同一个谓词对必然违规的串**必须命中** —— '
        + '否则 A4 的"零"只是词表坏了(这条正是上一版漏掉的那发毒的原文)');
    ok(outs.every((o) => (o.nextStep.match(/。/g) || []).length <= 1),
        'A5 下一步只说**一件**事(列三个选项等于没说)');
    const noScore = m.reportHeadline('成长', null);
    ok(!/null|NaN|undefined/.test(noScore.conclusion),
        'A6 🔴 没有分数时不许把 null/NaN 画到屏幕上');
}
ok(/data-testid="report-headline"/.test(SRC), 'A7 页面渲染了那一句');
ok(/reportHeadline\(report\.detail\?\.level, report\.detail\?\.total_score\)/.test(SRC),
    'A8 🔴 它读的是**报告自己的**等级与分数,不是页面另算一份');

// ── B 🔴「给客户看的版本」入口 ──────────────────────────────────────
ok(/data-testid="report-customer-version"/.test(SRC), 'B1 那排按钮上方有一块说明');
const bIdx = SRC.indexOf('report-customer-version');
const bBlock = SRC.slice(bIdx, bIdx + 900);
ok(bBlock.length > 300, 'B0 正样本臂:切到了说明块');
ok(/给客户看的版本/.test(bBlock), 'B2 明说这条链接是对外的那一份');
ok(/不会看到/.test(bBlock) && /(内部备注|成本)/.test(bBlock),
    'B3 🔴 明说客户**看不到**内部备注与成本 —— 不知道对方看到什么她就不敢发');
ok(/不需要注册|不需要登录|不用登录/.test(bBlock), 'B4 说清客户不用注册登录(token-only)');

// ── C 🔴 #63 五卡回落仍在(前一包做的,这里防回归)────────────────────
ok(/presentation\.cards\.length === 0 &&/.test(VIEW),
    'C1 🔴 一张卡都没有 ⇒ **不渲染五个空壳**(#63,前包已修,这里防回归)');
ok(/presentation\.cards\.length > 0 &&/.test(VIEW), 'C2 有卡才渲染网格');

// ── D 🔴 对客文案不说内部机制 ───────────────────────────────────────
const cs = copyStrings(SRC);
const JARGON = ['token', 'draft', 'stage', 'SOV', 'target_share', '净化测度', 'qwen3-max'];
ok(cs.length >= 40, `D0 正样本臂:抽到 ${cs.length} 条对客文案`);
const hit = JARGON.filter((j) => cs.some((c) => c.includes(j)));
ok(hit.length === 0, `D1 🔴 零工程术语(实得 ${JSON.stringify(hit)})`);
ok(JARGON.some((j) => 'target_share 很高'.includes(j)), 'D2 反向对照:词表对样例串确实命中');

// ── E 🔴 红臂 ───────────────────────────────────────────────────────
const BASE = 'a37f9cc1e';
let baseSrc = null;
try {
    baseSrc = execFileSync('git', ['show', `${BASE}:frontend/${REL}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 8 << 20 });
} catch (e) {
    notEvaluated += 1; skipped.push('D0');
    console.log(`  ⚠️ D0 **未评估**:取不到基线 ${BASE}(${String(e.message).slice(0, 50)})`);
}
if (baseSrc) {
    ok(!/report-headline/.test(baseSrc), 'E1 红臂:改动前顶部没有那一句结论');
    ok(!/report-customer-version/.test(baseSrc), 'E2 红臂:改动前没有「给客户看的版本」说明');
    ok(/handleCopyShareLink/.test(baseSrc),
        'E3 配对臂:基线里本来就有的锚确实在(否则上面两条"没有"只是取到空内容)');
}

// ── F dist 锚 ───────────────────────────────────────────────────────
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1; skipped.push('F0');
    console.log('  ⚠️ F0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验。');
} else {
    const js = readdirSync(distDir).filter((f) => f.endsWith('.js'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(js.includes('report-customer-version'), 'F1 客户版说明进了产物');
    ok(js.includes('report-headline'), 'F2 顶部结论也进了产物');
}

const unexpected = skipped.filter((t) => !ENV_SKIPS.includes(t));
if (unexpected.length) { bad += 1; console.log(`  🔴 SKIP-GUARD 清单外未评估:${JSON.stringify(unexpected)}`); }
if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
