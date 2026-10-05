import { useState } from 'react';
import { Search, ShieldCheck } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { taskStatusLabel } from '@/lib/v35Terminology';

interface AuditRow {
  id: number;
  turn_id?: string;
  profile_id?: string;
  tool_name: string;
  status: string;
  error?: string;
  latency_ms?: number;
  cost_points?: number;
  charged_points?: number;
  refunded_points?: number;
  billing_status?: string;
  created_at?: string;
}

export default function AgentAudit() {
  const [profileId, setProfileId] = useState('');
  const [turnId, setTurnId] = useState('');
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [summary, setSummary] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  async function search() {
    setLoading(true);
    setError('');
    try {
      const params = new URLSearchParams();
      if (turnId.trim()) params.set('turn_id', turnId.trim());
      else if (profileId.trim()) params.set('profile_id', profileId.trim());
      else throw new Error('请输入 turn_id 或 profile_id');
      const res = await authFetch(`/api/social/admin/agent-audit?${params.toString()}`);
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json?.detail || `读取失败 (${res.status})`);
      setRows(json.rows || []);
      setSummary(json.summary || {});
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mx-auto max-w-6xl space-y-5 p-6">
      <header className="rounded-2xl border bg-white p-5 shadow-sm">
        <div className="flex items-center gap-3">
          <span className="grid h-11 w-11 place-items-center rounded-2xl bg-slate-900 text-white">
            <ShieldCheck className="h-5 w-5" />
          </span>
          <div>
            <h1 className="text-xl font-semibold text-slate-950">Agent 工具审计</h1>
            <p className="text-sm text-slate-500">按 turn 或客户资料查看工具调用、耗时、算力消耗与退回。</p>
          </div>
        </div>
      </header>

      <section className="rounded-2xl border bg-white p-4 shadow-sm">
        <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto]">
          <input
            value={turnId}
            onChange={(e) => setTurnId(e.target.value)}
            placeholder="turn_id"
            className="rounded-xl border px-3 py-2 text-sm"
          />
          <input
            value={profileId}
            onChange={(e) => setProfileId(e.target.value)}
            placeholder="profile_id"
            className="rounded-xl border px-3 py-2 text-sm"
          />
          <button
            type="button"
            onClick={() => void search()}
            className="inline-flex items-center justify-center gap-2 rounded-xl bg-slate-950 px-4 py-2 text-sm font-medium text-white"
          >
            <Search className="h-4 w-4" />
            查询
          </button>
        </div>
        {error && <div className="mt-3 rounded-xl bg-red-50 px-3 py-2 text-sm text-red-700">{error}</div>}
      </section>

      <section className="grid gap-3 md:grid-cols-4">
        <Metric label="调用数" value={summary.count ?? rows.length} />
        <Metric label="算力消耗" value={summary.charged_points ?? 0} />
        <Metric label="退回" value={summary.refunded_points ?? 0} />
        <Metric label="错误数" value={summary.error_count ?? 0} />
      </section>

      <section className="overflow-hidden rounded-2xl border bg-white shadow-sm">
        <table className="min-w-full text-left text-sm">
          <thead className="bg-slate-50 text-slate-500">
            <tr>
              <th className="px-4 py-3">工具</th>
              <th className="px-4 py-3">状态</th>
              <th className="px-4 py-3">耗时</th>
              <th className="px-4 py-3">扣/退</th>
              <th className="px-4 py-3">时间</th>
            </tr>
          </thead>
          <tbody className="divide-y">
            {loading ? (
              <tr><td className="px-4 py-4 text-slate-500" colSpan={5}>正在查询...</td></tr>
            ) : rows.length === 0 ? (
              <tr><td className="px-4 py-4 text-slate-500" colSpan={5}>暂无记录</td></tr>
            ) : rows.map((row) => (
              <tr key={row.id}>
                <td className="px-4 py-3 font-medium text-slate-900">{row.tool_name}</td>
                <td className="px-4 py-3">
                  <div className={row.status === 'ok' ? 'text-emerald-700' : 'text-red-700'}>{taskStatusLabel(row.status)}</div>
                  {row.error && <div className="mt-1 max-w-[320px] truncate text-xs text-slate-500">{row.error}</div>}
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

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-2xl border bg-white p-4 shadow-sm">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-2 text-2xl font-semibold text-slate-950">{value}</div>
    </div>
  );
}

function formatDate(value?: string) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString('zh-CN', { hour12: false });
}
