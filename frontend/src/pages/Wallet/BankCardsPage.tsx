/**
 * 银行卡管理页
 * 绑定/解绑银行卡、设为默认
 */
import { useState, useEffect, useCallback } from 'react';
import { useIsMounted } from '@/hooks/useIsMounted';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { CreditCard, ArrowLeft, Loader2, Trash2, Star, Plus, ChevronDown, ChevronUp } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn, extractErrorMessage } from '@/lib/utils';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { toast } from 'sonner';

// ========== 类型定义 ==========

interface BankCard {
  id: string;
  bank_name: string;
  card_number_mask: string;  // masked, e.g. "****7890"
  card_holder: string;
  phone: string;
  is_default: boolean;
  created_at: string;
}

// ========== 常量 ==========

const BANK_LIST = [
  '中国工商银行', '中国建设银行', '中国农业银行', '中国银行', '交通银行',
  '招商银行', '中国邮储银行', '兴业银行', '中信银行', '浦发银行',
  '民生银行', '光大银行', '平安银行', '华夏银行', '广发银行',
] as const;

// ========== Luhn 校验 ==========

function luhnCheck(num: string): boolean {
  const digits = num.replace(/\s/g, '');
  if (!/^\d+$/.test(digits)) return false;
  let sum = 0;
  let alternate = false;
  for (let i = digits.length - 1; i >= 0; i--) {
    let n = parseInt(digits[i], 10);
    if (alternate) {
      n *= 2;
      if (n > 9) n -= 9;
    }
    sum += n;
    alternate = !alternate;
  }
  return sum % 10 === 0;
}

// ========== 主组件 ==========

