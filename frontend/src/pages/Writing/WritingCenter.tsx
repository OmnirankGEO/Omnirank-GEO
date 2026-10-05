import { useState, useEffect } from "react";
import { toast } from 'sonner';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Checkbox } from "@/components/ui/checkbox";
import {
    Loader2, Download, RefreshCw,
    Eye, Trash2, X, RefreshCcw
} from "lucide-react";
import { articlesApi, historyApi, type DiagnosisRecord } from "@/lib/api";
import { useClientContext } from "@/context/ClientContext";
import { saveAs } from 'file-saver';
import { isInWechatBrowser } from '@/lib/wechatJsapi';
import ReactMarkdown from '@/components/SafeMarkdown';
import { useNavigate } from 'react-router-dom';
import { NextStepBar } from '@/components/nav/NextStepBar';
// 标签映射
const TYPE_LABELS: Record<string, string> = {
    authority: '选购与多品牌比较',
    deep_dive: '选购与多品牌比较',
    case_study: '案例、数据与 ROI',
    pitfall: '趋势、政策与风险分析',
    trend: '趋势、政策与风险分析',
    faq: '证据型问答',
    checklist: '方法与实施指南',
    expert: '企业事实与品牌说明',
    company_profile: '企业事实与品牌说明',
    ranking_v2: '选购与多品牌比较',
    recommendation_review: '选购与多品牌比较',
    authority_ranking: '选购与多品牌比较',
    buying_guide: '方法与实施指南',
    trojan_horse: '趋势、政策与风险分析',
    qa_recommendation: '证据型问答',
    brand_softarticle: '企业事实与品牌说明',
    comparison_review: '选购与多品牌比较',
    risk_compliance: '趋势、政策与风险分析',
    price_roi: '案例、数据与 ROI',
    data_report: '案例、数据与 ROI',
};

