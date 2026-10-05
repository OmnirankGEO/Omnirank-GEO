/**
 * 语义洞察任务 hook。
 *
 * 硬约束（施工单 §6.1 / §13.3）：
 * - 只有显式"生成洞察"才创建任务并调 LLM；普通渲染/切 Tab/筛选/分页零 LLM 调用。
 * - 20 次并发/双击只创建 1 个任务：单飞锁 + 稳定 request_id 幂等。
 * - 失败保留全部确定性图表，显示"洞察暂不可用"，不自动循环重试烧模型。
 * - 切换客户/周期/卸载时取消：用"代次令牌"作废所有在飞回调，绝不把上一个品牌的洞察结果
 *   写进新品牌的状态（跨客户串数据防护）。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { observationClient } from '../client';
import { ObservationApiError, type Granularity, type InsightJobStatus } from '../types';

export type InsightPhase = 'idle' | 'starting' | 'running' | 'completed' | 'failed';

export interface SemanticInsightState {
  phase: InsightPhase;
  progress: number;
  result: InsightJobStatus | null;
  errorMessage: string | null;
  retryable: boolean;
  /** 显式生成/刷新；重复调用在单飞期内合并为同一任务。 */
  generate: () => void;
  reset: () => void;
}

const POLL_INTERVAL_MS = 1500;
const MAX_POLLS = 40; // 兜底上限，防止后端卡 running 时无限轮询

/** 稳定 request_id：与品牌+粒度+本次意图绑定；同一意图（含刷新/重试）复用，保证后端幂等命中。 */
function makeRequestId(brandId: number, granularity: Granularity): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  const rand = c?.randomUUID ? c.randomUUID() : `${brandId}-${granularity}-${Date.now().toString(36)}`;
  return `insight-${brandId}-${granularity}-${rand}`;
}

export function useSemanticInsight(brandId: number | null, granularity: Granularity): SemanticInsightState {
  const [phase, setPhase] = useState<InsightPhase>('idle');
  const [progress, setProgress] = useState(0);
  const [result, setResult] = useState<InsightJobStatus | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);

  // 单飞锁：一次 generate 生命周期内只允许一个在飞创建/轮询链
  const inFlightRef = useRef(false);
  const requestIdRef = useRef<string | null>(null);
  const pollTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 代次令牌：每次切换/取消 +1，作废所有捕获旧代次的在飞回调
  const genRef = useRef(0);

  const clearPoll = useCallback(() => {
    if (pollTimerRef.current) {
      clearTimeout(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  // 作废在飞工作（不动展示状态）
  const invalidate = useCallback(() => {
    genRef.current += 1;
    clearPoll();
    inFlightRef.current = false;
  }, [clearPoll]);

  const reset = useCallback(() => {
    invalidate();
    requestIdRef.current = null;
    setPhase('idle');
    setProgress(0);
    setResult(null);
    setErrorMessage(null);
    setRetryable(false);
  }, [invalidate]);

  // 切换客户/粒度 → 作废旧在飞并重置展示；卸载同样作废
  useEffect(() => {
    reset();
    return () => {
      invalidate();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [brandId, granularity]);

  const poll = useCallback(
    (jobId: string, count: number, gen: number) => {
      if (genRef.current !== gen || brandId === null) return;
      observationClient
        .getInsight(brandId, jobId)
        .then((status) => {
          if (genRef.current !== gen) return;
          if (typeof status.progress_percent === 'number') setProgress(status.progress_percent);
          if (status.state === 'completed') {
            setResult(status);
            setProgress(100);
            setPhase('completed');
            inFlightRef.current = false;
            // 完成后清幂等键：下次"重新生成洞察"是新意图，用新 request_id 真正重算；
            // 失败态不清（重试属同一次意图的瞬态重试，复用 request_id 由后端幂等去重）。
            requestIdRef.current = null;
            return;
          }
          if (status.state === 'failed') {
            setPhase('failed');
            setErrorMessage(null);
            setRetryable(true);
            inFlightRef.current = false;
            return;
          }
          // pending / running → 继续轮询（有上限，不无限烧）
          if (count >= MAX_POLLS) {
            setPhase('failed');
            setRetryable(true);
            inFlightRef.current = false;
            return;
          }
          setPhase('running');
          pollTimerRef.current = setTimeout(() => poll(jobId, count + 1, gen), POLL_INTERVAL_MS);
        })
        .catch((e: unknown) => {
          if (genRef.current !== gen) return;
          setPhase('failed');
          if (e instanceof ObservationApiError) {
            setRetryable(e.retryable);
            setErrorMessage(e.code === 'SEMANTIC_INSIGHT_UNAVAILABLE' ? null : e.message);
          } else {
            setRetryable(true);
          }
          inFlightRef.current = false;
        });
    },
    [brandId],
  );

  const generate = useCallback(() => {
    if (brandId === null) return;
    // 单飞锁：并发/双击直接忽略后续触发
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    const gen = genRef.current; // 本次生成的代次；切换/取消后旧回调会被作废
    clearPoll();
    setPhase('starting');
    setProgress(0);
    setResult(null);
    setErrorMessage(null);
    setRetryable(false);

    // 稳定幂等键：同一意图复用（含响应丢失/刷新/重试）；后端据此保证 20 并发只建 1 个任务
    if (!requestIdRef.current) requestIdRef.current = makeRequestId(brandId, granularity);
    const requestId = requestIdRef.current;

    observationClient
      .createInsight(brandId, granularity, requestId)
      .then((job) => {
        if (genRef.current !== gen) return;
        if (job.state === 'completed') {
          // 幂等命中已完成任务：确保取到完整摘要/证据引用
          observationClient
            .getInsight(brandId, job.job_id)
            .then((s) => {
              if (genRef.current !== gen) return;
              setResult(s);
              setProgress(100);
              setPhase('completed');
              inFlightRef.current = false;
              requestIdRef.current = null;
            })
            .catch(() => {
              if (genRef.current !== gen) return;
              setPhase('failed');
              setRetryable(true);
              inFlightRef.current = false;
            });
          return;
        }
        setPhase('running');
        poll(job.job_id, 0, gen);
      })
      .catch((e: unknown) => {
        if (genRef.current !== gen) return;
        setPhase('failed');
        if (e instanceof ObservationApiError) {
          setRetryable(e.retryable);
          setErrorMessage(e.code === 'SEMANTIC_INSIGHT_UNAVAILABLE' ? null : e.message);
        } else {
          setRetryable(true);
        }
        inFlightRef.current = false;
      });
  }, [brandId, granularity, clearPoll, poll]);

  return { phase, progress, result, errorMessage, retryable, generate, reset };
}
