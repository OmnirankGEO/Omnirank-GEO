import { authFetch } from '@/lib/api';
import { useState, useEffect, useRef } from "react";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import {
    Play,
    Pause,
    CheckCircle2,
    Circle,
    Loader2,
    AlertCircle,
    Download,
    Eye,
    ChevronRight
} from "lucide-react";
import ReactMarkdown from "@/components/SafeMarkdown";
interface Task {
    id: string;
    name: string;
    assignee_id: string;
    assignee_name: string;
    input_required: string;
    output_expected: string;
    acceptance_criteria: string;
    estimated_time: string;
    status: "pending" | "running" | "completed" | "failed" | "paused" | "skipped";
    result?: string;
    error?: string;
}

interface LogEntry {
    timestamp: string;
    type: "info" | "success" | "error" | "warning";
    message: string;
    taskId?: string;
}

interface TaskExecutionBoardProps {
    tasksMarkdown?: string;          // 从Markdown解析任务
    tasks?: Task[];                   // 直接传入任务
    context?: string;                 // 背景上下文
    participantIds?: string[];        // 参会人员ID
    onComplete?: (results: Record<string, string>) => void;
    autoStart?: boolean;              // 是否自动开始执行
}

export function TaskExecutionBoard({
    tasksMarkdown,
    tasks: initialTasks,
    context = "",
    participantIds = [],
    onComplete,
    autoStart = false,
}: TaskExecutionBoardProps) {
    const [tasks, setTasks] = useState<Task[]>(initialTasks || []);
    const [logs, setLogs] = useState<LogEntry[]>([]);
    const [isExecuting, setIsExecuting] = useState(false);
    const [isPaused, setIsPaused] = useState(false);
    const [isComplete, setIsComplete] = useState(false);
    const [results, setResults] = useState<Record<string, string>>({});
    const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null);
    const [showPreviewDialog, setShowPreviewDialog] = useState(false);
    const [isDownloading, setIsDownloading] = useState(false);

    const logsEndRef = useRef<HTMLDivElement>(null);

    // 自动滚动到最新日志
    useEffect(() => {
        logsEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [logs]);

    // 自动开始
    useEffect(() => {
        if (autoStart && (tasksMarkdown || initialTasks?.length)) {
            startExecution();
        }
    }, [autoStart]);

    const addLog = (type: LogEntry["type"], message: string, taskId?: string) => {
        setLogs(prev => [...prev, {
            timestamp: new Date().toLocaleTimeString(),
            type,
            message,
            taskId,
        }]);
    };

    const startExecution = async () => {
        setIsExecuting(true);
        setIsComplete(false);
        setResults({});
        addLog("info", "🚀 开始执行任务...");

        try {
            const response = await authFetch("/api/tasks/execute", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    tasks_markdown: tasksMarkdown,
                    tasks: initialTasks,
                    context,
                    participant_ids: participantIds,
                }),
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const reader = response.body?.getReader();
            if (!reader) throw new Error("No response body");

            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });
                const lines = buffer.split("\n");
                buffer = lines.pop() || "";

                for (const line of lines) {
                    if (line.startsWith("data: ")) {
                        try {
                            const event = JSON.parse(line.slice(6));
                            handleStreamEvent(event);
                        } catch (e) {
                            console.error("Parse error:", e);
                        }
                    }
                }
            }
        } catch (error) {
            addLog("error", `执行失败: ${error}`);
        } finally {
            setIsExecuting(false);
        }
    };

    const handleStreamEvent = (event: { type: string; data: any }) => {
        switch (event.type) {
            case "start":
                setTasks(event.data.tasks || []);
                addLog("info", `📋 共 ${event.data.total_tasks} 个任务待执行`);
                break;

            case "task_start":
                setTasks(prev => prev.map(t =>
                    t.id === event.data.task_id
                        ? { ...t, status: "running" as const }
                        : t
                ));
                addLog("info", `▶️ ${event.data.assignee} 开始执行: ${event.data.task_name}`, event.data.task_id);
                break;

            case "task_log":
                addLog(event.data.level || "info", event.data.message, event.data.task_id);
                break;

            case "task_complete":
                setTasks(prev => prev.map(t =>
                    t.id === event.data.task_id
                        ? { ...t, status: "completed" as const, result: event.data.result }
                        : t
                ));
                setResults(prev => ({ ...prev, [event.data.task_id]: event.data.result }));
                addLog("success", `✅ ${event.data.task_name} 完成 (${event.data.duration?.toFixed(1)}s)`, event.data.task_id);
                break;

            case "task_error":
                setTasks(prev => prev.map(t =>
                    t.id === event.data.task_id
                        ? { ...t, status: "failed" as const, error: event.data.error }
                        : t
                ));
                addLog("error", `❌ ${event.data.task_name} 失败: ${event.data.error}`, event.data.task_id);
                break;

            case "task_skipped":
                setTasks(prev => prev.map(t =>
                    t.id === event.data.task_id
                        ? { ...t, status: "skipped" as const }
                        : t
                ));
                addLog("warning", `⏭️ 跳过: ${event.data.reason}`, event.data.task_id);
                break;

            case "complete":
                setIsComplete(true);
                setResults(event.data.results || {});
                addLog("success", `🎉 全部完成! ${event.data.completed}/${event.data.total_tasks} 成功`);
                if (onComplete) onComplete(event.data.results);
                break;

            case "error":
                addLog("error", `💥 ${event.data.error}`);
                break;
        }
    };

    const getStatusIcon = (status: Task["status"]) => {
        switch (status) {
            case "completed":
                return <CheckCircle2 className="h-4 w-4 text-green-500" />;
            case "running":
                return <Loader2 className="h-4 w-4 text-blue-500 animate-spin" />;
            case "failed":
                return <AlertCircle className="h-4 w-4 text-red-500" />;
            case "skipped":
                return <Circle className="h-4 w-4 text-gray-400" />;
            default:
                return <Circle className="h-4 w-4 text-gray-300" />;
        }
    };

    const getLogColor = (type: LogEntry["type"]) => {
        switch (type) {
            case "success": return "text-green-600";
            case "error": return "text-red-600";
            case "warning": return "text-yellow-600";
            default: return "text-gray-600";
        }
    };

    // 打包下载所有结果
    const handleDownloadPackage = () => {
        setIsDownloading(true);
        try {
            // 生成Markdown内容
            let content = `# 任务执行报告\n\n`;
            content += `**执行时间**: ${new Date().toLocaleString()}\n`;
            content += `**任务总数**: ${tasks.length}\n`;
            content += `**完成数量**: ${completedCount}\n\n`;
            content += `---\n\n`;

            // 添加每个任务的结果
            tasks.forEach((task, index) => {
                content += `## ${index + 1}. ${task.name}\n\n`;
                content += `- **负责人**: ${task.assignee_name}\n`;
                content += `- **状态**: ${task.status === 'completed' ? '✅ 完成' : task.status === 'failed' ? '❌ 失败' : '⏸️ ' + task.status}\n`;
                content += `- **输入要求**: ${task.input_required}\n`;
                content += `- **期望产出**: ${task.output_expected}\n\n`;

                if (results[task.id]) {
                    content += `### 产出内容\n\n`;
                    content += results[task.id] + '\n\n';
                } else if (task.error) {
                    content += `### 错误信息\n\n`;
                    content += `\`\`\`\n${task.error}\n\`\`\`\n\n`;
                }
                content += `---\n\n`;
            });

            // 创建并下载文件
            const blob = new Blob([content], { type: 'text/markdown;charset=utf-8' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `任务执行报告_${new Date().toISOString().slice(0, 10)}.md`;
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            URL.revokeObjectURL(url);

            addLog("success", "📦 报告已下载");
        } catch (error) {
            addLog("error", `下载失败: ${error}`);
        } finally {
            setIsDownloading(false);
        }
    };

    const completedCount = tasks.filter(t => t.status === "completed").length;
    const progress = tasks.length > 0 ? (completedCount / tasks.length) * 100 : 0;

    return (
        <div className="flex h-full gap-4">
            {/* 左侧：任务列表和进度 */}
            <Card className="w-1/3 flex flex-col">
                <CardHeader className="pb-3">
                    <CardTitle className="text-base flex items-center justify-between">
                        <span>📋 任务执行进度</span>
                        <Badge variant={isComplete ? "default" : "outline"}>
                            {completedCount}/{tasks.length}
                        </Badge>
                    </CardTitle>
                </CardHeader>
                <CardContent className="flex-1 flex flex-col">
                    {/* 进度条 */}
                    <div className="mb-4">
                        <div className="w-full bg-gray-200 rounded-full h-2">
                            <div
                                className="bg-blue-600 h-2 rounded-full transition-all duration-300"
                                style={{ width: `${progress}%` }}
                            />
                        </div>
                    </div>

                    {/* 任务列表 */}
                    <ScrollArea className="flex-1">
                        <div className="space-y-2 pr-2">
                            {tasks.map((task, index) => (
                                <div
                                    key={task.id}
                                    className={`p-3 rounded-lg border cursor-pointer transition-colors ${selectedTaskId === task.id
                                        ? "bg-blue-50 border-blue-200"
                                        : "hover:bg-gray-50"
                                        } ${task.status === "running" ? "ring-2 ring-blue-400" : ""}`}
                                    onClick={() => setSelectedTaskId(task.id)}
                                >
                                    <div className="flex items-center gap-2">
                                        {getStatusIcon(task.status)}
                                        <span className="text-sm font-medium flex-1 truncate">
                                            {index + 1}. {task.name}
                                        </span>
                                        <ChevronRight className="h-4 w-4 text-gray-400" />
                                    </div>
                                    <div className="text-xs text-gray-500 mt-1 ml-6">
                                        {task.assignee_name}
                                    </div>
                                </div>
                            ))}
                        </div>
                    </ScrollArea>

                    {/* 操作按钮 */}
                    <div className="flex gap-2 mt-4 pt-4 border-t">
                        {!isExecuting && !isComplete && (
                            <Button onClick={startExecution} className="flex-1">
                                <Play className="h-4 w-4 mr-1" />
                                开始执行
                            </Button>
                        )}
                        {isExecuting && (
                            <Button
                                variant="outline"
                                onClick={() => setIsPaused(!isPaused)}
                                className="flex-1"
                            >
                                {isPaused ? (
                                    <><Play className="h-4 w-4 mr-1" /> 继续</>
                                ) : (
                                    <><Pause className="h-4 w-4 mr-1" /> 暂停</>
                                )}
                            </Button>
                        )}
                        {isComplete && (
                            <>
                                <Button
                                    variant="outline"
                                    className="flex-1"
                                    onClick={() => setShowPreviewDialog(true)}
                                >
                                    <Eye className="h-4 w-4 mr-1" />
                                    预览结果
                                </Button>
                                <Button
                                    className="flex-1"
                                    onClick={handleDownloadPackage}
                                    disabled={isDownloading}
                                >
                                    {isDownloading ? (
                                        <Loader2 className="h-4 w-4 mr-1 animate-spin" />
                                    ) : (
                                        <Download className="h-4 w-4 mr-1" />
                                    )}
                                    {isDownloading ? "打包中..." : "打包下载"}
                                </Button>
                            </>
                        )}
                    </div>
                </CardContent>
            </Card>

            {/* 右侧：实时日志 */}
            <Card className="flex-1 flex flex-col">
                <CardHeader className="pb-3">
                    <CardTitle className="text-base">
                        📜 实时执行日志
                    </CardTitle>
                </CardHeader>
                <CardContent className="flex-1 flex flex-col">
                    <ScrollArea className="flex-1 bg-gray-900 rounded-lg p-4">
                        <div className="font-mono text-sm space-y-1">
                            {logs.length === 0 ? (
                                <div className="text-gray-500">等待执行...</div>
                            ) : (
                                logs.map((log, index) => (
                                    <div key={index} className={`${getLogColor(log.type)}`}>
                                        <span className="text-gray-500">[{log.timestamp}]</span>{" "}
                                        {log.message}
                                    </div>
                                ))
                            )}
                            <div ref={logsEndRef} />
                        </div>
                    </ScrollArea>

                    {/* 选中任务的结果预览 */}
                    {selectedTaskId && results[selectedTaskId] && (
                        <div className="mt-4 p-4 bg-gray-50 rounded-lg border">
                            <h4 className="font-medium text-sm mb-2">
                                📄 {tasks.find(t => t.id === selectedTaskId)?.name} 产出
                            </h4>
                            <div className="text-sm text-gray-700 max-h-40 overflow-auto whitespace-pre-wrap">
                                {results[selectedTaskId].slice(0, 500)}
                                {results[selectedTaskId].length > 500 && "..."}
                            </div>
                        </div>
                    )}
                </CardContent>
            </Card>

            {/* 预览结果模态框 */}
            <Dialog open={showPreviewDialog} onOpenChange={setShowPreviewDialog}>
                <DialogContent className="max-w-4xl h-[80vh] flex flex-col">
                    <DialogHeader className="shrink-0">
                        <DialogTitle>📋 任务执行结果预览</DialogTitle>
                    </DialogHeader>
                    <ScrollArea className="flex-1 min-h-0 pr-4">
                        <div className="space-y-6">
                            {tasks.map((task, index) => (
                                <div key={task.id} className="border-b pb-4 last:border-b-0">
                                    <div className="flex items-center gap-2 mb-2">
                                        {getStatusIcon(task.status)}
                                        <h3 className="font-semibold">{index + 1}. {task.name}</h3>
                                        <Badge variant="outline" className="ml-auto">
                                            {task.assignee_name}
                                        </Badge>
                                    </div>
                                    {results[task.id] ? (
                                        <div className="bg-gray-50 rounded-lg p-4 prose prose-sm max-w-none">
                                            <ReactMarkdown>
                                                {results[task.id]}
                                            </ReactMarkdown>
                                        </div>
                                    ) : task.error ? (
                                        <div className="bg-red-50 text-red-700 rounded-lg p-4 text-sm">
                                            ❌ 错误: {task.error}
                                        </div>
                                    ) : (
                                        <div className="text-gray-400 text-sm">暂无结果</div>
                                    )}
                                </div>
                            ))}
                        </div>
                    </ScrollArea>
                </DialogContent>
            </Dialog>
        </div>
    );
}

export default TaskExecutionBoard;
