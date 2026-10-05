#!/usr/bin/env node
/**
 * 判据 · #165 死交互:点了没反应的按钮 / 点了白屏的链接。
 *
 * 三态退出码:**0 全过 / 1 有失败 / 3 有未评估**。
 *
 * 🔴 缺陷形态(真人点测在生产 a5878e567 上撞到的):
 *    ① 图文页「让 AI 先出选题」**没有 onClick** —— 点下去零反应、零提示;
 *    ② 同页唯一指引「去报价方案」指向 /quotes,路由表里没有这条,
 *       而全站又**没有未知路由兜底页** ⇒ 纯白屏;
 *    ③ 侧栏首项等七处指向 /dashboard/today,它 05-05 起只是一条重定向 ⇒
 *       已经在 /dashboard 上时点它页面不变,看起来就是死的。
 *
 * 🔴 三条都不产生错误信号:顺利路径全绿,只有真人去点才看得见。
 *    所以这里钉的是**类**,不是工单点名的那几个实例:
 *    · 死链锁的分母 = 全站 href/navigate/to 的**字面**目标,不是报告里那一条;
 *    · 死按钮锁的分母 = 那一页的**全部** Button,不是 distill 那一颗。
 *    (工单说 /dashboard/today 有三处,机械枚举出来是七处。)
 *
 * 🔴 排序用 matchRoutes **真跑**(它是纯函数,不需要浏览器),
 *    不靠"读 React Router 文档说静态段优先"。加了兜底 * 之后
 *    任何路径都能匹配,所以判死链时**必须把根 splat 排除掉** ——
 *    否则这把锁在兜底页落地那一刻就变成恒真。
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { RETIRED_ROUTES } from './lib/retired-routes.mjs';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');
const BACKSLASH = 92;
const STAR = 42;
const SLASH = 47;
/** 把一段替成等长空白,**保行号** —— 塌行会让报文指到不存在的位置。 */
const blank = (m) => m.replace(/[^\n]/g, ' ');
/**
 * 🔴 剥块注释时**必须认字符串**。
 *
 *    路由里的 `path="/c/*"` 含有 `/` 紧跟 `*` 这两个字符 —— 纯正则版把它当成
 *    注释开头,从那里一路吞到下一个注释结束符。本单实测:它吞掉了 **25 条**
 *    `<Route` 声明,于是死链锁报出 **205 条死链** —— 一个自信、具体、
 *    而且完全错误的大数字。被吞的正好是 `/c/*` `/s/*` `/m3/*` `/social/*`
 *    这几条全站兜底重定向,于是 `/social/…` `/m3/…` 全成了"死链"。
 *
 *    抓住它的不是我看代码,是 **B6 那条反臂**(真路由不许被判死)先红了 ——
 *    没有那条反臂,我会把 205 当成结论报出去。
 *
 *    行注释仍限定行首(老规矩),不会误伤 `https://`。
 */
function stripBlockComments(src) {
    let out = '';
    let i = 0;
    let q = null;
    while (i < src.length) {
        const c = src[i];
        if (q) {
            out += c;
            if (c === q && src.charCodeAt(i - 1) !== BACKSLASH) q = null;
            i += 1;
            continue;
        }
        const code = src.charCodeAt(i);
        if (c === '"' || c === "'" || c === String.fromCharCode(96)) { q = c; out += c; i += 1; continue; }
        if (code === SLASH && src.charCodeAt(i + 1) === STAR) {
            const e = src.indexOf(String.fromCharCode(STAR, SLASH), i + 2);
            const seg = src.slice(i, e === -1 ? src.length : e + 2);
            out += blank(seg);
            i += seg.length;
            continue;
        }
        out += c;
        i += 1;
    }
    return out;
}
const decomment = (s) => stripBlockComments(s).replace(/^\s*\/\/.*$/gm, blank);

