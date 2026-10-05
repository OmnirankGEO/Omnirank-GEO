import { useEffect, useState } from 'react';
import { Power, RefreshCw, ShieldAlert } from 'lucide-react';
import { authFetch } from '@/lib/api';

interface AgentLoopControlState {
  enabled: boolean;
  fallback: 'fast_path' | 'dry_run_only' | 'hard_off';
  source?: string;
}

export default function AgentLoopControl() {
  const [state, setState] = useState<AgentLoopControlState>({ enabled: true, fallback: 'fast_path' });
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  async function load() {
    setLoading(true);
    setError('');
    try {
      const res = await authFetch('/api/social/admin/agent-loop-control');
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json?.detail || `读取失败 (${res.status})`);
      setState({
        enabled: Boolean(json.enabled),
        fallback: json.fallback || 'fast_path',
        source: json.source,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : '读取失败');
    } finally {
      setLoading(false);
    }
  }

  async function save(patch: Partial<AgentLoopControlState>) {
    setSaving(true);
    setError('');
    try {
      const res = await authFetch('/api/social/admin/agent-loop-control', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(patch),
      });
      const json = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(json?.detail || `保存失败 (${res.status})`);
      setState({
        enabled: Boolean(json.enabled),
        fallback: json.fallback || state.fallback,
        source: json.source,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存失败');
    } finally {
      setSaving(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const disabled = loading || saving;

  return (
    <div className="mx-auto max-w-4xl space-y-5 p-6">
      <header className="rounded-2xl border bg-white p-5 shadow-sm">
        <div className="flex items-center gap-3">
          <span className="grid h-11 w-11 place-items-center rounded-2xl bg-slate-950 text-white">
            <Power className="h-5 w-5" />
          </span>
          <div>
            <h1 className="text-xl font-semibold text-slate-950">Agent Loop 控制台</h1>
            <p className="text-sm text-slate-500">全员同开关，只用于熔断和回退，不做灰度分流。</p>
          </div>
        </div>
      </header>

      {error && (
        <div className="rounded-xl border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-700">{error}</div>
      )}

      <section className="rounded-2xl border bg-white p-5 shadow-sm">
        <div className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <p className="text-sm text-slate-500">当前状态</p>
            <p className="mt-1 text-2xl font-semibold text-slate-950">{state.enabled ? '开启' : '关闭'}</p>
            <p className="mt-1 text-xs text-slate-400">来源: {state.source || 'unknown'}</p>
          </div>
          <button
            type="button"
            disabled={disabled}
            onClick={() => void save({ enabled: !state.enabled })}
            className={`rounded-xl px-5 py-2 text-sm font-medium text-white ${
              state.enabled ? 'bg-red-600 hover:bg-red-700' : 'bg-slate-950 hover:bg-slate-800'
            } disabled:opacity-60`}
          >
            {state.enabled ? '关闭 Agent Loop' : '开启 Agent Loop'}
          </button>
        </div>
      </section>

      <section className="rounded-2xl border bg-white p-5 shadow-sm">
        <div className="mb-4 flex items-center gap-2">
          <ShieldAlert className="h-5 w-5 text-amber-600" />
          <h2 className="font-semibold text-slate-950">异常兜底</h2>
        </div>
        <div className="grid gap-3 md:grid-cols-3">
          {[
            ['fast_path', '切回旧路径', 'Agent 出错时立即走旧的稳定生成链路。'],
            ['dry_run_only', '只看决策', '用于排查，只让 Agent 判断，不让工具落地。'],
            ['hard_off', '硬停', 'Agent 出错时直接提示稍后重试。'],
          ].map(([value, label, desc]) => (
            <button
              key={value}
              type="button"
              disabled={disabled}
              onClick={() => void save({ fallback: value as AgentLoopControlState['fallback'] })}
              className={`rounded-2xl border p-4 text-left ${
                state.fallback === value ? 'border-slate-950 bg-slate-950 text-white' : 'border-slate-200 bg-white text-slate-900'
              } disabled:opacity-60`}
            >
              <div className="font-medium">{label}</div>
              <div className={`mt-2 text-xs ${state.fallback === value ? 'text-slate-200' : 'text-slate-500'}`}>{desc}</div>
            </button>
          ))}
        </div>
      </section>

      <button
        type="button"
        disabled={disabled}
        onClick={() => void load()}
        className="inline-flex items-center gap-2 rounded-xl border bg-white px-4 py-2 text-sm text-slate-700"
      >
        <RefreshCw className="h-4 w-4" />
        刷新
      </button>
    </div>
  );
}
