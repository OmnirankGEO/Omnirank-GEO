/**
 * 套餐加充弹窗
 */

import { useState } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Loader2, Plus } from 'lucide-react';
import { toast } from 'sonner';

import * as managedApi from './api';

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  campaignId: number;
  onTopUp?: () => void;
}

const PRESET_AMOUNTS = [200, 500, 1000, 2000, 5000];

export function TopUpDialog({ open, onOpenChange, campaignId, onTopUp }: Props) {
  const [amount, setAmount] = useState<string>('500');
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit() {
    const n = parseFloat(amount);
    if (!Number.isFinite(n) || n <= 0) {
      toast.error('请输入有效金额');
      return;
    }
    if (n > 100000) {
      toast.error('单次加充上限 ¥100,000');
      return;
    }
    setSubmitting(true);
    try {
      const r = await managedApi.topUp(campaignId, n);
      toast.success(`已加充 ¥${n}，新余额 ¥${r.new_balance.toFixed(0)}`);
      onOpenChange(false);
      onTopUp?.();
    } catch (e: any) {
      if (e?.code === 'INSUFFICIENT_PAID_POINTS') {
        toast.error('充值算力不足，请先充值钱包');
      } else {
        toast.error(e?.message || '加充失败');
      }
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Plus className="h-4 w-4 text-primary" />
            套餐加充
          </DialogTitle>
          <DialogDescription>
            从您的充值算力消耗，加充后立即生效
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="grid grid-cols-5 gap-2">
            {PRESET_AMOUNTS.map(n => (
              <Button
                key={n}
                variant={amount === String(n) ? 'default' : 'outline'}
                size="sm"
                onClick={() => setAmount(String(n))}
              >
                ¥{n}
              </Button>
            ))}
          </div>

          <div>
            <label className="text-xs text-muted-foreground">自定义金额</label>
            <div className="flex items-center gap-2 mt-1">
              <span>¥</span>
              <input
                type="number"
                className="flex-1 rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                min={1}
                max={100000}
              />
            </div>
          </div>

          <div className="rounded border bg-amber-500/5 border-amber-500/30 p-2 text-[11px] text-amber-700 dark:text-amber-300">
            ⚠️ 充值即消费，套餐余额永久保留（不退款）
          </div>
        </div>

        <DialogFooter className="gap-2">
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={submitting}>取消</Button>
          <Button onClick={handleSubmit} disabled={submitting || !amount}>
            {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
            充值 ¥{amount}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
