/**
 * 审计日志页面
 * 日志列表 + 筛选（用户/操作/模块/时间范围） + 分页 + 详情弹窗
 */
import React, { useState, useEffect, useCallback } from 'react';
import { ScrollText } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { cn } from '@/lib/utils';
import { humanizeAuditSnapshot, formatRawAuditSnapshot } from '@/lib/auditTranslate';

// ========== 类型 ==========
interface AuditLog {
    id: number;
    user_id: number;
    username: string;
    action: string;
    module: string;
    entity_type: string;
    entity_id: number;
    summary: string;
    before_snapshot: unknown;
    after_snapshot: unknown;
    ip_address: string;
    created_at: string;
}

// ========== 主组件 ==========
export default function AuditLogs() {
    const [logs, setLogs] = useState<AuditLog[]>([]);
    const [total, setTotal] = useState(0);
    const [page, setPage] = useState(1);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const pageSize = 20;

    // 筛选
    const [filterUser, setFilterUser] = useState('');
    const [filterAction, setFilterAction] = useState('');
    const [filterModule, setFilterModule] = useState('');

    // 详情弹窗
    const [detail, setDetail] = useState<AuditLog | null>(null);

    const fetchLogs = useCallback(async () => {
        setLoading(true);
        try {
            const params: Record<string, any> = { page, page_size: pageSize };
            if (filterUser) params.username = filterUser;
            if (filterAction) params.action = filterAction;
            if (filterModule) params.module = filterModule;

            const res = await authApi.get('/api/admin/audit-logs', { params });
            setLogs(res.data.logs || []);
            setTotal(res.data.total || 0);
        } catch (e: any) {
            setError(e.response?.data?.detail || '加载失败');
        }
        setLoading(false);
    }, [page, filterUser, filterAction, filterModule]);

    useEffect(() => { fetchLogs(); }, [fetchLogs]);

    const totalPages = Math.ceil(total / pageSize);

    const actionBadgeClass: Record<string, string> = {
        create: 'bg-green-50 text-green-700',
        update: 'bg-blue-50 text-blue-700',
        delete: 'bg-red-50 text-red-700',
        login: 'bg-violet-50 text-violet-700',
        reset_password: 'bg-amber-50 text-amber-700',
        change_password: 'bg-amber-50 text-amber-700',
        disable: 'bg-red-50 text-red-700',
    };

    const getActionClass = (action: string) =>
        actionBadgeClass[action] || 'bg-secondary text-muted-foreground';

    if (error && logs.length === 0) return <div className="text-center py-20 text-destructive text-base">❌ {error}</div>;

    return (
        <div>
            <div className="flex justify-between items-center mb-4">
                <div className="flex items-center gap-3">
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                        <ScrollText className="h-5 w-5 text-brand" />
                    </div>
                    <div>
                        <h1 className="text-lg sm:text-xl md:text-2xl font-bold text-foreground m-0">审计日志</h1>
                        <p className="text-sm text-muted-foreground mt-1">共 {total} 条记录</p>
                    </div>
                </div>
            </div>

            {/* 筛选栏 */}
            <div className="flex gap-2.5 mb-4 flex-wrap">
                <input
                    className="px-3.5 py-2 rounded-xl border border-border text-xs outline-hidden w-full sm:w-[180px] bg-secondary focus:ring-2 focus:ring-brand"
                    placeholder="按用户名筛选"
                    value={filterUser}
                    onChange={e => { setFilterUser(e.target.value); setPage(1); }}
                />
                <select
                    className="px-3.5 py-2 rounded-xl border border-border text-xs outline-hidden bg-secondary cursor-pointer"
                    value={filterAction}
                    onChange={e => { setFilterAction(e.target.value); setPage(1); }}
                >
                    <option value="">全部操作</option>
                    <option value="create">创建</option>
                    <option value="update">修改</option>
                    <option value="delete">删除</option>
                    <option value="login">登录</option>
                    <option value="reset_password">重置密码</option>
                    <option value="disable">禁用</option>
                </select>
                <select
                    className="px-3.5 py-2 rounded-xl border border-border text-xs outline-hidden bg-secondary cursor-pointer"
                    value={filterModule}
                    onChange={e => { setFilterModule(e.target.value); setPage(1); }}
                >
                    <option value="">全部模块</option>
                    <option value="auth">认证</option>
                    <option value="users">用户管理</option>
                    <option value="roles">角色管理</option>
                    <option value="brands">客户管理</option>
                    <option value="writing">写作中心</option>
                    <option value="social">社媒操盘手</option>
                    <option value="diagnosis">诊断中心</option>
                </select>
                <button
                    className="px-4 py-2 rounded-xl border border-border bg-card cursor-pointer text-xs text-muted-foreground hover:bg-secondary transition-colors"
                    onClick={() => { setFilterUser(''); setFilterAction(''); setFilterModule(''); setPage(1); }}
                >
                    清除筛选
                </button>
            </div>

            {/* 日志表格 */}
            <div className="bg-card rounded-xl border border-border overflow-x-auto">
                <table className="w-full border-collapse">
                    <thead>
                        <tr>
                            <th className="px-3.5 py-3 text-left text-xs text-muted-foreground font-semibold border-b border-border bg-muted">时间</th>
                            <th className="px-3.5 py-3 text-left text-xs text-muted-foreground font-semibold border-b border-border bg-muted">用户</th>
                            <th className="px-3.5 py-3 text-left text-xs text-muted-foreground font-semibold border-b border-border bg-muted">操作</th>
                            <th className="px-3.5 py-3 text-left text-xs text-muted-foreground font-semibold border-b border-border bg-muted">模块</th>
                            <th className="px-3.5 py-3 text-left text-xs text-muted-foreground font-semibold border-b border-border bg-muted">摘要</th>
                            <th className="px-3.5 py-3 text-left text-xs text-muted-foreground font-semibold border-b border-border bg-muted">IP</th>
                            <th className="px-3.5 py-3 text-center text-xs text-muted-foreground font-semibold border-b border-border bg-muted">详情</th>
                        </tr>
                    </thead>
                    <tbody>
                        {loading ? (
                            <tr><td colSpan={7} className="px-3.5 py-10 text-center text-brand text-xs">加载中...</td></tr>
                        ) : logs.length === 0 ? (
                            <tr><td colSpan={7} className="px-3.5 py-10 text-center text-muted-foreground text-xs">暂无记录</td></tr>
                        ) : logs.map(log => (
                            <tr key={log.id} className="border-b border-border/50">
                                <td className="px-3.5 py-3 text-xs text-foreground">
                                    {new Date(log.created_at).toLocaleString('zh-CN', {
                                        month: '2-digit', day: '2-digit',
                                        hour: '2-digit', minute: '2-digit', second: '2-digit',
                                    })}
                                </td>
                                <td className="px-3.5 py-3 text-xs">
                                    <span className="font-semibold text-foreground">{log.username || `UID:${log.user_id}`}</span>
                                </td>
                                <td className="px-3.5 py-3 text-xs">
                                    <span className={cn("inline-block px-2.5 py-0.5 rounded-md text-xs font-semibold", getActionClass(log.action))}>
                                        {log.action}
                                    </span>
                                </td>
                                <td className="px-3.5 py-3 text-xs">
                                    <span className="text-muted-foreground">{log.module || '-'}</span>
                                    {log.entity_type && (
                                        <span className="text-muted-foreground/50 text-xs ml-1">
                                            / {log.entity_type}#{log.entity_id}
                                        </span>
                                    )}
                                </td>
                                <td className="px-3.5 py-3 text-xs max-w-[300px]">
                                    <span className="text-foreground/80 leading-tight block overflow-hidden text-ellipsis whitespace-nowrap">
                                        {log.summary || '-'}
                                    </span>
                                </td>
                                <td className="px-3.5 py-3 text-xs">
                                    <span className="text-muted-foreground text-xs font-mono">{log.ip_address || '-'}</span>
                                </td>
                                <td className="px-3.5 py-3 text-xs text-center">
                                    {(hasSnapshot(log.before_snapshot) || hasSnapshot(log.after_snapshot)) ? (
                                        <button className="px-2.5 py-1 rounded-md border border-border bg-card cursor-pointer text-xs text-muted-foreground hover:bg-secondary transition-colors" onClick={() => setDetail(log)}>查看</button>
                                    ) : (
                                        <span className="text-border">—</span>
                                    )}
                                </td>
                            </tr>
                        ))}
                    </tbody>
                </table>
            </div>

            {/* 分页 */}
            {totalPages > 1 && (
                <div className="flex justify-center items-center gap-4 mt-4 py-3">
                    <button className="px-4 py-1.5 rounded-lg border border-border bg-card cursor-pointer text-xs text-muted-foreground hover:bg-secondary transition-colors disabled:opacity-50 disabled:cursor-not-allowed" disabled={page <= 1} onClick={() => setPage(p => p - 1)}>← 上一页</button>
                    <span className="text-xs text-muted-foreground">第 {page} / {totalPages} 页</span>
                    <button className="px-4 py-1.5 rounded-lg border border-border bg-card cursor-pointer text-xs text-muted-foreground hover:bg-secondary transition-colors disabled:opacity-50 disabled:cursor-not-allowed" disabled={page >= totalPages} onClick={() => setPage(p => p + 1)}>下一页 →</button>
                </div>
            )}

            {/* 详情弹窗 */}
            {detail && (
                <div className="fixed inset-0 bg-black/40 flex justify-center items-center z-9999" onClick={() => setDetail(null)}>
                    <div className="bg-card rounded-xl w-full max-w-[95vw] sm:w-[560px] max-h-[85vh] overflow-auto border border-border" onClick={e => e.stopPropagation()}>
                        <div className="flex justify-between items-center px-4 sm:px-6 py-4 sm:py-5 border-b border-border">
                            <h2 className="text-lg font-bold m-0 text-foreground">审计详情 #{detail.id}</h2>
                            <button className="bg-transparent border-none text-lg cursor-pointer text-muted-foreground hover:text-foreground" onClick={() => setDetail(null)}>✕</button>
                        </div>
                        <div className="px-4 sm:px-6 py-4 sm:py-5">
                            <div className="flex gap-3 py-2 border-b border-border/30 text-sm">
                                <span className="font-semibold text-muted-foreground min-w-[60px] shrink-0">时间</span>
                                <span className="text-foreground">{new Date(detail.created_at).toLocaleString('zh-CN')}</span>
                            </div>
                            <div className="flex gap-3 py-2 border-b border-border/30 text-sm">
                                <span className="font-semibold text-muted-foreground min-w-[60px] shrink-0">用户</span>
                                <span className="text-foreground">{detail.username} (ID: {detail.user_id})</span>
                            </div>
                            <div className="flex gap-3 py-2 border-b border-border/30 text-sm">
                                <span className="font-semibold text-muted-foreground min-w-[60px] shrink-0">操作</span>
                                <span className="text-foreground">{detail.action}</span>
                            </div>
                            <div className="flex gap-3 py-2 border-b border-border/30 text-sm">
                                <span className="font-semibold text-muted-foreground min-w-[60px] shrink-0">模块</span>
                                <span className="text-foreground">{detail.module} / {detail.entity_type}#{detail.entity_id}</span>
                            </div>
                            <div className="flex gap-3 py-2 border-b border-border/30 text-sm">
                                <span className="font-semibold text-muted-foreground min-w-[60px] shrink-0">摘要</span>
                                <span className="text-foreground">{detail.summary}</span>
                            </div>
                            <div className="flex gap-3 py-2 border-b border-border/30 text-sm">
                                <span className="font-semibold text-muted-foreground min-w-[60px] shrink-0">IP</span>
                                <span className="font-mono text-foreground">{detail.ip_address}</span>
                            </div>
                            {hasSnapshot(detail.before_snapshot) && (
                                <div>
                                    <p className="text-xs font-semibold text-muted-foreground mt-4 mb-1.5">修改前 (Before)</p>
                                    <pre className="bg-secondary rounded-xl p-3.5 text-xs font-mono overflow-auto max-h-[200px] whitespace-pre-wrap leading-relaxed border border-border">{tryFormat(detail.before_snapshot)}</pre>
                                    <details className="mt-1 text-xs text-muted-foreground">
                                        <summary className="cursor-pointer">查看原始记录</summary>
                                        <pre className="mt-1 bg-muted rounded-lg p-2 font-mono overflow-auto max-h-[160px] whitespace-pre-wrap border border-border/60">{rawFormat(detail.before_snapshot)}</pre>
                                    </details>
                                </div>
                            )}
                            {hasSnapshot(detail.after_snapshot) && (
                                <div>
                                    <p className="text-xs font-semibold text-muted-foreground mt-4 mb-1.5">修改后 (After)</p>
                                    <pre className="bg-secondary rounded-xl p-3.5 text-xs font-mono overflow-auto max-h-[200px] whitespace-pre-wrap leading-relaxed border border-border">{tryFormat(detail.after_snapshot)}</pre>
                                    <details className="mt-1 text-xs text-muted-foreground">
                                        <summary className="cursor-pointer">查看原始记录</summary>
                                        <pre className="mt-1 bg-muted rounded-lg p-2 font-mono overflow-auto max-h-[160px] whitespace-pre-wrap border border-border/60">{rawFormat(detail.after_snapshot)}</pre>
                                    </details>
                                </div>
                            )}
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
}

// JSON 格式化：后端可能已经把 snapshot 解析成对象，不能直接交给 React 渲染。
function hasSnapshot(value: unknown): boolean {
    return value != null && value !== '';
}

function tryFormat(value: unknown): string {
    return humanizeAuditSnapshot(value);
}

function rawFormat(value: unknown): string {
    return formatRawAuditSnapshot(value);
}
