#!/usr/bin/env node
/**
 * 判据 · WO_267-A 行业大类:翻译函数真调 + 「英文 key 不上屏」源码锁(不起浏览器,进 build 链)。
 *
 * 为什么要两半:
 *   · 翻译函数(`industryTaxonomyText.ts`)是**唯一**把大类原值变成屏幕上那几个字的地方 ——
 *     四种输入(新 key / 存量中文 / 认不出的英文 key / 空)各有去处,逐种钉;
 *   · 但函数写对了不等于每个显示点都用了它。后端只给部分响应附 `industry_category_name`,
 *     新写入的原值是英文 key —— 任何一处直接渲染原值,就把 `new_energy` 露给用户。
 *     ⇒ 源码锁:前端每一次读原值 `industry_category`,要么在翻译函数调用里面,
 *       要么是类型声明 / 下拉的 raw 入参,要么在下面那张**写明理由的例外表**里。
 *     真渲染那半在 `test-industry-taxonomy-render.mjs`(要 chromium,挂 browser:arms)。
 *
 * 三态退出码:0 全过 / 1 有失败 / 3 判据自己没跑成(取不到模块、枚举塌了)。
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join, dirname, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'src');
const require_ = createRequire(import.meta.url);

let bad = 0;
const ok = (cond, label, detail = '') => {
    console.log(`  ${cond ? 'OK  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
    if (!cond) bad += 1;
};
const unusable = (why) => { console.log(`FAIL 判据不可用(不当绿灯):${why}`); process.exit(3); };

/* ══ A 翻译函数真调 ═══════════════════════════════════════════════════ */
let T;
try {
    const ts = require_('typescript');
    const src = readFileSync(join(SRC, 'lib', 'industryTaxonomyText.ts'), 'utf8');
    if (/^\s*import\s/m.test(src)) unusable('industryTaxonomyText.ts 出现了 import —— 它必须零 import 才能在这里真调');
    const js = ts.transpileModule(src, { compilerOptions: { module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2020 } }).outputText;
    T = await import(`data:text/javascript;base64,${Buffer.from(js).toString('base64')}`);
} catch (e) {
    unusable(`取不到翻译函数模块 —— ${String((e && e.message) || e).split('\n')[0]}`);
}

console.log('A 行业大类翻译函数(真调)');
const TAX = T.parseTaxonomy({
    version: 'v-test',
    categories: [
        { key: 'new_energy', name: '新能源', subcategories: ['光伏', '储能'] },
        { key: 'real_estate', name: '房地产', subcategories: [] },
        { key: 'education', name: '教育培训', subcategories: [] },
    ],
});
ok(!!TAX && TAX.categories.length === 3, 'A0 夹具字典解析成功(否则下面每一格都在空字典上跑)', TAX ? `${TAX.categories.length} 类` : 'null');
const nameOf = T.nameOfFrom(TAX);
const text = T.industryCategoryText;
/* 四种输入各一格(Review 09-23 点名) */
ok(text('new_energy', null, nameOf) === '新能源', 'A1 🔴 新写入的字典 key ⇒ 字典里的中文名', text('new_energy', null, nameOf));
ok(text('房产家居', null, nameOf) === '房产家居', 'A2 存量旧中文名 ⇒ 原样(它本来就是给人看的字)', text('房产家居', null, nameOf));
ok(text('zzz_unknown', null, nameOf) === '', 'A3 🔴 认不出的英文 key ⇒ 空(宁可不显示,也不把 key 露给用户)', JSON.stringify(text('zzz_unknown', null, nameOf)));
ok(text('', null, nameOf) === '' && text(null, null, nameOf) === '' && text('  ', '  ', nameOf) === '',
    'A4 空 / null / 全空白 ⇒ 空');
/* 后端附的 name 与它的陷阱 */
ok(text('房产家居', '房地产', nameOf) === '房地产', 'A5 后端附了中文名 ⇒ 用它(存量已由后端按映射翻过)');
ok(text('zzz_unknown', 'zzz_unknown', nameOf) === '',
    'A6 🔴 后端认不出时 name 回原值(英文 key)⇒ 仍不显示 —— 只信「看起来不像 key」的 name');
