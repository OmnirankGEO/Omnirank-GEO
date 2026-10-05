#!/usr/bin/env node
/**
 * 判据 · #178 §3 的 6 / 7 / 8 —— **真浏览器 · 真组件 · 真点击**。
 *
 * Review 09-12 明写「全部要行为臂;结构 grep 不算」。所以这里 esbuild 打真的
 * `SelectionPage.tsx`(连真 `sonner` 的 `<Toaster />`),在真 chromium 里用
 * `react-dom/client` 真挂载(effect 真跑、请求真发),用 Playwright 的 `page.route`
 * 在**浏览器网络层**回放后端 #178 契约的响应。
 *
 * 🔴 **最要紧的一条是 R9,不是"卡片在不在"**:
 *    卡片画出来很容易,难的是那张卡上的出口**真的通到一个允许态**。
 *    全排除后再打 submit-business-lines 会拿到一模一样的全排除结果 ——
 *    那是死循环,而"卡片在不在 / 按钮在不在"的锁会全绿。
 *    所以 R9 抓的是**点下去之后真正飞出去的那个请求**:
 *    必须是 `POST /submit-keywords` 且 body 里带客户刚写的那条词。
 *
 * 🔴 toast 不用桩:挂真 `<Toaster />`,直接问 DOM 里有没有那句话。
 *    (桩掉通知层就只能证明"我没调那个函数",证不了"客户没看到那句话"。)
 *
 * 跑法:cd frontend && node scripts/test-selection-all-excluded-render.mjs
 * 退出码:0=全绿 1=有红/判据不可用(**不可用一律非零** —— 跑不成和真绿必须分得开)
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync , mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
/*
 * 🔴 临时目录必须留在 `node_modules/.cache` **下面**:esbuild 解析 `react` 这类
 *    裸 import 是从 entry 所在目录逐级往上找 node_modules。放到系统 tmp 会当场
 *    「Could not resolve "react-dom/client"」——本机实测过。
 *    唯一性靠 mkdtemp 的随机后缀,不靠换盘。
 */
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const require_ = createRequire(import.meta.url);

let failed = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (cond, m, d = '') => (cond ? ok(m, d) : bad(m, d));
const section = (t) => console.log(`\n=== ${t} ===`);

function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 2000));
    process.exit(1);
}

let esbuild, playwright;
try {
    esbuild = require_('esbuild');
    playwright = require_('playwright');
} catch (err) { unusable('esbuild / playwright 取不到(npm ci --legacy-peer-deps)', err); }

/*
 * 🔴 [2026-09-13 · Review 复核抓到] 打包产物**必须每进程一份**,不能写死在
 *    `node_modules/.cache/<固定名>`。两处会咬人:
 *      ① 同一棵树里两把渲染闸同时跑(或注毒与另一把闸并行)——
 *         谁后 build,谁的 bundle 被对方的页面装走;
 *      ② 复核树用 junction 共享 node_modules —— 两棵树共用同一个 bundle.js,
 *         打了毒的那边可能装上干净包。
 *    后果最恶劣的形态:**注毒判决翻面**(Review 第一遍跑 MP5 是绿的,
 *    因为它的页面装的是我这边刚 build 出来的干净包)。红绿都不可信。
 *    ⇒ mkdtemp 一进程一份,跑完删。
 */
const outDir = mkdtempSync(join(CACHE_ROOT, 'a178-selection-render-'));
/* 🔴 a179-cleanup:跑完删掉这一份,免得 .cache 里堆满 bundle
   (一次全量注毒 = 17 发 × 两把闸 = 34 份)。进程怎么退出都删。 */
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });
const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

// ── 1. 入口:真组件 + 真 Toaster + MemoryRouter ──────────────────────
writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { Toaster } from 'sonner';
import { SelectionPage } from ${q('src/pages/Selection/SelectionPage.tsx')};

