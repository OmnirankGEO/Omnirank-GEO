#!/usr/bin/env node
/**
 * 发起页「点了之后她看得见吗」—— 真 chromium · 两档视口。
 *
 * ## 被测缺陷(2026-09-01 生产 P0)
 *
 * Owner 安卓手机点「开始体检」:9 秒 **14 次** `question-plans/preview` **全 200**,
 * 零 `run-previews`、零 `confirm`。形状 =「点得动、请求成功、**界面没推进**」。
 *
 * 机制:栅格(`NewDiagnosis.tsx` 的 `launch-grid`)在容器 <880px 是**单列** ⇒
 * 右栏 `launch-aside`(含 CTA)沉到主列**下方**,而 `LaunchPanel` 在主列里 ——
 * 手机上按钮在页尾、面板在页首。`setQuestionPlan` 之后**零视口处理**,
 * 她眼前什么都没变,于是再点。桌面双栏并排,所以同一份代码桌面几乎看不出来。
 * 实测(修前):390×844 下算价按钮 `y = -43`,在视口**上方之外**。
 *
 * ## 为什么不能用 `toBeVisible()`
 *
 * Playwright 的 visible 对「在 DOM 里、只是滚出屏幕」的元素**也通过** ——
 * 那正是修前的状态。用它的话这条判据从第一天起就没有判别力。
 * 所以判**视口相交**:`boundingBox()` 与 `viewportSize().height` 比。
 *
 * ## 为什么必须用设备描述符而不是只设 viewport
 *
 * 单列档要的是 Owner 那台机器的条件:`hasTouch` ⇒ `pointer: coarse`。
 * 只设 viewport 的话 `pointer-fine` 那条 sticky 会生效,测的就不是他那一格。
 *
 * 🔴 **本脚本刻意不进 `npm run build` 链**:build 链会在 `node:20-alpine` 的
 *    frontend-builder 里跑,那儿没有 chromium。同族教训见 2026-08-30 门八第一发现
 *    ——一条静态锁把整锅 docker build 卡死,五轮复审都没看见。
 *    它接在 `npm run verify:diagnosis-launch` 上。
 *
 * 跑法:cd frontend && node scripts/test-diagnosis-launch-viewport.mjs
 * 退出码:0=全绿 1=有红/判据不可用
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync , mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { join, extname } from 'node:path';
import { createRequire } from 'node:module';

const ROOT = process.cwd();
const require_ = createRequire(join(ROOT, 'package.json'));

let failed = 0;
const ok = (m, d = '') => console.log(`  \u2705 ${m}${d ? ` \u2014 ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  \u{1F534} ${m}${d ? ` \u2014 ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));

function unusable(why, detail) {
    console.log(`\u{1F534} 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 2000));
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
/*
 * 🔴 [2026-09-18] `node_modules/.cache` 是**构建产物**,全新 `npm ci` 的树里不存在。
 *    原来直接 mkdtemp 进去 ⇒ 在干净树上 ENOENT 崩成 **rc=1**(读作"有判据红了"),
 *    而真相是**门根本没跑起来**。我本机有这个目录,所以恒绿 ——
 *    与「浏览器门进 build 链」同形:这条臂只在它被写出来的那个环境里跑得动。
 * 🔴 只加 mkdir 是修一半:建目录这一步本身也要**包进三态**,
 *    否则任何发生在三态机制之外的失败都会伪装成"判据红了"。
 */
let outDir;
try {
    mkdirSync(CACHE_ROOT, { recursive: true });
    outDir = mkdtempSync(join(CACHE_ROOT, 'diagnosis-launch-viewport-'));
} catch (err) { unusable('临时目录建不出来', String((err && err.message) || err)); }
/* 🔴 a179-cleanup:跑完删掉这一份,免得 .cache 里堆满 bundle
   (一次全量注毒 = 17 发 × 两把闸 = 34 份)。进程怎么退出都删。 */
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });
const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

