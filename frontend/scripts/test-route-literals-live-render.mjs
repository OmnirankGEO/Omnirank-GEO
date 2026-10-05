#!/usr/bin/env node
/**
 * 行为臂 · WO_260 上提 m3 组件 + 改掉废弃路由之后,在役页在真浏览器里照旧。
 * 结构臂 = test-route-literals-live.mjs(build 链末步);壳链接在 E3 删域后不死 = verify-dead-interactions.mjs B8
 * (工单:「用现有臂扩,不另起一套」)。
 *
 * 🔴 打包的是 **src/main.tsx 本身**(真引导 · App.tsx 真路由表 · 真登录页 · 真外壳),不是手搭的 MemoryRouter:
 *    这一臂问的是「登录后真路由把人送到哪」,手搭一张路由表就把被量的东西换掉了。
 *    生产模式打包(NODE_ENV=production):console 里只剩用户那边也会出现的报错。
 * 接口全部在浏览器里拦截,夹具全部合成(用户 9101–9103、品牌 201–202 是夹具 id,不是真客户;
 * 登录框里填的是夹具串,请求从不出本机)。
 *
 *   L 登录落地五格(真登录页 → 真 AuthContext → 真 postLoginRedirect → 真 App 路由表):
 *     L1 普通用户(agent_level 0;后端回 recommended_route '/')
 *     L2 服务商(agent_level 1,M3 灰度白名单;后端按 api/c_end_api.py 的规则仍回 '/m3/sales/today')
 *     L3 管理员(后端同样回 '/m3/sales/today')
 *     L4 普通用户从社媒入口登录(/login?entry=social)
 *     L5 服务商浏览器里带着旧键 omnirank_c_end_mode='s'(改后 postLoginRedirect 不再读它;改前登录页 geo 入口挂载时会先删它)
 *     每格:.0 分母自证(登录框真渲染了;login / me 请求真发了;服务商 / 管理员两类另须 settings-mode 真发了 ——
 *              它们的要害就是后端仍回 /m3/sales/today;普通用户格不要求:改前社媒入口根本不问它,那是被测行为本身)
 *           .1 🔴 登录后 SPA 走过的每一个路径都是 '/' —— 不经过 /m3/* /s /c/* 这类要删路由
 *              (今天它们还是「重定向到首页」,E3 删掉那几条重定向后,经过它们就是 404)
 *           .2 落地页是在役首页(管理员 =「运营控制台」;其余 = 首页「GEO 业务」卡)且可见,不是 404 页
 *              (服务商 / 管理员首登会先弹强制二选一「欢迎使用全域上榜」,按真人路径点「我已经用过」后再断言可见)
 *           .3 console error 0 · pageerror 0(工单「console 增量 0」)
 *   W 我的客户(服务商):阶段 / 风险 / 完整度三列照旧渲染 —— m3 BFF 数据经上提后的
 *     StagePill / RiskDot / CompletenessBadge 落到屏幕(两行夹具,每行三件都在;完整度分数取 BFF 夹具值,
 *     与基础列表故意给不同的数,证明读的是 BFF 那条路)
 *   B 品牌详情(服务商):决策条(DecisionBarBridge:品牌名 + 阶段 +「下一步」)· 工具卡(ToolGridCard:
 *     展开「更多客户动作」后 11 张)· 资料确认徽标(MaterialConfirmStatusBadge:「客户已确认」)照旧渲染
 *   N 全程:SPA 到过的路径 ⊆ { 本臂打开的页 ∪ '/' }(白名单:多到过任何一处都红;命中冻结要删路由的在报文里标出)
 *
 * 不进 build 链(要 chromium + 先 npm run build 取真 CSS),挂 browser:arms。
 * 三态退出码:0 全过 / 1 有失败 / 3 判据不可用。
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync, mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { RETIRED_ROUTES } from './lib/retired-routes.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const require_ = createRequire(import.meta.url);
const outDir = mkdtempSync(join(CACHE_ROOT, 'a260-render-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
const DEBUG = !!process.env.A260_DEBUG;

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

/* ── 打包:整站真引导 src/main.tsx ─────────────────────────────────────── */
const viteShims = {
    name: 'vite-shims',
    setup(build) {
        build.onResolve({ filter: /\?raw$/ }, (args) => {
            const bare = args.path.replace(/\?raw$/, '');
            const abs = bare.startsWith('@/') ? join(ROOT, 'src', bare.slice(2)) : join(args.resolveDir, bare);
            return { path: abs, namespace: 'vite-raw' };
        });
        build.onLoad({ filter: /.*/, namespace: 'vite-raw' }, (args) => ({
            contents: readFileSync(args.path, 'utf8'), loader: 'text',
        }));
        /* 样式由 dist 的真 CSS 提供(下面),源里的 import './x.css' 打成空模块 */
        build.onResolve({ filter: /\.css$/ }, (args) => ({ path: args.path, namespace: 'css-stub' }));
        build.onLoad({ filter: /.*/, namespace: 'css-stub' }, () => ({ contents: '', loader: 'js' }));
    },
};
const ENV = { DEV: false, PROD: true, MODE: 'production', BASE_URL: '/', SSR: false };
try {
    await esbuild.build({
        entryPoints: [join(ROOT, 'src', 'main.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        define: { 'process.env.NODE_ENV': '"production"', global: 'globalThis', 'import.meta.env': JSON.stringify(ENV) },
        loader: {
            '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.jpeg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl',
            '.svg': 'dataurl', '.gif': 'dataurl', '.mp4': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl',
        },
        plugins: [viteShims], logLevel: 'silent',
    });
} catch (err) { unusable('打包 src/main.tsx 失败', (err && err.message) || err); }

try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, c[0].f), 'utf8'), 'utf8');
} catch (err) { unusable('取不到真 CSS —— 先 npm run build', err); }
/* 资源用绝对路径:页面会停在 /my-clients/201 这类多段路径上 */
writeFileSync(join(outDir, 'index.html'), `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>a260</title><link rel="stylesheet" href="/app.css"></head>
<body><div id="root"></div><script src="/bundle.js"></script></body></html>`, 'utf8');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
/* SPA 兜底:除两份资源外一律回 index.html(与生产 nginx 同形),BrowserRouter 才能直接落在 /login、/my-clients/201 */
const server = createServer((req, res) => {
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '');
    const file = n === 'bundle.js' || n === 'app.css' ? n : 'index.html';
    try {
        const b = readFileSync(join(outDir, file));
        res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' });
        res.end(req.method === 'HEAD' ? undefined : b);
    } catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const ORIGIN = `http://127.0.0.1:${server.address().port}`;

/* ── 夹具(全部合成值)──────────────────────────────────────────────── */
const baseUser = { is_active: 1, must_change_password: 0, roles: [], permissions: [], client_brand_ids: [] };
const USERS = {
    normal: { ...baseUser, id: 9101, username: 'qa-fixture-l0', display_name: '夹具普通用户', is_admin: false, agent_level: 0 },
    agent: { ...baseUser, id: 9102, username: 'qa-fixture-l1', display_name: '夹具服务商', is_admin: false, agent_level: 1 },
    admin: { ...baseUser, id: 9103, username: 'qa-fixture-admin', display_name: '夹具管理员', is_admin: true, agent_level: 1 },
};
/* 后端 api/c_end_api.py 的真规则:m3_enabled(管理员 / 灰度白名单服务商)⇒ '/m3/sales/today';否则 '/' */
const MODE = {
    normal: { user_id: 9101, is_admin: false, agent_level: 0, preferred_mode: null, recommended_route: '/', m3_enabled: false, can_switch: true },
    agent: { user_id: 9102, is_admin: false, agent_level: 1, preferred_mode: null, recommended_route: '/m3/sales/today', m3_enabled: true, can_switch: true },
    admin: { user_id: 9103, is_admin: true, agent_level: 1, preferred_mode: null, recommended_route: '/m3/sales/today', m3_enabled: true, can_switch: true },
};
const HOME_STATS = {
    status: 'success', nickname: '',
    geo: { brand_count: 2, article_count: 0, published_count: 0, diagnosis_count_month: 1 },
    balance: { total: 1000 },
};
const ADMIN_DASH = {
    status: 'success', cached_at: null,
    users: { total: 3, week_new: 0, today_new: 0, active_7d: 0, active_rate: 0, paid_count: 0, paid_rate: 0, level_distribution: { free: 1, paid: 0, agent_l1: 1 } },
    finance: {
        arpu: 0, cost_breakdown: { publish_media: 0 }, cost_margin_note: '', cost_yuan: 0, llm_api_cost_yuan: 0, profit_rate: 0,
        profit_yuan: 0, publish_external_cost_yuan: 0, recharge_revenue_yuan: 0, revenue_yuan: 0, total_cost_yuan: 0,
        total_operating_cost_yuan: 0, total_revenue_yuan: 0,
    },
    feature_usage: [], api_costs: [], activities: [],
    publishing: { total_orders: 0, published: 0, rejected: 0, media_count: 0, session_valid: true, revenue_yuan: 0, cost_yuan: 0, profit_yuan: 0 },
    referrals: { commission_leaderboard: [], direct_leaderboard: [], indirect_leaderboard: [] },
};
/* 我的客户:基础列表的完整度故意给 10/11,BFF 给 72/35 —— 屏幕上是 72/35 才证明走的是 BFF 那条路 */
const CLIENTS = [
    { id: 201, name: '夹具客户甲', industry: '软件', cities: '杭州', client_status: 'active', has_profile: true, updated_at: '2026-09-20T00:00:00Z', completeness: 10 },
    { id: 202, name: '夹具客户乙', industry: '餐饮', cities: '苏州', client_status: 'active', has_profile: true, updated_at: '2026-09-19T00:00:00Z', completeness: 11 },
];
const BFF_SCORE = { 201: 72, 202: 35 };
const M3_CUSTOMERS = [
    { id: 201, name: '夹具客户甲', industry: '软件', cities: '杭州', brand_type: 'client', diagnosis_count: 0, latest_score: null, latest_diagnosis_id: null, is_test: false, created_at: '2026-09-01T00:00:00Z', latest_quote: null, completeness: { score: BFF_SCORE[201], missing: [], groups: [] } },
    { id: 202, name: '夹具客户乙', industry: '餐饮', cities: '苏州', brand_type: 'client', diagnosis_count: 1, latest_score: 58, latest_diagnosis_id: 3001, is_test: false, created_at: '2026-09-02T00:00:00Z', latest_quote: null, completeness: { score: BFF_SCORE[202], missing: ['联系方式'], groups: [] } },
];
const CONTEXT_BRAND = { id: 201, name: '夹具客户甲', industry: '软件', brand_type: 'client', diagnosis_count: 0, latest_score: null, latest_diagnosis_id: null };
const DETAIL = {
    success: true,
    brand: { id: 201, name: '夹具客户甲', industry: '软件', company_name: '夹具甲公司', cities: '杭州', brand_type: 'client', completeness: 72, diagnosis_count: 0, client_status: 'active' },
    profile: {},
};
const SUMMARY = {
    company_name: '夹具甲公司', industry: '软件', intro_excerpt: '', usp_excerpt: '', fields_filled: [], fields_missing: [],
    filled_count: 3, total_fields: 8, selling_points_count: 0, cases_count: 0, testimonials_count: 0,
};
const MATERIAL = {
    status: 'confirmed', has_session: true, token: null, token_url: null, expires_at: null, confirmed_at: '2026-09-20T00:00:00Z',
    customer_notes: '', materials_summary: SUMMARY, current_summary: SUMMARY, last_session_id: 1, can_generate_link: true,
    brand_id: 201, brand_name: '夹具客户甲',
};
const GENERIC = { status: 'success', success: true, data: [], items: [], list: [], projects: [], clients: [], total: 0 };

function stubFor(who, path, method) {
    if (path === '/api/auth/login') return { success: true, token: `qa-fixture-token-${who}` };
    if (path === '/api/auth/me') return { success: true, user: USERS[who] };
    if (path === '/api/c-end/settings/mode') return MODE[who];
    if (path === '/api/user/home-stats') return HOME_STATS;
    if (path === '/api/admin/dashboard') return ADMIN_DASH;
    if (path === '/api/my-clients' && method === 'GET') return { success: true, clients: CLIENTS, total: CLIENTS.length };
    if (path === '/api/m3/customers') return { success: true, customers: M3_CUSTOMERS };
    if (path === '/api/client-context/list') return { success: true, clients: CLIENTS.map((c) => ({ id: c.id, name: c.name, industry: c.industry })) };
    if (path === '/api/client-context/201') return { success: true, context: { brand: CONTEXT_BRAND, profile: {} } };
    if (path === '/api/my-clients/201') return DETAIL;
    if (path === '/api/brands/201/completeness') return { score: 72, missing: ['联系方式'], groups: [] };
    if (path === '/api/m3/material-confirm/status/201') return MATERIAL;
    if (path === '/api/dashboard/flow-funnel') return { success: true, days: 30, stages: [], summary: { pay_ratio: 0, publish_ratio: 0 } };
    if (path.startsWith('/api/defensive-geo/customer-links')) return { brandId: 201, links: [], hint: '', copyRegistryVersion: 'fixture' };
    return undefined;
}

/** 冻结要删路由表里命中的那一条(只用于报文标注;判红靠白名单,不靠它) */
function retiredHit(p) {
    for (const [pat, bucket] of RETIRED_ROUTES) {
        if (pat.endsWith('/*')) { const b = pat.slice(0, -2); if (p === b || p.startsWith(b + '/')) return `${pat}(${bucket})`; continue; }
        const re = new RegExp('^' + pat.split('/').map((s) => (s.startsWith(':') ? '[^/]+' : s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))).join('/') + '$');
        if (re.test(p)) return `${pat}(${bucket})`;
    }
    return '';
}
const tag = (p) => { const h = retiredHit(p); return h ? `${p} ⟵ 要删路由 ${h}` : p; };

