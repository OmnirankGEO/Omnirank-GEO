import { useEffect, useMemo, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { AlertCircle, ArrowLeft, Cpu, ReceiptText, RefreshCw, Workflow } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { taskStatusLabel } from '@/lib/v35Terminology';

interface ToolCallRow {
  id?: number;
  tool_name?: string;
  status?: string;
  error?: string;
  latency_ms?: number;
  cost_points?: number;
  charged_points?: number;
  refunded_points?: number;
  billing_status?: string;
  created_at?: string;
}

interface AgentTracePayload {
  turn_id?: string;
  user_id?: number;
  profile_id?: string;
  summary?: Record<string, number>;
  llm_decision_path?: {
    model?: string;
    used_tools?: string[];
    tool_call_count?: number;
    tool_error_count?: number;
    cost_points?: number;
    plan_completed?: boolean;
    confirmation_requested?: boolean;
    confirmation_confirmed?: boolean;
  };
  plan_state?: {
    plan_id?: string;
    current_step?: number;
    status?: string;
    user_intent_summary?: string;
    state_json?: { steps?: Array<Record<string, unknown>> };
  } | null;
  tool_calls?: ToolCallRow[];
  metrics?: Array<Record<string, unknown>>;
}

export default function AgentTrace() {
  const { turnId = '' } = useParams();
  const [trace, setTrace] = useState<AgentTracePayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function load() {
    if (!turnId) return;
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams({ turn_id: turnId });
      const res = await authFetch(`/api/social/admin/agent-trace?${params.toString()}`);
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json?.detail || `读取失败 (${res.status})`);
      setTrace(json.trace || {});
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [turnId]);

  const tools = useMemo(() => trace?.llm_decision_path?.used_tools || [], [trace]);
  const planSteps = trace?.plan_state?.state_json?.steps || [];

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6">
      <header className="rounded-2xl border bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <span className="grid h-11 w-11 place-items-center rounded-2xl bg-slate-950 text-white">
              <Workflow className="h-5 w-5" />
            </span>
            <div>
              <div className="mb-1 text-xs text-slate-500">turn_id · {turnId}</div>
              <h1 className="text-xl font-semibold text-slate-950">Agent Trace</h1>
              <p className="text-sm text-slate-500">回看本轮工具调用、计划状态、模型决策和算力消耗，不展示 raw args / raw prompt。</p>
            </div>
          </div>
          <div className="flex gap-2">
            <Link
              to="/admin/agent-audit"
              className="inline-flex items-center gap-2 rounded-xl border px-4 py-2 text-sm font-medium text-slate-800"
            >
              <ArrowLeft className="h-4 w-4" />
              审计列表
            </Link>
            <button
              type="button"
              onClick={() => void load()}
              className="inline-flex items-center gap-2 rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              刷新
            </button>
          </div>
        </div>
      </header>

      {error && (
        <div className="flex items-center gap-2 rounded-2xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">
          <AlertCircle className="h-4 w-4" />
          {error}
        </div>
      )}

      <section className="grid gap-3 md:grid-cols-4">
        <Metric label="工具调用" value={trace?.summary?.tool_call_count ?? 0} />
        <Metric label="错误数" value={trace?.summary?.error_count ?? 0} />
        <Metric label="算力消耗" value={trace?.summary?.charged_points ?? 0} />
        <Metric label="净消耗" value={trace?.summary?.net_points ?? 0} />
      </section>

      <section className="grid gap-4 lg:grid-cols-[1.1fr_0.9fr]">
        <div className="rounded-2xl border bg-white p-5 shadow-sm">
          <div className="mb-4 flex items-center gap-2">
            <Cpu className="h-5 w-5 text-slate-600" />
            <h2 className="text-base font-semibold text-slate-950">模型决策</h2>
          </div>
          <dl className="grid gap-3 text-sm md:grid-cols-2">
            <Info label="模型" value={trace?.llm_decision_path?.model || '-'} />
            <Info label="工具数量" value={trace?.llm_decision_path?.tool_call_count ?? 0} />
            <Info label="工具错误" value={trace?.llm_decision_path?.tool_error_count ?? 0} />
            <Info label="本轮成本" value={`${trace?.llm_decision_path?.cost_points ?? 0} 算力`} />
            <Info label="计划完成" value={trace?.llm_decision_path?.plan_completed ? '是' : '否'} />
            <Info label="需要确认" value={trace?.llm_decision_path?.confirmation_requested ? '是' : '否'} />
          </dl>
          <div className="mt-4 flex flex-wrap gap-2">
            {tools.length ? tools.map((tool) => (
              <span key={tool} className="rounded-full bg-slate-100 px-3 py-1 text-xs font-medium text-slate-700">{tool}</span>
            )) : (
              <span className="text-sm text-slate-500">本轮未调用工具</span>
            )}
          </div>
        </div>

        <div className="rounded-2xl border bg-white p-5 shadow-sm">
          <div className="mb-4 flex items-center gap-2">
            <Workflow className="h-5 w-5 text-slate-600" />
            <h2 className="text-base font-semibold text-slate-950">计划状态</h2>
          </div>
          {trace?.plan_state ? (
            <div className="space-y-3 text-sm">
              <Info label="状态" value={trace.plan_state.status || '-'} />
              <Info label="当前步骤" value={trace.plan_state.current_step ?? 0} />
              <Info label="摘要" value={trace.plan_state.user_intent_summary || '-'} />
              <div className="rounded-xl bg-slate-50 p-3">
                <div className="mb-2 text-xs font-medium text-slate-500">步骤</div>
                <ol className="space-y-2">
                  {planSteps.length ? planSteps.map((step, idx) => (
                    <li key={idx} className="rounded-lg bg-white px-3 py-2 text-slate-700">
                      {idx + 1}. {String(step.title || step.name || step.summary || '未命名步骤')}
                    </li>
                  )) : <li className="text-slate-500">暂无步骤明细</li>}
                </ol>
              </div>
            </div>
          ) : (
            <div className="rounded-xl bg-slate-50 px-3 py-6 text-center text-sm text-slate-500">本轮没有持久化计划</div>
          )}
        </div>
      </section>

      <section className="overflow-hidden rounded-2xl border bg-white shadow-sm">
        <div className="flex items-center gap-2 border-b px-5 py-4">
          <ReceiptText className="h-5 w-5 text-slate-600" />
          <h2 className="text-base font-semibold text-slate-950">工具调用</h2>
        </div>
        <table className="min-w-full text-left text-sm">
          <thead className="bg-slate-50 text-slate-500">
            <tr>
              <th className="px-4 py-3">工具</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3">耗时</th>
              <th className="px-4 py-3">算力消耗</th>
              <th className="px-4 py-3">时间</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {loading ? (
              <tr><td className="px-4 py-4 text-slate-500" colSpan={5}>正在读取 trace...</td></tr>
            ) : (trace?.tool_calls || []).length === 0 ? (
              <tr><td className="px-4 py-4 text-slate-500" colSpan={5}>暂无工具调用</td></tr>
            ) : (trace?.tool_calls || []).map((row, idx) => (
              <tr key={row.id || idx}>
                <td className="px-4 py-3 font-medium text-slate-900">{row.tool_name}</td>
                <td className="px-4 py-3">
                  <div className={row.status === 'ok' ? 'text-emerald-700' : 'text-red-700'}>{taskStatusLabel(row.status)}</div>
                  {row.error && <div className="mt-1 max-w-[280px] truncate text-xs text-slate-500">{row.error}</div>}
                </td>
                <td className="px-4 py-3 text-slate-600">{row.latency_ms ?? 0} ms</td>
                <td className="px-4 py-3 text-slate-600">{row.charged_points ?? 0} / {row.refunded_points ?? 0}</td>
                <td className="px-4 py-3 text-slate-500">{formatDate(row.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
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

function Info({ label, value }: { label: string; value: number | string }) {
  return (
    <div>
      <dt className="text-xs text-slate-500">{label}</dt>
      <dd className="mt-1 break-words font-medium text-slate-900">{value}</dd>
    </div>
  );
}

function formatDate(value?: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}
