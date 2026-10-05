#!/usr/bin/env node
/**
 * 判据 · `container-type` 与 `display:contents` 的结构冲突(#125 2026-09-06)。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 为什么要有这把锁 —— 它守的是一类**不产生任何错误信号**的缺陷:
 *    元素同时带 `container-type:inline-size` 和 `display:contents` ⇒ 它不生成盒
 *    ⇒ 尺寸不可解析 ⇒ **子树里所有容器查询求值为假**,且**不回落到外层容器**。
 *    后果是子项上的 窄档 order 变体(order-1…order-6,带 @max- 前缀) 计算值恒为 0:
 *      · 不报错 · 不告警 · 不进日志 · 类**确实**编译进了产物
 *    只是运行时求假。#125 第一版就是这样活到线上的,靠人肉量 DOM 才发现。
 *    ⇒ 注释拦不住它,只有门拦得住。
 *
 * 🔴 机理不是推测:Deploy 2026-09-06 四格实验(含 E 格复原对照,证明无残留)
 *      inline-size + contents → order 0 ❌
 *      normal      + contents → order 1 ✅
 *      inline-size + block    → order 1 ✅
 *      normal      + block    → order 1 ✅
 *    ⇒ 两者同时存在才出事,改掉任一个都恢复。
 *
 * 🔴 判定面是 **className 表达式**,不是整段源码 —— 本文件的注释里就逐字写着
 *    contents 那个词,拿裸串扫全文会被自己的散文判红(这个坑今年踩过三次)。
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

// ── 工具:剥注释 · 取 className 表达式 ────────────────────────────────
function stripComments(src) {
    let out = '';
    let i = 0;
    while (i < src.length) {
        const two = src.slice(i, i + 2);
        if (two === '/*') { const e = src.indexOf('*/', i + 2); i = e < 0 ? src.length : e + 2; continue; }
        if (two === '//') { const e = src.indexOf('\n', i); i = e < 0 ? src.length : e; continue; }
        out += src[i]; i += 1;
    }
    return out;
}

/** 取每个 JSX 元素的 className 表达式;`{cn(...)}` 里的多个字符串字面量**合并成一份**
 *  —— CTA 的类就是分行写的,分开算会把「同一个元素上的两个类」看成两个元素。 */
function classNameUnits(src) {
    const units = [];
    const KEY = 'className=';
    let i = 0;
    while (true) {
        const k = src.indexOf(KEY, i);
        if (k < 0) break;
        const j = k + KEY.length;
        const c0 = src[j];
        if (c0 === '"' || c0 === String.fromCharCode(39)) {
            const e = src.indexOf(c0, j + 1);
            if (e > 0) { units.push(src.slice(j + 1, e)); i = e + 1; continue; }
            i = j + 1; continue;
        }
        if (c0 === '{') {
            let depth = 0;
            let e = j;
            for (; e < src.length; e += 1) {
                if (src[e] === '{') depth += 1;
                else if (src[e] === '}') { depth -= 1; if (depth === 0) break; }
            }
            const span = src.slice(j, e + 1);
            const lits = [];
            let p = 0;
            while (p < span.length) {
                const c = span[p];
                if (c === '"' || c === String.fromCharCode(39)) {
                    const e2 = span.indexOf(c, p + 1);
                    if (e2 < 0) break;
                    lits.push(span.slice(p + 1, e2));
                    p = e2 + 1; continue;
                }
                p += 1;
            }
            units.push(lits.join(' '));
            i = e + 1; continue;
        }
        i = j;
    }
    return units;
}

const tokens = (u) => u.split(' ').map((t) => t.trim()).filter(Boolean);
/** 裸 `@container`(不含 `@container/name` —— 命名容器不参与就近解析,不构成本冲突)。 */
const hasBareContainer = (u) => tokens(u).some((t) => t === '@container');
/** 任何变体下的 contents(`sm:contents` / `@max-[880px]:contents` / 裸 contents 都算)。 */
const hasContents = (u) => tokens(u).some((t) => t === 'contents' || t.endsWith(':contents'));
const isConflict = (u) => hasBareContainer(u) && hasContents(u);

function walk(dir, acc) {
    for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        if (statSync(p).isDirectory()) { walk(p, acc); continue; }
        if (name.endsWith('.tsx')) acc.push(p);
    }
    return acc;
}

