/**
 * 证据抽屉：打开某条问题的原始证据。
 * - 证据属于当前客户，不泄露其他客户数据。
 * - 展示回答节选、命中文字、引用来源、缺失证据、采集通道说明（代理通道明示）。
 * - loading/error/空证据都有状态；长文本可展开看全称，无跑马灯。
 */

import { useMemo } from 'react';
import { FileText, Link2, AlertCircle } from 'lucide-react';
import { Drawer } from './Drawer';
import { ResourceStateView } from './states';
import { OutcomeBadge } from './badges';
import { LongText, Pill } from './shared';
import { PAGE } from '../copy';
import { useObservationResource } from '../hooks/useObservationResource';
import type { ObservationClient } from '../client';

export function EvidenceDrawer({
  client,
  brandId,
  observationId,
  open,
  onClose,
  onAddEvidence,
}: {
  client: ObservationClient;
  brandId: number;
  observationId: string | null;
  open: boolean;
  onClose: () => void;
  onAddEvidence?: () => void;
}) {
  const res = useObservationResource(
    (signal) => client.evidence(brandId, observationId as string, signal),
    [brandId, observationId],
    open && observationId !== null,
  );

  const data = res.data;
  const stateView = useMemo(
    () =>
      ResourceStateView({
        status: res.status,
        errorCode: res.errorCode,
        httpStatus: res.httpStatus,
        message: res.errorMessage,
        isEmpty: res.status === 'success' && !data,
        onRetry: res.reload,
        compact: true,
        emptyTitle: PAGE.questions.noEvidence,
      }),
    [res, data],
  );

  return (
    <Drawer open={open} onClose={onClose} title={PAGE.evidence.title}>
      {stateView ? (
        stateView
      ) : data ? (
        <div className="space-y-4">
          <p className="text-xs text-muted-foreground">{PAGE.evidence.ownData}</p>

          <div>
            <div className="mb-1.5 flex items-center gap-2">
              <OutcomeBadge outcome={data.outcome} withHint />
            </div>
            <LongText text={data.question} clamp={3} className="font-medium" />
          </div>

          <section>
            <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-foreground">
              <FileText className="h-3.5 w-3.5 text-muted-foreground" />
              {PAGE.evidence.answerExcerpt}
            </h3>
            {data.answer_excerpt ? (
              <p className="rounded-lg border border-border bg-muted/50 p-3 text-sm leading-relaxed text-foreground break-words">
                {data.answer_excerpt}
              </p>
            ) : (
              <p className="rounded-lg border border-dashed border-border bg-muted/30 p-3 text-sm text-muted-foreground">
                {PAGE.evidence.noAnswerExcerpt}
              </p>
            )}
          </section>

          {data.matched_text ? (
            <section>
              <h3 className="mb-1.5 text-xs font-semibold text-foreground">{PAGE.evidence.matched}</h3>
              <Pill tone="good">{data.matched_text}</Pill>
            </section>
          ) : null}

          <section>
            <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-foreground">
              <Link2 className="h-3.5 w-3.5 text-muted-foreground" />
              {PAGE.evidence.citations}
              <span className="text-muted-foreground">（{data.citations.length}）</span>
            </h3>
            {data.citations.length === 0 ? (
              <p className="text-xs text-muted-foreground">本条回答没有提供引用来源。</p>
            ) : (
              <ul className="space-y-1.5">
                {/* AI-3 只给规范化域名 + 类型（隐私：无完整 URL/标题），按域名展示，不做外链 */}
                {data.citations.map((c, i) => (
                  <li key={i} className="flex items-start gap-2 text-sm">
                    <span className="mt-0.5 text-xs text-muted-foreground">{c.rank ?? i + 1}.</span>
                    <span className="min-w-0 break-words text-foreground" title={c.domain}>
                      {c.domain}
                    </span>
                    <Pill tone={c.source_type === 'citation' ? 'info' : 'neutral'}>
                      {c.source_type === 'citation' ? '引用' : '访问来源'}
                    </Pill>
                  </li>
                ))}
              </ul>
            )}
          </section>

          {data.evidence_gap.length > 0 ? (
            <section>
              <h3 className="mb-1.5 flex items-center gap-1.5 text-xs font-semibold text-foreground">
                <AlertCircle className="h-3.5 w-3.5 text-amber-500" />
                {PAGE.evidence.gap}
              </h3>
              <ul className="space-y-1">
                {data.evidence_gap.map((g, i) => (
                  <li key={i} className="flex items-start gap-1.5 text-sm text-foreground">
                    <span className="mt-1 h-1 w-1 shrink-0 rounded-full bg-amber-500" />
                    <span className="break-words">{g}</span>
                  </li>
                ))}
              </ul>
            </section>
          ) : null}

          {data.channel_disclosure ? (
            <section>
              <h3 className="mb-1.5 text-xs font-semibold text-foreground">{PAGE.evidence.channel}</h3>
              {/* section 六：用户端只显示采集平台，不展示 surface/代理实现细节 */}
              <span className="text-sm text-foreground">{data.channel_disclosure.platform}</span>
            </section>
          ) : null}

          {data.next_action && onAddEvidence ? (
            <div className="pt-1">
              <button
                type="button"
                onClick={onAddEvidence}
                className="inline-flex min-h-[38px] items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-xs font-medium text-primary-foreground transition hover:opacity-90 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
              >
                {data.next_action.title}
                {!data.next_action.may_charge ? (
                  <span className="rounded bg-primary-foreground/15 px-1.5 py-0.5 text-[10px]">
                    {PAGE.actions.noCharge}
                  </span>
                ) : null}
              </button>
            </div>
          ) : null}
        </div>
      ) : null}
    </Drawer>
  );
}
