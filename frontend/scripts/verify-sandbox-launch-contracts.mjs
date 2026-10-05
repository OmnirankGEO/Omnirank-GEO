#!/usr/bin/env node
/**
 * 判据 · #176 教程沙盒的启动页合同。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 **缺陷形态**:诊断启动页自 09-05 起「价只从服务端取」——
 *    POST `/api/pricing/diagnosis-preview` 拿不到 `points` + `pricePreviewId`
 *    就**闸死「开始品牌体检」**。沙盒拦截器没有这条规则 ⇒ 落到未登记分支 ⇒
 *    本地 501 ⇒ 客户端把它折成「这次没能算出价格」。
 *    于是教程走到这一步就死,而屏幕上那句话看起来像"网络不好,再试试"。
 *    Owner 09-12 现场演示正卡在这里。
 *
 * 🔴 **为什么必须有行为臂,不能只 grep「规则在不在」**:
 *    这条链是 `NewDiagnosis → fetchDiagnosisPrice → authFetch → lazy → 拦截器 → 规则`。
 *    grep 只能证明规则那一格存在;而本单的缺陷恰恰是**链上有一节不通**。
 *    所以 A2/B2 走**真消费方**:esbuild 打真的 `diagnosisPricePreview.ts`,
 *    只把 `@/lib/api` 换成一个**纯转发**的 authFetch 桩
 *    (Review 09-12 裁定:桩只能转发,不许返回固定 Response、不许自拼 payload)。
 *    跑的是真实的解析/校验 + 真实的拦截器规则。
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const require_ = createRequire(import.meta.url);

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

// node 里没有 window/localStorage;拦截器与 sandboxState 都有 typeof 守卫,
// 但给它们垫一个最小壳更稳(且 localStorage 为空 ⇒ isSandboxActive() 为 false,A3 要的正是这个)
if (typeof globalThis.window === 'undefined') globalThis.window = globalThis;
if (typeof globalThis.localStorage === 'undefined') {
    const store = new Map();
    globalThis.localStorage = {
        getItem: (k) => (store.has(k) ? store.get(k) : null),
        setItem: (k, v) => store.set(k, String(v)),
        removeItem: (k) => store.delete(k),
        clear: () => store.clear(),
    };
}

// ══ M 元判据:先证打包这一步真的成了 ═══════════════════════════════════
let M;
try {
    const esbuild = require_('esbuild');
    const tmp = mkdtempSync(join(tmpdir(), 'a176-'));
    // 🔴 authFetch 桩**只能转发** —— 它把 (url, init) 原样交给拦截器,
    //    不返回固定 Response、不自拼 payload。所以 A2/B2 跑的是真解析 + 真规则。
    const stub = join(tmp, 'api-stub.js');
    writeFileSync(stub, [
        'export async function authFetch(url, init) {',
        '  const fn = globalThis.__A176_FORWARD__;',
        '  if (typeof fn !== "function") throw new Error("A176: forwarder not installed");',
        '  return fn(url, init);',
        '}',
        'export function formatApiErrorForDisplay(e, fallback) { return fallback || String(e); }',
        'export default { authFetch };',
    ].join('\n'), 'utf8');
    const entry = join(tmp, 'entry.js');
    writeFileSync(entry, [
        `export { tryFetchSandboxMock } from ${JSON.stringify(join(SRC, 'sandbox/sandboxInterceptor.ts'))};`,
        `export { fetchDiagnosisPrice, priceMatchesInput } from ${JSON.stringify(join(SRC, 'pages/Diagnosis/launch/diagnosisPricePreview.ts'))};`,
        `export { fetchSuggestedQuestions } from ${JSON.stringify(join(SRC, 'pages/Diagnosis/launch/suggestQuestionsApi.ts'))};`,
    ].join('\n'), 'utf8');
    // Vite 的 `?raw` 导入(mockData 里那份 demo 报告)esbuild 不认 —— 给它一个 text loader。
    // 只是让打包过得去,不改任何被测逻辑。
    const rawPlugin = {
        name: 'a176-raw',
        setup(b) {
            b.onResolve({ filter: /\?raw$/ }, (args) => ({
                path: resolve(args.resolveDir, args.path.replace(/\?raw$/, '')),
                namespace: 'a176raw',
            }));
            b.onLoad({ filter: /.*/, namespace: 'a176raw' }, (args) => ({
                contents: readFileSync(args.path, 'utf8'),
                loader: 'text',
            }));
        },
    };
    const out = join(tmp, 'bundle.mjs');
    await esbuild.build({
        entryPoints: [entry],
        bundle: true,
        format: 'esm',
        platform: 'neutral',
        target: 'es2022',
        outfile: out,
        alias: { '@/lib/api': stub, '@': SRC },
        plugins: [rawPlugin],
        logLevel: 'silent',
    });
    M = await import(pathToFileURL(out).href);
    if (typeof M.tryFetchSandboxMock !== 'function') throw new Error('tryFetchSandboxMock 不是函数');
} catch (e) {
    console.log('  FAIL M0 🔴 打不出包,下面每一条行为臂都跑不了:' + String((e && e.message) || e));
    console.log('       修法:esbuild 是 vite 的依赖,构建镜像里应有对应平台的二进制;');
    console.log('       🔴 **不要**给本判据加 SKIP 分支 —— 那会把「没跑」伪装成「通过」。');
    process.exitCode = 1;
    process.exit(1);
}
console.log('  OK  M1 元判据:真消费方 + 真拦截器已打包(authFetch 为纯转发桩)');

// 纯转发:A2/B2 的 authFetch 就是这一行
globalThis.__A176_FORWARD__ = (url, init) =>
    M.tryFetchSandboxMock((init?.method ?? 'GET'), url, true, init);

