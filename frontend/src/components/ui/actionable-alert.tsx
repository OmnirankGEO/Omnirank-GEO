import type { ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { alertActionData, type AlertActionContract } from '@/contracts/alertAction';

interface ActionableAlertProps {
  contract: AlertActionContract;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  className?: string;
  allowed?: boolean;
  actionLabel?: string;
  onAction?: () => void;
  actionDisabled?: boolean;
}

export function ActionableAlert({
  contract,
  title,
  description,
  icon,
  className,
  allowed = true,
  actionLabel,
  onAction,
  actionDisabled = false,
}: ActionableAlertProps) {
  const governable = 'action' in contract && Boolean(contract.action);
  return (
    <div
      role="alert"
      {...alertActionData(contract)}
      className={cn('flex items-start gap-2 rounded-lg border px-4 py-3', className)}
    >
      {icon}
      <div className="min-w-0 flex-1 text-sm">
        <p className="font-medium">{title}</p>
        {description && <div className="mt-0.5 text-xs">{description}</div>}
      </div>
      {governable && allowed && actionLabel && onAction && (
        <Button size="sm" onClick={onAction} disabled={actionDisabled} className="shrink-0">
          {actionLabel}
        </Button>
      )}
    </div>
  );
}
