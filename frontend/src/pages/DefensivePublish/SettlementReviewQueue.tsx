/**
 * Z-1 —— 平台 admin 资金核验队列。
 *
 * 路由:`/admin/defensive-geo-settlement-review`
 *
 * 🔴 这一页每按一次按钮就是一次真的动钱,所以三条纪律
 * ══════════════════════════════════════════════════════════════════════
 * ① **三个按钮从服务端 `actionCatalog` 渲染**,不在前端写死三条。
 *    catalog 里同时带 `label`(文案已内含资金方向:「确认已执行（扣除这笔算力）」/
 *    「确认未执行（退回这笔算力）」)与 `reasonRequired`。
 *    前端**不**自己维护 action→扣/退 的映射 —— 那是资金方向,SSOT 在后端;
 *    在这里再写一份,后端哪天调了顺序或语义,这页就会对着 admin 说反话。
 *
 * ② **理由必填由服务端的 `reasonRequired` 驱动**,不填按钮 disabled。
 *    (后端 `settlement_review.apply_admin_action` 对 `admin_hold` 硬校验:
 *     「没有理由的『先放着』等于把死路写进账本」。)
 *
 * ③ **二次确认弹窗必须把钱说死**:动作原文 + 本次确切算力数。
 *    admin 在这一页看到的每一个数字都来自服务端,前端不做任何金额换算。
 *
 * 「超没超 7 天」也读服务端的 `isStale` / `staleOverDays`,
 * 不拿 `pendingSeconds` 自己除 86400 —— 阈值一旦在前端出现就会和后端漂。
 */

