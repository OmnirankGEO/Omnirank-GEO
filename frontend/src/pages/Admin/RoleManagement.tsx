/**
 * 角色管理页面
 * 角色列表 + 创建/编辑角色 + 权限矩阵编辑器
 */
import React, { useState, useEffect, useCallback } from 'react';
import { Shield } from 'lucide-react';
import { authApi } from '@/context/AuthContext';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

// ========== 类型 ==========
interface Permission {
    module: string;
    level: string;
}

interface RoleItem {
    id: number;
    name: string;
    display_name: string;
    description: string;
    is_system: number;
    permissions: Permission[];
    user_count: number;
    created_at: string;
}

interface ModuleMeta {
    id: string;
    label: string;
    levels: string[];
}

const LEVEL_LABELS: Record<string, string> = {
    read: '查看',
    write: '编辑',
    delete: '删除',
};

// ========== 主组件 ==========
export default function RoleManagement() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const [roles, setRoles] = useState<RoleItem[]>([]);
    const [modules, setModules] = useState<ModuleMeta[]>([]);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');

    // 弹窗
    const [showCreate, setShowCreate] = useState(false);
    const [editRole, setEditRole] = useState<RoleItem | null>(null);

    // 表单
    const [form, setForm] = useState({ name: '', display_name: '', description: '' });
    const [permMatrix, setPermMatrix] = useState<Record<string, boolean>>({});
    const [actionLoading, setActionLoading] = useState(false);
    const [msg, setMsg] = useState('');

    const fetchAll = useCallback(async () => {
        setLoading(true);
        try {
            const [rolesRes, modulesRes] = await Promise.all([
                authApi.get('/api/admin/roles'),
                authApi.get('/api/admin/modules'),
            ]);
            setRoles(rolesRes.data.roles || []);
            setModules(modulesRes.data.modules || []);
        } catch (e: any) {
            setError(e.response?.data?.detail || '加载失败');
        }
        setLoading(false);
    }, []);

    useEffect(() => { fetchAll(); }, [fetchAll]);

    const permKey = (module: string, level: string) => `${module}:${level}`;

    const buildMatrix = (perms: Permission[]) => {
        const m: Record<string, boolean> = {};
        perms.forEach(p => { m[permKey(p.module, p.level)] = true; });
        return m;
    };

    const matrixToPerms = (): string[][] => {
        return Object.entries(permMatrix)
            .filter(([, v]) => v)
            .map(([k]) => {
                const [module, level] = k.split(':');
                return [module, level];
            });
    };

    // 创建角色
    const handleCreate = async () => {
        if (!form.name || !form.display_name) {
            setMsg('请填写角色标识和显示名'); return;
        }
        setActionLoading(true);
        try {
            await authApi.post('/api/admin/roles', {
                name: form.name,
                display_name: form.display_name,
                description: form.description,
                permissions: matrixToPerms(),
            });
            setShowCreate(false);
            setForm({ name: '', display_name: '', description: '' });
            setPermMatrix({});
            setMsg('');
            fetchAll();
        } catch (e: any) {
            setMsg(e.response?.data?.detail || '创建失败');
        }
        setActionLoading(false);
    };

    // 编辑角色
    const handleEdit = async () => {
        if (!editRole) return;
        setActionLoading(true);
        try {
            await authApi.put(`/api/admin/roles/${editRole.id}`, {
                display_name: form.display_name,
                description: form.description,
                permissions: matrixToPerms(),
            });
            setEditRole(null);
            setForm({ name: '', display_name: '', description: '' });
            setPermMatrix({});
            setMsg('');
            fetchAll();
        } catch (e: any) {
            setMsg(e.response?.data?.detail || '修改失败');
        }
        setActionLoading(false);
    };

    // 删除角色
    const handleDelete = async (role: RoleItem) => {
        if (role.is_system) { toast.error('系统内置角色不可删除'); return; }
        if (!(await askConfirm({ title: `确定删除角色 "${role.display_name}"？`, danger: true }))) return;
        try {
            await authApi.delete(`/api/admin/roles/${role.id}`);
            fetchAll();
        } catch (e: any) {
            toast.error(e.response?.data?.detail || '删除失败');
        }
    };

    const openEdit = (role: RoleItem) => {
        setEditRole(role);
        setForm({ name: role.name, display_name: role.display_name, description: role.description || '' });
        setPermMatrix(buildMatrix(role.permissions));
    };

    const openCreate = () => {
        setShowCreate(true);
        setForm({ name: '', display_name: '', description: '' });
        setPermMatrix({});
        setMsg('');
    };

    const toggleRow = (moduleId: string, levels: string[]) => {
        const allChecked = levels.every(l => permMatrix[permKey(moduleId, l)]);
        const next = { ...permMatrix };
        levels.forEach(l => { next[permKey(moduleId, l)] = !allChecked; });
        setPermMatrix(next);
    };

    if (loading) return <div className="text-center py-20 text-brand text-base">加载中...</div>;
    if (error) return <div className="text-center py-20 text-destructive text-base">❌ {error}</div>;

    return (
        <div className="p-4 md:p-6">
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2 mb-6">
                <div className="flex items-center gap-2 sm:gap-3">
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                        <Shield className="h-5 w-5 text-brand" />
                    </div>
                    <div>
                        <h1 className="text-2xl font-bold text-foreground m-0">角色管理</h1>
                        <p className="text-sm text-muted-foreground mt-1">共 {roles.length} 个角色</p>
                    </div>
                </div>
                <button className="px-5 py-2.5 rounded-xl border-none cursor-pointer bg-brand text-white text-sm font-semibold hover:bg-brand-hover transition-colors" onClick={openCreate}>+ 创建角色</button>
            </div>

            {/* 角色卡片 */}
            <div className="grid grid-cols-[repeat(auto-fill,minmax(340px,1fr))] gap-4">
                {roles.map(role => (
                    <div key={role.id} className="bg-card rounded-xl p-6 border border-border hover:border-foreground/20 transition-all">
                        <div className="flex justify-between items-start mb-2">
                            <div>
                                <h3 className="text-base font-bold text-foreground m-0">{role.display_name}</h3>
                                <p className="text-xs text-muted-foreground mt-0.5">{role.name}</p>
                            </div>
                            {role.is_system ? (
                                <span className="px-2.5 py-0.5 rounded-lg bg-green-50 text-green-600 text-[11px] font-semibold">系统</span>
                            ) : (
                                <span className="px-2.5 py-0.5 rounded-lg bg-violet-50 text-violet-700 text-[11px] font-semibold">自定义</span>
                            )}
                        </div>
                        <p className="text-[13px] text-muted-foreground my-2 leading-relaxed">{role.description || '无描述'}</p>
                        <div className="flex gap-4 text-xs text-muted-foreground mb-3">
                            <span>👥 {role.user_count} 个用户</span>
                            <span>🔐 {role.permissions.length} 项权限</span>
                        </div>
                        <div className="flex flex-wrap gap-1 mb-4">
                            {role.permissions.slice(0, 6).map(p => (
                                <span key={`${p.module}:${p.level}`} className="px-2 py-0.5 rounded-md bg-brand-light text-green-700 text-[11px]">
                                    {p.module}:{LEVEL_LABELS[p.level] || p.level}
                                </span>
                            ))}
                            {role.permissions.length > 6 && (
                                <span className="px-2 py-0.5 rounded-md bg-secondary text-muted-foreground text-[11px]">
                                    +{role.permissions.length - 6}
                                </span>
                            )}
                        </div>
                        <div className="flex gap-2">
                            <button className="px-3.5 py-1.5 rounded-lg border border-border bg-card cursor-pointer text-[13px] text-muted-foreground hover:bg-secondary transition-colors" onClick={() => openEdit(role)}>编辑权限</button>
                            {!role.is_system && (
                                <button className="px-3.5 py-1.5 rounded-lg border border-destructive bg-destructive cursor-pointer text-[13px] text-white hover:bg-destructive/90 transition-colors" onClick={() => handleDelete(role)}>删除</button>
                            )}
                        </div>
                    </div>
                ))}
            </div>

            {/* 创建角色弹窗 */}
            {showCreate && (
                <MatrixModal
                    title="创建角色"
                    modules={modules}
                    form={form}
                    setForm={setForm}
                    permMatrix={permMatrix}
                    setPermMatrix={setPermMatrix}
                    permKey={permKey}
                    toggleRow={toggleRow}
                    showNameField
                    msg={msg}
                    loading={actionLoading}
                    onSubmit={handleCreate}
                    onClose={() => setShowCreate(false)}
                />
            )}

            {/* 编辑角色弹窗 */}
            {editRole && (
                <MatrixModal
                    title={`编辑角色: ${editRole.display_name}`}
                    modules={modules}
                    form={form}
                    setForm={setForm}
                    permMatrix={permMatrix}
                    setPermMatrix={setPermMatrix}
                    permKey={permKey}
                    toggleRow={toggleRow}
                    showNameField={false}
                    msg={msg}
                    loading={actionLoading}
                    onSubmit={handleEdit}
                    onClose={() => setEditRole(null)}
                />
            )}
          {confirmDialog}
        </div>
    );
}

