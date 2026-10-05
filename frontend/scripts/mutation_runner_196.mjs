#!/usr/bin/env node
/**
 * 注毒自证 · #196 素材准备轮询 —— 证明「纯函数臂 + 浏览器臂」两层真的有牙。
 *
 * 六发:
 *   W1 去掉"排下一次读"(= 回到现场那个缺陷:只读一次)      ⇒ 浏览器臂 P9b 红
 *   W2 每次 tick 重算钥匙(= 每 3 秒 claim 一条新 artifact)   ⇒ P9c 红
 *   W3 失败态换成一句通用文案                                 ⇒ P9f 红
 *   W4 拿掉 300s 那道闸(= 在用户浏览器里永远空转)            ⇒ 纯函数臂 Q4c 红
 *   W5 收起面板不清定时器                                     ⇒ P9i 红
 *   W6 `unknown` 也说可重试(= 可能让远端收两次)              ⇒ Q3b 红
 *
 * 🔴 两层都要跑:纯函数臂只证"计划算得对",浏览器臂才证"组件真的按计划办"。
 *    只红一层的毒说明另一层够不着,那一层就该补格子。
 *
 * 🔴 别与 build / 其他渲染闸并行(毒在工作树里);中途别 kill(还原不执行)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    q: 'scripts/verify-image-note-prepare-polling.mjs',
    r: 'scripts/test-image-note-publish-render.mjs',
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
            cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 12 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

const PLAN = 'src/pages/Writing/artifactPollPlan.ts';
const INL = 'src/pages/Writing/ImageNotePublishInline.tsx';

const POISONS = [
    {
        id: 'W1', file: INL,
        why: '去掉"排下一次读"—— 正好回到现场那个缺陷:prepare 只读一次,'
            + '状态永远停在 preparing,发布键永远灰着',
        from: `                        if (plan.keepPolling) {
                            clear();
                            artPollRef.current = window.setTimeout(tick, plan.delayMs);
                        }`,
        to: `                        void plan;`,
        expectRed: { r: ['P9b'] },
    },
    {
        id: 'W2', file: INL,
        why: '每次 tick 重算幂等钥匙(= 每 3 秒 claim 一条新 artifact,幂等根形同虚设)',
        from: `        const tick = () => {
            void preparePublishMedia(postId, key)`,
        to: `        const tick = () => {
            void preparePublishMedia(postId,
                stableRequestId('prep', \`\${postId}-\${revisionId}-\${Date.now()}\`))`,
        expectRed: { r: ['P9c'] },
    },
    {
        id: 'W3', file: PLAN,
        why: '失败态换成一句通用文案(= 用户看不出这一篇到底怎么了)',
        from: `            line: '这篇素材准备失败', stoppedReason: '',`,
        to: `            line: '操作未完成', stoppedReason: '',`,
        /* 🔴 a3 之后失败行优先显示**服务端原话**,所以毒兜底句不再影响 P9f;
           它现在该红在 Q3c(兜底句必须点名「这篇/素材」)。 */
        expectRed: { q: ['Q3c'] },
    },
    {
        id: 'W4', file: PLAN,
        why: '拿掉 300s 那道闸(= 面板开着就在用户浏览器里永远空转)',
        from: `    if (ms >= GIVE_UP_MS) {`,
        to: `    if (false && ms >= GIVE_UP_MS) {`,
        expectRed: { q: ['Q4c'] },
    },
    {
        id: 'W5', file: INL,
        why: '收起面板不清定时器(= 面板收起后仍在后台一直打接口)',
        from: `        return () => { cancelled = true; clear(); };`,
        to: `        return () => { cancelled = true; };`,
        expectRed: { r: ['P9i'], q: ['Q5b'] },
    },
    {
        id: 'W6', file: PLAN,
        why: '`unknown` 也说可重试 —— 远端可能已经收了,重传就是可能发两次'
            + '(后端把它写成硬约束)',
        from: `            keepPolling: false, delayMs: 0, terminal: true, retryable: false,
            line: '结果未知,人工核对中,不会重复扣算力', stoppedReason: '',`,
        to: `            keepPolling: false, delayMs: 0, terminal: true, retryable: true,
            line: '结果未知,人工核对中,不会重复扣算力', stoppedReason: '',`,
        expectRed: { q: ['Q3b'] },
    },
    {
        id: 'W9', file: PLAN,
        why: '[#196 a3] 只读前端自己合成的 `user_message`,把服务端原话绕过去'
            + '(= 契约错位重演:字段到了没人读,两边各自全绿)',
        from: `    const server = typeof artifact?.failure_reason === 'string' ? artifact.failure_reason.trim() : '';`,
        to: `    const server = '';`,
        expectRed: { q: ['Q3b1'], r: ['P9f'] },
    },
    {
        id: 'W7', file: PLAN,
        why: '`failed` 之后也不换钥匙(= 下一次展开只把同一条失败记录再读一遍,'
            + '失败从此没有出口 —— Review 2026-09-13 §4 裁的就是这一格)',
        from: `    return String(state || '').trim() === 'failed' ? now + 1 : now;`,
        to: `    void state;
    return now;`,
        expectRed: { q: ['Q6f1'], r: ['P9j2'] },
    },
    {
        id: 'W8', file: PLAN,
        why: '`unknown` 也换钥匙(= 再 claim 一条,远端可能收两次 —— 后端写成硬约束的那件事)',
        from: `    return String(state || '').trim() === 'failed' ? now + 1 : now;`,
        to: `    return ['failed', 'unknown'].includes(String(state || '').trim()) ? now + 1 : now;`,
        expectRed: { q: ['Q6f2'], r: ['P9k'] },
    },
];

