#!/usr/bin/env node
/**
 * #198 「禁猜」普查 · **第 2 步：截图**（只读，零产品改动）。
 *
 * 🔴 静态扫描看不见屏幕。今天实测的两个盲点：
 *    ① 它只看**页面文件**，而图片回显往往在**子组件**里
 *      （`IdCardUpload` 里有 `<img>`，而我把两条报成了「有图不显示图」）；
 *    ② 它看不到**旁边那一栏**（海报模板选项里没样图，但右边就是实时预览）。
 *    所以静态扫描只能缩小范围，**结论必须看图**。
 *
 * 注：为了让页面有数据，这里用**沙盒态**（产品自带的 QA 数据），
 * 真实态的同一块 UI 形状一致；截图里不会出现任何真客户品牌。
 *
 * 跑法：cd frontend && npm run build && node scripts/audit-198-shots.mjs
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a190-render-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const require_ = createRequire(import.meta.url);
let failed = 0;
let pending = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
const skip = (m, d = '') => { console.log(`  ..   未评估 ${m}${d ? ` — ${d}` : ''}`); pending++; };
const section = (t) => console.log(`\n=== ${t} ===`);
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 1500));
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
import OnlineQuoteFlow from ${q('src/pages/Quote/OnlineQuoteFlow.tsx')};
import { PublishCenter } from ${q('src/pages/Publishing/PublishCenter.tsx')};
/* [#194] 体检页与监测页 —— G2 的 36 条未评估里最大的两块。 */
import { NewDiagnosis } from ${q('src/pages/Diagnosis/NewDiagnosis.tsx')};
import MonitoringPage from ${q('src/pages/Monitoring/index.tsx')};
/* [#194] 真的左上角:写作大厅的项目列表按**当前客户**过滤,不先选客户它就是空的。
   用真控件建立前置状态,判据才不是拿我自己造的状态自证。 */
import { ClientSwitcherSidebar } from ${q('src/components/layout/ClientSwitcherSidebar.tsx')};
/* [#199 a2] 资料徽章的真挂载点(那张 Card 带 overflow-hidden) */
import { DecisionBarBridge } from ${q('src/components/workbench/DecisionBarBridge.tsx')};

const Stack = ({ children, entry }: any) => (
    <MemoryRouter initialEntries={[entry]}>
      <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
        <OnboardingProvider><PricingProvider><ClientProvider>
          <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
          {children}
        </ClientProvider></PricingProvider></OnboardingProvider>
      </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
    </MemoryRouter>
);
const PAGES: Record<string, { el: any; entry: string }> = {
    bridge: { el: <DecisionBarBridge brandId={9001} />, entry: '/writing' },
    quote: { el: <OnlineQuoteFlow />, entry: '/pricing' },
    publish: { el: <PublishCenter />, entry: '/publish' },
    writing: { el: <WritingHall />, entry: '/writing' },
    /* [#194] 两个新面 */
    diagnosis: { el: <NewDiagnosis />, entry: '/diagnosis/new' },
    monitoring: { el: <MonitoringPage />, entry: '/monitoring' },
};
/* 🔴 root 只建一次;nonce 换号 = 换 key 重挂被测组件(effect 重跑)。
   同一容器第二次 createRoot 在 React 18 下是两个 root,谁赢看运气。 */
let __root: any = null;
(globalThis as any).__mount = function (el: HTMLElement, surface: string, nonce?: number) {
    const p = PAGES[surface] || PAGES.writing;
    if (!__root) __root = createRoot(el);
    const child = nonce === undefined ? p.el : React.cloneElement(p.el, { key: 'm' + nonce });
    __root.render(<Stack entry={p.entry}>{child}</Stack>);
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
        entryPoints: [join(outDir, 'entry.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        loader: {
            '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.jpeg': 'dataurl',
            '.png': 'dataurl', '.webp': 'dataurl', '.svg': 'dataurl',
        },
        plugins: [rawSuffixPlugin], logLevel: 'silent',
    });
} catch (err) { unusable('打包失败', (err && err.message) || err); }

