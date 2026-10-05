#!/usr/bin/env node
/**
 * 注毒自证 · #180 发布中心客户范围 —— 证明两套判据**真的有牙**。
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
 * 跑法:cd frontend && npm run build && node scripts/mutation_runner_publish_client_scope.mjs
 *      加 --no-browser 跳过浏览器臂(快;但那几条的牙就没证到,交付物要写明)
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const NO_BROWSER = process.argv.includes('--no-browser');

const GATES = {
    scope: 'scripts/verify-publish-client-scope.mjs',
    browser: 'scripts/test-publish-client-scope-render.mjs',
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
const MOD = 'src/pages/Publishing/publishClientScope.ts';

const POISONS = [
    {
        id: 'T1', file: MOD,
        why: '把 list[0] 兜底放回去(= 左上角选了谁不重要,页面永远在第一个项目上工作 —— "显示对、记错人")',
        from: `    const hit = (projects || []).find((p) => num(p?.brand_id) === bid);
    return hit ? hit.id : null;`,
        to: `    const hit = (projects || []).find((p) => num(p?.brand_id) === bid);
    return hit ? hit.id : ((projects || [])[0] ? (projects || [])[0].id : null);`,
        expectRed: { scope: ['A3'], browser: ['S5a'] },
    },
    {
        id: 'T2', file: MOD,
        why: '深链不再反向同步(= 页面按深链走、左上角停在别人身上,两处不一致)',
        from: `    const target = num(input?.deepLinkBrandId);
    if (!target) return false;
    return num(input?.currentBrandId) !== target;`,
        to: `    return false;`,
        expectRed: { scope: ['B6'], browser: ['S2a'] },
    },
    {
        id: 'T3', file: PAGE,
        why: '深链同步不等品牌列表(= switchClient 立不住,深链**静默无效** —— 这正是我实测踩到的那一格)',
        from: `    if (clients.length === 0) return;          // 列表没到,switchClient 立不住`,
        to: `    if (false) return;`,
        expectRed: { scope: ['D7c'] },
    },
    {
        id: 'T4', file: MOD,
        why: '「全部客户」也当成 ready(= 替用户猜一个客户并真的去取他的文章)',
        /*
         * 🔴 [2026-09-20 重锚] 旧锚把**整个 if 块连同返回对象字面量**抄了一遍,
         *    而那个对象后来多了一个 `quoteIds: []` ⇒ 锚命中 0 次,这一发**一直没下成**
         *    (runner 如实报了「不是锁没牙」,但没人回来补)。
         *    ⇒ 锚只钉**行为开关**那一行:块里怎么改都不影响这一发还能不能下成。
         *      产品代码没问题,是锁钉在了会变的部位上。
         */
        from: `    if (input.isAllClientsMode) {`,
        to: `    if (false) {`,
        /**
         * 🔴 只有 scope 臂:真 provider 进「全部客户」时会把 `currentBrandId` 置 null
         *    (ClientContext:592 `switchClient(null, true)`),所以这发毒之后仍然落到
         *    `no_client` 那一格 —— 浏览器层两个分支**观察上等价**,毒够不着。
         *    这属于「毒够不着目标」,不是「锁没牙」。两格的区别(措辞/迷你选择)
         *    由 C1 与 S3b/S3c 守着。
         */
        expectRed: { scope: ['C1'] },
    },
    {
        id: 'T5', file: PAGE,
        why: '面板回报权威归属时不再切左上角(= 面板显示 A、左上角仍是 B)',
        from: `                    if (shouldSwitchClient({ deepLinkBrandId: bid, currentBrandId: currentBrandId ?? null })) {
                      switchClient(bid);
                    }`,
        to: `                    void bid;`,
        expectRed: { scope: ['D8'] },
    },
    {
        id: 'T6', file: MOD,
        why: '「该客户没有可发布文章」那格不说话(= 页面变空而不给任何解释)',
        from: `            message: '这个客户还没有可发布的文章 —— 先去「AI 写文章」给他写一篇',`,
        to: `            message: '',`,
        expectRed: { scope: ['C1', 'C3'], browser: ['S5b'] },
    },
    {
        id: 'T7', file: PAGE,
        why: '把页面级下拉的锚放回去(= 第二个客户入口又回来了)',
        from: `            <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="publish-client-scope">`,
        to: `            <div className="flex flex-wrap items-center gap-2 text-sm" data-testid="publish-project-combobox">`,
        expectRed: { scope: ['D6'], browser: ['S4a'] },
    },
    {
        id: 'T8', file: 'src/pages/Quote/OnlineQuoteFlow.tsx',
        why: '报价页改回自己存一份客户(= 与左上角又割裂,两处各记一个人)',
        from: `  const selectedBrandId = currentBrandId ?? null;`,
        to: `  const [selectedBrandId] = useState<number | null>(null);`,
        expectRed: { scope: ['E2'] },
    },
    {
        id: 'T9', file: 'src/pages/Quote/OnlineQuoteFlow.tsx',
        why: '报价页的下拉选项不再改左上角(= 页面选了 A,左上角还是 B)',
        /*
         * 🔴 [2026-09-20 重锚] 旧锚是 `selectBrand(b.id);` 带一长串缩进。
         *    那处调用后来被收进 `trySelect(bid)` 帮手、变量也从 `b.id` 改成 `bid`
         *    ⇒ 锚命中 0 次,这一发**一直没下成**。产品行为没变坏,是锚跟着缩进和
         *    变量名走了。⇒ 钉「这一行调用还在不在」,不钉它长什么样的上下文。
         */
        from: `        selectBrand(bid);`,
        to: `        void bid;`,
        expectRed: { scope: ['E5'] },
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
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
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
