#!/usr/bin/env node
/**
 * #198 「禁猜」存量普查 · **第 1 步:静态扫描**(只读,零产品改动)。
 *
 * 分母来自 `audit-198-routes.mjs`(198 条路由 · 服务商端 120 条)。
 * 这一层把每条服务商端路由映射到它的源码文件,逐文件扫五类。
 *
 * 🔴 **这是候选清单,不是判决**。静态扫描必然有假阳性(例如一个
 *    `<select>` 旁边其实画了剪影、只是不在同一段源码里)。所以:
 *    · 每条都带**文件 · 行号 · 原文片段**,能一眼复看;
 *    · 每条标 `confidence`(high/medium),medium 的必须人工看截图再下结论;
 *    · 报表里**不写"违规 N 条"**,写"候选 N 条、其中人工确认 M 条"。
 *    把候选当结论上交,就是拿一个看着精确的数字去替 Owner 做判断。
 *
 * ## 五类(质量标准第 11 条 1/2/3/4/6 款)
 *   1 裸选择         有形态的选项(风格/版式/模板/尺寸/封面)只给名字,不给样子
 *   2 无单位数字     步进器/数字输入旁没有单位,或改了之后的后果(加价/张数)不写
 *   3 折叠默认值     「高级/更多/调整」后面藏着会改变结果的默认项
 *   4 不可选无出口   disabled 控件旁没有原因,也没有去哪儿解决的链接
 *   5 有图不显示图   代码里明明有图片地址,界面上只给文字
 *
 * 跑法:cd frontend && node scripts/audit-198-noguess-scan.mjs [--json]
 */
