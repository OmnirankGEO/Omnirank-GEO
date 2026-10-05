#!/usr/bin/env node
/**
 * WO_267-A 反臂 —— 没毒过的锁不能当证据。
 *
 * 读法(不看 rc):每一发都要「**点名的那几格 OK→FAIL ∧ 其余格一个都不许动 ∧ 源文件逐字回位**」。
 *   · 一发把整页打崩的毒也会让点名格变红 —— 「其余不动」挡的就是这种假咬;
 *   · 毒落地后先过语法尺子:写成语法错 = 打包失败、所有格一起消失,那是「毒没下成」。
 *
 * 九发,每条臂各一发(同一文件的不同改法各算一发):
 *   Q1 历史表格退回「industry || industry_category」直出原值       ⇒ H1 H3
 *   Q2 翻译函数把像 key 的原值原样返回(不查字典)                  ⇒ H1 H3
 *   Q3 改选大类后重新预览不带 category_key                         ⇒ D2
 *   Q4 主榜不带 brand_id                                           ⇒ P3
 *   Q5 在飞任务 /active-task 不带 brand_id                          ⇒ P2
 *   Q6 历轮 /rounds 不带 brand_id                                   ⇒ P4
 *   Q7 not_open 时不说「尚未开通」                                  ⇒ P1
 *   Q8 客户资料保存**总是**带 industry_category(没动下拉也带)       ⇒ B2
 *   Q9 下拉提交中文名而不是字典 key                                 ⇒ B1 B3
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const ARM = join(ROOT, 'scripts', 'test-industry-taxonomy-render.mjs');
const F = (rel) => join(ROOT, 'src', ...rel.split('/'));

const POISONS = [
    { id: 'Q1', why: '历史表格退回直出原值', file: 'pages/History/HistoryList.tsx',
        from: '<TableCell>{industryTextOf(item)}</TableCell>',
        to: '<TableCell>{item.industry || item.industry_category || "-"}</TableCell>',
        targets: ['H1', 'H3'] },
    { id: 'Q2', why: '翻译函数不查字典,像 key 的原值原样返回', file: 'lib/industryTaxonomyText.ts',
        from: '    if (looksLikeCategoryKey(value)) return nameOf(value);',
        to: '    if (looksLikeCategoryKey(value)) return value;',
        targets: ['H1', 'H3'] },
    { id: 'Q3', why: '改选大类后重新预览不带 category_key', file: 'components/publishing/ResearchSelfserveDialog.tsx',
        from: 'body: JSON.stringify({ industry: ind, brand_id: brandId ?? undefined, category_key: categoryKey ?? undefined }),',
        to: 'body: JSON.stringify({ industry: ind, brand_id: brandId ?? undefined }),',
        targets: ['D2'] },
    { id: 'Q4', why: '主榜不带 brand_id', file: 'components/publishing/MediaEffectivenessPanel.tsx',
        from: "      if (brandId) q.set('brand_id', String(brandId));\n", to: '',
        targets: ['P3'] },
    { id: 'Q5', why: '在飞任务不带 brand_id', file: 'pages/Publishing/PublishCenter.tsx',
        from: "    if (proj?.brand_id) activeTaskQuery.set('brand_id', String(proj.brand_id));\n", to: '',
        targets: ['P2'] },
    { id: 'Q6', why: '历轮不带 brand_id', file: 'components/publishing/ResearchRoundReportDialog.tsx',
        from: "      if (brandId) q.set('brand_id', String(brandId));\n", to: '',
        targets: ['P4'] },
    { id: 'Q7', why: 'not_open 时不说「尚未开通」', file: 'pages/Publishing/PublishCenter.tsx',
        from: "{mode === 'proxy' && !imageNoteMode && researchStatus?.status === 'not_open' && (",
        to: "{mode === 'proxy' && !imageNoteMode && researchStatus?.status === 'never' && (",
        targets: ['P1'] },
    { id: 'Q8', why: '保存总是带 industry_category(没动下拉也带)', file: 'pages/Brand/BrandDetailPage.tsx',
        from: '    if (categoryPick.touched) payload.industry_category = categoryPick.key;',
        to: '    payload.industry_category = categoryPick.key;',
        targets: ['B2'] },
    { id: 'Q9', why: '下拉提交中文名而不是字典 key', file: 'components/brand/IndustryCategorySelect.tsx',
        from: '<option key={c.key} value={c.key}>{c.name}</option>',
        to: '<option key={c.key} value={c.name}>{c.name}</option>',
        targets: ['B1', 'B3'] },
];

const run = () => {
    let out = '';
    try { out = execFileSync(process.execPath, [ARM], { cwd: ROOT, encoding: 'utf8', timeout: 15 * 60 * 1000 }); }
    catch (e) { out = String(e.stdout || '') + String(e.stderr || ''); }
    /* 格行缩进两格;顶格的汇总行不是格;不许跨行(WO_258 修过的读法坑) */
    const cells = {};
    for (const m of out.matchAll(/^[ \t]+(OK|FAIL)[ \t]+[^A-Za-z0-9\n]*([A-Za-z0-9][^\s]*)/gm)) cells[m[2]] = m[1];
    return cells;
};

console.log('=== 基线(逐格读数;不看 rc)===');
const base = run();
const ids = Object.keys(base);
const red = ids.filter((k) => base[k] !== 'OK');
console.log(`  ${ids.length} 格 · 红 ${red.length}${red.length ? `(${red.join(',')})` : ''}`);
if (!ids.length || red.length) { console.log('🔴 基线不干净(或一格都没读到)—— 注毒读数无意义'); process.exit(3); }

let bad = 0;
for (const p of POISONS) {
    console.log(`\n=== ${p.id} ${p.why}(点名 ${p.targets.join(' / ')})===`);
    const path = F(p.file);
    const original = readFileSync(path, 'utf8');
    const hits = original.split(p.from).length - 1;
    if (hits !== 1) { console.log(`  🔴 毒没下成:${p.file} 锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`); bad += 1; continue; }
    let after = {};
    let landed = true;
    try {
        writeFileSync(path, original.replace(p.from, p.to), 'utf8');
        if (!syntaxOk(path)) { console.log('  🔴 毒没下成:写成了语法错'); landed = false; }
        else after = run();
    } finally {
        writeFileSync(path, original, 'utf8');
    }
    const back = readFileSync(path, 'utf8') === original;
    if (!landed || !back) { bad += 1; if (!back) console.log('  🔴 源文件没回位'); continue; }
    const flipped = p.targets.filter((k) => base[k] === 'OK' && after[k] === 'FAIL');
    const missed = p.targets.filter((k) => !flipped.includes(k));
    const drift = ids.filter((k) => !p.targets.includes(k) && after[k] !== base[k]);
    const good = !missed.length && !drift.length;
    if (!good) bad += 1;
    console.log(`  点名格:${p.targets.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(' · ')}`);
    console.log(`  其余格漂移:${drift.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(',') || '无'} · 源文件逐字回位`);
    console.log(`  ${good ? '✅ 有牙' : `🔴 不过${missed.length ? `(没红:${missed.join(',')})` : ''}${drift.length ? '(别的格跟着动了)' : ''}`}`);
}
console.log(bad ? `\n🔴 ${bad} 发不过` : `\n全部通过:${POISONS.length} 发,每发只红点名的格`);
process.exit(bad ? 1 : 0);
