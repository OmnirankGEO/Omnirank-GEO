/**
 * Trend chart dialog showing detection rate over time.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { BarChart3 } from 'lucide-react';
import type { TrendData } from '../types';

interface TrendChartDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    trendData: TrendData[];
}

export function TrendChartDialog({ open, onOpenChange, trendData }: TrendChartDialogProps) {
    const activeDays = trendData.filter(d => d.rate !== null) as { date: string; rate: number }[];
    const avgRate = activeDays.length > 0 ? Math.round(activeDays.reduce((s, d) => s + d.rate, 0) / activeDays.length) : 0;
    const maxRate = activeDays.length > 0 ? Math.max(...activeDays.map(d => d.rate)) : 0;
    const minRate = activeDays.length > 0 ? Math.min(...activeDays.map(d => d.rate)) : 0;
    const barHeight = (rate: number) => {
        const normalized = Math.max(0, Math.min(rate, 100));
        return Math.max(4, Math.round((normalized / 100) * 172));
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto p-0">
                <DialogHeader className="sticky top-0 z-20 bg-background/95 backdrop-blur border-b px-6 py-4">
                    <DialogTitle className="flex items-center gap-2">
                        <BarChart3 className="h-5 w-5" />
                        出现率趋势分析
                    </DialogTitle>
                </DialogHeader>
                <div className="space-y-4 px-6 pb-6">
                    <div className="h-64 flex items-end justify-between gap-2 p-4 bg-muted rounded-lg">
                        {trendData.map((item, i) => (
                            <div key={i} className="flex-1 flex flex-col items-center gap-2">
                                {item.rate !== null ? (
                                    <>
                                        <div
                                            className="w-full bg-brand rounded-t transition-all"
                                            style={{ height: `${barHeight(item.rate)}px` }}
                                        />
                                        <span className="text-xs font-medium">{Math.round(item.rate)}%</span>
                                    </>
                                ) : (
                                    <>
                                        <div className="w-full bg-border rounded-t" style={{ height: '2px' }} />
                                        <span className="text-xs text-muted-foreground">-</span>
                                    </>
                                )}
                                <span className="text-xs text-muted-foreground">{item.date}</span>
                            </div>
                        ))}
                    </div>
                    <div className="grid grid-cols-3 gap-2 md:gap-4 text-center">
                        <div className="p-2 md:p-3 bg-muted rounded">
                            <p className="text-lg md:text-2xl font-semibold text-foreground">{avgRate}%</p>
                            <p className="text-xs text-muted-foreground">
                                {activeDays.length}天平均
                                {activeDays.length < trendData.length && (
                                    <span className="text-muted-foreground">（排除{trendData.length - activeDays.length}天无数据）</span>
                                )}
                            </p>
                        </div>
                        <div className="p-2 md:p-3 bg-muted rounded">
                            <p className="text-lg md:text-2xl font-semibold text-green-400">{maxRate}%</p>
                            <p className="text-xs text-muted-foreground">最高值</p>
                        </div>
                        <div className="p-2 md:p-3 bg-muted rounded">
                            <p className="text-lg md:text-2xl font-semibold text-orange-400">{minRate}%</p>
                            <p className="text-xs text-muted-foreground">最低值</p>
                        </div>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
