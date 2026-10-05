#!/usr/bin/env node
/**
 * 判据 · #210 发布中心按**当前客户的全部有效报价**取文(结构 + 纯函数臂)。
 *
 * 病根(210-d1 真客户取证):`pickProjectForBrand` 用 `.find()` 只回**第一份**报价,
 * 而取文那条 SQL 是 `WHERE t.quote_id = %s` —— 一次只看一份。
 * 品牌 17 有两份有效报价:94 下 18 篇、378 下 1 篇;用户打开的是 378,
 * 于是他刚补的和重写的那几篇**一篇都看不见**。文章一篇没丢,丢的是取文的作用域。
 *
 * 两态退出码:**0 全过 / 1 有失败**(本闸在 build 链里,rc=3 会让后面的闸不跑)。
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

/* ── 让判据**真的调**那两个纯函数(零 import 模块,transpile 后 import)──── */
const require_ = createRequire(import.meta.url);
let SCOPE = null;
let PART = null;
const cacheRoot = join(ROOT, 'node_modules', '.cache');
mkdirSync(cacheRoot, { recursive: true });
const tmp = mkdtempSync(join(cacheRoot, 'a210-'));
process.on('exit', () => { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 尽力 */ } });
try {
    const ts = require_('typescript');
    for (const [rel, set] of [
        ['src/pages/Publishing/publishClientScope.ts', (m) => { SCOPE = m; }],
        ['src/pages/Publishing/publishCenterScopeLogic.ts', (m) => { PART = m; }],
    ]) {
        const js = ts.transpileModule(rd(rel), {
            compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
        }).outputText;
        const f = join(tmp, rel.split('/').pop().replace(/\.ts$/, '.mjs'));
        writeFileSync(f, js, 'utf8');
        set(await import(`file://${f.split('\\').join('/')}`));
    }
} catch (e) {
    console.log('FAIL 判据不可用(不当绿灯):取不到纯函数模块 —— '
        + String((e && e.message) || e).split('\n')[0]);
    process.exit(1);
}

console.log('P #210 发布中心按当前客户全部报价取文');

/* ── P1 分母:一个客户有几份报价 ─────────────────────────────────── */
{
    /* 夹具照真客户的形状:品牌 17 两份有效报价(94 paid / 378 confirmed)。
       另一个品牌混在同一份列表里 —— 反向对照,证它不会把别人的报价也算进来。 */
    const PROJECTS = [
        { id: 94, brand_id: 17, brand_name: 'QA 甲' },
        { id: 378, brand_id: 17, brand_name: 'QA 甲' },
        { id: 900, brand_id: 99, brand_name: 'QA 乙' },
    ];
    const ids = SCOPE.quoteIdsForBrand(PROJECTS, 17);
    ok(Array.isArray(ids) && ids.length === 2 && ids[0] === 94 && ids[1] === 378,
        'P1 🔴 一个客户的**全部**报价都取到了(病根就是只取了第一份)', JSON.stringify(ids));
    ok(!SCOPE.quoteIdsForBrand(PROJECTS, 17).includes(900),
        'P1b 反向对照:别的客户的报价**不会**混进来');
    ok(SCOPE.quoteIdsForBrand(PROJECTS, null).length === 0
        && SCOPE.quoteIdsForBrand(null, 17).length === 0,
        'P1c 没客户 / 没列表 ⇒ 空数组(不猜一个)');
    /*
     * 🔴 把病根本身钉下来:`pickProjectForBrand` **仍然只回一份**(它答的是
     *    "默认选中哪一份",没错)。两个函数答的是两个问题 —— 这一格存在的意义是:
     *    将来谁想"省事"把取文改回 `pickProjectForBrand`,这里会立刻说明白差别。
     */
    ok(SCOPE.pickProjectForBrand(PROJECTS, 17) === 94,
        'P1d 对照:`pickProjectForBrand` 只回第一份(它答的是"默认选中哪一份")');

    const st = SCOPE.scopeState({ isAllClientsMode: false, currentBrandId: 17, projects: PROJECTS });
    ok(st.kind === 'ready' && st.quoteIds.length === 2,
        'P1e `scopeState` 把全部报价带出来了(取文要用它)', JSON.stringify(st.quoteIds));
    for (const [label, input] of [
        ['全部客户模式', { isAllClientsMode: true, currentBrandId: null, projects: PROJECTS }],
        ['没选客户', { isAllClientsMode: false, currentBrandId: null, projects: PROJECTS }],
        ['该客户没有项目', { isAllClientsMode: false, currentBrandId: 12345, projects: PROJECTS }],
    ]) {
        const r = SCOPE.scopeState(input);
        ok(Array.isArray(r.quoteIds) && r.quoteIds.length === 0 && r.message.length > 0,
            `P1f 非 ready(${label}):报价集为空**且有一句话说下一步**`);
    }
}

