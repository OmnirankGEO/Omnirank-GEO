#!/usr/bin/env node
/**
 * 注毒自证 · #178 —— 证明上面那两套判据**真的有牙**。
 *
 * 纪律(每一条都是踩过的坑):
 * 1. **先证基线失败集为空**。基线本来就红时,每发毒都会显示「命中」,包括一条都没多红的那发
 *    (`a-red-baseline-makes-every-poison-look-caught`)。
 * 2. **比失败集的差,不比 rc**。并打印是**哪一条**抓住的 —— 钝杀(判据崩了)与真抓
 *    在 rc 上完全同形。
 * 3. **毒必自证下成了**:下毒前后 sha256 必须不同;没下成的毒和没牙的锁长得一模一样。
 * 4. **还原用字节拷回,不用 `git checkout --`**:后者回的是 HEAD,不是「下毒前那份」——
 *    同文件里未提交的改动会被静默冲掉(09-09 一天踩两次)。还原后再核 sha256 + git diff。
 * 5. 每发毒写死 `expectRed`:红必须落在**它那一格**。红在别处 = 判据钝,不算抓住。
 *
 * 跑法:cd frontend && node scripts/mutation_runner_selection_all_excluded.mjs
 *      加 --build-only 只跑 build 链那套(快;但浏览器臂的牙就没证到,交付物里要写明)
 */
