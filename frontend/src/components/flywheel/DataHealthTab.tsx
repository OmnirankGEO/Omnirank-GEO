// W7 Tab 3 · 数据健康(管道水位图,参考图 05)。主区全人话,裸字段名(last_raw_at/lag_hours/shadow)只留高级。
import { useCallback, useEffect, useState } from 'react';
import { AlertTriangle, ArrowRight, Database, Layers, Radio, Sparkles } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { cn } from '@/lib/utils';
import {
  apiGet,
  apiGetCached,
  fmtDate,
  fmtInt,
  fmtPct,
  lagHuman,
  type BridgeHealth,
  type PanoramaResponse,
  type QueryIntentCoverage,
} from './flywheelApi';

function enc(v: string) {
  return encodeURIComponent(v.trim());
}

type PipelineNode = {
  key: string;
  index: number;
  title: string;
  icon: React.ReactNode;
  value: string;
  latest?: string;
  lagText?: string;
  stale?: boolean;
  statusLabel: string;
  statusTone: 'green' | 'amber' | 'muted';
};

export function DataHealthTab({ industry, onNavigate }: { industry: string; onNavigate: (tab: string) => void }) {
  const [panorama, setPanorama] = useState<PanoramaResponse | null>(null);
  const [bridge, setBridge] = useState<BridgeHealth | null>(null);
  const [coverage, setCoverage] = useState<QueryIntentCoverage | null>(null);

  const load = useCallback(async () => {
    try {
      const [pano, br, cov] = await Promise.all([
        // V7 · 跨 tab 共享端点走缓存(与全景 tab 复用)。
        apiGetCached<PanoramaResponse>(`/flywheel-panorama?industry_key=${enc(industry)}`),
        // [review fix] 端点返回 {status, health:{...}}:必须解包 .health,否则滞后告警恒不触发(假绿)。
        apiGet<{ health?: BridgeHealth }>('/bridge/health').then((res) => res?.health ?? null).catch(() => null),
        apiGet<QueryIntentCoverage>(`/writing/query-intent/coverage?industry_key=${enc(industry)}`).catch(() => null),
      ]);
      setPanorama(pano);
      setBridge(br);
      setCoverage(cov);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '数据健康加载失败');
    }
  }, [industry]);

  useEffect(() => {
    void load();
  }, [load]);

  const nodes = panorama?.nodes || [];
  const val = (key: string) => Number(nodes.find((n) => n.key === key)?.value || 0);
  const pendingTotal = Number(panorama?.pending_total || 0);
  const stale = !!bridge?.stale;

  const pipeline: PipelineNode[] = [
    {
      key: 'collect',
      index: 1,
      title: '调研原始数据',
      icon: <Database className="h-5 w-5" />,
      value: fmtInt(val('collect')),
      latest: fmtDate(bridge?.last_raw_at),
      statusLabel: val('collect') > 0 ? '数据可用' : '暂无数据',
      statusTone: val('collect') > 0 ? 'green' : 'muted',
    },
    {
      key: 'learn',
      index: 2,
      title: '来源信号(自动学到的规律)',
      icon: <Radio className="h-5 w-5" />,
      value: fmtInt(val('learn')),
      latest: fmtDate(bridge?.last_source_signal_at),
      lagText: `滞后 ${lagHuman(bridge?.lag_hours)}`,
      stale,
      statusLabel: stale ? '需要补同步' : '已同步',
      statusTone: stale ? 'amber' : 'green',
    },
    {
      key: 'review',
      index: 3,
      title: '待人工候选',
      icon: <Layers className="h-5 w-5" />,
      value: fmtInt(pendingTotal),
      statusLabel: pendingTotal > 0 ? '待处理' : '无积压',
      statusTone: pendingTotal > 0 ? 'amber' : 'green',
    },
    {
      key: 'apply',
      index: 4,
      title: '已应用到写作/投放',
      icon: <Sparkles className="h-5 w-5" />,
      value: fmtInt(val('apply')),
      statusLabel: val('apply') > 0 ? '已生效' : '暂未接管',
      statusTone: val('apply') > 0 ? 'green' : 'muted',
    },
  ];

  const cov = Number(coverage?.coverage || 0);
  const classified = Number(coverage?.classified_queries || 0);
  const totalQ = Number(coverage?.total_queries || 0);

  return (
    <div className="space-y-4">
      {stale && (
        <div className="flex items-start gap-2 rounded-2xl border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-200">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
          <div>
            <p className="font-semibold">调研数据已更新,但下游同步滞后</p>
            <p className="mt-0.5 text-[13px] text-amber-100/80">
              来源信号滞后 {lagHuman(bridge?.lag_hours)},下游多处指标因此偏低或为 0。请按管道顺序补数据,数据将自动回流。
            </p>
          </div>
        </div>
      )}

      {/* 管道水位图 */}
      <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
        <h3 className="text-base font-semibold">数据管道水位</h3>
        <p className="mt-0.5 text-[12px] text-muted-foreground">调研 → 来源信号 → 候选 → 应用,每一节的最新时间与滞后一目了然。</p>
        <div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {pipeline.map((node, i) => (
            <div key={node.key} className="relative">
              <div className={cn('flex h-full flex-col rounded-xl border p-3.5', node.stale ? 'border-amber-500/40 bg-amber-500/5' : 'border-border/60 bg-background/40')}>
                <div className="flex items-center gap-2">
                  <span className="grid h-7 w-7 place-items-center rounded-full bg-muted/50 text-xs font-bold text-muted-foreground">{node.index}</span>
                  <span className="text-muted-foreground">{node.icon}</span>
                  <span className={cn('ml-auto rounded-md px-2 py-0.5 text-[11px]', node.statusTone === 'green' && 'bg-emerald-500/15 text-emerald-300', node.statusTone === 'amber' && 'bg-amber-500/15 text-amber-300', node.statusTone === 'muted' && 'bg-muted/50 text-muted-foreground')}>
                    {node.statusLabel}
                  </span>
                </div>
                <p className="mt-2 text-sm font-medium">{node.title}</p>
                <p className="mt-1 text-2xl font-semibold text-foreground">{node.value}</p>
                {node.latest && <p className="mt-1 text-[11px] text-muted-foreground">最新时间 {node.latest}</p>}
                {node.lagText && <p className={cn('text-[11px]', node.stale ? 'text-amber-300' : 'text-muted-foreground')}>{node.lagText}</p>}
                {node.stale && (
                  <Button size="sm" variant="outline" className="mt-2 border-amber-500/40 text-amber-300 hover:bg-amber-500/10" onClick={() => onNavigate('advanced')}>
                    去补数据
                  </Button>
                )}
              </div>
              {i < pipeline.length - 1 && (
                <ArrowRight className="absolute -right-2.5 top-1/2 hidden h-4 w-4 -translate-y-1/2 text-muted-foreground xl:block" />
              )}
            </div>
          ))}
        </div>
      </div>

      {/* 问题类型覆盖率 + 人话指标 */}
      <div className="grid gap-4 lg:grid-cols-2">
        <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
          <h3 className="text-base font-semibold">问题类型覆盖率</h3>
          <p className="mt-0.5 text-[12px] text-muted-foreground">搜索问题被打上「问题类型」标签的比例;标签越全,蒸馏越精准。</p>
          {totalQ > 0 ? (
            <div className="mt-4">
              <div className="flex items-end justify-between">
                <span className="text-3xl font-semibold text-emerald-400">{fmtPct(cov)}</span>
                <span className="text-[13px] text-muted-foreground">已分类 {fmtInt(classified)} / 共 {fmtInt(totalQ)} 个问题</span>
              </div>
              <Progress value={Math.min(100, cov * 100)} className="mt-2 h-2.5" />
              {coverage?.backfill_running && <p className="mt-2 text-[12px] text-sky-300">正在后台分类新问题…</p>}
            </div>
          ) : (
            <div className="mt-4 rounded-xl border border-dashed border-border/60 bg-background/30 px-4 py-6 text-center text-[13px] text-muted-foreground">
              数据积累中:还没有可分类的搜索问题。通水后可在「高级」页发起问题类型分类。
            </div>
          )}
        </div>

        <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
          <h3 className="text-base font-semibold">健康速览</h3>
          <p className="mt-0.5 text-[12px] text-muted-foreground">用人话说清楚:数据够不够新、够不够多。</p>
          <div className="mt-3 space-y-2">
            <MetricRow label="采集充足度" value={`${fmtInt(val('collect'))} 条真实调研`} tone={val('collect') > 0 ? 'green' : 'muted'} />
            <MetricRow label="来源信号新鲜度" value={stale ? `滞后 ${lagHuman(bridge?.lag_hours)},需补同步` : '已同步,最新'} tone={stale ? 'amber' : 'green'} />
            <MetricRow label="问题类型覆盖率" value={totalQ > 0 ? fmtPct(cov) : '数据积累中'} tone={cov >= 0.5 ? 'green' : totalQ > 0 ? 'amber' : 'muted'} />
            <MetricRow label="已反哺到写作" value={val('apply') > 0 ? `${fmtInt(val('apply'))} 项已生效` : '暂未接管(须人工确认)'} tone={val('apply') > 0 ? 'green' : 'muted'} />
          </div>
          <p className="mt-3 text-[11px] leading-5 text-muted-foreground">
            说明:修复后数据自动回流,相关看板将同步更新。原始字段名与逐节泵操作在「高级」页。
          </p>
          <Button variant="ghost" size="sm" className="mt-1" onClick={() => onNavigate('advanced')}>
            打开高级(工程)视图 <ArrowRight className="ml-1 h-3.5 w-3.5" />
          </Button>
        </div>
      </div>
    </div>
  );
}

function MetricRow({ label, value, tone }: { label: string; value: string; tone: 'green' | 'amber' | 'muted' }) {
  return (
    <div className="flex items-center justify-between rounded-xl border border-border/60 bg-background/40 px-3 py-2.5">
      <span className="text-[13px] text-muted-foreground">{label}</span>
      <span className={cn('text-sm font-semibold', tone === 'green' && 'text-emerald-400', tone === 'amber' && 'text-amber-400')}>{value}</span>
    </div>
  );
}
