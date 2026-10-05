#!/usr/bin/env node
/**
 * 判据 · #195 发布中心「图文」档列表 —— 真 chromium · 真组件 · 真请求 · dist 真 CSS。
 *
 * 结构闸(`verify-image-note-publish-list.mjs`)钉的是「映射表唯一、不推断、不冒充」。
 * 但那些都只证到**纯函数与源码**。屏幕上还有三件只有跑起来才看得见的事:
 *   · 徽章/链接/原话**真的渲染出来了**(而且看得见:rect 非零、不被 hidden);
 *   · chip 点下去**真的换了内容**,计数与点进去看到的条数一致;
 *   · 切客户之后列表**真的重拉**了 —— 用的是**真的左上角**那个切换器(#180 机制)。
 *
 * 🔴 必须注入 `dist/assets/index-*.css`:裸 DOM 上 `opacity-40` 也量到 1,
 *    「看得见」这件事在没有真样式时是恒真的。取不到就报「判据不可用」并非零退出。
 *
 * 跑法:cd frontend && npm run build && node scripts/test-image-note-publish-list-render.mjs
 *      出图:… --shots
 */
import {
    mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync,
} from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
/* 🔴 临时目录留在 `node_modules/.cache` 下:esbuild 解析裸 import 靠逐级往上找
   node_modules,放系统 tmp 会当场 resolve 失败。唯一性靠 mkdtemp 随机后缀 ——
   **每进程一份**,否则两把渲染闸/注毒并行时会互相装走对方的包(注毒判决会翻面)。 */
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

