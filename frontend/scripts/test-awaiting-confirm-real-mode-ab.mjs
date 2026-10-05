#!/usr/bin/env node
/**
 * 判据 · #191 —— **真实态(非沙盒)行为未变**的 A/B 实测臂。
 *
 * #191 从 `AwaitingConfirmDialog` 里删掉了整层沙盒教程分支(`tutorialLock`
 * 与三处 FeatureTooltip)。我的论证是「这些分支的前提都是 `sandboxActive`,
 * 非沙盒下恒假,所以真实态等价」。
 *
 * 🔴 但这条组件是**动钱的面**(「取消发布并退款」/「一键取消全部并退款」),
 *    而我改的正是那两颗按钮的 `disabled` 表达式。
 *    「读着等价」和「跑起来等价」是两件事(static sees could, runtime sees does):
 *    少抄一个 `saving ||`,tsc 不报错、类门不报错、屏幕上却多一次可点的退款。
 *    所以这里把 **改动前(git HEAD 的那份)** 与 **改动后(工作树这份)**
 *    同时挂进同一个真浏览器、同一份 dist 真 CSS,逐态比 DOM。
 *
 * 三个态:列表态(空/有项)、编辑态(点开第一项)。每态比:
 *   ① 规范化后的 outerHTML 完全一致;
 *   ② 每颗按钮的 `disabled` 实测值逐一相等(HTML 一致其实已含它,
 *      但单独列出来,报错时能直接看出是哪颗按钮)。
 *
 * 🔴 反向控制 A0:故意给两边**喂不同的 items**,必须**报不一致**。
 *    没有它,"两边一致"可能只是比较器坏了(比了空字符串 / 比了同一个容器)。
 *
 * 🔴 旧版从 `git show <base>:` 取(base 默认:工作树有改动时 = HEAD,否则 = 最后改过
 *    这个文件那一笔的父提交;也可 `--base=<rev>` 指定),**落到 src 下一个临时文件**
 *    (别名 `@/` 要能解析),
 *    finally 里删掉;跑完自己 `git status` 一眼。中途别 kill。
 *
 * 🔴 **不要把本臂加进 build 链**:它证的是「那一笔改动没动真实态」,
 *    是**一次性等价证明**,不是常驻闸 —— 常驻的那部分是 #190 的类门(G1a/G1b/G1c)。
 *    但它**必须可复现**:提交之后自动回退到那一笔的父提交作基线,复审的人照样跑得出来。
 *
 * 跑法:cd frontend && npm run build && node scripts/test-awaiting-confirm-real-mode-ab.mjs
 *      注毒自证:node scripts/mutation_runner_191.mjs
 */
import {
    mkdirSync, mkdtempSync, writeFileSync, rmSync, readFileSync, readdirSync, statSync, existsSync,
} from 'node:fs';
import { createServer } from 'node:http';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execFileSync } from 'node:child_process';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const REL = 'frontend/src/components/publishing/AwaitingConfirmDialog.tsx';
const OLD_REL = 'src/components/publishing/__ab_old_AwaitingConfirmDialog.tsx';
const OLD_ABS = join(ROOT, OLD_REL);

const require_ = createRequire(import.meta.url);
let failed = 0;
const ok = (m, d = '') => console.log(`  OK   ${m}${d ? ` — ${d}` : ''}`);
const bad = (m, d = '') => { console.log(`  FAIL ${m}${d ? ` — ${d}` : ''}`); failed++; };
const check = (c, m, d = '') => (c ? ok(m, d) : bad(m, d));
const section = (t) => console.log(`\n=== ${t} ===`);
/**
 * 报不一致时把**首处差异的上下文**打出来。
 * 🔴 只说「@733」没法判断是真差异还是我的规范化漏了一类随机 id ——
 *    而把"我的比较器噪声"当成"产品回归"去改代码,比不查更糟。
 */
