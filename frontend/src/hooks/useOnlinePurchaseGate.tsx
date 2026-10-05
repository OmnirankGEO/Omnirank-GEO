/**
 * 线上购买门控 hook(2026-07-29)
 *
 * 用法(各分散购买入口统一走这个,点击 → 弹窗提示):
 *
 *   const { canPurchase, guard, gateDialog } = useOnlinePurchaseGate();
 *   <Button onClick={() => guard(() => doPurchase())}>去充值</Button>
 *   {gateDialog}
 *
 * 判定只读 wallet.canPurchaseOnline(后端 GET /api/wallet 的 can_purchase_online),
 * 前端不自行拼两级逻辑。放行时 guard 直接调用原动作,行为与今日逐字节一致。
 */
import { useCallback, useState } from 'react';
import { useWallet } from '@/context/WalletContext';
import ContactReferrerDialog from '@/components/wallet/ContactReferrerDialog';

export function useOnlinePurchaseGate() {
  const wallet = useWallet();
  const [blockedOpen, setBlockedOpen] = useState(false);
  const canPurchase = wallet.canPurchaseOnline !== false;

  /** 被拦则弹窗并返回 false;放行则执行 action 并返回 true。 */
  const guard = useCallback((action?: () => void): boolean => {
    if (!canPurchase) {
      setBlockedOpen(true);
      return false;
    }
    action?.();
    return true;
  }, [canPurchase]);

  const gateDialog = (
    <ContactReferrerDialog open={blockedOpen} onOpenChange={setBlockedOpen} />
  );

  return { canPurchase, guard, gateDialog, showBlocked: () => setBlockedOpen(true) };
}