/*
 * 🔴 **开跑前给所有会被碰的文件拍一张 sha**,跑完逐个核回来。
 *    为什么需要这一层:逐发毒的还原核对只比它自己那一发的备份,
 *    而「上一轮跑挂在半路」或「还原逻辑本身有 bug」这两种它看不见 ——
 *    实测就发生过:旧版还原按顺序回写同一文件的两个备份,把中间态写回了树。
 *    残毒(一个没人调用的 `url?: unknown` 参数 + 一个永不执行的分支)
 *    **行为上是哑的**,所以所有闸照样全绿;而这些文件还是 untracked,
 *    `git status` 也看不出内容变化。
 *    🔴 全局 sha 是唯一能把「树被我自己弄脏了」变成红的东西。
 */
const TOUCHED_FILES = [...new Set(POISONS.flatMap(
    (p) => [p.file, ...(p.also || []).map((a) => a.file || p.file)]))];
const SHA_AT_START = Object.fromEntries(TOUCHED_FILES.map((f) => [f, sha(abs(f))]));
say('被碰文件开跑前 sha:');
for (const f of TOUCHED_FILES) say(`  ${f} ${SHA_AT_START[f].slice(0, 12)}`);

say('=== 0. 基线(三把闸的失败集都必须为空) ===');
const baseline = {};
for (const [name, gate] of Object.entries(GATES)) {
    const r = run(gate);
    baseline[name] = r.redSet;
    /* rc=3 是"有未评估",不是失败;判基线干净只看失败集。 */
    const good = (r.rc === 0 || r.rc === 3) && r.redSet.size === 0;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!good) {
        say('  🔴 基线不干净,注毒读数无意义(红基线会让每发毒都像命中)。');
        process.exit(1);
    }
}