export default function BankCardsPage() {
  const navigate = useEmbeddedNavigate();
  const isMounted = useIsMounted();

  // -- 卡列表 --
  const [cards, setCards] = useState<BankCard[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null); // card id being actioned

  // -- KYC 实名 --
  const [realName, setRealName] = useState('');
  const [kycVerified, setKycVerified] = useState<boolean | null>(null); // null=loading

  // -- 添加表单 --
  const [formOpen, setFormOpen] = useState(false);
  const [bankName, setBankName] = useState('');
  const [cardNumber, setCardNumber] = useState('');
  const [phone, setPhone] = useState('');
  const [submitting, setSubmitting] = useState(false);

  // -- 解绑确认 --
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  // ========== 数据获取 ==========

  const fetchCards = useCallback(async () => {
    setLoading(true);
    try {
      const res = await authFetch('/api/wallet/bank-cards');
      if (!isMounted()) return;
      if (res.ok) {
        const data = await res.json();
        setCards(data.cards ?? data ?? []);
      }
    } catch {
      // 静默
    } finally {
      if (isMounted()) setLoading(false);
    }
  }, [isMounted]);

  const fetchKyc = useCallback(async () => {
    try {
      const res = await authFetch('/api/wallet/withdrawal-kyc');
      if (!isMounted()) return;
      if (res.status === 403) {
        setKycVerified(false);
        return;
      }
      if (res.ok) {
        const data = await res.json();
        setRealName(data.real_name ?? '');
        setKycVerified(true);
      }
    } catch {
      if (isMounted()) setKycVerified(false);
    }
  }, [isMounted]);

  useEffect(() => {
    fetchCards();
    fetchKyc();
  }, [fetchCards, fetchKyc]);

  // ========== 操作 ==========

  const handleSetDefault = async (cardId: string) => {
    setActionLoading(cardId);
    try {
      const res = await authFetch(`/api/wallet/bank-cards/${cardId}/default`, { method: 'PATCH' });
      if (!isMounted()) return;
      if (res.ok) {
        toast.success('已设为默认银行卡');
        await fetchCards();
      } else {
        const err = await res.json().catch(() => null);
        toast.error(extractErrorMessage(err, '设置默认失败'));
      }
    } catch {
      toast.error('网络错误，请稍后重试');
    } finally {
      if (isMounted()) setActionLoading(null);
    }
  };

  const handleDelete = async (cardId: string) => {
    setActionLoading(cardId);
    try {
      const res = await authFetch(`/api/wallet/bank-cards/${cardId}`, { method: 'DELETE' });
      if (!isMounted()) return;
      if (res.ok) {
        toast.success('银行卡已解绑');
        setConfirmDeleteId(null);
        await fetchCards();
      } else {
        const err = await res.json().catch(() => null);
        toast.error(extractErrorMessage(err, '解绑失败'));
      }
    } catch {
      toast.error('网络错误，请稍后重试');
    } finally {
      if (isMounted()) setActionLoading(null);
    }
  };

  const handleSubmit = async () => {
    // 前端校验
    const trimmedCard = cardNumber.replace(/\s/g, '');
    if (!bankName) { toast.error('请选择开户行'); return; }
    if (trimmedCard.length < 16 || trimmedCard.length > 19 || !/^\d+$/.test(trimmedCard)) {
      toast.error('银行卡号格式不正确（16-19位数字）');
      return;
    }
    if (!luhnCheck(trimmedCard)) {
      toast.error('银行卡号校验未通过，请检查是否输入正确');
      return;
    }
    const trimmedPhone = phone.replace(/\s/g, '');
    if (!/^1\d{10}$/.test(trimmedPhone)) {
      toast.error('手机号格式不正确（11位）');
      return;
    }

    setSubmitting(true);
    try {
      const res = await authFetch('/api/wallet/bank-cards', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bank_name: bankName,
          card_number: trimmedCard,
          card_holder: realName,
          phone: trimmedPhone,
        }),
      });
      if (!isMounted()) return;

      if (res.ok) {
        toast.success('银行卡绑定成功');
        setBankName('');
        setCardNumber('');
        setPhone('');
        setFormOpen(false);
        await fetchCards();
      } else {
        const err = await res.json().catch(() => null);
        const status = res.status;
        if (status === 409) {
          toast.error('该银行卡已被绑定');
        } else if (status === 400) {
          toast.error(extractErrorMessage(err, '持卡人必须与实名认证一致'));
        } else if (status === 403) {
          toast.error('请先完成实名认证');
        } else {
          toast.error(extractErrorMessage(err, '绑定失败，请稍后重试'));
        }
      }
    } catch {
      toast.error('网络错误，请稍后重试');
    } finally {
      if (isMounted()) setSubmitting(false);
    }
  };

  // ========== 渲染 ==========

  return (
    <div className="space-y-6 p-3 sm:p-4 md:p-6 pb-24 lg:pb-6">
      {/* 标题 */}
      <div className="flex items-center gap-3">
        <button
          onClick={() => navigate('/wallet')}
          className="text-muted-foreground hover:text-foreground transition-colors"
        >
          <ArrowLeft className="size-5" />
        </button>
        <div>
          <h1 className="text-2xl font-bold text-foreground">银行卡管理</h1>
          <button
            onClick={() => navigate('/wallet')}
            className="text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            返回钱包
          </button>
        </div>
      </div>

      {/* KYC 未认证提示 */}
      {kycVerified === false && (
        <Card className="border-amber-500/30 bg-amber-500/5">
          <CardContent className="py-4">
            <p className="text-sm text-amber-400">
              请先完成实名认证后再绑定银行卡
            </p>
          </CardContent>
        </Card>
      )}

      {/* 卡列表 */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg flex items-center gap-2">
            <CreditCard className="size-5" />
            已绑定银行卡
          </CardTitle>
        </CardHeader>
        <CardContent>
          {loading ? (
            <div className="flex items-center justify-center py-12">
              <Loader2 className="size-6 animate-spin text-muted-foreground" />
            </div>
          ) : cards.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground text-sm">
              暂未绑定银行卡
            </div>
          ) : (
            <div className="space-y-3">
              {cards.map((card) => (
                <div
                  key={card.id}
                  className={cn(
                    'flex items-center justify-between rounded-lg border p-4 transition-colors',
                    card.is_default
                      ? 'border-foreground/30 bg-muted/30'
                      : 'border-border hover:bg-muted/20',
                  )}
                >
                  <div className="flex-1 min-w-0">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="text-sm font-medium text-foreground">
                        {card.bank_name}
                      </span>
                      <span className="text-sm text-muted-foreground tabular-nums">
                        {card.card_number_mask || '****'}
                      </span>
                      {card.is_default && (
                        <Badge className="bg-foreground/10 text-foreground border-foreground/20 text-xs">
                          默认
                        </Badge>
                      )}
                    </div>
                    <p className="text-xs text-muted-foreground mt-1">
                      持卡人: {card.card_holder}
                    </p>
                  </div>

                  <div className="flex items-center gap-2 shrink-0 ml-3">
                    {!card.is_default && (
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={actionLoading === card.id}
                        onClick={() => handleSetDefault(card.id)}
                      >
                        {actionLoading === card.id ? (
                          <Loader2 className="size-3.5 animate-spin" />
                        ) : (
                          <Star className="size-3.5" />
                        )}
                        <span className="ml-1">设为默认</span>
                      </Button>
                    )}
                    {confirmDeleteId === card.id ? (
                      <div className="flex items-center gap-1">
                        <Button
                          variant="destructive"
                          size="sm"
                          disabled={actionLoading === card.id}
                          onClick={() => handleDelete(card.id)}
                        >
                          {actionLoading === card.id ? (
                            <Loader2 className="size-3.5 animate-spin" />
                          ) : (
                            '确认解绑'
                          )}
                        </Button>
                        <Button
                          variant="outline"
                          size="sm"
                          onClick={() => setConfirmDeleteId(null)}
                        >
                          取消
                        </Button>
                      </div>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setConfirmDeleteId(card.id)}
                      >
                        <Trash2 className="size-3.5" />
                        <span className="ml-1">解绑</span>
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* 绑定新卡（可折叠） */}
      <Card>
        <CardHeader
          className="cursor-pointer select-none"
          onClick={() => {
            if (kycVerified !== false) setFormOpen(!formOpen);
          }}
        >
          <CardTitle className="text-lg flex items-center justify-between">
            <span className="flex items-center gap-2">
              <Plus className="size-5" />
              绑定新银行卡
            </span>
            {formOpen ? (
              <ChevronUp className="size-5 text-muted-foreground" />
            ) : (
              <ChevronDown className="size-5 text-muted-foreground" />
            )}
          </CardTitle>
        </CardHeader>
        {formOpen && kycVerified !== false && (
          <CardContent className="space-y-4 pt-0">
            {/* 持卡人（只读） */}
            <div className="space-y-2">
              <Label htmlFor="card_holder">持卡人</Label>
              <Input
                id="card_holder"
                value={realName}
                readOnly
                disabled
                placeholder={kycVerified === null ? '加载中...' : ''}
                className="bg-muted/40"
              />
              <p className="text-xs text-muted-foreground">
                自动填入实名认证姓名，不可修改
              </p>
            </div>

            {/* 开户行 */}
            <div className="space-y-2">
              <Label htmlFor="bank_name">开户行</Label>
              {/* 可搜索:15 家银行 · Review 裁决提进第一批(资金页误选后果=提现失败,打两个字比滚动稳) */}
              <SearchableSelect
                id="bank_name"
                value={bankName}
                onChange={setBankName}
                placeholder="请选择开户银行"
                searchPlaceholder="输入银行名，如「招商」"
                emptyText="没有匹配的银行"
                options={BANK_LIST.map((bank) => ({ value: bank, label: bank }))}
              />
            </div>

            {/* 卡号 */}
            <div className="space-y-2">
              <Label htmlFor="card_number">银行卡号</Label>
              <Input
                id="card_number"
                value={cardNumber}
                onChange={(e) => setCardNumber(e.target.value.replace(/[^\d\s]/g, ''))}
                placeholder="请输入16-19位银行卡号"
                maxLength={23}
                inputMode="numeric"
              />
            </div>

            {/* 预留手机号 */}
            <div className="space-y-2">
              <Label htmlFor="phone">预留手机号</Label>
              <Input
                id="phone"
                value={phone}
                onChange={(e) => setPhone(e.target.value.replace(/[^\d]/g, ''))}
                placeholder="请输入11位手机号"
                maxLength={11}
                inputMode="numeric"
              />
            </div>

            {/* 提交 */}
            <Button
              className="w-full bg-foreground text-background hover:bg-foreground/90"
              size="lg"
              disabled={submitting || !realName || kycVerified !== true}
              onClick={handleSubmit}
            >
              {submitting ? (
                <>
                  <Loader2 className="size-4 animate-spin mr-2" />
                  绑定中...
                </>
              ) : (
                '绑定银行卡'
              )}
            </Button>
          </CardContent>
        )}
      </Card>
    </div>
  );
}
