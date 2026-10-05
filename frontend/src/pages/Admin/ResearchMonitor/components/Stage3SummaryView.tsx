/**
 * P14.3 C1 · Stage 3 抓取分类展示组件 (历史列表 chips + 详情面板 card)
 *
 * 老板痛点 (round_20260529_014854 反馈): 历史跑批完成后看不到 stage 3 reasons + error_samples
 * 必须从 summary_json.stage_3 (P14.1 C3 镜像) 持久数据读 · running 时再用 live-status 实时
 *
 * 双 export:
 *   - Stage3SummaryChips: 紧凑横排 4 主类 (新抓/跨行业/复用旧/Jina失败) · 用于列表行下方
 *   - Stage3SummaryCard:  完整 7 类 grid + error_samples · 用于详情 dialog
 */
import { Badge } from '@/components/ui/badge';
import type {
    LiveStatusStage3Summary,
    LiveStatusStage3ErrorSample,
    LiveStatusIntentSummary,
} from '@/lib/researchMonitorApi';

type ReasonKey =
    | 'crawled_new' | 'crawled_dup' | 'reused_existing'
    | 'skipped_short' | 'skipped_jina_failed' | 'skipped_oss_failed' | 'failed_unknown';

const REASON_DEFS: ReadonlyArray<readonly [ReasonKey, string, string]> = [
    ['crawled_new',          '新抓',         'text-green-700 bg-green-50'],
    ['crawled_dup',          '跨行业复用',   'text-indigo-700 bg-indigo-50'],
    ['reused_existing',      '复用旧文章',   'text-blue-700 bg-blue-50'],
    ['skipped_short',        '内容太短',     'text-gray-700 bg-gray-50'],
    ['skipped_jina_failed',  'Jina 失败',    'text-red-700 bg-red-50'],
    ['skipped_oss_failed',   'OSS 失败',     'text-orange-700 bg-orange-50'],
    ['failed_unknown',       '未分类',       'text-purple-700 bg-purple-50'],
] as const;

// 列表行用 · 只显主 4 类 + 非 0 才显
const CHIP_KEYS: ReadonlyArray<ReasonKey> = [
    'crawled_new', 'crawled_dup', 'reused_existing', 'skipped_jina_failed',
] as const;

export function Stage3SummaryChips({ s3 }: { s3: LiveStatusStage3Summary | null | undefined }) {
    if (!s3) return null;
    const processed = s3.processed ?? 0;
    if (processed === 0) return null;
    const chips = CHIP_KEYS.map((k) => {
        const v = s3[k] ?? 0;
        if (!v) return null;
        const def = REASON_DEFS.find((d) => d[0] === k);
        if (!def) return null;
        return (
            <span key={k} className={`text-[10px] rounded px-1.5 py-0.5 ${def[2]}`}>
                {def[1]} {v}
            </span>
        );
    }).filter(Boolean);
    if (chips.length === 0) return null;
    return (
        <div className="flex flex-wrap items-center gap-1 mt-0.5">
            {chips}
        </div>
    );
}

export function Stage3SummaryCard({ s3 }: { s3: LiveStatusStage3Summary | null | undefined }) {
    if (!s3) return null;
    const errSamples: LiveStatusStage3ErrorSample[] = s3.error_samples || [];
    return (
        <div className="space-y-3 border rounded p-3 bg-muted/20">
            <div className="flex items-center justify-between text-sm">
                <span className="font-semibold">Stage 3 · 抓取分类</span>
                <span className="text-[10px] text-muted-foreground tabular-nums">
                    processed {s3.processed ?? 0} / {s3.total_urls ?? 0}
                    {typeof s3.jina_requests === 'number' && (
                        <> · Jina 请求 {s3.jina_requests}</>
                    )}
                </span>
            </div>
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 gap-2 text-[11px]">
                {REASON_DEFS.map(([k, label, cls]) => (
                    <div key={k} className={`rounded px-2 py-1 ${cls} flex items-center justify-between`}>
                        <span>{label}</span>
                        <span className="tabular-nums font-medium">{s3[k] ?? 0}</span>
                    </div>
                ))}
            </div>
            {errSamples.length > 0 && (
                <div className="border-t pt-2">
                    <div className="text-[11px] text-muted-foreground mb-1">
                        失败样本(前 {errSamples.length} 条):
                    </div>
                    <div className="space-y-1">
                        {errSamples.map((e, idx) => (
                            <div key={idx} className="flex items-start gap-2 text-[11px] border-l-2 border-red-300 pl-2 py-0.5">
                                <Badge variant="outline" className="shrink-0 text-[10px]">
                                    {e.error_type}
                                </Badge>
                                <span className="font-mono text-muted-foreground truncate flex-1" title={e.url}>
                                    {e.url}
                                </span>
                                <span className="text-red-700 truncate max-w-[40%]" title={e.error_message}>
                                    {e.error_message}
                                </span>
                            </div>
                        ))}
                    </div>
                </div>
            )}
        </div>
    );
}

/**
 * 从 round 行的 summary_json 字段拎出 stage_3 子对象 · null safe
 * 后端 P14.1 C3 后 list API 已返完整 summary_json · 前端读 stage_3 子字段 fallback
 */
export function extractStage3FromSummary(
    summaryJson: Record<string, unknown> | undefined | null,
): LiveStatusStage3Summary | null {
    if (!summaryJson || typeof summaryJson !== 'object') return null;
    const s3 = (summaryJson as Record<string, unknown>)['stage_3'];
    return s3 && typeof s3 === 'object' ? (s3 as LiveStatusStage3Summary) : null;
}

/**
 * P14.3 C2 · 判定 round 是否有 Stage 3 Jina 失败 · 用于完成态降级显示
 * (老板口径: skipped_jina_failed > 0 即"部分失败" · 不计 skipped_short / reused_existing)
 *
 * 可接受 summary_json 直传 OR 已 extract 出来的 stage3
 */
export function stage3HasJinaFailures(
    input: Record<string, unknown> | LiveStatusStage3Summary | null | undefined,
): boolean {
    if (!input || typeof input !== 'object') return false;
    let s3: LiveStatusStage3Summary | null;
    // 已经是 stage3 (有 skipped_jina_failed 等字段) 直接用 · 否则当 summary_json 抽
    if ('skipped_jina_failed' in input || 'crawled_new' in input) {
        s3 = input as LiveStatusStage3Summary;
    } else {
        s3 = extractStage3FromSummary(input as Record<string, unknown>);
    }
    return (s3?.skipped_jina_failed ?? 0) > 0;
}


export function extractIntentSummaryFromSummary(
    summaryJson: Record<string, unknown> | undefined | null,
): LiveStatusIntentSummary | null {
    if (!summaryJson || typeof summaryJson !== 'object') return null;
    const s = (summaryJson as Record<string, unknown>)['stage_4_5'];
    return s && typeof s === 'object' ? (s as LiveStatusIntentSummary) : null;
}

export function summaryHasIntentFailures(
    input: Record<string, unknown> | LiveStatusIntentSummary | null | undefined,
): boolean {
    if (!input || typeof input !== 'object') return false;
    let summary: LiveStatusIntentSummary | null;
    if ('failed' in input || 'classified' in input) {
        summary = input as LiveStatusIntentSummary;
    } else {
        summary = extractIntentSummaryFromSummary(input as Record<string, unknown>);
    }
    return (summary?.failed ?? 0) > 0;
}
