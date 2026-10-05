#!/usr/bin/env node
/**
 * 终审 P0-1 路由锁的**撕锁自证**(R1-① 2 发 + R2 3 发 = 5 发)。
 *
 * ## 为什么单独一把 runner
 *
 * Review 亲毒证明上一版锁有绕过面:**换个名字的 lazy import + 第三条客户路由
 * 直挂 MediaDecisionConfirm** → 38/38 + 静态闸全绿。
 * 原因是分母写成了「我知道的那两条 path + 两个具名常量」,
 * 而要守的是「App.tsx 里有没有任何一条路通到确认组件」。
 *
 * 分母重写之后,**这两发必须常驻**:只在某次 shell 里手跑一遍不算锁 ——
 * 下一个人改 App.tsx 时没有任何东西会替他重跑一次。
 *
 * MUT-A = Review R1 给的原样绕过形态(验收变异)
 * MUT-B = barrel 绕过(`pages/DefensivePublish/index.ts` 再导出了那两个组件,
 *         于是 specifier 里根本不出现组件名)—— Review 未点名,但在同一分母里。
 *
 * ## R2(2026-08-24)追加 3 发
 *
 * A/B 两发关掉的是 `import()` **调用形**;Review 逐形态实测后指出同成本的第三面还开着:
 * **静态 import + 第三条路由**(`import { MediaDecisionConfirm } from '@/pages/DefensivePublish';`
 * 再 `<MediaDecisionConfirm />`)—— DYN_IMPORTS 零匹配,CLOSED_ROUTES 不看新路由。
 *
 * MUT-C = Review 点名的那一形态(R2 验收变异)  → viaBarrel 格
 * MUT-D = specifier 直含确认页路径的静态 import → direct 格
 * MUT-E = `import * as` 命名空间导入 barrel     → viaNamespace 格
 *         (顺手关的第三格:组件名根本不出现在 clause 里,C/D 都够不到它。
 *          多关一处就多欠一条判据 —— 所以它有自己的正样本 + 自己这一发。)
 *
 * 🔴 每发都写死 `expectRed`:**红在哪一格**要对得上。钝杀也会 exit!=0,
 *    那种红证明不了这一格被守着 —— 判「没跑」。
 *
 * ## R2 收口(2026-08-24):barrel 那两行 export 被删了,但这几发一发不减
 *
 * Review 拍板做设计反转:`pages/DefensivePublish/index.ts` 不再导出两个确认页
 * (双树 census 零生产消费方),`viaBarrel` 面被**结构性消灭**。
 * 于是 MUT-B / MUT-C 从"打活的绕过面"变成**打复引入哨兵**:
 * 它们注的毒在今天的 barrel 上连 tsc 都过不去,但判据是静态扫描、照样必须转红 ——
 * 这正是哨兵要守的那一天(有人把 export 加回来)。**一发都不删**:
 * 删掉哨兵的变异,哨兵就没人验了,和没有哨兵一样。
 * MUT-E(`import * as`)打的仍是**活锁**:它只看"是不是 namespace 导入本 barrel",
 * 不看名字,export 在不在都红。
 *
 * 🔴 还原**不用** `git checkout`(会把同轮其它改动一起撤掉):内存留原文,
 *    finally 写回,并逐字节核对。
 *
 * 跑法:cd frontend && node scripts/mutation_runner_defgeo_p0_route.mjs
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const APP = join(ROOT, 'src', 'App.tsx');
const CRITERION = join(ROOT, 'scripts', 'test-defgeo-gate2-fixes.mjs');

const LAZY = "const DefgeoPublishDecisionLink = lazy(() => import('@/pages/DefensivePublish/PublishDeepLink').then(m => ({ default: m.PublishDecisionDeepLink })));";
/*
 * 🔴 [2026-09-20 重锚] 这里原来写死整行:
 *   `<Route path="…/decision/:snapshotId" element={<DefgeoPublishDecisionLink />} />`
 * 产品后来把 element 包进了 `<ProtectedRoute requiredModule="writing">…</ProtectedRoute>`
 * (合法的加保护改动)⇒ 锚命中 0,**5 发毒全部没下成**。
 * 而本 runner 的收尾把它报成「5 发存活/没跑 —— 绕过面仍在」——
 * 把「没下成」和「存活」并进同一个数,还据此下了一句**没有证据支持**的结论
 * (毒根本没落盘,谈不上"绕过面仍在")。
 *
 * ⇒ 不再钉整行字面量,改为**运行时从 App.tsx 取那一行**:
 *   路由的 path 是这条路由的身份,element 怎么包是实现细节。
 *   取不到就当场 rc=3(门没跑成),不让它退化成"锚没命中"那种半哑读数。
 */