const NAV_ALL = [];   // 全程 SPA 到过的路径(N 段)
async function openPage(browser, who, { path, token = null, preset = {} }) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const rec = { nav: [], consoleErrors: [], pageErrors: [], api: {}, unstubbed: new Set() };
    await page.exposeBinding('__a260nav', (_src, p) => { rec.nav.push(p); NAV_ALL.push({ who, entry: path, p }); });
    /* 记录 SPA 走过的每一个路径:BrowserRouter 的 navigate / <Navigate> 都落到 history.pushState / replaceState */
    await page.addInitScript(({ token, preset }) => {
        try {
            if (token) localStorage.setItem('omnirank_token', token);
            for (const [k, v] of Object.entries(preset)) localStorage.setItem(k, v);
        } catch { /* 隐私模式 */ }
        const send = () => { try { window.__a260nav(location.pathname); } catch { /* 绑定未就绪 */ } };
        for (const name of ['pushState', 'replaceState']) {
            const orig = history[name];
            history[name] = function (...args) { const r = orig.apply(this, args); send(); return r; };
        }
        window.addEventListener('popstate', send);
        send();
    }, { token, preset });
    page.on('console', (m) => { if (m.type() === 'error') rec.consoleErrors.push(m.text().slice(0, 240)); });
    page.on('pageerror', (e) => rec.pageErrors.push(String(e).slice(0, 240)));
    await context.route('**/api/**', async (route) => {
        const req = route.request();
        const p = new URL(req.url()).pathname;
        (rec.api[p] = rec.api[p] || []).push(req.method());
        let body = stubFor(who, p, req.method());
        if (body === undefined) { rec.unstubbed.add(`${req.method()} ${p}`); body = GENERIC; }
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto(ORIGIN + path);
    return { context, page, rec };
}
const asked = (rec, p) => (rec.api[p] || []).length;
/**
 * 首登欢迎二选一(WelcomeChoiceModal:服务商 / 管理员首次登录,强制选择、无关闭钮、点蒙层无效)。
 * 它是 Radix 对话框,开着时把下面整页标成 aria-hidden —— 按角色找首页标题会找不到,点页面按钮也会被蒙层吃掉。
 * 按真人路径处理:点「我已经用过」(直接进真实工作台)。返回是否出现过。
 */
async function dismissWelcome(page) {
    const dlg = page.getByRole('dialog').filter({ hasText: '欢迎使用全域上榜' });
    if (!(await dlg.count()) || !(await dlg.first().isVisible().catch(() => false))) return false;
    await dlg.first().getByText('我已经用过', { exact: true }).first().click({ timeout: 5000 }).catch(() => { });
    await dlg.first().waitFor({ state: 'hidden', timeout: 5000 }).catch(() => { });
    await page.waitForTimeout(400);
    return true;
}
const debugDump = (label, rec) => {
    if (!DEBUG) return;
    console.log(`  [debug ${label}] nav=${JSON.stringify(rec.nav)}`);
    console.log(`  [debug ${label}] 未专门打桩 ${rec.unstubbed.size}:${[...rec.unstubbed].slice(0, 40).join(' | ')}`);
    rec.consoleErrors.forEach((e) => console.log(`  [debug ${label}] console: ${e}`));
    rec.pageErrors.forEach((e) => console.log(`  [debug ${label}] pageerror: ${e}`));
};

const LOGIN_CASES = [
    ['L1', '普通用户', 'normal', '', {}],
    ['L2', '服务商(M3 灰度白名单)', 'agent', '', {}],
    ['L3', '管理员', 'admin', '', {}],
    ['L4', '普通用户 · 社媒入口', 'normal', '?entry=social', {}],
    ['L5', '服务商 · 浏览器里带着旧 c_end_mode 键', 'agent', '', { omnirank_c_end_mode: 's' }],
];
const STAGE_LABELS = ['询价', '诊断中', '待报价', '报价中', '写作', '投放', '监测', '报告', '续费'];

const browser = await playwright.chromium.launch();
try {
    console.log('L 登录落地(真登录页 → 真路由表)');
    for (const [id, label, who, entry, preset] of LOGIN_CASES) {
        const { context, page, rec } = await openPage(browser, who, { path: `/login${entry}`, preset });
        const userBox = page.locator('input[placeholder="请输入手机号或用户名"]');
        const pwdBox = page.locator('input[placeholder="请输入密码"]');
        await userBox.waitFor({ state: 'visible', timeout: 20000 }).catch(() => { });
        const formReady = (await userBox.count()) === 1 && (await pwdBox.count()) === 1;
        if (formReady) {
            await userBox.fill(`qa-fixture-${who}`);
            await pwdBox.fill('fixture-not-a-secret');
            await page.locator('form button[type="submit"]').first().click({ timeout: 8000 }).catch(() => { });
        }
        const marker = who === 'admin'
            ? page.getByRole('heading', { name: '运营控制台', level: 1 })
            : page.getByRole('heading', { name: 'GEO 业务' });
        /* 先等首页标题**挂上 DOM**(不按角色:首登欢迎弹窗开着时它在 aria-hidden 里),再处理欢迎弹窗,最后才按角色断言可见 */
        const markerDom = who === 'admin' ? page.locator('h1', { hasText: '运营控制台' }) : page.locator('h2', { hasText: 'GEO 业务' });
        await markerDom.first().waitFor({ state: 'attached', timeout: 20000 }).catch(() => { });
        await page.waitForTimeout(1500);   // 让落地后的跟随跳转(若有)跑完;欢迎弹窗在首页挂上之后才弹
        const welcomed = await dismissWelcome(page);
        await marker.first().waitFor({ state: 'visible', timeout: 5000 }).catch(() => { });
        const loginIdx = rec.nav.lastIndexOf('/login');
        const after = loginIdx >= 0 ? rec.nav.slice(loginIdx + 1) : [];
        const finalPath = new URL(page.url()).pathname;
        debugDump(id, rec);
        if (DEBUG) {
            const dbg = await page.evaluate(() => ({
                main: ((document.querySelector('main') || document.body).innerText || '').replace(/\s+/g, ' ').slice(0, 300),
                dialogs: Array.from(document.querySelectorAll('[role="dialog"],[role="alertdialog"]')).map((d) => (d.textContent || '').replace(/\s+/g, ' ').slice(0, 120)),
                hiddenGeo: Array.from(document.querySelectorAll('h2')).filter((h) => (h.textContent || '').includes('GEO 业务')).map((h) => !!h.closest('[aria-hidden="true"]')),
            }));
            console.log(`  [debug ${id}] main: ${dbg.main}`);
            console.log(`  [debug ${id}] dialogs: ${JSON.stringify(dbg.dialogs)} · 「GEO 业务」h2 在 aria-hidden 里: ${JSON.stringify(dbg.hiddenGeo)}`);
        }
        /* 分母不许依赖被测行为:settings-mode 只对服务商 / 管理员两格要求(这两格的要害就是后端仍回 /m3/sales/today,
           必须证明那份回包真被读了);普通用户格只看登录框 + login / me(改前社媒入口根本不问 settings-mode,那是被测行为本身) */
        const needMode = who !== 'normal';
        check(formReady && asked(rec, '/api/auth/login') >= 1 && asked(rec, '/api/auth/me') >= 1 && (!needMode || asked(rec, '/api/c-end/settings/mode') >= 1),
            `${id}.0 分母自证(${label}):登录框真渲染了;login / me${needMode ? ' / settings-mode(后端回 /m3/sales/today 那份)' : ''} 请求真发了`,
            `登录框${formReady ? '在' : '没出来'} · login ${asked(rec, '/api/auth/login')} · me ${asked(rec, '/api/auth/me')} · mode ${asked(rec, '/api/c-end/settings/mode')}`);
        check(after.length > 0 && after.every((p) => p === '/') && finalPath === '/',
            `${id}.1 🔴 ${label}登录后 SPA 走过的每一个路径都是 /(不经过任何要删路由)`,
            `登录后路径:${after.map(tag).join(' → ') || '(没离开登录页)'} · 停在 ${finalPath}`);
        const landed = await marker.count();
        const landedVisible = landed >= 1 && await marker.first().isVisible().catch(() => false);
        const notFound = await page.getByText('这个页面不存在').count();
        check(landedVisible && notFound === 0,
            `${id}.2 ${label}落地页是在役首页(${who === 'admin' ? '运营控制台' : '首页「GEO 业务」卡'})且可见,不是 404 页`,
            `首页标识 ${landed}${landedVisible ? '(可见)' : ''} · 404 文案 ${notFound} · 首登欢迎弹窗 ${welcomed ? '有(按真人路径点了「我已经用过」)' : '无'}`);
        check(rec.consoleErrors.length === 0 && rec.pageErrors.length === 0,
            `${id}.3 ${label}全程 console error 0 · pageerror 0`,
            [...rec.consoleErrors, ...rec.pageErrors].slice(0, 3).join(' | ') || '0 条');
        await context.close();
    }

    console.log('\nW 我的客户:阶段 / 风险 / 完整度三列');
    {
        const { context, page, rec } = await openPage(browser, 'agent', { path: '/my-clients', token: 'qa-fixture-token-agent' });
        await page.locator('[data-testid="client-star-202"]').first().waitFor({ state: 'attached', timeout: 20000 }).catch(() => { });
        await page.waitForTimeout(800);
        await dismissWelcome(page);
        const head = await page.evaluate(() => document.body.innerText || '');
        const rows = {};
        for (const id of [201, 202]) {
            const row = page.locator(`[data-testid="client-star-${id}"]`).first()
                .locator('xpath=ancestor::div[contains(@class,"cursor-pointer")][1]');
            rows[id] = (await row.count()) ? await row.evaluate((el) => {
                const cols = Array.from(el.children);
                const stageEl = cols[2] && cols[2].firstElementChild;
                return {
                    cols: cols.length,
                    stage: stageEl ? (stageEl.textContent || '').trim() : '',
                    risk: el.querySelectorAll('[role="img"][aria-label^="风险等级:"]').length,
                    comp: Array.from(el.querySelectorAll('[data-testid="completeness-badge"]')).map((b) => b.getAttribute('aria-label') || ''),
                };
            }) : null;
        }
        debugDump('W', rec);
        check(asked(rec, '/api/my-clients') >= 1 && asked(rec, '/api/m3/customers') >= 1 && rows[201] && rows[202]
            && head.includes('阶段 / 状态') && head.includes('资料完整度'),
            'W0 分母自证:基础列表与 m3 BFF 两个请求真发了,两行夹具客户与表头(阶段 / 状态 · 资料完整度)真渲染了',
            `my-clients ${asked(rec, '/api/my-clients')} · m3/customers ${asked(rec, '/api/m3/customers')} · 行 ${[201, 202].filter((i) => rows[i]).length}/2`);
        for (const id of [201, 202]) {
            const r = rows[id] || { stage: '', risk: 0, comp: [] };
            check(STAGE_LABELS.includes(r.stage), `W1.${id} 🔴 阶段列是阶段标签(StagePill),不是「—」占位`, `阶段列:「${r.stage || '(空)'}」`);
            check(r.risk === 1, `W2.${id} 🔴 风险点(RiskDot)在`, `${r.risk} 个`);
            check(r.comp.length === 1 && r.comp[0].includes(`评分 ${BFF_SCORE[id]} 分`),
                `W3.${id} 🔴 完整度徽标(CompletenessBadge)在,分数 = BFF 夹具值 ${BFF_SCORE[id]}`, r.comp.join(' | ') || '没有徽标');
        }
        check(rec.pageErrors.length === 0, 'W4 我的客户 pageerror 0', rec.pageErrors.slice(0, 2).join(' | ') || '0 个');
        await context.close();
    }

    console.log('\nB 品牌详情:决策条 / 工具卡 / 资料确认徽标');
    {
        const { context, page, rec } = await openPage(browser, 'agent', { path: '/my-clients/201', token: 'qa-fixture-token-agent' });
        await page.getByText('下一步:', { exact: true }).first().waitFor({ state: 'visible', timeout: 20000 }).catch(() => { });
        await page.locator('[aria-label^="写作资料状态:"]').first().waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
        await page.waitForTimeout(800);
        await dismissWelcome(page);
        const barText = (await page.getByText('下一步:', { exact: true }).count())
            ? await page.getByText('下一步:', { exact: true }).first()
                .locator('xpath=ancestor::div[contains(@class,"rounded")][1]')
                .evaluate((el) => (el.innerText || '').replace(/\s+/g, ' ').trim()).catch(() => '')
            : '';
        const badge = await page.locator('[aria-label="写作资料状态: 客户已确认"]').count();
        const trigger = page.getByText('更多客户动作', { exact: true }).first();
        const hasTrigger = await trigger.count();
        if (hasTrigger) await trigger.click({ timeout: 8000 }).catch(() => { });
        const grid = page.locator('[role="list"][aria-label="客户工具网格"]');
        await grid.first().waitFor({ state: 'visible', timeout: 8000 }).catch(() => { });
        const cards = await grid.locator('[role="listitem"]').count();
        const cardText = cards ? await grid.first().evaluate((el) => (el.innerText || '').replace(/\s+/g, ' ')) : '';
        debugDump('B', rec);
        check(asked(rec, '/api/my-clients/201') >= 1 && asked(rec, '/api/client-context/201') >= 1
            && asked(rec, '/api/brands/201/completeness') >= 1 && asked(rec, '/api/m3/material-confirm/status/201') >= 1,
            'B0 分母自证:详情 / 客户上下文 / 完整度 / 资料确认四个请求真发了',
            `详情 ${asked(rec, '/api/my-clients/201')} · 上下文 ${asked(rec, '/api/client-context/201')} · 完整度 ${asked(rec, '/api/brands/201/completeness')} · 资料确认 ${asked(rec, '/api/m3/material-confirm/status/201')}`);
        check(barText.includes('夹具客户甲') && STAGE_LABELS.some((s) => barText.includes(s)) && barText.includes('下一步:'),
            'B1 🔴 决策条(DecisionBarBridge)在:品牌名 + 阶段标签 +「下一步」', `决策条:${barText.slice(0, 90) || '(没读到)'}`);
        check(hasTrigger >= 1 && cards === 11 && cardText.includes('写文章') && cardText.includes('客户档案'),
            'B2 🔴 工具卡(ToolGridCard)在:展开「更多客户动作」后 11 张卡(含写文章 / 客户档案)',
            `展开钮 ${hasTrigger} · 卡 ${cards} 张`);
        check(badge >= 1, 'B3 🔴 资料确认徽标(MaterialConfirmStatusBadge)在:「客户已确认」', `${badge} 个`);
        check(rec.pageErrors.length === 0, 'B4 品牌详情 pageerror 0', rec.pageErrors.slice(0, 2).join(' | ') || '0 个');
        await context.close();
    }

    console.log('\nN 全程路径白名单');
    const ALLOWED = new Set(['/login', '/', '/my-clients', '/my-clients/201']);
    const stray = NAV_ALL.filter((x) => !ALLOWED.has(x.p));
    check(NAV_ALL.length >= 12, 'N0 分母自证:路径记录器真在记(每页至少记到开页那一跳)', `${NAV_ALL.length} 跳`);
    check(stray.length === 0, 'N1 🔴 全程 SPA 只到过本臂打开的页和 /(一处要删路由都没经过)',
        stray.slice(0, 6).map((x) => `${x.who}@${x.entry}: ${tag(x.p)}`).join(' | ') || '没有');
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
