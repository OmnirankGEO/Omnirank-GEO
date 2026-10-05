import { authFetch } from '@/lib/api';
import { toast } from 'sonner';
import { useState, useEffect } from "react";
import { useIsCEndContext } from '@/hooks/useIsCEndContext';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
    Loader2, Plus, BookOpen, Copy, ArrowLeft, Edit2, Trash2, History, Library
} from "lucide-react";
import ReactMarkdown from '@/components/SafeMarkdown';

// 范文类型
interface ReferenceArticle {
    id: number;
    title: string;
    content: string;
    source_url?: string;
    platform: string;
    industry: string;
    intent_type: string;
    analysis: any;
    use_count: number;
    success_rate: number;
    success_proof?: string;
    created_at: string;
}

// 平台选项
const PLATFORMS = ["知乎", "百家号", "搜狐号", "头条号", "腾讯内容平台", "其他"];

// 意图类型
const INTENT_TYPES = [
    { value: "ranking", label: "服务商排名" },
    { value: "pricing", label: "价格咨询" },
    { value: "guide", label: "选型指南" },
    { value: "company", label: "公司介绍" },
];

export function ReferenceLibrary() {
  const isCEnd = useIsCEndContext();
    const [view, setView] = useState<"list" | "add" | "detail" | "edit">("list");
    const [references, setReferences] = useState<ReferenceArticle[]>([]);
    const [loading, setLoading] = useState(true);
    const [selectedRef, setSelectedRef] = useState<ReferenceArticle | null>(null);

    // 编辑和删除状态
    const [editing, setEditing] = useState(false);
    const [deleting, setDeleting] = useState(false);
    const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
    const [imitationRecords, setImitationRecords] = useState<any[]>([]);
    const [showRecords, setShowRecords] = useState(false);
    const [recordArticles, setRecordArticles] = useState<any[]>([]);
    const [showArticles, setShowArticles] = useState(false);
    const [_selectedRecordId, setSelectedRecordId] = useState<number | null>(null);

    // 文章预览弹窗状态
    const [previewArticle, setPreviewArticle] = useState<any>(null);
    const [showPreview, setShowPreview] = useState(false);
    const [previewEditing, setPreviewEditing] = useState(false);
    const [previewEditContent, setPreviewEditContent] = useState("");
    const [previewSaving, setPreviewSaving] = useState(false);

    // 仿写弹窗状态
    const [showImitateModal, setShowImitateModal] = useState(false);
    const [imitating, setImitating] = useState(false);
    const [imitateResult, setImitateResult] = useState<any>(null);

    // 添加表单
    const [formData, setFormData] = useState({
        title: "",
        content: "",
        source_url: "",
        platform: "",
        industry: "",
        intent_type: "",
        success_proof: ""
    });
    const [submitting, setSubmitting] = useState(false);

    // 筛选
    const [searchKeyword, setSearchKeyword] = useState("");
    const [filterTimeRange, setFilterTimeRange] = useState("all");
    const [filterIntent, setFilterIntent] = useState("all");

    // 弹窗打开时禁止背景滚动
    useEffect(() => {
        if (showPreview || showArticles || showRecords || showImitateModal) {
            document.body.style.overflow = 'hidden';
        } else {
            document.body.style.overflow = 'auto';
        }
        return () => {
            document.body.style.overflow = 'auto';
        };
    }, [showPreview, showArticles, showRecords, showImitateModal]);

    // 加载范文列表
    const loadReferences = async () => {
        try {
            let url = "/api/references";
            const params = new URLSearchParams();
            if (searchKeyword) params.append("search", searchKeyword);
            if (filterTimeRange && filterTimeRange !== "all") params.append("time_range", filterTimeRange);
            if (filterIntent && filterIntent !== "all") params.append("intent_type", filterIntent);
            if (params.toString()) url += `?${params.toString()}`;

            const res = await authFetch(url);
            const data = await res.json();
            setReferences(data.references || []);
        } catch (e) {
            console.error("加载范文失败:", e);
            setReferences([]);
        } finally {
            setLoading(false);
        }
    };

    // 添加范文
    const handleSubmit = async () => {
        if (!formData.title || !formData.content) return;
        setSubmitting(true);
        try {
            const res = await authFetch("/api/references", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(formData)
            });
            const data = await res.json();
            if (data.success) {
                setFormData({
                    title: "", content: "", source_url: "",
                    platform: "", industry: "", intent_type: "", success_proof: ""
                });
                setView("list");
                loadReferences();
            }
        } catch (e) {
            console.error("添加范文失败:", e);
        } finally {
            setSubmitting(false);
        }
    };

    // 查看详情
    const viewDetail = async (ref: ReferenceArticle) => {
        try {
            const res = await authFetch(`/api/references/${ref.id}`);
            const data = await res.json();
            setSelectedRef(data);
            setView("detail");
        } catch (e) {
            console.error("获取详情失败:", e);
        }
    };

    useEffect(() => {
        loadReferences();
    }, [searchKeyword, filterTimeRange, filterIntent]);

    // 编辑范文
    const handleEdit = () => {
        if (!selectedRef) return;
        // 将选中范文数据填充到表单
        setFormData({
            title: selectedRef.title || "",
            content: selectedRef.content || "",
            source_url: selectedRef.source_url || "",
            platform: selectedRef.platform || "",
            industry: selectedRef.industry || "",
            intent_type: selectedRef.intent_type || "",
            success_proof: selectedRef.success_proof || ""
        });
        setView("edit");
    };

    // 保存编辑
    const handleSaveEdit = async () => {
        if (!selectedRef) return;
        setEditing(true);
        try {
            const res = await authFetch(`/api/references/${selectedRef.id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(formData)
            });
            const data = await res.json();
            if (data.success) {
                // 重新加载详情
                const detailRes = await authFetch(`/api/references/${selectedRef.id}`);
                const detailData = await detailRes.json();
                setSelectedRef(detailData);
                setView("detail");
                loadReferences();
            }
        } catch (e) {
            console.error("编辑范文失败:", e);
            toast.error("编辑失败");
        } finally {
            setEditing(false);
        }
    };

    // 删除范文
    const handleDelete = async () => {
        if (!selectedRef) return;
        setDeleting(true);
        try {
            const res = await authFetch(`/api/references/${selectedRef.id}`, {
                method: "DELETE"
            });
            const data = await res.json();
            if (data.success) {
                setShowDeleteConfirm(false);
                setView("list");
                loadReferences();
            }
        } catch (e) {
            console.error("删除范文失败:", e);
            toast.error("删除失败");
        } finally {
            setDeleting(false);
        }
    };

    // 获取仿写记录
    const loadImitationRecords = async () => {
        if (!selectedRef) return;
        try {
            const res = await authFetch(`/api/references/${selectedRef.id}/records`);
            const data = await res.json();
            if (data.success) {
                setImitationRecords(data.records || []);
                setShowRecords(true);
            }
        } catch (e) {
            console.error("获取仿写记录失败:", e);
        }
    };

    // 保存预览文章编辑
    const savePreviewArticle = async () => {
        if (!previewArticle) return;
        setPreviewSaving(true);
        try {
            const res = await authFetch(`/api/imitated-articles/${previewArticle.id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ content: previewEditContent })
            });
            const data = await res.json();
            if (data.success) {
                // 更新预览的文章内容
                setPreviewArticle({ ...previewArticle, content: previewEditContent });
                // 更新列表中的文章
                setRecordArticles(recordArticles.map(a =>
                    a.id === previewArticle.id ? { ...a, content: previewEditContent } : a
                ));
                setPreviewEditing(false);
            }
        } catch (e) {
            console.error("保存失败:", e);
            toast.error("保存失败");
        } finally {
            setPreviewSaving(false);
        }
    };

    // 一键下载所有文章
    const downloadAllArticles = () => {
        if (recordArticles.length === 0) return;

        const content = recordArticles.map((article, i) =>
            `# ${i + 1}. ${article.title || "未命名"}\n\n关键词: ${article.keyword || "-"}\n\n${article.content || "无内容"}\n\n---\n`
        ).join("\n");

        const blob = new Blob([content], { type: "text/markdown;charset=utf-8" });
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = `仿写文章_${new Date().toISOString().slice(0, 10)}.md`;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
    };

    // 仿写功能 - 添加项目选择
    const [showProjectSelect, setShowProjectSelect] = useState(false);
    const [availableProjects, setAvailableProjects] = useState<any[]>([]);
    const [selectedProjectId, setSelectedProjectId] = useState<number | null>(null);

    // 点击仿写按钮 - 先显示项目选择弹窗
    const handleImitateClick = async () => {
        if (!selectedRef) return;
        // 获取可用项目列表
        try {
            const projRes = await authFetch("/api/writing/projects");
            const projData = await projRes.json();
            const projects = projData.projects || [];

            if (projects.length === 0) {
                toast("暂无可用项目，请先在报价中心创建并确认报价单");
                return;
            }

            setAvailableProjects(projects);
            setSelectedProjectId(null);
            setShowProjectSelect(true);
        } catch (e) {
            console.error("获取项目列表失败:", e);
            toast.error("获取项目列表失败");
        }
    };

    // 确认选择项目后执行仿写
    const handleImitate = async () => {
        if (!selectedRef || !selectedProjectId) return;
        setShowProjectSelect(false);
        setImitating(true);
        try {
            const res = await authFetch(`/api/references/${selectedRef.id}/imitate`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    quote_id: selectedProjectId,
                    count: 5
                })
            });
            const data = await res.json();

            if (data.success) {
                setImitateResult(data);
                setShowImitateModal(true);
            } else {
                toast.error("仿写失败: " + (data.detail || "未知错误"));
            }
        } catch (e) {
            console.error("仿写失败:", e);
            toast.error("仿写请求失败");
        } finally {
            setImitating(false);
        }
    };

    // 渲染列表
    const renderList = () => (
        <div className="space-y-6">
            {/* 筛选栏 */}
            <div className="flex items-center gap-3 flex-wrap">
                {/* 综合搜索框（标题/行业/平台模糊匹配） */}
                <Input
                    value={searchKeyword}
                    onChange={(e) => setSearchKeyword(e.target.value)}
                    placeholder="🔍 搜索标题、行业、平台..."
                    className="w-64"
                />
                {/* 时间筛选 */}
                <Select value={filterTimeRange} onValueChange={setFilterTimeRange}>
                    <SelectTrigger className="w-32">
                        <SelectValue placeholder="全部时间" />
                    </SelectTrigger>
                    <SelectContent>
                        <SelectItem value="all">全部时间</SelectItem>
                        <SelectItem value="7d">近7天</SelectItem>
                        <SelectItem value="30d">近30天</SelectItem>
                        <SelectItem value="90d">近90天</SelectItem>
                        <SelectItem value="180d">近半年</SelectItem>
                        <SelectItem value="365d">一年内</SelectItem>
                    </SelectContent>
                </Select>
                {/* 意图筛选 */}
                <Select value={filterIntent} onValueChange={setFilterIntent}>
                    <SelectTrigger className="w-32">
                        <SelectValue placeholder="全部意图" />
                    </SelectTrigger>
                    <SelectContent>
                        <SelectItem value="all">全部意图</SelectItem>
                        {INTENT_TYPES.map(t => (
                            <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>
                        ))}
                    </SelectContent>
                </Select>
                <div className="flex-1" />
                <Button onClick={() => setView("add")}>
                    <Plus className="h-4 w-4 mr-1" />
                    添加范文
                </Button>
            </div>

            {/* 范文列表 */}
            {loading ? (
                <div className="flex justify-center py-12">
                    <Loader2 className="h-8 w-8 animate-spin text-brand" />
                </div>
            ) : references.length === 0 ? (
                <Card className="border border-border rounded-xl">
                    <CardContent className="py-12 text-center text-muted-foreground">
                        <BookOpen className="h-12 w-12 mx-auto mb-4 opacity-30" />
                        <p>暂无范文,点击"添加范文"开始积累</p>
                    </CardContent>
                </Card>
            ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                    {references.map(ref => (
                        <Card key={ref.id} className="border border-border rounded-xl hover:border-brand/40 transition-colors cursor-pointer" onClick={() => viewDetail(ref)}>
                            <CardHeader className="pb-2">
                                <div className="flex items-start justify-between">
                                    <CardTitle className="text-base line-clamp-2">
                                        {ref.title}
                                    </CardTitle>
                                    <Badge variant="outline">{ref.platform || "未知"}</Badge>
                                </div>
                                <CardDescription className="flex gap-2 mt-2">
                                    {ref.industry && <Badge variant="secondary">{ref.industry}</Badge>}
                                    {ref.intent_type && <Badge variant="secondary">{INTENT_TYPES.find(t => t.value === ref.intent_type)?.label || ref.intent_type}</Badge>}
                                </CardDescription>
                            </CardHeader>
                            <CardContent>
                                <div className="flex justify-between text-sm text-muted-foreground">
                                    <span>使用 {ref.use_count} 次</span>
                                    <span>{ref.created_at?.slice(0, 10)}</span>
                                </div>
                            </CardContent>
                        </Card>
                    ))}
                </div>
            )}
        </div>
    );

    // 渲染添加表单
    const renderAddForm = () => (
        <div className="space-y-6">
            <div className="flex items-center gap-4">
                <Button variant="ghost" size="sm" onClick={() => setView("list")}>
                    <ArrowLeft className="h-4 w-4 mr-1" />
                    返回列表
                </Button>
                <h2 className="text-xl font-bold">添加范文</h2>
            </div>

            <Card className="border border-border rounded-xl">
                <CardContent className="space-y-4 pt-6">
                    <div className="space-y-2">
                        <Label>标题 *</Label>
                        <Input
                            value={formData.title}
                            onChange={(e) => setFormData({ ...formData, title: e.target.value })}
                            placeholder="范文标题"
                        />
                    </div>

                    <div className="space-y-2">
                        <Label>内容 *</Label>
                        <Textarea
                            value={formData.content}
                            onChange={(e) => setFormData({ ...formData, content: e.target.value })}
                            placeholder="粘贴范文内容(支持Markdown)"
                            rows={12}
                        />
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                            <Label>来源URL</Label>
                            <Input
                                value={formData.source_url}
                                onChange={(e) => setFormData({ ...formData, source_url: e.target.value })}
                                placeholder="https://..."
                            />
                        </div>
                        <div className="space-y-2">
                            <Label>平台</Label>
                            <Select value={formData.platform} onValueChange={(v) => setFormData({ ...formData, platform: v })}>
                                <SelectTrigger>
                                    <SelectValue placeholder="选择平台" />
                                </SelectTrigger>
                                <SelectContent>
                                    {PLATFORMS.map(p => (
                                        <SelectItem key={p} value={p}>{p}</SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>

                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                            <Label>行业</Label>
                            <Input
                                value={formData.industry}
                                onChange={(e) => setFormData({ ...formData, industry: e.target.value })}
                                placeholder="如: 出海服务"
                            />
                        </div>
                        <div className="space-y-2">
                            <Label>意图类型</Label>
                            <Select value={formData.intent_type} onValueChange={(v) => setFormData({ ...formData, intent_type: v })}>
                                <SelectTrigger>
                                    <SelectValue placeholder="选择意图" />
                                </SelectTrigger>
                                <SelectContent>
                                    {INTENT_TYPES.map(t => (
                                        <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>

                    <div className="space-y-2">
                        <Label>成功证据(可选)</Label>
                        <Textarea
                            value={formData.success_proof}
                            onChange={(e) => setFormData({ ...formData, success_proof: e.target.value })}
                            placeholder="描述此文章被AI引用的证据"
                            rows={3}
                        />
                    </div>

                    <div className="flex justify-end gap-2 pt-4">
                        <Button variant="outline" onClick={() => setView("list")}>取消</Button>
                        <Button onClick={handleSubmit} disabled={submitting || !formData.title || !formData.content}>
                            {submitting && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
                            保存并分析
                        </Button>
                    </div>
                </CardContent>
            </Card>
        </div>
    );

    // 渲染编辑表单
    const renderEditForm = () => (
        <div className="space-y-6">
            <div className="flex items-center gap-4">
                <Button variant="ghost" size="sm" onClick={() => setView("detail")}>
                    <ArrowLeft className="h-4 w-4 mr-1" />
                    返回详情
                </Button>
                <h2 className="text-xl font-bold">编辑范文</h2>
            </div>

            <Card className="border border-border rounded-xl">
                <CardContent className="pt-6 space-y-4">
                    <div className="grid grid-cols-2 gap-4">
                        <div className="space-y-2">
                            <Label>标题 *</Label>
                            <Input
                                value={formData.title}
                                onChange={(e) => setFormData({ ...formData, title: e.target.value })}
                            />
                        </div>
                        <div className="space-y-2">
                            <Label>来源URL</Label>
                            <Input
                                value={formData.source_url}
                                onChange={(e) => setFormData({ ...formData, source_url: e.target.value })}
                            />
                        </div>
                    </div>

                    <div className="grid grid-cols-3 gap-4">
                        <div className="space-y-2">
                            <Label>平台</Label>
                            <Select value={formData.platform} onValueChange={(v) => setFormData({ ...formData, platform: v })}>
                                <SelectTrigger><SelectValue placeholder="选择平台" /></SelectTrigger>
                                <SelectContent>
                                    {PLATFORMS.map(p => <SelectItem key={p} value={p}>{p}</SelectItem>)}
                                </SelectContent>
                            </Select>
                        </div>
                        <div className="space-y-2">
                            <Label>行业</Label>
                            <Input
                                value={formData.industry}
                                onChange={(e) => setFormData({ ...formData, industry: e.target.value })}
                            />
                        </div>
                        <div className="space-y-2">
                            <Label>意图类型</Label>
                            <Select value={formData.intent_type} onValueChange={(v) => setFormData({ ...formData, intent_type: v })}>
                                <SelectTrigger><SelectValue placeholder="选择类型" /></SelectTrigger>
                                <SelectContent>
                                    {INTENT_TYPES.map(t => <SelectItem key={t.value} value={t.value}>{t.label}</SelectItem>)}
                                </SelectContent>
                            </Select>
                        </div>
                    </div>

                    <div className="space-y-2">
                        <Label>范文内容 *</Label>
                        <Textarea
                            rows={15}
                            value={formData.content}
                            onChange={(e) => setFormData({ ...formData, content: e.target.value })}
                            placeholder="粘贴范文内容（支持Markdown格式）"
                        />
                    </div>

                    <div className="space-y-2">
                        <Label>成功证明</Label>
                        <Textarea
                            rows={2}
                            value={formData.success_proof}
                            onChange={(e) => setFormData({ ...formData, success_proof: e.target.value })}
                            placeholder="例如：该文章在DeepSeek搜索'xxx'时排名第一"
                        />
                    </div>

                    <div className="flex justify-end gap-2 pt-4">
                        <Button variant="outline" onClick={() => setView("detail")}>取消</Button>
                        <Button onClick={handleSaveEdit} disabled={editing || !formData.title || !formData.content}>
                            {editing && <Loader2 className="h-4 w-4 animate-spin mr-1" />}
                            保存修改
                        </Button>
                    </div>
                </CardContent>
            </Card>
        </div>
    );

    // 渲染详情
    const renderDetail = () => {
        if (!selectedRef) return null;

        return (
            <div className="space-y-6">
                <div className="flex items-center gap-4">
                    <Button variant="ghost" size="sm" onClick={() => setView("list")}>
                        <ArrowLeft className="h-4 w-4 mr-1" />
                        返回列表
                    </Button>
                    <h2 className="text-xl font-bold flex-1">{selectedRef.title}</h2>
                    <Button variant="outline" size="sm" onClick={loadImitationRecords}>
                        <History className="h-4 w-4 mr-1" />
                        仿写记录
                    </Button>
                    <Button variant="outline" size="sm" onClick={handleEdit}>
                        <Edit2 className="h-4 w-4 mr-1" />
                        编辑
                    </Button>
                    <Button variant="outline" size="sm" className="text-red-500 hover:text-red-600" onClick={() => setShowDeleteConfirm(true)}>
                        <Trash2 className="h-4 w-4 mr-1" />
                        删除
                    </Button>
                    <Button onClick={handleImitateClick} disabled={imitating}>
                        {imitating ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Copy className="h-4 w-4 mr-1" />}
                        {imitating ? "生成中..." : "使用此范文仿写"}
                    </Button>
                </div>

                <div className="grid grid-cols-3 gap-6">
                    <div className="col-span-2">
                        <Card className="border border-border rounded-xl">
                            <CardHeader>
                                <CardTitle>范文内容</CardTitle>
                            </CardHeader>
                            <CardContent className="prose prose-sm dark:prose-invert max-w-none">
                                <ReactMarkdown>{selectedRef.content}</ReactMarkdown>
                            </CardContent>
                        </Card>
                    </div>

                    <div className="space-y-4">
                        <Card className="border border-border rounded-xl">
                            <CardHeader>
                                <CardTitle className="text-base">结构分析</CardTitle>
                            </CardHeader>
                            <CardContent className="text-sm space-y-3">
                                {selectedRef.analysis?.title_pattern && (
                                    <div>
                                        <p className="font-medium text-foreground">标题模式</p>
                                        <p className="text-muted-foreground">{selectedRef.analysis.title_pattern.template}</p>
                                    </div>
                                )}
                                {selectedRef.analysis?.structure && (
                                    <div>
                                        <p className="font-medium text-foreground">内容结构</p>
                                        <ul className="text-muted-foreground list-disc list-inside">
                                            {selectedRef.analysis.structure.map((s: any, i: number) => (
                                                <li key={i}>{s.section} ({s.word_ratio}%)</li>
                                            ))}
                                        </ul>
                                    </div>
                                )}
                                {selectedRef.analysis?.tone && (
                                    <div>
                                        <p className="font-medium text-foreground">语调风格</p>
                                        <p className="text-muted-foreground">{selectedRef.analysis.tone}</p>
                                    </div>
                                )}
                            </CardContent>
                        </Card>

                        <Card className="border border-border rounded-xl">
                            <CardHeader>
                                <CardTitle className="text-base">统计信息</CardTitle>
                            </CardHeader>
                            <CardContent className="text-sm space-y-2">
                                <div className="flex justify-between">
                                    <span className="text-muted-foreground">使用次数</span>
                                    <span className="font-medium">{selectedRef.use_count}</span>
                                </div>
                                <div className="flex justify-between">
                                    <span className="text-muted-foreground">平台</span>
                                    <span>{selectedRef.platform || "-"}</span>
                                </div>
                                <div className="flex justify-between">
                                    <span className="text-muted-foreground">行业</span>
                                    <span>{selectedRef.industry || "-"}</span>
                                </div>
                            </CardContent>
                        </Card>
                    </div>
                </div>
            </div>
        );
    };

    return (
        <div className="space-y-6 p-4 sm:p-6">
            <div className="flex items-center gap-4">
                <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                    <Library className="h-5 w-5 text-brand" />
                </div>
                <div>
                    <h1 className="text-2xl font-bold text-foreground">范文库</h1>
                    <p className="text-muted-foreground">收录被AI成功引用的优秀文章,复用成功模式</p>
                </div>
            </div>

            {view === "list" && renderList()}
            {view === "add" && renderAddForm()}
            {view === "detail" && renderDetail()}
            {view === "edit" && renderEditForm()}

            {/* 删除确认弹窗 */}
            {showDeleteConfirm && (
                <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
                    <Card className="w-[400px] bg-card rounded-xl border border-border">
                        <CardHeader>
                            <CardTitle>确认删除</CardTitle>
                            <CardDescription>确定要删除这篇范文吗？此操作不可撤销。</CardDescription>
                        </CardHeader>
                        <CardContent>
                            <div className="flex justify-end gap-2">
                                <Button variant="outline" onClick={() => setShowDeleteConfirm(false)}>
                                    取消
                                </Button>
                                <Button variant="destructive" onClick={handleDelete} disabled={deleting}>
                                    {deleting ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                                    {deleting ? "删除中..." : "确认删除"}
                                </Button>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 仿写记录弹窗 */}
            {showRecords && (
                <div
                    className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 overflow-hidden"
                    onMouseDown={(e) => {
                        if (e.target === e.currentTarget) {
                            setShowRecords(false);
                        }
                    }}
                >
                    <Card className="w-[700px] max-h-[80vh] overflow-auto bg-card rounded-xl border border-border" onMouseDown={(e) => e.stopPropagation()}>
                        <CardHeader>
                            <CardTitle>仿写记录</CardTitle>
                            <CardDescription>此范文已被使用 {imitationRecords.length} 次</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            {imitationRecords.length === 0 ? (
                                <p className="text-center text-muted-foreground py-4">暂无仿写记录</p>
                            ) : (
                                imitationRecords.map((record: any, i: number) => (
                                    <div key={i} className="p-3 bg-muted rounded-lg flex justify-between items-center">
                                        <div>
                                            <p className="font-medium">{record.brand_name || "未知客户"}</p>
                                            <p className="text-sm text-muted-foreground">
                                                {record.industry || "-"} · 生成 {record.generated_count} 篇
                                            </p>
                                        </div>
                                        <div className="flex items-center gap-2">
                                            <span className="text-xs text-muted-foreground">
                                                {record.created_at?.slice(0, 10)}
                                            </span>
                                            <Button size="sm" variant="outline" onClick={async () => {
                                                setSelectedRecordId(record.id);
                                                try {
                                                    const res = await authFetch(`/api/imitation-records/${record.id}/articles`);
                                                    const data = await res.json();
                                                    if (data.success) {
                                                        setRecordArticles(data.articles || []);
                                                        setShowArticles(true);
                                                    }
                                                } catch (e) {
                                                    console.error("获取文章失败:", e);
                                                }
                                            }}>
                                                查看文章
                                            </Button>
                                        </div>
                                    </div>
                                ))
                            )}
                            <div className="pt-4 border-t">
                                <Button variant="outline" className="w-full" onClick={() => setShowRecords(false)}>
                                    关闭
                                </Button>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 仿写文章列表弹窗 */}
            {showArticles && (
                <div
                    className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 overflow-hidden"
                    onMouseDown={(e) => {
                        if (e.target === e.currentTarget) {
                            setShowArticles(false);
                        }
                    }}
                >
                    <Card className="w-[800px] max-h-[85vh] overflow-auto bg-card rounded-xl border border-border" onMouseDown={(e) => e.stopPropagation()}>
                        <CardHeader>
                            <CardTitle>仿写文章列表</CardTitle>
                            <CardDescription>共 {recordArticles.length} 篇文章</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            {recordArticles.length === 0 ? (
                                <p className="text-center text-muted-foreground py-4">暂无文章</p>
                            ) : (
                                recordArticles.map((article: any, i: number) => (
                                    <div key={i} className="border rounded-lg p-4">
                                        <div className="flex justify-between items-start mb-2">
                                            <div>
                                                <h4 className="font-medium">{article.title || "未命名"}</h4>
                                                <p className="text-xs text-muted-foreground">
                                                    关键词: {article.keyword || "-"} · {article.word_count || 0}字
                                                </p>
                                            </div>
                                            <div className="flex items-center gap-2">
                                                <Badge variant={article.status === 'approved' ? 'default' : 'outline'}>
                                                    {article.status === 'approved' ? '已审核' : '草稿'}
                                                </Badge>
                                                <Button size="sm" variant="outline" onClick={() => {
                                                    setPreviewArticle(article);
                                                    setShowPreview(true);
                                                }}>
                                                    查看详情
                                                </Button>
                                            </div>
                                        </div>
                                        <div className="text-sm text-muted-foreground line-clamp-3 prose prose-sm dark:prose-invert max-w-none">
                                            <ReactMarkdown>{article.content?.slice(0, 300) + "..." || "无内容"}</ReactMarkdown>
                                        </div>
                                    </div>
                                ))
                            )}
                            <div className="pt-4 border-t flex justify-between gap-2">
                                {!isCEnd && (
                                    <Button variant="default" onClick={downloadAllArticles} disabled={recordArticles.length === 0}>
                                        📥 一键下载所有文章
                                    </Button>
                                )}
                                <Button variant="outline" onClick={() => setShowArticles(false)}>
                                    关闭
                                </Button>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 文章预览弹窗 */}
            {showPreview && previewArticle && (
                <div
                    className="fixed inset-0 bg-black/50 flex items-center justify-center z-60 overflow-hidden"
                    onMouseDown={(e) => {
                        // 点击外部关闭 - 使用onMouseDown确保在点击开始时就检测
                        if (e.target === e.currentTarget) {
                            setShowPreview(false);
                            setPreviewEditing(false);
                        }
                    }}
                    onWheel={(e) => e.stopPropagation()}
                >
                    <Card
                        className="w-[900px] max-h-[90vh] overflow-auto bg-card rounded-xl border border-border"
                        onMouseDown={(e) => e.stopPropagation()}
                    >
                        <CardHeader className="sticky top-0 bg-card z-10 border-b border-border">
                            <div className="flex justify-between items-start">
                                <div className="flex-1">
                                    <CardTitle className="text-lg">{previewArticle.title || "未命名"}</CardTitle>
                                    <CardDescription className="mt-1">
                                        关键词: {previewArticle.keyword || "-"} · {previewArticle.word_count || 0}字
                                        <Badge variant={previewArticle.status === 'approved' ? 'default' : 'outline'} className="ml-2">
                                            {previewArticle.status === 'approved' ? '已审核' : '草稿'}
                                        </Badge>
                                    </CardDescription>
                                </div>
                                <div className="flex items-center gap-2">
                                    {previewEditing ? (
                                        <>
                                            <Button size="sm" onClick={savePreviewArticle} disabled={previewSaving}>
                                                {previewSaving ? "保存中..." : "保存"}
                                            </Button>
                                            <Button size="sm" variant="outline" onClick={() => {
                                                setPreviewEditing(false);
                                                setPreviewEditContent(previewArticle.content || "");
                                            }}>
                                                取消
                                            </Button>
                                        </>
                                    ) : (
                                        <Button size="sm" variant="outline" onClick={() => {
                                            setPreviewEditContent(previewArticle.content || "");
                                            setPreviewEditing(true);
                                        }}>
                                            ✏️ 编辑
                                        </Button>
                                    )}
                                    <Button variant="ghost" size="sm" onClick={() => {
                                        setShowPreview(false);
                                        setPreviewEditing(false);
                                    }}>
                                        ✕
                                    </Button>
                                </div>
                            </div>
                        </CardHeader>
                        <CardContent className="pt-4">
                            {previewEditing ? (
                                <Textarea
                                    value={previewEditContent}
                                    onChange={(e) => setPreviewEditContent(e.target.value)}
                                    rows={20}
                                    className="font-mono text-sm"
                                    placeholder="输入文章内容..."
                                />
                            ) : (
                                <div className="prose prose-sm dark:prose-invert max-w-none">
                                    <ReactMarkdown>{previewArticle.content || "无内容"}</ReactMarkdown>
                                </div>
                            )}
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 项目选择弹窗 - 仿写前必须选择客户 */}
            {showProjectSelect && (
                <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
                    <Card className="w-[500px] max-h-[80vh] overflow-auto bg-card rounded-xl border border-border">
                        <CardHeader>
                            <CardTitle>选择客户项目</CardTitle>
                            <CardDescription>请选择要为哪个客户仿写文章</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            {availableProjects.map((proj: any) => (
                                <div
                                    key={proj.id}
                                    className={`p-3 border rounded-lg cursor-pointer transition-colors ${selectedProjectId === proj.id
                                        ? 'border-brand bg-brand/5'
                                        : 'border-border hover:border-brand/40'
                                        }`}
                                    onClick={() => setSelectedProjectId(proj.id)}
                                >
                                    <p className="font-medium">{proj.brand_name}</p>
                                    <p className="text-sm text-muted-foreground">
                                        {proj.industry} | 关键词: {proj.keyword_count}个
                                    </p>
                                </div>
                            ))}
                            <div className="flex justify-end gap-2 pt-4">
                                <Button variant="outline" onClick={() => setShowProjectSelect(false)}>
                                    取消
                                </Button>
                                <Button onClick={handleImitate} disabled={!selectedProjectId}>
                                    确认仿写
                                </Button>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 仿写结果弹窗 */}
            {showImitateModal && imitateResult && (
                <div
                    className="fixed inset-0 bg-black/50 flex items-center justify-center z-50 overflow-hidden"
                    onMouseDown={(e) => {
                        if (e.target === e.currentTarget) {
                            setShowImitateModal(false);
                        }
                    }}
                >
                    <Card className="w-[700px] max-h-[80vh] overflow-auto bg-card rounded-xl border border-border" onMouseDown={(e) => e.stopPropagation()}>
                        <CardHeader>
                            <CardTitle>✅ 仿写完成</CardTitle>
                            <CardDescription>已生成 {imitateResult.article_count} 篇文章，点击可查看详情</CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            {imitateResult.articles?.map((article: any, i: number) => (
                                <div key={i} className="p-4 bg-muted rounded-lg border hover:border-brand/40 transition-colors">
                                    <div className="flex justify-between items-start">
                                        <div className="flex-1">
                                            <p className="font-medium">{i + 1}. {article.title}</p>
                                            <p className="text-xs text-muted-foreground mt-1">关键词: {article.keyword}</p>
                                        </div>
                                        {article.id && (
                                            <Button size="sm" variant="outline" onClick={() => {
                                                setPreviewArticle(article);
                                                setShowPreview(true);
                                            }}>
                                                查看详情
                                            </Button>
                                        )}
                                    </div>
                                </div>
                            ))}
                            <div className="pt-4 border-t mt-4">
                                <div className="flex justify-end gap-2">
                                    <Button variant="outline" onClick={() => setShowImitateModal(false)}>
                                        关闭
                                    </Button>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}
        </div>
    );
}

export default ReferenceLibrary;