let cssName = '';
try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    cssName = c[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, cssName), 'utf8'), 'utf8');
} catch (err) {
    unusable('取不到真 CSS —— 没有它,"看得见"这件事量不了(裸 DOM 上什么都没有尺寸)', err);
}
console.log(`  (真 CSS:dist/assets/${cssName})`);

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a190</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>`, 'utf8');

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = createServer((req, res) => {
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try {
        const b = readFileSync(join(outDir, n));
        res.writeHead(200, { 'Content-Type': MIME[extname(n)] || 'application/octet-stream' });
        res.end(b);
    } catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;
const ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
    id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent',
};

/*
 * 🔴 这里**只列本 harness 真的挂得起来的面**。
 *    第一版我把 `dashboard_today` 也列上、却让它挂 `writing` 那个面 ——
 *    出来的图是写作大厅,文件名却叫 `dashboard_today.png`。
 *    **一张名字和内容对不上的截图,比没有截图更坏**:它会被当证据用。
 *    今日工作台没有 harness ⇒ 在报表里写"未取到图 · 缺 harness",不拿别的图顶。
 */
const SURFACES = [
    ['publish', 'publish'],
    ['monitoring', 'monitoring'],
    /* [a2] 资料徽章缺项弹层 —— 本单唯一肉眼可见的改动(Portal 之后出口不再被裁) */
    ['completeness_popover', 'bridge'],
];
const NEEDS_CLIENT = ['publish', 'monitoring'];
/* 🔴 沙盒态把 /api/brands/:id/completeness 接走,回「85 分 · 不缺项」⇒ 弹层压根不出现。
   要拍它必须关沙盒,让请求走到本文件的桩上。 */
const NO_SANDBOX = ['bridge'];

const OUT = join(ROOT, '..', '..', 'a199-shots');
mkdirSync(OUT, { recursive: true });

const browser = await playwright.chromium.launch();
try {
    for (const theme of ['light', 'dark']) {
        for (const [name, surface] of SURFACES) {
            const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
            const noSandbox = NO_SANDBOX.includes(surface);
            await page.addInitScript((skip) => {
                localStorage.setItem('omnirank_token', 'qa-token');
                if (!skip) localStorage.setItem('omnirank_sandbox_active', '1');
                localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
                    completed_steps: ['first_quote'],
                }));
            }, noSandbox);
            await page.route('**/api/**', async (route) => {
                const path = new URL(route.request().url()).pathname;
                const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
                if (path.includes('/auth/me')) return json(ME);
                /* 🔴 价目**故意打不通**:本单要拍的正是"价目读不到"那一格。
                   500 会让 PricingContext 落进 error 分支。 */
                if (process.argv.includes('--pricing-down') && path.includes('/pricing')) {
                    return route.fulfill({ status: 500, contentType: 'application/json',
                        body: JSON.stringify({ detail: '价目服务暂时不可用(QA 桩)' }) });
                }
                /* 🔴 形状照后端真回包:裸对象 {brand_id, score, groups, missing},不套 success */
                if (/\/api\/brands\/\d+\/completeness$/.test(path)) {
                    return json({
                        brand_id: 9001, score: 62,
                        groups: { identity: 20, business: 18, marketing: 14, deep_analysis: 10, market_insight: 0 },
                        missing: ['company_intro', 'selling_points', 'success_cases', 'core_value',
                            'testimonials', 'service_scope', 'local_competitors', 'authority_sources'],
                    });
                }
                if (/\/api\/client-context\/\d+$/.test(path)) {
                    return json({ success: true, context: { brand: {
                        id: 9001, name: 'QA 夹具客户', industry: '测试行业', city: '广州',
                        is_test: true, diagnosis_count: 1, latest_diagnosis_id: 1,
                        latest_diagnosis_created_at: '2026-09-13T00:00:00Z',
                    }, profile: {} } });
                }
                return json({ success: true, projects: [], topics: [], keywords: [], articles: [], clients: [], media: [], quotes: [] });
            });
            await page.goto(`http://127.0.0.1:${PORT}/index.html`);
            /* 🔴 主题 class 要在 goto 之后挂:addInitScript 跑在文档解析之前,
               那一刻 documentElement 还不在(同族坑今天第五次)。 */
            if (theme === 'dark') {
                await page.evaluate(() => document.documentElement.classList.add('dark'));
            }
            await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), surface);
            if (surface === 'bridge') {
                /* 第一发 snapshot 请求赶在鉴权确认之前会被挡住 ⇒ 换 key 重挂一次 */
                await page.waitForTimeout(2200);
                await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s, 2), surface);
                await page.waitForTimeout(2600);
                await page.evaluate(() => {
                    const b = document.querySelector('[data-testid="completeness-badge"]');
                    if (b) b.click();
                });
                await page.waitForTimeout(600);
            }
            await page.waitForTimeout(2600);
            if (NEEDS_CLIENT.includes(surface)) {
                await page.evaluate(() => {
                    const bar = document.querySelector('[data-testid="real-sidebar"]');
                    const first = bar && bar.querySelector('button');
                    if (first) first.click();
                }).catch(() => { });
                await page.waitForTimeout(700);
                await page.evaluate(() => {
                    const bar = document.querySelector('[data-testid="real-sidebar"]');
                    const hit = [...(bar ? bar.querySelectorAll('*') : [])]
                        .find((el) => (el.textContent || '').includes('一路顺风出行服务') && el.children.length === 0);
                    const t = hit && (hit.closest('button,[role="button"],.cursor-pointer') || hit);
                    if (t) t.click();
                }).catch(() => { });
                await page.waitForTimeout(1800);
            }
            const lum = await page.evaluate(() => {
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
                console.log(`  🔴 ${name}/${theme}:标称与实测不符(亮度 ${Math.round(lum)})—— 不出这张`);
                await page.close();
                continue;
            }
            const suffix = process.argv.includes('--pricing-down') ? '_pricingdown' : '';
            const f = join(OUT, `${name}${suffix}_${theme}.png`);
            await page.screenshot({ path: f, fullPage: true }).catch(() => { });
            console.log(`  ${name}${suffix}/${theme}: 亮度 ${Math.round(lum)} → ${f}`);
            await page.close();
        }
    }
} finally {
    try { await browser.close(); } catch { /* 尽力 */ }
    try { server.close(); } catch { /* 尽力 */ }
}
console.log('截图完成(每张都量过主题)');
