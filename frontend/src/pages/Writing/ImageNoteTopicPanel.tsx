/**
 * #204 a1 · 图文唯一一屏的**左栏 = 设置**。
 *
 * Owner 09-13 夜:「不是全自动,是半自动,和写作一样:默认把这几个选题写出来,
 * 用户可以修改,修改后就按照标题来进行创作。**用习惯写文章的用户可以完全不用学习**。」
 *
 * ## 逐项对照写文章大厅(工单铁律:不发明新交互)
 *
 * | 这里 | 写文章大厅 |
 * |---|---|
 * | 顶部一句话(买了 / 已做 / 还差) | 项目卡上的那一行 |
 * | 「生成选题 · N 算力」 | 「立即生成标题」(按词计费) |
 * | 三态 chip 待做 / 制作中 / 已完成 | 三 tab 待写 / 写作中 / 已完成 |
 * | 行内改标题(铅笔 → 输入 → 保存) | `startEdit` / `saveEdit`(WritingHall:3966) |
 * | 每行选风格(带样图) | 每行 `TopicStyleSelector` 选文体 |
 * | 勾选 + 「开始制作 (N) · X 算力」 | 勾选 + 「开始写作 (N) · X 算力」 |
 *
 * ## 三条不许破的
 *
 * 🔴 **价只来自服务端**(`production-quote` 的 `total_points`)。本文件一个价格算术都没有。
 * 🔴 **进度只来自服务端**(`distill-tasks/{id}` 的 `stage_label` / `percent`)。
 *    前端按时间涨的假进度条会让"卡住了"和"正在跑"长得一模一样。
 * 🔴 **改完标题要重新读回**,不是只改本地 state —— 本地改看起来成功了,
 *    刷新一下就打回原形,而用户已经按改后的标题去制作了。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Loader2, Pencil, Check, X, Sparkles } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import { STYLE_SAMPLES, CardStylePicker, type StyleOption } from '@/components/writing/CardStylePicker';
import { useClientContext } from '@/context/ClientContext';
import { safeRandomUUID } from '@/lib/safeRandomUUID';
import { productionLines, productionBatch, type ProductionQuote } from './imageNoteProduction';
import {
    keywordRow, topicRow, topicBuckets, distillLabel, startLabel, canStart, distillProgress,
    toggleTopicSelection, currentTopicRead, topicReadPlan, refreshNotice, userError, userFacingError,
    statusText, degradeNotice,
    type TopicRow, type TopicReadMode,
} from './imageNoteTopics';

interface Props {
    brandId: number | null;
    /** 当前选中的选题 —— 中栏预览与右栏编辑跟着它走。 */
    selectedTopicId: number | null;
    onSelectTopic: (row: TopicRow) => void;
    /** 有作品产出 / 状态变化时通知外层(外层重拉详情)。 */
    onChanged?: () => void;
    /*
     * 🔴 风格名的**唯一来源** —— 后端 `/api/geo-douyin/styles`
     *    (`services/geo_douyin/card_templates.py` 的 `STYLE_PRESETS`)。
     *    前端不再写第二份 key→名字 的表:写了就是两个源,后端改名/加款时
     *    两边只会有一边跟上,而且**不会报错**,只会有一处显示旧名字。
     */
    styles: StyleOption[];
    onStageChange?: (step: number) => void;
}

type ChipKey = 'all' | 'pending' | 'making' | 'done' | 'failed';
const CHIPS: Array<{ key: ChipKey; label: string }> = [
    { key: 'all', label: '全部' },
    /* [WO_283-F7] 各档的名字从徽章表取,不在这里另写一份(另写的那份和徽章已经对不上过一次) */
    { key: 'pending', label: statusText('pending') },
    { key: 'making', label: statusText('making') },
    { key: 'done', label: statusText('done') },
    { key: 'failed', label: statusText('failed') },
];

