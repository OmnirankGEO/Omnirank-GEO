import { useCallback, useEffect, useRef, useState } from 'react';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { lazyToast } from '@/lib/lazyToast';
import {
    nextPollDelayMs, nextUnchangedStreak, shouldShowLoading,
} from '../monitoringPollSchedule';
import { AlertCircle, Check, ChevronDown, ChevronUp, RefreshCw, X } from 'lucide-react';
import ReactMarkdown from '@/components/SafeMarkdown';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

// [工单 2026-08-03 ①] 完整回答的三段切分:锚点之前 / 锚点命中 / 锚点之后。
// anchor 语义由后端 build_identity_answer_anchor 给出:
//   evidence_window = 证据窗口在全文中的真实偏移(窗口落库后才会出现)
//   seen_prefix     = 卡片里已经看过的 500 字到此为止,后面是之前看不到的内容
//   none            = 定位不到 → 照常给全文,只是不高亮(不阻断)
type AnswerAnchor = 'evidence_window' | 'seen_prefix' | 'none';

interface FullAnswer {
    text: string;
    anchor: AnswerAnchor;
    start: number | null;
    end: number | null;
}

interface IdentityReviewItem {
    id: number;
    keyword: string;
    platform: string;
    response_snippet: string;
    identity_candidates: string[];
    identity_evidence_snippet?: string | null;
    identity_evidence_hash: string;
    identity_decision_version: number;
    tested_at: string;
}

function requestId(): string {
    return safeRandomUUID();
}

/**
 * [客户反馈⑤ 机制A 2026-08-09] 两次轮询结果是否等价。
 *
 * 🔴 刻意**不用** `JSON.stringify(a) === JSON.stringify(b)`:那对键序敏感,
 *   后端换一次序列化顺序就恒判"变了",判据当场退化成恒真(= 什么都没修)。
 * 🔴 也**不做**深比较:这一行的身份由 `id` + `identity_evidence_hash` +
 *   `identity_decision_version` 三者唯一确定 —— 证据变了 hash 就变,
 *   决策变了 version 就变。用它们做键是**语义等价**,不是抽样。
 */
export function identityItemsEqual(a: IdentityReviewItem[], b: IdentityReviewItem[]): boolean {
    if (a === b) return true;
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
        if (a[i].id !== b[i].id) return false;
        if (a[i].identity_evidence_hash !== b[i].identity_evidence_hash) return false;
        if (a[i].identity_decision_version !== b[i].identity_decision_version) return false;
    }
    return true;
}

// [工单 M-1 ③ 2026-07-28] 从回答片段里取候选称呼的上下文窗口(±40 字)供高亮定位:
// 用户必须看到"原文里这个称呼前后说了什么"才能判断是不是自己的品牌。
function candidateContext(text: string, candidate: string): { before: string; hit: string; after: string } | null {
    const source = String(text || '');
    const index = source.indexOf(candidate);
    if (index < 0) return null;
    return {
        before: source.slice(Math.max(0, index - 40), index),
        hit: source.slice(index, index + candidate.length),
        after: source.slice(index + candidate.length, index + candidate.length + 40),
    };
}

