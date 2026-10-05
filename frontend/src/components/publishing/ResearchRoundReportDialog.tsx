// [WO_MEDIA_BOARD_UX_CLOSURE §3] 「本轮调研结果」报表 + 历轮回看。
//
// 生产实证(2026-08-05 Owner 亲测单):数据反哺**其实成功了**(4 次引擎调用、
// 29 条引用落库、榜数据源已更新),断的是**交付面** —— 完成只有一句 toast,
// 无结果视图、无"已反哺"明示;该单又归并进已有行业(榜上本就有数据),
// 用户肉眼看不出任何变化 → **花 3900 算力像什么都没发生**。
//
// 本弹层做三件事:①完成即报表(按 round_id 从既有落库数据直出,零新增采集)
// ②反哺明示(反哺是自动的,用户缺的是"被告知它发生了")③历史可回看。
//
// 🔴 不编数:后端 `score_delta_available=false` 时**整段不渲染**分值变化,
//    并如实说明原因 —— 拿别的数糊上去比不显示更糟。
import { useCallback, useEffect, useState } from 'react';
import { CheckCircle2, ChevronLeft, Clock, Loader2, Sparkles } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

type ReportMedia = {
  domain: string;
  display_name?: string;
  one_liner?: string;
  citations?: number;
  articles?: number;
  platforms?: string[];
  is_new?: boolean;
};

type RoundReport = {
  round_id?: string;
  status?: string;
  prompts?: { prompt_id?: number; text?: string }[];
  prompt_count?: number;
  engine_count?: number;
  call_stats?: { total?: number; success?: number; failed?: number };
  media?: ReportMedia[];
  media_total?: number;
  new_media_domains?: string[];
  citation_count?: number;
  failed_calls?: { text?: string; platform?: string; error?: string }[];
  score_delta_available?: boolean;
  score_delta_reason?: string;
  has_data?: boolean;
  industry_key?: string;
  industry_raw?: string;
  price_points?: number;
  flywheel_note?: string;
  finished_at?: string | null;
};

type RoundListItem = {
  task_id?: number;
  round_id?: string;
  status?: string;
  price_points?: number;
  prompt_count?: number;
  created_at?: string | null;
  finished_at?: string | null;
};

const ENGINE_LABEL: Record<string, string> = {
  deepseek: 'DeepSeek',
  kimi: 'Kimi',
  doubao: '豆包',
  qwen: '通义千问',
};

function engineLabel(e: string) {
  return ENGINE_LABEL[e] || e;
}

