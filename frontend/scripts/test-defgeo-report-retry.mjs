#!/usr/bin/env node
/**
 * 门四 UX 返修判据 —— 报告页不许把瞬时 5xx 画成终态(**真浏览器 · 真组件 · 真取数**)
 *
 * ## 打在 bug 场景本身
 *
 * 门四实录:服务商打开 `/diagnosis/report/3`,6 个请求并发打在 WORKERS=1 的后端上,
 * nginx 把慢的几条打成 503;前端把这一次 503 当终态,整页画成
 * 「加载报告失败 · 返回首页」。同一端点紧接着连打 3 次全 200。
 *
 * 所以判据必须能**构造出那一次 503 再接一次 200**,并看页面最终画成什么样 ——
 * 纯函数判据(重试谓词返 true/false)证明不了"整页有没有被画成失败"。
 * 这里 esbuild 打真的 `DiagnosisReport.tsx`(连真 Provider 栈),
 * 在真 chromium 里用 `react-dom/client` 真挂载(effect 真跑、请求真发),
 * 用 Playwright 的 `page.route` 在**浏览器网络层**拦截 —— 这样 axios(XHR)
 * 与 fetch 两条路都被同一套脚本控制,不用去猜哪个库走哪个通道。
 *
 * ## 成功路径的"逐字节等价"怎么证
 *
 * 第二个 bundle 用 esbuild plugin 把 `DiagnosisReport.tsx` 的源码**换成
 * `git show <BASE>:...` 的那一份**(resolveDir 指回真目录,相对 import 照常解析),
 * 其余依赖完全相同。同一套 stub 各渲染一次,比 `#root` 的 innerHTML。
 * 比之前先做**确定性自检**:同一臂连渲两次必须逐字节相同 —— 否则这条判据
 * 比的是噪声不是改动。
 *
 * 跑法:cd frontend && node scripts/test-defgeo-report-retry.mjs
 * 退出码:0=全绿 1=有红/判据不可用
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync , mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { execFileSync } from 'node:child_process';
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
const REPO = join(ROOT, '..');
const require_ = createRequire(import.meta.url);

/** 🔴 底座钉死在门四工单给的那个 SHA,不用 HEAD —— HEAD 会随我这次提交漂走。 */
const BASE_SHA = 'fd9b63e17';
const PAGE_REL = 'frontend/src/pages/Diagnosis/DiagnosisReport.tsx';

let failed = 0;
const ok = (m, d = '') => console.log(`  \u2705 ${m}${d ? ` \u2014 ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  \u{1F534} ${m}${d ? ` \u2014 ${d}` : ''}`); failed++; };
const check = (cond, m, d = '') => (cond ? ok(m, d) : bad(m, d));
const section = (t) => console.log(`\n=== ${t} ===`);

function unusable(why, detail) {
    console.log(`\u{1F534} 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 3000));
    process.exit(1);
}

let esbuild, playwright;
try {
    esbuild = require_('esbuild');
    playwright = require_('playwright');
} catch (err) { unusable('esbuild / playwright 取不到', err); }

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
const outDir = mkdtempSync(join(CACHE_ROOT, 'defgeo-report-retry-'));
/* 🔴 a179-cleanup:跑完删掉这一份,免得 .cache 里堆满 bundle
   (一次全量注毒 = 17 发 × 两把闸 = 34 份)。进程怎么退出都删。 */
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

// ── 1. 入口:真 Provider 栈 + MemoryRouter + 真挂载 ────────────────
writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import { AuthProvider } from ${q('src/context/AuthContext.tsx')};
import { OrganizationProvider } from ${q('src/context/OrganizationContext.tsx')};
import { UserModeProvider } from ${q('src/context/UserModeContext.tsx')};
import { WalletProvider } from ${q('src/context/WalletContext.tsx')};
import { OnboardingProvider } from ${q('src/context/OnboardingContext.tsx')};
import { PricingProvider } from ${q('src/context/PricingContext.tsx')};
import { ClientProvider } from ${q('src/context/ClientContext.tsx')};
import { DiagnosisReport } from ${q('src/pages/Diagnosis/DiagnosisReport.tsx')};

