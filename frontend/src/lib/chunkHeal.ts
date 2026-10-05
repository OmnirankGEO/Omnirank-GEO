/**
 * chunk 加载失败 → 先刷浏览器缓存,再刷新(WO_276 · 2026-09-23)
 *
 * 🔴 病史(2026-09-23 06:47–16:19 北京):ESA 边缘把现役 index 引用的 38 个 chunk URL 缓存成
 *    「301 指向自己」(源站 http→https 的 301 被回源撞上后缓存)。ESA purge 之后边缘已回 200,
 *    但**浏览器把 301 永久缓存了**:窗口内访问过的用户,懒加载那个 chunk 时直接用缓存里的 301
 *    自己跳自己 ⇒「Failed to fetch dynamically imported module」⇒ 落到「正在更新到新版本」。
 *    普通 reload /「立即刷新」**不清**这份缓存,刷多少次都一样。
 * 🔴 实证(Review 在卡住的测试浏览器里):对坏掉的 URL 跑 fetch(url, {cache: 'reload'}) ⇒ 200,
 *    浏览器缓存被新响应覆盖,再进页面即正常渲染。缓存里是 404 这类坏响应时同理。
 * 🔴 坏掉的常是**被依赖的** chunk,不是报错点名的那个 —— 浏览器只报顶层模块的 URL。
 *    所以不只刷报错那一条:从页面入口 chunk 出发,把 `__vite__mapDeps` 的 f 清单(全站懒加载块)
 *    连同各块互相引用的 `./X.js` 递归刷一遍。`__vite__mapDeps` 是每个 chunk 模块作用域里的常量,
 *    运行时拿不到对象本身 ⇒ 读 chunk 源文本取清单(读的那一下,这个 chunk 自己也被刷新了)。
 *
 * 只在 chunk 加载失败这条路上跑,正常加载零开销。并发 ≤ 6、总时限 8s:到点不等,照走刷新。
 * 调用方:lib/errorReporter.ts 的 maybeReloadOnChunkError(唯一出口)与 App.tsx 的「立即刷新」按钮。
 * 本文件不 import 任何东西 —— 判据 scripts/test-chunk-heal.mjs 直接在 node 里跑它。
 */

/** 🔴 自愈的全部意义在 `cache: 'reload'`:绕过缓存去网络取,并用新响应覆盖缓存里的坏响应。 */
export const CHUNK_HEAL_FETCH_INIT: RequestInit = { cache: 'reload', credentials: 'same-origin' };
export const CHUNK_HEAL_CONCURRENCY = 6;
export const CHUNK_HEAL_DEADLINE_MS = 8000;
/** 自动自愈在一个标签页里多久最多跑一次(「立即刷新」不受限)—— 防「自愈 → 刷新 → 又坏 → 再自愈」空转。 */
export const CHUNK_HEAL_WINDOW_MS = 5 * 60 * 1000;
export const CHUNK_HEALED_KEY = 'omnirank_chunk_healed_at';

export interface ChunkHealEnv {
    fetchImpl: (url: string, init: RequestInit) => Promise<Response>;
    /** 页面上的入口脚本与 modulepreload(绝对 URL) */
    seeds: string[];
    /** 站点 base 的绝对 URL,用来解析清单里的 "assets/X.js" */
    baseUrl: string;
    deadlineMs?: number;
    concurrency?: number;
}

export interface ChunkHealResult {
    refreshed: string[];
    failed: string[];
    timedOut: boolean;
}

