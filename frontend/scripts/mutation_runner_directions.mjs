#!/usr/bin/env node
/**
 * 注毒自证 · #185 文章方向契约 —— 证明「防御型」那道开关**真的有牙**。
 *
 * 由来(Review 09-13):0913b 这班带我的前端、**不带** C 的 `USER_CHOICE_VALUES`,
 * 用户选了「防御型(公司词)」点「重新生成标题」就是 **422**,
 * 而界面上那一档看起来完全正常(有标签、有说明、能选中)—— #176 那个形状。
 * 裁定:这班不得暴露该档 ⇒ 编译期开关 `DEFENSIVE_DIRECTION_ENABLED = false`。
 *
 * 开关这种东西最容易变成摆设(值改了、过滤没接上),所以必须毒:
 *   ① 开关翻成 true 而后端仍没有 ⇒ D2 **红**(会 422 的死入口);
 *   ② 开关说关、对外清单却没过滤 ⇒ D2b **红**(开关没接上);
 *   ③ 契约值被改名 ⇒ D1 **红**(发一个后端不认的值)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = { dir: 'scripts/verify-article-directions-contract.mjs' };
const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

function run(gateRel) {
    let out = '';
    let rc = 0;
    try {
        out = execFileSync(process.execPath, [abs(gateRel)], {
            cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 5 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

const DIR = 'src/pages/Writing/articleDirections.ts';

const POISONS = [
    {
        id: 'X1', file: DIR,
        why: '开关翻成 true 而后端仍不认(= 界面上露出一档,用户选了就 422,'
            + '而那一档看起来完全正常)',
        from: 'export const DEFENSIVE_DIRECTION_ENABLED = false;',
        to: 'export const DEFENSIVE_DIRECTION_ENABLED = true;',
        expectRed: { dir: ['D2'] },
    },
    {
        id: 'X2', file: DIR,
        why: '开关说关、对外清单却不过滤(= 开关是个摆设,值改了没接上)',
        from: 'export const ARTICLE_DIRECTIONS: DirectionOption[] = ALL_ARTICLE_DIRECTIONS.filter(\n    (d) => DEFENSIVE_DIRECTION_ENABLED || !d.defensive);',
        to: 'export const ARTICLE_DIRECTIONS: DirectionOption[] = ALL_ARTICLE_DIRECTIONS;',
        expectRed: { dir: ['D2b'] },
    },
    {
        id: 'X3', file: DIR,
        why: '契约值改名(= 发一个后端不认的值,422 在用户点完之后才炸)',
        from: "        value: 'company_facts', label: '企业事实与品牌说明', emoji: '🏢',",
        to: "        value: 'company_facts_TYPO', label: '企业事实与品牌说明', emoji: '🏢',",
        expectRed: { dir: ['D1'] },
    },
];

say('=== 0. 基线(失败集必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    const r = run(gate);
    baseline[name] = r.redSet;
    /* rc=3 是"有未评估",不是失败;判基线干净只看失败集。 */
    const good = (r.rc === 0 || r.rc === 3) && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) { say('  🔴 基线不干净,注毒读数无意义。'); process.exit(1); }
}

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红(恒 true 的尺子与
 *    「每一发毒都下成了」读数完全同形)。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const target = abs(p.file);
    const bak = `${target}.x185bak-${p.id}`;
    const before = sha(target);
    copyFileSync(target, bak);
    const src = readFileSync(target, 'utf8');
    const hits = src.split(p.from).length - 1;
    if (hits !== 1) {
        say(`  FAIL 毒没下成:锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`);
        unlinkSync(bak); failures += 1; continue;
    }
    try { assertRulerWorks(target); } catch (e) { say(`  ${e.message}`); copyFileSync(bak, target); unlinkSync(bak); process.exit(3); }
    writeFileSync(target, src.replace(p.from, p.to), 'utf8');
    if (!syntaxOk(target)) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        copyFileSync(bak, target); unlinkSync(bak); failures += 1; continue;
    }
    if (sha(target) === before) {
        say('  FAIL 毒没下成:sha256 没变');
        copyFileSync(bak, target); unlinkSync(bak); failures += 1; continue;
    }
    say(`  毒已落地 sha ${before.slice(0, 12)} → ${sha(target).slice(0, 12)}`);

    let caught = true;
    for (const [name, gate] of Object.entries(GATES)) {
        const want = p.expectRed[name] || [];
        if (!want.length) continue;
        const r = run(gate);
        const newRed = [...r.redSet].filter((x) => !baseline[name].has(x));
        const missed = want.filter((w) => ![...r.redSet].some((x) => x === w || x.startsWith(w)));
        const good = missed.length === 0 && r.rc !== 0 && r.rc !== 3;
        if (!good) caught = false;
        say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}]`
            + `${missed.length ? ` 🔴 没红=[${missed.join(',')}]` : ' 全中'}`);
        say(`       新增报红 ${newRed.length} 条:${newRed.slice(0, 6).join(',') || '(无)'}`);
    }
    if (!caught) failures += 1;

    copyFileSync(bak, target);
    unlinkSync(bak);
    const backHits = readFileSync(target, 'utf8').split(p.from).length - 1;
    const good = sha(target) === before && backHits === 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} 还原:sha ${sha(target) === before ? '一致' : '🔴 不一致'}`
        + ` · 原文回位 ${backHits === 1 ? '是' : `🔴 否(${backHits} 处)`}`);
    if (!good) failures += 1;
}

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`);
process.exit(0);
