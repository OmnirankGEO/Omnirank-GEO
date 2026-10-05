#!/usr/bin/env node
/**
 * #201 第一步 · 原生 `<select>` 的**分母**(只查不改)。
 *
 * ## 为什么前两个数都不是分母
 *
 * · A 报的 18 处 = **只看 79 个页面文件**(#198 §3① 自承的盲点:不看子组件);
 * · Review 粗扫的 49 处 = **目录排除法**(排掉 Social 系、SEnd、Admin 等目录),
 *   没按"服务商点得到吗"归类,也没说漏在排除目录里的那些算不算。
 *
 * 两者都是**某种取法下的计数**,不是分母。分母只有一个来法:
 *   `frontend/src` 里**每一个** `<select`,逐个落进一个桶,一条不许进「其它」。
 *
 * ## 这份表怎么来的
 *
 * 1. 从 #198 的路由表取**服务商端 120 条**;
 * 2. 路由 → 页面文件(App.tsx 的两种 lazy 写法 + 具名 import);
 * 3. 🔴 **沿 import 树递归**(页面 → 子组件 → 孙组件,含组件内部的 lazy)——
 *    这是 #198 缺的那一步,也是 18 与 49 差出来的地方;
 * 4. 全树枚举 `<select`,按"哪些路由到得了它"分桶;
 * 5. 分母自证:全树计数 == 各桶之和。对不上就**报错退出**,不出表。
 *
 * 跑法:node scripts/audit-201-native-select.mjs [--json]
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const NL = String.fromCharCode(10);

/* ── 0 路由表(复用 #198 那把已经自证过分母的工具)────────────────── */
let routeData;
try {
    routeData = JSON.parse(execFileSync(process.execPath,
        [join(ROOT, 'scripts/audit-198-routes.mjs'), '--json'],
        { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 << 20 }));
} catch (e) {
    console.log('🔴 取不到路由表 —— 不出表(宁可没有,也不要一份不知道分母的表):'
        + String((e && e.message) || e).split(NL)[0]);
    process.exit(1);
}
const ROUTES = routeData.rows;
const agentRoutes = ROUTES.filter((r) => r.group === 'agent');

