/**
 * FlowFunnelCard · B2.3 (CTO-15.9 session 3 · 2026-04-25)
 *
 * 9 步主链漏斗图 · 消费 GET /api/dashboard/flow-funnel
 *
 * 用途:
 *   - AdminDashboard:scope='global' 看全平台
 *   - 代理 Dashboard:scope='agent' 看自己客户
 *
 * 数据源:pipeline_stage_log 7 埋点(diagnosis/quote/pay/write/publish/monitor/report)
 * Codex 0424 9 节点缩到 7 实际埋点 + 衍生 confirm/keyword 2 衍生节点
 */
import { useEffect, useState } from 'react';
import { authFetch } from '@/lib/api';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Loader2, TrendingDown, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';

interface Stage {
  name: string;
  label: string;
  count: number;
  ratio: number; // 相对第一步 diagnosis 的比例 0-1
}

interface FunnelResp {
  success: boolean;
  days: number;
  scope: string;
  stages: Stage[];
  summary: {
    diagnosis_count: number;
    pay_count: number;
    publish_count: number;
    pay_ratio: number;
    publish_ratio: number;
  };
}

interface Props {
  /** agent=自己 · global=admin 全局 */
  scope?: 'agent' | 'global';
  /** 时间窗(默认 90 天) */
  days?: number;
}

const STAGE_COLORS: Record<string, string> = {
  diagnosis: 'bg-sky-500/70',
  quote: 'bg-indigo-500/70',
  pay: 'bg-emerald-500/70',
  write: 'bg-amber-500/70',
  publish: 'bg-orange-500/70',
  monitor: 'bg-violet-500/70',
  report: 'bg-rose-500/70',
};

export function FlowFunnelCard({ scope = 'agent', days: initialDays = 90 }: Props) {
  const [data, setData] = useState<FunnelResp | null>(null);
  const [loading, setLoading] = useState(true);
  const [days, setDays] = useState(initialDays);

  const load = () => {
    setLoading(true);
    authFetch(`/api/dashboard/flow-funnel?days=${days}&scope=${scope}`)
      .then(r => r.json())
      .then(d => {
        if (d?.success) setData(d);
      })
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); /* eslint-disable-next-line react-hooks/exhaustive-deps */ }, [days, scope]);

  if (loading && !data) {
    return (
      <Card>
        <CardContent className="p-6 flex items-center gap-2 text-xs text-muted-foreground">
          <Loader2 className="h-3.5 w-3.5 animate-spin" /> 加载 9 步漏斗...
        </CardContent>
      </Card>
    );
  }

  if (!data) {
    return (
      <Card>
        <CardContent className="p-6 text-xs text-muted-foreground">
          无法加载漏斗数据(可能 pipeline_stage_log 表未建 · 等 Deploy-CTO 部署)
        </CardContent>
      </Card>
    );
  }

  const baseCount = data.stages[0]?.count || 0;

  return (
    <Card>
      <CardHeader className="pb-3 flex-row items-center justify-between">
        <div>
          <CardTitle className="text-sm flex items-center gap-2">
            <TrendingDown className="h-4 w-4 text-brand" />
            9 步交付漏斗 · {scope === 'global' ? '全平台' : '我的客户'}
          </CardTitle>
          <p className="text-[11px] text-muted-foreground mt-0.5">
            近 {data.days} 天 · 基线 {baseCount} 个诊断 · 支付率 {(data.summary.pay_ratio * 100).toFixed(1)}%
            · 发布率 {(data.summary.publish_ratio * 100).toFixed(1)}%
          </p>
        </div>
        <div className="flex items-center gap-2">
          <select
            value={days}
            onChange={e => setDays(parseInt(e.target.value, 10))}
            className="text-xs px-2 py-1 rounded border border-border bg-card"
          >
            {[7, 30, 90, 180, 365].map(d => <option key={d} value={d}>近 {d} 天</option>)}
          </select>
          <Button size="sm" variant="outline" onClick={load} disabled={loading}>
            {loading ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          </Button>
        </div>
      </CardHeader>
      <CardContent>
        {data.stages.length === 0 ? (
          <p className="text-xs text-muted-foreground py-2">本期无 stage_log 事件</p>
        ) : (
          <div className="space-y-1.5">
            {data.stages.map((s) => {
              const widthPct = baseCount > 0 ? (s.count / baseCount) * 100 : 0;
              const colorClass = STAGE_COLORS[s.name] || 'bg-muted';
              return (
                <div key={s.name} className="flex items-center gap-2 text-xs">
                  <span className="w-16 shrink-0 text-foreground/80">{s.label}</span>
                  <div className="flex-1 h-6 bg-muted/30 rounded relative overflow-hidden">
                    <div
                      className={`absolute inset-y-0 left-0 ${colorClass} transition-all`}
                      style={{ width: `${widthPct}%` }}
                    />
                    <div className="absolute inset-0 flex items-center justify-end px-2 text-[11px] font-medium text-foreground tabular-nums">
                      {s.count}
                    </div>
                  </div>
                  <span className="w-12 text-right shrink-0 text-muted-foreground tabular-nums">
                    {(s.ratio * 100).toFixed(1)}%
                  </span>
                </div>
              );
            })}
          </div>
        )}
        <p className="text-[10px] text-muted-foreground mt-3 leading-relaxed">
          * 数据源:pipeline_stage_log(7 埋点)· 比例 = 该步事件数 / 诊断事件数 · 跌幅大的环节是优化重点
        </p>
        <p className="text-[10px] text-amber-600/80 mt-1 leading-relaxed">
          ⚠ publish/monitor 仅覆盖手动发布事实源 + SSE 主路径 + 发布后 24h 自动监测;发布通道代发自动同步尚未打通埋点,实际转化数 ≥ 此处显示
        </p>
      </CardContent>
    </Card>
  );
}

export default FlowFunnelCard;
