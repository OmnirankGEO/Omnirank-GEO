// E1 · 行业媒体有效性榜(admin-only)。
//  数据源:answer-entity 飞轮结果侧「哪些媒体/实体被 AI 真实点名推荐」→ 综合有效分排序。
//  🔴 核心价值:每一行都可「溯源」——点开看这条推荐的 AI 原话在哪轮调研、哪个引擎、哪条 query 说的。
//  只读 shadow;数字全真实来自后端;evidence 里 null 字段(媒体有效分 / 平均排名)不渲染假 0。
import { useCallback, useEffect, useState } from 'react';
import { Loader2, RefreshCw, Search, ShieldCheck, Trophy } from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { cn } from '@/lib/utils';
import { apiGet, fmtDate, fmtInt, fmtScore } from './flywheelApi';

// ========== 契约类型(与后端 effectiveness-board / trace 一致)==========

type BoardEvidence = {
  '点名推荐次数'?: number;
  '平均排名'?: number | null;
  '引擎分布'?: string[];
  '媒体有效分'?: number | null;
  '可投放'?: string; // "已绑定" | "参考/待拓展"
  'l2_adopted'?: number | null;
};

type BoardCalibration = {
  basis?: string; // "调研数据" | "调研数据 + 发布效果校准"
  calibrated?: boolean;
  outcome_bonus?: number;
};

type BoardRow = {
  entity_key: string;
  entity_name: string;
  entity_type?: string;
  effectiveness_score?: number;
  mention_count?: number;
  evidence?: BoardEvidence;
  calibration?: BoardCalibration;
  traceable?: boolean;
};

type BoardResponse = {
  status: string;
  shadow_only?: boolean;
  industry_key?: string;
  weeks?: number;
  generated_rows?: number;
  has_data?: boolean;
  source?: string;
  rows?: BoardRow[];
};

type TraceMention = {
  engine?: string;
  recommendation_rank?: number | null;
  recommendation_reasons?: string[];
  evidence_phrases?: string[];
  source_urls?: string[];
  confidence?: number | null;
  query?: string;
  batch_id?: string;
  answer_excerpt?: string;
  created_at?: string;
};

type TraceResponse = {
  status: string;
  shadow_only?: boolean;
  entity_key?: string;
  mentions?: TraceMention[];
};

// ========== 展示映射 ==========

const ENTITY_TYPE_LABEL: Record<string, string> = {
  media: '媒体',
  platform: '平台',
  website: '站点',
  brand: '品牌',
  company: '公司',
  organization: '机构',
  product: '产品',
  person: '人物',
};

const ENGINE_LABEL: Record<string, string> = {
  deepseek: 'DeepSeek',
  'deepseek-v3.2': 'DeepSeek',
  kimi: 'Kimi',
  'kimi-k2': 'Kimi',
  doubao: '豆包',
  doubao_app: '豆包',
  qwen: '通义千问',
  'qwen3-max': '通义千问',
};

function entityTypeLabel(t?: string) {
  if (!t) return '实体';
  return ENTITY_TYPE_LABEL[t] || t;
}

function engineLabel(e?: string) {
  if (!e) return '未知引擎';
  return ENGINE_LABEL[e] || e;
}

function enc(v: string) {
  return encodeURIComponent((v || '').trim());
}

