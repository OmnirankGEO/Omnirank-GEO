import { authFetch } from '@/lib/api';
import { toast } from 'sonner';
import { useState, useEffect } from "react";
import { useClientContext } from '@/context/ClientContext';
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
    FileText, Edit, Send, Check, Clock, Filter, RefreshCw, Download,
    TrendingUp, TrendingDown, BarChart3, Eye, ArrowLeft, Loader2, Copy, CheckCircle, Sparkles,
    Monitor, Settings2, Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { copyAsyncText } from '@/lib/copyUtils';
import { ManualCopyDialog } from '@/components/common/ManualCopyDialog';
import ReactMarkdown from '@/components/SafeMarkdown';
import { BridgeBanner } from '@/components/workbench/BridgeBanner';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

interface KeywordStat {
    keyword: string;
    target_brand?: string;
    avg_rate: number;
    tests_count: number;
}

interface SummaryData {
    detection_rate?: number;
    avg_detection_rate?: number;
    target_rate?: number;
    tier_name?: string;
    total_tests?: number;
    total_detected?: number;
    task_count?: number;
    total_keywords?: number;
    total_publications?: number;
    period_label?: string;
    date?: string;
    keyword_stats?: KeywordStat[];
    top_keywords?: KeywordStat[];
    bottom_keywords?: KeywordStat[];
    generated_at?: string;
}

interface Report {
    id: number;
    brand_id: number;
    report_type: string;
    period_start: string;
    period_end: string;
    status: string;
    summary_data: SummaryData;
    content: string | null;
    reviewed_by: string | null;
    reviewed_at: string | null;
    sent_at: string | null;
    created_at: string;
}

const STATUS_MAP: Record<string, { label: string; color: string; badgeColor: string; icon: React.ComponentType<{ className?: string }> }> = {
    draft: { label: "草稿", color: "text-yellow-600", badgeColor: "bg-yellow-100 text-yellow-700 border-yellow-200", icon: Clock },
    reviewed: { label: "已审核", color: "text-blue-600", badgeColor: "bg-blue-100 text-blue-700 border-blue-200", icon: Check },
    sent: { label: "已发送", color: "text-green-600", badgeColor: "bg-green-100 text-green-700 border-green-200", icon: Send },
};

const TYPE_MAP: Record<string, string> = {
    daily: "日报", weekly: "周报", monthly: "月报", quarterly: "季报", yearly: "年报",
};

function getRateColor(rate: number) {
    if (rate >= 80) return "text-green-600";
    if (rate >= 60) return "text-blue-600";
    if (rate >= 40) return "text-yellow-600";
    return "text-red-600";
}

function getRateBg(rate: number) {
    if (rate >= 80) return "bg-green-50 border-green-200";
    if (rate >= 60) return "bg-blue-50 border-blue-200";
    if (rate >= 40) return "bg-yellow-50 border-yellow-200";
    return "bg-red-50 border-red-200";
}

// [CTO-15.23 2026-05-05] 删手写 renderMarkdown · 改用 ReactMarkdown + remark-gfm
// 老板反馈"展开回答里很多 markdown 符号没渲染干净"
// 原手写缺 `code` / > 引用 / [link] / ~~strike~~ / | 表格 / 嵌套列表

