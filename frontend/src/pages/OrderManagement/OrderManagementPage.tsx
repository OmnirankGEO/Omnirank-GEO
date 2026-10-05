import { useState, useEffect, useCallback, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { toast } from "sonner";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
    RefreshCw, Check, X, Loader2, Search,
    CheckCircle2, ExternalLink, RotateCcw,
} from "lucide-react";
import { authFetch } from "@/lib/api";
import { cn } from "@/lib/utils";

interface FilterProps {
    search: string;
    brandId: number | null;
}

// ---- 概览 ----
function PublishOverview({ onJumpToReview }: { onJumpToReview?: () => void }) {
    const [stats, setStats] = useState<any>(null);
    const [loading, setLoading] = useState(true);
    useEffect(() => {
        authFetch("/api/meijiehezi/admin/stats").then(r => r.json()).then(d => {
            if (d.status === "success") setStats(d);
        }).catch(() => {}).finally(() => setLoading(false));
    }, []);
    if (loading) return <div className="flex justify-center py-12"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>;
    if (!stats) return <div className="text-center py-12 text-muted-foreground">加载失败</div>;
    return (
        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
            <Card><CardContent className="p-4">
                <div className="text-xs text-muted-foreground mb-1">今日代发</div>
                <div className="text-2xl font-bold">{stats.today_orders || 0}</div>
                <div className="text-xs text-muted-foreground mt-1">已发布 {stats.published || 0} · 已拒稿 {stats.rejected || 0}</div>
            </CardContent></Card>
            <Card><CardContent className="p-4">
                <div className="text-xs text-muted-foreground mb-1">Session 状态</div>
                <Badge variant="outline" className={cn("text-xs", stats.session_configured
                    ? "bg-green-500/15 text-green-400 border-green-500/30" : "bg-red-500/15 text-red-400 border-red-500/30")}>
                    {stats.session_configured ? "有效" : "未配置"}
                </Badge>
            </CardContent></Card>
            <Card><CardContent className="p-4">
                <div className="text-xs text-muted-foreground mb-1">媒体库</div>
                <div className="text-2xl font-bold">{stats.media_count || 0}</div>
                <div className="text-xs text-muted-foreground mt-1">已同步的活跃媒体</div>
            </CardContent></Card>
            <Card><CardContent className="p-4">
                <div className="text-xs text-muted-foreground mb-1">总订单</div>
                <div className="text-2xl font-bold">{stats.total_orders || 0}</div>
                <div className="text-xs text-muted-foreground mt-1">待处理 {stats.pending || 0} · 已提交 {stats.submitted || 0}</div>
            </CardContent></Card>
            {/* [2026-04-30 衔接补丁] 把"本地未同步项"和"人工审核队列"显眼地展示出来，点击跳到对应 tab */}
            {(stats.pre_sync_count > 0 || stats.manual_review_count > 0) && (
                <button
                    onClick={() => onJumpToReview?.()}
                    className="text-left"
                    type="button"
                >
                    <Card className={cn("transition-colors hover:bg-accent/50",
                        stats.manual_review_count > 0 ? "border-red-500/40" : "border-amber-500/40")}>
                        <CardContent className="p-4">
                            <div className="text-xs text-muted-foreground mb-1">需关注（点击查看）</div>
                            <div className="text-2xl font-bold">
                                <span className={cn(stats.manual_review_count > 0 ? "text-red-400" : "text-amber-400")}>
                                    {(stats.pre_sync_count || 0) + (stats.manual_review_count || 0)}
                                </span>
                            </div>
                            <div className="text-xs text-muted-foreground mt-1">
                                {stats.manual_review_count > 0 && <span className="text-red-400">人工审核 {stats.manual_review_count}</span>}
                                {stats.manual_review_count > 0 && stats.pre_sync_count > 0 && " · "}
                                {stats.pre_sync_count > 0 && <span className="text-amber-400">本地待同步 {stats.pre_sync_count}</span>}
                            </div>
                        </CardContent>
                    </Card>
                </button>
            )}
        </div>
    );
}

