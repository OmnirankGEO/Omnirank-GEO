import { authFetch } from '@/lib/api';
import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
    DialogFooter,
} from "@/components/ui/dialog";
import { Loader2, Send, History, Users, Zap, MessageSquare, ClipboardList, Settings, Layers, ExternalLink, Paperclip, X, FileText, Image, File, MessageCircle } from "lucide-react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "@/components/SafeMarkdown";
// 技能ID → 中文名映射
const SKILL_NAMES: Record<string, string> = {
    douyin_search: '抖音搜索', xhs_search: '小红书搜索', web_search: '网页搜索',
    ai_visibility_test: 'AI可见度测试', citation_analysis: '引用分析',
    competitor_identification: '竞品识别', gap_analysis: '差距分析', viral_content_analysis: '爆款拆解',
    geo_scoring: 'GEO评分', report_generation: '报告生成',
    topic_planning: '选题规划', title_generation: '标题生成', trend_mining: '热点挖掘',
    article_writing: '长文撰写', content_rewriting: '内容改写', deai_optimization: '去AI味',
    hook_design: '钩子设计', script_writing: '脚本撰写', douyin_copywriting: '抖音话术',
    seeding_copywriting: '种草文案', cover_design: '封面设计', hashtag_strategy: '标签策略',
    ppt_generation: 'PPT生成', template_design: '模板设计',
    quality_review: '质量审核', style_unification: '风格统一', compliance_check: '合规检查',
    industry_insight: '行业洞察', knowledge_retrieval: '知识检索', expert_consultation: '专家咨询',
    market_analysis: '市场分析', brand_strategy: '品牌策略', content_planning: '内容规划',
    data_collection: '数据采集', social_media: '社媒运营', seo_optimization: 'SEO优化',
};
const skillName = (id: string) => SKILL_NAMES[id] || id.replace(/_/g, ' ');

// 员工类型
interface Employee {
    id: string;
    name: string;
    department: string;
    description: string;
    model: string;
    skills: string[];
    status: string;
    memory_type: string;
}

// 部门类型
interface Department {
    id: string;
    name: string;
    description: string;
}

// 任务结果类型
interface TaskResult {
    success: boolean;
    task_id?: number;
    result?: {
        employee_id: string;
        employee_name: string;
        task: string;
        result: string;
        status: string;
        execution_time: number;
        skills_used: string[];
    };
    error?: string;
}

// 复杂任务结果类型
interface ComplexTaskResult {
    success: boolean;
    status?: string;
    plan?: any[];
    results?: any[];
    summary?: string;
    completed_count?: number;
    total_count?: number;
    error?: string;
}

// 部门配置
const DEPARTMENT_CONFIG: Record<string, { color: string; bgColor: string }> = {
    leadership: { color: "text-purple-700", bgColor: "bg-purple-50 border-purple-200" },
    diagnosis: { color: "text-green-700", bgColor: "bg-green-50 border-green-200" },
    content: { color: "text-blue-700", bgColor: "bg-blue-50 border-blue-200" },
    support: { color: "text-orange-700", bgColor: "bg-orange-50 border-orange-200" },
};

