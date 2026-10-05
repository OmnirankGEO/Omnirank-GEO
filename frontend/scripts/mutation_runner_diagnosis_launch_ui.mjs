/**
 * mutation_runner_diagnosis_launch_ui.mjs —— 证明 test-diagnosis-launch-ui.mjs 的每条锁有判别力
 * (工单 WO_DIAGNOSIS_LAUNCH_UI_2026-08-05 · 2026-08-05)
 *
 * 做法:把真实源码逐个改坏(一次一处),重跑锁,**锁必须转红**。
 * 锁没红 = 那条判据抓不到它本该抓的行为 = 护栏是瞎的。
 *
 * ## 三条这次特意防住的旧坑
 *
 * 1. 🔴 **改坏了但没改动**(替换字符串没命中)会让变异静默变成"跑了个原样",
 *    结果锁当然绿,却被读成"锁没判别力"。所以每处变异都先断言 `命中数 === 期望数`,
 *    不命中直接 SKIP 并报红,**绝不**当成 SURVIVED。(SKIP≠SURVIVED,这条已经救过三次场。)
 * 2. 🔴 **改动丢失**:2026-08-04 有过 `git stash` 撞上后台变异 runner、把真改动烤进交付代码。
 *    这里全程只在内存里留原文 + `finally` 恢复 + 收尾用 sha256 逐文件核对还原成功,
 *    没还原干净就**非零退出**并把备份留在 .bak 旁边。
 * 3. 🔴 **行尾**:2026-08-05 媒体榜那单的 runner 自己把 10 个源文件写成了 CRLF,
 *    用例全绿而 diff 整片飘红。这里读写一律走 Buffer,不碰文本换行转换。
 *
 * 用法:node scripts/mutation_runner_diagnosis_launch_ui.mjs   (在 frontend/ 下跑)
 */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import process from 'node:process';
import { spawnSync } from 'node:child_process';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const root = process.cwd();
const LOCK = path.join(root, 'scripts/test-diagnosis-launch-ui.mjs');
const PAGE = path.join(root, 'src/pages/Diagnosis/NewDiagnosis.tsx');
const STEPS = path.join(root, 'src/pages/Diagnosis/launch/launchSteps.ts');

const TARGETS = [PAGE, STEPS];
const original = new Map(TARGETS.map(f => [f, fs.readFileSync(f)]));   // Buffer,不做换行转换
const sha = buf => crypto.createHash('sha256').update(buf).digest('hex');
const originalSha = new Map([...original].map(([f, b]) => [f, sha(b)]));

function restoreAll() {
    for (const [f, buf] of original) fs.writeFileSync(f, buf);
}

function runLock() {
    const r = spawnSync(process.execPath, [LOCK], { cwd: root, encoding: 'utf8' });
    return { code: r.status, out: (r.stdout || '') + (r.stderr || '') };
}

/**
 * 一处变异:在 file 里把 from 换成 to,期望恰好命中 expectHits 次。
 * 命中数不对 → SKIP(记红),绝不静默当 SURVIVED。
 */
function mutate(file, from, to, expectHits = 1) {
    const src = original.get(file).toString('utf8');
    const hits = src.split(from).length - 1;
    if (hits !== expectHits) return { skipped: true, hits };
    assertRulerWorks(file);
    fs.writeFileSync(file, Buffer.from(src.split(from).join(to), 'utf8'));
    /* 🔴 「毒写成语法错」与「锁咬住了」在 rc 上同形 ⇒ 并进 skipped 那一侧,
          不算杀也不算活(与本文件既有的「命中数不对 → SKIP」同一口径)。 */
    if (!syntaxOk(file)) {
        fs.writeFileSync(file, original.get(file));
        console.log(`  ${NOT_LANDED_SYNTAX} · ${file}`);
        return { skipped: true, hits, syntaxBroken: true };
    }
    return { skipped: false, hits };
}

