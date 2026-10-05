#!/usr/bin/env node
/**
 * verify-script-env-assumptions.mjs
 *   —— 判据脚本不许假设「我这棵树上有的东西,别人树上也有」(2026-09-18)
 *
 * ## 它防的是什么
 *
 * 2026-09-18:我新写的两条真渲染臂直接 `mkdtempSync(node_modules/.cache/...)`。
 * **`node_modules/.cache` 是构建产物** —— 全新 `npm ci` 的树里根本不存在。
 * 我那棵树有(之前构建留下的),所以本机**恒绿**;复审在干净的合并树上跑,
 * ENOENT 崩成 **rc=1**,读作「有判据红了」,而真相是**门根本没跑起来**。
 *
 * 🔴 这和「浏览器门进 build 链」是同一个形状:
 *    **门只在它被写出来的那个环境里跑得动**,而它自己的三态兜不住这件事 ——
 *    因为崩在三态机制建立**之前**。
 *
 * 复审点名了两条。机械枚举出来是**三条**(第三条 `test-diagnosis-launch-viewport.mjs`
 * 是存量的)。**点名的实例不是缺陷类** —— 所以这条闸的分母是
 * `scripts/` 下**全部** `.mjs`,不是链内那几十个,也不是本班改过的那几个。
 *
 * ## 判据口径
 *
 * 对每个 `mkdtempSync(` 调用:如果它的目标里出现 `.cache` / `CACHE_ROOT`,
 * 那么在**它之前**必须有一次 `mkdirSync(..., { recursive: true })`。
 *
 * 🔴 为什么不是"文件里有没有 mkdirSync":那会被文件**后半段**某个无关的
 *    `mkdirSync` 满足 —— 顺序错了照样崩。**行号顺序在这里恰好就是执行顺序**
 *    (都在模块顶层),所以按位置判是成立的;真有人把它挪进函数里,
 *    下面 D2 的自证会先红。
 *
 * 三态退出:0 全绿 · 1 有脚本违规 · 3 门自己没跑成。
 */
import { readdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const DIR = join(ROOT, 'scripts');

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

/*
 * 🔴 **先剥注释再判。** 这条闸第一次跑就把**自己**判红了 —— 本文件上面那段
 *    说明注释里写着 `mkdtempSync(` 和 `.cache`。
 *    「某串出现过」的锚会被**解释它自己的注释**满足,这是本仓反复踩的形态
 *    (同一天 WO_218 的 E0 也栽在自己刚写的注释上)。
 * 🔴 剥注释带来**反方向**的风险:剥过头把真代码也吃掉 ⇒ 判据恒绿。
 *    所以下面 D1a / D1b 两个方向各配一格自证。
 */
const stripComments = (src) => src
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/^[ \t]*\/\/.*$/gm, '');

let files;
try {
    files = readdirSync(DIR).filter((f) => f.endsWith('.mjs')).sort();
} catch (err) { cannotRun('列不出 scripts/', err); }

console.log('=== 判据脚本的环境假设 ===\n');

ok(files.length >= 60,
    'D0 分母自证:真的扫到了一批脚本 —— 分母缩了的话下面每一格都会"全绿"',
    `${files.length} 个 .mjs`);

