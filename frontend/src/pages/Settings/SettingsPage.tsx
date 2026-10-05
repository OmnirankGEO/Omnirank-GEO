import { useState, useEffect, useRef, useCallback } from "react";
import { toast } from 'sonner';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Slider } from "@/components/ui/slider";
import { Badge } from "@/components/ui/badge";
import {
    Settings, Bot, FileText, Database, Save, RefreshCw, Check, X, Loader2,
    Eye, EyeOff, Search, PenTool, BarChart3, Users, Lightbulb, Type, Zap, Lock, Trash2,
    Activity, Clock, CheckCircle2, AlertCircle, ExternalLink, ChevronRight, Send,
} from "lucide-react";
import { settingsApi, confirmCodeApi, authFetch, type ConfirmCodeInfo } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { useBranding } from "@/hooks/useWhitelabel";
import { cn } from "@/lib/utils";
import { useConfirmDialog } from "@/components/ui/confirm-dialog";
// P12 (2026-05-26): GEO 调研抓取 4 平台模型卡 · 数据源 geo_research_config (不动 settings.json)
import { GeoResearchExtractLLMCard } from "./GeoResearchExtractLLMCard";

// 服务商列表
const PROVIDERS = [
    { id: "dashscope", name: "DashScope (阿里云)", placeholder: "qwen3.7-max, qwen3.6-plus, qwen-turbo" },
    { id: "deepseek", name: "DeepSeek", placeholder: "deepseek-chat, deepseek-reasoner" },
    { id: "openrouter", name: "OpenRouter", placeholder: "google/gemini-3-pro, anthropic/claude-sonnet-4.6" },
    { id: "doubao", name: "豆包 (字节跳动)", placeholder: "doubao-seed-2-0-pro-260215" },
    { id: "kimi", name: "Kimi (月之暗面)", placeholder: "kimi-k2.5" },
];

// 历史 content_ratios 仅用于兼容旧设置载荷，不再作为运营可编辑的第二套文体口径。
const LEGACY_CONTENT_RATIO_DEFAULTS: Record<string, number> = {
    authority: 18,
    deep_dive: 24,
    case_study: 8,
    pitfall: 10,
    trend: 10,
    faq: 7,
    checklist: 16,
    expert: 7,
};

// GEO v1.4 outward SSOT: operators edit six families. The settings payload
// remains backward-compatible and is expanded into maintained internal codes.
// [P1-4 2026-07-28] members 的权重 = 该家族内部拆分比例，必须覆盖 11 码里
// 除退役集/固定槽位外的全部 style_code。ranking_v2 / authority_ranking 在
// WP12(D12)已复活并拿到默认配比，若不列进 members 会有两个后果：
//   ① 家族合计漏算它们 → 页面总计显示 80%(红色)，运营以为配置坏了；
//   ② updateFamilyRatio 重建 payload 时把它们当"未知键"→ 被 DISABLED 清零，
//      admin 保存一次就把后端默认档打回退榜单口径。
const FAMILY_RATIO_FIELDS = [
    { id: "evidence_qa", name: "证据型问答", members: { qa_recommendation: 14 } },
    {
        id: "multi_brand_comparison",
        name: "选购与多品牌比较",
        members: { ranking_v2: 20, comparison_review: 18, recommendation_review: 14 },
    },
    { id: "implementation_guide", name: "方法与实施指南", members: { buying_guide: 14 } },
    { id: "trend_policy_risk", name: "趋势、政策与风险分析", members: { risk_compliance: 8 } },
    { id: "case_data_roi", name: "案例、数据与 ROI", members: { price_roi: 4, data_report: 4 } },
    { id: "company_facts", name: "企业事实与品牌说明", members: { brand_softarticle: 4 } },
] as const;

// 六家族 UI 只能设"家族总额"，家族内部按 members 权重拆分。没有被任何家族
// members 承载的 style_code 必须钉 0，否则 11 码合计不等于 100，后端 validator
// 会拒存。这里钉 0 的两个含义不同，别再混为一谈：
//   · trojan_horse       = 真退役(DISABLED_NEW_GENERATION_STYLES)，永远 0；
//   · authority_ranking  = 已复活但默认不分份额(榜单额度先全给 ranking_v2)，
//                          属运营选择，要给它份额得先把它加进上面的 members。
const PINNED_ZERO_STYLE_RATIOS: Record<string, number> = {
    trojan_horse: 0,
    authority_ranking: 0,
};

const DEFAULT_STYLE_RATIOS: Record<string, number> = {
    ...PINNED_ZERO_STYLE_RATIOS,
    ...Object.assign({}, ...FAMILY_RATIO_FIELDS.map((family) => family.members)),
};

function familyRatioValue(
    ratios: Record<string, number> | undefined,
    family: typeof FAMILY_RATIO_FIELDS[number],
): number {
    const source = ratios ?? DEFAULT_STYLE_RATIOS;
    return Object.keys(family.members).reduce((sum, code) => sum + Number(source[code] ?? 0), 0);
}

function updateFamilyRatio(
    ratios: Record<string, number> | undefined,
    family: typeof FAMILY_RATIO_FIELDS[number],
    requested: number,
): Record<string, number> {
    const next = { ...DEFAULT_STYLE_RATIOS, ...(ratios ?? {}), ...PINNED_ZERO_STYLE_RATIOS };
    const total = Math.max(0, Math.min(100, Number.isFinite(requested) ? requested : 0));
    const entries = Object.entries(family.members);
    const defaultTotal = entries.reduce((sum, [, weight]) => sum + Number(weight), 0);
    let allocated = 0;
    entries.forEach(([code, weight], index) => {
        const value = index === entries.length - 1
            ? total - allocated
            : Math.round(total * Number(weight) / defaultTotal);
        next[code] = Math.max(0, value);
        allocated += value;
    });
    return { ...next, ...PINNED_ZERO_STYLE_RATIOS };
}

// v2.7.1 industry_overrides 默认值(只读展示 · 不可改) · 医疗/法律 完整 8+11 dict
const INDUSTRY_OVERRIDES_DEFAULT: Record<string, { content_ratios: Record<string, number>; style_ratios: Record<string, number> }> = {
    "医疗健康": {
        content_ratios: { authority: 0, deep_dive: 10, case_study: 10, pitfall: 15, trend: 10, faq: 20, checklist: 25, expert: 10 },
        style_ratios: { ranking_v2: 0, authority_ranking: 0, recommendation_review: 4, buying_guide: 20, trojan_horse: 0, qa_recommendation: 14, brand_softarticle: 4, comparison_review: 18, risk_compliance: 30, price_roi: 6, data_report: 4 },
    },
    "法律商务": {
        content_ratios: { authority: 0, deep_dive: 8, case_study: 12, pitfall: 15, trend: 8, faq: 20, checklist: 25, expert: 12 },
        style_ratios: { ranking_v2: 0, authority_ranking: 0, recommendation_review: 4, buying_guide: 18, trojan_horse: 0, qa_recommendation: 14, brand_softarticle: 4, comparison_review: 18, risk_compliance: 32, price_roi: 6, data_report: 4 },
    },
};

// LLM任务配置 - 调研报告板块
const DIAGNOSIS_TASKS = [
    { id: "geo_scoring", name: "GEO评分", icon: BarChart3, desc: "8维度评分计算" },
    { id: "diagnostic_report", name: "诊断报告", icon: FileText, desc: "生成完整报告" },
    { id: "keyword_optimization", name: "关键词优化", icon: Search, desc: "分析和优化关键词" },
    { id: "competitor_analysis", name: "竞品分析", icon: Users, desc: "竞争对手分析" },
];

// LLM任务配置 - 写作板块
const WRITING_TASKS = [
    { id: "geo_article", name: "GEO文章写作", icon: PenTool, desc: "生成营销文章" },
    { id: "title_generation", name: "标题生成", icon: Type, desc: "生成吸引力标题" },
    { id: "topic_planning", name: "选题规划", icon: Lightbulb, desc: "规划内容选题" },
];

// LLM任务配置 - 社媒操盘手
const SOCIAL_TASKS = [
    { id: "topic_generation", name: "选题生成", icon: Lightbulb, desc: "短视频选题规划" },
    { id: "script_writing", name: "脚本写作", icon: PenTool, desc: "视频脚本文案" },
    { id: "rewrite_analysis", name: "仿写分析", icon: Search, desc: "分析爆款结构" },
    { id: "opening_generation", name: "开篇生成", icon: Type, desc: "黄金开篇创作" },
];

// LLM任务配置 - AI员工
const EMPLOYEE_TASKS = [
    { id: "default", name: "默认模型", icon: Bot, desc: "日常任务使用" },
    { id: "high_quality", name: "高质量模型", icon: Zap, desc: "重要任务使用" },
    { id: "content_planning", name: "内容规划", icon: FileText, desc: "内容策略制定" },
];

// [工单 C-7 · Owner 2026-08-25 拍板] MONITORING_TASKS 与「监测中心 LLM」整块**已摘**。
// 后端 `config/settings_manager.SystemSettings.monitoring_tasks` 已按 Owner
// 2026-08-24 的批复删除(包F ⑦c:全仓零消费点的幽灵配置)。这一块留着的后果是
// 假保存:管理员改完点保存不报错,值在 pydantic 那一层被 extra=ignore 丢掉,
// 重新打开还是这里写死的默认值 —— 而他会以为线上监测按它路由。
// 「一个填了就丢的输入框比没有这个输入框更贵」。

// 子任务LLM配置接口
interface TaskLLMConfig {
    provider: string;
    model: string;
}

interface Settings {
    // API Keys (6+2个服务商)
    dashscope_api_key: string;
    deepseek_api_key: string;
    openrouter_api_key: string;
    doubao_api_key: string;
    doubao_endpoint_id: string;
    kimi_api_key: string;
    tikhub_api_key: string;
    siliconflow_api_key: string;
    metaso_api_key: string;
    jina_api_key: string;  // P14-v14: Jina Reader (调研监测 stage_3)
    // 调研报告板块 - 默认配置
    diagnosis_provider: string;
    diagnosis_model: string;
    // 调研报告板块 - 子任务LLM（可选覆盖）
    diagnosis_tasks: Record<string, TaskLLMConfig>;
    // 写作板块 - 默认配置
    writing_provider: string;
    writing_model: string;
    // 写作板块 - 子任务LLM（可选覆盖）
    writing_tasks: Record<string, TaskLLMConfig>;
    // 社媒操盘手 - 子任务LLM
    social_tasks: Record<string, TaskLLMConfig>;
    // AI员工 - 子任务LLM
    employee_tasks: Record<string, TaskLLMConfig>;
    // 内容配置(v2.7.1)
    content_ratios: Record<string, number>;
    style_ratios: Record<string, number>;
    industry_overrides: Record<string, { content_ratios?: Record<string, number>; style_ratios?: Record<string, number> }>;
    concurrent_writers: number;
    default_article_count: number;
    // ====== 报价系统配置 ======
    quote_cluster_mode: boolean;
    quote_content_cost: number;
    quote_media_cost: number;
    quote_operation_cost: number;
    quote_markup_ratio: number;
    quote_ai_reference_count: number;
    quote_default_target_share: number;
    quote_batch_concurrency: number;
    // ====== 文章生成配置 ======
    article_timeout: number;
    article_retry_count: number;
    article_client_position: number;
    article_client_coverage: number;
    article_max_competitors: number;
    // ====== 诊断系统配置 ======
    diagnosis_ai_test_timeout: number;
    diagnosis_step_cache: boolean;
    diagnosis_metaso_size: number;
    // ====== 社媒配置 ======
    social_asr_timeout: number;
    social_asr_model: string;
    social_tikhub_timeout: number;
    // ====== 知识库配置 ======
    kb_llm_clean_enabled: boolean;
    kb_llm_clean_model: string;
    kb_embedding_model: string;
    kb_rerank_enabled: boolean;
    // ====== LLM全局配置 ======
    llm_global_timeout: number;
    llm_global_max_retries: number;
    llm_global_temperature: number;
    // ====== 高级选项 ======
    debug_mode: boolean;
    sse_heartbeat_interval: number;
    // ====== 公开报告 v3 ======
    report_v3_enabled: boolean;
    report_v3_whitelist_user_ids: number[];
    report_v3_auto_enrich: boolean;
    llm_narrative_monthly_yuan: number;
    llm_narrative_max_concurrent: number;
    llm_narrative_alert_yuan: number;
}