export function EmployeeHall() {
    const navigate = useNavigate();
    const [employees, setEmployees] = useState<Employee[]>([]);
    const [departments, setDepartments] = useState<Department[]>([]);
    const [loading, setLoading] = useState(true);

    // 任务执行状态
    const [selectedEmployee, setSelectedEmployee] = useState<Employee | null>(null);
    const [taskDialogOpen, setTaskDialogOpen] = useState(false);
    const [taskInput, setTaskInput] = useState("");
    const [contextInput, setContextInput] = useState("");
    const [executing, setExecuting] = useState(false);
    const [taskResult, setTaskResult] = useState<TaskResult | null>(null);
    // 多轮对话支持
    const [conversationHistory, setConversationHistory] = useState<{ role: 'user' | 'assistant', content: string }[]>([]);
    const [followUpInput, setFollowUpInput] = useState("");

    // 智能派单
    const [smartTaskInput, setSmartTaskInput] = useState("");
    const [smartExecuting, setSmartExecuting] = useState(false);
    const [smartAttachments, setSmartAttachments] = useState<File[]>([]);

    // 复杂任务
    const [complexDialogOpen, setComplexDialogOpen] = useState(false);
    const [complexGoal, setComplexGoal] = useState("");
    const [complexContext, setComplexContext] = useState("");
    const [complexExecuting, setComplexExecuting] = useState(false);
    const [complexResult, setComplexResult] = useState<ComplexTaskResult | null>(null);
    const [complexSteps, setComplexSteps] = useState<{ step: string, status: 'pending' | 'running' | 'done' | 'error', result?: string }[]>([]);
    const [currentStep, setCurrentStep] = useState<string>("");
    const [savedToWorkspace, setSavedToWorkspace] = useState(false);

    // 加载员工列表
    useEffect(() => {
        fetchEmployees();
    }, []);

    const fetchEmployees = async () => {
        try {
            setLoading(true);
            const response = await authFetch("/api/employees");
            const data = await response.json();
            if (data.success) {
                setEmployees(data.employees || []);
                setDepartments(data.departments || []);
            }
        } catch (error) {
            console.error("Failed to fetch employees:", error);
        } finally {
            setLoading(false);
        }
    };

    // 打开任务弹窗
    const openTaskDialog = (employee: Employee) => {
        setSelectedEmployee(employee);
        setTaskInput("");
        setContextInput("");
        setTaskResult(null);
        setConversationHistory([]);
        setFollowUpInput("");
        setTaskDialogOpen(true);
    };

    // 执行任务
    const executeTask = async () => {
        if (!selectedEmployee || !taskInput.trim()) return;

        setExecuting(true);
        setTaskResult(null);

        // 记录用户消息到对话历史
        const userMessage = taskInput + (contextInput ? `\n\n背景信息：${contextInput}` : "");
        setConversationHistory(prev => [...prev, { role: 'user', content: userMessage }]);

        try {
            const response = await authFetch(`/api/employees/${selectedEmployee.id}/task`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    task: taskInput,
                    context: contextInput ? { background: contextInput } : null,
                }),
            });
            const data = await response.json();
            setTaskResult(data);

            // 记录助手回复到对话历史
            if (data.success && data.result?.result) {
                setConversationHistory(prev => [...prev, { role: 'assistant', content: data.result.result }]);
            }
        } catch (error) {
            setTaskResult({
                success: false,
                error: String(error),
            });
        } finally {
            setExecuting(false);
        }
    };

    // 继续对话
    const continueConversation = async () => {
        if (!selectedEmployee || !followUpInput.trim()) return;

        setExecuting(true);

        // 记录用户追问到对话历史
        setConversationHistory(prev => [...prev, { role: 'user', content: followUpInput }]);

        try {
            // 构建包含对话历史的上下文
            const historyContext = conversationHistory.map(msg =>
                `${msg.role === 'user' ? '用户' : '助手'}: ${msg.content}`
            ).join('\n\n');

            const response = await authFetch(`/api/employees/${selectedEmployee.id}/task`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    task: followUpInput,
                    context: {
                        background: contextInput || "",
                        conversation_history: historyContext,
                        is_follow_up: true
                    },
                }),
            });
            const data = await response.json();
            setTaskResult(data);

            // 记录助手回复
            if (data.success && data.result?.result) {
                setConversationHistory(prev => [...prev, { role: 'assistant', content: data.result.result }]);
            }

            setFollowUpInput("");
        } catch (error) {
            setTaskResult({
                success: false,
                error: String(error),
            });
        } finally {
            setExecuting(false);
        }
    };

    // 路由结果状态
    const [routeResult, setRouteResult] = useState<{
        success: boolean;
        employee_id?: string;
        employee_name?: string;
        confidence?: number;
        reason?: string;
        suggestions?: string[];
        error?: string;
    } | null>(null);

    // 智能派单
    const executeSmartTask = async () => {
        if (!smartTaskInput.trim()) return;

        setSmartExecuting(true);
        setRouteResult(null);

        try {
            // 1. 上传附件（如果有）
            let attachmentPaths: string[] = [];
            if (smartAttachments.length > 0) {
                const formData = new FormData();
                smartAttachments.forEach(file => {
                    formData.append("files", file);
                });

                const uploadResponse = await authFetch("/api/upload", {
                    method: "POST",
                    body: formData,
                });
                const uploadData = await uploadResponse.json();
                if (uploadData.success && uploadData.files) {
                    attachmentPaths = uploadData.files.map((f: any) => f.path || f.filename);
                }
            }

            // 2. 路由任务
            const response = await authFetch("/api/route", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    task: smartTaskInput,
                    attachments: attachmentPaths,
                }),
            });
            const data = await response.json();
            setRouteResult(data);

            if (data.success && data.employee_id) {
                // 打开对应员工的任务弹窗
                const employee = employees.find(e => e.id === data.employee_id);
                if (employee) {
                    setSelectedEmployee(employee);
                    setTaskInput(smartTaskInput);
                    // 将附件信息添加到背景信息
                    if (attachmentPaths.length > 0) {
                        setContextInput(`附件：${attachmentPaths.join(", ")}`);
                    }
                    setTaskDialogOpen(true);
                }
            }
        } catch (error) {
            console.error("Smart routing failed:", error);
            setRouteResult({
                success: false,
                error: String(error),
            });
        } finally {
            setSmartExecuting(false);
        }
    };

    // 按部门分组
    const employeesByDepartment = employees.reduce((acc, emp) => {
        const dept = emp.department || "other";
        if (!acc[dept]) acc[dept] = [];
        acc[dept].push(emp);
        return acc;
    }, {} as Record<string, Employee[]>);

    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
                <span className="ml-2 text-gray-500">加载员工列表...</span>
            </div>
        );
    }

    return (
        <div className="p-6 space-y-6">
            {/* 页面标题 */}
            <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                <div>
                    <h1 className="text-2xl font-bold flex items-center gap-2">
                        <Users className="h-6 w-6" />
                        员工大厅
                    </h1>
                    <p className="text-gray-500 mt-1">
                        选择员工执行任务，或使用智能派单自动分配
                    </p>
                </div>
                <div className="flex flex-wrap items-center gap-2 sm:gap-3">
                    <Button
                        variant="outline"
                        onClick={() => navigate("/employees/meeting")}
                        className="border-blue-200 text-blue-700 hover:bg-blue-50"
                    >
                        <MessageSquare className="h-4 w-4 mr-1" />
                        召开会议
                    </Button>
                    <Button
                        variant="outline"
                        onClick={() => navigate("/employees/meeting-history")}
                        className="border-amber-200 text-amber-700 hover:bg-amber-50"
                    >
                        <History className="h-4 w-4 mr-1" />
                        会议记录
                    </Button>
                    <Button
                        variant="outline"
                        onClick={() => navigate("/employees/tasks")}
                        className="border-green-200 text-green-700 hover:bg-green-50"
                    >
                        <ClipboardList className="h-4 w-4 mr-1" />
                        任务看板
                    </Button>
                    <Button
                        variant="outline"
                        onClick={() => navigate("/employees/settings")}
                        className="border-purple-200 text-purple-700 hover:bg-purple-50"
                    >
                        <Settings className="h-4 w-4 mr-1" />
                        员工设置
                    </Button>
                    <Button
                        onClick={() => {
                            setComplexGoal("");
                            setComplexContext("");
                            setComplexResult(null);
                            setComplexSteps([]);
                            setSavedToWorkspace(false);
                            setComplexDialogOpen(true);
                        }}
                        className="bg-linear-to-r from-orange-500 to-red-500 text-white hover:opacity-90"
                    >
                        <Layers className="h-4 w-4 mr-1" />
                        复杂任务
                    </Button>
                    <Badge variant="outline" className="text-sm">
                        {employees.length} 位员工在线
                    </Badge>
                </div>
            </div>

            {/* 智能派单 */}
            <Card className="border-2 border-dashed border-purple-200 bg-purple-50/50">
                <CardContent className="pt-6">
                    <div className="flex items-center gap-2 mb-3">
                        <Zap className="h-5 w-5 text-purple-600" />
                        <span className="font-medium text-purple-900">智能派单</span>
                        <span className="text-sm text-purple-600">输入需求，系统自动分配合适的员工</span>
                    </div>
                    <div className="flex gap-2">
                        <div className="flex-1 flex flex-col gap-2">
                            <div className="flex gap-2">
                                <Input
                                    placeholder='例如：帮我搜索"某某科技"在抖音的相关内容'
                                    value={smartTaskInput}
                                    onChange={(e) => setSmartTaskInput(e.target.value)}
                                    onKeyDown={(e) => e.key === "Enter" && executeSmartTask()}
                                    className="flex-1"
                                />
                                <label className="cursor-pointer">
                                    <input
                                        type="file"
                                        multiple
                                        className="hidden"
                                        onChange={(e) => {
                                            if (e.target.files) {
                                                setSmartAttachments(prev => [...prev, ...Array.from(e.target.files!)]);
                                            }
                                            e.target.value = '';
                                        }}
                                    />
                                    <Button
                                        type="button"
                                        variant="outline"
                                        className="border-purple-200 text-purple-600 hover:bg-purple-50"
                                        asChild
                                    >
                                        <span>
                                            <Paperclip className="h-4 w-4" />
                                        </span>
                                    </Button>
                                </label>
                                <Button
                                    onClick={executeSmartTask}
                                    disabled={smartExecuting || !smartTaskInput.trim()}
                                    className="bg-purple-600 hover:bg-purple-700"
                                >
                                    {smartExecuting ? (
                                        <Loader2 className="h-4 w-4 animate-spin" />
                                    ) : (
                                        <>
                                            <Send className="h-4 w-4 mr-1" />
                                            智能派单
                                        </>
                                    )}
                                </Button>
                            </div>
                            {/* 附件列表 */}
                            {smartAttachments.length > 0 && (
                                <div className="flex flex-wrap gap-2">
                                    {smartAttachments.map((file, idx) => (
                                        <div
                                            key={idx}
                                            className="flex items-center gap-1 px-2 py-1 bg-white border border-purple-200 rounded-lg text-sm"
                                        >
                                            {file.type.startsWith('image/') ? (
                                                <Image className="h-3 w-3 text-purple-500" />
                                            ) : file.type.includes('pdf') || file.type.includes('document') ? (
                                                <FileText className="h-3 w-3 text-purple-500" />
                                            ) : (
                                                <File className="h-3 w-3 text-purple-500" />
                                            )}
                                            <span className="max-w-[120px] truncate text-foreground">{file.name}</span>
                                            <button
                                                onClick={() => setSmartAttachments(prev => prev.filter((_, i) => i !== idx))}
                                                className="text-gray-400 hover:text-red-500"
                                            >
                                                <X className="h-3 w-3" />
                                            </button>
                                        </div>
                                    ))}
                                </div>
                            )}
                        </div>
                    </div>

                    {/* 路由结果展示 */}
                    {routeResult && (
                        <div className={`mt-4 p-3 rounded-lg ${routeResult.success
                            ? "bg-green-50 border border-green-200"
                            : "bg-yellow-50 border border-yellow-200"
                            }`}>
                            {routeResult.success ? (
                                <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-2">
                                    <div>
                                        <span className="text-green-700 font-medium">
                                            已匹配: {routeResult.employee_name}
                                        </span>
                                        {routeResult.confidence && (
                                            <Badge variant="outline" className="ml-2 text-xs">
                                                置信度 {Math.round(routeResult.confidence * 100)}%
                                            </Badge>
                                        )}
                                    </div>
                                    <span className="text-sm text-green-600">{routeResult.reason}</span>
                                </div>
                            ) : (
                                <div>
                                    <span className="text-yellow-700">
                                        {routeResult.error || "未能识别任务类型"}
                                    </span>
                                    {routeResult.suggestions && routeResult.suggestions.length > 0 && (
                                        <div className="mt-2 text-sm text-yellow-600">
                                            <span>建议尝试：</span>
                                            <ul className="list-disc list-inside mt-1">
                                                {routeResult.suggestions.slice(0, 3).map((s, i) => (
                                                    <li key={i} className="cursor-pointer hover:text-yellow-800"
                                                        onClick={() => setSmartTaskInput(s)}>
                                                        {s}
                                                    </li>
                                                ))}
                                            </ul>
                                        </div>
                                    )}
                                </div>
                            )}
                        </div>
                    )}
                </CardContent>
            </Card>

            {/* 员工列表（按部门分组） */}
            {departments.map((dept) => {
                const deptEmployees = employeesByDepartment[dept.id] || [];
                if (deptEmployees.length === 0) return null;

                const config = DEPARTMENT_CONFIG[dept.id] || { color: "text-foreground", bgColor: "bg-gray-50" };

                return (
                    <div key={dept.id} className="space-y-3">
                        <h2 className={`text-lg font-semibold ${config.color}`}>
                            {dept.name}
                        </h2>
                        <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-4">
                            {deptEmployees.map((employee) => (
                                <Card
                                    key={employee.id}
                                    className={`cursor-pointer transition-all hover:shadow-md border ${config.bgColor}`}
                                    onClick={() => openTaskDialog(employee)}
                                >
                                    <CardHeader className="pb-2">
                                        <div className="flex items-center justify-between">
                                            <CardTitle className="text-base">
                                                {employee.name}
                                            </CardTitle>
                                            <Badge
                                                variant="outline"
                                                className="bg-green-100 text-green-700 border-green-300"
                                            >
                                                在线
                                            </Badge>
                                        </div>
                                        <CardDescription className="text-xs line-clamp-2">
                                            {employee.description}
                                        </CardDescription>
                                    </CardHeader>
                                    <CardContent className="pt-0">
                                        <div className="flex flex-wrap gap-1">
                                            {employee.skills.slice(0, 3).map((skill) => (
                                                <Badge
                                                    key={skill}
                                                    variant="secondary"
                                                    className="text-xs"
                                                >
                                                    {skillName(skill)}
                                                </Badge>
                                            ))}
                                            {employee.skills.length > 3 && (
                                                <Badge variant="secondary" className="text-xs">
                                                    +{employee.skills.length - 3}
                                                </Badge>
                                            )}
                                        </div>
                                        <Button
                                            size="sm"
                                            className="w-full mt-3"
                                            variant="outline"
                                        >
                                            调用
                                        </Button>
                                    </CardContent>
                                </Card>
                            ))}
                        </div>
                    </div>
                );
            })}

            {/* 任务执行弹窗 */}
            <Dialog open={taskDialogOpen} onOpenChange={setTaskDialogOpen}>
                <DialogContent className="max-w-2xl max-h-[90vh] overflow-y-auto">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2">
                            {selectedEmployee?.name}
                        </DialogTitle>
                        <DialogDescription>
                            {selectedEmployee?.description}
                        </DialogDescription>
                    </DialogHeader>

                    <div className="space-y-4">
                        {/* 技能列表 */}
                        <div>
                            <label className="text-sm font-medium text-foreground">可用技能</label>
                            <div className="flex flex-wrap gap-1 mt-1">
                                {selectedEmployee?.skills.map((skill) => (
                                    <Badge key={skill} variant="outline">
                                        {skillName(skill)}
                                    </Badge>
                                ))}
                            </div>
                        </div>

                        {/* 任务输入 */}
                        <div>
                            <label className="text-sm font-medium text-foreground">
                                任务描述 <span className="text-red-500">*</span>
                            </label>
                            <Textarea
                                placeholder={`例如：帮我搜索"某某科技"在抖音的相关内容`}
                                value={taskInput}
                                onChange={(e) => setTaskInput(e.target.value)}
                                className="mt-1"
                                rows={3}
                            />
                        </div>

                        {/* 背景信息（可选） */}
                        <div>
                            <label className="text-sm font-medium text-foreground">
                                背景信息（可选）
                            </label>
                            <Textarea
                                placeholder="提供额外的背景信息，帮助员工更好地理解任务"
                                value={contextInput}
                                onChange={(e) => setContextInput(e.target.value)}
                                className="mt-1"
                                rows={2}
                            />
                        </div>

                        {/* 执行结果 */}
                        {taskResult && (
                            <div className={`p-4 rounded-lg ${taskResult.success ? "bg-green-50 border border-green-200" : "bg-red-50 border border-red-200"
                                }`}>
                                <div className="flex items-center justify-between mb-2">
                                    <span className={`font-medium ${taskResult.success ? "text-green-700" : "text-red-700"
                                        }`}>
                                        {taskResult.success ? "✅ 执行完成" : "❌ 执行失败"}
                                    </span>
                                    {taskResult.result?.execution_time && (
                                        <span className="text-sm text-gray-500">
                                            耗时: {taskResult.result.execution_time.toFixed(1)}s
                                        </span>
                                    )}
                                </div>
                                {taskResult.result?.skills_used && taskResult.result.skills_used.length > 0 && (
                                    <div className="text-sm text-gray-600 mb-2">
                                        使用技能: {taskResult.result.skills_used.join(", ")}
                                    </div>
                                )}
                                <div className="max-w-none max-h-80 overflow-y-auto bg-gray-50 border rounded-lg p-4">
                                    {taskResult.success ? (
                                        <div className="prose prose-sm dark:prose-invert max-w-none
                                            prose-headings:text-foreground prose-headings:font-semibold
                                            prose-p:text-foreground prose-p:leading-relaxed
                                            prose-li:text-foreground
                                            prose-strong:text-foreground
                                            prose-code:bg-muted prose-code:px-1 prose-code:rounded prose-code:text-foreground
                                            prose-pre:bg-gray-800 prose-pre:text-gray-100
                                            prose-table:text-sm
                                            prose-th:bg-muted prose-th:text-foreground prose-th:px-3 prose-th:py-2
                                            prose-td:px-3 prose-td:py-2 prose-td:text-foreground prose-td:border-b
                                        ">
                                            <ReactMarkdown>
                                                {taskResult.result?.result || ""}
                                            </ReactMarkdown>
                                        </div>
                                    ) : (
                                        <p className="text-red-600 font-medium">{taskResult.error}</p>
                                    )}
                                </div>
                            </div>
                        )}

                        {/* 继续对话输入区域 - 任务完成后显示 */}
                        {taskResult?.success && (
                            <div className="p-4 bg-blue-50 border border-blue-200 rounded-lg">
                                <label className="text-sm font-medium text-blue-700 flex items-center gap-1">
                                    <MessageCircle className="h-4 w-4" />
                                    继续对话
                                </label>
                                <p className="text-xs text-blue-600 mb-2">
                                    如果员工需要更多信息，您可以在此补充
                                </p>
                                <div className="flex gap-2">
                                    <Input
                                        placeholder="输入补充信息或追问..."
                                        value={followUpInput}
                                        onChange={(e) => setFollowUpInput(e.target.value)}
                                        onKeyDown={(e) => {
                                            if (e.key === 'Enter' && !e.shiftKey) {
                                                e.preventDefault();
                                                continueConversation();
                                            }
                                        }}
                                        disabled={executing}
                                    />
                                    <Button
                                        onClick={continueConversation}
                                        disabled={executing || !followUpInput.trim()}
                                        size="sm"
                                        className="bg-blue-600 hover:bg-blue-700"
                                    >
                                        {executing ? (
                                            <Loader2 className="h-4 w-4 animate-spin" />
                                        ) : (
                                            <Send className="h-4 w-4" />
                                        )}
                                    </Button>
                                </div>
                            </div>
                        )}
                    </div>

                    <DialogFooter className="gap-2">
                        <Button
                            variant="outline"
                            onClick={() => setTaskDialogOpen(false)}
                        >
                            关闭
                        </Button>
                        {!taskResult && (
                            <Button
                                onClick={executeTask}
                                disabled={executing || !taskInput.trim()}
                            >
                                {executing ? (
                                    <>
                                        <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                        执行中...
                                    </>
                                ) : (
                                    <>
                                        <Send className="h-4 w-4 mr-1" />
                                        执行任务
                                    </>
                                )}
                            </Button>
                        )}
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* 复杂任务弹窗 */}
            <Dialog open={complexDialogOpen} onOpenChange={setComplexDialogOpen}>
                <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
                    <DialogHeader>
                        <DialogTitle className="flex items-center gap-2">
                            <Layers className="h-5 w-5 text-orange-500" />
                            复杂任务
                        </DialogTitle>
                        <DialogDescription>
                            项目负责人将自动拆解任务并分配给多个员工执行
                        </DialogDescription>
                    </DialogHeader>

                    <div className="space-y-4">
                        <div>
                            <label className="text-sm font-medium text-foreground">
                                目标描述 <span className="text-red-500">*</span>
                            </label>
                            <Textarea
                                placeholder={`例如：帮我为"某某科技"制定完整的GEO优化方案，包括可见度检测、竞品分析、报告生成`}
                                value={complexGoal}
                                onChange={(e) => setComplexGoal(e.target.value)}
                                className="mt-1"
                                rows={4}
                            />
                        </div>

                        <div>
                            <label className="text-sm font-medium text-foreground">
                                背景信息（可选）
                            </label>
                            <Textarea
                                placeholder="提供额外的背景信息，如品牌行业、竞品名称等"
                                value={complexContext}
                                onChange={(e) => setComplexContext(e.target.value)}
                                className="mt-1"
                                rows={2}
                            />
                        </div>

                        {/* 执行步骤进度 */}
                        {(complexExecuting || complexSteps.length > 0) && (
                            <div className="p-4 bg-gray-50 rounded-lg border border-gray-200">
                                <div className="flex items-center gap-2 mb-3">
                                    <Layers className="h-4 w-4 text-orange-500" />
                                    <span className="font-medium text-foreground">执行进度</span>
                                    {complexExecuting && (
                                        <Loader2 className="h-4 w-4 animate-spin text-orange-500 ml-auto" />
                                    )}
                                </div>
                                <div className="space-y-2">
                                    {complexSteps.map((step, idx) => (
                                        <div
                                            key={idx}
                                            className={`flex items-center gap-2 p-2 rounded ${step.status === 'running' ? 'bg-blue-50 border border-blue-200' :
                                                step.status === 'done' ? 'bg-green-50 border border-green-200' :
                                                    step.status === 'error' ? 'bg-red-50 border border-red-200' :
                                                        'bg-white border border-gray-100'
                                                }`}
                                        >
                                            <span className="w-5 text-center">
                                                {step.status === 'running' && <Loader2 className="h-4 w-4 animate-spin text-blue-500" />}
                                                {step.status === 'done' && <span className="text-green-500">✓</span>}
                                                {step.status === 'error' && <span className="text-red-500">✗</span>}
                                                {step.status === 'pending' && <span className="text-gray-300">○</span>}
                                            </span>
                                            <span className={`text-sm flex-1 ${step.status === 'running' ? 'text-blue-700 font-medium' :
                                                step.status === 'done' ? 'text-green-700' :
                                                    step.status === 'error' ? 'text-red-700' :
                                                        'text-gray-500'
                                                }`}>
                                                {step.step}
                                            </span>
                                        </div>
                                    ))}
                                    {complexExecuting && currentStep && !complexSteps.some(s => s.step === currentStep) && (
                                        <div className="flex items-center gap-2 p-2 rounded bg-blue-50 border border-blue-200">
                                            <Loader2 className="h-4 w-4 animate-spin text-blue-500" />
                                            <span className="text-sm text-blue-700 font-medium">{currentStep}</span>
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}

                        {/* 执行结果 */}
                        {complexResult && (
                            <div className={`p-4 rounded-lg ${complexResult.success && complexResult.status === "completed"
                                ? "bg-green-50 border border-green-200"
                                : complexResult.status === "plan_only"
                                    ? "bg-blue-50 border border-blue-200"
                                    : "bg-red-50 border border-red-200"
                                }`}>
                                <div className="flex items-center justify-between mb-2">
                                    <span className={`font-medium ${complexResult.success ? "text-green-700" : "text-red-700"
                                        }`}>
                                        {complexResult.status === "completed" && `✅ 执行完成 (${complexResult.completed_count}/${complexResult.total_count})`}
                                        {complexResult.status === "plan_only" && "📋 任务拆解完成"}
                                        {!complexResult.success && "❌ 执行失败"}
                                    </span>
                                </div>

                                {complexResult.summary && (
                                    <div className="max-h-80 overflow-y-auto bg-white border rounded-lg p-4">
                                        <div className="prose prose-sm dark:prose-invert max-w-none">
                                            <ReactMarkdown>
                                                {complexResult.summary}
                                            </ReactMarkdown>
                                        </div>
                                    </div>
                                )}

                                {complexResult.error && (
                                    <p className="text-red-600">{complexResult.error}</p>
                                )}
                            </div>
                        )}
                    </div>

                    <DialogFooter className="gap-2">
                        {complexResult && complexResult.success && savedToWorkspace && (
                            <Button
                                variant="outline"
                                onClick={() => {
                                    setComplexDialogOpen(false);
                                    navigate("/workspace");
                                }}
                                className="border-green-200 text-green-700 hover:bg-green-50"
                            >
                                <ExternalLink className="h-4 w-4 mr-1" />
                                查看工作台
                            </Button>
                        )}
                        <Button
                            variant="outline"
                            onClick={() => setComplexDialogOpen(false)}
                        >
                            关闭
                        </Button>
                        <Button
                            onClick={async () => {
                                if (!complexGoal.trim()) return;
                                setComplexExecuting(true);
                                setComplexResult(null);
                                setComplexSteps([]);
                                setCurrentStep("正在分析任务...");

                                try {
                                    // 先添加初始步骤
                                    setComplexSteps([{ step: "任务分析与规划", status: "running" }]);

                                    const response = await authFetch("/api/tasks/complex/sync", {
                                        method: "POST",
                                        headers: { "Content-Type": "application/json" },
                                        body: JSON.stringify({
                                            goal: complexGoal,
                                            context: complexContext ? { background: complexContext } : null,
                                            auto_execute: true,
                                        }),
                                    });
                                    const data = await response.json();

                                    // 根据返回的plan生成步骤显示
                                    if (data.plan && Array.isArray(data.plan)) {
                                        const steps = data.plan.map((p: any, idx: number) => ({
                                            step: `${idx + 1}. ${p.description || p.task || "执行子任务"}`,
                                            status: data.results && data.results[idx]
                                                ? (data.results[idx].success ? "done" : "error")
                                                : "done" as const,
                                        }));
                                        setComplexSteps([
                                            { step: "任务分析与规划", status: "done" },
                                            ...steps,
                                            { step: "保存到工作台", status: "running" }
                                        ]);
                                    } else {
                                        setComplexSteps([
                                            { step: "任务分析与规划", status: "done" },
                                            { step: "保存到工作台", status: "running" }
                                        ]);
                                    }

                                    setComplexResult(data);
                                    setCurrentStep("");

                                    // 自动保存到工作台
                                    if (data.success && data.summary) {
                                        try {
                                            await authFetch("/api/workspace/results", {
                                                method: "POST",
                                                headers: { "Content-Type": "application/json" },
                                                body: JSON.stringify({
                                                    title: `复杂任务: ${complexGoal.slice(0, 50)}${complexGoal.length > 50 ? '...' : ''}`,
                                                    result_type: "任务成果",
                                                    content: data.summary,
                                                    status: "已完成",
                                                    assignee_name: "项目负责人",
                                                }),
                                            });
                                            setSavedToWorkspace(true);
                                            setComplexSteps(prev => prev.map(s =>
                                                s.step === "保存到工作台" ? { ...s, status: "done" as const } : s
                                            ));
                                        } catch (saveError) {
                                            console.error("保存到工作台失败:", saveError);
                                            setComplexSteps(prev => prev.map(s =>
                                                s.step === "保存到工作台" ? { ...s, status: "error" as const } : s
                                            ));
                                        }
                                    }
                                } catch (error) {
                                    setComplexSteps([{ step: "任务分析与规划", status: "error" }]);
                                    setComplexResult({
                                        success: false,
                                        error: String(error),
                                    });
                                    setCurrentStep("");
                                } finally {
                                    setComplexExecuting(false);
                                }
                            }}
                            disabled={complexExecuting || !complexGoal.trim()}
                            className="bg-linear-to-r from-orange-500 to-red-500"
                        >
                            {complexExecuting ? (
                                <>
                                    <Loader2 className="h-4 w-4 animate-spin mr-1" />
                                    执行中...
                                </>
                            ) : (
                                <>
                                    <Layers className="h-4 w-4 mr-1" />
                                    提交任务
                                </>
                            )}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}
