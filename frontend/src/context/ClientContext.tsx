/**
 * 🆕 全局客户上下文 Provider
 * Step 0-A: 统一客户标识 (brand_id)
 * 
 * 功能：
 * - 从 /api/client-context/list 加载品牌列表
 * - 选中品牌后从 /api/client-context/{brand_id} 加载完整上下文
 * - localStorage 持久化选中的 brand_id
 * - 提供 relatedQuoteIds 用于桥接监测模块
 */

import { createContext, useContext, useState, useEffect, useLayoutEffect, useCallback, useRef, type ReactNode } from 'react';
import { clientContextApi, type ClientBrandSummary, type ClientContextDetail } from '@/lib/api';
import { useAuth } from '@/context/AuthContext';
import {
    SESSION_CANDIDATE_EVENT,
    readStoredSessionToken,
} from '@/lib/authoritativeSession';
import { getActiveDemoSelection, setActiveDemoSelection } from '@/lib/demoMode';

// ========== 类型定义 ==========

export type BrandSummary = ClientBrandSummary;

export type ClientContextData = ClientContextDetail;

interface ClientContextType {
    // 品牌列表（供选择器使用）
    clients: BrandSummary[];
    // 当前选中的品牌ID
    currentBrandId: number | null;
    // 当前选中品牌的完整上下文
    clientContext: ClientContextData | null;
    // 关联的报价ID列表（监测模块桥接用）
    relatedQuoteIds: string[];
    // 加载状态
    loading: boolean;
    listLoading: boolean;
    listReady: boolean;
    error: string | null;
    listError: string | null;
    // "所有客户"模式（超级管理员）
    isAllClientsMode: boolean;
    // 切换客户
    switchClient: (brandId: number | null, allMode?: boolean) => void;
    // 刷新列表
    refreshClients: () => Promise<void>;
}

const LEGACY_SCOPED_SELECTION_PREFIX = 'omnirank_current_brand_id:';
const SELECTION_KEY_PREFIX = 'omnirank_current_brand_candidate:';
const clientContextCache = new Map<string, ClientContextData>();
const clientContextCacheUpdatedAt = new Map<string, number>();
const CLIENT_CONTEXT_FRESH_MS = 10 * 1000;
type ClientContextResponse = {
    data: { success: boolean; context: ClientContextDetail; redirect_to?: number };
};
type ClientAccessIdentity =
    | { mode: 'real' }
    | { mode: 'demo'; caseId: string };
const sharedContextRequests = new Map<string, { controller: AbortController; request: Promise<ClientContextResponse> }>();
let activeClientAuthorizationScope: string | null = null;

function clientContextCacheKey(
    authorizationScope: string,
    brandId: number,
    demoCaseId?: string,
): string {
    return `${authorizationScope}:${brandId}:${demoCaseId || 'real'}`;
}

function sharedClientContextRequest(
    authorizationScope: string,
    brandId: number,
    demoCaseId?: string,
): Promise<ClientContextResponse> {
    const key = clientContextCacheKey(authorizationScope, brandId, demoCaseId);
    const existing = sharedContextRequests.get(key);
    if (existing) return existing.request;
    const controller = new AbortController();
    const source = demoCaseId
        ? clientContextApi.getDemoContext(demoCaseId, brandId, controller.signal)
        : clientContextApi.getContext(brandId, controller.signal);
    const request = source.then((res) => {
        const data = res.data;
        if (activeClientAuthorizationScope === authorizationScope && data.success && data.context) {
            clientContextCache.set(key, data.context);
            clientContextCacheUpdatedAt.set(key, Date.now());
        }
        return res;
    }).finally(() => {
        if (sharedContextRequests.get(key)?.request === request) sharedContextRequests.delete(key);
    });
    sharedContextRequests.set(key, { controller, request });
    return request;
}

