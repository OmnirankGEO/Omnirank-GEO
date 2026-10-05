/**
 * Platform weights management dialog for AI search engine MAU weighting.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { BarChart3, RefreshCw, Save } from 'lucide-react';

interface PlatformWeightsDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    weightsLoading: boolean;
    weightsSearching: boolean;
    platformWeights: Record<string, number>;
    onPlatformWeightsChange: (weights: Record<string, number>) => void;
    platformMau: Record<string, string>;
    weightsSource: string;
    weightsUpdatedAt: string;
    onSearchMau: () => void;
    onSave: () => void;
}

// [P0-2 · 2026-07-26] 统一五引擎：元宝补进平台名映射。
// 平台行本身由后端返回的 weights 键驱动（Object.entries），这里只负责显示名；
// 漏一个键会让管理员看到裸 key（如 "yuanbao"），不会锁死保存。
const nameMap: Record<string, string> = {
    doubao: '豆包',
    dashscope: '通义千问',
    deepseek: 'DeepSeek',
    kimi: 'Kimi',
    yuanbao: '元宝'
};

export function PlatformWeightsDialog({
    open, onOpenChange, weightsLoading, weightsSearching,
    platformWeights, onPlatformWeightsChange, platformMau,
    weightsSource, weightsUpdatedAt, onSearchMau, onSave,
}: PlatformWeightsDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-[95vw] sm:max-w-lg">
                <DialogHeader>
                    <DialogTitle className="flex items-center gap-2">
                        <BarChart3 className="h-5 w-5 text-orange-500" />
                        AI平台占比权重
                    </DialogTitle>
                </DialogHeader>
                <div className="space-y-4">
                    <p className="text-sm text-muted-foreground">
                        出现率按各AI平台月活(MAU)加权计算。点击「搜索最新数据」自动获取最新MAU并生成建议权重，您可手动修改后确认保存。
                    </p>

                    {weightsSource && (
                        <div className="text-xs text-muted-foreground flex items-center gap-2">
                            <span>数据来源: {weightsSource === 'default' ? '默认值' : weightsSource === 'manual' ? '手动设置' : 'AI搜索'}</span>
                            {weightsUpdatedAt && <span>| 更新于 {new Date(weightsUpdatedAt).toLocaleDateString('zh-CN')}</span>}
                        </div>
                    )}

                    {weightsLoading ? (
                        <div className="flex justify-center py-8">
                            <RefreshCw className="h-6 w-6 animate-spin text-muted-foreground" />
                        </div>
                    ) : (
                        <div className="space-y-3">
                            <div className="grid grid-cols-[1fr_80px_60px] sm:grid-cols-[1fr_120px_80px] gap-2 text-xs font-medium text-muted-foreground px-1">
                                <span>平台</span>
                                <span>月活(MAU)</span>
                                <span>权重</span>
                            </div>
                            {Object.entries(platformWeights).map(([platform, weight]) => {
                                const mauText = platformMau[platform] || '';
                                const mauValue = mauText.includes(':') ? mauText.split(':').pop()?.trim() || '' : mauText;
                                return (
                                    <div key={platform} className="grid grid-cols-[1fr_80px_60px] sm:grid-cols-[1fr_120px_80px] gap-2 items-center">
                                        <span className="text-sm font-medium">{nameMap[platform] || platform}</span>
                                        <span className="text-sm text-muted-foreground">{mauValue || '-'}</span>
                                        <Input
                                            type="number"
                                            step="0.05"
                                            min="0"
                                            max="1"
                                            className="h-8 text-sm text-center"
                                            value={weight}
                                            onChange={(e) => {
                                                const val = parseFloat(e.target.value) || 0;
                                                onPlatformWeightsChange({ ...platformWeights, [platform]: val });
                                            }}
                                        />
                                    </div>
                                );
                            })}
                            <div className="flex justify-between items-center pt-2 border-t text-sm">
                                <span className="text-muted-foreground">权重总和</span>
                                {(() => {
                                    const total = Object.values(platformWeights).reduce((a, b) => a + b, 0);
                                    const ok = Math.abs(total - 1.0) <= 0.05;
                                    return (
                                        <span className={ok ? 'text-green-600 font-semibold' : 'text-red-500 font-semibold'}>
                                            {total.toFixed(2)} {ok ? '' : '(应为 1.00)'}
                                        </span>
                                    );
                                })()}
                            </div>
                        </div>
                    )}

                    <div className="flex gap-2 pt-2">
                        <Button
                            variant="outline"
                            className="flex-1"
                            disabled={weightsSearching}
                            onClick={onSearchMau}
                        >
                            {weightsSearching ? (
                                <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                            ) : (
                                <RefreshCw className="h-4 w-4 mr-2" />
                            )}
                            {weightsSearching ? '搜索中...' : '搜索最新数据'}
                        </Button>
                        <Button
                            className="flex-1"
                            disabled={weightsLoading}
                            onClick={onSave}
                        >
                            <Save className="h-4 w-4 mr-2" />
                            确认保存
                        </Button>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}
