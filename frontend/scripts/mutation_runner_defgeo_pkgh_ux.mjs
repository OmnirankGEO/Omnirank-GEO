#!/usr/bin/env node
/**
 * 包H U-10 判据的**撕锁自证**(8 发)。
 *
 * 没有自证的正样本判据与恒绿无法区分 —— 本仓 2026-08-20 记过
 * 「`0x08` 假 \\b 负向锁恒绿顶了我两轮」。所以这里逐把注毒:
 * 改一行源码,跑 `test-defgeo-pkgh-ux.mjs`,**必须转红**。
 *
 * 每一发都打在"这条判据声称守住的那一格"上:
 *   MUT-1/2  U-3:改成画前端写死的标签 / 画两个「下一步」
 *   MUT-3    U-4:把金额提前到算价之前就渲染(「先解释后出现」失效)
 *   MUT-4/5/6 U-5:只列待确认项 / 拿掉「不会一次性全扣」/ 深链落点写死
 *   MUT-7/8  U-6:priceUnchanged 恒 true / 过期不再自动重建
 *
 * 🔴 还原**不用** `git checkout`(本仓明令禁止:会把同轮其它改动一起撤掉)——
 *    进程内存着原文,finally 写回,并在最后核对字节数。
 *
 * 跑法:cd frontend && node scripts/mutation_runner_defgeo_pkgh_ux.mjs
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const CRITERION = join(ROOT, 'scripts', 'test-defgeo-pkgh-ux.mjs');

const P = {
    progress: join(SRC, 'components/defensiveGeo/CommercialProgressBar.tsx'),
    launch: join(SRC, 'components/defensiveGeo/LaunchPanel.tsx'),
    todo: join(SRC, 'pages/DefensivePublish/DeliveryTodoList.tsx'),
    pure: join(SRC, 'pages/DefensivePublish/deliveryTodo.ts'),
};

const MUTATIONS = [
    {
        id: 'MUT-1',
        why: 'U-3:改成画前端写死的五个词,不再读服务端 steps',
        file: P.progress,
        from: '{view.steps.map((label, i) => {',
        to: "{['报价已发','客户已同意','待核单','已收款','服务中'].map((label, i) => {",
    },
    {
        id: 'MUT-2',
        why: 'U-3:每态画两个「下一步」(U-3 逐字要求唯一一个)',
        file: P.progress,
        from: '                {view.nextAction?.label && (',
        to: '                {view.nextAction?.label && (<><Button type="button" size="sm" data-testid="commercial-progress-next">多出来的一颗</Button></>)}\n                {view.nextAction?.label && (',
    },
    {
        id: 'MUT-3',
        why: 'U-4:金额在算价之前就渲染 —— 「先解释后出现」变成「一直都在」',
        file: P.launch,
        // 🔴 精确变异走了两次弯路,记在这里免得下一个人再踩:
        //
        //    ① 第一版把 `{preview && (` 换成 `{(preview || {...}) && (` ——
        //       块里仍读 `preview.costUserLabel`,null 解引用把整个面板炸没了。
        //       那是 blunt kill:转红的是"判据活性",不是"算价前不许有金额"。
        //    ② 第二版改成"放开守卫 + 把块里读 preview 的地方也换成字面量" ——
        //       但 `            {preview && (` 在这个文件里有**两处**,
        //       `String.replace` 只换第一处,而第一处是上面那个 `<dl>` 里的
        //       `preview.plannedCells`,照样炸。**锚点不唯一 = 打在了别处**。
        //
        //    现在的做法:锚点取那行唯一的注释,在它后面**追加**一句常驻金额。
        //    单行、语法合法、不动任何现有分支 —— 只多出"算价前就有金额"这一件事。
        from: '            {/* ── U-4 必答时刻其一:点之前就知道要花多少 ───────────────── */}',
        to: '            <p className="text-[14px] font-medium">本次体检消耗你的算力 1200</p>',
    },
    {
        id: 'MUT-4',
        why: 'U-5:只列待确认项(把"一页看完这一单"退化成"只看没做的")',
        file: P.todo,
        from: '    const rows = data ? orderTodoItems(data.items) : [];',
        to: "    const rows = data ? orderTodoItems(data.items).filter((i) => i.todoState === 'awaiting_confirm') : [];",
    },
    {
        id: 'MUT-5',
        why: 'U-5:拿掉「各自确认、各自计费,不会一次性全扣」那句',
        file: P.todo,
        from: '                    {DEFGEO_COPY.deliveryTodoIntro}',
        to: '                    这一单要确认的媒体方案都在这里。',
    },
    {
        id: 'MUT-6',
        why: 'U-5:深链落点写死(不再带这一项自己的快照 id)',
        file: P.todo,
        from: '        navigate(decisionDeepLink(snapshotId));',
        to: "        navigate(decisionDeepLink('fixed-snapshot'));",
    },
    {
        id: 'MUT-7',
        why: 'U-6:priceUnchanged 恒 true —— 价格变了也说"没变"',
        file: P.pure,
        from: '    if (typeof before !== \'number\' || !Number.isFinite(before)) return false;',
        to: '    return true;\n    if (typeof before !== \'number\' || !Number.isFinite(before)) return false;',
    },
    {
        id: 'MUT-8',
        why: 'U-6:过期不再自动重建,退回"给个错误框让她自己点"',
        file: P.launch,
        from: "            if (err.code === 'PREVIEW_EXPIRED') {\n                await runPreview(preview.exactTotalPoints);\n                return;\n            }",
        to: '            /* mutated: 不再自动重建 */',
    },
];

