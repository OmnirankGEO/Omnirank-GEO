#!/usr/bin/env node
/**
 * 注毒自证 · #181 媒体列表下一页竞态 —— 证明两套判据**真的有牙**。
 *
 * 纪律(每一条都是踩过的坑,#178 当天刚被咬全):
 * 1. 先证**基线失败集为空**;基线本来就红时,每发毒都会显示"命中"。
 * 2. 比**失败集的差**,不比 rc;并打印是哪一条抓住的(钝杀与真抓在 rc 上同形)。
 * 3. 毒必自证下成了:下毒前后 sha256 必须不同;锚必须**恰好命中一次**
 *    (命中 0 或 ≥2 一律报「毒没下成」,而不是「锁没牙」—— 这两件事必须分得开)。
 * 4. 还原用**字节拷回**,不用 `git checkout --`(后者回的是 HEAD,不是"下毒前那份");
 *    判据是 **sha 回到下毒前** + **原文恰好回位一处**,`git diff` 只作旁证
 *    (它比的是 HEAD:对已改未提交的文件恒报有差异、对新建 untracked 的恒报干净)。
 * 5. 每发毒写死 `expectRed`:红必须落在**它那一格**。
 *
 * 跑法:cd frontend && npm run build && node scripts/mutation_runner_media_list_paging.mjs
 *      加 --no-browser 跳过浏览器臂(快;但那几条的牙就没证到,交付物要写明)
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const NO_BROWSER = process.argv.includes('--no-browser');

const GATES = {
    paging: 'scripts/verify-media-list-paging.mjs',
    browser: 'scripts/test-media-list-page-race.mjs',
};

const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

function run(gateRel) {
    let out = '';
    let rc = 0;
    try {
        out = execFileSync(process.execPath, [abs(gateRel)], {
            cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, timeout: 20 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

const PAGE = 'src/pages/Publishing/PublishCenter.tsx';
const MOD = 'src/pages/Publishing/mediaListPaging.ts';

const POISONS = [
    {
        id: 'Q1', file: PAGE,
        why: '把根因放回去:facets 回包里重新 setWmPage(1)(= 慢包把用户打回第 1 页)',
        from: `      if (shouldClearIndustry({ selected: wmIndustry, facetKeys: industries.map(x => x.key) })) {
        setWmIndustry('');
      }`,
        to: `      if (shouldClearIndustry({ selected: wmIndustry, facetKeys: industries.map(x => x.key) })) {
        setWmIndustry(''); setWmPage(1);
      }`,
        expectRed: { paging: ['A1', 'A3'], browser: ['R1b'] },
    },
    {
        id: 'Q2', file: PAGE,
        why: '撤掉自媒体 facets 的陈旧包闸(= 上一次筛选的答案按在这一次的状态上)',
        from: `      if (isStalePacket(epoch, wmEpochRef.current)) return;`,
        to: `      if (false) return;`,
        expectRed: { paging: ['B5'] },
    },
    {
        id: 'Q3', file: MOD,
        why: 'canGoNext 不看 loading(= 切筛选后到回包前用旧 pages 放行,请求越界页 ⇒ 屏幕"没翻")',
        from: `export function canGoNext(input: { page: number; pages: number; loading: boolean }): boolean {
    if (input.loading) return false;`,
        to: `export function canGoNext(input: { page: number; pages: number; loading: boolean }): boolean {
    if (false) return false;`,
        expectRed: { paging: ['D3'], browser: ['R2a'] },
    },
    {
        id: 'Q4', file: MOD,
        why: '钳位改用"请求页"而不是回显 pages(= 越界页钳不回来)',
        from: `    if (pages > 0 && echoed > pages) {
        return { page: pages, pages, clamped: true };
    }`,
        to: `    if (false) {
        return { page: pages, pages, clamped: true };
    }`,
        expectRed: { paging: ['C1'] },
    },
    {
        id: 'Q5', file: MOD,
        why: 'facet 为空时也清行业(= 这次没算出来就把用户自己选的条件丢了)',
        from: `    if (!input.facetKeys || input.facetKeys.length === 0) return false;`,
        to: `    if (false) return false;`,
        expectRed: { paging: ['E4'] },
    },
    {
        id: 'Q6', file: PAGE,
        why: '软文 tab 不做钳位(= 两个 tab 分家,同一个谓词两处必有一处没人验)',
        from: `        const st = reconcilePage(requested, d);
        setMhzPages(st.pages);
        if (st.page !== requested) setMhzPage(st.page);`,
        to: `        setMhzPages(d.pages || 0);`,
        /**
         * 🔴 只期望 A2:C4 数的是 `const requested = ...Page;` 的处数,这发毒没动那一行
         *    (它只删了钳位),所以 C4 抓不到 —— 那是我指错了格,不是锁没牙。
         *    真正守着"软文 tab 也得钳"的是 A2。
         */
        expectRed: { paging: ['A2'] },
    },
    {
        id: 'Q7', file: PAGE,
        why: '翻页键改回裸比较(= 在途窗口照样放行)',
        from: `disabled={!canGoNext({ page: wmPage, pages: wmPages, loading: wmLoading })}`,
        to: `disabled={wmPage >= wmPages}`,
        expectRed: { paging: ['D6', 'D7'], browser: ['R2a'] },
    },
];