let bad = 0;
let notEvaluated = 0;
const ok = (cond, label, detail) => {
    console.log(`${cond ? '  OK ' : '  FAIL'} ${label}${detail ? ' — ' + detail : ''}`);
    if (!cond) bad += 1;
};

// ══ M 元判据:仪器可用性先证,别让业务锁各自 SKIP ═══════════════════════
let matchRoutes;
try {
    ({ matchRoutes } = await import('react-router'));
    if (typeof matchRoutes !== 'function') throw new Error('matchRoutes 不是函数');
} catch (e) {
    console.log('  FAIL M0 🔴 加载不到 react-router 的 matchRoutes,排序与死链锁一条都跑不了:'
        + String((e && e.message) || e));
    console.log('       修法:npm ci --legacy-peer-deps(matchRoutes 是纯函数,不需要浏览器)。');
    process.exitCode = 1;
    process.exit(1);
}
console.log('  OK  M1 元判据:matchRoutes 可用(纯函数,无浏览器)');

// ══ 路由表:从 App.tsx 机械抽,不手写一份 ═══════════════════════════════
/** 从 `{` 起做括号配对(跳字符串),返回配对 `}` 的下标。 */
function matchBrace(src, open) {
    let d = 0;
    let q = null;
    for (let i = open; i < src.length; i += 1) {
        const c = src[i];
        if (q) {
            if (c === q && src.charCodeAt(i - 1) !== BACKSLASH) q = null;
            continue;
        }
        if (c === '"' || c === "'" || c === String.fromCharCode(96)) { q = c; continue; }
        if (c === '{') d += 1;
        else if (c === '}') { d -= 1; if (d === 0) return i; }
    }
    return -1;
}
/** 从 `<Route` 起的一小段里取 path="…";没有 path 但有 index ⇒ 空段;都没有 ⇒ null。 */
function segOf(head) {
    for (const q of ['path="', "path='"]) {
        const a = head.indexOf(q);
        if (a !== -1) {
            const b = head.indexOf(q[q.length - 1], a + q.length);
            if (b !== -1) return head.slice(a + q.length, b);
        }
    }
    return /\sindex[\s>/]/.test(head) ? '' : null;
}
/**
 * 抽路由表。
 *
 * 🔴 **不做通用 JSX 解析** —— 第一版那个「找标签结束的 `>`」的扫描器在
 *    `element={<Layout />}` 上会算错,而算错的表现是**安静地少一批路由**,
 *    看起来像"路由都没了"而不是"仪器坏了"。A0/A1 两格就是为这件事在的。
 *
 * 这一版只回答一个问题:每条 `<Route path="X">` 外面套着哪个分组。
 * 判分组的唯一依据是 `element={…}` 配对括号后紧跟的字符:`>` 是分组,`/` 是叶子。
 *
 * 🔴 并且**守住自己的假设**:本仓当前恰好一组(`/`)。[开源 E3 · 前端 · 2026-10-01] 原来是两组,
 *    `/social-ops` 那组随旧社媒操盘手整删。
 *    加了第三组这把锁会红,而不是安静地把新组的子路由前缀拼错。
 */
