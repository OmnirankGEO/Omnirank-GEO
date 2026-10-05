#!/usr/bin/env node
/**
 * 注毒自证 · #190 教程 coach mark 类门 —— 证明 G1 **真的有牙**。
 *
 * WO §1 G3 指定三发:
 *   ① 临时插一个没人消费的阶段 ⇒ G1 红
 *   ② 临时把一个锚点 testid 改名 ⇒ G1 红(锚点指着空气)
 *   ③ 把某阶段的引导 disabled 恒真 ⇒ G2 红(浏览器臂,见 test-tutorial-coachmark-render)
 * 另加四发守住**分母本身**:枚举器被改窄 / 例外表被塞条目 /
 * 两张阶段表(`TutorialStage` 与 `VALID_STAGES`)向任一侧脱钩(#191 新增 G3f·G3g)。
 *
 * 🔴 G3f/G3g 不是凑数:写 #191 的退役说明时我在注释里带引号提了三个已退役的阶段名,
 *    旧枚举写法当场把它们算回分母(|ALL| 报 46 而真值 43)。那次是这条互校先红了
 *    才发现的 —— 它值得被毒一次证明有牙。
 *
 * 🔴 跑它时别同时跑 build 或别的渲染闸;中途别 kill(还原不执行,毒会留在树里)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    g1: 'scripts/verify-tutorial-coachmark-anchors.mjs',
    g2: 'scripts/test-tutorial-coachmark-render.mjs',
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

const STAGE = 'src/sandbox/tutorialStage.ts';
const HALL = 'src/pages/Writing/WritingHall.tsx';
const ENUM = 'scripts/enumerate-tutorial-stages.mjs';
const GATE = 'scripts/verify-tutorial-coachmark-anchors.mjs';

const POISONS = [
    {
        id: 'G3a', file: 'src/pages/Publishing/PublishCenter.tsx',
        why: 'G3① 把一个阶段推到**没人消费**的地方(= 教程走到那儿就没有下文,'
            + '#180-B 的 step2-online-form 原样重演)',
        /*
         * 🔴 用一个**真实存在但当前不可达**的阶段名当靶子:
         *    `step3-pick-real`(源码注释标了「废弃 · 兼容老 localStorage」,
         *    枚举里它产生 0 消费 0)。把它推出来就制造了一个 S−C。
         *    这样毒**不需要改类型联合**,靶子小、还原干净。
         */
        from: "                  setTutorialStage('step4-sidebar');",
        to: "                  setTutorialStage('step3-pick-real');",
        expectRed: { g1: ['G1a'] },
    },
    {
        id: 'G3b', file: HALL,
        why: 'G3② 把锚点 testid 改名(= spotlight 指着空气,而代码不报错)',
        /*
         * 🔴 锚要**恰好命中一次**。第一版只写属性本身 —— 它出现两次
         *    (`targetSelector` 里一次、元素上一次),runner 如实报了「毒没下成:命中 2 次」。
         *    那是对的:命中数不对 ≠ 锁没牙,两件事必须分得开。
         *    这里带上元素那一行的缩进前缀,只改**元素**上的那个 ——
         *    selector 保持不变,于是它就指着空气了,正是要模拟的缺陷。
         */
        from: '                                        data-sandbox-coach-anchor="writing-generate-titles"',
        to: '                                        data-sandbox-coach-anchor="writing-generate-titles-RENAMED"',
        expectRed: { g1: ['G1e'] },
    },
    {
        id: 'G3d', file: ENUM,
        why: '守分母 · 把枚举器改窄(= 少认一种消费写法 ⇒ 一堆阶段被误报成没人消费。'
            + '这正是我上一轮报出 7 个假阳性的那个形状)',
        from: "    { kind: 'consume', why: 'tutorial.stage === / !==(另一个变量名 —— 上轮漏的就是它)', re: /tutorial(?:\\?)?\\.stage\\s*(?:===|!==)\\s*'([a-z0-9-]+)'/g },",
        to: '',
        expectRed: { g1: ['G1a'] },
    },
    {
        id: 'G3e', file: GATE,
        why: '守例外表 · 往例外表里塞第二条(= 悄悄把门关掉)',
        from: "const KNOWN_DEAD_GUIDANCE = {",
        to: "const KNOWN_DEAD_GUIDANCE = {\n    'step3-pick-real': { reason: '随手放行', owner: '没人', since: '不知道' },",
        expectRed: { g1: ['G1c'] },
    },
    {
        id: 'G3f', file: STAGE,
        why: '守分母 · 两张阶段表脱钩(类型联合里有、VALID_STAGES 里没有)。'
            + '后果是真的:这个阶段能被写进状态机,但**刷新一次就被校验打回 modal** ——'
            + '用户视角是"教程自己重置了",而代码两边都不报错。'
            + '#191 退役三个阶段时要同时改两处,所以这条互校长在分母里。',
        from: "    'step3-pub-pick-article', 'step3-pub-pick-media', 'step3-pub-add-cart', 'step3-pub-batch-send',",
        to: "    'step3-pub-pick-article', 'step3-pub-pick-media', 'step3-pub-add-cart',",
        expectRed: { g1: ['G0'] },
    },
    {
        id: 'G3g', file: STAGE,
        why: '守分母 · 从另一侧脱钩:把 #191 退役掉的阶段名**只加回 VALID_STAGES**。'
            + '这一格钉的是"半个退役" —— 旧 localStorage 值又能通过校验了,'
            + '而全站没有任何消费者 ⇒ 教程停在一个没有下文的阶段上(G1a 那类后果)。',
        from: "    'step3-pub-pick-article', 'step3-pub-pick-media', 'step3-pub-add-cart', 'step3-pub-batch-send',",
        to: "    'step3-pub-pick-article', 'step3-pub-pick-media', 'step3-pub-add-cart', 'step3-pub-batch-send',"
            + "\n    'step3-pub-await-process',",
        expectRed: { g1: ['G0'] },
    },
    {
        id: 'G3c', file: 'src/pages/Quote/OnlineQuoteFlow.tsx',
        why: 'G3③ 把第二步的引导 disabled 恒真(= 引导还在源码里,屏幕上永远不出现 ——'
            + '#180-B 那发 `|| true` 就是这一格)',
        /*
         * 🔴 这一发专门验 G2 的**冻结表**有没有用:
         *    没有冻结表的话,引导消失只会被报成"未评估"(我的桩没喂够数据),
         *    门就抓不住回归。有了它,step2-* 三个阶段必须变 FAIL。
         */
        from: '                  disabled={!isSandboxActive()}',
        to: '                  disabled={true}',
        expectRed: { g2: ['G2[step2-sidebar]'] },
    },
];

say('=== 0. 基线(失败集必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    const r = run(gate);
    baseline[name] = r.redSet;
    /* 🔴 rc=3 是"有未评估",不是失败 —— G2 基线本来就带未评估。
       判基线干净只看**失败集是否为空**。 */
    const good = (r.rc === 0 || r.rc === 3) && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) { say('  🔴 基线不干净,注毒读数无意义。'); process.exit(1); }
}

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const target = abs(p.file);
    const bak = `${target}.a190bak-${p.id}`;
    const before = sha(target);
    copyFileSync(target, bak);
    const src = readFileSync(target, 'utf8');
    const hits = src.split(p.from).length - 1;
    if (hits !== 1) {
        say(`  FAIL 毒没下成:锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`);
        unlinkSync(bak); failures += 1; continue;
    }
    try { assertRulerWorks(target); } catch (e) { say(`  ${e.message}`); process.exit(3); }
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
        const good = missed.length === 0 && r.rc !== 0 && r.rc !== 3;
        if (!good) caught = false;
        say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}]`
            + `${missed.length ? ` 🔴 没红=[${missed.join(',')}]` : ' 全中'}`);
        say(`       新增报红 ${newRed.length} 条:${newRed.slice(0, 6).join(',') || '(无)'}`);
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
