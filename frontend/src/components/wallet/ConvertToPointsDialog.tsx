/**
 * V3.3.1 服务费转充值积分弹窗
 *
 * 显示:
 *   - 待转换金额 + 月度可用额度
 *   - 将获得:充值积分 + 赠送积分(90 天)
 *   - 不可撤销 / 不可再提现 / bonus 不可提现外采 提示
 *   - 425 退款期未过 / 402 月度上限超出 错误处理
 *
 * 关联:
 * - 决策书 §11.3
 * - 后端 api/service_fee_api.py:POST /convert + /convert-large
 */

import { useState } from 'react';
import { toast } from 'sonner';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AlertTriangle, Sparkles } from 'lucide-react';
import {
  convertServiceFee, requestLargeConversion,
} from '@/lib/serviceFeeApi';

export interface ConvertToPointsDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  settledYuan: number;
  bonusRate: number;
  monthlyAvailable: number;
  onSuccess?: () => void;
}

export function ConvertToPointsDialog({
  open, onOpenChange, settledYuan, bonusRate, monthlyAvailable, onSuccess,
}: ConvertToPointsDialogProps) {
  const [amount, setAmount] = useState<string>('');
  const [submitting, setSubmitting] = useState(false);
  const [largeMode, setLargeMode] = useState(false);
  const [largeReason, setLargeReason] = useState('');

  const amountNum = Number(amount) || 0;
  const paidPoints = Math.floor(amountNum * 130);
  const bonusPoints = Math.floor(amountNum * 130 * bonusRate);
  const exceedQuota = amountNum > monthlyAvailable;
  const exceedSettled = amountNum > settledYuan;
  const canSubmit = amountNum > 0 && !exceedSettled && (!exceedQuota || largeMode);

  const handleSubmit = async () => {
    if (submitting || !canSubmit) return;
    setSubmitting(true);
    try {
      if (largeMode) {
        const r = await requestLargeConversion(amountNum, largeReason || '超月度上限');
        toast.success(`大额转换审批已提交 ${r.review_id} · SLA ${r.sla_days}`);
      } else {
        const r = await convertServiceFee(amountNum);
        toast.success(
          `转换成功 ${r.conversion_order_id} · 入账 ${r.paid_points_granted} 充值算力 + ${r.bonus_points_granted} 赠送算力`,
          { duration: 6000 }
        );
      }
      onOpenChange(false);
      setAmount('');
      onSuccess?.();
    } catch (err: any) {
      const code = err?.error || err?.code;
      if (code === 'monthly_quota_exceeded') {
        setLargeMode(true);
        toast.warning(`月度上限已用尽(可用 ¥${err.available_yuan}) · 可申请大额转换审批`);
      } else if (code === 'settled_records_within_refund_window') {
        toast.error(`部分余额未过 T+${err.min_age_days} 退款观察期 · 已可转换 ¥${err.ready_amount_yuan}`);
      } else if (code === 'insufficient_balance') {
        toast.error(`余额不足:可用 ¥${err.ready_amount_yuan ?? err.settled_balance_yuan ?? 0}`);
      } else {
        toast.error(err?.message || '转换失败 · 请稍后重试');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Sparkles className="h-5 w-5 text-amber-500" />
            转换为充值算力
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 rounded-md bg-muted/40 px-3 py-2 text-xs">
            <div>
              <div className="text-muted-foreground">可处理余额</div>
              <div className="font-mono text-sm">¥{settledYuan.toFixed(2)}</div>
            </div>
            <div>
              <div className="text-muted-foreground">本月剩余上限</div>
              <div className="font-mono text-sm">¥{monthlyAvailable.toFixed(2)}</div>
            </div>
          </div>

          <div>
            <Label htmlFor="amount">转换金额(元)</Label>
            <Input
              id="amount"
              type="number"
              step="0.01"
              min="0"
              max={settledYuan}
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder="请输入金额"
            />
          </div>

          {amountNum > 0 && (
            <div className="rounded-md border bg-card px-3 py-2 text-sm space-y-1">
              <div className="font-medium">您将获得</div>
              <div className="flex justify-between font-mono text-xs">
                <span>充值算力</span><span>{paidPoints.toLocaleString()}</span>
              </div>
              <div className="flex justify-between font-mono text-xs text-emerald-700 dark:text-emerald-400">
                <span>赠送算力(+{Math.round(bonusRate * 100)}% · 90 天有效)</span>
                <span>{bonusPoints.toLocaleString()}</span>
              </div>
            </div>
          )}

          {largeMode && (
            <div>
              <Label htmlFor="reason">大额转换理由(财务审核)</Label>
              <Input
                id="reason" value={largeReason}
                onChange={(e) => setLargeReason(e.target.value)}
                placeholder="请说明本次大额转换的业务原因"
              />
            </div>
          )}

          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs space-y-1 dark:border-amber-800/50 dark:bg-amber-950/30">
            <div className="flex items-start gap-1.5">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 text-amber-600 dark:text-amber-400 shrink-0" />
              <div className="space-y-1">
                <div className="font-medium">重要提示</div>
                <ul className="list-disc pl-4 space-y-0.5 text-muted-foreground">
                  <li>转换不可撤销</li>
                  <li>转换后不可再申请提现</li>
                  <li>充值算力可用于所有 AI 工具 + 媒体外采</li>
                  <li>赠送算力:不可提现 / 不可外采 / 90 天有效</li>
                  <li>异常退款时可能从后续服务费/算力消费抵扣</li>
                </ul>
              </div>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button onClick={handleSubmit} disabled={!canSubmit || submitting}>
            {submitting ? '提交中...' : largeMode ? '提交大额审批' : '确认转换'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
