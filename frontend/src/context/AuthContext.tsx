/**
 * RBAC 认证上下文
 * 管理 JWT Token、用户状态、权限检查
 */
import { installDetailNormalizer, readDetailContract } from '@/lib/api';
import React, { createContext, useContext, useState, useEffect, useCallback, ReactNode } from 'react';
import axios from 'axios';
import { installLazySandboxInterceptor } from '@/sandbox/lazySandboxInterceptor';
import type { AgreementRequirement } from '@/lib/agreementSupplement';
import { clearApiDedupeCache, invalidateApiResourcesForMutation } from '@/lib/api';
import {
    SESSION_CANDIDATE_EVENT,
    SESSION_TOKEN_KEY,
    adoptExternalSessionToken,
    clearAuthoritativeSessionToken,
    confirmAuthoritativeSessionToken,
    markSessionConfirmationPending,
    markSessionConfirmationFailed,
    awaitConfirmedSessionToken,
    SESSION_CONFIRM_RETRY_BACKOFF_MS,
    getAuthorizationEpoch,
    getConfirmedSessionAuthority,
    getConfirmedSessionToken,
    isCurrentSessionAuthority,
    isCurrentSessionCandidate,
    readStoredSessionToken,
    stageAuthoritativeSessionToken,
    type SessionAuthority,
    type SessionCandidateEventDetail,
    type SessionCandidateSource,
} from '@/lib/authoritativeSession';
import { bindBootstrapAgreementProbe, clearBootstrapAgreementProbe } from '@/lib/bootstrapAuthProbes';
import { getActiveDemoSelection, notifyDemoActionPreview, shouldAttachDemoSelection } from '@/lib/demoMode';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

// 开发环境走 Vite proxy（同域），避免 CORS 问题
const API_BASE_URL = '';

// ========== 类型定义 ==========
export interface UserRole {
    id: number;
    name: string;
    display_name: string;
}

export interface AuthUser {
    id: number;
    username: string;
    display_name: string;
    is_admin: boolean;
    is_active: number;
    must_change_password: number;
    roles: UserRole[];
    permissions: string[];
    client_brand_ids: number[];
    permission_version?: number;
    /** [单1] 该员工代作业的商业主体是不是服务商。**不是**他自己的服务商身份 —— 员工的
     *  agent_level 永远是他自己的真值(通常 0)。只用于放行"代服务商作业"的非资金面。 */
    operating_for_agent?: boolean;
    /** [单1] 组织操作员上下文;非操作员为 null。只含该员工自己已知的事实。 */
    operator_context?: {
        organization_id: number;
        principal_user_id: number;
        role_id: number | null;
        membership_status: string;
        organization_status: string;
        capabilities: string[];
        operating_for_agent: boolean;
        derivation_version: string;
    } | null;
    /** 服务商身份 SSOT，唯一来源为 /api/auth/me；资金接口不得覆盖该字段。 */
    agent_level?: number;
    avatar_url?: string;
    team_context?: {
        team_id: number;
        team_role: 'leader' | 'member';
        brand_id: number;
    } | null;
    last_login?: string;
}

interface AuthContextType {
    user: AuthUser | null;
    token: string | null;
    isLoading: boolean;
    isAuthenticated: boolean;
    /** Opaque, in-memory-only authority generation. Never contains or persists a token. */
    authorizationScope: string;
    /**
     * /api/auth/me 软失败标记(5xx / 网络抖动 / 超时 · 非真无效态)
     * - false: 正常态 / 真无效态(token 已清)
     * - true:  token 还在 · user 仍 null · 表示"认证服务暂时不可用 · 不要踢登录"
     * 消费方:ProtectedRoute 看到 token+!user+authSoftFailed=true 时显示"重试 fallback" 而不是 Navigate /login
     */
    authSoftFailed: boolean;
    /** 主动重试 /api/auth/me · ProtectedRoute fallback UI 的"重试"按钮调它 */
    retryAuth: () => Promise<void>;
    login: (username: string, password: string) => Promise<LoginResult>;
    loginSms: (phone: string, smsCode: string) => Promise<LoginResult>;
    logout: () => void;
    refreshToken: () => Promise<boolean>;
    hasPermission: (permission: string) => boolean;
    hasModule: (module: string) => boolean;
    updateToken: (newToken: string) => Promise<void>;
    isTeamLeader: () => boolean;
    getTeamId: () => number | null;
}

interface LoginResult {
    success: boolean;
    error?: string;
    mustChangePassword?: boolean;
    requiresAgreement?: boolean;
    agreementRequirement?: AgreementRequirement;
}

const AuthContext = createContext<AuthContextType | null>(null);

