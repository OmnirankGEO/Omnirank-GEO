#!/usr/bin/env node
/**
 * 判据 · #143(提交体漏带 price_preview_id / 输入即计价 / 0 题可启动)+ #144(候选题零接线)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 本判据**尽量打行为,不打文本**。Node 24 能直接 import .ts,
 *    所以纯逻辑模块(题源 SSOT / 启动闸 / 候选 API)一律真调函数。
 *    只有「那行代码在不在某个分支里」这种问题没法靠调用回答,才落到结构上 ——
 *    而那一条正是上一把锁栽的地方,见下面 A 段。
 */
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

/**
 * 🔴 [2026-09-07 停车返修] 加载 .ts 判据模块的**唯一**入口。
 *
 * 上一版直接 `await import('../src/….ts')`。那在**我本地 Node 24** 上能跑
 * (它原生 strip types),而生产构建镜像是 **node:20-alpine**(Dockerfile:15),
 * Node 20 不支持 ⇒ catch 成 null ⇒ 三个行为锁全部退化成「未评估」⇒ exit 3 ⇒ 0907b 停车。
 *
 * 🔴 **我在一个平台上验的绿,拿到另一个平台上一条行为锁都没跑。**
 *    (exitCode 那笔改对了:它诚实地拒绝放行,否则这三条会以「未评估」的形状
 *     悄悄躺在绿色构建里 —— 停车是这条链上唯一让问题暴露的动作。)
 *
 * 用 `typescript.transpileModule` 而不是 esbuild:
 *   · typescript 是**纯 JS**(devDependency,`tsc -b` 本来就在构建链上)——
 *     **没有平台二进制**,所以 Windows 树挂进 Linux 容器也能跑,自证不需要重装依赖;
 *   · esbuild 虽然零新依赖,但它是 per-platform native binary
 *     (本树只有 `@esbuild/win32-x64`)⇒ 自证会炸在与被测问题无关的地方。
 * 转出的代码走 data: URL 动态 import,**不落任何临时文件**。
 */
