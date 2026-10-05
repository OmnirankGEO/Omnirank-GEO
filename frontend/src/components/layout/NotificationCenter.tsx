import { authFetch } from '@/lib/api';
import { useState, useEffect, useCallback, useRef } from "react";
import { createPortal } from "react-dom";
import { Bell, Check, CheckCheck, FileText, AlertTriangle, Info, X, ExternalLink, PackageCheck, Undo2, ShoppingCart, CreditCard, CircleDollarSign } from "lucide-react";
import { cn } from "@/lib/utils";

interface Notification {
    id: number;
    brand_id: number;  // ✅ brand_id 统一化
    type: string;  // 'report', 'alert', 'system'
    title: string;
    content: string;
    related_id: number | null;
    is_read: number;
    created_at: string;
}

interface NotificationCenterProps {
    clientId: number;
    /** 面板向上弹出（用于侧边栏底部） */
    dropUp?: boolean;
}

export function NotificationCenter({ clientId, dropUp = false }: NotificationCenterProps) {
    const [notifications, setNotifications] = useState<Notification[]>([]);
    const [unreadCount, setUnreadCount] = useState(0);
    const [isOpen, setIsOpen] = useState(false);
    const [loading, setLoading] = useState(false);

    // 预览弹窗状态
    const [previewOpen, setPreviewOpen] = useState(false);
    const [previewNotification, setPreviewNotification] = useState<Notification | null>(null);
    const bellRef = useRef<HTMLButtonElement>(null);
    const [panelPos, setPanelPos] = useState<{ top: number; left: number } | null>(null);

    // clientId > 0 = 按品牌过滤; clientId === 0 = 管理员模式（所有品牌）
    const isAdminMode = clientId === 0;

    // 获取通知列表
    const fetchNotifications = useCallback(async () => {
        try {
            setLoading(true);
            const url = isAdminMode
                ? `/api/notifications?limit=20`
                : `/api/notifications?brand_id=${clientId}&limit=20`;
            const res = await authFetch(url);
            const data = await res.json();
            if (data.status === "success") {
                setNotifications(data.notifications || []);
            }
        } catch (err) {
            console.error("获取通知失败:", err);
        } finally {
            setLoading(false);
        }
    }, [clientId, isAdminMode]);

    // 获取未读数量
    const fetchUnreadCount = useCallback(async () => {
        try {
            const url = isAdminMode
                ? `/api/notifications/unread-count`
                : `/api/notifications/unread-count?brand_id=${clientId}`;
            const res = await authFetch(url);
            const data = await res.json();
            if (data.status === "success") {
                setUnreadCount(data.unread_count || 0);
            }
        } catch (err) {
            console.error("获取未读数量失败:", err);
        }
    }, [clientId, isAdminMode]);

    // 标记单个已读
    const markAsRead = async (notificationId: number) => {
        try {
            await authFetch(`/api/notifications/${notificationId}/read`, { method: "POST" });
            setNotifications(prev =>
                prev.map(n => n.id === notificationId ? { ...n, is_read: 1 } : n)
            );
            setUnreadCount(prev => Math.max(0, prev - 1));
        } catch (err) {
            console.error("标记已读失败:", err);
        }
    };

    // 标记全部已读
    const markAllAsRead = async () => {
        try {
            const url = isAdminMode
                ? `/api/notifications/mark-all-read`
                : `/api/notifications/mark-all-read?brand_id=${clientId}`;
            await authFetch(url, { method: "POST" });
            setNotifications(prev => prev.map(n => ({ ...n, is_read: 1 })));
            setUnreadCount(0);
        } catch (err) {
            console.error("标记全部已读失败:", err);
        }
    };

    // 打开预览弹窗
    const openPreview = (notification: Notification) => {
        if (!notification.is_read) {
            markAsRead(notification.id);
        }
        setPreviewNotification(notification);
        setPreviewOpen(true);
        setIsOpen(false); // 关闭下拉
    };

    // 初始加载 + 轮询
    useEffect(() => {
        fetchUnreadCount();
        const interval = setInterval(fetchUnreadCount, 30000); // 30秒轮询
        return () => clearInterval(interval);
    }, [fetchUnreadCount]);

    // 打开时加载列表
    useEffect(() => {
        if (isOpen) {
            fetchNotifications();
        }
    }, [isOpen, fetchNotifications]);

    // 获取图标
    const getIcon = (type: string) => {
        switch (type) {
            case "report": return <FileText className="h-4 w-4 text-blue-400" />;
            case "alert": return <AlertTriangle className="h-4 w-4 text-amber-400" />;
            case "keyword_submitted": return <PackageCheck className="h-4 w-4 text-green-400" />;
            case "keyword_withdrawn": return <Undo2 className="h-4 w-4 text-orange-400" />;
            case "keywords_added": return <PackageCheck className="h-4 w-4 text-cyan-400" />;
            case "quote_confirmed": return <ShoppingCart className="h-4 w-4 text-emerald-400" />;
            case "order_pending_payment": return <CreditCard className="h-4 w-4 text-purple-400" />;
            case "payment_received": return <CircleDollarSign className="h-4 w-4 text-green-500" />;
            default: return <Info className="h-4 w-4 text-slate-400" />;
        }
    };

    // 获取类型标签
    const getTypeLabel = (type: string) => {
        switch (type) {
            case "report": return { text: "报告", color: "bg-blue-500/20 text-blue-400" };
            case "alert": return { text: "告警", color: "bg-amber-500/20 text-amber-400" };
            case "keyword_submitted": return { text: "选词", color: "bg-green-500/20 text-green-400" };
            case "keyword_withdrawn": return { text: "撤回", color: "bg-orange-500/20 text-orange-400" };
            case "keywords_added": return { text: "加词", color: "bg-cyan-500/20 text-cyan-400" };
            case "quote_confirmed": return { text: "成交", color: "bg-emerald-500/20 text-emerald-400" };
            case "order_pending_payment": return { text: "待付", color: "bg-purple-500/20 text-purple-400" };
            case "payment_received": return { text: "收款", color: "bg-green-500/20 text-green-500" };
            default: return { text: "系统", color: "bg-slate-500/20 text-slate-400" };
        }
    };

    // 格式化通知内容（将 JSON 转为可读文本）
    const formatContent = (content: string, type: string): string => {
        if (!content) return '';
        try {
            const data = JSON.parse(content);
            switch (type) {
                case 'keyword_submitted':
                    return `选中 ${data.selected_count || 0} 个关键词` +
                        (data.custom_count ? `，自定义 ${data.custom_count} 个` : '');
                case 'keywords_added':
                    return data.new_keywords?.length
                        ? `新增关键词：${data.new_keywords.join('、')}`
                        : '追加了新关键词';
                case 'quote_confirmed':
                    return `${data.tier_label || ''}套餐，${data.keyword_count || 0} 个关键词，¥${(data.total_price || 0).toLocaleString()} · 累计达标30天`;
                case 'order_pending_payment':
                    return `成交价 ¥${(data.final_price || 0).toLocaleString()} · 累计达标30天` +
                        (data.discount_info ? `，${data.discount_info}` : '');
                case 'order_active':
                    return `已收款，关键词已同步到写作大厅`;
                case 'business_lines_submitted':
                    return data.selected_lines?.length
                        ? `已选业务：${data.selected_lines.join('、')}`
                        : '已提交业务线选择';
                default:
                    return content;
            }
        } catch {
            return content;
        }
    };

    // 格式化时间
    const formatTime = (dateStr: string) => {
        const date = new Date(dateStr);
        const now = new Date();
        const diff = now.getTime() - date.getTime();
        const minutes = Math.floor(diff / 60000);
        const hours = Math.floor(diff / 3600000);
        const days = Math.floor(diff / 86400000);

        if (minutes < 60) return `${minutes}分钟前`;
        if (hours < 24) return `${hours}小时前`;
        if (days < 7) return `${days}天前`;
        return date.toLocaleDateString("zh-CN");
    };

    // 格式化完整时间
    const formatFullTime = (dateStr: string) => {
        const date = new Date(dateStr);
        return date.toLocaleString("zh-CN", {
            year: "numeric",
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit"
        });
    };

    return (
        <>
            <div className="relative">
                {/* 铃铛按钮 */}
                <button
                    ref={bellRef}
                    onClick={() => {
                        if (!isOpen && bellRef.current) {
                            const rect = bellRef.current.getBoundingClientRect();
                            if (dropUp) {
                                setPanelPos({ top: rect.top - 8, left: rect.left });
                            } else {
                                setPanelPos({ top: rect.bottom + 8, left: rect.right - 320 });
                            }
                        }
                        setIsOpen(!isOpen);
                    }}
                    className="relative p-2 rounded-lg text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
                >
                    <Bell className="h-5 w-5" />
                    {unreadCount > 0 && (
                        <span className="absolute -top-1 -right-1 h-5 w-5 flex items-center justify-center bg-red-500 text-white text-xs font-bold rounded-full">
                            {unreadCount > 9 ? "9+" : unreadCount}
                        </span>
                    )}
                </button>

                {/* 下拉面板 - 通过 Portal 渲染到 body */}
                {isOpen && panelPos && createPortal(
                    <>
                        {/* 遮罩 · 2026-05-22 修:z-200 不是 Tailwind 有效 class(默认 z-50 max) · sidebar z-50 反而盖 portal · 改任意值语法 z-[200] */}
                        <div
                            className="fixed inset-0 z-[200]"
                            onClick={() => setIsOpen(false)}
                        />

                        {/* 通知面板 · P1 (CTO-15.23 2026-05-04) · 移动端全宽适配 · 桌面端固定 360 宽
                           · 2026-05-22 修:z-210 → z-[210] · 同上理 */}
                        <div
                            className="fixed bg-slate-800 border border-slate-700 rounded-xl shadow-2xl z-[210] overflow-hidden w-[calc(100vw-16px)] sm:w-[360px] max-h-[calc(100dvh-80px)]"
                            style={
                                dropUp
                                    ? { bottom: window.innerHeight - panelPos.top, left: window.innerWidth < 640 ? 8 : Math.max(8, Math.min(panelPos.left, window.innerWidth - 368)) }
                                    : { top: panelPos.top, left: window.innerWidth < 640 ? 8 : Math.max(8, Math.min(panelPos.left, window.innerWidth - 368)) }
                            }
                        >
                            {/* 头部 */}
                            <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
                                <h3 className="text-sm font-semibold text-white">通知中心</h3>
                                {unreadCount > 0 && (
                                    <button
                                        onClick={markAllAsRead}
                                        className="flex items-center gap-1 text-xs text-blue-400 hover:text-blue-300"
                                    >
                                        <CheckCheck className="h-3 w-3" />
                                        全部已读
                                    </button>
                                )}
                            </div>

                            {/* 通知列表 */}
                            <div className="max-h-80 overflow-y-auto">
                                {loading ? (
                                    <div className="p-4 text-center text-slate-400 text-sm">
                                        加载中...
                                    </div>
                                ) : notifications.length === 0 ? (
                                    <div className="p-8 text-center text-slate-500 text-sm">
                                        <Bell className="h-8 w-8 mx-auto mb-2 opacity-50" />
                                        暂无通知
                                    </div>
                                ) : (
                                    notifications.map((notification) => (
                                        <div
                                            key={notification.id}
                                            onClick={() => openPreview(notification)}
                                            className={cn(
                                                "px-4 py-3 border-b border-slate-700/50 cursor-pointer transition-colors",
                                                notification.is_read
                                                    ? "bg-slate-800/50 hover:bg-slate-800"
                                                    : "bg-slate-700/30 hover:bg-slate-700/50"
                                            )}
                                        >
                                            <div className="flex items-start gap-3">
                                                <div className="mt-0.5">
                                                    {getIcon(notification.type)}
                                                </div>
                                                <div className="flex-1 min-w-0">
                                                    <div className="flex items-center gap-2">
                                                        <p className={cn(
                                                            "text-sm line-clamp-2 break-words",
                                                            notification.is_read ? "text-slate-400" : "text-white font-medium"
                                                        )} title={notification.title}>
                                                            {notification.title}
                                                        </p>
                                                        {!notification.is_read && (
                                                            <span className="h-2 w-2 bg-blue-500 rounded-full shrink-0" />
                                                        )}
                                                    </div>
                                                    {notification.content && (
                                                        <p className="text-xs text-slate-500 mt-1 line-clamp-2">
                                                            {formatContent(notification.content, notification.type)}
                                                        </p>
                                                    )}
                                                    <p className="text-xs text-slate-600 mt-1">
                                                        {formatTime(notification.created_at)}
                                                    </p>
                                                </div>
                                                {notification.is_read && (
                                                    <Check className="h-4 w-4 text-green-500/50 shrink-0" />
                                                )}
                                            </div>
                                        </div>
                                    ))
                                )}
                            </div>

                            {/* 底部 */}
                            {notifications.length > 0 && (
                                <div className="px-4 py-2 border-t border-slate-700 text-center">
                                    <button className="text-xs text-blue-400 hover:text-blue-300">
                                        查看全部通知
                                    </button>
                                </div>
                            )}
                        </div>
                    </>,
                    document.body
                )}
            </div>

            {/* 预览弹窗 - 通过 Portal 渲染到 body */}
            {previewOpen && previewNotification && createPortal(
                <div className="fixed inset-0 z-300 flex items-center justify-center">
                    {/* 遮罩 */}
                    <div
                        className="absolute inset-0 bg-black/60 backdrop-blur-xs"
                        onClick={() => setPreviewOpen(false)}
                    />

                    {/* 弹窗内容 */}
                    <div className="relative w-full max-w-2xl mx-4 bg-slate-800 border border-slate-700 rounded-2xl shadow-2xl overflow-hidden">
                        {/* 头部 */}
                        <div className="flex items-center justify-between px-6 py-4 border-b border-slate-700 bg-slate-900/50">
                            <div className="flex items-center gap-3">
                                {getIcon(previewNotification.type)}
                                <div>
                                    <h2 className="text-lg font-semibold text-white">
                                        {previewNotification.title}
                                    </h2>
                                    <div className="flex items-center gap-2 mt-1">
                                        <span className={cn(
                                            "px-2 py-0.5 rounded text-xs font-medium",
                                            getTypeLabel(previewNotification.type).color
                                        )}>
                                            {getTypeLabel(previewNotification.type).text}
                                        </span>
                                        <span className="text-xs text-slate-500">
                                            {formatFullTime(previewNotification.created_at)}
                                        </span>
                                    </div>
                                </div>
                            </div>
                            <button
                                onClick={() => setPreviewOpen(false)}
                                className="p-2 rounded-lg text-slate-400 hover:text-white hover:bg-slate-700 transition-colors"
                            >
                                <X className="h-5 w-5" />
                            </button>
                        </div>

                        {/* 内容 */}
                        <div className="p-6 max-h-96 overflow-y-auto">
                            {previewNotification.content ? (
                                <div className="prose dark:prose-invert prose-sm max-w-none">
                                    <div className="whitespace-pre-wrap text-slate-300 leading-relaxed">
                                        {formatContent(previewNotification.content, previewNotification.type)}
                                    </div>
                                </div>
                            ) : (
                                <p className="text-slate-500 text-center py-8">
                                    暂无详细内容
                                </p>
                            )}
                        </div>

                        {/* 底部 */}
                        <div className="flex items-center justify-end gap-3 px-6 py-4 border-t border-slate-700 bg-slate-900/30">
                            {previewNotification.related_id && previewNotification.type === "report" && (
                                <button className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium rounded-lg transition-colors">
                                    <ExternalLink className="h-4 w-4" />
                                    查看报告详情
                                </button>
                            )}
                            <button
                                onClick={() => setPreviewOpen(false)}
                                className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white text-sm font-medium rounded-lg transition-colors"
                            >
                                关闭
                            </button>
                        </div>
                    </div>
                </div>,
                document.body
            )}
        </>
    );
}
