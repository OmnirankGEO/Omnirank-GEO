/**
 * Monitored keywords table with cluster grouping, compliance, and countdown.
 */
import { useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Checkbox } from '@/components/ui/checkbox';
import { Switch } from '@/components/ui/switch';
import { TrendingUp, TrendingDown, Download, RefreshCw, BarChart3, ArrowUpFromLine, CheckSquare, X, Loader2, Archive, ChevronDown, ChevronRight } from 'lucide-react';
import { authFetch } from '@/lib/api';
import { lazyToast } from '@/lib/lazyToast';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { KeywordCountdown } from './KeywordCountdown';
import type { Keyword } from '../types';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { useTutorialStage, setTutorialStage, type TutorialStage } from '@/sandbox/tutorialStage';
import { SANDBOX_MAGIC_KEYWORDS, SANDBOX_OPT_KEYWORD } from '@/sandbox/constants';
import { LazyFeatureTooltip as FeatureTooltip } from '@/components/onboarding/LazyFeatureTooltip';
import { cn } from '@/lib/utils';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

/** 沙盒 step 4 · 行级 spotlight 上下文 (穿进 renderKeywordRows) */
interface TutorialRowCtx {
    stage: TutorialStage;
    goodKeyword: string;     // 好 case · 高亮这个词(第 1 个魔法词)
    badId: number | null;    // 坏 case · 高亮未达标这行
    recoveredKeyword: string; // 补发后恢复达标的词(高亮它看效果)
}

/* CTO-15.23 Phase 3 · 列分级 · 老板 5/22 报"密度过载 + 响应式打穿"
 * primary 总显示:checkbox / 词条 / 出现率
 * secondary 默认显示(max 4):目标品牌 / 达标 / 状态 / 操作
 * tertiary 默认隐藏(toggle 或 xl+ 显示):见 TERTIARY_COLS */

/**
 * #200 · tertiary 四列名字的**唯一来源**:表头与 toggle 文案都从这里取。
 *
 * 改前这四个名字有**三个源**:四个 `<TableHead>` 里各写死一份、toggle 的 `title` 手抄一份、
 * 上面那行注释又抄一份 —— 而注释那份**已经抄错了**(写「服务期至」,真表头是「已服务」)。
 * 三个源里错了一个还全绿,正是因为没人把它们比过。
 *
 * 🔴 数量也**派生**,不再另写一个 4:名字加一个而 count 忘了改,
 *    按钮就会写「更多 4 列」却列出 5 个名字。
 */
const TERTIARY_COLS = {
    source: '来源',
    change: '变化',
    countdown: '倒计时',
    served: '已服务',
} as const;
const TERTIARY_COL_NAMES: readonly string[] = Object.values(TERTIARY_COLS);
const TERTIARY_COLS_COUNT = TERTIARY_COL_NAMES.length;
/** 列名连起来的那一串(可见文案与 `title` 共用,保证两处同变)。 */
const TERTIARY_COL_LIST = TERTIARY_COL_NAMES.join(' / ');

interface SupplementPreview {
    keyword_id: number;
    keyword: string;
    quote_id: number;
    suggested_articles: number;
    reason_code: string;
    reason_text: string;
    style_plan: Record<string, number>;
    estimated_points: number;
    plan_version: string;
    plan_hash: string;
    target_rate: number;
    recent_rate: number;
}

const SUPPLEMENT_STYLE_LABELS: Record<string, string> = {
    guide: '方法指南',
    comparison: '对比评测',
    risk: '避坑合规',
    price: '价格解读',
    data: '趋势洞察',
    qa: '问答FAQ',
    checklist: '选购清单',
    case: '案例分享',
    story: '品牌故事',
};

function summarizeSupplementStyles(stylePlan: Record<string, number>): string {
    const parts = Object.entries(stylePlan)
        .filter(([, count]) => count > 0)
        .map(([style, count]) => `${SUPPLEMENT_STYLE_LABELS[style] ?? style}${count}`);
    return parts.length > 0 ? parts.join('、') : '无需新增';
}

function createSupplementRequestId(): string {
    return safeRandomUUID().replace(/-/g, '');
}

interface KeywordTableProps {
    keywords: Keyword[];
    selectedKeywords: Set<string>;
    loading: boolean;
    isAdmin: boolean;
    selectedClient: string;
    latestUnsyncedTaskId: number | null;
    syncingTaskId: number | null;
    serviceStartDate?: string | null;
    /** 服务期至 · 后端 contract_end_date(= quotes.service_end_date)· 前端绝不自算 */
    serviceEndDate?: string | null;
    readOnly?: boolean;
    onSelectKeywords: (newSet: Set<string>) => void;
    onSyncTrends: (taskId: number) => void;
    onOpenWeights: () => void;
    onDeleteKeyword: (kw: Keyword) => void;
    onArchiveKeywords: (keywords: Keyword[]) => Promise<void> | void;
    onRefreshKeywords?: () => void;
}

// [服务期 SSOT 2026-08-06] 这里原来有个 calcServiceEndDate(start, service_days) ——
//   前端自己拿【履约达标天数配额】加到起始日上,算出又一个"服务期至"。
//   它跟后端轮换闸读的 service_end_date 毫无关系:11/13 张 paid 报价上两者相差近一年。
//   现在"服务期至"只能来自后端 contract_end_date(唯一 SSOT),前端不许再推算。

