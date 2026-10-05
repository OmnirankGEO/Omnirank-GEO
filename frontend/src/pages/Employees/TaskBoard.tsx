import { authFetch } from '@/lib/api';
import { useState, useEffect } from "react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";
import { Loader2, RefreshCw, Clock, User, CheckCircle, Play, Inbox } from "lucide-react";
import ReactMarkdown from "@/components/SafeMarkdown";
// 任务类型
interface Task {
    id: number;
    employee_id: string;
    employee_name: string;
    task_content: string;
    task_type: string;
    status: string;
    result: string | null;
    skills_used: string[];
    execution_time: number | null;
    error_message: string | null;
    created_at: string;
    completed_at: string | null;
}

// 状态配置
const STATUS_CONFIG: Record<string, { label: string; color: string; icon: typeof Clock }> = {
    pending: { label: "待处理", color: "bg-yellow-100 text-yellow-800 border-yellow-200", icon: Inbox },
    running: { label: "进行中", color: "bg-blue-100 text-blue-800 border-blue-200", icon: Play },
    completed: { label: "已完成", color: "bg-green-100 text-green-800 border-green-200", icon: CheckCircle },
    failed: { label: "失败", color: "bg-red-100 text-red-800 border-red-200", icon: Clock },
};

export function TaskBoard() {
    const [tasks, setTasks] = useState<Task[]>([]);
    const [loading, setLoading] = useState(true);
    const [refreshing, setRefreshing] = useState(false);

    // 任务详情弹窗
    const [selectedTask, setSelectedTask] = useState<Task | null>(null);
    const [detailOpen, setDetailOpen] = useState(false);

    useEffect(() => {
        fetchTasks();
        // 自动刷新
        const interval = setInterval(fetchTasks, 30000);
        return () => clearInterval(interval);
    }, []);

    const fetchTasks = async (showLoading = false) => {
        if (showLoading) setRefreshing(true);
        try {
            const response = await authFetch("/api/employees/tasks?limit=50");
            const data = await response.json();
            if (data.success) {
                setTasks(data.tasks || []);
            }
        } catch (error) {
            console.error("Failed to fetch tasks:", error);
        } finally {
            setLoading(false);
            setRefreshing(false);
        }
    };

    const openTaskDetail = (task: Task) => {
        try {
            setSelectedTask(task);
            setDetailOpen(true);
        } catch (error) {
            console.error("Error opening task detail:", error);
        }
    };

    // 按状态分组
    const pendingTasks = tasks.filter(t => t.status === "pending");
    const runningTasks = tasks.filter(t => t.status === "running");
    const completedTasks = tasks.filter(t => t.status === "completed" || t.status === "failed");

    const formatTime = (dateStr: string) => {
        const date = new Date(dateStr);
        const now = new Date();
        const diff = now.getTime() - date.getTime();

        if (diff < 60000) return "刚刚";
        if (diff < 3600000) return `${Math.floor(diff / 60000)}分钟前`;
        if (diff < 86400000) return `${Math.floor(diff / 3600000)}小时前`;
        return date.toLocaleDateString();
    };



    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            {/* 页面标题 */}
            <div className="flex items-center justify-between">
                <div>
                    <h1 className="text-2xl font-bold">任务看板</h1>
                    <p className="text-gray-500 mt-1">
                        查看和管理员工任务执行状态
                    </p>
                </div>
                <Button
                    variant="outline"
                    onClick={() => fetchTasks(true)}
                    disabled={refreshing}
                >
                    <RefreshCw className={`h-4 w-4 mr-1 ${refreshing ? "animate-spin" : ""}`} />
                    刷新
                </Button>
            </div>

            {/* 统计卡片 */}
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                <Card>
                    <CardContent className="p-4">
                        <div className="text-2xl font-bold">{tasks.length}</div>
                        <div className="text-sm text-gray-500">总任务数</div>
                    </CardContent>
                </Card>
                <Card className="border-yellow-200 bg-yellow-50/50">
                    <CardContent className="p-4">
                        <div className="text-2xl font-bold text-yellow-700">{pendingTasks.length}</div>
                        <div className="text-sm text-yellow-600">待处理</div>
                    </CardContent>
                </Card>
                <Card className="border-blue-200 bg-blue-50/50">
                    <CardContent className="p-4">
                        <div className="text-2xl font-bold text-blue-700">{runningTasks.length}</div>
                        <div className="text-sm text-blue-600">进行中</div>
                    </CardContent>
                </Card>
                <Card className="border-green-200 bg-green-50/50">
                    <CardContent className="p-4">
                        <div className="text-2xl font-bold text-green-700">{completedTasks.length}</div>
                        <div className="text-sm text-green-600">已完成</div>
                    </CardContent>
                </Card>
            </div>

            {/* 看板区 - 新布局 */}
            <div className="flex flex-col gap-4 h-[calc(100dvh-280px)]">
                {/* 上方：待处理和进行中 */}
                <Card className="shrink-0">
                    <CardContent className="p-4">
                        <div className="flex gap-6">
                            {/* 待处理 */}
                            <div className="flex-1">
                                <div className="flex items-center gap-2 mb-3">
                                    <Inbox className="h-4 w-4 text-yellow-600" />
                                    <span className="font-medium">待处理</span>
                                    <Badge variant="secondary">{pendingTasks.length}</Badge>
                                </div>
                                <div className="flex flex-wrap gap-2 min-h-[60px]">
                                    {pendingTasks.map((task) => (
                                        <div
                                            key={task.id}
                                            className="bg-yellow-50 border border-yellow-200 rounded-lg px-3 py-2 cursor-pointer hover:shadow-md transition-shadow flex items-center gap-2"
                                            onClick={() => openTaskDetail(task)}
                                        >
                                            <User className="h-3 w-3 text-gray-500" />
                                            <span className="text-sm truncate max-w-[200px]">{task.task_content.slice(0, 40)}{task.task_content.length > 40 && "..."}</span>
                                        </div>
                                    ))}
                                    {pendingTasks.length === 0 && (
                                        <div className="text-gray-400 text-sm">暂无待处理任务</div>
                                    )}
                                </div>
                            </div>
                            {/* 进行中 */}
                            <div className="flex-1">
                                <div className="flex items-center gap-2 mb-3">
                                    <Play className="h-4 w-4 text-blue-600" />
                                    <span className="font-medium">进行中</span>
                                    <Badge variant="secondary">{runningTasks.length}</Badge>
                                </div>
                                <div className="flex flex-wrap gap-2 min-h-[60px]">
                                    {runningTasks.map((task) => (
                                        <div
                                            key={task.id}
                                            className="bg-blue-50 border border-blue-200 rounded-lg px-3 py-2 cursor-pointer hover:shadow-md transition-shadow flex items-center gap-2"
                                            onClick={() => openTaskDetail(task)}
                                        >
                                            <Loader2 className="h-3 w-3 text-blue-600 animate-spin" />
                                            <span className="text-sm truncate max-w-[200px]">{task.task_content.slice(0, 40)}{task.task_content.length > 40 && "..."}</span>
                                        </div>
                                    ))}
                                    {runningTasks.length === 0 && (
                                        <div className="text-gray-400 text-sm">暂无进行中任务</div>
                                    )}
                                </div>
                            </div>
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
                                    <CheckCircle className="h-4 w-4 text-green-600" />
                                    <span className="font-medium">已完成</span>
                                    <Badge variant="secondary">{completedTasks.length}</Badge>
                                </div>
                                <div className="flex-1 overflow-y-auto">
                                    {completedTasks.map((task) => (
                                        <div
                                            key={task.id}
                                            className={`p-3 border-b cursor-pointer hover:bg-white transition-colors ${selectedTask?.id === task.id ? 'bg-blue-50 border-l-2 border-l-blue-500' : ''
                                                }`}
                                            onClick={() => setSelectedTask(task)}
                                        >
                                            <div className="flex items-center gap-2">
                                                <div className="flex-1 min-w-0">
                                                    <h4 className="text-sm font-medium truncate">{task.task_content.slice(0, 50)}{task.task_content.length > 50 && "..."}</h4>
                                                    <div className="flex items-center gap-2 mt-1">
                                                        <User className="h-3 w-3 text-gray-400" />
                                                        <span className="text-xs text-gray-500">{task.employee_name}</span>
                                                        <Clock className="h-3 w-3 text-gray-400 ml-2" />
                                                        <span className="text-xs text-gray-400">{formatTime(task.created_at)}</span>
                                                    </div>
                                                </div>
                                                {task.execution_time && (
                                                    <Badge variant="outline" className="text-xs shrink-0">
                                                        {task.execution_time.toFixed(1)}s
                                                    </Badge>
                                                )}
                                            </div>
                                        </div>
                                    ))}
                                    {completedTasks.length === 0 && (
                                        <div className="text-center text-gray-400 py-8 text-sm">暂无已完成的任务</div>
                                    )}
                                </div>
                            </div>

                            {/* 右侧：详情 */}
                            <div className="flex-1 flex flex-col h-full overflow-hidden">
                                {selectedTask && completedTasks.some(t => t.id === selectedTask.id) ? (
                                    <>
                                        <div className="flex items-center justify-between p-4 border-b bg-white">
                                            <div className="flex items-center gap-2">
                                                <h3 className="font-semibold">任务详情</h3>
                                                {STATUS_CONFIG[selectedTask.status] && (
                                                    <Badge className={STATUS_CONFIG[selectedTask.status].color}>
                                                        {STATUS_CONFIG[selectedTask.status].label}
                                                    </Badge>
                                                )}
                                            </div>
                                        </div>
                                        {/* 元数据 */}
                                        <div className="flex items-center gap-4 px-4 py-2 text-sm bg-gray-50 border-b flex-wrap">
                                            <span><span className="text-gray-500">员工：</span>{selectedTask.employee_name}</span>
                                            <span><span className="text-gray-500">类型：</span>{selectedTask.task_type}</span>
                                            <span><span className="text-gray-500">耗时：</span>{selectedTask.execution_time?.toFixed(2) || "-"}s</span>
                                            <span><span className="text-gray-500">创建：</span>{new Date(selectedTask.created_at).toLocaleString()}</span>
                                        </div>
                                        {/* 任务内容 */}
                                        <div className="px-4 py-3 border-b bg-white">
                                            <div className="text-sm text-gray-500 mb-1">任务内容</div>
                                            <div className="p-2 bg-gray-50 rounded text-sm">{selectedTask.task_content}</div>
                                        </div>
                                        {/* 执行结果 */}
                                        <div className="flex-1 overflow-y-auto p-4">
                                            {selectedTask.result ? (
                                                <div>
                                                    <div className="text-sm text-gray-500 mb-2">执行结果</div>
                                                    <div className="prose prose-sm max-w-none bg-gray-50 rounded-lg p-4">
                                                        <ReactMarkdown>
                                                            {selectedTask.result}
                                                        </ReactMarkdown>
                                                    </div>
                                                </div>
                                            ) : (
                                                <div className="text-gray-400 text-center py-8">无执行结果</div>
                                            )}
                                            {/* 错误信息 */}
                                            {selectedTask.error_message && (
                                                <div className="mt-4">
                                                    <div className="text-sm text-gray-500 mb-1">错误信息</div>
                                                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
                                                        {selectedTask.error_message}
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                    </>
                                ) : (
                                    <div className="flex-1 flex items-center justify-center text-gray-400">
                                        <div className="text-center">
                                            <Clock className="h-12 w-12 mx-auto mb-2 opacity-30" />
                                            <p>点击左侧列表查看详情</p>
                                        </div>
                                    </div>
                                )}
                            </div>
                        </div>
                    </CardContent>
                </Card>
            </div>

            {/* 任务详情弹窗 */}
            <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
                <DialogContent className="max-w-2xl max-h-[80vh] overflow-y-auto">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2">
                            任务详情
                            {selectedTask && selectedTask.status && STATUS_CONFIG[selectedTask.status] && (
                                <Badge className={STATUS_CONFIG[selectedTask.status].color}>
                                    {STATUS_CONFIG[selectedTask.status].label}
                                </Badge>
                            )}
                        </DialogTitle>
                    </DialogHeader>

                    {selectedTask ? (
                        <div className="space-y-4">
                            {/* 基本信息 */}
                            <div className="grid grid-cols-2 gap-4 text-sm">
                                <div>
                                    <span className="text-gray-500">执行员工：</span>
                                    <span className="font-medium ml-1">{selectedTask.employee_name}</span>
                                </div>
                                <div>
                                    <span className="text-gray-500">任务类型：</span>
                                    <span className="font-medium ml-1">{selectedTask.task_type}</span>
                                </div>
                                <div>
                                    <span className="text-gray-500">创建时间：</span>
                                    <span className="ml-1">{new Date(selectedTask.created_at).toLocaleString()}</span>
                                </div>
                                {selectedTask.execution_time && (
                                    <div>
                                        <span className="text-gray-500">执行耗时：</span>
                                        <span className="ml-1">{selectedTask.execution_time.toFixed(2)}s</span>
                                    </div>
                                )}
                            </div>

                            {/* 任务内容 */}
                            <div>
                                <div className="text-sm text-gray-500 mb-1">任务内容</div>
                                <div className="p-3 bg-gray-50 rounded-lg text-sm">
                                    {selectedTask.task_content}
                                </div>
                            </div>

                            {/* 使用技能 */}
                            {selectedTask.skills_used && Array.isArray(selectedTask.skills_used) && selectedTask.skills_used.length > 0 && (
                                <div>
                                    <div className="text-sm text-gray-500 mb-1">使用技能</div>
                                    <div className="flex flex-wrap gap-1">
                                        {selectedTask.skills_used.map((skill, i) => (
                                            <Badge key={i} variant="outline">{skill}</Badge>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {/* 执行结果 */}
                            {selectedTask.result && (
                                <div>
                                    <div className="text-sm text-gray-500 mb-1">执行结果</div>
                                    <div className="max-w-none max-h-80 overflow-y-auto bg-gray-50 border rounded-lg p-4">
                                        <div className="prose prose-sm dark:prose-invert max-w-none
                                            prose-headings:text-foreground prose-headings:font-semibold
                                            prose-p:leading-relaxed
                                            prose-code:bg-secondary prose-code:px-1 prose-code:rounded
                                            prose-pre:bg-gray-800 prose-pre:text-gray-100
                                            prose-table:text-sm
                                            prose-th:bg-gray-100 prose-th:text-gray-800 prose-th:px-3 prose-th:py-2
                                            prose-td:px-3 prose-td:py-2 prose-td:text-gray-700 prose-td:border-b
                                        ">
                                            <ReactMarkdown>
                                                {selectedTask.result || ""}
                                            </ReactMarkdown>
                                        </div>
                                    </div>
                                </div>
                            )}

                            {/* 错误信息 */}
                            {selectedTask.error_message && (
                                <div>
                                    <div className="text-sm text-gray-500 mb-1">错误信息</div>
                                    <div className="p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
                                        {selectedTask.error_message}
                                    </div>
                                </div>
                            )}
                        </div>
                    ) : (
                        <div className="text-center py-8 text-gray-400">
                            加载任务详情...
                        </div>
                    )}
                </DialogContent>
            </Dialog>
        </div>
    );
}