const offenders = [];
const seen = new Set();
let cacheUsers = 0;
let guarded = 0;
for (const f of files) {
    let src;
    try { src = stripComments(readFileSync(join(DIR, f), 'utf8')); } catch { continue; }
    /*
     * 🔴 形态要**跟到实参**,不能只匹配函数名后面一个左括号:
     *    本文件自己有一行 `/mkdtempSync…/` 的**正则字面量**(那是代码,剥注释剥不掉),
     *    只匹配名字+括号的话,这条闸会把自己判成违规 —— 第二跑就是这样。
     *    真调用的实参一定是 `join(...)` 或一个字符串;正则字面量后面跟的是 `/`。
     *    收紧形态比"把自己排除掉"好:**每一个排除项都是一处自陈的盲区。**
     */
    const m = /mkdtempSync\s*\(\s*join\s*\(/.exec(src);
    if (!m) continue;
    /* 目标是不是 node_modules/.cache 这一族 */
    const target = src.slice(m.index, m.index + 260);
    const head = src.slice(0, m.index);
    const touchesCache = target.includes('.cache') || target.includes('CACHE_ROOT')
        || (head.includes('.cache') && head.includes('CACHE_ROOT'));
    if (!touchesCache) continue;
    cacheUsers += 1;
    seen.add(f);
    /* 🔴 必须在 mkdtemp **之前**;写在后面等于没写 */
    if (/mkdirSync\s*\([^)]*recursive/s.test(head)) guarded += 1;
    else offenders.push(f);
}

ok(cacheUsers >= 3,
    'D1 分母自证:确实找到了若干"往 node_modules/.cache 里建临时目录"的脚本 —— '
    + '一个都没找到的话,说明我的形态假设错了(锚写错比违规为零更可能)',
    `${cacheUsers} 个(其中 ${guarded} 个事先建了目录)`);

/*
 * 🔴 **正样本:三个已知真调用必须被认出来。**
 *    上面的形态被收紧过两次(先是匹配到本文件的正则字面量,再是匹配到
 *    D1a 那一格**自己的说明文字**里的 `` mkdtempSync( ``)——
 *    每收紧一次,锚就有可能窄到把真调用也漏掉,而那会让 D2 **恒绿**。
 *    条数门槛(>= 3)防不住这个:它对"少认出几个"天生不敏感。
 *    所以点名钉住三个确定有真调用的文件。
 */
const MUST_SEE = [
    'test-topic-gen-charge-render.mjs',
    'test-tested-question-count-render.mjs',
    'test-diagnosis-launch-viewport.mjs',
];
const missed = MUST_SEE.filter((f) => !seen.has(f));
ok(missed.length === 0,
    'D1c 🔴 正样本:三个**确定**往 .cache 里建临时目录的脚本都被认出来了 —— '
    + '锚收窄到认不出它们时,D2 会恒绿(没有违规,因为谁都没被扫到)',
    missed.join(' / ') || `${MUST_SEE.length}/${MUST_SEE.length} 都认出来了`);

/* 剥注释这件事本身要两个方向都自证 —— 剥漏了会假红,剥过头会恒绿。 */
{
    const self = readFileSync(join(DIR, 'verify-script-env-assumptions.mjs'), 'utf8');
    const stripped = stripComments(self);
    ok(!/mkdtempSync\s*\(/.test(stripped.slice(0, stripped.indexOf('const offenders'))),
        'D1a 剥注释**够狠**:本文件说明注释里的 `mkdtempSync(` 已经被剥掉 —— '
        + '剥不干净的话这条闸会把解释它自己的注释判成违规(第一次跑就是这样)',
        '自己不再命中');
    ok(stripped.includes('readdirSync') && stripped.includes('offenders.push')
        && stripped.length > self.length * 0.3,
        'D1b 剥注释**没过头**:真代码还在(readdirSync / offenders.push 都还在,正文没被吃掉)—— '
        + '剥过头的话每一格都会恒绿',
        `剥后 ${stripped.length} / 原 ${self.length} 字节`);
}

ok(offenders.length === 0,
    'D2 🔴 每一个 mkdtemp 进 `node_modules/.cache` 的脚本都**先建了目录** —— '
    + '那是构建产物,干净的 `npm ci` 树里不存在;不建就在别人树上 ENOENT 崩成 rc=1,'
    + '读作"判据红了",而真相是**门根本没跑起来**',
    offenders.join(' / ') || '零个违规');

console.log('');
console.log('🔴 这条闸的分母是 scripts/ 下**全部** .mjs,不是 build 链里那几十个 ——');
console.log('   坏掉的那三条恰好都在链外,按链内分母扫的话一条都看不见。');
console.log('');
if (failures > 0) { console.log(`FAIL ${failures}/${ran} 项不通过`); process.exit(1); }
console.log(`PASS ${ran}/${ran} 项通过`);
process.exit(0);
