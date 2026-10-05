/**
 * ChargeNotifyGuide — 首次扣费提醒偏好引导(P5b 全站静默扣费)
 *
 * charge_notify_level == null 时,首次发生扣费弹一次**非阻断**引导气泡(右下角)。
 * 三按钮:每次都提醒 / 安静一点 / 不主动提醒 → 写偏好 → 之后按选择执行。
 *
 * 设计:非阻断(不是 Dialog 遮罩),不挡操作,可关。复用现有 Button + 卡片样式。
 * 王姐文案铁律:禁"静默扣费/冻结/流水/billing" · "积分"一律叫「算力」(开发原则 SSOT B4)。
 */
import { X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import type { ChargeNotifyLevel } from '@/context/WalletContext';

interface ChargeNotifyGuideProps {
  open: boolean;
  onClose: () => void;
  onChoose: (level: ChargeNotifyLevel) => void;
}

const OPTIONS: { level: ChargeNotifyLevel; label: string; hint: string }[] = [
  { level: 'each', label: '每次都提醒', hint: '每花一笔都弹一下' },
  { level: 'quiet', label: '安静一点', hint: '只在导航亮个小点' },
  { level: 'off', label: '不主动提醒', hint: '随时去钱包查' },
];

export function ChargeNotifyGuide({ open, onClose, onChoose }: ChargeNotifyGuideProps) {
  if (!open) return null;

  return (
    <div
      className="fixed z-[60] bottom-4 right-4 left-4 sm:left-auto sm:w-[360px] animate-in fade-in slide-in-from-bottom-4 duration-300"
      role="dialog"
      aria-label="扣费提醒设置"
    >
      <div className="rounded-xl border border-border bg-card shadow-2xl p-4">
        <div className="flex items-start justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-foreground">以后花算力时怎么提醒你？</h3>
            <p className="text-xs text-muted-foreground mt-1 leading-relaxed">
              每一笔都会记到账单里，随时能查。选一个你习惯的方式，之后能在钱包里改。
            </p>
          </div>
          <button
            onClick={onClose}
            className="shrink-0 p-1 rounded-md text-muted-foreground hover:bg-secondary transition-colors"
            aria-label="关闭"
          >
            <X className="size-4" />
          </button>
        </div>

        <div className="mt-3 grid gap-2">
          {OPTIONS.map((opt) => (
            <Button
              key={opt.level}
              variant="outline"
              className={cn(
                'w-full justify-between h-auto py-2.5 px-3 text-left',
                'hover:border-foreground/40',
              )}
              onClick={() => onChoose(opt.level)}
            >
              <span className="text-sm font-medium text-foreground">{opt.label}</span>
              <span className="text-[11px] text-muted-foreground font-normal">{opt.hint}</span>
            </Button>
          ))}
        </div>
      </div>
    </div>
  );
}