const post = (url, body) => M.tryFetchSandboxMock('POST', url, true, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
});

// ══ A 取价合同(§2.1 · 必修那条) ═══════════════════════════════════════
console.log('A 取价合同');
{
    const res = await post('/api/pricing/diagnosis-preview', {
        questions: ['甲', '乙', '乙', '  ', '丙'], aiOptimizeCustom: false, scope: 'geo',
    });
    ok(!!res, 'A1a 规则命中(返回了 Response,不是 null)');
    if (res) {
        ok(res.status === 200, 'A1b status 200', String(res.status));
        ok(res.headers.get('x-sandbox-mock') === '1', 'A1c 带 x-sandbox-mock');
        ok(!res.headers.get('x-sandbox-unhandled'),
            'A1d 🔴 **没有** x-sandbox-unhandled(有它就说明还在走 501 未登记分支)');
        const b = await res.json();
        // 🔴 被毒/被打坏时 d 可能缺字段甚至为空。下面每一格都必须**报红而不是抛** ——
        //    抛异常会让脚本中断,后面的 A2/A3/B 根本不跑,而非零退出码
        //    **看起来像命中**。那是仪器死了,不是锁咬住了。
        //    (本单初版就栽在 A1h 的 `d.pricePreviewId.startsWith(...)`:
        //     毒1 一下去它先崩,A2 连跑的机会都没有 —— 于是我没法说「毒1 让 A2 红了」。)
        const d = (b?.data ?? b) || {};
        // 甲/乙/乙/空白/丙 ⇒ 去重去空白后 3 条
        ok(d.questionCount === 3,
            'A1e 🔴 questionCount **回显请求里的数**(去重去空白后 3)—— 写死任何值都会在'
            + '用户增删题目时立刻与 priceMatchesInput 对不上,按钮重新闸死', String(d.questionCount));
        ok(typeof d.points === 'number', 'A1f points 是数字', typeof d.points);
        ok(typeof d.pricePreviewId === 'string' && d.pricePreviewId.length > 0,
            'A1g pricePreviewId 非空字符串', String(d.pricePreviewId));
        ok(typeof d.pricePreviewId === 'string' && d.pricePreviewId.startsWith('sandbox-'),
            'A1h id 带 sandbox- 前缀(一眼看出不是真 price_preview)',
            String(d.pricePreviewId));
        ok(d.estimate === d.points, 'A1i estimate === points');
    }
}

// ══ A2 🔴 走真消费方(锁整条边,不是锁规则本身) ═══════════════════════
console.log('A2 真消费方 fetchDiagnosisPrice');
{
    const r = await M.fetchDiagnosisPrice({
        questions: ['甲', '乙', '丙'], aiOptimizeCustom: false, scope: 'geo',
    });
    ok(r.ok === true, 'A2a 🔴 真消费方拿到价(ok===true)——'
        + ' 这一臂锁的是 fetchDiagnosisPrice → authFetch → 拦截器 → 规则整条边',
        r.ok ? '' : JSON.stringify(r.error));
    ok(r.ok === true && M.priceMatchesInput(r.data, 3) === true,
        'A2b 🔴 priceMatchesInput(data, 3) === true —— 客户端认这份价配得上当前输入');
    // 反臂:数字不对时这个匹配必须为 false(否则 A2b 是空断言)
    ok(r.ok === true && M.priceMatchesInput(r.data, 4) === false,
        'A2c 反臂:题数变了就不匹配(证明 A2b 不是恒真)');
}

// ══ A3 反面:未激活沙盒不许命中 mock ══════════════════════════════════
console.log('A3 反面');
{
    const res = await M.tryFetchSandboxMock('POST', '/api/pricing/diagnosis-preview', false, {
        method: 'POST', body: JSON.stringify({ questions: [] }),
    });
    ok(res === null,
        'A3 🔴 force=false 且沙盒未激活 ⇒ 返 null(真用户的请求不许被 fixture 劫走)',
        res === null ? '' : `status=${res.status}`);
}

// ══ B 候选问题(§2.2 · 同车) ═══════════════════════════════════════════
console.log('B 候选问题');
{
    const res = await post('/api/diagnosis/suggest-questions', {
        brand_id: 1, mode: 'growth', business_scope: '',
    });
    ok(!!res && res.status === 200, 'B1a 规则命中且 200', res ? String(res.status) : 'null');
    if (res) {
        const b = await res.json();
        const d = b?.data ?? b;
        const cs = Array.isArray(d?.candidates) ? d.candidates : [];
        ok(cs.length >= 1, 'B1b candidates >= 1', String(cs.length));
        const badOne = cs.find((c) => !String(c?.question || '').trim()
            || !['growth', 'defensive'].includes(String(c?.side)));
        ok(!badOne, 'B1c 每条 question 非空且 side ∈ {growth, defensive}(**后端词**,不是前端词)',
            badOne ? JSON.stringify(badOne) : `${cs.length} 条全合规`);
    }
    const r = await M.fetchSuggestedQuestions(
        (url, init) => M.tryFetchSandboxMock((init?.method ?? 'GET'), url, true, init),
        { brandId: 1, mode: 'growth' },
    );
    ok(r.ok === true, 'B2a 真消费方 fetchSuggestedQuestions 拿到候选',
        r.ok ? '' : JSON.stringify(r.error));
    const cands = r.ok ? (r.data?.candidates || []) : [];
    ok(cands.length >= 1, 'B2b candidates >= 1', String(cands.length));
    ok(cands.length > 0 && cands.every((c) => c.side === 'offensive'),
        'B2c 🔴 side 已被客户端映射成**前端词** offensive —— 后端词进、前端词出,'
        + '这一格证明映射那一步真跑了',
        cands.map((c) => c.side).join(','));
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
