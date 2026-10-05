#!/usr/bin/env node
/**
 * 判据 · WO_276 chunk 加载失败自愈(纯 node · 进 build 链)。行为臂(真 chromium · 真 HTTP 缓存)见
 * test-chunk-heal-render.mjs(挂 browser:arms)。
 *
 * 🔴 病史:ESA 把 chunk 缓存成「301 指向自己」,浏览器把 301 永久缓存;reload 不清这份缓存,
 *    用户卡在「正在更新到新版本」。修法 = 刷新前先对坏掉的块 fetch(url, {cache: 'reload'})。
 *    这一整件事的牙就是 `cache: 'reload'` —— 去掉它,fetch 会照样读到缓存里的坏响应。
 *
 * 单元格(把 src/lib/chunkHeal.ts 用 esbuild 转成 JS,在 node 里拿假 fetch 真跑):
 *   U1 🔴 每一次请求都带 cache:'reload';刷到的集合 = 报错点名的块 + 页面入口 / modulepreload
 *        + 入口 `__vite__mapDeps` 清单(js 与 css)+ 块间 `./X.js` 递归引用;别的站的脚本一个不碰
 *   U2 🔴 真正坏掉的是**被依赖的**块(报错只点名顶层模块):只出现在清单里的 / 只被顶层块引用的,都刷到了
 *   U3 并发上限 6(20 个块、每个 20ms:最大在途 = 6,且真的并行 > 1)
 *   U4 总时限:有一个请求永不返回时,到时限照样返回(timedOut = true),其余块照刷
 *   U5 从报错文本取模块 URL:Chrome / Firefox 取得到,Safari(不带 URL)给 null,句末标点不带进来
 *   U6 sessionStorage 记「已自愈」:自动路径一个窗口内只跑一次;「立即刷新」(force)不受限
 *   U7 永不 reject:fetch 同步抛、异步拒、回 500,都只记进 failed
 * 结构格(读源码):
 *   S1 🔴 errorReporter.maybeReloadOnChunkError 先起自愈、刷新等自愈完 —— 刷新动作仍在 guardedSilentReload(...) 实参里
 *   S2 🔴 App.tsx 错误边界「立即刷新」按钮:先 startChunkHeal(..., { force: true }),.then 里才 reload
 *   S3 🔴 chunkHeal.ts 的请求参数写着 cache: 'reload',且 healChunkCache 真用它发请求
 *   S4 并发常量 ≤ 6、总时限常量 ≤ 10s(Review:超时照走 reload,别让用户等)
 *
 * 三态退出码:0 全过 / 1 有失败 / 3 判据不可用。
 */
import { readFileSync, writeFileSync, mkdirSync, mkdtempSync, rmSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createRequire } from 'node:module';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const rd = (rel) => readFileSync(join(ROOT, rel), 'utf8');
let failed = 0;
const check = (c, m, d = '') => { console.log(`  ${c ? 'OK  ' : 'FAIL'} ${m}${d ? ` — ${d}` : ''}`); if (!c) failed += 1; };
function unusable(why, detail) {
    console.log(`FAIL 判据不可用(不当绿灯):${why}`);
    if (detail) console.log(String(detail).slice(0, 800));
    process.exit(3);
}
/** 剥 // 与块注释(保字符串);判据只看代码,不看讲代码的话 */
function decomment(src) {
    let out = '';
    let q = null;
    for (let i = 0; i < src.length; i += 1) {
        const c = src[i];
        const n = src[i + 1];
        if (q) { out += c; if (c === '\\') { out += n ?? ''; i += 1; } else if (c === q) q = null; continue; }
        if (c === '"' || c === "'" || c === '`') { q = c; out += c; continue; }
        if (c === '/' && n === '/') { while (i < src.length && src[i] !== '\n') i += 1; out += '\n'; continue; }
        if (c === '/' && n === '*') { i += 2; while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) { if (src[i] === '\n') out += '\n'; i += 1; } i += 1; continue; }
        out += c;
    }
    return out;
}
/** 从 open 位置(指向左括号)取到配对右括号之间的文本 */
function balanced(text, open) {
    let depth = 0;
    for (let i = open; i < text.length; i += 1) {
        if (text[i] === '(') depth += 1;
        else if (text[i] === ')') { depth -= 1; if (depth === 0) return text.slice(open + 1, i); }
    }
    return '';
}

