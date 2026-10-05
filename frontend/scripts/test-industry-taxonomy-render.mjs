#!/usr/bin/env node
/**
 * 行为臂 · WO_267-A 行业大类在页面上(真 chromium · 真组件 · 真请求)。
 *
 * 契约照 C 的后端(fix/c14-267 · e169924a6)写桩:
 *   · `GET /api/industry-taxonomy` → `{version, categories:[{key,name,subcategories}]}`;
 *   · 新写入的 `industry_category` 是英文 key;只有部分响应附 `industry_category_name`
 *     (`/api/history`、`/api/my-clients/{id}` 都不附 —— 桩也不附,这正是要测的);
 *   · `/draft` 回 `resolved.category = {key,name,secondary_key,secondary_name,needs_review,source}`,
 *     请求可带 `category_key`;`/active-task`、`/rounds`、主榜可带 `brand_id`;
 *   · `recommend-v2` 回 `research_status = {status: open|not_open|unknown, …}`。
 *
 * 四段,每段末尾都过同一条「英文 key 不上屏」:页面可见文字里不许出现字典里任何一个 key。
 *   H 诊断历史:新 key 翻成中文名、存量中文原样、认不出的 key 不显示;按大类筛选按 key 对上
 *   D 点亮调研弹窗:判出的大类(主 / 兼 / 待确认)上屏;改选 ⇒ 带 category_key 重新预览
 *   P 发布中心:not_open ⇒「该行业的调研尚未开通」;active-task / 主榜 / 历轮 都带 brand_id
 *   B 客户资料:大类下拉只提交 key;**没动下拉就不带这个字段**(空串 = 清空、key = 人工选定)
 *
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
const outDir = mkdtempSync(join(CACHE_ROOT, 'a267-render-'));
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
import { HistoryList } from ${q('src/pages/History/HistoryList.tsx')};
import BrandDetailPage from ${q('src/pages/Brand/BrandDetailPage.tsx')};
import { PublishCenter } from ${q('src/pages/Publishing/PublishCenter.tsx')};
import { ResearchSelfserveDialog } from ${q('src/components/publishing/ResearchSelfserveDialog.tsx')};
import { ClientSwitcherSidebar } from ${q('src/components/layout/ClientSwitcherSidebar.tsx')};

(globalThis as any).__mount = function (el: HTMLElement, entry: string) {
  createRoot(el).render(
    <MemoryRouter initialEntries={[entry]}>
      <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
        <OnboardingProvider><PricingProvider><ClientProvider>
          <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
          <Routes>
            <Route path="/history" element={<HistoryList />} />
            <Route path="/my-clients/:id" element={<BrandDetailPage />} />
            <Route path="/publish" element={<PublishCenter />} />
            <Route path="/dialog" element={
              <ResearchSelfserveDialog open onOpenChange={() => {}} brandId={101}
                industry="光伏组件制造" onCompleted={() => {}} />} />
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
<title>a267</title><link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>`, 'utf8');
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = createServer((req, res) => {
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try { const b = readFileSync(join(outDir, n)); res.writeHead(200, { 'Content-Type': MIME[extname(n)] || 'application/octet-stream' }); res.end(b); }
    catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

/* ── 夹具(全部合成值;品牌 id 101 是夹具 id,不是真客户)───────────────── */
const TAXONOMY = { version: 'industry-taxonomy-test', categories: [
    { key: 'new_energy', name: '新能源', subcategories: ['光伏', '储能'] },
    { key: 'construction', name: '建筑建材', subcategories: ['装修'] },
    { key: 'real_estate', name: '房地产', subcategories: [] },
    { key: 'education', name: '教育培训', subcategories: [] },
] };
const KEYS = [...TAXONOMY.categories.map((c) => c.key), 'zzz_unknown'];
const ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
    id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent',
};
const CLIENTS = [{ id: 101, name: 'A 夹具客户', industry: '光伏组件制造', industry_category: 'new_energy',
    industry_category_key: 'new_energy', industry_category_name: '新能源', diagnosis_count: 1, quote_count: 1 }];
const PROJECTS = [{ id: 11, brand_id: 101, brand_name: 'A 夹具客户', industry: '光伏组件制造', quote_ids: [11], keyword_count: 5 }];
/* 形状照 services/m3/materialConfirm.ts::MaterialsSummary */
const MATERIALS_SUMMARY = { company_name: '', industry: '', intro_excerpt: '', usp_excerpt: '',
    fields_filled: [], fields_missing: [], filled_count: 0, total_fields: 0 };
