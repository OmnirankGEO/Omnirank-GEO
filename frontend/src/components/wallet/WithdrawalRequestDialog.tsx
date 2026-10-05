/**
 * V3.3.1 服务费提现申请弹窗
 *
 * 必须前置:KYC 通过 + 实名收款账户 + 周频次锁 + 单笔 ≥ ¥100
 *
 * 显示:
 *   - 可提现金额 + 申请金额输入
 *   - 收款账户(已实名)
 *   - 审核须知:SLA 5-10 天 + 月累计 > ¥800 需发票 + > ¥1000 双签
 *   - 平台可拒绝/延迟/部分结算
 *
 * 关联:
 * - 决策书 §11.4
 * - 后端 api/service_fee_api.py:POST /withdraw-request
 */

import { useState } from 'react';
import { toast } from 'sonner';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { AlertCircle, Banknote, ShieldCheck } from 'lucide-react';
import { requestWithdrawal } from '@/lib/serviceFeeApi';

export interface WithdrawalRequestDialogProps {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  settledYuan: number;
  minAmount: number;
  kycPassed: boolean;
  bankVerified: boolean;
  onSuccess?: () => void;
}

export function WithdrawalRequestDialog({
  open, onOpenChange, settledYuan, minAmount, kycPassed, bankVerified, onSuccess,
}: WithdrawalRequestDialogProps) {
  const [amount, setAmount] = useState('');
  const [bankAccount, setBankAccount] = useState('招商银行 *** 1234');
  const [submitting, setSubmitting] = useState(false);

  const amountNum = Number(amount) || 0;
  const tooSmall = amountNum < minAmount;
  const tooLarge = amountNum > settledYuan;
  const canSubmit = !tooSmall && !tooLarge && kycPassed && bankVerified;

  const handleSubmit = async () => {
    if (submitting || !canSubmit) return;
    setSubmitting(true);
    try {
      const r = await requestWithdrawal(amountNum, bankAccount);
      toast.success(
        `提现申请已提交 ${r.settlement_code} · 审核 ${r.review_sla_days} 个工作日` +
        (r.invoice_required ? ' · 需上传发票' : '') +
        (r.requires_dual_sign ? ' · 大额需双签' : ''),
        { duration: 8000 }
      );
      onOpenChange(false);
      setAmount('');
      onSuccess?.();
    } catch (err: any) {
      const code = err?.error || err?.code;
      if (code === 'withdrawal_rate_limit') {
        toast.error(`本周已有提现申请待审核(限 ${err.weekly_max} 次/周)`);
      } else if (code === 'withdrawal_precondition_failed') {
        toast.error(err?.message || '提现前提条件不满足');
      } else if (code === 'insufficient_balance') {
        toast.error(`可结算余额 ¥${err.settled_balance_yuan ?? settledYuan} 不足`);
      } else {
        toast.error(err?.message || '提现申请失败');
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
            <Banknote className="h-5 w-5 text-emerald-600" />
            申请人工提现
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-4">
          <div className="rounded-md bg-muted/40 px-3 py-2 text-xs">
            <div className="text-muted-foreground">可提现金额</div>
            <div className="font-mono text-base">¥{settledYuan.toFixed(2)}</div>
          </div>

          <div>
            <Label htmlFor="wd-amount">申请金额(元)</Label>
            <Input
              id="wd-amount"
              type="number" step="0.01" min={minAmount} max={settledYuan}
              value={amount}
              onChange={(e) => setAmount(e.target.value)}
              placeholder={`≥ ¥${minAmount}`}
            />
            {amountNum > 0 && tooSmall && (
              <div className="text-xs text-red-500 mt-1">单笔 ≥ ¥{minAmount}</div>
            )}
            {amountNum > 0 && tooLarge && (
              <div className="text-xs text-red-500 mt-1">金额超出可处理余额</div>
            )}
          </div>

          <div>
            <Label htmlFor="bank">收款账户(已实名)</Label>
            <Input
              id="bank" value={bankAccount}
              onChange={(e) => setBankAccount(e.target.value)}
              placeholder="如:招商银行 *** 1234"
              disabled={!bankVerified}
            />
          </div>

          {(!kycPassed || !bankVerified) && (
            <div className="rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs dark:border-red-800 dark:bg-red-950/40">
              <div className="flex items-start gap-1.5">
                <AlertCircle className="mt-0.5 h-3.5 w-3.5 text-red-500 shrink-0" />
                <div>
                  <div className="font-medium text-red-700 dark:text-red-300">
                    提现前置条件未完成
                  </div>
                  <ul className="list-disc pl-4 mt-1 text-red-600/80 dark:text-red-300/80">
                    {!kycPassed && <li>请先完成实名认证(KYC)</li>}
                    {!bankVerified && <li>请先绑定实名收款账户</li>}
                  </ul>
                </div>
              </div>
            </div>
          )}

          <div className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs space-y-1 dark:border-amber-800/50 dark:bg-amber-950/30">
            <div className="flex items-start gap-1.5">
              <ShieldCheck className="mt-0.5 h-3.5 w-3.5 text-amber-600 shrink-0" />
              <div className="space-y-0.5">
                <div className="font-medium">提现审核须知</div>
                <ul className="list-disc pl-4 text-muted-foreground space-y-0.5">
                  <li>需通过 KYC + 绑定实名收款账户</li>
                  <li>平台核验:服务关系 / 退款率 / 税务</li>
                  <li>审核 SLA:5-10 工作日</li>
                  <li>月累计 &gt; ¥800 需提供个人税务材料</li>
                  <li>单笔 &gt; ¥1000 需财务 + CTO 双签</li>
                  <li>平台可拒绝、延迟或部分结算</li>
                  <li>财务审核通过后线下结算</li>
                </ul>
              </div>
            </div>
          </div>
        </div>

        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>取消</Button>
          <Button onClick={handleSubmit} disabled={!canSubmit || submitting}>
            {submitting ? '提交中...' : '提交申请'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
