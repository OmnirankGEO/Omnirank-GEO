/**
 * Report generation dialog for weekly/monthly/quarterly/yearly reports.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { FileBarChart, RefreshCw } from 'lucide-react';

interface ReportGenDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    reportGenType: string;
    onReportGenTypeChange: (val: string) => void;
    reportGenLoading: boolean;
    reportGenResult: string;
    currentBrandId: number | undefined;
    onGenerate: () => void;
}

export function ReportGenDialog({
    open, onOpenChange, reportGenType, onReportGenTypeChange,
    reportGenLoading, reportGenResult, currentBrandId, onGenerate,
}: ReportGenDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-[95vw] sm:max-w-md">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        <FileBarChart className="h-5 w-5" />
                        生成监测报告
                    </DialogTitle>
                </DialogHeader>
                <div className="space-y-4">
                    <div>
                        <Label className="text-sm font-medium">报告类型</Label>
                        <div className="grid grid-cols-2 gap-2 mt-2">
                            {[
                                { value: 'weekly', label: '周报' },
                                { value: 'monthly', label: '月报' },
                                { value: 'quarterly', label: '季报' },
                                { value: 'yearly', label: '年报' },
                            ].map(opt => (
                                <Button
                                    key={opt.value}
                                    variant={reportGenType === opt.value ? 'default' : 'outline'}
                                    className="w-full"
                                    onClick={() => onReportGenTypeChange(opt.value)}
                                >
                                    {opt.label}
                                </Button>
                            ))}
                        </div>
                    </div>
                    <p className="text-xs text-muted-foreground">
                        系统将根据当前品牌的监测数据自动汇总生成
                        {reportGenType === 'weekly' ? '周报' : reportGenType === 'monthly' ? '月报' : reportGenType === 'quarterly' ? '季报' : '年报'}
                        ，生成后可在「报告管理」页面查看和编辑。
                    </p>
                    {reportGenResult && (
                        <div className={`text-sm p-3 rounded-lg ${
                            reportGenResult.includes('成功') ? 'bg-green-500/10 text-green-400 border border-green-500/20'
                            : reportGenResult.includes('无监测') ? 'bg-amber-500/10 text-amber-400 border border-amber-500/20'
                            : 'bg-red-500/10 text-red-400 border border-red-500/20'
                        }`}>
                            {reportGenResult}
                        </div>
                    )}
                    <Button
                        className="w-full"
                        onClick={onGenerate}
                        disabled={reportGenLoading || !currentBrandId}
                    >
                        {reportGenLoading ? (
                            <>
                                <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                                生成中...
                            </>
                        ) : (
                            '立即生成'
                        )}
                    </Button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
