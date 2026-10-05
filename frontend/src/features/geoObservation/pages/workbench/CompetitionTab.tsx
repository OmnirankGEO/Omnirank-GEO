/**
 * 竞争格局 Tab（AI-3 无竞争端点，故用真实数据组合）：
 * - "样本内品牌提及占比" = 本品牌 share_of_voice（来自 summary，真实；不展示其他客户品牌名单）。
 * - "可能混淆的品牌" = 从真实问题列表里 outcome=entity_ambiguous 的条目派生，供人工确认。
 * - 不调用不存在的竞争端点，不编造竞品排名。
 */

import { AlertTriangle } from 'lucide-react';
import { Panel, SectionHeader, LongText } from '../../components/shared';
import { ResourceStateView, SkeletonRows } from '../../components/states';
import { PAGE } from '../../copy';
import { bpsToPercent } from '../../format';
import { useObservationResource } from '../../hooks/useObservationResource';
import type { ObservationClient } from '../../client';
import type { BrandSummary } from '../../types';

const SCAN_PAGE_SIZE = 100;

export function CompetitionTab({
  client,
  brandId,
  summary,
}: {
  client: ObservationClient;
  brandId: number;
  summary: BrandSummary;
}) {
  // 用真实问题列表派生易混淆品牌（entity_ambiguous），不发明竞争端点
  const res = useObservationResource(
    (signal) => client.brandQuestions(brandId, 1, SCAN_PAGE_SIZE, signal),
    [brandId],
    true,
  );

  const sov = summary.summary.share_of_voice_bps;
  const ambiguous = (res.data?.items ?? []).filter((q) => q.outcome === 'entity_ambiguous');
  // 只扫描最近 SCAN_PAGE_SIZE 条问题派生易混淆品牌；超过则明示口径，避免"看似全量"的误导。
  const total = res.data?.total ?? 0;
  const scanTruncated = total > SCAN_PAGE_SIZE;

  const ambiguousState = ResourceStateView({
    status: res.status,
    errorCode: res.errorCode,
    httpStatus: res.httpStatus,
    message: res.errorMessage,
    isEmpty: false,
    onRetry: res.reload,
    compact: true,
    loadingSkeleton: <SkeletonRows rows={2} />,
  });

  return (
    <div className="space-y-4">
      <SectionHeader title={PAGE.competition.title} hint={PAGE.competition.hint} />

      {/* 样本内品牌提及占比（本品牌，真实 summary 值） */}
      <Panel className="p-4 sm:p-5">
        <SectionHeader title={PAGE.competition.sovTitle} info={PAGE.competition.sovHint} />
        <div className="mt-3">
          {sov === null ? (
            <div className="rounded-lg border border-dashed border-border bg-muted/30 px-4 py-6 text-center">
              <p className="text-sm text-foreground">{PAGE.states.insufficient}</p>
              <p className="mt-1 text-xs text-muted-foreground">本周期样本还不足以给出稳定的品牌提及占比。</p>
            </div>
          ) : (
            <div className="flex items-baseline gap-2">
              <span className="text-3xl font-semibold tabular-nums text-foreground">{bpsToPercent(sov)}</span>
              <span className="text-xs text-muted-foreground">本品牌在样本内的提及占比</span>
            </div>
          )}
        </div>
      </Panel>

      {/* 可能混淆的品牌（人工确认） */}
      <Panel className="border-amber-500/30 p-4 sm:p-5">
        <div className="mb-2 flex items-center gap-1.5">
          <AlertTriangle className="h-4 w-4 text-amber-500" />
          <h4 className="text-sm font-semibold text-foreground">{PAGE.competition.ambiguousTitle}</h4>
        </div>
        <p className="mb-3 text-xs text-muted-foreground">{PAGE.competition.ambiguousHint}</p>
        {scanTruncated ? (
          <p className="mb-3 rounded-md bg-muted/40 px-2.5 py-1.5 text-xs text-muted-foreground">
            {PAGE.competition.scanLimitNote(SCAN_PAGE_SIZE, total)}
          </p>
        ) : null}
        {ambiguousState ? (
          ambiguousState
        ) : ambiguous.length === 0 ? (
          <p className="text-xs text-muted-foreground">{PAGE.competition.noAmbiguous}</p>
        ) : (
          <ul className="space-y-2">
            {ambiguous.map((q) => (
              <li key={q.observation_id} className="rounded-lg bg-muted/50 p-3">
                {q.matched_text ? (
                  <span className="text-sm font-medium text-foreground">命中：{q.matched_text}</span>
                ) : null}
                <LongText text={q.question} clamp={2} className="mt-0.5 text-xs" />
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