// 路由探针:判据要证明「重试是就地重拉、不丢路由」,得能读到当前路径。
function LocationProbe() {
    const loc = useLocation();
    (window as any).__loc = loc.pathname;
    return null;
}

(globalThis as any).__mount = function (el: HTMLElement) {
    createRoot(el).render(
        <MemoryRouter initialEntries={['/diagnosis/report/3']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <LocationProbe />
              <Routes>
                <Route path="/diagnosis/report/:id" element={<DiagnosisReport />} />
              </Routes>
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

async function bundle(name, extraPlugins = []) {
    const outfile = join(outDir, name);
    try {
        await esbuild.build({
            entryPoints: [join(outDir, 'entry.tsx')],
            bundle: true, outfile, format: 'iife', platform: 'browser',
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
            plugins: [rawSuffixPlugin, ...extraPlugins],
            logLevel: 'silent',
        });
    } catch (err) { unusable(`打包 ${name} 失败`, err); }
    return outfile;
}

await bundle('bundle.js');

// 基线臂:把页面源码换成 BASE_SHA 那一份,其余依赖不变
let baselineSource = '';
try {
    baselineSource = execFileSync('git', ['show', `${BASE_SHA}:${PAGE_REL}`],
        { cwd: REPO, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
} catch (err) { unusable(`取不到基线源码 ${BASE_SHA}:${PAGE_REL}`, err); }
if (!baselineSource.includes('DiagnosisReport')) unusable('基线源码内容不对', baselineSource.slice(0, 200));

await bundle('bundle-base.js', [{
    name: 'baseline-report-page',
    setup(build) {
        build.onLoad({ filter: /DiagnosisReport\.tsx$/ }, (args) => ({
            contents: baselineSource, loader: 'tsx', resolveDir: dirname(args.path),
        }));
    },
}]);

for (const b of ['bundle.js', 'bundle-base.js']) {
    writeFileSync(join(outDir, b.replace('.js', '.html')), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>report retry harness</title></head>
<body><div id="root"></div><script src="./${b}"></script></body></html>
`, 'utf8');
}

// ── 2. 本地静态服 ───────────────────────────────────────────────
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };
const server = createServer((req, res) => {
    const name = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '');
    try {
        const buf = readFileSync(join(outDir, name));
        res.writeHead(200, { 'Content-Type': MIME[extname(name)] || 'application/octet-stream' });
        res.end(buf);
    } catch { res.writeHead(404); res.end('nope'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

// ── 3. stub 后端 ────────────────────────────────────────────────
const DETAIL = {
    id: 3, brand_id: 4, brand_name: '门四测试品牌', industry: '教育培训',
    total_score: 62, level: '成长期', created_at: '2026-08-20T10:00:00Z',
    status: 'completed', diagnosis_scope: 'geo',
};
/**
 * 形状完整的 BrandSummary(`features/geoObservation/types.ts:84`)。
 * 🔴 一开始我图省事回 `{data:{}}`,结果 `data.summary.stability_status` 当场
 *    TypeError —— 那是**夹具自己的窟窿**,差点被我读成被测代码的缺陷。
 *    夹具形状必须跟真契约同构,不然抓到的红一半是自己造的。
 */
const BRAND_SUMMARY = {
    brand: { id: 4, name: '门四测试品牌' },
    window: { granularity: 'day', start: '2026-08-13', end: '2026-08-20' },
    data_updated_at: '2026-08-20T10:00:00Z',
    metric_version: 'v1',
    summary: {
        valid_observations: 0,
        presence_rate_bps: null, explicit_recommendation_rate_bps: null,
        conditional_recommendation_rate_bps: null, candidate_rate_bps: null,
        criteria_only_rate_bps: null, refusal_no_evidence_rate_bps: null,
        refusal_risk_rate_bps: null, not_mentioned_rate_bps: null,
        citation_rate_bps: null, evidence_coverage_rate_bps: null,
        share_of_voice_bps: null,
        stability_status: 'stable', stability_explanation: '',
    },
    comparison: { presence_change_bps: null, recommendation_change_bps: null, comparison_allowed: false },
    outcomes: [],
    next_actions: [],
};

const AUTH_USER = {
    id: 112, username: 'gate-agent', role: 'user', agent_level: 1,
    is_admin: false, email: 'gate@local', permissions: [], user_mode: 'agent',
};

/**
 * 🔴 不带 brand_id 的一份。理由见判据 6 的注释:带 brand_id 时页面会
 *    `switchClient(bid)` → `loadReport` 的 useCallback 依赖变 → effect 二次触发 →
 *    共享请求层把**第一发在途请求 abort 掉**(api.ts:744/851/884)。
 *    基线在这种情况下必然画成失败态,两臂就没有可比的成功路径了。
 */
const DETAIL_NO_BRAND = { ...DETAIL, brand_id: null };

const CONTENT_MARK = '门四报告正文标记';
const CONTENT = { content: `# ${CONTENT_MARK}\n\n这是报告正文。`, version: 'v1' };

/**
 * 起一个页面,按 plan 回放响应。
 * plan.contentStatuses: 依次给 /content 用的 HTTP 状态码(用尽后一律 200)
 * plan.detailStatuses:  同上,给 /api/diagnosis/3
 * plan.decorativeStatus: completeness / geo-observation 的状态码
 */
async function openPage(browser, html, plan = {}) {
    const page = await browser.newPage();
    const calls = { content: 0, detail: 0 };
    const pageErrors = [];
    const consoleErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300)); });

    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'gate-token');
        // 观察器必须在**任何页面脚本之前**装好 —— 装在 mount 之后就有竞态,
        // 失败态可能在我装上之前已经闪过又消失,于是"没闪过"变成假绿。
        // 谁 abort 的?抓调用栈 —— 只看 "CanceledError: canceled" 指认不了来源。
        window.__abortLog = [];
        const _origAbort = AbortController.prototype.abort;
        AbortController.prototype.abort = function (reason) {
            try { window.__abortLog.push(new Error('abort').stack || ''); } catch { /* ignore */ }
            return _origAbort.call(this, reason);
        };
        window.__errLog = [];
        const _origErr = console.error;
        console.error = function (...a) {
            try {
                window.__errLog.push(a.map((x) => (x && x.message)
                    ? `${x.name}: ${x.message}` : String(x)).join(' '));
            } catch { /* ignore */ }
            return _origErr.apply(console, a);
        };
        window.__everFailed = false;
        document.addEventListener('readystatechange', () => {
            if (!document.body || window.__obsOn) return;
            window.__obsOn = true;
            new MutationObserver(() => {
                if (document.querySelector('[data-testid="report-load-failure"]')) {
                    window.__everFailed = true;
                }
            }).observe(document.body, { childList: true, subtree: true });
        });
    });

    const nextStatus = (list, n) => (Array.isArray(list) && n < list.length ? list[n] : 200);

    await page.route('**/api/**', async (route) => {
        const url = route.request().url();
        const json = (body, status = 200) =>
            route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

        if (/\/api\/diagnosis\/3\/content/.test(url)) {
            const s = nextStatus(plan.contentStatuses, calls.content++);
            // 🔴 503 照 nginx 的真样子回 HTML,不回 JSON ——
            //    前端那条「没有 JSON 的失败当成有 JSON 来读」的老坑要能被复现。
            if (s >= 400) return route.fulfill({ status: s, contentType: 'text/html', body: '<html><head><title>503</title></head><body>503</body></html>' });
            return json(CONTENT);
        }
        if (/\/api\/diagnosis\/3(\?|$)/.test(url)) {
            const s = nextStatus(plan.detailStatuses, calls.detail++);
            if (s >= 400) return route.fulfill({ status: s, contentType: 'text/html', body: '<html>503</html>' });
            return json(plan.noBrandId ? DETAIL_NO_BRAND : DETAIL);
        }
        if (/completeness|geo-observation/.test(url)) {
            const s = plan.decorativeStatus ?? 200;
            if (s >= 400) return route.fulfill({ status: s, contentType: 'text/html', body: '<html>503</html>' });
            if (/geo-observation/.test(url)) return json(BRAND_SUMMARY);
            return json({ success: true, items: [], data: {} });
        }
        // 🔴 /me 必须是**真形状**:AuthContext 要 `data.success && data.user`
        //    (AuthContext.tsx:660)。给个假形状的话会话确认会落到 failed,
        //    authorization epoch 一变,在途请求被 CanceledError 掐掉 ——
        //    我第一版就是这么"造"出一个根本不存在的缺陷的。
        if (/\/api\/auth\/me/.test(url)) {
            return json({ success: true, user: AUTH_USER });
        }
        if (/\/api\/auth\/refresh/.test(url)) {
            return json({ success: true, token: 'gate-token' });
        }
        if (/\/api\/diagnosis\/3\/type/.test(url)) return json({ scope: 'geo', is_legacy: false });
        if (/diagnosis-history/.test(url)) return json({ success: true, items: [] });
        if (/defensive-geo\/reports\/3\/presentation/.test(url)) return json({ isV2: false });
        return json({ success: true, items: [], data: {} });
    });

    await page.goto(`http://127.0.0.1:${PORT}/${html}`, { waitUntil: 'load' });
    await page.evaluate(() => window.__mount(document.getElementById('root')));
    return { page, calls, pageErrors, consoleErrors };
}

