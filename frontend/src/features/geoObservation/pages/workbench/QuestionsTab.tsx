/**
 * 问题与证据 Tab：每条问题的全称、回答结果、命中文字、引用数、最近变化、查看证据。
 * - 后端分页驱动，前端不截断数据；长问题可展开看全称。
 * - "查看证据"打开证据抽屉（真实调用），空证据显示原因。
 */

import { useState } from 'react';
import { FileSearch, ChevronLeft, ChevronRight } from 'lucide-react';
import { Panel, SectionHeader, LongText, Pill } from '../../components/shared';
import { OutcomeBadge } from '../../components/badges';
import { ResourceStateView, SkeletonRows } from '../../components/states';
import { PAGE } from '../../copy';
import { formatPosition } from '../../format';
import { useObservationResource } from '../../hooks/useObservationResource';
import type { ObservationClient } from '../../client';

const PAGE_SIZE = 20;

export function QuestionsTab({
  client,
  brandId,
  onOpenEvidence,
}: {
  client: ObservationClient;
  brandId: number;
  /** 打开证据抽屉（抽屉提升到工作台层，供问题列表与洞察面板共用）。 */
  onOpenEvidence: (observationId: string) => void;
}) {
  const [page, setPage] = useState(1);

  // 问题分页无时间维度（AI-3 只接 page/page_size）
  const res = useObservationResource(
    (signal) => client.brandQuestions(brandId, page, PAGE_SIZE, signal),
    [brandId, page],
    true,
  );

  const data = res.data;
  const stateView = ResourceStateView({
    status: res.status,
    errorCode: res.errorCode,
    httpStatus: res.httpStatus,
    message: res.errorMessage,
    isEmpty: res.status === 'success' && (data?.items.length ?? 0) === 0,
    onRetry: res.reload,
    loadingSkeleton: <SkeletonRows rows={5} />,
    emptyTitle: '本周期还没有可展示的问题',
    emptyDesc: '跑一次监测后，客户购买的搜索问题会显示在这里。',
  });

  const totalPages = data ? Math.max(1, Math.ceil(data.total / (data.page_size || PAGE_SIZE))) : 1;

  return (
    <div className="space-y-4">
      <SectionHeader title={PAGE.questions.title} hint={PAGE.questions.hint} />

      {stateView ? (
        stateView
      ) : data ? (
        <>
          <ul className="space-y-2.5">
            {data.items.map((q) => (
              <li key={q.observation_id}>
                <Panel className="p-3.5">
                  <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                    <div className="min-w-0 flex-1">
                      <LongText text={q.question} clamp={2} className="font-medium" />
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <OutcomeBadge outcome={q.outcome} withHint />
                        {q.position !== null ? (
                          <Pill tone="info">{formatPosition(q.position)}</Pill>
                        ) : null}
                        {q.citations > 0 ? (
                          <Pill tone="neutral">引用 {q.citations}</Pill>
                        ) : null}
                        {q.changed ? <Pill tone="warn">{PAGE.questions.changed}</Pill> : null}
                        {q.matched_text ? (
                          <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
                            命中：
                            <span className="max-w-[12rem] truncate text-foreground" title={q.matched_text}>
                              {q.matched_text}
                            </span>
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => onOpenEvidence(q.observation_id)}
                      disabled={!q.evidence_available}
                      title={!q.evidence_available ? PAGE.questions.noEvidence : undefined}
                      className="inline-flex min-h-[36px] shrink-0 items-center gap-1.5 self-start rounded-lg border border-border bg-card px-3 py-2 text-xs font-medium text-foreground transition enabled:hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                    >
                      <FileSearch className="h-3.5 w-3.5" />
                      {PAGE.questions.viewEvidence}
                    </button>
                  </div>
                </Panel>
              </li>
            ))}
          </ul>

          {/* 分页（后端驱动） */}
          {data.total > (data.page_size || PAGE_SIZE) ? (
            <div className="flex items-center justify-between">
              <span className="text-xs text-muted-foreground">
                第 {data.page} / {totalPages} 页 · 共 {data.total} 条
              </span>
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  disabled={page <= 1}
                  aria-label="上一页"
                  className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-foreground transition enabled:hover:bg-muted disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                >
                  <ChevronLeft className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  disabled={page >= totalPages}
                  aria-label="下一页"
                  className="inline-flex h-8 w-8 items-center justify-center rounded-lg border border-border bg-card text-foreground transition enabled:hover:bg-muted disabled:opacity-40 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
                >
                  <ChevronRight className="h-4 w-4" />
                </button>
              </div>
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
