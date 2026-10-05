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
const outDir = mkdtempSync(join(CACHE_ROOT, 'a200-render-'));
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
/* [#199 a2 · R5] 缺项弹层被裁那件事只在**真正的挂载点**上才成立:
   DecisionBarBridge 那张 Card 带 overflow-hidden。自己搭一个壳子测,
   测的就是我自己搭的壳子。
   (注:这段在 entry.tsx 模板串里,不许出现反引号 —— 会截断外层模板字面量。) */
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
    quote: { el: <OnlineQuoteFlow />, entry: '/pricing' },
    publish: { el: <PublishCenter />, entry: '/publish' },
    writing: { el: <WritingHall />, entry: '/writing' },
    /* [#194] 两个新面 */
    diagnosis: { el: <NewDiagnosis />, entry: '/diagnosis/new' },
    monitoring: { el: <MonitoringPage />, entry: '/monitoring' },
    /* [a2 · R5] 资料徽章的真挂载点。brandId 用本地夹具号 9001 —— 所有请求都被桩掉,
       不碰任何真品牌。 */
    bridge: { el: <DecisionBarBridge brandId={9001} />, entry: '/writing' },
};
/* 🔴 root 只建一次。同一个容器上第二次 createRoot,React 18 会当成**两个** root
   同时挂着 —— 谁先谁后看运气,判据就此变成抛硬币。 */
let __root: any = null;
/**
 * nonce 换号 = 给被测组件换 key = React **卸载再挂**一个新实例(effect 重跑)。
 *
 * 🔴 为什么需要:有些组件在 mount 那一刻就发请求(DecisionBarBridge 的 snapshot),
 *    而本 harness 的鉴权要两三秒后才确认;第一发被请求拦截器挡在出网之前,
 *    组件落进"取不到 snapshot"分支,徽章整块不出现。
 * 🔴 为什么**不能**用"再 createRoot 一次"绕:同一容器第二次 createRoot,
 *    React 18 会当成两个 root 同时挂着 —— 谁赢看运气。实测:手跑绿、运行器里红。
 *    换 key 是同一个 root 内的正常重挂,确定性的。
 * 🔴 本段在 entry.tsx 模板串里:**不许出现反引号**,也不许出现未转义的 ${'$'}{...}。
 */
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
<html lang="zh"><head><meta charset="utf-8"><title>a200</title>
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
/* 脚手架已带 failed / ok / bad / check —— 不重复声明(同名会当场 SyntaxError) */

