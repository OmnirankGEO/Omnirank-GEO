#!/usr/bin/env node
/**
 * 判据 · #189「同行对比」三档 —— **真浏览器 · 真组件 · 真 dist CSS**。
 *
 * 为什么必须起浏览器(源码层证不了):
 *  · T2 颜色:「无红黄告警色」在源码层只能查类名,而**类名对不代表 CSS 生效**。
 *    要量 computed style 才知道屏幕上那枚 chip 到底什么颜色。
 *  · T3 禁用:0 家已核实时「点名对比」真的点不动、且原因**看得见**。
 *  · T4 切档不触发检索:要**数真实发出的请求**。
 *
 * 🔴 必须注入 dist 里刚构建的真 CSS。不注入的话页面是裸 DOM,Tailwind 一个类都没生效,
 *    所有 computed-style 断言在**真缺陷存在时也照样绿**。取不到就报「判据不可用」非零退出。
 *
 * 🔴 所有 DOM 询问**报错不抛错**:Playwright 在元素缺失时抛,而注毒恰恰让元素缺失 ⇒
 *    脚本当场死、后面的臂全没跑,而非零退出码看着像"抓住了"。
 *
 * 🔴 本页此前**没有任何浏览器臂**,夹具是新搭的。选中项目走的是真路径:
 *    `?quote_id=` 深链(WritingHall.tsx:2432 / 5008 那个 effect),
 *    不是我自己 setState —— 用真路径建立前置状态,判据才不是拿我自己造的状态自证。
 *
 * 跑法:cd frontend && npm run build && node scripts/test-peer-compare-render.mjs
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
/*
 * 🔴 产物目录每进程一份,且必须留在 node_modules/.cache 下面
 *    (esbuild 解析裸 import 从 entry 目录逐级往上找 node_modules;放系统 tmp 找不到)。
 *    共用固定目录会让两把闸互相覆盖 bundle —— 注毒判决会翻面。
 */
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a189-render-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

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
import { WritingHall } from ${q('src/pages/Writing/WritingHall.tsx')};

(globalThis as any).__mount = function (el: HTMLElement, entry: string) {
    createRoot(el).render(
        <MemoryRouter initialEntries={[entry]}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <WritingHall />
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
            '.jpg': 'dataurl', '.jpeg': 'dataurl', '.png': 'dataurl',
            '.webp': 'dataurl', '.svg': 'dataurl',
        },
        plugins: [rawSuffixPlugin], logLevel: 'silent',
    });
} catch (err) { unusable('打包失败', (err && err.message) || err); }