/** 平均排名 number|null → 人话;null 不渲染假值,显破折号。 */
function rankDisplay(v?: number | null) {
  if (v == null || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(1).replace(/\.0$/, '');
}

function isPurchasable(row: BoardRow) {
  return (row.evidence?.['可投放'] || '') === '已绑定';
}

function confidenceDisplay(v?: number | null) {
  if (v == null || !Number.isFinite(Number(v))) return null;
  const num = Number(v);
  // 0-1 归一化置信度 → 百分比;若后端返 0-100 则原样带 %。
  const pct = num <= 1 ? num * 100 : num;
  return `${pct.toFixed(0)}%`;
}

// ========== 溯源弹窗 ==========

function TraceMentionCard({ mention }: { mention: TraceMention }) {
  const reasons = (mention.recommendation_reasons || []).filter(Boolean);
  const phrases = (mention.evidence_phrases || []).filter(Boolean);
  const urls = (mention.source_urls || []).filter(Boolean);
  const conf = confidenceDisplay(mention.confidence);
  return (
    <div className="rounded-xl border bg-card/60 p-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge className="border-emerald-500/30 bg-emerald-500/10 text-emerald-300">
          {engineLabel(mention.engine)}
        </Badge>
        {mention.recommendation_rank != null && Number.isFinite(Number(mention.recommendation_rank)) && (
          <Badge variant="secondary">推荐排名 #{Number(mention.recommendation_rank)}</Badge>
        )}
        {conf && <Badge variant="outline">置信 {conf}</Badge>}
        {mention.batch_id && (
          <span className="text-[11px] text-muted-foreground">
            调研批次 {String(mention.batch_id).replace(/^batch_/, '')}
          </span>
        )}
      </div>

      {mention.query && (
        <p className="mt-3 text-sm">
          <span className="text-muted-foreground">用户问:</span>{' '}
          <span className="font-medium">{mention.query}</span>
        </p>
      )}

      {reasons.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-medium text-muted-foreground">AI 推荐理由</p>
          <ul className="mt-1.5 space-y-1">
            {reasons.map((r, i) => (
              <li key={`${r}-${i}`} className="text-sm leading-6">· {r}</li>
            ))}
          </ul>
        </div>
      )}

      {phrases.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-medium text-muted-foreground">证据短语</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {phrases.map((p, i) => (
              <span key={`${p}-${i}`} className="rounded-md border border-border/60 bg-muted/40 px-2 py-0.5 text-[12px] leading-5">
                “{p}”
              </span>
            ))}
          </div>
        </div>
      )}

      {mention.answer_excerpt && (
        <div className="mt-3 rounded-lg border-l-2 border-emerald-500/50 bg-background/60 px-3 py-2">
          <p className="text-[11px] font-medium text-muted-foreground">AI 原话摘要</p>
          <p className="mt-1 text-sm italic leading-6 text-foreground/90">{mention.answer_excerpt}</p>
        </div>
      )}

      {urls.length > 0 && (
        <div className="mt-3">
          <p className="text-xs font-medium text-muted-foreground">来源链接</p>
          <ul className="mt-1.5 space-y-1">
            {urls.map((u, i) => (
              <li key={`${u}-${i}`} className="truncate">
                <a
                  href={u}
                  target="_blank"
                  rel="noreferrer noopener"
                  className="text-[13px] text-emerald-400 underline underline-offset-2 hover:text-emerald-300"
                >
                  {u}
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      {mention.created_at && (
        <p className="mt-3 text-[11px] text-muted-foreground">采集时间 {fmtDate(mention.created_at)}</p>
      )}
    </div>
  );
}

function TraceDialog({
  open,
  onOpenChange,
  entityName,
  loading,
  mentions,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  entityName: string;
  loading: boolean;
  mentions: TraceMention[];
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl">
        <DialogHeader>
          <DialogTitle>溯源 · {entityName || '媒体实体'}</DialogTitle>
          <DialogDescription>
            这个媒体/实体被 AI 点名推荐的每一条原始证据:哪个引擎、哪条问题、哪轮调研、推荐理由与 AI 原话。仅供内部核对,不进客户页。
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[62vh] space-y-3 overflow-y-auto pr-1">
          {loading ? (
            <div className="flex items-center justify-center gap-2 py-12 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" /> 正在拉取溯源证据…
            </div>
          ) : mentions.length === 0 ? (
            <div className="py-12 text-center text-sm text-muted-foreground">
              暂无可溯源的推荐明细。可能该实体的答案原文尚未抽取,或调研轮次还在积累。
            </div>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">共 {mentions.length} 条推荐证据</p>
              {mentions.map((m, i) => (
                <TraceMentionCard key={`${m.engine}-${m.batch_id}-${i}`} mention={m} />
              ))}
            </>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ========== 主组件 ==========

export function MediaEffectivenessBoard({ industry }: { industry: string }) {
  const [weeks, setWeeks] = useState('4');
  const [loading, setLoading] = useState(false);
  const [board, setBoard] = useState<BoardResponse | null>(null);

  const [traceOpen, setTraceOpen] = useState(false);
  const [traceLoading, setTraceLoading] = useState(false);
  const [traceEntityName, setTraceEntityName] = useState('');
  const [traceMentions, setTraceMentions] = useState<TraceMention[]>([]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const data = await apiGet<BoardResponse>(
        `/media/effectiveness-board?industry=${enc(industry)}&weeks=${enc(weeks)}&limit=30`,
      );
      setBoard(data || null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '有效性榜加载失败');
      setBoard(null);
    } finally {
      setLoading(false);
    }
  }, [industry, weeks]);

  useEffect(() => {
    load();
  }, [load]);

  const openTrace = useCallback(async (row: BoardRow) => {
    setTraceEntityName(row.entity_name || '');
    setTraceMentions([]);
    setTraceOpen(true);
    setTraceLoading(true);
    try {
      const data = await apiGet<TraceResponse>(
        `/media/effectiveness-board/trace?entity_key=${enc(row.entity_key)}&industry=${enc(industry)}&limit=20&weeks=${enc(weeks)}`,
      );
      setTraceMentions((data?.mentions || []).filter(Boolean));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : '溯源明细加载失败');
      setTraceMentions([]);
    } finally {
      setTraceLoading(false);
    }
    // [review fix] deps 必须含 weeks:否则切 8/12 周后溯源仍用旧闭包的 weeks=4,
    // 榜面 mention_count 与溯源证据条数对不上账(同窗对账修复被陈旧闭包打空)。
  }, [industry, weeks]);

  const rows = [...(board?.rows || [])].sort(
    (a, b) => Number(b.effectiveness_score || 0) - Number(a.effectiveness_score || 0),
  );
  const hasData = !!board?.has_data && rows.length > 0;

  return (
    <div className="space-y-4">
      <div className="rounded-lg border bg-card/60 p-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div className="max-w-3xl">
            <div className="flex flex-wrap items-center gap-2">
              <Trophy className="h-4 w-4 text-emerald-300" />
              <h2 className="text-lg font-semibold">行业媒体有效性榜</h2>
              {board?.shadow_only && <Badge variant="secondary">只读 shadow</Badge>}
            </div>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">
              按「被 AI 真实点名推荐」的证据给该行业媒体/实体排综合有效分。每一行都可「溯源」——点开看这条推荐的 AI 原话在哪轮调研、哪个引擎说的。不接管线上推荐、不进客户页。
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className="text-sm text-muted-foreground">统计窗口</span>
            <Select value={weeks} onValueChange={setWeeks}>
              <SelectTrigger className="w-[120px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="4">近 4 周</SelectItem>
                <SelectItem value="8">近 8 周</SelectItem>
                <SelectItem value="12">近 12 周</SelectItem>
              </SelectContent>
            </Select>
            <Button variant="outline" size="icon" onClick={load} disabled={loading} aria-label="刷新有效性榜">
              {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            </Button>
          </div>
        </div>
      </div>

      {loading && !board ? (
        <div className="flex items-center justify-center gap-2 rounded-lg border py-16 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在加载有效性榜…
        </div>
      ) : !hasData ? (
        <div className="rounded-lg border bg-card/40 py-16 text-center">
          <p className="text-sm font-medium">该行业答案实体数据积累中</p>
          <p className="mx-auto mt-2 max-w-md text-xs leading-5 text-muted-foreground">
            还没有足够的「被 AI 点名推荐」证据来生成有效性榜。可切换到「高级 → 答案实体」先跑一轮抽取,或换一个数据更充分的行业与统计窗口。
          </p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-lg border">
          <div className="overflow-x-auto">
            <table className="w-full min-w-[900px] text-sm">
              <thead className="bg-muted/40 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="px-4 py-3">媒体 / 实体</th>
                  <th className="px-4 py-3">综合有效分</th>
                  <th className="px-4 py-3">点名推荐</th>
                  <th className="px-4 py-3">平均排名</th>
                  <th className="px-4 py-3">引擎分布</th>
                  <th className="px-4 py-3">媒体有效分</th>
                  <th className="px-4 py-3">可投放</th>
                  <th className="px-4 py-3 text-right">溯源</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, idx) => {
                  const evidence = row.evidence || {};
                  const engines = (evidence['引擎分布'] || []).filter(Boolean);
                  const mediaScore = evidence['媒体有效分'];
                  const purchasable = isPurchasable(row);
                  const basis = row.calibration?.basis;
                  return (
                    <tr key={row.entity_key} className="border-t align-top">
                      <td className="px-4 py-3">
                        <div className="flex items-center gap-2">
                          <span className="w-5 shrink-0 text-xs font-semibold text-muted-foreground">{idx + 1}</span>
                          <div className="min-w-0">
                            <div className="flex flex-wrap items-center gap-1.5">
                              <span className="font-medium">{row.entity_name}</span>
                              <Badge variant="outline" className="text-[10px]">{entityTypeLabel(row.entity_type)}</Badge>
                            </div>
                            {basis && (
                              <p className="mt-1 text-[11px] text-muted-foreground">
                                依据:{basis}
                                {row.calibration?.calibrated && Number(row.calibration?.outcome_bonus || 0) > 0 && (
                                  <span className="ml-1 text-emerald-400">
                                    (含发布效果校准 +{fmtScore(row.calibration?.outcome_bonus)})
                                  </span>
                                )}
                              </p>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <span className="text-lg font-semibold tabular-nums">{fmtScore(row.effectiveness_score)}</span>
                        <span className="ml-0.5 text-[11px] text-muted-foreground">/100</span>
                      </td>
                      <td className="px-4 py-3 tabular-nums">{fmtInt(row.mention_count)} 次</td>
                      <td className="px-4 py-3 tabular-nums">{rankDisplay(evidence['平均排名'])}</td>
                      <td className="px-4 py-3">
                        {engines.length > 0 ? (
                          <div className="flex flex-wrap gap-1">
                            {engines.map((e) => (
                              <Badge key={e} variant="secondary" className="text-[10px]">{engineLabel(e)}</Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3 tabular-nums">
                        {mediaScore == null || !Number.isFinite(Number(mediaScore))
                          ? <span className="text-muted-foreground">—</span>
                          : fmtScore(mediaScore)}
                      </td>
                      <td className="px-4 py-3">
                        {evidence['可投放'] ? (
                          <Badge
                            variant={purchasable ? 'default' : 'secondary'}
                            className={cn(purchasable && 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300')}
                          >
                            {evidence['可投放']}
                          </Badge>
                        ) : (
                          <span className="text-muted-foreground">—</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-8 gap-1"
                          disabled={row.traceable === false}
                          onClick={() => openTrace(row)}
                        >
                          <Search className="h-3.5 w-3.5" />
                          溯源
                        </Button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <div className="flex items-center gap-2 border-t bg-muted/20 px-4 py-2 text-[11px] text-muted-foreground">
            <ShieldCheck className="h-3.5 w-3.5" />
            综合有效分由「点名推荐次数 + 平均排名 + 引擎覆盖 + 置信度」加权,发布效果回流后再校准。「可投放/媒体资源绑定」仅作独立证据列展示,不进综合有效分。榜单只读,不自动接管投放。
          </div>
        </div>
      )}

      <TraceDialog
        open={traceOpen}
        onOpenChange={setTraceOpen}
        entityName={traceEntityName}
        loading={traceLoading}
        mentions={traceMentions}
      />
    </div>
  );
}
