/** Approved display-brand hook. Internal relationship and authorization fields never enter React state. */
import { useEffect, useState } from 'react';
import { useLocation } from 'react-router-dom';
import api from '@/lib/api';
import { useAuth } from '@/context/AuthContext';

export interface WhitelabelBrand {
  company_name: string;
  product_name?: string;
  logo_url?: string;
  favicon_url?: string;
  slogan?: string;
  contact_name?: string;
  contact_phone?: string;
  contact_wechat?: string;
  contact_email?: string;
  brand_color?: string;
}

export interface ResolvedBrand extends WhitelabelBrand {}

export type BrandingSurface = 'customer' | 'agent' | 'admin';
export type DisplayScope = 'platform' | 'approved_whitelabel';

const OMNIRANK_BRAND: ResolvedBrand = {
  company_name: 'OmniRank · 全域上榜',
  logo_url: '/logo-192.png',
};

export function resolveSurfaceFromPath(pathname: string): BrandingSurface {
  const p = pathname || '';
  if (p === '/admin' || p.startsWith('/admin/')) return 'admin';
  // ⚠️ /s 前缀歧义（板块 C 2026-07-22 记录）：/s/* 是社媒 C 端客户页（SEnd），
  // 但历史上也承载过代理沉浸任务页（Layout agent-back variant）。
  // 当前判定：/s 不在 customer 前缀表 → 落 'agent'（走带鉴权的 /api/referral/whitelabel，
  // 匿名 C 端用户 401 → hook catch → fail-closed 平台）。C 端客户白标页面统一走
  // /public|/q|/m|/intake|/portal 前缀（customer surface · 公开端点 quote_id 解析）。
  // 若未来 /s 需要客户白标，必须显式加前缀并评估 token 上下文，不能模糊落入 agent。
  if (/^\/(public|q|m|intake|portal)(\/|$)/.test(p)) return 'customer';
  return 'agent';
}

/** @deprecated Kept for older rendering call sites; the server already applies approval policy. */
export function resolveBrand(
  _legacyLevel: number,
  whitelabel: WhitelabelBrand | null | undefined,
): ResolvedBrand {
  return whitelabel?.company_name ? whitelabel as ResolvedBrand : OMNIRANK_BRAND;
}

interface UseBrandingArgs {
  /** Deprecated compatibility input. Agent branding always resolves the current authenticated account. */
  userId?: number | string | null;
  quoteId?: number | string | null;
  surface?: BrandingSurface;
  inlineWhitelabel?: WhitelabelBrand | null;
}

interface BrandingResponse {
  display_scope?: DisplayScope;
  brand?: WhitelabelBrand | null;
  whitelabel?: WhitelabelBrand | null;
  data?: Record<string, unknown>;
}

const BRANDING_CACHE_FRESH_MS = 30 * 1000;
// 缓存键含 surface 维度（customer:{quoteId} / agent:{scope} · 合同 §4.2-C6 (owner, surface, version)）。
// 主动失效：任何 /whitelabel 写操作（代理保存 / admin 授权/暂停/撤权）触发
// 'omnirank-api-mutated' 事件 → brandingCache.clear()（见下方监听器）；
// brand_version 由后端 D1 版本化提供，经响应下发供组件展示与未来键维度扩展。
const brandingCache = new Map<string, { value: BrandingResponse; updatedAt: number }>();
const brandingRequests = new Map<string, Promise<BrandingResponse>>();

function requestBranding(key: string, url: string, params?: Record<string, unknown>): Promise<BrandingResponse> {
  const cached = brandingCache.get(key);
  if (cached && Date.now() - cached.updatedAt < BRANDING_CACHE_FRESH_MS) {
    return Promise.resolve(cached.value);
  }
  const existing = brandingRequests.get(key);
  if (existing) return existing;
  const request = api.get<BrandingResponse>(url, { params }).then((response) => {
    brandingCache.set(key, { value: response.data || {}, updatedAt: Date.now() });
    if (brandingCache.size > 32) brandingCache.delete(brandingCache.keys().next().value as string);
    return response.data || {};
  }).finally(() => {
    if (brandingRequests.get(key) === request) brandingRequests.delete(key);
  });
  brandingRequests.set(key, request);
  return request;
}

