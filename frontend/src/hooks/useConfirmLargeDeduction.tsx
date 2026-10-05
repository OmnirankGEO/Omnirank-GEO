/**
 * useConfirmLargeDeduction — 大额扣费轻量确认（不是 Dialog）
 *
 * 场景: 按钮标价但总额大（>= 阈值）时，点后 toast 显示"X 秒后自动执行，点此取消"
 * 用户可以"慢一步"反悔，避免误触大额扣费但又不打断体验（不是 Dialog 那种强阻断）
 *
 * v1_3 · CTO-15.1 · 2026-04-19
 * v1_4 · 2026-07-27 · 微工单 WORKORDER_UI_DEDUCTION_TOAST:
 *        改用 toast.custom() 渲染 <DeductionCountdownToast />（v7 卡片容器 + 会动的
 *        倒计时 + shadcn 取消按钮）。**对外 API / 阈值 / 时长 / 小额直通 / 取消语义
 *        全部不变**，调用方零改动；文件由 .ts 改名 .tsx 只为容纳 JSX
 *        （import 路径不带扩展名，四个调用方不受影响）。
 *
 * @example
 *   const confirmLarge = useConfirmLargeDeduction();
 *   const onClick = () => {
 *     confirmLarge(totalPoints, () => generateArticles(ids));
 *   };
 */

import { useCallback } from 'react';
import { toast } from 'sonner';
import { DeductionCountdownToast } from '@/components/ui/deduction-countdown-toast';

const DEFAULT_THRESHOLD = 1000;  // 1000 积分 ≈ ¥7.7
const DEFAULT_COUNTDOWN_MS = 5000;

export function useConfirmLargeDeduction(threshold = DEFAULT_THRESHOLD) {
  return useCallback(
    (totalPoints: number, onConfirm: () => void | Promise<void>, label?: string) => {
      // 小额 → 直接执行（按钮已标价）
      if (totalPoints < threshold) {
        onConfirm();
        return;
      }

      // 大额 → 5s 倒计时 toast，期间可取消
      let cancelled = false;
      const actionLabel = label || `将消耗 ${totalPoints.toLocaleString()} 算力`;

      const toastId = toast.custom(
        (id) => (
          <DeductionCountdownToast
            label={actionLabel}
            durationMs={DEFAULT_COUNTDOWN_MS}
            onCancel={() => {
              // 取消语义不变：置 cancelled（下方 setTimeout 到点后不执行不扣费）+ 立即收起卡片
              cancelled = true;
              toast.dismiss(id);
            }}
          />
        ),
        { duration: DEFAULT_COUNTDOWN_MS },
      );

      setTimeout(() => {
        if (cancelled) {
          toast.dismiss(toastId);
          return;
        }
        onConfirm();
      }, DEFAULT_COUNTDOWN_MS);
    },
    [threshold],
  );
}
