/**
 * 管理员 · 行业基线（AI-3 /industries/{key}/baseline，真实公共端点）。
 * - k 匿名不满足时后端返回 status=insufficient_samples（HTTP 200），前端据 status 显示样本不足，不 fallback 全行业。
 * - 输入真实行业键 + 粒度查询；不发明"行业趋势"端点。
 */

import { useState } from 'react';
import { Search } from 'lucide-react';
import { Panel, SectionHeader, MetricTile } from '../../components/shared';
import { GranularityFilter } from '../../components/GranularityFilter';
import { StabilityTag } from '../../components/badges';
import { ResourceStateView, InsufficientBlock } from '../../components/states';
import { METRIC_LABELS } from '../../copy';
import { bpsToPercent, formatCount } from '../../format';
import { useObservationResource } from '../../hooks/useObservationResource';
import type { ObservationClient } from '../../client';
import type { Granularity } from '../../types';

export function AdminIndustryTab({ client }: { client: ObservationClient }) {
  const [draft, setDraft] = useState('');
  const [industry, setIndustry] = useState<string | null>(null);
  const [granularity, setGranularity] = useState<Granularity>('day');

  const res = useObservationResource(
    (signal) => client.industryBaseline(industry as string, granularity, signal),
    [industry, granularity],
    industry !== null,
  );
  const resp = res.data;

  const stateView = ResourceStateView({
    status: res.status,
    errorCode: res.errorCode,
    httpStatus: res.httpStatus,
    message: res.errorMessage,
    isEmpty: false,
    onRetry: res.reload,
    compact: true,
  });

  return (
    <div className="space-y-4">
      <SectionHeader
        title="行业基线"
        hint="匿名行业对照。只有达到匿名门槛的行业才展示，样本不足时明确说明，不给结论。"
      />

      <div className="flex flex-wrap items-center gap-2">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setIndustry(draft.trim() || null);
          }}
          className="flex flex-1 flex-wrap items-center gap-2"
        >
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="输入行业键，如 elevator_service"
            aria-label="行业键"
            className="min-h-[36px] w-full min-w-0 flex-1 rounded-lg border border-border bg-card px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground/60 focus:border-brand focus:outline-none focus:ring-2 focus:ring-brand/20 sm:w-64 sm:flex-none"
          />
          <button
            type="submit"
            disabled={draft.trim().length === 0}
            className="inline-flex min-h-[36px] items-center gap-1.5 rounded-lg bg-primary px-3.5 py-2 text-xs font-semibold text-primary-foreground transition hover:opacity-90 disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
          >
            <Search className="h-3.5 w-3.5" />
            查询
          </button>
        </form>
        <GranularityFilter value={granularity} onChange={setGranularity} />
      </div>

      {industry === null ? (
        <div className="rounded-lg border border-dashed border-border bg-muted/30 px-4 py-10 text-center text-sm text-muted-foreground">
          输入行业键后查询该行业的匿名基线。
        </div>
      ) : stateView ? (
        stateView
      ) : resp && resp.status === 'insufficient_samples' ? (
        <InsufficientBlock compact />
      ) : resp && resp.status === 'ok' && resp.baseline ? (
        <Panel className="p-4 sm:p-5">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <h4 className="text-sm font-semibold text-foreground">{resp.industry_key}</h4>
            <span className="text-xs text-muted-foreground">{resp.sample_scope}</span>
          </div>
          <div className="mt-3 grid grid-cols-2 gap-3 sm:grid-cols-4">
            <MetricTile label={METRIC_LABELS.valid_observations} value={formatCount(resp.baseline.valid_observations)} />
            <MetricTile label={METRIC_LABELS.presence_rate} value={bpsToPercent(resp.baseline.presence_rate_bps)} />
            <MetricTile
              label={METRIC_LABELS.explicit_recommendation_rate}
              value={bpsToPercent(resp.baseline.explicit_recommendation_rate_bps)}
            />
            <MetricTile
              label={METRIC_LABELS.evidence_coverage_rate}
              value={bpsToPercent(resp.baseline.evidence_coverage_rate_bps)}
            />
          </div>
          <div className="mt-3">
            <StabilityTag status={resp.baseline.stability_status} />
          </div>
        </Panel>
      ) : null}
    </div>
  );
}
