import { authFetch } from '@/lib/api';
import { plannedPosts, plannedTotal, slotsNote } from './writingCounts';
/* [WO_218-a1] 「生成标题这次会扣多少」的显示口径 —— 单点,五个入口共用。 */
import {
    chargeText, faceForKeyword, keywordIdsWithoutTopics, parseTopicGenCharge,
    type TitleGenFace, type TopicGenCharge,
} from './topicGenCharge';
// [P4 缺口作战计划 2026-08-08 · B6]
import { fetchPlanItem, parseGapPlanError } from '@/lib/gapPlanApi';

/** 交付计划深链带过来的冻结规格。只用于预填与定位,不驱动任何自动动作。 */
interface GapPlanPrefill {
    planItemId: string;
    targetQuestion: string;
    contentForm: string;
    sellingPoint: string;
    targetDomain: string;
    targetDisplayName: string;
    executable: boolean;
    statusLabel: string;
}
import { copyToClipboard, copyAsyncText } from '@/lib/copyUtils';
import { toast } from 'sonner';
import { GovernanceAlert } from '@/components/ui/governance-alert';
import type { GovernanceAlertAction, GovernanceAlertContract } from '@/contracts/governanceAlert';
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
import { useState, useEffect, useMemo, useRef, useCallback, type ChangeEvent, type DragEvent, type MouseEvent as ReactMouseEvent } from "react";
import { pauseMobileCoach, resumeMobileCoach } from '@/sandbox/mobileCoachPauseStore';
import { useSearchParams } from 'react-router-dom';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { useClientContext } from '@/context/ClientContext';
import { useIsCEndContext } from '@/hooks/useIsCEndContext';
import { useAuth } from '@/context/AuthContext';
import ReactMarkdown from '@/components/SafeMarkdown';
import WritingSettingsDialog from '@/components/WritingSettingsDialog';
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter, DialogDescription } from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import {
    Loader2, Play, RefreshCw, Edit2, Check, X, ArrowLeft, ChevronDown, ChevronRight,
    Eye, Sparkles, Download, CheckSquare, Settings, Undo2,
    ShieldCheck, ShieldAlert, ShieldX, Wrench, Plus, Search, BookOpen, Send,
    AlertCircle, Copy, Link2, FileText, FileUp, Trash2
} from "lucide-react";
import { BrandImageGallery } from '@/components/brand/BrandImageGallery';
// PublishDrawer removed — publishing now uses dedicated /publish page

// v1_3 (CTO-15.1 2026-04-19): 动态价目表 + 大额 toast 确认
// [2026-06-03 全站静默扣费] 去 FeatureCostBadge 前置标价 · 费用走铃铛/站内信/账单事后通知
// useFeatureCost 仍保留:用于余额预检 + freeze hold + confirmLarge 大额误触阈值
import { useFeatureCost } from '@/context/PricingContext';
import { useConfirmLargeDeduction } from '@/hooks/useConfirmLargeDeduction';
// [写作卡死根治 2026-06-08] 余额预检 + 扣费金额前置显示(批量总额 + 余额不足红态)
import { useWallet } from '@/context/WalletContext';
import { FeatureCostBadge } from '@/components/FeatureCostBadge';
import { BridgeBanner } from '@/components/workbench/BridgeBanner';
// Phase 06 (CTO-15.23 2026-05-03 T4) · 快速写作 Dialog · 共享客户录入组件
import { CustomerIntakeDialog } from '@/components/customer-intake';
// v2.10 (CTO-15.23 2026-05-28) · 文章方向配比器(老板拍 A · 高级入口 · 不大面积展现)
import {
    ARTICLE_DIRECTIONS, DEFENSIVE_HINT, defensiveMissingFactsLine, isDefensiveDirection,
    type ArticleDirection,
} from './articleDirections';
import {
    ADD_PEER_PLACEHOLDER, EVIDENCE_ONLY_NOTE, PEER_GROUP_LABEL, PEER_RESEARCH_LABEL,
    peerCounts, peerListTitle, peerModeOptions,
} from './peerCompareLabels';
import { DistributionConfigDialog } from '@/components/writing/DistributionConfigDialog';
import type { CustomerIntakeData, SubmitMode } from '@/components/customer-intake';
import { emitBrandUpdated } from '@/lib/brandProfileEvents';
import { useMarkStepCompleted } from '@/hooks/useMarkStepCompleted';
import { FeatureTooltip } from '@/components/onboarding/FeatureTooltip';
import { HelpHint } from '@/components/onboarding/HelpHint';
import { isSandboxActive } from '@/sandbox/sandboxState';
import { useTutorialStage, setTutorialStage } from '@/sandbox/tutorialStage';
import { shouldRenderTitleGenerationAction } from '@/sandbox/tutorialLayoutContract';
import { useOrganization } from '@/context/OrganizationContext';
import { safeRandomUUID } from '@/lib/safeRandomUUID';

// 写作项目类型
interface WritingProject {
    id: number;
    quote_ids?: number[];
    brand_id?: number | null;
    brand_name: string;
    industry: string;
    keyword_count: number;
    total_required_articles: number;
    monthly_price: number;
    writing_status: string;
    confirmed_at: string;
}

// [P2-2 2026-08-14] 运营助手:读真实计划推导的下一步(规则产出,零 LLM)
interface NextStepInfo {
    available: boolean;
    next_step?: { stage: string; title: string; why: string; cta: string };
    facts?: Record<string, number>;
}

interface DeliverySummary {
    enabled: boolean;
    simple_ui_enabled?: boolean;
    available: boolean;
    project_status?: string;
    message?: string;
    delivery?: {
        contract_total: number;
        completed: number;
        pending: number;
        blocked: number;
        published: number;
    };
    health?: {
        status: 'available' | 'insufficient_information';
        articles_with_evidence_record: number;
        articles_needing_review: number;
        waiting_for_information: number;
        message: string;
    };
    blockers?: Array<{ message: string; owner: string; next_action: string; target_time?: string | null }>;
    next_action?: { label: string; kind: string };
}

// 关键词类型
interface Keyword {
    id: number;
    keyword: string;
    required_articles: number;
    /* [#225 a1] 服务端按交付口径算好的**篇**数(槽 × 每槽篇数,round_half_up 在后端做)。
       前端只读不算 —— 自己算一遍会和媒体组合那屏对不上(取整位置不同)。 */
    planned_posts_default?: number | null;
    /* 🔴 [#225 a1 下半] `posts_done` 服务端也给,但**这里不声明** ——
       它是「已完成/已发布」,与屏幕上那个「已生成 M 篇」(数的是 topics)不是一回事。
       声明一个没人用的字段,下一个人会以为「已生成」就是它。要用的时候再加。 */
    recommended_platforms: string | null;
    final_price: number;
}

interface ArticleAdvisoryFinding {
    code?: string;
    message?: string;
    evidence?: string;
    excerpt?: string;
    severity?: string;
    // [WP9-P0-7 ②] span 级"AI 仅修此处"的定位锚(精确命中串)
    matched_text?: string;
    // [写作质量总工单 2026-07-29] §13 合同的人话三件套,后端已经在给,
    // 前端此前只读 message → 用户只看到"有问题"不知道"为什么/怎么办"。
    reason?: string;
    impact?: string;
    repair_hint?: string;
}

interface ArticleQualityWarning {
    evidence?: { soft?: ArticleAdvisoryFinding[] };
    evidence_precision?: { warnings?: ArticleAdvisoryFinding[] };
    // [写作质量总工单 2026-07-29 · A-3] 客户存在感与篇幅合同的产出侧结论。
    // 后端修过一次仍不达标时必须让代理**看得见**,否则又是"后端修了、前端命中 0"。
    client_presence?: { findings?: ArticleAdvisoryFinding[] };
    length_compliance?: { findings?: ArticleAdvisoryFinding[] };
    client_presence_unresolved?: string[];
    length_unresolved?: string[];
    // [WP9-P0-7 · D8] 文章层零阻断:违法绝对化不再拒存,降为定位标注 + needs_legal_fix
    // 草稿态;用户可"AI 修复此处"或忽略(放行权归用户),仅对外发布时才会被拦。
    evidence_legal?: { hard?: ArticleAdvisoryFinding[] };
    needs_legal_fix?: boolean;
    human_continue?: {
        acknowledged?: boolean;
        actor_user_id?: number | null;
        acknowledged_at?: string;
    };
    // [工单 C-4] 审核自动驾驶记录:hard 自动修复轮数/结果(可追溯,一条不少)
    auto_repair?: {
        auto_rounds?: number;
        manual_rounds?: number;
        repaired_count?: number;
        remaining_count?: number;
        records?: { code?: string; matched_text?: string; ok?: boolean; reason?: string }[];
        last_trigger?: string;
        last_at?: string;
    };
}

// [工单 C-3 T2 2026-07-27] 后端 findings 同类聚合视图(services/article_findings_aggregate)。
interface FindingSpan {
    matched_text: string;
    excerpt: string;
    evidence_ids?: string[];
    // [span 级 AI 免费修复 2026-07-30 · §2.1] 后端算出的"这一处能不能给 AI 修":
    // 医疗/法律/金融高风险 = false → AI 修复按钮**不渲染**(只留人工签发/手动改)。
    // 前端不自己判高风险 —— 判定口径只有后端 repair_route 一份。
    ai_repairable?: boolean;
    ai_repair_block_reason?: string;
    span_level?: boolean;
}

interface FindingsAggregateCard {
    code: string;
    severity: string;
    count: number;
    title: string;
    message: string;
    source: string;
    spans: FindingSpan[];
    repairable_count: number;
    // 可定位且允许 AI 修的处数(高风险已剔除)。缺省(老缓存)时回落 repairable_count。
    ai_repairable_count?: number;
}

// 选题类型(v2.7.1 加 user_choice + style_code 单一权威字段)
interface Topic {
    id: number;
    keyword_id: number;
    original_keyword: string;
    optimized_title: string;
    article_style: string;  // 历史字段(向后兼容)
    style_code?: string;    // v2.7.1 单一权威 style 字段
    user_choice?: UserChoice;  // 新写入只使用六个 outward family
    style_family?: Exclude<UserChoice, 'auto'>;
    is_fixed?: boolean;     // v2.7.1 系统 fixed slot(company_profile)标识
    status: string;
    article_id?: number;
    completed_at?: string;  // 完成时间
    reviewed_at?: string;   // 审核时间（null表示未审核，显示NEW标签）
    article_version?: number;  // 文章版本号，>1 表示重写过
    article_updated_at?: string;  // 文章最后更新时间
    is_optimize?: boolean;  // 是否为优化选题
    fail_reason?: string;   // 生成失败原因
    generation_request_id?: string;
    generation_error_code?: string;
    generation_error_message?: string;
    generation_retryable?: boolean;
    generation_failure_phase?: string;
    generation_refund_status?: string;
    generation_refund_message?: string;
    generation_revision?: number;
    article_review_status?: 'approved' | 'pending_human_review' | 'rewrite_required' | 'blocked' | 'legacy_unreviewed';
    article_human_review_status?: 'approved' | 'rejected' | null;
    article_review?: { quality_score?: number; confidence_grade?: string; evidence_summary?: { verified_count?: number } };
    evidence_manifest_hash?: string | null;
    publication_profile?: PublicationProfile;
    platform_review?: Record<string, unknown> | null;
    publication_eligible?: boolean;
    publication_eligibility_reason?: string;
    publication_eligibility_message?: string;
    // [P3a 批量审核 2026-08-01 · 工单 §6] 三态直传前端。P1 已经把它们塞进 topics
    // 响应(db/diagnosis_db.py 的 topic["review_state"]/["advisory_state"]/
    // ["advisory_open_count"]/["publication_h0_state"]),但写作大厅这边的类型里没声明,
    // 于是拿不到 —— 批量面板要按"卡在哪一档"分组,只有一个 eligible bool 是分不出来的。
    review_state?: 'not_run' | 'computed';
    advisory_state?: 'none' | 'open' | 'acknowledged' | 'repaired';
    advisory_open_count?: number;
    publication_h0_state?: 'clear' | 'legal_hard' | 'platform_profile_hard' | 'operator_hard';
    // [P3a · Owner 2026-08-01 口径] 内容审核主路径改为 AI 评估自动处理,人工界面
    // 只承载 L3(AI 判不了 + 法律真硬门)。AI 评估层本身是另一个包
    // (WORKORDER_AI_REVIEW_REPLACES_HUMAN_2026-08-01),**不归本包实现** ——
    // 这里只预留接口位,由那个包填。
    // 🔴 字段缺席时前端**不编**:如实显示"AI 尚未给出结论",绝不拿"没有结论"
    //    渲染成"AI 说没问题"(那会让人在 L3 卡片上一键放过法律硬门)。
    ai_review?: {
        verdict?: string;        // AI 结论标签(由 AI 评估包定义取值)
        summary?: string;        // 给人看的一句话预核对摘要
        needs_human?: boolean;   // AI 自己声明"这条我判不了" → L3
        decided_at?: string;
    } | null;
    quality_warning?: ArticleQualityWarning | null;
    findings_aggregate?: FindingsAggregateCard[];
    // [span 级 AI 免费修复 2026-07-30 · §2.1] 整篇级:医疗/法律/金融高风险题材
    // → 全篇不给 AI 修复出口(逐处按钮、一键修复本类、批量一键修复都不渲染)。
    ai_repair_blocked?: boolean;
}

// 历史值继续可读；新写作界面只暴露六个 GEO 文体家族 + 系统推荐。
type UserChoice = ArticleDirection;
type PublicationProfile = 'standard' | 'sohu_geo_strict_v1';
type CompetitorMode = 'real' | 'semi' | 'evidence_only';

// [工单 C-2 T1 2026-07-27] 占位符显示/交付兜底:渲染副本(contentRendered/content_export)
// 缺席或请求失败时,绝不把 [CLIENT_IMAGE]/[NEED_IMAGE] 内部占位符裸露给用户或打进下载包。
// content 本体不动(编辑态仍用原文,配图管理靠它数占位),只在"给人看/给人拿走"的面剥离。
function stripInternalPlaceholders(text: string): string {
    return text
        .replace(/[ \t]*\[(?:CLIENT_IMAGE|NEED_IMAGE)\b[^\]]*\][ \t]*/g, '')
        .replace(/\n{3,}/g, '\n\n');
}

function normalizeCompetitorMode(raw: string | null | undefined): CompetitorMode {
    if (raw === 'real' || raw === 'semi') return raw;
    return 'evidence_only';
}

/*
 * 🔴 [#185] 这份清单原来是**手写的第二份**(配比对话框里还有一份
 *    `USER_CHOICE_LABELS`)。加一档新方向时只改一处,就会出现
 *    「配比里能选、单篇下拉里没有」这种半截状态,而两边各自都"看起来对"。
 *    现在两处都从 `articleDirections.ARTICLE_DIRECTIONS` 派生 —— 一个谓词一处。
 */
const USER_CHOICE_OPTIONS: { value: UserChoice; label: string; desc: string }[] =
    ARTICLE_DIRECTIONS.map(d => ({ value: d.value, label: d.label, desc: d.desc }));

const LEGACY_USER_CHOICE_TO_FAMILY: Record<string, UserChoice> = {
    qa: 'evidence_qa',
    comparison: 'multi_brand_comparison',
    guide: 'implementation_guide',
    checklist: 'implementation_guide',
    risk: 'trend_policy_risk',
    price: 'case_data_roi',
    data: 'case_data_roi',
    case: 'case_data_roi',
    story: 'company_facts',
};

function normalizeUserChoice(raw: string | null | undefined): UserChoice {
    if (!raw || raw === 'auto') return 'auto';
    if (USER_CHOICE_OPTIONS.some(option => option.value === raw)) return raw as UserChoice;
    return LEGACY_USER_CHOICE_TO_FAMILY[raw] ?? 'auto';
}

// [Owner 2026-08-09] 由**选题页展示出来的那个体裁标签**反查 user_choice。
//
// 要解决的是「展示 A、生成 B」:选题卡上写着「选购与多品牌比较」,但请求体里
// 没带 user_choices,后端就按行业权重抽签,交付出来可能是别的文体。
//
// 🔴 刻意**不新建第三张映射表**:展示标签本身就是 `humanizeStyle()` 算出来的,
// 这里再用 `USER_CHOICE_OPTIONS` 的中文 label 反查回去 ——
// 「展示什么就默认什么」由**构造**保证,而不是靠两张表碰巧写得一样。
// 新增一个 style_code 只要进了 `__STYLE_HUMAN_FACING__`,这条路自动跟着走。
function normalizeStyleLabel(s: string): string {
    return s.replace(/[（(][^）)]*[）)]\s*$/, '').trim();
}
function userChoiceFromDisplayedStyle(raw: string | null | undefined): UserChoice {
    const human = humanizeStyle(raw || '');
    if (!human) return 'auto';
    const target = normalizeStyleLabel(human);
    const options = USER_CHOICE_OPTIONS.filter(o => o.value !== 'auto');
    const exact = options.find(o => normalizeStyleLabel(o.label) === target);
    if (exact) return exact.value;
    // 展示名与选项名一长一短时按前缀对上(例:展示「趋势、政策与风险」对选项
    // 「趋势、政策与风险分析」)。对不上就回 auto —— 宁可回落抽签,不猜。
    const prefixed = options.find(o => {
        const label = normalizeStyleLabel(o.label);
        return label.startsWith(target) || target.startsWith(label);
    });
    return prefixed ? prefixed.value : 'auto';
}

type StyleBearingTopic = {
    id?: number; user_choice?: string | null; style_family?: string | null;
    style_code?: string | null; article_style?: string | null;
};

/** 徽章展示读的那串字段。**唯一一处定义** —— 三个调用点都取它,免得再漂。 */
function displayedStyleSource(t: StyleBearingTopic): string {
    return (t.style_family || t.style_code || t.article_style || '') as string;
}

/** 这道选题**当前生效**的体裁:用户选过就是用户的,没选过就是徽章上那个。
 *
 * 🔴 徽章 / 下拉框默认值 / 请求体三处必须同源。改这个功能之前,
 * 同一屏上三处读的字段优先级各不相同(徽章读 style_family||style_code||article_style,
 * 下拉框只读 user_choice||style_family,请求体只读 user_choice)——
 * 这就是「展示 A 生成 B」的直接成因。现在收敛成这一个函数。
 */
function effectiveUserChoice(t: StyleBearingTopic): UserChoice {
    const explicit = normalizeUserChoice(t.user_choice);
    if (explicit !== 'auto') return explicit;              // ① 用户说了算(保留自由度)
    return userChoiceFromDisplayedStyle(displayedStyleSource(t));  // ② 展示什么默认什么
}

/** 「开始写作」请求体里的 `user_choices`。抽出来是为了能被单独断言。 */
function buildUserChoicesMap(
    allTopics: (StyleBearingTopic & { id: number })[],
    writableIds: number[],
): Record<number, string> {
    const writable = new Set(writableIds);
    const map: Record<number, string> = {};
    allTopics.forEach(t => {
        if (!writable.has(t.id)) return;
        const choice = effectiveUserChoice(t);
        // ③ 既没选过、徽章也没标签 → 不塞,保留现行行业配比抽签(Owner 明示保留)
        if (choice !== 'auto') map[t.id] = choice;
    });
    return map;
}

// 选题级六文体选择器；只在文章进入写作前展示。
/**
 * 🔴 [#185] 防御型徽章与缺事实提示放在**这个组件里面**,不放在三个调用点。
 *    本页有三处渲染它(7345 / 7405 / 7659)—— 在调用点各加一遍,
 *    必然有一处会漏(这正是 D5 那条判据说的"徽章有、提示行没有")。
 */
function TopicStyleSelector(props: {
    topicId: number;
    currentValue?: UserChoice;
    industry?: string;
    isCompanyFixedSlot?: boolean;
    /** 服务端给的缺事实项名(#185 c5)。没给就什么都不显示 —— 不编。 */
    missingFacts?: string[] | null;
    onChange?: (newChoice: UserChoice) => void;
}): JSX.Element {
    const { currentValue = 'auto', industry, isCompanyFixedSlot, missingFacts, onChange } = props;
    if (isCompanyFixedSlot) {
        return (
            <span className="text-xs px-2 py-1 rounded bg-muted text-muted-foreground" title="企业介绍每套餐自动生成 1 篇,无需手动选择">
                企业介绍
            </span>
        );
    }
    const isMedicalOrLegal = industry === '医疗健康' || industry === '法律商务';
    const select = (
        <select
            className="text-xs h-7 rounded border border-border bg-card px-2"
            value={normalizeUserChoice(currentValue)}
            onChange={(e) => onChange?.(e.target.value as UserChoice)}
            title={isMedicalOrLegal ? '医疗与法律内容必须使用可核验证据，不生成无依据榜单' : '切换后将按所选文体重生成标题'}
        >
            {USER_CHOICE_OPTIONS.map((opt) => (
                <option key={opt.value} value={opt.value}>{opt.label}</option>
            ))}
        </select>
    );
    if (!isDefensiveDirection(currentValue)) return select;
    /*
        🔴 防御型这一档改的是**题的主语**(题 = 公司名 + 问法),不只是写法。
        下拉里选中之后光看标题看不出这件事已经生效,所以给一枚徽章 + 一句说明。
        缺事实那一行只在服务端真的给了缺项名时才出现 —— 没给就不显示,
        「资料不足」那种话等于没说。
    */
    return (
        <span className="inline-flex flex-col items-start gap-1" data-testid="topic-defensive-wrap">
            <span className="inline-flex items-center gap-1.5">
                {select}
                <span className="shrink-0 rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[11px] text-foreground"
                    data-testid="topic-defensive-badge" title={DEFENSIVE_HINT}>
                    🛡 防御型
                </span>
            </span>
            {defensiveMissingFactsLine(missingFacts) && (
                <span className="text-[11px] text-amber-600 dark:text-amber-300"
                    data-testid="topic-defensive-missing">
                    {defensiveMissingFactsLine(missingFacts)}
                </span>
            )}
        </span>
    );
}

// v2.7.6 删除 BatchStyleBar 模式 toggle · 改为"每个待写 topic 默认显示 dropdown"
// 根因:老板真机看 v2.7.5 UX 反直觉(用户必须点"手动选择方向"才能改单 topic)·
//      v2.7.6 改回 dropdown 始终可见 · 默认 auto · 进阶玩家直接选 · 普通用户 0 操作

// 补充要求输入框(批量级 · 200 字上限)
function ExtraInstructionInput(props: {
    value: string;
    onChange: (v: string) => void;
}): JSX.Element {
    const { value, onChange } = props;
    return (
        <input
            value={value}
            onChange={(e) => onChange(e.target.value.slice(0, 200))}
            placeholder="例:突出价格、突出案例、不要写榜单、强调本地服务"
            maxLength={200}
            className="min-h-[40px] w-full rounded-lg border border-border bg-card px-3 text-xs"
        />
    );
}

// 把 article_style / style_code 工程词翻译成 human-facing 名(v2.7.1 G15 守护 · 用户面 0 暴露 ranking_v2 等)
const __STYLE_HUMAN_FACING__: Record<string, string> = {
    ranking_v2: '选购与多品牌比较',
    authority_ranking: '选购与多品牌比较',
    recommendation_review: '选购与多品牌比较',
    buying_guide: '方法与实施指南',
    trojan_horse: '趋势、政策与风险',
    qa_recommendation: '证据型问答',
    brand_softarticle: '企业事实与品牌说明',
    company_profile: '企业事实与品牌说明',
    comparison_review: '选购与多品牌比较',
    risk_compliance: '趋势、政策与风险分析',
    price_roi: '案例、数据与 ROI',
    data_report: '案例、数据与 ROI',
    evidence_qa: '证据型问答',
    multi_brand_comparison: '选购与多品牌比较',
    implementation_guide: '方法与实施指南',
    trend_policy_risk: '趋势、政策与风险分析',
    case_data_roi: '案例、数据与 ROI',
    company_facts: '企业事实与品牌说明',
    证据选型: '选购与多品牌比较',
    方法指南: '方法与实施指南',
    对比评测: '选购与多品牌比较',
    趋势洞察: '趋势、政策与风险分析',
    价格解读: '案例、数据与 ROI',
    案例分享: '案例、数据与 ROI',
    问答FAQ: '证据型问答',
    品牌故事: '企业事实与品牌说明',
    公司深度报道: '企业事实与品牌说明',
};
function humanizeStyle(raw: string | undefined): string {
    if (!raw) return '';
    return __STYLE_HUMAN_FACING__[raw] ?? raw;
}

/**
 * [写作质量总工单 2026-07-29 · D-1 / D-3] 文章审核态徽章的唯一样式与文案表。
 *
 * D-3(文案与行为不符,优先级高于其他 D 项):`blocked` 旧文案是红色「已阻断」,
 * 但同一段代码的注释与新版卡片正文都写着「草稿照存**不阻断**;只在对外发布时会被拦」。
 * 代理看到红色"已阻断"会以为文章废了、钱白花了,事实是草稿完好、发布前改一处即可。
 * 现在与卡片正文统一口径:**发布前需修改**。
 *
 * D-1(暗色对比度):`bg-X-100 + text-X-800` 是为白底设计的配色对,压在暗色卡片上
 * 必然低对比(截图实证)。这里改成语义 token + 成对 `dark:` 变体,不再逐个手调。
 */
const REVIEW_STATUS_BADGES: Record<string, { label: string; className: string; hint: string }> = {
    approved: {
        label: '审核通过',
        className: 'bg-emerald-100 text-emerald-900 dark:bg-emerald-500/20 dark:text-emerald-200',
        hint: '已通过发布前检查',
    },
    /* [核验流融合 §3C-2/§3C-5 · 2026-08-01] 三条徽章的**文案与行为已经对不上了**。
       §3A 按 Owner 裁定把内容类硬门全部降为提示级之后:
         · "待人工确认" —— 没有任何东西在等人确认,发布不再被它拦住;
         · "需要你确认一处后再对外发布" / "若不修改,仅在对外发布时会被拦下"
           —— 这两句现在是**假话**。
       这正是本文件 D-3 注释记过的同一类事故(代理看到红字以为文章废了、钱白花了),
       只是方向反过来:那次是"说得比实际重",这次会变成"说了一件不存在的事"。
       所以三条一起收敛为**提示级**口径:说清"有什么问题、要不要处理由你定"。 */
    pending_human_review: {
        label: '有待核查项',
        className: 'bg-amber-100 text-amber-900 dark:bg-amber-500/20 dark:text-amber-100',
        hint: '草稿已保存;有一处建议你看一眼,不处理也可以直接发布',
    },
    rewrite_required: {
        label: '渠道提示',
        className: 'bg-orange-100 text-orange-900 dark:bg-orange-500/20 dark:text-orange-100',
        hint: '草稿已保存;当前渠道对这篇有意见,是否调整由你决定,不影响发布',
    },
    blocked: {
        label: '合规提示',
        // 实测:text-destructive(#EF4444)在明/暗两侧都只有 3.3-3.5:1,不达 WCAG AA 4.5:1。
        className: 'bg-red-100 text-red-900 dark:bg-red-500/20 dark:text-red-200',
        hint: '草稿已正常保存,正文一字未改;可能涉及广告法风险,建议修正后再发 —— 发不发由你决定',
    },
    legacy_unreviewed: {
        label: '待审核',
        // 实测:muted-foreground on muted 在亮色下只有 4.35:1,差一点点也算不达标。
        className: 'bg-muted text-foreground',
        hint: '尚未跑发布前检查',
    },
};

function ArticleReviewBadge({ topic }: { topic: Topic }): JSX.Element | null {
    if (topic.status !== 'completed') return null;
    const score = topic.article_review?.quality_score;
    const evidenceCount = topic.article_review?.evidence_summary?.verified_count;
    const detail = [
        typeof score === 'number' ? `发布前质量 ${score} 分` : null,
        typeof evidenceCount === 'number' ? `已核验证据 ${evidenceCount} 条` : null,
        topic.publication_eligibility_message || topic.publication_eligibility_reason || null,
    ].filter(Boolean).join(' · ');
    if (topic.article_human_review_status === 'approved') {
        return <Badge className="bg-emerald-100 text-emerald-800 text-xs shrink-0 dark:bg-emerald-500/20 dark:text-emerald-200" title={detail}>人工签发</Badge>;
    }
    const status = topic.article_review_status || 'legacy_unreviewed';
    const config = REVIEW_STATUS_BADGES[status] || REVIEW_STATUS_BADGES.legacy_unreviewed;
    return (
        <Badge className={`${config.className} text-xs shrink-0`} title={detail || config.hint}>
            {config.label}
        </Badge>
    );
}

function evidenceAdvisoryFindings(topic: Topic): ArticleAdvisoryFinding[] {
    const warning = topic.quality_warning;
    if (!warning) return [];
    return [
        ...(Array.isArray(warning.evidence?.soft) ? warning.evidence!.soft! : []),
        ...(Array.isArray(warning.evidence_precision?.warnings)
            ? warning.evidence_precision!.warnings!
            : []),
        // [写作质量总工单 2026-07-29 · A-3] 客户存在感 / 篇幅合同的产出侧结论。
        // 后端已经"命中即重写一次",但**二次仍不达标时必须有用户可达面** ——
        // 否则又是本月踩过的"后端修了、前端命中 0"。
        ...(Array.isArray(warning.client_presence?.findings)
            ? warning.client_presence!.findings!
            : []),
        ...(Array.isArray(warning.length_compliance?.findings)
            ? warning.length_compliance!.findings!
            : []),
    ];
}

/**
 * [写作质量总工单 2026-07-29 · D-4] 正文片段的规范截断。
 *
 * 旧代码 `repair_hint: 定位:${finding.evidence}` 把**证据原文片段**直接当"定位"渲染,
 * 既不截断也不加省略号。截图实证渲染成「定位：头工厂模式发展白皮书》(2026年6月发布)
 * 〔EV-001〕」—— 开头被吃掉、从句子中间起,读起来像乱码。
 * 现在:主视线给规范截断(首尾省略号),完整原文进 tooltip(repair_hint_full)。
 */
const LOCATION_CLIP_CHARS = 48;

function clipLocation(raw: string | undefined): string {
    const text = String(raw || '').replace(/\s+/g, ' ').trim();
    if (!text) return '';
    if (text.length <= LOCATION_CLIP_CHARS) return text;
    return `${text.slice(0, LOCATION_CLIP_CHARS)}…`;
}

/** 把一条 finding 变成 §13 合同 —— 新旧两套渲染从此只剩这一条路径(D-5)。 */
function findingToContract(
    finding: ArticleAdvisoryFinding,
    fallback: { reason: string; impact: string; actions: GovernanceAlertAction[] },
): GovernanceAlertContract {
    const location = finding.evidence || finding.excerpt || '';
    const clipped = clipLocation(location);
    return {
        code: (finding.code || 'ARTICLE_FINDING').toUpperCase(),
        message: finding.message || finding.code || '这里有一处需要确认。',
        reason: finding.reason || fallback.reason,
        impact: finding.impact || fallback.impact,
        repair_hint: [
            finding.repair_hint || '',
            clipped ? `原文这一处：「${clipped}」` : '',
        ].filter(Boolean).join(' · ') || undefined,
        repair_hint_full: location || undefined,
        actions: fallback.actions,
        rule_version: 'article-finding-v1',
    };
}

// [WP9-P0-7 ② · D8] 违法绝对化 findings:定位标注 + 「AI 修复此处 / 忽略」(放行权归用户)。
// [工单 C-4 2026-07-27 · 审核自动驾驶] Owner:"客户又不看又不懂,叫他们审核只是增加负担。"
// hard 红线:生成期已自动修一轮(后端);仍有残留 → 这里**只出一张汇总卡**
// "本篇有 N 处需要处理 · [一键修复] [查看详情]",不再平铺逐条卡。
// 一键修复 = 服务端批量重试(累计上限 2 轮,平台侧成本);详情弹窗逐项高亮定位
// + [AI 修复] [手动改] [忽略];修复失败沿用 C-3 失败治理(原因 + 重试)。
// 全部修好(或本来就没有)且有自动修复记录 → 显示"审核通过 · 已自动修复 N 处"。

// 高亮定位:matched_text 锚在原文,渲染后 markdown 记号会消失 —— 取剥掉记号后的
// 特征片段,逐级缩短(24→16→10 字)在渲染 DOM 的文本节点里找,命中即 <mark> + 滚动。
function locateAndHighlight(container: HTMLElement, anchor: string): boolean {
    for (const mark of Array.from(container.querySelectorAll('mark[data-testid="finding-highlight"]'))) {
        const parent = mark.parentNode;
        if (!parent) continue;
        parent.replaceChild(document.createTextNode(mark.textContent || ''), mark);
        parent.normalize();
    }
    const core = anchor.replace(/[#*`>\[\]()|_~-]/g, '').replace(/\s+/g, '').trim();
    if (!core) return false;
    const walker = document.createTreeWalker(container, NodeFilter.SHOW_TEXT);
    for (const size of [24, 16, 10, 6]) {
        const needle = core.slice(0, size);
        if (!needle) continue;
        walker.currentNode = container;
        let node = walker.nextNode();
        while (node) {
            const text = (node.textContent || '').replace(/\s+/g, '');
            if (text.includes(needle)) {
                const mark = document.createElement('mark');
                mark.setAttribute('data-testid', 'finding-highlight');
                mark.className = 'rounded bg-amber-200 px-0.5 dark:bg-amber-500/40';
                node.parentNode?.replaceChild(mark, node);
                mark.appendChild(node);
                mark.scrollIntoView({ block: 'center', behavior: 'smooth' });
                return true;
            }
            node = walker.nextNode();
        }
    }
    return false;
}

function ArticleLegalFindings(props: {
    topic: Topic;
    onRepaired: () => void;
}): JSX.Element | null {
    const { topic, onRepaired } = props;
    const [busy, setBusy] = useState(false);
    const [failure, setFailure] = useState<GovernanceAlertContract | null>(null);
    const [detailOpen, setDetailOpen] = useState(false);
    const [detailBody, setDetailBody] = useState<string>('');
    const [ignoredIdx, setIgnoredIdx] = useState<Set<number>>(new Set());
    const [spanBusy, setSpanBusy] = useState<number | null>(null);
    const [spanFailures, setSpanFailures] = useState<Record<number, string>>({});
    const proseRef = useRef<HTMLDivElement | null>(null);
    const findings = Array.isArray(topic.quality_warning?.evidence_legal?.hard)
        ? topic.quality_warning!.evidence_legal!.hard!
        : [];
    const autoRepair = topic.quality_warning?.auto_repair;
    if (topic.status !== 'completed' || !topic.article_id) return null;
    if (findings.length === 0) {
        // 自动驾驶修干净了:用户零操作,只留一枚可追溯的"审核通过"记录徽章。
        if ((autoRepair?.repaired_count || 0) > 0) {
            return (
                <span
                    className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-600 dark:text-emerald-300"
                    data-testid="auto-repaired-badge"
                    title={`系统已自动修复 ${autoRepair!.repaired_count} 处红线表述,修复记录见质量记录`}
                    onClick={(event) => event.stopPropagation()}
                >
                    审核通过 · 已自动修复 {autoRepair!.repaired_count} 处
                </span>
            );
        }
        return null;
    }

    const roundsLeft = Math.max(0, 2 - (autoRepair?.manual_rounds || 0));
    // 后端 article_ai_repair_blocked 的结论(整篇级)。老缓存缺字段时按 false 处理,
    // 端点仍会再拒一次 —— 两层同源,不会因为前端旧版就把高风险放行。
    const aiRepairBlocked = topic.ai_repair_blocked === true;

    // [T1-3] 一键修复 = 服务端批量一轮(轮数硬闸在服务端,前端只做展示)。
    const autoRepairAll = async () => {
        if (!topic.article_id || busy) return;
        setBusy(true);
        setFailure(null);
        try {
            const res = await authFetch(`/api/articles/${topic.article_id}/auto-repair`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({}),
            });
            const data = await res.json();
            if (data?.success) {
                if ((data.remaining || []).length === 0) {
                    toast.success(`已修复 ${data.repaired_count} 处,本篇审核通过`);
                } else {
                    toast(`修复完成:成功 ${data.repaired_count} 处 · 剩余 ${data.remaining.length} 处(可查看详情)`);
                }
                onRepaired();
            } else if (data?.code) {
                setFailure(data as GovernanceAlertContract);  // 轮数用尽等:诚实失败带出口
            } else {
                toast.error('这次没能完成修复,正文未改动');
            }
        } catch {
            toast.error('修复请求失败,正文未改动');
        } finally {
            setBusy(false);
        }
    };

    const openDetail = async () => {
        setDetailOpen(true);
        try {
            const res = await authFetch(`/api/articles/${topic.article_id}`);
            const data = await res.json();
            // 占位符是内部信号,详情正文不裸显(与 C-2 交付面口径一致;C-2 合入后可换用
            // 其 stripInternalPlaceholders 公共函数,这里刻意不重名以免合并冲突)。
            const raw: string = data?.article?.content_export ?? data?.article?.content ?? '';
            setDetailBody(
                raw
                    .replace(/[ \t]*\[(?:CLIENT_IMAGE|NEED_IMAGE)\b[^\]]*\][ \t]*/g, '')
                    .replace(/\n{3,}/g, '\n\n'),
            );
        } catch {
            setDetailBody('');
        }
    };

    // 详情弹窗内的单处修复(C-3 段级链;失败治理:原因 + 重试)。
    const repairSpan = async (finding: ArticleAdvisoryFinding, index: number) => {
        if (!topic.article_id || !finding.matched_text) return;
        setSpanBusy(index);
        try {
            const res = await authFetch(`/api/articles/${topic.article_id}/repair-finding`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    finding_code: finding.code || 'absolute_first_claim',
                    matched_text: finding.matched_text,
                }),
            });
            const data = await res.json();
            if (data?.success) {
                toast.success('这一处已修复');
                setSpanFailures((prev) => { const next = { ...prev }; delete next[index]; return next; });
                onRepaired();
                void openDetail();
            } else {
                setSpanFailures((prev) => ({
                    ...prev,
                    [index]: String(data?.message || data?.reason || '修复未完成,正文未改动'),
                }));
            }
        } catch {
            setSpanFailures((prev) => ({ ...prev, [index]: '修复请求失败,正文未改动' }));
        } finally {
            setSpanBusy(null);
        }
    };

    const visible = findings.map((f, i) => [f, i] as const).filter(([, i]) => !ignoredIdx.has(i));

    return (
        <div className="max-w-full" onClick={(event) => event.stopPropagation()}>
            {/* 汇总卡:不再平铺 N 张 */}
            <div
                className="flex flex-wrap items-center gap-2 rounded-md border border-amber-300/60 bg-amber-500/10 px-2 py-1 text-xs"
                data-testid="hard-summary-card"
            >
                <span className="font-medium text-amber-700 dark:text-amber-300">
                    本篇有 {findings.length} 处需要处理
                </span>
                {/* [span 级 AI 免费修复 2026-07-30 · §2.1 / 锁 6] 医疗/法律/金融高风险:
                    AI 修复按钮**不渲染**。它缺的是人工签发,不是措辞 —— 让 AI 软化
                    措辞会让判定转绿而风险一点没减。 */}
                {aiRepairBlocked ? (
                    <span className="text-amber-700 dark:text-amber-300" data-testid="high-risk-no-ai-repair">
                        医疗/法律/金融高风险内容不做 AI 修复,请手动改或提交人工签发
                    </span>
                ) : (
                    <Button
                        size="sm"
                        variant="outline"
                        className="h-6 px-2 text-xs"
                        disabled={busy || roundsLeft <= 0}
                        data-testid="auto-repair-btn"
                        /* [核验流融合 §3C-4 · 2026-08-01] Owner:"为什么分几轮?一轮修完不行吗?"
                           轮数上限**内部化**:`roundsLeft` 仍然管着 disabled(服务端 2 轮上限
                           照旧,防不收敛),但**一个数字都不许渲染出来**。
                           "剩 N 轮"是内部实现概念泄漏到 UI —— 客户不需要知道我们内部重试几次,
                           他只需要知道"点一下,能修的都修好"。锁 5 对 UI 源码做 0 命中断言。 */
                        title={roundsLeft > 0
                            ? 'AI 自动修复本篇提示(免费,平台承担成本)'
                            : '本篇的自动修复已达上限,请点「查看详情」手动处理'}
                        onClick={() => void autoRepairAll()}
                    >
                        {busy ? '修复中…' : '一键修复'}
                    </Button>
                )}
                <Button
                    size="sm"
                    variant="ghost"
                    className="h-6 px-2 text-xs"
                    data-testid="view-findings-btn"
                    onClick={() => void openDetail()}
                >
                    查看详情
                </Button>
            </div>
            {failure && <div className="mt-1"><GovernanceAlert contract={failure} onAction={() => false} /></div>}

            {/* 详情弹窗:剩余项清单 + 正文滚动定位高亮 + [AI 修复][手动改][忽略] */}
            {detailOpen && (
                <div
                    role="dialog"
                    aria-modal="true"
                    className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
                    data-testid="hard-detail-dialog"
                    onClick={() => setDetailOpen(false)}
                >
                    <div
                        className="flex h-[min(80vh,48rem)] w-[min(64rem,95vw)] flex-col overflow-hidden rounded-lg border border-border bg-background shadow-xl"
                        onClick={(event) => event.stopPropagation()}
                    >
                        <div className="flex items-center justify-between border-b border-border px-4 py-2">
                            <span className="text-sm font-medium">需处理清单({visible.length} 处)· 点击条目在正文中定位</span>
                            <Button size="sm" variant="ghost" className="h-6 px-2 text-xs" onClick={() => setDetailOpen(false)}>关闭</Button>
                        </div>
                        <div className="flex min-h-0 flex-1">
                            <div className="w-72 shrink-0 space-y-2 overflow-y-auto border-r border-border p-3 text-xs">
                                {visible.length === 0 && (
                                    <p className="text-muted-foreground">没有剩余需要处理的条目。</p>
                                )}
                                {visible.map(([finding, index]) => (
                                    <div key={index} className="rounded-md border border-border/70 p-2" data-testid="hard-detail-item">
                                        <button
                                            className="text-left font-medium text-foreground underline-offset-2 hover:underline"
                                            data-testid="locate-finding-btn"
                                            onClick={() => {
                                                if (proseRef.current) {
                                                    const hit = locateAndHighlight(
                                                        proseRef.current,
                                                        finding.matched_text || finding.evidence || finding.excerpt || '',
                                                    );
                                                    if (!hit) toast('这一处在正文中未能精确定位,可用右侧全文手动查找');
                                                }
                                            }}
                                        >
                                            「{clipLocation(finding.matched_text || finding.evidence) || finding.code}」
                                        </button>
                                        <p className="mt-1 text-muted-foreground">{finding.message || '需要改为有依据的相对表述。'}</p>
                                        {spanFailures[index] && (
                                            <p className="mt-1 text-red-500" data-testid="span-repair-failure">失败:{spanFailures[index]}</p>
                                        )}
                                        <div className="mt-1.5 flex flex-wrap gap-1.5">
                                            {/* [§2.1 / 锁 6] 高风险类:这颗按钮不渲染。 */}
                                            {!aiRepairBlocked && (
                                                <Button
                                                    size="sm"
                                                    variant="outline"
                                                    className="h-6 px-2 text-xs"
                                                    data-testid="span-ai-repair-btn"
                                                    disabled={spanBusy !== null || !finding.matched_text}
                                                    onClick={() => void repairSpan(finding, index)}
                                                >
                                                    {spanBusy === index ? '修复中…' : spanFailures[index] ? '重试这一处' : 'AI 修复'}
                                                </Button>
                                            )}
                                            <Button
                                                size="sm"
                                                variant="ghost"
                                                className="h-6 px-2 text-xs"
                                                title="关闭弹窗后在文章预览里点「编辑」修改这一处"
                                                onClick={() => { setDetailOpen(false); toast('请在文章预览中点「编辑」手动修改'); }}
                                            >
                                                手动改
                                            </Button>
                                            <Button
                                                size="sm"
                                                variant="ghost"
                                                className="h-6 px-2 text-xs"
                                                onClick={() => setIgnoredIdx((prev) => new Set(prev).add(index))}
                                            >
                                                忽略
                                            </Button>
                                        </div>
                                    </div>
                                ))}
                            </div>
                            <div className="min-w-0 flex-1 overflow-y-auto p-4">
                                <div ref={proseRef} className="prose prose-sm max-w-none dark:prose-invert" data-testid="hard-detail-body">
                                    <ReactMarkdown>{detailBody}</ReactMarkdown>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

// [工单 C-3 T2/T3 2026-07-27] findings 同类聚合类型卡。
// Owner:"几十处独立卡片毫无意义,应该聚合+一键修复,还有修复失败"。
// 生产实况深档文每篇 61-88 处 → 按 code 聚合成类型卡(标题=问题类型+计数),
// 展开看位置清单;可定位的 span 支持「一键修复本类」(段级修复循环+进度),
// 失败逐条显示原因并可重试;全类操作:全部忽略本类 / 本类已确认(本机标记)。

// 客户端兜底聚合:后端 findings_aggregate 缺席(老缓存/其它入口)时由 quality_warning 现算。
function aggregateFindingsClientSide(topic: Topic): FindingsAggregateCard[] {
    const grouped = new Map<string, FindingsAggregateCard>();
    for (const finding of evidenceAdvisoryFindings(topic)) {
        const code = finding.code || 'article_finding';
        let card = grouped.get(code);
        if (!card) {
            card = {
                code,
                severity: finding.severity || 'advisory',
                count: 0,
                title: finding.message || code,
                message: finding.message || '',
                source: 'client_side',
                spans: [],
                repairable_count: 0,
            };
            grouped.set(code, card);
        }
        card.count += 1;
        card.spans.push({
            matched_text: finding.matched_text || '',
            excerpt: finding.excerpt || finding.evidence || '',
        });
        if (finding.matched_text) card.repairable_count += 1;
    }
    return Array.from(grouped.values());
}

// 全类操作的本机标记(忽略/已确认):按 文章id:类型code 存 localStorage,刷新不丢。
const FINDINGS_CLASS_STATE_KEY = 'omnirank_findings_class_state_v1';

function readFindingsClassState(): Record<string, 'ignored' | 'confirmed'> {
    try {
        return JSON.parse(localStorage.getItem(FINDINGS_CLASS_STATE_KEY) || '{}');
    } catch {
        return {};
    }
}

function writeFindingsClassState(state: Record<string, 'ignored' | 'confirmed'>): void {
    try {
        localStorage.setItem(FINDINGS_CLASS_STATE_KEY, JSON.stringify(state));
    } catch {
        // 存不进就只影响本次会话,不阻断
    }
}

// [误报治理 2026-07-30 · T2] 修复出口**三态**,不再一律红色「失败」:
//   needs_human         AI 诚实拒绝编造 / 高风险类 → 按设计的正确行为,**不给重试**
//                       (给了重试,用户必然点、必然再失败,两次点完免费额度归零)
//   service_unavailable 平台自身故障(超时/5xx/连接失败) → 不是用户的问题,不计次
//   failed              模型改了但没改对 → 真失败,重试可能有用
type SpanRepairOutcome = 'needs_human' | 'service_unavailable' | 'failed';

interface SpanRepairFailure {
    spanIndex: number;
    reason: string;
    outcome: SpanRepairOutcome;
}

// 后端 alert 带 outcome 字段;老缓存/异常路径缺字段时按 'failed' 处理(保守:
// 仍显示成失败并给重试,不会把真失败悄悄伪装成"需人工")。
function normalizeRepairOutcome(raw: unknown): SpanRepairOutcome {
    return raw === 'needs_human' || raw === 'service_unavailable' ? raw : 'failed';
}

// [span 级 AI 免费修复 2026-07-30 · §2.1] 这一处允不允许 AI 修:**只认后端结论**。
// 字段缺席(老缓存/客户端兜底聚合)时按"可修"处理 —— 端点会用同一个 repair_route
// 再拒一次,所以旧前端也放行不了高风险类。
function spanAiRepairable(span: FindingSpan): boolean {
    return span.ai_repairable !== false;
}

// 本类可 AI 修的处数。后端给了 ai_repairable_count 就用它;老缓存没有该字段时
// 现算(逐 span 看 ai_repairable),两条路都不会把高风险 span 算进去。
function aiRepairableCount(card: FindingsAggregateCard): number {
    if (typeof card.ai_repairable_count === 'number') return card.ai_repairable_count;
    return card.spans.filter((span) => span.matched_text && spanAiRepairable(span)).length;
}

// [P2-D 2026-07-31] 07-30 的 isHardCard 已删:它当时的作用是"整类批量修复只给 hard 卡",
// 前提是那颗按钮长在文章行里、给 soft 卡也配一颗就成了摊派。现在整块进了 Modal
// (用户主动点进来找活干),批量出口按"有没有可免费修的 span"给,与 severity 无关 ——
// 判断改用 aiRepairableCount(card) > 0。severity 仍然照旧渲染成"必须处理/建议处理/提示"标签。

function ArticleEvidenceAdvisory(props: {
    topic: Topic;
    busy: boolean;
    onRepair: (topic: Topic) => void;
    onContinue: (topic: Topic) => void;
    onRepaired: () => void;
}): JSX.Element | null {
    const { topic, busy, onRepair, onContinue, onRepaired } = props;
    const cards = (topic.findings_aggregate && topic.findings_aggregate.length > 0
        ? topic.findings_aggregate.filter((card) => card.source !== 'evidence_legal')
        : aggregateFindingsClientSide(topic));
    const [classState, setClassState] = useState<Record<string, 'ignored' | 'confirmed'>>(readFindingsClassState);
    const [repairProgress, setRepairProgress] = useState<{ code: string; done: number; total: number } | null>(null);
    const [repairFailures, setRepairFailures] = useState<Record<string, SpanRepairFailure[]>>({});
    const [repairSummary, setRepairSummary] = useState<Record<string, { ok: number; fail: number; needHuman: number }>>({});
    // [P2-C 2026-07-31] 面板从内联折叠元素改 Modal。旧结构展开时整块面板长在文章行里,
    // 79 条位置清单一撑,这一行连同下面所有行全被顶走 —— 用户"看一眼质量参考"的代价是
    // 丢掉当前浏览位置。Modal 走 Portal 挂 body,列表行高与展开状态完全无关。
    // 🔴 本组件体内不得再出现折叠元素标签(锁 2 直接数它的出现次数,须为 0);
    //    注释里也刻意不写那个标签本身,否则 grep 会被自己的说明文字命中(假红)。
    const [panelOpen, setPanelOpen] = useState(false);
    const [panelTab, setPanelTab] = useState<'overview' | 'todo' | 'reference'>('overview');
    // 位置清单的展开也用 state 驱动,不用折叠元素 ——
    // 免得下次有人"顺手"把它改回去又长回文章行里。
    const [openSpanCode, setOpenSpanCode] = useState<string | null>(null);
    // 「一键免费修复」跨卡串行的重入闸。repairClass 里那道 `if (repairProgress) return`
    // 读的是本次 render 的闭包值,循环中途不会更新 —— 单独用 ref 才拦得住用户连点。
    const batchBusyRef = useRef(false);
    if (topic.status !== 'completed' || cards.length === 0) return null;

    const totalCount = cards.reduce((sum, card) => sum + card.count, 0);
    const acknowledged = (
        topic.quality_warning?.human_continue?.acknowledged === true
        && topic.article_human_review_status === 'approved'
    );
    const stateKey = (code: string) => `${topic.article_id || topic.id}:${code}`;
    const setClass = (code: string, value: 'ignored' | 'confirmed' | null) => {
        const next = { ...readFindingsClassState() };
        if (value) next[stateKey(code)] = value; else delete next[stateKey(code)];
        writeFindingsClassState(next);
        setClassState(next);
    };
    const visibleCards = cards.filter((card) => classState[stateKey(card.code)] !== 'ignored');
    const ignoredCount = cards.length - visibleCards.length;

    // [T3-2] 一键修复本类:该类全部可定位 span 逐个走段级修复链(不重跑整篇、不二次计费)。
    // [T3-3] 失败治理:每个失败 span 记录原因,批量结束给成功/失败计数,失败可单独重试。
    const repairSpan = async (
        card: FindingsAggregateCard, spanIndex: number,
    ): Promise<{ reason: string; outcome: SpanRepairOutcome } | null> => {
        const span = card.spans[spanIndex];
        if (!topic.article_id || !span?.matched_text) {
            return { reason: '该处无法自动定位', outcome: 'failed' };
        }
        try {
            const res = await authFetch(`/api/articles/${topic.article_id}/repair-finding`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ finding_code: card.code, matched_text: span.matched_text }),
            });
            const data = await res.json();
            if (data?.success) return null;
            return {
                reason: String(data?.message || data?.reason || '修复未完成,正文未改动'),
                outcome: normalizeRepairOutcome(data?.outcome),
            };
        } catch {
            // 网络层异常 = 平台侧不可达,同样不该算成"你的内容修不好"。
            return { reason: '修复请求失败,正文未改动', outcome: 'service_unavailable' };
        }
    };

    const repairClass = async (card: FindingsAggregateCard, onlyIndexes?: number[]) => {
        if (repairProgress) return;
        const indexes = (onlyIndexes && onlyIndexes.length > 0)
            ? onlyIndexes
            : card.spans
                // [§2.1] 批量也要跳过高风险 span(否则端点逐条拒,用户看到一排失败)。
                .map((span, index) => (span.matched_text && spanAiRepairable(span) ? index : -1))
                .filter((i) => i >= 0);
        if (indexes.length === 0) return;
        setRepairProgress({ code: card.code, done: 0, total: indexes.length });
        setRepairFailures((prev) => ({ ...prev, [card.code]: [] }));
        let ok = 0;
        const failures: SpanRepairFailure[] = [];
        for (let i = 0; i < indexes.length; i += 1) {
            const outcome = await repairSpan(card, indexes[i]);
            if (outcome === null) ok += 1;
            else failures.push({ spanIndex: indexes[i], ...outcome });
            setRepairProgress({ code: card.code, done: i + 1, total: indexes.length });
        }
        setRepairProgress(null);
        setRepairFailures((prev) => ({ ...prev, [card.code]: failures }));
        // [T2] 汇总也要分三态:把"需人工"计进"失败"会让用户以为 AI 出了问题。
        const needHuman = failures.filter((f) => f.outcome === 'needs_human').length;
        const realFail = failures.length - needHuman;
        setRepairSummary((prev) => ({ ...prev, [card.code]: { ok, fail: realFail, needHuman } }));
        if (failures.length === 0) {
            toast.success(`「${card.title}」共 ${ok} 处已全部修复`);
        } else {
            const parts = [`成功 ${ok} 处`];
            if (realFail > 0) parts.push(`失败 ${realFail} 处(可重试)`);
            if (needHuman > 0) parts.push(`需人工 ${needHuman} 处`);
            toast(`「${card.title}」修复完成:${parts.join(' · ')}`);
        }
        if (ok > 0) onRepaired();
    };

    // [P2-D 2026-07-31] 「一键免费修复」= 把每张卡的可修 span 逐个走 repair-finding
    // (免费端点),**不碰 /api/writing/rewrite**。跨卡串行,失败项各自留在自己卡里。
    const repairAllFree = async () => {
        if (batchBusyRef.current) return;
        batchBusyRef.current = true;
        try {
            for (const card of visibleCards) {
                if (aiRepairableCount(card) === 0) continue;
                await repairClass(card);
            }
        } finally {
            batchBusyRef.current = false;
        }
    };

    const severityChip = (severity: string): string => {
        if (severity === 'hard') return 'bg-red-500/15 text-red-600 dark:text-red-300';
        if (severity === 'soft') return 'bg-amber-500/15 text-amber-600 dark:text-amber-300';
        return 'bg-muted text-muted-foreground';
    };

    // 全篇可免费修的处数(高风险 span 已被 aiRepairableCount 剔除)。
    const freeRepairableTotal = visibleCards.reduce((sum, card) => sum + aiRepairableCount(card), 0);
    // 一处都定位不到的类:段级修复用不上(repair-finding 需要 matched_text),
    // 只剩"整篇重写"这一条路。这是**唯一**保留计费入口的条件 —— 与改前逐卡的触发条件
    // 完全一致,只是从每卡一颗去重成整篇一颗,付费面只减不增。
    const needsWholeRewrite = cards.some((card) => card.repairable_count === 0);

    const renderSpanList = (card: FindingsAggregateCard) => {
        const failures = repairFailures[card.code] || [];
        return (
            <ul className="mt-1 space-y-1" data-testid="advisory-span-list">
                {card.spans.map((span, index) => {
                    const failure = failures.find((item) => item.spanIndex === index);
                    return (
                        <li key={index} className="flex flex-wrap items-center gap-1.5">
                            <span className="text-muted-foreground" title={span.excerpt || span.matched_text}>
                                「{clipLocation(span.matched_text || span.excerpt) || '(无法定位到原文)'}」
                            </span>
                            {/* [§2.1 / 锁 6] span.ai_repairable===false(医疗/法律/金融高风险)
                                → 这一处不渲染 AI 修复,只提示人工出口。 */}
                            {!failure && span.matched_text && spanAiRepairable(span) && (
                                <button
                                    className="rounded px-1.5 py-0.5 text-blue-600 hover:bg-blue-50 dark:text-blue-300"
                                    data-testid="span-ai-repair-link"
                                    title="只修这一处 · 免费"
                                    disabled={!!repairProgress}
                                    onClick={() => void repairClass(card, [index])}
                                >
                                    让 AI 改这一处
                                </button>
                            )}
                            {!failure && span.matched_text && !spanAiRepairable(span) && (
                                <span className="text-muted-foreground" data-testid="span-high-risk-note">
                                    高风险内容不做 AI 修复 · 请手动改或提交人工签发
                                </span>
                            )}
                            {/* [T2] 三态分离渲染。needs_human 刻意**不给重试**:
                                AI 已声明"不编造就改不好",再点一次必然同样结果,
                                只会白白烧掉两次免费机会然后彻底没路。 */}
                            {failure && failure.outcome === 'needs_human' && (
                                <span className="text-muted-foreground" data-testid="span-repair-needs-human">
                                    这一处需要人工:{failure.reason}
                                </span>
                            )}
                            {failure && failure.outcome === 'service_unavailable' && (
                                <>
                                    <span className="text-amber-600 dark:text-amber-300" data-testid="span-repair-unavailable">
                                        服务暂时不可用:{failure.reason}
                                    </span>
                                    <button
                                        className="rounded px-1.5 py-0.5 text-blue-600 hover:bg-blue-50 dark:text-blue-300"
                                        disabled={!!repairProgress}
                                        onClick={() => void repairClass(card, [index])}
                                    >
                                        稍后重试
                                    </button>
                                </>
                            )}
                            {failure && failure.outcome === 'failed' && (
                                <>
                                    <span className="text-red-500" data-testid="span-repair-failure">
                                        失败:{failure.reason}
                                    </span>
                                    <button
                                        className="rounded px-1.5 py-0.5 text-blue-600 hover:bg-blue-50 dark:text-blue-300"
                                        disabled={!!repairProgress}
                                        onClick={() => void repairClass(card, [index])}
                                    >
                                        重试这一处
                                    </button>
                                </>
                            )}
                        </li>
                    );
                })}
            </ul>
        );
    };

    const renderActionableCard = (card: FindingsAggregateCard) => {
        const progress = repairProgress?.code === card.code ? repairProgress : null;
        const summary = repairSummary[card.code];
        const confirmed = classState[stateKey(card.code)] === 'confirmed';
        const freeCount = aiRepairableCount(card);
        const spansOpen = openSpanCode === card.code;
        return (
            <div
                key={card.code}
                className="rounded-md border border-border/70 bg-background/60 p-2"
                data-testid="findings-type-card"
            >
                <div className="flex flex-wrap items-center gap-2">
                    <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${severityChip(card.severity)}`}>
                        {card.severity === 'hard' ? '必须处理' : card.severity === 'soft' ? '建议处理' : '提示'}
                    </span>
                    <span className="font-medium">{card.title} × {card.count} 处</span>
                    {confirmed && <span className="text-emerald-600 dark:text-emerald-300">已记录</span>}
                    {summary && (
                        <span className="text-muted-foreground" data-testid="repair-summary">
                            修复:成功 {summary.ok} · 失败 {summary.fail}
                            {summary.needHuman > 0 ? ` · 需人工 ${summary.needHuman}` : ''}
                        </span>
                    )}
                </div>
                <p className="mt-1 text-muted-foreground">{card.message}</p>
                <button
                    className="mt-1 text-muted-foreground underline-offset-2 hover:underline"
                    data-testid="toggle-span-list"
                    onClick={() => setOpenSpanCode(spansOpen ? null : card.code)}
                >
                    {spansOpen ? '收起位置清单' : `展开位置清单(${card.spans.length} 处)`}
                </button>
                {spansOpen && renderSpanList(card)}
                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                    {/* [P2-D 2026-07-31] 整类批量修复恢复给全部 severity(改前只给 hard)。
                        07-30 砍它是因为它当时**长在文章行里**,一颗"该处理"的按钮配着 79 条
                        计数就是压迫感;现在整块搬进 Modal,是用户主动点进来找活干的地方,
                        给出口不再等于摊派。它走的是 repair-finding(免费),标签明示免费。 */}
                    {freeCount > 0 && (
                        <Button
                            size="sm"
                            variant="outline"
                            className="h-6 px-2 text-xs"
                            disabled={!!repairProgress}
                            data-testid="repair-class-btn"
                            title="平台承担成本 · 不扣你的算力"
                            onClick={() => void repairClass(card)}
                        >
                            {progress
                                ? `修复中 ${progress.done}/${progress.total}…`
                                : `一键免费修复本类(${freeCount} 处可修)`}
                        </Button>
                    )}
                    {freeCount === 0 && card.repairable_count > 0 && (
                        // 可定位但不许 AI 修 = 高风险类(§2.1):只给人工出口。
                        <span className="text-muted-foreground" data-testid="class-high-risk-note">
                            医疗/法律/金融高风险内容不做 AI 修复 · 请手动改或提交人工签发
                        </span>
                    )}
                    {card.repairable_count === 0 && (
                        <span className="text-muted-foreground" data-testid="class-not-locatable-note">
                            这一类定位不到具体位置,免费的段级修复用不上;想改只能整篇重写(见「概览」)。
                        </span>
                    )}
                    <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 px-2 text-xs"
                        data-testid="ignore-class-btn"
                        onClick={() => setClass(card.code, 'ignored')}
                    >
                        全部忽略本类
                    </Button>
                    <Button
                        size="sm"
                        variant="ghost"
                        className="h-6 px-2 text-xs"
                        disabled={confirmed}
                        data-testid="confirm-class-btn"
                        onClick={() => setClass(card.code, 'confirmed')}
                    >
                        {confirmed ? '已记录' : '本类记一笔已看过'}
                    </Button>
                </div>
            </div>
        );
    };

    return (
        <>
            {/* [P2-C] 列表行上只剩这一颗定高按钮 —— 点它开 Modal,行高一个像素都不动。
                🔴 语义一致性:这里说"不影响发布",就不能再有任何"待确认/待处理"的字样
                或红点(工单 §5 点名的矛盾)。条数进 title,不摆在脸上。 */}
            <Button
                size="sm"
                variant="ghost"
                className="h-6 shrink-0 px-2 text-xs text-muted-foreground"
                data-testid="article-advisory-findings"
                title={`${visibleCards.length} 类共 ${totalCount} 条 · 不影响发布`}
                onClick={(event) => { event.stopPropagation(); setPanelOpen(true); }}
            >
                质量参考(不影响发布)
            </Button>
            <Dialog open={panelOpen} onOpenChange={setPanelOpen}>
                <DialogContent
                    className="max-w-3xl"
                    data-testid="advisory-panel-dialog"
                    onClick={(event) => event.stopPropagation()}
                >
                    <DialogHeader>
                        <DialogTitle>质量参考</DialogTitle>
                        <DialogDescription>
                            这些是让文章更有说服力的建议,<strong className="font-medium text-foreground">不影响发布</strong>。
                            改不改都行,不改也能直接投放。
                        </DialogDescription>
                    </DialogHeader>
                    <Tabs value={panelTab} onValueChange={(value) => setPanelTab(value as typeof panelTab)}>
                        <TabsList>
                            <TabsTrigger value="overview">概览</TabsTrigger>
                            <TabsTrigger value="todo">待修问题</TabsTrigger>
                            <TabsTrigger value="reference">质量参考</TabsTrigger>
                        </TabsList>

                        <TabsContent value="overview" className="max-h-[60vh] space-y-3 overflow-y-auto text-xs">
                            <div className="rounded-md border border-border/70 bg-background/60 p-3">
                                <p className="font-medium text-foreground">
                                    共 {visibleCards.length} 类 {totalCount} 条建议
                                    {freeRepairableTotal > 0 ? ` · 其中 ${freeRepairableTotal} 处可以免费自动改` : ''}
                                </p>
                                <p className="mt-1 text-muted-foreground">
                                    草稿已保存。这些提示不拦发布,改完说服力更强。
                                </p>
                                <div className="mt-2 flex flex-wrap items-center gap-1.5">
                                    {freeRepairableTotal > 0 && (
                                        <Button
                                            size="sm"
                                            variant="outline"
                                            className="h-7 px-2 text-xs"
                                            disabled={!!repairProgress}
                                            data-testid="advisory-repair-all-free-btn"
                                            title="平台承担成本 · 不扣你的算力"
                                            onClick={() => void repairAllFree()}
                                        >
                                            {repairProgress
                                                ? `修复中 ${repairProgress.done}/${repairProgress.total}…`
                                                : `一键免费修复(${freeRepairableTotal} 处)`}
                                        </Button>
                                    )}
                                    <Button
                                        size="sm"
                                        variant="ghost"
                                        className="h-7 px-2 text-xs"
                                        disabled={acknowledged}
                                        data-testid="advisory-continue-btn"
                                        title="不改了,记一笔继续往下走"
                                        onClick={() => { if (!acknowledged) onContinue(topic); }}
                                    >
                                        {acknowledged ? '已记录' : '忽略并继续'}
                                    </Button>
                                </div>
                            </div>

                            {/* [P2-D] 唯一的计费入口,单独一块、明写"整篇重写""计费"。
                                🔴 它**不叫修复** —— 改前这颗按钮走 /api/writing/rewrite 却挂在
                                "修复"名下,用户以为在修一处瑕疵,实际付钱重写了整篇。 */}
                            {needsWholeRewrite && (
                                <div
                                    className="rounded-md border border-dashed border-border/70 bg-muted/30 p-3"
                                    data-testid="advisory-whole-rewrite-section"
                                >
                                    <p className="font-medium text-foreground">整篇重写(计费)</p>
                                    <p className="mt-1 text-muted-foreground">
                                        有几类提示定位不到具体位置,免费的段级修复用不上。
                                        这里是<strong className="font-medium text-foreground">让 AI 把整篇重新写一遍</strong>
                                        (标题与商业方向保留),不是修某一处 —— 按重写计费,只有你主动点才会发生。
                                    </p>
                                    <Button
                                        size="sm"
                                        variant="outline"
                                        className="mt-2 h-7 px-2 text-xs"
                                        disabled={busy}
                                        data-testid="advisory-whole-rewrite-btn"
                                        title="整篇重新生成 · 按重写计费"
                                        onClick={() => { if (!busy) onRepair(topic); }}
                                    >
                                        {busy ? '重写中…' : '整篇重写(计费)'}
                                    </Button>
                                </div>
                            )}
                        </TabsContent>

                        <TabsContent value="todo" className="max-h-[60vh] space-y-2 overflow-y-auto text-xs">
                            {visibleCards.length === 0 && (
                                <p className="text-muted-foreground">没有需要看的条目了。</p>
                            )}
                            {visibleCards.map((card) => renderActionableCard(card))}
                            {ignoredCount > 0 && (
                                <button
                                    className="text-muted-foreground underline-offset-2 hover:underline"
                                    data-testid="restore-ignored-classes"
                                    onClick={() => {
                                        const next = { ...readFindingsClassState() };
                                        for (const card of cards) {
                                            if (next[stateKey(card.code)] === 'ignored') delete next[stateKey(card.code)];
                                        }
                                        writeFindingsClassState(next);
                                        setClassState(next);
                                    }}
                                >
                                    已忽略 {ignoredCount} 类 · 恢复显示
                                </button>
                            )}
                        </TabsContent>

                        {/* 只读档:含已忽略的类。没有任何动作按钮 —— 这一栏就是"给你看看
                            系统在盯哪些维度",不制造待办。 */}
                        <TabsContent value="reference" className="max-h-[60vh] space-y-2 overflow-y-auto text-xs">
                            <p className="text-muted-foreground">
                                下面是本篇跑过的全部质量维度(含已忽略的),只读。
                                它是生成质量的可见信号,不构成发布条件。
                            </p>
                            {cards.map((card) => (
                                <div
                                    key={card.code}
                                    className="rounded-md border border-border/60 bg-background/40 p-2"
                                    data-testid="advisory-reference-card"
                                >
                                    <div className="flex flex-wrap items-center gap-2">
                                        <span className={`rounded px-1.5 py-0.5 text-[10px] font-medium ${severityChip(card.severity)}`}>
                                            {card.severity === 'hard' ? '必须处理' : card.severity === 'soft' ? '建议处理' : '提示'}
                                        </span>
                                        <span className="font-medium">{card.title} × {card.count} 处</span>
                                        {classState[stateKey(card.code)] === 'ignored' && (
                                            <span className="text-muted-foreground">已忽略</span>
                                        )}
                                    </div>
                                    <p className="mt-1 text-muted-foreground">{card.message}</p>
                                </div>
                            ))}
                        </TabsContent>
                    </Tabs>
                </DialogContent>
            </Dialog>
        </>
    );
}

// 写作进度类型
interface WritingProgress {
    total: number;
    pending: number;
    writing: number;
    completed: number;
    failed: number;
    progress: number;
    writing_list: Topic[];
    completed_list: Topic[];
    failed_list: Topic[];
}

// ===========================================================================
// [P3a 批量审核入口 2026-08-01 · 工单 §6]「审核与修复 (N)」
// ===========================================================================
// 工单原话:主入口「审核与修复(N)」· 批量动作:审核选中 / 当前分组 / 全部已完成 /
// 批量忽略普通提示并继续 / 明确的"去发布"。
//
// 🔴 三条不许破的:
//  ① "一键通过"只能通过 A1 与可跳过的人审建议 —— 前端只把 advisory_open 那一组
//     喂给批量忽略,服务端 assert_bulk_pass_allowed 再独立挡一道(前端的过滤是
//     体验,不是安全边界:真正的护栏在服务端,而且是重算不是信前端传的状态);
//  ② 失败项单独显示、可单独重试;成功项不回滚、不被后续失败覆盖 ——
//     结果存在一个**累积 Map** 里,每批只 merge 不整体重置;
//  ③ 计费边界:本面板全部 authFetch 的 URL 必须落在零扣费白名单(锁 5 枚举,
//     抓不到 URL 就自炸)。批量修复走 repair-finding / auto-repair,
//     **绝不落到 /api/writing/rewrite**。

type BatchItemStatus = 'ok' | 'skipped' | 'failed';

interface BatchItemResult {
    article_id?: number;
    topic_id?: number;
    status: BatchItemStatus;
    error_code?: string;
    message?: string;
    retryable?: boolean;
}

// [P3b 2026-08-01 · 工单 §6] SSE 事件契约。九种 kind,每条带工单点名的八个字段。
// `seq` 是重连续传用的游标(时间戳会撞,seq 单调唯一)。
interface RepairJobEvent {
    seq: number;
    kind: 'queued' | 'reviewing' | 'locating' | 'repairing' | 'validating'
        | 'partial' | 'completed' | 'failed' | 'heartbeat';
    job_id: string;
    article_id: number | null;
    finding_id: string | null;
    done: number;
    total: number;
    success_count: number;
    failed_count: number;
    message: string;
    retryable?: boolean;
}

// 九种 kind 的人话标签。🔴 一一对应,不合并、不省略 ——
// 少一种 kind 落到 undefined,界面会显示 "当前:undefined"。
const REPAIR_STAGE_LABELS: Record<RepairJobEvent['kind'], string> = {
    queued: '排队中',
    reviewing: '读取问题清单',
    locating: '定位问题位置',
    repairing: '正在修复',
    validating: '重新审核',
    partial: '部分修好',
    completed: '已完成',
    failed: '未成功',
    heartbeat: '进行中',
};

interface BatchRunSummary {
    total: number;
    done: number;
    success_count: number;
    skipped_count: number;
    failed_count: number;
    results: BatchItemResult[];
}

type ReviewGroupKey = 'legal_hard' | 'platform_profile_hard' | 'operator_hard' | 'advisory_open' | 'not_run' | 'ready';

const REVIEW_GROUP_ORDER: ReviewGroupKey[] = [
    'legal_hard', 'platform_profile_hard', 'operator_hard', 'advisory_open', 'not_run', 'ready',
];

// [Owner 2026-08-01 口径] `l3` = 这一组是不是"AI 处理不掉、必须落到人"的残留。
// 定位:主路径是 AI 评估自动处理,本面板承载 **L3 残留 + Owner 手动覆盖**,
// 不是"人工逐篇主力"。所以 l3 组排在最前、文案讲"要你拍板",非 l3 组文案讲
// "AI 本来会自动处理,你也可以在这里手动过一遍"。
const REVIEW_GROUP_META: Record<ReviewGroupKey, { label: string; hint: string; needsAttention: boolean; l3: boolean }> = {
    legal_hard: { label: 'L3 · 法律硬门', hint: '命中广告法等法律硬门:AI 不可覆盖、批量通过也不可覆盖,必须改正文再重审。', needsAttention: true, l3: true },
    platform_profile_hard: { label: 'L3 · 该渠道需重写', hint: '只挡这一个发布渠道;改投兼容渠道后重新评估即可,要你选一个渠道。', needsAttention: true, l3: true },
    operator_hard: { label: 'L3 · 需你拍板', hint: '正文或证据在审核后被改过、或已被人工拒稿 —— 这类 AI 判不了,要你确认。', needsAttention: true, l3: true },
    advisory_open: { label: '有可优化处 · AI 可自动处理', hint: '普通质量提示,不影响发布。AI 评估层接入后会自动处理;现在也可以在这里一次性忽略并继续。', needsAttention: true, l3: false },
    not_run: { label: '已生成 · 可先看稿', hint: '机审不是发布前置条件。AI 评估层接入后会自动跑;现在也可以在这里手动跑一次。', needsAttention: true, l3: false },
    ready: { label: '可进入投放准备', hint: '没有待处理项,可以直接去发布。', needsAttention: false, l3: false },
};

// 🔴 只按后端三态分组,不自己二次推断。
// 反面教材(本仓踩过):前端拿 article_review_status 自己猜"卡在哪",与服务端判定函数
// 慢慢漂开,于是界面说"待审核"、服务端说"法律硬门",用户按界面提示跑一次审核,
// 跑完还是发不出去。分档口径只有一个 SSOT = evaluate_publication_eligibility。
function reviewGroupOf(topic: Topic): ReviewGroupKey {
    const h0 = topic.publication_h0_state;
    if (h0 === 'legal_hard' || h0 === 'platform_profile_hard' || h0 === 'operator_hard') return h0;
    if (topic.publication_eligible === false) return 'operator_hard';
    if (topic.advisory_state === 'open') return 'advisory_open';
    if (topic.review_state === 'not_run') return 'not_run';
    return 'ready';
}

function groupCompletedTopics(topics: Topic[]): Record<ReviewGroupKey, Topic[]> {
    const groups = {
        legal_hard: [], platform_profile_hard: [], operator_hard: [],
        advisory_open: [], not_run: [], ready: [],
    } as Record<ReviewGroupKey, Topic[]>;
    for (const topic of topics) {
        if (topic.status !== 'completed' || !topic.article_id) continue;
        groups[reviewGroupOf(topic)].push(topic);
    }
    return groups;
}

function attentionCount(topics: Topic[]): number {
    const groups = groupCompletedTopics(topics);
    return REVIEW_GROUP_ORDER
        .filter(key => REVIEW_GROUP_META[key].needsAttention)
        .reduce((sum, key) => sum + groups[key].length, 0);
}

function BatchReviewPanel(props: {
    open: boolean;
    onOpenChange: (open: boolean) => void;
    topics: Topic[];
    selectedTopicIds: Set<number>;
    quoteId?: number | string;
    onNavigatePublish: (articleIds: number[]) => void;
    onRefresh: () => void | Promise<void>;
}): JSX.Element {
    const { open, onOpenChange, topics, selectedTopicIds, onNavigatePublish, onRefresh } = props;
    const groups = groupCompletedTopics(topics);
    const [activeGroup, setActiveGroup] = useState<ReviewGroupKey>('not_run');
    // [Owner 2026-08-01 口径] 打开面板时默认落在**第一个非空的 L3 组** ——
    // 这个面板的定位是"处理 AI 判不了的残留",不是"从头翻一遍全部文章"。
    // 没有 L3 残留时才退回到普通提示/未审那几组。
    useEffect(() => {
        if (!open) return;
        const firstL3 = REVIEW_GROUP_ORDER.find(
            key => REVIEW_GROUP_META[key].l3 && groups[key].length > 0,
        );
        const firstAny = REVIEW_GROUP_ORDER.find(
            key => REVIEW_GROUP_META[key].needsAttention && groups[key].length > 0,
        );
        setActiveGroup(firstL3 ?? firstAny ?? 'ready');
        // 只在打开的那一刻决定;之后用户自己切了页签,不许被数据刷新拽回去。
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [open]);
    const [running, setRunning] = useState<string | null>(null);
    // 🔴 累积结果:key = article_id。每批只 merge,**绝不整体重置** ——
    // 这就是"成功项不回滚、不被后续失败覆盖"的落点。第二批全失败,第一批的成功仍在。
    const [reviewResults, setReviewResults] = useState<Record<number, BatchItemResult>>({});
    const [advisoryResults, setAdvisoryResults] = useState<Record<number, BatchItemResult>>({});
    const [lastRun, setLastRun] = useState<BatchRunSummary | null>(null);

    // 本文件没有 `import React`(只按需 import hooks),所以这里不能写 React.Dispatch。
    type ResultSetter = (updater: (prev: Record<number, BatchItemResult>) => Record<number, BatchItemResult>) => void;

    const mergeResults = (
        setter: ResultSetter,
        items: BatchItemResult[],
        key: 'article_id' | 'topic_id',
    ) => {
        setter(prev => {
            const next = { ...prev };
            for (const item of items) {
                const id = item[key];
                if (typeof id === 'number') next[id] = item;
            }
            return next;
        });
    };

    const runBatchReview = async (articleIds: number[], label: string) => {
        if (articleIds.length === 0) {
            toast('这一批里没有可审核的文章');
            return;
        }
        setRunning(label);
        try {
            const resp = await authFetch(`/api/articles/batch-review`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ article_ids: articleIds }),
            });
            const data = await resp.json();
            if (!resp.ok || !data?.success) {
                toast.error(apiErrorMessage(data, '批量审核提交失败'));
                return;
            }
            mergeResults(setReviewResults, data.results || [], 'article_id');
            setLastRun(data as BatchRunSummary);
            // 真实计数,不做百分比(工单 §6)
            toast.success(`已审核 ${data.success_count}/${data.total} 篇` +
                (data.skipped_count ? ` · 跳过 ${data.skipped_count}` : '') +
                (data.failed_count ? ` · 失败 ${data.failed_count}` : ''));
            await onRefresh();
        } catch (e) {
            toast.error('批量审核请求中断,已完成的部分不受影响,可对失败项单独重试');
        } finally {
            setRunning(null);
        }
    };

    const runBatchAdvisoryContinue = async (topicIds: number[]) => {
        if (topicIds.length === 0) {
            toast('没有待忽略的普通提示');
            return;
        }
        setRunning('advisory');
        try {
            const resp = await authFetch(`/api/articles/batch-advisory-continue`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic_ids: topicIds }),
            });
            const data = await resp.json();
            if (!resp.ok || !data?.success) {
                toast.error(apiErrorMessage(data, '批量忽略提交失败'));
                return;
            }
            mergeResults(setAdvisoryResults, data.results || [], 'topic_id');
            setLastRun(data as BatchRunSummary);
            toast.success(`已忽略并继续 ${data.success_count}/${data.total} 篇` +
                (data.failed_count ? ` · ${data.failed_count} 篇未处理` : ''));
            await onRefresh();
        } catch (e) {
            toast.error('批量忽略请求中断,已完成的部分不受影响,可对失败项单独重试');
        } finally {
            setRunning(null);
        }
    };

    // ------------------------------------------------------------------ P3b
    // 「一键修复全部系统问题」:span 级修复要调模型,一篇十几处就是十几次调用,
    // 同步等必然超时 → 走 SSE。这里只做**消费端**:解析事件、显示真实 done/total、
    // 断线用同一个 job_id + last_event_id 续上。
    // 🔴 进度条只用事件里的 done/total 画,前端**不自己插值、不自己估**;
    //    服务端没给的数字这里也不编(工单 §6:禁伪造百分比)。
    const [repairJobId, setRepairJobId] = useState<string | null>(null);
    const [repairProgress, setRepairProgress] = useState<RepairJobEvent | null>(null);
    const repairSeqRef = useRef(0);

    const runRepairAllStream = async (articleIds: number[], resumeJobId?: string) => {
        if (articleIds.length === 0) {
            toast('这一批里没有可修复的文章');
            return;
        }
        setRunning('repair-all');
        try {
            const resp = await authFetch(`/api/articles/batch-repair-stream`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    article_ids: articleIds,
                    job_id: resumeJobId ?? repairJobId ?? null,
                    last_event_id: resumeJobId ? repairSeqRef.current : 0,
                }),
            });
            if (!resp.ok || !resp.body) {
                const data = await resp.json().catch(() => null);
                toast.error(apiErrorMessage(data, '修复任务启动失败'));
                return;
            }
            const jobId = resp.headers.get('X-Job-Id');
            if (jobId) setRepairJobId(jobId);
            const reader = resp.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            for (;;) {
                const { done, value } = await reader.read();
                if (done) break;
                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split('\n');
                buffer = lines.pop() || '';
                for (const line of lines) {
                    if (!line.startsWith('data: ')) continue;
                    try {
                        const event = JSON.parse(line.slice(6)) as RepairJobEvent;
                        if (typeof event.seq === 'number') repairSeqRef.current = event.seq;
                        setRepairProgress(event);
                        if (event.kind === 'failed' && event.article_id) {
                            // 失败项进同一个累积 Map(不整体重置),沿用"单独重试"那条出口
                            mergeResults(setReviewResults, [{
                                article_id: event.article_id, status: 'failed',
                                message: event.message, retryable: event.retryable !== false,
                            }], 'article_id');
                        }
                    } catch {
                        // 单条解析失败不中断整条流:剩下的事件仍然有用
                    }
                }
            }
            await onRefresh();
        } catch (e) {
            toast.error('修复流中断;已修好的都已保存,点「继续上次修复」可以接着跑');
        } finally {
            setRunning(null);
        }
    };

    const completedTopics = topics.filter(t => t.status === 'completed' && t.article_id);
    const selectedCompleted = completedTopics.filter(t => selectedTopicIds.has(t.id));
    const groupTopics = groups[activeGroup];
    const eligibleTopics = completedTopics.filter(t => t.publication_eligible !== false);
    const failedItems = [
        ...Object.values(reviewResults),
        ...Object.values(advisoryResults),
    ].filter(item => item.status === 'failed');
    const busy = running !== null;

    return (
        <Dialog open={open} onOpenChange={onOpenChange}>
            <DialogContent className="max-w-3xl max-h-[80vh] overflow-y-auto" data-testid="batch-review-dialog">
                <DialogHeader>
                    <DialogTitle>审核与修复</DialogTitle>
                    <DialogDescription data-testid="batch-panel-positioning">
                        内容审核的主路径是 AI 自动评估。这里只处理 <strong>AI 判不了的(L3)和法律真硬门</strong>,
                        以及你想手动覆盖的部分。
                        <span className="block mt-1 text-muted-foreground">
                            AI 评估层尚未接入前,全部条目都会暂时出现在这里 —— 不是"每篇都要你审"。
                        </span>
                    </DialogDescription>
                </DialogHeader>

                <div className="flex flex-wrap gap-2" data-testid="batch-actions">
                    <Button
                        size="sm"
                        variant="outline"
                        disabled={busy || selectedCompleted.length === 0}
                        data-testid="batch-review-selected"
                        onClick={() => void runBatchReview(
                            selectedCompleted.map(t => t.article_id as number), 'selected',
                        )}
                    >
                        {running === 'selected' ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <ShieldCheck className="h-4 w-4 mr-1" />}
                        审核选中 ({selectedCompleted.length})
                    </Button>
                    <Button
                        size="sm"
                        variant="outline"
                        disabled={busy || groupTopics.length === 0}
                        data-testid="batch-review-group"
                        onClick={() => void runBatchReview(
                            groupTopics.map(t => t.article_id as number), 'group',
                        )}
                    >
                        {running === 'group' ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <ShieldCheck className="h-4 w-4 mr-1" />}
                        审核当前分组 ({groupTopics.length})
                    </Button>
                    <Button
                        size="sm"
                        variant="outline"
                        disabled={busy || completedTopics.length === 0}
                        data-testid="batch-review-all"
                        onClick={() => void runBatchReview(
                            completedTopics.map(t => t.article_id as number), 'all',
                        )}
                    >
                        {running === 'all' ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <ShieldCheck className="h-4 w-4 mr-1" />}
                        审核全部已完成 ({completedTopics.length})
                    </Button>
                    <Button
                        size="sm"
                        variant="outline"
                        disabled={busy || groups.advisory_open.length === 0}
                        data-testid="batch-ignore-advisory"
                        onClick={() => void runBatchAdvisoryContinue(groups.advisory_open.map(t => t.id))}
                    >
                        {running === 'advisory' ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Check className="h-4 w-4 mr-1" />}
                        忽略普通提示并继续 ({groups.advisory_open.length})
                    </Button>
                    <Button
                        size="sm"
                        variant="outline"
                        disabled={busy || completedTopics.length === 0}
                        data-testid="batch-repair-all"
                        onClick={() => void runRepairAllStream(
                            completedTopics.map(t => t.article_id as number),
                        )}
                    >
                        {running === 'repair-all' ? <Loader2 className="h-4 w-4 mr-1 animate-spin" /> : <Wrench className="h-4 w-4 mr-1" />}
                        一键修复全部系统问题
                    </Button>
                    {repairJobId && running !== 'repair-all' && (
                        <Button
                            size="sm"
                            variant="outline"
                            disabled={busy}
                            data-testid="batch-repair-resume"
                            onClick={() => void runRepairAllStream(
                                completedTopics.map(t => t.article_id as number), repairJobId,
                            )}
                        >
                            <RefreshCw className="h-4 w-4 mr-1" />
                            继续上次修复
                        </Button>
                    )}
                    <Button
                        size="sm"
                        disabled={busy || eligibleTopics.length === 0}
                        data-testid="batch-go-publish"
                        onClick={() => onNavigatePublish(eligibleTopics.map(t => t.article_id as number))}
                    >
                        <Send className="h-4 w-4 mr-1" />
                        去发布 ({eligibleTopics.length})
                    </Button>
                </div>

                {/* 🔴 免费/收费边界明示:本面板的动作全部零扣费。 */}
                <p className="text-xs text-muted-foreground" data-testid="batch-zero-charge-note">
                    以上批量动作都不计费。整篇创意重写才计费,入口在单篇的「质量参考」里。
                </p>

                {repairProgress && (
                    <div className="rounded-md border border-border/70 p-2 text-xs" data-testid="repair-stream-progress">
                        {/* 🔴 只画服务端给的真实 done/total,不插值、不换算百分比。
                            当前阶段用事件 kind 如实显示,不是"看着在动"的假动画。 */}
                        修复进度:{repairProgress.done}/{repairProgress.total} 篇
                        (成功 {repairProgress.success_count} · 未成功 {repairProgress.failed_count})
                        <span className="ml-2 text-muted-foreground">
                            当前:{REPAIR_STAGE_LABELS[repairProgress.kind]} · {repairProgress.message}
                        </span>
                    </div>
                )}

                {lastRun && (
                    <div className="rounded-md border border-border/70 p-2 text-xs" data-testid="batch-run-summary">
                        本次:成功 {lastRun.success_count} / 跳过 {lastRun.skipped_count} / 失败 {lastRun.failed_count},共 {lastRun.total} 篇
                    </div>
                )}

                {failedItems.length > 0 && (
                    <div className="rounded-md border border-red-200 p-2 dark:border-red-500/40" data-testid="batch-failed-list">
                        <p className="text-xs font-medium text-red-600 dark:text-red-300">
                            没处理成功的 {failedItems.length} 篇(其余已处理的不受影响)
                        </p>
                        <ul className="mt-1 space-y-1">
                            {failedItems.map(item => {
                                const articleId = item.article_id;
                                const topicId = item.topic_id;
                                const key = `${articleId ?? ''}-${topicId ?? ''}`;
                                return (
                                    <li key={key} className="flex items-center justify-between gap-2 text-xs" data-testid="batch-failed-item">
                                        <span className="text-muted-foreground">
                                            {topicId ? `选题 #${topicId}` : `文章 #${articleId}`} · {item.message || item.error_code}
                                        </span>
                                        {item.retryable !== false && (
                                            <Button
                                                size="sm"
                                                variant="outline"
                                                className="h-6 text-xs"
                                                disabled={busy}
                                                data-testid="batch-retry-one"
                                                onClick={() => {
                                                    if (typeof topicId === 'number') {
                                                        void runBatchAdvisoryContinue([topicId]);
                                                    } else if (typeof articleId === 'number') {
                                                        void runBatchReview([articleId], 'retry');
                                                    }
                                                }}
                                            >
                                                重试这一篇
                                            </Button>
                                        )}
                                    </li>
                                );
                            })}
                        </ul>
                    </div>
                )}

                <Tabs value={activeGroup} onValueChange={value => setActiveGroup(value as ReviewGroupKey)}>
                    <TabsList className="flex flex-wrap">
                        {REVIEW_GROUP_ORDER.map(key => (
                            <TabsTrigger key={key} value={key} data-testid={`batch-group-tab-${key}`}>
                                {REVIEW_GROUP_META[key].label} ({groups[key].length})
                            </TabsTrigger>
                        ))}
                    </TabsList>
                    {REVIEW_GROUP_ORDER.map(key => (
                        <TabsContent key={key} value={key} className="mt-2">
                            <p className="text-xs text-muted-foreground">{REVIEW_GROUP_META[key].hint}</p>
                            <ul className="mt-2 space-y-1">
                                {groups[key].map(topic => {
                                    const result = topic.article_id ? reviewResults[topic.article_id] : undefined;
                                    const aiSummary = topic.ai_review?.summary;
                                    return (
                                        <li key={topic.id} className="rounded-md border border-border/60 p-2 text-xs" data-testid="batch-group-row">
                                            <div className="flex items-center justify-between gap-2">
                                                <span className="truncate">{topic.optimized_title || topic.original_keyword}</span>
                                                {result && (
                                                    <Badge className="text-[10px] px-1 py-0" data-testid="batch-row-result">
                                                        {result.status === 'ok' ? '已处理' : result.status === 'skipped' ? '已跳过' : '未成功'}
                                                    </Badge>
                                                )}
                                            </div>
                                            {/* [Owner 2026-08-01] AI 预核对摘要 —— 接口位由 AI 评估包填。
                                                🔴 缺席时如实说"尚未给出结论",绝不把"没有结论"渲染成"AI 说没问题":
                                                   那会让人在 L3 卡片上一键放过法律硬门。 */}
                                            <p className="mt-1 text-muted-foreground" data-testid="batch-row-ai-verdict">
                                                {aiSummary
                                                    ? `AI 预核对:${aiSummary}`
                                                    : 'AI 预核对:尚未给出结论(AI 评估层未接入)'}
                                            </p>
                                            {/* 一键确认:走的还是那条带 H0 护栏的免费端点。
                                                🔴 只在**真有待确认提示**时才渲染。L3 三档点下去服务端必拒
                                                (护栏在服务端,不在这里),给一颗必然失败的按钮 = 骗用户点第二次;
                                                这也是本仓判过的"别让按钮说谎"。其余情况如实说下一步是什么。 */}
                                            <div className="mt-1 flex justify-end">
                                                {topic.advisory_state === 'open' ? (
                                                    <Button
                                                        size="sm"
                                                        variant="outline"
                                                        className="h-6 text-xs"
                                                        disabled={busy}
                                                        data-testid="batch-row-confirm"
                                                        onClick={() => void runBatchAdvisoryContinue([topic.id])}
                                                    >
                                                        确认这一篇并继续
                                                    </Button>
                                                ) : (
                                                    <span className="text-muted-foreground" data-testid="batch-row-next-step">
                                                        {REVIEW_GROUP_META[key].l3 ? '需要你处理后重新审核' : '无需确认'}
                                                    </span>
                                                )}
                                            </div>
                                        </li>
                                    );
                                })}
                                {groups[key].length === 0 && (
                                    <li className="text-xs text-muted-foreground">这一组是空的。</li>
                                )}
                            </ul>
                        </TabsContent>
                    ))}
                </Tabs>
            </DialogContent>
        </Dialog>
    );
}

// [P3a] 批量面板到此为止 —— 锁 5 枚举 authFetch URL 用的就是这两个标记之间的范围。

function apiErrorMessage(payload: any, fallback: string): string {
    const detail = payload?.detail;
    if (typeof detail === 'string') return detail;
    if (detail && typeof detail.message === 'string') return detail.message;
    if (typeof payload?.message === 'string') return payload.message;
    return fallback;
}

/*
 * 🔴 [#222 a1b' · WO_232] 关键词身份复核拒掉标题时,后端(9c7332b66)改成回
 *    `TITLE_ALIGNMENT_REJECTED` + 一句说人话的 `user_message`,不再无条件写死
 *    `TITLE_AI_UNAVAILABLE`。真因是**我们自己的复核**拒了标题(写作线 llm_call 全 success),
 *    而用户看到的是「AI 没能生成」—— 展示与真因不符,用户只能瞎点重试。
 *
 * 🔴 这条路径上的失败码**恰好两个**(`writing/title_ai_only.py:57-58` 枚举,不是按一个码写 else):
 *    `TITLE_AI_UNAVAILABLE`(AI 真的没出货)· `TITLE_ALIGNMENT_REJECTED`(出了货但被复核拒)。
 *    只有后者是「用户自己写个标题就能往下走」,所以只有后者给「编辑标题」入口。
 */
const TITLE_ALIGNMENT_REJECTED = 'TITLE_ALIGNMENT_REJECTED';

function isTitleAlignmentRejected(topic: Topic): boolean {
    return isTitlePhaseFailure(topic) && topic.generation_error_code === TITLE_ALIGNMENT_REJECTED;
}

function articleFailureText(topic: Topic): string {
    // [T5 P0 2026-07-28] 标题阶段失败没有扣费,也就没有退款记录 ——
    // 再拼一句"旧任务暂无退款记录"会把用户往资金问题上引,而这次根本没扣钱。
    if (isTitleAlignmentRejected(topic)) {
        /* 这一码的 user_message 本身就是说给用户听的,前面再挂一串全大写的码只会盖住真话。
           其余码保留码前缀 —— 它们对用户没有可操作含义,码是给客服/排查用的。 */
        return topic.generation_error_message || topic.fail_reason
            || '这批标题没通过关键词核对，你可以自己写一个标题继续。';
    }
    if (isTitlePhaseFailure(topic)) {
        return [
            topic.generation_error_code || 'TITLE_GENERATION_FAILED',
            topic.generation_error_message || topic.fail_reason || '标题没有生成成功，可重新生成。',
        ].join(' · ');
    }
    return [
        topic.generation_error_code || 'ARTICLE_LEGACY_FAILURE',
        topic.generation_error_message || topic.fail_reason || '文章生成未完成，可刷新后安全重试。',
        topic.generation_refund_message || '旧任务暂无退款记录',
    ].join(' · ');
}

/**
 * [T5 P0 2026-07-28] 标题阶段失败(topics 行已受理但标题没生成出来)。
 *
 * 与正文阶段失败必须分开:正文失败的重试入口是"安全重试"(重跑写作,
 * 且要等退款到位才放行);标题失败没扣过费,重试入口是"重新生成标题"。
 * 判据用既有列 generation_failure_phase —— 后端 title_generation /
 * title_output / title_persist 三个阶段码统一以 title_ 打头。
 */
function isTitlePhaseFailure(topic: Topic): boolean {
    return String(topic.generation_failure_phase || '').startsWith('title_');
}

type KnowledgeStatusValue = 'none' | 'draft' | 'pending' | 'feedback' | 'confirmed' | 'expired' | 'revoked';

interface MaterialsSummary {
    company_name?: string;
    industry?: string;
    intro_excerpt?: string;
    usp_excerpt?: string;
    fields_filled?: string[];
    fields_missing?: string[];
    filled_count?: number;
    total_fields?: number;
    selling_points_count?: number;
    cases_count?: number;
    testimonials_count?: number;
}

interface WritingKnowledgeStatus {
    quote_id: number;
    brand_id: number | null;
    diagnosis_id?: number | null;
    brand_name?: string;
    status: KnowledgeStatusValue;
    has_materials: boolean;
    knowledge_count: number;
    has_knowledge: boolean;
    can_start_writing: boolean;
    can_generate_link: boolean;
    token_url?: string | null;
    customer_notes?: string;
    confirmed_at?: string | null;
    expires_at?: string | null;
    materials_summary?: MaterialsSummary | null;
    message?: string;
}

interface StructureGuidanceEvidence {
    adopted_group_count?: number;
    explicit_cited_count?: number;
    search_only_control_count?: number;
    engine_count?: number;
    sample_domain_count?: number;
    qualified_structure_lift_count?: number;
}

interface StructureGuidance {
    success?: boolean;
    available: boolean;
    can_apply: boolean;
    default_enabled?: boolean;
    sample_status: 'ready' | 'observing' | 'not_enabled' | string;
    status_label: string;
    status_detail: string;
    guidance?: string;
    rules?: string[];
    guardrails?: string[];
    evidence?: StructureGuidanceEvidence;
    article_type?: string;
    article_type_label?: string;
    recommended_template_name?: string;
    default_reason?: string;
    evidence_note?: string;
    confidence?: number;
    production_takeover?: boolean;
}

interface KnowledgeFile {
    filename: string;
    relative_path?: string;
    size?: number;
    modified?: string;
    vectorized?: boolean;
    vector_chunks?: number;
}

interface ContactForm {
    contact_phone: string;
    contact_wechat: string;
    contact_website: string;
    contact_address: string;
}

interface KnowledgeBasicsForm {
    business_summary: string;
    target_customers: string;
    products_services: string;
    key_selling_points: string;
    proof_cases: string;
    forbidden_notes: string;
}

interface BrandImageAsset {
    id: number;
    file_name?: string;
    title?: string;
    public_url?: string;
    thumbnail_key?: string;
    image_type?: string;
    publish_allowed?: number;
    rights_confirmed?: number;
}

// 状态标签配置
const STATUS_CONFIG: Record<string, { label: string; color: string }> = {
    pending: { label: "待生成标题", color: "bg-amber-500/10 text-amber-500 border border-amber-500/20" },
    titles_generating: { label: "标题生成中", color: "bg-blue-500/10 text-blue-500 border border-blue-500/20" },
    titles_ready: { label: "待选择标题", color: "bg-emerald-500/10 text-emerald-500 border border-emerald-500/20" },
    writing: { label: "写作中", color: "bg-purple-500/10 text-purple-500 border border-purple-500/20" },
    completed: { label: "已完成", color: "bg-green-500/10 text-green-500 border border-green-500/20" },
    optimizing: { label: "优化中", color: "bg-orange-500/10 text-orange-500 border border-orange-500/20" },
    // [写作卡死根治 2026-06-08 · 方案 B] 卡死/扣费后线程未启动的选题 · 不可重选写作(防双扣)· 待退款资金批处理
    write_timeout: { label: "写作超时·待处理", color: "bg-red-500/10 text-red-600 border border-red-500/20" },
};

const KNOWLEDGE_STATUS_CONFIG: Record<KnowledgeStatusValue, { label: string; color: string }> = {
    none: { label: "未补资料", color: "bg-red-500/10 text-red-600 border-red-500/25" },
    draft: { label: "已整理资料", color: "bg-emerald-500/10 text-emerald-600 border-emerald-500/25" },
    pending: { label: "待客户确认", color: "bg-blue-500/10 text-blue-600 border-blue-500/25" },
    feedback: { label: "客户有反馈", color: "bg-amber-500/10 text-amber-600 border-amber-500/25" },
    confirmed: { label: "客户已确认", color: "bg-green-500/10 text-green-600 border-green-500/25" },
    expired: { label: "链接已过期", color: "bg-orange-500/10 text-orange-600 border-orange-500/25" },
    revoked: { label: "链接已撤销", color: "bg-slate-500/10 text-slate-500 border-slate-500/25" },
};

const KNOWLEDGE_FIELD_LABELS: Record<string, string> = {
    company_intro: "公司介绍",
    core_value: "价值主张",
    usp: "差异化卖点",
    selling_points: "核心卖点",
    products: "产品服务",
    customers: "目标客户",
    cases: "案例",
    testimonials: "客户证言",
};

function knowledgeFieldLabel(field: string): string {
    return KNOWLEDGE_FIELD_LABELS[field] || field;
}

function compactKnowledgeText(text: string): string {
    return text.replace(/\r\n/g, "\n").replace(/[ \t]+/g, " ").trim();
}

function stringifyKnowledgeValue(value: any): string {
    if (value === null || value === undefined) return "";
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
        return compactKnowledgeText(String(value));
    }
    if (Array.isArray(value)) {
        return value
            .map(item => stringifyKnowledgeValue(item))
            .filter(Boolean)
            .join("\n");
    }
    if (typeof value === "object") {
        const record = value as Record<string, any>;
        const orderedKeys = [
            "title",
            "name",
            "client",
            "customer",
            "industry",
            "background",
            "problem",
            "need",
            "solution",
            "result",
            "effect",
            "quote",
            "content",
            "description",
            "proof",
            "source",
        ];
        const values = orderedKeys
            .map(key => stringifyKnowledgeValue(record[key]))
            .filter(Boolean);
        if (values.length > 0) return values.join("；");
        return Object.values(record)
            .map(item => stringifyKnowledgeValue(item))
            .filter(Boolean)
            .join("；");
    }
    return "";
}

function firstKnowledgeText(...values: any[]): string {
    for (const value of values) {
        const text = stringifyKnowledgeValue(value);
        if (text) return text.slice(0, 3000);
    }
    return "";
}

function knowledgeRecord(value: any): Record<string, any> {
    if (!value || typeof value !== "object" || Array.isArray(value)) return {};
    return value;
}

function mergeKnowledgeBasicsFromCleaned(current: KnowledgeBasicsForm, cleaned: any): KnowledgeBasicsForm {
    const structured = knowledgeRecord(cleaned?.structured_knowledge);
    const nextBasics: KnowledgeBasicsForm = { ...current };

    const fillIfEmpty = (field: keyof KnowledgeBasicsForm, ...values: any[]) => {
        if (nextBasics[field].trim()) return;
        const text = firstKnowledgeText(...values);
        if (text) nextBasics[field] = text;
    };

    fillIfEmpty(
        "business_summary",
        cleaned?.unique_value,
        structured?.positioning,
        structured?.summary,
        cleaned?.company_intro,
    );
    fillIfEmpty(
        "target_customers",
        structured?.customers,
        structured?.target_customers,
        structured?.target_users,
        cleaned?.service_area,
    );
    fillIfEmpty(
        "products_services",
        structured?.products,
        structured?.services,
        structured?.service_items,
        cleaned?.methodology,
    );
    fillIfEmpty(
        "key_selling_points",
        cleaned?.core_selling_points,
        structured?.differentiation,
        structured?.selling_points,
        cleaned?.unique_value,
    );
    fillIfEmpty(
        "proof_cases",
        cleaned?.case_studies,
        structured?.cases,
        structured?.success_cases,
        cleaned?.testimonials,
        cleaned?.credentials,
    );
    fillIfEmpty(
        "forbidden_notes",
        cleaned?.forbidden_notes,
        cleaned?.forbidden_expressions,
        structured?.forbidden_notes,
        structured?.risk_notes,
    );

    return nextBasics;
}

/*
 * [#220 a1 ⓐ · 跨窗契约 2026-09-15] 基础资料表**读自哪一列**,由这张表定死:
 *
 *   business_summary   → client_profiles.business_summary
 *   target_customers   → target_users
 *   key_selling_points → selling_points
 *   forbidden_notes    → brand_constraints
 *   products_services  ┐ 220-c2 的新 JSONB `basic_info_fields`,
 *   proof_cases        ┘ 经 `profile.writing_basics` 下发;c2 交付前读不到 ⇒ **留空**
 *
 * 🔴 被**明令摘掉**的三条老回落,每条都有具体后果:
 *   · `negative_feedback` → 它是飞轮的「失败教训」,由 add_negative_feedback 往里
 *     **追加字典**(db/profile_db.py:799),读进「禁用表达」会把一串 JSON 塞进文本框;
 *   · `products` → 存的时候走 `json.dumps`(profile_db.py:380),是数组;
 *   · `success_cases` → 走 `_json_text_or_none`:**调用方传字符串就存字符串、传数组就存 JSON**。
 *     所以它的形态取决于谁写的 —— 在恰好存了字符串的客户身上看着正常,
 *     在另一些客户身上直接显示 JSON。「有时候是数组」比「是数组」更难发现。
 *   宁可留空等 c2,也不把这三样拼成文本显示给人看。
 */
function writingBasicsFromContract(profile: any): Partial<KnowledgeBasicsForm> {
    /* 🔴 键名是 `writing_basics` 不是 `basic_info`:本仓 api/profile_api.py 的
       人设访谈提示词模板里已经有一个 `basic_info`({identity, core_skills, …}),
       同名不同物。跨窗 09-15 裁定改名,就是为了不让这两个将来被谁合并。 */
    const wb = profile?.writing_basics && typeof profile.writing_basics === 'object'
        ? profile.writing_basics : {};
    const pick = (...vals: any[]) => firstKnowledgeText(...vals);
    return {
        business_summary: pick(wb.business_summary, profile?.business_summary),
        target_customers: pick(wb.target_customers, profile?.target_users),
        key_selling_points: pick(wb.key_selling_points, profile?.selling_points),
        forbidden_notes: pick(wb.forbidden_notes, profile?.brand_constraints),
        /* 🔴 这两个**只**认契约键:c2 之前读不到就留空,不回落(见上) */
        products_services: pick(wb.products_services),
        proof_cases: pick(wb.proof_cases),
    };
}

function mergeKnowledgeBasicsFromProfile(
    current: KnowledgeBasicsForm,
    profileBundle: any,
    summary?: MaterialsSummary | null,
): KnowledgeBasicsForm {
    const brand = profileBundle?.brand || {};
    const profile = profileBundle?.profile || profileBundle || {};
    /* 契约那六个先填(仍是 fillIfEmpty:用户打过字的不覆盖),剩下的空位才轮到下面的老来源 */
    const contract = writingBasicsFromContract(profile);
    const afterContract: KnowledgeBasicsForm = { ...current };
    (Object.keys(contract) as (keyof KnowledgeBasicsForm)[]).forEach((k) => {
        const v = contract[k];
        if (!afterContract[k].trim() && v) afterContract[k] = v;
    });
    current = afterContract;
    const structured = knowledgeRecord(
        profile?.structured_knowledge ||
        brand?.structured_knowledge ||
        profile?.industry_brief,
    );

    return mergeKnowledgeBasicsFromCleaned(current, {
        company_intro: firstKnowledgeText(
            profile?.company_intro,
            profile?.business,
            brand?.business,
            summary?.intro_excerpt,
        ),
        unique_value: firstKnowledgeText(
            profile?.core_value,
            profile?.unique_value,
            profile?.value_proposition,
            profile?.business_summary,
            summary?.usp_excerpt,
        ),
        core_selling_points: firstKnowledgeText(
            profile?.core_selling_points,
            profile?.selling_points,
            structured?.selling_points,
            structured?.differentiation,
        ),
        case_studies: firstKnowledgeText(
            /* 🔴 profile?.success_cases 已摘:形态取决于写入方(_json_text_or_none) */
            profile?.case_studies,
            profile?.cases,
            structured?.success_cases,
            structured?.cases,
        ),
        testimonials: firstKnowledgeText(
            profile?.testimonials,
            profile?.customer_reviews,
            structured?.testimonials,
            structured?.customer_reviews,
        ),
        forbidden_notes: firstKnowledgeText(
            profile?.forbidden_notes,
            profile?.forbidden_expressions,
            /* 🔴 profile?.negative_feedback 已摘:它是飞轮的失败教训(字典数组) */
            structured?.forbidden_notes,
            structured?.risk_notes,
        ),
        structured_knowledge: {
            ...structured,
            summary: firstKnowledgeText(structured?.summary, profile?.company_intro, brand?.business, summary?.intro_excerpt),
            positioning: firstKnowledgeText(structured?.positioning, profile?.core_value, summary?.usp_excerpt),
            customers: firstKnowledgeText(structured?.customers, structured?.target_customers, profile?.target_customers, profile?.target_users),
            /* 🔴 profile?.products 已摘:存的时候 json.dumps,是数组 */
            products: firstKnowledgeText(structured?.products, structured?.services, profile?.services),
            selling_points: firstKnowledgeText(structured?.selling_points, profile?.selling_points),
            success_cases: firstKnowledgeText(structured?.success_cases, profile?.case_studies),
        },
    });
}

function hasAutofillReadyMaterial(status?: WritingKnowledgeStatus | null): boolean {
    const summary = status?.materials_summary;
    return Boolean(
        status?.status === "draft" ||
        status?.status === "pending" ||
        status?.status === "confirmed" ||
        (summary?.filled_count ?? 0) > 0 ||
        summary?.intro_excerpt ||
        summary?.usp_excerpt ||
        (summary?.selling_points_count ?? 0) > 0 ||
        (summary?.cases_count ?? 0) > 0 ||
        (summary?.testimonials_count ?? 0) > 0,
    );
}

const KNOWLEDGE_FILE_ACCEPT = ".pdf,.docx,.txt,.md,.pptx,.csv,.json";

const formatKnowledgeFileSize = (size?: number) => {
    if (!size) return "";
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(0)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
};

export function WritingHall() {
    const navigate = useEmbeddedNavigate();
    const { user } = useAuth();
    const { overview, isMember, loading: organizationLoading, error: organizationError } = useOrganization();
    const canArchiveProject = !organizationLoading && !organizationError
        && (!isMember || Boolean(overview?.identity.capabilities.includes('team.output_handoff')));
    const isAdmin = user?.is_admin === true;
    // v1_3 (CTO-15.1 2026-04-19): 动态价目表 hooks
    // [P0-16 2026-07-12] 价目未加载时保持 null(不再 ?? 0)· `?? 0` 会让 totalCost=0 使余额预检
    //   (totalPoints < 0) 恒过 → 静默"免费"假象,但后端仍按权威价真实扣费。null 时禁开始写作 + 提示。
    const articleGenCost = useFeatureCost('article_gen');    // number | null · 写文章单价
    const articleRewriteCost = useFeatureCost('article_rewrite');  // number | null · 重写单价
    const pricingReady = articleGenCost != null;  // 价目就绪才允许批量写作动作
    // [写作卡死根治 2026-06-08] CHARGE_CONFIRM_THRESHOLD=2000:写 6 篇(6×390=2340)即过线轻确认(老板拍)
    const confirmLarge = useConfirmLargeDeduction(2000);
    const [confirmDialog, askConfirm] = useConfirmDialog();  // >=2000 算力走 toast 5s 反悔
    /*
     * 余额预检:批量总额 > 余额时拦截 + 红态显示"只够 X 篇"。
     *
     * 🔴 [WO_218-a1 2026-09-18] `walletStatus` 以前**只守了动作(4323),没守显示**。
     *    而这一行的注释自称它管「红态显示"只够 X 篇"」—— 注释说它守了,它没守。
     *    后果:余额还没拿到时 `totalPoints` 是空钱包的 0、`pricingReady` 为真,
     *    徽标就显示「· 只够 0 篇」——**「我们还不知道你的余额」被显示成「你只够写 0 篇」**。
     *    与本轮 `charge` 的 `None`/`0` 是同一个病,只不过这次是对服务商说的一个钱数。
     *    ⇒ 两处 `insufficient` 各加 `walletStatus === 'ready'`,与 4323 同源。
     */
    const { totalPoints, status: walletStatus } = useWallet();
    const markStep = useMarkStepCompleted();
    const [searchParams, setSearchParams] = useSearchParams();
    const preQuoteId = searchParams.get('quote_id') ? Number(searchParams.get('quote_id')) : null;
    const preTab = searchParams.get('tab') || '';
    // [P4 缺口作战计划 2026-08-08 · B6] 交付计划深链带来的计划项。
    // 🔴 必须在下面那个 `setSearchParams({})` **之前**读掉并冻结 ——
    //    清参数是同一个 effect 的最后一步,读晚一拍就永远读不到了。
    const prePlanItemId = searchParams.get('plan_item_id') || '';
    const prePlanGeneration = searchParams.get('plan_generation')
        ? Number(searchParams.get('plan_generation')) : null;
    const [gapPlanItem, setGapPlanItem] = useState<GapPlanPrefill | null>(null);
    const [gapPlanNotice, setGapPlanNotice] = useState<string>('');
    // 🔴 `globalBrandName` 已删除:它唯一的用处是拼 `brand_name=` 那个
    //    **后端不认的** query 参数(见 loadProjects 里的说明)。
    //    留着一个"看起来在做客户过滤"的变量,比没有更容易误导。
    const { currentBrandId } = useClientContext();
    const isCEnd = useIsCEndContext();  // v3.2: C 端隐藏下载按钮
    const [activeTab, setActiveTab] = useState(preTab || "all");
    const [projects, setProjects] = useState<WritingProject[]>([]);
    const [loading, setLoading] = useState(true);

    // Phase 06 (CTO-15.23 2026-05-03 T4) · 快速写作 Dialog
    const [showQuickWrite, setShowQuickWrite] = useState(false);

    // [CTO-15.23 2026-05-05] 添加关键词 Dialog · 老板诉求"漏词时不用重新走流程"
    const [showAddKeyword, setShowAddKeyword] = useState(false);
    const [addKwForm, setAddKwForm] = useState({
        keyword: '',
        category: '自定义',
        tier: 'standard' as 'entry' | 'standard' | 'flagship',
        final_price: 0,
        required_articles: 1,
    });
    const [addKwSubmitting, setAddKwSubmitting] = useState(false);

    // [CTO-15.23 2026-05-05] 添加关键词提交 · 复用 loadProjectDetail 刷新列表
    const submitAddKeyword = async () => {
        if (!selectedProject) return;
        const kw = addKwForm.keyword.trim();
        if (!kw) { toast.error('请输入关键词'); return; }
        if (kw.length > 100) { toast.error('关键词长度不能超过 100 字'); return; }
        setAddKwSubmitting(true);
        try {
            const res = await authFetch(`/api/writing/projects/${selectedProject.id}/add-keyword`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(addKwForm),
            });
            const data = await res.json();
            if (res.ok && data.success) {
                toast.success(data.message || '关键词已添加');
                // 刷新关键词列表 + 重置表单
                await loadProjectDetail(selectedProject.id);
                setAddKwForm({ keyword: '', category: '自定义', tier: 'standard', final_price: 0, required_articles: 1 });
                setShowAddKeyword(false);
            } else {
                toast.error(data.detail || data.message || '添加失败');
            }
        } catch (e) {
            console.error('添加关键词失败:', e);
            toast.error('添加失败 · 网络错误');
        } finally {
            setAddKwSubmitting(false);
        }
    };

    // 标题工作台状态
    const [selectedProject, setSelectedProject] = useState<WritingProject | null>(null);
    const [deliverySummary, setDeliverySummary] = useState<DeliverySummary | null>(null);
    // [P1-4] quote 级目标引擎('' = 不定向);[P2-2] 下一步卡
    const [targetEngine, setTargetEngine] = useState<string>('');
    const [nextStep, setNextStep] = useState<NextStepInfo | null>(null);
    const deliverySummaryRequestRef = useRef(0);
    const [showAdvancedWritingOptions, setShowAdvancedWritingOptions] = useState(false);
    const [keywords, setKeywords] = useState<Keyword[]>([]);
    /* [#225 a1 下半] 详情端点给的**篇**合计(`total_planned_posts_default`)。
       存下来是为了让表头直接渲染它,而不是前端 reduce —— 见 writingCounts.plannedTotal。 */
    const [plannedTotalServer, setPlannedTotalServer] = useState<number | null>(null);
    /*
     * [WO_218-a1] 本次生成标题要扣多少(后端算好的,前端只显示)。
     * 🔴 **单独一份 state,不挂在 `selectedProject` 上**:`loadProjectDetail()`
     *    从不 `setSelectedProject`(见 3020 那个函数),硬塞进去等于新加一条接线,
     *    而新接线就是新的会断的地方。挂在这里,它**恰好在关键词数变化时刷新** ——
     *    因为 add-keyword 成功后调的就是 `loadProjectDetail`。
     * 🔴 `null` = 不显示价(本次不走计费 / 后端没给 / 回包不合契约),**不是 0**。
     */
    const [topicGenCharge, setTopicGenCharge] = useState<TopicGenCharge | null>(null);
    /*
     * [WO_243 乙] 「新词面」的价 —— 后端 `charge_new_keywords_only`
     * (还没有任何选题行的那些词的价,**空集给 `null` 不给 0**)。
     * 🔴 为什么不能拿 `topicGenCharge` 顶:那是**整表**的价。
     *    10 个词的项目加 2 个新词,拿整表价显示 = 说 10 份而实扣 2 份。
     *    差额**不许前端自己算** —— 契约逐字禁止,而这正是那条禁令防的那种算。
     */
    const [topicGenChargeNewOnly, setTopicGenChargeNewOnly] = useState<TopicGenCharge | null>(null);
    const [topics, setTopics] = useState<Topic[]>([]);
    const [generating, setGenerating] = useState(false);
    const [retryingOptimizeTitles, setRetryingOptimizeTitles] = useState(false);
    // v2.7.2 GEO 文体改造 · 用户文体选择 + 补充要求
    // v2.7.6 删 batchManualMode · dropdown 改为每 topic 默认显示 · 不需模式切换
    const [extraInstruction, setExtraInstruction] = useState('');
    // [2026-06-02 GEO CTO] 写作大厅两开关:自动配图(默认开) / 插入联系方式(默认关·很多媒体审核不过)
    const [addImages, setAddImages] = useState(true);
    const [addContact, setAddContact] = useState(false);
    const [publicationProfile, setPublicationProfile] = useState<PublicationProfile>('standard');
    // v2.9.1 用户改 dropdown 后联动 DB(防 reload 后丢 user_choice · Codex v2.9 P0 修)
    // 流程:
    //   ① 立刻更新本地 user_choice state(乐观更新)
    //   ② 调 regenerate-titles 始终调用(包括 'auto')
    //     · newChoice='auto' → 后端 light path · 清 DB user_choice=NULL · 不进 LLM · 不扣费
    //     · newChoice 非 auto → 后端 LLM 按新文体重写标题 + UPDATE user_choice
    //   ③ 成功:reload topic 看新状态;失败:还原 user_choice + toast
    // 根因(v2.9 P0):原 `if (auto) return` 导致用户选回系统推荐时 DB 仍残留旧文体 ·
    //   reload + start-articles fallback 走旧文体模板 · 反向错账
    const updateTopicUserChoice = async (topicId: number, newChoice: UserChoice) => {
        const prevTopic = topics.find(t => t.id === topicId);
        const isResetToAuto = newChoice === 'auto';
        // 立刻更新 local state(乐观更新)· 非 auto 时 mark regenerating(LLM 重写标题需时间)
        setTopics(prev => prev.map(t => t.id === topicId
            ? { ...t, user_choice: newChoice, status: (!isResetToAuto && t.status === 'pending') ? 'regenerating' : t.status }
            : t
        ));
        try {
            const res = await authFetch("/api/writing/regenerate-titles", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ topic_ids: [topicId], user_choice: newChoice })  // v2.9.1:auto 也传 · 走 light path
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                // 还原 user_choice + 提示
                setTopics(prev => prev.map(t => t.id === topicId ? { ...t, user_choice: prevTopic?.user_choice || 'auto', status: 'pending' } : t));
                toast.error(err.detail || (isResetToAuto ? "重置文体失败 · 请重试" : "标题重写失败 · 请重试"));
                return;
            }
            // 成功 · reload 看 DB 真实状态(user_choice + optimized_title)
            if (selectedProject) await loadProjectDetail(selectedProject.id);
            // v2.9.3 · D2-B 老板拍:light reset 后 toast 加二级 action 给用户主动 LLM 重写选项
            // 默认 light(标题保留 · 符合"恢复系统推荐"预期)
            // 8s 内可点 "重写标题" 二级按钮 · 用户主动选 LLM 重写
            // 根因(老板反馈):light reset 后 optimized_title 不变 · 用户感知像 BUG · 必须显式告知 + 给 LLM 重写选项
            // [v2.9.4 hotfix 2026-05-28] duration 8000 → 12000 · 老板真机反馈 5s 消失
            // 根因:sonner toast 真机渲染受动画 in/out + 焦点抢占影响 · 8000 设定 < 实际可见时长
            // 修:加到 12000 · 保证真机至少可见 8 秒(action 按钮有效期)
            // [2026-06-03 全站静默扣费] action label 去金额 · 仅留动作
            if (isResetToAuto) {
                toast.success("已恢复为系统推荐 · 标题保留", {
                    description: "正文将按系统推荐生成 · 如需重新生成标题,可点右侧按钮",
                    duration: 12000,
                    action: {
                        label: `重写标题`,
                        onClick: () => { void llmRewriteAfterReset(topicId); }
                    }
                });
            } else {
                toast.success("标题已按新文体重写");
            }
        } catch (e) {
            setTopics(prev => prev.map(t => t.id === topicId ? { ...t, user_choice: prevTopic?.user_choice || 'auto', status: 'pending' } : t));
            toast.error(isResetToAuto ? "重置文体网络异常 · 请重试" : "标题重写网络异常 · 请重试");
        }
    };

    // v2.9.3 · D2-B 老板拍:light reset 后用户主动选"也想重写标题(扣费)"
    // 不传 user_choice → 后端 _is_light_reset=false → 走 LLM 默认 ratio 重写(原批量重写行为)
    const llmRewriteAfterReset = async (topicId: number) => {
        setTopics(prev => prev.map(t => t.id === topicId
            ? { ...t, status: 'regenerating' }
            : t
        ));
        try {
            const res = await authFetch("/api/writing/regenerate-titles", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ topic_ids: [topicId] })  // 不传 user_choice · 走 LLM 默认 ratio
            });
            if (!res.ok) {
                const err = await res.json().catch(() => ({}));
                setTopics(prev => prev.map(t => t.id === topicId ? { ...t, status: 'pending' } : t));
                toast.error(err.detail || "标题重写失败 · 请重试");
                return;
            }
            if (selectedProject) await loadProjectDetail(selectedProject.id);
            toast.success(`标题已按系统推荐重写`);
        } catch (e) {
            setTopics(prev => prev.map(t => t.id === topicId ? { ...t, status: 'pending' } : t));
            toast.error("标题重写网络异常 · 请重试");
        }
    };
    const [writingInProgress, setWritingInProgress] = useState(false);
    // v2.8 选题生成阶段批量文体(默认"auto"= 系统推荐 · 用户选其他 10 项之一时全部标题强制该文体)
    const [batchUserStyle, setBatchUserStyle] = useState<UserChoice>('auto');
    // v2.10 (CTO-15.23 2026-05-28) · 文章方向配比器 dialog state(老板 A · 高级入口 · 默认收起)
    const [distributionDialogOpen, setDistributionDialogOpen] = useState(false);
    // v2.10.2 P0-1:未生成 topics 时保存配比 · 等"批量生成标题"时一并传后端
    const [pendingDistribution, setPendingDistribution] = useState<Record<string, number> | null>(null);
    const [knowledgeStatus, setKnowledgeStatus] = useState<WritingKnowledgeStatus | null>(null);
    const [knowledgeLoading, setKnowledgeLoading] = useState(false);
    const [knowledgeRawText, setKnowledgeRawText] = useState("");
    /* [#220 a1'''] 「这一对(客户, 项目)的基础资料**确实从服务端读到过**」。
       只有它与当前选中项相等时才允许把表单写回档案 —— 理由见 putWritingBasics。 */
    const knowledgeBasicsLoadedKeyRef = useRef<string>('');
    const [knowledgeBasics, setKnowledgeBasics] = useState<KnowledgeBasicsForm>({
        business_summary: "",
        target_customers: "",
        products_services: "",
        key_selling_points: "",
        proof_cases: "",
        forbidden_notes: "",
    });
    const [knowledgeNotes, setKnowledgeNotes] = useState("");
    const [cleaningKnowledge, setCleaningKnowledge] = useState(false);
    const [generatingKnowledgeLink, setGeneratingKnowledgeLink] = useState(false);
    const [knowledgeLink, setKnowledgeLink] = useState("");
    const [materialsDrawerOpen, setMaterialsDrawerOpen] = useState(false);
    const [brandProfile, setBrandProfile] = useState<any | null>(null);
    const [contactForm, setContactForm] = useState<ContactForm>({
        contact_phone: "",
        contact_wechat: "",
        contact_website: "",
        contact_address: "",
    });
    const [contactSaving, setContactSaving] = useState(false);
    const [brandImageAssets, setBrandImageAssets] = useState<BrandImageAsset[]>([]);
    const [brandAssetsLoading, setBrandAssetsLoading] = useState(false);
    // Phase 06.1 (CTO-15.23 2026-05-03) · 客户知识库面板默认折叠 · 老板拍板 A
    const [knowledgePanelOpen, setKnowledgePanelOpen] = useState(false);
    // 沙盒: 知识库上传模拟状态
    const [sandboxKbUploading, setSandboxKbUploading] = useState(false);
    const [sandboxKbDone, setSandboxKbDone] = useState(false);
    const [structureGuidance, setStructureGuidance] = useState<StructureGuidance | null>(null);
    const [structureGuidanceLoading, setStructureGuidanceLoading] = useState(false);
    const [useStructureGuidance, setUseStructureGuidance] = useState(false);

    // 写作进度
    const [writingProgress, setWritingProgress] = useState<WritingProgress | null>(null);

    // 编辑状态
    const [editingTopicId, setEditingTopicId] = useState<number | null>(null);
    const [editingTitle, setEditingTitle] = useState("");

    // 选中的选题(用于批量重新生成或写作)
    const [selectedTopics, setSelectedTopics] = useState<Set<number>>(new Set());
    // 重新生成loading状态
    const [regenerating, setRegenerating] = useState(false);
    // [CTO-15.23 2026-05-08 死锁修] 单 kw 立即生成 loading
    const [generatingForKwIds, setGeneratingForKwIds] = useState<Set<number>>(new Set());

    // 关键词展开/折叠状态
    const [expandedKeywords, setExpandedKeywords] = useState<Set<number>>(new Set());
    // 优化tab独立的折叠状态（key = "groupKey-kwId"，三个分组互不影响）
    const [optExpandedKws, setOptExpandedKws] = useState<Set<string>>(new Set());

    // 工作台内部Tab: pending(待写), writing(写作中), completed(已完成), optimize(优化)
    const [workbenchTab, setWorkbenchTab] = useState<'pending' | 'writing' | 'completed' | 'optimize'>('pending');

    // 文章预览/编辑状态
    const [previewData, setPreviewData] = useState<{
        id: number;
        topicId: number;
        title: string;
        content: string;
        contentRendered?: string;  // [2026-06-02] 配图渲染后的副本(预览显示用·content 保留 [CLIENT_IMAGE] 占位符供编辑)
        // [工单 T4 2026-07-29] 服务端算的真实渲染数与掉图原因(marker_count 是"想配几张"·rendered_count 才是"出了几张")
        imageRender?: { marker_count: number; rendered_count: number; dropped_count: number; dropped_reasons: Record<string, number> };
        images?: Array<{ index: number; asset_id: number; role: string; caption: string; thumbnail_url: string; title: string; valid: boolean }>;
        brandId?: number;
        contactConsent?: 'enabled' | 'disabled' | 'legacy_unknown';
        lengthGuidance?: {
            summary: string;
            detail: string;
            depth: 'compact' | 'standard' | 'deep';
            evidence_limited: boolean;
            actual_chars: number;
            minimum_chars: number;
            target_chars: number;
            maximum_chars: number;
        };
        isEditing: boolean;
        isRewriting: boolean;
    } | null>(null);
    // [2026-06-02] 配图「更换」选图弹窗
    const [replaceImageIdx, setReplaceImageIdx] = useState<number | null>(null);
    const [galleryAssets, setGalleryAssets] = useState<any[]>([]);
    // [2026-05-27] previewData 切到 null 时(预览关掉) · 自动 resume 移动端教程遮罩 ·
    //   覆盖所有 setPreviewData(null) 调用方 · 不用每处单独加 resume
    useEffect(() => {
        if (!previewData) {
            resumeMobileCoach();
        }
    }, [previewData]);
    const [editContent, setEditContent] = useState("");
    // 重写相关状态
    const [revisionNote, setRevisionNote] = useState("");
    const [referenceArticle, setReferenceArticle] = useState("");
    const [rewriteLoading, setRewriteLoading] = useState(false);

    // 写作设置弹窗
    const [showWritingSettings, setShowWritingSettings] = useState(false);
    const [activeLLM, setActiveLLM] = useState<{ provider: string; model: string } | null>(null);

    // 知识库核查
    const [knowledgeCheckResults, setKnowledgeCheckResults] = useState<Record<number, {
        status: 'pass' | 'warning' | 'fail';
        // [M 方案 · CTO-15.23 2026-05-06] 每条 issue 持久化 · 加 issue_id + dismissed flag
        contradictions: { issue_id?: number; dismissed?: boolean; type: string; kb_data: string; article_data: string; context: string }[];
        verified: string[];
        message: string;
    }>>({});
    const [checkingKnowledge, setCheckingKnowledge] = useState(false);
    const [fixingIssues, setFixingIssues] = useState(false);
    const [knowledgeFiles, setKnowledgeFiles] = useState<KnowledgeFile[]>([]);
    const [knowledgeUploading, setKnowledgeUploading] = useState(false);
    const [knowledgeSavingText, setKnowledgeSavingText] = useState(false);
    const [knowledgeReindexing, setKnowledgeReindexing] = useState(false);
    const [knowledgeDeletingFile, setKnowledgeDeletingFile] = useState<string | null>(null);
    const [knowledgeAutofillState, setKnowledgeAutofillState] = useState<"idle" | "waiting" | "synced" | "delayed">("idle");
    const [knowledgeAutofillMessage, setKnowledgeAutofillMessage] = useState("");
    const knowledgeFileInputRef = useRef<HTMLInputElement>(null);
    const knowledgeAutofillTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
    const knowledgeAutofillRunRef = useRef(0);
    const titleRequestRef = useRef<{ fingerprint: string; requestId: string } | null>(null);
    const articleRequestRef = useRef<{ fingerprint: string; requestId: string } | null>(null);
    const resetRequestRef = useRef<{ fingerprint: string; requestId: string } | null>(null);
    const selectedProjectRef = useRef<WritingProject | null>(selectedProject);
    selectedProjectRef.current = selectedProject;
    const projectDetailRequestRef = useRef(0);
    const structureGuidanceRequestRef = useRef(0);
    const indexedKnowledgeFiles = knowledgeFiles.filter(f => f.vectorized || (f.vector_chunks || 0) > 0).length;
    const hasIndexedKnowledge = indexedKnowledgeFiles > 0;
    const hasKnowledgeBasicsInput = Object.values(knowledgeBasics).some(value => value.trim());
    const hasKnowledgeCleanInput = Boolean(knowledgeRawText.trim() || hasKnowledgeBasicsInput || knowledgeNotes.trim() || hasIndexedKnowledge);
    const canCleanKnowledgeMaterials = Boolean((knowledgeStatus?.brand_id || selectedProject?.brand_id) && hasKnowledgeCleanInput);

    // 找同行 — 三态证据成熟度
    const [competitors, setCompetitors] = useState<{ name: string; desc: string; profile?: string; confidence?: string; source_count?: number; confirmed_by?: string[]; projects?: string[]; excluded?: boolean; name_verified?: boolean; human_verified_name?: boolean }[]>([]);
    const [competitorMode, setCompetitorMode] = useState<CompetitorMode | 'loading'>('evidence_only');
    const [competitorLoadingLabel, setCompetitorLoadingLabel] = useState('搜索中...');
    const [competitorPanelOpen, setCompetitorPanelOpen] = useState(false);
    const [addCompetitorName, setAddCompetitorName] = useState('');
    const [addingCompetitor, setAddingCompetitor] = useState(false);
    // real=已核验, semi=待核验候选, evidence_only=仅写选型标准, loading=检索中

    // Phase 06 (CTO-15.23 2026-05-03 T4) · 快速写作 onSubmit orchestration
    // - mode='create': POST /api/my-clients/add → upload knowledge → POST /api/writing/projects/quick-create
    // - mode='reuse':  直接 POST /api/writing/projects/quick-create with existing brand_id(不传 profile_extras 不覆盖)
    const handleQuickWriteSubmit = useCallback(
        async (data: CustomerIntakeData, mode: SubmitMode, existingBrandId?: number) => {
            let brandId: number | null = null;
            const knowledgeFiles = data.knowledge_files || [];

            if (mode === 'reuse') {
                if (!existingBrandId) throw new Error('reuse 模式缺 brand_id');
                brandId = existingBrandId;
                // reuse 不上传新文件 / 不覆盖 profile · 仅创建 quote
            } else {
                // create 模式 · 1. 创建 brand
                const addResp = await authFetch('/api/my-clients', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        name: data.name,
                        industry: data.industry || '',
                        city: data.city || '',
                        business: data.business || '',
                        seed_keywords: data.seed_keywords || [],
                    }),
                });
                const addJson = await addResp.json().catch(() => ({}));
                if (!addResp.ok || !addJson?.success) {
                    throw new Error(addJson?.detail || addJson?.error || '创建客户失败');
                }
                brandId = addJson.brand_id ?? addJson.id ?? addJson.client?.id;
                if (!brandId) throw new Error('创建客户成功但无 brand_id 返回');

                // 2. 上传知识库文件(并行 · 单条失败 toast 提示 · 不阻塞主流程)
                // [2026-06-07 P0-1a/BUG-1] 原 POST 单数 `/api/profile/{id}/knowledge/upload` —— profile 路由实为复数 `/api/profiles`,
                //   单数路径匹配不到任何路由 → 404 静默丢文件(前端只 console.warn 吞掉 → 客户以为"传了但用不了")。
                //   改用客户统一知识库 multipart 端点 `/api/knowledge/upload-file`(kb_type=client · kb_id=brandId)——
                //   = BrandDetailPage / 写作页内上传同款正确写法 · 落 knowledge_client_{brand_id} 供诊断/报价/写作真正读到。
                if (knowledgeFiles.length > 0) {
                    toast.info(`正在上传 ${knowledgeFiles.length} 个文件 · AI 后台整理中`);
                    let okCount = 0;
                    const failNames: string[] = [];
                    // [2026-06-07 P0 fix v2] 同 uploadKnowledgeFiles · 区分超时网络断 vs 真失败 ·
                    //   原 catch 一律 push failNames → toast.error 假报"上传失败" ·
                    //   prod 实证后端 chunks 已入库 · 客户重复上传 UX 灾难
                    const timeoutNames: string[] = [];
                    await Promise.all(knowledgeFiles.map(async (file) => {
                        const ctrl = new AbortController();
                        const timer = setTimeout(() => ctrl.abort(), 90000);
                        try {
                            const fd = new FormData();
                            fd.append('file', file);
                            fd.append('kb_type', 'client');
                            fd.append('kb_id', String(brandId));
                            const upResp = await authFetch('/api/knowledge/upload-file', {
                                method: 'POST',
                                body: fd,
                                signal: ctrl.signal,
                            });
                            clearTimeout(timer);
                            const upJson = await upResp.json().catch(() => ({} as Record<string, unknown>));
                            if (upResp.ok && upJson?.success !== false && upJson?.status !== 'error') {
                                okCount += 1;
                            } else {
                                failNames.push(file.name);
                                console.warn(`[quick-write] 文件 ${file.name} 上传失败:`, upJson?.error || upJson?.detail || upResp.status);
                            }
                        } catch (e: any) {
                            clearTimeout(timer);
                            const msg = String(e?.message || e?.name || '');
                            if (e?.name === 'AbortError' || /network|fetch|timeout/i.test(msg)) {
                                // 后端可能仍在跑(后端 prod 实证 chunks 已入库)· 不假报失败
                                timeoutNames.push(file.name);
                                console.warn(`[quick-write] 文件 ${file.name} 上传超时/网络断 · 后端可能仍在处理:`, e);
                            } else {
                                failNames.push(file.name);
                                console.warn(`[quick-write] 文件 ${file.name} 上传异常:`, e);
                            }
                        }
                    }));
                    if (okCount > 0) toast.success(`已成功上传 ${okCount} 个文件 · AI 后台整理中`);
                    if (timeoutNames.length > 0) {
                        // 不调用刷新(创建客户场景 · 之后 quick-create 流程会重置)· 仅提示
                        toast.info(
                            `${timeoutNames.length} 个文件较大或网络中断 · 服务器可能仍在处理 · 进入写作大厅后可手动刷新查看`,
                            { duration: 6000 }
                        );
                    }
                    if (failNames.length > 0) toast.error(`${failNames.length} 个文件上传失败:${failNames.join('、')}`);
                }
            }

            // 3. 调 quick-create endpoint
            const profileExtras = mode === 'create' ? {
                business: data.business || undefined,
                target_users: data.target_users || undefined,
                company_intro: data.company_intro || undefined,
                core_value: data.core_value || undefined,
                selling_points: (data.selling_points && data.selling_points.length > 0) ? data.selling_points : undefined,
                success_cases: (data.success_cases && data.success_cases.length > 0) ? data.success_cases : undefined,
                competitors: (data.competitors && data.competitors.length > 0) ? data.competitors : undefined,
            } : undefined;
            // reuse 不传 profile_extras · 不覆盖原资料

            const qcResp = await authFetch('/api/writing/projects/quick-create', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    brand_id: brandId,
                    seed_keywords: data.seed_keywords || [],
                    profile_extras: profileExtras,
                }),
            });
            const qcJson = await qcResp.json().catch(() => ({}));
            if (!qcResp.ok || !qcJson?.success) {
                throw new Error(qcJson?.detail || qcJson?.error || '快速写作创建失败');
            }
            const quoteId = qcJson.quote_id;

            // 4. emit · 让 my-clients 列表刷新看到新客户
            emitBrandUpdated(brandId, mode === 'create' ? 'quick-write-create' : 'quick-write-reuse');

            // 5. 跳写作大厅项目详情(URL 带 quote_id 让 WritingHall 自动选中)
            toast.success(mode === 'create' ? '客户已创建 · 准备开写' : '已复用客户 · 准备开写');
            setSearchParams({ quote_id: String(quoteId) });
            await loadProjects(activeTab);
        },
        [activeTab, setSearchParams],
    );

    // 加载项目列表
    const loadProjects = async (status?: string) => {
        try {
            let url = status && status !== "all"
                ? `/api/writing/projects?status=${status}`
                : "/api/writing/projects";
            // 🔴 2026-08-03 修:原来这里传的是 `brand_name=`,而
            //    `GET /api/writing/projects` 的签名里**根本没有这个参数** ——
            //    FastAPI 直接忽略未声明的 query 参数,所以这个过滤
            //    **从来没有生效过**(不是"前端没传",是传了后端不收)。
            //    症状就是 Owner 看到的:左上角选了客户,大厅照旧列出所有客户的项目。
            //    改传权威的 brand_id,后端已同步加上这个可选参数。
            //    「优化中」tab 不按客户过滤是**既有产品语义**(展示所有有优化内容的
            //    项目),本次原样保留,不借这次改动顺手改掉它。
            if (currentBrandId !== null && status !== 'optimizing') {
                url += (url.includes('?') ? '&' : '?') + `brand_id=${currentBrandId}`;
            }
            const res = await authFetch(url);
            const data = await res.json();
            setProjects(data.projects || []);
        } catch (e) {
            console.error("加载项目失败:", e);
        } finally {
            setLoading(false);
        }
    };

    const loadStructureGuidance = async (projectId: number): Promise<StructureGuidance | null> => {
        const requestGeneration = ++structureGuidanceRequestRef.current;
        setStructureGuidanceLoading(true);
        setStructureGuidance(null);
        setUseStructureGuidance(false);
        try {
            const res = await authFetch(`/api/writing/projects/${projectId}/structure-guidance`);
            const data = await res.json().catch(() => ({}));
            if (requestGeneration !== structureGuidanceRequestRef.current
                || selectedProjectRef.current?.id !== projectId) return null;
            if (!res.ok || data?.success === false) {
                setStructureGuidance(null);
                setUseStructureGuidance(false);
                return null;
            }
            const nextGuidance = data as StructureGuidance;
            setStructureGuidance(nextGuidance);
            setUseStructureGuidance(Boolean(nextGuidance.default_enabled || nextGuidance.can_apply));
            return nextGuidance;
        } catch (e) {
            if (requestGeneration !== structureGuidanceRequestRef.current) return null;
            console.warn("读取文章结构建议失败:", e);
            setStructureGuidance(null);
            setUseStructureGuidance(false);
            return null;
        } finally {
            if (requestGeneration === structureGuidanceRequestRef.current) {
                setStructureGuidanceLoading(false);
            }
        }
    };

    // 加载项目详情
    const loadProjectDetail = async (projectId: number) => {
        const requestGeneration = ++projectDetailRequestRef.current;
        try {
            const res = await authFetch(`/api/writing/projects/${projectId}`);
            const data = await res.json();
            if (requestGeneration !== projectDetailRequestRef.current
                || selectedProjectRef.current?.id !== projectId) return;
            setKeywords(data.keywords || []);
            /*
             * [WO_218-a1] 价与关键词同一次响应、同一个赋值段落 ——
             * 🔴 **必须无条件写**(包括写成 `null`):写成
             *    `if (data.charge) setTopicGenCharge(...)` 的话,
             *    从"要收费"切到"不收费"时屏幕会**停在旧价上**,
             *    而那比不显示更糟 —— 客户是按那个数同意扣费的。
             */
            setTopicGenCharge(parseTopicGenCharge(data.charge));
            /* 同一次响应、同一段赋值,同样**无条件写**(包括写成 null)。 */
            setTopicGenChargeNewOnly(parseTopicGenCharge(data.charge_new_keywords_only));
            setPlannedTotalServer(
                typeof data.total_planned_posts_default === 'number'
                    ? data.total_planned_posts_default : null);
            setTopics((data.topics || []).map((topic: Topic) => ({
                ...topic,
                user_choice: normalizeUserChoice(topic.user_choice),
            })));
            // [P1-4] quote 级引擎定向现值(SELECT * 带出;空 = 不定向)
            setTargetEngine(String(data.quote?.target_engine || ''));
            void loadStructureGuidance(projectId);
            void loadNextStep(projectId);
        } catch (e) {
            console.error("加载项目详情失败:", e);
        }
    };

    // [P2-2] 下一步推导(O1:失败静默,不渲染该卡)
    const loadNextStep = async (projectId: number) => {
        try {
            const res = await authFetch(`/api/writing/projects/${projectId}/next-step`);
            if (!res.ok) { setNextStep(null); return; }
            const data = await res.json() as NextStepInfo;
            if (selectedProjectRef.current?.id !== projectId) return;
            setNextStep(data.available ? data : null);
        } catch {
            setNextStep(null);
        }
    };

    // [P1-4] 引擎定向设定(quote 级;空串 = 取消定向)
    const saveTargetEngine = async (value: string) => {
        if (!selectedProject) return;
        const previous = targetEngine;
        setTargetEngine(value);
        try {
            const res = await authFetch(`/api/writing/projects/${selectedProject.id}/target-engine`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_engine: value }),
            });
            if (!res.ok) throw new Error(String(res.status));
        } catch {
            setTargetEngine(previous);
            toast.error('目标引擎设定失败,请重试');
        }
    };

    const loadDeliverySummary = async (projectId: number) => {
        const requestGeneration = ++deliverySummaryRequestRef.current;
        try {
            const res = await authFetch(`/api/writing/closed-loop/projects/${projectId}/summary`);
            if (!res.ok) return null;
            const data = await res.json() as DeliverySummary;
            if (requestGeneration !== deliverySummaryRequestRef.current) return null;
            setDeliverySummary(data.enabled ? data : null);
            return data;
        } catch (error) {
            if (requestGeneration !== deliverySummaryRequestRef.current) return null;
            // The closed-loop summary is an additive sidecar. A timeout or 500
            // must leave the current writing flow fully usable and quiet.
            console.warn('交付摘要暂不可用，继续使用现役写作流程:', error);
            setDeliverySummary(null);
            return null;
        }
    };

    const retryFailedOptimizeTitles = async (failedTopics: Topic[]) => {
        if (!selectedProject || failedTopics.length === 0) return;
        setRetryingOptimizeTitles(true);
        let queued = 0;
        try {
            for (const topic of failedTopics) {
                const res = await authFetch(`/api/writing/optimize-retry/${topic.id}`, { method: 'POST' });
                const data = await res.json().catch(() => ({}));
                if (!res.ok) {
                    const detail = data.detail?.message || data.detail || `HTTP ${res.status}`;
                    throw new Error(typeof detail === 'string' ? detail : '重试失败');
                }
                queued += 1;
                setTopics(current => current.map(item => item.id === topic.id
                    ? { ...item, status: 'regenerating', optimized_title: '标题生成中...', fail_reason: undefined }
                    : item));
            }
            toast.success(`已重新加入 ${queued} 个标题任务`);
            await loadProjectDetail(selectedProject.id);
        } catch (error) {
            toast.error(`重试失败: ${error instanceof Error ? error.message : '未知错误'}`);
            await loadProjectDetail(selectedProject.id);
        } finally {
            setRetryingOptimizeTitles(false);
        }
    };

    const loadKnowledgeStatus = async (projectId: number): Promise<WritingKnowledgeStatus | null> => {
        setKnowledgeLoading(true);
        try {
            const res = await authFetch(`/api/writing/projects/${projectId}/knowledge-status`);
            const data = await res.json().catch(() => ({}));
            if (selectedProjectRef.current?.id !== projectId) return null;
            if (!res.ok || !data.success) {
                const detail = data?.detail || data?.message || `服务器错误 (${res.status})`;
                toast.error(`客户资料状态读取失败:${detail}`);
                return null;
            }
            const nextStatus = data as WritingKnowledgeStatus;
            setKnowledgeStatus(nextStatus);
            if (nextStatus.token_url) {
                const link = nextStatus.token_url.startsWith('http')
                    ? nextStatus.token_url
                    : `${window.location.origin}${nextStatus.token_url}`;
                setKnowledgeLink(link);
            } else {
                setKnowledgeLink("");
            }
            return nextStatus;
        } catch (e) {
            if (selectedProjectRef.current?.id !== projectId) return null;
            console.error("读取客户资料状态失败:", e);
            toast.error("客户资料状态读取失败，请稍后重试");
            return null;
        } finally {
            if (selectedProjectRef.current?.id === projectId) setKnowledgeLoading(false);
        }
    };

    const cleanKnowledgeMaterials = async () => {
        const brandId = knowledgeStatus?.brand_id || selectedProject?.brand_id;
        if (!selectedProject || !brandId) {
            toast.error("无法定位客户品牌，请先刷新项目");
            return;
        }
        const rawText = buildKnowledgeMaterialContent();
        const notes = knowledgeNotes.trim();
        if (!rawText && !notes && !hasIndexedKnowledge) {
            toast.warning("请先上传或粘贴客户资料");
            return;
        }
        setCleaningKnowledge(true);
        try {
            const res = await authFetch('/api/m3/material-confirm/clean', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    brand_id: brandId,
                    diagnosis_id: knowledgeStatus?.diagnosis_id || undefined,
                    raw_text: rawText,
                    uploaded_file_refs: hasIndexedKnowledge
                        ? knowledgeFiles
                            .filter(file => file.vectorized || (file.vector_chunks || 0) > 0)
                            .map(file => file.relative_path || file.filename)
                        : undefined,
                    notes,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data.success === false) {
                const detail = data?.detail || data?.message || '资料整理失败';
                toast.error(detail);
                return;
            }
            const nextBasics = mergeKnowledgeBasicsFromCleaned(knowledgeBasics, data?.cleaned_materials);
            setKnowledgeBasics(nextBasics);
            setKnowledgeRawText("");
            setKnowledgeNotes("");
            setKnowledgeAutofillState("synced");
            setKnowledgeAutofillMessage("资料整理完成，已自动回填上方基础资料表，并同步到客户档案页。");
            await loadKnowledgeStatus(selectedProject.id);
            await loadKnowledgeFiles(brandId);
            await loadWritingBrandAssets(brandId);
            notifyKnowledgeMaterialsCleaned(brandId);
            toast.success("AI已整理客户资料，并回填基础资料表");
        } catch (e) {
            console.error("整理客户资料失败:", e);
            toast.error("整理客户资料失败，请稍后重试");
        } finally {
            setCleaningKnowledge(false);
        }
    };

    // [WO_IOS_TOUCH_UX 2026-08-05] 🔴 不能是 async。原写法 writeText 排在 await 之后 →
    //   iOS 手势已过期,静默拦掉(还 `.catch(() => undefined)` 吞了失败),
    //   却照样 toast「已生成并复制」= 假成功。链接本身有 setKnowledgeLink 由 UI 展示(可选中),
    //   所以这里只修手势链 + 让文案按**实际结果**说话。
    const generateKnowledgeLink = () => {
        if (!selectedProject || !knowledgeStatus?.brand_id) return;
        const projectId = selectedProject.id;
        setGeneratingKnowledgeLink(true);
        void copyAsyncText(async () => {
            const res = await authFetch('/api/m3/material-confirm/generate-link', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ brand_id: knowledgeStatus.brand_id, force_new: false }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || !data.success) {
                throw new Error(data?.detail || data?.message || '确认链接生成失败');
            }
            const url = data.url || data.token_url;
            if (!url) throw new Error('确认链接生成成功，但后端没有返回链接');
            const nextUrl: string = url.startsWith('http') ? url : `${window.location.origin}${url}`;
            setKnowledgeLink(nextUrl);
            return nextUrl;
        })
            .then(async ({ ok, text, errorMessage }) => {
                if (!text) {
                    toast.error(errorMessage || '客户确认链接生成失败，请稍后重试');
                    return;
                }
                await loadKnowledgeStatus(projectId);
                if (ok) toast.success("客户确认链接已生成并复制");
                else toast.info("客户确认链接已生成 · 自动复制没成功，请长按下方链接复制");
            })
            .finally(() => {
                setGeneratingKnowledgeLink(false);
            });
    };

    // [WO_IOS_TOUCH_UX 2026-08-05] 手势链本来是好的(writeText 是第一句),但 `.catch(() => undefined)`
    //   把失败吞了之后**无条件** toast「已复制」= 假成功。改按实际结果说话;
    //   链接就在旁边 UI 上(knowledgeLink),失败时用户可以直接长按选中。
    const copyKnowledgeLink = () => {
        if (!knowledgeLink) return;
        void copyToClipboard(knowledgeLink).then((ok) => {
            if (ok) toast.success("链接已复制");
            else toast.info("自动复制没成功 · 请长按上方链接手动复制");
        });
    };

    const updateKnowledgeBasics = (field: keyof KnowledgeBasicsForm, value: string) => {
        setKnowledgeBasics(prev => ({ ...prev, [field]: value.slice(0, 3000) }));
    };

    const buildKnowledgeBasicsText = () => {
        const rows = ([
            ["一句话业务描述", knowledgeBasics.business_summary],
            ["目标客户", knowledgeBasics.target_customers],
            ["产品/服务", knowledgeBasics.products_services],
            ["核心卖点", knowledgeBasics.key_selling_points],
            ["案例/口碑", knowledgeBasics.proof_cases],
            ["禁用表达", knowledgeBasics.forbidden_notes],
        ] as Array<[string, string]>).filter(([, value]) => value.trim());

        if (rows.length === 0) return "";
        return [
            "## 基础资料表",
            ...rows.map(([label, value]) => `### ${label}\n${value.trim()}`),
        ].join("\n\n");
    };

    const buildKnowledgeMaterialContent = () => [
        buildKnowledgeBasicsText(),
        knowledgeRawText.trim(),
    ].filter(Boolean).join("\n\n");

    /*
     * [#220 a1 ⓐ] 只清**这一次输入**:粘贴进来的原始资料 + 备注。
     *
     * 🔴 基础资料表那六个字段**不在这里** —— 它们是「这个客户是谁」的事实,
     *    不是一次性草稿。保存完把它们一起抹掉,就是 Owner 报的
     *    「第一次会出现,第二次打开是空的」。
     * 换客户时该清的仍然清:那条路走 `resetKnowledgeDraft()`(下面那个)。
     */
    const resetKnowledgeInputsOnly = () => {
        setKnowledgeRawText("");
        setKnowledgeNotes("");
    };

    /* 整份草稿清空 —— 只给**换客户 / 退出到列表**用(见 enterWorkbench / backToList)。
       🔴 保存成功后不许再调它,否则就是上面那条缺陷。 */
    const resetKnowledgeDraft = () => {
        resetKnowledgeInputsOnly();
        setKnowledgeBasics({
            business_summary: "",
            target_customers: "",
            products_services: "",
            key_selling_points: "",
            proof_cases: "",
            forbidden_notes: "",
        });
    };

    const loadWritingBrandAssets = useCallback(async (brandId?: number | null): Promise<any | null> => {
        if (!brandId) {
            setBrandProfile(null);
            setContactForm({ contact_phone: "", contact_wechat: "", contact_website: "", contact_address: "" });
            setBrandImageAssets([]);
            return null;
        }
        setBrandAssetsLoading(true);
        try {
            const [imageRes, clientRes] = await Promise.allSettled([
                authFetch(`/api/brand-images/list/${brandId}`),
                authFetch(`/api/my-clients/${brandId}`),
            ]);
            if (selectedProjectRef.current?.brand_id !== brandId) return null;

            if (imageRes.status === "fulfilled") {
                const imageData = await imageRes.value.json().catch(() => ({}));
                setBrandImageAssets(Array.isArray(imageData?.assets) ? imageData.assets : []);
            } else {
                setBrandImageAssets([]);
            }

            let source: "client" | "self" | "unknown" = "unknown";
            let brand: any = null;
            let profile: any = null;

            if (clientRes.status === "fulfilled" && clientRes.value.ok) {
                const data = await clientRes.value.json().catch(() => ({}));
                brand = data?.brand || null;
                profile = data?.profile || null;
                source = "client";
            } else {
                const selfRes = await authFetch("/api/my-brand");
                const data = await selfRes.json().catch(() => ({}));
                if (selectedProjectRef.current?.brand_id !== brandId) return null;
                if (selfRes.ok && data?.brand?.id === brandId) {
                    brand = data.brand;
                    profile = data.profile || null;
                    source = "self";
                }
            }

            const nextProfileBundle = { brand, profile, source };
            setBrandProfile(nextProfileBundle);
            setContactForm({
                contact_phone: profile?.contact_phone || "",
                contact_wechat: profile?.contact_wechat || "",
                contact_website: profile?.contact_website || "",
                contact_address: profile?.contact_address || "",
            });
            return nextProfileBundle;
        } catch (e) {
            if (selectedProjectRef.current?.brand_id !== brandId) return null;
            console.warn("读取写作资料摘要失败:", e);
            setBrandProfile(null);
            setBrandImageAssets([]);
            return null;
        } finally {
            if (selectedProjectRef.current?.brand_id === brandId) setBrandAssetsLoading(false);
        }
    }, []);

    const saveContactProfile = async () => {
        const brandId = selectedProject?.brand_id;
        if (!brandId) {
            toast.error("缺少客户ID，无法保存联系方式");
            return;
        }
        const endpoint = brandProfile?.source === "self" ? "/api/my-brand" : `/api/my-clients/${brandId}`;
        setContactSaving(true);
        try {
            const res = await authFetch(endpoint, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(contactForm),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data?.success === false) {
                throw new Error(data?.detail || data?.error || "保存失败");
            }
            toast.success("联系方式已同步到客户档案");
            await loadWritingBrandAssets(brandId);
        } catch (e: any) {
            toast.error(e?.message || "联系方式保存失败");
        } finally {
            setContactSaving(false);
        }
    };

    const loadKnowledgeFiles = useCallback(async (brandId?: number | null) => {
        if (!brandId) {
            setKnowledgeFiles([]);
            return;
        }
        setKnowledgeLoading(true);
        try {
            const res = await authFetch(`/api/knowledge/client/${brandId}`);
            const data = await res.json().catch(() => ({}));
            if (selectedProjectRef.current?.brand_id !== brandId) return;
            if (!res.ok || data?.success === false) {
                throw new Error(data?.detail || data?.error || "知识库文件加载失败");
            }
            setKnowledgeFiles(data.documents || []);
        } catch (err) {
            if (selectedProjectRef.current?.brand_id !== brandId) return;
            console.error("加载客户知识库失败:", err);
            setKnowledgeFiles([]);
        } finally {
            if (selectedProjectRef.current?.brand_id === brandId) setKnowledgeLoading(false);
        }
    }, []);

    const clearKnowledgeAutofillPolling = () => {
        if (knowledgeAutofillTimerRef.current) {
            clearTimeout(knowledgeAutofillTimerRef.current);
            knowledgeAutofillTimerRef.current = null;
        }
        knowledgeAutofillRunRef.current += 1;
    };

    const notifyKnowledgeMaterialsCleaned = (brandId: number) => {
        emitBrandUpdated(brandId, "writing-knowledge-autofill");
        if (typeof window !== "undefined") {
            window.dispatchEvent(
                new CustomEvent("brand:materials-cleaned", { detail: { brandId } }),
            );
        }
    };

    const refreshKnowledgeAutofill = async (brandId: number, projectId?: number | null) => {
        const [status, profileBundle] = await Promise.all([
            projectId ? loadKnowledgeStatus(projectId) : Promise.resolve(null),
            loadWritingBrandAssets(brandId),
        ]);
        await loadKnowledgeFiles(brandId);
        if (profileBundle || status?.materials_summary) {
            setKnowledgeBasics(prev => mergeKnowledgeBasicsFromProfile(prev, profileBundle, status?.materials_summary));
        }
        /* 🔴 「读到了 profile」才算数,不是「请求回来了」:
           my-clients 非 2xx 时这里会拿到 {brand:null, profile:null, source:'unknown'}
           —— 一个真值,但里面什么都没有。拿它当加载成功,就会把空表写回档案。 */
        if (profileBundle?.profile) {
            knowledgeBasicsLoadedKeyRef.current = `${brandId}:${projectId ?? ''}`;
        }
        return status;
    };

    const startKnowledgeAutofillPolling = (
        brandId: number,
        projectId?: number | null,
        initialDelayMs = 6000,
    ) => {
        clearKnowledgeAutofillPolling();
        const runId = knowledgeAutofillRunRef.current + 1;
        knowledgeAutofillRunRef.current = runId;
        setKnowledgeAutofillState("waiting");
        setKnowledgeAutofillMessage("AI 正在整理资料；完成后会自动回填上方基础资料表，并同步到客户档案页。文件较大时可以先继续写作。");

        let attempt = 0;
        const maxAttempts = 24;

        const poll = async () => {
            if (knowledgeAutofillRunRef.current !== runId) return;
            const currentProject = selectedProjectRef.current;
            if (projectId && (!currentProject || currentProject.id !== projectId || currentProject.brand_id !== brandId)) {
                return;
            }

            attempt += 1;
            try {
                const status = await refreshKnowledgeAutofill(brandId, projectId);
                if (knowledgeAutofillRunRef.current !== runId) return;
                if (hasAutofillReadyMaterial(status)) {
                    if (knowledgeAutofillTimerRef.current) {
                        clearTimeout(knowledgeAutofillTimerRef.current);
                        knowledgeAutofillTimerRef.current = null;
                    }
                    setKnowledgeAutofillState("synced");
                    setKnowledgeAutofillMessage("资料整理完成，已自动回填上方基础资料表，并同步到客户档案页。");
                    notifyKnowledgeMaterialsCleaned(brandId);
                    toast.success("客户资料已整理完成，并回填到基础资料表");
                    return;
                }
            } catch (e) {
                console.warn("等待客户资料自动回填失败:", e);
            }

            if (attempt >= maxAttempts) {
                setKnowledgeAutofillState("delayed");
                setKnowledgeAutofillMessage("资料仍在后台整理。完成后会进入客户档案页；你也可以稍后点“刷新资料”或“整理/更新客户档案”。");
                return;
            }

            const nextDelay = attempt < 6 ? 5000 : 10000;
            knowledgeAutofillTimerRef.current = setTimeout(poll, nextDelay);
        };

        knowledgeAutofillTimerRef.current = setTimeout(poll, initialDelayMs);
    };

    const uploadKnowledgeFiles = async (files: File[]) => {
        const brandId = selectedProject?.brand_id;
        if (!brandId) {
            toast.error("缺少客户ID，无法上传到客户知识库");
            return;
        }
        if (files.length === 0) return;

        // [2026-06-13 客户报障] 单文件 ≤ 20MB:超限当场剔除 + 明确提示(原无检查 · 大文件走后端解析超时 → 客户以为 BUG)
        const MAX_UPLOAD = 20 * 1024 * 1024;
        const _oversized = files.filter((f) => f.size > MAX_UPLOAD);
        if (_oversized.length) {
            toast.error(`${_oversized.map((f) => f.name).join('、')} 超过 20MB，无法上传 · 请压缩或拆分后再传`, { duration: 6000 });
            files = files.filter((f) => f.size <= MAX_UPLOAD);
            if (files.length === 0) return;
        }

        setKnowledgeUploading(true);
        let successCount = 0;
        let savedWithoutAiCount = 0;
        // [2026-06-07 P0 fix] 超时/网络断开列表 · 区分于真失败:
        //   prod 实证(blue 容器 08:09 UTC)客户 docx 上传 backend logger info 显示
        //   "[client-kb] 上传并向量入库 chunks=3" + "上传后自动整理完成" 全部成功 ·
        //   但客户看到 toast "上传失败"(uvicorn access log 0 行 = 微信 WebView 提前断开)
        //   根因:WORKERS=1 + docx 解析 + dashscope embedding + LanceDB 入库 串行 5-15s ·
        //         微信内 fetch 默认 timeout 短 · res.ok=false 触发"失败" · 用户重复上传
        //   修法:AbortController 90s · 超时/网络断开 → toast.info "后台处理中" + 30s 后自动 reload
        const timeoutNames: string[] = [];
        let failedMessage = "";
        try {
            for (const file of files) {
                const formData = new FormData();
                formData.append("file", file);
                formData.append("kb_type", "client");
                formData.append("kb_id", String(brandId));

                // [2026-06-07 P0 fix] 90s timeout 保险 · 大文件后端 5-15s 是常态 · 不能因 fetch 默认 timeout 假报失败
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 90000);

                try {
                    const res = await authFetch("/api/knowledge/upload-file", {
                        method: "POST",
                        body: formData,
                        signal: ctrl.signal,
                    });
                    clearTimeout(timer);
                    const data = await res.json().catch(() => ({}));
                    if (!res.ok || data?.success === false) {
                        failedMessage = data?.detail || data?.error || `${file.name} 上传失败`;
                        continue;
                    }
                    if (data?.vectorized === false || data?.pipeline?.processed === false) {
                        savedWithoutAiCount += 1;
                    }
                    successCount += 1;
                } catch (err: any) {
                    clearTimeout(timer);
                    // [2026-06-07 P0 fix] AbortError(超时)/ NetworkError → 后端可能仍在跑 · 不是真失败 ·
                    //   不再让用户看到"失败"字样 · 用 30s 后自动 reload 揭晓真相
                    const msg = String(err?.message || err?.name || "");
                    if (err?.name === "AbortError" || /network|fetch|timeout/i.test(msg)) {
                        timeoutNames.push(file.name);
                    } else {
                        failedMessage = msg || `${file.name} 上传失败`;
                    }
                }
            }

            if (successCount > 0) {
                if (savedWithoutAiCount > 0) {
                    toast.info(
                        `已保存 ${successCount} 个客户资料；AI 正在后台整理，完成后会自动回填基础资料表和客户档案页`,
                        { duration: 6000 }
                    );
                } else {
                    toast.success(`已上传 ${successCount} 个客户资料 · AI 整理中，完成后会自动回填`);
                }
                await loadKnowledgeFiles(brandId);
                if (selectedProject) await loadKnowledgeStatus(selectedProject.id);
                startKnowledgeAutofillPolling(brandId, selectedProject?.id || null, 6000);
            }
            // [2026-06-07 P0 fix] 超时类提示"可能仍在处理" · 不说"失败"(后端 prod 实证 chunks 已入库)
            //   ⚠️ 老板审核 P2:90s abort 后浏览器/网关也可能真取消请求 · 旧文案"正在处理"过度承诺 ·
            //                    改"可能仍在处理" + 加"或网络中断"覆盖网络中断场景
            if (timeoutNames.length > 0) {
                toast.info(
                    `${timeoutNames.length} 个文件较大或网络中断 · 服务器可能仍在处理 · 30 秒后自动刷新查看结果`,
                    { duration: 6000 }
                );
                // [2026-06-07 P0 fix v2 老板审核] 闭包旧 brandId / selectedProject 守卫:
                //   30 秒延迟期间用户可能切项目 · 不能写错页面状态 ·
                //   用 selectedProjectRef.current 跟上传时 brandId 比对 · 不匹配则跳过刷新
                const uploadedBrandId = brandId;
                startKnowledgeAutofillPolling(uploadedBrandId, selectedProject?.id || null, 30000);
                setTimeout(() => {
                    const currentProject = selectedProjectRef.current;
                    if (!currentProject || currentProject.brand_id !== uploadedBrandId) {
                        // 用户已切到别的项目 · 不再刷新旧 brand 状态(防覆盖)
                        return;
                    }
                    void loadKnowledgeFiles(uploadedBrandId);
                    void loadKnowledgeStatus(currentProject.id);
                }, 30000);
            }
            if (failedMessage) toast.error(failedMessage);
        } catch (err: any) {
            toast.error(err?.message || "上传失败，请重试");
        } finally {
            setKnowledgeUploading(false);
            if (knowledgeFileInputRef.current) knowledgeFileInputRef.current.value = "";
        }
    };

    const handleKnowledgeFileSelect = (e: ChangeEvent<HTMLInputElement>) => {
        void uploadKnowledgeFiles(Array.from(e.target.files || []));
    };

    /*
     * 🔴 [#234-a1 2026-09-17] 沙盒教程第三步的模拟上传。
     *
     *    这段逻辑原样住在 `renderKnowledgePanel` 里 —— 而那个函数**全仓零调用点**,
     *    于是:活代码把用户推到 `step3-upload-kb-click`,却没有任何一处会响应,
     *    教程第三步在生产上直接卡死。搬出来给它一个**会被真的挂上去**的家。
     *
     *    逻辑逐字沿用,没有重写:它已经写好且审过,重写只会引入语义漂移。
     *    `quote_id` 必带 —— 否则 `startWriting` 会重 fetch 把状态拉回 'none'。
     */
    const handleSandboxKbUploadClick = (e: ReactMouseEvent<HTMLLabelElement>) => {
        if (!(isSandboxActive() && tutorialStage === 'step3-upload-kb-click')) return;
        e.preventDefault();
        setSandboxKbUploading(true);
        window.setTimeout(() => {
            setSandboxKbUploading(false);
            setSandboxKbDone(true);
            setKnowledgeStatus(prev => ({
                ...(prev || {}),
                success: true,
                status: 'confirmed' as const,
                can_start_writing: true,
                brand_id: selectedProject?.brand_id || prev?.brand_id,
                quote_id: selectedProject?.id || prev?.quote_id,
                token_url: null,
                message: '客户资料已上传 (教程模式)',
                materials_summary: {
                    company_name: '一路顺风出行服务',
                    industry: '高端商务用车 / 豪华专车服务',
                    intro_excerpt: '一路顺风出行服务·深圳高端商务用车专家·埃尔法 / 奔驰 V 级 / 库里南车型 + 双语司机.',
                    usp_excerpt: '司机背调认证·隐私协议保护·跨境电商 / 外资企业首选',
                    fields_filled: ['company_intro', 'products', 'usp', 'cases', 'tone'],
                    fields_missing: [],
                    filled_count: 5,
                    total_fields: 5,
                },
            } as WritingKnowledgeStatus));
            setKnowledgeFiles([{
                id: 1001,
                file_name: '一路顺风出行服务·官网介绍.pdf',
                file_size: 245760,
                uploaded_at: new Date().toISOString(),
                vectorized: true,
                vector_chunks: 12,
                status: 'indexed',
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            } as any]);
            toast.success('客户资料已上传 + 向量入库 (教程模式)');
            window.setTimeout(() => {
                setTutorialStage('step3-gen-titles');
            }, 800);
        }, 1500);
    };

    const handleKnowledgeDrop = (e: DragEvent<HTMLLabelElement>) => {
        e.preventDefault();
        void uploadKnowledgeFiles(Array.from(e.dataTransfer.files || []));
    };

    /*
     * [#220 a1'] 把基础资料表**结构化**写回客户档案。
     *
     * 在此之前保存只做一件事:把六个字段拍平成一个带时间戳的 markdown 传进知识库。
     * 那份 markdown 没人再读回表里 ⇒ 刷新页面就白了。220-c2 加了结构化落点,
     * 这里补上写的那一半。
     *
     * 🔴 六个字段**全传**,包括空串 —— 契约是「不传=不覆盖、显式空串=清空」
     *    (后端 `update_data = {k: v for k, v in data.dict().items() if v is not None}`)。
     *    只传非空的话,用户清掉某一栏再保存,服务端那栏会原样留着,
     *    下次打开又被重拉回来 —— 看起来就是「删不掉」。
     * 🔴 markdown 那条**照旧**发:它是给知识库检索用的,和结构化落点是两个用途,
     *    不是新旧替换关系。
     * 🔴 拿不到 profile id 就不发,也不假装成功 —— 宁可少做一件事,不做一件看不出来的错事。
     */
    const putWritingBasics = async () => {
        const profileId = brandProfile?.profile?.id;
        if (!profileId) {
            /* 🔴 [#220 a2] 这条分支原来只写 console.warn ——
               用户看到的是「客户资料已保存」,而基础资料其实一个字都没进档案,
               下次打开才发现白写了。跳过要让**用户**知道,不能只让开发者知道。
               console.warn 保留:它是判据用来区分「是哪一条守卫挡的」的痕迹
               (两条守卫产生的 PUT 次数都是 0,数 0 分不开)。 */
            console.warn('[220-a1] 没有 profile id,基础资料未结构化写回(markdown 仍已保存)');
            toast.warning('客户档案还没同步好,基础资料这次没有写回档案;资料本身已保存');
            return;
        }
        /*
         * 🔴 [#220 a1''' · 复审点名] **没成功加载过就不许写回**,否则会静默清空真客户档案。
         *
         *    表单里那六个值来自「打开抽屉时的重拉」。而 `enterWorkbench` 那次加载
         *    只填 `brandProfile`(于是 profileId 就位),**不填基础资料表**。
         *    所以存在这样一个窗口:profileId 有了、表单还是空的、重拉还在路上——
         *    用户这时按保存,六个空串就被送出去,
         *    而契约里「显式空串 = 清空」⇒ 档案里已有的四个真列被抹掉,零报错。
         *    网络慢 / 500 / 秒开秒存,三种都能落进这个窗口。
         *
         * 🔴 标志按 **(客户, 项目)** 记,不用布尔:换了客户而新客户的重拉还没回来时,
         *    布尔会因为上一个客户加载过而放行 —— 那是把 A 的空表写进 B 的档案。
         */
        const key = `${selectedProject?.brand_id ?? ''}:${selectedProject?.id ?? ''}`;
        if (knowledgeBasicsLoadedKeyRef.current !== key) {
            toast.warning('客户档案还没同步好,基础资料这次没有写回档案;资料本身已保存');
            return;
        }
        try {
            const res = await authFetch(`/api/profiles/${encodeURIComponent(String(profileId))}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    business_summary: knowledgeBasics.business_summary,
                    target_customers: knowledgeBasics.target_customers,
                    products_services: knowledgeBasics.products_services,
                    key_selling_points: knowledgeBasics.key_selling_points,
                    proof_cases: knowledgeBasics.proof_cases,
                    forbidden_notes: knowledgeBasics.forbidden_notes,
                }),
            });
            if (!res.ok) {
                /* 🔴 出声,但不把整次保存判失败:markdown 那条已经成了。
                   静默失败会让用户以为存住了,下次打开才发现没有。 */
                toast.warning('基础资料未能同步到客户档案,下次打开可能看不到;资料本身已保存');
            }
        } catch {
            toast.warning('基础资料未能同步到客户档案,下次打开可能看不到;资料本身已保存');
        }
    };

    const saveKnowledgeText = async () => {
        const brandId = selectedProject?.brand_id;
        if (!brandId) {
            toast.error("缺少客户ID，无法保存到客户知识库");
            return;
        }
        const content = [
            buildKnowledgeMaterialContent(),
            knowledgeNotes.trim() ? `\n\n备注：${knowledgeNotes.trim()}` : "",
        ].join("").trim();
        if (!content) {
            toast("请先填写基础资料或粘贴客户资料");
            return;
        }

        setKnowledgeSavingText(true);
        /*
         * 🔴 [#220 a1' · 端到端实测逼出来的] 结构化那一发**先走,而且独立走**。
         *
         *    第一版我把它放在 markdown 上传成功之后。真后端上 `/api/knowledge/upload`
         *    回了 500(那台机器的向量库没起来),于是 `throw` 把后面全跳过 ——
         *    **六个基础字段一个都没落库**,而用户看到的只是一句「保存失败,请重试」。
         *    重试也没用:知识库一直坏着,基础资料就一直存不进去。
         *    这两件事本来是两个用途(检索用的 markdown / 结构化的客户事实),
         *    不该一个坏了把另一个也带走。
         *    桩全绿时看不见这条 —— 桩里 knowledge/upload 永远回 200。
         */
        await putWritingBasics();
        try {
            const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
            const res = await authFetch("/api/knowledge/upload", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    kb_type: "client",
                    kb_id: String(brandId),
                    filename: `写作补充资料_${stamp}.md`,
                    content,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data?.success === false) {
                throw new Error(data?.detail || data?.error || "保存失败");
            }
            /* [#220 a1 ⓐ] 原来这里是 `resetKnowledgeDraft()`,把六个基础字段一起抹了。
               保留它们的理由见 `resetKnowledgeInputsOnly` 的注释。 */
            resetKnowledgeInputsOnly();
            toast.success("客户资料已保存");
            await loadKnowledgeFiles(brandId);
            if (selectedProject) await loadKnowledgeStatus(selectedProject.id);
        } catch (err: any) {
            toast.error(err?.message || "保存失败，请重试");
        } finally {
            setKnowledgeSavingText(false);
        }
    };

    const deleteKnowledgeFile = async (file: KnowledgeFile) => {
        const brandId = selectedProject?.brand_id;
        if (!brandId) return;
        if (!(await askConfirm({ title: `确定删除 "${file.filename}"？`, danger: true }))) return;

        const fileKey = file.relative_path || file.filename;
        setKnowledgeDeletingFile(fileKey);
        try {
            const candidates = Array.from(new Set([file.relative_path, file.filename].filter(Boolean) as string[]));
            let deleted = false;
            let lastError = "删除失败";

            for (const candidate of candidates) {
                const filename = encodeURIComponent(candidate);
                const res = await authFetch(`/api/knowledge/client/${brandId}/${filename}`, { method: "DELETE" });
                const data = await res.json().catch(() => ({}));
                if (res.ok && data?.success !== false) {
                    deleted = true;
                    break;
                }
                lastError = data?.detail || data?.error || `删除失败 (${res.status})`;
                if (res.status !== 404) break;
            }

            if (!deleted) {
                throw new Error(lastError);
            }
            toast.success("已删除知识库文件");
            setKnowledgeFiles(prev => prev.filter(item => (
                (item.relative_path || item.filename) !== fileKey && item.filename !== file.filename
            )));
            await loadKnowledgeFiles(brandId);
            if (selectedProject) await loadKnowledgeStatus(selectedProject.id);
        } catch (err: any) {
            toast.error(err?.message || "删除失败");
        } finally {
            setKnowledgeDeletingFile(null);
        }
    };

    const reindexKnowledgeFiles = async () => {
        const brandId = selectedProject?.brand_id;
        if (!brandId) return;

        setKnowledgeReindexing(true);
        try {
            const res = await authFetch(`/api/knowledge/client/${brandId}/reindex`, { method: "POST" });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data?.success === false) {
                const detail = data?.detail;
                const message = typeof detail === "string" ? detail : detail?.message;
                throw new Error(message || data?.error || "AI 重新学习失败");
            }
            toast.success(`AI 已重新学习 ${data.indexed || 0} 个客户资料`);
            await loadKnowledgeFiles(brandId);
        } catch (err: any) {
            toast.error(err?.message || "AI 重新学习失败");
        } finally {
            setKnowledgeReindexing(false);
        }
    };

    // 加载同行列表及模式
    const loadCompetitors = async (projectId: number) => {
        try {
            const res = await authFetch(`/api/writing/competitors/${projectId}`);
            const data = await res.json();
            if (selectedProjectRef.current?.id !== projectId) return;
            if (data.competitors && data.competitors.length > 0) {
                setCompetitors(data.competitors);
                setCompetitorMode(normalizeCompetitorMode(data.mode || 'real'));
            } else {
                setCompetitors([]);
                setCompetitorMode('evidence_only');
            }
        } catch {
            if (selectedProjectRef.current?.id !== projectId) return;
            setCompetitors([]);
            setCompetitorMode('evidence_only');
        }
    };

    // 联网搜索同行
    const researchCompetitors = async () => {
        if (!selectedProject) return;
        const previousMode = competitorMode === 'loading' ? 'evidence_only' : competitorMode;
        setCompetitorLoadingLabel('搜索中...');
        setCompetitorMode('loading');
        try {
            const res = await authFetch(`/api/writing/research-competitors/${selectedProject.id}`, { method: 'POST' });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || '找同行失败');
            if (data.competitors && data.competitors.length > 0) {
                setCompetitors(data.competitors);
                setCompetitorMode(normalizeCompetitorMode(data.mode || 'semi'));
            } else {
                setCompetitorMode('evidence_only');
            }
        } catch (e) {
            console.error("找同行失败:", e);
            setCompetitorMode(previousMode);
            toast.error(e instanceof Error ? e.message : '找同行失败，请稍后重试');
        }
    };

    const verifyCurrentCompetitors = async () => {
        if (!selectedProject) return;
        const previousMode = competitorMode === 'loading' ? 'semi' : competitorMode;
        setCompetitorLoadingLabel('核验中...');
        setCompetitorMode('loading');
        try {
            const res = await authFetch(`/api/writing/competitors/${selectedProject.id}/verify`, {
                method: 'POST'
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || '名称核验失败');
            if (Array.isArray(data.competitors)) setCompetitors(data.competitors);
            const nextMode = normalizeCompetitorMode(data.mode || 'semi');
            setCompetitorMode(nextMode);
            if (nextMode === 'real') {
                toast.success(`名称核验完成，${data.verified_count || 0} 家候选已有可追溯来源`);
            } else {
                const pendingNames = Array.isArray(data.pending_names)
                    ? data.pending_names.slice(0, 3).join('、')
                    : '';
                const providerErrorNote = Number(data.provider_error_count || 0) > 0
                    ? `；其中 ${data.provider_error_count} 家本轮服务未响应`
                    : '';
                toast.warning(
                    `本轮新增核验 ${data.newly_verified || 0} 家，仍有 ${data.pending_count || 0} 家待核验${pendingNames ? `：${pendingNames}` : ''}${providerErrorNote}`,
                    { duration: 6000 }
                );
            }
        } catch (e) {
            setCompetitorMode(previousMode);
            toast.error(e instanceof Error ? e.message : '名称核验失败，请稍后重试');
            console.error('名称核验失败:', e);
        }
    };

    /**
     * [#189] 「联网找同行并核实」—— **唯一**触发联网检索的地方。
     * 🔴 原来这段逻辑长在 `switchCompetitorMode('real')` 里:点那枚 chip 会去搜。
     *    一个按钮两种含义(状态 + 动作),用户以为自己只是在切显示,结果发起了一次检索。
     *    现在拆出来,chip 只切状态。
     */
    const researchAndVerifyPeers = async () => {
        if (!selectedProject) return;
        if (competitors.length === 0) await researchCompetitors();
        else await verifyCurrentCompetitors();
    };

    /** [#189] 两个实时数(已核实 / 还在核实)。三档文案与可用性都从它派生。 */
    const peerCountsNow = useMemo(() => peerCounts(competitors), [competitors]);

    // 切换同行对比档位(#189:只切状态,**不**触发检索)
    const switchCompetitorMode = async (mode: CompetitorMode) => {
        if (!selectedProject) return;
        const previousMode = competitorMode === 'loading' ? 'evidence_only' : competitorMode;
        setCompetitorMode(mode);
        // 持久化模式到后端；“已核验”由后端证据硬门裁决，前端不能自行升级。
        try {
            const res = await authFetch(`/api/writing/competitors/${selectedProject.id}/mode`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ mode })
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) throw new Error(data.detail || '切换同行对比档位失败');
            setCompetitorMode(normalizeCompetitorMode(data.mode));
        } catch (e: any) {
            setCompetitorMode(previousMode);
            toast.error(e?.message || '切换同行对比档位失败');
            console.error("切换同行对比档位失败:", e);
        }
    };

    // 切换同行排除状态（黑名单）
    const toggleCompetitorExclude = async (name: string, excluded: boolean) => {
        if (!selectedProject) return;
        // 乐观更新UI
        setCompetitors(prev => prev.map(c => c.name === name ? { ...c, excluded } : c));
        try {
            await authFetch(`/api/writing/competitors/${selectedProject.id}/exclude`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, excluded })
            });
        } catch (e) {
            // 回滚
            setCompetitors(prev => prev.map(c => c.name === name ? { ...c, excluded: !excluded } : c));
            console.error("切换排除状态失败:", e);
        }
    };

    // 手动添加同行
    const addCompetitor = async () => {
        if (!selectedProject || !addCompetitorName.trim()) return;
        setAddingCompetitor(true);
        try {
            const res = await authFetch(`/api/writing/competitors/${selectedProject.id}/add`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: addCompetitorName.trim() })
            });
            const data = await res.json();
            if (data.status === 'success' && data.competitor) {
                setCompetitors(prev => [...prev, data.competitor]);
                if (data.mode) setCompetitorMode(normalizeCompetitorMode(data.mode));
                setAddCompetitorName('');
            } else {
                toast.error(data.detail || '添加失败');
            }
        } catch (e: any) {
            // 尝试解析JSON错误响应
            try {
                const errData = await e?.response?.json?.();
                toast.error(errData?.detail || '添加同行失败');
            } catch {
                toast.error('添加同行失败，请检查网络');
            }
        } finally {
            setAddingCompetitor(false);
        }
    };

    // 进入标题工作台
    const enterWorkbench = async (project: WritingProject) => {
        selectedProjectRef.current = project;
        setSelectedProject(project);
        setKnowledgeStatus(null);
        setKnowledgeLink("");
        resetKnowledgeDraft();
        setKnowledgeFiles([]);
        setDeliverySummary(null);
        setShowAdvancedWritingOptions(false);
        await Promise.all([loadProjectDetail(project.id), loadDeliverySummary(project.id)]);
        await Promise.all([
            loadKnowledgeStatus(project.id),
            loadKnowledgeFiles(project.brand_id),
            loadWritingBrandAssets(project.brand_id),
        ]);
        await loadCompetitors(project.id);
    };

    // 返回列表
    const backToList = () => {
        selectedProjectRef.current = null;
        projectDetailRequestRef.current += 1;
        structureGuidanceRequestRef.current += 1;
        deliverySummaryRequestRef.current += 1;
        setSelectedProject(null);
        setKeywords([]);
        setTopics([]);
        setDeliverySummary(null);
        setShowAdvancedWritingOptions(false);
        setSelectedTopics(new Set());
        setCompetitors([]);
        setCompetitorMode('evidence_only');
        setKnowledgeStatus(null);
        setKnowledgeLink("");
        resetKnowledgeDraft();
        setKnowledgeFiles([]);
        setMaterialsDrawerOpen(false);
        setBrandProfile(null);
        setBrandImageAssets([]);
        setContactForm({ contact_phone: "", contact_wechat: "", contact_website: "", contact_address: "" });
    };

    // 批量生成标题(v2.8 加 batch user_style · 用户选文体后所有标题按同一风格生成)
    // v1_3 (CTO-15.1 2026-04-19 老板反馈): 生成完要自动切"待写"tab 让用户继续操作
    /*
     * [WO_243 乙] `onlyKeywordIds` = 只给这些词出题(`per_keyword_plan` 子集)。
     * 🔴 子集里的词置 >0、其余置 0 —— 显式 0 的语义是「这个词这次不出题」,
     *    不是「缺省」。后端按 `plannable_keywords()` 只为 >0 的词收费,
     *    且有运行期铁律锁:份数 != 真正出题词数 ⇒ 503 一分不扣。
     * 🔴 不传就是整表(旧行为逐字不变)。
     * 🔴 [Review 09-28 · 新词出题 400] 每项只发**槽数** `slots`(= 合同授权槽 `required_articles`),槽 -> 条只在后端换算
     *    (与详情的 `planned_posts_default` 同一个换算器);前端不换算。
     *    键名必须是后端读的那个:09-19 起这里发 `planned_count`、后端读 `count` ⇒ 每次 400,新词出不了题。
     *    0 槽合法(覆盖词)⇒ 0 条;只在缺值时兜底成 0,不许用 `|| 1` 把显式 0 吃成 1。
     */
    const generateTitles = async (onlyKeywordIds?: number[]) => {
        if (!selectedProject) return;
        const perKeywordPlan = onlyKeywordIds && onlyKeywordIds.length > 0
            ? keywords.map((kw) => ({
                confirmed_keyword_id: kw.id,
                slots: onlyKeywordIds.includes(kw.id) ? (kw.required_articles ?? 0) : 0,
            }))
            : null;
        const titleFingerprint = JSON.stringify({
            quoteId: selectedProject.id,
            topicState: topics.map(topic => [topic.id, topic.status, topic.article_id || 0]).sort(),
            userStyle: batchUserStyle,
            distribution: pendingDistribution,
            // [Review 09-28] 子集也进指纹:不同词的「立即生成标题」在同一局面下不许共用一个请求编号
            //   (受理凭据按编号幂等,共用编号的第二次会拿到第一次那几个词的凭据)。
            onlyKeywordIds: onlyKeywordIds && onlyKeywordIds.length > 0 ? [...onlyKeywordIds].sort((a, b) => a - b) : null,
        });
        if (titleRequestRef.current?.fingerprint !== titleFingerprint) {
            titleRequestRef.current = {
                fingerprint: titleFingerprint,
                requestId: `writing-titles-${safeRandomUUID()}`,
            };
        }
        setGenerating(true);
        try {
            const res = await authFetch("/api/writing/generate-titles", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Request-ID": titleRequestRef.current.requestId,
                },
                // v2.8:batchUserStyle = 'auto' 时不传 user_style(后端按默认 ratio 抽)
                // v2.10.2 P0-1:pendingDistribution 存在时优先(用户在高级弹窗设过自定义配比)
                //   后端按 distribution 算 style_plan + 写 source='batch_distribution'
                body: JSON.stringify({
                    quote_id: selectedProject.id,
                    user_style: batchUserStyle === 'auto' ? null : batchUserStyle,
                    user_choice_distribution: pendingDistribution,
                    ...(perKeywordPlan ? { per_keyword_plan: perKeywordPlan } : {}),
                })
            });
            const data = await res.json();
            if (data.success && Number(data.topic_count || 0) > 0) {
                titleRequestRef.current = null;
                await loadProjectDetail(selectedProject.id);
                await loadProjects(activeTab);
                // 切到"待写"tab 让用户看到生成出的标题，可以继续勾选开始写作
                setWorkbenchTab('pending');
                // v2.10.2 P0-1:pendingDistribution 已应用 · 清除(防下次生成误用)
                setPendingDistribution(null);
                toast.success('标题已生成，勾选需要的标题开始写文章');
                // 沙盒: 不自动展开 · 让用户点关键词行手动展开 (有 spotlight 引导)
                if (isSandboxActive()) {
                    setTutorialStage('step3-expand-titles');
                }
            } else {
                await loadProjectDetail(selectedProject.id);
                await loadProjects(activeTab);
                // [WO_317 第六笔] 非 2xx 的回包是 {detail: 串 | {code, message}}:原来只读 data.message,
                //   409「同一请求正在处理或已处理」、400「没有需要出题的关键词」都被显示成下面这句兜底。
                const detailMessage = typeof data?.detail === 'string' ? data.detail : data?.detail?.message;
                toast.warning(data.message || detailMessage || '本次没有生成可用标题，请稍后重试');
            }
        } catch (e) {
            console.error("生成标题失败:", e);
            toast.error('标题生成失败，请稍后重试');
        } finally {
            setGenerating(false);
        }
    };

    /*
     * 🔴 [WO_243 乙 2026-09-19] `generateTopicsForMissingKws` **已退役**。
     *
     *    它做的事是:把「没有选题行的词」(= **新词**)逐个发给
     *    `POST .../keywords/{id}/generate-topic`(**补救端点**)。
     *    WO_241 丙 之后那个端点有了补救闸:`generation_request_id` 为空即
     *    `403 TITLE_RECOVERY_NOT_AUTHORIZED`(`title_batch_charge_registry.py:78`),
     *    而新词**不属于任何已付批次**,给了也过不了。⇒ 这条路径上线即全灭。
     *
     *    契约里那句「前端不要把补救那一面当成"免费的生成标题"去覆盖新词场景」
     *    描述的**就是这个函数** —— 它读起来像提醒,其实是对现存代码的描述。
     *
     *    新词现在走批量端点带 `per_keyword_plan` 子集(收费,见三元按钮)。
     *    🔴 顺带一个它自己的老毛病,一并带走:那个循环把每次失败
     *       `console.error` 吞掉,末尾无条件 `toast.success('已为 N 个新关键词生成标题')`
     *       —— **一条题都没出,用户看到的是成功**。
     */
    // [CTO-15.23 2026-05-08 死锁修] 给单个 keyword 立即生成 topic
    // 场景:b7e61a01 后台 worker 失败 / 用户加 kw 后等不及 30 秒
    // v2.9:加 user_style body · 与批量 generate-titles 同款文体强制
    const generateTopicForKeyword = async (kwId: number, keyword: string) => {
        if (!selectedProject) return;
        if (generatingForKwIds.has(kwId)) return;
        /*
         * [WO_243 乙] 🔴 **先定这个词走哪一面**,再决定打哪个端点:
         *   · 没有选题行 = 新词 = 从没付过 ⇒ **不能**走补救端点(会 403),
         *     改走批量端点带只含它一个的子集(收费);
         *   · 有行 = 那一批付过 ⇒ 免费补救,**必须带那一行的 `generation_request_id`**。
         * 判据只有一处(`faceForKeyword`),不在这里再写一遍。
         */
        const { face, generationRequestId } = faceForKeyword(kwId, topics);
        if (face === 'new-keywords') {
            await generateTitles([kwId]);
            return;
        }
        setGeneratingForKwIds(prev => new Set(prev).add(kwId));
        try {
            const res = await authFetch(`/api/writing/projects/${selectedProject.id}/keywords/${kwId}/generate-topic`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    user_style: batchUserStyle === 'auto' ? null : batchUserStyle,
                    /* 钱的标识不下发前端也不回传;前端只带这个,服务端自己解到那一笔 charge。 */
                    generation_request_id: generationRequestId,
                }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok || data?.success === false) {
                /*
                 * 🔴 **按 code 分支,不按文案**(契约逐字):补救闸对所有拒绝理由
                 *    一律同一个 code + 同一句话,是为了不让人靠"拒绝理由"探别人的批次。
                 *    按文案判等于把一句会改的话当协议用。
                 */
                const code = data?.detail?.code || data?.code;
                if (res.status === 403 && code === 'TITLE_RECOVERY_NOT_AUTHORIZED') {
                    toast.error(data?.detail?.message || data?.message
                        || '这个词不满足免费补出标题的条件 · 可用「批量生成标题」重新出题(会计费)');
                    return;
                }
                const detail = data?.detail?.message || data?.detail || data?.message || "生成失败,请稍后重试";
                toast.error(typeof detail === 'string' ? detail : "生成失败,请稍后重试");
                return;
            }
            await loadProjectDetail(selectedProject.id);
            toast.success(data?.message || `已为 "${keyword}" 生成 ${data?.added || 0} 个标题`);
        } catch (e) {
            console.error("立即生成失败:", e);
            toast.error("立即生成失败,请稍后重试");
        } finally {
            setGeneratingForKwIds(prev => {
                const next = new Set(prev);
                next.delete(kwId);
                return next;
            });
        }
    };

    // 编辑标题
    const startEdit = (topic: Topic) => {
        setEditingTopicId(topic.id);
        setEditingTitle(topic.optimized_title);
    };

    const saveEdit = async () => {
        if (!editingTopicId) return;
        try {
            await authFetch(`/api/writing/topics/${editingTopicId}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ new_title: editingTitle })
            });
            setTopics(prev => prev.map(t =>
                t.id === editingTopicId ? { ...t, optimized_title: editingTitle } : t
            ));
        } catch (e) {
            console.error("保存失败:", e);
        }
        setEditingTopicId(null);
    };

    const cancelEdit = () => {
        setEditingTopicId(null);
        setEditingTitle("");
    };

    // 批量重新生成
    const regenerateSelected = async () => {
        if (selectedTopics.size === 0) return;
        setRegenerating(true);
        try {
            const res = await authFetch("/api/writing/regenerate-titles", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ topic_ids: Array.from(selectedTopics) })
            });
            const data = await res.json();
            setSelectedTopics(new Set());
            if (selectedProject) {
                await loadProjectDetail(selectedProject.id);
            }
            toast.success(data.message || "标题重新生成完成");
        } catch (e) {
            console.error("重新生成失败:", e);
            toast.error("重新生成失败，请重试");
        } finally {
            setRegenerating(false);
        }
    };

    // [CTO 2026-05-21] 选中项里"真正可写作"的（待写/草稿/标题就绪/失败）
    // 与后端 /api/writing/start-articles 的 status 过滤对齐（server.py: status IN draft/pending/titles_ready/failed）
    // 优化 tab 把"待写"和"已完成"放一个列表共用同一勾选集合：已完成项要留给"去发布"勾选，
    // 但绝不能计入"开始写作"，否则数量/扣费提示虚高、且可能误触发写作。
    // [T5 P0 2026-07-28] 再加一道:**没有标题的行不可写作**。
    // 标题受理凭据(pending,标题还没生成出来)和标题阶段失败行都落在上面这个
    // status 白名单里,但它们没有 optimized_title —— 拿去写作只会生成一篇无题稿
    // 并白扣一次算力。后端 status 过滤看不出这一层,前端在勾选口径上直接挡掉。
    const selectedWritableIds = topics
        .filter(t => selectedTopics.has(t.id) && (!t.status || ['draft', 'pending', 'titles_ready', 'failed'].includes(t.status)))
        .filter(t => String(t.optimized_title || '').trim().length > 0)
        .map(t => t.id);
    const selectedWritableCount = selectedWritableIds.length;

    // 开始写作(选中的标题)
    // v1_3 (CTO-15.1 2026-04-19): 按钮已标价，小额直接扣；>=1000 分走 toast 5s 反悔窗口
    const startWriting = async (retryTopicIds?: number[]) => {
        if (!selectedProject) return;
        const writableIds = retryTopicIds?.length ? retryTopicIds : selectedWritableIds;
        const writableCount = writableIds.length;
        if (writableCount === 0) {
            if (selectedTopics.size > 0) toast("勾选的都是已完成或进行中的文章，没有可开始写作的选题");
            return;
        }
        if (!selectedProject.brand_id) {
            toast.error("缺少客户ID，无法确认写作资料");
            return;
        }
        if (knowledgeLoading) {
            toast("正在检查客户知识库，请稍后再开始写作");
            return;
        }
        const status = knowledgeStatus?.quote_id === selectedProject.id
            ? knowledgeStatus
            : await loadKnowledgeStatus(selectedProject.id);
        // 沙盒态跳过知识库检查 (沙盒不依赖真实知识库向量索引 · 模拟上传后可能被重 fetch 清空致误报)
        if (!isSandboxActive()) {
            if (!status?.can_start_writing) {
                toast.error(status?.message || "客户知识库为空，请先补充客户资料");
                return;
            }
        }
        // [P0-16 2026-07-12] 价目未加载 → 禁止开始(不默认 0 造成免费假象)· 后端才是扣费权威
        if (articleGenCost == null) {
            toast.error("价目加载中,暂时不能开始写作 · 请稍候重试");
            return;
        }
        if (!isAdmin && walletStatus !== 'ready') {
            toast.error('暂时无法确认当前功能可用算力,请先在钱包重新读取余额;本次没有开始写作，也没有扣钱');
            return;
        }
        const totalCost = articleGenCost * writableCount;
        // [写作卡死根治 2026-06-08] 余额预检(治根因 A · 防 user98 盲目提交 97 篇致卡死)
        // 前端拦截 + 后端总量预检 402 双保险 · admin 豁免 · 余额不足提示"只够 X 篇"引导充值/减选
        if (!isAdmin && totalPoints < totalCost) {
            const affordable = Math.floor(totalPoints / Math.max(1, articleGenCost));
            toast.error(`余额只够写 ${affordable} 篇(本次需 ${totalCost.toLocaleString()} 算力 · 余额 ${totalPoints.toLocaleString()})· 请充值或减少选择`);
            return;
        }
        // [2026-06-08] confirmLarge(阈值 2000)· 大额/批量 5s 反悔窗口 · label 带金额让用户看见
        confirmLarge(
            totalCost,
            () => { void doStartWriting(writableIds); },
            `将扣 ${totalCost.toLocaleString()} 算力写 ${writableCount} 篇文章`,
        );
    };
    const doStartWriting = async (retryTopicIds?: number[]) => {
        const selectedIds = retryTopicIds?.length ? retryTopicIds : selectedWritableIds;
        if (selectedIds.length === 0 || !selectedProject) return;
        const projectId = selectedProject.id;
        const writableIds = selectedIds;  // 快照本次"可写作"选中项，已完成项不在其中
        const articleFingerprint = JSON.stringify({
            projectId,
            topicIds: [...writableIds].sort((left, right) => left - right),
            llm: activeLLM ? [activeLLM.provider, activeLLM.model] : null,
            extraInstruction: (extraInstruction || '').slice(0, 200),
            addImages,
            addContact,
            publicationProfile,
            useStructureGuidance,
        });
        if (articleRequestRef.current?.fingerprint !== articleFingerprint) {
            articleRequestRef.current = {
                fingerprint: articleFingerprint,
                requestId: `writing-articles-${safeRandomUUID()}`,
            };
        }
        setWritingInProgress(true);
        try {
            // v2.7.2:user_choice 映射 + extra_instruction 传到后端(后端读取走 resolve_user_choice + sanitize)
            // [Owner 2026-08-09] 默认值 = 选题页展示的体裁标签。旧写法只在
            // `t.user_choice` 非 auto 时才塞,于是"展示了标签但没手动选过"的选题
            // 请求体里根本没有它 → 后端按行业权重抽签 → 展示 A 生成 B。
            const userChoicesMap = buildUserChoicesMap(topics, writableIds);
            const res = await authFetch("/api/writing/start-articles", {
                method: "POST",
                headers: {
                    "Content-Type": "application/json",
                    "X-Request-ID": articleRequestRef.current.requestId,
                },
                body: JSON.stringify({
                    quote_id: projectId,
                    topic_ids: writableIds,
                    ...(activeLLM ? { llm_provider: activeLLM.provider, llm_model: activeLLM.model } : {}),
                    // v2.7.2 GEO 文体改造
                    user_choices: userChoicesMap,
                    extra_instruction: (extraInstruction || '').slice(0, 200),
                    // [2026-06-02 GEO CTO] 写作大厅两开关
                    add_images: addImages,
                    add_contact: addContact,
                    publication_profile: publicationProfile,
                    use_structure_guidance: useStructureGuidance,
                })
            });
            const data = await res.json().catch(() => ({}));
            // 修 silent-fail BUG (2026-04-28):4xx/5xx 后端返 {detail} 没 success · 必须显示
            if (!res.ok) {
                const detail = apiErrorMessage(data, `服务器错误 (${res.status})`);
                toast.error(`开始写作失败:${detail}`);
                if (detail.includes('知识库') || detail.includes('客户资料')) {
                    await loadKnowledgeStatus(projectId);
                }
                return;
            }
            if (data.success) {
                // Keep the key until the background terminal state. A safe
                // retry after refund reuses the same customer intent key.
                // 更新本地状态（只把"可写作"的选中项标记为写作中，已完成项保持不动）
                const writableSet = new Set(writableIds);
                setTopics(prev => prev.map(t =>
                    writableSet.has(t.id) ? { ...t, status: 'writing' } : t
                ));
                setSelectedTopics(new Set());
                // [Bug5修复] 切换到"写作中"Tab，防止项目从pending列表消失导致UI回退
                // 优化tab有自己的写作中分组，不需要跳
                if (workbenchTab !== 'optimize') {
                    setWorkbenchTab('writing');
                }
                // 启动强制轮询（useEffect会自动接管）
                startForcePolling();
                toast.success(data.message || `已开始生成 ${writableIds.length} 篇文章`);
                // 真实代理: 点开始写作就算 step 完成 (老逻辑保留, 写作期间小球能跳到 4/4)
                // 沙盒: 不在这里标 · 由 PublishCenter 一键发布走完后才算真完成 (跟用户教学预期对齐)
                if (!isSandboxActive()) {
                    markStep('first_article_publish');
                }
            } else {
                // 200 但 data.success=false (虽然后端目前不会这样返,兜底)
                toast.error(data?.detail || data?.message || '开始写作失败,请稍后重试');
            }
        } catch (e) {
            console.error("开始写作失败:", e);
            const msg = (e as Error)?.message || '网络异常';
            toast.error(`开始写作失败:${msg},请稍后重试`);
        } finally {
            setWritingInProgress(false);
        }
    };

    // ===== 自动轮询写作进度 =====
    // 只要有 writing 状态的 topic，就每3秒拉一次进度；全部完成自动停止
    const prevWritingCountRef = useRef<number>(0);
    const prevCompletedCountRef = useRef<number>(0);
    const hasWritingTopics = topics.some(t => t.status === 'writing');
    // 用于"刚发起写作"时强制轮询几轮，防止后台还没开始就误判结束
    const forcePollingRef = useRef(0);
    const [pollingActive, setPollingActive] = useState(false);
    // 防止"全部完成"弹窗重复弹出
    const completionAlertShownRef = useRef(false);
    const failureToastRequestRef = useRef<string | null>(null);

    const fetchWritingProgress = useCallback(async () => {
        const project = selectedProjectRef.current;
        if (!project) return;
        try {
            const res = await authFetch(`/api/writing/progress/${project.id}`);
            const data = await res.json();
            if (selectedProjectRef.current?.id !== project.id) return;
            if (!data.success) return;

            const currentWriting = data.writing || 0;
            const currentCompleted = data.completed || 0;
            const failedList = (data.failed_list || []) as Topic[];

            // 检测生成失败：之前有 writing，现在没了，但 completed 没增加
            if (prevWritingCountRef.current > 0 && currentWriting === 0
                && currentCompleted <= prevCompletedCountRef.current
                && forcePollingRef.current <= 0) {
                const failure = failedList[0];
                const failureKey = failure?.generation_request_id || `${project.id}:legacy`;
                if (failureToastRequestRef.current !== failureKey) {
                    failureToastRequestRef.current = failureKey;
                    toast.error(
                        `${failure?.generation_error_code || 'ARTICLE_GENERATION_FAILED'} · `
                        + `${failure?.generation_error_message || '文章生成未完成。'} · `
                        + `${failure?.generation_refund_message || '请刷新退款状态后安全重试。'}`,
                    );
                }
            }

            prevWritingCountRef.current = currentWriting;
            prevCompletedCountRef.current = currentCompleted;

            setWritingProgress(data);
            const completedIds = new Set(data.completed_list?.map((t: Topic) => t.id) || []);
            const writingIds = new Set(data.writing_list?.map((t: Topic) => t.id) || []);
            const failedById = new Map<number, Topic>(failedList.map(item => [item.id, item]));
            setTopics(prev => prev.map(t => ({
                ...t,
                status: completedIds.has(t.id) ? 'completed'
                    : writingIds.has(t.id) ? 'writing'
                        : failedById.has(t.id) ? 'failed'
                            : t.status,
                article_id: data.completed_list?.find((ct: Topic) => ct.id === t.id)?.article_id || t.article_id,
                article_version: (data.completed_list?.find((ct: any) => ct.id === t.id)?.version) || t.article_version,
                ...(failedById.get(t.id) || {}),
            })));

            // 全部完成：自动切换到已完成Tab（只弹一次）
            if (currentWriting === 0 && forcePollingRef.current <= 0 && currentCompleted > 0
                && !completionAlertShownRef.current) {
                completionAlertShownRef.current = true;
                articleRequestRef.current = null;
                await loadProjectDetail(project.id);
                setWorkbenchTab('completed');
                toast.success("文章写作全部完成！已自动切换到「已完成」标签页。");
            }

            if (forcePollingRef.current > 0) {
                forcePollingRef.current--;
                if (forcePollingRef.current <= 0 && currentWriting === 0) {
                    setPollingActive(false);
                }
            }
        } catch (e) {
            console.error("获取进度失败:", e);
        }
    }, []);

    // 自动轮询 effect：有 writing topic 或主动触发 pollingActive 时每3秒刷新
    useEffect(() => {
        if (!hasWritingTopics && !pollingActive) return;
        fetchWritingProgress(); // 立即拉一次
        const interval = setInterval(fetchWritingProgress, 3000);
        return () => clearInterval(interval);
    }, [hasWritingTopics, pollingActive, fetchWritingProgress]);

    // startWriting 中触发强制轮询
    const startForcePolling = () => {
        prevWritingCountRef.current = selectedTopics.size;
        prevCompletedCountRef.current = topics.filter(t => t.status === 'completed').length;
        forcePollingRef.current = 5;
        completionAlertShownRef.current = false; // 重置完成弹窗标记
        setPollingActive(true); // 触发 useEffect
    };

    // 预览文章 (需要topicId用于重写)
    const previewArticle = async (articleId: number, topicId: number) => {
        // [2026-05-27] 预览前显式暂停移动端教程遮罩 · 防黑罩挡预览内容
        //   500ms 兜底 · 若 fetch 失败 setPreviewData 没调 · 自动 resume 防永久消失
        pauseMobileCoach();
        const safetyTimer = window.setTimeout(() => {
            // 500ms 后仍没 setPreviewData(没进 success 分支)· 兜底 resume
            // 若已成功 setPreviewData · 关闭预览时另有 resume 调用 · 这里 resume 是 no-op
            resumeMobileCoach();
        }, 500);
        try {
            // ✅ 点击预览时，标记该topic为已审核，消除NEW标签
            authFetch(`/api/topics/${topicId}/mark-reviewed`, { method: 'POST' })
                .then(() => {
                    // 更新本地状态，消除NEW标签
                    setTopics(prev => prev.map(t =>
                        t.id === topicId ? { ...t, reviewed_at: new Date().toISOString() } : t
                    ));
                });

            const res = await authFetch(`/api/articles/${articleId}`);
            const data = await res.json();
            if (data.success && data.article) {
                window.clearTimeout(safetyTimer);
                setPreviewData({
                    id: articleId,
                    topicId: topicId,
                    title: data.article.title || '无标题',
                    content: data.article.content || '',
                    lengthGuidance: data.article.length_guidance || undefined,
                    isEditing: false,
                    isRewriting: false
                });
                setEditContent(data.article.content || '');
                setRevisionNote("");
                setReferenceArticle("");
                // Always use the article-level preview snapshot. The current
                // page checkbox configures the next batch; it must not change
                // a finished article's recorded contact consent.
                authFetch(`/api/brand-images/article-preview/${articleId}`)
                    .then(r => r.json())
                    .then(rd => {
                        if (rd && rd.success) {
                            setPreviewData(prev => (prev && prev.id === articleId) ? {
                                ...prev,
                                contentRendered: rd.content,
                                images: rd.images || [],
                                brandId: rd.brand_id,
                                contactConsent: rd.contact_consent,
                                lengthGuidance: rd.length_guidance || undefined,
                                imageRender: rd.image_render || undefined,
                            } : prev);
                        }
                    })
                    .catch(() => {});
            } else {
                window.clearTimeout(safetyTimer);
                resumeMobileCoach();
            }
        } catch (e) {
            window.clearTimeout(safetyTimer);
            resumeMobileCoach();
            console.error("预览失败:", e);
        }
    };

    // 保存编辑的文章
    const saveArticle = async () => {
        if (!previewData) return;
        try {
            const res = await authFetch(`/api/articles/${previewData.id}`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: editContent })
            });
            const data = await res.json();
            if (res.ok && data.success) {
                const canonicalContent = data?.article?.content ?? '';
                setPreviewData({
                    ...previewData,
                    content: canonicalContent,
                    contentRendered: undefined,
                    images: undefined,
                    contactConsent: data.contact_consent,
                    lengthGuidance: data?.article?.length_guidance || previewData.lengthGuidance,
                    isEditing: false,
                });
                setEditContent(canonicalContent);
                // Re-read the article-level snapshot after the server has
                // sanitized and persisted the canonical body. Never keep a
                // pre-save rendered copy that may contain stale contact data.
                authFetch(`/api/brand-images/article-preview/${previewData.id}`)
                    .then(r => r.json())
                    .then(rd => {
                        if (rd && rd.success) {
                            setPreviewData(prev => (prev && prev.id === previewData.id) ? {
                                ...prev,
                                content: rd.content_raw ?? canonicalContent,
                                contentRendered: rd.content,
                                images: rd.images || [],
                                brandId: rd.brand_id,
                                contactConsent: rd.contact_consent,
                                lengthGuidance: rd.length_guidance || undefined,
                                imageRender: rd.image_render || undefined,
                            } : prev);
                        }
                    })
                    .catch(() => {
                        // Canonical sanitized content remains visible; the old
                        // rendered body was already cleared above.
                    });
                // 刷新topics列表
                if (selectedProject) {
                    await loadProjectDetail(selectedProject.id);
                }
            } else {
                toast.error(data?.detail || '保存失败');
            }
        } catch (e) {
            console.error("保存失败:", e);
        }
    };

    // [2026-06-02] 配图「不使用 / 更换」· content 改写在后端(RBAC 校验新图属该客户且可外发)
    const handleImageAction = async (index: number, action: 'remove' | 'replace', newAssetId?: number) => {
        if (!previewData) return;
        try {
            const res = await authFetch(`/api/brand-images/article/${previewData.id}/image-action`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action, placeholder_index: index, new_asset_id: newAssetId })
            });
            const rd = await res.json();
            if (rd && rd.success) {
                setPreviewData(prev => prev ? {
                    ...prev,
                    content: rd.content,
                    contentRendered: rd.content_rendered,
                    images: rd.images || [],
                    lengthGuidance: rd.length_guidance || prev.lengthGuidance,
                } : prev);
                setEditContent(rd.content);
                setReplaceImageIdx(null);
                toast.success(action === 'remove' ? '已移除该配图' : '已更换配图');
            } else {
                toast.error((rd && rd.detail) || '操作失败');
            }
        } catch {
            toast.error('操作失败');
        }
    };

    // 打开「更换」选图弹窗(只列该客户已确认可外发的图)
    const openReplaceGallery = async (index: number) => {
        if (!previewData?.brandId) { toast.error('无法识别客户,无法换图'); return; }
        try {
            const res = await authFetch(`/api/brand-images/list/${previewData.brandId}`);
            const rd = await res.json();
            setGalleryAssets((rd.assets || []).filter((a: any) => a.publish_allowed === 1 && a.rights_confirmed === 1));
            setReplaceImageIdx(index);
        } catch {
            toast.error('加载图库失败');
        }
    };

    // 打开重写面板（直接进入重写模式）
    const openRewrite = async (articleId: number, topicId: number) => {
        try {
            const res = await authFetch(`/api/articles/${articleId}`);
            if (!res.ok) {
                toast.error(`加载文章失败 (${res.status})`);
                return;
            }
            const data = await res.json();
            if (data.success && data.article) {
                const isCurrentlyRewriting = rewritingTopicIds.has(topicId);
                setRewriteLoading(isCurrentlyRewriting);
                setPreviewData({
                    id: articleId,
                    topicId: topicId,
                    title: data.article.title || '无标题',
                    content: data.article.content || '',
                    lengthGuidance: data.article.length_guidance || undefined,
                    isEditing: false,
                    isRewriting: !isCurrentlyRewriting // 正在重写中则不显示重写表单，显示loading
                });
                setEditContent(data.article.content || '');
                setRevisionNote("");
                setReferenceArticle("");
            } else {
                toast.error("加载文章失败: " + (data.detail || data.message || "未知错误"));
            }
        } catch (e: any) {
            console.error("加载文章失败:", e);
            toast.error("加载文章失败: " + (e?.message || "网络错误"));
        }
    };

    // 重写文章（后台执行，关闭弹窗不中断）
    const rewriteArticle = () => {
        if (!previewData) return;
        const topicId = previewData.topicId;
        const refArticle = referenceArticle || null;
        const revNote = revisionNote || null;

        setRewriteLoading(true);
        setRewritingTopicIds(prev => new Set([...prev, topicId]));

        // 后台发请求，不 await
        authFetch(`/api/writing/rewrite/${topicId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ reference_article: refArticle, revision_note: revNote })
        }).then(async res => {
            if (!res.ok) {
                const errText = await res.text();
                toast.error(`重写失败 (${res.status}): ${errText}`);
                return;
            }
            const data = await res.json();
            if (data.success && data.article) {
                // 如果弹窗还开着且是同一篇文章，更新内容
                setPreviewData(prev => {
                    if (prev && prev.topicId === topicId) {
                        return {
                            ...prev,
                            id: data.article.id,
                            content: data.article.content,
                            contentRendered: undefined,
                            images: undefined,
                            contactConsent: undefined,
                            lengthGuidance: data.article.length_guidance || undefined,
                            isRewriting: false,
                        };
                    }
                    return prev;
                });
                authFetch(`/api/brand-images/article-preview/${data.article.id}`)
                    .then(r => r.json())
                    .then(rd => {
                        if (rd && rd.success) {
                            setPreviewData(prev => (prev && prev.topicId === topicId) ? {
                                ...prev,
                                id: data.article.id,
                                contentRendered: rd.content,
                                images: rd.images || [],
                                brandId: rd.brand_id,
                                contactConsent: rd.contact_consent,
                                lengthGuidance: rd.length_guidance || undefined,
                                imageRender: rd.image_render || undefined,
                            } : prev);
                        }
                    })
                    .catch(() => {});
                setEditContent(prev => prev); // 触发更新
                toast.success('重写完成');
                if (selectedProject) {
                    await loadProjectDetail(selectedProject.id);
                }
            } else {
                toast.error("重写失败: " + (data.detail || data.message || "未知错误"));
            }
        }).catch((e: any) => {
            toast.error("重写失败: " + (e?.message || "网络错误"));
        }).finally(() => {
            setRewriteLoading(false);
            setRewritingTopicIds(prev => {
                const next = new Set(prev);
                next.delete(topicId);
                return next;
            });
        });
    };

    // [P2-D 2026-07-31] 这条链路**是整篇重写,是计费的**,不是"修复"。
    // 改前它叫 repairEvidenceAdvisory、toast 写"AI 局部修复"、按钮挂在"修复"名下,
    // 打的却是 /api/writing/rewrite —— 用户以为在改一处瑕疵,实际付钱重写了整篇。
    // 🔴 免费修复走的是 /api/articles/{id}/repair-finding 与 /auto-repair
    //    (两端点函数体内 deduct_points 出现 0 次,返回体自带 "charged": False),
    //    与这条计费链路在 UI 上完全分开:免费的在「待修问题」,计费的在「概览」
    //    单独一块并明写"整篇重写(计费)"。
    const rewriteWholeArticleForAdvisory = (topic: Topic) => {
        if (articleRewriteCost == null) {
            toast.error("价目加载中，暂时不能整篇重写");
            return;
        }
        confirmLarge(
            articleRewriteCost,
            () => {
                setRewritingTopicIds(prev => new Set([...prev, topic.id]));
                authFetch(`/api/writing/rewrite/${topic.id}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ advisory_repair: true }),
                }).then(async response => {
                    const payload = await response.json().catch(() => ({}));
                    if (!response.ok || !payload.success) {
                        toast.error(apiErrorMessage(payload, '整篇重写失败，原稿未覆盖'));
                        return;
                    }
                    toast.success('已整篇重写出新版本，原版本仍保留');
                    if (selectedProject) await loadProjectDetail(selectedProject.id);
                }).catch(() => {
                    toast.error('整篇重写失败，原稿未覆盖');
                }).finally(() => {
                    setRewritingTopicIds(prev => {
                        const next = new Set(prev);
                        next.delete(topic.id);
                        return next;
                    });
                });
            },
            '整篇重新生成（保留标题与商业方向），按重写计费',
        );
    };

    const continueEvidenceAdvisory = async (topic: Topic) => {
        try {
            const response = await authFetch(
                `/api/topics/${topic.id}/mark-reviewed?evidence_advisory_continue=true`,
                { method: 'POST' },
            );
            const payload = await response.json().catch(() => ({}));
            if (!response.ok || !payload.success) {
                toast.error(apiErrorMessage(payload, '人工确认未保存'));
                return;
            }
            // [P2-A 2026-07-31] 🔴 **不再跳转**。改前确认一篇就 navigate 去 /publish,
            // 用户的展开位置 / 筛选 / 滚动 / 已勾选全部丢失 —— 一次要处理十几篇的人
            // 每确认一篇就得重新找回现场。现在留在原页,只刷新数据;要发布由用户点
            // 这一行的「去投放」按钮(它读的是刷新后的 publication_eligible)。
            //
            // 🔴 判断只认**本次响应的 payload**:改前那个 `|| topic.publication_eligible`
            // 读的是闭包里上一次 loadProjectDetail 的旧值,而"确认"这个动作本身就会
            // 改变它 —— 用旧值判新状态,必然有一类样本判错。
            const acknowledged = payload.article_human_review_status === 'approved'
                || payload.evidence_advisory_acknowledged === true;
            toast.success(
                payload.idempotent_replay
                    ? '这条之前已经记录过了'
                    : acknowledged
                        ? '已记录,可以继续;要发布点这一行的「去投放」'
                        : '已保存',
            );
            if (selectedProject) await loadProjectDetail(selectedProject.id);
        } catch {
            toast.error('人工确认未保存');
        }
    };


    // [P3a 批量审核 2026-08-01 · 工单 §6] 主入口「审核与修复 (N)」的开关。
    const [batchReviewPanelOpen, setBatchReviewPanelOpen] = useState(false);

    // 批量重写选中的已完成文章（后台执行，不阻塞 UI）
    const [batchRewriting, setBatchRewriting] = useState(false);
    const [rewritingTopicIds, setRewritingTopicIds] = useState<Set<number>>(new Set());
    // v1_3 (CTO-15.1 2026-04-19): 按钮已标价，去 window.confirm；>=1000 分走 toast 5s 反悔
    const batchRewriteSelected = () => {
        const completedSelected = topics.filter(
            t => t.status === 'completed' && t.article_id && selectedTopics.has(t.id)
        );
        if (completedSelected.length === 0) {
            toast("请先勾选要重写的文章");
            return;
        }
        const ids = completedSelected.map(t => t.id);
        // [P0-16 2026-07-12] 价目未加载 → 禁止重写(不默认 0 造成免费假象)· 后端才是扣费权威
        if (articleRewriteCost == null) {
            toast.error("价目加载中,暂时不能重写 · 请稍候重试");
            return;
        }
        // [2026-06-03 全站静默扣费] confirmLarge 仍保留(>1000 额度大额误触保护)· label 去金额改纯动作
        const totalCost = articleRewriteCost * ids.length;
        confirmLarge(
            totalCost,
            () => { void doBatchRewrite(ids); },
            `将重写 ${ids.length} 篇文章（覆盖原文）`,
        );
    };
    const waitForBatchRewrite = async (batchRef: string) => {
        const delay = (ms: number) => new Promise(resolve => window.setTimeout(resolve, ms));
        let lastStatus = "queued";

        for (let attempt = 0; attempt < 180; attempt += 1) {
            await delay(attempt < 5 ? 2000 : 5000);
            const resp = await authFetch(`/api/writing/batch-rewrite/${batchRef}`);
            if (!resp.ok) {
                if (resp.status === 404) break;
                continue;
            }
            const data = await resp.json();
            lastStatus = data.status || lastStatus;

            if (data.status === "completed") {
                if (selectedProject) {
                    await loadProjectDetail(selectedProject.id);
                }
                toast.success(`批量重写完成: ${data.rewritten ?? data.success ?? 0} 篇成功, ${data.failed ?? 0} 篇失败`);
                return data;
            }

            if (data.status === "failed") {
                if (selectedProject) {
                    await loadProjectDetail(selectedProject.id);
                }
                toast.error(data.message || "批量重写失败，本次未收费");
                return data;
            }

            if (data.status === "billing_failed") {
                if (selectedProject) {
                    await loadProjectDetail(selectedProject.id);
                }
                toast.error("文章已重写，但扣费记录异常，请联系管理员核对");
                return data;
            }
        }

        toast("已提交·后台生成中·稍候刷新查看结果");
        return { status: lastStatus };
    };

    const doBatchRewrite = async (ids: number[]) => {
        setRewritingTopicIds(prev => new Set([...prev, ...ids]));
        setBatchRewriting(true);
        toast(`已提交 ${ids.length} 篇文章，后台生成中，可以继续其他操作...`);

        try {
            const resp = await authFetch('/api/writing/batch-rewrite', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic_ids: ids })
            });
            if (!resp.ok) {
                const errText = await resp.text();
                toast.error(`重写未提交 (${resp.status}): ${errText}`);
                return;
            }
            const data = await resp.json();

            if (data.batch_ref) {
                if (data.idempotent_hit) {
                    toast("这批文章已提交过，后台生成中或已完成；不会重复扣费");
                } else {
                    toast("已提交·后台生成中·稍候自动刷新结果");
                }
                await waitForBatchRewrite(data.batch_ref);
                return;
            }

            if (selectedProject) {
                await loadProjectDetail(selectedProject.id);
            }
            toast.success(`批量重写完成: ${data.success ?? data.rewritten ?? 0} 篇成功, ${data.failed ?? 0} 篇失败`);
        } catch (e: any) {
            console.warn("批量重写提交或轮询中断:", e);
            toast("已提交·后台生成中·稍候刷新查看结果");
        } finally {
            setBatchRewriting(false);
            setRewritingTopicIds(prev => {
                const next = new Set(prev);
                ids.forEach(id => next.delete(id));
                return next;
            });
        }
    };

    // 将已完成文章重置为待写
    const [resettingToPending, setResettingToPending] = useState(false);
    const resetToPending = async (topicIds: number[]) => {
        if (topicIds.length === 0) {
            toast("请先勾选要放回待写的文章");
            return;
        }
        const label = topicIds.length === 1 ? '这篇文章' : `选中的 ${topicIds.length} 篇文章`;
        if (!(await askConfirm({
            title: `确定要将${label}放回待写吗？`,
            description: '当前标题和正文会被废弃，但旧稿会保留为不可变历史修订。返回后需重新生成标题，再生成完整正文。',
        }))) return;

        const fingerprint = JSON.stringify({
            projectId: selectedProject?.id,
            topicIds: [...topicIds].sort((left, right) => left - right),
        });
        if (resetRequestRef.current?.fingerprint !== fingerprint) {
            resetRequestRef.current = {
                fingerprint,
                requestId: `writing-full-reset-${safeRandomUUID()}`,
            };
        }

        setResettingToPending(true);
        try {
            const resp = await authFetch('/api/writing/reset-to-pending', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                    'X-Request-ID': resetRequestRef.current.requestId,
                },
                body: JSON.stringify({ topic_ids: topicIds })
            });
            if (!resp.ok) {
                const error = await resp.json().catch(() => ({}));
                toast.error(`操作失败 (${resp.status}): ${apiErrorMessage(error, '请刷新后重试')}`);
                return;
            }
            const data = await resp.json();
            resetRequestRef.current = null;
            if (selectedProject) {
                const nextProject = { ...selectedProject, writing_status: 'pending' };
                selectedProjectRef.current = nextProject;
                setSelectedProject(nextProject);
            }
            if (selectedProject) {
                await loadProjectDetail(selectedProject.id);
            }
            setWorkbenchTab('pending');
            toast.success(data.message || `已将 ${data.reset_count ?? topicIds.length} 篇文章放回完整再生成`);
            setSelectedTopics(new Set());
        } catch (e: any) {
            console.error("重置失败:", e);
            toast.error("操作失败: " + (e?.message || "网络错误"));
        } finally {
            setResettingToPending(false);
        }
    };

    // 全选/取消
    const toggleSelectAll = () => {
        // 只选当前"待写"Tab下的 pending/draft 状态文章，不影响已完成的
        const pendingTopics = topics.filter(t => t.status === 'pending' || t.status === 'draft' || !t.status);
        const pendingIds = pendingTopics.map(t => t.id);
        const allSelected = pendingIds.every(id => selectedTopics.has(id));

        if (allSelected) {
            const newSet = new Set(selectedTopics);
            pendingIds.forEach(id => newSet.delete(id));
            setSelectedTopics(newSet);
        } else {
            setSelectedTopics(new Set([...selectedTopics, ...pendingIds]));
        }
    };

    // 切换选中
    const toggleSelect = (id: number) => {
        const newSet = new Set(selectedTopics);
        if (newSet.has(id)) {
            newSet.delete(id);
        } else {
            newSet.add(id);
        }
        setSelectedTopics(newSet);
    };

    // 全选/取消已完成文章
    const toggleSelectAllCompleted = () => {
        const completedTopics = topics.filter(t => t.status === 'completed');
        const completedIds = completedTopics.map(t => t.id);
        const allSelected = completedIds.every(id => selectedTopics.has(id));

        if (allSelected) {
            // 取消选中已完成的
            const newSet = new Set(selectedTopics);
            completedIds.forEach(id => newSet.delete(id));
            setSelectedTopics(newSet);
        } else {
            // 全选已完成的
            const newSet = new Set(selectedTopics);
            completedIds.forEach(id => newSet.add(id));
            setSelectedTopics(newSet);
        }
    };

    // 知识库核查：检查已完成文章是否与客户知识库数据矛盾
    const runKnowledgeCheck = async () => {
        if (!selectedProject) return;
        setCheckingKnowledge(true);
        try {
            const res = await authFetch(`/api/placement/knowledge-check/${selectedProject.id}`);
            if (res.ok) {
                const data = await res.json();
                if (data.success && data.articles) {
                    const map: typeof knowledgeCheckResults = {};
                    for (const art of data.articles) {
                        map[art.topic_id] = {
                            status: art.status,
                            contradictions: art.contradictions || [],
                            verified: art.verified || [],
                            message: art.message,
                        };
                    }
                    setKnowledgeCheckResults(map);
                }
            }
        } catch (err) {
            console.error('知识库核查失败:', err);
        } finally {
            setCheckingKnowledge(false);
        }
    };

    // 一键修复核查问题
    const fixKnowledgeIssues = async () => {
        if (!selectedProject) return;
        // 找出有问题的 topic_ids
        const problemTopicIds = Object.entries(knowledgeCheckResults)
            .filter(([, r]) => r.contradictions?.length > 0)
            .map(([id]) => Number(id));

        if (problemTopicIds.length === 0) {
            toast('没有需要修复的问题');
            return;
        }

        if (!(await askConfirm({ title: `将对 ${problemTopicIds.length} 篇有问题的文章进行局部修复（只改问题部分，不影响其他内容）。`, description: `修复后建议重新核查确认。继续吗？` }))) {
            return;
        }

        setFixingIssues(true);
        try {
            const res = await authFetch(`/api/placement/knowledge-fix/${selectedProject.id}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ topic_ids: problemTopicIds }),
            });
            if (res.ok) {
                const data = await res.json();
                toast.success(`修复完成！${data.fixed}/${data.total} 篇成功修复。建议点击"知识库核查"重新验证。`);
                // 清除旧的核查结果，提示用户重新核查
                setKnowledgeCheckResults({});
                // 重新加载文章数据
                if (selectedProject) {
                    loadProjectDetail(selectedProject.id);
                }
            } else {
                const err = await res.json().catch(() => ({}));
                toast.error(`修复失败: ${err.detail || '未知错误'}`);
            }
        } catch (err) {
            console.error('一键修复失败:', err);
            toast.error('修复请求失败，请检查网络');
        } finally {
            setFixingIssues(false);
        }
    };

    // 下载选中的已完成文章（ZIP压缩包，每篇单独一个文件）
    const downloadSelectedArticles = async () => {
        const completedTopics = topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id));
        // 已选中的完成topics，准备下载

        if (completedTopics.length === 0) {
            toast('请先勾选要下载的已完成文章');
            return;
        }

        try {
            // 动态导入JSZip
            const JSZip = (await import('jszip')).default;
            const zip = new JSZip();

            let successCount = 0;
            for (const topic of completedTopics) {
                if (topic.article_id) {
                    const res = await authFetch(`/api/articles/${topic.article_id}`);
                    if (res.ok) {
                        const data = await res.json();
                        // [工单 C-2 T1] 下载是"成品"交付面:优先用服务端发布口径渲染的
                        // content_export(占位符→公网图/剥离);兜底也要剥占位符,零裸露。
                        const articleContent = data.article?.content_export
                            ?? stripInternalPlaceholders(data.article?.content || data.content || '');
                        if (articleContent) {
                            // 清理文件名中的非法字符
                            const safeTitle = topic.optimized_title
                                .replace(/[\\/:*?"<>|]/g, '_')
                                .slice(0, 80);
                            const fileName = `${safeTitle}.md`;
                            zip.file(fileName, `# ${topic.optimized_title}\n\n${articleContent}`);
                            successCount++;
                        }
                    }
                } else {
                }
            }

            if (successCount === 0) {
                toast.error('没有可下载的文章内容');
                return;
            }

            // 生成ZIP文件并下载
            const content = await zip.generateAsync({ type: 'blob' });
            const url = URL.createObjectURL(content);
            const a = document.createElement('a');
            a.href = url;
            a.download = `GEO文章_${selectedProject?.brand_name || '批量'}_${new Date().toISOString().slice(0, 10)}.zip`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);
        } catch (err) {
            console.error('下载失败:', err);
            toast.error('下载失败，请重试');
        }
    };

    useEffect(() => {
        if (!selectedProject) {
            setLoading(true);
            // 有 preQuoteId 时加载全部项目（不按 status 过滤），确保能匹配到
            loadProjects(preQuoteId ? 'all' : activeTab);
        }
    }, [activeTab, currentBrandId]);

    useEffect(() => {
        void loadKnowledgeFiles(selectedProject?.brand_id);
    }, [selectedProject?.brand_id, loadKnowledgeFiles]);

    useEffect(() => {
        clearKnowledgeAutofillPolling();
        setKnowledgeAutofillState("idle");
        setKnowledgeAutofillMessage("");
        return () => {
            clearKnowledgeAutofillPolling();
        };
    }, [selectedProject?.brand_id]);

    /*
     * [#220 a1 ⓐ] 打开「补全知识库 / 写作资料」时**从服务端取一次**。
     *
     * 🔴 在此之前,`refreshKnowledgeAutofill`(唯一能把客户档案读进基础资料表的函数)
     *    全仓只有一个调用点,在**上传后的轮询体**里。打开抽屉只拉 brandAssets,
     *    而那份数据写的是 `brandProfile`,不写 `knowledgeBasics`。
     *    ⇒ 档案里明明填好了,冷启动打开也是空的(比 Owner 报的症状更宽)。
     * 🔴 这里换成 `refreshKnowledgeAutofill`,它内部就包含原来那次 `loadWritingBrandAssets`,
     *    所以**不是多加一次请求**,是同一次请求多喂了一张表。
     * 🔴 合并语义是 `fillIfEmpty`(mergeKnowledgeBasicsFromCleaned)——
     *    用户已经打了字的字段不会被这次重拉覆盖。别把它改成无条件覆盖。
     * 拉取时机:effect 的依赖变了才跑 ⇒ 关着不拉、反复 re-render 不重复拉、每次打开拉一次。
     */
    useEffect(() => {
        const brandId = selectedProject?.brand_id;
        if (!materialsDrawerOpen || !brandId) return;
        void refreshKnowledgeAutofill(brandId, selectedProject?.id ?? null);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [materialsDrawerOpen, selectedProject?.brand_id, selectedProject?.id]);

    // 从监测中心跳转：自动选项目 + 加载详情 + 切到优化 tab
    useEffect(() => {
        if (!preQuoteId || projects.length === 0) return;
        const match = projects.find(p => p.id === preQuoteId || p.quote_ids?.includes(preQuoteId));
        if (match) {
            selectedProjectRef.current = match;
            setSelectedProject(match);
            setDeliverySummary(null);
            setShowAdvancedWritingOptions(false);
            loadProjectDetail(match.id);
            loadDeliverySummary(match.id);
            loadKnowledgeStatus(match.id);
            loadKnowledgeFiles(match.brand_id);
            loadWritingBrandAssets(match.brand_id);
            loadCompetitors(match.id);
            if (preTab === 'optimize') setWorkbenchTab('optimize');
            // [P4 · B6] 🔴 在清空查询参数**之前**消费 plan_item_id。
            //    冻结:目标问题 / 文体规格 / 单一卖点 / 目标平台 / 落点。
            //    "去写这篇"只做预填与定位 —— **不自动生成、不扣费、不发布**。
            //    计划不存在 / 代际过期 / 无权访问 → 留在词包总览 + 一句可执行解释,
            //    绝不落空白页(合同 §3.2)。
            if (prePlanItemId && preQuoteId) {
                void (async () => {
                    try {
                        const data = await fetchPlanItem(
                            preQuoteId, prePlanItemId, prePlanGeneration ?? undefined,
                        );
                        const spec = data?.item?.frozen_spec || {};
                        setGapPlanItem({
                            planItemId: data.item.plan_item_id,
                            targetQuestion: spec.target_question || '',
                            contentForm: spec.content_form || '',
                            sellingPoint: spec.single_selling_point || '',
                            targetDomain: spec.target_domain || '',
                            targetDisplayName: spec.target_display_name || '',
                            executable: Boolean(data.item.executable),
                            statusLabel: data.item.status?.label || '',
                        });
                        setGapPlanNotice('');
                    } catch (err) {
                        const parsed = parseGapPlanError(err);
                        setGapPlanItem(null);
                        setGapPlanNotice(
                            parsed?.message
                            || '这篇任务暂时打不开，请回到报价详情的「交付计划」重新选一篇。',
                        );
                    }
                })();
            }
            setSearchParams({});
        }
    }, [projects, preQuoteId, preTab, prePlanItemId, prePlanGeneration,
        loadKnowledgeFiles, loadWritingBrandAssets, setSearchParams]);

    // 沙盒态: 不自动选项目 · 让用户从项目列表手动点进 (跟真实代理操作一致)
    // spotlight 引导挂在项目卡上 · 见下方 ProjectCard wrap

    // 沙盒态自动勾选 · 减少用户在教程里点 "全选" 的步骤
    // - 标题就绪 / pending 状态 → 全勾, 让 "开始写作" 按钮可点
    // - 全部完成 → 全勾, 让 "发布" 按钮可点
    useEffect(() => {
        if (!isSandboxActive() || topics.length === 0) return;
        const allCompleted = topics.every(t => t.status === 'completed');
        const allPending = topics.every(t => t.status === 'pending');
        if (allCompleted || allPending) {
            setSelectedTopics(new Set(topics.map(t => t.id)));
        }
    }, [topics]);

    // 沙盒态: 跟 tutorialStage 联动 · 选择项目后强制重置教程子状态到第一步
    // 不管之前 stage 是 step3-page / step3-upload-kb / step3-gen-titles 都覆盖回 step3-explain-modes
    // 这样用户上次跑过没走完，重新进项目能从同行搜证这步重走（mock 数据也跟着重置）
    const tutorialStage = useTutorialStage();

    // 持久化教程 stage 可能在刷新后早于项目状态恢复。没有项目时不能继续寻找
    // 项目内按钮，否则用户只会看到“目标不可用”；退回项目选择是可执行恢复路径。
    useEffect(() => {
        if (!isSandboxActive() || selectedProject) return;
        if (
            tutorialStage.startsWith('step3-')
            && tutorialStage !== 'step3-sidebar'
            && tutorialStage !== 'step3-page'
        ) {
            setTutorialStage('step3-page');
        }
    }, [selectedProject, tutorialStage]);

    // 沙盒态写作中: 当 progress 推到 done 时自动切到"已完成" tab + 推 stage 到 "看文章" spotlight
    // (用户点完"看过文章 →"之后再推到 go-publish 显示 发布 spotlight)
    //
    // 2026-05-24 BUGFIX: 推进条件鲁棒化
    // 原代码只在 stage === 'step3-start-writing' 时才推到 show-articles · 卡死场景多:
    //   - 用户刷新页面 · stage 持久化值停在中间任意点
    //   - 用户切走再回来 · effect 不重跑
    //   - 上一步 spotlight 漏点 "下一步" 就跳过去了
    // 任一情况下都会出现 "6 篇 done 但 stage 还在 step3-gen-titles 等早期阶段 · spotlight 永远不显示"
    // 改成: 任何 step3-* 早期阶段进入 allDone 都强制推到 show-articles
    useEffect(() => {
        if (!isSandboxActive()) return;
        // step 4 补发流程: 不要把用户从 optimize tab 切走
        if (tutorialStage.startsWith('step4-')) return;
        const allDone = topics.length > 0 && topics.every(t => t.status === 'completed');
        if (allDone) {
            if (workbenchTab !== 'completed') setWorkbenchTab('completed');
            // 6 篇都 completed 时 · 任何 step3-* 早期阶段都推到 show-articles
            // show-articles / go-publish / pub-* 是 step3 后期 · 不应被覆盖
            const earlyStep3Stages: string[] = [
                'step3-sidebar', 'step3-page', 'step3-explain-modes', 'step3-pick-real',
                'step3-show-competitors', 'step3-upload-kb', 'step3-upload-kb-click',
                'step3-gen-titles', 'step3-expand-titles', 'step3-show-titles',
                'step3-start-writing',
            ];
            if (earlyStep3Stages.includes(tutorialStage)) {
                setTutorialStage('step3-show-articles');
            }
        }
    }, [topics, workbenchTab, tutorialStage]);

    // step 4 补发: opt-write 阶段自动勾选补发待写文(让"开始写作"可点)
    useEffect(() => {
        if (!isSandboxActive() || tutorialStage !== 'step4-opt-write') return;
        const optPend = topics.filter(t => t.is_optimize && (t.status === 'pending' || t.status === 'draft'));
        const optWritingNow = topics.filter(t => t.is_optimize && t.status === 'writing');
        // 还没开始写(有待写、无写作中)· 且当前没选 → 自动勾选待写补发文
        if (optPend.length > 0 && optWritingNow.length === 0 && selectedTopics.size === 0) {
            setSelectedTopics(new Set(optPend.map(t => t.id)));
        }
    }, [tutorialStage, topics, selectedTopics.size]);

    // step 4 补发: 补发文全部写完 → 自动勾选已完成 + 推进到"去发布"引导
    useEffect(() => {
        if (!isSandboxActive() || tutorialStage !== 'step4-opt-write') return;
        const optAll = topics.filter(t => t.is_optimize);
        const optDone = optAll.filter(t => t.status === 'completed');
        if (optAll.length > 0 && optDone.length === optAll.length) {
            setSelectedTopics(new Set(optDone.map(t => t.id)));
            setTutorialStage('step4-opt-gopublish');
        }
    }, [tutorialStage, topics]);

    const sandboxResetProjectIdRef = useRef<number | null>(null);
    useEffect(() => {
        if (!isSandboxActive() || !selectedProject) return;
        if (sandboxResetProjectIdRef.current === selectedProject.id) return;
        // step 4 收尾(从监测页补发跳来)· 不重置回 step3, 否则会冲掉收尾窗
        if (tutorialStage.startsWith('step4-')) return;
        sandboxResetProjectIdRef.current = selectedProject.id;
        // 首次进这个项目: 把 mode + competitors + 上传模拟状态 + tutorial stage 都拉到起点
        setCompetitorMode('evidence_only');
        setCompetitors([]);
        setSandboxKbUploading(false);
        setSandboxKbDone(false);
        setTutorialStage('step3-explain-modes');
    }, [selectedProject]);

    // 沙盒：候选完成核验后，让用户看 1.5s 绿色高亮再推到“展示同行” spotlight
    // 避免点击搜证后 stage 立刻跳走，用户没机会看到结果
    useEffect(() => {
        if (!isSandboxActive() || tutorialStage !== 'step3-explain-modes') return;
        if (competitorMode !== 'real') return;
        const t = window.setTimeout(() => {
            setTutorialStage('step3-show-competitors');
        }, 1500);
        return () => window.clearTimeout(t);
    }, [competitorMode, tutorialStage]);

    // 轮询：检测到 regenerating 状态的 topic 时，每 3 秒自动刷新
    const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const hasRegenerating = topics.some(t => t.status === 'regenerating');

    useEffect(() => {
        if (hasRegenerating && selectedProject) {
            if (!pollingRef.current) {
                pollingRef.current = setInterval(() => {
                    loadProjectDetail(selectedProject.id);
                }, 3000);
            }
        } else {
            if (pollingRef.current) {
                clearInterval(pollingRef.current);
                pollingRef.current = null;
            }
        }
        return () => {
            if (pollingRef.current) {
                clearInterval(pollingRef.current);
                pollingRef.current = null;
            }
        };
    }, [hasRegenerating, selectedProject?.id]);

    // 切换关键词展开/折叠
    const toggleKeywordExpand = (kwId: number) => {
        setExpandedKeywords(prev => {
            const newSet = new Set(prev);
            if (newSet.has(kwId)) {
                newSet.delete(kwId);
            } else {
                newSet.add(kwId);
            }
            return newSet;
        });
    };

    // 全部展开/折叠
    const toggleAllKeywords = () => {
        if (expandedKeywords.size === keywords.length) {
            setExpandedKeywords(new Set());
        } else {
            setExpandedKeywords(new Set(keywords.map(k => k.id)));
        }
    };

    // 渲染项目列表
    const renderProjectList = () => (
        <div className="space-y-3">
            {loading ? (
                <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
                    <Loader2 className="h-8 w-8 animate-spin text-brand mb-3" />
                    <span className="text-sm">加载项目中...</span>
                </div>
            ) : projects.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-20 text-muted-foreground">
                    <BookOpen className="h-12 w-12 mb-3 opacity-30" />
                    <p className="text-sm">暂无{STATUS_CONFIG[activeTab]?.label || ""}项目</p>
                    <p className="text-xs mt-2 opacity-60">完成品牌诊断并签约后，写作项目将自动出现在这里</p>
                </div>
            ) : (
                projects.map((project, projectIdx) => {
                    const status = STATUS_CONFIG[project.writing_status];
                    const showSandboxSpotlight = isSandboxActive() && projectIdx === 0 && !selectedProject;
                    const cardEl = (
                        <Card
                            key={project.id}
                            className="group border border-border rounded-xl hover:border-gray-300 transition-all duration-200 cursor-pointer"
                            onClick={() => enterWorkbench(project)}
                        >
                            <CardContent className="p-3 sm:p-5">
                                <div className="flex items-start sm:items-center justify-between gap-3 sm:gap-4">
                                    <div className="flex-1 min-w-0 space-y-1.5 sm:space-y-2">
                                        <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
                                            <h3 className="text-sm sm:text-base font-semibold text-foreground truncate max-w-[200px] sm:max-w-none">
                                                {project.brand_name}
                                            </h3>
                                            <Badge className={`text-xs font-normal ${status?.color || "bg-muted text-muted-foreground"}`}>
                                                {status?.label || project.writing_status}
                                            </Badge>
                                        </div>
                                        <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 text-xs sm:text-sm text-muted-foreground">
                                            <span>{project.industry}</span>
                                            <span className="hidden sm:inline text-border">|</span>
                                            <span>{project.keyword_count}个词</span>
                                            <span className="hidden sm:inline text-border">|</span>
                                            {/* 🔴 [#225 a1 下半] 这里的数来自**列表端点**的
                                                `total_required_articles` = 合同授权的**槽**总数。
                                                原来标着「篇」—— 那正是本单要消灭的错标。
                                                改标「槽」即可,不需要新字段。
                                                (按口径算出的「篇」合计只在**详情**端点有
                                                 `total_planned_posts_default`;列表端点今天不给,
                                                 要在卡上也显示篇数得先让 C 补那一侧。) */}
                                            <span>{project.total_required_articles || '-'}槽</span>
                                            <span className="hidden sm:inline text-border">|</span>
                                            <span>¥{(project.monthly_price || 0).toLocaleString()}</span>
                                        </div>
                                        <p className="text-xs text-muted-foreground">
                                            {project.confirmed_at?.slice(0, 10)}
                                        </p>
                                    </div>
                                    <div className="flex items-center gap-1.5 shrink-0">
                                        <Button
                                            variant={project.writing_status === 'pending' ? 'default' : 'outline'}
                                            size="sm"
                                            className="text-xs sm:text-sm"
                                            onClick={(e) => { e.stopPropagation(); enterWorkbench(project); }}
                                        >
                                            {project.writing_status === 'pending' ? (
                                                <><Play className="h-3.5 w-3.5 mr-1" /><span className="hidden sm:inline">开始</span>创作</>
                                            ) : project.writing_status === 'titles_ready' ? (
                                                <span><span className="hidden sm:inline">检查</span>标题</span>
                                            ) : (
                                                <span><span className="hidden sm:inline">查看</span>详情</span>
                                            )}
                                        </Button>
                                        {/* P1.4.2 (CTO-15.23 2026-05-04) · 写作项目软删除 · 误删后从报价系统重新触发 from-c-end-plan 自然新建 */}
                                        {canArchiveProject && <Button
                                            variant="ghost"
                                            size="icon"
                                            className="h-8 w-8 text-muted-foreground hover:text-destructive opacity-50 group-hover:opacity-100 sm:opacity-100"
                                            title="删除项目(可从报价处重新创建)"
                                            onClick={async (e) => {
                                                e.stopPropagation();
                                                try {
                                                    const previewResponse = await authFetch(`/api/writing/projects/${project.id}/archive-preview`);
                                                    const preview = await previewResponse.json();
                                                    if (!previewResponse.ok) throw new Error(preview?.detail?.message || preview?.detail || '无法读取归档影响');
                                                    const counts = preview.associations;
                                                    if (!(await askConfirm({ title: `归档写作项目「${preview.object.display_name}」(#${preview.object.quote_id})？`, description: `关联 ${counts.keywords} 个关键词、${counts.topics} 个主题、${counts.articles} 篇文章。\n内容全部保留，可通过恢复入口找回。`, danger: true }))) return;
                                                    const response = await authFetch(`/api/writing/projects/${project.id}`, { method: 'DELETE' });
                                                    const data = await response.json();
                                                    if (!response.ok || !data.success) throw new Error(data?.detail?.message || data?.detail || '归档失败');
                                                    toast.success('写作项目已归档 · 关联内容全部保留', {
                                                        description: '误操作可立即撤销。',
                                                        duration: 12000,
                                                        action: {
                                                            label: '撤销',
                                                            onClick: () => {
                                                                void authFetch(`/api/writing/projects/${project.id}/restore`, { method: 'POST' })
                                                                    .then(async restoreResponse => {
                                                                        const restoreBody = await restoreResponse.json().catch(() => ({}));
                                                                        if (!restoreResponse.ok || !restoreBody.success) {
                                                                            throw new Error(restoreBody?.detail?.message || restoreBody?.detail || '恢复失败');
                                                                        }
                                                                        toast.success('写作项目已恢复');
                                                                        await loadProjects(activeTab);
                                                                    })
                                                                    .catch(restoreError => toast.error(restoreError?.message || '恢复失败'));
                                                            },
                                                        },
                                                    });
                                                    loadProjects(activeTab);
                                                } catch (error: any) {
                                                    toast.error(error?.message || '归档失败');
                                                }
                                            }}
                                        >
                                            <X className="h-4 w-4" />
                                        </Button>}
                                    </div>
                                </div>
                            </CardContent>
                        </Card>
                    );
                    if (showSandboxSpotlight) {
                        return (
                            <FeatureTooltip
                                key={`sandbox-project-card-${project.id}`}
                                featureId="sandbox_writing_pick_project"
                                stepId="first_article_publish"
                                title="第二步: 点这个项目卡进入"
                                content="点这里进入一路顺风出行服务的写作工作台"
                                side="bottom"
                                disabled={!isSandboxActive() || !!selectedProject}
                                wrapClassName="relative block"
                            >
                                {cardEl}
                            </FeatureTooltip>
                        );
                    }
                    return cardEl;
                })
            )}
        </div>
    );

    const renderKnowledgeAutofillNotice = () => {
        if (!knowledgeAutofillMessage) return null;
        const tone = knowledgeAutofillState === "synced"
            ? "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300"
            : knowledgeAutofillState === "delayed"
                ? "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300"
                : "border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300";
        return (
            <div className={`flex items-start gap-2 rounded-lg border px-3 py-2 text-xs ${tone}`}>
                {knowledgeAutofillState === "waiting" ? (
                    <Loader2 className="mt-0.5 h-3.5 w-3.5 shrink-0 animate-spin" />
                ) : knowledgeAutofillState === "synced" ? (
                    <Check className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                ) : (
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                )}
                <span>{knowledgeAutofillMessage}</span>
            </div>
        );
    };

    const renderKnowledgeBasicsForm = () => (
        <div className="rounded-xl border border-emerald-500/20 bg-emerald-500/[0.06] p-3 space-y-3">
            <div>
                <div className="flex items-center gap-2">
                    <h4 className="text-sm font-semibold text-foreground">基础资料表</h4>
                    <Badge variant="outline" className="border-emerald-500/30 text-[11px] text-emerald-600 dark:text-emerald-300">
                        优先填写
                    </Badge>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                    这些是任何客户写文章都会用到的底表；联系方式只是可选补充，不影响整理和写作。
                </p>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
                <div className="space-y-1.5 sm:col-span-2">
                    <Label className="text-xs text-muted-foreground">一句话业务描述</Label>
                    <Input
                        value={knowledgeBasics.business_summary}
                        data-testid="kb-business_summary"
                        onChange={(e) => updateKnowledgeBasics("business_summary", e.target.value)}
                        placeholder="例如: 专注中高端家装,提供设计施工一体化服务"
                    />
                </div>
                <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">目标客户</Label>
                    <Input
                        value={knowledgeBasics.target_customers}
                        data-testid="kb-target_customers"
                        onChange={(e) => updateKnowledgeBasics("target_customers", e.target.value)}
                        placeholder="例如: 改善型业主、企业采购、宝妈群体"
                    />
                </div>
                <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">产品/服务</Label>
                    <Input
                        value={knowledgeBasics.products_services}
                        data-testid="kb-products_services"
                        onChange={(e) => updateKnowledgeBasics("products_services", e.target.value)}
                        placeholder="例如: 全屋设计、施工管理、软装搭配"
                    />
                </div>
                <div className="space-y-1.5 sm:col-span-2">
                    <Label className="text-xs text-muted-foreground">核心卖点</Label>
                    <textarea
                        value={knowledgeBasics.key_selling_points}
                        data-testid="kb-key_selling_points"
                        onChange={(e) => updateKnowledgeBasics("key_selling_points", e.target.value)}
                        className="min-h-[74px] w-full rounded-lg border border-border bg-card px-3 py-2 text-sm"
                        placeholder="列出客户最应该被 AI 记住的优势,可用逗号或换行分隔"
                    />
                </div>
                <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">案例/口碑</Label>
                    <textarea
                        value={knowledgeBasics.proof_cases}
                        data-testid="kb-proof_cases"
                        onChange={(e) => updateKnowledgeBasics("proof_cases", e.target.value)}
                        className="min-h-[74px] w-full rounded-lg border border-border bg-card px-3 py-2 text-sm"
                        placeholder="客户案例、评价、资质、合作对象都可以写这里"
                    />
                </div>
                <div className="space-y-1.5">
                    <Label className="text-xs text-muted-foreground">禁用表达</Label>
                    <textarea
                        value={knowledgeBasics.forbidden_notes}
                        data-testid="kb-forbidden_notes"
                        onChange={(e) => updateKnowledgeBasics("forbidden_notes", e.target.value)}
                        className="min-h-[74px] w-full rounded-lg border border-border bg-card px-3 py-2 text-sm"
                        placeholder="例如: 不写低价、不要承诺效果、不要露出内部价格"
                    />
                </div>
            </div>
        </div>
    );

    /* 🔴 [#234-a1 2026-09-17] `renderKnowledgePanel`(391 行)已退役:**全仓零调用点**。
       它不是普通死代码 —— 里面装着 `step3-upload-kb` 的全部消费点与 `step3-gen-titles`
       的唯一生产点,而把用户推进该阶段的代码在活的那一侧 ⇒ 沙盒教程第三步在生产上是断的。
       本笔先把那三样(coach mark / 拦截与模拟上传 / 视觉反馈)搬到活的抽屉屏,**再**退役这里;
       顺序反过来的话 #190 门会先红着,红基线之后的注毒都不可读。 */

    const renderKnowledgeSummaryCard = () => {
        const statusKey = knowledgeStatus?.status || 'none';
        const statusMeta = KNOWLEDGE_STATUS_CONFIG[statusKey] || KNOWLEDGE_STATUS_CONFIG.none;
        const summary = knowledgeStatus?.materials_summary;
        const filledCount = summary?.filled_count ?? 0;
        const totalFields = summary?.total_fields || 8;
        const contactCount = [
            contactForm.contact_phone,
            contactForm.contact_wechat,
            contactForm.contact_website,
            contactForm.contact_address,
        ].filter(Boolean).length;
        const contactReady = contactCount > 0;
        const previewAssets = brandImageAssets.slice(0, 3);
        const toAssetUrl = (asset: BrandImageAsset) => {
            const key = asset.thumbnail_key || asset.public_url || "";
            return key ? (key.startsWith("/") ? key : `/${key}`) : "";
        };

        return (
            <Card className="border border-emerald-500/25 bg-card/80">
                <CardContent className="p-4 space-y-4">
                    <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0">
                            <div className="flex items-center gap-2">
                                <BookOpen className="h-4 w-4 text-emerald-500" />
                                <h3 className="text-sm font-semibold text-foreground">知识库 / 写作资料</h3>
                            </div>
                            <p className="mt-1 text-xs text-muted-foreground">客户基础资料、图片和可选联系方式都会同步到客户档案。</p>
                        </div>
                        {(knowledgeLoading || brandAssetsLoading) && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
                    </div>

                    <div className="flex flex-wrap gap-1.5">
                        <Badge variant="outline" className="border-emerald-500/30 text-emerald-600 text-[11px] dark:text-emerald-300">资料 {filledCount}/{totalFields}</Badge>
                        <Badge variant="outline" className={`text-[11px] ${contactReady ? 'border-emerald-500/30 text-emerald-600' : 'border-border/60 text-muted-foreground'}`}>
                            {contactReady ? "可选联系方式已填" : "可选联系方式未填"}
                        </Badge>
                        <Badge variant="outline" className={`text-[11px] ${brandImageAssets.length > 0 ? 'border-emerald-500/30 text-emerald-600' : 'border-amber-500/30 text-amber-600'}`}>
                            图片 {brandImageAssets.length}张
                        </Badge>
                        <Badge className={`border text-[11px] font-normal ${statusMeta.color}`}>{statusMeta.label}</Badge>
                    </div>

                    <div className="space-y-2 text-xs">
                        <div className="rounded-lg border border-border/50 bg-background/40 p-2.5">
                            <div className="text-muted-foreground">资料摘要</div>
                            <div className="mt-1 line-clamp-2 text-foreground">
                                {summary?.intro_excerpt || summary?.usp_excerpt || "还没有整理出客户资料摘要"}
                            </div>
                        </div>
                        <div className="rounded-lg border border-border/50 bg-background/40 p-2.5">
                            <div className="text-muted-foreground">可选联系方式</div>
                            <div className="mt-1 line-clamp-2 text-foreground">
                                {[contactForm.contact_phone, contactForm.contact_wechat, contactForm.contact_website].filter(Boolean).join(" · ") || "未填写,不影响写作"}
                            </div>
                        </div>
                        <div className="rounded-lg border border-border/50 bg-background/40 p-2.5">
                            <div className="mb-2 flex items-center justify-between">
                                <span className="text-muted-foreground">图片素材</span>
                                <span className="text-muted-foreground/70">{brandImageAssets.length}张</span>
                            </div>
                            {previewAssets.length > 0 ? (
                                <div className="flex gap-2">
                                    {previewAssets.map(asset => {
                                        const src = toAssetUrl(asset);
                                        return src ? (
                                            <img key={asset.id} src={src} alt={asset.title || asset.file_name || ""} className="h-12 w-12 rounded-md border border-border/50 object-cover" loading="lazy" />
                                        ) : (
                                            <div key={asset.id} className="h-12 w-12 rounded-md border border-border/50 bg-muted" />
                                        );
                                    })}
                                </div>
                            ) : (
                                <div className="text-muted-foreground">暂无图片素材</div>
                            )}
                        </div>
                    </div>

                    <div className="flex flex-wrap gap-2">
                        {/* testid 是给门用的抓手:页面上「补全资料」这四个字还出现在步骤条上,
                            按文字找会先抓到那个标签(点了什么也不会发生)。 */}
                        {/* 🔴 [#234-a1] 教程第三步的 coach mark 原来挂在 `renderKnowledgePanel`
                            里的「展开客户资料区」按钮上,而那个函数**全仓零调用点** ——
                            活代码(下方 onNext)把用户推到 `step3-upload-kb`,却没有任何一处会响应它,
                            spotlight 不出现、链也永远推进不到 `step3-gen-titles`。
                            搬到这颗**真的会渲染**的按钮上。 */}
                        <FeatureTooltip
                            featureId="sandbox_step3_open_kb"
                            stepId="first_article_publish"
                            title="第三步: 补全客户资料"
                            content="没有客户资料 AI 写不出好文章 · 点这里打开资料抽屉试一下上传"
                            side="bottom"
                            disabled={!isSandboxActive() || tutorialStage !== 'step3-upload-kb'}
                            wrapClassName="flex-1 inline-block"
                        >
                        <Button size="sm" data-testid="knowledge-open-drawer"
                            onClick={() => {
                                setMaterialsDrawerOpen(true);
                                if (isSandboxActive() && tutorialStage === 'step3-upload-kb') {
                                    setTutorialStage('step3-upload-kb-click');
                                }
                            }} className="w-full">
                            <FileUp className="h-4 w-4 mr-1" />
                            补全资料
                        </Button>
                        </FeatureTooltip>
                        <Button
                            size="sm"
                            variant="outline"
                            onClick={generateKnowledgeLink}
                            disabled={generatingKnowledgeLink || !knowledgeStatus?.brand_id}
                        >
                            {generatingKnowledgeLink ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Link2 className="h-4 w-4 mr-1" />}
                            确认链接
                        </Button>
                    </div>
                </CardContent>
            </Card>
        );
    };

    const renderFactCheckSummaryCard = () => {
        const results = Object.values(knowledgeCheckResults);
        const passed = results.filter(r => r.status === 'pass').length;
        const warned = results.filter(r => r.status === 'warning').length;
        const failed = results.filter(r => r.status === 'fail').length;
        const activeIssues = results.reduce((sum, r) => sum + (r.contradictions || []).filter(c => !c.dismissed).length, 0);
        const completedCount = topics.filter(t => t.status === 'completed').length;
        // [§3C-3] "存在未核查项" = 已完成但还没被核查计数覆盖的那部分。
        // 由现成的三档计数推导,不新增接口;拿不准时取 0(宁可不亮,不误报)。
        const uncheckedCount = Math.max(0, completedCount - (passed + warned + failed));

        return (
            <Card className="border border-border/60 bg-card/80">
                <CardContent className="p-4 space-y-4">
                    <div className="flex items-start justify-between gap-3">
                        <div>
                            <div className="flex items-center gap-2">
                                <ShieldCheck className="h-4 w-4 text-blue-500" />
                                <h3 className="text-sm font-semibold text-foreground">文章事实核查</h3>
                            </div>
                            <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
                                核对文章中的价格、面积、同行名称是否与知识库一致。
                            </p>
                        </div>
                    </div>

                    <div className="grid grid-cols-3 gap-2 text-center text-xs">
                        <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 p-2">
                            <div className="text-lg font-semibold text-emerald-600 dark:text-emerald-300">{passed}</div>
                            <div className="text-muted-foreground">通过</div>
                        </div>
                        <div className="rounded-lg border border-amber-500/20 bg-amber-500/10 p-2">
                            <div className="text-lg font-semibold text-amber-600 dark:text-amber-300">{warned}</div>
                            <div className="text-muted-foreground">需复核</div>
                        </div>
                        <div className="rounded-lg border border-red-500/20 bg-red-500/10 p-2">
                            <div className="text-lg font-semibold text-red-600 dark:text-red-300">{failed}</div>
                            <div className="text-muted-foreground">严重</div>
                        </div>
                    </div>

                    <p className="rounded-lg border border-border/40 bg-muted/30 p-2 text-xs leading-relaxed text-muted-foreground">
                        不是要求每篇覆盖全部资料,只检查已写到的客户事实是否冲突。
                    </p>

                    <div className="flex flex-wrap gap-2">
                        <Button
                            size="sm"
                            variant="outline"
                            onClick={runKnowledgeCheck}
                            disabled={checkingKnowledge || completedCount === 0}
                            /* [核验流融合 §3C-3 · 2026-08-01] 提示上限 = **微光**。
                               Owner:"核查不核查全权交给用户,不点就默认通过,强行核查招人烦。
                               最多微微光提示。" → 存在未核查项时只让按钮泛一层柔光 + 一个小红点,
                               不弹窗、不拦、不加感叹号、不变红。不点它就是默认通过。 */
                            className={`relative flex-1 border-blue-300 text-blue-600 hover:bg-blue-50 dark:border-blue-500/40 dark:text-blue-300${
                                uncheckedCount > 0 && !checkingKnowledge
                                    ? ' ring-1 ring-blue-400/40 shadow-[0_0_0_3px_rgba(59,130,246,0.10)]'
                                    : ''
                            }`}
                            title={uncheckedCount > 0
                                ? `有 ${uncheckedCount} 篇还没核查过。核不核查由你决定,不影响发布。`
                                : undefined}
                        >
                            {checkingKnowledge ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <ShieldCheck className="h-4 w-4 mr-1" />}
                            {checkingKnowledge ? "核查中..." : "知识库核查"}
                            {uncheckedCount > 0 && !checkingKnowledge && (
                                <span
                                    data-testid="kb-check-unchecked-dot"
                                    aria-hidden
                                    className="absolute -right-0.5 -top-0.5 h-2 w-2 rounded-full bg-blue-500"
                                />
                            )}
                        </Button>
                        {activeIssues > 0 && (
                            <Button
                                size="sm"
                                variant="outline"
                                onClick={fixKnowledgeIssues}
                                disabled={fixingIssues}
                                className="border-orange-300 text-orange-600 hover:bg-orange-50 dark:border-orange-500/40 dark:text-orange-300"
                            >
                                {fixingIssues ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Wrench className="h-4 w-4 mr-1" />}
                                修复未忽略
                            </Button>
                        )}
                    </div>

                    {results.length === 0 && (
                        <div className="text-xs text-muted-foreground">
                            文章完成后可在这里核查事实一致性。
                        </div>
                    )}
                </CardContent>
            </Card>
        );
    };

    const renderMaterialsDrawer = () => {
        if (!materialsDrawerOpen || !selectedProject?.brand_id) return null;
        const brandId = selectedProject.brand_id;
        const profileRoute = brandProfile?.source === "self" ? "/my-brand" : `/my-clients/${brandId}`;
        const summary = knowledgeStatus?.materials_summary;
        const filledCount = summary?.filled_count ?? 0;
        const totalFields = summary?.total_fields || 8;
        const hasExistingProfileMaterial = Boolean(
            filledCount > 0 ||
            knowledgeStatus?.knowledge_count ||
            summary?.intro_excerpt ||
            summary?.usp_excerpt
        );

        return (
            <div className="fixed inset-0 z-50 bg-black/45" onClick={() => setMaterialsDrawerOpen(false)}>
                <aside
                    className="ml-auto flex h-full w-full max-w-2xl flex-col border-l border-border bg-background shadow-2xl"
                    onClick={(e) => e.stopPropagation()}
                >
                    <div className="flex items-start justify-between gap-3 border-b border-border p-4">
                        <div className="min-w-0">
                            <h2 className="text-lg font-semibold text-foreground">补全知识库 / 写作资料</h2>
                            <p className="mt-1 text-xs text-muted-foreground">当前录入会同步到我的客户 / 我的品牌。</p>
                            <p className="mt-2 rounded-lg border border-emerald-500/25 bg-emerald-500/10 px-2 py-1.5 text-xs text-emerald-600 dark:text-emerald-300">
                                已连接客户档案: {selectedProject.brand_name} · 仅当前账号可见
                            </p>
                        </div>
                        <Button data-testid="kb-close-drawer" variant="ghost" size="icon" onClick={() => setMaterialsDrawerOpen(false)}>
                            <X className="h-4 w-4" />
                        </Button>
                    </div>

                    <div className="flex-1 overflow-y-auto p-4 space-y-5">
                        <section className="space-y-3">
                            <div className="flex items-center justify-between gap-2">
                                <h3 className="text-sm font-semibold text-foreground">补充客户资料</h3>
                                <Button variant="outline" size="sm" onClick={() => navigate(profileRoute)}>
                                    进入客户档案补全
                                </Button>
                            </div>
                            <div className="rounded-xl border border-emerald-500/25 bg-emerald-500/10 p-3 text-xs leading-relaxed">
                                <div className="flex items-center justify-between gap-2">
                                    <span className="font-medium text-emerald-600 dark:text-emerald-300">
                                        {hasExistingProfileMaterial ? "已有客户档案会自动参与整理" : "可粘贴或上传客户资料"}
                                    </span>
                                    <Badge variant="outline" className="border-emerald-500/30 text-[11px] text-emerald-600 dark:text-emerald-300">
                                        已整理 {filledCount}/{totalFields}
                                    </Badge>
                                </div>
                                <p className="mt-1 text-muted-foreground">
                                    {hasExistingProfileMaterial
                                        ? "AI 会先读取现有客户档案，再结合这里新增的官网介绍、产品服务、案例和客户评价继续补全。"
                                        : "这里不是必填表格；有官网介绍、产品服务、案例或客户评价就粘贴，没有也可以先上传资料文件。"}
                                </p>
                                {(summary?.intro_excerpt || summary?.usp_excerpt) && (
                                    <p className="mt-2 line-clamp-2 text-foreground">
                                        {summary?.intro_excerpt || summary?.usp_excerpt}
                                    </p>
                                )}
                            </div>
                            <div className="grid gap-3">
                                {renderKnowledgeBasicsForm()}

                                <textarea
                                    value={knowledgeRawText}
                                    data-testid="kb-raw-text"
                                    onChange={(e) => setKnowledgeRawText(e.target.value.slice(0, 20000))}
                                    className="min-h-[110px] rounded-lg border border-border bg-card p-3 text-sm"
                                    placeholder="补充粘贴官网介绍、销售话术、长案例、客户评价等原始资料"
                                />
                                <input
                                    value={knowledgeNotes}
                                    data-testid="kb-notes"
                                    onChange={(e) => setKnowledgeNotes(e.target.value.slice(0, 2000))}
                                    className="min-h-[40px] rounded-lg border border-border bg-card px-3 text-sm"
                                    placeholder="备注:资料来源、销售补充、禁用表达等"
                                />
                                <div className="flex flex-wrap gap-2">
                                    <Button data-testid="kb-save" onClick={saveKnowledgeText} disabled={knowledgeSavingText || !hasKnowledgeCleanInput}>
                                        {knowledgeSavingText ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <FileText className="h-4 w-4 mr-1" />}
                                        保存资料
                                    </Button>
                                    <Button variant="outline" onClick={cleanKnowledgeMaterials} disabled={cleaningKnowledge || !canCleanKnowledgeMaterials}>
                                        {cleaningKnowledge ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Sparkles className="h-4 w-4 mr-1" />}
                                        整理/更新客户档案
                                    </Button>
                                    {/* 🔴 [#234-a1] `step3-upload-kb-click` 的拦截与模拟上传原在死函数里(:5814)。
                                        沙盒态点这里**不真传文件**:模拟上传中 → 已完成 → 800ms 后推 `step3-gen-titles`,
                                        下游 :6673/:6690 那两处活代码才等得到。 */}
                                    <FeatureTooltip
                                        featureId="sandbox_step3_upload_kb"
                                        stepId="first_article_publish"
                                        title="第三步: 上传客户资料"
                                        content="点这一块模拟上传 · 教程模式不真传文件 · 1.5 秒后自动变成已上传, 然后才能开始写作"
                                        side="top"
                                        disabled={!isSandboxActive() || tutorialStage !== 'step3-upload-kb-click'}
                                        wrapClassName="inline-block"
                                    >
                                    <label
                                        onClick={handleSandboxKbUploadClick}
                                        className="inline-flex min-h-[40px] cursor-pointer items-center rounded-md border border-border px-3 text-sm text-foreground hover:bg-muted/40">
                                        <input
                                            ref={knowledgeFileInputRef}
                                            type="file"
                                            multiple
                                            accept={KNOWLEDGE_FILE_ACCEPT}
                                            className="hidden"
                                            onChange={handleKnowledgeFileSelect}
                                        />
                                        {/* 🔴 模拟上传的**视觉反馈**也一起搬过来(原在死函数 :5871/:5876)。
                                            只搬 setter 不搬反馈的话,用户点完 1.5 秒内屏幕上什么都没有 ——
                                            那是换了个地方断,不是修好。 */}
                                        {sandboxKbUploading ? (
                                            <><Loader2 className="h-4 w-4 animate-spin mr-1 text-amber-500" />模拟上传中...</>
                                        ) : sandboxKbDone ? (
                                            <><Check className="h-4 w-4 mr-1 text-emerald-500" />已上传并入库</>
                                        ) : (
                                            <>{knowledgeUploading ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <FileUp className="h-4 w-4 mr-1" />}上传资料文件</>
                                        )}
                                    </label>
                                    </FeatureTooltip>
                                </div>
                                {renderKnowledgeAutofillNotice()}
                            </div>
                        </section>

                        <details className="rounded-xl border border-border/60 bg-card/60 p-3">
                            <summary className="cursor-pointer text-sm font-semibold text-foreground">
                                可选联系方式
                                <span className="ml-2 text-xs font-normal text-muted-foreground">不填写也能整理资料和写文章</span>
                            </summary>
                            <div className="mt-3 space-y-3">
                                <p className="text-xs text-muted-foreground">
                                    没有联系方式也可以继续写文章；仅在需要引流时使用，发布到媒体时仍会按媒体规则软化。
                                </p>
                                <div className="grid gap-3 sm:grid-cols-2">
                                    <Input value={contactForm.contact_phone} onChange={(e) => setContactForm(f => ({ ...f, contact_phone: e.target.value }))} placeholder="联系电话" />
                                    <Input value={contactForm.contact_wechat} onChange={(e) => setContactForm(f => ({ ...f, contact_wechat: e.target.value }))} placeholder="微信 / 企业微信" />
                                    <Input value={contactForm.contact_website} onChange={(e) => setContactForm(f => ({ ...f, contact_website: e.target.value }))} placeholder="官网" />
                                    <Input value={contactForm.contact_address} onChange={(e) => setContactForm(f => ({ ...f, contact_address: e.target.value }))} placeholder="地址" />
                                </div>
                                <Button variant="outline" onClick={saveContactProfile} disabled={contactSaving}>
                                    {contactSaving ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Check className="h-4 w-4 mr-1" />}
                                    保存可选联系方式
                                </Button>
                            </div>
                        </details>

                        <section className="space-y-3">
                            <div className="flex items-center justify-between">
                                <h3 className="text-sm font-semibold text-foreground">图片素材</h3>
                                <Badge variant="outline">{brandImageAssets.length} 张</Badge>
                            </div>
                            <BrandImageGallery brandId={brandId} embedded onAssetsChange={setBrandImageAssets} />
                        </section>

                        <section className="space-y-3">
                            <h3 className="text-sm font-semibold text-foreground">确认链接</h3>
                            <div className="rounded-lg border border-border/50 bg-card p-3">
                                <div className="flex flex-wrap gap-2">
                                    <Button variant="outline" onClick={generateKnowledgeLink} disabled={generatingKnowledgeLink}>
                                        {generatingKnowledgeLink ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Link2 className="h-4 w-4 mr-1" />}
                                        生成确认链接
                                    </Button>
                                    <Button variant="outline" onClick={copyKnowledgeLink} disabled={!knowledgeLink}>
                                        <Copy className="h-4 w-4 mr-1" />
                                        复制链接
                                    </Button>
                                </div>
                                {knowledgeLink && <p className="mt-2 truncate text-xs text-muted-foreground">{knowledgeLink}</p>}
                            </div>
                        </section>
                    </div>

                    <div className="flex items-center justify-between gap-3 border-t border-border p-4 text-xs text-muted-foreground">
                        <span>保存后不打断当前写作流程,可继续选择标题。</span>
                        <Button onClick={() => setMaterialsDrawerOpen(false)}>返回写作</Button>
                    </div>
                </aside>
            </div>
        );
    };

    const runDeliveryNextAction = () => {
        const kind = deliverySummary?.next_action?.kind;
        if (kind === 'generate_titles') {
            void generateTitles();
            return;
        }
        if (kind === 'open_publish_center' && selectedProject) {
            navigate(`/publish?quote_id=${selectedProject.id}`);
            return;
        }
        if (kind === 'resolve_blockers') setShowAdvancedWritingOptions(true);
        document.getElementById('writing-project-details')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };

    // 渲染标题工作台
    const renderWorkbench = () => {
        if (!selectedProject) return null;

        // 按关键词分组选题
        const topicsByKeyword: Record<number, Topic[]> = {};
        topics.forEach(t => {
            if (!topicsByKeyword[t.keyword_id]) {
                topicsByKeyword[t.keyword_id] = [];
            }
            topicsByKeyword[t.keyword_id].push(t);
        });
        const writingCount = topics.filter(t => t.status === 'writing').length;
        const completedCount = topics.filter(t => t.status === 'completed').length;
        const flowSteps = [
            { label: "报价进入", meta: `${keywords.length}词`, state: "done" },
            {
                label: "补全资料",
                meta: knowledgeStatus?.can_start_writing ? "可写作" : "待补充",
                state: knowledgeStatus?.can_start_writing ? "done" : "active",
            },
            {
                label: "生成标题",
                meta: topics.length > 0 ? `${topics.length}题` : "待生成",
                state: topics.length > 0 ? "done" : "active",
            },
            {
                label: "选择写作",
                meta: writingCount > 0 ? `${writingCount}篇写作中` : selectedWritableCount > 0 ? `${selectedWritableCount}篇已选` : "待选择",
                state: completedCount > 0 ? "done" : (writingCount > 0 || selectedWritableCount > 0) ? "active" : "todo",
            },
            {
                label: "完成核查",
                meta: completedCount > 0 ? `${completedCount}篇完成` : "待完成",
                state: completedCount > 0 ? "active" : "todo",
            },
        ];

        return (
            <div className="p-4 md:p-6 space-y-5">
                {/* 返回按钮 + 项目信息 */}
                <div className="space-y-3">
                    <div className="flex items-center gap-3 sm:gap-4">
                        <Button variant="ghost" size="sm" onClick={backToList} className="shrink-0">
                            <ArrowLeft className="h-4 w-4 mr-1" />
                            <span className="hidden sm:inline">返回列表</span>
                            <span className="sm:hidden">返回</span>
                        </Button>
                        <div className="flex-1 min-w-0">
                            <h2 className="text-lg sm:text-2xl font-bold truncate">{selectedProject.brand_name}</h2>
                            <p className="text-xs sm:text-sm text-slate-500 truncate">
                                {selectedProject.industry} | {keywords.length}个关键词 | {topics.length}个选题
                            </p>
                        </div>
                    </div>
                    {/* [P2-2/R2-5 2026-08-15] 下一步卡:CTA 是**真动作按钮**(点击导航/开面板/刷新),
                        不再是带箭头的死 <span>(R2 §0② 反例)。降级出口 = 卡片本体只陈述,
                        用户可无视按钮走任何既有入口。 */}
                    {nextStep?.available && nextStep.next_step && (
                        <div className="flex items-start gap-2 rounded-xl border border-emerald-500/25 bg-emerald-500/5 px-3 py-2 text-sm" aria-label="下一步建议" data-testid="next-step-card">
                            <span className="shrink-0 rounded bg-emerald-600/90 px-1.5 py-0.5 text-[11px] font-medium text-white">下一步</span>
                            <span className="min-w-0 flex-1">
                                <span className="font-medium">{nextStep.next_step.title}</span>
                                <span className="text-muted-foreground"> · {nextStep.next_step.why}</span>
                            </span>
                            <button
                                type="button"
                                data-testid="next-step-cta"
                                className="shrink-0 rounded-md border border-emerald-600/40 px-2 py-0.5 text-emerald-700 hover:bg-emerald-600/10 dark:text-emerald-300"
                                onClick={() => {
                                    const stage = nextStep.next_step?.stage;
                                    if (stage === 'publish') {
                                        navigate(`/publish?quote_id=${selectedProject?.id || ''}`);
                                    } else if (stage === 'review') {
                                        setBatchReviewPanelOpen(true);
                                    } else if (stage === 'monitor' || stage === 'observe') {
                                        navigate('/monitoring');
                                    } else {
                                        // keywords/titles/write/retry/writing:动作都在本页工作区 ——
                                        // 滚动到工作区并刷新状态(真动作,非装饰)。
                                        document.getElementById('writing-workspace')?.scrollIntoView({ behavior: 'smooth' });
                                        if (selectedProject) { void loadProjectDetail(selectedProject.id); }
                                    }
                                }}
                            >
                                {nextStep.next_step.cta} →
                            </button>
                        </div>
                    )}
                    {deliverySummary?.simple_ui_enabled && deliverySummary.available && deliverySummary.delivery && (
                        <section className="rounded-2xl border border-blue-500/20 bg-gradient-to-br from-blue-500/10 via-background to-emerald-500/5 p-4 sm:p-5" aria-label="项目交付摘要">
                            <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                                <div className="min-w-0 flex-1">
                                    <p className="text-xs font-medium text-blue-700 dark:text-blue-300">当前项目</p>
                                    <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-4">
                                        {[
                                            ['合同交付', deliverySummary.delivery.contract_total],
                                            ['已经完成', deliverySummary.delivery.completed],
                                            ['还要处理', deliverySummary.delivery.pending],
                                            ['需要解决', deliverySummary.delivery.blocked],
                                        ].map(([label, value]) => (
                                            <div key={String(label)} className="rounded-xl border border-border/60 bg-background/80 px-3 py-3">
                                                <p className="text-xs text-muted-foreground">{label}</p>
                                                <p className="mt-1 text-xl font-semibold text-foreground">{value}<span className="ml-1 text-xs font-normal text-muted-foreground">篇</span></p>
                                            </div>
                                        ))}
                                    </div>
                                    <div className="mt-3 rounded-xl bg-background/65 px-3 py-2.5 text-sm text-muted-foreground">
                                        {deliverySummary.health?.status === 'available' ? (
                                            <span>
                                                已记录证据链 {deliverySummary.health.articles_with_evidence_record} 篇
                                                {deliverySummary.health.articles_needing_review > 0 ? ` · ${deliverySummary.health.articles_needing_review} 篇需要核对` : ' · 暂无待核对文章'}
                                            </span>
                                        ) : (
                                            <span>{deliverySummary.health?.message || '尚无足够信息，生成正文后再显示健康情况。'}</span>
                                        )}
                                    </div>
                                    {deliverySummary.delivery.blocked > 0 && deliverySummary.blockers?.[0] && (
                                        <div className="mt-2 rounded-xl border border-amber-500/20 bg-amber-500/10 px-3 py-2.5 text-sm">
                                            <p className="font-medium text-amber-800 dark:text-amber-200">
                                                {deliverySummary.blockers[0].message}
                                            </p>
                                            <p className="mt-1 text-xs text-amber-700/80 dark:text-amber-200/75">
                                                下一步：{deliverySummary.blockers[0].next_action}
                                            </p>
                                        </div>
                                    )}
                                </div>
                                <div className="flex w-full flex-col gap-2 lg:w-56">
                                    <Button className="h-11 w-full" onClick={runDeliveryNextAction}>
                                        {deliverySummary.next_action?.label || '继续处理'}
                                        {/*
                                          * [WO_218-a1] 🔴 这颗按钮**最容易被漏掉**:它的文案是后端给的
                                          *    (`next_action.label`),所以通篇 grep「生成标题」找不到它,
                                          *    而 `kind === 'generate_titles'` 时它调的就是 `generateTitles()`
                                          *    (见 6141)—— 一次实打实的 N×基价扣费。
                                          * ⇒ 只有 kind 真是 generate_titles 时才挂价;
                                          *    别的 kind(去发布中心 / 解阻塞)不花钱,挂了就是报假收费。
                                          */}
                                        {deliverySummary.next_action?.kind === 'generate_titles'
                                            && chargeText(topicGenCharge) && (
                                            <span data-testid="topic-gen-charge" className="ml-1.5 text-xs opacity-90">
                                                · {chargeText(topicGenCharge)}
                                            </span>
                                        )}
                                    </Button>
                                    <button
                                        type="button"
                                        className="text-xs text-muted-foreground hover:text-foreground"
                                        onClick={() => setShowAdvancedWritingOptions(value => !value)}
                                    >
                                        {showAdvancedWritingOptions ? '收起高级设置' : '需要时打开高级设置'}
                                    </button>
                                </div>
                            </div>
                        </section>
                    )}
                    {/* 工具栏 - 移动端换行 */}
                    <div className={`${deliverySummary?.simple_ui_enabled && !showAdvancedWritingOptions ? 'hidden' : 'flex'} flex-wrap items-center gap-2`}>
                        {/* 刷新缓存按钮 */}
                        <Button
                            variant="outline"
                            size="sm"
                            aria-label="刷新缓存"
                            onClick={async () => {
                                try {
                                    await authFetch(`/api/writing/projects/${selectedProject.id}/clear-cache`, { method: 'POST' });
                                    toast.success('缓存已清除，下次生成将重新获取数据');
                                } catch (e) {
                                    console.error('清除缓存失败:', e);
                                }
                            }}
                        >
                            <RefreshCw className="h-4 w-4 sm:mr-1" />
                            <span className="hidden sm:inline">刷新缓存</span>
                        </Button>
                        {isAdmin && (
                            <Button
                                variant="outline"
                                size="sm"
                                aria-label="写作设置"
                                onClick={() => setShowWritingSettings(true)}
                            >
                                <Settings className="h-4 w-4 sm:mr-1" />
                                <span className="hidden sm:inline">写作设置</span>
                            </Button>
                        )}
                        {/* [CTO-15.23 2026-05-05] 添加关键词 · 漏词时不用重新走流程 */}
                        <Button
                            variant="outline"
                            size="sm"
                            aria-label="添加关键词"
                            onClick={() => setShowAddKeyword(true)}
                        >
                            <Plus className="h-4 w-4 sm:mr-1" />
                            <span className="hidden sm:inline">添加关键词</span>
                        </Button>
                        {/* ═══ [#189] 「同行对比」三档 ═══════════════════════════
                            Owner 09-13:「这三个标签不是找同行的吗?现在的表达是不是需要换一下?」
                            原来是「已核验 / 待核验 / 仅写标准」—— 写的是**证据状态**,没有主语,
                            用户看不出这是在决定「文章里要不要点名同行」。
                            🔴 三件事一起改:加主语(前置「同行对比」)、去告警色
                            (「不点名」是正当选择,原来配红点红字读起来像出错)、
                            动作与状态分开(检索独立成按钮,见下面那颗)。 */}
                        {competitorMode === 'loading' ? (
                            <Button variant="outline" size="sm" disabled data-testid="peer-mode-loading">
                                <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                {competitorLoadingLabel}
                            </Button>
                        ) : (
                            <>
                            <span className="text-xs text-muted-foreground shrink-0" data-testid="peer-group-label">
                                {PEER_GROUP_LABEL}
                            </span>
                            <HelpHint title="同行对比的三种做法" side="bottom" className="mr-0.5">
                                <b>点名对比</b>:文章里写出同行名字,只用已核实的那几家。
                                <br /><b>暂不点名</b>:名字还在核实,先不写进正文。
                                <br /><b>不点名</b>:不出现同行名字,只写该怎么挑 —— 这是个正常选择,不是降级。
                            </HelpHint>
                            <div className="flex items-center rounded-lg border overflow-hidden text-xs h-8"
                                data-testid="peer-mode-group">
                                {peerModeOptions(peerCountsNow).map((opt, idx) => {
                                    const active = competitorMode === opt.value;
                                    const chip = (
                                        <button
                                            key={opt.value}
                                            type="button"
                                            data-testid={`peer-mode-${opt.value}`}
                                            aria-pressed={active}
                                            disabled={opt.disabled}
                                            /* 🔴 切档**不再触发联网检索**(原来点「已核验」会去搜)。
                                               一个按钮两种含义,用户以为自己只是在切显示。 */
                                            onClick={() => switchCompetitorMode(opt.value)}
                                            title={opt.disabled ? opt.disabledReason : ''}
                                            className={`px-2 sm:px-3 h-full flex items-center transition-colors ${
                                                idx === 1 ? 'border-x ' : ''
                                            }${opt.disabled
                                                ? 'cursor-not-allowed bg-card text-muted-foreground/50'
                                                : active
                                                    /* 🔴 选中用主色,未选中中性 —— 三档都是合法选择,无红黄。 */
                                                    ? 'bg-primary text-primary-foreground font-medium'
                                                    : 'bg-card text-muted-foreground hover:bg-primary/10'}`}
                                        >
                                            {opt.label}
                                        </button>
                                    );
                                    if (opt.value !== 'real') return chip;
                                    return (
                                        <FeatureTooltip
                                            key={opt.value}
                                            featureId="sandbox_writing_explain_modes"
                                            stepId="first_article_publish"
                                            title="第一步: 决定文章里要不要点名同行"
                                            content="先联网找同行并核实名字来源。核实过的才能在正文里点名;没核实的先不写进正文。"
                                            side="bottom"
                                            disabled={!isSandboxActive() || tutorialStage !== 'step3-explain-modes'}
                                        >
                                            {chip}
                                        </FeatureTooltip>
                                    );
                                })}
                            </div>
                            {/* 🔴 检索**独立成按钮**:原来它藏在「已核验」chip 里,
                                那颗按钮既是状态又是动作。现在这是唯一触发联网检索的地方。 */}
                            <Button
                                variant="outline"
                                size="sm"
                                data-testid="peer-research-btn"
                                onClick={() => { void researchAndVerifyPeers(); }}
                            >
                                <RefreshCw className="h-4 w-4 sm:mr-1" />
                                <span className="hidden sm:inline">{PEER_RESEARCH_LABEL}</span>
                            </Button>
                            {/* 不可用时的原因:同屏给出,不只是置灰 */}
                            {peerModeOptions(peerCountsNow).some(o => o.disabled) && (
                                <span className="text-xs text-muted-foreground" data-testid="peer-mode-disabled-reason">
                                    {peerModeOptions(peerCountsNow).find(o => o.disabled)?.disabledReason}
                                </span>
                            )}
                            </>
                        )}
                        {activeLLM && (
                            <span className="text-xs text-muted-foreground bg-gray-100 px-2 py-1 rounded">
                                模型: {activeLLM.provider}/{activeLLM.model}
                            </span>
                        )}
                        {/* [CTO-15.23 2026-05-08 死锁修] 显示条件加 "任意 kw 没 topic"
                            新:有"待生成"kw 时也显示 · "为新词生成标题"走单 kw endpoint(不擦已有 draft)
                            沙盒: 把 spotlight 套在按钮上(step3-gen-titles) */}
                        {(() => {
                            // [WO_317 第三笔] 只看要出题的词(plannedPosts > 0):0 槽的词是有意不出题,不算「没标题的新词」。
                            //   原来它恒让这里为真 ⇒ 有核心 0 槽词的单永远只给「为新词生成标题」、整表按钮不出现,
                            //   且按钮写「将扣 80」,点了冻 80、出 0 条、退 80。
                            const hasAnyKwWithoutTopics = keywords.some(kw => plannedPosts(kw) > 0 && !topics.some(t => t.keyword_id === kw.id));
                            const sandboxTitleTutorialActive = isSandboxActive() && tutorialStage === 'step3-gen-titles';
                            if (!shouldRenderTitleGenerationAction(
                                selectedProject.writing_status,
                                hasAnyKwWithoutTopics,
                                tutorialStage,
                                isSandboxActive(),
                            )) return null;
                            const isNewKwOnly = !sandboxTitleTutorialActive
                                && selectedProject.writing_status !== 'pending'
                                && hasAnyKwWithoutTopics;
                            /*
                             * [WO_243 乙] 🔴 **这颗按钮的两面现在都收费**,只是份数不同:
                             *   · `batch-all`    整张表出题
                             *   · `new-keywords` 只给**没有选题行**的新词出题(带 per_keyword_plan 子集)
                             * 免费的那一面(`recover-missing`)**不在这颗按钮上** ——
                             * 它是"某批次里缺题的那一条"的补救,入口在选题行上。
                             *
                             * 🔴 2026-09-19 之前这里是反的:`isNewKwOnly` 那一面被当成免费。
                             *    WO_241 丙 裁定后新词 = 全新生产 = 从没付过 ⇒ **要收费**。
                             *    极性反转过的布尔不留,改用具名面孔(见 topicGenCharge.ts)。
                             */
                            const titleGenFace: TitleGenFace = isNewKwOnly ? 'new-keywords' : 'batch-all';
                            /* 🔴 面孔决定读哪一份价 —— 两份价由后端同一个 `charge_card()` 出,
                               前端只负责挑对那一份,不做任何算术。 */
                            const titleGenCharge = isNewKwOnly ? topicGenChargeNewOnly : topicGenCharge;
                            return (
                                <FeatureTooltip
                                    featureId="sandbox_writing_generate_titles"
                                    stepId="first_article_publish"
                                    title="第五步: AI 批量生成标题"
                                    content="基于 3 个关键词, AI 会扩出 6 个候选标题 (每词 2 篇) · 沙盒 2 秒模拟"
                                    side="bottom"
                                    disabled={!isSandboxActive() || tutorialStage !== 'step3-gen-titles'}
                                    targetSelector='[data-sandbox-coach-anchor="writing-generate-titles"]'
                                >
                                    <Button
                                        data-sandbox-coach-anchor="writing-generate-titles"
                                        data-testid="sandbox-generate-titles"
                                        onClick={() => {
                                            /*
                                             * [WO_243 乙] 🔴 **新词面改走批量端点带子集**。
                                             *    原来它调 `generateTopicsForMissingKws()` ——
                                             *    那个函数把「没有选题行的词」(= 新词)发给**补救端点**,
                                             *    而补救端点会 403 它们(那些词不属于任何已付批次)。
                                             *    契约里那句「前端不要把补救面当成免费的生成标题」
                                             *    描述的就是这段现存代码。
                                             */
                                            if (isNewKwOnly) {
                                                void generateTitles(keywordIdsWithoutTopics(
                                                    keywords.filter((kw) => plannedPosts(kw) > 0).map((kw) => kw.id), topics));
                                                return;
                                            }
                                            void generateTitles();
                                        }}
                                        disabled={generating}
                                        size="sm"
                                    >
                                        {generating ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Play className="h-4 w-4 mr-1" />}
                                        {isNewKwOnly ? '为新词生成标题' : '批量生成标题'}
                                        {/*
                                          * [WO_218-a1] 🔴 **同一颗按钮,两个面孔**:
                                          * `isNewKwOnly` 为真时它走逐词 `generate-topic`,
                                          * 那条路整段没有计费调用 —— 一分不收。
                                          * 所以价的显不显示由 `chargeText` 按这一面决定,
                                          * 而不是"按钮上有价就一直挂着"。
                                          * 把价显示在免费那一面 = **对客户报一个不存在的收费**。
                                          */}
                                        {/* 🔴 新词面读**新词面那一份**价(`charge_new_keywords_only`),
                                            不是整表价 —— 拿整表价显示就是说 10 份而实扣 2 份。 */}
                                        {chargeText(titleGenCharge, { face: titleGenFace }) && (
                                            <span data-testid="topic-gen-charge" className="ml-1.5 text-xs opacity-90">
                                                · {chargeText(titleGenCharge, { face: titleGenFace })}
                                            </span>
                                        )}
                                    </Button>
                                </FeatureTooltip>
                            );
                        })()}
                    </div>
                    {/* [P4 缺口作战计划 2026-08-08 · B6] 从交付计划「去写这篇」带过来的冻结规格。
                        🔴 只是一张说明卡:预填与定位,不自动生成、不扣费、不发布。 */}
                    {gapPlanItem && (
                        <div
                            className="mt-3 rounded-md border border-sky-500/30 bg-sky-500/10 px-3 py-2"
                            data-help-target="gap-plan-prefill"
                            aria-live="polite"
                        >
                            <p className="text-xs font-medium text-sky-700 dark:text-sky-300">
                                来自交付计划：{gapPlanItem.contentForm || '这一篇'}
                                {gapPlanItem.targetDisplayName ? ` → 发到${gapPlanItem.targetDisplayName}` : ''}
                            </p>
                            {gapPlanItem.targetQuestion && (
                                <p className="mt-1 text-xs text-muted-foreground">
                                    目标问题：{gapPlanItem.targetQuestion}
                                </p>
                            )}
                            <p className="mt-1 text-[11px] text-muted-foreground">
                                已为你定位到这个词包；内容仍由你确认后再生成，系统不会自动开始或扣费。
                            </p>
                        </div>
                    )}
                    {gapPlanNotice && (
                        <div className="mt-3 rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2">
                            <p className="text-xs text-amber-700 dark:text-amber-300">{gapPlanNotice}</p>
                        </div>
                    )}

                    {/*
                        🔴 [#185 · Owner 09-13 禁猜规则第 3 条] 这个入口原来叫
                        「⚙️ 高级:自定义文章文体配比」,而且**默认收起**
                        (`simple_ui_enabled` 时要先点「需要时打开高级设置」才看得见)。
                        两处都违反"可视化优先 · 禁猜":
                          · 「文体配比」是工程词,用户要找的是"这批文章往哪个方向写";
                          · 藏在高级里 = 大多数人根本不知道能选方向。
                        改成显眼的「文章方向」,与快捷模式同一处,**不再默认收起**。
                    */}
                    {selectedProject && (
                        <div className="mt-2 text-right flex items-center justify-end gap-3">
                            {/* v2.10.4 Codex 四审 P1:已启用自定义配比时显式提示(优先级可见) */}
                            {pendingDistribution !== null && (
                                <span className="text-xs text-green-600 dark:text-green-400"
                                    data-testid="writing-direction-custom-on">
                                    ✓ 已按自定义方向配比
                                </span>
                            )}
                            <button
                                type="button"
                                onClick={() => setDistributionDialogOpen(true)}
                                className="text-sm text-foreground underline underline-offset-2 hover:text-primary transition-colors"
                                title="这批文章往哪个方向写、各写几篇"
                                data-testid="writing-direction-entry"
                            >
                                文章方向
                            </button>
                        </div>
                    )}
                    <div id="writing-workspace" className={`${deliverySummary?.simple_ui_enabled && !showAdvancedWritingOptions ? 'hidden' : 'grid'} gap-2 rounded-xl border border-border/70 bg-card/60 p-3 sm:grid-cols-5`}>
                        {flowSteps.map((step, idx) => (
                            <div
                                key={step.label}
                                className={`min-w-0 rounded-lg border px-3 py-2 ${
                                    step.state === "done"
                                        ? "border-emerald-500/25 bg-emerald-500/10"
                                        : step.state === "active"
                                            ? "border-blue-500/25 bg-blue-500/10"
                                            : "border-border/60 bg-background/40"
                                }`}
                            >
                                <div className="flex items-center gap-2">
                                    <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${
                                        step.state === "done"
                                            ? "bg-emerald-500 text-white"
                                            : step.state === "active"
                                                ? "bg-blue-500 text-white"
                                                : "bg-muted text-muted-foreground"
                                    }`}>
                                        {idx + 1}
                                    </span>
                                    <span className="truncate text-xs font-medium text-foreground">{step.label}</span>
                                </div>
                                <div className="mt-1 truncate pl-7 text-[11px] text-muted-foreground">{step.meta}</div>
                            </div>
                        ))}
                    </div>
                </div>

                <div id="writing-project-details" className="grid items-start gap-5 xl:grid-cols-[minmax(0,1fr)_340px] 2xl:grid-cols-[minmax(0,1fr)_380px]">
                    <div className="min-w-0 space-y-4">

                {/* 同行状态展示条 */}
                {(competitorMode === 'real' || competitorMode === 'semi') && competitors.length > 0 && (() => {
                    const activeCount = competitors.filter(c => !c.excluded).length;
                    const excludedCount = competitors.filter(c => c.excluded).length;
                    const isReal = competitorMode === 'real';
                    const colorScheme = isReal
                        ? { bg: 'bg-green-50', border: 'border-green-200', hoverBg: 'hover:bg-green-100/50', headerText: 'text-green-800', dot: 'bg-green-500', nameList: 'text-green-600', btnText: 'text-green-600 hover:text-green-800', divider: 'border-green-200', rowBorder: 'border-green-100' }
                        : { bg: 'bg-yellow-50', border: 'border-yellow-200', hoverBg: 'hover:bg-yellow-100/50', headerText: 'text-yellow-800', dot: 'bg-yellow-500', nameList: 'text-yellow-600', btnText: 'text-yellow-600 hover:text-yellow-800', divider: 'border-yellow-200', rowBorder: 'border-yellow-100' };
                    return (
                    <FeatureTooltip
                        featureId="sandbox_writing_show_competitors"
                        stepId="first_article_publish"
                        title="第二步：查看已核实的 5 家同行"
                        content="联网检索得到的候选品牌。名称核验通过后才可进入比较候选；其能力事实仍需 Evidence Pack。待核验名称只用于继续搜证。"
                        side="bottom"
                        disabled={!isSandboxActive() || tutorialStage !== 'step3-show-competitors'}
                        nextLabel="知道了, 看下一步 →"
                        onNext={() => setTutorialStage('step3-upload-kb')}
                        wrapClassName="relative block"
                    >
                    <div className={`${colorScheme.bg} ${colorScheme.border} border rounded-lg text-sm overflow-hidden`}>
                        <div className={`flex items-center gap-2 px-4 py-2 cursor-pointer ${colorScheme.hoverBg} transition-colors`}
                             onClick={() => setCompetitorPanelOpen(!competitorPanelOpen)}>
                            {competitorPanelOpen ? <ChevronDown className={`h-4 w-4 ${colorScheme.nameList} shrink-0`} /> : <ChevronRight className={`h-4 w-4 ${colorScheme.nameList} shrink-0`} />}
                            <span className={`inline-block w-2 h-2 rounded-full ${colorScheme.dot} shrink-0`} />
                            <span className={`${colorScheme.headerText} font-medium shrink-0`}>
                                {peerListTitle(isReal ? 'real' : 'semi', activeCount)}{excludedCount > 0 ? `(${excludedCount} 家已排除)` : ''}
                            </span>
                            <span className={`${colorScheme.nameList} truncate flex-1 text-xs`}>
                                {competitors.filter(c => !c.excluded).map(c => c.name).join('、')}
                            </span>
                            {/* [Review-CTO 2026-07-27 · Owner] 待核验态原来没有重跑入口——
                                挖错方向(驰鲸挖出一堆东莞工厂)时用户没法重来。两态都给;
                                后端是整表替换,重跑即覆盖旧候选。 */}
                            <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); researchCompetitors(); }} className={`h-6 px-2 ${colorScheme.btnText} shrink-0`}>
                                <RefreshCw className="h-3 w-3 mr-1" />{isReal ? '重新搜索' : '重新找同行'}
                            </Button>
                        </div>
                        {competitorPanelOpen && (
                            <div className={`${colorScheme.divider} border-t max-h-[360px] overflow-y-auto`}>
                                <table className="w-full text-xs">
                                    <thead className="sticky top-0 bg-card/80 backdrop-blur-xs">
                                        <tr className="text-slate-400">
                                            <th className="w-8 py-1.5 pl-4"></th>
                                            <th className="w-8 py-1.5 text-center">启用</th>
                                            <th className="py-1.5 pl-2 text-left">同行名称</th>
                                            <th className="py-1.5 pl-2 text-left">置信度</th>
                                            <th className="py-1.5 pl-2 text-left hidden md:table-cell">简介 / 画像</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {competitors.map((c, i) => (
                                            <tr key={i} className={`${colorScheme.rowBorder} border-b last:border-0 ${c.excluded ? 'opacity-40' : ''} hover:bg-muted/40 transition-opacity`}>
                                                <td className="py-2 pl-4 text-slate-400 font-mono">{i + 1}</td>
                                                <td className="py-2 text-center">
                                                    <input
                                                        type="checkbox"
                                                        checked={!c.excluded}
                                                        onChange={() => toggleCompetitorExclude(c.name, !c.excluded)}
                                                        className="h-3.5 w-3.5 rounded border-slate-300 text-green-600 cursor-pointer dark:text-green-300"
                                                    />
                                                </td>
                                                <td className="py-2 pl-2">
                                                    <span className={`font-medium ${c.excluded ? 'line-through text-slate-400' : 'text-slate-800'}`}>{c.name}</span>
                                                </td>
                                                <td className="py-2 pl-2">
                                                    <div className="flex items-center gap-1">
                                                        {c.confidence === 'high'
                                                            ? <span className="px-1.5 py-0.5 rounded bg-green-100 text-green-700 dark:bg-green-500/20 dark:text-green-300">高</span>
                                                            : c.confidence === 'manual'
                                                            ? <span className="px-1.5 py-0.5 rounded bg-blue-100 text-blue-700 dark:bg-blue-500/20 dark:text-blue-300">手动</span>
                                                            : <span className="px-1.5 py-0.5 rounded bg-yellow-100 text-yellow-700 dark:bg-yellow-500/20 dark:text-yellow-300">中</span>
                                                        }
                                                        {c.source_count && <span className="text-slate-400">{c.source_count}源</span>}
                                                    </div>
                                                </td>
                                                <td className="py-2 pl-2 hidden md:table-cell text-slate-500 max-w-[400px]">
                                                    <div className="line-clamp-2">
                                                        {c.profile || c.desc || (c.projects?.length ? `楼盘: ${c.projects.join('、')}` : '—')}
                                                    </div>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                                {/* 手动添加同行 */}
                                <div className={`${colorScheme.divider} border-t px-4 py-2.5 flex items-center gap-2`}>
                                    <Plus className="h-3.5 w-3.5 text-slate-400 shrink-0" />
                                    <Input
                                        value={addCompetitorName}
                                        onChange={(e) => setAddCompetitorName(e.target.value)}
                                        onKeyDown={(e) => e.key === 'Enter' && !addingCompetitor && addCompetitorName.trim() && addCompetitor()}
                                        placeholder={ADD_PEER_PLACEHOLDER}
                                        className="h-7 text-xs flex-1"
                                        disabled={addingCompetitor}
                                    />
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={addCompetitor}
                                        disabled={addingCompetitor || !addCompetitorName.trim()}
                                        className="h-7 px-2.5 text-xs"
                                    >
                                        {addingCompetitor
                                            ? <><Loader2 className="h-3 w-3 animate-spin mr-1" />搜索中...</>
                                            : <><Search className="h-3 w-3 mr-1" />搜索并添加</>
                                        }
                                    </Button>
                                </div>
                            </div>
                        )}
                    </div>
                    </FeatureTooltip>
                    );
                })()}
                {competitorMode === 'evidence_only' && (
                    <>
                    {/*
                        🔴 [#189] 这一块原来是**红底红字红点**。它描述的是
                        「不点名 · 只写怎么选」这个**正当选择**的后果,不是错误 ——
                        用告警色劝退一个合法选项等于误导(Owner 截图里就是这一格)。
                        改中性色;检索按钮走那颗独立按钮的同一个动作。
                    */}
                    <div className="flex flex-col gap-1.5 rounded-lg border border-border bg-muted/40 px-3 py-2 text-xs sm:px-4 sm:text-sm"
                        data-testid="peer-evidence-only-note">
                        <div className="flex flex-wrap items-center gap-2">
                            <span className="text-foreground">{EVIDENCE_ONLY_NOTE}</span>
                            <Button variant="outline" size="sm"
                                data-testid="peer-research-btn-inline"
                                onClick={() => { void researchAndVerifyPeers(); }}
                                className="h-6 shrink-0 px-2 text-xs sm:ml-auto sm:text-sm">
                                {PEER_RESEARCH_LABEL}
                            </Button>
                        </div>
                        <div className="pl-0 text-[11px] text-muted-foreground sm:text-xs">
                            也可以在下方<span className="font-medium">手动加一个同行</span>,系统仍会联网核实之后才用于正文
                        </div>
                    </div>
                    {/* Phase 06.1.2 · 夜间模式 token */}
                    <div className="flex items-center gap-2 px-4 py-2 bg-muted/40 border border-border rounded-lg text-sm">
                        <Plus className="h-3.5 w-3.5 text-muted-foreground shrink-0" />
                        <Input
                            value={addCompetitorName}
                            onChange={(e) => setAddCompetitorName(e.target.value)}
                            onKeyDown={(e) => e.key === 'Enter' && !addingCompetitor && addCompetitorName.trim() && addCompetitor()}
                            placeholder="加一个同行,系统会联网核实"
                            className="h-7 text-xs flex-1"
                            disabled={addingCompetitor}
                        />
                        <Button
                            variant="outline"
                            size="sm"
                            onClick={addCompetitor}
                            disabled={addingCompetitor || !addCompetitorName.trim()}
                            className="h-7 px-2.5 text-xs"
                        >
                            {addingCompetitor
                                ? <><Loader2 className="h-3 w-3 animate-spin mr-1" />搜索中...</>
                                : <><Search className="h-3 w-3 mr-1" />搜索并添加</>
                            }
                        </Button>
                    </div>
                    </>
                )}

                {/* 标题列表 */}
                {topics.length === 0 && keywords.length > 0 ? (
                    <Card>
                        <CardContent className="py-10">
                            <div className="space-y-5">
                                <div className="text-center">
                                    {/* Phase 06.1.2 (CTO-15.23 2026-05-04) · 夜间模式 · slate 硬编码 → token */}
                                    <p className="text-base sm:text-lg font-semibold text-foreground">报价关键词已进入写作大厅</p>
                                    <p className="mt-1 text-sm text-muted-foreground">
                                        {/* 🔴 [#225 a1 下半] 合计**由服务端给**(`total_planned_posts_default`),
                                            前端不 reduce。C 侧注释写得很清楚:「只有一条取整路径 ——
                                            逐词先取整,再相加,合计由服务端给出」;前端自己加一遍,
                                            取整位置一变就会和别处差 1,而没有任何东西会报错。
                                            服务端没给时回落到逐行相加 —— 加的是**已经取整过**的每行值,
                                            不引入新的取整,等于老行为。 */}
                                        共 {keywords.length} 个词，预计 {plannedTotal(plannedTotalServer, keywords)} 篇文章。下一步生成标题后即可勾选写作。
                                    </p>
                                </div>
                                <div className="rounded-lg border border-border bg-muted/30 overflow-hidden">
                                    {keywords.map((kw) => (
                                        <div key={kw.id} className="flex flex-wrap items-center gap-2 border-b border-border/60 last:border-b-0 px-3 py-2 text-sm hover:bg-muted/50 transition-colors">
                                            <span className="font-medium text-foreground flex-1 min-w-[180px]">{kw.keyword}</span>
                                            {/* [#225 a1] 显示的是**按交付口径要做几篇**(服务端给),不是合同授权的槽数;
                                                两者不同时旁边说明「授权 N 槽」。 */}
                                            <Badge variant="secondary" className="text-xs">需{plannedPosts(kw)}篇</Badge>
                                            {slotsNote(kw) && (
                                                <span className="text-[11px] text-muted-foreground">{slotsNote(kw)}</span>
                                            )}
                                            <span className="text-xs text-muted-foreground">¥{(kw.final_price || 0).toFixed(0)}</span>
                                        </div>
                                    ))}
                                </div>
                                <div className="flex justify-center">
                                    {/* 🔴 必须包一层:`onClick={generateTitles}` 会把**点击事件**
                                        当成 `onlyKeywordIds` 传进去(编译器逮住的)。 */}
                                    <Button onClick={() => { void generateTitles(); }} disabled={generating}>
                                        {generating ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Play className="h-4 w-4 mr-1" />}
                                        为这些词生成标题
                                        {/* [WO_218-a1] 这一处**一定**打 generate-titles(不是三元),
                                            所以不传 isNewKwOnly —— 默认按"会收费"那一面判。 */}
                                        {chargeText(topicGenCharge) && (
                                            <span data-testid="topic-gen-charge" className="ml-1.5 text-xs opacity-90">
                                                · {chargeText(topicGenCharge)}
                                            </span>
                                        )}
                                    </Button>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                ) : topics.length === 0 ? (
                    <Card>
                        <CardContent className="py-12 text-center">
                            <p className="text-foreground font-medium mb-1">这张报价单还没有可写作的核心词</p>
                            <p className="text-muted-foreground text-sm mb-4">请回到报价方案确认关键词，或刷新缓存后再查看。</p>
                            <Button variant="outline" onClick={() => selectedProject && loadProjectDetail(selectedProject.id)} disabled={generating}>
                                <RefreshCw className="h-4 w-4 mr-1" />
                                重新读取关键词
                            </Button>
                        </CardContent>
                    </Card>
                ) : (
                    <>
                        {/* 工作台内部Tab */}
                        <Tabs value={workbenchTab} onValueChange={(v) => setWorkbenchTab(v as 'pending' | 'writing' | 'completed' | 'optimize')} className="mb-4">
                            <div className="overflow-x-auto -mx-1 px-1">
                                <TabsList className="w-max sm:w-auto">
                                    <TabsTrigger value="pending" className="text-xs sm:text-sm">
                                        待写 ({topics.filter(t => t.status === 'pending' || t.status === 'draft' || t.status === 'failed' || !t.status).length})
                                    </TabsTrigger>
                                    <TabsTrigger value="writing" className="text-xs sm:text-sm">
                                        写作中 ({topics.filter(t => t.status === 'writing').length})
                                    </TabsTrigger>
                                    <TabsTrigger value="completed" className="text-xs sm:text-sm">
                                        已完成 ({topics.filter(t => t.status === 'completed').length})
                                    </TabsTrigger>
                                    <TabsTrigger value="optimize" className="text-xs sm:text-sm">
                                        优化 ({topics.filter(t => t.is_optimize).length})
                                    </TabsTrigger>
                                </TabsList>
                            </div>
                        </Tabs>

                        {/* 操作栏 - 仅在待写Tab显示 */}
                        {workbenchTab === 'pending' && (
                            <div className="flex flex-wrap items-center gap-2 p-2 sm:p-3 bg-muted/40 rounded-lg">
                                <Button variant="outline" size="sm" onClick={toggleAllKeywords}>
                                    {expandedKeywords.size === keywords.length ? "全部折叠" : "全部展开"}
                                </Button>
                                <Button variant="outline" size="sm" onClick={toggleSelectAll}>
                                    {topics.filter(t => t.status === 'pending' || t.status === 'draft' || !t.status).every(t => selectedTopics.has(t.id))
                                        ? "取消全选" : "全选标题"}
                                </Button>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={regenerateSelected}
                                    disabled={selectedTopics.size === 0 || regenerating}
                                    title={selectedTopics.size === 0 ? "请先勾选要重写的标题" : "对选中标题重新生成"}
                                >
                                    <RefreshCw className={`h-4 w-4 mr-1 ${regenerating ? "animate-spin" : ""}`} />
                                    <span className="hidden sm:inline">
                                        {regenerating
                                            ? "生成中..."
                                            : selectedTopics.size === 0
                                                ? "重新生成"
                                                : `重新生成 (${selectedTopics.size})`}
                                    </span>
                                    <span className="sm:hidden">
                                        {regenerating
                                            ? "..."
                                            : selectedTopics.size === 0
                                                ? "重写"
                                                : `重生(${selectedTopics.size})`}
                                    </span>
                                </Button>
                                {/* 开始写作按钮 · 2026-06-03 全站静默扣费去价格角标 · 仅留动作 */}
                                <FeatureTooltip
                                    featureId="sandbox_writing_start_articles"
                                    stepId="first_article_publish"
                                    title="第八步: 勾选标题 + 点开始写作"
                                    content="教程已自动勾选 6 个标题 · 点击后 AI 并行生成 6 篇文章, 约 8 秒"
                                    side="bottom"
                                    disabled={!isSandboxActive() || tutorialStage !== 'step3-start-writing'}
                                >
                                    <Button
                                        size="sm"
                                        onClick={() => { void startWriting(); }}
                                        disabled={selectedWritableCount === 0 || writingInProgress || !pricingReady}
                                        className="bg-green-600 hover:bg-green-700"
                                    >
                                        {writingInProgress ? (
                                            <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                        ) : (
                                            <Sparkles className="h-4 w-4 mr-1" />
                                        )}
                                        <span className="inline-flex items-center gap-1.5">
                                            开始写作 ({selectedWritableCount})
                                            {selectedWritableCount > 0 && (
                                                <FeatureCostBadge
                                                    featureCode="article_gen"
                                                    multiplier={selectedWritableCount}
                                                    variant="badge"
                                                    insufficient={!isAdmin && pricingReady && walletStatus === 'ready' && totalPoints < (articleGenCost ?? 0) * selectedWritableCount}
                                                    affordableHint={` · 只够 ${Math.floor(totalPoints / Math.max(1, articleGenCost ?? 1))} 篇`}
                                                />
                                            )}
                                        </span>
                                    </Button>
                                </FeatureTooltip>
                                <div className="hidden sm:block flex-1" />
                                {/* 进度显示 */}
                                {writingProgress && writingProgress.writing > 0 && (
                                    <div className="flex items-center gap-2 text-xs sm:text-sm">
                                        <span className="text-purple-600 dark:text-purple-300">
                                            写作中: {writingProgress.writing}篇
                                        </span>
                                        <span className="text-green-600 dark:text-green-300">
                                            已完成: {writingProgress.completed}篇
                                        </span>
                                        {/* WJ-18 后台运行安心提示(防"离开会不会重复扣费"焦虑) */}
                                        <span className="text-muted-foreground">· 后台运行中,离开不重复消耗算力</span>
                                    </div>
                                )}
                                <span className="text-xs sm:text-sm text-slate-500">
                                    {keywords.length}词 / {topics.length}题
                                </span>
                            </div>
                        )}

                        {/* 日常写作只保留补充要求:标题已经决定正文写法,不再让运营二次选择文体 */}
                        {topics.length > 0 && (
                            <div className="mb-3 p-3 rounded-lg border border-border/40 bg-card space-y-2">
                                <div className="text-xs text-muted-foreground font-medium">📝 补充要求(选填)</div>
                                <div>
                                    <div className="text-xs text-muted-foreground mb-1">最多 200 字 · 例:突出价格、不要写榜单 · 不写也可以,系统会按标题内容写作</div>
                                    <ExtraInstructionInput value={extraInstruction} onChange={setExtraInstruction} />
                                </div>
                                {/* [2026-06-02 GEO CTO] 写作两开关:自动配图(默认开) / 插入联系方式(默认关 + 警示) */}
                                <div className="space-y-2 border-t border-border/40 pt-2">
                                    <label className="flex flex-col gap-1 text-xs sm:flex-row sm:items-center">
                                        <span className="font-medium">发布安全档</span>
                                        <select
                                            className="h-8 rounded border border-border bg-card px-2"
                                            value={publicationProfile}
                                            onChange={e => setPublicationProfile(e.target.value as PublicationProfile)}
                                        >
                                            <option value="standard">标准 GEO 文章</option>
                                            <option value="sohu_geo_strict_v1">严格平台安全版（搜狐 GEO）</option>
                                        </select>
                                        <span className="text-muted-foreground">严格版降低审核风险，不承诺平台通过</span>
                                    </label>
                                    {/* [P1-4 引擎定向 2026-08-14] quote 级目标引擎(选后立即保存;不定向 = 现行为) */}
                                    <label className="flex flex-col gap-1 text-xs sm:flex-row sm:items-center">
                                        <span className="font-medium">目标 AI 引擎</span>
                                        <select
                                            className="h-8 rounded border border-border bg-card px-2"
                                            value={targetEngine}
                                            onChange={e => void saveTargetEngine(e.target.value)}
                                        >
                                            <option value="">不定向（默认）</option>
                                            <option value="doubao">豆包</option>
                                            <option value="deepseek">DeepSeek</option>
                                            <option value="kimi">Kimi</option>
                                            <option value="dashscope">千问</option>
                                        </select>
                                        <span className="text-muted-foreground">定向后写作与效果归因围绕该引擎;随时可改,对后续生成生效</span>
                                    </label>
                                    {publicationProfile === 'sohu_geo_strict_v1' && (
                                        <div className="rounded-md border border-sky-200 bg-sky-50 px-2 py-1.5 text-[11px] leading-relaxed text-sky-800 dark:border-sky-500/40 dark:bg-sky-500/10 dark:text-sky-200">
                                            保留客户知识库、六大 GEO 文体和权威证据；正文自动禁用客户宣传图、联系方式、外链、促销与 GEO 效果保证。联系方式请使用搜狐号主页等平台允许的位置。
                                        </div>
                                    )}
                                    <label className="flex cursor-pointer items-center gap-2 text-xs">
                                        <input type="checkbox" checked={addImages} disabled={publicationProfile === 'sohu_geo_strict_v1'} onChange={e => setAddImages(e.target.checked)} className="h-4 w-4 rounded border-border accent-emerald-600 disabled:opacity-50" />
                                        <span className="font-medium">自动配图</span>
                                        <span className="text-muted-foreground">用客户图片素材给文章配图,没有合适图会自动跳过</span>
                                    </label>
                                    <label className="flex cursor-pointer items-start gap-2 text-xs">
                                        <input type="checkbox" checked={addContact} disabled={publicationProfile === 'sohu_geo_strict_v1'} onChange={e => setAddContact(e.target.checked)} className="mt-0.5 h-4 w-4 rounded border-border accent-amber-600 disabled:opacity-50" />
                                        <span className="min-w-0">
                                            <span className="font-medium">插入联系方式</span>
                                            <span className="text-muted-foreground"> · 只对本次生成生效；未勾选时文章、预览和发布都不会加入客户联系方式</span>
                                            {addContact && (
                                                <span className="mt-1 block rounded-md border border-amber-300 bg-amber-50 px-2 py-1.5 text-[11px] leading-relaxed text-amber-700 dark:border-amber-500/40 dark:bg-amber-500/10 dark:text-amber-300">
                                                    ⚠️ 加联系方式后，很多媒体可能审核不通过。发布到媒体时，系统会自动改成更稳妥的说法；如需显示官网或完整联系方式，请选择支持的媒体。
                                                </span>
                                            )}
                                        </span>
                                    </label>
                                </div>
                                <div className="text-xs text-muted-foreground/80">💡 标题已经决定文章方向,开始写作时会自动沿用标题对应的写法。</div>
                            </div>
                        )}

                        {/* 操作栏 - 优化Tab */}
                        {workbenchTab === 'optimize' && (() => {
                            const optTopics = topics.filter(t => t.is_optimize);
                            const optPending = optTopics.filter(t => t.status === 'pending' || t.status === 'draft');
                            const optCompleted = optTopics.filter(t => t.status === 'completed');
                            const optWriting = optTopics.filter(t => t.status === 'writing');
                            const optFailed = optTopics.filter(t => t.status === 'failed');
                            const allPendingSelected = optPending.length > 0 && optPending.every(t => selectedTopics.has(t.id));
                            const allCompletedSelected = optCompleted.length > 0 && optCompleted.every(t => selectedTopics.has(t.id));
                            const selectedCompletedIds = optCompleted.filter(t => selectedTopics.has(t.id));
                            return (
                                <div className="flex flex-wrap items-center gap-2 p-2 sm:p-3 bg-orange-50 rounded-lg mb-4 dark:bg-orange-500/10">
                                    {/* 待写操作 */}
                                    {optPending.length > 0 && (
                                        <>
                                            <Button variant="outline" size="sm" onClick={() => {
                                                if (allPendingSelected) {
                                                    setSelectedTopics(prev => {
                                                        const next = new Set(prev);
                                                        optPending.forEach(t => next.delete(t.id));
                                                        return next;
                                                    });
                                                } else {
                                                    setSelectedTopics(prev => {
                                                        const next = new Set(prev);
                                                        optPending.forEach(t => next.add(t.id));
                                                        return next;
                                                    });
                                                }
                                            }}>
                                                <CheckSquare className="h-4 w-4 mr-1" />
                                                {allPendingSelected ? "取消待写" : `全选待写 (${optPending.length})`}
                                            </Button>
                                            <FeatureTooltip
                                                featureId="sandbox_step4_opt_write"
                                                stepId="first_monitoring"
                                                title="补发:开始写智能建议文章"
                                                content={'已经帮你勾选好补发的标题了。点"开始写作", AI 会针对这个落后的词重新写文章。\n\n(教程模式生成的是占位壳子, 几秒就好)'}
                                                side="bottom"
                                                disabled={!isSandboxActive() || tutorialStage !== 'step4-opt-write'}
                                            >
                                            <Button
                                                size="sm"
                                                onClick={() => { void startWriting(); }}
                                                disabled={selectedWritableCount === 0 || writingInProgress || !pricingReady}
                                                className="bg-green-600 hover:bg-green-700"
                                            >
                                                {writingInProgress ? (
                                                    <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                                ) : (
                                                    <Sparkles className="h-4 w-4 mr-1" />
                                                )}
                                                <span className="inline-flex items-center gap-1.5">
                                                    开始写作 ({selectedWritableCount})
                                                    {selectedWritableCount > 0 && (
                                                        <FeatureCostBadge
                                                            featureCode="article_gen"
                                                            multiplier={selectedWritableCount}
                                                            variant="badge"
                                                            insufficient={!isAdmin && pricingReady && walletStatus === 'ready' && totalPoints < (articleGenCost ?? 0) * selectedWritableCount}
                                                            affordableHint={` · 只够 ${Math.floor(totalPoints / Math.max(1, articleGenCost ?? 1))} 篇`}
                                                        />
                                                    )}
                                                </span>
                                            </Button>
                                            </FeatureTooltip>
                                        </>
                                    )}
                                    {/* 分隔 */}
                                    {optPending.length > 0 && optCompleted.length > 0 && (
                                        <div className="w-px h-6 bg-slate-300 mx-1" />
                                    )}
                                    {/* 已完成操作 */}
                                    {optCompleted.length > 0 && (
                                        <>
                                            <Button variant="outline" size="sm" onClick={() => {
                                                if (allCompletedSelected) {
                                                    setSelectedTopics(prev => {
                                                        const next = new Set(prev);
                                                        optCompleted.forEach(t => next.delete(t.id));
                                                        return next;
                                                    });
                                                } else {
                                                    setSelectedTopics(prev => {
                                                        const next = new Set(prev);
                                                        optCompleted.forEach(t => next.add(t.id));
                                                        return next;
                                                    });
                                                }
                                            }}>
                                                <CheckSquare className="h-4 w-4 mr-1" />
                                                {allCompletedSelected ? "取消已完成" : `全选已完成 (${optCompleted.length})`}
                                            </Button>
                                            <FeatureTooltip
                                                featureId="sandbox_step4_opt_gopublish"
                                                stepId="first_monitoring"
                                                title="补发文章写好了 · 去发布"
                                                content={'补发的文章已经写好(已帮你勾选)。点"去发布"把它们发到媒体上。\n\n发布流程你第三步已经走过了, 这次教程帮你跳过选媒介那些步骤, 直接到一键发布。'}
                                                side="bottom"
                                                disabled={!isSandboxActive() || tutorialStage !== 'step4-opt-gopublish'}
                                            >
                                            <Button
                                                size="sm"
                                                onClick={() => {
                                                    if (selectedCompletedIds.length === 0) return;
                                                    const ids = selectedCompletedIds.map(t => t.article_id || t.id).join(',');
                                                    if (isSandboxActive() && tutorialStage === 'step4-opt-gopublish') {
                                                        setTutorialStage('step4-opt-batch');
                                                    }
                                                    navigate(`/publish?articles=${ids}&quote_id=${selectedProject?.id || ''}`);
                                                }}
                                                disabled={selectedCompletedIds.length === 0}
                                            >
                                                <Send className="h-4 w-4 mr-1" />
                                                去发布 ({selectedCompletedIds.length})
                                            </Button>
                                            </FeatureTooltip>
                                        </>
                                    )}
                                    {/* 进度 */}
                                    <div className="hidden sm:block flex-1" />
                                    {optWriting.length > 0 && (
                                        <span className="text-xs text-purple-600 dark:text-purple-300">写作中: {optWriting.length}篇</span>
                                    )}
                                    {optFailed.length > 0 && (
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            disabled={retryingOptimizeTitles}
                                            onClick={() => { void retryFailedOptimizeTitles(optFailed); }}
                                            className="border-red-200 text-red-700 hover:bg-red-50 dark:border-red-500/40 dark:text-red-300"
                                        >
                                            {retryingOptimizeTitles
                                                ? <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                                                : <RefreshCw className="h-4 w-4 mr-1" />}
                                            重试失败标题 ({optFailed.length})
                                        </Button>
                                    )}
                                    <span className="text-xs text-slate-500">
                                        待写{optPending.length} / 写作中{optWriting.length} / 失败{optFailed.length} / 已完成{optCompleted.length}
                                    </span>
                                </div>
                            );
                        })()}

                        {/* 操作栏 - 仅在已完成Tab显示 */}
                        {workbenchTab === 'completed' && (
                            <div className="flex flex-wrap items-center gap-2 p-2 sm:p-3 bg-green-50 rounded-lg mb-4 dark:bg-green-500/10">
                                {/* [P3a 2026-08-01 · 工单 §6] 主入口「审核与修复 (N)」。
                                    N = 已完成文章里还有待处理项的篇数(硬门 / 普通提示 / 没跑过审核);
                                    N=0 时按钮仍在但不显数字,不做"红点常驻"制造焦虑。 */}
                                <Button
                                    variant="outline"
                                    size="sm"
                                    data-testid="open-batch-review-panel"
                                    onClick={() => setBatchReviewPanelOpen(true)}
                                    disabled={topics.filter(t => t.status === 'completed' && t.article_id).length === 0}
                                >
                                    <ShieldCheck className="h-4 w-4 mr-1" />
                                    {attentionCount(topics) > 0 ? `审核与修复 (${attentionCount(topics)})` : '审核与修复'}
                                </Button>
                                <BatchReviewPanel
                                    open={batchReviewPanelOpen}
                                    onOpenChange={setBatchReviewPanelOpen}
                                    topics={topics}
                                    selectedTopicIds={selectedTopics}
                                    quoteId={selectedProject?.id}
                                    onNavigatePublish={(articleIds) => {
                                        if (articleIds.length === 0) return;
                                        setBatchReviewPanelOpen(false);
                                        navigate(`/publish?articles=${articleIds.join(',')}&quote_id=${selectedProject?.id || ''}`);
                                    }}
                                    onRefresh={async () => {
                                        // 🔴 只刷新数据,不跳转(P2 锁 1 的同一条纪律):
                                        // loadProjectDetail 只 setKeywords/setTopics,不碰 selectedTopics /
                                        // expandedKeywords / 滚动位置,现场不会丢。
                                        if (selectedProject) await loadProjectDetail(selectedProject.id);
                                    }}
                                />
                                <Button variant="outline" size="sm" onClick={toggleSelectAllCompleted}>
                                    <CheckSquare className="h-4 w-4 mr-1" />
                                    {topics.filter(t => t.status === 'completed').every(t => selectedTopics.has(t.id))
                                        ? "取消全选" : "全选"}
                                </Button>
                                <FeatureTooltip
                                    featureId="sandbox_writing_go_publish"
                                    stepId="first_article_publish"
                                    title="第十步: 勾选全部 + 点发布"
                                    content="6 篇文章全写好了 · 全选后点这里进入发布中心, 选代发媒介一次性投放"
                                    side="bottom"
                                    disabled={!isSandboxActive() || tutorialStage !== 'step3-go-publish'}
                                >
                                    <Button
                                        variant="default"
                                        size="sm"
                                        onClick={() => {
                                            const selected = topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id));
                                            if (selected.length === 0) return;
                                            const ids = selected.map(t => t.article_id || t.id).join(',');
                                            navigate(`/publish?articles=${ids}&quote_id=${selectedProject?.id || ''}`);
                                        }}
                                        disabled={topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length === 0}
                                    >
                                        <Send className="h-4 w-4 mr-1" />
                                        {topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length > 1
                                            ? `发布 (${topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length}) →`
                                            : `发布 →`
                                        }
                                    </Button>
                                </FeatureTooltip>
                                {!isCEnd && (
                                    <Button
                                        variant="default"
                                        size="sm"
                                        onClick={downloadSelectedArticles}
                                        disabled={topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length === 0}
                                        className="bg-green-600 hover:bg-green-700"
                                    >
                                        <Download className="h-4 w-4 mr-1" />
                                        下载选中 ({topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length})
                                    </Button>
                                )}
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={batchRewriteSelected}
                                    disabled={topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length === 0 || batchRewriting}
                                >
                                    {batchRewriting ? (
                                        <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                    ) : (
                                        <RefreshCw className="h-4 w-4 mr-1" />
                                    )}
                                    <span className="inline-flex items-center gap-1.5">
                                        {batchRewriting ? "重写中..." : `批量重写 (${topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length})`}
                                    </span>
                                </Button>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={runKnowledgeCheck}
                                    disabled={checkingKnowledge || topics.filter(t => t.status === 'completed').length === 0}
                                    className="border-blue-300 text-blue-600 hover:bg-blue-50 dark:border-blue-500/40 dark:text-blue-300"
                                >
                                    {checkingKnowledge ? (
                                        <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                    ) : (
                                        <ShieldCheck className="h-4 w-4 mr-1" />
                                    )}
                                    {checkingKnowledge ? "核查中..." : "知识库核查"}
                                </Button>
                                {/* 修复按钮:仅在核查发现 active 问题后显示 · [M 方案] 排除 dismissed */}
                                {Object.values(knowledgeCheckResults).some(r => (r.contradictions || []).some(c => !c.dismissed)) && (() => {
                                    const articlesWithActive = Object.values(knowledgeCheckResults).filter(r => (r.contradictions || []).some(c => !c.dismissed)).length;
                                    const totalActive = Object.values(knowledgeCheckResults).reduce((s, r) => s + (r.contradictions || []).filter(c => !c.dismissed).length, 0);
                                    const totalDismissed = Object.values(knowledgeCheckResults).reduce((s, r) => s + (r.contradictions || []).filter(c => c.dismissed).length, 0);
                                    return (
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            onClick={fixKnowledgeIssues}
                                            disabled={fixingIssues}
                                            className="border-orange-300 text-orange-600 hover:bg-orange-50 dark:border-orange-500/40 dark:text-orange-300"
                                            title={totalDismissed > 0 ? `已忽略 ${totalDismissed} 条误报 · 修复时跳过` : ''}
                                        >
                                            {fixingIssues ? (
                                                <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                            ) : (
                                                <Wrench className="h-4 w-4 mr-1" />
                                            )}
                                            {fixingIssues ? "修复中..." : `修复未忽略 (${articlesWithActive}篇·${totalActive}条)`}
                                        </Button>
                                    );
                                })()}
                                <span className="text-xs sm:text-sm text-slate-500 sm:ml-auto">
                                    已完成: {topics.filter(t => t.status === 'completed').length}篇
                                    {Object.keys(knowledgeCheckResults).length > 0 && (
                                        <span className="ml-2">
                                            {(() => {
                                                const results = Object.values(knowledgeCheckResults);
                                                const passed = results.filter(r => r.status === 'pass').length;
                                                const failed = results.filter(r => r.status === 'fail').length;
                                                const warned = results.filter(r => r.status === 'warning').length;
                                                return (
                                                    <>
                                                        {passed > 0 && <span className="text-green-600 dark:text-green-300">✓{passed}</span>}
                                                        {warned > 0 && <span className="text-yellow-600 ml-1 dark:text-yellow-300">⚠{warned}</span>}
                                                        {failed > 0 && <span className="text-red-600 ml-1 dark:text-red-300">✗{failed}</span>}
                                                    </>
                                                );
                                            })()}
                                        </span>
                                    )}
                                </span>
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => resetToPending(topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).map(t => t.id))}
                                    disabled={topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length === 0 || resettingToPending}
                                    className="border-orange-300 text-orange-600 hover:bg-orange-50 dark:border-orange-500/40 dark:text-orange-300"
                                >
                                    {resettingToPending ? (
                                        <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                    ) : (
                                        <Undo2 className="h-4 w-4 mr-1" />
                                    )}
                                    {resettingToPending ? "处理中..." : `放回待写 (${topics.filter(t => t.status === 'completed' && selectedTopics.has(t.id)).length})`}
                                </Button>
                            </div>
                        )}

                        {/* 根据Tab过滤的topics列表 */}
                        {(() => {
                            const filteredTopics = topics.filter(t => {
                                if (workbenchTab === 'optimize') return !!t.is_optimize;
                                if (workbenchTab === 'pending') return t.status === 'pending' || t.status === 'draft' || t.status === 'failed' || t.status === 'write_timeout' || !t.status;
                                if (workbenchTab === 'writing') return t.status === 'writing';
                                if (workbenchTab === 'completed') return t.status === 'completed';
                                return true;
                            });

                            if (filteredTopics.length === 0) {
                                return (
                                    <Card>
                                        <CardContent className="py-8 text-center text-slate-500">
                                            {workbenchTab === 'pending' && "暂无待写文章"}
                                            {workbenchTab === 'writing' && "暂无写作中文章"}
                                            {workbenchTab === 'completed' && "暂无已完成文章"}
                                            {workbenchTab === 'optimize' && "暂无优化文章"}
                                        </CardContent>
                                    </Card>
                                );
                            }

                            // 优化tab: 按状态分组 → 每组内按关键词分组（复用折叠结构）
                            if (workbenchTab === 'optimize') {
                                const statusGroups = [
                                    { key: 'pending', label: '待写', icon: '○', color: 'text-orange-700', bg: 'bg-orange-100', border: 'border-orange-200', items: filteredTopics.filter(t => t.status === 'pending' || t.status === 'draft' || t.status === 'regenerating' || t.status === 'failed') },
                                    { key: 'writing', label: '写作中', icon: '◎', color: 'text-purple-700', bg: 'bg-purple-100', border: 'border-purple-200', items: filteredTopics.filter(t => t.status === 'writing') },
                                    { key: 'completed', label: '已完成', icon: '●', color: 'text-green-700', bg: 'bg-green-100', border: 'border-green-200', items: filteredTopics.filter(t => t.status === 'completed') },
                                ].filter(g => g.items.length > 0);

                                return (
                                    <div className="space-y-6">
                                        {statusGroups.map(group => {
                                            // 按关键词分组
                                            const byKw: Record<number, Topic[]> = {};
                                            group.items.forEach(t => {
                                                const kid = t.keyword_id || 0;
                                                if (!byKw[kid]) byKw[kid] = [];
                                                byKw[kid].push(t);
                                            });
                                            const kwList = keywords.filter(kw => byKw[kw.id]?.length > 0);
                                            // 没有匹配关键词的 topic（keyword_id 为 null）
                                            const orphans = byKw[0] || [];

                                            return (
                                                <div key={group.key}>
                                                    <div className={`px-4 py-2 ${group.bg} rounded-t-lg border ${group.border} border-b-0 flex items-center gap-2`}>
                                                        <span className={`text-sm ${group.color}`}>{group.icon}</span>
                                                        <span className={`text-sm font-semibold ${group.color}`}>{group.label}</span>
                                                        <span className={`text-xs ${group.color} opacity-70`}>{group.items.length} 篇</span>
                                                    </div>
                                                    <div className={`border ${group.border} border-t-0 rounded-b-lg overflow-hidden`}>
                                                        {kwList.map((kw, kwIdx) => {
                                                            const kwTopics = byKw[kw.id] || [];
                                                            const optKey = `${group.key}-${kw.id}`;
                                                            const isExpanded = optExpandedKws.has(optKey);
                                                            return (
                                                                <div key={optKey} className={kwIdx > 0 ? "border-t" : ""}>
                                                                    <div
                                                                        className="flex items-center gap-2 sm:gap-3 p-2 sm:p-3 bg-slate-50 hover:bg-slate-100 cursor-pointer flex-wrap"
                                                                        onClick={() => setOptExpandedKws(prev => { const n = new Set(prev); n.has(optKey) ? n.delete(optKey) : n.add(optKey); return n; })}
                                                                    >
                                                                        <input
                                                                            type="checkbox"
                                                                            checked={kwTopics.length > 0 && kwTopics.every(t => selectedTopics.has(t.id))}
                                                                            ref={(el) => {
                                                                                if (el) {
                                                                                    const sc = kwTopics.filter(t => selectedTopics.has(t.id)).length;
                                                                                    el.indeterminate = sc > 0 && sc < kwTopics.length;
                                                                                }
                                                                            }}
                                                                            onChange={(e) => {
                                                                                e.stopPropagation();
                                                                                const ids = kwTopics.map(t => t.id);
                                                                                const all = kwTopics.every(t => selectedTopics.has(t.id));
                                                                                const ns = new Set(selectedTopics);
                                                                                ids.forEach(id => all ? ns.delete(id) : ns.add(id));
                                                                                setSelectedTopics(ns);
                                                                            }}
                                                                            onClick={(e) => e.stopPropagation()}
                                                                            className="h-4 w-4"
                                                                        />
                                                                        {isExpanded ? <ChevronDown className="h-4 w-4 text-slate-400" /> : <ChevronRight className="h-4 w-4 text-slate-400" />}
                                                                        <span className="font-medium flex-1 min-w-0 truncate text-sm sm:text-base">{kw.keyword}</span>
                                                                        <Badge variant="secondary" className="text-xs shrink-0">{kwTopics.length}篇</Badge>
                                                                    </div>
                                                                    {isExpanded && kwTopics.map((topic, idx) => (
                                                                        <div key={topic.id} className={`flex items-center gap-1.5 sm:gap-2 px-2 sm:px-4 py-2 border-t border-dashed hover:bg-slate-50 flex-wrap ${topic.status === 'regenerating' ? 'animate-pulse bg-blue-50/50' : topic.status === 'failed' ? 'bg-red-50/50' : ''}`}>
                                                                            <input type="checkbox" checked={selectedTopics.has(topic.id)} disabled={topic.status === 'regenerating' || topic.status === 'writing' || topic.status === 'write_timeout'} onChange={() => toggleSelect(topic.id)} className="h-4 w-4" onClick={(e) => e.stopPropagation()} />
                                                                            <span className="text-xs text-slate-400 w-5">{idx + 1}.</span>
                                                                            <Badge className="bg-orange-500/15 text-orange-400 border border-orange-500/30 text-[10px] shrink-0">优化</Badge>
                                                                            {topic.status === 'regenerating' && <Badge className="bg-blue-100 text-blue-800 text-xs shrink-0 dark:bg-blue-500/20 dark:text-blue-200"><Loader2 className="h-3 w-3 animate-spin mr-1" />标题生成中</Badge>}
                                                                            {topic.status === 'failed' && <Badge className="bg-red-100 text-red-700 border border-red-200 text-xs shrink-0 dark:bg-red-500/20 dark:text-red-300 dark:border-red-500/40" title={articleFailureText(topic)}><X className="h-3 w-3 mr-1" />失败</Badge>}
                                                                            {topic.status === 'writing' && <Badge className="bg-purple-100 text-purple-800 text-xs shrink-0 dark:bg-purple-500/20 dark:text-purple-200"><Loader2 className="h-3 w-3 animate-spin mr-1" />写作中</Badge>}
                                                                            {topic.status === 'completed' && !topic.reviewed_at && <Badge className="bg-red-500 text-white text-xs shrink-0 animate-pulse">NEW</Badge>}
                                                                            {topic.status === 'completed' && <Badge className="bg-green-100 text-green-800 text-xs shrink-0 dark:bg-green-500/20 dark:text-green-200">已完成</Badge>}
                                                                            <span className={`flex-1 min-w-0 text-xs sm:text-sm truncate ${topic.status === 'completed' ? 'text-green-700' : ''}`}>{topic.optimized_title || `${topic.original_keyword}（待重新生成标题）`}</span>
                                                                            {topic.status === 'failed' && (
                                                                                <span className="basis-full pl-10 text-xs text-red-700 dark:text-red-300" title={articleFailureText(topic)}>
                                                                                    {articleFailureText(topic)}
                                                                                </span>
                                                                            )}
                                                                            {topic.status === 'failed' && isTitlePhaseFailure(topic) && (
                                                                                <Button size="sm" variant="outline" data-testid="retry-title-generation" className="h-7 text-xs border-red-200 text-red-700 dark:border-red-500/40 dark:text-red-300" onClick={() => { void generateTopicForKeyword(topic.keyword_id, keywords.find((k) => k.id === topic.keyword_id)?.keyword || ''); }}>
                                                                                    重新生成标题
                                                                                    {/* 🔴 [WO_243 乙] **补救面:免费,不显示价**。重试按钮长在选题行里 ⇒ 有行 ⇒
                                                                                        后端 `if not topics:` 的全额退没触发 ⇒ **这个词的钱还在** ⇒ 再收一次
                                                                                        就是对同一个词收两次。走 `generate-topic` 并带这一行的
                                                                                        `generation_request_id`(由 `generateTopicForKeyword` 内的 `faceForKeyword` 定)。
                                                                                        🔴 徽标与端点**同笔翻**:只去价不改端点 = 照收钱而屏幕上没有任何数字。 */}
                                                                                </Button>
                                                                            )}
                                                                            {/* [WO_232] 复核拒掉的那一批:重试走同一梯子、大概率再被拒(生产实测 5/7 → 2/7),
                                                                                真正管用的是自己写一个标题。所以这个码把编辑入口摆在**同一层**,不藏二级菜单。 */}
                                                                            {topic.status === 'failed' && isTitleAlignmentRejected(topic) && (
                                                                                <Button size="sm" variant="outline" data-testid="edit-title-after-alignment-reject" className="h-7 text-xs" onClick={(e) => { e.stopPropagation(); startEdit(topic); }}>
                                                                                    编辑标题
                                                                                </Button>
                                                                            )}
                                                                            {topic.status === 'failed' && !isTitlePhaseFailure(topic) && topic.generation_retryable !== false
                                                                                && ['refunded', 'released'].includes(topic.generation_refund_status || '') && (
                                                                                <Button size="sm" variant="outline" className="h-7 text-xs border-red-200 text-red-700 dark:border-red-500/40 dark:text-red-300" onClick={() => { void startWriting([topic.id]); }}>
                                                                                    安全重试
                                                                                </Button>
                                                                            )}
                                                                            {(topic.status === 'pending' || topic.status === 'failed') ? (
                                                                                <TopicStyleSelector
                                                                                    topicId={topic.id}
                                                                                    currentValue={effectiveUserChoice(topic)}
                                                                                    industry={selectedProject?.industry}
                                                                                    isCompanyFixedSlot={topic.is_fixed}
                                                                                    onChange={(value) => void updateTopicUserChoice(topic.id, value)}
                                                                                />
                                                                            ) : (
                                                                                <Badge variant="secondary" className="text-xs shrink-0" title="标题已决定文章写法">
                                                                                    {humanizeStyle(displayedStyleSource(topic)) || '按标题写作'}
                                                                                </Badge>
                                                                            )}
                                                                            <ArticleReviewBadge topic={topic} />
                                                                            <ArticleEvidenceAdvisory
                                                                                topic={topic}
                                                                                busy={rewritingTopicIds.has(topic.id)}
                                                                                onRepair={rewriteWholeArticleForAdvisory}
                                                                                onContinue={continueEvidenceAdvisory}
                                                                            onRepaired={() => { if (selectedProject) void loadProjectDetail(selectedProject.id); }}
                                                                            />
                                                                            <ArticleLegalFindings
                                                                                topic={topic}
                                                                                onRepaired={() => { if (selectedProject) void loadProjectDetail(selectedProject.id); }}
                                                                            />
                                                                            {topic.status === 'completed' && topic.article_id && (
                                                                                <>
                                                                                    <Button size="icon" variant="ghost" className="h-7 w-7" title="预览" onClick={() => previewArticle(topic.article_id!, topic.id)}><Eye className="h-3 w-3 text-blue-600 dark:text-blue-300" /></Button>
                                                                                    <Button size="icon" variant="ghost" className="h-7 w-7" title="重写" disabled={rewritingTopicIds.has(topic.id)} onClick={() => openRewrite(topic.article_id!, topic.id)}><RefreshCw className={`h-3 w-3 text-purple-600 dark:text-purple-300 ${rewritingTopicIds.has(topic.id) ? 'animate-spin' : ''}`} /></Button>
                                                                                    <Button
                                                                                        size="icon"
                                                                                        variant="ghost"
                                                                                        className="h-7 w-7"
                                                                                        title={topic.publication_eligible ? '去投放' : (topic.publication_eligibility_message || '文章尚未通过发布审核')}
                                                                                        disabled={!topic.publication_eligible}
                                                                                        onClick={() => navigate(`/publish?article_id=${topic.article_id}&quote_id=${selectedProject?.id || ''}`)}
                                                                                    ><Send className="h-3 w-3 text-emerald-600 dark:text-emerald-300" /></Button>
                                                                                </>
                                                                            )}
                                                                            {topic.status !== 'writing' && topic.status !== 'regenerating' && (
                                                                                <Button size="icon" variant="ghost" className="h-7 w-7" title="编辑标题" onClick={() => startEdit(topic)}><Edit2 className="h-3 w-3" /></Button>
                                                                            )}
                                                                            {topic.status !== 'writing' && topic.status !== 'regenerating' && (
                                                                                <Button size="icon" variant="ghost" className="h-7 w-7 text-red-400 hover:text-red-600" title="删除" onClick={async () => {
                                                                                    if (!(await askConfirm({ title: '确定删除？', danger: true }))) return;
                                                                                    authFetch(`/api/writing/topics/${topic.id}`, { method: 'DELETE' }).then(r => r.json()).then(d => { if (d.success) { setTopics(prev => prev.filter(t => t.id !== topic.id)); toast.success('已删除'); } else toast.error(d.detail || '删除失败'); });
                                                                                }}><X className="h-3 w-3" /></Button>
                                                                            )}
                                                                        </div>
                                                                    ))}
                                                                </div>
                                                            );
                                                        })}
                                                        {/* keyword_id 为空的 orphan topics(v2.9 加 dropdown 同主待写规则)*/}
                                                        {orphans.map((topic, idx) => (
                                                            <div key={topic.id} className={`flex items-center gap-1.5 sm:gap-2 px-2 sm:px-4 py-2 border-t border-dashed hover:bg-slate-50 flex-wrap ${topic.status === 'regenerating' ? 'animate-pulse bg-blue-50/50' : topic.status === 'failed' ? 'bg-red-50/50' : ''}`}>
                                                                <input type="checkbox" checked={selectedTopics.has(topic.id)} disabled={topic.status === 'regenerating' || topic.status === 'writing' || topic.status === 'write_timeout'} onChange={() => toggleSelect(topic.id)} className="h-4 w-4" />
                                                                <span className="text-xs text-slate-400 w-5">{idx + 1}.</span>
                                                                <Badge className="bg-orange-500/15 text-orange-400 border border-orange-500/30 text-[10px] shrink-0">优化</Badge>
                                                                <span className="flex-1 min-w-0 text-xs sm:text-sm truncate">{topic.optimized_title || `${topic.original_keyword}（待重新生成标题）`}</span>
                                                                {(topic.status === 'pending' || topic.status === 'failed') ? (
                                                                    <TopicStyleSelector
                                                                        topicId={topic.id}
                                                                        currentValue={effectiveUserChoice(topic)}
                                                                        industry={selectedProject?.industry}
                                                                        isCompanyFixedSlot={topic.is_fixed}
                                                                        onChange={(value) => void updateTopicUserChoice(topic.id, value)}
                                                                    />
                                                                ) : (
                                                                    <Badge variant="secondary" className="text-xs shrink-0" title="标题已决定文章写法">
                                                                        {humanizeStyle(displayedStyleSource(topic)) || '按标题写作'}
                                                                    </Badge>
                                                                )}
                                                                <ArticleReviewBadge topic={topic} />
                                                                <ArticleEvidenceAdvisory
                                                                    topic={topic}
                                                                    busy={rewritingTopicIds.has(topic.id)}
                                                                    onRepair={rewriteWholeArticleForAdvisory}
                                                                    onContinue={continueEvidenceAdvisory}
                                                                onRepaired={() => { if (selectedProject) void loadProjectDetail(selectedProject.id); }}
                                                                />
                                                                <ArticleLegalFindings
                                                                    topic={topic}
                                                                    onRepaired={() => { if (selectedProject) void loadProjectDetail(selectedProject.id); }}
                                                                />
                                                            </div>
                                                        ))}
                                                    </div>
                                                </div>
                                            );
                                        })}
                                    </div>
                                );
                            }

                            // 按关键词分组过滤后的topics
                            const filteredByKeyword: Record<number, Topic[]> = {};
                            filteredTopics.forEach(t => {
                                if (!filteredByKeyword[t.keyword_id]) filteredByKeyword[t.keyword_id] = [];
                                filteredByKeyword[t.keyword_id].push(t);
                            });

                            return (
                                <div className="border rounded-lg overflow-hidden">
                                    {/* [CTO-15.23 2026-05-08 P0-B] 删过滤 · 显示所有 keyword
                                       原 keywords.filter(kw => filteredByKeyword[kw.id]?.length > 0) 把"加完关键词但没生成 topic"的 kw 过滤掉
                                       客户报"加了好几次都没成功"真因 → 用户 + 添加关键词 → confirmed_keywords +1 但 topics 没 · UI 看不到
                                       修法:全显示 · 没 topic 的 kw 行加"未生成标题 · 待批量生成"提示 */}
                                    {keywords.map((kw, kwIdx) => {
                                        const kwTopics = filteredByKeyword[kw.id] || [];
                                        const isExpanded = expandedKeywords.has(kw.id);
                                        const hasNoTopics = kwTopics.length === 0;
                                        /*
                                         * 🔴 [WO_254 附带] **这颗按钮的闸必须和面孔判据数同一个集合。**
                                         *
                                         * `kwTopics` 来自 `filteredByKeyword`,而那是**当前页签过滤后**的
                                         * 子集(`filteredTopics` 按 workbenchTab 过滤)。于是在「待写」页签上,
                                         * 一个所有选题都已 `completed` 的词读出来是「一条都没有」⇒ 按钮出现;
                                         * 而点下去时 `generateTopicForKeyword` 用的是**未过滤**的 `topics`,
                                         * 看见有行 ⇒ 判成补救 ⇒ 后端 403,toast 让用户去点一个不存在的按钮。
                                         * 生产实测 380/2900 词踩在这上面。
                                         *
                                         * 🔴 两个数看的是**不同的集合**,而各自单独看都对 —— 所以闸改成直接问
                                         *    那个唯一判据:只有「真的一行都没有」(new-keywords)才给这颗按钮。
                                         *    缺题那一条的补救入口在选题行上,不在这里。
                                         */
                                        const kwFace = faceForKeyword(kw.id, topics).face;
                                        // [WO_317 第三笔] 0 槽的词是有意不出题,不给「立即生成标题」(点了后端也会 400)
                                        const canGenerateTitlesNow = kwFace === 'new-keywords' && plannedPosts(kw) > 0;
                                        // 沙盒: 第一个关键词行带 spotlight 引导用户展开
                                        const showExpandSpotlight = isSandboxActive()
                                            && kwIdx === 0
                                            && tutorialStage === 'step3-expand-titles';
                                        const rowEl = (
                                            <div key={kw.id} className={kwIdx > 0 ? "border-t" : ""}>
                                                {/* 关键词行 - 点击展开，checkbox选中子文章 */}
                                                <div
                                                    className="flex min-h-11 items-center gap-2 sm:gap-3 p-2 sm:p-3 bg-slate-50 hover:bg-slate-100 cursor-pointer flex-wrap"
                                                    data-sandbox-coach-anchor={showExpandSpotlight ? 'writing-expand-titles' : undefined}
                                                    onClick={() => {
                                                        toggleKeywordExpand(kw.id);
                                                        // 沙盒: 第一次点开关键词行 · 顺便把另一个组也展开 + 推 stage
                                                        if (isSandboxActive() && tutorialStage === 'step3-expand-titles') {
                                                            setExpandedKeywords(new Set(keywords.map(k => k.id)));
                                                            setTutorialStage('step3-show-titles');
                                                        }
                                                    }}
                                                >
                                                    {/* 关键词级 checkbox：选中/取消该关键词下所有文章 */}
                                                    <input
                                                        type="checkbox"
                                                        checked={kwTopics.length > 0 && kwTopics.every(t => selectedTopics.has(t.id))}
                                                        ref={(el) => {
                                                            if (el) {
                                                                const selectedCount = kwTopics.filter(t => selectedTopics.has(t.id)).length;
                                                                el.indeterminate = selectedCount > 0 && selectedCount < kwTopics.length;
                                                            }
                                                        }}
                                                        onChange={(e) => {
                                                            e.stopPropagation();
                                                            const kwTopicIds = kwTopics.map(t => t.id);
                                                            const allSelected = kwTopics.every(t => selectedTopics.has(t.id));
                                                            const newSet = new Set(selectedTopics);
                                                            if (allSelected) {
                                                                kwTopicIds.forEach(id => newSet.delete(id));
                                                            } else {
                                                                kwTopicIds.forEach(id => newSet.add(id));
                                                            }
                                                            setSelectedTopics(newSet);
                                                        }}
                                                        onClick={(e) => e.stopPropagation()}
                                                        className="h-4 w-4"
                                                    />
                                                    {isExpanded ? (
                                                        <ChevronDown className="h-4 w-4 text-slate-400" />
                                                    ) : (
                                                        <ChevronRight className="h-4 w-4 text-slate-400" />
                                                    )}
                                                    <span className="font-medium flex-1 min-w-0 truncate text-sm sm:text-base">{kw.keyword}</span>
                                                    <Badge variant="secondary" className="text-xs shrink-0">
                                                        需{plannedPosts(kw)}篇
                                                    </Badge>
                                                    {/* 🔴 紧挨着的「已生成 M 篇」不动 —— 一对数必须同量词,只改一半会变成
                                                        「需 5 条 / 已生成 3 篇」,比两个都不改更糟。 */}
                                                    {slotsNote(kw) && (
                                                        <span className="text-[11px] text-muted-foreground shrink-0">{slotsNote(kw)}</span>
                                                    )}
                                                    <Badge variant={kwTopics.length >= plannedPosts(kw) ? "default" : "outline"} className={`text-xs shrink-0 ${hasNoTopics ? 'border-amber-500/40 text-amber-500' : ''}`}>
                                                        已生成{kwTopics.length}篇
                                                    </Badge>
                                                    {/* [CTO-15.23 2026-05-08 死锁修] 没 topic 的 kw · 直接给"立即生成"按钮
                                                        旧版本指向"点上方批量生成标题"但 writing_status≠pending 时按钮不显示·死锁
                                                        新版本同步调 /generate-topic 单 kw endpoint(b7e61a01 worker 失败的二次保险) */}
                                                    {canGenerateTitlesNow && (
                                                        <Button
                                                            data-testid="kw-generate-titles-now"
                                                            variant="outline"
                                                            size="sm"
                                                            className="h-6 px-2 text-xs shrink-0 border-amber-500/40 bg-amber-500/10 text-amber-500 hover:bg-amber-500/20"
                                                            onClick={(e) => {
                                                                e.stopPropagation();
                                                                void generateTopicForKeyword(kw.id, kw.keyword);
                                                            }}
                                                            disabled={generatingForKwIds.has(kw.id)}
                                                            title="AI 立即生成标题(普通生成耗时约 30 秒)"
                                                        >
                                                            {generatingForKwIds.has(kw.id)
                                                                ? <><Loader2 className="h-3 w-3 mr-1 animate-spin" />生成中</>
                                                                : <><Sparkles className="h-3 w-3 mr-1" />立即生成标题</>}
                                                        </Button>
                                                    )}
                                                    <span className="text-xs sm:text-sm text-slate-500 shrink-0 hidden sm:inline">¥{(kw.final_price || 0).toFixed(0)}</span>
                                                </div>

                                                {/* 标题列表 - 折叠时隐藏 */}
                                                {isExpanded && kwTopics.length > 0 && (
                                                    <div className="bg-white">
                                                        {kwTopics.map((topic, idx) => (
                                                            <div key={topic.id}>
                                                            <div
                                                                className={`flex items-center gap-1.5 sm:gap-2 px-2 sm:px-4 py-2 border-t border-dashed hover:bg-slate-50 flex-wrap ${topic.status === 'regenerating' ? 'animate-pulse bg-blue-50/50' : topic.status === 'failed' ? 'bg-red-50/50' : ''}`}
                                                            >
                                                                <input
                                                                    type="checkbox"
                                                                    checked={selectedTopics.has(topic.id)}
                                                                    disabled={topic.status === 'regenerating' || topic.status === 'write_timeout'}
                                                                    onChange={() => toggleSelect(topic.id)}
                                                                    className="h-4 w-4"
                                                                    onClick={(e) => e.stopPropagation()}
                                                                />
                                                                <span className="text-xs text-slate-400 w-5">{idx + 1}.</span>
                                                                {editingTopicId === topic.id ? (
                                                                    <>
                                                                        <Input
                                                                            value={editingTitle}
                                                                            onChange={(e) => setEditingTitle(e.target.value)}
                                                                            className="flex-1 h-7 text-sm"
                                                                            onClick={(e) => e.stopPropagation()}
                                                                        />
                                                                        <Button size="icon" variant="ghost" className="h-7 w-7" onClick={(e) => { e.stopPropagation(); saveEdit(); }}>
                                                                            <Check className="h-3 w-3 text-green-600 dark:text-green-300" />
                                                                        </Button>
                                                                        <Button size="icon" variant="ghost" className="h-7 w-7" onClick={(e) => { e.stopPropagation(); cancelEdit(); }}>
                                                                            <X className="h-3 w-3 text-red-600 dark:text-red-300" />
                                                                        </Button>
                                                                    </>
                                                                ) : (
                                                                    <>
                                                                        {/* 优化标识 */}
                                                                        {topic.is_optimize && (
                                                                            <Badge className="bg-orange-500/15 text-orange-400 border border-orange-500/30 text-[10px] shrink-0">
                                                                                优化
                                                                            </Badge>
                                                                        )}
                                                                        {/* 状态标识 */}
                                                                        {topic.status === 'regenerating' && (
                                                                            <Badge className="bg-blue-100 text-blue-800 text-xs shrink-0 dark:bg-blue-500/20 dark:text-blue-200">
                                                                                <Loader2 className="h-3 w-3 animate-spin mr-1" />
                                                                                标题生成中
                                                                            </Badge>
                                                                        )}
                                                                        {topic.status === 'failed' && (
                                                                            <Badge className="bg-red-100 text-red-700 border border-red-200 text-xs shrink-0 dark:bg-red-500/20 dark:text-red-300 dark:border-red-500/40" title={articleFailureText(topic)}>
                                                                                <X className="h-3 w-3 mr-1" />
                                                                                失败
                                                                            </Badge>
                                                                        )}
                                                                        {topic.status === 'failed' && (
                                                                            <span className="basis-full pl-8 text-xs text-red-700 dark:text-red-300" title={articleFailureText(topic)}>
                                                                                {articleFailureText(topic)}
                                                                            </span>
                                                                        )}
                                                                        {/* [T5 P0] 标题阶段失败:没扣过费,重试 = 重新生成标题(不走退款闸门) */}
                                                                        {topic.status === 'failed' && isTitlePhaseFailure(topic) && (
                                                                            <Button size="sm" variant="outline" data-testid="retry-title-generation" className="h-7 text-xs border-red-200 text-red-700 dark:border-red-500/40 dark:text-red-300" onClick={(e) => { e.stopPropagation(); void generateTopicForKeyword(topic.keyword_id, keywords.find((k) => k.id === topic.keyword_id)?.keyword || ''); }}>
                                                                                重新生成标题
                                                                                {/* 🔴 [WO_243 乙] **补救面:免费,不显示价**。重试按钮长在选题行里 ⇒ 有行 ⇒
                                                                                    后端 `if not topics:` 的全额退没触发 ⇒ **这个词的钱还在** ⇒ 再收一次
                                                                                    就是对同一个词收两次。走 `generate-topic` 并带这一行的
                                                                                    `generation_request_id`(由 `generateTopicForKeyword` 内的 `faceForKeyword` 定)。
                                                                                    🔴 徽标与端点**同笔翻**:只去价不改端点 = 照收钱而屏幕上没有任何数字。 */}
                                                                            </Button>
                                                                        )}
                                                                        {/* [WO_232] 复核拒掉的那一批:重试大概率还被拒(关键词里是公司名),
                                                                            给一条自己写标题的出路,别让用户在「重新生成」上打转。 */}
                                                                        {topic.status === 'failed' && isTitleAlignmentRejected(topic) && (
                                                                            <Button size="sm" variant="outline" data-testid="edit-title-after-alignment-reject" className="h-7 text-xs" onClick={(e) => { e.stopPropagation(); startEdit(topic); }}>
                                                                                编辑标题
                                                                            </Button>
                                                                        )}
                                                                        {topic.status === 'failed' && !isTitlePhaseFailure(topic) && topic.generation_retryable !== false
                                                                            && ['refunded', 'released'].includes(topic.generation_refund_status || '') && (
                                                                            <Button size="sm" variant="outline" className="h-7 text-xs border-red-200 text-red-700 dark:border-red-500/40 dark:text-red-300" onClick={(e) => { e.stopPropagation(); void startWriting([topic.id]); }}>
                                                                                安全重试
                                                                            </Button>
                                                                        )}
                                                                        {topic.status === 'writing' && (
                                                                            <Badge className="bg-purple-100 text-purple-800 text-xs shrink-0 dark:bg-purple-500/20 dark:text-purple-200">
                                                                                <Loader2 className="h-3 w-3 animate-spin mr-1" />
                                                                                写作中
                                                                            </Badge>
                                                                        )}
                                                                        {topic.status === 'write_timeout' && (
                                                                            <Badge className="bg-red-100 text-red-700 border border-red-200 text-xs shrink-0 dark:bg-red-500/20 dark:text-red-300 dark:border-red-500/40" title="写作超时未出稿 · 算力待退还 · 请联系客服">
                                                                                <AlertCircle className="h-3 w-3 mr-1" />
                                                                                写作超时·待处理
                                                                            </Badge>
                                                                        )}
                                                                        {topic.status === 'completed' && (
                                                                            <>
                                                                                {/* NEW标签 - 已完成但未审核的文章（reviewed_at为空）*/}
                                                                                {topic.status === 'completed' && !topic.reviewed_at && (
                                                                                    <Badge className="bg-red-500 text-white text-xs shrink-0 animate-pulse">
                                                                                        NEW
                                                                                    </Badge>
                                                                                )}
                                                                                <Badge className="bg-green-100 text-green-800 text-xs shrink-0 dark:bg-green-500/20 dark:text-green-200">
                                                                                    ✓ 已完成
                                                                                </Badge>
                                                                                {(topic.article_version ?? 0) > 1 && topic.article_updated_at &&
                                                                                  (Date.now() - new Date(topic.article_updated_at).getTime()) < 12 * 60 * 60 * 1000 && (
                                                                                    <Badge className="bg-purple-100 text-purple-800 text-xs shrink-0 dark:bg-purple-500/20 dark:text-purple-200">
                                                                                        已重写
                                                                                    </Badge>
                                                                                )}
                                                                                {topic.completed_at && (
                                                                                    <span className="text-xs text-slate-400 shrink-0">
                                                                                        {new Date(topic.completed_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}
                                                                                    </span>
                                                                                )}
                                                                            </>
                                                                        )}
                                                                        {/* 知识库核查结果标识 */}
                                                                        {knowledgeCheckResults[topic.id] && (
                                                                            <span title={knowledgeCheckResults[topic.id].message} className="shrink-0">
                                                                                {knowledgeCheckResults[topic.id].status === 'pass' && (
                                                                                    <ShieldCheck className="h-4 w-4 text-green-500" />
                                                                                )}
                                                                                {knowledgeCheckResults[topic.id].status === 'warning' && (
                                                                                    <ShieldAlert className="h-4 w-4 text-yellow-500" />
                                                                                )}
                                                                                {knowledgeCheckResults[topic.id].status === 'fail' && (
                                                                                    <ShieldX className="h-4 w-4 text-red-500" />
                                                                                )}
                                                                            </span>
                                                                        )}
                                                                        <span className={`flex-1 min-w-0 text-xs sm:text-sm truncate ${topic.status === 'completed' ? 'text-green-700' : ''}`}>
                                                                            {topic.optimized_title || `${topic.original_keyword}（待重新生成标题）`}
                                                                        </span>
                                                                        {(topic.status === 'pending' || topic.status === 'failed') ? (
                                                                            <TopicStyleSelector
                                                                                topicId={topic.id}
                                                                                currentValue={effectiveUserChoice(topic)}
                                                                                industry={selectedProject?.industry}
                                                                                isCompanyFixedSlot={topic.is_fixed}
                                                                                onChange={(value) => void updateTopicUserChoice(topic.id, value)}
                                                                            />
                                                                        ) : (
                                                                            <Badge variant="secondary" className="text-xs shrink-0" title="标题已决定文章写法">
                                                                                {humanizeStyle(displayedStyleSource(topic)) || '按标题写作'}
                                                                            </Badge>
                                                                        )}
                                                                        <ArticleReviewBadge topic={topic} />
                                                                        <ArticleEvidenceAdvisory
                                                                            topic={topic}
                                                                            busy={rewritingTopicIds.has(topic.id)}
                                                                            onRepair={rewriteWholeArticleForAdvisory}
                                                                            onContinue={continueEvidenceAdvisory}
                                                                        onRepaired={() => { if (selectedProject) void loadProjectDetail(selectedProject.id); }}
                                                                        />
                                                                        <ArticleLegalFindings
                                                                            topic={topic}
                                                                            onRepaired={() => { if (selectedProject) void loadProjectDetail(selectedProject.id); }}
                                                                        />
                                                                        {/* 知识库矛盾详情 */}
                                                                        {knowledgeCheckResults[topic.id]?.contradictions?.length > 0 && (
                                                                            <Badge className="bg-red-100 text-red-700 text-xs shrink-0 dark:bg-red-500/20 dark:text-red-300">
                                                                                {knowledgeCheckResults[topic.id].contradictions.length}处矛盾
                                                                            </Badge>
                                                                        )}
                                                                        {/* 操作按钮 */}
                                                                        {topic.status === 'completed' && topic.article_id && (() => {
                                                                            const showArticleSpotlight = isSandboxActive()
                                                                                && tutorialStage === 'step3-show-articles'
                                                                                && kwIdx === 0
                                                                                && idx === 0;
                                                                            const sandboxBlock = isSandboxActive() && tutorialStage === 'step3-show-articles';
                                                                            const btnGroup = (
                                                                                <span className="inline-flex items-center gap-1">
                                                                                <Button size="icon" variant="ghost" className="h-7 w-7" title="预览" onClick={(e) => { e.stopPropagation(); previewArticle(topic.article_id!, topic.id); }}>
                                                                                    <Eye className="h-3 w-3 text-blue-600 dark:text-blue-300" />
                                                                                </Button>
                                                                                <Button size="icon" variant="ghost" className="h-7 w-7" title={sandboxBlock ? '教程模式不可点 · 真实场景能让 AI 换风格重写' : '重写'}
                                                                                    disabled={sandboxBlock || rewritingTopicIds.has(topic.id)}
                                                                                    onClick={(e) => { e.stopPropagation(); if (!sandboxBlock) openRewrite(topic.article_id!, topic.id); }}>
                                                                                    <RefreshCw className={`h-3 w-3 text-purple-600 dark:text-purple-300 ${rewritingTopicIds.has(topic.id) ? 'animate-spin' : ''}`} />
                                                                                </Button>
                                                                                <Button size="icon" variant="ghost" className="h-7 w-7" title={sandboxBlock ? '教程模式不可点 · 真实场景能直接跳发布' : topic.publication_eligible ? '去投放' : (topic.publication_eligibility_message || '文章尚未通过发布审核')}
                                                                                    disabled={sandboxBlock || !topic.publication_eligible}
                                                                                    onClick={(e) => { e.stopPropagation(); if (!sandboxBlock) navigate(`/publish?article_id=${topic.article_id}&quote_id=${selectedProject?.id || ''}`); }}>
                                                                                    <Send className="h-3 w-3 text-emerald-600 dark:text-emerald-300" />
                                                                                </Button>
                                                                                <Button size="icon" variant="ghost" className="h-7 w-7" title={sandboxBlock ? '教程模式不可点' : '放回待写'}
                                                                                    disabled={sandboxBlock}
                                                                                    onClick={(e) => { e.stopPropagation(); if (!sandboxBlock) resetToPending([topic.id]); }}>
                                                                                    <Undo2 className="h-3 w-3 text-orange-500" />
                                                                                </Button>
                                                                                </span>
                                                                            );
                                                                            if (showArticleSpotlight) {
                                                                                return (
                                                                                    <FeatureTooltip
                                                                                        featureId="sandbox_writing_show_article"
                                                                                        stepId="first_article_publish"
                                                                                        title="第九步: 文章写好了, 看一下"
                                                                                        content={`🔵 [预览] 看 AI 写的文章内容 (教程模式可以点开看)
🟣 [重写] 让 AI 换风格再写一遍
🟢 [投放] 单篇直接发到媒介
🟠 [放回待写] 不满意, 回到待写让 AI 重新出标题

都看过觉得 OK 就点下一步去批量发布`}
                                                                                        side="left"
                                                                                        nextLabel="看过了, 去批量发布 →"
                                                                                        onNext={() => setTutorialStage('step3-go-publish')}
                                                                                    >
                                                                                        {btnGroup}
                                                                                    </FeatureTooltip>
                                                                                );
                                                                            }
                                                                            return btnGroup;
                                                                        })()}
                                                                        {topic.status !== 'writing' && topic.status !== 'regenerating' && (() => {
                                                                            const showPencilSpotlight = isSandboxActive()
                                                                                && tutorialStage === 'step3-show-titles'
                                                                                && kwIdx === 0
                                                                                && idx === 0;
                                                                            // 沙盒此 spotlight 阶段 · 笔按钮禁用 (用户只能看, 不能点) · 强制走 "都没问题" 按钮推进
                                                                            const pencilBtn = (
                                                                                <Button
                                                                                    size="icon"
                                                                                    variant="ghost"
                                                                                    className="h-7 w-7"
                                                                                    title={showPencilSpotlight ? '教程模式不可点 · 看教程说明' : '编辑标题'}
                                                                                    disabled={showPencilSpotlight}
                                                                                    onClick={(e) => { e.stopPropagation(); if (!showPencilSpotlight) startEdit(topic); }}
                                                                                >
                                                                                    <Edit2 className="h-3 w-3" />
                                                                                </Button>
                                                                            );
                                                                            if (showPencilSpotlight) {
                                                                                return (
                                                                                    <FeatureTooltip
                                                                                        featureId="sandbox_writing_pencil_edit"
                                                                                        stepId="first_article_publish"
                                                                                        title="第七步: 标题不满意可以点笔图标改"
                                                                                        content="每个标题旁都有这个笔图标 · 真实场景点了能手动改标题 · 也可以勾选标题后用「重新生成」让 AI 换角度重写 · 教程模式跳过, 知道有就行"
                                                                                        side="left"
                                                                                        nextLabel="都没问题, 开始写作 →"
                                                                                        onNext={() => setTutorialStage('step3-start-writing')}
                                                                                    >
                                                                                        {pencilBtn}
                                                                                    </FeatureTooltip>
                                                                                );
                                                                            }
                                                                            return pencilBtn;
                                                                        })()}
                                                                        {topic.is_optimize && topic.status !== 'writing' && topic.status !== 'regenerating' && (
                                                                            <Button size="icon" variant="ghost" className="h-7 w-7 text-red-400 hover:text-red-600" title="删除选题" onClick={async (e) => {
                                                                                e.stopPropagation();
                                                                                if (!(await askConfirm({ title: '确定删除这个优化选题？', danger: true }))) return;
                                                                                authFetch(`/api/writing/topics/${topic.id}`, { method: 'DELETE' })
                                                                                    .then(r => r.json())
                                                                                    .then(d => {
                                                                                        if (d.success) {
                                                                                            setTopics(prev => prev.filter(t => t.id !== topic.id));
                                                                                            toast.success('已删除');
                                                                                        } else toast.error(d.detail || '删除失败');
                                                                                    })
                                                                                    .catch(() => toast.error('删除失败'));
                                                                            }}>
                                                                                <X className="h-3 w-3" />
                                                                            </Button>
                                                                        )}
                                                                    </>
                                                                )}
                                                            </div>
                                                            {/* 知识库矛盾详情展开 · [M 方案] 每条加 checkbox / 忽略按钮 */}
                                                            {knowledgeCheckResults[topic.id]?.contradictions?.length > 0 && (
                                                                <div className="px-6 py-2 bg-red-50 border-t border-red-100 dark:bg-red-500/10">
                                                                    {knowledgeCheckResults[topic.id].contradictions.map((c, ci) => {
                                                                        const isDismissed = !!c.dismissed;
                                                                        const handleToggle = async () => {
                                                                            if (!c.issue_id) {
                                                                                toast.error('issue 未持久化 · 请先「知识库核查」一次');
                                                                                return;
                                                                            }
                                                                            const next = !isDismissed;
                                                                            try {
                                                                                const r = await authFetch(`/api/placement/issue/${c.issue_id}/dismiss`, {
                                                                                    method: 'PATCH',
                                                                                    headers: { 'Content-Type': 'application/json' },
                                                                                    body: JSON.stringify({ dismissed: next }),
                                                                                });
                                                                                if (!r.ok) throw new Error(`HTTP ${r.status}`);
                                                                                // 本地立即更新 · 不等下次刷新
                                                                                setKnowledgeCheckResults(prev => {
                                                                                    const cur = prev[topic.id];
                                                                                    if (!cur) return prev;
                                                                                    const newC = cur.contradictions.map((x, j) =>
                                                                                        j === ci ? { ...x, dismissed: next } : x
                                                                                    );
                                                                                    return { ...prev, [topic.id]: { ...cur, contradictions: newC } };
                                                                                });
                                                                                toast.success(next ? '已标记为误报' : '已恢复为有效问题');
                                                                            } catch (e) {
                                                                                console.error(e);
                                                                                toast.error('操作失败');
                                                                            }
                                                                        };
                                                                        return (
                                                                            <div key={c.issue_id || ci}
                                                                                className={`text-xs py-1 flex items-start gap-2 ${isDismissed ? 'text-slate-400 line-through' : 'text-red-700'}`}
                                                                            >
                                                                                <input
                                                                                    type="checkbox"
                                                                                    checked={!isDismissed}
                                                                                    onChange={handleToggle}
                                                                                    title={isDismissed ? '已忽略 · 点击恢复' : '取消勾选 = 标记误报 · 修复时跳过此问题'}
                                                                                    className="mt-0.5 size-3.5 cursor-pointer"
                                                                                />
                                                                                <div className="flex-1">
                                                                                    <span className="font-medium">{c.type}：</span>
                                                                                    知识库「{c.kb_data}」→ 文章写「{c.article_data}」
                                                                                    <span className={`ml-2 ${isDismissed ? 'text-slate-400' : 'text-red-400'}`}>{c.context}</span>
                                                                                </div>
                                                                            </div>
                                                                        );
                                                                    })}
                                                                </div>
                                                            )}
                                                            </div>
                                                        ))}
                                                    </div>
                                                )}

                                                {/* 未生成标题提示 */}
                                                {isExpanded && kwTopics.length === 0 && (
                                                    <div className="px-4 py-3 text-sm text-slate-400 text-center border-t border-dashed">
                                                        暂无标题,请点击"批量生成标题"
                                                    </div>
                                                )}
                                            </div>
                                        );
                                        if (showExpandSpotlight) {
                                            return (
                                                <FeatureTooltip
                                                    key={`sandbox-expand-${kw.id}`}
                                                    featureId="sandbox_writing_expand_titles"
                                                    stepId="first_article_publish"
                                                    title="第六步: 点这里展开看 AI 写出的标题"
                                                    content="AI 为这个关键词扩出 3 个不同角度的标题 · 点这一行展开看一下"
                                                    side="bottom"
                                                    wrapClassName="relative block"
                                                    targetSelector='[data-sandbox-coach-anchor="writing-expand-titles"]'
                                                >
                                                    {rowEl}
                                                </FeatureTooltip>
                                            );
                                        }
                                        return rowEl;
                                    })}
                                </div>
                            );
                        })()}
                    </>
                )}
                    </div>
                    <aside className="space-y-4 xl:sticky xl:top-4">
                        {renderKnowledgeSummaryCard()}
                        {renderFactCheckSummaryCard()}
                    </aside>
                </div>
                {renderMaterialsDrawer()}
            </div>
        );
    };

    return (
        <>
            <div className="space-y-6 p-4 sm:p-6">
            {confirmDialog}
                {/* CTO-15.20 桥接 banner */}
                <BridgeBanner />
                {/* 页头 */}
                <div className="flex items-center gap-4">
                    <div className="h-8 w-8 sm:h-10 sm:w-10 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
                        <BookOpen className="h-4 w-4 sm:h-5 sm:w-5 text-brand" />
                    </div>
                    <div className="min-w-0 flex-1">
                        <h1 className="text-lg sm:text-2xl font-bold tracking-tight text-foreground">写作大厅</h1>
                        <p className="text-xs sm:text-sm text-muted-foreground truncate">批量生成 GEO 优化标题和文章,让品牌在 AI 搜索中被推荐</p>
                    </div>
                    {/* Phase 06 (CTO-15.23 2026-05-03 T4) · 快速写作主入口 */}
                    <Button
                        onClick={() => setShowQuickWrite(true)}
                        className="shrink-0 hidden sm:flex"
                    >
                        <Plus className="h-4 w-4 mr-1.5" />
                        快速写作
                    </Button>
                    <Button
                        onClick={() => setShowQuickWrite(true)}
                        size="icon"
                        className="shrink-0 sm:hidden"
                        aria-label="快速写作"
                    >
                        <Plus className="h-4 w-4" />
                    </Button>
                </div>

                {selectedProject ? (
                    renderWorkbench()
                ) : (
                    <>
                        {/* Tab导航 */}
                        <Tabs value={activeTab} onValueChange={setActiveTab}>
                            <TabsList>
                                <TabsTrigger value="pending">待办项目</TabsTrigger>
                                <TabsTrigger value="titles_ready">标题已生成</TabsTrigger>
                                <TabsTrigger value="writing">写作中</TabsTrigger>
                                <TabsTrigger value="completed">已完成</TabsTrigger>
                                <TabsTrigger value="optimizing">优化中</TabsTrigger>
                                <TabsTrigger value="all">全部</TabsTrigger>
                            </TabsList>
                        </Tabs>

                        {/* 项目列表 */}
                        {renderProjectList()}
                    </>
                )}

                {/* 文章预览/编辑弹窗
                    [2026-05-27] aria-modal + role=dialog 提升 a11y(屏幕阅读器识别为 modal) */}
                {previewData && (
                    <div role="dialog" aria-modal="true" className="fixed inset-0 bg-black/50 flex items-center justify-center z-50" onClick={() => setPreviewData(null)}>
                        <div className="bg-card sm:rounded-xl border-0 sm:border border-border sm:max-w-4xl w-full h-full sm:h-auto sm:max-h-[85vh] overflow-hidden sm:m-4 flex flex-col" onClick={e => e.stopPropagation()}>
                            {/* 标题栏 */}
                            <div className="flex flex-col sm:flex-row sm:items-center justify-between p-3 sm:p-4 border-b gap-2 shrink-0">
                                <h3 className="text-base sm:text-lg font-bold flex-1 line-clamp-2 sm:truncate">{previewData.title}</h3>
                                <div className="flex items-center gap-2 shrink-0">
                                    {previewData.isEditing ? (
                                        <>
                                            <Button size="sm" onClick={saveArticle} className="bg-green-600 hover:bg-green-700">
                                                <Check className="h-4 w-4 mr-1" />
                                                保存
                                            </Button>
                                            <Button variant="outline" size="sm" onClick={() => {
                                                setEditContent(previewData.content);
                                                setPreviewData({ ...previewData, isEditing: false });
                                            }}>
                                                取消
                                            </Button>
                                        </>
                                    ) : (
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            disabled={isSandboxActive()}
                                            title={isSandboxActive() ? '教程模式不可点 · 真实场景能直接改文章正文' : undefined}
                                            onClick={() => { if (!isSandboxActive()) setPreviewData({ ...previewData, isEditing: true }); }}
                                        >
                                            <Edit2 className="h-4 w-4 mr-1" />
                                            编辑
                                        </Button>
                                    )}
                                    <Button variant="ghost" size="sm" onClick={() => setPreviewData(null)}>
                                        <X className="h-4 w-4" />
                                    </Button>
                                </div>
                            </div>
                            {/* 内容区 */}
                            <div className="p-3 sm:p-6 overflow-y-auto flex-1 sm:max-h-[calc(85vh-120px)]">
                                {!previewData.isEditing && !previewData.isRewriting && previewData.lengthGuidance && (
                                    <div data-testid="article-length-guidance" className="mb-4 rounded-lg border border-blue-200/60 bg-blue-50/70 p-3 text-sm text-blue-950 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-100">
                                        <div className="font-medium">{previewData.lengthGuidance.summary}</div>
                                        <div className="mt-1 text-xs opacity-80">{previewData.lengthGuidance.detail}</div>
                                        {showAdvancedWritingOptions && (
                                            <div data-testid="article-length-guidance-advanced" className="mt-2 text-xs opacity-75">
                                                建议有效正文 {previewData.lengthGuidance.minimum_chars.toLocaleString()}–{previewData.lengthGuidance.maximum_chars.toLocaleString()} 字
                                                · 当前约 {previewData.lengthGuidance.actual_chars.toLocaleString()} 字
                                            </div>
                                        )}
                                    </div>
                                )}
                                {previewData.isEditing ? (
                                    <textarea
                                        className="w-full h-[calc(100dvh-200px)] sm:h-[60vh] p-3 sm:p-4 border rounded-lg font-sans text-sm leading-relaxed resize-none focus:outline-hidden focus:ring-2 focus:ring-brand"
                                        value={editContent}
                                        onChange={(e) => setEditContent(e.target.value)}
                                    />
                                ) : previewData.isRewriting ? (
                                    <div className="space-y-4">
                                        {/* 修改意见 */}
                                        <div>
                                            <label className="block text-sm font-medium mb-2">修改意见 (可选)</label>
                                            <textarea
                                                className="w-full h-20 sm:h-24 p-3 border rounded-lg text-sm resize-none focus:outline-hidden focus:ring-2 focus:ring-brand"
                                                placeholder="例如:增加更多案例、调整语气更专业、强调价格优势..."
                                                value={revisionNote}
                                                onChange={(e) => setRevisionNote(e.target.value)}
                                            />
                                        </div>
                                        {/* 对标仿写 */}
                                        <div>
                                            <label className="block text-sm font-medium mb-2">对标文章仿写 (可选)</label>
                                            <textarea
                                                className="w-full h-28 sm:h-40 p-3 border rounded-lg text-sm resize-none focus:outline-hidden focus:ring-2 focus:ring-brand"
                                                placeholder="粘贴对标文章内容,AI将仿写其结构和风格..."
                                                value={referenceArticle}
                                                onChange={(e) => setReferenceArticle(e.target.value)}
                                            />
                                        </div>
                                        {/* 操作按钮 */}
                                        <div className="flex flex-wrap items-center gap-3">
                                            <Button
                                                onClick={rewriteArticle}
                                                disabled={rewriteLoading}
                                                className="bg-purple-600 hover:bg-purple-700"
                                            >
                                                {rewriteLoading ? (
                                                    <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                                ) : (
                                                    <RefreshCw className="h-4 w-4 mr-1" />
                                                )}
                                                {rewriteLoading ? '重写中...' : '开始重写'}
                                            </Button>
                                            <Button
                                                variant="outline"
                                                onClick={() => setPreviewData({ ...previewData, isRewriting: false })}
                                            >
                                                取消
                                            </Button>
                                            {!revisionNote && !referenceArticle && (
                                                <span className="text-xs sm:text-sm text-slate-500">
                                                    留空则直接重新生成
                                                </span>
                                            )}
                                        </div>
                                    </div>
                                ) : (
                                    // [CTO-15.23 2026-05-05] 加 remark-gfm 修 GFM 表格/删除线/任务列表全裸残留
                                    // 老板截图"预算与周期参考表"是表格 · 不加 plugin 时显示原始 |...| 文本
                                    <div className="prose dark:prose-invert prose-sm max-w-none prose-headings:font-bold prose-h1:text-xl sm:prose-h1:text-2xl prose-h2:text-lg sm:prose-h2:text-xl prose-h3:text-base sm:prose-h3:text-lg prose-p:leading-relaxed prose-li:leading-relaxed prose-table:my-4 prose-th:px-2 prose-th:py-1 prose-td:px-2 prose-td:py-1">
                                        <ReactMarkdown>{previewData.contentRendered ?? stripInternalPlaceholders(previewData.content)}</ReactMarkdown>
                                    </div>
                                )}
                                {/* [2026-06-02] 配图管理:系统自动配图 · 可更换/不使用 */}
                                {!previewData.isEditing && !previewData.isRewriting && previewData.images && previewData.images.length > 0 && (
                                    <div className="mt-4 rounded-lg border border-border/50 bg-muted/30 p-3">
                                        <div className="mb-2 text-xs font-medium text-foreground">本文配图（{previewData.images.length} 张）· 系统自动从客户图库选 · 可更换或不用</div>
                                        <div className="space-y-2">
                                            {previewData.images.map((img) => (
                                                <div key={img.index} className="flex items-center gap-2 text-xs">
                                                    {img.thumbnail_url
                                                        ? <img src={img.thumbnail_url} alt="" className="h-10 w-10 shrink-0 rounded object-cover border border-border/40" loading="lazy" />
                                                        : <div className="h-10 w-10 shrink-0 rounded bg-muted" />}
                                                    <span className="flex-1 min-w-0 truncate text-muted-foreground">
                                                        {({ hero: '首图', brand_intro: '品牌形象', product: '产品图', case: '案例图' } as Record<string, string>)[img.role] || '配图'}
                                                        {img.caption ? ` · ${img.caption}` : ''}
                                                    </span>
                                                    <button onClick={() => openReplaceGallery(img.index)} className="shrink-0 rounded px-2 py-1 text-blue-600 hover:bg-blue-50 dark:text-blue-300">更换</button>
                                                    <button onClick={() => handleImageAction(img.index, 'remove')} className="shrink-0 rounded px-2 py-1 text-red-400 hover:bg-red-50">不使用</button>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}
                            </div>
                            {/* 底部信息 */}
                            <div className="px-3 sm:px-6 py-3 border-t border-border bg-muted flex items-center justify-between gap-2 shrink-0">
                                <span className="text-xs sm:text-sm text-slate-500">
                                    字数: {previewData.isEditing ? editContent.length : previewData.content.length} 字
                                    {/* [工单 T4 2026-07-29] 说"配了几张"必须说**渲染出来几张**。
                                        旧写法数的是正文里的 [CLIENT_IMAGE 占位符原文,那是"想配几张";
                                        素材在写进正文后被软删时渲染层会静默剥离(生产 200 标记里 62 个
                                        正是这一形态),于是出现"已配图 2 张、却一张都不显示"——
                                        用户读到的就是"图片加载不出来"。改用服务端 image_render 的真实数,
                                        并把掉图张数如实说出来。服务端没给(老响应)时回落占位符计数。 */}
                                    {!previewData.isEditing && (() => {
                                        const render = previewData.imageRender;
                                        const markers = (previewData.content.match(/\[CLIENT_IMAGE/g) || []).length;
                                        const shown = render ? render.rendered_count : markers;
                                        const dropped = render ? render.dropped_count : 0;
                                        if (!markers && !shown) return null;
                                        return (
                                            <span className="ml-2 text-emerald-600 dark:text-emerald-300">
                                                · 已自动配图 {shown} 张
                                                {dropped > 0 && (
                                                    <span className="text-amber-600 dark:text-amber-400">
                                                        （{dropped} 张素材已下架，未展示）
                                                    </span>
                                                )}
                                            </span>
                                        );
                                    })()}
                                </span>
                                {!previewData.isEditing && !previewData.isRewriting && (
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        disabled={isSandboxActive()}
                                        title={isSandboxActive() ? '教程模式不可点 · 真实场景能让 AI 换风格重写' : undefined}
                                        onClick={() => { if (!isSandboxActive()) setPreviewData({ ...previewData, isRewriting: true }); }}
                                        className="text-purple-600 border-purple-300 hover:bg-purple-50 text-xs sm:text-sm dark:text-purple-300 dark:border-purple-500/40"
                                    >
                                        <RefreshCw className="h-3 w-3 sm:h-4 sm:w-4 mr-1" />
                                        <span className="hidden sm:inline">不满意?</span>重新写
                                    </Button>
                                )}
                            </div>
                        </div>
                    </div>
                )}

                {/* [2026-06-02] 配图「更换」选图弹窗(z-60 覆盖预览 · 只列该客户已确认可外发的图) */}
                {replaceImageIdx !== null && (
                    <div className="fixed inset-0 z-[60] bg-black/50 flex items-center justify-center p-4" onClick={() => setReplaceImageIdx(null)}>
                        <div className="bg-background rounded-xl max-w-2xl w-full max-h-[80vh] overflow-auto p-4" onClick={e => e.stopPropagation()}>
                            <div className="mb-3 flex items-center justify-between">
                                <h3 className="text-sm font-semibold text-foreground">选一张图替换</h3>
                                <button onClick={() => setReplaceImageIdx(null)} className="text-muted-foreground hover:text-foreground">✕</button>
                            </div>
                            {galleryAssets.length === 0 ? (
                                <p className="py-8 text-center text-sm text-muted-foreground">该客户暂无「可用于文章」的图片。<br />先去客户资料中心 → 图片素材,上传并打开「可用于文章」开关。</p>
                            ) : (
                                <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                                    {galleryAssets.map((a: any) => (
                                        <button key={a.id} onClick={() => handleImageAction(replaceImageIdx, 'replace', a.id)}
                                            className="rounded-lg border border-border/40 overflow-hidden text-left hover:border-blue-400 transition-colors">
                                            <img src={a.thumbnail_key ? '/' + a.thumbnail_key : a.public_url} alt="" className="w-full aspect-square object-cover" loading="lazy" />
                                            <div className="p-1.5 text-[11px] truncate text-foreground">{a.title || a.file_name}</div>
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>
                    </div>
                )}
            </div>

            {/* 写作设置弹窗 */}
            {isAdmin && (
                <WritingSettingsDialog
                    open={showWritingSettings}
                    onClose={() => setShowWritingSettings(false)}
                    onLLMConfigChange={(provider, model) => setActiveLLM({ provider, model })}
                />
            )}

            {/* [CTO-15.23 2026-05-05] 添加关键词 Dialog · 老板诉求"漏词时不用重新走流程" */}
            <Dialog open={showAddKeyword} onOpenChange={(open) => !addKwSubmitting && setShowAddKeyword(open)}>
                <DialogContent className="sm:max-w-md">
                    <DialogHeader>
                        <DialogTitle>添加关键词</DialogTitle>
                        <DialogDescription>
                            漏词补救通道 · 直接添加到当前项目 · 监测中心下次刷新自动同步
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-3 py-2">
                        <div className="space-y-1.5">
                            <Label htmlFor="add-kw-text">关键词 <span className="text-destructive">*</span></Label>
                            <Input
                                id="add-kw-text"
                                value={addKwForm.keyword}
                                onChange={(e) => setAddKwForm(f => ({ ...f, keyword: e.target.value }))}
                                placeholder="例如:深圳别墅电梯哪家好"
                                maxLength={100}
                                autoFocus
                            />
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="add-kw-category">类别</Label>
                            <Input
                                id="add-kw-category"
                                value={addKwForm.category}
                                onChange={(e) => setAddKwForm(f => ({ ...f, category: e.target.value }))}
                                placeholder="自定义/品牌词/通用词等"
                            />
                        </div>
                        <div className="grid grid-cols-2 gap-3">
                            <div className="space-y-1.5">
                                <Label htmlFor="add-kw-tier">套餐档位</Label>
                                <select
                                    id="add-kw-tier"
                                    value={addKwForm.tier}
                                    onChange={(e) => setAddKwForm(f => ({ ...f, tier: e.target.value as 'entry' | 'standard' | 'flagship' }))}
                                    className="w-full px-3 py-2 rounded-md border border-input bg-background text-sm"
                                >
                                    <option value="entry">入门版</option>
                                    <option value="standard">标准版</option>
                                    <option value="flagship">旗舰版</option>
                                </select>
                            </div>
                            <div className="space-y-1.5">
                                {/* 🔴 [#225 a1 下半] 这个输入框绑的是 `required_articles` = **合同授权的槽数**,
                                      不是要发几篇。原来叫「发布篇数」,同屏上就和下面按口径算出的「篇」混了。 */}
                                  <Label htmlFor="add-kw-articles">授权槽数</Label>
                                <Input
                                    id="add-kw-articles"
                                    type="number"
                                    min={1}
                                    max={50}
                                    value={addKwForm.required_articles}
                                    onChange={(e) => setAddKwForm(f => ({ ...f, required_articles: Math.max(1, Math.min(50, Number(e.target.value) || 1)) }))}
                                />
                            </div>
                        </div>
                        <div className="space-y-1.5">
                            <Label htmlFor="add-kw-price">单价 (元) <span className="text-xs text-muted-foreground">· 可填 0(免费补)</span></Label>
                            <Input
                                id="add-kw-price"
                                type="number"
                                min={0}
                                step={0.01}
                                value={addKwForm.final_price}
                                onChange={(e) => setAddKwForm(f => ({ ...f, final_price: Math.max(0, Number(e.target.value) || 0) }))}
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setShowAddKeyword(false)} disabled={addKwSubmitting}>
                            取消
                        </Button>
                        <Button onClick={submitAddKeyword} disabled={addKwSubmitting || !addKwForm.keyword.trim()}>
                            {addKwSubmitting ? <><Loader2 className="h-4 w-4 mr-1 animate-spin" />生成标题中,约 30 秒…</> : '添加并生成标题'}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* Phase 06 (CTO-15.23 2026-05-03 T4) · 快速写作 Dialog */}
            <CustomerIntakeDialog
                open={showQuickWrite}
                onOpenChange={setShowQuickWrite}
                blocks={['basic', 'profile', 'keywords', 'knowledge']}
                title="快速写作"
                description="老熟客直接写文章 · 填资料 → 自动建档 → 跳生成"
                submitText="创建客户并准备开写"
                onSubmit={handleQuickWriteSubmit}
                minKeywords={1}
                maxKeywords={15}
                keywordsRequired={true}
                complexFields={true}
                showAiFill={true}
            />

            {/* v2.10 (CTO-15.23 2026-05-28) 文章方向配比器 · 高级入口 dialog · 老板 A · 默认收起 */}
            {selectedProject && (
                <DistributionConfigDialog
                    open={distributionDialogOpen}
                    onOpenChange={setDistributionDialogOpen}
                    quoteId={selectedProject.id}
                    hasExistingTitles={topics.length > 0}
                    onApplied={() => {
                        // 应用配比后刷新项目详情(取最新 topics)
                        if (selectedProject) {
                            void loadProjectDetail(selectedProject.id);
                        }
                    }}
                    onSavePendingDistribution={setPendingDistribution}
                />
            )}

            {/* 发布功能已移到独立的 /publish 页面 */}
        </>
    );
}

export default WritingHall;
