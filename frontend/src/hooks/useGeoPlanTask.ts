/**
 * useGeoPlanTask — C 端 GEO 方案异步任务轮询 hook (CTO-15.5 Phase 4 PLAN 05 Task 5.1)
 *
 * 作用: 给 旧 C 端 GEO 方案页 用 · 每 2s 轮询 GET /api/geo-plan/task/{id}
 *
 * 状态机: queued -> running (进度推进) -> done/failed/cancelled/timeout
 * 终态到达后自动停止轮询 (clearInterval)
 *
 * 用法:
 *   const { task, isLoading, error, cancel, refetch } = useGeoPlanTask(taskId);
 *
 * 对齐 PRD Section 4.2 GEO-REQ-API-2 任务响应结构:
 *   {task_id, brand_id, brand_name, status, progress_stage, progress_percent,
 *    progress_message, result, error, data_mode, queued_at, started_at, done_at, ...}
 */

import { useEffect, useState, useCallback, useRef } from 'react';
import { authFetch } from '@/lib/api';

export type TaskStatus = 'queued' | 'running' | 'done' | 'failed' | 'cancelled' | 'timeout';
export type TaskStage = 'identify' | 'expand' | 'audit' | 'cluster' | 'pricing' | 'done';

export interface GeoPlanTaskDTO {
  task_id: number;
  brand_id: number;
  brand_name?: string | null;
  brand_industry?: string | null;
  status: TaskStatus;
  progress_stage: TaskStage | null;
  progress_percent: number; // 0-100
  progress_message: string | null;
  data_mode: 'full' | 'l1l2_fallback';
  source?: string | null;
  queued_at?: string | null;
  started_at?: string | null;
  heartbeat_at?: string | null;
  done_at?: string | null;
  linked_quote_id?: number | null;
  // 仅 done 返回
  result?: Record<string, any> | null;
  // 仅 failed/timeout/cancelled 返回
  error?: { code?: string; detail?: string } | null;
}

export interface UseGeoPlanTaskResult {
  task: GeoPlanTaskDTO | null;
  isLoading: boolean;
  error: Error | null;
  cancel: () => Promise<void>;
  refetch: () => Promise<void>;
}

const TERMINAL: ReadonlyArray<TaskStatus> = ['done', 'failed', 'cancelled', 'timeout'];
const POLL_INTERVAL_MS = 2000;

export function useGeoPlanTask(taskId: number | null): UseGeoPlanTaskResult {
  const [task, setTask] = useState<GeoPlanTaskDTO | null>(null);
  const [isLoading, setIsLoading] = useState<boolean>(!!taskId);
  const [error, setError] = useState<Error | null>(null);
  const intervalRef = useRef<number | null>(null);
  const stoppedRef = useRef<boolean>(false);

  const stopPolling = useCallback(() => {
    if (intervalRef.current !== null) {
      window.clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
    stoppedRef.current = true;
  }, []);

  const fetchOnce = useCallback(async (tid: number) => {
    try {
      const res = await authFetch(`/api/geo-plan/task/${tid}`);
      if (!res.ok) {
        if (res.status === 403) {
          setError(new Error('无权访问该任务'));
          stopPolling();
          return;
        }
        if (res.status === 404) {
          setError(new Error('任务不存在'));
          stopPolling();
          return;
        }
        // 5xx 保持轮询(可能后端抖动)
        setError(new Error(`服务异常 (${res.status})`));
        return;
      }
      const data = await res.json();
      // 后端返 { ... } 直接平铺 (无 success wrapper · 对齐 PLAN 01 API)
      const dto: GeoPlanTaskDTO = {
        task_id: data.task_id,
        brand_id: data.brand_id,
        brand_name: data.brand_name ?? null,
        brand_industry: data.brand_industry ?? null,
        status: data.status,
        progress_stage: (data.progress_stage ?? null) as TaskStage | null,
        progress_percent: Number(data.progress_percent ?? 0),
        progress_message: data.progress_message ?? null,
        data_mode: (data.data_mode ?? 'full') as 'full' | 'l1l2_fallback',
        source: data.source ?? null,
        queued_at: data.queued_at ?? null,
        started_at: data.started_at ?? null,
        heartbeat_at: data.heartbeat_at ?? null,
        done_at: data.done_at ?? null,
        linked_quote_id: data.linked_quote_id ?? null,
        result: data.result ?? null,
        error: data.error ?? null,
      };
      setTask(dto);
      setError(null);
      if (TERMINAL.includes(dto.status)) {
        stopPolling();
      }
    } catch (e: any) {
      // 网络错误 → 保持轮询(不立即停,让用户恢复网络后能看到数据)
      setError(new Error(e?.message || '网络错误'));
    } finally {
      setIsLoading(false);
    }
  }, [stopPolling]);

  const refetch = useCallback(async () => {
    if (!taskId) return;
    await fetchOnce(taskId);
  }, [taskId, fetchOnce]);

  const cancel = useCallback(async () => {
    if (!taskId) return;
    try {
      const res = await authFetch(`/api/geo-plan/task/${taskId}/cancel`, { method: 'POST' });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(
          typeof errData?.detail === 'string'
            ? errData.detail
            : errData?.detail?.message || `取消失败 (${res.status})`
        );
      }
      // 成功后立即刷一次让 UI 切到 cancelled
      await fetchOnce(taskId);
    } catch (e: any) {
      setError(new Error(e?.message || '取消失败'));
      throw e;
    }
  }, [taskId, fetchOnce]);

  useEffect(() => {
    stoppedRef.current = false;
    setTask(null);
    setError(null);
    if (!taskId) {
      setIsLoading(false);
      return;
    }
    setIsLoading(true);
    // 立即 fetch 一次(不等 2s)
    void fetchOnce(taskId);
    // setInterval 2s 轮询
    intervalRef.current = window.setInterval(() => {
      if (stoppedRef.current) return;
      void fetchOnce(taskId);
    }, POLL_INTERVAL_MS);
    return () => {
      if (intervalRef.current !== null) {
        window.clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [taskId, fetchOnce]);

  return { task, isLoading, error, cancel, refetch };
}

// ====== 5 阶段进度区间常量 (对齐 PRD 4.2 + 后端 one_click_geo_plan) ======
export const STAGE_RANGES: Record<TaskStage, { start: number; end: number; label: string }> = {
  identify: { start: 0, end: 10, label: '识别品牌行业' },
  expand:   { start: 10, end: 40, label: '扩展关键词池' },
  audit:    { start: 40, end: 60, label: '竞品饱和度审计' },
  cluster:  { start: 60, end: 85, label: '主题聚类' },
  pricing:  { start: 85, end: 100, label: '三档定价' },
  done:     { start: 100, end: 100, label: '完成' },
};

export const STAGE_ORDER: TaskStage[] = ['identify', 'expand', 'audit', 'cluster', 'pricing'];

export default useGeoPlanTask;
