import { useState, useEffect, useRef, useCallback } from 'react';
import { authFetch } from '@/lib/api';
import { useOssAttribution } from '@/hooks/useOssAttribution';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import {
    Upload, Sparkles, ExternalLink, Loader2,
    FileUp, ChevronDown, ChevronUp, Database, ShieldCheck, FileDown
} from 'lucide-react';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface PlacementRecommendationProps {
    quoteId: number;
    brandName: string;
}

type BudgetTier = 'budget' | 'balanced' | 'comprehensive';

interface PlatformRecommendation {
    name: string;
    price: number;
    engines: string[];
    reason: string;
    geoConfirmed?: boolean;
}

interface ArticleRecommendation {
    id: number;
    title: string;
    keyword: string;
    status: string;
    platforms: PlatformRecommendation[];
    estimatedCost: number;
    engineCoverage: number;
}

interface PlacementStats {
    totalOutlets: number;
    geoConfirmedCount: number;
}

interface AnalysisLogEntry {
    id: number;
    date: string;
    fileName: string;
    newCount: number;
    updatedCount: number;
    discoveries: string[];
}

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

// [2026-07-22 sink census F49/H5] 打印链 document.write 插值统一转义:
// 文章标题/媒体名/推荐理由/品牌名均为 DB 文本,未转义进 HTML 即注入面。
function escapeHtml(value: unknown): string {
    return String(value ?? '').replace(/[&<>"']/g, (ch) => (
        { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch] as string
    ));
}

const BUDGET_OPTIONS: { key: BudgetTier; label: string; desc: string }[] = [
    { key: 'budget', label: '试投控费', desc: '优先参考低预算媒体，控制单篇成本' },
    { key: 'balanced', label: '稳妥推荐', desc: '兼顾预算、行业样本与AI引擎覆盖面' },
    { key: 'comprehensive', label: '权威增强', desc: '预算较高时优先参考权威和高引用资源' },
];

const ENGINE_COLORS: Record<string, string> = {
    '豆包': 'bg-blue-100 text-blue-700 border-blue-200',
    'DeepSeek': 'bg-green-100 text-green-700 border-green-200',
    '千问': 'bg-purple-100 text-purple-700 border-purple-200',
    '文心一言': 'bg-red-100 text-red-700 border-red-200',
    'Kimi': 'bg-orange-100 text-orange-700 border-orange-200',
    '腾讯元宝': 'bg-teal-100 text-teal-700 border-teal-200',
};

const ACCEPTED_FILE_TYPES = '.csv,.xlsx,.zip';

// ---------------------------------------------------------------------------
// Helper: AI engine badge
// ---------------------------------------------------------------------------