function shortTime(value?: string | null) {
  if (!value) return '';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

export function ResearchRoundReportDialog({
  open,
  onOpenChange,
  roundId,
  industry,
  brandId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 打开时直接展示的轮次;不传则先进历轮列表 */
  roundId?: string;
  /** 历轮列表按行业查(与 /active-task 同一套零 LLM 归一) */
  industry?: string;
  /** [WO_267] 带上品牌:行业判定吃品牌上下文,与 /active-task、点亮弹窗 /draft 同一份输入 */
  brandId?: number | null;
}) {
  const [activeRound, setActiveRound] = useState<string>('');
  const [report, setReport] = useState<RoundReport | null>(null);
  const [rounds, setRounds] = useState<RoundListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState(false);

  useEffect(() => {
    if (open) setActiveRound(roundId || '');
  }, [open, roundId]);

  const loadReport = useCallback(async (rid: string) => {
    if (!rid) return;
    setLoading(true);
    try {
      const res = await authFetch(`/api/publish/research/round-report/${encodeURIComponent(rid)}`);
      if (!res.ok) throw new Error('load failed');
      setReport((await res.json()) as RoundReport);
      setLoadError(false);
    } catch {
      setReport(null);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadRounds = useCallback(async (ind: string) => {
    if (!ind) return;
    setLoading(true);
    try {
      const q = new URLSearchParams({ industry: ind });
      if (brandId) q.set('brand_id', String(brandId));
      const res = await authFetch(`/api/publish/research/rounds?${q}`);
      if (!res.ok) throw new Error('load failed');
      const d = await res.json();
      setRounds((d?.rounds || []).filter(Boolean));
      setLoadError(false);
    } catch {
      setRounds([]);
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, [brandId]); // [WO_267] 读了 brandId 就得依赖它,否则闭包锁住首次那个品牌

  useEffect(() => {
    if (!open) return;
    if (activeRound) void loadReport(activeRound);
    else void loadRounds((industry || '').trim());
  }, [open, activeRound, industry, loadReport, loadRounds]);

  const stats = report?.call_stats || {};
  const media = report?.media || [];
  const failed = report?.failed_calls || [];
  // fail-soft(工单 §3.4):取数失败 / 还没整理好 → "结果整理中",不是空白。
  const settling = !loading && (loadError || (activeRound && report && report.has_data === false));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[85dvh] max-w-2xl overflow-hidden" data-testid="research-round-report">
        <DialogHeader>
          <DialogTitle className="flex flex-wrap items-center gap-2 text-base">
            {activeRound && (
              <Button
                size="sm"
                variant="ghost"
                className="h-6 gap-1 px-1.5 text-xs"
                onClick={() => setActiveRound('')}
                data-testid="round-report-back"
              >
                <ChevronLeft className="size-3.5" />
                历轮
              </Button>
            )}
            {activeRound ? '本轮调研结果' : '历轮调研记录'}
            {report?.industry_raw && activeRound && (
              <Badge variant="secondary" className="px-1.5 py-0 text-[10px]">{report.industry_raw}</Badge>
            )}
          </DialogTitle>
          <DialogDescription className="text-xs">
            {activeRound
              ? '本轮跑了哪些题、被 AI 引用到了哪些媒体,以及这批数据去了哪里。'
              : '你在这个行业花过的每一轮调研都在这里,点开看当时的结果。'}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
          {loading && (
            <div className="flex items-center gap-2 py-8 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              正在整理结果…
            </div>
          )}

          {settling && (
            <div className="flex items-center gap-2 rounded-md border border-border/60 bg-muted/20 px-3 py-3 text-xs text-muted-foreground">
              <Clock className="size-3.5" />
              结果整理中,稍后回来看。数据不会丢 —— 这一轮已经跑完并入库了。
            </div>
          )}

          {/* ===== 历轮列表 ===== */}
          {!loading && !activeRound && !loadError && (
            rounds.length === 0 ? (
              <div className="py-8 text-center text-xs text-muted-foreground">
                这个行业还没有你发起过的调研记录。
              </div>
            ) : (
              <div className="space-y-1.5">
                {rounds.map((r) => (
                  <button
                    key={r.round_id}
                    type="button"
                    onClick={() => setActiveRound(String(r.round_id || ''))}
                    className="flex w-full flex-wrap items-center gap-2 rounded-md border border-border/60 bg-background/60 px-2.5 py-2 text-left text-xs transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/10"
                    data-testid="round-history-item"
                  >
                    <span className="font-medium">{shortTime(r.created_at) || r.round_id}</span>
                    <span className="text-muted-foreground">{r.prompt_count || 0} 题</span>
                    {typeof r.price_points === 'number' && (
                      <span className="text-muted-foreground">{r.price_points} 算力</span>
                    )}
                    <Badge variant="secondary" className="ml-auto px-1.5 py-0 text-[9px]">
                      {r.status === 'completed' ? '已完成' : r.status}
                    </Badge>
                  </button>
                ))}
              </div>
            )
          )}

          {/* ===== 单轮报表 ===== */}
          {!loading && activeRound && report && report.has_data && (
            <>
              <div className="flex flex-wrap items-center gap-2 rounded-md border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-xs">
                <CheckCircle2 className="size-4 shrink-0 text-emerald-500" />
                <span className="text-emerald-700 dark:text-emerald-300">
                  {/* [§3.2 反哺明示] 反哺是自动的(已实证),用户缺的是被告知它发生了。 */}
                  {report.flywheel_note || '本轮数据已自动计入本行业「AI 真实引用媒体榜」。'}
                </span>
              </div>

              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                {[
                  { label: '本轮题目', value: `${report.prompt_count || 0} 题` },
                  { label: '引擎覆盖', value: `${report.engine_count || 0} 个` },
                  { label: '引擎调用', value: `${stats.success || 0}/${stats.total || 0} 成功` },
                  { label: '拿到引用', value: `${report.citation_count || 0} 条` },
                ].map((s) => (
                  <div key={s.label} className="rounded-md border border-border/60 bg-background/60 px-2 py-1.5">
                    <div className="text-[10px] text-muted-foreground">{s.label}</div>
                    <div className="text-sm font-semibold tabular-nums">{s.value}</div>
                  </div>
                ))}
              </div>

              {(report.prompts || []).length > 0 && (
                <div>
                  <div className="mb-1 text-[10px] font-medium text-muted-foreground">本轮问了这些问题</div>
                  <div className="space-y-1">
                    {(report.prompts || []).map((p, i) => (
                      <div key={`${p.prompt_id}-${i}`} className="rounded border border-border/50 bg-muted/20 px-2 py-1 text-[11px]">
                        {p.text}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {media.length > 0 && (
                <div>
                  <div className="mb-1 flex flex-wrap items-baseline gap-1.5">
                    <span className="text-[10px] font-medium text-muted-foreground">
                      本轮被 AI 引用到的媒体
                    </span>
                    <span className="text-[10px] text-muted-foreground/70">
                      共 {report.media_total || media.length} 家
                      {(report.new_media_domains || []).length > 0 &&
                        ` · 其中 ${(report.new_media_domains || []).length} 家是本轮第一次出现`}
                    </span>
                  </div>
                  <div className="space-y-1">
                    {media.map((m) => (
                      <div
                        key={m.domain}
                        title={m.one_liner || m.domain}
                        className="flex flex-wrap items-center gap-1.5 rounded border border-border/50 bg-background/60 px-2 py-1 text-[11px]"
                        data-testid="round-report-media-row"
                      >
                        <span className="font-medium">{m.display_name || m.domain}</span>
                        {m.display_name && m.display_name !== m.domain && (
                          <span className="text-[9px] text-muted-foreground/70">{m.domain}</span>
                        )}
                        {m.is_new && (
                          <Badge className="border-emerald-500/30 bg-emerald-500/15 px-1 py-0 text-[9px] text-emerald-600 dark:text-emerald-300">
                            本轮新出现
                          </Badge>
                        )}
                        <span className="ml-auto shrink-0 text-muted-foreground">被引 {m.citations || 0} 次</span>
                        {(m.platforms || []).length > 0 && (
                          <span className="shrink-0 text-[9px] text-muted-foreground/70">
                            {(m.platforms || []).map(engineLabel).join('/')}
                          </span>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* 🔴 分值变化:后端说不可得就**整段不渲染数字**,只如实说明为什么没有。 */}
              {report.score_delta_available === false && report.score_delta_reason && (
                <p className="text-[10px] leading-relaxed text-muted-foreground/70">
                  {report.score_delta_reason}
                </p>
              )}

              {failed.length > 0 && (
                <div>
                  <div className="mb-1 text-[10px] font-medium text-muted-foreground">
                    这些题这一轮没跑成(照实列出)
                  </div>
                  <div className="space-y-1">
                    {failed.map((f, i) => (
                      <div key={`${f.text}-${f.platform}-${i}`} className="rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1 text-[10px]">
                        <span className="font-medium">{f.text || '(题目缺失)'}</span>
                        <span className="ml-1 text-muted-foreground">· {engineLabel(String(f.platform || ''))}</span>
                        {f.error && <span className="ml-1 text-muted-foreground/70">· {f.error}</span>}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {media.length === 0 && (
                <div className="flex items-center gap-2 rounded-md border border-border/60 bg-muted/20 px-3 py-2.5 text-xs text-muted-foreground">
                  <Sparkles className="size-3.5" />
                  本轮没有拿到新的媒体引用。题目本身可能太窄,换个问法下一轮通常就有了。
                </div>
              )}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
