/**
 * Scheduled monitoring configuration dialog.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Clock, Play, Check, RefreshCw, Square } from 'lucide-react';

interface ScheduleDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    scheduleHour: number;
    onScheduleHourChange: (val: number) => void;
    scheduleMinute: number;
    onScheduleMinuteChange: (val: number) => void;
    scheduleEnabled: boolean;
    onScheduleEnabledChange: (val: boolean) => void;
    clientMonitoringEnabled: boolean;
    onClientMonitoringEnabledChange: (val: boolean) => void;
    monitoringIntervalHours: number;
    onMonitoringIntervalHoursChange: (val: number) => void;
    monitoringStartHour: number;
    onMonitoringStartHourChange: (val: number) => void;
    clientMonitoringConfig?: any;
    scheduleStatus: any;
    scheduleLoading: boolean;
    onSave: () => void;
    onRunNow: () => void;
    onStop?: () => void;
}

export function ScheduleDialog({
    open, onOpenChange, scheduleHour, onScheduleHourChange,
    scheduleMinute, onScheduleMinuteChange, scheduleEnabled,
    onScheduleEnabledChange, clientMonitoringEnabled, onClientMonitoringEnabledChange,
    monitoringIntervalHours, onMonitoringIntervalHoursChange,
    monitoringStartHour, onMonitoringStartHourChange, clientMonitoringConfig,
    scheduleStatus, scheduleLoading,
    onSave, onRunNow, onStop,
}: ScheduleDialogProps) {
    void scheduleHour;
    void scheduleMinute;
    void onScheduleHourChange;
    void onScheduleMinuteChange;

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-[95vw] sm:max-w-md">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        <Clock className="h-5 w-5 text-brand" />
                        定时监测设置
                    </DialogTitle>
                </DialogHeader>
                <div className="space-y-4">
                    <div className="p-3 bg-blue-500/10 border border-blue-500/20 rounded-lg text-sm text-blue-400">
                        全局监测总开关。开启后系统每小时巡检一次，只检查哪些客户到点；当前客户默认每天一次，不会每小时消耗算力。<br/>
                        各客户需要单独开启监测并设置监测间隔，每次监测按关键词数消耗算力。
                    </div>

                    <div className="flex items-center justify-between">
                        <Label className="text-base font-medium">启用全局自动巡检</Label>
                        <div
                            className={`w-12 h-6 rounded-full cursor-pointer transition-colors ${scheduleEnabled ? 'bg-brand' : 'bg-muted'}`}
                            onClick={() => onScheduleEnabledChange(!scheduleEnabled)}
                        >
                            <div className={`w-5 h-5 bg-card rounded-full shadow-md transform transition-transform mt-0.5 ${scheduleEnabled ? 'translate-x-6 ml-0.5' : 'translate-x-0.5'}`} />
                        </div>
                    </div>

                    {scheduleEnabled && (
                        <div className="p-3 bg-muted/30 rounded-lg text-sm text-muted-foreground">
                            系统每小时整点巡检；只有当前客户达到设置间隔时才会执行监测。
                        </div>
                    )}

                    <div className="space-y-3 rounded-lg border border-border bg-card/50 p-3">
                        <div className="flex items-center justify-between">
                            <div>
                                <Label className="text-base font-medium">当前客户自动监测</Label>
                                <p className="mt-1 text-xs text-muted-foreground">每天 09:00 自动跑多平台 AI 检测一轮 · 按关键词数扣算力</p>
                            </div>
                            <div
                                className={`w-12 h-6 rounded-full cursor-pointer transition-colors ${clientMonitoringEnabled ? 'bg-brand' : 'bg-muted'}`}
                                onClick={() => onClientMonitoringEnabledChange(!clientMonitoringEnabled)}
                            >
                                <div className={`w-5 h-5 bg-card rounded-full shadow-md transform transition-transform mt-0.5 ${clientMonitoringEnabled ? 'translate-x-6 ml-0.5' : 'translate-x-0.5'}`} />
                            </div>
                        </div>

                        {/* [CTO-15.23 2026-05-09 老板拍板根治] 任务 1 daily charge_on_success 模型 · cron 固定每天 1 次 · 不可调
                            老 频率 select / 起始小时 select / 间隔 input 全砍 · 改为只读提示卡 · UI 跟实际扣费模型一致
                            老板原话:"添加关键词后立即生成标题"链路 + 监测扣费链路 都按 daily 模型 · 不再假装"代理可自定义频率"误导 */}
                        <div className="rounded-md bg-blue-500/10 border border-blue-500/20 p-2.5 text-xs text-blue-300 space-y-1">
                            <div className="font-medium text-blue-200">监测节奏(系统固定 · 不可调)</div>
                            <div>· 每天 09:00 自动跑 1 轮(系统打散避免拥堵)</div>
                            <div>· 多平台 AI 引擎全跑 · 每个关键词扣 {clientMonitoringConfig?.unit_price_per_keyword_per_day ?? 130} 算力/天</div>
                            <div>· 完成才扣模式(失败不扣)· 余额不足自动暂停 · 充值后自动恢复</div>
                        </div>

                        {clientMonitoringConfig && (() => {
                            // [CTO-15.23 2026-05-09] 本地实时算 · daily 模型 = kwCount × unitPrice(频率不影响)
                            const unitPrice = clientMonitoringConfig.unit_price_per_keyword_per_day ?? 130;
                            const kwCount = clientMonitoringConfig.kw_count ?? 0;
                            const dailyCost = kwCount * unitPrice;
                            const paidPoints = clientMonitoringConfig.paid_points ?? 0;
                            const daysAvailable = dailyCost > 0 ? Math.floor(paidPoints / dailyCost) : 999;
                            return (
                                <div className="rounded-md bg-muted/40 p-2 text-xs text-muted-foreground">
                                    {kwCount} 个关键词 × {unitPrice} 算力/天 = <span className="text-foreground font-medium">每日 {dailyCost.toLocaleString()} 算力</span>
                                    <span className="mx-1">·</span>
                                    余额 {paidPoints.toLocaleString()} 算力 · 约可跑 <span className="text-foreground font-medium">{daysAvailable.toLocaleString()}</span> 天
                                </div>
                            );
                        })()}
                    </div>

                    {scheduleStatus && (
                        <div className="space-y-2 text-sm">
                            {scheduleStatus.jobs?.length > 0 && (
                                <div className="flex justify-between text-muted-foreground">
                                    <span>下次执行</span>
                                    <span className="font-medium">{scheduleStatus.jobs[0].next_run || '-'}</span>
                                </div>
                            )}
                            {scheduleStatus.is_executing && (
                                <div className="flex items-center justify-between gap-2 p-2 bg-amber-500/10 border border-amber-500/20 rounded text-amber-400">
                                    <div className="flex items-center gap-2">
                                        <RefreshCw className="h-4 w-4 animate-spin" />
                                        <span>正在执行: {scheduleStatus.current_brand || '准备中'}</span>
                                    </div>
                                    {onStop && (
                                        <Button variant="destructive" size="sm" className="h-6 px-2 text-xs shrink-0" onClick={onStop}>
                                            <Square className="h-3 w-3 mr-1" />
                                            停止
                                        </Button>
                                    )}
                                </div>
                            )}
                            {scheduleStatus.last_result && !scheduleStatus.last_result.error && (
                                <div className="p-2 bg-green-500/10 border border-green-500/20 rounded text-green-400 text-xs">
                                    上次执行: {scheduleStatus.last_run} |
                                    {scheduleStatus.last_result.brands_count} 品牌 |
                                    {scheduleStatus.last_result.total_detected}/{scheduleStatus.last_result.total_tests} 检出 |
                                    {scheduleStatus.last_result.elapsed_seconds}秒
                                </div>
                            )}
                        </div>
                    )}

                    <div className="flex flex-col sm:flex-row justify-between gap-2 pt-2">
                        <Button variant="outline" size="sm" onClick={onRunNow}>
                            <Play className="h-4 w-4 mr-1" />
                            立即执行一次
                        </Button>
                        <div className="flex gap-2 justify-end">
                            <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
                            <Button onClick={onSave} disabled={scheduleLoading}>
                                {scheduleLoading ? <RefreshCw className="h-4 w-4 animate-spin mr-1" /> : <Check className="h-4 w-4 mr-1" />}
                                保存设置
                            </Button>
                        </div>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
