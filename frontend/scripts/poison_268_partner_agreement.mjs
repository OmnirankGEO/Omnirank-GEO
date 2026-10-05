#!/usr/bin/env node
/**
 * WO_268 §4 反臂 —— 没毒过的锁不能当证据。
 *
 * 读法(不看 rc):每一发都要「**我点名的那几格 OK→FAIL ∧ 其余格一个都不许动 ∧ 源文件逐字回位**」。
 *   · 只看 rc 的话,一发把整页打崩的毒也是 rc=1 —— 那证明的是"页面坏了",不是"这一格守得住";
 *   · 毒落地后先过语法尺子:写成语法错的毒会让臂整个打包失败、所有格一起消失,
 *     那和"锁咬住了"在 rc 上同形,必须当「毒没下成」报。
 *
 * 四发,各钉一条:
 *   Q1 提交不再看协议      ⇒ P1s / P1bs 红(失败文案照在:闸与文案是两件事)
 *   Q2 失败时什么都不说    ⇒ P1 / P1b 红(提交照样灰:两件事各有各的格)
 *   Q3 空正文当成加载成功  ⇒ P1b / P1bs 红(404 那两格不动)
 *   Q4 反过来一律判失败    ⇒ P2 / P2s 红 —— 对照臂自己的牙:闸若过度拦截,正常申请也提交不了
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const ARM = join(ROOT, 'scripts', 'test-partner-agreement-fail-closed.mjs');
const PAGE = join(ROOT, 'src', 'pages', 'Partner', 'PartnerApplyStep3.tsx');

const POISONS = [
    {
        id: 'Q1', why: '提交不再看协议是否就绪',
        from: '    agreementReady && allChecked && consentSensitive',
        to: '    allChecked && consentSensitive',
        targets: ['P1s', 'P1bs'],
    },
    {
        id: 'Q2', why: '协议拉不到时什么都不说(整块失败提示拿掉)',
        from: '            ) : agreementFailed ? (',
        to: '            ) : false ? (',
        targets: ['P1', 'P1b'],
    },
    {
        id: 'Q3', why: '200 但正文为空也当成加载成功',
        from: '        setAgreementFailed(!text.trim());',
        to: '        setAgreementFailed(!resp.ok);',
        targets: ['P1b', 'P1bs'],
    },
    {
        id: 'Q4', why: '反过来一律判失败(过度拦截:正常申请也提交不了)',
        from: '        setAgreementFailed(!text.trim());',
        to: '        setAgreementFailed(true);',
        targets: ['P2', 'P2s'],
    },
];

const run = () => {
    let out = '';
    try {
        out = execFileSync(process.execPath, [ARM], { cwd: ROOT, encoding: 'utf8', timeout: 10 * 60 * 1000 });
    } catch (e) {
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    /* 格行缩进两格;顶格的「FAIL N 项不通过」是汇总行。`[ \t]+` 且不跨行(WO_258 修过同一个读法坑)。 */
    const cells = {};
    for (const m of out.matchAll(/^[ \t]+(OK|FAIL)[ \t]+[^A-Za-z0-9\n]*([A-Za-z0-9][^\s]*)/gm)) cells[m[2]] = m[1];
    return cells;
};

console.log('=== 基线(逐格读数;不看 rc)===');
const base = run();
const ids = Object.keys(base);
const red = ids.filter((k) => base[k] !== 'OK');
console.log(`  ${ids.length} 格 · 红 ${red.length}${red.length ? `(${red.join(',')})` : ''}`);
if (!ids.length || red.length) {
    console.log('🔴 基线不干净(或一格都没读到)—— 注毒读数无意义');
    process.exit(3);
}

const original = readFileSync(PAGE, 'utf8');
let bad = 0;
for (const p of POISONS) {
    console.log(`\n=== ${p.id} ${p.why}(点名 ${p.targets.join(' / ')})===`);
    const hits = original.split(p.from).length - 1;
    if (hits !== 1) { console.log(`  🔴 毒没下成:锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`); bad += 1; continue; }
    let after = {};
    try {
        writeFileSync(PAGE, original.replace(p.from, p.to), 'utf8');
        if (!syntaxOk(PAGE)) { console.log('  🔴 毒没下成:写成了语法错(整臂会打包失败,所有格一起消失)'); bad += 1; continue; }
        after = run();
    } finally {
        writeFileSync(PAGE, original, 'utf8');
    }
    const flipped = p.targets.filter((k) => base[k] === 'OK' && after[k] === 'FAIL');
    const missed = p.targets.filter((k) => !flipped.includes(k));
    const drift = ids.filter((k) => !p.targets.includes(k) && after[k] !== base[k]);
    const good = !missed.length && !drift.length;
    if (!good) bad += 1;
    console.log(`  点名格:${p.targets.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(' · ')}`);
    console.log(`  其余格漂移:${drift.map((k) => `${k} ${base[k]}→${after[k] || '消失'}`).join(',') || '无'}`);
    console.log(`  ${good ? '✅ 有牙' : `🔴 不过${missed.length ? `(没红:${missed.join(',')})` : ''}${drift.length ? '(别的格跟着动了)' : ''}`}`);
}
const back = readFileSync(PAGE, 'utf8') === original;
console.log(`\n=== 复原自证 ===\n${back ? '源文件逐字回位' : '🔴 没回位'}`);
if (!back) bad += 1;
console.log(bad ? `\n🔴 ${bad} 发不过` : `\n全部通过:${POISONS.length} 发,每发只红点名的格`);
process.exit(bad ? 1 : 0);
