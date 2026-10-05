/**
 * UI-34:发布命令状态轮询 hook。两页共用(状态页轮询、确认页 confirm 之后接力)。
 *
 * 责任边界:
 *   · 排期与取消(卸载 / 换命令 / 终态停轮询)在这里;
 *   · **要不要采纳一份响应**在 `statusVersionGuard.ts`(纯函数,可被 node 脚本真跑)。
 *
 * 🔴 「终态」由服务端的 `nextAction === null` 判,不由前端列一张终态枚举表。
 *    后端 `_command_status_payload()` 里 `next_action = None` 只出现在 commit 分支;
 *    前端再抄一份终态清单 = 第二套状态机,后端加一档就漂。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import axios from 'axios';
import { fetchCommandStatus } from './api';
import type { CommandStatusResponse } from './contracts';
import { acceptStatusUpdate, type StatusUpdateVerdict } from './statusVersionGuard';

export interface PublishStatusPollingState {
    status: CommandStatusResponse | null;
    loading: boolean;
    /** 传输层错误(已格式化前交给页面自己翻译);null = 当前没有未处理的错误。 */
    error: unknown;
    /** 最近一次响应的采纳判定 —— 排障时能直接看出「慢响应被丢了」。 */
    lastVerdict: StatusUpdateVerdict | null;
    /** 被单调守卫丢弃的响应计数。可观测,便于判据断言「确实丢过」。 */
    discardedCount: number;
    refresh: () => void;
}

const DEFAULT_INTERVAL_MS = 4000;

export function usePublishStatusPolling(
    commandId: string | undefined,
    options: { intervalMs?: number; enabled?: boolean } = {},
): PublishStatusPollingState {
    const intervalMs = options.intervalMs ?? DEFAULT_INTERVAL_MS;
    const enabled = options.enabled ?? true;

    const [status, setStatus] = useState<CommandStatusResponse | null>(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState<unknown>(null);
    const [lastVerdict, setLastVerdict] = useState<StatusUpdateVerdict | null>(null);
    const [discardedCount, setDiscardedCount] = useState(0);

    /** 🔴 守卫要比对的是**当前真值**,不是闭包里那份可能过期的 state。 */
    const latestRef = useRef<CommandStatusResponse | null>(null);
    const timerRef = useRef<number | undefined>(undefined);
    const abortRef = useRef<AbortController | null>(null);
    const [tick, setTick] = useState(0);

    // 换命令 ⇒ 手上那份不再是「同一条命令的上一版」,必须清空,
    // 否则 A 命令的 statusVersion=9 会把 B 命令的 statusVersion=1 判成 stale。
    useEffect(() => {
        latestRef.current = null;
        setStatus(null);
        setLastVerdict(null);
        setDiscardedCount(0);
    }, [commandId]);

    const refresh = useCallback(() => setTick((n) => n + 1), []);

    useEffect(() => {
        if (!commandId || !enabled) return;

        let cancelled = false;
        const controller = new AbortController();
        abortRef.current = controller;

        const run = async () => {
            setLoading(true);
            try {
                const data = await fetchCommandStatus(commandId, controller.signal);
                if (cancelled) return;
                // 🔴 单调守卫。慢响应(更低 statusVersion)在这里被丢掉,
                //    completed 不会被迟到的 running 顶回去。
                const result = acceptStatusUpdate(latestRef.current, data, commandId);
                setLastVerdict(result.verdict);
                if (result.changed && result.next) {
                    latestRef.current = result.next;
                    setStatus(result.next);
                } else {
                    setDiscardedCount((n) => n + 1);
                }
                setError(null);
            } catch (e) {
                if (cancelled || axios.isCancel(e)) return;
                setError(e);
            } finally {
                if (!cancelled) setLoading(false);
            }
        };

        void run();

        return () => {
            cancelled = true;
            controller.abort();
            if (timerRef.current !== undefined) window.clearTimeout(timerRef.current);
        };
    }, [commandId, enabled, tick]);

    // 排期下一次。终态(服务端不再给出口)就停 —— 不空转、不烧配额。
    useEffect(() => {
        if (!commandId || !enabled) return;
        const terminal = status !== null && status.nextAction === null;
        if (terminal) return;
        timerRef.current = window.setTimeout(() => setTick((n) => n + 1), intervalMs);
        return () => {
            if (timerRef.current !== undefined) window.clearTimeout(timerRef.current);
        };
    }, [commandId, enabled, intervalMs, status, tick]);

    useEffect(() => () => { abortRef.current?.abort(); }, []);

    return { status, loading, error, lastVerdict, discardedCount, refresh };
}
