/**
 * Operation logs dialog showing action history.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { ClipboardList } from 'lucide-react';
import type { OperationLog } from '../types';

interface OperationLogsDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    operationLogs: OperationLog[];
}

const actionMap: Record<string, string> = {
    start_monitoring: '启动监测',
    add_keyword: '添加词条',
    batch_add_keywords: '批量添加词条',
    delete_keyword: '删除词条',
    generate_report: '生成报告',
    clear_data: '清除数据',
    rollback: '数据回退',
    sync_trend: '同步趋势',
};

const targetMap: Record<string, string> = {
    task: '监测任务',
    keyword: '词条',
    report: '报告',
};

function parseLogDetails(details: string): string {
    try {
        const d = typeof details === 'string' ? JSON.parse(details) : details;
        if (d) {
            const parts: string[] = [];
            if (d.keywords_count != null) parts.push(`${d.keywords_count} 个词条`);
            if (d.count != null) parts.push(`${d.count} 条`);
            if (d.keyword) parts.push(`"${d.keyword}"`);
            if (d.brand) parts.push(d.brand);
            if (d.platforms) {
                const pList = Array.isArray(d.platforms) ? d.platforms : [d.platforms];
                parts.push(pList.join(', '));
            }
            if (d.source) parts.push(`来源: ${d.source}`);
            return parts.length > 0 ? parts.join(' · ') : '-';
        }
    } catch {
        return String(details || '-');
    }
    return '-';
}

export function OperationLogsDialog({ open, onOpenChange, operationLogs }: OperationLogsDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto p-0">
                <DialogHeader className="sticky top-0 z-20 bg-background/95 backdrop-blur border-b px-6 py-4">
                    <DialogTitle className="flex items-center gap-2">
                        <ClipboardList className="h-5 w-5" />
                        操作日志
                    </DialogTitle>
                </DialogHeader>

                {operationLogs.length === 0 ? (
                    <div className="text-center py-8 text-muted-foreground px-6 pb-6">暂无操作日志</div>
                ) : (
                    <div className="space-y-2 px-6 pb-6">
                        {operationLogs.map(log => {
                            const timeStr = log.created_at
                                ? log.created_at.replace('T', ' ').replace(/\.\d+$/, '').slice(0, 19)
                                : '-';
                            return (
                                <div key={log.id} className="flex flex-col sm:flex-row sm:items-center gap-1.5 sm:gap-3 px-3 py-2.5 rounded-lg bg-secondary/50 border border-border/50">
                                    <span className="text-xs text-muted-foreground whitespace-nowrap shrink-0">{timeStr}</span>
                                    <div className="flex items-center gap-2 flex-wrap">
                                        <Badge variant="outline" className="shrink-0">{actionMap[log.action] || log.action}</Badge>
                                        <span className="text-sm text-foreground whitespace-nowrap">
                                            {targetMap[log.target_type] || log.target_type} #{log.target_id}
                                        </span>
                                    </div>
                                    <span className="text-xs text-muted-foreground sm:ml-auto line-clamp-2">
                                        {parseLogDetails(log.details)}
                                    </span>
                                </div>
                            );
                        })}
                    </div>
                )}
            </DialogContent>
        </Dialog>
    );
}
