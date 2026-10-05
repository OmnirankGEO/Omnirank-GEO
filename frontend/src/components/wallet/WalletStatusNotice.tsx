import { AlertTriangle, RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { WalletStatus } from '@/context/WalletContext';

interface WalletStatusNoticeProps {
  status: WalletStatus;
  errorMessage: string | null;
  lastUpdatedAt: string | null;
  onRetry: () => Promise<void> | void;
  className?: string;
  compact?: boolean;
}

function formatUpdatedAt(value: string | null): string {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
}

export function WalletStatusNotice({
  status,
  errorMessage,
  lastUpdatedAt,
  onRetry,
  className,
  compact = false,
}: WalletStatusNoticeProps) {
  const refreshingKnownValue = status === 'loading' && lastUpdatedAt !== null;
  if (status !== 'error' && status !== 'stale' && !refreshingKnownValue) return null;

  const stale = status === 'stale';
  const lastUpdated = formatUpdatedAt(lastUpdatedAt);
  return (
    <section
      role="status"
      aria-live="polite"
      className={cn(
        'rounded-lg border px-4 py-3',
        refreshingKnownValue
          ? 'border-border bg-muted/40'
          : (stale ? 'border-amber-500/30 bg-amber-500/10' : 'border-destructive/30 bg-destructive/10'),
        className,
      )}
    >
      <div className="flex items-start gap-3">
        {refreshingKnownValue ? (
          <RefreshCw className="mt-0.5 size-5 shrink-0 animate-spin text-muted-foreground" aria-hidden="true" />
        ) : (
          <AlertTriangle className={cn('mt-0.5 size-5 shrink-0', stale ? 'text-amber-500' : 'text-destructive')} aria-hidden="true" />
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <h2 className="font-semibold text-foreground">
            {refreshingKnownValue ? '正在更新余额' : (stale ? '余额数据可能已过期' : '余额暂时无法确认')}
          </h2>
          <p className="text-sm text-muted-foreground">
            {refreshingKnownValue ? '当前先显示上次成功数据；更新期间资金操作已暂停。' : (errorMessage || '读取余额失败；这不代表余额为 0。')}
          </p>
          {stale && lastUpdated && (
            <p className="text-xs text-muted-foreground">当前显示上次成功数据，最后更新：{lastUpdated}</p>
          )}
          {!compact && !refreshingKnownValue && (
            <p className="text-xs text-muted-foreground">
              读取失败不代表余额为 0。为避免错误扣费、充值或提现，相关操作已暂停；本次读取失败不会自动扣钱。
            </p>
          )}
        </div>
        {!refreshingKnownValue && (
          <Button type="button" variant="outline" size="sm" onClick={() => void onRetry()}>
            <RefreshCw className="size-4" />
            重新读取余额
          </Button>
        )}
      </div>
    </section>
  );
}
