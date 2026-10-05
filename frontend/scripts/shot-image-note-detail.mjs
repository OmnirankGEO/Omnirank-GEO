#!/usr/bin/env node
/**
 * 截图 · #203 图文三栏详情页(真 chromium + dist 真 CSS)。
 *
 * 出图前逐张量主题亮度:标称与实测不符就不出这张 —— 一张名字和内容对不上的图,
 * 比没有图更坏,它会被当证据用。
 *
 * 🔴 夹具用的是 `GET /api/geo-douyin/posts/{id}` 的**真实回包形状**,逐字段从
 *    `api/geo_douyin_api.py::api_get_post` 的返回体派生:
 *      `{status, post, preview_urls, task, siblings, ranking, style, redraw, contact}`
 *    其中 `post` 是**库行**(`body_text` / `oss_keys` / `cards` / `active_revision_id` …)。
 *    08-17 那张壳子页就是死在"按前端自己的字段造夹具":它读 `bodyText`、
 *    读 `cards[].image_url`,两个在库里都不存在,于是判据绿、真数据全空。
 *
 * 🔴 没有 dist 真 CSS 就没有尺寸:裸 DOM 上量 `getBoundingClientRect` 恒为 0,
 *    "看得见"与"三栏并排"两组断言会全部变成空话。取不到就报判据不可用并非零退出。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a203-shot-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const require_ = createRequire(import.meta.url);
let failed = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 1200));
    process.exit(1);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { unusable('esbuild / playwright 取不到', err); }

const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

/* 入口:只挂被测那条路由本身,走真 Router —— 路由解析也一起验了(D1)。 */
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
import { ImageNoteDetailRoute } from ${q('src/pages/Writing/ImageNoteDetailRoute.tsx')};

let __root: any = null;
(globalThis as any).__mount = function (el: HTMLElement, entry: string, nonce?: number) {
    if (!__root) __root = createRoot(el);
    __root.render(
      <MemoryRouter initialEntries={[entry]} key={'m' + (nonce || 0)}>
        <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
          <OnboardingProvider><PricingProvider><ClientProvider>
            <Routes>
              <Route path="/writing/image-note/:postId" element={<ImageNoteDetailRoute />} />
            </Routes>
          </ClientProvider></PricingProvider></OnboardingProvider>
        </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
      </MemoryRouter>);
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
    unusable('取不到真 CSS —— 没有它,"看得见"和"三栏并排"都量不了', err);
}
console.log(`  (真 CSS:dist/assets/${cssName})`);

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a203</title>
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

const IMG = 'data:image/svg+xml;utf8,'
    + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="270" height="360">'
        + '<rect width="270" height="360" fill="#8899aa"/></svg>');

/**
 * 🔴 夹具 = `api_get_post` 的真实回包形状。字段名一个都不改写。
 *    `post` 是库行:`body_text`(不是 bodyText)、`oss_keys`、`cards`、`hashtags`…
 */
const POST_PAYLOAD = (id) => ({
    status: 'success',
    post: {
        id, brand_id: 629, title: 'QA 夹具标题', body_text: 'QA 夹具正文的第一句。第二句。',
        hashtags: ['夹具', '图文'],
        cards: [{ idx: 0 }, { idx: 1 }, { idx: 2 }],
        oss_keys: ['k/0.png', 'k/1.png', 'k/2.png'],
        status: 'ready', city: '广州', keyword: 'QA 关键词', redraw_count: 0,
        style_key: 'clean', contact_enabled: false, closing_stale: false,
        aspect_ratio: '3:4', active_revision_id: 77,
    },
    preview_urls: [IMG, IMG, IMG],
    task: null,
    siblings: [
        { id: 40, city: '广州', status: 'ready', title: 'QA 夹具标题' },
        { id: 41, city: '深圳', status: 'ready', title: 'QA 深圳版' },
        { id: 42, city: '佛山', status: 'ready', title: 'QA 佛山版' },
    ],
    ranking: null,
    /* 🔴 当前这篇用的那一款,也得是后端真有的四款之一
       (`STYLE_PRESETS`:design_text 设计文字卡 / table_review 表格测评卡 /
        memo 备忘录体 / photo_overlay 实拍叠字)。
       编一个 `clean 清爽` 的话,顶栏会写着「当前风格:清爽」、
       选择器里**没有一款是选中的** —— 而截图上看不出这是夹具编的。 */
    style: { key: 'design_text', label: '设计文字卡' },
    redraw: { used: 0, limit: 3, remaining: 3 },
    contact: { configured: false, display: '', enabled: false, closing_stale: false },
});