// ---- 代发订单 ----
const MHZ_STATUS_MAP: Record<number, { label: string; cls: string }> = {
    0: { label: "待接单", cls: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30" },
    1: { label: "发布中", cls: "bg-blue-500/15 text-blue-400 border-blue-500/30" },
    2: { label: "已完成", cls: "bg-green-500/15 text-green-400 border-green-500/30" },
    [-1]: { label: "已拒稿", cls: "bg-red-500/15 text-red-400 border-red-500/30" },
    [-2]: { label: "已撤回", cls: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30" },
    3: { label: "已退款", cls: "bg-orange-500/15 text-orange-400 border-orange-500/30" },
};

function PublishOrders({
    filter,
    initialStatus = "",
    onStatusChange,
}: {
    filter: FilterProps;
    initialStatus?: string;
    onStatusChange?: (status: string) => void;
}) {
    const navigate = useNavigate();
    const [orders, setOrders] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);
    const [syncing, setSyncing] = useState(false);
    const [page, setPage] = useState(1);
    const [pages, setPages] = useState(0);
    const [total, setTotal] = useState(0);
    const [statusFilter, setStatusFilter] = useState("");
    const [stats, setStats] = useState({ total: 0, completed: 0, pending: 0, rejected: 0, withdrawn: 0 });

    // 筛选变化时重置页码
    useEffect(() => { setPage(1); }, [filter.search, filter.brandId]);
    useEffect(() => { setStatusFilter(initialStatus); setPage(1); }, [initialStatus]);

    const loadOrders = useCallback(() => {
        setLoading(true);
        const params = new URLSearchParams({ page: String(page), limit: "20", all: "1" });
        if (statusFilter) params.set("status", statusFilter);
        if (filter.brandId) params.set("brand_id", String(filter.brandId));
        if (filter.search) params.set("search", filter.search);
        authFetch(`/api/meijiehezi/mhz-orders?${params}`).then(r => r.json()).then(d => {
            if (d.status === "success") { setOrders(d.orders || []); setTotal(d.total || 0); setPages(d.pages || 0); }
        }).catch(() => {}).finally(() => setLoading(false));
    }, [page, statusFilter, filter.search, filter.brandId]);

    useEffect(() => {
        let cancelled = false;
        (async () => {
            setLoading(true);
            try {
                const res = await authFetch("/api/meijiehezi/admin/sync/status", { method: "POST" });
                const d = await res.json();
                if (!cancelled && d.status === "success") {
                    await new Promise(r => setTimeout(r, 3000));
                }
            } catch {}
            if (!cancelled) {
                await loadOrders();
                setLoading(false);
            }
        })();
        return () => { cancelled = true; };
    }, []);

    useEffect(() => { loadOrders(); }, [loadOrders]);

    const handleSync = () => {
        setSyncing(true);
        authFetch("/api/meijiehezi/admin/sync/status", { method: "POST" })
            .then(r => r.json())
            .then(d => {
                if (d.status === "success") {
                    toast.success("同步已启动，稍后刷新查看");
                    setTimeout(() => { loadOrders(); setSyncing(false); }, 5000);
                } else {
                    toast.error(d.detail || "同步失败");
                    setSyncing(false);
                }
            })
            .catch(() => { toast.error("网络错误"); setSyncing(false); });
    };

    useEffect(() => {
        const bp = filter.brandId ? `&brand_id=${filter.brandId}` : "";
        const sp = filter.search ? `&search=${encodeURIComponent(filter.search)}` : "";
        authFetch(`/api/meijiehezi/mhz-orders?page=1&limit=1&all=1${bp}${sp}`)
            .then(r => r.json()).then(d => { if (d.status === "success") setStats(prev => ({ ...prev, total: d.total || 0 })); }).catch(() => {});
        for (const { status: s, key } of [
            { status: "2", key: "completed" }, { status: "0", key: "pending" },
            { status: "-1", key: "rejected" }, { status: "-2", key: "withdrawn" },
        ] as const) {
            authFetch(`/api/meijiehezi/mhz-orders?page=1&limit=1&all=1&status=${s}${bp}${sp}`)
                .then(r => r.json()).then(d => { if (d.status === "success") setStats(prev => ({ ...prev, [key]: d.total || 0 })); }).catch(() => {});
        }
    }, [filter.brandId, filter.search]);

    const statusBadge = (s: number) => {
        const info = MHZ_STATUS_MAP[s] || { label: `状态${s}`, cls: "bg-muted text-muted-foreground" };
        return <Badge variant="outline" className={cn("text-[10px]", info.cls)}>{info.label}</Badge>;
    };

    return (
        <div className="space-y-3">
            <div className="flex justify-end">
                <Button variant="outline" size="sm" disabled={syncing} onClick={handleSync} className="text-xs h-7 gap-1.5">
                    <RefreshCw className={cn("size-3", syncing && "animate-spin")} />
                    {syncing ? "同步中..." : "同步订单状态"}
                </Button>
            </div>
            <div className="grid grid-cols-5 gap-2">
                {[
                    { label: "总订单", value: stats.total, color: "text-foreground" },
                    { label: "已完成", value: stats.completed, color: "text-green-400" },
                    { label: "待接单", value: stats.pending, color: "text-yellow-400" },
                    { label: "已拒稿", value: stats.rejected, color: "text-red-400" },
                    { label: "已撤回", value: stats.withdrawn, color: "text-zinc-400" },
                ].map(s => (
                    <Card key={s.label}><CardContent className="p-3 text-center">
                        <div className={cn("text-lg font-bold", s.color)}>{s.value}</div>
                        <div className="text-[10px] text-muted-foreground">{s.label}</div>
                    </CardContent></Card>
                ))}
            </div>
            <div className="flex gap-1.5 flex-wrap">
                {[
                    { value: "", label: "全部" }, { value: "2", label: "已完成" }, { value: "0", label: "待接单" },
                    { value: "1", label: "发布中" }, { value: "-1", label: "已拒稿" }, { value: "-2", label: "已撤回" },
                ].map(f => (
                    <Button key={f.value} variant={statusFilter === f.value ? "default" : "outline"} size="sm" className="text-xs h-7"
                        onClick={() => { setStatusFilter(f.value); setPage(1); onStatusChange?.(f.value); }}>{f.label}</Button>
                ))}
            </div>
            {loading ? <div className="flex justify-center py-12"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div> :
            orders.length === 0 ? <div className="text-center py-12 text-muted-foreground">暂无订单</div> :
            orders.map(o => (
                <Card key={o.id}>
                    <CardContent className="p-4">
                        <div className="flex items-start gap-3">
                            <div className="flex-1 min-w-0">
                                <div className="text-sm font-medium truncate">{o.title}</div>
                                <div className="flex gap-2 mt-1.5 text-[10px] text-muted-foreground flex-wrap items-center">
                                    {statusBadge(o.status)}
                                    {(o.user_display_name || o.user_login) && (
                                        <span className="px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                                            {o.user_display_name || o.user_login}
                                        </span>
                                    )}
                                    {o.media_name && <span>{o.media_name}</span>}
                                    <span>{o.created_at?.split("T")[0]}</span>
                                    {o.published_at && <span className="text-green-400">发布: {o.published_at.split("T")[0]}</span>}
                                </div>
                                {o.url && <a href={o.url} target="_blank" rel="noopener noreferrer" className="text-xs text-primary hover:underline mt-1 inline-flex items-center gap-1">查看文章 <ExternalLink className="size-3" /></a>}
                                {o.status === -1 && o.reason && <div className="text-[10px] text-red-400 mt-1">拒稿原因: {o.reason}</div>}
                                {o.status === -1 && (
                                    <Button variant="outline" size="sm"
                                        className={cn("text-xs h-6 mt-1 gap-1 border-amber-500/30 hover:bg-amber-500/10",
                                            o.republished_order_sn ? "text-zinc-400" : "text-amber-400"
                                        )}
                                        onClick={async () => {
                                            try {
                                                const res = await authFetch(`/api/meijiehezi/mhz-orders/${o.order_sn}/article-info`);
                                                const d = await res.json();
                                                if (d.status === "success" && d.article_id) {
                                                    // [2026-05-05] 跳转带 quote_id 让发布页自动定位到正确项目（解决"暂无已完成文章"）
                                                    const qs = new URLSearchParams();
                                                    qs.set("article_id", String(d.article_id));
                                                    if (d.quote_id) qs.set("quote_id", String(d.quote_id));
                                                    if (d.brand_id) qs.set("brand_id", String(d.brand_id));
                                                    if (d.media_type) qs.set("media_type", String(d.media_type));
                                                    qs.set("republish_from", o.order_sn);
                                                    navigate(`/publish?${qs.toString()}`);
                                                } else {
                                                    toast.error("找不到原始文章，无法重发");
                                                }
                                            } catch { toast.error("查询失败"); }
                                        }}>
                                        <RotateCcw className="size-3" />
                                        {o.republished_order_sn ? "已重发，再次重发？" : "重发其他平台"}
                                    </Button>
                                )}
                            </div>
                        </div>
                    </CardContent>
                </Card>
            ))}
            {pages > 1 && (
                <div className="flex justify-center gap-2 pt-2">
                    <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</Button>
                    <span className="text-sm text-muted-foreground">{page}/{pages}</span>
                    <Button variant="ghost" size="sm" disabled={page >= pages} onClick={() => setPage(p => p + 1)}>下一页</Button>
                </div>
            )}
        </div>
    );
}

// ---- 退款审核 ----
interface RefundItem {
    id: number;
    order_id: string;
    user_id: number;
    user_name: string;
    user_login: string;
    reason: string;
    refund_points: number;
    status: string;
    admin_note: string;
    title: string;
    media_name: string;
    price: number;
    url: string;
    created_at: string;
    reviewed_at: string | null;
}

function RefundReview({ filter: globalFilter }: { filter: FilterProps }) {
    const [items, setItems] = useState<RefundItem[]>([]);
    const [loading, setLoading] = useState(true);
    const [filter, setFilter] = useState<string>("");
    const [page, setPage] = useState(1);
    const [total, setTotal] = useState(0);
    const [reviewingId, setReviewingId] = useState<number | null>(null);
    const [adminNote, setAdminNote] = useState("");

    useEffect(() => { setPage(1); }, [globalFilter.search]);
    const [copiedIds, setCopiedIds] = useState<Set<string>>(() => {
        try {
            const saved = localStorage.getItem("mhz_refund_copied_ids");
            return saved ? new Set(JSON.parse(saved)) : new Set();
        } catch { return new Set(); }
    });

    const markCopied = (orderId: string) => {
        setCopiedIds(prev => {
            const next = new Set(prev).add(orderId);
            localStorage.setItem("mhz_refund_copied_ids", JSON.stringify([...next]));
            return next;
        });
    };

    const fetchList = useCallback(() => {
        setLoading(true);
        const params = new URLSearchParams({ page: String(page), limit: "20" });
        if (filter) params.set("status", filter);
        if (globalFilter.search) params.set("search", globalFilter.search);
        if (globalFilter.brandId) params.set("brand_id", String(globalFilter.brandId));
        authFetch(`/api/meijiehezi/admin/refund/list?${params}`)
            .then(r => r.json())
            .then(d => {
                if (d.status === "success") {
                    setItems(d.items || []);
                    setTotal(d.total || 0);
                }
            })
            .catch(() => toast.error("加载退款列表失败"))
            .finally(() => setLoading(false));
    }, [page, filter, globalFilter.search, globalFilter.brandId]);

    useEffect(() => { fetchList(); }, [fetchList]);

    const handleReview = async (id: number, approved: boolean) => {
        try {
            const resp = await authFetch("/api/meijiehezi/admin/refund/review", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ request_id: id, approved, admin_note: adminNote }),
            });
            const d = await resp.json();
            if (d.status === "success") {
                toast.success(approved ? "已通过，算力已退还" : "已拒绝");
                setReviewingId(null);
                setAdminNote("");
                fetchList();
            } else {
                toast.error(d.message || "操作失败");
            }
        } catch {
            toast.error("网络错误");
        }
    };

    const statusLabel = (s: string) => {
        if (s === "approved") return { text: "已通过", cls: "bg-green-500/15 text-green-400 border-green-500/30" };
        if (s === "rejected") return { text: "已拒绝", cls: "bg-red-500/15 text-red-400 border-red-500/30" };
        return { text: "待审核", cls: "bg-yellow-500/15 text-yellow-400 border-yellow-500/30" };
    };

    return (
        <div className="space-y-4">
            <div className="flex items-center gap-2">
                {(["", "pending", "approved", "rejected"] as const).map(s => (
                    <button key={s} onClick={() => { setFilter(s); setPage(1); }}
                        className={cn(
                            "px-3 py-1.5 text-xs rounded-md border transition-colors",
                            filter === s ? "bg-primary/10 text-primary border-primary/30" : "text-muted-foreground border-border hover:text-foreground"
                        )}>
                        {s === "" ? "全部" : s === "pending" ? "待审核" : s === "approved" ? "已通过" : "已拒绝"}
                    </button>
                ))}
                <span className="text-xs text-muted-foreground ml-auto">共 {total} 条</span>
            </div>
            {loading ? (
                <div className="flex justify-center py-12"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div>
            ) : items.length === 0 ? (
                <div className="text-center py-12 text-muted-foreground text-sm">暂无退款申请</div>
            ) : (
                <div className="space-y-3">
                    {items.map(item => {
                        const st = statusLabel(item.status);
                        const isCopied = copiedIds.has(item.order_id);
                        const isApprovedNotCopied = item.status === "approved" && !isCopied;
                        const isApprovedCopied = item.status === "approved" && isCopied;
                        return (
                            <Card key={item.id} className={cn(
                                isApprovedNotCopied && "border-amber-500/40 bg-amber-500/5",
                                isApprovedCopied && "opacity-60",
                            )}>
                                <CardContent className="p-4 space-y-3">
                                    <div className="flex items-start justify-between gap-3">
                                        <div className="flex-1 min-w-0">
                                            <div className="flex items-center gap-2">
                                                {item.url ? (
                                                    <a href={item.url} target="_blank" rel="noopener noreferrer"
                                                        className="font-medium text-sm text-blue-400 hover:text-blue-300 hover:underline truncate">
                                                        {item.title || "未知标题"}
                                                        <ExternalLink className="inline h-3 w-3 ml-1 shrink-0" />
                                                    </a>
                                                ) : (
                                                    <span className="font-medium text-sm truncate">{item.title || "未知标题"}</span>
                                                )}
                                            </div>
                                            <div className="flex items-center gap-2 text-xs text-muted-foreground mt-1.5 flex-wrap">
                                                <span>用户: {item.user_name || item.user_login || `ID:${item.user_id}`}</span>
                                                <span>·</span>
                                                <span>媒体: {item.media_name}</span>
                                                <span>·</span>
                                                <span className="inline-flex items-center gap-1">
                                                    订单号: <code className="bg-muted px-1 rounded text-[11px]">{item.order_id}</code>
                                                    <button onClick={() => { navigator.clipboard.writeText(item.order_id); markCopied(item.order_id); toast.success("订单号已复制"); }}
                                                        className="hover:text-foreground transition-colors" title="复制订单号">
                                                        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" className="h-3 w-3">
                                                            <rect width="14" height="14" x="8" y="8" rx="2" ry="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>
                                                        </svg>
                                                    </button>
                                                </span>
                                            </div>
                                            {item.reason && (
                                                <div className="text-xs text-muted-foreground mt-1">
                                                    退款原因: {item.reason}
                                                </div>
                                            )}
                                            <div className="text-xs text-muted-foreground mt-1">
                                                申请时间: {item.created_at}
                                                {item.reviewed_at && ` · 审核时间: ${item.reviewed_at}`}
                                            </div>
                                        </div>
                                        <div className="flex flex-col items-end gap-2 shrink-0">
                                            <Badge variant="outline" className={cn("text-xs", st.cls)}>{st.text}</Badge>
                                            {isApprovedNotCopied && (
                                                <span className="text-[10px] text-amber-400">待复制订单号</span>
                                            )}
                                            {isApprovedCopied && (
                                                <span className="text-[10px] text-muted-foreground">已复制</span>
                                            )}
                                            <div className="text-sm font-bold text-primary">{item.refund_points} 算力</div>
                                        </div>
                                    </div>
                                    {item.admin_note && (
                                        <div className="text-xs bg-muted/50 rounded p-2">
                                            管理员备注: {item.admin_note}
                                        </div>
                                    )}
                                    {item.status === "pending" && (
                                        <div className="space-y-2">
                                            {reviewingId === item.id ? (
                                                <>
                                                    <Input
                                                        placeholder="审核备注（可选）"
                                                        value={adminNote}
                                                        onChange={e => setAdminNote(e.target.value)}
                                                        className="text-sm"
                                                    />
                                                    <div className="flex gap-2">
                                                        <Button size="sm" onClick={() => handleReview(item.id, true)}
                                                            className="bg-green-600 hover:bg-green-700 text-white">
                                                            <Check className="h-3.5 w-3.5 mr-1" />通过退款
                                                        </Button>
                                                        <Button size="sm" variant="destructive" onClick={() => handleReview(item.id, false)}>
                                                            <X className="h-3.5 w-3.5 mr-1" />拒绝
                                                        </Button>
                                                        <Button size="sm" variant="ghost" onClick={() => { setReviewingId(null); setAdminNote(""); }}>
                                                            取消
                                                        </Button>
                                                    </div>
                                                </>
                                            ) : (
                                                <Button size="sm" variant="outline" onClick={() => setReviewingId(item.id)}>
                                                    审核
                                                </Button>
                                            )}
                                        </div>
                                    )}
                                </CardContent>
                            </Card>
                        );
                    })}
                </div>
            )}
            {total > 20 && (
                <div className="flex justify-center gap-2 pt-2">
                    <Button size="sm" variant="outline" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</Button>
                    <span className="text-xs text-muted-foreground self-center">第 {page} 页</span>
                    <Button size="sm" variant="outline" disabled={page * 20 >= total} onClick={() => setPage(p => p + 1)}>下一页</Button>
                </div>
            )}
        </div>
    );
}

