import { useEffect } from 'react';

/**
 * Shows browser "unsaved changes" warning when user tries to leave/refresh.
 * @param hasUnsaved - Whether there are unsaved changes
 */
export function useUnsavedWarning(hasUnsaved: boolean) {
  useEffect(() => {
    if (!hasUnsaved) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [hasUnsaved]);
}