import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, Loader2, RefreshCw, Wallet } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import {
    Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { formatApiErrorForDisplay } from '@/lib/api';
import { toast } from 'sonner';
import { applySettlementReviewAction, fetchSettlementReviewQueue } from './api';
import type {
    ReviewActionCatalogItem, ReviewQueueEntry, ReviewQueueResponse,
} from './contracts';
import { serverCopy } from './contracts';
import { ErrorNotice, ServerLabel } from './components';

interface PendingAction {
    entry: ReviewQueueEntry;
    action: ReviewActionCatalogItem;
}

export default function SettlementReviewQueue() {
    const [data, setData] = useState<ReviewQueueResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);
    const [pending, setPending] = useState<PendingAction | null>(null);
    const [reason, setReason] = useState('');
    const [submitting, setSubmitting] = useState(false);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            setData(await fetchSettlementReviewQueue({ limit: 100 }));
            setError(null);
        } catch (e) {
            setError(formatApiErrorForDisplay(e, '队列暂时读不到 · 请稍后重试', 'admin'));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { void load(); }, [load]);

    const openAction = (entry: ReviewQueueEntry, action: ReviewActionCatalogItem) => {
        setPending({ entry, action });
        setReason('');
    };

    // 🔴 理由必填由服务端 catalog 的 reasonRequired 决定,前端不自己定哪个动作要理由。
    const reasonBlocked = !!pending?.action.reasonRequired && reason.trim().length === 0;

    const doApply = async () => {
        if (!pending || reasonBlocked) return;
        setSubmitting(true);
        try {
            const res = await applySettlementReviewAction(pending.entry.publishCommandId, {
                action: pending.action.action,
                reason: reason.trim() ? reason.trim() : undefined,
            });
            // 结果也用服务端的资金态文案播报,不在前端另写一句「已扣/已退」。
            const done = serverCopy(res.fundingStateLabel);
            toast.success(done.ok ? done.text : '已处理');
            setPending(null);
            setReason('');
            await load();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '处置没有成功 · 请稍后重试', 'admin'));
        } finally {
            setSubmitting(false);
        }
    };

    const entries = data?.entries ?? [];
    const catalog = data?.actionCatalog ?? [];

    return (
        <div className="container mx-auto max-w-7xl space-y-6 py-6">
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                    <Wallet className="h-6 w-6" aria-hidden />
                    <h1 className="text-2xl font-bold">发布资金核验队列</h1>
                </div>
                <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
                    <RefreshCw className="mr-1 h-4 w-4" aria-hidden /> 刷新
                </Button>
            </div>

            {error && <ErrorNotice text={error} />}

            <Card>
                <CardHeader>
                    <CardTitle className="text-base">
                        待核验 {data?.totalPending ?? 0} 条
                        {data && (
                            <span className="ml-2 text-sm font-normal text-muted-foreground">
                                · 超过 {data.staleOverDays} 天未处理会标红
                            </span>
                        )}
                    </CardTitle>
                </CardHeader>
                <CardContent className="p-0">
                    <div className="overflow-x-auto">
                        <table className="w-full text-sm">
                            <thead>
                                <tr className="border-b bg-muted/50">
                                    <th className="p-3 text-left">命令号</th>
                                    <th className="p-3 text-left">租户 / 品牌</th>
                                    <th className="p-3 text-right">金额</th>
                                    <th className="p-3 text-left">资金态</th>
                                    <th className="p-3 text-right">已等待</th>
                                    <th className="p-3 text-left">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {entries.length === 0 && (
                                    <tr>
                                        <td colSpan={6} className="p-6 text-center text-muted-foreground">
                                            {loading ? '正在读取…' : '当前没有待核验的发布命令。'}
                                        </td>
                                    </tr>
                                )}
                                {entries.map((e) => (
                                    <tr
                                        key={e.publishCommandId}
                                        className={`border-b ${e.isStale ? 'bg-destructive/5' : ''}`}
                                        data-stale={e.isStale ? 'true' : 'false'}
                                    >
                                        <td className="p-3 font-mono text-xs">{e.publishCommandId}</td>
                                        <td className="p-3">
                                            <div className="text-xs text-muted-foreground">
                                                租户 {e.tenantOwnerId} · 品牌 {e.brandId}
                                            </div>
                                        </td>
                                        <td className="p-3 text-right font-medium">
                                            {e.exactSettlementPoints} 算力
                                        </td>
                                        <td className="p-3">
                                            <Badge variant="outline">
                                                <ServerLabel
                                                    value={e.fundingStateLabel}
                                                    missingHint="资金态文案后端暂未下发"
                                                />
                                            </Badge>
                                        </td>
                                        <td className="p-3 text-right">
                                            <span className={e.isStale ? 'font-semibold text-destructive' : ''}>
                                                {/* 天数只是把服务端秒数换个单位显示;是否超期用服务端 isStale */}
                                                {Math.floor(e.pendingSeconds / 86400)} 天
                                            </span>
                                            {e.isStale && (
                                                <span className="ml-1 inline-flex items-center text-destructive">
                                                    <AlertTriangle className="h-3.5 w-3.5" aria-hidden />
                                                    <span className="sr-only">已超期</span>
                                                </span>
                                            )}
                                        </td>
                                        <td className="p-3">
                                            <div className="flex flex-wrap gap-1.5">
                                                {catalog.map((a) => {
                                                    const v = serverCopy(a.label);
                                                    return (
                                                        <Button
                                                            key={a.action}
                                                            size="sm"
                                                            variant="outline"
                                                            disabled={!v.ok}
                                                            onClick={() => openAction(e, a)}
                                                            data-server-copy-missing={v.ok ? undefined : v.why}
                                                        >
                                                            {v.ok ? v.text : '—'}
                                                        </Button>
                                                    );
                                                })}
                                            </div>
                                        </td>
                                    </tr>
                                ))}
                            </tbody>
                        </table>
                    </div>
                </CardContent>
            </Card>

            {/* ── 二次确认:把钱说死 ─────────────────────────────────────── */}
            <Dialog open={pending !== null} onOpenChange={(o) => { if (!o) setPending(null); }}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>再确认一次</DialogTitle>
                    </DialogHeader>
                    {pending && (
                        <div className="space-y-3">
                            {/* 🔴 动作原文来自服务端,它本身就写明了「扣除」还是「退回」 */}
                            <p className="text-sm font-medium" data-settlement-confirm-action>
                                <ServerLabel value={pending.action.label} />
                            </p>
                            <p className="text-sm" data-settlement-confirm-amount>
                                本次涉及 <span className="font-semibold">
                                    {pending.entry.exactSettlementPoints}
                                </span> 算力，命令号 {pending.entry.publishCommandId}。
                            </p>
                            <div className="space-y-1.5">
                                <Label htmlFor="defgeo-review-reason">
                                    理由{pending.action.reasonRequired ? '（必填）' : '（选填）'}
                                </Label>
                                <Textarea
                                    id="defgeo-review-reason"
                                    value={reason}
                                    onChange={(ev) => setReason(ev.target.value)}
                                    rows={3}
                                    maxLength={1000}
                                    placeholder={pending.action.reasonRequired
                                        ? '写清为什么先放着 —— 没有理由的「先放着」会变成死账'
                                        : '可以补充说明'}
                                />
                            </div>
                        </div>
                    )}
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setPending(null)} disabled={submitting}>
                            算了
                        </Button>
                        <Button onClick={() => void doApply()} disabled={submitting || reasonBlocked}>
                            {submitting && <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden />}
                            确认执行
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}
