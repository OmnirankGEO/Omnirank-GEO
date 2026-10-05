/**
 * 通用确认 Dialog · 高危操作用(如配置重置)
 *
 * 审核界面默认是直接按钮 + 30 分钟撤销 · 不走这个 Dialog
 */
import {
    Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';

export interface ConfirmDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    title: string;
    description?: string;
    confirmText?: string;
    cancelText?: string;
    danger?: boolean;
    loading?: boolean;
    onConfirm: () => void | Promise<void>;
}

export function ConfirmDialog(props: ConfirmDialogProps) {
    const {
        open, onOpenChange, title, description,
        confirmText = '确认', cancelText = '取消',
        danger = false, loading = false, onConfirm,
    } = props;

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent>
                <DialogHeader>
                    <DialogTitle>{title}</DialogTitle>
                    {description && <DialogDescription>{description}</DialogDescription>}
                </DialogHeader>
                <DialogFooter>
                    <Button variant="outline" onClick={() => onOpenChange(false)} disabled={loading}>
                        {cancelText}
                    </Button>
                    <Button
                        variant={danger ? 'destructive' : 'default'}
                        onClick={() => { void onConfirm(); }}
                        disabled={loading}
                    >
                        {loading ? '处理中...' : confirmText}
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

export default ConfirmDialog;
