/** Three creation views of the existing image-note chain, not another backend workflow. */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { authFetch } from '@/lib/api';
import { useClientContext } from '@/context/ClientContext';
import { Button } from '@/components/ui/button';
import { type StyleOption } from '@/components/writing/CardStylePicker';
import { DouyinPostDetail } from './DouyinPostDetail';
import { ImageNoteTopicPanel } from './ImageNoteTopicPanel';
import { ImageNoteFlowSteps } from './ImageNoteFlowSteps';
import { positiveId, existingWorkStep } from './imageNoteFlow';
import { userError, userFacingError } from './imageNoteTopics';

interface ExistingPost { id: number; title?: string; keyword?: string; cover_url?: string }

export function ImageNoteDetailRoute() {
    const { postId } = useParams<{ postId: string }>();
    const navigate = useNavigate();
    const { currentBrandId, clients, switchClient } = useClientContext();
    const [siblingIds, setSiblingIds] = useState<number[]>([]);
    const [styles, setStyles] = useState<StyleOption[]>([]);
    const [posts, setPosts] = useState<ExistingPost[]>([]);
    const [page, setPage] = useState(0);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [reloadTick, setReloadTick] = useState(0);
    const [stage, setStage] = useState(1);
    useEffect(() => setStage(postId ? 3 : 1), [postId]);
    const [resolvedTick, setResolvedTick] = useState(0);
    const resolved = useRef<{ post: string; brand: number } | null>(null);
    const initialScopeSynced = useRef('');
    const contextRef = useRef({ currentBrandId, clients, switchClient });
    contextRef.current = { currentBrandId, clients, switchClient };
    const onSiblings = useCallback((ids: number[]) => setSiblingIds(prev =>
        prev.length === ids.length && prev.every((v, i) => v === ids[i]) ? prev : ids), []);
    const onBrandResolved = useCallback((brandId: number) => {
        resolved.current = { post: postId || '', brand: brandId };
        setResolvedTick(n => n + 1);
        const context = contextRef.current;
        if (context.clients.some(c => c.id === brandId)) {
            if (context.currentBrandId === brandId) initialScopeSynced.current = postId || '';
            else context.switchClient(brandId);
        }
    }, [postId]);
    useEffect(() => {
        const r = resolved.current;
        if (!postId || !r || r.post !== postId || initialScopeSynced.current === postId) return;
        if (clients.some(c => c.id === r.brand)) {
            if (currentBrandId === r.brand) initialScopeSynced.current = postId;
            else switchClient(r.brand);
        }
    }, [postId, clients, currentBrandId, switchClient, resolvedTick]);
    const previousBrand = useRef(currentBrandId);
    useEffect(() => {
        if (previousBrand.current === currentBrandId) return;
        previousBrand.current = currentBrandId;
        setPosts([]); setPage(0); setStage(postId ? 3 : 1);
        if (postId && initialScopeSynced.current === postId && resolved.current?.post === postId && resolved.current.brand !== currentBrandId) {
            resolved.current = null;
            navigate('/writing/image-note', { replace: true });
        }
    }, [currentBrandId, postId, navigate]);
    useEffect(() => {
        if (postId || !currentBrandId) { setPosts([]); return; }
        const ac = new AbortController();
        setLoading(true); setError(''); setStyles([]);
        void (async () => {
            try {
                const res = await authFetch(`/api/geo-douyin/posts?brand_id=${currentBrandId}&limit=20&offset=${page * 20}`, { signal: ac.signal });
                /* [WO_282-F4] 先判 ok 再用回包:nginx 502/504 回的是 HTML,裸 .json() 会抛解析器原话 */
                const d = await res.json().catch(() => null);
                if (ac.signal.aborted) return;
                if (!res.ok || d?.status !== 'success') throw userError('已有图文没取到，请重试。');
                setPosts(Array.isArray(d.posts) ? d.posts : []);
            } catch (e) { if (!ac.signal.aborted) setError(userFacingError(e, '已有图文没取到，请重试。')); }
            finally { if (!ac.signal.aborted) setLoading(false); }
        })();
        void (async () => {
            try {
                const res = await authFetch(`/api/geo-douyin/styles?brand_id=${currentBrandId}`, { signal: ac.signal });
                const d = await res.json();
                if (res.ok && !ac.signal.aborted) setStyles(d.styles || []);
            } catch { /* Optional style names do not block the topic list. */ }
        })();
        return () => ac.abort();
    }, [postId, currentBrandId, page, reloadTick]);
    const id = positiveId(postId);
    if (postId && !id) return <div className="space-y-3 p-6" data-testid="image-note-detail-badid">
        <p>这个链接里的作品编号不对，无法打开。</p>
        <Button variant="outline" data-testid="image-note-detail-badid-exit" onClick={() => navigate('/writing/image-note')}>回图文工作台</Button>
    </div>;
    return <div className="mx-auto w-full max-w-[1440px] space-y-5 p-4 sm:p-6" data-testid="image-note-workspace">
        <header className="flex flex-wrap items-end justify-between gap-3">
            <div><h1 className="text-2xl font-semibold tracking-tight">制作 GEO 图文</h1>
                <p className="mt-1.5 text-sm text-muted-foreground">{id ? '检查图片和文案，满意后去发布投放选择抖音账号。' : '选好要讲的内容，让 AI 制作图文；你检查后再发到抖音。'}</p></div>
            <Button variant="outline" onClick={() => navigate(id ? '/writing/image-note' : '/publish?media_type=svideo&content_type=imagenote')}>
                {id ? '回到选题与作品' : '查看图文发布情况'}</Button>
        </header>
        <ImageNoteFlowSteps current={stage} visitable={id ? [1, 2, 3] : undefined} onBack={id ? step => {
            const target = existingWorkStep(step);
            if ('stage' in target) setStage(target.stage); else navigate(target.href);
        } : undefined} />
        {id ? <DouyinPostDetail key={id} postId={id} siblingIds={siblingIds} onSiblings={onSiblings}
            showTopics={false} onBrandResolved={onBrandResolved} workspaceStep={stage} onStageChange={setStage}
            onNavigate={next => navigate(`/writing/image-note/${next}`)}
            onBack={() => navigate('/writing/image-note')} /> : <>
            {!currentBrandId ? <div className="space-y-3 rounded-xl border bg-card p-6" data-testid="image-note-no-work">
                <h2 className="text-lg font-semibold">先选一位客户</h2>
                <p className="text-sm text-muted-foreground">直接使用这个客户已有的关键词和资料，不用重新填写。</p>
                <select aria-label="选择制作图文的客户" value="" onChange={e => switchClient(Number(e.target.value))}
                    className="h-11 w-full max-w-sm rounded-md border border-input bg-background px-3 text-sm">
                    <option value="" disabled>选择客户</option>{clients.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
            </div> : <>
                <div className="rounded-xl border bg-card p-4 sm:p-6">
                    <ImageNoteTopicPanel key={currentBrandId} brandId={currentBrandId} styles={styles} selectedTopicId={null}
                        onStageChange={setStage}
                        onSelectTopic={row => { if (row.postId) navigate(`/writing/image-note/${row.postId}`); }}
                        onChanged={() => setReloadTick(n => n + 1)} />
                </div>
                <section className="space-y-3" aria-label="已有图文作品">
                    <div className="flex items-center justify-between"><h2 className="text-lg font-semibold">已经制作的图文</h2>
                        <Button variant="ghost" size="sm" onClick={() => setReloadTick(n => n + 1)}>刷新作品</Button></div>
                    <p className="text-sm text-muted-foreground">包含以前制作的内容，点开继续修改或发布，不用重新做。</p>
                    {loading && <p className="text-sm text-muted-foreground">正在读取…</p>}
                    {error && <p role="alert" className="text-sm text-destructive" data-testid="image-note-load-failed">{error}</p>}
                    {!loading && !error && posts.length === 0 && <p className="py-4 text-sm text-muted-foreground" data-testid="image-note-no-work-note">这一页还没有作品，可以先从上面选题制作。</p>}
                    <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{posts.map(p => <button key={p.id} type="button"
                        className="flex min-w-0 items-center gap-3 rounded-xl border bg-card p-3 text-left hover:border-primary"
                        onClick={() => navigate(`/writing/image-note/${p.id}`)}>
                        {p.cover_url && <img src={p.cover_url} alt="" className="h-20 w-16 shrink-0 rounded object-cover" />}
                        <span className="min-w-0"><span className="line-clamp-2 text-sm font-medium">{p.title || p.keyword || `图文 #${p.id}`}</span>
                            <span className="mt-2 block text-xs text-muted-foreground">打开内容 →</span></span>
                    </button>)}</div>
                    <div className="flex items-center justify-between"><Button variant="outline" disabled={loading || page === 0} onClick={() => setPage(p => p - 1)}>上一页</Button>
                        <span className="text-xs text-muted-foreground">第 {page + 1} 页</span><Button variant="outline" disabled={loading || posts.length < 20} onClick={() => setPage(p => p + 1)}>下一页</Button></div>
                </section>
            </>}
        </>}
    </div>;
}

export default ImageNoteDetailRoute;