export function WritingCenter() {
    const navigate = useNavigate();
    // 全局客户上下文
    const { currentBrandId, clientContext } = useClientContext();
    const brandName = clientContext?.brand?.name || null;

    // 诊断记录
    const [diagnoses, setDiagnoses] = useState<DiagnosisRecord[]>([]);
    const [selectedDiagnosis, setSelectedDiagnosis] = useState<string>("");
    const [loading, setLoading] = useState(true);



    // 文章列表
    const [articles, setArticles] = useState<any[]>([]);
    const [batches, setBatches] = useState<string[]>([]);
    const [selectedBatch, setSelectedBatch] = useState<string>("all");
    const [selectedArticles, setSelectedArticles] = useState<Set<string>>(new Set());
    const [downloading, setDownloading] = useState(false);
    const [deleting, setDeleting] = useState(false);

    // 删除确认
    const [deleteConfirm, setDeleteConfirm] = useState<{
        type: 'single' | 'batch';
        filePath?: string;
        count?: number;
    } | null>(null);

    // 文章预览
    const [previewArticle, setPreviewArticle] = useState<{ title: string; content: string } | null>(null);
    const [loadingPreview, setLoadingPreview] = useState(false);

    // 补发
    const [refillTitle, setRefillTitle] = useState("");
    const [refillInstruction, setRefillInstruction] = useState("");
    const [refilling, setRefilling] = useState(false);

    // 加载诊断记录 — 按全局客户品牌过滤（优先用 brand_id，兜底用 brand_name）
    useEffect(() => {
        setLoading(true);
        setSelectedDiagnosis("");
        setDiagnoses([]);
        const params: { limit: number; brand_id?: number; brand?: string } = { limit: 50 };
        if (currentBrandId) {
            params.brand_id = currentBrandId;
        } else if (brandName) {
            params.brand = brandName;
        }
        historyApi.list(params).then(res => {
            setDiagnoses(res.data);
            if (res.data.length > 0) {
                setSelectedDiagnosis(res.data[0].id.toString());
            }
            setLoading(false);
        });
    }, [currentBrandId, brandName]);

    // 切换诊断时加载文章列表
    useEffect(() => {
        if (selectedDiagnosis) {
            articlesApi.list(parseInt(selectedDiagnosis)).then(r => {
                setArticles(r.data.articles || []);
                setBatches(r.data.batches || []);
            });
        }
    }, [selectedDiagnosis]);

    // 筛选文章
    const filteredArticles = selectedBatch === "all"
        ? articles
        : articles.filter(a => a.file_path?.includes(selectedBatch));

    // 全选/取消
    const toggleSelectAll = () => {
        if (selectedArticles.size === filteredArticles.length) {
            setSelectedArticles(new Set());
        } else {
            setSelectedArticles(new Set(filteredArticles.map(a => a.file_path)));
        }
    };

    // 切换单选
    const toggleSelect = (path: string) => {
        const newSet = new Set(selectedArticles);
        if (newSet.has(path)) {
            newSet.delete(path);
        } else {
            newSet.add(path);
        }
        setSelectedArticles(newSet);
    };

    // 批量下载
    const handleDownload = async () => {
        if (selectedArticles.size === 0) return;
        if (isInWechatBrowser()) {
            toast.error("微信内暂不支持下载 ZIP · 请点右上角「···」→「在浏览器打开」后再下载");
            return;
        }
        setDownloading(true);
        try {
            const res = await articlesApi.downloadZip(Array.from(selectedArticles));
            saveAs(new Blob([res.data]), `articles_${Date.now()}.zip`);
            setSelectedArticles(new Set());
        } catch (e) {
            console.error("下载失败:", e);
            toast.error("下载失败 · 请重试");
        } finally {
            setDownloading(false);
        }
    };

    // 预览文章
    const handlePreview = async (article: any) => {
        setLoadingPreview(true);
        try {
            const res = await articlesApi.getContent(article.file_path);
            setPreviewArticle({
                title: article.title || article.filename,
                content: res.data.content
            });
        } catch (e) {
            console.error("加载文章失败:", e);
            toast.error("无法加载文章内容");
        } finally {
            setLoadingPreview(false);
        }
    };

    // 批量删除 - 显示确认对话框
    const handleBatchDelete = () => {
        if (selectedArticles.size === 0) return;
        setDeleteConfirm({ type: 'batch', count: selectedArticles.size });
    };

    // 删除单篇 - 显示确认对话框
    const handleDeleteSingle = (filePath: string) => {
        setDeleteConfirm({ type: 'single', filePath });
    };

    // 确认删除执行
    const confirmDelete = async () => {
        if (!deleteConfirm) return;

        setDeleting(true);
        try {
            if (deleteConfirm.type === 'batch') {
                await articlesApi.batchDelete(Array.from(selectedArticles));
                setSelectedArticles(new Set());
            } else if (deleteConfirm.filePath) {
                // [audit #5 返修] 单删走已加固的 batch-delete(/api/articles/delete 已 410 下线):
                //   batch-delete 端点已有登录态 + output 目录限制 + 单次上限,单篇即一元数组。
                await articlesApi.batchDelete([deleteConfirm.filePath]);
            }
            refreshArticles();
        } catch (e) {
            console.error("删除失败:", e);
        } finally {
            setDeleting(false);
            setDeleteConfirm(null);
        }
    };

    // 刷新文章列表
    const refreshArticles = () => {
        if (selectedDiagnosis) {
            articlesApi.list(parseInt(selectedDiagnosis)).then(r => {
                setArticles(r.data.articles || []);
                setBatches(r.data.batches || []);
            });
        }
    };

    // 补发文章
    const handleRefill = async () => {
        if (!selectedDiagnosis || !refillTitle) return;
        setRefilling(true);
        try {
            await articlesApi.replace({
                diagnosis_id: parseInt(selectedDiagnosis),
                original_title: refillTitle,
                instruction: refillInstruction || undefined
            });
            setRefillTitle("");
            setRefillInstruction("");
            // 刷新列表
            articlesApi.list(parseInt(selectedDiagnosis)).then(r => {
                setArticles(r.data.articles || []);
                setBatches(r.data.batches || []);
            });
            toast.success("补发成功");
        } catch (e) {
            console.error("补发失败:", e);
        } finally {
            setRefilling(false);
        }
    };

    if (loading) {
        return <div className="flex justify-center py-20"><Loader2 className="h-8 w-8 animate-spin text-brand" /></div>;
    }

    return (
        <div className="p-4 md:p-6 space-y-6">
            {/* 页头 */}
            <div className="flex flex-col sm:flex-row justify-between items-start gap-3 sm:gap-4">
                <div className="flex items-center gap-3">
                    <div className="h-8 w-8 sm:h-10 sm:w-10 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
                        <RefreshCcw className="h-4 w-4 sm:h-5 sm:w-5 text-brand" />
                    </div>
                    <div>
                        <h2 className="text-lg sm:text-2xl font-bold text-foreground">历史文章库</h2>
                        <p className="text-xs sm:text-sm text-muted-foreground">只读查看和下载旧诊断批次；新写作、重写与审核统一在 AI 写文章完成</p>
                    </div>
                </div>
                <div className="flex flex-col items-start sm:items-end gap-1 w-full sm:w-auto">
                    <span className="text-xs text-muted-foreground">选择诊断客户</span>
                    {/* 可搜索:接口来源(limit 50)· 生产 306 条诊断 / 单品牌最多 25 */}
                    <SearchableSelect
                        value={selectedDiagnosis}
                        onChange={setSelectedDiagnosis}
                        className="w-full sm:w-[280px]"
                        placeholder="选择诊断记录"
                        searchPlaceholder="搜索品牌名 / 日期"
                        emptyText="没有匹配的诊断记录"
                        options={diagnoses.map(d => ({
                            value: d.id.toString(),
                            label: `${d.brand_name || '未命名'} (${d.created_at?.slice(0, 10)})`,
                        }))}
                    />
                </div>
            </div>

            <Card className="border-emerald-500/25 bg-emerald-500/5">
                <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                    <div className="text-sm text-muted-foreground">历史页不再生成或补发文章，避免形成第二套文体和审核链路。</div>
                    <Button onClick={() => navigate('/writing')}>进入 AI 写文章</Button>
                </CardContent>
            </Card>

            <Tabs defaultValue="list" className="w-full">
                <TabsList><TabsTrigger value="list">历史文章</TabsTrigger></TabsList>

                {/* 历史文件只读列表 */}
                <TabsContent value="list" className="space-y-4">
                    <div className="flex flex-col sm:flex-row justify-between items-start sm:items-center gap-3">
                        <div className="flex items-center gap-3 w-full sm:w-auto">
                            <Select value={selectedBatch} onValueChange={setSelectedBatch}>
                                <SelectTrigger className="w-full sm:w-[200px]">
                                    <SelectValue placeholder="筛选批次" />
                                </SelectTrigger>
                                <SelectContent>
                                    <SelectItem value="all">全部批次</SelectItem>
                                    {batches.map(b => (
                                        <SelectItem key={b} value={b}>{b}</SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                            <span className="text-xs sm:text-sm text-muted-foreground whitespace-nowrap">
                                {filteredArticles.length}篇
                                {selectedArticles.size > 0 && ` (选${selectedArticles.size})`}
                            </span>
                        </div>
                        <div className="flex items-center gap-2">
                            {/* 批量下载 */}
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={handleDownload}
                                disabled={downloading || selectedArticles.size === 0}
                            >
                                {downloading ? <Loader2 className="h-4 w-4 animate-spin sm:mr-1" /> : <Download className="h-4 w-4 sm:mr-1" />}
                                <span className="hidden sm:inline">下载 {selectedArticles.size > 0 ? `(${selectedArticles.size})` : ''}</span>
                            </Button>
                            {/* 批量删除 */}
                            <Button
                                variant="outline"
                                size="sm"
                                onClick={handleBatchDelete}
                                disabled={deleting || selectedArticles.size === 0}
                                className="text-red-600 hover:text-red-700 hover:bg-red-50"
                            >
                                {deleting ? <Loader2 className="h-4 w-4 animate-spin sm:mr-1" /> : <Trash2 className="h-4 w-4 sm:mr-1" />}
                                <span className="hidden sm:inline">删除 {selectedArticles.size > 0 ? `(${selectedArticles.size})` : ''}</span>
                            </Button>
                            {/* 刷新 */}
                            <Button variant="outline" size="icon" onClick={refreshArticles} className="h-8 w-8 sm:h-9 sm:w-9">
                                <RefreshCw className="h-4 w-4" />
                            </Button>
                        </div>
                    </div>

                    <Card className="border border-border rounded-xl overflow-x-auto">
                        <Table>
                            <TableHeader>
                                <TableRow>
                                    <TableHead className="w-10">
                                        <Checkbox
                                            checked={selectedArticles.size > 0 && selectedArticles.size === filteredArticles.length}
                                            onCheckedChange={toggleSelectAll}
                                        />
                                    </TableHead>
                                    <TableHead>标题</TableHead>
                                    <TableHead className="hidden sm:table-cell">类型</TableHead>
                                    <TableHead className="hidden sm:table-cell">字数</TableHead>
                                    <TableHead className="hidden md:table-cell">生成时间</TableHead>
                                    <TableHead className="w-20 sm:w-24 text-center">操作</TableHead>
                                </TableRow>
                            </TableHeader>
                            <TableBody>
                                {filteredArticles.length === 0 ? (
                                    <TableRow>
                                        <TableCell colSpan={6} className="text-center py-8 text-muted-foreground">
                                            暂无文章
                                        </TableCell>
                                    </TableRow>
                                ) : (
                                    filteredArticles.map((article, i) => (
                                        <TableRow key={i}>
                                            <TableCell>
                                                <Checkbox
                                                    checked={selectedArticles.has(article.file_path)}
                                                    onCheckedChange={() => toggleSelect(article.file_path)}
                                                />
                                            </TableCell>
                                            <TableCell className="font-medium max-w-[150px] sm:max-w-md truncate text-xs sm:text-sm">
                                                {article.title || article.filename}
                                            </TableCell>
                                            <TableCell className="hidden sm:table-cell">
                                                <Badge variant="outline">
                                                    {TYPE_LABELS[article.type] || article.type || '-'}
                                                </Badge>
                                            </TableCell>
                                            <TableCell className="hidden sm:table-cell">{article.word_count || '-'}</TableCell>
                                            <TableCell className="text-muted-foreground hidden md:table-cell">
                                                {article.created_at?.slice(0, 16) || '-'}
                                            </TableCell>
                                            <TableCell>
                                                <div className="flex items-center gap-1 justify-center">
                                                    <Button
                                                        variant="ghost"
                                                        size="icon"
                                                        className="h-7 w-7 sm:h-8 sm:w-8"
                                                        onClick={() => handlePreview(article)}
                                                        disabled={loadingPreview}
                                                    >
                                                        <Eye className="h-4 w-4 text-blue-600" />
                                                    </Button>
                                                    <Button
                                                        variant="ghost"
                                                        size="icon"
                                                        className="h-7 w-7 sm:h-8 sm:w-8 text-red-500 hover:text-red-700 hover:bg-red-50"
                                                        onClick={() => handleDeleteSingle(article.file_path)}
                                                    >
                                                        <Trash2 className="h-4 w-4" />
                                                    </Button>
                                                </div>
                                            </TableCell>
                                        </TableRow>
                                    ))
                                )}
                            </TableBody>
                        </Table>
                    </Card>
                </TabsContent>


            </Tabs>

            {/* 文章预览模态框 */}
            {previewArticle && (
                <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-0 sm:p-4">
                    <div className="bg-card sm:rounded-xl border-0 sm:border border-border sm:max-w-4xl w-full h-full sm:h-auto sm:max-h-[90vh] flex flex-col">
                        <div className="flex items-center justify-between p-3 sm:p-4 border-b border-border shrink-0">
                            <h3 className="text-base sm:text-lg font-semibold line-clamp-2 sm:truncate flex-1 mr-3">
                                {previewArticle.title}
                            </h3>
                            <Button
                                variant="ghost"
                                size="icon"
                                onClick={() => setPreviewArticle(null)}
                                className="shrink-0"
                            >
                                <X className="h-5 w-5" />
                            </Button>
                        </div>
                        <div className="flex-1 overflow-auto p-3 sm:p-6">
                            <div className="prose prose-sm dark:prose-invert max-w-none">
                                <ReactMarkdown>{previewArticle.content}</ReactMarkdown>
                            </div>
                        </div>
                        <div className="flex justify-end gap-2 p-3 sm:p-4 border-t border-border shrink-0">
                            <Button variant="outline" onClick={() => setPreviewArticle(null)}>
                                关闭
                            </Button>
                        </div>
                    </div>
                </div>
            )}

            {/* 删除确认对话框 */}
            {deleteConfirm && (
                <div className="fixed inset-0 z-50 bg-black/50 flex items-center justify-center p-4">
                    <div className="bg-card rounded-xl border border-border max-w-md w-full">
                        <div className="p-6">
                            <div className="flex items-center gap-3 mb-4">
                                <div className="h-10 w-10 rounded-full bg-red-100 flex items-center justify-center">
                                    <Trash2 className="h-5 w-5 text-red-600" />
                                </div>
                                <h3 className="text-lg font-semibold">确认删除</h3>
                            </div>
                            <p className="text-muted-foreground mb-6">
                                {deleteConfirm.type === 'batch'
                                    ? `确定要删除选中的 ${deleteConfirm.count} 篇文章吗？`
                                    : "确定要删除这篇文章吗？"
                                }
                                <br />
                                <span className="text-red-600 text-sm">此操作不可撤销。</span>
                            </p>
                            <div className="flex justify-end gap-3">
                                <Button
                                    variant="outline"
                                    onClick={() => setDeleteConfirm(null)}
                                    disabled={deleting}
                                >
                                    取消
                                </Button>
                                <Button
                                    variant="destructive"
                                    onClick={confirmDelete}
                                    disabled={deleting}
                                    className="bg-red-600 hover:bg-red-700"
                                >
                                    {deleting ? <Loader2 className="h-4 w-4 animate-spin mr-2" /> : null}
                                    确认删除
                                </Button>
                            </div>
                        </div>
                    </div>
                </div>
            )}
            <NextStepBar page="writing" />
        </div>
    );
}
