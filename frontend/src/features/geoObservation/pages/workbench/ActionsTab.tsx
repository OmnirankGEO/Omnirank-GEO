/**
 * 行动建议 Tab：按优先级给建议，每条说明"为什么"。
 * - 只跳转现役写作/发布入口并带安全上下文，绝不自动执行/扣费。
 * - next_actions（本品牌当下）+ 内容机会（跨平台观测缺口）。
 */

import { PenLine, Send, Eye, ShieldQuestion, FileText, CheckCircle2 } from 'lucide-react';
import { Panel, SectionHeader, Pill, LongText } from '../../components/shared';
import { StabilityTag } from '../../components/badges';
import { ResourceStateView } from '../../components/states';
import { PAGE } from '../../copy';
import { formatCount } from '../../format';
import { useObservationResource } from '../../hooks/useObservationResource';
import { resolveNavigate, type NavTarget } from '../../nav';
import type { ObservationClient } from '../../client';
import type { Granularity, NextAction, Opportunity } from '../../types';

export type { NavTarget };

const CONTENT_TYPE_LABELS: Record<string, string> = {
  case_study: '项目案例',
  comparison_method: '对比与方法',
  qualification_evidence: '资质证据',
  faq: '常见问答',
  data_report: '数据报告',
};

const ACTION_ICON: Record<string, typeof PenLine> = {
  add_evidence: ShieldQuestion,
  add_content: PenLine,
  choose_media: Send,
  keep_observing: Eye,
  no_action: CheckCircle2,
};

export function ActionsTab({
  client,
  brandId,
  granularity,
  nextActions,
  onNavigate,
}: {
  client: ObservationClient;
  brandId: number;
  granularity: Granularity;
  nextActions: NextAction[];
  onNavigate?: (target: NavTarget, ctx: Record<string, unknown>) => void;
}) {
  const nav = resolveNavigate(onNavigate);
  const res = useObservationResource(
    (signal) => client.opportunities(brandId, granularity, signal),
    [brandId, granularity],
    true,
  );
  const opps = res.data?.items ?? [];

  const oppState = ResourceStateView({
    status: res.status,
    errorCode: res.errorCode,
    httpStatus: res.httpStatus,
    message: res.errorMessage,
    isEmpty: res.status === 'success' && opps.length === 0,
    onRetry: res.reload,
    compact: true,
    emptyTitle: '暂无内容机会建议',
    emptyDesc: '积累更多跨平台观测后，这里会给出内容缺口建议。',
  });

  function actionTarget(a: NextAction): NavTarget | null {
    if (a.action === 'choose_media') return 'publish';
    if (a.action === 'add_evidence' || a.action === 'add_content') return 'writing';
    return null;
  }

  return (
    <div className="space-y-4">
      <SectionHeader title={PAGE.actions.title} hint={PAGE.actions.hint} />

      {/* 当下行动 */}
      {nextActions.length > 0 ? (
        <ul className="space-y-2.5">
          {nextActions.map((a, i) => {
            const Icon = ACTION_ICON[a.action] || CheckCircle2;
            const target = actionTarget(a);
            return (
              <li key={`${a.action}-${i}`}>
                <Panel className="p-4">
                  <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
                    <div className="flex min-w-0 items-start gap-2.5">
                      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-muted">
                        <Icon className="h-4 w-4 text-brand" />
                      </span>
                      <div className="min-w-0">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-sm font-semibold text-foreground">{a.title}</span>
                          <Pill tone="muted">
                            {PAGE.actions.priority} {i + 1}
                          </Pill>
                          {!a.may_charge ? <Pill tone="good">{PAGE.actions.noCharge}</Pill> : null}
                          {a.requires_confirmation ? <Pill tone="info">{PAGE.actions.needConfirm}</Pill> : null}
                        </div>
                        <LongText text={a.reason} clamp={2} className="mt-1 text-xs text-muted-foreground" />
                      </div>
                    </div>
                    {target ? (
                      <button
                        type="button"
                        onClick={() => nav(target, { brandId, action: a.action })}
                        className="inline-flex min-h-[36px] shrink-0 items-center gap-1.5 self-start rounded-lg border border-border bg-card px-3 py-2 text-xs font-medium text-foreground transition hover:bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                      >
                        {target === 'publish' ? <Send className="h-3.5 w-3.5" /> : <PenLine className="h-3.5 w-3.5" />}
                        {target === 'publish' ? PAGE.actions.goPublish : PAGE.actions.goWriting}
                      </button>
                    ) : null}
                  </div>
                </Panel>
              </li>
            );
          })}
        </ul>
      ) : null}

      {/* 内容机会 */}
      <div>
        <h4 className="mb-2.5 text-sm font-semibold text-foreground">内容机会</h4>
        {oppState ? (
          oppState
        ) : (
          <ul className="space-y-2.5">
            {opps.map((o: Opportunity) => (
              <li key={o.opportunity_id}>
                <Panel className="p-4">
                  <div className="flex flex-wrap items-center gap-2">
                    <Pill tone="muted">
                      {PAGE.actions.priority} {o.priority}
                    </Pill>
                    <span className="text-sm font-semibold text-foreground">{o.topic}</span>
                    <Pill tone="info">
                      <FileText className="h-3 w-3" />
                      {CONTENT_TYPE_LABELS[o.recommended_content_type] || o.recommended_content_type}
                    </Pill>
                    <StabilityTag status={o.stability} />
                  </div>
                  <LongText text={o.reason} clamp={2} className="mt-1.5 text-xs text-muted-foreground" />
                  {o.recommended_evidence.length > 0 ? (
                    <div className="mt-2">
                      <span className="text-xs text-muted-foreground">建议准备的证据：</span>
                      <div className="mt-1 flex flex-wrap gap-1.5">
                        {o.recommended_evidence.map((ev, i) => (
                          <Pill key={i} tone="neutral">
                            {ev}
                          </Pill>
                        ))}
                      </div>
                    </div>
                  ) : null}
                  <div className="mt-3 flex items-center justify-between">
                    <span className="text-xs text-muted-foreground">本次分析样本数 {formatCount(o.sample_size)}</span>
                    <button
                      type="button"
                      onClick={() => nav('writing', { brandId, opportunityId: o.opportunity_id })}
                      className="inline-flex min-h-[34px] items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                      <PenLine className="h-3.5 w-3.5" />
                      {PAGE.actions.goWriting}
                    </button>
                  </div>
                </Panel>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
