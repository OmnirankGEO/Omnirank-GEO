// W7 Tab 2 · 反哺成果(图表化证据区,参考图 02 + 03)。C1-C6 全真实数据,空态「数据积累中」绝不造假。
import { useCallback, useEffect, useState } from 'react';
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { LineChart as LineIcon, TrendingUp } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { EvolutionBoard } from './EvolutionBoard';
import {
  apiGetCached,
  fmtInt,
  fmtPct,
  type AnswerAdoptionSummary,
  type ArticleStructureResponse,
  type FeatureLift,
  type FlywheelInsightResponse,
  type OutcomeMeasure,
  type OutcomeSummaryResponse,
  type TrendsResponse,
} from './flywheelApi';

const AXIS_COLOR = '#94a3b8';
const GRID_COLOR = 'rgba(148,163,184,0.15)';
const TOOLTIP_STYLE = {
  background: 'rgba(17,24,39,0.95)',
  border: '1px solid rgba(148,163,184,0.3)',
  borderRadius: 8,
  fontSize: 12,
  color: '#e2e8f0',
} as const;

function enc(v: string) {
  return encodeURIComponent(v.trim());
}

function ChartCard({
  title,
  subtitle,
  badge,
  children,
  note,
}: {
  title: string;
  subtitle: string;
  badge?: string;
  children: React.ReactNode;
  note?: string;
}) {
  return (
    <div className="flex flex-col rounded-2xl border border-border/60 bg-card/60 p-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h4 className="text-sm font-semibold">{title}</h4>
          <p className="mt-0.5 text-[11px] leading-4 text-muted-foreground">{subtitle}</p>
        </div>
        {badge && <span className="shrink-0 rounded-md bg-muted/50 px-2 py-0.5 text-[11px] text-muted-foreground">{badge}</span>}
      </div>
      <div className="mt-3 min-h-[200px] flex-1">{children}</div>
      {note && <p className="mt-2 text-[11px] leading-4 text-muted-foreground">{note}</p>}
    </div>
  );
}

function EmptyChart({ message, cta }: { message: string; cta?: { label: string; onClick: () => void } }) {
  return (
    <div className="flex h-[200px] flex-col items-center justify-center gap-3 rounded-lg border border-dashed border-border/60 bg-background/30 px-4 text-center text-[13px] text-muted-foreground">
      <p>{message}</p>
      {cta && (
        <Button variant="outline" size="sm" onClick={cta.onClick}>
          {cta.label}
        </Button>
      )}
    </div>
  );
}

