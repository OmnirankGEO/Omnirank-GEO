/**
 * 自然迁移率埋点 · CTO-15.18 PM 干预 D.10
 *
 * 老板红线(2026-04-28):
 * - M3 vs 旧版每页 PV / 切换次数 / 留存率
 * - 统计基础限定为新代理(注册 ≤ 30 天)
 * - 每周给老板报表 · 决定 M3 是否替代旧版
 *
 * KPI 验收(2026-05-26 周一拍板):
 * - 新代理(注册 ≤ 30 天)自然迁移率 > 60% = 替代旧版
 *
 * 用法:
 *   trackPageView('/m3/sales/today');
 *   trackEndpointSwitch('m3-to-legacy');
 *   trackEvent('m3_optout', { reason: 'click_back_to_legacy' });
 *
 * 数据上报后端 /api/analytics/event(批量 · debounce 5s)· 失败 graceful
 */

import { getConfirmedSessionToken } from '@/lib/authoritativeSession';

const EVENTS_QUEUE: AnalyticsEvent[] = [];
const FLUSH_DEBOUNCE_MS = 5000;
const FLUSH_MAX_BATCH = 20;
let flushTimer: number | null = null;

interface AnalyticsEvent {
    event: string;
    pathname: string;
    timestamp: string;
    user_id?: number;
    metadata?: Record<string, unknown>;
}

function getCurrentUserId(): number | undefined {
    try {
        const raw = localStorage.getItem('omnirank_user');
        if (!raw) return undefined;
        const u = JSON.parse(raw);
        return typeof u?.id === 'number' ? u.id : undefined;
    } catch {
        return undefined;
    }
}

function flush() {
    if (EVENTS_QUEUE.length === 0) return;
    // 🔴 2026-08-07:token 判断必须在 splice **之前**。
    //   原写法先 splice 再 `if (!token) return` —— 拿不到 token 时这批事件被静默吞掉,
    //   而支付埋点恰恰要在会话边界附近发,吞掉就等于没埋。
    const token = getConfirmedSessionToken() || '';
    if (!token) return;
    const batch = EVENTS_QUEUE.splice(0, FLUSH_MAX_BATCH);
    // 用 fetch + keepalive · 不阻塞 navigate / 关闭页(beacon-like)
    try {
        fetch('/api/analytics/event', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                ...(token ? { Authorization: `Bearer ${token}` } : {}),
            },
            body: JSON.stringify({ events: batch }),
            keepalive: true,
        }).catch(() => { /* graceful · 上报失败不影响业务 */ });
    } catch { /* graceful */ }
}

function scheduleFlush() {
    if (flushTimer != null) {
        window.clearTimeout(flushTimer);
    }
    flushTimer = window.setTimeout(() => {
        flush();
        flushTimer = null;
    }, FLUSH_DEBOUNCE_MS);
}

/**
 * 立刻把队列发出去,不等 5s debounce。
 * [WO_MOBILE_PAY_JUMP 2026-08-07 §2.1] 支付跳转是**离开本页**的动作 ——
 * 队列里的事件如果还在等 debounce,页面一走就没了,那正是"跳没跳出去"这件事
 * 服务端零可见度的原因。fetch 已带 keepalive,同步栈里发就能活过导航。
 */
export function flushAnalyticsNow(): void {
    if (flushTimer != null) {
        window.clearTimeout(flushTimer);
        flushTimer = null;
    }
    flush();
}

/** 埋一个点并立刻上报(用于跳转/离页这类"再不发就没机会"的时刻) */
export function trackEventNow(event: string, metadata?: Record<string, unknown>): void {
    trackEvent(event, metadata);
    flushAnalyticsNow();
}

/** 通用事件埋点 */
export function trackEvent(event: string, metadata?: Record<string, unknown>): void {
    if (typeof window === 'undefined') return;
    EVENTS_QUEUE.push({
        event,
        pathname: window.location.pathname,
        timestamp: new Date().toISOString(),
        user_id: getCurrentUserId(),
        metadata,
    });
    if (EVENTS_QUEUE.length >= FLUSH_MAX_BATCH) {
        flush();
    } else {
        scheduleFlush();
    }
}

/** 页面访问埋点(M3 vs 旧版 PV 关键指标) */
export function trackPageView(pathname: string): void {
    // [WO_260] 原先还按 /social(跳过)、/m3(is_m3)、/c/、=== '/s' 分流 —— 这几族路由随 E3 删域,在役页永远命不中。
    //   `/s/` 前缀保留:`/s/:token` 是在役的客户选词报价页(SelectionPage),照旧不算旧版工作面。
    //   is_m3 字段留着(恒 false)只为不改事件形状。
    const isLegacy = !pathname.startsWith('/login') && !pathname.startsWith('/s/') && !pathname.startsWith('/portal/') && !pathname.startsWith('/public/') && !pathname.startsWith('/intake/');
    trackEvent('page_view', {
        path: pathname,
        is_m3: false,
        is_legacy: isLegacy,
    });
}

/** Endpoint 切换埋点(代理在 M3 / 旧版之间切换) */
export function trackEndpointSwitch(direction: 'legacy-to-m3' | 'm3-to-legacy', metadata?: Record<string, unknown>): void {
    trackEvent('endpoint_switch', {
        direction,
        ...metadata,
    });
}

/** 页面切换 listener · App.tsx 装一次即可 */
export function installAnalyticsListener(): () => void {
    if (typeof window === 'undefined') return () => { /* noop */ };

    // CTO-15.20:暴露 window.__m3Analytics 给桥接组件用(BackToM3Button / DecisionBarBridge / M3 主任务卡 / ToolGrid / MyClientsPage)
    (window as unknown as { __m3Analytics?: { track: (event: string, metadata?: Record<string, unknown>) => void } }).__m3Analytics = {
        track: trackEvent,
    };

    let lastPath = window.location.pathname;
    const handle = () => {
        if (window.location.pathname !== lastPath) {
            lastPath = window.location.pathname;
            trackPageView(lastPath);
        }
    };

    // 初始页 PV
    trackPageView(lastPath);

    // popstate(浏览器后退/前进)
    window.addEventListener('popstate', handle);

    // pushState/replaceState(react-router 导航)需要 monkey-patch history
    const origPush = window.history.pushState.bind(window.history);
    const origReplace = window.history.replaceState.bind(window.history);
    window.history.pushState = function (...args: Parameters<typeof origPush>) {
        const r = origPush(...args);
        setTimeout(handle, 0);
        return r;
    };
    window.history.replaceState = function (...args: Parameters<typeof origReplace>) {
        const r = origReplace(...args);
        setTimeout(handle, 0);
        return r;
    };

    // 页面卸载时强制 flush
    window.addEventListener('beforeunload', () => {
        flush();
    });

    return () => {
        window.removeEventListener('popstate', handle);
        window.history.pushState = origPush;
        window.history.replaceState = origReplace;
    };
}
