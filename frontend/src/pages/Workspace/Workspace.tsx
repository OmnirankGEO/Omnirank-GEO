import { authFetch } from '@/lib/api';
import { taskStatusLabel } from '@/lib/v35Terminology';
import { useState, useEffect } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogFooter,
} from "@/components/ui/dialog";
import {
    Loader2,
    Search,
    Plus,
    Briefcase,
    CheckCircle2,
    Clock,
    PlayCircle,
    Archive,
    LayoutGrid,
    List,
    Eye,
    Pencil,
    Trash2,
} from "lucide-react";
import ReactMarkdown from "@/components/SafeMarkdown";
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

// 类型定义
interface WorkResult {
    id: number;
    title: string;
    result_type: string;
    brand_id: number;
    brand_name: string;
    meeting_id: string;
    assignee_id: string;
    assignee_name: string;
    content: string;
    tags: string;
    keywords: string;
    status: string;
    priority: number;
    due_date: string;
    created_at: string;
    completed_at: string;
}

interface ResultType {
    id: number;
    name: string;
    icon: string;
    color: string;
    description: string;
}

interface WorkspaceStats {
    total: number;
    by_status: Record<string, number>;
    by_type: Record<string, number>;
}

// 状态配置
const STATUS_CONFIG: Record<string, { label: string; icon: any; color: string }> = {
    "待执行": { label: "待执行", icon: Clock, color: "bg-gray-100 text-gray-700" },
    "进行中": { label: "进行中", icon: PlayCircle, color: "bg-blue-100 text-blue-700" },
    "已完成": { label: "已完成", icon: CheckCircle2, color: "bg-green-100 text-green-700" },
    "已归档": { label: "已归档", icon: Archive, color: "bg-purple-100 text-purple-700" },
};