const ROUTE_PATH_ATTR = 'path="defensive-geo/publish/decision/:snapshotId"';
const ROUTE = (() => {
    const line = readFileSync(APP, 'utf8').split('\n').find((l) => l.includes(ROUTE_PATH_ATTR));
    if (!line) {
        console.log(`\n3 门没跑成:App.tsx 里找不到 ${ROUTE_PATH_ATTR} —— `
            + '这条路由被改名或删了,得先确认它去哪了,不能当"锚没命中"糊过去');
        process.exit(3);
    }
    return line.trim();
})();
const STATIC_ANCHOR = "import { Layout } from '@/components/layout/Layout';";
const INDENT = ' '.repeat(28);

const MUTATIONS = [
    {
        id: 'MUT-A',
        why: 'Review 原样绕过:换名 lazy import + 第三条客户路由直挂确认页',
        expectRed: '直接 import 了确认/状态页',
        edits: [
            [LAZY, `${LAZY}\nconst QuietlyRenamedThing = lazy(() => import('@/pages/DefensivePublish/MediaDecisionConfirm'));`],
            [ROUTE, `${ROUTE}\n${INDENT}<Route path="defensive-geo/publish/decision2/:snapshotId" element={<QuietlyRenamedThing />} />`],
        ],
    },
    {
        id: 'MUT-B',
        why: 'barrel 绕过:specifier 里不出现组件名,从 .then 里取(同一分母,Review 未点名)',
        expectRed: '经 barrel 取到了确认/状态页',
        edits: [
            [LAZY, `${LAZY}\nconst ViaBarrel = lazy(() => import('@/pages/DefensivePublish').then(m => ({ default: m.MediaDecisionConfirm })));`],
            [ROUTE, `${ROUTE}\n${INDENT}<Route path="defensive-geo/publish/decision3/:snapshotId" element={<ViaBarrel />} />`],
        ],
    },
    // ── R2(2026-08-24):同成本的第三种形态 —— **静态 import**。 ──────────────
    // 上面两发都是 import() 调用形;把它写成顶部一行静态 import + 新路由直挂,
    // DYN_IMPORTS 零匹配、CLOSED_ROUTES 也不看新路由,于是整条漏出去(Review 逐形态实测)。
    // 三发分别打三个 cell(direct / viaBarrel / viaNamespace),一发一格,不叠。
    {
        id: 'MUT-C',
        why: 'R2 验收变异:barrel 静态命名导入 + 第三条路由直挂 MediaDecisionConfirm',
        expectRed: '经 barrel 静态取到了确认/状态页',
        edits: [
            [STATIC_ANCHOR, `${STATIC_ANCHOR}\nimport { MediaDecisionConfirm } from '@/pages/DefensivePublish';`],
            [ROUTE, `${ROUTE}\n${INDENT}<Route path="defensive-geo/publish/decision4/:snapshotId" element={<MediaDecisionConfirm />} />`],
        ],
    },
    {
        id: 'MUT-D',
        why: 'R2:specifier 直含确认页路径的静态 import + 新路由(direct 那一格)',
        expectRed: '静态 import 了确认/状态页',
        edits: [
            [STATIC_ANCHOR, `${STATIC_ANCHOR}\nimport PublishCommandStatus from '@/pages/DefensivePublish/PublishCommandStatus';`],
            [ROUTE, `${ROUTE}\n${INDENT}<Route path="defensive-geo/publish/commands5/:commandId" element={<PublishCommandStatus />} />`],
        ],
    },
    {
        id: 'MUT-E',
        why: 'R2:命名空间导入 barrel —— 组件名根本不出现在 clause 里(viaNamespace 那一格)',
        expectRed: '命名空间导入了 DefensivePublish barrel',
        edits: [
            [STATIC_ANCHOR, `${STATIC_ANCHOR}\nimport * as Defgeo from '@/pages/DefensivePublish';`],
            [ROUTE, `${ROUTE}\n${INDENT}<Route path="defensive-geo/publish/decision6/:snapshotId" element={<Defgeo.MediaDecisionConfirm />} />`],
        ],
    },
];

