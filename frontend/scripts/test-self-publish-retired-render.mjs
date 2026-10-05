#!/usr/bin/env node
/**
 * 行为臂 · WO_273 浏览器插件自助发布退役在页面上(真 chromium · 真组件 · 真请求)。
 * 结构臂(源码锁)见 test-self-publish-retired.mjs(build 链末尾)。
 *
 * 七段,全部夹具是合成值(品牌 101 / 用户 37 是夹具 id,不是真客户):
 *   T 顶栏:发布中心顶栏(标题 + 模式分页 + 调研入口)里**不出现「自助」**
 *   U 老链:`/publish?mode=self` 落到代发(文章栏 + 软文价格都在),不是一个没有渲染分支的白屏
 *   H 已分发列表里历史自助记录仍在(工单「用现有夹具」):夹具与判据**原样搬自**已删的
 *     tests/publish-self-axis/self-report-never-green.spec.ts 第 3 格「fallback 路(stats 未到货)」——
 *     历史自助回执(未核实)进第五桶「待核实」、不进已分发也不进未分发;对照臂:同一篇已核实 ⇒ 进「已分发」
 *   R 发布记录 → 自助发布:历史自助记录只读可见;后端即便仍下发 can_reverify / can_attest = true,
 *     「重新核实」「人工证据」两个按钮也不再出现(它们打的正是被删的插件接口)
 *   A 管理员 / 个人页:个人设置无「插件授权」、用户详情无「插件✓」、订单管理无「自发记录」且 ?tab=self 落回概览
 *   N 全程(以上每一页)**零** `/api/extension` 请求 —— 源码锁看不见的拼接构造,在这里按真请求兜住
 *   E 全程零未捕获异常(pageerror)
 *
 * 每段先过分母自证(页面真渲染出来、请求真发出去),否则「没出现」是空话。
 * 三态退出码:0 全过 / 1 有失败 / 3 判据不可用。
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync, mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const require_ = createRequire(import.meta.url);
const outDir = mkdtempSync(join(CACHE_ROOT, 'a273-render-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

let failed = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 1500));
    process.exit(3);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { unusable('esbuild / playwright 取不到', err); }

const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));
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
import { PublishCenter } from ${q('src/pages/Publishing/PublishCenter.tsx')};
import { ProfilePage } from ${q('src/pages/Account/ProfilePage.tsx')};
import AdminUserDetail from ${q('src/pages/Admin/AdminUserDetail.tsx')};
import { OrderManagementPage } from ${q('src/pages/OrderManagement/OrderManagementPage.tsx')};
import { ClientSwitcherSidebar } from ${q('src/components/layout/ClientSwitcherSidebar.tsx')};

(globalThis as any).__mount = function (el: HTMLElement, entry: string) {
  createRoot(el).render(
    <MemoryRouter initialEntries={[entry]}>
      <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
        <OnboardingProvider><PricingProvider><ClientProvider>
          <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
          <Routes>
            <Route path="/publish" element={<PublishCenter />} />
            <Route path="/account/profile" element={<ProfilePage />} />
            <Route path="/admin/users/:id" element={<AdminUserDetail />} />
            <Route path="/admin/orders" element={<OrderManagementPage />} />
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
        loader: { '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.jpeg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl', '.svg': 'dataurl' },
        plugins: [rawSuffixPlugin], logLevel: 'silent',
    });
} catch (err) { unusable('打包失败', (err && err.message) || err); }

try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, c[0].f), 'utf8'), 'utf8');
} catch (err) { unusable('取不到真 CSS —— 先 npm run build', err); }
writeFileSync(join(outDir, 'index.html'), `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>a273</title><link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>`, 'utf8');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = createServer((req, res) => {
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try { const b = readFileSync(join(outDir, n)); res.writeHead(200, { 'Content-Type': MIME[extname(n)] || 'application/octet-stream' }); res.end(b); }
    catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

/* ── 夹具(全部合成值)──────────────────────────────────────────────── */
const ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: true, permissions: [], user_mode: 'agent', extension_authorized: true },
    id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: true, permissions: [], user_mode: 'agent', extension_authorized: true,
};
const CLIENTS = [{ id: 101, name: 'A 夹具客户', industry: '软件', diagnosis_count: 1, quote_count: 1 }];
const PROJECTS = [{ id: 11, brand_id: 101, brand_name: 'A 夹具客户', industry: '软件', quote_ids: [11], keyword_count: 1 }];
/* 同已删 spec 的夹具:一篇 topic 501 / article 601 的已完成文章 */
const ARTICLES = [{ topic_id: 501, id: 501, article_id: 601, title: '历史自助回执夹具文章', keyword: '夹具词', status: 'completed', publication_eligible: true }];
const SELF_RECORD = {
    record_key: 'self-pr-1', source: 'self', action_id: 'pr-1', article_id: 601, article_title: '历史自助发布夹具记录',
    brand_id: 101, brand_name: 'A 夹具客户', channel_name: 'zhihu', media_type: 'article',
    status_key: 'reported_unverified', status_label: '待核实', status_family: 'reported_unverified',
    publication_axis: 'reported_success_unverified', can_view: true,
    /* 🔴 故意照后端现状下发 true:C 的后端改文案 / 摘标志位之前,前端也不许再冒出这两个按钮 */
    can_reverify: true, can_attest: true,
    points: 0, created_at: '2026-06-01T08:00:00Z', can_withdraw: false, can_refund: false, can_republish: false,
};
const PROXY_RECORD = {
    record_key: 'proxy-1', source: 'proxy', action_id: 'o-1', order_sn: 'SN-1', article_id: 602, article_title: '代发夹具记录',
    brand_id: 101, brand_name: 'A 夹具客户', channel_name: '夹具媒体', media_type: 'article',
    status_key: 'completed', status_label: '已完成', status_family: 'completed', points: 390,
    created_at: '2026-06-02T08:00:00Z', can_withdraw: false, can_refund: false, can_republish: false,
};
const HISTORY_STATS = (n) => ({ total: n, completed: 0, in_progress: 0, pending: 0, rejected: 0, withdrawn: 0, refunded: 0, reported_unverified: n });
const PROFILE = {
    success: true,
    user: { id: 112, username: 'qa', display_name: '夹具账号', email: '', company: '', job_title: '', industry: '', city: '', bio: '', extension_authorized: true },
    personality: null, creator_info: null, speaking_style: null, brands: [], team: null, wallet: {},
    top_features: [], total_operations: 3, direct_referrals: 2, publish_stats: { total: 0, published: 0 },
};
const ADMIN_DETAIL = {
    success: true,
    user: { id: 37, username: 'qa-detail', display_name: '夹具用户', is_admin: false, extension_authorized: true, created_at: '2026-09-01T00:00:00Z' },
    finance: { wallet: {} }, referrals: {}, usage: {}, personality: null, creator_info: null, brands: [], team: null,
};