function extractRoutes(src) {
    const groups = [];
    const leaves = [];
    let i = 0;
    while ((i = src.indexOf('<Route', i)) !== -1) {
        // head 必须**止于下一条 <Route** —— 否则 `<Route index …/>` 会把下一条的
        // path= 读进来,产出一条重复路径(实测 /dashboard 出现两次)。
        const nxt = src.indexOf('<Route', i + 6);
        const head = src.slice(i, nxt === -1 ? i + 240 : Math.min(nxt, i + 240));
        const seg = segOf(head);
        if (seg === null) { i += 6; continue; }
        const el = src.indexOf('element=', i);
        let isGroup = false;
        let closeFrom = i;
        if (el !== -1 && el - i < 240) {
            const brace = src.indexOf('{', el);
            if (brace !== -1 && brace - el <= 9) {
                const mb = matchBrace(src, brace);
                if (mb !== -1) {
                    isGroup = src.slice(mb + 1, mb + 5).trim().startsWith('>');
                    closeFrom = mb;
                }
            }
        }
        if (isGroup) {
            const cl = src.indexOf('</Route>', closeFrom);
            groups.push({ path: seg, from: i, to: cl === -1 ? src.length : cl });
        } else {
            leaves.push({ seg, at: i });
        }
        i += 6;
    }
    const norm = (p) => ('/' + p).replace(/\/{2,}/g, '/').replace(/(.)\/$/, '$1');
    const out = leaves.map((lf) => {
        const g = groups.filter((x) => lf.at > x.from && lf.at < x.to)
            .sort((a, b) => b.from - a.from)[0];
        return {
            path: norm((g ? g.path + '/' : '') + lf.seg),
            line: src.slice(0, lf.at).split('\n').length,
            group: g ? g.path : '(top)',
        };
    });
    out.groupCount = groups.length;
    out.groupPaths = groups.map((g) => g.path);
    return out;
}
const APP = decomment(rd('src/App.tsx'));
const ROUTES = extractRoutes(APP);
const PATHS = [...new Set(ROUTES.map((r) => r.path))];

if (process.argv.includes('--dump')) {
    console.log(`GROUPS ${ROUTES.groupCount}: ${ROUTES.groupPaths.join(' | ')}`);
    for (const r of ROUTES) console.log(`DUMP L${r.line} [${r.group}] ${r.path}`);
    process.exit(0);
}

console.log('A 路由表');
ok(ROUTES.groupCount === 1 && ROUTES.groupPaths.join('|') === '/',
    'A0 🔴 分组仍是已知那一组 —— 加了新组必须回来重看前缀拼接,不许安静拼错',
    `${ROUTES.groupCount} 组:${ROUTES.groupPaths.join(' | ')}`);
{
    // 🔴 分母自证:App.tsx 里 `<Route` 出现几次,就该抽出几条(减去 <Router>/<Routes>
    //    与 .map() 里那条动态声明)。差得多 = 扫描器吞了东西,当场喊,别默默变绿。
    const declared = (rd('src/App.tsx').match(/<Route[\s>]/g) || []).length;
    ok(ROUTES.length >= declared - 6,
        'A1 🔴 抽出的条数 ≈ 源码里的 <Route 条数(差太多 = 扫描器吞了源码)',
        `声明 ${declared} · 抽出 ${ROUTES.length} · 唯一 ${PATHS.length}`);
}
{
    const splat = ROUTES.filter((r) => r.path === '/*');
    ok(splat.length === 1, 'A2a 🔴 未知路由兜底 * 恰好一条(0 条 = 白屏回来了)',
        `${splat.length} 条`);
    ok(splat.length === 1 && splat[0].group === '/',
        'A2b 🔴 兜底挂在**受保护组内**(顶层再挂就永远匹配不到 = 死路由)',
        splat.length ? `group=${splat[0].group} @App.tsx:${splat[0].line}` : 'n/a');
}
{
    // 🔴 真跑排序:静态段必须赢过 splat。这是加兜底页时最容易搞砸的地方。
    const routes = PATHS.map((p) => ({ path: p }));
    const shadowed = [];
    for (const probe of ['/dashboard', '/pricing', '/portal', '/writing', '/publish', '/monitoring']) {
        const m = matchRoutes(routes, probe);
        const hit = m && m.length ? m[m.length - 1].route.path : null;
        if (hit !== probe) shadowed.push(`${probe}→${hit}`);
    }
    ok(shadowed.length === 0,
        'A3 🔴 matchRoutes 实测:已知静态路径没被兜底 * 吃掉', shadowed.join(', ') || '六条全中自己');
}

