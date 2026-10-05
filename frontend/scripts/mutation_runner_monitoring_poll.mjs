#!/usr/bin/env node
/**
 * 注毒自证 · #182 监测页轮询节奏 —— 证明两套判据**真的有牙**。
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
 * 跑法:cd frontend && npm run build && node scripts/mutation_runner_monitoring_poll.mjs
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
    poll: 'scripts/verify-monitoring-poll.mjs',
    browser: 'scripts/test-monitoring-poll-timing.mjs',
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

const PANEL = 'src/pages/Monitoring/components/IdentityReviewPanel.tsx';
const INDEX = 'src/pages/Monitoring/index.tsx';
const MOD = 'src/pages/Monitoring/monitoringPollSchedule.ts';

const POISONS = [
    {
        id: 'U1', file: MOD,
        why: '把"没事也接着轮"放回去(= 每个开着监测页的人每分钟两次打同一个端点)',
        from: `    if (!input.hasPending && !input.taskActive) return null;`,
        to: `    if (false) return null;`,
        expectRed: { poll: ['A1', 'A2'], browser: ['M1c'] },
    },
    {
        id: 'U2', file: MOD,
        why: '页面不可见也接着轮(= 用户根本没在看,后台照样打)',
        from: `    if (!input.visible) return null;`,
        to: `    if (false) return null;`,
        expectRed: { poll: ['A1'], browser: ['M3a'] },
    },
    {
        id: 'U3', file: MOD,
        why: '退避失效(= 一直最快档,内容没变也每分钟问)',
        from: `    if (streak >= 3) return POLL_IDLE_MS;
    if (streak >= 2) return POLL_SLOW_MS;`,
        to: `    if (false) return POLL_IDLE_MS;
    if (false) return POLL_SLOW_MS;`,
        expectRed: { poll: ['A3'], browser: ['M2e'] },
    },
    {
        id: 'U4', file: MOD,
        why: '有变化也不回最快档(= 有事发生时用户还得等 5 分钟)',
        from: `    if (changed) return 0;`,
        to: `    if (false) return 0;`,
        expectRed: { poll: ['A4'], browser: ['M2g'] },
    },
    {
        id: 'U5', file: MOD,
        why: '后台重取也进 loading(= 每次轮询整块闪一下 —— Owner 问的就是这个观感)',
        from: `    return !!input.manual || !!input.firstLoad;`,
        to: `    return true;`,
        expectRed: { poll: ['B3'], browser: ['M4d'] },
    },
    {
        id: 'U6', file: PANEL,
        why: '回到前台不立刻取(= 切回来看到的是离开那一刻的快照)',
        from: `            if (document.visibilityState === 'visible') { void poll(); } else { schedule(); }`,
        to: `            schedule();`,
        expectRed: { poll: ['D3'], browser: ['M3b'] },
    },
    {
        id: 'U7', file: PANEL,
        why: '裁决后不重取(= 刚处理完的那条还挂在屏幕上,像"点了没反应")',
        from: `            void load();`,
        to: `            void 0;`,
        expectRed: { poll: ['D7'], browser: ['M5a'] },
    },
    {
        id: 'U8', file: MOD,
        why: '流停滞判定永远为假(= 后端卡住时格子无限 pulse,屏幕说"还在跑")',
        from: `    return now - last >= limit;`,
        to: `    return false;`,
        expectRed: { poll: ['C1'] },
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