async function openPage(browser, plan = {}) {
    const page = await browser.newPage({ viewport: { width: plan.width || 1440, height: 1200 } });
    if (process.env.A199_DEBUG) {
        page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') console.log('  [browser]', m.text().slice(0, 220)); });
        page.on('pageerror', (e) => console.log('  [pageerror]', String(e).slice(0, 220)));
    }
    /*
     * 🔴 [a2 · R5] 沙盒态是**第二个后端**:`sandboxInterceptor` 在网络之前就把
     *    `/api/brands/:id/completeness` 接走,回的是 mockData 里那份「85 分 · 不缺项」。
     *    于是 `canExpand` 恒为 false —— **弹层被裁那件事在沙盒态里根本复现不出来**
     *    (这也正是原来的行为臂没有缺项夹具的原因)。
     *    ⇒ 需要自己夹具的面必须关掉沙盒,让请求走到 page.route 的桩上。
     */
    await page.addInitScript((noSandbox) => {
        if (noSandbox) window.__noSandbox = true;
        localStorage.setItem('omnirank_token', 'qa-token');
        if (!window.__noSandbox) localStorage.setItem('omnirank_sandbox_active', '1');
        localStorage.setItem('omnirank_onboarding_state', JSON.stringify({ completed_steps: ['first_quote'] }));
    }, !!plan.noSandbox);
    plan.counts = { pricing: 0 };
    await page.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
        if (path.includes('/auth/me')) return json(ME);
        /*
         * 🔴 [a2 · R6] 价目桩分三态,因为「禁用」这一格只有**配对**才携带信息:
         *    429 + Retry-After ⇒ 该禁用;500(同样失败、但没有退避)⇒ 该可点。
         *    只测 429 的话,一颗恒灰的按钮也能过。
         */
        if (path.includes('/wallet/pricing') || path.endsWith('/pricing')) {
            plan.counts.pricing += 1;
            if (plan.pricing429) {
                return route.fulfill({ status: 429, contentType: 'application/json',
                    headers: { 'Retry-After': '30' },
                    body: JSON.stringify({ detail: '请求过于频繁(QA 桩)' }) });
            }
            if (plan.pricingDown) {
                return route.fulfill({ status: 500, contentType: 'application/json',
                    body: JSON.stringify({ detail: '价目服务暂时不可用(QA 桩)' }) });
            }
        }
        /*
         * 🔴 [a2 · R5] 完整度夹具的形状**照后端真回包**:
         *    `api/brands/{id}/completeness` 返回的是**裸对象**
         *    `{brand_id, score, groups, missing}`(不套 success),
         *    前端 `services/m3/api.ts` 读的正是 `data.score / data.missing`。
         *    照前端字段自己造夹具,就会造出一个后端永远不会发的形状。
         */
        if (/\/api\/brands\/\d+\/completeness$/.test(path)) {
            return json({
                brand_id: 9001, score: 62,
                groups: { identity: 20, business: 18, marketing: 14, deep_analysis: 10, market_insight: 0 },
                /*
                 * 🔴 缺项数必须与分数**对得上**。第一版给「62 分 + 只缺 3 项」——
                 *    20 个字段里丢了 38 分却只缺 3 项,算术上讲不通;
                 *    而且它正好让弹层短到**塞得进**那张 Card,于是"退回 absolute"
                 *    这发毒落地了却不红(实测)。一个不真实的夹具会把缺陷藏起来。
                 *    ⇒ 取 8 项(也正好是弹层的显示上限,列表处于最高的一档)。
                 */
                missing: ['company_intro', 'selling_points', 'success_cases', 'core_value',
                    'testimonials', 'service_scope', 'local_competitors', 'authority_sources'],
            });
        }
        if (/\/api\/client-context\/\d+$/.test(path)) {
            return json({
                success: true,
                context: {
                    brand: {
                        id: 9001, name: 'QA 夹具客户', industry: '测试行业', city: '广州',
                        is_test: true, diagnosis_count: 1,
                        latest_diagnosis_id: 1, latest_diagnosis_created_at: '2026-09-13T00:00:00Z',
                    },
                    profile: {},
                },
            });
        }
        return json({ success: true, projects: [], topics: [], keywords: [], articles: [], clients: [], media: [], quotes: [] });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    /*
     * 🔴 deferMount:`DecisionBarBridge` 在 mount 那一刻就发 snapshot 请求,
     *    而本 harness 的鉴权 / 客户上下文要两三秒后才稳 —— 第一发赶在前面,
     *    组件落进 `error || !snapshot` 分支,徽章整块不出现。
     *    ⇒ 等 Provider 就绪**再挂一次**,全程只有一个 root。
     *    (不是产品缺陷:真实应用里 Provider 先于页面就绪。)
     */
    if (!plan.deferMount) {
        await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), plan.surface || 'publish');
    }
    await page.waitForTimeout(2600);
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
    if (plan.deferMount) {
        await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s, 1), plan.surface || 'publish');
        await page.waitForTimeout(1500);
        /* 换 key 重挂:第一发请求被鉴权确认挡住的话,这一发在确认之后 */
        await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s, 2), plan.surface || 'publish');
        await page.waitForTimeout(2200);
    }
    return page;
}