// 真 NewDiagnosis + 真 Provider 栈(照 App.tsx 的嵌套顺序,不自己编一套)
writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { AuthProvider } from ${q('src/context/AuthContext.tsx')};
import { OrganizationProvider } from ${q('src/context/OrganizationContext.tsx')};
import { UserModeProvider } from ${q('src/context/UserModeContext.tsx')};
import { WalletProvider } from ${q('src/context/WalletContext.tsx')};
import { OnboardingProvider } from ${q('src/context/OnboardingContext.tsx')};
import { PricingProvider } from ${q('src/context/PricingContext.tsx')};
import { ClientProvider } from ${q('src/context/ClientContext.tsx')};
import { NewDiagnosis } from ${q('src/pages/Diagnosis/NewDiagnosis.tsx')};

(globalThis as any).__mount = function (el: HTMLElement) {
    createRoot(el).render(
        <MemoryRouter initialEntries={['/diagnosis/new']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <Routes><Route path="/diagnosis/new" element={<NewDiagnosis />} /></Routes>
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
} catch (err) { unusable('打包真组件失败', err); }

writeFileSync(join(outDir, 'bundle.html'),
    '<!doctype html><html lang="zh"><head><meta charset="utf-8">'
    + '<meta name="viewport" content="width=device-width, initial-scale=1">'
    + '<title>launch viewport</title></head>'
    + '<body><div id="root"></div><script src="./bundle.js"></script></body></html>', 'utf8');

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };
const server = createServer((req, res) => {
    const name = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'bundle.html';
    try {
        const buf = readFileSync(join(outDir, name));
        res.writeHead(200, { 'Content-Type': MIME[extname(name)] || 'application/octet-stream' });
        res.end(buf);
    } catch { res.writeHead(404); res.end('nope'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

const AUTH_USER = {
    id: 112, username: 'launch-agent', role: 'user', agent_level: 1,
    is_admin: false, email: 'launch@local', permissions: [], user_mode: 'agent',
};
const BRAND = { id: 901, name: '视口判据品牌', industry: '教育培训' };
const PLAN = {
    planId: 'plan-viewport', planRevision: 1, canonicalHash: 'h', canonicalHashVersion: 'v1',
    brandId: BRAND.id, profileRevisionId: `brand-${BRAND.id}`, mode: 'defensive',
    modeUserLabel: '先守住品牌', questions: [],
    counts: { defensive: 3, offensive: 0, total: 3 },
    expiresAt: '2099-01-01T00:00:00Z', copyRegistryVersion: 'v', idempotentReplay: false,
};

let browser;
try { browser = await playwright.chromium.launch(); }
catch (err) { server.close(); unusable('Chromium 起不来', err); }

const { devices } = playwright;

/** 一档视口跑一遍:填表 → 点 CTA → preview 200 → 面板必须进视口。 */
async function arm(label, contextOptions) {
    console.log(`\n=== ${label} ===`);
    const ctx = await browser.newContext(contextOptions);
    const page = await ctx.newPage();
    const previews = [];
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    await page.addInitScript(() => localStorage.setItem('omnirank_token', 'launch-token'));
    await page.route('**/api/**', async (route) => {
        const url = route.request().url();
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        if (/\/api\/auth\/me/.test(url)) return json({ success: true, user: AUTH_USER });
        // 价目要**真形状** `{data:[...]}`:回错形状会让 for..of 抛,四格按钮一起变
        // 「价目不可用」——那是夹具造成的假红(2026-09-01 实测踩过)。
        if (/\/api\/wallet\/pricing/.test(url)) {
            return json({ data: [{ feature_code: 'geo_diagnosis', is_active: true, cost_points: 100 }] });
        }
        // 🔴 [#125] 补 `/api/pricing/diagnosis-preview` 的 mock。
        //    ⑤a 之后 CTA 由 `priceNotReady` 把门 —— 拿不到价就一直是灰的。
        //    这个 mock 缺席时,「CTA 可点」恒红,而脚本在那儿 **提前 return**,
        //    下游整整一半判据(面板进视口 / 算价按钮位置)**从此一次都没跑过**。
        //    我 ⑤a 那笔让这条判据的前提失效了,而它早就红着,所以没人发现 ——
        //    **一条已经红的判据,再红一格是看不出来的。**
        if (/\/api\/pricing\/diagnosis-preview/.test(url)) {
            return json({ questionCount: 0, mode: 'defensive', points: 650, estimate: 650,
                pricePreviewId: 'pp_fixture', breakdown: { base: 650, extra: 0, freeQuestions: 8, perExtraQuestion: 100 } });
        }
        if (/\/api\/diagnosis\/keyword-suggestion/.test(url)) {
            return json({ keywords: ['夹具词'], source: 'brand_name', aiAugmented: false });
        }
        if (/question-plans\/preview/.test(url)) { previews.push(1); return json(PLAN); }
        if (/\/api\/my-clients/.test(url)) return json({ clients: [BRAND] });
        return json({ success: true, items: [], data: [] });
    });
    await page.goto(`http://127.0.0.1:${PORT}/bundle.html?goal=defensive`, { waitUntil: 'load' });
    await page.waitForTimeout(700);
    await page.evaluate(() => window.__mount(document.getElementById('root')));

    const brand = page.locator('#brandName');
    let rendered = false;
    try { await brand.waitFor({ state: 'visible', timeout: 20000 }); rendered = true; } catch { /* 下面报 */ }
    check(rendered, '发起页真的渲染出来了(判据活性)');
    if (!rendered) { await ctx.close(); return; }

    await brand.fill(BRAND.name);
    await page.locator('#industry').fill('教育培训').catch(() => {});
    await page.waitForTimeout(1000);

    const submit = page.locator('[data-testid="launch-submit"]');
    // ── 臂 A：**首屏**（#125）· fresh load，量之前**一次都不许滚** ──
    /**
     * 🔴 这一臂是补上来的。原来这里只有一句 scrollIntoViewIfNeeded()，
     *    然后下面才量 boundingBox —— 于是量的是「滚过去之后在不在视口里」，
     *    那对任何能渲染的元素都近乎恒成立，而**「fresh load 时在不在首屏」一个字都没测**。
     *    更糟的是下面那段注释声称「与 Deploy 生产验收同口径」——
     *    Deploy 量的是不滚动的 fresh load，两者根本不是同一个量。
     *    **判据的措辞越像在证明覆盖，越容易掩盖它没覆盖的那一格。**
     *
     * 🔴 两臂**不共用一次页面操作**：A 臂量完首屏才允许 B 臂去滚。
     * 🔴 滚动容器按 <main> 取（Deploy 口径）。
     */
    /**
     * 🔴 **先证这台仪器测得了布局**:本 harness 用 esbuild 直接打包页面,
     *    **不加载任何 CSS**(全文件搜 tailwind/style 零命中)。没有样式表时,
     *    窄档 order 变体(order-1…order-6,带 @max- 前缀) / `@container` 全部不生效,量到的是**无样式文档流**的高度。
     *    那种情况下 A1 无论修没修都会红 —— **零区分力的红,比不测更坏**,
     *    因为它看起来像证据。所以先探一格:CTA 身上的 `min-h-[48px]` 有没有真的生效。
     *    没生效 ⇒ A1/A2 报**未评估**,交 Deploy 在生产上量。
     */
    const cssLive = await submit.evaluate((el) => {
        const mh = getComputedStyle(el).minHeight;
        return mh && mh !== '0px' && mh !== 'auto';
    }).catch(() => false);
    const vpA = page.viewportSize();
    const ctaBoxFresh = cssLive ? await submit.boundingBox() : null;
    if (!cssLive) {
        console.log('    ⚠️ A0/A1/A2 **未评估**:本 harness 不加载 CSS,布局量不了。');
        console.log('       (探针:CTA 的 min-h-[48px] 未生效 ⇒ 样式表没进来)');
        console.log('       🔴 这不是通过 —— 首屏读数必须由 Deploy 在生产 355×767 上量。');
    }
    if (cssLive) check(!!ctaBoxFresh, 'A0 CTA 拿得到 boundingBox（判据活性）');
    if (cssLive && ctaBoxFresh) {
        const bottom = ctaBoxFresh.y + ctaBoxFresh.height;
        check(bottom <= vpA.height,
              'A1 🔴 **fresh load 不滚动时 CTA 整颗在首屏内**（#125；修前 355×767 实测 top=2127）',
              `top=${Math.round(ctaBoxFresh.y)} bottom=${Math.round(bottom)} viewportH=${vpA.height}`);
    }
    const mainScroll = cssLive ? await page.evaluate(() => {
        const m = document.querySelector('main');
        return m ? { scrollTop: m.scrollTop, scrollHeight: m.scrollHeight } : null;
    }) : null;
    if (cssLive) check(!!mainScroll && mainScroll.scrollTop === 0,
          'A2 🔴 量 A1 时**确实没滚过**（scrollTop=0）—— 否则 A1 又变回“滚后再量”',
          JSON.stringify(mainScroll));

    // ── 臂 B：可点性 · 这一臂**允许**滚动 ──
    await submit.scrollIntoViewIfNeeded().catch(() => {});
    const enabled = await submit.isEnabled().catch(() => false);
    check(enabled, 'CTA 可点(判据活性:不可点的话下面全是零分母)');
    if (!enabled) { await ctx.close(); return; }

    await submit.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(2000);
    check(previews.length >= 1, '真的发出了 question-plans/preview(判据活性)',
          `preview ${previews.length} 次`);

    // 🔴 判视口相交,不用 toBeVisible():后者对"在 DOM 里但滚出屏幕"的元素也通过,
    //    而那正是修前的状态。
    const panel = page.locator('[data-testid="launch-panel"]');
    const panelCount = await panel.count();
    check(panelCount === 1, '页面上恰有一个 launch-panel(判据活性:0 或 2 都说明选择器漂了)',
          `count=${panelCount}`);
    let box = null;
    if (panelCount === 1) box = await panel.boundingBox();
    const vh = page.viewportSize().height;
    check(!!box, '面板拿得到 boundingBox');
    /**
     * 🔴 [#125] 下面三条声称的都是**视口位置**,而视口位置需要 CSS。
     *    本 harness 不加载样式表(同 A 臂的探针),它们量的是无样式文档流 ——
     *    历史上它们一直是绿的,而那个绿**没有意义**:写它们是为了抓 y=-43 / top=-396,
     *    而无样式文档里根本不会出现负值,所以它们**结构上抓不到自己要抓的东西**。
     *    统一纳入 cssLive 门:测不了就报未评估,别再发无意义的绿。
     */
    if (!cssLive) {
        console.log('    ⚠️ 面板/算价按钮/即时反馈的**视口位置**三条 **未评估**(同上:无 CSS)。');
    }
    if (cssLive && box) {
        const intersects = box.y < vh && box.y + box.height > 0;
        check(intersects,
              'preview 成功后面板**进入视口**(修前 390×844 实测 y=-43,在屏幕上方之外)',
              `y=${Math.round(box.y)} h=${Math.round(box.height)} viewportH=${vh}`);
    }

    // 🔴 [#125 订正] 这段原来写着「与 Deploy 生产验收**同口径**……说的是同一件事」。
    //    **那句话是错的**:上面有 scrollIntoViewIfNeeded,这里量的是**滚后**状态,
    //    而 Deploy 量的是 fresh load 不滚动。真正同口径的是上面新加的**臂 A**。
    //    这一条保留,但它的含义降级为「滚过去之后这颗按钮不越界」——
    //    仍有用(能抓到元素比视口还高之类),但**它不是首屏判据**。
    // 🔴 原注释:与 Deploy 在生产上的验收口径对齐:他们量的是「看看要花多少算力」那颗按钮,
    //    修复前 Pixel 5 仿真实测 top = **-396**(视口上方之外)。
    //    这里用**同一个元素、同一条判定**(top ≥ 0 且 bottom ≤ 视口高),
    //    这样"我这边绿"和"他那边 verified 绿"说的是同一件事;
    //    只判相交的话会比生产验收松,两边可能得出不同结论。
    const priceBtn = page.locator('text=看看要花多少');
    const priceCount = await priceBtn.count();
    check(priceCount >= 1, '算价入口在 DOM 里(判据活性)', `count=${priceCount}`);
    if (cssLive && priceCount) {
        const pb = await priceBtn.first().boundingBox();
        check(!!pb && pb.y >= 0 && pb.y + pb.height <= vh,
              '算价按钮**整颗**在视口内(生产验收同口径:修前 Pixel 5 top=-396)',
              pb ? `top=${Math.round(pb.y)} bottom=${Math.round(pb.y + pb.height)} viewportH=${vh}` : 'null');
    }

    // 静默成功等于没成功
    const feedback = page.locator('[data-testid="launch-feedback"]');
    const fbBox = (cssLive && await feedback.count()) ? await feedback.boundingBox() : null;
    if (cssLive) check(!!fbBox && fbBox.y < vh && fbBox.y + fbBox.height > 0,
          '即时反馈 launch-feedback 也在视口内',
          fbBox ? `y=${Math.round(fbBox.y)}` : '(不在 DOM 里)');

    check(pageErrors.length === 0, '无未捕获异常', pageErrors[0] || '');
    await ctx.close();
}

/**
 * ③ 侧栏与按钮两个谓词**绑一条**。
 *
 * Owner 看到的矛盾是「侧栏『启动检查』说品牌名称已填、按钮却灰」。
 * `resolveLaunchStep`(侧栏)与 `isLaunchBlocked`(按钮)是两个谓词、各说一半,
 * 而只有前者有锁 —— 所以矛盾能静默存在。这条把它们绑起来:
 * **灰按钮必须自带一句人话**,否则用户只能瞎猜。
 */
async function blockedReasonArm() {
    console.log('\n=== 灰按钮必须自带人话(侧栏-按钮绑定) ===');
    const ctx = await browser.newContext({ ...devices['Pixel 5'] });
    const page = await ctx.newPage();
    await page.addInitScript(() => localStorage.setItem('omnirank_token', 'launch-token'));
    await page.route('**/api/**', async (route) => {
        const url = route.request().url();
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        if (/\/api\/auth\/me/.test(url)) return json({ success: true, user: AUTH_USER });
        if (/\/api\/wallet\/pricing/.test(url)) {
            return json({ data: [{ feature_code: 'geo_diagnosis', is_active: true, cost_points: 100 }] });
        }
        if (/\/api\/my-clients/.test(url)) return json({ clients: [BRAND] });
        return json({ success: true, items: [], data: [] });
    });
    // offensive:只填品牌名 ⇒ 侧栏会说「品牌名称 已填」,但 isFormComplete 还差行业/问题
    // ⇒ 按钮灰。这一格必须有人话说明差什么。
    await page.goto(`http://127.0.0.1:${PORT}/bundle.html?goal=offensive`, { waitUntil: 'load' });
    await page.waitForTimeout(700);
    await page.evaluate(() => window.__mount(document.getElementById('root')));
    const brand = page.locator('#brandName');
    let rendered = false;
    try { await brand.waitFor({ state: 'visible', timeout: 20000 }); rendered = true; } catch { /* 下面报 */ }
    check(rendered, '发起页渲染出来了(判据活性)');
    if (!rendered) { await ctx.close(); return; }
    await brand.fill(BRAND.name);
    await page.waitForTimeout(900);

    const submit = page.locator('[data-testid="launch-submit"]');
    const disabled = await submit.isDisabled().catch(() => null);
    check(disabled === true, '这一格按钮确实是灰的(判据活性:不灰的话下面这条是零分母)',
          `disabled=${disabled}`);
    const text = await page.evaluate(() => document.body.innerText);
    check(text.includes('已填'), '侧栏确实说了「已填」(矛盾的另一半必须真的在)');
    if (disabled === true) {
        const reason = page.locator('[data-testid="launch-blocked-reason"]');
        const n = await reason.count();
        const t = n ? (await reason.first().innerText()).trim() : '';
        check(n >= 1 && t.length > 0,
              '灰按钮旁边有一句人话说明为什么点不了', t || '(零文案)');
    }
    await ctx.close();
}

// 🔴 单列档用**设备描述符**(hasTouch ⇒ pointer: coarse):只设 viewport 的话
//    `pointer-fine` 那条 sticky 会生效,测的就不是 Owner 那台安卓机。
await arm('Pixel 5(触屏 · 单列档)', { ...devices['Pixel 5'] });
await arm('桌面 1280×900(两栏档 · 对照)', { viewport: { width: 1280, height: 900 } });
await blockedReasonArm();

server.close();
await browser.close();

console.log('\n================================================================');
console.log(failed === 0
    ? '\u2705 发起页视口判据通过(单列档 + 两栏档)'
    : `\u{1F534} 发起页视口判据未通过 \u00b7 ${failed} 条红`);
process.exit(failed === 0 ? 0 : 1);
