#!/usr/bin/env node
/**
 * 判据 · #182 —— 监测页轮询节奏(真 chromium · 真组件 · **假时钟**)。
 *
 * 🔴 这几条只有**真的把时间推过去**才证得了:轮询隔多久、停不停、
 *    切到后台还问不问。用 Playwright 的 `page.clock` 装假时钟,
 *    推 10 分钟只花几毫秒 —— 而且推的是**页面里的** setTimeout,不是我这边 sleep。
 *
 * 🔴 数的是**真实发出的请求次数**。屏幕上看不出"它在后台每分钟问两次"。
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
const outDir = mkdtempSync(join(CACHE_ROOT, 'a182-poll-'));
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
import { IdentityReviewPanel } from ${q('src/pages/Monitoring/components/IdentityReviewPanel.tsx')};

(globalThis as any).__mount = function (el: HTMLElement, entry?: string) {
    createRoot(el).render(
        <MemoryRouter initialEntries={[entry || '/publish']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <IdentityReviewPanel brandId={7} taskActive={(globalThis as any).__taskActive === true} />
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
<html lang="zh"><head><meta charset="utf-8"><title>a182 harness</title>
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
const mkItem = (id, kw) => ({
    id, keyword: kw, platform: '豆包', result_id: id,
    // 🔴 真契约里是**字符串数组**;第一版给了对象 ⇒ React 当场
    //    「Objects are not valid as a React child」,面板整块渲染不出来,
    //    于是 M2/M3b/M4 一片红 —— 那是夹具的窟窿,不是被测代码的缺陷。
    identity_candidates: ['雅栖酒店', '雅栖轻居'],
    identity_evidence_snippet: '证据片段', response_snippet: '回答片段',
    identity_decision_version: 1, evidence_hash: `h${id}`,
});

const clickSafe = (l) => l.click({ timeout: 5000 }).then(() => '').catch((e) => String((e && e.message) || e).split(String.fromCharCode(10))[0]);
const seen = (page, sel) => page.locator(sel).count();

/**
 * @param plan.itemsByCall  第 N 次请求返回哪些条目(用尽后重复最后一份)
 * @param plan.taskActive   页面上有没有监测任务在跑
 */
