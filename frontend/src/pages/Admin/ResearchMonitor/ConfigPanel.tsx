/**
 * GEO 调研监测后台 · 系统配置 [P14-v11 · 2026-05-28 重设计]
 *
 * 旧设计: 表格直出 key/value/desc/updated_by · 让管理员理解 JSON 实现细节
 * 新设计: 面向管理员的设置表单 · 按业务分 5 区块 · 隐藏 key 概念
 *
 * 5 区块:
 *   1. 自动跑批 (cron_enabled / cron_days / cron_hour)
 *   2. 预算限制 (budget_per_round_yuan / budget_per_month_yuan)
 *   3. 入库与保留 (article_min_chars_for_review / article_oss_ttl_days / lock_stale_minutes)
 *   4. 高级 · 模型配置 (model_*) [默认折叠]
 *   5. 高级 · 失败保护 (circuit_breaker_*) [默认折叠]
 *
 * 交互:
 *   - 区块右上角 "编辑" · 进入 inline 编辑 · 底部 取消/保存
 *   - 保存成功 toast 说明生效时机 (跑批: 下次跑批生效 / 预算/熔断: 立即生效)
 *   - 顶部保留 "刷新" + "恢复默认"
 *
 * 后端 API 不变 · 仅前端 key → 表单字段映射
 */
'use client';

import { useCallback, useEffect, useMemo, useState } from 'react';
import {
    Loader2, RefreshCw, Pencil, AlertTriangle, ChevronDown, ChevronRight,
    Calendar, Wallet, Library, Brain, Shield,
} from 'lucide-react';
import { toast } from 'sonner';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
    Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import { Switch } from '@/components/ui/switch';
import { researchMonitorApi, type ConfigItem } from '@/lib/researchMonitorApi';
import { formatApiErrorForDisplay } from '@/lib/api';

const RESET_CONFIRM_CODE = 'RESET_RESEARCH_MONITOR_CONFIG';

// =========================================================================
// key → value 通用工具
// =========================================================================

function findValue<T = unknown>(items: ConfigItem[], key: string, fallback: T): T {
    const f = items.find(i => i.key === key);
    return f ? (f.value as T) : fallback;
}

// =========================================================================
// 主组件
// =========================================================================

