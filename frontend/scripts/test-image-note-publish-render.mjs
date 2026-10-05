#!/usr/bin/env node
/**
 * 判据 · #186 图文发布面板 —— 真 chromium · 真组件 · **真请求体**。
 *
 * 🔴 最要紧的一条是 P4:点「发布」之后**拦下真正飞出去的那个 body**,
 *    逐项断言恰好七个键、且没有 title/body/images。
 *    Review 09-13 明确提醒:服务端今天对多余键是**静默忽略**不是 422 ——
 *    所以「发了没报错」不是契约证据,只有请求体本身是。
 *
 * 🔴 价只从 publish-preview 来:改桩里的数字,屏幕上的合计必须跟着变(N4 行为臂)。
 *
 * 跑法:cd frontend && npm run build && node scripts/test-media-list-page-race.mjs
 */
import { mkdirSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync , mkdtempSync } from 'node:fs';
import { createServer } from 'node:http';
import { tmpdir } from 'node:os';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
/*
 * 🔴 临时目录必须留在 `node_modules/.cache` **下面**:esbuild 解析 `react` 这类
 *    裸 import 是从 entry 所在目录逐级往上找 node_modules。放到系统 tmp 会当场
 *    「Could not resolve "react-dom/client"」——本机实测过。
 *    唯一性靠 mkdtemp 的随机后缀,不靠换盘。
 */
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

/*
 * 🔴 [2026-09-13 · Review 复核抓到] 打包产物**必须每进程一份**,不能写死在
 *    `node_modules/.cache/<固定名>`。两处会咬人:
 *      ① 同一棵树里两把渲染闸同时跑(或注毒与另一把闸并行)——
 *         谁后 build,谁的 bundle 被对方的页面装走;
 *      ② 复核树用 junction 共享 node_modules —— 两棵树共用同一个 bundle.js,
 *         打了毒的那边可能装上干净包。
 *    后果最恶劣的形态:**注毒判决翻面**(Review 第一遍跑 MP5 是绿的,
 *    因为它的页面装的是我这边刚 build 出来的干净包)。红绿都不可信。
 *    ⇒ mkdtemp 一进程一份,跑完删。
 */
const outDir = mkdtempSync(join(CACHE_ROOT, 'a186-publish-'));
/* 🔴 a179-cleanup:跑完删掉这一份,免得 .cache 里堆满 bundle
   (一次全量注毒 = 17 发 × 两把闸 = 34 份)。进程怎么退出都删。 */
process.on('exit', () => { try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ } });
rmSync(outDir, { recursive: true, force: true });
mkdirSync(outDir, { recursive: true });
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
import { ImageNoteList } from ${q('src/pages/Publishing/ImageNoteList.tsx')};
import { useClientContext } from ${q('src/context/ClientContext.tsx')};
import { ClientSwitcherSidebar } from ${q('src/components/layout/ClientSwitcherSidebar.tsx')};

function ImageNoteListHost() {
    const { currentBrandId } = useClientContext();
    return <ImageNoteList brandId={currentBrandId ?? null} preGeoPostId={null} />;
}

