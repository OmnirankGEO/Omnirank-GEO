import { CheckCircle2, CircleDollarSign, Eye, FileCheck2, XCircle } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { RefundWorkOrder, RefundWorkOrderStatus } from './types';

const statusText: Record<RefundWorkOrderStatus, string> = {
  draft: '草稿',
  submitted: '待审核',
  approved: '待执行冲账',
  payout_pending: '待打款',
  completed: '已完成',
  rejected: '已驳回',
  cancelled: '已取消',
};

function money(cents?: number) {
  return `¥${((cents || 0) / 100).toLocaleString('zh-CN', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

export function RefundWorkOrderList({
  items,
  selectedId,
  onSelect,
  onApprove,
  onExecute,
  onComplete,
}: {
  items: RefundWorkOrder[];
  selectedId?: number | null;
  onSelect: (item: RefundWorkOrder) => void;
  onApprove: (item: RefundWorkOrder) => void;
  onExecute: (item: RefundWorkOrder) => void;
  onComplete: (item: RefundWorkOrder) => void;
}) {
  if (!items.length) {
    return (
      <div className="rounded-lg border border-white/10 bg-card/60 p-12 text-center text-sm text-muted-foreground">
        暂无对应状态的退款工单
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {items.map((item) => {
        const customerName = item.impact_snapshot?.order?.customer_name || `客户 ${item.customer_user_id || '-'}`;
        const selected = selectedId === item.id;
        return (
          <div
            key={item.id}
            className={cn(
              'rounded-lg border bg-card/60 p-4 transition hover:border-emerald-400/35',
              selected ? 'border-emerald-400/45' : 'border-white/10',
            )}
          >
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <button
                type="button"
                className="min-w-0 flex-1 cursor-pointer text-left"
                onClick={() => onSelect(item)}
              >
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-base font-semibold text-foreground">工单 #{item.id}</span>
                  <Badge variant="outline" className="border-white/10 text-muted-foreground">
                    {statusText[item.status]}
                  </Badge>
                </div>
                <p className="truncate text-sm text-muted-foreground">
                  {customerName} · 订单 {item.source_order_id}
                </p>
                <p className="mt-1 text-xs text-muted-foreground">
                  预计退款 {money(item.estimated_refund_cents)} · 可退算力 {(item.refundable_power || 0).toLocaleString('zh-CN')}
                </p>
              </button>
              <div className="flex flex-wrap gap-2">
                <Button size="sm" variant="outline" onClick={() => onSelect(item)}>
                  <Eye className="mr-1.5 h-4 w-4" />
                  查看
                </Button>
                {item.status === 'submitted' && (
                  <Button size="sm" onClick={() => onApprove(item)}>
                    <CheckCircle2 className="mr-1.5 h-4 w-4" />
                    审核通过
                  </Button>
                )}
                {item.status === 'approved' && (
                  <Button size="sm" className="bg-amber-500 text-black hover:bg-amber-400" onClick={() => onExecute(item)}>
                    <CircleDollarSign className="mr-1.5 h-4 w-4" />
                    执行冲账
                  </Button>
                )}
                {item.status === 'payout_pending' && (
                  <Button size="sm" onClick={() => onComplete(item)}>
                    <FileCheck2 className="mr-1.5 h-4 w-4" />
                    标记完成
                  </Button>
                )}
                {item.status === 'rejected' && (
                  <span className="inline-flex items-center gap-1 text-xs text-red-300">
                    <XCircle className="h-4 w-4" />
                    {item.rejected_reason || '已驳回'}
                  </span>
                )}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}