export default function ConfigPanel() {
    const [items, setItems] = useState<ConfigItem[]>([]);
    const [loading, setLoading] = useState(false);
    const [resetOpen, setResetOpen] = useState(false);
    const [resetCodeInput, setResetCodeInput] = useState('');
    const [resetSubmitting, setResetSubmitting] = useState(false);

    const fetchList = useCallback(async () => {
        setLoading(true);
        try {
            const res = await researchMonitorApi.listConfig();
            setItems(res.configs);
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '加载配置失败', 'admin'));
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { void fetchList(); }, [fetchList]);

    const submitReset = async () => {
        if (resetCodeInput !== RESET_CONFIRM_CODE) {
            toast.error('confirm_code 不匹配');
            return;
        }
        setResetSubmitting(true);
        try {
            const res = await researchMonitorApi.resetConfig(resetCodeInput);
            toast.success(`已重置 ${res.reset_count} 项`, {
                description: res.keys.join(' · '),
                duration: 6000,
            });
            setResetOpen(false);
            setResetCodeInput('');
            void fetchList();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '重置失败', 'admin'));
        } finally {
            setResetSubmitting(false);
        }
    };

    // 单 key 保存 · 区块 onSave 调
    const saveOne = useCallback(async (key: string, value: unknown) => {
        await researchMonitorApi.updateConfig(key, value);
    }, []);

    if (loading && items.length === 0) {
        return (
            <div className="flex items-center justify-center py-20">
                <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
            </div>
        );
    }

    return (
        <div className="space-y-4">
            {/* 顶部标题 + 操作 */}
            <div className="flex items-start justify-between gap-3">
                <div>
                    <h2 className="text-xl font-bold">系统配置</h2>
                    <p className="text-sm text-muted-foreground mt-1">
                        控制 GEO 调研的自动跑批、预算、模型和安全阈值
                    </p>
                </div>
                <div className="flex items-center gap-2 shrink-0">
                    <Button size="sm" variant="ghost" onClick={() => void fetchList()} disabled={loading}>
                        <RefreshCw className={`w-4 h-4 mr-1 ${loading ? 'animate-spin' : ''}`} />
                        刷新
                    </Button>
                    <Button size="sm" variant="outline" onClick={() => { setResetCodeInput(''); setResetOpen(true); }}>
                        <AlertTriangle className="w-4 h-4 mr-1" />
                        恢复默认
                    </Button>
                </div>
            </div>

            {/* 1. 自动跑批 */}
            <SectionCard
                icon={<Calendar className="w-4 h-4" />}
                title="自动跑批"
                subtitle="按时自动触发跑批 · 关闭后只能手动触发"
                effectNote="下次跑批生效"
                onSaveBatch={async (changes) => {
                    for (const [k, v] of Object.entries(changes)) await saveOne(k, v);
                }}
                onSavedRefresh={fetchList}
                renderView={() => {
                    const enabled = findValue<boolean>(items, 'cron_enabled', true);
                    const days = findValue<string>(items, 'cron_days', '1,16');
                    const hour = findValue<number>(items, 'cron_hour', 2);
                    return (
                        <div className="space-y-2 text-sm">
                            <Row label="自动跑批">
                                {enabled
                                    ? <span className="text-green-700">已开启</span>
                                    : <span className="text-amber-700">已关闭 · 仅手动触发</span>}
                            </Row>
                            {enabled && (
                                <>
                                    <Row label="每月日期">{days} 号</Row>
                                    <Row label="执行时间">{String(hour).padStart(2, '0')}:00 (北京时间)</Row>
                                </>
                            )}
                        </div>
                    );
                }}
                renderEdit={(draft, set) => {
                    const enabled = draft.cron_enabled ?? findValue<boolean>(items, 'cron_enabled', true);
                    const days = (draft.cron_days as string) ?? findValue<string>(items, 'cron_days', '1,16');
                    const hour = (draft.cron_hour as number) ?? findValue<number>(items, 'cron_hour', 2);
                    return (
                        <div className="space-y-3 text-sm">
                            <Row label="自动跑批">
                                <div className="flex items-center gap-2">
                                    <Switch checked={enabled as boolean}
                                        onCheckedChange={(v) => set('cron_enabled', v)} />
                                    <span className="text-xs text-muted-foreground">
                                        {enabled ? '开启' : '关闭'}
                                    </span>
                                </div>
                            </Row>
                            {enabled && (
                                <>
                                    <Row label="每月日期">
                                        <Input className="h-8 max-w-[180px]" value={days as string}
                                            onChange={(e) => set('cron_days', e.target.value)}
                                            placeholder="1,16" />
                                        <span className="text-xs text-muted-foreground ml-2">
                                            逗号分隔 · 1-28
                                        </span>
                                    </Row>
                                    <Row label="执行时间">
                                        <div className="flex items-center gap-2">
                                            <Input className="h-8 w-20" type="number" min={0} max={23}
                                                value={hour}
                                                onChange={(e) => set('cron_hour', Number(e.target.value || 0))} />
                                            <span className="text-xs text-muted-foreground">点 (0-23 · 北京时间)</span>
                                        </div>
                                    </Row>
                                </>
                            )}
                        </div>
                    );
                }}
            />

            {/* 2. 预算限制 */}
            <SectionCard
                icon={<Wallet className="w-4 h-4" />}
                title="预算限制"
                subtitle="超过预算会阻止新跑批 · 不影响已入库文章"
                effectNote="立即生效"
                onSaveBatch={async (changes) => {
                    for (const [k, v] of Object.entries(changes)) await saveOne(k, v);
                }}
                onSavedRefresh={fetchList}
                renderView={() => (
                    <div className="space-y-2 text-sm">
                        <Row label="单轮预算上限">
                            ¥ {findValue<number>(items, 'budget_per_round_yuan', 350)}
                        </Row>
                        <Row label="月度预算上限">
                            ¥ {findValue<number>(items, 'budget_per_month_yuan', 1000)}
                        </Row>
                    </div>
                )}
                renderEdit={(draft, set) => {
                    const round = (draft.budget_per_round_yuan as number) ??
                        findValue<number>(items, 'budget_per_round_yuan', 350);
                    const month = (draft.budget_per_month_yuan as number) ??
                        findValue<number>(items, 'budget_per_month_yuan', 1000);
                    return (
                        <div className="space-y-2 text-sm">
                            <Row label="单轮预算上限">
                                <div className="flex items-center gap-1">
                                    <span>¥</span>
                                    <Input className="h-8 w-32" type="number" min={1} value={round}
                                        onChange={(e) => set('budget_per_round_yuan', Number(e.target.value || 0))} />
                                </div>
                            </Row>
                            <Row label="月度预算上限">
                                <div className="flex items-center gap-1">
                                    <span>¥</span>
                                    <Input className="h-8 w-32" type="number" min={1} value={month}
                                        onChange={(e) => set('budget_per_month_yuan', Number(e.target.value || 0))} />
                                </div>
                            </Row>
                        </div>
                    );
                }}
            />

            {/* 3. 入库与保留 */}
            <SectionCard
                icon={<Library className="w-4 h-4" />}
                title="入库与保留"
                subtitle="文章库门槛 + OSS 原文保留时长 + 编辑锁释放"
                effectNote="立即生效"
                onSaveBatch={async (changes) => {
                    for (const [k, v] of Object.entries(changes)) await saveOne(k, v);
                }}
                onSavedRefresh={fetchList}
                renderView={() => (
                    <div className="space-y-2 text-sm">
                        <Row label="入库门槛">
                            {findValue<number>(items, 'article_min_chars_for_review', 100)} 字
                            <span className="text-xs text-muted-foreground ml-2">清洗后正文不达此长度自动跳过</span>
                        </Row>
                        <Row label="原文保留">
                            {findValue<number>(items, 'article_oss_ttl_days', 180)} 天
                        </Row>
                        <Row label="编辑锁释放">
                            {findValue<number>(items, 'lock_stale_minutes', 5)} 分钟
                        </Row>
                    </div>
                )}
                renderEdit={(draft, set) => {
                    const minc = (draft.article_min_chars_for_review as number) ??
                        findValue<number>(items, 'article_min_chars_for_review', 100);
                    const ttl = (draft.article_oss_ttl_days as number) ??
                        findValue<number>(items, 'article_oss_ttl_days', 180);
                    const lock = (draft.lock_stale_minutes as number) ??
                        findValue<number>(items, 'lock_stale_minutes', 5);
                    return (
                        <div className="space-y-2 text-sm">
                            <Row label="入库门槛">
                                <div className="flex items-center gap-2">
                                    <Input className="h-8 w-28" type="number" min={50} value={minc}
                                        onChange={(e) => set('article_min_chars_for_review', Number(e.target.value || 0))} />
                                    <span className="text-xs text-muted-foreground">字</span>
                                </div>
                            </Row>
                            <Row label="原文保留">
                                <div className="flex items-center gap-2">
                                    <Input className="h-8 w-28" type="number" min={1} max={3650} value={ttl}
                                        onChange={(e) => set('article_oss_ttl_days', Number(e.target.value || 0))} />
                                    <span className="text-xs text-muted-foreground">天</span>
                                </div>
                            </Row>
                            <Row label="编辑锁释放">
                                <div className="flex items-center gap-2">
                                    <Input className="h-8 w-28" type="number" min={1} max={60} value={lock}
                                        onChange={(e) => set('lock_stale_minutes', Number(e.target.value || 0))} />
                                    <span className="text-xs text-muted-foreground">分钟</span>
                                </div>
                            </Row>
                        </div>
                    );
                }}
            />

            {/* 4. 高级 · 模型配置 (默认折叠) */}
            <CollapsibleSection
                icon={<Brain className="w-4 h-4" />}
                title="高级 · 模型配置"
                subtitle="4 个 AI 引擎的底层模型名 · 通常不改"
                defaultOpen={false}
            >
                <SectionCard
                    flat
                    effectNote="下次跑批生效"
                    onSaveBatch={async (changes) => {
                        for (const [k, v] of Object.entries(changes)) await saveOne(k, v);
                    }}
                    onSavedRefresh={fetchList}
                    renderView={() => (
                        <div className="space-y-2 text-sm">
                            <ModelRow name="豆包" value={findValue<string>(items, 'model_doubao_app', '')} />
                            <ModelRow name="DeepSeek" value={findValue<string>(items, 'model_deepseek_via_dashscope', '')} />
                            <ModelRow name="Qwen" value={findValue<string>(items, 'model_qwen_default', '')} />
                            <ModelRow name="Kimi" value={findValue<string>(items, 'model_kimi_via_dashscope', '')} />
                            <div className="text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded p-2 mt-2">
                                ⚠️ 这些是模型名,不是 API Key。API Key 在服务器 .env / 部署环境里配置。
                            </div>
                        </div>
                    )}
                    renderEdit={(draft, set) => {
                        const fields: Array<[string, string, string]> = [
                            ['model_doubao_app', '豆包', 'doubao-seed-2-0-lite-260215'],
                            ['model_deepseek_via_dashscope', 'DeepSeek', 'deepseek-v4-flash'],
                            ['model_qwen_default', 'Qwen', 'qwen-plus-latest'],
                            ['model_kimi_via_dashscope', 'Kimi', 'kimi/kimi-k2.6'],
                        ];
                        return (
                            <div className="space-y-2 text-sm">
                                {fields.map(([key, name, placeholder]) => {
                                    const cur = (draft[key] as string) ?? findValue<string>(items, key, '');
                                    return (
                                        <Row key={key} label={name}>
                                            <Input className="h-8 font-mono text-xs max-w-md"
                                                value={cur} placeholder={placeholder}
                                                onChange={(e) => set(key, e.target.value)} />
                                        </Row>
                                    );
                                })}
                            </div>
                        );
                    }}
                />
            </CollapsibleSection>

            {/* 5. 高级 · 失败保护 (默认折叠) */}
            <CollapsibleSection
                icon={<Shield className="w-4 h-4" />}
                title="高级 · 失败保护"
                subtitle="熔断阈值 · 跑批失败率过高时自动停止 · 通常不改"
                defaultOpen={false}
            >
                <SectionCard
                    flat
                    effectNote="立即生效"
                    onSaveBatch={async (changes) => {
                        for (const [k, v] of Object.entries(changes)) await saveOne(k, v);
                    }}
                    onSavedRefresh={fetchList}
                    renderView={() => (
                        <div className="space-y-2 text-sm">
                            <Row label="连续失败熔断">
                                {findValue<number>(items, 'circuit_breaker_consecutive', 50)} 次
                                <span className="text-xs text-muted-foreground ml-2">连续 N 次失败立即停止</span>
                            </Row>
                            <Row label="起算样本">
                                {findValue<number>(items, 'circuit_breaker_min_processed', 100)} 次
                                <span className="text-xs text-muted-foreground ml-2">至少跑了 N 次才计算失败率</span>
                            </Row>
                            <Row label="失败率阈值">
                                {Math.round(findValue<number>(items, 'circuit_breaker_rate', 0.5) * 100)} %
                            </Row>
                        </div>
                    )}
                    renderEdit={(draft, set) => {
                        const cons = (draft.circuit_breaker_consecutive as number) ??
                            findValue<number>(items, 'circuit_breaker_consecutive', 50);
                        const minp = (draft.circuit_breaker_min_processed as number) ??
                            findValue<number>(items, 'circuit_breaker_min_processed', 100);
                        const rate = (draft.circuit_breaker_rate as number) ??
                            findValue<number>(items, 'circuit_breaker_rate', 0.5);
                        return (
                            <div className="space-y-2 text-sm">
                                <Row label="连续失败熔断">
                                    <Input className="h-8 w-24" type="number" min={1} value={cons}
                                        onChange={(e) => set('circuit_breaker_consecutive', Number(e.target.value || 0))} />
                                </Row>
                                <Row label="起算样本">
                                    <Input className="h-8 w-24" type="number" min={1} value={minp}
                                        onChange={(e) => set('circuit_breaker_min_processed', Number(e.target.value || 0))} />
                                </Row>
                                <Row label="失败率阈值">
                                    <div className="flex items-center gap-2">
                                        <Input className="h-8 w-24" type="number" min={1} max={100} step={1}
                                            value={Math.round((rate as number) * 100)}
                                            onChange={(e) => set('circuit_breaker_rate',
                                                Math.min(1, Math.max(0, Number(e.target.value || 0) / 100)))} />
                                        <span className="text-xs text-muted-foreground">%</span>
                                    </div>
                                </Row>
                            </div>
                        );
                    }}
                />
            </CollapsibleSection>

            {/* 重置 Dialog */}
            <Dialog open={resetOpen} onOpenChange={setResetOpen}>
                <DialogContent className="max-w-md">
                    <DialogHeader>
                        <DialogTitle className="text-destructive flex items-center gap-2">
                            <AlertTriangle className="w-5 h-5" />
                            恢复全部默认
                        </DialogTitle>
                        <DialogDescription>
                            把所有配置(自动跑批/预算/入库/模型/熔断)还原系统默认值 · 不可逆
                        </DialogDescription>
                    </DialogHeader>
                    <div className="space-y-3">
                        <div className="rounded-md bg-amber-50 border border-amber-200 p-3 text-sm text-amber-900">
                            请输入下方完整字符串确认:
                            <pre className="mt-1 font-mono text-xs select-all">{RESET_CONFIRM_CODE}</pre>
                        </div>
                        <Input value={resetCodeInput} onChange={(e) => setResetCodeInput(e.target.value)}
                            placeholder="请输入 confirm_code"
                            className="font-mono" autoComplete="off" spellCheck={false} />
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setResetOpen(false)} disabled={resetSubmitting}>
                            取消
                        </Button>
                        <Button variant="destructive" onClick={() => void submitReset()}
                            disabled={resetSubmitting || resetCodeInput !== RESET_CONFIRM_CODE}>
                            {resetSubmitting ? <Loader2 className="w-3 h-3 animate-spin mr-1" /> : null}
                            我已知风险 · 立即恢复
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}

