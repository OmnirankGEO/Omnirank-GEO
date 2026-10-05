import axios from 'axios';
import { installLazySandboxInterceptor, tryFetchSandboxMockLazy } from '@/sandbox/lazySandboxInterceptor';
import {
    clearAuthoritativeSessionToken,
    getAuthorizationEpoch,
    getConfirmedSessionAuthority,
    getConfirmedSessionToken,
    isCurrentSessionAuthority,
    isCurrentSessionCandidate,
    readStoredSessionToken,
    awaitConfirmedSessionToken,
    peekConfirmedSessionToken,
    stageAuthoritativeSessionToken,
    type SessionAuthority,
} from '@/lib/authoritativeSession';
import { getActiveDemoSelection, notifyDemoActionPreview, shouldAttachDemoSelection } from '@/lib/demoMode';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

// API基础配置 - 开发环境走 Vite proxy（同域），避免 CORS 问题
const API_BASE_URL = '';

// 全局 authFetch: 自动注入 JWT Token + 401 自动处理
// 覆盖所有原生 fetch() 调用的认证需求
// ==========================================
// JWT refresh 并发锁(Deploy-CTO D 任务 · 2026-05-06)
// ==========================================
//
// 老板反馈: PC 用着用着被踢需重登录 · 手机端同账号没事
// 根因: 一次操作触发 5 个 API 同时 PERMISSION_CHANGED · 5 个并发 POST /refresh ·
//       后端不严格幂等 → 多 token 生成 + localStorage 多次覆盖 → race · 用户被踢
//
// 修法: 全局 promise 单例 · 4 处 refresh 调用都复用同一个 promise ·
//       同一时刻最多只跑 1 次 /api/auth/refresh · localStorage 只被写 1 次
//
// 内部 2 次重试(中间 2s backoff) · 跟原 401 PERMISSION_CHANGED 行为对齐
// 403 分支从原 1 次重试升级为 2 次 · 偶发抖动恢复更稳
type RequestAuthority = { token: string | null; epoch: string };

type RefreshOutcome =
    | { status: 'refreshed'; token: string; authority: SessionAuthority }
    | { status: 'hard-invalid' | 'soft-failure' | 'superseded' | 'no-session'; token: null; authority?: never };

function authorityKey(authority: SessionAuthority): string {
    return `${authority.epoch}\u0000${authority.token}`;
}

function sameAuthority(left: SessionAuthority | null, right: SessionAuthority | null): boolean {
    return left?.token === right?.token && left?.epoch === right?.epoch;
}

let _refreshInFlight: { key: string; request: Promise<RefreshOutcome> } | null = null;
const _refreshSuccessors = new Map<string, SessionAuthority>();
const _authoritativeRefreshes = new Map<string, Promise<void>>();

function rememberRefreshSuccessor(previous: SessionAuthority, next: SessionAuthority): void {
    _refreshSuccessors.set(authorityKey(previous), next);
    if (_refreshSuccessors.size > 16) {
        const oldest = _refreshSuccessors.keys().next().value as string | undefined;
        if (oldest) _refreshSuccessors.delete(oldest);
    }
}

function forgetRefreshSuccessor(previous: SessionAuthority, next: SessionAuthority): void {
    const key = authorityKey(previous);
    if (sameAuthority(_refreshSuccessors.get(key) || null, next)) {
        _refreshSuccessors.delete(key);
    }
}

async function acquireRefreshedToken(
    maxAttempts = 2,
    requestedAuthority?: RequestAuthority,
): Promise<RefreshOutcome> {
    const currentAuthority = getConfirmedSessionAuthority();
    const requested = requestedAuthority || currentAuthority;
    if (!requested?.token) return { status: 'no-session', token: null };
    const expectedAuthority: SessionAuthority = { token: requested.token, epoch: requested.epoch };
    const expectedKey = authorityKey(expectedAuthority);
    if (_authoritativeRefreshes.has(expectedKey)) {
        return await refreshedOutcomeForExistingAuthority(expectedAuthority);
    }
    if (_refreshInFlight?.key === expectedKey) return await _refreshInFlight.request;
    const request = (async () => {
        const tryOnce = async (): Promise<RefreshOutcome> => {
            if (!isCurrentSessionAuthority(expectedAuthority)) {
                return { status: 'superseded', token: null };
            }
            try {
                const r = await fetch('/api/auth/refresh', {
                    method: 'POST',
                    headers: { 'Authorization': `Bearer ${expectedAuthority.token}`, 'Content-Type': 'application/json' },
                });
                if (r.ok) {
                    const d = await r.json();
                    if (d.success && d.token) {
                        // Token equality is insufficient: logout/login can reuse the same
                        // second-level JWT. The initiating authority generation must still own
                        // the browser session before the candidate is staged.
                        if (!isCurrentSessionAuthority(expectedAuthority)) {
                            return { status: 'superseded', token: null };
                        }
                        const nextAuthority: SessionAuthority = {
                            token: d.token,
                            epoch: stageAuthoritativeSessionToken(d.token, 'refresh'),
                        };
                        const refreshDetail = d as typeof d & {
                            epoch?: string;
                            authoritativeReady?: Promise<void>;
                        };
                        refreshDetail.epoch = nextAuthority.epoch;
                        window.dispatchEvent(new CustomEvent('token-refreshed', {
                            detail: refreshDetail,
                        }));
                        if (!refreshDetail.authoritativeReady) {
                            return { status: 'soft-failure', token: null };
                        }
                        const nextKey = authorityKey(nextAuthority);
                        const authoritativeReady = refreshDetail.authoritativeReady;
                        _authoritativeRefreshes.set(nextKey, authoritativeReady);
                        rememberRefreshSuccessor(expectedAuthority, nextAuthority);
                        try {
                            await authoritativeReady;
                        } catch {
                            forgetRefreshSuccessor(expectedAuthority, nextAuthority);
                            if (readStoredSessionToken() === null) {
                                return { status: 'hard-invalid', token: null };
                            }
                            if (!isCurrentSessionCandidate(nextAuthority)) {
                                return { status: 'superseded', token: null };
                            }
                            return { status: 'soft-failure', token: null };
                        } finally {
                            if (_authoritativeRefreshes.get(nextKey) === authoritativeReady) {
                                _authoritativeRefreshes.delete(nextKey);
                            }
                        }
                        if (!isCurrentSessionAuthority(nextAuthority)) {
                            return { status: 'superseded', token: null };
                        }
                        return { status: 'refreshed', token: nextAuthority.token, authority: nextAuthority };
                    }
                }
                if (r.status === 401 || r.status === 404) {
                    return { status: 'hard-invalid', token: null };
                }
            } catch { /* network failures are soft */ }
            return { status: 'soft-failure', token: null };
        };
        const t1 = await tryOnce();
        if (t1.status !== 'soft-failure' || maxAttempts <= 1) return t1;
        await new Promise(resolve => setTimeout(resolve, 2000));
        return await tryOnce();
    })();
    _refreshInFlight = { key: expectedKey, request };
    try {
        return await request;
    } finally {
        if (_refreshInFlight?.request === request) _refreshInFlight = null;
    }
}

function isSafeAuthReplayMethod(method: string | undefined): boolean {
    const normalized = (method || 'GET').toUpperCase();
    return normalized === 'GET' || normalized === 'HEAD' || normalized === 'OPTIONS';
}

type ReplayAwareConfig = {
    headers?: any;
    method?: string;
    signal?: AbortSignal;
    timeout?: number;
    __omnirankManagedSignal?: boolean;
    __omnirankCallerSignal?: AbortSignal;
    __omnirankRequestAuthority?: RequestAuthority;
    __omnirankStartedAt?: number;
    __omnirankDeadlineAt?: number;
};

export type AuthFetchRequestInit = RequestInit & {
    __omnirankManagedSignal?: boolean;
    __omnirankCallerSignal?: AbortSignal | null;
    __omnirankStartedAt?: number;
    __omnirankDeadlineAt?: number;
};

const DEFAULT_AUTH_REPLAY_TIMEOUT_MS = 30_000;

type ReplayGuard = {
    signal: AbortSignal;
    cleanup: () => void;
};

function replayCallerSignal(config: ReplayAwareConfig): AbortSignal | undefined {
    const caller = config.__omnirankManagedSignal
        ? config.__omnirankCallerSignal
        : (config.__omnirankCallerSignal || config.signal);
    return caller || undefined;
}

function createReplayGuard(
    authority: SessionAuthority,
    callerSignal: AbortSignal | undefined,
    deadlineAt: number,
    fetchStyle = false,
): ReplayGuard {
    const fail = (message: string): never => {
        if (fetchStyle) throw new DOMException(message, 'AbortError');
        throw new axios.CanceledError(message);
    };
    if (!isCurrentSessionAuthority(authority)) fail('authority changed before request replay');
    if (callerSignal?.aborted) fail('caller canceled before request replay');
    const remainingMs = deadlineAt - Date.now();
    if (remainingMs <= 0) fail('request deadline elapsed before authority replay');

    const controller = new AbortController();
    const abortFromCaller = () => controller.abort(callerSignal?.reason);
    const abortFromAuthority = () => {
        if (!isCurrentSessionAuthority(authority)) {
            controller.abort(new DOMException('Authorization changed during request replay', 'AbortError'));
        }
    };
    callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
    window.addEventListener('omnirank-authorization-changed', abortFromAuthority);
    const timeout = window.setTimeout(
        () => controller.abort(new DOMException('Authority replay deadline elapsed', 'TimeoutError')),
        remainingMs,
    );
    return {
        signal: controller.signal,
        cleanup: () => {
            window.clearTimeout(timeout);
            callerSignal?.removeEventListener('abort', abortFromCaller);
            window.removeEventListener('omnirank-authorization-changed', abortFromAuthority);
        },
    };
}