const FAILURE_SEL = '[data-testid="report-load-failure"]';
const RETRY_SEL = '[data-testid="report-retry"]';

/** 等到"稳定态":要么失败态出现,要么正文出现。 */
async function settle(page, timeout = 20000) {
    await page.waitForFunction(
        (mark) => !!document.querySelector('[data-testid="report-load-failure"]')
            || (document.body.innerText || '').includes(mark),
        CONTENT_MARK, { timeout },
    ).catch(() => { /* 超时交给下面的断言判 */ });
}

const snapshot = (page) => page.evaluate((mark) => ({
    hasFailure: !!document.querySelector('[data-testid="report-load-failure"]'),
    hasRetry: !!document.querySelector('[data-testid="report-retry"]'),
    hasContent: (document.body.innerText || '').includes(mark),
    text: document.body.innerText || '',
    loc: window.__loc,
    errLog: window.__errLog || [],
    abortLog: window.__abortLog || [],
    html: (document.getElementById('root') || {}).innerHTML || '',
}), CONTENT_MARK);

let browser;
try { browser = await playwright.chromium.launch(); }
catch (err) { server.close(); unusable('Chromium 起不来', err); }

// ── 判据 1:首次 503、第二次 200 ⇒ 自动重试后成功,失败态**从未上屏** ──
section('1. 瞬时 503 → 自动重试一次 → 成功(失败态不上屏)');
{
    const { page, calls, pageErrors } = await openPage(browser, 'bundle.html', { contentStatuses: [503] });
    await settle(page);
    // 观察器在 addInitScript 里、页面脚本之前就装好了(见 openPage)
    const everFailed = await page.evaluate(() => window.__obsOn === true);
    const s = await snapshot(page);
    const flashed = await page.evaluate(() => window.__everFailed);
    check(everFailed === true, '失败态观察器在页面脚本之前就装上了(判据活性)');
    check(pageErrors.length === 0, '页面无未捕获异常', pageErrors[0] || '');
    check(calls.content >= 2, `/content 真的被请求了两次(自动重试发生)`, `实际 ${calls.content} 次`);
    check(s.hasContent === true, '最终渲染出报告正文');
    check(s.hasFailure === false && flashed === false, '失败态全程未上屏(连一闪都没有)');
    await page.close();
}