const EXT = [];      // 全程所有 /api/extension 请求(N 段)
let API_SEEN = 0;    // 全程 API 请求数(N 段分母自证)
const PAGE_ERRORS = [];

/** scenario:'fallback-unverified' | 'fallback-verified' | 'plain' —— H 段唯一的自变量是核实位 */
async function openAt(browser, entry, log, scenario = 'plain') {
    const page = await browser.newPage({ viewport: { width: 1400, height: 1400 } });
    await page.addInitScript(() => { localStorage.setItem('omnirank_token', 'qa-token'); localStorage.setItem('omnirank_screenshot_mode', '1'); });
    page.on('pageerror', (e) => { PAGE_ERRORS.push(`${entry}: ${String(e).slice(0, 200)}`); if (process.env.A273_DEBUG) console.log('  [pageerror]', String(e).slice(0, 300)); });
    if (process.env.A273_DEBUG) page.on('console', (m) => { if (m.type() === 'error') console.log('  [console]', m.text().slice(0, 200)); });
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const path = url.pathname;
        API_SEEN += 1;
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        const rec = (k) => { (log[k] = log[k] || []).push({ query: Object.fromEntries(url.searchParams), body: req.postData() || '' }); };
        if (path.startsWith('/api/extension')) { EXT.push(`${entry} → ${req.method()} ${path}`); return json({ success: true }); }
        if (path.includes('/auth/me')) return json(ME);
        if (path === '/api/auth/profile') { rec('profile'); return json(PROFILE); }
        if (path === '/api/client-context/list') return json({ success: true, clients: CLIENTS });
        if (/^\/api\/client-context\/\d+$/.test(path)) return json({ success: true, context: { brand: { ...CLIENTS[0] }, profile: null } });
        if (path === '/api/writing/projects') return json({ projects: PROJECTS });
        if (/^\/api\/placement\/articles\/\d+$/.test(path)) { rec('articles'); return json({ articles: ARTICLES }); }
        if (path === '/api/meijiehezi/published-articles') {
            rec('published');
            if (scenario === 'fallback-unverified') {
                return json({ status: 'success', article_ids: [601], verified_published_article_ids: [], reported_success_unverified_article_ids: [601] });
            }
            if (scenario === 'fallback-verified') {
                return json({ status: 'success', article_ids: [601], verified_published_article_ids: [601], reported_success_unverified_article_ids: [] });
            }
            return json({ status: 'success', article_ids: [], verified_published_article_ids: [], reported_success_unverified_article_ids: [] });
        }
        if (path === '/api/meijiehezi/rejected-articles') return json({ status: 'success', article_ids: [] });
        if (path === '/api/meijiehezi/article-publish-stats') {
            /* 「stats 未到货」:首屏 / 请求失败 / 切客户空窗都是这个状态(搬自已删 spec) */
            if (scenario.startsWith('fallback')) return json({ status: 'error' }, 500);
            return json({ status: 'success', stats: {} });
        }
        if (path === '/api/meijiehezi/awaiting-confirmations') return json({ status: 'success', items: [] });
        if (path === '/api/meijiehezi/publish-history') {
            rec('history');
            const src = url.searchParams.get('source');
            const records = src === 'self' ? [SELF_RECORD] : [PROXY_RECORD];
            return json({ status: 'success', records, total: 1, page: 1, pages: 1, stats: HISTORY_STATS(1), filters: { brands: [], media_types: [] } });
        }
        if (/^\/api\/admin\/users\/\d+\/detail$/.test(path)) { rec('adminDetail'); return json(ADMIN_DETAIL); }
        if (path === '/api/meijiehezi/admin/stats') {
            rec('adminStats');
            return json({ status: 'success', today_orders: 1, published: 1, rejected: 0, session_configured: true, media_count: 3, total_orders: 1, pending: 0, submitted: 0 });
        }
        if (path === '/api/meijiehezi/mhz-orders/brands') { rec('brands'); return json({ status: 'success', brands: [] }); }
        return json({ status: 'success', success: true, data: [], items: [], projects: [], total: 0 });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate((e) => globalThis.__mount(document.getElementById('root'), e), entry);
    return page;
}
const text = (loc) => loc.evaluate((el) => (el.innerText || '').replace(/\s+/g, ' ').trim()).catch(() => '');
const bodyText = (page) => page.evaluate(() => (document.body.innerText || '').replace(/\s+/g, ' '));
const waitText = (page, t, ms = 15000) => page.getByText(t).first().waitFor({ state: 'attached', timeout: ms }).catch(() => { });
async function pickClient(page) {
    const bar = page.locator('[data-testid="real-sidebar"]');
    await bar.locator('button').first().click({ timeout: 5000 }).catch(() => { });
    await page.waitForTimeout(400);
    await bar.getByText('A 夹具客户').first().click({ timeout: 5000 }).catch(() => { });
}
const chipCount = (page, key) => page.locator(`[data-status-filter="${key}"]`).first()
    .getAttribute('data-status-filter-count', { timeout: 3000 }).catch(() => null);