const DEFAULT_SETTINGS: Settings = {
    dashscope_api_key: "",
    deepseek_api_key: "",
    openrouter_api_key: "",
    doubao_api_key: "",
    doubao_endpoint_id: "",
    kimi_api_key: "",
    tikhub_api_key: "",
    siliconflow_api_key: "",
    metaso_api_key: "",
    jina_api_key: "",
    diagnosis_provider: "dashscope",
    diagnosis_model: "qwen3.7-plus",
    diagnosis_tasks: {
        geo_scoring: { provider: "deepseek", model: "deepseek-reasoner" },
        diagnostic_report: { provider: "dashscope", model: "qwen3.7-plus" },
        keyword_optimization: { provider: "dashscope", model: "qwen3.7-max" },
        competitor_analysis: { provider: "openrouter", model: "google/gemini-3-flash" },
    },
    writing_provider: "dashscope",
    writing_model: "qwen3.6-plus",
    writing_tasks: {
        geo_article: { provider: "dashscope", model: "qwen3.7-max" },
        title_generation: { provider: "dashscope", model: "qwen3.6-plus" },
        topic_planning: { provider: "dashscope", model: "qwen3.7-max" },
    },
    social_tasks: {
        topic_generation: { provider: "dashscope", model: "qwen3.7-max" },
        script_writing: { provider: "dashscope", model: "qwen3.7-max" },
        rewrite_analysis: { provider: "dashscope", model: "qwen3.7-max" },
        opening_generation: { provider: "dashscope", model: "qwen3.7-max" },
    },
    employee_tasks: {
        default: { provider: "dashscope", model: "qwen3.6-plus" },
        high_quality: { provider: "dashscope", model: "qwen3.7-max" },
        content_planning: { provider: "dashscope", model: "qwen3.7-max" },
    },
    content_ratios: { ...LEGACY_CONTENT_RATIO_DEFAULTS },
    // v2.7.1 GEO 文体改造 · style_ratios + industry_overrides 默认 fallback
    style_ratios: DEFAULT_STYLE_RATIOS,
    industry_overrides: INDUSTRY_OVERRIDES_DEFAULT,
    concurrent_writers: 10,
    default_article_count: 40,
    // 报价系统配置
    quote_cluster_mode: true,
    quote_content_cost: 25,
    quote_media_cost: 40,
    quote_operation_cost: 15,
    quote_markup_ratio: 1.0,
    quote_ai_reference_count: 4,
    quote_default_target_share: 0.20,
    quote_batch_concurrency: 10,
    // 文章生成配置
    article_timeout: 180,
    article_retry_count: 2,
    article_client_position: 1,
    article_client_coverage: 0.5,
    article_max_competitors: 4,
    // 诊断系统配置
    diagnosis_ai_test_timeout: 180,
    diagnosis_step_cache: true,
    diagnosis_metaso_size: 100,
    // 社媒配置
    social_asr_timeout: 180,
    social_asr_model: "qwen3-asr-flash",
    social_tikhub_timeout: 120,
    // 知识库配置
    kb_llm_clean_enabled: true,
    kb_llm_clean_model: "qwen3.7-max",
    kb_embedding_model: "text-embedding-v4",
    kb_rerank_enabled: false,
    // LLM全局配置
    llm_global_timeout: 180,
    llm_global_max_retries: 2,
    llm_global_temperature: 0.7,
    // 高级选项
    debug_mode: false,
    sse_heartbeat_interval: 30,
    // 公开报告 v3
    report_v3_enabled: false,
    report_v3_whitelist_user_ids: [],
    report_v3_auto_enrich: false,
    llm_narrative_monthly_yuan: 100,
    llm_narrative_max_concurrent: 3,
    llm_narrative_alert_yuan: 80,
};

// ==========================================
// 操作确认码管理组件
// ==========================================
function ConfirmCodeSection() {
    const [codes, setCodes] = useState<ConfirmCodeInfo[]>([]);
    const [loading, setLoading] = useState(true);
    const [editingAction, setEditingAction] = useState<string | null>(null);
    const [newCode, setNewCode] = useState("");
    const [confirmNewCode, setConfirmNewCode] = useState("");
    const [saving, setSaving] = useState(false);
    const [error, setError] = useState("");
    const [successMsg, setSuccessMsg] = useState("");
    const [loadError, setLoadError] = useState("");
    const [confirmDialog, askConfirm] = useConfirmDialog();

    useEffect(() => {
        loadCodes();
    }, []);

    useEffect(() => {
        if (successMsg) {
            const timer = setTimeout(() => setSuccessMsg(""), 3000);
            return () => clearTimeout(timer);
        }
    }, [successMsg]);

    const loadCodes = async () => {
        setLoadError("");
        try {
            const res = await confirmCodeApi.list();
            setCodes(res.data.codes || []);
        } catch (err: any) {
            const detail = err?.response?.data?.detail || err?.message || "未知错误";
            setLoadError(`加载确认码失败: ${detail}`);
            console.error("加载确认码失败:", err);
        } finally {
            setLoading(false);
        }
    };

    const handleSave = async (action: string) => {
        setError("");
        if (newCode.length < 4) {
            setError("确认码至少4位");
            return;
        }
        if (newCode !== confirmNewCode) {
            setError("两次输入不一致");
            return;
        }
        setSaving(true);
        try {
            await confirmCodeApi.set(action, newCode);
            setEditingAction(null);
            setNewCode("");
            setConfirmNewCode("");
            setSuccessMsg("确认码已更新");
            loadCodes();
        } catch (err) {
            setError("保存失败，请重试");
        } finally {
            setSaving(false);
        }
    };

    const handleDelete = async (action: string, label: string) => {
        if (!(await askConfirm({ title: `确定要删除「${label}」吗？`, description: '删除后该操作将无需确认码。', danger: true }))) return;
        try {
            await confirmCodeApi.remove(action);
            setSuccessMsg("确认码已删除");
            loadCodes();
        } catch (err) {
            console.error("删除失败:", err);
        }
    };

    if (loading) {
        return (
            <Card>
                <CardContent className="py-8 text-center text-muted-foreground">
                    <Loader2 className="h-5 w-5 animate-spin mx-auto mb-2" />
                    加载中...
                </CardContent>
            </Card>
        );
    }

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <Lock className="h-5 w-5" />
                    操作确认码
                </CardTitle>
                <CardDescription>
                    为关键操作设置确认码，执行操作时需输入正确的确认码。连续错误5次将锁定10分钟。
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
                {confirmDialog}
                {loadError && (
                    <div className="px-4 py-2 rounded-lg text-sm font-medium bg-red-100 text-red-800 border border-red-200 flex items-center justify-between">
                        <span>{loadError}</span>
                        <Button variant="outline" size="sm" onClick={loadCodes}>重试</Button>
                    </div>
                )}
                {successMsg && (
                    <div className="px-4 py-2 rounded-lg text-sm font-medium bg-green-100 text-green-800 border border-green-200">
                        {successMsg}
                    </div>
                )}
                {codes.map((code) => (
                    <div key={code.action} className="border rounded-lg p-4 space-y-3">
                        <div className="flex items-center justify-between">
                            <div>
                                <h4 className="font-medium">{code.label}</h4>
                                {code.has_code ? (
                                    <p className="text-sm text-muted-foreground">
                                        已设置 · 最后修改: {code.updated_at} by {code.updated_by}
                                    </p>
                                ) : (
                                    <p className="text-sm text-amber-600">
                                        未设置 · 该操作当前无需确认码
                                    </p>
                                )}
                            </div>
                            <div className="flex gap-2">
                                {code.has_code && (
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => handleDelete(code.action, code.label)}
                                        className="text-red-600 hover:text-red-700 hover:bg-red-50"
                                    >
                                        <Trash2 className="h-4 w-4 mr-1" />
                                        删除
                                    </Button>
                                )}
                                <Button
                                    variant="outline"
                                    size="sm"
                                    onClick={() => {
                                        setEditingAction(editingAction === code.action ? null : code.action);
                                        setNewCode("");
                                        setConfirmNewCode("");
                                        setError("");
                                    }}
                                >
                                    {code.has_code ? "修改" : "设置"}确认码
                                </Button>
                            </div>
                        </div>
                        {editingAction === code.action && (
                            <div className="border-t pt-3 space-y-3">
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                    <div>
                                        <Label>新确认码（至少4位）</Label>
                                        <Input
                                            type="password"
                                            value={newCode}
                                            onChange={(e) => { setNewCode(e.target.value); setError(""); }}
                                            placeholder="输入新确认码"
                                            className="mt-1"
                                            autoFocus
                                        />
                                    </div>
                                    <div>
                                        <Label>确认新确认码</Label>
                                        <Input
                                            type="password"
                                            value={confirmNewCode}
                                            onChange={(e) => { setConfirmNewCode(e.target.value); setError(""); }}
                                            placeholder="再次输入确认码"
                                            className="mt-1"
                                            onKeyDown={(e) => e.key === "Enter" && handleSave(code.action)}
                                        />
                                    </div>
                                </div>
                                {error && <p className="text-sm text-red-600">{error}</p>}
                                <div className="flex gap-2">
                                    <Button
                                        size="sm"
                                        onClick={() => handleSave(code.action)}
                                        disabled={saving || !newCode || !confirmNewCode}
                                    >
                                        {saving ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Check className="h-4 w-4 mr-1" />}
                                        保存
                                    </Button>
                                    <Button
                                        variant="outline"
                                        size="sm"
                                        onClick={() => { setEditingAction(null); setError(""); }}
                                    >
                                        取消
                                    </Button>
                                </div>
                            </div>
                        )}
                    </div>
                ))}
                <div className="text-sm text-muted-foreground border-t pt-3">
                    <p>说明：确认码由管理员设置，销售在执行关键操作时需向财务获取确认码。确认码以加密方式存储，不可查看原文。</p>
                </div>
            </CardContent>
        </Card>
    );
}


