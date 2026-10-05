import { useState, useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Label } from '@/components/ui/label';
import { Plus, Trash2, Save, Building2, Target, FileText, DollarSign, MessageSquare, Upload, Loader2 } from 'lucide-react';
import api from '@/lib/api';
import { toast } from 'sonner';

interface SellingPoint {
    point: string;
    evidence: string;
}

interface CaseStudy {
    client: string;
    industry?: string;
    result: string;
    timeline: string;
}

interface PricingTier {
    name: string;
    price: string;
    includes: string[];
}

interface Testimonial {
    name: string;
    title?: string;
    company: string;
    quote: string;
}

interface Credential {
    type: string;
    name: string;
    year: number;
}

interface ClientMaterials {
    company_intro: string;
    founding_year: number | null;
    team_size: string;
    service_area: string;
    core_selling_points: SellingPoint[];
    unique_value: string;
    methodology: string;
    case_studies: CaseStudy[];
    pricing_tiers: PricingTier[];
    testimonials: Testimonial[];
    credentials: Credential[];
}

interface Props {
    diagnosisId: number;
    onSaved?: () => void;
}

const defaultMaterials: ClientMaterials = {
    company_intro: '',
    founding_year: null,
    team_size: '',
    service_area: '',
    core_selling_points: [{ point: '', evidence: '' }],
    unique_value: '',
    methodology: '',
    case_studies: [{ client: '', result: '', timeline: '' }],
    pricing_tiers: [{ name: '', price: '', includes: [] }],
    testimonials: [{ name: '', company: '', quote: '' }],
    credentials: []
};

