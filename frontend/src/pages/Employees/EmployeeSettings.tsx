import { authFetch } from '@/lib/api';
import { toast } from 'sonner';
import { useState, useEffect } from "react";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
    DialogFooter,
} from "@/components/ui/dialog";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Switch } from "@/components/ui/switch";
import {
    Loader2,
    Plus,
    Save,
    Trash2,
    Settings,
    Users,
    Building2,
    Bot,
    Sparkles,
    ArrowLeft,
    BookOpen,
} from "lucide-react";
import { RoleKnowledgeTab } from "@/components/RoleKnowledgeTab";
import { useNavigate } from "react-router-dom";
import { useConfirmDialog } from '@/components/ui/confirm-dialog';

// 员工配置类型
interface EmployeeConfig {
    id: string;
    name: string;
    department_id: string;
    department_name?: string;
    avatar: string;
    description: string;
    model_id: string;
    temperature: number;
    max_tokens: number;
    system_prompt: string;
    skills: string[];
    mcp_config: Record<string, any>;
    memory_type: string;
    usage_scope: string;
    is_active: boolean;
    sort_order: number;
}

// 部门类型
interface Department {
    id: string;
    name: string;
    icon: string;
    description: string;
    is_active: boolean;
    sort_order: number;
}

// 可用模型列表(2026-05-22 V3.2 → V4 全切)
const AVAILABLE_MODELS = [
    { id: "deepseek-v4-flash", name: "DeepSeek V4 Flash", provider: "DeepSeek" },
    { id: "deepseek-v4-pro", name: "DeepSeek V4 Pro", provider: "DeepSeek" },
    { id: "deepseek-chat", name: "DeepSeek Chat (V4 Flash alias)", provider: "DeepSeek" },
    { id: "qwen3-max", name: "Qwen3 Max", provider: "Alibaba" },
    { id: "qwen3.6-plus", name: "Qwen3.5 Plus", provider: "Alibaba" },
    { id: "doubao-seed-2-0-pro-260215", name: "Doubao Seed 2.0 Pro", provider: "ByteDance" },
    { id: "kimi-k2.6", name: "Kimi K2.6", provider: "Moonshot" },
    { id: "anthropic/claude-sonnet-4.6", name: "Claude Sonnet 4.6", provider: "OpenRouter" },
    { id: "openai/gpt-5.1", name: "GPT 5.1", provider: "OpenRouter" },
];

// 可用技能列表
const AVAILABLE_SKILLS = [
    // 数据采集
    { id: "douyin_search", name: "抖音搜索", category: "采集" },
    { id: "xhs_search", name: "小红书搜索", category: "采集" },
    { id: "web_search", name: "网页搜索", category: "采集" },
    // AI测试
    { id: "ai_visibility_test", name: "AI可见度测试", category: "测试" },
    { id: "citation_analysis", name: "引用分析", category: "测试" },
    // 竞品分析
    { id: "competitor_identification", name: "竞品识别", category: "分析" },
    { id: "gap_analysis", name: "差距分析", category: "分析" },
    { id: "viral_content_analysis", name: "爆款拆解", category: "分析" },
    // 报告
    { id: "geo_scoring", name: "GEO评分", category: "报告" },
    { id: "report_generation", name: "报告生成", category: "报告" },
    // 内容策划
    { id: "topic_planning", name: "选题规划", category: "策划" },
    { id: "title_generation", name: "标题生成", category: "策划" },
    { id: "trend_mining", name: "热点挖掘", category: "策划" },
    // 内容撰写
    { id: "article_writing", name: "长文撰写", category: "撰写" },
    { id: "content_rewriting", name: "内容改写", category: "撰写" },
    { id: "deai_optimization", name: "去AI味", category: "撰写" },
    // 抖音内容
    { id: "hook_design", name: "钩子设计", category: "抖音" },
    { id: "script_writing", name: "脚本撰写", category: "抖音" },
    { id: "douyin_copywriting", name: "抖音话术", category: "抖音" },
    // 小红书
    { id: "seeding_copywriting", name: "种草文案", category: "小红书" },
    { id: "cover_design", name: "封面设计", category: "小红书" },
    { id: "hashtag_strategy", name: "标签策略", category: "小红书" },
    // PPT
    { id: "ppt_generation", name: "PPT生成", category: "PPT" },
    { id: "template_design", name: "模板设计", category: "PPT" },
    // 审核
    { id: "quality_review", name: "质量审核", category: "审核" },
    { id: "style_unification", name: "风格统一", category: "审核" },
    { id: "compliance_check", name: "合规检查", category: "审核" },
    // 专家
    { id: "industry_insight", name: "行业洞察", category: "专家" },
    { id: "knowledge_retrieval", name: "知识检索", category: "专家" },
    { id: "expert_consultation", name: "专家咨询", category: "专家" },
];

