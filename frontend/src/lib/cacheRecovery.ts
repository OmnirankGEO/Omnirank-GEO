/**
 * Ask the backend to emit Clear-Site-Data for one explicit same-origin recovery.
 * Normal API responses must never clear the browser HTTP cache.
 */
export async function requestHttpCacheRecovery(): Promise<void> {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3000);
    try {
        await fetch('/api/public/cache-recovery', {
            method: 'GET',
            cache: 'no-store',
            credentials: 'same-origin',
            signal: controller.signal,
            headers: { 'Cache-Control': 'no-cache' },
        });
    } catch {
        // Recovery remains best-effort: a failed control request must not block reload.
    } finally {
        window.clearTimeout(timeout);
    }
}
