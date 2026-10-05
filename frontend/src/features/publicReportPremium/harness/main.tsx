/**
 * race harness — 公开报告赛马候选 A 独立演示入口
 *
 *  ⚠️ 本文件是 harness,不是生产接线;fixture 仅在此处进入组件树。
 *  URL:?scenario=<key> 切换场景;&bare=1 隐藏 harness 工具条(截图用)。
 */
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import '../../../index.css';
import '../print.css';
import { PublicReportPage } from '../components/ReportPage';
import { RaceLab } from './RaceLab';
import {
    createFixtureTransport,
    DEFAULT_SCENARIO,
    FIXTURE_SCENARIOS,
} from '../transport/fixtureTransport';
import { createHttpTransport } from '../transport/httpTransport';

function readParams() {
    const params = new URLSearchParams(window.location.search);
    const requestedScenario = params.get('scenario') ?? DEFAULT_SCENARIO;
    const scenario = requestedScenario === 'http-probe' || requestedScenario === 'race-lab'
        ? requestedScenario
        : FIXTURE_SCENARIOS[requestedScenario]
            ? requestedScenario
            : DEFAULT_SCENARIO;
    const bare = params.get('bare') === '1';
    return {
        scenario,
        bare,
    };
}

function HarnessBar({ current }: { current: string }) {
    return (
        <div
            data-testid="harness-bar"
            className="fixed bottom-3 left-3 z-50 max-h-[60vh] w-56 overflow-y-auto rounded-lg border border-slate-300 bg-white/95 p-2 shadow-lg print:hidden"
        >
            <p className="px-1 pb-1 text-[10px] font-semibold uppercase tracking-wide text-slate-400">
                Race Harness · 场景
            </p>
            <ul className="space-y-0.5">
                {Object.entries(FIXTURE_SCENARIOS).map(([key, scenario]) => (
                    <li key={key}>
                        <a
                            href={`?scenario=${key}`}
                            data-scenario={key}
                            className={`block rounded px-2 py-1 text-xs ${
                                key === current
                                    ? 'bg-sky-100 font-semibold text-sky-800'
                                    : 'text-slate-600 hover:bg-slate-100'
                            }`}
                        >
                            {scenario.label}
                        </a>
                    </li>
                ))}
            </ul>
        </div>
    );
}

function HarnessApp() {
    const { scenario, bare } = readParams();
    if (scenario === 'race-lab') {
        return <RaceLab />;
    }
    // fixture harness 使用占位数字 id(仅通过 ReportPage 的格式校验,不渲染、不上报)
    const transport = scenario === 'http-probe'
        ? createHttpTransport()
        : createFixtureTransport(scenario);
    return (
        <>
            <PublicReportPage transport={transport} reportId="1" shareToken={null} />
            {!bare && <HarnessBar current={scenario} />}
        </>
    );
}

const container = document.getElementById('root');
if (!container) {
    throw new Error('race harness: #root missing');
}

createRoot(container).render(
    <StrictMode>
        <HarnessApp />
    </StrictMode>,
);