export default function ReportManagement() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const { currentBrandId } = useClientContext();
    const [reports, setReports] = useState<Report[]>([]);
    const [loading, setLoading] = useState(false);
    const [statusFilter, setStatusFilter] = useState("");
    const [typeFilter, setTypeFilter] = useState("");

    // 详情/编辑
    const [viewReport, setViewReport] = useState<Report | null>(null);
    // [WO_WHITELABEL_COPY_UX 项2] 剪贴板被拒但链接已拿到 → 弹可选中链接框
    const [manualCopyText, setManualCopyText] = useState<string | null>(null);
    const [editMode, setEditMode] = useState(false);
    const [editContent, setEditContent] = useState("");
    const [saving, setSaving] = useState(false);
    const [copied, setCopied] = useState(false);
    const [aiWriting, setAiWriting] = useState(false);
    const [viewTab, setViewTab] = useState<"manage" | "client">("manage");
    const [allReports, setAllReports] = useState<Report[]>([]);

    const fetchReports = async () => {
        setLoading(true);
        try {
            let url = "/api/reports/all?limit=50";
            if (currentBrandId) url += `&brand_id=${currentBrandId}`;
            if (statusFilter) url += `&status=${statusFilter}`;
            if (typeFilter) url += `&report_type=${typeFilter}`;
            const res = await authFetch(url);
            const data = await res.json();
            if (data.status === "success") setReports(data.reports || []);
        } catch (e) {
            console.error("获取报告列表失败", e);
        } finally {
            setLoading(false);
        }
    };

    // 独立获取全部报告（客户视角用，不受筛选器影响）
    const fetchAllReports = async () => {
        try {
            let url = "/api/reports/all?limit=50";
            if (currentBrandId) url += `&brand_id=${currentBrandId}`;
            const res = await authFetch(url);
            const data = await res.json();
            if (data.status === "success") setAllReports(data.reports || []);
        } catch (e) {
            console.error("获取全部报告失败", e);
        }
    };

    useEffect(() => { fetchReports(); }, [statusFilter, typeFilter, currentBrandId]);
    useEffect(() => { fetchAllReports(); }, [currentBrandId]);

    const updateStatus = async (reportId: number, newStatus: string) => {
        try {
            const res = await authFetch(`/api/reports/${reportId}/status`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ status: newStatus, reviewed_by: "admin" })
            });
            const data = await res.json();
            if (data.status === "success") {
                fetchReports();
                fetchAllReports();
                if (viewReport?.id === reportId) {
                    setViewReport({ ...viewReport, status: newStatus });
                }
                // [CTO-15.3 2026-04-21] 加 toast 确认,避免老板"我发了但客户没看到"的疑虑
                if (newStatus === "sent") toast.success("报告已发送给客户,客户门户即可查看");
                else if (newStatus === "reviewed") toast.success("已标记为审核通过");
            } else {
                toast.error(data.message || "更新失败");
            }
        } catch (e) {
            console.error("更新状态失败", e);
            toast.error("网络错误,请稍后重试");
        }
    };

    const saveContent = async () => {
        if (!viewReport) return;
        setSaving(true);
        try {
            const res = await authFetch(`/api/reports/${viewReport.id}/content`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ content: editContent })
            });
            const data = await res.json();
            if (data.status === "success") {
                setEditMode(false);
                setViewReport({ ...viewReport, content: editContent });
                fetchReports();
            }
        } catch (e) { console.error("保存失败", e); }
        finally { setSaving(false); }
    };

    const handleExportPdf = async (reportId: number) => {
        try {
            const res = await authFetch(`/api/reports/${reportId}/export/pdf`);
            if (!res.ok) {
                toast.error(`导出失败: HTTP ${res.status}`);
                return;
            }
            const blob = await res.blob();
            const isPdf = blob.type === 'application/pdf';
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `report_${reportId}.${isPdf ? 'pdf' : 'html'}`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            toast.success(isPdf ? '报告 PDF 已下载' : '报告已下载，用浏览器打开后 Ctrl+P 可导出 PDF');
        } catch (e) {
            console.error('导出报告失败', e);
            toast.error('导出失败，请稍后重试');
        }
    };

    const handleCopyLink = (report: Report) => {
        // [CTO-15.3 2026-04-21] 修: 原复制 API URL(需 auth 客户打不开) · 老板反馈"应是客户门户链接"
        //   查 brand 最新 quote 的 portal token → 复制 /portal/:token 客户可直接登录看报告
        // [WO_WHITELABEL_COPY_UX 项2 2026-08-05] 复制必须在手势同步栈发起(iOS/微信 webview)
        void copyAsyncText(async () => {
            const res = await authFetch(`/api/portal/tokens/by-brand/${report.brand_id}`);
            const data = await res.json();
            if (data.status !== 'success' || !data.token?.token) {
                throw new Error(data.error || '生成客户门户链接失败,请先在监测页生成客户 Token');
            }
            return `${window.location.origin}/portal/${data.token.token}`;
        }).then(({ ok, text, errorMessage }) => {
            if (ok) {
                setCopied(true);
                setTimeout(() => setCopied(false), 2000);
                toast.success('客户门户链接已复制,可发给客户查看所有已发送报告');
            } else if (text) {
                setManualCopyText(text);
            } else {
                toast.error(errorMessage || '生成客户门户链接失败,请稍后重试');
            }
        });
    };

    const handleAiWrite = async () => {
        if (!viewReport) return;
        setAiWriting(true);
        try {
            const res = await authFetch(`/api/reports/${viewReport.id}/ai-write`, { method: "POST" });
            const data = await res.json();
            if (data.status === "success" && data.content) {
                setViewReport({ ...viewReport, content: data.content });
                setEditContent(data.content);
                setEditMode(true);
            } else {
                toast.error(data.error || "AI生成失败");
            }
        } catch (e) {
            console.error("AI写报告失败", e);
            toast.error("AI写报告失败，请稍后重试");
        } finally {
            setAiWriting(false);
        }
    };

    const deleteReport = async (reportId: number) => {
        if (!(await askConfirm({ title: "确定要删除这份报告吗？此操作不可撤销。", danger: true }))) return;
        try {
            const res = await authFetch(`/api/reports/${reportId}`, { method: "DELETE" });
            const data = await res.json();
            if (data.status === "success") {
                fetchReports();
                fetchAllReports();
                if (viewReport?.id === reportId) setViewReport(null);
            }
        } catch (e) { console.error("删除失败", e); }
    };

    const getRate = (s: SummaryData) => s.detection_rate ?? s.avg_detection_rate ?? 0;

    // ===== 报告详情视图 =====
    if (viewReport) {
        const s = viewReport.summary_data || {};
        const rate = getRate(s);
        const statusInfo = STATUS_MAP[viewReport.status] || STATUS_MAP.draft;
        const keywords = s.keyword_stats || [];
        const topKw = s.top_keywords || keywords.slice(0, 3);
        const bottomKw = s.bottom_keywords || keywords.slice(-3);

        // 生成可读报告文本
        const generateReadableContent = () => {
            let text = `# AI搜索可见度监测报告\n\n`;
            text += `**报告类型**: ${TYPE_MAP[viewReport.report_type] || viewReport.report_type}\n`;
            text += `**监测周期**: ${viewReport.period_start} ~ ${viewReport.period_end}\n`;
            text += `**整体检出率**: ${rate.toFixed(1)}%\n`;
            text += `**监测关键词**: ${s.total_keywords || keywords.length} 个\n`;
            text += `**监测次数**: ${s.task_count || 0} 次\n\n`;
            text += `## 关键词表现\n\n`;
            if (keywords.length > 0) {
                text += `| 关键词 | 平均检出率 | 测试次数 |\n|---|---|---|\n`;
                keywords.forEach(k => {
                    text += `| ${k.keyword} | ${k.avg_rate.toFixed(1)}% | ${k.tests_count} |\n`;
                });
            }
            if (topKw.length > 0) {
                text += `\n### 表现最佳\n`;
                topKw.forEach(k => { text += `- ${k.keyword}: ${k.avg_rate.toFixed(1)}%\n`; });
            }
            if (bottomKw.length > 0) {
                text += `\n### 需要关注\n`;
                bottomKw.forEach(k => { text += `- ${k.keyword}: ${k.avg_rate.toFixed(1)}%\n`; });
            }
            return text;
        };

        return (
            <div className="p-6 space-y-6 max-w-5xl mx-auto">
                {/* 返回 + 操作栏 */}
                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                    <div className="flex items-center gap-2">
                        <Button variant="ghost" size="sm" onClick={() => { setViewReport(null); setEditMode(false); }}>
                            <ArrowLeft className="h-4 w-4 mr-1" /> 返回列表
                        </Button>
                        <Badge className={cn("border shrink-0 whitespace-nowrap", statusInfo.badgeColor)}>
                            <statusInfo.icon className="h-3 w-3 mr-1" />
                            {statusInfo.label}
                        </Badge>
                    </div>
                    <div className="flex items-center gap-2 flex-wrap">
                        {/* [CTO-15.3 2026-04-21] 老板反馈: 周报保存后客户看不到,根因 draft→reviewed→sent 2 步操作易漏第 2 步
                            方案 A: draft 直接"发送给客户"一键 sent,跳过 reviewed 中间态
                            reviewed 状态保留给历史数据 + 未来审核流程 flag 开启时用 */}
                        {(viewReport.status === "draft" || viewReport.status === "reviewed") && (
                            <Button size="sm" onClick={() => updateStatus(viewReport.id, "sent")}>
                                <Send className="h-4 w-4 mr-1" /> 发送给客户
                            </Button>
                        )}
                        <Button size="sm" variant="outline" onClick={() => handleExportPdf(viewReport.id)}>
                            <Download className="h-4 w-4 mr-1" /> 导出报告
                        </Button>
                        <Button size="sm" variant="outline" onClick={() => handleCopyLink(viewReport)}>
                            {copied ? <CheckCircle className="h-4 w-4 mr-1 text-green-500" /> : <Copy className="h-4 w-4 mr-1" />}
                            {copied ? "已复制" : "复制链接"}
                        </Button>
                        <Button size="sm" variant="outline" className="text-red-500 border-red-200 hover:bg-red-50"
                            onClick={() => deleteReport(viewReport.id)}>
                            <Trash2 className="h-4 w-4 mr-1" /> 删除
                        </Button>
                    </div>
                </div>

                {/* 报告头部 */}
                <Card>
                    <CardContent className="pt-6">
                        <div className="text-center mb-6">
                            <h2 className="text-2xl font-bold">AI搜索可见度监测报告</h2>
                            <p className="text-muted-foreground mt-1">
                                {TYPE_MAP[viewReport.report_type] || viewReport.report_type} · {viewReport.period_start} ~ {viewReport.period_end}
                            </p>
                            {s.tier_name && (
                                <p className="text-sm mt-1">
                                    <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-blue-100 text-blue-700 text-xs font-medium">
                                        {s.tier_name} · 目标检出率 ≥{s.target_rate}%
                                    </span>
                                </p>
                            )}
                        </div>

                        {/* 核心指标卡片 */}
                        <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4 mb-6">
                            <div className={cn("rounded-lg border p-4 text-center", getRateBg(rate))}>
                                <div className={cn("text-3xl font-bold", getRateColor(rate))}>
                                    {rate.toFixed(1)}%
                                </div>
                                <div className="text-sm text-muted-foreground mt-1">整体检出率</div>
                            </div>
                            {s.target_rate != null && (
                                <div className={cn("rounded-lg border p-4 text-center",
                                    rate >= s.target_rate ? "bg-green-50 border-green-200" : "bg-amber-50 border-amber-200"
                                )}>
                                    <div className={cn("text-3xl font-bold",
                                        rate >= s.target_rate ? "text-green-600" : "text-amber-600"
                                    )}>
                                        {s.target_rate}%
                                    </div>
                                    <div className="text-sm text-muted-foreground mt-1">
                                        {rate >= s.target_rate ? "目标已达成" : `差距 ${(s.target_rate - rate).toFixed(1)}%`}
                                    </div>
                                </div>
                            )}
                            <div className="rounded-lg border border-border bg-muted p-4 text-center">
                                <div className="text-3xl font-bold text-foreground">
                                    {s.total_keywords || keywords.length}
                                </div>
                                <div className="text-sm text-muted-foreground mt-1">监测关键词</div>
                            </div>
                            <div className="rounded-lg border border-border bg-muted p-4 text-center">
                                <div className="text-3xl font-bold text-foreground">
                                    {s.total_tests || 0}
                                </div>
                                <div className="text-sm text-muted-foreground mt-1">总测试数</div>
                            </div>
                            <div className="rounded-lg border border-border bg-muted p-4 text-center">
                                <div className="text-3xl font-bold text-foreground">
                                    {s.task_count || 0}
                                </div>
                                <div className="text-sm text-muted-foreground mt-1">监测任务数</div>
                            </div>
                        </div>
                    </CardContent>
                </Card>

                {/* 关键词明细 */}
                {keywords.length > 0 && (
                    <Card>
                        <CardHeader>
                            <CardTitle className="flex items-center gap-2">
                                <BarChart3 className="h-5 w-5" /> 关键词表现明细
                            </CardTitle>
                        </CardHeader>
                        <CardContent>
                            <div className="space-y-2">
                                {keywords.map((kw, i) => (
                                    <div key={i} className="flex flex-col sm:flex-row sm:items-center sm:justify-between p-3 rounded-lg bg-muted hover:bg-muted/80 transition-colors gap-2">
                                        <div className="flex items-center gap-3">
                                            <span className="text-xs text-muted-foreground w-6">{i + 1}</span>
                                            <span className="font-medium">{kw.keyword}</span>
                                            {kw.target_brand && (
                                                <span className="text-xs text-muted-foreground">({kw.target_brand})</span>
                                            )}
                                        </div>
                                        <div className="flex flex-wrap items-center gap-2 sm:gap-4">
                                            <span className="text-sm text-muted-foreground">{kw.tests_count} 次测试</span>
                                            <div className="w-20 sm:w-32 bg-muted rounded-full h-2">
                                                <div
                                                    className={cn("h-2 rounded-full", kw.avg_rate >= 60 ? "bg-green-500" : kw.avg_rate >= 30 ? "bg-yellow-500" : "bg-red-500")}
                                                    style={{ width: `${Math.min(kw.avg_rate, 100)}%` }}
                                                />
                                            </div>
                                            <span className={cn("font-bold text-sm w-16 text-right", getRateColor(kw.avg_rate))}>
                                                {kw.avg_rate.toFixed(1)}%
                                            </span>
                                        </div>
                                    </div>
                                ))}
                            </div>
                        </CardContent>
                    </Card>
                )}

                {/* 表现最佳/需要关注 */}
                {(topKw.length > 0 || bottomKw.length > 0) && (
                    <div className="grid grid-cols-2 gap-4">
                        {topKw.length > 0 && (
                            <Card>
                                <CardHeader className="pb-3">
                                    <CardTitle className="text-sm flex items-center gap-2 text-green-600">
                                        <TrendingUp className="h-4 w-4" /> 表现最佳
                                    </CardTitle>
                                </CardHeader>
                                <CardContent>
                                    {topKw.map((kw, i) => (
                                        <div key={i} className="flex justify-between py-1.5 text-sm">
                                            <span>{kw.keyword}</span>
                                            <span className="font-medium text-green-600">{kw.avg_rate.toFixed(1)}%</span>
                                        </div>
                                    ))}
                                </CardContent>
                            </Card>
                        )}
                        {bottomKw.length > 0 && (
                            <Card>
                                <CardHeader className="pb-3">
                                    <CardTitle className="text-sm flex items-center gap-2 text-red-600">
                                        <TrendingDown className="h-4 w-4" /> 需要关注
                                    </CardTitle>
                                </CardHeader>
                                <CardContent>
                                    {bottomKw.map((kw, i) => (
                                        <div key={i} className="flex justify-between py-1.5 text-sm">
                                            <span>{kw.keyword}</span>
                                            <span className="font-medium text-red-600">{kw.avg_rate.toFixed(1)}%</span>
                                        </div>
                                    ))}
                                </CardContent>
                            </Card>
                        )}
                    </div>
                )}

                {/* 报告备注/自定义内容 */}
                <Card>
                    <CardHeader>
                        <div className="flex items-center justify-between">
                            <CardTitle className="text-sm">报告备注</CardTitle>
                            <div className="flex gap-2">
                                {!editMode ? (
                                    <>
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            onClick={handleAiWrite}
                                            disabled={aiWriting}
                                            className="text-purple-600 border-purple-200 hover:bg-purple-50"
                                        >
                                            {aiWriting ? (
                                                <><Loader2 className="h-4 w-4 mr-1 animate-spin" /> AI 生成中...</>
                                            ) : (
                                                <><Sparkles className="h-4 w-4 mr-1" /> AI 写报告</>
                                            )}
                                        </Button>
                                        <Button variant="ghost" size="sm" onClick={() => {
                                            setEditMode(true);
                                            setEditContent(viewReport.content || generateReadableContent());
                                        }}>
                                            <Edit className="h-4 w-4 mr-1" /> 编辑
                                        </Button>
                                    </>
                                ) : (
                                    <>
                                        <Button variant="ghost" size="sm" onClick={() => setEditMode(false)}>取消</Button>
                                        <Button size="sm" onClick={saveContent} disabled={saving}>
                                            {saving && <Loader2 className="h-4 w-4 mr-1 animate-spin" />}
                                            保存
                                        </Button>
                                    </>
                                )}
                            </div>
                        </div>
                    </CardHeader>
                    <CardContent>
                        {editMode ? (
                            <textarea
                                className="w-full h-64 border border-border rounded-lg p-4 text-sm resize-none focus:outline-hidden focus:ring-2 focus:ring-brand"
                                value={editContent}
                                onChange={(e) => setEditContent(e.target.value)}
                                placeholder="在此添加报告备注或修改内容..."
                            />
                        ) : viewReport.content ? (
                            <div className="prose prose-sm dark:prose-invert max-w-none text-sm leading-relaxed text-foreground min-h-[60px]">
                                <ReactMarkdown>
                                    {viewReport.content}
                                </ReactMarkdown>
                            </div>
                        ) : (
                            <div className="text-sm text-muted-foreground min-h-[60px]">
                                暂无备注，点击编辑添加
                            </div>
                        )}
                    </CardContent>
                </Card>

                {/* 报告元信息 */}
                <div className="text-xs text-muted-foreground text-center space-x-4">
                    <span>生成时间: {new Date(viewReport.created_at).toLocaleString('zh-CN')}</span>
                    {viewReport.reviewed_at && <span>审核时间: {new Date(viewReport.reviewed_at).toLocaleString('zh-CN')}</span>}
                    {viewReport.sent_at && <span>发送时间: {new Date(viewReport.sent_at).toLocaleString('zh-CN')}</span>}
                </div>
            </div>
        );
    }

    // 客户视角的已发送报告（基于全量数据，不受筛选器影响）
    const sentReports = allReports.filter(r => r.status === "sent");
    // 未发送的报告数（用于客户视角提示）
    const pendingCount = allReports.filter(r => r.status === "draft").length;
    const reviewedCount = allReports.filter(r => r.status === "reviewed").length;

    // ===== 报告列表视图 =====
    return (
        <div className="p-6 space-y-6">
            {/* CTO-15.20 桥接 banner */}
            <BridgeBanner />
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-3">
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                        <FileText className="h-5 w-5 text-brand" />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-foreground">报告管理</h1>
                        <p className="text-sm text-muted-foreground">审核、查看并发送监测报告给客户</p>
                    </div>
                </div>
                <Button variant="outline" onClick={fetchReports} disabled={loading}>
                    <RefreshCw className={cn("h-4 w-4 mr-2", loading && "animate-spin")} /> 刷新
                </Button>
            </div>

            {/* 视图切换标签 */}
            <div className="flex border-b">
                <button
                    className={cn(
                        "flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px",
                        viewTab === "manage"
                            ? "border-blue-500 text-blue-600"
                            : "border-transparent text-muted-foreground hover:text-foreground hover:border-border"
                    )}
                    onClick={() => setViewTab("manage")}
                >
                    <Settings2 className="h-4 w-4" /> 管理视图
                    <Badge variant="secondary" className="text-xs ml-1">{reports.length}</Badge>
                </button>
                <button
                    className={cn(
                        "flex items-center gap-2 px-4 py-2.5 text-sm font-medium border-b-2 transition-colors -mb-px",
                        viewTab === "client"
                            ? "border-blue-500 text-blue-600"
                            : "border-transparent text-muted-foreground hover:text-foreground hover:border-border"
                    )}
                    onClick={() => setViewTab("client")}
                >
                    <Monitor className="h-4 w-4" /> 客户视角
                    <Badge variant="secondary" className="text-xs ml-1">{sentReports.length}</Badge>
                </button>
            </div>

            {viewTab === "manage" ? (
                <>
                    {/* 筛选器 */}
                    <Card>
                        <CardContent className="p-4">
                            <div className="flex items-center gap-4 flex-wrap">
                                <Filter className="h-4 w-4 text-muted-foreground" />
                                <div className="flex gap-2">
                                    {[
                                        { key: "", label: "全部状态" },
                                        { key: "draft", label: "草稿" },
                                        { key: "reviewed", label: "已审核" },
                                        { key: "sent", label: "已发送" },
                                    ].map(f => (
                                        <Button key={f.key} variant={statusFilter === f.key ? "default" : "outline"} size="sm"
                                            onClick={() => setStatusFilter(f.key)}>
                                            {f.label}
                                        </Button>
                                    ))}
                                </div>
                                <div className="border-l pl-4 flex gap-2">
                                    <Button variant={typeFilter === "" ? "secondary" : "ghost"} size="sm" onClick={() => setTypeFilter("")}>
                                        全部类型
                                    </Button>
                                    {Object.entries(TYPE_MAP).map(([key, label]) => (
                                        <Button key={key} variant={typeFilter === key ? "secondary" : "ghost"} size="sm"
                                            onClick={() => setTypeFilter(key)}>
                                            {label}
                                        </Button>
                                    ))}
                                </div>
                            </div>
                        </CardContent>
                    </Card>

                    {/* 报告列表 */}
                    {loading ? (
                        <Card className="border border-border rounded-xl shadow-none p-12 text-center">
                            <Loader2 className="h-6 w-6 animate-spin mx-auto text-brand" />
                        </Card>
                    ) : reports.length === 0 ? (
                        <Card className="border border-border rounded-xl shadow-none p-12 text-center">
                            <FileText className="h-10 w-10 text-muted-foreground/30 mx-auto mb-3" />
                            <p className="text-muted-foreground">暂无报告</p>
                        </Card>
                    ) : (
                        <div className="grid gap-3">
                            {reports.map((report) => {
                                const statusInfo = STATUS_MAP[report.status] || STATUS_MAP.draft;
                                const StatusIcon = statusInfo.icon;
                                const rate = getRate(report.summary_data || {});
                                const kwCount = report.summary_data?.total_keywords || report.summary_data?.keyword_stats?.length || 0;

                                return (
                                    <Card
                                        key={report.id}
                                        className="border border-border rounded-xl shadow-none hover:bg-muted/30 transition-colors cursor-pointer"
                                        onClick={() => setViewReport(report)}
                                    >
                                        <CardContent className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 py-4">
                                            <div className="flex items-center gap-3">
                                                <div className={cn(
                                                    "w-12 h-12 sm:w-14 sm:h-14 rounded-full flex items-center justify-center text-white font-bold text-xs sm:text-sm shrink-0",
                                                    rate >= 80 ? "bg-green-500" : rate >= 60 ? "bg-blue-500" : rate >= 40 ? "bg-yellow-500" : "bg-red-500"
                                                )}>
                                                    {rate.toFixed(0)}%
                                                </div>
                                                <div className="min-w-0">
                                                    <div className="flex items-center gap-2 flex-wrap">
                                                        <Badge className={cn("border text-xs shrink-0 whitespace-nowrap", statusInfo.badgeColor)}>
                                                            <StatusIcon className="h-3 w-3 mr-1" />
                                                            {statusInfo.label}
                                                        </Badge>
                                                        <span className="font-semibold whitespace-nowrap">
                                                            {TYPE_MAP[report.report_type] || report.report_type}
                                                        </span>
                                                        <span className="text-sm text-muted-foreground whitespace-nowrap">
                                                            {report.period_start} ~ {report.period_end}
                                                        </span>
                                                    </div>
                                                    <div className="text-xs text-muted-foreground mt-1">
                                                        {kwCount} 个关键词 · {report.summary_data?.task_count || 0} 次任务 · 创建于 {new Date(report.created_at).toLocaleDateString('zh-CN')}
                                                    </div>
                                                </div>
                                            </div>
                                            <div className="flex items-center gap-2 shrink-0" onClick={e => e.stopPropagation()}>
                                                <Button size="sm" variant="ghost" onClick={() => setViewReport(report)}>
                                                    <Eye className="h-4 w-4 mr-1" /> 查看
                                                </Button>
                                                {/* [CTO-15.3 2026-04-21] 方案 A: draft 一键"发送给客户" 跳 reviewed · 对齐详情页 */}
                                                {(report.status === "draft" || report.status === "reviewed") && (
                                                    <Button size="sm" onClick={() => updateStatus(report.id, "sent")}>
                                                        <Send className="h-4 w-4 mr-1" /> 发送给客户
                                                    </Button>
                                                )}
                                                <Button size="sm" variant="ghost" className="text-red-500 hover:text-red-700 hover:bg-red-50"
                                                    onClick={() => deleteReport(report.id)}>
                                                    <Trash2 className="h-4 w-4" />
                                                </Button>
                                            </div>
                                        </CardContent>
                                    </Card>
                                );
                            })}
                        </div>
                    )}

                    <div className="text-center text-sm text-muted-foreground">
                        共 {reports.length} 份报告
                    </div>
                </>
            ) : (
                /* ===== 客户视角 ===== */
                <>
                    <Card className="border-blue-200 bg-blue-50/50">
                        <CardContent className="p-4">
                            <div className="flex items-center gap-2 text-sm text-blue-700">
                                <Monitor className="h-4 w-4" />
                                此视图展示客户在门户中看到的报告。只有「已发送」状态的报告才会显示在客户端。
                            </div>
                        </CardContent>
                    </Card>

                    {loading ? (
                        <Card className="border border-border rounded-xl shadow-none p-12 text-center">
                            <Loader2 className="h-6 w-6 animate-spin mx-auto text-brand" />
                        </Card>
                    ) : sentReports.length === 0 ? (
                        <Card className="border border-border rounded-xl shadow-none p-12 text-center">
                            <Monitor className="h-10 w-10 text-muted-foreground/30 mx-auto mb-3" />
                            <p className="font-medium text-muted-foreground">暂无已发送报告</p>
                            <p className="text-sm text-muted-foreground mt-1">将报告状态设为「已发送」后，客户即可在门户中查看</p>
                        </Card>
                    ) : (
                        <Card className="bg-card border border-border rounded-xl shadow-none">
                            <CardHeader className="pb-3">
                                <CardTitle className="flex items-center gap-2 text-lg">
                                    <FileText className="h-5 w-5 text-blue-500" />
                                    报告中心
                                    <Badge className="bg-blue-100 text-blue-700 border-blue-200 text-xs ml-1">
                                        {sentReports.length} 份
                                    </Badge>
                                </CardTitle>
                            </CardHeader>
                            <CardContent>
                                <div className="space-y-3">
                                    {sentReports.map(report => {
                                        const rate = getRate(report.summary_data || {});
                                        const label = report.summary_data?.period_label || `${report.period_start} ~ ${report.period_end}`;
                                        return (
                                            <div
                                                key={report.id}
                                                className={cn(
                                                    "flex justify-between items-center p-4 rounded-lg cursor-pointer hover:bg-muted/30 transition-all",
                                                    report.report_type === 'monthly'
                                                        ? 'bg-linear-to-r from-blue-50 to-indigo-50 border border-blue-200'
                                                        : 'bg-muted border border-border'
                                                )}
                                                onClick={() => setViewReport(report)}
                                            >
                                                <div>
                                                    <div className="flex items-center gap-2">
                                                        <Badge className={
                                                            report.report_type === 'monthly'
                                                                ? 'bg-linear-to-r from-[#2B4C7E] to-indigo-600 text-white'
                                                                : ''
                                                        } variant={report.report_type === 'weekly' ? 'default' : undefined}>
                                                            {TYPE_MAP[report.report_type] || report.report_type}
                                                        </Badge>
                                                        <span className="font-medium">{label}</span>
                                                    </div>
                                                    <div className="text-xs text-muted-foreground mt-1.5 flex flex-wrap items-center gap-2 sm:gap-3">
                                                        <span>平均出现率: <strong className={getRateColor(rate)}>{rate.toFixed(1)}%</strong></span>
                                                        <span>词条数: {report.summary_data?.total_keywords || 0}</span>
                                                    </div>
                                                </div>
                                                <div className="flex items-center gap-2 sm:gap-3 shrink-0">
                                                    <span className="text-xs text-muted-foreground">
                                                        {report.sent_at ? new Date(report.sent_at).toLocaleDateString('zh-CN') : report.created_at?.split('T')[0] || '-'}
                                                    </span>
                                                    <Button size="sm" variant="ghost" onClick={(e) => { e.stopPropagation(); setViewReport(report); }}>
                                                        <Eye className="h-4 w-4" />
                                                    </Button>
                                                </div>
                                            </div>
                                        );
                                    })}
                                </div>
                            </CardContent>
                        </Card>
                    )}

                    {/* 快捷操作提示 */}
                    {(pendingCount > 0 || reviewedCount > 0) && (
                        <Card className="border-amber-200 bg-amber-50/50">
                            <CardContent className="p-4">
                                <div className="text-sm text-amber-700">
                                    <strong>提示：</strong>
                                    有 {pendingCount + reviewedCount} 份报告尚未发送给客户
                                    {pendingCount > 0 && reviewedCount > 0 && `(${pendingCount} 份草稿 + ${reviewedCount} 份已审核)`}
                                    。切换到「管理视图」点"发送给客户"即可让客户看到。
                                </div>
                            </CardContent>
                        </Card>
                    )}
                </>
            )}

            <ManualCopyDialog text={manualCopyText} onClose={() => setManualCopyText(null)} />
          {confirmDialog}
        </div>
    );
}