/* ── 把 chunkHeal.ts 转成可 import 的 JS ────────────────────────────── */
const require_ = createRequire(import.meta.url);
let esbuild;
try { esbuild = require_('esbuild'); } catch (err) { unusable('esbuild 取不到(npm ci --legacy-peer-deps)', err); }
const CACHE = join(ROOT, 'node_modules', '.cache');
mkdirSync(CACHE, { recursive: true });
const tmp = mkdtempSync(join(CACHE, 'a276-heal-'));
process.on('exit', () => { try { rmSync(tmp, { recursive: true, force: true }); } catch { /* 尽力 */ } });
/* 🔴 模块不存在 = 自愈不存在 ⇒ 各格判红(改前读数就是这样),不是「判据不可用」;
   只有文件在却转不成 / 缺导出时才算尺子坏了(rc=3)。 */
let H = null;
if (existsSync(join(ROOT, 'src/lib/chunkHeal.ts'))) {
    try {
        const js = esbuild.transformSync(rd('src/lib/chunkHeal.ts'), { loader: 'ts', format: 'esm', target: 'es2020' }).code;
        writeFileSync(join(tmp, 'chunkHeal.mjs'), js, 'utf8');
        H = await import(pathToFileURL(join(tmp, 'chunkHeal.mjs')).href);
    } catch (err) { unusable('src/lib/chunkHeal.ts 转不成 / 载不进来', err); }
    const API = ['healChunkCache', 'startChunkHeal', 'failedModuleUrl', 'chunkRefs'];
    if (API.some((k) => typeof H[k] !== 'function')) unusable(`chunkHeal.ts 缺导出:${API.filter((k) => typeof H[k] !== 'function').join(',')}`);
}

/* ── 假世界:一个站点的产物,一份按 URL 回文本的假 fetch ─────────────── */
const O = 'https://app.example';
function world(files, { delayMs = 0, hang = new Set(), boom = new Set() } = {}) {
    const calls = [];
    let inflight = 0;
    let maxInflight = 0;
    const fetchImpl = (url, init) => {
        calls.push({ url, init });
        if (boom.has(url)) throw new Error('同步抛');
        inflight += 1;
        maxInflight = Math.max(maxInflight, inflight);
        return new Promise((resolve, reject) => {
            if (hang.has(url)) return;   // 永不返回
            setTimeout(() => {
                inflight -= 1;
                if (url.endsWith('/assets/reject.js')) { reject(new Error('异步拒')); return; }
                const body = files[url];
                const status = url.endsWith('/assets/e500.js') ? 500 : body === undefined ? 404 : 200;
                resolve({ ok: status < 400, status, text: async () => body ?? '' });
            }, delayMs);
        });
    };
    return { calls, fetchImpl, peak: () => maxInflight };
}
const A = (n) => `${O}/assets/${n}`;
const REASON = `TypeError: Failed to fetch dynamically imported module: ${A('Page-r1.js')}`;
const FILES = {
    [A('index-e1.js')]: 'const __vite__mapDeps=(i,m=__vite__mapDeps,d=(m.f||(m.f=["assets/Page-r1.js","assets/dep-shared.js","assets/Page-r1.css"])))=>i.map(i=>d[i]);'
        + 'const L=()=>import("./Lazy-noDeps.js");const P=()=>__vitePreload(()=>import("./Page-r1.js"),__vite__mapDeps([0,1,2]));',
    [A('vendor-v1.js')]: 'export const v=1;',
    /* dep-deep 只经 Page-r1 自己的导入可达;dep-shared 只在入口 mapDeps 清单里 —— 两条路各自单独可测 */
    [A('Page-r1.js')]: 'import{a as b}from"./dep-deep.js";export default b;',
    [A('Page-r1.css')]: '.x{}',
    [A('dep-shared.js')]: 'export const s=1;',
    [A('dep-deep.js')]: 'export const a=1;',
    [A('Lazy-noDeps.js')]: 'export default 1;',
};
const SEEDS = [A('index-e1.js'), A('vendor-v1.js'), 'https://cdn.other.example/sdk.js'];
const EXPECT = ['Page-r1.js', 'index-e1.js', 'vendor-v1.js', 'dep-shared.js', 'Page-r1.css', 'Lazy-noDeps.js', 'dep-deep.js'].map(A).sort();

