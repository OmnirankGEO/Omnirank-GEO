// W5 · 写作进化看板(三层 + 8 态矩阵,参考图 03)。运营唯一触点。
//  第一层 待决策(有候选文体卡,发布前质量更优的排最前)
//  第二层 观察中/无候选(折叠)
//  第三层 进化历史(已替换版本真实效果 · outcome-backfill)
// 采纳/驳回/回滚走既有「写作文体控制台」双闸(不新造发布通道);全部 askConfirm + 强制备注。
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  Download,
  Eye,
  FlaskConical,
  Loader2,
  Sparkles,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Progress } from '@/components/ui/progress';
import { Textarea } from '@/components/ui/textarea';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { cn } from '@/lib/utils';
import { geoIndustryLabel } from '@/lib/geoIndustries';
import { CompareDialog } from './CompareDialog';
import {
  apiGet,
  apiGetCached,
  apiPost,
  candidateHint,
  downloadTextFile,
  fmtInt,
  fmtPct,
  fmtScore,
  invalidateFlywheelCache,
  scGet,
  scPost,
  type CorpusExportResponse,
  type ArticleEvolutionCycle,
  type ArticleEvolutionCyclesResponse,
  type ArticleReviewQueueItem,
  type ArticleReviewQueueResponse,
  type EvolutionBoardResponse,
  type EvolutionCard,
  type OutcomeMeasure,
  type OutcomeSummaryResponse,
} from './flywheelApi';

// V7 · 写动作后需清的共享缓存前缀(否则 apiGetCached 命中旧值 → 采纳后看板假不动)。
const WRITE_INVALIDATE = ['/writing/evolution-board', '/writing/article-evolution-cycles', '/writing/outcome-summary', '/flywheel-panorama', '/writing/flywheel-insight'];

const ADOPT_COPY = '这是启用申请，不是仅凭 AI 评分上线。系统会再次校验预注册实验、直接引用结果、样本覆盖和人工签发；任一硬门不足都会拒绝。';
const CONFIRM_TOKEN = 'ACTIVATE_WRITING_STYLE_VERSION';
const DISTILL_SAMPLE_TARGET = 30;
const DISTILL_CONTROL_TARGET = 10;

function enc(v: string) {
  return encodeURIComponent(v.trim());
}

// [2026-08-01] 阻断原因翻人话(CLAUDE.md:工程术语全站翻译)。未收录的原样透出,
// 不吞 —— 宁可露个生词,也不能让新出现的阻断原因静默消失。
const BLOCKER_LABELS: Record<string, string> = {
  no_monitoring_results: '还没有 AI 监测结果',
  no_strict_article_question_events: '还没有「文章 ↔ 问题」的严格配对记录',
  monitoring_lineage_not_100_percent: '部分监测记录无法溯源到具体提问快照',
  provider_model_surface_below_95_percent: '部分监测记录没记全是哪个引擎/模型答的',
  publication_snapshot_not_100_percent: '部分已发布文章缺发布时的正文快照',
  future_time_rows_present: '存在时间戳落在未来的异常记录',
  publication_state_time_conflicts_present: '存在「已退回但有发布时间」的矛盾记录',
};

function blockerLabel(code: string): string {
  if (BLOCKER_LABELS[code]) return BLOCKER_LABELS[code];
  if (code.startsWith('strict_outcome_error:')) return `严格效果统计出错(${code.split(':')[1]})`;
  if (code.startsWith('data_health_error:')) return `数据健康度探测出错(${code.split(':')[1]})`;
  return code;
}

function sampleProgressLabel(adopted: number) {
  if (adopted >= DISTILL_SAMPLE_TARGET) return `已达标 · ${fmtInt(adopted)} 篇`;
  return `${fmtInt(adopted)}/${DISTILL_SAMPLE_TARGET}`;
}

async function getConfigVersion(): Promise<number> {
  const res = await scGet<{ data?: { config_version?: number } }>('/versions');
  return Number(res?.data?.config_version ?? 0);
}

function buildCorpusMarkdown(items: CorpusExportResponse['items'], scopeLabel: string): string {
  const head = `# GEO 结构研究正文集 · ${scopeLabel}\n\n> 共 ${items.length} 篇 · 导出时间 ${new Date().toLocaleString('zh-CN')}\n> 语料为公开抓取正文,仅含标题 / 来源 URL / 正文。JC3 只可研究结构，只有 JC5 直接血缘才是引用效果证据。\n\n`;
  const body = items
    .map((it, i) => {
      const src = it.source_url ? `来源:${it.source_url}${it.domain ? ` (${it.domain})` : ''}` : it.domain ? `来源:${it.domain}` : '';
      return `## ${i + 1}. ${it.title || '(无标题)'}\n\n${src}\n\n${it.content || ''}\n\n---\n`;
    })
    .join('\n');
  return head + body;
}

function stateBadge(card: EvolutionCard) {
  const s = card.board_state;
  if (s === 'recommend_replace') return <Badge className="bg-emerald-500 text-white hover:bg-emerald-500">发布前质量更优</Badge>;
  if (s === 'failed') return <Badge className="bg-red-500 text-white hover:bg-red-500">生成失败</Badge>;
  if (s === 'generating') return <Badge className="bg-sky-500 text-white hover:bg-sky-500">生成中</Badge>;
  if (s === 'disagree') return <Badge className="bg-amber-500 text-white hover:bg-amber-500">⚪ 意见不一致</Badge>;
  if (s === 'observe') return <Badge className="bg-amber-500 text-white hover:bg-amber-500">建议观察</Badge>;
  if (s === 'keep') return <Badge variant="secondary">当前版更优</Badge>;
  if (s === 'candidate_unreviewed') return <Badge variant="secondary">待评审</Badge>;
  if (s === 'ready_pending_distill') return <Badge className="bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/15">样本已达标</Badge>;
  return <Badge variant="secondary">{card.board_state_label}</Badge>;
}

