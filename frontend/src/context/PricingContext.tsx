/**
 * PricingContext — 全局价目表 Context
 *
 * 作用:
 *   - 把 `/api/wallet/pricing` 拉回的 feature_pricing 表缓存到全局
 *   - 所有按钮、卡片、AI 工具前端展示都走 usePricing() 动态取价
 *   - 改价只改 DB 的 feature_pricing 表，前端 5 分钟内自动同步
 *
 * 权威源:
 *   DB 表 `feature_pricing` (feature_code PK, cost_points, cost_compute, feature_name, is_active)
 *   后端接口 `GET /api/wallet/pricing` (api/wallet_api.py:160)
 *
 * 改价流程（见 docs/商业模式与定价/价目表改价同步指南.md）:
 *   1. `UPDATE feature_pricing SET cost_points = 新价 WHERE feature_code = 'xxx'`
 *   2. 同步更新 db/wallet_db.py:seed_feature_pricing 的 PRICING_DATA 列表（新环境初始化）
 *   3. 前端 5 分钟内自动刷新（或用户手动刷新页面立即生效）
 *
 * 禁止硬编码价格的地方:
 *   - 前端按钮文案（用 FeatureCostBadge 组件）
 *   - AI 工具 docstring / confirm_card description
 *   - 前端任何展示给用户的数字
 *
 * v1_3 · CTO-15.1 · 2026-04-19
 */

import { createContext, useContext, useEffect, useState, useCallback, useRef, ReactNode } from 'react';
import { useAuth } from '@/context/AuthContext';
import { authFetch } from '@/lib/api';

export interface PricingItem {
  feature_code: string;
  feature_name: string;
  cost_points: number;
  cost_compute: number;
  requires_paid_points: boolean;
  is_active: boolean;
}

interface PricingContextType {
  pricingMap: Record<string, PricingItem>;
  loading: boolean;
  error: string | null;
  /** 取某功能的 cost_points；未找到返 null（调用方可显示占位） */
  getCost: (featureCode: string) => number | null;
  /** 仅返回当前 authority 下仍新鲜、已确认且 active 的付费价格。 */
  getTrustedCost: (featureCode: string) => number | null;
  /** 取某功能的完整定义 */
  getPricing: (featureCode: string) => PricingItem | null;
  /** 旧值可展示，但付费动作必须同时要求 trusted=true。 */
  trusted: boolean;
  updatedAt: number | null;
  /** 手动刷新（管理员改价后立即生效）*/
  refresh: () => Promise<void>;
  /**
   * [#199] 退避窗到期的时间戳(ms);0 = 不在退避窗内。
   *
   * 🔴 为什么要暴露它:`refresh()` 在退避窗内**静默不发**(见 fetchPricing 开头那一段)。
   *    界面上给一颗「重试」而它这一刻什么都不做,就是一颗**点了没反应的按钮** ——
   *    本仓明令禁止那种东西。有了这个时间戳,出口可以写成「N 秒后可重试」并禁用,
   *    把「现在不能重试」这件事**说出来**,而不是让人点了之后自己猜。
   */
  retryNotBefore: number;
  /** Internal consumer lease: pricing reads exist only while a real consumer is mounted. */
  activate: () => () => void;
}

const PricingContext = createContext<PricingContextType | null>(null);

const REFRESH_INTERVAL_MS = 5 * 60 * 1000; // 5 分钟
const PRICING_CACHE_TTL_MS = 30 * 1000;
const pricingCache = new Map<string, { value: Record<string, PricingItem>; updatedAt: number; trusted: boolean }>();

function retryAfterMs(response: Response): number {
  const raw = response.headers.get('Retry-After')?.trim();
  if (!raw) return 30_000;
  const seconds = Number(raw);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1000;
  const retryAt = Date.parse(raw);
  return Number.isFinite(retryAt) ? Math.max(0, retryAt - Date.now()) : 30_000;
}