// 常用emoji头像
const AVATAR_OPTIONS = ["🤖", "📡", "🎯", "✍️", "📱", "📕", "📊", "✏️", "🧠", "📈", "📋", "🔍", "💡", "🎨", "🚀"];

export function EmployeeSettings() {
    const [confirmDialog, askConfirm] = useConfirmDialog();
    const navigate = useNavigate();
    const [employees, setEmployees] = useState<EmployeeConfig[]>([]);
    const [departments, setDepartments] = useState<Department[]>([]);
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);

    // 编辑状态
    const [editDialogOpen, setEditDialogOpen] = useState(false);
    const [editingEmployee, setEditingEmployee] = useState<EmployeeConfig | null>(null);
    const [isNewEmployee, setIsNewEmployee] = useState(false);

    // 部门编辑
    const [deptDialogOpen, setDeptDialogOpen] = useState(false);
    const [editingDept, setEditingDept] = useState<Department | null>(null);
    const [isNewDept, setIsNewDept] = useState(false);

    // 能力配置
    const [capDialogOpen, setCapDialogOpen] = useState(false);
    const [capEmployeeId, setCapEmployeeId] = useState("");
    const [capEmployeeName, setCapEmployeeName] = useState("");
    const [capabilities, setCapabilities] = useState<{
        call_employee: string[];
        call_advisor: string[];
        execute_tool: string[];
    }>({ call_employee: [], call_advisor: [], execute_tool: [] });
    const [savingCaps, setSavingCaps] = useState(false);
    const [advisors, setAdvisors] = useState<any[]>([]);

    // 加载数据
    useEffect(() => {
        fetchData();
    }, []);

    const fetchData = async () => {
        setLoading(true);
        try {
            const [empRes, deptRes] = await Promise.all([
                authFetch("/api/employee-configs"),
                authFetch("/api/departments"),
            ]);
            const empData = await empRes.json();
            const deptData = await deptRes.json();

            if (empData.success) {
                setEmployees(empData.employees);
            }
            if (deptData.success) {
                setDepartments(deptData.departments);
            }
        } catch (error) {
            console.error("Failed to fetch data:", error);
        } finally {
            setLoading(false);
        }
    };

    // 打开新建员工对话框
    const handleNewEmployee = () => {
        setEditingEmployee({
            id: "",
            name: "",
            department_id: departments[0]?.id || "diagnosis",
            avatar: "🤖",
            description: "",
            model_id: "deepseek-v4-flash",  // 2026-05-22 V3.2 → V4 全切
            temperature: 0.7,
            max_tokens: 15000,
            system_prompt: "",
            skills: [],
            mcp_config: {},
            memory_type: "temporary",
            usage_scope: "all",
            is_active: true,
            sort_order: 0,
        });
        setIsNewEmployee(true);
        setEditDialogOpen(true);
    };

    // 打开编辑员工对话框
    const handleEditEmployee = (emp: EmployeeConfig) => {
        setEditingEmployee({ ...emp });
        setIsNewEmployee(false);
        setEditDialogOpen(true);
    };

    // 保存员工
    const handleSaveEmployee = async () => {
        if (!editingEmployee) return;

        setSaving(true);
        try {
            const res = await authFetch("/api/employee-configs", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(editingEmployee),
            });
            const data = await res.json();

            if (data.success) {
                setEditDialogOpen(false);
                fetchData();
            } else {
                toast.error("保存失败: " + data.error);
            }
        } catch (error) {
            console.error("Save failed:", error);
            toast.error("保存失败");
        } finally {
            setSaving(false);
        }
    };

    // 删除员工
    const handleDeleteEmployee = async (id: string) => {
        if (!(await askConfirm({ title: "确定要删除该员工吗？", danger: true }))) return;

        try {
            const res = await authFetch(`/api/employee-configs/${id}`, {
                method: "DELETE",
            });
            const data = await res.json();

            if (data.success) {
                fetchData();
            }
        } catch (error) {
            console.error("Delete failed:", error);
        }
    };

    // 新建部门
    const handleNewDept = () => {
        setEditingDept({
            id: "",
            name: "",
            icon: "📁",
            description: "",
            is_active: true,
            sort_order: departments.length,
        });
        setIsNewDept(true);
        setDeptDialogOpen(true);
    };

    // 编辑部门
    const handleEditDept = (dept: Department) => {
        setEditingDept({ ...dept });
        setIsNewDept(false);
        setDeptDialogOpen(true);
    };

    // 保存部门
    const handleSaveDept = async () => {
        if (!editingDept) return;

        setSaving(true);
        try {
            const res = await authFetch("/api/departments", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(editingDept),
            });
            const data = await res.json();

            if (data.success) {
                setDeptDialogOpen(false);
                fetchData();
            }
        } catch (error) {
            console.error("Save dept failed:", error);
        } finally {
            setSaving(false);
        }
    };

    // 切换技能
    const toggleSkill = (skillId: string) => {
        if (!editingEmployee) return;
        const skills = editingEmployee.skills.includes(skillId)
            ? editingEmployee.skills.filter((s) => s !== skillId)
            : [...editingEmployee.skills, skillId];
        setEditingEmployee({ ...editingEmployee, skills });
    };

    // 打开能力配置
    const openCapabilityConfig = async (emp: EmployeeConfig) => {
        setCapEmployeeId(emp.id);
        setCapEmployeeName(emp.name);
        setCapDialogOpen(true);

        // 获取顾问列表
        try {
            const res = await authFetch("/api/advisors");
            const data = await res.json();
            if (data.success) {
                setAdvisors(data.advisors || []);
            }
        } catch (e) { }

        // 获取当前能力配置
        try {
            const res = await authFetch(`/api/employees/${emp.id}/capabilities`);
            const data = await res.json();
            if (data.success && data.capabilities) {
                setCapabilities({
                    call_employee: data.capabilities.call_employee?.map((c: any) => c.target_id) || [],
                    call_advisor: data.capabilities.call_advisor?.map((c: any) => c.target_id) || [],
                    execute_tool: data.capabilities.execute_tool?.map((c: any) => c.target_id) || [],
                });
            } else {
                setCapabilities({ call_employee: [], call_advisor: [], execute_tool: [] });
            }
        } catch (e) {
            setCapabilities({ call_employee: [], call_advisor: [], execute_tool: [] });
        }
    };

    // 保存能力配置
    const saveCapabilities = async () => {
        setSavingCaps(true);
        try {
            await authFetch(`/api/employees/${capEmployeeId}/capabilities`, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(capabilities),
            });
            setCapDialogOpen(false);
        } catch (e) { }
        setSavingCaps(false);
    };

    // 切换能力
    const toggleCapability = (type: "call_employee" | "call_advisor" | "execute_tool", targetId: string) => {
        const list = capabilities[type];
        if (list.includes(targetId)) {
            setCapabilities({ ...capabilities, [type]: list.filter(id => id !== targetId) });
        } else {
            setCapabilities({ ...capabilities, [type]: [...list, targetId] });
        }
    };

    // 按部门分组员工
    const employeesByDept: Record<string, EmployeeConfig[]> = {};
    employees.forEach((emp) => {
        if (!employeesByDept[emp.department_id]) {
            employeesByDept[emp.department_id] = [];
        }
        employeesByDept[emp.department_id].push(emp);
    });

    if (loading) {
        return (
            <div className="flex items-center justify-center min-h-[400px]">
                <Loader2 className="h-8 w-8 animate-spin text-purple-500" />
            </div>
        );
    }

    return (
        <div className="container mx-auto p-6 max-w-7xl">
            {/* 页面标题 */}
            <div className="flex items-center justify-between mb-6">
                <div className="flex items-center gap-4">
                    <Button variant="ghost" size="icon" onClick={() => navigate("/employees")}>
                        <ArrowLeft className="h-5 w-5" />
                    </Button>
                    <div>
                        <h1 className="text-2xl font-bold flex items-center gap-2">
                            <Settings className="h-6 w-6" />
                            员工设置
                        </h1>
                        <p className="text-muted-foreground">
                            配置员工模型、提示词、技能和MCP
                        </p>
                    </div>
                </div>
                <div className="flex gap-2">
                    <Button variant="outline" onClick={handleNewDept}>
                        <Building2 className="h-4 w-4 mr-2" />
                        新增部门
                    </Button>
                    <Button onClick={handleNewEmployee}>
                        <Plus className="h-4 w-4 mr-2" />
                        新增员工
                    </Button>
                </div>
            </div>

            {/* 统计卡片 */}
            <div className="grid grid-cols-3 gap-4 mb-6">
                <Card>
                    <CardContent className="p-4 flex items-center gap-4">
                        <div className="p-3 bg-purple-100 rounded-lg">
                            <Users className="h-6 w-6 text-purple-600" />
                        </div>
                        <div>
                            <div className="text-2xl font-bold">{employees.length}</div>
                            <div className="text-sm text-muted-foreground">员工总数</div>
                        </div>
                    </CardContent>
                </Card>
                <Card>
                    <CardContent className="p-4 flex items-center gap-4">
                        <div className="p-3 bg-blue-100 rounded-lg">
                            <Building2 className="h-6 w-6 text-blue-600" />
                        </div>
                        <div>
                            <div className="text-2xl font-bold">{departments.length}</div>
                            <div className="text-sm text-muted-foreground">部门数量</div>
                        </div>
                    </CardContent>
                </Card>
                <Card>
                    <CardContent className="p-4 flex items-center gap-4">
                        <div className="p-3 bg-green-100 rounded-lg">
                            <Bot className="h-6 w-6 text-green-600" />
                        </div>
                        <div>
                            <div className="text-2xl font-bold">
                                {employees.filter((e) => e.is_active).length}
                            </div>
                            <div className="text-sm text-muted-foreground">在线员工</div>
                        </div>
                    </CardContent>
                </Card>
            </div>

            {/* 部门和员工列表 */}
            <Tabs defaultValue="employees" className="w-full">
                <TabsList className="mb-4">
                    <TabsTrigger value="employees">
                        <Users className="h-4 w-4 mr-2" />
                        员工列表
                    </TabsTrigger>
                    <TabsTrigger value="departments">
                        <Building2 className="h-4 w-4 mr-2" />
                        部门管理
                    </TabsTrigger>
                </TabsList>

                <TabsContent value="employees">
                    <div className="space-y-6">
                        {departments.map((dept) => (
                            <Card key={dept.id}>
                                <CardHeader className="pb-3">
                                    <CardTitle className="text-lg flex items-center gap-2">
                                        <span>{dept.icon}</span>
                                        {dept.name}
                                        <Badge variant="secondary" className="ml-2">
                                            {employeesByDept[dept.id]?.length || 0} 人
                                        </Badge>
                                    </CardTitle>
                                    <CardDescription>{dept.description}</CardDescription>
                                </CardHeader>
                                <CardContent>
                                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                                        {employeesByDept[dept.id]?.map((emp) => (
                                            <Card
                                                key={emp.id}
                                                className="cursor-pointer hover:border-purple-300 transition-colors"
                                                onClick={() => handleEditEmployee(emp)}
                                            >
                                                <CardContent className="p-4">
                                                    <div className="flex items-start justify-between">
                                                        <div className="flex items-center gap-3">
                                                            <div className="text-3xl">{emp.avatar}</div>
                                                            <div>
                                                                <div className="font-medium">{emp.name}</div>
                                                                <div className="text-sm text-muted-foreground">
                                                                    {emp.model_id}
                                                                </div>
                                                            </div>
                                                        </div>
                                                        <Badge
                                                            variant={emp.is_active ? "default" : "secondary"}
                                                        >
                                                            {emp.is_active ? "在线" : "离线"}
                                                        </Badge>
                                                    </div>
                                                    <p className="text-sm text-muted-foreground mt-2 line-clamp-2">
                                                        {emp.description}
                                                    </p>
                                                    <div className="flex flex-wrap gap-1 mt-3">
                                                        {emp.skills.slice(0, 3).map((skill) => (
                                                            <Badge
                                                                key={skill}
                                                                variant="outline"
                                                                className="text-xs"
                                                            >
                                                                {AVAILABLE_SKILLS.find((s) => s.id === skill)?.name || skill}
                                                            </Badge>
                                                        ))}
                                                        {emp.skills.length > 3 && (
                                                            <Badge variant="outline" className="text-xs">
                                                                +{emp.skills.length - 3}
                                                            </Badge>
                                                        )}
                                                    </div>
                                                    <Button
                                                        variant="ghost"
                                                        size="sm"
                                                        className="mt-2 w-full justify-start text-xs text-purple-600"
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            openCapabilityConfig(emp);
                                                        }}
                                                    >
                                                        <Sparkles className="h-3 w-3 mr-1" />
                                                        能力配置
                                                    </Button>
                                                </CardContent>
                                            </Card>
                                        ))}
                                        {/* 添加员工到该部门 */}
                                        <Card
                                            className="cursor-pointer border-dashed hover:border-purple-300 transition-colors"
                                            onClick={() => {
                                                handleNewEmployee();
                                                if (editingEmployee) {
                                                    setEditingEmployee({
                                                        ...editingEmployee,
                                                        department_id: dept.id,
                                                    });
                                                }
                                            }}
                                        >
                                            <CardContent className="p-4 flex items-center justify-center h-full min-h-[120px]">
                                                <div className="text-center text-muted-foreground">
                                                    <Plus className="h-8 w-8 mx-auto mb-2" />
                                                    <span>添加员工</span>
                                                </div>
                                            </CardContent>
                                        </Card>
                                    </div>
                                </CardContent>
                            </Card>
                        ))}
                    </div>
                </TabsContent>

                <TabsContent value="departments">
                    <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                        {departments.map((dept) => (
                            <Card
                                key={dept.id}
                                className="cursor-pointer hover:border-purple-300 transition-colors"
                                onClick={() => handleEditDept(dept)}
                            >
                                <CardContent className="p-4">
                                    <div className="flex items-center gap-3 mb-2">
                                        <div className="text-3xl">{dept.icon}</div>
                                        <div>
                                            <div className="font-medium">{dept.name}</div>
                                            <div className="text-sm text-muted-foreground">
                                                ID: {dept.id}
                                            </div>
                                        </div>
                                    </div>
                                    <p className="text-sm text-muted-foreground">
                                        {dept.description}
                                    </p>
                                    <div className="mt-2">
                                        <Badge variant="secondary">
                                            {employeesByDept[dept.id]?.length || 0} 名员工
                                        </Badge>
                                    </div>
                                </CardContent>
                            </Card>
                        ))}
                        {/* 添加部门 */}
                        <Card
                            className="cursor-pointer border-dashed hover:border-purple-300 transition-colors"
                            onClick={handleNewDept}
                        >
                            <CardContent className="p-4 flex items-center justify-center h-full min-h-[120px]">
                                <div className="text-center text-muted-foreground">
                                    <Plus className="h-8 w-8 mx-auto mb-2" />
                                    <span>添加部门</span>
                                </div>
                            </CardContent>
                        </Card>
                    </div>
                </TabsContent>
            </Tabs>

            {/* 员工编辑对话框 */}
            <Dialog open={editDialogOpen} onOpenChange={setEditDialogOpen}>
                <DialogContent className="max-w-3xl max-h-[90vh] overflow-y-auto">
                    <DialogHeader>
                        <DialogTitle>
                            {isNewEmployee ? "新增员工" : `编辑员工: ${editingEmployee?.name}`}
                        </DialogTitle>
                        <DialogDescription>
                            配置员工的模型、提示词、技能和MCP
                        </DialogDescription>
                    </DialogHeader>

                    {editingEmployee && (
                        <Tabs defaultValue="basic" className="w-full">
                            <TabsList className="mb-4">
                                <TabsTrigger value="basic">基本信息</TabsTrigger>
                                <TabsTrigger value="model">模型配置</TabsTrigger>
                                <TabsTrigger value="prompt">提示词</TabsTrigger>
                                <TabsTrigger value="skills">技能配置</TabsTrigger>
                                <TabsTrigger value="knowledge">
                                    <BookOpen className="h-4 w-4 mr-1" />知识库
                                </TabsTrigger>
                            </TabsList>

                            <TabsContent value="basic" className="space-y-4">
                                <div className="grid grid-cols-2 gap-4">
                                    <div className="space-y-2">
                                        <Label>员工ID</Label>
                                        <Input
                                            value={editingEmployee.id}
                                            onChange={(e) =>
                                                setEditingEmployee({
                                                    ...editingEmployee,
                                                    id: e.target.value,
                                                })
                                            }
                                            placeholder="如: content_writer"
                                            disabled={!isNewEmployee}
                                        />
                                    </div>
                                    <div className="space-y-2">
                                        <Label>员工名称</Label>
                                        <Input
                                            value={editingEmployee.name}
                                            onChange={(e) =>
                                                setEditingEmployee({
                                                    ...editingEmployee,
                                                    name: e.target.value,
                                                })
                                            }
                                            placeholder="如: 正文撰稿人"
                                        />
                                    </div>
                                </div>

                                <div className="grid grid-cols-2 gap-4">
                                    <div className="space-y-2">
                                        <Label>所属部门</Label>
                                        <Select
                                            value={editingEmployee.department_id}
                                            onValueChange={(v) =>
                                                setEditingEmployee({
                                                    ...editingEmployee,
                                                    department_id: v,
                                                })
                                            }
                                        >
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                {departments.map((d) => (
                                                    <SelectItem key={d.id} value={d.id}>
                                                        {d.icon} {d.name}
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <Label>头像</Label>
                                        <div className="flex gap-2 flex-wrap">
                                            {AVATAR_OPTIONS.map((avatar) => (
                                                <Button
                                                    key={avatar}
                                                    variant={
                                                        editingEmployee.avatar === avatar
                                                            ? "default"
                                                            : "outline"
                                                    }
                                                    size="sm"
                                                    onClick={() =>
                                                        setEditingEmployee({
                                                            ...editingEmployee,
                                                            avatar,
                                                        })
                                                    }
                                                >
                                                    {avatar}
                                                </Button>
                                            ))}
                                        </div>
                                    </div>
                                </div>

                                <div className="space-y-2">
                                    <Label>员工描述</Label>
                                    <Textarea
                                        value={editingEmployee.description}
                                        onChange={(e) =>
                                            setEditingEmployee({
                                                ...editingEmployee,
                                                description: e.target.value,
                                            })
                                        }
                                        placeholder="描述员工的职责和能力"
                                        rows={2}
                                    />
                                </div>

                                <div className="grid grid-cols-2 gap-4">
                                    <div className="space-y-2">
                                        <Label>使用场景</Label>
                                        <Select
                                            value={editingEmployee.usage_scope}
                                            onValueChange={(v) =>
                                                setEditingEmployee({
                                                    ...editingEmployee,
                                                    usage_scope: v,
                                                })
                                            }
                                        >
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="all">全部场景</SelectItem>
                                                <SelectItem value="single">仅单独调用</SelectItem>
                                                <SelectItem value="meeting">仅圆桌会议</SelectItem>
                                                <SelectItem value="workflow">仅工作流</SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>
                                    <div className="space-y-2">
                                        <Label>记忆类型</Label>
                                        <Select
                                            value={editingEmployee.memory_type}
                                            onValueChange={(v) =>
                                                setEditingEmployee({
                                                    ...editingEmployee,
                                                    memory_type: v,
                                                })
                                            }
                                        >
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="temporary">临时记忆</SelectItem>
                                                <SelectItem value="persistent">持久记忆</SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>
                                </div>

                                <div className="flex items-center space-x-2">
                                    <Switch
                                        checked={editingEmployee.is_active}
                                        onCheckedChange={(checked) =>
                                            setEditingEmployee({
                                                ...editingEmployee,
                                                is_active: checked,
                                            })
                                        }
                                    />
                                    <Label>启用员工</Label>
                                </div>
                            </TabsContent>

                            <TabsContent value="model" className="space-y-4">
                                <div className="space-y-2">
                                    <Label>AI模型</Label>
                                    <Select
                                        value={editingEmployee.model_id}
                                        onValueChange={(v) =>
                                            setEditingEmployee({
                                                ...editingEmployee,
                                                model_id: v,
                                            })
                                        }
                                    >
                                        <SelectTrigger>
                                            <SelectValue />
                                        </SelectTrigger>
                                        <SelectContent>
                                            {AVAILABLE_MODELS.map((m) => (
                                                <SelectItem key={m.id} value={m.id}>
                                                    {m.name} ({m.provider})
                                                </SelectItem>
                                            ))}
                                        </SelectContent>
                                    </Select>
                                </div>

                                <div className="grid grid-cols-2 gap-4">
                                    <div className="space-y-2">
                                        <Label>
                                            Temperature: {editingEmployee.temperature}
                                        </Label>
                                        <Input
                                            type="range"
                                            min="0"
                                            max="1"
                                            step="0.1"
                                            value={editingEmployee.temperature}
                                            onChange={(e) =>
                                                setEditingEmployee({
                                                    ...editingEmployee,
                                                    temperature: parseFloat(e.target.value),
                                                })
                                            }
                                        />
                                        <p className="text-xs text-muted-foreground">
                                            较低值更确定，较高值更创意
                                        </p>
                                    </div>
                                    <div className="space-y-2">
                                        <Label>Max Tokens</Label>
                                        <Input
                                            type="number"
                                            value={editingEmployee.max_tokens}
                                            onChange={(e) =>
                                                setEditingEmployee({
                                                    ...editingEmployee,
                                                    max_tokens: parseInt(e.target.value),
                                                })
                                            }
                                        />
                                    </div>
                                </div>
                            </TabsContent>

                            <TabsContent value="prompt" className="space-y-4">
                                <div className="space-y-2">
                                    <Label>系统提示词</Label>
                                    <Textarea
                                        value={editingEmployee.system_prompt || ""}
                                        onChange={(e) =>
                                            setEditingEmployee({
                                                ...editingEmployee,
                                                system_prompt: e.target.value,
                                            })
                                        }
                                        placeholder="定义员工的角色、职责和输出格式..."
                                        rows={15}
                                        className="font-mono text-sm"
                                    />
                                    <p className="text-xs text-muted-foreground">
                                        支持Markdown格式，建议包含角色定义、核心职责、输出格式要求
                                    </p>
                                </div>
                            </TabsContent>

                            <TabsContent value="skills" className="space-y-4">
                                <div className="space-y-2">
                                    <Label>
                                        已选技能 ({editingEmployee.skills.length})
                                    </Label>
                                    <div className="flex flex-wrap gap-2 min-h-[40px] p-2 border rounded-md">
                                        {editingEmployee.skills.length === 0 ? (
                                            <span className="text-muted-foreground text-sm">
                                                点击下方技能添加
                                            </span>
                                        ) : (
                                            editingEmployee.skills.map((skillId) => {
                                                const skill = AVAILABLE_SKILLS.find(
                                                    (s) => s.id === skillId
                                                );
                                                return (
                                                    <Badge
                                                        key={skillId}
                                                        variant="default"
                                                        className="cursor-pointer"
                                                        onClick={() => toggleSkill(skillId)}
                                                    >
                                                        {skill?.name || skillId} ×
                                                    </Badge>
                                                );
                                            })
                                        )}
                                    </div>
                                </div>

                                <div className="space-y-2">
                                    <Label>可用技能</Label>
                                    <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
                                        {Object.entries(
                                            AVAILABLE_SKILLS.reduce((acc, skill) => {
                                                if (!acc[skill.category]) acc[skill.category] = [];
                                                acc[skill.category].push(skill);
                                                return acc;
                                            }, {} as Record<string, typeof AVAILABLE_SKILLS>)
                                        ).map(([category, skills]) => (
                                            <div key={category} className="border rounded-md p-2">
                                                <div className="font-medium text-sm mb-2">
                                                    {category}
                                                </div>
                                                <div className="flex flex-wrap gap-1">
                                                    {skills.map((skill) => (
                                                        <Badge
                                                            key={skill.id}
                                                            variant={
                                                                editingEmployee.skills.includes(skill.id)
                                                                    ? "default"
                                                                    : "outline"
                                                            }
                                                            className="cursor-pointer text-xs"
                                                            onClick={() => toggleSkill(skill.id)}
                                                        >
                                                            {skill.name}
                                                        </Badge>
                                                    ))}
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                </div>
                            </TabsContent>

                            <TabsContent value="knowledge">
                                {!isNewEmployee && editingEmployee && (
                                    <RoleKnowledgeTab
                                        roleType="employee"
                                        roleId={editingEmployee.id}
                                        roleName={editingEmployee.name}
                                    />
                                )}
                                {isNewEmployee && (
                                    <div className="flex flex-col items-center justify-center h-32 text-muted-foreground">
                                        <BookOpen className="h-8 w-8 mb-2 opacity-50" />
                                        <p>请先保存员工后再管理知识库</p>
                                    </div>
                                )}
                            </TabsContent>
                        </Tabs>
                    )}

                    <DialogFooter className="flex justify-between">
                        <div>
                            {!isNewEmployee && (
                                <Button
                                    variant="destructive"
                                    onClick={() => {
                                        if (editingEmployee) {
                                            handleDeleteEmployee(editingEmployee.id);
                                            setEditDialogOpen(false);
                                        }
                                    }}
                                >
                                    <Trash2 className="h-4 w-4 mr-2" />
                                    删除
                                </Button>
                            )}
                        </div>
                        <div className="flex gap-2">
                            <Button variant="outline" onClick={() => setEditDialogOpen(false)}>
                                取消
                            </Button>
                            <Button onClick={handleSaveEmployee} disabled={saving}>
                                {saving ? (
                                    <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                                ) : (
                                    <Save className="h-4 w-4 mr-2" />
                                )}
                                保存
                            </Button>
                        </div>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* 部门编辑对话框 */}
            <Dialog open={deptDialogOpen} onOpenChange={setDeptDialogOpen}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>
                            {isNewDept ? "新增部门" : `编辑部门: ${editingDept?.name}`}
                        </DialogTitle>
                    </DialogHeader>

                    {editingDept && (
                        <div className="space-y-4">
                            <div className="grid grid-cols-2 gap-4">
                                <div className="space-y-2">
                                    <Label>部门ID</Label>
                                    <Input
                                        value={editingDept.id}
                                        onChange={(e) =>
                                            setEditingDept({ ...editingDept, id: e.target.value })
                                        }
                                        placeholder="如: marketing"
                                        disabled={!isNewDept}
                                    />
                                </div>
                                <div className="space-y-2">
                                    <Label>部门名称</Label>
                                    <Input
                                        value={editingDept.name}
                                        onChange={(e) =>
                                            setEditingDept({ ...editingDept, name: e.target.value })
                                        }
                                        placeholder="如: 市场部"
                                    />
                                </div>
                            </div>

                            <div className="space-y-2">
                                <Label>图标</Label>
                                <div className="flex gap-2 flex-wrap">
                                    {["📊", "✍️", "📑", "🎯", "📈", "💡", "🔍", "📁"].map(
                                        (icon) => (
                                            <Button
                                                key={icon}
                                                variant={
                                                    editingDept.icon === icon ? "default" : "outline"
                                                }
                                                size="sm"
                                                onClick={() =>
                                                    setEditingDept({ ...editingDept, icon })
                                                }
                                            >
                                                {icon}
                                            </Button>
                                        )
                                    )}
                                </div>
                            </div>

                            <div className="space-y-2">
                                <Label>描述</Label>
                                <Textarea
                                    value={editingDept.description}
                                    onChange={(e) =>
                                        setEditingDept({
                                            ...editingDept,
                                            description: e.target.value,
                                        })
                                    }
                                    placeholder="部门职责描述"
                                    rows={2}
                                />
                            </div>
                        </div>
                    )}

                    <DialogFooter>
                        <Button variant="outline" onClick={() => setDeptDialogOpen(false)}>
                            取消
                        </Button>
                        <Button onClick={handleSaveDept} disabled={saving}>
                            {saving ? (
                                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            ) : (
                                <Save className="h-4 w-4 mr-2" />
                            )}
                            保存
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* 能力配置对话框 */}
            <Dialog open={capDialogOpen} onOpenChange={setCapDialogOpen}>
                <DialogContent className="max-w-lg">
                    <DialogHeader>
                        <DialogTitle>能力配置 - {capEmployeeName}</DialogTitle>
                        <DialogDescription>
                            配置该员工可以调用的其他员工和顾问
                        </DialogDescription>
                    </DialogHeader>

                    <div className="space-y-4 max-h-[400px] overflow-y-auto">
                        {/* 可调用员工 */}
                        <div>
                            <h4 className="text-sm font-medium mb-2">可调用员工</h4>
                            <div className="grid grid-cols-2 gap-2">
                                {employees.filter(e => e.id !== capEmployeeId).map((emp) => (
                                    <label key={emp.id} className="flex items-center gap-2 p-2 rounded border hover:bg-gray-50 cursor-pointer">
                                        <Checkbox
                                            checked={capabilities.call_employee.includes(emp.id)}
                                            onCheckedChange={() => toggleCapability("call_employee", emp.id)}
                                        />
                                        <span className="text-xl">{emp.avatar}</span>
                                        <span className="text-sm truncate">{emp.name}</span>
                                    </label>
                                ))}
                            </div>
                        </div>

                        {/* 可调用顾问 */}
                        <div>
                            <h4 className="text-sm font-medium mb-2">可调用顾问</h4>
                            <div className="grid grid-cols-2 gap-2">
                                {advisors.map((adv) => (
                                    <label key={adv.id} className="flex items-center gap-2 p-2 rounded border hover:bg-gray-50 cursor-pointer">
                                        <Checkbox
                                            checked={capabilities.call_advisor.includes(adv.id)}
                                            onCheckedChange={() => toggleCapability("call_advisor", adv.id)}
                                        />
                                        <span className="text-xl">{adv.avatar || "🧑‍🏫"}</span>
                                        <span className="text-sm truncate">{adv.name}</span>
                                    </label>
                                ))}
                                {advisors.length === 0 && (
                                    <p className="text-sm text-gray-400 col-span-2">暂无顾问</p>
                                )}
                            </div>
                        </div>
                    </div>

                    <DialogFooter>
                        <Button variant="outline" onClick={() => setCapDialogOpen(false)}>
                            取消
                        </Button>
                        <Button onClick={saveCapabilities} disabled={savingCaps}>
                            {savingCaps ? (
                                <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                            ) : (
                                <Save className="h-4 w-4 mr-2" />
                            )}
                            保存
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
          {confirmDialog}
        </div>
    );
}
