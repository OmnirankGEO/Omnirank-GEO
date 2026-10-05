/**
 * Dialog displaying latest monitoring check results.
 */
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import type { MonitoringResult } from '../types';

interface MonitoringResultsDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    results: MonitoringResult[];
}

export function MonitoringResultsDialog({ open, onOpenChange, results }: MonitoringResultsDialogProps) {
    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-2xl sm:max-w-4xl max-h-[80vh] overflow-y-auto p-0">
                <DialogHeader className="sticky top-0 z-20 bg-background/95 backdrop-blur border-b px-6 py-4">
                    <DialogTitle>最近监测报告</DialogTitle>
                </DialogHeader>
                <div className="overflow-x-auto px-6 pb-6">
                <Table className="min-w-[720px]">
                    <TableHeader>
                        <TableRow>
                            <TableHead className="min-w-64 whitespace-nowrap">词条</TableHead>
                            <TableHead className="whitespace-nowrap">平台</TableHead>
                            <TableHead className="text-center whitespace-nowrap">检出</TableHead>
                            <TableHead className="text-center whitespace-nowrap">排名</TableHead>
                            <TableHead className="whitespace-nowrap">检测时间</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {results.length === 0 ? (
                            <TableRow>
                                <TableCell colSpan={5} className="text-center py-8 text-muted-foreground">
                                    暂无检测结果
                                </TableCell>
                            </TableRow>
                        ) : (
                            results.map((result, idx) => (
                                <TableRow key={idx}>
                                    <TableCell className="font-medium whitespace-nowrap">{result.keyword}</TableCell>
                                    <TableCell className="whitespace-nowrap">
                                        <Badge variant="outline">{result.platform}</Badge>
                                    </TableCell>
                                    <TableCell className="text-center whitespace-nowrap">
                                        {result.is_detected ? (
                                            <Badge variant="outline" className="bg-green-500/10 text-green-400 border border-green-500/20">✓ 检出</Badge>
                                        ) : (
                                            <Badge variant="secondary">未检出</Badge>
                                        )}
                                    </TableCell>
                                    <TableCell className="text-center whitespace-nowrap">
                                        {result.rank_position ? `#${result.rank_position}` : '-'}
                                    </TableCell>
                                    <TableCell className="text-xs text-muted-foreground whitespace-nowrap">
                                        {result.tested_at}
                                    </TableCell>
                                </TableRow>
                            ))
                        )}
                    </TableBody>
                </Table>
                </div>
            </DialogContent>
        </Dialog>
    );
}
