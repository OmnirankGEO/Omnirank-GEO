import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useAuth } from '@/context/AuthContext';
import { OrganizationApiError, organizationRequest, type OrganizationOverview } from '@/lib/organizationApi';

interface OrganizationContextValue {
  overview: OrganizationOverview | null;
  loading: boolean;
  error: string | null;
  isOwner: boolean;
  isMember: boolean;
  refresh: () => Promise<void>;
}

const OrganizationContext = createContext<OrganizationContextValue | null>(null);

export function OrganizationProvider({ children }: { children: ReactNode }) {
  const { isAuthenticated, user } = useAuth();
  const [overview, setOverview] = useState<OrganizationOverview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const requestGeneration = useRef(0);
  const activePrincipal = useRef<number | null>(user?.id ?? null);
  activePrincipal.current = user?.id ?? null;
  /** 当前 `overview` 是**哪个账号**的。用来区分「换人了」和「同一个人刷新」。 */
  const loadedPrincipal = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    const generation = ++requestGeneration.current;
    const requestedPrincipal = user?.id ?? null;
    if (!isAuthenticated || !user) {
      loadedPrincipal.current = null;
      setOverview(null);
      setError(null);
      setLoading(false);
      return;
    }
    // Never render another principal's cached organization while a new
    // session is loading. A delayed response is accepted only if both its
    // request generation and initiating principal are still current.
    //
    // [§1-2 真因修复 2026-08-17] 原来这里**无条件** `setOverview(null)`,后果远超防串号:
    //   `OrganizationCenter` 的加载守卫是 `if (overviewLoading && !overview) → 骨架屏`,
    //   所以每一次刷新(包括每一次 `run()` 成功后的自动刷新)都会让整页退回骨架屏,
    //   `OwnerOperationsPanel` **整个卸载**,它的 `lastInviteUrl` 局部 state 随之丢掉。
    //   这就是「96198f54 说做了邀请链接可见化、实测看不见」的真因:面板确实渲染了,
    //   但生成/重发成功后紧跟的刷新把它连组件一起拆了,肉眼只看到一闪。
    //   现在只在**换了账号**时清空(防串号的原意保持不变);同一账号刷新保留旧数据,
    //   由 `loading` 标志驱动局部的转圈,不再整页重挂。
    if (loadedPrincipal.current !== requestedPrincipal) {
      setOverview(null);
    }
    setError(null);
    setLoading(true);
    try {
      const data = await organizationRequest<OrganizationOverview>('/overview');
      if (generation !== requestGeneration.current || activePrincipal.current !== requestedPrincipal) return;
      // [P0 2026-08-17] 「没有团队」现在是 200 + `has_organization:false`,不再是 404。
      // 判空看 `id` 而不是只看 `has_organization`,这样旧后端(不带该字段)也不会错认。
      const hasOrganization = data?.has_organization !== false && Number(data?.id) > 0;
      loadedPrincipal.current = hasOrganization ? requestedPrincipal : null;
      setOverview(hasOrganization ? data : null);
      setError(null);
    } catch (caught) {
      if (generation !== requestGeneration.current || activePrincipal.current !== requestedPrincipal) return;
      loadedPrincipal.current = null;
      setOverview(null);
      if (caught instanceof OrganizationApiError && caught.status === 404) {
        // 灰度兼容:旧后端仍以 404 表示「没有团队」。这不是错误,不上报。
        setError(null);
      } else {
        setError(caught instanceof Error ? caught.message : '团队信息加载失败，请刷新页面重试');
      }
    } finally {
      if (generation === requestGeneration.current && activePrincipal.current === requestedPrincipal) {
        setLoading(false);
      }
    }
  }, [isAuthenticated, user]);

  useEffect(() => {
    void refresh();
    return () => { requestGeneration.current += 1; };
  }, [refresh]);
  useEffect(() => {
    const handleAuthority = () => { void refresh(); };
    window.addEventListener('organization-authority-changed', handleAuthority);
    return () => window.removeEventListener('organization-authority-changed', handleAuthority);
  }, [refresh]);

  const value = useMemo<OrganizationContextValue>(() => ({
    overview,
    loading,
    error,
    isOwner: overview?.identity?.actor_kind === 'owner',
    isMember: overview?.identity?.actor_kind === 'member',
    refresh,
  }), [overview, loading, error, refresh]);

  return <OrganizationContext.Provider value={value}>{children}</OrganizationContext.Provider>;
}

export function useOrganization(): OrganizationContextValue {
  const value = useContext(OrganizationContext);
  if (!value) throw new Error('useOrganization must be used within OrganizationProvider');
  return value;
}
