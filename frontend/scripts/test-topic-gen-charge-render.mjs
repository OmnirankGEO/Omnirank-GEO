#!/usr/bin/env node
/**
 * 判据 · WO_218-a1 前端半 —— 「生成标题」这次会扣多少,**真浏览器臂**。
 *
 * Owner 2026-09-18 新规:做完必须调用前端真实测试,全部通过才报完成。
 *
 * 走的是完整链:后端回包(DTO)→ `loadProjectDetail` 里那次 `parseTopicGenCharge`
 * → 真渲染写作工作台 → 读屏上那个数。
 * 🔴 只做文本匹配的话,「读了字段但渲染时又顶回去」这种改动看不见 ——
 *    而本单要治的原病(`mapDto` 里 `testedQuestionCount: keywordCount`)就是那个形状。
 *
 * 🔴 **不走沙盒**:沙盒的详情夹具是我自己那份(WO_218-a1 同笔补的 `charge`),
 *    拿它当被测输入等于**拿我造的数自证**。这一臂自己喂回包,
 *    因而能构造沙盒构造不出来的两种局面:**份数变了**、**后端没给价**。
 *
 * 🔴 **不碰真客户**:标准写死「真客户品牌一律禁碰」,夹具客户是 QA 名。
 *
 * 三态退出:0 全绿 · 1 有判据红 · 3 门自己没跑成(打包/浏览器取不到)。
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

/*
 * 🔴 [2026-09-18] `node_modules/.cache` 是**构建产物**,全新 `npm ci` 的树里不存在。
 *    原来直接 mkdtemp 进去 ⇒ 在干净树上 ENOENT 崩成 **rc=1**(读作"有判据红了"),
 *    而真相是**门根本没跑起来**。我本机有这个目录,所以恒绿 ——
 *    与「浏览器门进 build 链」同形:这条臂只在它被写出来的那个环境里跑得动。
 * 🔴 只加 mkdir 是修一半:建目录这一步本身也要**包进三态**,
 *    否则任何发生在三态机制之外的失败都会伪装成"判据红了"。
 */
let outDir;
try {
    const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
    mkdirSync(CACHE_ROOT, { recursive: true });
    outDir = mkdtempSync(join(CACHE_ROOT, 'a218-charge-'));
} catch (err) { cannotRun('临时目录建不出来(node_modules/.cache 不存在且建不了)', err); }
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
import { OnboardingProvider } from ${q('src/context/OnboardingContext.tsx')};
import { PricingProvider } from ${q('src/context/PricingContext.tsx')};
import { ClientProvider } from ${q('src/context/ClientContext.tsx')};
import { WritingHall } from ${q('src/pages/Writing/WritingHall.tsx')};

(globalThis as any).__mount = function () {
    createRoot(document.getElementById('root')!).render(
        <MemoryRouter initialEntries={['/writing']}>
          <AuthProvider><OrganizationProvider><UserModeProvider><WalletProvider>
            <OnboardingProvider><PricingProvider><ClientProvider>
              <WritingHall />
            </ClientProvider></PricingProvider></OnboardingProvider>
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
        loader: { '.tsx': 'tsx', '.ts': 'ts', '.svg': 'dataurl', '.png': 'dataurl', '.md': 'text' },
        logLevel: 'silent',
    });
} catch (err) { cannotRun('打包失败', err); }

