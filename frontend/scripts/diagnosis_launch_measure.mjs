/**
 * diagnosis_launch_measure.mjs —— 品牌体检发起页 · 四档视口真浏览器实测
 * (工单 WO_DIAGNOSIS_LAUNCH_UI_2026-08-05 §2 硬判据 / §3 验收方式)
 *
 * ## 量的是真页面,不是 mock
 *
 * 2026-08-04 发布中心那单栽过「像素 A/B harness 第一版全绿但全假」——
 * harness 里搭的壳跟真页面不是一回事,差值与视口无关。所以这次:
 *   · 起一个静态服务器直接发 `npm run build` 的 **dist 真产物**(真 CSS、真 chunk),
 *     `/api` `/ws` 反代到本地全栈后端 —— 跟生产 nginx 同形态;
 *   · Playwright 真浏览器**真登录**、真进 `/diagnosis/new`,量的是真实 DOM 的 getBoundingClientRect;
 *   · 顺带把右栏"启动检查"在**填表过程中**是否实时变绿也一并点出来(工单 §1.4)。
 *
 * ## 判据(工单 §2 最后一条)
 *   1. 任何档位 `document.documentElement.scrollWidth <= window.innerWidth`(无横向滚动)
 *   2. 3840 宽下内容区实测占用 ≥ 千位像素级
 *   3. 四档各自的内容容器实宽,与设计的四档限宽一致(880/1600/2200/3000 容器断点)
 *
 * ## 用法
 *   node scripts/diagnosis_launch_measure.mjs              # 起服务 + 跑四档 + 出图,跑完**不关**服务
 *   node scripts/diagnosis_launch_measure.mjs --serve-only # 只起服务(留给 Owner / Review 用浏览器过目)
 *   node scripts/diagnosis_launch_measure.mjs --close      # 跑完就关(CI 用)
 *
 * 环境变量:PORT(默认 4180)· API_TARGET(默认 http://127.0.0.1:8010)
 *          LOGIN_USER / LOGIN_PASS(默认 admin / LocalDiag2026!)
 */
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import process from 'node:process';
import { chromium } from 'playwright';

const root = process.cwd();
const DIST = path.join(root, 'dist');
const PORT = Number(process.env.PORT || 4180);
const API_TARGET = process.env.API_TARGET || 'http://127.0.0.1:8010';
const LOGIN_USER = process.env.LOGIN_USER || 'admin';
const LOGIN_PASS = process.env.LOGIN_PASS || 'LocalDiag2026!';
const OUT = path.join(root, '../agent-test-artifacts/diaglaunch-2026-08-05');
const SERVE_ONLY = process.argv.includes('--serve-only');
const CLOSE_AFTER = process.argv.includes('--close');

if (!fs.existsSync(path.join(DIST, 'index.html'))) {
    console.error('🔴 没有 dist/index.html —— 先跑 npm run build。量 mock 不算数。');
    process.exit(1);
}

// ---------------------------------------------------------------- 静态 + 反代(生产 nginx 的同形态最小版)
const MIME = {
    '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.ico': 'image/x-icon', '.woff': 'font/woff', '.woff2': 'font/woff2', '.map': 'application/json',
};
const target = new URL(API_TARGET);