function runCriterion() {
    const r = spawnSync(process.execPath, [CRITERION], {
        cwd: ROOT, encoding: 'utf8', timeout: 15 * 60 * 1000,
    });
    return { code: r.status, out: `${r.stdout || ''}${r.stderr || ''}` };
}

// ── 基线:未注毒必须全绿,否则后面的"转红"说明不了任何事 ────────────
console.log('=== 基线(未注毒)===');
const base = runCriterion();
console.log(base.code === 0 ? '  ✅ 基线全绿' : '  🔴 基线就不绿 —— 后面的自证无效');
if (base.code !== 0) {
    console.log(base.out.split('\n').filter((l) => l.includes('🔴')).slice(0, 10).join('\n'));
    process.exit(1);
}

let survived = 0;
/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(MUTATIONS[0].file, console.log);

for (const m of MUTATIONS) {
    console.log(`\n=== ${m.id} · ${m.why} ===`);
    const original = readFileSync(m.file, 'utf8');
    const anchors = [m, ...(Array.isArray(m.extra) ? m.extra : m.extra ? [m.extra] : [])];
    const missing = anchors.filter((a) => !original.includes(a.from));
    if (missing.length) {
        // 🔴 「锚点没找到」记**没跑**,不记存活 —— 两者处置完全不同:
        //    没跑要去修锚点,存活要去修锁。
        console.log(`  🔴 注毒锚点没找到 —— 这一发**没跑**(不是存活)。锚点:${missing[0].from.slice(0, 60)}`);
        survived++;
        continue;
    }
    let poisoned = original;
    for (const a of anchors) poisoned = poisoned.replace(a.from, a.to);
    try { assertRulerWorks(m.file); } catch (err) { console.log(`  ${err.message}`); process.exit(3); }
    writeFileSync(m.file, poisoned, 'utf8');
    try {
        if (!syntaxOk(m.file)) {
            console.log(`  ${NOT_LANDED_SYNTAX}`);
            continue;
        }
        const r = runCriterion();
        if (r.code === 0) {
            console.log('  🔴 变异存活 —— 判据没守住这一格');
            survived++;
        } else {
            const reds = r.out.split('\n').filter((l) => l.includes('🔴')).slice(0, 3);
            console.log(`  ✅ 转红(${reds.length ? reds[0].trim() : `exit ${r.code}`})`);
        }
    } finally {
        writeFileSync(m.file, original, 'utf8');
        const back = readFileSync(m.file, 'utf8');
        if (back.length !== original.length) {
            console.log('  🔴 还原后字节数对不上 —— 立刻人工检查');
            process.exit(1);
        }
    }
}

console.log('\n================================================================');
console.log(survived === 0
    ? `✅ 撕锁自证通过:${MUTATIONS.length} 发全部转红`
    : `🔴 ${survived} 发存活/没跑 —— 判据有恒绿的格子`);
process.exit(survived === 0 ? 0 : 1);
