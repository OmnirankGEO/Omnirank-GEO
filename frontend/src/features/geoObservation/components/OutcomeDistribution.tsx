/**
 * 结果分布（十类 outcome 横向条）。
 * - 只渲染后端给的 count；条宽为 count/该分布总数的**视觉比例**（非业务指标再计算）。
 * - 每条右侧的百分比是"占本次结果分布"的份额，明确标注，不等于任何 SSOT 指标。
 * - UNKNOWN 两类（品牌可能混淆 / 本次检测未完成）单独展示，绝不并入"未提到"。
 */

import { OUTCOME_LABELS, OUTCOME_HINTS, EXCLUDED_FROM_RATE } from '../copy';
import { InfoTip } from './shared';
import { formatCount } from '../format';
import type { OutcomeCount, OutcomeKey } from '../types';

// 画布固定顺序（推荐→拒绝→未提到→不计入）
const ORDER: OutcomeKey[] = [
  'recommended',
  'conditionally_recommended',
  'candidate_only',
  'mentioned_only',
  'criteria_only',
  'refused_no_evidence',
  'refused_risk',
  'not_mentioned',
  'entity_ambiguous',
  'engine_error',
];

const BAR_TONE: Record<OutcomeKey, string> = {
  recommended: 'bg-emerald-500',
  conditionally_recommended: 'bg-blue-500',
  candidate_only: 'bg-sky-500',
  mentioned_only: 'bg-violet-500',
  criteria_only: 'bg-teal-500',
  refused_no_evidence: 'bg-amber-500',
  refused_risk: 'bg-orange-500',
  not_mentioned: 'bg-muted-foreground/40',
  entity_ambiguous: 'bg-muted-foreground/25',
  engine_error: 'bg-muted-foreground/25',
};

export function OutcomeDistribution({ outcomes }: { outcomes: OutcomeCount[] }) {
  const byKey = new Map<OutcomeKey, number>();
  for (const o of outcomes) byKey.set(o.outcome, o.count);
  const shown = ORDER.filter((k) => byKey.has(k));
  const total = shown.reduce((s, k) => s + (byKey.get(k) || 0), 0);
  const max = Math.max(1, ...shown.map((k) => byKey.get(k) || 0));

  if (total === 0) {
    return <p className="py-6 text-center text-sm text-muted-foreground">本周期暂无可分类的结果。</p>;
  }

  // 稳定布局：标签整行完整显示（不截断改变语义）+ 计数在右，进度条独占一行。
  return (
    <ul className="space-y-3">
      {shown.map((key) => {
        const count = byKey.get(key) || 0;
        const share = total > 0 ? (count / total) * 100 : 0;
        const width = (count / max) * 100;
        const excluded = EXCLUDED_FROM_RATE.includes(key);
        return (
          <li key={key} className="space-y-1.5">
            <div className="flex items-start justify-between gap-2">
              <span className="flex items-center gap-1 text-xs text-foreground">
                <span className="break-words">{OUTCOME_LABELS[key]}</span>
                <InfoTip label={OUTCOME_HINTS[key]} />
              </span>
              <span className="shrink-0 whitespace-nowrap text-right text-xs tabular-nums text-foreground">
                {formatCount(count)}
                <span className="ml-1 text-muted-foreground">{share.toFixed(1)}%</span>
              </span>
            </div>
            <span className="block h-2 overflow-hidden rounded-full bg-muted" role="presentation">
              <span
                className={`block h-full rounded-full ${BAR_TONE[key]} ${excluded ? 'opacity-70' : ''}`}
                style={{ width: `${Math.max(2, width)}%` }}
              />
            </span>
          </li>
        );
      })}
      <li className="flex items-center gap-1 pt-1 text-[11px] text-muted-foreground">
        <span>百分比为该结果占本周期结果分布的份额。</span>
        <InfoTip label="这里的百分比是每类结果在本次结果分布里的占比，用来看结果构成；被 AI 提到、被直接推荐等对外比例以顶部指标为准。" />
      </li>
    </ul>
  );
}
