/**
 * 全局错误上报 · Deploy-CTO 2026-05-06
 *
 * 抓:
 * - window.error          (JS 运行时 / 同步异常 / 资源加载错)
 * - window.unhandledrejection (未捕获 Promise reject)
 *
 * 上报到现有 /api/analytics/event(复用 trackEvent · batch 50 · debounce 5s · keepalive)
 *
 * 节流:50 次/分钟上限 · 防错误洪水自爆带宽
 *
 * 不抓:
 * - React 渲染错误 · main.tsx ErrorBoundary 已处理
 *
 * 配套 admin 后台查询 SQL:
 *   SELECT event, pathname, metadata, created_at FROM m3_analytics_events
 *   WHERE event IN ('frontend_error','unhandled_rejection')
 *   ORDER BY created_at DESC LIMIT 100;
 */
import { trackEvent } from './analytics';
import { startChunkHeal } from './chunkHeal';
import { guardedSilentReload, isAnythingDirty } from './dirtyGuard';
import { showUpdateBanner } from './versionPoll';

const RATE_LIMIT = 50;
const WINDOW_MS = 60 * 1000;
const bucket: number[] = [];

// chunk error 自动 reload · 解决"用户停留期间部署 · 旧 chunk hash 404"
// 配合 versionPoll(主动检测)+ ErrorBoundary chunk reload(React 树内捕获)三层防护
const CHUNK_ERR_RE = /Failed to fetch dynamically imported module|Loading chunk \d+ failed|ChunkLoadError|error loading dynamically imported|Importing a module script failed/i;
const CHUNK_RELOAD_KEY = 'omnirank_chunk_reload_at';
const CHUNK_RELOAD_COOLDOWN_MS = 30 * 1000;

/** 是否是"部署后旧 chunk 失效"这一类错误。
 *
 * [2026-07-28] 导出成单一来源。此前同一条正则在 `main.tsx`、本文件各有一份,
 * 而 `App.tsx` 的 `GlobalErrorBoundary` 一份都没有 —— React 错误边界**就近捕获**,
 * 内层的 App 边界先接住,外层 main.tsx 的静默 reload 根本没机会跑,
 * 用户于是看到一整页 stack trace。别再造第四份拷贝。
 */
export function isChunkLoadError(reason: unknown): boolean {
    const message = reason instanceof Error ? reason.message : String(reason ?? '');
    return CHUNK_ERR_RE.test(message);
}

/** chunk 失效 → 静默 reload。30s cooldown 防循环;返回是否真的排了 reload。 */
export function maybeReloadOnChunkError(reason: string): boolean {
    if (!CHUNK_ERR_RE.test(reason)) return false;
    try {
        const last = Number(sessionStorage.getItem(CHUNK_RELOAD_KEY) || 0);
        if (Date.now() - last < CHUNK_RELOAD_COOLDOWN_MS) return false;  // 30s 内已 reload 过 · 防循环
    } catch { return false; }
    // 🔴 [WO_NO_SILENT_RELOAD 2026-08-16 ①] 先问脏表单守卫。
    //   病史:一天四班车,每次部署都替换 /assets ⇒ 老 bundle 的懒加载 chunk 404
    //   ⇒ 这里 500ms 后静默 reload,用户填了一半的表单直接没。
    //   一旦部署频次高于用户填表时长,这条路就会稳定地吃掉用户输入 —— 今天就是。
    //   守卫拦下时改弹 banner(与版本轮询同一个),选择权还给用户;
    //   没人在填时行为一字不变(chunk 404 不自愈 = 用户卡在坏页面上,那更糟)。
    //
    //   🔴 cooldown 的写入放在守卫**之后**:被拦下时不该消耗那 30s 配额,
    //   否则用户点完 banner 前的这段时间里,真正需要自愈的场景会被误判成"刚 reload 过"。
    //
    // 🔴 [WO_276 · 2026-09-23] 刷新之前先把浏览器缓存里的坏响应刷掉(lib/chunkHeal.ts 抬头有病史):
    //   ESA 曾把 chunk 缓存成「301 指向自己」,浏览器把 301 永久缓存 —— 不先刷缓存,
    //   reload 只是把同一份坏响应再读一遍。自愈在脏表单守卫**之前**就开始:
    //   有人在填表时这里不刷新(改弹 banner),但缓存照样先修好,用户点 banner 刷新时就能好。
    //   自愈永不 reject、总时限 8s,到点照走刷新(用户不干等)。
    const healed = startChunkHeal(reason);
    const scheduled = guardedSilentReload(
        () => {
            // 🔴 [R2 · Codex 复现②] 500ms 是一个**窗口**,不是一个瞬间。
            //   排定时器那一刻用户可能确实没在填,但他完全可能在这 500ms 内开始打字 ——
            //   真机复现到的就是这个。⇒ 回调里**再问一次守卫**,这次脏了就改弹 banner。
            //   [WO_276] 等自愈完(或到时限)再开这 500ms 窗口,守卫仍是最后一刻才问。
            void healed.then(() => setTimeout(() => {
                if (isAnythingDirty()) {
                    try { showUpdateBanner(); } catch { /* banner 弹不出也不能刷掉输入 */ }
                    return;
                }
                window.location.reload();
            }, 500));   // 500ms 给 trackEvent keepalive 留时间
        },
        showUpdateBanner,
    );
    if (!scheduled) return false;
    try {
        sessionStorage.setItem(CHUNK_RELOAD_KEY, String(Date.now()));
    } catch { /* 写不进去不影响本次已经排好的 reload */ }
    return true;
}

function shouldReport(): boolean {
    const now = Date.now();
    while (bucket.length > 0 && now - bucket[0] >= WINDOW_MS) bucket.shift();
    if (bucket.length >= RATE_LIMIT) return false;
    bucket.push(now);
    return true;
}

function truncate(v: unknown, max: number): string {
    if (v == null) return '';
    const s = typeof v === 'string' ? v : String(v);
    return s.length > max ? s.slice(0, max) : s;
}

export function installErrorReporter(): void {
    if (typeof window === 'undefined') return;

    window.addEventListener('error', (e: ErrorEvent) => {
        if (!shouldReport()) {
            maybeReloadOnChunkError(e.message || '');  // 节流不阻断 reload
            return;
        }
        try {
            trackEvent('frontend_error', {
                message: truncate(e.message, 500),
                filename: truncate(e.filename, 200),
                lineno: e.lineno,
                colno: e.colno,
                stack: truncate(e.error?.stack, 1500),
                user_agent: truncate(navigator.userAgent, 200),
            });
        } catch { /* graceful */ }
        maybeReloadOnChunkError(e.message || '');
    });

    window.addEventListener('unhandledrejection', (e: PromiseRejectionEvent) => {
        let reasonText = '';
        try {
            const reason = e.reason as { message?: string; stack?: string } | string | null;
            reasonText = typeof reason === 'object' && reason
                ? (reason.message || JSON.stringify(reason).slice(0, 500))
                : String(reason || '');
        } catch { /* graceful */ }
        if (!shouldReport()) {
            maybeReloadOnChunkError(reasonText);
            return;
        }
        try {
            trackEvent('unhandled_rejection', {
                reason: truncate(reasonText, 500),
                stack: truncate((e.reason as { stack?: string })?.stack, 1500),
                user_agent: truncate(navigator.userAgent, 200),
            });
        } catch { /* graceful */ }
        maybeReloadOnChunkError(reasonText);
    });
}
