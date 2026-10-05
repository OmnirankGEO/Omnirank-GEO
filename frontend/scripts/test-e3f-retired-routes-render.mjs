#!/usr/bin/env node
/**
 * 行为臂 · 开源 E3 前端(WO_322)· 2026-10-02:删掉的入口在真浏览器里落到在役兜底,不白屏、不崩。
 * 结构臂 = test-route-literals-live.mjs(build 链,S3 管「站内链接都解析到现存路由」);
 * 本臂管「用户手里的旧书签 / 外链 / 旧二维码直接打开时看到什么」。
 *
 * 🔴 打包的是 **src/main.tsx 本身**(真引导 · App.tsx 真路由表 · 真外壳),接口全部在浏览器里拦截、夹具全合成
 *    (用户 9102 / 9103 是夹具 id,不是真客户;请求从不出本机)。生产模式打包。
 *
 *   R1 旧入口直达(已登录服务商;管理员格单列),逐个地址断言**落点**:
 *      NF  受保护组内的「这个页面不存在」兜底页(带侧栏,有「回首页」)——
 *          /social-ops · /social-ops/workshop · /social · /social/login · /social-studio/x · /agent ·
 *          /advisors · /advisors/manage · /advisors/chat/a1 · /s
 *      SEL 落到在役的客户选词报价 /s/:token,把旧段名当令牌去查 → 后端 404 → 「链接不存在」——
 *          /s/login · /s/wallet(删前它们是社媒登录 / 社媒钱包)
 *      HOME 原有的重定向照旧到首页(对照:本班没动它们)—— /c/chat · /m3/sales/today
 *      每格:.1 落点对 · .2 页面不是白屏(#root 有可见文字)· .3 pageerror 0
 *   R2 员工大厅(管理员;它在冻结废弃表 D 桶,仍挂路由):页面真渲染了(标题「员工大厅」),「顾问团」按钮不在
 *   R3 全程 console error 0(SEL 格里那一次 /api/s/<段> 的 404 资源报错是被测行为本身,单列放行、计数核对)
 *
 * 对照臂:`E3F_ROOT=<另一棵 frontend 源码树>` 跑同一臂 —— 基线树上 /advisors 渲染顾问团、/s/login 是社媒登录页、
 *   员工大厅有「顾问团」按钮,R1/R2 必须红(证明本臂分得出删前删后)。
 * 不进 build 链(要 chromium + 先 npm run build 取真 CSS),挂 browser:arms。
 * 三态退出码:0 全过 / 1 有失败 / 3 判据不可用。
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync, mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const HERE = join(dirname(fileURLToPath(import.meta.url)), '..');
const ROOT = process.env.E3F_ROOT ? resolve(process.env.E3F_ROOT) : HERE;   // 被量的源码树
const CACHE_ROOT = join(HERE, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const require_ = createRequire(import.meta.url);
const outDir = mkdtempSync(join(CACHE_ROOT, 'e3f-render-'));
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

/* ── 打包:被量树的 src/main.tsx(依赖从本树 node_modules 解析)───────────── */
const viteShims = {
    name: 'vite-shims',
    setup(build) {
        build.onResolve({ filter: /\?raw$/ }, (args) => {
            const bare = args.path.replace(/\?raw$/, '');
            const abs = bare.startsWith('@/') ? join(ROOT, 'src', bare.slice(2)) : join(args.resolveDir, bare);
            return { path: abs, namespace: 'vite-raw' };
        });
        build.onLoad({ filter: /.*/, namespace: 'vite-raw' }, (args) => ({ contents: readFileSync(args.path, 'utf8'), loader: 'text' }));
        build.onResolve({ filter: /\.css$/ }, (args) => ({ path: args.path, namespace: 'css-stub' }));
        build.onLoad({ filter: /.*/, namespace: 'css-stub' }, () => ({ contents: '', loader: 'js' }));
    },
};
const ENV = { DEV: false, PROD: true, MODE: 'production', BASE_URL: '/', SSR: false };
try {
    await esbuild.build({
        entryPoints: [join(ROOT, 'src', 'main.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        nodePaths: [join(HERE, 'node_modules')],
        define: { 'process.env.NODE_ENV': '"production"', global: 'globalThis', 'import.meta.env': JSON.stringify(ENV) },
        loader: {
            '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.jpeg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl',
            '.svg': 'dataurl', '.gif': 'dataurl', '.mp4': 'dataurl', '.woff': 'dataurl', '.woff2': 'dataurl',
        },
        plugins: [viteShims], logLevel: 'silent',
    });
} catch (err) { unusable(`打包 ${ROOT}/src/main.tsx 失败`, (err && err.message) || err); }
try {
    const dir = join(HERE, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, c[0].f), 'utf8'), 'utf8');
} catch (err) { unusable('取不到真 CSS —— 先 npm run build', err); }
writeFileSync(join(outDir, 'index.html'), `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>e3f</title><link rel="stylesheet" href="/app.css"></head>
<body><div id="root"></div><script src="/bundle.js"></script></body></html>`, 'utf8');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
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