let cssName = '';
try {
    const assets = join(ROOT, 'dist', 'assets');
    const cands = readdirSync(assets)
        .filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(assets, f)).mtimeMs }))
        .sort((a, b) => b.m - a.m);
    if (!cands.length) throw new Error('dist/assets 里没有 index-*.css');
    cssName = cands[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(assets, cssName), 'utf8'), 'utf8');
    console.log(`  (真 CSS:dist/assets/${cssName})`);
} catch (err) {
    unusable('取不到构建产物 CSS —— 样式臂没有真 CSS 就等于没量,先跑 `npm run build`',
        (err && err.message) || err);
}

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a189 harness</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>
`, 'utf8');

const MIME = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
};
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

// ── 夹具 ───────────────────────────────────────────────────────────
const BRAND_SUMMARY = {
    id: 7, name: 'QA 测试品牌', brand_code: 'QA-007', industry: '教育培训',
    diagnosis_count: 1, quote_count: 1, brand_status: 'active', client_status: 'active',
};
const CLIENT_CONTEXT = {
    brand: {
        id: 7, name: 'QA 测试品牌', brand_code: 'QA-007', industry: '教育培训',
        cities: '南京', diagnosis_count: 1, brand_type: 'client',
    }, profile: null,
};
const AUTH_ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
    id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent',
};
const PROJECT = {
    id: 901, brand_id: 7, brand_name: 'QA 测试品牌', name: 'QA 写作项目',
    writing_status: 'titles_ready', article_count: 3, quote_ids: [901],
    created_at: '2026-09-13T00:00:00Z',
};
/** 已核实 2 家 / 还在核实 1 家 / 已排除 1 家 —— 四种位都覆盖到。 */
const PEERS_MIXED = [
    { name: '甲同行', desc: 'x', confidence: 'high', source_count: 3, name_verified: true },
    { name: '乙同行', desc: 'y', confidence: 'manual', human_verified_name: true },
    { name: '丙同行', desc: 'z', confidence: 'mid' },
    { name: '丁同行', desc: 'w', name_verified: true, excluded: true },
];
const PEERS_NONE_VERIFIED = [
    { name: '丙同行', desc: 'z', confidence: 'mid' },
    { name: '戊同行', desc: 'v', confidence: 'mid' },
];

const TMO = { timeout: 2500 };
const isDisabledSafe = (l) => l.isDisabled(TMO).catch(() => null);
const textSafe = (l) => l.innerText(TMO).catch(() => '');
const oneLine = (e) => String((e && e.message) || e).split(String.fromCharCode(10))[0];
const clickSafe = (l) => l.click({ timeout: 4000 }).then(() => '').catch((e) => oneLine(e));
const seen = (page, sel) => page.locator(sel).count();

async function openHall(browser, plan = {}) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 }, deviceScaleFactor: 2 });
    const reqs = [];
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    await page.addInitScript((theme) => {
        localStorage.setItem('omnirank_token', 'qa-token');
        localStorage.setItem('omnirank-theme', theme);
        sessionStorage.setItem('omnirank_current_brand_candidate:112', '7');
        /*
         * 🔴 本 harness **没有挂 ThemeProvider**,所以手挂 class 是安全的:
         *    没有任何东西会把它 remove 掉。
         *    (截图器那边挂了 Provider,必须走它自己的 storage key —— 手挂会被擦掉,
         *     深色截出来是浅色。两处做法不同是有原因的,别互相抄。
         *     本轮就先踩了一次:只写 storage key,没有 Provider 读它 ⇒ 深色没生效,
         *     出图前那道"量背景色"的闸把它拦下了,没出成误导图。)
         */
        if (theme === 'dark') document.documentElement.classList.add('dark');
    }, plan.theme || 'light');
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const path = new URL(req.url()).pathname;
        reqs.push({ method: req.method(), path });
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        if (path === '/api/client-context/list') return json({ success: true, clients: [BRAND_SUMMARY] });
        if (/^\/api\/client-context\/\d+$/.test(path)) return json({ success: true, context: CLIENT_CONTEXT });
        if (path === '/api/writing/projects') return json({ projects: [PROJECT] });
        if (/^\/api\/writing\/projects\/\d+$/.test(path)) {
            return json({ project: PROJECT, keywords: [], topics: [], articles: [] });
        }
        if (/\/api\/writing\/competitors\/\d+$/.test(path)) {
            return json({ competitors: plan.peers || PEERS_MIXED, mode: plan.mode || 'semi' });
        }
        if (/\/api\/writing\/competitors\/\d+\/mode$/.test(path)) {
            const body = (() => { try { return req.postDataJSON(); } catch { return {}; } })();
            return json({ mode: body?.mode || 'semi' });
        }
        if (path.includes('/auth/me')) return json(AUTH_ME);
        return json({ success: true, projects: [], topics: [], keywords: [], articles: [], competitors: [] });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    /*
     * 🔴 主题 class 在 **goto 之后**挂,不在 addInitScript 里挂。
     *    实测 addInitScript 那条没生效(`html.class` 量出来是空的)——
     *    而"没生效"与"生效了但页面是浅色"在最终读数上同形。
     *    是出图前那道"量背景色"的闸把它喊出来的,不是我看出来的。
     */
    if ((plan.theme || 'light') === 'dark') {
        await page.evaluate(() => document.documentElement.classList.add('dark'));
    }
    await page.evaluate(() => globalThis.__mount(document.getElementById('root'), '/writing?quote_id=901'));
    await page.waitForSelector('[data-testid="peer-mode-group"]', { timeout: 20000 }).catch(() => { });
    await page.waitForTimeout(900);
    return { page, reqs, pageErrors };
}

const browser = await playwright.chromium.launch();
try {
    // ══ T1' 前置标签与三档文案(真 DOM) ═════════════════════════════
    section("T1' 前置标签与三档文案");
    {
        const { page, pageErrors } = await openHall(browser);
        check(await seen(page, '[data-testid="peer-mode-group"]') === 1,
            "T1'0 分母自证:三档控件真的渲染出来了(渲染不出来的话下面全是空断言)");
        check((await textSafe(page.locator('[data-testid="peer-group-label"]'))).includes('同行对比'),
            "T1'a 🔴 前置标签「同行对比」在屏幕上 —— Owner 问的正是「这三个标签不是找同行的吗」");
        const labels = await page.locator('[data-testid="peer-mode-group"] button')
            .allInnerTexts().catch(() => []);
        check(labels.length === 3, "T1'b 三档都在", JSON.stringify(labels));
        check(labels.some(t => /点名对比 · 只写已核实的 2 家/.test(t)),
            "T1'c 🔴 real 档带**实时数**(夹具 2 家已核实)", JSON.stringify(labels));
        check(labels.some(t => /暂不点名 · 1 家还在核实/.test(t)),
            "T1'd 🔴 semi 档带实时数(1 家还在核实;已排除那家两边都不算)");
        check(labels.some(t => /不点名 · 只写怎么选/.test(t)), "T1'e evidence_only 档文案");
        check(!labels.some(t => /已核验|待核验|仅写标准/.test(t)),
            "T1'f 反臂:老文案不在屏幕上", JSON.stringify(labels));
        check(pageErrors.length === 0, "T1'g 零 pageerror", pageErrors.join(' | ') || '干净');
        await page.close();
    }

    // ══ T2' 颜色:量 computed style,不看类名 ═══════════════════════
    section("T2' 三档无告警色(真 CSS · 转成真 sRGB 再判)");
    {
        const { page } = await openHall(browser, { mode: 'evidence_only' });
        /*
         * 🔴 **不能直接解析 getComputedStyle 的颜色字符串。**
         *    Tailwind v4 用的是 oklch/oklab,computed style 回的是
         *    `oklab(0.969 0.00004 0.00002 / 0.4)` 这种 —— 三个数是
         *    亮度/色度,**不是 0–255 的 R/G/B**。
         *    我第一版按 sRGB 写判红条件(`r > 150 && r - b > 60`),
         *    在 oklab 下第一位恒 ≤1 ⇒ 条件**永远为假** ⇒ 这两条臂
         *    「没有告警色」在**真的是红色时也照样绿**。绿得什么都没量。
         *
         * 🔴 改法:把颜色交给**浏览器自己**转 —— 画到 1×1 canvas 上再读像素。
         *    canvas 的 fillStyle 认得任何 CSS 颜色写法(含 oklch),
         *    getImageData 回的是真 sRGB。
         *    并配正样本臂 T2'ctl:同一条通路喂一个已知的红,必须被判成告警色。
         */
        const toRgb = `(css) => {
            const c = document.createElement('canvas');
            c.width = 1; c.height = 1;
            const ctx = c.getContext('2d');
            ctx.clearRect(0, 0, 1, 1);
            ctx.fillStyle = '#000';
            ctx.fillStyle = css;
            ctx.fillRect(0, 0, 1, 1);
            const d = ctx.getImageData(0, 0, 1, 1).data;
            return [d[0], d[1], d[2], d[3]];
        }`;
        const probe = await page.evaluate(`(() => {
            const toRgb = ${toRgb};
            const warnish = (rgb) => {
                const [r, g, b, a] = rgb;
                if (a === 0) return false;              // 完全透明不算颜色
                return r > 140 && (r - b) > 50;         // 红或黄:R 明显高于 B
            };
            const btns = [...document.querySelectorAll('[data-testid="peer-mode-group"] button')];
            const note = document.querySelector('[data-testid="peer-evidence-only-note"]');
            return {
                chips: btns.map((e) => {
                    const cs = getComputedStyle(e);
                    return {
                        t: (e.textContent || '').trim().slice(0, 10),
                        bg: toRgb(cs.backgroundColor),
                        warn: warnish(toRgb(cs.backgroundColor)),
                    };
                }),
                note: note ? { bg: toRgb(getComputedStyle(note).backgroundColor),
                               warn: warnish(toRgb(getComputedStyle(note).backgroundColor)) } : null,
                /* 正样本:同一条通路喂已知的红 —— 必须被判成告警色 */
                ctlRed: warnish(toRgb('rgb(239, 68, 68)')),
                ctlRedOklch: warnish(toRgb('oklch(0.637 0.237 25.331)')),
                ctlNeutral: warnish(toRgb('rgb(244, 244, 245)')),
            };
        })()`).catch(() => null);

        check(!!probe && probe.chips.length === 3,
            "T2'0 分母自证:量到了三档的真 sRGB",
            probe ? JSON.stringify(probe.chips.map((c) => c.t + ':' + c.bg.slice(0, 3))) : '量不到');
        check(!!probe && probe.ctlRed === true && probe.ctlRedOklch === true,
            "T2'ctl 🔴 正样本臂:已知的红(sRGB 与 oklch 两种写法)**都判得出来** ——"
            + "没有它,下面那两条在真红时也会绿(我第一版就是:oklab 的第一位恒 ≤1,"
            + "按 0–255 写的判红条件永远为假)",
            probe ? `red=${probe.ctlRed} oklch=${probe.ctlRedOklch}` : '量不到');
        check(!!probe && probe.ctlNeutral === false,
            "T2'ctl2 反向对照:中性灰**不会**被误判成告警色");
        const warnChips = (probe ? probe.chips : []).filter((c) => c.warn);
        check(warnChips.length === 0,
            "T2'a 🔴 三档**没有一档**是红/黄告警色(「不点名」是正当选择,"
            + "原来它是红底白字,读起来像出错)",
            JSON.stringify(warnChips.map((c) => c.t + ':' + c.bg.slice(0, 3))));
        check(!!probe && !!probe.note, "T2'b evidence_only 态那行提示在");
        check(!!probe && probe.note && probe.note.warn === false,
            "T2'c 🔴 提示行也不是红底(它描述的是后果,不是错误)",
            probe && probe.note ? JSON.stringify(probe.note.bg.slice(0, 3)) : '量不到');
        await page.close();
    }

    // ══ T3' 0 家已核实 ⇒ 点名对比点不动,且原因看得见 ═══════════════
    section("T3' 没核实过的时候点名对比不可用");
    {
        const { page } = await openHall(browser, { peers: PEERS_NONE_VERIFIED });
        const real = page.locator('[data-testid="peer-mode-real"]');
        check(await isDisabledSafe(real) === true,
            "T3'a 🔴 一家都没核实 ⇒「点名对比」点不动");
        const reason = await textSafe(page.locator('[data-testid="peer-mode-disabled-reason"]'));
        check(/先联网核实/.test(reason),
            "T3'b 🔴 原因**看得见**(只置灰不说话 = 用户反复点一个永远不动的按钮)", reason);
        await page.close();
    }
    {
        /* 反臂:有已核实的就该能点 —— 否则 T3'a 可能是"这颗按钮永远是灰的" */
        const { page } = await openHall(browser);
        check(await isDisabledSafe(page.locator('[data-testid="peer-mode-real"]')) === false,
            "T3'c 🔴 反臂:有 2 家已核实时它**可点**(否则上一条是恒真)");
        await page.close();
    }

    // ══ T4' 切档不触发联网检索(数真实请求) ═════════════════════════
    section("T4' 切档不再触发检索");
    {
        const { page, reqs } = await openHall(browser);
        const before = reqs.filter(r => /research-competitors|competitors\/\d+\/verify/.test(r.path)).length;
        /*
         * 🔴 **三档都要点到,尤其 real。** 第一版只点了 evidence_only 与 semi ——
         *    而老缺陷恰恰长在 real 上(点「已核验」会去搜)。注毒当场照出来:
         *    往 `switchCompetitorMode` 里塞回 `if (mode === 'real') verify…`,
         *    这条臂纹丝不动 —— 不是"锁没牙",是**毒够不着**:被测路径根本没走到 real。
         *    夹具里有 2 家已核实,所以 real 是可点的。
         */
        for (const m of ['evidence_only', 'semi', 'real']) {
            await clickSafe(page.locator(`[data-testid="peer-mode-${m}"]`));
            await page.waitForTimeout(700);
        }
        const after = reqs.filter(r => /research-competitors|competitors\/\d+\/verify/.test(r.path)).length;
        check(after === before,
            "T4'a 🔴 三档**逐个点一遍**(含 real),联网检索请求**一次都没发** —— 原来点「已核验」会去搜,"
            + "一个按钮两种含义,用户以为只是在切显示",
            `${before} → ${after}`);
        const modePuts = reqs.filter(r => r.method === 'PUT' && /competitors\/\d+\/mode/.test(r.path)).length;
        check(modePuts >= 3,
            "T4'b 分母自证:三下**真的点到了**(发了 3 次持久化档位的请求)——"
            + "没点到的话上一条恒真", String(modePuts));
        check(await seen(page, '[data-testid="peer-research-btn"]') === 1,
            "T4'c 检索是**独立按钮**,单独存在");
        await page.close();
    }
    // ══ 截图:深 / 浅各一(WO_189 T5 要求) ═════════════════════════
    if (process.argv.includes('--shots')) {
        section('截图(深/浅各一)');
        const OUT = join(ROOT, '..', '..', 'a189-shots');
        mkdirSync(OUT, { recursive: true });
        for (const theme of ['dark', 'light']) {
            const { page } = await openHall(browser, { theme, mode: 'evidence_only' });
            /*
             * 🔴 主题走**应用自己的** ThemeProvider(key `omnirank-theme`)。
             *    出图前量一次背景色:标称与实测不符就不出图 ——
             *    把浅色命名成 dark_ 发出去,没有任何东西会报警。
             */
            /*
             * 🔴 量的是**有主题令牌背景的那个元素**,不是 `document.body` ——
             *    harness 的 body 根本没设背景(真页面是外层 layout 设的),
             *    量它永远得到透明/白,于是"深色"永远判不成立。
             *    第一版就是这么写的:那道闸拦住了我,没让误导图流出去,
             *    但闸拦住的是**我的量法**不是页面。
             */
            const lum = await page.evaluate(() => {
                /* 🔴 探针用 `bg-background` 的**不透明**元素:
                   提示行是 `bg-muted/40`,40% 透明度会把读数拉向画布底色,
                   深浅两边都量到接近的值。 */
                const el = document.createElement('div');
                el.className = 'bg-background';
                document.body.appendChild(el);
                const c = document.createElement('canvas');
                c.width = c.height = 1;
                const ctx = c.getContext('2d');
                ctx.fillStyle = '#808080';
                ctx.fillStyle = getComputedStyle(el).backgroundColor;
                ctx.fillRect(0, 0, 1, 1);
                const d = ctx.getImageData(0, 0, 1, 1).data;
                el.remove();
                return d[0] * 0.299 + d[1] * 0.587 + d[2] * 0.114;
            }).catch(() => -1);
            const looksDark = lum >= 0 && lum < 110;
            if ((theme === 'dark') !== looksDark) {
                console.log(`  🔴 主题没生效:标称 ${theme} 实测亮度 ${Math.round(lum)} —— 不出这张图`);
                await page.close();
                continue;
            }
            const f = join(OUT, `peer_modes_${theme}.png`);
            const box = page.locator('[data-testid="peer-mode-group"]').first();
            await box.evaluate((e) => e.scrollIntoView({ block: 'center' })).catch(() => { });
            await page.waitForTimeout(300);
            await page.screenshot({ path: f, fullPage: false }).catch(() => { });
            console.log(`  ${theme}: 亮度 ${Math.round(lum)} → ${f}`);
            await page.close();
        }
    }

} catch (e) {
    console.log('FAIL 判据自身异常:' + String((e && e.stack) || e));
    failed += 1;
} finally {
    await browser.close();
    server.close();
}

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过:#189 真浏览器臂');
process.exit(0);
