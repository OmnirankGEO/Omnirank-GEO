#!/usr/bin/env node
/**
 * 注毒自证 · #180-B 撤报价页客户下拉 —— 证明 B1/B2/B3 **真的有牙**。
 *
 * Review 09-13 指定的两发:
 *   · 去掉沙盒自动选中 ⇒ B1 红(沙盒用户在报价页选不了客户 —— 这正是撤下拉的前提)
 *   · 去掉第二步锚点 ⇒ B3 红(`step2-online-form` 阶段回到零消费者,第二步断在这里)
 * 另加两发守住"撤干净"与"没绕开唯一入口"。
 *
 * 🔴 跑它的时候别同时跑 build 或别的渲染闸(毒在工作树里);中途别 kill(还原不执行)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    scope: 'scripts/verify-publish-client-scope.mjs',
    render: 'scripts/test-publish-client-scope-render.mjs',
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
            cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 15 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

const QUOTE = 'src/pages/Quote/OnlineQuoteFlow.tsx';

const ICP = 'src/sandbox/sandboxInterceptor.ts';

const POISONS = [
    {
        id: 'B1P', file: 'src/sandbox/mockData.ts',
        why: 'B1 · 让沙盒的客户列表回**两个**客户(= 单客户自动选中不成立 ⇒'
            + '沙盒态当前客户悬空,而报价页已经没有下拉可选)',
        /*
         * 🔴 靶子换了**两次**,两次都是"毒够不着",两次都不是"锁没牙":
         *   ① 第一版毒我自己在 ClientContext 里加的那段 `switchClient(111)` ——
         *      删掉 B1 照样绿,因为那段**根本不承重**(冗余代码,已删);
         *   ② 第二版毒 sandboxInterceptor 里那个内联 `clients: [{ id: 111 …}]` ——
         *      B1 还是绿,因为拦截器里 `client-context/list` **有两个 handler**,
         *      先匹配的那个走的是 `getSandboxClientContextList()`,
         *      我毒的是后面那个**永远匹配不到**的。
         *   ⇒ 现在毒真正被调用的那一个。
         *      教训:注毒前先确认"这条路真的会执行到我毒的那一行"。
         */
        from: '    return { success: true, clients: [SANDBOX_BRAND_BASE] };',
        to: "    return { success: true, clients: [{ id: 999, name: '多余客户' }, SANDBOX_BRAND_BASE] };",
        expectRed: { render: ['B1'] },
    },
    {
        id: 'B3P', file: QUOTE,
        why: 'B3 · 让第二步那颗 coach mark 在这个阶段**不显示**'
            + '(= `step2-online-form` 回到零消费者,侧栏把人送到 /pricing 之后没有下文)',
        /*
         * 🔴 第一版把阶段条件换成 `|| false` —— 那是**放宽**不是移除:
         *    引导反而无条件显示,B3 当然绿。毒要让它**真的不出现**。
         */
        from: `                      || tutorialStage !== 'step2-online-form'`,
        to: `                      || true`,
        expectRed: { scope: ['E7b'], render: ['B3'] },
    },
    {
        id: 'B2P', file: QUOTE,
        why: 'B2 · 把客户下拉加回来(= 页面上又出现第二个"当前客户"入口)',
        from: '            <div data-testid="quote-client-from-sidebar" className="text-xs text-muted-foreground">',
        to: '            <input placeholder="-- 选择已有客户或手动填写 / 输入名称搜索 --" />'
            + String.fromCharCode(10)
            + '            <div data-testid="quote-client-from-sidebar" className="text-xs text-muted-foreground">',
        expectRed: { render: ['B2b'] },
    },
];


say('=== 0. 基线(两把闸的失败集都必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    const r = run(gate);
    baseline[name] = r.redSet;
    const good = r.rc === 0 && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) { say('  🔴 基线不干净,注毒读数无意义。先修基线。'); process.exit(1); }
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
    const bak = `${target}.b180bak-${p.id}`;
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
