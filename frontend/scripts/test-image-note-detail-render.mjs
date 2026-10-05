#!/usr/bin/env node
/**
 * 行为臂 · #203 接回的图文三栏详情页(真 chromium + dist 真 CSS)。
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
 *
 * 🔴 [WO_258] 0913P 起详情页只剩两栏(「设置」整列搬去工作台第 1 步),选题 / 价 /
 *    蒸馏进度都在第 1 步:D7/D7b 按两栏量,T 段从 `/writing/image-note` 进。
 *    本臂此前不在 build 链也不在 arms,只被 runner 204 当 r 闸 —— 烂了两班没人知道;
 *    现在挂进 `browser:arms`(签字前必跑)。逐格定性见 WO_258 交付单。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a203-render-'));
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
import { WritingWorkspace } from ${q('src/pages/Writing/WritingWorkspace.tsx')};
import { useLocation } from 'react-router-dom';

/* 把当前 location 暴露出来:MemoryRouter 不改 window.location,
   不这样做就只能去源码里找那条模板 —— 而那正是被死代码骗过的那种锚。 */
function LocProbe() {
    const l = useLocation();
    (globalThis as any).__loc = l.pathname + l.search;
    return null;
}

let __root: any = null;
(globalThis as any).__mount = function (el: HTMLElement, entry: string, nonce?: number) {
    if (!__root) __root = createRoot(el);
    __root.render(
      <MemoryRouter initialEntries={[entry]} key={'m' + (nonce || 0)}>
        <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
          <OnboardingProvider><PricingProvider><ClientProvider>
            <LocProbe />
            <Routes>
              {/* 与 App.tsx 同形:无 id = 工作台第 1 步(选题面板在这里挂),有 id = 作品详情 */}
              <Route path="/writing/image-note" element={<ImageNoteDetailRoute />} />
              <Route path="/writing/image-note/:postId" element={<ImageNoteDetailRoute />} />
              <Route path="/writing" element={<WritingWorkspace />} />
              <Route path="/publish" element={<div data-testid="landed-on-publish" />} />
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

async function openDetail(browser, plan = {}) {
    /* 🔴 参数整体收成 `plan`:桩里要往它上面记请求计数与夹具状态
       (`plan.counts` / `plan.topics` / `plan.patched`),解构成局部变量就记不回去。 */
    const { width = 1600, postId = 41, entry = '' } = plan;
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
    plan.counts = {};
    plan.topics = plan.topics || [
        { id: 1, title: '待做的第一条', keyword: '关键词A', city: '广州',
            style_key: 'design_text', status: 'pending', source: 'distilled' },
        { id: 2, title: '正在做的那条', keyword: '关键词B', city: '深圳',
            style_key: 'memo', status: 'making', source: 'distilled' },
        { id: 3, title: '做好了的那条', keyword: '关键词C', city: '佛山',
            style_key: 'memo', status: 'done', post_id: 41, source: 'user' },
    ];
    await page.route('**/api/**', async (route) => {
        const url = new URL(route.request().url());
        const path = url.pathname;
        const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
        if (path.includes('/auth/me')) return json(ME);
        /* ── [WO_282] 只在新格的 plan 上打开的桩:不改既有各格的环境 ── */
        if (plan.batchNetworkFail && path.includes('/geo-douyin/posts/batch')) {
            plan.counts.batchAborted = (plan.counts.batchAborted || 0) + 1;
            /* 真断网:浏览器 fetch 抛的就是它自己的原话 TypeError「Failed to fetch」 */
            return route.abort('failed');
        }
        if (plan.postsListHtml502 && path === '/api/geo-douyin/posts') {
            plan.counts.postsList = (plan.counts.postsList || 0) + 1;
            /* nginx 502 回的是 HTML 页面 —— 原锁要拦的那一种:裸 .json() 会抛 Unexpected token '<' */
            return route.fulfill({ status: 502, contentType: 'text/html',
                body: '<html><head><title>502 Bad Gateway</title></head><body><center>502 Bad Gateway</center></body></html>' });
        }
        /* ── [WO_283] 同样只在新格的 plan 上打开 ── */
        if (plan.rankingGates && /\/api\/geo-douyin\/posts\/\d+$/.test(path)) {
            /* 形状照 `api_get_post` 的 ranking 摘要(`DouyinPostDetail.tsx` RankingSummary),
               gates 三键照 `services/geo_douyin/ranking_gates.SERIALIZED_KEYS` = gate / message / card_indices */
            const id = Number(path.split('/').pop());
            return json({ ...POST_PAYLOAD(id), ranking: {
                content_form: 'ranking', template: 't1', template_label: '合成母版', entity_count: 5,
                entity_count_actual: 3, degraded: false, engine_label: '合成引擎', fallback_notice: null,
                gates: plan.rankingGates } });
        }
        if (plan.redrawNetworkFail && /\/cards\/\d+\/redraw$/.test(path)) {
            plan.counts.redrawAborted = (plan.counts.redrawAborted || 0) + 1;
            return route.abort('failed');
        }
        if (plan.latestDegraded !== undefined && /\/clients\/9001\/latest-topics$/.test(path)) {
            plan.counts.latest = (plan.counts.latest || 0) + 1;
            return json({ status: 'success', topics: [], fewshot_degraded: plan.latestDegraded });
        }
        if ((plan.distillDone || plan.eta) && /\/distill-tasks\/\d+$/.test(path)) {
            plan.counts.distillPoll = (plan.counts.distillPoll || 0) + 1;
            return json(plan.distillDone
                ? { task_id: 77, state: 'succeeded', stage: 'done', stage_label: '完成', percent: 100,
                    active: false, eta_seconds: 0, fewshot_degraded: plan.distillDone, topics: [] }
                : { task_id: 77, state: 'running', stage: 'distilling', stage_label: '正在想选题',
                    percent: 60, active: true, eta_seconds: plan.eta });
        }
        if (plan.contextIndustry !== undefined && /\/api\/client-context\/9001$/.test(path)) {
            /* 形状照 `clientContextApi.getContext` 的声明 `{success, context: ClientContextDetail}`;
               行业是**合成值**(不是任何真客户的档案) */
            return json({ success: true, context: {
                brand: { id: 9001, name: 'QA 夹具客户', brand_code: 'QA-9001',
                    industry: plan.contextIndustry, diagnosis_count: 1 },
                profile: null, materials: null, relatedQuoteIds: [], socialProjects: [] } });
        }
        /*
         * 🔴 [WO_262] OCR 核对桩。形状照 `DouyinPostDetail.tsx:136 OcrReport`
         *    与后端 `/posts/{id}/ocr-check` 的回包:`cards[].missing` 是 **string[]**。
         *    这里给**两条** missing,正是客户那张截图的形状(他看到的是被 `join('、')`
         *    拼成一行、还带着列表 repr 方括号的一串)。
         * 🔴 故意让其中一条带 `['…','…']` 那种**列表 repr**:后端归一化漏了时前端会拿到它,
         *    而前端的口径是「照原样当普通文本显示、不解析」—— 判据要钉住这一点,
         *    否则哪天有人在前端加个 JSON.parse 兜底,后端那个缺陷就永远没人看见了。
         */
        if (/\/ocr-check$/.test(path)) {
            return json({
                checked: true, reason: '', flagged_count: 1, checked_count: 3,
                cards: [
                    { card_index: 0, checked: true, ok: true, reason: '', missing: [] },
                    { card_index: 1, checked: true, ok: true, reason: '', missing: [] },
                    {
                        card_index: 3, checked: true, ok: false, reason: '文字没排全',
                        missing: ['① 先列预算表:三档报价', "['② 再按工期拆分', '③ 最后签字']"],
                    },
                ],
            });
        }
        const m = path.match(/\/api\/geo-douyin\/posts\/(\d+)$/);
        if (m) return json(POST_PAYLOAD(Number(m[1])));
        if (path.includes('/geo-douyin/pricing')) {
            /* 🔴 形状照后端 `/pricing` 的真回包:蒸馏那一档的键是 **`topic_distill`**
               (`api/geo_douyin_api.py`:`"topic_distill": distill`),不是 `distill`。
               按前端自己的印象造夹具,就会把"键名写错"这个 bug 完整盖住。 */
            /* 🔴 [WO_258] 张数与画幅这几个键也照 `/pricing` 真回包补上
               (`services/geo_douyin/config.py`:CARD_COUNT_MIN/MAX/DEFAULT = 1/9/4、
               ASPECT_RATIOS、ASPECT_RATIO_DEFAULT = 3:4)。第 1 步的报价要等
               `card_default` 到了才发 —— 缺这个键,「开始制作」永远问不到价,
               T6 会红在夹具上而不是产品上。 */
            return json({ status: 'success', first_generation: null, regenerate: null,
                redraw: null, extra_card: null, included_cards: 4, redraw_limit: 3,
                card_min: 1, card_max: 9, card_default: 4,
                aspect_ratios: [
                    { key: '3:4', label: '竖版 3:4', hint: '抖音图文默认，信息密度高' },
                    { key: '9:16', label: '全屏 9:16', hint: '竖屏满屏场景，沿用十母版那一族' },
                ],
                aspect_ratio_default: '3:4',
                ranking_templates: [],
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
        /*
         * 🔴 `WritingWorkspace` 在没选客户时渲染的是「先选一个客户」那一屏,
         *    tab 根本不出现 —— D9r 会红在夹具上而不是产品上。
         *    这里给一个 **is_test 的 QA 客户**(不是真客户),让页面进到有 tab 的那一态。
         *    形状照 `clientContextApi.list` 的声明:`{success, clients: ClientBrandSummary[]}`。
         */
        if (/\/api\/client-context\/list$/.test(path)) {
            return json({ success: true, clients: [{
                id: 9001, name: 'QA 夹具客户', brand_code: 'QA-9001', industry: '测试行业',
                diagnosis_count: 1, quote_count: 0, quote_status: null,
                brand_status: 'active', is_test: true,
            }] });
        }
        /*
         * 🔴 客户关键词(面板默认来源,0913P 起):形状照 `services/geo_douyin/keyword_worklist.py`
         *    的 SELECT 别名 + `production_status` 派生,外层 `{status:'success', keywords, total, limit, offset}`
         *    (`api/geo_douyin_api.py::api_client_keywords` with_production=True 那一支)。
         *    面板读 `d.status === 'success'` 与 `d.keywords`;缺这一桩它会落进兜底 `{status:'success'}`,
         *    `d.keywords` 不是数组 ⇒ 整块报错、一行都不渲染 —— 2026-09-22 前本臂就是这样红着的。
         *    三行与 `plan.topics` 一一对应:待做 / 制作中(has_active_task)/ 已完成。
         */
        if (/\/clients\/\d+\/keywords$/.test(path)) {
            plan.counts.keywords = (plan.counts.keywords || 0) + 1;
            const kw = plan.keywords || [
                { confirmed_keyword_id: 501, keyword: '关键词A', required_articles: 3,
                    quote_id: 88, quote_status: 'paid', quote_city: '广州',
                    post_id: null, post_status: null, post_title: null, style_key: null, post_city: null,
                    publish_status: null, has_active_task: false,
                    topic_id: 1, topic_status: 'pending', topic_title: '待做的第一条',
                    produced_count: 0, production_status: 'pending' },
                { confirmed_keyword_id: 502, keyword: '关键词B', required_articles: 3,
                    quote_id: 88, quote_status: 'paid', quote_city: '深圳',
                    post_id: 40, post_status: 'generating', post_title: '正在做的那条', style_key: 'memo', post_city: '深圳',
                    publish_status: null, has_active_task: true,
                    topic_id: null, topic_status: 'making', topic_title: '正在做的那条',
                    produced_count: 0, production_status: 'making' },
                { confirmed_keyword_id: 503, keyword: '关键词C', required_articles: 3,
                    quote_id: 88, quote_status: 'paid', quote_city: '佛山',
                    post_id: 41, post_status: 'ready', post_title: '做好了的那条', style_key: 'memo', post_city: '佛山',
                    publish_status: null, has_active_task: false,
                    topic_id: null, topic_status: null, topic_title: null,
                    produced_count: 1, production_status: 'done' },
            ];
            return json({ status: 'success', keywords: kw, total: kw.length, limit: 200, offset: 0 });
        }
        /* ── #204 a1 的桩:选题 / 两处价 / 蒸馏进度 ──
         * 🔴 [WO_258] `/clients/{id}/plan` 的桩撤了:0913P(019ec0f55)删掉了读它的那一句,
         *    全前端不再读 /plan。留着一个没人读的桩,等于替一个不存在的功能作证。 */
        if (/\/clients\/\d+\/topics$/.test(path) && route.request().method() === 'GET') {
            plan.counts.topics = (plan.counts.topics || 0) + 1;
            /* 🔴 [WO_258] 形状照 `api_list_topics` = `{status:'success', **list_topics()}`
               (`db/geo_douyin_db.py::list_topics` 回 `{topics, total}`)。面板现在按
               `status === 'success'` 判成功 —— 缺了它整块报「没取到」、一行都不渲染。 */
            const topics = plan.topics || [];
            return json({ status: 'success', topics, total: topics.length });
        }
        if (path.includes('/geo-douyin/production-quote')) {
            /* 🔴 [WO_258] 形状照 `services/geo_douyin/production_quote.py::quote_production`:
               逐行 `price_fingerprint`,`lines` 与请求逐行对齐。面板拿 `lines.length`
               对不上请求行数就**不认这次报价**(否则指纹会张冠李戴)—— 旧桩只给
               `total_points`,于是价永远不上屏。每行 390,前端只显示 `total_points`。 */
            let lines = [];
            try { lines = JSON.parse(route.request().postData() || '{}').lines || []; } catch { lines = []; }
            const out = lines.map((l, i) => ({ keyword: l.keyword || '', city: l.city || '',
                card_count: l.card_count, extra_cards: 0, base_points: 390, extra_points: 0,
                line_points: 390, price_fingerprint: `qa-fp-${i}` }));
            return json({ status: 'success', lines: out, total_points: 390 * out.length,
                price_fingerprint: 'qa-fp-batch' });
        }
        if (path.includes('/geo-douyin/distill-topics')) {
            /* 形状照 `api_distill_topics` 的受理回包 `{status:'accepted', task_id, …}` */
            try { plan.distillBody = JSON.parse(route.request().postData() || '{}'); } catch { plan.distillBody = null; }
            return json({ status: 'accepted', task_id: 77 });
        }
        if (/\/distill-tasks\/\d+$/.test(path)) {
            /* 🔴 服务端给什么前端就显示什么 —— 这里故意给一个"半路"的数,
               前端若自己涨百分比,读数就不会是 37。 */
            return json({ task_id: 77, state: 'running', stage: 'gathering',
                stage_label: '正在找素材', percent: 37, active: true });
        }
        if (/\/geo-douyin\/topics\/\d+$/.test(path) && route.request().method() === 'PATCH') {
            plan.patched = true;
            /* 改完之后列表回的是**改后的**标题 —— 前端必须重新读回才看得到 */
            plan.topics = (plan.topics || []).map((t) => (t.id === 1
                ? { ...t, title: '改过的标题', source: 'user' } : t));
            /* 形状照 `api_patch_topic` = `{status:'success', topic}` */
            return json({ status: 'success', topic: plan.topics.find((t) => t.id === 1) || null });
        }
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
    const ENTRY = entry || `/writing/image-note/${postId}`;
    await page.evaluate((e) => globalThis.__mount(document.getElementById('root'), e, 1), ENTRY);
    await page.waitForTimeout(1200);
    /* 换 key 重挂一次:第一发请求可能赶在鉴权确认之前(harness 特有,不是产品缺陷)。
       同一容器第二次 createRoot 会变成两个 root,读数就成了抛硬币。 */
    await page.evaluate((e) => globalThis.__mount(document.getElementById('root'), e, 2), ENTRY);
    await page.locator('[data-testid="detail-three-cols"]').first()
        .waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
    await page.waitForTimeout(800);
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

const browser = await playwright.chromium.launch();
try {
    console.log('D #203 行为臂');
    {
        const page = await openDetail(browser, { width: 1600, postId: 41 });

        /* ── D2 真形状:标题 / 正文 / 大图 / 手机预览 / 去发布投放 全部看得见 ── */
        const cols = await visible(page, '[data-testid="detail-three-cols"]');
        check(cols.found && cols.vis, 'D2a 三栏栅格挂起来了(挂不起来下面都量不了)',
            cols.found ? `${cols.w}×${cols.h}` : '(没有)');

        const t = await visible(page, '[data-testid="title-input"]');
        check(t.found && t.vis && t.value.includes('QA 夹具标题'),
            'D2 🔴 标题来自回包的 `post.title` 并**显示在屏幕上**', t.value || '(空)');
        const b = await visible(page, '[data-testid="body-input"]');
        check(b.found && b.vis && b.value.includes('QA 夹具正文'),
            'D2b 🔴 正文来自 `post.body_text`(旧壳子读的 `bodyText` 库里没有,所以必然空)',
            b.value || '(空)');
        const phone = await visible(page, '[data-testid="phone-frame"]');
        check(phone.found && phone.vis, 'D2c 手机预览看得见', `${phone.w}×${phone.h}`);
        const phoneTitle = await visible(page, '[data-testid="phone-title"]');
        check(phoneTitle.found && phoneTitle.text.includes('QA 夹具标题'),
            'D2d 🔴 预览读的是**同一份**文案 state(改右栏 → 中栏跟着变的前提)',
            phoneTitle.text);
        const big = await page.evaluate(() => {
            const imgs = [...document.querySelectorAll('img')]
                .filter((i) => i.getBoundingClientRect().width > 120);
            return { n: imgs.length, w: imgs[0] ? Math.round(imgs[0].getBoundingClientRect().width) : 0 };
        });
        check(big.n >= 1, 'D2e 🔴 大图用的是 `preview_urls[0]`(签名链)——'
            + '旧壳子读 `cards[].image_url`,于是永远是「这张图还没生成」', `${big.n} 张 / 首张 ${big.w}px`);
        const go = await visible(page, '[data-testid="douyin-goto-publish"]');
        check(go.found && go.vis && go.text.includes('去发布投放'),
            'D2f 🔴 「去发布投放」看得见(Owner 09-13 点名要回来的就是这一屏)', go.text);

        /* ── D7 宽档**并排**(宽档)──
         * 🔴 [WO_258 改锚] 原命题「三栏 设置 → 预览 → 重要内容」(#203 §4)。
         *    0913P a9ab52b8e 把「设置」一列(选题 / 风格 / 生成)整列搬去工作台第 1 步,
         *    详情页传 `showTopics={false}`,栅格改成 `@3xl` 起两栏 ⇒ 三栏变三步。
         *    命题在新形态下仍要守的两件事:两栏并排且顺序不乱;设置列**不**在详情页
         *    (在的话就是同一块设置两个入口,只有一个被判据守着)。 */
        const layout = await page.evaluate(() => {
            const g = (t) => document.querySelector(`[data-testid="${t}"]`);
            const r = (t) => { const e = g(t); return e ? e.getBoundingClientRect() : null; };
            const c = r('detail-col-preview'); const d = r('detail-col-content');
            const settings = !!g('detail-col-settings');
            if (!c || !d) return { ok: false, why: 'missing', settings };
            return {
                ok: true, settings,
                sameRow: Math.abs(c.top - d.top) < 40,
                order: c.left < d.left,
                lefts: [Math.round(c.left), Math.round(d.left)],
                tops: [Math.round(c.top), Math.round(d.top)],
            };
        });
        check(layout.ok && layout.sameRow && layout.order && !layout.settings,
            'D7 🔴 宽档两栏**并排**且顺序是 预览 → 重要内容;设置列不在详情页(已搬去第 1 步)',
            layout.ok ? `left=${layout.lefts} top=${layout.tops} 设置列=${layout.settings ? '还在' : '无'}`
                : `${layout.why} 设置列=${layout.settings ? '还在' : '无'}`);

        /* ── D6 上一条/下一条:夹具 siblings 3 条 ── */
        const prev = await page.locator('[data-testid="post-prev"]');
        const next = await page.locator('[data-testid="post-next"]');
        const prevOn = await prev.isEnabled().catch(() => false);
        const nextOn = await next.isEnabled().catch(() => false);
        check(await prev.count() === 1 && await next.count() === 1,
            'D6a 上一条/下一条都在',
            /* 读数与判定同源:第一版这里印的是**卡片计数**(stage-counter),
               和这一格判的翻页根本不是一件事 —— 一个自说自话的读数。 */
            `prev=${prevOn ? '可点' : '灰'} next=${nextOn ? '可点' : '灰'}`);
        check(prevOn && nextOn,
            'D6 🔴 夹具 siblings 3 条、当前是**中间**那条(41)⇒ 上一条/下一条**都可点**'
            + '(容器没把顺序传回去的话 orderIds 只剩自己一条,两边恒灰)',
            `prev=${prevOn} next=${nextOn}`);
        await page.close();
    }
    {
        /* ── D7b 窄档:改为顺序纵排 ──
         * 🔴 [WO_258 改锚] 两栏的断点是容器 `@3xl`(48rem = 768px),不再是三栏时的 `@5xl`。
         *    原来的 900px 视口在两栏下装得下并排 —— 用它量「纵排」会红在尺子上。
         *    取 760px:页面 `sm:p-6` 两侧各 24px ⇒ 容器约 712px < 768,落在窄档里。 */
        const page = await openDetail(browser, { width: 760, postId: 41 });
        const stacked = await page.evaluate(() => {
            const r = (t) => { const e = document.querySelector(`[data-testid="${t}"]`); return e ? e.getBoundingClientRect() : null; };
            const grid = r('detail-three-cols');
            const c = r('detail-col-preview'); const d = r('detail-col-content');
            if (!c || !d) return { ok: false };
            return {
                ok: true,
                stacked: c.bottom <= d.top + 8,
                tops: [Math.round(c.top), Math.round(d.top)],
                gridW: grid ? Math.round(grid.width) : 0,
            };
        });
        check(stacked.ok && stacked.stacked,
            'D7b 🔴 窄档(760px)两栏**顺序纵排**,顺序仍是 预览 → 重要内容',
            stacked.ok ? `top=${stacked.tops} 栅格宽=${stacked.gridW}` : '(取不到)');
        await page.close();
    }
    {
        /*
         * ══ D3r 🔴 [a2] brand_id 钉在**跳转目标**上 ════════════════════
         *
         * Review 复跑把 `${b}` 从 navigate 的模板里拿掉、只留下 `const b = …`
         * 那行死代码 ⇒ 结构臂 33/33 全绿。**出现过 ≠ 被用上。**
         * 这里不再问源码长什么样,直接点那颗按钮,量**真的导航到了哪**。
         * 夹具 brand_id = 629(按规矩按 id 引用,不写名字)。
         */
        const page = await openDetail(browser, { width: 1600, postId: 41 });
        const before = await page.evaluate(() => String(globalThis.__loc || ''));
        await page.locator('[data-testid="douyin-goto-publish"]').click();
        await page.waitForTimeout(600);
        const loc = await page.evaluate(() => String(globalThis.__loc || ''));
        const target = new URL(loc, 'http://localhost');
        check(target.pathname === '/publish' && target.searchParams.get('media_type') === 'svideo'
            && target.searchParams.get('content_type') === 'imagenote', 'D3r0 进入中央短视频图文入口', loc);
        check(target.searchParams.get('geo_post_id') === '41' && target.searchParams.get('brand_id') === '629',
            'D3r2 当前作品和权威客户一起交接');
        await page.close();
    }
    {
        /* ══ D9r 🔴 [a2 重判] 老链 `?tab=douyin` 落到新屏上 ══════════════
         *
         * 原来这两格钉的是「图文 tab 在页面上 / 是选中的」—— 那是**位置**。
         * 本单撤了那个 tab(Owner「唯一一屏」),两格的指称对象一起没了。
         * 命题换成了更该守的那件事:**老链接不许落空**。
         * 它已经发出去过(#186 那批 + 用户收藏夹),不重定向的话它会静默
         * 落在「写文章」上 —— 页面正常、内容不是他要的,比 404 更难发现。
         */
        const page = await openDetail(browser, { width: 1440, entry: '/writing?tab=douyin' });
        const after = await page.evaluate(() => ({
            loc: String(globalThis.__loc || ''),
            hasOldTab: !!document.querySelector('[data-testid="writing-tab-douyin"]'),
            hasEntry: !!document.querySelector('[data-testid="writing-goto-image-note"]'),
        }));
        check(after.loc.startsWith('/writing/image-note'),
            'D9r 🔴 老链 `?tab=douyin` **被送到图文那条路由** —— 不送的话它静默落在'
            + '「写文章」上:页面看着正常,内容不是他要的那一条', after.loc || '(没动)');
        check(!after.hasOldTab,
            'D9r0 那个 tab 真的不在了(只改跳转、tab 还挂着的话,'
            + '等于同一块内容两个入口,而只有一个被判据守着)');
        await page.close();
        /* 🔴 入口要在 `/writing` **本身**上查:上面那一跳之后人已经在图文屏了,
           写作中心压根没渲染 —— 在那里找入口,量的是另一个页面。 */
        const wsPage = await openDetail(browser, { width: 1440, entry: '/writing' });
        const entry = await wsPage.evaluate(() => {
            const e = document.querySelector('[data-testid="writing-goto-image-note"]');
            return { found: !!e, href: e ? (e.getAttribute('href') || '') : '' };
        });
        check(entry.found && entry.href.includes('/writing/image-note'),
            'D9r2 🔴 撤了 tab,**入口没跟着撤**:`/writing` 上仍有一步可达图文的那一处',
            entry.found ? entry.href : '(没有)');
        await wsPage.close();
    }
    /*
     * ══ T #204 a1 → [WO_258] 工作台第 1 步(真浏览器)══════════════════════
     *
     * 🔴 本段原来开在详情页 `/writing/image-note/41` 的左栏上。0913P 把选题面板搬去了
     *    工作台第 1 步:a9ab52b8e 让详情页传 `showTopics={false}`(左栏整列不挂);
     *    019ec0f55 把默认来源改成「客户关键词」、chip 加「全部」并默认停在它、
     *    「生成选题」收进「让 AI 帮我想新选题(可选)」折叠框、删掉顶栏 /plan 一句话。
     *    ⇒ 这一段红的是**锚**,不是产品(逐格定性见 WO_258 交付单)。
     *
     * T1 撤于 WO_258,产品 0913P 删了这块:顶栏「买了 / 已做 / 还差」一句话与
     *    `/clients/{id}/plan` 的读取由 019ec0f55 删除,全前端不再读 /plan。
     *    (「前端不自己算配额」那半条命题仍有人守:tests/test_geo_douyin_detail_ui.py
     *     的 `test_create_flow_does_not_compute_its_own_quota` 扫图文创作流全部文件。)
     */
    const readPanel = (page) => page.evaluate(() => {
        const g = (t) => document.querySelector(`[data-testid="${t}"]`);
        const txt = (t) => { const e = g(t); return e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : ''; };
        const review = g('topics-production-review');
        return {
            distill: txt('topics-distill'),
            start: txt('topics-start'),
            startDisabled: g('topics-start') ? g('topics-start').disabled : null,
            startHint: txt('topics-start-hint'),
            chips: [...document.querySelectorAll('[data-testid="topics-chip"]')]
                .map((e) => `${e.getAttribute('data-chip')}=${e.getAttribute('data-chip-count')}`),
            rows: [...document.querySelectorAll('[data-testid="topic-row"]')]
                .map((e) => e.getAttribute('data-topic-status')),
            samples: document.querySelectorAll('[data-testid="topic-style-sample"]').length,
            /* 🔴 量**渲染后的实际尺寸**,不是"元素在不在"。
               28px 的色块也满足"矩形非零",可它分不出「设计文字卡」和「备忘录体」。 */
            sampleBox: (() => {
                const e = g('topic-style-sample');
                if (!e) return { w: 0, h: 0 };
                const r = e.getBoundingClientRect();
                return { w: Math.round(r.width), h: Math.round(r.height) };
            })(),
            styleNames: [...document.querySelectorAll('[data-testid="topic-style-name"]')]
                .map((e) => (e.textContent || '').trim()),
            /* 确认页里那个选择器:每一款是一颗带 aria-pressed 的按钮,样图是它里面的 img */
            pickerOptions: review ? review.querySelectorAll('button[aria-pressed]').length : 0,
            pickerSamples: review ? review.querySelectorAll('img[data-testid^="style-sample-"]').length : 0,
            title1: txt('topic-title'),
        };
    });
    /*
     * 进工作台第 1 步,像用户一样在「先选一位客户」下拉里选夹具客户 9001(与 T14 同一条路)。
     * 🔴 等的是**面板根节点** —— 与下面任何一格的断言都无关、真假都在的稳定锚;
     *    不等被断言的行 / chip,否则毒下去那几格是整格消失,不是翻红。
     */
    const openWorkspace = async (plan) => {
        plan.width = plan.width || 1600;
        plan.entry = '/writing/image-note';
        const page = await openDetail(browser, plan);
        await page.waitForTimeout(400);
        await page.evaluate(() => {
            const sel = document.querySelector('select[aria-label="选择制作图文的客户"]');
            if (sel) {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
                setter.call(sel, '9001');
                sel.dispatchEvent(new Event('change', { bubbles: true }));
            }
        });
        await page.locator('[data-testid="image-note-topic-panel"]').first()
            .waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
        await page.waitForTimeout(1200);
        return page;
    };
    const clickSel = (page, sel) => page.evaluate((s) => {
        const e = document.querySelector(s);
        if (e) e.click();
        return !!e;
    }, sel);
    {
        /* ── 第 1 步 · 默认来源「客户关键词」(桩三行:待做 / 制作中 / 已完成)── */
        const page = await openWorkspace({});
        const v = await readPanel(page);
        check(v.chips.join(',') === 'all=3,pending=1,making=1,done=1,failed=0',
            'T3 三态 chip 计数常驻(0 也显示;0913P 起多一档「全部」)', v.chips.join(','));
        check([...v.rows].sort().join(',') === 'done,making,pending',
            'T4 默认停在「全部」那一档(0913P 起;原来停在「待做」)—— 三行都在,没有被哪一档藏起来',
            v.rows.join(','));
        /*
         * 🔴 T10 [WO_258 改锚] 原命题「灰着必须带一句原因」钉在详情页右栏编辑区
         *    (A6:选中还没做出来的选题时锁编辑)。详情页里已经选不了别的选题,那一态没了;
         *    第 1 步里灰着的是「下一步:制作图文」—— 同一条命题落在它身上。
         */
        check(v.startDisabled === true && v.startHint.length > 0,
            'T10 🔴 什么都没勾时「下一步」灰着**且有一句原因** —— '
            + '灰着且一个字不说,和坏了长得一模一样', `${v.startDisabled ? '灰' : '可点'} · ${v.startHint || '(空)'}`);

        /*
         * 🔴 T9 [WO_258 改锚] 原命题 A6:选中还没做出来的选题时,不许把当前这篇的图
         *    当成它的来显示(张冠李戴)。新形态下详情页不挂选题;在第 1 步点一条没做出来的,
         *    应当**只是勾上**,人还在第 1 步 —— 不被带进任何一篇作品的详情。
         */
        await clickSel(page, '[data-testid="topic-row"][data-topic-status="pending"]');
        await page.waitForTimeout(700);
        const picked = await page.evaluate(() => {
            const r = document.querySelector('[data-testid="topic-row"][data-topic-status="pending"]');
            return {
                checked: r ? r.getAttribute('aria-checked') : '(没有)',
                loc: String(globalThis.__loc || ''),
                editor: !!document.querySelector('[data-testid="copy-editor-card"]'),
            };
        });
        check(picked.checked === 'true' && picked.loc === '/writing/image-note' && !picked.editor,
            'T9 🔴 点一条还没做出来的选题 ⇒ 只是勾上,人还在第 1 步(不被带进任何一篇作品的详情)',
            `checked=${picked.checked} loc=${picked.loc} 详情编辑区=${picked.editor ? '出现了' : '无'}`);

        /* 勾选 ⇒「下一步」⇒ 确认页:按钮带数量与价 */
        await page.waitForTimeout(600);
        await clickSel(page, '[data-testid="topics-start"]');
        await page.waitForTimeout(900);
        const review = await readPanel(page);
        check(review.start.includes('(1)') && review.start.includes('390 算力'),
            'T6 🔴 勾选后确认页「开始制作 (N) · X 算力」——价来自 production-quote,前端不算',
            review.start || '(没有)');
        /*
         * 🔴 T11:「卡面风格」每一款都要带出样图。
         *    这一格挡的是一整类事故:后端给的 key 与前端 `STYLE_SAMPLES` 对不上时,
         *    整块**退化成裸按钮**(Owner 两次点名不许出现的样子),而且**不报错**。
         *    我自己的夹具就栽在这里:编了 clean/rich/photo 三个不存在的 key,
         *    于是交付截图里一张样图都没有 —— 那是关于另一个世界的证据。
         *    [WO_258] 选择器从详情页左栏搬到了确认页(第一次做就能选风格的那个入口)。
         */
        check(review.pickerSamples > 0 && review.pickerOptions === review.pickerSamples,
            'T11 🔴 确认页的风格选择器**每一款都带出样图** —— key 与样图表对不上时会静默变成裸按钮',
            `${review.pickerSamples} 款有样图 / ${review.pickerOptions} 款`);
        await page.close();
    }
    {
        /*
         * ── 第 1 步 · 「自选题目 / AI 灵感」来源 ──
         * 🔴 [WO_258] 改标题、生成选题、行内风格样图都只在这个来源下有:
         *    客户关键词那几行是 `fromKeyword`、`editable=false`、不带风格。
         * 🔴 夹具**故意不放「制作中」**:有制作中的行时每 4 秒一次心跳重读列表,
         *    T8 靠「读取次数」分辨读回与本地回显 —— 心跳会替没读回的那一版作证。
         */
        const planIdeas = { topics: [
            { id: 1, title: '待做的第一条', keyword: '关键词A', city: '广州',
                style_key: 'design_text', status: 'pending', source: 'distilled' },
            { id: 3, title: '做好了的那条', keyword: '关键词C', city: '佛山',
                style_key: 'memo', status: 'done', post_id: 41, source: 'user' },
        ] };
        const page = await openWorkspace(planIdeas);
        await page.getByRole('button', { name: '自选题目 / AI 灵感' }).click();
        await page.waitForTimeout(900);
        await clickSel(page, '[data-testid="topics-chip"][data-chip="pending"]');
        await page.waitForTimeout(400);
        const v = await readPanel(page);
        check(v.samples === 1, 'T5 🔴 每行风格**带可见样图**(不是只给风格名)', `${v.samples} 张`);
        /*
         * 🔴 T5b/T5c 是截图复看时补的:第一版只数"有没有这个元素",
         *    而实际渲染出来的样图只有 36×28 —— 那个尺寸**分不出**
         *    「设计文字卡」和「备忘录体」,旁边又只写着一句笼统的
         *    「这一条的卡面风格」不写名字。判据绿着,用户什么也没看出来。
         */
        check(v.sampleBox.w >= 40 && v.sampleBox.h >= 52,
            'T5b 🔴 样图**大到看得出形态**(缩回一个小色块就该红)',
            `${v.sampleBox.w}×${v.sampleBox.h}`);
        check(v.styleNames.length === 1 && /设计文字卡|表格测评卡|备忘录体|实拍叠字/.test(v.styleNames[0]),
            'T5c 🔴 行上写着**这一款的真名**(名字来自后端那份 styles,前端不另写一表)',
            v.styleNames.join(' / ') || '(没有)');

        /* 「生成选题」收在折叠框里 —— 像用户一样先展开 */
        await page.locator('summary', { hasText: '让 AI 帮我想新选题' }).first().click().catch(() => { });
        await page.waitForTimeout(400);
        const v2 = await readPanel(page);
        check(v2.distill.includes('生成选题') && v2.distill.includes('130 算力'),
            'T2 🔴 「生成选题」**带价**,价来自服务端', v2.distill || '(没有)');
        /*
         * 🔴 T13 迁自已退役的 `verify-image-note-usable.mjs` S6 那条命题。
         *    老判据钉的是**类名** `w-full` —— 那是位置。Owner 09-13 骂的是截图里
         *    那条 **1432px 跨屏长条**(「视线落点上没有内容」),不是 w-full 本身:
         *    同一个 w-full 在 370px 的左栏里是对的排版。
         *    ⇒ 钉**渲染出来的宽度**,不钉类名。
         * 🔴 [WO_269 的锁 · Review 09-23 裁「算回归」] 也不钉像素常量(上一版的 ≤520px):
         *    取**同一容器、同一时刻**里那颗主按钮(「下一步 / 开始制作」)做对照 ——
         *    两颗同级(宽的不超过窄的两倍),且谁都不许铺满面板(各 ≤ 面板一半)。
         *    0913P 起面板从 370px 左栏搬到整页宽,019ec0f55 改了主按钮的宽、漏了这一颗。
         */
        const widths = await page.evaluate(() => {
            const w = (s) => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().width) : 0; };
            return { distill: w('[data-testid="topics-distill"]'), start: w('[data-testid="topics-start"]'),
                panel: w('[data-testid="image-note-topic-panel"]') };
        });
        const sameLevel = widths.distill > 0 && widths.start > 0
            && Math.max(widths.distill, widths.start) <= 2 * Math.min(widths.distill, widths.start);
        const noStrip = widths.panel > 0 && widths.distill <= widths.panel / 2 && widths.start <= widths.panel / 2;
        check(sameLevel && noStrip,
            'T13 🔴 「生成选题」与同一面板里的主按钮同级,谁都不铺满面板'
            + '(Owner 09-13:那条 1432px 的横条,视线落点上没有内容)',
            `生成选题 ${widths.distill}px · 主按钮 ${widths.start}px · 面板 ${widths.panel}px`);

        /* 进度:服务端说 37% 就是 37% */
        await clickSel(page, '[data-testid="topics-distill"]');
        await page.waitForTimeout(900);
        const pct = await page.evaluate(() => {
            const e = document.querySelector('[data-testid="topics-distill-bar"]');
            return e ? e.getAttribute('data-percent') : '';
        });
        check(pct === '37',
            'T7 🔴 进度条读数**等于服务端给的 37**(前端自己涨的话这里不会是 37)', pct || '(没有)');

        /* 改标题 ⇒ 重新读回 */
        await clickSel(page, '[data-testid="topic-title-edit"]');
        await page.waitForTimeout(200);
        await page.evaluate(() => {
            const i = document.querySelector('[data-testid="topic-title-input"]');
            if (i) {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                setter.call(i, '改过的标题');
                i.dispatchEvent(new Event('input', { bubbles: true }));
            }
        });
        /*
         * 🔴 存盘**前**先记一次列表读取次数。
         *    这一格是注毒 S2 逼出来的:第一版只断言"屏幕上是改后的标题",
         *    而毒把重新读回换成 `setRows(… title: editingTitle)` ——
         *    `editingTitle` 正是刚敲进去的那串,**屏幕上的字一模一样**,
         *    于是毒落地了这一格照样绿(判据名声称了它没在查的事)。
         *    「只改本地 state」与「读回」唯一可观测的差别,就是
         *    **列表到底有没有再读一遍**,所以断言必须落在读取次数上。
         */
        const topicsReadBefore = planIdeas.counts.topics || 0;
        await clickSel(page, '[data-testid="topic-title-save"]');
        await page.waitForTimeout(900);
        const afterEdit = await readPanel(page);
        check(planIdeas.patched === true, 'T8a 真的发了 PATCH');
        check((planIdeas.counts.topics || 0) > topicsReadBefore,
            'T8 🔴 存盘成功后**列表被重新读了一遍** —— 只改本地 state 的话'
            + '屏幕上的字同样是对的,能分辨两者的只有这个读取次数',
            `${topicsReadBefore} → ${planIdeas.counts.topics || 0}`);
        check(afterEdit.title1.includes('改过的标题'),
            'T8b 屏幕上是改后的标题(这一格分不出读回还是本地回显,'
            + '分辨那件事由 T8 管)', afterEdit.title1);
        await page.close();
    }
    {
        /*
         * 🔴 T12:「当前风格」必须**真的是选择器里的那一款**。
         *    post 回包的 style.key 与 styles 列表对不上时,顶栏照样写着
         *    「当前风格:X」,而下面**没有一款是选中的** —— 用户看不出
         *    自己现在用的是哪一款,换风格时也不知道在跟什么比。
         *    (我的夹具编了 `clean 清爽` 时就是这个样子,截图上看不出来。)
         * [WO_258 改锚] 详情页左栏那个选择器随整列一起不挂了;同一个组件现在出现在
         *    「再次创作」弹窗里,开弹窗时预选的正是这一篇当前的风格
         *    (`setRegenStyle(data.style?.key)`)。等的是弹窗,不是被断言的按钮。
         */
        const page = await openDetail(browser, { width: 1600, postId: 41 });
        await clickSel(page, '[data-testid="regen-open"]');
        await page.waitForSelector('[role="dialog"]', { timeout: 10000 }).catch(() => { });
        await page.waitForTimeout(300);
        const pressed = await page.evaluate(() => [...document.querySelectorAll(
            '[role="dialog"] button[aria-pressed="true"]')].map((e) => (e.getAttribute('aria-label') || '').trim()));
        check(pressed.length === 1 && pressed[0] === '设计文字卡',
            'T12 🔴 「再次创作」的选择器里**恰好一款被标为当前**,且就是这一篇的风格 —— '
            + '一款都没选中说明当前风格不在这份列表里(顶栏却照样写着它)', pressed.join(',') || '(一款都没有)');
        await page.close();
    }
    {
        /* ══ T14 制作中的心跳**静默**刷新(Owner 2026-09-22:「心跳一直在前端刷新…一直看着它闪」)══
         *
         * 根因:心跳与首读走同一条 `load`,每 4 秒 `setLoading(true)`,列表整块换成
         * 「正在读取内容和制作进度…」再换回来。这一格量的是**屏幕**:9 秒(≥2 跳)内
         * 占位符出现的次数必须是 0,而且要先证明心跳真的跳了(正控:读取次数 ≥ +2),
         * 否则「没闪」可能只是「没跳」。观察者在列表已渲染之后才装,首读那一次不算。
         */
        /*
         * 🔴 0913P 起选题面板只在 `/writing/image-note`(无 id,第 1 步)上挂,
         *    详情页 `/writing/image-note/:id` 传的是 `showTopics={false}`。
         *    所以本臂从工作台入口进,并像用户一样在「先选一位客户」下拉里选夹具客户 9001
         *    (`switchClient` 同步置 currentBrandId,面板随即挂载)。
         */
        const plan14 = { width: 1600, entry: '/writing/image-note' };
        const page = await openDetail(browser, plan14);
        await page.waitForTimeout(400);
        await page.evaluate(() => {
            const sel = document.querySelector('select[aria-label="选择制作图文的客户"]');
            if (sel) {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLSelectElement.prototype, 'value').set;
                setter.call(sel, '9001');
                sel.dispatchEvent(new Event('change', { bubbles: true }));
            }
        });
        await page.waitForTimeout(1500);
        const before = await page.evaluate(() => {
            const w = window;
            w.__loadingSeen = 0;
            const mo = new MutationObserver(() => {
                if (document.querySelector('[data-testid="topics-loading"]')) w.__loadingSeen += 1;
            });
            mo.observe(document.body, { childList: true, subtree: true });
            const chip = document.querySelector('[data-testid="topics-chip"][data-chip="making"]');
            return {
                rows: document.querySelectorAll('[data-testid="topic-row"]').length,
                making: chip ? chip.getAttribute('data-chip-count') : '(没有)',
                loadingNow: !!document.querySelector('[data-testid="topics-loading"]'),
                /* 前提不成立时把屏幕上有什么说出来 —— 「0 行」和「面板根本没挂」同形 */
                screen: [...new Set([...document.querySelectorAll('[data-testid]')]
                    .map((e) => e.getAttribute('data-testid')))].slice(0, 24).join(' '),
                text: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 160),
            };
        });
        const readsBefore = plan14.counts.keywords || 0;
        await page.waitForTimeout(9000);
        const after = await page.evaluate(() => ({
            rows: document.querySelectorAll('[data-testid="topic-row"]').length,
            loadingSeen: window.__loadingSeen,
            loadingNow: !!document.querySelector('[data-testid="topics-loading"]'),
            stalled: !!document.querySelector('[data-testid="topics-refresh-stalled"]'),
        }));
        const readsAfter = plan14.counts.keywords || 0;
        check(before.rows === 3 && before.making === '1' && !before.loadingNow,
            'T14a 前提:列表 3 行已渲染、「制作中」计 1、占位符不在(否则心跳不起,下面的 0 是空话)',
            `rows=${before.rows} making=${before.making} loadingNow=${before.loadingNow}`
            + (before.rows === 3 ? '' : ` · 屏上:[${before.screen}] 「${before.text}」`));
        check(readsAfter >= readsBefore + 2,
            'T14b 🔴 正控:9 秒内心跳真的重读了列表 ≥2 次(没读的话"没闪"只是因为没跳)',
            `${readsBefore} → ${readsAfter}`);
        check(after.loadingSeen === 0 && !after.loadingNow,
            'T14 🔴 心跳期间「正在读取内容和制作进度…」占位符**一次都没出现**(出现即 Owner 看到的闪)',
            `seen=${after.loadingSeen} now=${after.loadingNow}`);
        check(after.rows === before.rows && !after.stalled,
            'T14c 行数不变、没有误报「没刷新到」(桩每次都 200,出声就是记错了 miss)',
            `${before.rows} → ${after.rows} stalled=${after.stalled}`);
        await page.close();
    }
    /* ══ WO_282 · 09-15 改写丢掉的两件事(F4 出错分支的人话 / F1 蒸馏带行业)══════════
     * 每格先放一条正控(请求真的发出去了、真的被断 / 回了 HTML),
     * 否则「屏幕上没有原话」与「那条路根本没走到」读数同形。
     */
    {
        /* F4a:勾一条 ⇒ 下一步 ⇒ 开始制作,那一跳真断网 */
        const planF4 = { batchNetworkFail: true };
        const page = await openWorkspace(planF4);
        await clickSel(page, '[data-testid="topic-row"][data-topic-status="pending"]');
        await page.waitForTimeout(600);
        await clickSel(page, '[data-testid="topics-start"]');      // 下一步 ⇒ 确认页
        await page.waitForTimeout(1200);
        await clickSel(page, '[data-testid="topics-start"]');      // 开始制作 ⇒ posts/batch
        await page.waitForTimeout(1500);
        const err = await page.evaluate(() => {
            const e = document.querySelector('[data-testid="topics-error"]');
            return e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : '';
        });
        check((planF4.counts.batchAborted || 0) >= 1,
            'F4a0 正控:「开始制作」的整批请求真的发出去并被断网了', `${planF4.counts.batchAborted || 0} 次`);
        check(err.includes('网络不通') && !/Failed to fetch|TypeError/i.test(err),
            'F4a 🔴 断网时屏幕上是人话「网络不通…」,没有浏览器原话 Failed to fetch', err || '(没有报错块)');
        await page.close();
    }
    {
        /* F4b:工作台路由读「已经制作的图文」,网关回 HTML 502 */
        const planF4b = { postsListHtml502: true };
        const page = await openWorkspace(planF4b);
        await page.waitForTimeout(600);
        const err = await page.evaluate(() => {
            const e = document.querySelector('[data-testid="image-note-load-failed"]');
            return e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : '';
        });
        check((planF4b.counts.postsList || 0) >= 1,
            'F4b0 正控:作品列表那一跳真的发出去并拿到了 HTML 502', `${planF4b.counts.postsList || 0} 次`);
        check(err.includes('已有图文没取到') && !/Unexpected token|JSON|<html/i.test(err),
            'F4b 🔴 HTML 错误页不会变成解析器原话上屏(Owner 截图那一种)', err || '(没有报错块)');
        await page.close();
    }
    /* F1:「生成选题」的请求体带客户档案的行业原文;换一份档案,值跟着换(对照臂:不是写死的常量) */
    for (const [id, industry] of [['F1a', '测试行业甲'], ['F1b', '测试行业乙']]) {
        const planF1 = { contextIndustry: industry, topics: [
            { id: 1, title: '待做的第一条', keyword: '关键词A', city: '广州',
                style_key: 'design_text', status: 'pending', source: 'distilled' },
        ] };
        const page = await openWorkspace(planF1);
        await page.getByRole('button', { name: '自选题目 / AI 灵感' }).click();
        await page.waitForTimeout(700);
        await page.locator('summary', { hasText: '让 AI 帮我想新选题' }).first().click().catch(() => { });
        await page.waitForTimeout(400);
        await clickSel(page, '[data-testid="topics-distill"]');
        await page.waitForTimeout(900);
        const body = planF1.distillBody;
        check(!!body, `${id}0 正控:「生成选题」的请求真的发出去了`, body ? JSON.stringify(body) : '(没发)');
        check(!!body && body.industry_key === industry,
            `${id} 🔴 蒸馏请求体 industry_key == 客户档案的行业原文(${industry})`,
            body ? String(body.industry_key) : '(没发)');
        await page.close();
    }
    /* ══ WO_283 · 改写丢掉的另外五件(F2 / F5 / F6 / F7 / F8)+ 详情页不原样上屏(F9)══════════ */
    const ideasDistill = async (plan) => {
        const page = await openWorkspace(plan);
        await page.getByRole('button', { name: '自选题目 / AI 灵感' }).click();
        await page.waitForTimeout(700);
        await page.locator('summary', { hasText: '让 AI 帮我想新选题' }).first().click().catch(() => { });
        await page.waitForTimeout(300);
        return page;
    };
    const text = (page, sel) => page.evaluate((s) => {
        const e = document.querySelector(s);
        return e ? (e.textContent || '').replace(/\s+/g, ' ').trim() : null;
    }, sel);
    {
        /* F2a:这一批蒸完、服务端说是降级做的 ⇒ 列表上方常驻一句人话 */
        const plan = { distillDone: 'no_same_industry_image_post' };
        const page = await ideasDistill(plan);
        await clickSel(page, '[data-testid="topics-distill"]');
        await page.waitForTimeout(1500);
        const note = await text(page, '[data-testid="topics-distill-degraded"]');
        check((plan.counts.distillPoll || 0) >= 1, 'W283-F2a0 正控:蒸馏进度真的问到了终态', `${plan.counts.distillPoll || 0} 次`);
        check(!!note && note.includes('同行业图文样本不足'),
            'W283-F2a 🔴 降级做出来的一批 ⇒ 屏幕上如实说(原因译成人话)', note || '(没有)');
        await page.close();
    }
    {
        /* F2b / F2c:换个页面再进来,上一批的降级情况要读回来;没降级就一个字不说(对照臂) */
        for (const [id, code, want] of [['W283-F2b', 'partial_same_industry_image_post', '同行业图文样本偏少'],
            ['W283-F2c', '', null]]) {
            const plan = { latestDegraded: code };
            const page = await ideasDistill(plan);
            await page.waitForTimeout(400);
            const note = await text(page, '[data-testid="topics-distill-degraded"]');
            check((plan.counts.latest || 0) >= 1 && (want ? !!note && note.includes(want) : note === null),
                `${id} ${want ? '🔴 进页面就把上一批的降级情况读回来' : '对照:没降级 ⇒ 不出这块'}`,
                `读了 ${plan.counts.latest || 0} 次 · ${note === null ? '(没有这块)' : note}`);
            await page.close();
        }
    }
    {
        /* F6:服务端给 eta 42 ⇒ 进度那一行写「约 42 秒」 */
        const plan = { eta: 42 };
        const page = await ideasDistill(plan);
        await clickSel(page, '[data-testid="topics-distill"]');
        await page.waitForTimeout(1200);
        const eta = await text(page, '[data-testid="topics-distill-eta"]');
        check((plan.counts.distillPoll || 0) >= 1 && eta === '约 42 秒',
            'W283-F6 🔴 进度旁的剩余时间就是服务端给的 42 秒(最长那一段里只有它在动)', eta || '(没有)');
        await page.close();
    }
    {
        /* F7:筛选 chip 与行上徽章说的是同一句。
           🔴 要在「自选题目」视图量:关键词视图里改前就对得上(两边都叫未制作),对不上的是选题行(叫待做)。 */
        const page = await openWorkspace({});
        await page.getByRole('button', { name: '自选题目 / AI 灵感' }).click();
        await page.waitForTimeout(900);
        const v = await page.evaluate(() => {
            const chip = document.querySelector('[data-testid="topics-chip"][data-chip="pending"]');
            const row = document.querySelector('[data-testid="topic-row"][data-topic-status="pending"] [data-testid="topic-status"]');
            return { chip: chip ? (chip.textContent || '').trim() : '', row: row ? (row.textContent || '').replace(/^·\s*/, '').trim() : '' };
        });
        check(v.row !== '' && v.chip.startsWith(v.row),
            'W283-F7 🔴 「未制作」那一档:chip 与行上徽章同一句(原来一个叫未制作、一个叫待做)', `chip「${v.chip}」 · 行「${v.row}」`);
        await page.close();
    }
    {
        /* F5:榜单质量闸的发现画出来,「看第 N 张」真的切到那一张;没有发现就不出这块(对照臂) */
        const page = await openDetail(browser, { width: 1600, postId: 41,
            rankingGates: [{ gate: 'r2_count', message: '标题说 5 家，实测 3 家', card_indices: [1] }] });
        const items = await page.locator('[data-testid="ranking-gate"]').allInnerTexts();
        check(items.length === 1 && items[0].includes('标题说 5 家，实测 3 家'),
            'W283-F5 🔴 榜单闸的发现画出来了(message 原话)', items.join(' | ') || '(没有)');
        await clickSel(page, '[data-testid="ranking-gate-card"]');
        await page.waitForTimeout(500);
        const alt = await page.evaluate(() => {
            const e = document.querySelector('img[alt^="第 "][alt$=" 张"]');
            return e ? e.getAttribute('alt') : '';
        });
        check(alt === '第 2 张', 'W283-F5b card_indices 有用处:点「看第 2 张」就切到第 2 张', alt || '(没有主图)');
        await page.close();
        const plain = await openDetail(browser, { width: 1600, postId: 41 });
        check(await text(plain, '[data-testid="ranking-gates"]') === null,
            'W283-F5c 对照:没有发现(非榜单作品)就不出这块');
        await plain.close();
    }
    {
        /* F8 + F9:详情页「重抽这一张」那一跳真断网 ⇒ 人话 + role="alert" */
        const plan = { width: 1600, postId: 41, redrawNetworkFail: true };
        const page = await openDetail(browser, plan);
        await page.click('[data-testid="ocr-check-run"]');
        await page.waitForSelector('[data-testid="ocr-redraw-card-3"]', { timeout: 15000 });
        await page.click('[data-testid="ocr-redraw-card-3"]');
        await page.waitForSelector('[data-testid="redraw-submit"]', { timeout: 10000 });
        await page.click('[data-testid="redraw-submit"]');
        await page.waitForTimeout(1200);
        const v = await page.evaluate(() => {
            const e = document.querySelector('[data-testid="detail-action-error"]');
            return e ? { role: e.getAttribute('role'), text: (e.textContent || '').replace(/\s+/g, ' ').trim() } : null;
        });
        check((plan.counts.redrawAborted || 0) >= 1, 'W283-F9a0 正控:重抽那一跳真的发出去并被断网了',
            `${plan.counts.redrawAborted || 0} 次`);
        check(!!v && v.text.includes('网络不通') && !/Failed to fetch|TypeError/i.test(v.text),
            'W283-F9 🔴 详情页动作失败是人话,没有浏览器原话', v ? v.text : '(没有报错块)');
        check(!!v && v.role === 'alert', 'W283-F8 🔴 动作失败块 role="alert"(读屏会播报)', v ? String(v.role) : '(没有报错块)');
        await page.close();
    }
/* ═══ WO_262 · 「重抽什么」要说得出来 ═══════════════════════════════
 * 客户原话:「系统让我重抽,但没告诉我要重抽什么?重抽哪一部分」。
 * 两格分别钉两件事:
 *   W262-1 面板把 missing **逐条**列出来(不是 join 成一行),且**不解析**列表 repr;
 *   W262-2 面板那颗「重抽这一张」把人直接送进弹窗,而且**弹窗里带着原因**。
 * 🔴 每格前面先放一条分母自证:没有它,"没找到坏东西"与"根本没渲染出来"读数同形。
 */
{
    const page = await openDetail(browser, { width: 1600, postId: 41 });
    await page.click('[data-testid="ocr-check-run"]');
    await page.waitForSelector('[data-testid="ocr-missing"]', { timeout: 15000 });

    const items = await page.locator('[data-testid="ocr-missing-item"]').allInnerTexts();
    check(items.length === 2,
        'W262-0 分母自证:面板真的渲染出了那两条缺失(否则下面两格是空过)',
        `${items.length} 条`);
    check(items.every((t) => !t.includes('、') || items.length === 2) && items.length === 2,
        'W262-1 missing **逐条一行**,不再 join 成一句', `${items.length} 行`);
    /* 🔴 前端不解析列表 repr:桩里第二条故意带 `['…','…']`,它必须**原样**出现。
          若哪天有人在前端加 JSON.parse 兜底,这一格会红 —— 那正是要拦的,
          因为后端归一化的缺陷会因此永远没人看见。 */
    check(items.some((t) => t.includes('[') && t.includes("'")),
        'W262-1b 列表 repr **原样当文本显示**(前端不解析,免得盖住后端的归一化缺陷)',
        items.find((t) => t.includes('[')) ? '原样在' : '🔴 被解析掉了');

    await page.click('[data-testid="ocr-redraw-card-3"]');
    /*
     * 🔴 等的是**弹窗**,不是被测的那段原因块。
     *    第一版写的是 `waitForSelector('[data-testid="redraw-reason"]')` ——
     *    原因块正是 W262-2 要断言的东西,拿掉它这句就抛异常、整臂跳到外层 catch,
     *    于是 W262-2/2b **整格消失**而不是翻红。
     *    **这样的判据只会崩、不会红**:按 rc 读是 1,看起来完全像"咬住了"。
     *    (反臂就是这么把它揪出来的:要求"我的格 OK→FAIL"而不是"rc 非零"。)
     */
    await page.waitForSelector('[role="dialog"]', { timeout: 10000 });
    await page.waitForTimeout(300);
    const title = (await page.locator('[role="dialog"] h2, [role="dialog"] [class*="DialogTitle"]')
        .first().innerText().catch(() => '')) || '';
    const reasons = await page.locator('[data-testid="redraw-reason-item"]').allInnerTexts();
    check(reasons.length === 2,
        'W262-2 点面板那颗按钮 ⇒ 弹窗打开且**带着原因**(逐条,与面板同一份数据)',
        `原因 ${reasons.length} 条 · 标题「${title.slice(0, 12)}」`);
    /* activeIdx 真的被切到那一张:标题写的是「重抽第 4 张」(card_index 3)。 */
    check(/第\s*4\s*张/.test(title),
        'W262-2b activeIdx 真的切到了被点的那一张(标题第 4 张,不是默认第 1 张)',
        title.slice(0, 16));
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