const browser = await playwright.chromium.launch();
try {
    console.log('T 顶栏');
    {
        const page = await openAt(browser, '/publish', {});
        await page.getByRole('heading', { name: '发布中心', level: 1 }).first().waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
        await page.waitForTimeout(1500);
        /* 顶栏 = 真标题 <h1>发布中心</h1> 的父容器(标题 / 模式分页 / 调研入口同在这一行)。
           不靠本单新加的 testid —— 这样同一把尺子在改前的树上也量得到「自助发布」按钮 */
        const bar = page.getByRole('heading', { name: '发布中心', level: 1 }).first().locator('xpath=..');
        const t = await text(bar);
        check(t.includes('发布中心') && t.includes('代发') && t.includes('发布记录'),
            'T0 分母自证:顶栏真渲染出来了(标题 + 代发 / 发布记录两个分页都在)', `顶栏:${t.slice(0, 80)}`);
        /* 🔴 先要读到顶栏的字(t 非空)才算数:锚不在时 t = '',「没有自助」会空转成绿 */
        check(t.length > 0 && !t.includes('自助'), 'T1 🔴 顶栏(标题 + 模式分页 + 调研入口)不出现「自助」',
            !t.length ? '顶栏没读到(锚不在)' : t.includes('自助') ? `顶栏:${t.slice(0, 120)}` : '没有');
        await page.close();
    }

    console.log('\nU 老链 /publish?mode=self');
    {
        const page = await openAt(browser, '/publish?mode=self', {});
        await page.getByRole('heading', { name: '发布中心', level: 1 }).first().waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
        await page.waitForTimeout(2000);
        const top = await page.getByRole('heading', { name: '发布中心', level: 1 }).count();
        check(top === 1, 'U0 分母自证:页面真挂上了(发布中心标题在)', `${top} 个标题`);
        const leftPane = await page.locator('[data-left-pane]').count();
        const softPrice = await page.getByRole('button', { name: '软文价格' }).count();
        const history = await page.locator('[data-testid="history-basis"]').count();
        check(leftPane === 1 && softPrice >= 1 && history === 0,
            'U1 🔴 ?mode=self 落到代发:文章栏 + 「软文价格」都在,不是空白、也不是别的分页',
            `文章栏 ${leftPane} · 软文价格 ${softPrice} · 发布记录 ${history}`);
        await page.close();
    }

    console.log('\nH 已分发列表里历史自助记录仍在(夹具与判据搬自已删 spec 第 3 格)');
    for (const [scenario, label] of [['fallback-unverified', '未核实'], ['fallback-verified', '已核实(对照臂)']]) {
        const log = {};
        const page = await openAt(browser, '/publish?mode=proxy&articles=501', log, scenario);
        await page.waitForTimeout(1500);
        await pickClient(page);
        await page.locator('[data-status-filter="reportedUnverified"]').first().waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
        await page.waitForTimeout(1200);
        const reported = await chipCount(page, 'reportedUnverified');
        const published = await chipCount(page, 'published');
        const unpublished = await chipCount(page, 'unpublished');
        const tag = scenario === 'fallback-unverified' ? 'H1' : 'H2';
        check((log.articles || []).length >= 1 && (log.published || []).length >= 1 && reported !== null,
            `${tag}.0 分母自证(${label}):文章与 published-articles 都真请求了、分段筛选器真渲染了`,
            `articles ${(log.articles || []).length} 次 · published ${(log.published || []).length} 次 · 筛选器${reported === null ? '没出来' : '在'}`);
        if (scenario === 'fallback-unverified') {
            check(reported === '1' && published === '0' && unpublished === '0',
                'H1 🔴 历史自助回执(未核实)仍在列表里:进第五桶「待核实」,不进已分发、也不进未分发',
                `待核实 ${reported} · 已分发 ${published} · 未分发 ${unpublished}`);
        } else {
            check(published === '1' && reported === '0' && unpublished === '0',
                'H2 🔴 对照臂:同一篇历史自助记录已核实 ⇒ 进「已分发」',
                `已分发 ${published} · 待核实 ${reported} · 未分发 ${unpublished}`);
        }
        await page.close();
    }

    console.log('\nR 发布记录 → 自助发布(历史只读)');
    {
        const log = {};
        const page = await openAt(browser, '/publish?mode=history', log);
        await waitText(page, PROXY_RECORD.article_title);
        /* 🔴 按位置认「发布记录」里的来源切换(紧跟「媒介代发」那一颗),不按名字取第一个:
           改前的树上顶栏也有一颗「自助发布」,取第一个会点到顶栏,整段量错对象 */
        const selfToggle = page.locator('button:has-text("媒介代发") + button').first();
        const toggleText = await text(selfToggle);
        await selfToggle.click({ timeout: 8000 }).catch(() => { });
        await waitText(page, SELF_RECORD.article_title);
        await page.waitForTimeout(800);
        const selfAsked = (log.history || []).some((r) => r.query.source === 'self');
        const row = page.locator(`[data-record-key="${SELF_RECORD.record_key}"]`).first();
        const rowText = await text(row);
        check(toggleText === '自助发布' && selfAsked && rowText.includes(SELF_RECORD.article_title) && rowText.includes('知乎'),
            'R0 分母自证:点的是「发布记录」里的来源切换,真按 source=self 请求了,历史自助记录(标题 + 平台名)真渲染了',
            `切换钮「${toggleText}」· source=self ${selfAsked ? '有' : '没有'} · 行文字:${rowText.slice(0, 60) || '(没有这一行)'}`);
        const reverify = await page.getByRole('button', { name: /重新核实/ }).count();
        const attest = await page.getByRole('button', { name: /人工证据/ }).count();
        check(rowText.length > 0 && reverify === 0 && attest === 0,
            'R1 🔴 后端仍下发 can_reverify / can_attest = true,也不再出现「重新核实」「人工证据」(它们打的是被删的插件接口)',
            `重新核实 ${reverify} · 人工证据 ${attest}`);
        await page.close();
    }

    console.log('\nA 个人页 / 用户详情 / 订单管理');
    {
        const log = {};
        const page = await openAt(browser, '/account/profile', log);
        await waitText(page, '直接推荐');
        await page.waitForTimeout(800);
        const t = await bodyText(page);
        check((log.profile || []).length >= 1 && t.includes('直接推荐') && t.includes('服务商身份'),
            'A1.0 分母自证:个人设置的「账户」卡真渲染了(服务商身份 / 直接推荐都在)');
        check(!t.includes('插件授权'), 'A1 🔴 个人设置不再有「插件授权」一格(响应里即便带 extension_authorized=true)');
        await page.close();
    }
    {
        const log = {};
        const page = await openAt(browser, '/admin/users/37', log);
        await waitText(page, ADMIN_DETAIL.user.display_name);
        await page.waitForTimeout(800);
        const t = await bodyText(page);
        check((log.adminDetail || []).length >= 1 && t.includes('用户详情') && t.includes(ADMIN_DETAIL.user.display_name),
            'A2.0 分母自证:用户详情真渲染了(标题 + 用户名都在)');
        check(!t.includes('插件✓') && !t.includes('插件'), 'A2 🔴 用户详情不再有「插件✓」徽章(响应里即便带 extension_authorized=true)');
        await page.close();
    }
    {
        const log = {};
        const page = await openAt(browser, '/admin/orders?tab=self', log);
        await waitText(page, '退款审核');
        await page.waitForTimeout(1500);
        const t = await bodyText(page);
        /* 分母只认「子分页条真渲染了」;落到哪一页是 A3 要量的结果,不许混进分母 */
        check(t.includes('代发订单') && t.includes('退款审核'),
            'A3.0 分母自证:订单管理的子分页条真渲染了(代发订单 / 退款审核都在)');
        check(!t.includes('自发记录') && t.includes('今日代发') && (log.adminStats || []).length >= 1,
            'A3 🔴 订单管理没有「自发记录」;老链 ?tab=self 落回「概览」(概览真请求了、今日代发卡在)',
            `自发记录 ${t.includes('自发记录') ? '还在' : '没有'} · 概览 ${t.includes('今日代发') ? '在' : '没有'} · stats ${(log.adminStats || []).length} 次`);
        await page.close();
    }

    console.log('\nN 全程网络');
    check(API_SEEN >= 20, 'N0 分母自证:整场真的在发 API 请求(拦截器真在记)', `${API_SEEN} 次`);
    check(EXT.length === 0, 'N1 🔴 以上每一页全程零 `/api/extension` 请求', EXT.slice(0, 5).join(' | ') || '0 次');

    console.log('\nE 全程未捕获异常');
    check(PAGE_ERRORS.length === 0, 'E1 以上每一页零 pageerror', PAGE_ERRORS.slice(0, 3).join(' | ') || '0 个');
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
