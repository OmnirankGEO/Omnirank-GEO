/**
 * XiaobangExitBar — 抽屉内的**出口条**(包 D①③④)。
 *
 * 工单 §5.3:禁止以「去帮助中心」作为唯一答复或唯一动作。
 * 后端现在每一轮失败/低置信都会签发 `meta.exits`;这里把它渲染成可点的东西。
 *
 * 🔴 三条形态纪律:
 *   1. **只渲染服务端签发的出口** —— 前端不造出口(造出来的按钮背后没有合同);
 *   2. **转人工前先把「会提交什么」摊开给用户看**(`submits`),不悄悄外发;
 *   3. 提交成功后显示**工单 id 与下一步**,不是弹个 toast 就完事
 *      (工单 §6 包 D④ 原话)。
 */
import { useState } from 'react'
import { CheckCircle2, LifeBuoy, RefreshCw, MessageCircleQuestion, Hand } from 'lucide-react'
import { cn } from '@/lib/utils'
import type { XiaobangExit } from '@/hooks/useXiaobangChat'
import { useXiaobangHandoff } from '@/hooks/useXiaobangHandoff'

const ICONS: Record<string, typeof CheckCircle2> = {
  resolved: CheckCircle2,
  unresolved: Hand,
  handoff: LifeBuoy,
  retry: RefreshCw,
  clarify: MessageCircleQuestion,
  manual_path: Hand,
}

export interface XiaobangExitBarProps {
  exits: XiaobangExit[]
  /** 转人工要带的上下文 —— 由抽屉提供,这里只显示与提交 */
  question: string
  aiAnswer: string
  currentPage: string
  recentTurns: Array<{ role: string; content: string }>
  onResolved?: () => void
  onRetry?: () => void
  onClarify?: () => void
}

export function XiaobangExitBar({
  exits, question, aiAnswer, currentPage, recentTurns,
  onResolved, onRetry, onClarify,
}: XiaobangExitBarProps) {
  const [confirming, setConfirming] = useState(false)
  const [acked, setAcked] = useState<'resolved' | null>(null)
  const handoff = useXiaobangHandoff()

  if (!exits || exits.length === 0) return null

  const handoffExit = exits.find((e) => e.exit_id === 'handoff')

  // 提交成功 → 显示工单 id 与下一步(不是 toast)
  if (handoff.result) {
    return (
      <div
        data-testid="xiaobang-handoff-result"
        className="mt-2 rounded-lg border border-border/50 bg-muted/30 px-3 py-2 text-xs"
      >
        <p className="font-medium text-foreground">
          已提交给工作人员 · 工单 #{handoff.result.ticketId}
        </p>
        <p className="mt-0.5 text-muted-foreground">
          {handoff.result.status === 'existing'
            ? '这条之前已经提过了,我们并到同一张工单,不会重复排队。'
            : '有人跟进后会通知你;这期间你可以继续问我别的。'}
        </p>
      </div>
    )
  }

  if (acked === 'resolved') {
    return (
      <div
        data-testid="xiaobang-resolved-ack"
        className="mt-2 text-xs text-muted-foreground"
      >
        好的,那这条就先这样。还有别的随时问我。
      </div>
    )
  }

  return (
    <div data-testid="xiaobang-exit-bar" className="mt-2 space-y-2">
      {/* 转人工前:把会提交的东西摊开 */}
      {confirming && handoffExit && (
        <div
          data-testid="xiaobang-handoff-preview"
          className="rounded-lg border border-border/50 bg-muted/30 px-3 py-2 text-xs"
        >
          <p className="font-medium text-foreground">会一起提交给工作人员:</p>
          <ul className="mt-1 space-y-0.5 text-muted-foreground">
            {(handoffExit.submits || []).map((item) => (
              <li key={item}>· {item}</li>
            ))}
          </ul>
          {handoff.error && (
            <p data-testid="xiaobang-handoff-error" className="mt-1.5 text-destructive">
              {handoff.error} —— 你填的内容还在,可以直接再试一次。
            </p>
          )}
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              disabled={handoff.submitting}
              onClick={() => {
                void handoff.submit({ question, aiAnswer, currentPage, recentTurns })
              }}
              className={cn(
                'rounded-md px-2.5 py-1 text-xs font-medium',
                'bg-foreground text-background disabled:opacity-50',
              )}
            >
              {handoff.submitting ? '提交中…' : handoff.error ? '再试一次' : '确认提交'}
            </button>
            <button
              type="button"
              onClick={() => setConfirming(false)}
              className="rounded-md px-2.5 py-1 text-xs text-muted-foreground hover:text-foreground"
            >
              先不提交
            </button>
          </div>
        </div>
      )}

      <div className="flex flex-wrap gap-1.5">
        {exits.map((exit) => {
          const Icon = ICONS[exit.exit_id] || CheckCircle2
          return (
            <button
              key={exit.exit_id}
              type="button"
              data-testid={`xiaobang-exit-${exit.exit_id}`}
              onClick={() => {
                if (exit.exit_id === 'resolved') {
                  setAcked('resolved')
                  onResolved?.()
                  return
                }
                if (exit.exit_id === 'retry') return onRetry?.()
                if (exit.exit_id === 'clarify') return onClarify?.()
                // unresolved 与 handoff 都进「摊开要提交什么」这一步
                setConfirming(true)
              }}
              className={cn(
                'inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-xs',
                'transition-colors',
                exit.exit_id === 'handoff' || exit.exit_id === 'unresolved'
                  ? 'border-border/60 text-foreground hover:bg-muted/50'
                  : 'border-border/40 text-muted-foreground hover:bg-muted/40 hover:text-foreground',
              )}
            >
              <Icon className="size-3.5" />
              {exit.label}
            </button>
          )
        })}
      </div>
    </div>
  )
}

export default XiaobangExitBar
