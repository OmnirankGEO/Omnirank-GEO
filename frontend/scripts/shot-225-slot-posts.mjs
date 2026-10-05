#!/usr/bin/env node
/**
 * 截图 · #225 a1 ④:写作中心 + 报价中心,**L0 与服务商各一张、深浅各一** = 8 张。
 *
 * 🔴 挂的是**真页面**(WritingHall / OnlineQuoteFlow),不是自己搭的示意块 ——
 *    示意块能证明"这段 JSX 长这样",证不了"这段 JSX 在页面上会出现"。
 * 🔴 每张都量一次真实主题亮度,标称与实测不符就**不出这张**
 *    (深色那张其实是浅色的,会被当证据用)。
 * 🔴 文件名与内容必须对得上:一张名字和内容对不上的截图,比没有截图更坏。
 *    出图前断言页面上确实有本单要看的那串字,断言不过就不出图、打印原因。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a225-shot-'));
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });

const require_ = createRequire(import.meta.url);
function unusable(what, err) {
    console.log(`FAIL 截图不可用:${what} —— ${String((err && err.message) || err).slice(0, 400)}`);
    process.exit(1);
}
let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { unusable('esbuild / playwright 取不到', err); }

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
import { WritingHall } from ${q('src/pages/Writing/WritingHall.tsx')};
import OnlineQuoteFlow from ${q('src/pages/Quote/OnlineQuoteFlow.tsx')};
function Shell({ entry, children }: { entry: string; children: React.ReactNode }) {
    return (
        <MemoryRouter initialEntries={[entry]}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <div style={{ padding: 16 }}>{children}</div>
            </ClientProvider></PricingProvider></OnboardingProvider>
          </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
        </MemoryRouter>
    );
}

const SURFACES: Record<string, { el: React.ReactNode; entry: string }> = {
    /* 写作中心:?quote_id=7001 会自动选中项目并拉详情 —— 「需 N 篇 / 授权 M 槽」在词行上 */
    writing: { el: <WritingHall />, entry: '/writing?quote_id=7001' },
    /* 报价中心:会话 status=pricing_pending_review ⇒ 走到 Step4Review(下拉与换算句在那一步) */
    quote: { el: <OnlineQuoteFlow />, entry: '/quote' },
};

let root: any = null;
(globalThis as any).__mount = function (el: HTMLElement, surface: string, nonce?: number) {
    if (!root) root = createRoot(el);
    const s = SURFACES[surface];
    root.render(<div key={String(surface) + ':' + String(nonce || 1)}><Shell entry={s.entry}>{s.el}</Shell></div>);
};
`, 'utf8');

/* ?raw 后缀(SafeMarkdown 等会用)在 esbuild 里要自己接 */
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
} catch (err) { unusable('打包失败', err); }

let cssName = '';
try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    cssName = c[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, cssName), 'utf8'), 'utf8');
} catch (err) { unusable('取不到真 CSS —— 裸 DOM 上什么都没有尺寸,截出来不是产品的样子', err); }
console.log(`  (真 CSS:dist/assets/${cssName})`);

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a225</title>
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

/* ── 夹具 ───────────────────────────────────────────────────────────────
   🔴 槽与篇**故意取不同的数**,而且不成整数倍:
      同一个数两种量词看不出量词错没错(注毒 U1 就是被 7×5=35 这种巧合躲过去的)。
   词1:6 槽 → 19 条 · 词2:3 槽 → 3 条(相等 ⇒ 不出「授权 N 槽」旁注)· 词3:2 槽 → 7 条 */
const KEYWORDS_WRITING = [
    { id: 1, keyword: '工业除尘设备厂家', required_articles: 6, planned_posts_default: 19, status: 'pending' },
    { id: 2, keyword: '车间粉尘治理方案', required_articles: 3, planned_posts_default: 3, status: 'pending' },
    { id: 3, keyword: '布袋除尘器价格', required_articles: 2, planned_posts_default: 7, status: 'pending' },
];
const TOTAL_PLANNED = 29;   // = 19 + 3 + 7,由服务端给;前端不自己加
const TOTAL_SLOTS = 11;     // = 6 + 3 + 2

const tier = (label, share, prob, stars, price, articles) => ({
    label, target_share: share, ai_probability: prob, stars, total_price: price, total_articles: articles,
});
const pk = (id, keyword) => ({
    id, keyword, category_label: '核心词',
    entry: { price: 1200, articles: 2 },
    standard: { price: 2400, articles: 4 },
    flagship: { price: 4200, articles: 7 },
    difficulty_score: 62, value_score: 71, search_volume: 880, sem_price: 6.4,
    price_locked_until: null, price_lock_note: '本次未写入共享缓存,无锁期',
});
const PRICING_DATA = {
    generated_at: '2026-09-15T02:00:00Z',
    tiers: {
        entry: tier('入门', 0.5, '问 2 次', 3, 7200, 6),
        standard: tier('标准', 0.65, '问 3 次', 4, 14400, TOTAL_SLOTS),
        flagship: tier('旗舰', 0.75, '问 4 次', 5, 25200, 19),
    },
    keywords: KEYWORDS_WRITING.map((k) => pk(k.id, k.keyword)),
};
const SESSION = {
    token: 'qa-tok-225', quote_id: 7001, brand_name: 'QA 夹具客户', industry: '环保设备',
    city: '广州', status: 'pricing_pending_review', selected_count: 3, selected_tier: 'standard',
    final_price: 14400, created_at: '2026-09-14T02:00:00Z',
};
const MEDIA_MIX = {
    capacity_total: TOTAL_SLOTS,
    mix: { focus_media_anchor: 5, industry_platform: 4, douyin_doubao_only: 2 },
    ratio: { used: 1.8, source: 'industry' },
    delivery_perspective: 'self_media',
    posts_estimate_total: TOTAL_PLANNED,
};

const OUT = join(ROOT, '..', '..', 'a225-shots');
mkdirSync(OUT, { recursive: true });

/* 出图前的"名副其实"断言:页面上必须真有本单要看的那串字 */
const MUST_SEE = {
    writing: [/需\s*\d+\s*篇/, /授权\s*\d+\s*槽/],
    quote: [/发布口径/, /本次交付\s*\d+\s*槽/, /约需\s*\d+\s*条左右/],
};
/* 🔴 两种身份用**同一份**必见清单。
   第一版给 L0 的报价页配了**空清单** —— 空清单恒真,那两张图等于没验过就出了
   (和"删掉目标后负向断言永远绿"同一个病)。
   实测:`/quote` 由 `ProtectedRoute requiredModule="quote"` 按**模块权限**放行,
   不按 `agent_level`;`OnlineQuoteFlow` 内部没有身份分支,所以两种身份看到的是同一屏。
   「客户不要看到」那条由结构锁 Q4g 守在 `Selection/**`(C 端 token 页),不在这里。 */