function prepareSafeReplayConfig<T extends ReplayAwareConfig>(
    config: T,
    authority: SessionAuthority,
): { config: T; guard: ReplayGuard } {
    const callerSignal = replayCallerSignal(config);
    const startedAt = config.__omnirankStartedAt || Date.now();
    const deadlineAt = config.__omnirankDeadlineAt
        || startedAt + (config.timeout && config.timeout > 0 ? config.timeout : DEFAULT_AUTH_REPLAY_TIMEOUT_MS);
    const guard = createReplayGuard(authority, callerSignal, deadlineAt);
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${authority.token}`;
    config.signal = guard.signal;
    config.timeout = Math.max(1, deadlineAt - Date.now());
    config.__omnirankManagedSignal = true;
    config.__omnirankCallerSignal = callerSignal;
    config.__omnirankRequestAuthority = authority;
    config.__omnirankStartedAt = startedAt;
    config.__omnirankDeadlineAt = deadlineAt;
    return { config, guard };
}

async function replayAxiosRequest<T>(
    config: ReplayAwareConfig,
    authority: SessionAuthority,
): Promise<T> {
    const prepared = prepareSafeReplayConfig(config, authority);
    try {
        const response = await api.request(prepared.config);
        if (!isCurrentSessionAuthority(authority)) {
            throw new axios.CanceledError('authority changed before replay response was consumed');
        }
        return response as T;
    } finally {
        prepared.guard.cleanup();
    }
}

function prepareSafeFetchReplay(
    init: AuthFetchRequestInit | undefined,
    headers: Headers,
    authority: SessionAuthority,
): { init: RequestInit; guard: ReplayGuard } {
    const callerSignal = init?.__omnirankManagedSignal
        ? (init.__omnirankCallerSignal || undefined)
        : (init?.__omnirankCallerSignal || init?.signal || undefined);
    const startedAt = init?.__omnirankStartedAt || Date.now();
    const deadlineAt = init?.__omnirankDeadlineAt || startedAt + DEFAULT_AUTH_REPLAY_TIMEOUT_MS;
    const guard = createReplayGuard(authority, callerSignal, deadlineAt, true);
    const {
        __omnirankManagedSignal: _managed,
        __omnirankCallerSignal: _caller,
        __omnirankStartedAt: _started,
        __omnirankDeadlineAt: _deadline,
        ...nativeInit
    } = init || {};
    return {
        init: { ...nativeInit, headers, signal: guard.signal },
        guard,
    };
}

async function replayFetchOnce(
    input: RequestInfo | URL,
    init: AuthFetchRequestInit | undefined,
    headers: Headers,
    authority: SessionAuthority,
): Promise<Response> {
    const prepared = prepareSafeFetchReplay(init, headers, authority);
    try {
        const response = await fetch(input, prepared.init);
        if (!isCurrentSessionAuthority(authority)) {
            throw new DOMException('Authorization changed before replay response was consumed', 'AbortError');
        }
        if (response.status === 401) {
            let code: string | undefined;
            try {
                code = (await response.clone().json())?.code;
            } catch { /* non-JSON auth response */ }
            const hardInvalid = !code
                || code === 'INVALID_TOKEN'
                || code === 'USER_NOT_FOUND'
                || code === 'ACCOUNT_DISABLED';
            if (hardInvalid) expireSessionIfPrivate(authority);
        }
        return response;
    } finally {
        prepared.guard.cleanup();
    }
}

function currentAuthorityForRequest(requestAuthority: RequestAuthority): SessionAuthority | null {
    const current = getConfirmedSessionAuthority();
    if (!current || !requestAuthority.token) return null;
    let candidate: SessionAuthority | undefined = {
        token: requestAuthority.token,
        epoch: requestAuthority.epoch,
    };
    const seen = new Set<string>();
    while (candidate) {
        const key = authorityKey(candidate);
        if (seen.has(key)) return null;
        if (sameAuthority(candidate, current)) return current;
        seen.add(key);
        candidate = _refreshSuccessors.get(key);
    }
    return null;
}

async function refreshedOutcomeForExistingAuthority(authority: RequestAuthority): Promise<RefreshOutcome> {
    const current = currentAuthorityForRequest(authority);
    if (!current) return { status: 'superseded', token: null };
    const authoritativeReady = _authoritativeRefreshes.get(authorityKey(current));
    if (authoritativeReady) {
        try {
            await authoritativeReady;
        } catch {
            if (readStoredSessionToken() === null) return { status: 'hard-invalid', token: null };
            if (!isCurrentSessionCandidate(current)) return { status: 'superseded', token: null };
            return { status: 'soft-failure', token: null };
        }
    }
    if (!isCurrentSessionAuthority(current)) {
        return { status: 'superseded', token: null };
    }
    return { status: 'refreshed', token: current.token, authority: current };
}

async function refreshOutcomeForRequestAuthority(
    requestAuthority: RequestAuthority,
    maxAttempts: number,
): Promise<RefreshOutcome> {
    if (!requestAuthority.token) return { status: 'no-session', token: null };
    const exactAuthority: SessionAuthority = {
        token: requestAuthority.token,
        epoch: requestAuthority.epoch,
    };
    // A sibling response can arrive while the exact owner's refresh has staged
    // its successor but /me has not confirmed it yet. During that window there
    // is deliberately no current authority; join only this exact {token, epoch}
    // refresh promise instead of treating the sibling as a different session.
    if (_refreshInFlight?.key === authorityKey(exactAuthority)) {
        return await _refreshInFlight.request;
    }
    const activeAuthority = currentAuthorityForRequest(requestAuthority);
    if (!activeAuthority) return { status: 'superseded', token: null };
    return sameAuthority(activeAuthority, exactAuthority)
        ? await acquireRefreshedToken(maxAttempts, requestAuthority)
        : await refreshedOutcomeForExistingAuthority(activeAuthority);
}

/** Resolve only a refresh-lineage successor of the exact initiating authority. */
export function resolveRequestAuthoritySuccessor(authority: SessionAuthority): SessionAuthority | null {
    return currentAuthorityForRequest(authority);
}

function requestBearerToken(headers: unknown): string | null {
    const candidate = headers as {
        get?: (name: string) => unknown;
        Authorization?: unknown;
        authorization?: unknown;
    } | null | undefined;
    const raw = candidate?.get?.('Authorization')
        ?? candidate?.Authorization
        ?? candidate?.authorization;
    if (typeof raw !== 'string') return null;
    const match = raw.match(/^Bearer\s+(.+)$/i);
    return match?.[1] || null;
}

function requestHasDemoAuthority(headers: unknown): boolean {
    const candidate = headers as {
        get?: (name: string) => unknown;
        'X-Demo-Brand-ID'?: unknown;
        'x-demo-brand-id'?: unknown;
        'X-Demo-Case-ID'?: unknown;
        'x-demo-case-id'?: unknown;
    } | null | undefined;
    const brandId = candidate?.get?.('X-Demo-Brand-ID')
        ?? candidate?.['X-Demo-Brand-ID']
        ?? candidate?.['x-demo-brand-id'];
    const caseId = candidate?.get?.('X-Demo-Case-ID')
        ?? candidate?.['X-Demo-Case-ID']
        ?? candidate?.['x-demo-case-id'];
    return brandId !== null && brandId !== undefined && String(brandId).trim() !== ''
        && caseId !== null && caseId !== undefined && String(caseId).trim() !== '';
}

function canReplayRequestAuthority(authority: RequestAuthority, expected: SessionAuthority): boolean {
    return isCurrentSessionAuthority(expected)
        && sameAuthority(currentAuthorityForRequest(authority), expected);
}

const SOCIAL_STUDIO_PATHS = new Set([
    'login', 'approval', 'match', 'chat', 'history', 'persona-setup', 'my-ip',
    'video-script', 'stats', 'experts', 'managed', 'workshop', 'content-workshop', 'ideas', 'topics',
    'rewrite', 'imitate', 'planning', 'plan', 'profile', 'creator-profile', 'my-profile',
    'clients', 'my-clients', 'advisors', 'advisor-market', 'review', 'data-review',
    'interaction', 'interaction-management', 'interview', 'creator-test', 'corpus', 'team',
    'scripts', 'research', 'trending', 'operation', 'home',
]);

function isPublicSelectionRoute(pathname: string) {
    const parts = pathname.split('/').filter(Boolean);
    return parts.length === 2 && parts[0] === 's' && !SOCIAL_STUDIO_PATHS.has(parts[1]);
}

function isPublicPagePath(pathname: string): boolean {
    return isPublicSelectionRoute(pathname)
        || pathname.startsWith('/m/')
        || pathname.startsWith('/portal')
        || pathname.startsWith('/public/')
        || pathname.startsWith('/q/')
        || pathname.startsWith('/intake/')
        || pathname.startsWith('/api/sl/')
        || pathname === '/landing'
        || pathname === '/login'
        || pathname === '/agreement-update'
        || pathname === '/terms'
        || pathname === '/privacy';
}

function expireSessionIfPrivate(authority: RequestAuthority): void {
    if (!authority.token) return;
    if (!clearAuthoritativeSessionToken(authority.token, authority.epoch)) return;
    if (!isPublicPagePath(window.location.pathname)) window.location.href = '/login';
}

// ==========================================
// Wallet 余额自动刷新拦截器(2026-05-12 老板 P0 · "关乎钱,稳定第一")
// ==========================================
//
// 痛点:用户在 社媒工作台 调 AI 后扣费(后端 charge_subscription_entitlement / deduct_points)
//      但前端 BalanceBar 不刷新 · 用户感知"扣了钱但没显示" · 财务级体验破洞
//
// 设计:authFetch 全局拦截 — 任何 POST/PUT/PATCH/DELETE 2xx 响应都触发 wallet refresh
//      不依赖 社媒工作台 业务代码主动 dispatch,不依赖后端响应改动,不依赖 WebSocket
//
// 防递归:跳过 /api/wallet 自身 + /api/auth/refresh + auth/me 等 metadata 调用
// Debounce 500ms:多次扣费合并为一次 fetch,避免短时多次连击
//
// 配合 WalletContext.tsx 4 层防御:
//   1. 本拦截器(主防线,90% 场景)
//   2. visibilitychange + window.focus(切 tab 回来立即刷)
//   3. 可见时 120s 兜底 polling(抓 cron / 后端别处扣费)
//   4. window.dispatchEvent('wallet:refresh') 业务方按需触发(扩展点)
const WALLET_REFRESH_DEBOUNCE_MS = 500;
let _walletRefreshTimer: ReturnType<typeof setTimeout> | null = null;

// 不触发刷新的 URL 模式(避免无意义 fetch / 防递归)
const WALLET_REFRESH_SKIP_PATTERNS = [
    /\/api\/wallet(\/|$|\?)/,       // wallet 自身防递归
    /\/api\/auth\/refresh/,         // refresh token 不算业务操作
    /\/api\/auth\/login/,           // 登录走 fetchBalance 已有逻辑
    /\/api\/auth\/logout/,
    /\/api\/auth\/me/,              // metadata · 不扣费
    /\/api\/notify/,                // 推送 ack 不扣费
    /\/api\/health/,
    /\/api\/admin\/audit/,          // admin 日志记录
    /\/api\/scheduler\/heartbeat/,
    /\/api\/analytics\//,           // telemetry writes never change wallet state
    /\/api\/error-report/,           // telemetry writes never change wallet state
    /\/(preview|dry-run)(?:$|\?)/,   // explicitly read-only calculations transported as POST
];

function shouldTriggerWalletRefresh(url: string, method: string): boolean {
    // 仅写操作(POST/PUT/PATCH/DELETE)成功响应触发
    const m = method.toUpperCase();
    if (m !== 'POST' && m !== 'PUT' && m !== 'PATCH' && m !== 'DELETE') return false;
    // 黑名单 URL 跳过
    for (const pattern of WALLET_REFRESH_SKIP_PATTERNS) {
        if (pattern.test(url)) return false;
    }
    return true;
}

function scheduleWalletRefresh() {
    if (_walletRefreshTimer) clearTimeout(_walletRefreshTimer);
    _walletRefreshTimer = setTimeout(() => {
        _walletRefreshTimer = null;
        try {
            window.dispatchEvent(new CustomEvent('wallet:auto-refresh'));
        } catch { /* 老浏览器降级,忽略 */ }
    }, WALLET_REFRESH_DEBOUNCE_MS);
}

export async function authFetch(input: RequestInfo | URL, init?: AuthFetchRequestInit): Promise<Response> {
    // 沙盒拦截 (Stage 1 Batch 3, 2026-05-08) · 命中规则直接返合成 200, 不走真后端
    // Batch 4 (2026-05-18) · 改 async 支持 delayMs (模拟接口耗时)
    const reqUrl = typeof input === 'string' ? input : (input instanceof URL ? input.toString() : input.url);
    const reqMethod = (init?.method || (typeof input !== 'string' && !(input instanceof URL) ? input.method : 'GET') || 'GET').toUpperCase();
    const requestStartedAt = init?.__omnirankStartedAt || Date.now();
    const replayInit: AuthFetchRequestInit = {
        ...(init || {}),
        __omnirankStartedAt: requestStartedAt,
        __omnirankDeadlineAt: init?.__omnirankDeadlineAt
            || requestStartedAt + DEFAULT_AUTH_REPLAY_TIMEOUT_MS,
    };
    const sandboxResp = await tryFetchSandboxMockLazy(reqMethod, reqUrl, false, replayInit);
    if (sandboxResp) return sandboxResp;

    // [BUG-3] 会话在途 → 等待,不抛错炸页面(fail-closed 不变:未确认仍拿不到 token)
    const token = await awaitConfirmedSessionToken();
    const headers = new Headers(init?.headers);
    if (token && !headers.has('Authorization')) {
        headers.set('Authorization', `Bearer ${token}`);
    }
    const demoSelection = getActiveDemoSelection();
    const requestUrl = typeof input === 'string' ? input : input instanceof URL ? input.toString() : input.url;
    const hasExplicitDemoAuthority = requestHasDemoAuthority(headers);
    if (!hasExplicitDemoAuthority && demoSelection && shouldAttachDemoSelection(requestUrl)) {
        headers.set('X-Demo-Brand-ID', String(demoSelection.brandId));
        headers.set('X-Demo-Case-ID', demoSelection.caseId);
    } else if (!hasExplicitDemoAuthority) {
        headers.delete('X-Demo-Brand-ID');
        headers.delete('X-Demo-Case-ID');
    }
    const requestSessionToken = requestBearerToken(headers);
    const requestAuthority: RequestAuthority = {
        token: requestSessionToken,
        epoch: getAuthorizationEpoch(),
    };
    // 2026-05-13: body 是 JSON string 但调用方漏 Content-Type → 浏览器默认用 text/plain
    // FastAPI Pydantic 收到非 JSON content-type 报 "Input should be a valid dictionary"
    // 422 (老板自检 /api/subscription/checkout 报 422 真因)
    // 保守 fallback: body 是 string 且未设 Content-Type 时自动加 application/json
    // 不破坏 FormData / Blob body (那些应该让浏览器自己拼 multipart boundary)
    if (
        typeof init?.body === 'string'
        && !headers.has('Content-Type')
        && !headers.has('content-type')
    ) {
        headers.set('Content-Type', 'application/json');
    }
    const method = reqMethod;
    const urlStr = typeof input === 'string'
        ? input
        : (input instanceof URL ? input.toString() : ((input as Request).url || ''));

    const {
        __omnirankManagedSignal: _managedSignal,
        __omnirankCallerSignal: _callerSignal,
        __omnirankStartedAt: _startedAt,
        __omnirankDeadlineAt: _deadlineAt,
        ...nativeInit
    } = replayInit;
    const response = await fetch(input, { ...nativeInit, headers });

    // Successful writes invalidate only their resource domains. Identity rotation
    // is the sole reason to abort every unrelated read in the application.
    if (response.ok && isMutationMethod(method)) {
        invalidateApiResourcesForMutation(urlStr);
    }

    // Layer 1 主防线:写操作 2xx → 调度 wallet refresh(debounced)
    if (response.ok && shouldTriggerWalletRefresh(urlStr, method)) {
        scheduleWalletRefresh();
    }

    // 401 handling is bound to the exact {token, epoch} that issued the request.
    if (response.status === 401) {
        let code: string | undefined;
        try {
            const errData = await response.clone().json();
            code = errData?.code;
        } catch { /* non-JSON auth response */ }

        if (code === 'PERMISSION_CHANGED') {
            const refreshOutcome = await refreshOutcomeForRequestAuthority(requestAuthority, 2);
            if (refreshOutcome.status === 'refreshed'
                && canReplayRequestAuthority(requestAuthority, refreshOutcome.authority)
                && isSafeAuthReplayMethod(method)) {
                const retryHeaders = new Headers(headers);
                retryHeaders.set('Authorization', `Bearer ${refreshOutcome.token}`);
                return replayFetchOnce(input, replayInit, retryHeaders, refreshOutcome.authority);
            }
            console.warn('[authFetch] PERMISSION_CHANGED 未自动重放原请求');
            return response;
        }

        if (!code) {
            const refreshOutcome = await refreshOutcomeForRequestAuthority(requestAuthority, 1);
            if (refreshOutcome.status !== 'refreshed') {
                if (refreshOutcome.status === 'hard-invalid'
                    && isCurrentSessionAuthority(requestAuthority as SessionAuthority)) {
                    expireSessionIfPrivate(requestAuthority);
                }
                return response;
            }
            if (!canReplayRequestAuthority(requestAuthority, refreshOutcome.authority)) return response;
            if (!isSafeAuthReplayMethod(method)) return response;
            const retryHeaders = new Headers(headers);
            retryHeaders.set('Authorization', `Bearer ${refreshOutcome.token}`);
            return replayFetchOnce(input, replayInit, retryHeaders, refreshOutcome.authority);
        }

        const shouldLogout = code === 'INVALID_TOKEN'
            || code === 'USER_NOT_FOUND'
            || code === 'ACCOUNT_DISABLED';
        if (shouldLogout && isCurrentSessionAuthority(requestAuthority as SessionAuthority)) {
            expireSessionIfPrivate(requestAuthority);
        } else if (!shouldLogout) {
            console.warn(`[authFetch] 401 code=${code || '(none)'}，保守处理不清 token`);
        }
    }

    // 403 brand permission refresh follows the same authority-bound replay path.
    if (response.status === 403) {
        if (requestHasDemoAuthority(headers)) return response;
        try {
            const errData = await response.clone().json().catch(() => null);
            // [F-2] detail 现在可能是 §13 告警合同**对象**;直接 .includes 会抛 TypeError,
            // 被下方 catch 吞掉 → 403 授权刷新重放静默失效。对象一律归一为空串。
            const rawDetail = errData?.detail;
            const detail = typeof rawDetail === 'string' ? rawDetail : '';
            if (detail.includes('brand_id') || detail.includes('无权访问') || detail.includes('BRAND_ACCESS')) {
                const refreshOutcome = await refreshOutcomeForRequestAuthority(requestAuthority, 2);
                if (refreshOutcome.status === 'refreshed'
                    && canReplayRequestAuthority(requestAuthority, refreshOutcome.authority)
                    && isSafeAuthReplayMethod(method)) {
                    const retryHeaders = new Headers(headers);
                    retryHeaders.set('Authorization', `Bearer ${refreshOutcome.token}`);
                    return await replayFetchOnce(input, replayInit, retryHeaders, refreshOutcome.authority);
                }
            }
        } catch (error) {
            if ((error as Error)?.name === 'AbortError') throw error;
            console.error('[authFetch] 403 刷新失败:', error);
        }
    }

    if (response.status === 409) {
        try {
            notifyDemoActionPreview(await response.clone().json());
        } catch { /* non-JSON conflict */ }
    }

    return response;
}

const api = axios.create({
    baseURL: API_BASE_URL,
    timeout: 300000, // 5分钟超时（LLM知识处理较慢）
    headers: {
        'Content-Type': 'application/json',
    },
});

// 沙盒拦截器 (Stage 1 Batch 3, 2026-05-08) · 必须在 auth / 401 / dedupe 之前装
// 装顺序: response interceptor 用 FIFO, sandbox 先注册 → 命中 marker 时第一个处理
installLazySandboxInterceptor(api);

// ==========================================
// A.5 (CTO-15.18 · 2026-04-28):请求合并 dedupe · 解 429 限流
// ==========================================
//
// 老板痛点:M3 首屏触发 429 红屏 · /api/auth/me + /api/wallet/pricing 等多次重复调用
// Q8 老板裁决:只前端 dedupe · 不动 auth/rate_limiter.py(系统级单独审)
//
// 策略:仅合并仍未完成的同一 GET(method+url+params)· 后续请求等待同一 Promise。
// 已完成响应不在全局层缓存；业务级 stale/SWR 必须显式定义资源作用域和失效语义。
// 仅 GET 请求 · POST/PUT/PATCH/DELETE 不 dedupe(可能有副作用)
//
// 跳过 dedupe 的端点(写操作 / 流式 / 下载):
//   - 任何非 GET method
//   - POST/PUT/PATCH/DELETE
//   - 含 'export' / 'download' / 'stream' 路径(避免文件下载冲突)
const PENDING_GET_REQUESTS = new Map<string, {
    promise: Promise<unknown>;
    settled: boolean;
    controller: AbortController;
    subscribers: Set<symbol>;
    tags: Set<string>;
}>();

function apiPath(url: string): string {
    try {
        return new URL(url, typeof window === 'undefined' ? 'http://localhost' : window.location.origin).pathname;
    } catch {
        return url.split('?')[0] || url;
    }
}

export function apiResourceTags(url: string): Set<string> {
    const path = apiPath(url).toLowerCase();
    const tags = new Set<string>();
    const add = (...values: string[]) => values.forEach(value => tags.add(value));
    if (/\/notifications?(\/|$)/.test(path) || path.includes('/notify/')) add('notifications');
    if (path.includes('/monitoring')) add('monitoring');
    if (path.includes('/inventory')) add('inventory');
    if (path.includes('/pricing') || path.includes('/feature-pricing')) add('pricing');
    if (/\/(wallet|charge|recharge|checkout|purchase)(\/|$|-)/.test(path)) add('wallet');
    if (/\/(auth|permissions?|roles?|user-governance)(\/|$)/.test(path)) add('identity');
    if (/\/(client-context|my-clients|my-brand|brands|customers)(\/|$)/.test(path)) add('clients');
    if (/\/(publish|published|meijiehezi)(\/|$|-)/.test(path)) add('publishing');
    if (path.includes('/writing')) add('writing');
    if (path.includes('/diagnosis')) add('diagnosis');
    if (tags.size === 0) {
        const parts = path.split('/').filter(Boolean);
        tags.add(`api:${parts[1] || parts[0] || 'root'}`);
    }
    return tags;
}

function abortPendingByTags(tags: Set<string>): void {
    for (const [key, entry] of PENDING_GET_REQUESTS.entries()) {
        if (![...entry.tags].some(tag => tags.has(tag))) continue;
        if (!entry.settled) entry.controller.abort();
        PENDING_GET_REQUESTS.delete(key);
    }
}

export function invalidateApiResourcesForMutation(url: string): Set<string> {
    const tags = apiResourceTags(url);
    abortPendingByTags(tags);
    window.dispatchEvent(new CustomEvent('omnirank-api-mutated', {
        detail: { url, tags: [...tags] },
    }));
    return tags;
}

function stableSerializeParams(value: unknown, ancestors = new Set<object>()): string | null {
    if (value === null) return 'null';
    if (value === undefined) return 'undefined';
    if (typeof value === 'string') return `string:${JSON.stringify(value)}`;
    if (typeof value === 'boolean') return value ? 'boolean:true' : 'boolean:false';
    if (typeof value === 'number') {
        if (Number.isNaN(value)) return 'number:NaN';
        if (value === Infinity) return 'number:+Infinity';
        if (value === -Infinity) return 'number:-Infinity';
        if (Object.is(value, -0)) return 'number:-0';
        return `number:${value}`;
    }
    if (typeof value === 'bigint' || typeof value === 'function' || typeof value === 'symbol') {
        return null;
    }
    if (value instanceof Date) {
        const time = value.getTime();
        return Number.isFinite(time) ? `date:${value.toISOString()}` : null;
    }
    if (typeof URLSearchParams !== 'undefined' && value instanceof URLSearchParams) {
        // Axios transmits URLSearchParams in insertion order. Preserve that order so
        // only requests with the same wire representation share a transport.
        return `url-search-params:${JSON.stringify(value.toString())}`;
    }
    if (typeof value !== 'object') return null;
    if (ancestors.has(value)) return null;

    ancestors.add(value);
    try {
        if (Array.isArray(value)) {
            const items: string[] = [];
            for (let index = 0; index < value.length; index += 1) {
                if (!(index in value)) {
                    items.push('hole');
                    continue;
                }
                const serialized = stableSerializeParams(value[index], ancestors);
                if (serialized === null) return null;
                items.push(serialized);
            }
            return `array:[${items.join(',')}]`;
        }

        const prototype = Object.getPrototypeOf(value);
        if (prototype !== Object.prototype && prototype !== null) return null;
        if (Object.getOwnPropertySymbols(value).length > 0) return null;
        const entries: string[] = [];
        for (const key of Object.keys(value).sort()) {
            const serialized = stableSerializeParams((value as Record<string, unknown>)[key], ancestors);
            if (serialized === null) return null;
            entries.push(`${JSON.stringify(key)}:${serialized}`);
        }
        return `object:{${entries.join(',')}}`;
    } catch {
        // Getters/proxies can throw while being inspected. A request that cannot be
        // represented losslessly must bypass single-flight instead of colliding.
        return null;
    } finally {
        ancestors.delete(value);
    }
}

function buildDedupeKey(
    method: string,
    url: string,
    params: unknown,
    sessionToken: string | null,
    responseType?: string,
    accept?: string,
    demoScope?: string,
): string | null {
    const serializedParams = stableSerializeParams(params);
    if (serializedParams === null) return null;
    // The token is kept only in this in-memory key. It must never be logged or persisted.
    // Including it prevents a completed/pending GET from one account being reused by another.
    return `${sessionToken || 'anonymous'}::${demoScope || 'real'}::${method.toUpperCase()}::${url}::${serializedParams}::${responseType || 'json'}::${accept || ''}`;
}

function shouldDedupe(method: string | undefined, url: string | undefined): boolean {
    if (!method || !url) return false;
    if (method.toLowerCase() !== 'get') return false;
    if (/\b(export|download|stream|sse)\b/i.test(url)) return false;
    return true;
}

function isMutationMethod(method: string | undefined): boolean {
    const normalized = String(method || 'get').toLowerCase();
    return normalized === 'post' || normalized === 'put' || normalized === 'patch' || normalized === 'delete';
}

// 暴露给外面手动清 dedupe(数据 mutate 后强制刷新)
export function clearApiDedupeCache(): void {
    for (const entry of PENDING_GET_REQUESTS.values()) {
        if (!entry.settled) entry.controller.abort();
    }
    PENDING_GET_REQUESTS.clear();
}

if (typeof window !== 'undefined') {
    window.addEventListener('omnirank-demo-selection-changed', clearApiDedupeCache);
}

function subscribeToGet<T>(
    key: string,
    entry: { promise: Promise<unknown>; settled: boolean; controller: AbortController; subscribers: Set<symbol>; tags: Set<string> },
    signal?: AbortSignal,
): ReturnType<typeof _origGet<T>> {
    const subscriber = Symbol('get-subscriber');
    entry.subscribers.add(subscriber);
    return new Promise((resolve, reject) => {
        let finished = false;
        const cleanup = () => {
            if (finished) return;
            finished = true;
            signal?.removeEventListener('abort', onAbort);
            entry.subscribers.delete(subscriber);
        };
        const onAbort = () => {
            cleanup();
            if (!entry.settled && entry.subscribers.size === 0) {
                // Delete before aborting. StrictMode can remount synchronously, before the
                // aborted transport rejects; that remount must start a fresh request rather
                // than subscribe to a request that can no longer produce a value.
                if (PENDING_GET_REQUESTS.get(key) === entry) PENDING_GET_REQUESTS.delete(key);
                entry.controller.abort();
            }
            reject(new axios.CanceledError('request canceled'));
        };
        if (signal?.aborted) {
            onAbort();
            return;
        }
        signal?.addEventListener('abort', onAbort, { once: true });
        entry.promise.then(
            (value) => { if (!finished) { cleanup(); resolve(value as Awaited<ReturnType<typeof _origGet<T>>>); } },
            (error) => { if (!finished) { cleanup(); reject(error); } },
        );
    }) as ReturnType<typeof _origGet<T>>;
}

// ==========================================
// 错误文案兜底 · 不让用户看到裸错(P1 文案兜底 fix 2026-04-27)
// ==========================================
//
// 真实浏览器测试发现 raw 错误外泄:
//   "Request failed with status code 403"
//   "504 Gateway Timeout"
//   "Network Error"
//
// 这个 helper 把 axios/fetch 错对象转成对客户讲得通的人话:
//   - 403 → "权限校验没通过 · 请刷新页面或联系管理员"
//   - 401 → "登录已过期 · 请重新登录"
//   - 404 → 用 fallback("没找到对应数据")
//   - 408/504/502 → "后台还在处理 · 请稍后重试 · 不要重复操作"
//   - 500 → "服务暂时不稳定 · 请稍后重试"
//   - network → "网络抖动 · 请检查网络后重试"
//   - 其他 → 用 fallback
//
// 优先级(2026-05-27 UI 审计 P3 升级):
//   1. detail.code(V3.5 8 error code · 走 v35Terminology.errorCodeLabel 翻译 · 不暴露后端原文)
//   2. 后端返的 detail 字符串(可读 · 是后端写好的)
//   3. detail.message(对象形式 message)
//   4. 状态码兜底文案(避免 axios "Request failed with..." 漏出)
//   5. 调用方传的 fallback
//
// role 参数(可选 · default 'customer')· UI 审计 #1:前端 V3.5 error 必须按身份翻译
export function formatApiErrorForDisplay(
    error: unknown,
    fallback: string,
    role: 'customer' | 'agent' | 'admin' = 'customer',
): string {
    if (!error) return fallback;
    const err = error as {
        response?: { status?: number; data?: { detail?: unknown; message?: unknown } };
        detail?: unknown;
        message?: string;
        code?: string;
    };
    // detail 来源:axios 走 err.response?.data?.detail · fetch wrapper 直接 err.detail
    const detail: unknown = err.response?.data?.detail ?? err.detail;

    // 1. detail.code → v35Terminology 词典(最高优先)
    if (detail && typeof detail === 'object') {
        const code = (detail as { code?: unknown }).code;
        if (typeof code === 'string' && code) {
            // lazy import 避免循环依赖
            try {
                // eslint-disable-next-line @typescript-eslint/no-require-imports
                const { errorCodeLabel } = require('./v35Terminology');
                const labeled = errorCodeLabel(code, role);
                if (labeled) return labeled;
            } catch { /* 词典不可用 · 走下一级 */ }
        }
    }

    // 2. 后端 detail 字符串
    if (typeof detail === 'string' && detail.trim()) {
        // 防 axios 自身错文案泄漏(后端不会返这种)
        if (!/^Request failed/i.test(detail) && !/^Network/i.test(detail)) {
            return detail;
        }
    }
    // 3. detail.message(对象)
    if (detail && typeof detail === 'object') {
        const msg = (detail as { message?: unknown }).message;
        if (typeof msg === 'string' && msg.trim()) return msg;
    }
    // 2. HTTP 状态码兜底
    const status = err.response?.status;
    if (status) {
        if (status === 401) return '登录已过期 · 请重新登录';
        if (status === 403) return '权限校验没通过 · 请刷新页面或联系管理员';
        if (status === 408 || status === 504 || status === 502) {
            return '后台还在处理 · 请稍后重试 · 不要重复操作';
        }
        if (status === 429) return '操作太频繁 · 请稍后再试';
        if (status === 500) return '服务暂时不稳定 · 请稍后重试';
        if (status === 404) return fallback;
    }
    // 3. 网络层错误
    if (err.code === 'ECONNABORTED' || /timeout/i.test(err.message || '')) {
        return '请求超时 · 后台可能仍在处理 · 请稍后再试';
    }
    if (/Network Error/i.test(err.message || '')) {
        return '网络抖动 · 请检查网络后重试';
    }
    // 4. 兜底
    return fallback;
}

/**
 * 读一个 `authFetch` 的响应体,**保证抛出来的东西能被
 * `formatApiErrorForDisplay` 翻成人话**。
 *
 * 🔴 存在的理由(2026-08-03 Owner 实测撞到):
 *    到处都写着 `const d = await res.json()` 然后才判 `res.ok`。
 *    一旦服务端返回的**不是 JSON**(nginx 504 / 502 / 404 都是 HTML 页面),
 *    `.json()` 会抛 `SyntaxError: Unexpected token '<', "<html> <h"... is not valid JSON`,
 *    而调用方普遍用 `e.message` 展示 —— 于是用户看到的就是这句解析器内部报错。
 *    截图为证:整条红色 banner 就写着 `Unexpected token '<'`。
 *
 *    这不是文案问题,是**把没有 JSON 的失败当成有 JSON 来读**。
 *    504 恰恰是最需要说清楚的那一类(后台可能还在跑、可能已经扣费),
 *    却被降级成一句没人看得懂的话。
 *
 * 抛出的形状对齐 axios(`{ response: { status, data: { detail } } }`),
 * 这样 `formatApiErrorForDisplay` 的现成分支(401/403/502/504/429/500)直接生效,
 * **不另起一套错误翻译**。
 */
export async function readApiJson<T = any>(res: Response): Promise<T> {
    const raw = await res.text();
    let parsed: any = null;
    let isJson = false;
    try {
        parsed = raw ? JSON.parse(raw) : null;
        isJson = true;
    } catch {
        isJson = false;
    }
    if (!res.ok || !isJson) {
        const err: any = new Error(`api_error_${res.status}`);
        err.response = {
            status: res.status,
            // 不是 JSON 时**不要**把 HTML 原文塞进 detail —— 那正是乱码报错的来源。
            // 留空让 formatApiErrorForDisplay 走状态码分支给人话。
            data: isJson ? parsed : undefined,
        };
        err.detail = isJson ? parsed?.detail : undefined;
        throw err;
    }
    return parsed as T;
}

// 🆕 RBAC: 自动附加 JWT Token
api.interceptors.request.use(async config => {
    // [BUG-3] 同上
    const token = await awaitConfirmedSessionToken();
    if (token) {
        config.headers.Authorization = `Bearer ${token}`;
    } else {
        delete config.headers.Authorization;
    }
    const replayConfig = config as typeof config & ReplayAwareConfig;
    if (!replayConfig.__omnirankRequestAuthority) {
        replayConfig.__omnirankRequestAuthority = { token, epoch: getAuthorizationEpoch() };
    }
    if (!replayConfig.__omnirankStartedAt) {
        replayConfig.__omnirankStartedAt = Date.now();
        replayConfig.__omnirankDeadlineAt = replayConfig.__omnirankStartedAt
            + (replayConfig.timeout && replayConfig.timeout > 0
                ? replayConfig.timeout
                : DEFAULT_AUTH_REPLAY_TIMEOUT_MS);
    }
    const demoSelection = getActiveDemoSelection();
    const hasExplicitDemoAuthority = requestHasDemoAuthority(config.headers);
    if (!hasExplicitDemoAuthority && demoSelection && shouldAttachDemoSelection(String(config.url || ''))) {
        config.headers['X-Demo-Brand-ID'] = String(demoSelection.brandId);
        config.headers['X-Demo-Case-ID'] = demoSelection.caseId;
    } else if (!hasExplicitDemoAuthority) {
        delete config.headers['X-Demo-Brand-ID'];
        delete config.headers['X-Demo-Case-ID'];
    }
    return config;
});

// A.5 dedupe response interceptor (cleanup 用) · 真正的 dedupe 通过 wrapped get 实现
// 因为 axios interceptor 不能 short-circuit 返已有 Promise · 我们用 monkey-patch get 方法
const _origGet = api.get.bind(api);
api.get = function dedupedGet<T = unknown>(url: string, config?: import('axios').AxiosRequestConfig) {
    const params = config?.params;
    if (!shouldDedupe('get', url) || config?.paramsSerializer) {
        return _origGet<T>(url, config);
    }
    // [BUG-3] 这里只是为了触发一次 storage 漂移检测并拿 epoch 做 dedupe key,
    // 会话在途不该让它抛错 —— 真正的 fail-closed 由下游 interceptor 守住。
    peekConfirmedSessionToken();
    const sessionScope = getAuthorizationEpoch();
    const headerBag = new Headers(config?.headers as HeadersInit | undefined);
    const demoSelection = getActiveDemoSelection();
    const demoScope = demoSelection && shouldAttachDemoSelection(url)
        ? `${demoSelection.brandId}:${demoSelection.caseId}`
        : 'real';
    const key = buildDedupeKey(
        'get',
        url,
        params,
        sessionScope,
        config?.responseType,
        headerBag.get('Accept') || undefined,
        demoScope,
    );
    if (key === null) return _origGet<T>(url, config);
    const cached = PENDING_GET_REQUESTS.get(key);
    if (cached && !cached.settled) {
        return subscribeToGet<T>(key, cached, config?.signal as AbortSignal | undefined);
    }
    const controller = new AbortController();
    const promise = _origGet<T>(url, {
        ...config,
        signal: controller.signal,
        __omnirankManagedSignal: true,
        __omnirankCallerSignal: config?.signal as AbortSignal | undefined,
    } as import('axios').AxiosRequestConfig).then((value) => {
        const entry = PENDING_GET_REQUESTS.get(key);
        if (entry?.promise === promise) {
            entry.settled = true;
            PENDING_GET_REQUESTS.delete(key);
        }
        return value;
    }, (error) => {
        if (PENDING_GET_REQUESTS.get(key)?.promise === promise) PENDING_GET_REQUESTS.delete(key);
        throw error;
    });
    PENDING_GET_REQUESTS.set(key, {
        promise: promise as Promise<unknown>,
        settled: false,
        controller,
        subscribers: new Set(),
        tags: apiResourceTags(url),
    });
    return subscribeToGet<T>(key, PENDING_GET_REQUESTS.get(key)!, config?.signal as AbortSignal | undefined);
} as typeof api.get;

// #17 修复：api 实例的 401 响应拦截器
// 处理 token 过期自动登出 和 PERMISSION_CHANGED 自动重试
// v3.8 CTO-15.0: refresh 失败加 1 次重试（2s backoff） · 仅 INVALID_TOKEN 才清 token
// 背景：老板反馈 PC "用着用着被踢需重登录"，手机端同账号没事 → 后端软刷新偶发抖动 + 前端 refresh 失败立即跳登录
// D 任务 (2026-05-06): tryRefreshOnce 删除 · 4 处 refresh 全部复用顶部 acquireRefreshedToken (含并发锁)

api.interceptors.response.use(
    response => {
        if (isMutationMethod(response.config?.method)) {
            invalidateApiResourcesForMutation(String(response.config?.url || ''));
        }
        return response;
    },
    async error => {
        notifyDemoActionPreview(error.response?.data);
        const retryConfig = error.config as (import('axios').AxiosRequestConfig & { __retryAfter429?: boolean }) | undefined;
        if (
            error.response?.status === 429
            && retryConfig
            && String(retryConfig.method || 'get').toLowerCase() === 'get'
            && !retryConfig.__retryAfter429
        ) {
            retryConfig.__retryAfter429 = true;
            const raw = String(error.response.headers?.['retry-after'] ?? '').trim();
            const seconds = Number(raw);
            const retryAt = raw && Number.isFinite(seconds)
                ? Date.now() + Math.max(0, seconds) * 1000
                : (raw ? Date.parse(raw) : Number.NaN);
            const waitMs = Number.isFinite(retryAt)
                ? Math.max(0, retryAt - Date.now())
                : 1000;
            // Never retry before the server permits it. For a long backoff, return
            // the 429 to the caller so the UI can retain stale data and expose the
            // fault instead of keeping a page request alive for minutes.
            if (waitMs > 60_000) return Promise.reject(error);
            await new Promise<void>((resolve, reject) => {
                const signal = retryConfig.signal as AbortSignal | undefined;
                const timer = window.setTimeout(() => {
                    signal?.removeEventListener('abort', onAbort);
                    resolve();
                }, waitMs);
                const onAbort = () => {
                    window.clearTimeout(timer);
                    reject(new axios.CanceledError('request canceled during Retry-After'));
                };
                if (signal?.aborted) onAbort();
                else signal?.addEventListener('abort', onAbort, { once: true });
            });
            return api.request(retryConfig);
        }
        if (error.response?.status === 401) {
            const code = error.response?.data?.code;
            const config = error.config as (typeof error.config & ReplayAwareConfig & {
                __authRefreshAttempted?: boolean;
                __permissionRefreshAttempted?: boolean;
            });
            const requestSessionToken = requestBearerToken(config?.headers);
            const requestAuthority = config?.__omnirankRequestAuthority
                || { token: requestSessionToken, epoch: getAuthorizationEpoch() };
            const canAutoReplay = isSafeAuthReplayMethod(config?.method);

            if (code === 'PERMISSION_CHANGED' && config && !config.__authRefreshAttempted) {
                config.__authRefreshAttempted = true;
                const refreshOutcome = await refreshOutcomeForRequestAuthority(requestAuthority, 2);
                if (refreshOutcome.status === 'refreshed'
                    && canReplayRequestAuthority(requestAuthority, refreshOutcome.authority)
                    && canAutoReplay) {
                    return replayAxiosRequest(config, refreshOutcome.authority);
                }
                console.warn('[auth] PERMISSION_CHANGED 未自动重放原请求');
                return Promise.reject(error);
            }

            if (!code && config && !config.__authRefreshAttempted) {
                config.__authRefreshAttempted = true;
                const refreshOutcome = await refreshOutcomeForRequestAuthority(requestAuthority, 1);
                if (refreshOutcome.status === 'refreshed'
                    && canReplayRequestAuthority(requestAuthority, refreshOutcome.authority)
                    && canAutoReplay) {
                    return replayAxiosRequest(config, refreshOutcome.authority);
                }
                if (refreshOutcome.status === 'hard-invalid'
                    && isCurrentSessionAuthority(requestAuthority as SessionAuthority)) {
                    expireSessionIfPrivate(requestAuthority);
                }
                return Promise.reject(error);
            }

            const shouldLogout = code === 'INVALID_TOKEN'
                || code === 'USER_NOT_FOUND'
                || code === 'ACCOUNT_DISABLED';
            const confirmedExpired = !code && config?.__authRefreshAttempted && canAutoReplay;
            if ((shouldLogout || confirmedExpired)
                && isCurrentSessionAuthority(requestAuthority as SessionAuthority)) {
                expireSessionIfPrivate(requestAuthority);
            } else if (!shouldLogout && !confirmedExpired) {
                console.warn(`[auth] 401 code=${code || '(none)'}，保守处理不清 token`);
            }
        }
        if (error.response?.status === 403) {
            // [治理 §13] detail 现可能是一个告警合同对象(而非字符串);此分支只做基于
            // 字符串匹配的 brand-RBAC 重放判定,对象 detail 归一为空串,既避免 .includes
            // 抛 TypeError,也不误触发重放(合同层由组件消费,不在拦截器里处理)。
            const rawDetail = error.response?.data?.detail;
            const detail = typeof rawDetail === 'string' ? rawDetail : '';
            const config = error.config as (typeof error.config & ReplayAwareConfig & { __permissionRefreshAttempted?: boolean });
            // Demo grants are independent, revocable capabilities. A 403 on a
            // request carrying the exact demo authority must never rotate the
            // account JWT or replay the request as if ordinary brand RBAC had
            // changed; doing so can rebuild ClientContext into a request loop.
            if (requestHasDemoAuthority(config?.headers)) {
                return Promise.reject(error);
            }
            const requestSessionToken = requestBearerToken(config?.headers);
            const requestAuthority = config?.__omnirankRequestAuthority
                || { token: requestSessionToken, epoch: getAuthorizationEpoch() };
            if (!config?.__permissionRefreshAttempted
                && (detail.includes('brand_id') || detail.includes('无权访问') || detail.includes('BRAND_ACCESS'))) {
                config.__permissionRefreshAttempted = true;
                const refreshOutcome = await refreshOutcomeForRequestAuthority(requestAuthority, 2);
                if (refreshOutcome.status === 'refreshed'
                    && canReplayRequestAuthority(requestAuthority, refreshOutcome.authority)
                    && isSafeAuthReplayMethod(config.method)) {
                    return replayAxiosRequest(config, refreshOutcome.authority);
                }
            }
        }
        return Promise.reject(error);
    }
);

// ==========================================
// 诊断相关API
// ==========================================

export interface DiagnosisRequest {
    brand_name: string;
    industry: string;
    keywords: string[];
    additional_info?: string;
    own_accounts?: string[];
    competitors?: string[];
    /** 后端 server.py:1078 真实接受 · M3 NewDiagnosis 需要(CTO-15.13 A3.2) */
    brand_id?: number;
    diagnosis_scope?: 'geo' | 'social' | 'full';
    brand_display_names?: string[];
    client_location?: string;
    /** [WORKERS=4 · P0-1] 请求级幂等标识:每次"提交意图"生成稳定 UUID,超时/网络重试**复用同一个**,
     *  防重复冻结积分(后端 uq_diag_request_idem)。翻 WORKERS>1 后付费诊断缺此字段将被 400 拒。
     *  必须由发起处经 newDiagnosisRequestId() 生成并绑定按钮 pending 态,不得在重试时重新生成。 */
    client_request_id?: string;
    custom_questions?: string[];
    ai_optimize_custom?: boolean;
}

/** [WORKERS=4 · P0-1] 生成一个新的诊断请求幂等 ID(每次"新提交意图"调一次)。
 *  重试/重发同一次提交时**复用**上次的返回值,不要重新调用本函数。 */
export function newDiagnosisRequestId(): string {
    // safeRandomUUID 自带 randomUUID → getRandomValues → Math.random 三层退化,
    // 原来这里手写的兜底与它重复(工单 2026-08-06 §4)。
    return 'c_' + safeRandomUUID();
}

/** [返工2 P1-1] 诊断请求幂等 ID 短暂持久化(sessionStorage · TTL 10min)。
 *  刷新页面/组件重挂时**复用**在途提交的同一 ID → 避免"提交中刷新 → 新 ID → 重复冻结积分"。
 *  key 建议按 brand/意图区分(如 `m3confirm:${brandId}`)。 */
const _DIAG_REQ_TTL_MS = 10 * 60 * 1000;
export function loadOrCreateDiagnosisRequestId(key: string): string {
    const storeKey = 'diagreq:' + key;
    try {
        const raw = sessionStorage.getItem(storeKey);
        if (raw) {
            const o = JSON.parse(raw);
            if (o && typeof o.id === 'string' && typeof o.ts === 'number' && (Date.now() - o.ts) < _DIAG_REQ_TTL_MS) {
                return o.id;
            }
        }
    } catch { /* sessionStorage 不可用 · 退化为每次新生成 */ }
    const id = newDiagnosisRequestId();
    try { sessionStorage.setItem(storeKey, JSON.stringify({ id, ts: Date.now() })); } catch { /* ignore */ }
    return id;
}

/** [返工2 P1-1] 清除持久化的诊断请求 ID(成功 或 确定性失败 后调用 → 下次提交是全新意图)。 */
export function clearDiagnosisRequestId(key: string): void {
    try { sessionStorage.removeItem('diagreq:' + key); } catch { /* ignore */ }
}

/** [返工2 P1-1] 是否为**确定性**诊断拒绝(余额不足 402 / 参数非法 400)。
 *  确定性拒绝 = 该请求已终结(旧 run 变 cancelled)· 重试应换**新意图 ID**(否则后端幂等命中旧 cancelled run,
 *  页面跳进永不运行的死任务)。仅网络超时/断连/5xx(结果未知)才复用同一 ID 去重。 */
export function isDeterministicDiagnosisReject(err: any): boolean {
    const s = err?.response?.status;
    return s === 400 || s === 402;
}

export interface DiagnosisRecord {
    id: number;
    session_id?: string;
    brand_id?: number;
    brand_name: string;
    industry: string;
    industry_category?: string;
    /** [WO_267] 后端附的大类 key / 中文名(部分端点才有);显示一律走 industryCategoryText */
    industry_category_key?: string | null;
    industry_category_name?: string | null;
    total_score: number;  // 后端实际字段名
    level: string;
    created_at: string;
    report_md_path?: string;
    article_count?: number;
    diagnosis_type?: string;  // 旧版: 'sales_lite' | 'technical_full', 新版: 'geo' | 'social' | 'full'
}

export const diagnosisApi = {
    // 启动诊断
    start: (data: DiagnosisRequest) =>
        api.post<{ session_id: string; message: string }>('/api/diagnosis/start', data),

    // 获取诊断详情
    getDetail: (id: number) =>
        api.get<DiagnosisRecord>(`/api/diagnosis/${id}`),

    // 获取报告内容(Markdown)· CTO-B W4 · 支持 v2 audience 切换 + 完整度元数据
    getContent: (id: number, audience: 'internal' | 'client' = 'internal') =>
        api.get<{
            content?: string;
            error?: string;
            version?: 'v1' | 'v2';
            audience?: string;
            data_completeness_score?: number | null;
            data_completeness_breakdown?: {
                level?: string;
                groups?: Array<{ label: string; score: number; weight: number; percent: number; missing_fields?: Array<{ label: string; weight: number }> }>;
                impact_notes?: string[];
                missing_summary?: string;
            } | null;
            report_v2_error?: string | null;
        }>(`/api/diagnosis/${id}/content?audience=${audience}`),

    // 搜索诊断记录
    search: (query: string) =>
        api.get<DiagnosisRecord[]>(`/api/diagnoses/search?q=${encodeURIComponent(query)}`),

    // 获取复测预填数据
    getRetestData: (id: number) =>
        api.get<{
            brand_name: string;
            industry: string;
            keywords: string[];
            original_score: number;
            original_level: string;
            original_date: string;
        }>(`/api/diagnosis/${id}/retest-data`),

    // LLM 自动填写表单（支持指定 provider: doubao / dashscope / kimi）
    // userCity / userIndustry: 用户已填的城市和行业（强约束，防止 AI 幻觉覆盖）
    autofill: (brandName: string, provider?: string, userCity?: string, userIndustry?: string) =>
        api.post<{
            success: boolean;
            provider?: string;
            data: {
                industry: string;
                keywords: string[];
                clientLocation: string;
                additionalInfo: string;
                competitors: string[];
            };
        }>('/api/diagnosis/autofill', {
            brand_name: brandName,
            provider: provider || '',
            user_city: userCity || '',
            user_industry: userIndustry || '',
        }),

    // 获取品牌历史趋势
    getBrandTrend: (brandName: string) =>
        api.get<{
            brand_name: string;
            total_records: number;
            trend: Array<{
                id: number;
                score: number;
                level: string;
                date: string;
                dimensions: Record<string, number>;
            }>;
        }>(`/api/brand/${encodeURIComponent(brandName)}/trend`),
};

// ==========================================
// 历史和统计API
// ==========================================

// 后端实际返回的统计格式
export interface Statistics {
    total_records: number;
    average_score: number;
    level_distribution: Record<string, number>;
    industry_distribution: Record<string, number>;
}

// 兼容前端使用的格式转换
export interface StatsDisplay {
    total_diagnoses: number;
    total_articles: number;
    monthly_diagnoses: number;
    avg_score: number;
    // 新增：Token和成本统计
    total_cost?: number;
    total_calls?: number;
    cost_per_article?: number;
    cost_per_call?: number; // New metric
    total_words?: number;
    article_success_rate?: number;
    level_distribution?: Record<string, number>;
    article_type_distribution?: Record<string, number>;
    // 2026-05-17 媒体发布外采(独立 LLM 总成本 · 给 mhz 平台付的钱)
    publish_external_cost_yuan?: number;
    publish_external_cost_points?: number;
    publish_orders_count?: number;
    publish_items_count?: number;
    publish_status_breakdown?: Record<string, { count: number; cost_yuan: number }>;
}

export const historyApi = {
    // 获取历史记录 - 后端返回 { data: [...] } 格式
    list: async (params?: { limit?: number; offset?: number; days?: number; brand?: string; brand_id?: number }): Promise<{ data: DiagnosisRecord[] }> => {
        const res = await api.get<{ data: DiagnosisRecord[] }>('/api/history', { params });
        return { data: res.data.data || res.data as unknown as DiagnosisRecord[] };
    },

    // 获取统计数据 - 使用新的后端格式
    getStats: async (days: number = 30): Promise<{ data: StatsDisplay }> => {
        const res = await api.get<{
            total_diagnoses?: number; total_articles?: number; average_score?: number;
            total_cost?: number; total_calls?: number; cost_per_article?: number; cost_per_call?: number;
            total_words?: number; article_success_rate?: number;
            level_distribution?: Record<string, number>; article_type_distribution?: Record<string, number>;
            publish_external_cost_yuan?: number; publish_external_cost_points?: number;
            publish_orders_count?: number; publish_items_count?: number;
            publish_status_breakdown?: Record<string, { count: number; cost_yuan: number }>;
        }>(`/api/statistics?days=${days}`);
        const stats = res.data;
        return {
            data: {
                total_diagnoses: stats.total_diagnoses || 0,
                total_articles: stats.total_articles || 0,
                monthly_diagnoses: stats.total_diagnoses || 0,
                avg_score: stats.average_score || 0,
                // 新增字段
                total_cost: stats.total_cost || 0,
                total_calls: stats.total_calls || 0,
                cost_per_article: stats.cost_per_article || 0,
                cost_per_call: stats.cost_per_call || 0, // Map new field
                total_words: stats.total_words || 0,
                article_success_rate: stats.article_success_rate || 0,
                level_distribution: stats.level_distribution || {},
                article_type_distribution: stats.article_type_distribution || {},
                publish_external_cost_yuan: stats.publish_external_cost_yuan || 0,
                publish_external_cost_points: stats.publish_external_cost_points || 0,
                publish_orders_count: stats.publish_orders_count || 0,
                publish_items_count: stats.publish_items_count || 0,
                publish_status_breakdown: stats.publish_status_breakdown || {},
            }
        };
    },

    // 🆕 获取增强统计（社媒/AI协作/知识库/时间线）
    getEnhancedStats: async (): Promise<{
        social_stats: { topics_count: number; scripts_count: number; pending_count: number; published_count: number };
        ai_stats: { meetings_count: number; tasks_count: number; active_advisors: number };
        knowledge_stats: { client_docs: number; role_docs: number };
        recent_activities: Array<{ type: string; icon: string; title: string; relative_time: string }>;
    }> => {
        const res = await api.get<{ data?: { social_stats: { topics_count: number; scripts_count: number; pending_count: number; published_count: number }; ai_stats: { meetings_count: number; tasks_count: number; active_advisors: number }; knowledge_stats: { client_docs: number; role_docs: number }; recent_activities: Array<{ type: string; icon: string; title: string; relative_time: string }> } }>('/api/dashboard/enhanced-stats');
        return res.data.data || {
            social_stats: { topics_count: 0, scripts_count: 0, pending_count: 0, published_count: 0 },
            ai_stats: { meetings_count: 0, tasks_count: 0, active_advisors: 0 },
            knowledge_stats: { client_docs: 0, role_docs: 0 },
            recent_activities: []
        };
    },

    // 删除诊断记录
    delete: (id: number) =>
        api.delete(`/api/diagnosis/${id}`),
};

// ==========================================
// 品牌管理API
// ==========================================

export interface Brand {
    id: number;
    brand_code?: string;  // 唯一编号，如 BRD-0001
    name: string;
    company_name?: string;
    industry?: string;
    industry_category?: string;
    /** [WO_267] 后端附的大类 key / 中文名(部分端点才有);显示一律走 industryCategoryText */
    industry_category_key?: string | null;
    industry_category_name?: string | null;
    diagnosis_count: number;
    latest_score?: number;
    latest_diagnosis_id?: number;
    latest_diagnosis_at?: string | null;  // [CTO-15.23 2026-05-11 P0#1] 报价页客户下拉按此排序
    contact?: string;
    notes?: string;
    status?: 'active' | 'undecided' | 'won' | 'archived';
    social_enabled?: boolean;
    created_at: string;
    updated_at: string;
}

export interface BrandTrend {
    diagnoses: Array<{
        id: number;
        total_score: number;
        level: string;
        created_at: string;
        web_search_score?: number;
        platform_score?: number;
        content_quality_score?: number;
        authority_score?: number;
        brand_ownership_score?: number;
        ai_visibility_score?: number;
        ai_citation_score?: number;
        update_frequency_score?: number;
    }>;
    total_count: number;
    trend_insight: string;
}

export const brandsApi = {
    // 获取品牌列表（支持搜索）
    list: (params?: { search?: string; industry_category?: string; limit?: number; offset?: number }) =>
        api.get<{ data: Brand[] }>('/api/brands', { params }),

    // 获取品牌详情
    getById: (id: number) =>
        api.get<{ data: Brand }>(`/api/brands/${id}`),

    // 获取品牌的所有诊断记录
    getDiagnoses: (id: number, limit: number = 50) =>
        api.get<{ data: DiagnosisRecord[] }>(`/api/brands/${id}/diagnoses`, { params: { limit } }),

    // 获取品牌趋势数据
    getTrend: (id: number) =>
        api.get<{ data: BrandTrend }>(`/api/brands/${id}/trend`),

    // 执行数据迁移
    migrate: () =>
        api.post<{ data: { brands: number; diagnoses: number }; message: string }>('/api/brands/migrate'),

    // 删除品牌（需要密码）
    delete: (id: number, password: string) =>
        api.delete<{ status: string; message: string }>(`/api/brands/${id}`, { data: { password } }),

    // 合并品牌
    merge: (data: { primary_brand_id: number; merge_brand_ids: number[]; display_name?: string; password: string }) =>
        api.post<{ status: string; message: string; primary_brand_id: number; merged_count: number }>('/api/brands/merge', data),
};

// ==========================================
// 文章生成API
// ==========================================

export interface ArticlePlanRequest {
    diagnosis_id: number;
    total_count: number;
    skip_titles?: boolean;  // Step 1: 只获取分配方案，不生成标题
    distribution?: { [key: string]: number };  // Step 2: 用户调整后的分配方案
}

// 文章分配 - 使用索引签名支持动态类型 (8种CONTENT_ANGLES)
export interface ArticleDistribution {
    [key: string]: number;
}

export interface ArticleGenerateRequest {
    diagnosis_id: number;
    total_count: number;
    article_distribution: ArticleDistribution;
    model_key?: string;
    max_concurrent?: number;
    use_cache_data?: boolean;
    // 新增：用户确认的标题列表（跳过重新生成）
    topics?: Array<{
        id: number;
        title: string;
        style: string;
        styleName?: string;
    }>;
}

export interface ArticleItem {
    filename: string;
    filepath: string;
    type: string;
    size: number;
    created_at: string;
    batch?: string;
}

export interface ArticleProgress {
    status: string;
    progress: number;
    completed: number;
    total: number;
    logs: string[];
}

export const articlesApi = {
    // 规划文章分配
    plan: (data: ArticlePlanRequest) =>
        api.post<{ distribution: ArticleDistribution }>('/api/articles/plan', data),

    // 批量生成文章
    generate: (data: ArticleGenerateRequest) =>
        api.post<{ task_id: string }>('/api/articles/generate', data),

    // 获取生成进度
    getProgress: (taskId: string) =>
        api.get<ArticleProgress>(`/api/articles/progress/${taskId}`),

    // 获取文章列表
    list: (diagnosisId: number) =>
        api.get<{ articles: ArticleItem[]; batches: string[] }>(`/api/articles/list/${diagnosisId}`),

    // 获取文章内容
    getContent: (filePath: string) =>
        api.post<{ content: string }>('/api/articles/content', { file_path: filePath }),

    // 批量下载(ZIP)
    downloadZip: (filePaths: string[]) =>
        api.post('/api/articles/download-zip', { file_paths: filePaths }, { responseType: 'blob' }),

    // 补发文章
    replace: (data: { diagnosis_id: number; original_title: string; article_type?: string; instruction?: string }) =>
        api.post('/api/articles/replace', data),

    // 删除单篇文章(/api/articles/delete 已 410 下线 · 重指向已加固的批量删除端点)
    delete: (filePath: string) =>
        api.post('/api/articles/batch-delete', { file_paths: [filePath] }),

    // 批量删除文章
    batchDelete: (filePaths: string[]) =>
        api.post('/api/articles/batch-delete', { file_paths: filePaths }),
};

// ==========================================
// 销售工具API
// ==========================================

export const toolsApi = {
    // 生成销售策略
    createStrategy: (diagnosisId: number, includeCompetitors: boolean = true) =>
        api.post('/api/strategy', { diagnosis_id: diagnosisId, include_competitors: includeCompetitors }),

    // 生成报价单
    createQuote: (data: { diagnosis_id?: number; brand_name?: string; package_type?: string }) =>
        api.post('/api/quote', data),
};

// ==========================================
// 系统设置API
// 子任务LLM配置
export interface TaskLLMConfig {
    provider: string;
    model: string;
}

export interface SystemSettings {
    dashscope_api_key: string;
    deepseek_api_key: string;
    openrouter_api_key: string;
    doubao_api_key: string;
    kimi_api_key: string;
    tikhub_api_key: string;
    diagnosis_model: string;
    diagnosis_provider: string;
    diagnosis_tasks: Record<string, TaskLLMConfig>;
    writing_model: string;
    writing_provider: string;
    writing_tasks: Record<string, TaskLLMConfig>;
    content_ratios: Record<string, number>;
    style_ratios: Record<string, number>;
    industry_overrides: Record<string, {
        content_ratios?: Record<string, number>;
        style_ratios?: Record<string, number>;
    }>;
    concurrent_writers: number;
    default_article_count: number;
    report_v3_enabled: boolean;
    report_v3_whitelist_user_ids: number[];
    report_v3_auto_enrich: boolean;
    llm_narrative_monthly_yuan: number;
    llm_narrative_max_concurrent: number;
    llm_narrative_alert_yuan: number;
}

export const settingsApi = {
    // 获取当前设置
    get: () =>
        api.get<SystemSettings>('/api/settings'),

    // 更新设置
    update: (settings: SystemSettings) =>
        api.put('/api/settings', settings),

    // 测试API连接
    testConnection: (provider: string) =>
        api.post<{ success: boolean; message: string }>('/api/settings/test-connection', { provider }),

    // 批量健康检查（SSE流式）- 返回端点URL供前端 fetch 使用
    healthCheckUrl: '/api/settings/health-check',
};

// ==========================================
// 操作确认码管理API
// ==========================================

export interface ConfirmCodeInfo {
    action: string;
    label: string;
    has_code: boolean;
    updated_at: string;
    updated_by: string;
}

export const confirmCodeApi = {
    // 获取所有确认码状态
    list: () =>
        api.get<{ success: boolean; codes: ConfirmCodeInfo[] }>('/api/admin/confirm-codes'),

    // 设置/修改确认码
    set: (action: string, code: string) =>
        api.put<{ success: boolean; action: string; updated_at: string }>(
            `/api/admin/confirm-codes/${action}`,
            { code }
        ),

    // 删除确认码
    remove: (action: string) =>
        api.delete<{ success: boolean }>(`/api/admin/confirm-codes/${action}`),
};

// ==========================================
// 🆕 统一知识库管理API
// ==========================================

export interface KBDocument {
    filename: string;
    relative_path: string;
    size: number;
    modified: string;
}

export interface KBStats {
    public: { file_count: number; total_size: number };
    clients: Record<string, { file_count: number; total_size: number }>;
    roles: {
        advisors: Record<string, { file_count: number; total_size: number }>;
        employees: Record<string, { file_count: number; total_size: number }>;
    };
}

export interface KBUploadRequest {
    kb_type: 'public' | 'client' | 'role';
    kb_id?: string;
    role_type?: 'advisor' | 'employee';
    filename: string;
    content: string;
}

export const knowledgeApi = {
    // 获取统计信息
    getStats: () =>
        api.get<{ status: string; stats: KBStats }>('/api/knowledge/stats'),

    // 列出文档
    list: (kbType: string, kbId?: string, roleType?: string) =>
        api.get<{ status: string; documents: KBDocument[]; total: number }>(
            `/api/knowledge/${kbType}`,
            { params: { kb_id: kbId || '', role_type: roleType || '' } }
        ),

    // 上传文档
    upload: (data: KBUploadRequest) =>
        api.post<{ status: string; path?: string; size?: number; error?: string; pipeline?: Record<string, unknown> }>(
            '/api/knowledge/upload',
            data
        ),

    // 删除文档
    delete: (kbType: string, kbId: string, filename: string, roleType?: string) =>
        api.delete<{ status: string; message?: string; error?: string }>(
            `/api/knowledge/${kbType}/${kbId}/${encodeURIComponent(filename)}`,
            { params: { role_type: roleType || '' } }
        ),

    // 🆕 语义检索（向量检索 + Rerank）
    search: (query: string, brandId?: number, topK: number = 5, useRerank: boolean = true) =>
        api.post<{
            status: string;
            results: Array<{
                content: string;
                filename: string;
                brand_id: number;
                keywords: string;
                score: number;
            }>;
            reranked?: boolean;
            error?: string;
        }>('/api/knowledge/search', {
            query,
            brand_id: brandId,
            top_k: topK,
            use_rerank: useRerank
        }),

    // ========== 🆕 角色知识库专用方法 ==========

    // 获取角色知识库文档列表
    listRoleDocuments: (roleType: 'advisor' | 'employee', roleId: string) =>
        api.get<{ success: boolean; documents: KBDocument[]; total: number }>(
            `/api/knowledge/role/${roleType}/${roleId}`
        ),

    // 上传文档到角色知识库
    uploadRoleDocument: (roleType: 'advisor' | 'employee', roleId: string, file: File) => {
        const formData = new FormData();
        formData.append('file', file);
        // 设置Content-Type为undefined覆盖全局设置，让浏览器自动生成multipart边界
        // 超时30分钟，支持大文件（1MB+）处理
        return api.post<{ success: boolean; message: string; path?: string; size?: number }>(
            `/api/knowledge/role/${roleType}/${roleId}/upload`,
            formData,
            {
                headers: { 'Content-Type': undefined },
                timeout: 1800000  // 30分钟超时
            }
        );
    },

    // 删除角色知识库文档
    deleteRoleDocument: (roleType: 'advisor' | 'employee', roleId: string, filename: string) =>
        api.delete<{ success: boolean; message: string }>(
            `/api/knowledge/role/${roleType}/${roleId}/${encodeURIComponent(filename)}`
        ),

    // ========== 🆕 公共知识库专用方法 ==========

    // 获取公共知识库文档列表
    listPublicDocuments: () =>
        api.get<{ success: boolean; documents: KBDocument[]; total: number }>(
            '/api/knowledge/public'
        ),

    // 上传文档到公共知识库
    uploadPublicDocument: (file: File) => {
        const formData = new FormData();
        formData.append('file', file);
        return api.post<{ success: boolean; message: string; path?: string; size?: number }>(
            '/api/knowledge/public/upload',
            formData,
            { headers: { 'Content-Type': 'multipart/form-data' } }
        );
    },

    // 删除公共知识库文档
    deletePublicDocument: (filename: string) =>
        api.delete<{ success: boolean; message: string }>(
            `/api/knowledge/public/${encodeURIComponent(filename)}`
        ),
};

// ==========================================
// 客户上下文 API (Step 0)
// ==========================================

export interface ClientBrandSummary {
    id: number;
    name: string;
    brand_code?: string;
    industry?: string;
    industry_category?: string;
    /** [WO_267] 后端附的大类 key / 中文名;显示一律走 industryCategoryText */
    industry_category_key?: string | null;
    industry_category_name?: string | null;
    company_name?: string;
    latest_score?: number;
    diagnosis_count: number;
    latest_diagnosis_id?: number;
    quote_count: number;
    quote_status?: 'confirmed' | 'paid' | 'draft' | null;
    brand_status?: 'active' | 'undecided' | 'won' | 'archived';
    client_status?: 'active' | 'undecided' | 'won' | 'archived';
    status_label?: string;
    owner_user_id?: number | null;
    owner_name?: string | null;
    owner_account?: string | null;
    created_at: string;
    access_mode?: 'real' | 'demo';
    is_demo?: boolean;
    demo_case_id?: string;
    demo_expires_at?: string;
}

export interface ClientContextDetail {
    brand: {
        id: number;
        name: string;
        brand_code?: string;
        industry?: string;
        industry_category?: string;
        /** [WO_267] 后端附的大类 key / 中文名;显示一律走 industryCategoryText */
        industry_category_key?: string | null;
        industry_category_name?: string | null;
        company_name?: string;
        // [CTO-15.23 2026-05-07 P0-3] 加 cities · backend SELECT * 已返 · 让 TS 知道
        cities?: string;
        latest_score?: number;
        latest_diagnosis_id?: number | null;
        latest_diagnosis_created_at?: string | null;
        diagnosis_count: number;
        brand_type?: 'self' | 'client' | 'legacy';
    };
    profile: Record<string, string | number | boolean | null> | null;
    materials: Record<string, string | number | boolean | null> | null;
    relatedQuoteIds: string[];
    socialProjects: Array<{ id: number; name: string }>;
    access_mode?: 'real' | 'demo';
    demo_case_id?: string;
    demo_expires_at?: string;
    demo_watermark?: string;
    demo_frozen_at?: string | null;
}

export const clientContextApi = {
    // 获取品牌列表（含最新诊断分、关联数量）
    list: (signal?: AbortSignal) =>
        api.get<{ success: boolean; clients: ClientBrandSummary[] }>('/api/client-context/list', { signal }),

    // 获取指定品牌的完整上下文（品牌+档案+素材+报价+社媒项目）
    getContext: (brandId: number, signal?: AbortSignal) =>
        api.get<{ success: boolean; context: ClientContextDetail; redirect_to?: number }>(`/api/client-context/${brandId}`, { signal }),

    // Demo customers never use the ordinary live customer endpoint.  This
    // adapter projects the dedicated frozen case DTO into the existing context
    // shape so current presentation pages can keep their navigation shell.
    getDemoContext: async (
        caseId: string,
        brandId: number,
        signal?: AbortSignal,
    ): Promise<{ data: { success: boolean; context: ClientContextDetail } }> => {
        const response = await api.get<{
            success: boolean;
            case: {
                case_id: string;
                brand_id: number;
                diagnosis_id: number;
                expires_at?: string;
                customer_overview: Record<string, unknown>;
                diagnosis_snapshot: Record<string, unknown>;
                quote_snapshots: Array<Record<string, unknown>>;
                frozen_at?: string | null;
            };
        }>(`/api/demo-cases/${encodeURIComponent(caseId)}`, {
            signal,
            headers: {
                'X-Demo-Brand-ID': String(brandId),
                'X-Demo-Case-ID': caseId,
            },
        });
        const item = response.data.case;
        const overview = item.customer_overview || {};
        const diagnosis = item.diagnosis_snapshot || {};
        const context: ClientContextDetail = {
            brand: {
                id: item.brand_id,
                name: String(overview.brand_name || overview.name || '演示客户'),
                industry: typeof overview.industry === 'string' ? overview.industry : undefined,
                industry_category: typeof overview.industry_category === 'string' ? overview.industry_category : undefined,
                // [WO_267] 演示概览若附了大类中文名就一起带上;下游显示一律走 industryCategoryText
                industry_category_name: typeof overview.industry_category_name === 'string' ? overview.industry_category_name : undefined,
                latest_score: typeof diagnosis.total_score === 'number' ? diagnosis.total_score : undefined,
                latest_diagnosis_id: item.diagnosis_id,
                latest_diagnosis_created_at: typeof diagnosis.created_at === 'string' ? diagnosis.created_at : undefined,
                diagnosis_count: 1,
                brand_type: 'client',
            },
            profile: { industry: typeof overview.industry === 'string' ? overview.industry : null },
            materials: null,
            relatedQuoteIds: (item.quote_snapshots || [])
                .map(quote => quote.id)
                .filter(id => id !== null && id !== undefined)
                .map(String),
            socialProjects: [],
            access_mode: 'demo',
            demo_case_id: item.case_id,
            demo_expires_at: item.expires_at,
            demo_watermark: '演示案例',
            demo_frozen_at: item.frozen_at,
        };
        return { data: { success: true, context } };
    },
};

// ==========================================
// 报价经营包(GEO 域 · 持久化 + 客户公开分享 · 2026-06-17)
// ==========================================
export interface OperationPackage {
    // 客户/通用
    key?: string;
    name: string;
    scope?: string;
    fit_scene?: string;
    customer_copy?: string;
    search_item_min?: number;
    search_item_max?: number;
    search_item_count?: number | string;
    customer_price_min?: number | null;
    customer_price_max?: number | null;
    // 服务商管理字段(仅登录态服务商视图)
    id?: number;
    enabled?: boolean;
    sort_order?: number;
    template_key?: string | null;
    // 服务商成本/经营空间(仅服务商视图 · 客户面绝无)
    base_cost_min?: number;
    base_cost_max?: number;
    base_suggested_price_min?: number;
    base_suggested_price_max?: number;
    base_margin_min?: number;
    base_margin_max?: number;
    sub_cost_min?: number;
    sub_cost_max?: number;
    sub_suggested_price_min?: number;
    sub_suggested_price_max?: number;
    sub_margin_min?: number;
    sub_margin_max?: number;
    agent_space_min?: number;
    agent_space_max?: number;
    agent_space_note?: string;
}

export interface OperationPackageUpdate {
    name?: string;
    scope?: string;
    fit_scene?: string;
    customer_copy?: string;
    search_item_min?: number;
    search_item_max?: number;
    customer_price_min?: number;
    customer_price_max?: number;
    enabled?: boolean;
    sort_order?: number;
}

export const operationPackagesApi = {
    // 登录态:按身份返回经营包(服务商=持久化行·首访 seed)
    list: () =>
        api.get<{
            success: boolean; role: string; persisted: boolean;
            packages: OperationPackage[]; economics_note?: string | null; note?: string;
        }>('/api/operation-packages'),

    // 服务商:编辑自己的经营包
    update: (id: number, data: OperationPackageUpdate) =>
        api.patch<{ success: boolean; package: OperationPackage }>(`/api/operation-packages/${id}`, data),

    // 服务商:复制为自定义副本
    copy: (id: number) =>
        api.post<{ success: boolean; package: OperationPackage }>(`/api/operation-packages/${id}/copy`),

    // 服务商:软删
    remove: (id: number) =>
        api.delete<{ success: boolean }>(`/api/operation-packages/${id}`),

    // 服务商:批量排序
    reorder: (ids: number[]) =>
        api.post<{ success: boolean; updated: number }>('/api/operation-packages/reorder', { ids }),

    // 服务商:取/建客户公开分享 token
    shareToken: () =>
        api.post<{ success: boolean; token: string; share_path: string; is_test: boolean }>(
            '/api/operation-packages/share-token'),

    // 客户公开视图(无登录 · token 走 query · 仅客户白名单字段)
    listPublic: (token?: string) =>
        api.get<{ success: boolean; role: string; expired?: boolean; packages: OperationPackage[] }>(
            '/api/operation-packages/public', { params: token ? { token } : {} }),
};

// 导出默认api实例供其他地方使用
export default api;


/**
 * [F-2] 从任意 API 错误/响应里安全取出**可渲染的**用户文案。
 *
 * 背景:后端 §13 告警合同把 `detail` 变成了对象({code,message,reason,impact,actions,...}),
 * 而历史写法普遍是 `toast.error(e?.response?.data?.detail || '失败')` —— 传对象给 toast
 * 渲染不出来(用户看到空白/[object Object],等于"点了没反应")。
 * 本函数对字符串、§13 合同对象、纯 {message}/{detail} 结构、以及 Error 都能给出人话;
 * 取不到时回退调用方给的兜底文案,**绝不返回对象**。
 */
export function apiErrorText(source: unknown, fallback: string): string {
    const pick = (value: unknown): string | null => {
        if (typeof value === 'string' && value.trim()) return value;
        if (value && typeof value === 'object') {
            const o = value as Record<string, unknown>;
            for (const key of ['message', 'detail', 'error', 'msg']) {
                const nested = o[key];
                if (typeof nested === 'string' && nested.trim()) return nested;
            }
        }
        return null;
    };
    const err = source as { response?: { data?: unknown }; data?: unknown; message?: unknown };
    const candidates: unknown[] = [
        (err?.response?.data as Record<string, unknown> | undefined)?.detail,
        err?.response?.data,
        (err?.data as Record<string, unknown> | undefined)?.detail,
        err?.data,
        source,
        err?.message,
    ];
    for (const candidate of candidates) {
        const text = pick(candidate);
        if (text) return text;
    }
    return fallback;
}


/**
 * [F-2 存量收敛] 把响应体里**对象/数组形态**的 `detail` 归一成可渲染的字符串,
 * 同时把原始结构完整保留到 `detail_contract`。
 *
 * 背景:全仓约 217 处(88 文件)写的是 `toast.error(x?.response?.data?.detail || '失败')`。
 * 后端 §13 告警合同与 Pydantic 422 会让 `detail` 变成对象/数组 —— 直接丢给 toast
 * 渲染不出来(用户看到空白 = "点了没反应")。
 *
 * `AuthContext.tsx` 里本来就有一份同样的归一,但它挂在 **axios 全局默认实例** 上,
 * 而全站实际用的 `authApi`(AuthContext 内 `axios.create()`)与本文件的 `api` 都是
 * **独立实例,不继承全局拦截器** —— 那层保护一直是空转的。现在把归一直接装到这两个
 * 实例上,一处修掉整类,不必改动 217 个调用点。
 *
 * 保留 `detail_contract` 是为了不打断少数按对象读 `detail.code` 的分支
 * (它们改读 `detail_contract`,行为不变)。
 */
export function normalizeResponseDetail(data: unknown): void {
    if (!data || typeof data !== 'object') return;
    const bag = data as Record<string, unknown>;
    const detail = bag.detail;
    if (detail === null || detail === undefined || typeof detail === 'string') return;

    if (bag.detail_contract === undefined) bag.detail_contract = detail;

    if (Array.isArray(detail)) {
        // Pydantic 422:[{loc, msg, type}, ...]
        bag.detail = detail
            .map((item) => {
                if (item && typeof item === 'object') {
                    const o = item as Record<string, unknown>;
                    const text = o.msg ?? o.message;
                    if (typeof text === 'string' && text.trim()) return text;
                }
                return typeof item === 'string' ? item : JSON.stringify(item);
            })
            .join('; ');
        return;
    }

    if (typeof detail === 'object') {
        const o = detail as Record<string, unknown>;
        for (const key of ['message', 'msg', 'detail', 'error']) {
            const text = o[key];
            if (typeof text === 'string' && text.trim()) {
                bag.detail = text;
                return;
            }
        }
        bag.detail = JSON.stringify(detail);
    }
}

/**
 * 读回**结构化**的 detail —— `normalizeResponseDetail` 之后原对象在 `detail_contract`,
 * 没被归一化过(裸 fetch / 未装拦截器的实例)时仍在 `detail`。
 *
 * 🔴 为什么必须有这个函数:归一化把对象 `detail` 换成了字符串,任何写
 * `err.response.data.detail.code` 的分支从此恒读到 `undefined` —— 而且**静默**,
 * 因为字符串还能正常渲染成文案。2026-07-26 的登录协议门禁死循环就是这么来的
 * (428 的六项校验第一项 `detail.code` 直接判失败 → 补签页永远到不了)。
 * 新增按 code 分支的读取点一律走这里,不要再写 `?.detail?.code`。
 */
export function readDetailContract(err: unknown): unknown {
    const bag = (err as { response?: { data?: unknown } } | undefined)?.response?.data
        ?? (err as { data?: unknown } | undefined)?.data;
    if (!bag || typeof bag !== 'object') return undefined;
    const record = bag as Record<string, unknown>;
    return record.detail_contract !== undefined ? record.detail_contract : record.detail;
}

/** 把 detail 归一装到一个 axios 实例上(成功与错误响应都要装)。 */
export function installDetailNormalizer(instance: {
    interceptors: { response: { use: (onOk: (r: any) => any, onErr: (e: any) => any) => void } };
}): void {
    instance.interceptors.response.use(
        (response) => {
            normalizeResponseDetail(response?.data);
            return response;
        },
        (error) => {
            normalizeResponseDetail(error?.response?.data);
            return Promise.reject(error);
        },
    );
}

installDetailNormalizer(api);