import { readFileSync, existsSync, readdirSync, statSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (p) => readFileSync(join(ROOT, p), 'utf8');

/* ── 分母:路由表 ─────────────────────────────────────────────────── */
let routeData;
try {
    routeData = JSON.parse(execFileSync(process.execPath,
        [join(ROOT, 'scripts/audit-198-routes.mjs'), '--json'],
        { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }));
} catch (e) {
    console.log('FAIL 路由枚举器跑不起来 ⇒ 没有分母,本次普查无意义:'
        + String((e && e.message) || e).split('\n')[0]);
    process.exit(1);
}
const agentRoutes = routeData.rows.filter((r) => r.group === 'agent');

/* ── 组件名 → 源码文件(从 App.tsx 的 import / lazy 里取)───────────── */
const APP = rd('src/App.tsx');
const compFile = {};
/*
 * 🔴 本仓有**两种** lazy 写法,第一版只认第一种:
 *     ① `const X = lazy(() => import('@/pages/X'))`
 *     ② `const loadX = () => import('@/pages/X'); const X = lazy(loadX);`  ← 预加载用
 *    只认 ① 的话,`/dashboard`、`/publish`、`/monitoring`、`/` 这些**最常用的页**
 *    全部落进"映射不到源码"—— 普查表恰好漏掉最该查的那几页,而计数看起来仍然体面。
 */
for (const m of APP.matchAll(/const\s+([A-Z][A-Za-z0-9_]*)\s*=\s*lazy\(\s*\(\)\s*=>\s*import\(['"]([^'"]+)['"]\)/g)) {
    compFile[m[1]] = m[2];
}
{
    /* 写法②:先记 loader 名 → 路径,再把 `lazy(loadX)` 接上 */
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
const resolve = (spec) => {
    if (!spec) return '';
    const base = spec.startsWith('@/') ? join(ROOT, 'src', spec.slice(2))
        : spec.startsWith('.') ? join(ROOT, 'src', spec.replace(/^\.\//, '')) : '';
    if (!base) return '';
    for (const ext of ['.tsx', '.ts', '/index.tsx', '/index.ts']) {
        if (existsSync(base + ext)) return (base + ext).slice(join(ROOT, '').length);
    }
    return '';
};

/* ── 扫描规则 ────────────────────────────────────────────────────────
 * 🔴 每条规则都要能说出"看到了什么"。只给一个计数的规则没法复看,
 *    而没法复看的普查表,Owner 没办法用它挑页面。
 */
/* 🔴 "单位"不止量词:比例/倍数/秒/并发/上限 这些也回答了"这个数是什么"。
   第一版漏掉它们,于是把 `加价比例`、`默认文章数量` 报成没单位。 */
const UNIT = /(张|次|天|小时|分钟|秒|毫秒|ms|个|条|元|算力|积分|%|百分|篇|词|家|数量|比例|倍|并发|上限|阈值|超时|字数|长度)/;
const RULES = [
    {
        id: 1, name: '有形态的选项只给名字(第 11 条第 1 款)',
        /*
         * 🔴 工单的括号写得很清楚:**判据是「有形态的选项有没有样子」**。
         *    所以"选一个成员/选一个客户/选一个功能码"这种**没有形态**的下拉
         *    不属于这一款 —— 我第一版把 OrganizationCenter 里五个
         *    `选成员 / 选品牌 / 选功能码` 的 `<select>` 全报了进来,那是错的类。
         *    裸 `<select>`(与设计系统不一致)另记为"观察项",不混进违反款。
         */
        scan(text) {
            const hits = [];
            const FORM = /风格|版式|模板|尺寸|比例|封面|样式|排版|配色|字体|布局/;
            const SHOWN = /<img|svg|aspect-|剪影|预览|sample|Sample|backgroundImage/;
            /*
             * 🔴 **不能只查下拉**。本仓的风格/版式选择器大多是**自己画的按钮网格**
             *    (`CardStylePicker` 那种),根本不是 `<select>` / `<SelectItem>`。
             *    只查下拉的话这一款恒 0 —— 一个对它存在的目的**盲**的黑名单
             *    (今天已经栽过一次:L5a 的"发布"按钮黑名单被最普通的 `<button>` 绕过)。
             * 🔴 所以改成:出现形态词的地方,往前后各看一段,
             *    看这一段里**有没有在画一组可点的东西**(`.map(` + 可点元素),
             *    有的话再看**有没有给样子**。没给样子才进候选,且标 medium ——
             *    这一款最终要靠截图人工看,静态只负责把范围缩小。
             */
            for (const m of text.matchAll(FORM.source ? new RegExp(FORM.source, 'g') : FORM) ) {
                const win = text.slice(Math.max(0, m.index - 500), m.index + 900);
                const looksLikeOptions = /\.map\(/.test(win)
                    && /(<button|role="option"|SelectItem|ToggleGroupItem|onClick)/.test(win);
                if (!looksLikeOptions) continue;
                if (SHOWN.test(win)) continue;
                hits.push({
                    at: m.index,
                    snippet: win.replace(/\s+/g, ' ').slice(400, 520),
                    confidence: 'medium',
                });
            }
            return hits;
        },
    },
    {
        id: 6, name: '观察项:原生 <select>(与设计系统不一致,不一定是"猜")',
        scan(text) {
            const hits = [];
            for (const m of text.matchAll(/<select[^A-Za-z]/g)) {
                hits.push({ at: m.index, snippet: '<select>', confidence: 'observation' });
            }
            return hits;
        },
    },
    {
        id: 2, name: '数字没单位 / 没写改了的后果',
        /*
         * 🔴 **先列形态,再写锚**——这条我第一版就栽了。
         *    原来只查附近 260 字符里有没有「张/次/天/元…」,结果把
         *    `<Label>默认文章数量</Label>` + 「新建诊断时的默认文章生成数量」
         *    和 `<Label>加价比例</Label>` 都报成了"没单位"。手工复看三条,
         *    两条是**假阳性** —— 那不是"没单位",是我的单位表太窄。
         * 🔴 订正后的判据照 Owner 的原话来:**看不出改了它会怎样**才算。
         *    所以只要有 ① 说明这是什么的 `<Label>`,或 ② 紧跟的灰字说明,
         *    就不算候选。两者都没有的裸数字才进表。
         */
        scan(text) {
            const hits = [];
            for (const m of text.matchAll(/<[Ii]nput[^A-Za-z][^>]*type=["']number["'][^>]*>/g)) {
                const before = text.slice(Math.max(0, m.index - 320), m.index);
                const after = text.slice(m.index, m.index + 420);
                const hasLabel = /<Label[^>]*>[^<]{2,}<\/Label>/.test(before);
                const hasHelp = /text-xs text-muted-foreground[^>]*>[^<]{4,}/.test(after);
                const hasUnit = UNIT.test(before) || UNIT.test(after);
                if (hasLabel || hasHelp || hasUnit) continue;
                hits.push({
                    at: m.index,
                    snippet: m[0].replace(/\s+/g, ' ').slice(0, 110),
                    confidence: 'high',
                });
            }
            return hits;
        },
    },
    {
        id: 3, name: '折叠里藏着会改结果的默认值',
        scan(text) {
            const hits = [];
            for (const m of text.matchAll(/(高级设置|更多设置|更多选项|展开更多|高级选项)/g)) {
                const after = text.slice(m.index, m.index + 900);
                if (!/<[Ii]nput|<[Ss]elect|Switch|Slider|Checkbox|RadioGroup/.test(after)) continue;
                hits.push({ at: m.index, snippet: m[0], confidence: 'medium' });
            }
            return hits;
        },
    },
    {
        id: 4, name: '不可选但没说为什么、也没给出口',
        scan(text) {
            const hits = [];
            for (const m of text.matchAll(/\bdisabled=\{([^}]{0,160})\}/g)) {
                const around = text.slice(m.index, m.index + 420);
                /* 同段里有"理由/原因/为什么/去…"这类文字或链接就算给了出口 */
                if (/reason|Reason|理由|原因|先去|去补|去设置|<a\b|Link\b|title=/.test(around)) continue;
                /* 纯粹的"正在提交/加载中"不算(那是瞬时态,不是需要用户解决的事) */
                if (/submitting|loading|saving|pending|isBusy/i.test(m[1])) continue;
                hits.push({ at: m.index, snippet: `disabled={${m[1]}}`.replace(/\s+/g, ' ').slice(0, 110), confidence: 'medium' });
            }
            return hits;
        },
    },
    {
        id: 5, name: '有图不显示图',
        scan(text) {
            const hits = [];
            const hasUrl = /(cover_url|image_url|thumbnail|preview_url|oss_key|signed_url|card_urls)/.test(text);
            if (!hasUrl) return hits;
            if (/<img\b|backgroundImage|<Image\b/.test(text)) return hits;
            const m = text.match(/(cover_url|image_url|thumbnail|preview_url|oss_key|signed_url|card_urls)/);
            hits.push({ at: m.index, snippet: `代码里有 ${m[1]},界面上没有 <img>`, confidence: 'high' });
            return hits;
        },
    },
];

const lineOf = (text, at) => text.slice(0, at).split(String.fromCharCode(10)).length;

/* ── 逐路由扫 ────────────────────────────────────────────────────── */
const pages = new Map();          // file -> {routes:[], findings:[]}
const unresolved = [];
for (const r of agentRoutes) {
    const file = resolve(compFile[r.el]);
    if (!file) { unresolved.push(`${r.path}  (组件 ${r.el})`); continue; }
    if (!pages.has(file)) pages.set(file, { routes: [], findings: [] });
    pages.get(file).routes.push(r.path);
}
for (const [file, info] of pages) {
    let text;
    try { text = rd(file); } catch { info.readError = '读不到文件'; continue; }
    for (const rule of RULES) {
        for (const h of rule.scan(text)) {
            info.findings.push({
                rule: rule.id, ruleName: rule.name, line: lineOf(text, h.at),
                snippet: h.snippet, confidence: h.confidence,
            });
        }
    }
}

if (process.argv.includes('--json')) {
    console.log(JSON.stringify({
        agentRouteCount: agentRoutes.length,
        pageCount: pages.size,
        unresolved,
        pages: [...pages].map(([file, i]) => ({ file, ...i })),
    }, null, 2));
    process.exit(0);
}

console.log('#198 禁猜普查 · 静态扫描(候选清单,不是判决)');
console.log(`  服务商端路由 ${agentRoutes.length} 条 → 映射到 ${pages.size} 个页面文件`);
console.log(`  🔴 映射不到源码的 ${unresolved.length} 条(逐条列出,不许并进「其它」):`);
for (const u of unresolved) console.log(`      ${u}`);
console.log('');

const all = [...pages].flatMap(([file, i]) => i.findings.map((f) => ({ file, ...f })));
const byRule = {};
for (const f of all) (byRule[f.rule] ||= []).push(f);
console.log('按款计数(候选):');
for (const rule of RULES) {
    const list = byRule[rule.id] || [];
    const high = list.filter((x) => x.confidence === 'high').length;
    console.log(`  第 ${rule.id} 款 ${rule.name.padEnd(24)} ${String(list.length).padStart(3)} 条`
        + `(高把握 ${high})`);
}
console.log('');
console.log('按页面(候选数降序,前 25):');
const ranked = [...pages].map(([file, i]) => ({ file, n: i.findings.length, routes: i.routes }))
    .filter((x) => x.n > 0).sort((a, b) => b.n - a.n).slice(0, 25);
for (const p of ranked) {
    console.log(`  ${String(p.n).padStart(3)} 条  ${p.file}`);
    console.log(`         路由:${p.routes.slice(0, 4).join(' , ')}${p.routes.length > 4 ? ' …' : ''}`);
}
