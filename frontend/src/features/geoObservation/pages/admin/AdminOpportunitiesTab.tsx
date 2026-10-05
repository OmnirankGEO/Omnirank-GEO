/**
 * 管理员 · 内容与媒体机会：机会主题、建议文体、需要证据、多平台支持度、稳定度。
 * - 不直接生成"排名软文"，不显示未经验证的"必上榜"。跳转现役写作/发布工作台。
 */

import { useState } from 'react';
import { FileText, PenLine } from 'lucide-react';
import { Panel, SectionHeader, Pill, LongText } from '../../components/shared';
import { GranularityFilter } from '../../components/GranularityFilter';
import { StabilityTag } from '../../components/badges';
import { ResourceStateView } from '../../components/states';
import { formatCount } from '../../format';
import { useObservationResource } from '../../hooks/useObservationResource';
import type { ObservationClient } from '../../client';
import type { Granularity } from '../../types';
import { resolveNavigate, type NavTarget } from '../../nav';

const CONTENT_TYPE_LABELS: Record<string, string> = {
  case_study: '项目案例',
  comparison_method: '对比与方法',
  qualification_evidence: '资质证据',
  faq: '常见问答',
  data_report: '数据报告',
};

export function AdminOpportunitiesTab({
  client,
  onNavigate,
}: {
  client: ObservationClient;
  onNavigate?: (target: NavTarget, ctx: Record<string, unknown>) => void;
}) {
  const nav = resolveNavigate(onNavigate);
  const [granularity, setGranularity] = useState<Granularity>('day');
  const res = useObservationResource(
    (signal) => client.adminContentOpportunities(granularity, signal),
    [granularity],
    true,
  );
  const opps = res.data?.items ?? [];

  const stateView = ResourceStateView({
    status: res.status,
    errorCode: res.errorCode,
    httpStatus: res.httpStatus,
    message: res.errorMessage,
    isEmpty: res.status === 'success' && opps.length === 0,
    onRetry: res.reload,
    emptyTitle: '暂无内容机会',
    emptyDesc: '积累更多跨平台观测后，这里会给出内容缺口建议。',
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <SectionHeader
          title="内容与媒体机会"
          hint="基于跨平台观测的内容缺口建议，只做建议，不自动生成或发布。"
        />
        <GranularityFilter value={granularity} onChange={setGranularity} />
      </div>
      {stateView ? (
        stateView
      ) : (
        <ul className="space-y-2.5">
          {opps.map((o) => (
            <li key={o.opportunity_id}>
              <Panel className="p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <Pill tone="muted">优先级 {o.priority}</Pill>
                  <span className="text-sm font-semibold text-foreground">{o.topic}</span>
                  <Pill tone="info">
                    <FileText className="h-3 w-3" />
                    {CONTENT_TYPE_LABELS[o.recommended_content_type] || o.recommended_content_type}
                  </Pill>
                  <StabilityTag status={o.stability} />
                </div>
                <LongText text={o.reason} clamp={2} className="mt-1.5 text-xs text-muted-foreground" />
                {o.recommended_evidence.length > 0 ? (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {o.recommended_evidence.map((ev, i) => (
                      <Pill key={i} tone="neutral">
                        {ev}
                      </Pill>
                    ))}
                  </div>
                ) : null}
                <div className="mt-3 flex items-center justify-between">
                  <span className="text-xs text-muted-foreground">本次分析样本数 {formatCount(o.sample_size)}</span>
                  <button
                    type="button"
                    onClick={() => nav('writing', { opportunityId: o.opportunity_id })}
                    className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                  >
                    <PenLine className="h-3.5 w-3.5" />
                    去写作工作台
                  </button>
                </div>
              </Panel>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
