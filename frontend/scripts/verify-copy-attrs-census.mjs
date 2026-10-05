#!/usr/bin/env node
/**
 * 判据 · 对客文案禁词普查(**含属性**)。三态退出码:0 / 1 / 3。
 *
 * 🔴 [#124] 只查 innerText 的普查对**占位符全盲**。
 *    Deploy 首轮把「不填也行…」读成"不存在",根因就是这个 ——
 *    那句话在 `placeholder=` 里,而普查只看标签之间的文字。
 *    所以分母显式包含:placeholder / value / aria-label / title / alt / label / option 文本。
 *
 * 🔴 中文禁词**不需要 JSX 解析**:中文只可能出现在文案或注释里,注释已剥掉
 *    ⇒「去注释后的源码包含」就是可靠判据(昨天在钱包那笔上验过)。
 *    属性这一层同理 —— 所以本条用**整源包含**,再用属性抽取做**形状臂**,
 *    证明属性里的字确实进了分母,而不是靠"整源包含"蒙混过去。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const strip = (s) => s.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
const relPath = (p) => relative(ROOT, p).split(String.fromCharCode(92)).join('/');

let bad = 0; const ok = (c, l) => { console.log(`${c ? '  ✅' : '  🔴'} ${l}`); if (!c) bad += 1; };

/** GEO 主链页面 —— 社媒板块归 CTO-13,不在本窗口分母里。 */
const SCOPE = ['src/pages/Diagnosis', 'src/pages/Quote', 'src/pages/Reports',
    'src/pages/Brand', 'src/pages/Home', 'src/pages/Wallet', 'src/components/wallet'];
/** 中文禁词。引擎 → AI 平台(#124);额度/积分 → 算力;代理 → 服务商(对外)。 */
const BANNED = ['引擎', '额度', '积分', '工具额度'];

function walk(dir, acc = []) {
    let st; try { st = statSync(dir); } catch { return acc; }
    if (!st.isDirectory()) { if (/\.(tsx|ts)$/.test(dir)) acc.push(dir); return acc; }
    for (const e of readdirSync(dir)) walk(join(dir, e), acc);
    return acc;
}
const files = SCOPE.flatMap((d) => walk(join(ROOT, d)));
ok(files.length >= 20, `A0 正样本臂:扫到 ${files.length} 个文件(扫 0 会让下面全部恒真)`);

const hits = [];
let cjkChars = 0;
for (const p of files) {
    const src = strip(readFileSync(p, 'utf8'));
    cjkChars += (src.match(/[一-龥]/g) || []).length;
    for (const b of BANNED) {
        let i = src.indexOf(b);
        while (i >= 0) {
            hits.push([relPath(p), b, src.slice(Math.max(0, i - 20), i + 20).replace(/\s+/g, ' ')]);
            i = src.indexOf(b, i + 1);
        }
    }
}
ok(cjkChars >= 5000, `A1 正样本臂:范围内共 ${cjkChars} 个中文字(为 0 会让下一条恒真)`);
ok(hits.length === 0, `A2 🔴 GEO 主链页零禁词(实得 ${JSON.stringify(hits.slice(0, 4))})`);
ok(BANNED.some((b) => '4 个引擎同步检测'.includes(b)), 'A3 反向对照:词表对样例串确实命中');

// ── B 🔴 形状臂:属性里的字确实在分母里 ──────────────────────────────
/**
 * 🔴 光有 A2 还不够 —— 它靠"整源包含",而我要证的是**属性这一层没被漏掉**。
 *    所以抽一遍属性文本,断言它非空且确实抓到已知的占位符
 *    (「不填也行…」正是 Deploy 读成"不存在"的那一句)。
 */
const ATTR = /(?:placeholder|title|aria-label|alt|label)\s*=\s*["']([^"']*[一-龥][^"']*)["']/g;
const attrTexts = [];
for (const p of files) {
    const src = strip(readFileSync(p, 'utf8'));
    let m; ATTR.lastIndex = 0;
    while ((m = ATTR.exec(src)) !== null) attrTexts.push(m[1]);
}
ok(attrTexts.length >= 10, `B1 🔴 属性文本抽到 ${attrTexts.length} 条(为 0 说明属性这层根本没进分母)`);
ok(attrTexts.some((t) => t.includes('不填也行')),
    'B2 🔴 形状臂:抓到了 placeholder 里那句「不填也行…」—— '
    + 'Deploy 首轮把它读成"不存在",就是因为只查 innerText 对占位符全盲');
ok(!attrTexts.some((t) => BANNED.some((b) => t.includes(b))), 'B3 属性文本里也零禁词');

if (bad > 0) { console.log(`\n🔴 ${bad} 项不通过`); process.exit(1); }
console.log('\n✅ 全部通过');
process.exit(0);