/* ── P2 取文接线:用的是全部报价,不是选中那一份 ───────────────────── */
{
    const PC = decomment(rd('src/pages/Publishing/PublishCenter.tsx'));
    /*
     * 🔴 钉**被使用的值**:`clientScope.quoteIds` 必须真的喂进那次 `Promise.all`。
     *    只断言"文件里出现过 quoteIds"不行 —— 一个没人用的常量照样满足那种锚
     *    (#203 刚栽过:死代码满足了字面锚)。
     */
    ok(/const scopeQuoteIds = clientScope\.quoteIds\.length/.test(PC)
        && /const quoteIds: number\[\] = scopeQuoteIds;/.test(PC),
        'P2 🔴 取文用的是 `clientScope.quoteIds`(当前客户的全部报价)');
    ok(/Promise\.all\(quoteIds\.map\(qid =>/.test(PC),
        'P2b 逐份拉再合并(后端一次只认一个 quote_id)');
    ok(/__quoteId: qid/.test(PC) && /quoteId: a\.__quoteId/.test(PC),
        'P2c 🔴 每篇标上它来自哪一份报价 —— 取自**我们用哪个 id 拉的**,'
        + '不是回包里的字段(回包不一定带)');
    ok(/clientScope\.quoteIds\]\);/.test(PC),
        'P2d 报价集变了要重拉(切客户 ⇒ 列表跟着换)');
    /* 反向:老写法"只取选中那一份"不许再是主路径 */
    ok(!/const quoteIds: number\[\] = proj\?\.quote_ids \?\? \[selectedProject\];/.test(PC),
        'P2e 🔴 反向:`proj?.quote_ids ?? [selectedProject]` 已不再是取文的主路径');
}

/* ── P3 报价筛选:默认全部,且进「藏了几篇」那本账 ───────────────── */
{
    const A = (id, q) => ({ id, article_id: id, quoteId: q, publication_eligible: true });
    const ARTS = [A(1, 94), A(2, 94), A(3, 378), A(4, 378)];
    const all = PART.partitionArticles({ articles: ARTS });
    ok(all.unpublished.length === 4 && all.hidden.byQuote === 0,
        'P3 🔴 **默认看全部报价** ⇒ 两份报价各 2 篇 = 未分发 4 篇'
        + '(真客户那次就是默认只看了一份)', `未分发 ${all.unpublished.length}`);
    const one = PART.partitionArticles({ articles: ARTS, quoteFilter: 94 });
    ok(one.unpublished.length === 2 && one.hidden.byQuote === 2,
        'P3b 主动筛某一份 ⇒ 只看那一份,且**藏了几篇进账**', `未分发 ${one.unpublished.length}`);
    ok(one.hidden.total === one.hidden.byQuote + one.hidden.byPreselect
        + one.hidden.byOptimize + one.hidden.byCart,
        'P3c total 是四道之和(加一道不记账,「我的文章去哪了」那句话就开始撒谎)');
    ok(one.allArticles.length === 4,
        'P3d 反向:`allArticles` 仍是全集 —— 筛选收窄的是**看到的**,不是**有的**');
}

/* ── P4 界面:报价标签 + 筛选条 ───────────────────────────────────── */
{
    const PC = decomment(rd('src/pages/Publishing/PublishCenter.tsx'));
    ok(/data-testid="article-quote-tag"/.test(PC), 'P4 每篇带得出「报价 #N」标签');
    const tags = (PC.match(/<ArticleQuoteTag quoteId=\{a\.quoteId\}/g) || []).length;
    /* 🔴 [WO_273 · 2026-09-23 重锚] 原数 7 = 代发五个分档 + 自助 tab 两处(未代发 / 已代发)。
       浏览器插件自助发布整档退役,自助文章列表连同那两处一起删了 ⇒ 现役只剩代发五个分档,冻结数改 5。
       少一处仍然红(4 ≠ 5);有人把自助列表加回来也红(7 ≠ 5)。 */
    ok(tags === 5,
        `P4b 🔴 **每一行文章**都挂了(代发五个分档,共 ${tags} 处;自助两处随 WO_273 退役删除)——`
        + '漏一处那一档就看不出这篇属于谁,而它看起来仍然"正常"');
    ok(/data-testid="publish-quote-filter"/.test(PC) && /data-testid="publish-quote-chip"/.test(PC),
        'P4c 报价筛选条能被行为臂定位');
    ok(/clientScope\.quoteIds\.length > 1 && \(/.test(PC),
        'P4d 只有一份报价时不出这条筛选(对谁都没有信息量的控件不上屏)');
    ok(/setQuoteFilter\(null\); \}, \[currentBrandId\]\)/.test(PC),
        'P4e 🔴 切客户把筛选归零 —— 停在上一个客户的报价号上,列表会恒空,'
        + '而那看起来和"这个客户没有文章"一模一样');
    ok(!/<select/.test(PC.slice(PC.indexOf('publish-quote-filter') - 200,
        PC.indexOf('publish-quote-filter') + 1600)),
        'P4f 筛选条没用原生 `<select>`(#201 正在清它,别一边清一边加)');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过`);
    process.exit(1);
}
console.log('\n全部通过');
process.exit(0);
