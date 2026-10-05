/**
 * GEO 统一观测飞轮 vNext · Frontend-A 候选 —— 可注册页面入口。
 * 最终集成者只需从这里引入并接线 App.tsx / 侧栏 / 诊断报告，不改本目录内部实现。
 */

export { MonitoringObservationWorkbench } from './pages/MonitoringObservationWorkbench';
export type { MonitoringObservationWorkbenchProps } from './pages/MonitoringObservationWorkbench';

export { DiagnosisRecommendationBehavior } from './pages/DiagnosisRecommendationBehavior';
export type { DiagnosisRecommendationBehaviorProps } from './pages/DiagnosisRecommendationBehavior';

export { AdminObservationCenter } from './pages/AdminObservationCenter';
export type { AdminObservationCenterProps } from './pages/AdminObservationCenter';

export { MethodologyPage } from './pages/MethodologyPage';
export type { MethodologyPageProps } from './pages/MethodologyPage';

// 供集成/测试注入自定义 transport（默认走真实 HTTP）
export { createObservationClient, observationClient } from './client';
export type { ObservationClient } from './client';
export type { ObservationTransport } from './transport';
