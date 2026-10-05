/**
 * 语义洞察结果面板。
 * - 只在显式生成后出现：pending→progress→completed / failed。
 * - 失败显示"洞察暂不可用"+重试，确定性图表不受影响。
 * - 结果只解释、给证据引用，不自动执行任何写作/发布/扣费。
 */

import { Sparkles, Loader2, AlertTriangle, RefreshCw } from 'lucide-react';
import { Panel } from './shared';
import { PAGE } from '../copy';
import type { SemanticInsightState } from '../hooks/useSemanticInsight';

export function SemanticInsightPanel({
  state,
  onEvidenceClick,
}: {
  state: SemanticInsightState;
  onEvidenceClick?: (observationId: string) => void;
}) {
  if (state.phase === 'idle') return null;

  if (state.phase === 'starting' || state.phase === 'running') {
    const pct = Math.max(5, Math.min(100, state.progress || (state.phase === 'starting' ? 5 : 30)));
    return (
      <Panel className="p-4" aria-live="polite">
        <div className="mb-2 flex items-center gap-2 text-sm font-medium text-foreground">
          <Loader2 className="h-4 w-4 animate-spin text-brand" />
          {PAGE.insight.generating}
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-muted" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}>
          <div className="h-full rounded-full bg-brand transition-all duration-500" style={{ width: `${pct}%` }} />
        </div>
      </Panel>
    );
  }

  if (state.phase === 'failed') {
    return (
      <Panel className="border-amber-500/30 p-4" aria-live="polite">
        <div className="flex items-start gap-2">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-500" />
          <div className="min-w-0 flex-1">
            <p className="text-sm text-foreground">{state.errorMessage || PAGE.insight.unavailable}</p>
            {state.retryable ? (
              <button
                type="button"
                onClick={state.generate}
                className="mt-2 inline-flex min-h-[32px] items-center gap-1.5 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-foreground transition hover:bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
              >
                <RefreshCw className="h-3.5 w-3.5" />
                {PAGE.insight.retry}
              </button>
            ) : null}
          </div>
        </div>
      </Panel>
    );
  }

  // completed
  const r = state.result;
  return (
    <Panel className="border-brand/25 bg-brand/5 p-4" aria-live="polite">
      <div className="mb-1.5 flex items-center gap-2 text-sm font-semibold text-foreground">
        <Sparkles className="h-4 w-4 text-brand" />
        {PAGE.insight.completedTitle}
      </div>
      {r?.summary ? (
        <p className="text-sm leading-relaxed text-foreground break-words">{r.summary}</p>
      ) : null}
      {r?.evidence_refs && r.evidence_refs.length > 0 ? (
        <div className="mt-3">
          <p className="mb-1 text-xs font-medium text-muted-foreground">{PAGE.insight.basisTitle}</p>
          <div className="flex flex-wrap gap-1.5">
            {r.evidence_refs.map((ref) => (
              <button
                key={ref}
                type="button"
                onClick={() => onEvidenceClick?.(ref)}
                disabled={!onEvidenceClick}
                className="inline-flex min-h-[28px] items-center rounded-full border border-border bg-card px-2.5 py-1 text-xs text-foreground transition enabled:hover:bg-muted disabled:opacity-70 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
              >
                查看证据
              </button>
            ))}
          </div>
        </div>
      ) : null}
      <p className="mt-3 text-[11px] text-muted-foreground">{PAGE.insight.onlyExplains}</p>
    </Panel>
  );
}