// ══ 0. 基线 ═══════════════════════════════════════════════════════════
say('=== 0. 基线(三套判据的失败集都必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    if (name === 'browser' && NO_BROWSER) { say('  browser: --no-browser,跳过(牙未证,交付物须写明)'); continue; }
    const r = run(gate);
    baseline[name] = r.redSet;
    const good = r.rc === 0 && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) { say('  🔴 基线不干净,注毒读数无意义。先修基线。'); process.exit(1); }
}

// ══ 1. 逐发注毒 ═══════════════════════════════════════════════════════
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红(恒 true 的尺子与
 *    「每一发毒都下成了」读数完全同形)。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const target = abs(p.file);
    const bak = `${target}.a179bak-${p.id}`;
    const before = sha(target);
    copyFileSync(target, bak);
    const src = readFileSync(target, 'utf8');
    const hits = src.split(p.from).length - 1;
    if (hits !== 1) {
        say(`  FAIL 毒没下成:锚命中 ${hits} 次(要恰好 1 次)—— 锚不唯一/已漂移,不是"锁没牙"`);
        unlinkSync(bak); failures += 1; continue;
    }
    try { assertRulerWorks(target); } catch (e) { say(`  ${e.message}`); copyFileSync(bak, target); unlinkSync(bak); process.exit(3); }
    writeFileSync(target, src.replace(p.from, p.to), 'utf8');
    if (!syntaxOk(target)) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        copyFileSync(bak, target); unlinkSync(bak); failures += 1; continue;
    }
    const after = sha(target);
    if (after === before) {
        say('  FAIL 毒没下成:sha256 没变');
        copyFileSync(bak, target); unlinkSync(bak); failures += 1; continue;
    }
    say(`  毒已落地 sha ${before.slice(0, 12)} → ${after.slice(0, 12)}`);

    let caught = true;
    for (const [name, gate] of Object.entries(GATES)) {
        const want = p.expectRed[name] || [];
        if (!want.length) continue;
        if (name === 'browser' && NO_BROWSER) { say(`  browser: 跳过(--no-browser)`); continue; }
        const r = run(gate);
        const newRed = [...r.redSet].filter((x) => !baseline[name].has(x));
        const missed = want.filter((w) => ![...r.redSet].some((x) => x === w || x.startsWith(w)));
        const good = missed.length === 0 && r.rc !== 0;
        if (!good) caught = false;
        say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}]`
            + `${missed.length ? ` 🔴 没红=[${missed.join(',')}]` : ' 全中'}`);
        say(`       新增报红 ${newRed.length} 条:${newRed.slice(0, 8).join(',') || '(无)'}`);
        const okCount = (r.out.match(/^\s*OK/gm) || []).length;
        if (okCount < 5) {
            say(`       🔴 钝杀嫌疑:这一轮只打印了 ${okCount} 条 OK —— 判据可能是崩了不是抓住了`);
            caught = false;
        }
    }
    if (!caught) failures += 1;

    // 还原
    copyFileSync(bak, target);
    unlinkSync(bak);
    const restored = sha(target);
    const backHits = readFileSync(target, 'utf8').split(p.from).length - 1;
    let gitState = 'n/a';
    try {
        execFileSync('git', ['diff', '--quiet', '--', join('frontend', p.file).split('\\').join('/')],
            { cwd: REPO, stdio: 'pipe' });
        gitState = '与 HEAD 无差异';
    } catch { gitState = '与 HEAD 有差异(本单未提交的改动,预期如此)'; }
    const good = restored === before && backHits === 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} 还原:sha vs 下毒前 ${restored === before ? '一致' : '🔴 不一致'}`
        + ` · 原文回位 ${backHits === 1 ? '是' : `🔴 否(${backHits} 处)`}`
        + ` · (旁证 git:${gitState})`);
    if (!good) failures += 1;
}

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`
    + (NO_BROWSER ? '(--no-browser:浏览器臂的牙本轮未证)' : ''));
process.exit(0);
