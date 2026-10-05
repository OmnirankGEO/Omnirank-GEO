import { AlertTriangle, RefreshCw } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useWallet } from '@/context/WalletContext';

interface WalletUnavailableDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function WalletUnavailableDialog({ open, onOpenChange }: WalletUnavailableDialogProps) {
  const { status, errorMessage, lastUpdatedAt, refreshBalance } = useWallet();
  const stale = status === 'stale';
  const updatedAt = lastUpdatedAt ? new Date(lastUpdatedAt).toLocaleString() : null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <AlertTriangle className="size-5 text-amber-500" aria-hidden="true" />
            {stale ? '请先更新余额' : '暂时无法确认余额'}
          </DialogTitle>
          <DialogDescription>
            {errorMessage || '余额还没有读取成功。'}这不代表余额为 0；为避免误扣或重复操作，本次动作尚未发起。
          </DialogDescription>
        </DialogHeader>
        {stale && updatedAt && (
          <p className="text-sm text-muted-foreground">当前数据最后更新于 {updatedAt}。</p>
        )}
        <p className="text-sm text-muted-foreground">请重新读取余额；仍失败时请联系系统管理员。</p>
        <DialogFooter>
          <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
          <Button
            type="button"
            onClick={() => {
              // 先关闭本次动作门禁，避免余额恢复为 ready 时自动续接原危险动作。
              onOpenChange(false);
              void refreshBalance();
            }}
          >
            <RefreshCw className="size-4" aria-hidden="true" />
            重新读取余额
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
