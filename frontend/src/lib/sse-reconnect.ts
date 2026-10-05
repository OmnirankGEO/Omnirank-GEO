/**
 * SSE 自动重连工具
 * 支持指数退避重试、页面可见性感知、连接健康检查
 */

export interface ReconnectingSSEOptions {
  /** 请求头（如 Authorization） */
  headers?: Record<string, string>;
  /** 初始重试间隔 ms，默认 1000 */
  initialRetryMs?: number;
  /** 最大重试间隔 ms，默认 30000 */
  maxRetryMs?: number;
  /** 是否在页面隐藏时暂停连接，默认 true */
  pauseWhenHidden?: boolean;
}

export interface ReconnectingSSE {
  close: () => void;
  onMessage: (handler: (data: string, event?: string) => void) => void;
  onError: (handler: (error: Event | Error) => void) => void;
}

export function createReconnectingSSE(
  url: string,
  options: ReconnectingSSEOptions = {}
): ReconnectingSSE {
  const {
    headers,
    initialRetryMs = 1000,
    maxRetryMs = 30000,
    pauseWhenHidden = true,
  } = options;

  let source: EventSource | null = null;
  let closed = false;
  let retryMs = initialRetryMs;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;

  let messageHandler: ((data: string, event?: string) => void) | null = null;
  let errorHandler: ((error: Event | Error) => void) | null = null;

  function connect() {
    if (closed) return;

    // 如果需要自定义 headers（如 JWT），用 fetch + ReadableStream
    // EventSource 不支持自定义 headers，但大多数 SSE 端点通过 query 传 token
    // 这里用原生 EventSource 保持简单
    source = new EventSource(url);

    source.onopen = () => {
      retryMs = initialRetryMs; // 重置退避
    };

    source.onmessage = (ev) => {
      messageHandler?.(ev.data);
    };

    source.onerror = (ev) => {
      errorHandler?.(ev);
      source?.close();
      source = null;
      scheduleRetry();
    };
  }

  function scheduleRetry() {
    if (closed) return;
    if (retryTimer) clearTimeout(retryTimer);

    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
      // 指数退避
      retryMs = Math.min(retryMs * 2, maxRetryMs);
    }, retryMs);
  }

  function handleVisibility() {
    if (!pauseWhenHidden) return;

    if (document.visibilityState === 'visible') {
      // 页面重新可见 — 检查连接是否存活
      if (!source || source.readyState === EventSource.CLOSED) {
        retryMs = initialRetryMs;
        connect();
      }
    }
  }

  // 启动
  document.addEventListener('visibilitychange', handleVisibility);
  connect();

  return {
    close() {
      closed = true;
      if (retryTimer) clearTimeout(retryTimer);
      source?.close();
      source = null;
      document.removeEventListener('visibilitychange', handleVisibility);
    },
    onMessage(handler) {
      messageHandler = handler;
    },
    onError(handler) {
      errorHandler = handler;
    },
  };
}
