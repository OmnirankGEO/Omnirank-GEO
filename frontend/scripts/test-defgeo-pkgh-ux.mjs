#!/usr/bin/env node
/**
 * 包H · U-10 浏览器级正样本判据 —— **真组件 · 真 Provider 栈 · 真 chromium**。
 *
 * §0.5.5 U-10 逐字:「U-3 进度条、U-4 两个必答时刻、U-5 待办清单、
 * U-6 深链回环**各配一条浏览器级正样本判据**」。
 *
 * ## 为什么必须是浏览器级
 *
 * 这四条要证的都不是"某个函数返回值对",而是"**她那一屏上有没有那句话/那颗按钮**"。
 * 纯函数判据证明不了这个:`priceUnchanged()` 返 true 和"按钮上真的写着
 * 「价格没变，直接确认」"之间隔着渲染、隔着状态机、隔着 effect 有没有跑。
 * 门四那轮的教训就是这条(重试谓词返 true,整页照样画成失败态)。
 *
 * 所以照 `test-defgeo-report-retry.mjs`(门四)与 `test-defgeo-legacy-required.mjs`
 * (G2b)的模式:esbuild 打**真的**组件(连真 Provider 栈),
 * chromium 里 `react-dom/client` 真挂载(effect 真跑、请求真发),
 * 用 Playwright `page.route` 在**浏览器网络层**回放响应 ——
 * axios(XHR)与 fetch 两条通道都被同一套脚本控制。
 *
 * ## 判据活性
 *
 * 每一组的第一条断言都是"这一屏真的渲染出来了"(而不是空壳),
 * 否则后面的"没有出现 X"全部是**零分母恒绿**。
 *
 * 跑法:cd frontend && node scripts/test-defgeo-pkgh-ux.mjs
 * 退出码:0=全绿 1=有红/判据不可用
 *
 * 🔴 要 chromium,按本仓惯例(见 verify:publish-layout-interaction /
 *    verify:defgeo-legacy-required)**不进 build 链** ——
 *    build 链里守着的是 verify-defgeo-pkgh-ui.mjs 那几条不需要浏览器的静态锁。
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync , mkdtempSync } from 'node:fs';
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
const ok = (m, d = '') => console.log(`  \u2705 ${m}${d ? ` \u2014 ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  \u{1F534} ${m}${d ? ` \u2014 ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
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
const outDir = mkdtempSync(join(CACHE_ROOT, 'defgeo-pkgh-ux-'));
/* 🔴 a179-cleanup:跑完删掉这一份,免得 .cache 里堆满 bundle
   (一次全量注毒 = 17 发 × 两把闸 = 34 份)。进程怎么退出都删。 */
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

// ── 入口:四组各挂一个真组件,共用真 Provider 栈 ────────────────────
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

import { CommercialProgressBar } from ${q('src/components/defensiveGeo/CommercialProgressBar.tsx')};
import { LaunchPanel } from ${q('src/components/defensiveGeo/LaunchPanel.tsx')};
import DeliveryTodoList from ${q('src/pages/DefensivePublish/DeliveryTodoList.tsx')};
import { NewDiagnosis } from ${q('src/pages/Diagnosis/NewDiagnosis.tsx')};
import { DiagnosisProgress } from ${q('src/pages/Diagnosis/DiagnosisProgress.tsx')};

function LocationProbe() {
    const loc = useLocation();
    (window as any).__loc = loc.pathname + loc.search;
    return null;
}

const PLAN = {
    planId: 'plan-1', planRevision: 1, mode: 'defensive',
    modeUserLabel: '先守住品牌',
    counts: { total: 6, defensive: 6, offensive: 0 },
    canonicalHash: 'h1', canonicalHashVersion: 'v1',
    questions: [], platformKeys: ['qwen'],
    copyRegistryVersion: 'defensive-geo-copy-v2',
} as any;

function Stack({ entries, children }: any) {
    return (
        <MemoryRouter initialEntries={entries}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <LocationProbe />
              {children}
            </ClientProvider></PricingProvider></OnboardingProvider>
          </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
        </MemoryRouter>
    );
}

(globalThis as any).__mountProgress = function (el: HTMLElement, view: any) {
    createRoot(el).render(<Stack entries={['/x']}><CommercialProgressBar view={view} /></Stack>);
};

(globalThis as any).__mountLaunch = function (el: HTMLElement) {
    createRoot(el).render(
        <Stack entries={['/diagnosis/new']}>
          <LaunchPanel plan={PLAN} profileRevisionId="rev-1"
                       platformKeys={['qwen']} onConfirmed={() => { (window as any).__confirmed = true; }} />
        </Stack>
    );
};

// 🔴 引导期专用的空挂载。理由见判据 U-5 里的注释:AuthProvider 的 /me 一确认就
//    rotateAuthorizationScope() → clearApiDedupeCache() → **把当时所有在途 GET 一律 abort**。
//    待办页的取数正好落在这个窗口里,于是"请求根本没发出去"。
//    先空挂一次让引导期跑完,再挂真组件 —— 这是引导期特有的瞬时窗口,不是被测缺陷。
(globalThis as any).__mountBootstrap = function (el: HTMLElement) {
    createRoot(el).render(<Stack entries={['/x']}><span data-testid="bootstrap-ready">ok</span></Stack>);
};

// ── [门八第二发现] 真 NewDiagnosis → 真 LaunchPanel → 真跳转 → 真 DiagnosisProgress ──
//
// 🔴 被测缺陷就是 NewDiagnosis 里 onConfirmed 那一句 navigate 挑了哪个字段,
//    所以夹具**不能**自己写一句 navigate:一旦我在这里写
//    navigate('/diagnosis/progress/' + r.progressSessionId),判据就变成我跟我自己对话 ——
//    NewDiagnosis 里写成 runId 也照样全绿。这里挂的是**整页真组件**,
//    落地页也是**真的** DiagnosisProgress(它自己会去打 /status,那一发就是用户的第一发)。
(globalThis as any).__mountNewDiagnosis = function (el: HTMLElement) {
    createRoot(el).render(
        <Stack entries={['/diagnosis/new']}>
          <Routes>
            <Route path="/diagnosis/new" element={<NewDiagnosis />} />
            <Route path="/diagnosis/progress/:id" element={<DiagnosisProgress />} />
          </Routes>
        </Stack>
    );
};