/** 报错文本里的模块 URL(Chrome / Edge / Firefox 带;Safari 的「Importing a module script failed.」不带)。 */
export function failedModuleUrl(reason: string): string | null {
    const m = /https?:\/\/[^\s'"`<>()]+/.exec(reason || '');
    return m ? m[0].replace(/[.,;:]+$/, '') : null;
}

const MAP_DEP_RE = /["'](assets\/[A-Za-z0-9_.-]+\.(?:js|css))["']/g;
const SIBLING_RE = /["'](\.\/[A-Za-z0-9_.-]+\.(?:js|css))["']/g;

/** 一段 chunk 源文本引用的其它产物:`__vite__mapDeps` 清单里的 "assets/X"(相对站点 base)+ 块间导入的 "./X"(相对这个块)。 */
export function chunkRefs(text: string, chunkUrl: string, baseUrl: string): string[] {
    const out: string[] = [];
    for (const m of text.matchAll(MAP_DEP_RE)) {
        try { out.push(new URL(m[1], baseUrl).href); } catch { /* 解析不了就跳过 */ }
    }
    for (const m of text.matchAll(SIBLING_RE)) {
        try { out.push(new URL(m[1], chunkUrl).href); } catch { /* 同上 */ }
    }
    return out;
}

/**
 * 从报错点名的 URL 与页面入口出发,把同源的 .js / .css 产物逐个 `fetch(url, {cache: 'reload'})`,
 * 读 .js 的源文本继续找它引用的块。永不 reject;到时限就返回(在途的请求不等)。
 */
export function healChunkCache(reason: string, env: ChunkHealEnv): Promise<ChunkHealResult> {
    const deadlineMs = env.deadlineMs ?? CHUNK_HEAL_DEADLINE_MS;
    const concurrency = Math.max(1, Math.min(env.concurrency ?? CHUNK_HEAL_CONCURRENCY, CHUNK_HEAL_CONCURRENCY));
    const result: ChunkHealResult = { refreshed: [], failed: [], timedOut: false };
    let origin = '';
    try { origin = new URL(env.baseUrl).origin; } catch { return Promise.resolve(result); }

    const queue: string[] = [];
    const seen = new Set<string>();
    const push = (raw: string) => {
        let u: URL;
        try { u = new URL(raw, env.baseUrl); } catch { return; }
        if (u.origin !== origin || !/\.(?:m?js|css)$/.test(u.pathname)) return;   // 只刷本站产物
        const key = u.origin + u.pathname;
        if (seen.has(key)) return;
        seen.add(key);
        queue.push(key);
    };
    const named = failedModuleUrl(reason);
    if (named) push(named);                 // 报错点名的那条最先刷
    env.seeds.forEach(push);

    return new Promise<ChunkHealResult>((resolve) => {
        let active = 0;
        let done = false;
        const finish = () => { if (!done) { done = true; clearTimeout(timer); resolve(result); } };
        const timer = setTimeout(() => { result.timedOut = true; finish(); }, deadlineMs);
        const one = async (url: string) => {
            try {
                const res = await env.fetchImpl(url, CHUNK_HEAL_FETCH_INIT);
                if (!res.ok) { result.failed.push(url); return; }
                result.refreshed.push(url);
                if (/\.m?js$/.test(url)) chunkRefs(await res.text(), url, env.baseUrl).forEach(push);
            } catch {
                result.failed.push(url);
            }
        };
        const pump = () => {
            if (done) return;
            while (active < concurrency && queue.length) {
                const url = queue.shift() as string;
                active += 1;
                void one(url).finally(() => { active -= 1; pump(); });
            }
            if (active === 0 && queue.length === 0) finish();
        };
        pump();
    });
}

let inflight: Promise<void> | null = null;

/**
 * 浏览器入口:取页面上的入口脚本 / modulepreload 当种子,跑一次自愈。永不 reject。
 * 自动路径一个标签页 5 分钟内最多一次(sessionStorage 记「已自愈」);`force` =「立即刷新」按钮,不受限。
 */
export function startChunkHeal(reason: string, opts: { force?: boolean } = {}): Promise<void> {
    if (typeof window === 'undefined' || typeof document === 'undefined' || typeof fetch !== 'function') {
        return Promise.resolve();
    }
    if (inflight) return inflight;
    if (!opts.force) {
        try {
            const last = Number(sessionStorage.getItem(CHUNK_HEALED_KEY) || 0);
            if (Date.now() - last < CHUNK_HEAL_WINDOW_MS) return Promise.resolve();
        } catch { /* 存储不可用:照样自愈这一次 */ }
    }
    try { sessionStorage.setItem(CHUNK_HEALED_KEY, String(Date.now())); } catch { /* 写不进去不影响本次自愈 */ }
    const seeds: string[] = [];
    document.querySelectorAll<HTMLScriptElement>('script[type="module"][src]').forEach((s) => seeds.push(s.src));
    document.querySelectorAll<HTMLLinkElement>('link[rel="modulepreload"][href]').forEach((l) => seeds.push(l.href));
    let baseUrl = window.location.origin + '/';
    try { baseUrl = new URL(import.meta.env.BASE_URL || '/', window.location.href).href; } catch { /* 用站点根 */ }
    inflight = healChunkCache(reason, {
        fetchImpl: (url, init) => window.fetch(url, init),
        seeds,
        baseUrl,
    }).then(() => undefined, () => undefined).finally(() => { inflight = null; });
    return inflight;
}