if (typeof window !== 'undefined') {
  window.addEventListener('omnirank-api-mutated', (event) => {
    const url = String((event as CustomEvent<{ url?: string }>).detail?.url || '');
    if (url.includes('/whitelabel')) brandingCache.clear();
  });
}

export function useBranding(args: UseBrandingArgs = {}) {
  const { quoteId } = args;
  const hasInline = args.inlineWhitelabel !== undefined;
  const location = useLocation();
  const { authorizationScope } = useAuth();
  const surface = args.surface || resolveSurfaceFromPath(location.pathname);
  const [brand, setBrand] = useState<ResolvedBrand>(OMNIRANK_BRAND);
  const [raw, setRaw] = useState<WhitelabelBrand | null>(null);
  const [displayScope, setDisplayScope] = useState<DisplayScope>('platform');
  // D2（Owner 2026-07-22）：后台换肤权威判定 · 后端 backoffice_brand_allowed 下发 · fail-closed false
  const [backofficeBrandAllowed, setBackofficeBrandAllowed] = useState(false);
  const [brandVersion, setBrandVersion] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const applyPlatform = () => {
      if (cancelled) return;
      setBrand(OMNIRANK_BRAND);
      setRaw(null);
      setDisplayScope('platform');
      setBackofficeBrandAllowed(false);
      setBrandVersion(null);
      setLoading(false);
    };
    const applyApproved = (candidate: WhitelabelBrand | null | undefined, backofficeAllowed = false) => {
      if (cancelled) return;
      if (!candidate?.company_name || !candidate.logo_url) {
        applyPlatform();
        return;
      }
      setBrand(candidate as ResolvedBrand);
      setRaw(candidate);
      setDisplayScope('approved_whitelabel');
      setBackofficeBrandAllowed(backofficeAllowed);
      setLoading(false);
    };

    setLoading(true);
    if (surface === 'admin') {
      applyPlatform();
      return () => { cancelled = true; };
    }
    if (hasInline) {
      applyApproved(args.inlineWhitelabel);
      return () => { cancelled = true; };
    }

    const request = surface === 'customer'
      ? (quoteId
          ? requestBranding(`customer:${quoteId}`, '/api/public/whitelabel', { quote_id: quoteId, surface: 'customer' })
          : null)
      : requestBranding(`agent:${authorizationScope}`, '/api/referral/whitelabel');
    if (!request) {
      applyPlatform();
      return () => { cancelled = true; };
    }

    request
      .then((body) => {
        if (cancelled) return;
        if (surface === 'agent') {
          const data = (body.data || {}) as Record<string, unknown>;
          // D2（Owner 2026-07-22）：后台换肤只信后端 backoffice_brand_allowed 权威判定
          // （独立授权位 + status 有效 + 总闸）；缺字段/非 true → fail-closed 平台。
          const allowed = data.backoffice_brand_allowed === true;
          if (typeof data.brand_version === 'number') setBrandVersion(data.brand_version as number);
          if (allowed) {
            applyApproved(data as unknown as WhitelabelBrand, true);
          } else {
            applyPlatform();
          }
          return;
        }
        if (body.display_scope === 'approved_whitelabel') {
          applyApproved(body.brand || body.whitelabel);
        } else {
          applyPlatform();
        }
      })
      .catch(applyPlatform);

    return () => { cancelled = true; };
  }, [authorizationScope, quoteId, surface, hasInline, args.inlineWhitelabel, args.userId]);

  return { brand, raw, displayScope, surface, loading, backofficeBrandAllowed, brandVersion };
}

/** @deprecated Use useBranding. */
export function useWhitelabel(
  _userId: number | string | null | undefined,
  quoteId?: number | string | null,
) {
  return useBranding({ quoteId, surface: 'customer' });
}
