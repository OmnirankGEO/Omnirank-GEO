/**
 * 通用异步资源 hook：把一次 fetch 映射到完整状态机。
 * 状态：loading / success / empty / error（含 403/409/423/503/样本不足/模型升级）。
 * - 切换依赖时取消在飞请求，丢弃过期响应（防串数据）。
 * - 普通渲染/切 Tab/改筛选只会重取确定性数据，绝不触发 LLM。
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { ObservationApiError, type ObservationErrorCode } from '../types';

export type ResourceStatus = 'idle' | 'loading' | 'success' | 'error';

export interface ResourceState<T> {
  status: ResourceStatus;
  data: T | null;
  /** 结构化错误码：FORBIDDEN / VERSION_CONFLICT / ENV_OVERRIDE_ACTIVE / OBSERVATION_UNAVAILABLE / INSUFFICIENT_SAMPLES / … */
  errorCode: ObservationErrorCode | null;
  httpStatus: number | null;
  errorMessage: string | null;
  retryable: boolean;
  reload: () => void;
}

/**
 * @param fetcher  接收 AbortSignal 的取数函数
 * @param deps     依赖数组；变化触发重取
 * @param enabled  false 时保持 idle（例如未选客户）
 */
export function useObservationResource<T>(
  fetcher: (signal: AbortSignal) => Promise<T>,
  deps: ReadonlyArray<unknown>,
  enabled = true,
): ResourceState<T> {
  const [status, setStatus] = useState<ResourceStatus>('idle');
  const [data, setData] = useState<T | null>(null);
  const [errorCode, setErrorCode] = useState<ObservationErrorCode | null>(null);
  const [httpStatus, setHttpStatus] = useState<number | null>(null);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [retryable, setRetryable] = useState(false);
  const [nonce, setNonce] = useState(0);

  // 保留最新 fetcher，避免把它放进 deps 造成无限重取
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!enabled) {
      setStatus('idle');
      setData(null);
      setErrorCode(null);
      setHttpStatus(null);
      setErrorMessage(null);
      return;
    }
    const controller = new AbortController();
    let active = true;
    setStatus('loading');
    setErrorCode(null);
    setErrorMessage(null);
    setHttpStatus(null);

    fetcherRef.current(controller.signal)
      .then((result) => {
        if (!active) return;
        setData(result);
        setStatus('success');
      })
      .catch((e: unknown) => {
        if (!active || controller.signal.aborted) return;
        if ((e as { name?: string })?.name === 'AbortError') return;
        if (e instanceof ObservationApiError) {
          setErrorCode(e.code);
          setHttpStatus(e.status);
          setErrorMessage(e.message);
          setRetryable(e.retryable);
        } else {
          setErrorCode('OBSERVATION_UNAVAILABLE');
          setHttpStatus(null);
          setErrorMessage(e instanceof Error ? e.message : String(e));
          setRetryable(true);
        }
        setStatus('error');
      });

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, enabled, nonce]);

  return { status, data, errorCode, httpStatus, errorMessage, retryable, reload };
}
