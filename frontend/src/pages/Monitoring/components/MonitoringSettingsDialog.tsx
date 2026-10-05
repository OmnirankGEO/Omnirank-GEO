/**
 * 客户级监测设置弹窗 — 开启/关闭/频率/费用预估
 */
import { useState, useEffect } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { SearchableSelect } from '@/components/ui/searchable-select';
import { Label } from '@/components/ui/label';
import { Input } from '@/components/ui/input';
import { Activity, Wallet, AlertTriangle, Check, RefreshCw } from 'lucide-react';
import { authFetch } from '@/lib/api';  // [CTO-15.9 build 修] AuthContext 没 authFetch · 用 lib/api

interface Props {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    quoteId: number;
    brandName: string;
    onSaved?: () => void;
}

export function MonitoringSettingsDialog({ open, onOpenChange, quoteId, brandName, onSaved }: Props) {
    const [loading, setLoading] = useState(false);
    const [saving, setSaving] = useState(false);
    const [config, setConfig] = useState<any>(null);
    const [enabled, setEnabled] = useState(false);
    const [intervalHours, setIntervalHours] = useState(24);
    const [intervalMode, setIntervalMode] = useState('24');
    const [startHour, setStartHour] = useState(8);

    const presetIntervals = [6, 12, 24, 48, 72, 168];

    useEffect(() => {
        if (open && quoteId) {
            setLoading(true);
            authFetch(`/api/monitoring/client/${quoteId}/monitoring-config`)
                .then((r: Response) => r.json())
                .then((data: { status?: string; monitoring_enabled?: boolean; monitoring_frequency?: number; monitoring_interval_hours?: number; monitoring_start_hour?: number; [k: string]: unknown }) => {
                    if (data.status === 'success') {
                        setConfig(data);
                        setEnabled(!!data.monitoring_enabled);
                        const interval =
                            data.monitoring_interval_hours ||
                            ({ 1: 24, 2: 12, 3: 8 } as Record<number, number>)[data.monitoring_frequency || 1] ||
                            24;
                        setIntervalHours(interval);
                        setIntervalMode(presetIntervals.includes(interval) ? interval.toString() : 'custom');
                        setStartHour(data.monitoring_start_hour || 8);
                    }
                })
                .finally(() => setLoading(false));
        }
    }, [open, quoteId]);

    const handleSave = async () => {
        setSaving(true);
        try {
            const res = await authFetch(`/api/monitoring/client/${quoteId}/monitoring-config`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled, interval_hours: intervalHours, start_hour: startHour }),
            });
            const data = await res.json();
            if (data.status === 'success') {
                onOpenChange(false);
                onSaved?.();
            }
        } finally { setSaving(false); }
    };

    const costPerRun = (config?.kw_count || 0) * 38;
    const runsPerDay = intervalHours > 0 ? 24 / intervalHours : 1;
    const dailyCost = costPerRun * runsPerDay;
    const monthlyCost = dailyCost * 30;
    const daysAvailable = dailyCost > 0 ? Math.floor((config?.paid_points || 0) / dailyCost) : 999;
    const displayDailyCost = Math.ceil(dailyCost);
    const displayMonthlyCost = Math.ceil(monthlyCost);

    const getScheduleDesc = () => {
        const first = `${startHour.toString().padStart(2, '0')}:00`;
        if (intervalHours === 24) return `每天 ${first}`;
        if (intervalHours < 24) return `首次 ${first},之后每 ${intervalHours} 小时一次`;
        if (intervalHours === 48) return `首次 ${first},之后每 2 天一次`;
        if (intervalHours === 72) return `首次 ${first},之后每 3 天一次`;
        if (intervalHours === 168) return `首次 ${first},之后每周一次`;
        return `首次 ${first},之后每 ${intervalHours} 小时一次`;
    };

    const handleIntervalModeChange = (value: string) => {
        setIntervalMode(value);
        if (value !== 'custom') {
            setIntervalHours(parseInt(value, 10));
        }
    };

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-[95vw] sm:max-w-md">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        <Activity className="h-5 w-5" />
                        监测设置 — {brandName}
                    </DialogTitle>
                </DialogHeader>

                {loading ? (
                    <div className="flex justify-center py-8">
                        <RefreshCw className="h-5 w-5 animate-spin text-muted-foreground" />
                    </div>
                ) : (
                    <div className="space-y-4">
                        {/* 开关 */}
                        <div className="flex items-center justify-between">
                            <Label className="text-base font-medium">开启定时监测</Label>
                            <div
                                className={`w-12 h-6 rounded-full cursor-pointer transition-colors ${enabled ? 'bg-primary' : 'bg-muted'}`}
                                onClick={() => setEnabled(!enabled)}
                            >
                                <div className={`w-5 h-5 bg-card rounded-full shadow-md transform transition-transform mt-0.5 ${enabled ? 'translate-x-6 ml-0.5' : 'translate-x-0.5'}`} />
                            </div>
                        </div>

                        {/* 暂停原因 */}
                        {config?.monitoring_paused_reason && (
                            <div className="flex items-center gap-2 p-2 bg-amber-500/10 border border-amber-500/20 rounded text-amber-400 text-sm">
                                <AlertTriangle className="h-4 w-4 shrink-0" />
                                <span>上次暂停原因: {config.monitoring_paused_reason}</span>
                            </div>
                        )}

                        {enabled && (
                            <>
                                {/* 频率 */}
                                <div className="space-y-2">
                                    <Label>监测频率</Label>
                                    <Select value={intervalMode} onValueChange={handleIntervalModeChange}>
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="6">每 6 小时</SelectItem>
                                            <SelectItem value="12">每 12 小时</SelectItem>
                                            <SelectItem value="24">每 24 小时（默认）</SelectItem>
                                            <SelectItem value="48">每 48 小时</SelectItem>
                                            <SelectItem value="72">每 3 天</SelectItem>
                                            <SelectItem value="168">每周</SelectItem>
                                            <SelectItem value="custom">自定义</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    {intervalMode === 'custom' && (
                                        <div className="flex items-center gap-2">
                                            <Input
                                                type="number"
                                                min={1}
                                                max={720}
                                                value={intervalHours}
                                                onChange={(e) => {
                                                    const next = parseInt(e.target.value || '24', 10);
                                                    setIntervalHours(Number.isNaN(next) ? 24 : Math.max(1, Math.min(720, next)));
                                                }}
                                                className="w-28"
                                            />
                                            <span className="text-sm text-muted-foreground">小时一次</span>
                                        </div>
                                    )}
                                </div>

                                {/* 开始时间 */}
                                <div className="space-y-2">
                                    <Label>首次执行时间（北京时间）</Label>
                                    {/* 可搜索:24 项 · 阈值例外(语义是时间轮盘不是列表,收益在"输入 9 直达 09:00") */}
                                    <SearchableSelect
                                        value={startHour.toString()}
                                        onChange={v => setStartHour(parseInt(v))}
                                        className="w-28"
                                        searchPlaceholder="输入小时"
                                        emptyText="没有匹配的时间"
                                        options={Array.from({ length: 24 }, (_, i) => ({
                                            value: i.toString(),
                                            label: `${i.toString().padStart(2, '0')}:00`,
                                            keywords: i.toString(),
                                        }))}
                                    />
                                    <p className="text-xs text-muted-foreground">执行时间: {getScheduleDesc()}</p>
                                </div>

                                {/* 费用预估 */}
                                <div className="p-3 bg-muted/30 rounded-lg space-y-1.5 text-sm">
                                    <div className="flex justify-between">
                                        <span className="text-muted-foreground">关键词数</span>
                                        <span className="font-medium">{config?.kw_count || 0} 个</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-muted-foreground">单次费用</span>
                                        <span className="font-medium">{costPerRun.toLocaleString()} 算力</span>
                                    </div>
                                    <div className="flex justify-between">
                                        <span className="text-muted-foreground">每日费用</span>
                                        <span className="font-medium">{displayDailyCost.toLocaleString()} 算力</span>
                                    </div>
                                    <div className="flex justify-between border-t border-border pt-1.5">
                                        <span className="text-muted-foreground">月度预估</span>
                                        <span className="font-semibold text-primary">{displayMonthlyCost.toLocaleString()} 算力</span>
                                    </div>
                                    <div className="flex justify-between items-center">
                                        <span className="text-muted-foreground flex items-center gap-1">
                                            <Wallet className="h-3.5 w-3.5" />
                                            当前余额
                                        </span>
                                        <span className={`font-medium ${daysAvailable < 7 ? 'text-amber-400' : 'text-green-400'}`}>
                                            {(config?.paid_points || 0).toLocaleString()} 算力（约可用 {daysAvailable} 天）
                                        </span>
                                    </div>
                                </div>

                                {daysAvailable < 7 && (
                                    <div className="flex items-center gap-2 p-2 bg-amber-500/10 border border-amber-500/20 rounded text-amber-400 text-xs">
                                        <AlertTriangle className="h-4 w-4 shrink-0" />
                                        余额不足 7 天用量，余额耗尽后监测将自动暂停
                                    </div>
                                )}
                            </>
                        )}

                        <div className="flex justify-end gap-2 pt-2">
                            <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
                            <Button onClick={handleSave} disabled={saving}>
                                {saving ? <RefreshCw className="h-4 w-4 animate-spin mr-1" /> : <Check className="h-4 w-4 mr-1" />}
                                {enabled ? '确认开启' : '关闭监测'}
                            </Button>
                        </div>
                    </div>
                )}
            </DialogContent>
        </Dialog>
    );
}
