import { Clock3, MapPin, UserRound } from 'lucide-react'
import { cn } from '@/lib/utils'
import type {
  CustomerOperationPlanSummary,
  XiaobangServerContext,
} from '@/hooks/useXiaobangContext'

function formatUpdatedAt(raw: string | null | undefined): string {
  if (!raw) return '暂无数据时间'
  const parsed = new Date(raw)
  if (Number.isNaN(parsed.getTime())) return '数据时间待确认'
  return `更新于 ${parsed.toLocaleString('zh-CN', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  })}`
}

export function XiaobangContextBar({
  context,
  operationPlan,
  loading,
  onChooseCustomer,
}: {
  context?: XiaobangServerContext | null
  operationPlan?: CustomerOperationPlanSummary | null
  loading?: boolean
  onChooseCustomer?: () => void
}) {
  return (
    <div
      data-testid="xiaobang-context-bar"
      className="shrink-0 border-b border-border/30 bg-muted/20 px-3 py-2"
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
        <span className="inline-flex min-w-0 items-center gap-1">
          <UserRound className="size-3 shrink-0" />
          {loading ? (
            <span>正在核对客户</span>
          ) : context?.has_customer ? (
            <span className="max-w-28 truncate text-foreground">{context.brand_name || '当前客户'}</span>
          ) : (
            <button type="button" onClick={onChooseCustomer} className="text-brand hover:underline">
              选择客户
            </button>
          )}
        </span>
        <span className="inline-flex min-w-0 items-center gap-1">
          <MapPin className="size-3 shrink-0" />
          <span className="max-w-28 truncate">{context?.page_name || '当前页面'}</span>
        </span>
        <span className={cn('inline-flex items-center gap-1', loading && 'opacity-60')}>
          <Clock3 className="size-3 shrink-0" />
          {loading ? '正在更新' : formatUpdatedAt(context?.data_updated_at)}
        </span>
        {context?.has_customer && operationPlan?.metrics?.writing && (
          <span
            data-testid="xiaobang-plan-status"
            className="rounded-md bg-brand/10 px-1.5 py-0.5 text-brand"
          >
            当前计划：待写 {operationPlan.metrics.writing.pending ?? 0}
          </span>
        )}
      </div>
    </div>
  )
}
