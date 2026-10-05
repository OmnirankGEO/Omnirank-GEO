#!/usr/bin/env node
/**
 * 判据 · #225 a1 写作中心的「槽」与「篇」(纯函数真调 + 接线臂)。
 *
 * Review 09-15 定的口径:
 *   · 量词**不改**成「条」—— 写作大厅写的是文章,计划数与已产出数都是「篇」;
 *   · 要区分的是**合同槽**:凡显示 `required_articles` 的地方改读
 *     `planned_posts_default`(仍叫「篇」),旁边写「授权 K 槽」;
 *   · 同一屏「槽」只指合同分配,「篇」只指要做/做了的文章。
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

/* ── 真调那只零 import 模块 ─────────────────────────────────────── */
const require_ = createRequire(import.meta.url);
const cacheRoot = join(ROOT, 'node_modules', '.cache');
mkdirSync(cacheRoot, { recursive: true });
const tmp = mkdtempSync(join(cacheRoot, 'a225-'));
process.on('exit', () => { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 尽力 */ } });
let M = null;
try {
    const ts = require_('typescript');
    const js = ts.transpileModule(rd('src/pages/Writing/writingCounts.ts'), {
        compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 },
    }).outputText;
    const f = join(tmp, 'writingCounts.mjs');
    writeFileSync(f, js, 'utf8');
    M = await import(`file://${f.split('\\').join('/')}`);
} catch (e) {
    console.log('FAIL 判据不可用(不当绿灯):取不到那只纯模块 —— '
        + String((e && e.message) || e).split('\n')[0]);
    process.exit(1);
}

console.log('W1 槽:老语义逐字复刻(NULL ⇒ 1,显式 0 保留)');
{
    ok(M.slots({ required_articles: 3 }) === 3, 'W1a 正常值');
    ok(M.slots({ required_articles: null }) === 1,
        'W1b 🔴 NULL ⇒ **1**,不是 0 —— 后端 `_required_article_count` 就是这条规则,'
        + '两边不一致的话 NULL 那一行会从 1 篇变 0 篇', String(M.slots({ required_articles: null })));
    ok(M.slots({}) === 1, 'W1c 字段缺失同 NULL');
    ok(M.slots({ required_articles: 0 }) === 0,
        'W1d 🔴 **显式 0 保留** —— 把它也当成 1 会凭空多派一篇的活');
    ok(M.slots(null) === 1 && M.slots(undefined) === 1, 'W1e 边界不抛');
}

console.log('\nW2 篇:读服务端给的值,前端不算');
{
    /*
     * 🔴 夹具数字**故意挑成"任何倍数都算不出来"**的:槽 2 / 篇 7。
     *    上一版写的是槽 2 / 篇 10 —— 而注毒 T4 把读值换成「槽 × 5」,
     *    2 × 5 也是 10,**错的算法算出了对的数**,这一格照样绿。
     *    夹具里的巧合会把缺陷藏起来。
     */
    ok(M.plannedPosts({ required_articles: 2, planned_posts_default: 7 }) === 7,
        'W2a 🔴 有服务端值就用它(**不是** 槽 × 倍数 前端自己算;'
        + '夹具 2→7 不是任何整数倍,算的话对不上)',
        String(M.plannedPosts({ required_articles: 2, planned_posts_default: 7 })));
    /*
     * 🔴 W2a2 第二组:**同槽不同值**。
     *    单组夹具挡不住"按槽编一个数"的实现 —— Review 09-15 的 P2c
     *    (`p===0?0:(p===槽?p:trunc(槽×3.6))`)能 18/18 全绿。
     *    同一个槽数给出两个不同答案,只吃槽的函数就过不了。
     */
    ok(M.plannedPosts({ required_articles: 2, planned_posts_default: 9 }) === 9,
        'W2a2 🔴 同槽(2)不同值(9)—— 只吃槽的实现给不出两个答案',
        String(M.plannedPosts({ required_articles: 2, planned_posts_default: 9 })));
    ok(M.plannedPosts({ required_articles: 3 }) === 3,
        'W2b 服务端没给 ⇒ 回落槽数 = 225 之前的老行为(口径读不到 / mixed 时后端也回落 k=1)');
    ok(M.plannedPosts({ required_articles: null, planned_posts_default: null }) === 1,
        'W2c 两个都没有 ⇒ 仍是老语义的 1');
    ok(M.plannedPosts({ required_articles: 2, planned_posts_default: 0 }) === 0,
        'W2d 服务端显式 0 保留(不回落)');
    /*
     * 🔴 反臂:这只模块里**不许**出现换算算术。
     *    换算带 round_half_up,前端自己算一遍就会和媒体组合那屏对不上 ——
     *    那边逐桶取整,这边会变成逐词取整,同一张报价两个数各自看着都对、并排放着矛盾。
     */
    const SRC = decomment(rd('src/pages/Writing/writingCounts.ts'));
    ok(!/10000|bps|round|\*\s*k\b/i.test(SRC),
        'W2e 🔴 反臂:这只模块里没有任何换算算术(bps / 10000 / round)—— '
        + '换算只许在服务端做一次');
}