writeFileSync(join(outDir, 'index.html'),
    '<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>a218</title></head>'
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

const QA_CLIENT = 'QA 夹具客户';
const PROJECT_ID = 90218;

/** 后端 `charge_card()` 的形状。**照契约原件**(services/topic_gen_charge.py)。 */
const card = (base, count, { ceiling } = {}) => ({
    feature_code: 'topic_gen',
    base_points: base,
    keyword_count: count,
    estimated_points: base * count,
    ceiling_points: ceiling === undefined ? base * count : ceiling,
});

/** 本轮回包由它决定;每一轮重载页面后生效。 */
let ROUND = { keywords: 3, charge: card(80, 3), admin: false };

const kwList = (n) => Array.from({ length: n }, (_, i) => ({
    id: 40000 + i, keyword: `QA 夹具词 ${i + 1}`, required_articles: 2,
    recommended_platforms: '搜狐号,百家号', final_price: 1200,
}));

function apiBody(path) {
    if (path.includes('/auth/me')) {
        return { id: 9001, username: 'qa-agent', is_admin: ROUND.admin, agent_level: 1, email: 'qa@example.invalid' };
    }
    if (path === '/api/wallet' || path.endsWith('/api/wallet')) {
        return { success: true, data: {
            paid_points: 100000, commission_points: 0, bonus_points: 0,
            frozen_points: 0, total_recharged: 100000,
            customer_credit: {
                tool_credit_points: 100000, publish_credit_points: 100000,
                bonus_credit_points: 0, total_purchased_points: 100000, total_consumed_points: 0,
            },
        } };
    }
    if (path.includes('/wallet/pricing')) {
        return { success: true, data: [
            { feature_code: 'article_gen', cost_points: 390, is_active: true },
            { feature_code: 'topic_gen', cost_points: 80, is_active: true },
        ] };
    }
    if (/^\/api\/writing\/projects\/\d+$/.test(path)) {
        /* 🔴 `charge` 缺省时**整个键都不出现**,而不是给 `null` ——
              两种都要能被前端处理成"不显示价",这一轮测的是前者。 */
        return {
            success: true, keywords: kwList(ROUND.keywords), topics: [],
            ...(ROUND.charge === null ? {} : { charge: ROUND.charge }),
        };
    }
    if (path === '/api/writing/projects') {
        return { success: true, projects: [{
            id: PROJECT_ID, quote_id: PROJECT_ID, brand_name: QA_CLIENT,
            client_name: QA_CLIENT, keyword_count: ROUND.keywords,
            writing_status: 'pending', created_at: '2026-09-18T00:00:00Z',
        }] };
    }
    return { success: true, projects: [], topics: [], keywords: [], articles: [], clients: [], media: [], data: [] };
}

/* ── 浏览器 ───────────────────────────────────────────────────────────── */

let browser, page;
try {
    browser = await playwright.chromium.launch();
    page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    await page.addInitScript(() => { localStorage.setItem('omnirank_token', 'qa-token'); });
    await page.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        await route.fulfill({
            status: 200, contentType: 'application/json',
            body: JSON.stringify(apiBody(path)),
        });
    });
} catch (err) { cannotRun('chromium 起不来', err); }

/**
 * 跑一轮:重载 → 进工作台 → 把屏幕上所有价徽标读回来。
 *
 * 🔴 进工作台那颗按钮的文案**随项目状态变**,这里固定喂 `writing_status: 'pending'`
 *    所以是「开始创作」;仍然用一组候选文案找,免得哪天状态变了这一臂假红。
 */
async function runRound(round) {
    ROUND = round;
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate(() => globalThis.__mount());
    await page.waitForTimeout(1800);
    const entered = await page.evaluate(() => {
        const TXT = ['开始创作', '创作', '检查标题', '查看详情'];
        const nodes = [...document.querySelectorAll('button,[role="button"],a')];
        for (const t of TXT) {
            const hit = nodes.find((n) => (n.textContent || '').includes(t)
                && n.getBoundingClientRect().width > 0 && !n.disabled);
            if (hit) { hit.click(); return t; }
        }
        return '';
    }).catch(() => '');
    if (!entered) return { entered: '', badges: [], screen: '' };
    await page.waitForTimeout(1800);
    const badges = await page.evaluate(() => [...document.querySelectorAll('[data-testid="topic-gen-charge"]')]
        .filter((el) => {
            const r = el.getBoundingClientRect();
            const cs = getComputedStyle(el);
            return r.width > 0 && r.height > 0 && cs.display !== 'none' && cs.visibility !== 'hidden';
        })
        .map((el) => (el.textContent || '').replace(/\s+/g, ' ').trim()));
    const screen = await page.evaluate(() => (document.getElementById('root') || document.body).innerText.replace(/\s+/g, ' '));
    return { entered, badges, screen };
}

console.log('=== WO_218-a1 · 生成标题计费显示(真浏览器)===\n');

/* ── C0 分母自证 ──────────────────────────────────────────────────────── */
const r1 = await runRound({ keywords: 3, charge: card(80, 3), admin: false });
ok(Boolean(r1.entered), 'C0 分母自证:真的进到了写作工作台',
    r1.entered ? `点了「${r1.entered}」` : '🔴 一颗进工作台的按钮都没点到 —— 下面每一格的读数都不作数');
if (!r1.entered) cannotRun('进不了工作台,后面几格无从谈起', new Error('no workbench'));

/* ── C1 正臂:点之前就看得见价,且是后端给的那个数 ────────────────────── */
ok(r1.badges.length > 0, 'C1 🔴 **点按钮之前**屏幕上就有价 —— 元指令 2「按钮级确认扣费」',
    `${r1.badges.length} 处 · ${r1.badges[0] || ''}`);
ok(r1.badges.every((t) => t.includes('240')), 'C1b 显示的是后端给的 `ceiling_points`(3 词 × 80 = 240)',
    r1.badges.join(' | ') || '(无)');