// ── 判据 2:两次都 503 ⇒ 失败态 + 重试按钮 ⇒ 点击就地恢复 ──────────
section('2. 连续 503 → 失败态带「重试」→ 点击就地恢复、路由不变');
{
    const { page, calls } = await openPage(browser, 'bundle.html', { contentStatuses: [503, 503] });
    await settle(page);
    const before = await snapshot(page);
    check(before.hasFailure === true, '两次都 503 ⇒ 失败态出现');
    check(before.hasRetry === true, '失败态里有「重试」按钮');
    check(before.hasContent === false, '此时正文未渲染(确认确实处在失败态)');
    check(before.loc === '/diagnosis/report/3', '失败态下路由未被改写', String(before.loc));

    await page.click(RETRY_SEL);
    await settle(page);
    const after = await snapshot(page);
    check(after.hasContent === true, '点「重试」后就地恢复,正文渲染出来');
    check(after.hasFailure === false, '失败态已消失');
    check(after.loc === '/diagnosis/report/3', '恢复后路由仍是原路由(没被赶回首页)', String(after.loc));
    check(calls.content >= 3, '「重试」真的重新发了请求', `/content 共 ${calls.content} 次`);
    await page.close();
}

// ── 判据 3:文案锁 ───────────────────────────────────────────────
section('3. 文案:不吓人 + 带行动指引 + 把钱说死');
{
    const { page } = await openPage(browser, 'bundle.html', { contentStatuses: [503, 503] });
    await settle(page);
    const box = await page.evaluate((sel) => {
        const el = document.querySelector(sel);
        return el ? el.innerText : null;
    }, FAILURE_SEL);
    check(typeof box === 'string' && box.length > 0, '拿到失败态文案(判据活性)', box || '');
    const scary = ['错误', '异常', '无法', '丢失', '不存在', '崩溃', 'Error', 'error', 'undefined', 'null'];
    const hit = scary.filter((w) => (box || '').includes(w));
    check(hit.length === 0, '不含恐吓性/技术性表述', hit.length ? `命中 ${hit.join('/')}` : '');
    check((box || '').includes('重试'), '带行动指引(明写点「重试」)');
    check(/不额外扣算力|不会再扣|没有扣除/.test(box || ''), '把钱说死(她刚花了几千算力)');
    check(/报告还在|报告已经生成/.test(box || ''), '明说报告没丢');
    await page.close();
}