console.log('\nW2x 合计:服务端给就用它,不前端 reduce');
{
    const rows = [
        { required_articles: 2, planned_posts_default: 7 },
        { required_articles: 3, planned_posts_default: 11 },
    ];
    ok(M.plannedTotal(20, rows) === 20,
        'W2f 🔴 服务端给了合计就**用它** —— 前端自己加一遍,取整位置一变就会和别处差 1,'
        + '而没有任何东西会报错', String(M.plannedTotal(20, rows)));
    ok(M.plannedTotal(null, rows) === 18,
        'W2g 服务端没给 ⇒ 逐行相加(加的是**已取整**的每行值,不引入新取整)',
        String(M.plannedTotal(null, rows)));
    ok(M.plannedTotal(undefined, []) === 0 && M.plannedTotal(null, null) === 0,
        'W2h 边界不抛');
    /* 🔴 反臂:服务端合计与逐行之和**不等**时,以服务端为准(这正是 W2f 要挡的那件事) */
    ok(M.plannedTotal(99, rows) === 99,
        'W2i 🔴 两者不等时用服务端的 —— 不许"看着不对就自己算一遍"');
}

console.log('\nW3 「授权 N 槽」只在两个数不一样时才说');
{
    ok(M.slotsNote({ required_articles: 2, planned_posts_default: 10 }) === '授权 2 槽',
        'W3a 不一样 ⇒ 说明出现', M.slotsNote({ required_articles: 2, planned_posts_default: 10 }));
    ok(M.slotsNote({ required_articles: 3, planned_posts_default: 3 }) === '',
        'W3b 🔴 一样 ⇒ **不说** —— 说一遍等于把同一个数换个量词又讲一次,'
        + '屏幕上凭空多出一个要读者去比对的东西');
    ok(M.slotsNote({ required_articles: 3 }) === '',
        'W3c 服务端没给时也不说(回落后两个数相等)');
}

console.log('\nW4 接线:两处真的用上了,且量词没被改成「条」');
{
    const HALL = decomment(rd('src/pages/Writing/WritingHall.tsx'));
    const uses = (HALL.match(/plannedPosts\(kw\)/g) || []).length;
    /*
     * 🔴 这一格原来钉的是「恰好 2 处」—— 那是个**数字**,不是命题。
     *    下半合法地加了第三处(填色阈值 W4f),它就红了。
     *    名字说的是「都用上了纯函数」,实际查的却是「恰好两处」:名实不符。
     *    ⇒ 这里只证"纯函数真的被接上了"(≥2);
     *      「不许在别处各写一遍」那件事的牙在 W4b(不直接显示槽)与 W4f(阈值也比篇)。
     */
    ok(uses >= 2, 'W4a 纯函数真的被接上了(牙在 W4b / W4f)', `${uses} 处`);
    ok(!/需\{kw\.required_articles/.test(HALL),
        'W4b 🔴 那两处不再直接显示**槽**数(它是合同分配,不是要做几篇)');
    ok((HALL.match(/slotsNote\(kw\)/g) || []).length >= 2,
        'W4c 「授权 N 槽」两处都挂上了');
    /*
     * 🔴 W4d:紧挨着的那一对必须**同量词**。
     *    `:7575` 需 N 篇 / `:7578` 已生成 M 篇 —— 只改一半会变成
     *    「需 5 条 / 已生成 3 篇」,读起来像两件不同的东西,比两个都不改更糟。
     */
    ok(/已生成\{kwTopics\.length\}篇/.test(HALL),
        'W4d 🔴 紧挨着的「已生成 M **篇**」原样没动 —— 一对数必须同量词');
    ok(!/需\{plannedPosts\(kw\)\}条/.test(HALL),
        'W4e 反臂:没有把计划数改成「条」(Review 09-15:写作大厅统一用「篇」,'
        + '「条」留给图文面板)');
    /*
     * 🔴 W4f:**填色阈值**也要用「篇」。
     *    上半只改了标签,紧挨着的这个比较还在拿槽数比 ——
     *    需 10 篇、生成 2 篇,徽章就填成「够了」。
     *    改标签不改阈值 = 同一屏上说一套、算一套。
     */
    ok(/kwTopics\.length >= plannedPosts\(kw\)/.test(HALL)
        && !/kwTopics\.length >= kw\.required_articles/.test(HALL),
        'W4f 🔴 「已生成」徽章的填色阈值比的是**篇**(不是槽)');
    ok(/plannedTotal\(plannedTotalServer, keywords\)/.test(HALL)
        && !/keywords\.reduce\(\(sum, kw\) => sum \+ \(Number\(kw\.required_articles\)/.test(HALL),
        'W4g 🔴 表头合计读服务端字段,前端不 reduce 槽数');
    ok(/total_required_articles \|\| '-'\}槽/.test(HALL),
        'W4h 🔴 项目卡那个数标的是**槽**(它来自列表端点的 total_required_articles,'
        + '是合同授权数;原来标着「篇」就是本单要消灭的错标)');
    ok(/授权槽数/.test(HALL) && !/发布篇数/.test(HALL),
        'W4i 🔴 添加关键词的输入框标「授权槽数」—— 它绑的是 required_articles');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
