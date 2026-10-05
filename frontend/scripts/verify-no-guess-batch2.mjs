#!/usr/bin/env node
/**
 * 判据 · #200 「禁猜」存量清理第二批(结构 + 纯文本臂)。
 *
 * 两处:
 *   1.1 监测词表「更多 4 列」不说是哪四列(第 11 条第 3 款);
 *   1.2 体检页三档只解释选中那一档(第 1/3 款)。
 *
 * 两态退出码:**0 全过 / 1 有失败**。
 * 🔴 本闸在 build 链里,而链是 `&&` 串的 —— rc=3 会让后面的闸根本不跑(#199 a2 栽过)。
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

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

const KT_RAW = rd('src/pages/Monitoring/components/KeywordTable.tsx');
const KT = decomment(KT_RAW);
const TABS_RAW = rd('src/components/defensiveGeo/ModeTabs.tsx');
const TABS = decomment(TABS_RAW);
const CARDS = rd('src/components/defensiveGeo/ModeRadioCards.tsx');

console.log('M #200 禁猜清理第二批');

/* ══ M1 词表「更多 4 列」:列名上屏 + 单源 ═══════════════════════════ */
{
    /*
     * 🔴 改前这四个名字有**三个源**:四个 `<TableHead>` 里各写死一份、
     *    toggle 的 `title` 手抄一份、文件顶部注释又抄一份 ——
     *    而注释那份**已经抄错了**(写「服务期至」,真表头是「已服务」)。
     *    三个源里错了一个还全绿,正是因为没人把它们比过。
     */
    const names = ['来源', '变化', '倒计时', '已服务'];
    ok(/const TERTIARY_COLS = \{/.test(KT) && /source: '来源'/.test(KT),
        'M1a 四个列名有**唯一**来源 `TERTIARY_COLS`');
    ok(/const TERTIARY_COL_NAMES[^\n]*Object\.values\(TERTIARY_COLS\)/.test(KT),
        'M1a2 名字列表从它派生,不是另写一份');
    ok(/const TERTIARY_COLS_COUNT = TERTIARY_COL_NAMES\.length;/.test(KT),
        'M1a3 🔴 数量也**派生** —— 另写一个字面量 4,加一列时按钮会写「更多 4 列」却列出 5 个名字');
    /* 表头必须从常量取,不许再写死 */
    const headHardcoded = names.filter(
        (n) => new RegExp(`<TableHead[^>]*>${n}</TableHead>`).test(KT));
    ok(headHardcoded.length === 0,
        'M1b 🔴 四个表头都从常量渲染(写死就还是两个源)',
        headHardcoded.join(',') || '零处写死');
    const headFromConst = names.filter((_n, i) => new RegExp(
        `\\{TERTIARY_COLS\\.(source|change|countdown|served)\\}`).test(KT) || i < 0);
    ok((KT.match(/\{TERTIARY_COLS\.[a-z]+\}/g) || []).length === 4,
        'M1b2 恰好四处表头引用了常量(少一处就是漏改)',
        `${(KT.match(/\{TERTIARY_COLS\.[a-z]+\}/g) || []).length} 处 · ${headFromConst.length ? '' : ''}`);
    /* 可见文案 */
    ok(/更多 \$\{TERTIARY_COLS_COUNT\} 列 · \$\{TERTIARY_COL_LIST\}/.test(KT),
        'M1c 🔴 折叠态**可见文案**里就有四个列名 —— '
        + '原来只在 `title` 里,而手机上根本没有 hover');
    ok(/收起 \$\{TERTIARY_COLS_COUNT\} 列`/.test(KT),
        'M1c2 展开态不再列名字(列头已经在屏幕上,再列一遍是同一件事说两遍)');
    /*
     * 🔴 [订正] M1d 第一版写成「源码里出现过 `列详情(${TERTIARY_COL_LIST})`」。
     *    而 `title` 是个**三元**,收起/展开两支各带一次 —— 毒 Q4 只把「展开」那支
     *    换成手抄的名字,**另一支替它满足了锚** ⇒ 毒落地却全绿。
     *    今天第五次「锚被隔壁满足」,而且是在我自己新写的判据里。
     *    ⇒ 改成问**整个 `title` 表达式**:它必须引用常量,且四个列名
     *      **一个字面量都不许出现**(哪一支手抄都算)。
     */
    const tStart = KT.indexOf('title={showTertiaryCols');
    const titleExpr = tStart >= 0 ? KT.slice(tStart, KT.indexOf(String.fromCharCode(10), tStart)) : '';
    ok(titleExpr.length > 0 && titleExpr.length < 300,
        'M1d0 取窗自证:切出来的是 `title` 那一整条表达式(切空/切太大都说明锚错了)',
        `${titleExpr.length} 字符`);
    ok(titleExpr.includes('TERTIARY_COL_LIST'),
        'M1d `title` 里引用的是常量(与可见文案同源,改常量两处同变)');
    const copied = names.filter((n) => titleExpr.includes(n));
    ok(copied.length === 0,
        'M1d2 🔴 `title` 的**任何一支**都不许手抄列名 —— '
        + '抄一支就又是两个源,而另一支会替它满足"用了常量"这个锚',
        copied.join(',') || '两支都没抄');
    /* 正样本臂:真有手抄时检得出来(否则 M1d2 是空断言) */
    const fake = 'title={a ? `收起 4 列详情(${TERTIARY_COL_LIST})` : `展开 4 列详情(来源/变化/倒计时/已服务)`}';
    ok(names.filter((n) => fake.includes(n)).length === 4,
        'M1d3 正样本臂:一支手抄时**检得出来**');
    /* 顶部那句抄错的注释 */
    /*
     * 🔴 这一格第一版写成"全文件不许出现那个错名字" —— **是我自己写错的判据**:
     *    那个词在本文件里有**合法的别处用法**(合同到期字段、导出表头的一列),
     *    于是它红了,而产品没有任何问题。**假红也是仪器缺陷。**
     *    ⇒ 只取「tertiary 分级」那一段注释来判:它现在应当指向常量、不再自己列名字。
     */
    const tail = KT_RAW.slice(KT_RAW.indexOf('tertiary 默认隐藏'));
    const seg = tail.slice(0, tail.indexOf('*/') + 2);
    ok(seg.length > 0 && seg.length < 400,
        'M1e0 取窗自证:切出来的是那一段注释本身', `${seg.length} 字符`);
    ok(seg.includes('TERTIARY_COLS'),
        'M1e 🔴 那段注释现在**指向常量**,不再自己抄一份列名 —— '
        + '它抄的那份当初就抄错了一个,而错注释不会红');
    const wrong = ['服务期至'].filter((w) => seg.includes(w));
    ok(wrong.length === 0, 'M1e2 那段注释里不再有抄错的列名', wrong.join(',') || '无');
    ok(/data-testid="keyword-tertiary-toggle"/.test(KT), 'M1f 开关能被行为臂定位');
}

/* ══ M2 体检页三档说明同屏 ═══════════════════════════════════════════ */
{
    ok(/data-testid="launch-mode-explainer-grid"/.test(TABS),
        'M2a 三句说明有自己的容器锚');
    /* 🔴 三句是**一起渲染**的:只渲染选中那一档时,`options.map` 只会出现一次(标签那次) */
    const maps = (TABS.match(/options\.map\(/g) || []).length;
    ok(maps === 2,
        `M2a2 🔴 \`options.map\` 恰好两处(标签一处 + 说明一处)—— `
        + `只渲染选中那一档的话只有一处`, `${maps} 处`);
    ok(/\{o\.explainer\}/.test(TABS) && !/\{active\.explainer\}/.test(TABS),
        'M2b 🔴 渲染的是**每一档自己**的说明(`o.explainer`),不是只渲染 `active.explainer`');
    ok(/data-selected=\{selected \? 'true' : 'false'\}/.test(TABS),
        'M2c 每一句标出自己是不是选中态(行为臂与读屏都要能分辨)');
    ok(/\{TAB_LABEL\[o\.mode\]\}/.test(TABS)
        && (TABS.match(/\{TAB_LABEL\[o\.mode\]\}/g) || []).length === 2,
        'M2d 🔴 句首的标签名取自 `TAB_LABEL`(标签那处一份、说明这处一份,同一个源)'
        + ' —— 窄档三句纵排时,"哪句对哪档"只能靠它');
    /*
     * ══ M2e 🔴 文案单源锁 —— **从三态闸搬进 build 链** ═════════════════
     *
     * `verify-pkg1-mode-tabs.mjs` 的 B1 早就钉了这件事(说明不许抄进 ModeTabs),
     * 但那把闸是**三态**的(有 rc=3 的路径),因此**不在 build 链里** ——
     * 也就是说这把锁从来没在收集范围内跑过。
     * ⇒ 等价断言搬到这里(两态),链内每次构建都过一遍。
     * 查的是**原文**(含注释):抄进注释也算抄,改文案时那份不会跟着变。
     */
    const EXPLAINERS = [...CARDS.matchAll(/explainer:\s*\n?\s*'([^']+)'/g)].map((m) => m[1]);
    ok(EXPLAINERS.length === 3,
        `M2e0 正样本臂:从 ModeRadioCards 解析出 ${EXPLAINERS.length} 句说明`
        + '(解析不出来的话,下面"没抄"的判定不携带信息)');
    if (EXPLAINERS.length === 3) {
        const copied = EXPLAINERS.filter((t) => TABS_RAW.includes(t));
        ok(copied.length === 0,
            `M2e 🔴 说明文案**没有**被抄进 ModeTabs(抄了 ${copied.length} 句)`
            + ' —— 同一段话两个源,改一处另一处不跟,而两处各自看起来都对');
    }
    ok(/modeOptions\(\)/.test(TABS), 'M2e2 说明取自 `modeOptions()`');
    /* 可访问结构保留 */
    ok((TABS.match(/role="tabpanel"/g) || []).length === 1
        && /id=\{selected \? 'launch-mode-explainer'/.test(TABS),
        'M2f `role=tabpanel` 结构保留(源码里一处、三句各渲染一个),'
        + '且选中那句沿用老 id `launch-mode-explainer`(别处按它定位)');
    ok(/aria-controls=\{selected \? 'launch-mode-explainer'/.test(TABS),
        'M2f2 每个标签 `aria-controls` 指向**自己那一句**');
    /* 窄档纵排 */
    ok(/@container/.test(TABS) && /@min-\[560px\]:grid-cols-3/.test(TABS),
        'M2g 宽够三列、窄了纵排;断点问**本组件自己那一栏**有多宽,不问页面');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过`);
    process.exit(1);
}
console.log('\n全部通过');
process.exit(0);
