#!/usr/bin/env node
/**
 * 判据 · r8「后端错误原文不许原样弹给用户」。三态退出码:0 / 1 / 3。
 *
 * 🔴 分母**机械枚举全前端** `toast.error(` 调用点(不是手写四个文件的清单)——
 *    手写清单漏掉的那一项不会让任何东西变红。
 *
 * 🔴 但**修的范围**只有那四个文件(Review 裁「走甲」)。所以 199 处存量
 *    **冻结成豁免清单** `scripts/toast-direct-message-stock.json`,门 = **只减不增**:
 *    · 新增一处直传 ⇒ 红;
 *    · 清单里的项被修好 ⇒ 允许(数字只能往下走),但要把它从清单里删掉才算数;
 *    · 清单项**必须仍然存在且仍是直传**(非死豁免臂)—— 否则清单会烂成一张
 *      谁也不敢动的"历史遗留",而里面早就没有真东西了。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

let bad = 0; let notEvaluated = 0; const skipped = [];
const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

/** 取出每个 toast.error( 的**完整实参**(括号配对,不用正则截断)。 */
function toastArgs(src) {
    const out = [];
    const re = /toast\.error\(/g;
    let m;
    while ((m = re.exec(src)) !== null) {
        let j = m.index + m[0].length; let d = 1;
        while (j < src.length && d > 0) {
            if (src[j] === '(') d += 1;
            else if (src[j] === ')') d -= 1;
            j += 1;
        }
        out.push(src.slice(m.index + m[0].length, j - 1));
    }
    return out;
}
/**
 * 🔴 直传判定要**看穿一层本地封装**。
 *    B 发现 `IndustriesPromptsPanel.tsx` 会弹后端原文,却不在我 199 清单里 ——
 *    因为它把 `.message` 藏在自己写的 `humanizeError()` 里,而我的检测只看实参那一层。
 *    **一个本地的同功能副本,把直传藏在了一层函数调用之后。**
 *    修法:先收集本文件里「函数体含 .message 的本地函数名」,实参调用了它们的一律算直传。
 *    不是通用过程间分析,但恰好覆盖这一类 —— 第二份实现总写在同一个文件里。
 */
function localMessageHelpers(src) {
    const names = new Set();
    const re = /(?:function\s+(\w+)\s*\(|const\s+(\w+)\s*=)/g;
    let m;
    while ((m = re.exec(src)) !== null) {
        const name = m[1] || m[2];
        if (/\.message\b/.test(src.slice(m.index, m.index + 900))) names.add(name);
    }
    return names;
}
const isDirectWith = (a, helpers) =>
    (/\.message\b/.test(a) || [...helpers].some((h) => a.includes(h + '(')))
    && !/formatApiError/.test(a);


function walk(dir, acc = []) {
    for (const e of readdirSync(dir)) {
        const p = join(dir, e);
        if (statSync(p).isDirectory()) walk(p, acc);
        else if (/\.(tsx|ts)$/.test(e)) acc.push(p);
    }
    return acc;
}
const files = walk(SRC);
const live = {};
let totalSites = 0;
for (const p of files) {
    const args = toastArgs(strip(readFileSync(p, 'utf8')));
    totalSites += args.length;
    const helpers = localMessageHelpers(strip(readFileSync(p, 'utf8')));
    const n = args.filter((a) => isDirectWith(a, helpers)).length;
    if (n) live[relative(ROOT, p).split(String.fromCharCode(92)).join('/')] = n;
}

// ── A 正样本臂:枚举器真的在工作 ─────────────────────────────────────
// [开源 E3 · 前端 · 2026-10-01 · WO_322] 下限 700 → 550:基线 0cce841f9 读 877 → E3 后 694;逐文件对照,减少的全在已删文件里,在役文件一处没变
ok(totalSites >= 550, `A1 正样本臂:枚举到 ${totalSites} 个 toast.error 调用点(枚举为 0 会让下面全部恒真)`);
ok(isDirectWith('`失败: ${(e as Error).message}`', new Set()),
    'A2 反向对照:判定对样例直传串确实命中');
ok(isDirectWith('`失败: ${myHelper(e)}`', new Set(['myHelper'])),
    'A2a 🔴 反向对照:**藏在本地封装后面**的直传也算(B 抓到的那一类)');
ok(!isDirectWith("formatApiErrorForDisplay(e, '失败', 'admin')", new Set()),
    'A3 反向对照:走了 formatter 的不算直传');

// ── B 🔴 本笔改的四个文件必须归零 ───────────────────────────────────
const FIXED = [
    'src/pages/Admin/ResearchMonitor/ArticlesPanel.tsx',
    'src/pages/Admin/ResearchMonitor/CitationsPanel.tsx',
    'src/pages/Admin/ResearchMonitor/ConfigPanel.tsx',
    'src/pages/Admin/ResearchMonitor/RunningMonitorView.tsx',
];
for (const f of FIXED) ok(!live[f], `B1[${f.split('/').pop()}] 🔴 零直传(实得 ${live[f] || 0})`);
ok(FIXED.every((f) => /formatApiErrorForDisplay/.test(readFileSync(join(ROOT, f), 'utf8'))),
    'B2 四个文件都真的改走了共用 formatter(不是把 toast 删了了事)');

// ── C 🔴 存量只减不增 ───────────────────────────────────────────────
let stock = null;
try { stock = JSON.parse(readFileSync(join(ROOT, 'scripts/toast-direct-message-stock.json'), 'utf8')); }
catch { /* 下面按未评估处理 */ }
if (!stock) {
    notEvaluated += 1; skipped.push('C0');
    console.log('  ⚠️ C0 **未评估**:读不到存量冻结清单。');
} else {
    const frozen = stock.files || {};
    const added = Object.entries(live).filter(([p, n]) => !frozen[p] || n > frozen[p].count);
    ok(added.length === 0,
        `C1 🔴 **只减不增**:没有新增的直传(实得 ${JSON.stringify(added.slice(0, 5))})`);
    // 🔴 非死豁免臂:清单里的项必须**仍然存在且仍是直传**
    const dead = Object.keys(frozen).filter((p) => !live[p]);
    ok(dead.length === 0,
        `C2 🔴 豁免非死:清单里每一项都仍然存在且仍是直传(已修好的 ${JSON.stringify(dead.slice(0, 5))} 要从清单删掉)`
        + ' —— 否则清单会烂成一张里面早没有真东西的"历史遗留"');
    ok(Object.keys(frozen).length >= 50,
        `C3 正样本臂:清单里有 ${Object.keys(frozen).length} 个文件(清单为空会让 C1/C2 恒真)`);
    const cto13 = Object.entries(frozen).filter(([, v]) => v.note === 'CTO-13');
    // [开源 E3 · 前端 · 2026-10-01 · WO_322] 社媒板块前端整删,原「至少一个 CTO-13 项」的前提没了;
    //   反过来锁:清单里不许再有 CTO-13 项(有 = 删文件后清单没跟着清,C2 也会红)。
    ok(cto13.length === 0,
        `C4 社媒板块已整删:清单里 CTO-13 项应为 0(实得 ${cto13.length})`);
}

if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
if (notEvaluated > 0) { console.log(`\n⚠️ 未完成:${notEvaluated} 项未评估`); process.exit(3); }
console.log('\n✅ 全部通过');
process.exit(0);