// ---- 主页面 ----
// [WO_273] 「自发记录」子 tab 随浏览器插件自助发布退役下线:它读的 publish-records 端点由后端同单删除。
//   老链 ?tab=self 不在白名单里 ⇒ parseOrderTab 落回概览。历史自助记录用户侧仍在「发布记录 → 自助发布」可见。
type SubTab = "overview" | "orders" | "refunds" | "review";

const ORDER_TABS = new Set<SubTab>(["overview", "orders", "refunds", "review"]);
const parseOrderTab = (value: string | null): SubTab =>
    value && ORDER_TABS.has(value as SubTab) ? (value as SubTab) : "overview";

// ---- [2026-04-30] 人工审核队列 ----
function ManualReviewQueue({ filter }: { filter: FilterProps }) {
    const [items, setItems] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);
    const [page, setPage] = useState(1);
    const [pages, setPages] = useState(0);
    const [total, setTotal] = useState(0);
    const [actingId, setActingId] = useState<number | null>(null);
    const [adminNote, setAdminNote] = useState("");

    const load = useCallback(() => {
        setLoading(true);
        const params = new URLSearchParams({ page: String(page), limit: "20" });
        if (filter.brandId) params.set("brand_id", String(filter.brandId));
        if (filter.search) params.set("search", filter.search);
        authFetch(`/api/meijiehezi/admin/manual-review-queue?${params}`)
            .then(r => r.json())
            .then(d => { if (d.status === "success") { setItems(d.items || []); setTotal(d.total || 0); setPages(d.pages || 0); } })
            .catch(() => {})
            .finally(() => setLoading(false));
    }, [page, filter.search, filter.brandId]);

    useEffect(() => { load(); }, [load]);
    useEffect(() => { setPage(1); }, [filter.search, filter.brandId]);

    const resolve = async (item_id: number, action: "mark_published" | "mark_failed") => {
        try {
            const resp = await authFetch("/api/meijiehezi/admin/manual-review-queue/resolve", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ item_id, action, admin_note: adminNote }),
            });
            const d = await resp.json();
            if (d.status === "success") {
                toast.success(d.message || "已处理");
                setActingId(null); setAdminNote("");
                load();
            } else {
                toast.error(d.message || "操作失败");
            }
        } catch { toast.error("网络错误"); }
    };

    return (
        <div className="space-y-3">
            <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-300">
                这里是「外部通道已接单但 12 小时反查不到凭证号」的特殊订单。<b>不会自动退款也不会自动重发</b>，
                你必须自己去外部发布后台核对：如果那边真发出去了 → 标"已发布"；
                如果那边没接到 → 标"未接单"，系统会按 item 退款给用户。
            </div>
            {loading ? <div className="flex justify-center py-12"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div> :
            items.length === 0 ? <div className="text-center py-12 text-muted-foreground">人工审核队列是空的 ✓</div> :
            items.map(it => (
                <Card key={it.id}>
                    <CardContent className="p-4 space-y-2">
                        <div className="flex items-start justify-between gap-3">
                            <div className="flex-1 min-w-0">
                                <div className="text-sm font-medium truncate">{it.title}</div>
                                <div className="flex gap-2 mt-1.5 text-[10px] text-muted-foreground flex-wrap items-center">
                                    <Badge variant="outline" className="text-[10px] bg-red-500/15 text-red-400 border-red-500/30">需人工审核</Badge>
                                    {(it.user_display_name || it.user_login) && (
                                        <span className="px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                                            {it.user_display_name || it.user_login}
                                        </span>
                                    )}
                                    {it.media_name && <span>{it.media_name}（{it.media_type === "wemedia" ? "自媒体" : "软文"}）</span>}
                                    {it.cost_points > 0 && <span>{it.cost_points} 算力</span>}
                                    {it.awaiting_sync_since && <span className="text-amber-400">挂起于 {it.awaiting_sync_since.split("T")[0]}</span>}
                                </div>
                                {it.reject_reason && <div className="text-[10px] text-muted-foreground mt-1 truncate">备注: {it.reject_reason}</div>}
                            </div>
                        </div>
                        {actingId === it.id ? (
                            <div className="space-y-2 pt-2 border-t">
                                <Input placeholder="管理员备注（可选）" value={adminNote} onChange={e => setAdminNote(e.target.value)} className="h-8 text-xs" />
                                <div className="flex gap-2">
                                    <Button variant="default" size="sm" className="text-xs h-7 gap-1" onClick={() => resolve(it.id, "mark_published")}>
                                        <CheckCircle2 className="size-3" />已发布（不退款）
                                    </Button>
                                    <Button variant="destructive" size="sm" className="text-xs h-7 gap-1" onClick={() => resolve(it.id, "mark_failed")}>
                                        <X className="size-3" />未接单（退款）
                                    </Button>
                                    <Button variant="ghost" size="sm" className="text-xs h-7" onClick={() => { setActingId(null); setAdminNote(""); }}>取消</Button>
                                </div>
                            </div>
                        ) : (
                            <Button variant="outline" size="sm" className="text-xs h-7" onClick={() => setActingId(it.id)}>处理</Button>
                        )}
                    </CardContent>
                </Card>
            ))}
            {pages > 1 && (
                <div className="flex justify-center gap-2 pt-2">
                    <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</Button>
                    <span className="text-sm text-muted-foreground">{page}/{pages}</span>
                    <Button variant="ghost" size="sm" disabled={page >= pages} onClick={() => setPage(p => p + 1)}>下一页</Button>
                </div>
            )}
            <div className="text-xs text-muted-foreground">共 {total} 条待人工审核</div>
        </div>
    );
}

