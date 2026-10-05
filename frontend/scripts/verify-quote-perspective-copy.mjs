#!/usr/bin/env node
/**
 * 判据 · #225 a1 ②③ 报价页「发布口径」与换算文案锁。
 *
 * 🔴 文案锁**真调 `buildPriceRationale` 跑出句子再验**,不扫源码:
 *    扫源码只能证明"某个词没写在文件里",而文案是拼出来的 ——
 *    `${perspectiveWord}` 这种拼接,源码里看不见最终句子。
 *
 * 🔴 契约在 C 尖 `2f3307a3e` 上按 sha 核过(不是 grep 对方工作树):
 *      GET /api/quotes/{id}/media-mix?perspective={self_media|portal|mixed}
 *      → mix.*(槽,口径不改它) · posts_estimate.*(条) · posts_estimate_total
 *        · delivery_perspective · posts_per_slot_bps(**禁显示**)
 *
 * 两态退出码:0 全过 / 1 有失败。
 */
import { readFileSync, mkdirSync, writeFileSync, rmSync, mkdtempSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const decomment = (s) => s
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^\s*\/\/.*$/gm, '');

let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};

const require_ = createRequire(import.meta.url);
const cacheRoot = join(ROOT, 'node_modules', '.cache');
mkdirSync(cacheRoot, { recursive: true });
const tmp = mkdtempSync(join(cacheRoot, 'a225q-'));
process.on('exit', () => { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 尽力 */ } });

let R = null;
try {
    /*
     * 🔴 用 **esbuild 打包**,不是 transpileModule 单文件:
     *    `priceRationale.ts` 不是零 import 模块(它引 `@/lib/...`),
     *    单文件转译出来的东西 import 不动,报的是「判据不可用」——
     *    那和「判据全绿」只差一个字,但意思相反,所以这里宁可显式失败。
     */
    const esbuild = require_('esbuild');
    const out = join(tmp, 'priceRationale.mjs');
    await esbuild.build({
        entryPoints: [join(ROOT, 'src/pages/Quote/utils/priceRationale.ts')],
        bundle: true, outfile: out, format: 'esm', platform: 'neutral',
        alias: { '@': join(ROOT, 'src') }, logLevel: 'silent',
    });
    R = await import(`file://${out.split('\\').join('/')}`);
} catch (e) {
    console.log('FAIL 判据不可用(不当绿灯):取不到 priceRationale —— '
        + String((e && e.message) || e).split('\n')[0]);
    process.exit(1);
}

/** 一条典型关键词(只给 buildPriceRationale 用得到的字段)。 */
const KW = {
    keyword: 'QA 词', standard: { articles: 7 }, entry: { articles: 7 }, flagship: { articles: 7 },
    effective_competition: 'medium', value_score: 60,
};
const rowsFor = (mixOpts) => R.buildPriceRationale(KW, 'standard', mixOpts).rows;
const mixRow = (mixOpts) => (rowsFor(mixOpts).find((r) => r.kind === 'articles') || {}).body || '';

console.log('Q1 投放组合行:槽是槽,条是条');
{
    /* 🔴 33 是**任何整数倍都算不出来**的:articles=7,7×{1,2,3,5}=7/14/21/35。
       上一版写 35,而注毒 U1 把读值换成 `articles * 5` —— 也是 35,
       错的算法算出对的数,这一格照样绿。 */
    const body = mixRow({ deliveryPerspective: 'self_media', postsEstimateTotal: 33 });
    ok(body.length > 10, 'Q1a 分母自证:真的跑出了一句话', body.slice(0, 90));
    ok(/本次交付 \d+ 槽/.test(body),
        'Q1b 🔴 合计说的是**槽**(合同分配)—— 原来写「N 条」,'
        + '和同屏别处的槽数是同一个数却两个量词', body.match(/本次交付[^。]*/)?.[0] || '');
    ok(!/重点媒体锚点 \d+ 篇|行业与平台覆盖 \d+ 篇/.test(body),
        'Q1c 逐桶也不再标「篇」(它们是槽的分布)');
    ok(/按自媒体发布约需 33 条左右/.test(body),
        'Q1d 🔴 条数用**服务端给的** `posts_estimate_total`(这里 33 —— '
        + '故意挑成任何整数倍都算不出来的数),前端不乘不加',
        body.match(/按[^。]*/)?.[0] || '(没有这句)');
    ok(/运营口径/.test(body),
        'Q1e 带「运营口径」四字(裁定书 v2 §7:换算不得被读成效果承诺)');
}

