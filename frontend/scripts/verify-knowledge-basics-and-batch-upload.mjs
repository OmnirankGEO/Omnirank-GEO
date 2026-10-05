#!/usr/bin/env node
/**
 * #220 a1 门 · 基础资料表二次打开为空 + 客户图片批量上传。
 *
 * 🔴 **行为门,不是文本门**。本单两件缺陷的形态都是「代码里那行在,但它从不跑」:
 *    ⓐ `refreshKnowledgeAutofill` 一直存在,只是全仓唯一调用点在上传后的轮询体里;
 *    ⓑ `handleUpload` 一直在,只是只取 `files[0]`。
 *    grep「有没有这个函数」对这两件都恒绿 ⇒ 必须真挂页面、真点、真数请求。
 *
 * 三态退出:0 全绿 · 1 有判据红 · 3 **门自己没跑成**(浏览器/打包/真 CSS 取不到)。
 * 🔴 3 和 1 必须分开:「我没跑」被读成「我跑了没事」是这套门最贵的一种假绿。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer, request as httpRequest } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import {
    readStructureSources, runStructureCriteria, STRUCTURE_CRITERIA_COUNT,
} from './lib/a220-structure-criteria.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const require_ = createRequire(import.meta.url);

let failures = 0;
let ran = 0;
const ok = (cond, name, detail) => {
    ran += 1;
    if (!cond) failures += 1;
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${name}${detail !== undefined ? ` — ${detail}` : ''}`);
};
function cannotRun(what, err) {
    console.log(`\n3 门没跑成:${what} —— ${String((err && err.message) || err).slice(0, 400)}`);
    console.log('   (退出码 3 = 本门这次**没有测过任何东西**,不要当成通过)');
    process.exit(3);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { cannotRun('esbuild / playwright 取不到', err); }

const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a220-gate-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const q = (rel) => JSON.stringify(join(ROOT, rel).split('\\').join('/'));

/* ── 0. 结构面 ─────────────────────────────────────────────────────────
 * 🔴 判据本体搬到 `lib/a220-structure-criteria.mjs`,**行为门与结构臂共用同一份**。
 *    结构臂(verify-knowledge-basics-structure.mjs)进 build 链,本门进不了 —— 它起浏览器。
 *    不抄一份的理由:同一个谓词落两个文件,早晚一边改一边不改,而两边都是绿的。
 *    格名(I6a/I6/I7/I8a/I8b/K4a)逐字不变 —— 注毒表按名字断言期望红。
 */
console.log('I0 结构:两个上传入口都要能多选(分母自证)');
let structureSources;
try { structureSources = readStructureSources(ROOT); }
catch (err) { cannotRun('结构面源文件读不到', err); }
const structureRan = runStructureCriteria(ok, structureSources);
if (structureRan !== STRUCTURE_CRITERIA_COUNT) {
    cannotRun(`结构面只跑了 ${structureRan} 格,应为 ${STRUCTURE_CRITERIA_COUNT} 格`, '有人删了格');
}

/* ── 1. 打包真页面 ─────────────────────────────────────────────────────── */
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
import { WritingHall } from ${q('src/pages/Writing/WritingHall.tsx')};
import { BrandImageGallery } from ${q('src/components/brand/BrandImageGallery.tsx')};
/* 🔴 真把 Toaster 挂上:提示语是**产品的一部分**,不挂它,门量的就是自己没接的东西。
   第一版没挂,I3b 报「没有失败提示」——像产品缺陷,其实是我的仪器缺一块。 */
import { Toaster } from ${q('src/components/ui/sonner.tsx')};

function Shell({ children }: { children: React.ReactNode }) {
    return (
        <MemoryRouter initialEntries={['/writing?quote_id=7001']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <div style={{ padding: 12 }}>{children}</div>
              <Toaster richColors position="top-center" />
            </ClientProvider></PricingProvider></OnboardingProvider>
          </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
        </MemoryRouter>
    );
}

let root: any = null;
(globalThis as any).__mount = function (el: HTMLElement, surface: string, nonce?: number) {
    if (!root) root = createRoot(el);
    const inner = surface === 'gallery'
        ? <BrandImageGallery brandId={629} />
        : <WritingHall />;
    root.render(<div key={surface + ':' + String(nonce || 1)}><Shell>{inner}</Shell></div>);
};
`, 'utf8');

const rawSuffixPlugin = {
    name: 'raw-suffix',
    setup(build) {
        build.onResolve({ filter: /\?raw$/ }, (a) => ({
            path: join(a.resolveDir, a.path.replace(/\?raw$/, '').replace(/^@\//, '')),
            namespace: 'raw-ns',
        }));
        build.onLoad({ filter: /.*/, namespace: 'raw-ns' }, (a) => ({
            contents: `export default ${JSON.stringify(readFileSync(a.path, 'utf8'))}`,
            loader: 'js',
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
} catch (err) { cannotRun('打包失败', err); }

let cssName = '';
try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    cssName = c[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, cssName), 'utf8'), 'utf8');
} catch (err) { cannotRun('取不到 dist 真 CSS', err); }

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a220</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>`, 'utf8');

/* 三张真 PNG(1x1),给文件选择框喂 */
const PNG_1x1 = Buffer.from(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
    'base64');
const mkPic = (n) => {
    const p = join(outDir, n);
    writeFileSync(p, PNG_1x1);
    return p;
};
const picPaths = ['pic_a.png', 'pic_b.png', 'pic_c.png'].map(mkPic);
/* 25 张:比上限 20 多 5 张 —— 用来量「多出来的真的被截掉了」。
   🔴 25 不是随便挑的:它不能是 20 的整数倍、也不能等于 20,
      否则「截断了」和「没截断」会算出同一个请求数。 */
const picPaths25 = Array.from({ length: 25 }, (_, i) => mkPic(`batch_${i + 1}.png`));

/*
 * [a1'] 端到端那一臂要真连后端。
 * 🔴 走**同源代理**,不让浏览器直接打 :55703 —— 跨域会把 CORS 变成被测对象之一,
 *    而 CORS 不是本单要证的东西。代理只在设了 A220_E2E_BACKEND 时生效。
 */
