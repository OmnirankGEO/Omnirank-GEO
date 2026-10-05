/**
 * publicReportPremium — 公开 GEO 诊断报告 · 赛马候选 A
 *
 * 生产接线只从这里导入(最终集成说明见 docs/race/public-report-premium-race-a/):
 *   import { PublicReportPage, createHttpTransport } from '@/features/publicReportPremium';
 *
 * ⚠️ fixtures / fixtureTransport 不从此导出,仅供 harness 与 Playwright;
 *    本入口保持 tree-shake 干净,生产 bundle 不含 fixture。
 */
import './print.css';

export { PublicReportPage } from './components/ReportPage';
export type { PublicReportPageProps } from './components/ReportPage';
export { createHttpTransport } from './transport/httpTransport';
export type {
    PublicReportTransport,
    PublicReportLoadInput,
    PublicReportLoadResult,
} from './transport/transport';
export type {
    PublicReportPresentationV1,
    PublicReportPageState,
} from './contract/types';