/** 看得见 = 在 DOM 里 + rect 非零 + 不被 hidden + opacity > 0.05 */
const visibleTexts = (page, sel) => page.evaluate((s) => [...document.querySelectorAll(s)]
    .filter((el) => {
        const r = el.getBoundingClientRect();
        const cs = getComputedStyle(el);
        return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
            && cs.display !== 'none' && Number(cs.opacity) > 0.05;
    })
    .map((el) => (el.textContent || '').replace(/\s+/g, ' ').trim()), sel);

const browser = await playwright.chromium.launch();
try {
    console.log('M #200 行为臂');
    {
        /* ── 1.1 词表「更多 4 列」:列名**在屏幕上** ────────────────────
           🔴 视口必须 < 1600:那颗开关带 `min-[1600px]:hidden`,宽屏上四列本来就常显,
              开关根本不出现 —— 在 1600 以上量它,量到的是"没有",不是"没写对"。 */
        const page = await openPage(browser, { surface: 'monitoring', width: 1440 });
        const t = await page.evaluate(() => {
            const el = document.querySelector('[data-testid="keyword-tertiary-toggle"]');
            if (!el) return { found: false };
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return {
                found: true,
                vis: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
                    && cs.display !== 'none' && Number(cs.opacity) > 0.05,
                text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
            };
        });
        check(t.found && t.vis, 'M1r0 折叠开关在屏幕上(1440 宽)', t.text || '(没有)');
        const names = ['来源', '变化', '倒计时', '已服务'];
        const missing = names.filter((n) => !(t.text || '').includes(n));
        check(t.found && missing.length === 0,
            'M1r 🔴 可见文案里**四个列名都在** —— 原来只在 title 里,手机上没有 hover',
            missing.length ? `缺 ${missing.join(',')}` : t.text);
        await page.close();
    }
    {
        /* ── 1.2 体检三档说明**同屏** ──────────────────────────────── */
        const page = await openPage(browser, { surface: 'diagnosis', width: 1440 });
        const lines = await page.evaluate(() => [...document.querySelectorAll(
            '[data-testid="launch-mode-explainer-grid"] [data-testid="launch-mode-explainer"]')]
            .map((el) => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                const txt = el.querySelector('[data-testid="launch-mode-explainer-text"]');
                return {
                    mode: el.getAttribute('data-mode'),
                    selected: el.getAttribute('data-selected'),
                    vis: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
                        && cs.display !== 'none' && Number(cs.opacity) > 0.05,
                    top: Math.round(r.top), left: Math.round(r.left),
                    text: (txt ? txt.textContent : '') || '',
                    name: (el.querySelector('[data-testid="launch-mode-explainer-name"]') || {}).textContent || '',
                };
            }));
        check(lines.length === 3, 'M2r0 三句都渲染出来了', `${lines.length} 句`);
        const invisible = lines.filter((l) => !l.vis).map((l) => l.mode);
        check(lines.length === 3 && invisible.length === 0,
            'M2r 🔴 三句**同时看得见**(rect 非零 · 真 CSS)—— '
            + '改前另外两档要点一下才知道是干什么的',
            invisible.length ? `看不见:${invisible.join(',')}` : lines.map((l) => l.mode).join(' / '));
        /* 逐字等于 modeOptions():文案不许在这一层被改写 */
        /* 🔴 原文**自己从源码解析**,不走环境变量:env 传不到下一次调用,
           而一个空数组会让这一格静默退化成"没东西可比"。 */
        const cardsSrc = readFileSync(join(ROOT, 'src/components/defensiveGeo/ModeRadioCards.tsx'), 'utf8');
        const want = [...cardsSrc.matchAll(/explainer:\s*\n?\s*'([^']+)'/g)].map((m) => m[1]);
        if (want.length === 3) {
            const mism = lines.filter((l, i) => l.text !== want[i]);
            check(mism.length === 0,
                'M2r2 🔴 三句与 `modeOptions()` **逐字相等**(这一层只搬位置,不改字)',
                mism.length ? mism.map((m) => m.mode).join(',') : '三句逐字一致');
        } else {
            bad('M2r2 取不到 modeOptions() 原文 ⇒ 无从逐字比(不当未评估:这是我自己该准备的输入)');
        }
        /* 每句带自己的标签名 —— 窄档纵排时全靠它分辨 */
        const noName = lines.filter((l) => !l.name.trim()).map((l) => l.mode);
        check(noName.length === 0, 'M2r3 每句前面带**自己那一档的标签名**',
            noName.length ? `缺:${noName.join(',')}` : lines.map((l) => l.name).join(' / '));
        /* 宽档三句并排 */
        const sameRow = lines.length === 3
            && Math.abs(lines[0].top - lines[1].top) < 24 && Math.abs(lines[1].top - lines[2].top) < 24;
        check(sameRow, 'M2r4 宽档三句并排(top 相近)', lines.map((l) => l.top).join(' / '));
        /* 恰好一句是选中态 */
        const sel = lines.filter((l) => l.selected === 'true');
        check(sel.length === 1, 'M2r5 恰好一句是选中态', `${sel.length} 句 · ${sel.map((l) => l.mode).join('')}`);
        /* 切档:选中态跟着动,而三句仍然都在 */
        await page.evaluate(() => {
            const tabs = [...document.querySelectorAll('[data-testid="launch-mode-tab"]')];
            const other = tabs.find((t) => t.getAttribute('aria-selected') !== 'true');
            if (other) other.click();
        });
        await page.waitForTimeout(400);
        const after = await page.evaluate(() => [...document.querySelectorAll(
            '[data-testid="launch-mode-explainer-grid"] [data-testid="launch-mode-explainer"]')]
            .map((el) => ({ mode: el.getAttribute('data-mode'), selected: el.getAttribute('data-selected') })));
        const selAfter = after.filter((l) => l.selected === 'true');
        check(after.length === 3 && selAfter.length === 1
            && selAfter[0].mode !== (sel[0] || {}).mode,
            'M2r6 🔴 切档后选中态换了一句,而三句**仍然都在**(不是又回到只显示一句)',
            `${(sel[0] || {}).mode} → ${(selAfter[0] || {}).mode} · 共 ${after.length} 句`);
        await page.close();
    }
    {
        /* ── 窄档纵排 ─────────────────────────────────────────────── */
        /*
         * 🔴 用 520 而不是工单字面写的 880。
         *    本组件的断点问的是**它自己那一栏**有多宽(`@container` + 560),不是页面宽度:
         *    760px 视口下这一栏还有 ~700px,三列各 ~230px,一句 28 字排两行 —— 三列此时
         *    比纵排更省竖向空间,纵排反而更差。第一版按 760 断言"应当纵排"**是测法错了**,
         *    不是实现错了(实测 top 三个都是 897,并排)。
         *    真正该测的是"窄到放不下时会不会纵排" ⇒ 取一个这一栏确实 < 560 的宽度。
         */
        const page = await openPage(browser, { surface: 'diagnosis', width: 520 });
        const tops = await page.evaluate(() => [...document.querySelectorAll(
            '[data-testid="launch-mode-explainer-grid"] [data-testid="launch-mode-explainer"]')]
            .map((el) => Math.round(el.getBoundingClientRect().top)));
        const stacked = tops.length === 3 && tops[0] < tops[1] && tops[1] < tops[2];
        check(stacked, 'M2r7 🔴 窄档(520px)三句**顺序纵排**(每句带标签名,所以仍分得清谁是谁)',
            tops.join(' / '));
        await page.close();
    }
} catch (err) {
    console.log('FAIL 判据不可用(不当绿灯):跑挂了 ' + String((err && err.stack) || err).slice(0, 700));
    process.exit(1);
} finally {
    try { await browser.close(); } catch { /* 尽力 */ }
    try { server.close(); } catch { /* 尽力 */ }
}
console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