const server = http.createServer((req, res) => {
    const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
    if (url.pathname.startsWith('/api') || url.pathname.startsWith('/ws')) {
        const proxied = http.request({
            hostname: target.hostname, port: target.port, path: req.url, method: req.method,
            headers: { ...req.headers, host: `${target.hostname}:${target.port}` },
        }, up => { res.writeHead(up.statusCode || 502, up.headers); up.pipe(res); });
        proxied.on('error', err => { res.writeHead(502); res.end(`proxy error: ${err.message}`); });
        req.pipe(proxied);
        return;
    }
    let file = path.join(DIST, decodeURIComponent(url.pathname));
    if (!fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(DIST, 'index.html'); // SPA fallback
    const body = fs.readFileSync(file);
    res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
    res.end(body);
});

await new Promise(resolve => server.listen(PORT, '127.0.0.1', resolve));
console.log(`静态 dist + /api 反代已起:http://127.0.0.1:${PORT}  → ${API_TARGET}`);

if (SERVE_ONLY) {
    console.log('--serve-only:不跑浏览器,进程留着。Ctrl-C 结束。');
    await new Promise(() => {});
}

// ---------------------------------------------------------------- 真浏览器四档
fs.mkdirSync(OUT, { recursive: true });
const VIEWPORTS = [
    // 🔴 工单点名的四档是 1280/1600/1920/3840。多量两档窄的,是**反向对照**:
    //    只量宽档的话,"三档独立 layout"里的单列那一档等于没验过 ——
    //    容器查询即便完全失效,宽档也照样好看。
    { w: 820, h: 1180, tag: 'narrow-820' },
    { w: 1024, h: 900, tag: 'lg-1024' },
    { w: 1280, h: 900, tag: 'lg-1280' },
    { w: 1600, h: 1000, tag: 'xl-1600' },
    { w: 1920, h: 1080, tag: 'xl-1920' },
    { w: 3840, h: 2160, tag: '4k-3840' },
];

const browser = await chromium.launch();
const rows = [];
let failed = 0;
const check = (name, ok, detail = '') => {
    console.log(`${ok ? '  ✅' : '  🔴'} ${name}${detail ? ` — ${detail}` : ''}`);
    if (!ok) failed++;
};

for (const vp of VIEWPORTS) {
    const ctx = await browser.newContext({ viewport: { width: vp.w, height: vp.h }, deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    const consoleErrors = [];
    page.on('console', m => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 200)); });
    // 把 404 的真实 URL 记下来 —— 只报「有 2 条 404」等于没报
    const failedRequests = [];
    page.on('response', r => { if (r.status() >= 400) failedRequests.push(`${r.status()} ${r.url().replace(`http://127.0.0.1:${PORT}`, '')}`); });

    // 真登录:走真接口拿 token,再塞进 localStorage(和前端 lib/api.ts 的 TOKEN_KEY 一致)
    await page.goto(`http://127.0.0.1:${PORT}/login`, { waitUntil: 'domcontentloaded' });
    const token = await page.evaluate(async ([u, p]) => {
        const r = await fetch('/api/auth/login', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: u, password: p }),
        });
        const d = await r.json();
        if (!d.token) throw new Error('登录没拿到 token:' + JSON.stringify(d).slice(0, 200));
        localStorage.setItem('omnirank_token', d.token);
        return d.token;
    }, [LOGIN_USER, LOGIN_PASS]);
    if (!token) throw new Error('登录失败');

    await page.goto(`http://127.0.0.1:${PORT}/diagnosis/new`, { waitUntil: 'networkidle' });
    await page.waitForSelector('[data-testid="diagnosis-launch-shell"]', { timeout: 30000 });

    // 填表,顺便看右栏"启动检查"是不是实时联动(工单 §1.4)
    const checklistBefore = await page.$$eval('[data-testid="checklist-item"]', els => els.map(e => e.dataset.ok));
    await page.fill('#brandName', '本地演示·深圳怡然瑜伽');
    await page.fill('#industry', '瑜伽馆');
    await page.fill('#client-location', '深圳');
    await page.click('[data-testid="use-all-examples"]');
    await page.waitForTimeout(300);
    const checklistAfter = await page.$$eval('[data-testid="checklist-item"]', els => els.map(e => e.dataset.ok));
    const questionCountText = await page.textContent('[data-testid="question-count"]');
    const exampleQs = await page.$$eval('[data-testid="example-question"]', els => els.map(e => e.textContent.trim()));
    const exampleSource = await page.getAttribute('[data-testid="question-example-card"]', 'data-source');

    const m = await page.evaluate(() => {
        const r = sel => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const b = el.getBoundingClientRect();
            return { w: Math.round(b.width), h: Math.round(b.height), left: Math.round(b.left) };
        };
        const main = document.querySelector('[data-testid="app-page-main"]');
        const grid = document.querySelector('[data-testid="launch-grid"]');
        return {
            innerWidth: window.innerWidth,
            docScrollWidth: document.documentElement.scrollWidth,
            bodyScrollWidth: document.body.scrollWidth,
            mainAreaWidth: main ? Math.round(main.getBoundingClientRect().width) : null,
            shell: r('[data-testid="diagnosis-launch-shell"]'),
            grid: r('[data-testid="launch-grid"]'),
            aside: r('[data-testid="launch-aside"]'),
            mainCol: r('[data-testid="launch-main-col"]'),
            gridColumns: grid ? getComputedStyle(grid).gridTemplateColumns : null,
            asidePosition: getComputedStyle(document.querySelector('[data-testid="launch-aside"]')).position,
            ctaPosition: getComputedStyle(document.querySelector('[data-testid="launch-cta"]')).position,
            stepBarSteps: [...document.querySelectorAll('[data-testid^="launch-step-"]')].filter(e => e.dataset.state).map(e => e.dataset.state),
        };
    });

    const shot = path.join(OUT, `${vp.tag}.png`);
    await page.screenshot({ path: shot, fullPage: false });
    const shotFull = path.join(OUT, `${vp.tag}-full.png`);
    await page.screenshot({ path: shotFull, fullPage: true });

    rows.push({ vp, m, checklistBefore, checklistAfter, questionCountText, exampleQs, exampleSource, consoleErrors, failedRequests, shot });
    await ctx.close();
}
await browser.close();