const E2E_BACKEND = process.env.A220_E2E_BACKEND || '';
const proxyToBackend = (req, res) => {
    const u = new URL(req.url, E2E_BACKEND);
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
        const body = Buffer.concat(chunks);
        const out = httpRequest({
            hostname: u.hostname, port: u.port, path: u.pathname + u.search, method: req.method,
            headers: { ...req.headers, host: u.host, 'content-length': String(body.length) },
        }, (br) => {
            res.writeHead(br.statusCode || 502, br.headers);
            br.pipe(res);
        });
        out.on('error', (e) => { res.writeHead(502); res.end(String(e.message)); });
        out.end(body);
    });
};

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
const server = createServer((req, res) => {
    if (E2E_BACKEND && (req.url || '').startsWith('/api/')) return proxyToBackend(req, res);
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try {
        const b = readFileSync(join(outDir, n));
        res.writeHead(200, { 'Content-Type': MIME[extname(n)] || 'application/octet-stream' });
        res.end(b);
    } catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

/* ── 夹具 ─────────────────────────────────────────────────────────────────
   🔴 brand 按 **id** 引用(629),不写名字。
   档案里**六个字段全部有值**,而且是能一眼认出来源的串 —— 这样「显示出来的到底是
   服务端的还是本地残留的」不用猜。 */
const BRAND_ID = 629;

/*
 * 🔴 [a1' · Review 09-16 裁定] 桩**来自真端点录制**,不是按契约手写。
 *
 *    上一版我给 `/api/my-clients/{id}` 的桩里手写了 `writing_basics` ——
 *    而那个 sha 的真端点**根本不回它**(只有 `/api/profiles/{id}` 回)。
 *    结果:门 30/30 全绿,真跑起来那两个 JSONB 字段永远是空的。
 *    桩按契约写、端点不按契约给 —— 这种错只有真后端能看见。
 *    录制脚本 `scripts/fixtures/record_a220.py`,录的是 c2'' 的尖。
 */
const REC = JSON.parse(readFileSync(join(ROOT, 'scripts/fixtures/a220_recorded_c6bdcb529.json'), 'utf8'));
const RECORDED = REC.my_clients_get.body.profile;
ok(REC.provenance.backend_sha.startsWith('c6bdcb529') && REC.my_clients_get.http === 200,
    'K0a 分母自证:桩的出处(后端 sha / 端点 / 回包状态)',
    `${REC.provenance.backend_sha.slice(0, 9)} · my-clients http=${REC.my_clients_get.http} · profile ${Object.keys(RECORDED).length} 键`);
ok(RECORDED.writing_basics && Object.keys(RECORDED.writing_basics).length === 6,
    'K0b 🔴 录制里 `my-clients` **确实**回了 writing_basics 六字段 —— '
    + '这一格就是上次那个缺口的守门人:哪天后端又不回了,录制重跑时它先红',
    JSON.stringify(RECORDED.writing_basics || null).slice(0, 80));

/* 甲 = 录制原样,只额外塞一个**与契约键不同值**的老列,用来量优先级(K1e)。
   值来自录制(端到端-*),不是我编的。 */
const PROFILE = {
    ...RECORDED,
    /* 🔴 老列的值必须**和契约键不同**,否则优先级不可观测。
       上一版我只改了 core_value,而 business_summary 这个**真列**仍是录制里那份
       (和 writing_basics.business_summary 一模一样)—— 于是注毒 M14
       (读不到契约键)把它落到真列上,K1e 读到同一个字符串,照样绿。
       同族第四次:两个来源取同一个值 = 那一格没在测优先级。 */
    business_summary: '旧列-业务描述',
    core_value: '旧列-业务描述',
    structured_knowledge: { customers: '旧列-目标客户' },
};
const REC_SIX = RECORDED.writing_basics;
/* 乙(brand 777)= **c2 之前**的形态:没有 basic_info,只有四个真列;
   另外故意塞进三条**明令不许读**的来源,值都带「不许读-」前缀 ——
   它们只要出现在表里就是回落没摘干净。
   🔴 这三条不是随手挑的:
     negative_feedback 是飞轮往里追加字典的失败教训;
     products 存的时候 json.dumps;
     success_cases 走 _json_text_or_none —— **传字符串存字符串、传数组存 JSON**,
     形态取决于写入方,所以在一部分客户身上看着正常。 */
const PROFILE_B = {
    ...RECORDED,
    writing_basics: undefined,      /* JSON.stringify 会把 undefined 键丢掉 = 端点没回它 */
    business_summary: 'B-业务描述',
    target_users: 'B-目标客户',
    selling_points: 'B-核心卖点',
    brand_constraints: 'B-禁用表达',
    negative_feedback: [{ lesson: '不许读-飞轮失败教训' }],
    products: ['不许读-产品数组'],
    success_cases: '不许读-成功案例列',
};
/* 丙(brand 888)= 四个真列**全空**,只剩三条禁用来源。
   🔴 K1d 必须量在这一份上。量在乙身上够不着:乙的 brand_constraints 有值,
      `firstKnowledgeText` 取到就返回,回落那条路根本不会走 ——
      注毒 M15 因此 rc=0 存活。判据要能红,先得让被测的那条路有人走。 */
const PROFILE_C = {
    ...RECORDED,
    writing_basics: undefined,
    business_summary: null, target_users: null, selling_points: null, brand_constraints: null,
    negative_feedback: [{ lesson: '不许读-飞轮失败教训' }],
    products: ['不许读-产品数组'],
    success_cases: '不许读-成功案例列',
};
const PROJECT = {
    id: 7001, quote_ids: [7001], brand_id: BRAND_ID, brand_name: 'QA 夹具客户',
    industry: '环保设备', keyword_count: 1, total_required_articles: 3,
    monthly_price: 1000, writing_status: 'in_progress', confirmed_at: '2026-09-14T02:00:00Z',
};
const PROJECT_B = { ...PROJECT, id: 7002, quote_ids: [7002], brand_id: 777, brand_name: 'QA 夹具客户乙' };
const PROJECT_C = { ...PROJECT, id: 7003, quote_ids: [7003], brand_id: 888, brand_name: 'QA 夹具客户丙' };
const FIELD_IDS = ['business_summary', 'target_customers', 'products_services',
    'key_selling_points', 'proof_cases', 'forbidden_notes'];

const browser = await playwright.chromium.launch().catch((err) => cannotRun('chromium 起不来', err));

/* 每个面一个新页:桩 + 计数 + 「确认后再挂」 */
async function openPage(surface, opts = {}) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
    const calls = [];
    const uploadBodies = [];
    const putBodies = [];
    /* 🔴 剧本一律**显式开关**,不靠数第几次调用。
       noProfile:my-clients 回 200 但 `profile` 为空 —— 「请求回来了」≠「读到了 profile」。 */
    const ctl = { slow: false, slowMs: 9000, noProfile: false };
    /* 🔴 console 痕迹:`!profileId` 那条只写 console.warn,是**只有它**会留下的记号。
       K9d2 靠它点名「是谁挡的」,而不是靠「数到 0」—— 0 有两条成因,数 0 分不开。 */
    const consoleLines = [];
    page.on('console', (m) => { try { consoleLines.push(m.text()); } catch { /* 忽略 */ } });
    let uploadSeq = 0;
    await page.addInitScript(() => { localStorage.setItem('omnirank_token', 'qa-token'); });
    await page.route('**/api/**', async (route) => {
        const u = new URL(route.request().url());
        const p = u.pathname;
        calls.push(p);
        const json = (b, status = 200) => route.fulfill({
            status, contentType: 'application/json', body: JSON.stringify(b),
        });
        if (p.includes('/auth/me')) {
            return json({
                success: true,
                user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
                id: 112, username: 'qa', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent',
            });
        }
        if (p.includes('/organization')) return json({ success: true, overview: { identity: { capabilities: ['quote.create'] } } });
        if (/\/api\/brand-images\/upload$/.test(p)) {
            uploadSeq += 1;
            const seq = uploadSeq;
            uploadBodies.push({ seq, at: Date.now(), body: route.request().postData() || '' });
            /* 桩加延时:没有延时就量不出「顺序还是并发」 */
            await new Promise((r) => setTimeout(r, 300));
            if (opts.failNth && opts.failNth === seq) {
                return json({ detail: '桩:这张故意失败' }, 400);
            }
            uploadBodies[uploadBodies.length - 1].doneAt = Date.now();
            return json({ success: true, asset: { id: seq, file_name: `x${seq}.png` }, vision_ok: true });
        }
        if (/\/api\/brand-images\/list\/\d+$/.test(p)) return json({ success: true, assets: [] });
        if (/\/api\/my-clients\/\d+$/.test(p)) {
            const id = Number(p.split('/').pop());
            /* 🔴 变慢由测试**显式开关**,不靠数第几次:
               第一版我写「第 2 次起变慢」,结果那一发正好落在 `enterWorkbench` 的加载上
               ⇒ brandProfile 从头没建立 ⇒ 走的是更早那条 `!profileId` 静默 return,
               判据看到的 0 次 PUT **不是**新守卫挡的。剧本踩错了目标,绿得没道理。 */
            if (ctl.slow) await new Promise((r) => setTimeout(r, ctl.slowMs));
            /* 🔴 200 但 profile 为空:前端会拿到 {brand, profile:null} —— 一个真值,里面什么都没有 */
            if (ctl.noProfile) {
                return json({ success: true, brand: { id, name: 'QA 夹具客户', is_test: true }, profile: null });
            }
            return json({ success: true, brand: { id, name: 'QA 夹具客户', is_test: true },
                profile: id === BRAND_ID ? PROFILE : (id === 888 ? PROFILE_C : PROFILE_B) });
        }
        if (/\/api\/writing\/projects$/.test(p)) return json({ projects: [PROJECT, PROJECT_B, PROJECT_C] });
        if (/\/api\/writing\/projects\/\d+$/.test(p)) return json({ keywords: [], topics: [], total_planned_posts_default: 3 });
        if (/\/api\/knowledge\/upload$/.test(p)) {
            /* 桩可以按剧本回 500 —— 真后端上它就 500 过,而桩恒 200 时看不见后果 */
            if (opts.knowledgeUploadStatus && opts.knowledgeUploadStatus !== 200) {
                return json({ success: false, detail: '桩:知识库这一发故意失败' }, opts.knowledgeUploadStatus);
            }
            return json({ success: true, path: 'x.md' });
        }
        if (/\/api\/profiles\//.test(p) && route.request().method() === 'PUT') {
            putBodies.push(route.request().postData() || '{}');
            return json({ success: true });
        }
        if (/\/api\/knowledge\/client\/\d+$/.test(p)) return json({ success: true, files: [] });
        return json({ success: true, projects: [], topics: [], keywords: [], assets: [], files: [], items: [], sessions: [] });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    /* 🔴 先挂一次点火 → 等 /me 把会话确认下来 → 换 key 重挂。
       确认前发出的 axios 请求会被 rotateAuthorizationScope 掐掉(canceled),
       挂了就截 = 页面停在空列表。 */
    await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), surface);
    await page.waitForTimeout(1800);
    await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s, 2), surface);
    await page.waitForTimeout(2200);
    calls.length = 0;               // 挂载期的请求不计入判据
    return { page, calls, uploadBodies, putBodies, ctl, consoleLines };
}

