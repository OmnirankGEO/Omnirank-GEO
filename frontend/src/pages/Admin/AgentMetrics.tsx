import { useEffect, useState } from 'react';
import { Activity, RefreshCw } from 'lucide-react';
import { authFetch } from '@/lib/api';

interface AgentMetricsSummary {
  window_hours?: number;
  profile_id?: string | null;
  turn_count?: number;
  tool_call_count?: number;
  tool_call_freq?: Record<string, number>;
  tool_failure_rate?: number;
  avg_cost_points?: number;
  latency_p50_ms?: number;
  latency_p95_ms?: number;
  plan_completion_rate?: number;
  abcd_confirm_rate?: number;
  totals?: Record<string, number>;
  generated_at?: string;
}

export default function AgentMetrics() {
  const [hours, setHours] = useState(24);
  const [profileId, setProfileId] = useState('');
  const [summary, setSummary] = useState<AgentMetricsSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function load(nextHours = hours) {
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ hours: String(nextHours) });
      if (profileId.trim()) params.set('profile_id', profileId.trim());
      const res = await authFetch(`/api/social/admin/agent-metrics?${params.toString()}`);
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json?.detail || `读取失败 (${res.status})`);
      setSummary(json.summary || {});
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load(24);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const tools = Object.entries(summary?.tool_call_freq || {}).sort((a, b) => b[1] - a[1]).slice(0, 12);

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6">
      <header className="rounded-2xl border bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="grid h-11 w-11 place-items-center rounded-2xl bg-slate-900 text-white">
              <Activity className="h-5 w-5" />
            </span>
            <div>
              <h1 className="text-xl font-semibold text-slate-950">Agent Loop 监控</h1>
              <p className="text-sm text-slate-500">工具频次、失败率、成本、延迟、计划完成率和确认率。</p>
            </div>
          </div>
          <button
            type="button"
            onClick={() => void load()}
            className="inline-flex items-center gap-2 rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white"
          >
            <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            刷新
          </button>
        </div>
      </header>

      <section className="rounded-2xl border bg-white p-4 shadow-sm">
        <div className="grid gap-3 md:grid-cols-[160px_1fr_auto]">
          <select
            value={hours}
            onChange={(e) => setHours(Number(e.target.value))}
            className="rounded-xl border px-3 py-2 text-sm"
          >
            <option value={24}>近 24 小时</option>
            <option value={168}>近 7 天</option>
            <option value={720}>近 30 天</option>
          </select>
          <input
            value={profileId}
            onChange={(e) => setProfileId(e.target.value)}
            placeholder="可选 profile_id"
            className="rounded-xl border px-3 py-2 text-sm"
          />
          <button
            type="button"
            onClick={() => void load()}
            className="rounded-xl border px-4 py-2 text-sm font-medium text-slate-900"
          >
            应用筛选
          </button>
        </div>
        {error && <div className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      </section>

      <section className="grid gap-3 md:grid-cols-3">
        <Metric label="对话轮次" value={summary?.turn_count ?? 0} />
        <Metric label="工具调用" value={summary?.tool_call_count ?? 0} />
        <Metric label="平均成本" value={`${formatNumber(summary?.avg_cost_points)} 算力`} />
        <Metric label="失败率" value={formatPercent(summary?.tool_failure_rate)} />
        <Metric label="P95 延迟" value={`${summary?.latency_p95_ms ?? 0} ms`} />
        <Metric label="确认率" value={formatPercent(summary?.abcd_confirm_rate)} />
      </section>

      <section className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <div className="rounded-2xl border bg-white p-4 shadow-sm">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-sm font-semibold text-slate-950">工具频次</h2>
            <span className="text-xs text-slate-500">{summary?.generated_at ? formatDate(summary.generated_at) : ''}</span>
          </div>
          <div className="space-y-3">
            {tools.length ? tools.map(([tool, count]) => (
              <div key={tool}>
                <div className="mb-1 flex items-center justify-between text-sm">
                  <span className="font-medium text-slate-800">{tool}</span>
                  <span className="text-slate-500">{count}</span>
                </div>
                <div className="h-2 rounded-full bg-slate-100">
                  <div
                    className="h-2 rounded-full bg-slate-900"
                    style={{ width: `${Math.max(6, Math.min(100, (count / Math.max(1, tools[0][1])) * 100))}%` }}
                  />
                </div>
              </div>
            )) : (
              <div className="rounded-xl bg-slate-50 px-3 py-6 text-center text-sm text-slate-500">暂无工具调用</div>
            )}
          </div>
        </div>

        <div className="rounded-2xl border bg-white p-4 shadow-sm">
          <h2 className="text-sm font-semibold text-slate-950">质量指标</h2>
          <dl className="mt-3 space-y-3 text-sm">
            <MetricRow label="P50 延迟" value={`${summary?.latency_p50_ms ?? 0} ms`} />
            <MetricRow label="计划完成率" value={formatPercent(summary?.plan_completion_rate)} />
            <MetricRow label="总成本" value={`${summary?.totals?.cost_points ?? 0} 算力`} />
            <MetricRow label="确认请求" value={summary?.totals?.confirmation_requested ?? 0} />
            <MetricRow label="确认完成" value={summary?.totals?.confirmation_confirmed ?? 0} />
          </dl>
        </div>
      </section>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="rounded-2xl border bg-white p-4 shadow-sm">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-2 text-2xl font-semibold text-slate-950">{value}</div>
    </div>
  );
}

function MetricRow({ label, value }: { label: string; value: number | string }) {
  return (
    <div className="flex items-center justify-between border-b pb-2 last:border-0">
      <dt className="text-slate-500">{label}</dt>
      <dd className="font-medium text-slate-900">{value}</dd>
    </div>
  );
}

function formatPercent(value?: number) {
  return `${Math.round((Number(value || 0)) * 100)}%`;
}

function formatNumber(value?: number) {
  return Number(value || 0).toFixed(1);
}

function formatDate(value?: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}