(globalThis as any).__mountTodo = function (el: HTMLElement) {
    createRoot(el).render(
        <Stack entries={['/defensive-geo/publish/todo?acceptedSnapshotId=7']}>
          <Routes>
            <Route path="/defensive-geo/publish/todo" element={<DeliveryTodoList />} />
            <Route path="/defensive-geo/publish/decision/:id" element={<div data-testid="landed-on-confirm">落到确认页</div>} />
          </Routes>
        </Stack>
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
} catch (err) { unusable('打包失败', err); }

writeFileSync(join(outDir, 'bundle.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>pkgH ux harness</title></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>
`, 'utf8');

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

// ══════════════════════════════════════════════════════════════════════
// [工单 V5-B · Codex fix-of-fix3 P2-NEW-5 ③] 夹具吃**真 registry**,不吃我手写的假文案
//
// Codex 原话:「浏览器夹具使用了已经修正的假文案,未锁真实信封」。
// 上一版我在 fixture 里手写了 publicExplanation 与 nextAction.label ——
// 那等于判据在跟自己对话:后端把话改了(甚至改错了),它一样全绿。
//
// 现在从**生成物** `src/lib/defensiveGeoCopy.ts` 读。那个文件由
// `scripts/defgeo_census/emit_frontend_copy.py` 从后端 registry 机械导出,
// 并由 `verify-defgeo-copy-registry.mjs` 逐字节比对 —— 后端改一个字而没重新生成,
// 那一道先红。所以这里读到的就是 registry 里那几个字。
// ══════════════════════════════════════════════════════════════════════
const COPY = (() => {
    const src = readFileSync(join(ROOT, 'src/lib/defensiveGeoCopy.ts'), 'utf8');
    const body = src.slice(src.indexOf('DEFGEO_COPY'));
    const out = {};
    for (const m of body.matchAll(/^\s{4}([A-Za-z0-9_]+):\s*"((?:[^"\\]|\\.)*)",\s*$/gm)) {
        out[m[1]] = m[2];
    }
    return out;
})();

for (const k of ['pricingDeactivatedAfterPreview', 'newPreviewAction', 'topUpAction',
                 'policyUnavailable', 'insufficientPoints']) {
    if (!COPY[k]) unusable(`registry 生成物里取不到 ${k} —— 夹具会退回假文案,那等于没锁`);
}

const AUTH_USER = {
    id: 112, username: 'pkgh-agent', role: 'user', agent_level: 1,
    is_admin: false, email: 'pkgh@local', permissions: [], user_mode: 'agent',
};

// ══════════════════════════════════════════════════════════════════════
// [门八第二发现] confirm 交出去的是**两把不同的键**,夹具照后端的真实关系造:
//   · runId               = run_token
//   · progressSessionId   = diagnosis_runs.session_id = "defgeo_" + run_token
//     (`services/defensive_geo/run_executor.progress_session_id` 是唯一拼法)
// legacy 进度端点/WS 的 `auth.session_access.authorize_session` 只按 session_id 查,
// 查不到就 fail-closed 403 —— 所以拿 runId 去拼路径,她付完钱看到的是「无权访问」。
// 🔴 前缀只在这一处出现一次;下面的 /status 路由不再手写一遍,直接拿 PROGRESS_SID 比。
//    判据要区分的是"前端挑了哪个字段",不是"我会不会拼字符串"。
// ══════════════════════════════════════════════════════════════════════
const RUN_TOKEN = 'run_3805gate8';
const PROGRESS_SID = `defgeo_${RUN_TOKEN}`;
/** 进度真的开始跑之后那一行日志 —— 用来证"横幅撤了之后她看到的是进度,不是空屏"。 */
const GATE8_RUNNING_LINE = '正在问第 1 批问题';
/** 前端那句"任务不存在"横幅 —— 🔴 **从源码现取**,不手写。
 *  手写的话:哪天那句话改了,"屏幕上不该出现它"这条断言会因为**找不到**而恒绿
 *  (本仓记过「夹具里手写对客文案 = 判据在跟自己对话」)。取不到就判据不可用。 */
const NOT_FOUND_BANNER = (() => {
    const src = readFileSync(join(ROOT, 'src/pages/Diagnosis/DiagnosisProgress.tsx'), 'utf8');
    const m = src.match(/setError\("(未找到[^"]*)"\)/);
    if (!m) unusable('DiagnosisProgress 里取不到"未找到"那句横幅原文 —— 断言会因找不到而恒绿');
    return m[1];
})();
const GATE8_BRAND = { id: 901, name: '门八体检品牌', industry: '教育培训' };
const GATE8_PLAN = {
    planId: 'plan-gate8', planRevision: 1,
    canonicalHash: 'h1', canonicalHashVersion: 'v1',
    brandId: GATE8_BRAND.id, profileRevisionId: `brand-${GATE8_BRAND.id}`,
    mode: 'defensive', modeUserLabel: '先守住品牌',
    questions: [], counts: { defensive: 3, offensive: 0, total: 3 },
    expiresAt: '2099-01-01T00:00:00Z',
    copyRegistryVersion: 'defensive-geo-copy-v2', idempotentReplay: false,
};

/**
 * 起一页。`plan` 描述这一组要回放什么。
 * 🔴 /me 必须是真形状(`{success, user}`)—— 假形状会让会话确认落 failed,
 *    authorization epoch 一变在途请求被 abort,于是"造"出一个不存在的缺陷。
 */
async function openPage(browser, plan = {}) {
    const page = await browser.newPage();
    // lastReqAt:最后一次被拦到的 /api/ 请求时刻,用来判"引导期的请求潮退干净了没"。
    // [V5-A · P2-1] previewKeys/confirmKeys:逐次记下 Idempotency-Key。
    // 「用户重新发起换新键 / 网络重试同键」这条只能靠**真发出去的请求头**来证 ——
    // 读源码只能证明代码里写了 setState,证不了它真的换了键发出去。
    const calls = { preview: 0, confirm: 0, todo: 0, lastReqAt: 0,
                    previewKeys: [], confirmKeys: [],
                    // [门八第二发现] 进度端点收到的 id 与它给出的状态码,逐次记下。
                    // 「跳对了没有」只能靠**真发出去的那一发请求**来证。
                    statusIds: [], statusCodes: [], statusFound: [], questionPlans: 0 };
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));

    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'pkgh-token');
    });

    const seen = [];
    page.on('response', (r) => { if (/\/api\//.test(r.url())) seen.push(`${r.status()} ${r.url()}`); });
    await page.route('**/api/**', async (route) => {
        const url = route.request().url();
        seen.push(`ROUTE ${url}`);
        calls.lastReqAt = Date.now();
        const json = (body, status = 200) =>
            route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });

        if (/\/api\/auth\/me/.test(url)) return json({ success: true, user: AUTH_USER });
        if (/\/api\/auth\/refresh/.test(url)) return json({ success: true, token: 'pkgh-token' });

        // ── [门八第二发现] NewDiagnosis 整页跑起来需要的三条 ──────────────
        if (/\/api\/my-clients/.test(url)) return json({ clients: [GATE8_BRAND] });
        if (/\/question-plans\/preview$/.test(url)) { calls.questionPlans++; return json(GATE8_PLAN); }
        // legacy 进度端点:归属校验只认 diagnosis_runs.session_id。
        // 键不对 ⇒ authorize_session 一条都命中不到 ⇒ fail-closed 403(真后端就是这样)。
        const st = url.match(/\/api\/diagnosis\/session\/([^/?]+)\/status/);
        if (st) {
            const id = decodeURIComponent(st[1]);
            calls.statusIds.push(id);
            if (id !== PROGRESS_SID) {
                calls.statusCodes.push(403);
                return json({ detail: '无权访问该诊断进度' }, 403);
            }
            calls.statusCodes.push(200);
            // [门八第三发现] 前 N 发照"还没被执行器领走"那一格回:200 但 found:false。
            // 这就是 confirm 之后到 cron 领取之间用户真正会撞上的那一段。
            if (calls.statusIds.length <= (plan.statusNotFound || 0)) {
                calls.statusFound.push(false);
                return json({ found: false, message: '未找到该诊断任务' });
            }
            calls.statusFound.push(true);
            // 形状照后端 `{"found": True, **manager.get_task_status(sid)}`。
            return json({
                found: true, type: 'progress', stage: 'collecting',
                progress: 12, message: GATE8_RUNNING_LINE, done: false,
            });
        }

        if (/\/defensive-geo\/run-previews$/.test(url)) {
            const n = calls.preview++;
            calls.previewKeys.push(route.request().headers()['idempotency-key'] || '');
            // [V5-A] 第 n 次 preview 要不要故意失败(用来打"网络重试同键"那一臂)。
            if (Array.isArray(plan.previewFails) && plan.previewFails[n]) {
                return json({ detail: plan.previewFails[n] }, 503);
            }
            const points = Array.isArray(plan.previewPoints)
                ? (plan.previewPoints[n] ?? plan.previewPoints[plan.previewPoints.length - 1])
                : 1200;
            return json({
                previewId: `pv-${n}`, canonicalHash: `ch-${n}`, canonicalHashVersion: 'v1',
                questionPlanId: 'plan-1', questionPlanRevision: 1, profileRevisionId: 'rev-1',
                platformKeys: ['qwen'], plannedCells: 24,
                baseCostPoints: points, extraCostPoints: 0, exactTotalPoints: points,
                costUserLabel: `本次体检消耗你的算力 ${points}`,
                fundingPolicy: 'personal_wallet', fundingPolicyUserLabel: '从你的算力扣',
                expiresAt: '2099-01-01T00:00:00Z', canConfirm: true,
                nextAction: { kind: 'view_result', label: '查看体检结果', actionRef: 'r', target: {} },
                copyRegistryVersion: 'defensive-geo-copy-v2',
            });
        }
        if (/\/defensive-geo\/run-previews\/.+\/confirm$/.test(url)) {
            const n = calls.confirm++;
            calls.confirmKeys.push(route.request().headers()['idempotency-key'] || '');
            // [V5-A · P2-1] 价目在她预览之后被停用 —— 后端**每次**都返同一个
            // 503 POLICY_UNAVAILABLE + nextAction.kind=new_preview。
            // 刻意不是"第一次才返":被测缺陷正是"按钮重放同一次 confirm",
            // 只在第一次返的话,第二次 confirm 会成功,缺陷就被夹具替我修好了。
            if (plan.confirmPolicyUnavailableAlways) {
                // 🔴 文案与 nextAction.label 都取自**真 registry**(见上面 COPY)。
                return json({
                    detail: {
                        code: 'POLICY_UNAVAILABLE', retryable: true,
                        publicExplanation: COPY.pricingDeactivatedAfterPreview,
                        nextAction: {
                            kind: 'new_preview', label: COPY.newPreviewAction,
                            actionRef: 'r',
                            target: { kind: 'question_plan', id: 'plan-1' },
                        },
                    },
                }, 503);
            }
            // [V5-B · P2-NEW-5] 余额不足 → top_up + 钱包页。站内**真的有** /wallet。
            if (plan.confirmInsufficientAlways) {
                return json({
                    detail: {
                        code: 'INSUFFICIENT_POINTS', retryable: false,
                        publicExplanation: COPY.insufficientPoints,
                        nextAction: {
                            kind: 'top_up', label: COPY.topUpAction, actionRef: 'r',
                            target: { kind: 'page', page: 'wallet' },
                        },
                    },
                }, 402);
            }
            // [V5-B · P3-NEW-1] 一个**没有站内落点**的 kind:面板保持在原地
            // (只出文字、不跳走),于是"再点一次主按钮"就是一次真实的 confirm 重试。
            if (plan.confirmNoDestinationAlways) {
                return json({
                    detail: {
                        code: 'POLICY_UNAVAILABLE', retryable: true,
                        publicExplanation: COPY.policyUnavailable,
                        nextAction: {
                            kind: 'wait', label: '稍等，正在体检', actionRef: 'r', target: {},
                        },
                    },
                }, 503);
            }
            if (plan.confirmExpiresFirst && n === 0) {
                return json({
                    detail: {
                        code: 'PREVIEW_EXPIRED', retryable: false,
                        publicExplanation: '刚才那一步已过期，请重新发起；没有扣除任何算力。',
                        nextAction: { kind: 'new_preview', label: '重新发起体检', actionRef: 'r', target: {} },
                    },
                }, 409);
            }
            return json({
                // 🔴 [门八第二发现] 照后端 `RunConfirmResponse` 的**真形状**回:
                //    runId 与 progressSessionId 是两把不同的键。上一版夹具只回了
                //    commandId 这半截,于是"前端挑了哪个字段"这件事在浏览器里根本不可见。
                diagnosisCommandId: RUN_TOKEN, runId: RUN_TOKEN,
                progressSessionId: PROGRESS_SID,
                statusUrl: `/api/diagnosis/session/${PROGRESS_SID}/status`,
                commandId: 'cmd-1', runStatus: 'queued', runStateUserLabel: '排队中',
                idempotentReplay: false, fundingPolicy: 'personal_wallet',
                principalKind: 'user', billingModeProjection: 'paid',
                fundingState: 'frozen', fundingStateUserLabel: '算力已冻结（本次体检预留）',
                fundingHandle: { kind: 'freeze', ref: 'frz-1', approvalRef: null },
                sponsorPolicyRef: null,
                exactTotalPoints: 1200, costUserLabel: '本次体检消耗你的算力 1200',
                nextAction: { kind: 'wait', label: '稍等，正在体检', actionRef: 'r', target: {} },
                copyRegistryVersion: 'defensive-geo-copy-v2',
                runStatusProjectionVersion: 'defgeo-run-status-v1',
            });
        }
        if (/\/defensive-geo\/publish\/delivery-todo/.test(url)) {
            calls.todo++;
            return json(plan.todo ?? DEFAULT_TODO);
        }
        return json({ success: true, items: [], data: {} });
    });

    // 🔴 `?goal=` 走的是**真的** window.location.search:NewDiagnosis 的
    //    campaignMode 初值就取自它(MemoryRouter 的 entries 影响不到那一句)。
    await page.goto(`http://127.0.0.1:${PORT}/bundle.html${plan.search || ''}`,
                    { waitUntil: 'load' });
    return { page, calls, pageErrors, seen };
}

/**
 * 等"引导期的请求潮退干净"。
 *
 * 🔴 为什么需要它:`AuthProvider` 的 `/me` 一确认就 `rotateAuthorizationScope()`
 *    → `clearApiDedupeCache()` → **把当时所有在途 GET 一律 abort**。
 *    原来的做法是等 `auth/me` 的 resource entry 出现就挂真组件 —— 那只等到了
 *    "响应到了",而 abort 是在那之后几个 tick 才发生的,所以窗口没关上:
 *    实测 5 连跑抖 1 次(rows=0),而且 abort 不会让组件进错误态,
 *    页面上那颗「再试一次」根本不存在,于是重试分支 `break` 掉、判据直接红。
 *
 * 抖动的锁 = 下一个人会把它当噪音忽略 = 等于没有锁。所以改成**等静默**:
 * 连续 `quietMs` 没有任何 /api/ 请求被拦到,才认为引导期结束。
 * 这不是放宽判据 —— 断言一条没动,只是把挂载时机挪出 abort 窗口。
 */
const settleNetwork = async (page, calls, { quietMs = 500, timeoutMs = 10000 } = {}) => {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
        const since = calls.lastReqAt ? Date.now() - calls.lastReqAt : quietMs + 1;
        if (since >= quietMs) return true;
        await page.waitForTimeout(Math.min(quietMs - since + 20, 200));
    }
    return false;                       // 超时不抛:让后面的断言去红,红在业务格上
};

