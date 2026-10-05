/**
 * GEO 调研监测后台 · 行业 + Prompts 合并面板 [P12-2b · 2026-05-26]
 *
 * 老板需求:
 *   "行业管理和提示词管理根本没有和现在的发布参谋的行业和提示词做一个同步,
 *    而且这两个管理可以直接合并成一个页面,左边选择行业,右边就显示对应的提示词
 *    可以随时更改。自动定时跑就按照这个内容进行跑"
 *
 * 设计:
 *   - 左侧 (4/12): 行业列表 · 搜索 · 列表 · 选中态高亮 · 编辑/删除/启停 · 新增
 *   - 右侧 (8/12): 当前行业的 prompts · 列表 · 编辑/启停/删除 · 新增/批量新建
 *   - 不引入新 endpoint · 全部复用 researchMonitorApi 既有方法
 *   - 不动 round_runner 跑批逻辑 · 它本来就读 active=TRUE 的 prompts 跑 · 改这里下次跑批立即生效
 *
 * 替代:
 *   原 IndustriesPanel.tsx + PromptsPanel.tsx 两个 tab → 1 个合并 tab
 *   旧两个文件本 commit 保留 · 后续验收后另开 commit 删
 */
'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
    Loader2, Plus, RefreshCw, Pencil, Trash2, Search, Upload,
    Power, PowerOff, AlertCircle, Play, ChevronDown, ChevronRight,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { useConfirmDialog } from '@/components/ui/confirm-dialog';
