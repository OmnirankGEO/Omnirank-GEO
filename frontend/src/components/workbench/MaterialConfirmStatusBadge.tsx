/**
 * MaterialConfirmStatusBadge — 写作资料确认状态徽章 (CTO-F 2026-04-27)
 *
 * 用途:
 *   - 写作工具屏顶部 / 内容队列卡片 / 客户工作台 头部
 *   - 一眼区分"客户已确认 / 待客户确认 / 客户有修改意见 / 还没整理"
 *
 * 不假按钮 — 纯展示
 */

import { CheckCircle2, AlertCircle, Clock, Inbox, XCircle, MessageSquareWarning } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { type MaterialConfirmStatus, STATUS_LABEL } from '@/services/m3';

interface Props {
  status: MaterialConfirmStatus;
  size?: 'sm' | 'md';
  className?: string;
}

const STATUS_VISUAL: Record<MaterialConfirmStatus, {
  icon: typeof CheckCircle2;
  variant: 'default' | 'secondary' | 'outline' | 'destructive';
  classes: string;
}> = {
  none: {
    icon: Inbox,
    variant: 'outline',
    classes: 'text-muted-foreground border-muted-foreground/30',
  },
  draft: {
    icon: Inbox,
    variant: 'outline',
    classes: 'text-foreground border-foreground/30',
  },
  pending: {
    icon: Clock,
    variant: 'outline',
    classes: 'text-blue-600 dark:text-blue-300 border-blue-300/50 bg-blue-50/50 dark:bg-blue-900/20',
  },
  feedback: {
    icon: MessageSquareWarning,
    variant: 'outline',
    classes: 'text-amber-700 dark:text-amber-300 border-amber-300 bg-amber-50/60 dark:bg-amber-900/20',
  },
  confirmed: {
    icon: CheckCircle2,
    variant: 'outline',
    classes: 'text-emerald-700 dark:text-emerald-300 border-emerald-300 bg-emerald-50/60 dark:bg-emerald-900/20',
  },
  expired: {
    icon: AlertCircle,
    variant: 'outline',
    classes: 'text-amber-700 dark:text-amber-300 border-amber-300/50',
  },
  revoked: {
    icon: XCircle,
    variant: 'outline',
    classes: 'text-rose-700 dark:text-rose-300 border-rose-300/50',
  },
};

export function MaterialConfirmStatusBadge({ status, size = 'sm', className }: Props) {
  const visual = STATUS_VISUAL[status];
  const Icon = visual.icon;
  return (
    <Badge
      variant={visual.variant}
      className={cn(
        'gap-1 font-medium',
        size === 'sm' ? 'text-[11px] px-2 py-0.5' : 'text-xs px-2.5 py-1',
        visual.classes,
        className,
      )}
      aria-label={`写作资料状态: ${STATUS_LABEL[status]}`}
    >
      <Icon className={cn(size === 'sm' ? 'h-3 w-3' : 'h-3.5 w-3.5')} aria-hidden />
      {STATUS_LABEL[status]}
    </Badge>
  );
}

export default MaterialConfirmStatusBadge;
