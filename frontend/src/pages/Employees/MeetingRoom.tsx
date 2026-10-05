import { authFetch } from '@/lib/api';
import { useState, useEffect, useRef } from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Checkbox } from "@/components/ui/checkbox";
import {
    Select,
    SelectContent,
    SelectItem,
    SelectTrigger,
    SelectValue,
} from "@/components/ui/select";
import { SearchableSelect } from "@/components/ui/searchable-select";
import { Loader2, Users, MessageSquare, Play, ArrowLeft, Upload, Building2, Home, FileText, X, Hand, ClipboardList } from "lucide-react";
import { useNavigate } from "react-router-dom";
import ReactMarkdown from "@/components/SafeMarkdown"; // [2026-07-22 板块B] 全站 Markdown SSOT（verify-markdown-ssot 门禁）
import { TaskExecutionBoard } from "@/components/TaskExecutionBoard";
import { useBranding } from "@/hooks/useWhitelabel";
import { useAuth } from "@/context/AuthContext";
import { industryCategoryText, useIndustryTaxonomy } from "@/lib/industryTaxonomy";

// 员工类型
interface Employee {
    id: string;
    name: string;
    department: string;
    description: string;
    skills: string[];
}

// 客户/品牌类型
interface Brand {
    id: number;
    name: string;
    /** [WO_267] 存量是旧中文名、新写入是英文 key —— 显示一律走 industryCategoryText */
    industry_category?: string;
    industry_category_name?: string | null;
    company_name?: string;
}

// 上传文件类型
interface UploadedFile {
    name: string;
    type: string;
    content: string;
    size: number;
}

// 会议记录类型
interface TranscriptItem {
    role: string;
    speaker: string;
    speaker_id: string;
    content: string;
    round: number;
    type: string;
}

// 流式事件类型 - v3.0
interface StreamEvent {
    type: "start" | "speaking" | "message" | "complete" | "error" | "confirm_start" | "meeting_closed"
    | "question_start" | "data_prepared"
    | "phase_change" | "material_digest" | "gap_analysis" | "round_start" | "info_gap"
    | "report_ready" | "awaiting_confirmation";
    data: Record<string, unknown>;
}

// 正在发言状态
interface SpeakingState {
    speaker: string;
    speaker_id: string;
    phase: string;
    round?: number;
}

// 公司信息（总部）- 名称走白标 brand（OEM 出代理品牌 · 否则平台默认），见组件内 hqName
const COMPANY_INFO = {
    id: 0,
    description: "我们公司的内部事务讨论",
    type: "headquarters",
};

// 气泡颜色映射
const BUBBLE_COLORS: Record<string, { bg: string; border: string; avatar: string }> = {
    opening: { bg: "bg-linear-to-br from-purple-50 to-indigo-50", border: "border-purple-200", avatar: "bg-purple-500" },
    conclusion: { bg: "bg-linear-to-br from-amber-50 to-orange-50", border: "border-amber-200", avatar: "bg-amber-500" },
    discussion: { bg: "bg-white", border: "border-gray-200", avatar: "bg-blue-500" },
    // v2.0: 问题标记和系统消息样式
    question_marker: { bg: "bg-linear-to-r from-blue-50 to-indigo-50", border: "border-blue-300", avatar: "bg-blue-600" },
    system: { bg: "bg-gray-50", border: "border-gray-200", avatar: "bg-gray-500" },
};

// 根据speaker_id生成固定颜色
const getSpeakerColor = (speakerId: string | undefined): string => {
    const colors = [
        "bg-blue-500",
        "bg-green-500",
        "bg-pink-500",
        "bg-indigo-500",
        "bg-teal-500",
        "bg-orange-500",
        "bg-cyan-500",
        "bg-rose-500",
    ];
    // 保护：如果speakerId未定义，返回默认颜色
    if (!speakerId) {
        return colors[0];
    }
    let hash = 0;
    for (let i = 0; i < speakerId.length; i++) {
        hash = speakerId.charCodeAt(i) + ((hash << 5) - hash);
    }
    return colors[Math.abs(hash) % colors.length];
};

