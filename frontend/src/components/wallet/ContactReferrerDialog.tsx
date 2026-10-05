/**
 * 线上购买被关闭时的提示弹窗(2026-07-29)
 *
 * 客户侧唯一拦截弹窗。文案全部取自 onlinePurchaseGate.ts,零内部术语、
 * 零服务商信息 —— 详见工单 §0 白标铁律。
 */
import { Info } from 'lucide-react';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import {
  ONLINE_PURCHASE_BLOCKED_HINT,
  ONLINE_PURCHASE_BLOCKED_MESSAGE,
  ONLINE_PURCHASE_BLOCKED_TITLE,
} from './onlinePurchaseGate';

interface ContactReferrerDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export default function ContactReferrerDialog({ open, onOpenChange }: ContactReferrerDialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm" data-testid="online-purchase-blocked-dialog">
        <DialogHeader>
          <div className="flex items-center gap-2">
            <Info className="size-5 text-primary" />
            <DialogTitle>{ONLINE_PURCHASE_BLOCKED_TITLE}</DialogTitle>
          </div>
          <DialogDescription className="pt-2 text-left leading-relaxed">
            {ONLINE_PURCHASE_BLOCKED_MESSAGE}
          </DialogDescription>
        </DialogHeader>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {ONLINE_PURCHASE_BLOCKED_HINT}
        </p>
        <DialogFooter>
          <Button className="w-full" onClick={() => onOpenChange(false)}>
            我知道了
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