// ── A 全树普查 ───────────────────────────────────────────────────────
console.log('A 全树普查:同一元素不许同时带裸 @container 与任何 contents 变体');
const files = walk(SRC, []);
let unitCount = 0;
let containerSites = 0;
let contentsSites = 0;
const conflicts = [];
for (const f of files) {
    const units = classNameUnits(stripComments(readFileSync(f, 'utf8')));
    unitCount += units.length;
    for (const u of units) {
        if (hasBareContainer(u)) containerSites += 1;
        if (hasContents(u)) contentsSites += 1;
        if (isConflict(u)) conflicts.push(`${relative(ROOT, f)} :: ${u.slice(0, 90)}`);
    }
}
ok(files.length >= 50 && unitCount >= 300,
    `A0 分母自证:扫到 ${files.length} 个 tsx / ${unitCount} 个 className 表达式(取样器坏了会读成 0)`);
ok(containerSites >= 1 && contentsSites >= 1,
    `A0b 分母自证:裸 @container ${containerSites} 处 · contents ${contentsSites} 处 —— 两边都得有,否则 A1 的「零冲突」是空的`);
ok(conflicts.length === 0, 'A1 🔴 零冲突', conflicts.join(' | ') || '0 处');
ok(isConflict('@container @max-[880px]:contents'),
    'A2 正样本臂:判据认得出合成的冲突串(不认 ⇒ A1 恒真)');
ok(!isConflict('@container w-full') && !isConflict('hidden sm:contents'),
    'A3 反向对照:单独一边都不算冲突');
ok(!isConflict('@container/page @max-[880px]:contents'),
    'A4 反向对照:**命名**容器 @container/page 不算裸 @container(它不参与就近解析)');
ok(isConflict('a @container b contents c'),
    'A5 正样本臂:裸 contents(无变体)同样算 —— 它在任何断点都不生成盒');

// ── B 站点锁:launch-main-col ─────────────────────────────────────────
console.log('B 站点锁:launch-main-col 的容器身份只在宽档');
const PAGE = stripComments(readFileSync(join(SRC, 'pages/Diagnosis/NewDiagnosis.tsx'), 'utf8'));
const PAGE_UNITS = classNameUnits(PAGE);
const mainCol = PAGE_UNITS.find((u) => u.includes('@max-[880px]:contents') && u.includes('space-y-5'));
if (!mainCol) {
    bad += 1;
    console.log('  FAIL B0 没找到 launch-main-col 的 className —— 锚失效,B1-B3 不算通过');
} else {
    ok(mainCol.includes('@max-[880px]:contents'), 'B1 窄档 contents(重排要它)');
    ok(mainCol.includes('@min-[880px]:[container-type:inline-size]'),
        'B2 🔴 容器身份**只在宽档**(卡片按主列宽排,不按整页宽排)');
    ok(!hasBareContainer(mainCol),
        'B3 🔴 不许退回裸 @container —— 那会让窄档三条 order 静默失效', mainCol.slice(0, 80));
}

// ── C 接线:六个 order 值都还在 ───────────────────────────────────────
console.log('C 接线:窄档六块的 order 都在(顺手删掉不许静默通过)');
// 拼出来,别让这行自己变成一个「新的容器查询变体」被底线闸盘点到(今天第 4 次)
const PFX = '@max-[880px]:' + 'order-';
const orderTokens = PAGE_UNITS.flatMap(tokens).filter((t) => t.startsWith(PFX));
const orderVals = [...new Set(orderTokens.map((t) => t.slice(PFX.length)))].sort();
ok(orderVals.join(',') === '1,2,3,4,5,6', 'C1 六个 order 值齐全',
    `实得 ${orderVals.join(',') || '(空)'} · 共 ${orderTokens.length} 处`);

// ── D 产物锁 ─────────────────────────────────────────────────────────
console.log('D 产物锁:那两条真的编译进了 CSS');
const distDir = join(ROOT, 'dist', 'assets');
if (!existsSync(distDir)) {
    notEvaluated += 1;
    console.log('  SKIP D0 **未构建 ⇒ 未评估**:没有 dist,产物锚没验(不是通过)。');
} else {
    const css = readdirSync(distDir).filter((f) => f.endsWith('.css'))
        .map((f) => readFileSync(join(distDir, f), 'utf8')).join('\n');
    ok(css.length > 10000, `D0b 分母自证:合并 CSS ${css.length} 字节(读成 0 会让下面全绿)`);
    const wide = css.indexOf('@container (min-width:880px)');
    ok(wide >= 0 && css.slice(wide, wide + 400).includes('container-type:inline-size'),
        'D1 🔴 宽档容器身份编译进产物(arbitrary property 没编译出来会静默丢失)');
    ok(css.includes('@container not (min-width:880px)'),
        'D2 窄档那组 order 走的是 max 方向的容器查询');
    ok(!css.includes('@container (min-width:99999px)'),
        'D3 反臂:编造的锚必须不命中');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exit(1);
}
if (notEvaluated > 0) {
    console.log(`\n未完成:${notEvaluated} 项未评估`);
    process.exit(3);
}
console.log('\n全部通过');
process.exit(0);
