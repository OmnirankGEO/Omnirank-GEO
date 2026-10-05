// W7 Tab 1 · 飞轮全景(镜头第一屏 = 实习生第一屏,参考图 01)。
//  左:飞轮环(FlywheelRing) + 4 健康 chip   右:今日待办(统一审核收件箱)
//  下:今天飞轮学到了什么(feature_lift 人话结论) + 人工闸门声明
import { useCallback, useEffect, useState } from 'react';
import {
  BarChart3,
  BookOpen,
  ChevronDown,
  ChevronUp,
  ClipboardCheck,
  Loader2,
  Pencil,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  Users,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { FlywheelRing } from './FlywheelRing';
import {
  apiGet,
  apiGetCached,
  fmtDate,
  fmtEpoch,
  fmtInt,
  invalidateFlywheelCache,
  lagHuman,
  type ArticleStructureResponse,
  type BridgeHealth,
  type EvolutionBoardResponse,
  type FeatureLift,
  type FlywheelInsightResponse,
  type PanoramaResponse,
} from './flywheelApi';

function enc(v: string) {
  return encodeURIComponent(v.trim());
}

function healthLabel(pct: number): string {
  if (pct >= 80) return '数据充足良好';
  if (pct >= 60) return '基本健康';
  if (pct >= 40) return '数据积累中';
  return '数据不足';
}

export function PanoramaTab({
  industry,
  industryLabel = industry,
  onNavigate,
}: {
  industry: string;
  /** 行业中文展示名(用于口径提示文案);未传时回退为原始值 */
  industryLabel?: string;
  onNavigate: (tab: string) => void;
}) {
  const [panorama, setPanorama] = useState<PanoramaResponse | null>(null);
  const [bridge, setBridge] = useState<BridgeHealth | null>(null);
  const [board, setBoard] = useState<EvolutionBoardResponse | null>(null);
  const [lift, setLift] = useState<FeatureLift[]>([]);
  const [insight, setInsight] = useState<FlywheelInsightResponse | null>(null);
  const [insightLoading, setInsightLoading] = useState(false);
  const [showExplainer, setShowExplainer] = useState(true);

  const load = useCallback(async () => {
    try {
      // V7 · 跨 tab 共享端点走 apiGetCached(命中复用 + SWR);bridge/health 关乎新鲜度,保持实拉。
      const [pano, br, bd, struct, ins] = await Promise.all([
        apiGetCached<PanoramaResponse>(`/flywheel-panorama?industry_key=${enc(industry)}`),
        // [review fix] 端点返回 {status, health:{...}}:必须解包 .health(旧控制台同款写法),
        // 否则 stale/lag 恒空 → 数据断流时全景仍恒绿(假健康)。
        apiGet<{ health?: BridgeHealth }>('/bridge/health').then((res) => res?.health ?? null).catch(() => null),
        apiGetCached<EvolutionBoardResponse>(`/writing/evolution-board?industry_key=${enc(industry)}`).catch(() => null),
        apiGetCached<ArticleStructureResponse>(`/writing/article-structure/analyze?industry_key=${enc(industry)}&limit=300&min_chars=500`).catch(() => null),
        apiGetCached<FlywheelInsightResponse>(`/writing/flywheel-insight?industry_key=${enc(industry)}`).catch(() => null),
      ]);
      setPanorama(pano);
      setBridge(br);
      setBoard(bd);
      setLift((struct?.feature_lift || []).filter((r) => Number(r.lift || 0) > 0).sort((a, b) => Number(b.lift || 0) - Number(a.lift || 0)).slice(0, 3));
      setInsight(ins);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '飞轮全景加载失败');
    }
  }, [industry]);

  useEffect(() => {
    void load();
  }, [load]);

  // V5 · 手动刷新 AI 总结:走 refresh=true(后端重算)+ 清缓存,让下次页面加载读到新版。
  const refreshInsight = useCallback(async () => {
    setInsightLoading(true);
    try {
      const res = await apiGet<FlywheelInsightResponse>(`/writing/flywheel-insight?industry_key=${enc(industry)}&refresh=true`);
      setInsight(res);
      invalidateFlywheelCache(['/writing/flywheel-insight']);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : 'AI 总结刷新失败');
    } finally {
      setInsightLoading(false);
    }
  }, [industry]);

  const nodes = panorama?.nodes || [];
  const reviewNode = nodes.find((n) => n.key === 'review');
  const applyNode = nodes.find((n) => n.key === 'apply');
  const bindingCount = Number(reviewNode?.detail?.binding_candidates || 0);
  const engineWeightCount = Number(reviewNode?.detail?.engine_weight_candidates || 0);
  const appliedCount = Number(applyNode?.value || 0);
  const pendingTotal = Number(panorama?.pending_total || 0);
  const candidateStyles = Number(board?.has_candidate_count || 0);
  const recommendReplace = Number(board?.recommend_replace_count || 0);

  // 健康度:5 环节中在产出数据的比例(review 为运营中环节,始终计入)。
  let healthy = 0;
  if (nodes.length) {
    for (const n of nodes) {
      if (n.key === 'review') healthy += 1;
      else if (Number(n.value) > 0) healthy += 1;
    }
  }
  const healthPct = nodes.length ? Math.round((healthy / 5) * 100) : 0;

  const stale = !!bridge?.stale;
  // [P0 2026-08-16] 后端 fail-soft 吞掉取数失败时,上面 nodes 里全是 0。
  // 「算不出来」和「真的是 0」处置完全相反(前者重试/报警,后者等数据),
  // 事故当天正是这片 0 + 「运行正常」把全站被锁排死伪装成了正常态。
  const degraded = !!panorama?.degraded;

  const navForNode = (key: string) => {
    if (key === 'collect' || key === 'learn') return onNavigate('health');
    return onNavigate('feedback');
  };

  return (
    <div className="space-y-4">
      {/* V5 · AI 总结(飞轮总汇总 insight)· 数字来自 facts,文案来自后端 LLM,前端不编造 */}
      <div className="rounded-2xl border border-emerald-500/25 bg-emerald-500/5 p-4">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            <Sparkles className="h-4 w-4 text-emerald-300" />
            <h3 className="text-base font-semibold">AI 总结</h3>
            {insight?.cached && <span className="rounded-md bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">缓存</span>}
          </div>
          <div className="flex items-center gap-2">
            {insight?.updated_at != null && (
              <span className="text-[11px] text-muted-foreground">上次更新 {fmtEpoch(insight.updated_at)}</span>
            )}
            <Button variant="ghost" size="sm" onClick={refreshInsight} disabled={insightLoading}>
              {insightLoading ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="mr-1.5 h-3.5 w-3.5" />}
              刷新
            </Button>
          </div>
        </div>
        {insight ? (
          <div className="mt-2 space-y-2.5">
            {insight.summary && <p className="text-[13px] leading-6 text-foreground/90">{insight.summary}</p>}
            {insight.data_available !== false && (insight.insights || []).length > 0 && (
              <ul className="space-y-1.5">
                {(insight.insights || []).map((s, i) => (
                  <li key={i} className="flex gap-2 text-[13px]">
                    <span className="text-emerald-400">•</span>
                    <span className="text-foreground/90">{s}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : (
          <p className="mt-2 text-[13px] text-muted-foreground">AI 总结加载中或暂不可用。</p>
        )}
      </div>

      {/* 30 秒看懂 */}
      <div className="rounded-2xl border border-border/60 bg-card/50">
        <button type="button" onClick={() => setShowExplainer((v) => !v)} className="flex w-full items-center justify-between px-4 py-3 text-sm">
          <span className="flex items-center gap-2 font-medium">
            <BookOpen className="h-4 w-4 text-emerald-300" /> 30 秒看懂这个页面
          </span>
          {showExplainer ? <ChevronUp className="h-4 w-4 text-muted-foreground" /> : <ChevronDown className="h-4 w-4 text-muted-foreground" />}
        </button>
        {showExplainer && (
          <div className="border-t border-border/60 px-4 py-3 text-[13px] leading-6 text-muted-foreground">
            <p>① 下面的环 = 系统在自动从调研数据里学写作规律。</p>
            <p>② 右边「今日待办」= 今天需要你人工确认的事(其余系统自动处理)。</p>
            <p>③ 成果和证据在「反哺成果」;数据是否新鲜在「数据健康」。所有线上影响都必须经过人工闸门。</p>
          </div>
        )}
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.05fr_1fr]">
        {/* 左:飞轮环 + 健康 chip */}
        <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
          <div className="flex items-center gap-2">
            <h3 className="text-base font-semibold">飞轮正在学习</h3>
            {degraded ? (
              <span className="flex items-center gap-1 text-xs text-red-300">
                <span className="h-1.5 w-1.5 rounded-full bg-red-400" /> 数据加载失败
              </span>
            ) : stale ? (
              <span className="flex items-center gap-1 text-xs text-amber-300">
                <span className="h-1.5 w-1.5 rounded-full bg-amber-400" /> 数据滞后
              </span>
            ) : (
              <span className="flex items-center gap-1 text-xs text-emerald-300">
                <span className="h-1.5 w-1.5 rounded-full bg-emerald-400" /> 运行正常
              </span>
            )}
          </div>
          <p className="mt-0.5 text-[12px] text-muted-foreground">采集 → 学习 → 人工把关 → 应用 → 效果回流</p>
          {/* [行业口径 2026-08-18] 选了具体行业时,把「哪些环不随行业变」一次说清 ——
              环上每张卡还有各自的「全站」角标(FlywheelRing),这里是页面级的一句话解释。 */}
          {panorama?.industry_scoped && (panorama.site_scope_nodes || []).length > 0 && (
            <p className="mt-1 text-[11px] text-muted-foreground">
              采集 / 学习 / 效果回流已按「{industryLabel}」过滤;
              <span className="text-amber-300">人工把关、应用到写作暂无行业维度,仍是全站口径</span>
              (标「全站」的环)。
            </p>
          )}

          {degraded ? (
            <div className="mt-3 rounded-xl border border-red-500/30 bg-red-500/5 p-3 text-[12px] text-red-200">
              <div className="font-medium">数据加载失败,下面的数字不可信</div>
              <div className="mt-1 text-red-200/80">
                后端取数出错,缺失项被记为 0。这不是“暂时没有数据”,请稍后重试;若持续出现请联系管理员。
              </div>
            </div>
          ) : null}

          <div className="mt-4">
            <FlywheelRing
              nodes={nodes}
              healthPercent={degraded ? 0 : healthPct}
              healthLabel={degraded ? '数据加载失败' : healthLabel(healthPct)}
              onNodeClick={navForNode}
              industryScoped={!!panorama?.industry_scoped}
            />
          </div>

          <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4">
            <HealthChip label="最后调研时间" value={fmtDate(bridge?.last_raw_at).slice(0, 16)} sub={stale ? '需要补同步' : '更新及时'} warn={stale} />
            <HealthChip
              label="来源信号时间"
              value={fmtDate(bridge?.last_source_signal_at).slice(0, 16)}
              sub={`滞后 ${lagHuman(bridge?.lag_hours)}`}
              warn={stale}
            />
            <HealthChip label="人工审核积压" value={`${fmtInt(pendingTotal)} 条`} sub="待处理" warn={pendingTotal > 0} />
            <HealthChip label="线上影响状态" value={`${fmtInt(appliedCount)} 项`} sub={appliedCount > 0 ? '已生效' : '暂不接管'} />
          </div>
        </div>

        {/* 右:今日待办 */}
        <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
          <div className="flex items-center gap-2">
            <ClipboardCheck className="h-4 w-4 text-emerald-300" />
            <h3 className="text-base font-semibold">今日待办</h3>
            <span className="rounded-md bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">人工审核收件箱</span>
          </div>
          <p className="mt-0.5 text-[12px] text-muted-foreground">统一入口,优先处理建议替换和高把握事项。</p>

          <div className="mt-3 space-y-2">
            <InboxRow
              icon={<Users className="h-4 w-4" />}
              title="媒体匹配建议"
              subtitle="高把握候选已生成,等待人工确认后才进入媒体映射。"
              count={`${fmtInt(bindingCount)} 条`}
              actionLabel="去审核"
              onAction={() => onNavigate('advanced')}
              tone={bindingCount > 0 ? 'green' : 'muted'}
            />
            <InboxRow
              icon={<Pencil className="h-4 w-4" />}
              title="写作新候选"
              subtitle={recommendReplace > 0 ? `AI 已完成样文对比与评审,建议先处理 ${recommendReplace} 个「建议替换」文体。` : 'AI 已完成样文对比与评审,点开看两篇样文和评分。'}
              count={`${fmtInt(candidateStyles)} 个文体`}
              actionLabel="看对比"
              onAction={() => onNavigate('feedback')}
              tone={candidateStyles > 0 ? 'green' : 'muted'}
            />
            <InboxRow
              icon={<BarChart3 className="h-4 w-4" />}
              title="引擎重要性建议"
              subtitle="不同引擎的采纳贡献变化较大时,需要确认引擎重要性权重。"
              count={`${fmtInt(engineWeightCount)} 条`}
              actionLabel="查看"
              onAction={() => onNavigate('advanced')}
              tone={engineWeightCount > 0 ? 'green' : 'muted'}
            />
            <InboxRow
              icon={<ShieldCheck className="h-4 w-4" />}
              title="上线闸门"
              subtitle={`当前线上影响 ${appliedCount} 项,系统不会自动改写客户可见推荐,须人工确认。`}
              count={appliedCount > 0 ? '已生效' : '暂不接管'}
              actionLabel="查看状态"
              onAction={() => onNavigate('advanced')}
              tone="amber"
            />
          </div>
          {pendingTotal === 0 && bindingCount === 0 && candidateStyles === 0 && (
            <p className="mt-3 rounded-lg border border-emerald-500/25 bg-emerald-500/5 px-3 py-2 text-center text-[13px] text-emerald-200">今天没有要处理的 ✅</p>
          )}
        </div>
      </div>

      {/* 今天飞轮学到了什么 */}
      <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
        <div className="flex items-center justify-between">
          <div>
            <h3 className="text-base font-semibold">今天飞轮学到了什么</h3>
            <p className="mt-0.5 text-[12px] text-muted-foreground">基于最新采纳数据提炼的可操作结论,帮助优化策略。</p>
          </div>
          <Button variant="ghost" size="sm" onClick={() => onNavigate('feedback')}>查看详情</Button>
        </div>
        {lift.length ? (
          <div className="mt-3 grid gap-3 md:grid-cols-3">
            {lift.map((r, i) => (
              <div key={i} className="rounded-xl border border-border/60 bg-background/40 p-3">
                <h4 className="text-sm font-semibold">{r.label || r.feature}</h4>
                <p className="mt-1 text-[13px] text-muted-foreground">
                  这类文章被 AI 采纳率提升 <span className="font-semibold text-emerald-400">{Number(r.lift || 0).toFixed(1)} 倍</span>。
                </p>
                <span className={cn('mt-2 inline-block rounded-md px-2 py-0.5 text-[11px]', r.recommended ? 'bg-emerald-500/15 text-emerald-300' : 'bg-amber-500/15 text-amber-300')}>
                  {r.recommended ? '高把握' : '中把握'}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <div className="mt-3 rounded-xl border border-dashed border-border/60 bg-background/30 px-4 py-6 text-center text-[13px] text-muted-foreground">
            数据积累中:采纳/对照样本还不够形成结论,通水后自动出现。
          </div>
        )}
      </div>

      {/* 人工闸门声明 */}
      <div className="flex items-start gap-2 rounded-2xl border border-emerald-500/25 bg-emerald-500/5 px-4 py-3 text-[13px] leading-6 text-emerald-100">
        <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-emerald-300" />
        所有写作模板、结构策略和权重建议,必须经过人工审核后才能影响后续新文章;旧文章保持不变,可随时回滚。
      </div>
    </div>
  );
}

function HealthChip({ label, value, sub, warn }: { label: string; value: string; sub?: string; warn?: boolean }) {
  return (
    <div className={cn('rounded-xl border p-2.5', warn ? 'border-amber-500/40 bg-amber-500/5' : 'border-border/60 bg-background/40')}>
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate text-sm font-semibold text-foreground" title={value}>{value}</div>
      {sub && <div className={cn('text-[11px]', warn ? 'text-amber-300' : 'text-muted-foreground')}>{sub}</div>}
    </div>
  );
}

function InboxRow({
  icon,
  title,
  subtitle,
  count,
  actionLabel,
  onAction,
  tone,
}: {
  icon: React.ReactNode;
  title: string;
  subtitle: string;
  count: string;
  actionLabel: string;
  onAction: () => void;
  tone: 'green' | 'amber' | 'muted';
}) {
  return (
    <div className="flex items-center gap-3 rounded-xl border border-border/60 bg-background/40 p-3">
      <span
        className={cn(
          'grid h-9 w-9 shrink-0 place-items-center rounded-lg',
          tone === 'green' && 'bg-emerald-500/15 text-emerald-300',
          tone === 'amber' && 'bg-amber-500/15 text-amber-300',
          tone === 'muted' && 'bg-muted/50 text-muted-foreground',
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">{title}</p>
        <p className="truncate text-[12px] text-muted-foreground">{subtitle}</p>
      </div>
      <span className="shrink-0 text-sm font-semibold">{count}</span>
      <Button size="sm" variant="outline" className="shrink-0" onClick={onAction}>{actionLabel}</Button>
    </div>
  );
}