(globalThis as any).__mount = function (el: HTMLElement, entry?: string) {
    createRoot(el).render(
        <MemoryRouter initialEntries={[entry || '/publish']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              {/* 🔴 把**真的左上角**一起挂上:本单说的就是"跟着左上角走",
                  用真控件建立前置状态,判据才不是拿我自己新加的控件自证。 */}
              <div data-testid="real-sidebar"><ClientSwitcherSidebar /></div>
              {/* 🔴 [#204 a2] 发布 #203 收口到**发布中心的图文档**,制作台本单退役。
                  所以这里挂的是 ImageNoteList(那一档真正渲染的东西)。
                  🔴 这段注释里**不许出现反引号** —— 它整段住在外层模板字符串里,
                     一个反引号就把模板提前终结(本轮第三次栽在这)。
                  发布区仍要先点开那一行的「发布」才出现 —— 见 armFirstRow。
                  🔴 挂的必须是**产品真的渲染的那个组件**:挂一个我自己搭的壳
                  等于拿自己造的控件自证。brandId 从真的 ClientContext 取,
                  与产品里 PublishCenter 传它的方式同源。 */}
              <ImageNoteListHost />
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
            // 🔴 Vite 自己懂 `import x from './a.jpg'`，esbuild 不懂 —— 不配 loader 就是
            //    「打包失败 ⇒ 判据不可用」(而不是静默少一张图)。用 dataurl 而不是置空：
            //    样图本身就是 #188 要验的东西(「风格这里必须让人看到成品是什么样」)，
            //    置空会让所有样图臂在真缺陷下也照样绿。
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
<html lang="zh"><head><meta charset="utf-8"><title>a186 harness</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>
`, 'utf8');

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8' };
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
const AUTH_ME = {
    success: true,
    user: { id: 112, username: 'qa', role: 'user', agent_level: 1, is_admin: false, permissions: [], user_mode: 'agent' },
};
const CLIENTS = [{ id: 101, name: 'A 客户', industry: '教育培训', diagnosis_count: 1, quote_count: 1 }];
const CTX = { brand: { id: 101, name: 'A 客户', industry: '教育培训', diagnosis_count: 1 }, profile: null };

/** 两条 ready:一条有版本(可发),一条没有(等待版本)。 */
/*
 * 🔴 [#204 a2] 夹具形状照 **0913e 尖 e7669e699** 的真回包(不是我自己分支里的旧副本)。
 *
 *    出处:`db/geo_douyin_db.py:19-27 _POST_FIELDS` + `api/geo_douyin_api.py:1284-1330`
 *    现场补的 `progress / publish_pending_confirm / failure_reason / progress_* /
 *    card_count(len(oss_keys)) / publish_failure_reason / cover_url(签名)`。
 *    生产实测(Review 09-15 打 brand 629):200、6 帖、**36 键**。
 *
 *    🔴 `failure_reason` 与 `publish_failure_reason` 是**两件事**:
 *       前者是**制作**任务的 error_msg,后者是**发布**被拒的供应商原话
 *       (只在 publish_status='failed' 时非空)。两者混用会让"发布被拒"
 *       显示成"生成失败"。夹具里两个都给,各给各的值。
 *
 *    两档:
 *      ① POSTS_TODAY —— 生产今天的分布:6 帖里 **1 条有 active_revision_id、5 条 null**
 *         (status 全 ready,是 09-13 前的存量,等回填)。
 *         ⇒ 断言"那 5 条发不出去、而且屏幕上说得出为什么",这才是今天真实的 UX。
 *      ② POSTS_MIXED —— 回填之后。断言发布流程能走完。
 */
const _base = (id, keyword, extra) => ({
    id, brand_id: 629, keyword, title: keyword + ' 图文', city: '梅州',
    content_type: 'note', status: 'ready', style_key: 'design_text', aspect_ratio: '3:4',
    oss_keys: ['a.jpg', 'b.jpg', 'c.jpg'], card_count: 3,
    cover_url: 'https://example.invalid/signed/' + id + '.jpg',
    cover_oss_key: 'geo/' + id + '/cover.jpg',
    publish_status: '', published_url: '', published_at: '',
    publish_pending_confirm: false,
    failure_reason: '',            // 制作任务的 error_msg
    publish_failure_reason: '',    // 发布被拒的原话(#195 c1)
    active_revision_id: null,
    ...extra,
});
/** ① 今天:1 条能发,5 条是等回填的存量。 */
const POSTS_TODAY = [
    _base(501, '梅州婚宴酒店', { active_revision_id: 9001 }),
    _base(502, '梅州婚宴价格', {}),
    _base(503, '梅州婚宴场地', {}),
    _base(504, '梅州婚宴实拍', {}),
    _base(505, '梅州婚宴流程', {}),
    _base(506, '梅州婚宴清单', {}),
];
/** ② 回填之后:有版本的能发,没版本的仍然不能(两条对照)。 */
const POSTS_MIXED = [
    _base(501, '梅州婚宴酒店', { active_revision_id: 9001 }),
    _base(502, '梅州婚宴价格', { active_revision_id: null }),
];
/** 整档都发不出去(存量全 null)。 */
const POSTS_ALL_BLOCKED = [
    _base(601, '等回填 A', {}),
    _base(602, '等回填 B', {}),
];

/** 账号目录:只有 1 号该出现。 */
const ACCOUNTS = [
    { id: 11, media_name: '能发图文的号', can_tuwen: 1, blacklist: 0, is_active: true },
    { id: 12, media_name: '不能发图文', can_tuwen: 0, blacklist: 0, is_active: true },
    { id: 13, media_name: '被拉黑', can_tuwen: 1, blacklist: 1, is_active: true },
];

const clickSafe = (l) => l.click({ timeout: 5000 }).then(() => '').catch((e) => String((e && e.message) || e).split(String.fromCharCode(10))[0]);
const textSafe = (l) => l.innerText({ timeout: 2500 }).catch(() => '');
const seen = (page, sel) => page.locator(sel).count();

async function open(browser, plan = {}) {
    const page = await browser.newPage();
    const reqs = [];
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    await page.addInitScript((theme) => {
        localStorage.setItem('omnirank_token', 'qa-token');
        /* 🔴 本 harness **没挂 ThemeProvider**,手挂 class 是安全的
           (挂了 Provider 的地方必须走它自己的 storage key,手挂会被 remove 掉 ——
            两处做法不同是有原因的,别互相抄)。 */
        if (theme === 'dark') document.documentElement.classList.add('dark');
    }, plan.theme || 'light');
    let pollHits = 0;
    let prepareHits = 0;
    const prepareKeys = [];
    await page.route('**/api/**', async (route) => {
        const req = route.request();
        const url = new URL(req.url());
        const path = url.pathname;
        let post = null;
        try { post = req.postDataJSON(); } catch { post = req.postData(); }
        /* 🔴 连 query 一起记:#192 要验"请求真的带了 can_tuwen=1 / search=",
           只记 path 的话那几条臂根本无从判断。 */
        reqs.push({ method: req.method(), path, post, query: url.search });
        const json = (b, s = 200) => route.fulfill({ status: s, contentType: 'application/json', body: JSON.stringify(b) });
        if (path.includes('/auth/me')) return json(AUTH_ME);
        if (path === '/api/client-context/list') return json({ success: true, clients: CLIENTS });
        if (/^\/api\/client-context\/\d+$/.test(path)) return json({ success: true, context: CTX });
        /* 🔴 真回包是 `{"status":"success","posts":rows}`(api/geo_douyin_api.py:1315)。
           少一个 status 键,发布中心那侧按 `data?.status !== 'success'` 直接判失败 ——
           屏幕上是「图文列表没取到」,而桩自以为回的是成功。
           (制作台那侧的解析不看这个键,所以旧桩一直没露馅:夹具是按**消费方**写的,
            换一个消费方就塌。形状要照后端。) */
        if (path === '/api/geo-douyin/posts') {
            /* 🔴 发布成功之后重拉,后端回的那一行**已经是 published** ——
               桩不跟着变的话,重拉回来还是"未发布",而那不是真实世界会发生的事,
               判据就会在一个不可能出现的状态上通过或失败。 */
            const base = plan.posts || POSTS_MIXED;
            const rows = plan.publishedIds && plan.publishedIds.size
                ? base.map((r) => (plan.publishedIds.has(r.id)
                    ? { ...r, publish_status: 'published', published_at: '2026-09-15T10:00:00Z',
                        published_url: 'https://example.invalid/note/' + r.id }
                    : r))
                : base;
            return json({ status: 'success', posts: rows });
        }
        if (path === '/api/meijiehezi/short-video') {
            /* 🔴 [#192] 桩同构真接口:回 media + total。
               emptyAccounts / 搜索无结果两种空,**分开**给 —— 它们在界面上是两句话。 */
            if (plan.emptyAccounts) return json({ media: [], total: 0 });
            if (url.searchParams.get('search')) {
                const q = url.searchParams.get('search');
                const hit = (plan.accounts || ACCOUNTS).filter(
                    (a) => String(a.media_name || '').includes(q));
                return json({ media: hit, total: hit.length });
            }
            const rows = plan.accounts || ACCOUNTS;
            return json({ media: rows, total: plan.accountTotal ?? rows.length });
        }
        if (/prepare-publish-media-v2$/.test(path)) {
            if (plan.prepareFails) return json({ code: 'PREPARE_FAILED', message: '素材没能准备好' }, 500);
            const pid = Number(path.split('/')[4]);
            /*
             * 🔴 [#196] prepare-v2 是**异步**的:真后端只 claim 一行 `preparing` 就返回,
             *    上传与 mark_ready 由 cron worker 做。桩要照这个形状来 ——
             *    原来这个桩**一上来就返 ready**,于是"前端只调一次"这个缺陷
             *    在判据里根本不可能显形(桩替产品把活干了)。
             *    `prepareStates` 按次数给状态,并记下每次的 Idempotency-Key。
             */
            prepareHits += 1;
            prepareKeys.push(route.request().headers()['idempotency-key'] || '');
            const seq = plan.prepareStates;
            const st = Array.isArray(seq)
                ? (seq[Math.min(prepareHits - 1, seq.length - 1)] || 'ready')
                : 'ready';
            /* 🔴 [#196 a3] 回包照 C 的 196-c1 形状带 `failure_reason`
               (failed/unknown 时非空,其余空串)。桩不带它的话,
               「字段到了没人读」这个缺陷在判据里根本显不出形。 */
            return json({
                status: 'accepted', replayed: prepareHits > 1,
                geo_post_id: pid, post_revision_id: 9001, state: st,
                prepared_artifact_id: `art-${pid}`, manifest_hash: `mh-${pid}`,
                failure_reason: (st === 'failed' || st === 'unknown')
                    ? (plan.failureReason ?? '第 2 张图读取为空') : '',
            });
        }
        if (path.endsWith('/image-notes/publish-preview')) {
            const items = (post?.items || []).map((it) => ({
                item_request_id: it.item_request_id,
                geo_post_id: it.geo_post_id,
                post_revision_id: it.post_revision_id,
                media_id: it.media_id,
                final_price_points: plan.unitPoints ?? 130,
                publish_price_fingerprint: `fp-${it.geo_post_id}`,
            }));
            return json({
                status: 'success', items,
                total_price_points: (plan.unitPoints ?? 130) * items.length,
            });
        }
        if (path.endsWith('/image-notes/publish-batch')) {
            /*
             * 🔴 [#192 a3] 现场那个 403:组织成员没有 publish.execute。
             *    合同里**带 actions**(交给团队负责人 / 联系管理员),改前一条都没渲染。
             *    另塞一个**认不出的 id**,验"降级成文字而不是死按钮"。
             */
            if (plan.publishForbidden) {
                return json({
                    detail: {
                        code: 'ABILITY_NOT_GRANTED',
                        message: '你还没有这个操作权限',
                        reason: '组织成员的能力快照里没有 publish.execute',
                        actions: [
                            { id: 'handoff', label: '交给团队负责人', type: 'nav' },
                            { id: 'contact_admin', label: '联系管理员', type: 'contact' },
                            { id: 'unknown_thing', label: '某个前端还不认识的出口', type: 'nav' },
                        ],
                        retryable: false,
                    },
                }, 403);
            }
            if (plan.submitRejects) {
                return json({ detail: { code: 'BAD_REQUEST_FIELDS', message: '请求里有不该有的字段' } }, 400);
            }
            return json({ status: 'accepted', command_id: 'cmd-1', command_status: 'accepted' });
        }
        if (/\/image-notes\/commands\//.test(path)) {
            pollHits += 1;
            const terminal = pollHits >= 2;
            /* 🔴 收敛到终态 = 服务端那一行已经变成 published。
               记下来,下面 /posts 重拉时照这个回 —— 桩的两个口要讲同一个故事,
               各讲各的话,判据就会在一个不可能出现的状态上通过。 */
            if (terminal) { plan.publishedIds = plan.publishedIds || new Set(); plan.publishedIds.add(501); }
            return json({
                status: 'success', command_id: 'cmd-1',
                command_status: terminal ? 'succeeded' : 'running',
                items: [{
                    item_request_id: 'x', geo_post_id: 501, state: terminal ? 'published' : 'publishing',
                    retryable: false, published_url: terminal ? 'https://example.test/p/1' : null,
                }],
            });
        }
        return json({ success: true, status: 'success', items: [], media: [], posts: [] });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    /*
     * 🔴 主题 class 在 **goto 之后**挂,不在 addInitScript 里。
     *    实测 addInitScript 那条不生效(量出来 html.class 是空的)——
     *    而"没生效"与"生效了但页面就是浅色"在最终读数上同形。
     *    #189 那把 harness 踩过一模一样的,这里直接照搬结论。
     */
    if ((plan.theme || 'light') === 'dark') {
        await page.evaluate(() => document.documentElement.classList.add('dark'));
    }
    await page.evaluate(() => (globalThis).__mount(document.getElementById('root')));
    await page.waitForTimeout(2400);
    return { page, reqs, pageErrors, pollCount: () => pollHits , prepareKeys: () => [...prepareKeys], prepareHits: () => prepareHits };
}

async function pickClient(page) {
    const bar = page.locator('[data-testid="real-sidebar"]');
    await clickSafe(bar.locator('button').first());
    await page.waitForTimeout(300);
    await clickSafe(bar.locator('text=A 客户').first());
    await page.waitForTimeout(1500);
}

/**
 * 走到"可发布"那一步。
 * 🔴 [#188] 形状变了:原来是"在批量面板里勾第 501 行 + 给它选个账号";
 *    现在是"在 501 那张卡上点开行内发布 + 点一枚账号 chip"。
 *    动作换了,**它要建立的前置状态没换**(选中一条 + 指定账号 + 素材与报价就绪)。
 */
async function armFirstRow(page) {
    const card = page.locator('[data-post-id="501"]');
    await clickSafe(card.locator('[data-testid="pub-imagenote-publish-toggle"]'));
    await page.waitForTimeout(800);
    await clickSafe(page.locator('[data-testid="inp-account-11"]'));
    await page.waitForTimeout(1400);
}

const browser = await playwright.chromium.launch();
try {
    // ══ P1 行状态 ═════════════════════════════════════════════════════
    section('P1 有版本才可勾');
    {
        const { page, pageErrors } = await open(browser);
        await pickClient(page);
        check(pageErrors.length === 0, 'P1a 零 pageerror', pageErrors.join(' | ').slice(0, 200) || '干净');

        /**
         * 🔴 P1b–e **重锚(#188 · Review 09-13)**:从批量面板的"行 + 勾选框"
         *    换成区 E 的"卡 + 发布键"。要验的三件事一件没少,而且门槛更高:
         *      b 两条都渲染(不静默丢);
         *      c 有版本的那条**给**发布键;
         *      d 没版本的那条**根本没有**发布键 —— 原来只是"勾选框置灰",
         *        现在是压根不渲染(Review:不给按钮、也不做灰行,灰按钮只会被反复点)。
         *      e 「为什么发不了」这句话升成**区域级一行**,不再每卡一行灰字 ⇒ 并进 P1f。
         */
        check(await seen(page, '[data-testid="pub-imagenote-row"]') === 2, 'P1b 两张卡都在(不静默丢)');
        const has501 = await page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]')
            .count().catch(() => 0);
        check(has501 === 1, 'P1c 有版本那条给发布键', String(has501));
        const has502 = await page.locator('[data-post-id="502"] [data-testid="pub-imagenote-publish-toggle"]')
            .count().catch(() => 0);
        check(has502 === 0,
            'P1d 🔴 没有版本那条**压根没有**发布键(拿 0 去提交后端整批拒,'
            + '而用户以为是自己操作错了;灰着也不行 —— 灰按钮会被反复点)', String(has502));
        /* 🔴 [#204 a2 重锚] 区域级那句只在**全都发不了**时说(L7c)。
           只有一部分发不了时,原因挂在**那一行**上 —— 否则屏幕上是
           "有的行有发布键、有的没有",用户会以为是自己点错了。 */
        const rowReason = await textSafe(
            page.locator('[data-post-id="502"] [data-testid="pub-imagenote-row-blocked"]'));
        check(/旧作品待系统回填/.test(rowReason),
            'P1e 发不了的那一行**自己带一句原因**(不是整区一句,也不是一个字不说)', rowReason);
        await page.close();
    }
    {
        const { page } = await open(browser, { posts: POSTS_ALL_BLOCKED });
        await pickClient(page);
        /**
         * 🔴 P1f **改锚到区域级那一行**(Review 09-13 明确点名的那条)。
         *    意图完全没变:上线第一天全库 active_revision_id 皆为 NULL,
         *    没有这句话,用户看到的就是一排作品、一个发布键都没有,零线索。
         *    变的只是它的位置与数量:**整区一行**,不是每卡一行。
         */
        check(await seen(page, '[data-testid="pub-imagenote-blocked-note"]') === 1,
            'P1f 🔴 **一条都发不了**时给一句解释(整区恰好一行)—— 这在上线第一天就是常态'
            + '(C 09-13:存量 36 条 active_revision_id 全为 NULL,等 J4 回填),'
            + '没有它用户看到的就是一排作品、一个发布键都没有,零线索');
        check(await seen(page, '[data-testid="pub-imagenote-publish-toggle"]') === 0,
            'P1f2 🔴 而且此时**一个发布键都不该有**(挡"说明也给了、灰按钮也摆了")');
        await page.close();
    }

    // ══ P2 账号只列能发图文的 ═════════════════════════════════════════
    section('P2 账号过滤');
    {
        const { page } = await open(browser);
        await pickClient(page);
        /**
         * 🔴 P2 **重锚**:账号从"每行一个 <select>"换成行内的一排 chip。
         *    过滤规则(只列 can_tuwen 且未拉黑、在用的)一个字没改 ——
         *    换的是控件,不是判据的意思。要先点开那张卡的发布区才看得到。
         */
        await clickSafe(page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]'));
        await page.waitForTimeout(900);
        const opts = await page.locator('[data-testid="inp-account-chips"] button')
            .allInnerTexts().catch(() => []);
        check(opts.length === 1, 'P2a 只列出 1 个可用号(chip)', JSON.stringify(opts));
        check(!opts.some(t => /不能发图文|被拉黑/.test(t)),
            'P2b 🔴 不能发图文/被拉黑的号**不出现** —— 列出来就是死选项(选了必被拒)');
        check(!opts.some(t => /^\s*\d+\s*$/.test(t)),
            'P2c 🔴 chip 上是**账号名字**,不是 media_id(id 是我们的内部编号,用户不认得)',
            JSON.stringify(opts));
        await page.close();
    }

    // ══ P3 价只来自 preview ═══════════════════════════════════════════
    section('P3 价来自服务端');
    {
        const { page } = await open(browser, { unitPoints: 130 });
        await pickClient(page); await armFirstRow(page);
        /**
         * 🔴 P3 **重锚**:原来价在单独一行 `inp-summary`;行内发布把它放到**按钮上**
         *    (元指令 2 原文:不弹 Dialog、按钮直接标价 —— 确认动作就是那颗标了价的按钮)。
         *    要验的事没变、而且更严:屏幕上那个数必须**等于**服务端 preview 给的数。
         */
        check(/130 算力/.test(await textSafe(page.locator('[data-testid="inp-publish"]'))),
            'P3a 按钮上的价 = 服务端给的数',
            await textSafe(page.locator('[data-testid="inp-publish"]')));
        await page.close();
    }
    {
        // 🔴 换个数字:屏幕必须跟着变。不变 = 前端在自己算(或者写死了)
        const { page } = await open(browser, { unitPoints: 777 });
        await pickClient(page); await armFirstRow(page);
        check(/777 算力/.test(await textSafe(page.locator('[data-testid="inp-publish"]'))),
            'P3b 🔴 改 preview 的数字 ⇒ 屏幕同步变(证明它真的来自服务端,不是前端算的)',
            await textSafe(page.locator('[data-testid="inp-summary"]')));
        await page.close();
    }

    // ══ P4 🔴 真请求体:恰好七个键 ═════════════════════════════════════
    section('P4 提交体(拦真 body)');
    {
        const { page, reqs } = await open(browser);
        await pickClient(page); await armFirstRow(page);
        const btn = page.locator('[data-testid="inp-publish"]');
        check(await btn.isEnabled().catch(() => null) === true, 'P4a 备齐之后发布键可点');
        await clickSafe(btn);
        await page.waitForTimeout(900);
        const sent = reqs.filter(r => r.path.endsWith('/image-notes/publish-batch'));
        check(sent.length === 1, 'P4b 真的发出了一次提交(分母非空)', `${sent.length} 次`);
        const body = sent[0]?.post || {};
        const items = Array.isArray(body.items) ? body.items : [];
        check(items.length === 1, 'P4c 一项', JSON.stringify(Object.keys(body)));
        const keys = Object.keys(items[0] || {}).sort();
        check(keys.length === 7,
            'P4d 🔴🔴 **真正飞出去的** body 每项恰好七个键 —— 服务端对多余键是'
            + '静默忽略不是 422,所以"发了没报错"不是证据,只有请求体本身是',
            `${keys.length} 个:${keys.join(',')}`);
        for (const forbidden of ['title', 'body', 'images', 'image_urls', 'content']) {
            check(!(forbidden in (items[0] || {})),
                `P4e 🔴 body 里没有 ${forbidden}(内容来自冻结版本,塞进来就是绕过冻结)`);
        }
        check(typeof body.expected_total_price_points === 'number',
            'P4f 总价原样回报(不重算、不摊平)', String(body.expected_total_price_points));
        await page.close();
    }

    // ══ P5 轮询到终态停 ═══════════════════════════════════════════════
    section('P5 轮询终止');
    {
        const { page, reqs, pollCount } = await open(browser);
        await pickClient(page); await armFirstRow(page);
        await clickSafe(page.locator('[data-testid="inp-publish"]'));
        await page.waitForTimeout(8000);
        const n1 = pollCount();
        await page.waitForTimeout(6000);
        const n2 = pollCount();
        check(n1 >= 1, 'P5a 提交后真的开始轮询', `${n1} 次`);
        check(n2 === n1,
            'P5b 🔴 到终态就**停**(再等 6 秒一次都不多打)—— 终态还接着轮是白打接口',
            `${n1} → ${n2}`);
        /*
         * 🔴 [#204 a2 换锚不换意图] 终态显示的**地方**跟着宿主变了。
         *    制作台里面板留在原处,终态写在面板结果区;
         *    发布中心发布成功后 `onPublished` 是「收起面板 + 重拉列表」,
         *    终态写在**那一行**上 —— 而且那才是服务端的真状态,
         *    不是面板里留下的最后一帧。
         *    意图一个字没变:① 机器态要翻成人话;② 最终链接要给得出来。
         */
        const rowTxt = await textSafe(page.locator('[data-post-id="501"]'));
        check(/已发布/.test(rowTxt),
            'P5c 终态用人话显示(机器态 published → 「已发布」)', rowTxt.slice(0, 80));
        check(await seen(page, '[data-post-id="501"] [data-testid="pub-imagenote-url"]') === 1,
            'P5d 给最终链接');
        await page.close();
    }

    // ══ P6 拒绝时把码和原话都显示 ═════════════════════════════════════
    section('P6 服务端拒绝');
    {
        const { page } = await open(browser, { submitRejects: true });
        await pickClient(page); await armFirstRow(page);
        await clickSafe(page.locator('[data-testid="inp-publish"]'));
        await page.waitForTimeout(900);
        const err = await textSafe(page.locator('[data-testid="inp-submit-error"]'));
        check(/请求里有不该有的字段/.test(err), 'P6a 显示服务端**原话**', err);
        check(/BAD_REQUEST_FIELDS/.test(err),
            'P6b 🔴 连**错误码**一起显示(吞掉码只剩一句笼统的"操作未完成",定位信息就没了)');
        await page.close();
    }

    // ══ P7 禁用理由在按钮外面 ═════════════════════════════════════════
    section('P7 禁用理由');
    {
        const { page } = await open(browser);
        await pickClient(page);
        /**
         * 🔴 P7 **重锚**:行内发布区要先点开那张卡才存在(原来面板一直摆在页面上)。
         *    要验的仍是「还没选账号时按钮点不动,而且**当场说出为什么**」——
         *    #179 根因①:灰按钮不说话 = 死按钮。
         *    这里**故意不选账号**,停在"缺账号"那一格。
         */
        await clickSafe(page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]'));
        await page.waitForTimeout(900);
        const btn = page.locator('[data-testid="inp-publish"]');
        check(await btn.isDisabled().catch(() => null) === true, 'P7a 还没选账号 ⇒ 点不动');
        const reason = page.locator('[data-testid="inp-disabled-reason"]');
        check(await reason.count() === 1, 'P7b 理由行在');
        const inside = await page.evaluate(() => {
            const b = document.querySelector('[data-testid="inp-publish"]');
            const r = document.querySelector('[data-testid="inp-disabled-reason"]');
            return !!(b && r && b.contains(r));
        }).catch(() => true);
        check(inside === false,
            'P7c 🔴 理由**不在按钮子树里** —— 在里面就会跟着 disabled 的透明度一起变淡(#179 根因①)');
        const vis = await reason.evaluate((el) => {
            const cs = getComputedStyle(el);
            return { opacity: Number(cs.opacity), fs: parseFloat(cs.fontSize) };
        }, undefined, { timeout: 2500 }).catch(() => ({ opacity: -1, fs: 0 }));
        check(vis.opacity >= 0.9 && vis.fs >= 13, 'P7d 看得清', JSON.stringify(vis));
        await page.close();
    }

    // ══ 截图(质量标准 §三:深 / 浅 / 375) ══════════════════════════
    if (process.env.A186_SHOT_DIR) {
        section('截图');
        const { page } = await open(browser);
        await pickClient(page); await armFirstRow(page);
        for (const [theme, w, tag] of [['light', 1280, 'light'], ['dark', 1280, 'dark'], ['dark', 375, 'mobile']]) {
            await page.setViewportSize({ width: w, height: 780 });
            await page.evaluate((t) => {
                document.documentElement.classList.toggle('dark', t === 'dark');
                document.documentElement.style.background = t === 'dark' ? '#0b1220' : '#ffffff';
            }, theme);
            await page.waitForTimeout(300);
            const path = `${process.env.A186_SHOT_DIR}/A_186_publish_panel_${tag}.png`;
            await page.locator('[data-testid="image-note-publish-panel"]').screenshot({ path })
                .catch(async () => { await page.screenshot({ path }); });
            console.log(`  (已写 ${tag}:${path})`);
        }
        // 375 宽不许横向滚动(质量标准 §一.8)
        await page.setViewportSize({ width: 375, height: 780 });
        await page.waitForTimeout(300);
        const overflow = await page.evaluate(() => {
            const el = document.querySelector('[data-testid="image-note-publish-panel"]');
            return el ? el.scrollWidth - el.clientWidth : -1;
        }).catch(() => -1);
        check(overflow <= 1, 'S1 🔴 375 宽下面板不横向滚动', `溢出 ${overflow}px`);
        await page.close();
    }
    // ══ P9 #196 素材准备:轮询到终态 ═══════════════════════════════
    section('P9 #196 素材准备是异步的,前端要读到终态');
    {
        /*
         * 🔴 现场(0913b):prepare-v2 200 ⇒「正在准备发布素材…」⇒ **3 分钟零后续请求**。
         *    桩照真形状给:前两次 `preparing`,第三次 `ready`。
         *    改动前的代码只调一次 ⇒ 永远停在第一次那个 preparing。
         */
        const { page, prepareKeys, prepareHits } = await open(browser, {
            prepareStates: ['preparing', 'preparing', 'ready'],
        });
        await pickClient(page);
        /* 🔴 要选一个账号:价格只有选了账号才问得出来(P9e 要看的就是那一步)。
           第一版没选,P9e 报红 —— 那是**我的臂没把页面推到位**,不是产品没算价。 */
        await armFirstRow(page);
        await page.waitForTimeout(1200);
        const first = await textSafe(page.locator('[data-testid="inp-artifact-state"]'));
        /* 🔴 断言的是「有次数」,不是「正好第 1 次」:
           臂里多等了一会儿(要先选账号)时就已经轮到第 2 次了 ——
           把时序写死会让这一格变成在测我的 sleep,而不是测产品。
           「次数真的随轮次变」由纯函数臂 Q1d 钉。 */
        check(/第 \d+ 次检查/.test(first),
            'P9a 🔴 第一次读完就把「第 N 次检查」显示出来 —— 只写"正在准备"的话,'
            + '卡住和正常进行长得一模一样(现场那一次就是卡住)', first);
        /* 3s 一轮 × 2 轮 ⇒ 等 ~8s 足够走到第三次 */
        await page.waitForTimeout(8000);
        check(prepareHits() >= 3,
            'P9b 🔴 **真的又读了** —— 改动前这里恒等于 1(整单的缺陷就是这个数字)',
            `${prepareHits()} 次`);
        const keys = prepareKeys();
        check(keys.length >= 3 && new Set(keys).size === 1 && keys[0].length > 10,
            'P9c 🔴 三次用的是**同一把** Idempotency-Key —— 换钥匙 = 每 3 秒 claim 一条新 artifact'
            + '(而那正是幂等根要防的事)',
            `${keys.length} 次 / 去重后 ${new Set(keys).size} 把`);
        const done = await textSafe(page.locator('[data-testid="inp-artifact-block"]'));
        check(done === '' || !/第 \d+ 次检查/.test(done),
            'P9d 读到 ready 之后那一行就不显示了(素材已就绪)', done || '(不显示)');
        /* ready ⇒ 立刻问价:发布键上应当带出价钱 */
        await page.waitForTimeout(1200);
        const btn = await textSafe(page.locator('[data-testid="inp-publish"]'));
        check(/\d/.test(btn),
            'P9e 🔴 ready 之后**立刻走到算价**(工单 a1:ready 就调 publish-preview)', btn);
        await page.close();
    }
    {
        /* 终态 failed:说清楚,但**不给**重试按钮(N7d 禁手动「重新准备」) */
        const { page, prepareHits } = await open(browser, { prepareStates: ['preparing', 'failed'] });
        await pickClient(page);
        /* 🔴 要先选账号:灰理由的优先级里「先选一个要发到的账号」排在素材之前 ——
           那是**对的产品行为**(账号都没选,谈素材没意义)。
           不选账号就断言"灰理由 = 素材原话",是我的臂在跟产品的优先级较劲。 */
        await armFirstRow(page);
        await page.waitForTimeout(6000);
        const ftxt = await textSafe(page.locator('[data-testid="inp-artifact-state"]'));
        check(ftxt.includes('第 2 张图读取为空'),
            'P9f 🔴 [#196 a3] 失败行显示**服务端原话**(`failure_reason`)——'
            + '改前只读前端自己合成的 `user_message`,C 发的字段没人读', ftxt);
        const freason = await textSafe(page.locator('[data-testid="inp-disabled-reason"]'));
        check(freason.includes('第 2 张图读取为空'),
            'P9f1 发布键的灰理由**同一句**(两处读点同源,不许各说各的)', freason);
        const hitsAtFail = prepareHits();
        await page.waitForTimeout(5000);
        check(prepareHits() === hitsAtFail,
            'P9f2 🔴 终态之后**不再读** —— 终态还接着轮就是白打接口',
            `终态时 ${hitsAtFail} 次,又等 5s 还是 ${prepareHits()} 次`);
        check(await seen(page, '[data-testid="inp-artifact-retry"]') === 0,
            'P9f3 🔴 失败态**没有**「重新准备」按钮 —— N7d(#186/#188 极简裁定)禁它;'
            + '后端说 failed 可重试是另一件事,要给出口得同一笔改 N7d');
        await page.close();
    }
    {
        const { page } = await open(browser, { prepareStates: ['preparing', 'unknown'] });
        await pickClient(page);
        await clickSafe(page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]'));
        await page.waitForTimeout(5000);
        const txt = await textSafe(page.locator('[data-testid="inp-artifact-state"]'));
        check(/不会重复扣算力/.test(txt) && /第 2 张图读取为空/.test(txt),
            'P9g 🔴 `unknown` ⇒ **承诺句 + 服务端原话两句都在** ——'
            + '承诺句里是"不会重复扣你的钱",被原话挤掉的话用户就看不到了', txt);
        check(await seen(page, '[data-testid="inp-artifact-retry"]') === 0,
            'P9h 🔴 `unknown` **不给重试** —— 远端可能已经收了,重传就是可能发两次');
        await page.close();
    }
    {
        /* 收起面板 ⇒ 轮询停:收起后再等,请求数不许再涨 */
        const { page, prepareHits } = await open(browser, { prepareStates: ['preparing'] });
        await pickClient(page);
        const toggle = page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]');
        await clickSafe(toggle);
        await page.waitForTimeout(5000);
        const before = prepareHits();
        check(before >= 2, 'P9i0 分母自证:收起之前它确实在轮', `${before} 次`);
        await clickSafe(toggle);                     // 再点一次 = 收起
        await page.waitForTimeout(5000);
        check(prepareHits() === before,
            'P9i 🔴 收起面板之后**一次都不再读** —— 不清定时器的话它会在后台一直打接口',
            `收起前 ${before} 次,收起后 ${prepareHits()} 次`);
        await page.close();
    }

    {
        /*
         * 🔴 [#196 §4 裁定] `failed` 的出口不是按钮(N7d 不放宽),
         *    是**下一次展开换一把新钥匙重新 claim**;`unknown` 保持终态、同 key 不换。
         *    这一格只有"收起再展开"才看得见 —— 光看一次展开是看不出区别的。
         */
        const { page, prepareKeys } = await open(browser, { prepareStates: ['failed'] });
        await pickClient(page);
        const toggle = page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]');
        await clickSafe(toggle);
        await page.waitForTimeout(2500);
        const k1 = [...prepareKeys()];
        check(k1.length >= 1, 'P9j0 分母自证:第一次展开真的 claim 过', `${k1.length} 次`);
        await clickSafe(toggle);                      // 收起
        await page.waitForTimeout(600);
        await clickSafe(toggle);                      // 再展开
        await page.waitForTimeout(2500);
        const k2 = prepareKeys();
        check(k2.length > k1.length,
            'P9j1 🔴 `failed` 之后再展开**真的又 claim 了一次**', `${k1.length} → ${k2.length}`);
        check(k2[k2.length - 1] !== k1[k1.length - 1],
            'P9j2 🔴 而且用的是**新的一把钥匙** —— 同 key 重来只会把同一条失败记录再读一遍'
            + '(那就是一颗点了没反应的按钮,只是没有按钮)',
            `${String(k1[k1.length - 1]).slice(0, 8)}… → ${String(k2[k2.length - 1]).slice(0, 8)}…`);
        /* 钥匙仍是合法 UUID 形状:后端 normalize_uuid 对别的形状直接 400 */
        check(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
            .test(String(k2[k2.length - 1])),
            'P9j3 🔴 新钥匙仍是合法 UUID —— 在 UUID 后面接 `-retry2` 会被后端 400',
            String(k2[k2.length - 1]));
        await page.close();
    }
    {
        /* unknown:收起再展开**不许**换钥匙、不许新 claim */
        const { page, prepareKeys } = await open(browser, { prepareStates: ['unknown'] });
        await pickClient(page);
        const toggle = page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]');
        await clickSafe(toggle);
        await page.waitForTimeout(2500);
        const k1 = [...prepareKeys()];
        await clickSafe(toggle);
        await page.waitForTimeout(600);
        await clickSafe(toggle);
        await page.waitForTimeout(2500);
        const k2 = prepareKeys();
        check(k1.length >= 1 && k2.length > k1.length
            && k2[k2.length - 1] === k1[k1.length - 1],
            'P9k 🔴 `unknown` 再展开用的是**同一把**钥匙(回放,不新建)——'
            + '换 key = 再 claim 一条 = 远端可能收两次(后端把这写成硬约束)',
            `${k1.length} → ${k2.length} 次,末位钥匙 ${k2[k2.length - 1] === k1[k1.length - 1] ? '相同' : '不同'}`);
        await page.close();
    }

    // ══ P8 #192 账号选择:服务端过滤 + 搜索 + 文案 + 错误合同 actions ═══
    section('P8 #192 账号选择与错误出口');
    {
        /*
         * 🔴 现场(0913a):面板拉的是媒介盒子**供应商目录**(生产 21,850 条),
         *    第一页 50 条 can_tuwen 全 0 ⇒ 面板永远说「还没有能发图文的账号」。
         *    这一格钉:请求**真的带** can_tuwen=1 与分页。数的是真实发出的请求。
         */
        const { page, reqs } = await open(browser);
        await pickClient(page);
        await clickSafe(page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]'));
        await page.waitForTimeout(900);
        const acctReqs = reqs.filter((r) => /meijiehezi\/short-video/.test(r.path));
        check(acctReqs.length >= 1, 'P8a0 分母自证:真的发了账号请求', `${acctReqs.length} 次`);
        const withFilter = acctReqs.filter((r) => /can_tuwen=1/.test(r.query || ''));
        check(withFilter.length === acctReqs.length && acctReqs.length > 0,
            'P8a 🔴 账号请求**每一次都带 can_tuwen=1** —— 不带就是在两万条目录第一页里'
            + '找能发图文的号,永远找不到(现场那一格)',
            `${withFilter.length}/${acctReqs.length}`);
        check(acctReqs.every((r) => /limit=50/.test(r.query || '')),
            'P8b 带分页参数(目录两万条,不翻页只看得见头 50 个)');

        // 搜索:输入并回车 ⇒ 请求带 search=
        const before = acctReqs.length;
        await page.locator('[data-testid="inp-account-search"]').fill('抖音').catch(() => { });
        await clickSafe(page.locator('[data-testid="inp-account-search-btn"]'));
        await page.waitForTimeout(900);
        const after = reqs.filter((r) => /meijiehezi\/short-video/.test(r.path));
        const searched = after.slice(before).filter((r) => /search=/.test(r.query || ''));
        check(searched.length >= 1,
            'P8c 🔴 搜索**真的发到服务端**(带 search=)—— 只在本地过滤那 50 条等于没搜',
            `新增 ${after.length - before} 次请求,其中带 search 的 ${searched.length} 次`);
        await page.close();
    }
    {
        /* 无结果 ≠ 空态:两句话必须不同(a2) */
        const { page } = await open(browser, { emptyAccounts: true });
        await pickClient(page);
        await clickSafe(page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]'));
        await page.waitForTimeout(900);
        const emptyTxt = await textSafe(page.locator('[data-testid="inp-no-accounts"]'));
        check(/媒体库里没找到能发图文的账号/.test(emptyTxt),
            'P8d 🔴 空态文案改了 —— 原话「这个客户名下还没有能发图文的账号,先去媒体盒子里绑定一个」'
            + '把媒体库说成"客户名下的账号",用户照着去绑定,绑完回来还是找不到', emptyTxt);
        await page.locator('[data-testid="inp-account-search"]').fill('不存在的媒体').catch(() => { });
        await clickSafe(page.locator('[data-testid="inp-account-search-btn"]'));
        await page.waitForTimeout(900);
        check(await seen(page, '[data-testid="inp-search-empty"]') === 1,
            'P8e 🔴 「搜了没结果」与「一个都没有」**分开两句**(a2);并给「清空搜索」出口');
        await page.close();
    }
    {
        /*
         * 🔴 403 现场:面板只显示了「你还没有这个操作权限」一句,
         *    而后端合同里**已经给了两条出路**(actions),一条都没渲染。
         *    禁猜清单第 4 条:断头必须有出口。
         */
        const { page } = await open(browser, { publishForbidden: true });
        await pickClient(page);
        await armFirstRow(page);
        await clickSafe(page.locator('[data-testid="inp-publish"]'));
        await page.waitForTimeout(1200);
        check(await seen(page, '[data-testid="inp-submit-error"]') === 1,
            'P8f0 分母自证:错误真的渲染了');
        check(await seen(page, '[data-testid="inp-error-actions"]') === 1,
            'P8f 🔴 403 时把合同里的 actions 渲染出来 —— 改前一条都没有,'
            + '用户只看到"你没权限",拿不到"交给团队负责人 / 联系管理员"');
        /* 🔴 `asChild` 会把 props **合并到子元素上**,所以带 testid 的就是那个 <a> 本身,
           不是它的子节点。第一版写成 `… a` 找子节点,取到 null —— 选择器错,不是代码错。 */
        const navHref = await page.locator('[data-testid="inp-error-action-handoff"]')
            .getAttribute('href').catch(() => '');
        check(navHref === '/team',
            'P8g 🔴 「交给团队负责人」指向**真实存在**的路由(App.tsx 有 /team)——'
            + '编一个不存在的路径 = 点了掉进 404,比不给按钮更糟', String(navHref));
        const unknownTxt = await seen(page, '[data-testid="inp-error-action-text-unknown_thing"]');
        check(unknownTxt === 1,
            'P8h 🔴 认不出去处的 action **降级成文字**,不给一颗点了没反应的按钮');
        await page.close();
    }

    // ══ 截图:#192 三态 × 深浅(WO §2)═══════════════════════════════
    if (process.argv.includes('--shots')) {
        section('截图(有结果 / 无结果 / 403 × 深浅)');
        const OUT = join(ROOT, '..', '..', 'a192-shots');
        mkdirSync(OUT, { recursive: true });
        const CASES = [
            ['results', {}, '有结果'],
            ['empty', { emptyAccounts: true }, '无结果'],
            ['forbidden', { publishForbidden: true }, '403 无权限'],
            /* [#196] 素材准备三态:准备中(带第 N 次检查)/ 已就绪(含预览价)/ 失败原话。
               🔴 「准备中」那张要 `prepareStates: ['preparing']` —— 恒 preparing 才截得到
               进度行;桩一上来就 ready 的话这一张永远拍不到。 */
            ['prep_waiting', { prepareStates: ['preparing'] }, '素材准备中(带检查次数)'],
            ['prep_ready', { prepareStates: ['ready'] }, '素材已就绪(含预览价)'],
            ['prep_failed', { prepareStates: ['failed'] }, '素材准备失败'],
        ];
        for (const theme of ['dark', 'light']) {
            for (const [name, plan, label] of CASES) {
                const { page } = await open(browser, { ...plan, theme });
                await pickClient(page);
                if (name === 'forbidden') {
                    await armFirstRow(page);
                    await clickSafe(page.locator('[data-testid="inp-publish"]'));
                    await page.waitForTimeout(1200);
                } else {
                    await clickSafe(page.locator('[data-post-id="501"] [data-testid="pub-imagenote-publish-toggle"]'));
                    await page.waitForTimeout(1000);
                }
                /*
                 * 🔴 出图前**量**一次主题:标称与实测不符就不出图。
                 *    把浅色命名成 dark_ 发出去,没有任何东西会报警
                 *    —— 这道闸在 #188 / #189 各拦过一次。
                 *    探针用不透明的 `bg-background`,别用半透明元素(读数会被底色拉平)。
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
                const target = page.locator('[data-testid="inp-inline"]').first();
                const n = await target.count().catch(() => 0);
                const f = join(OUT, `publish_${name}_${theme}.png`);
                if (n > 0) {
                    await target.evaluate((e) => e.scrollIntoView({ block: 'center' })).catch(() => { });
                    await page.waitForTimeout(250);
                    await target.screenshot({ path: f }).catch(() => { });
                } else {
                    await page.screenshot({ path: f }).catch(() => { });
                }
                console.log(`  ${theme}/${label}: 亮度 ${Math.round(lum)} → ${f}`);
                await page.close();
            }
        }
    }

} finally {
    await browser.close();
    server.close();
}

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
