#!/usr/bin/env node
/**
 * 判据 · #236-c1c 前端半 —— 顶部「真实问题 N 个」必须是**题数**,不是关键词数。
 *
 * 🔴 缺陷原状:`mapDto.ts` 里 `testedQuestionCount: keywordCount` ——
 *    **根本没读后端字段**,而紧挨着的上一行 `testedPlatformCount` 是读的。
 *    一个读后端、一个自己顶,挨着写的。
 *    #700 真客户报告上:顶部「真实问题 1 个」(该品牌只有 1 个关键词)、
 *    方法说明「本次实测 3 个问题」、四个平台各「3 个问题」—— **顶部那个与谁都对不上**。
 *
 * 🔴 **真浏览器臂**(Owner 2026-09-18 新规:做完必须调用前端真实测试)。
 *    走的是完整链:原始 DTO → `mapDtoToPresentation`(我改的那一段)→ 真渲染 → 读屏。
 *    只做文本匹配的话,「读了字段但渲染时又顶回去」这种改动看不见。
 *
 * 🔴 **不碰真客户页面**:标准里写死「真客户品牌一律禁碰、截图只用 QA/is_test 画面」。
 *    所以这一臂用**本地夹具**跑,不去开 #700 那张真客户报告。
 *    生产页面上的三处一致性由复审在浏览器里自查(他已声明要自己看过再裁定)。
 *    本臂反而比线上单点更有分辨力 —— 它能构造「关键词数 ≠ 题数」与「两者相等」两种夹具。
 *
 * 三态退出:0 全绿 · 1 有判据红 · 3 门自己没跑成(打包/浏览器取不到)。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);

let failures = 0;
let ran = 0;
const ok = (cond, name, detail) => {
    ran += 1;
    if (!cond) failures += 1;
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${name}${detail !== undefined ? ` — ${detail}` : ''}`);
};
function cannotRun(what, err) {
    console.log(`\n3 门没跑成:${what} —— ${String((err && err.message) || err).slice(0, 300)}`);
    console.log('   (退出码 3 = 本门这次**没有测过任何东西**,不要当成通过)');
    process.exit(3);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { cannotRun('esbuild / playwright 取不到', err); }

/*
 * 🔴 [2026-09-18] `node_modules/.cache` 是**构建产物**,全新 `npm ci` 的树里不存在。
 *    原来直接 mkdtemp 进去 ⇒ 在干净树上 ENOENT 崩成 **rc=1**(读作"有判据红了"),
 *    而真相是**门根本没跑起来**。我本机有这个目录,所以恒绿 ——
 *    与「浏览器门进 build 链」同形:这条臂只在它被写出来的那个环境里跑得动。
 * 🔴 只加 mkdir 是修一半:建目录这一步本身也要**包进三态**,
 *    否则任何发生在三态机制之外的失败都会伪装成"判据红了"。
 */
let outDir;
try {
    const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
    mkdirSync(CACHE_ROOT, { recursive: true });
    outDir = mkdtempSync(join(CACHE_ROOT, 'c1c-'));
} catch (err) { cannotRun('临时目录建不出来(node_modules/.cache 不存在且建不了)', err); }
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { mapDtoToPresentation } from ${q('src/features/publicReportPremium/transport/mapDto.ts')};
import { HeroSummary } from ${q('src/features/publicReportPremium/components/sections/HeroSummary.tsx')};

