#!/usr/bin/env node
/**
 * 行为臂 · #222 a1b「普通账号权限 = 服务商(除经营后台)」(真 chromium + dist 真 CSS)。
 *
 * 🔴 两份夹具各挂一次:**L0(agent_level=0)** 与 **服务商(agent_level=1)**。
 *    只跑一份就只能证明"某一种身份看得见",证不了"两种看到的差别正是经营后台"。
 *
 * 🔴 量的是**渲染后屏幕上有没有那张卡**,不是源码里有没有那个字符串:
 *    源码锚被一句注释就能满足,而这一格问的是用户看不看得见。
 *
 * 不进 build 链(要 chromium + 先 npm run build 取真 CSS),按本仓惯例。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a222-render-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const require_ = createRequire(import.meta.url);
let bad = 0;
const check = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};
function unusable(what, err) {
    console.log(`FAIL 判据不可用(不当绿灯):${what} —— ${String((err && err.message) || err).slice(0, 300)}`);
    process.exit(1);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { unusable('esbuild / playwright 取不到', err); }

const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

/*
 * 🔴 直接挂 `ActionCards`,不挂整张监测页:身份在它这里是**props**
 * (`isAdmin` / `isAgent`),这是产品真正做分流的那一层。
 * 挂整页要造一大堆无关夹具,而那些夹具一旦造错,红的原因就和身份无关了。
 */
writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { ActionCards } from ${q('src/pages/Monitoring/components/ActionCards.tsx')};