// ==========================================
// 服务状态监控组件（SSE 实时检测）
// ==========================================
interface HealthService {
    id: string;
    name: string;
    type: string;
    status: 'ok' | 'error' | 'unconfigured';
    message: string;
    latency_ms: number | null;
}

function PublishSettings() {
    return <PublishConfig />;
}

// ---- 配置管理 ----
function PublishConfig() {
    const [sessionId, setSessionId] = useState('');
    const [sessionValid, setSessionValid] = useState<boolean | null>(null);
    const [checking, setChecking] = useState(false);
    // 媒体同步和订单同步独立状态
    const [mediaSyncing, setMediaSyncing] = useState(false);
    const [mediaSyncMsg, setMediaSyncMsg] = useState('');
    const [lastMediaSync, setLastMediaSync] = useState('');
    const [statusSyncing, setStatusSyncing] = useState(false);
    const [statusSyncMsg, setStatusSyncMsg] = useState('');
    const [lastStatusSync, setLastStatusSync] = useState('');
    const [wmSyncing, setWmSyncing] = useState(false);
    const [wmSyncMsg, setWmSyncMsg] = useState('');
    const [lastWmSync, setLastWmSync] = useState('');
    const [markupRatio, setMarkupRatio] = useState('2.0');
    const [savingRatio, setSavingRatio] = useState(false);

    const checkSession = async () => {
        setChecking(true);
        try {
            const res = await authFetch('/api/meijiehezi/admin/stats');
            const data = await res.json();
            setSessionValid(data.session_configured ?? null);
        } catch {
            setSessionValid(null);
        } finally {
            setChecking(false);
        }
    };

    const updateSession = async () => {
        if (!sessionId.trim()) return;
        try {
            const res = await authFetch('/api/meijiehezi/admin/config/session', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ phpsessid: sessionId.trim() }),
            });
            const data = await res.json();
            setSessionValid(data.session_valid ?? true);
            setSessionId('');
            toast.success('Session 已更新');
        } catch {
            toast.error('更新失败');
        }
    };

    // 同步进度
    const [mediaProgress, setMediaProgress] = useState<{ phase: string; current: number; total: number; detail: string }>({ phase: '', current: 0, total: 0, detail: '' });
    const [statusProgress, setStatusProgress] = useState<{ phase: string; current: number; total: number; detail: string }>({ phase: '', current: 0, total: 0, detail: '' });
    const [wmProgress, setWmProgress] = useState<{ phase: string; current: number; total: number; detail: string }>({ phase: '', current: 0, total: 0, detail: '' });

    // 通用轮询函数（按 type 独立）
    const startPoll = useCallback((type: 'media' | 'status' | 'wemedia') => {
        const key = `mhz_sync_${type}`;
        const stored = localStorage.getItem(key);
        if (!stored) return;
        const { startedAt } = JSON.parse(stored);
        if (Date.now() - startedAt > 600000) { localStorage.removeItem(key); return; }

        const setSyncing = type === 'media' ? setMediaSyncing : type === 'status' ? setStatusSyncing : setWmSyncing;
        const setMsg = type === 'media' ? setMediaSyncMsg : type === 'status' ? setStatusSyncMsg : setWmSyncMsg;
        const setProgress = type === 'media' ? setMediaProgress : type === 'status' ? setStatusProgress : setWmProgress;
        setSyncing(true);
        setMsg('同步中...');

        const poll = setInterval(async () => {
            try {
                const r = await authFetch('/api/meijiehezi/admin/sync/result');
                const d = await r.json();
                if (d.status === 'success') {
                    // 更新进度
                    const prog = type === 'media' ? d.media_progress : type === 'status' ? d.status_progress : d.wemedia_progress;
                    if (prog) setProgress(prog);

                    // 进度完成或出错 → 停止轮询
                    if (prog && (prog.phase === 'done' || prog.phase === 'error')) {
                        const result = type === 'media' ? d.media_sync_result : type === 'status' ? d.status_sync_result : d.wemedia_sync_result;
                        const time = type === 'media' ? d.media_sync : type === 'status' ? d.status_sync : d.wemedia_sync;
                        setMsg(result?.startsWith('失败') ? result : result ? `同步完成：${result}` : '同步完成');
                        if (type === 'media' && time) setLastMediaSync(time);
                        if (type === 'status' && time) setLastStatusSync(time);
                        if (type === 'wemedia' && time) setLastWmSync(time);
                        setSyncing(false);
                        localStorage.removeItem(key);
                        clearInterval(poll);
                        return;
                    }

                    // 兼容旧逻辑：result 时间比 startedAt 新
                    const result = type === 'media' ? d.media_sync_result : type === 'status' ? d.status_sync_result : d.wemedia_sync_result;
                    const time = type === 'media' ? d.media_sync : type === 'status' ? d.status_sync : d.wemedia_sync;
                    if (result && time && new Date(time).getTime() > startedAt) {
                        setMsg(result.startsWith('失败') ? result : `同步完成：${result}`);
                        if (type === 'media') setLastMediaSync(time);
                        else if (type === 'status') setLastStatusSync(time);
                        else setLastWmSync(time);
                        setSyncing(false);
                        localStorage.removeItem(key);
                        clearInterval(poll);
                    }
                }
            } catch {}
            if (Date.now() - startedAt > 600000) { setSyncing(false); localStorage.removeItem(key); clearInterval(poll); }
        }, 2000);
        return () => clearInterval(poll);
    }, []);

    // 组件挂载时恢复同步状态
    useEffect(() => {
        const c1 = startPoll('media');
        const c2 = startPoll('status');
        const c3 = startPoll('wemedia');
        return () => { c1?.(); c2?.(); c3?.(); };
    }, [startPoll]);

    const triggerSync = async (type: 'media' | 'status' | 'wemedia') => {
        const setSyncing = type === 'media' ? setMediaSyncing : type === 'status' ? setStatusSyncing : setWmSyncing;
        const setMsg = type === 'media' ? setMediaSyncMsg : type === 'status' ? setStatusSyncMsg : setWmSyncMsg;
        setSyncing(true);
        setMsg('同步中...');
        try {
            const endpoints: Record<string, string> = {
                media: '/api/meijiehezi/admin/sync/media',
                status: '/api/meijiehezi/admin/sync/status',
                wemedia: '/api/meijiehezi/admin/sync/wemedia',
            };
            const res = await authFetch(endpoints[type], { method: 'POST' });
            await res.json();
            localStorage.setItem(`mhz_sync_${type}`, JSON.stringify({ startedAt: Date.now() }));
            startPoll(type);
        } catch {
            setMsg('操作失败');
            setSyncing(false);
        }
    };

    useEffect(() => {
        checkSession();
        // 加载加价比例
        authFetch('/api/meijiehezi/admin/config/markup')
            .then(r => r.json()).then(d => { if (d.status === 'success') setMarkupRatio(String(d.ratio)); }).catch(() => {});
        // 加载上次同步时间和结果
        authFetch('/api/meijiehezi/admin/sync/result').then(r => r.json()).then(d => {
            if (d.status === 'success') {
                if (d.media_sync) setLastMediaSync(d.media_sync);
                if (d.media_sync_result) setMediaSyncMsg(d.media_sync_result);
                if (d.status_sync) setLastStatusSync(d.status_sync);
                if (d.status_sync_result) setStatusSyncMsg(d.status_sync_result);
                if (d.wemedia_sync) setLastWmSync(d.wemedia_sync);
                if (d.wemedia_sync_result) setWmSyncMsg(d.wemedia_sync_result);
            }
        }).catch(() => {});
    }, []);

    return (
        <>
            <Card>
                <CardHeader>
                    <CardTitle>外部发布通道授权</CardTitle>
                    <CardDescription>
                        代发功能需要外部发布通道的授权 Session。获取方法：
                        1. 浏览器打开授权后台并登录
                        2. 按 F12 打开开发者工具 → Application → Cookies
                        3. 找到渠道的会话 Cookie，复制其 Value 填入下方。
                        系统每 10 分钟自动保活，仅在外部通道授权失效时需要重新获取。
                    </CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    <div className="flex items-center gap-3">
                        <span className="text-sm">当前状态：</span>
                        {checking ? (
                            <Loader2 className="h-4 w-4 animate-spin" />
                        ) : sessionValid === true ? (
                            <Badge className="bg-green-100 text-green-700">有效</Badge>
                        ) : sessionValid === false ? (
                            <Badge variant="destructive">已失效</Badge>
                        ) : (
                            <Badge variant="secondary">未配置</Badge>
                        )}
                        <Button variant="ghost" size="sm" onClick={checkSession}>
                            <RefreshCw className="h-3.5 w-3.5" />
                        </Button>
                    </div>
                    <div className="flex gap-2">
                        <Input
                            placeholder="输入会话 Cookie..."
                            value={sessionId}
                            onChange={e => setSessionId(e.target.value)}
                            className="font-mono text-sm"
                        />
                        <Button onClick={updateSession} disabled={!sessionId.trim()}>
                            <Save className="h-4 w-4 mr-1" /> 更新
                        </Button>
                    </div>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>数据同步</CardTitle>
                    <CardDescription>手动触发媒体列表或订单状态同步</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    {/* 媒体列表同步 */}
                    <div className="space-y-2">
                        <div className="flex items-center gap-3">
                            <Button variant="outline" onClick={() => triggerSync('media')} disabled={mediaSyncing}>
                                {mediaSyncing ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                                同步媒体列表
                            </Button>
                            {!mediaSyncing && mediaSyncMsg && (
                                <span className={cn('text-xs', mediaSyncMsg.includes('完成') ? 'text-green-400' : mediaSyncMsg.includes('失败') ? 'text-red-400' : 'text-muted-foreground')}>
                                    {mediaSyncMsg}
                                </span>
                            )}
                        </div>
                        {mediaSyncing && mediaProgress.phase && (
                            <div className="space-y-1.5 ml-1">
                                <div className="flex items-center justify-between text-xs text-muted-foreground">
                                    <span>{mediaProgress.detail || '同步中...'}</span>
                                    {mediaProgress.total > 0 && (
                                        <span>{Math.round((mediaProgress.current / mediaProgress.total) * 100)}%</span>
                                    )}
                                </div>
                                {mediaProgress.total > 0 && (
                                    <div className="h-1.5 bg-muted rounded-full overflow-hidden">
                                        <div
                                            className="h-full bg-brand rounded-full transition-all duration-500"
                                            style={{ width: `${Math.min(100, (mediaProgress.current / mediaProgress.total) * 100)}%` }}
                                        />
                                    </div>
                                )}
                            </div>
                        )}
                        {lastMediaSync && (
                            <p className="text-[11px] text-muted-foreground flex items-center gap-1 ml-1">
                                <Clock className="h-3 w-3" /> 上次: {lastMediaSync}
                            </p>
                        )}
                    </div>

                    {/* 订单状态同步 */}
                    <div className="space-y-2">
                        <div className="flex items-center gap-3">
                            <Button variant="outline" onClick={() => triggerSync('status')} disabled={statusSyncing}>
                                {statusSyncing ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Clock className="h-4 w-4 mr-1" />}
                                同步订单状态
                            </Button>
                            {!statusSyncing && statusSyncMsg && (
                                <span className={cn('text-xs', statusSyncMsg.includes('完成') ? 'text-green-400' : statusSyncMsg.includes('失败') ? 'text-red-400' : 'text-muted-foreground')}>
                                    {statusSyncMsg}
                                </span>
                            )}
                        </div>
                        {statusSyncing && statusProgress.phase && (
                            <div className="space-y-1.5 ml-1">
                                <div className="text-xs text-muted-foreground">
                                    {statusProgress.detail || '同步中...'}
                                </div>
                            </div>
                        )}
                        {lastStatusSync && (
                            <p className="text-[11px] text-muted-foreground flex items-center gap-1 ml-1">
                                <Clock className="h-3 w-3" /> 上次: {lastStatusSync}
                            </p>
                        )}
                    </div>

                    {/* 自媒体同步 */}
                    <div className="space-y-2">
                        <div className="flex items-center gap-3">
                            <Button variant="outline" onClick={() => triggerSync('wemedia')} disabled={wmSyncing}>
                                {wmSyncing ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                                同步自媒体
                            </Button>
                            {!wmSyncing && wmSyncMsg && (
                                <span className={cn('text-xs', wmSyncMsg.includes('完成') ? 'text-green-400' : wmSyncMsg.includes('失败') ? 'text-red-400' : 'text-muted-foreground')}>
                                    {wmSyncMsg}
                                </span>
                            )}
                        </div>
                        {wmSyncing && wmProgress.phase && (
                            <div className="space-y-1.5 ml-1">
                                <div className="flex items-center justify-between text-xs text-muted-foreground">
                                    <span>{wmProgress.detail || '同步中...'}</span>
                                    {wmProgress.total > 0 && (
                                        <span>{Math.round((wmProgress.current / wmProgress.total) * 100)}%</span>
                                    )}
                                </div>
                                {wmProgress.total > 0 && (
                                    <div className="h-1.5 bg-muted rounded-full overflow-hidden">
                                        <div
                                            className="h-full bg-brand rounded-full transition-all duration-500"
                                            style={{ width: `${Math.min(100, (wmProgress.current / wmProgress.total) * 100)}%` }}
                                        />
                                    </div>
                                )}
                            </div>
                        )}
                        {lastWmSync && (
                            <p className="text-[11px] text-muted-foreground flex items-center gap-1 ml-1">
                                <Clock className="h-3 w-3" /> 上次: {lastWmSync}
                            </p>
                        )}
                    </div>
                </CardContent>
            </Card>

            <Card>
                <CardHeader>
                    <CardTitle>代发定价</CardTitle>
                    <CardDescription>
                        加价比例 = 我们售价 / 外部通道基础价。默认 1.5（加价 50%）。
                        修改后需重新同步媒体列表生效。
                    </CardDescription>
                </CardHeader>
                <CardContent>
                    <div className="flex gap-3 items-end">
                        <div className="space-y-1.5">
                            <Label>加价比例</Label>
                            <Input
                                type="number"
                                min="1.1"
                                max="5.0"
                                step="0.1"
                                value={markupRatio}
                                onChange={e => setMarkupRatio(e.target.value)}
                                className="w-32"
                            />
                        </div>
                        <div className="text-sm text-muted-foreground pb-2">
                            示例：VIP ¥100 → 售价 ¥{Math.ceil(100 * parseFloat(markupRatio || '2.0'))} → {Math.ceil(100 * parseFloat(markupRatio || '2.0') * 130).toLocaleString()} 算力
                        </div>
                        <Button
                            disabled={savingRatio}
                            onClick={async () => {
                                const ratio = parseFloat(markupRatio);
                                if (isNaN(ratio) || ratio < 1.1 || ratio > 5) {
                                    toast.error('加价比例需在 1.1 - 5.0 之间');
                                    return;
                                }
                                setSavingRatio(true);
                                try {
                                    const res = await authFetch('/api/meijiehezi/admin/config/markup', {
                                        method: 'PUT',
                                        headers: {
                                            'Content-Type': 'application/json',
                                        },
                                        body: JSON.stringify({ ratio }),
                                    });
                                    const data = await res.json();
                                    toast.success(data.message || '已保存');
                                } catch {
                                    toast.error('保存失败');
                                } finally {
                                    setSavingRatio(false);
                                }
                            }}
                        >
                            <Save className="h-4 w-4 mr-1" /> 保存
                        </Button>
                    </div>
                </CardContent>
            </Card>
        </>
    );
}

function ServiceHealthCheck() {
    const [checking, setChecking] = useState(false);
    const [services, setServices] = useState<HealthService[]>([]);
    const [progress, setProgress] = useState({ current: 0, total: 0 });
    const [checkedAt, setCheckedAt] = useState<string | null>(null);
    const [error, setError] = useState("");
    const [logs, setLogs] = useState<string[]>([]);
    const logRef = useRef<HTMLDivElement>(null);

    // 自动滚动日志到底部
    useEffect(() => {
        if (logRef.current) {
            logRef.current.scrollTop = logRef.current.scrollHeight;
        }
    }, [logs]);

    const addLog = (msg: string) => {
        const time = new Date().toLocaleTimeString('zh-CN');
        setLogs(prev => [...prev, `[${time}] ${msg}`]);
    };

    const runHealthCheck = async () => {
        setChecking(true);
        setError("");
        setServices([]);
        setProgress({ current: 0, total: 0 });
        setCheckedAt(null);
        setLogs([]);
        addLog("🚀 开始检测所有外部服务...");

        try {
            const resp = await authFetch(settingsApi.healthCheckUrl);

            if (!resp.ok) {
                throw new Error(`HTTP ${resp.status}: ${resp.statusText}`);
            }

            const reader = resp.body?.getReader();
            if (!reader) throw new Error("无法读取响应流");

            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop() || "";

                for (const line of lines) {
                    if (!line.startsWith("data: ")) continue;
                    try {
                        const event = JSON.parse(line.slice(6));

                        if (event.type === "service_result") {
                            const s = event.service;
                            setProgress({ current: event.progress, total: event.total });
                            setServices(prev => [...prev, s]);

                            // 实时日志
                            const icon = s.status === 'ok' ? '✅' : s.status === 'error' ? '❌' : '⚠️';
                            const latency = s.latency_ms ? ` (${s.latency_ms}ms)` : '';
                            addLog(`${icon} [${event.progress}/${event.total}] ${s.name} — ${s.message}${latency}`);
                        } else if (event.type === "complete") {
                            setCheckedAt(event.checked_at);
                            addLog("🏁 检测完成！");
                        } else if (event.type === "error") {
                            setError(event.message);
                            addLog(`💥 错误: ${event.message}`);
                        }
                    } catch { /* skip malformed */ }
                }
            }
        } catch (err: any) {
            const msg = err?.message || "检测失败";
            setError(msg);
            addLog(`💥 连接错误: ${msg}`);
        } finally {
            setChecking(false);
        }
    };

    const statusIcon = (status: string) => {
        if (status === 'ok') return <Check className="h-4 w-4 text-green-500" />;
        if (status === 'error') return <X className="h-4 w-4 text-red-500" />;
        return <Clock className="h-4 w-4 text-amber-500" />;
    };

    const statusBadge = (status: string) => {
        if (status === 'ok') return <Badge className="bg-green-500 hover:bg-green-600">正常</Badge>;
        if (status === 'error') return <Badge variant="destructive">异常</Badge>;
        return <Badge variant="outline" className="text-amber-600 border-amber-300">未配置</Badge>;
    };

    // 计算汇总
    const summary = services.length > 0 ? {
        total: services.length,
        ok: services.filter(s => s.status === 'ok').length,
        error: services.filter(s => s.status === 'error').length,
        unconfigured: services.filter(s => s.status === 'unconfigured').length,
    } : null;

    return (
        <Card>
            <CardHeader>
                <div className="flex items-center justify-between">
                    <div>
                        <CardTitle className="flex items-center gap-2">
                            <Activity className="h-5 w-5 text-brand" />
                            服务状态监控
                        </CardTitle>
                        <CardDescription>
                            检测所有外部API和数据服务的连通性，排查失效或欠费问题（零费用检测）
                        </CardDescription>
                    </div>
                    <Button onClick={runHealthCheck} disabled={checking}>
                        {checking ? (
                            <><Loader2 className="mr-2 h-4 w-4 animate-spin" />检测中 ({progress.current}/{progress.total})...</>
                        ) : (
                            <><RefreshCw className="mr-2 h-4 w-4" />一键检测</>
                        )}
                    </Button>
                </div>
            </CardHeader>
            <CardContent className="space-y-4">
                {error && (
                    <div className="px-4 py-2 rounded-lg text-sm font-medium bg-red-100 text-red-800 border border-red-200">
                        {error}
                    </div>
                )}

                {/* 进度条 */}
                {checking && progress.total > 0 && (
                    <div className="space-y-1">
                        <div className="flex justify-between text-xs text-muted-foreground">
                            <span>检测进度</span>
                            <span>{progress.current}/{progress.total}</span>
                        </div>
                        <div className="h-2 bg-muted rounded-full overflow-hidden">
                            <div
                                className="h-full bg-brand rounded-full transition-all duration-300"
                                style={{ width: `${(progress.current / progress.total) * 100}%` }}
                            />
                        </div>
                    </div>
                )}

                {/* 实时日志窗口 */}
                {logs.length > 0 && (
                    <div
                        ref={logRef}
                        className="bg-zinc-950 text-zinc-300 rounded-lg p-3 font-mono text-xs max-h-[200px] overflow-y-auto border border-zinc-800"
                    >
                        {logs.map((log, i) => (
                            <div key={i} className="py-0.5 leading-relaxed">
                                {log}
                            </div>
                        ))}
                        {checking && (
                            <div className="py-0.5 text-zinc-500 animate-pulse">▌</div>
                        )}
                    </div>
                )}

                {/* 空状态 */}
                {services.length === 0 && !checking && logs.length === 0 && (
                    <div className="text-center py-8 text-muted-foreground">
                        <Activity className="h-10 w-10 mx-auto mb-3 opacity-30" />
                        <p>点击「一键检测」检查所有外部服务连通性</p>
                        <p className="text-xs mt-1">使用 /v1/models 接口验证，不消耗任何 Token</p>
                    </div>
                )}

                {services.length > 0 && (
                    <>
                        {/* 汇总统计 */}
                        {summary && !checking && (
                            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
                                <div className="text-center p-3 rounded-lg bg-muted/50 border">
                                    <p className="text-2xl font-bold">{summary.total}</p>
                                    <p className="text-xs text-muted-foreground">总计</p>
                                </div>
                                <div className="text-center p-3 rounded-lg bg-green-50 border border-green-200">
                                    <p className="text-2xl font-bold text-green-600">{summary.ok}</p>
                                    <p className="text-xs text-green-600">正常</p>
                                </div>
                                <div className="text-center p-3 rounded-lg bg-red-50 border border-red-200">
                                    <p className="text-2xl font-bold text-red-600">{summary.error}</p>
                                    <p className="text-xs text-red-600">异常</p>
                                </div>
                                <div className="text-center p-3 rounded-lg bg-amber-50 border border-amber-200">
                                    <p className="text-2xl font-bold text-amber-600">{summary.unconfigured}</p>
                                    <p className="text-xs text-amber-600">未配置</p>
                                </div>
                            </div>
                        )}

                        {/* 服务列表 */}
                        <div className="border rounded-lg overflow-hidden">
                            <table className="w-full text-sm">
                                <thead className="bg-muted/50">
                                    <tr>
                                        <th className="text-left px-4 py-2 font-medium">状态</th>
                                        <th className="text-left px-4 py-2 font-medium">服务名称</th>
                                        <th className="text-left px-4 py-2 font-medium">类型</th>
                                        <th className="text-left px-4 py-2 font-medium">延迟</th>
                                        <th className="text-left px-4 py-2 font-medium">信息</th>
                                    </tr>
                                </thead>
                                <tbody className="divide-y">
                                    {services.map(s => (
                                        <tr key={s.id} className={s.status === 'error' ? 'bg-red-50/50' : ''}>
                                            <td className="px-4 py-3">
                                                <div className="flex items-center gap-2">
                                                    {statusIcon(s.status)}
                                                    {statusBadge(s.status)}
                                                </div>
                                            </td>
                                            <td className="px-4 py-3 font-medium">{s.name}</td>
                                            <td className="px-4 py-3">
                                                <Badge variant="outline" className="text-xs">{s.type}</Badge>
                                            </td>
                                            <td className="px-4 py-3 font-mono text-xs">
                                                {s.latency_ms !== null ? `${s.latency_ms}ms` : '-'}
                                            </td>
                                            <td className="px-4 py-3 text-xs text-muted-foreground max-w-[300px] truncate" title={s.message}>
                                                {s.message}
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>

                        {/* 检查时间 */}
                        {checkedAt && (
                            <p className="text-xs text-muted-foreground text-right">
                                检查时间: {new Date(checkedAt).toLocaleString('zh-CN')}
                            </p>
                        )}
                    </>
                )}
            </CardContent>
        </Card>
    );
}



/** 默认AI操盘手配置（管理员专用） */
function DefaultWriterConfig() {
    const [writers, setWriters] = useState<{ id: string; name: string }[]>([]);
    const [currentId, setCurrentId] = useState('');
    const [saving, setSaving] = useState(false);

    useEffect(() => {
        Promise.all([
            authFetch('/api/advisors?role=writer').then(r => r.json()),
            authFetch('/api/advisors/config/default-writer').then(r => r.json()),
        ]).then(([advData, cfgData]) => {
            if (advData.advisors) setWriters(advData.advisors.map((a: any) => ({ id: a.id, name: a.name })));
            if (cfgData.default_writer_id) setCurrentId(cfgData.default_writer_id);
        }).catch(() => {});
    }, []);

    const handleSave = async (id: string) => {
        setSaving(true);
        try {
            const res = await authFetch('/api/advisors/config/default-writer', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ writer_id: id }),
            });
            const data = await res.json();
            if (data.success) {
                setCurrentId(id);
                toast.success('默认操盘手已更新');
            }
        } catch {
            toast.error('保存失败');
        } finally {
            setSaving(false);
        }
    };

    if (writers.length === 0) return null;

    return (
        <Card>
            <CardHeader className="pb-3">
                <CardTitle className="text-base">默认AI操盘手</CardTitle>
                <CardDescription>所有用户生成脚本时默认注入的操盘手方法论。切换后新生成的脚本立即生效。</CardDescription>
            </CardHeader>
            <CardContent>
                <div className="flex items-center gap-3">
                    <Select value={currentId} onValueChange={handleSave} disabled={saving}>
                        <SelectTrigger className="w-48">
                            <SelectValue placeholder="选择操盘手" />
                        </SelectTrigger>
                        <SelectContent>
                            {writers.map(w => (
                                <SelectItem key={w.id} value={w.id}>{w.name}</SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                    {saving && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
                </div>
            </CardContent>
        </Card>
    );
}


export function SettingsPage() {
    // v3.6 白标(oem)：系统版本行用代理品牌名(oem)，否则平台默认。
    // D2（Owner 2026-07-22）：product_name 仅 backoffice 独立授权才出；未授权 hook 已解析平台默认(SSOT)。
    const { user } = useAuth();
    const { brand: settingsBrand, backofficeBrandAllowed } = useBranding({ userId: user?.id });
    const settingsBrandName = (backofficeBrandAllowed ? settingsBrand?.product_name : undefined) || settingsBrand?.company_name || '';
    const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [saveStatus, setSaveStatus] = useState<{ success: boolean; message: string } | null>(null);
    const [testingConnection, setTestingConnection] = useState<string | null>(null);
    const [connectionStatus, setConnectionStatus] = useState<Record<string, boolean | null>>({});
    const [showKeys, setShowKeys] = useState<Record<string, boolean>>({});

    useEffect(() => {
        loadSettings();
    }, []);

    // 自动隐藏保存状态
    useEffect(() => {
        if (saveStatus) {
            const timer = setTimeout(() => setSaveStatus(null), 4000);
            return () => clearTimeout(timer);
        }
    }, [saveStatus]);

    const loadSettings = async () => {
        // 15s 超时兜底：避免接口 hang 导致"加载设置..."永久转圈（sprint0_findings.md P1-002）
        const timeoutPromise = new Promise<never>((_, reject) =>
            setTimeout(() => reject(new Error('加载设置超时 (15s)，请检查网络或联系管理员')), 15000)
        );
        try {
            const res = await Promise.race([settingsApi.get(), timeoutPromise]) as Awaited<ReturnType<typeof settingsApi.get>>;
            if (res.data) {
                setSettings({ ...DEFAULT_SETTINGS, ...res.data });
            }
        } catch (err: any) {
            console.error("加载设置失败:", err);
            setSaveStatus({ success: false, message: err?.message || '加载设置失败，请刷新重试' });
        } finally {
            setLoading(false);
        }
    };

    const saveSettings = async () => {
        const familyTotal = FAMILY_RATIO_FIELDS.reduce(
            (sum, family) => sum + familyRatioValue(settings.style_ratios, family),
            0,
        );
        if (familyTotal !== 100) {
            setSaveStatus({ success: false, message: "六类文体比例合计必须为 100%" });
            return;
        }
        setSaving(true);
        setSaveStatus(null);
        try {
            await settingsApi.update(settings);
            setSaveStatus({ success: true, message: "✓ 设置已成功保存到 settings.json" });
        } catch (err) {
            console.error("保存设置失败:", err);
            setSaveStatus({ success: false, message: "✗ 保存失败，请检查后端连接" });
        } finally {
            setSaving(false);
        }
    };

    const testConnection = async (provider: string) => {
        setTestingConnection(provider);
        try {
            const res = await settingsApi.testConnection(provider);
            setConnectionStatus(prev => ({ ...prev, [provider]: res.data.success }));
        } catch {
            setConnectionStatus(prev => ({ ...prev, [provider]: false }));
        } finally {
            setTestingConnection(null);
        }
    };

    const updateSetting = <K extends keyof Settings>(key: K, value: Settings[K]) => {
        setSettings(prev => ({ ...prev, [key]: value }));
    };

    const updateTaskConfig = (
        section: "diagnosis_tasks" | "writing_tasks" | "social_tasks" | "employee_tasks",
        taskId: string,
        field: "provider" | "model",
        value: string
    ) => {
        setSettings(prev => ({
            ...prev,
            [section]: {
                ...prev[section],
                [taskId]: {
                    ...prev[section][taskId],
                    [field]: value
                }
            }
        }));
    };

    const toggleShowKey = (key: string) => {
        setShowKeys(prev => ({ ...prev, [key]: !prev[key] }));
    };

    const renderConnectionBadge = (provider: string) => {
        const status = connectionStatus[provider];
        if (status === null || status === undefined) return null;
        return status ? (
            <Badge className="bg-green-500"><Check className="h-3 w-3 mr-1" />已连接</Badge>
        ) : (
            <Badge variant="destructive"><X className="h-3 w-3 mr-1" />连接失败</Badge>
        );
    };

    // 渲染API Key输入框
    /**
     * renderApiKeyInput
     *   - hideTestButton: 给非 API Key 的 secret (例如豆包 Endpoint ID) 用 · 不显示测试连接按钮但保持其他样式一致
     *   - hint: 可选 hint 文字在输入框下方 (例如 "Seed 2.0 Pro 可直接用模型名")
     */
    const renderApiKeyInput = (
        keyName: keyof Settings,
        label: string,
        provider: string,
        placeholder: string,
        opts?: { hideTestButton?: boolean; hint?: string }
    ) => (
        <div className="space-y-2">
            <div className="flex items-center justify-between">
                <Label className="text-sm">{label}</Label>
                {!opts?.hideTestButton && (
                    <div className="flex items-center gap-2">
                        {renderConnectionBadge(provider)}
                        <Button
                            variant="outline"
                            size="sm"
                            onClick={() => testConnection(provider)}
                            disabled={testingConnection === provider}
                        >
                            {testingConnection === provider ? (
                                <Loader2 className="h-4 w-4 animate-spin" />
                            ) : (
                                <RefreshCw className="h-4 w-4" />
                            )}
                        </Button>
                    </div>
                )}
            </div>
            <div className="flex gap-2">
                <Input
                    type={showKeys[provider] ? "text" : "password"}
                    value={settings[keyName] as string}
                    onChange={(e) => updateSetting(keyName, e.target.value)}
                    placeholder={placeholder}
                    className="font-mono text-sm"
                />
                <Button variant="ghost" size="icon" onClick={() => toggleShowKey(provider)}>
                    {showKeys[provider] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </Button>
            </div>
            {opts?.hint && (
                <p className="text-xs text-muted-foreground">{opts.hint}</p>
            )}
        </div>
    );

    // 渲染子任务LLM配置
    const renderTaskLLMConfig = (
        task: { id: string; name: string; icon: any; desc: string },
        section: "diagnosis_tasks" | "writing_tasks" | "social_tasks" | "employee_tasks"
    ) => {
        const taskConfig = settings[section][task.id] || { provider: "dashscope", model: "" };
        const currentProvider = PROVIDERS.find(p => p.id === taskConfig.provider);
        const IconComponent = task.icon;

        return (
            <div key={task.id} className="p-4 border rounded-lg space-y-3 bg-muted/50">
                <div className="flex items-center gap-2">
                    <IconComponent className="h-4 w-4 text-brand" />
                    <span className="font-medium">{task.name}</span>
                    <span className="text-xs text-muted-foreground">- {task.desc}</span>
                </div>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                    <div className="space-y-1">
                        <Label className="text-xs">服务商</Label>
                        <Select
                            value={taskConfig.provider}
                            onValueChange={(v) => updateTaskConfig(section, task.id, "provider", v)}
                        >
                            <SelectTrigger className="h-9">
                                <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                                {PROVIDERS.map(p => (
                                    <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                    </div>
                    <div className="space-y-1">
                        <Label className="text-xs">模型名称</Label>
                        <Input
                            className="h-9"
                            value={taskConfig.model}
                            onChange={(e) => updateTaskConfig(section, task.id, "model", e.target.value)}
                            placeholder={currentProvider?.placeholder || "输入模型名称"}
                        />
                    </div>
                </div>
            </div>
        );
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center h-96">
                <Loader2 className="h-8 w-8 animate-spin text-brand" />
                <span className="ml-2">加载设置...</span>
            </div>
        );
    }

    return (
        <div className="p-3 md:p-6 space-y-3">
            {/* 保存状态反馈 */}
            {saveStatus && (
                <div className={`px-3 py-2 rounded-lg text-sm ${saveStatus.success
                    ? "bg-green-500/10 text-green-400 border border-green-500/20"
                    : "bg-red-500/10 text-red-400 border border-red-500/20"
                }`}>
                    {saveStatus.message}
                </div>
            )}

            <Tabs defaultValue="models" className="space-y-3">
                {/* Tab 栏 + 保存按钮同行 */}
                <div className="flex items-center gap-2 border-b border-border pb-2">
                    <TabsList className="flex gap-0.5 h-auto bg-transparent p-0 overflow-x-auto flex-1 min-w-0">
                        <TabsTrigger value="models" className="data-[state=active]:bg-foreground data-[state=active]:text-background rounded-lg text-xs px-2.5 py-1.5 whitespace-nowrap shrink-0">
                            AI模型
                        </TabsTrigger>
                        <TabsTrigger value="content" className="data-[state=active]:bg-foreground data-[state=active]:text-background rounded-lg text-xs px-2.5 py-1.5 whitespace-nowrap shrink-0">
                            GEO写作
                        </TabsTrigger>
                        <TabsTrigger value="quote" className="data-[state=active]:bg-foreground data-[state=active]:text-background rounded-lg text-xs px-2.5 py-1.5 whitespace-nowrap shrink-0">
                            报价
                        </TabsTrigger>
                        <TabsTrigger value="security" className="data-[state=active]:bg-foreground data-[state=active]:text-background rounded-lg text-xs px-2.5 py-1.5 whitespace-nowrap shrink-0">
                            安全
                        </TabsTrigger>
                        <TabsTrigger value="advanced" className="data-[state=active]:bg-foreground data-[state=active]:text-background rounded-lg text-xs px-2.5 py-1.5 whitespace-nowrap shrink-0">
                            高级
                        </TabsTrigger>
                        <TabsTrigger value="system" className="data-[state=active]:bg-foreground data-[state=active]:text-background rounded-lg text-xs px-2.5 py-1.5 whitespace-nowrap shrink-0">
                            系统
                        </TabsTrigger>
                        <TabsTrigger value="publish" className="data-[state=active]:bg-foreground data-[state=active]:text-background rounded-lg text-xs px-2.5 py-1.5 whitespace-nowrap shrink-0">
                            发布配置
                        </TabsTrigger>
                    </TabsList>
                    <Button size="sm" onClick={saveSettings} disabled={saving} className="bg-foreground text-background hover:bg-foreground/90 shrink-0 h-7 text-xs px-3">
                        {saving ? <Loader2 className="mr-1 h-3 w-3 animate-spin" /> : <Save className="mr-1 h-3 w-3" />}
                        保存
                    </Button>
                </div>

                {/* AI模型配置 Tab */}
                <TabsContent value="models" className="space-y-4">
                    {/* API Key 配置 */}
                    <Card>
                        <CardHeader>
                            <CardTitle>API Key 配置</CardTitle>
                            <CardDescription>配置各AI服务商的API密钥（支持6个服务商）</CardDescription>
                        </CardHeader>
                        <CardContent className="grid grid-cols-1 md:grid-cols-2 gap-6">
                            {renderApiKeyInput("dashscope_api_key", "DashScope (阿里云)", "dashscope", "sk-xxxxxxxx")}
                            {renderApiKeyInput("deepseek_api_key", "DeepSeek", "deepseek", "sk-xxxxxxxx")}
                            {renderApiKeyInput("openrouter_api_key", "OpenRouter", "openrouter", "sk-or-xxxxxxxx")}
                            {renderApiKeyInput("doubao_api_key", "豆包 (字节跳动)", "doubao", "xxxxxxxx")}
                            {renderApiKeyInput("kimi_api_key", "Kimi (月之暗面)", "kimi", "sk-xxxxxxxx")}
                            {renderApiKeyInput("tikhub_api_key", "TikHub (社媒数据)", "tikhub", "xxxxxxxx")}
                            {renderApiKeyInput("siliconflow_api_key", "SiliconFlow (DeepSeek备用)", "siliconflow", "sk-xxxxxxxx")}
                            {renderApiKeyInput("metaso_api_key", "Metaso (诊断搜索引擎)", "metaso", "xxxxxxxx")}
                            {/* P14-v14 (2026-05-28 老板): Jina Reader (调研监测 stage_3 抓取原文) */}
                            {renderApiKeyInput("jina_api_key", "Jina Reader (调研抓原文)", "jina", "jina_xxxxxxxx")}
                            {/* 豆包 Endpoint ID · 复用 renderApiKeyInput 让样式跟其他 API Key 一致
                                hideTestButton=true 因为 Endpoint ID 不是 key · 没独立测试连接接口
                                provider 用唯一 id 'doubao_endpoint' · 跟 'doubao' 隔开 toggleShowKey 状态 */}
                            {renderApiKeyInput(
                                "doubao_endpoint_id",
                                "豆包 Endpoint ID",
                                "doubao_endpoint",
                                "ep-xxxxxxxx",
                                {
                                    hideTestButton: true,
                                    hint: "旧模型需要 Endpoint ID，Seed 2.0 Pro 可直接用模型名",
                                }
                            )}
                        </CardContent>
                    </Card>

                    {/* 调研报告板块LLM配置 */}
                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <Search className="h-5 w-5 text-brand" />
                                调研报告板块 LLM
                            </CardTitle>
                            <CardDescription>GEO诊断、数据分析、报告生成等任务的LLM配置</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            {/* 默认配置 */}
                            <div className="p-4 border border-border rounded-lg bg-muted/50">
                                <div className="flex items-center gap-2 mb-3">
                                    <span className="font-medium">默认配置</span>
                                    <span className="text-xs text-muted-foreground">（未单独配置的任务使用此设置）</span>
                                </div>
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                    <div className="space-y-2">
                                        <Label>服务商</Label>
                                        <Select
                                            value={settings.diagnosis_provider}
                                            onValueChange={(v) => updateSetting("diagnosis_provider", v)}
                                        >
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {PROVIDERS.map(p => (
                                                    <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <Label>模型名称</Label>
                                        <Input
                                            value={settings.diagnosis_model}
                                            onChange={(e) => updateSetting("diagnosis_model", e.target.value)}
                                            placeholder={PROVIDERS.find(p => p.id === settings.diagnosis_provider)?.placeholder}
                                        />
                                    </div>
                                </div>
                            </div>

                            {/* 子任务配置 */}
                            <div className="space-y-3">
                                <Label className="text-sm font-medium">子任务LLM配置（可为每个任务指定不同模型）</Label>
                                {DIAGNOSIS_TASKS.map(task => renderTaskLLMConfig(task, "diagnosis_tasks"))}
                            </div>
                        </CardContent>
                    </Card>

                    {/* 写作板块LLM配置 */}
                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <PenTool className="h-5 w-5 text-green-600" />
                                写作板块 LLM
                            </CardTitle>
                            <CardDescription>文章生成、标题优化、内容创作等任务的LLM配置</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            {/* 默认配置 */}
                            <div className="p-4 border border-border rounded-lg bg-muted/50">
                                <div className="flex items-center gap-2 mb-3">
                                    <span className="font-medium">默认配置</span>
                                    <span className="text-xs text-muted-foreground">（未单独配置的任务使用此设置）</span>
                                </div>
                                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                    <div className="space-y-2">
                                        <Label>服务商</Label>
                                        <Select
                                            value={settings.writing_provider}
                                            onValueChange={(v) => updateSetting("writing_provider", v)}
                                        >
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {PROVIDERS.map(p => (
                                                    <SelectItem key={p.id} value={p.id}>{p.name}</SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <Label>模型名称</Label>
                                        <Input
                                            value={settings.writing_model}
                                            onChange={(e) => updateSetting("writing_model", e.target.value)}
                                            placeholder={PROVIDERS.find(p => p.id === settings.writing_provider)?.placeholder}
                                        />
                                    </div>
                                </div>
                            </div>

                            {/* 子任务配置 */}
                            <div className="space-y-3">
                                <Label className="text-sm font-medium">子任务LLM配置（可为每个任务指定不同模型）</Label>
                                {WRITING_TASKS.map(task => renderTaskLLMConfig(task, "writing_tasks"))}
                            </div>
                        </CardContent>
                    </Card>

                    {/* 社媒操盘手 LLM */}
                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <PenTool className="h-5 w-5 text-pink-500" />
                                社媒操盘手 LLM
                            </CardTitle>
                            <CardDescription>选题、脚本、仿写等任务的LLM配置</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="space-y-3">
                                {SOCIAL_TASKS.map(task => renderTaskLLMConfig(task, "social_tasks"))}
                            </div>
                        </CardContent>
                    </Card>

                    {/* AI员工 LLM */}
                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <Bot className="h-5 w-5 text-green-500" />
                                AI员工 LLM
                            </CardTitle>
                            <CardDescription>AI员工使用的LLM模型配置</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="space-y-3">
                                {EMPLOYEE_TASKS.map(task => renderTaskLLMConfig(task, "employee_tasks"))}
                            </div>
                        </CardContent>
                    </Card>

                    {/* P12 (2026-05-26): GEO 调研抓取 4 平台模型 · 数据源 geo_research_config
                        独立组件 · 自管 state + 独立保存按钮 · 不入 settings.json */}
                    <GeoResearchExtractLLMCard />
                </TabsContent>

                {/* GEO写作配置 Tab */}
                <TabsContent value="content" className="space-y-4">
                    <DefaultWriterConfig />

                    <Card>
                        <CardHeader className="pb-3">
                            <CardTitle className="text-base">历史内容角度（只读兼容）</CardTitle>
                            <CardDescription>
                                旧版八种内容角度不再参与运营配比，也不再形成第二套文体口径。历史值只用于读取旧文章；新文章统一按下方六类文体生成和进化。
                            </CardDescription>
                        </CardHeader>
                    </Card>

                    {/* GEO v1.4: operations only sees the six-family SSOT. */}
                    <Card>
                        <CardHeader className="pb-3">
                            <CardTitle className="text-base">GEO 文章文体比例（六类 SSOT）</CardTitle>
                            <CardDescription>运营只调整六类目标文体 · 内部兼容引擎由系统拆分 · 三类历史高风险模板固定为 0</CardDescription>
                        </CardHeader>
                        <CardContent>
                            <div className="grid grid-cols-2 sm:grid-cols-3 gap-3">
                                {FAMILY_RATIO_FIELDS.map(family => (
                                    <div key={family.id} className="flex items-center gap-2 p-2 rounded-lg bg-muted/50">
                                        <span className="text-xs text-muted-foreground flex-1 truncate">{family.name}</span>
                                        <Input
                                            type="number"
                                            value={familyRatioValue(settings.style_ratios, family)}
                                            onChange={(e) => setSettings(prev => ({
                                                ...prev,
                                                style_ratios: updateFamilyRatio(
                                                    prev.style_ratios,
                                                    family,
                                                    parseInt(e.target.value) || 0,
                                                ),
                                            }))}
                                            min={0} max={100}
                                            className="w-14 h-7 text-xs text-center px-1 tabular-nums"
                                        />
                                        <span className="text-xs text-muted-foreground">%</span>
                                    </div>
                                ))}
                            </div>
                            <div className="flex items-center justify-end gap-2 mt-3 text-sm">
                                <span className="text-muted-foreground">六类总计</span>
                                <span className={FAMILY_RATIO_FIELDS.reduce((sum, family) => sum + familyRatioValue(settings.style_ratios, family), 0) === 100 ? "text-green-400 font-medium" : "text-red-400 font-medium"}>
                                    {FAMILY_RATIO_FIELDS.reduce((sum, family) => sum + familyRatioValue(settings.style_ratios, family), 0)}%
                                </span>
                            </div>
                        </CardContent>
                    </Card>

                    {/* 行业安全配比只读展示；旧工程文体仅在后端兼容，不向运营暴露。 */}
                    <Card>
                        <CardHeader className="pb-3">
                            <CardTitle className="text-base">行业 override · 医疗 / 法律 hard rule(只读)</CardTitle>
                            <CardDescription>医疗与法律行业不生成榜单式内容，统一采用证据问答、实施指南和风险分析。</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3 text-xs">
                            {Object.entries(settings.industry_overrides ?? INDUSTRY_OVERRIDES_DEFAULT).map(([industry, override]) => (
                                <div key={industry} className="p-3 rounded-lg bg-muted/30 border border-border/30">
                                    <div className="font-medium text-foreground mb-2">{industry}</div>
                                    <div className="grid grid-cols-1 gap-2">
                                        <div>
                                            <div className="text-muted-foreground mb-1">六类文体比例：</div>
                                            <div className="space-y-0.5 tabular-nums">
                                                {FAMILY_RATIO_FIELDS.map((family) => (
                                                    <div key={family.id} className="flex justify-between gap-3">
                                                        <span>{family.name}</span>
                                                        <span className="text-muted-foreground">{familyRatioValue(override.style_ratios, family)}%</span>
                                                    </div>
                                                ))}
                                            </div>
                                        </div>
                                    </div>
                                </div>
                            ))}
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle className="text-base">GEO 批量生成参数</CardTitle>
                            <CardDescription>GEO 写作大厅的并发和数量配置</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>并发写手数</Label>
                                    <Select
                                        value={settings.concurrent_writers.toString()}
                                        onValueChange={(v) => updateSetting("concurrent_writers", parseInt(v))}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="3">3 (保守)</SelectItem>
                                            <SelectItem value="5">5 (稳定)</SelectItem>
                                            <SelectItem value="10">10 (推荐)</SelectItem>
                                            <SelectItem value="15">15 (激进)</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">并发越高，生成越快，但可能增加API错误率</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>默认文章数量</Label>
                                    <Input
                                        type="number"
                                        value={settings.default_article_count}
                                        onChange={(e) => updateSetting("default_article_count", parseInt(e.target.value) || 40)}
                                        min={10}
                                        max={200}
                                    />
                                    <p className="text-xs text-muted-foreground">新建诊断时的默认文章生成数量</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle className="text-base">GEO 文章高级配置</CardTitle>
                            <CardDescription>超时、重试、竞品植入规则（仅影响 GEO 写作大厅）</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>单篇超时时间 (秒)</Label>
                                    <Input
                                        type="number"
                                        value={settings.article_timeout}
                                        onChange={(e) => updateSetting("article_timeout", parseInt(e.target.value) || 180)}
                                        min={60}
                                        max={600}
                                    />
                                    <p className="text-xs text-muted-foreground">单篇文章生成的最大等待时间</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>失败重试次数</Label>
                                    <Input
                                        type="number"
                                        value={settings.article_retry_count}
                                        onChange={(e) => updateSetting("article_retry_count", parseInt(e.target.value) || 2)}
                                        min={0}
                                        max={5}
                                    />
                                    <p className="text-xs text-muted-foreground">生成失败时的重试次数</p>
                                </div>
                            </div>
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                <div className="space-y-2">
                                    <Label>客户排名位置</Label>
                                    <Select
                                        value={settings.article_client_position.toString()}
                                        onValueChange={(v) => updateSetting("article_client_position", parseInt(v))}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="1">第1名 (最强曝光)</SelectItem>
                                            <SelectItem value="2">第2名 (可信度高)</SelectItem>
                                            <SelectItem value="3">第3名 (稳健)</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">客户在排名文章中的位置</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>客户内容占比 (%)</Label>
                                    <Input
                                        type="number"
                                        value={Math.round((settings.article_client_coverage || 0.5) * 100)}
                                        onChange={(e) => updateSetting("article_client_coverage", (parseInt(e.target.value) || 50) / 100)}
                                        min={30}
                                        max={80}
                                    />
                                    <p className="text-xs text-muted-foreground">文章中客户内容的比例</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>最大竞品数</Label>
                                    <Input
                                        type="number"
                                        value={settings.article_max_competitors}
                                        onChange={(e) => updateSetting("article_max_competitors", parseInt(e.target.value) || 4)}
                                        min={2}
                                        max={10}
                                    />
                                    <p className="text-xs text-muted-foreground">每篇文章最多展示的竞品数量</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </TabsContent>

                {/* 报价配置 Tab */}
                <TabsContent value="quote" className="space-y-4">
                    <Card>
                        <CardHeader>
                            <CardTitle>报价模式</CardTitle>
                            <CardDescription>选择新报价使用的定价模式</CardDescription>
                        </CardHeader>
                        <CardContent>
                            <div className="flex items-center justify-between">
                                <div>
                                    <Label className="text-sm font-medium">主题包模式</Label>
                                    <p className="text-xs text-muted-foreground mt-0.5">
                                        {settings.quote_cluster_mode
                                            ? '新报价将按「主题包」聚类定价，客户选包而非选词'
                                            : '新报价将使用传统「逐词定价」模式'}
                                    </p>
                                </div>
                                <button
                                    type="button"
                                    role="switch"
                                    aria-checked={settings.quote_cluster_mode}
                                    onClick={() => updateSetting("quote_cluster_mode", !settings.quote_cluster_mode)}
                                    className={`relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${settings.quote_cluster_mode ? 'bg-brand' : 'bg-muted'}`}
                                >
                                    <span className={`pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow-lg ring-0 transition-transform ${settings.quote_cluster_mode ? 'translate-x-5' : 'translate-x-0'}`} />
                                </button>
                            </div>
                        </CardContent>
                    </Card>
                    <Card>
                        <CardHeader>
                            <CardTitle>成本配置</CardTitle>
                            <CardDescription>设置单篇文章的各项成本（元）</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                <div className="space-y-2">
                                    <Label>内容创作成本</Label>
                                    <Input
                                        type="number"
                                        value={settings.quote_content_cost}
                                        onChange={(e) => updateSetting("quote_content_cost", parseInt(e.target.value) || 20)}
                                        min={0}
                                    />
                                    <p className="text-xs text-muted-foreground">AI写作+人工审核</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>媒体发布费</Label>
                                    <Input
                                        type="number"
                                        value={settings.quote_media_cost}
                                        onChange={(e) => updateSetting("quote_media_cost", parseInt(e.target.value) || 30)}
                                        min={0}
                                    />
                                    <p className="text-xs text-muted-foreground">权威平台发布</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>运营执行成本</Label>
                                    <Input
                                        type="number"
                                        value={settings.quote_operation_cost}
                                        onChange={(e) => updateSetting("quote_operation_cost", parseInt(e.target.value) || 10)}
                                        min={0}
                                    />
                                    <p className="text-xs text-muted-foreground">监测与优化</p>
                                </div>
                            </div>
                            <div className="p-4 bg-muted rounded-lg">
                                <div className="flex justify-between items-center">
                                    <span className="font-medium">单篇总成本</span>
                                    <span className="text-xl font-bold text-brand">
                                        ￥{(settings.quote_content_cost || 0) + (settings.quote_media_cost || 0) + (settings.quote_operation_cost || 0)}/篇
                                    </span>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardHeader>
                            <CardTitle>利润与报价配置</CardTitle>
                            <CardDescription>设置售价倍率和默认参数</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>售价倍率</Label>
                                    <Input
                                        type="number"
                                        step="0.1"
                                        value={settings.quote_markup_ratio}
                                        onChange={(e) => updateSetting("quote_markup_ratio", parseFloat(e.target.value) || 1.0)}
                                        min={1}
                                        max={5}
                                    />
                                    <p className="text-xs text-muted-foreground">售价 = 成本 × 倍率（默认 1.0 = 成本价 · 服务商自行往上加利润）</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>默认目标占比 (%)</Label>
                                    <Input
                                        type="number"
                                        value={Math.round((settings.quote_default_target_share || 0.20) * 100)}
                                        onChange={(e) => updateSetting("quote_default_target_share", (parseInt(e.target.value) || 25) / 100)}
                                        min={10}
                                        max={50}
                                    />
                                    <p className="text-xs text-muted-foreground">报价时的默认市场占比目标</p>
                                </div>
                            </div>
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>AI引用条数</Label>
                                    <Input
                                        type="number"
                                        value={settings.quote_ai_reference_count}
                                        onChange={(e) => updateSetting("quote_ai_reference_count", parseInt(e.target.value) || 4)}
                                        min={1}
                                        max={10}
                                    />
                                    <p className="text-xs text-muted-foreground">AI每次回答平均引用的条数</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>批量查询并发数</Label>
                                    <Input
                                        type="number"
                                        value={settings.quote_batch_concurrency}
                                        onChange={(e) => updateSetting("quote_batch_concurrency", parseInt(e.target.value) || 10)}
                                        min={1}
                                        max={20}
                                    />
                                    <p className="text-xs text-muted-foreground">批量报价时的并发请求数</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </TabsContent>

                {/* 安全配置 Tab - 操作确认码 */}
                <TabsContent value="security" className="space-y-4">
                    <ConfirmCodeSection />
                </TabsContent>

                {/* 高级配置 Tab */}
                <TabsContent value="advanced" className="space-y-4">
                    {/* 诊断系统配置 */}
                    <Card>
                        <CardHeader>
                            <CardTitle>诊断系统配置</CardTitle>
                            <CardDescription>AI引擎测试、缓存和搜索参数</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                <div className="space-y-2">
                                    <Label>AI测试超时 (秒)</Label>
                                    <Input
                                        type="number"
                                        value={settings.diagnosis_ai_test_timeout}
                                        onChange={(e) => updateSetting("diagnosis_ai_test_timeout", parseInt(e.target.value) || 180)}
                                        min={60}
                                        max={600}
                                    />
                                    <p className="text-xs text-muted-foreground">AI引擎测试的超时时间</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>启用步骤缓存</Label>
                                    <Select
                                        value={settings.diagnosis_step_cache ? "true" : "false"}
                                        onValueChange={(v) => updateSetting("diagnosis_step_cache", v === "true")}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="true">启用</SelectItem>
                                            <SelectItem value="false">禁用</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">缓存诊断步骤结果</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>竞品搜索数量</Label>
                                    <Input
                                        type="number"
                                        value={settings.diagnosis_metaso_size}
                                        onChange={(e) => updateSetting("diagnosis_metaso_size", parseInt(e.target.value) || 100)}
                                        min={20}
                                        max={200}
                                    />
                                    <p className="text-xs text-muted-foreground">秘塔搜索返回的结果数量</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    {/* 社媒操盘手配置 */}
                    <Card>
                        <CardHeader>
                            <CardTitle>社媒操盘手配置</CardTitle>
                            <CardDescription>ASR转录、TikHub API参数</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                <div className="space-y-2">
                                    <Label>ASR超时 (秒)</Label>
                                    <Input
                                        type="number"
                                        value={settings.social_asr_timeout}
                                        onChange={(e) => updateSetting("social_asr_timeout", parseInt(e.target.value) || 180)}
                                        min={60}
                                        max={600}
                                    />
                                    <p className="text-xs text-muted-foreground">语音转文字超时时间</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>ASR模型</Label>
                                    <Input
                                        value={settings.social_asr_model}
                                        onChange={(e) => updateSetting("social_asr_model", e.target.value)}
                                        placeholder="qwen3-asr-flash"
                                    />
                                    <p className="text-xs text-muted-foreground">DashScope ASR模型名称</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>TikHub超时 (秒)</Label>
                                    <Input
                                        type="number"
                                        value={settings.social_tikhub_timeout}
                                        onChange={(e) => updateSetting("social_tikhub_timeout", parseInt(e.target.value) || 120)}
                                        min={30}
                                        max={300}
                                    />
                                    <p className="text-xs text-muted-foreground">TikHub API超时时间</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    {/* 知识库配置 */}
                    <Card>
                        <CardHeader>
                            <CardTitle>知识库配置</CardTitle>
                            <CardDescription>LLM清洗、向量化和重排序参数</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>启用LLM清洗</Label>
                                    <Select
                                        value={settings.kb_llm_clean_enabled ? "true" : "false"}
                                        onValueChange={(v) => updateSetting("kb_llm_clean_enabled", v === "true")}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="true">启用</SelectItem>
                                            <SelectItem value="false">禁用</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">上传时使用LLM清洗内容</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>LLM清洗模型</Label>
                                    <Input
                                        value={settings.kb_llm_clean_model}
                                        onChange={(e) => updateSetting("kb_llm_clean_model", e.target.value)}
                                        placeholder="qwen3.7-max"
                                    />
                                    <p className="text-xs text-muted-foreground">清洗使用的模型</p>
                                </div>
                            </div>
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>Embedding模型</Label>
                                    <Input
                                        value={settings.kb_embedding_model}
                                        onChange={(e) => updateSetting("kb_embedding_model", e.target.value)}
                                        placeholder="text-embedding-v4"
                                    />
                                    <p className="text-xs text-muted-foreground">向量化使用的模型</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>启用重排序</Label>
                                    <Select
                                        value={settings.kb_rerank_enabled ? "true" : "false"}
                                        onValueChange={(v) => updateSetting("kb_rerank_enabled", v === "true")}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="true">启用</SelectItem>
                                            <SelectItem value="false">禁用</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">检索时使用Rerank优化</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    {/* LLM全局配置 */}
                    <Card>
                        <CardHeader>
                            <CardTitle>LLM全局配置</CardTitle>
                            <CardDescription>所有LLM调用的默认参数</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                <div className="space-y-2">
                                    <Label>默认超时 (秒)</Label>
                                    <Input
                                        type="number"
                                        value={settings.llm_global_timeout}
                                        onChange={(e) => updateSetting("llm_global_timeout", parseInt(e.target.value) || 180)}
                                        min={30}
                                        max={600}
                                    />
                                    <p className="text-xs text-muted-foreground">LLM调用的默认超时时间</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>默认重试次数</Label>
                                    <Input
                                        type="number"
                                        value={settings.llm_global_max_retries}
                                        onChange={(e) => updateSetting("llm_global_max_retries", parseInt(e.target.value) || 2)}
                                        min={0}
                                        max={5}
                                    />
                                    <p className="text-xs text-muted-foreground">失败时的重试次数</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>默认温度</Label>
                                    <Input
                                        type="number"
                                        step="0.1"
                                        value={settings.llm_global_temperature}
                                        onChange={(e) => updateSetting("llm_global_temperature", parseFloat(e.target.value) || 0.7)}
                                        min={0}
                                        max={2}
                                    />
                                    <p className="text-xs text-muted-foreground">越高越有创造性(0-2)</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    {/* 公开报告 v3 */}
                    <Card>
                        <CardHeader>
                            <CardTitle>公开报告 v3</CardTitle>
                            <CardDescription>控制新版公开报告、LLM 扩写队列和月度预算</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                <div className="space-y-2">
                                    <Label>v3 全量开关</Label>
                                    <Select
                                        value={settings.report_v3_enabled ? "true" : "false"}
                                        onValueChange={(v) => updateSetting("report_v3_enabled", v === "true")}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="false">关闭</SelectItem>
                                            <SelectItem value="true">开启</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">开启后公开报告和 PDF 统一走 v3 视觉</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>自动扩写</Label>
                                    <Select
                                        value={settings.report_v3_auto_enrich ? "true" : "false"}
                                        onValueChange={(v) => updateSetting("report_v3_auto_enrich", v === "true")}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="false">关闭</SelectItem>
                                            <SelectItem value="true">开启</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">默认关闭，灰度验证后再自动入队</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>并发上限</Label>
                                    <Input
                                        type="number"
                                        value={settings.llm_narrative_max_concurrent}
                                        onChange={(e) => updateSetting("llm_narrative_max_concurrent", parseInt(e.target.value) || 3)}
                                        min={1}
                                        max={10}
                                    />
                                    <p className="text-xs text-muted-foreground">后台扩写 runner 每轮最多处理数量</p>
                                </div>
                            </div>
                            <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
                                <div className="space-y-2 sm:col-span-1">
                                    <Label>灰度用户 ID</Label>
                                    <Input
                                        value={(settings.report_v3_whitelist_user_ids || []).join(", ")}
                                        onChange={(e) => updateSetting(
                                            "report_v3_whitelist_user_ids",
                                            e.target.value
                                                .split(/[,，\s]+/)
                                                .map((item) => parseInt(item.trim(), 10))
                                                .filter((item) => Number.isFinite(item))
                                        )}
                                        placeholder="例如 1, 12, 37"
                                    />
                                    <p className="text-xs text-muted-foreground">为空时只允许 admin 预览</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>月预算 (元)</Label>
                                    <Input
                                        type="number"
                                        value={settings.llm_narrative_monthly_yuan}
                                        onChange={(e) => updateSetting("llm_narrative_monthly_yuan", parseFloat(e.target.value) || 100)}
                                        min={0}
                                        step="10"
                                    />
                                    <p className="text-xs text-muted-foreground">达到预算后停止新的 LLM 扩写</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>预警阈值 (元)</Label>
                                    <Input
                                        type="number"
                                        value={settings.llm_narrative_alert_yuan}
                                        onChange={(e) => updateSetting("llm_narrative_alert_yuan", parseFloat(e.target.value) || 80)}
                                        min={0}
                                        step="10"
                                    />
                                    <p className="text-xs text-muted-foreground">达到阈值时管理看板标记预警</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    {/* 高级选项 */}
                    <Card>
                        <CardHeader>
                            <CardTitle>高级选项</CardTitle>
                            <CardDescription>调试和系统级参数</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>调试模式</Label>
                                    <Select
                                        value={settings.debug_mode ? "true" : "false"}
                                        onValueChange={(v) => updateSetting("debug_mode", v === "true")}
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            <SelectItem value="false">关闭</SelectItem>
                                            <SelectItem value="true">开启</SelectItem>
                                        </SelectContent>
                                    </Select>
                                    <p className="text-xs text-muted-foreground">开启后输出详细调试日志</p>
                                </div>
                                <div className="space-y-2">
                                    <Label>SSE心跳间隔 (秒)</Label>
                                    <Input
                                        type="number"
                                        value={settings.sse_heartbeat_interval}
                                        onChange={(e) => updateSetting("sse_heartbeat_interval", parseInt(e.target.value) || 30)}
                                        min={10}
                                        max={120}
                                    />
                                    <p className="text-xs text-muted-foreground">实时进度推送的心跳间隔</p>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </TabsContent>

                {/* 系统信息 Tab */}
                <TabsContent value="system" className="space-y-4">
                    <Card>
                        <CardHeader>
                            <CardTitle>系统信息</CardTitle>
                            <CardDescription>当前系统版本和运行状态</CardDescription>
                        </CardHeader>
                        <CardContent>
                            <div className="space-y-3">
                                <div className="flex justify-between py-2 border-b">
                                    <span className="text-muted-foreground">系统版本</span>
                                    <span className="font-medium">{settingsBrandName} AI v3.1</span>
                                </div>
                                <div className="flex justify-between py-2 border-b">
                                    <span className="text-muted-foreground">前端框架</span>
                                    <span className="font-medium">React 18 + Vite + TypeScript</span>
                                </div>
                                <div className="flex justify-between py-2 border-b">
                                    <span className="text-muted-foreground">后端框架</span>
                                    <span className="font-medium">FastAPI + AgentScope</span>
                                </div>
                                <div className="flex justify-between py-2 border-b">
                                    <span className="text-muted-foreground">支持服务商</span>
                                    <span className="font-medium">DashScope / DeepSeek / OpenRouter / 豆包 / Kimi</span>
                                </div>
                                <div className="flex justify-between py-2">
                                    <span className="text-muted-foreground">配置文件</span>
                                    <span className="font-mono text-sm">settings.json</span>
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    {/* 服务状态监控 */}
                    <ServiceHealthCheck />
                </TabsContent>

                {/* 发布设置 Tab */}
                <TabsContent value="publish" className="space-y-4">
                    <PublishSettings />
                </TabsContent>
            </Tabs>
        </div>
    );
}