const run = () => spawnSync(process.execPath, [CRITERION],
    { cwd: ROOT, encoding: 'utf8', timeout: 10 * 60 * 1000 });

console.log('=== 基线(未注毒)===');
if (run().status !== 0) {
    console.log('  🔴 基线就不绿 —— 后面的自证无效');
    process.exit(1);
}
console.log('  ✅ 基线全绿');

let survived = 0;
/* 🔴 牙证:前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(APP, console.log);

for (const m of MUTATIONS) {
    console.log(`\n=== ${m.id} · ${m.why} ===`);
    const original = readFileSync(APP, 'utf8');
    // 🔴 锚点没找到 / 不唯一,都记**没跑**,不记存活:处置完全不同。
    //    「不唯一」是本仓踩过的坑:String.replace 只换第一处,毒会落到我以为的**另一处**,
    //    红出现在别的判据上,看起来"转红了"其实那一格根本没被打(钝杀)。
    const badAnchor = m.edits
        .map(([from]) => [from, original.split(from).length - 1])
        .find(([, n]) => n !== 1);
    if (badAnchor) {
        const [from, n] = badAnchor;
        console.log(`  🔴 注毒锚点出现 ${n} 次(要求恰好 1 次)—— 这一发**没跑**:${from.slice(0, 60)}`);
        survived++;
        continue;
    }
    try {
        let poisoned = original;
        for (const [from, to] of m.edits) poisoned = poisoned.replace(from, to);
        assertRulerWorks(APP);
        writeFileSync(APP, poisoned, 'utf8');
        if (!syntaxOk(APP)) {
            console.log(`  ${NOT_LANDED_SYNTAX}`);
            errored++;
            continue;
        }
        const r = run();
        if (r.status === 0) {
            console.log('  🔴 变异存活 —— 绕过面还开着');
            survived++;
        } else {
            // 🔴 转红还不够 —— 必须红在**它该红的那一格**上。
            //    钝杀(把判据整个搞崩、或撞到隔壁那把锁)也会 exit != 0,
            //    那种红证明不了这一格被守着。
            const reds = (r.stdout || '').split('\n').filter((l) => l.includes('🔴'));
            const onTarget = reds.some((l) => l.includes(m.expectRed));
            if (!onTarget) {
                console.log(`  🔴 红了,但不在目标格上(要 "${m.expectRed}")—— 记**没跑**`);
                reds.slice(0, 3).forEach((l) => console.log(`      实际:${l.trim()}`));
                survived++;
            } else {
                console.log(`  ✅ 转红于目标格(${reds.find((l) => l.includes(m.expectRed)).trim()})`);
            }
        }
    } finally {
        writeFileSync(APP, original, 'utf8');
        if (readFileSync(APP, 'utf8').length !== original.length) {
            console.log('  🔴 还原后字节数对不上 —— 立刻人工检查');
            process.exit(1);
        }
    }
}

console.log('\n================================================================');
console.log(survived === 0
    ? `✅ P0-1 路由锁撕锁自证通过:${MUTATIONS.length} 发全部转红`
    : `🔴 ${survived} 发存活/没跑 —— 绕过面仍在`);
process.exit(survived === 0 ? 0 : 1);
