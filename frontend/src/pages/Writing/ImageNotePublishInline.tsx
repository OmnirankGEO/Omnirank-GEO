/**
 * Publishing center's single selected-work publisher (historical filename retained).
 * The list supplies the frozen work, while this component owns preparation, quote,
 * idempotent submission and receipts. Directory view state lives in the list so
 * switching works clears payable state without resetting account filters.
 * Existing seven-field submission contract and inp-* test hooks are unchanged.
 */
import React, { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { ShortVideoAccountMarket, type MarketViewProps } from '../Publishing/ShortVideoAccountMarket';
import { receiptAccountName, type MarketAccount } from '../Publishing/shortVideoMarket';
import { Loader2 } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import {
    /* 🔴 [#196] `ARTIFACT_COPY` 不再从这里取:那张表只有四句静态文案,
       而现在这一行要带「第 N 次检查」。文案与「还读不读」必须同源 ——
       两处各写一遍,必有一天屏幕说「准备中」而轮询已经停了。
       它仍留在 lib 里给别的调用方用。 */
    ImageNoteError, PUBLISH_ITEM_COPY, fetchPublishCommand,
    preparePublishMedia, previewPublish, stableRequestId, submitPublish,
    type PreparedArtifact, type PublishCommandState, type PublishPreview,
} from '@/lib/imageNoteApi';
import {
    buildBatchItems, commandMediaId, isCommandTerminal, totalPointsFromPreview, publishCommandLabel, failedCommandAccounts,
} from '@/pages/Publishing/imageNotePublishScope';
import { hasActions, resolveActions } from './publishErrorActions';
import { deliverPublishCompletion } from './imageNoteFlow';
import {
    artifactLine, nextAttemptAfter, planNextPoll, prepAttemptStorageKey, prepSeed,
} from './artifactPollPlan';

interface Props extends MarketViewProps {
    brandId: number;
    postId: number;
    /** 锁定版本 id。**调用方保证非空** —— 没有版本的卡根本不渲染本组件(区级说明代之)。 */
    revisionId: number;
    keyword: string;
    /** 发成功后让外层重取列表(发布态是列表字段)。 */
    onPublished?: () => void;
    publicationAllowed?: boolean;
    publicationStatusLabel?: string;
    onCommandStatus?: (status: string) => void;
    /** Central image-note lane uses the existing Douyin-capable account directory. */
    platformScope?: '抖音';
}

export function PublishRecordFallback({ brandId, statusLabel }: { brandId: number; statusLabel?: string }) {
    return <div role="status" className="space-y-2 text-sm" data-testid="inp-record-fallback">
        <p>{statusLabel || '这条图文已有发布记录'}。完整发布情况和失败处理可在发布记录中查看。</p>
        <Button asChild variant="outline" className="min-h-11"><a href={`/publish?mode=history&brand_id=${brandId}`}>查看发布记录</a></Button>
    </div>;
}

export function ImageNotePublishInline({ brandId, postId, revisionId, keyword, onPublished, publicationAllowed = true, publicationStatusLabel, onCommandStatus, platformScope, viewState, setViewState }: Props) {
    const { user } = useAuth();
    const [chosenAccount, setChosenAccount] = useState<MarketAccount | null>(null);
    const [mediaId, setMediaId] = useState<number>(0);
    const [artifact, setArtifact] = useState<PreparedArtifact | null>(null);
    const [preview, setPreview] = useState<PublishPreview | null>(null);
    const [previewErr, setPreviewErr] = useState<string | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [submitErr, setSubmitErr] = useState<ImageNoteError | null>(null);
    const [command, setCommand] = useState<PublishCommandState | null>(null);
    const selectedMediaId = commandMediaId(command, mediaId);
    useEffect(() => { if (command) onCommandStatus?.(command.command_status); }, [command, onCommandStatus]);
    const commandKey = `omnirank-image-note-command:${user?.id ?? 'none'}:${brandId}:${postId}:${revisionId}`;
    const [failedAccounts, setFailedAccounts] = useState<number[]>([]);
    const retryAccounts = failedCommandAccounts(command);
    const onPublishedRef = useRef(onPublished);
    onPublishedRef.current = onPublished;
    const mountedRef = useRef(true);
    useEffect(() => {
        mountedRef.current = true;
        return () => { mountedRef.current = false; };
    }, []);
    useEffect(() => {
        let cancelled = false;
        setCommand(null);
        try {
            const hint = JSON.parse(sessionStorage.getItem(`${commandKey}:account`) || 'null');
            setChosenAccount(hint && Number.isSafeInteger(hint.id) && hint.id > 0 && typeof hint.media_name === 'string' ? hint : null);
            const excluded = JSON.parse(sessionStorage.getItem(`${commandKey}:failed-accounts`) || '[]');
            setFailedAccounts(Array.isArray(excluded) ? excluded.filter(n => Number.isSafeInteger(n) && n > 0) : []);
            const id = sessionStorage.getItem(commandKey);
            if (id) {
                setCommand({ status: 'success', command_id: id, command_status: 'unknown', items: [] });
                void fetchPublishCommand(id).then(d => { if (!cancelled) setCommand(d); }).catch(() => undefined);
            }
        } catch { /* Storage is only a resume hint; the server still authorizes every read. */ }
        return () => { cancelled = true; };
    }, [commandKey]);
    const pollRef = useRef<number | null>(null);
    /*
     * 🔴 [#196] 素材准备是**异步**的:prepare-v2 只 claim 一行 `preparing` 就返回,
     *    上传与 mark_ready 由 cron 容器的 worker 20s 一轮做。只调一次 ⇒ 状态永远不变
     *    ——现场就是「3 分钟零后续请求、发布键一直灰着」。
     *    这条链没有 GET 端点;同 Idempotency-Key 再 POST 走回放分支、返回**当前** state,
     *    所以"再 POST 一次"就是"读一次",且不会重复 claim。
     */
    const artPollRef = useRef<number | null>(null);
    const artStartedAtRef = useRef<number>(0);
    const [artAttempts, setArtAttempts] = useState(0);
    const [artStoppedReason, setArtStoppedReason] = useState('');

    // Account reads, filters and pagination live in the shared ShortVideoAccountMarket.
    // Quotes, prepared versions, commands and receipts remain in this publisher.

    /**
     * 展开就后台准备素材 —— 不设"准备"按钮(Owner:不要弄非必要的按钮)。
     *
     * 🔴 [#196] 一次不够:**读到终态为止**。节奏与两条时间闸都在
     *    `artifactPollPlan.planNextPoll` 里(纯函数,判据用假时钟能直接跑到 120s/300s)。
     * 🔴 幂等钥匙在**同作品同版本**内稳定 —— `stableRequestId` 是确定性派生,
     *    收起再打开、甚至刷新页面都是同一把,所以不会每次展开都 claim 一条新 artifact。
     *    只有手动「重新准备」才带 `-retryN` 换一把(那条 artifact 已经 failed,
     *    同钥匙重试只会把同一条失败记录再读一遍 = 一颗点了没反应的按钮)。
     * 🔴 收起面板(组件卸载)⇒ cleanup 清掉定时器:留着它会在后台一直打接口。
     */
    useEffect(() => {
        // Reading a receipt must not start material preparation (including the
        // first render before the command resume effect has populated state).
        if (!publicationAllowed || command) return;
        try { if (sessionStorage.getItem(commandKey)) return; } catch { /* optional resume hint */ }
        let cancelled = false;
        /*
         * 🔴 [#196 §4 裁定] `failed` 的出口是「下一次展开换一把钥匙」——
         *    上一次看到 failed 时把尝试次数 +1 存进 sessionStorage,这次展开读出来。
         *    `unknown` / `ready` / `preparing` 都不递增(换 key 就是再 claim 一条,
         *    而 unknown 意味着远端可能已经收了)。
         * 🔴 换的是**派生种子**,不是在 UUID 后面接字符串:
         *    后端 `normalize_uuid` 对非 UUID 形状直接 400。
         */
        let attempt = 1;
        try {
            const raw = window.sessionStorage.getItem(prepAttemptStorageKey(postId, revisionId));
            attempt = Math.max(1, Number(raw) || 1);
        } catch { /* 隐私模式下读不到就当第一次 */ }
        const key = stableRequestId('prep', prepSeed(postId, revisionId, attempt));
        if (!artStartedAtRef.current) artStartedAtRef.current = Date.now();

        const clear = () => {
            if (artPollRef.current !== null) {
                window.clearTimeout(artPollRef.current);
                artPollRef.current = null;
            }
        };

        const tick = () => {
            void preparePublishMedia(postId, key)
                .then((a) => {
                    if (cancelled) return;
                    setArtifact(a);
                    /* 看到 failed 就把"下一次展开用第几把钥匙"记下来(只有 failed 递增)。 */
                    try {
                        const next = nextAttemptAfter(a?.state, attempt);
                        if (next !== attempt) {
                            window.sessionStorage.setItem(
                                prepAttemptStorageKey(postId, revisionId), String(next));
                        }
                    } catch { /* 存不进去就退化成"不换钥匙",不影响本次读数 */ }
                    setArtAttempts((n) => {
                        const attempts = n + 1;
                        const elapsed = Date.now() - artStartedAtRef.current;
                        const plan = planNextPoll(a?.state, elapsed, attempts);
                        setArtStoppedReason(plan.stoppedReason);
                        if (plan.keepPolling) {
                            clear();
                            artPollRef.current = window.setTimeout(tick, plan.delayMs);
                        }
                        return attempts;
                    });
                })
                .catch((e: unknown) => {
                    if (cancelled) return;
                    /* HTTP 错误走**服务端错误合同的原话**;那是这条链唯一的服务端原话来源
                       (200 + state=failed 的回包里**没有**任何说明字段,见交付说明的"缺字段")。 */
                    setArtifact({
                        geo_post_id: postId, post_revision_id: revisionId, state: 'failed',
                        user_message: e instanceof ImageNoteError ? e.contract.message : '素材准备失败',
                    });
                    setArtAttempts((n) => n + 1);
                });
        };

        tick();
        return () => { cancelled = true; clear(); };
        /* 🔴 `artifact` **不进依赖**:进了就会"每收到一次状态重建一次 effect",
           定时器被反复拆建、`artStartedAtRef` 的计时也就没有意义了。 */
    }, [postId, revisionId, publicationAllowed, !!command, commandKey]);

    const itemRequestId = useMemo(
        () => stableRequestId('item', `${postId}-${revisionId}-${mediaId}`),
        [postId, revisionId, mediaId]);

    /** 报价:选了账号且素材就绪才问。**唯一**价格来源,前端不算钱。 */
    useEffect(() => {
        if (!mediaId || artifact?.state !== 'ready') { setPreview(null); setPreviewErr(null); return; }
        let cancelled = false;
        void previewPublish({
            brand_id: brandId,
            items: [{
                item_request_id: itemRequestId,
                geo_post_id: postId,
                post_revision_id: revisionId,
                prepared_artifact_id: String(artifact.prepared_artifact_id || ''),
                manifest_hash: String(artifact.manifest_hash || ''),
                media_id: mediaId,
            }],
        }).then(p => { if (!cancelled) { setPreview(p); setPreviewErr(null); } })
            .catch((e: unknown) => {
                if (cancelled) return;
                setPreview(null);
                setPreviewErr(e instanceof ImageNoteError ? e.contract.message : '这次没能算出价格');
            });
        return () => { cancelled = true; };
    }, [brandId, postId, revisionId, mediaId, itemRequestId, artifact]);

    const total = totalPointsFromPreview(preview);
    const batchItems = useMemo(() => (artifact && mediaId ? buildBatchItems({
        chosen: [{ postId, revisionId, mediaId, itemRequestId }],
        artifacts: { [postId]: artifact },
        previewItems: preview?.items || [],
    }) : []), [artifact, mediaId, postId, revisionId, itemRequestId, preview]);

    /**
     * 发布键的闸 + **就地原因**。
     * 🔴 灰按钮不说话 = 死按钮(#179 根因①)。每一格 disabled 都必须带一句话,
     *    而且那句话画在按钮**外面**,不跟着 disabled 的透明度一起变淡。
     */
    const artState = artifact?.state || 'preparing';
    /* 屏幕上那一行、能不能重试、发布键为什么灰着 —— 都由纯函数说了算。
       🔴 组件里**不写第二套**状态判断:两处各写一遍,必有一天
       按钮说「正在准备素材…」而轮询早就停了(或者反过来)。 */
    const artPlan = planNextPoll(artState,
        Date.now() - (artStartedAtRef.current || Date.now()), artAttempts);

    const gate = useMemo(() => {
        if (submitting) return { disabled: true, reason: '正在提交…' };
        if (command) return { disabled: true, reason: publishCommandLabel(command.command_status) };
        if (!publicationAllowed) return { disabled: true, reason: '这条图文已在发布流程中，请等待回执或刷新发布情况。' };
        if (!mediaId) return { disabled: true, reason: '先选一个要发到的账号' };
        if (failedAccounts.includes(mediaId)) return { disabled: true, reason: '这个账号的上次提交已失败，请选择另一个账号重新询价。' };
        /* 素材没就绪 ⇒ 灰,理由用**同一句**:有服务端原话就用原话,否则用计划层那句
           (准备中带「第 N 次检查」、失败/未知各有各的话)。 */
        if (!artPlan.terminal || artState !== 'ready') {
            return { disabled: true, reason: artifactLine(artifact, artPlan) };
        }
        if (previewErr) return { disabled: true, reason: previewErr };
        if (total === null) return { disabled: true, reason: '正在算价…' };
        if (batchItems.length !== 1) return { disabled: true, reason: '正在算价…' };
        return { disabled: false, reason: '' };
    }, [submitting, command, failedAccounts, publicationAllowed, mediaId, artifact, artState, artPlan,
        previewErr, total, batchItems.length]);

    const doPublish = async () => {
        if (gate.disabled || total === null) return;
        setSubmitting(true); setSubmitErr(null);
        try {
            const requestId = stableRequestId('pubcmd',
                batchItems.map(i => i.item_request_id).sort().join('|'));
            const res = await submitPublish({
                request_id: requestId,
                expected_total_price_points: total,
                items: batchItems,
            });
            const commandId = res.command_id;
            if (commandId) {
                // A completed request still gets its resume hint, but a late response
                // must not navigate back to a work/customer the user already left.
                deliverPublishCompletion({
                    commandKey, commandId,
                    remember: (key, id) => {
                        try { sessionStorage.setItem(key, id); } catch { /* optional resume hint */ }
                    },
                    isCurrent: () => mountedRef.current,
                    onCurrent: () => {
                        setCommand({ status: res.status, command_id: commandId,
                            command_status: res.command_status || 'accepted', items: [] });
                        onPublishedRef.current?.();
                    },
                });
            }
        } catch (e) {
            if (e instanceof ImageNoteError) {
                if (mountedRef.current) setSubmitErr(e);
                // Another tab may already have submitted this exact version.
                // Resume the authoritative command rather than offering another pay action.
                const existing = e.contract.command_id;
                if (e.contract.code === 'PUBLISH_ATTEMPT_EXISTS' && existing) {
                    deliverPublishCompletion({
                        commandKey, commandId: existing,
                        remember: (key, id) => {
                            try { sessionStorage.setItem(key, id); } catch { /* optional resume hint */ }
                        },
                        isCurrent: () => mountedRef.current,
                        onCurrent: () => {
                            setCommand({ status: 'success', command_id: existing, command_status: 'unknown', items: [] });
                            onPublishedRef.current?.();
                        },
                    });
                }
            }
            else if (mountedRef.current) setSubmitErr(new ImageNoteError({ code: 'NETWORK', message: '提交回执暂未收到。请保持同一条内容与账号重试，系统会核对同一笔请求。' }, 0));
        } finally {
            if (mountedRef.current) setSubmitting(false);
        }
    };

    /** 轮询到终态就停。认不出的状态**当作还没结束**(宁可多轮一次,不谎称结束)。 */
    useEffect(() => {
        const id = command?.command_id;
        if (!id || isCommandTerminal(command?.command_status)) {
            if (pollRef.current !== null) { window.clearInterval(pollRef.current); pollRef.current = null; }
            return;
        }
        if (pollRef.current !== null) return;
        let cancelled = false;
        pollRef.current = window.setInterval(() => {
            void fetchPublishCommand(id).then(next => {
                if (cancelled) return;
                setCommand(next);
                if (next.command_status !== command?.command_status) onPublishedRef.current?.();
            }).catch(() => undefined);
        }, 3000);
        return () => {
            cancelled = true;
            if (pollRef.current !== null) { window.clearInterval(pollRef.current); pollRef.current = null; }
        };
        // onPublished 故意不进依赖:它是外层每次渲染都新建的函数,进依赖会把轮询反复重建。
    }, [command?.command_id, command?.command_status]);


    return (
        <div className="min-w-0 space-y-4"
            data-testid="inp-inline" data-post-id={postId}>
            {/* ⑤ 提交后的状态,轮到终态 */}
            {command && (
                <div className="space-y-2 rounded-lg border bg-muted/20 p-4 text-sm" data-testid="inp-command">
                    <div data-testid="inp-command-status">
                        {publishCommandLabel(command.command_status)}
                    </div>
                    {command.command_status === 'failed' && <a href={`/writing/image-note/${postId}`} className="inline-block underline">返回编辑，按回执原因修改内容</a>}
                    {retryAccounts.length > 0 && <div className="space-y-2">
                        <p>渠道已确认失败，算力已退回。可以保留这份图文，换一个账号重新询价。</p>
                        <Button variant="outline" size="sm" onClick={() => {
                            const excluded = [...new Set([...failedAccounts, ...retryAccounts])];
                            setFailedAccounts(excluded); setMediaId(0); setChosenAccount(null); setPreview(null); setPreviewErr(null);
                            setCommand(null); onCommandStatus?.('');
                            try { sessionStorage.removeItem(commandKey); sessionStorage.removeItem(`${commandKey}:account`); sessionStorage.setItem(`${commandKey}:failed-accounts`, JSON.stringify(excluded)); } catch { /* optional resume hint */ }
                        }}>换一个账号重新询价</Button>
                    </div>}
                    {(command.items || []).map((it, i) => (
                        <div key={i} data-testid="inp-command-item" className="text-muted-foreground">
                            {/* 机器态由 PUBLISH_ITEM_COPY 翻成人话;翻不动就原样显示机器态,
                                不编一句好听的。失败原因用服务端的 failure_reason 原话。 */}
                            {(PUBLISH_ITEM_COPY[String(it.state)]?.label || '') !== publishCommandLabel(command.command_status)
                                ? (PUBLISH_ITEM_COPY[String(it.state)]?.label || '正在核对渠道回执') : ''}
                            {` · ${receiptAccountName(it, chosenAccount)}`}
                            {it.failure_reason ? ` · ${it.failure_reason}` : ''}
                            {it.published_url ? (
                                <a className="ml-1 underline" href={it.published_url}
                                    target="_blank" rel="noreferrer"
                                    data-testid="inp-published-url">看发出去的那条</a>
                            ) : null}
                        </div>
                    ))}
                </div>
            )}
            {!command && !publicationAllowed && <PublishRecordFallback brandId={brandId} statusLabel={publicationStatusLabel} />}
            <ShortVideoAccountMarket scope={{ platform: platformScope || '抖音', imageNote: true }}
                viewState={viewState} setViewState={setViewState}
                selected={selectedMediaId ? [chosenAccount?.id === selectedMediaId ? chosenAccount : {
                    id: selectedMediaId,
                    media_name: '发布账号资料暂未读取',
                    platform: platformScope || '', price_points: null,
                }] : []}
                locked={!!command || submitting || !publicationAllowed} excludedIds={failedAccounts}
                onChange={accounts => {
                    const account = accounts[0] || null;
                    setChosenAccount(account); setMediaId(account?.id || 0); setPreview(null); setPreviewErr(null);
                    try {
                        if (account) sessionStorage.setItem(`${commandKey}:account`, JSON.stringify({ id: account.id, media_name: account.media_name, platform: account.platform, price_points: account.price_points }));
                        else sessionStorage.removeItem(`${commandKey}:account`);
                    } catch { /* display-only hint; submission still uses the server quote */ }
                }} />

            {/*
                ② 素材准备态。🔴 有服务端原话就用原话,没有才用这一层的话。
                🔴 [#196] 准备中要**看得见进度**(第 N 次检查)—— 只写"正在准备"的话,
                   卡住和正常进行长得一模一样,而现场那一次就是卡住。
            */}
            {!command && publicationAllowed && artState !== 'ready' && (
                <div className="space-y-1" data-testid="inp-artifact-block">
                    <p className="text-xs text-muted-foreground" data-testid="inp-artifact-state">
                        {artifactLine(artifact, artPlan)}
                    </p>
                    {artStoppedReason && (
                        <p className="text-xs text-muted-foreground" data-testid="inp-artifact-stopped">
                            {artStoppedReason}
                        </p>
                    )}
                    {/*
                     * 🔴 [#196] 这里**故意没有**「重新准备」按钮。
                     *    后端 `RETRYABLE_STATES` 里 failed 是可重试的,工单 a1 的措辞也像是允许;
                     *    但本面板另有一条更早的锁 —— `verify-image-note-publish` N7d:
                     *    「撤掉的东西没偷偷回来:自定义标题/正文、手动『重新准备』、失败重投」,
                     *    那是 #186/#188 的极简裁定(Owner:不要非必要的按钮)。
                     *    两条规矩打架时按**已经立着的那条锁**做,并把这一格交回 Review 定:
                     *    现状是 failed 在这个面板里**没有出口**(收起再打开是同一把幂等钥匙,
                     *    读回来的还是那条失败记录)。要给出口就得同时改 N7d,不能绕过它。
                     */}
                </div>
            )}

            {/* ③ 发布键(自带价)+ 键外理由 */}
            {!command && publicationAllowed && <div className="sticky bottom-0 z-20 flex flex-wrap items-center gap-2 border-t border-border bg-background py-3" data-testid="inp-publish-footer">
                <Button type="button" size="sm" className="min-h-11 min-w-48 text-sm" disabled={gate.disabled}
                    data-testid="inp-publish" onClick={() => void doPublish()}>
                    {submitting ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : null}
                    {/* 🔴 元指令 2:不弹 Dialog,**按钮直接标价**。
                          算不出价时按钮是灰的,文案里**不写 0** —— 0 等于说"免费"。 */}
                    {command ? '本次已提交' : total === null ? '发布' : `发布 · ${total} 算力`}
                </Button>
                {!command && gate.disabled && gate.reason && (
                    <span className="text-sm text-foreground" data-testid="inp-disabled-reason">
                        {gate.reason}
                    </span>
                )}
            </div>}

            {/* ④ 提交失败:服务端原话 + 码 */}
            {submitErr && (
                <div role="status" className="space-y-1.5" data-testid="inp-submit-error">
                    <p className="text-sm text-destructive">
                        {submitErr.contract.message}
                        <span className="ml-1 text-xs opacity-80" data-testid="inp-submit-error-code">
                            {submitErr.contract.code}
                        </span>
                    </p>
                    {submitErr.contract.reason && (
                        <p className="text-xs text-muted-foreground">{submitErr.contract.reason}</p>
                    )}
                    {/*
                        🔴 [#192 a3] 后端**已经给了出路**(`detail.actions[]`),改前一条都没渲染:
                        用户只看到「你还没有这个操作权限」,拿不到"交给团队负责人 / 联系管理员"。
                        禁猜清单第 4 条:断头必须有出口。
                        🔴 认不出去处的 action **降级成文字**,不给一颗点了没反应的按钮 ——
                        那就是又造一颗死按钮。判定在 `publishErrorActions.resolveActions`。
                    */}
                    {hasActions(submitErr.contract.actions) && (
                        <div className="flex flex-wrap items-center gap-2" data-testid="inp-error-actions">
                            {resolveActions(submitErr.contract.actions).map((a) => (
                                a.kind === 'nav' ? (
                                    <Button key={a.id} variant="outline" size="sm" asChild
                                        data-testid={`inp-error-action-${a.id}`}>
                                        <a href={a.to}>{a.label}</a>
                                    </Button>
                                ) : a.kind === 'retry' ? (
                                    <Button key={a.id} variant="outline" size="sm"
                                        data-testid={`inp-error-action-${a.id}`}
                                        onClick={() => { setSubmitErr(null); void doPublish(); }}>
                                        {a.label}
                                    </Button>
                                ) : (
                                    <span key={a.id} className="text-xs text-muted-foreground"
                                        data-testid={`inp-error-action-text-${a.id}`}>{a.label}</span>
                                )
                            ))}
                        </div>
                    )}
                </div>
            )}

            <span className="sr-only" data-testid="inp-keyword">{keyword}</span>
        </div>
    );
}