async function openDetail(browser, { width = 1600, postId = 41, theme = 'light', noImages = false, action = null } = {}) {
    const page = await browser.newPage({ viewport: { width, height: 1200 } });
    if (process.env.A203_DEBUG) {
        page.on('pageerror', (e) => console.log('  [pageerror]', String(e).slice(0, 300)));
        page.on('console', (m) => { if (m.type() === 'error') console.log('  [console]', m.text().slice(0, 300)); });
    }
    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'qa-token');
        /* 🔴 **不开沙盒**:沙盒拦截器是第二个后端,会把 geo-douyin 的口接走,
           本臂要验的正是"这张页读真回包读得对不对"。 */
    });
    await page.route('**/api/**', async (route) => {
        const url = new URL(route.request().url());
        const path = url.pathname;
        const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
        if (path.includes('/auth/me')) return json(ME);
        const m = path.match(/\/api\/geo-douyin\/posts\/(\d+)$/);
        if (m) {
            const p = POST_PAYLOAD(Number(m[1]));
            /* 无图态:签名失败时后端回的就是空 preview_urls(不是没有这个字段) */
            if (noImages) { p.preview_urls = []; p.post.oss_keys = []; p.post.cards = []; }
            return json(p);
        }
        /*
         * ── #204 a1 的桩:左栏选题流 ──────────────────────────────────
         * 🔴 没有这几口,它们会掉进文件末尾那个兜底 `{status:'success'}`：
         *    选题列表回不出 `topics` ⇒ **左栏整块是空的**,而那不是产品的样子。
         *    「认不出的口回最小成功体」对读不到就降级的口是对的,
         *    对左栏这种"没数据就整块不渲染"的口就是**把内容截丢了**。
         */
        if (/\/clients\/\d+\/topics$/.test(path)) {
            return json({ topics: TOPICS });
        }
        if (/\/clients\/\d+\/plan$/.test(path)) {
            return json({ total_quota: 10, total_done: 3, total_gap: 7 });
        }
        if (path.includes('/geo-douyin/production-quote')) {
            return json({ total_points: 390 });
        }
        if (path.includes('/geo-douyin/distill-topics')) {
            return json({ task_id: 77 });
        }
        if (/\/distill-tasks\/\d+$/.test(path)) {
            return json({ task_id: 77, state: 'running', stage: 'gathering',
                stage_label: '正在找素材', percent: 37, active: true });
        }
        if (path.includes('/geo-douyin/pricing')) {
            /* 🔴 蒸馏那一档的键是 **`topic_distill`**(`api/geo_douyin_api.py`:
               `"topic_distill": distill`)。写错键名不会报错,只会让
               「生成选题」上永远写着「价目读不到」并永远禁用 ——
               那颗按钮会以那个样子进交付截图。 */
            return json({ first_generation: null, regenerate: null, redraw: null, extra_card: null,
                included_cards: 3, redraw_limit: 3, ranking_templates: [],
                topic_distill: { feature_code: 'geo_douyin_topic_distill', cost_points: 130,
                    feature_name: '图文选题蒸馏' } });
        }
        /* 形状照后端 `style_choices()`:每款带 key/label/density/needs_photo
           + disabled 与 **disabled_reason**(不可选那款必须带原因,是后端的既有口径)。
           回空数组的话左栏的「卡面风格」整块不渲染 —— 截图就少了一块真实内容。 */
        if (path.includes('/geo-douyin/styles')) {
            /*
             * 🔴 四款的 key/label 照后端 `services/geo_douyin/card_templates.py`
             *    的 `STYLE_PRESETS` 原样抄:
             *      design_text 设计文字卡 / table_review 表格测评卡
             *      memo 备忘录体 / photo_overlay 实拍叠字
             *    第一版这里编了 `clean/rich/photo`(清爽/信息量大/实拍风)——
             *    产品里根本没有这四个字。而且它们**对不上** `STYLE_SAMPLES` 的键,
             *    于是「卡面风格」整块渲染成**裸按钮、一张样图都没有**,
             *    交付截图里就是 Owner 两次点名不许出现的那个样子。
             *    🔴 夹具按前端印象编,截出来的图是**关于另一个世界的证据**。
             *    `disabled_reason` 的原话也抄 `style_choices()`(需实拍图那款)。
             */
            return json({ status: 'success', styles: [
                { key: 'design_text', label: '设计文字卡', density: 'mid', needs_photo: false,
                    disabled: false, disabled_reason: '' },
                { key: 'table_review', label: '表格测评卡', density: 'high', needs_photo: false,
                    disabled: false, disabled_reason: '' },
                { key: 'memo', label: '备忘录体', density: 'mid', needs_photo: false,
                    disabled: false, disabled_reason: '' },
                { key: 'photo_overlay', label: '实拍叠字', density: 'low', needs_photo: true,
                    disabled: true,
                    disabled_reason: '这个客户还没有可对外使用的实拍图，先在资料库里补几张并确认可用' },
            ] });
        }
        /*
         * 🔴 形状照后端 `/api/geo-douyin/status`:
         *    `{status, can_make, can_publish, publish_disabled_reason}`。
         *    第一版随手回了个 `{ok:true}` —— 页面读 `d.can_publish` 得到 undefined,
         *    于是「去发布投放」**灰着**出现在交付截图里,而那不是产品的样子。
         *    夹具的字段名错了,截出来的图就是**关于另一个世界的证据**。
         */
        if (path.includes('/geo-douyin/status')) {
            return json({ status: 'success', can_make: true, can_publish: true,
                publish_disabled_reason: '' });
        }
        /*
         * 🔴 右栏资料卡的形状照 `api_post_knowledge` = `{status, **build_client_knowledge, sources_used}`。
         *    第一版这里落进了下面那个笼统兜底 `{success, items, total}` ——
         *    页面读 `knowledge.materials.total` 当场抛 TypeError,**整张页白屏**,
         *    于是 11 格全红而原因和产品无关。
         *    记一笔:**兜底夹具的形状也是夹具**;给一个"看起来像成功"的错形状,
         *    比返回 404 更坏 —— 404 那条路页面是有兜底的(显示「—」)。
         */
        if (/\/knowledge$/.test(path)) {
            return json({
                status: 'success', has_brand: true, load_failed: false,
                materials: {
                    filled: 3, total: 5,
                    items: [
                        { key: 'company_intro', label: '公司简介', present: true },
                        { key: 'selling_points', label: '核心卖点', present: true },
                        { key: 'success_cases', label: '成功案例', present: true },
                        { key: 'core_value', label: '核心价值', present: false },
                        { key: 'testimonials', label: '客户证言', present: false },
                    ],
                },
                images: { count: 0, thumbs: [] },
                sources_used: ['品牌档案'],
            });
        }
        if (/\/consistency$/.test(path)) {
            return json({
                status: 'success', checked: true, reason: '', flagged_count: 0, cards: [],
                alignment: { checked: true, reason: '', ok: true, issues: [] },
            });
        }
        /* 认不出的口一律回**最小成功体**,不给一个长得像业务数据的形状。 */
        return json({ status: 'success' });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    if (theme === 'dark') await page.evaluate(() => document.documentElement.classList.add('dark'));
    await page.evaluate((e) => globalThis.__mount(document.getElementById('root'), e, 1),
        `/writing/image-note/${postId}`);
    await page.waitForTimeout(1200);
    /* 换 key 重挂一次:第一发请求可能赶在鉴权确认之前(harness 特有,不是产品缺陷)。
       同一容器第二次 createRoot 会变成两个 root,读数就成了抛硬币。 */
    await page.evaluate((e) => globalThis.__mount(document.getElementById('root'), e, 2),
        `/writing/image-note/${postId}`);
    await page.locator('[data-testid="detail-three-cols"]').first()
        .waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
    await page.waitForTimeout(800);
    /* 截图前的交互(勾选 / 点 chip / 起蒸馏):每个场景一把,别互相叠。 */
    if (action) { await action(page); await page.waitForTimeout(900); }
    if (process.env.A203_DEBUG) {
        const txt = await page.evaluate(() => (document.getElementById('root') || document.body).innerText);
        console.log('  [debug]', JSON.stringify(txt).slice(0, 500));
    }
    return page;
}

/** 看得见 = rect 非零 + 不 hidden + opacity > 0.05 */
const visible = (page, sel) => page.evaluate((s) => {
    const el = document.querySelector(s);
    if (!el) return { found: false };
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
        found: true,
        w: Math.round(r.width), h: Math.round(r.height), top: Math.round(r.top), left: Math.round(r.left),
        vis: r.width > 0 && r.height > 0 && cs.visibility !== 'hidden'
            && cs.display !== 'none' && Number(cs.opacity) > 0.05,
        text: (el.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 80),
        value: 'value' in el ? String(el.value).slice(0, 60) : '',
    };
}, sel);