ok(text('new_energy', null, () => '') === '', 'A7 字典还没到(nameOf 回空)⇒ 新 key 暂不显示,不闪一下英文');
/* 原值 → key(下拉选中项 / 按大类筛选) */
const key = T.categoryKeyFromRaw;
ok(key('new_energy', TAX) === 'new_energy', 'K1 字典里有的 key ⇒ 原样');
ok(key('zzz_unknown', TAX) === '', 'K2 字典里没有的英文 key ⇒ 空(不选一个不存在的项)');
ok(key('教育培训', TAX) === 'education', 'K3 存量中文名恰好等于大类名 ⇒ 反查到 key');
ok(key('房产家居', TAX) === '', 'K4 存量中文名对不上任何大类名 ⇒ 空(不猜)');
ok(key('new_energy', null) === '', 'K5 字典没到 ⇒ 空');
ok(T.looksLikeCategoryKey('new_energy') && !T.looksLikeCategoryKey('新能源') && !T.looksLikeCategoryKey('New Energy'),
    'K6 「像 key」的判定:小写 ASCII + 下划线才算');

/* ══ B 源码锁:英文 key 不上屏 ══════════════════════════════════════════ */
const HELPERS = ['industryCategoryText(', 'categoryKeyFromRaw(', 'categoryTextOf('];
/** 例外表:读原值但**不上屏**的地方。每条写明理由;多一条少一条都要改这张表。 */
const DECLARED = [
    { file: 'lib/api.ts', line: "list: (params?: { search?: string; industry_category?: string; limit?: number; offset?: number }) =>",
        why: '请求参数的类型声明(按大类筛选的查询参数),不是显示' },
    { file: 'lib/api.ts', line: "industry_category: typeof overview.industry_category === 'string' ? overview.industry_category : undefined,",
        why: '演示概览原值透传进 ClientContextDetail;下游显示点一律再过翻译函数' },
    { file: 'sandbox/mockData.ts', line: 'industry_category: SANDBOX_BRAND_BASE.industry_category,',
        why: '沙盒夹具之间的透传(值是中文「汽车出行」)' },
    { file: 'pages/Brand/BrandDetailPage.tsx', line: 'if (categoryPick.touched) payload.industry_category = categoryPick.key;',
        why: '写请求(保存):值是下拉选定的字典 key,且只在用户动过下拉时才带' },
    { file: 'pages/Brand/BrandDetailPage.tsx', line: '...(categoryPick.touched ? { industry_category: categoryPick.key } : {}),',
        why: '写请求(深度解析静默保存):同上' },
];
const EXCLUDED = new Set(['lib/industryTaxonomy.ts', 'lib/industryTaxonomyText.ts', 'components/brand/IndustryCategorySelect.tsx']);

const decomment = (s) => s
    .replace(/\/\*[\s\S]*?\*\//g, (m) => m.replace(/[^\n]/g, ' '))
    .replace(/(^|[^:])\/\/[^\n]*/g, (m, p) => p + ' '.repeat(m.length - p.length));

function walk(dir, out = []) {
    for (const name of readdirSync(dir)) {
        const p = join(dir, name);
        if (statSync(p).isDirectory()) walk(p, out);
        else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.d\.ts$/.test(name)) out.push(p);
    }
    return out;
}

/** 一段源码里「读原值且不在翻译函数里」的位置。纯文本输入,反臂拿同一个函数喂毒。 */
function leaksIn(rel, code) {
    const src = decomment(code);
    const found = [];
    const re = /industry_category(?![A-Za-z0-9_])/g;
    let m;
    while ((m = re.exec(src))) {
        const at = m.index;
        const lineStart = src.lastIndexOf('\n', at) + 1;
        const lineEnd = src.indexOf('\n', at) === -1 ? src.length : src.indexOf('\n', at);
        const line = src.slice(lineStart, lineEnd).trim();
        /* ① 类型 / 接口声明:`industry_category?: string;` */
        if (/^industry_category\??\s*:\s*[A-Za-z|\s]+[;,]?$/.test(line)) continue;
        /* ② 下拉的 raw 入参 */
        if (/raw=\{[^}]*industry_category\}/.test(line)) continue;
        /* ③ 在翻译函数调用的括号里面(可跨行:从调用的左括号数到这里,深度不归零) */
        let inside = false;
        for (const h of HELPERS) {
            let from = src.lastIndexOf(h, at);
            while (from !== -1 && !inside) {
                let depth = 0;
                for (let i = from + h.length - 1; i < at; i += 1) {
                    if (src[i] === '(') depth += 1;
                    else if (src[i] === ')') depth -= 1;
                    if (depth === 0) break;
                }
                if (depth > 0) inside = true;
                from = src.lastIndexOf(h, from - 1);
                if (at - from > 600) break;
            }
            if (inside) break;
        }
        if (inside) continue;
        /* ④ 沙盒夹具里写死的中文值 */
        if (rel.startsWith('sandbox/') && /industry_category:\s*'[^'a-z]*'/.test(line)) continue;
        /* ⑤ 例外表 */
        if (DECLARED.some((d) => d.file === rel && d.line === line)) continue;
        found.push(`${rel}:${src.slice(0, at).split('\n').length} ${line.slice(0, 90)}`);
    }
    return found;
}

