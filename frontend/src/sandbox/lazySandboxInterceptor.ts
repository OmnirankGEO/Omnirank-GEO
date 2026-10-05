import type { AxiosInstance, InternalAxiosRequestConfig } from 'axios';
import { isSandboxActive } from './sandboxState';

const SANDBOX_MARKER = '__SANDBOX_MOCK__';
const INSTALLED = new WeakSet<AxiosInstance>();

function loadSandboxInterceptor() {
    return import('./sandboxInterceptor');
}

export async function tryFetchSandboxMockLazy(
    method: string,
    url: string,
    force = false,
    init?: RequestInit,
): Promise<Response | null> {
    if (!force && !isSandboxActive()) return null;
    const { tryFetchSandboxMock } = await loadSandboxInterceptor();
    return await tryFetchSandboxMock(method, url, force, init);
}

/**
 * Synchronously registers a tiny transport gate. The large sandbox fixture is
 * fetched only after the browser has authoritatively entered sandbox mode.
 */
export function installLazySandboxInterceptor(instance: AxiosInstance): void {
    if (INSTALLED.has(instance)) return;
    INSTALLED.add(instance);

    instance.interceptors.request.use(async (config: InternalAxiosRequestConfig) => {
        if (!isSandboxActive()) return config;
        const { interceptSandboxRequest } = await loadSandboxInterceptor();
        return await interceptSandboxRequest(config);
    });

    instance.interceptors.response.use(
        (response) => response,
        async (error: unknown) => {
            const marked = Boolean(
                error
                && typeof error === 'object'
                && (error as Record<string, unknown>)[SANDBOX_MARKER] === true,
            );
            if (!marked) return Promise.reject(error);
            const { resolveSandboxResponse } = await loadSandboxInterceptor();
            return await resolveSandboxResponse(error);
        },
    );
}