async function open(browser, plan = {}) {
    const page = await browser.newPage();
    const calls = [];
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    // 🔴 假时钟必须在任何页面脚本之前装好
    await page.clock.install();
    await page.addInitScript((ta) => {
        localStorage.setItem('omnirank_token', 'qa-token');
        globalThis.__taskActive = ta;
    }, !!plan.taskActive);
    await page.route('**/api/**', async (route) => {
        const url = new URL(route.request().url());
        const path = url.pathname;
        const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
        if (path === '/api/monitoring/identity-reviews') {
            const n = calls.length;
            calls.push({ at: Date.now(), path });
            const seq = plan.itemsByCall || [[]];
            const items = seq[Math.min(n, seq.length - 1)];
            return json({ items });
        }
        if (path.includes('/decision')) return json({ success: true, detected: false });
        if (path.includes('/auth/me')) {
            return json({ success: true, user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' } });
        }
        return json({ success: true, items: [] });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate(() => (globalThis).__mount(document.getElementById('root')));
    await page.waitForTimeout(600);
    return { page, calls, pageErrors };
}

/** 推进页面里的时间(假时钟),再给真实的事件循环一点时间跑完请求。 */
async function advance(page, ms) {
    await page.clock.runFor(ms);
    await page.waitForTimeout(250);
}

const browser = await playwright.chromium.launch();
try {
    // ══ M1 没事就不轮 ═════════════════════════════════════════════════
    section('M1 没待审、没任务 ⇒ 首次之后彻底停');
    {
        const { page, calls, pageErrors } = await open(browser, { itemsByCall: [[]] });
        check(pageErrors.length === 0, 'M1a 零 pageerror', pageErrors.join(' | ').slice(0, 200) || '干净');
        check(calls.length === 1, 'M1b 首次加载请求 1 次(分母非空)', `${calls.length} 次`);
        await advance(page, 10 * 60_000);
        check(calls.length === 1,
            'M1c 🔴🔴 推进 **10 分钟**,请求数仍是 1 —— 老代码这 10 分钟会打 20 次'
            + '(无条件 30 秒);Deploy 09-12 读数该端点今天 165 次',
            `${calls.length} 次`);
        await page.close();
    }

    // ══ M2 有待审:60s → 退避 → 有变化回到最快 ═══════════════════════
    section('M2 有待审时的节奏与退避');
    {
        const A = [mkItem(1, '雅栖怎么样')];
        const B = [mkItem(1, '雅栖怎么样'), mkItem(2, '雅栖贵吗')];
        // 1:A(首次) 2:A(没变) 3:A(没变) 4:B(变了) 5:B …
        const { page, calls } = await open(browser, { itemsByCall: [A, A, A, B, B, B, B] });
        console.log('       [debug] root=' + (await page.locator('#root').innerText().catch(() => 'ERR')).slice(0,160).replace(/\s+/g,' '));
        check(calls.length === 1, 'M2a 首次 1 次', `${calls.length} 次`);
        await advance(page, 59_000);
        check(calls.length === 1, 'M2b 59 秒还不到点', `${calls.length} 次`);
        await advance(page, 2_000);
        check(calls.length === 2, 'M2c 🔴 60 秒 ⇒ 第 2 次', `${calls.length} 次`);
        // 第 2 次内容没变 ⇒ streak=1 ⇒ 仍 60s
        await advance(page, 61_000);
        check(calls.length === 3, 'M2d 内容没变(streak=1)⇒ 仍 60 秒', `${calls.length} 次`);
        // 第 3 次又没变 ⇒ streak=2 ⇒ 120s
        await advance(page, 61_000);
        check(calls.length === 3,
            'M2e 🔴 连续两次没变 ⇒ **退避到 120 秒**(61 秒时还不该来)', `${calls.length} 次`);
        await advance(page, 61_000);
        check(calls.length === 4, 'M2f 满 120 秒 ⇒ 第 4 次(这一次内容变了)', `${calls.length} 次`);
        // 变了 ⇒ streak 归零 ⇒ 回到 60s
        await advance(page, 61_000);
        check(calls.length === 5,
            'M2g 🔴 有变化 ⇒ **立刻回到 60 秒档**(不是慢慢降回来 —— 有事发生时用户在等)',
            `${calls.length} 次`);
        await page.close();
    }

    // ══ M3 页面隐藏 ═══════════════════════════════════════════════════
    section('M3 切到后台就停,回来立刻问');
    {
        const A = [mkItem(1, '雅栖怎么样')];
        const { page, calls } = await open(browser, { itemsByCall: [A] });
        const before = calls.length;
        await page.evaluate(() => {
            Object.defineProperty(document, 'visibilityState', { value: 'hidden', configurable: true });
            document.dispatchEvent(new Event('visibilitychange'));
        });
        await advance(page, 10 * 60_000);
        check(calls.length === before,
            'M3a 🔴 隐藏后推 10 分钟 ⇒ **一次都不问**(用户根本没在看)',
            `${calls.length - before} 次`);
        await page.evaluate(() => {
            Object.defineProperty(document, 'visibilityState', { value: 'visible', configurable: true });
            document.dispatchEvent(new Event('visibilitychange'));
        });
        await page.waitForTimeout(400);
        check(calls.length === before + 1,
            'M3b 🔴 回到前台 ⇒ **立刻问一次**(不是等下一个周期 ——'
            + '用户切回来要看到现在的状态,不是他离开那一刻的快照)',
            `${calls.length - before} 次`);
        await page.close();
    }

    // ══ M4 手动刷新 / 后台重取 的 loading 差别 ═════════════════════════
    section('M4 只有该显示的时候才显示 loading');
    {
        const A = [mkItem(1, '雅栖怎么样')];
        const { page, calls } = await open(browser, { itemsByCall: [A] });
        const btn = page.locator('[data-testid="identity-review-refresh"]');
        check(await btn.count() === 1, 'M4a 刷新按钮在(存在理由:轮询变慢/停下后,用户要能立刻问一次)');
        const label = await btn.getAttribute('aria-label');
        check(label === '刷新待确认列表', 'M4b 图标按钮有无障碍名字', String(label));
        const n0 = calls.length;
        await clickSafe(btn);
        await page.waitForTimeout(400);
        check(calls.length === n0 + 1, 'M4c 点刷新 ⇒ 真的多发一次', `${calls.length - n0} 次`);
        // 后台重取:推进到下一个周期,过程中不该出现 loading 态的 spin
        /**
         * 🔴 锚钉在**用户看得见的那件事**上:`loading` 为真时整块面板
         *    `return null`(IdentityReviewPanel:330)—— 后台重取一旦进 loading,
         *    面板就会消失再出现,那就是 Owner 说的"闪"。
         *    第一版盯 30ms 一次的 spinner:假时钟下请求几毫秒就回,spinner 可能
         *    压根没被采到 —— 那是**采样率不够**造成的假绿,不是真的没闪。
         *    改成在整个后台重取过程中反复问"面板还在吗",一次都不许为 0。
         */
        /**
         * 🔴 **不能用轮询采样**:每次 `locator.count()` 是一次跨进程往返(5–15ms),
         *    而假时钟下请求几毫秒就回 —— 面板消失的窗口比采样间隔还短,
         *    于是"没采到"被当成"没消失"(第一版就是这么假绿的)。
         *    改成在页面里装 MutationObserver:**每一次 DOM 变动都查一遍**,漏不掉。
         */
        await page.evaluate(() => {
            (globalThis).__vanished = false;
            const check = () => {
                if (!document.querySelector('[data-testid="identity-review-panel"]')) {
                    (globalThis).__vanished = true;
                }
            };
            new MutationObserver(check).observe(document.getElementById('root'), {
                childList: true, subtree: true,
            });
        });
        await advance(page, 61_000);
        await page.waitForTimeout(300);
        const vanished = await page.evaluate(() => (globalThis).__vanished === true);
        check(!vanished,
            'M4d 🔴 后台重取过程中面板**一次都没消失** —— 进 loading 会让它整块 return null,'
            + '消失再出现就是"每 30 秒闪一下"的观感来源(08-09 那次只治了数据引用,没治这个)');
        await page.close();
    }

    // ══ 截图(Review 前端质量标准 §三):深色 / 浅色各一张 ═══════════════
    if (process.env.A182_SHOT_DIR) {
        section('截图');
        const A = [mkItem(1, '雅栖怎么样')];
        const { page } = await open(browser, { itemsByCall: [A] });
        await page.setViewportSize({ width: 1280, height: 720 });
        for (const theme of ['light', 'dark']) {
            await page.evaluate((t) => {
                document.documentElement.classList.toggle('dark', t === 'dark');
                document.documentElement.style.background = t === 'dark' ? '#0b1220' : '#ffffff';
            }, theme);
            await page.waitForTimeout(250);
            const path = `${process.env.A182_SHOT_DIR}/A_182_identity_panel_${theme}.png`;
            await page.locator('[data-testid="identity-review-panel"]')
                .screenshot({ path }).catch(async () => { await page.screenshot({ path }); });
            // 质量标准 §一.6:对比度可读 —— 顺手把正文色量出来写进日志
            const c = await page.locator('[data-testid="identity-review-panel"] p').first()
                .evaluate((el) => getComputedStyle(el).color).catch(() => 'n/a');
            console.log(`  (已写 ${theme}:${path} · 正文色 ${c})`);
        }
        await page.close();
    }

    // ══ M5 裁决后立刻重取 ═════════════════════════════════════════════
    section('M5 裁决完立刻更新');
    {
        const A = [mkItem(1, '雅栖怎么样')];
        const { page, calls } = await open(browser, { itemsByCall: [A, []] });
        const n0 = calls.length;
        // 真控件是候选名旁边的「是这个品牌」——按文字定位,别拿"第一个 button"猜
        const confirmBtn = page.locator('button:has-text("是这个品牌")').first();
        const err = await clickSafe(confirmBtn);
        await page.waitForTimeout(600);
        check(calls.length > n0,
            'M5a 🔴 裁决后**立刻重取**(不等下一个周期)—— 等 60 秒才更新看起来就像"点了没反应"',
            `${calls.length - n0} 次${err ? ' · 点击:' + err.slice(0, 60) : ''}`);
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
