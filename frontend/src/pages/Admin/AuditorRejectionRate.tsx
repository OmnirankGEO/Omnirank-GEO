/**
 * AuditorRejectionRate · A.4 (CTO-15.9 session 3 · 2026-04-25)
 *
 * /admin/auditor 路由 · 行业 auditor 建议拒绝率监督看板
 *
 * 数据源:audit_logs · action='auditor_suggestion'
 * 拒绝率 > 40% 的行业 → 触发 M1b prompt 调优 / industry_median seed 校准
 *
 * 关联 endpoint:
 * - GET /api/admin/auditor/rejection-rate?industry=X&days=N&min_samples=M
 */
import { useEffect, useState, useCallback } from 'react';
import { authFetch } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Loader2, RefreshCw, AlertTriangle, CheckCircle2, TrendingUp, Wrench } from 'lucide-react';

interface IndustryRow {
  industry: string;
  total: number;
  accepted: number;
  rejected: number;
  rejection_rate: number;
  avg_deviation_pct: number;
  level_breakdown: Record<string, number>;
  needs_tuning: boolean;
}

interface ApiResp {
  window_days: number;
  min_samples: number;
  rows: IndustryRow[];
  summary: {
    industries_total: number;
    industries_needing_tuning: number;
    global_rejection_rate: number;
    global_total_decisions: number;
  };
}

// C2.3 (CTO-15.9 session 3 · 2026-04-25) · prompt 调优建议
interface TuningSuggestion {
  scope: 'global' | 'industry';
  industry?: string;
  severity: 'high' | 'medium' | 'low';
  issue: string;
  recommendation: string;
  evidence: Record<string, number>;
}
interface TuningResp {
  window_days: number;
  threshold: number;
  min_samples: number;
  total_decisions: number;
  suggestions_count: number;
  suggestions: TuningSuggestion[];
}

const WINDOW_OPTIONS = [7, 30, 90];
const MIN_SAMPLE_OPTIONS = [1, 3, 5, 10];