function diffWhere(a, b) {
    if (a === b) return '一致';
    let i = 0;
    while (i < a.length && i < b.length && a[i] === b[i]) i += 1;
    const w = (t) => JSON.stringify(t.slice(Math.max(0, i - 60), i + 90));
    return `首处差异 @${i}\n       改动前 ${w(a)}\n       改动后 ${w(b)}`;
}
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 1500));
    process.exit(1);
}

let esbuild, playwright;
try { esbuild = require_('esbuild'); playwright = require_('playwright'); }
catch (err) { unusable('esbuild / playwright 取不到', err); }

console.log('A/B · #191 待确认弹窗真实态等价(改动前 vs 改动后)');

/* ── 取改动前那一份 ────────────────────────────────────────────────
 * 🔴 比的必须是 **HEAD 的那份**,不是我记忆里的那份。
 *    若工作树这个文件没有改动,A/B 就退化成"自己比自己"—— 那是恒绿,要报不可用。
 */
const git = (args) => execFileSync('git', args,
    { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }).trim();
const newSrc = readFileSync(join(ROOT, 'src/components/publishing/AwaitingConfirmDialog.tsx'), 'utf8');

/*
 * 🔴 基线怎么选:**未提交时** = `HEAD`;**已提交后** = 最后改过这个文件那一笔的**父提交**。
 *    不这么做的话,#191 一提交本臂就永远自报不可用 —— 复审的人没法复现,
 *    而"不可复现的绿"等于没量过。也可以 `--base=<rev>` 手工指定。
 */
