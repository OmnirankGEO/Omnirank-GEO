import { useEffect, useState, useMemo } from "react";
import { readLevelMeta, levelBadgeClass, levelDotClass } from '@/lib/defensiveGeoPresentation';
import { useEmbeddedNavigate } from '@/hooks/useEmbeddedNavigate';
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
    Search, Building2, TrendingUp, Plus, ArrowRight, Archive, RotateCcw,
    Trash2, Loader2, Merge, X, Check,
} from "lucide-react";
import { toast } from 'sonner';
import api, { brandsApi, type Brand } from "@/lib/api";
import { useClientContext } from "@/context/ClientContext";
import {
    AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
    AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import {
    Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter,
} from "@/components/ui/dialog";

export function BrandList() {
    const navigate = useEmbeddedNavigate();
    const { refreshClients } = useClientContext();

    const [brands, setBrands] = useState<Brand[]>([]);
    const [loading, setLoading] = useState(true);
    const [search, setSearch] = useState("");
    const [statusTab, setStatusTab] = useState<'active' | 'archived'>('active');
    const [error, setError] = useState<string | null>(null);
    const [processing, setProcessing] = useState<number | null>(null);

    // 删除相关
    const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
    const [deleteTarget, setDeleteTarget] = useState<Brand | null>(null);
    const [deletePassword, setDeletePassword] = useState("");
    const [deleteError, setDeleteError] = useState("");

    // 合并相关
    const [mergeMode, setMergeMode] = useState(false);
    const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
    const [mergeDialogOpen, setMergeDialogOpen] = useState(false);
    const [primaryBrandId, setPrimaryBrandId] = useState<number | null>(null);
    const [mergeDisplayName, setMergeDisplayName] = useState("");
    const [mergePassword, setMergePassword] = useState("");
    const [mergeError, setMergeError] = useState("");
    const [merging, setMerging] = useState(false);

    useEffect(() => {
        fetchBrands();
    }, []);

    const fetchBrands = async (searchTerm?: string) => {
        setLoading(true);
        try {
            const res = await brandsApi.list({ search: searchTerm, limit: 200 });
            setBrands(res.data.data || []);
            setError(null);
        } catch (err: any) {
            console.error("Failed to fetch brands:", err);
            setError(err.message || "加载失败");
        } finally {
            setLoading(false);
        }
    };

    const handleSearch = (e: React.FormEvent) => {
        e.preventDefault();
        fetchBrands(search);
    };

    const filteredBrands = useMemo(() => {
        return brands.filter(b => (b.status || 'active') === statusTab);
    }, [brands, statusTab]);

    const activeCnt = useMemo(() => brands.filter(b => (b.status || 'active') === 'active').length, [brands]);
    const archivedCnt = useMemo(() => brands.filter(b => b.status === 'archived').length, [brands]);

    // [CUR-03 2026-08-21] 🔴 前端**不再自己算等级和颜色**。
    //
    // 改造前这里是一套独立阈值(领先>=81 成熟>=61 成长>=41 起步>=21),
    // 与后端 SSOT `tools/scoring/scoring_levels.py`(领先>=85 成熟70-84
    // 成长55-69 起步40-54 待提升20-39 空白0-19)**逐档不一致**:
    // 阈值全错、领先/成熟两档颜色互换、整个「待提升」档缺失。
    // 一个 82 分的品牌在本页显示「领先」,在别处是「成熟」。
    // `HistoryList.tsx` 还有第三套(按字符串 includes 判色)。
    //
    // 规格 §9.1「前端只渲染服务端版本化 DTO,不计算 outcome、等级」。
    // 所以等级一律读服务端下发的 `level_meta`;拿不到就显示「暂无结论」——
    // **不用分数反推**,反推正是这条债务的病根。
    const levelOf = (brand: Brand) => readLevelMeta((brand as { level_meta?: unknown }).level_meta);

    // 社媒操盘手开关
    const handleToggleSocial = async (brand: Brand, e: React.MouseEvent) => {
        e.stopPropagation();
        const newVal = !brand.social_enabled;
        setProcessing(brand.id);
        try {
            const res = await api.patch(`/api/brands/${brand.id}/social`, { social_enabled: newVal });
            if (res.data.status === 'success') {
                setBrands(prev => prev.map(b => b.id === brand.id ? { ...b, social_enabled: newVal } : b));
            } else {
                toast.error(res.data.error || '操作失败');
            }
        } catch {
            toast.error('请求失败');
        } finally {
            setProcessing(null);
        }
    };

    // 归档/激活
    const handleToggleArchive = async (brand: Brand, e: React.MouseEvent) => {
        e.stopPropagation();
        const newStatus = (brand.status || 'active') === 'active' ? 'archived' : 'active';
        setProcessing(brand.id);
        try {
            const res = await api.patch(`/api/brands/${brand.id}/status`, { status: newStatus });
            const data = res.data;
            if (data.status === 'success') {
                setBrands(prev => prev.map(b => b.id === brand.id ? { ...b, status: newStatus } : b));
                refreshClients();
            } else {
                toast.error(data.error || '操作失败');
            }
        } catch {
            toast.error('请求失败');
        } finally {
            setProcessing(null);
        }
    };

    // 删除（需要密码）
    const openDeleteDialog = (brand: Brand, e: React.MouseEvent) => {
        e.stopPropagation();
        setDeleteTarget(brand);
        setDeletePassword("");
        setDeleteError("");
        setDeleteDialogOpen(true);
    };

    const handleConfirmDelete = async () => {
        if (!deleteTarget) return;
        if (!deletePassword) {
            setDeleteError("请输入密码");
            return;
        }
        setProcessing(deleteTarget.id);
        try {
            const res = await brandsApi.delete(deleteTarget.id, deletePassword);
            if (res.data.status === 'success') {
                setBrands(prev => prev.filter(b => b.id !== deleteTarget.id));
                refreshClients();
                setDeleteDialogOpen(false);
                setDeleteTarget(null);
            } else {
                setDeleteError((res.data as any).error || res.data.message || "删除失败");
            }
        } catch (err: any) {
            const msg = err.response?.data?.error || "删除失败，请重试";
            setDeleteError(msg);
        } finally {
            setProcessing(null);
        }
    };

    // ===== 合并相关 =====
    const toggleSelect = (id: number, e: React.MouseEvent) => {
        e.stopPropagation();
        setSelectedIds(prev => {
            const next = new Set(prev);
            if (next.has(id)) next.delete(id);
            else next.add(id);
            return next;
        });
    };

    const exitMergeMode = () => {
        setMergeMode(false);
        setSelectedIds(new Set());
    };

    const openMergeDialog = () => {
        if (selectedIds.size < 2) {
            toast("请至少选择2个客户进行合并");
            return;
        }
        const selected = brands.filter(b => selectedIds.has(b.id));
        setPrimaryBrandId(selected[0]?.id || null);
        setMergeDisplayName(selected[0]?.name || "");
        setMergePassword("");
        setMergeError("");
        setMergeDialogOpen(true);
    };

    const selectedBrands = useMemo(() => {
        return brands.filter(b => selectedIds.has(b.id));
    }, [brands, selectedIds]);

    const handleConfirmMerge = async () => {
        if (!primaryBrandId) {
            setMergeError("请选择主品牌");
            return;
        }
        if (!mergePassword) {
            setMergeError("请输入密码");
            return;
        }
        const mergeIds = [...selectedIds].filter(id => id !== primaryBrandId);
        if (mergeIds.length === 0) {
            setMergeError("至少选择一个待合并品牌");
            return;
        }
        setMerging(true);
        setMergeError("");
        try {
            const res = await brandsApi.merge({
                primary_brand_id: primaryBrandId,
                merge_brand_ids: mergeIds,
                display_name: mergeDisplayName || undefined,
                password: mergePassword,
            });
            if (res.data.status === 'success') {
                setMergeDialogOpen(false);
                exitMergeMode();
                refreshClients();
                fetchBrands();
            } else {
                setMergeError(res.data.message || "合并失败");
            }
        } catch (err: any) {
            setMergeError(err.response?.data?.error || "合并失败，请重试");
        } finally {
            setMerging(false);
        }
    };

    return (
        <div className="p-4 md:p-6 space-y-6">
            {/* 页头 */}
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3">
                <div className="flex items-center gap-3 sm:gap-4">
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
                        <Building2 className="h-5 w-5 text-brand" />
                    </div>
                    <div>
                        <h2 className="text-xl md:text-2xl font-bold text-foreground">客户管理</h2>
                        <p className="text-xs md:text-sm text-muted-foreground">按品牌分组管理所有诊断记录</p>
                    </div>
                </div>
                <div className="flex items-center gap-2 flex-wrap">
                    {!mergeMode && (
                        <Button variant="outline" onClick={() => setMergeMode(true)}>
                            <Merge className="mr-2 h-4 w-4" /> 合并客户
                        </Button>
                    )}
                    {mergeMode && (
                        <>
                            <Button
                                variant="default"
                                disabled={selectedIds.size < 2}
                                onClick={openMergeDialog}
                            >
                                <Check className="mr-2 h-4 w-4" />
                                合并已选 ({selectedIds.size})
                            </Button>
                            <Button variant="ghost" onClick={exitMergeMode}>
                                <X className="mr-2 h-4 w-4" /> 取消
                            </Button>
                        </>
                    )}
                    {!mergeMode && (
                        <Button onClick={() => navigate('/diagnosis/new')}>
                            <Plus className="mr-2 h-4 w-4" /> 新建诊断
                        </Button>
                    )}
                </div>
            </div>

            {/* 合并模式提示 */}
            {mergeMode && (
                <Card className="border border-blue-200 bg-blue-50 rounded-xl">
                    <CardContent className="py-3 text-sm text-blue-700">
                        点击选择要合并的客户（至少2个），然后点击"合并已选"完成操作。合并后所有关联数据将迁移到主品牌。
                    </CardContent>
                </Card>
            )}

            {/* 搜索 + 状态切换 */}
            <Card className="border border-border rounded-xl">
                <CardContent className="pt-6 space-y-3">
                    <form onSubmit={handleSearch} className="flex flex-col sm:flex-row gap-3 sm:gap-4">
                        <div className="relative flex-1">
                            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-muted-foreground" />
                            <Input
                                placeholder="搜索编号(BRD-0001)、品牌名称或公司名..."
                                value={search}
                                onChange={(e) => setSearch(e.target.value)}
                                className="pl-10"
                            />
                        </div>
                        <Button type="submit">搜索</Button>
                        {search && (
                            <Button variant="outline" onClick={() => { setSearch(''); fetchBrands(); }}>
                                清空
                            </Button>
                        )}
                    </form>
                    <div className="flex gap-2">
                        <Button
                            variant={statusTab === 'active' ? 'default' : 'ghost'}
                            size="sm"
                            onClick={() => setStatusTab('active')}
                        >
                            活跃客户 ({activeCnt})
                        </Button>
                        <Button
                            variant={statusTab === 'archived' ? 'secondary' : 'ghost'}
                            size="sm"
                            onClick={() => setStatusTab('archived')}
                        >
                            <Archive className="h-3.5 w-3.5 mr-1" />
                            已归档 ({archivedCnt})
                        </Button>
                    </div>
                </CardContent>
            </Card>

            {/* 品牌列表 */}
            {error ? (
                <Card className="border border-border rounded-xl p-6 text-center text-red-500">{error}</Card>
            ) : loading ? (
                <Card className="border border-border rounded-xl p-12">
                    <div className="flex flex-col items-center justify-center gap-3">
                        <Loader2 className="h-8 w-8 animate-spin text-brand" />
                        <p className="text-sm text-muted-foreground">加载中...</p>
                    </div>
                </Card>
            ) : filteredBrands.length === 0 ? (
                <Card className="border border-border rounded-xl p-12">
                    <div className="flex flex-col items-center justify-center gap-3">
                        <Building2 className="h-10 w-10 text-muted-foreground/50" />
                        <p className="text-sm text-muted-foreground">
                            {statusTab === 'archived' ? '暂无已归档客户' : '暂无活跃客户'}
                        </p>
                    </div>
                </Card>
            ) : (
                <div className="grid gap-4">
                    {filteredBrands.map((brand) => (
                        <Card
                            key={brand.id}
                            className={`border border-border rounded-xl hover:border-gray-300 transition-all duration-200 cursor-pointer ${
                                statusTab === 'archived' ? 'opacity-70' : ''
                            } ${mergeMode && selectedIds.has(brand.id) ? 'ring-2 ring-brand bg-brand/5' : ''}`}
                            onClick={() => {
                                if (mergeMode) {
                                    setSelectedIds(prev => {
                                        const next = new Set(prev);
                                        if (next.has(brand.id)) next.delete(brand.id);
                                        else next.add(brand.id);
                                        return next;
                                    });
                                } else {
                                    navigate(`/brands/${brand.id}`);
                                }
                            }}
                        >
                            <CardContent className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 py-4">
                                <div className="flex items-center gap-3 sm:gap-4 min-w-0">
                                    {mergeMode && (
                                        <Checkbox
                                            checked={selectedIds.has(brand.id)}
                                            onClick={(e) => e.stopPropagation()}
                                            onCheckedChange={() => {
                                                setSelectedIds(prev => {
                                                    const next = new Set(prev);
                                                    if (next.has(brand.id)) next.delete(brand.id);
                                                    else next.add(brand.id);
                                                    return next;
                                                });
                                            }}
                                        />
                                    )}
                                    <div className={`w-10 h-10 sm:w-12 sm:h-12 rounded-full flex items-center justify-center text-white font-bold text-sm sm:text-base shrink-0 ${levelDotClass(levelOf(brand))}`}>
                                        {brand.latest_score || '-'}
                                    </div>
                                    <div className="min-w-0">
                                        <div className="flex items-center gap-2 flex-wrap">
                                            {brand.brand_code && (
                                                <span className="text-xs font-mono bg-muted px-1.5 py-0.5 rounded text-muted-foreground">
                                                    {brand.brand_code}
                                                </span>
                                            )}
                                            <h3 className="font-semibold text-base sm:text-lg text-foreground truncate">{brand.name}</h3>
                                            {statusTab === 'archived' && (
                                                <Badge variant="secondary" className="text-xs">已归档</Badge>
                                            )}
                                        </div>
                                        <p className="text-sm text-muted-foreground flex items-center gap-1.5">
                                            {brand.industry || '未分类'} · {brand.diagnosis_count} 次诊断
                                            {brand.social_enabled && (
                                                <span className="inline-flex items-center px-1.5 py-0.5 rounded bg-brand/10 text-brand text-[10px] font-medium">社媒</span>
                                            )}
                                        </p>
                                    </div>
                                </div>
                                {!mergeMode && (
                                    <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
                                        <Badge className={levelBadgeClass(levelOf(brand))}>
                                            {levelOf(brand).label}
                                        </Badge>
                                        {brand.diagnosis_count > 1 && (
                                            <div className="flex items-center gap-1 text-sm text-muted-foreground">
                                                <TrendingUp className="h-4 w-4" />
                                                <span>趋势</span>
                                            </div>
                                        )}
                                        {/* 社媒操盘手开关 */}
                                        {statusTab === 'active' && (
                                            <Button
                                                variant="ghost"
                                                size="sm"
                                                className={brand.social_enabled
                                                    ? "text-brand hover:text-brand/80 hover:bg-brand/10 text-xs h-7 px-2"
                                                    : "text-muted-foreground hover:text-foreground hover:bg-muted text-xs h-7 px-2"
                                                }
                                                title={brand.social_enabled ? '已开启社媒操盘手' : '开启社媒操盘手'}
                                                onClick={(e) => handleToggleSocial(brand, e)}
                                                disabled={processing === brand.id}
                                            >
                                                {brand.social_enabled ? '✓ 社媒' : '+ 社媒'}
                                            </Button>
                                        )}
                                        {/* 归档/激活按钮 */}
                                        <Button
                                            variant="ghost"
                                            size="icon"
                                            className={statusTab === 'archived'
                                                ? "text-blue-500 hover:text-blue-700 hover:bg-blue-50"
                                                : "text-amber-500 hover:text-amber-700 hover:bg-amber-50"
                                            }
                                            title={statusTab === 'archived' ? '激活' : '归档'}
                                            onClick={(e) => handleToggleArchive(brand, e)}
                                            disabled={processing === brand.id}
                                        >
                                            {processing === brand.id ? (
                                                <Loader2 className="h-4 w-4 animate-spin" />
                                            ) : statusTab === 'archived' ? (
                                                <RotateCcw className="h-4 w-4" />
                                            ) : (
                                                <Archive className="h-4 w-4" />
                                            )}
                                        </Button>
                                        {/* 永久删除（仅已归档可删，需密码） */}
                                        {statusTab === 'archived' && (
                                            <Button
                                                variant="ghost"
                                                size="icon"
                                                className="text-red-500 hover:text-red-700 hover:bg-red-50"
                                                title="永久删除"
                                                onClick={(e) => openDeleteDialog(brand, e)}
                                                disabled={processing === brand.id}
                                            >
                                                <Trash2 className="h-4 w-4" />
                                            </Button>
                                        )}
                                        <ArrowRight className="h-5 w-5 text-muted-foreground" />
                                    </div>
                                )}
                            </CardContent>
                        </Card>
                    ))}
                </div>
            )}

            {/* 统计 */}
            <div className="text-center text-sm text-muted-foreground">
                共 {brands.length} 个客户（活跃 {activeCnt} · 归档 {archivedCnt}）
            </div>

            {/* 删除确认弹窗（带密码） */}
            <AlertDialog open={deleteDialogOpen} onOpenChange={setDeleteDialogOpen}>
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>确认永久删除</AlertDialogTitle>
                        <AlertDialogDescription asChild>
                            <div className="space-y-3">
                                <p>您确定要永久删除以下客户吗？</p>
                                {deleteTarget && (
                                    <div className="bg-muted p-3 rounded-md">
                                        <p><strong>客户名称:</strong> {deleteTarget.name}</p>
                                        <p><strong>行业:</strong> {deleteTarget.industry || '未分类'}</p>
                                        <p><strong>诊断次数:</strong> {deleteTarget.diagnosis_count} 次</p>
                                    </div>
                                )}
                                <p className="text-red-600 font-medium">
                                    此操作不可撤销，该客户的所有数据将被永久删除！
                                </p>
                                <div>
                                    <label className="text-sm font-medium text-foreground">请输入管理密码:</label>
                                    <Input
                                        type="password"
                                        placeholder="输入密码以确认删除"
                                        value={deletePassword}
                                        onChange={(e) => { setDeletePassword(e.target.value); setDeleteError(""); }}
                                        className="mt-1"
                                        onKeyDown={(e) => { if (e.key === 'Enter') handleConfirmDelete(); }}
                                    />
                                    {deleteError && (
                                        <p className="text-red-500 text-sm mt-1">{deleteError}</p>
                                    )}
                                </div>
                            </div>
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel onClick={() => { setDeletePassword(""); setDeleteError(""); }}>
                            取消
                        </AlertDialogCancel>
                        <AlertDialogAction
                            onClick={handleConfirmDelete}
                            className="bg-red-600 hover:bg-red-700"
                            disabled={!deletePassword || processing === deleteTarget?.id}
                        >
                            {processing === deleteTarget?.id ? (
                                <Loader2 className="h-4 w-4 animate-spin mr-2" />
                            ) : null}
                            确认删除
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>

            {/* 合并弹窗 */}
            <Dialog open={mergeDialogOpen} onOpenChange={setMergeDialogOpen}>
                <DialogContent className="max-w-lg">
                    <DialogHeader>
                        <DialogTitle>合并客户</DialogTitle>
                        <DialogDescription>
                            选择保留的主品牌，其他品牌的数据将全部迁移到主品牌下
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-4">
                        {/* 选择主品牌 */}
                        <div>
                            <label className="text-sm font-medium">选择主品牌（保留）:</label>
                            <div className="mt-2 space-y-2 max-h-48 overflow-y-auto">
                                {selectedBrands.map(b => (
                                    <label
                                        key={b.id}
                                        className={`flex items-center gap-3 p-2 rounded-md cursor-pointer border ${
                                            primaryBrandId === b.id
                                                ? 'border-brand bg-brand/5'
                                                : 'border-border hover:bg-muted'
                                        }`}
                                        onClick={() => {
                                            setPrimaryBrandId(b.id);
                                            setMergeDisplayName(b.name);
                                        }}
                                    >
                                        <input
                                            type="radio"
                                            name="primary_brand"
                                            checked={primaryBrandId === b.id}
                                            onChange={() => {
                                                setPrimaryBrandId(b.id);
                                                setMergeDisplayName(b.name);
                                            }}
                                            className="accent-brand"
                                        />
                                        <div className="flex-1">
                                            <span className="font-medium text-foreground">{b.name}</span>
                                            {b.brand_code && (
                                                <span className="ml-2 text-xs font-mono bg-muted px-1.5 py-0.5 rounded text-muted-foreground">
                                                    {b.brand_code}
                                                </span>
                                            )}
                                            <span className="text-sm text-muted-foreground ml-2">
                                                {b.diagnosis_count} 次诊断
                                            </span>
                                        </div>
                                        {primaryBrandId === b.id && (
                                            <Badge className="bg-brand-50 text-brand-700 border border-brand-200">主</Badge>
                                        )}
                                    </label>
                                ))}
                            </div>
                        </div>

                        {/* 显示名称 */}
                        <div>
                            <label className="text-sm font-medium">合并后显示名称:</label>
                            <Input
                                value={mergeDisplayName}
                                onChange={(e) => setMergeDisplayName(e.target.value)}
                                placeholder="选择或输入合并后的显示名称"
                                className="mt-1"
                            />
                            <div className="flex gap-1 mt-1 flex-wrap">
                                {selectedBrands.map(b => (
                                    <Button
                                        key={b.id}
                                        variant="ghost"
                                        size="sm"
                                        className="text-xs h-6 px-2"
                                        onClick={() => setMergeDisplayName(b.name)}
                                    >
                                        {b.name}
                                    </Button>
                                ))}
                            </div>
                        </div>

                        {/* 将被合并（删除）的品牌 */}
                        {primaryBrandId && (
                            <div className="bg-amber-50 border border-amber-200 rounded-md p-3 text-sm">
                                <p className="font-medium text-amber-700">以下品牌将被合并（数据迁移后删除）:</p>
                                <ul className="mt-1 space-y-1">
                                    {selectedBrands
                                        .filter(b => b.id !== primaryBrandId)
                                        .map(b => (
                                            <li key={b.id} className="text-amber-600">
                                                • {b.name} ({b.diagnosis_count} 次诊断)
                                            </li>
                                        ))}
                                </ul>
                            </div>
                        )}

                        {/* 密码 */}
                        <div>
                            <label className="text-sm font-medium">管理密码:</label>
                            <Input
                                type="password"
                                placeholder="输入密码以确认合并"
                                value={mergePassword}
                                onChange={(e) => { setMergePassword(e.target.value); setMergeError(""); }}
                                className="mt-1"
                            />
                            {mergeError && (
                                <p className="text-red-500 text-sm mt-1">{mergeError}</p>
                            )}
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setMergeDialogOpen(false)}>
                            取消
                        </Button>
                        <Button
                            onClick={handleConfirmMerge}
                            disabled={!primaryBrandId || !mergePassword || merging}
                        >
                            {merging && <Loader2 className="h-4 w-4 animate-spin mr-2" />}
                            确认合并
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}