export default function AuditorRejectionRate() {
  const [data, setData] = useState<ApiResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(30);
  const [minSamples, setMinSamples] = useState(5);
  // C2.3 · prompt 调优建议
  const [tuning, setTuning] = useState<TuningResp | null>(null);
  const [tuningLoading, setTuningLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    authFetch(
      `/api/admin/auditor/rejection-rate?days=${days}&min_samples=${minSamples}`,
    )
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [days, minSamples]);

  const loadTuning = useCallback(() => {
    setTuningLoading(true);
    authFetch(
      `/api/admin/auditor/tuning-suggestions?days=${days}&min_samples=${minSamples}`,
    )
      .then((r) => r.json())
      .then(setTuning)
      .catch(() => { /* 静默失败 · 不影响主表 */ })
      .finally(() => setTuningLoading(false));
  }, [days, minSamples]);

  useEffect(() => { load(); }, [load]);

  return (
    <div className="p-6 space-y-6 max-w-6xl mx-auto">
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div>
          <h1 className="text-xl font-semibold flex items-center gap-2">
            <TrendingUp className="h-5 w-5 text-brand" />
            auditor 拒绝率监督看板
          </h1>
          <p className="text-xs text-muted-foreground mt-1">
            行业中位审计 · 服务方采纳/拒绝事件聚合 · 拒绝率 &gt; 40% 触发 prompt 调优
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={days}
            onChange={(e) => setDays(parseInt(e.target.value, 10))}
            className="text-xs px-2 py-1 rounded border border-border bg-card"
          >
            {WINDOW_OPTIONS.map((d) => (
              <option key={d} value={d}>近 {d} 天</option>
            ))}
          </select>
          <select
            value={minSamples}
            onChange={(e) => setMinSamples(parseInt(e.target.value, 10))}
            className="text-xs px-2 py-1 rounded border border-border bg-card"
          >
            {MIN_SAMPLE_OPTIONS.map((m) => (
              <option key={m} value={m}>最小样本 {m}</option>
            ))}
          </select>
          <Button size="sm" variant="outline" onClick={load} disabled={loading}>
            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1" /> : <RefreshCw className="h-3.5 w-3.5 mr-1" />}
            刷新
          </Button>
        </div>
      </div>

      {error && (
        <div className="p-3 rounded border border-rose-500/40 bg-rose-500/5 text-xs text-rose-600">
          加载失败: {error}
        </div>
      )}

      {data && (
        <div className="grid grid-cols-1 sm:grid-cols-4 gap-3">
          <Card>
            <CardContent className="p-4">
              <p className="text-xs text-muted-foreground">总决策数</p>
              <p className="text-2xl font-semibold mt-1 tabular-nums">
                {data.summary.global_total_decisions}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <p className="text-xs text-muted-foreground">总体拒绝率</p>
              <p className="text-2xl font-semibold mt-1 tabular-nums">
                {(data.summary.global_rejection_rate * 100).toFixed(1)}%
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="p-4">
              <p className="text-xs text-muted-foreground">行业数(达样本)</p>
              <p className="text-2xl font-semibold mt-1 tabular-nums">
                {data.summary.industries_total}
              </p>
            </CardContent>
          </Card>
          <Card className={data.summary.industries_needing_tuning > 0 ? 'border-amber-500/40 bg-amber-500/5' : ''}>
            <CardContent className="p-4">
              <p className="text-xs text-muted-foreground">需调优行业</p>
              <p className="text-2xl font-semibold mt-1 tabular-nums text-amber-600">
                {data.summary.industries_needing_tuning}
              </p>
            </CardContent>
          </Card>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle className="text-sm">行业明细</CardTitle>
        </CardHeader>
        <CardContent>
          {loading && !data && (
            <div className="flex items-center gap-2 text-xs text-muted-foreground py-6 justify-center">
              <Loader2 className="h-4 w-4 animate-spin" /> 加载中...
            </div>
          )}
          {data && data.rows.length === 0 && (
            <p className="text-xs text-muted-foreground py-6 text-center">
              本时间窗内无达样本阈值的行业(min_samples={minSamples})
            </p>
          )}
          {data && data.rows.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-xs">
                <thead>
                  <tr className="border-b border-border text-muted-foreground">
                    <th className="text-left py-2 pr-3 font-normal">行业</th>
                    <th className="text-right py-2 px-3 font-normal">总决策</th>
                    <th className="text-right py-2 px-3 font-normal">采纳</th>
                    <th className="text-right py-2 px-3 font-normal">拒绝</th>
                    <th className="text-right py-2 px-3 font-normal">拒绝率</th>
                    <th className="text-right py-2 px-3 font-normal">平均偏差</th>
                    <th className="text-left py-2 pl-3 font-normal">level 分布</th>
                    <th className="text-center py-2 pl-3 font-normal">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {data.rows.map((r) => (
                    <tr
                      key={r.industry}
                      className={`border-b border-border/50 ${r.needs_tuning ? 'bg-amber-500/5' : ''}`}
                    >
                      <td className="py-2 pr-3 font-medium">{r.industry}</td>
                      <td className="py-2 px-3 text-right tabular-nums">{r.total}</td>
                      <td className="py-2 px-3 text-right tabular-nums text-emerald-600">{r.accepted}</td>
                      <td className="py-2 px-3 text-right tabular-nums text-rose-600">{r.rejected}</td>
                      <td className={`py-2 px-3 text-right tabular-nums font-semibold ${r.needs_tuning ? 'text-amber-600' : ''}`}>
                        {(r.rejection_rate * 100).toFixed(1)}%
                      </td>
                      <td className="py-2 px-3 text-right tabular-nums">
                        {r.avg_deviation_pct > 0 ? '+' : ''}{r.avg_deviation_pct}%
                      </td>
                      <td className="py-2 pl-3">
                        <div className="flex gap-1 flex-wrap">
                          {Object.entries(r.level_breakdown)
                            .filter(([, v]) => v > 0)
                            .map(([k, v]) => (
                              <Badge
                                key={k}
                                variant="secondary"
                                className={`text-[10px] font-normal ${
                                  k === 'danger' ? 'bg-rose-500/10 text-rose-600' :
                                  k === 'warn' ? 'bg-amber-500/10 text-amber-600' :
                                  k === 'normal' ? 'bg-emerald-500/10 text-emerald-600' :
                                  ''
                                }`}
                              >
                                {k} {v}
                              </Badge>
                            ))}
                        </div>
                      </td>
                      <td className="py-2 pl-3 text-center">
                        {r.needs_tuning ? (
                          <span className="inline-flex items-center gap-1 text-amber-600">
                            <AlertTriangle className="h-3.5 w-3.5" />
                            需调优
                          </span>
                        ) : (
                          <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500 mx-auto" />
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      {/* C2.3 (CTO-15.9 session 3 · M2 §A.4 后续) prompt 调优反馈循环 */}
      <Card>
        <CardHeader className="flex-row items-center justify-between">
          <CardTitle className="text-sm flex items-center gap-2">
            <Wrench className="h-4 w-4 text-brand" />
            prompt 调优建议(自动产出)
          </CardTitle>
          <Button size="sm" variant="outline" onClick={loadTuning} disabled={tuningLoading}>
            {tuningLoading ? <Loader2 className="h-3 w-3 animate-spin mr-1" /> : <Wrench className="h-3 w-3 mr-1" />}
            生成建议
          </Button>
        </CardHeader>
        <CardContent>
          {!tuning && (
            <p className="text-xs text-muted-foreground py-2">
              点右上"生成建议"基于当前时间窗(近 {days} 天)产出针对性 prompt/seed 调优建议
            </p>
          )}
          {tuning && tuning.suggestions.length === 0 && (
            <p className="text-xs text-emerald-600 py-2 flex items-center gap-2">
              <CheckCircle2 className="h-3.5 w-3.5" />
              当前无需调优 · 总决策 {tuning.total_decisions} · 阈值 {(tuning.threshold * 100).toFixed(0)}%
            </p>
          )}
          {tuning && tuning.suggestions.length > 0 && (
            <div className="space-y-3">
              {tuning.suggestions.map((s, i) => (
                <div
                  key={i}
                  className={`p-3 rounded border ${
                    s.severity === 'high' ? 'border-rose-500/40 bg-rose-500/5' :
                    s.severity === 'medium' ? 'border-amber-500/40 bg-amber-500/5' :
                    'border-sky-500/30 bg-sky-500/5'
                  }`}
                >
                  <div className="flex items-center gap-2 mb-1">
                    <Badge
                      variant="outline"
                      className={`text-[10px] ${
                        s.severity === 'high' ? 'border-rose-500/50 text-rose-600' :
                        s.severity === 'medium' ? 'border-amber-500/50 text-amber-600' :
                        'border-sky-500/50 text-sky-600'
                      }`}
                    >
                      {s.severity === 'high' ? '高优' : s.severity === 'medium' ? '中优' : '低优'}
                    </Badge>
                    <Badge variant="secondary" className="text-[10px]">
                      {s.scope === 'global' ? '全局' : `行业 · ${s.industry}`}
                    </Badge>
                  </div>
                  <p className="text-sm font-medium text-foreground mb-1">{s.issue}</p>
                  <p className="text-xs text-muted-foreground leading-relaxed">{s.recommendation}</p>
                  {s.evidence && Object.keys(s.evidence).length > 0 && (
                    <p className="text-[10px] text-muted-foreground/80 mt-2 font-mono">
                      证据: {Object.entries(s.evidence).map(([k, v]) => `${k}=${v}`).join(' · ')}
                    </p>
                  )}
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      <p className="text-[11px] text-muted-foreground leading-relaxed">
        * 数据源:audit_logs.action='auditor_suggestion' · 由服务方在报价 banner 上点 "采纳/拒绝" 写入 ·
        拒绝率 &gt; 40% 表示 industry_median seed 与服务方实际定价偏离过大 · 需要校准 seed 或调优 prompt(M1b §M3)
      </p>
    </div>
  );
}
