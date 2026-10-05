/**
 * Data archives dialog for restoring previously cleared data.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { ArchiveRestore, RefreshCw } from 'lucide-react';

interface ArchivesDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    archives: any[];
    restoreLoading: string | null;
    onRestore: (archiveKey: string) => void;
}

export function ArchivesDialog({ open, onOpenChange, archives, restoreLoading, onRestore }: ArchivesDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-[95vw] sm:max-w-2xl max-h-[80vh] overflow-y-auto p-0">
                <DialogHeader className="sticky top-0 z-20 bg-background/95 backdrop-blur border-b px-6 py-4">
                    <DialogTitle className="flex items-center gap-2">
                        <ArchiveRestore className="h-5 w-5" />
                        数据归档记录
                    </DialogTitle>
                </DialogHeader>
                <div className="overflow-x-auto px-6 pb-6"><Table className="min-w-[680px]">
                    <TableHeader>
                        <TableRow>
                            <TableHead className="whitespace-nowrap">归档时间</TableHead>
                            <TableHead className="whitespace-nowrap">原因</TableHead>
                            <TableHead className="text-center whitespace-nowrap">结果数</TableHead>
                            <TableHead className="text-center whitespace-nowrap">趋势数</TableHead>
                            <TableHead className="text-right whitespace-nowrap">操作</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {archives.length === 0 ? (
                            <TableRow>
                                <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                                    暂无归档记录
                                </TableCell>
                            </TableRow>
                        ) : (
                            archives.map(a => (
                                <TableRow key={a.archive_key}>
                                    <TableCell className="text-sm whitespace-nowrap">
                                        {new Date(a.created_at).toLocaleString('zh-CN')}
                                    </TableCell>
                                    <TableCell className="text-sm text-muted-foreground min-w-48">
                                        {a.reason || '-'}
                                    </TableCell>
                                    <TableCell className="text-center whitespace-nowrap">{a.result_count}</TableCell>
                                    <TableCell className="text-center whitespace-nowrap">{a.trend_count}</TableCell>
                                    <TableCell className="text-right whitespace-nowrap">
                                        <Button
                                            size="sm"
                                            variant="outline"
                                            onClick={() => onRestore(a.archive_key)}
                                            disabled={restoreLoading === a.archive_key}
                                        >
                                            {restoreLoading === a.archive_key ? (
                                                <RefreshCw className="h-3 w-3 animate-spin mr-1" />
                                            ) : (
                                                <ArchiveRestore className="h-3 w-3 mr-1" />
                                            )}
                                            恢复
                                        </Button>
                                    </TableCell>
                                </TableRow>
                            ))
                        )}
                    </TableBody>
                </Table></div>
            </DialogContent>
        </Dialog>
    );
}