// ── 判据 4:装饰性请求 503 不许拖垮整页 ────────────────────────────
section('4. 装饰性请求(completeness / geo-observation)503 ⇒ 整页照常');
{
    const { page, pageErrors } = await openPage(browser, 'bundle.html', { decorativeStatus: 503 });
    await settle(page);
    const s = await snapshot(page);
    check(s.hasContent === true, '主请求 200 时,装饰性请求 503 不影响正文渲染');
    check(s.hasFailure === false, '未画成失败态');
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ── 判据 5:终态 4xx 不该被重试(重试只会让她白等 2 秒) ───────────
section('5. 终态 4xx:不自动重试,直接给失败态');
{
    const { page, calls } = await openPage(browser, 'bundle.html', { contentStatuses: [403, 403, 403] });
    await settle(page);
    const s = await snapshot(page);
    check(calls.content === 1, '403 只请求了一次(没有白等 2 秒的自动重试)', `实际 ${calls.content} 次`);
    check(s.hasFailure === true, '仍然给出失败态');
    check(s.hasRetry === true, '4xx 也留着「重试」按钮(她至少有个出口)');
    await page.close();
}

// ── 判据 6:成功路径与基线逐字节等价 ──────────────────────────────
section('6. 成功路径:与基线 ' + BASE_SHA + ' 渲染结果逐字节等价');
{
    /**
     * 🔴 为什么要**挂第二次**才比。
     *
     * 首次挂载时 `AuthProvider` 的 `/me` 一确认就 `rotateAuthorizationScope()`
     * → `clearApiDedupeCache()`(AuthContext.tsx:430 / api.ts:849)
     * → **把当时所有在途 GET 一律 abort**。报告页的两条主请求正好在这个窗口里,
     * 于是**基线在首次挂载时必然拿到 `CanceledError: canceled`**、画成失败态。
     * (这不是夹具造的:main.tsx 里没有任何"挂载前先确认会话"的引导步骤,
     *  生产就是这个顺序 —— 见判据 7。)
     *
     * 那个窗口只存在于引导期。第二次挂载时 scope 已经稳定,两臂都能真正走完
     * 成功路径 —— 这时比 innerHTML 才是在比"我这次改动动没动成功路径",
     * 而不是在比"谁能熬过引导期"。两臂**同样处理**,不给任何一边开小灶。
     */
    const renderOnce = async (html) => {
        const { page, pageErrors, consoleErrors } = await openPage(browser, html, { noBrandId: true });
        await settle(page);                       // 第一次:让引导期跑完(结果丢弃)
        // 🔴 那个 abort 窗口偶尔会盖到第二次挂载(撕锁 MUT-8 那轮基线臂就栽在这,
        //    于是判据 6 冒出一条与被测改动无关的红)。它是**引导期特有的瞬时窗口**,
        //    不是渲染差异。两臂**同样**最多试 3 次,谁也不开小灶;
        //    三次都拿不到成功渲染就如实判红,不当绿灯。
        let s = null;
        for (let rootN = 2; rootN <= 4 && !(s && s.hasContent); rootN++) {
            const rootId = `root${rootN}`;
            await page.evaluate((rid) => {
                const d = document.createElement('div');
                d.id = rid;
                document.body.appendChild(d);
                window.__mount(d);
            }, rootId);
            await page.waitForFunction(
                (arg) => (document.getElementById(arg.rid)?.innerText || '').includes(arg.mark),
                { rid: rootId, mark: CONTENT_MARK }, { timeout: 20000 },
            ).catch(() => { /* 交给下一轮或下面的断言判 */ });
            s = await page.evaluate((arg) => {
                const el = document.getElementById(arg.rid);
                return {
                    hasContent: (el?.innerText || '').includes(arg.mark),
                    hasFailure: !!el?.querySelector('[data-testid="report-load-failure"]'),
                    text: el?.innerText || '',
                    html: el?.innerHTML || '',
                    mounts: arg.rid,
                    errLog: window.__errLog || [],
                    abortLog: window.__abortLog || [],
                };
            }, { rid: rootId, mark: CONTENT_MARK });
        }
        await page.close();
        return { ...s, pageErrors, consoleErrors };
    };
    /**
     * React `useId` 产出的 `:r<n>:` 是**全局计数器**,跨 root 递增,值取决于
     * "在它之前有多少个 useId 被调用过"。两臂的第一次挂载渲的东西不一样
     * (基线画失败态、本臂画正文),计数器起点就不同 —— 于是第二次挂载里
     * 每个 radix 组件的 id 都差一个数。那是**比较方法的产物**,不是渲染差异。
     *
     * 所以把这一个 token 归一化掉,并**另外比一次它的出现次数** ——
     * 只归一化不比数量的话,"少渲了三个带 id 的组件"就会被一起抹平。
     */
    const RID = /:r[0-9a-z]+:/g;
    const norm = (h) => h.replace(RID, ':rN:');
    const ridCount = (h) => (h.match(RID) || []).length;

    const a1 = await renderOnce('bundle.html');
    const a2 = await renderOnce('bundle.html');
    check(a1.html.length > 2000, '成功渲染产物是整页(判据活性)', `${a1.html.length} 字节`);
    check(ridCount(a1.html) > 0, 'useId 归一化确实作用在真实存在的 token 上(判据活性)',
        `${ridCount(a1.html)} 个`);
    const deterministic = norm(a1.html) === norm(a2.html);
    check(deterministic, '确定性自检:同一臂连渲两次逐字节相同');

    if (!deterministic) {
        bad('跳过等价比对 —— 渲染不确定,比出来的差异说明不了问题');
    } else {
        const b = await renderOnce('bundle-base.html');
        if (!b.hasContent) {
            console.log('  · 基线 abort 调用栈(前 2 条):');
            const NL = String.fromCharCode(10);
            (b.abortLog || []).slice(0, 2).forEach((st, i) =>
                console.log(`    [${i}] ` + String(st).split(NL).slice(1, 6).join(' | ')));
        }
        check(b.hasContent === true, '基线臂也真渲染出来了(不是空壳对空壳)',
            b.hasContent ? '' : `err=${(b.errLog[0] || '-')} text=${(b.text || '').slice(0, 120)}`);
        const mine = norm(a1.html);
        const base = norm(b.html);
        if (mine !== base) {
            let i = 0;
            while (i < mine.length && i < base.length && mine[i] === base[i]) i++;
            console.log(`  · 首个差异在第 ${i} 字节:`);
            console.log(`    本臂 …${mine.slice(Math.max(0, i - 60), i + 90)}…`);
            console.log(`    基线 …${base.slice(Math.max(0, i - 60), i + 90)}…`);
        }
        check(ridCount(a1.html) === ridCount(b.html),
            'useId token 个数与基线相同(没有被归一化悄悄抹平的结构差异)',
            `本臂 ${ridCount(a1.html)} / 基线 ${ridCount(b.html)}`);
        check(mine === base, '成功路径 innerHTML 与基线逐字节相同(useId 计数器已归一化)',
            mine === base ? '' : `本臂 ${mine.length} 字节 / 基线 ${base.length} 字节`);
    }
}

// ── 判据 7:本次修复顺带盖住的另一条真实路径 ───────────────────────
section('7. 自取消(switchClient churn → 在途请求被 abort)也不再画成终态');
{
    // 页面拿到 brand_id 后会 switchClient(bid),loadReport 的依赖随之变化、effect 二次触发;
    // 共享请求层对同一 key 的第二个请求会把第一发 abort 掉,第一发以 CanceledError 落地。
    // 🔴 基线在这里是**数据其实已经到了、页面却画着「加载报告失败」**。
    const base = await (async () => {
        const { page } = await openPage(browser, 'bundle-base.html', {});
        await settle(page);
        const s = await snapshot(page);
        await page.close();
        return s;
    })();
    console.log(`  · 基线实测:hasFailure=${base.hasFailure} hasContent=${base.hasContent}` +
                ` err="${(base.errLog[0] || '-').slice(0, 90)}"`);

    const { page } = await openPage(browser, 'bundle.html', {});
    await settle(page);
    const mine = await snapshot(page);
    await page.close();
    check(mine.hasContent === true, '本臂:全 200 + 有 brand_id ⇒ 正文照常渲染');
    check(mine.hasFailure === false, '本臂:不因自取消画成失败态');
    check(/CanceledError|canceled/i.test(base.errLog[0] || '') || base.hasFailure === true,
        '对照存在:基线在同一场景下确实走到了取消/失败(否则这条"顺带盖住"是空话)',
        `基线 hasFailure=${base.hasFailure}`);
}

server.close();
await browser.close();

console.log('\n================================================================');
console.log(failed === 0
    ? '\u2705 门四判据通过(瞬时 503 自动恢复 · 失败态可就地重试 · 成功路径零回归)'
    : `\u{1F534} 门四判据未通过 \u00b7 ${failed} 条红`);
process.exit(failed === 0 ? 0 : 1);
