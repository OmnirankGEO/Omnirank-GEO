// 审批队列:L3/L4 待处理动作 · approve/reject(通过≠立即执行,SSH Runner 另行领取)

import { useEffect, useState } from 'react'
import { Check, Loader2, X } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { approveAction, listApprovals, rejectAction } from '../api'
import type { AiOpsApproval } from '../types'
import { EmptyHint, Panel, RiskBadge, timeAgo } from './shared'

// action_type → 人话(worker=merge_fix · ssh_runner allowlist + 不可执行清单)
const ACTION_LABEL: Record<string, string> = {
  merge_fix: '合并 Codex 代码修复',
  prod_status: '查看生产容器状态',
  log_tail: '查看服务日志',
  health_check: '服务健康检查',
  restart_worker: '重启后台服务',
  deploy: '部署上线',
  rollback: '回滚版本',
  db_write: '数据库写入',
  nginx: '修改网关配置',
  env_change: '修改环境变量',
  refund: '退款',
  wallet_adjust: '钱包调账',
  settlement: '结算打款',
  withdrawal: '提现',
  commission: '佣金操作',
}

export function ApprovalQueue({ onOpenTask, refreshKey }: {
  onOpenTask: (taskId: number) => void
  refreshKey: number
}) {
  const [items, setItems] = useState<AiOpsApproval[]>([])
  const [loading, setLoading] = useState(true)
  const [busyId, setBusyId] = useState<number | null>(null)

  const reload = async () => {
    try {
      setItems(await listApprovals('pending'))
    } catch {
      /* ignore */
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void reload() }, [refreshKey])

  const act = async (id: number, kind: 'approve' | 'reject') => {
    setBusyId(id)
    try {
      if (kind === 'approve') await approveAction(id)
      else await rejectAction(id, '控制塔驳回')
      toast.success(kind === 'approve' ? '已通过(等待执行)' : '已驳回')
      await reload()
    } catch (err) {
      toast.error('操作失败', { description: String((err as Error).message) })
    } finally {
      setBusyId(null)
    }
  }

  return (
    <Panel title="审批队列" action={<span className="text-xs text-muted-foreground">待处理 {items.length}</span>}>
      {loading ? (
        <EmptyHint text="加载中" />
      ) : items.length === 0 ? (
        <EmptyHint text="没有待审批动作,不需要你处理" />
      ) : (
        <ul className="space-y-2">
          <li className="rounded-md border border-border bg-muted/20 px-2.5 py-1.5 text-[11px] leading-4 text-muted-foreground">
            AI 只提方案不执行:下面每条都要你人工确认。「通过」后由执行器领取执行,「驳回」即终止。
          </li>
          {items.map((ap) => (
            <li key={ap.id} className="rounded-md border bg-background p-2.5">
              <div className="mb-1.5 flex items-center gap-2">
                <RiskBadge risk={ap.risk_level} />
                <button type="button" onClick={() => onOpenTask(ap.task_id)}
                  className="min-w-0 flex-1 truncate text-left text-xs font-medium text-foreground hover:underline">
                  {ACTION_LABEL[ap.action_type] || ap.action_type}
                </button>
                <span className="shrink-0 text-[11px] text-muted-foreground">{timeAgo(ap.created_at)}</span>
              </div>
              {ap.requested_reason && (
                <p className="mb-2 line-clamp-2 text-[11px] text-muted-foreground">{ap.requested_reason}</p>
              )}
              <div className="flex justify-end gap-2">
                <Button size="sm" variant="ghost" className="text-muted-foreground"
                  onClick={() => void act(ap.id, 'reject')} disabled={busyId === ap.id}>
                  <X className="mr-1 size-3.5" />驳回
                </Button>
                <Button size="sm" variant="default"
                  onClick={() => void act(ap.id, 'approve')} disabled={busyId === ap.id}>
                  {busyId === ap.id ? <Loader2 className="mr-1 size-3.5 animate-spin" /> : <Check className="mr-1 size-3.5" />}
                  通过
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  )
}