export function FeedbackTab({ industry, onNavigate }: { industry: string; onNavigate?: (tab: string) => void }) {
  const [lift, setLift] = useState<FeatureLift[]>([]);
  const [trends, setTrends] = useState<TrendsResponse | null>(null);
  const [engines, setEngines] = useState<AnswerAdoptionSummary | null>(null);
  const [measures, setMeasures] = useState<OutcomeMeasure[]>([]);
  const [insight, setInsight] = useState<FlywheelInsightResponse | null>(null);

  const load = useCallback(async () => {
    try {
      // V7 · 全部走带缓存的只读 GET(跨 tab 复用 + SWR);outcome 走 outcome-summary(读写分离,页面加载零 POST)。
      const [structRes, trendRes, engineRes, outcomeRes, insightRes] = await Promise.all([
        apiGetCached<ArticleStructureResponse>(`/writing/article-structure/analyze?industry_key=${enc(industry)}&limit=300&min_chars=500`).catch(() => null),
        apiGetCached<TrendsResponse>('/flywheel-trends?weeks=12').catch(() => null),
        apiGetCached<AnswerAdoptionSummary>(`/answer-adoption/summary?industry=${enc(industry)}&limit=20`).catch(() => null),
        apiGetCached<OutcomeSummaryResponse>('/writing/outcome-summary?since_days=30&min_age_days=30').catch(() => null),
        apiGetCached<FlywheelInsightResponse>(`/writing/flywheel-insight?industry_key=${enc(industry)}`).catch(() => null),
      ]);
      setLift((structRes?.feature_lift || []).filter((r) => Number(r.lift || 0) > 0).sort((a, b) => Number(b.lift || 0) - Number(a.lift || 0)).slice(0, 8));
      setTrends(trendRes);
      setEngines(engineRes);
      setMeasures(outcomeRes?.measures || []);
      setInsight(insightRes);
      // [review fix] 各请求各自吞错兜 null,全挂时必须报错,不能把故障伪装成「数据积累中」
      if (!structRes && !trendRes && !engineRes && !outcomeRes) {
        toast.error('反哺成果数据加载失败,请稍后重试');
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '反哺成果数据加载失败');
    }
  }, [industry]);

  useEffect(() => {
    void load();
  }, [load]);

  // C1 数据
  const c1Data = lift.map((r) => ({ label: r.label || r.feature || '', lift: Number((Number(r.lift || 0)).toFixed(1)), recommended: !!r.recommended }));

  // C2 数据:可比较的替换效果
  const c2Data = measures
    .filter((m) => m.comparable && !m.insufficient_data && typeof m.new_rate === 'number')
    .map((m) => ({ style: m.style_name || m.style_code, 旧版: Number(((m.old_rate || 0) * 100).toFixed(2)), 新版: Number(((m.new_rate || 0) * 100).toFixed(2)) }));

  // C3 / C4 数据
  const trendSeries = (trends?.series || []).map((p) => ({
    week: p.week.slice(5),
    引用率: Number((p.citation_rate * 100).toFixed(2)),
    累计: p.cumulative,
    采集: p.total,
  }));

  // C5 数据 — 引擎原始码映射为产品人话名(与全站一致:qwen→千问,不露原始码)
  const ENGINE_LABELS: Record<string, string> = { qwen: '千问', deepseek: 'DeepSeek', doubao: '豆包', kimi: 'Kimi' };
  const c5Data = (engines?.by_engine || [])
    .map((row) => ({
      engine: ENGINE_LABELS[(row.engine || '').toLowerCase()] || row.engine || '未知',
      采纳: Number(row.answer_adopted_rows ?? row.answer_adopted_count ?? 0),
      明确引用: Number(row.explicit_cited_rows ?? row.explicit_cited_count ?? 0),
    }))
    .filter((r) => r.采纳 > 0 || r.明确引用 > 0);

  // C6 叙事(best-effort:从真实 lift + 可比较替换事件串)
  const c6Items: Array<{ text: string; tone: 'green' | 'amber' | 'muted' }> = [];
  if (c1Data[0]) c6Items.push({ text: `系统从数据里发现:「${c1Data[0].label}」的文章被 AI 采纳率高 ${c1Data[0].lift} 倍`, tone: 'green' });
  measures.filter((m) => m.comparable).slice(0, 3).forEach((m) => {
    const name = m.style_name || m.style_code; // [review fix] 人话名,不裸吐 style_code
    if (m.insufficient_data) c6Items.push({ text: `「${name}」新模板上线 ${m.age_days ?? '-'} 天 · 进入 30 天回流观察(样本积累中)`, tone: 'muted' });
    else if (m.rollback_suggestion?.suggest_rollback) c6Items.push({ text: `「${name}」新版被引率低于旧版,已亮黄灯建议回滚`, tone: 'amber' });
    else c6Items.push({ text: `「${name}」新版被引率 ${fmtPct(m.new_rate)} 高于旧版 ${fmtPct(m.old_rate)},反哺有效`, tone: 'green' });
  });

  // V5 · C6 优先用飞轮总汇总 insight(后端 LLM 文案);拿不到回退上面的前端拼接。
  const useInsightC6 = !!insight && (!!insight.summary || (insight.insights || []).length > 0);
  // V6 · 空态下一步 CTA(去数据健康看管道)。
  const healthCta = onNavigate ? { label: '去「数据健康」看管道', onClick: () => onNavigate('health') } : undefined;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 rounded-2xl border border-emerald-500/25 bg-emerald-500/5 px-4 py-3 text-sm text-emerald-100">
        <TrendingUp className="h-4 w-4 text-emerald-300" />
        系统正在从 AI 采纳的真实内容中学习规律,并将验证有效的写作升级应用到后续内容中,持续提升 AI 采纳率。
      </div>

      <div className="grid gap-4 xl:grid-cols-3">
        {/* C1 学习成果榜 */}
        <ChartCard title="学习成果榜" subtitle="从采纳文章 vs 未采纳文章中提炼的结构特征提升(倍数越高越关键)" badge="Top 8" note="提升越高,越能显著提高被 AI 采纳的概率。">
          {c1Data.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={c1Data} layout="vertical" margin={{ left: 8, right: 24, top: 4, bottom: 4 }}>
                <CartesianGrid horizontal={false} stroke={GRID_COLOR} />
                <XAxis type="number" tick={{ fill: AXIS_COLOR, fontSize: 11 }} tickFormatter={(v) => `${v}x`} />
                <YAxis type="category" dataKey="label" width={110} tick={{ fill: AXIS_COLOR, fontSize: 11 }} />
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => [`${v} 倍`, '采纳提升']} />
                <Bar dataKey="lift" radius={[0, 4, 4, 0]}>
                  {c1Data.map((d, i) => (
                    <Cell key={i} fill={d.recommended ? '#34d399' : '#64748b'} />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart message="数据积累中:还没有足够的采纳/对照样本形成结构规律。通水后自动填充。" cta={healthCta} />
          )}
        </ChartCard>

        {/* C2 进化前后对比 */}
        <ChartCard title="进化前后对比" subtitle="每次模板替换后,新版 vs 旧版的被引率(越高越好)" badge="30 天回流">
          {c2Data.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={c2Data} margin={{ left: 0, right: 8, top: 8, bottom: 4 }}>
                <CartesianGrid vertical={false} stroke={GRID_COLOR} />
                <XAxis dataKey="style" tick={{ fill: AXIS_COLOR, fontSize: 11 }} />
                <YAxis tick={{ fill: AXIS_COLOR, fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => `${v}%`} />
                <Bar dataKey="旧版" fill="#64748b" radius={[4, 4, 0, 0]} />
                <Bar dataKey="新版" fill="#34d399" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart message="数据积累中:首个模板替换上线满 30 天后,这里显示真实被引率对比。当前无发布,诚实留空。" />
          )}
        </ChartCard>

        {/* C3 引用率趋势线 */}
        <ChartCard title="引用率趋势线" subtitle="内容整体被 AI 引用率的按周走势(越高越好)" badge="按周趋势">
          {trendSeries.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <LineChart data={trendSeries} margin={{ left: 0, right: 8, top: 8, bottom: 4 }}>
                <CartesianGrid vertical={false} stroke={GRID_COLOR} />
                <XAxis dataKey="week" tick={{ fill: AXIS_COLOR, fontSize: 11 }} />
                <YAxis tick={{ fill: AXIS_COLOR, fontSize: 11 }} tickFormatter={(v) => `${v}%`} />
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => `${v}%`} />
                <Line type="monotone" dataKey="引用率" stroke="#34d399" strokeWidth={2} dot={{ r: 2 }} />
              </LineChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart message="数据积累中:还没有足够周度采集数据形成趋势。" cta={healthCta} />
          )}
        </ChartCard>

        {/* C4 数据增长曲线 */}
        <ChartCard title="数据增长曲线" subtitle="已分析的 AI 回答累计数(轮子一直在转)" badge="累计">
          {trendSeries.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <AreaChart data={trendSeries} margin={{ left: 0, right: 8, top: 8, bottom: 4 }}>
                <defs>
                  <linearGradient id="fw-cum" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor="#38bdf8" stopOpacity={0.5} />
                    <stop offset="100%" stopColor="#38bdf8" stopOpacity={0.05} />
                  </linearGradient>
                </defs>
                <CartesianGrid vertical={false} stroke={GRID_COLOR} />
                <XAxis dataKey="week" tick={{ fill: AXIS_COLOR, fontSize: 11 }} />
                <YAxis tick={{ fill: AXIS_COLOR, fontSize: 11 }} tickFormatter={(v) => fmtInt(v)} width={48} />
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => fmtInt(v)} />
                <Area type="monotone" dataKey="累计" stroke="#38bdf8" strokeWidth={2} fill="url(#fw-cum)" />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart message="数据积累中:暂无周度累计数据。" cta={healthCta} />
          )}
        </ChartCard>

        {/* C5 引擎覆盖分布 */}
        <ChartCard title="引擎覆盖分布" subtitle="各 AI 引擎中被采纳 / 明确引用的数量" badge="近 30 天" note="部分引擎不返回原生引用标记,按答案正文标记推断;无标记不代表未被使用。">
          {c5Data.length ? (
            <ResponsiveContainer width="100%" height={220}>
              <BarChart data={c5Data} margin={{ left: 0, right: 8, top: 8, bottom: 4 }}>
                <CartesianGrid vertical={false} stroke={GRID_COLOR} />
                <XAxis dataKey="engine" tick={{ fill: AXIS_COLOR, fontSize: 11 }} />
                <YAxis tick={{ fill: AXIS_COLOR, fontSize: 11 }} tickFormatter={(v) => fmtInt(v)} width={48} />
                <Tooltip contentStyle={TOOLTIP_STYLE} formatter={(v: number) => fmtInt(v)} />
                <Bar dataKey="采纳" fill="#34d399" radius={[4, 4, 0, 0]} />
                <Bar dataKey="明确引用" fill="#a78bfa" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart message="数据积累中:暂无按引擎拆分的采纳指标。" cta={healthCta} />
          )}
        </ChartCard>

        {/* C6 反哺时间线 · V5 优先渲染飞轮总汇总 insight(AI 文案) */}
        <ChartCard title="反哺时间线" subtitle="从学习到应用、再到效果回流的关键节点" badge={useInsightC6 ? 'AI 总结' : '叙事'}>
          {useInsightC6 ? (
            <div className="space-y-3">
              {insight?.summary && <p className="text-[13px] leading-6 text-foreground/90">{insight.summary}</p>}
              {(insight?.insights || []).length > 0 && (
                <ul className="space-y-2.5">
                  {(insight?.insights || []).map((s, i) => (
                    <li key={i} className="flex gap-2 text-[13px]">
                      <span className="text-emerald-400">•</span>
                      <span className="text-foreground/90">{s}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : c6Items.length ? (
            <ul className="space-y-2.5">
              {c6Items.map((it, i) => (
                <li key={i} className="flex gap-2 text-[13px]">
                  <span className={it.tone === 'green' ? 'text-emerald-400' : it.tone === 'amber' ? 'text-amber-400' : 'text-muted-foreground'}>
                    {it.tone === 'green' ? '✅' : it.tone === 'amber' ? '🟡' : '•'}
                  </span>
                  <span className="text-foreground/90">{it.text}</span>
                </li>
              ))}
            </ul>
          ) : (
            <div className="flex h-[200px] flex-col items-center justify-center gap-2 text-center text-[13px] text-muted-foreground">
              <LineIcon className="h-6 w-6 opacity-40" />
              数据积累中:学习→采纳→回流的完整故事线将在首次替换后自动生成。
            </div>
          )}
        </ChartCard>
      </div>

      {/* W5 写作进化看板 · V7 measures 下传避免重复 outcome 请求;onNavigate 供空态 CTA */}
      <EvolutionBoard industry={industry} measures={measures} onNavigate={onNavigate} />
    </div>
  );
}
