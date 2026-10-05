/**
 * [CTO-15.23 2026-05-08 监测归档区]
 * 已归档关键词列表 · 支持续费(重启监测)+ 软删除
 * 老板诉求:已达标自动归档 + 归档区续费重新计算服务期 + 不要的可硬删
 */
import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Archive, RotateCcw, Trash2, ChevronDown, ChevronUp, Loader2 } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { lazyToast } from '@/lib/lazyToast';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

interface ArchivedKeyword {
    id: number;
    source?: 'confirmed' | 'extra';
    keyword: string;
    tier: string | null;
    final_price: number | null;
    archived_at: string | null;
    archive_reason: string | null;
    quote_id: number;
    brand_id: number;
    brand_name: string;
}

const REASON_LABELS: Record<string, string> = {
    compliance_complete: '已达标',
    service_expired: '服务到期',
    manual: '手动归档',
    renewed: '已重启',
};

const REASON_COLORS: Record<string, string> = {
    compliance_complete: 'bg-green-500/10 text-green-400 border-green-500/20',
    service_expired: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    manual: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
    renewed: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
};

interface Props {
    quoteId?: number | null;
    brandId?: number | null;
    onChanged?: () => void;
}

export function ArchivedKeywordsList({ quoteId, brandId, onChanged }: Props) {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const [keywords, setKeywords] = useState<ArchivedKeyword[]>([]);
    const [loading, setLoading] = useState(false);
    const [expanded, setExpanded] = useState(false);
    const [actionLoading, setActionLoading] = useState<number | null>(null);

    const loadArchived = useCallback(async () => {
        if (!quoteId && !brandId) return;
        setLoading(true);
        try {
            const params = quoteId ? `quote_id=${quoteId}` : `brand_id=${brandId}`;
            const res = await authFetch(`/api/monitoring/archived-keywords?${params}`);
            const data = await res.json();
            if (res.ok && data.success) {
                setKeywords(data.keywords || []);
            }
        } catch (e: any) {
            console.error('加载归档列表失败', e);
        } finally {
            setLoading(false);
        }
    }, [quoteId, brandId]);

    useEffect(() => {
        if (expanded) loadArchived();
    }, [expanded, loadArchived]);

    const handleRenew = async (kw: ArchivedKeyword) => {
        // [WO_MANUAL_KEYWORD_PARITY 2026-08-16 K3] 🔴 文案必须分来源:手动词**不挂报价单生命周期**,
        //   续费它不会重置那张单的服务期(后端 renew 的 extra 支刻意跳过 service_start_date)。
        //   照抄合同词的"服务期从今天重算"= 对用户说假话,而且会让人以为续一个手动词
        //   会把同单所有合同词的已履约天数清零 —— 那正是 K3 要防的事。
        const isExtraKw = (kw.source || 'confirmed') === 'extra';
        if (!(await askConfirm({
            title: `续费「${kw.keyword}」?`,
            description: isExtraKw
                ? '手动词重新进入监测 · 每天跑每天扣 130 算力\n不影响这张报价单的服务期与已达标天数'
                : '服务期将从今天重新计算 · 重置已达标天数',
            danger: true,
        }))) return;
        setActionLoading(kw.id);
        try {
            const params = new URLSearchParams({ source: kw.source || 'confirmed' });
            const res = await authFetch(`/api/monitoring/keywords/${kw.id}/renew?${params}`, { method: 'POST' });
            const data = await res.json();
            if (res.ok && data.success) {
                lazyToast.success(data.message || `已续费「${kw.keyword}」`);
                await loadArchived();
                onChanged?.();
                // 通知父页面刷新监测列表
                window.dispatchEvent(new Event('monitoring-keywords-refresh'));
            } else {
                lazyToast.error(data?.detail || '续费失败');
            }
        } catch (e: any) {
            lazyToast.error(`续费失败: ${e.message}`);
        } finally {
            setActionLoading(null);
        }
    };

    const handleRestore = async (kw: ArchivedKeyword) => {
        if (!(await askConfirm({ title: `恢复「${kw.keyword}」?`, description: `恢复会保留原服务期和历史达标状态，只把词条放回监测词条列表。` }))) return;
        setActionLoading(kw.id);
        try {
            const params = new URLSearchParams({ source: kw.source || 'confirmed' });
            const res = await authFetch(`/api/monitoring/keywords/${kw.id}/restore?${params}`, { method: 'POST' });
            const data = await res.json();
            if (res.ok && data.success) {
                lazyToast.success(data.message || `已恢复「${kw.keyword}」`);
                await loadArchived();
                onChanged?.();
                window.dispatchEvent(new Event('monitoring-keywords-refresh'));
            } else {
                lazyToast.error(data?.detail || '恢复失败');
            }
        } catch (e: any) {
            lazyToast.error(`恢复失败: ${e.message}`);
        } finally {
            setActionLoading(null);
        }
    };

    const handleDelete = async (kw: ArchivedKeyword) => {
        if (!(await askConfirm({ title: `确认删除「${kw.keyword}」?`, description: `删除后将无法在归档区找回(实际为软删除 · 保留追溯)`, danger: true }))) return;
        setActionLoading(kw.id);
        try {
            const params = new URLSearchParams({ source: kw.source || 'confirmed' });
            const res = await authFetch(`/api/monitoring/keywords/${kw.id}/hard-delete?${params}`, { method: 'DELETE' });
            const data = await res.json();
            if (res.ok && data.success) {
                lazyToast.success(`已删除「${kw.keyword}」`);
                await loadArchived();
                onChanged?.();
            } else {
                lazyToast.error(data?.detail || '删除失败');
            }
        } catch (e: any) {
            lazyToast.error(`删除失败: ${e.message}`);
        } finally {
            setActionLoading(null);
        }
    };

    if (!quoteId && !brandId) return null;

    return (
        <Card className="border border-border rounded-xl mt-4">
            <CardHeader
                className="flex flex-row items-center justify-between cursor-pointer p-4 sm:p-5"
                onClick={() => setExpanded(!expanded)}
            >
                <div className="flex items-center gap-3">
                    <Archive className="h-5 w-5 text-muted-foreground" />
                    <CardTitle className="text-lg">已归档词条 {keywords.length > 0 && `(${keywords.length})`}</CardTitle>
                    {!expanded && keywords.length === 0 && (
                        <span className="text-xs text-muted-foreground">展开查看</span>
                    )}
                </div>
                {expanded ? <ChevronUp className="h-4 w-4" /> : <ChevronDown className="h-4 w-4" />}
            </CardHeader>
            {expanded && (
                <CardContent className="p-0">
                    {loading ? (
                        <div className="flex items-center justify-center p-8 text-muted-foreground">
                            <Loader2 className="h-4 w-4 animate-spin mr-2" />
                            加载中...
                        </div>
                    ) : keywords.length === 0 ? (
                        <div className="p-8 text-center text-muted-foreground text-sm">
                            暂无已归档词条 · 词条达标或服务期到会自动出现在这里
                        </div>
                    ) : (
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead>词条</TableHead>
                                    <TableHead>品牌</TableHead>
                                    <TableHead>归档原因</TableHead>
                                    <TableHead>归档时间</TableHead>
                                    <TableHead className="text-right">操作</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {keywords.map(kw => {
                                    const reasonLabel = REASON_LABELS[kw.archive_reason || 'manual'] || kw.archive_reason;
                                    const reasonColor = REASON_COLORS[kw.archive_reason || 'manual'] || 'bg-gray-500/10';
                                    return (
                                        <TableRow key={`${kw.source || 'confirmed'}-${kw.id}`}>
                                            <TableCell className="font-medium">{kw.keyword}</TableCell>
                                            <TableCell className="text-sm text-muted-foreground">{kw.brand_name}</TableCell>
                                            <TableCell>
                                                <Badge variant="outline" className={reasonColor}>
                                                    {reasonLabel}
                                                </Badge>
                                            </TableCell>
                                            <TableCell className="text-sm text-muted-foreground">
                                                {kw.archived_at ? new Date(kw.archived_at).toLocaleDateString('zh-CN') : '-'}
                                            </TableCell>
                                            <TableCell className="text-right">
                                                <div className="flex items-center justify-end gap-1">
                                                    <Button
                                                        variant="ghost"
                                                        size="sm"
                                                        className="text-blue-500 hover:text-blue-400 hover:bg-blue-500/10 h-7 text-xs"
                                                        onClick={() => handleRestore(kw)}
                                                        disabled={actionLoading === kw.id}
                                                        title="恢复 · 保留原服务期和历史状态 · 回到监测词条"
                                                    >
                                                        {actionLoading === kw.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3 mr-1" />}
                                                        恢复
                                                    </Button>
                                                    {/* [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-7] 去掉 source==='extra' 禁用。
                                                        K1:手动词与合同词功能一模一样。后端 renew 端点本单已真正分支
                                                        (原来它接受 source 却写死 UPDATE confirmed_keywords = 串写别人的合同词)。
                                                        🔴 注释必须放在这里(子节点位)——放进属性列表里是 TS1005 语法错,
                                                        真实 build 当场炸;只做静态扫描是看不出来的。 */}
                                                    <Button
                                                        variant="ghost"
                                                        size="sm"
                                                        className="text-purple-500 hover:text-purple-400 hover:bg-purple-500/10 h-7 text-xs"
                                                        onClick={() => handleRenew(kw)}
                                                        disabled={actionLoading === kw.id}
                                                        title={kw.source === 'extra'
                                                            ? '续费 · 重新进入监测(手动词不重算报价单服务期)'
                                                            : '续费 · 重新进入监测 · 服务期从今天重算'}
                                                    >
                                                        {actionLoading === kw.id ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3 mr-1" />}
                                                        续费
                                                    </Button>
                                                    <Button
                                                        variant="ghost"
                                                        size="sm"
                                                        className="text-red-500 hover:text-red-400 hover:bg-red-500/10 h-7 text-xs"
                                                        onClick={() => handleDelete(kw)}
                                                        disabled={actionLoading === kw.id}
                                                        title="软删除 · 保留追溯不真删行"
                                                    >
                                                        <Trash2 className="h-3 w-3 mr-1" />
                                                        删除
                                                    </Button>
                                                </div>
                                            </TableCell>
                                        </TableRow>
                                    );
                                })}
                            </TableBody>
                        </Table>
                    )}
                </CardContent>
            )}
          {confirmDialog}
        </Card>
    );
}