function exportKeywordsToExcel(keywords: Keyword[], serviceEndDate?: string | null) {
    const headers = ['词条', '目标品牌', '来源', '出现率', '变化', '达标', '均值', '目标', '还需达标天数', '已达标天数', '达标天数配额', '服务期至'];
    const rows = keywords.map(kw => [
        kw.keyword,
        kw.target_brand,
        kw.source,
        kw.detection_rate != null ? `${kw.detection_rate}%` : '',
        kw.rate_change != null ? `${kw.rate_change > 0 ? '+' : ''}${kw.rate_change}%` : '',
        kw.is_compliant ? '达标' : '未达标',
        kw.effective_rate != null ? `${kw.effective_rate}%` : '',
        kw.target_rate != null ? `${kw.target_rate}%` : '',
        kw.remaining_days != null ? String(kw.remaining_days) : '',
        kw.compliant_days != null ? String(kw.compliant_days) : '',
        kw.service_days != null ? String(kw.service_days) : '',
        serviceEndDate ?? '',
    ]);
    // BOM + CSV
    const csv = '\uFEFF' + [headers, ...rows].map(r => r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `监测词条_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
}

function getKeywordKey(kw: Keyword): string {
    return `${kw.source || 'confirmed'}-${kw.id}`;
}

export function KeywordTable({
    keywords,
    selectedKeywords,
    loading,
    isAdmin,
    selectedClient,
    latestUnsyncedTaskId,
    syncingTaskId,
    serviceStartDate,
    serviceEndDate,
    readOnly = false,
    onSelectKeywords,
    onSyncTrends,
    onOpenWeights,
    onDeleteKeyword,
    onArchiveKeywords,
    onRefreshKeywords,
}: KeywordTableProps) {
    const [showOptimizeDialog, setShowOptimizeDialog] = useState(false);
    const [supplementPreviews, setSupplementPreviews] = useState<Record<number, SupplementPreview>>({});
    const [supplementErrors, setSupplementErrors] = useState<Record<number, string>>({});
    const [supplementLoading, setSupplementLoading] = useState(false);
    const supplementRequestIds = useRef<Record<number, string>>({});
    const [optimizeSubmitting, setOptimizeSubmitting] = useState(false);
    const navigate = useNavigate();
    // [2026-06-04 视觉] 顶部筛选 chip(纯前端视图过滤 · 不改数据/接口/表格逻辑)
    const [filterMode, setFilterMode] = useState<'all' | 'fail' | 'pass' | 'auto'>('all');
    const displayKeywords = filterMode === 'fail' ? keywords.filter(k => k.is_compliant === false)
        : filterMode === 'pass' ? keywords.filter(k => k.is_compliant === true)
        : filterMode === 'auto' ? keywords.filter(k => !!k.is_monitored)
        : keywords;

    // 沙盒 step 4 教程
    const sandboxActive = isSandboxActive();
    const tutorialStage = useTutorialStage();
    const badKw = keywords.find(kw => kw.is_compliant === false);
    const tutorialCtx: TutorialRowCtx | undefined = sandboxActive
        ? { stage: tutorialStage, goodKeyword: SANDBOX_MAGIC_KEYWORDS[0], badId: badKw?.id ?? null, recoveredKeyword: SANDBOX_OPT_KEYWORD }
        : undefined;
    const [archiveSubmitting, setArchiveSubmitting] = useState(false);
    /* CTO-15.23 Phase 3 · 列分级 toggle · 默认 false · 1600+ 自动显示 · 1600 以下 toggle 控制
     * Phase 3.1 (Codex 六审修):xl(1280) → 2xl(1536)
     *   1440 sidebar 后容器 ~1094 < 11 列 1238 · xl 仍横向 scroll
     * Phase 3.2 (Codex 七审修):2xl(1536) → 自定义 min-[1600px]
     *   1536 sidebar 后容器 ~1190 < 11 列 1238 · 仍踩线 scroll
     *   1600+ sidebar 后 ~1254 ≥ 1238 · 真不溢出
     *   Tailwind 任意断点语法 min-[1600px]: · 不污染全局 theme.screens */
    const [showTertiaryCols, setShowTertiaryCols] = useState(false);
    /* tertiary <td>/<th> className · 1600+ 始终显 · 1600 以下看 toggle */
    const tertiaryCls = showTertiaryCols ? '' : 'hidden min-[1600px]:table-cell';
    /* tertiary <col> className · table-fixed 下隐藏 col 元素同步缩列宽 · 否则空列仍占 ~400px */
    const tertiaryColCls = showTertiaryCols ? '' : 'hidden min-[1600px]:table-column';

    // CTO-15.23 2026-05-09 · 自动监测 toggle
    const [confirmDialog, confirmAction] = useConfirmDialog();
    const [togglingId, setTogglingId] = useState<number | null>(null);
    const [batchToggleSubmitting, setBatchToggleSubmitting] = useState(false);

    // CTO-15.23 2026-05-09 老板诉求 · 一键全开/全关监测(代理 N 个 keyword 单点 toggle 太烦)
    const handleBatchToggle = async (enable: boolean) => {
        // [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 P0-A.4] 批量也要带上手动词。
        //   改前 filter 死钉 source==='confirmed' → 手动词批量开关碰不到它,
        //   而它在后台一直跟着跑(唯一闸是 status,库默认 'active')。
        //   🔴 [R5 2026-08-17] 这句已过时:K1/K2 之后**两种词同一个计费模型**
        //   (都是逐词日订阅 130/词/天),唯一差别只剩「合同/手动」这个标签。
        //   留着旧口径 = 界面在对用户说假话(他会按"跑一次扣一次"理解,实际每天都在扣)。
        //   所以分两批发、确认文案分开算钱,不混成一个"每天 X 算力"糊弄过去。
        const candidates = keywords.filter(kw =>
            (enable ? !kw.is_monitored : !!kw.is_monitored)
        );
        if (candidates.length === 0) {
            lazyToast.info(enable ? '所有关键词已开通自动监测' : '没有需要关闭的关键词');
            return;
        }
        const contractKws = candidates.filter(kw => kw.source !== 'extra');
        const extraKws = candidates.filter(kw => kw.source === 'extra');
        const dailyCost = contractKws.length * 130;
        const perRunCost = extraKws.length * 130;
        const costLines = [
            contractKws.length > 0
                ? `合同词 ${contractKws.length} 个 · 每日 09:00 一轮 · ${dailyCost.toLocaleString()} 算力/天`
                : '',
            extraKws.length > 0
                ? `手动词 ${extraKws.length} 个 · 与合同词同样每天跑 · ${perRunCost.toLocaleString()} 算力/天`
                : '',
        ].filter(Boolean).join('\n');
        const ok = enable
            ? await confirmAction({
                title: `一键开通 ${candidates.length} 个关键词自动监测?`,
                description:
                    `${costLines}\n` +
                    `余额不足时自动暂停 · 充值后自动恢复 · 完成才扣(失败不扣)`,
                confirmLabel: '一键开通',
                cancelLabel: '取消',
            })
            : await confirmAction({
                title: `一键关闭 ${candidates.length} 个关键词自动监测?`,
                description:
                    `关闭后不再监测 · 已扣的算力不退(完成才扣模型)\n` +
                    `${costLines}\n可重新开通`,
                confirmLabel: '一键关闭',
                cancelLabel: '取消',
                danger: true,
            });
        if (!ok) return;

        setBatchToggleSubmitting(true);
        try {
            const url = `/api/monitoring/keyword/${enable ? 'batch-enable' : 'batch-disable'}`;
            const batches: { source: string; ids: number[] }[] = [];
            if (contractKws.length) batches.push({ source: 'confirmed', ids: contractKws.map(kw => kw.id) });
            if (extraKws.length) batches.push({ source: 'extra', ids: extraKws.map(kw => kw.id) });

            let cnt = 0, failedCnt = 0, skippedCnt = 0;
            for (const batch of batches) {
                const res = await authFetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ keyword_ids: batch.ids, source: batch.source }),
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.detail?.message || err.detail || `HTTP ${res.status}`);
                }
                const data = await res.json();
                cnt += (enable ? data.enabled_count : data.disabled_count) || 0;
                failedCnt += data.failed_count || 0;
                skippedCnt += data.skipped_count || 0;
            }
            const parts = [`${enable ? '开通' : '关闭'} ${cnt} 个`];
            if (skippedCnt) parts.push(`跳过 ${skippedCnt}`);
            if (failedCnt) parts.push(`失败 ${failedCnt}`);
            lazyToast.success(parts.join(' · '));
            onRefreshKeywords?.();
        } catch (e: any) {
            lazyToast.error(`批量${enable ? '开通' : '关闭'}失败:${e.message || '未知错误'}`);
        } finally {
            setBatchToggleSubmitting(false);
        }
    };

    const handleToggleMonitor = async (kw: Keyword, enable: boolean) => {
        // [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 P0-A.4] 手动词(extra)同样可开可关。
        //   改前这里硬拒 non-confirmed,而后台根本没有任何闸拦它 —— 加一个词就永久在跑、
        //   界面上关不掉(只能整条归档)。现在两种词都有开关,且默认关。
        //   [WO_MANUAL_KEYWORD_PARITY 2026-08-16 K1/K2] 🔴 计费模型已统一,文案必须跟着改:
        //     Owner 2026-08-16 定:手动词 = 合同词,每天跑每天扣 130 算力/词/**天**,
        //     走同一条逐词日订阅(monitoring_keyword_daily),唯一差别是「合同/手动」标签。
        //     改前这里写的是"搭批次跑 · 130/词/**次** · 没有每日订阅" —— 那是 optin 那一版的
        //     模型,parity 之后它是**假话**:用户会按"跑一次扣一次"理解,实际每天都在扣。
        //     元指令 #3(商业模型区分必须明示)在这里的正确落法是「明示它们现在一样」,
        //     不是继续保留一段已经不成立的区分。
        const isExtra = kw.source === 'extra';

        // ══════════════════════════════════════════════════════════════
        // [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §3] 管理员必须当场看见扣谁的钱
        // ══════════════════════════════════════════════════════════════
        // 病史(2026-08-16 生产实证):Owner 用管理员账户给岱林开监测,界面完全不显示
        //   计费主体 ⇒ 在不知情的情况下花掉了服务商 4,290 算力,而且开启时没提示、
        //   扣费时没通知、暂停时只有一行日志 —— 三方都不知道。
        // 🔴 默认必须是「记服务商账」:默认成平台承担 = 任何管理员随手一点都是平台成本敞口。
        // 🔴 非管理员看不到这个二选一(他们只能给自己的品牌开,主体无歧义),UI 与今天一致。
        // 🔴 后端不信前端:server 端对 platform 再校验一次 is_admin,非管理员传 platform 403。
        let billingMode: 'brand_owner' | 'platform' = 'brand_owner';
        if (enable && isAdmin) {
            const usePlatform = await confirmAction({
                title: `「${kw.keyword}」这次开通,费用记谁的账?`,
                description:
                    `默认记服务商账 —— 从该品牌归属人的钱包按 130 算力/词/天 扣。
` +
                    `只有演示、售前等**平台承担**场景才选记平台账。
` +
                    `(选错了可以关掉重开,已扣的不退)`,
                confirmLabel: '记平台账',
                cancelLabel: '记服务商账(默认)',
            });
            billingMode = usePlatform ? 'platform' : 'brand_owner';
        }

        const ok = enable
            ? await confirmAction({
                title: `开通"${kw.keyword}"自动监测?`,
                description: (isExtra ? '这是手动添加的词 · 与合同词一样每天跑\n' : '')
                    // 🔴 [WO_MONITORING_PLATFORM_COVERED 2026-08-16 §3] 最终确认里**明示扣谁的钱**。
                    //   只给一个二选一不够 —— Owner 这次就是"点完了也不知道花的是谁的钱",
                    //   然后在不知情的情况下花掉了服务商 4,290 算力。
                    //   非管理员没有那个二选一,但这里同样明示(他的主体本来就无歧义,明示零成本)。
                    + (billingMode === 'platform'
                        ? '本次记【平台账】· 从平台承担账户扣费\n'
                        : '本次记【服务商账】· 从该品牌归属人的钱包扣费\n')
                    + '每天 09:00 自动跑多平台 AI 检测一轮\n每日每词扣 130 算力 · 完成才扣(失败不扣)\n余额不足时自动暂停 · 充值后恢复',
                confirmLabel: '开通',
                cancelLabel: '取消',
            })
            : await confirmAction({
                title: `关闭"${kw.keyword}"自动监测?`,
                description: '关闭后明天起不再监测\n本周期已扣的算力不退(完成才扣模型)\n明日 09:00 不再 fire'
                    + '\n词条留在列表里 · 随时可以重新打开',
                confirmLabel: '关闭',
                cancelLabel: '取消',
                danger: true,
            });
        if (!ok) return;

        setTogglingId(kw.id);
        try {
            const _params = new URLSearchParams();
            if (isExtra) _params.set('source', 'extra');
            // 只在开通且选了平台账时才带 —— 不带 = 后端默认 brand_owner(默认值锁在后端)
            if (enable && billingMode === 'platform') _params.set('billing_mode', 'platform');
            const _qs = _params.toString();
            const url = `/api/monitoring/keyword/${kw.id}/${enable ? 'enable' : 'disable'}`
                + (_qs ? `?${_qs}` : '');
            const res = await authFetch(url, { method: 'POST' });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                throw new Error(err.detail?.message || err.detail || `HTTP ${res.status}`);
            }
            const data = await res.json();
            if (enable) {
                // extra 没有"每日订阅",days_runway 对它无意义 —— 别把"可跑 N 天"套在按次计费的词上
                lazyToast.success(isExtra
                    ? '已开启 · 每天 09:00 与合同词一起跑(130 算力/天)'
                    : `已开通自动监测 · 当前余额可跑 ${data.days_runway || 0} 天`);
            } else {
                lazyToast.success('已关闭自动监测');
            }
            onRefreshKeywords?.();
        } catch (e: any) {
            lazyToast.error(`${enable ? '开通' : '关闭'}失败:${e.message || '未知错误'}`);
        } finally {
            setTogglingId(null);
        }
    };

    const selectedKws = keywords.filter(kw => selectedKeywords.has(getKeywordKey(kw)));

    // 获取选中的未达标词条
    const selectedFailedKws = keywords.filter(kw => {
        const key = getKeywordKey(kw);
        return selectedKeywords.has(key) && kw.is_compliant === false;
    });
    // 🔴 [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-7②] 本单**未解禁**,理由必须留在这里:
    //   工单要求把手动词纳入「智能补足」。但后端整条链只认 confirmed_keywords ——
    //     · server.py `_require_supplement_keyword_access`:
    //         SELECT quote_id FROM confirmed_keywords WHERE id=%s
    //     · services/smart_article_supplement.build_supplement_preview_from_db:FROM confirmed_keywords ck
    //   两处都只按 id 查、不带来源。
    //   🔴 严重度要写准(工单 §7 措辞纪律):**今天**打过去是 404,不是跨客户越权 ——
    //   extra_keywords id 2–24 / confirmed_keywords id 526–3074,零重叠,查不到行。
    //   **引爆条件 = 两表 id 号段重叠**(与 §1 那颗地雷同源:两表各自独立自增,
    //   手动词再加约 500 个就进入合同词区间)。一旦重叠,手动词 id 会匹配到同 id 的合同词,
    //   而那条可能属于另一个客户 ⇒ 那时才是越权读报价/按别人 quote 计价。
    //   ⇒ 顺序焊死:**后端先 source-aware,前端才解禁**。先解禁 = 把越权面提前打开。
    //   与本单 P0-7① 同一条纪律:renew 的后端串写本单已修,所以那个按钮解禁了;
    //   补足链没修,所以这个不解。后端待办已写进交付单「未完成」一节。
    const selectedConfirmedFailedKws = selectedFailedKws.filter(kw => !kw.source || kw.source === 'confirmed');

    const requestArchive = async (items: Keyword[]) => {
        const uniqueItems = items.filter((kw, index, arr) => arr.findIndex(item => getKeywordKey(item) === getKeywordKey(kw)) === index);
        if (uniqueItems.length === 0) return;
        const label = uniqueItems.length === 1 ? `「${uniqueItems[0].keyword}」` : `${uniqueItems.length} 个词条`;
        if (!(await confirmAction({
            title: `确认归档${label}?`,
            description: '归档后会停止监测；之后可在归档区“恢复”回原状态，或“续费”重新起算服务期。',
            confirmLabel: '归档',
            danger: true,
        }))) return;
        setArchiveSubmitting(true);
        try {
            await onArchiveKeywords(uniqueItems);
        } finally {
            setArchiveSubmitting(false);
        }
    };

    const loadSupplementPreviews = async () => {
        setSupplementLoading(true);
        setSupplementErrors({});
        const nextPreviews: Record<number, SupplementPreview> = {};
        const nextErrors: Record<number, string> = {};
        await Promise.all(selectedConfirmedFailedKws.map(async (kw) => {
            try {
                const res = await authFetch(`/api/writing/optimize-preview/${kw.id}`);
                const data = await res.json().catch(() => ({}));
                if (!res.ok || !data.preview) {
                    const detail = data.detail?.message || data.detail || `HTTP ${res.status}`;
                    throw new Error(typeof detail === 'string' ? detail : '预览失败');
                }
                nextPreviews[kw.id] = data.preview as SupplementPreview;
            } catch (error) {
                nextErrors[kw.id] = error instanceof Error ? error.message : '预览失败';
            }
        }));
        setSupplementPreviews(nextPreviews);
        setSupplementErrors(nextErrors);
        setSupplementLoading(false);
    };

    const openSupplementDialog = () => {
        supplementRequestIds.current = {};
        setShowOptimizeDialog(true);
        void loadSupplementPreviews();
        if (sandboxActive && tutorialStage === 'step4-send') {
            setTutorialStage('step4-opt-confirm');
        }
    };

    const handleOptimizeConfirm = async () => {
        const quoteId = parseInt(selectedClient);
        if (!quoteId) return;
        const actionable = selectedConfirmedFailedKws
            .map(kw => supplementPreviews[kw.id])
            .filter((preview): preview is SupplementPreview => !!preview && preview.suggested_articles > 0);
        if (actionable.length === 0) {
            lazyToast.info('当前没有需要补发的文章');
            return;
        }

        setOptimizeSubmitting(true);
        try {
            let created = 0;
            for (const preview of actionable) {
                const requestId = supplementRequestIds.current[preview.keyword_id]
                    ?? createSupplementRequestId();
                supplementRequestIds.current[preview.keyword_id] = requestId;
                const res = await authFetch('/api/writing/optimize-generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        keyword_id: preview.keyword_id,
                        plan_version: preview.plan_version,
                        plan_hash: preview.plan_hash,
                        client_request_id: requestId,
                    }),
                });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    if (res.status === 409 && data.detail?.preview) {
                        setSupplementPreviews(current => ({
                            ...current,
                            [preview.keyword_id]: data.detail.preview as SupplementPreview,
                        }));
                    }
                    const detail = data.detail?.message || data.detail || `HTTP ${res.status}`;
                    throw new Error(typeof detail === 'string' ? detail : '创建失败');
                }
                created += Number(data.created || 0);
                setSupplementPreviews(current => ({
                    ...current,
                    [preview.keyword_id]: { ...preview, suggested_articles: 0, estimated_points: 0 },
                }));
            }
            lazyToast.success(`已创建 ${created} 篇智能补足选题`);
            setShowOptimizeDialog(false);
            onSelectKeywords(new Set());
            if (sandboxActive) {
                setTutorialStage('step4-opt-write');
                navigate(`/writing?quote_id=${quoteId}&tab=optimize`);
            } else {
                window.location.href = `/writing?quote_id=${quoteId}&tab=optimize`;
            }
        } catch (error) {
            lazyToast.error(`创建失败: ${error instanceof Error ? error.message : '未知错误'}`);
        } finally {
            setOptimizeSubmitting(false);
        }
    };

    return (
        <>
        <Card className="border border-border rounded-xl">
            <CardHeader className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 p-4 sm:p-5">
                <div className="flex flex-col gap-2">
                    <div className="flex items-center gap-3">
                        <CardTitle className="text-lg whitespace-nowrap">监测词条 ({keywords.length})</CardTitle>
                        {selectedKeywords.size > 0 && (
                            <Badge variant="outline" className="text-brand border-brand/30 whitespace-nowrap">
                                已选 {selectedKeywords.size} 项
                            </Badge>
                        )}
                    </div>
                    {keywords.length > 0 && (
                        <div className="flex flex-wrap gap-1.5">
                            {([
                                { k: 'all', label: '全部', n: keywords.length },
                                { k: 'fail', label: '未达标', n: keywords.filter(x => x.is_compliant === false).length },
                                { k: 'pass', label: '达标', n: keywords.filter(x => x.is_compliant === true).length },
                                { k: 'auto', label: '自动监测', n: keywords.filter(x => !!x.is_monitored).length },
                            ] as const).map(f => (
                                <button
                                    key={f.k}
                                    type="button"
                                    onClick={() => setFilterMode(f.k)}
                                    className={cn(
                                        'rounded-full border px-2.5 py-0.5 text-xs transition-colors',
                                        filterMode === f.k
                                            ? 'border-brand/40 bg-brand/10 text-brand font-medium'
                                            : 'border-border text-muted-foreground hover:text-foreground',
                                    )}
                                >
                                    {f.label} {f.n}
                                </button>
                            ))}
                        </div>
                    )}
                </div>
                <div className="flex flex-wrap gap-2">
                    {isAdmin && latestUnsyncedTaskId && (
                        <Button
                            variant="outline"
                            size="sm"
                            className="text-brand border-brand/30 hover:bg-brand/5"
                            disabled={syncingTaskId === latestUnsyncedTaskId}
                            onClick={() => onSyncTrends(latestUnsyncedTaskId)}
                        >
                            {syncingTaskId === latestUnsyncedTaskId ? (
                                <RefreshCw className="h-4 w-4 mr-2 animate-spin" />
                            ) : (
                                <ArrowUpFromLine className="h-4 w-4 mr-2" />
                            )}
                            同步数据
                        </Button>
                    )}
                    {isAdmin && <Button
                        variant="outline"
                        size="sm"
                        className="text-orange-400 border-orange-500/20 hover:bg-orange-500/10"
                        onClick={onOpenWeights}
                    >
                        <BarChart3 className="h-4 w-4 mr-2" />
                        AI占比权重
                    </Button>}
                    {/* [CTO-15.23 2026-05-11] 归档 · 去 isAdmin 守门 · owner/代理也能批量归档自己的词条 */}
                    {selectedKws.length > 0 && (
                        <Button
                            variant="outline"
                            size="sm"
                            className="text-blue-400 border-blue-500/20 hover:bg-blue-500/10"
                            disabled={archiveSubmitting}
                            onClick={() => void requestArchive(selectedKws)}
                        >
                            {archiveSubmitting ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <Archive className="h-4 w-4 mr-2" />}
                            归档所选 ({selectedKws.length})
                        </Button>
                    )}
                    {/* CTO-15.23 2026-05-09 · 一键全开/全关自动监测 */}
                    {(() => {
                        // [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 P0-A.4] 计数含手动词
                        //   (改前只数 confirmed → 手动词既不在计数里也不会被批量操作碰到)
                        const enableable = keywords.filter(kw => !kw.is_monitored).length;
                        const disableable = keywords.filter(kw => !!kw.is_monitored).length;
                        return (
                            <>
                                {enableable > 0 && (
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        className="text-emerald-500 border-emerald-500/30 hover:bg-emerald-500/10"
                                        disabled={batchToggleSubmitting}
                                        onClick={() => void handleBatchToggle(true)}
                                    >
                                        {batchToggleSubmitting ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <CheckSquare className="h-4 w-4 mr-2" />}
                                        一键开启自动监测 ({enableable})
                                    </Button>
                                )}
                                {disableable > 0 && (
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        className="text-amber-500 border-amber-500/30 hover:bg-amber-500/10"
                                        disabled={batchToggleSubmitting}
                                        onClick={() => void handleBatchToggle(false)}
                                    >
                                        {batchToggleSubmitting ? <Loader2 className="h-4 w-4 mr-2 animate-spin" /> : <X className="h-4 w-4 mr-2" />}
                                        一键关闭自动监测 ({disableable})
                                    </Button>
                                )}
                            </>
                        );
                    })()}
                    <FeatureTooltip
                        featureId="sandbox_step4_fix"
                        stepId="first_monitoring"
                        title="第一步:选中没达标的词"
                        content={'点这个按钮, 一键选中所有未达标的词(这里就是那个 35% 的)。\n\n选中后, 屏幕底部会出现“智能补足”操作栏。'}
                        side="bottom"
                        disabled={!sandboxActive || tutorialStage !== 'step4-fix'}
                    >
                        <Button
                            variant="outline"
                            size="sm"
                            onClick={() => {
                                const failedKeys = keywords
                                    .filter(kw => kw.is_compliant === false)
                                    .map(getKeywordKey);
                                onSelectKeywords(new Set(failedKeys));
                                if (sandboxActive && tutorialStage === 'step4-fix') {
                                    setTutorialStage('step4-send');
                                }
                            }}
                            disabled={keywords.filter(kw => kw.is_compliant === false).length === 0}
                        >
                            <CheckSquare className="h-4 w-4 mr-2" />
                            选中未达标 ({keywords.filter(kw => kw.is_compliant === false).length})
                        </Button>
                    </FeatureTooltip>
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={() => exportKeywordsToExcel(keywords, serviceEndDate)}
                        disabled={readOnly}
                        title={readOnly ? '演示案例为只读' : undefined}
                    >
                        <Download className="h-4 w-4 mr-2" />
                        导出Excel
                    </Button>
                    {/* CTO-15.23 Phase 3 · 列分级 toggle · 默认隐藏 4 列 · xl 以下展开看全部 · xl+ 自动显 */}
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={() => setShowTertiaryCols((v) => !v)}
                        className="min-[1600px]:hidden"
                        data-testid="keyword-tertiary-toggle"
                        /* 🔴 #200:折叠态**可见文案**里就要有四个列名 —— 原来只在 `title` 里,
                           手机上没有 hover,等于"多出来的是哪四列"只能靠猜。
                           展开态不再列名字:列头已经在屏幕上了,再列一遍是同一件事说两遍。 */
                        title={showTertiaryCols ? `收起 ${TERTIARY_COLS_COUNT} 列详情(${TERTIARY_COL_LIST})` : `展开 ${TERTIARY_COLS_COUNT} 列详情(${TERTIARY_COL_LIST})`}
                    >
                        {showTertiaryCols ? <ChevronDown className="h-4 w-4 mr-2" /> : <ChevronRight className="h-4 w-4 mr-2" />}
                        {showTertiaryCols ? `收起 ${TERTIARY_COLS_COUNT} 列` : `更多 ${TERTIARY_COLS_COUNT} 列 · ${TERTIARY_COL_LIST}`}
                    </Button>
                </div>
            </CardHeader>
            <CardContent className="p-5 pt-0 overflow-x-auto">
                {/* Phase 3.1:断点改 2xl(1536)· xl(1280)被 sidebar 挤后容器仅 ~1094 < 11 列 1238 还会横向 scroll · Codex 六审复现
                   tertiary 隐藏时 minWidth 从 1080 降到 ~720 防响应式打穿 */}
                <Table className={cn('table-fixed', showTertiaryCols ? 'min-w-[1080px] min-[1600px]:min-w-[1180px]' : 'min-w-[720px] min-[1600px]:min-w-[1180px]')}>
                    <colgroup>
                        {/* [CTO-15.23 2026-05-11] checkbox 列 + 操作列 都不再 admin only · 让代理/owner 也能多选+归档+操作自己的词
                             后端 archive/restore/renew endpoint 已加 owner RBAC 兜底
                             [CTO-15.23 Phase 3 2026-05-22] 4 列(来源/变化/倒计时/已服务)tertiary · xl 以下默认隐藏
                             col 元素也 hidden 缩列宽 · 否则 table-fixed 下空 col 仍占 ~400px */}
                        <col style={{width: '48px'}} />
                        <col style={{width: '250px'}} />
                        <col style={{width: '150px'}} />
                        <col style={{width: '72px'}} className={tertiaryColCls} />
                        <col style={{width: '86px'}} />
                        <col style={{width: '78px'}} className={tertiaryColCls} />
                        <col style={{width: '112px'}} />
                        <col style={{width: '146px'}} className={tertiaryColCls} />
                        <col style={{width: '108px'}} className={tertiaryColCls} />
                        <col style={{width: '96px'}} />
                        <col style={{width: '92px'}} />
                    </colgroup>
                    <TableHeader>
                        <TableRow>
                            <TableHead>
                                <Checkbox
                                    checked={displayKeywords.length > 0 && displayKeywords.every(k => selectedKeywords.has(getKeywordKey(k)))}
                                    onCheckedChange={(checked) => {
                                        if (checked) {
                                            onSelectKeywords(new Set(displayKeywords.map(getKeywordKey)));
                                        } else {
                                            onSelectKeywords(new Set());
                                        }
                                    }}
                                />
                            </TableHead>
                            <TableHead>词条</TableHead>
                            <TableHead>目标品牌</TableHead>
                            {/* tertiary 4 列 · 默认 xl 以下隐藏 · toggle 展开 */}
                            <TableHead className={tertiaryCls}>{TERTIARY_COLS.source}</TableHead>
                            <TableHead className="text-center">
                                {sandboxActive && tutorialStage === 'step4-rate' ? (
                                    <FeatureTooltip
                                        featureId="sandbox_step4_rate"
                                        stepId="first_monitoring"
                                        title="核心指标:出现率"
                                        content={'出现率 = 在 AI 里搜这个词时, 你的品牌被各家 AI 搜索引擎提到/推荐的比例。\n\n注意: 看的是"被不被提到", 不是排名第几。达标线按套餐档定(入门版默认)。\n\n点任意一行还能看它 7 天的趋势曲线。'}
                                        side="top"
                                        nextLabel="看看哪些词达标了 →"
                                        onNext={() => setTutorialStage('step4-good')}
                                    >
                                        <span>出现率</span>
                                    </FeatureTooltip>
                                ) : (
                                    <span className="inline-flex items-center justify-center gap-1">
                                        出现率
                                        <HelpHint title="出现率是什么?跟「排名」啥区别?" side="top">
                                            出现率 = 在 AI 里搜这个词时, 你的品牌被 AI <b>提到/推荐的比例</b>(各家 AI 搜索引擎加权)。
                                            <br />关键: 看的是<b>「有没有被提到」</b>, 不是传统 SEO 的「排第几名」—— AI 搜索没有固定排名榜, 被不被推荐才是核心指标。
                                            <br />达标线按套餐档定。统计近 7 天、首次检出后开始算、自动排除前期铺量。点任意一行可看趋势曲线。
                                        </HelpHint>
                                    </span>
                                )}
                            </TableHead>
                            <TableHead className={cn('text-center', tertiaryCls)}>{TERTIARY_COLS.change}</TableHead>
                            <TableHead className="text-center">
                                <span className="inline-flex items-center justify-center gap-1">
                                    达标
                                    <HelpHint title="「达标」是什么意思?" side="top">
                                        每个词有一条<b>达标线</b>(出现率目标), 按客户买的套餐档定。
                                        <br />这个词近 7 天出现率 <b>≥ 达标线</b> = 达标(绿 ✓);没到 = 未达标(红 ✗), 该给它补发文章。
                                        <br />右边「目标」列就是这条线的具体数值。
                                    </HelpHint>
                                </span>
                            </TableHead>
                            <TableHead className={cn('text-center', tertiaryCls)}>{TERTIARY_COLS.countdown}</TableHead>
                            <TableHead className={cn('text-center', tertiaryCls)} title="该词已累计达标的服务天数">{TERTIARY_COLS.served}</TableHead>
                            <TableHead>状态</TableHead>
                            <TableHead>操作</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {displayKeywords.length === 0 ? (
                            <TableRow data-testid="monitoring-keyword-empty">
                                <TableCell colSpan={11} className="text-center text-muted-foreground py-8">
                                    {loading ? '加载中...' : (filterMode === 'all' ? '暂无监测词条' : '该筛选下暂无词条')}
                                </TableCell>
                            </TableRow>
                        ) : (
                            renderKeywordRows(displayKeywords, selectedKeywords, isAdmin, onSelectKeywords, onDeleteKeyword, serviceStartDate, (kw) => void requestArchive([kw]), togglingId, handleToggleMonitor, tutorialCtx, tertiaryCls)
                        )}
                    </TableBody>
                </Table>
            </CardContent>
        </Card>

        {/* 选中词条浮动操作栏 · [CTO-15.23 2026-05-11] 去 isAdmin 守门 · owner 也能用 */}
        {selectedKws.length > 0 && (
            <div className="fixed bottom-6 left-1/2 -translate-x-1/2 z-50 bg-zinc-900 border border-zinc-700 rounded-xl px-5 py-3 flex flex-wrap items-center justify-center gap-3 shadow-2xl">
                <span className="text-sm text-zinc-300">
                    已选 <span className="text-white font-bold">{selectedKws.length}</span> 个词条
                </span>
                <Button variant="outline" size="sm" disabled={archiveSubmitting} onClick={() => void requestArchive(selectedKws)}>
                    {archiveSubmitting ? <Loader2 className="size-3.5 mr-1.5 animate-spin" /> : <Archive className="size-3.5 mr-1.5" />}
                    归档所选
                </Button>
                {selectedConfirmedFailedKws.length > 0 && (
                    <FeatureTooltip
                        featureId="sandbox_step4_send"
                        stepId="first_monitoring"
                        title="第二步:给它补发文章"
                        content={'点“智能补足”，系统会按计划缺口、出现率差距和行业文体比例给出建议。\n\n确认后进入写作页——这就是完整闭环:发文 → 盯出现率 → 智能补足 → 再盯。'}
                        side="top"
                        disabled={!sandboxActive || tutorialStage !== 'step4-send'}
                    >
                    <Button size="sm" onClick={openSupplementDialog}>
                        <ArrowUpFromLine className="size-3.5 mr-1.5" />
                        智能补足 ({selectedConfirmedFailedKws.length})
                    </Button>
                    </FeatureTooltip>
                )}
            </div>
        )}

        {/* 优化追加配置弹窗 */}
        {showOptimizeDialog && (() => {
            const optConfirmLock = sandboxActive && tutorialStage === 'step4-opt-confirm';
            return (
            <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/60" onClick={() => { if (!optConfirmLock) setShowOptimizeDialog(false); }}>
                <div className="bg-zinc-900 border border-zinc-700 rounded-xl w-full max-w-lg mx-4 max-h-[80vh] flex flex-col" onClick={e => e.stopPropagation()}>
                    {/* 头部 */}
                    <div className="flex items-center justify-between px-5 py-4 border-b border-zinc-800">
                        <h3 className="text-base font-semibold text-white">智能补足文章</h3>
                        {!optConfirmLock && (
                            <button onClick={() => setShowOptimizeDialog(false)} className="text-zinc-400 hover:text-white">
                                <X className="size-4" />
                            </button>
                        )}
                    </div>
                    {optConfirmLock && (
                        <div className="px-5 pt-3 text-xs text-amber-400 leading-5">
                            教程模式: 系统已根据计划缺口、近 7 天出现率和行业文体比例给出建议。确认后进入写作页。
                        </div>
                    )}

                    {/* 只读智能建议；高级区展示计算依据，不把篇数/文体决策转嫁给普通用户 */}
                    <div className="flex-1 overflow-y-auto px-5 py-3 space-y-2">
                        {supplementLoading && (
                            <div className="flex items-center justify-center gap-2 py-10 text-sm text-zinc-400">
                                <Loader2 className="size-4 animate-spin" /> 正在计算计划缺口和文体分布…
                            </div>
                        )}
                        {!supplementLoading && selectedConfirmedFailedKws.map(kw => {
                            const preview = supplementPreviews[kw.id];
                            const error = supplementErrors[kw.id];
                            return (
                                <div key={kw.id} className="rounded-lg bg-zinc-800/50 px-3 py-3">
                                    <div className="flex min-w-0 items-start justify-between gap-3">
                                        <div className="min-w-0 flex-1">
                                            <div className="truncate text-sm font-medium text-zinc-100" title={kw.keyword}>{kw.keyword}</div>
                                            {preview && (
                                                <div className="mt-1 text-sm text-zinc-300">
                                                    建议补 <strong className="text-white">{preview.suggested_articles}</strong> 篇：{summarizeSupplementStyles(preview.style_plan)}
                                                </div>
                                            )}
                                        </div>
                                        {preview && (
                                            <span className="shrink-0 text-xs text-amber-300">约 {preview.estimated_points.toLocaleString()} 算力</span>
                                        )}
                                    </div>
                                    {error && <div className="mt-2 text-xs text-red-400">无法生成建议：{error}</div>}
                                    {preview && (
                                        <details className="mt-2 text-xs text-zinc-400">
                                            <summary className="cursor-pointer select-none hover:text-zinc-200">高级调整与计算依据</summary>
                                            <div className="mt-2 rounded-md border border-zinc-700 p-2 leading-5">
                                                <div>{preview.reason_text}</div>
                                                <div>近 7 天出现率 {preview.recent_rate}% · 目标 {preview.target_rate}%</div>
                                                <div>文体安排：{summarizeSupplementStyles(preview.style_plan)}</div>
                                                <div className="text-zinc-500">建议为只读方案；数据变化时确认前会自动拦截并要求刷新。</div>
                                            </div>
                                        </details>
                                    )}
                                </div>
                            );
                        })}
                    </div>

                    {/* 底部 */}
                    <div className="flex items-center justify-between px-5 py-4 border-t border-zinc-800">
                        <span className="text-sm text-zinc-400">
                            建议共 {Object.values(supplementPreviews).reduce((sum, item) => sum + item.suggested_articles, 0)} 篇 ·
                            约 {Object.values(supplementPreviews).reduce((sum, item) => sum + item.estimated_points, 0).toLocaleString()} 算力
                        </span>
                        <div className="flex gap-2">
                            {!optConfirmLock && (
                                <Button variant="outline" size="sm" onClick={() => setShowOptimizeDialog(false)}>取消</Button>
                            )}
                            <Button
                                size="sm"
                                disabled={optimizeSubmitting || supplementLoading || Object.keys(supplementErrors).length > 0}
                                onClick={handleOptimizeConfirm}
                                className={optConfirmLock ? 'ring-2 ring-amber-400 ring-offset-2 ring-offset-zinc-900 animate-pulse' : undefined}
                            >
                                {optimizeSubmitting ? <Loader2 className="size-3.5 mr-1.5 animate-spin" /> : <ArrowUpFromLine className="size-3.5 mr-1.5" />}
                                {optimizeSubmitting ? '创建中...' : '确认并跳转'}
                            </Button>
                        </div>
                    </div>
                </div>
            </div>
            );
        })()}
        {confirmDialog}
        </>
    );
}

function renderKeywordRows(
    keywords: Keyword[],
    selectedKeywords: Set<string>,
    isAdmin: boolean,
    onSelectKeywords: (newSet: Set<string>) => void,
    onDeleteKeyword: (kw: Keyword) => void,
    serviceStartDate?: string | null,
    onArchiveKeyword?: (kw: Keyword) => void,
    togglingId?: number | null,
    onToggleMonitor?: (kw: Keyword, enable: boolean) => Promise<void> | void,
    tutorial?: TutorialRowCtx,
    /* CTO-15.23 Phase 3 · tertiary 列 className · 同步给 TableCell 跟 header 一起隐藏 */
    tertiaryCls: string = '',
) {
    const hasClusters = keywords.some(kw => kw.cluster_id);
    const groups: { clusterId: number | null; clusterName: string; keywords: Keyword[] }[] = [];

    if (hasClusters) {
        const clusterMap = new Map<number | null, { name: string; kws: Keyword[] }>();
        for (const kw of keywords) {
            const cid = kw.cluster_id ?? null;
            if (!clusterMap.has(cid)) {
                clusterMap.set(cid, { name: kw.cluster_name || (cid ? `主题包${cid}` : '未分组'), kws: [] });
            }
            clusterMap.get(cid)!.kws.push(kw);
        }
        for (const [cid, { name, kws }] of clusterMap) {
            groups.push({ clusterId: cid, clusterName: name, keywords: kws });
        }
    } else {
        groups.push({ clusterId: null, clusterName: '', keywords });
    }

    return groups.flatMap(group => {
        const rows: React.ReactNode[] = [];

        if (hasClusters && group.clusterName) {
            // 用 effective_rate 平均(后端 v2:达标前=瞬时·达标后=历史平滑)·避免代理"昨天 100% 今天 0% 包出现率被拖到 0"
            const monitoringKws = group.keywords.filter(k => k.lifecycle === 'monitoring');
            const groupTotal = monitoringKws.length;
            const groupRate = groupTotal > 0
                ? Math.round(monitoringKws.reduce((sum, k) => sum + (k.effective_rate ?? k.detection_rate ?? 0), 0) / groupTotal)
                : 0;
            rows.push(
                <TableRow key={`cluster-${group.clusterId}`} className="bg-muted/50 hover:bg-muted/50">
                    {/* [CTO-15.23 2026-05-11] checkbox + 操作列总显示 · cluster row colspan 改成固定 11 */}
                    <TableCell colSpan={11} className="py-2">
                        <div className="flex items-center gap-2 text-sm font-medium">
                            <span>📦</span>
                            <span>{group.clusterName}</span>
                            <Badge variant="outline" className="text-xs">{group.keywords.length} 词</Badge>
                            {groupTotal > 0 && (
                                <span className={`text-xs ml-2 ${groupRate >= 80 ? 'text-green-600' : groupRate >= 50 ? 'text-amber-600' : 'text-red-600'}`}>
                                    包出现率 {groupRate}%
                                </span>
                            )}
                        </div>
                    </TableCell>
                </TableRow>
            );
        }

        for (const kw of group.keywords) {
            const keywordKey = getKeywordKey(kw);
            const isSelected = selectedKeywords.has(keywordKey);
            const isTested = kw.detection_rate !== undefined;

            rows.push(
                /* [客户反馈⑤ 2026-08-09 · Review P1-3] 稳定测试钩子:
                   真浏览器锁要断言"写操作期间这些行一直在、不闪空"。 */
                <TableRow key={keywordKey} data-testid={`monitoring-keyword-row-${kw.id}`} className={isSelected ? "bg-brand/5" : ""}>
                    {/* [CTO-15.23 2026-05-11] 行 checkbox 总显示 · 让代理/owner 能多选 */}
                    <TableCell>
                        <Checkbox
                            checked={isSelected}
                            onCheckedChange={(checked) => {
                                const newSet = new Set(selectedKeywords);
                                if (checked) {
                                    newSet.add(keywordKey);
                                } else {
                                    newSet.delete(keywordKey);
                                }
                                onSelectKeywords(newSet);
                            }}
                        />
                    </TableCell>
                    <TableCell className="font-medium truncate">
                        {tutorial && tutorial.stage === 'step4-good' && kw.keyword === tutorial.goodKeyword ? (
                            <FeatureTooltip
                                featureId="sandbox_step4_good"
                                stepId="first_monitoring"
                                title="你发的文章见效了!"
                                content={'还记得刚才视频里 AI 推荐的那两个词吗?\n\n「' + SANDBOX_MAGIC_KEYWORDS[0] + '」「' + SANDBOX_MAGIC_KEYWORDS[1] + '」出现率已经涨到 81% / 74%, 都达标了(绿色✓)。\n\n这就是持续发文的效果。'}
                                side="right"
                                nextLabel="那没达标的呢? →"
                                onNext={() => setTutorialStage('step4-bad')}
                            >
                                <span>{kw.keyword}</span>
                            </FeatureTooltip>
                        ) : tutorial && tutorial.stage === 'step4-bad' && kw.id === tutorial.badId ? (
                            <FeatureTooltip
                                featureId="sandbox_step4_bad"
                                stepId="first_monitoring"
                                title="这个词偏慢, 没达标"
                                content={'「' + kw.keyword + '」才 35%, 还没到 50% 达标线(红色✗)。\n\n才第 10 天就能发现它落后——监测的价值就是早发现早补救, 现在补发文章还来得及, 不用等月底才发现白跑。'}
                                side="right"
                                nextLabel="那怎么补救? →"
                                onNext={() => setTutorialStage('step4-fix')}
                            >
                                <span>{kw.keyword}</span>
                            </FeatureTooltip>
                        ) : tutorial && tutorial.stage === 'step4-recovered' && kw.keyword === tutorial.recoveredKeyword ? (
                            <FeatureTooltip
                                featureId="sandbox_step4_recovered"
                                stepId="first_monitoring"
                                title="补发见效了!这个词也达标了"
                                content={'又过了几天, 你给「' + kw.keyword + '」补发的文章被收录后, 它的出现率从 35% 涨到了 58%, 现在也达标了(绿色✓)。\n\n这就是完整闭环走了一圈的效果。'}
                                side="right"
                                nextLabel="最后:让客户自己看实时数据 →"
                                onNext={() => setTutorialStage('step4-token-card')}
                            >
                                <span>{kw.keyword}</span>
                            </FeatureTooltip>
                        ) : kw.keyword}
                    </TableCell>
                    <TableCell className="truncate">{kw.target_brand || '-'}</TableCell>
                    {/* 来源 · tertiary */}
                    <TableCell className={cn('whitespace-nowrap', tertiaryCls)}>
                        <Badge variant={kw.source === 'confirmed' ? 'default' : 'outline'}>
                            {kw.source === 'confirmed' ? '合同' : '手动'}
                        </Badge>
                    </TableCell>
                    <TableCell className="text-center">
                        {/* [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-8] 开关关着时,徽章不许出现任何
                            现在进行时的"在跑"字样。后端 lifecycle 现在会在开关关闭时给出
                            stopped_detected / stopped_deploying(见 db/monitoring_db.py 生命周期段)。
                            Owner 2026-08-16 截图报的就是"绿色『监测中』+ 开关『已关闭』"并排。 */}
                        {kw.lifecycle === 'stopped_detected' ? (
                            <span className="text-muted-foreground text-sm" title="监测已关闭 · 此前曾被检出">已停(曾检出)</span>
                        ) : kw.lifecycle === 'stopped_deploying' ? (
                            <span className="text-muted-foreground text-sm" title="监测已关闭 · 铺量期未完成">已停(铺量未完成)</span>
                        ) : kw.lifecycle === 'deploying' ? (
                            <span className="text-amber-400 text-sm">铺量中</span>
                        ) : kw.lifecycle === 'monitoring' ? (() => {
                            // [CTO-15.23 2026-05-12 BUG fix] 主展示 effective_rate(累计平均 · 跟 is_compliant 同口径)·
                            // 老板 mental model:"出现率 75% > 目标 50% → 应达标"基于累计判定 · 非今日单点。
                            // 今日 detection_rate < target 时加 ⚠️ 警告标(防客户展开原文今日没看到品牌的不一致感)。
                            const displayRate = kw.effective_rate ?? kw.detection_rate ?? 0;
                            const todayDropped = kw.is_today_dropped === true;
                            const tooltipText = todayDropped
                                ? `⚠️ 今日实时 ${kw.detection_rate}% 低于目标 · 但累计 ${kw.effective_rate}% 已达标`
                                : (kw.effective_rate != null && kw.detection_rate != null && kw.effective_rate !== kw.detection_rate
                                    ? `今日实时 ${kw.detection_rate}% (累计平均 · 服务期平滑)`
                                    : undefined);
                            return (
                                <span
                                    className={`font-semibold ${displayRate >= 80 ? 'text-green-400' : displayRate >= 50 ? 'text-amber-400' : 'text-red-400'}`}
                                    title={tooltipText}
                                >
                                    {displayRate}%{todayDropped && <span className="ml-1 text-amber-400" title={tooltipText}>⚠️</span>}
                                </span>
                            );
                        })() : '-'}
                    </TableCell>
                    {/* 变化 · tertiary */}
                    <TableCell className={cn('text-center', tertiaryCls)}>
                        {kw.rate_change !== undefined && kw.rate_change !== null ? (() => {
                            // [CTO-15.23 2026-05-19 BUG fix · Deploy-CTO 复查铁证]
                            // 后端 get_client_keywords L1734 算 rate_change = 实时 detection_rate(滚动 7 天加权)
                            //                                                   - prev_trend.detection_rate(昨日 trend_stats)
                            // 出现率 列(L653) 主展示 effective_rate(累计平均 · 服务期平滑 · 89%)
                            // 二者口径不同 → 用户看 "89% 累计 + -75% 变化" 矛盾 = 实际是字段口径错位
                            //
                            // 修法 quick fix:
                            //   - clip 异常值 [-99, +99](防 |rc| > 100 显示)
                            //   - 加 tooltip 解释口径 + 显示 prev/current 上下文
                            //   - 累计仍达标时 |rc| 大单日突变给 "波动" 文案 · 不显示惊悚百分比
                            const rcRaw = kw.rate_change;
                            const rc = Math.max(-99, Math.min(99, rcRaw));
                            const clipped = rc !== rcRaw;
                            const isLargeFluct = Math.abs(rc) >= 30;
                            // 累计已达标 + 大幅"下降" → 多半是单日 0 检出(品牌检索量波动)· 不应报警
                            const isCumulCompliantFluct = isLargeFluct && rc < 0 && kw.is_compliant === true;
                            const tooltip = clipped
                                ? `今日实时检出率与昨日趋势对比 · 原值 ${rcRaw}% (clip ±99)`
                                : isCumulCompliantFluct
                                    ? `今日实时 ${kw.detection_rate}% · 昨日趋势 ${(kw.detection_rate ?? 0) - rc}%${'\n'}累计 ${kw.effective_rate}% 仍达标 · 单日波动属正常`
                                    : `今日实时 ${kw.detection_rate}% vs 昨日趋势 ${(kw.detection_rate ?? 0) - rc}%`;
                            // 大幅波动但累计达标 → 灰色"波动"标识 · 不用红色惊悚
                            const colorClass = isCumulCompliantFluct
                                ? 'text-muted-foreground'
                                : (rc >= 0 ? 'text-green-400' : 'text-red-400');
                            return (
                                <span className={colorClass} title={tooltip}>
                                    {isCumulCompliantFluct ? (
                                        <span className="text-xs">波动</span>
                                    ) : (
                                        <>
                                            {rc >= 0 ? <TrendingUp className="h-4 w-4 inline" /> : <TrendingDown className="h-4 w-4 inline" />}
                                            {Math.abs(rc)}%
                                        </>
                                    )}
                                </span>
                            );
                        })() : '-'}
                    </TableCell>
                    <TableCell className="text-center whitespace-nowrap">
                        {kw.lifecycle === 'monitoring' ? (
                            kw.is_compliant ? (
                                <Badge variant="outline" className="bg-green-500/10 text-green-400 border-green-500/20 whitespace-nowrap">
                                    ✓ 达标
                                </Badge>
                            ) : (
                                <Badge variant="outline" className="bg-red-500/10 text-red-400 border-red-500/20 whitespace-nowrap">
                                    ✗ 未达标
                                </Badge>
                            )
                        ) : '-'}
                        {kw.lifecycle === 'monitoring' && (
                            <div className="text-[11px] text-muted-foreground">
                                {kw.effective_rate != null && kw.effective_rate !== kw.detection_rate
                                    ? `均值${kw.effective_rate}% / 目标${kw.target_rate}%`
                                    : `目标${kw.target_rate}%`
                                }
                            </div>
                        )}
                    </TableCell>
                    {/* 倒计时 · tertiary
                        [WJ-24 2026-06-01 老板 A · 双口径分显]
                        主显「服务剩余 X 天」= 合同自然日历(service_remaining_days_natural · 防"剩24天实际已过期")·
                        副显「已达标 N/M 天」= 履约进度(compliant_days/service_days · 保留 Deploy-CTO 5-30 要的每词达标区分)·
                        过期 → 已到期 N 天 · 自然日跑完 → 已交付 · 无合同日期 → 组件内"服务进行中"。
                        (Deploy-CTO 5-30 撤的是"纯替换成日历丢了达标区分";A 主+副两不丢 · 履约仍在副显) */}
                    {/* [v12 item8] 履约口径:到期/交付只由【累计达标天数 compliant_days】决定,不看自然日 service_expired。
                        未达标不消耗服务天数 → 未达标绝不显"已到期";只有 compliant_days>=service_days 才"服务完成";
                        其余进行中始终显履约倒计时(组件内表达"未达标·暂未开始计时")。自然日字段仅兼容保留,不再决定展示。 */}
                    <TableCell className={cn('text-center', tertiaryCls)}>
                        {/* [WO_MANUAL_KEYWORD_PARITY 2026-08-16 P0-8] 同上:关着的词这一格也不许显示
                            "铺量中"或履约倒计时(倒计时暗示"还在跑")。 */}
                        {kw.lifecycle === 'stopped_detected' || kw.lifecycle === 'stopped_deploying' ? (
                            <span className="text-xs text-muted-foreground">已停</span>
                        ) : kw.lifecycle === 'deploying' ? (
                            <span className="text-xs text-amber-400">铺量中</span>
                        ) : kw.lifecycle === 'monitoring' && (kw.service_days || 0) > 0 && (kw.compliant_days || 0) >= (kw.service_days || 0) ? (
                            <span className="text-xs text-green-500" title="累计达标天数已满 · 服务完成">服务完成</span>
                        ) : kw.lifecycle === 'monitoring' ? (
                            <div>
                                <KeywordCountdown
                                    compliantDays={kw.compliant_days || 0}
                                    serviceDays={kw.service_days ?? null}
                                    isCompliant={!!kw.is_compliant}
                                    isStable={!!kw.is_stable}
                                    remainingCompliant={kw.remaining_compliant ?? null}
                                />
                                <div className="w-16 h-1.5 bg-muted rounded-full mt-1 mx-auto">
                                    <div className="h-full bg-brand rounded-full transition-all" style={{width: `${Math.min(kw.compliance_progress ?? 0, 100)}%`}} />
                                </div>
                            </div>
                        ) : '-'}
                    </TableCell>
                    {/* 已服务 · tertiary · [2026-06-04 纯履约口径] 原"服务期至"日历算法 → 显已累计达标天数 */}
                    <TableCell className={cn('text-center whitespace-nowrap', tertiaryCls)}>
                        {kw.lifecycle === 'monitoring' ? (
                            <span className="text-xs text-muted-foreground" title="该词已累计达标的服务天数">
                                已服务 {kw.compliant_days || 0} 天
                            </span>
                        ) : (
                            <span className="text-xs text-muted-foreground">-</span>
                        )}
                    </TableCell>
                    <TableCell className="whitespace-nowrap">
                        {kw.lifecycle === 'monitoring' ? (
                            <Badge
                                variant="outline"
                                className={
                                    kw.is_compliant
                                        ? "bg-blue-500/10 text-blue-400 border border-blue-500/20 whitespace-nowrap"
                                        : "bg-green-500/10 text-green-400 border border-green-500/20 whitespace-nowrap"
                                }
                            >
                                {/* [CTO-15.23 2026-05-10 老板报]
                                    老:is_compliant=TRUE 显"已交付"·但单点达标 1 天 ≠ 服务交付完成
                                    · 跟同行 line 709 真"已交付"(remaining_days<=0)语义撞车
                                    · 客户看到剩 179 天却显"已交付" UX 失真
                                    新:文案改"达标中" · "已交付"留给真正服务期跑完的场景
                                    老板原话:"真完成后自动取消监测才能达成已交付"
                                    · 此版仅文案修 · 后端"到期自动关 is_monitored"逻辑后续单独评估 */}
                                {kw.is_compliant ? "达标中" : "监测中"}
                            </Badge>
                        ) : kw.lifecycle === 'deploying' ? (
                            <Badge variant="outline" className="bg-amber-500/10 text-amber-400 border border-amber-500/20 whitespace-nowrap">铺量中</Badge>
                        ) : isTested ? (
                            <Badge variant="outline" className="bg-green-500/10 text-green-400 border border-green-500/20 whitespace-nowrap">已检测</Badge>
                        ) : (
                            <Badge variant="secondary" className="whitespace-nowrap">待检测</Badge>
                        )}
                        {/* CTO-15.23 2026-05-09 · 自动监测 toggle(消费一次扣一次)
                            [WO_MONITORING_OPTIN_DEFAULT_OFF 2026-08-15 P0-A.4] 去掉 source==='confirmed' 限制:
                            手动词以前没有开关(取词 SQL 里 is_monitored 是硬编码 FALSE),而后台照跑照扣。
                            现在两种来源都渲染开关,状态读真列。*/}
                        {onToggleMonitor && (
                            <div className="flex items-center gap-1.5 mt-1.5">
                                <Switch
                                    checked={!!kw.is_monitored}
                                    disabled={togglingId === kw.id}
                                    onCheckedChange={(checked) => void onToggleMonitor(kw, checked)}
                                    aria-label={kw.is_monitored ? '关闭自动监测' : '开通自动监测'}
                                />
                                <span className="text-[11px] text-muted-foreground">
                                    {togglingId === kw.id ? '处理中...' : kw.is_monitored ? '自动监测·已开启' : '自动监测·已关闭'}
                                </span>
                            </div>
                        )}
                    </TableCell>
                    {/* [CTO-15.23 2026-05-11] 单行操作列总显示 · 用户管自己内容 · 后端 RBAC 兜底防越权 */}
                    <TableCell>
                        <div className="flex flex-col gap-1">
                            {/* [CTO-15.23 2026-05-08 监测归档] 用户决定哪些词停止监测，不再只允许达标词归档 */}
                            <Button
                                variant="ghost"
                                size="sm"
                                className="text-blue-500 hover:text-blue-400 hover:bg-blue-500/10 h-7 text-xs"
                                onClick={() => onArchiveKeyword?.(kw)}
                                title="归档后停止监测 · 可在归档区恢复或续费"
                            >
                                归档
                            </Button>
                            {kw.source === 'extra' && (
                                <Button
                                    variant="ghost"
                                    size="sm"
                                    className="text-red-500 hover:text-red-400 hover:bg-red-500/100/10 h-7 text-xs"
                                onClick={() => onDeleteKeyword(kw)}
                            >
                                删除
                            </Button>
                        )}
                        </div>
                    </TableCell>
                </TableRow>
            );
        }
        return rows;
    });
}
