/**
 * 媒体投放管理弹窗 — 投放记录（含状态 + 退款）+ 手动录入
 */
import { useState } from 'react';
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Link, ChevronDown, Loader2 } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { lazyToast } from '@/lib/lazyToast';
import { cn } from '@/lib/utils';
import type { Publication } from '../types';

const REFUND_REASONS = [
    '文章已下架',
    '内容与要求不符',
    '未按时发布',
    '发布平台不对',
    '其他',
];

const STATUS_STYLES: Record<number, string> = {
    0: 'bg-yellow-500/15 text-yellow-400',
    1: 'bg-blue-500/15 text-blue-400',
    2: 'bg-green-500/15 text-green-400',
};

const REFUND_STATUS_MAP: Record<string, { label: string; cls: string }> = {
    pending: { label: '退款审核中', cls: 'text-yellow-400' },
    approved: { label: '已退款', cls: 'text-green-400' },
    rejected: { label: '退款被拒', cls: 'text-red-400' },
};

interface PublicationDialogProps {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    publications: Publication[];
    newPublication: { platform_name: string; platform_url: string; article_title: string; publish_date: string };
    onNewPublicationChange: (val: { platform_name: string; platform_url: string; article_title: string; publish_date: string }) => void;
    onAdd: () => void;
    onRefresh?: () => void;
}

function RefundPanel({ pub, onDone }: { pub: Publication; onDone: () => void }) {
    const [selected, setSelected] = useState('');
    const [custom, setCustom] = useState('');
    const [loading, setLoading] = useState(false);

    const reason = selected === '其他' ? custom : selected;

    const submit = async () => {
        if (!reason.trim()) { lazyToast.error('请选择或输入退款原因'); return; }
        setLoading(true);
        try {
            const res = await authFetch('/api/meijiehezi/refund/request', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ order_id: String(pub.id), reason: reason.trim() }),
            });
            const d = await res.json();
            if (d.status === 'success') {
                lazyToast.success(`退款申请已提交，预计退还 ${d.refund_points} 算力`);
                onDone();
            } else {
                lazyToast.error(d.detail || d.message || '申请失败');
            }
        } catch { lazyToast.error('网络错误'); }
        finally { setLoading(false); }
    };

    return (
        <div className="mt-2 p-2 rounded-md bg-muted/30 space-y-2">
            <div className="flex flex-wrap gap-1.5">
                {REFUND_REASONS.map(r => (
                    <button key={r} onClick={() => setSelected(r)}
                        className={cn('text-[11px] px-2 py-0.5 rounded-full border transition-colors',
                            selected === r ? 'border-foreground bg-foreground/10 text-foreground' : 'border-border text-muted-foreground hover:text-foreground')}>
                        {r}
                    </button>
                ))}
            </div>
            {selected === '其他' && (
                <Input placeholder="请输入退款原因" value={custom} onChange={e => setCustom(e.target.value)}
                    className="h-7 text-xs" />
            )}
            <div className="flex justify-end">
                <Button size="sm" className="h-6 text-[11px] px-3" disabled={!reason.trim() || loading} onClick={submit}>
                    {loading && <Loader2 className="h-3 w-3 mr-1 animate-spin" />}
                    提交退款申请
                </Button>
            </div>
        </div>
    );
}

