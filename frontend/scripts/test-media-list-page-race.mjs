#!/usr/bin/env node
/**
 * 判据 · #181 —— **把竞态真的排出来**(真 chromium · 真组件 · 真时序)。
 *
 * 客户报「点索引后再点下一页还是当前页」,Owner 自测未复现 —— 因为要三件事同时发生:
 *   ① 选了行业大类;② 换索引后该行业在新条件下计数为 0;
 *   ③ 在 facets 回包**之前**点了下一页。
 * 纯函数证不了这个:它是**两个请求之间的时序**。这里用 `page.route` 把
 * facets 请求**人为延迟 500ms**,在这 500ms 里点下一页,然后放包回来 ——
 * 老代码那一刻会 `setWmPage(1)` 把用户打回第 1 页。
 *
 * 🔴 判据看两样东西:页码显示,**以及最后一次列表请求真的带了 page=2** ——
 *    只看屏幕数字会被"显示 2 而请求的是 1"骗过去(本单另一半缺陷正是
 *    前端从不回写服务端回显的 page)。
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
const outDir = mkdtempSync(join(CACHE_ROOT, 'a181-race-'));
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

(globalThis as any).__mount = function (el: HTMLElement) {
    createRoot(el).render(
        <MemoryRouter initialEntries={['/publish']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
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
<html lang="zh"><head><meta charset="utf-8"><title>a181 harness</title>
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

const AUTH_ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
    id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent',
};
const BRAND_SUMMARY = { id: 7, name: 'QA 测试品牌', industry: '教育培训', diagnosis_count: 1, quote_count: 1 };
const CLIENT_CONTEXT = { brand: { id: 7, name: 'QA 测试品牌', industry: '教育培训', diagnosis_count: 1 }, profile: null };

const mkMedia = (page) => Array.from({ length: 3 }, (_, i) => ({
    id: page * 100 + i, name: `媒体 P${page}-${i}`, platform: '微博', province: '江苏',
    price_points: 100, fans: 10000, industry: 'edu',
}));

const TMO = { timeout: 3000 };
const clickSafe = (l) => l.click({ timeout: 5000 }).then(() => '').catch((e) => String((e && e.message) || e).split(String.fromCharCode(10))[0]);
const textSafe = (l) => l.innerText(TMO).catch(() => '');

/**
 * @param plan.facetsDelayMs  facets 回包延迟(制造竞态窗口)
 * @param plan.facetsIndustries 回包里的行业 facet(不含 'edu' 就会触发"清行业")
 * @param plan.pagesByPage    每页响应里回显的 pages
 */
async function open(browser, plan = {}) {
    const page = await browser.newPage();
    const reqs = [];
    const pageErrors = [];
    let facetCall = 0;
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'qa-token');
        sessionStorage.setItem('omnirank_current_brand_candidate:112', '7');
    });
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const path = url.pathname;
        reqs.push({ method: req.method(), path, query: Object.fromEntries(url.searchParams) });
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        if (path === '/api/client-context/list') return json({ success: true, clients: [BRAND_SUMMARY] });
        if (/^\/api\/client-context\/\d+$/.test(path)) return json({ success: true, context: CLIENT_CONTEXT });
        if (path.includes('/auth/me')) return json(AUTH_ME);
        if (path === '/api/meijiehezi/wemedia/filters') {
            if (plan.facetsDelayMs) await new Promise((r) => setTimeout(r, plan.facetsDelayMs));
            // 🔴 第一包必须**含** edu(否则页面上根本没有"教育"这个 chip 可点);
            //    之后的包按 plan 去掉它 —— 这才是"换索引后该行业计数为 0"那一格。
            facetCall += 1;
            const industries = facetCall === 1
                ? [{ key: 'edu', label: '教育', count: 9 }]
                : (plan.facetsIndustries || [{ key: 'edu', label: '教育', count: 9 }]);
            return json({
                status: 'success',
                platforms: ['微博', '小红书'],
                industries,
                provinces: ['江苏', '浙江'], geo_platforms: [],
            });
        }
        if (path === '/api/meijiehezi/wemedia') {
            const p = Number(url.searchParams.get('page') || 1);
            // 选了省份 = "换了索引":新条件下只剩 1 页(pages 从 5 掉到 1)
            const filtered = !!url.searchParams.get('province');
            const pages = filtered && plan.pagesAfterFilter !== undefined
                ? plan.pagesAfterFilter
                : (plan.pages === undefined ? 5 : plan.pages);
            if (plan.listDelayMs) await new Promise((r) => setTimeout(r, plan.listDelayMs));
            return json({ status: 'success', media: mkMedia(p), total: pages * 3, page: p, pages });
        }
        if (path === '/api/meijiehezi/media/filters') {
            return json({ status: 'success', areas: ['华东'], resource_types: [], news_resources: [], portal_medias: [], resource_type_names: [], geo_platforms: [], special_industries: [] });
        }
        if (path === '/api/meijiehezi/media') {
            const p = Number(url.searchParams.get('page') || 1);
            const filtered = !!url.searchParams.get('area');
            const pages = filtered && plan.pagesAfterFilter !== undefined
                ? plan.pagesAfterFilter
                : (plan.pages === undefined ? 5 : plan.pages);
            if (plan.listDelayMs) await new Promise((r) => setTimeout(r, plan.listDelayMs));
            return json({ status: 'success', media: mkMedia(p), total: pages * 3, page: p, pages });
        }
        return json({ status: 'success', success: true, data: [], items: [], total: 0 });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate(() => (globalThis).__mount(document.getElementById('root')));
    await page.waitForTimeout(2500);
    return { page, reqs, pageErrors };
}

