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
    quote: { el: <OnlineQuoteFlow />, entry: '/pricing' },
    publish: { el: <PublishCenter />, entry: '/publish' },
    writing: { el: <WritingHall />, entry: '/writing' },
    /* [#194] 两个新面 */
    diagnosis: { el: <NewDiagnosis />, entry: '/diagnosis/new' },
    monitoring: { el: <MonitoringPage />, entry: '/monitoring' },
};
(globalThis as any).__mount = function (el: HTMLElement, surface: string) {
    const p = PAGES[surface] || PAGES.writing;
    createRoot(el).render(<Stack entry={p.entry}>{p.el}</Stack>);
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
    ['quote', 'quote'],
    ['writing', 'writing'],
    ['publish', 'publish'],
    ['monitoring', 'monitoring'],
    ['diagnosis', 'diagnosis'],
];

/** 有些页要先选客户才有内容(写作/发布/监测)。用**真的左上角**选。 */
const NEEDS_CLIENT = ['writing', 'publish', 'monitoring'];

const OUT = join(ROOT, '..', '..', 'a198-shots');
mkdirSync(OUT, { recursive: true });

const browser = await playwright.chromium.launch();
try {
    for (const [name, surface] of SURFACES) {
        const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
        await page.addInitScript(() => {
            localStorage.setItem('omnirank_token', 'qa-token');
            localStorage.setItem('omnirank_sandbox_active', '1');
            localStorage.setItem('omnirank_onboarding_state', JSON.stringify({
                completed_steps: ['first_quote'],
            }));
        });
        await page.route('**/api/**', async (route) => {
            const path = new URL(route.request().url()).pathname;
            const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
            if (path.includes('/auth/me')) return json(ME);
            return json({ success: true, projects: [], topics: [], keywords: [], articles: [], clients: [], media: [] });
        });
        await page.goto(`http://127.0.0.1:${PORT}/index.html`);
        await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), surface);
        await page.waitForTimeout(2600);
        if (NEEDS_CLIENT.includes(surface)) {
            /* 用真的左上角选客户;选不上就**说出来**,不默默截一张空页当证据。 */
            const picked = await page.evaluate(() => {
                const bar = document.querySelector('[data-testid="real-sidebar"]');
                if (!bar) return 'no-sidebar';
                const first = bar.querySelector('button');
                if (first) first.click();
                return 'clicked';
            }).catch(() => 'err');
            await page.waitForTimeout(700);
            const chose = await page.evaluate(() => {
                const bar = document.querySelector('[data-testid="real-sidebar"]');
                const hit = [...(bar ? bar.querySelectorAll('*') : [])]
                    .find((el) => (el.textContent || '').includes('一路顺风出行服务')
                        && el.children.length === 0);
                const target = hit && (hit.closest('button,[role="button"],.cursor-pointer') || hit);
                if (!target) return false;
                target.click();
                return true;
            }).catch(() => false);
            await page.waitForTimeout(1800);
            if (!chose) console.log(`  🔴 ${name}:没选上客户(${picked})—— 这张图是"没选客户"的状态`);
        }
        /* 主题自证:标称浅色,实测亮度必须是浅的(否则不出图) */
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
        if (!(lum >= 110)) {
            console.log(`  🔴 ${name}:标称浅色,实测亮度 ${Math.round(lum)} —— 不出这张`);
            await page.close();
            continue;
        }
        const f = join(OUT, `${name}.png`);
        await page.screenshot({ path: f, fullPage: true }).catch(() => { });
        const txt = await page.evaluate(() => (document.getElementById('root') || document.body).innerText)
            .catch(() => '');
        console.log(`  ${name}: 亮度 ${Math.round(lum)} · 文字 ${txt.replace(/\s+/g, ' ').length} 字 → ${f}`);
        await page.close();
    }
} finally {
    try { await browser.close(); } catch { /* 尽力 */ }
    try { server.close(); } catch { /* 尽力 */ }
}
console.log('截图完成(浅色;每张都量过主题)');
