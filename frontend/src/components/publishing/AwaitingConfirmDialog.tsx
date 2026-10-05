/**
 * 待确认发布弹框 [2026-04-30 重写版]
 *
 * 当 mhz API 返回 confirm code（203/204/205）时，后端把 item 改为 awaiting_confirmation
 * 状态。前端拉到这些 item 后，本组件展示**全部列表**让用户逐个处理：
 *   - 列表式展示，一次看清所有待确认项及其敏感词
 *   - 点"去处理" → 内联打开文章原文编辑器，自动高亮 mhz 提示的敏感词
 *   - 编辑器内可手动搜索关键词（持续高亮）
 *   - 改完保存 → "保存并仍然发布"会先保存文章再 confirm 重投（mhz 收到的是新内容）
 *   - 也可以「取消发布」自动退款，或「我先回去改」延后处理
 *
 * 旧版每次只显示一个、不能编辑、不能搜索 → 用户被迫"全取消"。
 */

import { useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Loader2, Search, ArrowLeft, AlertCircle } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';

export interface AwaitingItem {
    item_id: number;
    order_id: number;
    article_id: number | null;
    article_title: string;
    media_id: number | null;
    media_name: string;
    media_type: string;
    brand_id: number | null;
    pending_codes: number[];
    pending_msg: string;
    awaiting_since: string | null;
}

interface ItemDetail {
    item: AwaitingItem;
    article: { id: number | null; content: string };
    sensitive_keywords: string[];
}

const FALLBACK_MSG_BY_CODE: Record<number, string> = {
    203: '检测到您此前已向该媒体发送过相同内容的文章，是否仍要继续发布？',
    204: '检测到此标题与您此前发送过的文章重复，是否仍要继续发布？',
    205: '本次发布内容需要您再次确认，是否继续？',
};

function buildPromptMessage(item: AwaitingItem): string {
    const trimmed = (item.pending_msg || '').trim();
    if (trimmed) return trimmed;
    for (const code of item.pending_codes) {
        if (FALLBACK_MSG_BY_CODE[code]) return FALLBACK_MSG_BY_CODE[code];
    }
    return '本次发布需要您再次确认，是否继续？';
}

interface Props {
    /** 待确认列表 */
    items: AwaitingItem[];
    /** 列表是否打开 */
    open: boolean;
    /** 关闭整个弹窗 */
    onClose: () => void;
    /** 用户做出决定后回调（confirm/cancel）→ 上层重新拉列表 */
    onResolved?: () => void;
}