const DEFAULT_TODO = {
    acceptedSnapshotId: 7,
    items: [
        {
            planItemKey: 'q1-a', publishSlotId: 's1', decisionSnapshotId: 'snap-1',
            lifecycle: 'open', todoState: 'awaiting_confirm', todoLabel: '等你确认媒体方案',
            exactPoints: 480, publicMediaName: '某垂直媒体 A', expiresAt: null, statusUrl: null,
        },
        {
            planItemKey: 'q2-b', publishSlotId: 's2', decisionSnapshotId: 'snap-2',
            lifecycle: 'open', todoState: 'awaiting_confirm', todoLabel: '等你确认媒体方案',
            exactPoints: 520, publicMediaName: '某垂直媒体 B', expiresAt: null, statusUrl: null,
        },
        {
            planItemKey: 'q3-c', publishSlotId: 's3', decisionSnapshotId: null,
            lifecycle: null, todoState: 'in_progress', todoLabel: '已确认，正在发布',
            exactPoints: 300, publicMediaName: '某垂直媒体 C', expiresAt: null, statusUrl: '/x',
        },
    ],
    pendingCount: 2, totalPendingPoints: 1000,
};

/** 文案三禁:恐吓词 / 工程术语 / 供应商穿透。 */
const SCARY = ['错误', '异常', '失败了', '崩溃', 'Error', 'undefined', 'null', 'NaN'];
const ENGINEERING = ['snapshot', 'revision', 'token', 'preview_id', 'runState', 'fundingState',
    'idempotency', 'canonical', 'commandId', 'planItemKey'];