// =========================================================================
// 子组件
// =========================================================================

function Row({ label, children }: { label: string; children: React.ReactNode }) {
    return (
        <div className="flex items-center gap-3">
            <span className="text-xs text-muted-foreground w-28 shrink-0">{label}</span>
            <div className="flex items-center flex-wrap gap-1">{children}</div>
        </div>
    );
}

function ModelRow({ name, value }: { name: string; value: string }) {
    return (
        <div className="flex items-center gap-3 text-sm">
            <span className="text-xs text-muted-foreground w-20 shrink-0">{name}</span>
            <span className="font-mono text-xs">{value || '-'}</span>
        </div>
    );
}

interface SectionCardProps {
    icon?: React.ReactNode;
    title?: string;
    subtitle?: string;
    effectNote: string;
    flat?: boolean;
    renderView: () => React.ReactNode;
    renderEdit: (draft: Record<string, unknown>, set: (k: string, v: unknown) => void) => React.ReactNode;
    onSaveBatch: (changes: Record<string, unknown>) => Promise<void>;
    onSavedRefresh: () => void | Promise<void>;
}

function SectionCard({
    icon, title, subtitle, effectNote, flat,
    renderView, renderEdit, onSaveBatch, onSavedRefresh,
}: SectionCardProps) {
    const [editing, setEditing] = useState(false);
    const [draft, setDraft] = useState<Record<string, unknown>>({});
    const [saving, setSaving] = useState(false);

    const onSet = (k: string, v: unknown) => setDraft(d => ({ ...d, [k]: v }));

    const onSave = async () => {
        if (Object.keys(draft).length === 0) {
            // 无改动 · 直接退出编辑
            setEditing(false);
            return;
        }
        setSaving(true);
        try {
            await onSaveBatch(draft);
            toast.success(`已保存 · ${effectNote}`);
            setDraft({});
            setEditing(false);
            await onSavedRefresh();
        } catch (e) {
            toast.error(formatApiErrorForDisplay(e, '保存失败', 'admin'));
        } finally {
            setSaving(false);
        }
    };

    const onCancel = () => {
        setDraft({});
        setEditing(false);
    };

    const body = (
        <div className="space-y-3">
            {(title || subtitle) && (
                <div className="flex items-start justify-between gap-3">
                    <div className="flex items-start gap-2">
                        {icon && <div className="mt-0.5 text-muted-foreground">{icon}</div>}
                        <div>
                            {title && <h3 className="text-sm font-semibold">{title}</h3>}
                            {subtitle && <p className="text-xs text-muted-foreground mt-0.5">{subtitle}</p>}
                        </div>
                    </div>
                    {!editing && (
                        <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
                            <Pencil className="w-3 h-3 mr-1" />编辑
                        </Button>
                    )}
                </div>
            )}
            {!editing && renderView()}
            {editing && (
                <>
                    {renderEdit(draft, onSet)}
                    <div className="flex justify-end gap-2 pt-2 border-t">
                        <Button size="sm" variant="outline" onClick={onCancel} disabled={saving}>取消</Button>
                        <Button size="sm" onClick={() => void onSave()} disabled={saving}>
                            {saving ? <Loader2 className="w-3 h-3 animate-spin mr-1" /> : null}
                            保存
                        </Button>
                    </div>
                </>
            )}
            {flat && !title && !editing && (
                <div className="flex justify-end">
                    <Button size="sm" variant="ghost" onClick={() => setEditing(true)}>
                        <Pencil className="w-3 h-3 mr-1" />编辑
                    </Button>
                </div>
            )}
        </div>
    );

    if (flat) return <div>{body}</div>;
    return (
        <Card>
            <CardContent className="pt-4 pb-4">{body}</CardContent>
        </Card>
    );
}

