/**
 * 提现弹窗
 * 选择银行卡、输入金额、计算手续费、提交提现申请
 */
import { useState, useEffect, useCallback } from 'react';
import { useIsMounted } from '@/hooks/useIsMounted';
import { Loader2 } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { extractErrorMessage } from '@/lib/utils';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { toast } from 'sonner';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

// ========== 类型 ==========

interface BankCard {
  id: string;
  bank_name: string;
  card_number_mask: string; // masked
  is_default: boolean;
}

interface Props {
  open: boolean;
  onClose: () => void;
  commissionPoints: number;
  onSuccess: () => void;
}

const EXCHANGE_RATE = 130;
const FEE_RATE = 0.01;
const MIN_AMOUNT = 100;

export default function WithdrawalDialog({ open, onClose, commissionPoints, onSuccess }: Props) {
  const isMounted = useIsMounted();

  const availableYuan = Math.floor((commissionPoints / EXCHANGE_RATE) * 100) / 100;

  // -- 银行卡列表 --
  const [cards, setCards] = useState<BankCard[]>([]);
  const [cardsLoading, setCardsLoading] = useState(false);
  const [selectedCardId, setSelectedCardId] = useState('');

  // -- 金额 --
  const [amountStr, setAmountStr] = useState('');
  const amount = Number(amountStr) || 0;
  const fee = Math.round(amount * FEE_RATE * 100) / 100;
  const actual = Math.round((amount - fee) * 100) / 100;

  // -- 提交 --
  const [submitting, setSubmitting] = useState(false);

  // 验证
  const amountValid = amount >= MIN_AMOUNT && amount <= availableYuan;
  const canSubmit = amountValid && selectedCardId && !submitting;

  // ========== 获取银行卡 ==========

  const fetchCards = useCallback(async () => {
    setCardsLoading(true);
    try {
      const res = await authFetch('/api/wallet/bank-cards');
      if (!isMounted()) return;
      if (res.ok) {
        const data = await res.json();
        const list: BankCard[] = data.cards ?? data ?? [];
        setCards(list);
        // 默认选中 is_default 或第一张
        const defaultCard = list.find((c) => c.is_default) ?? list[0];
        if (defaultCard) setSelectedCardId(defaultCard.id);
      }
    } catch {
      // 静默
    } finally {
      if (isMounted()) setCardsLoading(false);
    }
  }, [isMounted]);

  useEffect(() => {
    if (open) {
      fetchCards();
      setAmountStr('');
    }
  }, [open, fetchCards]);

  // ========== 提交 ==========

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    try {
      const idempotencyKey = safeRandomUUID();
      const res = await authFetch('/api/wallet/withdrawal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          amount_yuan: amount,
          bank_card_id: selectedCardId,
          idempotency_key: idempotencyKey,
        }),
      });
      if (!isMounted()) return;
      if (res.ok) {
        toast.success('提现申请已提交，请等待审核');
        onSuccess();
        onClose();
      } else {
        const err = await res.json().catch(() => null);
        toast.error(extractErrorMessage(err, '提现失败，请稍后重试'));
      }
    } catch {
      toast.error('网络错误，请稍后重试');
    } finally {
      if (isMounted()) setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>申请提现</DialogTitle>
          <DialogDescription>
            可提现金额: <span className="font-semibold text-foreground">&yen;{availableYuan.toFixed(2)}</span>
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 pt-2">
          {/* 银行卡选择 */}
          <div className="space-y-2">
            <Label>收款银行卡</Label>
            {cardsLoading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground py-2">
                <Loader2 className="size-4 animate-spin" />
                加载银行卡...
              </div>
            ) : cards.length === 0 ? (
              <p className="text-sm text-muted-foreground">暂无银行卡</p>
            ) : (
              <Select value={selectedCardId} onValueChange={setSelectedCardId}>
                <SelectTrigger>
                  <SelectValue placeholder="请选择银行卡" />
                </SelectTrigger>
                <SelectContent>
                  {cards.map((card) => (
                    <SelectItem key={card.id} value={card.id}>
                      {card.bank_name} {card.card_number_mask || '****'}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          {/* 提现金额 */}
          <div className="space-y-2">
            <Label htmlFor="withdrawal-amount">提现金额 (元)</Label>
            <Input
              id="withdrawal-amount"
              type="number"
              min={MIN_AMOUNT}
              max={availableYuan}
              step="0.01"
              value={amountStr}
              onChange={(e) => setAmountStr(e.target.value)}
              placeholder={`最低 ${MIN_AMOUNT} 元，最高 ${availableYuan.toFixed(2)} 元`}
              inputMode="decimal"
            />
            {amountStr && amount < MIN_AMOUNT && (
              <p className="text-xs text-red-400">最低提现金额 {MIN_AMOUNT} 元</p>
            )}
            {amountStr && amount > availableYuan && (
              <p className="text-xs text-red-400">超出可提现余额</p>
            )}
          </div>

          {/* 费用明细 */}
          {amount > 0 && (
            <div className="rounded-lg bg-muted/40 p-3 space-y-1.5 text-sm">
              <div className="flex justify-between">
                <span className="text-muted-foreground">提现金额</span>
                <span className="text-foreground tabular-nums">&yen;{amount.toFixed(2)}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">手续费 (1%)</span>
                <span className="text-red-400 tabular-nums">-&yen;{fee.toFixed(2)}</span>
              </div>
              <div className="border-t border-border/50 pt-1.5 flex justify-between font-medium">
                <span className="text-muted-foreground">实际到账</span>
                <span className="text-foreground tabular-nums">&yen;{actual.toFixed(2)}</span>
              </div>
            </div>
          )}

          {/* 提交 */}
          <Button
            className="w-full bg-foreground text-background hover:bg-foreground/90"
            size="lg"
            disabled={!canSubmit}
            onClick={handleSubmit}
          >
            {submitting ? (
              <>
                <Loader2 className="size-4 animate-spin mr-2" />
                提交中...
              </>
            ) : (
              '确认提现'
            )}
          </Button>
        </div>
      </DialogContent>
    </Dialog>
  );
}
