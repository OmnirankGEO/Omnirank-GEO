import { useState, useEffect } from "react";
import { readLevelMeta, levelBadgeClass } from '@/lib/defensiveGeoPresentation';
import { toast } from 'sonner';
import { useClientContext } from '@/context/ClientContext';
import { useIsMounted } from '@/hooks/useIsMounted';
import { Card } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
    AlertDialog,
    AlertDialogAction,
    AlertDialogCancel,
    AlertDialogContent,
    AlertDialogDescription,
    AlertDialogFooter,
    AlertDialogHeader,
    AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { cn } from "@/lib/utils";
import { Loader2, Trash2, RefreshCw, History } from "lucide-react";
import { useNavigate, Link } from "react-router-dom";
import { historyApi, type DiagnosisRecord } from "@/lib/api";
import { useIsMobile } from "@/hooks/useIsMobile";
import { categoryKeyFromRaw, industryCategoryText, useIndustryTaxonomy } from "@/lib/industryTaxonomy";

export function HistoryList() {
    const navigate = useNavigate();
    const isMobile = useIsMobile();
    const isMounted = useIsMounted();
    // Phase 07 (CTO-15.23 2026-05-04) · 客户切换在 sidebar 顶部 ClientSwitcher · 此页不再内嵌
    // (Phase 06.2 加的 Select 已 rollback · 因为重复造轮子)
    const { currentBrandId, clientContext } = useClientContext();
    const [history, setHistory] = useState<DiagnosisRecord[]>([]);
    const [loading, setLoading] = useState(true);
    const [searchQuery, setSearchQuery] = useState("");
    const [industryFilter, setIndustryFilter] = useState("all");
    /*
     * [WO_267] 行业筛选改用后端那张大类字典(原来本页手写 11 个值,与库里的值、与字典都对不上)。
     * 🔴 显示:自由文本 `industry` 优先(原行为);没有才用大类名,英文 key 一律不原样上屏。
     * 🔴 匹配:按大类 key。后端附了 `industry_category_key` 就用;新写入的原值本身就是 key;
     *    存量旧中文名只在它恰好等于字典里某个大类名时能反查到 key(其余存量只在「所有行业」下可见)。
     */
    const { taxonomy, nameOf: industryNameOf } = useIndustryTaxonomy();
    const industryTextOf = (item: DiagnosisRecord) =>
        item.industry || industryCategoryText(item.industry_category, item.industry_category_name, industryNameOf) || "-";
    const categoryKeyOf = (item: DiagnosisRecord) =>
        item.industry_category_key || categoryKeyFromRaw(item.industry_category, taxonomy);
    const [scopeFilter, setScopeFilter] = useState("all");
    const [deleting, setDeleting] = useState<number | null>(null);

    // 删除确认弹窗状态
    const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
    const [recordToDelete, setRecordToDelete] = useState<DiagnosisRecord | null>(null);

    const fetchHistory = async (brandId?: number, brandName?: string) => {
        try {
            const params: Record<string, any> = { limit: 100, days: 90 };
            // 优先用 brand_id 精确过滤，兜底用 brand_name
            if (brandId) params.brand_id = brandId;
            else if (brandName) params.brand = brandName;
            const res = await historyApi.list(params);
            if (!isMounted()) return;  // 组件卸载/已切换过滤条件，丢弃本次结果
            setHistory(res.data);
        } catch (e) {
            console.error("Failed to fetch history:", e);
        } finally {
            if (isMounted()) setLoading(false);
        }
    };

    useEffect(() => {
        setLoading(true);
        fetchHistory(currentBrandId || undefined, clientContext?.brand?.name || '');
    }, [currentBrandId, clientContext]);

    // 监听诊断完成事件：自动刷新历史列表，避免用户回到页面时看到过期数据
    useEffect(() => {
        const handler = () => {
            fetchHistory(currentBrandId || undefined, clientContext?.brand?.name || '');
        };
        window.addEventListener('diagnosis:completed', handler as EventListener);
        return () => window.removeEventListener('diagnosis:completed', handler as EventListener);
    }, [currentBrandId, clientContext]);

    // 打开删除确认弹窗
    const openDeleteDialog = (record: DiagnosisRecord) => {
        setRecordToDelete(record);
        setDeleteDialogOpen(true);
    };

    // 确认删除
    const handleConfirmDelete = async () => {
        if (!recordToDelete) return;

        const id = recordToDelete.id;
        setDeleteDialogOpen(false);
        setDeleting(id);

        try {
            await historyApi.delete(id);
            // 从列表中移除
            setHistory(prev => prev.filter(item => item.id !== id));
        } catch (e) {
            console.error("删除失败:", e);
            toast.error("删除失败，请重试");
        } finally {
            setDeleting(null);
            setRecordToDelete(null);
        }
    };

    // 筛选
    const filteredHistory = history.filter(item => {
        const matchesSearch = !searchQuery ||
            item.brand_name?.toLowerCase().includes(searchQuery.toLowerCase());
        const matchesIndustry = industryFilter === "all" ||
            categoryKeyOf(item) === industryFilter ||
            (!!item.industry && item.industry === industryNameOf(industryFilter));
        const matchesScope = scopeFilter === "all" ||
            (scopeFilter === "legacy"
                ? (item.diagnosis_type === 'sales_lite' || (!item.diagnosis_type))
                : item.diagnosis_type === scopeFilter);
        return matchesSearch && matchesIndustry && matchesScope;
    });

    // [CUR-03 2026-08-21] 🔴 这是全站**第三套**等级口径(BrandList 一套按分数、
    // 后端 scoring_levels.py 一套、这里一套按字符串 includes 判色)。
    // 三套并存 ⇒ 同一个诊断在列表、详情、历史三处可能显示不同颜色。
    // 规格 §9.1「前端只渲染服务端版本化 DTO,不计算等级」——
    // 改为读服务端 level_meta;拿不到就「暂无结论」,不按字符串猜。
    const levelOf = (item: unknown) => readLevelMeta((item as { level_meta?: unknown } | null)?.level_meta);

    const getScoreColor = (score: number, maxScore: number = 100) => {
        const pct = maxScore > 0 ? score / maxScore : 0;
        if (pct >= 0.8) return 'bg-green-50 text-green-700 border border-green-200';
        if (pct >= 0.6) return 'bg-blue-50 text-blue-700 border border-blue-200';
        if (pct >= 0.4) return 'bg-yellow-50 text-yellow-700 border border-yellow-200';
        return 'bg-red-50 text-red-700 border border-red-200';
    };

    return (
        <div className="p-4 md:p-6 space-y-6">
            <div className="flex items-center gap-4">
                <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                    <History className="h-5 w-5 text-brand" />
                </div>
                <div>
                    <h2 className="text-2xl font-bold text-foreground">历史记录</h2>
                    <p className="text-muted-foreground">查看过往的所有诊断记录</p>
                </div>
            </div>

            <div className="flex flex-col sm:flex-row gap-3 sm:gap-4">
                <Input
                    placeholder="搜索品牌..."
                    className="sm:max-w-sm focus:ring-brand"
                    value={searchQuery}
                    onChange={(e) => setSearchQuery(e.target.value)}
                />
                <div className="flex gap-3 items-center">
                    <Select value={scopeFilter} onValueChange={setScopeFilter}>
                        <SelectTrigger className="w-[130px] sm:w-[150px]">
                            <SelectValue placeholder="所有类型" />
                        </SelectTrigger>
                        <SelectContent>
                            <SelectItem value="all">所有类型</SelectItem>
                            <SelectItem value="geo">GEO诊断</SelectItem>
                            <SelectItem value="legacy">历史记录</SelectItem>
                        </SelectContent>
                    </Select>
                    <Select value={industryFilter} onValueChange={setIndustryFilter}>
                        <SelectTrigger className="w-[140px] sm:w-[180px]">
                            <SelectValue placeholder="所有行业" />
                        </SelectTrigger>
                        <SelectContent>
                            <SelectItem value="all">所有行业</SelectItem>
                            {(taxonomy?.categories || []).map(c => (
                                <SelectItem key={c.key} value={c.key}>
                                    {c.name}
                                </SelectItem>
                            ))}
                        </SelectContent>
                    </Select>
                    <span className="text-sm text-muted-foreground whitespace-nowrap">
                        共 {filteredHistory.length} 条记录
                    </span>
                </div>
            </div>

            {/* 移动端: 卡片列表 */}
            {isMobile && (
                <div className="space-y-3">
                    {loading ? (
                        <div className="text-center py-8"><Loader2 className="h-6 w-6 animate-spin mx-auto text-brand" /></div>
                    ) : filteredHistory.length === 0 ? (
                        <div className="text-center py-12">
                            <History className="h-10 w-10 text-muted-foreground/30 mx-auto mb-3" />
                            <p className="text-muted-foreground">暂无诊断记录</p>
                        </div>
                    ) : filteredHistory.map((item) => (
                        <Card key={item.id} className="border border-border rounded-xl shadow-none p-4">
                            <div className="flex justify-between items-start mb-1">
                                <Link to={`/diagnosis/report/${item.id}`} className="font-semibold text-base hover:text-blue-600 transition-colors leading-snug flex-1 min-w-0 mr-2">
                                    {item.brand_name || "(未命名)"}
                                </Link>
                                <span className="text-xs text-muted-foreground whitespace-nowrap">#{item.id}</span>
                            </div>
                            <div className="text-sm text-muted-foreground mb-2.5">{industryTextOf(item)}</div>
                            <div className="flex gap-2 flex-wrap mb-3">
                                <Badge className={getScoreColor(item.total_score || 0, 100)}>
                                    {item.total_score ?? "-"}/100
                                </Badge>
                                <Badge className={levelBadgeClass(levelOf(item))}>{levelOf(item).label}</Badge>
                                <Badge variant="secondary" className={
                                    item.diagnosis_type === 'geo'
                                        ? 'bg-blue-50 text-blue-700 border border-blue-200'
                                        : 'bg-amber-50 text-amber-700 border border-amber-200'
                                }>
                                    {item.diagnosis_type === 'geo' ? 'GEO' : '历史'}
                                </Badge>
                            </div>
                            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                                <span className="text-xs text-muted-foreground">{item.created_at?.slice(0, 10)}</span>
                                <div className="flex gap-2 flex-wrap">
                                    <Button variant="outline" size="sm" onClick={() => navigate(`/diagnosis/report/${item.id}`)}>查看报告</Button>
                                    <Button size="sm" className="text-green-600 bg-green-50 hover:bg-green-100 border-0" onClick={() => {
                                        const params = new URLSearchParams({ retest: '1', brand_name: item.brand_name || '', industry: item.industry || '', original_id: String(item.id), original_score: String(item.total_score || 0) });
                                        navigate(`/diagnosis/new?${params.toString()}`);
                                    }}>
                                        <RefreshCw className="h-3.5 w-3.5 mr-1" />复测
                                    </Button>
                                    <Button variant="destructive" size="sm" onClick={() => openDeleteDialog(item)} disabled={deleting === item.id}>
                                        {deleting === item.id ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                                    </Button>
                                </div>
                            </div>
                        </Card>
                    ))}
                </div>
            )}

            {/* 桌面端: 表格 */}
            <Card className={cn("border border-border rounded-xl shadow-none", isMobile && "hidden")}>
                <Table>
                    <TableHeader className="sticky top-0 z-10">
                        <TableRow className="bg-muted">
                            <TableHead className="text-muted-foreground">ID</TableHead>
                            <TableHead className="text-muted-foreground">品牌名称</TableHead>
                            <TableHead className="text-muted-foreground">行业</TableHead>
                            <TableHead className="text-muted-foreground">评分</TableHead>
                            <TableHead className="text-muted-foreground">等级</TableHead>
                            <TableHead className="text-muted-foreground">版本</TableHead>
                            <TableHead className="text-muted-foreground">诊断时间</TableHead>
                            <TableHead className="text-right text-muted-foreground">操作</TableHead>
                        </TableRow>
                    </TableHeader>
                    <TableBody>
                        {loading ? (
                            <TableRow>
                                <TableCell colSpan={8} className="text-center py-8">
                                    <Loader2 className="h-6 w-6 animate-spin mx-auto text-brand" />
                                </TableCell>
                            </TableRow>
                        ) : filteredHistory.length === 0 ? (
                            <TableRow>
                                <TableCell colSpan={8} className="text-center py-12">
                                    <History className="h-10 w-10 text-muted-foreground/30 mx-auto mb-3" />
                                    <p className="text-muted-foreground">暂无诊断记录</p>
                                </TableCell>
                            </TableRow>
                        ) : (
                            filteredHistory.map((item) => (
                                <TableRow key={item.id} className="hover:bg-muted/50 transition-colors">
                                    <TableCell className="text-muted-foreground">#{item.id}</TableCell>
                                    <TableCell className="font-medium">
                                        <Link
                                            to={`/diagnosis/report/${item.id}`}
                                            className="hover:text-blue-600 transition-colors"
                                        >
                                            {item.brand_name || "(未命名)"}
                                        </Link>
                                    </TableCell>
                                    <TableCell>{industryTextOf(item)}</TableCell>
                                    <TableCell>
                                        <Badge className={getScoreColor(item.total_score || 0, 100)}>
                                            {item.total_score ?? "-"}/100
                                        </Badge>
                                    </TableCell>
                                    <TableCell>
                                        <Badge className={levelBadgeClass(levelOf(item))}>
                                            {levelOf(item).label}
                                        </Badge>
                                    </TableCell>
                                    <TableCell>
                                        <Badge variant="secondary" className={
                                            item.diagnosis_type === 'geo'
                                                ? 'bg-blue-50 text-blue-700 border border-blue-200 hover:bg-blue-50'
                                                : 'bg-amber-50 text-amber-700 border border-amber-200 hover:bg-amber-50'
                                        }>
                                            {item.diagnosis_type === 'geo' ? 'GEO' : '历史'}
                                        </Badge>
                                    </TableCell>
                                    <TableCell>{item.created_at?.slice(0, 10)}</TableCell>
                                    <TableCell className="text-right space-x-2">
                                        <Button
                                            variant="ghost"
                                            size="sm"
                                            className="text-green-600 hover:text-green-700 hover:bg-green-50"
                                            onClick={() => {
                                                // 复测：跳转到新建诊断页面，预填客户信息
                                                const params = new URLSearchParams({
                                                    retest: '1',
                                                    brand_name: item.brand_name || '',
                                                    industry: item.industry || '',
                                                    original_id: String(item.id),
                                                    original_score: String(item.total_score || 0),
                                                });
                                                navigate(`/diagnosis/new?${params.toString()}`);
                                            }}
                                            title="对该客户进行复测"
                                        >
                                            <RefreshCw className="h-4 w-4 mr-1" />
                                            复测
                                        </Button>
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            onClick={() => navigate(`/diagnosis/report/${item.id}`)}
                                        >
                                            查看报告
                                        </Button>
                                        <Button
                                            size="sm"
                                            onClick={() => navigate(`/articles?diagnosis_id=${item.id}`)}
                                        >
                                            写作中心
                                        </Button>
                                        <Button
                                            variant="destructive"
                                            size="sm"
                                            onClick={() => openDeleteDialog(item)}
                                            disabled={deleting === item.id}
                                        >
                                            {deleting === item.id ? (
                                                <Loader2 className="h-4 w-4 animate-spin" />
                                            ) : (
                                                <Trash2 className="h-4 w-4" />
                                            )}
                                        </Button>
                                    </TableCell>
                                </TableRow>
                            ))
                        )}
                    </TableBody>
                </Table>
            </Card>

            {/* 删除确认弹窗 */}
            <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>⚠️ 确认删除</AlertDialogTitle>
                        <AlertDialogDescription className="space-y-2">
                            <p>您确定要删除以下诊断记录吗？</p>
                            {recordToDelete && (
                                <div className="bg-muted p-3 rounded-md mt-2">
                                    <p><strong>记录ID:</strong> #{recordToDelete.id}</p>
                                    <p><strong>品牌名称:</strong> {recordToDelete.brand_name || "(未命名)"}</p>
                                    <p><strong>行业:</strong> {industryTextOf(recordToDelete)}</p>
                                    <p><strong>评分:</strong> {recordToDelete.total_score ?? "-"}</p>
                                </div>
                            )}
                            <p className="text-red-600 font-medium mt-2">
                                此操作不可撤销，相关数据将被永久删除！
                            </p>
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>取消</AlertDialogCancel>
                        <AlertDialogAction
                            onClick={handleConfirmDelete}
                            className="bg-red-600 hover:bg-red-700"
                        >
                            确认删除
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
}
