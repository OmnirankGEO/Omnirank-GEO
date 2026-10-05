#!/usr/bin/env node
/**
 * 行为臂 · WO_276 chunk 加载失败自愈(真 chromium · 真 vite 产物 · 真浏览器 HTTP 缓存)。
 * 结构 / 单元臂 = test-chunk-heal.mjs(build 链)。
 *
 * 🔴 复现的是 2026-09-23 的真事故形态:边缘把 chunk 缓存成「301 指向自己」,浏览器把 301 永久缓存;
 *    边缘恢复 200 之后,浏览器照样从缓存里读 301 —— 普通刷新 /「立即刷新」都不清这份缓存。
 *    另一臂(Review 追加):依赖块被缓存成 404 后源站恢复 200,同一条自愈路径也要能落页。
 * 🔴 **不用 page.route**:Playwright 一开请求拦截就关掉 HTTP 缓存,而这一臂量的正是缓存。
 *    所以产物、接口全由本机 node 服务器直接回,浏览器缓存照真实规则工作。
 * 🔴 毒的是**被依赖的**块:/login 懒加载 LoginPage 块,LoginPage 静态依赖若干不在启动闭包里的块;
 *    挑其中一个下毒 —— 浏览器报错只点名 LoginPage,真正坏的是它的依赖(事故里就是这样)。
 *
 * 三格场景,每格两段:
 *   段一(源站坏着):新上下文(干净缓存)开 /login → 坏响应进浏览器缓存,页面落到「正在更新到新版本」
 *   段二(源站已恢复 200,缓存里仍是坏响应):同一上下文开**新标签页**(新 sessionStorage)再进 /login
 *   H301 依赖块「301 指向自己」 · H404 依赖块 404 · HB「立即刷新」按钮(段二预置刷新冷却 + 已自愈标记,
 *        自动路径不跑,只剩按钮这一条路)
 *   HM 清单全刷(Review:「失败模块 + __vite__mapDeps.f 全部 chunk」):段一另把 /terms 的页面块也弄坏并进缓存;
 *        段二在 /login 自愈之后,同一标签页再进 /terms 也要直接能开 —— 它不是这一页的依赖,
 *        只有「读入口清单全刷」才刷得到(刷新冷却 + 已自愈标记都已用掉,救不了第二次)
 *   .0 分母自证:段一源站真回了坏响应、页面真落到了 chunk 错误页(毒真的下成了)
 *        HB 另证:段二点按钮前,目标块**一次网络请求都没有**(浏览器全从缓存读坏响应 = 刷新不清缓存)
 *   .1 🔴 段二落到登录框(自愈后能用)
 *   .2 段二里目标块真的走了一次网络(是 cache:'reload' 那一下把缓存换掉的,不是碰巧)
 *        HM:/terms 页面块在**进 /terms 之前**就已走过网络(是 /login 那次自愈顺手刷的)
 *   .3 不空转:段一(源站一直坏,自愈也救不了)与段二的页面加载次数都 ≤ 3
 *
 * 产物:默认当场 vite build 一份到临时目录(约 20s,量的永远是当下源码);A276_DIST=<目录> 指定现成产物(改前读数 / 反臂用)。
 * 三态退出码:0 全过 / 1 有失败 / 3 判据不可用。
 */
import { readFileSync, readdirSync, existsSync, mkdirSync, mkdtempSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);
let failed = 0;
const check = (c, m, d = '') => { console.log(`  ${c ? 'OK  ' : 'FAIL'} ${m}${d ? ` — ${d}` : ''}`); if (!c) failed += 1; };
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 800));
    process.exit(3);
}
let playwright;
try { playwright = require_('playwright'); } catch (err) { unusable('playwright 取不到', err); }