export default function ClientMaterialsEditor({ diagnosisId, onSaved }: Props) {
    const [materials, setMaterials] = useState<ClientMaterials>(defaultMaterials);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [activeTab, setActiveTab] = useState<'basic' | 'selling' | 'cases' | 'pricing' | 'testimonials'>('basic');
    const fileInputRef = useRef<HTMLInputElement>(null);

    // AI自动提取：上传文件
    const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        setUploading(true);
        try {
            const formData = new FormData();
            formData.append('file', file);

            const res = await api.post<{ success?: boolean; data?: ClientMaterials; error?: string; message?: string }>(
                `/api/materials/${diagnosisId}/extract`,
                formData,
                { headers: { 'Content-Type': 'multipart/form-data' } }
            );

            if (res.data.success && res.data.data) {
                setMaterials({ ...defaultMaterials, ...res.data.data });
                toast.success('AI已自动提取资料，请核对各项信息后保存');
            } else {
                toast.error('提取失败：' + (res.data.error || '未知错误'));
            }
        } catch (e: any) {
            toast.error('上传失败：' + (e.message || e));
        } finally {
            setUploading(false);
            if (fileInputRef.current) fileInputRef.current.value = '';
        }
    };


    // 加载现有资料
    useEffect(() => {
        const fetchMaterials = async () => {
            try {
                const res = await api.get<{ data: ClientMaterials | null }>(`/api/materials/${diagnosisId}`);
                if (res.data.data) {
                    setMaterials({ ...defaultMaterials, ...res.data.data });
                }
            } catch (e) {
                console.error('加载资料失败:', e);
            } finally {
                setLoading(false);
            }
        };
        fetchMaterials();
    }, [diagnosisId]);

    // 保存资料
    const handleSave = async () => {
        setSaving(true);
        try {
            const res = await api.post<{ success: boolean; error?: string }>(`/api/materials/${diagnosisId}`, materials);
            if (res.data.success) {
                toast.success('资料保存成功！');
                onSaved?.();
            } else {
                toast.error('保存失败：' + (res.data.error || '未知错误'));
            }
        } catch (e) {
            toast.error('保存失败：' + e);
        } finally {
            setSaving(false);
        }
    };

    // 添加/删除卖点
    const addSellingPoint = () => {
        setMaterials(m => ({
            ...m,
            core_selling_points: [...m.core_selling_points, { point: '', evidence: '' }]
        }));
    };
    const removeSellingPoint = (index: number) => {
        setMaterials(m => ({
            ...m,
            core_selling_points: m.core_selling_points.filter((_, i) => i !== index)
        }));
    };

    // 添加/删除案例
    const addCaseStudy = () => {
        setMaterials(m => ({
            ...m,
            case_studies: [...m.case_studies, { client: '', result: '', timeline: '' }]
        }));
    };
    const removeCaseStudy = (index: number) => {
        setMaterials(m => ({
            ...m,
            case_studies: m.case_studies.filter((_, i) => i !== index)
        }));
    };

    // 添加/删除报价
    const addPricingTier = () => {
        setMaterials(m => ({
            ...m,
            pricing_tiers: [...m.pricing_tiers, { name: '', price: '', includes: [] }]
        }));
    };
    const removePricingTier = (index: number) => {
        setMaterials(m => ({
            ...m,
            pricing_tiers: m.pricing_tiers.filter((_, i) => i !== index)
        }));
    };

    // 添加/删除证言
    const addTestimonial = () => {
        setMaterials(m => ({
            ...m,
            testimonials: [...m.testimonials, { name: '', company: '', quote: '' }]
        }));
    };
    const removeTestimonial = (index: number) => {
        setMaterials(m => ({
            ...m,
            testimonials: m.testimonials.filter((_, i) => i !== index)
        }));
    };

    if (loading) return <div className="text-center py-8">加载中...</div>;

    const tabs = [
        { id: 'basic' as const, label: '基础信息', icon: Building2 },
        { id: 'selling' as const, label: '核心卖点', icon: Target },
        { id: 'cases' as const, label: '成功案例', icon: FileText },
        { id: 'pricing' as const, label: '服务报价', icon: DollarSign },
        { id: 'testimonials' as const, label: '客户证言', icon: MessageSquare }
    ];

    return (
        <Card className="w-full">
            <CardHeader>
                <div className="flex items-center justify-between">
                    <div>
                        <CardTitle className="flex items-center gap-2">
                            <Building2 className="h-5 w-5" />
                            企业资料编辑
                        </CardTitle>
                        <CardDescription>
                            上传公司资料文件，AI将自动提取关键信息
                        </CardDescription>
                    </div>
                    <div className="flex gap-2">
                        <input
                            type="file"
                            ref={fileInputRef}
                            onChange={handleUpload}
                            accept=".pdf,.docx,.txt,.md"
                            className="hidden"
                        />
                        <Button
                            variant="outline"
                            onClick={() => fileInputRef.current?.click()}
                            disabled={uploading}
                        >
                            {uploading ? (
                                <>
                                    <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                    AI提取中...
                                </>
                            ) : (
                                <>
                                    <Upload className="mr-2 h-4 w-4" />
                                    上传文件自动填充
                                </>
                            )}
                        </Button>
                    </div>
                </div>
            </CardHeader>

            {/* Tab导航 */}
            <div className="flex border-b px-6">
                {tabs.map(tab => (
                    <button
                        key={tab.id}
                        onClick={() => setActiveTab(tab.id)}
                        className={`flex items-center gap-2 px-4 py-3 border-b-2 transition-colors ${activeTab === tab.id
                            ? 'border-primary text-primary'
                            : 'border-transparent text-muted-foreground hover:text-foreground'
                            }`}
                    >
                        <tab.icon className="h-4 w-4" />
                        {tab.label}
                    </button>
                ))}
            </div>

            <CardContent className="pt-6">
                {/* 基础信息 */}
                {activeTab === 'basic' && (
                    <div className="space-y-4">
                        <div>
                            <Label>公司简介</Label>
                            <Textarea
                                placeholder="介绍公司核心业务、发展历程..."
                                value={materials.company_intro}
                                onChange={e => setMaterials(m => ({ ...m, company_intro: e.target.value }))}
                                rows={4}
                            />
                        </div>
                        <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                            <div>
                                <Label>成立年份</Label>
                                <Input
                                    type="number"
                                    placeholder="如：2018"
                                    value={materials.founding_year || ''}
                                    onChange={e => setMaterials(m => ({ ...m, founding_year: parseInt(e.target.value) || null }))}
                                />
                            </div>
                            <div>
                                <Label>团队规模</Label>
                                <Input
                                    placeholder="如：50-100人"
                                    value={materials.team_size}
                                    onChange={e => setMaterials(m => ({ ...m, team_size: e.target.value }))}
                                />
                            </div>
                            <div>
                                <Label>服务范围</Label>
                                <Input
                                    placeholder="如：全国/深圳/海外"
                                    value={materials.service_area}
                                    onChange={e => setMaterials(m => ({ ...m, service_area: e.target.value }))}
                                />
                            </div>
                        </div>
                        <div>
                            <Label>一句话价值主张</Label>
                            <Input
                                placeholder="客户选择你的核心理由（一句话）"
                                value={materials.unique_value}
                                onChange={e => setMaterials(m => ({ ...m, unique_value: e.target.value }))}
                            />
                        </div>
                        <div>
                            <Label>服务方法论/流程</Label>
                            <Textarea
                                placeholder="描述服务流程、独特方法论..."
                                value={materials.methodology}
                                onChange={e => setMaterials(m => ({ ...m, methodology: e.target.value }))}
                                rows={3}
                            />
                        </div>
                    </div>
                )}

                {/* 核心卖点 */}
                {activeTab === 'selling' && (
                    <div className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                            列出你的核心差异化卖点（建议3-5个），每个卖点需有证据支撑
                        </p>
                        {materials.core_selling_points.map((sp, i) => (
                            <div key={i} className="flex gap-2 items-start p-3 border rounded-lg">
                                <div className="flex-1 space-y-2">
                                    <Input
                                        placeholder="卖点描述"
                                        value={sp.point}
                                        onChange={e => {
                                            const newPoints = [...materials.core_selling_points];
                                            newPoints[i] = { ...newPoints[i], point: e.target.value };
                                            setMaterials(m => ({ ...m, core_selling_points: newPoints }));
                                        }}
                                    />
                                    <Input
                                        placeholder="证据/数据支撑"
                                        value={sp.evidence}
                                        onChange={e => {
                                            const newPoints = [...materials.core_selling_points];
                                            newPoints[i] = { ...newPoints[i], evidence: e.target.value };
                                            setMaterials(m => ({ ...m, core_selling_points: newPoints }));
                                        }}
                                    />
                                </div>
                                <Button variant="ghost" size="icon" onClick={() => removeSellingPoint(i)}>
                                    <Trash2 className="h-4 w-4 text-destructive" />
                                </Button>
                            </div>
                        ))}
                        <Button variant="outline" onClick={addSellingPoint} className="w-full">
                            <Plus className="h-4 w-4 mr-2" /> 添加卖点
                        </Button>
                    </div>
                )}

                {/* 成功案例 */}
                {activeTab === 'cases' && (
                    <div className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                            真实成功案例（可脱敏），生成文章时会直接引用
                        </p>
                        {materials.case_studies.map((cs, i) => (
                            <div key={i} className="p-3 border rounded-lg space-y-2">
                                <div className="flex gap-2">
                                    <Input
                                        placeholder="客户名称（可脱敏：某XX企业）"
                                        value={cs.client}
                                        onChange={e => {
                                            const newCases = [...materials.case_studies];
                                            newCases[i] = { ...newCases[i], client: e.target.value };
                                            setMaterials(m => ({ ...m, case_studies: newCases }));
                                        }}
                                        className="flex-1"
                                    />
                                    <Button variant="ghost" size="icon" onClick={() => removeCaseStudy(i)}>
                                        <Trash2 className="h-4 w-4 text-destructive" />
                                    </Button>
                                </div>
                                <Input
                                    placeholder="效果数据（如：AI搜索可见度从0%提升至45%）"
                                    value={cs.result}
                                    onChange={e => {
                                        const newCases = [...materials.case_studies];
                                        newCases[i] = { ...newCases[i], result: e.target.value };
                                        setMaterials(m => ({ ...m, case_studies: newCases }));
                                    }}
                                />
                                <Input
                                    placeholder="服务周期（如：3个月）"
                                    value={cs.timeline}
                                    onChange={e => {
                                        const newCases = [...materials.case_studies];
                                        newCases[i] = { ...newCases[i], timeline: e.target.value };
                                        setMaterials(m => ({ ...m, case_studies: newCases }));
                                    }}
                                />
                            </div>
                        ))}
                        <Button variant="outline" onClick={addCaseStudy} className="w-full">
                            <Plus className="h-4 w-4 mr-2" /> 添加案例
                        </Button>
                    </div>
                )}

                {/* 服务报价 */}
                {activeTab === 'pricing' && (
                    <div className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                            真实服务套餐（价格区间），增加文章可信度
                        </p>
                        {materials.pricing_tiers.map((pt, i) => (
                            <div key={i} className="p-3 border rounded-lg space-y-2">
                                <div className="flex gap-2">
                                    <Input
                                        placeholder="套餐名称（如：基础版、进阶版）"
                                        value={pt.name}
                                        onChange={e => {
                                            const newTiers = [...materials.pricing_tiers];
                                            newTiers[i] = { ...newTiers[i], name: e.target.value };
                                            setMaterials(m => ({ ...m, pricing_tiers: newTiers }));
                                        }}
                                        className="flex-1"
                                    />
                                    <Input
                                        placeholder="价格（如：2-5万/月）"
                                        value={pt.price}
                                        onChange={e => {
                                            const newTiers = [...materials.pricing_tiers];
                                            newTiers[i] = { ...newTiers[i], price: e.target.value };
                                            setMaterials(m => ({ ...m, pricing_tiers: newTiers }));
                                        }}
                                        className="w-40"
                                    />
                                    <Button variant="ghost" size="icon" onClick={() => removePricingTier(i)}>
                                        <Trash2 className="h-4 w-4 text-destructive" />
                                    </Button>
                                </div>
                            </div>
                        ))}
                        <Button variant="outline" onClick={addPricingTier} className="w-full">
                            <Plus className="h-4 w-4 mr-2" /> 添加套餐
                        </Button>
                    </div>
                )}

                {/* 客户证言 */}
                {activeTab === 'testimonials' && (
                    <div className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                            真实客户评价，增强E-E-A-T可信度
                        </p>
                        {materials.testimonials.map((t, i) => (
                            <div key={i} className="p-3 border rounded-lg space-y-2">
                                <div className="flex gap-2">
                                    <Input
                                        placeholder="客户姓名/职位"
                                        value={t.name}
                                        onChange={e => {
                                            const newT = [...materials.testimonials];
                                            newT[i] = { ...newT[i], name: e.target.value };
                                            setMaterials(m => ({ ...m, testimonials: newT }));
                                        }}
                                        className="flex-1"
                                    />
                                    <Input
                                        placeholder="公司"
                                        value={t.company}
                                        onChange={e => {
                                            const newT = [...materials.testimonials];
                                            newT[i] = { ...newT[i], company: e.target.value };
                                            setMaterials(m => ({ ...m, testimonials: newT }));
                                        }}
                                        className="flex-1"
                                    />
                                    <Button variant="ghost" size="icon" onClick={() => removeTestimonial(i)}>
                                        <Trash2 className="h-4 w-4 text-destructive" />
                                    </Button>
                                </div>
                                <Textarea
                                    placeholder="客户评价原话..."
                                    value={t.quote}
                                    onChange={e => {
                                        const newT = [...materials.testimonials];
                                        newT[i] = { ...newT[i], quote: e.target.value };
                                        setMaterials(m => ({ ...m, testimonials: newT }));
                                    }}
                                    rows={2}
                                />
                            </div>
                        ))}
                        <Button variant="outline" onClick={addTestimonial} className="w-full">
                            <Plus className="h-4 w-4 mr-2" /> 添加证言
                        </Button>
                    </div>
                )}
            </CardContent>

            <CardFooter className="justify-end">
                <Button onClick={handleSave} disabled={saving}>
                    <Save className="h-4 w-4 mr-2" />
                    {saving ? '保存中...' : '保存资料'}
                </Button>
            </CardFooter>
        </Card>
    );
}
