/**
 * 算力不足弹窗
 * 当余额不够时引导用户充值
 */
import { useNavigate } from 'react-router-dom';
import { AlertTriangle } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { useOnlinePurchaseGate } from '@/hooks/useOnlinePurchaseGate';

interface InsufficientDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  needed: number;
  currentBalance: number;
}

export default function InsufficientDialog({
  open,
  onOpenChange,
  needed,
  currentBalance,
}: InsufficientDialogProps) {
  const navigate = useNavigate();
  // [客户线上购买门控 2026-07-29] 被禁客户点"去充值"改为弹提示,不跳购买页。
  const { guard, gateDialog } = useOnlinePurchaseGate();
  const deficit = needed - currentBalance;

  return (
    <>
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <AlertTriangle className="size-5 text-red-400" />
            <DialogTitle>算力不够</DialogTitle>
          </div>
          <DialogDescription>
            还差 {deficit.toLocaleString()}，充值后再继续
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3 py-2">
          <div className="flex justify-between items-center text-sm">
            <span className="text-muted-foreground">这一步要用</span>
            <span className="font-medium text-foreground tabular-nums">{needed.toLocaleString()} 算力</span>
          </div>
          <div className="flex justify-between items-center text-sm">
            <span className="text-muted-foreground">你现在有</span>
            <span className="font-medium text-foreground tabular-nums">{currentBalance.toLocaleString()}</span>
          </div>
          <div className="flex justify-between items-center text-sm border-t border-border/50 pt-3">
            <span className="text-muted-foreground">还差</span>
            <span className="font-medium text-red-400 tabular-nums">{deficit.toLocaleString()}</span>
          </div>
        </div>

        <DialogFooter className="gap-2 sm:gap-0">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            className="bg-foreground text-background hover:bg-foreground/90"
            onClick={() => {
              guard(() => {
                onOpenChange(false);
                // [2026-06-07] 跳新版 /customer/recharge(BuyCredit · 已绑客户走邀请服务商系数 SKU)
                // 不跳老 /wallet/recharge(平台硬编码价 · 不绑服务商系数 · 已废弃)
                navigate('/customer/recharge');
              });
            }}
          >
            去充值
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    {gateDialog}
    </>
  );
}
