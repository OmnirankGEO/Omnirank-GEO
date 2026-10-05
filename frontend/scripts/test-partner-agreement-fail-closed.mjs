#!/usr/bin/env node
/**
 * 行为臂 · WO_268 §4 服务商申请第 3 步:协议拉不到就 fail-closed(真 chromium + dist 真 CSS)。
 *
 * 事实(WO_268,Review 生产只读取证):07-12 起生产镜像里没有协议文件,`/api/partner/agreement/v2.3`
 * 恒 404;而页面把拉协议失败**静默吞掉**、`canSubmit` 不看协议 ⇒ 用户在**空白协议框**下
 * 照样能点「确认签署并提交申请」,提交才 404。
 *
 * 🔴 三种回包各跑一遍,**表单都填满**(九条承诺 + 敏感信息同意 + 签名 = 真实姓名 + 验证码):
 *    不填满的话「提交灰着」可以只因为没勾承诺 —— 那一格恒真,证明不了协议闸。
 *      · 404            ⇒ 明说「协议加载失败,请刷新或联系客服」且提交灰;
 *      · 200 但正文为空 ⇒ 同上(空白协议框就是这次事故的样子);
 *      · 200 有正文     ⇒ 正文上屏、没有失败文案、**提交可点**(对照臂:同一套填表能点亮提交,
 *                          否则前两格的「灰」可能是别的条件在灰)。
 * 🔴 等的是卡片标题「《服务商申请协议 v2.3》」(与断言无关、真假都在的稳定锚),不等被断言的东西。
 *
 * 夹具全是合成值:姓名「测试服务商甲」、证件号全 0、上传 key 形如 qa/*。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a268-render-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const require_ = createRequire(import.meta.url);
let failed = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 1200));
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
import PartnerApplyStep3 from ${q('src/pages/Partner/PartnerApplyStep3.tsx')};
/* 🔴 必须挂在 AuthProvider 里:authFetch 在会话被 /auth/me 确认之前**一直等**
   (awaitConfirmedSessionToken),不挂的话协议请求根本发不出去,页面永远停在「加载协议…」——
   第一版就是这样:三种回包读数一模一样,全是"还在加载",测的不是本单要测的那条路。 */
