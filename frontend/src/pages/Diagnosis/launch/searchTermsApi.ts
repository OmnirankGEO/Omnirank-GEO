/**
 * 搜索词预填的**取数**(订正三十① · C 的只读端点 `GET /api/diagnosis/keyword-suggestion`)。
 *
 * 语义全在 `searchTermsPrefill.ts`(纯函数、可注毒);这里只负责把 HTTP 结果
 * 翻成那边的 `PrefillState`,不做任何判断。
 *
 * 🔴 失败一律翻成 `unavailable`,**不返回半个结果**:
 *    她屏幕上什么都没看到时,提交体就不带 keywords、交服务端派生;
 *    而只要拿到了(哪怕是空数组),带的就必须是**屏幕上那一份**。
 *    「取到空数组」与「没取到」在框里都是空的,处置却相反 —— 所以在这里就分开。
 *
 * 🔴 404 也算 `unavailable`:C 用 404 而不是 403 表示越权
 *    (403 等于告诉探测者「这个 brand_id 存在,只是不是你的」)。
 *    对页面来说两者后果一样:没有可显示的建议词。
 */
import { authFetch } from '@/lib/api';
import type { PrefillState } from './searchTermsPrefill';

export async function fetchSearchTermSuggestion(
    brandId: number | null | undefined,
    signal?: AbortSignal,
): Promise<PrefillState> {
    if (!brandId) return { kind: 'unavailable' };
    try {
        const res = await authFetch(
            `/api/diagnosis/keyword-suggestion?brand_id=${encodeURIComponent(String(brandId))}`,
            { signal },
        );
        if (!res.ok) return { kind: 'unavailable' };
        const d = await res.json();
        const body = d?.data ?? d;
        if (!body || !Array.isArray(body.keywords) || typeof body.source !== 'string') {
            // 字段缺 ⇒ 当作没拿到。**不猜**:猜出来的"空"会被当成"确实没有词"而带空提交。
            return { kind: 'unavailable' };
        }
        return {
            kind: 'ready',
            terms: body.keywords.filter((k: unknown) => typeof k === 'string' && k.trim()),
            source: body.source,
            aiAugmented: body.aiAugmented === true,
        };
    } catch {
        return { kind: 'unavailable' };
    }
}
