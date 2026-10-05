import { authFetch } from '@/lib/api';
import { useEffect, useState, useRef } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { CheckCircle2, Loader2, AlertCircle, ArrowLeft, FileText, PenTool } from "lucide-react";

interface ProgressData {
    status: string;
    articles_generated: number;
    articles_total: number;
    current_title?: string;
    logs: string[];
    done?: boolean;
    error?: string;
}

export function WritingProgress() {
    const navigate = useNavigate();
    const [searchParams] = useSearchParams();
    const taskId = searchParams.get("task_id");
    const diagnosisId = searchParams.get("diagnosis_id");
    const quoteId = searchParams.get("quote_id");

    const [progress, setProgress] = useState<ProgressData>({
        status: "preparing",
        articles_generated: 0,
        articles_total: 0,
        logs: [],
    });
    const [error, setError] = useState<string | null>(null);
    const logEndRef = useRef<HTMLDivElement>(null);

    // 轮询进度
    useEffect(() => {
        if (!taskId) return;

        const fetchProgress = async () => {
            try {
                // 优先用新系统进度接口，回退到旧接口
                const res = await authFetch(`/api/writing/progress/${taskId}`)
                    .then(r => r.ok ? r : authFetch(`/api/articles/progress/${taskId}`));
                const data = await res.json();

                if (data.error) {
                    setError(data.error);
                    return;
                }

                setProgress({
                    status: data.status || "generating",
                    articles_generated: data.articles_generated || 0,
                    articles_total: data.articles_total || 0,
                    current_title: data.current_title,
                    logs: data.logs || [],
                    done: data.status === "completed",
                });

                // 完成后自动跳转（优先用 quote_id，兼容旧系统 diagnosis_id）
                if (data.status === "completed") {
                    const target = quoteId
                        ? `/writing?quote_id=${quoteId}`
                        : `/articles?diagnosis_id=${diagnosisId}`;
                    setTimeout(() => navigate(target), 3000);
                }
            } catch (e) {
                console.error("Failed to fetch progress:", e);
            }
        };

        fetchProgress();
        const interval = setInterval(fetchProgress, 2000);
        return () => clearInterval(interval);
    }, [taskId, diagnosisId, navigate]);

    // 自动滚动日志
    useEffect(() => {
        if (logEndRef.current) {
            logEndRef.current.scrollIntoView({ behavior: "smooth" });
        }
    }, [progress.logs]);

    const overallProgress = progress.articles_total > 0
        ? (progress.articles_generated / progress.articles_total) * 100
        : 0;

    return (
        <div className="space-y-6 max-w-4xl mx-auto p-4 sm:p-6">
            <div className="flex items-center gap-4">
                <Button variant="ghost" size="icon" onClick={() => navigate("/articles")}>
                    <ArrowLeft className="h-4 w-4" />
                </Button>
                <div className="flex items-center gap-4">
                    <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center">
                        <PenTool className="h-5 w-5 text-brand" />
                    </div>
                    <div>
                    <h2 className="text-2xl font-bold text-foreground">文章生成中</h2>
                    <p className="text-muted-foreground">
                        {progress.done ? (
                            <span className="text-green-600">● 生成完成</span>
                        ) : (
                            <span className="text-blue-600">● 正在生成…</span>
                        )}
                    </p>
                    </div>
                </div>
            </div>

            {error && (
                <div className="bg-red-50 border border-red-200 text-red-700 px-4 py-3 rounded-lg flex items-center gap-2">
                    <AlertCircle className="h-5 w-5" />
                    {error}
                </div>
            )}

            {/* 进度卡片 */}
            <Card className="border border-border rounded-xl">
                <CardHeader>
                    <CardTitle className="flex items-center gap-2">
                        <PenTool className="h-5 w-5" />
                        生成进度
                    </CardTitle>
                </CardHeader>
                <CardContent className="space-y-6">
                    {/* 总进度条 */}
                    <div>
                        <div className="flex justify-between text-sm mb-2">
                            <span>总进度</span>
                            <span>{progress.articles_generated} / {progress.articles_total} 篇</span>
                        </div>
                        <Progress value={overallProgress} className="h-3" />
                    </div>

                    {/* 并行任务状态 */}
                    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
                        {Array.from({ length: progress.articles_total }, (_, i) => (
                            <div
                                key={i}
                                className={`h-12 rounded-lg flex items-center justify-center text-sm font-medium transition-colors ${i < progress.articles_generated
                                    ? "bg-green-100 text-green-700 border border-green-200"
                                    : i === progress.articles_generated
                                        ? "bg-blue-100 text-blue-700 border border-blue-200 animate-pulse"
                                        : "bg-muted text-muted-foreground border border-border"
                                    }`}
                            >
                                {i < progress.articles_generated ? (
                                    <CheckCircle2 className="h-5 w-5" />
                                ) : i === progress.articles_generated ? (
                                    <Loader2 className="h-5 w-5 animate-spin text-brand" />
                                ) : (
                                    <FileText className="h-4 w-4" />
                                )}
                            </div>
                        ))}
                    </div>

                    {/* 当前任务 */}
                    {progress.current_title && (
                        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
                            <div className="text-sm text-blue-600 mb-1">正在撰写</div>
                            <div className="font-medium text-blue-900">{progress.current_title}</div>
                        </div>
                    )}
                </CardContent>
            </Card>

            {/* 终端风格日志 */}
            <Card className="bg-[#1e1e2e] border border-border rounded-xl">
                <CardHeader className="border-b border-white/10 py-3">
                    <div className="flex items-center gap-2">
                        <div className="flex gap-1.5">
                            <div className="w-3 h-3 rounded-full bg-red-500/80" />
                            <div className="w-3 h-3 rounded-full bg-yellow-500/80" />
                            <div className="w-3 h-3 rounded-full bg-green-500/80" />
                        </div>
                        <span className="ml-3 text-xs font-mono text-muted-foreground">article_generator.log</span>
                    </div>
                </CardHeader>
                <CardContent className="p-0">
                    <div className="h-64 overflow-auto p-4 font-mono text-xs space-y-1">
                        {progress.logs.length === 0 ? (
                            <div className="text-muted-foreground italic">等待日志输出...</div>
                        ) : (
                            progress.logs.map((log, i) => {
                                const isError = log.includes("Error") || log.includes("失败");
                                const isSuccess = log.includes("✅") || log.includes("完成");
                                const isInfo = log.includes("✍️") || log.includes("🤖");

                                return (
                                    <div
                                        key={i}
                                        className={`${isError ? "text-red-400" :
                                            isSuccess ? "text-green-400" :
                                                isInfo ? "text-blue-300" : "text-[#cdd6f4]"
                                            }`}
                                    >
                                        {log}
                                    </div>
                                );
                            })
                        )}
                        <div ref={logEndRef} />
                    </div>
                </CardContent>
            </Card>

            {/* 完成状态 */}
            {progress.done && (
                <Card className="bg-emerald-500/10 border border-emerald-500/20 rounded-xl">
                    <CardContent className="py-6 text-center">
                        <CheckCircle2 className="h-12 w-12 text-green-600 mx-auto mb-2" />
                        <p className="text-lg font-medium text-green-900">生成完成！</p>
                        <p className="text-green-700 mb-4">成功生成 {progress.articles_generated} 篇文章</p>
                        <Button onClick={() => navigate(quoteId ? `/writing?quote_id=${quoteId}` : `/articles?diagnosis_id=${diagnosisId}`)}>
                            查看文章列表
                        </Button>
                    </CardContent>
                </Card>
            )}
        </div>
    );
}
