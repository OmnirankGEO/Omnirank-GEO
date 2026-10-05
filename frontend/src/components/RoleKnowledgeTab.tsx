/**
 * 角色知识库管理Tab组件
 * 可复用于顾问和员工的知识库管理
 */

import { useState, useEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { AlertCircle, FileText, Trash2, Upload, RefreshCw, BookOpen } from 'lucide-react';
import { knowledgeApi, type KBDocument } from '@/lib/api';
import { cn, extractErrorMessage } from '@/lib/utils';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

interface RoleKnowledgeTabProps {
    roleType: 'advisor' | 'employee';
    roleId: string;
    roleName: string;
}

export function RoleKnowledgeTab({ roleType, roleId, roleName }: RoleKnowledgeTabProps) {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const [documents, setDocuments] = useState<KBDocument[]>([]);
    const [loading, setLoading] = useState(true);
    const [uploading, setUploading] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    // 加载文档列表
    const loadDocuments = async () => {
        setLoading(true);
        setError(null);
        try {
            const res = await knowledgeApi.listRoleDocuments(roleType, roleId);
            setDocuments(res.data.documents || []);
        } catch (err: any) {
            setError(err.response?.data?.detail || '加载失败');
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadDocuments();
    }, [roleType, roleId]);

    // 上传文件
    const handleUpload = async (e: React.ChangeEvent<HTMLInputElement>) => {
        const file = e.target.files?.[0];
        if (!file) return;

        const allowedTypes = ['.md', '.txt', '.json', '.jsonl'];
        const ext = file.name.toLowerCase().substring(file.name.lastIndexOf('.'));
        if (!allowedTypes.includes(ext)) {
            setError(`不支持的文件类型: ${ext}。支持: ${allowedTypes.join(', ')}`);
            return;
        }

        setUploading(true);
        setError(null);
        try {
            await knowledgeApi.uploadRoleDocument(roleType, roleId, file);
            await loadDocuments();
        } catch (err: any) {
            setError(extractErrorMessage(err?.response?.data || err, '上传失败'));
        } finally {
            setUploading(false);
            if (fileInputRef.current) {
                fileInputRef.current.value = '';
            }
        }
    };

    // 删除文件
    const handleDelete = async (filename: string) => {
        if (!(await askConfirm({ title: `确定删除「${filename}」？`, description: '此操作不可撤销。', confirmLabel: '删除', danger: true }))) return;

        try {
            await knowledgeApi.deleteRoleDocument(roleType, roleId, filename);
            await loadDocuments();
        } catch (err: any) {
            setError(err.response?.data?.detail || '删除失败');
        }
    };

    const formatSize = (bytes: number) => {
        if (bytes < 1024) return `${bytes} B`;
        if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
        return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
    };

    return (
        <div className="space-y-4">
            {/* 顶部操作栏 */}
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <BookOpen className="size-4" />
                    <span>{documents.length} 份文档</span>
                </div>
                <div className="flex gap-2">
                    <Button
                        variant="ghost"
                        size="sm"
                        className="h-8 rounded-lg text-xs"
                        onClick={loadDocuments}
                        disabled={loading}
                    >
                        <RefreshCw className={cn('size-3.5 mr-1', loading && 'animate-spin')} />
                        刷新
                    </Button>
                    <Button
                        size="sm"
                        className="h-8 rounded-lg text-xs bg-foreground text-background hover:bg-foreground/90"
                        onClick={() => fileInputRef.current?.click()}
                        disabled={uploading}
                    >
                        <Upload className={cn('size-3.5 mr-1', uploading && 'animate-pulse')} />
                        {uploading ? '上传中...' : '上传文档'}
                    </Button>
                    <input
                        ref={fileInputRef}
                        type="file"
                        accept=".md,.txt,.json,.jsonl"
                        onChange={handleUpload}
                        className="hidden"
                    />
                </div>
            </div>

            {/* 错误提示 */}
            {error && (
                <div className="p-3 rounded-xl bg-red-500/10 border border-red-500/20 flex items-start gap-2 text-red-400 text-sm">
                    <AlertCircle className="size-4 mt-0.5 shrink-0" />
                    <span>{error}</span>
                </div>
            )}

            {/* 文档列表 */}
            {loading ? (
                <div className="flex items-center justify-center py-12 text-muted-foreground">
                    <RefreshCw className="size-5 animate-spin mr-2" />
                    加载中...
                </div>
            ) : documents.length === 0 ? (
                <div className="flex flex-col items-center justify-center py-12 text-muted-foreground">
                    <BookOpen className="size-8 mb-2 opacity-40" />
                    <p className="text-sm">暂无知识文档</p>
                    <p className="text-xs mt-1 opacity-60">支持 .md / .txt / .json / .jsonl</p>
                </div>
            ) : (
                <div className="space-y-1.5">
                    {documents.map((doc) => (
                        <div
                            key={doc.relative_path}
                            className="flex items-center gap-3 p-2.5 rounded-xl bg-secondary/40 border border-border/40 hover:border-border/80 transition-colors group"
                        >
                            <FileText className="size-4 text-muted-foreground shrink-0" />
                            <div className="flex-1 min-w-0">
                                <p className="text-sm text-foreground truncate">
                                    {doc.filename}
                                </p>
                                <p className="text-[11px] text-muted-foreground/60">
                                    {formatSize(doc.size)} · {new Date(doc.modified).toLocaleDateString('zh-CN')}
                                </p>
                            </div>
                            <Button
                                variant="ghost"
                                size="icon"
                                className="h-7 w-7 rounded-lg text-muted-foreground/40 hover:text-red-400 hover:bg-red-500/10 opacity-0 group-hover:opacity-100 transition-all shrink-0"
                                onClick={() => handleDelete(doc.filename)}
                            >
                                <Trash2 className="size-3.5" />
                            </Button>
                        </div>
                    ))}
                </div>
            )}

            {/* 底部提示 */}
            <p className="text-[11px] text-muted-foreground/40 text-center pt-1">
                上传的文档将自动索引到向量库，AI 对话时优先引用
            </p>
            {confirmDialog}
        </div>
    );
}