// 🔴 [#149 裁定退役] M7(L3 示例卡必须真渲染)与 M9/M10(L5 示例题面拼接 fail-soft)
//    随 `QuestionExampleCard.tsx` / `searchQuestionExamples.ts` 一并退役。
//    M6「本次会得到」锚点保留(Review 明确)。
const MUTATIONS = [
    {
        name: 'M1 限宽容器改回裸视口断点(lg:max-w-)',
        why: 'L2 布局骨架不许挂裸视口断点',
        file: PAGE,
        from: '"@min-[880px]:max-w-[1240px]"',
        to: '"lg:max-w-[1240px]"',
    },
    {
        name: 'M2 摘掉 @container 查询锚点',
        why: 'L2 没有锚点,所有容器断点都是死类',
        file: PAGE,
        from: '<div className="@container w-full">',
        to: '<div className="w-full">',
    },
    {
        name: 'M3 两栏栅格改回裸视口断点',
        why: 'L2 launch-grid 必须用容器断点',
        file: PAGE,
        from: '@min-[880px]:grid-cols-[minmax(0,1fr)_340px]',
        to: 'lg:grid-cols-[minmax(0,1fr)_340px]',
    },
    {
        // 🔴 [#149 返修 · 换锚不换意图] 原靶是「贴底类必须带 pointer-fine 守卫」那三条类,
        //    它们已被**裁定乙**(560b9c4f1)整组删除 ⇒ 替换串不命中 ⇒ 本条 SKIP。
        //    **SKIP 不是 PASS**:那等于这一格根本没测。
        //    但不变式没死,而且换了更强的形态:继任锁是
        //    `test-diagnosis-launch-ui.mjs:254`「CTA className 一条贴底/固定类都不许有」。
        //    ⇒ 变异跟着不变式走:往 CTA 加回一条贴底类,继任锁必须杀掉它。
        name: 'M4 给 CTA 加回一条贴底类(继任锁必须杀掉)',
        why: 'L2-e CTA 在任何档任何指针类型下都必须在流里(裁定乙)',
        file: PAGE,
        from: '"@max-[880px]:order-2",',
        to: '"@max-[880px]:order-2", "@max-[880px]:sticky",',
    },
    // 🔴 [#149 返修] M5「提交一个请求模型里没有的字段」**从本 runner 退役**。
    //
    //    它的杀手(§1 提交字段集 ⊆ `server.py` `DiagnosisRequest`)已于 2026-08-17
    //    (WO-LATENT-TRAPS §3)**整条挪到后端 pytest**:
    //      `tests/test_diagnosis_launch_request_model_lock_2026_08_17.py`
    //    理由是它要引后端文件,而镜像的 frontend-builder 阶段够不到 server.py,
    //    于是它在每次构建里大声 SKIP 然后 exit 0 —— 构建绿 ≠ 判据过。
    //    抽取器同源留在 JS 侧(`dump-diagnosis-submit-fields.mjs`,后端锁调它 2 处)。
    //
    //    🔴 所以它在这里 SURVIVED **不代表有洞** —— 是**杀手根本不在这把尺子里**。
    //    留着只会让本 runner 永远交付不了,并把「杀手在别处」误报成「锁没牙」。
    //    (变异存活三态之外的第四问:杀手是不是根本没被调用。)
    {
        name: 'M6 拆掉「本次会得到」锚点',
        why: 'L3 IA 锚点在位',
        file: PAGE,
        from: 'data-testid="outcome-list"',
        to: 'data-testid="outcome-list-renamed"',
    },
    {
        name: 'M8 把全屏弹窗塞回 @container 子树里',
        why: 'L4 fixed 后代会被 contain:layout 拽住,遮罩错位',
        file: PAGE,
        from: '            </form>\n\n            </>\n            )}',
        to: '            </form>\n            <div className="fixed inset-0 z-50" />\n\n            </>\n            )}',
    },
    {
        name: 'M12 步骤指示恒返回第 3 步',
        why: 'L5 三步必须真的分得开',
        file: STEPS,
        from: "    if (!input.brandName.trim() || !input.industry.trim()) return 1;\n    if (input.questionCount < 1) return 2;\n    return 3;",
        to: '    return 3;',
    },
];

console.log('=== 变异前:先证锁在干净树上是绿的(基线) ===');
const base = runLock();
console.log(`  基线退出码 = ${base.code}`);
if (base.code !== 0) {
    console.log('🔴 干净树上锁就是红的 —— 变异结果没有意义,先修锁/修代码');
    console.log(base.out.split('\n').filter(l => l.includes('🔴')).join('\n'));
    process.exit(1);
}

/* 🔴 牙证:这道语法前置在本 runner 里真的会红(抛异常、自还原)。 */
proveGuardHasTeeth(PAGE);

const rows = [];
try {
    for (const m of MUTATIONS) {
        const applied = mutate(m.file, m.from, m.to, m.expectHits ?? 1);
        if (applied.skipped) {
            rows.push({ ...m, verdict: 'SKIP', detail: `替换串命中 ${applied.hits} 次(期望 ${m.expectHits ?? 1})` });
            restoreAll();
            continue;
        }
        const r = runLock();
        restoreAll();
        rows.push({ ...m, verdict: r.code === 0 ? 'SURVIVED' : 'KILLED', detail: `退出码 ${r.code}` });
    }
} finally {
    restoreAll();
}

// 收尾:逐文件核对真的还原干净了(不核对 = 把变异烤进交付代码的老坑)
let dirty = 0;
for (const f of TARGETS) {
    const now = sha(fs.readFileSync(f));
    if (now !== originalSha.get(f)) {
        dirty++;
        const bak = f + '.mutation-backup';
        fs.writeFileSync(bak, original.get(f));
        console.log(`🔴 未还原:${path.relative(root, f)} · 原文已写到 ${path.relative(root, bak)}`);
    }
}

console.log(`\n${'='.repeat(70)}`);
for (const r of rows) {
    const mark = r.verdict === 'KILLED' ? '✅' : r.verdict === 'SKIP' ? '⚠️ ' : '🔴';
    console.log(`${mark} ${r.verdict.padEnd(8)} ${r.name}`);
    console.log(`           判据:${r.why} · ${r.detail}`);
}
const killed = rows.filter(r => r.verdict === 'KILLED').length;
const survived = rows.filter(r => r.verdict === 'SURVIVED').length;
const skipped = rows.filter(r => r.verdict === 'SKIP').length;
console.log(`\n共 ${rows.length} 处变异 · 杀死 ${killed} · 存活 ${survived} · SKIP ${skipped} · 未还原文件 ${dirty}`);
console.log('🔴 SKIP 不是 PASS:替换串没命中说明变异根本没发生,那一条等于没测。');
if (survived || skipped || dirty) {
    console.log('🔴 变异未全杀 / 有 SKIP / 有未还原 —— 不能交付');
    process.exit(1);
}
console.log('✅ 全部变异被杀,锁有判别力');
