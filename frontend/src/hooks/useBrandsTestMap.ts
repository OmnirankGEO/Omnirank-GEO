/**
 * useBrandsTestMap · 全站测试客户 is_test 映射 hook
 *
 * CTO-15.20 v2 · D.1:旧版 5 工作页测试客户隔离 toggle
 *   - 旧版 endpoint(/api/client-context/list / /api/monitoring/clients 等)不返 is_test
 *   - 此 hook 用 /api/m3/customers?include_test=true 拉一次 · 返 brand_id → is_test 映射
 *   - 调用方:`const { isTest } = useBrandsTestMap();` 然后 `clients.filter(c => includeTest || !isTest(c.brand_id))`
 *   - sessionStorage 缓存 60s 防多次调用
 *
 * 元指令:Refactor Not Rewrite · 后端 0 改动 · 复用 M3 BFF endpoint。
 */

import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';
import { useAuth } from '@/context/AuthContext';
import api from '@/lib/api';

const CACHE_KEY_PREFIX = 'omnirank_brands_test_map_v2:';
const CACHE_TTL_MS = 60 * 1000;
const memoryCache = new Map<string, CachedMap>();
const sharedRequests = new Map<string, { controller: AbortController; request: Promise<CachedMap> }>();
let activeBrandsAuthorizationScope: string | null = null;

function cacheKey(authorizationScope: string): string {
  return `${CACHE_KEY_PREFIX}${authorizationScope}`;
}

function readCached(authorizationScope: string | null): CachedMap | null {
  if (authorizationScope === null) return null;
  const memory = memoryCache.get(authorizationScope);
  if (memory && Date.now() - memory.ts <= CACHE_TTL_MS) return memory;
  try {
    const raw = sessionStorage.getItem(cacheKey(authorizationScope));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedMap;
    return Date.now() - parsed.ts <= CACHE_TTL_MS ? parsed : null;
  } catch {
    return null;
  }
}

interface CachedMap {
  ts: number;
  ids: number[];
}

function clearBrandsArtifactsExcept(authorizationScope: string | null): void {
  for (const key of memoryCache.keys()) {
    if (key !== authorizationScope) memoryCache.delete(key);
  }
  for (let index = sessionStorage.length - 1; index >= 0; index -= 1) {
    const key = sessionStorage.key(index);
    if (key?.startsWith(CACHE_KEY_PREFIX) && key !== (authorizationScope ? cacheKey(authorizationScope) : '')) {
      sessionStorage.removeItem(key);
    }
  }
}

function abortRequestsExcept(authorizationScope: string | null): void {
  for (const [ownerScope, entry] of sharedRequests) {
    if (ownerScope !== authorizationScope) {
      entry.controller.abort();
      sharedRequests.delete(ownerScope);
    }
  }
}

function requestMap(authorizationScope: string, force = false): Promise<CachedMap> {
  const cached = readCached(authorizationScope);
  if (!force && cached) return Promise.resolve(cached);
  const existing = sharedRequests.get(authorizationScope);
  if (existing) return existing.request;
  const controller = new AbortController();
  const request = api.get<{ customers?: { id: number; is_test?: boolean }[] }>(
    '/api/m3/customers?include_test=true',
    { signal: controller.signal },
  ).then((res) => {
    const ids = (res.data.customers || []).filter((customer) => customer.is_test).map((customer) => customer.id);
    const value = { ts: Date.now(), ids };
    if (activeBrandsAuthorizationScope === authorizationScope) {
      memoryCache.set(authorizationScope, value);
      try {
        sessionStorage.setItem(cacheKey(authorizationScope), JSON.stringify(value));
      } catch { /* ignore */ }
    }
    return value;
  }).finally(() => {
    if (sharedRequests.get(authorizationScope)?.request === request) sharedRequests.delete(authorizationScope);
  });
  sharedRequests.set(authorizationScope, { controller, request });
  return request;
}

interface BrandsTestMapResult {
  /** 给一个 brand_id 判断是否测试客户 · 默认 false(未知/老 brand fallback 真) */
  isTest: (brandId: number | null | undefined) => boolean;
  /** 测试客户总数 · 给 toggle hiddenCount 用 */
  testCount: number;
  /** loading · 已缓存时立即 false */
  loading: boolean;
  /** 强制 refresh 的 fn(给 toggle 切换时用 · 一般无需) */
  refresh: () => Promise<void>;
}

export function useBrandsTestMap(): BrandsTestMapResult {
  const { user, authorizationScope } = useAuth();
  const userId = user?.id ?? null;
  const cacheScope = userId === null ? null : authorizationScope;
  const [testIds, setTestIds] = useState<Set<number>>(() => {
    const cached = readCached(cacheScope);
    return new Set(cached?.ids || []);
  });
  const [loading, setLoading] = useState(testIds.size === 0);
  const ownerScopeRef = useRef<string | null>(cacheScope);
  const generationRef = useRef(0);

  useLayoutEffect(() => {
    generationRef.current += 1;
    activeBrandsAuthorizationScope = cacheScope;
    abortRequestsExcept(cacheScope);
    clearBrandsArtifactsExcept(cacheScope);
    ownerScopeRef.current = cacheScope;
    const cached = readCached(cacheScope);
    setTestIds(new Set(cached?.ids || []));
    setLoading(cacheScope !== null && !cached);
  }, [cacheScope]);

  const fetchMap = useCallback(async (force = false) => {
    if (cacheScope === null) return;
    const generation = generationRef.current;
    setLoading(true);
    try {
      // Responsive shell components can unmount before the monitoring chunk mounts.
      // Keep the same-account read alive and cache it; account changes abort it below.
      const value = await requestMap(cacheScope, force);
      if (generationRef.current !== generation || ownerScopeRef.current !== cacheScope) return;
      setTestIds(new Set(value.ids));
    } catch {
      if (generationRef.current !== generation || ownerScopeRef.current !== cacheScope) return;
      /* silent · 测试客户标记降级到全显 */
    } finally {
      if (generationRef.current === generation && ownerScopeRef.current === cacheScope) setLoading(false);
    }
  }, [cacheScope]);

  useEffect(() => {
    // 第一次/缓存过期才拉
    const cached = readCached(cacheScope);
    if (cached) {
      setLoading(false);
      return;
    }
    void fetchMap();
    return () => { generationRef.current += 1; };
  }, [cacheScope, fetchMap]);

  const isTest = useCallback(
    (brandId: number | null | undefined) => {
      if (brandId == null) return false;
      return testIds.has(brandId);
    },
    [testIds],
  );

  return {
    isTest,
    testCount: testIds.size,
    loading,
    refresh: () => fetchMap(true),
  };
}
