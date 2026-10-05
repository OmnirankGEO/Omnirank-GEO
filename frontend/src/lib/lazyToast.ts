import type { ExternalToast } from 'sonner';

type ToastMessage = string | number;

export const TOASTER_REQUEST_EVENT = 'omnirank-toaster-requested';

function requestToasterHost(): void {
  if (typeof window === 'undefined') return;
  (window as Window & { __OMNIRANK_TOASTER_REQUESTED__?: boolean }).__OMNIRANK_TOASTER_REQUESTED__ = true;
  window.dispatchEvent(new Event(TOASTER_REQUEST_EVENT));
}

async function withToast(
  level: 'message' | 'success' | 'info' | 'error' | 'warning',
  message: ToastMessage,
  options?: ExternalToast,
): Promise<void> {
  requestToasterHost();
  // Let React mount the deferred host before a cached sonner import can emit.
  // Without this yield, the first toast on a route can be lost when the
  // module is already warm but the host has not committed yet.
  if (typeof window !== 'undefined') {
    await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
  }
  const { toast } = await import('sonner');
  if (level === 'message') toast(message, options);
  else toast[level](message, options);
}

export const lazyToast = {
  message: (message: ToastMessage, options?: ExternalToast): void => {
    void withToast('message', message, options);
  },
  success: (message: ToastMessage, options?: ExternalToast): void => {
    void withToast('success', message, options);
  },
  info: (message: ToastMessage, options?: ExternalToast): void => {
    void withToast('info', message, options);
  },
  error: (message: ToastMessage, options?: ExternalToast): void => {
    void withToast('error', message, options);
  },
  warning: (message: ToastMessage, options?: ExternalToast): void => {
    void withToast('warning', message, options);
  },
};