(globalThis as any).__mount = function (el: HTMLElement) {
  createRoot(el).render(
    <MemoryRouter initialEntries={['/partner/apply/agreement']}>
      <AuthProvider>
        <Routes>
          <Route path="/partner/apply/agreement" element={<PartnerApplyStep3 />} />
          <Route path="*" element={<div data-testid="navigated-away" />} />
        </Routes>
      </AuthProvider>
    </MemoryRouter>);
};
`, 'utf8');

/* `?raw` 是 Vite 的文本导入(authFetch 那条链会带进 sandbox 的 `demoReport.md?raw`),esbuild 不认,照 Vite 语义补上 */
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
} catch (err) { unusable('取不到真 CSS —— 没有它,「看得见」量不了', err); }

writeFileSync(join(outDir, 'index.html'), `<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>a268</title><link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>`, 'utf8');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = createServer((req, res) => {
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try { const b = readFileSync(join(outDir, n)); res.writeHead(200, { 'Content-Type': MIME[extname(n)] || 'application/octet-stream' }); res.end(b); }
    catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

const DRAFT = {
    real_name: '测试服务商甲', id_card_no: '000000000000000000',
    promotion_scenes: ['线上'], expected_monthly_customers: '1-5', remark: '',
    front_upload: { oss_key: 'qa/front.jpg' }, back_upload: { oss_key: 'qa/back.jpg' }, selfie_upload: null,
};
const AGREEMENT = '# 服务商申请协议 v2.3\n\n第一条 本协议为测试夹具正文。';
/* 形状照 image-note 渲染臂的 ME 桩;申请人是普通用户(agent_level 0),正要申请成为服务商 */
const ME = {
    success: true,
    user: { id: 113, username: 'qa-applicant', role: 'user', agent_level: 0, is_admin: false, permissions: [], user_mode: 'agent' },
    id: 113, username: 'qa-applicant', role: 'user', agent_level: 0, is_admin: false, permissions: [], user_mode: 'agent',
};

async function openStep3(browser, agreement) {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1400 } });
    if (process.env.A268_DEBUG) {
        page.on('pageerror', (e) => console.log('  [pageerror]', String(e).slice(0, 300)));
        page.on('console', (m) => { if (m.type() === 'error') console.log('  [console]', m.text().slice(0, 300)); });
        page.on('request', (r) => { if (r.url().includes('/api/')) console.log('  [req]', r.method(), new URL(r.url()).pathname); });
    }
    await page.addInitScript((draft) => {
        localStorage.setItem('omnirank_token', 'qa-token');
        sessionStorage.setItem('partner_apply_draft', JSON.stringify(draft));
    }, DRAFT);
    await page.route('**/api/**', (route) => {
        const path = new URL(route.request().url()).pathname;
        const json = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
        if (path.includes('/auth/me')) return json(200, ME);
        if (/\/api\/partner\/agreement\/v2\.3$/.test(path)) {
            agreement.requested = (agreement.requested || 0) + 1;
            return json(agreement.status, agreement.body);
        }
        return json(200, { status: 'success' });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate(() => globalThis.__mount(document.getElementById('root')));
    await page.getByText('《服务商申请协议 v2.3》').first().waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
    await page.waitForTimeout(800);
    /* 像用户一样把表单填满:九条承诺 + 敏感信息同意 + 签名 + 验证码 */
    for (const box of await page.locator('input[type="checkbox"]').all()) {
        if (!(await box.isChecked())) await box.check().catch(() => { });
    }
    await page.getByPlaceholder(`请输入：${DRAFT.real_name}`).fill(DRAFT.real_name).catch(() => { });
    await page.getByPlaceholder('输入短信中的验证码').fill('123456').catch(() => { });
    await page.waitForTimeout(300);
    if (process.env.A268_DEBUG) {
        const box = await page.evaluate(() => {
            const p = document.querySelector('.prose');
            return p ? p.innerHTML.slice(0, 400) : '(没有 .prose)';
        });
        console.log('  [debug] 协议框:', box);
    }
    return page;
}

const readStep3 = (page) => page.evaluate(() => {
    const submit = [...document.querySelectorAll('button')].find((b) => (b.textContent || '').includes('确认签署并提交申请'));
    const boxes = [...document.querySelectorAll('input[type="checkbox"]')];
    const failedEl = document.querySelector('[data-testid="partner-agreement-failed"]');
    const r = failedEl ? failedEl.getBoundingClientRect() : null;
    return {
        away: !!document.querySelector('[data-testid="navigated-away"]'),
        boxes: boxes.length, checked: boxes.filter((b) => b.checked).length,
        inputs: [...document.querySelectorAll('input:not([type="checkbox"])')].map((i) => i.value),
        submitFound: !!submit, submitDisabled: submit ? submit.disabled : null,
        failedText: failedEl ? (failedEl.textContent || '').replace(/\s+/g, ' ').trim() : '',
        failedVisible: !!r && r.width > 0 && r.height > 0,
        agreementShown: (document.body.innerText || '').includes('本协议为测试夹具正文'),
        loading: (document.body.innerText || '').includes('加载协议'),
    };
});

const CASES = [
    { id: 'P1', why: '404(生产现状)', agreement: { status: 404, body: { detail: '协议版本不存在' } }, expectBlocked: true },
    { id: 'P1b', why: '200 但正文为空(空白协议框)', agreement: { status: 200, body: { version: 'v2.3', text: '' } }, expectBlocked: true },
    { id: 'P2', why: '200 有正文(对照臂)', agreement: { status: 200, body: { version: 'v2.3', text: AGREEMENT } }, expectBlocked: false },
];

const browser = await playwright.chromium.launch();
try {
    console.log('WO_268 §4 服务商申请第 3 步 · 协议 fail-closed');
    for (const c of CASES) {
        const page = await openStep3(browser, c.agreement);
        const v = await readStep3(page);
        /*
         * 分母自证:① 协议请求**真的发出去了**、页面已离开「加载协议…」——
         *   第一版没有这一条,三种回包的读数全是"还在加载",红的是 harness 不是产品;
         * ② 表单确实填满了,否则「提交灰着」可以只因为没勾承诺。
         */
        check((c.agreement.requested || 0) >= 1 && !v.loading && !v.away && v.submitFound
            && v.boxes >= 10 && v.checked === v.boxes
            && v.inputs.includes(DRAFT.real_name) && v.inputs.includes('123456'),
            `${c.id}-0 分母自证(${c.why}):协议请求已发出且不在加载中、仍在第 3 步、承诺与同意全勾、签名与验证码已填`,
            `请求 ${c.agreement.requested || 0} 次 · ${v.loading ? '🔴 还在加载' : '已加载完'} · 勾 ${v.checked}/${v.boxes}`
            + ` · 输入=${JSON.stringify(v.inputs)}${v.away ? ' · 🔴 被导走了' : ''}`);
        if (c.expectBlocked) {
            check(v.failedVisible && v.failedText.includes('协议加载失败') && v.failedText.includes('请刷新或联系客服'),
                `${c.id} 🔴 ${c.why} ⇒ 明说「协议加载失败,请刷新或联系客服」`, v.failedText || '(没有)');
            check(v.submitDisabled === true,
                `${c.id}s 🔴 ${c.why} ⇒ 表单全填了提交也点不了(不许在空白协议框下签署)`,
                `提交${v.submitDisabled ? '灰' : '可点'}`);
        } else {
            check(v.agreementShown && !v.failedText,
                `${c.id} 协议正文上屏、没有失败文案`, v.agreementShown ? '正文在' : '🔴 正文没上屏');
            check(v.submitDisabled === false,
                `${c.id}s 🔴 对照:同一套填表在协议正常时能点亮提交(否则前两格的「灰」证明不了协议闸)`,
                `提交${v.submitDisabled ? '灰' : '可点'}`);
        }
        await page.close();
    }
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
