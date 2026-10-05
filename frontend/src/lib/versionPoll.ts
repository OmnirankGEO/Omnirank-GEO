/**
 * 前端版本轮询 · Deploy-CTO 2026-05-06 · CTO-15.23 2026-05-19 升级
 *
 * 解决场景:
 * - 移动端用户没"强制刷新"功能 · 微信 WebView 激进 cache 老 HTML
 * - 用户停留期间部署 · 60s 内自动检测到新版 · 顶部 banner 提示用户刷新
 * - iOS Safari bfcache(从其他 app/页面 back 回来)· 直接拿老页面 → 立即比对 → 静默 reload
 * - tab 切换回前台 → 立即比对一次
 *
 * [CTO-15.23 2026-05-19 升级] 老板 2026-05-19 报"服务器部署好了 · 用户刷新没效果":
 *   旧版用 HEAD / + etag 比对:
 *   - fetch('/') 无 cache-buster · 浏览器 disk cache 可能劫持返 304 + 老 etag
 *   - baseline 启动时拉 · 如果首次就被 cache 劫持 · 后续永远比对老 etag · 永不触发 banner
 *   修法:
 *   - 改用 chunk hash 比对(从 DOM 取当前页面跑的 /assets/index-XXX.js · 跟服务端 / 拉的 HTML 内引用的 hash 比)
 *   - 加 ?_v=${Date.now()} cache-buster · 100% bypass 浏览器 disk cache
 *   - chunk hash 是 vite content hash · 代码改了就变 · 比 etag 更可靠
 *
 * 实现:
 * - 启动时 getCurrentBuildId()(从 DOM 拿当前 chunk hash)当 baseline · 不再依赖网络拉
 * - setInterval 60s · GET `/?_v=${ts}` 解析新 HTML 拿新 chunk hash · 不一致 → banner
 * - pageshow e.persisted=true(bfcache 复活)→ 比对 · 不一致 → 静默 reload
 * - visibilitychange → tab 重新可见 → 立即比对
 *
 * 不依赖任何 React / sonner / 第三方组件 · 独立模块 · 0 dep
 */

import { requestHttpCacheRecovery } from '@/lib/cacheRecovery';
import { guardedSilentReload } from '@/lib/dirtyGuard';

const POLL_INTERVAL_MS = 60 * 1000;

let currentBuildId: string | null = null;
let timer: number | null = null;
let bannerShown = false;
let checkInFlight: Promise<void> | null = null;
let activeController: AbortController | null = null;

/** 从 DOM 拿当前页面跑的 chunk hash(vite content hash · 100% 反映 build 真实版本) */
function getCurrentBuildId(): string | null {
    try {
        // vite 产物固定 <script type="module" crossorigin src="/assets/index-XXXX.js"></script>
        const script = document.querySelector<HTMLScriptElement>('script[type="module"][src*="/assets/index-"]');
        const src = script?.src || '';
        const match = src.match(/\/assets\/(index-[A-Za-z0-9_-]+)\.js/);
        return match?.[1] || null;
    } catch {
        return null;
    }
}

/** 从服务端拉最新 / 的 HTML · 解析里面引用的 chunk hash · cache-buster query 防 disk cache 劫持 */
async function fetchLatestBuildId(signal: AbortSignal): Promise<string | null> {
    try {
        const res = await fetch(`/?_v=${Date.now()}`, {
            method: 'GET',
            cache: 'no-store',
            credentials: 'same-origin',
            signal,
            headers: { 'Pragma': 'no-cache', 'Cache-Control': 'no-cache' },
        });
        if (!res.ok) return null;
        const text = await res.text();
        const match = text.match(/\/assets\/(index-[A-Za-z0-9_-]+)\.js/);
        return match?.[1] || null;
    } catch {
        return null;
    }
}

/** [WO_NO_SILENT_RELOAD 2026-08-16] 导出:chunk 失效那条路被守卫拦下时复用同一个 banner。
 *  不另造第二个提示 —— 两条路对用户是同一件事(系统更新了),两个样式只会更乱。 */
