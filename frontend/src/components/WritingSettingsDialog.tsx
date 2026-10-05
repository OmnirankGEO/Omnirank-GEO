import { useState, useEffect } from "react";
import { authFetch } from "@/lib/api";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription
} from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Switch } from "@/components/ui/switch";
import {
    Select, SelectContent, SelectItem, SelectTrigger, SelectValue
} from "@/components/ui/select";
import { GEO_INDUSTRY_OPTIONS_WITH_GENERAL, geoIndustryLabel } from "@/lib/geoIndustries";
import {
    Save, RotateCcw, Check, Loader2, Cpu, FileText, AlertCircle, SlidersHorizontal,
    ShieldCheck, RefreshCw, GitBranch, Database, Wand2, History, Rocket, Search, Power
} from "lucide-react";
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

// 类型定义
interface StyleConfig {
    code: string;
    name: string;
    description: string;
    ratio: number;
    prompt: string;
    is_overridden: boolean;
    prompt_length: number;
}
interface ProviderConfig {
    id: string;
    name: string;
    models: string[];
}
interface WritingConfig {
    llm_config: { provider: string; model: string };
    styles: StyleConfig[];
    available_providers: ProviderConfig[];
}

interface ShadowRunRow {
    run_id: string;
    status: string;
    case_id: string;
    shadow_only: boolean;
    shadow_gate_allowed: boolean;
    customer_output_allowed: boolean;
    sidecar_visibility: string;
    sidecar_present: boolean;
    customer_article_sha256: string;
    blocker_count: number;
}

interface ShadowCounterItem {
    label: string;
    count: number;
}

interface ShadowObservability {
    total_runs: number;
    live_run_count: number;
    manual_review_required_count: number;
    sidecar_present_count: number;
    customer_output_open_count: number;
    customer_output_closed_count: number;
    recorder_error_count: number;
    recorder_error_log_unreadable: boolean;
    unreadable_manifest_count: number;
    shadow_gate_allowed_count: number;
    shadow_gate_blocked_count: number;
    by_guard_decision: Record<string, number>;
    by_semantic_decision: Record<string, number>;
    by_evidence_mode: Record<string, number>;
    top_blockers: ShadowCounterItem[];
    top_recorder_error_types: ShadowCounterItem[];
}

interface ShadowRunsData {
    status: string;
    artifact_count: number;
    runs: ShadowRunRow[];
    observability?: ShadowObservability;
    safety?: Record<string, boolean>;
}

interface ShadowSentenceSummary {
    index: number;
    text_excerpt: string;
    linked_evidence_ids: string[];
}

interface ShadowBindingSummary {
    index: number;
    evidence_id: string;
    claim_excerpt: string;
    start: number;
    end: number;
}

interface ShadowRunDetail {
    run_id: string;
    case_id: string;
    shadow_gate_allowed: boolean;
    customer_output_allowed: boolean;
    blockers: string[];
    warnings: string[];
    sidecar: { present: boolean; visibility: string };
    article: {
        sha256: string;
        char_count: number;
        body_preview_redacted?: boolean;
        body_preview_policy?: string;
        sentence_summaries: ShadowSentenceSummary[];
    };
    binding_summaries: ShadowBindingSummary[];
    manual_review: {
        route: string;
        manual_review_required: boolean;
        manual_review_reasons: string[];
        blockers: string[];
        warnings: string[];
        customer_output_allowed: boolean;
    };
    quality_summary: {
        guard_decision: string;
        semantic_decision: string;
        provider_count: number;
        required_provider_count: number;
        warn_count: number;
        fail_count: number;
        issue_count: number;
        semantic_needs_human_review: boolean;
        metric_decision: string;
        metric_retention_rate: number | null;
        source_conflict_count: number;
        source_conflict_needs_review: boolean;
    };
    source_summary: {
        evidence_mode: string;
        evidence_packet_source: string;
        trusted_source: boolean;
        evidence_count: number;
        binding_count: number;
    };
}

interface StyleGuardSummary {
    decision: string;
    worst_action: string;
    finding_count: number;
    findings: { label: string; action: string; reason: string }[];
}

interface StyleJudgeSummary {
    semantic_decision: string;
    provider_count: number;
    note?: string;
}

interface StyleVersionRow {
    version_id: string;
    style_code: string;
    style_name: string;
    source: string;
    status: string;
    prompt_sha256: string;
    strategy_summary: string;
    guard_summary: StyleGuardSummary;
    judge_summary: StyleJudgeSummary;
    created_at: string;
    created_by: number;
    activated_at: string | null;
    stable_baseline: boolean;
    rollback_parent: string | null;
    sample_count: number;
    control_sample_count: number;
    risk_level: string;
    allowed_industries: string[];
    blocked_industries: string[];
    rollout_recommendation: {
        eligibility: string;
        reason: string;
        may_activate: boolean;
        customer_output_allowed: boolean;
    };
}

interface StyleControlState {
    schema_version: number;
    config_version: number;
    updated_at?: string;
    active_by_style: Record<string, string>;
    stable_by_style: Record<string, string>;
    version_count: number;
    by_status: Record<string, number>;
}

interface StyleControlData extends StyleControlState {
    versions: StyleVersionRow[];
    customer_output_allowed: boolean;
}

interface FlywheelSummary {
    industry_key: string;
    style_code: string;
    sample_count: number;
    control_sample_count: number;
    sample_count_status?: string;
    control_sample_count_status?: string;
    shadow_pass_rate: number;
    semantic_pass_rate: number;
    latest_candidate: string;
    eligibility: string;
    eligibility_reason: string;
    source_file_count: number;
    article_structure?: {
        loaded?: number;
        research_article_count?: number;
        generated_article_count?: number;
        sample_status?: string;
        source?: string;
        recommended_rules?: string[];
        feature_lift?: {
            label: string;
            adopted_share?: number;
            control_share?: number;
            lift?: number;
            recommended?: boolean;
        }[];
    };
    customer_output_allowed: boolean;
}

interface EvidenceChain {
    config_version: number;
    version_id: string;
    style_code: string;
    status: string;
    evidence_chain: {
        summary: string;
        why_new_is_better: string[];
        sample_threshold: Record<string, string | number | boolean>;
        evidence_mode: string;
        source_file_count: number;
    };
    guard_summary: Record<string, unknown>;
    judge_summary: Record<string, unknown>;
    rollout_recommendation: Record<string, unknown>;
}

interface AuditEvent {
    created_at: string;
    action: string;
    actor_id: number;
    style_code: string;
    version_id: string;
    from_status?: string | null;
    to_status?: string | null;
    previous_active_id?: string | null;
    note?: string;
}

interface FeatureSwitchRow {
    key: string;
    label: string;
    description: string;
    consequence: string;
    enabled: boolean;
    locked: boolean;
    dangerous: boolean;
    requires_confirmation: boolean;
    enable_confirm?: string;
    disable_confirm?: string;
    source: string;
    updated_at?: string;
    updated_by?: number;
    note?: string;
    runtime?: {
        effective_enabled?: boolean;
        active_style_count?: number;
        permanently_locked?: boolean;
    };
}

interface FeatureSwitchData {
    schema_version: number;
    config_version: number;
    updated_at?: string;
    switches: FeatureSwitchRow[];
}

interface WritingSettingsDialogProps {
    open: boolean;
    onClose: () => void;
    onLLMConfigChange?: (provider: string, model: string) => void;
}

