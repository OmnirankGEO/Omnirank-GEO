// W5 + V1/V2/V3 · 对比工作台(运营决策唯一深页,参考图 04)。
//  ① AI 评审结论置顶(总分 current→candidate + 置信徽章 + 一句话建议 + 差异摘要 ≤3)
//  ② V2 候选版评语(优势 / 风险 / 一句话建议;缺失回退 diff_summary)
//  ③ 阶段指示器 stepper(未生成→样文待评审→评审中→已评分待决策→已采纳/驳回)
//  ④ 结构 diff 徽章(后端已算 structure_diff:{added,missing})
//  ⑤ 五维评分条(review_summary 有逐维分则渲染,否则回退总分对比)
//  ⑥ 两篇样文并排(左当前/右候选,各自独立滚动 + 结构骨架标签 + 单篇复制 / 导出 md)
//  ⑦ 该文体历史对比切换(style-simulations 列表,含 failed)
//  ⑧ 结构研究证据折叠 + 导出正文（JC3 不冒充引用证明）
//  ⑨ sticky 底栏:备注(必填)+ 采纳/驳回/继续观察(askConfirm 在 board 层)
import { useEffect, useMemo, useState } from 'react';
import { Loader2, ScrollText, ShieldCheck } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Textarea } from '@/components/ui/textarea';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import {
  apiGet,
  downloadTextFile,
  fmtDate,
  fmtInt,
  fmtScore,
  type SimulationDetail,
  type SimulationListRow,
  type StyleSimulationsResponse,
} from './flywheelApi';

// [C11] 五维「有效权重」SSOT:services/writing_style_reviewer.py:35-36
//   _BASE_WEIGHT=0.70 / _ADOPTION_WEIGHT=0.30 × writing/geo_triforce_reviewer.py DIMENSION_WEIGHTS(35/25/25/15)
//   → 发布前质量 35/25/25/15 = 100%；结果拟合权重固定为 0，真实引用只由预注册实验裁决。
const DIM_LABELS: Array<{ key: string; label: string; weight: string }> = [
  { key: 'ai_trust', label: '可信与可核验', weight: '权重 35%' },
  { key: 'platform_compliance', label: '平台合规', weight: '权重 25%' },
  { key: 'brand_value', label: '客户价值与平衡', weight: '权重 25%' },
  { key: 'user_value', label: '真实用户价值', weight: '权重 15%' },
];

const STEPS = ['未生成', '样文已生成待评审', '评审中', '已评分待决策', '已采纳/驳回'];

function enc(v: string) {
  return encodeURIComponent(v.trim());
}