// ══ B 死链:全站字面目标 ⊆ 路由表(排除根 splat) ═════════════════════════
const walk = (dir, out) => {
    for (const e of readdirSync(join(ROOT, dir), { withFileTypes: true })) {
        const p = dir + '/' + e.name;
        if (e.isDirectory()) { if (e.name !== 'node_modules') walk(p, out); }
        else if (/\.tsx?$/.test(e.name)) out.push(p);
    }
    return out;
};
/** 纯函数:扫出站内链接目标。判据与打印同源。 */
function scanLinks(sources) {
    const literal = [];
    const dynamic = [];
    const PAT = [
        /\bhref=\{?["'`](\/[^"'`\s{}]*)["'`]/g,
        /\bto=\{?["'`](\/[^"'`\s{}]*)["'`]/g,
        /\bnavigate\(\s*["'`](\/[^"'`\s]*)["'`]/g,
        /location\.href\s*=\s*["'`](\/[^"'`\s]*)["'`]/g,
    ];
    const DYN = [
        /\bhref=\{`[^`]*\$\{/,
        /\bto=\{`[^`]*\$\{/,
        /\bnavigate\(\s*`[^`]*\$\{/,
        /\bnavigate\(\s*[A-Za-z_$][\w$]*\s*\)/,
        /\bto=\{[A-Za-z_$][\w$.]*\}/,
    ];
    for (const { path, text } of sources) {
        const lines = decomment(text).split('\n');
        lines.forEach((L, i) => {
            for (const re of PAT) {
                for (const m of L.matchAll(re)) literal.push({ path, line: i + 1, t: m[1] });
            }
            if (DYN.some((re) => re.test(L))) dynamic.push({ path, line: i + 1, src: L.trim().slice(0, 70) });
        });
    }
    return { literal, dynamic };
}
/** 目标 → 可喂给 matchRoutes 的具体路径。`${…}` 用占位段替掉(路由里是 :param)。 */
const concrete = (t) => (t.split('#')[0].split('?')[0].replace(/\$\{[^}]*\}/g, '_') || '/');
const NON_ROUTE = (t) => t.startsWith('/api/') || t.startsWith('//') || t === '/';
function deadLinks(literal, paths) {
    const routes = paths.filter((p) => p !== '/*').map((p) => ({ path: p }));
    return literal.filter((x) => {
        if (NON_ROUTE(x.t)) return false;
        const m = matchRoutes(routes, concrete(x.t));
        return !m || m.length === 0;
    });
}
console.log('B 死链:站内目标 ⊆ 路由表');
{
    const scan = scanLinks(walk('src', []).map((p) => ({ path: p, text: rd(p) })));
    const dead = deadLinks(scan.literal, PATHS);
    ok(scan.literal.length > 50, 'B1 分母非空(扫到 0 条 = 仪器死了)',
        `字面 ${scan.literal.length} 处 · 动态 ${scan.dynamic.length} 处(判不了,不算通过)`);
    for (const d of dead.slice(0, 40)) console.log(`  ..   死链 ${d.path}:${d.line} → ${d.t}`);
    if (dead.length > 40) console.log(`  ..   (另有 ${dead.length - 40} 条未列出)`);
    // 🔴 存量红三态:这 6 条是本单**之前就在**的死链,都在社媒域(不是 A 的地界)。
    //    [WO_260] 原第 7 条(物料中心「去编辑」→ /profiles/:id/edit,路由从来不存在)已修:按钮删了,那一行随之删。
    //    不静默豁免、也不顺手改别人的页:冻成一张**只许变短**的表,每次都打印,
    //    并且 B2b 会盯着它 —— 谁修好了就必须把那一行删掉,不许留成死条目。
    //    (清单按 文件+目标 记,不记行号:行号会随无关改动漂,漂了这把锁就开始骗人。)
    // [开源 E3 · 前端 · 2026-10-01 · WO_322] 原 6 条全在社媒工作台设置页的推荐 / 钱包两个 tab 里,
    //   随社媒工作台整删 ⇒ 存量清零(宿主没了,不是链接被修活了)。表留着:以后真有存量死链照规矩登记。
    const STOCK = [];
    const inStock = (d) => STOCK.some((x) => x.f === d.path && x.t === d.t);
    const fresh = dead.filter((d) => !inStock(d));
    for (const x of STOCK) console.log(`  ..   存量死链(已记 #166,归社媒/素材域):${x.f} -> ${x.t}`);
    ok(fresh.length === 0, 'B2 🔴 没有**新增**死链(存量表只许变短;#166 的 6 条随社媒工作台删)',
        fresh.map((d) => `${d.path}:${d.line}=${d.t}`).join(', ') || `新增 0 · 存量 ${STOCK.length}`);
    const healed = STOCK.filter((x) => !dead.some((d) => d.path === x.f && d.t === x.t));
    ok(healed.length === 0,
        'B2b 存量表里没有**已经修好**的条目(修好了就删那一行,别留死条目)',
        healed.map((x) => x.f + '->' + x.t).join(', ') || `${STOCK.length} 条仍死`);
    // 🔴 正样本 + 反臂。B6 反臂是本单真正救命的一格:它先红,才让我发现
    //    "205 条死链"是我的注释剥离器吞了源码,而不是代码真有 205 条死链。
    const probe = (t) => deadLinks([{ path: 'p.tsx', line: 1, t }], PATHS).length;
    ok(probe('/nosuchroute') === 1, 'B3 正样本臂:写错的路径**判得出来**');
    ok(probe('/quotes') === 1, 'B4 正样本臂:#165 F2 那条原链接(/quotes)本来就该判死');
    ok(probe('/pricing') === 0, 'B5 反臂:真路由不许被误判死(误杀会让下一个人去放宽这把锁)');
    ok(probe('/my-clients/${id}') === 0, 'B6 🔴 反臂:带动态段的目标能匹配 :param,不算死链');
    // [开源 E3 · 前端] 原例 /social/* 随社媒删;换成仍在的兜底 splat /c/*
    ok(probe('/c/anything') === 0, 'B7 反臂:splat 路由(/c/*)覆盖的目标不算死链');
}

// ══ B8 [WO_260 · 2026-09-23] 壳链接在 E3 删域之后也不死 ═══════════════════════
// 🔴 上面 B 臂拿「今天的 App.tsx」判:要删的社媒 / M3 / C 端路由今天都还在(还有 /social/* /m3/* 这类兜底),
//    所以指向它们的链接今天判活 —— E3 一删就全是 404。这里把同一份 matchRoutes 判法换成
//    「今天的路由表 − 冻结的要删路由」(scripts/lib/retired-routes.mjs,与 test-route-literals-live 共用一份),
//    专扫壳(页头 / 外壳 / 侧栏):每页都挂着它们,点一遍不许 404。
//    侧栏项是对象(`{ to: '/x' }`),不是 JSX —— B 臂的 `to=` 模式看不见,这里补 `to:` / `href:` 两种属性写法。
console.log('B8 壳链接(E3 删域之后)');
{
    const retired = new Set(RETIRED_ROUTES.map((r) => r[0]));
    const postE3 = PATHS.filter((p) => !retired.has(p));
    const SHELL = ['src/components/layout/Header.tsx', 'src/components/layout/Layout.tsx', 'src/components/layout/AppSidebar.tsx'];
    const shellSources = SHELL.map((p) => ({ path: p, text: rd(p) }));
    const lits = scanLinks(shellSources).literal;
    for (const { path, text } of shellSources) {
        decomment(text).split('\n').forEach((L, i) => {
            for (const m of L.matchAll(/\b(?:to|href)\s*:\s*["'`](\/[^"'`\s]*)["'`]/g)) lits.push({ path, line: i + 1, t: m[1] });
        });
    }
    const deadAfter = deadLinks(lits, postE3);
    ok(retired.size === 105 && postE3.length > 100 && lits.length >= 30,
        'B8a 分母自证:要删路由 105 条读到了、E3 后的路由表非空、壳里扫到了站内目标(含侧栏对象项)',
        `要删 ${retired.size} · E3 后路由 ${postE3.length} · 壳目标 ${lits.length} 处`);
    ok(deadAfter.length === 0,
        'B8 🔴 页头 / 外壳 / 侧栏里的每个站内目标,在「今天路由表 − 要删路由」里都还活着(E3 删域后点了不 404)',
        deadAfter.map((d) => `${d.path}:${d.line}=${d.t}`).join(', ') || `${lits.length} 处全活`);
    const probeAfter = (t) => deadLinks([{ path: 'p.tsx', line: 1, t }], postE3).length;
    ok(probeAfter('/m3/queue') === 1 && probeAfter('/social/anything') === 1 && probeAfter('/my-clients/${id}') === 0,
        'B8b 牙证 + 对照:/m3/queue、/social/… 在 E3 之后判死;/my-clients/${id} 照旧活');
}

// ══ C 死按钮:那一页的全部 Button 都要能做事 ═════════════════════════════
console.log('C 死按钮');
{
    /*
     * 🔴 [#204 a2] 被扫的那一页换了。
     *    原来扫 `ImageNoteStudio.tsx`(制作台)—— 那块屏本单退役、文件已删。
     *    C1 原本钉的是「制作台里那颗无 onClick 的『让 AI 先出选题』已不在」:
     *    那是一个**位置**,随文件死(而且那件事已经反过来做成了:
     *    a1 的左栏真有一颗「生成选题 · N 算力」,接了线、异步、扣费前确认)。
     *
     *    C2 钉的是**命题**:「这一页每颗 Button 都要能做事」。命题没死,
     *    宿主换成了图文现在真正住的两块屏。
     *    🔴 扫描集合写成数组而不是一个文件:漏掉新屏的话,
     *       这把闸会以"没找到死按钮"的样子安静地不再保护任何东西。
     */
    const SCREENS = [
        'src/pages/Writing/ImageNoteTopicPanel.tsx',
        'src/pages/Writing/DouyinPostDetail.tsx',
        'src/pages/Writing/ImageNoteDetailRoute.tsx',
    ];
    const STUDIO = SCREENS.map((f) => decomment(rd(f))).join('\n');
    ok(SCREENS.length === 3 && STUDIO.length > 5000,
        `C0 分母自证:扫 ${SCREENS.length} 块屏、${STUDIO.length} 字符`
        + '(扫不到东西的扫描器"什么都没找到",和"确实没有"在报文上同形)',
        String(STUDIO.length));
    /** 纯函数:找出没有任何动作的 Button。 */
    const deadButtons = (src) => {
        const out = [];
        let i = 0;
        while ((i = src.indexOf('<Button', i)) !== -1) {
            /**
             * 🔴 [#188] 原来是 `src.indexOf('>', i)` 找标签结尾 —— **被 JSX 表达式里的 `>` 骗了**。
             *    `disabled={cardCount >= cardBounds.max}` 里那个 `>` 让标签在 onClick **之前**
             *    就被截断,一颗接线完好的步进器按钮被报成死按钮(本轮实测 ImageNoteStudio.tsx:627 假阳性;
             *    而同一对里用 `<=` 的那颗没事 —— "两颗同构按钮只红一颗"正是仪器坏了的形状)。
             *
             * 🔴 不能用"把窗口放宽到 400 字符"糊过去:那会把**下一颗** Button 的 onClick
             *    也吃进窗口,于是"真死按钮紧跟活按钮"读成两颗都活 —— 假阳性换成假阴性,
             *    而假阴性正是这把锁存在的理由。按**花括号深度**扫:标签结束于深度 0 的第一个 `>`。
             *    C3b / C3c 两条控制臂分别钉这两个方向。
             */
            let depth = 0, end = -1;
            for (let j = i + 7; j < src.length; j++) {
                const ch = src[j];
                if (ch === '{') depth++;
                else if (ch === '}') depth--;
                else if (ch === '>' && depth === 0) { end = j; break; }
            }
            const tag = src.slice(i, end === -1 ? src.length : end + 1);
            /*
             * 🔴 [#188] Radix 的 `<XTrigger asChild><Button …>` 里,按钮的行为由**外层
             *    Trigger** 挂上(asChild 把 props 合并进子元素),Button 自己身上没有 onClick。
             *    只看 Button 标签本身会把这种接线完好的按钮报成死按钮
             *    —— 区 E 那颗「···」就是(实测 @943)。
             * 🔴 放宽的**边界要窄**:只认"紧挨在前面的那个 asChild Trigger",
             *    真正的边界是正则里的 `>\s*$`:Trigger 的 `>` 与这颗 Button 之间**只许有空白**。
             *    窗口大小只是"够不够装下那行标签 + 缩进"(实测深缩进要 200),
             *    不是边界本身 —— 边界靠 `\s*$`。C3e 钉的就是这条:
             *    Trigger 只罩紧挨着的那一颗,它后面那颗仍然要被抓出来。
             */
            const before = src.slice(Math.max(0, i - 200), i);
            const wrapped = /Trigger[^>]*\basChild\b[^>]*>\s*$/.test(before);
            const acts = wrapped
                || /\bonClick=|\btype="submit"|\basChild\b|\bonPointerDown=/.test(tag);
            if (!acts) {
                out.push({
                    line: src.slice(0, i).split('\n').length,
                    tag: tag.slice(0, 60).replace(/\s+/g, ' '),
                });
            }
            i += 7;
        }
        return out;
    };
    const dead = deadButtons(STUDIO);
    for (const d of dead) console.log(`  ..   无动作 Button @${d.line}: ${d.tag}`);
    ok(dead.length === 0,
        'C2 🔴 图文那几块屏上每颗 Button 都有动作(onClick / submit / asChild)',
        dead.map((d) => '@' + d.line).join(', ') || '无');
    ok(deadButtons('<Button variant="outline">点我没用</Button>').length === 1,
        'C3 正样本臂:无 onClick 的 Button 检得出来(否则 C2 是空断言)');
    ok(deadButtons('<Button disabled={a >= b} onClick={f}>+</Button>').length === 0,
        'C3b 反臂:属性里带 `>=`(JSX 表达式内的 `>`)的**活**按钮不许误判死'
        + ' —— 这正是 #188 那颗被冤枉的步进器');
    ok(deadButtons('<DropdownMenuTrigger asChild><Button>···</Button></DropdownMenuTrigger>').length === 0,
        'C3d 🔴 反臂:Radix `Trigger asChild` 包着的 Button 不算死'
        + '(它的 onClick 由 Trigger 合并进来)');
    ok(deadButtons('<DropdownMenuTrigger asChild><Button onClick={f}>a</Button></DropdownMenuTrigger>'
        + '<Button>真死</Button>').length === 1,
        'C3e 🔴 正样本臂:Trigger 只罩住**紧挨着的**那一颗;'
        + '它后面那颗没人管的仍然要被抓出来 —— 挡"上面出现过 asChild 就全放行"');
    ok(deadButtons('<Button disabled={a >= b}>死</Button><Button onClick={f}>活</Button>').length === 1,
        'C3c 🔴 正样本臂:一颗**真死**按钮(属性含 `>=`)紧跟一颗活按钮时,仍然只报那颗死的'
        + ' —— 挡"把窗口放宽到吃掉下一颗 onClick"这种假修');
    ok(deadButtons('<Button onClick={() => f()}>行</Button>').length === 0,
        'C4 反臂:有 onClick 的不算死');
}

if (bad > 0) {
    console.log(`\nFAIL ${bad} 项不通过` + (notEvaluated ? ` · 另有 ${notEvaluated} 项未评估` : ''));
    process.exitCode = 1;
} else if (notEvaluated > 0) {
    console.log(`\n未完成:${notEvaluated} 项未评估`);
    process.exitCode = 3;
} else {
    console.log('\n全部通过');
    process.exitCode = 0;
}
