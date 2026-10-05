#!/usr/bin/env node
/**
 * 判据 · WO_243 甲 —— admin 进「服务商工作台 → 进货」时,那句话**由后端出**。
 *
 * Owner 2026-09-19 第 7 件「明示」。后端(WO_241 甲)返
 * `409 {"code":"PLATFORM_DIRECT_NO_PROCUREMENT","message":"平台直营账号不需要进货"}`,
 * 页面用**后端的 message** 明示,隐藏档位与下单;其它错误码走现有处理。
 *
 * 🔴 为什么必须是真渲染臂:本单要治的是「**屏幕上那句话从哪来**」。
 *    读源码只能证"代码里引用了 message",证不了"渲染出来的就是它"——
 *    而前端原本硬编码的那一段话**还在同一张卡里**,两者在源码上离得很近。
 *
 * 🔴 两个**只差一个词**的近名码必须各配一格反向对照:
 *      `PLATFORM_DIRECT_NOT_READY`      = 平台直营**未就绪**(故障)
 *      `PLATFORM_DIRECT_NO_PROCUREMENT` = 这个账号**本来就不进货**(正常态)
 *    两者同为 409。按状态码判、或按前缀判,都会把故障渲染成一句安抚话。
 *    工单没要求这两格 —— 是这两个名字的形状要求的。
 *
 * 三态退出:0 全绿 · 1 有判据红 · 3 门自己没跑成。
 */
import { mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

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
    console.log(`\n3 门没跑成:${what} —— ${String((err && err.message) || err).slice(0, 300)}`);
    console.log('   (退出码 3 = 本门这次**没有测过任何东西**,不要当成通过)');
    process.exit(3);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { cannotRun('esbuild / playwright 取不到', err); }

/* 🔴 `.cache` 是构建产物,干净的 `npm ci` 树里不存在;建目录这一步也要包进三态。 */
let outDir;
try {
    const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
    mkdirSync(CACHE_ROOT, { recursive: true });
    outDir = mkdtempSync(join(CACHE_ROOT, 'a243-'));
} catch (err) { cannotRun('临时目录建不出来', err); }
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
import { PricingProvider } from ${q('src/context/PricingContext.tsx')};
import { ClientProvider } from ${q('src/context/ClientContext.tsx')};
import InventoryCenter from ${q('src/pages/Agent/InventoryCenter.tsx')};
/* [复审 ①] 第二处钱面:客户买算力页,同一条 SSOT_DISABLED 到 legacy 的回退。
   注意:本段在反引号模板串里,注释中**不许出现反引号**——会把模板提前闭合。 */
import BuyCredit from ${q('src/pages/Customer/BuyCredit.tsx')};

(globalThis as any).__mount = function (which: string) {
    const Page = which === 'buycredit' ? BuyCredit : InventoryCenter;
    const entry = which === 'buycredit' ? '/customer/recharge' : '/agent/inventory';
    createRoot(document.getElementById('root')!).render(
        <MemoryRouter initialEntries={[entry]}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <PricingProvider><ClientProvider>
              <Page />
            </ClientProvider></PricingProvider>
          </WalletProvider></UserModeProvider></OrganizationProvider></AuthProvider>
        </MemoryRouter>,
    );
};
`, 'utf8');

try {
    await esbuild.build({
        entryPoints: [join(outDir, 'entry.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        loader: { '.tsx': 'tsx', '.ts': 'ts', '.svg': 'dataurl', '.png': 'dataurl', '.md': 'text', '.json': 'json' },
        logLevel: 'silent',
    });
} catch (err) { cannotRun('打包失败', err); }

writeFileSync(join(outDir, 'index.html'),
    '<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>a243</title></head>'
    + '<body><div id="root"></div><script src="./bundle.js"></script></body></html>', 'utf8');

const server = createServer((req, res) => {
    const name = (req.url || '/').split('?')[0] === '/' ? '/index.html' : (req.url || '').split('?')[0];
    try {
        const body = readFileSync(join(outDir, name.slice(1)));
        res.writeHead(200, { 'Content-Type': name.endsWith('.js') ? 'text/javascript' : 'text/html' });
        res.end(body);
    } catch { res.writeHead(404); res.end(); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

/* ── 夹具 ─────────────────────────────────────────────────────────────── */

/** 后端那句话。🔴 判据不认硬编码的旧文案,只认**这一份**被渲染出来。 */
/*
 * 🔴 **故意带一个前端不可能写死的记号。** 不带记号的话,这句话与前端原来硬编码的
 *    那一段**字面相同** —— 有人再硬编码一次同样的话,A1 照样绿,
 *    而本单要治的正是"第二份"。带上记号之后,A1 才真的在证
 *    「屏幕上这句是**从响应里来的**」,而不是"内容看着对"。
 */
const BACKEND_MSG = '平台直营账号不需要进货 · QA243-NONCE';
/** 前端原本硬编码的那一段(已删)。它要是又出现,说明"第二份"回来了。 */
const OLD_HARDCODED = '平台仓库不向自身进货';

let ROUND = { admin: true, catalog: { kind: '409', code: 'PLATFORM_DIRECT_NO_PROCUREMENT', message: BACKEND_MSG } };

/**
 * 🔴 [WO_254] 每轮记下**页面真打了哪些端点**。
 *
 * 0913N 上线后 N1 屏幕红,复盘写的是「A 的夹具喂给了没人打的端点」——
 * 那句对本门**不成立**(本门从第一版起就喂 catalog,见 `ROUND` 初值)。
 * 真正的洞是另一个,更难看见:
 *
 * **本门证的是一个条件句**——「catalog 若回该码,屏幕就出那句话」。
 * 它当时是对的,现在也是对的。而生产上那个**前件从来没成立过**:
 * 后端只在 `purchase-options` 出该码,catalog 回的是 `PRICE_CONFIGURATION_UNAVAILABLE`。
 * 🔴 **夹具把一个没有生产方的契约当成了既定事实**,而消费方这一侧的判据
 *    结构上看不见「谁来保证前件」。
 *
 * ⇒ 两件事一起补:
 *   · 这里的 `HITS` —— 至少钉住「页面确实在打 catalog」(前件的**载体**还在);
 *   · 下面 H1 —— 去后端源码里核「catalog 这条路真的会发这个码」(前件的**生产方**在)。
 *     前者防页面改道,后者防契约只活在夹具里。
 */
const HITS = {};

const CATALOG_OK = {
    catalog_version: 'v-qa-243',
    items: [
        { product_code: 'QA_TIER_A', display_name: 'QA 档位 A', cash_price_cents: 100000,
            paid_inventory_points: 1000, bonus_inventory_points: 100, total_inventory_points: 1100 },
        { product_code: 'QA_TIER_B', display_name: 'QA 档位 B', cash_price_cents: 200000,
            paid_inventory_points: 2100, bonus_inventory_points: 300, total_inventory_points: 2400 },
    ],
    tier_progress: null,
};

/* [复审 ①] 旧链的两个端点,各给一个**只可能来自旧链**的名字 ——
   它出现在屏幕上,就等于"确实走进了 legacy 回退"。 */
const LEGACY_AGENT_NAME = 'QA243-LEGACY-档位';
const LEGACY_SKU_NAME = 'QA243-LEGACY-算力包';

function catalogResponse() {
    const c = ROUND.catalog;
    if (c.kind === '200') return { status: 200, body: CATALOG_OK };
    if (c.kind === '500') return { status: 500, body: { detail: '内部错误' } };
    /* 🔴 503 也走**结构化** detail —— 与生产同形:归一化会把它压成字符串并挪到
          `detail_contract`,这正是本单修好的那一步。 */
    const status = c.kind === '503' ? 503 : 409;
    return { status, body: { detail: { code: c.code, message: c.message } } };
}

/* ── 浏览器 ───────────────────────────────────────────────────────────── */

let browser, page;
const consoleErrors = [];
try {
    browser = await playwright.chromium.launch();
    page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 160)); });
    page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e.message).slice(0, 160)));
    await page.addInitScript(() => { localStorage.setItem('omnirank_token', 'qa-token'); });
    await page.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        HITS[path] = (HITS[path] || 0) + 1;
        const json = (status, body) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
        if (path.includes('/auth/me')) {
            /* 🔴 形状不能猜:`AuthContext:660` 要 `me.data.success && me.data.user`,
                  裸的 user 对象会让 `user` 恒为 null —— 于是 is_admin 永远是假、
                  `reload()` 也不跑,页面停在"暂无可选进货档位",
                  而那与"档位被正确隐藏了"**读数完全同形**。第一版就栽在这儿。 */
            return json(200, { success: true, user: {
                id: 9243, username: 'qa-user', is_admin: ROUND.admin,
                /* 🔴 买算力页按 `agent_level >= 1` 整块换成**服务商视图**
                      (`BuyCredit.tsx:183`/`:615`),客户那半边根本不渲染。
                      本轮测的是客户面,所以身份按页面给。 */
                agent_level: ROUND.page === 'buycredit' ? 0 : 1,
                role: ROUND.admin ? 'admin' : 'agent', permissions: [], modules: [],
            } });
        }
        if (path === '/api/pricing/procurement/catalog' || path === '/api/pricing/retail/catalog') {
            const r = catalogResponse();
            return json(r.status, r.body);
        }
        if (path === '/api/wallet' || path.endsWith('/api/wallet')) {
            return json(200, { success: true, data: {
                paid_points: 1000, commission_points: 0, bonus_points: 0, frozen_points: 0, total_recharged: 1000,
                customer_credit: { tool_credit_points: 0, publish_credit_points: 0, bonus_credit_points: 0,
                    total_purchased_points: 0, total_consumed_points: 0 },
            } });
        }
        if (path === '/api/agent/inventory/purchase-options') {
            return json(200, { catalog_version: 'legacy-v1', options: [
                /* 🔴 字段名照 PurchaseCard 真读的那几个(amount_cents/label/base_points…),
                      不是我按直觉起的名 —— 少一个就渲染崩,而崩了之后屏幕全空,
                      下面每一条否定断言都会空过。 */
                { option_id: 'legacy-1', label: LEGACY_AGENT_NAME, amount_cents: 100000,
                    base_points: 1000, bonus_points: 0, total_points: 1000,
                    tier_at_order: null, tier_bonus_rate_bps: 0,
                    crosses_tier_threshold: false, reward_description: '' },
            ] });
        }
        if (path === '/api/customer/recharge/skus') {
            return json(200, { items: [
                /* 🔴 category 必须落在 `CATEGORY_LABELS`(`BuyCredit.tsx:117` 只认
                      credit_pack / addon_pack)—— 不在名单里的分组**根本不渲染**,
                      而页面其余部分一切正常,读数与"没进 legacy"完全同形。 */
                /* 🔴 字段名照**真消费点**给(`BuyCredit.tsx:733-790` 读的是
                      retail_cents / points_granted / scene / subtitle / promo_text /
                      usage_examples),不是我按直觉起的 price_cents / points。
                      少一个就渲染崩,而崩了之后屏幕全空 —— G3a 那一格就是为此设的。 */
                { retail_sku_id: 'legacy-sku-1', retail_sku_version: 1, sku_key: 'legacy1',
                    category: 'credit_pack', display_name: LEGACY_SKU_NAME,
                    subtitle: null, promo_text: null, scene: null,
                    retail_cents: 10000, points_granted: 100, bonus_points: 0,
                    usage_examples: [] },
            ] });
        }
        if (path.includes('/pricing/flags')) return json(200, { PRICING_DUAL_SSOT_ENABLED: true });
        return json(200, { success: true, items: [], options: [], data: [], transactions: [] });
    });
} catch (err) { cannotRun('chromium 起不来', err); }

async function runRound(round) {
    ROUND = round;
    consoleErrors.length = 0;
    for (const k of Object.keys(HITS)) delete HITS[k];
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate((w) => globalThis.__mount(w), round.page || 'inventory');
    await page.waitForTimeout(2200);
    const view = await page.evaluate(() => {
        const txt = (document.getElementById('root') || document.body).innerText.replace(/\s+/g, ' ');
        const vis = (sel) => [...document.querySelectorAll(sel)].filter((el) => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        });
        return {
            screen: txt,
            notice: vis('[data-testid="platform-direct-notice"]').map((e) => e.textContent.trim()),
            pending: vis('[data-testid="platform-direct-notice-pending"]').length,
            tierCards: txt.includes('QA 档位 A') || txt.includes('QA 档位 B'),
        };
    });
    /* 端点命中数与页面读数一起回,免得调用方还要自己去翻全局。 */
    return { ...view, hits: { ...HITS } };
}

console.log('=== WO_243 甲 · admin 进货页明示(真浏览器)===\n');


/*
 * 🔴 **分母自证必须在最前面。** 本门大半是**否定断言**(不出现那句话 / 没有档位),
 *    而页面整个没渲染出来时它们**全部空过** —— 第一版就是这样:
 *    A2/A3/C2/F2 四格"通过",实际是屏幕上什么都没有。
 *    先证这一页真的在,后面每一句"没有 X"才有意义。
 */
{
    const probe = await runRound({ admin: true, catalog: { kind: '409', code: 'PLATFORM_DIRECT_NO_PROCUREMENT', message: BACKEND_MSG } });
    ok(probe.screen.length > 200 && (probe.screen.includes('流水') || probe.screen.includes('线下划拨')),
        'Z0 分母自证:库存中心**真的渲染出来了** —— 没渲染的话下面每一条"没有 X"都是空过',
        `屏幕 ${probe.screen.length} 字 · ${probe.screen.slice(0, 80)}`);
    if (process.argv.includes('--debug')) console.log('  [debug] ' + probe.screen.slice(0, 1600));
}
/* ── A 正臂:409 专码 ─────────────────────────────────────────────────── */
const A = await runRound({ admin: true, catalog: { kind: '409', code: 'PLATFORM_DIRECT_NO_PROCUREMENT', message: BACKEND_MSG } });
ok(A.notice.length === 1 && A.notice[0] === BACKEND_MSG,
    'A1 🔴 屏幕上那句话**逐字等于后端 message** —— 不是前端另写的一句',
    JSON.stringify(A.notice));
ok(!A.screen.includes(OLD_HARDCODED),
    'A2 🔴 前端原来硬编码的那一段**不再出现** —— 留着就是"第二份",两份迟早分家而各自看都对',
    `屏幕上没有「${OLD_HARDCODED}」`);
ok(!A.tierCards, 'A3 档位卡一张都不显示', A.tierCards ? '🔴 还看得见档位' : '零张');

/* ── [WO_254] A5/A6:把「前件的载体」和「兜底态」也钉住 ───────────────── */
const CATALOG_PATH = '/api/pricing/procurement/catalog';
ok((A.hits[CATALOG_PATH] || 0) >= 1,
    'A5 [WO_254] 页面**确实调用了** catalog —— 前件的载体还在',
    `catalog ×${A.hits[CATALOG_PATH] || 0} · 本轮全部端点:${Object.keys(A.hits).join(', ') || '(一个都没打)'}`);
/*
 * 🔴 A6 是 N1 屏幕上真实看到的那一幕:后端已经把码发回来了,而屏上是
 *    「尚未取到平台口径说明 · 下单入口已按兜底规则关闭」。
 *    A1 只说「那句话在」,说不出「兜底句同时也在」——**两句同屏**是可能的,
 *    而那种屏幕对 admin 来说仍然是坏的。否定这一句要单独一格。
 */
ok(A.pending === 0,
    'A6 [WO_254] 反臂:后端已回该码时**不许**再显示兜底句(N1 屏幕上就是它)',
    A.pending === 0 ? '兜底句零个' : `🔴 兜底句还在 ×${A.pending}`);
/*
 * 🔴 「console 0」要区分两种东西:
 *   · **浏览器自己**对非 2xx 响应打的 `Failed to load resource: ... 409` ——
 *     这是 HTTP 语义的一部分,本轮**就是故意**让它 409 的,算它头上没有意义;
 *   · **应用**报的错 —— 那才是本单要求的 0。
 * 🔴 但**每一个排除项都是一处自陈的盲区**,所以:
 *   ① 排除串与本轮真实 mock 的状态码绑定(不是笼统地滤掉所有 resource 行);
 *   ② 下面 A4b 打一发**正样本**,证明过滤之后这把还抓得住真报错。
 */
const appErrors = (list, mockedStatus) => list.filter(
    (t) => !new RegExp(`Failed to load resource.*status of ${mockedStatus}`).test(t));
{
    const app = appErrors(consoleErrors, 409);
    ok(app.length === 0, 'A4 console 无**应用**报错(浏览器对故意 409 打的资源日志不算)',
        app.join(' | ') || `0 条 · 已排除 ${consoleErrors.length - app.length} 条 409 资源日志`);
    /* 正样本:注一条真报错,过滤后必须仍然看得见 —— 看不见就说明这把已经瞎了。 */
    await page.evaluate(() => console.error('QA-probe-app-error'));
    await page.waitForTimeout(200);
    ok(appErrors(consoleErrors, 409).some((t) => t.includes('QA-probe-app-error')),
        'A4b 🔴 过滤器正样本:注一条真应用报错,它**必须**还被算进来 —— '
        + '不然 A4 的"0 条"只证明我把所有东西都滤掉了',
        appErrors(consoleErrors, 409).join(' | ') || '🔴 一条都没抓到');
}

/* ── B 反向对照:200 → 档位照旧 ───────────────────────────────────────── */
const B = await runRound({ admin: false, catalog: { kind: '200' } });
ok(B.tierCards, 'B1 反向对照:200 时档位**照旧显示** —— 新分支没有把成功路径一起吞掉',
    B.tierCards ? '看得见 QA 档位' : '🔴 档位不见了');
ok(B.notice.length === 0 && !B.screen.includes(BACKEND_MSG),
    'B2 200 时不出现那句话', `notice ${B.notice.length} 处`);

/* ── C 反向对照:500 不许渲染成那句 ───────────────────────────────────── */
const C = await runRound({ admin: false, catalog: { kind: '500' } });
ok(C.notice.length === 0 && !C.screen.includes(BACKEND_MSG),
    'C1 🔴 500 **不许**被渲染成那句安抚话 —— 那会把一次故障说成"你不需要进货"',
    `notice ${C.notice.length} 处`);
ok(!C.tierCards, 'C2 500 时也没有档位可点', C.tierCards ? '🔴 还看得见档位' : '零张');

/* ── D/E 近名码:同为 409,只差一个词 ─────────────────────────────────── */
const D = await runRound({ admin: false, catalog: { kind: '409', code: 'NO_PUBLISHED_PROCUREMENT', message: '平台进货目录未发布' } });
ok(D.notice.length === 0,
    'D1 🔴 同为 409 的 `NO_PUBLISHED_PROCUREMENT`(目录没发布 = 运维故障)**不走那一支** —— '
    + '按状态码判会把故障渲染成安抚话,故障就此没人看见',
    `notice ${D.notice.length} 处`);
const E = await runRound({ admin: false, catalog: { kind: '409', code: 'PLATFORM_DIRECT_NOT_READY', message: '平台直营未就绪' } });
ok(E.notice.length === 0,
    'E1 🔴 名字只差一个词的 `PLATFORM_DIRECT_NOT_READY`(未就绪 = 故障)**不走那一支** —— '
    + '按前缀匹配的话这一格会绿,而它正是最容易写错的那一个',
    `notice ${E.notice.length} 处`);

/* ── F 兜底:admin + 后端还没出声 ⇒ 明说"没拿到",且仍不给下单 ─────────── */
const F = await runRound({ admin: true, catalog: { kind: '200' } });
ok(F.pending === 1,
    'F1 🔴 admin 而后端**还没出声**(WO_241 甲 未上线)⇒ 明说"尚未取到平台口径",'
    + '不拿旧文案顶上 ——「后端没说」和「后端说了不用进货」必须分得开',
    `pending ${F.pending} 处`);
/*
 * 🔴 [注毒 M6 打出来的] F1 只证了"那个盒子在",证不了"它说了什么" ——
 *    M6 把它的文案换成「平台仓库不向自身进货。」,F1 照样绿。
 *    真正的不变式不是"有个待取态",而是:
 *    **后端没说的那句话,屏幕上一个字都不许出现。**
 *    (这一条比钉死某句文案强:换个说法照样抓得住,因为抓的是"断言了业务事实"。)
 */
const ASSERTS_BUSINESS_FACT = ['不需要进货', '不向自身进货', '无需进货'];
const claimed = ASSERTS_BUSINESS_FACT.filter((w) => F.screen.includes(w));
ok(claimed.length === 0,
    'F1b 🔴 后端**还没出声**时,屏幕上不许出现任何"这个账号不用进货"式的断言 —— '
    + '那句话的权威在后端;前端替它说,就是又造了一份会过期的口径',
    claimed.join(' / ') || '一个都没有');

ok(!F.tierCards,
    'F2 🔴 同一局面下**仍然不给下单** —— 后端那笔上线前,这个端点对 admin 会正常返一份真目录'
    + '(`_require_service_provider` 对 is_admin 直接放行),只认后端的话就把档位摆到平台账号面前了',
    F.tierCards ? '🔴 看得见档位' : '零张');

/* ── G 反向对照:被本单"复活"的两条钱面回退 ─────────────────────────────
 * 🔴 这两条 `SSOT_DISABLED → legacy` 分支在 `toPricingError` 修好之前**恒不可达**
 *    (码永远是 UNKNOWN)。修好之后它们第一次变得可达 ——
 *    **第一次可达的分支,是最没人验过的那一种**。
 *    生产 flag 为 True 所以不触发,但"不会触发"不是"不会错":
 *    哪天关掉 flag,这是钱面上第一眼看到的东西。
 * 🔴 正臂证"进得去",反臂证"别的码进不去"。只有正臂的话,
 *    把条件写成 `status === 503`(不看码)也会绿。
 */
const SSOT_OFF = { kind: '503', code: 'SSOT_DISABLED', message: '双价目表未启用' };
const OTHER_503 = { kind: '503', code: 'PRICE_CONFIGURATION_UNAVAILABLE', message: '定价配置不可用' };

const G1 = await runRound({ admin: false, page: 'inventory', catalog: SSOT_OFF });
ok(G1.screen.includes(LEGACY_AGENT_NAME),
    'G1 🔴 正臂:结构化 `SSOT_DISABLED` ⇒ 进货页**真的进了 legacy 回退** —— '
    + '这条分支在码恒为 UNKNOWN 的年代够不着,本单是它第一次可达',
    G1.screen.includes(LEGACY_AGENT_NAME) ? '看得见旧链档位' : ('🔴 没看见 · 屏幕: ' + G1.screen.slice(0, 420)));
const G2 = await runRound({ admin: false, page: 'inventory', catalog: OTHER_503 });
ok(!G2.screen.includes(LEGACY_AGENT_NAME),
    'G2 反向对照:同为 503 的**别的码**不许进 legacy —— 只按状态码判的话这一格会红',
    G2.screen.includes(LEGACY_AGENT_NAME) ? '🔴 也进了 legacy' : '没进');

const G3 = await runRound({ admin: false, page: 'buycredit', catalog: SSOT_OFF });
ok(G3.screen.length > 100,
    'G3a 分母自证:客户买算力页**真的渲染出来了** —— 没渲染的话 G3/G4 都是空过',
    `屏幕 ${G3.screen.length} 字`);
ok(G3.screen.includes(LEGACY_SKU_NAME),
    'G3 🔴 正臂:同一条回退在**客户买算力页**也成立(第二处钱面)',
    G3.screen.includes(LEGACY_SKU_NAME) ? '看得见旧链算力包' : ('🔴 没看见 · 屏幕: ' + G3.screen.slice(0, 460)));
const G4 = await runRound({ admin: false, page: 'buycredit', catalog: OTHER_503 });
ok(!G4.screen.includes(LEGACY_SKU_NAME),
    'G4 反向对照:客户页别的 503 码不许进 legacy',
    G4.screen.includes(LEGACY_SKU_NAME) ? '🔴 也进了 legacy' : '没进');

/* ── [WO_254] H:前件的**生产方**在不在 ───────────────────────────────── */
/*
 * 🔴 本门其余每一格都是条件句「catalog 若回该码,屏幕就…」。
 *    条件句可以全绿,而前件在生产上**从未成立** —— N1 就是这么红的:
 *    后端把该码放在 `purchase-options`(页面零调用),catalog 回的是别的码。
 *    消费方这一侧的判据结构上看不见这件事,所以必须**跨到生产方那一侧**看一眼。
 *
 * 🔴 「某串出现过」会被注释满足(本仓惯犯),所以先剥掉 `#` 注释再找。
 *    剥完之后还要有**正控**:一个已知就在那个文件里的码必须找得到 ——
 *    否则「没找到」与「我的读法坏了 / 剥过头了」读数完全同形。
 *
 * 🔴🔴 **H1 能力边界,先自陈,别把它当证明**:
 *    它只证「这个码出现在该模块的非注释源码里」,**证不了**
 *    「platform_direct 走 catalog 时真的会发它」——**存在 ≠ 可达**。
 *    实测过:把这个码作为一句裸表达式插进该文件,H1 一样转绿。
 *    ⇒ H1 是**耦合绊线**(后端把码挪走 / 改名 / 撤回时,消费方这侧当场有人喊),
 *      不是契约证明。真证明在 C 那侧的后端判据(admin 打 catalog ⇒ 409 该码,
 *      两端点 message 逐字相等,helper 只接一处必红),由 WO_254 派给 C。
 *      两件成对才算接好线:**一侧证行为,一侧防脱钩**。
 */
let notRun = 0;
{
    const bePath = (...p) => join(ROOT, '..', ...p);
    const readBe = (...p) => { try { return readFileSync(bePath(...p), 'utf8'); } catch { return null; } };
    /** 只剥 `#` 注释 —— docstring 不剥,所以下面的锚都带引号/括号,躲开散文里的同名词。 */
    const bare = (src) => src.split('\n')
        .filter((l) => !l.trim().startsWith('#'))
        .map((l) => l.replace(/#.*$/, ''))
        .join('\n');

    const catalogSrc = readBe('api', 'pricing_ssot_api.py');
    if (catalogSrc === null) {
        notRun += 1;
        console.log('  SKIP H 后端源码读不到(不在全仓检出里?)—— 这一格**没跑成**,不是通过');
    } else {
        const catalogBare = bare(catalogSrc);
        /* 正控在最前:找不到这个已知就在该文件里的码 ⇒ 是我的读法/剥注释坏了,不是后端缺东西。 */
        const control = catalogBare.includes('PRICE_CONFIGURATION_UNAVAILABLE');
        if (!control) {
            notRun += 1;
            console.log('  SKIP H 正控失败:连 `PRICE_CONFIGURATION_UNAVAILABLE` 都找不到 ⇒ '
                + '是我的读法坏了,不是后端缺码。这一格**没跑成**');
        } else {
            /*
             * 🔴 H1 第一版钉的是「`PLATFORM_DIRECT_NO_PROCUREMENT` 这个字面量出现在
             *    `api/pricing_ssot_api.py` 里」—— 那是**我猜 C 会怎么写**。
             *    C 实际按「码与文案只放一处」把字面量收进
             *    `services/platform_direct_procurement.py`,catalog 侧只 import + raise。
             *    ⇒ 合成后这一格**照样红**,而接线其实是好的:
             *      本仓 `a-lock-aimed-at-a-guessed-future-shape-is-blind` 的同形。
             *    改成按**行为形状**认:谁 raise、谁定义,两头各钉一格。
             */
            const raises = /raise\s+platform_direct_no_procurement\s*\(/.test(catalogBare);
            ok(raises,
                'H1 [WO_254] 🔴 catalog 那条路**接上了**这句明示(`raise platform_direct_no_procurement(`)—— '
                + '本门其余每一格的**前件**由它保证',
                raises ? '接线在' : '🔴 catalog 侧没有这个 raise ⇒ 屏幕永远走不到那一支(上面那些格照样全绿)');
            ok(control, 'H2 正控:剥注释后仍能找到已知就在该文件里的码(证明 H1 的「没找到」是真没有)',
                '找得到 PRICE_CONFIGURATION_UNAVAILABLE');

            const helperSrc = readBe('services', 'platform_direct_procurement.py');
            if (helperSrc === null) {
                ok(false, 'H3 码与文案的**唯一出处**模块在',
                    '🔴 `services/platform_direct_procurement.py` 不存在 —— 后端在(H2 过了)而这个模块没有,'
                    + '是接线断了,不是"跑不成"');
            } else {
                const helperBare = bare(helperSrc);
                ok(/PLATFORM_DIRECT_NO_PROCUREMENT_CODE\s*=\s*["']PLATFORM_DIRECT_NO_PROCUREMENT["']/.test(helperBare),
                    'H3 码的字面量定义在那个唯一出处里(前端按这个 code 分支渲染,改名必须两边一起改)',
                    '定义在');
                /*
                 * 🔴 H4 是本门这一侧**独有**的那一格:页面真正打的那个端点,
                 *    必须在后端登记的「会交付这个 code 的端点」名单里。
                 *    0913N 的病就是这两张表错位 —— 出声点与被消费点不是同一个。
                 */
                const registered = new RegExp(`["']${CATALOG_PATH}["']\\s*,`).test(helperBare);
                ok(registered,
                    `H4 [WO_254] 🔴 页面真正打的端点 \`${CATALOG_PATH}\` 在后端登记的交付名单里 —— `
                    + '「出声点」与「被消费点」不许再错位',
                    registered ? '已登记' : '🔴 不在名单里');
            }
        }
    }
}
/*
 * 🔴🔴 **本门这几格证的是「接线在」,不是「可达」** —— 边界写在这里,免得被当成后者:
 *    把 catalog 那一行改成 `if False and is_platform_direct_actor(user):`,
 *    H1 **照样绿**(raise 还在源码里),而屏幕又坏了。
 *    那一发由 C 侧的行为判据咬(admin 打 catalog ⇒ 409 该码),本门不重复证,
 *    也**不冒充**能证。实测过这一发在本门存活,不是没试过就这么写。
 */

console.log('');
if (notRun > 0) {
    console.log(`3 有 ${notRun} 格**没跑成**(不是通过)· 其余 ${ran - failures}/${ran} 通过`);
    await browser.close();
    process.exit(3);
}
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); await browser.close(); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
await browser.close();
process.exit(0);