/* ── 1 组件名 → 源码文件(App.tsx 的两种 lazy 写法 + 具名 import)──── */
const APP = rd('src/App.tsx');
const compFile = {};
for (const m of APP.matchAll(/const\s+([A-Z][A-Za-z0-9_]*)\s*=\s*lazy\(\s*\(\)\s*=>\s*import\(['"]([^'"]+)['"]\)/g)) {
    compFile[m[1]] = m[2];
}
{
    const loaders = {};
    for (const m of APP.matchAll(/const\s+(load[A-Za-z0-9_]*)\s*=\s*\(\)\s*=>\s*import\(['"]([^'"]+)['"]\)/g)) {
        loaders[m[1]] = m[2];
    }
    for (const m of APP.matchAll(/const\s+([A-Z][A-Za-z0-9_]*)\s*=\s*lazy\(\s*(load[A-Za-z0-9_]*)\s*\)/g)) {
        if (loaders[m[2]]) compFile[m[1]] = loaders[m[2]];
    }
}
for (const m of APP.matchAll(/import\s+\{?\s*([A-Za-z0-9_,\s]+?)\s*\}?\s+from\s+['"]([^'"]+)['"]/g)) {
    for (const name of m[1].split(',').map((x) => x.trim()).filter(Boolean)) {
        if (/^[A-Z]/.test(name) && !compFile[name]) compFile[name] = m[2];
    }
}

/*
 * 🔴 **内联路由包装**要再跳一层。
 *    `<Route element={<AgentPricingRoute />}>` 里的 `AgentPricingRoute` 不是 import 进来的,
 *    是 App.tsx 里的一个函数,函数体里才渲染真页面(`<AgentPricingCenter />`)。
 *    不跳这一层,这些路由会落进"映射不到源码",它们底下**整棵子树**跟着掉出可达集 ——
 *    实测:`ClientPurchaseGatePanel` 明明被 `Agent/PricingCenter` 用着,
 *    却被判成"任何路由都到不了"。`DefaultRouteGate`(渲染 Dashboard)同理。
 *    ⇒ 取函数体里出现的大写组件名,再走一次 compFile。只跳一层,跳不到就照实记未映射。
 */
const inlineWrapped = {};
for (const m of APP.matchAll(/function\s+([A-Z][A-Za-z0-9_]*)\s*\([^)]*\)\s*\{/g)) {
    const start = m.index + m[0].length;
    /* 函数体:从 `{` 起做花括号配平,别用固定字符数窗口(会切到隔壁函数) */
    let depth = 1;
    let i = start;
    while (i < APP.length && depth > 0) {
        const ch = APP[i];
        if (ch === '{') depth += 1;
        else if (ch === '}') depth -= 1;
        i += 1;
    }
    const body = APP.slice(start, i);
    const names = [...body.matchAll(/<([A-Z][A-Za-z0-9_]*)/g)].map((x) => x[1]);
    if (names.length) inlineWrapped[m[1]] = names;
}

/** 模块说明符 → 仓内相对路径(解析不出来返回空串,由调用方计数)。 */
function resolveSpec(spec, fromFile) {
    if (!spec) return '';
    let base = '';
    if (spec.startsWith('@/')) base = join(ROOT, 'src', spec.slice(2));
    else if (spec.startsWith('.')) base = join(ROOT, dirname(fromFile || 'src/App.tsx'), spec);
    else return '';                     // 第三方包:不进 import 树
    for (const ext of ['.tsx', '.ts', '/index.tsx', '/index.ts']) {
        if (!existsSync(base + ext)) continue;
        /* 🔴 规范化:`slice(ROOT.length)` 会留下前导分隔符(`\\src/...`),
           而全树枚举的键是 `src/...` —— 两边永远对不上,可达集就会**全空**,
           而和式自证(各桶之和 == 总数)照样成立。实测:102 处全落进「到不了」。 */
        return (base + ext).slice(join(ROOT, '').length)
            .split('\\').join('/').replace(/^[/]+/, '');
    }
    return '';
}

/* ── 2 沿 import 树递归 ─────────────────────────────────────────────
 * 🔴 静态 import 与组件内部的 `lazy(() => import(...))` 都要走。
 *    只走静态 import 的话,凡是被懒加载的子面都会掉出树 ——
 *    而懒加载正是"大页面"的标配。
 */
const importCache = new Map();
function importsOf(file) {
    if (importCache.has(file)) return importCache.get(file);
    let text = '';
    try { text = rd(file); } catch { importCache.set(file, []); return []; }
    const out = new Set();
    for (const m of text.matchAll(/from\s+['"]([^'"]+)['"]/g)) {
        const r = resolveSpec(m[1], file);
        if (r) out.add(r);
    }
    for (const m of text.matchAll(/import\(\s*['"]([^'"]+)['"]\s*\)/g)) {
        const r = resolveSpec(m[1], file);
        if (r) out.add(r);
    }
    const arr = [...out];
    importCache.set(file, arr);
    return arr;
}

/** 从一组入口文件出发,BFS 出**可达文件集**。 */
function reachFrom(entries) {
    const seen = new Set();
    const queue = [...entries];
    while (queue.length) {
        const f = queue.shift();
        if (!f || seen.has(f)) continue;
        seen.add(f);
        for (const n of importsOf(f)) if (!seen.has(n)) queue.push(n);
    }
    return seen;
}

/* 每个路由组各自的可达集 */
const entriesOf = (group) => {
    const out = new Set();
    const miss = [];
    for (const r of ROUTES.filter((x) => x.group === group)) {
        const f = resolveSpec(compFile[r.el], 'src/App.tsx');
        if (f) { out.add(f); continue; }
        /* 内联包装:取它渲染的组件再解析一次(只跳一层) */
        const inner = (inlineWrapped[r.el] || [])
            .map((n) => resolveSpec(compFile[n], 'src/App.tsx')).filter(Boolean);
        if (inner.length) { for (const x of inner) out.add(x); continue; }
        miss.push(`${r.path} (组件 ${r.el})`);
    }
    return { entries: [...out], miss };
};
const GROUPS = ['agent', 'admin', 'customer', 'public', 'social'];
const reach = {};
const missByGroup = {};
for (const g of GROUPS) {
    const { entries, miss } = entriesOf(g);
    reach[g] = reachFrom(entries);
    missByGroup[g] = miss;
}

/* 路由 → 该路由可达的文件(给"这一处属于哪几页"用) */
const routesTouching = new Map();     // file -> Set(route path)
for (const r of agentRoutes) {
    let starts = [resolveSpec(compFile[r.el], 'src/App.tsx')].filter(Boolean);
    if (!starts.length) {
        starts = (inlineWrapped[r.el] || [])
            .map((n) => resolveSpec(compFile[n], 'src/App.tsx')).filter(Boolean);
    }
    if (!starts.length) continue;
    for (const hit of reachFrom(starts)) {
        if (!routesTouching.has(hit)) routesTouching.set(hit, new Set());
        routesTouching.get(hit).add(r.path);
    }
}

/* ── 3 全树枚举 `<select`(这就是分母)──────────────────────────── */
function walk(rel, out) {
    for (const name of readdirSync(join(ROOT, rel))) {
        const p = `${rel}/${name}`;
        if (statSync(join(ROOT, p)).isDirectory()) walk(p, out);
        else if (/\.tsx?$/.test(name)) out.push(p);
    }
    return out;
}
const ALL_FILES = walk('src', []);

/*
 * 🔴 只认**开标签** `<select` 后面跟空白 / `>` / 换行 —— 不要前缀匹配:
 *    `<selectSomething` 之类不算。同族老病:`indexOf('<Route')` 命中了 `<Router>`。
 */
const SELECT_RE = /<select(?=[\s>/])/g;

/** 资金 / 下单路径:规则写出来,好让人复看(不是我说了算)。 */
const FUNDS_FILE = /(Quote\/|Order|Publish|Wallet|Cart|Payment|Recharge|Pricing)/i;
const FUNDS_ROUTE = /(pricing|quote|publish|wallet|order|recharge|pay)/i;

/*
 * 🔴 社媒板块归 CTO-13(CLAUDE.md 板块边界),本窗口不碰。
 *    但**不许自己把它从分母里剪掉** —— 枚举完再用 grep -v 缩分母是本仓老病。
 *    做法:打标、单独切一档、在表里写清"这一档不是我的作业面",由 Review 定。
 *    两个轴都认:路由前缀 与 文件所在目录(有的社媒页挂在别的路由下)。
 */
const SOCIAL_FILE = /(pages[/]Social|pages[/]SEnd|components[/]s_end|components[/]social[/])/;
const SOCIAL_ROUTE = /^[/](social|s)([/]|$|-)/;

/** admin-only 分支:向前找 400 字符里有没有 admin 味的条件。**候选,不是判决**。 */
const ADMIN_HINT = /(isAdmin|is_admin|role\s*===\s*['"]admin|admin-only|requiredRole=['"]admin)/;

const rows = [];
for (const file of ALL_FILES) {
    let text = '';
    try { text = rd(file); } catch { continue; }
    for (const m of text.matchAll(SELECT_RE)) {
        const at = m.index;
        const line = text.slice(0, at).split(NL).length;
        const tagEnd = text.indexOf('>', at);
        const tag = text.slice(at, tagEnd > at ? tagEnd + 1 : at + 200).replace(/\s+/g, ' ');
        const back = text.slice(Math.max(0, at - 400), at);
        const groups = GROUPS.filter((g) => reach[g].has(file));
        const routes = [...(routesTouching.get(file) || [])];
        rows.push({
            file,
            line,
            /* 语义:value= 与 onChange= 原样截出来,复看时不用回源码翻 */
            value: (tag.match(/value=\{([^}]{0,60})\}/) || [])[1] || '',
            onChange: (tag.match(/onChange=\{([^}]{0,80})/) || [])[1] || '',
            id: (tag.match(/id="([^"]{0,40})"/) || [])[1] || '',
            ariaLabel: (tag.match(/aria-label="([^"]{0,40})"/) || [])[1] || '',
            groups,
            agentVisible: groups.includes('agent'),
            routes,
            adminHint: ADMIN_HINT.test(back),
            funds: FUNDS_FILE.test(file) || routes.some((r) => FUNDS_ROUTE.test(r)),
            social: SOCIAL_FILE.test(file)
                || (routes.length > 0 && routes.every((r) => SOCIAL_ROUTE.test(r))),
        });
    }
}

/* ── 4 分母自证 ───────────────────────────────────────────────────── */
const bucketAgent = rows.filter((r) => r.agentVisible);
const bucketOtherRoute = rows.filter((r) => !r.agentVisible && r.groups.length > 0);
const bucketUnreached = rows.filter((r) => r.groups.length === 0);
const total = rows.length;
const sum = bucketAgent.length + bucketOtherRoute.length + bucketUnreached.length;

if (process.argv.includes('--json')) {
    console.log(JSON.stringify({ total, rows, missByGroup }, null, 2));
    process.exit(0);
}

console.log('#201 第一步 · 原生 <select> 分母(只查不改)');
console.log('');
console.log(`分母:frontend/src 全树 .ts/.tsx ${ALL_FILES.length} 个文件,<select 开标签 **${total} 处**`);
console.log(`分桶:服务商端可达 ${bucketAgent.length} · 仅其它路由组可达 ${bucketOtherRoute.length}`
    + ` · 任何路由都到不了 ${bucketUnreached.length}`);
/*
 * 🔴 和式自证**不够**:0 + 0 + 102 也等于 102。
 *    "全都到不了"这种读数看起来像结论,实际上是**扫描器死了**的样子
 *    (本脚本第一版就是:路径带前导分隔符,可达集全空)。
 *    ⇒ 桶退化时必须**喊出来并非零退出**,不许把它当成一份表交出去。
 */
if (agentRoutes.length > 0 && bucketAgent.length === 0) {
    console.log('🔴 仪器自检不过:服务商端有 ' + agentRoutes.length
        + ' 条路由,却一处 <select 都到不了 —— 这是可达性解析坏了的样子,不是结论。不出表。');
    process.exit(1);
}
if (reach.agent.size < 50) {
    console.log(`🔴 仪器自检不过:服务商路由树只走到 ${reach.agent.size} 个文件(全树 ${ALL_FILES.length} 个)`
        + ' —— import 树没走开,不出表。');
    process.exit(1);
}
if (sum !== total) {
    console.log(`🔴 分母对不上:${sum} ≠ ${total} —— 不出表(有一处没落进任何桶)`);
    process.exit(1);
}
console.log(`✅ 分母自证:${bucketAgent.length} + ${bucketOtherRoute.length} + ${bucketUnreached.length}`
    + ` = ${sum} = 全树计数 ${total}(一条没进「其它」)`);
console.log('');
console.log(`路由:共 ${ROUTES.length} 条,服务商端 ${agentRoutes.length} 条`);
for (const g of GROUPS) {
    if (missByGroup[g].length) {
        console.log(`  ⚠️ ${g} 组映射不到源码的 ${missByGroup[g].length} 条(多为 Navigate 重定向):`);
        for (const u of missByGroup[g]) console.log(`      ${u}`);
    }
}

const fmt = (r) => {
    const sem = [r.id && `id=${r.id}`, r.ariaLabel && `aria=${r.ariaLabel}`,
        r.value && `value={${r.value}}`, r.onChange && `onChange={${r.onChange.slice(0, 40)}…}`]
        .filter(Boolean).join(' · ') || '(开标签上没有 value/onChange —— 复看时要回源码)';
    const flags = [r.funds ? '💰资金/下单' : '', r.adminHint ? '🔒admin 候选' : ''].filter(Boolean).join(' ');
    const where = r.routes.length
        ? `${r.routes.slice(0, 3).join(' , ')}${r.routes.length > 3 ? ` …共 ${r.routes.length} 条` : ''}`
        : `(不在服务商路由树里;可达组:${r.groups.join('/') || '无'})`;
    return `  ${r.file}:${r.line}${flags ? '  ' + flags : ''}${NL}      路由 ${where}${NL}      ${sem}`;
};

console.log('');
const agentGeo = bucketAgent.filter((r) => !r.social);
const agentSocial = bucketAgent.filter((r) => r.social);
console.log(`── A 服务商端可达(${bucketAgent.length} 处)= GEO 域 ${agentGeo.length}`
    + ` + 社媒域 ${agentSocial.length}`);
console.log('   🔴 社媒域(旧社媒操盘手 / SEnd / s_end / 社媒组件目录;前三者与社媒组件已随开源 E3 删)归 **CTO-13**,');
console.log('      本窗口不碰 —— 但它**留在分母里**,由 Review 定要不要、由谁改。');
console.log('      枚举完再自己剪掉,就又造了一个「某种取法下的计数」。');
console.log('');
console.log(`── A1 GEO 域(${agentGeo.length} 处)── 这一档才是 #201 第二步的作业面`);
const byFile = new Map();
for (const r of agentGeo) {
    if (!byFile.has(r.file)) byFile.set(r.file, []);
    byFile.get(r.file).push(r);
}
for (const [f, list] of [...byFile].sort((a, b) => b[1].length - a[1].length)) {
    console.log(`${NL}  【${f}】${list.length} 处`);
    for (const r of list) console.log(fmt(r).split(NL).slice(0, 3).join(NL));
}

console.log('');
console.log(`── A2 社媒域(${agentSocial.length} 处)── **归 CTO-13,本窗口不改**,列出来是因为它在分母里`);
{
    const m = new Map();
    for (const r of agentSocial) m.set(r.file, (m.get(r.file) || 0) + 1);
    for (const [f, n] of [...m].sort((x, y) => y[1] - x[1])) console.log(`  ${f}  ${n} 处`);
}

console.log('');
console.log(`── B 仅其它路由组可达(${bucketOtherRoute.length} 处)── 不在本单作业面,但要列出来`);
for (const r of bucketOtherRoute) console.log(`  ${r.file}:${r.line}  (${r.groups.join('/')})`);

console.log('');
console.log(`── C 任何路由都到不了(${bucketUnreached.length} 处)── 死码 / 非路由入口,逐条列`);
for (const r of bucketUnreached) console.log(`  ${r.file}:${r.line}`);

console.log('');
console.log('🔴 读这张表的三条提醒:');
console.log('  1. 「💰资金/下单」是**按文件名与路由名**打的标(规则写在脚本里),');
console.log('     它是**候选**不是判决 —— 第二步动它之前要逐个看 payload 走哪条路。');
console.log('  2. 「🔒admin 候选」是向前 400 字符里有 admin 味的条件,同样只是候选:');
console.log('     条件可能写在包着这段 JSX 的更外层,静态看不见。');
console.log('  3. 可达 ≠ 用户真点得到(路由可达只说明代码走得到)。要不要改由 Review 定批。');
process.exit(0);