export function EvolutionBoard({
  industry,
  measures: measuresProp,
  onNavigate,
}: {
  industry: string;
  measures?: OutcomeMeasure[];
  onNavigate?: (tab: string) => void;
}) {
  const [confirmDialog, askConfirm] = useConfirmDialog();
  const [board, setBoard] = useState<EvolutionBoardResponse | null>(null);
  // V7 · 父级(FeedbackTab)已拉 measures 时下传,跳过自身 outcome 请求(消除重复的 dry_run)。
  const [ownMeasures, setOwnMeasures] = useState<OutcomeMeasure[]>([]);
  const hasMeasuresProp = measuresProp !== undefined;
  const measures = measuresProp ?? ownMeasures;
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [showLayer2, setShowLayer2] = useState(false);
  const [cycles, setCycles] = useState<ArticleEvolutionCycle[]>([]);
  const [cycleReviewNote, setCycleReviewNote] = useState('');
  const [reviewQueue, setReviewQueue] = useState<ArticleReviewQueueItem[]>([]);
  const [articleReviewReason, setArticleReviewReason] = useState<Record<number, string>>({});

  // 对比弹窗
  const [compareCard, setCompareCard] = useState<EvolutionCard | null>(null);

  const load = useCallback(async (silent = false) => {
    // [07-05 老板反馈] silent=轮询用:不置 loading 骨架(转圈期间 8s 一刷,骨架会闪瞎)。
    if (!silent) setLoading(true);
    try {
      // V7 · 走带缓存的只读 GET(跨 tab 复用 + SWR);key 含 industry_key,行业切换自然穿透重取。
      const [boardRes, cycleRes, reviewQueueRes] = await Promise.all([
        apiGetCached<EvolutionBoardResponse>(`/writing/evolution-board?industry_key=${enc(industry)}`),
        apiGetCached<ArticleEvolutionCyclesResponse>('/writing/article-evolution-cycles?limit=12').catch(() => null),
        apiGet<ArticleReviewQueueResponse>('/writing/article-review-queue?limit=50').catch(() => null),
      ]);
      setBoard(boardRes);
      setCycles(cycleRes?.items || []);
      setReviewQueue(reviewQueueRes?.items || []);
      // V7 · 页面加载零 POST:outcome 只读走 outcome-summary(读写分离)。父级已下传则跳过。
      if (!hasMeasuresProp && !silent) {
        const outcomeRes = await apiGetCached<OutcomeSummaryResponse>('/writing/outcome-summary?since_days=30&min_age_days=30').catch(() => null);
        setOwnMeasures(outcomeRes?.measures || []);
      }
    } catch (e) {
      if (!silent) toast.error(e instanceof Error ? e.message : '写作进化看板加载失败');
    } finally {
      if (!silent) setLoading(false);
    }
  }, [industry, hasMeasuresProp]);

  useEffect(() => {
    void load();
  }, [load]);

  // ============================================================
  // [07-05 老板反馈「一直转圈圈无法自动刷新」] 后台任务状态轮询,不再要求手动刷新页面。
  // ① 样文生成:看板有 generating 卡 → 8s 失效前端缓存重拉(后端完成/失败瞬间已失效
  //    SCOPE_BOARD 服务缓存,拉到即新态);离开 generating → toast 成功/失败。
  // ② 双评审:看板 8 态无「评审中」(verdict 未回填前仍是 candidate_unreviewed),
  //    故轮询**详情端点**(无缓存)盯 review_summary.verdict 回填;完成 → toast 结论 +
  //    detailRefreshKey++ 让打开着的对比框自动刷出评审结论区。
  // 均带超时保护(防后端任务挂死轮询永转):生成 6 分钟 / 评审 5 分钟。
  // ============================================================
  const generatingRef = useRef<Map<string, string>>(new Map());
  const generatingSinceRef = useRef<number>(0);
  const [reviewingSimId, setReviewingSimId] = useState<number | null>(null);
  const [detailRefreshKey, setDetailRefreshKey] = useState(0);

  useEffect(() => {
    const active = new Map<string, string>();
    (board?.cards || []).forEach((c) => {
      if (c.board_state === 'generating') active.set(c.style_code, c.style_name || c.style_code);
    });
    // 离开 generating 的卡 → 按新态提示(failed = 生成失败;其余 = 已生成待评审)
    generatingRef.current.forEach((name, code) => {
      if (active.has(code)) return;
      const now = (board?.cards || []).find((c) => c.style_code === code);
      if (now?.board_state === 'failed') {
        toast.error(`「${name}」样文生成失败,可点「重试生成」`);
      } else {
        toast.success(`「${name}」对比样文已生成,点开卡片可发起评审`);
      }
    });
    generatingRef.current = active;
    if (active.size === 0) {
      generatingSinceRef.current = 0;
      return;
    }
    if (!generatingSinceRef.current) generatingSinceRef.current = Date.now();
    if (Date.now() - generatingSinceRef.current > 6 * 60_000) {
      toast.warning('样文生成耗时超预期,已暂停自动刷新;稍后可手动刷新查看');
      generatingSinceRef.current = 0;
      return;
    }
    const t = setInterval(() => {
      invalidateFlywheelCache(['/writing/evolution-board']);
      void load(true);
    }, 8000);
    return () => clearInterval(t);
  }, [board, load]);

  useEffect(() => {
    if (!reviewingSimId) return;
    const startedAt = Date.now();
    const t = setInterval(async () => {
      if (Date.now() - startedAt > 5 * 60_000) {
        clearInterval(t);
        setReviewingSimId(null);
        toast.warning('评审耗时超预期,已暂停自动刷新;稍后可手动刷新查看');
        return;
      }
      try {
        const res = await apiGet<{ simulation: { review_summary?: { verdict?: string } } }>(
          `/writing/style-simulation/${reviewingSimId}`,
        );
        const verdict = res?.simulation?.review_summary?.verdict;
        if (!verdict) return;
        clearInterval(t);
        setReviewingSimId(null);
        const label = verdict === 'replace' ? '发布前质量更优，仍待真实效果实验' : verdict === 'keep' ? '当前版质量更优' : '建议继续观察';
        toast.success(`评审完成:${label}`);
        invalidateFlywheelCache(WRITE_INVALIDATE);
        setDetailRefreshKey((k) => k + 1); // 对比框开着 → 自动刷出评审结论区
        void load(true);
      } catch {
        // 轮询失败静默重试(网络抖动不打断)
      }
    }, 8000);
    return () => clearInterval(t);
  }, [reviewingSimId, load]);

  const cards = board?.cards || [];
  const withCandidate = cards.filter((c) => c.candidate_version_id);
  const withoutCandidate = cards.filter((c) => !c.candidate_version_id);
  const observingCount = cards.filter((c) => c.board_state === 'observing' || c.board_state === 'ready_pending_distill').length;
  const styleNameByCode = useMemo(() => {
    const m = new Map<string, string>();
    cards.forEach((c) => m.set(c.style_code, c.style_name));
    return m;
  }, [cards]);
  const trainAdoptedTotal = cards.reduce((sum, c) => sum + Number(c.train_adopted || 0), 0);

  // 上次替换效果(取一条可比较且非样本不足的)
  const lastEffective = measures.find((m) => m.comparable && !m.insufficient_data && typeof m.new_rate === 'number');
  const latestCycle = cycles[0];

  const runEvolutionCycle = useCallback(async () => {
    setBusy(true);
    try {
      await apiPost('/writing/article-evolution-cycle/run?dry_run=false');
      toast.success('本半月 GEO 文章进化审计已生成，等待人工签发');
      invalidateFlywheelCache(['/writing/article-evolution-cycles']);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '生成进化审计失败');
    } finally {
      setBusy(false);
    }
  }, [load]);

  const reviewEvolutionCycle = useCallback(async (decision: 'approved' | 'no_change' | 'rejected') => {
    if (!latestCycle) return;
    if (cycleReviewNote.trim().length < 5) {
      toast.warning('请填写至少 5 个字的审核依据');
      return;
    }
    setBusy(true);
    try {
      await apiPost(`/writing/article-evolution-cycle/${latestCycle.id}/review`, {
        decision,
        note: cycleReviewNote.trim(),
      });
      toast.success(decision === 'no_change' ? '已签发：本轮不变化' : '本轮进化审计已签发');
      setCycleReviewNote('');
      invalidateFlywheelCache(['/writing/article-evolution-cycles']);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '签发失败');
    } finally {
      setBusy(false);
    }
  }, [cycleReviewNote, latestCycle, load]);

  const reviewArticle = useCallback(async (article: ArticleReviewQueueItem, decision: 'approved' | 'rejected') => {
    const reason = (articleReviewReason[article.id] || '').trim();
    if (reason.length < 5) {
      toast.warning('请填写至少 5 个字的文章审核依据');
      return;
    }
    const ok = await askConfirm({
      title: `${decision === 'approved' ? '签发' : '退回'}文章「${article.title || article.id}」?`,
      description: decision === 'approved'
        ? '签发只确认当前正文与当前证据清单；正文变化后签发自动失效。机器硬门不能被人工覆盖。'
        : '退回后文章不能提交发布，修改正文并重新审核后才可再次签发。',
      confirmLabel: decision === 'approved' ? '确认签发' : '确认退回',
      danger: decision === 'rejected',
    });
    if (!ok) return;
    setBusy(true);
    try {
      await apiPost(`/writing/article/${article.id}/human-review`, { decision, reason });
      toast.success(decision === 'approved' ? '文章已由 GEO 文体专家签发' : '文章已退回');
      setArticleReviewReason((old) => ({ ...old, [article.id]: '' }));
      await load(true);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '文章审核失败');
    } finally {
      setBusy(false);
    }
  }, [articleReviewReason, askConfirm, load]);

  // V6 · 候选版人话命名(替换弹窗标题里的裸「候选版」)。
  const compareCandidateLabel = useMemo(() => {
    if (!compareCard) return undefined;
    const hint = candidateHint(compareCard.candidate_created_at, compareCard.candidate_evidence_samples);
    return hint === '—' ? '候选版' : `候选版 · ${hint}`;
  }, [compareCard]);

  const exportCorpus = useCallback(
    async (styleCode?: string) => {
      setBusy(true);
      try {
        const q = `/writing/corpus-export?industry_key=${enc(industry)}${styleCode ? `&style_code=${enc(styleCode)}` : ''}&signal_layer=adopted&limit=50`;
        const res = await apiGet<CorpusExportResponse>(q);
        if (!res.items?.length) {
          toast.warning('暂无可导出的 GEO 结构研究正文(该筛选下语料为空)');
          return;
        }
        const scopeLabel = styleCode ? `${styleNameByCode.get(styleCode) || styleCode} · ${geoIndustryLabel(industry)}` : geoIndustryLabel(industry);
        const md = buildCorpusMarkdown(res.items, scopeLabel);
        const stamp = new Date().toISOString().slice(0, 10);
        downloadTextFile(`GEO结构研究正文集_${styleCode || industry}_${stamp}.md`, md);
        toast.success(`已导出 ${res.items.length} 篇 GEO 结构研究正文`);
      } catch (e) {
        toast.error(e instanceof Error ? e.message : '导出失败');
      } finally {
        setBusy(false);
      }
    },
    [industry, styleNameByCode],
  );

  const adopt = useCallback(
    async (card: EvolutionCard, note: string) => {
      if (note.trim().length < 6) {
        toast.warning('请先填写采纳备注(≥6 字)');
        return;
      }
      const ok = await askConfirm({ title: `申请启用「${card.style_name}」候选版?`, description: ADOPT_COPY, confirmLabel: '校验并申请启用' });
      if (!ok) return;
      setBusy(true);
      try {
        const cv = await getConfigVersion();
        await scPost('/activate', {
          version_id: card.candidate_version_id,
          expected_config_version: cv,
          note,
          confirm: CONFIRM_TOKEN,
        });
        toast.success('真实效果实验与全部硬门已通过，候选版已由人工启用(可随时回滚)');
        setCompareCard(null);
        invalidateFlywheelCache(WRITE_INVALIDATE);
        await load();
      } catch (e) {
        toast.error(`${e instanceof Error ? e.message : '启用申请失败'} · 不得绕过实验与血缘硬门`);
      } finally {
        setBusy(false);
      }
    },
    [askConfirm, load],
  );

  const reject = useCallback(
    async (card: EvolutionCard, note: string) => {
      if (note.trim().length < 6) {
        toast.warning('请先填写驳回原因(≥6 字)');
        return;
      }
      const ok = await askConfirm({ title: `驳回「${card.style_name}」候选版?`, description: '驳回后这版新模板作废,线上不受影响;范文更新后 AI 会再提炼新的。', confirmLabel: '确认驳回', danger: true });
      if (!ok) return;
      setBusy(true);
      try {
        const cv = await getConfigVersion();
        await scPost('/retire', { version_id: card.candidate_version_id, expected_config_version: cv, note });
        toast.success('已驳回候选版');
        setCompareCard(null);
        invalidateFlywheelCache(WRITE_INVALIDATE);
        await load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : '驳回失败');
      } finally {
        setBusy(false);
      }
    },
    [askConfirm, load],
  );

  const generateSimulation = useCallback(
    async (card: EvolutionCard) => {
      // [review fix] 花钱动作加确认;后端互斥返回 in_progress 时不再误报成功
      const ok = await askConfirm({
        title: `生成「${card.style_name}」对比样文?`,
        description: '将真实生成 2 篇文章(当前版 vs 候选版,平台承担 AI 成本),仅存内部对比,不进客户面。',
        confirmLabel: '开始生成',
      });
      if (!ok) return;
      setBusy(true);
      try {
        const res = await apiPost<{ status?: string; message?: string }>('/writing/style-simulation', {
          style_code: card.style_code,
          industry_key: industry,
          version_id: card.candidate_version_id,
          dry_run: false,
        });
        if (res?.status === 'in_progress') {
          toast.info(res.message || '已有一组样文正在生成中,请稍后再试');
          return;
        }
        toast.success('已在后台生成两篇样文,约 2 分钟后刷新可看对比');
        // [收尾] 失效 board+insight 并重载 → 立刻显「生成中」态,不滞后到下个导航周期
        invalidateFlywheelCache(['/writing/evolution-board', '/writing/flywheel-insight']);
        await load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : '生成对比样文失败');
      } finally {
        setBusy(false);
      }
    },
    [askConfirm, industry, load],
  );

  const runReview = useCallback(
    async (simId: number) => {
      // [review fix] 花钱动作加确认;后端互斥返回 in_progress 时不再误报成功
      const ok = await askConfirm({
        title: '发起双评审?',
        description: '两个评审同时独立评估发布前质量(平台承担 AI 成本)。一致只产生质量候选，不代表引用效果，仍需预注册真实实验。',
        confirmLabel: '发起评审',
      });
      if (!ok) return;
      setBusy(true);
      try {
        const res = await apiPost<{ status?: string; message?: string }>(`/writing/style-simulation/${simId}/review`, { dry_run: false });
        if (res?.status === 'in_progress') {
          toast.info(res.message || '该对比正在评审中,请稍后再试');
          setReviewingSimId(simId); // 已在跑也接上轮询,完成同样自动出结论
          return;
        }
        toast.success('双评审已发起,完成后将自动显示结论(约 1-2 分钟)');
        // [07-05] 详情轮询接管:verdict 回填 → 自动 toast + 对比框自动刷出结论,无需手动刷新
        setReviewingSimId(simId);
        invalidateFlywheelCache(['/writing/evolution-board', '/writing/flywheel-insight']);
        await load();
      } catch (e) {
        toast.error(e instanceof Error ? e.message : '发起评审失败');
      } finally {
        setBusy(false);
      }
    },
    [askConfirm, load],
  );

  return (
    <div className="space-y-4">
      {/* 汇总条 */}
      <div className="rounded-2xl border border-border/60 bg-card/60 p-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h3 className="flex items-center gap-2 text-base font-semibold">
              <Sparkles className="h-4 w-4 text-emerald-300" /> 写作进化看板
            </h3>
            <p className="mt-0.5 text-[13px] text-muted-foreground">
              基于 GEO 正文、发布快照与 AI 引用血缘的模板进化 · 结构样本与真实效果分开 · 行业:{geoIndustryLabel(industry)}
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={() => exportCorpus()} disabled={busy}>
            <Download className="mr-2 h-4 w-4" /> 导出 GEO 结构研究正文
          </Button>
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <SummaryStat value={`${board?.has_candidate_count ?? 0} 个`} label="有新候选待决策" hint="AI 仅评发布前质量，不能预测引用" tone="green" />
          <SummaryStat value={`${observingCount} 个`} label="观察/待蒸馏" hint="样本不足、已达标待蒸馏或意见不一致" tone="amber" />
          <SummaryStat
            value={lastEffective ? `${fmtPct(lastEffective.new_rate)} vs ${fmtPct(lastEffective.old_rate)}` : '数据积累中'}
            label="最近严格 GEO 实验"
            hint={lastEffective ? `${lastEffective.effect_decision || 'INCONCLUSIVE'} · ${styleNameByCode.get(lastEffective.style_code) || lastEffective.style_code} · ${lastEffective.age_days ?? '-'} 天` : '等待预注册实验积累直接引用'}
          />
          <SummaryStat value={`${fmtInt(trainAdoptedTotal)} 篇`} label="本轮结构研究样本" hint="JC3 可研究结构；只有 JC5 可进入效果证据" />
        </div>
        {/* [2026-08-01 刷屏修] 阻断原因是页面级单例,这里出一次;卡内只留短标签「数据不可裁决」。 */}
        {board?.data_blocked ? (
          <div className="mt-3 flex items-start gap-2 rounded-xl border border-amber-500/30 bg-amber-500/5 p-3 text-xs text-amber-300">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <div className="min-w-0">
              <div className="font-medium">
                数据不可裁决 · 影响 {board.data_blocked_card_count ?? 0} 张卡
              </div>
              <ul className="mt-1 space-y-0.5 text-amber-200/80">
                {(board.data_blocked_reasons || []).map((reason) => (
                  <li key={reason}>· {blockerLabel(reason)}</li>
                ))}
              </ul>
            </div>
          </div>
        ) : null}
      </div>

      <div className="rounded-2xl border border-sky-500/25 bg-sky-500/5 p-4">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <h4 className="text-sm font-semibold">每月两次 GEO 文章进化审计</h4>
            <p className="mt-1 text-xs text-muted-foreground">
              每月 1 日、16 日冻结 Jina 正文等级、发布快照、真实 AI 监测、文章审核、问题候选和预注册实验；只产出待签发建议，不自动改模板或客户问题。
            </p>
          </div>
          <Button variant="outline" size="sm" onClick={runEvolutionCycle} disabled={busy}>
            <FlaskConical className="mr-2 h-4 w-4" /> 生成本轮审计
          </Button>
        </div>
        {latestCycle ? (
          <div className="mt-3 grid gap-3 lg:grid-cols-[1fr_1.4fr]">
            <div className="rounded-xl border border-border/60 bg-card/60 p-3 text-xs">
              <div className="flex items-center justify-between gap-2">
                <span className="font-medium">周期 {latestCycle.cycle_key}</span>
                <Badge variant={latestCycle.state === 'review_ready' ? 'secondary' : 'outline'}>{latestCycle.state}</Badge>
              </div>
              <div className="mt-2 text-muted-foreground">真相等级 {latestCycle.truth_level || 'unknown'} · 数据状态 {latestCycle.data_health?.decision_status || latestCycle.data_health?.state || 'unknown'}</div>
              <div className="mt-2 space-y-1">
                {(latestCycle.recommendations || []).slice(0, 3).map((item, index) => (
                  <div key={`${item.action}-${index}`}>[{item.priority || 'P2'}] {item.reason || item.action}</div>
                ))}
              </div>
            </div>
            {latestCycle.state === 'review_ready' ? (
              <div className="space-y-2">
                <Textarea value={cycleReviewNote} onChange={e => setCycleReviewNote(e.target.value)} placeholder="写明审核依据：采纳哪些变化、为什么本轮不变化，或为什么退回。" />
                <div className="flex flex-wrap gap-2">
                  <Button size="sm" onClick={() => reviewEvolutionCycle('approved')} disabled={busy}>签发建议</Button>
                  <Button size="sm" variant="outline" onClick={() => reviewEvolutionCycle('no_change')} disabled={busy}>签发：本轮不变化</Button>
                  <Button size="sm" variant="destructive" onClick={() => reviewEvolutionCycle('rejected')} disabled={busy}>退回</Button>
                </div>
                <p className="text-[11px] text-muted-foreground">签发审计也不会直接启用文体；候选仍必须完成模拟、双评审、预注册真实效果实验和单独启用确认。</p>
              </div>
            ) : (
              <div className="rounded-xl border border-border/60 bg-card/40 p-3 text-xs text-muted-foreground">
                已由管理员签发。{latestCycle.review_note ? `依据：${latestCycle.review_note}` : ''}
              </div>
            )}
          </div>
        ) : (
          <div className="mt-3 text-xs text-muted-foreground">尚无进化审计。首次可手动生成；自动任务默认关闭，启用后按每月 1 日、16 日运行。</div>
        )}
      </div>

      <div className="rounded-2xl border border-amber-500/25 bg-amber-500/5 p-4">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h4 className="text-sm font-semibold">GEO 文体专家 · 发布前审核队列</h4>
            <p className="mt-1 text-xs text-muted-foreground">
              审核事实证据、E-E-A-T、问题匹配、客户价值与平台风险。质量分不等于 AI 引用概率；硬门失败不能靠高分或人工按钮覆盖。
            </p>
          </div>
          <Badge variant="secondary">{reviewQueue.length} 篇待处理/留痕</Badge>
        </div>
        {reviewQueue.length ? (
          <div className="mt-3 space-y-2">
            {reviewQueue.slice(0, 10).map((article) => {
              const review = article.article_review || {};
              const canApprove = !['blocked', 'rewrite_required'].includes(article.article_review_status || '');
              return (
                <div key={article.id} className="rounded-xl border border-border/60 bg-card/60 p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{article.title || `文章 #${article.id}`}</div>
                      <div className="mt-1 text-[11px] text-muted-foreground">
                        {article.brand_name || '品牌未知'} · {article.industry || '行业未知'} · 机器结论 {article.article_review_status || 'unknown'} · 质量分 {review.quality_score ?? '—'}
                      </div>
                    </div>
                    <Badge variant={canApprove ? 'secondary' : 'destructive'}>{canApprove ? '待人工判断' : '必须重写'}</Badge>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">{review.explanation || '请核对当前正文、证据清单与商业关系披露。'}</p>
                  <Textarea
                    className="mt-2 min-h-[52px]"
                    value={articleReviewReason[article.id] || ''}
                    onChange={(e) => setArticleReviewReason((old) => ({ ...old, [article.id]: e.target.value }))}
                    placeholder="写明核对了哪些证据、风险和适用边界（必填）"
                  />
                  <div className="mt-2 flex gap-2">
                    <Button size="sm" disabled={busy || !canApprove} onClick={() => reviewArticle(article, 'approved')}>签发当前正文</Button>
                    <Button size="sm" variant="destructive" disabled={busy} onClick={() => reviewArticle(article, 'rejected')}>退回重写</Button>
                  </div>
                </div>
              );
            })}
            {reviewQueue.length > 10 && <div className="text-xs text-muted-foreground">另有 {reviewQueue.length - 10} 篇，请按创建时间继续处理。</div>}
          </div>
        ) : (
          <div className="mt-3 text-xs text-muted-foreground">当前没有需要人工处理的文章。</div>
        )}
      </div>

      {loading && (
        <div className="flex items-center justify-center gap-2 rounded-xl border border-border/60 py-10 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在加载看板…
        </div>
      )}

      {/* 第一层:待决策 */}
      {!loading && (
        <section className="space-y-2">
          <h4 className="text-sm font-semibold text-muted-foreground">第一层 · 待决策(有候选的文体)</h4>
          {withCandidate.length === 0 ? (
            <EmptyBox cta={onNavigate ? { label: '去「数据健康」看管道', onClick: () => onNavigate('health') } : undefined}>
              暂无待决策候选。结构研究正文攒够 30 篇后，系统可生成一版候选草稿；是否有效仍须预注册实验验证。先到「数据健康」确认 Jina 正文等级与直接结果血缘。
            </EmptyBox>
          ) : (
            <div className="grid gap-3 lg:grid-cols-2 xl:grid-cols-3">
              {withCandidate.map((card) => (
                <StyleCard
                  key={card.style_code}
                  card={card}
                  busy={busy}
                  onCompare={() => setCompareCard(card)}
                  onGenerate={() => generateSimulation(card)}
                  onExport={() => exportCorpus(card.style_code)}
                />
              ))}
            </div>
          )}
        </section>
      )}

      {/* 第二层:观察中 / 无候选(折叠) */}
      {!loading && withoutCandidate.length > 0 && (
        <section className="rounded-xl border border-border/60">
          <button type="button" onClick={() => setShowLayer2((v) => !v)} className="flex w-full items-center justify-between px-4 py-3 text-sm">
            <span className="font-medium">
              第二层 · 观察中 / 无候选(全部 {withoutCandidate.length} 个文体)
            </span>
            <span className="text-muted-foreground">{showLayer2 ? '收起 ▲' : '展开 ▼'}</span>
          </button>
          {showLayer2 && (
            <div className="overflow-x-auto border-t border-border/60">
              <table className="w-full min-w-[560px] text-sm">
                <thead className="bg-muted/30 text-left text-xs text-muted-foreground">
                  <tr>
                    <th className="px-4 py-2.5">文体</th>
                    <th className="px-4 py-2.5">状态</th>
                    <th className="px-4 py-2.5">结构样本进度</th>
                    <th className="px-4 py-2.5">下一步</th>
                  </tr>
                </thead>
                <tbody>
                  {withoutCandidate.map((card) => {
                    const adopted = Number(card.train_adopted || 0);
                    const control = Number(card.train_control || 0);
                    const ready = card.board_state === 'ready_pending_distill';
                    return (
                      <tr key={card.style_code} className="border-t border-border/40">
                        <td className="px-4 py-2.5 font-medium">{card.style_name}</td>
                        <td className="px-4 py-2.5">
                          {ready ? (
                            <Badge className="bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/15">样本已达标</Badge>
                          ) : card.board_state === 'observing' ? (
                            <Badge variant="secondary">样本积累中</Badge>
                          ) : (
                            <Badge variant="secondary">暂无候选</Badge>
                          )}
                        </td>
                        <td className="px-4 py-2.5">
                          <div className="flex items-center gap-2">
                            <Progress value={Math.min(100, (adopted / DISTILL_SAMPLE_TARGET) * 100)} className="h-2 w-24" />
                            <span className="text-xs text-muted-foreground">{sampleProgressLabel(adopted)}</span>
                          </div>
                        </td>
                        <td className="px-4 py-2.5 text-xs text-muted-foreground">
                          {ready
                            ? control < DISTILL_CONTROL_TARGET
                              ? `再积累 ${fmtInt(DISTILL_CONTROL_TARGET - control)} 篇普通文章作对照(${fmtInt(control)}/${DISTILL_CONTROL_TARGET}),就能自动提炼新模板`
                              : card.board_state_label
                            : adopted > 0 && adopted < DISTILL_SAMPLE_TARGET
                              ? '够 30 篇后自动提炼新模板'
                              : '等待范文更新后自动提炼新模板'}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* 第三层:进化历史 */}
      {!loading && (
        <section className="space-y-2">
          <h4 className="text-sm font-semibold text-muted-foreground">第三层 · 预注册实验的真实 GEO 效果</h4>
          {measures.length === 0 ? (
            <EmptyBox cta={onNavigate ? { label: '去「数据健康」看采集', onClick: () => onNavigate('health') } : undefined}>
              暂无严格实验结果。候选与对照完成发布快照、同品牌发布后监测及冻结观察窗后，这里才显示直接引用率；当前留空，不用代理数据补数。
            </EmptyBox>
          ) : (
            <div className="space-y-2">
              {measures.map((m) => {
                const name = m.style_name || styleNameByCode.get(m.style_code) || m.style_code;
                const insufficient = m.insufficient_data || !m.comparable;
                const decision = m.effect_decision || (insufficient ? 'INSUFFICIENT_SAMPLES' : 'INCONCLUSIVE');
                return (
                  <div key={`${m.style_code}-${m.new_version_id}`} className={cn('rounded-xl border p-3', decision === 'FAIL' ? 'border-red-500/40 bg-red-500/5' : 'border-border/60 bg-card/40')}>
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <div className="min-w-0">
                        <span className="font-medium">{name}</span>
                        <span className="ml-2 text-xs text-muted-foreground">观察 {m.age_days ?? '-'} 天</span>
                      </div>
                      {insufficient ? (
                        <Badge variant="secondary">数据积累中</Badge>
                      ) : decision === 'PASS' ? (
                        <Badge className="bg-emerald-500 text-white hover:bg-emerald-500">PASS · 候选更优</Badge>
                      ) : decision === 'FAIL' ? (
                        <Badge className="bg-red-500 text-white hover:bg-red-500">FAIL · 候选更差</Badge>
                      ) : (
                        <Badge variant="secondary">INCONCLUSIVE</Badge>
                      )}
                    </div>
                    <p className="mt-1 text-[13px] text-muted-foreground">
                      {insufficient
                        ? m.reason || '样本、覆盖或观察窗口不足，暂不裁决'
                        : `候选文章直接引用率 ${fmtPct(m.new_rate)} vs 对照 ${fmtPct(m.old_rate)} · ${m.reason || decision}`}
                    </p>
                  </div>
                );
              })}
            </div>
          )}
        </section>
      )}

      <CompareDialog
        open={!!compareCard}
        simulationId={compareCard?.latest_sim_id ?? null}
        styleName={compareCard?.style_name || ''}
        candidateLabel={compareCandidateLabel}
        styleCode={compareCard?.style_code}
        industry={industry}
        boardState={compareCard?.board_state}
        latestSimStatus={compareCard?.latest_sim_status}
        hasCandidate={!!compareCard?.candidate_version_id}
        busy={busy}
        reviewingSimId={reviewingSimId}
        detailRefreshKey={detailRefreshKey}
        onOpenChange={(v) => !v && setCompareCard(null)}
        onAdopt={(note) => compareCard && adopt(compareCard, note)}
        onReject={(note) => compareCard && reject(compareCard, note)}
        onObserve={() => {
          toast.info('不做变更,已关闭');
          setCompareCard(null);
        }}
        onReview={() => compareCard?.latest_sim_id && runReview(compareCard.latest_sim_id)}
        onExportCorpus={() => compareCard && exportCorpus(compareCard.style_code)}
      />
      {confirmDialog}
    </div>
  );
}

function SummaryStat({ value, label, hint, tone }: { value: string; label: string; hint?: string; tone?: 'green' | 'amber' }) {
  return (
    <div className="rounded-xl border border-border/60 bg-background/40 p-3">
      <div className={cn('text-xl font-semibold', tone === 'green' && 'text-emerald-400', tone === 'amber' && 'text-amber-400')}>{value}</div>
      <div className="mt-0.5 text-[13px] font-medium text-foreground">{label}</div>
      {hint && <div className="text-[11px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

function EmptyBox({ children, cta }: { children: React.ReactNode; cta?: { label: string; onClick: () => void } }) {
  return (
    <div className="rounded-xl border border-dashed border-border/60 bg-card/30 px-4 py-6 text-center text-[13px] text-muted-foreground">
      <p>{children}</p>
      {cta && (
        <Button variant="outline" size="sm" className="mt-3" onClick={cta.onClick}>
          {cta.label}
        </Button>
      )}
    </div>
  );
}

function StyleCard({
  card,
  busy,
  onCompare,
  onGenerate,
  onExport,
}: {
  card: EvolutionCard;
  busy: boolean;
  onCompare: () => void;
  onGenerate: () => void;
  onExport: () => void;
}) {
  const review = card.review_summary || {};
  const hasSim = card.latest_sim_id != null;
  const isRecommend = card.board_state === 'recommend_replace';
  const isGenerating = card.board_state === 'generating';
  const isFailed = card.board_state === 'failed';

  return (
    <div
      className={cn(
        'flex flex-col rounded-2xl border bg-card/60 p-4',
        isRecommend && 'border-emerald-500/40 bg-emerald-500/5',
        isFailed && 'border-red-500/40',
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          {/* [2026-08-01] 原来是 truncate 单行,文体名被截成「排…/选…/品…/对…」完全读不出是哪张卡。
              改双行 clamp + title 悬浮全名:两行足够放下现有全部文体名,超长仍可 hover 看全。 */}
          <h5 className="line-clamp-2 break-words text-base font-semibold" title={card.style_name}>
            {card.style_name}
          </h5>
        </div>
        {stateBadge(card)}
      </div>

      <dl className="mt-3 grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs">
        <div>
          <dt className="text-muted-foreground">当前版</dt>
          <dd className="truncate font-medium" title={card.current_version_label || ''}>{card.current_version_label || '代码默认'}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">候选版</dt>
          <dd className="font-medium">{candidateHint(card.candidate_created_at, card.candidate_evidence_samples)}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">结构研究正文</dt>
          <dd className="font-medium">{fmtInt(card.candidate_evidence_samples)} 篇</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">结构样本</dt>
          <dd className="font-medium">{fmtInt(card.train_adopted)} 篇</dd>
        </div>
      </dl>

      {/* AI 评分对比 */}
      {review.verdict != null ? (
        <div className="mt-3 rounded-lg border border-border/60 bg-background/40 p-2.5">
          <div className="flex items-center justify-between text-[11px] text-muted-foreground">
            <span>AI 评分对比</span>
            <span>{review.dual_model ? (review.reviewer_agreement ? '双模型一致' : '双模型不一致') : '单评审'}</span>
          </div>
          <div className="mt-1 flex items-center gap-2">
            <span className="text-lg font-semibold">{fmtScore(review.avg_current_score)}</span>
            <span className="text-muted-foreground">→</span>
            <span className="text-lg font-semibold text-emerald-400">{fmtScore(review.avg_candidate_score)}</span>
            {typeof review.score_delta === 'number' && (
              <span className={cn('text-xs', review.score_delta >= 0 ? 'text-emerald-300' : 'text-red-300')}>
                {review.score_delta >= 0 ? `↑${review.score_delta}` : `↓${Math.abs(review.score_delta)}`}
              </span>
            )}
          </div>
        </div>
      ) : isGenerating ? (
        <div className="mt-3 flex items-center gap-2 rounded-lg border border-sky-500/30 bg-sky-500/5 p-2.5 text-xs text-sky-300">
          <Loader2 className="h-4 w-4 animate-spin" /> AI 正在生成对比样文(约 2 分钟)
        </div>
      ) : isFailed ? (
        <div className="mt-3 flex items-center gap-2 rounded-lg border border-red-500/30 bg-red-500/5 p-2.5 text-xs text-red-300">
          <AlertTriangle className="h-4 w-4" /> 生成失败,点击「重试」重新生成
        </div>
      ) : (
        <div className="mt-3 rounded-lg border border-dashed border-border/60 bg-background/30 p-2.5 text-xs text-muted-foreground">
          有候选,待生成对比样文并评审。
        </div>
      )}

      {/* 操作区 */}
      <div className="mt-3 flex flex-wrap gap-2">
        {hasSim ? (
          <Button size="sm" className={cn(isRecommend && 'bg-emerald-500 text-white hover:bg-emerald-600')} variant={isRecommend ? 'default' : 'outline'} onClick={onCompare} disabled={busy}>
            <Eye className="mr-1.5 h-3.5 w-3.5" /> 看对比并决策
          </Button>
        ) : (
          <Button size="sm" onClick={onGenerate} disabled={busy}>
            {busy ? <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" /> : <FlaskConical className="mr-1.5 h-3.5 w-3.5" />}
            {isFailed ? '重试生成' : '生成对比样文'}
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={onExport} disabled={busy}>
          <Download className="mr-1.5 h-3.5 w-3.5" /> 导出范文
        </Button>
      </div>
    </div>
  );
}