function structureTags(md: string) {
  const text = md || '';
  const headings = (text.match(/^#{1,4}\s/gm) || []).length;
  const hasFaq = /FAQ|常见问题|Q\s*&\s*A|问[:：]/i.test(text);
  const hasData = (text.match(/\d+(?:\.\d+)?%|\d{2,}/g) || []).length >= 4;
  return { headings, hasFaq, hasData, chars: text.length };
}

function TagPill({ ok, children }: { ok: boolean; children: React.ReactNode }) {
  return (
    <span
      className={cn(
        'rounded-md border px-2 py-0.5 text-[11px]',
        ok ? 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300' : 'border-border/60 bg-muted/40 text-muted-foreground',
      )}
    >
      {children} {ok ? '✓' : '✗'}
    </span>
  );
}

function ScoreBar({ value, tone }: { value: number; tone: 'current' | 'candidate' }) {
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-muted/50">
      <div
        className={cn('h-full rounded-full', tone === 'candidate' ? 'bg-emerald-400' : 'bg-violet-400')}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

function historyRowLabel(r: SimulationListRow): string {
  const t = r.created_at ? fmtDate(r.created_at).slice(5) : '';
  const title = r.topic_title ? ` · ${r.topic_title.slice(0, 14)}` : '';
  const v = r.review_summary?.verdict;
  const vlabel = v === 'replace' ? ' · 发布前质量更优' : v === 'keep' ? ' · 当前版质量更优' : v === 'observe' ? ' · 建议观察' : '';
  const failed = r.status && String(r.status).toLowerCase().includes('fail') ? ' · 生成失败' : '';
  return `${t}${title}${vlabel}${failed}`.trim() || `#${r.id}`;
}

export function CompareDialog({
  open,
  simulationId,
  styleName,
  candidateLabel,
  styleCode,
  industry,
  boardState,
  latestSimStatus,
  hasCandidate,
  busy,
  reviewingSimId,
  detailRefreshKey,
  onOpenChange,
  onAdopt,
  onReject,
  onObserve,
  onReview,
  onExportCorpus,
}: {
  open: boolean;
  simulationId: number | null;
  styleName: string;
  candidateLabel?: string;
  styleCode?: string;
  industry?: string;
  boardState?: string;
  latestSimStatus?: string | null;
  hasCandidate: boolean;
  busy: boolean;
  /** [07-05] 父级正在轮询评审结果的 sim id:命中当前查看的 sim → 显示「评审进行中」条。 */
  reviewingSimId?: number | null;
  /** [07-05] 父级轮询到评审完成时 ++:详情自动重拉,结论区无需手动刷新即出现。 */
  detailRefreshKey?: number;
  onOpenChange: (v: boolean) => void;
  onAdopt: (note: string) => void;
  onReject: (note: string) => void;
  onObserve: () => void;
  onReview: () => void;
  onExportCorpus: () => void;
}) {
  // 当前查看的对比 id(默认 = prop;历史下拉可切换)。
  const [selectedSimId, setSelectedSimId] = useState<number | null>(simulationId);
  const [detail, setDetail] = useState<SimulationDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [note, setNote] = useState('');
  const [showEvidence, setShowEvidence] = useState(false);
  const [history, setHistory] = useState<SimulationListRow[]>([]);

  // open / prop 变化时重置 selectedSimId(回到父级传入的最新对比);关闭时清备注/历史。
  useEffect(() => {
    setSelectedSimId(simulationId);
    if (!open) {
      setNote('');
      setError('');
      setHistory([]);
      setDetail(null);
    }
  }, [open, simulationId]);

  // 详情随 selectedSimId 变化重拉(保留 cancelled 守卫防旧请求覆盖新详情)。
  // [07-05] detailRefreshKey:父级轮询到评审完成时 ++ → 自动重拉,结论区免手动刷新。
  useEffect(() => {
    if (!open || !selectedSimId) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError('');
    apiGet<{ simulation: SimulationDetail }>(`/writing/style-simulation/${selectedSimId}`)
      .then((res) => {
        if (!cancelled) setDetail(res.simulation || null);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e.message : '对比样文加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, selectedSimId, detailRefreshKey]);

  // 该文体历史对比列表(V1 新接的 style-simulations,前端原 0 消费)。
  useEffect(() => {
    if (!open || !styleCode) {
      setHistory([]);
      return;
    }
    let cancelled = false;
    apiGet<StyleSimulationsResponse>(
      `/writing/style-simulations?style_code=${enc(styleCode)}&industry_key=${enc(industry || '')}&limit=20`,
    )
      .then((res) => {
        if (!cancelled) setHistory(res.simulations || []);
      })
      .catch(() => {
        if (!cancelled) setHistory([]);
      });
    return () => {
      cancelled = true;
    };
  }, [open, styleCode, industry]);

  const review = detail?.review_summary || {};
  const hasReview = review.verdict != null;
  const curDims = review.current_dims;
  const candDims = review.candidate_dims;
  const hasDims = !!curDims && !!candDims;
  const diff = detail?.structure_diff;
  const hasDiff = !!diff && ((diff.added?.length || 0) > 0 || (diff.missing?.length || 0) > 0);

  const curTags = useMemo(() => structureTags(detail?.current_article || ''), [detail?.current_article]);
  const candTags = useMemo(() => structureTags(detail?.candidate_article || ''), [detail?.candidate_article]);

  const noteValid = note.trim().length >= 6;

  // 置信徽章:双评审一致 / 双评审不一致 / 单评审。
  const confidence = review.dual_model ? (review.reviewer_agreement ? '双评审一致' : '双评审不一致') : '单评审';
  const confidenceOk = !!review.dual_model && !!review.reviewer_agreement;

  // 阶段指示器 stepper(从现有信号推导,不新造状态机)。
  const stepIndex = useMemo(() => {
    const st = String(latestSimStatus || detail?.status || '').toLowerCase();
    if (!detail) return st.includes('generat') ? 1 : 0;
    if (hasReview) return 3; // 已评分 → 待决策
    if (st.includes('review')) return 2;
    return 1; // 样文已生成,待评审
  }, [detail, hasReview, latestSimStatus]);

  const verdictBadge = (() => {
    if (!hasReview) return <Badge variant="secondary">评审待发起</Badge>;
    if (review.verdict === 'replace') return <Badge className="bg-emerald-500 text-white hover:bg-emerald-500">发布前质量更优</Badge>;
    if (review.verdict === 'keep') return <Badge variant="secondary">当前版更优</Badge>;
    return <Badge className="bg-amber-500 text-white hover:bg-amber-500">建议观察</Badge>;
  })();

  const copyText = async (text: string, label: string) => {
    try {
      await navigator.clipboard.writeText(text || '');
      toast.success(`已复制${label}`);
    } catch {
      toast.error('复制失败(浏览器不支持或未授权)');
    }
  };

  const exportMd = (text: string, which: 'current' | 'candidate') => {
    const stamp = new Date().toISOString().slice(0, 10);
    const name = `${styleName || 'style'}_${which === 'candidate' ? '候选版样文' : '当前版样文'}_${stamp}.md`;
    downloadTextFile(name, text || '(空)');
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="flex max-h-[92vh] w-[calc(100%-1.5rem)] max-w-[1180px] flex-col gap-0 p-0">
        {/* 头部 */}
        <div className="flex flex-col gap-2 border-b border-border/60 px-5 py-4 pr-12">
          <div className="flex flex-wrap items-center gap-2">
            <ScrollText className="h-4 w-4 text-emerald-300" />
            <DialogTitle className="text-base font-semibold">
              {styleName} · 当前版 vs {candidateLabel || '候选版'}
            </DialogTitle>
            {verdictBadge}
            <Badge variant="outline" className={cn('border-border/60', confidenceOk ? 'text-emerald-300' : 'text-amber-300')}>
              {confidence}
            </Badge>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-2">
            <p className="text-[13px] text-muted-foreground">
              {detail?.topic_title ? `题目:${detail.topic_title}` : '同一题目、同一演示品牌生成的两篇样文对比'}
              {detail?.demo_brand_name ? ` · 演示品牌:${detail.demo_brand_name}` : ''}
            </p>
            {history.length > 1 && (
              <div className="flex items-center gap-2">
                <span className="text-[11px] text-muted-foreground">该文体历史对比</span>
                <Select value={selectedSimId ? String(selectedSimId) : ''} onValueChange={(v) => setSelectedSimId(Number(v))}>
                  <SelectTrigger className="h-8 w-[248px] text-xs">
                    <SelectValue placeholder="选择一组对比" />
                  </SelectTrigger>
                  <SelectContent>
                    {history.map((r) => (
                      <SelectItem key={r.id} value={String(r.id)} className="text-xs">
                        {historyRowLabel(r)}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
            )}
          </div>
        </div>

        {/* 主体滚动区 */}
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {loading && (
            <div className="flex items-center justify-center gap-2 py-16 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在加载对比样文…
            </div>
          )}
          {!loading && error && (
            <div className="rounded-lg border border-red-500/30 bg-red-500/5 px-4 py-6 text-center text-sm text-red-300">{error}</div>
          )}

          {!loading && !error && detail && (
            <div className="space-y-5">
              {/* 阶段指示器 */}
              <div className="flex flex-wrap items-center gap-x-1.5 gap-y-2">
                {STEPS.map((label, i) => (
                  <div key={label} className="flex items-center gap-1.5">
                    <span
                      className={cn(
                        'rounded-full border px-2 py-0.5 text-[11px]',
                        i < stepIndex && 'border-emerald-500/40 bg-emerald-500/10 text-emerald-300',
                        i === stepIndex && 'border-emerald-400 bg-emerald-500/20 font-medium text-emerald-200',
                        i > stepIndex && 'border-border/60 bg-muted/30 text-muted-foreground',
                      )}
                    >
                      {i < stepIndex ? '✓ ' : `${i + 1} `}
                      {label}
                    </span>
                    {i < STEPS.length - 1 && <span className="text-muted-foreground/40">→</span>}
                  </div>
                ))}
              </div>

              {/* ① 结论置顶 */}
              <div className={cn('rounded-2xl border p-4', review.verdict === 'replace' ? 'border-emerald-500/40 bg-emerald-500/5' : 'border-border/60 bg-card/60')}>
                {hasReview ? (
                  <div className="grid gap-4 lg:grid-cols-[1.1fr_1fr]">
                    <div>
                      <div className="flex items-baseline gap-2">
                        <span className="text-sm text-muted-foreground">AI 评审结论</span>
                        <span className={cn('text-xs', confidenceOk ? 'text-emerald-300' : 'text-amber-300')}>{confidence}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-3">
                        <span className="text-sm text-muted-foreground">总分</span>
                        <span className="text-2xl font-bold text-foreground">{fmtScore(review.avg_current_score)}</span>
                        <span className="text-muted-foreground">→</span>
                        <span className="text-2xl font-bold text-emerald-400">{fmtScore(review.avg_candidate_score)}</span>
                        {typeof review.score_delta === 'number' && (
                          <span className={cn('text-sm', review.score_delta >= 0 ? 'text-emerald-300' : 'text-red-300')}>
                            {review.score_delta >= 0 ? `↑${review.score_delta}` : `↓${Math.abs(review.score_delta)}`}
                          </span>
                        )}
                      </div>
                      {review.one_line_advice && (
                        <p className="mt-2 rounded-md bg-muted/40 px-2.5 py-1.5 text-[13px] font-medium text-foreground">建议:{review.one_line_advice}</p>
                      )}
                      {review.reason && <p className="mt-1 text-xs text-muted-foreground">{review.reason}</p>}
                    </div>
                    <div>
                      <p className="text-sm text-muted-foreground">差异摘要(人话)</p>
                      <ul className="mt-1 space-y-1 text-[13px] text-foreground">
                        {(review.diff_summary || []).slice(0, 3).map((d, i) => (
                          <li key={i} className="flex gap-1.5">
                            <span className="text-emerald-400">•</span>
                            <span>{d}</span>
                          </li>
                        ))}
                        {!(review.diff_summary || []).length && <li className="text-muted-foreground">评审未给出差异摘要。</li>}
                      </ul>
                    </div>
                  </div>
                ) : reviewingSimId != null && reviewingSimId === selectedSimId ? (
                  // [07-05] 评审进行中:父级 8s 轮询详情,verdict 回填后本框自动刷出上方结论区。
                  <div className="flex items-center gap-2 rounded-lg border border-sky-500/30 bg-sky-500/10 px-3 py-2 text-sm text-sky-300">
                    <Loader2 className="h-4 w-4 animate-spin" />
                    双评审进行中(约 1-2 分钟),完成后将自动显示结论,无需刷新。
                  </div>
                ) : (
                  <div className="flex flex-col items-start gap-2">
                    <p className="text-sm text-muted-foreground">这组样文尚未评审。发起后由两个独立评审给出发布前质量评分；双评审一致也只形成候选，不代表 AI 引用效果。</p>
                    <Button size="sm" onClick={onReview} disabled={busy}>
                      {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                      发起评审
                    </Button>
                  </div>
                )}
              </div>

              {/* ② V2 候选版评语(优势 / 风险);缺失回退上方 diff_summary */}
              {hasReview && ((review.strengths || []).length > 0 || (review.risks || []).length > 0) && (
                <div className="grid gap-3 sm:grid-cols-2">
                  {(review.strengths || []).length > 0 && (
                    <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/5 p-3">
                      <p className="text-xs font-medium text-emerald-300">{review.commentary_side === 'current' ? '当前版优势' : review.commentary_side === 'candidate' ? '候选版优势' : '更优版本优势'}</p>
                      <ul className="mt-1.5 space-y-1 text-[13px] text-foreground/90">
                        {(review.strengths || []).map((s, i) => (
                          <li key={i} className="flex gap-1.5">
                            <span className="text-emerald-400">+</span>
                            <span>{s}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                  {(review.risks || []).length > 0 && (
                    <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-3">
                      <p className="text-xs font-medium text-amber-300">{review.commentary_side === 'current' ? '当前版风险 / 注意' : review.commentary_side === 'candidate' ? '候选版风险 / 注意' : '更优版本风险 / 注意'}</p>
                      <ul className="mt-1.5 space-y-1 text-[13px] text-foreground/90">
                        {(review.risks || []).map((r, i) => (
                          <li key={i} className="flex gap-1.5">
                            <span className="text-amber-400">!</span>
                            <span>{r}</span>
                          </li>
                        ))}
                      </ul>
                    </div>
                  )}
                </div>
              )}

              {/* 结构 diff 徽章(后端 structure_diff) */}
              {hasDiff && (
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="text-[11px] text-muted-foreground">结构差异:</span>
                  {(diff?.added || []).map((a) => (
                    <span key={`add-${a}`} className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-300">
                      候选版新增 {a}
                    </span>
                  ))}
                  {(diff?.missing || []).map((m) => (
                    <span key={`miss-${m}`} className="rounded-md border border-border/60 bg-muted/40 px-2 py-0.5 text-[11px] text-muted-foreground">
                      候选版缺失 {m}
                    </span>
                  ))}
                </div>
              )}

              {/* ⑤ 五维 / 总分对比 */}
              {hasDims ? (
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-5">
                  {DIM_LABELS.map((d) => {
                    const cur = Number(curDims?.[d.key] || 0);
                    const cand = Number(candDims?.[d.key] || 0);
                    return (
                      <div key={d.key} className="rounded-lg border border-border/60 bg-card/40 p-3">
                        <div className="flex items-center justify-between text-xs text-muted-foreground">
                          <span className="font-medium text-foreground">{d.label}</span>
                          <span>{d.weight}</span>
                        </div>
                        <div className="mt-2 space-y-1.5">
                          <div className="flex items-center gap-2 text-xs">
                            <span className="w-9 text-muted-foreground">当前</span>
                            <span className="w-6 font-medium">{fmtScore(cur)}</span>
                            <ScoreBar value={cur} tone="current" />
                          </div>
                          <div className="flex items-center gap-2 text-xs">
                            <span className="w-9 text-muted-foreground">候选</span>
                            <span className="w-6 font-medium">{fmtScore(cand)}</span>
                            <ScoreBar value={cand} tone="candidate" />
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              ) : hasReview ? (
                <div className="rounded-lg border border-border/60 bg-card/40 p-3">
                  <p className="mb-2 text-xs text-muted-foreground">评分对比(逐维分本轮未细分,展示加权总分)</p>
                  <div className="space-y-2">
                    <div className="flex items-center gap-2 text-xs">
                      <span className="w-9 text-muted-foreground">当前</span>
                      <span className="w-8 font-medium">{fmtScore(review.avg_current_score)}</span>
                      <ScoreBar value={Number(review.avg_current_score || 0)} tone="current" />
                    </div>
                    <div className="flex items-center gap-2 text-xs">
                      <span className="w-9 text-muted-foreground">候选</span>
                      <span className="w-8 font-medium">{fmtScore(review.avg_candidate_score)}</span>
                      <ScoreBar value={Number(review.avg_candidate_score || 0)} tone="candidate" />
                    </div>
                  </div>
                </div>
              ) : null}

              {/* ⑥ 两篇样文并排 */}
              <div className="grid gap-3 lg:grid-cols-2">
                {[
                  { title: '当前版样文', tags: curTags, body: detail.current_article, tone: 'current' as const },
                  { title: '候选版样文', tags: candTags, body: detail.candidate_article, tone: 'candidate' as const },
                ].map((col) => (
                  <div
                    key={col.title}
                    className={cn(
                      'flex min-h-[220px] flex-col overflow-hidden rounded-xl border',
                      col.tone === 'candidate' ? 'border-emerald-500/40' : 'border-border/60',
                    )}
                  >
                    <div className="flex flex-wrap items-center gap-2 border-b border-border/60 bg-muted/20 px-3 py-2">
                      <span className="text-sm font-medium">{col.title}</span>
                      <span className="ml-auto flex flex-wrap gap-1.5">
                        <TagPill ok={col.tags.headings > 0}>小标题 {col.tags.headings}</TagPill>
                        <TagPill ok={col.tags.hasFaq}>FAQ</TagPill>
                        <TagPill ok={col.tags.hasData}>数据引用</TagPill>
                      </span>
                    </div>
                    <div className="max-h-[46vh] flex-1 overflow-auto px-3 py-3">
                      <pre className="whitespace-pre-wrap break-words font-sans text-[13px] leading-6 text-foreground/90">
                        {col.body || '(空)'}
                      </pre>
                    </div>
                    <div className="flex items-center justify-between gap-2 border-t border-border/60 px-3 py-1.5">
                      <span className="text-[11px] text-muted-foreground">{fmtInt(col.tags.chars)} 字</span>
                      <span className="flex gap-1">
                        <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={() => copyText(col.body, col.title)}>
                          复制
                        </Button>
                        <Button variant="ghost" size="sm" className="h-6 px-2 text-[11px]" onClick={() => exportMd(col.body, col.tone)}>
                          导出 .md
                        </Button>
                      </span>
                    </div>
                  </div>
                ))}
              </div>
              {/* [C10] 默认不显示工程状态;仅当两臂生成上下文确证不一致(user_message_identical===false)时提示,结果仅供参考。 */}
              {detail.user_message_identical === false && (
                <p className="text-[11px] text-amber-300">
                  注意:两篇样文生成条件不完全一致,对比结果仅供参考。
                </p>
              )}

              {/* ⑧ 证据折叠 */}
              <div className="rounded-xl border border-border/60">
                <button
                  type="button"
                  onClick={() => setShowEvidence((v) => !v)}
                  className="flex w-full items-center justify-between px-3 py-2.5 text-sm"
                >
                  <span className="font-medium">结构研究区(JC3 非引用证明)</span>
                  <span className="text-muted-foreground">{showEvidence ? '收起 ▲' : '展开 ▼'}</span>
                </button>
                {showEvidence && (
                  <div className="space-y-2 border-t border-border/60 px-3 py-3 text-[13px] text-muted-foreground">
                    <p>
                      候选版可参考该类公开正文的结构规律；只有带文章快照、精确问题和 provider/model/surface 的 JC5 事件才能证明真实引用。
                    </p>
                    <Button variant="outline" size="sm" onClick={onExportCorpus}>
                      导出这类结构研究正文
                    </Button>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        {/* ⑨ sticky 底栏 */}
        <div className="border-t border-border/60 bg-card/80 px-5 py-3">
          <div className="flex items-start gap-2 text-[11px] text-amber-300">
            <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>启用申请会再次校验预注册实验与真实引用硬门；发布前质量分不能替代结果证据。启用/驳回均需备注并留审计日志。</span>
          </div>
          <div className="mt-2 flex flex-col gap-2 lg:flex-row lg:items-end">
            <Textarea
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="请填写启用申请/驳回备注(必填,≥6 字)"
              aria-invalid={note.trim().length > 0 && !noteValid}
              className={cn(
                'min-h-[44px] flex-1',
                // [C14] 已开始输入但不足 6 字 → amber 边框,配合按钮 title 提示为何不可点。
                note.trim().length > 0 && !noteValid && 'border-amber-500/60 focus-visible:ring-amber-500/40',
              )}
            />
            <div className="flex flex-wrap gap-2">
              <Button variant="outline" onClick={onObserve} disabled={busy}>
                继续观察
              </Button>
              <Button
                variant="outline"
                className="border-red-500/40 text-red-300 hover:bg-red-500/10"
                onClick={() => onReject(note)}
                disabled={busy || !hasCandidate || !noteValid}
                // [C14] 备注不足 6 字时说明为何不可点(禁用按钮 tooltip 兜底,配合备注框 amber 边框)。
                title={!noteValid ? '请先填写至少 6 字备注' : undefined}
              >
                驳回
              </Button>
              <Button
                className="bg-emerald-500 text-white hover:bg-emerald-600"
                onClick={() => onAdopt(note)}
                disabled={busy || !hasCandidate || !noteValid}
                // [C14] 备注不足 6 字时说明为何不可点。
                title={!noteValid ? '请先填写至少 6 字备注' : undefined}
              >
                {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                校验并申请启用
              </Button>
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
