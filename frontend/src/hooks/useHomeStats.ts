import { useCallback, useEffect, useRef, useState } from 'react';
import { useAuth } from '@/context/AuthContext';
import { authFetch } from '@/lib/api';

interface HomeStatsState<T> {
    data: T | null;
    loading: boolean;
    stale: boolean;
    error: string | null;
    retry: () => Promise<void>;
}

const CACHE_TTL_MS = 30_000;
const homeStatsCache = new Map<string, { value: unknown; updatedAt: number }>();

function retryAfterMs(response: Response): number {
    const raw = response.headers.get('Retry-After')?.trim();
    if (!raw) return 30_000;
    const seconds = Number(raw);
    if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
    const dateMs = Date.parse(raw);
    return Number.isFinite(dateMs) ? Math.max(0, dateMs - Date.now()) : 30_000;
}

export function useHomeStats<T = Record<string, unknown>>(): HomeStatsState<T> {
    const { user, authorizationScope } = useAuth();
    const userId = user?.id ?? null;
    const homeScope = userId === null ? null : authorizationScope;
    const [data, setData] = useState<T | null>(null);
    const [dataOwnerScope, setDataOwnerScope] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [stale, setStale] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const controllerRef = useRef<AbortController | null>(null);
    const inFlightRef = useRef<Promise<void> | null>(null);
    const retryNotBeforeRef = useRef(0);
    const retryOwnerScopeRef = useRef<string | null>(null);
    const lastResumeRef = useRef(0);

    const load = useCallback((force = false): Promise<void> => {
        if (homeScope === null) return Promise.resolve();
        const cached = homeStatsCache.get(homeScope) as { value: T; updatedAt: number } | undefined;
        if (!force && cached && Date.now() - cached.updatedAt <= CACHE_TTL_MS) {
            setData(cached.value);
            setDataOwnerScope(homeScope);
            setLoading(false);
            setStale(false);
            setError(null);
            return Promise.resolve();
        }
        if (retryOwnerScopeRef.current === homeScope && Date.now() < retryNotBeforeRef.current) return Promise.resolve();
        if (inFlightRef.current) {
            if (controllerRef.current?.signal.aborted) {
                return inFlightRef.current.finally(() => load(force));
            }
            return inFlightRef.current;
        }
        const controller = new AbortController();
        controllerRef.current = controller;
        setLoading(!cached);
        const request = (async () => {
            try {
                const response = await authFetch('/api/user/home-stats', { signal: controller.signal });
                if (response.status === 429) {
                    retryOwnerScopeRef.current = homeScope;
                    retryNotBeforeRef.current = Date.now() + retryAfterMs(response);
                }
                if (!response.ok) throw new Error(`首页数据读取失败: HTTP ${response.status}`);
                const value = await response.json() as T & { status?: string };
                if (value?.status !== 'success') throw new Error('首页数据响应不完整');
                if (controller.signal.aborted) return;
                homeStatsCache.set(homeScope, { value, updatedAt: Date.now() });
                setData(value);
                setDataOwnerScope(homeScope);
                setStale(false);
                setError(null);
                retryNotBeforeRef.current = 0;
                retryOwnerScopeRef.current = null;
            } catch (loadError) {
                if (controller.signal.aborted) return;
                const lastGood = homeStatsCache.get(homeScope) as { value: T } | undefined;
                if (lastGood) {
                    setData(lastGood.value);
                    setDataOwnerScope(homeScope);
                    setStale(true);
                }
                setError(loadError instanceof Error ? loadError.message : '首页数据读取失败');
            } finally {
                if (!controller.signal.aborted) setLoading(false);
                if (controllerRef.current === controller) controllerRef.current = null;
            }
        })();
        inFlightRef.current = request;
        void request.finally(() => {
            if (inFlightRef.current === request) inFlightRef.current = null;
        });
        return request;
    }, [homeScope]);

    useEffect(() => {
        controllerRef.current?.abort();
        inFlightRef.current = null;
        if (homeScope === null) {
            homeStatsCache.clear();
            setData(null);
            setDataOwnerScope(null);
            setLoading(false);
            setStale(false);
            setError(null);
            return;
        }
        for (const scope of homeStatsCache.keys()) {
            if (scope !== homeScope) homeStatsCache.delete(scope);
        }
        const cached = homeStatsCache.get(homeScope) as { value: T; updatedAt: number } | undefined;
        setData(cached?.value ?? null);
        setDataOwnerScope(cached ? homeScope : null);
        setLoading(!cached);
        setStale(!!cached && Date.now() - cached.updatedAt > CACHE_TTL_MS);
        setError(null);
        void load(!!cached);
        const onResume = () => {
            if (document.visibilityState !== 'visible') {
                controllerRef.current?.abort();
                return;
            }
            const now = Date.now();
            if (now - lastResumeRef.current < 250) return;
            lastResumeRef.current = now;
            void load(true);
        };
        document.addEventListener('visibilitychange', onResume);
        window.addEventListener('focus', onResume);
        return () => {
            document.removeEventListener('visibilitychange', onResume);
            window.removeEventListener('focus', onResume);
            controllerRef.current?.abort();
        };
    }, [homeScope, load]);

    return {
        data: dataOwnerScope === homeScope ? data : null,
        loading,
        stale,
        error,
        retry: () => load(true),
    };
}