function CollapsibleSection({
    icon, title, subtitle, defaultOpen = false, children,
}: {
    icon?: React.ReactNode;
    title: string;
    subtitle?: string;
    defaultOpen?: boolean;
    children: React.ReactNode;
}) {
    const [open, setOpen] = useState(defaultOpen);
    return (
        <Card>
            <CardContent className="pt-4 pb-4 space-y-3">
                <button
                    type="button"
                    onClick={() => setOpen(o => !o)}
                    className="w-full text-left flex items-start gap-2 hover:bg-muted/20 -mx-2 px-2 py-1 rounded"
                >
                    <div className="mt-0.5 text-muted-foreground">
                        {open ? <ChevronDown className="w-4 h-4" /> : <ChevronRight className="w-4 h-4" />}
                    </div>
                    {icon && <div className="mt-0.5 text-muted-foreground">{icon}</div>}
                    <div className="flex-1">
                        <h3 className="text-sm font-semibold">{title}</h3>
                        {subtitle && <p className="text-xs text-muted-foreground mt-0.5">{subtitle}</p>}
                    </div>
                </button>
                {open && <div className="pt-1 pl-6 border-t">{children}</div>}
            </CardContent>
        </Card>
    );
}

// noop · 防 unused-import 警告 (保留 useMemo import 给后续扩展用)
const _useMemo = useMemo;
void _useMemo;
