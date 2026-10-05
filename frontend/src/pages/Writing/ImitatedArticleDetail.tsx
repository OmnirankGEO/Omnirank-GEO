import { authFetch } from '@/lib/api';
import { toast } from 'sonner';
import { useState, useEffect } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Loader2, ArrowLeft, Edit2, Save, MessageSquare, RefreshCw, Check } from "lucide-react";
import ReactMarkdown from '@/components/SafeMarkdown';

interface ImitatedArticle {
    id: number;
    imitation_record_id: number;
    reference_id: number;
    quote_id: number;
    keyword: string;
    title: string;
    content: string;
    word_count: number;
    status: string;
    revision_notes: string;
    created_at: string;
    updated_at: string;
}

export function ImitatedArticleDetail() {
    const { articleId } = useParams<{ articleId: string }>();
    const navigate = useNavigate();

    const [article, setArticle] = useState<ImitatedArticle | null>(null);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);

    // 编辑模式
    const [isEditing, setIsEditing] = useState(false);
    const [editTitle, setEditTitle] = useState("");
    const [editContent, setEditContent] = useState("");

    // 修改意见
    const [showRevisionInput, setShowRevisionInput] = useState(false);
    const [revisionNote, setRevisionNote] = useState("");
    const [rewriting, setRewriting] = useState(false);

    // 加载文章详情
    const loadArticle = async () => {
        if (!articleId) return;
        setLoading(true);
        try {
            const res = await authFetch(`/api/imitated-articles/${articleId}`);
            const data = await res.json();
            if (data.success) {
                setArticle(data);
                setEditTitle(data.title || "");
                setEditContent(data.content || "");
            }
        } catch (e) {
            console.error("加载文章失败:", e);
        } finally {
            setLoading(false);
        }
    };

    useEffect(() => {
        loadArticle();
    }, [articleId]);

    // 保存编辑
    const handleSave = async () => {
        if (!article) return;
        setSaving(true);
        try {
            const res = await authFetch(`/api/imitated-articles/${article.id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    title: editTitle,
                    content: editContent
                })
            });
            const data = await res.json();
            if (data.success) {
                setIsEditing(false);
                loadArticle();
            }
        } catch (e) {
            console.error("保存失败:", e);
            toast.error("保存失败");
        } finally {
            setSaving(false);
        }
    };

    // 提交修改意见并重写
    const handleRewrite = async () => {
        if (!article || !revisionNote.trim()) return;
        setRewriting(true);
        try {
            // 调用重写API（需要后续实现）
            const res = await authFetch(`/api/imitated-articles/${article.id}/rewrite`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    revision_note: revisionNote
                })
            });
            const data = await res.json();
            if (data.success) {
                setRevisionNote("");
                setShowRevisionInput(false);
                loadArticle();
            } else {
                toast.error("重写失败: " + (data.detail || "未知错误"));
            }
        } catch (e) {
            console.error("重写失败:", e);
            toast.error("重写请求失败");
        } finally {
            setRewriting(false);
        }
    };

    // 标记为已审核
    const handleApprove = async () => {
        if (!article) return;
        try {
            const res = await authFetch(`/api/imitated-articles/${article.id}`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ status: "approved" })
            });
            const data = await res.json();
            if (data.success) {
                loadArticle();
            }
        } catch (e) {
            console.error("审核失败:", e);
        }
    };

    if (loading) {
        return (
            <div className="flex justify-center py-20">
                <Loader2 className="h-8 w-8 animate-spin text-blue-500" />
            </div>
        );
    }

    if (!article) {
        return (
            <div className="text-center py-20">
                <p className="text-slate-500">文章不存在</p>
                <Button variant="ghost" className="mt-4" onClick={() => navigate(-1)}>
                    返回
                </Button>
            </div>
        );
    }

    return (
        <div className="space-y-4 sm:space-y-6">
            {/* 顶部导航栏 */}
            <div className="flex flex-col sm:flex-row sm:items-center gap-2 sm:gap-4">
                <Button variant="ghost" size="sm" onClick={() => navigate("/references")} className="self-start">
                    <ArrowLeft className="h-4 w-4 mr-1" />
                    返回范文库
                </Button>
                <div className="flex-1 min-w-0">
                    {isEditing ? (
                        <Input
                            value={editTitle}
                            onChange={(e) => setEditTitle(e.target.value)}
                            className="text-lg sm:text-xl font-bold"
                        />
                    ) : (
                        <h1 className="text-lg sm:text-xl font-bold">{article.title}</h1>
                    )}
                </div>
                <Badge variant={article.status === 'approved' ? 'default' : 'outline'} className="self-start sm:self-auto">
                    {article.status === 'approved' ? '已审核' : '草稿'}
                </Badge>
            </div>

            {/* 文章信息 */}
            <div className="flex flex-wrap items-center gap-2 sm:gap-4 text-sm text-slate-500">
                <span>关键词: {article.keyword || "-"}</span>
                <span>·</span>
                <span>{article.word_count || 0} 字</span>
                <span>·</span>
                <span>创建于 {article.created_at?.slice(0, 10)}</span>
            </div>

            {/* 操作按钮栏 */}
            <div className="flex flex-wrap gap-2">
                {isEditing ? (
                    <>
                        <Button onClick={handleSave} disabled={saving}>
                            {saving ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <Save className="h-4 w-4 mr-1" />}
                            保存
                        </Button>
                        <Button variant="outline" onClick={() => {
                            setIsEditing(false);
                            setEditTitle(article.title);
                            setEditContent(article.content);
                        }}>
                            取消
                        </Button>
                    </>
                ) : (
                    <>
                        <Button variant="outline" onClick={() => setIsEditing(true)}>
                            <Edit2 className="h-4 w-4 mr-1" />
                            编辑
                        </Button>
                        <Button variant="outline" onClick={() => setShowRevisionInput(!showRevisionInput)}>
                            <MessageSquare className="h-4 w-4 mr-1" />
                            提出修改意见
                        </Button>
                        {article.status !== 'approved' && (
                            <Button variant="outline" onClick={handleApprove}>
                                <Check className="h-4 w-4 mr-1" />
                                标记已审核
                            </Button>
                        )}
                    </>
                )}
            </div>

            {/* 修改意见输入 */}
            {showRevisionInput && (
                <Card>
                    <CardHeader>
                        <CardTitle className="text-base">提出修改意见</CardTitle>
                        <CardDescription>AI将根据您的意见重新生成文章</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <Textarea
                            rows={3}
                            value={revisionNote}
                            onChange={(e) => setRevisionNote(e.target.value)}
                            placeholder="例如：请增加更多数据支撑，语气更正式一些..."
                        />
                        <div className="flex justify-end gap-2">
                            <Button variant="outline" onClick={() => setShowRevisionInput(false)}>
                                取消
                            </Button>
                            <Button onClick={handleRewrite} disabled={rewriting || !revisionNote.trim()}>
                                {rewriting ? <Loader2 className="h-4 w-4 animate-spin mr-1" /> : <RefreshCw className="h-4 w-4 mr-1" />}
                                {rewriting ? "重写中..." : "提交并重写"}
                            </Button>
                        </div>
                    </CardContent>
                </Card>
            )}

            {/* 文章内容 */}
            <Card>
                <CardContent className="pt-6">
                    {isEditing ? (
                        <div className="space-y-2">
                            <Label>文章内容</Label>
                            <Textarea
                                rows={20}
                                value={editContent}
                                onChange={(e) => setEditContent(e.target.value)}
                                className="font-mono text-sm"
                            />
                        </div>
                    ) : (
                        <div className="prose prose-sm dark:prose-invert max-w-none">
                            <ReactMarkdown>{article.content}</ReactMarkdown>
                        </div>
                    )}
                </CardContent>
            </Card>
        </div>
    );
}

export default ImitatedArticleDetail;
