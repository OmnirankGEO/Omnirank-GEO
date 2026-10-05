#!/usr/bin/env node
/**
 * #198 「禁猜」存量普查 · **第 0 步:分母**。
 *
 * 🔴 这不是门,是**路由清单**。先把 `App.tsx` 的 Route 表机械枚举全、分好类、
 *    打印计数,数字与手工抽查对上,再谈扫违规 —— 分母不完整的普查,
 *    结论只会是「我扫到的那些里没问题」。
 *
 * 🔴 **零产品改动**(工单铁律):本脚本只读。
 *
 * ## 怎么枚举
 *
 * `<Route path="X" element={<C/>} />` 逐条抓,并处理三件事:
 *   ① **嵌套**:`<Route path="/" element={<Layout/>}>` 里面的子路由是**相对路径**
 *      (`writing`、`admin/users`),要拼成 `/writing`。不拼的话它们看起来像另一套路由,
 *      而且会和顶层同名路径混淆;
 *   ② **模板生成的**:`socialStudioModulePaths.map(...)` 这种不是字面量,
 *      单独登记为一类(数量从被 map 的数组长度取),不然分母会少掉一批;
 *   ③ **抓不出来的**:一律**列出来**,不许并进「其它」——
 *      「有几条我没解析出来」本身就是分母的一部分。
 *
 * ## 分类(工单 §1:服务商端全部路由;客户端 C 面另表)
 *   public   —— 不登录就能开(落地/登录/公开报告/白标门户/报价链接)
 *   customer —— 客户(C 端)面:`/c/*`、`/portal/*`、`/customer/*`、`/q/:code`
 *   admin    —— `/admin/*`(平台权限)
 *   social   —— 社媒板块 `/s/*`(归 CTO-13,本单不查)
 *   agent    —— **服务商端** = 本单要查的那些
 *
 * 跑法:cd frontend && node scripts/audit-198-routes.mjs [--json]
 */
import { readFileSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = readFileSync(join(ROOT, 'src/App.tsx'), 'utf8');

const lines = SRC.split('\n');

/**
 * 每一处 `<Route` 的原文(到它自己那个 `>` 为止,带花括号深度)。
 *
 * 🔴 `indexOf('<Route')` 会**顺带命中** `<Router>` 与 `<Routes>` ——
 *    前缀匹配多算,第一版因此报「200 处、2 条没解析出来」,
 *    而那 2 条根本不是路由。真值是 **198**。
 *    (同族:统计前先列形态;前缀 grep 多算是本仓反复出现的那一类。)
 *    ⇒ 要求 `<Route` 后面是空白或 `>`。
 */
function routeTags(text) {
    const out = [];
    const isRealTag = (at) => /[\s>]/.test(text[at + 6] || '');
    let i = text.indexOf('<Route');
    while (i >= 0 && !isRealTag(i)) i = text.indexOf('<Route', i + 1);
    while (i >= 0) {
        let depth = 0;
        let end = -1;
        for (let k = i; k < text.length; k += 1) {
            const ch = text[k];
            if (ch === '{') depth += 1;
            else if (ch === '}') depth -= 1;
            else if (ch === '>' && depth === 0 && text[k - 1] !== '=' && text[k + 1] !== '=') {
                end = k;
                break;
            }
        }
        if (end < 0) break;
        out.push({ index: i, text: text.slice(i, end + 1), line: text.slice(0, i).split('\n').length });
        i = text.indexOf('<Route', end);
        while (i >= 0 && !isRealTag(i)) i = text.indexOf('<Route', i + 1);
    }
    return out;
}

const tags = routeTags(SRC);

/*
 * 🔴 **嵌套不止一层、也不止一处**。第一版只处理了 `/` 那个 Layout,
 *    结果 `/portal/*`、社媒工作台那一组子路由全被标成「相对但不在布局内」——
 *    看起来像解析失败,其实是**我的嵌套模型只有一层**。
 *    ⇒ 改成扫描时维护一个**父路径栈**:遇到不自闭合的 `<Route path=...>` 入栈,
 *    遇到 `</Route>` 出栈。这样几层都对。
 *
 * 🔴 出栈靠的是源码里真的有 `</Route>`;栈空时多出来的 `</Route>` 单独记,
 *    不静默丢 —— 静默丢掉会让后面所有路径少一层前缀,而输出看起来完全正常。
 */
const rows = [];
const unparsed = [];
const stray = [];
{
    const stack = [];
    const closes = [...SRC.matchAll(/<\/Route>/g)].map((m) => m.index);
    const events = [
        ...tags.map((t) => ({ at: t.index, kind: 'open', t })),
        ...closes.map((at) => ({ at, kind: 'close' })),
    ].sort((a, b) => a.at - b.at);

    const joinPath = (parent, raw) => {
        if (raw.startsWith('/')) return raw;
        const base = parent.endsWith('/') ? parent.slice(0, -1) : parent;
        return `${base}/${raw}`;
    };

    for (const ev of events) {
        if (ev.kind === 'close') {
            if (stack.length) stack.pop();
            else stray.push(SRC.slice(0, ev.at).split(String.fromCharCode(10)).length);
            continue;
        }
        const t = ev.t;
        const selfClosing = /\/>\s*$/.test(t.text);
        const pm = t.text.match(/path=\{?[`"']([^`"']*)[`"']\}?/);
        /* 🔴 `index` 要按**词**匹配。上一版这里写的是词边界转义,
           被改写脚本吃成了**退格字符**,正则从此永远匹配不上 ——
           `verify-no-control-chars` 当场把它照了出来(今天第三次)。
           所以这里用**显式字符类**做边界,一律不写那个转义。 */
        const isIndex = /(^|[^A-Za-z])index([^A-Za-z]|$)/.test(t.text) && !pm;
        const parent = stack.length ? stack[stack.length - 1] : '';

        if (!pm && !isIndex) {
            if (/path=\{`/.test(t.text)) {
                rows.push({ path: '(模板生成 · /s/<module>)', line: t.line, kind: 'template', el: 'SocialStudioProtected' });
            } else {
                unparsed.push({ line: t.line, snippet: t.text.replace(/\s+/g, ' ').slice(0, 90) });
            }
            if (!selfClosing) stack.push(parent || '/');
            continue;
        }
        const raw = isIndex ? '' : pm[1];
        const full = isIndex ? (parent || '/') : joinPath(parent || '', raw) || '/';
        /*
         * 🔴 element 里第一个组件名往往是**包装**(`ProtectedRoute` / `Suspense`),
         *    真正的页面在里面。取**最后一个**非包装名 —— 取第一个的话,
         *    总表上会出现一排 `ProtectedRoute`,分不出这是哪一页。
         */
        const WRAP = new Set(['ProtectedRoute', 'Suspense', 'ErrorBoundary', 'React']);
        const names = [...t.text.matchAll(/<([A-Z][A-Za-z0-9_]*)/g)].map((m) => m[1])
            .filter((n) => n !== 'Route');
        const real = [...names].reverse().find((n) => !WRAP.has(n)) || names[0] || '(未识别)';
        rows.push({ path: full, line: t.line, kind: 'literal', el: real });
        if (!selfClosing) stack.push(full);
    }
}

/** 分类。顺序要紧:先客户端与 admin,剩下的才是服务商端。 */
function classify(p) {
    if (p.startsWith('(模板')) return 'social';
    if (/^\/(landing|login|register|terms|privacy|agreement-update|change-password)/.test(p)) return 'public';
    if (/^\/public\//.test(p) || /^\/q\//.test(p) || /^\/portal\//.test(p)) return 'customer';
    if (/^\/c(\/|$)/.test(p) || /^\/customer\//.test(p)) return 'customer';
    if (/^\/admin(\/|$)/.test(p)) return 'admin';
    if (/^\/s(\/|$)/.test(p) || /^\/social(\/|$)/.test(p)) return 'social';
    return 'agent';
}

for (const r of rows) r.group = classify(r.path);

const byGroup = {};
for (const r of rows) (byGroup[r.group] ||= []).push(r);

if (process.argv.includes('--json')) {
    console.log(JSON.stringify({ rows, unparsed }, null, 2));
    process.exit(0);
}

console.log('#198 路由分母(App.tsx 的 Route 表机械枚举)');
console.log(`  源码里 <Route 出现 ${tags.length} 处`);
console.log(`  解析出 ${rows.length} 条 · 没解析出 ${unparsed.length} 条`);
console.log(`  多余的 </Route>(栈空时出现):${stray.length ? stray.join(',') : '无'}`);
console.log('');
for (const g of ['agent', 'admin', 'customer', 'public', 'social']) {
    const list = (byGroup[g] || []);
    console.log(`  ${g.padEnd(9)} ${String(list.length).padStart(3)} 条`);
}
console.log('');
if (unparsed.length) {
    console.log('🔴 没解析出来的(不许并进「其它」,逐条列出来):');
    for (const u of unparsed) console.log(`   App.tsx:${u.line}  ${u.snippet}`);
    console.log('');
}
console.log('服务商端(本单要查的那批):');
for (const r of (byGroup.agent || [])) {
    console.log(`  ${r.path.padEnd(42)} ${r.el}   (App.tsx:${r.line})`);
}