console.log('\nB 英文 key 不上屏(源码锁)');
const files = walk(SRC).map((p) => ({ rel: relative(SRC, p).split('\\').join('/'), p }))
    .filter((f) => !EXCLUDED.has(f.rel));
const readers = files.filter((f) => /industry_category(?![A-Za-z0-9_])/.test(decomment(readFileSync(f.p, 'utf8'))));
/* 分母自证:枚举塌了(改目录 / 正则坏了)⇒ 下面的「零泄漏」是空话 */
if (files.length < 300 || readers.length < 8) unusable(`枚举塌了:src 下 ${files.length} 个文件、读原值的 ${readers.length} 个`);
ok(readers.length >= 8, 'B0 分母自证:枚举到了读 industry_category 的文件', `${readers.length} 个 / 全部 ${files.length} 个`);
const leaks = files.flatMap((f) => leaksIn(f.rel, readFileSync(f.p, 'utf8')));
ok(leaks.length === 0, 'B1 🔴 前端每一次读原值 industry_category 都过翻译函数(或在例外表里写明理由)',
    leaks.length ? `${leaks.length} 处:${leaks.slice(0, 6).join(' | ')}${leaks.length > 6 ? ' …' : ''}` : '零泄漏');
const stale = DECLARED.filter((d) => {
    const f = files.find((x) => x.rel === d.file);
    return !f || !decomment(readFileSync(f.p, 'utf8')).split('\n').some((l) => l.trim() === d.line);
});
ok(stale.length === 0, 'B2 例外表每一条都还对得上源码(对不上 = 陈账,删掉它)',
    stale.map((d) => `${d.file}: ${d.line.slice(0, 40)}`).join(' | ') || `${DECLARED.length} 条都在`);
const own = readFileSync(join(SRC, 'lib', 'industryTaxonomy.ts'), 'utf8');
ok(!/function\s+(industryCategoryText|categoryKeyFromRaw|looksLikeCategoryKey)\b/.test(decomment(own)),
    'B3 industryTaxonomy.ts 只转出、不另写一份翻译函数(否则业务用的是没被 A 段测过的那份)');

/* ══ C 锁自己的牙:同一个 leaksIn,喂毒与对照 ═══════════════════════════ */
console.log('\nC 锁的反臂(同一谓词)');
const poisonDirect = 'const x = <span>{item.industry_category}</span>;\n';
ok(leaksIn('pages/X.tsx', poisonDirect).length === 1, 'C1 🔴 直接渲染原值 ⇒ 抓到 1 处');
const poisonFallback = 'const t = item.industry || item.industry_category || "-";\n';
ok(leaksIn('pages/X.tsx', poisonFallback).length === 1, 'C2 🔴 老写法「industry || industry_category」兜底 ⇒ 抓到');
const cleanMulti = 'const t = industryCategoryText(\n  brand.industry_category as string,\n  brand.industry_category_name,\n  nameOf);\n';
ok(leaksIn('pages/X.tsx', cleanMulti).length === 0, 'C3 对照:跨行的翻译函数调用 ⇒ 不误报');
const afterCall = 'const t = industryCategoryText(a, b, nameOf) || item.industry_category;\n';
ok(leaksIn('pages/X.tsx', afterCall).length === 1, 'C4 🔴 调用括号已闭合、后面又直读原值 ⇒ 抓到(不是「同行有调用就放过」)');
const inComment = '/* 旧写法:item.industry_category */\n// item.industry_category\n';
ok(leaksIn('pages/X.tsx', inComment).length === 0, 'C5 对照:注释里提到 ⇒ 不算');

if (bad > 0) { console.log(`\nFAIL ${bad} 项不通过`); process.exit(1); }
console.log('\n全部通过');
process.exit(0);