/* ── 产物 ─────────────────────────────────────────────────────────── */
/* 🔴 默认**自己打一份**(vite build 到临时目录),不读 frontend/dist:
 *    第一版按「dist 比 src 新」判新鲜度,挂进 browser:arms 一跑就判不可用 —— 排在前面的
 *    mutation 臂(243/250/251/253)下毒后**回写**源文件复原,内容逐字不变、mtime 却变新了。
 *    按 mtime 判新鲜度在这条链上恒假;自己打一份,量的永远是当下的源码。 */
let DIST = process.env.A276_DIST;
if (!DIST) {
    const cache = join(ROOT, 'node_modules', '.cache');
    mkdirSync(cache, { recursive: true });
    DIST = mkdtempSync(join(cache, 'a276-render-dist-'));
    const built = DIST;
    process.on('exit', () => { try { rmSync(built, { recursive: true, force: true }); } catch { /* 尽力 */ } });
    try {
        execFileSync(process.execPath, [join(ROOT, 'node_modules', 'vite', 'bin', 'vite.js'), 'build', '--outDir', DIST, '--emptyOutDir', '--logLevel', 'error'],
            { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'], timeout: 10 * 60 * 1000 });
    } catch (err) { unusable('vite build 打不出产物', String(err.stderr || err.message || err)); }
}
const ASSETS = join(DIST, 'assets');
if (!existsSync(join(DIST, 'index.html')) || !existsSync(ASSETS)) unusable(`没有产物 ${DIST}`);
const INDEX = readFileSync(join(DIST, 'index.html'), 'utf8');
const files = readdirSync(ASSETS);
const STATIC_RE = /(?:import|export)\s*(?:[^'"();]*?\bfrom\s*)?["']\.\/([A-Za-z0-9_.-]+\.js)["']/g;
const staticDeps = (f) => [...readFileSync(join(ASSETS, f), 'utf8').matchAll(STATIC_RE)].map((m) => m[1]);
const bootSeen = new Set();
const q = [...INDEX.matchAll(/(?:src|href)="\/assets\/([A-Za-z0-9_.-]+\.js)"/g)].map((m) => m[1]);
while (q.length) { const f = q.shift(); if (bootSeen.has(f) || !files.includes(f)) continue; bootSeen.add(f); q.push(...staticDeps(f)); }
const LOGIN = files.find((f) => /^LoginPage-[A-Za-z0-9_-]+\.js$/.test(f));
const TARGET = LOGIN ? staticDeps(LOGIN).filter((d) => !bootSeen.has(d) && files.includes(d)).sort()[0] : undefined;
if (!LOGIN || !TARGET) unusable(`产物里找不到 LoginPage 块 / 它的非启动依赖(LoginPage=${LOGIN || '无'})`);
const TERMS = files.find((f) => /^TermsPage-[A-Za-z0-9_-]+\.js$/.test(f));
if (!TERMS || bootSeen.has(TERMS)) unusable(`产物里找不到不在启动闭包里的 TermsPage 块(${TERMS || '无'})`);

/* ── 本机服务器:源站 + 可切换的「坏边缘」─────────────────────────── */
const MIME = { '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.html': 'text/html; charset=utf-8',
    '.png': 'image/png', '.svg': 'image/svg+xml', '.webp': 'image/webp', '.woff2': 'font/woff2', '.json': 'application/json' };
const state = { mode: 'ok', poisoned: new Set([TARGET]), hits: new Map() };
const hit = (k) => state.hits.set(k, (state.hits.get(k) || 0) + 1);
const hits = (k) => state.hits.get(k) || 0;
const server = createServer((req, res) => {
    const p = decodeURIComponent(new URL(req.url || '/', 'http://x').pathname);
    if (p.startsWith('/api/')) {
        res.writeHead(200, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' });
        return res.end('{"success":true,"status":"success","data":[],"items":[]}');
    }
    if (p.startsWith('/assets/')) {
        const name = p.slice('/assets/'.length);
        if (name === TERMS) hit(`terms:${state.mode}`);
        if (state.poisoned.has(name)) {
            if (name === TARGET) hit(`target:${state.mode}`);
            if (state.mode === 'loop301') {
                res.writeHead(301, { Location: `${ORIGIN}${p}`, 'Cache-Control': 'public, max-age=31536000' });
                return res.end();
            }
            if (state.mode === 'e404') {
                res.writeHead(404, { 'Content-Type': 'text/plain', 'Cache-Control': 'public, max-age=31536000' });
                return res.end('not found');
            }
        }
        try {
            const b = readFileSync(join(ASSETS, name));
            res.writeHead(200, { 'Content-Type': MIME[extname(name)] || 'application/octet-stream', 'Cache-Control': 'public, max-age=31536000, immutable' });
            return res.end(req.method === 'HEAD' ? undefined : b);
        } catch { res.writeHead(404); return res.end('x'); }
    }
    if (p === '/login') hit('doc:/login');
    res.writeHead(200, { 'Content-Type': MIME['.html'], 'Cache-Control': 'no-cache' });
    return res.end(req.method === 'HEAD' ? undefined : INDEX);
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const ORIGIN = `http://127.0.0.1:${server.address().port}`;

const ERR = '正在更新到新版本';
const FORM = 'input[placeholder="请输入手机号或用户名"]';
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function settle(page, maxMs = 25000) {
    /* 等到页面加载次数 4 秒不再变(自动刷新跑完),最多 maxMs */
    const t0 = Date.now();
    let last = hits('doc:/login');
    let stableSince = Date.now();
    while (Date.now() - t0 < maxMs) {
        await sleep(500);
        const now = hits('doc:/login');
        if (now !== last) { last = now; stableSince = Date.now(); }
        if (Date.now() - stableSince >= 4000) break;
    }
    await page.waitForTimeout(200);
}
const visible = async (page, sel) => page.locator(sel).first().isVisible().catch(() => false);
const errShown = async (page) => page.getByText(ERR).first().isVisible().catch(() => false);

console.log(`目标:LoginPage 块 ${LOGIN} 的依赖块 ${TARGET}(不在启动闭包里;报错只会点名 LoginPage)`);
const browser = await playwright.chromium.launch();
try {
    for (const [id, label, mode, viaButton, alsoTerms] of [
        ['H301', '依赖块「301 指向自己」', 'loop301', false, false],
        ['H404', '依赖块 404', 'e404', false, false],
        ['HB', '「立即刷新」按钮(自动路径被冷却挡住)', 'loop301', true, false],
        ['HM', '清单全刷:/terms 页面块也坏在缓存里', 'loop301', false, true],
    ]) {
        console.log(`\n${id} ${label}`);
        const ctx = await browser.newContext({ viewport: { width: 1280, height: 900 } });
        /* 段一:源站坏着 */
        state.hits.clear();
        state.mode = mode;
        state.poisoned = new Set(alsoTerms ? [TARGET, TERMS] : [TARGET]);
        const p1 = await ctx.newPage();
        await p1.goto(`${ORIGIN}/login`).catch(() => { });
        await p1.getByText(ERR).first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => { });
        await settle(p1);
        const bad1 = hits(`target:${mode}`);
        const err1 = await errShown(p1);
        const form1 = await visible(p1, FORM);
        const loads1 = hits('doc:/login');
        await p1.close();
        let termsBad1 = 0;
        let termsErr1 = true;
        if (alsoTerms) {
            /* 段一另开一页进 /terms,让它的页面块也以坏响应进缓存 */
            const p1b = await ctx.newPage();
            await p1b.goto(`${ORIGIN}/terms`).catch(() => { });
            await p1b.getByText(ERR).first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => { });
            await p1b.waitForTimeout(3000);
            termsBad1 = hits(`terms:${mode}`);
            termsErr1 = await errShown(p1b);
            await p1b.close();
        }
        /* 段二:源站恢复 200,浏览器缓存里还是坏响应;新标签页 = 新 sessionStorage */
        state.hits.clear();
        state.mode = 'ok';
        const p2 = await ctx.newPage();
        if (viaButton) {
            await p2.addInitScript(() => {
                try {
                    sessionStorage.setItem('omnirank_chunk_reload_at', String(Date.now()));
                    sessionStorage.setItem('omnirank_chunk_healed_at', String(Date.now()));
                } catch { /* 隐私模式 */ }
            });
        }
        await p2.goto(`${ORIGIN}/login`).catch(() => { });
        let beforeClick = null;
        let errBeforeClick = false;
        if (viaButton) {
            await p2.getByText(ERR).first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => { });
            await p2.waitForTimeout(1500);
            errBeforeClick = await errShown(p2);
            beforeClick = hits('target:ok');
            await p2.getByRole('button', { name: '立即刷新' }).first().click({ timeout: 5000 }).catch(() => { });
        }
        await p2.locator(FORM).first().waitFor({ state: 'visible', timeout: 30000 }).catch(() => { });
        await settle(p2, 12000);
        const form2 = await visible(p2, FORM);
        const net2 = hits('target:ok');
        const loads2 = hits('doc:/login');
        let termsNetBefore = 0;
        let termsOk = false;
        if (alsoTerms) {
            termsNetBefore = hits('terms:ok');
            await p2.goto(`${ORIGIN}/terms`).catch(() => { });
            const h1 = p2.getByRole('heading', { name: 'OmniRank AI 用户服务协议', level: 1 });
            await h1.first().waitFor({ state: 'visible', timeout: 15000 }).catch(() => { });
            termsOk = await h1.first().isVisible().catch(() => false);
        }
        await ctx.close();

        check(bad1 >= 1 && err1 && !form1 && (!viaButton || (errBeforeClick && beforeClick === 0)) && (!alsoTerms || (termsBad1 >= 1 && termsErr1)),
            `${id}.0 分母自证:段一源站真回了坏响应、页面真落到「${ERR}」${viaButton ? ';段二点按钮前也在错误页,目标块 0 次网络请求(全从缓存读坏响应)' : ''}`,
            `段一坏响应 ${bad1} 次 · 错误页 ${err1 ? '在' : '没有'} · 登录框 ${form1 ? '竟然在' : '没有'}`
            + (viaButton ? ` · 段二点前错误页 ${errBeforeClick ? '在' : '没有'} · 点前网络 ${beforeClick}` : '')
            + (alsoTerms ? ` · /terms 段一坏响应 ${termsBad1} 次、错误页 ${termsErr1 ? '在' : '没有'}` : ''));
        check(form2, `${id}.1 🔴 段二(源站已恢复,缓存里仍是坏响应)${viaButton ? '点「立即刷新」后' : '新标签页'}落到登录框`,
            form2 ? '登录框在' : '仍卡住(登录框没出来)');
        check(net2 >= 1, `${id}.2 段二里目标块真的走了网络(缓存被 cache:'reload' 那一下换掉)`, `目标块网络请求 ${net2} 次`);
        if (alsoTerms) {
            check(termsOk, `${id}.4 🔴 同一标签页再进 /terms 直接能开(它不是 /login 的依赖,只有读入口清单全刷才刷得到)`,
                termsOk ? '协议页标题在' : '仍卡住');
            check(termsNetBefore >= 1, `${id}.5 /terms 页面块在进 /terms 之前就已走过网络(是 /login 那次自愈顺手刷的)`,
                `进 /terms 前网络请求 ${termsNetBefore} 次`);
        }
        check(loads1 <= 3 && loads2 <= 3, `${id}.3 不空转:段一 / 段二页面加载次数都 ≤ 3`, `段一 ${loads1} 次 · 段二 ${loads2} 次`);
    }
} catch (err) {
    console.log('FAIL 判据不可用(不当绿灯):跑挂了 ' + String((err && err.stack) || err).slice(0, 700));
    process.exit(3);
} finally {
    try { await browser.close(); } catch { /* 尽力 */ }
    try { server.close(); } catch { /* 尽力 */ }
}
console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
