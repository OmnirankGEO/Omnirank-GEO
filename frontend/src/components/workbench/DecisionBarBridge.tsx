/**
 * DecisionBarBridge · 桥接版 · 旧版工作面顶部用
 *
 * CTO-15.20 桥接重设计:
 *   旧版 /writing /publish /monitoring /reports /pricing /diagnosis/* 顶部加这个
 *   - 拉客户 snapshot
 *   - 显示精简版决策条(品牌名 + stage + 推荐下一步 + 主按钮 + 回客户工作台 button)
 *   - 主按钮跳到 stage 主按钮目标(可能是 M3 也可能是当前页 + 别的 brand · 此处仅展示 + 跳转)
 *   - 顶部固定 BackToClientButton(原 BackToM3Button,WO_260 改名改目标 → /my-clients/:id)
 *
 * 不依赖 M3 布局壳 上下文 · 可挂在旧版任何页面顶部。
 */

import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowRight, Sparkles } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { BackToClientButton } from './BackToClientButton';
import { StagePill, CompletenessBadge } from './parts';
import {
  m3Api,
  getPrimaryAction,
  type ClientWorkbenchSnapshot,
} from '@/services/m3';
import { cn } from '@/lib/utils';
import { useClientContext } from '@/context/ClientContext';

interface DecisionBarBridgeProps {
  brandId: number | string | null | undefined;
  className?: string;
  onSnapshot?: (snapshot: ClientWorkbenchSnapshot) => void;
  /**
   * 是否显示"回客户工作台"按钮 · 默认 false(CTO-15.21 v5 收口 · M3 已废弃;WO_260 目标改为 /my-clients/:id)
   * 仅 M3 内部测试页面可显式传 true · 用户可见区域不应露出。
   */
  showBackToClient?: boolean;
  showMissingState?: boolean;
}

