#!/usr/bin/env node
/**
 * 注毒自证 · #191 真实态等价 A/B —— 证明那一把 A/B **真的有牙**。
 *
 * A/B 比的是 `git show HEAD:` 的旧版 vs **工作树**这一份,所以毒下在工作树上,
 * 正好就是「改动引入了真实态回归」的形状。三发:
 *   Y1 把「取消发布并退款」少抄一个 `saving ||`(= 保存中也能点退款)⇒ A4c 红
 *      (A4 是专门把保存接口拖住、停在 `saving === true` 的那一段 —— 没有它这发毒够不着);
 *   Y2 把列表态弹窗的 className 改成教程态那一份(= 真实态把关闭按钮藏了)⇒ DOM 臂 A1b 红;
 *   Y3 把「一键取消全部」的空列表保护删掉(= 0 项时也能点"全部退款")⇒ A2c 红。
 *
 * 🔴 Y1/Y3 都是**动钱的按钮**。这一把 A/B 存在的理由就是它们:
 *    tsc 不会红、类门不会红、屏幕上却多一次可点的退款。
 *
 * 🔴 别与 build / 其他渲染闸并行(毒在工作树里);中途别 kill(还原不执行)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = { ab: 'scripts/test-awaiting-confirm-real-mode-ab.mjs' };
const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

/*
 * 🔴 基线必须**钉死在改动前那一笔**,而且要在**下毒之前**算出来。
 *    不钉的话:毒一落地工作树就"有改动",A/B 的自动探测会把基线选成 HEAD ——
 *    而 HEAD 已经是清干净的 #191,于是它的前提检查(旧版里必须还有 `tutorialLock`)
 *    直接报「判据不可用」,三发毒全部读不出来。第一版就是这么错的:
 *    **仪器把基线选到了错的参照物上**(verified-correctly-against-the-wrong-referent)。
 */
const REL_IN_REPO = 'frontend/src/components/publishing/AwaitingConfirmDialog.tsx';
const git = (args) => execFileSync('git', args,
    { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }).trim();
let BASE = 'HEAD';
/* 🔴 pathspec 用 `:/` 锚到仓库根:本脚本 cwd 是 frontend/,
   传 'frontend/src/...' 会被当成 frontend/frontend/src/... ⇒ git log 空、git diff 恒"干净",
   基线悄悄退回 'HEAD^' —— 一次刚好对、下一笔就选到错的参照物上。 */
const PATHSPEC = `:/${REL_IN_REPO}`;
let dirty = false;
try { execFileSync('git', ['diff', '--quiet', '--', PATHSPEC], { cwd: ROOT }); }
catch { dirty = true; }
if (dirty) {
    /* 有未提交改动 ⇒ #191 还没提交 ⇒ 基线 = HEAD */
    BASE = 'HEAD';
} else {
    const last = git(['log', '-n', '1', '--format=%H', '--', PATHSPEC]);
    if (!last) { say('FAIL 查不到改过该文件的提交 ⇒ 基线选不出来'); process.exit(1); }
    BASE = `${last}^`;
}
say(`基线钉死在 ${BASE}(= 改动前那一份;下毒前算好,毒不会挪动它)`);

function run(gateRel) {
    let out = '';
    let rc = 0;
    try {
        out = execFileSync(process.execPath, [abs(gateRel), `--base=${BASE}`], {
            cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024, timeout: 10 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set([...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]));
    return { rc, redSet, out };
}

const DLG = 'src/components/publishing/AwaitingConfirmDialog.tsx';
/* A/B 会在 src 下落一个临时旧版文件;上一次跑没清干净的话本轮读数不可信。 */
const LEFTOVER = abs('src/components/publishing/__ab_old_AwaitingConfirmDialog.tsx');

const POISONS = [
    {
        id: 'Y1', file: DLG,
        why: '「取消发布并退款」少抄一个 `saving ||`(= 正在保存时也能点退款,'
            + '双击就可能既保存又退款)—— tsc 与类门都不会红',
        from: `                    <Button variant="ghost" size="sm" disabled={saving || resolving}
                        onClick={() => detail && handleCancelOnly(detail.item.item_id)}>`,
        to: `                    <Button variant="ghost" size="sm" disabled={resolving}
                        onClick={() => detail && handleCancelOnly(detail.item.item_id)}>`,
        /* 🔴 第一版写的是 A3c(编辑态)—— **毒仍绿**:那一态 `saving` 恒为 false,
           两个表达式的取值一样,判据够不着。补了 A4(把保存接口拖住、停在 saving 中)
           才有得比。这就是"毒仍绿四解"里的**够不着**,不是锁没牙。 */
        expectRed: { ab: ['A4c'] },
    },
    {
        id: 'Y2', file: DLG,
        why: '把列表态弹窗的 className 改成教程锁那一份(= 真实态下把右上角关闭按钮藏了)'
            + ' —— 这是"删壳时顺手把样式也带走/带错"的形状',
        from: `                <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col">`,
        to: `                <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col [&>button]:hidden">`,
        expectRed: { ab: ['A1b'] },
    },
    {
        id: 'Y3', file: DLG,
        why: '「一键取消全部并退款」把空列表保护删掉(= 0 项时也能点"全部退款")',
        from: 'disabled={resolving || items.length === 0}',
        to: 'disabled={resolving}',
        expectRed: { ab: ['A2c'] },
    },
];

if (existsSync(LEFTOVER)) {
    say(`FAIL 上一次 A/B 没清干净:${LEFTOVER} 还在 —— 先删掉再跑,否则读数不可信`);
    process.exit(1);
}

say('=== 0. 基线(失败集必须为空) ===');
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
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红(恒 true 的尺子与
 *    「每一发毒都下成了」读数完全同形)。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const target = abs(p.file);
    const bak = `${target}.y191bak-${p.id}`;
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

if (existsSync(LEFTOVER)) {
    say(`\nFAIL A/B 跑完把 ${LEFTOVER} 留在了树里 —— 自己清掉再交`);
    failures += 1;
}

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`);
process.exit(0);
