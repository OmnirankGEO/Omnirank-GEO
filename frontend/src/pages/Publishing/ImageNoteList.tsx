/** Publishing center: finished content left, one account market/publisher right. */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { useClientContext } from '@/context/ClientContext';
import { positiveId, scopedPost } from '@/pages/Writing/imageNoteFlow';
import { Loader2, ExternalLink, ImageOff } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { ImageNotePublishInline } from '@/pages/Writing/ImageNotePublishInline';
import { Badge } from '@/components/ui/badge';
import { ShortVideoAccountMarket, newMarketViewState } from './ShortVideoAccountMarket';
import {
    EMPTY_CTA, EMPTY_CTA_HREF, EMPTY_NOTE, FAILED_REASON_FALLBACK, canPublishRow,
    blockedAllNotice, rowBlockedReason, PUBLISHED_WITHOUT_URL_NOTE, PUBLISH_CHIPS,
    listState, publishRow, type PublishBucket, type PublishRow,
} from './imageNotePublishStatus';

const BADGE_CLASS: Record<PublishBucket, string> = {
    published: 'bg-emerald-500/12 text-emerald-600 border-emerald-500/30 dark:text-emerald-400',
    inflight: 'bg-amber-500/12 text-amber-600 border-amber-500/30 dark:text-amber-400',
    failed: 'bg-red-500/12 text-red-600 border-red-500/30 dark:text-red-400',
    unpublished: 'bg-muted text-muted-foreground border-border',
    unknown: 'bg-muted text-muted-foreground border-border',
    incomplete: 'bg-muted text-muted-foreground border-border',
};
function whenText(raw: string): string {
    if (!raw || !Number.isFinite(Date.parse(raw))) return '';
    const d = new Date(raw), p = (n: number) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
interface Props {
    brandId: number | null;
    preGeoPostId?: number | null;
    onSelectClient?: (brandId: number) => void;
    onSelectPost?: (postId: number) => void;
}

/** Deep-link failures remain actionable even before a customer can be resolved. */
export function ImageNoteFocusNotice({ loading, error, onRetry }: { loading: boolean; error: string; onRetry: () => void }) {
    return <>
        {loading && <p className="px-4 py-2 text-xs text-muted-foreground">正在读取当前作品…</p>}
        {error && <div role="alert" data-testid="pub-imagenote-focus-error" className="space-y-2 px-4 py-2 text-sm text-destructive">
            <p>{error}</p><Button variant="outline" className="min-h-11" onClick={onRetry}>重新读取</Button>
        </div>}
    </>;
}

/** Same two panes on desktop; explicit pane navigation on small screens. */
export function ImageNotePublishingLayout({ content, publisher, mobilePane, onPaneChange }: {
    content: ReactNode; publisher: ReactNode; mobilePane: 'content' | 'accounts';
    onPaneChange: (pane: 'content' | 'accounts') => void;
}) {
    return <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="pub-imagenote-list">
        <nav aria-label="图文发布步骤" className="flex shrink-0 gap-2 border-b p-3 lg:hidden">
            <Button className="min-h-11 flex-1" variant={mobilePane === 'content' ? 'default' : 'outline'}
                aria-pressed={mobilePane === 'content'} onClick={() => onPaneChange('content')}>1 选图文</Button>
            <Button className="min-h-11 flex-1" variant={mobilePane === 'accounts' ? 'default' : 'outline'}
                aria-pressed={mobilePane === 'accounts'} onClick={() => onPaneChange('accounts')}>2 选账号与发布</Button>
        </nav>
        <div className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden lg:flex-row">
            <section aria-label="图文内容列表" data-testid="pub-imagenote-content-pane"
                className={cn('min-h-0 w-full shrink-0 flex-col overflow-y-auto border-border lg:flex lg:w-[40%] lg:min-w-[320px] lg:max-w-[560px] lg:border-r',
                    mobilePane === 'content' ? 'flex' : 'hidden')}>{content}</section>
            <section aria-label="账号选择与发布" data-testid="pub-imagenote-account-pane"
                className={cn('min-h-0 min-w-0 flex-1 overflow-y-auto p-3 sm:p-4 lg:block', mobilePane === 'accounts' ? 'block' : 'hidden')}>{publisher}</section>
        </div>
    </div>;
}

export function ImageNoteList({ brandId, preGeoPostId = null, onSelectClient, onSelectPost }: Props) {
    const { clients } = useClientContext();
    const [selection, setSelection] = useState<{ brandId: number; row: PublishRow } | null>(null);
    const [focusError, setFocusError] = useState('');
    const [focusLoading, setFocusLoading] = useState(false);
    const [page, setPage] = useState(0);
    const [hasMore, setHasMore] = useState(false);
    const [posts, setPosts] = useState<unknown[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [chip, setChip] = useState('unpublished');
    const [counts, setCounts] = useState<Record<string, number>>({});
    const [reloadTick, setReloadTick] = useState(0);
    const [mobilePane, setMobilePane] = useState<'content' | 'accounts'>('content');
    // Only directory presentation survives changing works. Accounts, fees and receipts
    // live inside the publisher keyed by customer + work + frozen revision.
    const [marketView, setMarketView] = useState(newMarketViewState);
    const listScope = useRef('');
    const handoffScope = useRef('');
    const reload = useCallback(() => setReloadTick(n => n + 1), []);
    const selected = selection?.brandId === brandId &&
        (!positiveId(preGeoPostId) || selection.row.id === preGeoPostId) ? selection.row : null;

    const load = useCallback(async (bid: number, signal: AbortSignal) => {
        setLoading(true); setError('');
        try {
            const res = await authFetch(`/api/geo-douyin/posts?brand_id=${bid}&publication_bucket=${chip}&limit=20&offset=${page * 20}`, { signal });
            const data = await res.json();
            if (signal.aborted) return;
            if (!res.ok || data?.status !== 'success') throw new Error('list');
            const next = Array.isArray(data.posts) ? data.posts : [];
            setPosts(next); setHasMore(data.has_more === true); setCounts(data.counts || {});
            // The server, not this list, decides whether publication succeeded.
            setSelection(current => {
                if (current?.brandId !== bid) return current;
                const match = next.find((p: Record<string, unknown>) => Number(p.id) === current.row.id);
                return match ? { brandId: bid, row: publishRow(match) } : current;
            });
        } catch {
            if (!signal.aborted) { setError('图文列表没取到，刷新一下再看'); setPosts([]); setCounts({}); }
        } finally { if (!signal.aborted) setLoading(false); }
    }, [page, chip]);

    useEffect(() => {
        setSelection(null); setPage(0); setCounts({}); setFocusError('');
        setMobilePane('content'); handoffScope.current = '';
    }, [brandId]);

    // Deep links work even when the selected work is outside this page or status filter.
    useEffect(() => {
        setFocusError('');
        // Changing customers removes the deep link and aborts its old request.
        // That request's finally cannot clear the loading state after abort.
        setFocusLoading(false);
        if (!positiveId(preGeoPostId)) return;
        const ac = new AbortController();
        const nextScope = `${brandId}:${preGeoPostId}`;
        setFocusLoading(true);
        void (async () => {
            try {
                const res = await authFetch(`/api/geo-douyin/posts/${preGeoPostId}`, { signal: ac.signal });
                const d = await res.json();
                if (ac.signal.aborted) return;
                if (!res.ok || !d?.post) throw new Error('这条图文暂时打不开，请重试或返回编辑页。');
                const owner = positiveId(d.post.brand_id);
                if (owner !== brandId) {
                    if (owner && !brandId && clients.some(c => positiveId(c.id) === owner)) { onSelectClient?.(owner); return; }
                    throw new Error('这条图文不属于当前客户，请返回编辑页确认，或选择当前客户的作品。');
                }
                const value = scopedPost(d.post, brandId);
                if (!value || !brandId) throw new Error('这条图文的信息不完整，请返回编辑页重试。');
                const row = publishRow({ ...value, cover_url: d.preview_urls?.[0] || '', card_count: (value.oss_keys as unknown[] || []).length });
                setSelection({ brandId, row });
                if (handoffScope.current !== nextScope) { handoffScope.current = nextScope; setMobilePane('accounts'); }
            } catch (e) {
                if (!ac.signal.aborted) { setSelection(null); setFocusError(e instanceof Error ? e.message : '这条图文没取到，请重试。'); }
            } finally { if (!ac.signal.aborted) setFocusLoading(false); }
        })();
        return () => ac.abort();
    }, [brandId, preGeoPostId, clients.length, reloadTick]);

    useEffect(() => {
        const nextScope = `${brandId}:${page}:${chip}`;
        if (listScope.current !== nextScope) setPosts([]);
        listScope.current = nextScope; setError('');
        if (!brandId) return;
        const ac = new AbortController(); void load(brandId, ac.signal);
        return () => ac.abort();
    }, [brandId, load, reloadTick]);

    const state = useMemo(() => listState(posts), [posts]);
    const shown = state.rows;
    const pinned = selected && !shown.some(r => r.id === selected.id) ? selected : null;
    const selectPost = (row: PublishRow) => {
        if (!brandId) return;
        setSelection({ brandId, row }); setFocusError(''); setMobilePane('accounts'); onSelectPost?.(row.id);
    };
    const canViewCommand = selected && selected.revisionId !== null &&
        (canPublishRow(selected) || ['inflight', 'published', 'unknown'].includes(selected.bucket));
    const emptyMarket = <ShortVideoAccountMarket scope={{ platform: '抖音', imageNote: true }}
        viewState={marketView} setViewState={setMarketView} selected={[]} locked onChange={() => undefined} />;

    return <ImageNotePublishingLayout mobilePane={mobilePane} onPaneChange={setMobilePane}
        content={<>
            <div className="space-y-2 border-b p-3 sm:p-4" data-testid="publish-imagenote-moved">
                <div className="flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">已制作的图文</h2>
                    <a className="inline-flex min-h-11 shrink-0 items-center text-xs underline" data-testid="publish-imagenote-goto-studio" href="/writing/image-note">去制作图文 →</a></div>
                <p className="text-xs text-muted-foreground">选一条内容，再在右侧选账号。确认费用后才会发布。</p>
            </div>
            <ImageNoteFocusNotice loading={focusLoading} error={focusError} onRetry={reload} />
            {!brandId ? <div className="space-y-3 p-4" data-testid="pub-imagenote-no-client">
                <h2 className="text-sm font-semibold">要发布哪位客户的图文？</h2>
                <p className="text-xs text-muted-foreground">选择后显示做好的内容，右侧可以先浏览账号。</p>
                <select aria-label="选择图文客户" className="min-h-11 w-full rounded-md border border-input bg-background px-3 text-sm"
                    value="" onChange={e => onSelectClient?.(Number(e.target.value))}>
                    <option value="" disabled>选择客户</option>
                    {clients.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
            </div> : <>
                <div className="flex flex-wrap gap-1 border-b px-3 py-2" data-testid="pub-imagenote-chips">
                    {PUBLISH_CHIPS.map(c => <button key={c.key} type="button" data-testid={`pub-imagenote-chip-${c.key}`}
                        aria-pressed={chip === c.key} onClick={() => { setChip(c.key); setPage(0); }}
                        className={cn('min-h-11 rounded-md px-2.5 text-xs font-medium transition-colors',
                            chip === c.key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-secondary hover:text-foreground')}>
                        {c.label} {loading ? '…' : counts[c.key] ?? 0}
                    </button>)}
                </div>
                <div className="flex-1 space-y-2 px-3 py-2 sm:px-4">
                    {pinned && <div data-testid="pub-imagenote-focused"><p className="py-2 text-xs text-muted-foreground">当前选择 · 不在本页筛选中</p>
                        <ul><ImageNoteContentRow row={pinned} selected onSelect={() => selectPost(pinned)} /></ul></div>}
                    {loading && <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground" data-testid="pub-imagenote-loading"><Loader2 className="size-4 animate-spin" />读取中…</div>}
                    {!loading && error && <div className="space-y-2 py-6 text-sm text-destructive" data-testid="pub-imagenote-error"><p>{error}</p><Button variant="outline" onClick={reload}>重新读取</Button></div>}
                    {!loading && !error && state.kind === 'empty' && counts.all === 0 && <div className="space-y-2 py-8" data-testid="pub-imagenote-empty">
                        <p className="text-sm text-muted-foreground">{EMPTY_NOTE}</p><a className="inline-flex min-h-11 items-center text-sm underline" data-testid="pub-imagenote-empty-cta" href={EMPTY_CTA_HREF}>{EMPTY_CTA}</a></div>}
                    {!loading && !error && (counts.all || 0) > 0 && shown.length === 0 && <div className="space-y-2 py-8" data-testid="pub-imagenote-chip-empty">
                        <p className="text-sm text-muted-foreground">这一档下没有作品。</p><button type="button" className="min-h-11 text-sm underline" data-testid="pub-imagenote-chip-reset"
                            onClick={() => { setChip('all'); setPage(0); }}>看全部 →</button></div>}
                    {!loading && !error && blockedAllNotice(shown) && <p className="py-2 text-xs text-muted-foreground" data-testid="pub-imagenote-blocked-note">{blockedAllNotice(shown)}</p>}
                    {!error && shown.length > 0 && <ul className="divide-y divide-border">{shown.map(row => <ImageNoteContentRow key={row.id} row={row} selected={selected?.id === row.id} onSelect={() => selectPost(row)} />)}</ul>}
                </div>
                <div className="flex items-center justify-between gap-2 border-t p-3 text-xs">
                    <Button variant="outline" className="min-h-11" disabled={loading || page === 0} onClick={() => setPage(p => p - 1)}>上一页</Button><span>第 {page + 1} 页</span>
                    <Button variant="outline" className="min-h-11" disabled={loading || !hasMore} onClick={() => setPage(p => p + 1)}>下一页</Button>
                </div>
            </>}
        </>}
        publisher={<>
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2 border-b pb-3" data-testid="pub-imagenote-current-selection">
                {selected ? <><div className="min-w-0 flex-1"><p className="text-xs text-muted-foreground">当前图文 · {selected.statusLabel}</p><h2 className="mt-1 break-words text-sm font-medium">{selected.title || selected.keyword || '未命名图文'}</h2></div>
                    <a className="inline-flex min-h-11 shrink-0 items-center text-sm underline" href={selected.proofHref}>查看 / 修改内容</a></>
                    : <p className="text-sm text-muted-foreground">{focusLoading ? '正在打开所选图文…' : '先在左侧选择图文，也可以先浏览发布账号。'}</p>}
            </div>
            {canViewCommand && selected && brandId ? <ImageNotePublishInline
                key={`${brandId}:${selected.id}:${selected.revisionId}`}
                brandId={brandId} postId={selected.id} revisionId={selected.revisionId as number}
                keyword={selected.keyword} publicationAllowed={canPublishRow(selected)} publicationStatusLabel={selected.statusLabel}
                platformScope="抖音" viewState={marketView} setViewState={setMarketView}
                onPublished={() => { onSelectPost?.(selected.id); reload(); }} /> : <>
                {selected && <div className="mb-4 space-y-2 text-sm" role="status"><p>{rowBlockedReason(selected) || '请先查看这条内容的发布情况。'}</p>
                    <a className="inline-flex min-h-11 items-center underline" href={selected.proofHref}>返回修改内容 →</a></div>}{emptyMarket}
            </>}
        </>} />;
}

/** Select content without nesting another directory or payment action in a row. */
export function ImageNoteContentRow({ row, selected, onSelect }: { row: PublishRow; selected: boolean; onSelect: () => void }) {
    const when = whenText(row.publishedAt), blockedReason = rowBlockedReason(row);
    return <li className={cn('rounded-md py-2', selected && 'bg-primary/5')} data-testid="pub-imagenote-row" data-post-id={row.id} data-bucket={row.bucket}>
        <button type="button" onClick={onSelect} aria-pressed={selected} aria-label={`选择图文：${row.title || row.keyword || '未命名图文'}`}
            data-testid="pub-imagenote-publish-toggle" className="flex min-h-11 w-full items-start gap-3 px-2 py-2 text-left">
            <span className={cn('mt-1 flex size-4 shrink-0 items-center justify-center rounded-full border', selected ? 'border-primary bg-primary' : 'border-muted-foreground')} aria-hidden="true">
                {selected && <span className="size-1.5 rounded-full bg-primary-foreground" />}</span>
            <span className="flex h-[72px] w-[54px] shrink-0 items-center justify-center overflow-hidden rounded border border-border bg-muted">
                {row.cover ? <img src={row.cover} alt="" className="h-full w-full object-cover" data-testid="pub-imagenote-cover" /> : <ImageOff className="size-4 text-muted-foreground" aria-hidden="true" />}</span>
            <span className="min-w-0 flex-1 space-y-1.5"><span className="block break-words text-sm font-medium" data-testid="pub-imagenote-keyword">{row.title || row.keyword || '未命名图文'}</span>
                <span className="flex flex-wrap items-center gap-2"><Badge variant="outline" className={cn('text-[10px]', BADGE_CLASS[row.bucket])} data-testid="pub-imagenote-badge">{row.statusLabel}</Badge>
                    {row.cardCount !== null && <span className="text-xs text-muted-foreground" data-testid="pub-imagenote-cardcount">{row.cardCount} 张</span>}</span>
                {row.body && <span className="line-clamp-2 whitespace-pre-wrap text-xs text-muted-foreground">{row.body}</span>}
                {when && <span className="block text-xs text-muted-foreground" data-testid="pub-imagenote-when">发布于 {when}</span>}
            </span>
        </button>
        <div className="space-y-1.5 px-2 pb-1 text-xs">
            {row.pendingConfirm && <p className="text-amber-600 dark:text-amber-400" data-testid="pub-imagenote-pending-confirm">发布结果待确认，发布渠道还没有返回最终结果</p>}
            {row.bucket === 'failed' && <p className="break-words text-destructive" data-testid="pub-imagenote-failed-note" data-raw={row.failureReason ? '1' : '0'}>{row.failureReason ? `发布失败 · ${row.failureReason}` : FAILED_REASON_FALLBACK}</p>}
            {row.bucket === 'published' && !row.url && <p className="text-muted-foreground" data-testid="pub-imagenote-nourl">{PUBLISHED_WITHOUT_URL_NOTE}</p>}
            {blockedReason && <p className="text-muted-foreground" data-testid="pub-imagenote-row-blocked">{blockedReason}</p>}
            <div className="flex flex-wrap gap-3">{row.url && <a className="inline-flex min-h-11 items-center gap-1 underline" data-testid="pub-imagenote-url" href={row.url} target="_blank" rel="noopener noreferrer">看已发布的那篇 <ExternalLink className="size-3.5" aria-hidden="true" /></a>}
                <a className="inline-flex min-h-11 items-center underline" data-testid="pub-imagenote-proof" href={row.proofHref}>查看 / 修改</a></div>
        </div>
    </li>;
}

export default ImageNoteList;
