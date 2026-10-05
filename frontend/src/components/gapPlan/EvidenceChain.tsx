/**
 * 证据链四格进度(B4)
 *
 * 已发布 → 已收录 → 被 AI 引用 → 客户进入推荐
 *
 * 🔴 组件刻意做成**可复用且无业务依赖**:只吃 stages/notice/schedule 三样,
 *    不 import 报价、不 import 快照类型之外的东西 —— 客户门户(P5 之后)要复用它。
 *    本包**不接门户**,只是不给未来挖坑。
 *
 * 🔴 R9:P5 回查调度上线前,只有「已发布」能亮。此时必须显示成
 *    「收录与引用检查将在发布后 7 天起自动进行」这种**计划中**的话术,
 *    不是故障、不是转圈、不是红色失败。长期无进展也给人话建议而不是"审核失败"。
 *
 * 🔴 响应式(合同 §10):≥768 横向四格;<768 改纵向时间线。
 */
import { Check } from 'lucide-react';
import type { GapEvidenceBlock } from '@/lib/gapPlanApi';
import { cn } from '@/lib/utils';

interface Props {
  evidence?: GapEvidenceBlock;
  className?: string;
  /** 纵向时间线(移动端)。由调用方按断点决定,组件自身不猜视口。 */
  vertical?: boolean;
}

export function EvidenceChain({ evidence, className, vertical = false }: Props) {
  const stages = evidence?.stages ?? [];
  if (!stages.length) {
    return (
      <div className={cn('text-xs text-muted-foreground', className)}>
        还没有发布记录。填入发布链接后，这里会显示收录与引用的检查进度。
      </div>
    );
  }

  return (
    <div className={cn('space-y-2', className)}>
      <ol
        className={cn(
          'flex gap-1',
          vertical ? 'flex-col items-start gap-2' : 'flex-row items-center overflow-x-auto',
        )}
        aria-label="内容效果进度"
      >
        {stages.map((stage, idx) => (
          <li
            key={stage.key}
            className={cn(
              'flex items-center gap-1.5 shrink-0',
              vertical ? 'w-full' : '',
            )}
          >
            <span
              className={cn(
                'flex h-5 w-5 items-center justify-center rounded-full border text-[10px]',
                stage.done
                  ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-700 dark:text-emerald-300'
                  : 'border-border bg-muted/40 text-muted-foreground',
              )}
              aria-hidden="true"
            >
              {stage.done ? <Check className="h-3 w-3" /> : idx + 1}
            </span>
            {/* 🔴 状态不只靠颜色:文字标签始终在(合同 §11) */}
            <span
              className={cn(
                'text-xs whitespace-nowrap',
                stage.done ? 'text-foreground' : 'text-muted-foreground',
              )}
            >
              {stage.label}
              <span className="sr-only">{stage.done ? '（已完成）' : '（未完成）'}</span>
            </span>
            {!vertical && idx < stages.length - 1 && (
              <span className="mx-0.5 h-px w-4 bg-border shrink-0" aria-hidden="true" />
            )}
          </li>
        ))}
      </ol>

      {/* R9 态文案:计划中,不是故障 */}
      {evidence?.notice ? (
        <p className="text-[11px] leading-relaxed text-muted-foreground" aria-live="polite">
          {evidence.notice}
        </p>
      ) : null}

      {evidence?.checkback_schedule?.length ? (
        <p className="text-[11px] text-muted-foreground">
          回查安排：
          {evidence.checkback_schedule.map((c) => `第 ${c.due_day} 天`).join(' · ')}
        </p>
      ) : null}
    </div>
  );
}

export default EvidenceChain;