console.log('U 单元(src/lib/chunkHeal.ts 在 node 里真跑)');
if (!H) {
    for (const id of ['U1', 'U2', 'U3', 'U4', 'U5', 'U6', 'U7']) check(false, `${id} 🔴 自愈模块 src/lib/chunkHeal.ts 不存在`, '没有自愈');
} else {
{
    const w = world(FILES);
    const r = await H.healChunkCache(REASON, { fetchImpl: w.fetchImpl, seeds: SEEDS, baseUrl: `${O}/` });
    const got = [...new Set(w.calls.map((c) => c.url))].sort();
    const allReload = w.calls.length > 0 && w.calls.every((c) => c.init && c.init.cache === 'reload');
    check(w.calls.length === got.length && JSON.stringify(got) === JSON.stringify(EXPECT) && allReload
        && !w.calls.some((c) => c.url.startsWith('https://cdn.other.example')),
        'U1 🔴 每次请求都带 cache:\'reload\';刷到 = 点名的块 + 入口 / modulepreload + mapDeps 清单(js/css)+ 递归 ./X.js;别站脚本一个不碰',
        `请求 ${w.calls.length} 次 · 带 reload ${w.calls.filter((c) => c.init && c.init.cache === 'reload').length} · `
        + `多了 ${got.filter((u) => !EXPECT.includes(u)).map((u) => u.replace(O, '')).join(',') || '无'} · `
        + `少了 ${EXPECT.filter((u) => !got.includes(u)).map((u) => u.replace(O, '')).join(',') || '无'}`);
    check(got.includes(A('dep-deep.js')) && got.includes(A('dep-shared.js')) && r.refreshed.includes(A('dep-deep.js')),
        'U2 🔴 坏掉的是被依赖的块也刷到:只被顶层块引用的 dep-deep、只在清单里的 dep-shared 都刷了(报错只点名 Page-r1)',
        `refreshed ${r.refreshed.length} · failed ${r.failed.length} · timedOut ${r.timedOut}`);
}
{
    /* 20 个块直接当种子(不经清单):这一格只量并发,不暗中依赖「读清单」那条机制 */
    const many = {};
    const urls = Array.from({ length: 20 }, (_, i) => A(`m${i}.js`));
    urls.forEach((u) => { many[u] = 'export default 1;'; });
    const w = world(many, { delayMs: 20 });
    const r = await H.healChunkCache('', { fetchImpl: w.fetchImpl, seeds: urls, baseUrl: `${O}/`, concurrency: 50 });
    check(w.peak() === 6 && r.refreshed.length === 20, 'U3 并发上限 6(20 个块各 20ms:最大在途恰为 6,即便调用方要 50)',
        `最大在途 ${w.peak()} · 刷到 ${r.refreshed.length}`);
}
{
    /* 挂住的块与两个正常块都直接当种子(不经清单) */
    const w = world({ [A('ok-1.js')]: 'export default 1;', [A('ok-2.js')]: 'export default 2;' }, { hang: new Set([A('hang.js')]) });
    const t0 = Date.now();
    /* 看门狗:时限若被拿掉,这里 3 秒后判红,而不是让整把尺子挂住 */
    const r = await Promise.race([
        H.healChunkCache('', { fetchImpl: w.fetchImpl, seeds: [A('hang.js'), A('ok-1.js'), A('ok-2.js')], baseUrl: `${O}/`, deadlineMs: 300 }),
        new Promise((res) => setTimeout(() => res({ timedOut: 'HANG', refreshed: [] }), 3000)),
    ]);
    const ms = Date.now() - t0;
    check(r.timedOut === true && ms >= 250 && ms < 1500 && r.refreshed.includes(A('ok-1.js')) && r.refreshed.includes(A('ok-2.js')),
        'U4 有一个请求永不返回:到时限照样返回(timedOut),其余块照刷', `${ms}ms · timedOut ${r.timedOut} · 刷到 ${r.refreshed.length}`);
}
{
    const cases = [
        [`TypeError: Failed to fetch dynamically imported module: ${A('X-1.js')}`, A('X-1.js')],
        [`TypeError: error loading dynamically imported module: ${A('Y-2.js')}.`, A('Y-2.js')],
        ['TypeError: Importing a module script failed.', null],
    ];
    const bad = cases.filter(([m, want]) => H.failedModuleUrl(m) !== want);
    check(bad.length === 0, 'U5 报错取 URL:Chrome / Firefox 取到(句末标点不带),Safari 不带 URL ⇒ null',
        bad.map(([m]) => `${m} → ${H.failedModuleUrl(m)}`).join(' | ') || `${cases.length} 例`);
}
{
    const store = new Map();
    const w = world(FILES);
    globalThis.window = { location: { origin: O, href: `${O}/login` }, fetch: w.fetchImpl };
    globalThis.document = {
        querySelectorAll: (sel) => (sel.startsWith('script')
            ? [{ src: A('index-e1.js') }]
            : sel.startsWith('link') ? [{ href: A('vendor-v1.js') }] : []),
    };
    globalThis.sessionStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
    const realFetch = globalThis.fetch;
    globalThis.fetch = w.fetchImpl;
    try {
        await H.startChunkHeal(REASON);
        const first = w.calls.length;
        await H.startChunkHeal(REASON);
        const second = w.calls.length - first;
        await H.startChunkHeal(REASON, { force: true });
        const forced = w.calls.length - first - second;
        check(first > 0 && store.has('omnirank_chunk_healed_at') && second === 0 && forced === first,
            'U6 sessionStorage 记「已自愈」:自动路径窗口内只跑一次;force(立即刷新)不受限',
            `第一次 ${first} 次请求 · 窗口内再调 ${second} · force ${forced}`);
    } finally {
        globalThis.fetch = realFetch;
        delete globalThis.window; delete globalThis.document; delete globalThis.sessionStorage;
    }
}
{
    /* 三种失败直接当种子(不经清单) */
    const w = world({}, { boom: new Set([A('boom.js')]) });
    let rejected = false;
    const r = await H.healChunkCache('', { fetchImpl: w.fetchImpl, seeds: [A('reject.js'), A('e500.js'), A('boom.js')], baseUrl: `${O}/` }).catch(() => { rejected = true; });
    check(!rejected && r && ['reject.js', 'e500.js', 'boom.js'].every((n) => r.failed.includes(A(n))),
        'U7 永不 reject:fetch 同步抛 / 异步拒 / 回 500 都只记进 failed', r ? `failed ${r.failed.length} · refreshed ${r.refreshed.length}` : '拒了');
}
}