function abortSharedContextRequestsExcept(authorizationScope: string | null): void {
    const keepPrefix = authorizationScope === null ? null : `${authorizationScope}:`;
    for (const [key, entry] of sharedContextRequests) {
        if (!keepPrefix || !key.startsWith(keepPrefix)) {
            entry.controller.abort();
            sharedContextRequests.delete(key);
        }
    }
}

function abortSharedContextRequest(authorizationScope: string, brandId: number): void {
    const prefix = `${authorizationScope}:${brandId}:`;
    for (const [key, entry] of sharedContextRequests) {
        if (!key.startsWith(prefix)) continue;
        entry.controller.abort();
        sharedContextRequests.delete(key);
    }
}

function selectionStorageKey(userId: number): string {
    return `${SELECTION_KEY_PREFIX}${userId}`;
}

// [Review-CTO 2026-07-26] 演示案例的 case_id 也要跟着选择一起活下来。
// 症状:选好左上角的演示案例 → 打开效果监测 → 左上角自己空了、页面没数据,
//      必须再选一次才出来。
// 根因:token 刷新/权限刷新会轮换 authorizationScope,把 clients 列表清空重取;
//      这个窗口里自动加载 effect 已经跑起来,`clientsRef` 是空的 → 解析不到
//      该品牌的 access_mode='demo' → 按**真实品牌**去请求演示品牌 → 403
//      → 403 分支连带清掉 currentBrandId 与 sessionStorage。
// 修法:case_id 与品牌选择同源持久化(tab 级)。它不是凭证——服务端每个请求都
//      用 resolve_demo_case_access 重新校验 grant 的状态与有效期,存下来只是让
//      前端别把演示品牌误当真实品牌请求。
function demoCaseStorageKey(userId: number): string {
    return `${SELECTION_KEY_PREFIX}demo:${userId}`;
}

function readPersistedDemoCaseId(userId: number | null, brandId: number): string | undefined {
    if (userId === null) return undefined;
    try {
        const raw = sessionStorage.getItem(demoCaseStorageKey(userId));
        if (!raw) return undefined;
        const parsed = JSON.parse(raw) as { brandId?: number; caseId?: string };
        if (parsed && Number(parsed.brandId) === Number(brandId) && parsed.caseId) {
            return String(parsed.caseId);
        }
    } catch {
        /* 存储不可用/格式坏 → 当作没有,回落原有语义 */
    }
    return undefined;
}

function writePersistedDemoCase(userId: number | null, selection: { brandId: number; caseId: string } | null): void {
    if (userId === null) return;
    try {
        if (selection) {
            sessionStorage.setItem(demoCaseStorageKey(userId), JSON.stringify(selection));
        } else {
            sessionStorage.removeItem(demoCaseStorageKey(userId));
        }
    } catch {
        /* 忽略:持久化失败只影响跨刷新体验,不影响本次会话 */
    }
}

function clearClientArtifactsExcept(authorizationScope: string | null, userId: number | null): void {
    const keepPrefix = authorizationScope === null ? null : `${authorizationScope}:`;
    for (const key of clientContextCache.keys()) {
        if (!keepPrefix || !key.startsWith(keepPrefix)) {
            clientContextCache.delete(key);
            clientContextCacheUpdatedAt.delete(key);
        }
    }
    for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
        const key = sessionStorage.key(index);
        if (key?.startsWith(LEGACY_SCOPED_SELECTION_PREFIX)) {
            sessionStorage.removeItem(key);
        } else if (key?.startsWith(SELECTION_KEY_PREFIX)
            && (userId === null || key !== selectionStorageKey(userId))) {
            sessionStorage.removeItem(key);
        }
    }
}

const ClientContext = createContext<ClientContextType>({
    clients: [],
    currentBrandId: null,
    clientContext: null,
    relatedQuoteIds: [],
    loading: false,
    listLoading: false,
    listReady: false,
    error: null,
    listError: null,
    isAllClientsMode: false,
    switchClient: () => { },
    refreshClients: async () => { },
});

// ========== Provider ==========

