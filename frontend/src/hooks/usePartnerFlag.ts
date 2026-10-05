/**
 * usePartnerFlag — 读取后端 PARTNER_APPLY_ENABLED feature flag
 *
 * 后端 /api/partner/flag（公开端点，免鉴权）返回 {enabled: boolean}
 * - enabled=true  : v1.1 审核制已开, UI 应隐藏老"成为代理"引导, 展示"合作伙伴计划"入口
 * - enabled=false : 保持老逻辑 (消费自动升级), UI 维持现状
 *
 * v1.1-law-fix 修订:
 *   - 之前用 /api/partner/about 需要登录, 401/token 失效会降级到 false → 老代理入口泄露
 *   - 现在用 /api/partner/flag 公开端点, 独立于登录态
 *   - 缓存策略: **仅缓存 enabled=true** (5 分钟), false 不缓存每次重探
 *     → 部署开 flag 后, UI 立即切换; 关 flag 后, 缓存过期自动回落
 *   - sessionStorage key 加部署版本号, 后续部署变更可强制刷缓存
 */

import { useEffect, useState } from 'react';

const CACHE_KEY = 'omnirank_partner_flag_v2';
const CACHE_TTL_MS = 5 * 60 * 1000; // 5 分钟

type CachedFlag = {
  enabled: boolean;
  cachedAt: number;
};

function readCache(): CachedFlag | null {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as CachedFlag;
    if (!parsed || typeof parsed.enabled !== 'boolean') return null;
    if (Date.now() - (parsed.cachedAt || 0) > CACHE_TTL_MS) return null;
    // 仅接受 true 值缓存 (防御: 历史可能写入过 false 值)
    if (parsed.enabled !== true) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeCacheTrueOnly(enabled: boolean) {
  // 仅缓存 true；false 不缓存, 下次 mount 继续探
  if (enabled !== true) return;
  try {
    sessionStorage.setItem(
      CACHE_KEY,
      JSON.stringify({ enabled: true, cachedAt: Date.now() } as CachedFlag),
    );
  } catch {
    // 忽略写入失败 (隐私模式 / 磁盘满等)
  }
}

export interface PartnerFlagState {
  /** feature flag 是否开启 */
  enabled: boolean;
  /** 是否仍在首次加载（无缓存时为 true，缓存命中后立即为 false） */
  loading: boolean;
}

/**
 * 读取 PARTNER_APPLY_ENABLED flag
 *
 * - 初次渲染有缓存（true）→ loading=false, enabled=true, 后台不再重探
 * - 初次渲染无缓存 → loading=true, enabled=false, 立刻 fetch /api/partner/flag
 * - fetch 成功 enabled=true → 更新 state + 缓存
 * - fetch 失败 / enabled=false → 不缓存, 保持 enabled=false
 */
export function usePartnerFlag(): PartnerFlagState {
  const cached = readCache();
  const [enabled, setEnabled] = useState<boolean>(cached?.enabled === true);
  const [loading, setLoading] = useState<boolean>(!cached);

  useEffect(() => {
    // 缓存已命中 (true) 则不再重探
    if (cached?.enabled === true) return;

    let cancelled = false;
    (async () => {
      try {
        // /api/partner/flag 是公开端点, 免鉴权; 但显式不附 Authorization 避免不必要 401 日志
        const resp = await fetch('/api/partner/flag', {
          method: 'GET',
          credentials: 'same-origin',
        });
        if (!resp.ok) {
          if (!cancelled) setLoading(false);
          return;
        }
        const data = await resp.json();
        const next = Boolean(data?.enabled);
        if (next) writeCacheTrueOnly(true);
        if (!cancelled) {
          setEnabled(next);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
    // 仅首次 mount 拉一次
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { enabled, loading };
}