/* 诊断历史:三种原值各一条,自由文本 industry 故意留空,逼页面走大类那一支 */
const HISTORY = [
    { id: 1, brand_name: 'A 夹具客户', industry: '', industry_category: 'new_energy', total_score: 60, level: 'B', created_at: '2026-09-20T00:00:00Z', diagnosis_type: 'geo' },
    { id: 2, brand_name: 'B 夹具客户', industry: '', industry_category: '房产家居', total_score: 50, level: 'C', created_at: '2026-09-19T00:00:00Z', diagnosis_type: 'geo' },
    { id: 3, brand_name: 'C 夹具客户', industry: '', industry_category: 'zzz_unknown', total_score: 40, level: 'C', created_at: '2026-09-18T00:00:00Z', diagnosis_type: 'geo' },
];
const DRAFT = (categoryKey) => {
    const picked = TAXONOMY.categories.find((c) => c.key === categoryKey);
    return {
        resolved: {
            industry_name: picked ? picked.name : '新能源', industry_key: 'x', is_new: false, resolved_by: 'alias',
            category: picked
                ? { key: picked.key, name: picked.name, secondary_key: null, secondary_name: null, needs_review: false, source: 'override' }
                : { key: 'new_energy', name: '新能源', secondary_key: 'construction', secondary_name: '建筑建材', needs_review: true, source: 'alias' },
        },
        existing_prompts: [], suggested_prompts: ['光伏组件怎么选'], default_price_points: 100,
        price_by_count: { 1: 100 }, pricing_configured: true, industry_last_by_others: null, active_task: null,
    };
};

async function openAt(browser, entry, log) {
    const page = await browser.newPage({ viewport: { width: 1400, height: 1400 } });
    await page.addInitScript(() => { localStorage.setItem('omnirank_token', 'qa-token'); });
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const path = url.pathname;
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        const rec = (k) => { (log[k] = log[k] || []).push({ query: Object.fromEntries(url.searchParams), body: req.postData() || '' }); };
        if (path.includes('/auth/me')) return json(ME);
        if (path === '/api/industry-taxonomy') return json(TAXONOMY);
        if (path === '/api/history') return json({ success: true, data: HISTORY, total: HISTORY.length });
        if (path === '/api/client-context/list') return json({ success: true, clients: CLIENTS });
        const ctx = path.match(/^\/api\/client-context\/(\d+)$/);
        if (ctx) return json({ success: true, context: { brand: { ...CLIENTS[0] }, profile: null } });
        if (path === '/api/writing/projects') return json({ projects: PROJECTS });
        if (path === '/api/publish/media/recommend-v2') {
            rec('recommend');
            return json({ status: 'success', media_vertical: [], media_generic: [], wemedia_vertical: [], wemedia_generic: [],
                matched_industry: '光伏组件制造',
                research_status: { status: 'not_open', category_key: 'new_energy', category_name: '新能源', research_industry: null } });
        }
        if (path === '/api/publish/research/active-task') { rec('active'); return json({ active_task: null, industry_key: 'x' }); }
        if (path === '/api/publish/media/effectiveness-board') { rec('board'); return json({ rows: [], general_rows: [], scope: 'fallback', has_data: false }); }
        if (path === '/api/publish/research/rounds') { rec('rounds'); return json({ rounds: [] }); }
        if (path === '/api/publish/research/draft') {
            rec('draft');
            let key = null;
            try { key = JSON.parse(req.postData() || '{}').category_key || null; } catch { key = null; }
            return json(DRAFT(key));
        }
        /* 客户资料页上的两块子面板:形状照它们的接口声明(CustomerLinksResponse /
           MaterialConfirmStatusResponse)。缺了它们,兜底的「最小成功体」会让整页崩成空白 ——
           第一版就是这样,B 段四格读到的全是空页面,不是产品。 */
        if (path === '/api/defensive-geo/customer-links') {
            return json({ brandId: 101, links: [], hint: '', copyRegistryVersion: 'test' });
        }
        if (/^\/api\/m3\/material-confirm\/status\/\d+$/.test(path)) {
            return json({ status: 'none', has_session: false, token: null, token_url: null, expires_at: null,
                confirmed_at: null, customer_notes: '', materials_summary: MATERIALS_SUMMARY, last_session_id: null });
        }
        const mc = path.match(/^\/api\/my-clients\/(\d+)$/);
        if (mc && req.method() === 'PUT') { rec('put'); return json({ success: true }); }
        if (mc) {
            return json({ success: true, brand: { id: 101, name: 'A 夹具客户', industry: '', industry_category: 'new_energy', cities: '广州' },
                profile: { id: 7, business: '光伏组件' } });
        }
        return json({ status: 'success', success: true, data: [], items: [], projects: [], total: 0 });
    });
    if (process.env.A267_DEBUG) {
        page.on('pageerror', (e) => console.log('  [pageerror]', String(e).slice(0, 300)));
        page.on('console', (m) => { if (m.type() === 'error') console.log('  [console]', m.text().slice(0, 200)); });
    }
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate((e) => globalThis.__mount(document.getElementById('root'), e), entry);
    return page;
}
const visibleText = (page) => page.evaluate(() => (document.body.innerText || ''));
const noKeys = (text) => KEYS.filter((k) => new RegExp(`\\b${k}\\b`).test(text));
const waitText = (page, t, ms = 15000) => page.getByText(t).first().waitFor({ state: 'attached', timeout: ms }).catch(() => { });