/* ── 夹具(全合成)────────────────────────────────────────────────────── */
const baseUser = { is_active: 1, must_change_password: 0, roles: [], permissions: [], client_brand_ids: [] };
const USERS = {
    agent: { ...baseUser, id: 9102, username: 'qa-fixture-l1', display_name: '夹具服务商', is_admin: false, agent_level: 1 },
    admin: { ...baseUser, id: 9103, username: 'qa-fixture-admin', display_name: '夹具管理员', is_admin: true, agent_level: 1 },
};
const MODE = {
    agent: { user_id: 9102, is_admin: false, agent_level: 1, preferred_mode: null, recommended_route: '/', m3_enabled: false, can_switch: true },
    admin: { user_id: 9103, is_admin: true, agent_level: 1, preferred_mode: null, recommended_route: '/', m3_enabled: false, can_switch: true },
};
const HOME_STATS = { status: 'success', nickname: '', geo: { brand_count: 0, article_count: 0, published_count: 0, diagnosis_count_month: 0 }, balance: { total: 1000 } };
const GENERIC = { status: 'success', success: true, data: [], items: [], list: [], projects: [], clients: [], employees: [], advisors: [], total: 0 };
const ONBOARD = { welcome_choice: 'never', version: 1, completed_steps: [], skipped_steps: [], dismissed_features: [], viewed_videos: [],
    first_seen_at: '2026-09-01T00:00:00Z', last_updated_at: '2026-09-01T00:00:00Z' };

function respond(who, path, method) {
    if (path === '/api/auth/me') return [200, { success: true, user: USERS[who] }];
    if (path === '/api/c-end/settings/mode') return [200, MODE[who]];
    if (path === '/api/user/home-stats') return [200, HOME_STATS];
    if (/^\/api\/s\/[^/]+$/.test(path) && method === 'GET') return [404, { detail: '链接不存在' }];   // 选词报价:旧段名当令牌查不到
    return [200, GENERIC];
}

async function open(browser, who, path) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    const rec = { consoleErrors: [], pageErrors: [], s404: 0 };
    await page.addInitScript(({ token, onboard }) => {
        try {
            localStorage.setItem('omnirank_token', token);
            localStorage.setItem('omnirank_onboarding_state', onboard);
            localStorage.setItem('omnirank_screenshot_mode', '1');
        } catch { /* 隐私模式 */ }
    }, { token: `qa-fixture-token-${who}`, onboard: JSON.stringify(ONBOARD) });
    page.on('console', (m) => { if (m.type() === 'error') rec.consoleErrors.push(m.text().slice(0, 240)); });
    page.on('pageerror', (e) => rec.pageErrors.push(String(e).slice(0, 240)));
    await context.route('**/api/**', async (route) => {
        const req = route.request();
        const p = new URL(req.url()).pathname;
        const [status, body] = respond(who, p, req.method());
        if (status === 404) rec.s404 += 1;
        return route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    });
    await page.goto(ORIGIN + path);
    await page.waitForLoadState('networkidle').catch(() => { });
    await page.waitForTimeout(600);
    return { context, page, rec };
}
async function dismissWelcome(page) {
    const dlg = page.getByRole('dialog').filter({ hasText: '欢迎使用全域上榜' });
    if (!(await dlg.count()) || !(await dlg.first().isVisible().catch(() => false))) return;
    await dlg.first().getByText('我已经用过', { exact: true }).first().click({ timeout: 5000 }).catch(() => { });
    await dlg.first().waitFor({ state: 'hidden', timeout: 5000 }).catch(() => { });
    await page.waitForTimeout(300);
}
const visibleText = (page) => page.evaluate(() => (document.getElementById('root')?.innerText || '').trim().length);
/** 404 资源报错是 SEL 格被测行为本身:只放行「恰好对应 /api/s/<段> 的那几次」,其余照算 */
const realConsole = (rec) => rec.consoleErrors.filter((e) => !/status of 404/.test(e));