function nextAuthorizationScope(): string {
    try {
        return `auth-${safeRandomUUID()}`;
    } catch {
        return `auth-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
    }
}

// ========== Token 存储 ==========
const SOCIAL_STUDIO_PATHS = new Set([
    'login',
    'approval',
    'match',
    'chat',
    'history',
    'persona-setup',
    'my-ip',
    'video-script',
    'stats',
    'experts',
    'managed',
    'workshop',
    'content-workshop',
    'ideas',
    'topics',
    'rewrite',
    'imitate',
    'planning',
    'plan',
    'profile',
    'creator-profile',
    'my-profile',
    'clients',
    'my-clients',
    'advisors',
    'advisor-market',
    'review',
    'data-review',
    'interaction',
    'interaction-management',
    'interview',
    'creator-test',
    'corpus',
    'team',
    'scripts',
    'research',
    'trending',
    'operation',
    'home',
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

function saveToken(token: string, source: Exclude<SessionCandidateSource, 'logout' | 'storage' | 'storage-mismatch'> = 'login') {
    stageAuthoritativeSessionToken(token, source);
    // 新登录/注册时清除旧的引导状态，避免新用户继承上一个账号的进度
    localStorage.removeItem('onboarding_state');
    localStorage.removeItem('social_guide_dismissed');
}

function loadToken(): string | null {
    return readStoredSessionToken();
}

function clearToken(expectedToken?: string | null, expectedEpoch?: string): boolean {
    const cleared = clearAuthoritativeSessionToken(expectedToken, expectedEpoch);
    if (cleared) clearBootstrapAgreementProbe();
    return cleared;
}

function clearAccountOwnedBrowserState(): void {
    clearBootstrapAgreementProbe();
    try {
        const exactKeys = [
            'social_main_advisor',
            'social_guide_dismissed',
            'currentProjectId',
            'omnirank_current_brand_id',
            'onboarding_state',
            'portal_brand_id',
            'portal_brand_name',
            'omnirank_user_mode',
        ];
        exactKeys.forEach(key => localStorage.removeItem(key));
        // Interview progress is account-owned. Remove the legacy key and every
        // authority-scoped successor so logout/login or cross-tab replacement can
        // never restore another session's messages.
        for (let index = localStorage.length - 1; index >= 0; index -= 1) {
            const key = localStorage.key(index);
            if (key === 'interview_cache' || key?.startsWith('interview_cache:')) {
                localStorage.removeItem(key);
            }
        }
        sessionStorage.clear();
        import('@/hooks/useAgentSessions').then(m => m.clearLegacyAgentStorage()).catch(() => {});
        window.dispatchEvent(new CustomEvent('omnirank-user-changed'));
    } catch {
        // Storage can be disabled by browser policy. Render/request state is still
        // cleared synchronously by the caller.
    }
}

function isHardAuthError(err: any): boolean {
    const status = err?.response?.status;
    const code = err?.response?.data?.code;
    if (status === 404) return true;
    if (status === 401) {
        return code === 'INVALID_TOKEN'
            || code === 'USER_NOT_FOUND'
            || code === 'ACCOUNT_DISABLED';
    }
    return false;
}

function authErrorMessage(err: any, fallback: string): string {
    const detail = err?.response?.data?.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail === 'object') {
        if (typeof detail.message === 'string') return detail.message;
        if (typeof detail.msg === 'string') return detail.msg;
    }
    return typeof err?.message === 'string' && err.message ? err.message : fallback;
}

/**
 * 后端 428 = 缺当前版本协议签署。**只看 HTTP 状态**,不依赖 body 能否解析。
 *
 * 这条独立于下面的 payload 解析,是本次死循环事故的 fail-safe 底座:哪怕 body 形态
 * 再漂一次,登录页也必须知道"这是协议门禁"从而给出补签入口,而不是降级成一句红字。
 */
function isAgreementGateError(err: any): boolean {
    return err?.response?.status === 428;
}

function agreementRequirementFromError(err: any): AgreementRequirement | null {
    if (!isAgreementGateError(err)) return null;
    // 🔴 必须读 detail_contract:`installDetailNormalizer`(下方 391 行装在 authApi 上)
    //    会把对象 detail 归一成字符串并把原对象挪到 detail_contract。直接读 detail
    //    会让第一项 `detail.code` 恒判失败 → 返回 null → 用户被永久锁在登录页。
    const detail = readDetailContract(err) as any;
    if (
        !detail
        || typeof detail !== 'object'
        || detail.code !== 'REGISTRATION_AGREEMENTS_REQUIRED'
        || detail.requires_agreement !== true
        || typeof detail.agreement_session_token !== 'string'
        || !Number.isFinite(detail.expires_at)
        || !Array.isArray(detail.agreements)
    ) {
        return null;
    }
    return {
        agreement_session_token: detail.agreement_session_token,
        expires_at: detail.expires_at,
        agreements: detail.agreements,
    };
}

// ========== 创建 axios 实例（带 JWT） ==========
const authApi = axios.create({
    baseURL: API_BASE_URL,
    timeout: 30000,
});

// 沙盒拦截器 (Stage 1 Batch 3, 2026-05-08) · 必须在 auth 请求/响应拦截器之前装
installLazySandboxInterceptor(authApi);

type BootstrapMeProbe = {
    token: string;
    promise: Promise<any>;
};

function takeBootstrapMeProbe(requestToken: string): Promise<any> | null {
    if (typeof window === 'undefined') return null;
    const bootstrapWindow = window as Window & { __OMNIRANK_BOOTSTRAP_ME__?: BootstrapMeProbe };
    const probe = bootstrapWindow.__OMNIRANK_BOOTSTRAP_ME__;
    if (!probe) return null;
    delete bootstrapWindow.__OMNIRANK_BOOTSTRAP_ME__;
    return probe.token === requestToken ? probe.promise : null;
}

// React StrictMode 会在开发态重复挂载 Provider。认证自检和裸 401 刷新必须跨挂载合并，
// 否则一次页面进入会产生两组 /me + /refresh，既放大限流也违背“只刷新一次”。
let meRequestInFlight: { token: string; epoch: string; request: Promise<any> } | null = null;
let bootstrapRefreshInFlight: { token: string; request: Promise<boolean> } | null = null;

function fetchMeShared(requestToken: string): Promise<any> {
    const epoch = getAuthorizationEpoch();
    if (meRequestInFlight?.token === requestToken && meRequestInFlight.epoch === epoch) {
        return meRequestInFlight.request;
    }
    const request = takeBootstrapMeProbe(requestToken) || authApi.get('/api/auth/me', {
        headers: { Authorization: `Bearer ${requestToken}` },
    });
    meRequestInFlight = { token: requestToken, epoch, request };
    const clear = () => {
        if (meRequestInFlight?.request === request) meRequestInFlight = null;
    };
    request.then(clear, clear);
    return request;
}

async function refreshBootstrapSessionOnce(expectedToken: string): Promise<boolean> {
    // 另一个 StrictMode 挂载已经刷新成功时直接复用新 token，不再发第二个请求。
    const currentToken = loadToken();
    if (currentToken !== expectedToken) return currentToken !== null;
    if (bootstrapRefreshInFlight?.token !== expectedToken) {
        const refreshPromise = (async () => {
            const refresh = await authApi.post('/api/auth/refresh', undefined, {
                headers: { Authorization: `Bearer ${expectedToken}` },
            });
            if (!refresh.data?.success || !refresh.data?.token) return false;
            // 请求期间若已经切换账号，旧刷新响应不得覆盖新账号 token。
            if (loadToken() !== expectedToken) return true;
            saveToken(refresh.data.token, 'refresh');
            return true;
        })();
        bootstrapRefreshInFlight = { token: expectedToken, request: refreshPromise };
        refreshPromise.then(
            () => { if (bootstrapRefreshInFlight?.request === refreshPromise) bootstrapRefreshInFlight = null; },
            () => { if (bootstrapRefreshInFlight?.request === refreshPromise) bootstrapRefreshInFlight = null; },
        );
    }
    return await bootstrapRefreshInFlight.request;
}

// 请求拦截器：自动加 Authorization
authApi.interceptors.request.use(async config => {
    const path = String(config.url || '');
    const candidateAuthRequest = path === '/api/auth/me' || path === '/api/auth/refresh';
    const publicAuthRequest = path === '/api/auth/login'
        || path === '/api/auth/login-sms'
        || path === '/api/auth/register';
    // [BUG-3] 会话在途时【等】而不是抛错炸页面;401 走匿名;超时抛可恢复错由调用方处理。
    const token = candidateAuthRequest
        ? loadToken()
        : publicAuthRequest
            ? null
            : await awaitConfirmedSessionToken();
    if (!candidateAuthRequest || !config.headers.Authorization) {
        if (token) config.headers.Authorization = `Bearer ${token}`;
        else delete config.headers.Authorization;
    }
    const demoSelection = getActiveDemoSelection();
    const explicitDemoBrand = config.headers.get?.('X-Demo-Brand-ID')
        ?? config.headers['X-Demo-Brand-ID'];
    const explicitDemoCase = config.headers.get?.('X-Demo-Case-ID')
        ?? config.headers['X-Demo-Case-ID'];
    const hasExplicitDemoAuthority = explicitDemoBrand !== null
        && explicitDemoBrand !== undefined
        && String(explicitDemoBrand).trim() !== ''
        && explicitDemoCase !== null
        && explicitDemoCase !== undefined
        && String(explicitDemoCase).trim() !== '';
    if (!hasExplicitDemoAuthority && demoSelection && shouldAttachDemoSelection(String(config.url || ''))) {
        config.headers['X-Demo-Brand-ID'] = String(demoSelection.brandId);
        config.headers['X-Demo-Case-ID'] = demoSelection.caseId;
    } else if (!hasExplicitDemoAuthority) {
        delete config.headers['X-Demo-Brand-ID'];
        delete config.headers['X-Demo-Case-ID'];
    }
    return config;
});

// [F-2 存量收敛] authApi 是 axios.create() 出来的独立实例,不继承本文件下方那条
// 挂在 axios 全局默认实例上的 detail 归一 —— 那层保护对全站请求一直是空转的。
// 这里显式装上:对象/数组形态的 detail 统一转成可渲染文案,原始结构保留在
// detail_contract。一处生效,全仓约 217 处 `toast.error(...detail || '失败')` 同时得救。
installDetailNormalizer(authApi);

authApi.interceptors.response.use(
    response => {
        const method = String(response.config?.method || 'get').toLowerCase();
        if (method === 'post' || method === 'put' || method === 'patch' || method === 'delete') {
            invalidateApiResourcesForMutation(String(response.config?.url || ''));
        }
        return response;
    },
    error => {
        notifyDemoActionPreview(error.response?.data);
        return Promise.reject(error);
    },
);

// ========== Provider ==========
export function AuthProvider({ children }: { children: ReactNode }) {
    const [user, setUser] = useState<AuthUser | null>(null);
    const [token, setToken] = useState<string | null>(loadToken);
    const [authorizationScope, setAuthorizationScope] = useState(nextAuthorizationScope);
    const [isLoading, setIsLoading] = useState(true);
    const [authSoftFailed, setAuthSoftFailed] = useState(false);
    const rotateAuthorizationScope = useCallback(() => {
        clearApiDedupeCache();
        window.dispatchEvent(new Event('omnirank-authorization-changed'));
        setAuthorizationScope(nextAuthorizationScope());
    }, []);

    // CTO-15.13 security: /api/auth/me 5xx / 网络错误 / 超时不再清 token 跳登录
    //   旧版任何 catch 都 clearToken → 后端偶发 500/抖动 + 网络抖动把活跃用户踢回登录页
    //   新版策略:
    //     1. 仅"真无效态"清 token: 401 + INVALID_TOKEN/USER_NOT_FOUND/ACCOUNT_DISABLED · 或 404 用户不存在
    //     2. 5xx / 网络错误 / 超时 / 无 code 的 401 → 一次 2s 重试
    //        - 仍失败 → 保留 token · user 留 null · setAuthSoftFailed(true)
    //        - ProtectedRoute 据此渲染"认证服务暂时不可用 · 重试"fallback · 不 Navigate /login
    //        - 不动 isAuthenticated 语义(仍 = !!user && !!token)防止 hasModule/requiredModule 因 user=null 乱跳首页
    // 抽出供 useEffect + retryAuth 复用
    const fetchMeWithRetry = useCallback(async (): Promise<void> => {
        const savedToken = loadToken();
        if (!savedToken) {
            setIsLoading(false);
            setAuthSoftFailed(false);
            return;
        }
        // [BUG-3] /me 起飞 → 标 pending,让 awaitConfirmedSessionToken 的等待方继续等,
        // 而不是拿上一轮的失败态提前放弃(retryAuth 重试时尤其重要)。
        markSessionConfirmationPending();
        let requestToken = savedToken;
        let requestEpoch = getAuthorizationEpoch();
        const isCurrentRequest = () => loadToken() === requestToken && getAuthorizationEpoch() === requestEpoch;
        const tryFetchMe = async () => fetchMeShared(requestToken);
        const refreshSessionOnce = async (): Promise<boolean> => {
            const refreshed = await refreshBootstrapSessionOnce(savedToken);
            if (refreshed) {
                requestToken = loadToken() || savedToken;
                requestEpoch = getAuthorizationEpoch();
                setToken(requestToken);
            }
            return refreshed;
        };
        let res;
        try {
            res = await tryFetchMe();
        } catch (err1: any) {
            if (!isCurrentRequest()) {
                if (!loadToken()) setIsLoading(false);
                return;
            }
            if (isHardAuthError(err1)) {
                markSessionConfirmationFailed('unauthorized');
                rotateAuthorizationScope();
                clearToken(requestToken, requestEpoch);
                setToken(null);
                setUser(null);
                setAuthSoftFailed(false);
                setIsLoading(false);
                return;
            }
            const firstStatus = err1?.response?.status;
            const firstCode = err1?.response?.data?.code;
            if (firstStatus === 401 && !firstCode) {
                // 网关/旧服务可能只给裸 401：先受控刷新一次，再二次确认。
                try {
                    const refreshed = await refreshSessionOnce();
                    if (!refreshed) throw err1;
                    res = await tryFetchMe();
                } catch (confirmError: any) {
                    if (!isCurrentRequest()) {
                        if (!loadToken()) setIsLoading(false);
                        return;
                    }
                    if (confirmError?.response?.status === 401) {
                        markSessionConfirmationFailed('unauthorized');
                        rotateAuthorizationScope();
                        clearToken(requestToken, requestEpoch);
                        setToken(null);
                        setUser(null);
                        setAuthSoftFailed(false);
                    } else {
                        // 网络/5xx → 可恢复:保 token,给 ProtectedRoute 的"重试"出口
                        markSessionConfirmationFailed('unreachable');
                        setToken(requestToken);
                        setAuthSoftFailed(true);
                    }
                    setIsLoading(false);
                    return;
                }
            } else {
            // 5xx / 网络错误 → 退避后重试一次(次数/间隔常量见 authoritativeSession.ts,取值理由同处)
            await new Promise(resolve => setTimeout(resolve, SESSION_CONFIRM_RETRY_BACKOFF_MS));
            if (!isCurrentRequest()) {
                if (!loadToken()) setIsLoading(false);
                return;
            }
            try {
                res = await tryFetchMe();
            } catch (err2: any) {
                if (!isCurrentRequest()) {
                    if (!loadToken()) setIsLoading(false);
                    return;
                }
                if (isHardAuthError(err2)) {
                    markSessionConfirmationFailed('unauthorized');
                    rotateAuthorizationScope();
                    clearToken(requestToken, requestEpoch);
                    setToken(null);
                    setUser(null);
                    setAuthSoftFailed(false);
                } else {
                    // 重试已耗尽且不是真无效态 → 标 unreachable(可恢复),等待方拿到可重试错误而非白屏
                    markSessionConfirmationFailed('unreachable');
                    // 仍 5xx / 网络问题 → 保 token · user 留 null · 标 soft-failed
                    const status = err2?.response?.status;
                    const code = err2?.response?.data?.code;
                    console.warn(
                        `[Auth] /api/auth/me 重试 2 次仍失败但保留 token (status=${status} code=${code || '(none)'})`
                    );
                    setToken(requestToken);
                    setAuthSoftFailed(true);
                }
                setIsLoading(false);
                return;
            }
            }
        }

        if (!isCurrentRequest()) {
            if (!loadToken()) setIsLoading(false);
            return;
        }
        if (res.data.success) {
            const confirmedEpoch = confirmAuthoritativeSessionToken(requestToken);
            if (confirmedEpoch === null) {
                if (!loadToken()) setIsLoading(false);
                return;
            }
            rotateAuthorizationScope();
            bindBootstrapAgreementProbe({ token: requestToken, epoch: confirmedEpoch });
            setUser(res.data.user);
            setToken(requestToken);
            setAuthSoftFailed(false);
        } else {
            // 业务层 success=false (罕见) → 视作用户态异常 · 清
            // clearToken 内部会 settle('anonymous'),等待方不会吊死
            markSessionConfirmationFailed('unauthorized');
            rotateAuthorizationScope();
            clearToken();
            setToken(null);
            setUser(null);
            setAuthSoftFailed(false);
        }
        setIsLoading(false);
    }, [rotateAuthorizationScope]);

    // 初始化：进 app 跑一次
    useEffect(() => {
        fetchMeWithRetry();
    }, [fetchMeWithRetry]);

    const reconcileExternalSession = useCallback((nextToken: string | null, alreadyAdopted = false) => {
        clearAccountOwnedBrowserState();
        if (!alreadyAdopted) adoptExternalSessionToken(nextToken);
        // Clear sensitive render state synchronously with the storage notification.
        // The replacement token remains a candidate until its own /me succeeds.
        rotateAuthorizationScope();
        setUser(null);
        setToken(nextToken);
        setAuthSoftFailed(false);
        setIsLoading(nextToken !== null);
        if (nextToken) void fetchMeWithRetry();
    }, [fetchMeWithRetry, rotateAuthorizationScope]);

    // localStorage events are delivered to the *other* tabs. A same-user token
    // rotation is still a new authority generation and must clear the old DOM/cache.
    useEffect(() => {
        const onStorage = (event: StorageEvent) => {
            if (event.storageArea !== localStorage || event.key !== SESSION_TOKEN_KEY) return;
            reconcileExternalSession(event.newValue);
        };
        const onCandidateMismatch = (event: Event) => {
            const detail = (event as CustomEvent<SessionCandidateEventDetail>).detail;
            if (detail?.source === 'logout') {
                if (readStoredSessionToken() === null) reconcileExternalSession(null, true);
                return;
            }
            if (detail?.source === 'storage-mismatch') {
                reconcileExternalSession(readStoredSessionToken(), true);
            }
        };
        window.addEventListener('storage', onStorage);
        window.addEventListener(SESSION_CANDIDATE_EVENT, onCandidateMismatch);
        return () => {
            window.removeEventListener('storage', onStorage);
            window.removeEventListener(SESSION_CANDIDATE_EVENT, onCandidateMismatch);
        };
    }, [reconcileExternalSession]);

    // 暴露给 ProtectedRoute fallback 的"重试"按钮 · 重置 isLoading 让 UI 出 loading 态
    const retryAuth = useCallback(async () => {
        setIsLoading(true);
        setAuthSoftFailed(false);
        await fetchMeWithRetry();
    }, [fetchMeWithRetry]);

    // 登录/短信登录/权限刷新只接受 /api/auth/me 的权威用户对象。
    // 登录接口 DTO 仅用于签发 token，不得决定 agent_level 或侧栏身份。
    const establishAuthoritativeSession = useCallback(async (
        sessionToken: string,
        source: Exclude<SessionCandidateSource, 'logout' | 'storage' | 'storage-mismatch'> = 'login',
        stagedEpoch?: string,
    ): Promise<AuthUser> => {
        clearBootstrapAgreementProbe();
        rotateAuthorizationScope();
        setIsLoading(true);
        if (source === 'login' || source === 'password-change') {
            clearAccountOwnedBrowserState();
        }
        if (stagedEpoch) {
            if (!isCurrentSessionCandidate({ token: sessionToken, epoch: stagedEpoch })) {
                throw new Error('登录会话已被更新，请重新确认当前账号');
            }
        } else {
            saveToken(sessionToken, source);
        }
        setToken(sessionToken);
        setUser(null);
        setAuthSoftFailed(false);
        const sessionEpoch = stagedEpoch || getAuthorizationEpoch();
        try {
            const me = await fetchMeShared(sessionToken);
            if (loadToken() !== sessionToken || getAuthorizationEpoch() !== sessionEpoch) {
                throw new Error('登录会话已被更新，请重新确认当前账号');
            }
            if (!me.data?.success || !me.data?.user) {
                throw new Error('登录成功，但权威身份信息暂时无法确认');
            }
            if (confirmAuthoritativeSessionToken(sessionToken) === null) {
                throw new Error('登录会话已被更新，请重新确认当前账号');
            }
            const authoritativeUser = me.data.user as AuthUser;
            setUser(authoritativeUser);
            setAuthSoftFailed(false);
            setIsLoading(false);
            return authoritativeUser;
        } catch (err: any) {
            if (loadToken() === sessionToken && getAuthorizationEpoch() === sessionEpoch) {
                setUser(null);
                if (isHardAuthError(err)) {
                    clearToken(sessionToken, sessionEpoch);
                    setToken(null);
                    setAuthSoftFailed(false);
                } else {
                    setToken(sessionToken);
                    setAuthSoftFailed(true);
                }
                setIsLoading(false);
            }
            throw err;
        }
    }, [rotateAuthorizationScope]);

    // 登录
    const login = useCallback(async (username: string, password: string) => {
        try {
            const res = await authApi.post('/api/auth/login', { username, password });
            if (res.data.success) {
                const authoritativeUser = await establishAuthoritativeSession(res.data.token);
                const mustChange = authoritativeUser.must_change_password === 1;
                return { success: true, mustChangePassword: mustChange };
            }
            return { success: false, error: res.data.error || '登录失败' };
        } catch (err: any) {
            const requirement = agreementRequirementFromError(err);
            if (requirement) {
                return {
                    success: false,
                    requiresAgreement: true,
                    agreementRequirement: requirement,
                };
            }
            // 🔴 fail-safe(本单核心):428 但 payload 解析不出时,**仍然**告诉登录页
            //    "这是协议门禁",由它给出可点击的补签入口。绝不允许退化成
            //    "拦住 + 唯一出口坏了 = 用户永久锁死"。
            if (isAgreementGateError(err)) {
                return {
                    success: false,
                    requiresAgreement: true,
                    error: authErrorMessage(err, '为继续使用，请确认当前《用户服务协议》和《隐私政策》'),
                };
            }
            return { success: false, error: authErrorMessage(err, '登录或身份确认失败') };
        }
    }, [establishAuthoritativeSession]);

    const loginSms = useCallback(async (phone: string, smsCode: string) => {
        try {
            const res = await authApi.post('/api/auth/login-sms', {
                phone,
                sms_code: smsCode,
            });
            if (res.data.success && res.data.token) {
                const authoritativeUser = await establishAuthoritativeSession(res.data.token);
                return {
                    success: true,
                    mustChangePassword: authoritativeUser.must_change_password === 1,
                };
            }
            return { success: false, error: res.data.error || res.data.message || '登录失败' };
        } catch (err: any) {
            const requirement = agreementRequirementFromError(err);
            if (requirement) {
                return {
                    success: false,
                    requiresAgreement: true,
                    agreementRequirement: requirement,
                };
            }
            // 🔴 fail-safe(本单核心):428 但 payload 解析不出时,**仍然**告诉登录页
            //    "这是协议门禁",由它给出可点击的补签入口。绝不允许退化成
            //    "拦住 + 唯一出口坏了 = 用户永久锁死"。
            if (isAgreementGateError(err)) {
                return {
                    success: false,
                    requiresAgreement: true,
                    error: authErrorMessage(err, '为继续使用，请确认当前《用户服务协议》和《隐私政策》'),
                };
            }
            return { success: false, error: authErrorMessage(err, '登录或身份确认失败') };
        }
    }, [establishAuthoritativeSession]);

    // 登出 —— 彻底清理所有跨用户残留数据，防止切账号后上一个账号的状态泄漏
    const logout = useCallback((expectedToken?: string, expectedEpoch?: string) => {
        if (!clearToken(expectedToken, expectedEpoch)) return;
        rotateAuthorizationScope();
        setToken(null);
        setUser(null);
        setIsLoading(false);
        setAuthSoftFailed(false); // 状态卫生:登出后清残留 soft-fail 标
        clearAccountOwnedBrowserState();
    }, [rotateAuthorizationScope]);

    // 刷新令牌（权限变更时）
    const refreshToken = useCallback(async (
        expectedToken?: string,
        expectedEpoch?: string,
    ) => {
        const confirmed = getConfirmedSessionAuthority();
        const refreshOwner: SessionAuthority | null = expectedToken
            ? {
                token: expectedToken,
                epoch: expectedEpoch
                    || (confirmed?.token === expectedToken ? confirmed.epoch : ''),
            }
            : confirmed;
        if (!refreshOwner?.epoch || !isCurrentSessionAuthority(refreshOwner)) return false;
        try {
            const res = await authApi.post('/api/auth/refresh', undefined, {
                headers: { Authorization: `Bearer ${refreshOwner.token}` },
            });
            if (!isCurrentSessionAuthority(refreshOwner)) return false;
            if (res.data.success && res.data.token) {
                await establishAuthoritativeSession(res.data.token, 'refresh');
                return true;
            }
            return false;
        } catch (error: any) {
            // A stale request may finish after the same textual JWT was logged out
            // and reissued. Epoch CAS prevents that old owner from touching E2.
            if (!isCurrentSessionAuthority(refreshOwner)) return false;
            const status = error?.response?.status;
            if (status === 401 || status === 404) {
                logout(refreshOwner.token, refreshOwner.epoch);
            } else {
                setAuthSoftFailed(true);
            }
            return false;
        }
    }, [establishAuthoritativeSession, logout]);

    // #1 修复：注册全局 axios 拦截器（处理 401/PERMISSION_CHANGED）
    // 必须在 cleanup 中 eject 旧拦截器，防止重渲染时拦截器无限累积
    useEffect(() => {
        const ids = setupAxiosInterceptors(logout, refreshToken);
        return () => {
            axios.interceptors.request.eject(ids.request);
            axios.interceptors.response.eject(ids.response);
        };
    }, [logout, refreshToken]);

    // 监听全局 token 刷新（403 自动刷新后同步状态）
    useEffect(() => {
        const handler = (e: Event) => {
            const ce = e as CustomEvent<{
                token?: string;
                epoch?: string;
                authoritativeReady?: Promise<void>;
            }>;
            if (ce.detail?.token) {
                // dispatchEvent 是同步的：刷新方在返回前必须拿到并等待这个 Promise。
                // 缺监听器、/me 失败或会话在确认期间被替换时，业务请求均不得重放。
                const authoritativeReady = establishAuthoritativeSession(
                    ce.detail.token,
                    'refresh',
                    ce.detail.epoch,
                ).then(() => undefined);
                ce.detail.authoritativeReady = authoritativeReady;
                void authoritativeReady.catch((err) => {
                    console.warn('[Auth] token 刷新后权威身份读取失败', err);
                });
            }
        };
        window.addEventListener('token-refreshed', handler);
        return () => window.removeEventListener('token-refreshed', handler);
    }, [establishAuthoritativeSession]);

    // 更新 Token（修改密码后）
    const updateToken = useCallback(async (newToken: string): Promise<void> => {
        await establishAuthoritativeSession(newToken, 'password-change');
    }, [establishAuthoritativeSession]);

    // 权限检查：精确匹配 "module:level"
    const hasPermission = useCallback((permission: string) => {
        if (!user) return false;
        if (user.is_admin) return true;
        return user.permissions?.includes(permission) ?? false;
    }, [user]);

    // 团队角色检查
    const isTeamLeader = useCallback(() => {
        return user?.team_context?.team_role === 'leader';
    }, [user]);

    const getTeamId = useCallback(() => {
        return user?.team_context?.team_id ?? null;
    }, [user]);

    // 模块检查：是否有某模块的任意级别权限
    const hasModule = useCallback((module: string) => {
        if (!user) return false;
        if (user.is_admin) return true;
        return user.permissions?.some(p => p.startsWith(`${module}:`)) ?? false;
    }, [user]);

    return (
        <AuthContext.Provider value={{
            user,
            token,
            isLoading,
            isAuthenticated: !!user && !!token,
            authorizationScope,
            authSoftFailed,
            retryAuth,
            login,
            loginSms,
            logout,
            refreshToken,
            hasPermission,
            hasModule,
            updateToken,
            isTeamLeader,
            getTeamId,
        }}>
            {children}
        </AuthContext.Provider>
    );
}

// ========== Hook ==========
export function useAuth() {
    const ctx = useContext(AuthContext);
    if (!ctx) throw new Error('useAuth 必须在 AuthProvider 内使用');
    return ctx;
}

// ========== 全局 axios 拦截器（处理 401/403） ==========
function isSafeAuthReplayMethod(method: string | undefined): boolean {
    const normalized = (method || 'GET').toUpperCase();
    return normalized === 'GET' || normalized === 'HEAD' || normalized === 'OPTIONS';
}

type GlobalAxiosRequestAuthority = { token: string | null; epoch: string };
type GlobalAxiosReplayConfig = {
    __authRefreshAttempted?: boolean;
    __omnirankManagedSignal?: boolean;
    __omnirankCallerSignal?: AbortSignal;
    __omnirankRequestAuthority?: GlobalAxiosRequestAuthority;
    __omnirankStartedAt?: number;
    __omnirankDeadlineAt?: number;
};

function isCurrentGlobalRequestAuthority(config: GlobalAxiosReplayConfig | undefined): config is GlobalAxiosReplayConfig & {
    __omnirankRequestAuthority: GlobalAxiosRequestAuthority & { token: string };
} {
    const authority = config?.__omnirankRequestAuthority;
    return !!authority
        && authority.token !== null
        && isCurrentSessionAuthority({ token: authority.token, epoch: authority.epoch });
}

async function replayGlobalAxiosRequest(config: any, authority: SessionAuthority): Promise<unknown> {
    const callerSignal: AbortSignal | undefined = config.__omnirankManagedSignal
        ? config.__omnirankCallerSignal
        : (config.__omnirankCallerSignal || config.signal);
    if (callerSignal?.aborted) throw new axios.CanceledError('request canceled before authority replay');
    if (!isCurrentSessionAuthority(authority)) {
        throw new axios.CanceledError('authority changed before request replay');
    }
    const startedAt = config.__omnirankStartedAt || Date.now();
    const deadlineAt = config.__omnirankDeadlineAt
        || startedAt + (config.timeout && config.timeout > 0 ? config.timeout : 30_000);
    const remainingMs = deadlineAt - Date.now();
    if (remainingMs <= 0) throw new axios.CanceledError('request deadline elapsed before authority replay');

    const controller = new AbortController();
    const abortFromCaller = () => controller.abort(callerSignal?.reason);
    const abortFromAuthority = () => {
        if (!isCurrentSessionAuthority(authority)) {
            controller.abort(new DOMException('Authorization changed during request replay', 'AbortError'));
        }
    };
    callerSignal?.addEventListener('abort', abortFromCaller, { once: true });
    window.addEventListener('omnirank-authorization-changed', abortFromAuthority);
    const deadline = window.setTimeout(
        () => controller.abort(new DOMException('Authority replay deadline elapsed', 'TimeoutError')),
        remainingMs,
    );
    config.headers = config.headers || {};
    config.headers.Authorization = `Bearer ${authority.token}`;
    config.signal = controller.signal;
    config.timeout = Math.max(1, remainingMs);
    config.__omnirankManagedSignal = true;
    config.__omnirankCallerSignal = callerSignal;
    config.__omnirankRequestAuthority = authority;
    config.__omnirankStartedAt = startedAt;
    config.__omnirankDeadlineAt = deadlineAt;
    try {
        const response = await axios.request(config);
        if (!isCurrentSessionAuthority(authority)) {
            throw new axios.CanceledError('authority changed before replay response was consumed');
        }
        return response;
    } finally {
        window.clearTimeout(deadline);
        callerSignal?.removeEventListener('abort', abortFromCaller);
        window.removeEventListener('omnirank-authorization-changed', abortFromAuthority);
    }
}

export function setupAxiosInterceptors(
    logout: (expectedToken?: string, expectedEpoch?: string) => void,
    refreshToken: (expectedToken?: string, expectedEpoch?: string) => Promise<boolean>,
) {
    // 给 lib/api.ts 的 axios 实例也加上 token
    const requestId = axios.interceptors.request.use(async config => {
        // [BUG-3] 同上:在途等待,不把"慢"放大成整页白屏
        const token = await awaitConfirmedSessionToken();
        if (token) {
            config.headers.Authorization = `Bearer ${token}`;
        } else {
            delete config.headers.Authorization;
        }
        const replayConfig = config as typeof config & GlobalAxiosReplayConfig;
        if (!replayConfig.__omnirankRequestAuthority) {
            replayConfig.__omnirankRequestAuthority = { token, epoch: getAuthorizationEpoch() };
        }
        if (!replayConfig.__omnirankStartedAt) {
            replayConfig.__omnirankStartedAt = Date.now();
            const timeout = Number(config.timeout);
            replayConfig.__omnirankDeadlineAt = replayConfig.__omnirankStartedAt
                + (Number.isFinite(timeout) && timeout > 0 ? timeout : 30_000);
        }
        return config;
    });

    // 响应拦截器：处理认证失败 + 序列化 error.detail
    const responseId = axios.interceptors.response.use(
        response => response,
        async error => {
            // 防止 Pydantic 422 的 detail 数组被前端直接渲染导致 React error #31
            if (error.response?.data?.detail && typeof error.response.data.detail !== 'string') {
                const d = error.response.data.detail;
                if (Array.isArray(d)) {
                    error.response.data.detail = d.map((e: any) => e.msg || e.message || JSON.stringify(e)).join('; ');
                } else if (typeof d === 'object') {
                    error.response.data.detail = d.message || d.msg || JSON.stringify(d);
                }
            }
            if (error.response?.status === 401) {
                const code = error.response.data?.code;
                const config = error.config as (typeof error.config & GlobalAxiosReplayConfig);
                const canAutoReplay = isSafeAuthReplayMethod(config?.method);
                if (code === 'PERMISSION_CHANGED' && config && !config.__authRefreshAttempted) {
                    config.__authRefreshAttempted = true;
                    if (!isCurrentGlobalRequestAuthority(config)) return Promise.reject(error);
                    // 权限变更，自动刷新后重试原始请求
                    const owner = config.__omnirankRequestAuthority;
                    const refreshed = await refreshToken(owner.token, owner.epoch);
                    const nextAuthority = getConfirmedSessionAuthority();
                    if (refreshed && nextAuthority && config && canAutoReplay) {
                        return replayGlobalAxiosRequest(config, nextAuthority);
                    }
                    return Promise.reject(error);
                }
                if (!code && config && !config.__authRefreshAttempted) {
                    config.__authRefreshAttempted = true;
                    if (!isCurrentGlobalRequestAuthority(config)) return Promise.reject(error);
                    const owner = config.__omnirankRequestAuthority;
                    const refreshed = await refreshToken(owner.token, owner.epoch);
                    const nextAuthority = getConfirmedSessionAuthority();
                    if (refreshed && nextAuthority && canAutoReplay) {
                        return replayGlobalAxiosRequest(config, nextAuthority);
                    }
                    // refresh 429/5xx/断网是软失败，refreshToken 已保留会话；
                    // 非安全方法即使刷新成功也不能自动重放。
                    return Promise.reject(error);
                }
                // Token 无效或过期 — 公开页面不 logout（客户不需要登录）
                const p = typeof window !== 'undefined' ? window.location.pathname : '';
                const isPublicPage = isPublicPagePath(p);
                const confirmedExpired = !code && config?.__authRefreshAttempted && canAutoReplay;
                const hardInvalid = code === 'INVALID_TOKEN' || code === 'USER_NOT_FOUND' || code === 'ACCOUNT_DISABLED';
                if (!isPublicPage && (confirmedExpired || hardInvalid) && isCurrentGlobalRequestAuthority(config)) {
                    logout(config.__omnirankRequestAuthority.token, config.__omnirankRequestAuthority.epoch);
                }
            }
            return Promise.reject(error);
        }
    );

    return { request: requestId, response: responseId };
}

export { authApi, loadToken };
