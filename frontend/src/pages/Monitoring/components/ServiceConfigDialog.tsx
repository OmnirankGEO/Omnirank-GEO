/**
 * 累计达标天数(履约配额)设置对话框。
 *
 * [服务期 SSOT 2026-08-06 §1.1/§1.2] 本对话框**不再改合同服务期起止日**。
 *   旧版这里有个"服务开始日期"输入框,它能把合同起始日往前挪,却从不重算
 *   service_end_date —— 起点变了、终点没变,两钟当场错开(晨光富士 #286 就是这个形状)。
 *   合同服务期(起止日)只在报价单里改;这里只管"累计达标多少天算交付完成"。
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { CalendarClock, Save, RefreshCw } from 'lucide-react';

interface ServiceConfigDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    serviceConfigDays: number;
    onServiceConfigDaysChange: (val: number) => void;
    serviceConfigCustom: string;
    onServiceConfigCustomChange: (val: string) => void;
    serviceConfigLoading: boolean;
    currentServiceDays: number | null;
    currentServiceStart: string | null;
    /** 合同服务期至 · 只读回显 · 要改去报价单 */
    currentServiceEnd: string | null;
    selectedClient: string;
    onSave: () => void;
    onGoToServicePeriod: () => void;
}

export function ServiceConfigDialog({
    open, onOpenChange, serviceConfigDays, onServiceConfigDaysChange,
    onServiceConfigCustomChange, serviceConfigCustom, serviceConfigLoading, currentServiceDays,
    currentServiceStart, currentServiceEnd, selectedClient, onSave, onGoToServicePeriod,
}: ServiceConfigDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="sm:max-w-md">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        <CalendarClock className="h-5 w-5 text-amber-600" />
                        累计达标天数设置
                    </DialogTitle>
                </DialogHeader>
                <div className="space-y-4 py-2">
                    {currentServiceDays && (
                        <div className="text-sm text-muted-foreground bg-muted rounded-lg p-3">
                            当前: <span className="font-medium text-foreground">累计达标 {currentServiceDays} 天</span>
                        </div>
                    )}

                    {/* 合同服务期只读回显 + 指路 —— 提示铁律:说清"这里不能改",同时给能改的地方 */}
                    <div className="text-xs text-muted-foreground bg-muted/50 rounded-lg p-3 space-y-1">
                        <div>
                            合同服务期:{' '}
                            <span className="font-medium text-foreground">
                                {currentServiceStart || '未设'} → {currentServiceEnd || '未设'}
                            </span>
                        </div>
                        <div>
                            自动监测按<b>合同服务期</b>排班;这里改的是<b>交付承诺</b>(累计达标多少天),两者不是一回事。
                        </div>
                        <Button variant="link" size="sm" className="h-auto p-0 text-xs" onClick={onGoToServicePeriod}>
                            要改服务期起止日 → 去报价单
                        </Button>
                    </div>

                    <div className="space-y-2">
                        <Label>累计达标天数</Label>
                        <div className="flex gap-2">
                            {[30, 90, 180].map(d => (
                                <Button
                                    key={d}
                                    size="sm"
                                    variant={serviceConfigDays === d && !serviceConfigCustom ? 'default' : 'outline'}
                                    onClick={() => { onServiceConfigDaysChange(d); onServiceConfigCustomChange(''); }}
                                >
                                    {d}天
                                </Button>
                            ))}
                            <Input
                                type="number"
                                placeholder="自定义"
                                className="w-24 h-9"
                                value={serviceConfigCustom}
                                onChange={e => onServiceConfigCustomChange(e.target.value)}
                                min={1}
                            />
                        </div>
                        <p className="text-xs text-muted-foreground">
                            将设置为: <span className="font-medium">累计达标 {serviceConfigCustom ? serviceConfigCustom : serviceConfigDays} 天</span>
                            {' '}· 单位是<b>达标天数</b>,不是日历天(未达标的日子不消耗)
                        </p>
                    </div>

                    <Button
                        className="w-full"
                        onClick={onSave}
                        disabled={serviceConfigLoading || !selectedClient}
                    >
                        {serviceConfigLoading ? (
                            <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                        ) : (
                            <Save className="h-4 w-4 mr-2" />
                        )}
                        保存
                    </Button>
                </div>
            </DialogContent>
        </Dialog>
    );
}
