#!/usr/bin/env node
/**
 * 注毒自证 · #225 a1 写作中心的「槽」与「篇」。
 *
 * 🔴 开跑前后各断言一次 HEAD 与 dirty 清单(222 起的规矩,起因见那把运行器的抬头)。
 * 别与 build 并行(毒在工作树里)。
 */
import { execFileSync, execSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const abs = (rel) => join(ROOT, rel);
const sha = (p) => createHash('sha256').update(readFileSync(p)).digest('hex');
const say = (m) => console.log(m);
let failures = 0;

function treeState() {
    const head = execSync('git rev-parse HEAD', { cwd: REPO, encoding: 'utf8' }).trim();
    const dirty = execSync('git status --porcelain', { cwd: REPO, encoding: 'utf8' })
        .split('\n').map((s) => s.trim()).filter(Boolean).sort().join('|');
    return { head, dirty };
}
const BEFORE = treeState();
say(`开跑前 HEAD=${BEFORE.head.slice(0, 9)} · dirty ${BEFORE.dirty.split('|').filter(Boolean).length} 项`);

const GATE = 'scripts/verify-writing-slot-posts.mjs';
function run() {
    try {
        const out = execFileSync(process.execPath, [GATE], { cwd: ROOT, encoding: 'utf8' });
        return { rc: 0, red: [...out.matchAll(/^\s*FAIL\s+(\w+)/gm)].map((m) => m[1]) };
    } catch (e) {
        const out = String(e.stdout || '') + String(e.stderr || '');
        return {
            rc: e.status === undefined ? -1 : e.status,
            red: [...out.matchAll(/^\s*FAIL\s+(\w+)/gm)].map((m) => m[1]),
        };
    }
}

const MOD = 'src/pages/Writing/writingCounts.ts';
const HALL = 'src/pages/Writing/WritingHall.tsx';

const POISONS = [
    {
        /* 🔴 Review 09-15 P2c 照出的夹具洞:只吃槽、按倍数编一个数,单组夹具挡不住。 */
        id: 'T8', file: MOD,
        why: '按槽编一个数(同槽必然同答案)—— 单组夹具挡不住,同槽不同值那组才有牙',
        from: '    const p = kw ? int(kw.planned_posts_default) : null;',
        to: '    const p = kw ? (int(kw.planned_posts_default) === 0 ? 0 : slots(kw) * 3) : null;',
        expectRed: ['W2a', 'W2a2'],
    },
    {
        id: 'T9', file: MOD,
        why: '合计改成前端逐行相加,不用服务端给的那个数',
        from: '    if (t !== null) return Math.max(0, t);',
        to: '',
        expectRed: ['W2f', 'W2i'],
    },
    {
        /* 🔴 上半就漏过一次的那种错:改了标签、没改紧挨着的比较。 */
        id: 'T10', file: HALL,
        why: '「已生成」徽章的填色阈值又拿槽数比(需 10 篇生成 2 篇就填成够了)',
        from: 'kwTopics.length >= plannedPosts(kw)',
        to: 'kwTopics.length >= kw.required_articles',
        expectRed: ['W4f'],
    },
    {
        id: 'T11', file: HALL,
        why: '项目卡那个槽总数又标回「篇」',
        from: "{project.total_required_articles || '-'}槽",
        to: "{project.total_required_articles || '-'}篇",
        expectRed: ['W4h'],
    },
    {
        /* 🔴 这一发就是判据 W2c 当场抓到的那个真 bug —— 把它钉住,别再回来。 */
        id: 'T1', file: MOD,
        why: 'int() 不再先挡 null —— Number(null) 是 0,「服务端没给」会变成「给了 0」',
        from: "    if (v === null || v === undefined || v === '') return null;",
        to: '',
        expectRed: ['W2c'],
    },
    {
        id: 'T2', file: MOD,
        why: 'NULL 槽数当成 0(后端那条老语义是 NULL ⇒ 1)',
        from: '    if (raw === null || raw === undefined) return 1;',
        to: '    if (raw === null || raw === undefined) return 0;',
        expectRed: ['W1b'],
    },
    {
        id: 'T3', file: MOD,
        why: '显式 0 槽被当成 1 —— 凭空多派一篇的活',
        from: '    return n === null ? 1 : Math.max(0, n);',
        to: '    return n === null ? 1 : Math.max(1, n);',
        expectRed: ['W1d'],
    },
    {
        /* 🔴 前端自己算换算 = 与媒体组合那屏取整位置不同,两个数并排矛盾。 */
        id: 'T4', file: MOD,
        why: '前端自己按倍数算篇数,不读服务端给的值',
        from: '    const p = kw ? int(kw.planned_posts_default) : null;',
        to: '    const p = kw ? Math.round(slots(kw) * 50000 / 10000) : null;',
        expectRed: ['W2a', 'W2e'],
    },
    {
        id: 'T5', file: MOD,
        why: '两个数一样时也把「授权 N 槽」说一遍(同一个数换量词讲两遍)',
        from: "    return plannedPosts(kw) === s ? '' : `授权 ${s} 槽`;",
        to: '    return `授权 ${s} 槽`;',
        expectRed: ['W3b'],
    },
    {
        /* 🔴 锚带上整行:`需{plannedPosts(kw)}篇` 在文件里出现 **2 次**(两处都改了),
           只用那一小段会报「毒没下成」—— 和「锁没牙」在 rc 上同形。 */
        id: 'T6', file: HALL,
        why: '那两处又直接显示合同槽数(而它不是要做几篇)',
        from: '<Badge variant="secondary" className="text-xs">需{plannedPosts(kw)}篇</Badge>',
        to: '<Badge variant="secondary" className="text-xs">需{kw.required_articles}篇</Badge>',
        /* 🔴 期望红跟着锁走:W4a 已改成「纯函数被接上了(≥2)」,
           这一发只挪走一处、另一处加阈值那处还在,W4a 不该红。牙在 W4b。 */
        expectRed: ['W4b'],
    },
    {
        /* 🔴 成对的两个数被改成不同量词 —— 比两个都不改更糟。 */
        id: 'T7', file: HALL,
        why: '把计划数改成「条」,而紧挨着的「已生成 M 篇」没动',
        from: '                                                        需{plannedPosts(kw)}篇',
        to: '                                                        需{plannedPosts(kw)}条',
        /* 🔴 该红的是 W4e(反臂:没改成「条」),不是 W4a ——
           W4a 数的是 `plannedPosts(kw)` 出现几次,换个量词它不动。
           期望红挂错格 = 毒下去了却没人接,和"锁没牙"在报文上同形。 */
        expectRed: ['W4e'],
    },
];

const TOUCHED = [...new Set(POISONS.map((p) => p.file))];
const SHA0 = Object.fromEntries(TOUCHED.map((f) => [f, sha(abs(f))]));
const SRC0 = Object.fromEntries(TOUCHED.map((f) => [f, readFileSync(abs(f), 'utf8')]));
say('被碰文件开跑前 sha:');
for (const f of TOUCHED) say(`  ${f} ${SHA0[f].slice(0, 12)}`);

say('\n=== 0. 基线(失败集必须为空) ===');
const base = run();
const clean = base.rc === 0 && base.red.length === 0;
if (!clean) failures += 1;
say(`  ${clean ? 'OK  ' : 'FAIL'} rc=${base.rc} 失败集=${base.red.join(',') || '空'}`);
if (!clean) { say('\n🔴 基线不干净 —— 红基线会让每发毒都像命中,不往下跑。'); process.exit(1); }
const baseSet = new Set(base.red);

/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const n = SRC0[p.file].split(p.from).length - 1;
    if (n !== 1) {
        say(`  FAIL 毒没下成:${p.file} 的锚命中 ${n} 次(要恰好 1 次)—— 不是"锁没牙"`);
        failures += 1;
        continue;
    }
    try { assertRulerWorks(abs(p.file)); } catch (err) { say(`  ${err.message}`); process.exit(3); }
    writeFileSync(abs(p.file), SRC0[p.file].replace(p.from, p.to), 'utf8');
    if (!syntaxOk(abs(p.file))) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        failures += 1;
        writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
        continue;
    }
    say('  毒已落地(1 处改动)');
    const r = run();
    const missing = p.expectRed.filter((w) => !r.red.includes(w));
    const fresh = r.red.filter((x) => !baseSet.has(x));
    const good = r.rc !== 0 && missing.length === 0;
    if (!good) failures += 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} rc=${r.rc} 期望红=[${p.expectRed.join(',')}]`
        + `${missing.length ? ` 🔴 没红=[${missing.join(',')}]` : ' 全中'}`);
    say(`       新增报红 ${fresh.length} 条:${fresh.join(',') || '(无)'}`);
    writeFileSync(abs(p.file), SRC0[p.file], 'utf8');
    const back = sha(abs(p.file)) === SHA0[p.file];
    if (!back) failures += 1;
    say(`  ${back ? 'OK  ' : 'FAIL'} 还原:逐文件 sha 与下毒前一致`);
}

say('\n=== 收尾 ===');
for (const f of TOUCHED) {
    const now = sha(abs(f));
    const good = now === SHA0[f];
    if (!good) failures += 1;
    say(`  ${good ? 'OK  ' : 'FAIL'} ${f} ${now.slice(0, 12)}`);
}
const AFTER = treeState();
const headSame = AFTER.head === BEFORE.head;
const dirtySame = AFTER.dirty === BEFORE.dirty;
if (!headSame) failures += 1;
if (!dirtySame) failures += 1;
say(`  ${headSame ? 'OK  ' : 'FAIL'} HEAD 与开跑前一致 ${AFTER.head.slice(0, 9)}`);
say(`  ${dirtySame ? 'OK  ' : 'FAIL'} dirty 清单与开跑前一致`);

say('');
if (failures > 0) { say(`FAIL 注毒自证 ${failures} 项不过`); process.exit(1); }
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,树状态逐字回位`);
process.exit(0);
