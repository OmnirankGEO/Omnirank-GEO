/**
 * 客户营销资料Tab - 嵌入 BrandDetail 页面
 * 管理员编辑公司营销信息 + 5维度业务知识库 + AI顾问一键填写
 */
import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
    Loader2, Wand2, Save, ChevronDown, ChevronUp, X, Plus, Sparkles, Bot, Link2, Copy, Check, ExternalLink,
} from 'lucide-react';
import { authFetch } from '@/lib/api';
import { cn } from '@/lib/utils';
import { copyToClipboard, copyAsyncText } from '@/lib/copyUtils';
import { ManualCopyDialog } from '@/components/common/ManualCopyDialog';
import { BusinessKnowledgeCards } from '@/components/BusinessKnowledgeCards';

interface Advisor {
    id: string;
    name: string;
    avatar: string;
    description: string;
    specialty?: string;
}

// ========== Types ==========
interface ProfileData {
    id?: string;
    name?: string;
    industry?: string;
    business?: string;
    target_users?: string;
    products?: string[];
    pain_points?: string[];
    competitors?: string[];
    company_intro?: string;
    core_value?: string;
    selling_points?: string;
    success_cases?: string[];   // [WO_227-c1] 一行一个案例
    testimonials?: string;
    brand_id?: number;
}

interface StructuredKnowledge {
    products?: { name?: string; features?: string[]; scenarios?: string[]; metrics?: string };
    painPoints?: { scenario?: string; emotions?: string[]; consequences?: string; triggers?: string };
    customers?: { segments?: string[]; needs?: string[]; barriers?: string[]; concerns?: string[] };
    differentiation?: { competitors?: string[]; advantages?: string[]; usp?: string; killerData?: string[] };
    cases?: Array<{ client?: string; background?: string; solution?: string; results?: string; quote?: string }>;
}

interface Props {
    brandId: number;
    brandName: string;
    canEdit: boolean;
}

// ========== Tag Input ==========
function TagInput({ value: rawValue, onChange, placeholder, disabled }: {
    value: string[] | string;
    onChange: (v: string[]) => void;
    placeholder?: string;
    disabled?: boolean;
}) {
    const value: string[] = Array.isArray(rawValue)
        ? rawValue
        : (typeof rawValue === 'string' && rawValue
            ? (rawValue as string).split(/[,，、]+/).map((s: string) => s.trim()).filter(Boolean)
            : []);
    const [input, setInput] = useState('');
    const handleKeyDown = (e: React.KeyboardEvent) => {
        if ((e.key === 'Enter' || e.key === ',') && input.trim()) {
            e.preventDefault();
            if (!value.includes(input.trim())) {
                onChange([...value, input.trim()]);
            }
            setInput('');
        }
    };
    const remove = (idx: number) => onChange(value.filter((_: string, i: number) => i !== idx));
    return (
        <div className="flex flex-wrap gap-1.5 items-center p-2 border border-border rounded-lg bg-background min-h-[42px]">
            {value.map((tag: string, i: number) => (
                <Badge key={i} variant="secondary" className="gap-1 pr-1">
                    {tag}
                    {!disabled && (
                        <button onClick={() => remove(i)} className="ml-0.5 hover:text-destructive">
                            <X className="h-3 w-3" />
                        </button>
                    )}
                </Badge>
            ))}
            {!disabled && (
                <input
                    value={input}
                    onChange={e => setInput(e.target.value)}
                    onKeyDown={handleKeyDown}
                    placeholder={value.length === 0 ? placeholder : ''}
                    className="flex-1 min-w-[120px] bg-transparent border-none outline-hidden text-sm text-foreground placeholder:text-muted-foreground"
                />
            )}
        </div>
    );
}

// ========== Auto-resize Textarea ==========
// [WO_227-c1] `success_cases` 的形态**取决于写入方**:
// 已存的数组行回 string[];老的 plain_text 行回 string;AI 回填也给 string。
// 🔴 三种都要归一,不能假设它一定是数组 —— 生产上两种形态并存
//    (227-d1 实测:真品牌 json_array 9 行 / plain_text 8 行)。
// 🔴 一行一个案例:自由文本里的逗号、顿号是**句内标点**,不是分隔符。
//    老写法按逗号拆,把「某租车公司,3 个月询盘翻倍、成本降 20%」
//    切成了三条,还把顿号换成了逗号。
function toCaseLines(v: string[] | string | undefined | null): string[] {
    if (Array.isArray(v)) return v;
    if (typeof v === 'string') return v.split('\n').filter(s => s.trim());
    return [];
}