const NF = ['/social-ops', '/social-ops/workshop', '/social', '/social/login', '/social-studio/x', '/agent',
    '/advisors', '/advisors/manage', '/advisors/chat/a1', '/s'];
const SEL = ['/s/login', '/s/wallet'];
const HOME = ['/c/chat', '/m3/sales/today'];

let browser;
try { browser = await playwright.chromium.launch(); } catch (err) { unusable('chromium 起不来', err); }
const allConsole = [];
let allowed404 = 0, seen404Console = 0;
try {
    console.log(`被量源码树:${ROOT}`);
    console.log('R1 旧入口直达');
    for (const [group, list] of [['NF', NF], ['SEL', SEL], ['HOME', HOME]]) {
        for (const path of list) {
            const { context, page, rec } = await open(browser, 'agent', path);
            await dismissWelcome(page);
            const final = new URL(page.url()).pathname;
            let landed = false, detail = '';
            if (group === 'NF') {
                landed = await page.getByTestId('route-not-found').isVisible().catch(() => false);
                detail = `落点 ${final}${landed ? ' · 「这个页面不存在」' : ''}`;
            } else if (group === 'SEL') {
                landed = final === path && (await page.getByText('链接不存在', { exact: false }).first().isVisible().catch(() => false));
                detail = `落点 ${final}${landed ? ' · 选词报价「链接不存在」' : ''}`;
            } else {
                landed = final === '/' && !(await page.getByTestId('route-not-found').isVisible().catch(() => false));
                detail = `落点 ${final}`;
            }
            check(landed, `R1.${group} ${path} ⇒ ${group === 'NF' ? '页面不存在兜底' : group === 'SEL' ? '选词报价「链接不存在」' : '首页'}`, detail);
            check((await visibleText(page)) > 0, `R1.${group} ${path} 不是白屏`);
            check(rec.pageErrors.length === 0, `R1.${group} ${path} pageerror 0`, rec.pageErrors.slice(0, 2).join(' | '));
            if (group === 'SEL') { allowed404 += rec.s404; seen404Console += rec.consoleErrors.length - realConsole(rec).length; }
            allConsole.push(...realConsole(rec).map((e) => `${path}: ${e}`));
            await context.close();
        }
    }

    console.log('\nR2 员工大厅(管理员)');
    {
        const { context, page, rec } = await open(browser, 'admin', '/employees');
        await dismissWelcome(page);
        const rendered = await page.getByRole('heading', { name: '员工大厅' }).first().isVisible().catch(() => false);
        check(rendered, 'R2.1 员工大厅真渲染了(标题「员工大厅」)—— 分母自证:否则「按钮不在」恒真', new URL(page.url()).pathname);
        const btn = await page.getByRole('button', { name: '顾问团' }).count();
        check(btn === 0, 'R2.2 「顾问团」按钮不在(顾问团前端随 WO_322 删,入口同删)', `${btn} 个`);
        check(rec.pageErrors.length === 0, 'R2.3 pageerror 0', rec.pageErrors.slice(0, 2).join(' | '));
        allConsole.push(...realConsole(rec).map((e) => `/employees: ${e}`));
        await context.close();
    }

    console.log('\nR3 console');
    check(allowed404 === SEL.length && seen404Console <= allowed404,
        'R3.1 放行的 404 资源报错恰好来自 SEL 格那几次 /api/s/<段> 查询(不多放)', `查询 ${allowed404} 次 · 对应报错 ${seen404Console} 条`);
    check(allConsole.length === 0, 'R3.2 其余 console error 0', allConsole.slice(0, 4).join(' | '));
} finally {
    await browser.close();
    server.close();
}
console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
