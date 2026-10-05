import { useEffect, useState, useRef } from "react";
import { useParams, useNavigate } from "react-router-dom";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Progress } from "@/components/ui/progress";
import { Button } from "@/components/ui/button";
import { CheckCircle2, Loader2, AlertCircle, ArrowLeft, Clock, Activity, Share2, Calculator } from "lucide-react";
import { toast } from "sonner";
import { useClientContext } from "@/context/ClientContext";
import { emitAgentEvent } from "@/lib/agentEvents";
import { authFetch } from "@/lib/api";
import { isSandboxActive } from "@/sandbox/sandboxState";
import { SANDBOX_DIAGNOSIS_ID } from "@/sandbox/mockData";
import { copyAsyncText } from "@/lib/copyUtils";
import { ManualCopyDialog } from "@/components/common/ManualCopyDialog";
import { awaitConfirmedSessionToken } from "@/lib/authoritativeSession";

interface ProgressData {
    stage: string;
    progress: number;
    message: string;
    done?: boolean;
    error?: string;
    diagnosis_scope?: string;
    // [CTO-15.9 build 修] 完成时由后端附带 score / level
    score?: number;
    level?: string;
}

// GEO 诊断步骤（6步，约5分钟）
const GEO_STAGES = [
    { id: "business_analysis", label: "业务理解", description: "深度分析客户业务场景与竞争环境", progress: 15 },
    { id: "parallel_collect", label: "数据采集", description: "网页搜索 + 竞品分析（并行）", progress: 50 },
    { id: "ai_test", label: "AI可见度测试", description: "4 个 AI 平台同步检测品牌可见度", progress: 75 },
    { id: "scoring", label: "GEO评分", description: "5维度AI搜索可见度评分", progress: 80 },
    { id: "report", label: "生成报告", description: "AI生成GEO诊断报告", progress: 95 },
    { id: "saving", label: "保存结果", description: "保存诊断记录", progress: 99 },
];

// 日志条目
interface LogEntry {
    time: string;
    message: string;
    stage: string;
}


