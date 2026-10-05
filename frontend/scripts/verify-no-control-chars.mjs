#!/usr/bin/env node
/**
 * 闸 · 源码里不许有**看不见的控制字符**。
 *
 * ## 为什么要有这把闸(2026-09-13 · #195 当场扫出四处)
 *
 * 我多次用脚本改写判据文件,正则里的 `\b`(词边界)被**吃成了退格字符 0x08**:
 *
 *   `ok(!/ImageNotePanel\b/.test(pc), 'G5b 回来的不是那个旧壳')`
 *        ↓ 一次改写之后
 *   `ok(!/ImageNotePanel<0x08>/.test(pc), 'G5b 回来的不是那个旧壳')`
 *
 * 那个正则**永远匹配不上**,于是 `!/…/` 这条否定断言**恒真、恒绿**。
 * 屏幕上它照样打印 `OK G5b …`,而它守的那件事从此没人守。实测同族四处:
 *   · `verify-image-note-studio.mjs` B2(「503 不许说成 0 算力」)—— 半边失效
 *   · `verify-image-note-studio.mjs` G5b(「不许是那个 598 行旧壳」)—— 整条失效
 *   · `verify-pay-exit.mjs` S9(「hook 不许整包 `as T`」)—— 半边失效
 *   · `test-header-latin1.mjs` 里一个裸 NUL(语义碰巧对,但源码里看不见)
 *
 * 🔴 这一类坏法的要害是**没有任何东西会报警**:tsc 不管、eslint 不管,
 *    判据自己更不会说"我这条已经恒真了"。所以它必须变成一把**闸**,
 *    而不是注释里的提醒(注释传不出去,门才传得出去)。
 *
 * 🔴 配套习惯:判据里**别用 `\b`**。要边界就用显式字符类 / 前后瞻 / 干脆
 *    `includes` 精确子串;并给关键正则加一组**正负样本控制** ——
 *    正则被改坏成"永不匹配"时,那条控制会先红。
 *
 * 跑法:cd frontend && node scripts/verify-no-control-chars.mjs
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative, extname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const DIRS = ['src', 'scripts'];
const EXT = new Set(['.ts', '.tsx', '.mjs', '.js', '.cjs']);

let bad = 0;
const ok = (cond, label, detail) => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

/** 允许的控制字符:换行与制表。**回车不在内**(见下面的冻结名单)。 */
const LF = String.fromCharCode(10);
const TAB = String.fromCharCode(9);
const CR_CODE = 13;
const ALLOWED = new Set([LF, TAB]);

/**
 * 🔴 **CRLF 的冻结名单**。零容忍那一档是「退格 / NUL 这类**判据杀手**」;
 *    回车是另一回事:本仓已经有一个历史 CRLF 文件,把它改成 LF 会产生一整屏
 *    与本单无关的 diff(而且它属于 M3 那一摊)。
 *    所以:名单里的照旧,**名单外新增 CRLF 一律红** —— 冻结名单把
 *    「本来就这样」和「这次弄进来的」分开,而不是把整条闸关掉。
 *
 * 🔴 名单只许变短。要变长时先问:这个文件为什么会是 CRLF?
 *    (同族记录:windows-newline-poisons-any-rewrite-tool-2026-08-13 ——
 *     同一段文本在两种换行下**不相等**,替换锚命中 0 次却看起来像"改过了")
 */
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 唯一一条(M3 收入页,整文件 CRLF 510 处)随 pages/M3 整删 ⇒ 名单清零。
//   名单外新增 CRLF 照旧一律红。
const CRLF_BASELINE = {};

const offenders = (text) => {
    const out = [];
    for (let i = 0; i < text.length; i += 1) {
        const c = text[i];
        if (c.charCodeAt(0) < 32 && !ALLOWED.has(c)) {
            out.push({ line: text.slice(0, i).split(LF).length, code: c.charCodeAt(0) });
        }
    }
    return out;
};

function walk(dir, out = []) {
    for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        const st = statSync(p);
        if (st.isDirectory()) walk(p, out);
        else if (EXT.has(extname(name))) out.push(p);
    }
    return out;
}

console.log('闸 · 源码无隐形控制字符(#195 扫出四处失效判据之后建的)');

/*
 * 🔴 判据自证:探测器自己得先能抓到、又不误伤。
 *    少了这一条,`offenders` 哪天被改坏成恒空,本闸就变成一句"全绿"的空话
 *    —— 那正是它要防的病本身。
 */
{
    const BS = String.fromCharCode(8);
    const NUL = String.fromCharCode(0);
    const CR = String.fromCharCode(13);
    const hitBS = offenders(`const re = /x${BS}/;`);
    const hitNUL = offenders(`a${NUL}b`);
    const hitCR = offenders(`a${CR}${LF}b`);
    const clean = offenders(`line1${LF}${TAB}line2 中文 \u00ff${LF}`);
    ok(hitBS.length === 1 && hitBS[0].code === 8, 'C0a 自证:抓得到退格 0x08');
    ok(hitNUL.length === 1 && hitNUL[0].code === 0, 'C0b 自证:抓得到 NUL');
    ok(hitCR.length === 1 && hitCR[0].code === CR_CODE, 'C0c 自证:抓得到回车');
    ok(clean.length === 0, 'C0d 自证:换行/制表/中文/Latin-1 高位**不误报**');
}

const files = DIRS.flatMap((d) => walk(join(ROOT, d)));
ok(files.length > 100, 'C0e 分母自证:真的扫到了一堆文件(扫空的话下面恒绿)',
    `${files.length} 个`);

const killers = [];            // 退格 / NUL 之类:零容忍
const crNew = [];              // 冻结名单**之外**的 CRLF
const crBaselineSeen = new Set();
for (const f of files) {
    const rel = relative(ROOT, f).split('\\').join('/');
    const list = offenders(readFileSync(f, 'utf8'));
    if (!list.length) continue;
    const cr = list.filter((h) => h.code === CR_CODE);
    const rest = list.filter((h) => h.code !== CR_CODE);
    for (const h of rest) killers.push(`${rel}:${h.line} 0x${h.code.toString(16)}`);
    if (cr.length) {
        if (CRLF_BASELINE[rel]) crBaselineSeen.add(rel);
        else crNew.push(`${rel}(${cr.length} 处)`);
    }
}

ok(killers.length === 0,
    'C1 🔴 没有**判据杀手级**控制字符(退格 / NUL 等)—— 词边界被吃成 0x08 时,'
    + '`!/…/` 那类否定断言会**恒真恒绿**,而没有任何东西会报警',
    killers.length
        ? killers.slice(0, 12).join(' / ') + (killers.length > 12 ? ` …共 ${killers.length} 处` : '')
        : `${files.length} 个文件干净`);

ok(crNew.length === 0,
    'C2 🔴 冻结名单之外**没有新的 CRLF 文件**',
    crNew.length ? crNew.join(' / ')
        : `名单外全是 LF(名单 ${Object.keys(CRLF_BASELINE).length} 个)`);

/* 🔴 冻结名单要**自己会过期**:名单里的文件已经改成 LF 了这条就红,
   提醒把它删掉 —— 不然名单会越留越久、越读越像事实。 */
{
    const stale = Object.keys(CRLF_BASELINE).filter((rel) => !crBaselineSeen.has(rel));
    ok(stale.length === 0,
        'C3 冻结名单没有过期条目(已改成 LF 的要删掉,别让名单变成一句假话)',
        stale.length ? stale.join(',') : '名单逐条仍然成立');
}

console.log('');
if (bad > 0) { console.log(`FAIL ${bad} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
