/**
 * 总览 Tab：先说"本周期结果分布"，再给趋势；口径断点显式标记。
 * - 不用单点大数字替代趋势；无真数据不画假折线。
 * - comparison_allowed=false 时绝不把变化画成"提升/下降"。
 * - 口径断点提示来自后端字段（summary.stability_explanation / trend.comparison_note），不硬编码。
 */

import { Panel, SectionHeader } from '../../components/shared';
import { OutcomeDistribution } from '../../components/OutcomeDistribution';
import { TrendChart } from '../../components/TrendChart';
import { ResourceStateView, ModelShiftBanner } from '../../components/states';
import { PAGE, METRIC_LABELS } from '../../copy';
import { useObservationResource } from '../../hooks/useObservationResource';
import type { ObservationClient } from '../../client';
import type { BrandSummary, Granularity } from '../../types';

export function OverviewTab({
  client,
  brandId,
  granularity,
  summary,
}: {
  client: ObservationClient;
  brandId: number;
  granularity: Granularity;
  summary: BrandSummary;
}) {
  const trend = useObservationResource(
    (signal) => client.brandTrend(brandId, granularity, signal),
    [brandId, granularity],
    true,
  );

  const points = trend.data?.points ?? [];

  const trendState = ResourceStateView({
    status: trend.status,
    errorCode: trend.errorCode,
    httpStatus: trend.httpStatus,
    message: trend.errorMessage,
    isEmpty: trend.status === 'success' && points.length === 0,
    onRetry: trend.reload,
    compact: true,
    emptyTitle: '暂无足够的趋势数据',
    emptyDesc: '继续积累样本后，这里会显示随时间的变化。',
  });

  return (
    <div className="space-y-4">
      {/* 口径断点 / 跨期不可比（均来自后端字段） */}
      {summary.summary.stability_status === 'shifted' ? (
        <ModelShiftBanner explanation={summary.summary.stability_explanation} />
      ) : null}
      {!summary.comparison.comparison_allowed ? (
        <div className="rounded-lg border border-border bg-muted/40 px-3.5 py-2.5 text-xs text-muted-foreground">
          {PAGE.overview.comparisonBlockedTitle}
          {summary.comparison.reason === 'model_shift' ? '（本周期部分平台模型升级，前后口径不一致）' : ''}
        </div>
      ) : null}

      {/* 结果分布 */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title={PAGE.overview.whatHappened} hint={PAGE.overview.distributionHint} />
        <div className="mt-4">
          <OutcomeDistribution outcomes={summary.outcomes} />
        </div>
      </Panel>

      {/* 趋势 */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader
          title={PAGE.overview.trendTitle}
          hint={
            trend.data?.comparison_note
              ? PAGE.overview.trendHint
              : `${METRIC_LABELS.presence_rate}与${METRIC_LABELS.explicit_recommendation_rate}随时间的变化。`
          }
        />
        <div className="mt-4">
          {trendState ? (
            trendState
          ) : (
            <>
              {trend.data?.comparison_note ? (
                <div className="mb-3">
                  <ModelShiftBanner explanation={trend.data.comparison_note} />
                </div>
              ) : null}
              <TrendChart points={points} />
            </>
          )}
        </div>
      </Panel>
    </div>
  );
}
