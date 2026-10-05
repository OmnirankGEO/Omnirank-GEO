/**
 * httpTransport — 生产适配器:真实后端 DTO → PresentationV1
 *
 * 数据源:GET /api/public/report/{id}?st=...(api/share_api.py)
 * 状态契约:
 *  - 200 + status:"success"        → ready(mapDtoToPresentation)
 *  - 200 + status:"not_ready"      → not_ready(CLIENT_REPORT_NOT_READY,结构化)
 *  - 200 + status:"pending"        → not_ready(生成中,文案区分)
 *  - 404(无效/token 不符/withheld) → invalid_link
 *  - 其他 HTTP/网络/解析异常        → error(只给人话,不露内部异常)
 * 本适配器不含任何 fixture 回退。
 */
import type { PublicReportResponseBody } from '../contract/dto';
import { mapDtoToPresentation } from './mapDto';
import {
    PUBLIC_MESSAGES,
    type PublicReportLoadInput,
    type PublicReportLoadResult,
    type PublicReportTransport,
} from './transport';

async function parseBody(response: Response): Promise<PublicReportResponseBody | null> {
    try {
        const body: unknown = await response.json();
        if (typeof body === 'object' && body !== null) return body as PublicReportResponseBody;
        return null;
    } catch {
        return null;
    }
}

export function createHttpTransport(): PublicReportTransport {
    return {
        async loadReport(input: PublicReportLoadInput): Promise<PublicReportLoadResult> {
            const params = new URLSearchParams();
            if (input.shareToken) params.set('st', input.shareToken);
            const qs = params.toString();
            const url = `/api/public/report/${encodeURIComponent(input.reportId)}${qs ? `?${qs}` : ''}`;

            let response: Response;
            try {
                response = await fetch(url, {
                    headers: { Accept: 'application/json' },
                    signal: input.signal ?? null,
                });
            } catch {
                return { kind: 'error', message: PUBLIC_MESSAGES.networkError };
            }

            if (response.status === 404) {
                return { kind: 'invalid_link', message: PUBLIC_MESSAGES.invalidLink };
            }

            if (!response.ok) {
                // 5xx/其他异常:不解析 detail(可能含内部信息),统一人话
                return { kind: 'error', message: PUBLIC_MESSAGES.genericError };
            }

            const body = await parseBody(response);
            if (!body) {
                return { kind: 'error', message: PUBLIC_MESSAGES.genericError };
            }

            if ('status' in body && body.status === 'not_ready') {
                return { kind: 'not_ready', message: PUBLIC_MESSAGES.notReady };
            }

            if ('status' in body && body.status === 'pending') {
                return { kind: 'not_ready', message: '报告正在生成中,请稍后刷新查看。' };
            }

            if ('status' in body && body.status === 'success' && body.report) {
                try {
                    return { kind: 'ready', report: mapDtoToPresentation(body.report) };
                } catch {
                    return { kind: 'error', message: PUBLIC_MESSAGES.genericError };
                }
            }

            // 未知成功形态(后端漂移):不给客户看原始结构
            return { kind: 'error', message: PUBLIC_MESSAGES.genericError };
        },
    };
}