export function AwaitingConfirmDialog({ items, open, onClose, onResolved }: Props) {
    // 当前在编辑哪条 item（null 表示在列表页）
    const [activeItemId, setActiveItemId] = useState<number | null>(null);
    const [detail, setDetail] = useState<ItemDetail | null>(null);
    const [loadingDetail, setLoadingDetail] = useState(false);
    const [editorContent, setEditorContent] = useState('');
    const [searchKeyword, setSearchKeyword] = useState('');
    const [saving, setSaving] = useState(false);
    const [resolving, setResolving] = useState(false);
    const [confirmDialog, askConfirm] = useConfirmDialog();

    /*
     * 🔴 [#191 · 2026-09-13] 本组件里**沙盒教程的全部分支已删除**。
     *
     *    原先有 `tutorialLock` + 三个阶段(`step3-pub-await-process/edit/confirm`)
     *    与三处 FeatureTooltip。2026-07-27 的 88134ebd7 把沙盒发布流程改成
     *    「提交成功后不再创建/等待媒介审核任务」,沙盒侧那句「推进到 await-process」
     *    随之删掉 —— 这条子链**从那天起就不可达**,而这里的消费者留了七周,
     *    看起来完全是活代码(下一个人会照着它改)。#190 的类门把它照出来。
     *
     *    🔴 真实态行为**未变**:这些分支的前提都是 `sandboxActive`,非沙盒下恒假 ——
     *    `tutorialLock` 恒 false,而 `LazyFeatureTooltip` 在 `disabled` 时直接返回
     *    `<>{children}</>`(连 `wrapClassName` 的 div 都不渲染)。删掉的是
     *    「只在沙盒里才有意义、而沙盒又到不了」的那一层。
     *
     *    要恢复教程的这一段:先在沙盒侧把 `await-process` **重新产生出来**,
     *    再回来加消费者 —— 反过来做就又是一段死引导。
     */

    // 弹窗关闭时重置
    useEffect(() => {
        if (!open) {
            setActiveItemId(null);
            setDetail(null);
            setEditorContent('');
            setSearchKeyword('');
        }
    }, [open]);

    // 切换 active item 时拉详情
    useEffect(() => {
        if (activeItemId == null) {
            setDetail(null);
            return;
        }
        let cancelled = false;
        setLoadingDetail(true);
        setSearchKeyword('');
        authFetch(`/api/meijiehezi/awaiting-confirmations/${activeItemId}/detail`)
            .then(r => r.json())
            .then((d) => {
                if (cancelled) return;
                if (d.status !== 'success') {
                    toast.error('加载详情失败', { description: d.detail || '' });
                    setActiveItemId(null);
                    return;
                }
                setDetail(d as ItemDetail);
                setEditorContent(d.article?.content || '');
            })
            .catch((err) => {
                if (cancelled) return;
                toast.error('网络异常', { description: String(err) });
                setActiveItemId(null);
            })
            .finally(() => {
                if (!cancelled) setLoadingDetail(false);
            });
        return () => { cancelled = true; };
    }, [activeItemId]);

    const callConfirm = async (itemId: number, action: 'confirm' | 'cancel') => {
        setResolving(true);
        try {
            const res = await authFetch(`/api/meijiehezi/confirm/${itemId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action }),
            });
            if (!res.ok) {
                const body = await res.text();
                toast.error(action === 'confirm' ? '确认失败' : '取消失败', {
                    description: body || `状态码 ${res.status}`,
                });
                return false;
            }
            const data = await res.json();
            if (action === 'confirm') {
                toast.success('已确认，正在重新提交');
            } else {
                const refunded = data?.refunded_points;
                toast.success(refunded ? `已取消并退还 ${refunded} 算力` : '已取消发布');
            }
            onResolved?.();
            return true;
        } catch (err) {
            toast.error('网络异常', { description: String(err) });
            return false;
        } finally {
            setResolving(false);
        }
    };

    const saveArticle = async (): Promise<boolean> => {
        if (!detail?.article.id) return false;
        setSaving(true);
        try {
            const res = await authFetch(`/api/articles/${detail.article.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: editorContent }),
            });
            if (!res.ok) {
                const body = await res.text();
                toast.error('保存失败', { description: body || `状态码 ${res.status}` });
                return false;
            }
            toast.success('文章已保存');
            return true;
        } catch (err) {
            toast.error('网络异常', { description: String(err) });
            return false;
        } finally {
            setSaving(false);
        }
    };

    const handleSaveAndConfirm = async () => {
        if (!detail) return;
        // 1. 保存文章
        if (!(await saveArticle())) return;
        // 2. 确认提交（resubmit 路径会重新从 articles 表拉 content，自动用新内容）
        const ok = await callConfirm(detail.item.item_id, 'confirm');
        if (ok) {
            // 处理完一条 → 回到列表，让用户继续处理下一条
            setActiveItemId(null);
        }
    };

    const handleCancelOnly = async (itemId: number) => {
        const ok = await callConfirm(itemId, 'cancel');
        if (ok) setActiveItemId(null);
    };

    // [CTO-15.23 2026-05-17 老板要"一键处理"]
    // 列表里逐个点取消太累 · 加批量"全部取消并退款" · 并行调 confirm cancel
    const handleBulkCancelAll = async () => {
        if (items.length === 0) return;
        if (!(await askConfirm({ title: `确认要取消全部 ${items.length} 篇待确认发布并退款吗?`, confirmLabel: '全部取消并退款', danger: true }))) return;
        setResolving(true);
        try {
            type BulkResult = { ok: boolean; refunded: number };
            const results: BulkResult[] = await Promise.all(items.map(async (it): Promise<BulkResult> => {
                try {
                    const r = await authFetch(`/api/meijiehezi/confirm/${it.item_id}`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ action: 'cancel' }),
                    });
                    if (!r.ok) return { ok: false, refunded: 0 };
                    const d = await r.json();
                    return { ok: true, refunded: Number(d?.refunded_points) || 0 };
                } catch {
                    return { ok: false, refunded: 0 };
                }
            }));
            const okCount = results.filter(r => r.ok).length;
            const totalRefunded = results.reduce((sum, r) => sum + (r.refunded || 0), 0);
            if (okCount === items.length) {
                toast.success(`已全部取消 ${okCount} 篇${totalRefunded ? ` · 退还 ${totalRefunded} 算力` : ''}`);
            } else {
                toast.warning(`取消 ${okCount}/${items.length} 篇成功${totalRefunded ? ` · 退还 ${totalRefunded} 算力` : ''}`, {
                    description: '部分订单取消失败 · 可稍后重试',
                });
            }
            onResolved?.();
        } finally {
            setResolving(false);
        }
    };

    // 高亮关键词（敏感词 + 用户搜索词）
    const highlightTerms = useMemo(() => {
        if (!detail) return [] as string[];
        const list = [...(detail.sensitive_keywords || [])];
        if (searchKeyword.trim()) list.push(searchKeyword.trim());
        return list.filter((s, i) => s && list.indexOf(s) === i);
    }, [detail, searchKeyword]);

    // 计算当前关键词在文章里出现的次数（让用户知道还有多少处要改）
    const matchCount = useMemo(() => {
        if (!editorContent || highlightTerms.length === 0) return 0;
        let total = 0;
        for (const t of highlightTerms) {
            const re = new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g');
            const m = editorContent.match(re);
            if (m) total += m.length;
        }
        return total;
    }, [editorContent, highlightTerms]);

    // 沙盒态: 只统计敏感词(不含用户搜索词) · 全部删干净后推进到"保存并仍然发布"引导
    const sensitiveLeft = useMemo(() => {
        if (!detail || !editorContent) return 0;
        let total = 0;
        for (const t of (detail.sensitive_keywords || [])) {
            const re = new RegExp(t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'g');
            const m = editorContent.match(re);
            if (m) total += m.length;
        }
        return total;
    }, [detail, editorContent]);

    // 列表视图
    if (!activeItemId) {
        return (
            <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
                <DialogContent className="max-w-2xl max-h-[80vh] flex flex-col">
                    {confirmDialog}
                    <DialogHeader>
                        <DialogTitle>需要您确认（{items.length} 篇）</DialogTitle>
                    </DialogHeader>
                    <div className="text-xs text-muted-foreground mb-2">
                        发布通道检测到这些文章可能含敏感词或与历史文章重复。点「去处理」可以**直接修改文章原文**（自动标出敏感词位置）后再发布，也可以取消退款。若 24 小时内未处理订单将自动取消并退款。
                    </div>
                    <div className="space-y-2 overflow-y-auto flex-1 -mx-1 px-1">
                        {items.length === 0 ? (
                            <div className="text-center py-12 text-muted-foreground">没有待确认的订单 ✓</div>
                        ) : items.map(it => {
                            const msg = buildPromptMessage(it);
                            return (
                                <div key={it.item_id} className="rounded-md border border-amber-300/40 bg-amber-50/30 p-3 dark:bg-amber-950/20">
                                    <div className="flex items-start gap-3">
                                        <div className="flex-1 min-w-0">
                                            <div className="text-sm font-medium truncate">《{it.article_title || '未命名'}》</div>
                                            <div className="flex gap-1.5 mt-1 text-xs text-muted-foreground items-center flex-wrap">
                                                <span>{it.media_name || '—'}</span>
                                                <Badge variant="outline" className="text-[10px]">
                                                    {it.media_type === 'wemedia' ? '自媒体' : '软文'}
                                                </Badge>
                                                {it.pending_codes.map(c => (
                                                    <Badge key={c} variant="outline" className="text-[10px] bg-amber-500/10 text-amber-400 border-amber-500/30">code {c}</Badge>
                                                ))}
                                            </div>
                                            <div className="mt-2 text-xs text-amber-800 dark:text-amber-200">
                                                <AlertCircle className="inline size-3 mr-1 -mt-0.5" />
                                                {msg}
                                            </div>
                                        </div>
                                    </div>
                                    <div className="flex gap-2 mt-2 justify-end">
                                        <Button variant="ghost" size="sm" disabled={resolving} className="text-xs h-7"
                                            onClick={() => handleCancelOnly(it.item_id)}>
                                            取消发布
                                        </Button>
                                        <Button size="sm" disabled={resolving} className="text-xs h-7"
                                            onClick={() => setActiveItemId(it.item_id)}>
                                            去处理 →
                                        </Button>
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                    <div className="flex justify-between gap-2 pt-2 border-t">
                        {/* 一键取消全部 + 退款 · 老板新增 2026-05-17 · 沙盒引导期间禁用防逃逸 */}
                        <Button
                            variant="ghost"
                            size="sm"
                            disabled={resolving || items.length === 0}
                            onClick={handleBulkCancelAll}
                            className="text-xs text-red-500 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-950/30"
                        >
                            {resolving ? <Loader2 className="size-3 animate-spin mr-1" /> : null}
                            一键取消全部并退款
                        </Button>
                        <Button variant="outline" size="sm" onClick={onClose}>稍后处理</Button>
                    </div>
                </DialogContent>
            </Dialog>
        );
    }

    // 编辑视图
    return (
        <Dialog open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
            <DialogContent className="max-w-3xl max-h-[90vh] flex flex-col">
                <DialogHeader>
                    <div className="flex items-center gap-2">
                        <Button variant="ghost" size="sm" className="h-7 px-2" onClick={() => setActiveItemId(null)}>
                            <ArrowLeft className="size-3.5 mr-1" />返回列表
                        </Button>
                        <DialogTitle className="text-sm">
                            编辑文章 - {detail?.item.article_title || '加载中...'}
                        </DialogTitle>
                    </div>
                </DialogHeader>
                {loadingDetail || !detail ? (
                    <div className="flex justify-center py-12"><Loader2 className="h-6 w-6 animate-spin text-muted-foreground" /></div>
                ) : (
                    <div className="space-y-3 flex-1 flex flex-col overflow-hidden">
                        {/* mhz 提示 */}
                        <div className="rounded-md border border-amber-300/40 bg-amber-50/30 p-3 text-xs dark:bg-amber-950/20">
                            <div className="text-amber-900 dark:text-amber-200 font-medium mb-1">发布通道提示</div>
                            <div className="text-amber-800 dark:text-amber-200">{buildPromptMessage(detail.item)}</div>
                            {detail.sensitive_keywords.length > 0 && (
                                <div className="mt-1.5 flex gap-1 flex-wrap text-[11px]">
                                    <span className="text-muted-foreground">已识别敏感词：</span>
                                    {detail.sensitive_keywords.map(w => (
                                        <Badge key={w} variant="outline" className="text-[10px] bg-red-500/15 text-red-400 border-red-500/30">{w}</Badge>
                                    ))}
                                </div>
                            )}
                        </div>

                        {/* 关键词搜索 */}
                        <div className="flex items-center gap-2">
                            <div className="relative flex-1">
                                <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground" />
                                <Input
                                    placeholder="搜索其他关键词（敏感词已自动高亮）"
                                    value={searchKeyword}
                                    onChange={e => setSearchKeyword(e.target.value)}
                                    className="pl-8 h-8 text-xs"
                                />
                            </div>
                            <Badge variant="outline" className={cn('text-xs', matchCount > 0 ? 'bg-red-500/15 text-red-400 border-red-500/30' : 'bg-green-500/15 text-green-400 border-green-500/30')}>
                                {matchCount > 0 ? `还有 ${matchCount} 处需要处理` : '✓ 文中已无敏感词/搜索词'}
                            </Badge>
                        </div>

                        {/* 单块就地编辑器 (带敏感词高亮) */}
                        <ContentEditor
                            value={editorContent}
                            onChange={setEditorContent}
                            highlightTerms={highlightTerms}
                        />
                    </div>
                )}
                <div className="flex justify-between gap-2 pt-2 border-t">
                    <Button variant="ghost" size="sm" disabled={saving || resolving}
                        onClick={() => detail && handleCancelOnly(detail.item.item_id)}>
                        取消发布并退款
                    </Button>
                    <div className="flex gap-2">
                        <Button variant="outline" size="sm" disabled={saving || resolving}
                            onClick={async () => { if (await saveArticle()) toast.success('已保存，可继续编辑'); }}>
                            {saving ? '保存中...' : '仅保存'}
                        </Button>
                        <Button size="sm" disabled={saving || resolving}
                            onClick={handleSaveAndConfirm}>
                            {saving ? '保存中...' : resolving ? '提交中...' : '保存并仍然发布'}
                        </Button>
                    </div>
                </div>
            </DialogContent>
        </Dialog>
    );
}

/**
 * 编辑器：单块就地编辑 + 敏感词高亮 (overlay 技术)
 *
 * 不再用"左编辑/右预览"双栏 (两边滚动不同步 · 看着累)。
 * 改成一个 textarea 直接编辑, 底下叠一层等位的高亮背景层:
 *   - backdrop: absolute inset-0 · 渲染带 <mark> 的同一段文字 · pointer-events-none
 *   - textarea: absolute inset-0 · 文字透明只留光标 · 用户实际在这里打字
 *   两层 box-model 完全一致 (字体/行高/padding/换行规则), 文字逐字对齐;
 *   滚动天然同步 (textarea onScroll → 写回 backdrop scrollTop)。
 */
const EDITOR_TEXT_CLASS =
    'w-full h-full p-3 text-xs font-mono leading-5 whitespace-pre-wrap break-words box-border m-0 border-0';

function ContentEditor({
    value, onChange, highlightTerms,
}: {
    value: string;
    onChange: (v: string) => void;
    highlightTerms: string[];
}) {
    const backdropRef = useRef<HTMLDivElement>(null);
    const taRef = useRef<HTMLTextAreaElement>(null);
    const jumpedRef = useRef(false);

    // textarea 滚动 → 同步背景高亮层
    const syncScroll = () => {
        if (taRef.current && backdropRef.current) {
            backdropRef.current.scrollTop = taRef.current.scrollTop;
            backdropRef.current.scrollLeft = taRef.current.scrollLeft;
        }
    };

    // 进编辑器后自动跳到第一个敏感词并选中 · 只做一次 (避免编辑过程中光标乱跳)
    //   [2026-05-28 v3] 之前用 (line-3)*lineHeight 算 scrollTop 不准 (中文换行 / 行高估计不准) ·
    //   现在直接 querySelector 拿 backdrop 内第一个 <mark> 元素 scrollIntoView ·
    //   backdrop 和 textarea 是 absolute inset-0 + 同字体/行高/padding · 滚动 backdrop 后 sync 给 textarea
    useEffect(() => {
        if (jumpedRef.current) return;
        if (!value || highlightTerms.length === 0) return;
        let firstIdx = -1;
        let firstLen = 0;
        for (const t of highlightTerms) {
            const idx = value.indexOf(t);
            if (idx >= 0 && (firstIdx === -1 || idx < firstIdx)) { firstIdx = idx; firstLen = t.length; }
        }
        if (firstIdx < 0) return;

        let attempts = 0;
        let timer = 0;
        const tryJump = () => {
            attempts += 1;
            const ta = taRef.current;
            const backdrop = backdropRef.current;
            const mark = backdrop?.querySelector('mark') as HTMLElement | null;
            if (!ta || !backdrop || !mark) {
                if (attempts < 30) timer = window.setTimeout(tryJump, 100);
                return;
            }
            // mark.offsetTop = mark 在 backdrop 内的像素位置 · 减半屏高度让 mark 居中
            const targetScroll = Math.max(0, mark.offsetTop - backdrop.clientHeight / 2);
            backdrop.scrollTop = targetScroll;
            ta.scrollTop = targetScroll;
            ta.focus({ preventScroll: true });
            ta.setSelectionRange(firstIdx, firstIdx + firstLen);
            // 验证 scrollTop 是否真生效 (容差 2px · 或已 scroll 到底)
            if (Math.abs(ta.scrollTop - targetScroll) < 2 || ta.scrollTop >= ta.scrollHeight - ta.clientHeight - 2) {
                jumpedRef.current = true;
                return;
            }
            if (attempts < 30) timer = window.setTimeout(tryJump, 100);
        };
        // 首次延迟 200ms 等 dialog 进场动画 + backdrop dangerouslySetInnerHTML 渲染完
        timer = window.setTimeout(tryJump, 200);
        return () => { window.clearTimeout(timer); };
    }, [value, highlightTerms]);

    const highlighted = useMemo(() => {
        // 末尾补一个换行 · 保证背景层与 textarea 在最后一行的高度一致 (textarea 末行后留白)
        const safe = escapeHtml(value) + '\n';
        if (highlightTerms.length === 0) return safe;
        const pattern = highlightTerms
            .map(t => t.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))
            .join('|');
        const re = new RegExp(`(${pattern})`, 'g');
        // mark 内 inline -webkit-text-fill-color: currentColor · 防被父级 cascade 透明
        return safe.replace(re, '<mark class="bg-red-400/50 text-foreground rounded-[2px]" style="-webkit-text-fill-color: currentColor;">$1</mark>');
    }, [value, highlightTerms]);

    return (
        <div
            data-mobile-coach="awaiting-editor"
            className="relative flex-1 min-h-[300px] border rounded-md overflow-hidden bg-background"
        >
            {/* 背景高亮层 · 与 textarea 等位渲染同一段文字 + 红色 mark
                [2026-05-28] WebkitTextFillColor: currentColor · 防全局/父级 -webkit-text-fill-color
                透明 cascade 把 backdrop 文字也整透明 (跟 textarea fix 配套, 双管齐下) */}
            <div
                ref={backdropRef}
                aria-hidden
                className={cn(EDITOR_TEXT_CLASS, 'absolute inset-0 z-0 overflow-auto pointer-events-none text-foreground')}
                style={{ WebkitTextFillColor: 'currentColor' }}
                dangerouslySetInnerHTML={{ __html: highlighted }}
            />
            {/* 编辑层 · 文字透明只显光标, 实际编辑在这里
                [2026-05-28] WebkitTextFillColor: transparent · iOS Safari / Chrome Android
                上 textarea 文字色被 -webkit-text-fill-color 默认值覆盖, text-transparent 不生效 ·
                导致 textarea 实色文字盖住 backdrop 的红色 mark · 移动端看不到敏感词高亮 */}
            <textarea
                ref={taRef}
                value={value}
                onChange={e => onChange(e.target.value)}
                onScroll={syncScroll}
                spellCheck={false}
                /* [2026-05-28] awaiting-editor-input 配合 globals.css 把原生 ::selection 整透明 ·
                   setSelectionRange 选中第 1 个敏感词后, 原生蓝底白字会盖住 backdrop 的红 mark ·
                   隐藏蓝色选区让红 mark 透出来, textarea 仍真实选中 → 用户按 Delete 仍能删 */
                className={cn(EDITOR_TEXT_CLASS, 'awaiting-editor-input absolute inset-0 z-10 overflow-auto resize-none bg-transparent text-transparent outline-none')}
                style={{ caretColor: 'hsl(var(--foreground))', WebkitTextFillColor: 'transparent' }}
            />
        </div>
    );
}

function escapeHtml(s: string): string {
    return s
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