export function PublicationDialog({ open, onOpenChange, publications, newPublication, onNewPublicationChange, onAdd, onRefresh }: PublicationDialogProps) {
    const [expandedId, setExpandedId] = useState<string | number | null>(null);

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-md">
                <DialogHeader>
                    <DialogTitle>媒体投放管理</DialogTitle>
                </DialogHeader>
                <Tabs defaultValue="list">
                    <TabsList>
                        <TabsTrigger value="list">投放记录 ({publications.length})</TabsTrigger>
                        <TabsTrigger value="add">录入投放</TabsTrigger>
                    </TabsList>

                    {/* 录入投放 */}
                    <TabsContent value="add" className="space-y-4 pt-4">
                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                            <div>
                                <Label>投放平台</Label>
                                <Input placeholder="如：百家号、搜狐号、知乎等" value={newPublication.platform_name}
                                    onChange={e => onNewPublicationChange({ ...newPublication, platform_name: e.target.value })} />
                            </div>
                            <div>
                                <Label>发布日期</Label>
                                <Input type="date" className="[&::-webkit-calendar-picker-indicator]:invert [&::-webkit-calendar-picker-indicator]:opacity-70"
                                    value={newPublication.publish_date}
                                    onChange={e => onNewPublicationChange({ ...newPublication, publish_date: e.target.value })} />
                            </div>
                        </div>
                        <div>
                            <Label>文章标题</Label>
                            <Input placeholder="投放文章的标题" value={newPublication.article_title}
                                onChange={e => onNewPublicationChange({ ...newPublication, article_title: e.target.value })} />
                        </div>
                        <div>
                            <Label>文章链接</Label>
                            <Input placeholder="https://..." value={newPublication.platform_url}
                                onChange={e => onNewPublicationChange({ ...newPublication, platform_url: e.target.value })} />
                        </div>
                        <div className="flex justify-end gap-2">
                            <Button variant="outline" onClick={() => onOpenChange(false)}>取消</Button>
                            <Button onClick={onAdd}>添加投放</Button>
                        </div>
                    </TabsContent>

                    {/* 投放记录 */}
                    <TabsContent value="list" className="pt-2">
                        <div className="max-h-[50vh] overflow-y-auto overflow-x-hidden">
                            {publications.length === 0 ? (
                                <div className="text-center text-muted-foreground py-8 text-sm space-y-1">
                                    <p>暂无投放记录</p>
                                    <p className="text-[11px] opacity-70">代发订单完成后自动同步到这里(约 10 分钟)· 非代发渠道请走"录入投放"</p>
                                </div>
                            ) : publications.map(pub => {
                                const refundInfo = pub.refund ? REFUND_STATUS_MAP[pub.refund.status] : null;
                                const isExpanded = expandedId === pub.id;

                                return (
                                    <div key={pub.id} className="py-2 px-1 border-b border-border/20 last:border-0">
                                        {/* 第一行：状态 + 平台 + 日期 + 链接 */}
                                        <div className="flex items-center gap-1.5 text-xs">
                                            <span className={cn('shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium', STATUS_STYLES[pub.status] || 'bg-muted text-muted-foreground')}>
                                                {pub.status_label}
                                            </span>
                                            <span className="text-muted-foreground shrink-0">{pub.platform_name}</span>
                                            <span className="flex-1" />
                                            <span className="text-muted-foreground shrink-0">{pub.publish_date}</span>
                                            {pub.platform_url && (
                                                <a href={pub.platform_url} target="_blank" rel="noopener noreferrer" className="shrink-0">
                                                    <Link className="h-3 w-3 text-brand" />
                                                </a>
                                            )}
                                        </div>

                                        {/* 第二行：标题 */}
                                        <p className="text-xs mt-0.5 truncate text-foreground/80" title={pub.article_title}>
                                            {pub.article_title || '-'}
                                        </p>

                                        {/* 第三行：退款状态 或 退款按钮 */}
                                        {refundInfo && (
                                            <div className="mt-1 flex items-center gap-1.5">
                                                <span className={cn('text-[11px] font-medium', refundInfo.cls)}>{refundInfo.label}</span>
                                                {pub.refund?.admin_note && (
                                                    <span className="text-[10px] text-muted-foreground">({pub.refund.admin_note})</span>
                                                )}
                                            </div>
                                        )}

                                        {pub.can_refund && (
                                            <button onClick={() => setExpandedId(isExpanded ? null : pub.id)}
                                                className="mt-1 text-[11px] text-muted-foreground hover:text-foreground flex items-center gap-0.5 transition-colors">
                                                申请退款 <ChevronDown className={cn("h-3 w-3 transition-transform", isExpanded && "rotate-180")} />
                                            </button>
                                        )}

                                        {pub.can_refund && isExpanded && (
                                            <RefundPanel pub={pub} onDone={() => { setExpandedId(null); onRefresh?.(); }} />
                                        )}
                                    </div>
                                );
                            })}
                        </div>
                    </TabsContent>
                </Tabs>
            </DialogContent>
        </Dialog>
    );
}