export function showUpdateBanner(): void {
    if (bannerShown) return;
    bannerShown = true;
    try {
        if (document.getElementById('__omnirank_update_banner')) return;
        const div = document.createElement('div');
        div.id = '__omnirank_update_banner';
        div.style.cssText = [
            'position:fixed', 'top:12px', 'left:50%', 'transform:translateX(-50%)',
            'z-index:999999', 'background:#1f2937', 'color:#fff',
            'padding:10px 18px', 'border-radius:10px',
            'box-shadow:0 6px 20px rgba(0,0,0,0.35)',
            'font-size:13px', 'font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif',
            'display:flex', 'align-items:center', 'gap:12px',
            'max-width:90vw',
        ].join(';');
        const text = document.createElement('span');
        text.textContent = '系统已更新';
        const btn = document.createElement('button');
        btn.textContent = '立即刷新';
        btn.style.cssText = 'background:#10b981;color:#fff;border:0;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px;font-weight:600;';
        btn.addEventListener('click', () => window.location.reload());
        const closeBtn = document.createElement('button');
        closeBtn.textContent = '稍后';
        closeBtn.style.cssText = 'background:transparent;color:#9ca3af;border:0;padding:6px 8px;cursor:pointer;font-size:12px;';
        closeBtn.addEventListener('click', () => div.remove());
        div.appendChild(text);
        div.appendChild(btn);
        div.appendChild(closeBtn);
        document.body.appendChild(div);
    } catch {
        try {
            if (window.confirm('系统已更新 · 点击确定刷新加载最新版')) window.location.reload();
        } catch { /* ignore */ }
    }
}

async function checkVersion(silent: boolean = false): Promise<void> {
    if (document.hidden && !silent) return;
    if (checkInFlight) return checkInFlight;
    const controller = new AbortController();
    activeController = controller;
    const run = (async () => {
    if (!currentBuildId) return;  // 启动时 DOM 没 script 标签 · 跳过比对防误报
    const latest = await fetchLatestBuildId(controller.signal);
    if (!latest) return;  // 拉失败 · 跳过
    if (latest === currentBuildId) return;  // 一致 · 用户已是最新
    if (silent) {
        // bfcache 复活场景 · 用户预期就是"打开新页面" · 直接 hard reload
        // [CTO-15.23 2026-05-19] reload 前清 SW + caches API 防 SW 拦截返老资源
        //
        // 🔴 [WO_NO_SILENT_RELOAD 2026-08-16] 先问脏表单守卫。
        //   病史:用户在监测中心填品牌 → 切出去查资料 → 切回来触发 bfcache 复活 →
        //   这里静默 hardReload 把整屏输入刷没了(还跳首页)。
        //   「切出去查资料再回来」正是这条路最常见的真实触发方式,不是边缘场景。
        //   守卫只拦"有人正在填"的情况;没人填时**行为一字不变**(自愈别修死 ——
        //   微信 WebView 的激进缓存要靠它兜底)。
        guardedSilentReload(() => { void hardReload(); }, showUpdateBanner);
    } else {
        showUpdateBanner();
    }
    })();
    checkInFlight = run;
    try {
        await run;
    } finally {
        if (checkInFlight === run) checkInFlight = null;
        if (activeController === controller) activeController = null;
    }
}

/** 清 SW + caches API 后 hard reload(防 SW 拦截 chunks 返老版) */
async function hardReload(): Promise<void> {
    await requestHttpCacheRecovery();
    try {
        if ('serviceWorker' in navigator) {
            const regs = await navigator.serviceWorker.getRegistrations();
            await Promise.all(regs.map(r => r.unregister().catch(() => undefined)));
        }
    } catch { /* noop */ }
    try {
        if ('caches' in window) {
            const keys = await caches.keys();
            await Promise.all(keys.map(k => caches.delete(k).catch(() => undefined)));
        }
    } catch { /* noop */ }
    // 加 query 防浏览器 disk cache · location.reload(true) 已 deprecated · 走 href 重定向
    //
    // 🔴 [WO_NO_SILENT_RELOAD 2026-08-16 ②] 保当前路径,不许跳首页。
    //   原来写死 `/?_r=...`:用户在 /monitoring?brand_id=592 被复活刷新,
    //   醒来发现自己在首页 —— **丢表单之外还丢路由**,得从头找回刚才在哪。
    //   cache-buster 的作用只是绕开 disk cache,与"去哪个页面"无关,
    //   所以把它挂在当前 URL 上即可,search 里已有的参数一并保留。
    try {
        const url = new URL(window.location.href);
        url.searchParams.set('_r', String(Date.now()));
        window.location.href = url.pathname + url.search + url.hash;
    } catch {
        window.location.href = `/?_r=${Date.now()}`;   // URL 解析失败的极端兜底
    }
}

export function installVersionPoll(): void {
    if (typeof window === 'undefined') return;

    // 启动时立即从 DOM 拿当前 build id(同步 · 不依赖网络)· 100% 反映用户正在跑的版本
    currentBuildId = getCurrentBuildId();

    if (timer != null) window.clearInterval(timer);
    timer = window.setInterval(() => { void checkVersion(false); }, POLL_INTERVAL_MS);

    // iOS Safari bfcache 复活 · 立即比对 · 不一致 → 静默 reload(用户预期是"打开新页面")
    window.addEventListener('pageshow', (e: PageTransitionEvent) => {
        if (!e.persisted) return;
        void checkVersion(true);
    });

    // tab 切回前台 → 立即比对一次(用户可能切走数小时再回来)
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
            activeController?.abort();
            return;
        }
        void checkVersion(false);
    });
}