console.log('\nQ2 口径换了,句子跟着换');
{
    const self = mixRow({ deliveryPerspective: 'self_media', postsEstimateTotal: 33 });
    const portal = mixRow({ deliveryPerspective: 'portal', postsEstimateTotal: 9 });
    const mixed = mixRow({ deliveryPerspective: 'mixed', postsEstimateTotal: 26 });
    ok(/自媒体/.test(self) && /门户/.test(portal) && /混合/.test(mixed),
        'Q2a 三个口径各说各的名字', [self, portal, mixed].map((x) => x.match(/按(.+?)发布/)?.[1]).join('/'));
    ok(/33 条/.test(self) && /9 条/.test(portal) && /26 条/.test(mixed),
        'Q2b 🔴 条数**跟着服务端给的数走** —— 三个口径三个数,前端没有自己的算法');
    /* 🔴 反臂:认不出的口径**不说这句话**,而不是编一个名字出来 */
    const unknown = mixRow({ deliveryPerspective: 'whatever', postsEstimateTotal: 33 });
    ok(!/发布约需/.test(unknown),
        'Q2c 🔴 认不出的口径 ⇒ **不说** —— 宁可少一句,也不能编一个口径名',
        unknown.match(/按[^。]*/)?.[0] || '(没说,对)');
    /* 服务端没给条数时同样不说(不拿槽数硬顶成条数) */
    const noTotal = mixRow({ deliveryPerspective: 'self_media' });
    ok(!/发布约需/.test(noTotal),
        'Q2d 服务端没给条数 ⇒ 不说(不拿槽数当条数)');
}

console.log('\nQ3 文案锁:跑出来的句子里不许有那些词');
{
    const all = [];
    for (const p of ['self_media', 'portal', 'mixed', 'whatever', '']) {
        for (const t of [0, 9, 33, 999]) {
            for (const r of rowsFor({ deliveryPerspective: p, postsEstimateTotal: t })) {
                all.push(`${r.title}:${r.body}`);
            }
        }
    }
    ok(all.length >= 20, 'Q3a 分母自证:跑出的句子条数', `${all.length} 句`);
    const text = all.join('\n');
    for (const w of R.RATIONALE_FORBIDDEN_PHRASES) {
        ok(!text.includes(w), `Q3b-${w} 🔴 禁用词不出现在**跑出来的文案**里`,
            text.includes(w) ? '🔴 出现了' : '零处');
    }
    /* 🔴 必含词:只查**带换算的**那些句子(没换算的句子本来就不该硬塞「约」) */
    const converted = all.filter((x) => /发布约需/.test(x));
    ok(converted.length >= 3, 'Q3c 分母自证:带换算的句子', `${converted.length} 句`);
    for (const h of R.RATIONALE_REQUIRED_HEDGES) {
        ok(converted.every((x) => x.includes(h)),
            `Q3d-${h} 🔴 换算文案必含「${h}」—— 把估算说成估算`);
    }
    /*
     * 🔴 Q3e 的域是**换算那句话**,不是整块文案(工单 §158 主语 =「换算文案」)。
     *    第一版一刀切查整块,当场打红了「投放节奏」里一句**在产的老文案**:
     *      「行业与平台覆盖内容初始通常约为重点媒体锚点的 2 倍」
     *    那句讲的是**初始组合先验**(priceRationale :114 注释明写「不是效果等价」),
     *    与槽→条换算是两件事。
     *    🔴 缩域已单独报 Review —— 不是默默把它藏到锁外面。
     */
    const convSentences = converted.map((x) => (x.match(/按[^。]*。/) || [''])[0]);
    ok(convSentences.every((x) => x.length > 5),
        'Q3e0 分母自证:切出了换算那句话', convSentences[0] || '(切不出来)');
    for (const w of R.CONVERSION_FORBIDDEN_PHRASES) {
        ok(convSentences.every((x) => !x.includes(w)),
            `Q3e-${w.trim()} 🔴 换算文案里不出现「${w.trim()}」—— `
            + '一旦给出倍数,读者会自己做「N 条 ≈ 1 篇」的效果等价换算');
    }
    ok(convSentences.every((x) => !/\d+\s*倍/.test(x)),
        'Q3e2 🔴 换算文案里不出现倍数数字');
    /*
     * 🔴 [④ 截图逮到的] 这句挂在**每个关键词**的卡片上,引的却是**整单**合计。
     *    真页面上是「本次交付 4 槽 · 按自媒体发布约需 29 条左右」——
     *    4 是这个词的槽、29 是整单的条,并排读成「这 4 槽要发 29 条」。
     *    两个数各自都对,所以前面所有静态判据都是绿的;
     *    是把页面截出来用眼睛看才看见的。补一道锁钉住**指代词**。
     */
    ok(converted.every((x) => /(本单|整单)按[^。]*发布约需/.test(x)),
        'Q3f 🔴 换算句必须写明它说的是**整单**(「本单按…」)—— '
        + '这句在每个关键词的卡片里出现,而条数是整单合计;'
        + '不写指代词就会和同句的「本次交付 N 槽」(那是这个词的槽)读成一笔账');
}

