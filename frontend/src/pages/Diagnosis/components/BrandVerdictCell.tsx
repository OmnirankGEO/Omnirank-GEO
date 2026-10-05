/**
 * BrandVerdictCell · 板块 A(2026-07-22)诊断「疑似提到」人工快速确认
 *
 * 参照 Monitoring/IdentityReviewPanel 交互:
 *   - 五态单元格:YES 提到 / NO 未提到 / PENDING_IDENTITY 疑似提到 /
 *     PROVIDER_UNKNOWN 判定未知 / NOT_COLLECTED 未采集
 *   - 疑似提到格旁直接出现:确认提到 / 确认未提到 / 添加本品牌别名
 *   - 确认后该格与总分/等级就地更新(响应携带重算结果,不重拉整页)
 *
 * 数据全部来自 GET /api/diagnosis/{id}/brand-cells(读 raw_data_json,不调 LLM);
 * 决策走 POST /api/diagnosis/{id}/brand-cells/decision(纯本地重判,零扣费)。
 */
import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { lazyToast } from '@/lib/lazyToast';
import { AlertCircle, Check, FileText, Loader2, RefreshCw, X } from 'lucide-react';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

export type BrandCellState =
    | 'YES'
    | 'NO'
    | 'PENDING_IDENTITY'
    | 'PROVIDER_UNKNOWN'
    | 'NOT_COLLECTED';

export interface BrandCell {
    question: string;
    engine: string;
    state: BrandCellState;
    brand_verdict: string | null;
    brand_detected: boolean;
    matched_text: string | null;
    candidates: string[];
    /**
     * [WO 2026-08-06 §1.2-1] 疑似同品牌变体 —— **不是命中**,是待确认线索。
     *
     * 生产实证(诊断 561):AI 答案通篇写「阿强龙虾」而品牌登记名是「阿强小龙虾」,
     * 判定层判 NO 判得对(差一个字,机器不该替人拍板),但代理**从来看不到**
     * 这条线索 —— 复核层的理由里逐条写着「证据中为'阿强龙虾'」,没有任何界面
     * 把它给到人。这个字段就是把它给到人的那条线。
     */
    near_miss_candidates?: Array<{ display: string; trusted_form?: string | null }>;
    evidence_snippet: string | null;
    answer_summary: string | null;
    /** [P1-8] 该格 AI 原文全文（弹层里看全，判断不再靠 2 行截断） */
    answer_full?: string | null;
    answer_truncated?: boolean;
    /** 判定依据（resolver 给的 reason/method），用人话解释为什么是"疑似" */
    detection_reason?: string | null;
    detection_method?: string | null;
    identity_review_state: string;
    identity_decision_version: number;
    evidence_hash: string | null;
    human_decision: {
        action?: string;
        selected_name?: string | null;
        reason?: string | null;
    } | null;
}

interface BrandCellsResponse {
    diagnosis_id: number;
    brand_id: number;
    cells: BrandCell[];
    aggregates: {
        dimension_stats: Record<string, Record<string, number>>;
        totals: Record<string, number>;
    };
    score: number | null;
    level: string | null;
}

interface DecisionResponse {
    status: string;
    success?: boolean;
    /** FastAPI 422 等场景 detail 可能是数组，消费前必须先判 string */
    detail?: unknown;
    cell?: BrandCell;
    aggregates?: BrandCellsResponse['aggregates'];
    score?: number | null;
    level?: string | null;
}

const ENGINE_LABELS: Record<string, string> = {
    dashscope: '通义千问',
    deepseek: 'DeepSeek',
    doubao: '豆包',
    yuanbao: '元宝',
    kimi: 'Kimi',
};

function engineLabel(engine: string): string {
    return ENGINE_LABELS[engine] || engine;
}

function requestId(): string {
    return safeRandomUUID();
}