(globalThis as any).__mount = function (el: HTMLElement) {
    createRoot(el).render(
        <MemoryRouter initialEntries={['/s/tok-178']}>
          <Routes>
            <Route path="/s/:token" element={<SelectionPage />} />
          </Routes>
          <Toaster />
        </MemoryRouter>
    );
};
`, 'utf8');

const rawSuffixPlugin = {
    name: 'vite-raw-suffix',
    setup(build) {
        build.onResolve({ filter: /\?raw$/ }, (args) => {
            const bare = args.path.replace(/\?raw$/, '');
            const abs = bare.startsWith('@/') ? join(ROOT, 'src', bare.slice(2)) : join(args.resolveDir, bare);
            return { path: abs, namespace: 'vite-raw' };
        });
        build.onLoad({ filter: /.*/, namespace: 'vite-raw' }, (args) => ({
            contents: readFileSync(args.path, 'utf8'), loader: 'text',
        }));
    },
};

try {
    await esbuild.build({
        entryPoints: [join(outDir, 'entry.tsx')],
        bundle: true, outfile: join(outDir, 'bundle.js'), format: 'iife', platform: 'browser',
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        loader: {
            '.tsx': 'tsx', '.ts': 'ts',
            // 🔴 Vite 自己懂 `import x from './a.jpg'`，esbuild 不懂 —— 不配 loader 就是
            //    「打包失败 ⇒ 判据不可用」(而不是静默少一张图)。用 dataurl 而不是置空：
            //    样图本身就是 #188 要验的东西(「风格这里必须让人看到成品是什么样」)，
            //    置空会让所有样图臂在真缺陷下也照样绿。
            '.jpg': 'dataurl', '.jpeg': 'dataurl', '.png': 'dataurl',
            '.webp': 'dataurl', '.svg': 'dataurl',
        },
        plugins: [rawSuffixPlugin], logLevel: 'silent',
    });
} catch (err) { unusable('打包失败', (err && err.message) || err); }

/**
 * 🔴 **把刚构建出来的真 CSS 挂进来**,否则所有 computed-style 断言都在裸 DOM 上跑 ——
 *    R8e(原因行不跟着 disabled 的透明度变淡)与 R12d(主出口是实心按钮)会变成
 *    「绿得什么都没量」:没有 Tailwind 时 `opacity-40` 也照样量到 opacity=1。
 *    (这是我自己的判据病:`a-boolean-verdict-hides-green-that-measured-nothing`。)
 *    取法按本仓惯例读 `dist/assets` —— dist 是累积的,所以**按 mtime 取最新那份**;
 *    取不到就报「判据不可用」非零退出,绝不降级成"没量也算过"。
 */
let cssFile = '';
try {
    const assets = join(ROOT, 'dist', 'assets');
    const cands = readdirSync(assets)
        .filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(assets, f)).mtimeMs }))
        .sort((a, b) => b.m - a.m);
    if (!cands.length) throw new Error('dist/assets 里没有 index-*.css');
    cssFile = join(assets, cands[0].f);
    console.log(`  (真 CSS:dist/assets/${cands[0].f})`);
} catch (err) {
    unusable('取不到构建产物 CSS —— 样式臂没有真 CSS 就等于没量。'
        + '先跑 `npm run build`,再跑本判据', (err && err.message) || err);
}
writeFileSync(join(outDir, 'app.css'), readFileSync(cssFile, 'utf8'), 'utf8');
writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a178 harness</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>
`, 'utf8');

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = createServer((req, res) => {
    const name = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try {
        const buf = readFileSync(join(outDir, name));
        res.writeHead(200, { 'Content-Type': MIME[extname(name)] || 'application/octet-stream' });
        res.end(buf);
    } catch { res.writeHead(404); res.end('nope'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

// ── 2. 夹具:形状跟真契约同构 ────────────────────────────────────────
// 🔴 夹具少一个字段,抓到的红一半是自己造的(#176 那次 `{data:{}}` 的教训)。
const KNOWLEDGE_WORDS = [
    { id: 11, keyword: '钢琴怎么保养', category: 'knowledge', category_label: '知识', difficulty: 3, recommended: false },
    { id: 12, keyword: '钢琴有几个键', category: 'knowledge', category_label: '知识', difficulty: 2, recommended: false },
    { id: 13, keyword: '钢琴', category: 'other', category_label: '其他', difficulty: 5, recommended: false },
];
const BUSINESS_LINES = [
    { id: 1, name: '钢琴培训', description: '成人与儿童钢琴课', example_scenarios: ['钢琴怎么保养'], is_selected: false },
    { id: 2, name: '钢琴销售', description: '立式与三角钢琴', example_scenarios: ['钢琴有几个键'], is_selected: false },
];
const pageData = (over = {}) => ({
    brand_name: '韵宝钢琴', status: 'selecting',
    keywords: KNOWLEDGE_WORDS, business_lines: BUSINESS_LINES,
    selected_ids: [], custom_keywords: [],
    pricing_data: null, clusters_data: null, whitelabel: null,
    branding_status: 'platform', delivery_plan: null,
    ...over,
});
const EXCLUDED_ROWS = [
    { id: 11, keyword: '钢琴怎么保养', reason: '知识类问法，AI 不会因此推荐品牌', kind: 'knowledge_term_not_deliverable', policy_version: 'v1.0' },
    { id: 12, keyword: '钢琴有几个键', reason: '百科类问法，AI 不会因此推荐品牌', kind: 'knowledge_term_not_deliverable', policy_version: 'v1.0' },
    { id: 13, keyword: '钢琴', reason: '裸词，无法确认是否选型意图', kind: 'needs_clarification', policy_version: 'v1.0' },
];
const allExcludedBody = (notifySent) => ({
    success: true, all_excluded: true, status: 'business_lines_submitted',
    // C 09-12 更正:这里的 selected_count 是**业务方向数**(≥1),恒 0 的是 keywords_auto_selected
    selected_count: 1, keywords_auto_selected: 0,
    delivery_excluded_keywords: EXCLUDED_ROWS,
    next_action: { kind: 'add_commercial_keywords', notify_sent: notifySent },
});

/**
 * 起一页,按 plan 回放。记录**所有**发出去的 /api 请求(R9 要读它)。
 */
async function open(browser, plan = {}) {
    const page = await browser.newPage();
    const reqs = [];
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    /**
     * 🔴 桩必须**有状态** —— 真后端提交完会改 status,GET 下一次读到的是新状态。
     *    上一版桩的 GET 恒回 `status:'selecting'`(等于模拟了一个"提交完就忘"的后端),
     *    于是 R10b 对着正确代码报红:那是**夹具自己的窟窿**,差点被我读成被测代码的缺陷。
     *    有状态之后还顺带证到一件更要紧的事:全排除的卡片**活过了紧随其后的那次 fetchData**
     *    (GET 按 C 的契约带同名三字段;非全排除时那三个键一个都不带)。
     */
    const st = { status: 'selecting', selectedLines: [], getExtra: null };
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const path = url.pathname;
        let post = null;
        try { post = req.postDataJSON(); } catch { post = req.postData(); }
        reqs.push({ method: req.method(), path, post });
        const json = (body, status = 200) =>
            route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
        if (path === '/api/s/tok-178' && req.method() === 'GET') {
            if (plan.getData) return json(plan.getData);
            return json(pageData({
                status: st.status,
                business_lines: BUSINESS_LINES.map((bl) => ({
                    ...bl, is_selected: st.selectedLines.includes(bl.id),
                })),
                // C 09-12:非全排除时 GET **一个键都不加**(不是加 all_excluded:false)
                // plan.legacyGet = 模拟**还没带这三个字段的后端**(前端先上车 / 后端被回滚那一格)
                ...((plan.legacyGet ? null : st.getExtra) || {}),
            }));
        }
        if (path.endsWith('/submit-business-lines')) {
            if (plan.blStatus && plan.blStatus !== 200) {
                return json({ detail: plan.blDetail || '失败' }, plan.blStatus);
            }
            const body = plan.blBody || allExcludedBody(false);
            st.selectedLines = (post && post.selected_business_line_ids) || [];
            if (body.status) st.status = body.status;
            // C:「POST 与 GET 同名同形」—— reason 也必须跟着进 GET,漏一个字段
            //    卡片就会在刷取之后退回默认那档标题(那句话是在冤枉客户)
            st.getExtra = body.all_excluded === true
                ? {
                    all_excluded: true,
                    reason: body.reason || 'all_excluded',
                    delivery_excluded_keywords: body.delivery_excluded_keywords || [],
                    next_action: body.next_action || null,
                }
                : null;
            return json(body);
        }
        if (path.endsWith('/submit-keywords')) {
            const body = plan.kwBody || { success: true, status: 'keywords_submitted' };
            if (body.status) st.status = body.status;
            if (body.all_excluded !== true) st.getExtra = null;
            return json(body);
        }
        if (path.endsWith('/notify-no-deliverable')) {
            return plan.notifyFail
                ? json({ success: false }, 500)
                : json({ success: true, notify_sent: true, already_sent: false });
        }
        return json({ success: true });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate(() => (globalThis).__mount(document.getElementById('root')));
    await page.waitForSelector('text=请选择需要推广的业务', { timeout: 15000 });
    return { page, reqs, pageErrors };
}

const browser = await playwright.chromium.launch();
const seen = (page, sel) => page.locator(sel).count();
/**
 * 🔴 **全部 DOM 询问都报错不抛错**。Playwright 的 `isEnabled / isDisabled / innerText /
 *    click / fill` 在元素不存在时会等满超时然后**抛**,而注毒恰恰会让元素不存在 ——
 *    一抛,这一轮后面所有的臂连打印的机会都没有,非零退出码看着**像是抓住了**。
 *    (#176 的构造规则:每一条断言都必须在被毒状态下活得下来。今天这套脚本被
 *     P2/P3/P8 三发毒各撞了一次,才把它补全。)
 *    够不着时回 null / '' / 错误串,让断言自己判红。
 */
const TMO = { timeout: 2500 };
const isEnabledSafe = (loc) => loc.isEnabled(TMO).catch(() => null);
const isDisabledSafe = (loc) => loc.isDisabled(TMO).catch(() => null);
const textSafe = (loc) => loc.innerText(TMO).catch(() => '');
const oneLine = (e) => String((e && e.message) || e).split(String.fromCharCode(10))[0];
const clickSafe = (loc) => loc.click({ timeout: 4000 }).then(() => '').catch((e) => oneLine(e));
const fillSafe = (loc, v) => loc.fill(v, { timeout: 4000 }).then(() => '').catch((e) => oneLine(e));
/** 走完「勾一个业务方向 → 点确认」这段前置动作 */
async function submitBusinessLines(page) {
    await clickSafe(page.locator('button:has-text("钢琴培训")').first());
    await clickSafe(page.locator('button:has-text("确认业务选择")'));
}

try {
    // ══ R6 卡片 + 输入框 + 没有 toast + 没有那条绿条 ═══════════════════
    section('R6 全排除 ⇒ 出口卡(判据 6)');
    {
        const { page, pageErrors } = await open(browser);
        await submitBusinessLines(page);
        await page.waitForSelector('[data-testid="all-excluded-card"]', { timeout: 10000 })
            .catch(() => { });
        const cardN = await seen(page, '[data-testid="all-excluded-card"]');
        check(cardN === 1, 'R6a 出口卡渲染出来了', `${cardN} 张`);
        const inputN = await seen(page, '[data-testid="all-excluded-card"] input[type="text"]');
        check(inputN >= 1, 'R6b 出口①的自定义词输入框就在卡片里', `${inputN} 个`);
        // 逐条原因可见 + 两类分开
        // 报错不抛错:毒把卡片删掉时 innerText 会等 30s 然后抛,后面的臂全被连带删掉
        const body = await textSafe(page.locator('[data-testid="all-excluded-card"]'));
        check(EXCLUDED_ROWS.every((r) => body.includes(r.keyword)),
            'R6c 三条被排除的词逐条可见(不静默缩减)');
        check(body.includes('知识类问法，AI 不会因此推荐品牌'), 'R6d 逐条原因可见');
        check(/改写清楚后可以再提交/.test(body),
            'R6e 🔴 needs_clarification 那条**单列**并说"可以再提交"(与不可改选的那类分开)');
        // 🔴 toast 不许出现(真 Toaster 在页面上,问的是 DOM)
        const toastTxt = await page.locator('[data-sonner-toast]').count();
        check(toastTxt === 0, 'R6f 🔴 一个 toast 都没弹(老逻辑正是靠 toast 把客户送进死胡同)',
            `${toastTxt} 个`);
        // 🔴 那条骗人的绿条不许挂
        const lie = await page.locator('text=我们正在为您准备专属关键词方案').count();
        check(lie === 0,
            'R6g 🔴🔴 「已提交 · 正在准备方案」绿条**没有**挂 —— 没有人在准备方案,'
            + '挂着它客户会安心去等一个永远不来的方案', `${lie} 条`);
        check(pageErrors.length === 0, 'R6h 零 pageerror(崩溃会把上面几条一起变成假信号)',
            pageErrors.join(' | ') || '干净');
        await page.close();
    }

    // ══ R7 出口②(判据 7) ═════════════════════════════════════════════
    section('R7 让报价方补充(判据 7)');
    {
        const { page } = await open(browser, { blBody: allExcludedBody(false) });
        await submitBusinessLines(page);
        await page.waitForSelector('[data-testid="notify-button"]', { timeout: 10000 }).catch(() => { });
        const btn = page.locator('[data-testid="notify-button"]');
        check(await btn.count() === 1, 'R7a notify_sent=false ⇒ 按钮在');
        check(await isEnabledSafe(btn) === true, 'R7b 按钮可点');
        await clickSafe(btn);
        await page.waitForSelector('[data-testid="notify-done"]', { timeout: 10000 }).catch(() => { });
        check(await seen(page, '[data-testid="notify-done"]') === 1,
            'R7c 点完转「已通知报价方」态');
        check(await seen(page, '[data-testid="notify-button"]') === 0,
            'R7d 转态后按钮收掉(不给重复打扰报价方)');
        await page.close();
    }
    {
        const { page } = await open(browser, { blBody: allExcludedBody(true) });
        await submitBusinessLines(page);
        await page.waitForSelector('[data-testid="all-excluded-card"]', { timeout: 10000 }).catch(() => { });
        check(await seen(page, '[data-testid="notify-done"]') === 1,
            'R7e notify_sent=true ⇒ 直接显示已通知(后端已经推过了)');
        check(await seen(page, '[data-testid="notify-button"]') === 0, 'R7f 此时不给按钮');
        await page.close();
    }
    {
        const { page } = await open(browser, { blBody: allExcludedBody(false), notifyFail: true });
        await submitBusinessLines(page);
        await page.waitForSelector('[data-testid="notify-button"]', { timeout: 10000 }).catch(() => { });
        await clickSafe(page.locator('[data-testid="notify-button"]'));
        await page.waitForTimeout(600);
        check(await seen(page, '[data-testid="notify-done"]') === 0,
            'R7g 🔴 通知失败**不许**谎称已通知(静默成功是这类按钮最常见的假绿)');
        check(await page.locator('[data-sonner-toast]').count() >= 1,
            'R7h 失败照实说(这一条才该用 toast:它是动作失败,不是状态说明)');
        await page.close();
    }

    // ══ R8 提交闸 + 就地原因(判据 8) ═══════════════════════════════════
    section('R8 零可交付 ⇒ 禁用 + 旁边有原因(判据 8)');
    {
        const { page } = await open(browser);
        await submitBusinessLines(page);
        await page.waitForSelector('[data-testid="all-excluded-card"]', { timeout: 10000 }).catch(() => { });
        const submit = page.locator('button:has-text("提交新增问法")');
        check(await submit.count() === 1, 'R8a 全排除态下提交键换成「提交新增问法」');
        check(await isDisabledSafe(submit) === true, 'R8b 还没加词 ⇒ 点不动');
        const reason = page.locator('[data-testid="bottom-bar-disabled-reason"]');
        check(await reason.count() === 1, 'R8c 🔴 原因就画在按钮旁边(不是 toast、不是 title)');
        const rtxt = await textSafe(reason);
        check(/加一条|报价方/.test(rtxt), 'R8d 原因里给的是**下一步**,不是"不可提交"', rtxt);
        // 🔴 原因必须真的看得见(#179 那条缺陷:理由跟着 50% 透明度一起变淡)
        //    这里**报错不抛错**:毒把原因行删掉时 evaluate 会等 30s 然后抛,
        //    整个脚本当场死 —— R9/R10/R11 连跑的机会都没有,而非零退出码看着像抓住了。
        const vis = await reason.evaluate((el) => {
            const s = getComputedStyle(el);
            const r = el.getBoundingClientRect();
            return { opacity: Number(s.opacity), w: r.width, h: r.height, display: s.display };
        }, undefined, { timeout: 3000 }).catch(() => ({ opacity: -1, w: 0, h: 0, display: 'MISSING' }));
        check(vis.opacity >= 0.9 && vis.w > 0 && vis.h > 0 && vis.display !== 'none',
            'R8e 🔴 原因行是**清晰可读**的(不跟着按钮的 disabled 透明度一起变淡 ——'
            + ' #179 的根因①就是这个)', JSON.stringify(vis));
        await page.close();
    }

    // ══ R9 🔴 出口①真的通:点下去飞出的是 submit-keywords ═══════════════
    section('R9 出口①落在真的允许态上(本单最要紧的一条)');
    {
        const { page, reqs } = await open(browser);
        await submitBusinessLines(page);
        await page.waitForSelector('[data-testid="all-excluded-card"]', { timeout: 10000 }).catch(() => { });
        const input = page.locator('[data-testid="all-excluded-card"] input[type="text"]').first();
        await fillSafe(input, '韵宝钢琴怎么样');
        await clickSafe(page.locator('[data-testid="all-excluded-card"] button:has-text("添加")'));
        await page.waitForTimeout(300);
        const submit = page.locator('button:has-text("提交新增问法")');
        check(await isEnabledSafe(submit) === true, 'R9a 加了一条商业问法 ⇒ 闸放开');
        check(await seen(page, '[data-testid="bottom-bar-disabled-reason"]') === 0,
            'R9b 放开后原因行收掉(还挂着就是自相矛盾)');
        const before = reqs.length;
        /**
         * 🔴 **报错不抛错**。上一版这里是裸 `await submit.click()`:注毒(P8 断开 onAdd)
         *    之后这个键本该点不动,于是 Playwright 抛 TimeoutError,**整个脚本当场死**——
         *    R9c/R9d/R9e 连打印的机会都没有,而非零退出码看起来**像是抓住了**。
         *    (#176 同一课:每一条断言都必须在被毒状态下活得下来,否则毒把你要验的臂
         *     连带删掉了。)所以点击失败只记账,后面的臂照跑,各自报自己的红。
         */
        const clickErr = await clickSafe(submit);
        if (clickErr) console.log(`       (点击没成功:${clickErr.slice(0, 80)} —— 下面几条会照实报红)`);
        await page.waitForTimeout(800);
        const fired = reqs.slice(before).filter((r) => r.method === 'POST' && /\/submit-/.test(r.path));
        check(fired.length >= 1, 'R9c 点下去真的发出了提交请求', fired.map((f) => f.path).join(' '));
        const kw = fired.find((f) => f.path.endsWith('/submit-keywords'));
        check(!!kw,
            'R9d 🔴🔴 飞出去的是 **submit-keywords**(该端点允许态含 business_lines_submitted)'
            + ' —— 若是 submit-business-lines 就会拿到一模一样的全排除结果,死胡同变死循环',
            fired.map((f) => f.path.split('/').pop()).join(' ') || '无');
        const carried = !!kw && Array.isArray(kw.post?.custom_keywords)
            && kw.post.custom_keywords.includes('韵宝钢琴怎么样');
        check(carried,
            'R9e 🔴 客户刚写的那条词**真的进了请求体**(输入框能打字但加不进去 = 死输入框)',
            kw ? JSON.stringify(kw.post?.custom_keywords) : 'n/a');
        await page.close();
    }

    // ══ R10 反臂:不在这一态时老路径一点没变 ════════════════════════════
    section('R10 反臂(老路径零漂移)');
    {
        const { page } = await open(browser, {
            blBody: { success: true, status: 'keywords_submitted', selected_count: 3, delivery_excluded_keywords: [] },
        });
        await submitBusinessLines(page);
        await page.waitForTimeout(1200);
        check(await seen(page, '[data-testid="all-excluded-card"]') === 0,
            'R10a 正常提交 ⇒ 不弹出口卡');
        /**
         * 🔴 换锚不换意图:上一版这里断言那条绿条「照挂」。桩改成有状态之后才看清 ——
         *    正常提交后 status 变 keywords_submitted,页面**整支换成等报价屏**,
         *    那条绿条只在 POST 回来到 GET 回来之间闪一下,它不是好路径的终点。
         *    所以锚从"闪一下的绿条"挪到**真正的目的地**:客户到达等报价屏。
         *    这比原来的锚更强(它断言了真的有进展),且绿条自己的正分支
         *    仍由纯函数臂 B10b(shouldClaimSubmitted 正常态返 true)守着,覆盖没丢。
         */
        const waiting = await page.locator('text=已提交').count()
            + await page.locator('text=等待').count()
            + await page.locator('text=方案').count();
        check(waiting >= 1,
            'R10b 🔴 正常提交 ⇒ 页面真的往前走到等报价屏(好路径没被我掐掉)',
            `命中 ${waiting} 处等待态文案`);
        check(await page.locator('text=请选择需要推广的业务').count() === 0,
            'R10c0 正常提交后**不**还停在业务方向步(停在原地就是没进展)');
        await page.close();
    }
    {
        // 部分排除(有排除条目、但后端没说 all_excluded)—— 老行为:横幅照走,卡片不弹
        const { page } = await open(browser, {
            blBody: {
                success: true, status: 'keywords_submitted', selected_count: 2,
                delivery_excluded_keywords: [EXCLUDED_ROWS[0]],
            },
        });
        await submitBusinessLines(page);
        await page.waitForTimeout(800);
        check(await seen(page, '[data-testid="all-excluded-card"]') === 0,
            'R10c 🔴 部分排除**不**弹出口卡(否则每次正常提交都弹一张"死胡同"卡)');
        await page.close();
    }
    // ══ R11 🔴 后端还没带 GET 字段时(前后端不同车 / 后端被回滚)卡片也得活过刷取 ═══
    section('R11 与"还没带 GET 字段的后端"共存(跨车偏斜格)');
    {
        const { page } = await open(browser, { legacyGet: true });
        await submitBusinessLines(page);
        // POST 立卡 → 紧随其后的 fetchData 回来(GET 一个新字段都没带)
        await page.waitForSelector('[data-testid="all-excluded-card"]', { timeout: 10000 }).catch(() => { });
        await page.waitForTimeout(1200);
        check(await seen(page, '[data-testid="all-excluded-card"]') === 1,
            'R11a 🔴 GET 不带那三个字段时,POST 给的卡片**活过**了紧随其后的 fetchData ——'
            + '无条件覆盖会让客户只看见卡片闪一下,然后退回死胡同,且零报错',
            '1 张');
        check(await page.locator('text=我们正在为您准备专属关键词方案').count() === 0,
            'R11b 这一格下那条骗人的绿条也不许挂');
        await page.close();
    }
    // ══ R12 第二档 reason:这个方向一条候选词都没生成(C #178-B) ═══════════
    section('R12 no_keywords_for_lines:主出口换人');
    {
        const { page } = await open(browser, {
            blBody: {
                success: true, all_excluded: true, reason: 'no_keywords_for_lines',
                status: 'business_lines_submitted', selected_count: 1, keywords_auto_selected: 0,
                delivery_excluded_keywords: [],   // C:这一档恒为空 —— 那里一条词都没有
                next_action: { kind: 'add_commercial_keywords', notify_sent: false },
            },
        });
        await submitBusinessLines(page);
        await page.waitForSelector('[data-testid="all-excluded-card"]', { timeout: 10000 }).catch(() => { });
        const title = await textSafe(page.locator('[data-testid="all-excluded-title"]'));
        check(/还没有配好的问法/.test(title) && !/不会让 AI 推荐/.test(title),
            'R12a 🔴 标题不说"你的问法被排除了" —— 那里一条词都没有,说这话是冤枉客户', title);
        const sub = await textSafe(page.locator('[data-testid="all-excluded-subtitle"]'));
        check(/不是你写错了/.test(sub), 'R12b 解释也跟着换(半句真话比整句假话更难发现)', sub);
        /**
         * 🔴 主出口的先后用**真 DOM 几何**量,不看类名:
         *    order-1/order-2 只对 flex 子元素生效,父容器不是 flex 时这两个类名
         *    什么都不做 —— 只有问"谁画在上面"才看得出来。
         */
        const box = async (sel) => page.locator(sel).boundingBox({ timeout: 2500 })
            .catch(() => null);
        const bNotify = await box('[data-testid="exit-notify"]');
        const bAdd = await box('[data-testid="exit-add-keywords"]');
        check(!!bNotify && !!bAdd && bNotify.y < bAdd.y,
            'R12c 🔴🔴 「让报价方补充」画在「自己加一条」**上面**(这一档该动的是报价方;'
            + '把"自己写一条"摆成主出口等于把配词推给客户)',
            bNotify && bAdd ? `notify y=${Math.round(bNotify.y)} · add y=${Math.round(bAdd.y)}` : '取不到位置');
        const btnStyle = await page.locator('[data-testid="notify-button"]').evaluate((el) => {
            const cs = getComputedStyle(el);
            return { fs: parseFloat(cs.fontSize), bg: cs.backgroundColor };
        }, undefined, { timeout: 2500 }).catch(() => ({ fs: 0, bg: 'MISSING' }));
        check(btnStyle.fs >= 13 && !/rgba\(0, 0, 0, 0\)|transparent/.test(btnStyle.bg),
            'R12d 主出口按钮是实心且字号不小(主出口长得像次要按钮 = 没人点)',
            JSON.stringify(btnStyle));
        check(await seen(page, '[data-testid="all-excluded-card"] li') === 0,
            'R12e 逐条清单为空时不画空 bullet(这一档 delivery_excluded_keywords 恒为 [])');
        await page.close();
    }
} finally {
    await browser.close();
    server.close();
}

console.log('');
if (failed > 0) {
    console.log(`FAIL ${failed} 项不通过`);
    process.exit(1);
}
console.log('全部通过');
process.exit(0);
