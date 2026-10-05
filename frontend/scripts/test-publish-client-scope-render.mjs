#!/usr/bin/env node
/**
 * 判据 · #180 —— 发布中心的客户范围(真 chromium · 真组件 · 真请求)。
 *
 * 🔴 要证的不是"下拉框没了",是**页面到底在替谁工作**:
 *    老代码自己维护一份 selectedProject,兜底落 `list[0]` ——
 *    左上角显示 B、页面在 A 上取文章、提交体的 brand_id 又取页面这一份,
 *    「显示对、记错人」。所以每条臂都看**真实发出的请求**(取的是谁的报价单),
 *    而不是屏幕上那行字。
 *
 * 跑法:cd frontend && npm run build && node scripts/test-media-list-page-race.mjs
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
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
const section = (t) => console.log(`\n=== ${t} ===`);
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 2000));
    process.exit(1);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { unusable('esbuild / playwright 取不到', err); }

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
const outDir = mkdtempSync(join(CACHE_ROOT, 'a180-scope-'));
/* 🔴 a179-cleanup:跑完删掉这一份,免得 .cache 里堆满 bundle
   (一次全量注毒 = 17 发 × 两把闸 = 34 份)。进程怎么退出都删。 */
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });
const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from ${q('src/context/AuthContext.tsx')};
import { OrganizationProvider } from ${q('src/context/OrganizationContext.tsx')};
import { UserModeProvider } from ${q('src/context/UserModeContext.tsx')};
import { WalletProvider } from ${q('src/context/WalletContext.tsx')};
import { OnboardingProvider } from ${q('src/context/OnboardingContext.tsx')};
import { PricingProvider } from ${q('src/context/PricingContext.tsx')};
import { ClientProvider } from ${q('src/context/ClientContext.tsx')};
import { PublishCenter } from ${q('src/pages/Publishing/PublishCenter.tsx')};
import OnlineQuoteFlow from ${q('src/pages/Quote/OnlineQuoteFlow.tsx')};
import { ClientSwitcherSidebar } from ${q('src/components/layout/ClientSwitcherSidebar.tsx')};

