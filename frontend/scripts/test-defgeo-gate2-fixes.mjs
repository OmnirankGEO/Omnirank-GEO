#!/usr/bin/env node
/**
 * 门二返修判据 · G2 / G4 / G5 —— 各配正反,G2 另配 legacy 字节等价回归。
 *
 * 为什么是 node 脚本而不是 pytest:这三条都是**前端**逻辑。
 * G2 的本体已抽到无 React 依赖的 `defensiveLaunchGate.ts`,可以直接 import 真跑;
 * G4/G5 打 `defensiveGeoApi.ts` 的行为(fetch 用 stub 拦),同样不需要浏览器。
 *
 * 🔴 判据不读源码串:读串只能证明"我写了那行字",证明不了"分支真的会那样走"。
 */

import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', 'src');
// 🔴 [门三修] 镜像里的 builder 是 node20,裸 import .ts 直接
//    ERR_UNKNOWN_FILE_EXTENSION(宿主机 node22+ 自带 strip-types 才碰巧能跑——
//    「本机跑通 ≠ 目标运行时」)。用 vite 自带的 esbuild 转译成临时 .mjs 再进,
//    两种 node 都真跑同一份源码,不读源码串。
import { createRequire } from 'node:module';
// esbuild 从 frontend/ 的依赖树解析(builder 里 npm ci 装的就是它;vite 的直接依赖)
const _require = createRequire(join(dirname(fileURLToPath(import.meta.url)), '..', 'package.json'));
const { transformSync } = _require('esbuild');
import { readFileSync, writeFileSync, mkdtempSync , mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
const load = async (rel) => {
    const src = join(SRC, rel);
    if (!rel.endsWith('.ts')) return import(pathToFileURL(src).href);
    const js = transformSync(readFileSync(src, 'utf8'), { loader: 'ts', format: 'esm' }).code;
    const out = join(mkdtempSync(join(tmpdir(), 'defgeo-gate2-')), 'mod.mjs');
    writeFileSync(out, js);
    return import(pathToFileURL(out).href);
};

let pass = 0, fail = 0;
const ok = (name) => { pass++; console.log(`  ✅ ${name}`); };
const bad = (name, e) => { fail++; console.log(`  🔴 ${name} — ${e && e.message}`); };
const t = async (name, fn) => { try { await fn(); ok(name); } catch (e) { bad(name, e); } };

// ═══════════════════════════════════════════════════════════════════
console.log('=== G2 · 启动按钮可用性(防御新规 + legacy 字节等价)===');
// ═══════════════════════════════════════════════════════════════════
const gate = await load('pages/Diagnosis/launch/defensiveLaunchGate.ts');
const { isLaunchBlocked, isDefensiveMode } = gate;

const base = {
    isFormComplete: false, totalDiagnosisCost: null,
    defensiveQuestionCount: 0, hasBrandId: false, loading: false,
};

for (const mode of ['defensive', 'hybrid']) {
    await t(`${mode}:题单≥1 且已选品牌 ⇒ 放行`, () => {
        assert.equal(isLaunchBlocked({ ...base, mode, defensiveQuestionCount: 1, hasBrandId: true }), false);
    });
    // 反向:两个条件各自缺一必须拦
    await t(`${mode}:题单为 0 ⇒ 拦`, () => {
        assert.equal(isLaunchBlocked({ ...base, mode, defensiveQuestionCount: 0, hasBrandId: true }), true);
    });
    // 🔴 [P0 2026-09-01 翻转] 这一条原来断言「未选客户品牌 ⇒ 拦」——
    //    **它把缺陷钉成了预期**。生产实测:防御/混合模式下手输新品牌名,按钮灰、
    //    屏幕零文案,而品牌下拉自己写着「无匹配品牌 · 继续输入将创建新品牌」——
    //    UI 已经许诺会建档,按钮却是死的。legacy 一直是「新品牌直接填,系统自动建档」。
    //    新契约:**放行**,点击时由 runDefensivePlanPreview 走现役建档原语
    //    (POST /api/my-clients → get_or_create_brand,与 legacy 同源)拿 brandId 再 preview。
    //    翻转而不是删除:删断言 = 净减覆盖。
    await t(`${mode}:未选客户品牌(手输新品牌名)⇒ 放行,点击时就地建档`, () => {
        assert.equal(isLaunchBlocked({ ...base, mode, defensiveQuestionCount: 3, hasBrandId: false }), false);
    });
    // 🔴 planPending:preview 在飞时必须拦(生产实测 9 秒连点 14 次的必要条件)。
    //    正反两臂 —— 只写"进行中拦"的话,把函数改成恒 true 也绿。
    await t(`${mode}:preview 进行中 ⇒ 拦`, () => {
        assert.equal(isLaunchBlocked({
            ...base, mode, defensiveQuestionCount: 2, hasBrandId: true, planPending: true,
        }), true);
    });
    await t(`${mode}:preview 结束后 ⇒ 放行`, () => {
        assert.equal(isLaunchBlocked({
            ...base, mode, defensiveQuestionCount: 2, hasBrandId: true, planPending: false,
        }), false);
    });
    // 🔴 关键正样本:legacy 的两个条件**都不满足**时,防御流仍应放行
    //    —— 这正是本次返修要解决的死锁(她填了题单却点不动按钮)。
    await t(`${mode}:legacy 表单不完整 + 价目未读到 ⇒ 仍放行`, () => {
        assert.equal(isLaunchBlocked({
            ...base, mode, defensiveQuestionCount: 2, hasBrandId: true,
            isFormComplete: false, totalDiagnosisCost: null,
        }), false);
    });
    await t(`${mode}:loading 期间仍拦(防双点)`, () => {
        assert.equal(isLaunchBlocked({
            ...base, mode, defensiveQuestionCount: 2, hasBrandId: true, loading: true,
        }), true);
    });
}

// ── legacy 字节等价回归:offensive 的结论必须与返修前表达式逐格相同 ──
// 返修前:disabled = loading || !isFormComplete || totalDiagnosisCost == null
const legacyBefore = (i) =>
    i.loading || !i.isFormComplete || i.totalDiagnosisCost == null;

await t('legacy(offensive):真值表逐格与返修前等价', () => {
    let checked = 0;
    for (const loading of [true, false])
    for (const isFormComplete of [true, false])
    for (const totalDiagnosisCost of [null, 0, 650])
    // 防御侧输入**任意取值**都不得影响 offensive 的结论
    for (const defensiveQuestionCount of [0, 5])
    for (const hasBrandId of [true, false]) {
        const input = {
            mode: 'offensive', loading, isFormComplete, totalDiagnosisCost,
            defensiveQuestionCount, hasBrandId,
        };
        assert.equal(isLaunchBlocked(input), legacyBefore(input),
            `offensive 漂移:${JSON.stringify(input)}`);
        checked++;
    }
    assert.equal(checked, 2 * 2 * 3 * 2 * 2);   // 48 格全覆盖
});

await t('offensive 不是防御流(分流键形态)', () => {
    assert.equal(isDefensiveMode('offensive'), false);
    assert.equal(isDefensiveMode('defensive'), true);
    assert.equal(isDefensiveMode('hybrid'), true);   // hybrid 不得被漏进 legacy
});

// ═══════════════════════════════════════════════════════════════════
console.log('=== G4 · createRunPreview 的 Idempotency-Key ===');
// ═══════════════════════════════════════════════════════════════════
const api = await load('lib/defensiveGeoApi.ts');

function stubFetch() {
    const calls = [];
    globalThis.fetch = async (url, init) => {
        calls.push({ url, headers: init?.headers ?? {} });
        return { ok: true, status: 200, text: async () => JSON.stringify({ previewId: 'p1' }) };
    };
    return calls;
}
globalThis.localStorage = { getItem: () => 'tok', setItem() {}, removeItem() {} };

await t('createRunPreview 传了 key ⇒ 请求头带 Idempotency-Key', async () => {
    const calls = stubFetch();
    await api.createRunPreview({
        questionPlanId: 'pl', questionPlanRevision: 1,
        profileRevisionId: 'pr', platformKeys: ['kimi'],
    }, 'defgeo-preview-key-1');
    assert.equal(calls.length, 1);
    assert.equal(calls[0].headers['Idempotency-Key'], 'defgeo-preview-key-1');
});

await t('反向:不传 key ⇒ 请求头不带该字段(不伪造)', async () => {
    const calls = stubFetch();
    await api.createRunPreview({
        questionPlanId: 'pl', questionPlanRevision: 1,
        profileRevisionId: 'pr', platformKeys: ['kimi'],
    });
    assert.equal('Idempotency-Key' in calls[0].headers, false);
});

await t('同一 key 重试两次 ⇒ 两次请求头逐字相同(跨重试稳定)', async () => {
    const calls = stubFetch();
    const k = 'defgeo-preview-key-stable';
    const body = { questionPlanId: 'pl', questionPlanRevision: 1, profileRevisionId: 'pr', platformKeys: ['kimi'] };
    await api.createRunPreview(body, k);
    await api.createRunPreview(body, k);
    assert.equal(calls[0].headers['Idempotency-Key'], calls[1].headers['Idempotency-Key']);
});

await t('confirm 与 preview 用不同 key ⇒ 两个命令不互相顶掉', async () => {
    const calls = stubFetch();
    await api.createRunPreview({
        questionPlanId: 'pl', questionPlanRevision: 1,
        profileRevisionId: 'pr', platformKeys: ['kimi'],
    }, 'key-preview');
    await api.confirmRunPreview('p1', 'h'.repeat(64), 'key-confirm');
    assert.notEqual(calls[0].headers['Idempotency-Key'], calls[1].headers['Idempotency-Key']);
});

// ═══════════════════════════════════════════════════════════════════
console.log('=== G5 · 错误永不静默(除 IDEMPOTENCY_CONFLICT)===');
// ═══════════════════════════════════════════════════════════════════
const { DefGeoError } = api;
const FALLBACK = '没能完成,请再试一次;没有扣除任何算力';

await t('无 publicExplanation ⇒ 不静默,渲染兜底人话', () => {
    const e = new DefGeoError(500, { code: 'SOME_NEW_CODE' });
    assert.equal(e.isSilent, false);
    assert.equal(e.userSentence, FALLBACK);
});

await t('IDEMPOTENCY_CONFLICT ⇒ 唯一静默项', () => {
    const e = new DefGeoError(409, { code: 'IDEMPOTENCY_CONFLICT' });
    assert.equal(e.isSilent, true);
});

await t('有 publicExplanation ⇒ 用服务端的话,不用兜底', () => {
    const e = new DefGeoError(409, {
        code: 'PREVIEW_EXPIRED',
        publicExplanation: '刚才那一步已过期,请重新发起;没有扣除任何算力',
    });
    assert.equal(e.isSilent, false);
    assert.notEqual(e.userSentence, FALLBACK);
    assert.match(e.userSentence, /没有扣除任何算力/);
});

await t('兜底句本身把钱说死(失败路径必须显式说钱)', () => {
    assert.match(FALLBACK, /没有扣除任何算力/);
    const e = new DefGeoError(500, { code: 'X' });
    assert.match(e.userSentence, /没有扣除任何算力/);
});

await t('userSentence 永不回落成 code(内部枚举裸串上屏 = 红)', () => {
    for (const code of ['INTERNAL_ERROR', 'POLICY_UNAVAILABLE', 'WEIRD_NEW_CODE']) {
        const e = new DefGeoError(500, { code });
        assert.equal(e.userSentence.includes(code), false, code);
    }
});

await t('反向:409 家族里除幂等冲突外都必须上屏', () => {
    for (const code of ['PREVIEW_EXPIRED', 'SNAPSHOT_CHANGED', 'QUESTION_PLAN_NOT_RUNNABLE']) {
        assert.equal(new DefGeoError(409, { code }).isSilent, false, code);
    }
});

// ═══════════════════════════════════════════════════════════════════
console.log('=== G2b · legacy 核心搜索问题只在 offensive 必填(接线锁)===');
// ═══════════════════════════════════════════════════════════════════
// 🔴 这一条是 **build 链里的接线锁**,不是本体判据。
//    本体是 DOM 级的 `scripts/test-defgeo-legacy-required.mjs`(真组件 + 真浏览器
//    + 真 form.checkValidity()/requestSubmit()),但那套要 chromium,按本仓惯例
//    不进 build 链(见 package.json 里 verify:publish-layout-interaction 那条注释)。
//    所以这里补一条**不需要浏览器**的 AST 锁,让"有人把它改回裸 required"
//    在每次构建时就红,而不是等到门阶段才发现。
//
//    判据打真 AST 不打源码串:`grep required` 会命中另外两格必填、
//    也会命中解释它的注释。
const ts = (await import('typescript')).default;
// [门四合流] readFileSync 已由顶部 node:fs import 提供(4e447112b loader 修),此处不再重复声明
const PAGE = join(SRC, 'pages/Diagnosis/NewDiagnosis.tsx');
const sf = ts.createSourceFile(
    PAGE, readFileSync(PAGE, 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);

const jsxEls = [];
(function walk(n) {
    if (ts.isJsxOpeningElement(n) || ts.isJsxSelfClosingElement(n)) jsxEls.push(n);
    n.forEachChild(walk);
})(sf);

const attrOf = (el, name) => el.attributes.properties.find(
    (a) => ts.isJsxAttribute(a) && a.name.getText() === name);
const idOf = (el) => {
    const a = attrOf(el, 'id');
    return a && a.initializer && ts.isStringLiteral(a.initializer) ? a.initializer.text : null;
};
const elById = (id) => jsxEls.find((e) => idOf(e) === id);

await t('AST 活性:三个被测控件都能在 AST 里定位到', () => {
    // [⑤b 2026-09-05] `keywords` 已从清单里去掉:那个控件被删了(题只剩题单编辑器一个入口)。
    for (const id of ['brandName', 'industry']) {
        assert.ok(elById(id), `AST 里找不到 #${id}`);
    }
});

/**
 * 🔴 [⑤b 2026-09-05] 原来这里锁的是 `#keywords` 的 required 必须是表达式。
 *    那个控件已被删(题只剩题单编辑器一个入口)⇒ **锁的前提被一次正确的改动推翻**,
 *    继续锁它会把对的代码判红。
 *
 *    但**意图**没变,而且比原来更该守:原生 `required` 会在 submit 事件**之前**
 *    拦掉校验,表现为「按钮能点、点了没反应」。所以换成**分母式继任锁** ——
 *    不再钉某一个 id,而是断言**页面上没有任何输入位带裸 required**。
 *    这比原锁强:新加一个裸 required 的控件也会红,而原锁只看那三个。
 */
/**
 * 🔴 **冻结豁免清单**:`#brandName` 的裸 required 是**既有设计**(本文件下方那段
 *    「反向对照从 #industry 换到 #brandName」讲的就是它:品牌名无条件必填是对的)。
 *    豁免必须显式冻结 + 写理由,不能把锁放宽成"至多一个" ——
 *    放宽之后再多出一个裸 required 就没人吭声了。
 */
const BARE_REQUIRED_EXEMPT = ['brandName'];

await t('继任锁:除冻结豁免外,没有任何输入位带**裸** required', () => {
    const bare = jsxEls.filter((e) => {
        const a = attrOf(e, 'required');
        return a && !a.initializer && !BARE_REQUIRED_EXEMPT.includes(idOf(e));
    });
    assert.equal(bare.length, 0,
        `有 ${bare.length} 个豁免清单外的控件带裸 required(它们会让「按钮点了没反应」回来)`);
});

await t('豁免非死条目:清单里每一项**确实**带着裸 required(否则豁免是空头,锁等于没设防)', () => {
    for (const id of BARE_REQUIRED_EXEMPT) {
        const a = attrOf(elById(id), 'required');
        assert.ok(a && !a.initializer,
            `#${id} 在豁免清单里,但它已经不是裸 required —— 该把它从清单里删掉`);
    }
});

await t('继任锁的正样本臂:AST 真的扫到了控件(扫空会让上一条恒真)', () => {
    assert.ok(jsxEls.length > 10, `AST 只扫到 ${jsxEls.length} 个 JSX 元素,疑似提取失败`);
});

/**
 * 🔴 [包H 2026-08-23] 反向对照**从 #industry 换到 #brandName**。
 *
 * 原文是「#industry 的 required 仍是裸属性(本次不该动它)」—— 那在 G2b 那一轮
 * 是对的。包H 拿到 Owner 2026-08-24 的口径:**防守/混合模式所属行业不强制**
 * (与 keywords 同一个坑:原生 required 在 submit 事件之前拦掉校验,
 *  于是 runDefensivePlanPreview() 永远走不到 —— 按钮看着能点、点了没反应)。
 * 于是 #industry 也变成了表达式,这条反向对照当场转红。
 *
 * 处置**不是**放宽这条锁,而是把反向对照挪到一个仍然无条件必填的控件上:
 * #brandName —— 防守流也必须绑定具体品牌(defensiveLaunchGate.hasBrandId),
 * 它在任何模式下都是裸 required。这样"检测器分得清两种形态"仍然被证明,
 * 分母没有被悄悄缩小。
 *
 * 同时**补上包H 自己欠的那条**:#industry 的 required 现在必须是取自
 * isDefensiveFlow 的表达式(见下一条 t)。顺手多修一处 = 顺手多欠一条判据。
 */
await t('反向对照:#brandName 的 required 仍是**裸属性**(检测器分得清两种形态)', () => {
    const attr = attrOf(elById('brandName'), 'required');
    assert.ok(attr, '#brandName 上没有 required —— 品牌名在任何模式下都必填');
    assert.equal(attr.initializer, undefined,
        '#brandName 的 required 变成了表达式(本次不该动它)');
});

await t('[包H] #industry 的 required 是**表达式**且取自 isDefensiveFlow', () => {
    const attr = attrOf(elById('industry'), 'required');
    assert.ok(attr, '#industry 上没有 required 属性');
    assert.ok(attr.initializer && ts.isJsxExpression(attr.initializer),
        '#industry 的 required 退回成裸属性 —— 防守流又会被原生校验拦死');
    assert.match(attr.initializer.getText(), /isDefensiveFlow/,
        'required 没接到页面的唯一分流键 isDefensiveFlow(不许另写一份 campaignMode 判断)');
});

// ═══════════════════════════════════════════════════════════════════
console.log('=== 门四 · 报告页取数失败态(纯逻辑,不需要浏览器)===');
// ═══════════════════════════════════════════════════════════════════
// 本体判据在 `scripts/test-defgeo-report-retry.mjs`(真浏览器 · 真组件 · 真取数),
// 那套要 chromium 所以不进 build 链。这里放的是**同一份纯逻辑模块**的真跑判据,
// 每次构建都会跑 —— 谁把重试谓词或那句文案改坏了,构建期就红。
const rlf = await load('pages/Diagnosis/report/reportLoadFailure.ts');

await t('瞬时判定:5xx 与"拿不到 status"算瞬时', () => {
    assert.equal(rlf.isTransientLoadFailure({ response: { status: 503 } }), true, '503(axios 形状)');
    assert.equal(rlf.isTransientLoadFailure({ response: { status: 500 } }), true, '500');
    assert.equal(rlf.isTransientLoadFailure({ response: { status: 599 } }), true, '599');
    assert.equal(rlf.isTransientLoadFailure({ status: 502 }), true, '502(fetch wrapper 形状)');
    // CanceledError 没有 response.status —— 这条正是门四实录里 clearApiDedupeCache
    // 把在途 GET abort 掉之后落到 catch 的那一种
    assert.equal(rlf.isTransientLoadFailure(new Error('canceled')), true, '取消/网络错');
    assert.equal(rlf.isTransientLoadFailure(undefined), true, 'undefined 也当瞬时(宁可多试一次)');
});

await t('反向:4xx 是终态,不许被当成瞬时', () => {
    for (const status of [400, 401, 403, 404, 422, 499]) {
        assert.equal(rlf.isTransientLoadFailure({ response: { status } }), false, String(status));
    }
});

await t('loadWithOneRetry:瞬时失败一次 ⇒ 跑两次并返回成功值', async () => {
    let n = 0;
    const slept = [];
    const v = await rlf.loadWithOneRetry(async () => {
        n += 1;
        if (n === 1) throw { response: { status: 503 } };
        return 'ok';
    }, async (ms) => { slept.push(ms); });
    assert.equal(n, 2);
    assert.equal(v, 'ok');
    assert.deepEqual(slept, [rlf.REPORT_RETRY_DELAY_MS], '退避用的是模块里那个常量');
});

await t('loadWithOneRetry:只重试**一次** ⇒ 连续两次瞬时失败就抛出去', async () => {
    let n = 0;
    await assert.rejects(
        rlf.loadWithOneRetry(async () => { n += 1; throw { response: { status: 503 } }; },
            async () => {}),
    );
    assert.equal(n, 2, '不许无限重试(用户对着骨架屏干等)');
});

await t('反向:终态 4xx 不重试(只跑一次)', async () => {
    let n = 0;
    await assert.rejects(
        rlf.loadWithOneRetry(async () => { n += 1; throw { response: { status: 403 } }; },
            async () => { throw new Error('4xx 不该走到退避'); }),
    );
    assert.equal(n, 1);
});

await t('文案:带行动指引 + 把钱说死 + 不吓人', () => {
    const c = rlf.TRANSIENT_FAILURE_COPY;
    assert.match(c.body, /重试/, '要明写点「重试」');
    assert.match(c.body, /不额外扣算力/, '失败页上她最想知道的是钱动没动');
    assert.match(c.headline, /报告还在/, '要明说报告没丢');
    for (const w of ['错误', '异常', '无法', '丢失', '崩溃']) {
        assert.ok(!c.headline.includes(w) && !c.body.includes(w), `不许出现「${w}」`);
    }
});

await t('describeReportLoadFailure:瞬时用固定人话,终态用服务端消息', () => {
    const t1 = rlf.describeReportLoadFailure({ response: { status: 503 } }, '加载报告失败');
    assert.equal(t1.isTransient, true);
    assert.equal(t1.headline, rlf.TRANSIENT_FAILURE_COPY.headline);
    assert.ok(!t1.headline.includes('失败'), '瞬时态不上原始 detail(503 的 body 是 nginx 的 HTML)');

    const t2 = rlf.describeReportLoadFailure({ response: { status: 403 } }, '没有权限查看这份报告');
    assert.equal(t2.isTransient, false);
    assert.equal(t2.headline, '没有权限查看这份报告', '终态消息有信息量,不许换成笼统句');
    assert.match(t2.body, /重试/, '终态也要给出口');
});

// ═══════════════════════════════════════════════════════════════════
console.log('=== 包E · 发布链客户入口的两条路由已恢复 ===');
// ═══════════════════════════════════════════════════════════════════
// 🔴 这一段是**改断言,不是退役**:P0-1 那一版断言"这两条不在",
//    包E 把执行侧接上之后断言"这两条在"。同一把尺子,方向反过来 ——
//    退役掉它等于从此没有任何东西看着这两条路由。
//
// 重开的前置条件在后端(publish_worker 的三个生产调用点 + 三条 cron job),
// 由 python 侧判据钉住;这里只管前端这一半:路由在、lazy 在、admin 那条没被误伤。
//
// 🔴 分母机械导出:把 App.tsx 里**所有** <Route path="..."> 扫出来当全集,
//    再判两条在不在。手写清单是拿结论当分母。
const APP_TSX = join(SRC, 'App.tsx');
const appSrc = (await import('node:fs')).readFileSync(APP_TSX, 'utf8');
const ROUTE_PATHS = [...appSrc.matchAll(/<Route\s+[^>]*?path="([^"]+)"/g)].map((m) => m[1]);

const REOPENED_ROUTES = [
    'defensive-geo/publish/decision/:snapshotId',
    'defensive-geo/publish/commands/:commandId',
];
const KEPT_ROUTE = 'admin/defensive-geo-settlement-review';
const NEVER_ROUTE = 'defensive-geo/publish/decision/:snapshotId/__never__';

await t('路由全集扫得出来(判据活性:分母不是空的)', () => {
    assert.ok(ROUTE_PATHS.length > 50,
        `只扫到 ${ROUTE_PATHS.length} 条 <Route> —— 正则没匹配上,后面几条断言会恒绿`);
});

await t('两条客户入口路由已恢复到 App.tsx(结构锚)', () => {
    const missing = REOPENED_ROUTES.filter((p) => !ROUTE_PATHS.includes(p));
    assert.deepEqual(missing, [],
        `这些客户入口路由没有恢复:${missing.join(' / ')} —— ` +
        '后端入口开着而前端没有入口 = 她看不到、只能靠直接调 API');
});

/* ═══════════════════════════════════════════════════════════════════
 * [合流 2026-08-24 Review-CTO · 预裁终态] 包E 重开客户入口(双端常量 true)
 * 与包H 的 DeepLink 锁族在此归并:路由**在**(上面包E 的结构锚)、且只许经
 * PublishDeepLink 入口闸挂载(下面包H 的锁族)。H 锁族原文写于其分支上
 * 入口仍关(false)的时期 —— 注释里的「必须是 false」按裁定改为「双端一致,
 * 现为 true」;锁的形态(import 恒零 / 路由必须挂 DeepLink / 闸先于确认屏)
 * 与常量方向无关,原样保留。
 * ═══════════════════════════════════════════════════════════════════ */
/**
 * 🔴🔴 [包H 2026-08-23] 这条锁**从"路径不在"收紧成"到不了确认屏"**。
 *
 * ## 为什么必须改,而不是给自己开个白名单
 *
 * 原锁判的是 `App.tsx` 里有没有这两个 path 字符串。那是**代理指标**:
 * 它真正想守的是「她点不到一个会冻钱的确认屏」,而"路径不存在"只是当时
 * 实现那个目标的一种方式。
 *
 * 包H 按工单要 U-6「审批通过 → 深链直达确认页」。落点不存在时她点开通知
 * 看到的是**白页** —— 白页解释不了任何事,§0.5.6 铁律(任何阻塞必须自带
 * 解决方案)在那里是失效的。所以落点补回来了,但补的是一个**会解释的落点**:
 * 两条 path 都指向 `PublishDeepLink`,它先问 `publishEntryGate`,
 * 入口关着就渲染 typed 说明(「还没开放 · 没有扣除任何算力」)+ 出口,
 * **不渲染确认页**。
 *
 * ## 收紧后的锁在守什么(比原来强,不是弱)
 *
 *   ① 这两条 path 若存在,element 必须是 `PublishDeepLink` 家族,
 *      **不许**直接挂 MediaDecisionConfirm / PublishCommandStatus;
 *   ② `publishEntryGate.PUBLISH_CUSTOMER_ENTRY_OPEN` 与后端逐值一致
 *      ([合流 2026-08-24] 原文「必须是 false」——包E 接线后双端翻 true,
 *      方向由裁定定,一致性由 X-2 镜像判据钉);
 *   ③ `PublishDeepLink` 里那两个组件必须在 gate 之后才 render
 *      (源码里 `publishEntryScreen()` 的判断必须出现在 lazy 组件使用之前)。
 *
 * 原锁只要有人把 path 加回去就红;新锁**允许 path 存在但不允许它通向冻钱屏**,
 * 而且把后端那半边也钉住了 —— 后端 `_CUSTOMER_PUBLISH_ENTRY_OPEN` 由
 * `tests/defensive_geo_pkgh_2026_08_23/test_pkgh_cross_layer.py::test_publish_entry_flag_mirrors_backend`
 * 与前端常量逐值对账(终审 P0-1 那次的教训正是"只摘了前端,后端端点还能直接调")。
 *
 * 🔴 白名单**一个都没加**:两条 path 仍然逐条被检查,只是检查的内容从
 *    "在不在"换成了"通向哪"。想绕过它必须真的把确认页挂上去 —— 那会当场红。
 */
/**
 * 🔴🔴 [包H · R1-① 2026-08-24] 分母重写:从「我知道的那两条 path」换成
 *      「App.tsx 里**全部** import() 调用形」。
 *
 * Review 亲毒证明上一版是**继承来的盲区**(原锁同样瞎):
 * 把 lazy 常量改个名字、再加**第三条**客户路由直挂 MediaDecisionConfirm ——
 * 38/38 + 静态闸全绿。因为上一版数的是两条硬编码 path + 两个具名常量,
 * 而真正要守的是「App.tsx 里有没有任何一条路能通到确认组件」。
 * 第三条路由不在我的名单里,于是整个逃出去。
 *
 * 新分母 = App.tsx 里每一个 `import('…')` 调用形。理由:App.tsx 要挂上那两个
 * 组件,**只能**经由动态 import —— 改常量名、改路由数、改 element 写法都绕不开它。
 *
 * 🔴 锚在 import() **调用形**上,不锚裸名:第 294 行那句
 *    「MediaDecisionConfirm / PublishCommandStatus 两个 lazy 入口随路由一起摘除」
 *    是病历性注释,判红它等于逼人删掉现场记录(本仓记过这条)。
 *    所以先剥注释再扫。
 *
 * 🔴 顺带堵上 Review 没点名、但在同一分母里的第二个绕过面:
 *    `pages/DefensivePublish/index.ts` **barrel 再导出**了这两个组件,
 *    于是 `import('@/pages/DefensivePublish').then(m => ({default: m.MediaDecisionConfirm}))`
 *    的 specifier 里根本不出现组件名。所以除了看 specifier,还要看它后面那截 `.then`。
 */
// 🔴 剥注释要**保守**。第一版用无差别的块注释正则配对,App.tsx 里字符串与
//    正则字面量中那些散装的块注释开头,会跟很后面的块注释结尾配上对,
//    **把中间的真代码整段吃掉** —— 实测 PublishDeepLink 在原文出现 3 次,
//    剥完变 0 次,分母里一条 DefensivePublish 都没有。
//    (那一版扫出来 71 条;正则修好后实测 158 条 —— 交付单初版沿用了坏数字,R2 已更正。)
//    (又是"判据活性"那条把它抓出来的。)
//    现在只剥**行首**或 `{` 之后的块注释(JSX 注释与文档注释的真实形态),
//    URL 里的双斜杠、字符串里的星号斜杠都碰不到。
//    ⚠️ 这段用行注释写:上一版写成块注释,正文里粘了正则,里面那个
//       块注释结束符把注释自己提前闭合了,整个脚本语法错。
const APP_CODE_ONLY = appSrc
    .replace(/(^|\{)\s*\/\*[\s\S]*?\*\//gm, '$1')
    .split('\n').filter((l) => !l.trim().startsWith('//')).join('\n');

/**
 * 全部 import() 调用形 + 其后 200 字符(够看到 .then 里取了什么)。
 *
 * 🔴 尾巴用**前瞻**而不是普通捕获组。第一版写成 `)([\s\S]{0,200})`,
 *    matchAll 的 lastIndex 会跳过那 200 字符 —— 而 App.tsx 的 lazy import
 *    一行一个、每行才 100 多字符,于是**下一条 import() 被尾巴吃掉**,
 *    扫出来的分母只有真实值的一小半,连 PublishDeepLink 那条都不见了。
 *    是上面那条"判据活性"当场把它抓出来的:没有那条,这个锁会带着一个
 *    残缺分母恒绿。
 */
const DYN_IMPORTS = [...APP_CODE_ONLY.matchAll(
    /\bimport\(\s*(['"])([^'"]+)\1\s*\)(?=([\s\S]{0,200}))/g)]
    .map((m) => ({ spec: m[2], tail: m[3] }));

const CONFIRM_MODULES = ['MediaDecisionConfirm', 'PublishCommandStatus'];

await t('判据活性:App.tsx 的 import() 调用形扫得出来(分母不是空的)', () => {
    assert.ok(DYN_IMPORTS.length > 20,
        `只扫到 ${DYN_IMPORTS.length} 个 import() —— 正则没匹配上,下面那条会恒绿`);
    // 反向对照:探测器**看得见**真实存在的那条(闸本身的落点)。
    assert.ok(DYN_IMPORTS.some((d) => /PublishDeepLink/.test(d.spec)),
        '探测器连 PublishDeepLink 的 import() 都看不见 —— 它不是在扫真东西');
});

await t('R1-① App.tsx 通向确认组件的 import() 调用形恒零(分母=全部 import())', () => {
    const direct = DYN_IMPORTS.filter(
        (d) => CONFIRM_MODULES.some((n) => d.spec.includes(`DefensivePublish/${n}`)));
    assert.deepEqual(direct.map((d) => d.spec), [],
        `App.tsx 直接 import 了确认/状态页:${direct.map((d) => d.spec).join(' / ')}`);

    // barrel 面:specifier 指到 DefensivePublish 目录,再从 .then 里取组件名。
    const viaBarrel = DYN_IMPORTS.filter(
        (d) => /pages\/DefensivePublish$/.test(d.spec)
            && CONFIRM_MODULES.some((n) => d.tail.includes(n)));
    assert.deepEqual(viaBarrel.map((d) => d.spec), [],
        `App.tsx 经 barrel 取到了确认/状态页:${viaBarrel.map((d) => d.spec).join(' / ')}`);
});

/**
 * 🔴🔴 [包H · R2 2026-08-24] 第三条恒零断言:**静态 import**。
 *
 * R1-① 的两条断言都锚 `import()` **调用形**,于是同成本的第三种形态整条漏出去:
 *   App.tsx 顶部加一行 `import { MediaDecisionConfirm } from '@/pages/DefensivePublish';`
 *   + 新 path 直接 `<MediaDecisionConfirm />`
 * → DYN_IMPORTS 零匹配(它不是 import() 调用形),CLOSED_ROUTES 也不看新路由。
 * 同文件、同成本、绕过。所以按同一把尺子关。
 *
 * 三个子面(各自一个 cell,各自一发验收变异 MUT-C / MUT-D / MUT-E):
 *   direct       specifier 直含 `DefensivePublish/<确认组件>`
 *   viaBarrel    specifier 正是 barrel 目录,且**命名导入**里出现二者之一
 *   viaNamespace specifier 正是 barrel 目录,且是 `* as` —— 命名空间把整个 barrel
 *                都拿走了(`<NS.MediaDecisionConfirm />`),名字根本不出现在 clause 里
 *
 * 🔴🔴 [R2 收口 2026-08-24] **后两格现在是「复引入哨兵」态,不是主防线。**
 *   Review 拍板做了设计反转:`pages/DefensivePublish/index.ts` 已**不再导出**
 *   `MediaDecisionConfirm` / `PublishCommandStatus`(双树 census 零生产消费方)。
 *   面被结构性消灭 —— 没有面就不用守,这比在判据里再加一块 pattern 补丁强。
 *
 *   两格**保留不删**,各自的语义变成:
 *     `viaBarrel`    哪天有人把那两行 export 加回来、并在 App.tsx 用命名导入取到它,
 *                    这一格第一个叫(在 barrel 空着的现在,写它连 tsc 都过不去);
 *     `viaNamespace` **仍是完整的锁,不只是哨兵** —— 它只看"是不是 `* as` 这个 barrel",
 *                    不看名字,所以无论 export 在不在,namespace 导入本 barrel 一律红。
 *   （所以下面 MUT-C 打的是哨兵态、MUT-E 打的仍是活锁;两发都必须继续转红。）
 *
 * 🔴 不许误伤合法静态导入:`SettlementReviewQueue`(直接路径与 barrel 命名导入两种写法)、
 *    `import type { StatusUpdateVerdict } from '@/pages/DefensivePublish'`、以及全部
 *    非 DefensivePublish 的静态 import。下面用**同一个函数**跑 legit / bypass 两组样本,
 *    正样本逐条点名"该由哪条规则抓"(只断言"有命中"会被别的规则顺手判绿/判红)。
 *
 * 🔴🔴 本锁**已知够不到**的四类(明示不追,不是忘了):
 *   ① 模板串 specifier —— import(`@/pages/${d}/MediaDecisionConfirm`)
 *   ② 拼接 —— specifier 拼接,或 barrel `.then` / clause 里用 `m['Media'+'...']` 取名
 *   ③ App.tsx **外**的转发文件 —— 某个第三方组件自己 import 确认页,App.tsx 只挂它
 *   ④ require(...) —— 本仓 ESM,出现即是刻意行为
 *   这四类都要**刻意伪装**才写得出来,不是"改个名顺手就绕过"那一档;
 *   而且它们全部撞在同一道后端闸上:`_CUSTOMER_PUBLISH_ENTRY_OPEN`
 *   —— 由 test_pkgh_cross_layer.py::test_publish_entry_flag_mirrors_backend
 *   与前端 `publishEntryGate` 逐值对账。[合流 2026-08-24] 入口现为双端 true
 *   (开门是业务意图);这道闸的价值变为「将来关门时一关全关」——
 *   镜像判据保证不会再出现「只摘前端」那半拉子状态。
 */
const staticImportBypasses = (source) => {
    // `(?:^|;)` —— 行首,外加"同一行分号之后"(`import a from 'x'; import {B} from 'y';`
    // 是合法 ESM,只锚行首会漏)。`import\s+` 后面必须是空白,所以 `import(` 天然不入这个分母。
    const stmts = [...source.matchAll(
        /(?:^|;)\s*import\s+(?:([\s\S]*?)\s+from\s+)?(['"])([^'"]+)\2/gm)]
        .map((m) => ({ clause: (m[1] || '').replace(/\s+/g, ' ').trim(), spec: m[3] }));
    const isBarrel = (s) => /pages\/DefensivePublish$/.test(s.spec);
    return {
        all: stmts,
        direct: stmts.filter(
            (s) => CONFIRM_MODULES.some((n) => s.spec.includes(`DefensivePublish/${n}`))),
        viaBarrel: stmts.filter((s) => isBarrel(s)
            // `\b…\b`:`MediaDecisionConfirmXyz` 不算,别名 `as` 后面照样算。
            && CONFIRM_MODULES.some((n) => new RegExp(`\\b${n}\\b`).test(s.clause))),
        viaNamespace: stmts.filter((s) => isBarrel(s) && /^\*\s+as\b/.test(s.clause)),
    };
};

const STATIC_LEGIT = [
    "import SettlementReviewQueue from '@/pages/DefensivePublish/SettlementReviewQueue';",
    "import { SettlementReviewQueue } from '@/pages/DefensivePublish';",
    "import type { StatusUpdateVerdict } from '@/pages/DefensivePublish';",
    "import { usePublishStatusPolling } from '@/pages/DefensivePublish';",
    "import { ProtectedRoute } from '@/components/auth/ProtectedRoute';",
    "import * as React from 'react';",
];
const STATIC_BYPASS = [
    { rule: 'direct', src: "import MediaDecisionConfirm from '@/pages/DefensivePublish/MediaDecisionConfirm';" },
    { rule: 'direct', src: "import PublishCommandStatus from '@/pages/DefensivePublish/PublishCommandStatus';" },
    { rule: 'viaBarrel', src: "import { MediaDecisionConfirm } from '@/pages/DefensivePublish';" },
    { rule: 'viaBarrel', src: "import { MediaDecisionConfirm as Anything } from '@/pages/DefensivePublish';" },
    { rule: 'viaBarrel', src: "import { SettlementReviewQueue, PublishCommandStatus } from '@/pages/DefensivePublish';" },
    // 同一行分号之后 —— 证明 `(?:^|;)` 那半边真的在干活,不是白写的。
    { rule: 'viaBarrel', src: "import { Layout } from '@/components/layout/Layout'; import { PublishCommandStatus } from '@/pages/DefensivePublish';" },
    { rule: 'viaNamespace', src: "import * as Defgeo from '@/pages/DefensivePublish';" },
];
const RULES = ['direct', 'viaBarrel', 'viaNamespace'];

await t('R2 反向对照:静态 import 规则不误伤合法导入,且正样本逐条点名规则', () => {
    // 判据活性:探测器在真文件上看得见东西(空分母 ⇒ 下面全恒绿)。
    const real = staticImportBypasses(APP_CODE_ONLY);
    assert.ok(real.all.length >= 10,
        `只扫到 ${real.all.length} 条静态 import —— 正则没匹配上,恒零断言会恒绿`);
    assert.ok(real.all.some((s) => s.spec === '@/context/AuthContext'),
        '探测器连 AuthContext 的静态 import 都看不见 —— 它不是在扫真东西');

    for (const src of STATIC_LEGIT) {
        const r = staticImportBypasses(src);
        assert.ok(r.all.length >= 1, `合法样本自己就没被解析出来(样本无效):${src}`);
        for (const rule of RULES) {
            assert.deepEqual(r[rule].map((s) => s.spec), [],
                `误伤合法静态导入(规则 ${rule}):${src}`);
        }
    }

    for (const { rule, src } of STATIC_BYPASS) {
        const r = staticImportBypasses(src);
        assert.equal(r[rule].length, 1, `规则 ${rule} 没抓到它该抓的正样本:${src}`);
        for (const other of RULES.filter((x) => x !== rule)) {
            assert.equal(r[other].length, 0,
                `正样本被规则 ${other} 顺手判红(说明它不是被 ${rule} 抓的):${src}`);
        }
    }
});

await t('R2 App.tsx 通向确认组件的**静态 import** 恒零(分母=全部静态 import)', () => {
    const r = staticImportBypasses(APP_CODE_ONLY);
    assert.deepEqual(r.direct.map((s) => s.spec), [],
        `App.tsx 静态 import 了确认/状态页:${r.direct.map((s) => s.spec).join(' / ')}`);
    assert.deepEqual(r.viaBarrel.map((s) => s.clause), [],
        `App.tsx 经 barrel 静态取到了确认/状态页:${r.viaBarrel.map((s) => s.clause).join(' / ')}`);
    assert.deepEqual(r.viaNamespace.map((s) => s.clause), [],
        `App.tsx 命名空间导入了 DefensivePublish barrel(整包组件都在手里):${r.viaNamespace.map((s) => s.clause).join(' / ')}`);
});

await t('两条客户入口路由 = ProtectedRoute(writing) 包 PublishDeepLink,唯一挂法', () => {
    // [合流 2026-08-24 Review-CTO] 三处与 H 原版不同,都是**收紧**:
    //  · 分母沿用包E 的 REOPENED_ROUTES(同两条 path;H 侧原名 CLOSED_ROUTES
    //    在合并树上已不存在);
    //  · H 原版用 `[^>]*?\/>` 抓整段 JSX —— 在 ProtectedRoute 包裹形上,
    //    element 里的 `>` 会把匹配提前截断 ⇒ `if (!hit) continue` 静默放行。
    //    改成**按行**锚定:App.tsx 一条 Route 一行,行就是完整证据;
    //  · 预裁终态要求 ProtectedRoute(writing) 包裹,一并钉进锁(拆包裹即红);
    //    路由必须恰好一条(合流曾出现同 path 重复挂载,first-match 影蔽)。
    const lines = appSrc.split('\n');
    for (const p of REOPENED_ROUTES) {
        const hits = lines.filter((l) => l.includes(`path="${p}"`));
        assert.equal(hits.length, 1,
            `${p} 的 Route 行应恰好 1 条,实得 ${hits.length} —— 0=锁空转,>1=重复挂载互相影蔽`);
        const line = hits[0];
        assert.match(line, /DefgeoPublish(Decision|Command)Link/,
            `${p} 没有经 PublishDeepLink 的入口闸挂载`);
        assert.doesNotMatch(line, /MediaDecisionConfirm|PublishCommandStatus/,
            `${p} 绕过闸直接渲染冻钱屏`);
        assert.match(line, /<ProtectedRoute requiredModule="writing">/,
            `${p} 没有被 ProtectedRoute(writing) 包裹 —— 预裁终态要求登录+writing 模块`);
    }
});

// 🔴 与上面的 appSrc 同一写法:在**模块作用域**读文件(顶层 await 才允许 import),
//    不要在 `t()` 的回调里 `await import` —— 那个回调不是 async,会是语法错。
const nodeFs = (await import('node:fs'));
const gateSrc = nodeFs.readFileSync(
    join(SRC, 'pages/DefensivePublish/publishEntryGate.ts'), 'utf8');
const deepLinkSrc = nodeFs.readFileSync(
    join(SRC, 'pages/DefensivePublish/PublishDeepLink.tsx'), 'utf8');

await t('入口闸常量为开(双端一致),且确认页在闸之后才 render', () => {
    // [合流 2026-08-24] H 原版钉 false(其分支上执行侧未接线)。包E 接线完成
    // 后按裁定双端翻 true —— 这里钉前端半边,后端半边与两侧一致性由
    // test_pkgh_cross_layer.py::test_publish_entry_flag_mirrors_backend 钉。
    // 闸先于确认屏的顺序检查与常量方向无关,原样保留(将来关闸即生效)。
    const gate = gateSrc;
    assert.match(gate, /export const PUBLISH_CUSTOMER_ENTRY_OPEN = true;/,
        '前端入口闸不是 true —— 双端常量必须一起翻(合流裁定:执行侧已接线,入口开)');

    const link = deepLinkSrc;
    const gateAt = link.indexOf('publishEntryScreen()');
    const confirmAt = link.indexOf('<MediaDecisionConfirm');
    assert.ok(gateAt !== -1, 'PublishDeepLink 里没有调 publishEntryScreen —— 闸没接线');
    assert.ok(confirmAt === -1 || gateAt < confirmAt,
        '确认页在闸之前就渲染了 —— 闸形同虚设');
});

await t('反向对照:一条**不该存在**的同族路由必须扫不到(证明检测不是恒真)', () => {
    assert.ok(!ROUTE_PATHS.includes(NEVER_ROUTE),
        '连一条编出来的路由都"扫得到" —— 这个检测器是恒真的,上面那条不算数');
});

await t('admin 只读队列路由仍在(重开没有误伤运维入口)', () => {
    assert.ok(ROUTE_PATHS.includes(KEPT_ROUTE),
        'admin 结算复核队列路由不见了');
});

await t('两个 lazy 入口也一并恢复(路由在但组件没接 = 白屏)', () => {
    // [合流 2026-08-24] E 原版锚直挂常量 DefgeoMediaDecisionConfirm/…Status ——
    // 预裁终态改经 PublishDeepLink 家族,直挂常量已删(R1-① import 恒零锁着),
    // 所以这条的锚跟着搬家:守的仍是「路由在但组件没接 = 白屏」这件事。
    for (const name of ['DefgeoPublishDecisionLink', 'DefgeoPublishCommandLink']) {
        assert.ok(new RegExp(`const\\s+${name}\\s*=`).test(appSrc),
            `${name} 的 lazy 声明不在 —— 路由恢复了却没有组件可渲染`);
    }
    // 反向对照:一个**不存在**的同族名字必须匹配不到,证明这条检测不是恒真
    assert.ok(!/const\s+DefgeoNeverExistsPage\s*=/.test(appSrc),
        '连一个编出来的组件名都"匹配得到" —— 检测器是恒真的');
});

console.log('================================================================');
console.log(`总计 ${pass + fail} 条 · 通过 ${pass} · 失败 ${fail}`);
if (fail > 0) { console.log('🔴 门二/三/四返修判据未通过'); process.exit(1); }
console.log('✅ 门二/三/四返修判据通过(G2/G4/G5 + legacy 48 格等价 + G2b 接线锁 + 门四纯逻辑)');