export function Workspace() {
    const [results, setResults] = useState<WorkResult[]>([]);
    const [types, setTypes] = useState<ResultType[]>([]);
    const [stats, setStats] = useState<WorkspaceStats | null>(null);
    const [loading, setLoading] = useState(true);

    // 筛选
    const [searchText, setSearchText] = useState("");
    const [filterStatus, setFilterStatus] = useState<string>("");
    const [filterType, setFilterType] = useState<string>("");

    // 视图模式
    const [viewMode, setViewMode] = useState<"board" | "list">("board");

    // 新建弹窗
    const [createOpen, setCreateOpen] = useState(false);
    const [newTitle, setNewTitle] = useState("");
    const [newType, setNewType] = useState("");
    const [newContent, setNewContent] = useState("");
    const [creating, setCreating] = useState(false);

    // 详情弹窗
    const [detailOpen, setDetailOpen] = useState(false);
    const [selectedResult, setSelectedResult] = useState<WorkResult | null>(null);

    // 编辑弹窗
    const [editOpen, setEditOpen] = useState(false);
    const [editForm, setEditForm] = useState({ title: "", content: "", result_type: "", status: "" });
    const [saving, setSaving] = useState(false);

    // 删除确认
    const [deleteOpen, setDeleteOpen] = useState(false);
    const [deleteTarget, setDeleteTarget] = useState<WorkResult | null>(null);
    const [deleting, setDeleting] = useState(false);

    useEffect(() => {
        fetchData();
        fetchTypes();
    }, []);

    useEffect(() => {
        fetchData();
    }, [filterStatus, filterType]);

    const fetchData = async () => {
        setLoading(true);
        try {
            const params = new URLSearchParams();
            if (filterStatus) params.append("status", filterStatus);
            if (filterType) params.append("result_type", filterType);
            if (searchText) params.append("search", searchText);
            const response = await authFetch(`/api/workspace/results?${params}`);
            const data = await response.json();
            if (data.success) {
                setResults(data.results || []);
                setStats(data.stats);
            }
        } catch (error) {
            console.error("Failed to fetch results:", error);
        } finally {
            setLoading(false);
        }
    };

    const fetchTypes = async () => {
        try {
            const response = await authFetch("/api/workspace/types");
            const data = await response.json();
            if (data.success) {
                setTypes(data.types || []);
            }
        } catch (error) {
            console.error("Failed to fetch types:", error);
        }
    };

    const handleSearch = () => {
        fetchData();
    };

    const handleCreate = async () => {
        if (!newTitle.trim()) return;
        setCreating(true);
        try {
            const response = await authFetch("/api/workspace/results", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    title: newTitle,
                    result_type: newType || null,
                    content: newContent || null,
                    status: "待执行",
                }),
            });
            const data = await response.json();
            if (data.success) {
                setCreateOpen(false);
                setNewTitle("");
                setNewType("");
                setNewContent("");
                fetchData();
            }
        } catch (error) {
            console.error("Failed to create:", error);
        } finally {
            setCreating(false);
        }
    };

    const updateStatus = async (id: number, status: string) => {
        try {
            await authFetch(`/api/workspace/results/${id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ status }),
            });
            fetchData();
        } catch (error) {
            console.error("Failed to update:", error);
        }
    };

    // 打开详情弹窗
    const openDetail = (result: WorkResult) => {
        setSelectedResult(result);
        setDetailOpen(true);
    };

    // 打开编辑弹窗
    const openEdit = (result: WorkResult) => {
        setEditForm({
            title: result.title || "",
            content: result.content || "",
            result_type: result.result_type || "",
            status: result.status || "待执行",
        });
        setSelectedResult(result);
        setEditOpen(true);
    };

    // 保存编辑
    const handleSave = async () => {
        if (!selectedResult) return;
        setSaving(true);
        try {
            const response = await authFetch(`/api/workspace/results/${selectedResult.id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(editForm),
            });
            const data = await response.json();
            if (data.success) {
                setEditOpen(false);
                setDetailOpen(false);
                fetchData();
            }
        } catch (error) {
            console.error("Failed to save:", error);
        } finally {
            setSaving(false);
        }
    };

    // 确认删除
    const confirmDelete = (result: WorkResult) => {
        setDeleteTarget(result);
        setDeleteOpen(true);
    };

    // 执行删除
    const handleDelete = async () => {
        if (!deleteTarget) return;
        setDeleting(true);
        try {
            const response = await authFetch(`/api/workspace/results/${deleteTarget.id}`, {
                method: "DELETE",
            });
            const data = await response.json();
            if (data.success) {
                setDeleteOpen(false);
                setDetailOpen(false);
                fetchData();
            }
        } catch (error) {
            console.error("Failed to delete:", error);
        } finally {
            setDeleting(false);
        }
    };

    const getTypeInfo = (typeName: string) => {
        return types.find(t => t.name === typeName) || { icon: "📄", color: "#6366f1" };
    };

    // 按状态分组（看板视图）
    const groupedByStatus = {
        "待执行": results.filter(r => r.status === "待执行"),
        "进行中": results.filter(r => r.status === "进行中"),
        "已完成": results.filter(r => r.status === "已完成"),
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
                <span className="ml-2 text-gray-500">加载中...</span>
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6 min-h-[calc(100dvh-3rem)] bg-linear-to-br from-slate-50 to-indigo-50">
            {/* 页面标题 */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold flex items-center gap-2">
                        <Briefcase className="h-6 w-6 text-indigo-600" />
                        工作台
                    </h1>
                    <p className="text-gray-500 mt-1">
                        管理工作成果和任务
                    </p>
                </div>
                <Button onClick={() => setCreateOpen(true)}>
                    <Plus className="h-4 w-4 mr-1" />
                    新建成果
                </Button>
            </div>

            {/* 统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <Card className="bg-linear-to-br from-gray-50 to-gray-100 border-gray-200">
                    <CardContent className="p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-gray-500">待执行</p>
                                <p className="text-2xl font-bold text-gray-700">{stats?.by_status?.["待执行"] || 0}</p>
                            </div>
                            <Clock className="h-8 w-8 text-gray-400" />
                        </div>
                    </CardContent>
                </Card>
                <Card className="bg-linear-to-br from-blue-50 to-blue-100 border-blue-200">
                    <CardContent className="p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-blue-600">进行中</p>
                                <p className="text-2xl font-bold text-blue-700">{stats?.by_status?.["进行中"] || 0}</p>
                            </div>
                            <PlayCircle className="h-8 w-8 text-blue-400" />
                        </div>
                    </CardContent>
                </Card>
                <Card className="bg-linear-to-br from-green-50 to-green-100 border-green-200">
                    <CardContent className="p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-green-600">已完成</p>
                                <p className="text-2xl font-bold text-green-700">{stats?.by_status?.["已完成"] || 0}</p>
                            </div>
                            <CheckCircle2 className="h-8 w-8 text-green-400" />
                        </div>
                    </CardContent>
                </Card>
                <Card className="bg-linear-to-br from-indigo-50 to-indigo-100 border-indigo-200">
                    <CardContent className="p-4">
                        <div className="flex items-center justify-between">
                            <div>
                                <p className="text-sm text-indigo-600">全部</p>
                                <p className="text-2xl font-bold text-indigo-700">{stats?.total || 0}</p>
                            </div>
                            <Briefcase className="h-8 w-8 text-indigo-400" />
                        </div>
                    </CardContent>
                </Card>
            </div>

            {/* 搜索和筛选 */}
            <Card>
                <CardContent className="p-4">
                    <div className="flex items-center gap-4">
                        <div className="flex-1 flex gap-2">
                            <Input
                                placeholder="搜索... (多关键词用空格分隔，如：抖音 营销)"
                                value={searchText}
                                onChange={(e) => setSearchText(e.target.value)}
                                onKeyDown={(e) => e.key === "Enter" && handleSearch()}
                                className="flex-1"
                            />
                            <Button variant="outline" onClick={handleSearch}>
                                <Search className="h-4 w-4" />
                            </Button>
                        </div>
                        <Select value={filterStatus || "all"} onValueChange={(v) => setFilterStatus(v === "all" ? "" : v)}>
                            <SelectTrigger className="w-32">
                                <SelectValue placeholder="状态" />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value="all">全部状态</SelectItem>
                                <SelectItem value="待执行">待执行</SelectItem>
                                <SelectItem value="进行中">进行中</SelectItem>
                                <SelectItem value="已完成">已完成</SelectItem>
                            </SelectContent>
                        </Select>
                        <Select value={filterType || "all"} onValueChange={(v) => setFilterType(v === "all" ? "" : v)}>
                            <SelectTrigger className="w-36">
                                <SelectValue placeholder="类型" />
                            </SelectTrigger>
                            <SelectContent>
                                <SelectItem value="all">全部类型</SelectItem>
                                {types.map((t) => (
                                    <SelectItem key={t.id} value={t.name}>
                                        {t.icon} {t.name}
                                    </SelectItem>
                                ))}
                            </SelectContent>
                        </Select>
                        <div className="flex gap-1 border rounded-lg p-1">
                            <Button
                                variant={viewMode === "board" ? "default" : "ghost"}
                                size="sm"
                                onClick={() => setViewMode("board")}
                            >
                                <LayoutGrid className="h-4 w-4" />
                            </Button>
                            <Button
                                variant={viewMode === "list" ? "default" : "ghost"}
                                size="sm"
                                onClick={() => setViewMode("list")}
                            >
                                <List className="h-4 w-4" />
                            </Button>
                        </div>
                    </div>
                </CardContent>
            </Card>

            {/* 看板视图 - 新布局 */}
            {viewMode === "board" && (
                <div className="flex flex-col gap-4 h-[calc(100dvh-280px)]">
                    {/* 上方：待执行和进行中 */}
                    <Card className="shrink-0">
                        <CardContent className="p-4">
                            <div className="flex gap-6">
                                {(["待执行", "进行中"] as const).map((status) => {
                                    const StatusIcon = STATUS_CONFIG[status]?.icon;
                                    const items = groupedByStatus[status];
                                    return (
                                        <div key={status} className="flex-1">
                                            <div className="flex items-center gap-2 mb-3">
                                                {StatusIcon && <StatusIcon className="h-4 w-4" />}
                                                <span className="font-medium">{status}</span>
                                                <Badge variant="secondary">{items.length}</Badge>
                                            </div>
                                            <div className="flex flex-wrap gap-2 min-h-[60px]">
                                                {items.map((result) => (
                                                    <div
                                                        key={result.id}
                                                        className="bg-white border rounded-lg px-3 py-2 cursor-pointer hover:shadow-md transition-shadow flex items-center gap-2 group"
                                                        onClick={() => openDetail(result)}
                                                    >
                                                        <span>{getTypeInfo(result.result_type).icon}</span>
                                                        <span className="text-sm truncate max-w-[200px]">{result.title}</span>
                                                        {status === "待执行" && (
                                                            <Button
                                                                size="sm"
                                                                variant="ghost"
                                                                className="h-5 px-2 text-xs opacity-0 group-hover:opacity-100"
                                                                onClick={(e) => { e.stopPropagation(); updateStatus(result.id, "进行中"); }}
                                                            >
                                                                开始
                                                            </Button>
                                                        )}
                                                        {status === "进行中" && (
                                                            <Button
                                                                size="sm"
                                                                variant="ghost"
                                                                className="h-5 px-2 text-xs text-green-600 opacity-0 group-hover:opacity-100"
                                                                onClick={(e) => { e.stopPropagation(); updateStatus(result.id, "已完成"); }}
                                                            >
                                                                完成
                                                            </Button>
                                                        )}
                                                    </div>
                                                ))}
                                                {items.length === 0 && (
                                                    <div className="text-gray-400 text-sm">暂无{status}的任务</div>
                                                )}
                                            </div>
                                        </div>
                                    );
                                })}
                            </div>
                        </CardContent>
                    </Card>

                    {/* 下方：已完成 - 左右分栏 */}
                    <Card className="flex-1 overflow-hidden">
                        <CardContent className="p-0 h-full">
                            <div className="flex h-full">
                                {/* 左侧：标题列表 */}
                                <div className="w-80 border-r flex flex-col h-full bg-gray-50/50">
                                    <div className="flex items-center gap-2 p-3 border-b bg-white">
                                        <CheckCircle2 className="h-4 w-4 text-green-600" />
                                        <span className="font-medium">已完成</span>
                                        <Badge variant="secondary">{groupedByStatus["已完成"].length}</Badge>
                                    </div>
                                    <div className="flex-1 overflow-y-auto">
                                        {groupedByStatus["已完成"].map((result) => (
                                            <div
                                                key={result.id}
                                                className={`p-3 border-b cursor-pointer hover:bg-white transition-colors ${selectedResult?.id === result.id ? 'bg-blue-50 border-l-2 border-l-blue-500' : ''
                                                    }`}
                                                onClick={() => setSelectedResult(result)}
                                            >
                                                <div className="flex items-center gap-2">
                                                    <span>{getTypeInfo(result.result_type).icon}</span>
                                                    <div className="flex-1 min-w-0">
                                                        <h4 className="text-sm font-medium truncate">{result.title}</h4>
                                                        <div className="flex items-center gap-2 mt-1">
                                                            {result.result_type && (
                                                                <span className="text-xs text-gray-500">{result.result_type}</span>
                                                            )}
                                                            {result.brand_name && (
                                                                <span className="text-xs text-gray-400">· {result.brand_name}</span>
                                                            )}
                                                        </div>
                                                    </div>
                                                </div>
                                            </div>
                                        ))}
                                        {groupedByStatus["已完成"].length === 0 && (
                                            <div className="text-center text-gray-400 py-8 text-sm">暂无已完成的任务</div>
                                        )}
                                    </div>
                                </div>

                                {/* 右侧：内容详情 */}
                                <div className="flex-1 flex flex-col h-full overflow-hidden">
                                    {selectedResult && groupedByStatus["已完成"].some(r => r.id === selectedResult.id) ? (
                                        <>
                                            <div className="flex items-center justify-between p-4 border-b bg-white">
                                                <div className="flex items-center gap-2">
                                                    <span className="text-xl">{getTypeInfo(selectedResult.result_type).icon}</span>
                                                    <h3 className="font-semibold text-lg">{selectedResult.title}</h3>
                                                </div>
                                                <div className="flex gap-2">
                                                    <Button variant="outline" size="sm" onClick={() => openEdit(selectedResult)}>
                                                        <Pencil className="h-4 w-4 mr-1" />
                                                        编辑
                                                    </Button>
                                                    <Button variant="outline" size="sm" className="text-red-600" onClick={() => confirmDelete(selectedResult)}>
                                                        <Trash2 className="h-4 w-4 mr-1" />
                                                        删除
                                                    </Button>
                                                </div>
                                            </div>
                                            {/* 元数据 */}
                                            <div className="flex items-center gap-4 px-4 py-2 text-sm bg-gray-50 border-b">
                                                <span><span className="text-gray-500">类型：</span>{selectedResult.result_type || "-"}</span>
                                                <span><span className="text-gray-500">品牌：</span>{selectedResult.brand_name || "-"}</span>
                                                <span><span className="text-gray-500">执行者：</span>{selectedResult.assignee_name || "-"}</span>
                                                <span><span className="text-gray-500">创建：</span>{new Date(selectedResult.created_at).toLocaleDateString()}</span>
                                            </div>
                                            {/* 内容 */}
                                            <div className="flex-1 overflow-y-auto p-4">
                                                {selectedResult.content ? (
                                                    <div className="prose prose-sm dark:prose-invert max-w-none">
                                                        <ReactMarkdown>
                                                            {selectedResult.content}
                                                        </ReactMarkdown>
                                                    </div>
                                                ) : (
                                                    <div className="text-gray-400 text-center py-8">暂无内容</div>
                                                )}
                                            </div>
                                        </>
                                    ) : (
                                        <div className="flex-1 flex items-center justify-center text-gray-400">
                                            <div className="text-center">
                                                <Eye className="h-12 w-12 mx-auto mb-2 opacity-30" />
                                                <p>点击左侧列表查看详情</p>
                                            </div>
                                        </div>
                                    )}
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 列表视图 */}
            {viewMode === "list" && (
                <Card>
                    <CardContent className="p-0">
                        <table className="w-full">
                            <thead className="bg-gray-50">
                                <tr className="text-left text-sm text-gray-600">
                                    <th className="p-3">标题</th>
                                    <th className="p-3">类型</th>
                                    <th className="p-3">状态</th>
                                    <th className="p-3">品牌</th>
                                    <th className="p-3">执行者</th>
                                    <th className="p-3">创建时间</th>
                                    <th className="p-3">操作</th>
                                </tr>
                            </thead>
                            <tbody>
                                {results.map((result) => {
                                    const typeInfo = getTypeInfo(result.result_type);
                                    const statusConfig = STATUS_CONFIG[result.status] || STATUS_CONFIG["待执行"];
                                    return (
                                        <tr key={result.id} className="border-t hover:bg-gray-50">
                                            <td className="p-3">
                                                <div className="flex items-center gap-2">
                                                    <span>{typeInfo.icon}</span>
                                                    <span className="font-medium">{result.title}</span>
                                                </div>
                                            </td>
                                            <td className="p-3">
                                                <Badge variant="outline">{result.result_type || "-"}</Badge>
                                            </td>
                                            <td className="p-3">
                                                <Badge className={statusConfig.color}>{taskStatusLabel(result.status)}</Badge>
                                            </td>
                                            <td className="p-3 text-sm text-gray-600">{result.brand_name || "-"}</td>
                                            <td className="p-3 text-sm text-gray-600">{result.assignee_name || "-"}</td>
                                            <td className="p-3 text-sm text-gray-500">
                                                {new Date(result.created_at).toLocaleDateString()}
                                            </td>
                                            <td className="p-3">
                                                <div className="flex gap-1">
                                                    {result.status === "待执行" && (
                                                        <Button size="sm" variant="ghost" onClick={() => updateStatus(result.id, "进行中")}>
                                                            开始
                                                        </Button>
                                                    )}
                                                    {result.status === "进行中" && (
                                                        <Button size="sm" variant="ghost" onClick={() => updateStatus(result.id, "已完成")}>
                                                            完成
                                                        </Button>
                                                    )}
                                                </div>
                                            </td>
                                        </tr>
                                    );
                                })}
                            </tbody>
                        </table>
                        {results.length === 0 && (
                            <div className="text-center py-12 text-gray-400">
                                暂无工作成果，点击"新建成果"创建第一个
                            </div>
                        )}
                    </CardContent>
                </Card>
            )}

            {/* 新建弹窗 */}
            <Dialog open={createOpen} onOpenChange={setCreateOpen}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>新建工作成果</DialogTitle>
                    </DialogHeader>
                    <div className="space-y-4">
                        <div>
                            <label className="text-sm font-medium">标题 *</label>
                            <Input
                                value={newTitle}
                                onChange={(e) => setNewTitle(e.target.value)}
                                placeholder="输入成果标题..."
                            />
                        </div>
                        <div>
                            <label className="text-sm font-medium">类型</label>
                            <Select value={newType} onValueChange={setNewType}>
                                <SelectTrigger>
                                    <SelectValue placeholder="选择类型" />
                                </SelectTrigger>
                                <SelectContent>
                                    {types.map((t) => (
                                        <SelectItem key={t.id} value={t.name}>
                                            {t.icon} {t.name}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div>
                            <label className="text-sm font-medium">内容</label>
                            <Textarea
                                value={newContent}
                                onChange={(e) => setNewContent(e.target.value)}
                                placeholder="输入成果内容..."
                                rows={4}
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setCreateOpen(false)}>
                            取消
                        </Button>
                        <Button onClick={handleCreate} disabled={creating || !newTitle.trim()}>
                            {creating ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                            创建
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* 详情弹窗 */}
            <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
                <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2">
                            {selectedResult && getTypeInfo(selectedResult.result_type).icon}
                            {selectedResult?.title}
                        </DialogTitle>
                    </DialogHeader>
                    {selectedResult && (
                        <div className="space-y-4">
                            {/* 元数据 */}
                            <div className="grid grid-cols-2 gap-4 text-sm">
                                <div>
                                    <span className="text-gray-500">类型：</span>
                                    <span className="ml-1">{selectedResult.result_type || "-"}</span>
                                </div>
                                <div>
                                    <span className="text-gray-500">状态：</span>
                                    <Badge className={STATUS_CONFIG[selectedResult.status]?.color || "bg-gray-100"}>
                                        {selectedResult.status}
                                    </Badge>
                                </div>
                                <div>
                                    <span className="text-gray-500">品牌：</span>
                                    <span className="ml-1">{selectedResult.brand_name || "-"}</span>
                                </div>
                                <div>
                                    <span className="text-gray-500">执行者：</span>
                                    <span className="ml-1">{selectedResult.assignee_name || "-"}</span>
                                </div>
                                <div>
                                    <span className="text-gray-500">创建时间：</span>
                                    <span className="ml-1">{new Date(selectedResult.created_at).toLocaleString()}</span>
                                </div>
                            </div>
                            {/* 内容 */}
                            {selectedResult.content && (
                                <div>
                                    <div className="text-sm text-gray-500 mb-2">内容</div>
                                    <div className="bg-gray-50 rounded-lg p-4 prose prose-sm max-w-none max-h-60 overflow-y-auto">
                                        <ReactMarkdown>
                                            {selectedResult.content}
                                        </ReactMarkdown>
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                    <DialogFooter>
                        <Button variant="outline" onClick={() => confirmDelete(selectedResult!)}>
                            <Trash2 className="h-4 w-4 mr-1" />
                            删除
                        </Button>
                        <Button onClick={() => openEdit(selectedResult!)}>
                            <Pencil className="h-4 w-4 mr-1" />
                            编辑
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* 编辑弹窗 */}
            <Dialog open={editOpen} onOpenChange={setEditOpen}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>编辑成果</DialogTitle>
                    </DialogHeader>
                    <div className="space-y-4">
                        <div>
                            <label className="text-sm font-medium">标题</label>
                            <Input
                                value={editForm.title}
                                onChange={(e) => setEditForm({ ...editForm, title: e.target.value })}
                            />
                        </div>
                        <div>
                            <label className="text-sm font-medium">类型</label>
                            <Select value={editForm.result_type} onValueChange={(v) => setEditForm({ ...editForm, result_type: v })}>
                                <SelectTrigger>
                                    <SelectValue placeholder="选择类型" />
                                </SelectTrigger>
                                <SelectContent>
                                    {types.map((t) => (
                                        <SelectItem key={t.id} value={t.name}>
                                            {t.icon} {t.name}
                                        </SelectItem>
                                    ))}
                                </SelectContent>
                            </Select>
                        </div>
                        <div>
                            <label className="text-sm font-medium">状态</label>
                            <Select value={editForm.status} onValueChange={(v) => setEditForm({ ...editForm, status: v })}>
                                <SelectTrigger>
                                    <SelectValue />
                                </SelectTrigger>
                                <SelectContent>
                                    <SelectItem value="待执行">待执行</SelectItem>
                                    <SelectItem value="进行中">进行中</SelectItem>
                                    <SelectItem value="已完成">已完成</SelectItem>
                                </SelectContent>
                            </Select>
                        </div>
                        <div>
                            <label className="text-sm font-medium">内容</label>
                            <Textarea
                                value={editForm.content}
                                onChange={(e) => setEditForm({ ...editForm, content: e.target.value })}
                                rows={6}
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setEditOpen(false)}>
                            取消
                        </Button>
                        <Button onClick={handleSave} disabled={saving || !editForm.title.trim()}>
                            {saving ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                            保存
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* 删除确认弹窗 */}
            <AlertDialog open={deleteOpen} onOpenChange={setDeleteOpen}>
                <AlertDialogContent>
                    <AlertDialogHeader>
                        <AlertDialogTitle>确认删除</AlertDialogTitle>
                        <AlertDialogDescription>
                            确定要删除 "{deleteTarget?.title}" 吗？此操作无法撤销。
                        </AlertDialogDescription>
                    </AlertDialogHeader>
                    <AlertDialogFooter>
                        <AlertDialogCancel>取消</AlertDialogCancel>
                        <AlertDialogAction
                            className="bg-red-600 hover:bg-red-700"
                            onClick={handleDelete}
                            disabled={deleting}
                        >
                            {deleting ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : null}
                            删除
                        </AlertDialogAction>
                    </AlertDialogFooter>
                </AlertDialogContent>
            </AlertDialog>
        </div>
    );
}