function AutoTextarea({ value, onChange, placeholder, disabled, rows = 3 }: {
    value: string;
    onChange: (v: string) => void;
    placeholder?: string;
    disabled?: boolean;
    rows?: number;
}) {
    return (
        <textarea
            value={value}
            onChange={e => onChange(e.target.value)}
            placeholder={placeholder}
            disabled={disabled}
            rows={rows}
            className="w-full p-3 border border-border rounded-lg bg-background text-sm text-foreground resize-y focus:outline-hidden focus:ring-2 focus:ring-ring disabled:opacity-60 disabled:cursor-not-allowed placeholder:text-muted-foreground"
        />
    );
}

// ========== Main Component ==========
export default function MarketingTab({ brandId, brandName, canEdit }: Props) {
    const [profile, setProfile] = useState<ProfileData>({});
    const [knowledge, setKnowledge] = useState<StructuredKnowledge>({});
    const [existingId, setExistingId] = useState<string | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [dirty, setDirty] = useState(false);
    const [toast, setToast] = useState<{ type: 'success' | 'error'; msg: string } | null>(null);
    // [WO_WHITELABEL_COPY_UX 项2] 剪贴板被拒但链接已拿到 → 弹可选中链接框
    const [manualCopyText, setManualCopyText] = useState<string | null>(null);

    // AI fill states
    const [showAiDialog, setShowAiDialog] = useState(false);
    const [aiText, setAiText] = useState('');
    const [aiLoading, setAiLoading] = useState(false);
    const [aiFilledFields, setAiFilledFields] = useState<Set<string>>(new Set());
    const [advisors, setAdvisors] = useState<Advisor[]>([]);
    const [selectedAdvisor, setSelectedAdvisor] = useState<string>('');

    // Section collapse
    const [detailOpen, setDetailOpen] = useState(true);
    const [knowledgeOpen, setKnowledgeOpen] = useState(true);

    // Material confirmation link
    const [confirmStatus, setConfirmStatus] = useState<{
        has_session: boolean;
        token?: string;
        status?: string;
        confirmed_at?: string;
        customer_notes?: string;
        url?: string;
    } | null>(null);
    const [generatingLink, setGeneratingLink] = useState(false);
    const [resending, setResending] = useState(false);
    const [linkCopied, setLinkCopied] = useState(false);

    // Toast auto-dismiss
    useEffect(() => {
        if (toast) {
            const t = setTimeout(() => setToast(null), 3000);
            return () => clearTimeout(t);
        }
    }, [toast]);

    // Load existing profile for this brand
    const loadProfile = useCallback(async () => {
        setLoading(true);
        try {
            const res = await authFetch('/api/profiles');
            const data = await res.json();
            if (data.success && data.profiles) {
                // 取 brand_id 匹配的所有档案，选 updated_at 最新的
                const matches = data.profiles.filter((p: any) =>
                    p.brand_id === brandId || String(p.brand_id) === String(brandId)
                );
                const match = matches.length > 1
                    ? matches.sort((a: any, b: any) => (b.updated_at || '').localeCompare(a.updated_at || ''))[0]
                    : matches[0];
                if (match) {
                    setExistingId(match.id);
                    setProfile({
                        name: match.name || brandName,
                        industry: match.industry || '',
                        business: match.business || '',
                        target_users: match.target_users || '',
                        products: match.products || [],
                        pain_points: match.pain_points || [],
                        competitors: match.competitors || [],
                        company_intro: match.company_intro || '',
                        core_value: match.core_value || '',
                        selling_points: match.selling_points || '',
                        success_cases: toCaseLines(match.success_cases),
                        testimonials: match.testimonials || '',
                        brand_id: brandId,
                    });
                    if (match.structured_knowledge) {
                        setKnowledge(match.structured_knowledge);
                    }
                } else {
                    setProfile({ name: brandName, brand_id: brandId });
                }
            }
        } catch (e) {
            console.error('Failed to load profile:', e);
        } finally {
            setLoading(false);
        }
    }, [brandId, brandName]);

    useEffect(() => {
        loadProfile();
    }, [loadProfile]);

    // Fetch advisors when dialog opens
    useEffect(() => {
        if (!showAiDialog || advisors.length > 0) return;
        (async () => {
            try {
                const res = await authFetch('/api/advisors');
                const data = await res.json();
                if (data.success && data.advisors) {
                    setAdvisors(data.advisors);
                    if (data.advisors.length > 0 && !selectedAdvisor) {
                        setSelectedAdvisor(data.advisors[0].id);
                    }
                }
            } catch (e) {
                console.error('Failed to load advisors:', e);
            }
        })();
    }, [showAiDialog]);

    // Update a field
    const updateField = (field: string, value: any) => {
        setProfile(prev => ({ ...prev, [field]: value }));
        setDirty(true);
        // Remove AI-filled label when user manually edits
        if (aiFilledFields.has(field)) {
            setAiFilledFields(prev => { const n = new Set(prev); n.delete(field); return n; });
        }
    };

    // Save profile
    const handleSave = async () => {
        if (!profile.name?.trim()) {
            setToast({ type: 'error', msg: '公司名称不能为空' });
            return;
        }
        setSaving(true);
        try {
            const payload = { ...profile, brand_id: brandId, structured_knowledge: knowledge };
            let res: Response;
            if (existingId) {
                res = await authFetch(`/api/profiles/${existingId}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            } else {
                res = await authFetch('/api/profiles', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload),
                });
            }
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || '保存失败');
            if (data.profile_id && !existingId) {
                setExistingId(data.profile_id);
            }
            setDirty(false);
            setAiFilledFields(new Set());
            setToast({ type: 'success', msg: '营销资料保存成功' });
        } catch (e: any) {
            setToast({ type: 'error', msg: e.message || '保存失败' });
        } finally {
            setSaving(false);
        }
    };

    // AI fill
    const handleAiFill = async () => {
        setAiLoading(true);
        try {
            const res = await authFetch('/api/profiles/ai-fill', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    brand_id: brandId,
                    advisor_id: selectedAdvisor || undefined,
                    text: aiText.trim() || undefined,
                }),
            });
            const result = await res.json();
            if (!res.ok) throw new Error(result.detail || 'AI分析失败');
            if (result.success && result.data) {
                const d = result.data;
                const filled = new Set<string>();
                const newProfile = { ...profile };

                // Fill basic fields
                const fieldMap: Record<string, string> = {
                    industry: 'industry', business: 'business', target_users: 'target_users',
                    company_intro: 'company_intro', core_value: 'core_value',
                    selling_points: 'selling_points', success_cases: 'success_cases',
                    testimonials: 'testimonials',
                };
                for (const [aiKey, profileKey] of Object.entries(fieldMap)) {
                    if (d[aiKey]) {
                        // [WO_227-c1] success_cases 现在是 string[],AI 给的是 string
                        (newProfile as any)[profileKey] =
                            profileKey === 'success_cases' ? toCaseLines(d[aiKey]) : d[aiKey];
                        filled.add(profileKey);
                    }
                }
                // Array fields
                if (d.pain_points?.length) { newProfile.pain_points = d.pain_points; filled.add('pain_points'); }
                if (d.competitors?.length) { newProfile.competitors = d.competitors; filled.add('competitors'); }

                setProfile(newProfile);
                setDirty(true);
                setAiFilledFields(filled);

                // Fill structured knowledge
                if (d.structured_knowledge) {
                    setKnowledge(d.structured_knowledge);
                }

                setShowAiDialog(false);
                setAiText('');
                setToast({ type: 'success', msg: `AI已填充 ${filled.size} 个字段，请检查后保存` });
            }
        } catch (e: any) {
            setToast({ type: 'error', msg: e.message || 'AI分析失败' });
        } finally {
            setAiLoading(false);
        }
    };

    // Load confirmation status
    const loadConfirmStatus = useCallback(async () => {
        try {
            const res = await authFetch(`/api/marketing-confirm/status/${brandId}`);
            const data = await res.json();
            setConfirmStatus(data);
        } catch {
            // ignore
        }
    }, [brandId]);

    useEffect(() => { loadConfirmStatus(); }, [loadConfirmStatus]);

    // Generate confirmation link
    // [WO_WHITELABEL_COPY_UX 项2 2026-08-05] 复制必须在手势同步栈发起(iOS/微信 webview):
    // handler 不再 async,生成链接的请求装进 copyAsyncText 的 getText。
    const handleGenerateLink = () => {
        if (dirty) {
            setToast({ type: 'error', msg: '请先保存修改后再生成确认链接' });
            return;
        }
        setGeneratingLink(true);
        void copyAsyncText(async () => {
            const res = await authFetch('/api/marketing-confirm/generate-link', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ brand_id: brandId }),
            });
            const data = await res.json();
            if (!res.ok) throw new Error(data.detail || '生成失败');
            setConfirmStatus({
                has_session: true,
                token: data.token,
                status: 'pending',
                url: data.url,
            });
            return `${window.location.origin}${data.url}`;
        }).then(({ ok, text, errorMessage }) => {
            if (ok) {
                setLinkCopied(true);
                setTimeout(() => setLinkCopied(false), 3000);
                setToast({ type: 'success', msg: '确认链接已生成并复制到剪贴板' });
            } else if (text) {
                setManualCopyText(text);
            } else {
                setToast({ type: 'error', msg: errorMessage || '生成失败' });
            }
        }).finally(() => setGeneratingLink(false));
    };

    const handleCopyLink = async () => {
        if (!confirmStatus?.url) return;
        const fullUrl = `${window.location.origin}${confirmStatus.url}`;
        const ok = await copyToClipboard(fullUrl);
        if (ok) {
            setLinkCopied(true);
            setTimeout(() => setLinkCopied(false), 3000);
            setToast({ type: 'success', msg: '链接已复制' });
        } else {
            // [WO_WHITELABEL_COPY_UX 项2] 失败给可选中链接,不许只报「复制失败」
            setManualCopyText(fullUrl);
        }
    };

    // [WO_WHITELABEL_COPY_UX 项2] 同 handleGenerateLink:复制走手势同步栈
    const handleResend = () => {
        if (dirty) {
            setToast({ type: 'error', msg: '请先保存修改后再重新发送' });
            return;
        }
        setResending(true);
        void copyAsyncText(async () => {
            const res = await authFetch(`/api/marketing-confirm/resend/${brandId}`, {
                method: 'POST',
            });
            const data = await res.json();
            if (!data.success) throw new Error('操作失败');
            loadConfirmStatus();
            return `${window.location.origin}${data.url}`;
        }).then(({ ok, text, errorMessage }) => {
            if (ok) {
                setToast({ type: 'success', msg: '资料已更新，链接已复制到剪贴板' });
            } else if (text) {
                setManualCopyText(text);
            } else {
                setToast({ type: 'error', msg: errorMessage || '操作失败' });
            }
        }).finally(() => setResending(false));
    };

    if (loading) {
        return (
            <div className="flex flex-col items-center justify-center py-16 gap-3">
                <Loader2 className="h-6 w-6 animate-spin text-brand" />
                <p className="text-sm text-muted-foreground">加载营销资料...</p>
            </div>
        );
    }

    const isAiFilled = (field: string) => aiFilledFields.has(field);

    return (
        <div className="space-y-6">
            {/* Toast */}
            {toast && (
                <div className={cn(
                    'fixed top-4 right-4 z-50 px-4 py-3 rounded-lg shadow-lg text-sm font-medium transition-all',
                    toast.type === 'success' ? 'bg-green-600 text-white' : 'bg-red-600 text-white'
                )}>
                    {toast.msg}
                </div>
            )}

            {/* AI一键填写 Button + Dialog */}
            {canEdit && (
                <Card className="border border-amber-200 bg-amber-50/50 rounded-xl">
                    <CardContent className="py-4">
                        <div className="flex items-center justify-between gap-4">
                            <div className="flex items-center gap-3">
                                <div className="h-10 w-10 rounded-xl bg-amber-100 flex items-center justify-center shrink-0">
                                    <Wand2 className="h-5 w-5 text-amber-600" />
                                </div>
                                <div>
                                    <p className="text-sm font-medium text-amber-900">AI顾问一键填写</p>
                                    <p className="text-xs text-amber-700">选择顾问，基于知识库自动生成结构化营销资料</p>
                                </div>
                            </div>
                            <Button
                                onClick={() => setShowAiDialog(true)}
                                className="bg-amber-600 hover:bg-amber-700 text-white"
                                size="sm"
                            >
                                <Sparkles className="h-4 w-4 mr-1.5" />
                                开始填写
                            </Button>
                        </div>
                    </CardContent>
                </Card>
            )}

            {/* AI Dialog */}
            {showAiDialog && (
                <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4" onClick={() => !aiLoading && setShowAiDialog(false)}>
                    <div className="bg-card rounded-2xl w-full max-w-lg p-6 space-y-4" onClick={e => e.stopPropagation()}>
                        <div className="flex items-center justify-between">
                            <h3 className="text-lg font-semibold text-foreground">AI顾问一键填写</h3>
                            <button onClick={() => !aiLoading && setShowAiDialog(false)} className="text-muted-foreground hover:text-foreground">
                                <X className="h-5 w-5" />
                            </button>
                        </div>
                        <p className="text-sm text-muted-foreground">
                            选择AI顾问，基于该客户的知识库文档自动提取行业、业务、卖点、案例等营销资料。
                        </p>

                        {/* 顾问选择 */}
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">选择顾问</label>
                            {advisors.length === 0 ? (
                                <div className="flex items-center gap-2 text-sm text-muted-foreground py-2">
                                    <Loader2 className="h-4 w-4 animate-spin" />
                                    加载顾问列表...
                                </div>
                            ) : (
                                <div className="grid grid-cols-1 gap-2 max-h-[200px] overflow-y-auto">
                                    {advisors.map(adv => (
                                        <button
                                            key={adv.id}
                                            onClick={() => setSelectedAdvisor(adv.id)}
                                            disabled={aiLoading}
                                            className={cn(
                                                'flex items-center gap-3 p-3 rounded-lg border text-left transition-colors',
                                                selectedAdvisor === adv.id
                                                    ? 'border-brand bg-brand/5 ring-1 ring-brand/30'
                                                    : 'border-border hover:border-muted-foreground/30 hover:bg-secondary/50'
                                            )}
                                        >
                                            <span className="text-2xl shrink-0">{adv.avatar || '🤖'}</span>
                                            <div className="flex-1 min-w-0">
                                                <div className="text-sm font-medium text-foreground">{adv.name}</div>
                                                <div className="text-xs text-muted-foreground truncate">{adv.specialty || adv.description}</div>
                                            </div>
                                            {selectedAdvisor === adv.id && (
                                                <div className="w-2 h-2 rounded-full bg-brand shrink-0" />
                                            )}
                                        </button>
                                    ))}
                                </div>
                            )}
                        </div>

                        {/* 补充文本（可选） */}
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                补充资料 <span className="text-muted-foreground font-normal">（可选）</span>
                            </label>
                            <textarea
                                value={aiText}
                                onChange={e => setAiText(e.target.value)}
                                placeholder="如知识库不够完整，可在此粘贴额外的公司介绍文本..."
                                rows={4}
                                className="w-full p-3 border border-border rounded-lg bg-background text-sm resize-y focus:outline-hidden focus:ring-2 focus:ring-ring"
                                disabled={aiLoading}
                            />
                        </div>

                        <div className="flex justify-end gap-3">
                            <Button variant="outline" onClick={() => setShowAiDialog(false)} disabled={aiLoading}>
                                取消
                            </Button>
                            <Button onClick={handleAiFill} disabled={aiLoading || !selectedAdvisor}>
                                {aiLoading ? (
                                    <>
                                        <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                        分析中...
                                    </>
                                ) : (
                                    <>
                                        <Wand2 className="h-4 w-4 mr-1.5" />
                                        开始分析
                                    </>
                                )}
                            </Button>
                        </div>
                    </div>
                </div>
            )}

            {/* ========== 客户确认链接（醒目位置） ========== */}
            {canEdit && existingId && (
                <div className="rounded-xl border-2 border-indigo-300 bg-linear-to-r from-indigo-50 to-purple-50 p-4 sm:p-5">
                    <div className="flex items-start gap-4">
                        <div className="h-12 w-12 rounded-xl bg-linear-to-br from-indigo-500 to-purple-600 flex items-center justify-center shrink-0 shadow-md">
                            <Link2 className="h-6 w-6 text-white" />
                        </div>
                        <div className="flex-1 min-w-0">
                            <div className="flex items-center justify-between gap-3 flex-wrap">
                                <div>
                                    <h3 className="text-base font-bold text-foreground">发送给客户确认</h3>
                                    <p className="text-sm text-muted-foreground mt-0.5">
                                        填写完资料后，生成链接发给客户审核确认，确认后即可开始内容创作
                                    </p>
                                </div>
                                <div className="flex items-center gap-2">
                                    {confirmStatus?.has_session && confirmStatus.status === 'confirmed' && (
                                        <Badge className="bg-emerald-100 text-emerald-700 border-emerald-200 text-sm px-3 py-1">
                                            <Check className="h-3.5 w-3.5 mr-1" />客户已确认
                                        </Badge>
                                    )}
                                    {confirmStatus?.has_session && confirmStatus.status === 'pending' && (
                                        <Badge className="bg-amber-100 text-amber-700 border-amber-200 text-sm px-3 py-1">
                                            待客户确认
                                        </Badge>
                                    )}
                                    {confirmStatus?.has_session && confirmStatus.status === 'feedback' && (
                                        <Badge className="bg-red-100 text-red-700 border-red-200 text-sm px-3 py-1">
                                            客户有修改意见
                                        </Badge>
                                    )}
                                    {/* feedback 状态：显示"资料已修改，重新发送" */}
                                    {confirmStatus?.has_session && confirmStatus.status === 'feedback' ? (
                                        <Button
                                            size="sm"
                                            onClick={handleResend}
                                            disabled={resending || dirty}
                                            className="text-sm bg-linear-to-r from-indigo-500 to-purple-600 hover:from-indigo-600 hover:to-purple-700 text-white shadow-md"
                                        >
                                            {resending ? (
                                                <><Loader2 className="h-4 w-4 mr-1.5 animate-spin" />发送中...</>
                                            ) : (
                                                <><Save className="h-4 w-4 mr-1.5" />资料已修改，重新发送</>
                                            )}
                                        </Button>
                                    ) : (
                                        <Button
                                            size="sm"
                                            onClick={handleGenerateLink}
                                            disabled={generatingLink || dirty}
                                            className={cn(
                                                "text-sm",
                                                !confirmStatus?.has_session && "bg-linear-to-r from-indigo-500 to-purple-600 hover:from-indigo-600 hover:to-purple-700 text-white shadow-md"
                                            )}
                                            variant={confirmStatus?.has_session ? 'outline' : 'default'}
                                        >
                                            {generatingLink ? (
                                                <><Loader2 className="h-4 w-4 mr-1.5 animate-spin" />生成中...</>
                                            ) : confirmStatus?.has_session ? (
                                                <><Link2 className="h-4 w-4 mr-1.5" />重新生成</>
                                            ) : (
                                                <><Link2 className="h-4 w-4 mr-1.5" />生成确认链接</>
                                            )}
                                        </Button>
                                    )}
                                </div>
                            </div>
                            {dirty && (
                                <p className="text-xs text-amber-600 mt-2 font-medium">* 请先保存修改后再生成链接</p>
                            )}

                            {/* Show existing link */}
                            {confirmStatus?.has_session && confirmStatus.url && (
                                <div className="mt-3 flex items-center gap-2 bg-card rounded-lg border border-border px-3 py-2.5 shadow-xs">
                                    <code className="text-sm text-indigo-600 flex-1 truncate font-medium">
                                        {window.location.origin}{confirmStatus.url}
                                    </code>
                                    <button
                                        onClick={handleCopyLink}
                                        className="text-indigo-500 hover:text-indigo-700 shrink-0 p-1 rounded hover:bg-indigo-50 transition-colors"
                                        title="复制链接"
                                    >
                                        {linkCopied ? <Check className="h-4 w-4 text-emerald-500" /> : <Copy className="h-4 w-4" />}
                                    </button>
                                    <a
                                        href={confirmStatus.url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="text-indigo-500 hover:text-indigo-700 shrink-0 p-1 rounded hover:bg-indigo-50 transition-colors"
                                        title="预览页面"
                                    >
                                        <ExternalLink className="h-4 w-4" />
                                    </a>
                                </div>
                            )}

                            {/* Customer notes */}
                            {confirmStatus?.customer_notes && (
                                <div className="mt-2 bg-card rounded-lg border border-border px-3 py-2.5 shadow-xs">
                                    <p className="text-xs text-amber-600 font-semibold mb-0.5">客户备注</p>
                                    <p className="text-sm text-foreground">{confirmStatus.customer_notes}</p>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}

            {/* ========== 基础信息 ========== */}
            <Card className="border border-border rounded-xl">
                <CardHeader className="pb-4">
                    <CardTitle className="text-base text-foreground">基础信息</CardTitle>
                    <CardDescription>客户公司的基本业务信息</CardDescription>
                </CardHeader>
                <CardContent className="space-y-4">
                    {/* Row 1: 名称 + 行业 */}
                    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                公司名称
                                {isAiFilled('name') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                            </label>
                            <Input
                                value={profile.name || ''}
                                onChange={e => updateField('name', e.target.value)}
                                placeholder="公司/品牌名称"
                                disabled={!canEdit}
                            />
                        </div>
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                行业大类
                                {isAiFilled('industry') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                            </label>
                            <Input
                                value={profile.industry || ''}
                                onChange={e => updateField('industry', e.target.value)}
                                placeholder="如：互联网/科技服务、餐饮/连锁加盟"
                                disabled={!canEdit}
                            />
                        </div>
                    </div>

                    {/* Row 2: 主营业务 */}
                    <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">
                            主营业务
                            {isAiFilled('business') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                        </label>
                        <Input
                            value={profile.business || ''}
                            onChange={e => updateField('business', e.target.value)}
                            placeholder="一句话描述主营业务"
                            disabled={!canEdit}
                        />
                    </div>

                    {/* Row 3: 目标客户 */}
                    <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">
                            目标客户
                            {isAiFilled('target_users') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                        </label>
                        <Input
                            value={profile.target_users || ''}
                            onChange={e => updateField('target_users', e.target.value)}
                            placeholder="目标客户画像描述"
                            disabled={!canEdit}
                        />
                    </div>

                    {/* Row 4: 客户痛点 (Tags) */}
                    <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">
                            客户痛点
                            {isAiFilled('pain_points') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                        </label>
                        <TagInput
                            value={profile.pain_points || []}
                            onChange={v => updateField('pain_points', v)}
                            placeholder="输入痛点后按回车添加"
                            disabled={!canEdit}
                        />
                    </div>

                    {/* Row 5: 竞争对手 (Tags) */}
                    <div>
                        <label className="text-sm font-medium text-foreground mb-1.5 block">
                            竞争对手
                            {isAiFilled('competitors') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                        </label>
                        <TagInput
                            value={profile.competitors || []}
                            onChange={v => updateField('competitors', v)}
                            placeholder="输入竞品名称后按回车添加"
                            disabled={!canEdit}
                        />
                    </div>
                </CardContent>
            </Card>

            {/* ========== 公司详情（可折叠）========== */}
            <Card className="border border-border rounded-xl">
                <CardHeader
                    className="pb-4 cursor-pointer select-none"
                    onClick={() => setDetailOpen(!detailOpen)}
                >
                    <div className="flex items-center justify-between">
                        <div>
                            <CardTitle className="text-base text-foreground">公司详情</CardTitle>
                            <CardDescription>公司简介、核心价值、卖点、案例、证言</CardDescription>
                        </div>
                        {detailOpen ? <ChevronUp className="h-5 w-5 text-muted-foreground" /> : <ChevronDown className="h-5 w-5 text-muted-foreground" />}
                    </div>
                </CardHeader>
                {detailOpen && (
                    <CardContent className="space-y-4 pt-0">
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                公司简介
                                {isAiFilled('company_intro') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                            </label>
                            <AutoTextarea
                                value={profile.company_intro || ''}
                                onChange={v => updateField('company_intro', v)}
                                placeholder="公司简介，突出核心优势和差异化..."
                                disabled={!canEdit}
                                rows={4}
                            />
                        </div>
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                核心价值主张
                                {isAiFilled('core_value') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                            </label>
                            <Input
                                value={profile.core_value || ''}
                                onChange={e => updateField('core_value', e.target.value)}
                                placeholder="一句话总结核心价值"
                                disabled={!canEdit}
                            />
                        </div>
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                核心卖点
                                {isAiFilled('selling_points') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                            </label>
                            <AutoTextarea
                                value={profile.selling_points || ''}
                                onChange={v => updateField('selling_points', v)}
                                placeholder="核心卖点，分点列出..."
                                disabled={!canEdit}
                            />
                        </div>
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                成功案例
                                {isAiFilled('success_cases') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                            </label>
                            <AutoTextarea
                                value={toCaseLines(profile.success_cases).join('\n')}
                                onChange={v => updateField('success_cases', v.split('\n'))}
                                placeholder="成功案例，包含客户名、效果数据..."
                                disabled={!canEdit}
                                rows={4}
                            />
                        </div>
                        <div>
                            <label className="text-sm font-medium text-foreground mb-1.5 block">
                                客户证言
                                {isAiFilled('testimonials') && <Badge variant="outline" className="ml-2 text-[10px] text-amber-600 border-amber-300">AI生成</Badge>}
                            </label>
                            <AutoTextarea
                                value={profile.testimonials || ''}
                                onChange={v => updateField('testimonials', v)}
                                placeholder="客户评价原话..."
                                disabled={!canEdit}
                            />
                        </div>
                    </CardContent>
                )}
            </Card>

            {/* ========== 业务知识库（可折叠）========== */}
            <Card className="border border-border rounded-xl">
                <CardHeader
                    className="pb-4 cursor-pointer select-none"
                    onClick={() => setKnowledgeOpen(!knowledgeOpen)}
                >
                    <div className="flex items-center justify-between">
                        <div>
                            <CardTitle className="text-base text-foreground">业务知识库</CardTitle>
                            <CardDescription>5维度结构化业务知识：卖什么、解决什么、客户是谁、为什么选、成功案例</CardDescription>
                        </div>
                        {knowledgeOpen ? <ChevronUp className="h-5 w-5 text-muted-foreground" /> : <ChevronDown className="h-5 w-5 text-muted-foreground" />}
                    </div>
                </CardHeader>
                {knowledgeOpen && (
                    <CardContent className="pt-0">
                        <BusinessKnowledgeCards
                            data={knowledge}
                            onUpdate={canEdit ? (newData) => { setKnowledge(newData); setDirty(true); } : undefined}
                            editable={canEdit}
                        />
                    </CardContent>
                )}
            </Card>

            {/* ========== Sticky Save Bar ========== */}
            {canEdit && (
                <div className="sticky bottom-0 z-30 bg-card/95 backdrop-blur-xs border-t border-border py-3 px-4 -mx-4 flex items-center justify-between">
                    <div className="text-sm text-muted-foreground">
                        {dirty ? (
                            <span className="text-amber-600 font-medium">有未保存的修改</span>
                        ) : existingId ? (
                            <span className="text-green-600">已保存</span>
                        ) : (
                            <span>尚未创建营销资料</span>
                        )}
                    </div>
                    <Button onClick={handleSave} disabled={saving || !dirty} className="min-w-[120px]">
                        {saving ? (
                            <>
                                <Loader2 className="h-4 w-4 mr-1.5 animate-spin" />
                                保存中...
                            </>
                        ) : (
                            <>
                                <Save className="h-4 w-4 mr-1.5" />
                                保存全部修改
                            </>
                        )}
                    </Button>
                </div>
            )}


            {/* Read-only notice */}
            {!canEdit && (
                <div className="text-center py-4 text-sm text-muted-foreground">
                    仅管理员可编辑客户营销资料
                </div>
            )}

            <ManualCopyDialog text={manualCopyText} onClose={() => setManualCopyText(null)} />
        </div>
    );
}