const outDir = mkdtempSync(join(CACHE_ROOT, 'a195-list-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
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
import { PublishCenter } from ${q('src/pages/Publishing/PublishCenter.tsx')};
import { ClientSwitcherSidebar } from ${q('src/components/layout/ClientSwitcherSidebar.tsx')};

(globalThis as any).__mount = function (el: HTMLElement, entry?: string) {
    createRoot(el).render(
        <MemoryRouter initialEntries={[entry || '/publish']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              {/* 🔴 把**真的左上角**一起挂上:L6 说的就是"跟着左上角走",
                  用真控件建立前置状态,判据才不是拿我自己新加的控件自证。 */}
              <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
              <PublishCenter />
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
<html lang="zh"><head><meta charset="utf-8"><title>a195 list</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>
`, 'utf8');

const MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
};
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
const ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
    id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent',
};
const CLIENTS = [
    { id: 101, name: 'A 客户', industry: '教育培训', diagnosis_count: 1, quote_count: 1 },
    { id: 202, name: 'B 客户', industry: '医疗美容', diagnosis_count: 1, quote_count: 1 },
    { id: 303, name: 'C 客户还没做图文', industry: '餐饮', diagnosis_count: 1, quote_count: 1 },
];
const ctxFor = (id) => ({
    brand: {
        id,
        name: (CLIENTS.find((c) => c.id === id) || {}).name || '',
        industry: (CLIENTS.find((c) => c.id === id) || {}).industry || '',
        diagnosis_count: 1,
    },
    profile: null,
});
const PROJECTS = [
    { id: 11, brand_id: 101, brand_name: 'A 客户', industry: '教育培训', quote_ids: [11], keyword_count: 5 },
    { id: 21, brand_id: 202, brand_name: 'B 客户', industry: '医疗美容', quote_ids: [21], keyword_count: 3 },
    { id: 31, brand_id: 303, brand_name: 'C 客户还没做图文', industry: '餐饮', quote_ids: [31], keyword_count: 2 },
];

/**
 * 🔴 **行的形状照生产契约抽**,不是我顺手编的:
 *    列名来自 `db/geo_douyin_db._POST_FIELDS`,API 层再加
 *    `progress` / `publish_pending_confirm` / `failure_reason` /
 *    `card_count` / `cover_url`(#184 c1)/ `publish_failure_reason`(#195 c1)。
 *    契约取法:`git show 452b8e812:api/geo_douyin_api.py`(C 的 195-c1 那一笔)。
 *
 * 🔴 每一行都带 `failure_reason`(制作失败原因)—— 故意的:
 *    L3 要证的正是"发布失败行**不许**把它顶上来"。夹具里没有它,那条就没牙。
 */
const REAL_ROW_KEYS = [
    'id', 'brand_id', 'created_by', 'industry_key', 'city', 'keyword',
    'content_type', 'title', 'body_text', 'hashtags', 'cards',
    'oss_keys', 'cover_oss_key', 'status',
    'publish_order_id', 'publish_item_ids', 'publish_status',
    'published_url', 'published_at',
    'generation_meta', 'created_at', 'updated_at',
    'redraw_count', 'style_key', 'contact_enabled', 'closing_stale',
    'aspect_ratio', 'active_revision_id',
    /* API 层后加的 */
    'progress', 'publish_pending_confirm', 'failure_reason',
    'progress_done', 'progress_total', 'card_count', 'cover_url',
    'publish_failure_reason',
];
const PX = 'data:image/svg+xml;utf8,'
    + encodeURIComponent('<svg xmlns="http://www.w3.org/2000/svg" width="54" height="72">'
        + '<rect width="54" height="72" fill="#7c3aed"/></svg>');

const row = (over) => {
    const base = {};
    for (const k of REAL_ROW_KEYS) base[k] = '';
    return {
        ...base,
        brand_id: 101, created_by: 112, content_type: 'image_post', status: 'ready',
        cards: [], oss_keys: ['a', 'b', 'c'], publish_item_ids: [],
        generation_meta: {}, created_at: '2026-09-10T02:00:00Z', updated_at: '2026-09-12T02:00:00Z',
        redraw_count: 0, contact_enabled: false, closing_stale: false,
        active_revision_id: 9001,
        progress: { state: 'succeeded', percent: 100, line: '' },
        publish_pending_confirm: false,
        failure_reason: '',
        progress_done: 3, progress_total: 3,
        card_count: 3, cover_url: PX,
        publish_failure_reason: '',
        ...over,
    };
};
const RAW_REJECT = '账号当日额度已用完,明天再试';
const GEN_ERROR = '生成第 3 张图超时(这是制作失败,不该出现在发布失败行)';

const POSTS_BY_BRAND = {
    101: [
        row({ id: 501, keyword: '成人英语培训 哪家好', publish_status: 'published',
            published_url: 'https://www.xiaohongshu.com/explore/abc501',
            published_at: '2026-09-12T06:30:00Z' }),
        row({ id: 502, keyword: '雅思一对一 价格', publish_status: 'publishing',
            publish_pending_confirm: true, failure_reason: GEN_ERROR }),
        row({ id: 503, keyword: '少儿编程 怎么选', publish_status: 'failed',
            publish_failure_reason: RAW_REJECT, failure_reason: GEN_ERROR }),
        row({ id: 504, keyword: '成人高考 报名', publish_status: '' }),
        row({ id: 505, keyword: '考研英语 冲刺', publish_status: 'self_reported_unverified',
            published_url: 'https://www.xiaohongshu.com/explore/claimed505' }),
        row({ id: 506, keyword: '留学文书 代写', publish_status: 'published', published_url: '',
            published_at: '' }),
        row({ id: 507, keyword: '后端新加的状态', publish_status: 'brand_new_backend_state' }),
    ],
    202: [
        row({ id: 601, brand_id: 202, keyword: '玻尿酸 多少钱', publish_status: 'failed',
            publish_failure_reason: '', failure_reason: GEN_ERROR }),
    ],
    303: [],
};

const clickSafe = (l) => l.click({ timeout: 5000 }).then(() => '').catch((e) => String((e && e.message) || e).split(String.fromCharCode(10))[0]);
const textSafe = (l) => l.innerText({ timeout: 2500 }).catch(() => '');

async function open(browser, plan = {}) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const reqs = [];
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'qa-token');
    });
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const path = url.pathname;
        reqs.push({ method: req.method(), path, query: url.search });
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        if (path.includes('/auth/me')) return json(ME);
        if (path === '/api/client-context/list') return json({ success: true, clients: CLIENTS });
        const m = path.match(/^\/api\/client-context\/(\d+)$/);
        if (m) return json({ success: true, context: ctxFor(Number(m[1])) });
        if (path === '/api/writing/projects') return json({ projects: PROJECTS });
        if (path === '/api/geo-douyin/posts') {
            if (plan.postsBroken) return json({ status: 'error', detail: '后端炸了' }, 500);
            const bid = Number(url.searchParams.get('brand_id') || 0);
            return json({ status: 'success', posts: POSTS_BY_BRAND[bid] || [] });
        }
        return json({ status: 'success', success: true, data: [], items: [], projects: [], total: 0 });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    /*
     * 🔴 主题 class 必须在 **goto 之后、mount 之前**挂。
     *    `addInitScript` 跑在文档解析**之前**,那一刻 `document.documentElement`
     *    还不在,class 加不上去 —— 而出图函数会照旧把浅色命名成 dark_ 发出去。
     *    (同族第四次:#188 手挂被 ThemeProvider 清掉 / #189 探针用了半透明元素 /
     *     #192 同一条 addInitScript 不生效。每次都是"量一次主题"那道闸抓到的。)
     *    本 harness **没挂** ThemeProvider,所以直接挂 class 是安全的;
     *    挂了 Provider 的地方必须走它的 storage key,否则会被 remove 掉。
     */
    if ((plan.theme || 'light') === 'dark') {
        await page.evaluate(() => document.documentElement.classList.add('dark'));
    }
    await page.evaluate((entry) => {
        (globalThis).__mount(document.getElementById('root'), entry);
    }, '/publish');
    await page.waitForTimeout(2600);
    return { page, reqs, pageErrors };
}

/** 通过**真的左上角**选客户(不预塞 sessionStorage:ClientContext 会在 auth 未解析时清掉)。 */
async function pickClientInSidebar(page, name) {
    const bar = page.locator('[data-testid="real-sidebar"]');
    await clickSafe(bar.locator('button').first());
    await page.waitForTimeout(400);
    const err = await clickSafe(bar.locator(`text=${name}`).first());
    await page.waitForTimeout(1800);
    return err;
}

/** 切到「图文发布」档。 */
async function openImageNoteTab(page) {
    const err = await clickSafe(page.locator('[data-testid="publish-tab-imagenote"]'));
    await page.waitForTimeout(1400);
    return err;
}

const postsFetched = (reqs) => reqs
    .filter((r) => r.path === '/api/geo-douyin/posts')
    .map((r) => Number(new URLSearchParams(r.query).get('brand_id')));

/** 「看得见」= 在 DOM 里 **且** rect 非零 **且** 不被 hidden。 */
async function visibleCount(page, sel) {
    return page.evaluate((s) => Array.from(document.querySelectorAll(s)).filter((el) => {
        const r = el.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) return false;
        const st = getComputedStyle(el);
        return st.visibility !== 'hidden' && st.display !== 'none' && Number(st.opacity) > 0.05;
    }).length, sel);
}

const browser = await playwright.chromium.launch();
try {
    // ══ R1 列表真的渲染出来 ═══════════════════════════════════════════
    section('R1 列表渲染(A 客户 7 条:已发/在发/失败/未发/自报/已发无链接/认不出)');
    let A;
    {
        const { page, reqs, pageErrors } = await open(browser, {});
        A = page;
        const pickErr = await pickClientInSidebar(page, 'A 客户');
        check(!pickErr, 'R1a0 夹具自证:真的用**左上角**选上了 A(选不上下面全是假信号)',
            pickErr || '选上了');
        const tabErr = await openImageNoteTab(page);
        check(!tabErr, 'R1a1 夹具自证:「图文发布」这一档点开了', tabErr || '开了');
        check(pageErrors.length === 0, 'R1a2 零 pageerror',
            pageErrors.join(' | ').slice(0, 200) || '干净');
        const fetched = postsFetched(reqs);
        check(fetched.length >= 1 && fetched.every((b) => b === 101),
            'R1a3 🔴 只拉了**左上角那个客户**的作品(拉成别人的就是"显示对、记错人")',
            JSON.stringify(fetched));
        const rows = await visibleCount(page, '[data-testid="pub-imagenote-row"]');
        check(rows === 7, 'R1b 七行都**看得见**(rect 非零、不被 hidden)', `${rows} 行`);
        const badges = await page.locator('[data-testid="pub-imagenote-badge"]').allInnerTexts();
        check(badges.length === 7, 'R1c 每行一枚徽章', `${badges.length} 枚`);
        /* 🔴 逐条对人话,不只数个数:徽章文字错了但数目对,是最容易溜过去的形状。 */
        const want = ['已发布', '发布中', '发布失败', '未发布', '已自报 · 待核实', '已发布', '状态未知'];
        const off = want.map((w, i) => (badges[i] === w ? null : `第${i + 1}行 "${badges[i]}"(应 "${w}")`))
            .filter(Boolean);
        check(off.length === 0, 'R1d 🔴 七条徽章文字逐条对上',
            off.length ? off.join(' / ') : want.join(' | '));
        check(!(await page.locator('[data-testid="pub-imagenote-badge"]').allInnerTexts())
            .join(' ').includes('brand_new_backend_state'),
            'R1e 🔴 认不出的状态**没把裸串上屏**(后端红线)');
        const covers = await visibleCount(page, '[data-testid="pub-imagenote-cover"]');
        check(covers === 7, 'R1f 封面用的是服务端签名 URL,七张都渲染出来了', `${covers} 张`);
        const cc = await page.locator('[data-testid="pub-imagenote-cardcount"]').first().innerText();
        check(cc.trim() === '3 张', 'R1g 张数取服务端 `card_count`,不是前端自己数', cc.trim());
    }

    // ══ R2 已发布行的链接 ═════════════════════════════════════════════
    section('R2 已发布行的真实链接');
    {
        const link = A.locator('[data-post-id="501"] [data-testid="pub-imagenote-url"]');
        check(await link.count() === 1, 'R2a0 分母自证:已发布行上真的有那颗链接');
        const href = await link.getAttribute('href').catch(() => '');
        check(href === 'https://www.xiaohongshu.com/explore/abc501',
            'R2a 🔴 href = 服务端 `published_url` **原值**(拼站内详情 = 点了看不到那篇)', String(href));
        check(await link.getAttribute('target') === '_blank'
            && (await link.getAttribute('rel') || '').includes('noopener'),
            'R2b 新开 + rel=noopener');
        /* 已发布**但没回链接**那一行:不给空链接,给一句说明 */
        check(await A.locator('[data-post-id="506"] [data-testid="pub-imagenote-url"]').count() === 0,
            'R2c 🔴 `published` 但服务端没给 url ⇒ **不给链接**(空 href 又是一颗点了没反应的东西)');
        check(await A.locator('[data-post-id="506"] [data-testid="pub-imagenote-nourl"]').count() === 1,
            'R2d 那一行有一句「链接还没回来」,不是默默什么都不显示');
        /* 自报未核实那一行带着 url,**也不许**给链接 */
        check(await A.locator('[data-post-id="505"] [data-testid="pub-imagenote-url"]').count() === 0,
            'R2e 🔴 「自报 · 待核实」带着 url 也不给「看已发布的那篇」——'
            + '那是替服务商对客户断言一件没有证据的事');
    }

    // ══ R3 失败行的原话 ═══════════════════════════════════════════════
    section('R3 失败行显示供应商原话,不冒充制作失败');
    {
        const note = A.locator('[data-post-id="503"] [data-testid="pub-imagenote-failed-note"]');
        check(await note.count() === 1, 'R3a0 分母自证:失败行上真的有那一行字');
        const txt = await textSafe(note);
        check(txt.includes(RAW_REJECT),
            'R3a 🔴 显示的是供应商 `reject_reason` **原话**(195-c1)', txt);
        check(!txt.includes('生成第 3 张图超时'),
            'R3b 🔴🔴 **没有**把同一行上的制作失败 `failure_reason` 顶上来 ——'
            + '用户按那句会去重做内容,而发布被拒真正要做的是换账号或改文案', txt);
        const all = await A.locator('#root').innerText();
        check(!all.includes('生成第 3 张图超时'),
            'R3c 整页任何地方都没露出制作失败那句(顶不上来,也不许在别处冒出来)');
    }

    // ══ R4 chip 过滤 + 计数 ═══════════════════════════════════════════
    section('R4 chip 按状态过滤,计数与点进去的条数一致');
    {
        const labels = await A.locator('[data-testid="pub-imagenote-chips"] button').allInnerTexts();
        check(labels.length === 5, 'R4a0 分母自证:五枚 chip 都在', labels.join(' | '));
        /* 计数直接读屏:chip 上写的数就是用户看到的那个数 */
        const want = { 全部: 7, 已发布: 2, 发布中: 2, 发布失败: 1, 未发布: 1 };
        const offs = Object.entries(want)
            .filter(([k, v]) => !labels.some((t) => t.replace(/\s+/g, '') === `${k}${v}`))
            .map(([k, v]) => `${k} 应 ${v}`);
        check(offs.length === 0, 'R4a 🔴 chip 上的计数逐个对(含认不出的那条只进「全部」)',
            offs.length ? `${offs.join(' / ')} · 实际 ${labels.join('|')}` : labels.join(' | '));
        for (const [chip, expect] of [['failed', 1], ['published', 2], ['inflight', 2], ['unpublished', 1]]) {
            await clickSafe(A.locator(`[data-testid="pub-imagenote-chip-${chip}"]`));
            await A.waitForTimeout(500);
            const n = await visibleCount(A, '[data-testid="pub-imagenote-row"]');
            check(n === expect,
                `R4b[${chip}] 🔴 点下去看到的条数 = chip 上那个数`, `${n} 行(应 ${expect})`);
        }
        await clickSafe(A.locator('[data-testid="pub-imagenote-chip-all"]'));
        await A.waitForTimeout(500);
        check(await visibleCount(A, '[data-testid="pub-imagenote-row"]') === 7,
            'R4c 回到「全部」= 七行(认不出的那条仍然在)');
    }

    // ══ R5 只看不发 ═══════════════════════════════════════════════════
    section('R5 这一档没有发布按钮(行为臂)');
    {
        const panelTexts = await A.locator('[data-testid="pub-imagenote-list"] button').allInnerTexts();
        const looksLikePublish = panelTexts.filter((t) => /^发布$|一键发布|提交发布/.test(t.trim()));
        check(looksLikePublish.length === 0,
            'R5a 🔴 面板里没有任何"发布"按钮 —— 发布在制作台;'
            + '同一个付费动作两处实现,必有一处的幂等或价格没人验',
            looksLikePublish.join(' | ') || `按钮共 ${panelTexts.length} 颗,无发布`);
        check(panelTexts.length >= 5,
            'R5a0 分母自证:面板里确实有一堆按钮可查(五枚 chip 至少)', `${panelTexts.length} 颗`);
        check(await A.locator('[data-testid="publish-imagenote-goto-studio"]').count() === 1,
            'R5b 指路那个出口在(人想发东西时得知道去制作台)');
        await A.close();
    }

    // ══ R6 切客户 ⇒ 列表随之变(真的左上角)══════════════════════════
    section('R6 左上角切客户 ⇒ 列表重拉(#180 机制)');
    {
        const { page, reqs } = await open(browser, {});
        await pickClientInSidebar(page, 'A 客户');
        await openImageNoteTab(page);
        const before = postsFetched(reqs).length;
        check(before >= 1 && await visibleCount(page, '[data-testid="pub-imagenote-row"]') === 7,
            'R6a0 前置自证:A 的七行先在屏幕上', `请求 ${before} 次`);
        const switchErr = await pickClientInSidebar(page, 'B 客户');
        check(!switchErr, 'R6a1 夹具自证:真的用左上角切到了 B', switchErr || '切了');
        await page.waitForTimeout(1200);
        const after = postsFetched(reqs);
        check(after.slice(before).some((b) => b === 202),
            'R6b 🔴 切客户后**真的重拉**了,且 brand_id 是新客户的 202',
            JSON.stringify(after));
        const n = await visibleCount(page, '[data-testid="pub-imagenote-row"]');
        check(n === 1, 'R6c 🔴 屏幕上换成 B 的 1 行 —— 留着上一个客户的 7 行就是"记错人"', `${n} 行`);
        /* B 那条是 failed 但**原话为空** ⇒ 退到兜底那句,不显示一个空的「失败原因:」 */
        const bnote = await textSafe(page.locator('[data-post-id="601"] [data-testid="pub-imagenote-failed-note"]'));
        check(bnote.includes('选择这条图文') && bnote.includes('发布回执') && !bnote.includes('供应商'),
            'R6d 原话为空时退到兜底那句(显示一个空的「失败原因:」比不显示更像坏了)', bnote);
        check(!bnote.includes('生成第 3 张图超时'),
            'R6e 🔴 原话为空**也不**把制作失败那句顶上来');
        await page.close();
    }

    // ══ R7 空态 / 取不到 ══════════════════════════════════════════════
    section('R7 空态与取数失败分开说');
    {
        const { page } = await open(browser, {});
        await pickClientInSidebar(page, 'C 客户还没做图文');
        await openImageNoteTab(page);
        check(await visibleCount(page, '[data-testid="pub-imagenote-empty"]') === 1,
            'R7a 该客户一条图文都没有 ⇒ 空态那句');
        check(await page.locator('[data-testid="pub-imagenote-empty-cta"]').getAttribute('href') === '/writing',
            'R7b 空态给的是**去创作中心**的出口');
        await page.close();
    }
    {
        const { page } = await open(browser, { postsBroken: true });
        await pickClientInSidebar(page, 'A 客户');
        await openImageNoteTab(page);
        check(await visibleCount(page, '[data-testid="pub-imagenote-error"]') === 1,
            'R7c 🔴 接口 500 时说「没取到」,**不显示一张空列表** ——'
            + '空列表会被读成"这个客户没有图文",那是一句错的事实');
        check(await visibleCount(page, '[data-testid="pub-imagenote-empty"]') === 0,
            'R7d 那种时候**不**同时显示空态(两句话一起出现等于都不可信)');
        await page.close();
    }

    // ══ 截图:深浅 × 有数据/空态 ══════════════════════════════════════
    if (process.argv.includes('--shots')) {
        section('截图(有数据 / 空态 × 深浅)');
        const OUT = join(ROOT, '..', '..', 'a195-shots');
        mkdirSync(OUT, { recursive: true });
        for (const theme of ['dark', 'light']) {
            for (const [name, who] of [['rows', 'A 客户'], ['empty', 'C 客户还没做图文']]) {
                const { page } = await open(browser, { theme });
                await pickClientInSidebar(page, who);
                await openImageNoteTab(page);
                /*
                 * 🔴 出图前**量**一次主题:标称与实测不符就不出图。
                 *    把浅色命名成 dark_ 发出去,没有任何东西会报警 ——
                 *    这道闸在 #188 / #189 / #192 各拦过一次。
                 *    探针用**不透明**的 `bg-background`,别用半透明元素(读数会被底色拉平)。
                 */
                const lum = await page.evaluate(() => {
                    const el = document.createElement('div');
                    el.className = 'bg-background';
                    document.body.appendChild(el);
                    const c = document.createElement('canvas');
                    c.width = c.height = 1;
                    const ctx = c.getContext('2d');
                    ctx.fillStyle = '#808080';
                    ctx.fillStyle = getComputedStyle(el).backgroundColor;
                    ctx.fillRect(0, 0, 1, 1);
                    const d = ctx.getImageData(0, 0, 1, 1).data;
                    el.remove();
                    return d[0] * 0.299 + d[1] * 0.587 + d[2] * 0.114;
                }).catch(() => -1);
                const looksDark = lum >= 0 && lum < 110;
                if ((theme === 'dark') !== looksDark) {
                    console.log(`  🔴 主题没生效:标称 ${theme} 实测亮度 ${Math.round(lum)} —— 不出这张`);
                    await page.close();
                    continue;
                }
                const target = page.locator('[data-testid="pub-imagenote-list"]').first();
                const f = join(OUT, `imagenote_${name}_${theme}.png`);
                if (await target.count()) {
                    await target.screenshot({ path: f }).catch(() => { });
                } else {
                    await page.screenshot({ path: f }).catch(() => { });
                }
                console.log(`  ${theme}/${name}: 亮度 ${Math.round(lum)} → ${f}`);
                await page.close();
            }
        }
    }
} catch (err) {
    unusable('跑挂了', (err && err.stack) || err);
} finally {
    try { await browser.close(); } catch { /* 尽力 */ }
    try { server.close(); } catch { /* 尽力 */ }
}

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
