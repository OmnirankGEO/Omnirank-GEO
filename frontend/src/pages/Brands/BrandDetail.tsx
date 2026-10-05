import { useEffect, useState, useCallback } from "react";
import { measured } from '@/lib/defensiveGeoPresentation';
import { useParams, useNavigate, Link, useSearchParams } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ArrowLeft, RefreshCw, FileText, TrendingUp, Calendar, Database, Upload, Trash2, AlertCircle, Loader2, CheckCircle2, Brain, Zap, Megaphone } from "lucide-react";
import { toast } from 'sonner';
import MarketingTab from "./MarketingTab";
import { brandsApi, knowledgeApi, type Brand, type BrandTrend, type DiagnosisRecord, type KBDocument , authFetch } from "@/lib/api";
import { useAuth } from "@/context/AuthContext";
import { useWaitMessage } from "@/hooks/useWaitMessage";
import {
    LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Legend,
    RadarChart, PolarGrid, PolarAngleAxis, PolarRadiusAxis, Radar,
} from 'recharts';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

// 上传进度状态类型
type UploadStatus = 'idle' | 'uploading' | 'cleaning' | 'vectorizing' | 'done' | 'error';

export function BrandDetail() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const { id } = useParams<{ id: string }>();
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const isEmbedded = searchParams.get('embedded') === 'true';
    const { hasPermission, user } = useAuth();
    const isAdmin = user?.is_admin === true;
    const canDeleteBrand = !isEmbedded && (isAdmin || hasPermission('brands:delete'));
    const [brand, setBrand] = useState<Brand | null>(null);
    const [trend, setTrend] = useState<BrandTrend | null>(null);
    const [diagnoses, setDiagnoses] = useState<DiagnosisRecord[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState<string | null>(null);

    // 🆕 知识库状态
    const [kbDocuments, setKbDocuments] = useState<KBDocument[]>([]);
    const [kbLoading, setKbLoading] = useState(false);
    const [uploading, setUploading] = useState(false);
    const [selectedFile, setSelectedFile] = useState<File | null>(null);
    const [uploadStatus, setUploadStatus] = useState<UploadStatus>('idle');
    const [uploadResult, setUploadResult] = useState<any>(null);
    // [CTO-13.3 2026-04-20] 文档处理 rotating 安慰词 (不暴露模型名)
    const kbWaitActive = uploadStatus !== 'idle' && uploadStatus !== 'done' && uploadStatus !== 'error';
    const kbWaitMsg = useWaitMessage(kbWaitActive, 'kb_upload');



    useEffect(() => {
        if (id) {
            fetchData(parseInt(id));
        }
    }, [id]);

    const fetchData = async (brandId: number) => {
        setLoading(true);
        try {
            const brandRes = await brandsApi.getById(brandId);
            // 合并重定向：如果品牌已被合并到其他品牌，自动跳转
            if ((brandRes.data as any).redirect_to) {
                navigate(`/brands/${(brandRes.data as any).redirect_to}`, { replace: true });
                return;
            }
            const [trendRes, diagnosesRes] = await Promise.all([
                brandsApi.getTrend(brandId),
                brandsApi.getDiagnoses(brandId),
            ]);
            setBrand(brandRes.data.data);
            setTrend(trendRes.data.data);
            setDiagnoses(diagnosesRes.data.data || []);
            setError(null);
        } catch (err: any) {
            console.error("Failed to fetch brand data:", err);
            setError(err.message || "加载失败");
        } finally {
            setLoading(false);
        }
    };

    // 🆕 加载知识库文档
    const loadKbDocuments = useCallback(async () => {
        if (!id) return;
        setKbLoading(true);
        try {
            const res = await knowledgeApi.list('client', id, '');
            if (res.data.status === 'success') {
                setKbDocuments(res.data.documents || []);
            }
        } catch (e) {
            console.error('加载知识库失败', e);
        } finally {
            setKbLoading(false);
        }
    }, [id]);

    // 🆕 上传文件（含LLM清洗+向量化）
    const handleUpload = async () => {
        if (!selectedFile || !id) {
            toast('请选择要上传的文件');
            return;
        }

        // 检查文件类型
        const ext = selectedFile.name.split('.').pop()?.toLowerCase();
        if (!['md', 'txt', 'pdf'].includes(ext || '')) {
            toast.error('仅支持 .md, .txt, .pdf 格式');
            return;
        }

        setUploading(true);
        setUploadStatus('uploading');
        setUploadResult(null);

        try {
            // 读取文件内容 (对PDF使用后端解析)
            let content = '';
            if (ext === 'pdf') {
                // PDF需要用FormData上传，后端解析
                const formData = new FormData();
                formData.append('file', selectedFile);
                formData.append('kb_type', 'client');
                formData.append('kb_id', id);

                setUploadStatus('cleaning');

                // [2026-06-07 P0 fix] AbortController 90s · 主动控制超时 · 不依赖网络栈底层
                //   外层 catch 已有"可能是超时"文案 · 加 signal 后超时统一走 catch · 显"可能仍在处理"
                // [2026-06-07 P0 fix v3 老板审核 P3] timer 包 inner try-finally:
                //   原:authFetch 90s 前抛错 → clearTimeout 不执行 → timer 仍触发一次 abort(影响小但脏)
                //   修:finally 必清 · 不留 dangling timer
                const ctrl = new AbortController();
                const timer = setTimeout(() => ctrl.abort(), 90000);
                try {
                    const res = await authFetch('/api/knowledge/upload-file', {
                        method: 'POST',
                        body: formData,
                        signal: ctrl.signal,
                    });
                    const data = await res.json();

                    if (data.status === 'success') {
                        const pipeline = data.pipeline;
                        setUploadStatus('done');
                        setUploadResult({
                            success: true,
                            chunks: pipeline?.chunk_count || 0,
                            knowledgePoints: pipeline?.knowledge_points || 0,
                            keywords: pipeline?.keywords || [],
                            timeMs: pipeline?.processing_time_ms || 0
                        });
                        setSelectedFile(null);
                        loadKbDocuments();
                    } else {
                        setUploadStatus('error');
                        setUploadResult({ success: false, error: data.error || '上传失败' });
                    }
                } finally {
                    clearTimeout(timer);
                }
                return;
            }

            // 对于文本文件，读取内容
            content = await selectedFile.text();

            setUploadStatus('cleaning');

            const res = await knowledgeApi.upload({
                kb_type: 'client',
                kb_id: id,
                filename: selectedFile.name,
                content: content,
            });

            if (res.data.status === 'success') {
                const pipeline = res.data.pipeline;
                if (pipeline?.processed) {
                    setUploadStatus('done');
                    setUploadResult({
                        success: true,
                        chunks: pipeline.chunk_count,
                        knowledgePoints: pipeline.knowledge_points,
                        keywords: pipeline.keywords || [],
                        timeMs: pipeline.processing_time_ms
                    });
                } else {
                    setUploadStatus('done');
                    setUploadResult({ success: true, chunks: 0, knowledgePoints: 0, keywords: [], timeMs: 0 });
                }
                setSelectedFile(null);
                loadKbDocuments();
            } else {
                setUploadStatus('error');
                setUploadResult({ success: false, error: res.data.error || '上传失败' });
            }
        } catch (e: any) {
            // [2026-06-07 P0 fix] 区分超时/网络断 vs 真失败 · 后端 prod 实证可能仍在处理 · 不报"失败"
            const msg = String(e?.message || e?.name || '');
            const isTimeout = e?.name === 'AbortError' || /timeout|network|fetch/i.test(msg);
            if (isTimeout) {
                setUploadStatus('done');  // 不标"error" · 不让客户重复上传(后端 chunks 可能已入库)
                setUploadResult({
                    success: true,
                    chunks: 0,
                    knowledgePoints: 0,
                    keywords: [],
                    timeMs: 0,
                    pending: true,  // [2026-06-07] UI 可据此标"后台处理中"
                } as any);
                toast.info('文件较大或网络中断 · 服务器可能仍在处理 · 请稍后刷新查看结果', { duration: 6000 });
            } else {
                setUploadStatus('error');
                const errorMsg = e?.response?.data?.error || e?.message || '上传失败';
                setUploadResult({ success: false, error: errorMsg });
            }
            console.error('上传错误:', e);
        } finally {
            setUploading(false);
        }
    };

    // 🆕 删除文档
    const handleDeleteDoc = async (filename: string) => {
        if (!(await askConfirm({ title: `确定删除 ${filename}？`, danger: true }))) return;
        if (!id) return;
        try {
            const res = await knowledgeApi.delete('client', id, filename);
            if (res.data.status === 'success') {
                toast.success('删除成功');
                loadKbDocuments();
            } else {
                toast.error(res.data.error || '删除失败');
            }
        } catch (e) {
            toast.error('删除失败');
        }
    };

    // 🆕 格式化文件大小
    const formatSize = (bytes: number) => {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    };


    const getLevelColor = (level: string) => {
        switch (level) {
            case '领先': return 'bg-green-500';
            case '成熟': return 'bg-emerald-500';
            case '成长': return 'bg-blue-500';
            case '起步': return 'bg-yellow-500';
            default: return 'bg-gray-400';
        }
    };

    const formatDate = (dateStr: string) => {
        const date = new Date(dateStr);
        return `${date.getMonth() + 1}/${date.getDate()}`;
    };

    // 准备趋势图数据
    const trendChartData = trend?.diagnoses.map((d, index) => ({
        name: `第${index + 1}次`,
        date: formatDate(d.created_at),
        总分: d.total_score,
        // [CUR-02] 未测量传 null 而不是 0:Recharts 会**断线**,
        // 如实表示"这次没测";写 0 会画成一条掉到底的线,读起来像"分数崩了"。
        网页搜索: measured(d.web_search_score),
        平台覆盖: measured(d.platform_score),
        内容质量: measured(d.content_quality_score),
        权威性: measured(d.authority_score),
        AI可见度: measured(d.ai_visibility_score),
    })) || [];

    // 准备最新诊断的雷达图数据
    //
    // [CUR-02 2026-08-21] 🔴 null 与 0 严格分开。
    // 改造前每一维都是 `xxx_score || 0` —— **没测过**被画成 0 分,
    // 和**真的 0 分**在雷达图上长得一模一样。对销售来说这两件事的
    // 下一步完全不同(去测 vs 去修),混在一起等于把「不知道」谎报成「很差」。
    // §9.7 逐字:「不显示 0 分;解释『还没有测试数据』」。
    //
    // 修法:未测量的维度**不进雷达**,单独列出来告诉用户还没测。
    const latestDiagnosis = trend?.diagnoses[trend.diagnoses.length - 1];
    type TrendDiagnosis = BrandTrend['diagnoses'][number];
    const RADAR_DIMENSIONS: { dimension: string; pick: (d: TrendDiagnosis) => number | null | undefined; fullMark: number }[] = [
        { dimension: '网页搜索', pick: (d) => d.web_search_score, fullMark: 15 },
        { dimension: '平台覆盖', pick: (d) => d.platform_score, fullMark: 15 },
        { dimension: '内容质量', pick: (d) => d.content_quality_score, fullMark: 15 },
        { dimension: '权威性', pick: (d) => d.authority_score, fullMark: 15 },
        { dimension: '品牌所有权', pick: (d) => d.brand_ownership_score, fullMark: 15 },
        { dimension: 'AI可见度', pick: (d) => d.ai_visibility_score, fullMark: 10 },
        { dimension: 'AI搜索可见度', pick: (d) => d.ai_citation_score, fullMark: 10 },
        { dimension: '更新频率', pick: (d) => d.update_frequency_score, fullMark: 5 },
    ];
    const radarData = latestDiagnosis
        ? RADAR_DIMENSIONS
            .map((d) => ({ dimension: d.dimension, value: measured(d.pick(latestDiagnosis)), fullMark: d.fullMark }))
            .filter((d): d is { dimension: string; value: number; fullMark: number } => d.value !== null)
        : [];
    const unmeasuredDimensions = latestDiagnosis
        ? RADAR_DIMENSIONS.filter((d) => measured(d.pick(latestDiagnosis)) === null).map((d) => d.dimension)
        : [];

    if (loading) {
        return (
            <div className="flex flex-col items-center justify-center h-64 gap-3">
                <Loader2 className="h-8 w-8 animate-spin text-brand" />
                <p className="text-sm text-muted-foreground">加载中...</p>
            </div>
        );
    }

    if (error || !brand) {
        return (
            <div className="space-y-4">
                <Button variant="ghost" onClick={() => navigate('/brands')}>
                    <ArrowLeft className="mr-2 h-4 w-4" /> 返回
                </Button>
                <Card className="border border-border rounded-xl p-6 text-center text-red-500">{error || "品牌不存在"}</Card>
            </div>
        );
    }

    return (
        <div className="p-4 md:p-6 space-y-6">
            {/* 页头 */}
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                    <Button variant="ghost" size="icon" onClick={() => navigate('/brands')}>
                        <ArrowLeft className="h-4 w-4" />
                    </Button>
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                        <TrendingUp className="h-5 w-5 text-brand" />
                    </div>
                    <div>
                        <h2 className="text-2xl font-bold text-foreground">{brand.name}</h2>
                        <p className="text-sm text-muted-foreground">
                            {brand.industry || '未分类'} · {brand.diagnosis_count} 次诊断
                        </p>
                    </div>
                </div>
                {!isEmbedded && (
                <Link to={`/diagnosis/new?retest=1&brand=${encodeURIComponent(brand.name)}&industry=${encodeURIComponent(brand.industry || '')}`}>
                    <Button>
                        <RefreshCw className="mr-2 h-4 w-4" /> 发起复测
                    </Button>
                </Link>
                )}
            </div>

            {/* 🆕 使用Tabs分隔概览和知识库 */}
            <Tabs defaultValue={isEmbedded ? "marketing" : "overview"} onValueChange={(value) => {
                if (value === 'knowledge') loadKbDocuments();
            }}>
                <TabsList className="mb-4">
                    <TabsTrigger value="overview">
                        <TrendingUp className="w-4 h-4 mr-2" />
                        概览
                    </TabsTrigger>
                    <TabsTrigger value="marketing">
                        <Megaphone className="w-4 h-4 mr-2" />
                        客户营销资料
                    </TabsTrigger>
                    <TabsTrigger value="knowledge">
                        <Database className="w-4 h-4 mr-2" />
                        知识库
                    </TabsTrigger>
                </TabsList>

                {/* 概览Tab */}
                <TabsContent value="overview" className="space-y-6">
                    {/* 趋势洞察 */}
                    {trend && trend.trend_insight && (
                        <Card className="border border-blue-200 rounded-xl bg-blue-50/50">
                            <CardContent className="py-4">
                                <div className="flex items-center gap-3">
                                    <div className="h-10 w-10 rounded-xl bg-blue-100 flex items-center justify-center shrink-0">
                                        <TrendingUp className="h-5 w-5 text-blue-600" />
                                    </div>
                                    <p className="text-base font-medium text-foreground">{trend.trend_insight}</p>
                                </div>
                            </CardContent>
                        </Card>
                    )}

                    {/* 图表区域 */}
                    <div className="grid gap-6 md:grid-cols-2">
                        {/* GEO分数趋势图 */}
                        <Card className="border border-border rounded-xl">
                            <CardHeader>
                                <CardTitle className="text-foreground">GEO分数趋势</CardTitle>
                                <CardDescription>历次诊断总分变化</CardDescription>
                            </CardHeader>
                            <CardContent>
                                {trendChartData.length > 1 ? (
                                    <ResponsiveContainer width="100%" height={250}>
                                        <LineChart data={trendChartData}>
                                            <CartesianGrid strokeDasharray="3 3" />
                                            <XAxis dataKey="date" />
                                            <YAxis domain={[0, 100]} />
                                            <Tooltip />
                                            <Legend />
                                            <Line type="monotone" dataKey="总分" stroke="#3b82f6" strokeWidth={2} dot={{ r: 4 }} />
                                        </LineChart>
                                    </ResponsiveContainer>
                                ) : (
                                    <div className="h-[250px] flex items-center justify-center text-muted-foreground">
                                        需要至少2次诊断才能显示趋势图
                                    </div>
                                )}
                            </CardContent>
                        </Card>

                        {/* 8维度雷达图 */}
                        <Card className="border border-border rounded-xl">
                            <CardHeader>
                                <CardTitle className="text-foreground">8维度评分</CardTitle>
                                <CardDescription>最新诊断各维度得分</CardDescription>
                            </CardHeader>
                            <CardContent>
                                {unmeasuredDimensions.length > 0 && (
                                    // §9.7「不显示 0 分」:没测的维度如实说没测,
                                    // 而不是让它们以 0 分混进雷达里假装测过。
                                    <p className="mb-2 text-xs text-muted-foreground">
                                        本次未测:{unmeasuredDimensions.join('、')}
                                    </p>
                                )}
                                {radarData.length > 0 ? (
                                    <ResponsiveContainer width="100%" height={250}>
                                        <RadarChart data={radarData}>
                                            <PolarGrid />
                                            <PolarAngleAxis dataKey="dimension" tick={{ fontSize: 11 }} />
                                            <PolarRadiusAxis angle={30} domain={[0, 15]} />
                                            <Radar name="得分" dataKey="value" stroke="#8b5cf6" fill="#8b5cf6" fillOpacity={0.5} />
                                        </RadarChart>
                                    </ResponsiveContainer>
                                ) : (
                                    <div className="h-[250px] flex items-center justify-center text-muted-foreground">
                                        暂无诊断数据
                                    </div>
                                )}
                            </CardContent>
                        </Card>
                    </div>

                    {/* 诊断历史 */}
                    <Card className="border border-border rounded-xl">
                        <CardHeader>
                            <CardTitle className="text-foreground">诊断历史</CardTitle>
                            <CardDescription>该品牌的所有诊断记录</CardDescription>
                        </CardHeader>
                        <CardContent>
                            {diagnoses.length === 0 ? (
                                <p className="text-muted-foreground text-center py-4">暂无诊断记录</p>
                            ) : (
                                <div className="space-y-3">
                                    {diagnoses.map((d, index) => (
                                        <div
                                            key={d.id}
                                            className="flex items-center justify-between p-3 rounded-lg border border-border hover:bg-muted transition-all duration-200 cursor-pointer"
                                            onClick={() => navigate(`/diagnosis/report/${d.id}`)}
                                        >
                                            <div className="flex items-center gap-4">
                                                <div className="text-sm text-muted-foreground w-16">
                                                    #{diagnoses.length - index}
                                                </div>
                                                <div>
                                                    <div className="flex items-center gap-2">
                                                        <Calendar className="h-4 w-4 text-muted-foreground" />
                                                        <span className="text-sm">{new Date(d.created_at).toLocaleString('zh-CN')}</span>
                                                    </div>
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-3">
                                                <span className="text-2xl font-semibold text-foreground">{d.total_score}</span>
                                                <Badge className={getLevelColor(d.level)}>{d.level}</Badge>
                                                <FileText className="h-5 w-5 text-muted-foreground" />
                                            </div>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </CardContent>
                    </Card>
                </TabsContent>

                {/* 客户营销资料Tab */}
                <TabsContent value="marketing">
                    <MarketingTab
                        brandId={parseInt(id!)}
                        brandName={brand.name}
                        canEdit={isAdmin || hasPermission('brands:write')}
                    />
                </TabsContent>

                {/* 🆕 知识库Tab */}
                <TabsContent value="knowledge" className="space-y-6">
                    <Card className="border border-border rounded-xl">
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2 text-foreground">
                                <Database className="w-5 h-5 text-brand" />
                                客户专属知识库
                            </CardTitle>
                            <CardDescription>
                                上传的知识文档将被AI员工和顾问在处理该客户任务时参考
                            </CardDescription>
                        </CardHeader>
                        <CardContent className="space-y-6">
                            {/* 文档列表 */}
                            <div>
                                <h3 className="font-medium mb-3 text-foreground">文档列表</h3>
                                {kbLoading ? (
                                    <div className="flex flex-col items-center justify-center py-8 gap-3">
                                        <Loader2 className="h-6 w-6 animate-spin text-brand" />
                                        <p className="text-sm text-muted-foreground">加载中...</p>
                                    </div>
                                ) : kbDocuments.length === 0 ? (
                                    <div className="text-center py-8 text-muted-foreground flex flex-col items-center gap-2">
                                        <AlertCircle className="w-8 h-8 text-muted-foreground/50" />
                                        <p className="text-sm">暂无文档，上传后可被AI员工使用</p>
                                    </div>
                                ) : (
                                    <div className="space-y-2">
                                        {kbDocuments.map(doc => (
                                            <div key={doc.filename}
                                                className="flex items-center justify-between p-3 bg-muted rounded-lg hover:bg-muted/80 transition-all duration-200"
                                            >
                                                <div className="flex items-center gap-3">
                                                    <FileText className="w-5 h-5 text-brand" />
                                                    <div>
                                                        <p className="font-medium text-foreground">{doc.filename}</p>
                                                        <p className="text-xs text-muted-foreground">
                                                            {formatSize(doc.size)} · {new Date(doc.modified).toLocaleString()}
                                                        </p>
                                                    </div>
                                                </div>
                                                {canDeleteBrand && (
                                                    <Button
                                                        variant="ghost"
                                                        size="sm"
                                                        className="text-red-500 hover:text-red-700"
                                                        onClick={() => handleDeleteDoc(doc.filename)}
                                                    >
                                                        <Trash2 className="w-4 h-4" />
                                                    </Button>
                                                )}
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            {/* 上传表单 */}
                            <div className="border-t border-border pt-6">
                                <h3 className="font-medium mb-3 text-foreground">上传文档</h3>
                                <div className="space-y-4">
                                    {/* 文件选择器 */}
                                    <div className="border-2 border-dashed border-border rounded-xl p-6 text-center hover:border-brand transition-colors">
                                        <input
                                            type="file"
                                            id="kb-file-upload"
                                            accept=".md,.txt,.pdf"
                                            onChange={(e) => setSelectedFile(e.target.files?.[0] || null)}
                                            disabled={uploading}
                                            className="hidden"
                                        />
                                        <label htmlFor="kb-file-upload" className="cursor-pointer">
                                            <Upload className="w-8 h-8 mx-auto text-muted-foreground mb-2" />
                                            {selectedFile ? (
                                                <div>
                                                    <p className="font-medium text-brand">{selectedFile.name}</p>
                                                    <p className="text-sm text-muted-foreground">{(selectedFile.size / 1024).toFixed(1)} KB</p>
                                                </div>
                                            ) : (
                                                <div>
                                                    <p className="text-muted-foreground">点击或拖拽文件到此处</p>
                                                    <p className="text-sm text-muted-foreground/70 mt-1">支持 .md, .txt, .pdf 格式</p>
                                                </div>
                                            )}
                                        </label>
                                    </div>


                                    {/* 上传进度显示 */}
                                    {uploadStatus !== 'idle' && uploadStatus !== 'done' && uploadStatus !== 'error' && (
                                        <div className="flex items-center gap-3 p-4 bg-blue-50 rounded-xl border border-blue-200">
                                            <Loader2 className="w-5 h-5 animate-spin text-brand" />
                                            <div className="flex-1">
                                                <p className="font-medium text-blue-800">
                                                    {uploadStatus === 'uploading' && '正在上传文档...'}
                                                    {uploadStatus === 'cleaning' && '🧠 LLM正在提取知识点...'}
                                                    {uploadStatus === 'vectorizing' && '⚡ 正在向量化...'}
                                                </p>
                                                {/* [CTO-13.3 2026-04-20] rotating 安慰词替代"使用 qwen3-max + text-embedding-v4 处理"模型名泄漏 */}
                                                <p className="text-sm text-blue-600">
                                                    {kbWaitMsg || 'AI 正在处理文档...'}
                                                </p>
                                            </div>
                                        </div>
                                    )}

                                    {/* [2026-06-07 P0 fix v3 老板审核 P2] pending 分支优先于绿色"处理完成":
                                          AbortError/超时路径设了 pending=true · 但 UI 之前没消费 · 仍走绿色"完成"+0 指标 = 假成功
                                          改为:pending 时显黄色"可能仍在处理 · 请稍后刷新查看" · 不展示 0 指标 */}
                                    {uploadStatus === 'done' && uploadResult?.pending && (
                                        <div className="p-4 bg-amber-50 rounded-xl border border-amber-200">
                                            <div className="flex items-center gap-2 mb-2">
                                                <AlertCircle className="w-5 h-5 text-amber-600" />
                                                <span className="font-medium text-amber-800">服务器可能仍在处理</span>
                                            </div>
                                            <p className="text-sm text-amber-700">
                                                文件较大或网络中断 · 后台可能仍在向量化中 · 请稍后刷新页面查看是否入库。
                                            </p>
                                        </div>
                                    )}

                                    {/* 处理结果显示(真成功 · 排除 pending) */}
                                    {uploadStatus === 'done' && uploadResult?.success && !uploadResult?.pending && (
                                        <div className="p-4 bg-green-50 rounded-xl border border-green-200">
                                            <div className="flex items-center gap-2 mb-2">
                                                <CheckCircle2 className="w-5 h-5 text-green-600" />
                                                <span className="font-medium text-green-800">处理完成</span>
                                            </div>
                                            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
                                                <div className="flex items-center gap-1">
                                                    <Zap className="w-4 h-4 text-amber-500" />
                                                    <span>知识块: <span className="text-foreground font-semibold">{uploadResult.chunks}</span></span>
                                                </div>
                                                <div className="flex items-center gap-1">
                                                    <Brain className="w-4 h-4 text-purple-500" />
                                                    <span>知识点: <span className="text-foreground font-semibold">{uploadResult.knowledgePoints}</span></span>
                                                </div>
                                                <div className="col-span-2">
                                                    关键词: {uploadResult.keywords?.slice(0, 3).join(', ') || '无'}
                                                </div>
                                            </div>
                                            <p className="text-xs text-muted-foreground mt-2">耗时: <span className="text-foreground font-semibold">{uploadResult.timeMs}ms</span></p>
                                        </div>
                                    )}

                                    {/* 错误显示 */}
                                    {uploadStatus === 'error' && (
                                        <div className="p-4 bg-red-50 rounded-xl border border-red-200">
                                            <div className="flex items-center gap-2">
                                                <AlertCircle className="w-5 h-5 text-red-600" />
                                                <span className="font-medium text-red-800">处理失败</span>
                                            </div>
                                            <p className="text-sm text-red-600 mt-1">{uploadResult?.error}</p>
                                        </div>
                                    )}

                                    <Button onClick={handleUpload} disabled={uploading}>
                                        <Upload className="w-4 h-4 mr-2" />
                                        {uploading ? '处理中...' : '上传并处理'}
                                    </Button>
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </TabsContent>
            </Tabs>
          {confirmDialog}
        </div>
    );
}