let root: any = null;
(globalThis as any).__mount = function (dto: any, nonce: number) {
    if (!root) root = createRoot(document.getElementById('root')!);
    /* 🔴 真调 mapDto:这一臂测的就是 DTO → 映射 → 渲染这条链,
       只喂映射后的对象等于把被测的那一段跳过去。 */
    const report = mapDtoToPresentation(dto);
    root.render(<div key={String(nonce)}><HeroSummary report={report} /></div>);
};
`, 'utf8');

try {
    await esbuild.build({
        entryPoints: [join(outDir, 'entry.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        loader: { '.tsx': 'tsx', '.ts': 'ts', '.svg': 'dataurl', '.png': 'dataurl' },
        logLevel: 'silent',
    });
} catch (err) { cannotRun('打包失败', err); }

writeFileSync(join(outDir, 'index.html'),
    '<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>c1c</title></head>'
    + '<body><div id="root"></div><script src="./bundle.js"></script></body></html>', 'utf8');

/** 造一份最小 DTO;`presentation.summary.testedQuestionCount` 是本单的被测字段。 */
const dto = (keywordCount, questionCount) => ({
    brand_name: 'QA 夹具客户',
    score: 61,
    level: '成长级',
    data_completeness_score: 70,
    keyword_count: keywordCount,
    report_version: 'v2',
    presentation: {
        summary: {
            testedPlatformCount: 4,
            validAnswerCount: 12,
            ...(questionCount === undefined ? {} : { testedQuestionCount: questionCount }),
        },
    },
});

const server = createServer((req, res) => {
    const name = (req.url || '/').split('?')[0] === '/' ? '/index.html' : (req.url || '').split('?')[0];
    try {
        const body = require_('node:fs').readFileSync(join(outDir, name.slice(1)));
        res.writeHead(200, { 'Content-Type': name.endsWith('.js') ? 'text/javascript' : 'text/html' });
        res.end(body);
    } catch { res.writeHead(404); res.end(); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

let browser, page;
try {
    browser = await playwright.chromium.launch();
    page = await browser.newPage();
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
} catch (err) { cannotRun('chromium 起不来', err); }

/** 读「真实问题」那一格旁边的数字。 */
async function readQuestionMetric(keywordCount, questionCount, nonce) {
    await page.evaluate(() => { globalThis.__lastMetric = null; globalThis.__stableTicks = 0; });
    await page.evaluate(([d, n]) => globalThis.__mount(d, n), [dto(keywordCount, questionCount), nonce]);
    /*
     * 🔴 `HeroMetric` 用 `useCountUp` 把数字**滚动**上去。第一版在 300ms 就读,
     *    读到的是**中途值**:题数 5 那一格读成了 4,而 4 恰好等于同屏的平台数,
     *    看起来像「抓错了格」。取数窗口比被测过程快 ⇒ 读数在变化中途被截断。
     * ⇒ 等它**停止变化**再读,而不是等一个固定时长,也不是等"期望值出现"
     *   (后者会把判据变成自证预言:只要期望值在动画里出现过一瞬就绿)。
     */
    await page.waitForFunction(() => {
        const nodes = Array.from(document.querySelectorAll('*'));
        const label = nodes.find((el) => el.children.length === 0 && el.textContent.trim() === '真实问题');
        if (!label) return false;
        const now = (label.closest('div') || {}).textContent || '';
        const w = globalThis;
        const stable = w.__lastMetric === now ? (w.__stableTicks || 0) + 1 : 0;
        w.__lastMetric = now;
        w.__stableTicks = stable;
        return stable >= 3;          /* 连续三次读数不变 ⇒ 动画停了 */
    }, null, { timeout: 8000, polling: 120 }).catch(() => {});
    return page.evaluate(() => {
        const nodes = Array.from(document.querySelectorAll('*'));
        const label = nodes.find((el) => el.children.length === 0 && el.textContent.trim() === '真实问题');
        if (!label) return { found: false, text: '(页面上没有「真实问题」这一格)' };
        /* 数字在同一格里、标签的兄弟节点上 */
        const box = label.closest('div');
        return { found: true, text: (box ? box.textContent : '').replace(/\s+/g, ' ').trim() };
    });
}

console.log('#236-c1c 顶部「真实问题 N 个」:N 必须是题数,不是关键词数');

/* ── Q1 正臂:关键词 1 / 题 3 ⇒ 顶部必须是 3 ────────────────────────── */
{
    const r = await readQuestionMetric(1, 3, 1);
    ok(r.found, 'Q0 分母自证:「真实问题」那一格真的渲染出来了(找不到的话下面每格都恒真)', r.text);
    ok(r.found && /\b3\b/.test(r.text) && !/\b1\b/.test(r.text.replace('真实问题', '')),
        'Q1 🔴 关键词数 1 / 题数 3 ⇒ 顶部显示 **3** —— 这正是 #700 真客户报告上「1 vs 3 vs 3」那一幕',
        r.text);
}

/* ── Q2 反臂:两者相等时也要对 ──────────────────────────────────────
 * 🔴 复审点名的那一格:关键词数恰好等于题数的报告,改完后仍正确 ——
 *    否则「把顶部也写死成方法说明那个数」这种假修也会通过。
 *    两者相等的夹具对这个缺陷**天生没分辨力**,所以它只能当反臂,不能当正臂。 */
{
    const r = await readQuestionMetric(5, 5, 2);
    ok(r.found && /\b5\b/.test(r.text),
        'Q2 反臂:关键词数 == 题数(都是 5)时仍显示 5 —— '
        + '这一格**证明不了修对了**(两者相等时对错同形),它证明的是没改坏',
        r.text);
}

/* ── Q3 反臂:后端没给这个字段时,**不许回落成关键词数** ────────────── */
{
    const r = await readQuestionMetric(7, undefined, 3);
    ok(r.found && !/\b7\b/.test(r.text),
        'Q3 🔴 反臂:后端**没给**题数时不许顶一个关键词数上去 —— '
        + '拿关键词数冒充题数正是本单要治的病,在「后端没给」时再顶一次等于留一条后路',
        r.text);
}

await browser.close();
server.close();

console.log('');
console.log(`跑满 ${ran} 条判据`);
if (failures > 0) { console.log(`FAIL ${failures} 条红`); process.exit(1); }
console.log('全部通过');
process.exit(0);