export function DiagnosisProgress() {
    const { id } = useParams<{ id: string }>();
    const navigate = useNavigate();
    const [progress, setProgress] = useState<ProgressData>({ stage: "collecting", progress: 0, message: "准备开始..." });
    // [§6b.3] 显式 3 态连接机:live=实时 WS / reconnecting=断连重连中 / polling=纯轮询兜底
    const [connectionState, setConnectionState] = useState<'live' | 'polling' | 'reconnecting'>('reconnecting');
    const [error, setError] = useState<string | null>(null);
    // [settlement-manual-ux 2026-08-25] 结算转人工是**确定终态**,但既不是成交也不是故障:
    //   钱冻着、等人核实,重试无意义且可能双花。所以它不走 error 那一档
    //   (error 档是红色故障横幅 + "重新诊断"按钮),单独一个状态、单独渲染。
    const [settlementManual, setSettlementManual] = useState<string | null>(null);
    const [diagnosisId, setDiagnosisId] = useState<number | null>(null);
    const [brandId, setBrandId] = useState<number | null>(null);
    const [completed, setCompleted] = useState(false);
    // [WO_WHITELABEL_COPY_UX 项2] 剪贴板被拒但链接已拿到 → 弹可选中链接框
    const [manualCopyText, setManualCopyText] = useState<string | null>(null);
    const wsRef = useRef<WebSocket | null>(null);
    const { refreshClients } = useClientContext();

    // [看板增强] 实时日志 + 计时
    const [logs, setLogs] = useState<LogEntry[]>([]);
    const [startTime] = useState<number>(Date.now());
    const [elapsed, setElapsed] = useState<number>(0);
    const logEndRef = useRef<HTMLDivElement>(null);

    const currentStages = GEO_STAGES;

    // 计时器
    useEffect(() => {
        const timer = setInterval(() => {
            setElapsed(Math.floor((Date.now() - startTime) / 1000));
        }, 1000);
        return () => clearInterval(timer);
    }, [startTime]);

    // 自动滚动日志到底部
    useEffect(() => {
        logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [logs]);

    // 用 ref 追踪 done 状态，避免 onclose 闭包中读取 stale progress
    const isDoneRef = useRef(false);
    const [initialCheckDone, setInitialCheckDone] = useState(false);

    // ===== 沙盒态: 5 秒客户端动画 + 自动跳报告 (Stage 1 Batch 4 · 2026-05-18) =====
    // 沙盒拦不到 ws://, 连真后端 WS 又不会推消息 → 卡死在初始进度
    // 这里走纯客户端 5s 倒计时 · 阶段化推 0/15/50/75/95/100 · 跳过 WS/轮询
    useEffect(() => {
        if (!id || !isSandboxActive()) return;
        const DURATION = 5000;
        const STAGES = [
            { at: 0,    stage: 'business_analysis', progress: 0,   message: '诊断启动 (教程模式)' },
            { at: 500,  stage: 'business_analysis', progress: 15,  message: '业务理解中...' },
            { at: 1500, stage: 'parallel_collect',  progress: 50,  message: '数据采集中 (网页搜索 + 竞品分析)...' },
            { at: 2800, stage: 'ai_test',           progress: 75,  message: 'AI 可见度测试中...' },
            { at: 3600, stage: 'scoring',           progress: 80,  message: 'GEO 评分中...' },
            { at: 4200, stage: 'report',            progress: 95,  message: '生成诊断报告...' },
            { at: 4700, stage: 'saving',            progress: 99,  message: '保存诊断记录...' },
        ];
        setConnectionState('live');
        const timers: number[] = [];
        STAGES.forEach((s) => {
            timers.push(window.setTimeout(() => {
                setProgress({ stage: s.stage, progress: s.progress, message: s.message });
                const now = new Date();
                const timeStr = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}`;
                setLogs(prev => {
                    const last = prev[prev.length - 1];
                    if (last && last.message === s.message) return prev;
                    return [...prev, { time: timeStr, message: s.message, stage: s.stage }];
                });
            }, s.at));
        });
        timers.push(window.setTimeout(() => {
            isDoneRef.current = true;
            setDiagnosisId(SANDBOX_DIAGNOSIS_ID);
            setBrandId(null);
            setProgress({ stage: 'done', progress: 100, message: '诊断完成 (教程模式)', done: true });
            setCompleted(true);
            toast.success('诊断完成 · 跳转报告页');
            refreshClients();
            window.dispatchEvent(new CustomEvent('diagnosis:completed', {
                detail: { diagnosis_id: SANDBOX_DIAGNOSIS_ID }
            }));
            window.setTimeout(() => navigate(`/diagnosis/report/${SANDBOX_DIAGNOSIS_ID}`), 1500);
        }, DURATION));
        setInitialCheckDone(true); // 阻止下面真 WS 那个 effect 启动
        return () => { timers.forEach(t => window.clearTimeout(t)); };
    }, [id, navigate, refreshClients]);

    // 页面加载时先查一次：如果诊断已完成，直接显示完成状态，不等 WebSocket
    // [CTO-15.23 2026-05-11 P0-2] WS disconnect 后 UI 卡死根治:mount 时若已 done · 也自动 navigate
    //   原 bug:mount 时拿到 done=true 只 setCompleted · 没 navigate · 用户停在进度页找不到报告
    //   修法:跟 polling done 行为一致 · 2s 后自动跳报告页
    //   后端 P0-2 修了同时返 brand_id/score/level 直接用 · 不再二次 fetch /api/diagnosis/<id>
    useEffect(() => {
        if (!id || isSandboxActive()) return; // 沙盒走上面的客户端动画, 跳过真接口检查
        authFetch(`/api/diagnosis/session/${id}/status`)
            .then(r => r.json())
            .then(data => {
                if (data.found && data.done && data.diagnosis_id) {
                    isDoneRef.current = true;
                    setDiagnosisId(data.diagnosis_id);
                    setCompleted(true);
                    toast.success("诊断完成 · 跳转报告页");
                    setProgress({ stage: "done", progress: 100, message: "诊断已完成", done: true });
                    // [P0-2] 后端 status 已带 brand_id · 直接用避免二次 fetch
                    if (data.brand_id) {
                        setBrandId(data.brand_id);
                    } else {
                        authFetch(`/api/diagnosis/${data.diagnosis_id}`)
                            .then(r => r.json())
                            .then(d => { if (d.brand_id) setBrandId(d.brand_id); })
                            .catch(() => {});
                    }
                    // [P0-2 新增] 跟 polling done 行为一致 · 2s 后自动跳报告页
                    setTimeout(() => navigate(`/diagnosis/report/${data.diagnosis_id}`), 2000);
                }
            })
            .catch(() => {})
            .finally(() => setInitialCheckDone(true));
    }, [id]);

    useEffect(() => {
        // 等初始检查完成后再连 WebSocket（如果已完成就不连了）
        // 沙盒态走纯客户端 5s 动画, 不连真 WS (沙盒拦不到 ws:// + 真后端不认 sandbox session)
        if (!id || !initialCheckDone || isDoneRef.current || isSandboxActive()) return;

        let reconnectAttempts = 0;
        const maxReconnects = 60; // 覆盖~15分钟的诊断时长
        let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
        let pollingTimer: ReturnType<typeof setInterval> | null = null;
        let isCancelled = false;

        // ── [门八第三发现] 「还没开跑」不是「不存在」──────────────────────
        // 发起成功之后到执行器真正领取之间有一段窗口(防御体检走 cron,生产 20s
        // 一轮;WS 刚连上时也会先探一次)。这段窗口里 /status 回 found:false,
        // 而原来的代码**第一次**拿到 false 就挂红色横幅并 clearInterval ——
        // 之后进度真的来了也回不去,横幅一直粘到终态。
        // 她刚付过钱,横幅却写着「请返回重新发起」;真去重新发起就是**再冻一笔**。
        //
        // 改成有限退避:宽限期内静默重探,超过才认为真的不存在
        // (所以"链接抄错一位"这种真·不存在仍然有横幅,只是晚 90 秒)。
        const NOT_FOUND_GRACE_MS = 90_000;
        const NOT_FOUND_RETRY_MS = 3000;
        let firstNotFoundAt = 0;
        let notFoundRetry: ReturnType<typeof setTimeout> | null = null;

        // 查一次当前诊断状态（WebSocket 重连 + 轮询共用）
        const fetchCurrentStatus = async () => {
            if (isCancelled || isDoneRef.current) return;
            try {
                const resp = await authFetch(`/api/diagnosis/session/${id}/status`);
                const data = await resp.json();
                if (data.found) {
                    // [门八第三发现] 任务确实在 —— 退避计时归零,并把"未找到"横幅撤掉。
                    // (下面的终态分支如果要报错,会在同一趟里重新 setError,不受影响。)
                    firstNotFoundAt = 0;
                    if (notFoundRetry) { clearTimeout(notFoundRetry); notFoundRetry = null; }
                    setError(null);
                    setProgress({
                        stage: data.stage || "collecting",
                        progress: data.progress || 0,
                        message: data.message || "",
                        done: data.done,
                        error: data.error,
                    });
                    if (data.message) {
                        const now = new Date();
                        const timeStr = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}`;
                        setLogs(prev => {
                            const last = prev[prev.length - 1];
                            if (last && last.message === data.message) return prev;
                            return [...prev, { time: timeStr, message: data.message, stage: data.stage || "" }];
                        });
                    }
                    // [HC3 终态权威] terminal:true 无条件当终态处理 · done 保留兼容 · 独立于任何 seq
                    const isTerminal = data.terminal === true || data.done === true;
                    if (isTerminal && data.diagnosis_id) {
                        isDoneRef.current = true;
                        setDiagnosisId(data.diagnosis_id);
                        setCompleted(true);
                        toast.success("诊断完成 · 跳转报告页");
                        setError(null);
                        refreshClients();
                        // 获取 brand_id 用于"制定方案"跳转
                        authFetch(`/api/diagnosis/${data.diagnosis_id}`)
                            .then(r => r.json())
                            .then(d => { if (d.brand_id) setBrandId(d.brand_id); })
                            .catch(() => {});
                        window.dispatchEvent(new CustomEvent('diagnosis:completed', {
                            detail: { diagnosis_id: data.diagnosis_id }
                        }));
                        if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
                        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                        // 🆕 BUG-P1-7 (CTO-15.22 2026-05-03): 2s 后自动跳到报告页
                        // 防止"GEO诊断完成"日志显示但用户停留在进度页找不到报告
                        setTimeout(() => navigate(`/diagnosis/report/${data.diagnosis_id}`), 2000);
                    } else if (data.needs_manual_review === true) {
                        // [settlement-manual-ux · 轮询路径] 与 WS 路径同款,必须排在 error 之前。
                        isDoneRef.current = true;
                        setError(null);
                        setSettlementManual(data.message || "结算转人工核实中,费用已冻结、不会多扣。");
                        if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
                        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                    } else if (data.error || (data.terminal === true && !data.diagnosis_id)) {
                        // [HC3] 终态失败(轮询路径)· 修原 gap:显示错误 + 停轮询/重连 · 页面不再空转
                        isDoneRef.current = true;
                        if (data.error) setError(data.error);
                        if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
                        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                    }
                } else if (!data.found) {
                    // [门八第三发现] 见上方 NOT_FOUND_GRACE_MS 那段注释。
                    // 第一次 false 只开始计时并安排重探,**不挂横幅、不停轮询**。
                    if (!firstNotFoundAt) firstNotFoundAt = Date.now();
                    const waited = Date.now() - firstNotFoundAt;
                    setProgress(prev => {
                        if (prev.progress > 0) {
                            // 已经有进度 = 任务确实在跑,这一发是误报,忽略
                            return prev;
                        }
                        if (waited >= NOT_FOUND_GRACE_MS) {
                            setError("未找到此诊断任务。可能已过期或无权查看，请返回重新发起。");
                            if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
                        } else if (!notFoundRetry) {
                            // 宽限期内继续探。这一条不能只靠轮询定时器:WS 连上时
                            // 轮询根本没起,而那一发探测正好落在窗口里。
                            notFoundRetry = setTimeout(() => {
                                notFoundRetry = null;
                                void fetchCurrentStatus();
                            }, NOT_FOUND_RETRY_MS);
                        }
                        return prev;
                    });
                }
            } catch (e) {
                console.error("[状态查询] 请求失败:", e);
            }
        };

        // HTTP 轮询降级：当 WebSocket 全部重连失败后，每5秒查一次后端状态
        const startPolling = () => {
            if (pollingTimer || isCancelled) return;
            console.log("[降级] 启动 HTTP 轮询...");
            // [§6b.3] 传输态切轮询不再污染红色错误横幅 · 改由 connectionState 芯片提示
            setConnectionState('polling');

            pollingTimer = setInterval(fetchCurrentStatus, 5000);
        };

        const connectWs = async () => {
            if (isCancelled || isDoneRef.current) return;

            const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
            // [BUG-3] 会话在途先等确认再连 WS。原来直接抛错会把整个诊断进度页炸白;
            // 确认失败(超时/网络)时 awaitConfirmedSessionToken 抛可恢复错,
            // 由下面的 catch 落到轮询兜底 —— 页面照常可用。
            let token = '';
            try {
                token = (await awaitConfirmedSessionToken()) || '';
            } catch {
                startPolling();
                return;
            }
            // 等待期间组件可能已卸载/诊断已完成
            if (isCancelled || isDoneRef.current) return;
            const ws = new WebSocket(
                `${protocol}//${window.location.host}/ws/progress/${id}`,
                ['omnirank-auth', token],
            );
            wsRef.current = ws;

            ws.onopen = () => {
                setConnectionState('live');
                setError(null);
                reconnectAttempts = 0;
                // 如果之前在轮询，WebSocket恢复后停止轮询
                if (pollingTimer) {
                    clearInterval(pollingTimer);
                    pollingTimer = null;
                }
                // 重连后主动查一次当前状态（补上断连期间错过的进度）
                fetchCurrentStatus();
            };

            ws.onmessage = (event) => {
                try {
                    const data = JSON.parse(event.data);

                    setProgress({
                        stage: data.stage || data.stage_id || "collecting",
                        progress: data.progress || 0,
                        message: data.message || "",
                        done: data.done,
                        error: data.error,
                    });

                    if (data.message) {
                        const now = new Date();
                        const timeStr = `${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}:${now.getSeconds().toString().padStart(2, '0')}`;
                        setLogs(prev => {
                            const last = prev[prev.length - 1];
                            if (last && last.message === data.message) return prev;
                            return [...prev, { time: timeStr, message: data.message, stage: data.stage || "" }];
                        });
                    }

                    // [HC3 终态权威] terminal:true 无条件当终态处理 · done 保留兼容 · 独立于任何 seq
                    const isTerminal = data.terminal === true || data.done === true;
                    if (isTerminal && data.diagnosis_id) {
                        // 终态成功 → 跳报告(保留原导航)
                        isDoneRef.current = true;
                        setDiagnosisId(data.diagnosis_id);
                        setCompleted(true);
                        toast.success("诊断完成 · 跳转报告页");
                        setError(null);
                        refreshClients();
                        authFetch(`/api/diagnosis/${data.diagnosis_id}`)
                            .then(r => r.json())
                            .then(d => { if (d.brand_id) setBrandId(d.brand_id); })
                            .catch(() => {});
                        window.dispatchEvent(new CustomEvent('diagnosis:completed', {
                            detail: { diagnosis_id: data.diagnosis_id }
                        }));
                        if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
                        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                        // 🆕 BUG-P1-7 (CTO-15.22 2026-05-03): WS 推送 done 后 2s 自动跳报告
                        setTimeout(() => navigate(`/diagnosis/report/${data.diagnosis_id}`), 2000);
                    } else if (data.needs_manual_review === true) {
                        // [settlement-manual-ux · 必须排在 error 那一档**之前**]
                        //   品牌识别复核档同时带 error 键(后端本单一个字没改),
                        //   排在后面的话它还是会掉进红色故障横幅 + "重新诊断"按钮。
                        isDoneRef.current = true;
                        setError(null);
                        setSettlementManual(data.message || "结算转人工核实中,费用已冻结、不会多扣。");
                        if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
                        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                    } else if (data.error || (data.terminal === true && !data.diagnosis_id)) {
                        // [HC3] 终态失败 → 显示错误 + 停轮询/重连(修原 gap:原来只 setError 不停止 · 页面空转)
                        isDoneRef.current = true;
                        if (data.error) setError(data.error);
                        if (pollingTimer) { clearInterval(pollingTimer); pollingTimer = null; }
                        if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null; }
                    } else if (!isDoneRef.current) {
                        // 收到正常进度消息 = 任务确实存在 → 撤掉临时横幅。
                        // 🔴 [门八第三发现] 原来这里写的是 `else if (error)`,而 `error`
                        //    是**这个 effect 闭包里的陈旧值**(deps 是
                        //    [id, navigate, initialCheckDone],不含 error)—— effect 建立时
                        //    error 恒为 null,所以这一支**从来没执行过**:
                        //    WS 推 50%、日志都来了,红色横幅照样粘着直到终态。
                        //    改成按 isDoneRef 判:终态错误(上面几支已置 isDoneRef=true)
                        //    仍然留着,只清非终态的临时横幅。
                        //    值相同(null)时 React 会 bail out,不会多一次渲染。
                        setError(null);
                    }
                } catch (e) {
                    console.error("Failed to parse WS message:", e);
                }
            };

            ws.onerror = (event) => {
                console.error("WebSocket error:", event);
            };

            ws.onclose = () => {
                if (isCancelled || isDoneRef.current) return;

                reconnectAttempts++;

                // 第一次断连就启动轮询兜底（不等重连成功）
                if (!pollingTimer) {
                    console.log("[降级] WS断连，立即启动HTTP轮询兜底");
                    pollingTimer = setInterval(fetchCurrentStatus, 5000);
                    // 立刻查一次
                    fetchCurrentStatus();
                }

                // 同时继续尝试 WS 重连（最多10次，成功后停轮询）
                if (reconnectAttempts <= 10) {
                    // [§6b.3] 重连尝试期间 → 芯片显"重连中…"(黄)
                    setConnectionState('reconnecting');
                    const delay = Math.min(2000 * reconnectAttempts, 10000);
                    reconnectTimer = setTimeout(() => { void connectWs(); }, delay);
                } else {
                    // [§6b.3] 重连次数耗尽 → 纯轮询兜底(中性)
                    setConnectionState('polling');
                }
            };
        };

        const stopForAuthorityChange = () => {
            // Close the previous account's live transport synchronously. ProtectedRoute
            // will mount a fresh effect only after the replacement token passes /me.
            isCancelled = true;
            if (reconnectTimer) clearTimeout(reconnectTimer);
            if (pollingTimer) clearInterval(pollingTimer);
            wsRef.current?.close();
        };
        window.addEventListener('omnirank-authorization-changed', stopForAuthorityChange, { once: true });
        void connectWs();

        return () => {
            isCancelled = true;
            window.removeEventListener('omnirank-authorization-changed', stopForAuthorityChange);
            if (reconnectTimer) clearTimeout(reconnectTimer);
            if (pollingTimer) clearInterval(pollingTimer);
            if (notFoundRetry) clearTimeout(notFoundRetry);
            if (wsRef.current) {
                wsRef.current.close();
            }
        };
    }, [id, navigate, initialCheckDone]);

    // 匹配当前步骤：如果后端发的stage不在列表中，用progress值推断最接近的步骤
    let currentStageIndex = currentStages.findIndex(s => s.id === progress.stage);
    if (currentStageIndex === -1 && !progress.done && progress.progress > 0) {
        // 未知stage时，根据progress百分比推断应该到哪个步骤了
        for (let i = currentStages.length - 1; i >= 0; i--) {
            if (progress.progress >= currentStages[i].progress) {
                currentStageIndex = i;
                break;
            }
        }
    }
    // 伪进度平滑：后端进度跳跃时，前端每秒增加 0.5%，直到追上真实进度
    const [smoothProgress, setSmoothProgress] = useState(0);
    const realProgress = progress.done ? 100 : progress.progress || 0;
    // 诊断完成时通知 AI 助手
    const doneEmitted = useRef(false);
    useEffect(() => {
        if (progress.done && !doneEmitted.current) {
            doneEmitted.current = true;
            emitAgentEvent('diagnosis_completed', {
                session_id: id,
                score: progress.score,
                level: progress.level,
            });
            /* 2026-05-22 BUG 3 修:诊断完成清 localStorage 活跃 session 标记
             * 防 BrandDetail DiagnosisTab 仍显"生成中"占位行 */
            try {
                const raw = localStorage.getItem('active_diagnosis_session');
                if (raw) {
                    const data = JSON.parse(raw);
                    if (data?.sessionId === id) localStorage.removeItem('active_diagnosis_session');
                }
            } catch { /* 静默 */ }
        }
    }, [progress.done]); // eslint-disable-line react-hooks/exhaustive-deps
    useEffect(() => {
        if (realProgress >= 100) { setSmoothProgress(100); return; }
        if (realProgress > smoothProgress) { setSmoothProgress(realProgress); return; }
        // 如果真实进度没变，每秒+0.5%（不超过真实值+8%，防止超前太多）
        const timer = setInterval(() => {
            setSmoothProgress(prev => {
                const ceiling = Math.min(realProgress + 8, 99);
                return prev < ceiling ? prev + 0.5 : prev;
            });
        }, 1000);
        return () => clearInterval(timer);
    }, [realProgress]);
    const overallProgress = Math.round(smoothProgress);

    // 预估剩余时间(2026-06-05 根治 · 老板报"刚开始 50% 就显即将完成")
    // 旧根因:lockedTotal = elapsed/progress*100 线性外推总时。GEO 进度对时间非线性——
    //   前期「业务理解」几秒冲到 30-50%,真正耗时在后期「数据采集 + LLM」。早期外推把总时
    //   算成十几秒 → elapsed 一过 remaining=0 → 50% 就误显「即将完成」;进度卡住时校准
    //   effect 不再触发,错误的小 total 一直不被纠正。
    // 根治:改「进度驱动」稳定估算 —— 剩余 = 典型总时 ×(1 - 进度)。不用 elapsed 外推:
    //   既不早期归零误报,也不会重蹈 2026-05-09「倒计时跟已用时同步走」老 bug(elapsed 不参与)。
    //   进度涨→倒计时减;进度卡→估值稳;进度 ≥93%(剩余 ≤20s)→ 才说「即将完成」。
    const DEFAULT_ESTIMATE_SECONDS = 300; // GEO 典型 ~5min(6 步)
    const estimateRemaining = () => {
        if (overallProgress >= 100) return null;
        // 已用时远超典型总时 2 倍仍没完成 → 估值不可靠 · 不报假倒计时
        if (elapsed > DEFAULT_ESTIMATE_SECONDS * 2) return null;
        const remaining = Math.round(DEFAULT_ESTIMATE_SECONDS * (1 - overallProgress / 100));
        if (remaining <= 20) return '即将完成'; // 最后 ~20s(进度 ≥93%)才说即将完成
        const min = Math.floor(remaining / 60);
        const sec = remaining % 60;
        return min > 0 ? `约 ${min}分${sec}秒` : `约 ${sec}秒`;
    };

    const formatElapsed = (s: number) => {
        const min = Math.floor(s / 60);
        const sec = s % 60;
        return `${min}:${sec.toString().padStart(2, '0')}`;
    };

    // 空值保护：无 session ID 时跳转首页
    if (!id) {
        return (
            <div className="flex flex-col items-center justify-center h-64 text-muted-foreground text-sm gap-3">
                <p>缺少诊断会话 ID</p>
                <Button size="sm" onClick={() => navigate('/')}>返回首页</Button>
            </div>
        );
    }

    return (
        <div className="p-4 md:p-6 space-y-4 md:space-y-6">
            {/* 2026-05-22 P2 fix(老板本地浏览器实测):mobile 首屏挤
             * 原 header 单行 flex 4 列(back/icon/title/timer)在 390 屏宽超挤
             * "GEO诊断中" 撞计时器 + ETA 字号小看不清
             * 修法 v2:header 砍计时器列 · 标题缩 text-xl(mobile)/text-2xl(md+)
             *   ETA 独立成 muted 块 · mobile 全宽显著 + ETA 字号放大 + brand 高亮 */}
            <div className="flex items-center gap-3">
                <Button variant="ghost" size="icon" onClick={() => navigate("/")} className="shrink-0">
                    <ArrowLeft className="h-4 w-4" />
                </Button>
                <div className="h-10 w-10 rounded-xl bg-brand/10 flex items-center justify-center shrink-0">
                    <Activity className="h-5 w-5 text-brand" />
                </div>
                <div className="flex-1 min-w-0">
                    <h2 className="text-xl md:text-2xl font-bold text-foreground truncate">GEO诊断中</h2>
                    <p className="text-xs md:text-sm text-muted-foreground">
                        {connectionState === 'live' ? (
                            <span className="text-green-600">● 实时</span>
                        ) : connectionState === 'reconnecting' ? (
                            <span className="text-yellow-600">● 重连中…</span>
                        ) : (
                            <span className="text-muted-foreground">● 轮询中</span>
                        )}
                    </p>
                </div>
            </div>

            {/* 计时器 + ETA 卡 · mobile 全宽显著 · 视觉清晰度提升 */}
            <div className="flex items-center justify-between gap-3 px-3 py-2.5 rounded-lg bg-muted/30 border border-border/40">
                <div className="flex items-center gap-1.5 text-sm text-muted-foreground">
                    <Clock className="h-4 w-4 shrink-0" />
                    <span>已用时</span>
                    <span className="font-mono font-medium text-foreground">{formatElapsed(elapsed)}</span>
                </div>
                {estimateRemaining() && !progress.done && (
                    <div className="text-sm md:text-base font-semibold text-brand whitespace-nowrap">
                        预计还需 {estimateRemaining()}
                    </div>
                )}
            </div>

            {settlementManual && (
                /* [settlement-manual-ux 2026-08-25] 结算转人工:确定终态,**不是故障**。
                   所以不用 destructive 配色,也**不给「重新诊断」** ——
                   这一档钱冻着等人核实,重试无意义且可能双花。
                   只留「返回」:文案(Owner 定稿)说的是"回到这里就能看到结果",
                   即再进来一次、不是原地刷新 —— 所以这里也不该摆刷新按钮。 */
                <div className="bg-amber-500/10 border border-amber-500/25 text-amber-900 dark:text-amber-200 px-4 py-3 rounded-xl flex items-center gap-2">
                    <AlertCircle className="h-5 w-5 shrink-0" />
                    <span className="flex-1">{settlementManual}</span>
                    <Button variant="outline" size="sm" onClick={() => navigate(-1)}>
                        返回
                    </Button>
                </div>
            )}

            {error && (
                <div className="bg-destructive/10 border border-destructive/20 text-destructive px-4 py-3 rounded-xl flex items-center gap-2">
                    <AlertCircle className="h-5 w-5 shrink-0" />
                    <span className="flex-1">{error}</span>
                    <Button
                        variant="outline"
                        size="sm"
                        onClick={async () => {
                            try {
                                const resp = await authFetch(`/api/diagnosis/session/${id}/status`);
                                const data = await resp.json();
                                if (data.found) {
                                    setProgress({
                                        stage: data.stage || "collecting",
                                        progress: data.progress || 0,
                                        message: data.message || "",
                                        done: data.done,
                                        error: data.error,
                                    });
                                    if (data.done && data.diagnosis_id) {
                                        isDoneRef.current = true;
                                        setDiagnosisId(data.diagnosis_id);
                                        setError(null);
                                        refreshClients();
                                        navigate(`/diagnosis/report/${data.diagnosis_id}`);
                                    } else {
                                        setError(null);
                                    }
                                } else {
                                    setError("后端未找到该诊断任务，请返回重新发起");
                                }
                            } catch {
                                setError("无法连接服务器，请检查网络");
                            }
                        }}
                    >
                        手动刷新状态
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => navigate(-1)}>
                        返回
                    </Button>
                    <Button variant="outline" size="sm" onClick={() => navigate('/diagnosis/new', { replace: true })}>
                        重新诊断
                    </Button>
                </div>
            )}

            <Card className="border border-border rounded-xl shadow-none">
                <CardHeader className="p-5 pb-3">
                    <CardTitle>诊断进度</CardTitle>
                </CardHeader>
                <CardContent className="p-5 pt-0 space-y-6">
                    <div>
                        <div className="flex justify-between text-sm mb-2">
                            <span>总进度</span>
                            <span>{Math.round(overallProgress)}%</span>
                        </div>
                        <Progress value={overallProgress} className="h-3" />
                    </div>

                    <div className="space-y-4">
                        {currentStages.map((stage, index) => {
                            const isActive = stage.id === progress.stage;
                            const isCompleted = index < currentStageIndex || progress.done;

                            return (
                                <div
                                    key={stage.id}
                                    className={`flex items-center gap-4 p-4 rounded-xl transition-colors ${isActive ? 'bg-brand/5 border border-brand/20' :
                                        isCompleted ? 'bg-green-50' : 'bg-muted'
                                        }`}
                                >
                                    <div className="shrink-0">
                                        {isCompleted ? (
                                            <CheckCircle2 className="h-6 w-6 text-green-600" />
                                        ) : isActive ? (
                                            <Loader2 className="h-6 w-6 text-brand animate-spin" />
                                        ) : (
                                            <div className="h-6 w-6 rounded-full border-2 border-border" />
                                        )}
                                    </div>
                                    <div className="flex-1">
                                        <div className="font-medium text-foreground">{stage.label}</div>
                                        <div className="text-sm text-muted-foreground">{stage.description}</div>
                                        {isActive && progress.message && (
                                            <div className="text-sm text-brand mt-1 font-medium">{progress.message}</div>
                                        )}
                                    </div>
                                </div>
                            );
                        })}
                    </div>

                    {completed && (
                        <div className="text-center py-4">
                            <CheckCircle2 className="h-12 w-12 text-green-600 mx-auto mb-2" />
                            <p className="text-lg font-medium">诊断完成！</p>
                            <p className="text-muted-foreground">
                                总耗时 {formatElapsed(elapsed)}
                            </p>
                            {diagnosisId && (
                                <div className="flex gap-2 justify-center mt-4">
                                    <Button onClick={() => navigate(`/diagnosis/report/${diagnosisId}`)}>
                                        查看报告
                                    </Button>
                                    {brandId && (
                                        <Button variant="outline" onClick={() => navigate(`/pricing?brand_id=${brandId}&diagnosis_id=${diagnosisId}`)}>
                                            <Calculator className="mr-2 h-4 w-4" />
                                            制定 GEO 方案
                                        </Button>
                                    )}
                                    <Button variant="outline" onClick={() => {
                                        // [WO_WHITELABEL_COPY_UX 项2] 复制必须在手势同步栈发起(iOS/微信 webview)
                                        void copyAsyncText(async () => {
                                            const res = await authFetch(`/api/diagnosis/${diagnosisId}/share-link`, { method: 'POST' });
                                            const data = await res.json();
                                            if (!data.share_url) throw new Error('no share_url');
                                            return data.share_url as string;
                                        }).then(({ ok, text }) => {
                                            if (ok) toast.success('链接已复制，发给客户即可查看');
                                            else if (text) setManualCopyText(text);
                                            else toast.error('生成分享链接失败，请稍后重试');
                                        });
                                    }}>
                                        <Share2 className="mr-2 h-4 w-4" />
                                        复制分享链接
                                    </Button>
                                </div>
                            )}
                        </div>
                    )}
                </CardContent>
            </Card>

            {/* [看板增强] 实时日志面板 */}
            {logs.length > 0 && (
                <Card className="border border-border rounded-xl shadow-none">
                    <CardHeader className="p-5 pb-3">
                        <CardTitle className="text-base flex items-center gap-2">
                            实时日志
                            <span className="text-xs font-normal text-muted-foreground">
                                ({logs.length} 条消息)
                            </span>
                        </CardTitle>
                    </CardHeader>
                    <CardContent className="p-5 pt-0">
                        <div
                            className="bg-secondary rounded-xl p-4 font-mono text-xs max-h-[240px] overflow-y-auto border border-border/50"
                            style={{ scrollbarWidth: 'thin' }}
                        >
                            {logs.map((log, i) => (
                                <div key={i} className="flex gap-2 py-0.5">
                                    <span className="text-muted-foreground/60 shrink-0">{log.time}</span>
                                    <span className="text-emerald-400">{log.message}</span>
                                </div>
                            ))}
                            <div ref={logEndRef} />
                        </div>
                    </CardContent>
                </Card>
            )}

            <ManualCopyDialog text={manualCopyText} onClose={() => setManualCopyText(null)} />
        </div>
    );
}