export function DecisionBarBridge({
  brandId,
  className,
  onSnapshot,
  showBackToClient = false,
  showMissingState = false,
}: DecisionBarBridgeProps) {
  const navigate = useNavigate();
  const { clientContext } = useClientContext();
  const [snapshot, setSnapshot] = useState<ClientWorkbenchSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!brandId) {
      setLoading(false);
      return;
    }
    const id = typeof brandId === 'string' ? Number.parseInt(brandId, 10) : brandId;
    if (!Number.isFinite(id) || id <= 0) {
      setLoading(false);
      return;
    }
    setLoading(true);
    let cancelled = false;
    const reusableContext = clientContext?.brand.id === id ? clientContext : null;
    m3Api
      .getClientWorkbench(id, reusableContext)
      .then((s) => {
        if (cancelled) return;
        setSnapshot(s);
        onSnapshot?.(s);
        setError(null);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        const msg = e instanceof Error ? e.message : '加载失败';
        setError(msg);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [brandId, clientContext, onSnapshot]);

  if (!brandId) {
    if (!showMissingState) return null;
    return (
      <Card className={cn('min-h-[132px] border-primary/20 bg-primary/5 sm:min-h-0', className)}>
        <CardContent className="grid gap-3 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)_auto] sm:items-center sm:p-4">
          <span className="text-sm font-semibold text-foreground">尚未选择客户</span>
          <span className="text-xs text-muted-foreground">选择客户后显示当前阶段和下一步建议。</span>
          <Button size="sm" disabled className="min-h-[44px] w-full sm:w-36">请先选择客户</Button>
        </CardContent>
      </Card>
    );
  }

  if (loading) {
    return (
      <Card className={cn('min-h-[132px] border-primary/20 bg-primary/5 sm:min-h-0', className)}>
      <CardContent className="grid gap-3 p-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.2fr)_auto] sm:items-center sm:p-4">
          <Skeleton className="h-5 w-40 max-w-full" />
          <Skeleton className="h-5 w-full max-w-[280px]" />
          <Skeleton className="h-11 w-full sm:w-36" />
      </CardContent>
      </Card>
    );
  }

  if (error || !snapshot) {
    return (
      <Card className={cn('min-h-[132px] border-muted bg-muted/20 sm:min-h-0', className)}>
        <CardContent className="flex flex-col gap-3 p-3 sm:flex-row sm:items-center sm:justify-between sm:p-4">
          <span className="text-xs text-muted-foreground">这个客户的下一步建议暂时没拿到,不影响你编辑资料。</span>
          {showBackToClient && <BackToClientButton brandId={brandId} />}
        </CardContent>
      </Card>
    );
  }

  const action = getPrimaryAction(snapshot.stage, {
    brandId: snapshot.id,
    brandName: snapshot.name,
    diagnosisId: snapshot.diagnosis?.id,
    quoteId: snapshot.quote?.id,
    quoteStatus: snapshot.quote?.status,
    serviceStatus: snapshot.quote?.service_status,
    monitoringSummary: snapshot.monitoring?.summary ?? null,
    hasReportThisMonth: false,
  });

  const Icon = action.icon;
  const handlePrimaryClick = () => {
    if (typeof window !== 'undefined') {
      const w = window as unknown as { __m3Analytics?: { track?: (e: string, p?: unknown) => void } };
      w.__m3Analytics?.track?.('bridge_primary_click', {
        brand_id: snapshot.id,
        stage: snapshot.stage,
        action_key: action.actionKey,
      });
    }
    if (action.target) navigate(action.target);
  };

  return (
    /* CTO-15.23 Phase 2.2 · 老板 5/22 报"顶部通知 mobile 溢出"修
     * max-w-full + overflow-hidden 防 brand name / action.title 中文长串撑出 viewport */
    <Card className={cn('min-h-[132px] max-w-full overflow-hidden border-primary/20 bg-primary/5 sm:min-h-0', className)}>
      <CardContent className="grid gap-3 p-3 sm:p-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.25fr)_auto] lg:items-center">
        {/* 品牌信息 · min-w-0 + max-w-full 保 mobile 单列 truncate 起作用 */}
        <div className="flex min-w-0 max-w-full flex-wrap items-center gap-2">
          <span className="min-w-0 max-w-full truncate text-sm font-semibold" title={snapshot.name}>
            {snapshot.name}
          </span>
          <StagePill stage={snapshot.stage} />
          {typeof snapshot.completeness === 'number' && (
            /*
             * 🔴 [#199] 改前这里**两个都没传**:徽章上只有「85 /100」,
             *    什么分、缺哪几项全在 hover 的 title 里,手机上根本看不到。
             * 🔴 缺项**不是新造的字段**:`completenessDetail` 就是
             *    `/api/brands/{id}/completeness` 的回包(`BrandCompleteness.missing`),
             *    和阻断 Dialog 用的是同一个来源 —— 所以本单**不需要** 199-c1 那条后端补项。
             */
            <CompletenessBadge
              score={snapshot.completeness}
              missing={snapshot.completenessDetail?.missing}
              brandId={snapshot.id}
              showLabel
            />
          )}
        </div>

        {/* 推荐下一步 · 防 action.title 长字符串溢出 · 5/22 修 */}
        <div className="flex min-w-0 max-w-full items-center gap-1.5 text-xs text-muted-foreground">
          <Sparkles className="h-3.5 w-3.5 text-primary shrink-0" aria-hidden />
          <span className="shrink-0">下一步:</span>
          <span className="min-w-0 text-foreground font-medium truncate" title={action.title}>{action.title}</span>
          {action.subtitle && (
            <span className="hidden md:inline text-[11px] truncate shrink min-w-0" title={action.subtitle}>· {action.subtitle}</span>
          )}
        </div>

        {/* 主按钮 + 回客户工作台 · 按钮 label 也防长字符串撑爆 */}
        <div className="flex min-w-0 max-w-full items-center gap-2 lg:justify-end">
          {action.target && (
            <Button
              size="sm"
              onClick={handlePrimaryClick}
              className="min-h-[44px] w-full max-w-full gap-1.5 overflow-hidden sm:w-auto"
              aria-label={action.title}
            >
              <Icon className="h-4 w-4 shrink-0" aria-hidden />
              <span className="min-w-0 truncate">{action.title}</span>
              {action.costPoints !== undefined && (
                <span className="text-[10px] opacity-80 ml-1 shrink-0">
                  消耗 {action.costPoints} 算力
                </span>
              )}
              <ArrowRight className="h-3.5 w-3.5 shrink-0" aria-hidden />
            </Button>
          )}
          {showBackToClient && <BackToClientButton brandId={snapshot.id} />}
        </div>
      </CardContent>
    </Card>
  );
}
