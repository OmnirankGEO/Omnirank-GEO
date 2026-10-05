/**
 * 预览/验证壳（仅供 Playwright 与本地可视化）。
 * - 不含任何 fixture；页面数据来自真实 HTTP transport，测试时由 Playwright 网络层注入。
 * - 生产集成时不使用本文件；最终集成者把 features/geoObservation 的页面接入 App.tsx。
 * - hash 路由：#/workbench #/workbench-nobrand #/diagnosis #/admin #/methodology
 */

import { useEffect, useState } from 'react';
import {
  MonitoringObservationWorkbench,
  DiagnosisRecommendationBehavior,
  AdminObservationCenter,
  MethodologyPage,
} from '../index';

const DEMO_BRAND_ID = 900001;
const DEMO_INDUSTRY_KEY = 'elevator_service';

type Route = 'workbench' | 'workbench-nobrand' | 'diagnosis' | 'admin' | 'methodology';

const ROUTES: { key: Route; label: string }[] = [
  { key: 'workbench', label: '统一观测（服务商）' },
  { key: 'workbench-nobrand', label: '统一观测（未选客户）' },
  { key: 'diagnosis', label: '诊断 · AI 推荐行为' },
  { key: 'admin', label: '观测治理中心' },
  { key: 'methodology', label: '方法说明' },
];

function currentRoute(): Route {
  const h = window.location.hash.replace(/^#\/?/, '');
  const found = ROUTES.find((r) => r.key === h);
  return found ? found.key : 'workbench';
}

export function PreviewApp() {
  const [route, setRoute] = useState<Route>(currentRoute());
  const [dark, setDark] = useState(true);
  const [navMsg, setNavMsg] = useState<string | null>(null);

  useEffect(() => {
    const onHash = () => setRoute(currentRoute());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark);
  }, [dark]);

  function go(r: Route) {
    window.location.hash = `#/${r}`;
    setRoute(r);
  }

  const onNavigate = (target: string, ctx: Record<string, unknown>) => {
    setNavMsg(`将跳转到${target === 'publish' ? '发布' : '写作'}工作台（上下文：${JSON.stringify(ctx)}）`);
  };

  // Playwright-only integration probe: the production Monitoring page passes
  // its existing insights center through this same fallback slot while the
  // observation product flag/readiness gate is closed.
  const verifyLegacyFallback = new URLSearchParams(window.location.search).has('verify-legacy-fallback');
  const legacyFallback = verifyLegacyFallback ? (
    <section aria-label="现役数据洞察回退">现役数据洞察保持可用</section>
  ) : undefined;

  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="sticky top-0 z-40 border-b border-border bg-card/95 backdrop-blur">
        <div className="mx-auto flex max-w-[1440px] flex-wrap items-center gap-2 px-4 py-2">
          <span className="mr-2 text-xs font-semibold text-muted-foreground">预览壳</span>
          <nav aria-label="预览导航" className="flex flex-wrap gap-1.5">
            {ROUTES.map((r) => (
              <button
                key={r.key}
                type="button"
                data-route={r.key}
                aria-current={route === r.key ? 'page' : undefined}
                onClick={() => go(r.key)}
                className={`min-h-[32px] rounded-lg px-3 py-1.5 text-xs font-medium transition focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40 ${
                  route === r.key ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:bg-muted'
                }`}
              >
                {r.label}
              </button>
            ))}
          </nav>
          <button
            type="button"
            onClick={() => setDark((v) => !v)}
            className="ml-auto min-h-[32px] rounded-lg border border-border bg-card px-3 py-1.5 text-xs text-foreground hover:bg-muted focus:outline-none focus-visible:ring-2 focus-visible:ring-brand/40"
          >
            {dark ? '切到浅色' : '切到深色'}
          </button>
        </div>
      </header>

      {navMsg ? (
        <div
          data-testid="nav-intent"
          role="status"
          className="mx-auto max-w-[1440px] px-4 pt-3"
        >
          <div className="flex items-center justify-between rounded-lg border border-brand/30 bg-brand/5 px-3 py-2 text-xs text-foreground">
            <span>{navMsg}</span>
            <button type="button" onClick={() => setNavMsg(null)} className="text-muted-foreground hover:text-foreground">
              知道了
            </button>
          </div>
        </div>
      ) : null}

      <main className="mx-auto max-w-[1440px] px-4 py-5">
        {route === 'workbench' ? (
          <MonitoringObservationWorkbench
            brandId={DEMO_BRAND_ID}
            onNavigate={onNavigate}
            unavailableFallback={legacyFallback}
          />
        ) : null}
        {route === 'workbench-nobrand' ? (
          <MonitoringObservationWorkbench brandId={null} onNavigate={onNavigate} />
        ) : null}
        {route === 'diagnosis' ? (
          <DiagnosisRecommendationBehavior brandId={DEMO_BRAND_ID} industryKey={DEMO_INDUSTRY_KEY} onNavigate={onNavigate} />
        ) : null}
        {route === 'admin' ? <AdminObservationCenter onNavigate={onNavigate} /> : null}
        {route === 'methodology' ? <MethodologyPage showTechnicalAppendix /> : null}
      </main>
    </div>
  );
}