const noop = () => {};
let root = null;
(globalThis as any).__mount = function (el: HTMLElement, isAgent: boolean, isAdmin: boolean, key: number) {
    if (!root) root = createRoot(el);
    root.render(
        <MemoryRouter initialEntries={['/monitoring']}>
          <div key={key}>
            <ActionCards
              scheduleEnabled={true}
              currentServiceDays={30}
              currentServiceStart={'2026-09-01'}
              archivesCount={0}
              selectedClient={'QA 客户'}
              currentBrandId={629}
              isAdmin={isAdmin}
              isAgent={isAgent}
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
        loader: {
            '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.jpeg': 'dataurl',
            '.png': 'dataurl', '.webp': 'dataurl', '.svg': 'dataurl',
        },
        logLevel: 'silent',
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
    unusable('取不到真 CSS —— 没有它,"看得见"量不了(裸 DOM 上 opacity-40 也量到 1)', err);
}
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

const browser = await playwright.chromium.launch();
let mountKey = 0;
async function shot(page, isAgent, isAdmin) {
    mountKey += 1;
    /* 🔴 缓存 root + 换 key 重挂:同一容器第二次 createRoot 会变成两个 root,读数变抛硬币。 */
    await page.evaluate(([a, d, k]) => globalThis.__mount(document.getElementById('root'), a, d, k),
        [isAgent, isAdmin, mountKey]);
    await page.waitForTimeout(400);
    /* 「更多工具」要先展开才看得到二级卡 */
    await page.evaluate(() => {
        const b = [...document.querySelectorAll('button')].find((x) => /更多工具/.test(x.textContent || ''));
        if (b) b.click();
    });
    await page.waitForTimeout(300);
    return page.evaluate(() => {
        const vis = (el) => {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
                && cs.display !== 'none' && Number(cs.opacity) > 0.05;
        };
        const titles = [...document.querySelectorAll('p.font-medium')]
            .filter((e) => vis(e)).map((e) => (e.textContent || '').trim());
        const moreBtn = [...document.querySelectorAll('button')]
            .find((x) => /更多工具/.test(x.textContent || ''));
        return { titles, more: moreBtn ? (moreBtn.textContent || '').trim() : '' };
    });
}

try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);

    console.log('N1 普通账号(agent_level = 0)');
    const l0 = await shot(page, false, false);
    check(l0.titles.length >= 4,
        'N1a 分母自证:L0 屏幕上确实渲染出了卡(数到 0 说明挂载坏了,'
        + '而挂载坏了和"全被挡住"在报文上同形)', `${l0.titles.length} 张:${l0.titles.join(' / ')}`);
    for (const t of ['交付设置', '媒体投放', '操作日志']) {
        check(l0.titles.includes(t),
            `N1b-${t} 🔴 L0 **看得见**「${t}」(交付工具,不是经营后台)`,
            l0.titles.includes(t) ? '在' : '🔴 还被挡着');
    }
    /* 🔴 [a1b' 2026-09-16] 这一格**翻面**:上一版钉「L0 看不见数据回退(待 Owner)」,
       Owner 已点头放开(删的是自己账号的监测历史,与服务商同权;不可逆由二次确认承担)。
       判据与修法互斥时改的是判据方向 —— 旧判据留着会一直红,红着的判据会盖住下一个真缺陷。 */
    check(l0.titles.includes('数据回退'),
        'N1c 🔴 L0 **看得见**「数据回退」(Owner 09-16 放开)',
        l0.titles.includes('数据回退') ? '在' : '🔴 还被挡着');
    check(!l0.titles.includes('添加关键词') && !l0.titles.includes('清除数据'),
        'N1d 反臂:仅 admin 的两项对 L0 仍不可见');

    console.log('\nN2 服务商(agent_level = 1)· 反向臂:他看到的不许变少');
    const ag = await shot(page, true, false);
    for (const t of ['交付设置', '媒体投放', '操作日志', '数据回退']) {
        check(ag.titles.includes(t), `N2a-${t} 服务商仍看得见「${t}」`);
    }
    const lost = ag.titles.filter((t) => !l0.titles.includes(t));
    /* 🔴 翻面后这一格**变强了**:最后一项差异也去掉之后,两种身份在这一屏上
       看到的应当**完全一样**,差集为空。比原来的「恰好只剩一项」更难绕 ——
       任何一边多出或少掉任何一张卡都会红。 */
    const gained = l0.titles.filter((t) => !ag.titles.includes(t));
    check(lost.length === 0 && gained.length === 0,
        'N2b 🔴 两种身份在这一屏上看到的**完全一样**(差集两个方向都为空)—— '
        + '监测是交付面,不是经营后台;剩一项差异都说明还有没对齐的',
        `服务商多出 [${lost.join(' / ') || '无'}] · L0 多出 [${gained.join(' / ') || '无'}]`);

    console.log('\nN3 「更多工具 (N)」的数字与真实可见数一致');
    const l0More = (l0.more.match(/\((\d+)\)/) || [])[1];
    const agMore = (ag.more.match(/\((\d+)\)/) || [])[1];
    /*
     * 🔴 注毒 Q3 照出来的:第一版只钉两个数的**差**。
     *    计数整体错 2 时,差仍然是 1 —— 毒下去了这一格照样绿。
     *    ⇒ 既钉差,也钉**值**:按钮上的 N 必须等于展开后真的数得出来的卡数。
     */
    const l0Tiles = l0.titles.filter((x) => ['趋势报表', '报告管理', '媒体投放', '操作日志',
        '数据回退', '添加关键词', '清除数据', '数据归档'].includes(x)).length;
    const agTiles = ag.titles.filter((x) => ['趋势报表', '报告管理', '媒体投放', '操作日志',
        '数据回退', '添加关键词', '清除数据', '数据归档'].includes(x)).length;
    check(Number(l0More) === l0Tiles,
        'N3a 🔴 L0:按钮上的 N **等于**展开后真的看得见的卡数(只钉两种身份的差的话,'
        + '整体错 2 也照样绿)', `按钮 ${l0More} · 实际 ${l0Tiles}`);
    check(Number(agMore) === agTiles,
        'N3a2 🔴 服务商:同上', `按钮 ${agMore} · 实际 ${agTiles}`);
    check(!!l0More && !!agMore && Number(agMore) === Number(l0More),
        'N3b 两种身份的「更多工具 (N)」**相等** —— 随 N2b 一起翻面:'
        + '最后一项差异去掉后,这个数也不该再差',
        `L0 ${l0More} · 服务商 ${agMore}`);

    await page.close();
} catch (err) {
    console.log('FAIL 判据不可用(不当绿灯):跑挂了 '
        + String((err && err.stack) || err).slice(0, 700));
    bad += 1;
} finally {
    await browser.close();
    server.close();
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
