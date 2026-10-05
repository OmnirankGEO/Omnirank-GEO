/**
 * 统一知识库管理页面
 * 支持公共库/客户库/角色库的文档上传、查看、删除
 */

import { useState, useEffect, useCallback, useRef } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Input } from '@/components/ui/input';
import {
    Database, Upload, Trash2, FileText, RefreshCw,
    Building2, Users, Globe, AlertCircle, File
} from 'lucide-react';
import { toast } from 'sonner';
import { knowledgeApi, KBDocument, KBStats, authFetch } from '@/lib/api';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

export default function KnowledgeManager() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const [activeTab, setActiveTab] = useState('public');
    const [stats, setStats] = useState<KBStats | null>(null);
    const [documents, setDocuments] = useState<KBDocument[]>([]);
    const [loading, setLoading] = useState(false);
    const [uploading, setUploading] = useState(false);

    // 文件上传引用
    const fileInputRef = useRef<HTMLInputElement>(null);
    const [selectedFile, setSelectedFile] = useState<File | null>(null);

    // 角色库表单
    const [roleForm, setRoleForm] = useState({
        kb_id: '',
        role_type: 'advisor' as 'advisor' | 'employee',
    });
    // 角色列表（下拉选择用）
    const [roleOptions, setRoleOptions] = useState<{ id: string; name: string }[]>([]);
    useEffect(() => {
        if (roleForm.role_type === 'advisor') {
            authFetch('/api/advisors').then(r => r.json())
                .then(data => {
                    const list = Array.isArray(data) ? data : data.advisors || [];
                    setRoleOptions(list.map((a: any) => ({ id: a.id, name: a.name || a.id })));
                })
                .catch(() => setRoleOptions([]));
        } else {
            authFetch('/api/employees').then(r => r.json())
                .then(data => {
                    const list = Array.isArray(data) ? data : data.employees || [];
                    setRoleOptions(list.map((e: any) => ({ id: e.id || e.employee_id, name: e.name || e.id })));
                })
                .catch(() => setRoleOptions([]));
        }
        setRoleForm(prev => ({ ...prev, kb_id: '' }));
    }, [roleForm.role_type]);

    // 加载统计信息
    const loadStats = useCallback(async () => {
        try {
            const res = await knowledgeApi.getStats();
            if (res.data.status === 'success') {
                setStats(res.data.stats);
            }
        } catch (e) {
            console.error('加载统计失败', e);
        }
    }, []);

    // 加载公共库文档列表
    const loadPublicDocuments = useCallback(async () => {
        setLoading(true);
        try {
            const res = await knowledgeApi.listPublicDocuments();
            if (res.data.success) {
                setDocuments(res.data.documents || []);
            } else {
                setDocuments([]);
            }
        } catch (e) {
            console.error('加载公共库文档失败', e);
            setDocuments([]);
        } finally {
            setLoading(false);
        }
    }, []);

    // 加载角色库文档列表
    const loadRoleDocuments = useCallback(async () => {
        if (!roleForm.kb_id) {
            setDocuments([]);
            return;
        }
        setLoading(true);
        try {
            const res = await knowledgeApi.listRoleDocuments(roleForm.role_type, roleForm.kb_id);
            if (res.data.success) {
                setDocuments(res.data.documents || []);
            } else {
                setDocuments([]);
            }
        } catch (e) {
            console.error('加载角色库文档失败', e);
            setDocuments([]);
        } finally {
            setLoading(false);
        }
    }, [roleForm.kb_id, roleForm.role_type]);

    // 根据当前Tab加载文档
    const loadDocuments = useCallback(async () => {
        if (activeTab === 'public') {
            await loadPublicDocuments();
        } else if (activeTab === 'role') {
            await loadRoleDocuments();
        }
    }, [activeTab, loadPublicDocuments, loadRoleDocuments]);

    // 处理文件选择
    const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (file) {
            setSelectedFile(file);
        }
    };

    // 上传公共库文档（文件上传）
    const handlePublicUpload = async () => {
        if (!selectedFile) {
            toast.error('请选择要上传的文件');
            return;
        }

        setUploading(true);
        try {
            const res = await knowledgeApi.uploadPublicDocument(selectedFile);
            if (res.data.success) {
                toast.success('上传成功');
                setSelectedFile(null);
                if (fileInputRef.current) fileInputRef.current.value = '';
                loadPublicDocuments();
                loadStats();
            } else {
                toast.error('上传失败');
            }
        } catch (e: any) {
            console.error('上传失败:', e);
            toast.error(e?.response?.data?.detail || '上传失败');
        } finally {
            setUploading(false);
        }
    };

    // 上传角色库文档（文件上传）
    const handleRoleUpload = async () => {
        if (!selectedFile) {
            toast.error('请选择要上传的文件');
            return;
        }
        if (!roleForm.kb_id) {
            toast.error('请输入角色ID');
            return;
        }

        setUploading(true);
        try {
            const res = await knowledgeApi.uploadRoleDocument(
                roleForm.role_type,
                roleForm.kb_id,
                selectedFile
            );
            if (res.data.success) {
                toast.success('上传成功');
                setSelectedFile(null);
                if (fileInputRef.current) fileInputRef.current.value = '';
                loadRoleDocuments();
                loadStats();
            } else {
                toast.error('上传失败');
            }
        } catch (e: any) {
            console.error('上传失败:', e);
            toast.error(e?.response?.data?.detail || '上传失败');
        } finally {
            setUploading(false);
        }
    };

    // 删除公共库文档
    const handlePublicDelete = async (filename: string) => {
        if (!(await askConfirm({ title: `确定删除 ${filename}？`, danger: true }))) return;

        try {
            const res = await knowledgeApi.deletePublicDocument(filename);
            if (res.data.success) {
                toast.success('删除成功');
                loadPublicDocuments();
                loadStats();
            } else {
                toast.error('删除失败');
            }
        } catch (e) {
            toast.error('删除失败');
        }
    };

    // 删除角色库文档
    const handleRoleDelete = async (filename: string) => {
        if (!(await askConfirm({ title: `确定删除 ${filename}？`, danger: true }))) return;

        try {
            const res = await knowledgeApi.deleteRoleDocument(
                roleForm.role_type,
                roleForm.kb_id,
                filename
            );
            if (res.data.success) {
                toast.success('删除成功');
                loadRoleDocuments();
                loadStats();
            } else {
                toast.error('删除失败');
            }
        } catch (e) {
            toast.error('删除失败');
        }
    };

    useEffect(() => {
        loadStats();
    }, [loadStats]);

    useEffect(() => {
        loadDocuments();
        // 切换Tab时清除选中的文件
        setSelectedFile(null);
        if (fileInputRef.current) fileInputRef.current.value = '';
    }, [activeTab]);

    // 格式化文件大小
    const formatSize = (bytes: number) => {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
    };

    // 渲染文档列表
    const renderDocumentList = (onDelete: (filename: string) => void) => (
        <div className="mt-6">
            <h3 className="font-medium mb-3">文档列表</h3>
            {loading ? (
                <div className="text-center py-8 text-gray-500">加载中...</div>
            ) : documents.length === 0 ? (
                <div className="text-center py-8 text-gray-400 flex flex-col items-center gap-2">
                    <AlertCircle className="w-8 h-8" />
                    <p>暂无文档</p>
                </div>
            ) : (
                <div className="space-y-2">
                    {documents.map(doc => (
                        <div key={doc.filename}
                            className="flex flex-col sm:flex-row sm:items-center sm:justify-between p-3 bg-secondary rounded-lg hover:bg-secondary/80 gap-2"
                        >
                            <div className="flex items-center gap-2 sm:gap-3">
                                <FileText className="w-5 h-5 text-blue-500" />
                                <div>
                                    <p className="font-medium">{doc.filename}</p>
                                    <p className="text-xs text-gray-500">
                                        {formatSize(doc.size)} · {new Date(doc.modified).toLocaleString()}
                                    </p>
                                </div>
                            </div>
                            <Button
                                variant="ghost"
                                size="sm"
                                className="text-red-500 hover:text-red-700"
                                onClick={() => onDelete(doc.filename)}
                            >
                                <Trash2 className="w-4 h-4" />
                            </Button>
                        </div>
                    ))}
                </div>
            )}
        </div>
    );

    // 渲染文件上传区域
    const renderFileUpload = (onUpload: () => void) => (
        <div className="mt-6 border-t pt-6">
            <h3 className="font-medium mb-3">上传文档</h3>
            <div className="space-y-4">
                <div className="flex flex-wrap items-center gap-2 sm:gap-4">
                    <input
                        ref={fileInputRef}
                        type="file"
                        accept=".md,.txt,.json,.pdf"
                        onChange={handleFileSelect}
                        className="hidden"
                        id="file-upload"
                    />
                    <label
                        htmlFor="file-upload"
                        className="flex items-center gap-2 px-4 py-2 border-2 border-dashed border-border rounded-lg cursor-pointer hover:border-primary/40 hover:bg-primary/5 transition-colors"
                    >
                        <File className="w-5 h-5 text-gray-500" />
                        <span className="text-sm text-gray-600">
                            {selectedFile ? selectedFile.name : '选择文件 (.md, .txt, .json, .pdf, .docx, .xlsx)'}
                        </span>
                    </label>
                    {selectedFile && (
                        <span className="text-sm text-gray-500">
                            {formatSize(selectedFile.size)}
                        </span>
                    )}
                </div>
                <Button onClick={onUpload} disabled={uploading || !selectedFile}>
                    <Upload className="w-4 h-4 mr-2" />
                    {uploading ? '上传中...' : '上传文档'}
                </Button>
                <p className="text-xs text-gray-500">
                    📌 支持 Markdown (.md)、文本 (.txt)、JSON (.json)、PDF (.pdf) 格式
                </p>
            </div>
        </div>
    );

    return (
        <div className="p-6 max-w-6xl mx-auto space-y-6">
            {/* 页面标题 */}
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                <div className="flex items-center gap-2 sm:gap-3">
                    <Database className="w-8 h-8 text-blue-600" />
                    <div>
                        <h1 className="text-2xl font-bold text-gray-900">知识库管理</h1>
                        <p className="text-sm text-gray-500">管理公共、客户和角色专属知识</p>
                    </div>
                </div>
                <Button variant="outline" onClick={() => { loadStats(); loadDocuments(); }}>
                    <RefreshCw className="w-4 h-4 mr-2" />
                    刷新
                </Button>
            </div>

            {/* 统计卡片 */}
            {stats && (
                <div className="grid grid-cols-3 gap-4">
                    <Card>
                        <CardContent className="p-4 flex items-center gap-4">
                            <Globe className="w-10 h-10 text-green-500" />
                            <div>
                                <p className="text-2xl font-bold">{stats.public?.file_count ?? 0}</p>
                                <p className="text-sm text-gray-500">公共库文档</p>
                            </div>
                        </CardContent>
                    </Card>
                    <Card>
                        <CardContent className="p-4 flex items-center gap-4">
                            <Building2 className="w-10 h-10 text-blue-500" />
                            <div>
                                <p className="text-2xl font-bold">{Object.keys(stats.clients || {}).length}</p>
                                <p className="text-sm text-gray-500">客户知识库</p>
                            </div>
                        </CardContent>
                    </Card>
                    <Card>
                        <CardContent className="p-4 flex items-center gap-4">
                            <Users className="w-10 h-10 text-purple-500" />
                            <div>
                                <p className="text-2xl font-bold">
                                    {Object.keys(stats.roles?.advisors || {}).length + Object.keys(stats.roles?.employees || {}).length}
                                </p>
                                <p className="text-sm text-gray-500">角色私有库</p>
                            </div>
                        </CardContent>
                    </Card>
                </div>
            )}

            {/* 知识库管理 */}
            <Card>
                <CardHeader>
                    <CardTitle>知识库管理</CardTitle>
                </CardHeader>
                <CardContent>
                    <Tabs value={activeTab} onValueChange={setActiveTab}>
                        <TabsList className="mb-4">
                            <TabsTrigger value="public">
                                <Globe className="w-4 h-4 mr-2" />
                                公共库
                            </TabsTrigger>
                            <TabsTrigger value="client">
                                <Building2 className="w-4 h-4 mr-2" />
                                客户库
                            </TabsTrigger>
                            <TabsTrigger value="role">
                                <Users className="w-4 h-4 mr-2" />
                                角色库
                            </TabsTrigger>
                        </TabsList>

                        {/* 公共库 */}
                        <TabsContent value="public" className="space-y-4">
                            <div className="bg-emerald-500/10 p-3 rounded-lg text-sm text-emerald-700 dark:text-emerald-400">
                                公共知识库中的内容将被所有顾问和员工共享使用
                            </div>
                            {renderDocumentList(handlePublicDelete)}
                            {renderFileUpload(handlePublicUpload)}
                        </TabsContent>

                        {/* 客户库 - 引导去客户管理 */}
                        <TabsContent value="client" className="space-y-4">
                            <div className="bg-blue-500/10 p-6 rounded-lg text-center">
                                <Building2 className="w-12 h-12 text-blue-500 mx-auto mb-3" />
                                <h3 className="text-lg font-medium text-foreground mb-2">客户知识库已迁移至客户管理</h3>
                                <p className="text-sm text-muted-foreground mb-4">
                                    为简化操作，客户专属知识库现在在客户详情页面中管理。<br />
                                    点击下方按钮进入客户列表，选择客户后在"知识库"Tab中管理。
                                </p>
                                <a href="/brands">
                                    <Button>
                                        <Building2 className="w-4 h-4 mr-2" />
                                        去客户管理
                                    </Button>
                                </a>
                            </div>
                        </TabsContent>

                        {/* 角色库 */}
                        <TabsContent value="role" className="space-y-4">
                            <div className="bg-purple-500/10 p-3 rounded-lg text-sm text-purple-700 dark:text-purple-400">
                                角色私有知识库，仅对应顾问/员工可使用
                            </div>
                            <div className="flex gap-4 items-center flex-wrap">
                                <label className="text-sm font-medium">角色类型:</label>
                                <select
                                    className="border rounded px-3 py-2"
                                    value={roleForm.role_type}
                                    onChange={e => setRoleForm(prev => ({
                                        ...prev,
                                        role_type: e.target.value as 'advisor' | 'employee'
                                    }))}
                                >
                                    <option value="advisor">顾问</option>
                                    <option value="employee">员工</option>
                                </select>
                                <label className="text-sm font-medium">选择角色:</label>
                                <select
                                    className="border rounded px-3 py-2"
                                    value={roleForm.kb_id}
                                    onChange={e => {
                                        setRoleForm(prev => ({ ...prev, kb_id: e.target.value }));
                                    }}
                                >
                                    <option value="">请选择</option>
                                    {roleOptions.map(opt => (
                                        <option key={opt.id} value={opt.id}>{opt.name} ({opt.id})</option>
                                    ))}
                                </select>
                                <Button variant="outline" size="sm" onClick={loadRoleDocuments} disabled={!roleForm.kb_id}>
                                    加载
                                </Button>
                            </div>
                            {renderDocumentList(handleRoleDelete)}
                            {renderFileUpload(handleRoleUpload)}
                        </TabsContent>
                    </Tabs>
                </CardContent>
            </Card>
          {confirmDialog}
        </div>
    );
}
