/**
 * fixtureTransport — 赛马/测试适配器
 * ⚠️ 仅 race harness 与 Playwright 使用;生产组件禁止 import,
 *    生产构建产物中不得包含本模块与 fixtures。
 */
import type {
    PublicReportLoadInput,
    PublicReportLoadResult,
    PublicReportTransport,
} from './transport';
import { DEFAULT_SCENARIO, FIXTURE_SCENARIOS } from '../fixtures';

export function createFixtureTransport(scenarioKey: string): PublicReportTransport {
    const scenario = FIXTURE_SCENARIOS[scenarioKey] ?? FIXTURE_SCENARIOS[DEFAULT_SCENARIO];
    return {
        loadReport(_input: PublicReportLoadInput): Promise<PublicReportLoadResult> {
            return new Promise((resolve) => {
                window.setTimeout(() => resolve(scenario.resolve()), scenario.delayMs);
            });
        },
    };
}

export { DEFAULT_SCENARIO, FIXTURE_SCENARIOS };