import { readFileSync, writeFileSync, copyFileSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { syntaxOk, assertRulerWorks, proveGuardHasTeeth, NOT_LANDED_SYNTAX } from './lib/poison-syntax-guard.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO = join(ROOT, '..');
const BUILD_ONLY = process.argv.includes('--build-only');

const GATE_BUILD = 'scripts/verify-selection-all-excluded-exits.mjs';
const GATE_BROWSER = 'scripts/test-selection-all-excluded-render.mjs';

const sha = (abs) => createHash('sha256').update(readFileSync(abs)).digest('hex');
const abs = (rel) => join(ROOT, rel);

let failures = 0;
const say = (m) => console.log(m);

/** 跑一个判据,回 {rc, redSet, out}。redSet = 报红的判据编号集合。 */
function run(gateRel) {
    let out = '';
    let rc = 0;
    try {
        out = execFileSync(process.execPath, [abs(gateRel)], {
            cwd: ROOT, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024,
            timeout: 15 * 60 * 1000,
        });
    } catch (e) {
        rc = typeof e.status === 'number' ? e.status : 1;
        out = String(e.stdout || '') + String(e.stderr || '');
    }
    const redSet = new Set(
        [...out.matchAll(/^\s*FAIL\s+(\S+)/gm)].map((m) => m[1]),
    );
    return { rc, redSet, out };
}

const PAGE = 'src/pages/Selection/SelectionPage.tsx';
const CARD = 'src/pages/Selection/components/AllExcludedExitCard.tsx';
const MOD = 'src/pages/Selection/utils/allExcludedExits.ts';

/**
 * 每发毒:file + 一处**唯一**的字符串替换 + 期望变红的判据编号 + 要跑哪几套。
 *
 * 毒的选法遵循「改位置/改语义,不是删掉那一行」—— 删掉整行常常只触发
 * 语法/类型错误,证不了判据在看语义。
 */
const POISONS = [
    {
        id: 'P1',
        why: '把绿条的门换回裸 businessLinesSubmitted(= 全排除时照挂「正在准备方案」这句谎)',
        file: PAGE,
        from: `        {shouldClaimSubmitted({
          status: data.status,
          allExcluded: !!allExcluded,
          businessLinesSubmitted,
        }) && (`,
        to: `        {businessLinesSubmitted && (`,
        expectRed: { build: ['C6'], browser: ['R6g'] },
    },
    {
        id: 'P2',
        why: '把业务线步里的出口卡摘掉(只剩选词步那一处 —— 存在锁会全绿)',
        file: PAGE,
        from: `        <SelectionHeader brand={brand} brandName={data.brand_name} phase="selection" context={data.selection_context} />

        {allExcludedCard}`,
        to: `        <SelectionHeader brand={brand} brandName={data.brand_name} phase="selection" context={data.selection_context} />`,
        expectRed: { build: ['C2'], browser: ['R6a'] },
    },
    {
        id: 'P3',
        why: '提交闸照旧禁用,但把原因抹成空串(= 灰按钮不说话,和死按钮无区别)',
        file: MOD,
        from: `            reason: '还没有可交付的问法 —— 先在上方加一条选型/价格类问法，或让报价方补充',`,
        to: `            reason: '',`,
        expectRed: { build: ['B9b', 'B9c'], browser: ['R8c'] },
    },
    {
        id: 'P4',
        why: '全排除态的提交键打回 submit-business-lines(死胡同变死循环;卡片照在、按钮照在)',
        file: PAGE,
        from: `            label="提交新增问法 →"
            onClick={handleSubmitKeywords}`,
        to: `            label="提交新增问法 →"
            onClick={handleSubmitBusinessLines}`,
        expectRed: { build: ['C4'], browser: ['R9d'] },
    },
    {
        id: 'P5',
        why: 'reconcileFromPage 改成无条件覆盖(= 卡片被紧随其后的 fetchData 抹掉,客户只看见它闪一下)',
        file: MOD,
        from: `    if (!Object.prototype.hasOwnProperty.call(o, 'all_excluded')) return prev;`,
        to: `    if (false) return prev;`,
        // 🔴 browser 臂是 R11(不是 R6a):C 把三个字段补进 GET 之后,R6a 那一格
        //    即使无条件覆盖也会被 GET 重新立起来 —— 这个 bug 只在**后端还没带字段**
        //    (前端先上车 / 后端被回滚)那一格露出来,R11 钉的正是它。
        expectRed: { build: ['B8'], browser: ['R11a'] },
    },
    {
        id: 'P6',
        why: 'notifyState 不看后端的 notify_sent(= 后端已推过还给客户一个按钮,重复打扰报价方)',
        file: MOD,
        from: `    if (input.notifySent || input.alreadySent) {`,
        to: `    if (input.alreadySent) {`,
        expectRed: { build: ['B11a'], browser: ['R7e'] },
    },
    {
        id: 'P7',
        why: 'allExcluded 改成「有排除条目就算」(= 每一次正常的部分排除都弹一张死胡同卡)',
        file: MOD,
        from: `        allExcluded: o.all_excluded === true,`,
        to: `        allExcluded: rows.length > 0,`,
        /**
         * 🔴 **只有 build 臂 —— 这是实测结论,不是省事**。把桩改成有状态之后才看清:
         *    带排除条目的「部分排除」响应在真契约里**一定**把 status 推出
         *    `selecting / business_lines_submitted`(落 keywords_submitted),
         *    而出口卡只挂在那两个态的分支里 ⇒ 这发毒在浏览器层**够不着**,
         *    渲染分支结构本身成了第二道防线。
         *    这属于「毒够不着目标」,不是「锁没牙」—— 两者在 rc 上同形,必须分开写明。
         *    哪天出现"部分排除但留在 selecting"的响应,这里要补一格浏览器臂。
         */
        expectRed: { build: ['B5'] },
    },
    {
        id: 'P8',
        why: '卡片里的输入框 onAdd 断线(能打字、加不进去 —— 死输入框,而"输入框在不在"全绿)',
        file: CARD,
        from: `                    <CustomKeywordInput
                        onAdd={onAddKeyword}`,
        to: `                    <CustomKeywordInput
                        onAdd={() => { }}`,
        expectRed: { build: ['C8'], browser: ['R9e'] },
    },
    {
        id: 'P9',
        why: '卡片标题不看 reason(= 方向里压根没配词时也说"你的问法被排除了",冤枉客户)',
        file: MOD,
        /**
         * 🔴 锚要**逐站点**,不是「这个判断在文件里还出现着」:上一版锚在
         *    `if (state.reason === REASON_NO_KEYWORDS) {` 上,而 cardTitle 与 cardSubtitle
         *    各有一条同样的判断 ⇒ 命中 2 次,runner 当场拒绝下毒(报的是"毒没下成",
         *    不是"锁没牙" —— 这两件事必须分得开)。
         */
        from: `        return '这个方向下还没有配好的问法';`,
        to: `        return \`这 \${state.total} 个问法不会让 AI 推荐具体品牌，未计入本次交付\`;`,
        // 只有 build 臂:现在没有后端在发 no_keywords_for_lines,浏览器层够不着(见交付物 §7)
        expectRed: { build: ['B13b'] },
    },
    {
        id: 'P10',
        why: '标题换了、解释没换(半句真话:继续拿"知识/百科类问法"怪客户写错词)',
        file: MOD,
        from: `        return '不是你写错了 —— 这个业务方向下还没有配好可交付的问法。你可以自己加一条，或让报价方补。';`,
        to: `        return '它们是知识/百科类问法，投了也拿不到推荐位。';`,
        expectRed: { build: ['B13e'] },
    },
    {
        id: 'P11',
        why: '主出口不看 reason(= 方向里一条词都没生成时,还把"自己写一条"摆成主出口,把配词推给客户)',
        file: MOD,
        from: `    return state.reason === REASON_NO_KEYWORDS ? 'notify' : 'add_keywords';`,
        to: `    return 'add_keywords';`,
        expectRed: { build: ['B14b'], browser: ['R12c'] },
    },
    {
        id: 'P12',
        why: 'order 的父容器退回普通 block(两个 order-* 类名当场变成什么都不做,主出口顺序静默失效)',
        file: CARD,
        from: `            <div className="flex flex-col rounded-2xl border border-amber-200 bg-amber-50 p-4 md:p-5">`,
        to: `            <div className="rounded-2xl border border-amber-200 bg-amber-50 p-4 md:p-5">`,
        expectRed: { build: ['B14e'], browser: ['R12c'] },
    },
    {
        id: 'P13',
        why: 'GET 漏 reason 时整体覆盖(卡片退回默认档 ⇒ 在"方向里压根没配词"时说成"你的问法被排除了")',
        file: MOD,
        from: `    if (prev && parsed.reason === REASON_ALL_EXCLUDED`,
        to: `    if (false && parsed.reason === REASON_ALL_EXCLUDED`,
        expectRed: { build: ['B8f'] },
    },
    {
        id: 'P14',
        why: '把原因行也调成半透明(= #179 根因①那个形态:理由跟着禁用按钮一起变淡,看不清)。'
            + '这发毒同时证明样式臂在**注入真 CSS 之后**才真的有牙 —— 裸 DOM 上它恒绿',
        file: 'src/pages/Selection/components/BottomActionBar.tsx',
        from: `            <p className="text-xs text-amber-700 text-right max-w-[52%] shrink" data-testid="bottom-bar-disabled-reason">`,
        to: `            <p className="text-xs text-amber-700 text-right max-w-[52%] shrink opacity-40" data-testid="bottom-bar-disabled-reason">`,
        expectRed: { browser: ['R8e'] },
    },
];

// ══ 0. 基线:两套判据必须**失败集为空** ═══════════════════════════════
say('=== 0. 基线(必须失败集为空,否则每发毒都像"命中") ===');
const baseline = {};
for (const [name, gate] of [['build', GATE_BUILD], ['browser', GATE_BROWSER]]) {
    if (name === 'browser' && BUILD_ONLY) { say('  browser: --build-only,跳过(牙未证,交付物须写明)'); continue; }
    const r = run(gate);
    baseline[name] = r.redSet;
    const okBase = r.rc === 0 && r.redSet.size === 0;
    say(`  ${okBase ? 'OK  ' : 'FAIL'} ${name} 基线 rc=${r.rc} 失败集=${r.redSet.size ? [...r.redSet].join(',') : '空'}`);
    if (!okBase) {
        say('  🔴 基线不干净,注毒读数无意义。先修基线。');
        process.exit(1);
    }
}

// ══ 1. 逐发注毒 ═══════════════════════════════════════════════════════
/*
 * 🔴 牙证:这道语法前置**在本 runner 里**真的会红(恒 true 的尺子与
 *    「每一发毒都下成了」读数完全同形)。它抛异常、自己还原。
 */
proveGuardHasTeeth(abs(POISONS[0].file), say);

for (const p of POISONS) {
    say(`\n=== ${p.id} ${p.why} ===`);
    const target = abs(p.file);
    const bak = target + `.a178bak-${p.id}`;
    const before = sha(target);
    // 🔴 字节备份。还原靠它,不靠 git。
    copyFileSync(target, bak);
    const src = readFileSync(target, 'utf8');
    const hits = src.split(p.from).length - 1;
    if (hits !== 1) {
        say(`  FAIL 毒没下成:锚命中 ${hits} 次(要恰好 1 次)—— 锚不唯一/已漂移,不是"锁没牙"`);
        unlinkSync(bak);
        failures += 1;
        continue;
    }
    try { assertRulerWorks(target); } catch (e) { say(`  ${e.message}`); copyFileSync(bak, target); unlinkSync(bak); process.exit(3); }
    writeFileSync(target, src.replace(p.from, p.to), 'utf8');
    if (!syntaxOk(target)) {
        say(`  FAIL ${NOT_LANDED_SYNTAX}`);
        copyFileSync(bak, target); unlinkSync(bak); failures += 1; continue;
    }
    const after = sha(target);
    if (after === before) {
        say('  FAIL 毒没下成:sha256 没变(没下成的毒和没牙的锁长得一模一样)');
        copyFileSync(bak, target); unlinkSync(bak);
        failures += 1;
        continue;
    }
    say(`  毒已落地 sha ${before.slice(0, 12)} → ${after.slice(0, 12)}`);

    let caught = true;
    for (const [name, gate] of [['build', GATE_BUILD], ['browser', GATE_BROWSER]]) {
        const want = p.expectRed[name] || [];
        if (!want.length) continue;
        if (name === 'browser' && BUILD_ONLY) { say(`  browser: 跳过(--build-only)`); continue; }
        const r = run(gate);
        const newRed = [...r.redSet].filter((x) => !baseline[name].has(x));
        const hit = want.filter((w) => r.redSet.has(w));
        const missed = want.filter((w) => !r.redSet.has(w));
        const okArm = missed.length === 0 && r.rc !== 0;
        if (!okArm) caught = false;
        say(`  ${okArm ? 'OK  ' : 'FAIL'} ${name} rc=${r.rc} 期望红=[${want.join(',')}] `
            + `实际抓住=[${hit.join(',')}]${missed.length ? ` 🔴 没红=[${missed.join(',')}]` : ''}`);
        say(`       新增报红共 ${newRed.length} 条:${newRed.join(',') || '(无)'}`);
        // 钝杀检查:判据整个崩了(一条 OK 都没打印)也会 rc!=0,那不算抓住
        const okCount = (r.out.match(/^\s*OK/gm) || []).length;
        if (okCount < 5) {
            say(`       🔴 钝杀嫌疑:这一轮只打印了 ${okCount} 条 OK —— 判据可能是崩了而不是抓住了。`);
            caught = false;
        }
    }
    if (!caught) failures += 1;

    // ══ 还原:字节拷回 + 三重复核 ═══════════════════════════════════
    copyFileSync(bak, target);
    unlinkSync(bak);
    const restored = sha(target);
    const sameSha = restored === before;
    /**
     * 🔴 承重判据是 **sha 与「下毒前那份」相等**,不是 `git diff --quiet`。
     *    上一版拿 git diff 当还原判据,两头都错(同一个错参照物,两个方向各骗一次):
     *    · 对 SelectionPage.tsx(本单已改、未提交)它恒报"有残留" —— 假警报,
     *      因为它比的是 **HEAD**,而我要比的是「下毒前」;
     *    · 对 allExcludedExits.ts(新文件、untracked)它恒报"干净" —— 假安心,
     *      真没还原成功也照样绿。
     *    所以 git 那行降级为旁证,判据看 sha + 毒词残留。
     */
    let clean = false;
    try {
        execFileSync('git', ['diff', '--quiet', '--', join('frontend', p.file).split('\\').join('/')],
            { cwd: REPO, stdio: 'pipe' });
        clean = true;
    } catch { clean = false; }
    /**
     * 还原的正向判据:**原文那一段回来了,且恰好一处**。
     *
     * 🔴 上一版查的是「毒词不许残留」(`includes(p.to)`),对**删除型**的毒必然误报:
     *    P2 的 `to` 是把 `{allExcludedCard}` 那行删掉后剩下的 SelectionHeader 那行 ——
     *    它本来就是原文的子串,还原成功之后当然还在。查"坏的没了"不如查"好的回来了"。
     */
    const restoredSrc = readFileSync(target, 'utf8');
    const backHits = restoredSrc.split(p.from).length - 1;
    const residue = backHits !== 1;
    say(`  ${sameSha && !residue ? 'OK  ' : 'FAIL'} 还原:sha vs 下毒前 ${sameSha ? '一致' : '🔴 不一致'}`
        + ` · 原文回位 ${residue ? `🔴 否(命中 ${backHits} 处，要 1)` : '是'}`
        + ` · (旁证 git vs HEAD:${clean ? '无差异' : '有差异 —— 本单未提交的改动,预期如此'})`);
    if (!sameSha || residue) { failures += 1; }
}

say('');
if (failures > 0) {
    say(`FAIL 注毒自证 ${failures} 项不过`);
    process.exit(1);
}
say(`全部通过:${POISONS.length} 发毒,每发都红在写死的那一格,且逐发字节还原`
    + (BUILD_ONLY ? '(--build-only:浏览器臂的牙本轮未证)' : ''));
process.exit(0);