function EngineBadge({ name }: { name: string }) {
    const colorClass = ENGINE_COLORS[name] || 'bg-muted text-muted-foreground border-border';
    return (
        <span
            className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium ${colorClass}`}
        >
            {name}
        </span>
    );
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function PlacementRecommendation({ quoteId, brandName }: PlacementRecommendationProps) {
    // WO_329 开源版署名位:打印清单页脚用(开关关 ⇒ null ⇒ 页脚与改动前一致);先取好,点打印时同步用
    const ossAttribution = useOssAttribution();
    // State
    const [articles, setArticles] = useState<ArticleRecommendation[]>([]);
    const [stats, setStats] = useState<PlacementStats | null>(null);
    const [generating, setGenerating] = useState(false);
    const [budget, setBudget] = useState<BudgetTier>('budget');
    const [uploading, setUploading] = useState(false);
    const [uploadProgress, setUploadProgress] = useState(0);
    const [logs, setLogs] = useState<AnalysisLogEntry[]>([]);
    const [showUpload, setShowUpload] = useState(false);
    const [expandedCards, setExpandedCards] = useState<Set<number>>(new Set());
    const [dragOver, setDragOver] = useState(false);
    const fileInputRef = useRef<HTMLInputElement>(null);

    // -------------------------------------------------------------------
    // Data fetching
    // -------------------------------------------------------------------

    const loadArticles = useCallback(async () => {
        try {
            const res = await authFetch(`/api/placement/articles/${quoteId}`);
            if (res.ok) {
                const data = await res.json();
                const raw = data.articles ?? data ?? [];
                const mapped: ArticleRecommendation[] = raw.map((art: any) => {
                    const outlets = art.recommendation?.outlets ?? [];
                    const platforms: PlatformRecommendation[] = outlets.map((o: any) => ({
                        name: o.outlet_name || o.platform || o.name || '',
                        price: o.price ?? 0,
                        engines: o.ai_engines ?? [],
                        reason: o.match_reason ?? '',
                        geoConfirmed: !!o.geo_confirmed,
                    }));
                    const uniqueEngines = new Set(platforms.flatMap(p => p.engines));
                    return {
                        id: art.topic_id ?? art.id,
                        title: art.title ?? art.optimized_title ?? '',
                        keyword: art.keyword ?? '',
                        status: art.status ?? '',
                        platforms,
                        estimatedCost: art.recommendation?.total_cost ?? 0,
                        engineCoverage: uniqueEngines.size,
                    };
                });
                setArticles(mapped);
            }
        } catch (err) {
            console.error('Failed to load placement articles:', err);
        }
    }, [quoteId]);

    const loadStats = useCallback(async () => {
        try {
            const res = await authFetch('/api/placement/stats');
            if (res.ok) {
                const data = await res.json();
                setStats({
                    totalOutlets: data.total_outlets ?? 0,
                    geoConfirmedCount: data.geo_confirmed ?? 0,
                });
            }
        } catch (err) {
            console.error('Failed to load placement stats:', err);
        }
    }, []);

    const loadLogs = useCallback(async () => {
        try {
            const res = await authFetch('/api/placement/analysis-log');
            if (res.ok) {
                const data = await res.json();
                const raw = data.logs ?? data ?? [];
                const mapped: AnalysisLogEntry[] = raw.map((log: any) => ({
                    id: log.id ?? log.task_id ?? 0,
                    date: log.created_at ?? log.date ?? '',
                    fileName: log.file_name ?? log.fileName ?? '',
                    newCount: log.new_outlets_count ?? log.newCount ?? 0,
                    updatedCount: log.updated_outlets_count ?? log.updatedCount ?? 0,
                    discoveries: log.discoveries ?? [],
                }));
                setLogs(mapped);
            }
        } catch (err) {
            console.error('Failed to load analysis logs:', err);
        }
    }, []);

    useEffect(() => {
        loadArticles();
        loadStats();
        loadLogs();
    }, [loadArticles, loadStats, loadLogs]);

    // -------------------------------------------------------------------
    // Actions
    // -------------------------------------------------------------------

    const handleGenerate = async () => {
        setGenerating(true);
        try {
            const res = await authFetch(`/api/placement/generate/${quoteId}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ budget }),
            });
            if (res.ok) {
                await loadArticles();
            }
        } catch (err) {
            console.error('Failed to generate recommendations:', err);
        } finally {
            setGenerating(false);
        }
    };

    const handleExportPDF = () => {
        if (articles.length === 0) return;

        const tierLabel = BUDGET_OPTIONS.find(o => o.key === budget)?.label ?? budget;
        const now = new Date().toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' });

        // Build rows for each article with expanded platform details.
        const articleRows = articles.map((art, artIdx) => {
            const platformRows = (art.platforms ?? []).map((p, pIdx) => `
                <tr>
                    ${pIdx === 0 ? `<td rowspan="${art.platforms.length}" style="vertical-align:top;font-weight:600;padding:8px 10px;border:1px solid #ddd;background:#fafafa;width:30px;text-align:center">${artIdx + 1}</td>
                    <td rowspan="${art.platforms.length}" style="vertical-align:top;padding:8px 10px;border:1px solid #ddd;background:#fafafa">
                        <div style="font-weight:600;margin-bottom:2px">${escapeHtml(art.title)}</div>
                        <div style="font-size:11px;color:#888">${escapeHtml(art.keyword)}</div>
                    </td>` : ''}
                    <td style="padding:6px 10px;border:1px solid #ddd">${escapeHtml(p.name)}${p.geoConfirmed ? ' <span style="color:#16a34a;font-size:10px;border:1px solid #bbf7d0;border-radius:3px;padding:0 3px;background:#f0fdf4">GEO</span>' : ''}</td>
                    <td style="padding:6px 10px;border:1px solid #ddd;text-align:right;white-space:nowrap">${escapeHtml(p.price)}元</td>
                    <td style="padding:6px 10px;border:1px solid #ddd">${escapeHtml((p.engines ?? []).join('、'))}</td>
                    <td style="padding:6px 10px;border:1px solid #ddd;color:#666;font-size:12px">${escapeHtml(p.reason)}</td>
                    ${pIdx === 0 ? `<td rowspan="${art.platforms.length}" style="vertical-align:top;padding:8px 10px;border:1px solid #ddd;text-align:right;font-weight:600;white-space:nowrap">¥${escapeHtml(art.estimatedCost)}</td>` : ''}
                </tr>
            `).join('');
            return platformRows;
        }).join('');

        const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>投放清单 - ${escapeHtml(brandName)}</title>
<style>
  @page { size: A4 landscape; margin: 15mm; }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: "Microsoft YaHei","PingFang SC","Helvetica Neue",Arial,sans-serif; color: #222; font-size: 13px; line-height: 1.5; }
  .header { display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 2px solid #333; padding-bottom: 10px; margin-bottom: 16px; }
  .header h1 { font-size: 20px; font-weight: 700; }
  .header .meta { text-align: right; font-size: 12px; color: #666; }
  .summary { display: flex; gap: 24px; margin-bottom: 14px; font-size: 13px; }
  .summary .item { display: flex; align-items: center; gap: 4px; }
  .summary .val { font-weight: 700; font-size: 15px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  thead th { background: #f3f4f6; font-weight: 600; padding: 8px 10px; border: 1px solid #ddd; text-align: left; white-space: nowrap; }
  .footer { margin-top: 16px; padding-top: 10px; border-top: 1px solid #ddd; display: flex; justify-content: space-between; font-size: 11px; color: #999; }
  @media print { body { -webkit-print-color-adjust: exact; print-color-adjust: exact; } }
</style>
</head>
<body>
<div class="header">
  <div>
    <h1>${escapeHtml(brandName)} — GEO投放清单</h1>
    <div style="font-size:12px;color:#888;margin-top:2px">投放策略: ${escapeHtml(tierLabel)}</div>
  </div>
  <div class="meta">
    <div>生成日期: ${escapeHtml(now)}</div>
    <div>报价单ID: ${escapeHtml(quoteId)}</div>
  </div>
</div>

<div class="summary">
  <div class="item">文章数 <span class="val">${articles.length}</span></div>
  <div class="item">预估总成本 <span class="val" style="color:#2563eb">¥${escapeHtml(totalEstimatedCost)}</span></div>
  <div class="item">覆盖引擎 <span class="val">${allEngines.size}</span></div>
</div>

<table>
<thead>
  <tr>
    <th style="width:30px">#</th>
    <th style="min-width:200px">文章标题</th>
    <th>推荐媒体</th>
    <th style="width:60px;text-align:right">价格</th>
    <th>AI引擎覆盖</th>
    <th>推荐理由</th>
    <th style="width:70px;text-align:right">小计</th>
  </tr>
</thead>
<tbody>
  ${articleRows}
</tbody>
<tfoot>
  <tr>
    <td colspan="6" style="padding:8px 10px;border:1px solid #ddd;text-align:right;font-weight:600">合计</td>
    <td style="padding:8px 10px;border:1px solid #ddd;text-align:right;font-weight:700;font-size:14px;color:#2563eb">¥${escapeHtml(totalEstimatedCost)}</td>
  </tr>
</tfoot>
</table>

<div class="footer">
  <span>GEO智能投放系统自动生成</span>
  <span>本清单为优先投放建议，请结合预算和执行情况确认</span>
</div>${ossAttribution ? `<p class="oss-attribution" style="margin-top:8px;font-size:11px;color:#6b7280;text-align:center"><a href="${escapeHtml(ossAttribution.href)}" style="color:#6b7280;text-decoration:none">${escapeHtml(ossAttribution.report_text ?? ossAttribution.text)}</a></p>` : ''}

<script>window.onload=function(){window.print()}</script>
</body>
</html>`;

        const printWin = window.open('', '_blank');
        if (printWin) {
            printWin.document.write(html);
            printWin.document.close();
        }
    };

    const handleFileUpload = async (file: File) => {
        setUploading(true);
        setUploadProgress(0);
        try {
            const formData = new FormData();
            formData.append('file', file);
            const progressInterval = setInterval(() => {
                setUploadProgress(prev => Math.min(prev + 10, 90));
            }, 300);
            const res = await authFetch('/api/placement/upload-data', {
                method: 'POST',
                body: formData,
            });
            clearInterval(progressInterval);
            setUploadProgress(100);
            if (res.ok) {
                await loadStats();
                await loadLogs();
            }
        } catch (err) {
            console.error('Failed to upload media data:', err);
        } finally {
            setTimeout(() => {
                setUploading(false);
                setUploadProgress(0);
            }, 600);
        }
    };

    const handleFileDrop = (e: React.DragEvent<HTMLDivElement>) => {
        e.preventDefault();
        setDragOver(false);
        const file = e.dataTransfer.files[0];
        if (file) handleFileUpload(file);
    };

    const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (file) handleFileUpload(file);
        e.target.value = '';
    };

    const toggleCardExpand = (id: number) => {
        setExpandedCards(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id); else next.add(id);
            return next;
        });
    };

    // -------------------------------------------------------------------
    // Computed
    // -------------------------------------------------------------------

    const totalEstimatedCost = articles.reduce((sum, a) => sum + (a.estimatedCost || 0), 0);
    const allEngines = new Set(articles.flatMap(a => a.platforms?.flatMap(p => p.engines) ?? []));

    // -------------------------------------------------------------------
    // Render
    // -------------------------------------------------------------------

    return (
        <div className="space-y-5">
            {/* ============================================================ */}
            {/* Toolbar Card                                                 */}
            {/* ============================================================ */}
            <Card className="border border-border rounded-xl">
                <CardContent className="py-4 space-y-4">
                    {/* Row 1: Budget selector + Generate button */}
                    <div className="flex items-center gap-3 flex-wrap">
                        <span className="text-sm font-medium text-muted-foreground shrink-0">投放策略</span>
                        <div className="flex rounded-lg border bg-muted/40 p-0.5">
                            {BUDGET_OPTIONS.map(opt => (
                                <button
                                    key={opt.key}
                                    onClick={() => setBudget(opt.key)}
                                    title={opt.desc}
                                    className={`px-4 py-1.5 text-sm font-medium rounded-md transition-all ${
                                        budget === opt.key
                                            ? 'bg-card text-foreground shadow-xs'
                                            : 'text-muted-foreground hover:text-foreground'
                                    }`}
                                >
                                    {opt.label}
                                </button>
                            ))}
                        </div>
                        <Button onClick={handleGenerate} disabled={generating} className="ml-auto">
                            {generating ? (
                                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                            ) : (
                                <Sparkles className="h-4 w-4 mr-2" />
                            )}
                            生成投放建议
                        </Button>
                    </div>

                    {/* Row 2: Stats + secondary actions */}
                    <div className="flex items-center justify-between flex-wrap gap-2 sm:gap-3 pt-2 border-t">
                        <div className="flex flex-wrap items-center gap-2 sm:gap-5 text-sm">
                            <div className="flex items-center gap-1.5">
                                <Database className="h-4 w-4 text-muted-foreground" />
                                <span className="text-muted-foreground">媒体库</span>
                                <span className="font-semibold tabular-nums">{stats?.totalOutlets?.toLocaleString() ?? '—'}</span>
                            </div>
                            <div className="flex items-center gap-1.5">
                                <ShieldCheck className="h-4 w-4 text-green-600" />
                                <span className="text-muted-foreground">GEO收录</span>
                                <span className="font-semibold tabular-nums text-green-700">{stats?.geoConfirmedCount ?? 0}</span>
                            </div>
                            {articles.length > 0 && (
                                <>
                                    <div className="w-px h-4 bg-border" />
                                    <span className="text-muted-foreground">
                                        {articles.length} 篇文章
                                    </span>
                                    <span className="text-muted-foreground">
                                        预估 <span className="font-semibold text-foreground tabular-nums">¥{totalEstimatedCost}</span>
                                    </span>
                                    <span className="text-muted-foreground">
                                        覆盖 <span className="font-semibold text-foreground">{allEngines.size}</span> 引擎
                                    </span>
                                </>
                            )}
                        </div>
                        <div className="flex items-center gap-2">
                            <Button variant="outline" size="sm" onClick={handleExportPDF} disabled={articles.length === 0}>
                                <FileDown className="h-3.5 w-3.5 mr-1.5" />
                                导出PDF
                            </Button>
                            <Button variant="outline" size="sm" onClick={() => setShowUpload(!showUpload)}>
                                <Upload className="h-3.5 w-3.5 mr-1.5" />
                                上传数据
                            </Button>
                        </div>
                    </div>
                </CardContent>
            </Card>

            {/* ============================================================ */}
            {/* Generating indicator                                         */}
            {/* ============================================================ */}
            {generating && (
                <Card className="border border-blue-200 bg-blue-50/50 rounded-xl">
                    <CardContent className="py-8 flex flex-col items-center gap-3">
                        <Loader2 className="h-8 w-8 animate-spin text-brand" />
                        <p className="text-blue-700 font-medium">AI 正在根据文章内容、行业样本和当前媒体资源生成优先投放建议...</p>
                        <p className="text-sm text-blue-500">
                            品牌: {brandName} | 策略: {BUDGET_OPTIONS.find(o => o.key === budget)?.label}
                        </p>
                    </CardContent>
                </Card>
            )}

            {/* ============================================================ */}
            {/* Empty State                                                  */}
            {/* ============================================================ */}
            {!generating && articles.length === 0 && (
                <Card className="border border-border rounded-xl">
                    <CardContent className="py-16 text-center">
                        <Sparkles className="h-12 w-12 text-muted-foreground/40 mx-auto mb-4" />
                        <h3 className="text-lg font-medium mb-2">尚无投放建议</h3>
                        <p className="text-muted-foreground mb-4">
                            选择投放策略后点击"生成投放建议"，AI 将根据已完成文章、行业样本和当前媒体资源给出优先投放建议
                        </p>
                    </CardContent>
                </Card>
            )}

            {/* ============================================================ */}
            {/* Article Recommendation Cards                                 */}
            {/* ============================================================ */}
            {!generating && articles.length > 0 && (
                <div className="space-y-3">
                    {articles.map(article => {
                        const isExpanded = expandedCards.has(article.id);
                        const hasPlatforms = article.platforms?.length > 0;
                        const visiblePlatforms = isExpanded
                            ? article.platforms
                            : article.platforms?.slice(0, 3);
                        const hasMore = article.platforms?.length > 3;

                        return (
                            <Card key={article.id} className="overflow-hidden border border-border rounded-xl">
                                {/* Article header */}
                                <div
                                    className="flex flex-wrap items-center gap-2 sm:gap-3 px-3 sm:px-5 py-3 cursor-pointer hover:bg-muted/30 transition-colors"
                                    onClick={() => hasMore && toggleCardExpand(article.id)}
                                >
                                    <Badge variant="secondary" className="text-xs shrink-0 font-normal">
                                        {article.keyword}
                                    </Badge>
                                    <h4 className="text-sm font-medium flex-1 truncate">
                                        {article.title}
                                    </h4>
                                    {hasPlatforms && (
                                        <div className="flex items-center gap-3 shrink-0 text-xs text-muted-foreground">
                                            <span className="tabular-nums">¥{article.estimatedCost}</span>
                                            <span>{article.engineCoverage}引擎</span>
                                        </div>
                                    )}
                                    {hasMore && (
                                        <Button variant="ghost" size="icon" className="h-6 w-6 shrink-0">
                                            {isExpanded
                                                ? <ChevronUp className="h-3.5 w-3.5" />
                                                : <ChevronDown className="h-3.5 w-3.5" />
                                            }
                                        </Button>
                                    )}
                                </div>

                                {/* Platform recommendations */}
                                {hasPlatforms && (
                                    <div className="border-t">
                                        <table className="w-full text-sm">
                                            <thead>
                                                <tr className="border-b bg-muted/30">
                                                    <th className="text-left font-medium text-muted-foreground pl-5 pr-2 py-2 w-8">#</th>
                                                    <th className="text-left font-medium text-muted-foreground px-2 py-2">媒体</th>
                                                    <th className="text-right font-medium text-muted-foreground px-2 py-2 w-20">价格</th>
                                                    <th className="text-left font-medium text-muted-foreground px-2 py-2">AI引擎覆盖</th>
                                                    <th className="text-left font-medium text-muted-foreground px-2 pr-5 py-2">推荐理由</th>
                                                </tr>
                                            </thead>
                                            <tbody>
                                                {visiblePlatforms?.map((platform, idx) => (
                                                    <tr key={idx} className="border-b last:border-b-0 hover:bg-muted/20">
                                                        <td className="pl-5 pr-2 py-2.5 text-muted-foreground tabular-nums">{idx + 1}</td>
                                                        <td className="px-2 py-2.5">
                                                            <div className="flex items-center gap-1.5">
                                                                <span className="font-medium">{platform.name}</span>
                                                                {platform.geoConfirmed && (
                                                                    <Badge className="bg-green-100 text-green-700 border-green-200 hover:bg-green-100 text-[10px] px-1 py-0 leading-tight">
                                                                        GEO
                                                                    </Badge>
                                                                )}
                                                            </div>
                                                        </td>
                                                        <td className="px-2 py-2.5 text-right tabular-nums">{platform.price}元</td>
                                                        <td className="px-2 py-2.5">
                                                            <div className="flex items-center gap-1 flex-wrap">
                                                                {platform.engines?.map(engine => (
                                                                    <EngineBadge key={engine} name={engine} />
                                                                ))}
                                                            </div>
                                                        </td>
                                                        <td className="px-2 pr-5 py-2.5 text-muted-foreground text-xs max-w-[240px] truncate">
                                                            {platform.reason}
                                                        </td>
                                                    </tr>
                                                ))}
                                            </tbody>
                                        </table>
                                        {hasMore && !isExpanded && (
                                            <div
                                                className="text-center py-1.5 text-xs text-muted-foreground hover:text-foreground cursor-pointer hover:bg-muted/30 transition-colors border-t"
                                                onClick={() => toggleCardExpand(article.id)}
                                            >
                                                展开全部 {article.platforms.length} 个平台
                                            </div>
                                        )}
                                    </div>
                                )}
                            </Card>
                        );
                    })}
                </div>
            )}

            {/* ============================================================ */}
            {/* Upload Section                                               */}
            {/* ============================================================ */}
            {showUpload && (
                <Card className="border border-border rounded-xl">
                    <CardHeader className="pb-3">
                        <CardTitle className="flex items-center gap-2 text-base">
                            <FileUp className="h-4 w-4" />
                            上传媒体数据
                        </CardTitle>
                        <CardDescription>
                            上传媒体价格表、收录数据等，系统将自动解析并更新知识库
                        </CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <div
                            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
                            onDragLeave={() => setDragOver(false)}
                            onDrop={handleFileDrop}
                            onClick={() => fileInputRef.current?.click()}
                            className={`relative flex flex-col items-center justify-center rounded-lg border-2 border-dashed p-6 cursor-pointer transition-colors ${
                                dragOver
                                    ? 'border-primary bg-primary/5'
                                    : 'border-muted-foreground/25 hover:border-primary/50 hover:bg-muted/30'
                            }`}
                        >
                            <input
                                ref={fileInputRef}
                                type="file"
                                accept={ACCEPTED_FILE_TYPES}
                                className="hidden"
                                onChange={handleFileSelect}
                            />
                            <Upload className="h-8 w-8 text-muted-foreground/50 mb-2" />
                            <p className="text-sm font-medium">拖拽文件到此处或点击选择</p>
                            <p className="text-xs text-muted-foreground mt-1">
                                支持 .csv, .xlsx, .zip 格式
                            </p>
                        </div>

                        {uploading && (
                            <div className="space-y-2">
                                <div className="flex items-center justify-between text-sm">
                                    <span className="flex items-center gap-2">
                                        <Loader2 className="h-4 w-4 animate-spin" />
                                        正在上传并解析...
                                    </span>
                                    <span>{uploadProgress}%</span>
                                </div>
                                <Progress value={uploadProgress} className="h-2" />
                            </div>
                        )}
                    </CardContent>
                </Card>
            )}

            {/* Analysis log */}
            {showUpload && logs.length > 0 && (
                <Card className="border border-border rounded-xl">
                    <CardHeader className="pb-3">
                        <CardTitle className="text-base">解析日志</CardTitle>
                    </CardHeader>
                    <CardContent>
                        <div className="relative pl-6 space-y-4">
                            <div className="absolute left-2 top-1 bottom-1 w-px bg-border" />
                            {logs.map(log => (
                                <div key={log.id} className="relative">
                                    <div className="absolute -left-6 top-1.5 h-2.5 w-2.5 rounded-full border-2 border-primary bg-background" />
                                    <div className="space-y-1">
                                        <div className="flex items-center gap-3 text-sm">
                                            <span className="font-medium">{log.date}</span>
                                            <span className="text-muted-foreground">{log.fileName}</span>
                                        </div>
                                        <div className="flex items-center gap-2 text-xs text-muted-foreground">
                                            <Badge variant="outline" className="text-[10px]">
                                                新增 {log.newCount}
                                            </Badge>
                                            <Badge variant="outline" className="text-[10px]">
                                                更新 {log.updatedCount}
                                            </Badge>
                                        </div>
                                        {log.discoveries?.length > 0 && (
                                            <ul className="text-xs text-muted-foreground space-y-0.5 mt-1">
                                                {log.discoveries.map((d, i) => (
                                                    <li key={i} className="flex items-start gap-1.5">
                                                        <ExternalLink className="h-3 w-3 mt-0.5 shrink-0" />
                                                        <span>{d}</span>
                                                    </li>
                                                ))}
                                            </ul>
                                        )}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </CardContent>
                </Card>
            )}
        </div>
    );
}
