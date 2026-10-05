// [R 批 · U6] 代理端「⚡ 点亮本行业调研」确认弹窗(R3 交互)。
//  打开即免费预览(POST /draft):归并明示 + 现有题目 + 建议题 + 动态价 + 新鲜度知情。
//  确认(POST /self-serve)→ 冻结算力发起跑批 → 弹窗内进度轮询(GET /task/{id})。
//  完成才扣、失败自动退,全程弹窗内闭环,不跳页(永远不中断对话铁律)。
//  引擎名(DeepSeek/Kimi/豆包/通义千问)可露;不涉及 %/SOV,不碰社媒/svideo。
import { useEffect, useMemo, useState } from 'react';
import { Loader2, Zap, Plus, X } from 'lucide-react';
import { toast } from 'sonner';
import { authFetch } from '@/lib/api';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';
import { industryCategoryText, useIndustryTaxonomy } from '@/lib/industryTaxonomy';

type ResolvedInfo = {
  industry_name?: string;
  industry_key?: string;
  is_new?: boolean;
  resolved_by?: string;
  merged_note?: string;
  /** [WO_267] 判出的行业大类(弹窗显示 + 可改选;改选后带 category_key 重新 /draft) */
  category?: {
    key?: string;
    name?: string;
    secondary_key?: string | null;
    secondary_name?: string | null;
    needs_review?: boolean;
    source?: string;
  } | null;
};

type ExistingPrompt = { id: number; text: string };

type DraftResponse = {
  resolved: ResolvedInfo;
  existing_prompts: ExistingPrompt[];
  suggested_prompts: string[];
  default_price_points: number;
  per_count_hint?: number;
  // U6 小后端补充:1..20 题各档预览价,弹窗按当前勾选题数即时查表,零额外请求。
  // [2026-07-28 块A] 价格改为价目表驱动(与实扣同源)。
  // [2026-07-28 定稿] 基础价含 15 题;16..20 每题 +290 → **各档不再同价**,
  //   本 map 必须真查(别再假设"恒等于基础价"而把查表优化掉)。
  price_by_count?: Record<string, number>;
  // [2026-07-28 块A] 定价未配置(部署时 UPDATE feature_pricing 还没跑)→ 后端发起会 503。
  //   这里提前禁用按钮并说明,不让用户看着价格点下去才失败。老后端不返回该字段 → undefined
  //   按「已配置」处理(向后兼容,不会因为前端先上线就把功能锁死)。
  pricing_configured?: boolean;
  industry_last_by_others?: { days_ago: number } | null;
  // [FIX-1] 本人本行业在飞任务(服务端恢复源·非属主查不到)→ 打开弹窗直接续显进度。
  active_task?: { task_id: number; status: string } | null;
};

const MIN_PROMPTS = 1;
const MAX_PROMPTS = 20;

/** 归一化(去空白 + 小写),镜像后端 _clean_texts 的 occupied_lower 去重基准。 */
function normalizePrompt(s: string): string {
  return (s || '').trim().toLowerCase();
}

// [FIX-1] 进度轮询已上移父层(PublishCenter):POLL_* 常量 / humanizeFailure(失败文案·不超售)/ 轮询
//   逻辑全在 PublishCenter。本弹窗只保留 STATUS_LABEL 供受控 running 视图显示 activeTask.status。
const STATUS_LABEL: Record<string, string> = {
  queued: '排队中',
  running: '调研进行中',
  completed: '已完成',
  failed: '未完成',
  timeout: '超时',
  cancelled: '已取消',
};