// ---- [2026-04-30] 本地待同步项 ----
function LocalPendingItems({ filter }: { filter: FilterProps }) {
    const [items, setItems] = useState<any[]>([]);
    const [loading, setLoading] = useState(true);
    const [page, setPage] = useState(1);
    const [pages, setPages] = useState(0);
    const [total, setTotal] = useState(0);

    const load = useCallback(() => {
        setLoading(true);
        const params = new URLSearchParams({ page: String(page), limit: "20" });
        if (filter.brandId) params.set("brand_id", String(filter.brandId));
        if (filter.search) params.set("search", filter.search);
        authFetch(`/api/meijiehezi/admin/local-pending-items?${params}`)
            .then(r => r.json())
            .then(d => { if (d.status === "success") { setItems(d.items || []); setTotal(d.total || 0); setPages(d.pages || 0); } })
            .catch(() => {})
            .finally(() => setLoading(false));
    }, [page, filter.search, filter.brandId]);

    useEffect(() => { load(); }, [load]);
    useEffect(() => { setPage(1); }, [filter.search, filter.brandId]);

    const localStatusBadge = (s: string) => {
        const map: Record<string, { label: string; cls: string }> = {
            pending: { label: "待提交", cls: "bg-zinc-500/15 text-zinc-400 border-zinc-500/30" },
            submitting: { label: "提交中", cls: "bg-blue-500/15 text-blue-400 border-blue-500/30" },
            submitted: { label: "已提交", cls: "bg-cyan-500/15 text-cyan-400 border-cyan-500/30" },
            awaiting_confirmation: { label: "等用户确认", cls: "bg-purple-500/15 text-purple-400 border-purple-500/30" },
            awaiting_sync: { label: "等同步回查", cls: "bg-amber-500/15 text-amber-400 border-amber-500/30" },
            // [2026-07-26] 卡单人工出口的落点。不补这条，下面的 map[s] 兜底会把原始状态串
            // 直接显示成 "awaiting_action"（工程术语，违反全站说人话）。
            awaiting_action: { label: "待你确认", cls: "bg-orange-500/15 text-orange-400 border-orange-500/30" },
        };
        const info = map[s] || { label: s, cls: "bg-muted text-muted-foreground" };
        return <Badge variant="outline" className={cn("text-[10px]", info.cls)}>{info.label}</Badge>;
    };

    return (
        <div className="space-y-3">
            <div className="rounded-md border border-amber-500/20 bg-amber-500/5 px-3 py-2 text-xs text-muted-foreground">
                这里是用户已经提交、但外部发布通道的状态还没拉到我们这边的订单。
                正常情况下每 10 分钟同步一次会自动消化掉。如果某条订单**长期挂在这里没动**，可能是外部发布通道接口出问题。
            </div>
            {loading ? <div className="flex justify-center py-12"><Loader2 className="h-5 w-5 animate-spin text-muted-foreground" /></div> :
            items.length === 0 ? <div className="text-center py-12 text-muted-foreground">没有待同步的订单 ✓</div> :
            items.map(it => (
                <Card key={it.id}>
                    <CardContent className="p-4">
                        <div className="text-sm font-medium truncate">{it.title}</div>
                        <div className="flex gap-2 mt-1.5 text-[10px] text-muted-foreground flex-wrap items-center">
                            {localStatusBadge(it.local_status)}
                            {(it.user_display_name || it.user_login) && (
                                <span className="px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20">
                                    {it.user_display_name || it.user_login}
                                </span>
                            )}
                            {it.media_name && <span>{it.media_name}（{it.media_type === "wemedia" ? "自媒体" : "软文"}）</span>}
                            {it.cost_points > 0 && <span>{it.cost_points} 算力</span>}
                            {it.last_submit_at && <span>提交: {it.last_submit_at.split("T")[0]} {it.last_submit_at.split("T")[1]?.slice(0, 5)}</span>}
                        </div>
                        {it.hint && <div className="text-[10px] text-amber-400 mt-1">{it.hint}</div>}
                    </CardContent>
                </Card>
            ))}
            {pages > 1 && (
                <div className="flex justify-center gap-2 pt-2">
                    <Button variant="ghost" size="sm" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>上一页</Button>
                    <span className="text-sm text-muted-foreground">{page}/{pages}</span>
                    <Button variant="ghost" size="sm" disabled={page >= pages} onClick={() => setPage(p => p + 1)}>下一页</Button>
                </div>
            )}
            <div className="text-xs text-muted-foreground">共 {total} 条本地待同步</div>
        </div>
    );
}

