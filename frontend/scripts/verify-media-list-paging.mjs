#!/usr/bin/env node
/**
 * 判据 · #181 媒体列表「点了下一页还是当前页」(间歇)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 **为什么它间歇**:要三件事同时发生 —— 选了行业大类 + 换索引后该行业计数为 0 +
 *    在 facets 回包**之前**点了下一页。所以"Owner 自测未复现"不等于"没有这个缺陷";
 *    真正的证据是代码里那一处**在 click handler 之外改页码**的地方
 *    (`PublishCenter.tsx:1907-1909` 的 `setWmPage(1)`),它不受列表请求的
 *    AbortController 约束。
 *
 * 本文件是 build 链那一半:纯函数真调 + 接线边。
 * 竞态本身(慢包回来把用户打回第 1 页)要真的排时序,在
 * `test-media-list-page-race.mjs`(真 chromium,不进 build 链)。
 */
import { readFileSync, writeFileSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

const blank = (m) => m.replace(/[^\n]/g, ' ');
const decomment = (s) => s
    .replace(/\{\/\*[\s\S]*?\*\/\}/g, blank)
    .replace(/\/\*[\s\S]*?\*\//g, blank)
    .replace(/^\s*\/\/.*$/gm, blank);

const PAGE = 'src/pages/Publishing/PublishCenter.tsx';
const MOD = 'src/pages/Publishing/mediaListPaging.ts';

// ══ M 元判据 ═══════════════════════════════════════════════════════════
let M;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd(MOD), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    const tmp = mkdtempSync(join(tmpdir(), 'a181-'));
    const f = join(tmp, 'paging.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(pathToFileURL(f).href);
} catch (e) {
    console.log('  FAIL M0 🔴 判定模块加载不了:' + String((e && e.message) || e));
    console.log('       🔴 不要加 SKIP —— 那会把「没跑」伪装成「通过」。');
    process.exit(1);
}
ok(['reconcilePage', 'canGoNext', 'canGoPrev', 'shouldClearIndustry', 'isStalePacket']
    .every((k) => typeof M[k] === 'function'), 'M1 五个判定函数都导出了');

const page = decomment(rd(PAGE));

// ══ A 根因本身:异步回包不许改页码 ═════════════════════════════════════
console.log('\nA 根因:click handler 之外没有人改页码');
{
    /**
     * 分母:全文件所有 `setWmPage(` / `setMhzPage(` 调用,逐处判它在不在
     * 同步的 click/keydown handler 里。异步回包里出现一处,本单的缺陷就还在。
     */
    const lines = page.split(String.fromCharCode(10));
    /**
     * 🔴 判"这一处在不在异步回包里"要看**构造窗口**,不是单行。
     *    第一版按单行找 `onClick`,结果把四处多行写法的同步 handler 全冤枉了
     *    (`onClick={() => {` 在上面几行,`setWmPage(1)` 在下面)。
     *    这和 #179 E2 是同一个轴错误。换轴:向上回看 14 行,
     *    先看有没有同步入口,再看是不是落在 `.then(` 回调里。
     *    正/反两条对照在下面 A1b/A1c —— 换轴之后必须仍抓得住真的异步重置。
     */
    /**
     * 🔴 用**就近原则**判归属,不用固定回看行数:向上找最近的 `.then(` 与最近的
     *    同步 handler(onClick/onKeyDown/...),谁更近就算谁的。
     *    第一版写死回看 14 行 —— facets 那段里 `.then(d => {` 离 `setWmPage(1)` 有二十多行
     *    (中间全是注释,去注释后是空行但仍占行号),于是**根因那一处反而漏掉了**:
     *    注毒把它放回去,A1 照样绿。窗口太短与窗口太长各有各的假,就近原则两头都躲开。
     */
    const SYNC_RE = /on(Click|KeyDown|ValueChange|CheckedChange|Change)\s*=/;
    const classify = (src) => {
        const ls = src.split(String.fromCharCode(10));
        const out = [];
        ls.forEach((L, i) => {
            if (!/set(Wm|Mhz)Page\(/.test(L)) return;
            if (/st\.page/.test(L)) return;                 // 钳位那一处是按服务端真相校准,不是重置
            let thenAt = -1;
            let syncAt = -1;
            for (let k = i; k >= 0; k -= 1) {
                if (thenAt === -1 && /\.then\(/.test(ls[k])) thenAt = k;
                if (syncAt === -1 && SYNC_RE.test(ls[k])) syncAt = k;
                if (thenAt !== -1 && syncAt !== -1) break;
            }
            if (thenAt > syncAt) out.push({ line: i + 1, text: L.trim() });
        });
        return out;
    };
    const setters = [];
    lines.forEach((L, i) => { if (/set(Wm|Mhz)Page\(/.test(L)) setters.push(i + 1); });
    ok(setters.length >= 10,
        'A0 分母非空且量级对(全文件 setWmPage/setMhzPage 调用点)', `${setters.length} 处`);
    const asyncResets = classify(page);
    ok(asyncResets.length === 0,
        'A1 🔴🔴 异步回包里**没有**任何一处重置页码 —— 老代码 :1908 那处 `setWmPage(1)`'
        + '正是根因:它不受列表请求的 AbortController 约束,用户抢在 facets 回包前'
        + '点下一页,慢包回来把他打回第 1 页',
        asyncResets.map((s) => `${s.line}: ${s.text.slice(0, 60)}`).join(' | ') || '0 处');
    // 正样本臂:把老代码那一段原样喂进来,必须被识别(否则 A1 是空断言)
    const legacy = [
        "    authFetch('/x').then(r => r.json()).then(d => {",
        '      if (wmIndustry && industries.length > 0 && !industries.some(x => x.key === wmIndustry)) {',
        "        setWmIndustry(''); setWmPage(1);",
        '      }',
        '    });',
    ].join(String.fromCharCode(10));
    ok(classify(legacy).length === 1,
        'A1b 🔴 正样本臂:**老代码那一段**(facets 回包里 setWmPage(1))被识别出来'
        + ' —— 抓不到就说明 A1 是空断言');
    // 反臂:多行写法的同步 handler 不许误杀(这就是第一版的假阳性)
    const syncMultiline = [
        '                  <span onClick={() => {',
        '                    const maxPts = parseFloat(wmPriceMax) || 0;',
        '                    setWmPriceRange([0, maxPts]);',
        '                    setWmPage(1);',
        '                  }}>确定</span>',
    ].join(String.fromCharCode(10));
    ok(classify(syncMultiline).length === 0,
        'A1c 反臂:onClick 在上面几行的**多行同步 handler** 不误杀(换轴的直接理由)');
    // 反臂:钳位那一处**必须**存在(把它也删了就矫枉过正:越界页永远回不来)
    ok(/if \(st\.page !== requested\) setWmPage\(st\.page\)/.test(page)
        && /if \(st\.page !== requested\) setMhzPage\(st\.page\)/.test(page),
        'A2 反臂:两个 tab 的列表 .then 里各有一处**按回显 pages 的钳位** ——'
        + '不许连它也删掉(否则越界页永远回不来)');
    // facets 回包那一段里,一个页码设置都不许有
    const facetsStart = page.indexOf('wemedia/filters');
    const facetsBlock = page.slice(facetsStart, facetsStart + 1400);
    ok(!/setWmPage\(/.test(facetsBlock),
        'A3 🔴 facets 回包那一段里一个 setWmPage 都没有(病根所在的那一段)');
    ok(/shouldClearIndustry\(/.test(facetsBlock),
        'A4 它只回答"清不清行业",判定走纯函数');
}

// ══ B 陈旧回包丢弃 ═══════════════════════════════════════════════════
console.log('\nB 陈旧回包');
{
    ok(M.isStalePacket(1, 2) === true, 'B1 序号对不上 ⇒ 丢');
    ok(M.isStalePacket(2, 2) === false, 'B2 序号一致 ⇒ 收');
    ok(M.isStalePacket(undefined, 2) === true && M.isStalePacket('x', 2) === true,
        'B3 判不了的一律当陈旧(宁可丢一包,也不拿上一次的答案按在这一次上)');
    ok(/wmEpochRef/.test(page) && /mhzEpochRef/.test(page),
        'B4 两个 tab 都有筛选变更序号');
    ok(/isStalePacket\(epoch, wmEpochRef\.current\)/.test(page)
        && /isStalePacket\(epoch, mhzEpochRef\.current\)/.test(page),
        'B5 🔴 两个 tab 的 facets/filters 回包都过了这道闸 ——'
        + 'AbortController 管不到它们:facets 与列表是两个请求,'
        + '列表那个有自己的 controller,facets 那个此前谁也没管');
    const bumps = (page.match(/EpochRef\.current \+= 1/g) || []).length;
    ok(bumps === 2, 'B6 序号在每次筛选变更时递增(两个 tab 各一处)', `${bumps} 处`);
}

// ══ C 钳位只用这一次响应回显的 pages ═════════════════════════════════
console.log('\nC 钳位用回显值');
{
    const cells = [
        ['请求第 2 页 · 服务端说共 5 页、回显 2', 2, { page: 2, pages: 5 }, { page: 2, pages: 5, clamped: false }],
        ['请求第 2 页 · 新条件下只剩 1 页', 2, { page: 2, pages: 1 }, { page: 1, pages: 1, clamped: true }],
        ['请求第 7 页 · 共 3 页', 7, { page: 7, pages: 3 }, { page: 3, pages: 3, clamped: true }],
        ['服务端没回显 page(老接口) · 共 5 页', 2, { pages: 5 }, { page: 2, pages: 5, clamped: false }],
        ['服务端回显 page=1(它自己纠偏了)', 2, { page: 1, pages: 5 }, { page: 1, pages: 5, clamped: false }],
        ['pages=0(一条都没有)', 3, { page: 3, pages: 0 }, { page: 3, pages: 0, clamped: false }],
    ];
    let allOk = true;
    for (const [name, req, echo, want] of cells) {
        const got = M.reconcilePage(req, echo);
        const good = got.page === want.page && got.pages === want.pages && got.clamped === want.clamped;
        console.log(`     ${good ? '✓' : '✗'} ${name} ⇒ ${JSON.stringify(got)}`);
        if (!good) allOk = false;
    }
    ok(allOk, 'C1 🔴 钳位矩阵(6 格):越界按**这一次回显的** pages 钳,'
        + '不用旧 state —— 用旧 pages 钳等于拿上一次筛选的总页数判这一次的越界',
        `${cells.length} 格`);
    ok(M.reconcilePage(2, null).page === 2 && M.reconcilePage(2, undefined).pages === 0,
        'C2 边界:响应为空不抛、不乱钳');
    ok(/const st = reconcilePage\(requested, d\)/.test(page),
        'C3 接线:两个 tab 的 .then 都调它');
    const reqCapture = (page.match(/const requested = (wm|mhz)Page;/g) || []).length;
    ok(reqCapture === 2,
        'C4 🔴 发请求时**捕获当时的页码**(不是回包时再读 state)——'
        + '回包时 state 可能已经被用户点到别处了', `${reqCapture} 处`);
}

// ══ D 请求在途不给翻页 ═══════════════════════════════════════════════
console.log('\nD 在途禁点');
{
    ok(M.canGoNext({ page: 1, pages: 5, loading: false }) === true, 'D1 有下一页 ⇒ 可点');
    ok(M.canGoNext({ page: 5, pages: 5, loading: false }) === false, 'D2 最后一页 ⇒ 不可点');
    ok(M.canGoNext({ page: 1, pages: 5, loading: true }) === false,
        'D3 🔴 请求在途 ⇒ 不可点。切筛选后到回包前 `pages` 还是**上一次**的值,'
        + '照它放行就会请求一个越界页 ⇒ 后端返空 media ⇒ 屏幕看起来"没翻"');
    ok(M.canGoPrev({ page: 2, loading: false }) === true && M.canGoPrev({ page: 1, loading: false }) === false,
        'D4 上一页同理');
    ok(M.canGoPrev({ page: 2, loading: true }) === false, 'D5 上一页在途也不给点');
    const nexts = (page.match(/canGoNext\(\{/g) || []).length;
    const prevs = (page.match(/canGoPrev\(\{/g) || []).length;
    ok(nexts === 2 && prevs === 2,
        'D6 两个 tab 的四个翻页键都过这两个纯函数(各写一遍必有一处漂)',
        `next ${nexts} · prev ${prevs}`);
    ok(!/disabled=\{wmPage >= wmPages\}/.test(page) && !/disabled=\{mhzPage >= mhzPages\}/.test(page),
        'D7 🔴 旧的裸比较已不在(只加新判定不撤旧的 = 旧的照样放行)');
}

// ══ E 清行业的判定 ═══════════════════════════════════════════════════
console.log('\nE 行业回退');
{
    ok(M.shouldClearIndustry({ selected: 'edu', facetKeys: ['med', 'fin'] }) === true,
        'E1 所选行业不在新 facet 里 ⇒ 清');
    ok(M.shouldClearIndustry({ selected: 'edu', facetKeys: ['edu', 'fin'] }) === false,
        'E2 还在 ⇒ 不动');
    ok(M.shouldClearIndustry({ selected: '', facetKeys: [] }) === false, 'E3 本来就没选 ⇒ 不动');
    ok(M.shouldClearIndustry({ selected: 'edu', facetKeys: [] }) === false,
        'E4 🔴 facet 一条都没有时**不清** —— 那多半是这次没算出来,'
        + '不是"这个行业没了";清了用户就丢了一个他自己选的条件');
}

console.log('');
if (bad > 0) {
    console.log(`FAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('全部通过');
    process.exitCode = 0;
}
