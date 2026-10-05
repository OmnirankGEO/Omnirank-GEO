#!/usr/bin/env node
/**
 * 注毒自证 · #199 「禁猜」清理第一批 —— 每处一毒(工单 §2)。
 *
 * @@R@@ 别与 build / 其他渲染闸并行(毒在工作树里);中途别 kill(还原不执行)。
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, NOT_LANDED_SYNTAX , proveGuardHasTeeth } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const GATES = {
    n: 'scripts/verify-no-guess-batch1.mjs',
    /* [a2] 轮询闸:a3 附注那条"按状态判而不是按文案判"的锁在它里面 */
    q: 'scripts/verify-image-note-prepare-polling.mjs',
    /*
     * [a2] 🔴 行为臂也得进来。Review 复跑时那两发绿的毒,恰恰是
     *    "结构上看得见、行为上量不到"的那一类 —— 只拿结构闸注毒,
     *    就永远发现不了「DOM 里有但点不到」和「暴露了但从没写进去」。
     */
    r: 'scripts/test-no-guess-batch1-render.mjs',
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

const LABELS = 'src/pages/Publishing/mediaTableLabels.ts';
const PC = 'src/pages/Publishing/PublishCenter.tsx';
const ROW = 'src/components/density/InfoBadgeRow.tsx';
const HINT = 'src/components/pricing/PricingUnavailableHint.tsx';
const BRIDGE = 'src/components/workbench/DecisionBarBridge.tsx';
const MON = 'src/pages/Monitoring/index.tsx';
const PARTS = 'src/components/workbench/parts.tsx';
const PCTX = 'src/context/PricingContext.tsx';
const PLAN = 'src/pages/Writing/artifactPollPlan.ts';

