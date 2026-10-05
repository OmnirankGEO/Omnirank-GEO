// 应用内 ConfirmDialog · 替换 window.confirm
// Phase Mainpath 2026-05-08:统一 6 处 window.confirm + ARIA(role=alertdialog · Esc · 焦点)
// 用法:
//   const [confirmDialog, confirm] = useConfirmDialog();
//   ...
//   const ok = await confirm({ title: '删掉这条吗?', danger: true });
//   if (!ok) return;
//   ...
//   return (<>...{confirmDialog}</>)
import { useCallback, useState, type ReactNode } from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { cn } from '@/lib/utils';

export interface ConfirmOptions {
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  /** 高危红按钮(删除/不可逆) */
  danger?: boolean;
  /** 附加到 AlertDialogContent 的 className(portal 到 body 后可强制 dark 主题等) */
  contentClassName?: string;
}

interface State {
  open: boolean;
  options: ConfirmOptions;
  resolver?: (v: boolean) => void;
}

const DEFAULT_OPTIONS: ConfirmOptions = { title: '' };

export function useConfirmDialog() {
  const [state, setState] = useState<State>({ open: false, options: DEFAULT_OPTIONS });

  const confirm = useCallback((options: ConfirmOptions): Promise<boolean> => {
    return new Promise<boolean>((resolve) => {
      setState({ open: true, options, resolver: resolve });
    });
  }, []);

  const close = useCallback(
    (result: boolean) => {
      setState((prev) => {
        prev.resolver?.(result);
        return { open: false, options: prev.options };
      });
    },
    [],
  );

  const dialog: ReactNode = (
    <AlertDialog
      open={state.open}
      onOpenChange={(next) => {
        // 点 overlay / Esc 关闭 → 当作 cancel
        if (!next) close(false);
      }}
    >
      <AlertDialogContent className={cn('max-w-md', state.options.contentClassName)}>
        <AlertDialogHeader>
          <AlertDialogTitle>{state.options.title}</AlertDialogTitle>
          {state.options.description && (
            <AlertDialogDescription>{state.options.description}</AlertDialogDescription>
          )}
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel
            className="min-h-11"
            onClick={() => close(false)}
          >
            {state.options.cancelLabel ?? '取消'}
          </AlertDialogCancel>
          <AlertDialogAction
            className={cn(
              'min-h-11',
              state.options.danger && 'bg-red-600 text-white hover:bg-red-700 focus-visible:ring-red-600',
            )}
            onClick={() => close(true)}
          >
            {state.options.confirmLabel ?? '确定'}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );

  return [dialog, confirm] as const;
}