let base = (process.argv.find((a) => a.startsWith('--base=')) || '').slice(7);
if (!base) {
    let headSrc = '';
    /* 🔴 这里**不能用 git()** —— 它 trim 过,跟工作树原文比永远不等,
       于是"已提交"也会被判成"有改动",基线选成 HEAD、A/B 自比自己。
       (第一版就是这么错的:一个 .trim() 把自动回退整条路废掉。) */
    try {
        headSrc = execFileSync('git', ['show', `HEAD:${REL}`],
            { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
    } catch { headSrc = ''; }
    if (headSrc && headSrc !== newSrc) base = 'HEAD';
    else {
        try {
            /* 🔴 pathspec 必须用 `:/` 前缀锚到**仓库根**:本脚本的 cwd 是 frontend/,
               直接传 'frontend/src/...' 会被当成 frontend/frontend/src/... ⇒ git log 返回空
               ⇒ 悄悄退回 'HEAD^'。那不是"没有历史",是我把路径写错了,
               而它只会体现为一个**选错参照物**的绿/红。 */
            const last = git(['log', '-n', '1', '--format=%H', '--', `:/${REL}`]);
            if (!last) unusable(`git log 查不到改过 ${REL} 的提交 ⇒ basisline 选不出来`);
            base = `${last}^`;
        } catch (err) { unusable('推不出基线提交', err); }
    }
}
let oldSrc = '';
try {
    oldSrc = execFileSync('git', ['show', `${base}:${REL}`],
        { cwd: ROOT, encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
} catch (err) { unusable(`取不到 ${base}:${REL}`, err); }
console.log(`  (基线 = ${base} · 对照 = 工作树)`);
if (oldSrc === newSrc) {
    unusable(`${base} 与工作树这份**完全相同** ⇒ A/B 在比同一份代码,恒绿。`
        + '换一个 --base=<rev>(#191 那一笔的父提交)再跑。');
}
/* 旧版里那一层必须真的在(否则我就是在比两份都已清干净的代码) */
for (const needle of ['tutorialLock', 'sandbox_pub_await_process']) {
    if (!oldSrc.includes(needle)) {
        unusable(`改动前那一份里找不到 \`${needle}\` ⇒ 基线不是我以为的那份`);
    }
}
/* 组件改名,好让两份能同时 import */
const oldRenamed = oldSrc.replace(
    /export function AwaitingConfirmDialog\(/,
    'export function AwaitingConfirmDialogOld(');
if (oldRenamed === oldSrc) unusable('改不动旧版的导出名 ⇒ 两份没法同时挂');

const CACHE_ROOT = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE_ROOT, { recursive: true });
const outDir = mkdtempSync(join(CACHE_ROOT, 'a191-ab-'));
let server = null;
let browser = null;
const cleanup = () => {
    try { if (existsSync(OLD_ABS)) rmSync(OLD_ABS, { force: true }); } catch { /* 尽力 */ }
    try { rmSync(outDir, { recursive: true, force: true }); } catch { /* 尽力 */ }
};
process.on('exit', cleanup);

if (existsSync(OLD_ABS)) unusable(`${OLD_REL} 已存在 —— 上一次跑没清干净,先手动删掉再跑`);
writeFileSync(OLD_ABS, oldRenamed, 'utf8');

const q = (p) => JSON.stringify(p.split('\\').join('/'));

writeFileSync(join(outDir, 'entry.tsx'), `
import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import { AuthProvider } from ${q(join(ROOT, 'src/context/AuthContext.tsx'))};
import { AwaitingConfirmDialog } from ${q(join(ROOT, 'src/components/publishing/AwaitingConfirmDialog.tsx'))};
import { AwaitingConfirmDialogOld } from ${q(OLD_ABS)};

/* AuthProvider 是必须的:authFetch 会 await 会话确认(awaitConfirmedSessionToken),
   没有它详情请求**永远不发出**,编辑器就一直停在加载态 —— 第一版就是这么空跑的。
   两份用的是同一层壳,所以不影响等价比较。 */
const Stack = ({ children }: any) => (
    <MemoryRouter initialEntries={['/publish']}><AuthProvider>{children}</AuthProvider></MemoryRouter>
);
(globalThis as any).__mount = function (el: HTMLElement, which: string, items: any[]) {
    const Cmp: any = which === 'old' ? AwaitingConfirmDialogOld : AwaitingConfirmDialog;
    createRoot(el).render(<Stack><Cmp items={items} open onClose={() => {}} /></Stack>);
};
`, 'utf8');

/* vite 的 `?raw` 导入:esbuild 不认,自己解析成 text loader(与其他 harness 同一份写法)。 */
const rawSuffixPlugin = {
    name: 'vite-raw-suffix',
    setup(build) {
        build.onResolve({ filter: /\?raw$/ }, (args) => {
            const bare = args.path.replace(/\?raw$/, '');
            const abs2 = bare.startsWith('@/') ? join(ROOT, 'src', bare.slice(2)) : join(args.resolveDir, bare);
            return { path: abs2, namespace: 'vite-raw' };
        });
        build.onLoad({ filter: /.*/, namespace: 'vite-raw' }, (args) => ({
            contents: readFileSync(args.path, 'utf8'), loader: 'text',
        }));
    },
};
try {
    await esbuild.build({
        entryPoints: [join(outDir, 'entry.tsx')], bundle: true, outfile: join(outDir, 'bundle.js'),
        format: 'iife', platform: 'browser', jsx: 'automatic', alias: { '@': join(ROOT, 'src') },
        define: { 'process.env.NODE_ENV': '"development"', global: 'globalThis' },
        loader: {
            '.tsx': 'tsx', '.ts': 'ts', '.jpg': 'dataurl', '.jpeg': 'dataurl',
            '.png': 'dataurl', '.webp': 'dataurl', '.svg': 'dataurl',
        },
        plugins: [rawSuffixPlugin], logLevel: 'silent',
    });
} catch (err) { unusable('打包失败', (err && err.message) || err); }

/* 🔴 dist 真 CSS:没有它,disabled 的视觉与尺寸都量不了,而 HTML 比较也会少掉
   Tailwind 生成的类冲突合并结果(cn/twMerge 的输出仍在 HTML 里,但样式臂会恒真)。 */
let cssName = '';
try {
    const dir = join(ROOT, 'dist', 'assets');
    const c = readdirSync(dir).filter((f) => f.startsWith('index-') && f.endsWith('.css'))
        .map((f) => ({ f, m: statSync(join(dir, f)).mtimeMs })).sort((a, b) => b.m - a.m);
    if (!c.length) throw new Error('dist/assets 无 index-*.css');
    cssName = c[0].f;
    writeFileSync(join(outDir, 'app.css'), readFileSync(join(dir, cssName), 'utf8'), 'utf8');
} catch (err) {
    unusable('取不到真 CSS —— 先 npm run build', err);
}
console.log(`  (真 CSS:dist/assets/${cssName})`);

writeFileSync(join(outDir, 'index.html'), `<!doctype html>
<html lang="zh"><head><meta charset="utf-8"><title>a191-ab</title>
<link rel="stylesheet" href="./app.css"></head>
<body><div id="root"></div><script src="./bundle.js"></script></body></html>`, 'utf8');

const MIME = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
};
server = createServer((req, res) => {
    const n = decodeURIComponent((req.url || '/').split('?')[0]).replace(/^\/+/, '') || 'index.html';
    try {
        const b = readFileSync(join(outDir, n));
        res.writeHead(200, { 'Content-Type': MIME[extname(n)] || 'application/octet-stream' });
        res.end(b);
    } catch { res.writeHead(404); res.end('x'); }
});
await new Promise((r) => server.listen(0, '127.0.0.1', r));
const PORT = server.address().port;

const ITEMS = [
    {
        item_id: 9001, order_id: 7001, article_id: 5001,
        article_title: 'QA 测试稿件 · 甲',
        media_id: 301, media_name: 'QA 媒体甲', media_type: 'wemedia',
        brand_id: 629, pending_codes: [203], pending_msg: '',
        awaiting_since: '2026-09-13T00:00:00Z',
    },
    {
        item_id: 9002, order_id: 7002, article_id: 5002,
        article_title: 'QA 测试稿件 · 乙',
        media_id: 302, media_name: 'QA 媒体乙', media_type: 'soft',
        brand_id: 629, pending_codes: [204, 205], pending_msg: '这是发布通道给的原话',
        awaiting_since: null,
    },
];
const DETAIL = {
    status: 'success',
    item: ITEMS[0],
    article: { id: 5001, content: '这是一篇 QA 正文,里面有一个敏感词 特价 用来触发高亮。' },
    sensitive_keywords: ['特价'],
};
const ME = {
    success: true,
    user: {
        id: 112, username: 'qa', role: 'user', agent_level: 1,
        is_admin: false, permissions: [], user_mode: 'agent',
    },
    id: 112, username: 'qa', role: 'user', agent_level: 1,
    is_admin: false, permissions: [], user_mode: 'agent',
};

/**
 * Radix / React 每次挂载生成的随机 id 与 aria 引用要抹掉,否则两边永远不相等。
 * 🔴 只抹**确定是随机**的东西。`aria-hidden` / `pointer-events` 这类**语义属性不抹** ——
 *    抹了就等于把"弹窗被别的弹窗盖住了"这种真差异也一起放过。
 *    (它们之前确实在报红,但根因是我把两份同时挂在一页 ⇒ 改成逐份分开挂。)
 */
const NORMALIZE = `(html) => html
    .replace(/radix-[-:\\w]+/g, 'RADIX')
    .replace(/\\sid="[^"]*"/g, ' id="ID"')
    .replace(/aria-(?:controls|labelledby|describedby)="[^"]*"/g, 'aria-REF="REF"')
    .replace(/\\s+/g, ' ')
    .trim()`;

/** 抓一个容器里所有按钮的 (文字, disabled),顺序即 DOM 顺序。 */
const BUTTONS = `(el) => Array.from(el.querySelectorAll('button'))
    .map((b) => ((b.textContent || '').replace(/\\s+/g, ' ').trim() || '(无文字)')
        + ' :: disabled=' + (b.disabled ? 1 : 0))`;

browser = await playwright.chromium.launch();

/**
 * 挂**一份**并读数。
 * 🔴 一页只挂一份:两份同时挂,Radix 会把先挂的那个 dialog 标成
 *    aria-hidden + pointer-events:none(modal 互斥),读数全是仪器噪声。
 */
async function capture(which, items, opts = {}) {
    const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
    const noise = [];
    page.on('pageerror', (e) => noise.push(`pageerror: ${String(e.message).slice(0, 200)}`));
    page.on('console', (m) => {
        if (m.type() === 'error') noise.push(`console: ${m.text().slice(0, 200)}`);
    });
    /* 🔴 真实态 = **不设** omnirank_sandbox_active。这是本臂的全部前提,
       所以顺带把它读回来断言一次(见 A0b),别只靠"我没写那行"。 */
    await page.addInitScript(() => {
        localStorage.setItem('omnirank_token', 'qa-token');
        localStorage.removeItem('omnirank_sandbox_active');
        localStorage.removeItem('omnirank_sandbox_tutorial_stage');
    });
    const seenPaths = [];
    await page.route('**/api/**', async (route) => {
        const path = new URL(route.request().url()).pathname;
        seenPaths.push(path);
        const json = (b) => route.fulfill({
            status: 200, contentType: 'application/json', body: JSON.stringify(b),
        });
        if (path.includes('/auth/me')) return json(ME);
        if (/\/awaiting-confirmations\/\d+\/detail$/.test(path)) return json(DETAIL);
        /* 🔴 holdSaving:把保存接口**拖住**,好让 `saving === true` 这个态停下来被读。
           没有这一格,A/B 只采样到 saving=false —— `disabled={saving || resolving}`
           少抄一个 `saving ||` 也照样全绿(Y1 那发毒当场证明了这点:够不着)。 */
        if (opts.holdSaving && /\/api\/articles\/\d+$/.test(path)) {
            await new Promise((r) => setTimeout(r, 6000));
            return json({ success: true, status: 'success' });
        }
        return json({ success: true, status: 'success' });
    });
    await page.goto(`http://127.0.0.1:${PORT}/index.html`);
    await page.evaluate(([w, its]) => {
        const root = document.getElementById('root');
        root.innerHTML = '<div id="host"></div>';
        globalThis.__mount(document.getElementById('host'), w, its);
    }, [which, items]);
    await page.waitForTimeout(1200);
    if (opts.enterEdit) {
        const clicked = await page.evaluate(() => {
            const d = document.querySelector('[role="dialog"]');
            if (!d) return 0;
            const btn = Array.from(d.querySelectorAll('button'))
                .find((b) => (b.textContent || '').includes('去处理'));
            if (!btn) return 0;
            btn.click();
            return 1;
        });
        if (!clicked) return { which, clicked: 0, count: 0, noise, seenPaths };
        await page.waitForTimeout(1800);
    }
    if (opts.holdSaving) {
        /* 点「仅保存」→ 保存接口被拖住 ⇒ saving 停在 true。读数时那颗
           「取消发布并退款」必须是 disabled。 */
        const hit = await page.evaluate(() => {
            const d = document.querySelector('[role="dialog"]');
            if (!d) return 0;
            const btn = Array.from(d.querySelectorAll('button'))
                .find((b) => (b.textContent || '').trim() === '仅保存');
            if (!btn) return 0;
            btn.click();
            return 1;
        });
        if (!hit) return { which, clicked: 1, savingClicked: 0, count: 0, noise, seenPaths };
        await page.waitForTimeout(900);
    }
    const r = await page.evaluate(([norm, btns]) => {
        const dialogs = Array.from(document.querySelectorAll('[role="dialog"]'));
        const N = eval(norm);
        const B = eval(btns);
        return {
            count: dialogs.length,
            html: dialogs.length ? N(dialogs[0].outerHTML) : '',
            buttons: dialogs.length ? B(dialogs[0]) : [],
            textareas: dialogs.length ? dialogs[0].querySelectorAll('textarea').length : 0,
            sandbox: localStorage.getItem('omnirank_sandbox_active'),
        };
    }, [NORMALIZE, BUTTONS]);
    await page.close();
    return {
        which, clicked: opts.enterEdit ? 1 : 0,
        savingClicked: opts.holdSaving ? 1 : 0, ...r, noise, seenPaths,
    };
}

try {
    section('A0 反向控制 + 前提自证');
    let ctrlOld;
    let ctrlNew;
    {
        ctrlOld = await capture('old', [ITEMS[0]]);
        ctrlNew = await capture('new', ITEMS);        // 故意喂不同的 items
        check(ctrlOld.count === 1 && ctrlNew.count === 1,
            'A0a 分母自证:两份**都真的挂上了**(各一页一个 dialog)',
            `old ${ctrlOld.count} / new ${ctrlNew.count}`);
        check(!ctrlOld.sandbox && !ctrlNew.sandbox,
            'A0b 前提自证:沙盒**没开**(真实态)',
            `omnirank_sandbox_active=${ctrlOld.sandbox}`);
        check(ctrlOld.html !== ctrlNew.html,
            'A0c 🔴 反向控制:喂不同的 items 时**必须报不一致** —— '
            + '恒等的话说明比较器坏了(比了空串 / 比了同一个容器),下面每条"一致"都不算数',
            `长度 ${ctrlOld.html.length} vs ${ctrlNew.html.length}`);
        const noise = [...ctrlOld.noise, ...ctrlNew.noise];
        check(noise.length === 0,
            'A0d 控制台无报错 —— 有报错说明组件在这套桩下没正常跑,读数别当真',
            noise.slice(0, 3).join(' / ') || '干净');
    }

    section('A1 列表态(有 2 项)');
    {
        const a = await capture('old', ITEMS);
        const b = await capture('new', ITEMS);
        check(a.count === 1 && b.count === 1, 'A1a0 分母自证:两份都挂上了',
            `old ${a.count} / new ${b.count}`);
        check(a.buttons.length >= 5,
            'A1a1 分母自证:列表态里**真的有一堆按钮**可比(取消/去处理/一键取消/稍后处理)',
            `${a.buttons.length} 颗`);
        check(a.html === b.html,
            'A1b 🔴 列表态 DOM **逐字一致**(规范化随机 id 之后)',
            diffWhere(a.html, b.html));
        check(JSON.stringify(a.buttons) === JSON.stringify(b.buttons),
            'A1c 🔴 每颗按钮的 disabled 实测值相等 —— '
            + '这里面有两颗是**动钱的**(取消发布 / 一键取消全部并退款)',
            b.buttons.join(' | '));
    }

    section('A2 列表态(空列表)');
    {
        const a = await capture('old', []);
        const b = await capture('new', []);
        check(a.count === 1 && b.count === 1, 'A2a 分母自证:两份都挂上了',
            `old ${a.count} / new ${b.count}`);
        check(a.html === b.html, 'A2b 空列表态 DOM 一致', diffWhere(a.html, b.html));
        check(JSON.stringify(a.buttons) === JSON.stringify(b.buttons),
            'A2c 空列表时按钮 disabled 一致(「一键取消全部」应为 disabled=1)',
            b.buttons.join(' | '));
    }

    section('A3 编辑态(点开第一项 · 原 FeatureTooltip 包着编辑器的那一层)');
    {
        const a = await capture('old', ITEMS, { enterEdit: true });
        const b = await capture('new', ITEMS, { enterEdit: true });
        check(a.clicked === 1 && b.clicked === 1,
            'A3a0 分母自证:两份的「去处理 →」都点到了');
        check(a.textareas === 1 && b.textareas === 1,
            'A3a1 分母自证:两份都**真的进了编辑态**(各有 1 个 textarea)—— '
            + '没进去的话比的是两个加载态,等价是假的',
            `textarea old ${a.textareas} / new ${b.textareas}`
            + `${a.textareas ? '' : ` · 请求过的路径:${[...new Set(a.seenPaths)].join(',')}`}`
            + `${a.noise.length ? ` · 控制台:${a.noise.slice(0, 2).join(' / ')}` : ''}`);
        check(a.html === b.html,
            'A3b 🔴 编辑态 DOM 一致 —— 这一态原来有 `wrapClassName` 的包装 div,'
            + '删壳后必须仍然同形(LazyFeatureTooltip 在 disabled 时本来就不渲染那个 div)',
            diffWhere(a.html, b.html));
        check(JSON.stringify(a.buttons) === JSON.stringify(b.buttons),
            'A3c 🔴 编辑态按钮 disabled 一致(含「取消发布并退款」/「仅保存」/「保存并仍然发布」)',
            b.buttons.join(' | '));
    }
    section('A4 保存中(saving === true)· 动钱按钮最容易少抄条件的那个态');
    {
        const a = await capture('old', ITEMS, { enterEdit: true, holdSaving: true });
        const b = await capture('new', ITEMS, { enterEdit: true, holdSaving: true });
        check(a.count === 1 && b.count === 1 && a.textareas === 1 && b.textareas === 1,
            'A4a0 分母自证:两份都进了编辑态',
            `dialog ${a.count}/${b.count} · textarea ${a.textareas}/${b.textareas}`);
        /*
         * 🔴 这一条是**采样自证**:必须真的抓到 saving===true 的那一瞬。
         *    抓不到的话 A4b/A4c 就退化成又一次 saving=false 的比较 —— 恒绿。
         *    判据:「仅保存」按钮此刻应显示"保存中..."。
         */
        const inSaving = (x) => x.buttons.some((t) => t.startsWith('保存中...'));
        check(inSaving(a) && inSaving(b),
            'A4a1 🔴 采样自证:**真的停在 saving 中**(按钮文字变成"保存中...")—— '
            + '抓不到这个态,下面两条就是又一次 saving=false 的比较,恒绿',
            `old ${a.buttons.join(' | ')}`);
        check(a.html === b.html, 'A4b 保存中 DOM 一致', diffWhere(a.html, b.html));
        check(JSON.stringify(a.buttons) === JSON.stringify(b.buttons),
            'A4c 🔴 保存中每颗按钮 disabled 一致 —— '
            + '「取消发布并退款」此刻必须是 disabled=1(少抄一个 `saving ||` 就会变 0)',
            b.buttons.join(' | '));
        check(b.buttons.some((t) => t.startsWith('取消发布并退款') && t.endsWith('disabled=1')),
            'A4d 🔴 正面锚:保存中时「取消发布并退款」**确实是灰的** —— '
            + '光比"两边一样"不够,两边**一起坏**也会一样',
            b.buttons.find((t) => t.startsWith('取消发布并退款')) || '(没找到这颗按钮)');
    }
} catch (err) {
    unusable('跑挂了', (err && err.stack) || err);
} finally {
    try { if (browser) await browser.close(); } catch { /* 尽力 */ }
    try { if (server) server.close(); } catch { /* 尽力 */ }
    cleanup();
}

console.log('');
if (failed > 0) {
    console.log(`FAIL A/B ${failed} 条不过 —— 真实态不等价或判据不可信,先别交`);
    process.exit(1);
}
console.log('全部通过:真实态(非沙盒)下改动前后 DOM 与按钮 disabled 逐项一致');
process.exit(0);