const STATE_META: Record<BrandCellState, { label: string; className: string }> = {
    YES: { label: '提到', className: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30' },
    NO: { label: '未提到', className: 'bg-muted text-muted-foreground border-border' },
    PENDING_IDENTITY: { label: '疑似提到', className: 'bg-amber-500/10 text-amber-600 border-amber-500/40' },
    PROVIDER_UNKNOWN: { label: '判定未知', className: 'bg-muted text-muted-foreground border-border' },
    NOT_COLLECTED: { label: '未采集', className: 'bg-transparent text-muted-foreground/70 border-dashed border-border' },
};

interface CellProps {
    cell: BrandCell;
    busy: boolean;
    /** 返回是否保存成功；add_alias 失败时调用方据此回填别名输入 */
    onDecide: (cell: BrandCell, action: 'confirm_yes' | 'confirm_no' | 'add_alias', name?: string) => Promise<boolean>;
}

export function BrandVerdictCell({ cell, busy, onDecide }: CellProps) {
    const [aliasInput, setAliasInput] = useState('');
    // [P1-8] 原文展开态：默认收起（列表可扫读），展开后可读全文
    const [expanded, setExpanded] = useState(false);
    const meta = STATE_META[cell.state] || STATE_META.NOT_COLLECTED;
    const decided = cell.identity_review_state === 'confirmed' || cell.identity_review_state === 'rejected';

    if (cell.state === 'NOT_COLLECTED') {
        return (
            <div className={`rounded-md border px-2 py-1.5 text-xs ${meta.className}`}>
            {engineLabel(cell.engine)} · {meta.label}
            </div>
        );
    }

    return (
        <div
            className={`rounded-md border px-2 py-1.5 text-xs space-y-1.5 ${meta.className}`}
            // [WO 2026-08-07 §1.2] 「一键去确认」的落点。挂在**待确认且未处理**的格上,
            // 让明示处能直接滚到第一个要处理的格 —— 复用现有确认交互,不新建。
            data-identity-pending={cell.state === 'PENDING_IDENTITY' && !decided ? '1' : undefined}
        >
            <div className="flex items-center justify-between gap-2">
                <span className="font-medium">{engineLabel(cell.engine)}</span>
                <Badge variant="outline" className="text-[10px] font-normal">
                    {meta.label}
                    {decided && cell.human_decision ? ' · 已人工确认' : ''}
                </Badge>
            </div>
            {cell.matched_text && (
                <div className="text-foreground/80">
                    命中:<span className="font-medium">{cell.matched_text}</span>
                </div>
            )}
            {cell.state === 'PENDING_IDENTITY' && !decided && (
                <div className="space-y-2">
                    {/*
                      * [P1-8 · 2026-07-26] 证据可读性。
                      * 旧版：evidence_snippet 被 line-clamp-2 截断、原文看不到 →
                      * 代理判不动，"确认提到/确认未提到"点了也是瞎点。
                      * 现在：摘要默认 2 行 + 「看全文」展开完整原文和判定依据；
                      * 两个决策按钮**永远在展开区之外**，任何折叠状态都可点、
                      * 移动端也不会被长文顶出视口。
                      */}
                    {(cell.evidence_snippet || cell.answer_full || cell.answer_summary) && (
                        <div className="space-y-1">
                            <p className={expanded ? 'text-muted-foreground leading-5 whitespace-pre-wrap break-words' : 'line-clamp-2 text-muted-foreground leading-5'}>
                                {expanded
                                    ? (cell.answer_full || cell.evidence_snippet || cell.answer_summary)
                                    : (cell.evidence_snippet || cell.answer_summary)}
                            </p>
                            {expanded && cell.answer_truncated && (
                                <p className="text-[10px] text-muted-foreground/80">原文过长，已截断展示。</p>
                            )}
                            {expanded && (cell.detection_reason || cell.detection_method) && (
                                <p className="text-[10px] text-muted-foreground/80">
                                    判定依据：{cell.detection_method || '本地匹配'}
                                    {cell.detection_reason ? ` · ${cell.detection_reason}` : ''}
                                </p>
                            )}
                            <button
                                type="button"
                                className="inline-flex min-h-6 items-center text-[11px] font-medium text-brand underline-offset-2 hover:underline"
                                aria-expanded={expanded}
                                onClick={() => setExpanded(v => !v)}
                            >
                                <FileText className="mr-1 h-3 w-3" />
                                {expanded ? '收起原文' : '看全文和判定依据'}
                            </button>
                        </div>
                    )}
                    <div className="flex flex-wrap gap-1.5">
                        <Button
                            size="sm"
                            className="h-7 min-w-[92px] px-2 text-xs"
                            disabled={busy}
                            onClick={() => void onDecide(cell, 'confirm_yes', cell.candidates[0] || cell.matched_text || undefined)}
                        >
                            <Check className="mr-1 h-3 w-3" />确认提到
                        </Button>
                        <Button
                            size="sm"
                            variant="outline"
                            className="h-7 min-w-[100px] px-2 text-xs"
                            disabled={busy}
                            onClick={() => void onDecide(cell, 'confirm_no')}
                        >
                            <X className="mr-1 h-3 w-3" />确认未提到
                        </Button>
                    </div>
                    <div className="flex gap-1.5">
                        <Input
                            value={aliasInput}
                            onChange={event => setAliasInput(event.target.value)}
                            placeholder="添加本品牌别名"
                            maxLength={80}
                            className="h-7 text-xs"
                        />
                        <Button
                            size="sm"
                            variant="secondary"
                            className="h-7 px-2 text-xs whitespace-nowrap"
                            disabled={busy || !aliasInput.trim()}
                            onClick={() => {
                                const alias = aliasInput.trim();
                                setAliasInput('');
                                void onDecide(cell, 'add_alias', alias).then((saved) => {
                                    if (!saved) setAliasInput(alias); // 保存失败回填，不丢用户输入
                                });
                            }}
                        >
                            加别名
                        </Button>
                    </div>
                </div>
            )}
            {/*
              * [WO 2026-08-06 §1.2-1] 已判「未提到」但原文里有疑似写法 → 露线索 + 一键确认。
              *
              * 🔴 判定保持 NO 不变(测量诚实:未经确认不算提到)。这里只是把系统
              * 已经知道、却一直丢掉的那条线索交给能拍板的人。确认后走既有闭环
              * (写别名 → 本地重判 → 重算漏斗/总分,零 provider 调用、零扣费)。
              * 🔴 没有候选就整块不渲染 —— 提示要么帮人解决要么不显示。
              */}
            {cell.state === 'NO' && !decided && (cell.near_miss_candidates?.length ?? 0) > 0 && (
                <div className="space-y-1.5 rounded border border-amber-500/40 bg-amber-500/5 px-2 py-1.5">
                    <p className="text-[11px] text-amber-700 dark:text-amber-500">
                        疑似同品牌变体:
                        <span className="font-medium">{cell.near_miss_candidates![0].display}</span>
                        <span className="text-muted-foreground"> · 待确认</span>
                    </p>
                    <div className="flex flex-wrap gap-1.5">
                        <Button
                            size="sm"
                            className="h-7 px-2 text-xs"
                            disabled={busy}
                            onClick={() => void onDecide(
                                cell, 'add_alias', cell.near_miss_candidates![0].display,
                            )}
                        >
                            <Check className="mr-1 h-3 w-3" />是我们，记为别名
                        </Button>
                        <Button
                            size="sm"
                            variant="outline"
                            className="h-7 px-2 text-xs"
                            disabled={busy}
                            onClick={() => void onDecide(
                                cell, 'confirm_no', cell.near_miss_candidates![0].display,
                            )}
                        >
                            <X className="mr-1 h-3 w-3" />不是我们
                        </Button>
                    </div>
                </div>
            )}
            {cell.state === 'PENDING_IDENTITY' && decided && cell.human_decision && (
                <div className="text-muted-foreground">
                    人工确认:{cell.human_decision.selected_name || cell.matched_text || '—'}
                </div>
            )}
        </div>
    );
}

interface SectionProps {
    diagnosisId: number;
    onScoreUpdated?: (score: number | null, level: string | null) => void;
    /** fetch 失败时的降级渲染(原 markdown 模块体) */
    fallback?: ReactNode;
}

export function BrandCellsSection({ diagnosisId, onScoreUpdated, fallback }: SectionProps) {
    const [data, setData] = useState<BrandCellsResponse | null>(null);
    const [loading, setLoading] = useState(true);
    const [failed, setFailed] = useState(false);
    const [savingKey, setSavingKey] = useState<string | null>(null);
    const requestGeneration = useRef(0);
    const decisionRequestIds = useRef<Record<string, string>>({});
    const onScoreUpdatedRef = useRef(onScoreUpdated);
    onScoreUpdatedRef.current = onScoreUpdated;

    const load = useCallback(async (signal?: AbortSignal) => {
        const generation = ++requestGeneration.current;
        setLoading(true);
        try {
            const response = await authFetch(`/api/diagnosis/${diagnosisId}/brand-cells`, { signal });
            if (!response.ok) throw new Error('品牌判定单元格读取失败');
            const payload = await response.json();
            if (generation === requestGeneration.current && payload?.data) {
                setData(payload.data);
                setFailed(false);
            }
        } catch (error) {
            if (signal?.aborted) return;
            if (generation === requestGeneration.current) setFailed(true);
        } finally {
            if (generation === requestGeneration.current) setLoading(false);
        }
    }, [diagnosisId]);

    useEffect(() => {
        setData(null);
        setFailed(false);
        decisionRequestIds.current = {};
        const controller = new AbortController();
        void load(controller.signal);
        return () => {
            controller.abort();
            requestGeneration.current += 1;
        };
    }, [load]);

    const decide = async (cell: BrandCell, action: 'confirm_yes' | 'confirm_no' | 'add_alias', name?: string): Promise<boolean> => {
        const key = `${cell.question}::${cell.engine}`;
        setSavingKey(key);
        // 代际守卫：入口捕获当前代际，响应返回时若已发生过 reload/换诊断则丢弃过期响应
        const generation = requestGeneration.current;
        const idemKey = [diagnosisId, key, cell.identity_decision_version, action, name || ''].join(':');
        const stableRequestId = decisionRequestIds.current[idemKey] || requestId();
        decisionRequestIds.current[idemKey] = stableRequestId;
        try {
            const response = await authFetch(`/api/diagnosis/${diagnosisId}/brand-cells/decision`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    question: cell.question,
                    engine: cell.engine,
                    action,
                    selected_name: name || null,
                    request_id: stableRequestId,
                    expected_version: cell.identity_decision_version,
                }),
            });
            const payload: DecisionResponse = await response.json().catch(() => ({ status: 'error' }));
            if (generation !== requestGeneration.current) return false; // 过期响应：新数据由 load 覆盖
            const detailMessage = typeof payload.detail === 'string' ? payload.detail : '保存失败，请重试';
            if (!response.ok) {
                if (response.status === 409) {
                    lazyToast.error(typeof payload.detail === 'string' ? payload.detail : '确认状态已变化，正在刷新');
                    void load();
                    return false;
                }
                throw new Error(detailMessage);
            }
            delete decisionRequestIds.current[idemKey];
            setData(current => {
                if (!current) return current;
                const nextCells = current.cells.map(value =>
                    value.question === cell.question && value.engine === cell.engine && payload.cell
                        ? payload.cell
                        : value,
                );
                return {
                    ...current,
                    cells: nextCells,
                    aggregates: payload.aggregates || current.aggregates,
                    score: payload.score !== undefined ? payload.score : current.score,
                    level: payload.level !== undefined ? payload.level : current.level,
                };
            });
            if (onScoreUpdatedRef.current && payload.score !== undefined) {
                onScoreUpdatedRef.current(payload.score ?? null, payload.level ?? null);
            }
            lazyToast.success('已保存，分数已同步重算');
            return true;
        } catch (error) {
            if (generation === requestGeneration.current) {
                lazyToast.error(error instanceof Error ? error.message : '保存失败，请刷新后重试');
            }
            return false;
        } finally {
            setSavingKey(null); // savingKey 属本组件实例；409 触发 load 升代际后也必须复位
        }
    };

    if (failed) {
        return (
            <div className="space-y-2">
                <div className="flex items-center gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 px-3 py-2 text-sm text-amber-700 dark:text-amber-400">
                    <AlertCircle className="h-4 w-4 shrink-0" />
                    <span>逐格判定加载失败，已降级为原文展示</span>
                    <Button
                        variant="outline"
                        size="sm"
                        className="h-6 px-2 text-xs ml-auto"
                        disabled={loading}
                        onClick={() => void load()}
                    >
                        <RefreshCw className={`mr-1 h-3 w-3 ${loading ? 'animate-spin' : ''}`} />重试
                    </Button>
                </div>
                {fallback ?? null}
            </div>
        );
    }
    if (loading && !data) {
        return (
            <div className="flex items-center gap-2 text-sm text-muted-foreground py-2">
                <Loader2 className="h-4 w-4 animate-spin" />正在读取品牌判定单元格…
            </div>
        );
    }
    if (!data || data.cells.length === 0) {
        return <>{fallback ?? null}</>;
    }

    const byQuestion = new Map<string, BrandCell[]>();
    for (const cell of data.cells) {
        const list = byQuestion.get(cell.question) || [];
        list.push(cell);
        byQuestion.set(cell.question, list);
    }
    const pendingTotal = data.aggregates?.totals?.pending_identity ?? 0;
    const definiteTotal = data.aggregates?.totals?.total ?? 0;

    return (
        <div className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex items-center gap-2 text-sm">
                    <Badge className="bg-brand text-white">逐格判定</Badge>
                    {pendingTotal > 0 && (
                        <Badge variant="outline" className="border-amber-500/40 text-amber-600">
                            <AlertCircle className="mr-1 h-3 w-3" />{pendingTotal} 格疑似提到待确认
                        </Badge>
                    )}
                    {/*
                      * [WO_UNKNOWN_DENOMINATOR_DISCLOSURE 2026-08-07 · §1.1]
                      * 旧版这里只说"有 N 格待确认",**没说这 N 格不算进出现率** ——
                      * 而那正是最要紧的一句:561 跑了 32 格、分母只认 16 格,
                      * 出现率报 25%,按实跑格数是 12.5%,高了一倍。
                      * 🔴 分母是多少、扣掉多少、为什么,必须在同一个视觉单元里看得到。
                      */}
                    {pendingTotal > 0 && (
                        <span className="text-xs text-muted-foreground">
                            已确认 {definiteTotal} 格 · 出现率按这 {definiteTotal} 格算 ·
                            另 {pendingTotal} 格未计入
                        </span>
                    )}
                    {definiteTotal === 0 && (
                        <span className="text-xs text-muted-foreground">暂无确定样本 · 确认后才会计分</span>
                    )}
                </div>
                <div className="flex items-center gap-1">
                    {/* [WO 2026-08-07 §1.2] 一键去确认 —— 滚到第一个待确认格,
                        用的还是下面那套确认按钮(不新建交互、零扣费)。
                        🔴 N=0 时整个按钮不渲染。 */}
                    {pendingTotal > 0 && (
                        <Button
                            variant="outline"
                            size="sm"
                            data-testid="goto-identity-pending"
                            onClick={() => {
                                document
                                    .querySelector('[data-identity-pending="1"]')
                                    ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                            }}
                        >
                            去确认
                        </Button>
                    )}
                    <Button variant="ghost" size="sm" onClick={() => void load()} disabled={loading}>
                        <RefreshCw className={`mr-1 h-3.5 w-3.5 ${loading ? 'animate-spin' : ''}`} />刷新
                    </Button>
                </div>
            </div>
            <div className="space-y-2">
                {Array.from(byQuestion.entries()).map(([question, cells]) => (
                    <div key={question} className="rounded-lg border border-border p-3 space-y-2">
                        <div className="text-sm font-medium text-foreground break-words">{question}</div>
                        {/* [P1-8] 移动端单列：两个决策按钮和别名输入不会被挤成半截 */}
                        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                            {cells.map(cell => (
                                <BrandVerdictCell
                                    key={`${cell.question}::${cell.engine}`}
                                    cell={cell}
                                    busy={savingKey === `${cell.question}::${cell.engine}`}
                                    onDecide={decide}
                                />
                            ))}
                        </div>
                    </div>
                ))}
            </div>
            {data.score !== null && (
                <p className="text-xs text-muted-foreground">
                    当前 GEO 总分 {data.score}/100 · {data.level || '—'} · 疑似提到格确认后自动重算
                </p>
            )}
        </div>
    );
}

export default BrandVerdictCell;