const val = (page, tid) => page.evaluate((t) => {
    const el = document.querySelector(`[data-testid="${t}"]`);
    return el ? String(el.value ?? '') : '__NO_ELEMENT__';
}, tid);
const readAll = async (page) => Object.fromEntries(
    await Promise.all(FIELD_IDS.map(async (f) => [f, await val(page, `kb-${f}`)])));
const clickText = async (page, needle) => {
    await page.getByText(needle, { exact: false }).filter({ visible: true }).first().click({ timeout: 6000 });
};
/* 🔴 抽屉开关一律用 testid,不用文字:
   页面上「补全资料」这四个字还出现在**步骤条**上,而且排在按钮前面,
   `getByText(...).first()` 抓到的是那个标签 —— 点了什么都不会发生,
   然后门会报「字段不在页面上」,像是产品坏了,其实是我点错了东西。 */
const openDrawer = async (page) => {
    await page.locator('[data-testid="knowledge-open-drawer"]').first().click({ timeout: 6000 });
    await page.waitForSelector('[data-testid="kb-business_summary"]', { timeout: 8000 });
    await page.waitForTimeout(1200);
};
const closeDrawer = async (page) => {
    await page.locator('[data-testid="kb-close-drawer"]').first().click({ timeout: 6000 });
    await page.waitForTimeout(500);
};