const browser = await playwright.chromium.launch();
try {
    console.log('H 诊断历史');
    {
        const log = {};
        const page = await openAt(browser, '/history', log);
        await waitText(page, '查看过往的所有诊断记录');
        await page.waitForTimeout(1500);
        const rows = await page.locator('table tbody tr').count();
        const text = await visibleText(page);
        check(rows === 3, 'H0 分母自证:三条夹具记录都渲染了(否则下面的"没出现"是空话)', `${rows} 行`);
        check(text.includes('新能源'), 'H1 🔴 新写入的英文 key(接口不附中文名)⇒ 按字典翻成「新能源」上屏');
        check(text.includes('房产家居'), 'H2 存量旧中文名 ⇒ 原样上屏');
        const leaked = noKeys(text);
        check(leaked.length === 0, 'H3 🔴 英文 key 不上屏(含认不出的 zzz_unknown)', leaked.join(',') || '无');
        /* 按大类筛选:选「新能源」⇒ 只剩那条新 key 记录(按 key 对上,不是按字面) */
        /* 按触发器上的字认,不按第几个:左上角客户切换器也可能是 combobox,序号会漂 */
        await page.getByRole('combobox').filter({ hasText: '所有行业' }).first().click({ timeout: 5000 }).catch(() => { });
        await page.getByRole('option', { name: '新能源' }).click().catch(() => { });
        await page.waitForTimeout(600);
        const after = await page.locator('table tbody tr').count();
        check(after === 1, 'H4 按大类筛选「新能源」⇒ 只剩新 key 那一条(按 key 对上)', `${after} 行`);
        await page.close();
    }

    console.log('\nD 点亮调研弹窗');
    {
        const log = {};
        const page = await openAt(browser, '/dialog', log);
        await page.locator('[role="dialog"]').first().waitFor({ state: 'attached', timeout: 15000 }).catch(() => { });
        await page.waitForTimeout(1500);
        const first = await page.evaluate(() => {
            const t = (id) => (document.querySelector(`[data-testid="${id}"]`)?.textContent || '').replace(/\s+/g, ' ').trim();
            return { name: t('research-category-name'), secondary: t('research-category-secondary'), review: t('research-category-review'),
                hasSelect: !!document.querySelector('[data-testid="research-category-select"]') };
        });
        check((log.draft || []).length >= 1, 'D0 分母自证:弹窗真的发了 /draft', `${(log.draft || []).length} 次`);
        check(first.name === '新能源' && first.secondary.includes('建筑建材') && first.review === '待确认',
            'D1 🔴 判出的大类上屏:主类「新能源」、兼「建筑建材」、待确认', JSON.stringify(first));
        const bodyBefore = (log.draft || []).length;
        await page.locator('[data-testid="research-category-select"]').selectOption('real_estate').catch(() => { });
        await page.waitForTimeout(1500);
        const again = (log.draft || []).slice(bodyBefore);
        const sentKey = again.map((r) => { try { return JSON.parse(r.body).category_key; } catch { return undefined; } });
        check(first.hasSelect && sentKey.includes('real_estate'),
            'D2 🔴 改选大类 ⇒ 带 category_key 重新 /draft(用户选定的以后为准)', `新请求 ${again.length} 次 · category_key=${sentKey.join(',') || '(没带)'}`);
        const leaked = noKeys(await visibleText(page));
        check(leaked.length === 0, 'D3 🔴 弹窗里英文 key 不上屏', leaked.join(',') || '无');
        await page.close();
    }

    console.log('\nP 发布中心');
    {
        const log = {};
        const page = await openAt(browser, '/publish', log);
        await page.waitForTimeout(2500);
        /* 用真的左上角选客户 —— 与 test-publish-client-scope-render.mjs 同一条路 */
        const bar = page.locator('[data-testid="real-sidebar"]');
        await bar.locator('button').first().click({ timeout: 5000 }).catch(() => { });
        await page.waitForTimeout(400);
        await bar.getByText('A 夹具客户').first().click({ timeout: 5000 }).catch(() => { });
        await page.waitForTimeout(3500);
        const brandOf = (k) => (log[k] || []).map((r) => r.query.brand_id).filter(Boolean);
        check((log.recommend || []).length >= 1, 'P0 分母自证:选中客户后推荐真的请求了', `${(log.recommend || []).length} 次`);
        const notOpen = await page.locator('[data-testid="research-not-open"]').count();
        const txt = notOpen ? await page.locator('[data-testid="research-not-open"]').first().innerText() : '';
        check(notOpen === 1 && txt.includes('该行业的调研尚未开通'),
            'P1 🔴 recommend-v2 回 not_open ⇒ 明说「该行业的调研尚未开通」(不再匹到邻居行业)', txt || '(没有)');
        /* 主榜在「媒体建议与榜单」抽屉里:收起时整块不挂载 —— 像用户一样先展开它 */
        await page.locator('button[data-drawer-handle][aria-expanded="false"]').first().click({ timeout: 5000 }).catch(() => { });
        await page.waitForTimeout(2000);
        check(brandOf('active').includes('101'), 'P2 🔴 在飞任务 /active-task 带 brand_id(与 /draft 同一份行业判定输入)',
            JSON.stringify((log.active || []).map((r) => r.query)).slice(0, 120));
        check(brandOf('board').includes('101'), 'P3 🔴 主榜 effectiveness-board 带 brand_id',
            JSON.stringify((log.board || []).map((r) => r.query)).slice(0, 120));
        await page.locator('[data-testid^="media-board-rounds-entry"]').first().click({ timeout: 5000 }).catch(() => { });
        await page.waitForTimeout(1200);
        check(brandOf('rounds').includes('101'), 'P4 🔴 历轮 /rounds 带 brand_id',
            JSON.stringify((log.rounds || []).map((r) => r.query)).slice(0, 120) || '(没请求)');
        const leaked = noKeys(await visibleText(page));
        check(leaked.length === 0, 'P5 🔴 发布中心英文 key 不上屏', leaked.join(',') || '无');
        await page.close();
    }

    console.log('\nB 客户资料(大类下拉)');
    {
        const log = {};
        const page = await openAt(browser, '/my-clients/101', log);
        await waitText(page, '保存客户资料');
        await page.waitForTimeout(1500);
        if (process.env.A267_DEBUG) {
            const dump = await page.evaluate(() => ({
                ids: [...new Set([...document.querySelectorAll('[data-testid]')].map((e) => e.getAttribute('data-testid')))].slice(0, 40).join(' '),
                text: (document.body.innerText || '').replace(/\s+/g, ' ').slice(0, 400) }));
            console.log('  [debug B]', JSON.stringify(dump));
        }
        const sel = page.locator('[data-testid="industry-category-select"]:visible').first();
        const count = await page.locator('[data-testid="industry-category-select"]:visible').count();
        const value = count ? await sel.inputValue() : '';
        const shown = count ? await sel.evaluate((s) => s.options[s.selectedIndex]?.text || '') : '';
        check(count >= 1 && value === 'new_energy' && shown === '新能源',
            'B1 下拉按字典渲染、选中当前大类(显示中文名,值是 key)', `value=${value} 显示=${shown}`);
        const save = page.getByRole('button', { name: '保存客户资料' });
        await save.click({ timeout: 5000 }).catch(() => { });
        await page.waitForTimeout(1200);
        const firstPut = (log.put || [])[0];
        let firstBody = {};
        try { firstBody = JSON.parse(firstPut?.body || '{}'); } catch { firstBody = {}; }
        check(!!firstPut && !('industry_category' in firstBody),
            'B2 🔴 没动下拉就保存 ⇒ 请求里**不带** industry_category(带了就是替用户清空或锁死)',
            firstPut ? `键:${Object.keys(firstBody).includes('industry_category') ? '带了' : '没带'}` : '(没发 PUT)');
        await sel.selectOption('real_estate').catch(() => { });
        await save.click({ timeout: 5000 }).catch(() => { });
        await page.waitForTimeout(1200);
        const lastPut = (log.put || []).slice(-1)[0];
        let lastBody = {};
        try { lastBody = JSON.parse(lastPut?.body || '{}'); } catch { lastBody = {}; }
        check((log.put || []).length >= 2 && lastBody.industry_category === 'real_estate',
            'B3 🔴 改选后保存 ⇒ 只提交字典 key(real_estate)', `industry_category=${JSON.stringify(lastBody.industry_category)}`);
        const leaked = noKeys(await visibleText(page));
        check(leaked.length === 0, 'B4 🔴 客户资料页英文 key 不上屏', leaked.join(',') || '无');
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
