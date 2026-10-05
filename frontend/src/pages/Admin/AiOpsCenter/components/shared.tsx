// AI 运维控制塔 · 共用小组件 / 格式化

import type { ReactNode } from 'react'
import ReactMarkdown from '@/components/SafeMarkdown'
import { Badge } from '@/components/ui/badge'
import type { EventSeverity, RiskLevel, TaskStatus } from '../types'
import { RISK_LABEL, STATUS_LABEL } from '../types'

// 给人看的 Markdown(日报正文 / AI 输出全文等)渲染成排版,不再裸露 # / ## / - 字面。
// prose 来自 @tailwindcss/typography(与 ResearchMonitor/DiagnosisReport 同款);
// 控制塔全程 .dark 包裹 → dark:prose-invert 生效。
export function MarkdownBlock({ text, size = 'sm' }: { text: string; size?: 'sm' | 'xs' }) {
  // typography 插件只有 prose-sm 档;xs 用 utilities 层 text-xs 覆盖根字号(子元素 em 跟随缩放)
  return (
    <div className={`prose prose-sm prose-neutral max-w-none dark:prose-invert ${size === 'xs' ? 'text-xs' : ''}`}>
      <ReactMarkdown>{text}</ReactMarkdown>
    </div>
  )
}

export function timeAgo(iso: string | null): string {
  if (!iso) return '—'
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return '—'
  const diff = Date.now() - t
  const m = Math.floor(diff / 60000)
  if (m < 1) return '刚刚'
  if (m < 60) return `${m} 分钟前`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h} 小时前`
  return `${Math.floor(h / 24)} 天前`
}

const STATUS_VARIANT: Record<TaskStatus, 'default' | 'secondary' | 'outline' | 'destructive'> = {
  queued: 'secondary',
  running: 'default',
  waiting_approval: 'outline',
  succeeded: 'outline',
  failed: 'destructive',
  cancelled: 'outline',
}

export function StatusBadge({ status }: { status: TaskStatus }) {
  return <Badge variant={STATUS_VARIANT[status]}>{STATUS_LABEL[status]}</Badge>
}

const RISK_COLOR: Record<RiskLevel, string> = {
  L0: 'bg-muted text-muted-foreground border-border',
  L1: 'bg-sky-500/10 text-sky-600 border-sky-500/30',
  L2: 'bg-amber-500/10 text-amber-600 border-amber-500/30',
  L3: 'bg-orange-500/10 text-orange-600 border-orange-500/30',
  L4: 'bg-rose-500/10 text-rose-600 border-rose-500/30',
}

export function RiskBadge({ risk }: { risk: RiskLevel }) {
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-[11px] ${RISK_COLOR[risk]}`}>
      {RISK_LABEL[risk]}
    </span>
  )
}

const SEVERITY_COLOR: Record<EventSeverity, string> = {
  info: 'bg-muted-foreground/40',
  warn: 'bg-amber-500',
  error: 'bg-rose-500',
  security: 'bg-violet-500',
}

export function SeverityDot({ severity }: { severity: EventSeverity }) {
  return <span className={`mt-1.5 inline-block size-2 shrink-0 rounded-full ${SEVERITY_COLOR[severity] || SEVERITY_COLOR.info}`} />
}

export function Panel({ title, action, children, className = '' }: {
  title: string
  action?: ReactNode
  children: ReactNode
  className?: string
}) {
  return (
    <section className={`rounded-lg border bg-card p-4 ${className}`}>
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold text-foreground">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  )
}

export function EmptyHint({ text }: { text: string }) {
  return <div className="py-6 text-center text-xs text-muted-foreground">{text}</div>
}
