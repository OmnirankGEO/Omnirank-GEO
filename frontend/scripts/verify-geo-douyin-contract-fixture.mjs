#!/usr/bin/env node
/**
 * 判据 · 夹具与生产契约同步(#188 · Review 09-13 抓到)。
 *
 * 🔴 **本判据故意不进 build 链。** 它要读 `../services/geo_douyin/*.py`,
 *    而 build 链有一道越界闸(`verify-no-backend-refs-in-build-chain.mjs`)禁止
 *    链上的脚本引用后端路径 —— 那道闸是对的:构建镜像是 node:20-alpine,
 *    里面**只有 frontend/**,链上脚本一读 `../services` 就是构建当场崩。
 *
 * 🔴 所以它必须**被人记得跑** —— 而"只靠人记得"正是本仓栽过的那类控制。
 *    对冲办法有三条,交付物里要写明:
 *      1. 它和浏览器臂在同一条手跑清单里(见 A_188 交付物 §判据);
 *      2. 夹具 JSON 顶部写了"不要手改";
 *      3. 它一红就说明**截图和判据在验另一个世界的文案**,后果足够大,值得单列。
 *
 * 跑法:cd frontend && node scripts/verify-geo-douyin-contract-fixture.mjs
 */
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const rdRepo = (p) => readFileSync(join(ROOT, '..', p), 'utf8');

