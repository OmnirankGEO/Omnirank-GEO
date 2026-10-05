/**
 * 提现记录列表
 * 分页加载，展示状态、金额、费用等
 */
import { useState, useEffect, useCallback } from 'react';
import { useIsMounted } from '@/hooks/useIsMounted';
import { Loader2 } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn, formatDateTime } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';

// ========== 类型 ==========

interface Withdrawal {
  id: string;
  amount_yuan: number;
  fee_yuan: number;
  actual_yuan: number;
  status: 'pending' | 'approved' | 'paid' | 'rejected';
  reject_reason: string | null;
  external_tx_ref: string | null;
  created_at: string;
}

interface WithdrawalPage {
  items: Withdrawal[];
  total: number;
  page: number;
  limit: number;
}

const STATUS_CONFIG: Record<Withdrawal['status'], { label: string; className: string }> = {
  pending:  { label: '待审核', className: 'bg-amber-500/15 text-amber-400 border-amber-500/20' },
  approved: { label: '审核通过', className: 'bg-blue-500/15 text-blue-400 border-blue-500/20' },
  paid:     { label: '已打款', className: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/20' },
  rejected: { label: '已拒绝', className: 'bg-red-500/15 text-red-400 border-red-500/20' },
};

const PAGE_SIZE = 10;

export default function WithdrawalHistory() {
  const isMounted = useIsMounted();

  const [items, setItems] = useState<Withdrawal[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [initialLoaded, setInitialLoaded] = useState(false);

  const hasMore = items.length < total;

  const fetchPage = useCallback(async (p: number, append: boolean) => {
    setLoading(true);
    try {
      const res = await authFetch(`/api/wallet/withdrawals?page=${p}&limit=${PAGE_SIZE}`);
      if (!isMounted()) return;
      if (res.ok) {
        const data: WithdrawalPage = await res.json();
        setItems((prev) => append ? [...prev, ...(data.items ?? [])] : (data.items ?? []));
        setTotal(data.total ?? 0);
        setPage(data.page ?? p);
      }
    } catch {
      // 静默
    } finally {
      if (isMounted()) {
        setLoading(false);
        setInitialLoaded(true);
      }
    }
  }, [isMounted]);

  useEffect(() => {
    fetchPage(1, false);
  }, [fetchPage]);

  const handleLoadMore = () => {
    if (!loading && hasMore) {
      fetchPage(page + 1, true);
    }
  };

  // ========== 渲染 ==========

  if (!initialLoaded) {
    return (
      <div className="flex items-center justify-center py-12">
        <Loader2 className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (items.length === 0) {
    return (
      <div className="text-center py-12 text-muted-foreground text-sm">
        暂无提现记录
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {items.map((w) => {
        const cfg = STATUS_CONFIG[w.status] ?? STATUS_CONFIG.pending;
        return (
          <div
            key={w.id}
            className="rounded-lg border border-border/50 p-4 space-y-2 hover:bg-muted/20 transition-colors"
          >
            <div className="flex items-center justify-between gap-2 flex-wrap">
              <div className="flex items-center gap-2">
                <span className="text-base font-semibold text-foreground tabular-nums">
                  &yen;{w.amount_yuan.toFixed(2)}
                </span>
                <Badge className={cn('text-xs', cfg.className)}>{cfg.label}</Badge>
              </div>
              <span className="text-xs text-muted-foreground">
                {formatDateTime(w.created_at)}
              </span>
            </div>

            <div className="flex gap-4 text-xs text-muted-foreground">
              <span>手续费: &yen;{w.fee_yuan.toFixed(2)}</span>
              <span>实际到账: &yen;{w.actual_yuan.toFixed(2)}</span>
            </div>

            {w.status === 'rejected' && w.reject_reason && (
              <div className="text-xs text-red-400 bg-red-500/5 rounded px-2 py-1">
                拒绝原因: {w.reject_reason}
              </div>
            )}

            {w.status === 'paid' && w.external_tx_ref && (
              <div className="text-xs text-muted-foreground">
                交易流水号: <span className="tabular-nums">{w.external_tx_ref}</span>
              </div>
            )}
          </div>
        );
      })}

      {hasMore && (
        <div className="pt-2 text-center">
          <Button
            variant="outline"
            size="sm"
            disabled={loading}
            onClick={handleLoadMore}
          >
            {loading ? (
              <>
                <Loader2 className="size-3.5 animate-spin mr-1.5" />
                加载中...
              </>
            ) : (
              '加载更多'
            )}
          </Button>
        </div>
      )}
    </div>
  );
}
