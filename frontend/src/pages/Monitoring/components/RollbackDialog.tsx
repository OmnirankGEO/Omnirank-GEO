/**
 * Data rollback dialog for reverting monitoring tasks.
 */
import { taskStatusLabel } from '@/lib/v35Terminology';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { RotateCcw, Trash2, RefreshCw, Check, ArrowUpFromLine } from 'lucide-react';

interface RollbackDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    rollbackTasks: any[];
    selectedRollbackIds: Set<number>;
    onSelectedRollbackIdsChange: (ids: Set<number>) => void;
    rollbackLoading: boolean;
    syncingTaskId: number | null;
    onRollback: () => void;
    onSyncTrends: (taskId: number) => void;
}

export function RollbackDialog({
    open, onOpenChange, rollbackTasks, selectedRollbackIds,
    onSelectedRollbackIdsChange, rollbackLoading, syncingTaskId,
    onRollback, onSyncTrends,
}: RollbackDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-[95vw] sm:max-w-2xl max-h-[80vh] overflow-y-auto p-0">
                <DialogHeader className="sticky top-0 z-20 bg-background/95 backdrop-blur border-b px-6 py-4">
                    <DialogTitle className="flex items-center gap-2 text-red-600">
                        <RotateCcw className="h-5 w-5" />
                        数据回退
                    </DialogTitle>
                </DialogHeader>
                <div className="px-6 pb-6 space-y-2">
                <p className="text-sm text-muted-foreground">
                    选择要回退的监测任务，回退后该任务的所有检测结果和趋势数据将被删除。此操作不可恢复。
                </p>
                <div className="space-y-2 mt-2">
                    {rollbackTasks.length === 0 ? (
                        <div className="text-center py-8 text-muted-foreground">暂无监测任务</div>
                    ) : (
                        <div className="overflow-x-auto"><Table className="min-w-[720px]">
                            <TableHeader>
                                <TableRow>
                                    <TableHead className="w-10"></TableHead>
                                    <TableHead className="whitespace-nowrap">时间</TableHead>
                                    <TableHead className="text-center whitespace-nowrap">总测试</TableHead>
                                    <TableHead className="text-center whitespace-nowrap">检出</TableHead>
                                    <TableHead className="text-center whitespace-nowrap">检出率</TableHead>
                                    <TableHead className="whitespace-nowrap">状态</TableHead>
                                    <TableHead className="text-center whitespace-nowrap">趋势同步</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {rollbackTasks.map((task: any) => {
                                    const rate = task.result_count > 0
                                        ? Math.round(task.detected_count / task.result_count * 100)
                                        : 0;
                                    const isRolledBack = task.status === 'rolled_back';
                                    const isSelected = selectedRollbackIds.has(task.id);
                                    const isSynced = task.trend_synced === 1;
                                    const isScheduled = task.trigger_type === 'scheduled';
                                    return (
                                        <TableRow
                                            key={task.id}
                                            className={`${isRolledBack ? 'opacity-40' : isSelected ? 'bg-red-500/5' : ''}`}
                                        >
                                            <TableCell>
                                                {!isRolledBack && (
                                                    <Checkbox
                                                        checked={isSelected}
                                                        onCheckedChange={(checked) => {
                                                            const next = new Set(selectedRollbackIds);
                                                            if (checked) next.add(task.id);
                                                            else next.delete(task.id);
                                                            onSelectedRollbackIdsChange(next);
                                                        }}
                                                    />
                                                )}
                                            </TableCell>
                                            <TableCell className="text-sm">
                                                <div>{task.created_at?.replace('T', ' ').slice(0, 19) || '-'}</div>
                                                <div className="text-xs text-muted-foreground">
                                                    {isScheduled ? '定时' : '手动'}
                                                </div>
                                            </TableCell>
                                            <TableCell className="text-center">{task.result_count || 0}</TableCell>
                                            <TableCell className="text-center">{task.detected_count || 0}</TableCell>
                                            <TableCell className="text-center">
                                                <span className={rate > 50 ? 'text-green-400' : rate > 0 ? 'text-amber-400' : 'text-muted-foreground'}>
                                                    {rate}%
                                                </span>
                                            </TableCell>
                                            <TableCell>
                                                {isRolledBack ? (
                                                    <Badge variant="outline" className="text-muted-foreground">已回退</Badge>
                                                ) : task.status === 'completed' ? (
                                                    <Badge variant="outline" className="bg-green-500/10 text-green-400 border border-green-500/20">已完成</Badge>
                                                ) : (
                                                    <Badge variant="secondary">{taskStatusLabel(task.status)}</Badge>
                                                )}
                                            </TableCell>
                                            <TableCell className="text-center">
                                                {isRolledBack ? (
                                                    <span className="text-xs text-muted-foreground">-</span>
                                                ) : isSynced ? (
                                                    <Badge variant="outline" className="text-green-600 border-green-500/30">
                                                        <Check className="h-3 w-3 mr-1" />已同步
                                                    </Badge>
                                                ) : task.status === 'completed' ? (
                                                    <Button
                                                        variant="outline"
                                                        size="sm"
                                                        className="h-7 text-xs text-brand border-brand/30 hover:bg-brand/5"
                                                        disabled={syncingTaskId === task.id}
                                                        onClick={() => onSyncTrends(task.id)}
                                                    >
                                                        {syncingTaskId === task.id ? (
                                                            <RefreshCw className="h-3 w-3 animate-spin mr-1" />
                                                        ) : (
                                                            <ArrowUpFromLine className="h-3 w-3 mr-1" />
                                                        )}
                                                        同步数据
                                                    </Button>
                                                ) : (
                                                    <span className="text-xs text-muted-foreground">-</span>
                                                )}
                                            </TableCell>
                                        </TableRow>
                                    );
                                })}
                            </TableBody>
                        </Table></div>
                    )}
                </div>
                {selectedRollbackIds.size > 0 && (
                    <div className="flex items-center justify-between mt-4 p-3 bg-red-500/10 border border-red-500/20 rounded-lg">
                        <span className="text-sm text-red-400">
                            已选择 <strong>{selectedRollbackIds.size}</strong> 个任务，回退后数据不可恢复
                        </span>
                        <Button
                            variant="destructive"
                            size="sm"
                            onClick={onRollback}
                            disabled={rollbackLoading}
                        >
                            {rollbackLoading ? (
                                <RefreshCw className="h-4 w-4 animate-spin mr-1" />
                            ) : (
                                <Trash2 className="h-4 w-4 mr-1" />
                            )}
                            确认回退
                        </Button>
                    </div>
                )}
                </div>
            </DialogContent>
        </Dialog>
    );
}