/*
 * 🔴 三态各一条,状态字串照 c1 的真集合(pending/making/done/failed)。
 *    夹具里**故意不放**认不出的状态:那一条的行为由判据 A9b 与注毒 S6 管,
 *    截图管的是"产品长什么样",不是"怪数据进来会怎样"。
 */
const TOPICS = [
    { id: 1, title: '梅州客天下附近有没有靠谱的婚宴酒店', keyword: '梅州 婚宴酒店',
        city: '梅州', style_key: 'design_text', status: 'pending', source: 'distilled' },
    { id: 4, title: '梅州办婚宴一桌大概多少钱', keyword: '梅州 婚宴 价格',
        city: '梅州', style_key: 'memo', status: 'pending', source: 'user' },
    { id: 2, title: '梅州婚宴场地怎么挑不踩坑', keyword: '梅州 婚宴 场地',
        city: '梅州', style_key: 'memo', status: 'making', source: 'distilled' },
    { id: 3, title: '梅州婚宴酒店实拍对比', keyword: '梅州 婚宴 实拍',
        city: '梅州', style_key: 'memo', status: 'done', post_id: 41, source: 'user' },
];

const click = (sel) => (page) => page.evaluate((s2) => {
    const e = document.querySelector(s2);
    if (e) e.click();
}, sel);