let failed = 0;
const ok = (cond, msg, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${msg}${detail ? ` — ${detail}` : ''}`);
    if (!cond) failed += 1;
};

// ══ G0 夹具与生产契约同步 ══════════════════════════════════════════════
/**
 * 🔴 [#188 · Review 09-13 抓到] 我三个脚本里的画幅标签是**编的**:
 *    「竖版 9:16 / 抖音常用」「小红书常用」。生产 config 写的是
 *    「全屏 9:16 / 竖屏满屏场景,沿用十母版那一族」,而 3:4 的 hint 是
 *    「抖音图文默认,信息密度高」—— 我那句「小红书常用」不只是编的,是**反的**。
 *    后果:截图和判据验的都是另一个世界的文案,而两边都绿。
 *
 * 🔴 修法不是把三个文件各改一遍(那是修实例,下次加第五款风格照样漂),
 *    是**一份夹具 + 一道每次重新比对的闸**:
 *    `scripts/fixtures/geo-douyin-contract.json` 由生产源抽取,本段每次跑都从
 *    `services/geo_douyin/config.py` / `card_templates.py` 重新抽一遍去比。
 *    生产改了文案而夹具没跟 ⇒ 这里红。
 *
 * 🔴 分母自证:抽出来是空的时候,`[] 等于 []` 会恒真 —— 那是"扫描器死了"
 *    而不是"契约一致"。所以先要求两边都非空、且条数相等。
 */
console.log('\nG0 夹具与生产契约同步(挡"验的是另一个世界的文案")');
{
    const FIX = JSON.parse(rd('scripts/fixtures/geo-douyin-contract.json'));
    const cfg = rdRepo('services/geo_douyin/config.py');
    const tpl = rdRepo('services/geo_douyin/card_templates.py');

    const arBlock = cfg.slice(cfg.indexOf('ASPECT_RATIOS = {'), cfg.indexOf('ASPECT_RATIO_DEFAULT'));
    const liveAr = [...arBlock.matchAll(new RegExp(
        '"([0-9]+:[0-9]+)":\\s*\\{\\s*"label":\\s*"([^"]+)",\\s*"hint":\\s*"([^"]+)"', 'g'))]
        .map((m) => ({ key: m[1], label: m[2], hint: m[3] }));
    const liveStyles = [...tpl.matchAll(/key="(\w+)", label="([^"]+)"/g)]
        .map((m) => ({ key: m[1], label: m[2] }));

    ok(liveAr.length >= 2 && FIX.aspect_ratios.length === liveAr.length,
        'G0a 分母自证:从 config.py 真的抽出了画幅(抽空了就是扫描器死了,不是"一致")',
        `生产 ${liveAr.length} 条 · 夹具 ${FIX.aspect_ratios.length} 条`);
    ok(liveStyles.length >= 4 && FIX.styles.length === liveStyles.length,
        'G0b 分母自证:从 card_templates.py 真的抽出了风格',
        `生产 ${liveStyles.length} 款 · 夹具 ${FIX.styles.length} 款`);

    const arSame = liveAr.every((L) => {
        const f = FIX.aspect_ratios.find((x) => x.key === L.key);
        return f && f.label === L.label && f.hint === L.hint;
    });
    ok(arSame, 'G0c 🔴 画幅的 label 与 hint 与生产**逐字**一致'
        + '(编一个「抖音常用」出来,截图和判据就都在验另一个世界)',
        JSON.stringify(liveAr.map((x) => x.key + '=' + x.label)));
    const stSame = liveStyles.every((L) => {
        const f = FIX.styles.find((x) => x.key === L.key);
        return f && f.label === L.label;
    });
    ok(stSame, 'G0d 风格的 key 与 label 与生产逐字一致',
        JSON.stringify(liveStyles.map((x) => x.key)));

    const liveDefault = (/ASPECT_RATIO_DEFAULT\s*=\s*"([^"]+)"/.exec(cfg) || [])[1] || '';
    ok(liveDefault !== '' && liveDefault === FIX.aspect_ratio_default,
        'G0e 默认画幅与生产一致', `${liveDefault} vs ${FIX.aspect_ratio_default}`);

    /**
     * 🔴 [同一天第二次栽] 上面几条钉的是**文案**,而夹具还会写错**形状**:
     *    我照 production-quote 的 `rule: {included_cards}` 写了 /pricing 的桩,
     *    真 /pricing 是 `included_cards` 在顶层、加价在 `extra_card.cost_points`
     *    (api/geo_douyin_api.py:api_pricing 的 return)。
     *    后果:`freeCardCount` 恒 0 ⇒ 加价角标一枚都不出现 ——
     *    而那正是要给 Owner 看的东西。判据全绿,截图静静地少了那件事。
     *    ⇒ 形状也要钉:那几个键必须真的在端点的 return 里。
     */
    const api = rdRepo('api/geo_douyin_api.py');
    const pricingFn = api.slice(api.indexOf('async def api_pricing'));
    const retIdx = pricingFn.indexOf('return {"status": "success", "first_generation"');
    const retBlock = retIdx >= 0 ? pricingFn.slice(retIdx, retIdx + 1800) : '';
    ok(retBlock.length > 100, 'G0g0 分母自证:真的定位到了 /pricing 的 return 块',
        `${retBlock.length} 字符`);
    for (const key of ['included_cards', 'extra_card', 'card_min', 'card_max', 'card_default']) {
        ok(retBlock.includes(`"${key}"`),
            `G0g[${key}] /pricing 真的在**顶层**返回这个键(桩必须同形)`);
    }
    ok(!/"rule"\s*:/.test(retBlock),
        'G0h 🔴 反臂:/pricing **没有** `rule` 这一层 —— 有的话说明我把它和 '
        + 'production-quote 的形状搞混了(上一版就是这么错的)');

    /**
     * 🔴 正样本臂:上面四条全是"两边相等"型断言,而**相等**在两边一起错时也成立。
     *    这条证明比对真的会失败 —— 改一个字就必须红。没有它,G0c/G0d 可能是空断言。
     */
    const tampered = FIX.aspect_ratios.map((x, k) => (k === 0 ? { ...x, label: x.label + '!' } : x));
    const wouldCatch = !liveAr.every((L) => {
        const f = tampered.find((x) => x.key === L.key);
        return f && f.label === L.label && f.hint === L.hint;
    });
    ok(wouldCatch, 'G0f 正样本臂:夹具里改一个字,上面那几条真的会红(否则它们是空断言)');
}


console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过:夹具与生产契约逐字一致');
process.exit(0);
