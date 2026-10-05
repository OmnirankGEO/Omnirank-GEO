/**
 * BridgeBanner · 旧版工作页顶部桥接 banner
 *
 * CTO-15.20 桥接重设计 · C.2 (v2 修正):旧版 5+1 工作页顶部统一接入
 *   /diagnosis/new · /diagnosis/report/:id · /pricing · /writing · /publish · /reports · /monitoring
 *
 * brandId 来源(优先级):
 *   1. 显式 prop  · 用于诊断报告页(从 detail.brand_id 拿) · 监测页(从 ClientContext 拿)
 *   2. URL ?brand_id=X 自动读 · 用于 M3 客户工作台 跳过来时
 * 没有 brandId 则不渲染。
 * 内部用 DecisionBarBridge 拉真值 + 显示主按钮 + 回 M3 button
 */

import { useSearchParams } from 'react-router-dom';
import { DecisionBarBridge } from './DecisionBarBridge';
import { cn } from '@/lib/utils';

interface BridgeBannerProps {
  className?: string;
  /** 显式传 brandId · 优先于 URL · 用于 detail.brand_id / ClientContext 来源场景 */
  brandId?: number | string | null;
  /** Keep a stable, explicit empty state while no client is selected. */
  showMissingState?: boolean;
}

export function BridgeBanner({ className, brandId: propBrandId, showMissingState = false }: BridgeBannerProps) {
  const [params] = useSearchParams();

  // 优先 prop · fallback URL
  let id: number | null = null;
  if (propBrandId != null) {
    const n = typeof propBrandId === 'string' ? parseInt(propBrandId, 10) : propBrandId;
    if (Number.isFinite(n) && n > 0) id = n;
  }
  if (id === null) {
    const raw = params.get('brand_id');
    if (raw) {
      const n = parseInt(raw, 10);
      if (Number.isFinite(n) && n > 0) id = n;
    }
  }
  if (id === null && !showMissingState) return null;

  return (
    /* CTO-15.23 Phase 2.2 · 老板 5/22 报"顶部通知 mobile 溢出"修
     * max-w-full overflow-hidden 防内部 truncate/flex 撑出 viewport */
    <div className={cn('mb-3 max-w-full overflow-hidden', className)}>
      <DecisionBarBridge brandId={id} showMissingState={showMissingState} />
    </div>
  );
}