const require_ = createRequire(import.meta.url);
function loadTs(relPath) {
    try {
        const ts = require_('typescript');
        const src = readFileSync(join(ROOT, relPath), 'utf8');
        const out = ts.transpileModule(src, {
            compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
            fileName: relPath,
        }).outputText;
        // 🔴 data: URL 模块**解析不了相对 import**。本判据加载的模块都必须是零**运行时**依赖;
        //    哪天有人给它们加了真的相对 import,这里要**明确报错**,而不是抛一个看不懂的解析错。
        //
        // 🔴 [#233-a1 2026-09-17] 这道检查原来查**源码**,于是把 `import type` 也拦了 ——
        //    而 `import type` 转译后是被**完全擦除**的,运行时根本不存在。
        //    量错了对象:该问的是「跑起来会不会去解析相对路径」,不是「源码里写没写过」。
        //    改查**转译产物**,更准,不是放宽:真的运行时相对 import 仍然会被拦下。
        if (/from\s+['"]\.{1,2}\//.test(out)) {
            return { ok: false, err: `${relPath} 转译后仍有**运行时**相对 import —— data: URL 加载器解析不了。`
                + '把它拆成零依赖纯逻辑(类型用 `import type`,转译后会被擦除),或改用真实文件路径加载。' };
        }
        return { ok: true, url: 'data:text/javascript;base64,' + Buffer.from(out, 'utf8').toString('base64') };
    } catch (e) {
        return { ok: false, err: String(e && e.message || e) };
    }
}
async function importTs(relPath) {
    const r = loadTs(relPath);
    if (!r.ok) return { ok: false, err: r.err };
    try { return { ok: true, mod: await import(r.url) }; }
    catch (e) { return { ok: false, err: String(e && e.message || e) }; }
}

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

// ══ M · 元判据:本环境到底能不能加载 .ts 判据模块 ═══════════════════════
//
// 🔴 必须**排在所有业务锁之前**,而且不支持时是 **exit 1**,不是让三个锁各自 SKIP。
//    上一版就是各自 SKIP ⇒ 读数是「三项未评估」,看起来像环境缺件(可容忍),
//    实际是**这个环境下一条行为锁都跑不了**(不可容忍)。
//    「一项没验」和「整类没验」在 SKIP 的形状下完全同形。
{
    const probe = 'export const answer: number = 42; export type T = string;';
    let capable = false;
    let why = '';
    try {
        const ts = require_('typescript');
        const out = ts.transpileModule(probe, {
            compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
        }).outputText;
        const m = await import('data:text/javascript;base64,' + Buffer.from(out, 'utf8').toString('base64'));
        capable = m.answer === 42;
        if (!capable) why = 'transpile 通过但取到的值不对:' + String(m.answer);
    } catch (e) {
        why = String(e && e.message || e);
    }
    if (!capable) {
        console.log('  FAIL M0 🔴 **本环境无法加载 .ts 判据模块**,三条行为锁一条都跑不了。');
        console.log(`       原因:${why}`);
        console.log('       修法:确认 devDependency `typescript` 已安装(本判据用它的 transpileModule,');
        console.log('       纯 JS、无平台二进制)。生产构建镜像是 node:20-alpine(Dockerfile:15),');
        console.log('       **不要**改回直接 import(".ts") —— 那只在 Node 22+ 能跑。');
        console.log('       这条**不是**未评估,是失败:环境不具备就不许放行。');
        process.exitCode = 1;
        process.exit(1);
    }
    console.log(`  OK  M1 元判据:.ts 判据模块可加载(typescript ${require_('typescript').version} · 纯 JS 转译 + data URL)`);
}

const PAGE = strip(rd('src/pages/Diagnosis/NewDiagnosis.tsx'));

// ══ A · #143① price_preview_id 必须无条件带上 ═══════════════════════════
//
// 🔴 上一把锁(verify-pkg1-server-price.mjs C1)判的是「这一行**存在**」。
//    它确实存在 —— 只是躺在 `if (自定义题数 > 0)` 里,0 题时根本不执行,
//    而服务端无条件要它 ⇒ 增长模式 0 题必 400。**存在 ≠ 可达。**
//    所以这里判的是「它**不在**那个条件块的块体内」。
console.log('A #143① 提交体无条件带 price_preview_id');

/** 取 `if (<cond>) { ... }` 的**块体**(大括号配对)。取不到返 null —— 调用点必须当红处理。 */
function ifBlockBody(src, condSnippet) {
    const k = src.indexOf(condSnippet);
    if (k < 0) return null;
    const open = src.indexOf('{', k);
    if (open < 0) return null;
    let depth = 0;
    for (let i = open; i < src.length; i += 1) {
        if (src[i] === '{') depth += 1;
        else if (src[i] === '}') { depth -= 1; if (depth === 0) return src.slice(open + 1, i); }
    }
    return null;
}

const ASSIGN = 'payload.price_preview_id =';
const assignCount = (PAGE.match(/payload\.price_preview_id =/g) || []).length;
ok(assignCount === 1, 'A1 提交体里 price_preview_id 只赋值一处', `实得 ${assignCount} 处`);

const COND = 'if (ownQuestions.unique.length > 0)';
const body = ifBlockBody(PAGE, COND);
if (body === null) {
    bad += 1;
    console.log(`  FAIL A0 取不到 \`${COND}\` 的块体 —— 锚失效,A2/A3 不算通过`);
} else {
    ok(!body.includes(ASSIGN),
        'A2 🔴 赋值**不在**「自定义题数 > 0」的块体内(在里面 ⇒ 0 题时必 400)');
    ok(body.includes('payload.custom_questions'),
        'A2b 正样本臂:块体确实取到了(里面还有 custom_questions),否则 A2 只是块体为空');
    const iAssign = PAGE.indexOf(ASSIGN);
    const iCond = PAGE.indexOf(COND);
    ok(iAssign >= 0 && iCond >= 0 && iAssign < iCond,
        'A3 赋值在条件块**之前** ⇒ 任何题数都会执行');
}
// 反向对照:把「在块内」的样子喂给同一个提取器,它必须认得出来 —— 否则 A2 恒真。
{
    const synthetic = `if (ownQuestions.unique.length > 0) {\n  payload.price_preview_id = x;\n}`;
    const synBody = ifBlockBody(synthetic, COND);
    ok(synBody !== null && synBody.includes(ASSIGN),
        'A4 反向对照:合成的「赋值在块内」必须被识别(不识别 ⇒ A2 是空断言)');
}

// ══ B · #143③ 提交才计数(行为) ════════════════════════════════════════
console.log('B #143③ 输入中不计数、提交后才计数');
const oqR = await importTs('src/pages/Diagnosis/launch/ownQuestions.ts');
if (!oqR.ok) {
    bad += 1;
    console.log('  FAIL B0 加载 ownQuestions.ts 失败 —— 行为锁没跑,这是**失败**不是未评估:' + oqR.err);
} else {
    const oq = oqR.mod;
    const draftRow = { text: '客户会问什么', draft: true };
    const doneRow = { text: '客户会问什么', draft: undefined };
    ok(oq.collectOwnQuestions([draftRow]).unique.length === 0,
        'B1 🔴 还在打字的行(draft)**不计数** —— 计数触发时机是提交,不是按键');
    ok(oq.collectOwnQuestions([doneRow]).unique.length === 1,
        'B2 正样本臂:提交后的同一行计数(否则 B1 可能只是因为这一行本来就不算)');
    ok(oq.collectOwnQuestions([{ text: '老数据没有 draft 字段' }]).unique.length === 1,
        'B3 🔴 缺省 = 已提交:历史数据(记录回填 / AI 候选)一个字不改仍然计数');
    ok(oq.isQuestionCommittable('s') === false
        && oq.isQuestionCommittable('贵吗') === true
        && oq.isQuestionCommittable('x'.repeat(oq.CUSTOM_QUESTION_MAX_CHARS + 1)) === false,
        'B4 提交门:单字符不行 / 两字中文可以 / 超长不行');
}
{
    const ed = strip(rd('src/components/defensiveGeo/QuestionPlanEditor.tsx'));
    ok(/draft: true/.test(ed), 'B5 编辑器新建行是草稿');
    ok(/onKeyDown=/.test(ed) && /onBlur=/.test(ed) && /commitManual\(/.test(ed),
        'B6 三条提交路都在:回车 / 失焦 / 「加入」按钮');
    const onChangeIdx = ed.indexOf('onChange={(e) => {');
    ok(onChangeIdx >= 0 && !ed.slice(onChangeIdx, onChangeIdx + 400).includes('draft: undefined'),
        'B7 🔴 反臂:onChange 里**不许**清 draft —— 清了就等于又变回「打字即计数」');
}

// ══ C · #143④ 0 题不许启动(行为) ═════════════════════════════════════
console.log('C #143④ 0 道题的体检不许启动');
const gateR = await importTs('src/pages/Diagnosis/launch/defensiveLaunchGate.ts');
if (!gateR.ok) {
    bad += 1;
    console.log('  FAIL C0 加载 defensiveLaunchGate.ts 失败 —— 行为锁没跑,这是**失败**不是未评估:' + gateR.err);
} else {
    const gate = gateR.mod;
    const base = {
        mode: 'offensive', isFormComplete: true, totalDiagnosisCost: 650,
        defensiveQuestionCount: 0, hasBrandId: true, loading: false,
        planPending: false, priceNotReady: false,
    };
    /**
     * 🔴 [#143④ **撤回** 2026-09-07] 上一版这里锁的是「0 道题必须拦住」。
     *    那条闸是个回归:`workflows/diagnosis_workflow.py:881` 明写
     *    「custom 为空: 总是走 system 8 题」—— **「0 道题的体检」不存在**,
     *    0 道自定义题是**最常见的默认流程**。闸会拦住每一个不填题的用户。
     *    锁**方向翻过来**:0 道自定义题**必须能启动**。
     */
    ok(gate.isLaunchBlocked({ ...base }) === false,
        'C1a 🔴 0 道自定义题**可以**启动 —— 后端会走 system 题,这是默认流程不是缺项');
    /**
     * 🔴 C1b 是注毒**逼出来**的:我第一版只有 C1a,而 `base` 里根本没有题数那一格。
     *    把闸放回去(判 `=== 0` 才拦)时 C1a **仍然绿** —— 因为它传的是 undefined,
     *    而真正出事的情形是页面传了**实值 0**。
     *    「不传」和「传 0」是两格,我只测了不会出事的那一格。
     */
    ok(gate.isLaunchBlocked({ ...base, effectiveQuestionCount: 0 }) === false,
        'C1b 🔴 **即使调用方传了 0**,也不许拦 —— 这条才咬得住"把闸放回去"');
    ok(gate.isLaunchBlocked({ ...base, loading: true }) === true,
        'C2 正样本臂:闸本身有效(否则 C1 的"没拦"可能只是这个函数恒 false)');
    ok(!/effectiveQuestionCount/.test(rd('src/pages/Diagnosis/launch/defensiveLaunchGate.ts')
        .replace(/\/\*[\s\S]*?\*\//g, '')),
        'C3 🔴 那条闸的**入参与判定都已从代码里去掉**(只在注释里留了撤回原因)');
    ok(gate.isLaunchBlocked({ ...base, mode: 'defensive', defensiveQuestionCount: 0 }) === true
        && gate.isLaunchBlocked({ ...base, mode: 'defensive', defensiveQuestionCount: 1 }) === false,
        'C4 防守/混合流仍要求 ≥1 道(它没有 system 兜底那条路)');
    // #143② —— 已成立的行为,这里锁住它,不假装修过
    ok(gate.isLaunchBlocked({ ...base, effectiveQuestionCount: 1, priceNotReady: true }) === true,
        'C5 [#143②] 预览没就绪 ⇒ 按钮被拦(本笔未改代码,只锁住)');
}
{
    const iErr = PAGE.indexOf('if (priceError) return priceError;');
    const iNot = PAGE.indexOf("if (priceNotReady) return '正在按你现在的题单重新算价");
    ok(iErr >= 0 && iNot >= 0 && iErr < iNot,
        'C6 [#143②] 失败原文排在「正在算价」**之前** ⇒ 预览失败时说的是失败原因,不是"正在算"');
    /**
     * 🔴 [撤回订正] 上一版我把「题可以不填,系统会替你出 8 道」当成假承诺删了 ——
     *    **它是真的**(`diagnosis_workflow.py:881`)。锁**方向翻过来**:这句说明必须在,
     *    否则不填题的用户会以为自己漏了一步。
     *    🔴 但数字写「最多 8 道」不写「8 道」:`real_questions` 是 `[:8]`,
     *    兜底那条硬编码只有 5 条 ⇒ 写死 8 会变成同一类看起来精确的假话。
     */
    ok(PAGE.includes('题可以不填,系统会按你的行业自动出题'),
        'C7 🔴 那句**真实**说明必须在(不填题时系统会出题,这是默认流程)');
    ok(!/系统会替你出 8 道/.test(PAGE) && /最多 8 道/.test(PAGE),
        'C7b 数字是「最多 8 道」——`[:8]` 且兜底只有 5 条,写死 8 是另一个假承诺');
    /**
     * 🔴 [#165 F11 换锚 · 不换意图] 原锚是整个对象字面量的**形状**
     *    (`… optional: false }` 逐字),#165 给这一项加了个纯显示用的 `okLabel`
     *    (0 道时说「等 AI 出题」而不是「已填」),`ok` 的判据**一字未动**,
     *    形状锁却红了。锁形状本来就比锁不变式弱:把 `ok` 换成别的等价写法它也会红,
     *    而把判据改成 `>= 1` 只要形状还对它就不红。
     *    现在钉**谓词本身 + 明确禁止的反面**:空题集必须仍然合法。
     */
    ok(/\{ label: '题单', ok: ownQuestions\.tooLong\.length === 0,/.test(PAGE),
        'C8a 🔴 侧栏「题单」的判据仍是「没有超长题」,不是「有几道」');
    ok(!/label: '题单'[\s\S]{0,500}?(effectiveRunCount|liveQuestionCount|ownQuestionCount|counts\.total)\s*>=\s*1/.test(PAGE),
        'C8b 🔴 反面:不许要求 ≥1 —— 空题集本来就合法(diagnosis_workflow.py:881)');
}

// ══ D · #144 候选题(行为 · mock fetch) ═══════════════════════════════
console.log('D #144 候选题状态空间 {成功/空/异常/无品牌/401/网络} × {growth/full/defensive}');
const sqR = await importTs('src/pages/Diagnosis/launch/suggestQuestionsApi.ts');
if (!sqR.ok) {
    bad += 1;
    console.log('  FAIL D0 加载 suggestQuestionsApi.ts 失败 —— 行为锁没跑,这是**失败**不是未评估:' + sqR.err);
} else {
    const sq = sqR.mod;
    ok(sq.toSuggestMode('offensive') === 'growth'
        && sq.toSuggestMode('hybrid') === 'full'
        && sq.toSuggestMode('defensive') === null,
        'D1 🔴 模式映射;**防守返 null = 不调接口**(工单:防守保留品牌名模板)');

    let calls = 0;
    const mk = (status, body) => async () => {
        calls += 1;
        return { status, ok: status >= 200 && status < 300, json: async () => body };
    };
    const run = (fetcher, brandId = 7) =>
        sq.fetchSuggestedQuestions(fetcher, { brandId, mode: 'growth', businessScope: '' });

    const r1 = await run(mk(200, { questions: ['客户会问 A', '客户会问 B'], source: 'ai', fallbackReason: null }));
    ok(r1.ok === true && r1.data.questions.length === 2, 'D2 成功 ⇒ 拿到候选');
    ok(calls === 1, 'D2b 正样本臂:真的发了请求(否则下面的失败格全是"根本没调"的假绿)');

    const r2 = await run(mk(200, { questions: [], source: 'fallback', fallbackReason: 'exception' }));
    ok(r2.ok === false && r2.error.kind === 'exception',
        'D3 🔴 空 + exception ⇒ 记 exception(不压成通用失败:该查网络还是查 prompt 是两种处置)');

    const r3 = await run(mk(200, { questions: [], source: 'fallback', fallbackReason: 'empty' }));
    ok(r3.ok === false && r3.error.kind === 'empty', 'D4 空 + empty ⇒ 记 empty');

    const before = calls;
    const r4 = await run(mk(200, {}), null);
    ok(r4.ok === false && r4.error.kind === 'no_brand' && calls === before,
        'D5 🔴 没有 brand_id ⇒ **不发请求**,且这一格不是"失败"是"还差一步"');
    ok(!/失败|错误|出错/.test(r4.error.message),
        'D5b 无品牌那句话不能说成报错', r4.error.message);

    const r5 = await run(mk(401, {}));
    ok(r5.ok === false && r5.error.kind === 'unauthorized', 'D6 401 ⇒ unauthorized');
    const r6 = await run(mk(500, {}));
    ok(r6.ok === false && r6.error.kind === 'network', 'D7 5xx ⇒ network');
    const r7 = await run(async () => { throw new Error('boom'); });
    ok(r7.ok === false && r7.error.kind === 'network', 'D8 抛异常 ⇒ network(不外泄异常)');
}

// ══ E · 页面接线(#144 真的被调用了) ═══════════════════════════════════
console.log('E #144 接线:端点从"零调用方"变成有调用方');
ok(/import \{ fetchSuggestedQuestions, toSuggestMode/.test(PAGE), 'E1 页面 import 了候选模块');
ok(/fetchSuggestedQuestions\(authFetch,/.test(PAGE), 'E2 🔴 真的调用了,并注入 authFetch(带登录态)');
// 🔴 [#149] 原 E3「候选进的是示例卡」**已被 F3/F4 取代**(方向相反且更细)。
//    留着就是同一谓词两处,必有一处没人维护 —— 删,不并存。

ok(/suggestTriedRef/.test(PAGE),
    'E4 每个(品牌 × 模式)只取一次 —— 否则预填改 manualQuestions ⇒ effect 自己喂自己');
ok(/data-testid="suggest-questions-notice"/.test(PAGE) && /data-testid="suggest-questions-pending"/.test(PAGE),
    'E5 候选取不到时屏幕上有话说(空题单与"接口挂了"不许同形)');

// ══ F · #149 候选直接进题单(2026-09-08) ═══════════════════════════════
//
// 🔴 本段**替换**了 09-07 那段「候选是示例,不是"系统将问"」。
//    那段的前提是 #147:预览题与实跑题输入不同源。
//    `13865e440`(#147-B,0907c)把两侧输入合成同一份 ⇒ 前提没了。
//    Owner 原话:「为什么非要在下面添加个示例,直接填充到上面这个框不行吗?」
//    示例卡与 `searchQuestionExamples.ts` 一并退役(L3 与 L5 M7/M9/M10 按裁定退役)。
console.log('F #149 候选进题单 · 题单整体即运行集');
{
    ok(!existsSync(join(ROOT, 'src/pages/Diagnosis/launch/QuestionExampleCard.tsx'))
        && !existsSync(join(ROOT, 'src/pages/Diagnosis/launch/searchQuestionExamples.ts')),
        'F1 🔴 示例卡与其模块已删');
    ok(!/QuestionExampleCard/.test(PAGE) && !/buildQuestionExamples/.test(PAGE),
        'F2 页面不再引用它们(死导入也清了)');
    ok(/setAiSuggested\(r\.data\.candidates\.map/.test(PAGE),
        'F3 🔴 候选进的是 **suggested 半区**');
    ok(!/setManualQuestions\(r\.data/.test(PAGE),
        'F4 🔴 候选**不进**她自己的题单 —— 那会让后端从 mode_default 静默切到 mode_verbatim');
    ok(/const planQuestions: DraftQuestion\[\] = isDefensiveFlow/.test(PAGE)
        && /collectOwnQuestions\(planQuestions\)/.test(PAGE),
        'F5 🔴 题单整体 = 运行集:取价/计数/提交体读**同一个**派生变量');
    ok(/payload\.question_meta = buildQuestionMeta\(planQuestions, ownQuestions\.unique\)/.test(PAGE),
        'F6 🔴 提交体带 question_meta,且**由提交那份 unique 派生**(不第二次归一化 ⇒ 不会 422)');
    ok(/payload\.ai_optimize_custom = isDefensiveFlow \? formData\.aiOptimizeCustom : false/.test(PAGE),
        'F7 增长线 ai_optimize_custom 恒 false');
    ok(/\{isDefensiveFlow && customQuestionCount > 0 && \(/.test(PAGE),
        'F8 增长线不显示双轨开关(AI 的题已在题单里,开关没有对象)');
    ok(/input_params\?\.question_meta/.test(PAGE) && /meta\?\.origin === 'ai_suggested' \? 'ai' : 'user'/.test(PAGE),
        'F9 复测按 question_meta 还原两半;缺 meta 的老记录全按 customer');
    ok(/: ownQuestionCount;/.test(PAGE) && /isDefensiveFlow && aiQuestionsMuted/.test(PAGE),
        'F10 增长线不折叠不静音;实跑数 = 题单条数(不双计 suggested)');
    /**
     * 🔴 [返修 · Review 注毒 Pd] **显示源必须与计价源同出一处**。
     *
     * Pd:把 `planQuestions` 增长分支改成 `[...manualQuestions]`(丢掉 AI 那半)——
     * 我原来的判据**全部照绿**。那时编辑器仍按 `suggested` 显示 AI 那半,
     * 而计价/计数/提交体都不含它 ⇒ **屏幕上 N 道、按 M 道收钱**。
     * 我在注释里点名要防这一格,锁的却只是「planQuestions 长这个样子」。
     * ⇒ 两侧都必须走 `growthRunSet`/`growthSuggested` 这**同一对**标识符。
     */
    ok(/: growthRunSet\(growthSuggested, manualQuestions\)/.test(PAGE),
        'F11 🔴 计价源 = growthRunSet(growthSuggested, manualQuestions)');
    ok(/suggested=\{isDefensiveFlow \? suggestedWithFiller : growthSuggested\}/.test(PAGE),
        'F12 🔴 显示源(编辑器 suggested prop)增长分支**就是** growthSuggested —— 与计价源同一份');
    ok(!/: \[\.\.\.growthSuggested, \.\.\.manualQuestions\]/.test(PAGE),
        'F13 反臂:不许绕开构造器手写展开(手写的那份不会被 F11 咬住)');
}

// ══ G · #149 纯逻辑行为(状态空间 · 真调函数) ═════════════════════════
console.log('G #149 题面元数据 {全 AI / AI+自己 / 只自己 / 空} × {有 layer / 无 layer}');
{
    const sqR2 = await importTs('src/pages/Diagnosis/launch/suggestQuestionsApi.ts');
    if (!sqR2.ok) {
        bad += 1;
        console.log('  FAIL G0 加载 suggestQuestionsApi.ts 失败 —— 这是失败不是未评估:' + sqR2.err);
    } else {
        const m = sqR2.mod;
        ok(m.sideFromBackend('growth') === 'offensive' && m.sideFromBackend('defensive') === 'defensive'
            && m.sideFromBackend('nonsense') === null && m.sideToBackend('offensive') === 'growth'
            && m.sideToBackend('defensive') === 'defensive' && m.sideToBackend('nonsense') === null,
            'G1 🔴 侧别两套词的映射**只此一处**,两个方向都对,不认识的值给 null 不瞎猜');
        const rows = [
            { text: 'AI 出的一道', source: 'ai', modeSide: 'offensive', layer: 'brand_awareness' },
            { text: '她自己写的', source: 'user', modeSide: 'offensive' },
            { text: '草稿不该进', source: 'user', modeSide: 'offensive', draft: true },
        ];
        const allAi = m.buildQuestionMeta([rows[0]], ['AI 出的一道']);
        ok(allAi.length === 1 && allAi[0].origin === 'ai_suggested'
            && allAi[0].side === 'growth' && allAi[0].layer === 'brand_awareness',
            'G2 全 AI + 有 layer ⇒ origin=ai_suggested · side 已转后端词 · layer 原样带上');
        const mixed = m.buildQuestionMeta(rows, ['AI 出的一道', '她自己写的']);
        ok(mixed.length === 2 && mixed[0].origin === 'ai_suggested' && mixed[1].origin === 'customer',
            'G3 AI + 自己 ⇒ 两种 origin 各归各位');
        ok(mixed[1].layer === undefined,
            'G4 无 layer 的题**不带** layer 字段(后端据「有没有层」决定要不要再花 LLM 归层)');
        ok(m.buildQuestionMeta(rows, []).length === 0, 'G5 空题单 ⇒ 空元数据');
        // 🔴 承重约束:text 必须 ⊆ custom_questions,多一条就 422
        const metaTexts = new Set(mixed.map((x) => x.text));
        ok(metaTexts.size === 2 && [...metaTexts].every((t) => ['AI 出的一道', '她自己写的'].includes(t)),
            'G6 🔴 question_meta.text ⊆ custom_questions(多一条后端就 422 且明说没扣算力)');
        ok(!m.buildQuestionMeta(rows, ['AI 出的一道', '她自己写的']).some((x) => x.text === '草稿不该进'),
            'G7 草稿行不进元数据');
        ok(m.buildQuestionMeta([{ text: '未知来源', source: 'system', modeSide: 'defensive' }], ['未知来源'])[0].origin
            === 'ai_suggested',
            'G8 正样本臂:非 user 的来源一律 ai_suggested(不是只认 ai)');
        // layer 是自由字符串:前端不许把它当枚举校验
        const weird = m.buildQuestionMeta(
            [{ text: 'x', source: 'ai', modeSide: 'offensive', layer: '生成器明天新增的一层' }], ['x']);
        // 🔴 [返修] 行为臂:N 条 AI + M 条自己 ⇒ 运行集必须是 N+M 条且**顺序固定**。
        //    形状锁挡不住 Pd(丢掉一半仍然"长得对"),数量/顺序锁挡得住。
        {
            const ai = [{ text: 'a1' }, { text: 'a2' }, { text: 'a3' }];
            const mine = [{ text: 'm1' }, { text: 'm2' }];
            const run = m.growthRunSet(ai, mine);
            ok(run.length === 5, 'G10 🔴 3 条 AI + 2 条自己 ⇒ 运行集 5 条(少一条就是"显示 5 收 2")',
                `实得 ${run.length}`);
            ok(run[0].text === 'a1' && run[3].text === 'm1',
                'G11 顺序固定:AI 那半在前、她自己写的在后');
            ok(m.growthRunSet(null, mine).length === 2 && m.growthRunSet(ai, null).length === 3
                && m.growthRunSet(null, null).length === 0,
                'G12 两侧任一为空都不崩(空题单是合法状态)');
            const meta = m.buildQuestionMeta(
                run.map((q, i) => ({ ...q, source: i < 3 ? 'ai' : 'user', modeSide: 'offensive' })),
                run.map((q) => q.text));
            ok(meta.length === 5 && meta.filter((x) => x.origin === 'ai_suggested').length === 3,
                'G13 🔴 运行集条数 == 提交体元数据条数 == 3 AI + 2 customer(三个数必须同时对上)');
        }
        ok(weird[0].layer === '生成器明天新增的一层',
            'G9 🔴 layer 是**自由字符串**:陌生取值原样带上,不校验、不丢弃(写死枚举会整页 500)');
    }
}

// ══ Q · #233-a1 增长侧题面必须带行业词/地域词,取不到行业词就不发 ═════════
//
// 🔴 生产实证:增长侧两题原来是**写死的空字符串**(「这一类服务哪家做得好?」
//    「有什么推荐的吗?」),不含任何变量 ⇒ AI 回答的是王者荣耀游戏推荐,
//    系统据此记「未被提及」并按 40% 权重扣分,而我们还为这道没有主语的题
//    向 4 个付费引擎各调了一次。空题既花钱又扣分。
//
// 🔴 这一组**真调函数**,不做文本匹配:文本匹配对「插值被删掉」天生没分辨力
//    (模板串还在,变量没了,grep 照样命中)。为此把纯逻辑抽成了零运行时依赖的
//    `questionSuggest.ts` —— 判据看得见它要防的那件事,才算判据。
{
    console.log('');
    console.log('Q #233-a1 建议题单:增长侧要带行业词,没有行业词就不发');
    const r = await importTs('src/components/defensiveGeo/questionSuggest.ts');
    if (!r.ok) {
        ok(false, 'Q0 🔴 取不到 `questionSuggest.ts` —— 本组一条都没跑成', r.err.slice(0, 160));
    } else {
        const { suggestedQuestions, removalKey } = r.mod;
        const CTX = { industry: '宠物医院', location: '深圳', businessScope: 'regional' };
        const off = suggestedQuestions('offensive', '瑞恩宠物', CTX);

        ok(off.length === 2, 'Q0 分母自证:有行业词时增长侧确实出了 2 道题(出 0 道的话下面每一格都真空通过)',
            `${off.length} 道`);
        ok(off.length === 2 && off.every((q) => q.text.includes('宠物医院')),
            'Q1 🔴 每一道增长题都含**行业词** —— 没有主语的题会被 AI 当成别的行业回答,'
            + '而我们按它的回答扣分',
            off.map((q) => q.text).join(' / '));
        ok(off.length === 2 && off.every((q) => q.text.includes('深圳')),
            'Q1b 🔴 区域生意的增长题含**地域词**(口径见 NewDiagnosis.tsx:403-406:'
            + '区域出「城市+行业」题、全国出行业大词)',
            off.map((q) => q.text).join(' / '));
        ok(!off.some((q) => q.text === '这一类服务哪家做得好?' || q.text === '有什么推荐的吗?'),
            'Q1c 🔴 那两句写死的空题**一个字都不许再出现**在输出里',
            off.map((q) => q.text).join(' / '));

        /* 🔴 反臂:没有行业词就一道都不发。宁可少发,不发空题。 */
        const noInd = suggestedQuestions('offensive', '瑞恩宠物', { location: '深圳' });
        ok(noInd.length === 0,
            'Q2 🔴 反臂:取不到行业词 ⇒ 增长侧**一道都不发**(空题既花钱又扣分,是两头亏)',
            noInd.map((q) => q.text).join(' / ') || '零道');
        const hyb = suggestedQuestions('hybrid', '瑞恩宠物', { location: '深圳' });
        ok(hyb.length === 3 && hyb.every((q) => q.modeSide === 'defensive'),
            'Q2b 反臂:hybrid 在没有行业词时只剩防御那三道(不是塞两道空题进去)',
            `${hyb.length} 道 · 侧别 ${[...new Set(hyb.map((q) => q.modeSide))].join(',')}`);

        /* 全国生意:仍要行业词,但不带城市 */
        const nat = suggestedQuestions('offensive', '瑞恩宠物',
            { industry: '宠物医院', location: '深圳', businessScope: 'national' });
        ok(nat.length === 2 && nat.every((q) => q.text.includes('宠物医院') && !q.text.includes('深圳')),
            'Q3 全国生意出行业大词,**不带城市**(带了就把全国盘子缩成一个城市)',
            nat.map((q) => q.text).join(' / '));

        /* 🔴 反向对照:防御侧三道**逐字不变**。本单只动增长侧。 */
        const def = suggestedQuestions('defensive', '瑞恩宠物', CTX);
        const DEF_EXPECT = ['瑞恩宠物靠谱吗?', '瑞恩宠物是做什么的?', '瑞恩宠物更适合哪些客户?'];
        ok(def.length === 3 && def.every((q, i) => q.text === DEF_EXPECT[i]),
            'Q4 反向对照:防御侧三道题**逐字不变** —— 本单只动增长侧,'
            + '防御侧跟着变了就说明我改宽了',
            def.map((q) => q.text).join(' / '));

        /* 🔴 题面变动态之后冒出来的新面:划掉的题不许因为题面变了而复活 */
        const k1 = removalKey(off[0]);
        const k2 = removalKey(suggestedQuestions('offensive', '瑞恩宠物',
            { industry: '宠物医院', location: '广州', businessScope: 'regional' })[0]);
        ok(k1 === k2,
            'Q5 🔴 系统题的「已划掉」键**不随题面变** —— 否则用户在没填行业时划掉的题,'
            + '填完行业就会复活(键是 `familyKey::text`,题面一变键就对不上)',
            `${k1} vs ${k2}`);
        ok(/^sys::/.test(k1) && !k1.includes('哪家做得好'),
            'Q5b 系统题的键里**不含题面**', k1);
        ok(removalKey({ familyKey: 'category_choice', text: 'AI 出的某道题', source: 'ai' })
            .includes('AI 出的某道题'),
            'Q5c 反臂:**AI 题的键仍连题面一起** —— 同一 family 下可能有好几道 AI 题,'
            + '只按 family 记会互相顶掉');
    }
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`\n未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('\n全部通过');
    process.exitCode = 0;
}
