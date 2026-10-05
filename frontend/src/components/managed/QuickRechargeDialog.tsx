/**
 * QuickRechargeDialog — 就地快速充值（积分不足时弹出，用户充完即可原地继续）
 *
 * 触发场景：
 *   用户在某个功能 Dialog（如 WordPlanDialog）点"启动"→ 后端返 INSUFFICIENT_PAID_POINTS
 *   或前端预检余额 < 所需 → 立即打开本 Dialog
 *
 * 购买动作统一跳转 /customer/recharge,由已发布零售目录创建持久化报价后下单。
 * 旧轮询仅保留用于兼容已创建、仍在支付中的历史订单。
 */

import { useEffect, useRef, useState, type CSSProperties } from 'react';
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Loader2, CheckCircle2, QrCode, Smartphone } from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/lib/api';
import { useWallet } from '@/context/WalletContext';
import { WalletStatusNotice } from '@/components/wallet/WalletStatusNotice';
import { useOnlinePurchaseGate } from '@/hooks/useOnlinePurchaseGate';

interface Props {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  /** 原操作需要的人民币金额 (元) */
  requiredYuan: number;
  /** 提示语, 比如 "启动托管套餐需要" */
  purposeHint?: string;
  /** 充值并到账后回调, 调用方可重试原操作 */
  onPaid?: () => void;
  /**
   * 主题 style (可选, 2026-05-13 加)
   * Dialog 走 Portal, 外层 themeStyle 注入失效。当社媒板块 (Social Studio) 调用时
   * 传 useSocialStudioTheme() 让 ss-* token 在 Portal 内生效, 否则用全站 shadcn token (默认 GEO 板块视觉)
   */
  themeStyle?: CSSProperties;
}

// 积分汇率 (与 RechargePage 一致)
const POINTS_PER_YUAN = 130;

// 推荐快速档位（覆盖大多数托管场景）
const QUICK_PRESETS = [200, 500, 1000, 2000];

function ceilToPreset(yuan: number): number {
  // 给用户一个 >= 所需金额的合理档位, 向上取到 100 的倍数
  if (yuan <= 0) return 100;
  return Math.ceil(yuan / 100) * 100;
}

