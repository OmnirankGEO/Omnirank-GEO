/**
 * useDouyinPostTask — GEO 图文「制作中」进度轮询
 *
 * 为什么需要它:生产 nginx 对 /api/geo-douyin/* 是 60s 超时,而做一组卡实测
 * ≈137s。所以下单端点改成了立刻返回 202,真进度只能靠轮询拿。
 *
 * 形态照仓内既有的 useGeoPlanTask(2s 轮询 + 终态停):
 *   - 不自建 SSE/WebSocket(元指令 15:禁第二套 SSE)
 *   - 后端 `active` 字段说了算,前端不自己判"算不算跑完"
 *
 * 🔴 进度语义(百分比 / 预计剩余 / 是不是卡住)全部由后端算好下发。
 *    前端再算一套,就会出现"列表页 40%、详情页 60%"这种谁也说不清的分歧。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { authFetch } from '@/lib/api';

export type DouyinTaskState =
    | 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled' | 'stalled';

export interface DouyinTaskProgress {
    state: DouyinTaskState;
    stage: string;
    stage_label: string;
    percent: number;
    done: number;
    total: number;
    eta_seconds: number | null;
    elapsed_seconds: number | null;
    stalled: boolean;
    active: boolean;
    post_status?: string;
    failure_reason?: string;
}

const POLL_INTERVAL_MS = 2000;

export function useDouyinPostTask(postId: number | null, enabled = true) {
    const [progress, setProgress] = useState<DouyinTaskProgress | null>(null);
    const timerRef = useRef<number | null>(null);
    const controllerRef = useRef<AbortController | null>(null);
    const fetchingRef = useRef(false);
    // 终态到达时要通知外面刷一次内容 —— 用 ref 存回调,免得回调换了就重启轮询
    const doneCbRef = useRef<(() => void) | null>(null);

    const stop = useCallback(() => {
        if (timerRef.current !== null) {
            window.clearInterval(timerRef.current);
            timerRef.current = null;
        }
    }, []);

    const fetchOnce = useCallback(async (id: number, signal: AbortSignal) => {
        if (signal.aborted || fetchingRef.current) return;
        fetchingRef.current = true;
        try {
            const res = await authFetch(`/api/geo-douyin/posts/${id}/task`, { signal });
            if (signal.aborted) return;
            if (!res.ok) {
                // 404/403 没有继续轮的意义;5xx 可能是后端抖动,留着下一拍
                if (res.status === 404 || res.status === 403) stop();
                return;
            }
            const data = await res.json();
            if (signal.aborted) return;
            const next: DouyinTaskProgress = {
                state: data.state,
                stage: data.stage ?? '',
                stage_label: data.stage_label ?? '制作中',
                percent: Number(data.percent ?? 0),
                done: Number(data.done ?? 0),
                total: Number(data.total ?? 0),
                eta_seconds: data.eta_seconds ?? null,
                elapsed_seconds: data.elapsed_seconds ?? null,
                stalled: !!data.stalled,
                active: !!data.active,
                post_status: data.post_status ?? '',
                failure_reason: data.failure_reason ?? '',
            };
            setProgress(next);
            if (!next.active) {
                stop();
                doneCbRef.current?.();
            }
        } catch {
            /* 网络抖动:保持轮询,下一拍再试 */
        } finally { if (!signal.aborted) fetchingRef.current = false; }
    }, [stop]);

    useEffect(() => {
        stop();
        controllerRef.current?.abort();
        fetchingRef.current = false;
        setProgress(null);
        if (!postId || !enabled) { setProgress(null); return; }
        const ac = new AbortController();
        controllerRef.current = ac;
        void fetchOnce(postId, ac.signal);
        timerRef.current = window.setInterval(() => { void fetchOnce(postId, ac.signal); },
            POLL_INTERVAL_MS);
        return () => { ac.abort(); stop(); };
    }, [postId, enabled, fetchOnce, stop]);

    const onSettled = useCallback((cb: () => void) => { doneCbRef.current = cb; }, []);

    return { progress, onSettled, refetch: () => postId && controllerRef.current && fetchOnce(postId, controllerRef.current.signal) };
}

/** 秒 → "约 2 分 10 秒"。给不出估算(null)时返回空串,**不瞎编一个数**。 */
export function formatEta(seconds: number | null | undefined): string {
    if (seconds === null || seconds === undefined) return '';
    const s = Math.max(0, Math.round(seconds));
    if (s < 60) return `约 ${s} 秒`;
    const m = Math.floor(s / 60);
    const rest = s % 60;
    return rest ? `约 ${m} 分 ${rest} 秒` : `约 ${m} 分钟`;
}

export default useDouyinPostTask;