const POISONS = [
    {
        id: 'V1', file: LABELS,
        why: '取证还没到就先填一个"看着合理"的区间(99)—— 这张卡要清的正是这种猜',
        from: `export const WEIGHT_RANGE_MAX: number | null = null;`,
        to: `export const WEIGHT_RANGE_MAX: number | null = 99;`,
        expectRed: { n: ['N1a'] },
    },
    {
        id: 'V2', file: PC,
        why: '把权重列头自己的 HelpHint 删掉(退回"说明藏在隔壁那一列")',
        from: `                  {weightHeader('pc')}
                  <HelpHint title="电脑权重怎么看?" side="bottom">{weightHint()}</HelpHint>`,
        to: `                  {weightHeader('pc')}`,
        expectRed: { n: ['N1d'] },
    },
    {
        id: 'V3', file: PC,
        why: '关掉图例的可见短标签(退回四个光秃秃的 chip)',
        from: `      <InfoBadgeRow
        showLabel
        gap="gap-3"`,
        to: `      <InfoBadgeRow
        gap="gap-3"`,
        expectRed: { n: ['N2b'] },
    },
    {
        id: 'V4', file: LABELS,
        why: '折叠文案**漏列排序** —— 排序决定你先看到谁,不列的话人永远不知道这张表怎么排的',
        from: `    return \`当前生效:排序 \${sortLabel(s.sort)} · \${filterPart} · 展开调整 · \${countPart}\`;`,
        to: `    return \`当前生效:\${filterPart} · 展开调整 · \${countPart}\`;`,
        expectRed: { n: ['N3a'] },
    },
    {
        id: 'V5', file: HINT,
        why: '退避窗内出口**仍可点** —— 点了什么都不会发生(refresh 在窗内静默不发)',
        from: `                disabled={waiting}`,
        to: `                disabled={false}`,
        expectRed: { n: ['N4f'] },
    },
    {
        id: 'V6', file: BRIDGE,
        why: '徽章的 showLabel / missing 又不传了(退回只有「85 /100」)',
        from: `              missing={snapshot.completenessDetail?.missing}
              brandId={snapshot.id}
              showLabel`,
        to: ``,
        expectRed: { n: ['N5b'] },
    },
    {
        id: 'V7', file: MON,
        why: '「价目不可用」四个字回来一处(结构锁的分母是整个 src,不是我记得的那三处)',
        from: `                                pricingLoading ? '价目读取中…' : PRICING_UNAVAILABLE_LABEL`,
        to: `                                pricingLoading ? '价目读取中…' : '价目不可用'`,
        expectRed: { n: ['N4a'] },
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
/* ══ a2 追加:Review 复跑抓到的四条接缝,每条一毒 ═══════════════════════ */
POISONS.push(
    {
        id: 'V8', file: PARTS,
        why: '把缺项弹层从 Portal 退回 absolute —— DOM 里照样有,但被 overflow-hidden 的 Card 裁掉',
        from: `      {open && pos && typeof document !== 'undefined' && createPortal(
        <span ref={popRef}
          style={{ position: 'fixed', top: pos.top, left: pos.left, width: pos.width, zIndex: 9999 }}
          className="block rounded-md border border-border bg-popover p-2 text-[11px]
          font-sans font-normal text-popover-foreground shadow-lg"
          role="dialog" data-testid="completeness-missing">`,
        to: `      {open && pos && typeof document !== 'undefined' && (
        <span ref={popRef}
          className="absolute left-0 top-full z-50 mt-1 block w-56 rounded-md border border-border
          bg-popover p-2 text-[11px] font-sans font-normal text-popover-foreground shadow-lg"
          role="dialog" data-testid="completeness-missing">`,
        also: [{
            from: `            data-testid="completeness-goto">去补资料 →</a>
        </span>,
        document.body,
      )}`,
            to: `            data-testid="completeness-goto">去补资料 →</a>
        </span>
      )}`,
        }],
        /*
         * 🔴 期望红钉的是 **R5b**(与弹层高度无关的那格),不是 R5。
         *    实测:3 个缺项时弹层塞得进那张 Card,R5 照样绿 —— 那不是"没被裁",
         *    是"今天这组数据还没超出边界"。夹具已改成 8 项让 R5 也有牙,
         *    但**锁要钉在不变式上**,不能钉在一个恰好的高度上。
         */
        expectRed: { n: ['N5c2'], r: ['R5b'] },
    },
    {
        id: 'V9', file: LABELS,
        why: '让说法表只覆盖前 6 档(复刻今天的真缺陷:芯片 9 档、表里 6 键)',
        from: `    const hit = MEDIA_SORT_OPTIONS.find((o) => o.value === raw);`,
        to: `    const hit = MEDIA_SORT_OPTIONS.slice(0, 6).find((o) => o.value === raw);`,
        expectRed: { n: ['N3d'] },
    },
    {
        id: 'V10', file: LABELS,
        why: '兜底句退回「默认顺序」—— 一句关于排列方式的断言拿去兜"不知道"',
        from: `export const SORT_FALLBACK_LABEL = '未识别的排序';`,
        to: `export const SORT_FALLBACK_LABEL = '默认顺序';`,
        expectRed: { n: ['N3d4'] },
    },
    {
        id: 'V11', file: PCTX,
        why: '429 时**不写** retryNotBefore 的 state(Review 那发绿毒 P3 的原形):'
            + '字段照样暴露、N4e 照样绿,界面却永远不知道自己在退避窗里',
        from: `            setRetryNotBefore(retryNotBeforeRef.current);`,
        to: ``,
        expectRed: { r: ['R6a'] },
    },
    {
        id: 'V12', file: PLAN,
        why: 'artifactLine 退回**按文案**判(includes 那串中文)—— 改一个字承诺句就被静默挤掉',
        from: `    const keepPlanLine = typeof plan !== 'string' && plan?.state === 'unknown';`,
        to: `    const keepPlanLine = typeof plan !== 'string' && plan?.terminal === true`
            + ` && !plan?.retryable && planLine.includes('不会重复扣算力');`,
        expectRed: { q: ['Q3b10'] },
    },
    {
        id: 'V13', file: PLAN,
        why: '把 unknown 那一档的 state 标成 failed —— 新字段"存在"不等于"标对了"',
        from: `            state: 'unknown',`,
        to: `            state: 'failed',`,
        expectRed: { q: ['Q3b12'] },
    },
);

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
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红。
 *    少了它,`syntaxOk()` 平时永远返回 true —— 一把恒 true 的尺子
 *    与「每一发毒都下成了」读数完全同形。
 *    (对照臂 `assertRulerWorks` 管反方向:恒 false。两条臂缺一不可。)
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
     * 🔴 对照臂**一发一次**,不是一条 edit 一次:放进循环的话,
     *    第二条 edit 之前文件已被第一条改过,对照臂看到的是**中间态** ——
     *    它会喊「没下毒的文件就判不过 ⇒ 尺子坏了」,而尺子好好的。
     *    对照臂要问的是「这一发**开始之前**盘上是好的吗」,所以在这里问一次。
     */
    for (const rel of touched) {
        try {
            assertRulerWorks(abs(rel));
        } catch (err) {
            /*
             * 🔴 退出前**必须先还原**。第一版直接 `process.exit(3)`,
             *    备份文件与半截毒就留在了树上 —— 下一次跑基线当场不干净,
             *    而那个读数(「基线红」)和「产品真坏了」完全同形。
             *    本文件抬头那段全局 sha 警告说的就是这件事,我还是踩了一次。
             */
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
     * 🔴 语法闸放在**所有 edits 都落完之后**,不放在逐条循环里。
     *    一发毒可以由多条 edit 组成(V8:先把 `createPortal(` 换成 `(`,
     *    再由 `also` 去掉 `, document.body,`),**中间态本来就是不平衡的**。
     *    第一版我把它放进循环 ⇒ V8 被误报成「毒写成了语法错」,
     *    而它其实完全正常 —— 是我的闸放错了位置,不是那发毒坏了。
     *    (「毒没下成」这个读数本身也会误报,它同样需要被怀疑。)
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