export function OrderManagementPage() {
    const [searchParams, setSearchParams] = useSearchParams();
    const [subTab, setSubTab] = useState<SubTab>(() => parseOrderTab(searchParams.get("tab")));
    const [brands, setBrands] = useState<{ id: number; name: string }[]>([]);
    const [brandId, setBrandId] = useState<number | null>(() => {
        const raw = Number(searchParams.get("brand_id"));
        return Number.isFinite(raw) && raw > 0 ? raw : null;
    });
    const [searchInput, setSearchInput] = useState(searchParams.get("search") || "");
    const [search, setSearch] = useState(searchParams.get("search") || "");
    const [brandSearch, setBrandSearch] = useState("");
    const [brandOpen, setBrandOpen] = useState(false);
    const brandRef = useRef<HTMLDivElement>(null);
    const urlBrandId = searchParams.get("brand_id") || "";
    const initialOrderStatus = searchParams.get("status") || "";

    const updateUrl = (patch: Record<string, string | null>) => {
        const next = new URLSearchParams(searchParams);
        Object.entries(patch).forEach(([key, value]) => {
            if (value == null || value === "") next.delete(key);
            else next.set(key, value);
        });
        setSearchParams(next);
    };

    const changeSubTab = (tab: SubTab) => {
        setSubTab(tab);
        updateUrl({ tab });
    };

    const applySearch = (value: string) => {
        const clean = value.trim();
        setSearch(clean);
        updateUrl({ search: clean || null });
    };

    const clearDashboardEntry = () => {
        setSubTab("overview");
        setSearch("");
        setSearchInput("");
        setBrandId(null);
        setSearchParams(new URLSearchParams());
    };

    const entryNotice = (() => {
        if (searchParams.get("from") !== "dashboard") return null;
        if (subTab === "orders") return "来自运营控制台:代发订单";
        if (subTab === "review") return "来自运营控制台:需关注订单";
        if (subTab === "refunds") return "来自运营控制台:退款审核";
        return "来自运营控制台:订单概览";
    })();

    // 切换 tab 时加载品牌列表([WO_273] 自发来源随「自发记录」子 tab 下线,只剩代发来源)
    useEffect(() => {
        authFetch(`/api/meijiehezi/mhz-orders/brands?source=proxy`)
            .then(r => r.json())
            .then(d => { if (d.status === "success") setBrands(d.brands || []); })
            .catch(() => {});
        const raw = Number(urlBrandId);
        setBrandId(Number.isFinite(raw) && raw > 0 ? raw : null);
    }, [subTab, urlBrandId]);

    // 点击外部关闭品牌下拉
    useEffect(() => {
        const handler = (e: MouseEvent) => {
            if (brandRef.current && !brandRef.current.contains(e.target as Node)) setBrandOpen(false);
        };
        document.addEventListener("mousedown", handler);
        return () => document.removeEventListener("mousedown", handler);
    }, []);

    const handleSearchKeyDown = (e: React.KeyboardEvent) => {
        if (e.key === "Enter") applySearch(searchInput);
    };

    const filteredBrands = brandSearch
        ? brands.filter(b => b.name.toLowerCase().includes(brandSearch.toLowerCase()))
        : brands;

    const selectedBrandName = brandId ? brands.find(b => b.id === brandId)?.name || "" : "";

    const filter: FilterProps = { search, brandId };
    const showFilter = subTab === "orders" || subTab === "refunds" || subTab === "review";

    return (
        <div className="p-3 md:p-6 space-y-4">
            {/* 搜索 + 品牌筛选 */}
            {showFilter && (
                <div className="flex items-center gap-2">
                    <div className="relative flex-1 max-w-sm">
                        <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 size-3.5 text-muted-foreground" />
                        <Input
                            placeholder="搜索标题、订单号、用户名..."
                            value={searchInput}
                            onChange={e => setSearchInput(e.target.value)}
                            onKeyDown={handleSearchKeyDown}
                            onBlur={() => applySearch(searchInput)}
                            className="pl-8 h-8 text-xs"
                        />
                    </div>
                    {brands.length > 0 && (
                        <div className="relative" ref={brandRef}>
                            <button
                                onClick={() => setBrandOpen(!brandOpen)}
                                className={cn(
                                    "h-8 px-3 text-xs rounded-md border border-border bg-background flex items-center gap-1.5 min-w-[120px] max-w-[200px]",
                                    brandId ? "text-foreground" : "text-muted-foreground"
                                )}
                            >
                                <span className="truncate">{brandId ? selectedBrandName : "全部客户"}</span>
                                <svg className="size-3 shrink-0 opacity-50" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" /></svg>
                            </button>
                            {brandOpen && (
                                <div className="absolute top-full left-0 mt-1 w-56 bg-popover border border-border rounded-lg shadow-lg z-50 py-1">
                                    <div className="px-2 py-1.5">
                                        <Input
                                            placeholder="搜索客户..."
                                            value={brandSearch}
                                            onChange={e => setBrandSearch(e.target.value)}
                                            className="h-7 text-xs"
                                            autoFocus
                                        />
                                    </div>
                                    <div className="max-h-48 overflow-y-auto">
                                        <button
                                            onClick={() => { setBrandId(null); setBrandOpen(false); setBrandSearch(""); updateUrl({ brand_id: null }); }}
                                            className={cn("w-full text-left px-3 py-1.5 text-xs hover:bg-accent transition-colors",
                                                !brandId && "text-primary font-medium"
                                            )}
                                        >全部客户</button>
                                        {filteredBrands.map(b => (
                                            <button key={b.id}
                                                onClick={() => { setBrandId(b.id); setBrandOpen(false); setBrandSearch(""); updateUrl({ brand_id: String(b.id) }); }}
                                                className={cn("w-full text-left px-3 py-1.5 text-xs hover:bg-accent transition-colors truncate",
                                                    brandId === b.id && "text-primary font-medium"
                                                )}
                                            >{b.name}</button>
                                        ))}
                                        {filteredBrands.length === 0 && (
                                            <div className="px-3 py-2 text-xs text-muted-foreground">无匹配客户</div>
                                        )}
                                    </div>
                                </div>
                            )}
                        </div>
                    )}
                    {(search || brandId) && (
                        <Button variant="ghost" size="sm" className="h-8 text-xs text-muted-foreground"
                            onClick={() => { setSearch(""); setSearchInput(""); setBrandId(null); updateUrl({ search: null, brand_id: null }); }}>
                            <X className="size-3 mr-1" />清除筛选
                        </Button>
                    )}
                </div>
            )}

            {entryNotice && (
                <div className="rounded-lg border border-primary/20 bg-primary/5 px-3 py-2 flex items-center justify-between gap-3">
                    <div className="text-xs text-muted-foreground">
                        <span className="text-foreground font-medium">{entryNotice}</span>
                        <span className="ml-2">已自动进入对应板块，可继续筛选或清除。</span>
                    </div>
                    <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={clearDashboardEntry}>清除筛选</Button>
                </div>
            )}

            <div className="flex gap-1 border-b border-border">
                {([
                    { key: "overview" as const, label: "概览" },
                    { key: "orders" as const, label: "代发订单" },
                    { key: "review" as const, label: "人工审核" },
                    { key: "refunds" as const, label: "退款审核" },
                ]).map(t => (
                    <button key={t.key} onClick={() => changeSubTab(t.key)}
                        className={cn(
                            "px-4 py-2 text-sm font-medium border-b-2 transition-colors -mb-[1px]",
                            subTab === t.key ? "border-primary text-foreground" : "border-transparent text-muted-foreground hover:text-foreground"
                        )}>
                        {t.label}
                    </button>
                ))}
            </div>
            {subTab === "overview" && <PublishOverview onJumpToReview={() => changeSubTab("review")} />}
            {subTab === "orders" && <PublishOrders filter={filter} initialStatus={initialOrderStatus} onStatusChange={(status) => updateUrl({ status })} />}
            {subTab === "review" && (
                <div className="space-y-6">
                    <ManualReviewQueue filter={filter} />
                    <div className="border-t pt-4">
                        <div className="text-sm font-medium mb-2">本地待同步项</div>
                        <LocalPendingItems filter={filter} />
                    </div>
                </div>
            )}
            {subTab === "refunds" && <RefundReview filter={filter} />}
        </div>
    );
}