/** 最后一次自媒体列表请求带的 page 参数 —— 屏幕数字骗得了人,请求参数骗不了。 */
const lastWmPage = (reqs) => {
    const hits = reqs.filter((r) => r.path === '/api/meijiehezi/wemedia');
    return hits.length ? Number(hits[hits.length - 1].query.page || 1) : null;
};

const browser = await playwright.chromium.launch();
try {
    section('R0 先证夹具立得起来(否则下面全是假信号)');
    let goToWemedia = null;
    {
        const { page, reqs, pageErrors } = await open(browser);
        // 切到「自媒体」tab
        const tab = page.locator('text=自媒体').first();
        await clickSafe(tab);
        await page.waitForTimeout(1500);
        const n = reqs.filter((r) => r.path === '/api/meijiehezi/wemedia').length;
        check(n >= 1, 'R0a 自媒体列表真的请求了(夹具与 tab 切换都通)', `${n} 次`);
        check(pageErrors.length === 0, 'R0b 零 pageerror', pageErrors.join(' | ').slice(0, 200) || '干净');
        check(lastWmPage(reqs) === 1, 'R0c 首次请求是第 1 页', String(lastWmPage(reqs)));
        goToWemedia = n >= 1;
        await page.close();
    }
    if (!goToWemedia) {
        unusable('自媒体 tab 起不来 —— 下面的竞态臂没有可信的底座,不当绿灯');
    }

    // ══ R1 复现臂:facets 慢包回来不许把页码打回 1 ═══════════════════
    section('R1 竞态:慢 facets 包不许改页码');
    {
        const { page, reqs } = await open(browser, {
            facetsDelayMs: 700,
            // 🔴 回包里**不含** 'edu':老代码正是在这一刻 setWmIndustry('') + setWmPage(1)
            facetsIndustries: [{ key: 'med', label: '医疗', count: 4 }],
        });
        await clickSafe(page.locator('text=自媒体').first());
        await page.waitForTimeout(1200);
        /**
         * 🔴 **先选一个行业大类** —— 少了这一步,`shouldClearIndustry` 的条件永远不成立,
         *    那个分支根本不执行:注毒把 `setWmPage(1)` 放回去,R1b 照样绿。
         *    (第一版就漏了它 —— 一条**绿得什么都没量**的臂,靠注毒才看出来。)
         *    工单原话的复现条件是三件事同时发生,"选了行业"是第一件。
         */
        // 🔴 chip 上印的是 `{i.key} {i.count}`(也就是「edu 9」),**不是** label。
        //    按文字找会点空 —— 用它自带的 data-industry-key。
        const eduClickErr = await clickSafe(page.locator('[data-industry-key="edu"]').first());
        await page.waitForTimeout(800);
        /**
         * 🔴 **夹具自证**:行业必须真的选上了,否则 `shouldClearIndustry` 的条件不成立、
         *    那个分支根本不执行 —— R1b 就会"绿得什么都没量"(注毒放回根因也照样绿)。
         *    证据取**请求参数**,不取屏幕高亮:参数骗不了人。
         */
        const withIndustry = reqs.filter((r) => r.path === '/api/meijiehezi/wemedia' && r.query.industry);
        check(withIndustry.length >= 1,
            'R1a0 夹具自证:行业真的选上了(列表请求带了 industry)——'
            + '选不上的话下面那条臂等于没测',
            `${withIndustry.length} 次带 industry${eduClickErr ? ' · 点击报错:' + eduClickErr.slice(0, 60) : ''}`);
        // 点一个省份 chip ⇒ 同时触发 facets(慢) 与列表(快)
        const provinceChip = page.locator('text=江苏').first();
        await clickSafe(provinceChip);
        // 抢在 facets 回包之前点「下一页」
        await page.waitForTimeout(150);
        const nextBtn = page.locator('button:has-text("下一页")').first();
        await clickSafe(nextBtn);
        const beforeFacets = lastWmPage(reqs);
        // 让慢包回来
        await page.waitForTimeout(1800);
        const after = lastWmPage(reqs);
        check(beforeFacets === 2 || after === 2,
            'R1a 点了下一页之后确实请求过第 2 页', `点后=${beforeFacets} · 最终=${after}`);
        check(after === 2,
            'R1b 🔴🔴 facets 慢包回来之后,**最后一次列表请求仍是 page=2** ——'
            + '老代码这一刻会 setWmPage(1) 把用户打回第 1 页,这正是客户反馈的'
            + '"点了下一页还是当前页"',
            `最后一次列表请求 page=${after}`);
        await page.close();
    }

    // ══ R3 陈旧包臂:两次筛选连点,第一次的 facets 慢回 ⇒ 丢弃 ══════════
    section('R3 陈旧 facets 包被丢弃');
    {
        const { page, reqs } = await open(browser, {
            facetsDelayMs: 900,
            facetsIndustries: [{ key: 'med', label: '医疗', count: 4 }],
        });
        await clickSafe(page.locator('text=自媒体').first());
        await page.waitForTimeout(1200);
        await clickSafe(page.locator('text=江苏').first());
        await page.waitForTimeout(120);
        await clickSafe(page.locator('text=浙江').first());   // 第二次筛选
        await page.waitForTimeout(2200);
        const facetCalls = reqs.filter((r) => r.path === '/api/meijiehezi/wemedia/filters').length;
        check(facetCalls >= 2, 'R3a 两次筛选各发了一次 facets(分母非空)', `${facetCalls} 次`);
        check(lastWmPage(reqs) === 1,
            'R3b 连点两次筛选后停在第 1 页(筛选变更本来就该回第 1 页 —— 那是**同步** handler 干的)',
            `page=${lastWmPage(reqs)}`);
        await page.close();
    }

    // ══ R2 越界臂:切筛选后到回包前,pages 还是**旧值** ══════════════════
    //
    // 🔴 换锚说明:第一版让 `pages` 一开始就等于 1,然后断言「下一页不可点」——
    //    可本页的翻页条本来就写着 `{wmPages > 1 && (...)}`,只有一页时**整条都不渲染**,
    //    于是 `isDisabled()` 回 null(元素不存在),判据对着正确行为报红。
    //    那是我把"按钮禁用"与"按钮不存在"混成了一件事。
    //    真正要钉的是工单说的那一格:**切筛选之后、回包之前**,`pages` 还是上一次的 5,
    //    照它放行就会请求一个越界页(后端返空 ⇒ 屏幕"没翻")。所以锚挪到那个窗口。
    section('R2 切筛选后的在途窗口不许放行越界页');
    {
        const { page, reqs } = await open(browser, { pages: 5, pagesAfterFilter: 1, listDelayMs: 900 });
        await clickSafe(page.locator('text=自媒体').first());
        await page.waitForTimeout(1500);
        const nextBtn = page.locator('button:has-text("下一页")').first();
        check(await nextBtn.count() === 1, 'R2a0 初始 5 页 ⇒ 翻页条在(分母非空)');
        // 换索引:选省份 ⇒ 列表请求在途(900ms),此刻 wmPages 仍是旧的 5
        await clickSafe(page.locator('text=江苏').first());
        await page.waitForTimeout(200);
        const disabledInFlight = await nextBtn.isDisabled().catch(() => null);
        check(disabledInFlight === true,
            'R2a 🔴🔴 请求在途时「下一页」**不可点** —— 此刻 pages 还是上一次的 5,'
            + '照旧逻辑会放行,于是请求一个越界页、后端返空 media ⇒ 屏幕看起来"没翻"',
            String(disabledInFlight));
        await page.waitForTimeout(1600);
        const seen = reqs.filter((r) => r.path === '/api/meijiehezi/wemedia')
            .map((r) => Number(r.query.page || 1));
        check(!seen.some((p) => p > 1),
            'R2b 🔴 整个过程一次越界请求都没发出去', JSON.stringify(seen));
        await page.close();
    }

    // ══ R4 对称臂:软文 tab 同一格 ════════════════════════════════════
    section('R4 软文 tab 对称');
    {
        const { page, reqs } = await open(browser, { pages: 5, pagesAfterFilter: 1, listDelayMs: 900 });
        await page.waitForTimeout(1800);
        const nextBtn = page.locator('button:has-text("下一页")').first();
        check(await nextBtn.count() === 1, 'R4a0 软文 tab 初始 5 页 ⇒ 翻页条在');
        await clickSafe(page.locator('text=华东').first());
        await page.waitForTimeout(200);
        const d = await nextBtn.isDisabled().catch(() => null);
        check(d === true,
            'R4a 软文 tab 同一格:在途时不可点(防漂移 —— 同一个谓词两处必有一处没人验)',
            String(d));
        await page.waitForTimeout(1600);
        const seen = reqs.filter((r) => r.path === '/api/meijiehezi/media')
            .map((r) => Number(r.query.page || 1));
        check(seen.length >= 1 && !seen.some((p) => p > 1),
            'R4b 软文 tab 也没发出越界请求', JSON.stringify(seen));
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