export function MeetingRoom() {
    const navigate = useNavigate();
    const { user } = useAuth();
    const { brand, backofficeBrandAllowed } = useBranding({ userId: user?.id });
    // 总部公司名：VITE 覆盖 > 白标 brand 公司名。
    // D2（Owner 2026-07-22）：hook 判定层已 fail-closed —— 未授权时 brand 即平台默认(SSOT)，
    // company_name 不会出代理品牌；backofficeBrandAllowed 仅授权时为 true（本处公司名与授权位同源，直接读 brand）。
    void backofficeBrandAllowed;
    const hqName = import.meta.env.VITE_COMPANY_NAME || `${brand?.company_name || ''}（总部）`;
    const transcriptEndRef = useRef<HTMLDivElement>(null);
    const fileInputRef = useRef<HTMLInputElement>(null);

    const [employees, setEmployees] = useState<Employee[]>([]);
    const [advisors, setAdvisors] = useState<{ id: string, name: string, avatar: string, description: string }[]>([]);
    const [brands, setBrands] = useState<Brand[]>([]);
    const [loading, setLoading] = useState(true);
    const { nameOf: industryNameOf } = useIndustryTaxonomy();
    /** [WO_267] 行业大类显示名 —— 新写入的英文 key 不许原样上屏 */
    const categoryOf = (b: Brand) => industryCategoryText(b.industry_category, b.industry_category_name, industryNameOf);

    // 项目/客户选择
    const [selectedBrandId, setSelectedBrandId] = useState<number | null>(null);
    const [isHeadquarters, setIsHeadquarters] = useState(false);

    // 会议设置
    const [topic, setTopic] = useState("");
    const [context, setContext] = useState("");
    const [selectedParticipants, setSelectedParticipants] = useState<Set<string>>(new Set());
    const [moderatorId, setModeratorId] = useState<string>("");
    const [maxRounds, setMaxRounds] = useState(2);

    // 会议高级设置
    const [meetingStyle, setMeetingStyle] = useState("");
    const [coModeratorId, setCoModeratorId] = useState<string>("");
    const [weightBoost, setWeightBoost] = useState(1.5);
    const [autoExecute, setAutoExecute] = useState(false);
    const [presets, setPresets] = useState<{ id: string; name: string; participants: string[]; topic?: string; style?: string; co_moderator_id?: string; weight_boost?: number; auto_execute?: boolean; rounds?: number }[]>([]);
    const [showAdvanced, setShowAdvanced] = useState(false);

    // 上传文件
    const [uploadedFiles, setUploadedFiles] = useState<UploadedFile[]>([]);
    const [uploading, setUploading] = useState(false);

    // 会议状态（流式）
    const [meetingInProgress, setMeetingInProgress] = useState(false);
    const [transcript, setTranscript] = useState<TranscriptItem[]>([]);
    const [speakingState, setSpeakingState] = useState<SpeakingState | null>(null);
    const [meetingInfo, setMeetingInfo] = useState<{ meeting_id?: string; topic?: string; participants?: string[]; moderator?: string } | null>(null);
    const [meetingComplete, setMeetingComplete] = useState(false);
    const [meetingDuration, setMeetingDuration] = useState<number | null>(null);
    const [meetingError, setMeetingError] = useState<string | null>(null);

    // 用户反馈
    const [feedbackInput, setFeedbackInput] = useState("");
    const [feedbackSubmitting, setFeedbackSubmitting] = useState(false);

    // 打断会议
    const abortControllerRef = useRef<AbortController | null>(null);
    const [meetingInterrupted, setMeetingInterrupted] = useState(false);

    // 任务执行阶段
    const [showTaskExecution, setShowTaskExecution] = useState(false);
    const [taskAssignmentMarkdown, setTaskAssignmentMarkdown] = useState<string | null>(null);

    // v2.0: 问题锚定状态
    const [, setDiscussionQuestions] = useState<string[]>([]);
    const [, setCurrentQuestionIndex] = useState<number | null>(null);
    const [, setCurrentQuestion] = useState<string | null>(null);

    // v3.0: 三阶段五步法状态
    const [currentPhase, setCurrentPhase] = useState<string>("");  // understand | deliberate | execute
    const [currentPhaseName, setCurrentPhaseName] = useState<string>("");
    const [, setCurrentStep] = useState<number>(0);  // 1-6
    const [currentRound, setCurrentRound] = useState<number>(0);  // 1-3
    const [awaitingConfirmation, setAwaitingConfirmation] = useState(false);  // 等待用户确认
    const [, setMeetingReport] = useState<string>("");  // 会议报告
    const [, setTaskPlan] = useState<any[]>([]);  // 任务计划

    useEffect(() => {
        fetchEmployees();
        fetchBrands();
        fetchAdvisors();
        fetchPresets();
    }, []);

    useEffect(() => {
        // 自动滚动到最新消息
        transcriptEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }, [transcript, speakingState]);

    const fetchEmployees = async () => {
        try {
            const response = await authFetch("/api/employees");
            const data = await response.json();
            if (data.success) {
                setEmployees(data.employees || []);
            }
        } catch (error) {
            console.error("Failed to fetch employees:", error);
        } finally {
            setLoading(false);
        }
    };

    const fetchPresets = async () => {
        try {
            const response = await authFetch("/api/meeting-presets");
            const data = await response.json();
            if (data.success) {
                setPresets(data.presets || []);
            }
        } catch (error) {
            console.error("Failed to fetch presets:", error);
        }
    };

    const fetchBrands = async () => {
        try {
            const response = await authFetch("/api/brands?limit=100");
            const data = await response.json();
            setBrands(data.data || data.items || []);
        } catch (error) {
            console.error("Failed to fetch brands:", error);
        }
    };

    const fetchAdvisors = async () => {
        try {
            const response = await authFetch("/api/advisors");
            const data = await response.json();
            if (data.success) {
                setAdvisors(data.advisors || []);
            }
        } catch (error) {
            console.error("Failed to fetch advisors:", error);
        }
    };

    const toggleParticipant = (empId: string) => {
        const newSelected = new Set(selectedParticipants);
        if (newSelected.has(empId)) {
            newSelected.delete(empId);
            if (moderatorId === empId) setModeratorId("");
        } else {
            newSelected.add(empId);
        }
        setSelectedParticipants(newSelected);
    };

    // 文件上传处理
    const handleFileUpload = async (event: React.ChangeEvent<HTMLInputElement>) => {
        const files = event.target.files;
        if (!files) return;

        setUploading(true);
        const newFiles: UploadedFile[] = [];

        for (const file of Array.from(files)) {
            try {
                const content = await readFileContent(file);
                newFiles.push({
                    name: file.name,
                    type: file.type || getFileType(file.name),
                    content,
                    size: file.size,
                });
            } catch (error) {
                console.error(`Failed to read file ${file.name}:`, error);
            }
        }

        setUploadedFiles([...uploadedFiles, ...newFiles]);
        setUploading(false);

        if (fileInputRef.current) {
            fileInputRef.current.value = "";
        }
    };

    const readFileContent = (file: File): Promise<string> => {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();

            if (file.type === "application/pdf") {
                resolve(`[PDF文件: ${file.name}]\n请在服务器端解析PDF内容。`);
            } else {
                reader.onload = (e) => resolve(e.target?.result as string);
                reader.onerror = reject;
                reader.readAsText(file);
            }
        });
    };

    const getFileType = (filename: string): string => {
        const ext = filename.split(".").pop()?.toLowerCase();
        const typeMap: Record<string, string> = {
            md: "text/markdown",
            txt: "text/plain",
            json: "application/json",
            csv: "text/csv",
            pdf: "application/pdf",
        };
        return typeMap[ext || ""] || "text/plain";
    };

    const removeFile = (index: number) => {
        setUploadedFiles(uploadedFiles.filter((_, i) => i !== index));
    };

    const getSelectedBrand = (): Brand | null => {
        if (isHeadquarters) {
            return { id: 0, name: hqName };
        }
        return brands.find(b => b.id === selectedBrandId) || null;
    };

    const buildMeetingContext = (): string => {
        const parts: string[] = [];

        const brand = getSelectedBrand();
        if (brand) {
            if (isHeadquarters) {
                parts.push(`【会议背景】这是一次公司内部会议，讨论的是我们公司"${brand?.company_name || ''}"自身的事务。`);
            } else {
                parts.push(`【项目背景】本次会议讨论的是客户\"${brand.name}\"的相关事务。`);
                const category = categoryOf(brand);
                if (category) {
                    parts.push(`行业分类: ${category}`);
                }
            }
            parts.push("");
        }

        if (context.trim()) {
            parts.push("【补充资料】");
            parts.push(context);
            parts.push("");
        }

        if (uploadedFiles.length > 0) {
            parts.push("【参考文件】");
            for (const file of uploadedFiles) {
                parts.push(`--- ${file.name} ---`);
                parts.push(file.content);
                parts.push("");
            }
        }

        return parts.join("\n");
    };

    // 流式会议
    const startMeetingStream = async () => {
        if (!topic.trim() || selectedParticipants.size < 2) return;

        setMeetingInProgress(true);
        setTranscript([]);
        setSpeakingState(null);
        setMeetingInfo(null);
        setMeetingComplete(false);
        setMeetingDuration(null);
        setMeetingError(null);
        setMeetingInterrupted(false);

        // 创建可中断的请求
        const controller = new AbortController();
        abortControllerRef.current = controller;

        try {
            const fullContext = buildMeetingContext();

            const response = await authFetch("/api/meetings/stream", {
                method: "POST",
                signal: controller.signal,
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    topic,
                    participant_ids: Array.from(selectedParticipants),
                    moderator_id: moderatorId || null,
                    max_rounds: maxRounds,
                    context: fullContext || null,
                    // 高级设置
                    style: meetingStyle || null,
                    co_moderator_id: coModeratorId || null,
                    weight_boost: coModeratorId ? weightBoost : null,
                }),
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const reader = response.body?.getReader();
            if (!reader) {
                throw new Error("No response body");
            }

            const decoder = new TextDecoder();
            let buffer = "";

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                buffer += decoder.decode(value, { stream: true });

                // 解析SSE事件
                const lines = buffer.split("\n");
                buffer = lines.pop() || "";

                for (const line of lines) {
                    if (line.startsWith("data: ")) {
                        try {
                            const event: StreamEvent = JSON.parse(line.slice(6));
                            handleStreamEvent(event);
                        } catch (e) {
                            console.error("Failed to parse SSE event:", e);
                        }
                    }
                }
            }

        } catch (error) {
            console.error("Meeting stream error:", error);
            setMeetingError(String(error));
        } finally {
            setMeetingInProgress(false);
            setSpeakingState(null);
        }
    };

    // 保存会议到数据库
    const saveMeetingToDb = async (
        info: { meeting_id?: string; topic?: string; participants?: string[]; moderator?: string },
        transcriptData: TranscriptItem[],
        duration: number,
    ) => {
        try {
            const brand = getSelectedBrand();
            const conclusion = transcriptData.find(t => t.type === "conclusion")?.content || "";
            const moderator = employees.find(e => e.id === moderatorId);

            await authFetch("/api/meeting-records", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    meeting_id: info.meeting_id,
                    topic: info.topic || topic,
                    participants: info.participants || [],
                    participant_ids: Array.from(selectedParticipants),
                    moderator_id: moderatorId || null,
                    moderator_name: moderator?.name || info.moderator || null,
                    brand_id: isHeadquarters ? null : selectedBrandId,
                    brand_name: brand?.name || null,
                    is_internal: isHeadquarters,
                    context: buildMeetingContext(),
                    transcript: transcriptData,
                    conclusion: conclusion,
                    status: "completed",
                    rounds: maxRounds,
                    duration: duration,
                    attachments: uploadedFiles.map(f => ({ name: f.name, size: f.size })),
                }),
            });
        } catch (error) {
            console.error("Failed to save meeting:", error);
        }
    };

    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    type EventData = Record<string, any>; // SSE 事件数据结构动态，按事件类型不同，在各 case 分支中使用
    const handleStreamEvent = (event: StreamEvent) => {
        const d = event.data as EventData;
        switch (event.type) {
            case "start":
                setMeetingInfo(d);
                break;
            case "speaking":
                setSpeakingState(d as SpeakingState);
                break;
            case "message":
                setTranscript(prev => [...prev, d as TranscriptItem]);
                setSpeakingState(null);
                // 如果是任务分配消息，保存Markdown用于后续执行
                if (d.phase === "task_assignment" && d.content) {
                    setTaskAssignmentMarkdown(d.content as string);
                }
                // v2.0: 如果是开场消息且包含问题列表，保存
                if (d.type === "opening" && d.questions) {
                    setDiscussionQuestions(d.questions as string[]);
                }
                break;
            case "confirm_start":
                // 任务确认阶段开始
                setTranscript(prev => [...prev, {
                    role: "system",
                    speaker: "系统",
                    speaker_id: "system",
                    content: "📋 " + d.message,
                    type: "system",
                    round: 0,
                }]);
                break;
            case "meeting_closed":
                // 会议正式结束
                setMeetingComplete(true);
                setMeetingInProgress(false);
                setTranscript(prev => [...prev, {
                    role: "system",
                    speaker: "系统",
                    speaker_id: "system",
                    content: "✅ " + d.message,
                    type: "system",
                    round: 0,
                }]);
                break;
            case "data_prepared":
                // v2.0 阶段三：数据预备完成
                setTranscript(prev => [...prev, {
                    role: "system",
                    speaker: "数据采集员",
                    speaker_id: "data_collector",
                    content: `📊 ${d.message}\n\n${d.summary || ''}`,
                    type: "system",
                    round: 0,
                }]);
                break;
            case "question_start":
                // v2.0: 问题切换事件
                setCurrentQuestionIndex(d.question_index as number);
                setCurrentQuestion(d.question as string);
                // 如果有问题列表，更新
                if (d.question_index === 0 && d.total_questions) {
                    // 第一个问题时，需要等待 opening message 中的 questions
                }
                // 添加问题切换提示到记录
                setTranscript(prev => [...prev, {
                    role: "system",
                    speaker: "系统",
                    speaker_id: "system",
                    content: `🎯 开始讨论问题 ${(d.question_index as number) + 1}/${d.total_questions}：${d.question}`,
                    type: "question_marker",
                    round: (d.question_index as number) + 1,
                }]);
                break;

            // ==================== v3.0 事件处理 ====================
            case "phase_change":
                // v3.0: 阶段切换
                setCurrentPhase(d.phase as string);
                setCurrentPhaseName(d.name as string);
                setTranscript(prev => [...prev, {
                    role: "system",
                    speaker: "系统",
                    speaker_id: "system",
                    content: `📌 **${d.name}**`,
                    type: "phase_marker",
                    round: 0,
                }]);
                break;
            case "round_start":
                // v3.0: 轮次开始
                setCurrentRound(d.round as number);
                setCurrentStep(d.step as number);
                setTranscript(prev => [...prev, {
                    role: "system",
                    speaker: "系统",
                    speaker_id: "system",
                    content: `🔄 第${d.round}轮：${d.name}`,
                    type: "round_marker",
                    round: d.round as number,
                }]);
                break;
            case "material_digest":
                // v3.0: 资料消化完成
                setCurrentStep(1);
                break;
            case "gap_analysis":
                // v3.0: 差距分析完成
                setCurrentStep(2);
                break;
            case "info_gap":
                // v3.0: 信息缺口识别
                // 可以在UI上高亮显示缺口
                break;
            case "report_ready":
                // v3.0: 报告生成完成
                setMeetingReport(d.report as string);
                setTaskPlan(d.task_plan as unknown[] || []);
                break;
            case "awaiting_confirmation":
                // v3.0: 等待用户确认
                setAwaitingConfirmation(true);
                setMeetingReport(d.report as string);
                setTaskPlan(d.task_plan as unknown[] || []);
                setTranscript(prev => [...prev, {
                    role: "system",
                    speaker: "系统",
                    speaker_id: "system",
                    content: `📋 ${d.message}\n\n请审核上述报告，确认无误后点击"确认执行"，或输入修改意见后点击"提交意见"。`,
                    type: "confirmation_prompt",
                    round: 0,
                }]);
                // 会议暂停在此，等待用户操作
                setMeetingInProgress(false);
                break;
            case "complete":
                setMeetingComplete(true);
                setMeetingDuration(d.duration as number);
                // 清除问题状态
                setCurrentQuestion(null);
                setCurrentQuestionIndex(null);
                // 会议完成后保存到数据库
                // 使用setTimeout确保transcript状态已更新
                setTimeout(() => {
                    setTranscript(currentTranscript => {
                        if (meetingInfo) {
                            saveMeetingToDb(meetingInfo, currentTranscript, d.duration as number);
                        }
                        return currentTranscript;
                    });
                }, 100);
                break;
            case "error":
                setMeetingError(d.error as string);
                break;
        }
    };

    const resetMeeting = () => {
        setTranscript([]);
        setMeetingInfo(null);
        setMeetingComplete(false);
        setMeetingDuration(null);
        setMeetingError(null);
        setTopic("");
        setContext("");
        setSelectedParticipants(new Set());
        setModeratorId("");
        setUploadedFiles([]);
        setFeedbackInput("");
        // v2.0: 清除问题锚定状态
        setDiscussionQuestions([]);
        setCurrentQuestionIndex(null);
        setCurrentQuestion(null);
    };

    // 提交用户反馈，继续讨论
    const handleSubmitFeedback = async () => {
        if (!feedbackInput.trim() || meetingInProgress) return;

        setFeedbackSubmitting(true);
        setMeetingComplete(false);

        // 添加用户反馈到记录
        const userFeedbackItem: TranscriptItem = {
            role: "user",
            speaker: "用户反馈",
            speaker_id: "user_feedback",
            content: feedbackInput,
            round: transcript.length > 0 ? Math.max(...transcript.map(t => t.round)) + 1 : 1,
            type: "discussion",
        };
        setTranscript(prev => [...prev, userFeedbackItem]);

        const feedback = feedbackInput;
        setFeedbackInput("");

        try {
            // 调用继续讨论API
            const response = await authFetch("/api/meetings/continue", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    topic: topic,
                    participant_ids: Array.from(selectedParticipants),
                    feedback: feedback,
                    previous_transcript: transcript,
                }),
            });

            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }

            const reader = response.body?.getReader();
            if (!reader) {
                throw new Error("No response body");
            }

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
                            console.error("Failed to parse SSE event:", e);
                        }
                    }
                }
            }
        } catch (error) {
            console.error("Feedback submission error:", error);
            setMeetingError(String(error));
        } finally {
            setFeedbackSubmitting(false);
        }
    };

    // 获取气泡样式
    const getBubbleStyle = (item: TranscriptItem) => {
        const baseColors = BUBBLE_COLORS[item.type] || BUBBLE_COLORS.discussion;
        return baseColors;
    };

    if (loading) {
        return (
            <div className="flex items-center justify-center h-64">
                <Loader2 className="h-8 w-8 animate-spin text-gray-400" />
            </div>
        );
    }

    const selectedBrand = getSelectedBrand();

    return (
        <div className="p-6 space-y-6 min-h-[calc(100dvh-3rem)] bg-linear-to-br from-slate-50 to-blue-50">
            {/* 页面标题 */}
            <div className="flex items-center justify-between">
                <div className="flex items-center gap-4">
                    <Button variant="ghost" size="sm" onClick={() => navigate("/employees")}>
                        <ArrowLeft className="h-4 w-4 mr-1" />
                        返回员工大厅
                    </Button>
                    <div>
                        <h1 className="text-2xl font-bold flex items-center gap-2">
                            <MessageSquare className="h-6 w-6 text-blue-600" />
                            AI圆桌会议
                        </h1>
                        <p className="text-gray-500 mt-1">
                            {selectedBrand ? (
                                <span className="flex items-center gap-1">
                                    {isHeadquarters ? <Home className="h-4 w-4" /> : <Building2 className="h-4 w-4" />}
                                    当前项目: <span className="font-medium text-blue-600">{selectedBrand.name}</span>
                                </span>
                            ) : (
                                "请先选择项目或回到总部"
                            )}
                        </p>
                    </div>
                </div>
            </div>

            <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
                {/* 左侧：会议设置 */}
                <div className="lg:col-span-4 space-y-4">
                    {/* 项目选择 */}
                    <Card className="border-2 border-blue-100">
                        <CardHeader className="pb-3">
                            <CardTitle className="text-base flex items-center gap-2">
                                <Building2 className="h-4 w-4" />
                                选择项目
                            </CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-3">
                            <div className="flex gap-2">
                                <Button
                                    variant={isHeadquarters ? "default" : "outline"}
                                    size="sm"
                                    onClick={() => {
                                        setIsHeadquarters(true);
                                        setSelectedBrandId(null);
                                    }}
                                    className="flex-1"
                                    disabled={meetingInProgress}
                                >
                                    <Home className="h-4 w-4 mr-1" />
                                    回到总部
                                </Button>
                                <Button
                                    variant={!isHeadquarters && selectedBrandId ? "default" : "outline"}
                                    size="sm"
                                    onClick={() => setIsHeadquarters(false)}
                                    className="flex-1"
                                    disabled={meetingInProgress}
                                >
                                    <Building2 className="h-4 w-4 mr-1" />
                                    客户项目
                                </Button>
                            </div>

                            {!isHeadquarters && (
                                // 可搜索:接口来源(/api/brands?limit=100)· 生产 321 品牌 / 单服务商最多 62
                                <SearchableSelect
                                    value={selectedBrandId?.toString() || null}
                                    onChange={(v) => setSelectedBrandId(Number(v))}
                                    disabled={meetingInProgress}
                                    placeholder="选择客户/品牌..."
                                    searchPlaceholder="搜索品牌名 / 行业"
                                    emptyText="没有匹配的客户"
                                    options={brands.map((brand) => ({
                                        value: brand.id.toString(),
                                        label: brand.name,
                                        keywords: categoryOf(brand) || undefined,
                                        render: (
                                            <>
                                                {brand.name}
                                                {categoryOf(brand) && (
                                                    <span className="text-gray-400 ml-2">
                                                        ({categoryOf(brand)})
                                                    </span>
                                                )}
                                            </>
                                        ),
                                    }))}
                                />
                            )}

                            {isHeadquarters && (
                                <div className="bg-blue-50 p-3 rounded-lg text-sm text-blue-700">
                                    <Home className="h-4 w-4 inline mr-1" />
                                    讨论的是公司内部事务
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    {/* 会议议题 */}
                    <Card>
                        <CardHeader className="pb-3">
                            <CardTitle className="text-base">会议议题</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-4">
                            <Input
                                placeholder="输入会议议题..."
                                value={topic}
                                onChange={(e) => setTopic(e.target.value)}
                                disabled={meetingInProgress}
                                className="text-base"
                            />
                            <Textarea
                                placeholder="背景资料（可选）&#10;可以粘贴相关信息、数据、要求等..."
                                value={context}
                                onChange={(e) => setContext(e.target.value)}
                                rows={6}
                                disabled={meetingInProgress}
                                className="text-sm resize-none"
                            />

                            {/* 文件上传区 */}
                            <div className="space-y-2">
                                <div className="flex items-center justify-between">
                                    <span className="text-sm text-gray-600">上传参考文件</span>
                                    <Badge variant="outline">{uploadedFiles.length} 个文件</Badge>
                                </div>
                                <input
                                    ref={fileInputRef}
                                    type="file"
                                    multiple
                                    accept=".pdf,.md,.txt,.json,.csv"
                                    onChange={handleFileUpload}
                                    className="hidden"
                                    disabled={meetingInProgress}
                                />
                                <Button
                                    variant="outline"
                                    className="w-full"
                                    onClick={() => fileInputRef.current?.click()}
                                    disabled={meetingInProgress || uploading}
                                >
                                    {uploading ? (
                                        <Loader2 className="h-4 w-4 animate-spin mr-2" />
                                    ) : (
                                        <Upload className="h-4 w-4 mr-2" />
                                    )}
                                    上传 PDF / MD / TXT / JSON
                                </Button>

                                {uploadedFiles.length > 0 && (
                                    <div className="space-y-1 max-h-32 overflow-y-auto">
                                        {uploadedFiles.map((file, index) => (
                                            <div
                                                key={index}
                                                className="flex items-center justify-between bg-gray-50 px-2 py-1 rounded text-sm"
                                            >
                                                <div className="flex items-center gap-2 min-w-0">
                                                    <FileText className="h-4 w-4 text-gray-400 shrink-0" />
                                                    <span className="truncate">{file.name}</span>
                                                    <span className="text-gray-400 text-xs">
                                                        ({(file.size / 1024).toFixed(1)}KB)
                                                    </span>
                                                </div>
                                                <Button
                                                    variant="ghost"
                                                    size="sm"
                                                    className="h-6 w-6 p-0"
                                                    onClick={() => removeFile(index)}
                                                    disabled={meetingInProgress}
                                                >
                                                    <X className="h-3 w-3" />
                                                </Button>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            <div className="flex items-center gap-2">
                                <span className="text-sm text-gray-600">讨论轮数：</span>
                                <Select
                                    value={maxRounds.toString()}
                                    onValueChange={(v) => setMaxRounds(Number(v))}
                                    disabled={meetingInProgress}
                                >
                                    <SelectTrigger className="w-20">
                                        <SelectValue />
                                    </SelectTrigger>
                                    <SelectContent>
                                        <SelectItem value="1">1轮</SelectItem>
                                        <SelectItem value="2">2轮</SelectItem>
                                        <SelectItem value="3">3轮</SelectItem>
                                    </SelectContent>
                                </Select>
                            </div>

                            {/* 高级设置开关 */}
                            <Button
                                variant="ghost"
                                size="sm"
                                className="w-full text-gray-500"
                                onClick={() => setShowAdvanced(!showAdvanced)}
                            >
                                {showAdvanced ? "收起高级设置 ▲" : "高级设置 ▼"}
                            </Button>

                            {/* 高级设置面板 */}
                            {showAdvanced && (
                                <div className="space-y-3 pt-2 border-t">
                                    {/* 会议风格 */}
                                    <div>
                                        <label className="text-xs text-gray-500 mb-1 block">会议风格要求</label>
                                        <Input
                                            value={meetingStyle}
                                            onChange={(e) => setMeetingStyle(e.target.value)}
                                            placeholder="如：务实简洁、聚焦执行、控制50字以内"
                                            className="text-sm"
                                            disabled={meetingInProgress}
                                        />
                                    </div>

                                    {/* 权重角色（副主持） */}
                                    <div>
                                        <label className="text-xs text-gray-500 mb-1 block">权重角色（发言权重更高）</label>
                                        <Select
                                            value={coModeratorId || "none"}
                                            onValueChange={(v) => setCoModeratorId(v === "none" ? "" : v)}
                                            disabled={meetingInProgress}
                                        >
                                            <SelectTrigger className="text-sm">
                                                <SelectValue placeholder="选择权重角色（可选）" />
                                            </SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="none">无</SelectItem>
                                                {/* 已选员工 */}
                                                {employees.filter(e => selectedParticipants.has(e.id)).map((emp) => (
                                                    <SelectItem key={emp.id} value={emp.id}>
                                                        {emp.name}
                                                    </SelectItem>
                                                ))}
                                                {/* 已选顾问 */}
                                                {advisors.filter(a => selectedParticipants.has(`advisor_${a.id}`)).map((adv) => (
                                                    <SelectItem key={`advisor_${adv.id}`} value={`advisor_${adv.id}`}>
                                                        {adv.name} (顾问)
                                                    </SelectItem>
                                                ))}
                                            </SelectContent>
                                        </Select>
                                    </div>

                                    {coModeratorId && (
                                        <div>
                                            <label className="text-xs text-gray-500 mb-1 block">权重倍数</label>
                                            <Select
                                                value={weightBoost.toString()}
                                                onValueChange={(v) => setWeightBoost(parseFloat(v))}
                                                disabled={meetingInProgress}
                                            >
                                                <SelectTrigger className="w-24 text-sm">
                                                    <SelectValue />
                                                </SelectTrigger>
                                                <SelectContent>
                                                    <SelectItem value="1.5">1.5x</SelectItem>
                                                    <SelectItem value="2">2x</SelectItem>
                                                </SelectContent>
                                            </Select>
                                        </div>
                                    )}

                                    {/* 自动执行 */}
                                    <div className="flex items-center gap-2">
                                        <Checkbox
                                            checked={autoExecute}
                                            onCheckedChange={(checked) => setAutoExecute(checked as boolean)}
                                            disabled={meetingInProgress}
                                        />
                                        <span className="text-sm text-gray-600">会议结束后自动执行任务</span>
                                    </div>

                                    {/* 预设 */}
                                    {presets.length > 0 && (
                                        <div>
                                            <label className="text-xs text-gray-500 mb-1 block">加载预设</label>
                                            <Select
                                                onValueChange={(presetId) => {
                                                    const preset = presets.find(p => p.id.toString() === presetId);
                                                    if (preset) {
                                                        setMeetingStyle(preset.style || "");
                                                        setCoModeratorId(preset.co_moderator_id || "");
                                                        setWeightBoost(preset.weight_boost || 1.5);
                                                        setAutoExecute(preset.auto_execute || false);
                                                        setMaxRounds(preset.rounds || 2);
                                                    }
                                                }}
                                            >
                                                <SelectTrigger className="text-sm">
                                                    <SelectValue placeholder="选择预设" />
                                                </SelectTrigger>
                                                <SelectContent>
                                                    {presets.map((p) => (
                                                        <SelectItem key={p.id} value={p.id.toString()}>
                                                            {p.name}
                                                        </SelectItem>
                                                    ))}
                                                </SelectContent>
                                            </Select>
                                        </div>
                                    )}
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    {/* 参会员工 */}
                    <Card>
                        <CardHeader className="pb-3">
                            <CardTitle className="text-base flex items-center justify-between">
                                <span className="flex items-center gap-2">
                                    <Users className="h-4 w-4" />
                                    参会员工
                                </span>
                                <Badge variant="outline">
                                    已选 {selectedParticipants.size}
                                </Badge>
                            </CardTitle>
                        </CardHeader>
                        <CardContent>
                            <div className="space-y-2 max-h-64 overflow-y-auto pr-1">
                                {employees.map((emp) => (
                                    <div
                                        key={emp.id}
                                        className={`flex items-center gap-2 p-2 rounded cursor-pointer transition-colors ${selectedParticipants.has(emp.id)
                                            ? "bg-blue-50 border border-blue-200"
                                            : "hover:bg-gray-50 border border-transparent"
                                            }`}
                                        onClick={() => !meetingInProgress && toggleParticipant(emp.id)}
                                    >
                                        <Checkbox
                                            checked={selectedParticipants.has(emp.id)}
                                            disabled={meetingInProgress}
                                        />
                                        <div className="flex-1 min-w-0">
                                            <div className="font-medium text-sm truncate">
                                                {emp.name}
                                            </div>
                                        </div>
                                        {selectedParticipants.has(emp.id) && (
                                            <Button
                                                size="sm"
                                                variant={moderatorId === emp.id ? "default" : "outline"}
                                                className="text-xs h-6 px-2"
                                                onClick={(e) => {
                                                    e.stopPropagation();
                                                    setModeratorId(moderatorId === emp.id ? "" : emp.id);
                                                }}
                                                disabled={meetingInProgress}
                                            >
                                                {moderatorId === emp.id ? "主持人" : "设为主持"}
                                            </Button>
                                        )}
                                    </div>
                                ))}
                            </div>

                            {/* 顾问团 */}
                            {advisors.length > 0 && (
                                <>
                                    <div className="border-t pt-3 mt-3">
                                        <span className="text-xs text-purple-600 font-medium">顾问团</span>
                                    </div>
                                    <div className="space-y-2">
                                        {advisors.map((advisor) => (
                                            <div
                                                key={`advisor_${advisor.id}`}
                                                className={`flex items-center gap-2 p-2 rounded cursor-pointer transition-colors ${selectedParticipants.has(`advisor_${advisor.id}`)
                                                    ? "bg-purple-50 border border-purple-200"
                                                    : "hover:bg-gray-50 border border-transparent"
                                                    }`}
                                                onClick={() => !meetingInProgress && toggleParticipant(`advisor_${advisor.id}`)}
                                            >
                                                <Checkbox
                                                    checked={selectedParticipants.has(`advisor_${advisor.id}`)}
                                                    disabled={meetingInProgress}
                                                />
                                                <span className="text-lg">{advisor.avatar}</span>
                                                <div className="flex-1 min-w-0">
                                                    <div className="font-medium text-sm truncate text-purple-700">
                                                        {advisor.name}
                                                    </div>
                                                    <div className="text-xs text-gray-400 truncate">
                                                        {advisor.description}
                                                    </div>
                                                </div>
                                                <Badge variant="outline" className="text-xs text-purple-500 border-purple-200">顾问</Badge>
                                            </div>
                                        ))}
                                    </div>
                                </>
                            )}
                        </CardContent>
                    </Card>

                    {/* 开始/打断会议按钮 */}
                    <div className="flex gap-2">
                        <Button
                            className="flex-1 h-12 text-base"
                            size="lg"
                            onClick={startMeetingStream}
                            disabled={meetingInProgress || !topic.trim() || selectedParticipants.size < 2}
                        >
                            {meetingInProgress ? (
                                <>
                                    <Loader2 className="h-5 w-5 animate-spin mr-2" />
                                    进行中...
                                </>
                            ) : (
                                <>
                                    <Play className="h-5 w-5 mr-2" />
                                    开始会议
                                </>
                            )}
                        </Button>

                        {meetingInProgress && (
                            <Button
                                variant="destructive"
                                className="h-12 px-4"
                                size="lg"
                                onClick={() => {
                                    // 中断当前会议流
                                    if (abortControllerRef.current) {
                                        abortControllerRef.current.abort();
                                    }
                                    setMeetingInterrupted(true);
                                    setMeetingInProgress(false);
                                    setSpeakingState(null);
                                }}
                            >
                                <Hand className="h-5 w-5 mr-1" />
                                打断
                            </Button>
                        )}
                    </div>

                    {selectedParticipants.size < 2 && selectedParticipants.size > 0 && (
                        <p className="text-sm text-yellow-600 text-center">
                            至少需要选择2位员工参会
                        </p>
                    )}

                    {meetingInterrupted && !meetingInProgress && (
                        <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-sm text-amber-700">
                            <Hand className="h-4 w-4 inline mr-1" />
                            会议已打断，请在右侧输入反馈意见继续讨论
                        </div>
                    )}
                </div>

                {/* 右侧：会议记录（聊天气泡样式） */}
                <div className="lg:col-span-8">
                    <Card className="h-[calc(100dvh-120px)] min-h-[600px] max-h-[1200px] flex flex-col">
                        <CardHeader className="pb-3 shrink-0 border-b">
                            <CardTitle className="text-base flex items-center justify-between">
                                <span className="flex items-center gap-2">
                                    <MessageSquare className="h-4 w-4" />
                                    会议记录
                                    {transcript.length > 0 && (
                                        <Badge variant="secondary" className="text-xs">
                                            {transcript.length}条
                                        </Badge>
                                    )}
                                </span>
                                {meetingComplete && (
                                    <div className="flex items-center gap-2">
                                        <Badge variant="outline" className="bg-green-50 text-green-700">
                                            会议完成 · {meetingDuration}s
                                        </Badge>
                                        <Button size="sm" variant="outline" onClick={resetMeeting}>
                                            新会议
                                        </Button>
                                    </div>
                                )}
                            </CardTitle>
                        </CardHeader>

                        {/* v3.0: 阶段进度指示器 */}
                        {(currentPhase || meetingInProgress) && !meetingComplete && (
                            <div className="px-4 py-3 bg-linear-to-r from-indigo-50 via-purple-50 to-pink-50 border-b border-indigo-100">
                                <div className="flex items-center justify-between mb-2">
                                    <span className="text-xs font-medium text-indigo-600">会议进度</span>
                                    <span className="text-xs text-gray-500">
                                        {currentPhaseName || "准备中..."}
                                    </span>
                                </div>
                                {/* 三阶段进度条 */}
                                <div className="flex gap-1">
                                    <div className={`h-1.5 flex-1 rounded ${currentPhase === 'understand' || currentPhase === 'deliberate' || currentPhase === 'execute' ? 'bg-indigo-500' : 'bg-gray-200'}`}>
                                    </div>
                                    <div className={`h-1.5 flex-1 rounded ${currentPhase === 'deliberate' || currentPhase === 'execute' ? 'bg-purple-500' : 'bg-gray-200'}`}>
                                    </div>
                                    <div className={`h-1.5 flex-1 rounded ${currentPhase === 'execute' ? 'bg-pink-500' : 'bg-gray-200'}`}>
                                    </div>
                                </div>
                                <div className="flex justify-between text-[10px] text-gray-400 mt-1">
                                    <span>理解</span>
                                    <span>推演</span>
                                    <span>执行</span>
                                </div>
                                {/* 当前轮次显示 */}
                                {currentRound > 0 && (
                                    <div className="mt-2 text-xs text-gray-500">
                                        🔄 第{currentRound}轮讨论中...
                                    </div>
                                )}
                            </div>
                        )}

                        {/* v3.0: 用户确认面板 */}
                        {awaitingConfirmation && (
                            <div className="px-4 py-4 bg-linear-to-r from-amber-50 to-orange-50 border-b border-amber-200">
                                <div className="flex items-center gap-2 mb-3">
                                    <span className="text-sm font-medium text-amber-700">📋 请确认会议报告</span>
                                </div>
                                <div className="flex gap-2">
                                    <Button
                                        size="sm"
                                        className="bg-green-600 hover:bg-green-700 text-white"
                                        onClick={async () => {
                                            setAwaitingConfirmation(false);
                                            setMeetingComplete(true);
                                            // TODO: 调用continue_meeting API
                                        }}
                                    >
                                        ✓ 确认执行
                                    </Button>
                                    <Input
                                        placeholder="输入修改意见..."
                                        className="flex-1 text-sm"
                                        value={feedbackInput}
                                        onChange={(e) => setFeedbackInput(e.target.value)}
                                    />
                                    <Button
                                        size="sm"
                                        variant="outline"
                                        onClick={async () => {
                                            if (!feedbackInput.trim()) return;
                                            // TODO: 调用continue_meeting API with feedback
                                            setFeedbackInput("");
                                        }}
                                    >
                                        提交意见
                                    </Button>
                                </div>
                            </div>
                        )}

                        <CardContent className="flex-1 overflow-hidden p-4">
                            <div className="h-full overflow-y-auto pr-2 space-y-4 scrollbar-thin scrollbar-thumb-gray-300 scrollbar-track-gray-100">
                                {/* 空状态 */}
                                {!meetingInfo && !meetingInProgress && transcript.length === 0 && (
                                    <div className="flex flex-col items-center justify-center h-full text-gray-400">
                                        <MessageSquare className="h-16 w-16 mb-4 opacity-30" />
                                        <p className="text-lg">设置会议议题和参会人员后，点击"开始会议"</p>
                                        <p className="text-sm mt-2">实时显示每位员工的发言</p>
                                    </div>
                                )}

                                {/* 会议开始信息 */}
                                {meetingInfo && (
                                    <div className="bg-linear-to-r from-blue-50 to-indigo-50 p-4 rounded-xl border border-blue-100 mb-4">
                                        <div className="font-semibold text-lg text-gray-800">{meetingInfo.topic}</div>
                                        <div className="text-sm text-gray-600 mt-1">
                                            参会：{meetingInfo.participants?.join("、")}
                                        </div>
                                        {selectedBrand && (
                                            <div className="text-sm text-gray-500 mt-1">
                                                项目：{selectedBrand.name}
                                            </div>
                                        )}
                                    </div>
                                )}

                                {/* 聊天气泡 */}
                                {transcript.map((item, index) => {
                                    const style = getBubbleStyle(item);
                                    const avatarColor = item.type === "opening" || item.type === "conclusion"
                                        ? style.avatar
                                        : getSpeakerColor(item.speaker_id);

                                    return (
                                        <div key={index} className="flex gap-3 animate-in fade-in slide-in-from-bottom-2 duration-300">
                                            {/* 头像 */}
                                            <div className={`w-10 h-10 rounded-full shrink-0 flex items-center justify-center text-white font-bold text-sm ${avatarColor}`}>
                                                {item.speaker.slice(0, 2)}
                                            </div>

                                            {/* 气泡内容 */}
                                            <div className="flex-1 min-w-0">
                                                <div className="flex items-center gap-2 mb-1">
                                                    <span className="font-semibold text-gray-800">{item.speaker}</span>
                                                    {item.type === "opening" && (
                                                        <Badge className="bg-purple-100 text-purple-700 text-xs">开场</Badge>
                                                    )}
                                                    {item.type === "conclusion" && (
                                                        <Badge className="bg-amber-100 text-amber-700 text-xs">总结</Badge>
                                                    )}
                                                    {item.type === "discussion" && (
                                                        <Badge variant="outline" className="text-xs">
                                                            第{item.round}轮
                                                        </Badge>
                                                    )}
                                                </div>

                                                <div className={`rounded-2xl rounded-tl-sm p-4 ${style.bg} ${style.border} border shadow-xs`}>
                                                    <div className="prose prose-sm dark:prose-invert max-w-none
                                                        prose-headings:text-foreground prose-headings:font-semibold prose-headings:mt-2 prose-headings:mb-1
                                                        prose-p:leading-relaxed prose-p:my-1
                                                        prose-li:my-0.5
                                                        prose-code:bg-secondary prose-code:px-1 prose-code:rounded
                                                        prose-ul:my-1 prose-ol:my-1
                                                    ">
                                                        <ReactMarkdown>
                                                            {item.content}
                                                        </ReactMarkdown>
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    );
                                })}

                                {/* 正在发言提示 */}
                                {speakingState && (
                                    <div className="flex gap-3 animate-in fade-in duration-300">
                                        <div className={`w-10 h-10 rounded-full shrink-0 flex items-center justify-center text-white font-bold text-sm ${getSpeakerColor(speakingState.speaker_id)}`}>
                                            {speakingState.speaker.slice(0, 2)}
                                        </div>
                                        <div className="flex-1">
                                            <div className="flex items-center gap-2 mb-1">
                                                <span className="font-semibold text-gray-800">{speakingState.speaker}</span>
                                                <Badge className="bg-blue-100 text-blue-700 text-xs animate-pulse">
                                                    正在发言...
                                                </Badge>
                                            </div>
                                            <div className="rounded-2xl rounded-tl-sm p-4 bg-gray-50 border border-gray-200">
                                                <div className="flex items-center gap-2 text-gray-400">
                                                    <Loader2 className="h-4 w-4 animate-spin" />
                                                    <span className="text-sm">思考中...</span>
                                                </div>
                                            </div>
                                        </div>
                                    </div>
                                )}

                                {/* 错误提示 */}
                                {meetingError && (
                                    <div className="bg-red-50 border border-red-200 rounded-xl p-4 text-red-700">
                                        <p className="font-medium">会议出错</p>
                                        <p className="text-sm mt-1">{meetingError}</p>
                                    </div>
                                )}

                                <div ref={transcriptEndRef} />
                            </div>
                        </CardContent>

                        {/* 用户反馈输入（放在Card内部底部，像聊天输入框） */}
                        {(meetingComplete || transcript.length > 0) && (
                            <div className="p-3 border-t bg-gray-50/50">
                                <div className="flex gap-2">
                                    <Input
                                        value={feedbackInput}
                                        onChange={(e) => setFeedbackInput(e.target.value)}
                                        placeholder="输入反馈意见，让他们继续讨论..."
                                        disabled={meetingInProgress || feedbackSubmitting}
                                        onKeyDown={(e) => {
                                            if (e.key === 'Enter' && !e.shiftKey && feedbackInput.trim()) {
                                                // 触发继续讨论
                                                handleSubmitFeedback();
                                            }
                                        }}
                                    />
                                    <Button
                                        onClick={handleSubmitFeedback}
                                        disabled={!feedbackInput.trim() || meetingInProgress || feedbackSubmitting}
                                        className="px-4"
                                        variant="outline"
                                    >
                                        {feedbackSubmitting ? (
                                            <Loader2 className="h-4 w-4 animate-spin" />
                                        ) : (
                                            <>
                                                <MessageSquare className="h-4 w-4 mr-1" />
                                                追加讨论
                                            </>
                                        )}
                                    </Button>
                                    <Button
                                        onClick={() => {
                                            setFeedbackInput("同意，请分配任务");
                                            setTimeout(() => handleSubmitFeedback(), 100);
                                        }}
                                        disabled={meetingInProgress || feedbackSubmitting}
                                        className="px-4 bg-green-600 hover:bg-green-700"
                                    >
                                        {feedbackSubmitting ? (
                                            <Loader2 className="h-4 w-4 animate-spin" />
                                        ) : (
                                            <>
                                                ✓ 确认执行
                                            </>
                                        )}
                                    </Button>
                                    {/* 任务分配完成后显示开始执行按钮 */}
                                    {taskAssignmentMarkdown && !showTaskExecution && (
                                        <Button
                                            onClick={() => setShowTaskExecution(true)}
                                            className="px-4 bg-blue-600 hover:bg-blue-700"
                                        >
                                            <ClipboardList className="h-4 w-4 mr-1" />
                                            开始执行任务
                                        </Button>
                                    )}
                                </div>
                                <p className="text-xs text-gray-400 mt-1">
                                    输入反馈继续讨论，或点击"确认执行"结束会议并分配任务
                                </p>
                            </div>
                        )}

                        {/* 任务执行看板 */}
                        {showTaskExecution && taskAssignmentMarkdown && (
                            <div className="p-4">
                                <TaskExecutionBoard
                                    tasksMarkdown={taskAssignmentMarkdown}
                                    context={buildMeetingContext()}
                                    participantIds={Array.from(selectedParticipants)}
                                    onComplete={async (results) => {
                                        // 保存到工作台
                                        try {
                                            const content = Object.entries(results)
                                                .map(([taskId, result]) => `## ${taskId}\n${result}`)
                                                .join('\n\n---\n\n');

                                            await authFetch('/api/workspace/results', {
                                                method: 'POST',
                                                headers: { 'Content-Type': 'application/json' },
                                                body: JSON.stringify({
                                                    title: `会议执行结果: ${topic.slice(0, 30)}`,
                                                    result_type: '会议纪要',
                                                    brand_name: selectedBrandId ? brands.find(b => b.id === selectedBrandId)?.name : null,
                                                    brand_id: selectedBrandId,
                                                    meeting_id: meetingInfo?.meeting_id,
                                                    content: content,
                                                    status: '已完成',
                                                    keywords: topic,
                                                }),
                                            });
                                        } catch (error) {
                                            console.error("保存到工作台失败:", error);
                                        }
                                    }}
                                    autoStart={true}
                                />
                            </div>
                        )}
                    </Card>
                </div >
            </div >
        </div >
    );
}