import {
    Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { SearchableSelect } from '@/components/ui/searchable-select';
import {
    researchMonitorApi, type Industry, type Prompt, type IndustryAlias,
} from '@/lib/researchMonitorApi';
import { formatApiErrorForDisplay } from '@/lib/api';

// ===========================================
// R1/R2 工具 (2026-07-05 · 自助调研 admin 侧)
// ===========================================

// 4 个 AI 引擎全量跑 (调研对象名允许露出)
const RESEARCH_ENGINES = 'DeepSeek / Kimi / 豆包 / 千问';

// 调研调用成本粗估: 每题 4 引擎 · 调研 AI 约 ¥0.38/题 (cost_estimator ai_call_per_round_4_platforms)
// 仅估「调研调用」· 不含后续抓取/清洗/分析 · 精确成本以跑批成本明细为准
function estimateResearchCost(promptCount: number): { calls: number; yuan: string } {
    return { calls: promptCount * 4, yuan: (promptCount * 0.38).toFixed(2) };
}

// R2-1 数据新鲜度: 从 last_research_at 算「上次调研 X 天前」· >45 天建议补跑 · null 未调研
function freshnessInfo(last: string | null | undefined): { text: string; tone: 'none' | 'ok' | 'stale' } {
    if (!last) return { text: '未调研', tone: 'none' };
    const ms = Date.now() - new Date(last).getTime();
    if (Number.isNaN(ms)) return { text: '未调研', tone: 'none' };
    const days = Math.floor(ms / 86_400_000);
    const text = days <= 0 ? '今天调研过' : `上次调研 ${days} 天前`;
    return { text, tone: days > 45 ? 'stale' : 'ok' };
}

// round 终态判定 (轮询到终态即停 + toast)
const TERMINAL_ROUND_STATUSES = new Set([
    'completed', 'failed', 'cancelled', 'partial_success', 'failed_resumable',
]);

// C5: round 终态 → 人话 (不泄露 raw status 码如 failed_resumable)
//   completed / partial_success 走成功 toast · 此表覆盖 else 分支的失败/取消/可续跑态
const ROUND_END_STATUS_LABEL: Record<string, string> = {
    failed: '调研失败 · 详情见「跑批管理」',
    cancelled: '调研已取消',
    failed_resumable: '调研部分未完成(可续跑) · 详情见「跑批管理」',
};


// ===========================================
// 行业编辑 Dialog
// P13-v7 (2026-05-27 老板): 删 slug + sort_order 字段 · 名称 + 启用即可
//   slug 后端自动生成 ind_<unix_ms> · sort_order 后端默认 0
// ===========================================
interface IndustryFormState {
    id?: number;
    name: string;
    active: boolean;
}
const EMPTY_IND_FORM: IndustryFormState = { name: '', active: true };

function IndustryEditorDialog(props: {
    open: boolean;
    onOpenChange: (v: boolean) => void;
    initial: IndustryFormState;
    onSaved: () => void;
}) {
    const [form, setForm] = useState<IndustryFormState>(props.initial);
    const [submitting, setSubmitting] = useState(false);
    useEffect(() => { setForm(props.initial); }, [props.initial, props.open]);

    const submit = async () => {
        const name = form.name.trim();
        if (!name) return toast.error('行业名称不能为空');
        setSubmitting(true);
        try {
            if (form.id) {
                await researchMonitorApi.updateIndustry(form.id, { name, active: form.active });
                toast.success(`已更新「${name}」`);
            } else {
                await researchMonitorApi.createIndustry({ name, active: form.active });
                toast.success(`已新增「${name}」`);
            }
            props.onSaved();
            props.onOpenChange(false);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '保存失败', 'admin'));
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <Dialog open={props.open} onOpenChange={props.onOpenChange}>
            <DialogContent>
                <DialogHeader>
                    <DialogTitle>{form.id ? '编辑行业' : '新增行业'}</DialogTitle>
                    <DialogDescription>填中文行业名即可 · 其他字段后端自动处理</DialogDescription>
                </DialogHeader>
                <div className="space-y-3 py-2">
                    <div className="space-y-1">
                        <Label className="text-xs">名称</Label>
                        <Input
                            value={form.name}
                            onChange={e => setForm(prev => ({ ...prev, name: e.target.value }))}
                            placeholder="例: 房地产 / 教育培训"
                            autoFocus
                        />
                    </div>
                    <div className="flex items-center gap-2 pt-1">
                        <Switch checked={form.active} onCheckedChange={v => setForm(prev => ({ ...prev, active: v }))} />
                        <Label className="text-sm">启用</Label>
                    </div>
                </div>
                <DialogFooter>
                    <Button variant="ghost" onClick={() => props.onOpenChange(false)} disabled={submitting}>取消</Button>
                    <Button onClick={submit} disabled={submitting}>
                        {submitting && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
                        保存
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

// ===========================================
// Prompt 编辑 Dialog
// ===========================================
interface PromptFormState {
    id?: number;
    prompt_text: string;
    sort_order: number;
    active: boolean;
}
const EMPTY_PROMPT_FORM: PromptFormState = { prompt_text: '', sort_order: 0, active: true };

// P12-fix-v2 (2026-05-26): prompts 数量不再有"行业上限" (老板拍板)
// 仅单次批量提交大小 500 (跟后端 MAX_BULK_CREATE_PROMPTS 对齐 · 防一次提交太大)
const MAX_BULK_PASTE = 500;

function PromptEditorDialog(props: {
    open: boolean;
    onOpenChange: (v: boolean) => void;
    industry: Industry | null;
    initial: PromptFormState;
    onSaved: () => void;
}) {
    const [form, setForm] = useState<PromptFormState>(props.initial);
    const [submitting, setSubmitting] = useState(false);
    useEffect(() => { setForm(props.initial); }, [props.initial, props.open]);

    const submit = async () => {
        const text = form.prompt_text.trim();
        if (!text) return toast.error('调研题目不能为空');
        if (text.length > 500) return toast.error('调研题目不能超过 500 字符');
        if (!props.industry) return toast.error('未选行业');
        setSubmitting(true);
        try {
            if (form.id) {
                await researchMonitorApi.updatePrompt(form.id, {
                    prompt_text: text, sort_order: form.sort_order, active: form.active,
                });
                toast.success('已更新调研题目');
            } else {
                await researchMonitorApi.createPrompt({
                    industry_id: props.industry.id,
                    prompt_text: text,
                    sort_order: form.sort_order,
                    active: form.active,
                });
                toast.success('已新增调研题目');
            }
            props.onSaved();
            props.onOpenChange(false);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '保存失败', 'admin'));
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <Dialog open={props.open} onOpenChange={props.onOpenChange}>
            <DialogContent>
                <DialogHeader>
                    <DialogTitle>{form.id ? '编辑调研题目' : '新增调研题目'}</DialogTitle>
                    <DialogDescription>
                        行业「{props.industry?.name ?? '?'}」 · 单条最多 500 字符 · 行业总数不限
                    </DialogDescription>
                </DialogHeader>
                <div className="space-y-3 py-2">
                    <div className="space-y-1">
                        <Label className="text-xs">调研题目</Label>
                        <Textarea
                            rows={4}
                            value={form.prompt_text}
                            onChange={e => setForm(prev => ({ ...prev, prompt_text: e.target.value }))}
                            placeholder="例: 2026 年买房有什么注意事项?"
                        />
                        <p className="text-[11px] text-muted-foreground">{form.prompt_text.length} / 500 字符</p>
                    </div>
                    {/* P14-v11 (2026-05-28 老板): 删 sort_order UI · 没啥意义 · 后端默认 0 */}
                    <div className="flex items-center gap-2 pt-1">
                        <Switch checked={form.active} onCheckedChange={v => setForm(prev => ({ ...prev, active: v }))} />
                        <Label className="text-sm">启用 (关闭后定时跑批不会扫到这条)</Label>
                    </div>
                </div>
                <DialogFooter>
                    <Button variant="ghost" onClick={() => props.onOpenChange(false)} disabled={submitting}>取消</Button>
                    <Button onClick={submit} disabled={submitting}>
                        {submitting && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
                        保存
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

// ===========================================
// 批量新建 Prompt Dialog
// ===========================================
function BulkCreatePromptsDialog(props: {
    open: boolean;
    onOpenChange: (v: boolean) => void;
    industry: Industry | null;
    // P12-fix-v2 (2026-05-26): 行业 prompts 数量不再有业务上限
    // 仅用 existingTotal 做显示参考 + MAX_BULK_PASTE 限制单次粘贴防一次太大
    existingTotal: number;
    onSaved: () => void;
}) {
    const [text, setText] = useState('');
    const [submitting, setSubmitting] = useState(false);
    useEffect(() => { if (props.open) setText(''); }, [props.open]);

    const lines = text.split('\n').map(s => s.trim()).filter(Boolean);
    // 单次粘贴超 500 条提示 · 不截断(让用户感知 · 但仍能提交前 500)
    const overLimit = lines.length > MAX_BULK_PASTE;
    const willInsert = overLimit ? MAX_BULK_PASTE : lines.length;

    const submit = async () => {
        if (!props.industry) return toast.error('未选行业');
        if (lines.length === 0) return toast.error('粘贴至少一行调研题目');
        setSubmitting(true);
        try {
            const res = await researchMonitorApi.bulkCreatePrompts(props.industry.id, lines.slice(0, willInsert));
            const inserted = (res as { inserted?: number }).inserted ?? willInsert;
            toast.success(`批量新增 ${inserted} 条调研题目`);
            props.onSaved();
            props.onOpenChange(false);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '批量新建失败', 'admin'));
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <Dialog open={props.open} onOpenChange={props.onOpenChange}>
            <DialogContent>
                <DialogHeader>
                    <DialogTitle>批量新建调研题目 · 「{props.industry?.name ?? '?'}」</DialogTitle>
                    <DialogDescription>
                        一行一个调研题目 · 当前已有 {props.existingTotal} 条 · 行业总数不限 · 单次粘贴最多 {MAX_BULK_PASTE} 条
                    </DialogDescription>
                </DialogHeader>
                <div className="space-y-2 py-2">
                    <Textarea
                        rows={12}
                        value={text}
                        onChange={e => setText(e.target.value)}
                        placeholder={'2026 年买房注意事项\n房贷怎么选\n烂尾楼怎么维权\n...'}
                    />
                    <p className="text-xs text-muted-foreground">
                        识别 {lines.length} 行 · 将插入 {willInsert} 条
                        {overLimit && (
                            <span className="text-amber-600">
                                {' '}(单次提交上限 {MAX_BULK_PASTE} 条 · 超出 {lines.length - MAX_BULK_PASTE} 条本次不提交 · 可分批粘贴)
                            </span>
                        )}
                    </p>
                </div>
                <DialogFooter>
                    <Button variant="ghost" onClick={() => props.onOpenChange(false)} disabled={submitting}>取消</Button>
                    <Button onClick={submit} disabled={submitting || willInsert === 0}>
                        {submitting && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
                        新增 {willInsert} 条
                    </Button>
                </DialogFooter>
            </DialogContent>
        </Dialog>
    );
}

// ===========================================
// 主面板
// ===========================================
export default function IndustriesPromptsPanel() {
    // ---- 行业 state ----
    const [industries, setIndustries] = useState<Industry[]>([]);
    const [includeInactive, setIncludeInactive] = useState(true);
    const [loadingInd, setLoadingInd] = useState(false);
    const [selectedId, setSelectedId] = useState<number | null>(null);
    const [search, setSearch] = useState('');

    // ---- prompts state (right pane) ----
    const [prompts, setPrompts] = useState<Prompt[]>([]);
    const [loadingPrompts, setLoadingPrompts] = useState(false);
    const [includePromptInactive, setIncludePromptInactive] = useState(true);

    // ---- dialogs ----
    const [indEditorOpen, setIndEditorOpen] = useState(false);
    const [indForm, setIndForm] = useState<IndustryFormState>(EMPTY_IND_FORM);
    const [promptEditorOpen, setPromptEditorOpen] = useState(false);
    const [promptForm, setPromptForm] = useState<PromptFormState>(EMPTY_PROMPT_FORM);
    const [bulkOpen, setBulkOpen] = useState(false);

    // ---- 单条 toggle 防抖 ----
    const [togglingId, setTogglingId] = useState<number | null>(null);

    // ---- R1 · 立即调研 (复用 manual-trigger · 单行业) ----
    const [confirmTarget, setConfirmTarget] = useState<Industry | null>(null); // 行业行/头部触发共用
    const [triggering, setTriggering] = useState(false);
    // 有一轮在跑时禁用所有触发按钮 (round_id 也用于完成轮询)
    const [runningRoundId, setRunningRoundId] = useState<string | null>(null);
    // [出口审核 FIX-2-FE F1] 轮询代际:超时点仍 running 时,refreshRunningRound 回填【同】round_id →
    //   effect deps [runningRoundId] 不变 → effect 不重跑 → 轮询永停 → 轮次真结束后按钮仍禁用只能 F5。
    //   bump pollGen 强制 effect 拆卸重建(重置 35min 窗口续 8s 轮询),终态最终被捕获并解锁。
    const [pollGen, setPollGen] = useState(0);
    // [出口审核 FE-2] 超时提示只弹一次:pollGen 续跑会重建 effect,若每次超时都 toast 则长轮次每 35min 反复
    //   弹同一条误导语。用 ref 跨 effect 重建持续记忆;新轮次(runningRoundId 变)时由下方 effect 重置。
    const timeoutWarnedRef = useRef(false);
    useEffect(() => { timeoutWarnedRef.current = false; }, [runningRoundId]);

    // C1/C2: 应用内确认弹窗替换 iOS 点不动的 window.confirm
    const [confirmDialog, askConfirm] = useConfirmDialog();

    // ---- loaders ----
    const fetchIndustries = useCallback(async () => {
        setLoadingInd(true);
        try {
            const res = await researchMonitorApi.listIndustries({ include_inactive: includeInactive });
            const sorted = [...res.industries].sort(
                (a, b) => a.sort_order - b.sort_order || a.id - b.id
            );
            setIndustries(sorted);
            // 没选过 + 列表非空 → 自动选第一个 active
            if (selectedId === null && sorted.length > 0) {
                const firstActive = sorted.find(i => i.active) ?? sorted[0];
                setSelectedId(firstActive.id);
            }
            // 当前选中已删/筛掉 → 切到第一个
            if (selectedId !== null && !sorted.find(i => i.id === selectedId)) {
                setSelectedId(sorted[0]?.id ?? null);
            }
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载行业失败', 'admin'));
        } finally {
            setLoadingInd(false);
        }
    }, [includeInactive, selectedId]);

    const fetchPrompts = useCallback(async () => {
        if (selectedId === null) {
            setPrompts([]);
            return;
        }
        setLoadingPrompts(true);
        try {
            const res = await researchMonitorApi.listPrompts(selectedId, {
                include_inactive: includePromptInactive,
            });
            setPrompts(
                [...res.prompts].sort((a, b) => a.sort_order - b.sort_order || a.id - b.id)
            );
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载调研题目失败', 'admin'));
        } finally {
            setLoadingPrompts(false);
        }
    }, [selectedId, includePromptInactive]);

    useEffect(() => { void fetchIndustries(); }, [fetchIndustries]);
    useEffect(() => { void fetchPrompts(); }, [fetchPrompts]);

    // ---- R1 · 检测是否已有一轮在跑 (任意来源: cron / 手动 / 自助) ----
    const refreshRunningRound = useCallback(async () => {
        try {
            const res = await researchMonitorApi.listRounds({ limit: 10 });
            const active = res.rounds.find(r => r.status === 'running' || r.status === 'pending');
            setRunningRoundId(active ? active.round_id : null);
        } catch {
            /* 静默 · 不阻塞主面板 */
        }
    }, []);
    useEffect(() => { void refreshRunningRound(); }, [refreshRunningRound]);

    // ---- R1 · 发起后进度轮询 (复用 EvolutionBoard 模式 · 超时 ≥35min) ----
    useEffect(() => {
        if (!runningRoundId) return;
        const startedAt = Date.now();
        const timer = setInterval(async () => {
            if (Date.now() - startedAt > 35 * 60_000) {
                clearInterval(timer);
                // [出口审核 FE-2] 文案匹配"续跑"真实行为(自动刷新并未暂停·pollGen 重建续轮询),且只弹一次
                //   (避免长轮次每 35min 重复弹误导语)。
                if (!timeoutWarnedRef.current) {
                    timeoutWarnedRef.current = true;
                    toast.info('调研耗时较长,仍在后台执行 · 自动刷新继续中');
                }
                // FIX-5: 不盲目 setRunningRoundId(null)(轮次可能真还在跑) · 按后端真相复位
                //   终态 → refreshRunningRound 清 runningRoundId 解锁三处触发按钮。
                void refreshRunningRound();
                // [出口审核 FIX-2-FE F1] 仍 running 时 refreshRunningRound 回填同 round_id 不触发 effect
                //   重跑(轮询会永停) → bump pollGen 强制 effect 重建续下一段 35min 轮询,直到轮次真终态被
                //   上方 TERMINAL 分支捕获清 runningRoundId 解锁。终态则重建后因 runningRoundId=null 早退。
                setPollGen(g => g + 1);
                return;
            }
            try {
                const st = await researchMonitorApi.getRoundLiveStatus(runningRoundId);
                const s = st.round.status;
                if (TERMINAL_ROUND_STATUSES.has(s)) {
                    clearInterval(timer);
                    setRunningRoundId(null);
                    void fetchIndustries(); // 刷新 last_research_at 新鲜度
                    if (s === 'completed') toast.success('调研完成 · 新数据已入库');
                    else if (s === 'partial_success') toast.success('调研部分完成 · 详见「跑批管理」');
                    else toast.error(ROUND_END_STATUS_LABEL[s] ?? '调研结束 · 详情见「跑批管理」');
                }
            } catch {
                /* 网络抖动静默重试 · 不打断 */
            }
        }, 8000);
        return () => clearInterval(timer);
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [runningRoundId, pollGen]);

    // ---- R1 · 发起单行业调研 (复用 manual-trigger · industry_ids=[id]) ----
    const doTriggerResearch = async (industry: Industry) => {
        setTriggering(true);
        try {
            const res = await researchMonitorApi.manualTriggerRound({ industry_ids: [industry.id] });
            toast.success(`已发起「${industry.name}」调研 · 编号 ${res.round_id}`, {
                description: '后台异步执行 · 完成后自动提示',
                duration: 6000,
            });
            setConfirmTarget(null);
            setRunningRoundId(res.round_id);
        } catch (e) {
            const err = e as Error & { status?: number };
            if (err.status === 409) {
                // 后端 enforce_single_active / 月预算 熔断
                toast.error('已有调研进行中或本月预算不足 · 请稍后在「跑批管理」查看', { duration: 8000 });
                void refreshRunningRound();
            } else {
                toast.error(formatApiErrorForDisplay(err, '发起失败', 'admin'));
            }
        } finally {
            setTriggering(false);
        }
    };

    // ---- derived ----
    const filteredIndustries = useMemo(() => {
        const q = search.trim().toLowerCase();
        if (!q) return industries;
        // P13-v7: search 只匹配 name (slug 用户看不到 · 没意义搜)
        return industries.filter(i => i.name.toLowerCase().includes(q));
    }, [industries, search]);
    const selectedIndustry = industries.find(i => i.id === selectedId) ?? null;
    const activePromptsCount = prompts.filter(p => p.active).length;

    // ---- 行业 actions ----
    const openCreateIndustry = () => {
        // P13-v7: 删 sort_order · 后端自动默认 0
        setIndForm({ ...EMPTY_IND_FORM });
        setIndEditorOpen(true);
    };
    const openEditIndustry = (ind: Industry) => {
        // P13-v7: 删 slug + sort_order · 编辑只传 id + name + active
        setIndForm({
            id: ind.id, name: ind.name, active: ind.active,
        });
        setIndEditorOpen(true);
    };
    const toggleIndustryActive = async (ind: Industry) => {
        setTogglingId(ind.id);
        try {
            await researchMonitorApi.updateIndustry(ind.id, { active: !ind.active });
            toast.success(`${ind.name} ${!ind.active ? '已启用' : '已禁用'}`);
            await fetchIndustries();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '切换失败', 'admin'));
        } finally {
            setTogglingId(null);
        }
    };
    const deleteIndustry = async (ind: Industry) => {
        // Issue 5 修 (LOW · 2026-05-26): 文案对齐后端实际行为
        //   后端 DELETE /industries/{id} 只 UPDATE industry active=FALSE · 不动 prompts 表
        //   prompts 仍 active=TRUE 但因为父 industry 禁用 · 跑批扫不到 → 实际"不跑"
        //   老板 review 指出原文案"prompts 也会一起禁用"误导 · 改成准确描述
        const ok = await askConfirm({
            title: `停用行业「${ind.name}」?`,
            description: '停用后:该行业不再参与定时跑批;题目全部保留;随时可重新启用。',
            confirmLabel: '停用',
            cancelLabel: '取消',
        });
        if (!ok) return;
        try {
            await researchMonitorApi.deleteIndustry(ind.id);
            toast.success(`已删除「${ind.name}」`);
            // 删了当前选中 → 切到列表第一个
            if (selectedId === ind.id) setSelectedId(null);
            await fetchIndustries();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '删除失败', 'admin'));
        }
    };

    // ---- prompt actions ----
    const openCreatePrompt = () => {
        if (!selectedIndustry) return toast.error('请先选行业');
        // Issue 2 修: 用 active 数对齐后端 _count_active_prompts() · 软删的不占名额
        // P12-fix-v2 (2026-05-26): 行业 prompts 数量不限 · 删 25 上限校验
        const nextOrder = prompts.length > 0 ? Math.max(...prompts.map(p => p.sort_order)) + 10 : 0;
        setPromptForm({ ...EMPTY_PROMPT_FORM, sort_order: nextOrder });
        setPromptEditorOpen(true);
    };
    const openEditPrompt = (p: Prompt) => {
        setPromptForm({
            id: p.id, prompt_text: p.prompt_text,
            sort_order: p.sort_order, active: p.active,
        });
        setPromptEditorOpen(true);
    };
    const togglePromptActive = async (p: Prompt) => {
        setTogglingId(p.id);
        try {
            await researchMonitorApi.togglePrompt(p.id);
            await fetchPrompts();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '切换失败', 'admin'));
        } finally {
            setTogglingId(null);
        }
    };
    const deletePrompt = async (p: Prompt) => {
        const ok = await askConfirm({
            title: '停用这条题目?',
            description: '定时跑批会跳过它,内容保留,可随时重新启用。',
            confirmLabel: '停用',
            cancelLabel: '取消',
        });
        if (!ok) return;
        try {
            await researchMonitorApi.deletePrompt(p.id);
            toast.success('已删除调研题目');
            await fetchPrompts();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '删除失败', 'admin'));
        }
    };

    // ===========================================
    // 渲染
    // ===========================================
    return (
        <div className="grid grid-cols-1 lg:grid-cols-12 gap-4">
            {/* ============ 左侧 · 行业列表 ============ */}
            <div className="lg:col-span-4 space-y-3">
                <Card>
                    <CardContent className="p-3 space-y-3">
                        {/* 头部 */}
                        <div className="flex items-center justify-between gap-2">
                            <h3 className="text-sm font-semibold">行业列表</h3>
                            <div className="flex items-center gap-2">
                                <div className="flex items-center gap-1">
                                    <Switch
                                        checked={includeInactive}
                                        onCheckedChange={setIncludeInactive}
                                        className="data-[state=checked]:bg-foreground"
                                    />
                                    <Label className="text-[11px] text-muted-foreground">含未启用</Label>
                                </div>
                                <Button size="icon" variant="ghost" onClick={fetchIndustries} disabled={loadingInd}>
                                    {loadingInd ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                                </Button>
                            </div>
                        </div>

                        {/* 搜索 */}
                        <div className="relative">
                            <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
                            <Input
                                value={search}
                                onChange={e => setSearch(e.target.value)}
                                placeholder="搜索行业名"
                                className="pl-9 h-9 text-sm"
                            />
                        </div>

                        {/* 列表 */}
                        <div className="space-y-1 max-h-[60vh] overflow-y-auto pr-1">
                            {filteredIndustries.length === 0 && (
                                <div className="text-center py-8 text-sm text-muted-foreground">
                                    {search ? '无匹配行业' : '暂无行业 · 点 + 新建一个'}
                                </div>
                            )}
                            {filteredIndustries.map(ind => {
                                const selected = ind.id === selectedId;
                                return (
                                    <div
                                        key={ind.id}
                                        className={`group rounded-md border px-2.5 py-2 cursor-pointer transition-colors ${
                                            selected ? 'border-brand bg-brand/5' : 'hover:bg-muted/30'
                                        }`}
                                        onClick={() => setSelectedId(ind.id)}
                                    >
                                        <div className="flex items-center gap-2">
                                            <span className={`font-medium text-sm truncate flex-1 ${!ind.active ? 'text-muted-foreground line-through' : ''}`}>
                                                {ind.name}
                                            </span>
                                            {/* P13-v7 (老板): 删 slug 显示 · 改成 active/inactive 状态 badge */}
                                            {!ind.active && (
                                                <Badge variant="secondary" className="text-[10px] shrink-0 bg-muted">
                                                    未启用
                                                </Badge>
                                            )}
                                        </div>
                                        {/* R2-1: 数据新鲜度 + 参与跑批题目数 (人话化) */}
                                        {(() => {
                                            const f = freshnessInfo(ind.last_research_at);
                                            const toneCls = f.tone === 'stale'
                                                ? 'text-amber-600'
                                                : f.tone === 'none'
                                                    ? 'text-muted-foreground/60'
                                                    : 'text-muted-foreground';
                                            const cnt = ind.active_prompt_count;
                                            return (
                                                <div className={`mt-0.5 text-[10px] ${toneCls}`}>
                                                    {f.tone === 'stale' && '⚠ 建议补跑 · '}
                                                    {f.text}
                                                    {typeof cnt === 'number' && (
                                                        <span className="text-muted-foreground/70"> · {cnt} 条题目参与跑批</span>
                                                    )}
                                                </div>
                                            );
                                        })()}
                                        <div className="flex items-center gap-1 mt-1 opacity-40 group-hover:opacity-100 transition-opacity">
                                            {/* R1: 立即调研本行业 (复用 manual-trigger · 单行业) */}
                                            <Button
                                                size="sm"
                                                variant="ghost"
                                                className="h-6 px-1.5 text-xs text-brand"
                                                disabled={(ind.active_prompt_count ?? 0) === 0 || !!runningRoundId}
                                                onClick={e => { e.stopPropagation(); setConfirmTarget(ind); }}
                                                title={
                                                    (ind.active_prompt_count ?? 0) === 0
                                                        ? '无启用题目 · 请先配置调研题目'
                                                        : runningRoundId
                                                            ? '当前有调研进行中'
                                                            : '立即调研本行业'
                                                }
                                            >
                                                <Play className="h-3 w-3" />
                                            </Button>
                                            <Button
                                                size="sm"
                                                variant="ghost"
                                                className="h-6 px-1.5 text-xs"
                                                onClick={e => { e.stopPropagation(); openEditIndustry(ind); }}
                                                title="编辑"
                                            >
                                                <Pencil className="h-3 w-3" />
                                            </Button>
                                            <Button
                                                size="sm"
                                                variant="ghost"
                                                className="h-6 px-1.5 text-xs"
                                                disabled={togglingId === ind.id}
                                                onClick={e => { e.stopPropagation(); toggleIndustryActive(ind); }}
                                                title={ind.active ? '禁用' : '启用'}
                                            >
                                                {ind.active ? <PowerOff className="h-3 w-3" /> : <Power className="h-3 w-3 text-green-600" />}
                                            </Button>
                                            <Button
                                                size="sm"
                                                variant="ghost"
                                                className="h-6 px-1.5 text-xs text-red-600 hover:text-red-700"
                                                onClick={e => { e.stopPropagation(); deleteIndustry(ind); }}
                                                title="软删"
                                            >
                                                <Trash2 className="h-3 w-3" />
                                            </Button>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>

                        {/* 底部新增 */}
                        <Button size="sm" variant="outline" className="w-full" onClick={openCreateIndustry}>
                            <Plus className="h-3.5 w-3.5 mr-1.5" />新增行业
                        </Button>
                    </CardContent>
                </Card>
            </div>

            {/* ============ 右侧 · 当前行业的 Prompts ============ */}
            <div className="lg:col-span-8 space-y-3">
                <Card>
                    <CardContent className="p-4 space-y-3">
                        {/* 头部 */}
                        {selectedIndustry ? (
                            <div className="flex items-center gap-2 flex-wrap">
                                <h3 className="text-base font-semibold">
                                    调研题目 · 「{selectedIndustry.name}」
                                </h3>
                                {/* P12-fix-v2: 行业 prompts 不再有"上限" · R2-2 人话化: active → 参与跑批 */}
                                <Badge variant="secondary" className="text-[10px]" title="启用题目数 / 总数 · 无上限">
                                    参与跑批 {activePromptsCount}
                                    {prompts.length !== activePromptsCount && (
                                        <span className="text-muted-foreground/70 ml-1">
                                            / 共 {prompts.length}
                                        </span>
                                    )}
                                </Badge>
                                {!selectedIndustry.active && (
                                    <Badge variant="secondary" className="text-[10px] bg-red-100 text-red-700">
                                        行业已禁用 · 题目不会跑
                                    </Badge>
                                )}
                                <div className="ml-auto flex items-center gap-2">
                                    {/* R1: 用当前行业启用题目立即调研 (主按钮) */}
                                    <Button
                                        size="sm"
                                        onClick={() => setConfirmTarget(selectedIndustry)}
                                        disabled={
                                            activePromptsCount === 0 ||
                                            !selectedIndustry.active ||
                                            !!runningRoundId
                                        }
                                        title={
                                            !selectedIndustry.active
                                                ? '行业已禁用 · 无法调研'
                                                : activePromptsCount === 0
                                                    ? '无启用题目 · 请先配置调研题目'
                                                    : runningRoundId
                                                        ? '当前有调研进行中'
                                                        : '用这些启用题目立即调研本行业'
                                        }
                                    >
                                        <Play className="h-3.5 w-3.5 mr-1.5" />
                                        {runningRoundId
                                            ? '调研进行中…'
                                            : `用这 ${activePromptsCount} 条题目立即调研`}
                                    </Button>
                                    <div className="flex items-center gap-1">
                                        <Switch
                                            checked={includePromptInactive}
                                            onCheckedChange={setIncludePromptInactive}
                                            className="data-[state=checked]:bg-foreground"
                                        />
                                        <Label className="text-[11px] text-muted-foreground">含未启用</Label>
                                    </div>
                                    <Button size="sm" variant="ghost" onClick={fetchPrompts} disabled={loadingPrompts}>
                                        {loadingPrompts ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                                    </Button>
                                    {/* P12-fix-v2: 行业 prompts 不限上限 · 只看是否选了行业 */}
                                    <Button size="sm" variant="outline" onClick={() => setBulkOpen(true)} disabled={!selectedIndustry}>
                                        <Upload className="h-3.5 w-3.5 mr-1.5" />批量
                                    </Button>
                                    <Button size="sm" onClick={openCreatePrompt} disabled={!selectedIndustry}>
                                        <Plus className="h-3.5 w-3.5 mr-1.5" />新增调研题目
                                    </Button>
                                </div>
                            </div>
                        ) : (
                            <h3 className="text-base font-semibold text-muted-foreground">请先从左侧选一个行业</h3>
                        )}

                        {/* prompts 列表 */}
                        {selectedIndustry && (
                            <div className="space-y-1.5">
                                {loadingPrompts && (
                                    <div className="py-8 text-center">
                                        <Loader2 className="h-5 w-5 animate-spin text-muted-foreground mx-auto" />
                                    </div>
                                )}

                                {!loadingPrompts && prompts.length === 0 && (
                                    <div className="text-center py-12 border rounded-md border-dashed">
                                        <AlertCircle className="h-8 w-8 text-muted-foreground/30 mx-auto mb-2" />
                                        <p className="text-sm text-muted-foreground">该行业暂无调研题目</p>
                                        <p className="text-xs text-muted-foreground mt-1">
                                            点右上 "+ 新增调研题目" 或 "批量" 粘贴一批
                                        </p>
                                    </div>
                                )}

                                {!loadingPrompts && prompts.length > 0 && (
                                    <div className="space-y-1.5">
                                        {prompts.map((p, idx) => (
                                            <div
                                                key={p.id}
                                                className={`flex items-start gap-2 p-2.5 rounded-md border ${
                                                    p.active ? 'bg-muted/20' : 'bg-muted/40 opacity-60'
                                                }`}
                                            >
                                                <span className="w-6 text-xs font-bold text-muted-foreground tabular-nums shrink-0 pt-0.5 text-right">
                                                    {idx + 1}
                                                </span>
                                                <div className="flex-1 min-w-0">
                                                    <p className={`text-sm break-words ${!p.active ? 'line-through' : ''}`}>{p.prompt_text}</p>
                                                    <p className="text-[10px] text-muted-foreground mt-1">
                                                        更新 {p.updated_at?.slice(0, 16).replace('T', ' ') ?? '?'}
                                                    </p>
                                                </div>
                                                <div className="flex items-center gap-1 shrink-0">
                                                    <Button
                                                        size="sm"
                                                        variant="ghost"
                                                        className="h-7 px-2 text-xs"
                                                        disabled={togglingId === p.id}
                                                        onClick={() => togglePromptActive(p)}
                                                        title={p.active ? '禁用' : '启用'}
                                                    >
                                                        {p.active ? <PowerOff className="h-3 w-3" /> : <Power className="h-3 w-3 text-green-600" />}
                                                    </Button>
                                                    <Button
                                                        size="sm"
                                                        variant="ghost"
                                                        className="h-7 px-2 text-xs"
                                                        onClick={() => openEditPrompt(p)}
                                                        title="编辑"
                                                    >
                                                        <Pencil className="h-3 w-3" />
                                                    </Button>
                                                    <Button
                                                        size="sm"
                                                        variant="ghost"
                                                        className="h-7 px-2 text-xs text-red-600 hover:text-red-700"
                                                        onClick={() => deletePrompt(p)}
                                                        title="软删"
                                                    >
                                                        <Trash2 className="h-3 w-3" />
                                                    </Button>
                                                </div>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>
                        )}

                        {/* 底部 hint */}
                        {selectedIndustry && prompts.length > 0 && (
                            <div className="text-[11px] text-muted-foreground border-t pt-2 mt-2">
                                <span className="font-medium">自动定时跑批逻辑:</span>
                                每月扫描所有启用行业 → 取其所有启用题目 → 4 个 AI 引擎({RESEARCH_ENGINES})抓取。
                                改完这里立即生效,无需重启。也可点上方「立即调研」对本行业即时跑一轮。
                            </div>
                        )}
                    </CardContent>
                </Card>
            </div>

            {/* ============ R2 · 行业别名归并审核 (折叠 · 面板底部次级区) ============ */}
            <div className="lg:col-span-12">
                <AliasReviewSection industries={industries} />
            </div>

            {/* ============ R1 · 立即调研 确认弹窗 ============ */}
            <Dialog open={!!confirmTarget} onOpenChange={v => { if (!v) setConfirmTarget(null); }}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>立即调研「{confirmTarget?.name}」</DialogTitle>
                        <DialogDescription>
                            将对该行业所有启用题目发起一次调研跑批(4 个 AI 引擎:{RESEARCH_ENGINES})。
                        </DialogDescription>
                    </DialogHeader>
                    {confirmTarget && (() => {
                        const cnt = confirmTarget.active_prompt_count ?? 0;
                        const est = estimateResearchCost(cnt);
                        return (
                            <div className="space-y-2 py-2 text-sm">
                                <div className="flex justify-between">
                                    <span className="text-muted-foreground">参与题目</span>
                                    <span className="font-medium">{cnt} 条</span>
                                </div>
                                <div className="flex justify-between">
                                    <span className="text-muted-foreground">调研调用</span>
                                    <span className="font-medium">{cnt} 题 × 4 引擎 = {est.calls} 次</span>
                                </div>
                                <div className="flex justify-between">
                                    <span className="text-muted-foreground">预估调研成本</span>
                                    <span className="font-medium">约 ¥{est.yuan}</span>
                                </div>
                                <p className="text-[11px] text-muted-foreground pt-1">
                                    (仅估调研调用 · 不含后续抓取/清洗/分析 · 实际成本以「跑批管理 → 成本明细」为准)
                                </p>
                                {runningRoundId && (
                                    <p className="text-[11px] text-amber-600">当前已有调研进行中 · 需等其结束后再发起</p>
                                )}
                            </div>
                        );
                    })()}
                    <DialogFooter>
                        <Button variant="ghost" onClick={() => setConfirmTarget(null)} disabled={triggering}>取消</Button>
                        <Button
                            onClick={() => confirmTarget && void doTriggerResearch(confirmTarget)}
                            disabled={
                                triggering ||
                                !!runningRoundId ||
                                (confirmTarget?.active_prompt_count ?? 0) === 0
                            }
                        >
                            {triggering && <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />}
                            确认调研
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* ============ Dialogs ============ */}
            <IndustryEditorDialog
                open={indEditorOpen}
                onOpenChange={setIndEditorOpen}
                initial={indForm}
                onSaved={fetchIndustries}
            />
            <PromptEditorDialog
                open={promptEditorOpen}
                onOpenChange={setPromptEditorOpen}
                industry={selectedIndustry}
                initial={promptForm}
                onSaved={fetchPrompts}
            />
            <BulkCreatePromptsDialog
                open={bulkOpen}
                onOpenChange={setBulkOpen}
                industry={selectedIndustry}
                existingTotal={prompts.length}
                onSaved={fetchPrompts}
            />

            {/* C1/C2: 应用内确认弹窗 (iOS window.confirm 点不动铁律) */}
            {confirmDialog}
        </div>
    );
}

// ===========================================
// R2 · 行业别名归并审核区 (折叠 · 次级 · 别喧宾夺主)
//   列 AI/人工归并记录 (用户行业原文 → 标准行业) · admin 可下拉改判
// ===========================================
function AliasReviewSection({ industries }: { industries: Industry[] }) {
    const [open, setOpen] = useState(false);
    const [aliases, setAliases] = useState<IndustryAlias[]>([]);
    const [loading, setLoading] = useState(false);
    const [loaded, setLoaded] = useState(false);
    const [savingId, setSavingId] = useState<number | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        try {
            const res = await researchMonitorApi.listIndustryAliases();
            setAliases(res.aliases);
            setLoaded(true);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载别名失败', 'admin'));
        } finally {
            setLoading(false);
        }
    }, []);

    // 首次展开才拉 (别喧宾夺主 · 默认不请求)
    useEffect(() => { if (open && !loaded) void load(); }, [open, loaded, load]);

    const reassign = async (alias: IndustryAlias, industryId: number) => {
        if (industryId === alias.industry_id) return;
        setSavingId(alias.id);
        try {
            await researchMonitorApi.updateIndustryAlias(alias.id, industryId);
            toast.success('已改判别名归属 (记为人工确认)');
            await load();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '改判失败', 'admin'));
        } finally {
            setSavingId(null);
        }
    };

    return (
        <Card>
            <CardContent className="p-3 space-y-2">
                <div className="flex items-center gap-2">
                    <button
                        type="button"
                        className="flex items-center gap-2 flex-1 min-w-0 text-left"
                        onClick={() => setOpen(o => !o)}
                    >
                        {open ? <ChevronDown className="h-3.5 w-3.5 shrink-0" /> : <ChevronRight className="h-3.5 w-3.5 shrink-0" />}
                        <span className="text-sm font-semibold shrink-0">行业别名归并审核</span>
                        <Badge variant="secondary" className="text-[10px] shrink-0">AI 自动归并 · 可人工改判</Badge>
                        {loaded && (
                            <span className="text-[11px] text-muted-foreground shrink-0">共 {aliases.length} 条</span>
                        )}
                    </button>
                    {open && (
                        <Button size="sm" variant="ghost" onClick={() => void load()} disabled={loading}>
                            {loading ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <RefreshCw className="h-3.5 w-3.5" />}
                        </Button>
                    )}
                </div>

                {open && (
                    <div className="space-y-1.5">
                        <p className="text-[11px] text-muted-foreground">
                            用户输入的行业原文经 AI 归并到标准行业 · 归并有误可在此改判(改判后记为人工确认)。
                        </p>

                        {loading && aliases.length === 0 && (
                            <div className="py-6 text-center">
                                <Loader2 className="h-5 w-5 animate-spin text-muted-foreground mx-auto" />
                            </div>
                        )}

                        {!loading && loaded && aliases.length === 0 && (
                            <div className="py-6 text-center text-sm text-muted-foreground">
                                暂无别名归并记录
                            </div>
                        )}

                        {aliases.map(a => (
                            <div key={a.id} className="flex items-center gap-2 p-2 rounded-md border text-sm">
                                <span className="font-medium truncate flex-1 min-w-0" title={a.normalized_alias}>
                                    {a.normalized_alias}
                                </span>
                                <span className="text-muted-foreground text-xs shrink-0">→</span>
                                {/* 可搜索:接口来源(geo_research_industries)· 生产 19 个行业且会随扩容增长 */}
                                <SearchableSelect
                                    value={String(a.industry_id)}
                                    onChange={v => void reassign(a, Number(v))}
                                    disabled={savingId === a.id}
                                    className="w-40 h-8 text-xs shrink-0"
                                    placeholder={a.industry_name ?? '选择行业'}
                                    searchPlaceholder="搜索行业"
                                    emptyText="没有匹配的行业"
                                    options={industries.map(ind => ({ value: String(ind.id), label: ind.name }))}
                                />
                                <Badge
                                    variant="secondary"
                                    className={`text-[10px] shrink-0 ${a.resolved_by === 'admin' ? 'bg-green-100 text-green-700' : ''}`}
                                >
                                    {a.resolved_by === 'admin' ? '人工确认' : 'AI 归并'}
                                </Badge>
                                {typeof a.confidence === 'number' && a.resolved_by === 'llm' && (
                                    <span className="text-[10px] text-muted-foreground tabular-nums shrink-0">
                                        {Math.round(a.confidence * 100)}%
                                    </span>
                                )}
                                {savingId === a.id && <Loader2 className="h-3.5 w-3.5 animate-spin shrink-0" />}
                            </div>
                        ))}
                    </div>
                )}
            </CardContent>
        </Card>
    );
}