const browser = await playwright.chromium.launch();
let shot = 0, skipped = 0;
try {
    for (const theme of ['light', 'dark']) {
        for (const [who, agentLevel] of [['l0', 0], ['agent', 1]]) {
            for (const surface of ['writing', 'quote']) {
                const page = await browser.newPage({ viewport: { width: 1440, height: 1400 } });
                const ME = {
                    success: true,
                    user: { id: 112, username: 'qa', role: 'user', agent_level: agentLevel, is_admin: false, permissions: [], user_mode: agentLevel >= 1 ? 'agent' : 'user' },
                    id: 112, username: 'qa', role: 'user', agent_level: agentLevel, is_admin: false,
                    permissions: [], user_mode: agentLevel >= 1 ? 'agent' : 'user',
                };
                await page.addInitScript(() => { localStorage.setItem('omnirank_token', 'qa-token'); });
                await page.route('**/api/**', async (route) => {
                    const u = new URL(route.request().url());
                    const p = u.pathname;
                    const json = (b) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(b) });
                    if (p.includes('/auth/me')) return json(ME);
                    if (p.includes('/organization')) return json({ success: true, overview: { identity: { capabilities: ['quote.create', 'team.output_handoff'] } } });
                    /* 写作中心 */
                    if (/\/api\/writing\/projects$/.test(p)) {
                        return json({ projects: [{
                            id: 7001, quote_ids: [7001], brand_id: 629, brand_name: 'QA 夹具客户',
                            industry: '环保设备', keyword_count: KEYWORDS_WRITING.length,
                            total_required_articles: TOTAL_SLOTS, monthly_price: 14400,
                            writing_status: 'in_progress', confirmed_at: '2026-09-14T02:00:00Z',
                        }] });
                    }
                    if (/\/api\/writing\/projects\/\d+$/.test(p)) {
                        return json({
                            keywords: KEYWORDS_WRITING, topics: [],
                            total_planned_posts_default: TOTAL_PLANNED,
                        });
                    }
                    /* 报价中心 */
                    if (p.includes('/keyword-selection/list')) return json({ sessions: [SESSION] });
                    if (/\/api\/s\/[^/]+$/.test(p)) {
                        return json({
                            ...SESSION, pricing_data: PRICING_DATA,
                            selected_ids: [1, 2, 3], custom_keywords: [], clusters_data: null,
                        });
                    }
                    if (/\/api\/quotes\/\d+\/media-mix$/.test(p)) {
                        /* 口径参数原样回显 —— 下拉切换时能看出请求真的带上了 */
                        const persp = u.searchParams.get('perspective') || MEDIA_MIX.delivery_perspective;
                        return json({ ...MEDIA_MIX, delivery_perspective: persp });
                    }
                    if (/\/api\/quotes$/.test(p)) return json({ items: [] });
                    return json({ success: true, projects: [], topics: [], keywords: [], articles: [], clients: [], media: [], quotes: [], items: [], sessions: [] });
                });
                await page.goto(`http://127.0.0.1:${PORT}/index.html`);
                /* 🔴 主题 class 要在 goto 之后挂:addInitScript 跑在文档解析之前,
                   那一刻 documentElement 还不在。 */
                if (theme === 'dark') await page.evaluate(() => document.documentElement.classList.add('dark'));
                /*
                 * 🔴 **确认之后再挂**,不是挂了就截。
                 *    第一版挂完直接等 —— 页面永远停在"暂无在线报价记录"。
                 *    查出来:首屏发出的 `api.get` 全部 `canceled` ——
                 *    `/api/auth/me` 确认会话时 `rotateAuthorizationScope()` 会把
                 *    **确认前发出**的在途请求掐掉(fail-closed 重放设计)。
                 *    真实应用里后续会重来,harness 里只发一次,于是一次空列表定格。
                 *    所以:先挂一次点火(让 AuthProvider 去发 /me),等确认落定,
                 *    再换 key 重挂 —— 这一次所有 effect 都在 confirmed 之后跑。
                 */
                await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s), surface);
                await page.waitForTimeout(1800);
                await page.evaluate((s) => globalThis.__mount(document.getElementById('root'), s, 2), surface);
                await page.waitForTimeout(Number(process.env.WAIT1 || 2800));
                if (surface === 'quote') {
                    /*
                     * 两步真点击。
                     * 🔴 用 Playwright 的**真鼠标点**,不用 `el.click()`:
                     *    DOM 直发的 click 在这两处都没让 React 的 onClick 跑起来
                     *    (实测「选中标记」false),换真点击立刻 true。
                     * ① 侧栏选中那份报价 ⇒ status=pricing_pending_review ⇒ 走到 Step4Review
                     *    (下拉与三档槽数在那一步)。
                     * ② 展开一行关键词 ⇒ `WhyThisPrice` 才渲染 ——
                     *    「本次交付 N 槽 · 按…约需 M 条左右」这句在它里面。
                     *    不展开就截图 = 截了一张没有本单主角的图。
                     */
                    /* 🔴 用 Playwright 自己的文字定位 —— 先 setAttribute 再按属性找的做法
                       在这页会扑空:标记完那一瞬 React 重渲染,自定义属性随节点一起没了。 */
                    const clickText = async (needle, what) => {
                        /* 🔴 `.filter({ visible: true })` 不能省:关键词明细有**卡片版和表格版两套**,
                           1440 宽下卡片版是隐藏的,`.first()` 正好选中它 ⇒ 一直超时,
                           看起来像点不到,其实是点在了一个本来就不该显示的副本上。 */
                        try { await page.getByText(needle, { exact: false }).filter({ visible: true }).first()
                            .click({ timeout: 6000 }); return true; }
                        catch (e) { console.log('    🔴 点不到' + what + ':' + String(e).slice(0, 110)); return false; }
                    };
                    await clickText('QA 夹具客户', '报价行');
                    await page.waitForTimeout(3000);
                    await clickText('工业除尘设备厂家', '关键词行');
                    await page.waitForTimeout(2500);
                }
                const lum = await page.evaluate(() => {
                    const c = getComputedStyle(document.body).backgroundColor;
                    const m = c.match(/[\d.]+/g) || ['255', '255', '255'];
                    return (Number(m[0]) + Number(m[1]) + Number(m[2])) / 3;
                });
                const looksDark = lum < 128;
                const name = `${surface}_${who}_${theme}`;
                if ((theme === 'dark') !== looksDark) {
                    console.log(`  🔴 ${name}:主题标称与实测不符(亮度 ${Math.round(lum)})—— 不出这张`);
                    skipped += 1; await page.close(); continue;
                }
                const text = await page.evaluate(() => document.body.innerText || '');
                if (process.env.PROBE_TEXT && surface === 'quote') console.log('--8<--' + text.slice(0, 900) + '--8<--');
                const want = MUST_SEE[surface];
                const missing = want.filter((re) => !re.test(text));
                if (missing.length) {
                    console.log(`  🔴 ${name}:页面上找不到 ${missing.map(String).join(' / ')} —— 不出这张`
                        + `(名字和内容对不上的截图会被当证据用)`);
                    skipped += 1; await page.close(); continue;
                }
                const f = join(OUT, `${name}.png`);
                await page.screenshot({ path: f, fullPage: true });
                console.log(`  ${name}: 亮度 ${Math.round(lum)} → ${f}`);
                shot += 1;
                await page.close();
            }
        }
    }
} finally {
    await browser.close();
    server.close();
}
console.log(`\n出图 ${shot} 张,跳过 ${skipped} 张(每张都量过主题、验过页面上真有那串字)`);
if (skipped > 0) process.exit(1);
