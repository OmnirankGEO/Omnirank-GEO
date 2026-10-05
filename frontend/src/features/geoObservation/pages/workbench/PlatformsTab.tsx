/**
 * 平台表现 Tab：豆包/千问/DeepSeek/元宝按产品展示；每个平台给样本量、比例、稳定度、更新时间。
 * - section 六：用户端只显示平台名 + 指标 + 样本量 + 稳定性，不展示 surface 实现细节、不显示"官方通道"徽标。
 * - Kimi 等历史平台默认折叠进"历史平台"，不占首屏。
 * - 移动端为可展开信息行，不横向压表格。
 */

import { useState } from 'react';
import { ChevronDown, History } from 'lucide-react';
import { Panel, SectionHeader } from '../../components/shared';
import { StabilityTag } from '../../components/badges';
import { ResourceStateView } from '../../components/states';
import { PAGE, METRIC_LABELS } from '../../copy';
import { bpsToPercent, formatCount, formatUpdatedAt } from '../../format';
import { useObservationResource } from '../../hooks/useObservationResource';
import type { ObservationClient } from '../../client';
import type { Granularity } from '../../types';

export function PlatformsTab({
  client,
  brandId,
  granularity,
}: {
  client: ObservationClient;
  brandId: number;
  granularity: Granularity;
}) {
  const res = useObservationResource(
    (signal) => client.brandPlatforms(brandId, granularity, signal),
    [brandId, granularity],
    true,
  );
  const [historyOpen, setHistoryOpen] = useState(false);

  const data = res.data;
  const stateView = ResourceStateView({
    status: res.status,
    errorCode: res.errorCode,
    httpStatus: res.httpStatus,
    message: res.errorMessage,
    isEmpty: res.status === 'success' && (data?.items.length ?? 0) === 0,
    onRetry: res.reload,
    emptyTitle: '本周期还没有平台数据',
    emptyDesc: '跑一次监测后，各平台的表现会显示在这里。',
  });

  if (stateView) return <>{stateView}</>;
  if (!data) return null;

  return (
    <div className="space-y-4">
      <SectionHeader title={PAGE.platforms.title} hint={PAGE.platforms.hint} />

      <div className="grid grid-cols-1 gap-3 md:grid-cols-2">
        {data.items.map((p) => (
          <Panel key={p.platform_key} className="p-4">
            <div className="flex flex-wrap items-start justify-between gap-x-2 gap-y-1.5">
              <h4 className="text-sm font-semibold text-foreground">{p.display_name}</h4>
              <StabilityTag status={p.stability_status} />
            </div>
            <div className="mt-3 grid grid-cols-3 gap-2">
              <MiniStat label={METRIC_LABELS.valid_observations} value={formatCount(p.valid_observations)} />
              <MiniStat label={METRIC_LABELS.presence_rate} value={bpsToPercent(p.presence_rate_bps)} />
              <MiniStat
                label={METRIC_LABELS.explicit_recommendation_rate}
                value={bpsToPercent(p.explicit_recommendation_rate_bps)}
              />
            </div>
            <p className="mt-2.5 text-[11px] text-muted-foreground">
              {METRIC_LABELS.data_updated_at} {formatUpdatedAt(p.updated_at)}
            </p>
          </Panel>
        ))}
      </div>

      {/* 通用采集说明（section 六.7）——不暴露具体代理/官方实现细节 */}
      <p className="text-[11px] leading-relaxed text-muted-foreground">{PAGE.platforms.collectionNote}</p>

      {/* 历史平台（默认折叠） */}
      {data.historical_platforms.length > 0 ? (
        <Panel className="overflow-hidden">
          <button
            type="button"
            aria-expanded={historyOpen}
            onClick={() => setHistoryOpen((v) => !v)}
            className="flex w-full items-center justify-between px-4 py-3 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
          >
            <span className="flex items-center gap-2 text-sm font-medium text-foreground">
              <History className="h-4 w-4 text-muted-foreground" />
              {PAGE.platforms.historical}
              <span className="text-xs text-muted-foreground">（{data.historical_platforms.length}）</span>
            </span>
            <ChevronDown className={`h-4 w-4 text-muted-foreground transition ${historyOpen ? 'rotate-180' : ''}`} />
          </button>
          {historyOpen ? (
            <div className="border-t border-border px-4 py-3">
              <p className="mb-2 text-xs text-muted-foreground">{PAGE.platforms.historicalHint}</p>
              <ul className="space-y-2">
                {data.historical_platforms.map((h) => (
                  <li
                    key={h.platform_key}
                    className="flex items-center justify-between rounded-lg bg-muted/50 px-3 py-2 text-sm"
                  >
                    <span className="text-foreground">{h.display_name}</span>
                    <span className="text-xs text-muted-foreground">
                      最近记录 {formatUpdatedAt(h.last_observed_at)}
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </Panel>
      ) : null}
    </div>
  );
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg bg-muted/50 p-2.5">
      {/* 标签整词换行显示（不截断），窄屏触摸可读，无需 hover title */}
      <div className="text-[11px] leading-tight text-muted-foreground break-words">{label}</div>
      <div className="mt-0.5 text-base font-semibold tabular-nums text-foreground">{value}</div>
    </div>
  );
}
