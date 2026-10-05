#!/usr/bin/env node
/**
 * G2b 门三返修判据 —— legacy「核心搜索问题」的 `required` 分模式(**DOM 级**)
 *
 * ## 为什么必须打 DOM 级,不能打源码串或纯逻辑
 *
 * G2 已经把按钮的 `disabled` 放开了,但**没解开原生表单校验**。这一页的 CTA 是
 * `<form>` 内的 `type="submit"`(NewDiagnosis.tsx 第 1912 行注释:双触发依赖
 * `e.currentTarget.form`),浏览器在 submit 事件**之前**跑约束校验 ——
 * `required` 不过就根本不 fire `onSubmit`,`handleSubmit` 里那条
 * `if (isDefensiveFlow) runDefensivePlanPreview()` 永远走不到。
 *
 * 也就是说:**"按钮能不能点"和"点了会不会真提交"是两把不同的闸**。
 * G2 的判据(defensiveLaunchGate 纯逻辑)只证明了第一把开了;
 * 第二把闸是浏览器实现的,只有把真 DOM 交给真浏览器问一句
 * `form.checkValidity()` 才算验过。用 jsdom 都不够硬 —— 约束校验是
 * 浏览器行为,拿它的实现当判据才是同构的。
 *
 * ## 判据里"被测的那一行"从哪来
 *
 * 🔴 **不自己拼 HTML**。自己写一个 `<form><textarea required>` 只能证明浏览器
 *    的 checkValidity 是按规范实现的 —— 那是在验 Chromium,不是在验我们的代码。
 *    这里 esbuild 打包**真的 NewDiagnosis.tsx**(连同真的 Provider 栈),
 *    在**真浏览器里**用 `renderToString` 渲染,再把产物塞进真 document。
 *    `required` 属性是真组件按真 `campaignMode` 渲出来的。
 *
 *    渲染放在浏览器里跑,顺带解决了 window / localStorage / matchMedia 的
 *    polyfill 问题 —— 真的全都在;而 renderToString 不跑 effect,
 *    所以没有网络请求、没有异步竞态,判据是确定性的。
 *
 * ## 输入怎么给
 *
 * 品牌名/行业用 `el.value = ...` 直接写 DOM —— 约束校验读的就是 DOM value,
 * 这等价于"用户已经打过字"。真实防御流里这两格由**选中客户品牌**自动填
 * (NewDiagnosis.tsx 第 428 行 `brandName: b.name, industry: b.industry || prev.industry`),
 * 而防御闸本来就要求 `hasBrandId`。`keywords` 一格**留空**,那正是被测场景。
 *
 * ## 成对
 *
 * 同一份填充状态,只有 `?goal=` 不同:
 *   · 防御/混合 ⇒ checkValidity() 必须 **true**
 *   · legacy(offensive)⇒ 必须仍 **false**,且 `:invalid` 里**恰好是 #keywords**
 * 再加一格区分力对照:legacy 把 keywords 填上 ⇒ 必须转 true
 * (否则那个 false 可能来自别的必填格,判据就指错了地方)。
 *
 * 跑法:cd frontend && node scripts/test-defgeo-legacy-required.mjs
 * 退出码:0=全绿 1=有红/判据不可用
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
const check = (cond, m, d = '') => (cond ? ok(m, d) : bad(m, d));
const section = (t) => console.log(`\n=== ${t} ===`);

/** 判据不可用时**不当绿灯** —— 直接非零退出,别让"没跑起来"混成"没问题"。 */
function unusable(why, detail) {
    console.log(`\u{1F534} 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 3000));
    process.exit(1);
}

// ── 1. 打包真组件 ────────────────────────────────────────────────
let esbuild, playwright;
try {
    esbuild = require_('esbuild');
    playwright = require_('playwright');
} catch (err) {
    unusable('esbuild / playwright 取不到', err);
}

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
const outDir = mkdtempSync(join(CACHE_ROOT, 'defgeo-legacy-required-'));
/* 🔴 a179-cleanup:跑完删掉这一份,免得 .cache 里堆满 bundle
   (一次全量注毒 = 17 发 × 两把闸 = 34 份)。进程怎么退出都删。 */
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });

const p = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { renderToString } from 'react-dom/server';
import { BrowserRouter } from 'react-router-dom';
import { AuthProvider } from ${p('src/context/AuthContext.tsx')};
import { OrganizationProvider } from ${p('src/context/OrganizationContext.tsx')};
import { UserModeProvider } from ${p('src/context/UserModeContext.tsx')};
import { WalletProvider } from ${p('src/context/WalletContext.tsx')};
import { OnboardingProvider } from ${p('src/context/OnboardingContext.tsx')};
import { PricingProvider } from ${p('src/context/PricingContext.tsx')};
import { ClientProvider } from ${p('src/context/ClientContext.tsx')};
import { NewDiagnosis } from ${p('src/pages/Diagnosis/NewDiagnosis.tsx')};

// 🔴 Provider 栈照 App.tsx 第 520-526 / 624 行的真实嵌套顺序,不自己编一套。
(globalThis as any).__renderPage = function () {
    return renderToString(
        <BrowserRouter>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <NewDiagnosis />
            </ClientProvider></PricingProvider></OnboardingProvider>
          </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
        </BrowserRouter>
    );
};
`, 'utf8');

try {
    await esbuild.build({
        entryPoints: [join(outDir, 'entry.tsx')],
        bundle: true,
        outfile: join(outDir, 'bundle.js'),
        format: 'iife',
        // 组件是给浏览器写的,按浏览器条件解析才同构(见 test-gap-plan-render.mjs 的教训)
        platform: 'browser',
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        jsx: 'automatic',
        alias: { '@': join(ROOT, 'src') },
        loader: {
            '.tsx': 'tsx', '.ts': 'ts',
            // 🔴 Vite 自己懂 `import x from './a.jpg'`，esbuild 不懂 —— 不配 loader 就是
            //    「打包失败 ⇒ 判据不可用」(而不是静默少一张图)。用 dataurl 而不是置空：
            //    样图本身就是 #188 要验的东西(「风格这里必须让人看到成品是什么样」)，
            //    置空会让所有样图臂在真缺陷下也照样绿。
            '.jpg': 'dataurl', '.jpeg': 'dataurl', '.png': 'dataurl',
            '.webp': 'dataurl', '.svg': 'dataurl',
        },
        // Vite 的 `?raw` 后缀 esbuild 不认(sandbox/mockData.ts 引 demoReport.md?raw)。
        // 补一个最小 plugin,行为与 Vite 一致:把文件当纯文本 default export。
        plugins: [{
            name: 'vite-raw-suffix',
            setup(build) {
                build.onResolve({ filter: /\?raw$/ }, (args) => {
                    const bare = args.path.replace(/\?raw$/, '');
                    const abs = bare.startsWith('@/')
                        ? join(ROOT, 'src', bare.slice(2))
                        : join(args.resolveDir, bare);
                    return { path: abs, namespace: 'vite-raw' };
                });
                build.onLoad({ filter: /.*/, namespace: 'vite-raw' }, (args) => ({
                    contents: readFileSync(args.path, 'utf8'),
                    loader: 'text',
                }));
            },
        }],
        logLevel: 'silent',
    });
} catch (err) {
    unusable('打包真组件失败', err);
}

writeFileSync(join(outDir, 'harness.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>defgeo G2b harness</title></head>
<body><div id="out"></div>
<script src="./bundle.js"></script>
<script>
try { window.__html = window.__renderPage(); window.__err = null; }
catch (e) { window.__err = String((e && e.stack) || e); }
</script>
</body></html>
`, 'utf8');

// ── 2. 起一个本地静态服 ─────────────────────────────────────────
// file:// 下 location.search 与脚本加载在各内核上行为不一,用 http 消除这个变量。
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8' };
const server = createServer((req, res) => {
    const name = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'harness.html';
    try {
        const buf = readFileSync(join(outDir, name));
        res.writeHead(200, { 'Content-Type': MIME[extname(name)] || 'application/octet-stream' });
        res.end(buf);
    } catch { res.writeHead(404); res.end('nope'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;
const url = (q) => `http://127.0.0.1:${PORT}/harness.html${q}`;

// ── 3. 真浏览器 ─────────────────────────────────────────────────
let browser;
try { browser = await playwright.chromium.launch(); }
catch (err) { server.close(); unusable('Chromium 起不来', err); }

/**
 * 渲染一次并在真 DOM 上做一次约束校验。
 * @param goalQuery  ?goal=... (空串 = 不带参,走默认 offensive)
 * @param fill       要写进 DOM 的值(等价于"用户打过字")
 */
async function probe(goalQuery, fill) {
    const page = await browser.newPage();
    const consoleErrors = [];
    page.on('pageerror', (e) => consoleErrors.push(String(e)));
    await page.goto(url(goalQuery), { waitUntil: 'load' });

    const renderErr = await page.evaluate(() => window.__err);
    if (renderErr) { await page.close(); return { renderErr, consoleErrors }; }

    const result = await page.evaluate((values) => {
        document.getElementById('out').innerHTML = window.__html;
        const kw = document.getElementById('keywords');
        const brand = document.getElementById('brandName');
        const industry = document.getElementById('industry');
        if (!kw || !brand || !industry) {
            return { missing: { kw: !!kw, brand: !!brand, industry: !!industry } };
        }
        // 约束校验读的是 DOM value —— 直接写 value 等价于用户已经打过字。
        brand.value = values.brandName;
        industry.value = values.industry;
        kw.value = values.keywords;

        const form = kw.form;
        if (!form) return { missing: { form: false } };

        // 🔴 checkValidity() **不受 noValidate 影响**(2026-08-22 撕锁 MUT-5 实测:
        //    给 <form> 加 noValidate,checkValidity 照样 false)。所以"点了会不会
        //    真提交"的完整谓词是 `!noValidate && checkValidity()` —— 与其推,
        //    不如直接问浏览器:真的 requestSubmit 一次,看 submit 事件 fire 没有。
        //    这才是用户那句"按钮看着能点、点了没反应"的逐字对应物。
        let submitFired = false;
        const onSubmit = (e) => { submitFired = true; e.preventDefault(); };
        form.addEventListener('submit', onSubmit);
        try { form.requestSubmit(); } catch (e) { /* 记为未 fire */ }
        form.removeEventListener('submit', onSubmit);

        return {
            submitFired,
            htmlLen: window.__html.length,
            hasCta: window.__html.includes('data-testid="launch-cta"'),
            hasSubmit: window.__html.includes('data-testid="launch-submit"'),
            search: window.location.search,
            keywordsRequiredAttr: kw.hasAttribute('required'),
            keywordsRequiredProp: kw.required,
            brandRequired: brand.required,
            industryRequired: industry.required,
            formNoValidate: form.noValidate,
            valid: form.checkValidity(),
            invalid: Array.from(form.querySelectorAll(':invalid')).map((e) => e.id || e.tagName),
            requiredIds: Array.from(form.querySelectorAll('[required]')).map((e) => e.id || e.tagName),
        };
    }, fill);

    await page.close();
    return { ...result, consoleErrors };
}

const FILLED = { brandName: '门三返修测试品牌', industry: '教育培训', keywords: '' };
const FILLED_WITH_KW = { ...FILLED, keywords: '深圳瑜伽馆哪家好' };
const EMPTY_INDUSTRY = { ...FILLED, industry: '' };

// ── 4. 先证明"渲染真的发生了" ────────────────────────────────────
section('判据活性:真组件真渲染(渲染不出来 ⇒ 后面全部作废)');
const defensive = await probe('?goal=defensive', FILLED);
if (defensive.renderErr) {
    server.close(); await browser.close();
    unusable('真组件在浏览器里渲染抛错', defensive.renderErr);
}
if (defensive.missing) {
    server.close(); await browser.close();
    unusable('渲染产物里找不到被测控件', JSON.stringify(defensive.missing));
}
check(defensive.htmlLen > 5000, '渲染产物是整页而不是空壳', `${defensive.htmlLen} 字节`);
check(defensive.hasCta && defensive.hasSubmit, 'CTA(launch-cta / launch-submit)在产物里');
check(defensive.search === '?goal=defensive', 'location.search 真的进到了组件初值', defensive.search);
check(defensive.formNoValidate === false, '<form> 没有 noValidate(原生校验是活的)');
// 🔴 [包H 2026-08-24] 原文是「品牌名/行业两格仍是必填」。Owner 已裁定防御模式
//    行业不强制,于是 industryRequired 在 ?goal=defensive 下**应当**是 false。
//    品牌名不受影响:防守流也必须绑定具体品牌(defensiveLaunchGate.hasBrandId),
//    它在任何模式下都必填 —— 这一格保持原样,继续守"别顺手改掉 legacy 语义"。
check(defensive.brandRequired === true,
    '品牌名仍是必填(任何模式都要绑定具体品牌)');
check(defensive.industryRequired === false,
    '防御模式下行业**不是**必填(Owner 2026-08-24)',
    `industryRequired=${defensive.industryRequired}`);

// ── 5. 正:防御 / 混合 ───────────────────────────────────────────
section('正向:防御 / 混合模式 + 空 legacy 栏 ⇒ checkValidity() 必须 true');
for (const [label, q] of [['defensive', '?goal=defensive'], ['hybrid', '?goal=hybrid']]) {
    const r = label === 'defensive' ? defensive : await probe(q, FILLED);
    if (r.renderErr || r.missing) { bad(`${label}:渲染失败`, r.renderErr || JSON.stringify(r.missing)); continue; }
    check(r.keywordsRequiredAttr === false && r.keywordsRequiredProp === false,
        `${label}:#keywords 不带 required 属性`);
    check(r.valid === true,
        `${label}:空「核心搜索问题」下 form.checkValidity() === true`,
        r.valid ? '' : `:invalid = [${r.invalid.join(', ')}]`);
    check(!r.requiredIds.includes('keywords'),
        `${label}:表单必填集合里没有 keywords`, `[${r.requiredIds.join(', ')}]`);
    // 🔴 真行为:CTA 是 form 内的 type="submit",submit 不 fire ⇒ handleSubmit 里
    //    那条 `if (isDefensiveFlow) runDefensivePlanPreview()` 根本走不到。
    check(r.submitFired === true,
        `${label}:requestSubmit() 真的 fire 了 submit 事件(防御流能走到 handleSubmit)`);
}

// ── 6. 反:legacy ───────────────────────────────────────────────
section('反向:legacy(offensive)必须仍被拦住');
for (const [label, q] of [['显式 ?goal=offensive', '?goal=offensive'], ['不带参(默认)', '']]) {
    const r = await probe(q, FILLED);
    if (r.renderErr || r.missing) { bad(`${label}:渲染失败`, r.renderErr || JSON.stringify(r.missing)); continue; }
    check(r.keywordsRequiredAttr === true && r.keywordsRequiredProp === true,
        `${label}:#keywords 仍带 required`);
    check(r.valid === false, `${label}:空「核心搜索问题」下 checkValidity() === false`);
    // 🔴 区分力:那个 false 必须**恰好**由 keywords 造成,不能是别的格顺手判红。
    check(r.invalid.length === 1 && r.invalid[0] === 'keywords',
        `${label}::invalid 恰好是 #keywords 一格`, `[${r.invalid.join(', ')}]`);
    check(r.submitFired === false,
        `${label}:requestSubmit() 被原生校验拦住,submit 事件不 fire`);
}

section('区分力对照:legacy 把 keywords 填上 ⇒ 必须转 true');
{
    const r = await probe('?goal=offensive', FILLED_WITH_KW);
    check(r.valid === true, 'legacy + 已填 keywords ⇒ checkValidity() === true',
        r.valid ? '' : `:invalid = [${r.invalid.join(', ')}]`);
}

// ── 7. Owner 已裁(2026-08-24):防御/混合「所属行业」不强制 ──────────
//
// 🔴 这一节原本是**现状锁**,原文写着「本次未改,**等 Owner 裁**」——
//    G2b 那一轮把残留洞如实钉住,把决定权留给 Owner。Owner 已于 2026-08-24
//    拍板:防御模式所属行业不强制(但 AI 尽量填全)。所以现状锁到期,
//    翻成**期望锁**,并且比原来多守一格:两个模式都要验。
//
//    为什么不是删掉这一节:删掉等于这条行为从此没人验 ——
//    "把锁摘了"和"锁通过了"在构建日志里长得一样。
section('Owner 08-24 裁定:防御/混合 行业为空**放行**;legacy 仍拦');
{
    const d = await probe('?goal=defensive', EMPTY_INDUSTRY);
    check(d.valid === true,
        '防御模式下「所属行业」空着**不再**拦提交(Owner 2026-08-24)',
        `valid=${d.valid} :invalid=[${d.invalid.join(', ')}]`);
    check(!d.invalid.includes('industry'),
        ':invalid 里没有 industry(原生 required 真的解开了)',
        `:invalid = [${d.invalid.join(', ')}]`);

    // 🔴 必须不命中的那一半:legacy(offensive)仍然拦,且拦的**恰好**是 industry。
    //    没有这一臂,把 required 整条删掉上面两条照样全绿 —— 那会把 legacy
    //    的必填语义顺手改掉,而 ACT-01 要求 legacy 提交路径逐字不变。
    const o = await probe('?goal=offensive', EMPTY_INDUSTRY);
    check(o.valid === false && o.invalid.includes('industry'),
        'legacy(offensive)行业为空**仍然**被拦(必填语义未被顺手改掉)',
        `valid=${o.valid} :invalid=[${o.invalid.join(', ')}]`);
}

server.close();
await browser.close();

console.log('\n================================================================');
console.log(failed === 0
    ? '\u2705 G2b DOM 判据通过(防御/混合放行 + legacy 仍拦 + 指向恰好是 keywords)'
    : `\u{1F534} G2b DOM 判据未通过 \u00b7 ${failed} 条红`);
process.exit(failed === 0 ? 0 : 1);