export default function WritingSettingsDialog({ open, onClose, onLLMConfigChange }: WritingSettingsDialogProps) {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [config, setConfig] = useState<WritingConfig | null>(null);
    const [activeSettingsTab, setActiveSettingsTab] = useState("overview");

    // 模型设置
    const [selectedProvider, setSelectedProvider] = useState("dashscope");
    const [selectedModel, setSelectedModel] = useState("qwen3-max");
    const [customModel, setCustomModel] = useState("");
    const [useCustomModel, setUseCustomModel] = useState(false);

    // 文章模板设置
    const [activeStyle, setActiveStyle] = useState<string>("");
    const [editedPrompts, setEditedPrompts] = useState<Record<string, string>>({});
    const [savedMessage, setSavedMessage] = useState("");

    // 运行参数
    const [concurrentWriters, setConcurrentWriters] = useState(10);
    const [articleTimeout, setArticleTimeout] = useState(180);
    const [articleRetryCount, setArticleRetryCount] = useState(2);

    // 内部观测运行摘要
    const [shadowRunsData, setShadowRunsData] = useState<ShadowRunsData | null>(null);
    const [shadowRunsLoading, setShadowRunsLoading] = useState(false);
    const [shadowRunsError, setShadowRunsError] = useState("");
    const [shadowRunDetail, setShadowRunDetail] = useState<ShadowRunDetail | null>(null);
    const [shadowRunDetailLoading, setShadowRunDetailLoading] = useState(false);
    const [shadowRunDetailError, setShadowRunDetailError] = useState("");

    // 文体飞轮控制台
    const [styleControlData, setStyleControlData] = useState<StyleControlData | null>(null);
    const [styleControlLoading, setStyleControlLoading] = useState(false);
    const [styleControlError, setStyleControlError] = useState("");
    const [selectedVersionId, setSelectedVersionId] = useState("");
    const [evidenceChain, setEvidenceChain] = useState<EvidenceChain | null>(null);
    const [auditEvents, setAuditEvents] = useState<AuditEvent[]>([]);
    const [flywheelSummary, setFlywheelSummary] = useState<FlywheelSummary | null>(null);
    const [alignStyleCode, setAlignStyleCode] = useState("buying_guide");
    const [alignIndustryKey, setAlignIndustryKey] = useState("general");
    const [alignEvidenceMode, setAlignEvidenceMode] = useState("with_evidence");
    const [alignRiskLevel, setAlignRiskLevel] = useState("normal");
    const [operationNote, setOperationNote] = useState("");
    const [featureSwitchData, setFeatureSwitchData] = useState<FeatureSwitchData | null>(null);
    const [featureSwitchLoading, setFeatureSwitchLoading] = useState(false);
    const [featureSwitchError, setFeatureSwitchError] = useState("");

    // 加载配置
    useEffect(() => {
        if (open) {
            setActiveSettingsTab("overview");
            loadConfig();
        }
    }, [open]);

    const loadShadowRuns = async () => {
        setShadowRunsLoading(true);
        setShadowRunsError("");
        try {
            const res = await authFetch("/api/writing/shadow-runs");
            const data = await res.json();
            if (data.success && data.data) {
                setShadowRunsData(data.data as ShadowRunsData);
            } else {
                setShadowRunsError(data.detail || data.message || "内部观测读取失败");
            }
        } catch (e) {
            console.error("加载内部观测失败:", e);
            setShadowRunsError("内部观测读取失败");
        } finally {
            setShadowRunsLoading(false);
        }
    };

    const loadFeatureSwitches = async () => {
        setFeatureSwitchLoading(true);
        setFeatureSwitchError("");
        try {
            const res = await authFetch("/api/writing/feature-switches");
            const data = await res.json();
            if (data?.switches) {
                setFeatureSwitchData(data as FeatureSwitchData);
            } else {
                setFeatureSwitchError(data.detail?.message || data.detail || data.message || "功能开关读取失败");
            }
        } catch (e) {
            console.error("加载功能开关失败:", e);
            setFeatureSwitchError("功能开关读取失败");
        } finally {
            setFeatureSwitchLoading(false);
        }
    };

    const updateFeatureSwitch = async (item: FeatureSwitchRow, enabled: boolean) => {
        if (item.locked) return;
        const confirmToken = enabled ? item.enable_confirm : item.disable_confirm;
        if (item.dangerous || confirmToken) {
            const ok = await askConfirm({
                title: `请再次确认: ${enabled ? "开启" : "关闭"}「${item.label}」。`,
                description: `后果: ${item.consequence}。此操作只允许管理员执行,并会写入配置版本。`,
                danger: true,
            });
            if (!ok) return;
        }
        setFeatureSwitchLoading(true);
        setFeatureSwitchError("");
        try {
            const res = await authFetch(`/api/writing/feature-switches/${encodeURIComponent(item.key)}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    enabled,
                    expected_config_version: featureSwitchData?.config_version ?? 0,
                    note: operationNote || "writing settings toggle",
                    confirm: confirmToken || "",
                })
            });
            const data = await res.json();
            if (!res.ok) {
                throw new Error(data.detail?.message || data.detail || "功能开关保存失败");
            }
            setFeatureSwitchData(data as FeatureSwitchData);
            showSaved(`「${item.label}」已${enabled ? "开启" : "关闭"}`);
            if (item.key === "r6h_shadow_injection") {
                await loadShadowRuns();
            }
        } catch (e) {
            console.error("保存功能开关失败:", e);
            setFeatureSwitchError(e instanceof Error ? e.message : "功能开关保存失败");
        } finally {
            setFeatureSwitchLoading(false);
        }
    };

    const loadShadowRunDetail = async (runId: string) => {
        setShadowRunDetailLoading(true);
        setShadowRunDetailError("");
        try {
            const res = await authFetch(`/api/writing/shadow-runs/${encodeURIComponent(runId)}`);
            const data = await res.json();
            if (data.success && data.data) {
                setShadowRunDetail(data.data as ShadowRunDetail);
            } else {
                setShadowRunDetailError(data.detail || data.message || "内部观测详情读取失败");
            }
        } catch (e) {
            console.error("加载内部观测详情失败:", e);
            setShadowRunDetailError("内部观测详情读取失败");
        } finally {
            setShadowRunDetailLoading(false);
        }
    };

    const errorText = (data: any, fallback: string) => {
        if (!data) return fallback;
        if (typeof data.detail === "string") return data.detail;
        if (data.detail?.message) return data.detail.message;
        if (data.detail?.code) return data.detail.code;
        return data.message || fallback;
    };

    const loadEvidenceChain = async (versionId: string) => {
        if (!versionId) {
            setEvidenceChain(null);
            return;
        }
        try {
            const res = await authFetch(`/api/writing/style-control/evidence-chain/${encodeURIComponent(versionId)}`);
            const data = await res.json();
            if (data.success && data.data) {
                setEvidenceChain(data.data as EvidenceChain);
            }
        } catch (e) {
            console.error("加载文体证据链失败:", e);
        }
    };

    const loadStyleControl = async (styleCode = alignStyleCode, industryKey = alignIndustryKey) => {
        setStyleControlLoading(true);
        setStyleControlError("");
        try {
            const [versionsRes, flywheelRes, auditRes] = await Promise.all([
                authFetch("/api/writing/style-control/versions"),
                authFetch(`/api/writing/style-control/flywheel-summary?industry_key=${encodeURIComponent(industryKey)}&style_code=${encodeURIComponent(styleCode)}`),
                authFetch("/api/writing/style-control/audit-log?limit=80"),
            ]);
            const [versionsData, flywheelData, auditData] = await Promise.all([
                versionsRes.json(),
                flywheelRes.json(),
                auditRes.json(),
            ]);
            if (versionsData.success && versionsData.data) {
                const payload = versionsData.data as StyleControlData;
                setStyleControlData(payload);
                const selectedStillExists = selectedVersionId && payload.versions.some(v => v.version_id === selectedVersionId);
                const nextVersionId = selectedStillExists
                    ? selectedVersionId
                    : payload.versions.find(v => v.status === "active")?.version_id || payload.versions[0]?.version_id || "";
                setSelectedVersionId(nextVersionId);
                if (nextVersionId) {
                    await loadEvidenceChain(nextVersionId);
                }
            } else {
                setStyleControlError(errorText(versionsData, "文体版本读取失败"));
            }
            if (flywheelData.success && flywheelData.data) {
                setFlywheelSummary(flywheelData.data as FlywheelSummary);
            }
            if (auditData.success && auditData.data) {
                setAuditEvents((auditData.data.events || []) as AuditEvent[]);
            }
        } catch (e) {
            console.error("加载文体控制台失败:", e);
            setStyleControlError("文体控制台读取失败");
        } finally {
            setStyleControlLoading(false);
        }
    };

    const createAlignedDraft = async () => {
        if (!styleControlData) return;
        setSaving(true);
        setStyleControlError("");
        try {
            const res = await authFetch("/api/writing/style-control/align-draft", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    style_code: alignStyleCode,
                    industry_key: alignIndustryKey,
                    evidence_mode: alignEvidenceMode,
                    risk_level: alignRiskLevel,
                    expected_config_version: styleControlData.config_version,
                    source_summary: flywheelSummary ? {
                        sample_count: flywheelSummary.sample_count,
                        control_sample_count: flywheelSummary.control_sample_count,
                        sample_count_status: flywheelSummary.sample_count_status,
                        control_sample_count_status: flywheelSummary.control_sample_count_status,
                        shadow_pass_rate: flywheelSummary.shadow_pass_rate,
                        semantic_pass_rate: flywheelSummary.semantic_pass_rate,
                    } : undefined,
                })
            });
            const data = await res.json();
            if (data.success && data.data) {
                const versionId = data.data.version.version_id;
                setSelectedVersionId(versionId);
                showSaved("一键对齐已生成待审核候选，未自动上线");
                await loadStyleControl(alignStyleCode, alignIndustryKey);
                await loadEvidenceChain(versionId);
            } else {
                setStyleControlError(errorText(data, "一键对齐失败"));
            }
        } catch (e) {
            console.error("一键对齐失败:", e);
            setStyleControlError("一键对齐失败");
        } finally {
            setSaving(false);
        }
    };

    const activateSelectedVersion = async () => {
        if (!styleControlData || !selectedVersionId) return;
        const ok = await askConfirm({
            title: "请再次确认: 设为当前启用会影响后续文章使用的文章模板。",
            description: "不会让实验内容给客户看,但会改变管理员控制的线上写作配置。",
            danger: true,
        });
        if (!ok) return;
        setSaving(true);
        setStyleControlError("");
        try {
            const res = await authFetch("/api/writing/style-control/activate", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    version_id: selectedVersionId,
                    expected_config_version: styleControlData.config_version,
                    note: operationNote || "管理员在写作中心设为当前启用",
                    confirm: "ACTIVATE_WRITING_STYLE_VERSION",
                })
            });
            const data = await res.json();
            if (data.success) {
                showSaved("文体版本已设为当前启用");
                setOperationNote("");
                await loadStyleControl(alignStyleCode, alignIndustryKey);
            } else {
                setStyleControlError(errorText(data, "启用失败"));
            }
        } catch (e) {
            console.error("启用失败:", e);
            setStyleControlError("启用失败");
        } finally {
            setSaving(false);
        }
    };

    const rollbackSelectedStyle = async (styleCode?: string) => {
        if (!styleControlData) return;
        const targetStyle = styleCode || selectedVersion?.style_code || alignStyleCode;
        setSaving(true);
        setStyleControlError("");
        try {
            const res = await authFetch("/api/writing/style-control/rollback", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    style_code: targetStyle,
                    expected_config_version: styleControlData.config_version,
                    note: operationNote || "管理员回退默认版本",
                })
            });
            const data = await res.json();
            if (data.success) {
                const versionId = data.data.active_version.version_id;
                setSelectedVersionId(versionId);
                showSaved("已回退到默认版本");
                setOperationNote("");
                await loadStyleControl(targetStyle, alignIndustryKey);
                await loadEvidenceChain(versionId);
            } else {
                setStyleControlError(errorText(data, "回退失败"));
            }
        } catch (e) {
            console.error("回退失败:", e);
            setStyleControlError("回退失败");
        } finally {
            setSaving(false);
        }
    };

    const retireSelectedVersion = async () => {
        if (!styleControlData || !selectedVersionId) return;
        setSaving(true);
        setStyleControlError("");
        try {
            const res = await authFetch("/api/writing/style-control/retire", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    version_id: selectedVersionId,
                    expected_config_version: styleControlData.config_version,
                    note: operationNote || "管理员退役候选版本",
                })
            });
            const data = await res.json();
            if (data.success) {
                showSaved("候选版本已退役");
                setOperationNote("");
                await loadStyleControl(alignStyleCode, alignIndustryKey);
            } else {
                setStyleControlError(errorText(data, "退役失败"));
            }
        } catch (e) {
            console.error("退役失败:", e);
            setStyleControlError("退役失败");
        } finally {
            setSaving(false);
        }
    };

    const formatFlywheelCount = (value?: number, status?: string) => {
        return status === "unknown" ? "数据待接入" : `${value ?? 0}`;
    };

    const statusLabel = (status?: string) => {
        const labels: Record<string, string> = {
            active: "当前启用",
            draft: "待审核候选",
            stable: "默认回退",
            blocked: "未通过",
            retired: "已退役",
        };
        return labels[status || ""] || "待审核";
    };

    const sourceLabel = (source?: string) => {
        const labels: Record<string, string> = {
            code_default: "代码默认",
            flywheel_align: "飞轮生成",
            manual: "管理员创建",
        };
        return labels[source || ""] || "内部来源";
    };

    const guardDecisionLabel = (decision?: string) => {
        const labels: Record<string, string> = {
            pass: "通过",
            pass_with_warning: "有提醒",
            retry_required: "需复核",
            blocked: "阻断",
        };
        return labels[decision || ""] || "未校验";
    };

    const guardActionLabel = (action?: string) => {
        const labels: Record<string, string> = {
            PASS: "通过",
            WARN: "提醒",
            RETRY: "需复核",
            BLOCK: "阻断",
        };
        return labels[action || ""] || action || "-";
    };

    const semanticDecisionLabel = (decision?: string) => {
        const labels: Record<string, string> = {
            pass: "通过",
            pass_with_warning: "有提醒",
            blocked: "阻断",
            incomplete: "未完成",
            not_run: "未复核",
            missing: "未复核",
        };
        return labels[decision || ""] || "未复核";
    };

    const eligibilityLabel = (eligibility?: string) => {
        const labels: Record<string, string> = {
            eligible: "可生成候选",
            shadow_only: "仅内部观察",
            insufficient_samples: "样本不足",
            unknown: "数据待接入",
        };
        return labels[eligibility || ""] || "数据待接入";
    };

    const evidenceModeLabel = (mode?: string) => {
        const labels: Record<string, string> = {
            with_evidence: "有证据资料",
            no_evidence: "无证据资料",
            missing: "未提供资料",
            unknown: "资料状态未知",
        };
        return labels[mode || ""] || "有证据资料";
    };

    const riskLevelLabel = (risk?: string) => {
        const labels: Record<string, string> = {
            normal: "标准风险",
            high: "高责任行业",
            low: "低风险",
        };
        return labels[risk || ""] || "标准风险";
    };

    const routeLabel = (route?: string) => {
        const labels: Record<string, string> = {
            auto_continue: "可继续内部流程",
            manual_review: "需要人工复核",
            // [写作质量总工单 2026-07-29 · D-3] 统一口径:不用"阻断"吓人,说清实际后果。
            blocked: "发布前需修改",
            incomplete: "资料不完整",
            missing: "未计算",
        };
        return labels[route || ""] || "需要人工复核";
    };

    const sourceTrustLabel = (source?: string) => {
        const labels: Record<string, string> = {
            trusted_research_storage: "服务端资料库",
            research_storage: "服务端资料库",
            server_research_store: "服务端资料库",
            admin_shadow_packet: "管理员内部资料",
            llm_output: "模型输出",
            client_input: "前端输入",
            customer_submitted: "客户提交",
            missing: "无来源标记",
        };
        return labels[source || ""] || "未确认来源";
    };

    const reasonLabel = (reason?: string) => {
        const labels: Record<string, string> = {
            customer_output_feature_flag_off: "实验内容给客户看已关闭",
            manual_review_required: "需要人工复核",
            high_liability_manual_review: "高责任内容需人工复核",
            high_liability_manual_review_required: "高责任内容需人工复核",
            high_liability_critical_metric_missing: "高责任关键指标缺失",
            metric_retention_below_minimum: "关键指标保留不足",
            soft_fabrication_warn: "可能有超出资料的延展",
            evidence_binding_warn: "证据绑定偏弱",
            warn_count_threshold: "提醒较多，需要复核",
            missing_evidence_packet_source: "缺少资料来源",
            untrusted_evidence_packet_source: "资料来源不可信",
            missing_customer_article_sha256: "缺少文章校验码",
            evidence_binding_missing: "缺少证据绑定",
            guard_blocked: "规则校验未通过",
            guard_retry_required: "规则校验需要重试",
            semantic_incomplete: "语义复核未完成",
            semantic_blocked: "语义复核未通过",
            provider_count_insufficient: "复核方数量不足",
            source_conflict_needs_review: "资料存在冲突",
            stripped_guard_rejudge_after_strip: "需重新校验客户看到的正文",
            strip_evidence_markers_rejudge: "需剥离内部证据标记后复核",
            deterministic_guard_rejudge_after_strip: "需重新跑规则校验",
            cross_vendor_judge_rejudge_after_strip: "需重新跑三方语义复核",
            no_evidence_visible_marker: "无证据模式残留内部标记",
            missing_evidence_packet: "缺少证据资料",
            route_missing: "缺少处理路线",
            route_customer_output_not_allowed: "处理路线不允许给客户看",
            route_customer_output_not_explicitly_allowed: "未明确允许给客户看",
        };
        if (!reason) return "无";
        return labels[reason] || reason.replace(/_/g, " ");
    };

    const runStatusLabel = (status?: string) => {
        const labels: Record<string, string> = {
            recorded: "已记录",
            listed: "已列入",
            blocked: "发布前需修改",
            incomplete: "资料不完整",
            error: "记录失败",
        };
        return labels[status || ""] || "已记录";
    };

    const auditActionLabel = (action?: string) => {
        const labels: Record<string, string> = {
            align_draft: "生成候选",
            activate: "设为当前启用",
            rollback: "回退默认版本",
            retire: "退役候选",
            bootstrap: "登记默认版本",
        };
        return labels[action || ""] || action || "操作";
    };

    const sampleThresholdLabel = (key?: string) => {
        const labels: Record<string, string> = {
            sample_count: "样本数",
            control_sample_count: "对照数",
            minimum_sample_count: "最低样本",
            required_provider_count: "复核方数量",
            metric_retention_rate: "指标保留",
            risk_level: "风险等级",
            evidence_mode: "资料模式",
        };
        return labels[key || ""] || (key || "").replace(/_/g, " ");
    };

    const versionDisplayName = (version?: StyleVersionRow) => {
        if (!version) return "未选择";
        const day = version.created_at ? version.created_at.slice(0, 10) : "默认版本";
        const suffix = version.stable_baseline ? "默认回退" : statusLabel(version.status);
        return `${version.style_name || "文章模板"} · ${suffix} · ${day}`;
    };

    const featureSwitchStateLabel = (item: FeatureSwitchRow) => {
        if (item.locked) return "永久关闭";
        if (item.key === "writing_style_overrides" && item.enabled && !item.runtime?.effective_enabled) {
            return "待启用模板";
        }
        return item.enabled ? "开启" : "关闭";
    };

    const featureSwitchStateClass = (item: FeatureSwitchRow) => {
        if (item.locked) return "bg-red-950/30 text-red-200 border-red-800";
        if (item.key === "writing_style_overrides" && item.enabled && !item.runtime?.effective_enabled) {
            return "bg-amber-950/30 text-amber-200 border-amber-800";
        }
        if (item.enabled) return "bg-green-950/30 text-green-200 border-green-800";
        return "bg-gray-900 text-muted-foreground border-border";
    };

    const loadConfig = async () => {
        setLoading(true);
        try {
            // 加载写作模型 + 文章模板配置
            const res = await authFetch("/api/writing/config");
            const data = await res.json();
            if (data.success && data.data) {
                const cfg = data.data as WritingConfig;
                setConfig(cfg);
                setSelectedProvider(cfg.llm_config.provider);
                setSelectedModel(cfg.llm_config.model);
                const provider = cfg.available_providers.find(p => p.id === cfg.llm_config.provider);
                if (provider && !provider.models.includes(cfg.llm_config.model)) {
                    setUseCustomModel(true);
                    setCustomModel(cfg.llm_config.model);
                }
                if (cfg.styles.length > 0) {
                    setActiveStyle(cfg.styles[0].code);
                    setAlignStyleCode(prev => prev || cfg.styles[0].code);
                }
            }
            // 加载系统设置中的运行参数
            const settingsRes = await authFetch("/api/settings");
            const settingsData = await settingsRes.json();
            if (settingsData.success && settingsData.data) {
                const s = settingsData.data;
                setConcurrentWriters(s.concurrent_writers ?? 10);
                setArticleTimeout(s.article_timeout ?? 180);
                setArticleRetryCount(s.article_retry_count ?? 2);
            }
            await loadShadowRuns();
            await loadStyleControl();
            await loadFeatureSwitches();
        } catch (e) {
            console.error("加载写作配置失败:", e);
        } finally {
            setLoading(false);
        }
    };

    // 保存运行参数（写入系统 settings）
    const saveRuntimeConfig = async () => {
        setSaving(true);
        try {
            const res = await authFetch("/api/settings", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    concurrent_writers: concurrentWriters,
                    article_timeout: articleTimeout,
                    article_retry_count: articleRetryCount,
                })
            });
            const data = await res.json();
            if (data.success) {
                showSaved("运行参数已保存");
            }
        } catch (e) {
            console.error("保存运行参数失败:", e);
        } finally {
            setSaving(false);
        }
    };

    // 保存模型配置
    const saveLLMConfig = async () => {
        setSaving(true);
        const model = useCustomModel ? customModel : selectedModel;
        try {
            const res = await authFetch("/api/writing/config", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    llm_config: { provider: selectedProvider, model }
                })
            });
            const data = await res.json();
            if (data.success) {
                showSaved("模型配置已保存");
                onLLMConfigChange?.(selectedProvider, model);
            }
        } catch (e) {
            console.error("保存模型配置失败:", e);
        } finally {
            setSaving(false);
        }
    };

    // 保存文章模板
    const savePrompt = async (styleCode: string) => {
        if (!editedPrompts[styleCode]) return;
        setSaving(true);
        try {
            const res = await authFetch("/api/writing/config", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    prompt_overrides: { [styleCode]: editedPrompts[styleCode] }
                })
            });
            const data = await res.json();
            if (data.success) {
                // 更新本地状态
                setConfig(prev => {
                    if (!prev) return prev;
                    return {
                        ...prev,
                        styles: prev.styles.map(s =>
                            s.code === styleCode
                                ? { ...s, prompt: editedPrompts[styleCode], is_overridden: true, prompt_length: editedPrompts[styleCode].length }
                                : s
                        )
                    };
                });
                // 清除编辑状态
                setEditedPrompts(prev => {
                    const next = { ...prev };
                    delete next[styleCode];
                    return next;
                });
                showSaved(`「${config?.styles.find(s => s.code === styleCode)?.name}」文章模板已保存`);
            }
        } catch (e) {
            console.error("保存文章模板失败:", e);
        } finally {
            setSaving(false);
        }
    };

    // 重置文章模板
    const resetPrompt = async (styleCode: string) => {
        setSaving(true);
        try {
            const res = await authFetch("/api/writing/config/reset-prompt", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ style_code: styleCode })
            });
            const data = await res.json();
            if (data.success) {
                setConfig(prev => {
                    if (!prev) return prev;
                    return {
                        ...prev,
                        styles: prev.styles.map(s =>
                            s.code === styleCode
                                ? { ...s, prompt: data.prompt, is_overridden: false, prompt_length: data.prompt.length }
                                : s
                        )
                    };
                });
                setEditedPrompts(prev => {
                    const next = { ...prev };
                    delete next[styleCode];
                    return next;
                });
                showSaved(`「${config?.styles.find(s => s.code === styleCode)?.name}」已恢复默认文章模板`);
            }
        } catch (e) {
            console.error("重置文章模板失败:", e);
        } finally {
            setSaving(false);
        }
    };

    const showSaved = (msg: string) => {
        setSavedMessage(msg);
        setTimeout(() => setSavedMessage(""), 3000);
    };

    const currentStyle = config?.styles.find(s => s.code === activeStyle);
    const currentPrompt = editedPrompts[activeStyle] ?? currentStyle?.prompt ?? "";
    const hasUnsavedChanges = activeStyle in editedPrompts;
    const currentProvider = config?.available_providers.find(p => p.id === selectedProvider);
    const shadowRuns = shadowRunsData?.runs ?? [];
    const shadowGateAllowedCount = shadowRuns.filter(r => r.shadow_gate_allowed).length;
    const shadowSidecarCount = shadowRuns.filter(r => r.sidecar_present).length;
    const shadowCustomerOutputClosedCount = shadowRuns.filter(r => !r.customer_output_allowed).length;
    const shadowObservability = shadowRunsData?.observability;
    const topShadowBlockers = shadowObservability?.top_blockers ?? [];
    const styleVersions = styleControlData?.versions ?? [];
    const selectedVersion = styleVersions.find(v => v.version_id === selectedVersionId) || styleVersions[0];
    const draftCount = styleControlData?.by_status?.draft ?? 0;
    const activeCount = styleControlData?.by_status?.active ?? 0;
    const stableCount = styleControlData?.by_status?.stable ?? 0;
    const blockedCount = styleControlData?.by_status?.blocked ?? 0;
    const featureSwitches = featureSwitchData?.switches ?? [];
    const styleOverrideSwitch = featureSwitches.find(item => item.key === "writing_style_overrides");
    const shadowRecordSwitch = featureSwitches.find(item => item.key === "r6h_shadow_injection");
    const customerOutputSwitch = featureSwitches.find(item => item.key === "r6h_customer_output");
    const styleOverrideRuntimeActiveCount = styleOverrideSwitch?.runtime?.active_style_count ?? activeCount;
    const styleOverrideEffectivelyEnabled = Boolean(styleOverrideSwitch?.runtime?.effective_enabled);
    const enabledOperatorSwitchCount = featureSwitches.filter(item => item.enabled && !item.locked).length;
    const lockedSwitchCount = featureSwitches.filter(item => item.locked).length;
    const activeStyleVersionId = selectedVersion ? styleControlData?.active_by_style?.[selectedVersion.style_code] : "";
    const settingsArticleStructureLoaded = Number(flywheelSummary?.article_structure?.loaded ?? 0);
    const settingsResearchArticleCount = Number(flywheelSummary?.article_structure?.research_article_count ?? 0);
    const settingsGeneratedArticleCount = Number(flywheelSummary?.article_structure?.generated_article_count ?? 0);
    const baselineVersion = selectedVersion
        ? styleVersions.find(v => v.style_code === selectedVersion.style_code && v.stable_baseline)
        : undefined;
    const selectedVersionScoreText = selectedVersion
        ? `${guardDecisionLabel(selectedVersion.guard_summary?.decision)} / ${semanticDecisionLabel(selectedVersion.judge_summary?.semantic_decision)}`
        : "未选择";
    const baselineScoreText = baselineVersion
        ? `${guardDecisionLabel(baselineVersion.guard_summary?.decision)} / ${semanticDecisionLabel(baselineVersion.judge_summary?.semantic_decision)}`
        : "代码默认";
    const optimizationIssueCards = [
        {
            title: "减少无证据数字",
            before: "旧模板容易写出价格、百分比、案例数量等具体数字。",
            after: "新规则会把无证据数字拦住或改成定性表达。",
        },
        {
            title: "避免内部信息外露",
            before: "旧流程可能把审核提示、证据包说明写进正文。",
            after: "新规则会把内部提示、成本、毛利、工作流字眼列为阻断项。",
        },
        {
            title: "加强证据绑定",
            before: "旧文章可能出现引用错位，读者难判断依据来自哪里。",
            after: "新流程保留内部证据链，审核时能看每个候选的依据。",
        },
        {
            title: "区分内部观察和客户可见实验",
            before: "测试结果容易被误认为已经可以上线。",
            after: "实验内容给客户看永久关闭，内部观察只给管理员看。",
        },
    ];
    const canActivateSelected = Boolean(
        selectedVersion &&
        selectedVersion.status !== "active" &&
        selectedVersion.status !== "blocked" &&
        selectedVersion.guard_summary?.worst_action !== "BLOCK"
    );
    const canRetireSelected = Boolean(selectedVersion && !selectedVersion.stable_baseline && selectedVersion.status !== "active");
    const statusBadgeClass = (status?: string) => {
        if (status === "active") return "bg-green-50 text-green-700 border-green-200";
        if (status === "stable") return "bg-blue-50 text-blue-700 border-blue-200";
        if (status === "blocked") return "bg-red-50 text-red-700 border-red-200";
        if (status === "retired") return "bg-gray-100 text-gray-600 border-gray-200";
        return "bg-amber-50 text-amber-700 border-amber-200";
    };

    return (
        <Dialog open={open} onOpenChange={(v) => !v && onClose()}>
            <DialogContent className="max-w-[1180px] max-h-[86vh] flex flex-col p-0 gap-0">
                <DialogHeader className="px-6 py-4 border-b shrink-0">
                    <DialogTitle className="text-lg font-semibold">写作设置</DialogTitle>
                    <DialogDescription className="text-sm text-muted-foreground">
                        日常只看总览和功能开关；模板、文体优化和高级诊断仅管理员需要时进入。
                    </DialogDescription>
                </DialogHeader>

                {loading ? (
                    <div className="flex-1 flex items-center justify-center">
                        <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
                        <span className="ml-2 text-muted-foreground">加载配置中...</span>
                    </div>
                ) : (
                    <Tabs value={activeSettingsTab} onValueChange={setActiveSettingsTab} className="flex-1 flex flex-col min-h-0 overflow-hidden">
                        <TabsList className="mx-6 mt-3 w-fit h-auto flex flex-wrap shrink-0">
                            <TabsTrigger value="overview" className="gap-1.5">
                                <ShieldCheck className="w-4 h-4" /> 日常总览
                            </TabsTrigger>
                            <TabsTrigger value="feature-switches" className="gap-1.5">
                                <Power className="w-4 h-4" /> 功能开关
                            </TabsTrigger>
                            <TabsTrigger value="optimize" className="gap-1.5">
                                <Wand2 className="w-4 h-4" /> 文体优化
                            </TabsTrigger>
                            <TabsTrigger value="advanced" className="gap-1.5">
                                <SlidersHorizontal className="w-4 h-4" /> 高级工具
                            </TabsTrigger>
                        </TabsList>

                        <TabsContent value="overview" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-5 mt-4">
                                <div className="rounded-lg border border-emerald-800/40 bg-emerald-950/20 p-4">
                                    <div className="flex items-start justify-between gap-4">
                                        <div>
                                            <h3 className="font-medium text-emerald-100">当前安全状态</h3>
                                            <p className="text-sm text-emerald-200/80 mt-1">
                                                实验内容给客户看保持永久关闭；这里的设置只影响后续写作流程，不会改已经生成的文章。
                                            </p>
                                        </div>
                                        <span className="shrink-0 rounded-full border border-emerald-700 bg-emerald-950/40 px-3 py-1 text-xs text-emerald-100">
                                            客户侧关闭
                                        </span>
                                    </div>
                                </div>

                                <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-xs text-muted-foreground">新模板用于后续文章</div>
                                        <div className="text-2xl font-semibold mt-2">
                                            {styleOverrideEffectivelyEnabled ? "已生效" : styleOverrideSwitch?.enabled ? "使用新版默认" : "未启用"}
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            当前可用模板 {styleOverrideRuntimeActiveCount} 个；没有人工候选时使用新版内置模板。
                                        </p>
                                    </div>
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-xs text-muted-foreground">内部观察</div>
                                        <div className="text-2xl font-semibold mt-2">
                                            {shadowRecordSwitch?.enabled ? "记录中" : "未开启"}
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            已记录 {shadowRunsData?.artifact_count ?? 0} 条，仅管理员可看。
                                        </p>
                                    </div>
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-xs text-muted-foreground">实验内容给客户看</div>
                                        <div className="text-2xl font-semibold mt-2 text-red-300">
                                            {customerOutputSwitch?.locked ? "永久关闭" : "关闭"}
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            这条链路不会把实验内容给客户看。
                                        </p>
                                    </div>
                                </div>

                                <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
                                    <button
                                        type="button"
                                        onClick={() => setActiveSettingsTab("feature-switches")}
                                        className="text-left border rounded-lg p-4 bg-background/40 hover:bg-muted/60 transition-colors"
                                    >
                                        <div className="flex items-center gap-2 font-medium">
                                            <Power className="w-4 h-4 text-emerald-300" />
                                            日常要改功能，就点这里
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            统一管理新模板、文章结构建议、内部观察和媒体推荐。危险项会二次确认。
                                        </p>
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setActiveSettingsTab("optimize")}
                                        className="text-left border rounded-lg p-4 bg-background/40 hover:bg-muted/60 transition-colors"
                                    >
                                        <div className="flex items-center gap-2 font-medium">
                                            <Wand2 className="w-4 h-4 text-blue-300" />
                                            要优化文章风格，就点这里
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            查看飞轮资料、生成待审核候选，再由管理员确认是否启用。
                                        </p>
                                    </button>
                                </div>

                                <div className="rounded-lg border p-4 bg-background/40">
                                    <h3 className="font-medium">运营只需要记住三句话</h3>
                                    <div className="grid grid-cols-1 md:grid-cols-3 gap-3 mt-3 text-sm text-muted-foreground">
                                        <div>1. 开关页显示的就是后端真实状态。</div>
                                        <div>2. 新模板真正生效前，必须先有已启用模板。</div>
                                        <div>3. 实验内容给客户看永久关闭，不在这里开放。</div>
                                    </div>
                                </div>
                            </div>
                        </TabsContent>

                        {/* === Tab 1: 模型设置 === */}
                        <TabsContent value="llm" className="flex-1 px-6 pb-6 overflow-auto">
                            <div className="space-y-6 mt-4">
                                <div className="p-4 bg-blue-50 rounded-lg border border-blue-200">
                                    <p className="text-sm text-blue-700">
                                        <AlertCircle className="w-4 h-4 inline mr-1" />
                                        此处修改的模型配置将在下次生成文章时生效，不影响已生成的文章。
                                    </p>
                                </div>

                                {/* 服务商选择 */}
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">写作服务</label>
                                    <Select value={selectedProvider} onValueChange={(v) => {
                                        setSelectedProvider(v);
                                        const prov = config?.available_providers.find(p => p.id === v);
                                        if (prov && prov.models.length > 0) {
                                            setSelectedModel(prov.models[0]);
                                            setUseCustomModel(false);
                                        }
                                    }}>
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {config?.available_providers.map(p => (
                                                <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                                            ))}
                                        </SelectContent>
                                    </Select>
                                </div>

                                {/* 具体模型选择 */}
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">具体模型</label>
                                    <div className="flex items-center gap-2 mb-2">
                                        <Button
                                            variant={!useCustomModel ? "default" : "outline"}
                                            size="sm"
                                            onClick={() => setUseCustomModel(false)}
                                        >
                                            推荐模型
                                        </Button>
                                        <Button
                                            variant={useCustomModel ? "default" : "outline"}
                                            size="sm"
                                            onClick={() => setUseCustomModel(true)}
                                        >
                                            自定义
                                        </Button>
                                    </div>
                                    {useCustomModel ? (
                                        <Input
                                            value={customModel}
                                            onChange={(e) => setCustomModel(e.target.value)}
                                            placeholder="输入模型名称，如 qwen3-235b-a22b"
                                        />
                                    ) : (
                                        <Select value={selectedModel} onValueChange={setSelectedModel}>
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {currentProvider?.models.map(m => (
                                                    <SelectItem key={m} value={m}>{m}</SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    )}
                                </div>

                                {/* 当前配置预览 */}
                                <div className="p-3 bg-background/40 rounded-lg border text-sm">
                                    <span className="text-muted-foreground">当前写作服务：</span>
                                    <span className="font-mono font-medium ml-1">
                                        {selectedProvider} / {useCustomModel ? customModel : selectedModel}
                                    </span>
                                </div>

                                <Button onClick={saveLLMConfig} disabled={saving} className="w-full">
                                    {saving ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Save className="w-4 h-4 mr-2" />}
                                    保存写作服务
                                </Button>
                            </div>
                        </TabsContent>

                        {/* === Tab 2: 文章模板管理 === */}
                        <TabsContent value="prompts" className="flex-1 overflow-hidden flex flex-col px-6 pb-4">
                            <div className="mt-3 mb-3 rounded-lg border border-amber-800/40 bg-amber-950/20 p-3 text-sm text-amber-100 shrink-0">
                                这里是高级模板编辑区，适合管理员或写作负责人使用。日常只需要在“功能开关”里查看新模板是否真正用于后续文章。
                            </div>
                            <div className={`mb-3 rounded-lg border p-3 text-sm shrink-0 ${styleOverrideEffectivelyEnabled
                                ? "border-emerald-800/40 bg-emerald-950/20 text-emerald-100"
                                : "border-amber-800/40 bg-amber-950/20 text-amber-100"
                                }`}>
                                {styleOverrideEffectivelyEnabled
                                    ? "当前已有启用模板，后续文章会使用管理员确认过的新模板。"
                                    : "当前展示的是新版内置模板；人工候选只会在“文体优化”确认启用后覆盖它。"}
                            </div>
                            <div className="flex gap-4 flex-1 overflow-hidden mt-2">
                                {/* 左侧：风格列表 */}
                                <div className="w-48 shrink-0 border rounded-lg overflow-auto">
                                    {config?.styles.map(style => (
                                        <button
                                            key={style.code}
                                            onClick={() => setActiveStyle(style.code)}
                                            className={`w-full text-left px-3 py-2.5 text-sm border-b last:border-b-0 transition-colors ${activeStyle === style.code
                                                ? "bg-blue-50 text-blue-700 font-medium"
                                                : "hover:bg-background/40"
                                                }`}
                                        >
                                            <div className="flex items-center justify-between">
                                                <span>{style.name}</span>
                                                {style.is_overridden && (
                                                    <span className="text-xs px-1.5 py-0.5 bg-amber-100 text-amber-700 rounded">
                                                        已改
                                                    </span>
                                                )}
                                                {style.code in editedPrompts && (
                                                    <span className="text-xs px-1.5 py-0.5 bg-red-100 text-red-700 rounded">
                                                        未存
                                                    </span>
                                                )}
                                            </div>
                                            <div className="text-xs text-muted-foreground mt-0.5">
                                                {style.ratio}% · {(style.prompt_length / 1000).toFixed(1)}k字
                                            </div>
                                        </button>
                                    ))}
                                </div>

                                {/* 右侧：文章模板编辑 */}
                                <div className="flex-1 flex flex-col overflow-hidden">
                                    {currentStyle && (
                                        <>
                                            <div className="flex items-center justify-between mb-2 shrink-0">
                                                <div>
                                                    <h3 className="font-medium">{currentStyle.name}</h3>
                                                    <p className="text-xs text-muted-foreground">{currentStyle.description}</p>
                                                </div>
                                                <div className="flex gap-2">
                                                    {currentStyle.is_overridden && (
                                                        <Button
                                                            variant="outline"
                                                            size="sm"
                                                            onClick={() => resetPrompt(activeStyle)}
                                                            disabled={saving}
                                                        >
                                                            <RotateCcw className="w-3.5 h-3.5 mr-1" />
                                                            重置默认
                                                        </Button>
                                                    )}
                                                    <Button
                                                        size="sm"
                                                        onClick={() => savePrompt(activeStyle)}
                                                        disabled={saving || !hasUnsavedChanges}
                                                    >
                                                        {saving ? (
                                                            <Loader2 className="w-3.5 h-3.5 animate-spin mr-1" />
                                                        ) : (
                                                            <Save className="w-3.5 h-3.5 mr-1" />
                                                        )}
                                                        保存
                                                    </Button>
                                                </div>
                                            </div>
                                            <Textarea
                                                value={currentPrompt}
                                                onChange={(e) => setEditedPrompts(prev => ({
                                                    ...prev,
                                                    [activeStyle]: e.target.value
                                                }))}
                                                className="flex-1 font-mono text-xs leading-relaxed resize-none"
                                                placeholder="文章模板内容..."
                                            />
                                            <div className="flex items-center justify-between mt-1.5 text-xs text-muted-foreground shrink-0">
                                                <span>{currentPrompt.length.toLocaleString()} 字符</span>
                                                {hasUnsavedChanges && (
                                                    <span className="text-amber-600">● 有未保存的修改</span>
                                                )}
                                            </div>
                                        </>
                                    )}
                                </div>
                            </div>
                        </TabsContent>

                        {/* === 功能开关 === */}
                        <TabsContent value="feature-switches" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-4 mt-4">
                                <div className="flex items-center justify-between gap-3">
                                    <div>
                                        <h3 className="font-medium">功能开关</h3>
                                        <p className="text-xs text-muted-foreground mt-0.5">
                                            这里显示真实后台状态。打开或关闭后，会影响之后的新文章，不影响已经生成的文章。
                                        </p>
                                    </div>
                                    <Button variant="outline" size="sm" onClick={loadFeatureSwitches} disabled={featureSwitchLoading}>
                                        {featureSwitchLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <RefreshCw className="w-3.5 h-3.5 mr-1.5" />}
                                        刷新
                                    </Button>
                                </div>

                                {featureSwitchError && (
                                    <div className="p-3 bg-red-50 rounded-lg border border-red-200 text-sm text-red-700">
                                        <AlertCircle className="w-4 h-4 inline mr-1" />
                                        {featureSwitchError}
                                    </div>
                                )}

                                <div className="grid grid-cols-3 gap-3">
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">设置版本</div>
                                        <div className="text-xl font-semibold mt-1">{featureSwitchData?.config_version ?? "-"}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">已开启功能</div>
                                        <div className="text-xl font-semibold mt-1">
                                            {enabledOperatorSwitchCount}
                                        </div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">永久关闭</div>
                                        <div className="text-xl font-semibold mt-1">
                                            {lockedSwitchCount}
                                        </div>
                                    </div>
                                </div>

                                <div className="space-y-3">
                                    {featureSwitches.map(item => (
                                        <div key={item.key} className="border rounded-lg p-4 bg-background/40">
                                            <div className="flex items-start justify-between gap-4">
                                                <div className="min-w-0">
                                                    <div className="flex items-center gap-2">
                                                        <div className="font-medium">{item.label}</div>
                                                        <span className={`text-xs px-2 py-0.5 rounded border ${featureSwitchStateClass(item)}`}>
                                                            {featureSwitchStateLabel(item)}
                                                        </span>
                                                        {item.dangerous && (
                                                            <span className="text-xs px-2 py-0.5 rounded border border-amber-700 bg-amber-950/30 text-amber-200">
                                                                请再次确认
                                                            </span>
                                                        )}
                                                    </div>
                                                    <p className="text-xs text-muted-foreground mt-1">{item.description}</p>
                                                    <p className="text-xs text-muted-foreground mt-2">开启/关闭影响: {item.consequence}</p>
                                                    {item.key === "writing_style_overrides" && (
                                                        <div className="mt-2 space-y-1">
                                                            <p className="text-xs text-muted-foreground">
                                                                当前可用模板: {item.runtime?.active_style_count ?? 0} 个；后续文章是否使用新版模板: {item.runtime?.effective_enabled ? "会" : "不会"}
                                                            </p>
                                                            {item.enabled && !item.runtime?.effective_enabled && (
                                                                <p className="text-xs text-amber-200">
                                                                    总开关已开，但还没有可用模板；请先刷新或检查后台配置。
                                                                </p>
                                                            )}
                                                        </div>
                                                    )}
                                                    {item.key === "r6h_customer_output" && (
                                                        <p className="text-xs text-red-300 mt-2">
                                                            永久关闭: 不提供开启入口。需要老板单独授权和新部署方案才可重新评估。
                                                        </p>
                                                    )}
                                                </div>
                                                <Switch
                                                    checked={item.enabled}
                                                    disabled={featureSwitchLoading || item.locked}
                                                    onCheckedChange={(checked) => updateFeatureSwitch(item, checked)}
                                                    aria-label={`${item.label}开关`}
                                                />
                                            </div>
                                        </div>
                                    ))}
                                    {featureSwitches.length === 0 && (
                                        <div className="border rounded-lg p-6 text-sm text-muted-foreground text-center">
                                            暂无功能开关配置
                                        </div>
                                    )}
                                </div>
                            </div>
                        </TabsContent>

                        <TabsContent value="optimize" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-5 mt-4">
                                <div className="flex items-center justify-between gap-3">
                                    <div>
                                        <h3 className="font-medium">文体优化</h3>
                                        <p className="text-sm text-muted-foreground mt-1">
                                            这里看整体文章经验，不需要先选行业。系统会优先使用全量文章结构库，再结合候选版本做人工审核。
                                        </p>
                                    </div>
                                    <Button variant="outline" size="sm" onClick={() => loadStyleControl()} disabled={styleControlLoading}>
                                        {styleControlLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <RefreshCw className="w-3.5 h-3.5 mr-1.5" />}
                                        刷新
                                    </Button>
                                </div>

                                <div className="grid grid-cols-1 md:grid-cols-4 gap-3">
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-xs text-muted-foreground">文章结构库</div>
                                        <div className="text-2xl font-semibold mt-2">
                                            {flywheelSummary?.article_structure?.loaded ?? 0}
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">用于总结整体文章写法。</p>
                                    </div>
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-xs text-muted-foreground">待审核候选</div>
                                        <div className="text-2xl font-semibold mt-2">{draftCount}</div>
                                        <p className="text-xs text-muted-foreground mt-2">生成后不会自动上线。</p>
                                    </div>
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-xs text-muted-foreground">当前启用</div>
                                        <div className="text-2xl font-semibold mt-2">{activeCount}</div>
                                        <p className="text-xs text-muted-foreground mt-2">只影响后续文章。</p>
                                    </div>
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-xs text-muted-foreground">未通过</div>
                                        <div className="text-2xl font-semibold mt-2">{blockedCount}</div>
                                        <p className="text-xs text-muted-foreground mt-2">不会被启用。</p>
                                    </div>
                                </div>

                                <div className="rounded-lg border p-4 bg-background/40">
                                    <div className="flex items-start justify-between gap-3">
                                        <div>
                                            <h3 className="font-medium">我们之前优化了什么</h3>
                                            <p className="text-sm text-muted-foreground mt-1">
                                                这些是多轮长文测试、规则校验和三方语义复核沉淀下来的问题清单。这里展示结果摘要，不需要运营去翻后端证据链。
                                            </p>
                                        </div>
                                        <span className="shrink-0 text-xs px-2 py-1 rounded border bg-emerald-950/30 text-emerald-200 border-emerald-800">
                                            已接入审核流程
                                        </span>
                                    </div>
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-3 mt-4">
                                        {optimizationIssueCards.map(item => (
                                            <div key={item.title} className="rounded-lg border bg-background p-3">
                                                <div className="font-medium text-sm">{item.title}</div>
                                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 mt-3 text-xs">
                                                    <div className="rounded-md bg-red-950/20 border border-red-900/50 p-2">
                                                        <div className="font-medium text-red-200">旧问题</div>
                                                        <p className="text-muted-foreground mt-1 leading-relaxed">{item.before}</p>
                                                    </div>
                                                    <div className="rounded-md bg-emerald-950/20 border border-emerald-900/50 p-2">
                                                        <div className="font-medium text-emerald-200">现在怎么防住</div>
                                                        <p className="text-muted-foreground mt-1 leading-relaxed">{item.after}</p>
                                                    </div>
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>

                                <div className="rounded-lg border p-4 bg-background/40">
                                    <h3 className="font-medium">新旧模板对比</h3>
                                    <p className="text-sm text-muted-foreground mt-1">
                                        左侧是永久回退版本，右侧是当前选中的候选或启用版本。只有管理员确认后，新模板才会影响后续文章。
                                    </p>
                                    <div className="grid grid-cols-1 lg:grid-cols-2 gap-3 mt-4">
                                        <div className="rounded-lg border bg-background p-4">
                                            <div className="text-xs text-muted-foreground">旧模板 / 默认回退</div>
                                            <div className="font-medium mt-2 truncate">
                                                {baselineVersion?.style_name || selectedVersion?.style_name || "系统默认模板"}
                                            </div>
                                            <div className="text-xs text-muted-foreground mt-1 truncate">
                                                {baselineVersion ? "永久安全回退" : "代码默认版本"}
                                            </div>
                                            <div className="grid grid-cols-2 gap-2 mt-3 text-sm">
                                                <div className="text-muted-foreground">测试分数</div>
                                                <div>{baselineScoreText}</div>
                                                <div className="text-muted-foreground">样本/对照</div>
                                                <div>{baselineVersion ? `${baselineVersion.sample_count}/${baselineVersion.control_sample_count}` : "0/0"}</div>
                                            </div>
                                            <p className="text-xs text-muted-foreground mt-3">
                                                这是安全回退根，不会被退役；出问题可以随时回到这里。
                                            </p>
                                        </div>
                                        <div className="rounded-lg border bg-background p-4">
                                            <div className="text-xs text-muted-foreground">新模板 / 当前候选</div>
                                            <div className="font-medium mt-2 truncate">
                                                {selectedVersion?.style_name || "暂无候选"}
                                            </div>
                                            <div className="text-xs text-muted-foreground mt-1 truncate">
                                                {selectedVersion ? statusLabel(selectedVersion.status) : "请先生成候选"}
                                            </div>
                                            <div className="grid grid-cols-2 gap-2 mt-3 text-sm">
                                                <div className="text-muted-foreground">状态</div>
                                                <div>{statusLabel(selectedVersion?.status)}</div>
                                                <div className="text-muted-foreground">测试分数</div>
                                                <div>{selectedVersionScoreText}</div>
                                                <div className="text-muted-foreground">样本/对照</div>
                                                <div>{selectedVersion ? `${selectedVersion.sample_count}/${selectedVersion.control_sample_count}` : "0/0"}</div>
                                            </div>
                                            <p className="text-xs text-muted-foreground mt-3">
                                                {selectedVersion?.rollout_recommendation?.reason || "生成候选后，这里会显示审核结果和推荐理由。"}
                                            </p>
                                        </div>
                                    </div>
                                </div>

                                <div className="rounded-lg border p-4 bg-background/40">
                                    <h3 className="font-medium">一键流程</h3>
                                    <p className="text-sm text-muted-foreground mt-1">
                                        生成、看分数、看内部测试和启用回退都在这里进。当前不会在线上实时调用三方模型，不伪造测试通过；分数来自已有规则校验、语义复核和内部观测结果。
                                    </p>
                                    <div className="grid grid-cols-1 md:grid-cols-4 gap-3 mt-3">
                                        <Button variant="outline" onClick={createAlignedDraft} disabled={saving || !styleControlData}>
                                            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <Wand2 className="w-3.5 h-3.5 mr-1.5" />}
                                            一键生成候选
                                        </Button>
                                        <Button variant="outline" onClick={() => setActiveSettingsTab("evidence")}>
                                            <Search className="w-3.5 h-3.5 mr-1.5" />
                                            查看测试分数
                                        </Button>
                                        <Button variant="outline" onClick={() => setActiveSettingsTab("shadow")}>
                                            <ShieldCheck className="w-3.5 h-3.5 mr-1.5" />
                                            查看内部测试记录
                                        </Button>
                                        <Button variant="outline" onClick={() => setActiveSettingsTab("release")}>
                                            <Rocket className="w-3.5 h-3.5 mr-1.5" />
                                            启用或回退
                                        </Button>
                                    </div>
                                    <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
                                        <span className="rounded-full border px-2 py-1">候选不会自动上线</span>
                                        <span className="rounded-full border px-2 py-1">测试分数来自已有证据</span>
                                        <span className="rounded-full border px-2 py-1">实验内容给客户看仍然永久关闭</span>
                                    </div>
                                </div>
                            </div>
                        </TabsContent>

                        {/* === Tab 3: 文体版本 === */}
                        <TabsContent value="versions" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-4 mt-4">
                                <div className="flex items-center justify-between gap-3">
                                    <div>
                                        <h3 className="font-medium">文体版本</h3>
                                        <p className="text-xs text-muted-foreground mt-0.5">
                                            结构策略由飞轮沉淀，文章模板是可回退的候选版本；启用前需要管理员审核。
                                        </p>
                                    </div>
                                    <Button variant="outline" size="sm" onClick={() => loadStyleControl()} disabled={styleControlLoading}>
                                        {styleControlLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <RefreshCw className="w-3.5 h-3.5 mr-1.5" />}
                                        刷新
                                    </Button>
                                </div>

                                {styleControlError && (
                                    <div className="p-3 bg-red-50 rounded-lg border border-red-200 text-sm text-red-700">
                                        <AlertCircle className="w-4 h-4 inline mr-1" />
                                        {styleControlError}
                                    </div>
                                )}

                                <div className="grid grid-cols-5 gap-3">
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">配置版本</div>
                                        <div className="text-xl font-semibold mt-1">{styleControlData?.config_version ?? "-"}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">当前启用</div>
                                        <div className="text-xl font-semibold mt-1">{activeCount}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">待审核候选</div>
                                        <div className="text-xl font-semibold mt-1">{draftCount}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">默认回退版本</div>
                                        <div className="text-xl font-semibold mt-1">{stableCount}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">未通过</div>
                                        <div className="text-xl font-semibold mt-1">{blockedCount}</div>
                                    </div>
                                </div>

                                <div className="border rounded-lg overflow-hidden">
                                    <div className="grid grid-cols-[1.2fr_0.9fr_0.8fr_0.8fr_0.8fr_1.1fr_0.9fr] gap-2 px-3 py-2 bg-background/40 border-b text-xs font-medium text-muted-foreground">
                                        <span>版本</span>
                                        <span>文体</span>
                                        <span>状态</span>
                                        <span>来源</span>
                                        <span>样本</span>
                                        <span>规则校验</span>
                                        <span>操作</span>
                                    </div>
                                    <div className="max-h-[360px] overflow-auto">
                                        {styleVersions.length === 0 ? (
                                            <div className="px-3 py-8 text-sm text-muted-foreground text-center">暂无版本</div>
                                        ) : styleVersions.map((version) => (
                                            <div
                                                key={version.version_id}
                                                className={`grid grid-cols-[1.2fr_0.9fr_0.8fr_0.8fr_0.8fr_1.1fr_0.9fr] gap-2 px-3 py-2.5 border-b last:border-b-0 text-sm ${selectedVersionId === version.version_id ? "bg-blue-950/30 text-foreground ring-1 ring-blue-500/40" : ""}`}
                                            >
                                                <div className="min-w-0">
                                                    <div className="font-medium truncate">{versionDisplayName(version)}</div>
                                                    <div className="text-xs text-muted-foreground mt-0.5 truncate">{version.strategy_summary || "暂无摘要"}</div>
                                                </div>
                                                <span className="self-center truncate">{version.style_name || version.style_code}</span>
                                                <span className={`self-center w-fit text-xs px-2 py-1 rounded border ${statusBadgeClass(version.status)}`}>
                                                    {statusLabel(version.status)}
                                                </span>
                                                <span className="self-center text-xs text-muted-foreground">{sourceLabel(version.source)}</span>
                                                <span className="self-center text-xs">{version.sample_count}/{version.control_sample_count}</span>
                                                <span className={`self-center text-xs ${version.guard_summary?.worst_action === "BLOCK" ? "text-red-700" : "text-green-700"}`}>
                                                    {guardDecisionLabel(version.guard_summary?.decision)} · {version.guard_summary?.finding_count ?? 0}
                                                </span>
                                                <Button
                                                    variant="outline"
                                                    size="sm"
                                                    className="h-7 px-2 self-center"
                                                    onClick={async () => {
                                                        setSelectedVersionId(version.version_id);
                                                        await loadEvidenceChain(version.version_id);
                                                    }}
                                                >
                                                    查看
                                                </Button>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </div>
                        </TabsContent>

                        {/* === Tab 4: 飞轮资料 === */}
                        <TabsContent value="flywheel" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-4 mt-4">
                                <div className="flex items-center justify-between gap-3">
                                    <div>
                                        <h3 className="font-medium">飞轮资料</h3>
                                        <p className="text-xs text-muted-foreground mt-0.5">
                                            写作优化默认看全部文章，不跟随行业筛选；行业只作为生成候选时的说明标签。
                                        </p>
                                    </div>
                                    <Button variant="outline" size="sm" onClick={() => loadStyleControl(alignStyleCode, alignIndustryKey)} disabled={styleControlLoading}>
                                        {styleControlLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" /> : <RefreshCw className="w-3.5 h-3.5 mr-1.5" />}
                                        刷新资料
                                    </Button>
                                </div>

                                <div className="grid grid-cols-[1fr_1fr] gap-3">
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium">候选标签</label>
                                        <Select value={alignIndustryKey} onValueChange={setAlignIndustryKey}>
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {GEO_INDUSTRY_OPTIONS_WITH_GENERAL.map(option => (
                                                    <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium">文体</label>
                                        <Select value={alignStyleCode} onValueChange={setAlignStyleCode}>
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {(config?.styles ?? []).map(style => (
                                                    <SelectItem key={style.code} value={style.code}>{style.name}</SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                </div>

                                <div className="grid grid-cols-4 gap-3">
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">已读取文章</div>
                                        <div className="text-xl font-semibold mt-1">
                                            {settingsArticleStructureLoaded}
                                        </div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">采纳/引用样本</div>
                                        <div className="text-xl font-semibold mt-1">
                                            {formatFlywheelCount(flywheelSummary?.sample_count, flywheelSummary?.sample_count_status)}
                                        </div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">参考/对照文章</div>
                                        <div className="text-xl font-semibold mt-1">
                                            {formatFlywheelCount(flywheelSummary?.control_sample_count, flywheelSummary?.control_sample_count_status)}
                                        </div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">内部测试通过</div>
                                        <div className="text-xl font-semibold mt-1">{Math.round((flywheelSummary?.shadow_pass_rate ?? 0) * 100)}%</div>
                                    </div>
                                </div>

                                <div className="border rounded-lg p-4 bg-background/40">
                                    <div className="flex items-center justify-between gap-3">
                                        <div>
                                            <div className="text-sm font-medium">当前资料状态</div>
                                            <div className="text-xs text-muted-foreground mt-1">
                                                {settingsArticleStructureLoaded > 0
                                                    ? `已读取 ${settingsArticleStructureLoaded} 篇文章，其中研究文章 ${settingsResearchArticleCount} 篇、正式生成文章 ${settingsGeneratedArticleCount} 篇。`
                                                    : flywheelSummary?.eligibility_reason || "等待飞轮资料；没有具名样本字段时会读取线上文章结构库和正式文章表。"}
                                            </div>
                                        </div>
                                        <span className="text-xs px-2 py-1 rounded border bg-background">
                                            {eligibilityLabel(flywheelSummary?.eligibility)}
                                        </span>
                                    </div>
                                    <div className="mt-3 text-xs text-muted-foreground">
                                        数据来源: {flywheelSummary?.latest_candidate === "article_structure_service" ? "线上文章结构库 + 正式文章表" : "本地飞轮摘要"} ·
                                        来源数量 {flywheelSummary?.source_file_count ?? 0}
                                    </div>
                                </div>

                                {flywheelSummary?.article_structure && (
                                    <div className="border rounded-lg p-4">
                                        <div className="flex items-center justify-between gap-3">
                                            <div>
                                                <div className="text-sm font-medium">文章结构研究</div>
                                                <div className="text-xs text-muted-foreground mt-1">
                                                    已读取 {flywheelSummary.article_structure.loaded ?? 0} 篇文章；先看整体写法，再用采纳/引用文章给推荐加权。
                                                </div>
                                            </div>
                                            <span className="text-xs px-2 py-1 rounded border bg-background/40">
                                                {flywheelSummary.article_structure.sample_status || "样本观察中"}
                                            </span>
                                        </div>
                                        {(flywheelSummary.article_structure.recommended_rules ?? []).length > 0 && (
                                            <div className="mt-3 grid gap-2">
                                                {flywheelSummary.article_structure.recommended_rules!.slice(0, 5).map(rule => (
                                                    <div key={rule} className="text-sm flex gap-2">
                                                        <Check className="w-4 h-4 text-green-700 shrink-0 mt-0.5" />
                                                        <span>{rule}</span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                        {(flywheelSummary.article_structure.feature_lift ?? []).length > 0 && (
                                            <div className="mt-4 border rounded-lg overflow-hidden">
                                                <div className="grid grid-cols-[1.2fr_0.6fr_0.6fr_0.6fr] gap-2 px-3 py-2 bg-background/40 text-xs font-medium text-muted-foreground">
                                                    <span>结构特征</span>
                                                    <span>采纳组</span>
                                                    <span>对照组</span>
                                                    <span>提升</span>
                                                </div>
                                                {flywheelSummary.article_structure.feature_lift!.slice(0, 6).map(item => (
                                                    <div key={item.label} className="grid grid-cols-[1.2fr_0.6fr_0.6fr_0.6fr] gap-2 px-3 py-2 border-t text-sm">
                                                        <span>{item.label}</span>
                                                        <span>{Math.round((item.adopted_share ?? 0) * 100)}%</span>
                                                        <span>{Math.round((item.control_share ?? 0) * 100)}%</span>
                                                        <span>{(item.lift ?? 0).toFixed(1)}x</span>
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>
                                )}
                            </div>
                        </TabsContent>

                        {/* === Tab 5: 一键对齐 === */}
                        <TabsContent value="align" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-4 mt-4">
                                <div className="p-4 bg-amber-50 rounded-lg border border-amber-200">
                                    <p className="text-sm text-amber-800">
                                        <AlertCircle className="w-4 h-4 inline mr-1" />
                                        一键对齐只生成待审核候选，不直接启用，不覆盖默认回退版本，不会让实验内容给客户看。
                                    </p>
                                </div>

                                <div className="grid grid-cols-4 gap-3">
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium">文体</label>
                                        <Select value={alignStyleCode} onValueChange={setAlignStyleCode}>
                                            <SelectTrigger><SelectValue /></SelectTrigger>
                                            <SelectContent>
                                                {(config?.styles ?? []).map(style => (
                                                    <SelectItem key={style.code} value={style.code}>{style.name}</SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium">行业</label>
                                        <Select value={alignIndustryKey} onValueChange={setAlignIndustryKey}>
                                            <SelectTrigger>
                                                <SelectValue>{geoIndustryLabel(alignIndustryKey)}</SelectValue>
                                            </SelectTrigger>
                                            <SelectContent>
                                                {GEO_INDUSTRY_OPTIONS_WITH_GENERAL.map(option => (
                                                    <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium">证据模式</label>
                                        <Select value={alignEvidenceMode} onValueChange={setAlignEvidenceMode}>
                                            <SelectTrigger><SelectValue /></SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="with_evidence">有证据资料</SelectItem>
                                                <SelectItem value="no_evidence">无证据资料</SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <label className="text-sm font-medium">风险级别</label>
                                        <Select value={alignRiskLevel} onValueChange={setAlignRiskLevel}>
                                            <SelectTrigger><SelectValue /></SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="normal">标准风险</SelectItem>
                                                <SelectItem value="high">高责任行业</SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>
                                </div>

                                <div className="grid grid-cols-[1fr_1fr] gap-3">
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-sm font-medium">对齐依据</div>
                                        <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                                            <div className="text-muted-foreground">采纳样本</div>
                                            <div>{formatFlywheelCount(flywheelSummary?.sample_count, flywheelSummary?.sample_count_status)}</div>
                                            <div className="text-muted-foreground">对照样本</div>
                                            <div>{formatFlywheelCount(flywheelSummary?.control_sample_count, flywheelSummary?.control_sample_count_status)}</div>
                                            <div className="text-muted-foreground">门槛</div>
                                            <div>{eligibilityLabel(flywheelSummary?.eligibility)}</div>
                                            <div className="text-muted-foreground">配置版本</div>
                                            <div>{styleControlData?.config_version ?? "-"}</div>
                                        </div>
                                    </div>
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-sm font-medium">规则校验分级</div>
                                        <div className="mt-3 space-y-1.5 text-xs text-muted-foreground">
                                            <div>阻断: 内部字段、伪来源、无证据价格/百分比/案例、高责任承诺。</div>
                                            <div>需复核: 结构不足或候选过短，需要重试或人工。</div>
                                            <div>提醒: 对冲泛化与非阻断提醒，仍进入人工证据链。</div>
                                        </div>
                                    </div>
                                </div>

                                <Button onClick={createAlignedDraft} disabled={saving || !styleControlData} className="w-full">
                                    {saving ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Wand2 className="w-4 h-4 mr-2" />}
                                    生成待审核候选
                                </Button>
                            </div>
                        </TabsContent>

                        {/* === Tab 6: 证据链 === */}
                        <TabsContent value="evidence" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-4 mt-4">
                                <div className="flex items-center justify-between gap-3">
                                    <div>
                                        <h3 className="font-medium">证据链</h3>
                                        <p className="text-xs text-muted-foreground mt-0.5">
                                            只展示可审计摘要，不展示文章模板正文。
                                        </p>
                                    </div>
                                    <Select value={selectedVersionId} onValueChange={async (versionId) => {
                                        setSelectedVersionId(versionId);
                                        await loadEvidenceChain(versionId);
                                    }}>
                                        <SelectTrigger className="w-[360px]">
                                            <SelectValue placeholder="选择版本" />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {styleVersions.map(version => (
                                                <SelectItem key={version.version_id} value={version.version_id}>
                                                    {versionDisplayName(version)}
                                                </SelectItem>
                                            ))}
                                        </SelectContent>
                                    </Select>
                                </div>

                                {selectedVersion && (
                                    <div className="grid grid-cols-4 gap-3">
                                        <div className="border rounded-lg p-3 bg-background/40">
                                            <div className="text-xs text-muted-foreground">版本状态</div>
                                            <div className={`mt-1 w-fit text-xs px-2 py-1 rounded border ${statusBadgeClass(selectedVersion.status)}`}>{statusLabel(selectedVersion.status)}</div>
                                        </div>
                                        <div className="border rounded-lg p-3 bg-background/40">
                                            <div className="text-xs text-muted-foreground">规则校验</div>
                                            <div className="text-sm font-semibold mt-1">{guardDecisionLabel(selectedVersion.guard_summary?.decision)}</div>
                                        </div>
                                        <div className="border rounded-lg p-3 bg-background/40">
                                            <div className="text-xs text-muted-foreground">语义复核</div>
                                            <div className="text-sm font-semibold mt-1">{semanticDecisionLabel(selectedVersion.judge_summary?.semantic_decision)}</div>
                                        </div>
                                        <div className="border rounded-lg p-3 bg-background/40">
                                            <div className="text-xs text-muted-foreground">实验内容</div>
                                            <div className="text-sm font-semibold mt-1 text-green-700">关闭</div>
                                        </div>
                                    </div>
                                )}

                                <div className="border rounded-lg p-4 bg-background/40">
                                    <div className="text-sm font-medium">证据摘要</div>
                                    <div className="mt-2 text-sm text-muted-foreground leading-relaxed">
                                        {evidenceChain?.evidence_chain?.summary || "请选择版本查看证据链"}
                                    </div>
                                    <div className="mt-3 flex flex-wrap gap-2">
                                        {Object.entries(evidenceChain?.evidence_chain?.sample_threshold ?? {}).map(([key, value]) => (
                                            <span key={key} className="text-xs px-2 py-1 rounded border bg-background">
                                                {sampleThresholdLabel(key)}: {String(value)}
                                            </span>
                                        ))}
                                    </div>
                                </div>

                                <div className="grid grid-cols-[1fr_1fr] gap-3">
                                    <div className="border rounded-lg p-4">
                                        <div className="text-sm font-medium mb-2">为什么更好</div>
                                        <div className="space-y-2">
                                            {(evidenceChain?.evidence_chain?.why_new_is_better ?? []).length === 0 ? (
                                                <div className="text-sm text-muted-foreground">暂无说明</div>
                                            ) : (
                                                evidenceChain?.evidence_chain.why_new_is_better.map((item) => (
                                                    <div key={item} className="text-sm flex gap-2">
                                                        <Check className="w-4 h-4 text-green-700 shrink-0 mt-0.5" />
                                                        <span>{item}</span>
                                                    </div>
                                                ))
                                            )}
                                        </div>
                                    </div>
                                    <div className="border rounded-lg p-4">
                                        <div className="text-sm font-medium mb-2">审计日志</div>
                                        <div className="space-y-2 max-h-52 overflow-auto">
                                            {auditEvents.length === 0 ? (
                                                <div className="text-sm text-muted-foreground">暂无审计事件</div>
                                            ) : (
                                                auditEvents.slice().reverse().slice(0, 10).map((event, index) => (
                                                    <div key={`${event.created_at}-${event.version_id}-${index}`} className="text-xs border-b last:border-b-0 pb-2">
                                                        <div className="font-medium">{auditActionLabel(event.action)} · {event.note || "无备注"}</div>
                                                        <div className="text-muted-foreground mt-0.5">{event.created_at} · {statusLabel(event.to_status || event.from_status || "")}</div>
                                                    </div>
                                                ))
                                            )}
                                        </div>
                                    </div>
                                </div>
                            </div>
                        </TabsContent>

                        {/* === Tab 7: 发布与回退 === */}
                        <TabsContent value="release" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-4 mt-4">
                                <div className="p-4 bg-blue-50 rounded-lg border border-blue-200">
                                    <p className="text-sm text-blue-800">
                                        <ShieldCheck className="w-4 h-4 inline mr-1" />
                                        设为当前启用只影响管理员控制的写作覆盖配置；实验内容给客户看始终关闭，不改变客户能看到内容的安全限制。
                                    </p>
                                </div>

                                <div className="grid grid-cols-[1.2fr_0.8fr] gap-3">
                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-sm font-medium">当前选择</div>
                                        {selectedVersion ? (
                                            <div className="mt-3 space-y-2 text-sm">
                                                <div className="flex justify-between gap-3">
                                                    <span className="text-muted-foreground">版本</span>
                                                    <span className="text-xs truncate">{versionDisplayName(selectedVersion)}</span>
                                                </div>
                                                <div className="flex justify-between gap-3">
                                                    <span className="text-muted-foreground">文体</span>
                                                    <span>{selectedVersion.style_name}</span>
                                                </div>
                                                <div className="flex justify-between gap-3">
                                                    <span className="text-muted-foreground">状态</span>
                                                    <span className={`text-xs px-2 py-1 rounded border ${statusBadgeClass(selectedVersion.status)}`}>{statusLabel(selectedVersion.status)}</span>
                                                </div>
                                                <div className="flex justify-between gap-3">
                                                    <span className="text-muted-foreground">当前是否启用</span>
                                                    <span className="text-xs truncate">{activeStyleVersionId === selectedVersion.version_id ? "当前已启用" : "未启用"}</span>
                                                </div>
                                                <div className="flex justify-between gap-3">
                                                    <span className="text-muted-foreground">回退根</span>
                                                    <span className="text-xs truncate">{styleControlData?.stable_by_style?.[selectedVersion.style_code] ? "系统默认模板" : "-"}</span>
                                                </div>
                                            </div>
                                        ) : (
                                            <div className="mt-3 text-sm text-muted-foreground">请选择一个版本</div>
                                        )}
                                    </div>

                                    <div className="border rounded-lg p-4 bg-background/40">
                                        <div className="text-sm font-medium">发布护栏</div>
                                        <div className="mt-3 space-y-2 text-xs text-muted-foreground">
                                            <div>启用前先过规则校验；阻断项不允许启用。</div>
                                            <div>回退根固定为代码默认版本。</div>
                                            <div>所有写接口要求管理员权限与配置版本号校验。</div>
                                            <div>所有操作写入审计日志。</div>
                                        </div>
                                    </div>
                                </div>

                                <div className="space-y-2">
                                    <label className="text-sm font-medium">操作备注</label>
                                    <Input value={operationNote} onChange={(e) => setOperationNote(e.target.value)} placeholder="例如: 真实客户内部观测通过后启用" />
                                </div>

                                <div className="grid grid-cols-3 gap-3">
                                    <Button onClick={activateSelectedVersion} disabled={saving || !canActivateSelected}>
                                        {saving ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Rocket className="w-4 h-4 mr-2" />}
                                        设为当前启用
                                    </Button>
                                    <Button variant="outline" onClick={() => rollbackSelectedStyle()} disabled={saving || !selectedVersion}>
                                        <RotateCcw className="w-4 h-4 mr-2" />
                                        回退默认版本
                                    </Button>
                                    <Button variant="outline" onClick={retireSelectedVersion} disabled={saving || !canRetireSelected}>
                                        <History className="w-4 h-4 mr-2" />
                                        退役候选
                                    </Button>
                                </div>
                            </div>
                        </TabsContent>

                        {/* === Tab 3: 运行参数 === */}
                        <TabsContent value="runtime" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-6 mt-4">
                                <div className="p-4 bg-blue-50 rounded-lg border border-blue-200">
                                    <p className="text-sm text-blue-700">
                                        <AlertCircle className="w-4 h-4 inline mr-1" />
                                        这些参数同时影响写作大厅和补发助手，修改后立即生效。
                                    </p>
                                </div>

                                {/* 并发数 */}
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">并发写手数</label>
                                    <p className="text-xs text-muted-foreground">同时调用几个 API 并行生成文章</p>
                                    <Select value={concurrentWriters.toString()} onValueChange={(v) => setConcurrentWriters(parseInt(v))}>
                                        <SelectTrigger className="w-48">
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="1">1 - 最稳定</SelectItem>
                                            <SelectItem value="3">3 - 保守</SelectItem>
                                            <SelectItem value="5">5 - 稳定（默认）</SelectItem>
                                            <SelectItem value="10">10 - 高速（阿里云推荐上限）</SelectItem>
                                            <SelectItem value="15">15 - 极速（需高配额）</SelectItem>
                                        </SelectContent>
                                    </Select>
                                </div>

                                {/* 超时时间 */}
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">单篇超时时间（秒）</label>
                                    <p className="text-xs text-muted-foreground">单篇文章生成超过此时间将自动放弃</p>
                                    <Select value={articleTimeout.toString()} onValueChange={(v) => setArticleTimeout(parseInt(v))}>
                                        <SelectTrigger className="w-48">
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="120">120秒（2分钟）</SelectItem>
                                            <SelectItem value="180">180秒（3分钟）</SelectItem>
                                            <SelectItem value="300">300秒（5分钟）</SelectItem>
                                            <SelectItem value="600">600秒（10分钟）</SelectItem>
                                        </SelectContent>
                                    </Select>
                                </div>

                                {/* 重试次数 */}
                                <div className="space-y-2">
                                    <label className="text-sm font-medium">失败重试次数</label>
                                    <p className="text-xs text-muted-foreground">文章生成失败后自动重试的次数</p>
                                    <Select value={articleRetryCount.toString()} onValueChange={(v) => setArticleRetryCount(parseInt(v))}>
                                        <SelectTrigger className="w-48">
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="0">不重试</SelectItem>
                                            <SelectItem value="1">1 次</SelectItem>
                                            <SelectItem value="2">2 次</SelectItem>
                                            <SelectItem value="3">3 次</SelectItem>
                                        </SelectContent>
                                    </Select>
                                </div>

                                {/* 当前配置预览 */}
                                <div className="p-3 bg-background/40 rounded-lg border text-sm">
                                    <span className="text-muted-foreground">当前配置：</span>
                                    <span className="font-mono font-medium ml-1">
                                        并发 {concurrentWriters} · 超时 {articleTimeout}s · 重试 {articleRetryCount}次
                                    </span>
                                </div>

                                <Button onClick={saveRuntimeConfig} disabled={saving} className="w-full">
                                    {saving ? <Loader2 className="w-4 h-4 animate-spin mr-2" /> : <Save className="w-4 h-4 mr-2" />}
                                    保存运行参数
                                </Button>
                            </div>
                        </TabsContent>

                        <TabsContent value="advanced" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-5 mt-4">
                                <div>
                                    <h3 className="font-medium">高级工具</h3>
                                    <p className="text-sm text-muted-foreground mt-1">
                                        这些入口主要给管理员排查或精细调整使用。日常运营一般只需要“日常总览”和“功能开关”。
                                    </p>
                                </div>
                                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                                    <button
                                        type="button"
                                        onClick={() => setActiveSettingsTab("llm")}
                                        className="text-left border rounded-lg p-4 bg-background/40 hover:bg-muted/60 transition-colors"
                                    >
                                        <div className="flex items-center gap-2 font-medium">
                                            <Cpu className="w-4 h-4" />
                                            生成模型
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            调整下次生成文章使用的模型服务商和模型名称。
                                        </p>
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setActiveSettingsTab("runtime")}
                                        className="text-left border rounded-lg p-4 bg-background/40 hover:bg-muted/60 transition-colors"
                                    >
                                        <div className="flex items-center gap-2 font-medium">
                                            <SlidersHorizontal className="w-4 h-4" />
                                            生成速度与重试
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            调整并发数量、单篇等待时间和失败重试次数。
                                        </p>
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setActiveSettingsTab("prompts")}
                                        className="text-left border rounded-lg p-4 bg-background/40 hover:bg-muted/60 transition-colors"
                                    >
                                        <div className="flex items-center gap-2 font-medium">
                                            <FileText className="w-4 h-4" />
                                            高级模板（慎用）
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            查看或编辑底层文章模板。一般运营不需要进入，启用新模板请走“文体优化”。
                                        </p>
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setActiveSettingsTab("evidence")}
                                        className="text-left border rounded-lg p-4 bg-background/40 hover:bg-muted/60 transition-colors"
                                    >
                                        <div className="flex items-center gap-2 font-medium">
                                            <Search className="w-4 h-4" />
                                            审核依据
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            查看某个候选版本为什么可以或不可以启用。
                                        </p>
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => setActiveSettingsTab("shadow")}
                                        className="text-left border rounded-lg p-4 bg-background/40 hover:bg-muted/60 transition-colors"
                                    >
                                        <div className="flex items-center gap-2 font-medium">
                                            <ShieldCheck className="w-4 h-4" />
                                            内部记录
                                        </div>
                                        <p className="text-xs text-muted-foreground mt-2">
                                            查看内部观察记录和阻断原因，不会展示给客户。
                                        </p>
                                    </button>
                                </div>
                                <div className="rounded-lg border border-amber-800/40 bg-amber-950/20 p-4 text-sm text-amber-100">
                                    高级工具不等于上线开关。是否让新模板用于后续文章，请回到“功能开关”和“文体优化”按流程操作。
                                </div>
                            </div>
                        </TabsContent>

                        {/* === Tab 4: 内部观测运行摘要 === */}
                        <TabsContent value="shadow" className="px-6 pb-6 overflow-auto mt-2">
                            <div className="space-y-4 mt-4">
                                <div className="flex items-center justify-between gap-3">
                                    <div>
                                        <h3 className="font-medium">内部观测</h3>
                                        <p className="text-xs text-muted-foreground mt-0.5">
                                            仅展示内部摘要；实验内容给客户看保持关闭。
                                        </p>
                                    </div>
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={loadShadowRuns}
                                        disabled={shadowRunsLoading}
                                    >
                                        {shadowRunsLoading ? (
                                            <Loader2 className="w-3.5 h-3.5 animate-spin mr-1.5" />
                                        ) : (
                                            <RefreshCw className="w-3.5 h-3.5 mr-1.5" />
                                        )}
                                        刷新
                                    </Button>
                                </div>

                                {shadowRunsError && (
                                    <div className="p-3 bg-red-50 rounded-lg border border-red-200 text-sm text-red-700">
                                        <AlertCircle className="w-4 h-4 inline mr-1" />
                                        {shadowRunsError}
                                    </div>
                                )}

                                <div className="grid grid-cols-4 gap-3">
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">运行数</div>
                                        <div className="text-xl font-semibold mt-1">{shadowObservability?.total_runs ?? shadowRunsData?.artifact_count ?? 0}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">内部通过</div>
                                        <div className="text-xl font-semibold mt-1">{shadowObservability?.shadow_gate_allowed_count ?? shadowGateAllowedCount}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">内部绑定</div>
                                        <div className="text-xl font-semibold mt-1">{shadowObservability?.sidecar_present_count ?? shadowSidecarCount}</div>
                                    </div>
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs text-muted-foreground">客户侧关闭</div>
                                        <div className="text-xl font-semibold mt-1">{shadowObservability?.customer_output_closed_count ?? shadowCustomerOutputClosedCount}</div>
                                    </div>
                                </div>

                                <div className="grid grid-cols-[1fr_1.2fr] gap-3">
                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs font-medium text-muted-foreground">观测</div>
                                        <div className="mt-3 grid grid-cols-4 gap-2 text-sm">
                                            <div>
                                                <div className="text-xs text-muted-foreground">线上记录</div>
                                                <div className="font-semibold">{shadowObservability?.live_run_count ?? 0}</div>
                                            </div>
                                            <div>
                                                <div className="text-xs text-muted-foreground">人工</div>
                                                <div className="font-semibold">{shadowObservability?.manual_review_required_count ?? 0}</div>
                                            </div>
                                            <div>
                                                <div className="text-xs text-muted-foreground">客户侧异常</div>
                                                <div className={`font-semibold ${(shadowObservability?.customer_output_open_count ?? 0) > 0 ? "text-red-700" : "text-green-700"}`}>
                                                    {shadowObservability?.customer_output_open_count ?? 0}
                                                </div>
                                            </div>
                                            <div>
                                                <div className="text-xs text-muted-foreground">记录错误</div>
                                                <div className={`font-semibold ${(shadowObservability?.recorder_error_count ?? 0) > 0 ? "text-red-700" : "text-green-700"}`}>
                                                    {shadowObservability?.recorder_error_count ?? 0}
                                                </div>
                                            </div>
                                        </div>
                                        <div className="mt-3 flex flex-wrap gap-1.5 text-xs">
                                            {Object.entries(shadowObservability?.by_evidence_mode ?? {}).slice(0, 4).map(([mode, count]) => (
                                                <span key={mode} className="px-2 py-0.5 rounded border bg-background">
                                                    {evidenceModeLabel(mode)}: {count}
                                                </span>
                                            ))}
                                        </div>
                                    </div>

                                    <div className="border rounded-lg p-3 bg-background/40">
                                        <div className="text-xs font-medium text-muted-foreground">阻断原因</div>
                                        <div className="mt-2 flex flex-wrap gap-1.5">
                                            {topShadowBlockers.length === 0 ? (
                                                <span className="text-xs text-muted-foreground">暂无阻断原因</span>
                                            ) : (
                                                topShadowBlockers.slice(0, 8).map((item) => (
                                                    <span key={item.label} className="text-xs px-2 py-1 rounded bg-amber-50 text-amber-800 border border-amber-200">
                                                        {reasonLabel(item.label)}: {item.count}
                                                    </span>
                                                ))
                                            )}
                                        </div>
                                    </div>
                                </div>

                                {shadowRunsLoading && shadowRuns.length === 0 ? (
                                    <div className="border rounded-lg p-6 flex items-center justify-center text-sm text-muted-foreground">
                                        <Loader2 className="w-4 h-4 animate-spin mr-2" />
                                        读取中...
                                    </div>
                                ) : shadowRuns.length === 0 ? (
                                    <div className="border rounded-lg p-6 text-sm text-muted-foreground text-center">
                                        暂无内部观测记录
                                    </div>
                                ) : (
                                    <div className="border rounded-lg overflow-hidden">
                                        <div className="grid grid-cols-[1.3fr_1fr_0.8fr_0.8fr_0.8fr_1fr_0.6fr] gap-2 px-3 py-2 bg-background/40 border-b text-xs font-medium text-muted-foreground">
                                            <span>运行</span>
                                            <span>客户样本</span>
                                            <span>内部观测</span>
                                            <span>阻断</span>
                                            <span>绑定</span>
                                            <span>文章 SHA</span>
                                            <span>详情</span>
                                        </div>
                                        <div className="max-h-72 overflow-auto">
                                            {shadowRuns.map((run) => (
                                                <div
                                                    key={run.run_id}
                                                    className="grid grid-cols-[1.3fr_1fr_0.8fr_0.8fr_0.8fr_1fr_0.6fr] gap-2 px-3 py-2.5 border-b last:border-b-0 text-sm"
                                                >
                                                    <div className="min-w-0">
                                                        <div className="font-mono text-xs truncate">{run.run_id}</div>
                                                        <div className="text-xs text-muted-foreground mt-0.5">{runStatusLabel(run.status)}</div>
                                                    </div>
                                                    <span className="font-mono text-xs truncate self-center">{run.case_id || "-"}</span>
                                                    <span className={`text-xs self-center ${run.shadow_gate_allowed ? "text-green-700" : "text-amber-700"}`}>
                                                        {run.shadow_gate_allowed ? "通过" : "未过"}
                                                    </span>
                                                    <span className={`text-xs self-center ${run.blocker_count > 0 ? "text-amber-700" : "text-green-700"}`}>
                                                        {run.blocker_count}
                                                    </span>
                                                    <span className="text-xs self-center">
                                                        {run.sidecar_present ? "已记录" : "无"}
                                                    </span>
                                                    <span className="font-mono text-xs truncate self-center">
                                                        {run.customer_article_sha256 ? `${run.customer_article_sha256.slice(0, 12)}...` : "-"}
                                                    </span>
                                                    <Button
                                                        variant="outline"
                                                        size="sm"
                                                        className="h-7 px-2 self-center"
                                                        onClick={() => loadShadowRunDetail(run.run_id)}
                                                        disabled={shadowRunDetailLoading}
                                                    >
                                                        查看
                                                    </Button>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                )}

                                {shadowRunDetailError && (
                                    <div className="p-3 bg-red-50 rounded-lg border border-red-200 text-sm text-red-700">
                                        <AlertCircle className="w-4 h-4 inline mr-1" />
                                        {shadowRunDetailError}
                                    </div>
                                )}

                                {shadowRunDetailLoading && (
                                    <div className="border rounded-lg p-4 flex items-center text-sm text-muted-foreground">
                                        <Loader2 className="w-4 h-4 animate-spin mr-2" />
                                        读取详情中...
                                    </div>
                                )}

                                {shadowRunDetail && !shadowRunDetailLoading && (
                                    <div className="border rounded-lg overflow-hidden">
                                        <div className="px-4 py-3 bg-background/40 border-b flex items-center justify-between gap-3">
                                            <div className="min-w-0">
                                                <div className="font-medium truncate">{shadowRunDetail.run_id}</div>
                                                <div className="text-xs text-muted-foreground mt-0.5">
                                                    {shadowRunDetail.case_id || "-"} · {shadowRunDetail.article.char_count.toLocaleString()} 字符 · {shadowRunDetail.sidecar.present ? "内部绑定已记录" : "无内部绑定"}
                                                </div>
                                            </div>
                                            <div className={`text-xs px-2 py-1 rounded border ${shadowRunDetail.shadow_gate_allowed ? "bg-green-50 text-green-700 border-green-200" : "bg-amber-50 text-amber-700 border-amber-200"}`}>
                                                {shadowRunDetail.shadow_gate_allowed ? "内部通过" : "内部未过"}
                                            </div>
                                        </div>

                                        <div className="p-4 space-y-4">
                                            <div className="grid grid-cols-3 gap-3">
                                                <div className="border rounded-lg p-3 bg-background/40">
                                                    <div className="text-xs font-medium text-muted-foreground">人工复核</div>
                                                    <div className={`mt-2 text-sm font-medium ${shadowRunDetail.manual_review.manual_review_required ? "text-amber-700" : "text-green-700"}`}>
                                                        {shadowRunDetail.manual_review.manual_review_required ? "需要人工复核" : "无需人工复核"}
                                                    </div>
                                                    <div className="mt-1 text-xs text-muted-foreground">
                                                        处理方式: {routeLabel(shadowRunDetail.manual_review.route)} · 实验内容: {shadowRunDetail.manual_review.customer_output_allowed ? "可见" : "关闭"}
                                                    </div>
                                                    <div className="mt-2 flex flex-wrap gap-1.5">
                                                        {shadowRunDetail.manual_review.manual_review_reasons.length === 0 ? (
                                                            <span className="text-xs text-muted-foreground">无复核原因</span>
                                                        ) : (
                                                            shadowRunDetail.manual_review.manual_review_reasons.slice(0, 6).map((reason) => (
                                                                <span key={reason} className="text-xs px-2 py-0.5 rounded bg-amber-50 text-amber-800 border border-amber-200">
                                                                    {reasonLabel(reason)}
                                                                </span>
                                                            ))
                                                        )}
                                                    </div>
                                                </div>

                                                <div className="border rounded-lg p-3 bg-background/40">
                                                    <div className="text-xs font-medium text-muted-foreground">语义评审</div>
                                                    <div className="mt-2 text-sm font-medium">
                                                        {semanticDecisionLabel(shadowRunDetail.quality_summary.semantic_decision)}
                                                    </div>
                                                    <div className="mt-1 text-xs text-muted-foreground">
                                                        复核方 {shadowRunDetail.quality_summary.provider_count}/{shadowRunDetail.quality_summary.required_provider_count}
                                                    </div>
                                                    <div className="mt-2 grid grid-cols-3 gap-2 text-xs">
                                                        <span>提醒 {shadowRunDetail.quality_summary.warn_count}</span>
                                                        <span>失败 {shadowRunDetail.quality_summary.fail_count}</span>
                                                        <span>问题 {shadowRunDetail.quality_summary.issue_count}</span>
                                                    </div>
                                                    {(shadowRunDetail.quality_summary.metric_decision || shadowRunDetail.quality_summary.source_conflict_count > 0) && (
                                                        <div className="mt-2 text-xs text-muted-foreground">
                                                            指标: {reasonLabel(shadowRunDetail.quality_summary.metric_decision || "")} · 冲突: {shadowRunDetail.quality_summary.source_conflict_count}
                                                        </div>
                                                    )}
                                                </div>

                                                <div className="border rounded-lg p-3 bg-background/40">
                                                    <div className="text-xs font-medium text-muted-foreground">来源</div>
                                                    <div className={`mt-2 text-sm font-medium ${shadowRunDetail.source_summary.trusted_source ? "text-green-700" : "text-amber-700"}`}>
                                                        {shadowRunDetail.source_summary.trusted_source ? "可信服务端来源" : "未确认可信来源"}
                                                    </div>
                                                    <div className="mt-1 text-xs text-muted-foreground">
                                                        {evidenceModeLabel(shadowRunDetail.source_summary.evidence_mode)} · {sourceTrustLabel(shadowRunDetail.source_summary.evidence_packet_source || "missing")}
                                                    </div>
                                                    <div className="mt-2 text-xs text-muted-foreground">
                                                        证据 {shadowRunDetail.source_summary.evidence_count} · 绑定 {shadowRunDetail.source_summary.binding_count}
                                                    </div>
                                                </div>
                                            </div>

                                            <div>
                                                <div className="text-xs font-medium text-muted-foreground mb-2">阻断 / 警告</div>
                                                <div className="flex flex-wrap gap-2">
                                                    {[...shadowRunDetail.blockers, ...shadowRunDetail.warnings].length === 0 ? (
                                                        <span className="text-sm text-muted-foreground">无</span>
                                                    ) : (
                                                        [...shadowRunDetail.blockers, ...shadowRunDetail.warnings].map((item) => (
                                                            <span key={item} className="text-xs px-2 py-1 rounded bg-amber-50 text-amber-800 border border-amber-200">
                                                                {reasonLabel(item)}
                                                            </span>
                                                        ))
                                                    )}
                                                </div>
                                            </div>

                                            <div className="grid grid-cols-2 gap-4">
                                                <div className="space-y-2">
                                                    <div className="text-xs font-medium text-muted-foreground">文章句子片段</div>
                                                    <div className="border rounded-lg max-h-64 overflow-auto">
                                                        {shadowRunDetail.article.body_preview_redacted ? (
                                                            <div className="px-3 py-6 text-sm text-muted-foreground text-center">
                                                                正文预览已收起,仅保留哈希和字数
                                                            </div>
                                                        ) : shadowRunDetail.article.sentence_summaries.length === 0 ? (
                                                            <div className="px-3 py-6 text-sm text-muted-foreground text-center">无句子摘要</div>
                                                        ) : (
                                                            shadowRunDetail.article.sentence_summaries.slice(0, 12).map((sentence) => (
                                                                <div key={sentence.index} className="px-3 py-2 border-b last:border-b-0">
                                                                    <div className="text-xs text-muted-foreground mb-1">
                                                                        #{sentence.index}
                                                                        {sentence.linked_evidence_ids.length > 0 && (
                                                                            <span className="ml-2 font-mono">{sentence.linked_evidence_ids.join(", ")}</span>
                                                                        )}
                                                                    </div>
                                                                    <div className="text-sm leading-relaxed">{sentence.text_excerpt}</div>
                                                                </div>
                                                            ))
                                                        )}
                                                    </div>
                                                </div>

                                                <div className="space-y-2">
                                                    <div className="text-xs font-medium text-muted-foreground">绑定摘要</div>
                                                    <div className="border rounded-lg max-h-64 overflow-auto">
                                                        {shadowRunDetail.binding_summaries.length === 0 ? (
                                                            <div className="px-3 py-6 text-sm text-muted-foreground text-center">无绑定摘要</div>
                                                        ) : (
                                                            shadowRunDetail.binding_summaries.slice(0, 20).map((binding) => (
                                                                <div key={`${binding.index}-${binding.evidence_id}`} className="px-3 py-2 border-b last:border-b-0">
                                                                    <div className="text-xs text-muted-foreground mb-1">
                                                                        #{binding.index} · <span className="font-mono">{binding.evidence_id}</span> · {binding.start}-{binding.end}
                                                                    </div>
                                                                    <div className="text-sm leading-relaxed">{binding.claim_excerpt}</div>
                                                                </div>
                                                            ))
                                                        )}
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                )}
                            </div>
                        </TabsContent>
                    </Tabs>
                )}

                {/* 保存成功提示 */}
                {savedMessage && (
                    <div className="absolute bottom-4 left-1/2 -translate-x-1/2 px-4 py-2 bg-green-600 text-white rounded-lg shadow-lg text-sm flex items-center gap-1.5 animate-in fade-in slide-in-from-bottom-2">
                        <Check className="w-4 h-4" />
                        {savedMessage}
                    </div>
                )}
            </DialogContent>
          {confirmDialog}
        </Dialog>
    );
}
