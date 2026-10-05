// 营销军师 · 共用 UI 原语(主题感知 · 语义 token 自动亮暗 · Framer Motion 微动效)
import type { ReactNode } from 'react'
import { motion } from 'framer-motion'
import { Loader2, Inbox, AlertTriangle } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/badge'
import type { CaseStatus, RiskLevel, OpportunityGroup } from '../types'
import { CASE_STATUS_LABEL, RISK_LABEL, GROUP_LABEL } from '../types'

// 统一微动效参数;prefers-reduced-motion 由 index.tsx 根部 MotionConfig reducedMotion="user" 全局尊重(R7)
const fade = { initial: { opacity: 0, y: 6 }, animate: { opacity: 1, y: 0 }, transition: { duration: 0.22 } }

export function FadeIn({ children, delay = 0, className }: { children: ReactNode; delay?: number; className?: string }) {
  return (
    <motion.div {...fade} transition={{ ...fade.transition, delay }} className={className}>
      {children}
    </motion.div>
  )
}

export function Panel({ title, action, children, className }: {
  title?: string; action?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <section className={cn('rounded-xl border border-border bg-card p-4 sm:p-5', className)}>
      {(title || action) && (
        <div className="mb-3 flex items-center justify-between gap-2">
          {title && <h3 className="text-sm font-semibold text-foreground">{title}</h3>}
          {action}
        </div>
      )}
      {children}
    </section>
  )
}

// 事实数字卡(替代"预计转化 ¥28,500" 预测性收入 · 只显真实事实数)
export function StatCard({ label, value, sub, icon, accent = false, delay = 0 }: {
  label: string; value: ReactNode; sub?: ReactNode; icon?: ReactNode; accent?: boolean; delay?: number
}) {
  return (
    <FadeIn delay={delay}>
      <div className={cn('rounded-xl border border-border bg-card p-4 transition-shadow hover:shadow-sm',
        accent && 'ring-1 ring-brand/30')}>
        <div className="flex items-center gap-2 text-xs text-muted-foreground">
          {icon}
          <span>{label}</span>
        </div>
        <div className="mt-2 text-2xl font-semibold tabular-nums text-foreground">{value}</div>
        {sub && <div className="mt-1 text-xs text-muted-foreground">{sub}</div>}
      </div>
    </FadeIn>
  )
}

export function LoadingState({ text = '加载中…' }: { text?: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
      <Loader2 className="size-4 animate-spin" /> {text}
    </div>
  )
}

// 空态即引导(上线首月大多空 · 空态给下一步动作)
export function EmptyState({ title, hint, action }: { title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-14 text-center">
      <Inbox className="size-8 text-muted-foreground/50" />
      <div className="text-sm font-medium text-foreground">{title}</div>
      {hint && <div className="max-w-md text-xs text-muted-foreground">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function ErrorState({ text, onRetry }: { text: string; onRetry?: () => void }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-14 text-center">
      <AlertTriangle className="size-7 text-amber-500" />
      <div className="text-sm text-foreground">{text}</div>
      {onRetry && (
        <button onClick={onRetry} className="mt-1 rounded-md border border-border px-3 py-1 text-xs text-muted-foreground hover:bg-muted">
          重试
        </button>
      )}
    </div>
  )
}

const STATUS_VARIANT: Record<CaseStatus, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  draft: 'secondary', pending: 'outline', approved: 'default', rejected: 'destructive',
  changes_requested: 'outline', executed: 'default', expired: 'secondary', cancelled: 'secondary',
}
export function CaseStatusBadge({ status }: { status: CaseStatus }) {
  return <Badge variant={STATUS_VARIANT[status]}>{CASE_STATUS_LABEL[status]}</Badge>
}

const RISK_COLOR: Record<RiskLevel, string> = {
  low: 'bg-muted text-muted-foreground border-border',
  med: 'bg-amber-500/10 text-amber-600 border-amber-500/30',
  high: 'bg-rose-500/10 text-rose-600 border-rose-500/30',
}
export function RiskBadge({ risk }: { risk: RiskLevel }) {
  return (
    <span className={cn('inline-flex items-center rounded-full border px-2 py-0.5 text-[11px]', RISK_COLOR[risk])}>
      {RISK_LABEL[risk]}
    </span>
  )
}

const GROUP_COLOR: Record<OpportunityGroup, string> = {
  high_value: 'text-violet-500', convertible: 'text-sky-500',
  dormant: 'text-amber-500', offer: 'text-brand',
}
export function GroupTag({ group }: { group: OpportunityGroup }) {
  return <span className={cn('text-[11px] font-medium', GROUP_COLOR[group])}>{GROUP_LABEL[group]}</span>
}

// 算力 + ¥ 双口径(1 元 = 130 算力)
export function points2yuan(points: number): string {
  return (points / 130).toFixed(2)
}
export function PowerAndYuan({ points }: { points: number }) {
  return (
    <span className="tabular-nums">
      {points.toLocaleString()} 算力 <span className="text-muted-foreground">(约 ¥{points2yuan(points)})</span>
    </span>
  )
}