export function ImageNoteTopicPanel({ brandId, selectedTopicId, onSelectTopic,
    onChanged, styles, onStageChange }: Props) {
    const { clientContext } = useClientContext();
    const [sourceMode, setSourceMode] = useState<'keywords' | 'ideas'>('keywords');
    const [reviewing, setReviewing] = useState(false);
    const [cardCount, setCardCount] = useState<number | null>(null);
    const [cardRange, setCardRange] = useState({ min: 1, max: 9 });
    const [styleKey, setStyleKey] = useState('');
    const [aspectRatio, setAspectRatio] = useState('');
    const [ratios, setRatios] = useState<Array<{ key: string; label: string }>>([]);
    const [quote, setQuote] = useState<ProductionQuote | null>(null);
    const requestRef = useRef<{ signature: string; id: string } | null>(null);
    const [topicPage, setTopicPage] = useState(0);
    const [topicTotal, setTopicTotal] = useState(0);
    const [manualOpen, setManualOpen] = useState(false);
    const [manualTitle, setManualTitle] = useState('');
    const [manualKeyword, setManualKeyword] = useState('');
    const [manualConfirmedId, setManualConfirmedId] = useState<number | null>(null);
    const [adding, setAdding] = useState(false);
    const [recentTopics, setRecentTopics] = useState<number[]>([]);
    const settings = useMemo(() => ({ cardCount: cardCount || 0, styleKey, aspectRatio,
        industry: clientContext?.brand.id === brandId ? clientContext.brand.industry || '' : '' }),
        [cardCount, styleKey, aspectRatio, clientContext, brandId]);
    const [rows, setRows] = useState<TopicRow[]>([]);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState('');
    const [chip, setChip] = useState<ChipKey>('all');
    const [selected, setSelected] = useState<Set<number>>(new Set());
    /* 轮询连续失败次数:单次失败不终止是对的,**一直失败还不吭声**就不对了。 */
    const [pollMisses, setPollMisses] = useState(0);
    /* [WO_283-F2] 最近一批选题是降级做出来的 ⇒ 常驻一句人话(不是一闪就没的 toast) */
    const [degradedNote, setDegradedNote] = useState('');
    /* 后台心跳连着没读到的次数(读到一次就清零);≥3 次由 refreshNotice 出声,列表不动 */
    const [refreshMisses, setRefreshMisses] = useState(0);
    /* 风格 key → 显示名。只查后端给的那份;查不到就返回空,由调用处说「系统推荐」。 */
    const styleNameOf = (key: string) => (styles || []).find((o) => o.key === key)?.label || '';

    /* Prices are server-owned. Article quote capacity is not an image-note-only debt. */
    const [distillPoints, setDistillPoints] = useState<number | null>(null);
    const [quotePoints, setQuotePoints] = useState<number | null>(null);

    /* 蒸馏任务 */
    const [taskId, setTaskId] = useState<number | null>(null);
    const [progress, setProgress] = useState<ReturnType<typeof distillProgress> | null>(null);
    const [starting, setStarting] = useState(false);

    /* 行内改标题 */
    const [editingId, setEditingId] = useState<number | null>(null);
    const [editingTitle, setEditingTitle] = useState('');
    const [savingTitle, setSavingTitle] = useState(false);
    const listScope = useRef('');
    listScope.current = `${brandId}:${sourceMode}:${topicPage}`;
    const loadSequence = useRef(0);

    const buckets = useMemo(() => topicBuckets(rows), [rows]);
    const recentlyDone = rows.filter(r => recentTopics.includes(r.id) && r.status === 'done' && r.postId);
    useEffect(() => {
        if (chip === 'making' && recentTopics.length > 0 && buckets.making.length === 0 && recentlyDone.length > 0) setChip('done');
    }, [chip, recentTopics.length, buckets.making.length, recentlyDone.length]);

    /* ── 取选题列表 ─────────────────────────────────────────────── */
    /*
     * 🔴 `mode`:首读 / 换范围 / 用户重试走 `read`(清屏出「正在读取…」);
     *    制作中的 4 秒心跳走 `refresh`(**不碰 loading、不清行、不报错**)。
     *    两条路怎么碰屏幕由纯函数 `topicReadPlan` 说了算,这里只照办。
     *    Owner 2026-09-22 看到的「一直闪」就是心跳走了 `read` 这条路。
     */
    const load = useCallback(async (bid: number, signal?: AbortSignal, mode: TopicReadMode = 'read') => {
        const plan = topicReadPlan(mode);
        const scope = `${bid}:${sourceMode}:${topicPage}`;
        const sequence = ++loadSequence.current;
        const current = () => currentTopicRead(scope, listScope.current, sequence, loadSequence.current, signal?.aborted);
        if (plan.showLoading) setLoading(true);
        if (!plan.silentErrors) setError('');
        try {
            const path = sourceMode === 'keywords'
                ? `/api/geo-douyin/clients/${bid}/keywords?with_production=true&limit=200&offset=${topicPage * 200}`
                : `/api/geo-douyin/clients/${bid}/topics?limit=200&offset=${topicPage * 200}`;
            const res = await authFetch(path, { signal });
            const d = await res.json().catch(() => null);
            if (!current()) return;
            if (!res.ok || d?.status !== 'success') {
                /* 心跳读不到:行照旧、记一次 miss(≥3 次由 refreshNotice 出声),下一跳再问。 */
                if (plan.silentErrors) { setRefreshMisses((n) => n + 1); return; }
                /* 🔴 服务端原话优先(总闸关时它会说清是为什么);没有原话才退兜底。 */
                const detail = d && typeof d.detail === 'object' ? d.detail : d;
                setError(String((detail && detail.message) || '内容和制作进度没取到，请重试。'));
                setRows([]);
                return;
            }
            const raw = sourceMode === 'keywords' ? d.keywords : d.topics;
            if (!Array.isArray(raw)) throw new Error('列表格式不完整');
            setRows(raw.map(sourceMode === 'keywords' ? keywordRow : topicRow));
            setTopicTotal(Number(d?.total) || 0);
            setRefreshMisses(0);
        } catch (e) {
            if (!current()) return;
            if (plan.silentErrors) { setRefreshMisses((n) => n + 1); return; }
            setError('内容和制作进度没取到，请重试。');
            setRows([]);
        } finally {
            if (plan.showLoading && current()) setLoading(false);
        }
    }, [topicPage, sourceMode]);

    useEffect(() => {
        setTopicPage(0); setChip('all'); setReviewing(false); setEditingId(null);
        setRecentTopics([]); onStageChange?.(1);
    }, [brandId, sourceMode]);

    useEffect(() => {
        if (!brandId) { setRows([]); setSelected(new Set()); return; }
        const ac = new AbortController();
        setSelected(new Set());
        setRows([]);
        void load(brandId, ac.signal);
        return () => ac.abort();
    }, [brandId, load]);

    /* [WO_283-F2] 进来就把「上一批是不是降级做的」读回来(只读、不收费);读不到就不说,不猜 */
    useEffect(() => {
        setDegradedNote('');
        if (!brandId) return;
        const ac = new AbortController();
        void (async () => {
            try {
                const res = await authFetch(`/api/geo-douyin/clients/${brandId}/latest-topics`, { signal: ac.signal });
                const d = await res.json().catch(() => null);
                if (!ac.signal.aborted && res.ok && d) setDegradedNote(degradeNotice(d.fewshot_degraded));
            } catch { /* 读不到就不说 */ }
        })();
        return () => ac.abort();
    }, [brandId]);

    /* ── 两处价:各来各的服务端口 ───────────────────────────────── */
    useEffect(() => {
        const ac = new AbortController();
        void (async () => {
            try {
                const res = await authFetch('/api/geo-douyin/pricing', { signal: ac.signal });
                if (!res.ok) return;
                const d = await res.json();
                if (ac.signal.aborted) return;
                setCardCount(Number(d.card_default) || null);
                setCardRange({ min: Number(d.card_min) || 1, max: Number(d.card_max) || 9 });
                setRatios(Array.isArray(d.aspect_ratios) ? d.aspect_ratios : []);
                setAspectRatio(d.aspect_ratio_default || '');
                /*
                 * 蒸馏那一档的实价。🔴 键名是 **`topic_distill`** ——
                 * `api/geo_douyin_api.py` 的 `/pricing` 回包里就叫这个
                 * (`"topic_distill": distill`)。
                 * 我第一版按自己的印象写成 `distill` / `distill_topics`,两个都不对,
                 * 于是价**永远读不到**、按钮**永远禁用** —— 而界面上看起来只是
                 * 「价目读不到」,像是服务端的问题。
                 * 🔴 这个 bug 是靠**先去读后端回包**发现的:
                 *    如果我按自己前端的猜法去造夹具,夹具会把它完整盖住。
                 */
                const p = Number(d?.topic_distill?.cost_points);
                if (d?.topic_distill?.cost_points !== null && Number.isFinite(p) && p >= 0) setDistillPoints(Math.floor(p));
            } catch { /* 读不到就不显示数字,不猜 */ }
        })();
        return () => ac.abort();
    }, []);

    const selectedPending = useMemo(
        () => buckets.pending.filter((r) => r.selectable && selected.has(r.id)), [buckets.pending, selected]);

    useEffect(() => {
        setQuotePoints(null); setQuote(null);
        if (!brandId || selectedPending.length === 0 || !cardCount) return;
        const ac = new AbortController();
        void (async () => {
            try {
                const res = await authFetch('/api/geo-douyin/production-quote', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ lines: productionLines(selectedPending, settings) }),
                    signal: ac.signal,
                });
                if (!res.ok) { setQuotePoints(null); return; }
                const d = await res.json();
                if (ac.signal.aborted) return;
                const p = Number(d?.total_points);
                if (!Array.isArray(d?.lines) || d.lines.length !== selectedPending.length) return;
                setQuote(d);
                setQuotePoints(Number.isFinite(p) && p >= 0 ? Math.floor(p) : null);
            } catch { if (!ac.signal.aborted) setQuotePoints(null); }
        })();
        return () => ac.abort();
    }, [brandId, selectedPending, cardCount, settings]);

    /* ── 制作中的后台心跳:每 4 秒**静默**重读(不闪列表) ──────────── */
    useEffect(() => {
        if (!brandId || !buckets.making.length) { setRefreshMisses(0); return; }
        const ac = new AbortController();
        const timer = window.setInterval(() => { void load(brandId, ac.signal, 'refresh'); }, 4000);
        return () => { window.clearInterval(timer); ac.abort(); };
    }, [brandId, buckets.making.length, load]);

    /* ── 生成选题:提交 → 轮进度 → 成功后重拉列表 ─────────────── */
    const pollRef = useRef<number | null>(null);
    const clearPoll = () => { if (pollRef.current) { window.clearInterval(pollRef.current); pollRef.current = null; } };
    useEffect(() => () => clearPoll(), []);

    useEffect(() => {
        if (taskId === null) return;
        clearPoll();
        const ac = new AbortController();
        let inFlight = false;
        const tick = async () => {
            if (inFlight || ac.signal.aborted) return;
            inFlight = true;
            try {
                const res = await authFetch(`/api/geo-douyin/distill-tasks/${taskId}`, { signal: ac.signal });
                const d = await res.json().catch(() => null);
                if (ac.signal.aborted) return;
                if (!res.ok || !d) throw new Error('进度暂时未返回');
                const p = distillProgress(d);
                setProgress(p);
                setPollMisses(0);
                if (!p.keepPolling) {
                    clearPoll();
                    setTaskId(null);
                    /* 这一批做成了 ⇒ 它是不是降级做的,以这一批为准(没做成的话上一批的那句照旧) */
                    if (d.state === 'succeeded') setDegradedNote(p.degraded);
                    if (brandId) void load(brandId);
                    onChanged?.();
                }
            } catch {
                if (ac.signal.aborted) return;
                /* 🔴 单次失败不终止(下一 tick 再问)—— 但**连着失败**要出声:
                   不出声的话进度条就一直停在原地,和"卡死了"长得一模一样,
                   而用户刚为这次蒸馏付过钱。 */
                setPollMisses((n) => n + 1);
            } finally { inFlight = false; }
        };
        void tick();
        pollRef.current = window.setInterval(() => { void tick(); }, 3000);
        return () => { ac.abort(); clearPoll(); };
        // onChanged 放依赖会因父层每次渲染换引用而反复重建定时器
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [taskId, brandId, load]);

    const startDistill = async () => {
        if (!brandId || starting || taskId !== null) return;
        setStarting(true);
        setProgress(null);
        try {
            const res = await authFetch('/api/geo-douyin/distill-topics', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                /* [WO_282-F1] 行业与下单体同源(客户档案的行业原文);后端在入口归一(WO_282-C) */
                body: JSON.stringify({ brand_id: brandId, industry_key: settings.industry }),
            });
            const d = await res.json().catch(() => null);
            if (!res.ok) {
                const detail = d && typeof d.detail === 'object' ? d.detail : d;
                setError(String((detail && detail.message) || '这次没生成出来，可以再试一次'));
                return;
            }
            const id = Number(d?.task_id);
            if (Number.isFinite(id) && id > 0) setTaskId(id);
        } catch { setError('这次提交没有收到结果，请刷新选题后再试。'); } finally {
            setStarting(false);
        }
    };

    /* ── 行内改标题:存完**重新读回** ───────────────────────────── */
    const saveTitle = async () => {
        if (editingId === null || savingTitle) return;
        setSavingTitle(true);
        try {
            const res = await authFetch(`/api/geo-douyin/topics/${editingId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: editingTitle }),
            });
            if (!res.ok) {
                const d = await res.json().catch(() => null);
                const detail = d && typeof d.detail === 'object' ? d.detail : d;
                setError(String((detail && detail.message) || '这个标题没存上'));
                return;
            }
            /*
             * 🔴 **重新读回**,不是 `setRows(prev => …改本地…)`。
             *    只改本地 state 时屏幕上看起来成功了,刷新一下打回原形 ——
             *    而用户已经按改后的标题去制作了。判据 A3 的毒打的就是这个。
             */
            if (brandId) await load(brandId);
            setEditingId(null);
        } catch { setError('标题没保存上，输入已保留，请重试。');
        } finally {
            setSavingTitle(false);
        }
    };

    /* ── 开始制作 ──────────────────────────────────────────────── */
    const [making, setMaking] = useState(false);
    const doStart = async () => {
        if (!brandId || !quote || !canStart({ selectedCount: selectedPending.length, points: quotePoints, busy: making })) return;
        setMaking(true);
        try {
            const signature = JSON.stringify({ brandId, rows: selectedPending, settings, quote });
            if (requestRef.current?.signature !== signature) requestRef.current = { signature, id: safeRandomUUID() };
            const res = await authFetch('/api/geo-douyin/posts/batch', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(productionBatch(brandId, selectedPending, settings, quote, requestRef.current.id)),
            });
            const d = await res.json().catch(() => null);
            if (!res.ok) {
                const detail = d && typeof d.detail === 'object' ? d.detail : d;
                setError(String((detail && detail.message) || '这次没开始制作'));
                return;
            }
            const failures = (d?.items || []).filter((item: { post_id?: number }) => !item.post_id);
            if (!Array.isArray(d?.items) || d.items.length === 0) {
                setError('这批请求已收到，结果尚未返回。请保留当前清单，稍后重试会读取同一批结果。');
                return;
            }
            setRecentTopics(selectedPending.map(r => r.id));
            setSelected(new Set());
            setReviewing(false); onStageChange?.(2); setChip('making');
            if (brandId) await load(brandId);
            if (failures.length) setError(failures.map((item: { message?: string }) => item.message || '有一条未提交，请重新确认').join('；'));
            onChanged?.();
        } catch (e) { setError(userFacingError(e, '提交结果暂未收到，可用同一清单重试，不会重新建单。'));
        } finally {
            setMaking(false);
        }
    };

    const shown = chip === 'all' ? rows.filter(r => r.status !== 'archived') : buckets[chip] as TopicRow[];

    if (!brandId) {
        return (
            <div className="p-4 text-xs text-muted-foreground" data-testid="topics-no-client">
                先在左上角选一个客户,再来做图文。
            </div>
        );
    }

    return (
        <div className="space-y-3" data-testid="image-note-topic-panel">
            <div className="flex flex-wrap items-center justify-between gap-3"><div>
                <h2 className="text-lg font-semibold">{reviewing ? '确认这次要制作的图文' : '选一个关键词，开始做图文'}</h2>
                <p className="mt-1 text-sm text-muted-foreground">{reviewing ? '先看风格、张数和费用，确认后才开始制作。' : '已买或已确认的关键词都在这里。勾选还没做的词，AI 会围绕它写标题、正文并配图。'}</p>
            </div><Button variant="outline" onClick={() => { setManualConfirmedId(null); setSourceMode('ideas'); setManualOpen(v => !v); setReviewing(false); onStageChange?.(1); }}>自己写一个选题</Button></div>
            {!reviewing && <div className="flex flex-wrap gap-2" aria-label="内容来源">
                <Button variant={sourceMode === 'keywords' ? 'default' : 'outline'} onClick={() => { setSourceMode('keywords'); setManualOpen(false); }}>客户关键词</Button>
                <Button variant={sourceMode === 'ideas' ? 'default' : 'ghost'} onClick={() => setSourceMode('ideas')}>自选题目 / AI 灵感</Button>
            </div>}
            {manualOpen && <form className="space-y-3 rounded-lg border bg-muted/30 p-4" onSubmit={async e => {
                e.preventDefault(); if (!brandId || adding) return; setAdding(true); setError('');
                try {
                    const res = await authFetch(`/api/geo-douyin/clients/${brandId}/topics`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ title: manualTitle, keyword: manualKeyword,
                            ...(manualConfirmedId ? { confirmed_keyword_id: manualConfirmedId } : {}) }) });
                    if (!res.ok) throw userError('选题没存上，请检查标题和关键词后重试。');
                    setManualOpen(false); setManualTitle(''); setManualKeyword(''); setChip('pending'); await load(brandId);
                } catch (err) { setError(userFacingError(err, '选题没存上，请重试。')); } finally { setAdding(false); }
            }}>
                <label className="block text-sm">关联关键词<Input required maxLength={60} readOnly={Boolean(manualConfirmedId)} value={manualKeyword} onChange={e => setManualKeyword(e.target.value)} placeholder="例如：揭阳商务出差住哪里" /></label>
                {manualConfirmedId && <p className="text-xs text-muted-foreground">沿用刚才的客户关键词。下面写一个新角度，原来的作品保留；保存选题不收费。</p>}
                <label className="block text-sm">图文标题<Input required maxLength={120} value={manualTitle} onChange={e => setManualTitle(e.target.value)} placeholder="这条图文要讲清楚什么" /></label>
                <Button type="submit" disabled={adding}>{adding ? '正在保存…' : '保存选题'}</Button>
            </form>}
            {reviewing && <div className="space-y-4 rounded-xl border bg-muted/20 p-4" data-testid="topics-production-review">
                <ol className="list-inside list-decimal space-y-2 text-sm">{selectedPending.map(r => <li key={r.id}>{r.fromKeyword && !r.topicId ? `围绕「${r.keyword}」制作` : r.title}</li>)}</ol>
                <label className="block text-sm">每条图片张数<select className="ml-3 h-10 rounded-md border bg-background px-3" value={cardCount || ''} onChange={e => setCardCount(Number(e.target.value))}>
                    {Array.from({ length: cardRange.max - cardRange.min + 1 }, (_, i) => i + cardRange.min).map(n => <option key={n} value={n}>{n} 张</option>)}</select></label>
                <label className="block text-sm">图片比例<select className="ml-3 h-10 rounded-md border bg-background px-3" value={aspectRatio} onChange={e => setAspectRatio(e.target.value)}>
                    {ratios.map(r => <option key={r.key} value={r.key}>{r.label}</option>)}</select></label>
                {styles.length > 0 && <CardStylePicker styles={styles} value={styleKey} onChange={setStyleKey} columns={4} />}
                <Button variant="outline" onClick={() => { setReviewing(false); onStageChange?.(1); }}>返回修改选题</Button>
            </div>}
            {recentlyDone.length > 0 && <div className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-emerald-500/30 bg-emerald-500/5 p-3" role="status">
                <p className="text-sm">本次已有 {recentlyDone.length} 条图文做好了，接下来检查图片和文案。</p>
                <Button size="sm" onClick={() => onSelectTopic(recentlyDone[0])}>检查已完成图文</Button>
            </div>}
            {/* ── 生成选题 ── */}
            {sourceMode === 'ideas' && <details className={cn('space-y-2 rounded-lg border p-3', reviewing && 'hidden')}>
                <summary className="cursor-pointer text-sm">让 AI 帮我想新选题（可选）</summary>
                <p className="text-xs text-muted-foreground">已有关键词可以直接制作；只有想拓展新角度时，才需要这一步。</p>
                <Button
                    type="button" size="sm" className="w-full sm:w-auto sm:min-w-64"
                    data-testid="topics-distill"
                    disabled={starting || taskId !== null || distillPoints === null}
                    onClick={() => void startDistill()}
                >
                    {starting || taskId !== null
                        ? <Loader2 className="mr-2 size-3.5 animate-spin" />
                        : <Sparkles className="mr-2 size-3.5" />}
                    {distillLabel(distillPoints)}
                </Button>
                {distillPoints === null && (
                    <p className="text-[11px] text-muted-foreground" data-testid="topics-distill-noprice">
                        价目暂时读不到,先不能下单 —— 刷新一下页面再试。
                    </p>
                )}
                {/* 🔴 进度条与那句话**都来自服务端**(stage_label / percent),前端不编 */}
                {progress && (
                    <div className="space-y-1" data-testid="topics-distill-progress">
                        <div className="h-1 w-full overflow-hidden rounded bg-muted">
                            <div className="h-full bg-primary transition-all"
                                style={{ width: `${progress.percent}%` }}
                                data-testid="topics-distill-bar"
                                data-percent={progress.percent} />
                        </div>
                        <p className="text-[11px] text-muted-foreground">
                            {progress.line}
                            {/* [WO_283-F6] 服务端估的剩余时间:最长那一段里条不动,只有它在倒数 */}
                            {progress.eta && <span className="ml-2 tabular-nums" data-testid="topics-distill-eta">{progress.eta}</span>}
                        </p>
                        {/* 🔴 连着问不到就出声。进度条停在原地和"卡死了"长得一模一样,
                            而用户刚为这次蒸馏付过钱 —— 沉默是最坏的那一种。 */}
                        {pollMisses >= 3 && (
                            <p className="text-[11px] text-amber-600" data-testid="topics-distill-stalled">
                                进度连着 {pollMisses} 次没问到。任务多半还在跑,页面留着别关;
                                一直这样就找我们看一眼。
                            </p>
                        )}
                    </div>
                )}
            </details>}
            {/* [WO_283-F2] 降级要如实说:悄悄用别的行业 / 视频样本去教模型写图文,产出走形时没人知道为什么 */}
            {sourceMode === 'ideas' && !reviewing && degradedNote && (
                <p role="status" data-testid="topics-distill-degraded"
                    className="rounded-md border border-amber-500/30 bg-amber-500/5 p-2 text-xs text-amber-700">
                    {degradedNote}
                </p>
            )}

            {error && (
                <div className="space-y-2 rounded-lg border border-destructive/30 p-3" role="alert" data-testid="topics-error">
                    <p className="text-sm text-destructive">{error}</p>
                    <Button variant="outline" size="sm" onClick={() => void load(brandId)}>重新读取</Button>
                    <Button variant="ghost" size="sm" onClick={() => { setSourceMode('ideas'); setManualOpen(true); }}>先自己写一个选题</Button>
                </div>
            )}

            {/* ── 三态 chip(计数常驻,0 也显示)── */}
            <div className={cn('flex items-center gap-1 overflow-x-auto', reviewing && 'hidden')} data-testid="topics-chips">
                {CHIPS.map((c) => (
                    <button key={c.key} type="button"
                        data-testid="topics-chip" data-chip={c.key}
                        data-chip-count={c.key === 'all' ? rows.length : buckets.counts[c.key]}
                        onClick={() => setChip(c.key)}
                        className={cn('shrink-0 rounded px-2 py-0.5 text-[11px] transition-colors',
                            'min-h-11 text-xs', chip === c.key ? 'bg-primary text-primary-foreground'
                                : 'text-muted-foreground hover:text-foreground')}>
                        {c.label} {c.key === 'all' ? rows.length : buckets.counts[c.key]}
                    </button>
                ))}
            </div>

            {/* 心跳连着没读到:出声但**不换掉列表**(下面还是上次读到的) */}
            {refreshNotice(refreshMisses) && (
                <p className={cn('text-[11px] text-amber-600', reviewing && 'hidden')} data-testid="topics-refresh-stalled">
                    {refreshNotice(refreshMisses)}
                </p>
            )}

            {/* ── 选题列表 ── */}
            <div className={cn('grid gap-3 md:grid-cols-2 xl:grid-cols-3', reviewing && 'hidden')} data-testid="topics-list">
                {loading && (
                    <p className="py-4 text-center text-xs text-muted-foreground" data-testid="topics-loading">
                        <Loader2 className="mr-1 inline size-3 animate-spin" />正在读取内容和制作进度…
                    </p>
                )}
                {!loading && !error && shown.length === 0 && (
                    <div className="space-y-3 rounded-lg border border-dashed p-5 md:col-span-2 xl:col-span-3" data-testid="topics-empty">
                        <p className="text-sm text-muted-foreground">{rows.length > 0 ? '这一档暂时没有内容，可查看全部关键词。'
                            : sourceMode === 'keywords' ? '这个客户还没有已购或已确认的关键词。你可以先确认报价里的词，也可以直接自己写一个选题。'
                            : '还没有自选题目。可以自己写一个，或回到客户关键词直接制作。'}</p>
                        <div className="flex flex-wrap gap-2"><Button variant="outline" onClick={() => { setSourceMode('ideas'); setManualOpen(true); }}>自己写一个选题</Button>
                            {rows.length > 0 ? <Button variant="ghost" onClick={() => setChip('all')}>查看全部</Button>
                                : <a className="inline-flex min-h-11 items-center px-3 text-sm underline" href="/pricing">去报价里确认关键词</a>}</div>
                    </div>
                )}
                {!loading && !error && shown.map((r) => {
                    const isEditing = editingId === r.id;
                    const isActive = selected.has(r.id) || selectedTopicId === r.id;
                    const activate = () => {
                        if (r.selectable) setSelected(prev => toggleTopicSelection(prev, r.id));
                        else if (r.postId) onSelectTopic(r);
                    };
                    return (
                        <div key={r.id}
                            data-testid="topic-row" data-topic-id={r.id} data-topic-status={r.status}
                            data-topic-active={isActive ? 'true' : 'false'}
                            role={r.selectable ? 'checkbox' : r.postId ? 'button' : undefined}
                            aria-checked={r.selectable ? selected.has(r.id) : undefined}
                            tabIndex={r.selectable || r.postId ? 0 : undefined}
                            onClick={activate}
                            onKeyDown={e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); activate(); } }}
                            className={cn('rounded-xl border p-4 transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-primary',
                                (r.selectable || r.postId) && 'cursor-pointer',
                                isActive ? 'border-primary bg-primary/10' : 'border-border hover:border-primary/60 hover:bg-muted/30')}>
                            <div className="flex items-start gap-2">
                                {r.selectable && (
                                    <Checkbox
                                        aria-hidden="true" tabIndex={-1}
                                        className="pointer-events-none mt-0.5 size-4 shrink-0"
                                        data-testid="topic-check"
                                        checked={selected.has(r.id)}
                                    />
                                )}
                                <div className="min-w-0 flex-1 space-y-1">
                                    {isEditing ? (
                                        <div className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                                            <Input value={editingTitle} autoFocus
                                                data-testid="topic-title-input"
                                                className="h-7 text-xs"
                                                onChange={(e) => setEditingTitle(e.target.value)} />
                                            <Button type="button" size="icon" variant="ghost" className="size-7"
                                                data-testid="topic-title-save"
                                                disabled={savingTitle}
                                                onClick={() => void saveTitle()}>
                                                {savingTitle ? <Loader2 className="size-3.5 animate-spin" />
                                                    : <Check className="size-3.5" />}
                                            </Button>
                                            <Button type="button" size="icon" variant="ghost" className="size-7"
                                                data-testid="topic-title-cancel"
                                                onClick={() => setEditingId(null)}>
                                                <X className="size-3.5" />
                                            </Button>
                                        </div>
                                    ) : (
                                        <div className="flex items-start gap-1">
                                            <span className="min-w-0 flex-1 text-sm font-medium leading-relaxed"
                                                data-testid="topic-title">{r.fromKeyword ? r.keyword : r.title || '(还没有标题)'}</span>
                                            {r.editable && (
                                                <button type="button"
                                                    data-testid="topic-title-edit"
                                                    aria-label="改这个标题"
                                                    className="shrink-0 text-muted-foreground hover:text-foreground"
                                                    onClick={(e) => {
                                                        e.stopPropagation();
                                                        setEditingId(r.id);
                                                        setEditingTitle(r.title);
                                                    }}>
                                                    <Pencil className="size-3" />
                                                </button>
                                            )}
                                        </div>
                                    )}
                                    <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                                        {r.keyword && <span data-testid="topic-keyword">{r.fromKeyword ? `${r.originLabel} · 方案 #${r.quoteId}` : r.keyword}</span>}
                                        {r.city && <span data-testid="topic-city">· {r.city}</span>}
                                        <span data-testid="topic-status">· {r.statusLabel}</span>
                                    </div>
                                    {r.fromKeyword && <p className="text-xs text-muted-foreground">{r.producedCount ? `已制作 ${r.producedCount} 条图文` : r.status === 'pending' ? '围绕这个词制作；标题由 AI 生成，做好后可以改。' : ''}</p>}
                                    {r.postId && <Button size="sm" className="min-h-11" variant="outline" onClick={e => { e.stopPropagation(); onSelectTopic(r); }}>{r.status === 'making' ? '查看制作进度' : r.status === 'failed' ? '查看详情并重试' : '打开图文，继续检查'}</Button>}
                                    {r.fromKeyword && (r.status === 'done' || (r.status === 'pending' && !r.selectable)) && <Button variant="ghost" size="sm" className="min-h-11" onClick={e => {
                                        e.stopPropagation(); setManualKeyword(r.keyword); setManualConfirmedId(r.confirmedKeywordId || null);
                                        setManualTitle(''); setSourceMode('ideas'); setManualOpen(true);
                                    }}>{r.status === 'done' ? '再写一个角度' : '自己为这个词加选题'}</Button>}
                                    {/* 🔴 风格要**样图 + 名字**两样都给(#188 区 C 的样图库拆到行内)。
                                        只给名字 = 第 11 条要清的"有形态的选项只给名字";
                                        只给一个 28px 的色块同样不顶用 —— 那个尺寸分不出
                                        「设计文字卡」和「备忘录体」,而"矩形非零"这种判据会被它满足,
                                        用户却什么也没看出来。名字取自后端那份 `styles`,不在这里另写一表。 */}
                                    {!r.fromKeyword && <div className="flex items-center gap-2">
                                        {STYLE_SAMPLES[r.styleKey] ? (
                                            <img src={STYLE_SAMPLES[r.styleKey]} alt=""
                                                data-testid="topic-style-sample"
                                                className="block h-16 w-12 shrink-0 rounded border border-border object-cover" />
                                        ) : (
                                            <span className="flex h-16 w-12 shrink-0 items-center justify-center rounded border border-dashed border-border text-[10px] text-muted-foreground"
                                                data-testid="topic-style-none">推荐</span>
                                        )}
                                        <span className="text-[11px] leading-tight text-muted-foreground"
                                            data-testid="topic-style-name">
                                            {/* 🔴 有 styleKey 却查不到名字(styles 还没回来 / 取失败)
                                                时**不能**说「系统推荐」—— 这一条是有风格的,
                                                只是名字没拿到。说成系统推荐就是把"不知道"
                                                讲成了一个具体的、错的事实。 */}
                                            {styleNameOf(r.styleKey)
                                                || (r.styleKey ? '这一条的卡面风格' : '系统推荐的卡面风格')}
                                        </span>
                                    </div>}
                                    {r.status === 'failed' && r.failure && (
                                        <p className="text-[10px] text-destructive" data-testid="topic-failure">
                                            {r.failure}
                                        </p>
                                    )}
                                </div>
                            </div>
                        </div>
                    );
                })}
            </div>
            {!reviewing && <div className="flex items-center justify-between gap-3 text-xs text-muted-foreground">
                <span>本页 {rows.length} 条 / 共 {topicTotal} 条{sourceMode === 'keywords' ? '关键词' : '选题'}；每次最多制作 20 条</span>
                {topicTotal > 200 && <div className="flex gap-2"><Button variant="outline" disabled={loading || topicPage === 0} onClick={() => setTopicPage(n => n - 1)}>上一页</Button>
                    <Button variant="outline" disabled={loading || (topicPage + 1) * 200 >= topicTotal} onClick={() => setTopicPage(n => n + 1)}>下一页</Button></div>}
            </div>}

            {/* ── 开始制作 ── */}
            {((['all', 'pending'].includes(chip) && rows.some(r => r.selectable)) || reviewing) && <div className="space-y-1 border-t border-border pt-2">
                <Button type="button" size="sm" className="min-h-11 w-full sm:w-auto sm:min-w-64"
                    data-testid="topics-start"
                    disabled={!canStart({ selectedCount: selectedPending.length, points: quotePoints, busy: making })}
                    onClick={() => { if (reviewing) void doStart(); else { setReviewing(true); onStageChange?.(2); } }}>
                    {making && <Loader2 className="mr-2 size-3.5 animate-spin" />}
                    {reviewing ? startLabel(selectedPending.length, quotePoints) : `下一步：制作图文${selectedPending.length ? ` (${selectedPending.length})` : ''}`}
                </Button>
                {selectedPending.length > 0 && quotePoints === null && (
                    <p className="text-[11px] text-muted-foreground" data-testid="topics-start-noprice">
                        价目暂时读不到,先不能下单 —— 这不是"免费",是还没问到价。
                    </p>
                )}
                {selectedPending.length === 0 && (
                    <p className="text-[11px] text-muted-foreground" data-testid="topics-start-hint">
                        勾选还没做的关键词或选题，下一步确认风格、张数和费用。
                    </p>
                )}
            </div>}
        </div>
    );
}

export default ImageNoteTopicPanel;