export function IdentityReviewPanel({
    brandId,
    readOnly = false,
    taskActive = false,
    onDecided,
}: {
    brandId: number;
    readOnly?: boolean;
    /**
     * [#182] 本页此刻有没有监测任务在跑。
     * 🔴 它是"还该不该继续问"的两个理由之一(另一个是"有待审条目")——
     *    两个都没有时,首次加载之后就**彻底停下**,不再安排下一次。
     */
    taskActive?: boolean;
    // [M-1 ①] 确认落库后回调页面刷新进度面板/结果行与统计(回写"已确认·已计入")
    onDecided?: (info: { resultId: number; detected: boolean | null }) => void;
}) {
    const [items, setItems] = useState<IdentityReviewItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [savingId, setSavingId] = useState<number | null>(null);
    const [decisionError, setDecisionError] = useState<string | null>(null);
    const [customNames, setCustomNames] = useState<Record<number, string>>({});
    // [工单 2026-08-03 ①] 全文按 result_id 单独拉、单独缓存,不进列表接口
    const [fullAnswers, setFullAnswers] = useState<Record<number, FullAnswer>>({});
    const [expandedIds, setExpandedIds] = useState<Record<number, boolean>>({});
    const [fullLoadingId, setFullLoadingId] = useState<number | null>(null);
    const anchorRefs = useRef<Record<number, HTMLElement | null>>({});
    const requestGeneration = useRef(0);
    const loadInFlight = useRef<{ brandId: number; promise: Promise<void> } | null>(null);
    const decisionRequestIds = useRef<Record<string, string>>({});
    /** [#182] 连续几次取回来没变化 —— 退避用。 */
    const unchangedStreak = useRef(0);
    const firstLoadDone = useRef(false);
    /** 调度要读"此刻有没有待审",但它活在 effect 的闭包里 ⇒ 用 ref 读当前值。 */
    const itemsRef = useRef<IdentityReviewItem[]>([]);

    /**
     * [#182] `manual` = 用户自己点了「刷新」(或首次加载)。
     * 🔴 只有这两种情况显示 loading 骨架:**后台重取不进 loading** ——
     *    内容没变却因为 loading 态切一下而整块重绘,那正是"每 30 秒闪一下"的观感来源。
     */
    const load = useCallback((signal?: AbortSignal, opts?: { manual?: boolean }) => {
        if (loadInFlight.current?.brandId === brandId) return loadInFlight.current.promise;

        const generation = ++requestGeneration.current;
        const showSpinner = shouldShowLoading({
            manual: !!opts?.manual, firstLoad: !firstLoadDone.current,
        });
        const promise = (async () => {
            if (showSpinner) setLoading(true);
            try {
                const response = await authFetch(`/api/monitoring/identity-reviews?brand_id=${brandId}`, { signal });
                if (!response.ok) throw new Error('待确认内容读取失败');
                const payload = await response.json();
                if (generation === requestGeneration.current) {
                    const next: IdentityReviewItem[] = Array.isArray(payload.items) ? payload.items : [];
                    /**
                     * [#182] 内容变没变决定下一次的间隔:变了回到最快档,没变就退避。
                     * 🔴 从 ref 读当前值,**不要**借 `setItems(prev => …)` 的 updater 做这件事:
                     *    updater 在 StrictMode 下会被调用两次,计数会凭空多加一次。
                     */
                    const changed = !identityItemsEqual(itemsRef.current, next);
                    unchangedStreak.current = nextUnchangedStreak(unchangedStreak.current, changed);
                    // [客户反馈⑤ 机制A 2026-08-09] 30 秒轮询原本**无条件** setItems 一个新数组引用,
                    //   内容一字没变也会让整块琥珀色面板连同下面的结果行重渲染 —— 用户看到的就是
                    //   "监测中心每 30 秒闪一下"(雅栖 2 条 pending 身份确认时面板常驻 → 一直闪)。
                    //   这里只在**内容真变了**时才换引用。
                    setItems(prev => {
                        const kept = identityItemsEqual(prev, next) ? prev : next;
                        itemsRef.current = kept;
                        return kept;
                    });
                }
            } catch (error) {
                if (signal?.aborted) return;
                lazyToast.error(error instanceof Error ? error.message : '待确认内容读取失败');
            } finally {
                if (generation === requestGeneration.current) {
                    if (showSpinner) setLoading(false);
                    firstLoadDone.current = true;
                }
            }
        })();
        loadInFlight.current = { brandId, promise };
        void promise.finally(() => {
            if (loadInFlight.current?.promise === promise) loadInFlight.current = null;
        });
        return promise;
    }, [brandId]);

    useEffect(() => {
        setItems([]);
        setCustomNames({});
        setDecisionError(null);
        decisionRequestIds.current = {};
        unchangedStreak.current = 0;
        firstLoadDone.current = false;
        const controller = new AbortController();
        let timer: number | undefined;
        /**
         * 🔴 [#182] 「有事才轮」。老代码是 `setTimeout(poll, 30_000)` **无条件**接上 ——
         *    不看有没有待审、也不看有没有任务在跑,于是每个开着监测页的人每分钟两次
         *    打同一个端点(Deploy 09-12 读数:该端点今天 165 次)。
         *    现在下一次的间隔由纯函数决定,**返回 null 就彻底不再安排**:
         *    没待审、没任务、或页面不可见 ⇒ 定时器停下,不在后台醒来。
         */
        const schedule = () => {
            if (timer !== undefined) { window.clearTimeout(timer); timer = undefined; }
            if (controller.signal.aborted || readOnly) return;
            const delay = nextPollDelayMs({
                hasPending: itemsRef.current.length > 0,
                taskActive,
                unchangedStreak: unchangedStreak.current,
                visible: typeof document === 'undefined' || document.visibilityState === 'visible',
            });
            if (delay === null) return;
            timer = window.setTimeout(() => { void poll(); }, delay);
        };
        const poll = async () => {
            await load(controller.signal);
            schedule();
        };
        void poll();
        /**
         * 🔴 回到前台**立刻取一次**(不是等下一个周期)——
         *    用户切回来时看到的必须是现在的状态,而不是他离开那一刻的快照。
         */
        const onVisibility = () => {
            if (typeof document === 'undefined') return;
            if (document.visibilityState === 'visible') { void poll(); } else { schedule(); }
        };
        if (typeof document !== 'undefined') {
            document.addEventListener('visibilitychange', onVisibility);
        }
        return () => {
            controller.abort();
            if (typeof document !== 'undefined') {
                document.removeEventListener('visibilitychange', onVisibility);
            }
            if (loadInFlight.current?.brandId === brandId) loadInFlight.current = null;
            if (timer !== undefined) window.clearTimeout(timer);
            requestGeneration.current += 1;
        };
    }, [brandId, load, readOnly, taskActive]);

    // [工单 2026-08-03 ①] 展开完整回答。收起时不清缓存(再展开不重复打接口),
    // 但**每次展开都重新定位** —— 用户可能滚走了。
    const toggleFullAnswer = async (item: IdentityReviewItem) => {
        const alreadyOpen = !!expandedIds[item.id];
        if (alreadyOpen) {
            setExpandedIds(current => ({ ...current, [item.id]: false }));
            return;
        }
        setExpandedIds(current => ({ ...current, [item.id]: true }));
        if (!fullAnswers[item.id]) {
            setFullLoadingId(item.id);
            try {
                const response = await authFetch(
                    `/api/monitoring/identity-reviews/${item.id}/full-response?brand_id=${brandId}`,
                );
                if (!response.ok) throw new Error('完整回答读取失败');
                const payload = await response.json();
                setFullAnswers(current => ({
                    ...current,
                    [item.id]: {
                        text: String(payload?.full_response || ''),
                        anchor: (payload?.anchor as AnswerAnchor) || 'none',
                        start: typeof payload?.anchor_start === 'number' ? payload.anchor_start : null,
                        end: typeof payload?.anchor_end === 'number' ? payload.anchor_end : null,
                    },
                }));
            } catch (error) {
                setExpandedIds(current => ({ ...current, [item.id]: false }));
                lazyToast.error(error instanceof Error ? error.message : '完整回答读取失败');
                return;
            } finally {
                setFullLoadingId(null);
            }
        }
        // 等这一帧渲染完再滚 —— 直接滚会滚到还没挂上的节点(等于没滚)
        requestAnimationFrame(() => {
            const target = anchorRefs.current[item.id];
            if (target) target.scrollIntoView({ block: 'center', behavior: 'auto' });
        });
    };

    const decide = async (item: IdentityReviewItem, action: 'yes' | 'no' | 'custom', name: string) => {
        if (readOnly) {
            lazyToast.info('演示案例为只读，不能提交品牌确认');
            return;
        }
        if (!name.trim() && action !== 'no') {
            lazyToast.info('请先选择候选名称或填写正确名称');
            return;
        }
        setSavingId(item.id);
        setDecisionError(null);
        const decisionKey = [
            brandId,
            item.id,
            item.identity_decision_version,
            item.identity_evidence_hash,
            action,
            name.trim(),
        ].join(':');
        const stableRequestId = decisionRequestIds.current[decisionKey] || requestId();
        decisionRequestIds.current[decisionKey] = stableRequestId;
        try {
            const response = await authFetch(`/api/monitoring/identity-reviews/${item.id}/decision`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    brand_id: brandId,
                    action,
                    selected_name: name.trim(),
                    expected_version: item.identity_decision_version,
                    evidence_hash: item.identity_evidence_hash,
                    request_id: stableRequestId,
                }),
            });
            const payload = await response.json().catch(() => ({} as Record<string, any>));
            if (!response.ok) {
                throw new Error(payload.detail || '保存失败，请刷新后重试');
            }
            delete decisionRequestIds.current[decisionKey];
            setItems(current => current.filter(value => value.id !== item.id));
            // [M-1 ①②] 后端已用确认结果对原回答重新判定并计入 —— 如实回显计入结论
            const detected = typeof payload?.data?.is_detected === 'boolean'
                ? payload.data.is_detected as boolean
                : null;
            lazyToast.success(
                detected === false
                    ? '已确认 · 本条按「未提及」计入统计'
                    : '已确认 · 本条已重新判定并计入统计',
            );
            onDecided?.({ resultId: item.id, detected });
            /**
             * 🔴 [#182 §1.2] 裁决完**立刻重取一次** —— 用户刚做完一条,
             *    这一条该从列表里消失。等下一个轮询周期(最快 60 秒)才更新,
             *    看起来就像"点了没反应"。
             *    走后台档(不显示 loading 骨架,避免整块闪)。
             */
            void load();
        } catch (error) {
            const message = error instanceof Error ? error.message : '保存失败，请刷新后重试';
            setDecisionError(message);
            lazyToast.error(message);
        } finally {
            setSavingId(null);
        }
    };

    // 加载态必须纳入判空:数据还没回来时 items 恒为 [],旧写法会先渲染出
    // "仅这 0 条待确认" —— 用户被告知一个还没查出来的结论。
    if (loading || items.length === 0) return null;

    return (
        // [M-1 ⑤] 卡片容器错版修复:border-y 全宽段落原先没有横向内边距,内容顶边框渲染
        // [工单 2026-08-03 ② · Owner 拍板] 改成与同页卡片同款圆角边框卡。
        //   实测(生产 71edab7f · 1024/1440/1536 三档):面板与「下一步操作」那排**逐像素齐平**,
        //   几何上从来没有越界。但它是 border-y 全出血带(方角、无左右边框),而四周邻居全是
        //   rounded-xl 圆角卡 —— 于是**看起来**像冲出了卡片边界。改的是这个视觉不一致,
        //   不是改宽度:左右边界本来就对齐,加上左右边框和圆角后对齐关系不变。
        <section className="mb-5 min-w-0 overflow-hidden rounded-xl border border-amber-500/30 bg-amber-500/5 p-4" aria-labelledby="identity-review-title" data-testid="identity-review-panel">
            <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                    <h2 id="identity-review-title" className="flex items-center gap-2 text-base font-semibold text-foreground">
                        <AlertCircle className="h-4 w-4 text-amber-500" />需要确认的品牌名称
                    </h2>
                    <p className="mt-1 text-sm text-muted-foreground">
                        {readOnly
                            ? '演示案例展示冻结结果；品牌确认操作仅真实客户负责人可执行。'
                            /* [M-1 ④] 只有这几条待确认,其余结果已计入 —— 不再让用户以为整轮作废 */
                            : `仅这 ${items.length} 条待确认，其余监测结果已正常计入、不受影响；确认后本条会立即按确认结果重新判定并计入。`}
                    </p>
                </div>
                <Button variant="ghost" size="sm" data-testid="identity-review-refresh"
                    aria-label="刷新待确认列表"
                    onClick={() => void load(undefined, { manual: true })} disabled={loading}>
                    <RefreshCw className={`mr-2 h-4 w-4 ${loading ? 'animate-spin' : ''}`} />刷新
                </Button>
            </div>
            {decisionError ? (
                <p role="alert" className="mt-3 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
                    {decisionError}
                </p>
            ) : null}
            <div className="mt-4 grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-2">
                {items.map(item => {
                    const candidates = Array.isArray(item.identity_candidates) ? item.identity_candidates : [];
                    const busy = savingId === item.id;
                    const evidenceText = item.identity_evidence_snippet || item.response_snippet || '';
                    return (
                        <article key={item.id} className="min-w-0 overflow-hidden rounded-md border border-border bg-card p-4">
                            <div className="flex flex-wrap items-center gap-2 text-sm">
                                <span className="font-medium text-foreground">{item.keyword}</span>
                                <span className="text-muted-foreground">{item.platform}</span>
                            </div>
                            <div
                                className="mt-3 max-h-72 min-h-24 overflow-y-auto overscroll-contain rounded-md border border-border/70 bg-muted/20 p-3 pr-2 text-sm leading-6 text-muted-foreground [overflow-wrap:anywhere]"
                                aria-label={`${item.keyword} 的待确认回答`}
                                data-testid={`identity-review-evidence-${item.id}`}
                                tabIndex={0}
                            >
                                <div className="prose prose-sm dark:prose-invert max-w-none break-words prose-headings:mb-2 prose-headings:mt-3 prose-headings:text-sm prose-p:my-2 prose-p:leading-6 prose-li:my-0.5 prose-table:block prose-table:max-w-full prose-table:overflow-x-auto prose-th:p-2 prose-td:p-2 prose-a:break-all">
                                    <ReactMarkdown>
                                        {item.identity_evidence_snippet || item.response_snippet || '本条回答没有可展示的证据片段'}
                                    </ReactMarkdown>
                                </div>
                            </div>
                            {/* [工单 2026-08-03 ①] 看得到全文:卡片里那段只是全文前 500 字,
                                这里给唯一入口。按 result_id 单独拉,列表接口不变。 */}
                            <div className="mt-2">
                                <Button
                                    size="sm"
                                    variant="ghost"
                                    className="h-7 px-2 text-xs"
                                    onClick={() => void toggleFullAnswer(item)}
                                    disabled={fullLoadingId === item.id}
                                    data-testid={`toggle-full-answer-${item.id}`}
                                    aria-expanded={!!expandedIds[item.id]}
                                >
                                    {expandedIds[item.id]
                                        ? <><ChevronUp className="mr-1 h-3.5 w-3.5" />收起完整回答</>
                                        : <><ChevronDown className="mr-1 h-3.5 w-3.5" />{fullLoadingId === item.id ? '正在读取…' : '查看完整回答'}</>}
                                </Button>
                            </div>
                            {expandedIds[item.id] && fullAnswers[item.id] ? (() => {
                                const full = fullAnswers[item.id];
                                const start = full.anchor !== 'none' && typeof full.start === 'number' ? full.start : null;
                                const end = full.anchor !== 'none' && typeof full.end === 'number' ? full.end : null;
                                const head = start === null ? full.text : full.text.slice(0, start);
                                const body = start === null ? '' : full.text.slice(start, end ?? undefined);
                                const tail = end === null ? '' : full.text.slice(end);
                                return (
                                    <div
                                        className="mt-2 max-h-96 overflow-y-auto overscroll-contain rounded-md border border-border/70 bg-background p-3 text-sm leading-6 text-foreground [overflow-wrap:anywhere]"
                                        data-testid={`full-answer-${item.id}`}
                                        aria-label={`${item.keyword} 的完整回答`}
                                        tabIndex={0}
                                    >
                                        <p className="mb-2 text-xs text-muted-foreground">
                                            完整回答共 {full.text.length} 字
                                            {full.anchor === 'evidence_window' && ' · 已定位到判断依据所在段落'}
                                            {full.anchor === 'seen_prefix' && ' · 已定位到上方片段之后的内容'}
                                            {full.anchor === 'none' && ' · 无法定位到具体段落，下面是全文'}
                                        </p>
                                        <div className="whitespace-pre-wrap break-words">
                                            {head}
                                            {start !== null ? (
                                                full.anchor === 'seen_prefix' ? (
                                                    <>
                                                        <span
                                                            ref={node => { anchorRefs.current[item.id] = node; }}
                                                            className="my-2 block border-t border-dashed border-amber-500/60 pt-2 text-xs font-medium text-amber-600"
                                                            data-testid={`full-answer-anchor-${item.id}`}
                                                        >
                                                            ↓ 以下是上方片段里看不到的内容
                                                        </span>
                                                        {body}
                                                    </>
                                                ) : (
                                                    <mark
                                                        ref={node => { anchorRefs.current[item.id] = node; }}
                                                        className="rounded bg-amber-500/30 px-0.5 font-medium text-foreground"
                                                        data-testid={`full-answer-anchor-${item.id}`}
                                                    >
                                                        {body}
                                                    </mark>
                                                )
                                            ) : null}
                                            {tail}
                                        </div>
                                    </div>
                                );
                            })() : null}
                            {candidates.length > 0 ? (
                                <div className="mt-3 space-y-2">
                                    {candidates.map(candidate => {
                                        // [M-1 ③] 高亮定位:展示原文中该称呼的上下文片段(「」括起,
                                        // 避免与候选名文本节点撞唯一性断言)
                                        const context = candidateContext(evidenceText, candidate);
                                        return (
                                            <div key={candidate} className="min-w-0 border-t border-border pt-2">
                                                <div className="flex flex-wrap items-center justify-between gap-2">
                                                    <span className="min-w-0 break-words text-sm font-medium text-foreground">{candidate}</span>
                                                    <Button size="sm" onClick={() => void decide(item, 'yes', candidate)} disabled={busy || readOnly}>
                                                        <Check className="mr-1 h-4 w-4" />是这个品牌
                                                    </Button>
                                                </div>
                                                <p
                                                    className="mt-1 break-words text-xs leading-5 text-muted-foreground [overflow-wrap:anywhere]"
                                                    data-testid="candidate-context"
                                                >
                                                    {context ? (
                                                        <>
                                                            …{context.before}
                                                            <mark className="rounded bg-amber-500/30 px-0.5 font-medium text-foreground">「{context.hit}」</mark>
                                                            {context.after}…
                                                        </>
                                                    ) : (
                                                        '该称呼出现在完整回答中，请在上方回答片段内定位后再判断。'
                                                    )}
                                                </p>
                                            </div>
                                        );
                                    })}
                                    <Button
                                        size="sm"
                                        variant="outline"
                                        onClick={() => void decide(item, 'no', candidates[0])}
                                        disabled={busy || readOnly}
                                    >
                                        <X className="mr-1 h-4 w-4" />都不是这些品牌
                                    </Button>
                                </div>
                            ) : (
                                <p className="mt-3 text-sm text-muted-foreground">
                                    没有可靠候选。若回答里没有客户品牌，可直接记为未出现；若用了其他名称，请在下方填写。
                                </p>
                            )}
                            <div className="mt-3 flex flex-col gap-3 rounded-md border border-border bg-muted/20 p-3 sm:flex-row sm:items-center sm:justify-between">
                                <div className="min-w-0">
                                    <p className="text-sm font-medium text-foreground">整段回答里没有客户品牌？</p>
                                    <p className="mt-1 text-xs leading-5 text-muted-foreground">
                                        直接记为未出现，本条会正常计入监测结果。
                                    </p>
                                </div>
                                <Button
                                    size="sm"
                                    variant="outline"
                                    className="w-full shrink-0 sm:w-auto"
                                    onClick={() => void decide(item, 'no', '')}
                                    disabled={busy || readOnly}
                                    data-testid={`identity-not-mentioned-${item.id}`}
                                >
                                    <X className="mr-1 h-4 w-4" />没有出现
                                </Button>
                            </div>
                            <p className="mt-3 text-xs text-muted-foreground">
                                如果回答使用了客户的其他名称，请填写后确认：
                            </p>
                            <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                                <Input
                                    value={customNames[item.id] || ''}
                                    onChange={event => setCustomNames(current => ({ ...current, [item.id]: event.target.value }))}
                                    placeholder="填写正确的品牌名称"
                                    maxLength={80}
                                    disabled={readOnly}
                                />
                                <Button
                                    variant="secondary"
                                    disabled={busy || readOnly || !(customNames[item.id] || '').trim()}
                                    onClick={() => void decide(item, 'custom', customNames[item.id] || '')}
                                >
                                    使用这个名称
                                </Button>
                            </div>
                        </article>
                    );
                })}
            </div>
        </section>
    );
}