console.log('\nQ4 下拉:整单一个,只在服务商端');
{
    const FLOW = decomment(rd('src/pages/Quote/OnlineQuoteFlow.tsx'));
    ok(/data-testid="quote-perspective-select"/.test(FLOW),
        'Q4a 下拉在报价页上');
    ok((FLOW.match(/data-testid="quote-perspective-select"/g) || []).length === 1,
        'Q4b 🔴 **整单只有一个** —— `WhyThisPrice` 在两处渲染且都在关键词循环里,'
        + '放进去会变成每词一个下拉');
    ok(/perspective \? `\?perspective=/.test(FLOW),
        'Q4c 切口径时**真的带参数重取**(不带就只是个好看的下拉)');
    ok(/pricingData\?\.generated_at, perspective\]/.test(FLOW),
        'Q4d 🔴 `perspective` 进了依赖数组 —— 不进的话切了不重取,'
        + '句子永远停在第一次的口径上,而下拉看起来是好用的');
    ok(!/posts_per_slot_bps/.test(FLOW),
        'Q4e 🔴 前端**不取** `posts_per_slot_bps`:内部运营参数,取了就会有人显示它');

    /* 🔴 结构锁:客户面零引用 */
    // [开源 E3 · 前端 · 2026-10-01] src/pages/M3/Selection(M3 版选词页)随 pages/M3 整删;客户面只剩在役的 /s/:token 选词页
    const CUST = ['src/pages/Selection'];
    const { readdirSync, statSync } = require_('node:fs');
    const walk = (d, acc = []) => {
        for (const f of readdirSync(join(ROOT, d))) {
            const rel = d + '/' + f;
            if (statSync(join(ROOT, rel)).isDirectory()) walk(rel, acc);
            else if (/\.tsx?$/.test(f)) acc.push(rel);
        }
        return acc;
    };
    const files = CUST.flatMap((d) => walk(d));
    ok(files.length > 5, 'Q4f 分母自证:扫到客户面文件', `${files.length} 个`);
    const leaked = files.filter((f) => /posts_estimate|delivery_perspective|posts_per_slot_bps|WhyThisPrice|发布口径/
        .test(rd(f)));
    ok(leaked.length === 0,
        'Q4g 🔴 客户面(`Selection/**`)**零引用**换算字段与口径控件 —— '
        + 'Owner ③「不要看到,这是运营的事」', leaked.join(', ') || '零处');
}

console.log('\nQ5 同屏槽/篇不混');
{
    const FLOW = decomment(rd('src/pages/Quote/OnlineQuoteFlow.tsx'));
    ok(/\{t\.total_articles\} 槽/.test(FLOW),
        'Q5a 🔴 三档卡那个数标的是**槽** —— 它 = Σ 每词 articles,'
        + '与 `/media-mix` 的 `capacity_total` 同源;原来标「篇」,'
        + '会和同屏「本次交付 N 槽」变成同一个数两个量词');
    ok(!/\{t\.total_articles\}篇/.test(FLOW), 'Q5b 反臂:那处不再标「篇」');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
