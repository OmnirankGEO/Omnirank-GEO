/**
 * 结果 / 稳定度 / 通道 徽标。全部走业务标签（copy.ts），不露工程枚举。
 */

import { Pill, InfoTip } from './shared';
import { OUTCOME_LABELS, OUTCOME_HINTS, STABILITY_LABELS } from '../copy';
import type { OutcomeKey, StabilityStatus } from '../types';

const OUTCOME_TONE: Record<OutcomeKey, Parameters<typeof Pill>[0]['tone']> = {
  recommended: 'good',
  conditionally_recommended: 'info',
  candidate_only: 'info',
  mentioned_only: 'neutral',
  criteria_only: 'neutral',
  refused_no_evidence: 'warn',
  refused_risk: 'warn',
  not_mentioned: 'muted',
  entity_ambiguous: 'muted',
  engine_error: 'muted',
};

export function OutcomeBadge({ outcome, withHint = false }: { outcome: OutcomeKey; withHint?: boolean }) {
  return (
    <span className="inline-flex items-center gap-1">
      <Pill tone={OUTCOME_TONE[outcome]}>{OUTCOME_LABELS[outcome]}</Pill>
      {withHint ? <InfoTip label={OUTCOME_HINTS[outcome]} /> : null}
    </span>
  );
}

const STABILITY_TONE: Record<StabilityStatus, Parameters<typeof Pill>[0]['tone']> = {
  stable: 'good',
  watch: 'warn',
  insufficient: 'muted',
  shifted: 'info',
};

export function StabilityTag({
  status,
  explanation,
}: {
  status: StabilityStatus;
  explanation?: string;
}) {
  return (
    <span className="inline-flex items-center gap-1">
      <Pill tone={STABILITY_TONE[status]}>{STABILITY_LABELS[status]}</Pill>
      {explanation ? <InfoTip label={explanation} /> : null}
    </span>
  );
}

const STABILITY_DOT: Record<StabilityStatus, string> = {
  stable: 'bg-emerald-500',
  watch: 'bg-amber-500',
  insufficient: 'bg-muted-foreground/50',
  shifted: 'bg-blue-500',
};

/**
 * 稳定度内联版（可换行），用于窄格 KPI 磁贴，避免长标签溢出。
 */
export function StabilityInline({ status }: { status: StabilityStatus }) {
  return (
    <span className="inline-flex items-start gap-1.5">
      <span className={`mt-1 h-2 w-2 shrink-0 rounded-full ${STABILITY_DOT[status]}`} />
      <span className="text-sm font-medium text-foreground break-words">{STABILITY_LABELS[status]}</span>
    </span>
  );
}