try {
    /* ── K:基础资料表 ──────────────────────────────────────────────────── */
    console.log('\nK 基础资料表:打开能看见 · 保存后不消失 · 换客户要清掉');
    const w = await openPage('writing');
    await clickText(w.page, 'QA 夹具客户');          // 进项目工作台
    await w.page.waitForTimeout(1500);
    w.calls.length = 0;
    await openDrawer(w.page);                       // 开抽屉

    if (process.env.PROBE) {
        const t = await w.page.evaluate(() => document.body.innerText || '');
        console.log('--8<-- ' + t.slice(0, 1400).split(String.fromCharCode(10)).join(' | ') + ' --8<--');
    }
    const first = await readAll(w.page);
    ok(!Object.values(first).includes('__NO_ELEMENT__'),
        'K0 分母自证:六个字段都在页面上', Object.values(first).filter((x) => x === '__NO_ELEMENT__').length === 0
            ? '6/6' : `缺 ${Object.values(first).filter((x) => x === '__NO_ELEMENT__').length} 个`);
    const filled = FIELD_IDS.filter((f) => first[f] && first[f] === REC_SIX[f]);
    ok(filled.length === 6,
        'K1 🔴 打开就显示客户档案里已有的六个字段 —— 在此之前唯一能读档案的那个函数'
        + '只挂在「上传后的轮询」里,冷启动打开是空的',
        `${filled.length}/6 · 例:${first.business_summary}`);
    /* 🔴 甲的档案里契约键与老列**同时有值**(writing_basics.business_summary='档案-业务描述',
       core_value='档案-旧业务描述')。取哪个不是风格问题:c2 之后老列不再被写,
       谁先谁后决定用户看到的是新的还是一年前的。 */
    ok(first.business_summary === REC_SIX.business_summary,
        'K1e 🔴 契约键与老列同时有值时,取**契约键**',
        first.business_summary);
    ok(w.calls.filter((p) => /\/api\/my-clients\//.test(p)).length === 1,
        'K5 打开只拉一次档案(不是每次 render 都拉)',
        `${w.calls.filter((p) => /\/api\/my-clients\//.test(p)).length} 次`);

    /* 填原始资料与备注,保存 */
    await w.page.locator('[data-testid="kb-raw-text"]').fill('一段粘贴进来的原始资料');
    await w.page.locator('[data-testid="kb-notes"]').fill('一条备注');
    await w.page.locator('[data-testid="kb-save"]').first().click({ timeout: 6000 });
    await w.page.waitForTimeout(2000);

    /*
     * 🔴 K10r [#220 a2] **反臂**:一切正常的保存路径上,那句「没有写回档案」不许出现。
     *    没有这一格的话,K10 可以被一个**无条件常驻**的提示满足 ——
     *    那种提示等于每次保存都喊狼来了,用户很快学会无视它,
     *    到真出事那次就跟没提示一样。出声的价值来自「只在该出声时出声」。
     */
    const okPathText = await w.page.evaluate(() => document.body.innerText || '');
    ok(!/没有写回档案|还没同步好/.test(okPathText),
        'K10r 反臂:正常保存路径上**不许**出现「没有写回档案」那句 —— '
        + '否则 K10 会被一个无条件提示满足,而那种提示等于没有',
        (okPathText.match(/[^\n]*(没有写回档案|还没同步好)[^\n]*/) || ['(正常路径干净)'])[0].slice(0, 70));

    const afterSave = await readAll(w.page);
    const kept = FIELD_IDS.filter((f) => afterSave[f] === first[f] && first[f]);
    ok(kept.length === 6,
        'K2 🔴 保存成功后六个基础字段**仍在** —— 它们是「这个客户是谁」的事实,不是一次性草稿;'
        + '原来这里整体清空,就是 Owner 报的「第二次打开是空的」',
        `${kept.length}/6`);
    ok((await val(w.page, 'kb-raw-text')) === '' && (await val(w.page, 'kb-notes')) === '',
        'K2b 反臂:这一次的输入(原始资料 / 备注)该清的清掉了 —— 不能把「不清」做成「什么都不清」',
        `raw="${await val(w.page, 'kb-raw-text')}" notes="${await val(w.page, 'kb-notes')}"`);

    /* 关掉再开 */
    await closeDrawer(w.page);
    await openDrawer(w.page);
    const reopened = await readAll(w.page);
    ok(FIELD_IDS.every((f) => reopened[f] === first[f]),
        'K3 关掉再打开六个字段还在 —— 🔴 注意它**分辨不了保存有没有清空**:'
        + '清了也会被打开重拉填回来。有牙的是下面 K3b',
        FIELD_IDS.filter((f) => reopened[f] !== first[f]).join(',') || '六个逐字相同');

    /*
     * 🔴 K3b 是 K3 该有的那颗牙。
     *    注毒 M1(保存后又整体清空)在 K3 上**存活**:表被清了,可是关掉再打开时
     *    「打开重拉」又把档案里那六个值原样填回来 —— 判据看到的和没坏时一模一样。
     *    (丙类:判据被另一条路满足。)
     *    能分辨的只有**档案里没有的内容**:用户自己写的那条,清掉就再也回不来。
     */
    const MINE = '用户写的案例-档案里没有这条';
    await w.page.locator('[data-testid="kb-proof_cases"]').fill(MINE);
    await w.page.locator('[data-testid="kb-save"]').first().click({ timeout: 6000 });
    await w.page.waitForTimeout(2000);
    await closeDrawer(w.page);
    await openDrawer(w.page);
    ok((await val(w.page, 'kb-proof_cases')) === MINE,
        'K3b 🔴 保存 → 关掉 → 再打开,**用户自己写的、档案里没有的**那条仍在',
        await val(w.page, 'kb-proof_cases'));

    /* 用户自己打的字不许被重拉覆盖 */
    await w.page.locator('[data-testid="kb-business_summary"]').fill('用户自己写的');
    await closeDrawer(w.page);
    await openDrawer(w.page);
    ok((await val(w.page, 'kb-business_summary')) === '用户自己写的',
        'K6 🔴 重拉是 fillIfEmpty:用户已经写过的字段不被服务端值盖掉'
        + '(把它改成无条件覆盖 = 打字打到一半被吞)',
        await val(w.page, 'kb-business_summary'));

    /* 换客户必须清空 */
    await closeDrawer(w.page);
    await clickText(w.page, '返回列表');
    await w.page.waitForTimeout(1200);
    await clickText(w.page, 'QA 夹具客户乙');
    await w.page.waitForTimeout(1500);
    await openDrawer(w.page);
    const switched = await readAll(w.page);
    const bFilled = ['business_summary', 'target_customers', 'key_selling_points', 'forbidden_notes']
        .filter((f) => (switched[f] || '').startsWith('B-'));
    ok(bFilled.length === 4,
        'K1b 🔴 c2 之前:四个**真列**照契约读得到'
        + '(business_summary / target_users / selling_points / brand_constraints)',
        `${bFilled.length}/4`);
    ok(!switched.products_services && !switched.proof_cases,
        'K1c 🔴 两个 JSONB 字段在 c2 之前**留空** —— 不许回落到 products / success_cases',
        `products_services="${switched.products_services}" proof_cases="${switched.proof_cases}"`);
    ok(FIELD_IDS.every((f) => !Object.values(REC_SIX).includes(switched[f] || '__none__') && switched[f] !== '用户自己写的'),
        'K4 🔴 换客户后表里没有上一家的任何内容 —— 🔴 它的牙来自 `backToList` 那一处清空:'
        + 'UI 上换客户必须先返回列表,所以 `enterWorkbench` 里那处在当前导航下是**冗余守卫**,'
        + '只有结构锁 K4a 看得见它(注毒 M3 实证)',
        JSON.stringify(switched).slice(0, 120));

    /* 丙:四个真列全空,只剩三条禁用来源 —— 这一屏才量得出「回落有没有摘干净」 */
    await closeDrawer(w.page);
    await clickText(w.page, '返回列表');
    await w.page.waitForTimeout(1200);
    await clickText(w.page, 'QA 夹具客户丙');
    await w.page.waitForTimeout(1500);
    await openDrawer(w.page);
    const cFields = await readAll(w.page);
    ok(!Object.values(cFields).some((v) => String(v).includes('不许读-')),
        'K1d 🔴 三条禁用来源一个都没进表:negative_feedback(飞轮往里追加字典)/ products(存时 json.dumps)'
        + '/ success_cases(走 _json_text_or_none —— 写入方传字符串就存字符串、传数组就存 JSON,'
        + '所以形态取决于谁写的,在一部分客户身上看着正常)',
        Object.entries(cFields).filter(([, v]) => String(v).includes('不许读-')).map(([k]) => k).join(',') || '零处');
    ok(Object.values(cFields).every((v) => !v),
        'K1d2 反臂:这一屏本来就该是空的(否则上面那条「零处」是因为压根没读到东西)',
        JSON.stringify(cFields).slice(0, 100));
    await w.page.close();

    /*
     * 🔴 K7 / K8 是**端到端那一臂逼出来的**,做成不依赖真后端的常驻判据。
     *    真后端上 `/api/knowledge/upload` 回了 500(那台机器向量库没起来),
     *    而我第一版把结构化 PUT 放在它成功之后 ⇒ 六个字段一个都没落库,
     *    用户只看到「保存失败,请重试」,重试也永远存不进去。
     *    桩里那条永远回 200,所以 32 条判据全绿也看不见。
     */
    const w2 = await openPage('writing', { knowledgeUploadStatus: 500 });
    await clickText(w2.page, 'QA 夹具客户');
    await w2.page.waitForTimeout(1500);
    await openDrawer(w2.page);
    await w2.page.locator('[data-testid="kb-raw-text"]').fill('随便一段资料');
    /* 清掉一栏:契约是「显式空串=清空」,所以它必须**出现在请求体里**且为 "" */
    await w2.page.locator('[data-testid="kb-proof_cases"]').fill('');
    w2.calls.length = 0;
    w2.putBodies.length = 0;
    await w2.page.locator('[data-testid="kb-save"]').first().click({ timeout: 8000 });
    await w2.page.waitForTimeout(2500);
    ok(w2.calls.some((p) => /\/api\/profiles\//.test(p)),
        'K7 🔴 知识库那一发失败(500)时,结构化 PUT **仍然发出** —— '
        + '两件事两个用途,不该一个坏了把另一个也带走',
        w2.calls.filter((p) => /\/api\/(profiles|knowledge)/.test(p)).join(' , ') || '(一个都没发)');
    const put = w2.putBodies[0] ? JSON.parse(w2.putBodies[0]) : {};
    ok(FIELD_IDS.every((f) => Object.prototype.hasOwnProperty.call(put, f)),
        'K8 🔴 六个字段**全传**,包括空的 —— 契约是「不传=不覆盖、显式空串=清空」;'
        + '只传非空的话,用户清掉某一栏再保存,服务端那栏原样留着,下次打开又被重拉回来(像删不掉)',
        `键 ${Object.keys(put).length} 个 · proof_cases=${JSON.stringify(put.proof_cases)}`);
    ok(put.proof_cases === '',
        'K8b 被清掉那一栏在请求体里就是空串(不是 undefined、不是被省略)',
        JSON.stringify(put.proof_cases));
    await w2.page.close();

    /*
     * 🔴 K9 [a1''' · 复审点名] **没成功加载过就不许写回** —— 否则静默清空真客户档案。
     *    复现的是那个真实窗口:`enterWorkbench` 那次加载让 profileId 就位,
     *    但它**不填基础资料表**;抽屉打开的重拉还在路上时表单是空的。
     *    这时按保存,六个空串送出去,而契约里「显式空串 = 清空」——
     *    档案里已有的四个真列被抹掉,零报错。
     *    剧本:my-clients **第二次起变慢** = 抽屉那次重拉还没回来。
     */
    const w3 = await openPage('writing');
    await clickText(w3.page, 'QA 夹具客户');      /* 这一步要**正常加载**,profileId 得先就位 */
    await w3.page.waitForTimeout(1800);
    w3.ctl.slow = true;                            /* 从现在起 my-clients 变慢 = 抽屉那次重拉还在路上 */
    await w3.page.locator('[data-testid="knowledge-open-drawer"]').first().click({ timeout: 6000 });
    await w3.page.waitForSelector('[data-testid="kb-business_summary"]', { timeout: 8000 });
    await w3.page.waitForTimeout(300);          /* 故意不等重拉回来 */
    const beforeSave = await readAll(w3.page);
    ok(FIELD_IDS.every((f) => !beforeSave[f]),
        'K9a 分母自证:这一刻表单确实是空的(重拉还没回来)—— 否则下面那条没在测它写的事',
        JSON.stringify(beforeSave).slice(0, 80));
    await w3.page.locator('[data-testid="kb-raw-text"]').fill('秒开秒存的一段资料');
    w3.calls.length = 0;
    w3.putBodies.length = 0;
    await w3.page.locator('[data-testid="kb-save"]').first().click({ timeout: 8000 });
    await w3.page.waitForTimeout(2500);
    ok(w3.putBodies.length === 0,
        'K9 🔴 重拉还没回来时保存,**一发 PUT 都不许出去** —— '
        + '出去的话就是拿六个空串把档案里已有的四个真列抹掉(契约:显式空串=清空)',
        `PUT ${w3.putBodies.length} 次${w3.putBodies[0] ? ' · 体=' + w3.putBodies[0].slice(0, 80) : ''}`);
    const w3text = await w3.page.evaluate(() => document.body.innerText || '');
    if (process.env.PROBE) console.log('--toast-- len=' + w3text.length + ' | 抽屉在=' + /补全知识库/.test(w3text) + ' | 含保存提示=' + /已保存|保存失败/.test(w3text) + ' | 尾=' + w3text.slice(-300).split(String.fromCharCode(10)).join(' / '));
    ok(/没有写回档案|还没同步好/.test(w3text),
        'K9b 而且要**出声**:跳过了要让用户知道,不能当作成功',
        (w3text.match(/[^\n]*(没有写回档案|还没同步好)[^\n]*/) || ['(没有提示)'])[0].slice(0, 70));
    await w3.page.close();

    /*
     * 🔴 K9c [M26 存活逼出来的] 守卫必须记**是哪一个客户**,不能只记「加载过没有」。
     *    真实窗口:甲加载好(ref=甲)→ 返回列表 → 点乙。`enterWorkbench` 把 `brandProfile`
     *    换成**乙的**(于是 profileId 是乙的、不为空),但它**不调** `refreshKnowledgeAutofill`
     *    ⇒ ref 还停在甲。抽屉那次重拉在路上时表单是空的,这时按保存——
     *    布尔守卫会因为「甲加载过」而放行,把六个空串写进**乙**的档案。
     * 🔴 为什么原来没有这一格:K9 的剧本只有「同一客户、重拉没回来」一种情形,
     *    物理上看不见「记的是哪一个」这一面 ⇒ M26 在 38 格全绿下存活。
     *    毒钉的是抬头承诺的每一样,判据只跟上了其中一样。
     */
    const w4 = await openPage('writing');
    await clickText(w4.page, 'QA 夹具客户');
    await w4.page.waitForTimeout(1800);
    await openDrawer(w4.page);                   /* 甲这一次要**真加载成功**,ref 才会是甲 */
    const w4a = await readAll(w4.page);
    ok(Object.values(w4a).some((v) => v),
        'K9c0 分母自证:甲这一屏确实加载成功了 —— 否则下面测的不是「换了客户」而是「压根没加载过」',
        JSON.stringify(w4a).slice(0, 80));
    await closeDrawer(w4.page);
    await clickText(w4.page, '返回列表');
    await w4.page.waitForTimeout(1200);
    await clickText(w4.page, 'QA 夹具客户乙');    /* 乙的 enterWorkbench 正常跑完 ⇒ profileId 是乙的 */
    await w4.page.waitForTimeout(1800);
    w4.ctl.slow = true;                          /* 从现在起变慢 = 抽屉那次重拉还在路上 */
    await w4.page.locator('[data-testid="knowledge-open-drawer"]').first().click({ timeout: 6000 });
    await w4.page.waitForSelector('[data-testid="kb-business_summary"]', { timeout: 8000 });
    await w4.page.waitForTimeout(300);
    const w4b = await readAll(w4.page);
    ok(FIELD_IDS.every((f) => !w4b[f]),
        'K9c1 分母自证:换到乙以后表是空的(重拉还没回来)—— 否则送出去的不是空表',
        JSON.stringify(w4b).slice(0, 80));
    await w4.page.locator('[data-testid="kb-raw-text"]').fill('换客户后秒存的一段资料');
    w4.putBodies.length = 0;
    await w4.page.locator('[data-testid="kb-save"]').first().click({ timeout: 8000 });
    await w4.page.waitForTimeout(2500);
    const w4text = await w4.page.evaluate(() => document.body.innerText || '');
    const w4spoke = /没有写回档案|还没同步好/.test(w4text);
    ok(w4.putBodies.length === 0 && w4spoke,
        'K9c 🔴 换了客户、新客户的重拉还没回来时保存:一发 PUT 都不许出去,而且要**出声** —— '
        + '守卫只记「加载过没有」的话,会拿**甲**的绿灯放行,把空表写进**乙**的档案(契约:显式空串=清空)',
        `PUT ${w4.putBodies.length} 次 · 出声=${w4spoke}`);
    await w4.page.close();

    /*
     * 🔴 K9d [M27 存活逼出来的] 「请求回来了」≠「读到了 profile」。
     *    my-clients 回 200 但 `profile` 为空时,`loadWritingBrandAssets` 交出来的是
     *    `{brand, profile:null}` —— 一个真值,里面什么都没有。
     *
     * 🔴 归因(K9d2):这一格上「PUT=0」**有两条成因** —— 新守卫,或更早那条
     *    `if (!profileId) return`(WritingHall.tsx:3627)。在这个剧本下两者**互为冗余**
     *    (profile 为空 ⇒ setBrandProfile({profile:null}) ⇒ profileId 也为空),
     *    所以 M27 单发必然存活 —— 「毒仍绿四解」的第四解**目标冗余**,不是没牙。
     *    真正证明这一面被锁住的是 **M27b 双改**(M27 + 摘掉 `!profileId`),那一发必红。
     *    这里用 console 痕迹点名**是谁挡的**,而不是只数 0。
     */
    const w5 = await openPage('writing');
    await clickText(w5.page, 'QA 夹具客户');
    await w5.page.waitForTimeout(1800);
    w5.ctl.noProfile = true;                     /* 从现在起 my-clients 回 200 但 profile 为空 */
    await openDrawer(w5.page);
    const w5a = await readAll(w5.page);
    ok(FIELD_IDS.every((f) => !w5a[f]),
        'K9d0 分母自证:profile 为空时表也是空的 —— 否则送出去的不是空表',
        JSON.stringify(w5a).slice(0, 80));
    await w5.page.locator('[data-testid="kb-raw-text"]').fill('profile 为空时保存的一段资料');
    w5.putBodies.length = 0;
    w5.consoleLines.length = 0;
    await w5.page.locator('[data-testid="kb-save"]').first().click({ timeout: 8000 });
    await w5.page.waitForTimeout(2500);
    ok(w5.putBodies.length === 0,
        'K9d 🔴 my-clients 回 200 但 profile 为空:一发 PUT 都不许出去 —— '
        + '把「请求回来了」当成「加载成功」的话,六个空串照样送出去,把档案抹掉',
        `PUT ${w5.putBodies.length} 次${w5.putBodies[0] ? ' · 体=' + w5.putBodies[0].slice(0, 80) : ''}`);
    ok(w5.consoleLines.some((l) => l.includes('没有 profile id')),
        'K9d2 🔴 归因:点名是**谁**挡的 —— 只有 `!profileId` 那条会留下这行 console;'
        + '「数到 0」两条路都能产生,分不开(绿也要归因)',
        w5.consoleLines.filter((l) => l.includes('220-a1')).join(' | ').slice(0, 90) || '(没有那行)');
    /*
     * 🔴 K10 [#220 a2] console.warn 是给**我**看的,用户看不见。
     *    这条分支原来只写 console,用户看到的是「客户资料已保存」,
     *    而基础资料一个字都没进档案 —— 下次打开才发现白写了。
     *    K9d2 钉的是「是谁挡的」(开发者视角的归因痕迹),
     *    K10 钉的是「用户知不知道」—— 两件事,不能互相顶替。
     */
    const w5text = await w5.page.evaluate(() => document.body.innerText || '');
    ok(/没有写回档案|还没同步好/.test(w5text),
        'K10 🔴 拿不到 profile id 而跳过写回时,**用户**必须看得见 —— '
        + '只写 console.warn 等于只告诉开发者,用户那头是一句「已保存」',
        (w5text.match(/[^\n]*(没有写回档案|还没同步好)[^\n]*/) || ['(用户那头一个字都没有)'])[0].slice(0, 70));
    await w5.page.close();

    /* ── I:批量上传 ───────────────────────────────────────────────────── */
    console.log('\nI 客户图片:一次选多张 · 顺序发 · 中间失败不中断');
    const g = await openPage('gallery');
    await g.page.locator('input[type=file]').first().setInputFiles(picPaths);
    await g.page.waitForFunction(() => !document.querySelector('input[type=file]')?.disabled,
        null, { timeout: 30000 }).catch(() => {});
    await g.page.waitForTimeout(600);

    const uploads = g.calls.filter((p) => /\/api\/brand-images\/upload$/.test(p));
    ok(uploads.length === 3, 'I1 🔴 选 3 张就发 3 次(原来只取 files[0],永远只发 1 次)', `${uploads.length} 次`);
    const names = g.uploadBodies.map((b) => (b.body.match(/filename="([^"]+)"/) || [])[1]).filter(Boolean);
    ok(new Set(names).size === 3, 'I1b 三次发的是**三个不同文件**(不是同一张发三遍)', names.join(','));
    const sequential = g.uploadBodies.every((b, i) =>
        i === 0 || (g.uploadBodies[i - 1].doneAt && b.at >= g.uploadBodies[i - 1].doneAt));
    ok(sequential,
        'I2 🔴 顺序不并发 —— 后端每张都要跑一次视觉识别,Promise.all 过去等于同时压 N 个识别',
        g.uploadBodies.map((b) => b.at - g.uploadBodies[0].at).join('ms / ') + 'ms');
    ok(g.calls.filter((p) => /\/api\/brand-images\/list\//.test(p)).length === 1,
        'I5 列表只在全部结束后刷一次(每张都刷 = N 次请求 + 后到覆盖先到)',
        `${g.calls.filter((p) => /\/api\/brand-images\/list\//.test(p)).length} 次`);
    await g.page.close();

    /* 中间一张失败 */
    const g2 = await openPage('gallery', { failNth: 2 });
    await g2.page.locator('input[type=file]').first().setInputFiles(picPaths);
    await g2.page.waitForFunction(() => !document.querySelector('input[type=file]')?.disabled,
        null, { timeout: 30000 }).catch(() => {});
    await g2.page.waitForTimeout(800);
    const uploads2 = g2.calls.filter((p) => /\/api\/brand-images\/upload$/.test(p));
    ok(uploads2.length === 3,
        'I3 🔴 第 2 张失败后第 3 张**仍然发** —— 选了 10 张不该因为第 3 张坏了就停在 2 张',
        `${uploads2.length} 次`);
    const body2 = await g2.page.evaluate(() => document.body.innerText || '');
    ok(/失败\s*1\s*张/.test(body2) && /pic_b\.png/.test(body2),
        'I3b 结束提示说得出**失败几张、哪一张**(只说「上传失败」= 用户得自己一张张试)',
        (body2.match(/失败[^\n]{0,60}/) || ['(没有失败提示)'])[0]);
    await g2.page.close();

    /* 上传中入口禁用 —— 用长延时那一轮量,不然抓不到中间态 */
    const g3 = await openPage('gallery');
    await g3.page.locator('input[type=file]').first().setInputFiles(picPaths);
    await g3.page.waitForTimeout(350);
    const midState = await g3.page.evaluate(() => {
        const ins = [...document.querySelectorAll('input[type=file]')];
        return { total: ins.length, disabled: ins.filter((x) => x.disabled).length, text: document.body.innerText };
    });
    ok(midState.total > 0 && midState.disabled === midState.total,
        'I4 🔴 上传进行中,页面上**所有**文件选择框都是禁用的',
        `${midState.disabled}/${midState.total}`);
    ok(/正在识别第\s*\d+\s*\/\s*3\s*张/.test(midState.text),
        'I4b 进度说得出第几张 / 共几张(只说「上传中…」时用户不知道还要等多久)',
        (midState.text.match(/正在识别[^\n]{0,30}/) || ['(没有进度文案)'])[0]);
    await g3.page.waitForFunction(() => !document.querySelector('input[type=file]')?.disabled,
        null, { timeout: 30000 }).catch(() => {});
    await g3.page.close();

    /* 超限那一批:选 25 张,只该发 20 次,并且要**说出来**为什么少了 5 张 */
    const g4 = await openPage('gallery');
    await g4.page.locator('input[type=file]').first().setInputFiles(picPaths25);
    /* 🔴 超限提示要在**选完的那一刻**取:它是开跑前弹的 toast,
       而 20 张传完要 6 秒开外,toast 早自动消失了。
       第一版等传完再读,报「没有超限提示」——量错了时刻,不是产品没提示。 */
    await g4.page.waitForTimeout(600);
    const overText = await g4.page.evaluate(() => document.body.innerText || '');
    await g4.page.waitForFunction(() => !document.querySelector('input[type=file]')?.disabled,
        null, { timeout: 120000 }).catch(() => {});
    await g4.page.waitForTimeout(800);
    const over = g4.calls.filter((p) => /\/api\/brand-images\/upload$/.test(p));
    ok(over.length === 20,
        'I8c 🔴 选 25 张只发 **20** 次 —— 上限不是提示语,是真的截断',
        `${over.length} 次`);
    ok(/一次最多传\s*20\s*张/.test(overText),
        'I8d 🔴 超限要**说得出上限是多少、剩下的怎么办** —— 静悄悄丢掉 5 张最坏',
        (overText.match(/一次最多传[^\n]{0,40}/) || ['(没有超限提示)'])[0]);
    await g4.page.close();

    /* ── E:端到端(真后端)────────────────────────────────────────────────
     * 🔴 这一臂**只桩掉写作项目列表**,其余一律走真后端(同源代理)。
     *    桩掉它是因为它与本单要证的东西无关(建一条写作项目要另铺一堆夹具);
     *    要证的是那条链本身:表单 → PUT → 落库 → 重新打开 → 逐字相同。
     * 🔴 没给 A220_E2E_BACKEND 就**明说没跑**,不计进判据条数 ——
     *    「跑了没事」和「没跑」必须长得不一样。
     */
    if (!E2E_BACKEND) {
        console.log('\nE 端到端:**本次未跑**(没给 A220_E2E_BACKEND)—— 不是通过,是没测');
    } else {
        console.log('\nE 端到端(真后端 · 只桩项目列表)');
        const e = await browser.newPage({ viewport: { width: 1440, height: 1200 } });
        const user = process.env.A220_E2E_USER || '';
        const pass = process.env.A220_E2E_PASS || '';
        if (!user || !pass) cannotRun('给了 A220_E2E_BACKEND 却没给 A220_E2E_USER / A220_E2E_PASS', 'missing');
        const brandId = Number(process.env.A220_E2E_BRAND_ID || 0);
        const projId = 7101;
        /* 登录拿真 token(凭据只从 env 读,不落盘、不进日志) */
        const login = await e.request.post(`http://127.0.0.1:${PORT}/api/auth/login`,
            { data: { username: user, password: pass } });
        if (!login.ok()) cannotRun('端到端登录失败 http=' + login.status(), await login.text());
        const token = (await login.json()).token;
        if (!token) cannotRun('端到端登录没拿到 token', '');
        /* 起点先清干净:六个字段全置空串,这样「重开后看见的」只可能来自这次保存 */
        await e.request.put(`http://127.0.0.1:${PORT}/api/profiles/${process.env.A220_E2E_PROFILE_ID}`, {
            headers: { Authorization: 'Bearer ' + token },
            data: { business_summary: '', target_customers: '', products_services: '',
                key_selling_points: '', proof_cases: '', forbidden_notes: '' },
        });
        await e.addInitScript((t) => { localStorage.setItem('omnirank_token', t); }, token);
        await e.route('**/api/writing/projects**', (route) => {
            const p = new URL(route.request().url()).pathname;
            const body = /\/projects$/.test(p)
                ? { projects: [{ id: projId, quote_ids: [projId], brand_id: brandId,
                    brand_name: 'A220 端到端', industry: '测试行业', keyword_count: 0,
                    total_required_articles: 1, monthly_price: 0, writing_status: 'in_progress',
                    confirmed_at: '2026-09-16T00:00:00Z' }] }
                : { keywords: [], topics: [], total_planned_posts_default: 0 };
            return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) });
        });
        if (process.env.PROBE) {
            e.on('response', (r) => { const u = new URL(r.url()).pathname;
                if (u.startsWith('/api/') && !u.includes('projects')) console.log('    [resp] ' + r.status() + ' ' + r.request().method() + ' ' + u); });
            e.on('console', (m) => { if (m.type() === 'error' || m.text().startsWith('[220')) console.log('    [console] ' + m.text().slice(0, 180)); });
        }
        await e.goto(`http://127.0.0.1:${PORT}/index.html`);
        await e.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), 'writing');
        await e.waitForTimeout(2000);
        await e.evaluate((s) => globalThis.__mount(document.getElementById('root'), s, 2), 'writing');
        await e.waitForTimeout(2500);
        await clickText(e, 'A220 端到端');
        await e.waitForTimeout(2000);
        await openDrawer(e);

        const TYPED = {
            business_summary: 'E2E-业务描述-' + PORT,
            target_customers: 'E2E-目标客户-' + PORT,
            products_services: 'E2E-产品服务-' + PORT,
            key_selling_points: 'E2E-核心卖点-' + PORT,
            proof_cases: 'E2E-案例口碑-' + PORT,
            forbidden_notes: 'E2E-禁用表达-' + PORT,
        };
        /* 🔴 值里带端口号:同一台机器上跑两遍不会互相冒充成功 */
        for (const [k, v] of Object.entries(TYPED)) {
            await e.locator(`[data-testid="kb-${k}"]`).fill(v);
        }
        await e.locator('[data-testid="kb-save"]').first().click({ timeout: 8000 });
        await e.waitForTimeout(2500);

        /* 真「刷新」:整页重载,React 树与所有内存态一起丢掉 */
        await e.reload();
        await e.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), 'writing');
        await e.waitForTimeout(2500);
        await e.evaluate((s) => globalThis.__mount(document.getElementById('root'), s, 2), 'writing');
        await e.waitForTimeout(2500);
        await clickText(e, 'A220 端到端');
        await e.waitForTimeout(2000);
        await openDrawer(e);
        const back = await readAll(e);
        const same = FIELD_IDS.filter((f) => back[f] === TYPED[f]);
        ok(same.length === 6,
            'E5 🔴 **保存 → 整页刷新 → 重开**,六个字段逐字相同 —— '
            + '这一条在 220-c2 之前做不到(保存只把它们拍平成 markdown,没有结构化落点)',
            `${same.length}/6 · 差:${FIELD_IDS.filter((f) => back[f] !== TYPED[f]).join(',') || '无'}`);
        /* 被服务方视角:别只信页面,直接问后端 */
        const srv = await e.request.get(
            `http://127.0.0.1:${PORT}/api/profiles/${process.env.A220_E2E_PROFILE_ID}`,
            { headers: { Authorization: 'Bearer ' + token } });
        const wb = ((await srv.json()).profile || {}).writing_basics || {};
        ok(FIELD_IDS.every((f) => wb[f] === TYPED[f]),
            'E6 🔴 **问被服务方**:后端回包里那六个值就是刚才在页面上打的 —— '
            + '页面显示对可能只是本地状态没掉,落没落库得直接问后端',
            JSON.stringify(wb).slice(0, 120));
        await e.close();
    }
} catch (err) {
    await browser.close().catch(() => {});
    server.close();
    cannotRun('跑判据时抛了异常(不是判据红,是门自己断了)', err);
}

await browser.close();
server.close();

console.log(`\n跑满 ${ran} 条判据`);
if (ran < 38) { console.log('3 判据条数不足 —— 有分支没跑到,这不是通过'); process.exit(3); }
if (failures > 0) { console.log(`FAIL ${failures} 条不过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