const OUT = join(ROOT, '..', '..', 'a204-shots');
mkdirSync(OUT, { recursive: true });

const browser = await playwright.chromium.launch();
try {
    for (const theme of ['light', 'dark']) {
        for (const [name, opt] of [
            ['detail_with_images', { width: 1600, postId: 41 }],
            ['detail_no_images', { width: 1600, postId: 41, noImages: true }],
            /* ── #204 a1 的四个场景 ── */
            ['topics_pending', { width: 1600, postId: 41 }],
            ['topics_selected', { width: 1600, postId: 41,
                action: click('[data-testid="topic-check"]') }],
            ['topics_distilling', { width: 1600, postId: 41,
                action: click('[data-testid="topics-distill"]') }],
            ['topics_making_locked', { width: 1600, postId: 41,
                action: async (page) => {
                    await page.evaluate(() => {
                        const c = document.querySelector('[data-testid="topics-chip"][data-chip="making"]');
                        if (c) c.click();
                    });
                    await page.waitForTimeout(400);
                    await page.evaluate(() => {
                        const r = document.querySelector('[data-testid="topic-row"]');
                        if (r) r.click();
                    });
                } }],
            ['detail_narrow', { width: 900, postId: 41 }],
        ]) {
            const page = await openDetail(browser, { ...opt, theme });
            /* 🔴 主题 class 在 goto 之后、量之前挂 —— addInitScript 跑在文档解析之前,
               那一刻 documentElement 还不在(同族坑本仓第六次)。 */
            const lum = await page.evaluate(() => {
                const bg = getComputedStyle(document.body).backgroundColor;
                const c = document.createElement('canvas');
                c.width = 1; c.height = 1;
                const g = c.getContext('2d');
                g.fillStyle = bg; g.fillRect(0, 0, 1, 1);
                const d = g.getImageData(0, 0, 1, 1).data;
                return 0.2126 * d[0] + 0.7152 * d[1] + 0.0722 * d[2];
            });
            const looksDark = lum < 128;
            if ((theme === 'dark') !== looksDark) {
                console.log(`  🔴 ${name}/${theme}:标称与实测不符(亮度 ${Math.round(lum)})—— 不出这张`);
                await page.close();
                continue;
            }
            const f = join(OUT, `${name}_${theme}.png`);
            await page.screenshot({ path: f, fullPage: true }).catch(() => { });
            console.log(`  ${name}/${theme}: 亮度 ${Math.round(lum)} → ${f}`);
            await page.close();
        }
    }
} finally {
    try { await browser.close(); } catch { /* 尽力 */ }
    try { server.close(); } catch { /* 尽力 */ }
}
console.log('截图完成(每张都量过主题)');
process.exit(0);
