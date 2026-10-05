#!/usr/bin/env node
/**
 * 注毒自证 · #210 发布中心按当前客户的**全部**有效报价取文。
 *
 * @@R@@ 别与 build / 其他渲染闸并行(毒在工作树里);中途别 kill(还原不执行)。
 *
 * 🔴 每发都标了打的是哪一轴:结构锁用结构毒,行为锁用行为毒。
 *    最关键的一发是 R1 —— 把取文退回只取选中那一份,那正是真客户那次的原样。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    p: 'scripts/verify-publish-multi-quote.mjs',
    s: 'scripts/test-publish-center-scope.mjs',
    r: 'scripts/test-publish-multi-quote-render.mjs',
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

const PC = 'src/pages/Publishing/PublishCenter.tsx';
const SCOPE = 'src/pages/Publishing/publishClientScope.ts';
const LOGIC = 'src/pages/Publishing/publishCenterScopeLogic.ts';

const POISONS = [
    {
        /* 轴:结构 + 行为。这一发就是真客户那次的原样。 */
        id: 'R1', file: PC,
        why: '取文退回只取选中那一份报价 —— 另一份下的文章一篇都看不见(真客户原样)',
        from: '    const quoteIds: number[] = scopeQuoteIds;',
        to: '    const quoteIds: number[] = [selectedProject];',
        expectRed: { p: ['P2'], r: ['P5'] },
    },
    {
        /* 轴:纯函数 + 行为。病根本身:find 只回第一份。 */
        id: 'R2', file: SCOPE,
        why: 'quoteIdsForBrand 退回 find —— 一个客户只认一份报价',
        from: `    return (projects || [])
        .filter((p) => num(p?.brand_id) === bid)
        .map((p) => p?.id)
        .filter((id): id is number => num(id) !== null);`,
        to: `    const one = (projects || []).find((p) => num(p?.brand_id) === bid);
    return one ? [one.id] : [];`,
        expectRed: { p: ['P1'], r: ['P5'] },
    },
    {
        /* 轴:纯函数。默认就收窄 = 等于没修。 */
        id: 'R3', file: LOGIC,
        why: '报价筛选的默认值变成第一份 —— 默认就收窄,等于没修',
        from: '  const afterQuote = qf === null ? articles : articles.filter(a => a.quoteId === qf);',
        to: `  const firstQ = articles.length ? articles[0].quoteId : null;
  const eff = qf === null ? (firstQ ?? null) : qf;
  const afterQuote = eff === null ? articles : articles.filter(a => a.quoteId === eff);`,
        expectRed: { p: ['P3'] },
    },
    {
        /* 轴:结构。加一道过滤不记账,「我的文章去哪了」就开始撒谎。 */
        id: 'R4', file: LOGIC,
        why: '报价筛选藏起来的篇数不进 hidden 总账',
        from: '      total: byQuote + byPreselect + byOptimize + byCart,',
        to: '      total: byPreselect + byOptimize + byCart,',
        expectRed: { p: ['P3c'] },
    },
    {
        /* 轴:结构。漏一档,那一档看起来仍然正常。 */
        /*
         * 🔴 第一版的锚是那一行标签本身 —— 它在文件里出现 **7 次**,
         *    运行器当场报「毒没下成」(锚命中 7 次,要求恰好 1 次)。
         *    它把"没下成"和"锁没牙"分开报了,这正是它该做的:
         *    两者在 rc 上同形,混在一起就会把一次失败的下毒读成一把没牙的锁。
         *    ⇒ 锚带上**它上下各一行**,定位到「未分发」那一档那一处(唯一)。
         */
        id: 'R5', file: PC,
        why: '把「未分发」那一档的报价标签摘掉(漏的那一档看不出这篇属于谁)',
        from: `                        <span className="text-[10px] text-muted-foreground">{a.keyword}</span>
                        <ArticleQuoteTag quoteId={a.quoteId} show={quoteTagVisible} />
                      </div>`,
        to: `                        <span className="text-[10px] text-muted-foreground">{a.keyword}</span>
                      </div>`,
        expectRed: { p: ['P4b'] },
    },
    {
        /* 轴:结构。切客户不归零 ⇒ 列表恒空,长得像"这个客户没有文章"。 */
        id: 'R6', file: PC,
        why: '切客户不把报价筛选归零',
        from: '  useEffect(() => { setQuoteFilter(null); }, [currentBrandId]);',
        to: '',
        expectRed: { p: ['P4e'] },
    },
];

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
        const bak = `${target}.z210bak-${p.id}`;
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