export function ClientProvider({ children }: { children: ReactNode }) {
    const { user, authorizationScope } = useAuth();
    const userId = user?.id ?? null;
    const isAdmin = user?.is_admin === true;
    const [clients, setClients] = useState<BrandSummary[]>([]);
    const [currentBrandId, setCurrentBrandId] = useState<number | null>(null);
    const [clientContext, setClientContext] = useState<ClientContextData | null>(null);
    const [loading, setLoading] = useState(false);
    const [listLoading, setListLoading] = useState(false);
    const [listReady, setListReady] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [listError, setListError] = useState<string | null>(null);
    const [isAllClientsMode, setIsAllClientsMode] = useState(false);
    const clientsRef = useRef<BrandSummary[]>([]);
    const latestBrandIdRef = useRef<number | null>(null);
    const lastAutoLoadBrandIdRef = useRef<number | null>(null);
    const listAbortRef = useRef<AbortController | null>(null);
    const contextAbortRef = useRef<AbortController | null>(null);
    const listInFlightRef = useRef<Promise<void> | null>(null);
    const contextInFlightRef = useRef<{ brandId: number; request: Promise<void> } | null>(null);
    const pendingBrandIdRef = useRef<number | null>(null);
    const autoSelectionBlockedBrandIdRef = useRef<number | null>(null);
    const authorizationScopeRef = useRef(authorizationScope);
    const previousUserIdRef = useRef<number | null>(null);

    useLayoutEffect(() => {
        const previousUserId = previousUserIdRef.current;
        const samePrincipal = userId !== null && previousUserId === userId;
        previousUserIdRef.current = userId;
        const activeScope = userId === null ? null : authorizationScope;
        // Pre-session builds persisted this unscoped key in localStorage. It cannot be
        // trusted after an authority change, so remove it instead of migrating it into the
        // new opaque session scope. The authoritative client list will choose a safe default.
        localStorage.removeItem('omnirank_current_brand_id');
        authorizationScopeRef.current = authorizationScope;
        activeClientAuthorizationScope = activeScope;
        abortSharedContextRequestsExcept(activeScope);
        clearClientArtifactsExcept(activeScope, userId);
        listAbortRef.current?.abort();
        contextAbortRef.current?.abort();
        listInFlightRef.current = null;
        contextInFlightRef.current = null;
        latestBrandIdRef.current = null;
        lastAutoLoadBrandIdRef.current = null;
        clientsRef.current = [];
        setClients([]);
        setClientContext(null);
        setIsAllClientsMode(false);
        setError(null);
        setListError(null);
        setLoading(false);
        setListLoading(false);
        setListReady(false);
        // Token/permission refreshes rotate authorizationScope. A demo grant is
        // still revalidated by the dedicated endpoint, so keep the selection
        // for the same principal and avoid a transient live-brand request.
        if (!samePrincipal) {
            setActiveDemoSelection(null);
            writePersistedDemoCase(userId, null);   // 换人登录绝不继承演示选择
        }
        if (userId === null) {
            pendingBrandIdRef.current = null;
            setCurrentBrandId(null);
            return;
        }
        const saved = sessionStorage.getItem(selectionStorageKey(userId));
        const parsed = saved ? parseInt(saved, 10) : null;
        pendingBrandIdRef.current = parsed !== null && !isNaN(parsed) ? parsed : null;
        autoSelectionBlockedBrandIdRef.current = null;
        // Never render or request a persisted brand until the new authority's list
        // confirms that it is still assigned to this user.
        setCurrentBrandId(null);
    }, [authorizationScope, userId]);

    // 加载品牌列表——统一使用 api.ts 封装
    const refreshClients = useCallback((): Promise<void> => {
        if (userId === null) return Promise.resolve();
        const requestScope = authorizationScope;
        if (listInFlightRef.current) return listInFlightRef.current;
        listAbortRef.current?.abort();
        const controller = new AbortController();
        listAbortRef.current = controller;
        setListLoading(true);
        const request = (async () => {
        try {
            const res = await clientContextApi.list(controller.signal);
            if (controller.signal.aborted || authorizationScopeRef.current !== requestScope) return;
            const data = res.data;
            if (data.success && data.clients) {
                clientsRef.current = data.clients;
                setClients(data.clients);
                setListError(null);
                setListReady(true);
            }
        } catch (err) {
            if (controller.signal.aborted || authorizationScopeRef.current !== requestScope) return;
            console.error('[ClientContext] 加载品牌列表失败:', err);
            setListError('客户列表读取失败 · 已保留上次成功数据');
        } finally {
            if (!controller.signal.aborted && authorizationScopeRef.current === requestScope) setListLoading(false);
            if (listAbortRef.current === controller) listAbortRef.current = null;
        }
        })();
        listInFlightRef.current = request;
        void request.finally(() => {
            if (listInFlightRef.current === request) listInFlightRef.current = null;
        });
        return request;
    }, [authorizationScope, userId]);

    // 加载选中品牌的完整上下文——统一使用 api.ts 封装
    const loadClientContext = useCallback((
        brandId: number,
        force = false,
        accessIdentity?: ClientAccessIdentity,
    ): Promise<void> => {
        if (userId === null) return Promise.resolve();
        const requestScope = authorizationScope;
        const selectedClient = clientsRef.current.find(client => client.id === brandId);
        if (!accessIdentity && selectedClient?.access_mode === 'demo' && !selectedClient.demo_case_id) {
            setActiveDemoSelection(null);
            setClientContext(null);
            setLoading(false);
            autoSelectionBlockedBrandIdRef.current = brandId;
            setError('演示案例缺少访问凭证 · 已停止自动加载');
            return Promise.resolve();
        }
        // 列表未就绪时 selectedClient 为 undefined —— 此时**不能**默认按真实品牌请求
        // (演示品牌会 403 并连带清空选择,见 demoCaseStorageKey 注释)。回退顺序:
        // 调用方显式身份 → 列表行 → 进程内保留的选择 → tab 级持久化的 case_id → 真实。
        const preserved = getActiveDemoSelection();
        const fallbackDemoCaseId = selectedClient
            ? undefined
            : (preserved && preserved.brandId === brandId ? preserved.caseId : undefined)
                ?? readPersistedDemoCaseId(userId, brandId);
        const resolvedAccess = accessIdentity
            ?? (
                selectedClient?.access_mode === 'demo' && selectedClient.demo_case_id
                    ? { mode: 'demo' as const, caseId: selectedClient.demo_case_id }
                    : fallbackDemoCaseId
                        ? { mode: 'demo' as const, caseId: fallbackDemoCaseId }
                        : { mode: 'real' as const }
            );
        const demoCaseId = resolvedAccess.mode === 'demo' ? resolvedAccess.caseId : undefined;
        const cacheKey = clientContextCacheKey(requestScope, brandId, demoCaseId);
        if (force) {
            contextAbortRef.current?.abort();
            abortSharedContextRequest(requestScope, brandId);
            contextInFlightRef.current = null;
            // [客户反馈⑤ 机制B 2026-08-09] 改 stale-while-revalidate:**不再 delete 缓存条目**。
            //   旧写法先 delete 再 `setClientContext(cached)`,而 delete 之后 cached 必然是 null
            //   → clientContext 被**同步置空** → Monitoring 的 useLayoutEffect 把关键词/发布/
            //   趋势/结果全部 state 清空 → 数据回来再填 = 页面"跳一下"。触发它的不是报错,
            //   是任意一次**成功的**写操作(api.ts 写成功即广播失效 → force 重载)。
            //   07-28 修过的是 403/404 误清空那条,200 成功这条通路当时没覆盖。
            //   现在只把"更新时间"抹掉(强制它过期 → 一定重新拉),旧数据留在屏幕上直到新数据回来。
            clientContextCacheUpdatedAt.delete(cacheKey);
        } else if (contextInFlightRef.current?.brandId === brandId) {
            return contextInFlightRef.current.request;
        }
        contextAbortRef.current?.abort();
        const controller = new AbortController();
        contextAbortRef.current = controller;
        latestBrandIdRef.current = brandId;
        lastAutoLoadBrandIdRef.current = brandId;
        const cached = clientContextCache.get(cacheKey) ?? null;
        setClientContext(cached);
        setError(null);
        // force 时上面已抹掉 updatedAt,这条新鲜度短路必然不成立 —— 强制重载语义不变。
        if (cached && Date.now() - (clientContextCacheUpdatedAt.get(cacheKey) || 0) < CLIENT_CONTEXT_FRESH_MS) {
            setLoading(false);
            return Promise.resolve();
        }
        setLoading(true);
        const request = (async () => {
        try {
            // The same provider can briefly remount while a route synchronizes query params.
            // Share the underlying read across those consumers; the local controller still
            // prevents an obsolete component from writing state.
            const res = await sharedClientContextRequest(requestScope, brandId, demoCaseId);
            // 校验：如果在请求期间用户又切换了，丢弃这个响应
            if (controller.signal.aborted || authorizationScopeRef.current !== requestScope || latestBrandIdRef.current !== brandId) return;
            const data = res.data;
            // 合并重定向：品牌已被合并到其他品牌，自动切换
            if (data.redirect_to) {
                const newId = data.redirect_to;
                setCurrentBrandId(newId);
                sessionStorage.setItem(selectionStorageKey(userId), String(newId));
                latestBrandIdRef.current = newId;
                void loadClientContext(newId);
                return;
            }
            if (data.success && data.context) {
                setClientContext(data.context);
                const nextDemoSelection = resolvedAccess.mode === 'demo'
                    ? { brandId, caseId: resolvedAccess.caseId }
                    : null;
                setActiveDemoSelection(nextDemoSelection);
                // 与品牌选择同源持久化:换页/刷新后仍能按演示身份请求(见上方注释)。
                writePersistedDemoCase(userId, nextDemoSelection);
                setError(null);
            } else {
                setActiveDemoSelection(null);
                setError('客户上下文响应不完整 · 这不代表客户数据为空');
            }
        } catch (err: any) {
            if (controller.signal.aborted || authorizationScopeRef.current !== requestScope || latestBrandIdRef.current !== brandId) return;
            // [P2-14 fix 2026-05-23] 区分 403/404/重复 vs 真错 · 降噪 console
            const status = err?.response?.status ?? err?.status;
            if (status === 403 || status === 404) {
                // [M-2 2026-07-28] 403/404 **不再清除用户已选客户**(选择是 UI 状态
                // 不是凭证)。权限刷新/演示 grant 校验的瞬时 403 一旦连带清 state +
                // sessionStorage,用户看到的就是"客户被清空 → 页面闪断"。
                // 防无界循环由两道既有闸承担:lastAutoLoadBrandIdRef 让自动加载
                // effect 对同一品牌只试一次;autoSelectionBlockedBrandIdRef 阻止
                // pickDefault 自动重选。品牌真被删/真撤权由列表校验 effect
                // (exists 检查,以权威列表为准)负责收尾清理。
                // 持久化的演示 case_id 同样保留:服务端每请求都重新校验 grant,
                // 保留它只是让显式重试仍按演示身份走,不构成越权面。
                setClientContext(null);
                setActiveDemoSelection(null);
                autoSelectionBlockedBrandIdRef.current = brandId;
                setError(status === 403
                    ? '当前账号暂时无权读取该客户 · 已停止自动重试,可手动重选或刷新'
                    : '该客户暂时无法读取(可能已被删除) · 已停止自动重试');
            } else {
                console.error('[ClientContext] 加载客户上下文失败:', err);
                // 同账号同客户保留最后一次成功值；没有缓存时保持明确未知态。
                setClientContext(clientContextCache.get(cacheKey) ?? null);
                setError('客户详情读取失败 · 已保留上次成功数据');
            }
        } finally {
            if (!controller.signal.aborted && authorizationScopeRef.current === requestScope && latestBrandIdRef.current === brandId) {
                setLoading(false);
            }
            if (contextAbortRef.current === controller) contextAbortRef.current = null;
        }
        })();
        contextInFlightRef.current = { brandId, request };
        void request.finally(() => {
            if (contextInFlightRef.current?.request === request) contextInFlightRef.current = null;
        });
        return request;
    }, [authorizationScope, userId]);

    // 切换客户
    const switchClient = useCallback((brandId: number | null, allMode: boolean = false) => {
        if (userId === null) return;
        // Explicit selection/retry is allowed to revalidate a brand that was
        // previously blocked from automatic reselection after a 403/404.
        if (brandId !== null) autoSelectionBlockedBrandIdRef.current = null;
        const nextClient = brandId === null ? null : clientsRef.current.find(client => client.id === brandId);
        const nextAccessIdentity: ClientAccessIdentity | null = nextClient?.access_mode === 'demo'
            ? nextClient.demo_case_id
                ? { mode: 'demo', caseId: nextClient.demo_case_id }
                : null
            : { mode: 'real' };
        // A list row is not enough to authorize demo reads. Business requests
        // remain pending until the dedicated demo detail endpoint succeeds.
        setActiveDemoSelection(null);
        const previousBrandId = latestBrandIdRef.current;
        if (previousBrandId !== null && previousBrandId !== brandId) {
            abortSharedContextRequest(authorizationScope, previousBrandId);
        }
        contextAbortRef.current?.abort();
        contextInFlightRef.current = null;
        setCurrentBrandId(brandId);
        setIsAllClientsMode(allMode);
        if (brandId !== null) {
            sessionStorage.setItem(selectionStorageKey(userId), String(brandId));
            // Never pair a new brand id with the previous brand's detail during the request.
            setClientContext(clientContextCache.get(clientContextCacheKey(
                authorizationScope,
                brandId,
                nextClient?.access_mode === 'demo' ? nextClient.demo_case_id : undefined,
            )) ?? null);
            setError(null);
            if (!nextAccessIdentity) {
                setClientContext(null);
                setLoading(false);
                autoSelectionBlockedBrandIdRef.current = brandId;
                setError('演示案例缺少访问凭证 · 已停止自动加载');
            } else {
                void loadClientContext(brandId, false, nextAccessIdentity);
            }
        } else {
            sessionStorage.removeItem(selectionStorageKey(userId));
            latestBrandIdRef.current = null;
            lastAutoLoadBrandIdRef.current = null;
            if (allMode) {
                setClientContext(null); // 不加载特定客户上下文
            } else {
                setClientContext(null);
            }
        }
        // 清理社媒相关的缓存，防止跨客户数据残留
        localStorage.removeItem('currentProjectId');
    }, [authorizationScope, loadClientContext, userId]);

    // 初始化：加载列表 + 恢复上次选中的品牌
    useEffect(() => {
        if (userId !== null) void refreshClients();
        return () => {
            listAbortRef.current?.abort();
            contextAbortRef.current?.abort();
        };
    }, [refreshClients, userId]);

    useEffect(() => () => setActiveDemoSelection(null), []);


    useEffect(() => {
        const clearAnonymousSelection = () => {
            if (readStoredSessionToken() !== null) return;
            clearClientArtifactsExcept(null, null);
            pendingBrandIdRef.current = null;
        };
        window.addEventListener(SESSION_CANDIDATE_EVENT, clearAnonymousSelection);
        return () => window.removeEventListener(SESSION_CANDIDATE_EVENT, clearAnonymousSelection);
    }, []);

    useEffect(() => {
        const onMutation = (event: Event) => {
            const tags = (event as CustomEvent<{ tags?: string[] }>).detail?.tags || [];
            if (!tags.includes('clients') || userId === null) return;
            const brandId = latestBrandIdRef.current ?? currentBrandId;
            listAbortRef.current?.abort();
            listInFlightRef.current = null;
            void (async () => {
                await refreshClients();
                if (brandId !== null) {
                    lastAutoLoadBrandIdRef.current = null;
                    await loadClientContext(brandId, true);
                }
            })();
        };
        window.addEventListener('omnirank-api-mutated', onMutation);
        return () => window.removeEventListener('omnirank-api-mutated', onMutation);
    }, [currentBrandId, loadClientContext, refreshClients, userId]);

    // 🆕 BUG-P0-4 根本修 (CTO-15.22 2026-05-03):
    // 启动时 useState 从 localStorage 恢复 currentBrandId · 但原代码不自动 loadClientContext
    // 导致 currentBrandId !== null 但 clientContext === null · 所有依赖 clientContext 的 prefill 失效
    // 修法:启动 + currentBrandId 变化时 · 如果 clientContext 缺失 · 主动加载一次。
    // 失败后必须等待用户显式切换/重试，不能因 loading=false 立即自旋。
    useEffect(() => {
        if (currentBrandId !== null
            && !clientContext
            && !loading
            && lastAutoLoadBrandIdRef.current !== currentBrandId) {
            loadClientContext(currentBrandId);
        }
    }, [currentBrandId, clientContext, loading, loadClientContext]);

    // 品牌列表加载后的初始化/校验
    useEffect(() => {
        if (clients.length === 0) {
            if (listReady && !listLoading && !listError && userId !== null && pendingBrandIdRef.current !== null) {
                sessionStorage.removeItem(selectionStorageKey(userId));
                pendingBrandIdRef.current = null;
            }
            return;
        }
        if (listReady && currentBrandId === null && pendingBrandIdRef.current !== null) {
            const pendingBrandId = pendingBrandIdRef.current;
            pendingBrandIdRef.current = null;
            if (clients.some(client => client.id === pendingBrandId)) {
                switchClient(pendingBrandId);
                return;
            }
            if (userId !== null) sessionStorage.removeItem(selectionStorageKey(userId));
        }
        if (currentBrandId !== null) {
            // 验证保存的 brandId 是否还存在
            const exists = clients.some(c => c.id === currentBrandId);
            if (!exists) {
                // 品牌已被删除 → 按角色选择默认
                if (userId !== null) sessionStorage.removeItem(selectionStorageKey(userId));
                latestBrandIdRef.current = null;
                lastAutoLoadBrandIdRef.current = null;
                setCurrentBrandId(null);
                setClientContext(null);
                setLoading(false);
                setActiveDemoSelection(null);
                writePersistedDemoCase(userId, null);   // 该品牌已不在可见列表
                pickDefault();
            }
        } else if (!isAllClientsMode) {
            // 首次登录没有选过品牌 → 按角色选择默认
            pickDefault();
        }

        function pickDefault() {
            if (isAdmin) {
                // 管理员 → 默认"全部客户"模式
                switchClient(null, true);
            } else if (clients.length === 1) {
                // 只有一个品牌 → 直接选中
                if (autoSelectionBlockedBrandIdRef.current !== clients[0].id) {
                    switchClient(clients[0].id);
                }
            } else {
                // 多个品牌 → 不自动选，等用户手动选
                // （避免选错客户导致数据混淆）
            }
        }
    }, [authorizationScope, clients, isAdmin, isAllClientsMode, listError, listLoading, listReady, switchClient, userId]);

    const relatedQuoteIds = clientContext?.relatedQuoteIds || [];

    return (
        <ClientContext.Provider value={{
            clients,
            currentBrandId,
            clientContext,
            relatedQuoteIds,
            loading,
            listLoading,
            listReady,
            error,
            listError,
            isAllClientsMode,
            switchClient,
            refreshClients,
        }}>
            {children}
        </ClientContext.Provider>
    );
}

// ========== Hooks ==========

export function useClientContext() {
    return useContext(ClientContext);
}

export default ClientContext;