export function ResearchSelfserveDialog({
  open,
  onOpenChange,
  brandId,
  industry,
  onCompleted,
  activeTask = null,
  onStarted,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  brandId?: number | null;
  /** 用户填写的行业原文(未归并),交给后端 /draft 归一。 */
  industry: string;
  /** 付费完成后回调(父层 bump refreshKey 让本行业榜立即重拉)。 */
  onCompleted: () => void;
  // [FIX-1] 受控进度:轮询归父层(PublishCenter),弹窗只是视图。父层传入本行业在飞任务 →
  //   弹窗直接显 running(关窗重开仍在·验收⑤)。付费发起 / draft 探到在飞任务 → onStarted 通知父层。
  activeTask?: { taskId: number; status: string; overrun: boolean } | null;
  onStarted?: (taskId: number, status: string, industryKey?: string) => void;
}) {
  // [FIX-1] running 由父层 activeTask 驱动(受控),不再有内部 taskId/轮询。
  const running = !!activeTask;
  const [loadingDraft, setLoadingDraft] = useState(false);
  const [draft, setDraft] = useState<DraftResponse | null>(null);
  const [draftError, setDraftError] = useState<string | null>(null);

  const [checkedExisting, setCheckedExisting] = useState<Set<number>>(new Set());
  const [checkedSuggested, setCheckedSuggested] = useState<Set<number>>(new Set());
  const [newTexts, setNewTexts] = useState<string[]>([]);
  const [newInput, setNewInput] = useState('');

  const [submitting, setSubmitting] = useState(false);
  /** [WO_267] 用户改选的行业大类 key;null = 用后端判出来的。改选即带着它重新 /draft。 */
  const [categoryKey, setCategoryKey] = useState<string | null>(null);
  const { taxonomy, nameOf: industryNameOf } = useIndustryTaxonomy();

  // ── 打开 → 免费预览拉 draft;关闭 → 重置表单态(进度态归父层 activeTask,关窗不丢)。
  useEffect(() => {
    if (!open) {
      setDraft(null);
      setDraftError(null);
      setCheckedExisting(new Set());
      setCheckedSuggested(new Set());
      setNewTexts([]);
      setNewInput('');
      setSubmitting(false);
      setCategoryKey(null);
      return;
    }
    const ind = (industry || '').trim();
    if (!ind) {
      setDraftError('还没选品牌:请先在页面上方选一个填了行业的品牌');
      return;
    }
    let cancelled = false;
    (async () => {
      setLoadingDraft(true);
      setDraftError(null);
      try {
        const res = await authFetch('/api/publish/research/draft', {
          method: 'POST',
          body: JSON.stringify({ industry: ind, brand_id: brandId ?? undefined, category_key: categoryKey ?? undefined }),
        });
        if (res.status === 403) {
          if (!cancelled) setDraftError('仅服务方可使用自助调研');
          return;
        }
        if (!res.ok) {
          if (!cancelled) setDraftError('加载失败,请重试');
          return;
        }
        const data = (await res.json()) as DraftResponse;
        if (cancelled) return;
        setDraft(data);
        // [FIX-1] draft 探到本行业在飞任务 → 通知父层 seed activeTask(打开弹窗直接显进度·验收⑤;
        //   父层若已由 active-task 端点 seed 则同 taskId 幂等)。
        if (data.active_task?.task_id && onStarted) {
          onStarted(data.active_task.task_id, data.active_task.status, data.resolved?.industry_key);
        }
        // 现有题目默认勾到上限;若无现有题目(新行业),默认勾建议题给一个可用起点。
        const exIds = new Set<number>((data.existing_prompts || []).slice(0, MAX_PROMPTS).map((p) => p.id));
        setCheckedExisting(exIds);
        if ((data.existing_prompts || []).length === 0) {
          setCheckedSuggested(new Set<number>((data.suggested_prompts || []).slice(0, MAX_PROMPTS).map((_, i) => i)));
        } else {
          setCheckedSuggested(new Set<number>());
        }
      } catch {
        if (!cancelled) setDraftError('加载失败,请重试');
      } finally {
        if (!cancelled) setLoadingDraft(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [open, industry, brandId, categoryKey]);

  // [FIX-1] 进度轮询已上移父层(PublishCenter):弹窗关闭 / 刷新 / 换设备也能完成 toast + 刷新榜(验收①②③④)。
  //   本弹窗只是视图:running 态与 status/overrun 由父层 activeTask 受控传入;发起 / draft 探到在飞任务
  //   → onStarted 通知父层启动轮询。轮询的 #12(排队不计超时)/#13(错误分类)/stale 守卫全在父层落实。

  // [#10] 前端镜像后端 _clean_texts 去重,使弹窗题数/价 == 后端实际冻结 n:
  //  后端剔除任何与"全部现有 active 题 + 已勾选建议题"文本(归一后)重复的新题,并去 newTexts 内部重复。
  //  若不镜像,用户自加题 == 某现有 active 题时弹窗显 X 算力却实扣 Y<X,且该重复题从未跑,还无提示。
  const newTextStatus = useMemo(() => {
    // 去重基准 = 全部现有 active 题 + 已勾选建议题(镜像后端 occupied_lower;建议题在 new_prompt_texts 前序,故先占位)。
    const occupied = new Set<string>();
    for (const p of draft?.existing_prompts || []) {
      const n = normalizePrompt(p.text);
      if (n) occupied.add(n);
    }
    (draft?.suggested_prompts || []).forEach((t, i) => {
      if (checkedSuggested.has(i)) {
        const n = normalizePrompt(t);
        if (n) occupied.add(n);
      }
    });
    const seen = new Set<string>();
    return newTexts.map((text) => {
      const n = normalizePrompt(text);
      // dropped = 后端会剔除的(空 / 与现有或已选重复 / newTexts 内部重复)→ 不计入题数与价。
      const dropped = !n || occupied.has(n) || seen.has(n);
      if (!dropped) seen.add(n);
      return { text, dropped };
    });
  }, [draft, newTexts, checkedSuggested]);

  const effectiveNewCount = useMemo(
    () => newTextStatus.filter((x) => !x.dropped).length,
    [newTextStatus],
  );

  // [FIX-9] 已勾选建议题中与现有 active 题(归一后)撞车的 → 后端 _clean_texts 会剔(new_prompt_texts 里
  //   撞 occupied_lower 的建议题被丢),前端若仍按 checkedSuggested.size 全额计价则显示价 > 实扣价、且该
  //   题静默不跑。这里同步从计价剔除(标"复用现有"·不多算钱),使弹窗题数/价 == 后端实际冻结 n。
  const existingNorm = useMemo(() => {
    const s = new Set<string>();
    for (const p of draft?.existing_prompts || []) {
      const n = normalizePrompt(p.text);
      if (n) s.add(n);
    }
    return s;
  }, [draft]);
  const suggestedDupIdx = useMemo(() => {
    const dup = new Set<number>();
    (draft?.suggested_prompts || []).forEach((t, i) => {
      if (checkedSuggested.has(i) && existingNorm.has(normalizePrompt(t))) dup.add(i);
    });
    return dup;
  }, [draft, checkedSuggested, existingNorm]);
  const effectiveSuggestedCount = Math.max(0, checkedSuggested.size - suggestedDupIdx.size);

  const selectedCount = checkedExisting.size + effectiveSuggestedCount + effectiveNewCount;
  const selectableCount = (draft?.existing_prompts || []).length + (draft?.suggested_prompts || []).length;
  const selectableRoom = Math.max(0, MAX_PROMPTS - effectiveNewCount);
  const selectedSelectableCount = checkedExisting.size + checkedSuggested.size;
  const allSelectableSelected = selectableCount > 0
    && selectedSelectableCount >= Math.min(selectableCount, selectableRoom);

  const currentPrice = useMemo(() => {
    if (!draft) return 0;
    const c = Math.max(MIN_PROMPTS, Math.min(MAX_PROMPTS, selectedCount || MIN_PROMPTS));
    const map = draft.price_by_count || {};
    const v = map[String(c)];
    if (typeof v === 'number') return v;
    return draft.default_price_points || 0;
  }, [draft, selectedCount]);

  const mergedNote = useMemo(() => {
    if (!draft) return '';
    const r = draft.resolved || {};
    if (r.merged_note) return r.merged_note;
    const name = r.industry_name || industry;
    if (r.is_new) return `将新建行业「${name}」`;
    return `你填写的「${industry}」已归入平台行业「${name}」`;
  }, [draft, industry]);

  const toggleExisting = (id: number, on: boolean) => {
    setCheckedExisting((prev) => {
      const next = new Set(prev);
      if (on) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const toggleSuggested = (idx: number, on: boolean) => {
    setCheckedSuggested((prev) => {
      const next = new Set(prev);
      if (on) next.add(idx);
      else next.delete(idx);
      return next;
    });
  };

  const toggleAllPrompts = () => {
    if (!draft) return;
    if (allSelectableSelected) {
      setCheckedExisting(new Set());
      setCheckedSuggested(new Set());
      return;
    }
    const nextExisting = new Set<number>();
    const nextSuggested = new Set<number>();
    let room = selectableRoom;
    for (const p of draft.existing_prompts || []) {
      if (room <= 0) break;
      nextExisting.add(p.id);
      room -= 1;
    }
    for (let i = 0; i < (draft.suggested_prompts || []).length; i += 1) {
      if (room <= 0) break;
      nextSuggested.add(i);
      room -= 1;
    }
    setCheckedExisting(nextExisting);
    setCheckedSuggested(nextSuggested);
  };

  const addNewText = () => {
    const s = newInput.trim();
    if (!s) return;
    const n = normalizePrompt(s);
    // [#10] 若与某现有 active 题重复:后端会剔除该新题(occupied_lower 含全部 existing)。改为"复用现有题"
    //  —— 勾选它并提示,避免用户以为加了题实则被静默丢弃、题数/价对不上。
    const existingHit = (draft?.existing_prompts || []).find((p) => normalizePrompt(p.text) === n);
    if (existingHit) {
      setCheckedExisting((prev) => {
        const next = new Set(prev);
        next.add(existingHit.id);
        return next;
      });
      toast.info('该题已存在,已为你复用');
      setNewInput('');
      return;
    }
    // 归一后与已加新题重复不重复添加(镜像后端 newTexts 内部去重)。
    setNewTexts((prev) => (prev.some((x) => normalizePrompt(x) === n) ? prev : [...prev, s]));
    setNewInput('');
  };

  const removeNewText = (t: string) => {
    setNewTexts((prev) => prev.filter((x) => x !== t));
  };

  const countValid = selectedCount >= MIN_PROMPTS && selectedCount <= MAX_PROMPTS;
  // [块A] 老后端不返回 pricing_configured → undefined 视为已配置(前端先上线不锁死功能)。
  const pricingReady = draft?.pricing_configured !== false;
  const canSubmit = countValid && pricingReady;

  const handleConfirm = async () => {
    if (!draft || submitting || running) return;
    if (!canSubmit) return;
    const ind = (industry || '').trim();
    if (!ind) return;
    setSubmitting(true);
    const selected_prompt_ids = Array.from(checkedExisting);
    const suggestedTexts = (draft.suggested_prompts || []).filter((_, i) => checkedSuggested.has(i));
    const new_prompt_texts = [...suggestedTexts, ...newTexts];
    try {
      const res = await authFetch('/api/publish/research/self-serve', {
        method: 'POST',
        body: JSON.stringify({
          industry: ind,
          brand_id: brandId ?? undefined,
          category_key: categoryKey ?? undefined,
          selected_prompt_ids,
          new_prompt_texts,
        }),
      });
      if (res.status === 402) {
        // [C12] 算力不足给可操作入口(去充值页),不让用户自己找。
        toast.error('算力不足,请先充值', {
          action: { label: '去充值', onClick: () => { onOpenChange(false); window.location.href = '/customer/recharge'; } },
        });
        setSubmitting(false);
        return;
      }
      if (res.status === 409) {
        let msg = '你近期已调研过本行业,暂无需重跑';
        try {
          const d = await res.json();
          msg = d?.detail?.message || msg;
        } catch {
          /* ignore */
        }
        toast.error(msg);
        setSubmitting(false);
        return;
      }
      if (res.status === 422) {
        let msg = `题目数需在 ${MIN_PROMPTS}-${MAX_PROMPTS} 之间`;
        try {
          const d = await res.json();
          msg = d?.detail?.message || msg;
        } catch {
          /* ignore */
        }
        toast.error(msg);
        setSubmitting(false);
        return;
      }
      if (!res.ok) {
        toast.error('发起失败,请重试');
        setSubmitting(false);
        return;
      }
      const data = await res.json();
      // [FIX-1] 付费发起成功 → 通知父层 seed activeTask + 启动后台轮询(弹窗关了/刷新也能完成)。
      //   running 态由父层 activeTask 回传驱动(受控),本弹窗不再自持 taskId/轮询。
      if (onStarted) {
        onStarted(Number(data.task_id), String(data.status || 'queued'), draft?.resolved?.industry_key);
      }
      setSubmitting(false);
    } catch {
      toast.error('发起失败,请重试');
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Zap className="size-4 text-emerald-400" />
            点亮本行业调研
          </DialogTitle>
          <DialogDescription>
            跑一轮真实 AI 搜索调研(DeepSeek · Kimi · 豆包 · 通义千问),点亮本行业「AI 真实引用媒体榜」。
          </DialogDescription>
        </DialogHeader>

        {running ? (
          // ── 进度态(受控:status/overrun 由父层 activeTask 传入)────────────
          <div className="flex flex-col items-center gap-3 py-6 text-center">
            <Loader2 className="size-8 animate-spin text-emerald-400" />
            <div className="text-sm font-medium text-foreground">
              {activeTask?.overrun ? '调研仍在后台进行' : `调研进行中 · ${STATUS_LABEL[activeTask?.status || 'running'] || activeTask?.status || '进行中'}`}
            </div>
            <div className="text-xs leading-relaxed text-muted-foreground">
              {activeTask?.overrun ? (
                <>
                  本次调研耗时较长,仍在后台继续进行(未扣费,完成才扣、失败自动退)。
                  <br />
                  完成后本行业榜会自动点亮;你可以保持此窗口开启,或稍后回到本页查看。
                </>
              ) : (
                <>
                  正在多引擎采集本行业的真实推荐来源,约 10-30 分钟。
                  <br />
                  完成后本页会自动点亮本行业榜,可保持页面开启或稍后回来查看。
                </>
              )}
            </div>
          </div>
        ) : loadingDraft ? (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> 正在准备调研题目…
          </div>
        ) : draftError ? (
          <div className="py-6 text-center text-sm text-muted-foreground">{draftError}</div>
        ) : draft ? (
          // ── 表单态 ──────────────────────────────────────────────
          <div className="space-y-3">
            {/* 归并明示(老板拍板③透明化) */}
            <div className="rounded-md border border-border/60 bg-muted/30 px-3 py-2 text-xs leading-relaxed text-muted-foreground">
              {mergedNote}
            </div>

            {/* [WO_267] 判出的行业大类:明示 + 可改选(改选后带 category_key 重新预览)。
                英文 key 不许上屏 —— 名字一律走 industryCategoryText。 */}
            {draft.resolved?.category?.key && (
              <div className="space-y-1.5 rounded-md border border-border/60 px-3 py-2 text-xs" data-testid="research-category">
                <div>
                  判定的行业大类:
                  <span className="font-medium text-foreground" data-testid="research-category-name">
                    {industryCategoryText(draft.resolved.category.key, draft.resolved.category.name, industryNameOf) || '未能确定'}
                  </span>
                  {industryCategoryText(draft.resolved.category.secondary_key, draft.resolved.category.secondary_name, industryNameOf) && (
                    <span className="text-muted-foreground" data-testid="research-category-secondary">
                      (也涉及 {industryCategoryText(draft.resolved.category.secondary_key, draft.resolved.category.secondary_name, industryNameOf)})
                    </span>
                  )}
                  {draft.resolved.category.needs_review && (
                    <Badge variant="outline" className="ml-2 text-[10px]" data-testid="research-category-review">待确认</Badge>
                  )}
                </div>
                {(taxonomy?.categories.length ?? 0) > 0 && (
                  <label className="flex items-center gap-2 text-muted-foreground">
                    不对?改成
                    <select
                      aria-label="改选行业大类"
                      data-testid="research-category-select"
                      className="h-8 rounded-md border border-input bg-background px-2 text-xs text-foreground"
                      value={categoryKey ?? draft.resolved.category.key}
                      disabled={loadingDraft}
                      onChange={(e) => setCategoryKey(e.target.value)}
                    >
                      {taxonomy!.categories.map((c) => (
                        <option key={c.key} value={c.key}>{c.name}</option>
                      ))}
                    </select>
                  </label>
                )}
              </div>
            )}

            {/* 新鲜度知情(不拦截) */}
            {draft.industry_last_by_others && (
              <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs leading-relaxed text-amber-300">
                该行业 {draft.industry_last_by_others.days_ago} 天前刚有新调研数据,通常无需立即重跑。
              </div>
            )}

            {/* 题目勾选 */}
            <div className="max-h-[38dvh] space-y-3 overflow-y-auto pr-1">
              {selectableCount > 0 && (
                <div className="flex items-center justify-between gap-2">
                  <span className="text-[11px] font-medium text-muted-foreground">
                    调研题目(最多 {MAX_PROMPTS} 题)
                  </span>
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={toggleAllPrompts}
                    className="h-7 px-2 text-[11px]"
                  >
                    {allSelectableSelected ? '取消全选' : '全选'}
                  </Button>
                </div>
              )}
              {(draft.existing_prompts || []).length > 0 && (
                <div className="space-y-1.5">
                  <div className="text-[11px] font-medium text-muted-foreground">平台已有的题目(和自定义题合计最多选 20 题)</div>
                  {draft.existing_prompts.map((p) => (
                    <label
                      key={p.id}
                      className="flex cursor-pointer items-start gap-2 rounded-md border border-border/50 bg-background/60 px-2.5 py-1.5 text-xs hover:border-emerald-400/40"
                    >
                      <Checkbox
                        className="mt-0.5"
                        checked={checkedExisting.has(p.id)}
                        onCheckedChange={(c) => toggleExisting(p.id, c === true)}
                      />
                      <span className="min-w-0 flex-1 break-words leading-snug">{p.text}</span>
                    </label>
                  ))}
                </div>
              )}

              {(draft.suggested_prompts || []).length > 0 && (
                <div className="space-y-1.5">
                  <div className="flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
                    建议题目
                    <Badge variant="secondary" className="px-1 py-0 text-[9px]">AI 推荐</Badge>
                  </div>
                  {draft.suggested_prompts.map((t, i) => (
                    <label
                      key={`sg-${i}`}
                      className="flex cursor-pointer items-start gap-2 rounded-md border border-border/50 bg-background/60 px-2.5 py-1.5 text-xs hover:border-emerald-400/40"
                    >
                      <Checkbox
                        className="mt-0.5"
                        checked={checkedSuggested.has(i)}
                        onCheckedChange={(c) => toggleSuggested(i, c === true)}
                      />
                      <span className="min-w-0 flex-1 break-words leading-snug">{t}</span>
                      {/* [FIX-9] 撞现有题:后端自动复用,不计价 */}
                      {suggestedDupIdx.has(i) && (
                        <Badge variant="secondary" className="shrink-0 px-1 py-0 text-[9px] text-amber-300">已有同题·自动复用</Badge>
                      )}
                    </label>
                  ))}
                </div>
              )}

              {/* 自加题目 */}
              <div className="space-y-1.5">
                <div className="text-[11px] font-medium text-muted-foreground">自定义题目(可选)</div>
                {newTextStatus.map(({ text: t, dropped }) => (
                  <div
                    key={t}
                    className={cn(
                      'flex items-start gap-2 rounded-md border px-2.5 py-1.5 text-xs',
                      dropped
                        ? 'border-border/50 bg-muted/30 text-muted-foreground'
                        : 'border-emerald-500/30 bg-emerald-500/10',
                    )}
                  >
                    <span className="min-w-0 flex-1 break-words leading-snug">
                      {t}
                      {dropped && (
                        <span className="ml-1 text-[10px] text-amber-300">· 与现有/已选题重复,不计入</span>
                      )}
                    </span>
                    <button
                      type="button"
                      onClick={() => removeNewText(t)}
                      className="shrink-0 text-muted-foreground hover:text-foreground"
                      aria-label="移除"
                    >
                      <X className="size-3.5" />
                    </button>
                  </div>
                ))}
                <div className="flex items-center gap-1.5">
                  <Input
                    value={newInput}
                    onChange={(e) => setNewInput(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') {
                        e.preventDefault();
                        addNewText();
                      }
                    }}
                    placeholder="如「XX 哪个品牌好」"
                    className="h-8 text-xs"
                  />
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    onClick={addNewText}
                    disabled={!newInput.trim()}
                    className="h-8 shrink-0 gap-1 px-2.5 text-xs"
                  >
                    <Plus className="size-3.5" /> 添加
                  </Button>
                </div>
              </div>
            </div>

            {/* 总价(与实扣同源:价目表驱动)。基础价含 15 题,第 16 题起每题 +290,
                所以勾选题数**会**改变总价 —— currentPrice 依赖 selectedCount 重算。 */}
            <div
              className={cn(
                'rounded-md border px-3 py-2 text-xs leading-relaxed',
                countValid && pricingReady
                  ? 'border-emerald-500/30 bg-emerald-500/10 text-foreground'
                  : 'border-amber-500/30 bg-amber-500/10 text-amber-300',
              )}
            >
              {!pricingReady ? (
                // [块A] 定价未配置 → 明示不可用,不展示价格(避免"看到价却发起失败")
                '该功能定价未配置,暂不可用,请联系客服。'
              ) : countValid ? (
                <>
                  共 <span className="font-semibold">{selectedCount}</span> 题 · 消耗{' '}
                  <span className="font-semibold text-emerald-400">{currentPrice}</span> 算力
                  <span className="text-muted-foreground">(完成才扣,失败自动退)</span>
                  <br />
                  <span className="text-muted-foreground">约 10-30 分钟,完成后本页自动更新。</span>
                </>
              ) : selectedCount < MIN_PROMPTS ? (
                `请至少勾选 ${MIN_PROMPTS} 道题目`
              ) : (
                `最多 ${MAX_PROMPTS} 题,当前 ${selectedCount} 题,请取消部分`
              )}
            </div>
          </div>
        ) : null}

        {!running && draft && (
          <DialogFooter>
            <Button variant="outline" size="sm" onClick={() => onOpenChange(false)} disabled={submitting}>
              取消
            </Button>
            <Button
              size="sm"
              onClick={handleConfirm}
              disabled={submitting || !canSubmit}
              className="gap-1"
            >
              {submitting ? <Loader2 className="size-3.5 animate-spin" /> : <Zap className="size-3.5" />}
              确认发起 · {currentPrice} 算力
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
