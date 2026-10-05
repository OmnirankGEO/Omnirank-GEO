#!/usr/bin/env node
/**
 * 截图 · #222 a1b:**L0 与服务商各一张,深浅各一** = 4 张。
 *
 * 🔴 只截一种身份没意义:本单要看的是**两种身份看到的差别**,
 *    而那个差别应该恰好只剩「数据回退」一张卡。
 * 每张都量主题亮度,标称与实测不符就不出图(免得深色那张其实是浅色的)。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a222-shot-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const require_ = createRequire(import.meta.url);
function unusable(what, err) {
    console.log(`FAIL 截图不可用:${what} —— ${String((err && err.message) || err).slice(0, 300)}`);
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
import { ActionCards } from ${q('src/pages/Monitoring/components/ActionCards.tsx')};

const noop = () => {};
let root = null;
(globalThis as any).__mount = function (el: HTMLElement, isAgent: boolean, key: number) {
    if (!root) root = createRoot(el);
    root.render(
        <MemoryRouter initialEntries={['/monitoring']}>
          <div key={key} style={{ padding: 24, maxWidth: 1100 }}>
            <ActionCards
              scheduleEnabled={true} currentServiceDays={30} currentServiceStart={'2026-09-01'}
              archivesCount={0} selectedClient={'QA 客户'} currentBrandId={629}
              isAdmin={false} isAgent={isAgent}
              onAddKeyword={noop} onTrend={noop} onToken={noop} onPublication={noop}
              onLogs={noop} onReportGen={noop} onRollback={noop} onSchedule={noop}
              onServiceConfig={noop} onClearData={noop} onArchives={noop}
            />
          </div>
        </MemoryRouter>
    );
};
`, 'utf8');

try {
    await esbuild.build({
        entryPoints: [join(outDir, 'entry.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        loader: { '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.png': 'dataurl', '.svg': 'dataurl' },
        logLevel: 'silent',
    });
} catch (err) { unusable('打包失败', err); }

let cssName = '';
try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    cssName = c[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, cssName), 'utf8'), 'utf8');
} catch (err) { unusable('取不到真 CSS', err); }
console.log(`  (真 CSS:dist/assets/${cssName})`);

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a222</title>
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

const OUT = join(ROOT, '..', '..', 'a222-shots');
mkdirSync(OUT, { recursive: true });
const browser = await playwright.chromium.launch();
let key = 0;
try {
    for (const theme of ['light', 'dark']) {
        for (const [name, isAgent] of [['l0', false], ['agent', true]]) {
            const page = await browser.newPage({ viewport: { width: 1200, height: 1000 } });
            await page.goto(`http://127.0.0.1:${PORT}/index.html`);
            if (theme === 'dark') await page.evaluate(() => document.documentElement.classList.add('dark'));
            key += 1;
            await page.evaluate(([a, k]) => globalThis.__mount(document.getElementById('root'), a, k),
                [isAgent, key]);
            await page.waitForTimeout(500);
            await page.evaluate(() => {
                const b = [...document.querySelectorAll('button')].find((x) => /更多工具/.test(x.textContent || ''));
                if (b) b.click();
            });
            await page.waitForTimeout(400);
            /* 主题自证:量一次真实亮度,标称与实测不符就不出图 */
            const lum = await page.evaluate(() => {
                const c = getComputedStyle(document.body).backgroundColor;
                const m = c.match(/[\d.]+/g) || ['255', '255', '255'];
                return (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3;
            });
            const looksDark = lum < 128;
            if ((theme === 'dark') !== looksDark) {
                console.log(`  🔴 ${name}/${theme}:标称与实测不符(亮度 ${Math.round(lum)})—— 不出这张`);
                await page.close();
                continue;
            }
            const f = join(OUT, `${name}_${theme}.png`);
            await page.screenshot({ path: f, fullPage: true });
            console.log(`  ${name}/${theme}: 亮度 ${Math.round(lum)} → ${f}`);
            await page.close();
        }
    }
} finally {
    await browser.close();
    server.close();
}
console.log('截图完成(每张都量过主题)');