console.log('\nS 结构(源码里自愈真接在错误出口上)');
{
    const src = decomment(rd('src/lib/errorReporter.ts'));
    const at = src.indexOf('export function maybeReloadOnChunkError');
    const body = at >= 0 ? src.slice(at, src.indexOf('\n}\n', at) + 2) : '';
    const healAt = body.search(/const\s+healed\s*=\s*startChunkHeal\(\s*reason\s*\)/);
    const gsrAt = body.search(/guardedSilentReload\s*\(/);
    const args = gsrAt >= 0 ? balanced(body, body.indexOf('(', gsrAt)) : '';
    const thenAt = args.search(/healed\.then\(/);
    const reloadAt = args.indexOf('window.location.reload()');
    check(/import\s*\{\s*startChunkHeal\s*\}\s*from\s*'\.\/chunkHeal'/.test(src) && healAt >= 0 && gsrAt > healAt
        && thenAt >= 0 && reloadAt > thenAt,
        'S1 🔴 maybeReloadOnChunkError 先起自愈(守卫之前),刷新动作在 guardedSilentReload(...) 实参里、且排在 healed.then( 之后',
        `起自愈 @${healAt} · 守卫 @${gsrAt} · then @${thenAt} · reload @${reloadAt}`);
}
{
    const src = decomment(rd('src/App.tsx'));
    const cls = src.indexOf('export class GlobalErrorBoundary');
    const branch = cls >= 0 ? src.indexOf('isChunkLoadError(this.state.error)', cls) : -1;
    const seg = branch >= 0 ? src.slice(branch, src.indexOf('isRecoverableSessionError(this.state.error)', branch)) : '';
    const btn = /<button\s+onClick=\{\(\)\s*=>\s*\{\s*void\s+startChunkHeal\([^;]*\{\s*force:\s*true\s*\}\)\.then\(\(\)\s*=>\s*window\.location\.reload\(\)\);\s*\}\s*\}/.test(seg);
    const plain = /onClick=\{\(\)\s*=>\s*\{[^}]*window\.location\.reload\(\)/.test(seg.replace(/\.then\(\(\)\s*=>\s*window\.location\.reload\(\)\)/g, ''));
    check(seg.includes('正在更新到新版本') && btn && !plain && /import\s*\{\s*startChunkHeal\s*\}\s*from\s*'@\/lib\/chunkHeal'/.test(src),
        'S2 🔴 错误边界「立即刷新」先 startChunkHeal(…, { force: true }),.then 里才 reload(不再是裸 reload)',
        `chunk 分支 ${seg ? '在' : '没找到'} · 先自愈再刷新 ${btn ? '是' : '否'} · 裸 reload ${plain ? '还有' : '无'}`);
}
{
    const src = H ? decomment(rd('src/lib/chunkHeal.ts')) : '';
    const init = /export\s+const\s+CHUNK_HEAL_FETCH_INIT\s*:\s*RequestInit\s*=\s*\{([^}]*)\}/.exec(src);
    const used = /env\.fetchImpl\(\s*url\s*,\s*CHUNK_HEAL_FETCH_INIT\s*\)/.test(src);
    check(!!init && /cache:\s*'reload'/.test(init[1]) && used,
        'S3 🔴 chunkHeal.ts 的请求参数写着 cache: \'reload\',healChunkCache 真用它发请求',
        `参数 ${init ? `{${init[1].trim()}}` : '没找到'} · 用上 ${used ? '是' : '否'}`);
}
check(!!H && H.CHUNK_HEAL_CONCURRENCY <= 6 && H.CHUNK_HEAL_DEADLINE_MS <= 10000 && H.CHUNK_HEAL_DEADLINE_MS >= 1000,
    'S4 并发常量 ≤ 6、总时限常量 ≤ 10s(超时照走刷新,用户不干等)',
    H ? `并发 ${H.CHUNK_HEAL_CONCURRENCY} · 时限 ${H.CHUNK_HEAL_DEADLINE_MS}ms` : '没有自愈模块');

console.log('');
if (failed > 0) { console.log(`FAIL ${failed} 项不通过`); process.exit(1); }
console.log('全部通过');
process.exit(0);