/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    /*
     * 一发毒可能动**同一个文件的两处**(主锚 + also)。
     *
     * 🔴 第一版是「逐条 edit 各备份一次、再按顺序逐个还原」—— **那是错的**:
     *    同一个文件被备份两次时,bak#2 存的是**第一处已经下毒后**的中间态,
     *    顺序还原会把中间态又写回去。Z3 实测:`genFailure: str(o.failure_reason)`
     *    留在了工作树里,而运行器照样打印「还原:sha 一致、原文回位」——
     *    因为它比的是那个中间态的 sha。**运行器自己撒了谎。**
     *  ⇒ 改成:**按文件**各备份一次(下毒前的原始态),还原也按文件一次;
     *    并在最后逐文件核 sha **与原始态**相等、逐条核原文回位。
     */
    const edits = [{ file: p.file, from: p.from, to: p.to },
        ...(p.also || []).map((a) => ({ file: a.file || p.file, from: a.from, to: a.to }))];
    const touched = [...new Set(edits.map((e) => e.file))];
    const backups = touched.map((rel) => {
        const target = abs(rel);
        const bak = `${target}.z195bak-${p.id}`;
        copyFileSync(target, bak);
        return { rel, target, bak, before: sha(target) };
    });
    const restoreAll = () => {
        for (const b of backups) { copyFileSync(b.bak, b.target); unlinkSync(b.bak); }
    };
    /*
     * 🔴 对照臂**一发一次**,不是一条 edit 一次:放进循环的话,第二条 edit 之前
     *    文件已被第一条改过,对照臂看到**中间态**就会喊「没下毒的文件都判不过 ⇒
     *    尺子坏了」,而尺子好好的。退出前先还原,否则半截毒留在树上,
     *    下一次跑基线当场不干净 —— 那个读数和「产品真坏了」完全同形。
     */
    for (const rel of touched) {
        try {
            assertRulerWorks(abs(rel));
        } catch (err) {
            restoreAll();
            say(`  ${err.message}`);
            process.exit(3);
        }
    }
    let landed = true;
    for (const e of edits) {
        const target = abs(e.file);
        const src = readFileSync(target, 'utf8');
        const hits = src.split(e.from).length - 1;
        if (hits !== 1) {
            say(`  FAIL 毒没下成:${e.file} 的锚命中 ${hits} 次(要恰好 1 次)—— 不是"锁没牙"`);
            landed = false;
            break;
        }
        writeFileSync(target, src.replace(e.from, e.to), 'utf8');
        if (sha(target) === createHash('sha256').update(src).digest('hex')) {
            say(`  FAIL 毒没下成:${e.file} 内容没变`);
            landed = false;
            break;
        }
    }
    if (!landed) { restoreAll(); failures += 1; continue; }
    /*
     * 🔴 语法闸放在**所有 edit 都落完之后**:一发毒可以由多条 edit 组成,
     *    中间态本来就是不平衡的,逐条验会把好毒误报成「写成语法错」。
     */
    const brokeSyntax = touched.filter((rel) => !syntaxOk(abs(rel)));
    if (brokeSyntax.length > 0) {
        say(`  FAIL ${brokeSyntax.join(', ')} ${NOT_LANDED_SYNTAX}`);
        restoreAll(); failures += 1; continue;
    }
    say(`  毒已落地(${edits.length} 处改动 · ${touched.length} 个文件)`);

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

    restoreAll();
    /* 🔴 还原核对**按文件**比下毒前的原始 sha,并逐条确认原文真的回位了。 */
    let restored = true;
    for (const b of backups) {
        if (sha(b.target) !== b.before) {
            restored = false;
            say(`  FAIL 还原:${b.rel} 的 sha 与下毒前不一致`);
        }
    }
    for (const e of edits) {
        const hits = readFileSync(abs(e.file), 'utf8').split(e.from).length - 1;
        if (hits !== 1) {
            restored = false;
            say(`  FAIL 还原:${e.file} 的原文没回位(${hits} 处)`);
        }
    }
    say(`  ${restored ? 'OK  ' : 'FAIL'} 还原:`
        + `${restored ? '逐文件 sha 与下毒前一致、原文逐条回位' : '🔴 有文件没回位'}`);
    if (!restored) failures += 1;
}

say('');
say('=== 收尾:被碰文件逐个核回开跑前的 sha ===');
for (const f of TOUCHED_FILES) {
    const now = sha(abs(f));
    const good = now === SHA_AT_START[f];
    say(`  ${good ? 'OK  ' : 'FAIL'} ${f} ${now.slice(0, 12)}`
        + `${good ? '' : ' 🔴 与开跑前不一致 —— 树里留了东西'}`);
    if (!good) failures += 1;
}

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`);
process.exit(0);