// ========== 权限矩阵弹窗 ==========
function MatrixModal({
    title, modules, form, setForm, permMatrix, setPermMatrix, permKey, toggleRow,
    showNameField, msg, loading, onSubmit, onClose,
}: {
    title: string;
    modules: ModuleMeta[];
    form: { name: string; display_name: string; description: string };
    setForm: (f: any) => void;
    permMatrix: Record<string, boolean>;
    setPermMatrix: (m: Record<string, boolean>) => void;
    permKey: (m: string, l: string) => string;
    toggleRow: (m: string, levels: string[]) => void;
    showNameField: boolean;
    msg: string;
    loading: boolean;
    onSubmit: () => void;
    onClose: () => void;
}) {
    const allLevels = ['read', 'write', 'delete'];

    return (
        <div className="fixed inset-0 bg-black/40 flex justify-center items-center z-9999" onClick={onClose}>
            <div className="bg-card rounded-xl w-[640px] max-h-[85vh] overflow-auto border border-border" onClick={e => e.stopPropagation()}>
                <div className="flex justify-between items-center px-6 py-5 border-b border-border">
                    <h2 className="text-lg font-bold m-0 text-foreground">{title}</h2>
                    <button className="bg-transparent border-none text-lg cursor-pointer text-muted-foreground hover:text-foreground" onClick={onClose}>✕</button>
                </div>
                <div className="px-6 py-5 flex flex-col gap-3.5">
                    {/* 基本信息 */}
                    {showNameField && (
                        <div className="flex flex-col gap-1">
                            <label className="text-[13px] font-semibold text-muted-foreground">角色标识 (英文)</label>
                            <input className="px-3.5 py-2.5 rounded-xl border border-border text-sm outline-hidden bg-secondary focus:ring-2 focus:ring-brand text-foreground" value={form.name}
                                onChange={e => setForm({ ...form, name: e.target.value })}
                                placeholder="如 content_editor" />
                        </div>
                    )}
                    <div className="flex flex-col gap-1">
                        <label className="text-[13px] font-semibold text-muted-foreground">显示名称</label>
                        <input className="px-3.5 py-2.5 rounded-xl border border-border text-sm outline-hidden bg-secondary focus:ring-2 focus:ring-brand text-foreground" value={form.display_name}
                            onChange={e => setForm({ ...form, display_name: e.target.value })}
                            placeholder="如 内容编辑" />
                    </div>
                    <div className="flex flex-col gap-1">
                        <label className="text-[13px] font-semibold text-muted-foreground">描述</label>
                        <input className="px-3.5 py-2.5 rounded-xl border border-border text-sm outline-hidden bg-secondary focus:ring-2 focus:ring-brand text-foreground" value={form.description}
                            onChange={e => setForm({ ...form, description: e.target.value })}
                            placeholder="可选" />
                    </div>

                    {/* 权限矩阵 */}
                    <div className="mt-2">
                        <label className="text-[13px] font-semibold text-muted-foreground">权限矩阵</label>
                        <div className="rounded-xl border border-border overflow-hidden mt-2">
                            <table className="w-full border-collapse">
                                <thead>
                                    <tr>
                                        <th className="px-3 py-2.5 text-left text-xs text-muted-foreground font-semibold bg-muted border-b border-border">模块</th>
                                        {allLevels.map(l => (
                                            <th key={l} className="px-3 py-2.5 text-center text-xs text-muted-foreground font-semibold bg-muted border-b border-border w-[70px]">
                                                {LEVEL_LABELS[l]}
                                            </th>
                                        ))}
                                        <th className="px-3 py-2.5 text-center text-xs text-muted-foreground font-semibold bg-muted border-b border-border w-[60px]">全选</th>
                                    </tr>
                                </thead>
                                <tbody>
                                    {modules.map(mod => (
                                        <tr key={mod.id}>
                                            <td className="px-3 py-2.5 text-[13px] border-b border-border/50">
                                                <span className="font-semibold text-foreground">{mod.label}</span>
                                                <span className="text-muted-foreground/40 text-[11px] ml-1.5">{mod.id}</span>
                                            </td>
                                            {allLevels.map(l => {
                                                const available = mod.levels.includes(l);
                                                const key = permKey(mod.id, l);
                                                return (
                                                    <td key={l} className="px-3 py-2.5 text-center border-b border-border/50">
                                                        {available ? (
                                                            <input
                                                                type="checkbox"
                                                                checked={!!permMatrix[key]}
                                                                onChange={e => setPermMatrix({ ...permMatrix, [key]: e.target.checked })}
                                                                className="w-4 h-4 cursor-pointer"
                                                            />
                                                        ) : (
                                                            <span className="text-border">—</span>
                                                        )}
                                                    </td>
                                                );
                                            })}
                                            <td className="px-3 py-2.5 text-center border-b border-border/50">
                                                <button
                                                    className="bg-transparent border-none cursor-pointer text-sm text-brand hover:text-brand-hover"
                                                    onClick={() => toggleRow(mod.id, mod.levels)}
                                                >
                                                    ☑
                                                </button>
                                            </td>
                                        </tr>
                                    ))}
                                </tbody>
                            </table>
                        </div>
                    </div>

                    {msg && <p className="text-destructive text-[13px] m-0">{msg}</p>}

                    <button className="px-5 py-2.5 rounded-xl border-none cursor-pointer bg-brand text-white text-sm font-semibold hover:bg-brand-hover transition-colors disabled:opacity-50" onClick={onSubmit} disabled={loading}>
                        {loading ? '提交中...' : '保存'}
                    </button>
                </div>
            </div>
        </div>
    );
}
