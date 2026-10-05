/**
 * Clear monitoring data dialog with password confirmation.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { ShieldAlert, Trash2, RefreshCw } from 'lucide-react';

interface ClearDataDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    clearPassword: string;
    onClearPasswordChange: (val: string) => void;
    clearReason: string;
    onClearReasonChange: (val: string) => void;
    clearLoading: boolean;
    onClear: () => void;
}

export function ClearDataDialog({
    open, onOpenChange, clearPassword, onClearPasswordChange,
    clearReason, onClearReasonChange, clearLoading, onClear,
}: ClearDataDialogProps) {
    return (
        <Dialog open={open} onOpenChange={(o) => {
            onOpenChange(o);
            if (!o) {
                onClearPasswordChange('');
                onClearReasonChange('');
            }
        }}>
            <DialogContent className="max-w-[95vw] sm:max-w-md">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2 text-red-600">
                        <ShieldAlert className="h-5 w-5" />
                        清除监测数据
                    </DialogTitle>
                </DialogHeader>
                <div className="space-y-4">
                    <div className="p-3 bg-red-500/10 border border-red-500/20 rounded-lg text-sm text-red-400">
                        此操作将清除该客户的<strong>所有监测任务和结果</strong>。
                        数据会自动归档，如果误删可通过"数据归档"恢复。
                    </div>
                    <div className="space-y-2">
                        <Label>操作密码</Label>
                        <Input
                            type="password"
                            placeholder="请输入操作密码"
                            value={clearPassword}
                            onChange={e => onClearPasswordChange(e.target.value)}
                        />
                    </div>
                    <div className="space-y-2">
                        <Label>清除原因（可选）</Label>
                        <Input
                            placeholder="例：客户正式上线，清除测试期数据"
                            value={clearReason}
                            onChange={e => onClearReasonChange(e.target.value)}
                        />
                    </div>
                    <div className="flex justify-end gap-2">
                        <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
                        <Button
                            variant="destructive"
                            onClick={onClear}
                            disabled={clearLoading || !clearPassword}
                        >
                            {clearLoading ? <RefreshCw className="h-4 w-4 animate-spin mr-1" /> : <Trash2 className="h-4 w-4 mr-1" />}
                            确认清除
                        </Button>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