export function QuickRechargeDialog({
  open, onOpenChange, requiredYuan, purposeHint, onPaid, themeStyle,
}: Props) {
  const {
    // [BUG-2 2026-07-27] 余额展示必须用合并值,否则 V3.5 客户看到 0 会重复充值
    paidPointsDisplayed,
    refreshBalance,
    status: walletStatus,
    errorMessage: walletError,
    lastUpdatedAt: walletLastUpdatedAt,
  } = useWallet();
  const { guard: guardOnlinePurchase, gateDialog: onlinePurchaseGateDialog } = useOnlinePurchaseGate();
  const paidYuan = paidPointsDisplayed / POINTS_PER_YUAN;
  const shortfallYuan = Math.max(0, requiredYuan - paidYuan);
  const suggestedYuan = ceilToPreset(shortfallYuan);

  const [amount, setAmount] = useState<number>(suggestedYuan);
  const [submitting, setSubmitting] = useState(false);
  const [paymentUrl, setPaymentUrl] = useState<string | null>(null);
  const [orderId, setOrderId] = useState<string | null>(null);
  const [status, setStatus] = useState<'idle' | 'pending' | 'paid' | 'failed'>('idle');
  const pollTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const completionTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollSessionRef = useRef(0);

  // 重置（每次打开）
  useEffect(() => {
    pollSessionRef.current += 1;
    if (open) {
      setAmount(ceilToPreset(Math.max(0, requiredYuan - paidYuan)));
      setPaymentUrl(null);
      setOrderId(null);
      setStatus('idle');
    } else {
      // 关闭时清理轮询
      if (pollTimerRef.current) {
        clearInterval(pollTimerRef.current);
        pollTimerRef.current = null;
      }
      if (completionTimerRef.current) {
        clearTimeout(completionTimerRef.current);
        completionTimerRef.current = null;
      }
    }
  }, [open, requiredYuan, paidYuan]);

  // 组件卸载时清理
  useEffect(() => {
    return () => {
      pollSessionRef.current += 1;
      if (pollTimerRef.current) clearInterval(pollTimerRef.current);
      if (completionTimerRef.current) clearTimeout(completionTimerRef.current);
    };
  }, []);

  const startPolling = (oid: string) => {
    if (pollTimerRef.current) clearInterval(pollTimerRef.current);
    const pollSession = ++pollSessionRef.current;
    let tries = 0;
    const MAX_TRIES = 120; // 约 6 分钟
    pollTimerRef.current = setInterval(async () => {
      tries++;
      if (tries > MAX_TRIES) {
        if (pollTimerRef.current) clearInterval(pollTimerRef.current);
        setStatus('failed');
        toast.error('支付超时，请刷新钱包确认是否到账');
        return;
      }
      try {
        // [GEO-R5-CAN-014] 对齐单一 order-status 契约(RechargePage/BuyCredit/InventoryCenter 同款)
        // 原轮询 GET /api/wallet/recharge/{oid} 是不存在的路由 → r.ok 恒 false 每 tick 早退 → 永远轮询超时;
        // 且 order-status 返回 {data:{status,...}} 而非 payment_status。该端点会主动 query 微信兜底救援。
        const r = await authFetch(`/api/wallet/order-status/${oid}`);
        if (!r.ok) return;
        const d = await r.json();
        if (pollSession !== pollSessionRef.current) return;
        if (d?.data?.status === 'paid') {
          if (pollTimerRef.current) clearInterval(pollTimerRef.current);
          setStatus('paid');
          await refreshBalance();
          toast.success('支付成功，算力已到账');
          // 1 秒后关闭 + 回调
          completionTimerRef.current = setTimeout(() => {
            onOpenChange(false);
            onPaid?.();
          }, 1000);
        }
      } catch { /* 静默, 下次再试 */ }
    }, 3000);
  };

  const handleRecharge = () => {
    // [客户线上购买门控 2026-07-29] 被禁客户只弹提示，不跳购买页。
    if (!guardOnlinePurchase()) return;
    if (!Number.isFinite(amount) || amount < 1) {
      toast.error('请输入有效金额');
      return;
    }
    toast.info('正在前往新版购买算力页 · 将按已发布价目表报价');
    onOpenChange(false);
    window.location.href = '/customer/recharge';
  };

  return (
    <>
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md" style={themeStyle}>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            💰 快速充值
          </DialogTitle>
          <DialogDescription>
            {purposeHint
              ? `${purposeHint} ¥${requiredYuan.toFixed(0)}`
              : '为继续当前操作, 请完成充值'}
          </DialogDescription>
        </DialogHeader>

        {/* 余额/差额提示 */}
        {status === 'idle' && (
          <>
            <WalletStatusNotice
              status={walletStatus}
              errorMessage={walletError}
              lastUpdatedAt={walletLastUpdatedAt}
              onRetry={refreshBalance}
              compact
            />
            <div className="rounded-lg border border-border bg-muted/20 p-3 text-xs space-y-1">
              <div className="flex justify-between">
                <span className="text-muted-foreground">当前充值算力余额</span>
                <span>{walletStatus === 'ready' || walletStatus === 'stale' || walletLastUpdatedAt !== null ? `¥${paidYuan.toFixed(2)}（${paidPointsDisplayed.toLocaleString()} 算力）` : '待确认'}</span>
              </div>
              <div className="flex justify-between">
                <span className="text-muted-foreground">本次操作所需</span>
                <span className="text-foreground font-medium">¥{requiredYuan.toFixed(0)}</span>
              </div>
              {shortfallYuan > 0 && (
                <div className="flex justify-between pt-1 border-t border-border">
                  <span className="text-amber-400">还需充值</span>
                  <span className="text-amber-400 font-semibold">
                    ≥ ¥{shortfallYuan.toFixed(0)}
                  </span>
                </div>
              )}
            </div>

            {/* 档位快选 */}
            <div className="space-y-2">
              <div className="text-xs text-muted-foreground">选择充值金额</div>
              <div className="grid grid-cols-4 gap-2">
                {QUICK_PRESETS.map(p => (
                  <Button
                    key={p}
                    variant={amount === p ? 'default' : 'outline'}
                    size="sm"
                    onClick={() => setAmount(p)}
                    disabled={p < shortfallYuan}
                    className="relative"
                  >
                    ¥{p}
                    {p === suggestedYuan && (
                      <span className="absolute -top-2 left-1/2 -translate-x-1/2 text-[9px] bg-emerald-500 text-white rounded px-1 leading-3">
                        推荐
                      </span>
                    )}
                  </Button>
                ))}
              </div>

              {/* 自定义 */}
              <div className="flex items-center gap-2 pt-1">
                <span className="text-xs text-muted-foreground">自定义</span>
                <div className="flex-1 flex items-center gap-1 border border-border rounded-md px-2 bg-background">
                  <span className="text-xs">¥</span>
                  <input
                    type="number"
                    className="flex-1 bg-transparent py-1.5 text-sm outline-none"
                    value={amount}
                    onChange={e => setAmount(Math.max(0, Number(e.target.value) || 0))}
                    min={shortfallYuan}
                    max={10000}
                  />
                </div>
                {amount < shortfallYuan && (
                  <span className="text-[10px] text-red-400">不够本次</span>
                )}
              </div>
            </div>
          </>
        )}

        {/* 支付中 */}
        {status === 'pending' && (
          <div className="space-y-3 text-center py-2">
            {paymentUrl ? (
              <>
                <QrCode className="h-5 w-5 text-muted-foreground mx-auto" />
                <div className="text-sm">用微信扫码支付 ¥{amount}</div>
                <div className="mx-auto w-40 h-40 border border-border rounded-md flex items-center justify-center bg-white p-1">
                  <img
                    src={`https://api.qrserver.com/v1/create-qr-code/?size=160x160&data=${encodeURIComponent(paymentUrl)}`}
                    alt="微信支付二维码"
                    className="w-full h-full"
                  />
                </div>
                <div className="flex items-center justify-center gap-1 text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  等待支付中…
                </div>
              </>
            ) : (
              <>
                <Smartphone className="h-5 w-5 text-muted-foreground mx-auto" />
                <div className="text-sm">已打开微信支付页，请在新标签完成支付</div>
                <div className="flex items-center justify-center gap-1 text-xs text-muted-foreground">
                  <Loader2 className="h-3 w-3 animate-spin" />
                  等待支付完成…
                </div>
              </>
            )}
            <div className="text-[10px] text-muted-foreground">
              订单号 {orderId?.slice(0, 12)}…
            </div>
          </div>
        )}

        {/* 支付成功 */}
        {status === 'paid' && (
          <div className="text-center py-4 space-y-2">
            <CheckCircle2 className="h-10 w-10 text-emerald-400 mx-auto" />
            <div className="text-sm font-medium">支付成功！算力已到账</div>
            <div className="text-xs text-muted-foreground">即将返回继续原操作…</div>
          </div>
        )}

        {status === 'failed' && (
          <div className="text-center py-4 text-sm text-red-400">
            支付状态异常，请前往钱包核对
          </div>
        )}

        <DialogFooter className="gap-2">
          {status === 'idle' && (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
              <Button
                onClick={handleRecharge}
                disabled={submitting || amount < shortfallYuan || amount < 1}
              >
                {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
                前往购买算力 ¥{amount}
              </Button>
            </>
          )}
          {status === 'pending' && (
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              稍后再说
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
    {onlinePurchaseGateDialog}
    </>
  );
}