// ---------------------------------------------------------------- 判据
console.log('\n================ 四档实测 ================');
for (const r of rows) {
    const { vp, m } = r;
    console.log(`\n--- ${vp.tag}(视口 ${vp.w}×${vp.h})`);
    console.log(`    内容区(main)实宽      = ${m.mainAreaWidth}px`);
    console.log(`    限宽容器 shell 实宽   = ${m.shell?.w}px  (占内容区 ${(m.shell.w / m.mainAreaWidth * 100).toFixed(1)}%)`);
    console.log(`    主列 / 右栏           = ${m.mainCol?.w}px / ${m.aside?.w}px`);
    console.log(`    grid-template-columns = ${m.gridColumns}`);
    console.log(`    右栏 position         = ${m.asidePosition} · CTA position = ${m.ctaPosition}`);
    console.log(`    三步条状态            = ${JSON.stringify(m.stepBarSteps)}`);
    console.log(`    启动检查 填表前→填表后 = ${JSON.stringify(r.checklistBefore)} → ${JSON.stringify(r.checklistAfter)}`);
    console.log(`    实时计数 / 示例来源    = ${r.questionCountText} / ${r.exampleSource}`);
    console.log(`    示例问题              = ${r.exampleQs.join(' | ')}`);
    console.log(`    截图                  = ${path.relative(root, r.shot)}`);
    if (r.failedRequests.length) console.log(`    ⚠️ 4xx/5xx 请求:${[...new Set(r.failedRequests)].join(' , ')}`);
}

console.log('\n================ 判据 ================');
for (const r of rows) {
    const { vp, m } = r;
    check(`${vp.tag} 无横向滚动(docScrollWidth ${m.docScrollWidth} ≤ innerWidth ${m.innerWidth})`,
        m.docScrollWidth <= m.innerWidth);
    check(`${vp.tag} 右栏检查项随表单实时变绿`,
        r.checklistBefore.join('') !== r.checklistAfter.join('') && r.checklistAfter.filter(v => v === '1').length >= 3,
        `${r.checklistBefore.join('')} → ${r.checklistAfter.join('')}`);
}
const fourK = rows.find(r => r.vp.w === 3840);
check('3840 宽下内容区实测占用 ≥ 千位像素级', fourK.m.shell.w >= 1000, `${fourK.m.shell.w}px`);
check('3840 档比 1280 档明显更宽(不是等比放大出来的假宽)',
    fourK.m.shell.w > rows.find(r => r.vp.w === 1280).m.shell.w + 400,
    `${rows.find(r => r.vp.w === 1280).m.shell.w}px → ${fourK.m.shell.w}px`);
check('1280 档右栏没被挤没(两栏成立)', rows.find(r => r.vp.w === 1280).m.aside.w >= 300);
// 反向对照:四档的量值不能全一样(全一样说明容器查询压根没生效,判据就成了摆设)
check('反向对照:四档 shell 实宽不是同一个数',
    new Set(rows.map(r => r.m.shell.w)).size >= 3,
    rows.map(r => `${r.vp.tag}=${r.m.shell.w}`).join(' '));

const summary = { measuredAt: new Date().toISOString(), viewports: rows.map(r => ({ tag: r.vp.tag, ...r.m, checklistBefore: r.checklistBefore, checklistAfter: r.checklistAfter, exampleQs: r.exampleQs, exampleSource: r.exampleSource })) };
fs.writeFileSync(path.join(OUT, 'measure.json'), JSON.stringify(summary, null, 2));
console.log(`\n量值已写 ${path.relative(root, path.join(OUT, 'measure.json'))}`);

if (failed) {
    console.log(`\n🔴 ${failed} 条判据没过`);
    if (CLOSE_AFTER) { server.close(); process.exit(1); }
    process.exitCode = 1;
} else {
    console.log('\n✅ 四档判据全过');
}

if (CLOSE_AFTER) { server.close(); }
else {
    console.log(`\n环境留着:http://127.0.0.1:${PORT}/diagnosis/new(账号 ${LOGIN_USER} / ${LOGIN_PASS})· Ctrl-C 结束`);
    await new Promise(() => {});
}