(globalThis as any).__mountQuote = function (el: HTMLElement) {
    createRoot(el).render(
        <MemoryRouter initialEntries={['/pricing']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
              <OnlineQuoteFlow />
            </ClientProvider></PricingProvider></OnboardingProvider>
          </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
        </MemoryRouter>
    );
};

(globalThis as any).__mount = function (el: HTMLElement, entry?: string) {
    createRoot(el).render(
        <MemoryRouter initialEntries={[entry || '/publish']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              {/* 🔴 把**真的左上角**一起挂上:本单说的就是"跟着左上角走",
                  用真控件建立前置状态,判据才不是拿我自己新加的控件自证。 */}
              <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
              <PublishCenter />
            </ClientProvider></PricingProvider></OnboardingProvider>
          </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
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

let cssName = '';
try {
    const assets = join(ROOT, 'dist', 'assets');
    const cands = readdirSync(assets).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(assets, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!cands.length) throw new Error('dist/assets 里没有 index-*.css');
    cssName = cands[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(assets, cssName), 'utf8'), 'utf8');
    console.log(`  (真 CSS:dist/assets/${cssName})`);
} catch (err) {
    unusable('取不到构建产物 CSS —— 先跑 `npm run build`', (err && err.message) || err);
}

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a180 harness</title>
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


// ── 夹具 ────────────────────────────────────────────────────────────
const mkMe = (isAdmin) => ({
    success: true,
    user: { id: 112, username: 'qa', role: isAdmin ? 'admin' : 'user', agent_level: 1, is_admin: isAdmin, permissions: [], user_mode: 'agent' },
    id: 112, username: 'qa', role: isAdmin ? 'admin' : 'user', agent_level: 1,
    is_admin: isAdmin, permissions: [], user_mode: 'agent',
});
const CLIENTS = [
    { id: 101, name: 'A 客户', industry: '教育培训', diagnosis_count: 1, quote_count: 1 },
    { id: 202, name: 'B 客户', industry: '医疗美容', diagnosis_count: 1, quote_count: 1 },
    { id: 303, name: 'C 客户没有写作项目', industry: '餐饮', diagnosis_count: 1, quote_count: 0 },
];
const ctxFor = (id) => ({
    brand: {
        id,
        name: (CLIENTS.find((c) => c.id === id) || {}).name || '',
        industry: (CLIENTS.find((c) => c.id === id) || {}).industry || '',
        diagnosis_count: 1,
    },
    profile: null,
});
/** 🔴 303 **故意没有**写作项目 —— 那正是老代码会悄悄落到 list[0] 的那一格。 */
const PROJECTS = [
    { id: 11, brand_id: 101, brand_name: 'A 客户', industry: '教育培训', quote_ids: [11, 12], keyword_count: 5 },
    { id: 21, brand_id: 202, brand_name: 'B 客户', industry: '医疗美容', quote_ids: [21], keyword_count: 3 },
];

const clickSafe = (l) => l.click({ timeout: 5000 }).then(() => '').catch((e) => String((e && e.message) || e).split(String.fromCharCode(10))[0]);
const textSafe = (l) => l.innerText({ timeout: 2500 }).catch(() => '');
const seen = (page, sel) => page.locator(sel).count();

async function open(browser, plan = {}) {
    const page = await browser.newPage();
    const reqs = [];
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    await page.addInitScript((bid) => {
        localStorage.setItem('omnirank_token', 'qa-token');
        if (bid) sessionStorage.setItem('omnirank_current_brand_candidate:112', String(bid));
    }, plan.storedBrandId || 0);
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const path = url.pathname;
        reqs.push({ method: req.method(), path });
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        if (path.includes('/auth/me')) return json(mkMe(!!plan.isAdmin));
        if (path === '/api/client-context/list') return json({ success: true, clients: CLIENTS });
        const m = path.match(/^\/api\/client-context\/(\d+)$/);
        if (m) return json({ success: true, context: ctxFor(Number(m[1])) });
        if (path === '/api/writing/projects') return json({ projects: PROJECTS });
        if (/^\/api\/placement\/articles\/\d+$/.test(path)) {
            return json({ articles: [{ id: 1, topic_id: 1, article_id: 1, status: 'completed', title: 'a', keyword: 'k' }] });
        }
        return json({ status: 'success', success: true, data: [], items: [], projects: [], total: 0 });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    /**
     * 🔴 深链要从 **MemoryRouter 的 initialEntries** 进,不能改 window.history ——
     *    MemoryRouter 有自己的历史栈,`useSearchParams` 读的是它,不是浏览器地址栏。
     *    第一版用 history.replaceState,于是 `?brand_id=101` **根本没到组件手里**,
     *    S2 那两条等于在测空气(而且看着像"反向同步没生效")。
     */
    await page.evaluate((entry) => {
        (globalThis).__mount(document.getElementById('root'), entry);
    }, '/publish' + (plan.search || ''));
    await page.waitForTimeout(2600);
    return { page, reqs, pageErrors };
}

/**
 * 通过**真的左上角**选一个客户。
 *
 * 🔴 为什么不预先塞 sessionStorage:`ClientContext` 在 `/auth/me` 还没回来
 *    (`userId === null`)时会把**所有** `omnirank_current_brand_candidate:*` 键
 *    一律清掉(:171-178 那段 sweep)。预塞的值活不到 auth 解析完 ——
 *    第一版就是这么让 S1/S2/S5 一起红的,而那是**夹具**的问题不是被测代码的。
 */
async function pickClientInSidebar(page, name) {
    const bar = page.locator('[data-testid="real-sidebar"]');
    await clickSafe(bar.locator('button').first());          // 展开切换器
    await page.waitForTimeout(400);
    const err = await clickSafe(bar.locator(`text=${name}`).first());
    await page.waitForTimeout(1600);
    return err;
}

/** 取过哪些报价单的文章 —— **请求参数骗不了人**,屏幕上那行字骗得了。 */
const quotesFetched = (reqs) => reqs
    .filter((r) => /^\/api\/placement\/articles\/\d+$/.test(r.path))
    .map((r) => Number(r.path.split('/').pop()));

const browser = await playwright.chromium.launch();
try {
    // ══ S1 跟随左上角 ═════════════════════════════════════════════════
    section('S1 页面跟着左上角走');
    {
        const { page, reqs, pageErrors } = await open(browser, {});
        const pickErr = await pickClientInSidebar(page, 'B 客户');
        check(pageErrors.length === 0, 'S1a 零 pageerror', pageErrors.join(' | ').slice(0, 200) || '干净');
        check(!pickErr, 'S1a1 夹具自证:真的用**左上角**选上了 B(选不上的话下面全是假信号)',
            pickErr || '选上了');
        const qs = quotesFetched(reqs);
        console.log('       [debug] 请求路径:' + JSON.stringify([...new Set(reqs.map(r => r.path))]));
        console.log('       [debug] #root 文本:' + (await page.locator('#root').innerText().catch(() => '')).slice(0, 300).replace(/\s+/g, ' '));
        check(qs.length >= 1, 'S1a0 真的取过文章(分母非空)', JSON.stringify(qs));
        check(qs.length >= 1 && qs.every((q) => q === 21),
            'S1b 🔴🔴 左上角是 B(202)⇒ 只取 **B 的报价单 21** 的文章。'
            + '老代码兜底 list[0] 会取 A 的 11/12 —— 那就是"显示对、记错人"',
            JSON.stringify(qs));
        check(/B 客户/.test(await textSafe(page.locator('[data-testid="publish-current-client"]'))),
            'S1c 原位那行只读文字显示的也是 B');
        await page.close();
    }

    // ══ S2 深链反向同步 ═══════════════════════════════════════════════
    section('S2 深链 brand_id=101 把左上角切过去');
    {
        /**
         * 场景:用户的左上角本来停在别处(这里就是"还没选"),深链带着 A 进来 ⇒
         * 页面按 A 取文章,**且左上角也变成 A**。
         * (先手选 B 再看深链把它切走会更像真实场景,但那需要"选完之后再带参进入"——
         *  本 harness 一次挂载一次路由,所以这里测的是**带参进入**这一路,
         *  另一路由 S1 的"跟着左上角走"覆盖。)
         */
        const { page, reqs } = await open(browser, { search: '?brand_id=101' });
        const qs = quotesFetched(reqs);
        check(qs.some((q) => q === 11 || q === 12),
            'S2a 🔴 深链指 A(101)⇒ 页面按 A 取文章(11/12)', JSON.stringify(qs));
        const shown = await textSafe(page.locator('[data-testid="publish-current-client"]'));
        check(/A 客户/.test(shown),
            'S2c 🔴🔴 **左上角那份也跟着变了**(只读行取自 ClientContext)——'
            + '这就是"反向同步":两处永远一致,不是页面自己偷偷记另一个客户', shown);
        await page.close();
    }

    // ══ S3 全部客户模式 ═══════════════════════════════════════════════
    section('S3 全部客户模式:不猜一个客户');
    {
        const { page, reqs } = await open(browser, { isAdmin: true });
        const qs = quotesFetched(reqs);
        check(qs.length === 0,
            'S3a 🔴 「全部客户」时**一个文章请求都不发** —— 老代码会落 list[0] 并真的去取 A 的文章',
            JSON.stringify(qs));
        check(await seen(page, '[data-testid="publish-scope-hint"]') === 1, 'S3b 主区有一行说明');
        check(/左上角/.test(await textSafe(page.locator('[data-testid="publish-scope-hint"]'))),
            'S3c 说明指路去**左上角**(唯一入口)');
        const picker = page.locator('[data-testid="publish-mini-client-picker"]');
        check(await picker.count() === 1, 'S3d 给一个迷你选择(复用左上角同一份 clients)');
        await picker.selectOption('202').catch(() => { });
        await page.waitForTimeout(1800);
        const after = quotesFetched(reqs);
        check(after.includes(21),
            'S3e 🔴 选中之后**真的切过去了**(开始取 B 的文章)—— 证明它调的是 switchClient,'
            + '不是页面自建的第二份选择', JSON.stringify(after));
        await page.close();
    }

    // ══ S4 页面级下拉不在了 ═══════════════════════════════════════════
    section('S4 页面自带下拉已撤');
    {
        const { page } = await open(browser, {});
        await pickClientInSidebar(page, 'B 客户');
        check(await seen(page, '[data-testid="publish-project-combobox"]') === 0,
            'S4a 🔴 渲染树里没有那个可搜索 combobox(Owner:统一入口,还能省空间)');
        await page.close();
    }

    // ══ S5 选了客户但他没有写作项目 ═══════════════════════════════════
    section('S5 该客户还没有可发布的文章');
    {
        const { page, reqs } = await open(browser, {});
        await pickClientInSidebar(page, 'C 客户没有写作项目');
        const qs = quotesFetched(reqs);
        check(qs.length === 0,
            'S5a 🔴🔴 该客户没有写作项目 ⇒ **一个文章请求都不发**。'
            + '老代码这一格正是落 list[0]:左上角显示 C,页面却在取 A 的文章',
            JSON.stringify(qs));
        const hint = await textSafe(page.locator('[data-testid="publish-scope-hint"]'));
        check(/还没有可发布的文章/.test(hint), 'S5b 明说这一格是怎么回事', hint);
        check(await seen(page, '[data-testid="publish-goto-writing"]') === 1,
            'S5c 给下一步(去 AI 写文章),不是只说"暂无数据"');
        await page.close();
    }
    // ══ B 段 · #180-B 撤客户下拉 + 沙盒自动选中 ═══════════════════════
    section('B #180-B 撤下拉(Review 裁定 B)');
    {
        /*
         * 🔴 B1 是本单的**前提条件**:沙盒态下没有它,撤掉下拉就等于
         *    让沙盒用户在报价页没有任何办法选客户(取证见 A_180B_BLOCKER)。
         *    所以它必须**真的在浏览器里、不点任何东西**就成立。
         */
        const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
        const pageErrors = [];
        page.on('pageerror', (e) => pageErrors.push(String(e)));
        await page.addInitScript(() => {
            localStorage.setItem('omnirank_token', 'qa-token');
            /* 进沙盒:这是真开关(sandboxState 读的就是这个 key) */
            localStorage.setItem('omnirank_sandbox_active', '1');
            /*
             * 🔴 阶段种成 `step2-online-form`:真流程里这一步由 **PricingCenter**
             *    进页面时设(PricingCenter.tsx:34-35),而本 harness 直接挂的是
             *    内层的 OnlineQuoteFlow,外壳不在 ⇒ 阶段不会自己变。
             *    种它是**补上外壳做的事**,不是编一个不存在的状态 ——
             *    下面 B3b 用源码臂钉住"真的有人这么设",免得我种了一个
             *    生产里根本不会出现的阶段(那样 B3 就是自说自话)。
             */
            localStorage.setItem('omnirank_sandbox_tutorial_stage', 'step2-online-form');
        });
        await page.route('**/api/**', async (route) => {
            const path = new URL(route.request().url()).pathname;
            const json = (b, st = 200) => route.fulfill({
                status: st, contentType: 'application/json', body: JSON.stringify(b),
            });
            if (path.includes('/auth/me')) return json(mkMe(false));
            if (path === '/api/client-context/list') return json({ success: true, clients: CLIENTS });
            if (/^\/api\/client-context\/\d+$/.test(path)) {
                const id = Number(path.split('/').pop());
                return json({ success: true, context: { brand: { id, name: `客户 ${id}` }, profile: null } });
            }
            return json({ success: true, brands: CLIENTS, diagnoses: [], quotes: [] });
        });
        await page.goto(`http://127.0.0.1:${PORT}/index.html`);
        await page.evaluate(() => (globalThis).__mountQuote(document.getElementById('root')));
        await page.waitForTimeout(2500);

        const sel = await page.evaluate(() => {
            try { return sessionStorage.getItem('omnirank_current_brand_candidate:112'); }
            catch { return null; }
        }).catch(() => null);
        check(String(sel) === '111',
            "B1 🔴 沙盒态打开 /pricing **不点任何东西**,当前客户 = 教程客户(111)"
            + " —— 这是撤下拉的前提;没有它沙盒用户在这页选不了客户", String(sel));

        /*
         * 🔴 报价页打开是**列表**,表单在「新建报价」后面 —— 所以要走真路径点进去,
         *    不能在列表页上断言表单里的东西。(第一版就是这么错的:B2a 报红,
         *    而红的原因是我没打开表单,不是控件不在。)
         */
        const newBtn = page.getByRole('button', { name: /新建报价/ }).first();
        check(await newBtn.count().catch(() => 0) >= 1,
            "B2a0 分母自证:列表页上有「新建报价」(第二步真正要碰的第一个控件)");
        await newBtn.click({ timeout: 5000 }).catch(() => { });
        await page.waitForTimeout(1200);

        check(await page.locator('[data-testid="quote-client-from-sidebar"]').count()
            .catch(() => 0) === 1,
            "B2a 表单里客户那一格现在只是**跟随显示**,不是选择器");
        const dropdowns = await page.locator('input[placeholder*="选择已有客户"]').count().catch(() => 0);
        check(dropdowns === 0,
            "B2b 🔴 客户下拉**不在 DOM 里**(沙盒态臂 · 表单已打开)", String(dropdowns));
        /*
         * 🔴 B3:`step2-online-form` 这个阶段必须**有人接**。
         *    撤下拉之前它全仓零消费者 —— 撤了第二步的页内引导就归零。
         *    这里验的是"阶段的消费者真的渲染出来了",不是"源码里写了这个字符串"。
         */
        /*
         * 🔴 不查 `data-feature-id` —— FeatureTooltip **不渲染这个属性**
         *    (它渲染的是一个 `<span className={wrapClassName}>` + 一个挂到 body 的 portal)。
         *    第一版查它,`coach=0`,而原因是**选择器写错**不是引导缺失。
         *    改查**用户真读到的那句话**:这比查属性更接近"屏幕上有没有指引"。
         */
        const stageTip = await page.evaluate(() => (
            document.body.innerText.includes('已经替你选好') ? 1 : 0
        )).catch(() => 0);
        const stageNow = await page.evaluate(() => {
            try { return localStorage.getItem('omnirank_sandbox_tutorial_stage'); }
            catch { return null; }
        }).catch(() => null);
        {
            const pc = readFileSync(join(ROOT, 'src/pages/Quote/PricingCenter.tsx'), 'utf8');
            check(/setTutorialStage\('step2-online-form'\)/.test(pc),
                "B3b 🔴 接线自证:真流程里 **PricingCenter** 确实会把阶段推到 "
                + "`step2-online-form`(否则上面种的阶段是我编的,B3 等于自说自话)"
                .replace('🔴', String.fromCodePoint(0x1F534)));
        }
        check(stageTip >= 1,
            "B3 🔴 `step2-online-form` 阶段在 /pricing 上有一个**渲染出来的** coach mark"
            + "(撤下拉前这个阶段零消费者,第二步会断在这里)",
            `coach=${stageTip} stage=${stageNow}`);
        const tip = await page.locator('[data-feature-id="sandbox_quote_select_brand"]').count()
            .catch(() => 0);
        check(tip === 0, "B2c 老的 coach mark 也一并不在", String(tip));
        check(pageErrors.length === 0, 'B 段零 pageerror', pageErrors.join(' | ') || '干净');
        await page.close();
    }
    {
        /*
         * 🔴 B2 的**真实态**臂:非沙盒下同样不该有那个下拉
         *    (#180 的结论是"当前客户唯一来源是左上角",真实态本来就该跟左上角)。
         *    两态各一臂 —— 只验一态的话,另一态留着下拉也会绿。
         */
        const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
        await page.addInitScript(() => {
            localStorage.setItem('omnirank_token', 'qa-token');
            localStorage.removeItem('omnirank_sandbox_active');
        });
        await page.route('**/api/**', async (route) => {
            const path = new URL(route.request().url()).pathname;
            const json = (b) => route.fulfill({
                status: 200, contentType: 'application/json', body: JSON.stringify(b),
            });
            if (path.includes('/auth/me')) return json(mkMe(false));
            if (path === '/api/client-context/list') return json({ success: true, clients: CLIENTS });
            return json({ success: true, brands: CLIENTS, diagnoses: [], quotes: [] });
        });
        await page.goto(`http://127.0.0.1:${PORT}/index.html`);
        await page.evaluate(() => (globalThis).__mountQuote(document.getElementById('root')));
        await page.waitForTimeout(2000);
        const d = await page.locator('input[placeholder*="选择已有客户"]').count().catch(() => 0);
        check(d === 0, "B2d 🔴 客户下拉在**真实态**也不在 DOM 里", String(d));
        const sidebarN = await page.locator('[data-testid="real-sidebar"]').count().catch(() => 0);
        check(sidebarN === 1,
            "B2e 分母自证:真实态左上角那个**真**选择器在(它才是唯一入口)", String(sidebarN));
        await page.close();
    }

} finally {
    await browser.close();
    server.close();
}

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
