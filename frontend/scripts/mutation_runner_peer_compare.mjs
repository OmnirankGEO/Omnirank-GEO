#!/usr/bin/env node
/**
 * 注毒自证 · #189「同行对比」三档 —— 证明 T1–T6 **真的有牙**。
 *
 * 纪律(每条都是踩过的坑):
 * 1. 先证**基线失败集为空**;基线本来就红时每发毒都会显示"命中"。
 * 2. 比**失败集的差**,不比 rc;并打印是哪一条抓住的。
 * 3. 毒必自证下成了:sha 必须变,锚必须**恰好命中一次**
 *    (命中 0 或 ≥2 报「毒没下成」—— 那和「锁没牙」必须分得开)。
 * 4. 还原用**字节拷回**,不用 `git checkout --`(后者回的是 HEAD)。
 * 5. 🔴 跑它的时候**别同时跑 build 或别的渲染闸**:毒在工作树里,
 *    期间任何构建读到的都是被毒的树。中途也别 kill —— 还原不执行,毒会留在树里。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    peer: 'scripts/verify-peer-compare-labels.mjs',
    /* 🔴 颜色/禁用/请求数这类只有渲染后才成立的,源码层钉不住。 */
    render: 'scripts/test-peer-compare-render.mjs',
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
            cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 10 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

const MOD = 'src/pages/Writing/peerCompareLabels.ts';
const HALL = 'src/pages/Writing/WritingHall.tsx';

const POISONS = [
    {
        id: 'P1', file: MOD,
        why: 'T1 · 去掉前置标签「同行对比」(= 三档退回三个没有主语的形容词,'
            + '正是 Owner 问的那件事)',
        from: "export const PEER_GROUP_LABEL = '同行对比';",
        to: "export const PEER_GROUP_LABEL = '';",
        expectRed: { peer: ['T1e'], render: ["T1'a"] },
    },
    {
        id: 'P1b', file: MOD,
        why: 'T1 · 三档退回只写证据状态(= 没有"我在做什么选择"这层意思)',
        from: "            label: `点名对比 · 只写已核实的 ${verified} 家`,",
        to: "            label: '已核验',",
        expectRed: { peer: ['T1b'] },
    },
    {
        id: 'P2', file: HALL,
        why: 'T2 · 把告警色加回三档(=「不点名」这个正当选择又被画成出错)',
        from: "                                                    ? 'bg-primary text-primary-foreground font-medium'",
        to: "                                                    ? 'bg-red-500 text-white font-medium'",
        expectRed: { peer: ['T2'], render: ["T2'a"] },
    },
    {
        id: 'P3', file: MOD,
        why: 'T3 · 0 家已核实时也放行「点名对比」(= 选了正文点不了名,'
            + '后端硬门打回来,用户以为是自己操作错了)',
        from: "            disabled: verified === 0,",
        to: "            disabled: false,",
        expectRed: { peer: ['T3'], render: ["T3'a"] },
    },
    {
        id: 'P3b', file: MOD,
        why: 'T3 · 不可用但**不说原因**(= 一个永远不动的灰按钮,用户反复点)',
        from: "            disabledReason: verified === 0 ? '还没有核实过的同行,先联网核实' : '',",
        to: "            disabledReason: '',",
        expectRed: { peer: ['T3b'] },
    },
    {
        id: 'P4', file: HALL,
        why: 'T4 · 切档又去触发联网检索(= 回到"一个按钮两种含义",'
            + '用户以为只是在切显示)',
        from: "        const previousMode = competitorMode === 'loading' ? 'evidence_only' : competitorMode;\n        setCompetitorMode(mode);",
        to: "        const previousMode = competitorMode === 'loading' ? 'evidence_only' : competitorMode;\n        if (mode === 'real') { await verifyCurrentCompetitors(); }\n        setCompetitorMode(mode);",
        expectRed: { peer: ['T4c'], render: ["T4'a"] },
    },
    {
        id: 'P5', file: HALL,
        why: 'T5 · 「竞品」字样溜回页面(= Owner 口径是「同行」)',
        from: '                                        <th className="py-1.5 pl-2 text-left">同行名称</th>',
        to: '                                        <th className="py-1.5 pl-2 text-left">竞品名称</th>',
        expectRed: { peer: ['T5'] },
    },
    {
        id: 'P6', file: MOD,
        why: 'T6 · 已排除的也算进数里(= 两个数和眼前的列表对不上)',
        from: "        if (o.excluded === true) continue;",
        to: "        // poisoned",
        expectRed: { peer: ['T6'] },
    },
];

say('=== 0. 基线(失败集必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    const r = run(gate);
    baseline[name] = r.redSet;
    const good = r.rc === 0 && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) { say('  🔴 基线不干净,注毒读数无意义。先修基线。'); process.exit(1); }
}

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红(恒 true 的尺子与
 *    「每一发毒都下成了」读数完全同形)。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const target = abs(p.file);
    const bak = `${target}.a189bak-${p.id}`;
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
        const r = run(gate);
        const newRed = [...r.redSet].filter((x) => !baseline[name].has(x));
        const missed = want.filter((w) => ![...r.redSet].some((x) => x === w || x.startsWith(w)));
        const good = missed.length === 0 && r.rc !== 0;
        if (!good) caught = false;
        say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}]`
            + `${missed.length ? ` 🔴 没红=[${missed.join(',')}]` : ' 全中'}`);
        say(`       新增报红 ${newRed.length} 条:${newRed.slice(0, 6).join(',') || '(无)'}`);
        const okCount = (r.out.match(/^\s*OK/gm) || []).length;
        if (okCount < 5) {
            say(`       🔴 钝杀嫌疑:这一轮只打印了 ${okCount} 条 OK —— 判据可能是崩了不是抓住了`);
            caught = false;
        }
    }
    if (!caught) failures += 1;

    copyFileSync(bak, target);
    unlinkSync(bak);
    const restored = sha(target);
    const backHits = readFileSync(target, 'utf8').split(p.from).length - 1;
    const good = restored === before && backHits === 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} 还原:sha ${restored === before ? '一致' : '🔴 不一致'}`
        + ` · 原文回位 ${backHits === 1 ? '是' : `🔴 否(${backHits} 处)`}`);
    if (!good) failures += 1;
}

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`);
process.exit(0);