export function PricingProvider({ children }: { children: ReactNode }) {
  const { user, isLoading: authLoading, authorizationScope } = useAuth();
  const userId = user?.id ?? null;
  const pricingScope = userId === null ? null : authorizationScope;
  const [pricingMap, setPricingMap] = useState<Record<string, PricingItem>>({});
  const [dataOwnerScope, setDataOwnerScope] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [consumerCount, setConsumerCount] = useState(0);
  const hasConsumers = consumerCount > 0;
  const [error, setError] = useState<string | null>(null);
  const [trustedUntil, setTrustedUntil] = useState(0);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const activeControllerRef = useRef<AbortController | null>(null);
  const inFlightRef = useRef<{ scope: string; request: Promise<void> } | null>(null);
  const retryNotBeforeRef = useRef(0);
  /* 🔴 [#199] ref 变化不会触发重渲染 —— 界面要显示「N 秒后可重试」就必须有 state。
     两者同写:ref 给 fetch 里的同步判断用,state 给界面用。只写 ref 的话,
     退避窗开始/结束时按钮不会更新,那又是一处「看起来能点其实不能」。 */
  const [retryNotBefore, setRetryNotBefore] = useState(0);
  const retryOwnerScopeRef = useRef<string | null>(null);

  const fetchPricing = useCallback((force = false): Promise<void> => {
    if (authLoading || pricingScope === null || !hasConsumers) {
      setLoading(false);
      return Promise.resolve();
    }
    const cached = pricingCache.get(pricingScope);
    if (force) {
      setTrustedUntil(0);
      if (cached) pricingCache.set(pricingScope, { ...cached, trusted: false });
      // A mutation can complete while the pre-mutation price GET is still in flight.
      // Never join that obsolete transport and later bless its result as fresh.
      if (inFlightRef.current?.scope === pricingScope) {
        activeControllerRef.current?.abort();
        inFlightRef.current = null;
      }
    }
    if (retryOwnerScopeRef.current === pricingScope && Date.now() < retryNotBeforeRef.current) {
      setLoading(false);
      return Promise.resolve();
    }
    if (!force && cached?.trusted && Date.now() - cached.updatedAt <= PRICING_CACHE_TTL_MS) {
      setPricingMap(cached.value);
      setDataOwnerScope(pricingScope);
      setLoading(false);
      setError(null);
      setUpdatedAt(cached.updatedAt);
      setTrustedUntil(cached.updatedAt + PRICING_CACHE_TTL_MS);
      return Promise.resolve();
    }
    if (inFlightRef.current?.scope === pricingScope) {
      if (activeControllerRef.current?.signal.aborted) {
        return inFlightRef.current.request.finally(() => fetchPricing(force));
      }
      return inFlightRef.current.request;
    }

    activeControllerRef.current?.abort();
    const controller = new AbortController();
    activeControllerRef.current = controller;
    // dataOwnerScope is only a render guard. Making it a callback dependency causes the
    // successful response to recreate this callback and re-run the loading effect.
    setLoading(!cached);
    const request = (async () => {
      try {
        const res = await authFetch('/api/wallet/pricing', { signal: controller.signal });
        if (!res.ok) {
          if (res.status === 429) {
            retryOwnerScopeRef.current = pricingScope;
            retryNotBeforeRef.current = Date.now() + retryAfterMs(res);
            setRetryNotBefore(retryNotBeforeRef.current);
          }
          throw new Error(`加载价目表失败: HTTP ${res.status}${res.status === 429 ? ' · 请按服务端提示稍后重试' : ''}`);
        }
        const body = await res.json();
        const list: PricingItem[] = body?.data || [];
        const map: Record<string, PricingItem> = {};
        for (const p of list) map[p.feature_code] = p;
        if (controller.signal.aborted) return;
        const refreshedAt = Date.now();
        pricingCache.set(pricingScope, { value: map, updatedAt: refreshedAt, trusted: true });
        setPricingMap(map);
        setDataOwnerScope(pricingScope);
        setError(null);
        setUpdatedAt(refreshedAt);
        setTrustedUntil(refreshedAt + PRICING_CACHE_TTL_MS);
        retryNotBeforeRef.current = 0;
        setRetryNotBefore(0);
        retryOwnerScopeRef.current = null;
      } catch (e) {
        if (controller.signal.aborted) return;
        // 同账号最后一次成功值继续展示；错误不能退化成旧价格或伪 0。
        if (cached) pricingCache.set(pricingScope, { ...cached, trusted: false });
        setError((e as Error)?.message || '加载价目表失败');
        setTrustedUntil(0);
      } finally {
        if (!controller.signal.aborted) setLoading(false);
        if (activeControllerRef.current === controller) activeControllerRef.current = null;
      }
    })();
    inFlightRef.current = { scope: pricingScope, request };
    void request.finally(() => {
      if (inFlightRef.current?.request === request) inFlightRef.current = null;
    });
    return request;
  }, [authLoading, hasConsumers, pricingScope]);

  const activate = useCallback(() => {
    setConsumerCount(count => count + 1);
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      setConsumerCount(count => Math.max(0, count - 1));
    };
  }, []);

  useEffect(() => {
    activeControllerRef.current?.abort();
    inFlightRef.current = null;
    for (const scope of pricingCache.keys()) {
      if (scope !== pricingScope) pricingCache.delete(scope);
    }
    if (authLoading || pricingScope === null || !hasConsumers) {
      if (pricingScope === null) pricingCache.clear();
      if (pricingScope === null) {
        setPricingMap({});
        setDataOwnerScope(null);
        setError(null);
        setUpdatedAt(null);
      }
      setTrustedUntil(0);
      setLoading(false);
      return;
    }
    const cached = pricingCache.get(pricingScope);
    if (cached) {
      setPricingMap(cached.value);
      setDataOwnerScope(pricingScope);
      setLoading(false);
      setUpdatedAt(cached.updatedAt);
      setTrustedUntil(cached.trusted ? cached.updatedAt + PRICING_CACHE_TTL_MS : 0);
    } else {
      setPricingMap({});
      setDataOwnerScope(pricingScope);
      setLoading(true);
      setUpdatedAt(null);
      setTrustedUntil(0);
    }
    void fetchPricing();
    const t = window.setInterval(() => {
      if (document.visibilityState === 'visible') void fetchPricing(true);
    }, REFRESH_INTERVAL_MS);
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        activeControllerRef.current?.abort();
      } else {
        void fetchPricing();
      }
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(t);
      document.removeEventListener('visibilitychange', onVisibility);
      activeControllerRef.current?.abort();
    };
  }, [authLoading, fetchPricing, hasConsumers, pricingScope]);

  useEffect(() => {
    if (!hasConsumers || trustedUntil <= Date.now()) return;
    const timeout = window.setTimeout(() => {
      setTrustedUntil(0);
      if (document.visibilityState === 'visible') void fetchPricing(true);
    }, Math.max(0, trustedUntil - Date.now()) + 1);
    return () => window.clearTimeout(timeout);
  }, [fetchPricing, hasConsumers, trustedUntil]);

  useEffect(() => {
    if (!hasConsumers) return;
    const onMutation = (event: Event) => {
      const tags = (event as CustomEvent<{ tags?: string[] }>).detail?.tags || [];
      if (tags.includes('pricing')) void fetchPricing(true);
    };
    window.addEventListener('omnirank-api-mutated', onMutation);
    return () => window.removeEventListener('omnirank-api-mutated', onMutation);
  }, [fetchPricing, hasConsumers]);

  const visiblePricingMap = dataOwnerScope === pricingScope ? pricingMap : {};
  const trusted = pricingScope !== null
    && dataOwnerScope === pricingScope
    && error === null
    && trustedUntil > Date.now();

  const getCost = useCallback(
    (featureCode: string): number | null => {
      const p = visiblePricingMap[featureCode];
      if (!p) return null;
      return p.cost_points;
    },
    [visiblePricingMap],
  );

  const getPricing = useCallback(
    (featureCode: string): PricingItem | null => visiblePricingMap[featureCode] ?? null,
    [visiblePricingMap],
  );

  const getTrustedCost = useCallback(
    (featureCode: string): number | null => {
      if (!trusted) return null;
      const pricing = visiblePricingMap[featureCode];
      if (!pricing?.is_active || !Number.isFinite(pricing.cost_points)) return null;
      return pricing.cost_points;
    },
    [trusted, visiblePricingMap],
  );

  return (
    <PricingContext.Provider
      value={{ pricingMap: visiblePricingMap, loading, error, getCost, getTrustedCost, getPricing, trusted, updatedAt, refresh: () => fetchPricing(true), retryNotBefore, activate }}
    >
      {children}
    </PricingContext.Provider>
  );
}

export function usePricing(): PricingContextType {
  const ctx = useContext(PricingContext);
  useEffect(() => ctx?.activate(), [ctx?.activate]);
  if (!ctx) {
    // Provider 未挂载时的 fallback（避免页面崩），返空 map + no-op
    return {
      pricingMap: {},
      loading: false,
      error: 'PricingProvider 未挂载',
      retryNotBefore: 0,
      getCost: () => null,
      getTrustedCost: () => null,
      getPricing: () => null,
      trusted: false,
      updatedAt: null,
      refresh: async () => {},
      activate: () => () => {},
    };
  }
  return ctx;
}

/**
 * 便捷 hook：一次取一个 feature 的 cost_points
 *
 * @example
 *   const cost = useFeatureCost('article_gen');  // → 390 / null
 */
export function useFeatureCost(featureCode: string): number | null {
  const { getTrustedCost } = usePricing();
  return getTrustedCost(featureCode);
}