ok(!r1.screen.includes('本次将扣 80 算力'),
    'C1c 🔴 显示的**不是基价** —— 把 `base_points` 当成本次要扣的数,客户会以为写 3 个词只花 80',
    '屏幕上没有「本次将扣 80 算力」');

/* ── C2 反臂:份数变了,数必须跟着变 ─────────────────────────────────── */
const r2 = await runRound({ keywords: 5, charge: card(80, 5), admin: false });
ok(r2.badges.length > 0 && r2.badges.every((t) => t.includes('400')),
    'C2 🔴 `keyword_count` 变了(3→5),屏幕上的数**跟着变**(240→400)',
    r2.badges.join(' | ') || '(无)');
ok(!r2.screen.includes('240'),
    'C2b 🔴 旧价没有留在屏幕上 —— 「显示一个过期的价」比不显示更糟,客户是按那个数同意扣费的',
    '屏幕上找不到 240');

/* ── C3 反臂:后端没给价 ⇒ 一个数都不许显示(**不是 0**) ──────────────── */
const r3 = await runRound({ keywords: 3, charge: null, admin: false });
ok(r3.badges.length === 0,
    'C3 🔴 后端没给 `charge`(当前:admin 不计费)⇒ **不显示价**,而不是显示 0',
    `${r3.badges.length} 处`);
ok(!r3.screen.includes('本次将扣') && !r3.screen.includes('本次免费'),
    'C3b 🔴 也没有退而求其次顶一个数上去 —— `None` 不是 `0`,压成一种就会对客户说错话',
    '屏幕上既无「本次将扣」也无「本次免费」');

/* ── C4 反臂:上限比实扣小(契约违规回包)⇒ 不显示,不许糊过去 ────────── */
const r4 = await runRound({ keywords: 3, charge: card(80, 3, { ceiling: 100 }), admin: false });
ok(r4.badges.length === 0,
    'C4 🔴 `ceiling < estimated` 的回包**不显示** —— 退而求其次显示实扣,等于把一次契约违规悄悄糊过去,'
    + '而那正是客户按之同意扣费的数',
    `${r4.badges.length} 处 · ${r4.badges.join(' | ')}`);

/* ── C6 反臂:**同一张页面**里加一个关键词,价当场跟着变 ────────────────
 * 这一格与 C2 看着像,分辨力完全不同:C2 换轮时**重载**了页面(状态被清成 null),
 * 所以「只在有值时才写」那种改法在 C2 下照样绿。这里页面不动,
 * 只把回包换掉再走一次真实的 add-keyword 成功路径(它内部调 `loadProjectDetail`)。
 */
const r6 = await runRound({ keywords: 3, charge: card(80, 3), admin: false });
ok(r6.badges.some((t) => t.includes('240')), 'C6a 前置:加词之前屏幕上是 240', r6.badges.join(' | ') || '(无)');
const opened = await page.evaluate(() => {
    const btn = [...document.querySelectorAll('button')]
        .find((b) => (b.getAttribute('aria-label') || '') === '添加关键词');
    if (!btn) return false;
    btn.click();
    return true;
}).catch(() => false);
await page.waitForTimeout(900);
ok(opened, 'C6b 分母自证:「添加关键词」对话框真的打开了 —— 打不开的话下一格会"绿"得没有意义',
    opened ? '已打开' : '🔴 没找到那颗按钮');
let r6after = { badges: [], screen: '' };
if (opened) {
    /* 🔴 先把回包换成 4 个词,再提交 —— 提交成功后产品自己会重拉详情。 */
    ROUND = { keywords: 4, charge: card(80, 4), admin: false };
    await page.evaluate(() => {
        const input = document.querySelector('input[placeholder^="例如"]');
        if (!input) return;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, 'QA 夹具新词');
        input.dispatchEvent(new Event('input', { bubbles: true }));
    }).catch(() => { });
    await page.waitForTimeout(400);
    /*
     * 🔴 **必须缩到对话框里面找。** 第一版用 `includes('添加')` 在全文档找,
     *    DOM 顺序上先命中的是工具栏那颗「添加关键词」(6351 行,对话框在 8286 行)——
     *    于是"提交"变成了"把对话框又关一次",页面纹丝不动,
     *    而读数是「价没变」,**与真缺陷完全同形**。归因之前差点把它记成产品的账。
     */
    const submitted = await page.evaluate(() => {
        const dlg = document.querySelector('[role="dialog"]');
        if (!dlg) return 'no-dialog';
        const btn = [...dlg.querySelectorAll('button')]
            .find((b) => (b.textContent || '').includes('添加并生成标题'));
        if (!btn) return 'no-button';
        if (btn.disabled) return 'disabled';
        btn.click();
        return 'clicked';
    }).catch(() => 'threw');
    ok(submitted === 'clicked',
        'C6b2 分母自证:**确实点到了对话框里那颗提交** —— 点不到就是"我没点上",'
        + '不是"它没刷新";这两件事在下一格的读数上一模一样',
        submitted);
    await page.waitForTimeout(2000);
    r6after = await page.evaluate(() => ({
        badges: [...document.querySelectorAll('[data-testid="topic-gen-charge"]')]
            .map((el) => (el.textContent || '').replace(/\s+/g, ' ').trim()),
        screen: (document.getElementById('root') || document.body).innerText.replace(/\s+/g, ' '),
    }));
}
ok(opened && r6after.badges.length > 0 && r6after.badges.every((t) => t.includes('320')),
    'C6 🔴 **页面不动**、加了一个关键词(3→4)⇒ 屏幕上的价当场变成 320 —— '
    + '加完词还停在旧价上,比不显示更糟:客户是按那个数同意扣费的',
    r6after.badges.join(' | ') || '(无)');
