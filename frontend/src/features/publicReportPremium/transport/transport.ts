/**
 * Transport adapter — 公开报告数据通道接口
 *
 * 实现：
 *  - httpTransport.ts    生产适配器(真实后端 DTO → PresentationV1)
 *  - fixtureTransport.ts 赛马/测试适配器(仅 harness 与 Playwright 使用，
 *                        生产组件禁止 import,构建产物不得包含 fixture)
 */
import type { PublicReportPresentationV1 } from '../contract/types';

export interface PublicReportLoadInput {
    /** 路由参数 /public/report/:id 的 id(纯数字字符串，校验在调用方) */
    readonly reportId: string;
    /** ?st= 分享 token；严格模式下后端必需 */
    readonly shareToken: string | null;
    /** 取消信号(竞态守卫:发起方在新请求时中止旧请求;实现方可选支持) */
    readonly signal?: AbortSignal;
}

export type PublicReportLoadResult =
    | { readonly kind: 'ready'; readonly report: PublicReportPresentationV1 }
    | { readonly kind: 'not_ready'; readonly message: string }
    | { readonly kind: 'invalid_link'; readonly message: string }
    | { readonly kind: 'error'; readonly message: string };

export interface PublicReportTransport {
    loadReport(input: PublicReportLoadInput): Promise<PublicReportLoadResult>;
}

/** 客户可读的通用文案(不含内部信息) */
export const PUBLIC_MESSAGES = {
    notReady: '客户报告尚未就绪。请稍后刷新，或联系发给你链接的人。',
    invalidLink: '链接已失效。请联系发你链接的人重新发送。',
    networkError: '网络不太稳定，请稍后重试。',
    genericError: '报告暂时打不开，请稍后重试。',
} as const;