const VENDORS = ['qwen', 'deepseek', 'kimi', 'doubao', 'openrouter', 'dashscope'];

function copyViolations(text) {
    const t = String(text || '');
    return {
        scary: SCARY.filter((w) => t.includes(w)),
        engineering: ENGINEERING.filter((w) => t.includes(w)),
        vendors: VENDORS.filter((w) => t.toLowerCase().includes(w)),
    };
}

let browser;
try { browser = await playwright.chromium.launch(); }
catch (err) { server.close(); unusable('Chromium 起不来', err); }

// ══════════════════════════════════════════════════════════════════════
// U-3:五里程碑折成一条人话进度条
// ══════════════════════════════════════════════════════════════════════
section('U-3 · 五格进度条(画的是服务端下发的 steps,每态恰一个下一步)');
{
    const { page, pageErrors } = await openPage(browser);
    // 🔴 服务端下发的 steps **故意与前端可能写死的五个词不同** ——
    //    这样"前端偷偷用自己那份常量"会被当场抓到。
    const SERVER_STEPS = ['报价已发', '客户已同意', '待核单', '已收款', '服务中'];
    const PROBE_STEP = '待核单·服务端版';
    await page.evaluate(({ steps, probe }) => {
        window.__mountProgress(document.getElementById('root'), {
            milestone: 'sales_validated',
            userLabel: '待核单',
            stepIndex: 3, stepTotal: 5,
            steps: [steps[0], steps[1], probe, steps[3], steps[4]],
            nextAction: { kind: 'collect_payment', label: '与客户完成收款对账' },
            projectionVersion: 'defgeo-milestone-v1',
        });
    }, { steps: SERVER_STEPS, probe: PROBE_STEP });
    await page.waitForSelector('[data-testid="commercial-progress"]', { timeout: 10000 })
        .catch(() => {});

    const s = await page.evaluate(() => {
        const root = document.querySelector('[data-testid="commercial-progress"]');
        const steps = Array.from(document.querySelectorAll('[data-testid="commercial-progress-step"]'));
        return {
            mounted: !!root,
            text: root ? root.innerText : '',
            stepTexts: steps.map((e) => e.innerText.trim()),
            currentCount: steps.filter((e) => e.getAttribute('data-state') === 'current').length,
            nextCount: document.querySelectorAll('[data-testid="commercial-progress-next"]').length,
            nextLabel: document.querySelector('[data-testid="commercial-progress-next"]')?.innerText || '',
        };
    });

    check(s.mounted === true, '进度条真的渲染出来了(判据活性)');
    check(s.stepTexts.length === 5, '恰五格', `实际 ${s.stepTexts.length}`);
    check(s.stepTexts.includes(PROBE_STEP),
        '画的是**服务端下发**的那份 steps(不是前端写死的五个词)',
        s.stepTexts.join(' / '));
    check(s.currentCount === 1, '当前格恰一个', `实际 ${s.currentCount}`);
    check(s.nextCount === 1, '每态恰一个「下一步」按钮(U-3 逐字)', `实际 ${s.nextCount}`);
    check(s.nextLabel.includes('收款'), '下一步文案来自服务端', s.nextLabel);
    const v = copyViolations(s.text);
    check(v.engineering.length === 0, '不含工程术语(commercial_basis_established 等禁上屏)',
        v.engineering.join('/'));
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// U-4:两个必答时刻
// ══════════════════════════════════════════════════════════════════════
section('U-4 · 涉钱两个必答时刻(按之前说清要花多少 / 按之后说清钱动了没有)');
{
    const { page, pageErrors } = await openPage(browser);
    await page.evaluate(() => window.__mountLaunch(document.getElementById('root')));
    await page.waitForSelector('button', { timeout: 10000 }).catch(() => {});

    // ① 还没算价时,屏幕上**不应该**已经出现"要花多少"——
    //    否则"先解释后出现"这条就无从证明(它可能一直都在)。
    const before = await page.evaluate(() => document.body.innerText);
    check(/看看要花多少算力/.test(before), '初始态给的是「看看要花多少算力」(判据活性)');
    check(!/消耗你的算力\s*\d/.test(before), '算价前屏幕上没有具体金额');

    await page.click('text=看看要花多少算力');
    await page.waitForFunction(() => /消耗你的算力/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    const priced = await page.evaluate(() => document.body.innerText);
    check(/消耗你的算力\s*1200/.test(priced), '必答时刻①:确认按钮之前就写清了要花多少', '');
    check(/从你的算力扣/.test(priced), '同时说清从哪条钱腿扣');
    check(/确认并开始体检/.test(priced), '此时主行动是「确认并开始体检」');

    await page.click('text=确认并开始体检');
    await page.waitForFunction(() => /已开始体检/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    const done = await page.evaluate(() => document.body.innerText);
    check(/已开始体检/.test(done), '必答时刻②:按下之后有确定性反馈');
    const v = copyViolations(done);
    check(v.vendors.length === 0, '全程零供应商穿透', v.vendors.join('/'));
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// U-5:交付待办聚合页
// ══════════════════════════════════════════════════════════════════════
section('U-5 · 交付待办聚合页(一页逐项 · 资金仍逐项 · 点进去落到确认页)');
{
    const { page, calls, pageErrors, seen } = await openPage(browser);
    // ① 先让会话引导期跑完(见 __mountBootstrap 的注释)
    await page.evaluate(() => window.__mountBootstrap(document.getElementById('root')));
    await page.waitForSelector('[data-testid="bootstrap-ready"]', { timeout: 10000 }).catch(() => {});
    // 🔴 等到**请求潮退干净**为止,不是等 /me 的 resource entry 出现 ——
    //    abort 发生在响应之后几个 tick,只等响应等不出窗口(实测 5 连跑抖 1 次)。
    await settleNetwork(page, calls);
    // ② 再挂真组件
    await page.evaluate(() => {
        const d = document.createElement('div');
        d.id = 'root2';
        document.body.appendChild(d);
        window.__mountTodo(d);
    });
    await page.waitForSelector('[data-testid="delivery-todo-row"]', { timeout: 10000 })
        .catch(() => {});
    // 🔴 引导期那个 abort 窗口偶尔会盖到第二次挂载(实测:同一条命令连跑两遍,
    //    第二遍 rows=0)。它是**引导期特有的瞬时窗口**,不是被测缺陷 ——
    //    门四判据里对同一现象的处置也是"两臂同样最多重试三次,谁也不开小灶"。
    //    这里走的是页面上真有的那颗「再试一次」,不是绕过 UI 直接重发请求。
    for (let attempt = 0; attempt < 2; attempt++) {
        const empty = await page.evaluate(
            () => document.querySelectorAll('[data-testid="delivery-todo-row"]').length === 0);
        if (!empty) break;
        const retry = await page.$('text=再试一次');
        if (!retry) break;
        await retry.click();
        await page.waitForSelector('[data-testid="delivery-todo-row"]', { timeout: 10000 })
            .catch(() => {});
    }

    const s = await page.evaluate(() => ({
        mounted: !!document.querySelector('[data-testid="delivery-todo-page"]'),
        rows: document.querySelectorAll('[data-testid="delivery-todo-row"]').length,
        pending: document.querySelector('[data-testid="delivery-todo-pending"]')?.innerText || '',
        moneyNote: document.querySelector('[data-testid="delivery-todo-money-note"]')?.innerText || '',
        confirmButtons: document.querySelectorAll('[data-testid="delivery-todo-confirm"]').length,
        firstRowState: document.querySelector('[data-testid="delivery-todo-row"]')
            ?.getAttribute('data-todo-state') || '',
        text: document.body.innerText,
    }));

    check(s.mounted === true, '聚合页真的渲染出来了(判据活性)');
    if (calls.todo === 0) {
        console.log('  · 实际拦到的请求:', seen.slice(0, 8));
        console.log('  · 页面文本:', JSON.stringify(s.text).slice(0, 400));
        console.log('  · __loc:', await page.evaluate(() => window.__loc));
    }
    check(calls.todo >= 1, '真的请求了 delivery-todo', `${calls.todo} 次`);
    check(s.rows === 3, '三项全部逐项列出(不是只显示待确认的那两条)', `实际 ${s.rows}`);
    check(/还有 2 项等你确认/.test(s.pending), '顶部数出待确认项数', s.pending);
    check(/合计 1000 算力/.test(s.text), '合计算力来自逐项累加');
    // 🔴 U-5 的核心承诺:资金仍逐项。这句话不在 = 她会怕一按全扣。
    check(/各自确认|各自计费|不会一次性全扣/.test(s.moneyNote),
        '明说「各自确认、各自计费,不会一次性全扣」', s.moneyNote);
    check(s.confirmButtons === 2, '恰两颗「确认下一项」(只有待确认项才有)',
        `实际 ${s.confirmButtons}`);
    check(s.firstRowState === 'awaiting_confirm', '待确认项排在最前');

    await page.click('[data-testid="delivery-todo-confirm"]');
    await page.waitForSelector('[data-testid="landed-on-confirm"]', { timeout: 8000 })
        .catch(() => {});
    const landed = await page.evaluate(() => ({
        landed: !!document.querySelector('[data-testid="landed-on-confirm"]'),
        loc: window.__loc,
    }));
    check(landed.landed === true, '点「确认下一项」落到确认页(深链真的通)');
    check(/\/defensive-geo\/publish\/decision\/snap-1/.test(String(landed.loc)),
        '落点带的是那一项自己的快照 id', String(landed.loc));

    const v = copyViolations(s.text);
    check(v.engineering.length === 0, '不含工程术语', v.engineering.join('/'));
    check(v.scary.length === 0, '不含恐吓词', v.scary.join('/'));
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// U-6:preview 过期 → 自动重建 → 价格没变突出提示
// ══════════════════════════════════════════════════════════════════════
section('U-6 · 过期自动重建 + 「价格没变，直接确认」');
{
    // ── 臂 A:两次价格相同 ⇒ 必须出现「价格没变，直接确认」 ──────────
    const { page, calls, pageErrors } = await openPage(browser, {
        confirmExpiresFirst: true, previewPoints: [1200, 1200],
    });
    await page.evaluate(() => window.__mountLaunch(document.getElementById('root')));
    await page.waitForSelector('button', { timeout: 10000 }).catch(() => {});
    await page.click('text=看看要花多少算力');
    await page.waitForFunction(() => /消耗你的算力/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    await page.click('text=确认并开始体检');
    await page.waitForSelector('[data-testid="preview-rebuilt"]', { timeout: 10000 })
        .catch(() => {});

    const a = await page.evaluate(() => ({
        rebuilt: document.querySelector('[data-testid="preview-rebuilt"]')?.getAttribute('data-price') || '',
        banner: document.querySelector('[data-testid="preview-rebuilt"]')?.innerText || '',
        text: document.body.innerText,
        hasAlert: !!document.querySelector('[role="alert"]'),
    }));
    check(calls.preview >= 2, '过期后**自动**重建了 preview(她没点任何东西)',
        `preview ${calls.preview} 次`);
    check(a.rebuilt === 'unchanged', '判定为「价格没变」', a.rebuilt || '(没渲染)');
    check(/价格没变，直接确认/.test(a.text), 'U-6 逐字那句突出提示真的在按钮上');
    check(/已经帮你重新算过/.test(a.banner), '说明里明说是系统帮她重算的(她不需要自己想起来)');
    check(a.hasAlert === false, '过期不上错误框(不是把她吓一跳再让她重来)');
    await page.close();

    // ── 臂 B:两次价格不同 ⇒ **不许**说"没变" ──────────────────────
    const b = await openPage(browser, { confirmExpiresFirst: true, previewPoints: [1200, 1500] });
    await b.page.evaluate(() => window.__mountLaunch(document.getElementById('root')));
    await b.page.waitForSelector('button', { timeout: 10000 }).catch(() => {});
    await b.page.click('text=看看要花多少算力');
    await b.page.waitForFunction(() => /消耗你的算力/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    await b.page.click('text=确认并开始体检');
    await b.page.waitForSelector('[data-testid="preview-rebuilt"]', { timeout: 10000 })
        .catch(() => {});
    const bs = await b.page.evaluate(() => ({
        rebuilt: document.querySelector('[data-testid="preview-rebuilt"]')?.getAttribute('data-price') || '',
        text: document.body.innerText,
    }));
    // 🔴 这一臂是「必须不命中」的那一半:没有它,把 priceUnchanged 改成 `return true`
    //    上面那条照样全绿。
    check(bs.rebuilt === 'changed', '价格变了时判定为 changed', bs.rebuilt || '(没渲染)');
    check(!/价格没变/.test(bs.text), '价格变了时**绝不**说"价格没变"');
    check(/价格有变化/.test(bs.text), '而是明说这次的价格有变化');
    check(/消耗你的算力\s*1500/.test(bs.text), '并且显示的是新价格');
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await b.page.close();
}

// ══════════════════════════════════════════════════════════════════════
// [工单 V5-A · Codex fix-of-fix2 P2-1] 恢复按钮的**点击级**闭环
//
// 被测缺陷逐字:后端给 `nextAction.kind = new_preview`,前端按钮却
// 确定性地重试同一次 confirm ——「即使改成 runPreview(),固定的 preview
// 幂等键又会重放旧 preview」。所以这一组要证的是**两件事一起成立**:
//   ① 点下去打的是 preview 端点,不是 confirm;
//   ② 那一次 preview 带的是一把**新**幂等键(否则后端 ON CONFLICT DO NOTHING
//      会把那条旧的原样回读,她永远出不来)。
// 只证①的话这个 finding 只修好了一半 —— 而修好的那一半看起来完全正常。
// ══════════════════════════════════════════════════════════════════════
section('V5-A · 价目被停用:恢复按钮打 preview + 换新幂等键');
{
    const { page, calls, pageErrors } = await openPage(browser, {
        confirmPolicyUnavailableAlways: true,
    });
    await page.evaluate(() => window.__mountLaunch(document.getElementById('root')));
    await page.waitForSelector('button', { timeout: 10000 }).catch(() => {});
    await page.click('text=看看要花多少算力');
    await page.waitForFunction(() => /消耗你的算力/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    await page.click('text=确认并开始体检');
    await page.waitForSelector('[data-testid="next-action-new-preview"]', { timeout: 10000 })
        .catch(() => {});

    const shown = await page.evaluate(() => ({
        hasButton: !!document.querySelector('[data-testid="next-action-new-preview"]'),
        label: document.querySelector('[data-testid="next-action-new-preview"]')?.innerText || '',
        text: document.body.innerText,
    }));
    // 判据活性:先证明这一屏真的走到了"给出恢复出口"那一态,
    // 否则下面"没有再 confirm"是因为**根本没按钮可点**,而不是因为修好了。
    check(shown.hasButton, '价目被停用后,屏幕上真的出现了恢复按钮(判据活性)');
    // 🔴 断言的是**真 registry** 里那几个字,不是我手写的字符串。
    check(shown.label.includes(COPY.newPreviewAction),
        '按钮上写的是 registry 里那句话', `${shown.label} vs ${COPY.newPreviewAction}`);
    check(/没有扣除任何算力/.test(shown.text), '涉钱失败显式说清钱没动');

    const beforeConfirm = calls.confirm;
    const beforePreview = calls.preview;
    // 🔴 点法刻意分两级,为的是让**红臂也跑得完**:
    //    改动前那一版渲染的是同一颗按钮、只是没有 testid,如果这里直接按 testid 点
    //    就会抛 TimeoutError 把整个 runner 打崩 —— 崩溃与"跑完全红"长得不一样,
    //    而且后面几条断言一条都不会跑。所以:有 testid 就点它,没有就按**按钮上那句话**找,
    //    让"点下去到底打了哪个端点"这条断言在两个世界里都真的被执行。
    //
    // 🔴 而且必须显式断言"**点到了**"。第一版只是 `.catch(() => {})` 吞掉点击失败,
    //    结果改动前那一轮里按钮压根没被点到,`calls.confirm` 当然没涨,
    //    于是「没有再 confirm」在**红臂里变成了绿的** —— 一条零分母恒绿,
    //    而它恰好长得像"这个缺陷不存在"。
    const btn = shown.hasButton
        ? page.locator('[data-testid="next-action-new-preview"]')
        : page.locator('button', { hasText: COPY.newPreviewAction });
    const btnCount = await btn.count();
    let clicked = false;
    try {
        await btn.first().click({ timeout: 5000 });
        clicked = true;
    } catch { /* 由下面那条断言报出来,不静默 */ }
    await page.waitForTimeout(800);

    check(btnCount >= 1, '错误框里确实有一颗可点的恢复按钮(判据活性)', `count=${btnCount}`);
    check(clicked, '那颗恢复按钮**真的被点到了** —— 否则下面几条全是零分母恒绿');

    check(calls.confirm === beforeConfirm,
        '点恢复按钮**没有**再打一次 confirm(改动前它确定性地重放同一个 503)',
        `confirm ${beforeConfirm} -> ${calls.confirm}`);
    check(calls.preview === beforePreview + 1,
        '点下去打的是 preview 端点', `preview ${beforePreview} -> ${calls.preview}`);
    const [pk0, pk1] = calls.previewKeys;
    check(!!pk0 && !!pk1, '两次 preview 都带了 Idempotency-Key(判据活性)',
        `${pk0 || '(空)'} / ${pk1 || '(空)'}`);
    // 🔴 三个条件写在一起,不拆成"先有再不等":拆开的话第二次请求**根本没发出去**时
    //    `pk1 === undefined`,`pk0 !== pk1` 会平白为真 —— 在红臂里冒充一条绿的。
    check(!!pk0 && !!pk1 && pk0 !== pk1,
        '「用户重新发起」换了一把**新**幂等键 —— 沿用旧键会让后端回读那条旧 preview',
        `${pk0} vs ${pk1}`);
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// 配对的必须不命中:**网络重试**必须保持同一把键。
// 只有上面那条的话,把 runPreview 改成"每次都现算一把新键"也会全绿 ——
// 而那样同一次逻辑预览在后端会变成两条命令,「她看到的那一版」有了分身。
// ══════════════════════════════════════════════════════════════════════
section('V5-A · 同一次逻辑预览的网络重试:键必须不变');
{
    const { page, calls, pageErrors } = await openPage(browser, {
        // 第 0 次 preview 打回一个**没有 new_preview 出口**的瞬时故障:
        // 屏幕退回初始态,她再点一次那颗主按钮 = 同一次逻辑预览的重试。
        previewFails: [{
            code: 'UPSTREAM_BUSY', retryable: true,
            publicExplanation: '这一步这次没能算出来，请再试一次；没有扣除任何算力。',
            nextAction: { kind: 'wait', label: '稍等一下再试', actionRef: 'r', target: {} },
        }],
    });
    await page.evaluate(() => window.__mountLaunch(document.getElementById('root')));
    await page.waitForSelector('button', { timeout: 10000 }).catch(() => {});
    await page.click('text=看看要花多少算力');
    await page.waitForFunction(() => /请再试一次/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});

    const s = await page.evaluate(() => ({
        text: document.body.innerText,
        // 这一 kind 这一屏做不了 ⇒ 只出文字,不给一颗点下去会做错事的按钮
        hasNewPreviewBtn: !!document.querySelector('[data-testid="next-action-new-preview"]'),
        hasRetryBtn: !!document.querySelector('[data-testid="next-action-retry"]'),
        hasText: !!document.querySelector('[data-testid="next-action-text"]'),
    }));
    check(/请再试一次/.test(s.text), '瞬时故障走到了错误态(判据活性)');
    check(s.hasText && !s.hasNewPreviewBtn && !s.hasRetryBtn,
        'kind=wait 这一屏做不了 ⇒ 只显示那句话,不给一颗确定性做错事的按钮');

    await page.click('text=看看要花多少算力', { timeout: 5000 }).catch(() => {});
    await page.waitForFunction(() => /消耗你的算力/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    const [rk0, rk1] = calls.previewKeys;
    check(calls.preview === 2, '真的重试了第二次 preview(判据活性)', `preview=${calls.preview}`);
    check(!!rk0 && rk0 === rk1,
        '**网络重试**用的是同一把键 —— 换键 = 同一次预览在后端变成两条命令',
        `${rk0} vs ${rk1}`);
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// [工单 V5-B · Codex fix-of-fix3 P2-NEW-5 ②] 有真实落点的动作要真的跳过去
//
// Codex 原话:「余额不足返回 top_up/wallet target,但 LaunchPanel 只对
// new_preview、retry 画可点击按钮,top_up/request_approval/contact_support
// 等只显示文字」。站内**真的有** /wallet —— 只显示"去充值算力"五个字,
// 等于把她推回去自己找路。
// ══════════════════════════════════════════════════════════════════════
section('V5-B · 余额不足:按钮真的把她送到钱包页');
{
    const { page, pageErrors } = await openPage(browser, { confirmInsufficientAlways: true });
    await page.evaluate(() => window.__mountLaunch(document.getElementById('root')));
    await page.waitForSelector('button', { timeout: 10000 }).catch(() => {});
    await page.click('text=看看要花多少算力');
    await page.waitForFunction(() => /消耗你的算力/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    await page.click('text=确认并开始体检');
    await page.waitForSelector('[data-testid="next-action-navigate"]', { timeout: 10000 })
        .catch(() => {});

    const s = await page.evaluate(() => ({
        hasBtn: !!document.querySelector('[data-testid="next-action-navigate"]'),
        href: document.querySelector('[data-testid="next-action-navigate"]')?.getAttribute('data-href') || '',
        label: document.querySelector('[data-testid="next-action-navigate"]')?.innerText || '',
        text: document.body.innerText,
        locBefore: window.__loc,
    }));
    check(s.hasBtn, '余额不足时给的是一颗**可点**的按钮,不是一行字(判据活性)');
    check(s.label.includes(COPY.topUpAction), '按钮上写的是 registry 里那句话',
        `${s.label} vs ${COPY.topUpAction}`);
    check(s.href === '/wallet', '落点是站内真实存在的钱包页', s.href || '(空)');
    check(s.locBefore !== '/wallet', '点之前不在钱包页(否则下面那条恒真)', String(s.locBefore));

    await page.click('[data-testid="next-action-navigate"]', { timeout: 5000 }).catch(() => {});
    await page.waitForFunction(() => window.__loc === '/wallet', null, { timeout: 5000 })
        .catch(() => {});
    const after = await page.evaluate(() => window.__loc);
    check(after === '/wallet', '点下去**真的**导航到了 /wallet(不是只画了颗按钮)', String(after));
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// 配对的必须不命中:**没有站内落点**的 kind 不许画按钮。
// 没有这一条,把 KIND_ROUTES 写成"什么都接"也会让上面那组全绿 ——
// 而那样 request_approval 会得到一颗点了 404 的按钮。
// ══════════════════════════════════════════════════════════════════════
section('V5-B · 没有落点的动作:只出文字,不给按钮');
{
    const { page, calls, pageErrors } = await openPage(browser,
        { confirmNoDestinationAlways: true });
    await page.evaluate(() => window.__mountLaunch(document.getElementById('root')));
    await page.waitForSelector('button', { timeout: 10000 }).catch(() => {});
    await page.click('text=看看要花多少算力');
    await page.waitForFunction(() => /消耗你的算力/.test(document.body.innerText), null,
        { timeout: 10000 }).catch(() => {});
    await page.click('text=确认并开始体检');
    await page.waitForSelector('[data-testid="next-action-text"]', { timeout: 10000 })
        .catch(() => {});

    const s = await page.evaluate(() => ({
        hasText: !!document.querySelector('[data-testid="next-action-text"]'),
        hasNav: !!document.querySelector('[data-testid="next-action-navigate"]'),
        hasNewPreview: !!document.querySelector('[data-testid="next-action-new-preview"]'),
        text: document.body.innerText,
    }));
    check(s.hasText, '那句话原样显示出来了 —— 她仍然知道下一步是什么(判据活性)');
    check(!s.hasNav && !s.hasNewPreview, 'kind=wait 没有站内落点 ⇒ 一颗按钮都不给');
    check(s.text.includes(COPY.policyUnavailable),
        '屏幕上那句话来自 registry,不是前端编的');

    // ── [P3-NEW-1] 同一屏里再点一次主按钮 = 一次真实的 confirm 重试 ──
    //
    // Codex 原话:「把 confirm 调用改为每次 newIdempotencyKey(),静态闸与真
    // Chromium 仍 rc=0」。原因是上一版根本没在浏览器里重试过 confirm ——
    // 断言的是 state 声明,不是**真发出去的那两个请求头**。
    const before = calls.confirm;
    await page.click('text=确认并开始体检', { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(800);
    const [ck0, ck1] = calls.confirmKeys;
    check(calls.confirm === before + 1, '真的重试了第二次 confirm(判据活性)',
        `confirm ${before} -> ${calls.confirm}`);
    check(!!ck0 && !!ck1, '两次 confirm 都带了 Idempotency-Key(判据活性)',
        `${ck0 || '(空)'} / ${ck1 || '(空)'}`);
    check(!!ck0 && !!ck1 && ck0 === ck1,
        '两次重试用的是**同一把**键 —— 换键就是把一次确认变成两条命令',
        `${ck0} vs ${ck1}`);
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// [门八第二/第三发现] 付完钱之后落在哪一页 · 落在那一页之后看到什么
//
// 门四那条 G8 判据打的是 API(`authorize_session(user, sid)` 直接调),
// 证明的是"后端这把键对得上",证明不了"前端把哪把键放进了 URL"。
// 于是 `NewDiagnosis.tsx` 那句 navigate 用 runId 一直没人看见:
// confirm 200(算力真冻结、run 真在跑),她被送到一个 403 的进度页。
//
// 所以这两组从**填品牌名**开始点到底:真 NewDiagnosis → 真 LaunchPanel →
// 真 confirm → 真 navigate → 真 DiagnosisProgress → 它自己发的 /status。
// ══════════════════════════════════════════════════════════════════════

/**
 * 从填品牌名一路点到 confirm。返回一路上的**活性事实**,由调用方逐条断言。
 *
 * 🔴 抽出来共用不是为了好看:两组都要"真的点到底"。各写一份的话,
 *    改了一处忘了另一处,而漂掉的那一组会静默退化成"根本没点到",
 *    然后它的断言全部变成零分母恒绿。
 */
async function driveToConfirm(page) {
    const out = { brandVisible: false, enabled: false, priced: false, confirmable: false };
    await page.evaluate(() => window.__mountNewDiagnosis(document.getElementById('root')));

    // ① 品牌:填名字,让页面自己按名字匹配到那条品牌(真实路径,不是我塞 state)
    const brandInput = page.locator('#brandName');
    try { await brandInput.waitFor({ state: 'visible', timeout: 15000 }); out.brandVisible = true; }
    catch { return out; }
    await brandInput.fill(GATE8_BRAND.name);

    // ② 按钮从 disabled 变 enabled = 页面真的匹配上了品牌且题单非空(defensiveLaunchGate)
    const submit = page.locator('[data-testid="launch-submit"]');
    try { await submit.waitFor({ state: 'visible', timeout: 10000 }); } catch { return out; }
    for (let i = 0; i < 40 && !out.enabled; i++) {
        out.enabled = await submit.isEnabled().catch(() => false);
        if (!out.enabled) await page.waitForTimeout(150);
    }
    if (!out.enabled) return out;

    // ③ 提交 → 题单预览 → LaunchPanel 拿到 plan
    await submit.click({ timeout: 5000 }).catch(() => {});
    const priceBtn = page.locator('text=看看要花多少');
    try { await priceBtn.waitFor({ state: 'visible', timeout: 15000 }); out.priced = true; }
    catch { return out; }

    // ④ 算价 → 确认
    await priceBtn.click({ timeout: 5000 }).catch(() => {});
    const confirmBtn = page.locator('text=确认并开始体检');
    try { await confirmBtn.waitFor({ state: 'visible', timeout: 15000 }); out.confirmable = true; }
    catch { return out; }
    await confirmBtn.click({ timeout: 5000 }).catch(() => {});
    return out;
}

/** 一路上那几条活性断言 —— 少任何一条,后面的判别断言都是零分母。 */
function checkDriveAlive(d, calls) {
    check(d.brandVisible, '发起页真的渲染出来了(判据活性)');
    check(d.enabled, '「开始体检」按钮可点了 —— 品牌匹配上 + 题单非空(判据活性)');
    check(calls.questionPlans >= 1, '真的去要了一份题单(判据活性)',
          `question-plans ${calls.questionPlans}`);
    check(d.priced, '启动面板拿到题单并给出了算价入口(判据活性)');
    check(calls.preview >= 1, '真的算了价(判据活性)', `preview ${calls.preview}`);
    check(d.confirmable, '算完价出现了确认按钮(判据活性)');
}

/** 等到 /status 至少被打了 n 发(跳转 + 落地页自己那一发都要时间)。 */
async function waitStatusCalls(page, calls, n, ticks = 80) {
    for (let i = 0; i < ticks; i++) {
        if (calls.statusIds.length >= n) return true;
        await page.waitForTimeout(150);
    }
    return false;
}

section('门八第二发现 · confirm 之后落在能看的进度页(真 NewDiagnosis 点到底)');
{
    const { page, calls, pageErrors } = await openPage(browser, { search: '?goal=defensive' });
    await settleNetwork(page, calls);
    const d = await driveToConfirm(page);
    checkDriveAlive(d, calls);
    if (d.confirmable) await waitStatusCalls(page, calls, 1);

    const loc = await page.evaluate(() => window.__loc || '');
    check(calls.confirm >= 1, '真的确认了(判据活性)', `confirm ${calls.confirm}`);

    // ── 本组的判别信号:URL 里那把键 ──────────────────────────────
    check(loc.startsWith('/diagnosis/progress/'),
          '确认之后**真的**跳到了进度页', loc || '(没跳)');
    const idInUrl = loc.startsWith('/diagnosis/progress/')
        ? loc.slice('/diagnosis/progress/'.length).split(/[?#]/)[0] : '';
    check(idInUrl === PROGRESS_SID,
          'URL 里那把键是 progressSessionId —— 进度端点/WS 认的就是它',
          `${idInUrl || '(空)'} vs ${PROGRESS_SID}`);
    // 配对的必须不命中:钉住**具体错在哪**。只断"等于对的那把"的话,
    // 哪天两把键碰巧长得一样(比如后端不再加前缀),这条会假绿。
    check(!!idInUrl && idInUrl !== RUN_TOKEN,
          '不是 runId —— 那是 run_token,归属查询一条都命中不到,fail-closed 403',
          `runId=${RUN_TOKEN}`);

    // ── 落地页自己发出去的第一发 /status ─────────────────────────
    check(calls.statusIds.length >= 1,
          '进度页真的去查了一次状态(判据活性:没有这一发,下面两条是零分母恒绿)',
          `status 请求 ${calls.statusIds.length} 次`);
    check(calls.statusCodes[0] === 200,
          '她看到的第一发进度请求是 200,不是 403',
          `${calls.statusCodes[0]} · id=${calls.statusIds[0] || '(无)'}`);
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

// ══════════════════════════════════════════════════════════════════════
// [门八第三发现] confirm 成功 → 执行器还没领 → /status 回 found:false
//
// 这一段窗口(生产 cron 20s 一轮)里,后端此前什么都不写、前端第一次拿到
// found:false 就挂红色横幅「未找到此诊断任务…请返回重新发起」并**永久停轮询**。
// 她刚付过 7800,而"重新发起"意味着**再冻一笔** —— 资金相邻的误导。
//
// 这一组把那段窗口原样造出来:前 3 发 /status 回 found:false,之后回真进度。
// ══════════════════════════════════════════════════════════════════════
section('门八第三发现 · 还没被执行器领走的那一段:不许说"未找到,请返回重新发起"');
{
    const { page, calls, pageErrors } = await openPage(browser, {
        search: '?goal=defensive',
        statusNotFound: 3,
    });
    await settleNetwork(page, calls);
    const d = await driveToConfirm(page);
    checkDriveAlive(d, calls);

    // 窗口期:等到那 3 发 found:false 都真的发生
    const probed = await waitStatusCalls(page, calls, 3, 120);
    const falses = calls.statusFound.filter((f) => f === false).length;
    check(probed && falses >= 2,
          '窗口期里前端**反复重探**了(判据活性:第一次 false 之后没有停手)',
          `status ${calls.statusIds.length} 发 · found:false ${falses} 发`);

    const midText = await page.evaluate(() => document.body.innerText);
    check(!midText.includes(NOT_FOUND_BANNER),
          '窗口期里屏幕上**没有**「未找到…请返回重新发起」—— 她刚付过钱,那句话会把她推去再冻一笔',
          NOT_FOUND_BANNER);

    // 窗口结束:mock 开始回真进度
    let sawFound = false;
    for (let i = 0; i < 120; i++) {
        if (calls.statusFound.includes(true)) { sawFound = true; break; }
        await page.waitForTimeout(150);
    }
    check(sawFound, '轮询自己熬到了 found:true(判据活性:退避没被永久停掉)',
          `found 序列 ${calls.statusFound.join(',') || '(空)'}`);

    let endText = '';
    for (let i = 0; i < 40; i++) {
        endText = await page.evaluate(() => document.body.innerText);
        if (endText.includes(GATE8_RUNNING_LINE)) break;
        await page.waitForTimeout(150);
    }
    check(endText.includes(GATE8_RUNNING_LINE),
          '进度真的上屏了 —— 她看到的是"正在跑",不是空屏', GATE8_RUNNING_LINE);
    check(!endText.includes(NOT_FOUND_BANNER),
          '进度到了之后横幅也不在(配对的必须不命中:横幅粘死是原缺陷的后半段)');
    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await page.close();
}

server.close();
await browser.close();

console.log('\n================================================================');
console.log(failed === 0
    ? '\u2705 包H U-10 浏览器级判据通过(U-3 / U-4 / U-5 / U-6 各一条正样本 + V5-A 恢复按钮两组 + V5-B 落点/同键两组 + 门八进度页落点一组)'
    : `\u{1F534} 包H U-10 判据未通过 \u00b7 ${failed} 条红`);
process.exit(failed === 0 ? 0 : 1);