ok(opened && !r6after.badges.some((t) => t.includes('240')),
    'C6c 🔴 旧价 240 已经不在任何一处徽标上',
    r6after.badges.join(' | ') || '(无)');

/* ── C7 反臂:**同一张页面**里价从「有」变成「没有」⇒ 必须归零 ──────────
 * 🔴 这一格是 M6 打出来的,不是我一开始想到的:
 *    把 `setTopicGenCharge(parse(...))` 改成 `if (data.charge) setTopicGenCharge(...)`,
 *    在 C6 下**存活** —— 因为 C6 两次回包都带 charge,条件写照样触发。
 *    真正会出事的是价**从有到无**那一刻:后端这一次已经不报价了(比如切到不计费),
 *    而屏幕停在旧价上。**显示一个已经不存在的收费**,比不显示更糟。
 */
if (opened) {
    ROUND = { keywords: 5, charge: null, admin: false };
    const reopened = await page.evaluate(() => {
        const btn = [...document.querySelectorAll('button')]
            .find((b) => (b.getAttribute('aria-label') || '') === '添加关键词');
        if (!btn) return false;
        btn.click();
        return true;
    }).catch(() => false);
    await page.waitForTimeout(800);
    await page.evaluate(() => {
        const input = document.querySelector('input[placeholder^="例如"]');
        if (!input) return;
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        setter.call(input, 'QA 夹具新词二');
        input.dispatchEvent(new Event('input', { bubbles: true }));
    }).catch(() => { });
    await page.waitForTimeout(400);
    const submitted2 = await page.evaluate(() => {
        const dlg = document.querySelector('[role="dialog"]');
        if (!dlg) return 'no-dialog';
        const btn = [...dlg.querySelectorAll('button')]
            .find((b) => (b.textContent || '').includes('添加并生成标题'));
        if (!btn || btn.disabled) return 'no-button';
        btn.click();
        return 'clicked';
    }).catch(() => 'threw');
    await page.waitForTimeout(2000);
    const after7 = await page.evaluate(() => [...document.querySelectorAll('[data-testid="topic-gen-charge"]')]
        .map((el) => (el.textContent || '').trim()));
    ok(reopened && submitted2 === 'clicked',
        'C7a 分母自证:第二次加词也真的提交了', `${reopened} / ${submitted2}`);
    ok(submitted2 === 'clicked' && after7.length === 0,
        'C7 🔴 价从「有」变成「没有」⇒ 屏幕上**一处都不剩** —— '
        + '停在旧价上等于显示一个已经不存在的收费,比不显示更糟',
        after7.join(' | ') || '零处');
}

/* ── C5 反臂:免费那一面(**补救**)不许挂价 ─────────────────────────
 * 🔴 [2026-09-19 契约反转] 以前这里写的是「`isNewKwOnly` 那一面免费」—— **已经不成立**:
 *    新词面现在**要收费**(全新生产、从没付过),免费的是**补救**面。
 * 🔴 这一格**故意不在浏览器里做**:要构造补救面需要"某批次里缺题的那一条",
 *    构造出来的那个局面本身就得靠我编状态,编错了这一格会假绿。
 *    它由逻辑臂 `verify-topic-gen-charge.mjs` 直接调 `chargeText({isNewKwOnly:true})` 钉死 ——
 *    那是真调函数,比我在浏览器里摆一个可能摆错的局面更有分辨力。
 *    **写在这里是为了让"这一格去哪了"有答案**,而不是让它悄悄消失。
 */

console.log('');
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); await browser.close(); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
await browser.close();
process.exit(0);
