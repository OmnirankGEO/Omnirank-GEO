/**
 * GEO 调研抓取 LLM 配置卡片 [P12 · 2026-05-26]
 *
 * 历史背景:
 *   P09 时把 4 个平台模型名(豆包/DeepSeek/千问/Kimi)加到了 geo_research_config 表
 *   但只在"调研后台 → 系统配置 tab"(/admin/research-monitor)展示
 *   老板找不到 · 期望在 /settings 系统设置页能直接看到改
 *
 * 数据源(单 source of truth · 不引入新存储):
 *   GET  /api/admin/research-monitor/config              → 取全部 config 行
 *   PUT  /api/admin/research-monitor/config/{key}        → 改单个 (有 _require_admin 校验)
 *
 *   关心 4 个 keys (跟 platforms.py 一致):
 *     model_doubao_app                = "doubao-seed-2-0-lite-260215"
 *     model_deepseek_via_dashscope    = "deepseek-v4-flash"
 *     model_qwen_default              = "qwen-plus-latest"
 *     model_kimi_via_dashscope        = "kimi/kimi-k2.6"
 *
 * UI 格式 (P12 v2 · 跟 SettingsPage renderTaskLLMConfig 对齐):
 *   - task box: p-4 border rounded-lg space-y-3 bg-muted/50
 *   - 顶部 icon + 平台名 + desc
 *   - 两列 grid: 服务商 (disabled Select · 跟后端 platforms.py 写死) + 模型名称 (Input)
 *
 * 跟 SettingsPage 主体保存按钮解耦:
 *   - SettingsPage 主体保存走 settings.json
 *   - 本组件保存走 geo_research_config DB · 独立按钮
 *   - 防混淆: 改这 4 个模型不影响 AI员工/监测中心配置
 */
import { useState, useEffect, useCallback } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import {
    Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';
import { FlaskConical, Save, Loader2, RotateCcw, AlertCircle, Cpu, Search, Sparkles, Zap } from 'lucide-react';
import { authFetch } from '@/lib/api';

// 4 平台配置 (跟后端 platforms.py _load_model_from_config 调用对齐)
// provider_id 跟 SettingsPage 顶部 PROVIDERS 数组的 id 对齐 (用 SelectItem 显示同名)
const PLATFORMS: Array<{
    key: string;
    name: string;
    desc: string;
    icon: React.ComponentType<{ className?: string }>;
    provider_id: string;
    provider_label: string;
    placeholder: string;
}> = [
    {
        key: 'model_doubao_app',
        name: '豆包',
        desc: 'doubao_app + ai_search · 火山方舟原生联网 ¥0.20/次',
        icon: Sparkles,
        provider_id: 'doubao',
        provider_label: '火山方舟 (字节跳动)',
        placeholder: 'doubao-seed-2-0-lite-260215',
    },
    {
        key: 'model_deepseek_via_dashscope',
        name: 'DeepSeek',
        desc: '走阿里百炼 generation API · 火山方舟尚未上线 V4',
        icon: Cpu,
        provider_id: 'dashscope',
        provider_label: 'DashScope (阿里云)',
        placeholder: 'deepseek-v4-flash',
    },
    {
        key: 'model_qwen_default',
        name: '千问',
        desc: 'DashScope 原生 generation · enable_search=True',
        icon: Search,
        provider_id: 'dashscope',
        provider_label: 'DashScope (阿里云)',
        placeholder: 'qwen-plus-latest',
    },
    {
        key: 'model_kimi_via_dashscope',
        name: 'Kimi',
        desc: '走阿里百炼 OpenAI 兼容模式 · builtin_function $web_search',
        icon: Zap,
        provider_id: 'dashscope',
        provider_label: 'DashScope (阿里云)',
        placeholder: 'kimi/kimi-k2.6',
    },
];

interface ConfigRow {
    key: string;
    value: unknown;
    description: string;
    updated_by: string | null;
    updated_at: string | null;
}

export function GeoResearchExtractLLMCard() {
    const [original, setOriginal] = useState<Record<string, string>>({});   // 初始值 · 比对脏不脏
    const [edited, setEdited] = useState<Record<string, string>>({});       // 输入框当前值
    const [loading, setLoading] = useState(true);
    const [saving, setSaving] = useState(false);
    const [loadError, setLoadError] = useState<string | null>(null);
    const [saveStatus, setSaveStatus] = useState<{ ok: boolean; msg: string } | null>(null);

    const load = useCallback(async () => {
        setLoading(true);
        setLoadError(null);
        try {
            const res = await authFetch('/api/admin/research-monitor/config');
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            const data = await res.json();
            const rows: ConfigRow[] = data.configs ?? [];
            const next: Record<string, string> = {};
            for (const p of PLATFORMS) {
                const row = rows.find(r => r.key === p.key);
                // value_json 已被后端 _serialize_row 反序列化成 JSON-native 类型
                next[p.key] = typeof row?.value === 'string' ? row.value : (row?.value != null ? String(row.value) : '');
            }
            setOriginal(next);
            setEdited({ ...next });
        } catch (e) {
            setLoadError((e as Error).message || '加载失败');
        } finally {
            setLoading(false);
        }
    }, []);

    useEffect(() => { load(); }, [load]);

    const dirty = PLATFORMS.some(p => (edited[p.key] ?? '') !== (original[p.key] ?? ''));

    const save = async () => {
        setSaving(true);
        setSaveStatus(null);
        // 仅 PUT 真正变化的 key · 减少噪音 · 一个失败不影响其他
        const changes = PLATFORMS.filter(p => (edited[p.key] ?? '') !== (original[p.key] ?? ''));
        let okCount = 0;
        const failures: string[] = [];
        for (const p of changes) {
            const value = (edited[p.key] ?? '').trim();
            if (!value) {
                failures.push(`${p.name}: 不能为空`);
                continue;
            }
            try {
                const res = await authFetch(`/api/admin/research-monitor/config/${encodeURIComponent(p.key)}`, {
                    method: 'PUT',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ value }),
                });
                if (!res.ok) {
                    const body = await res.json().catch(() => ({ detail: `HTTP ${res.status}` }));
                    failures.push(`${p.name}: ${body.detail ?? body.message ?? `HTTP ${res.status}`}`);
                    continue;
                }
                okCount += 1;
            } catch (e) {
                failures.push(`${p.name}: ${(e as Error).message}`);
            }
        }
        setSaving(false);
        if (failures.length === 0) {
            setSaveStatus({ ok: true, msg: `✓ 已保存 ${okCount} 项 · 调研抓取下次跑批立即生效` });
            await load();  // reload baseline + updated_at 同步
        } else {
            setSaveStatus({
                ok: false,
                msg: `部分失败 (${okCount}/${changes.length} 成功)\n` + failures.join('\n'),
            });
        }
    };

    const reset = () => setEdited({ ...original });

    return (
        <Card>
            <CardHeader>
                <CardTitle className="flex items-center gap-2">
                    <FlaskConical className="h-5 w-5 text-cyan-500" />
                    GEO 调研抓取 LLM
                </CardTitle>
                <CardDescription>
                    GEO 调研后台 4 平台抓取使用的模型 (改完下次跑批立即生效) · 服务商由 platforms.py 写死不可改
                </CardDescription>
            </CardHeader>
            <CardContent className="space-y-4">
                {loading && (
                    <div className="flex items-center justify-center py-8 text-sm text-muted-foreground">
                        <Loader2 className="h-4 w-4 animate-spin mr-2" />加载 geo_research_config...
                    </div>
                )}

                {loadError && (
                    <div className="text-sm bg-red-50 text-red-700 px-3 py-2 rounded-md flex items-start gap-2">
                        <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" />
                        <div>
                            <p className="font-medium">加载失败: {loadError}</p>
                            <p className="text-xs mt-1 text-red-600">
                                需要管理员身份 · GET /api/admin/research-monitor/config 返回非 200
                            </p>
                        </div>
                    </div>
                )}

                {!loading && !loadError && (
                    <>
                        {/* task boxes · 跟 SettingsPage.renderTaskLLMConfig 同款 */}
                        <div className="space-y-3">
                            {PLATFORMS.map(p => {
                                const IconComponent = p.icon;
                                const isDirty = (edited[p.key] ?? '') !== (original[p.key] ?? '');
                                return (
                                    <div key={p.key} className="p-4 border rounded-lg space-y-3 bg-muted/50">
                                        <div className="flex items-center gap-2">
                                            <IconComponent className="h-4 w-4 text-brand" />
                                            <span className="font-medium">{p.name}</span>
                                            <span className="text-xs text-muted-foreground">- {p.desc}</span>
                                            {isDirty && (
                                                <Badge className="ml-auto bg-amber-100 text-amber-700 hover:bg-amber-100 text-[10px]">
                                                    未保存
                                                </Badge>
                                            )}
                                        </div>
                                        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                                            <div className="space-y-1">
                                                <Label className="text-xs">服务商</Label>
                                                <Select value={p.provider_id} disabled>
                                                    <SelectTrigger className="h-9">
                                                        <SelectValue>{p.provider_label}</SelectValue>
                                                    </SelectTrigger>
                                                    <SelectContent>
                                                        <SelectItem value={p.provider_id}>{p.provider_label}</SelectItem>
                                                    </SelectContent>
                                                </Select>
                                            </div>
                                            <div className="space-y-1">
                                                <Label className="text-xs">模型名称</Label>
                                                <Input
                                                    className="h-9"
                                                    value={edited[p.key] ?? ''}
                                                    onChange={e => setEdited(prev => ({ ...prev, [p.key]: e.target.value }))}
                                                    placeholder={p.placeholder}
                                                />
                                            </div>
                                        </div>
                                    </div>
                                );
                            })}
                        </div>

                        {/* 保存反馈 */}
                        {saveStatus && (
                            <div className={`text-xs px-3 py-2 rounded-md whitespace-pre-wrap ${
                                saveStatus.ok ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-700'
                            }`}>
                                {saveStatus.msg}
                            </div>
                        )}

                        {/* 独立保存/重置 · 跟 SettingsPage 主保存按钮解耦 (改这里走 geo_research_config 而不是 settings.json) */}
                        <div className="flex items-center justify-end gap-2 pt-1 border-t border-border/50">
                            <Button
                                variant="ghost"
                                size="sm"
                                onClick={reset}
                                disabled={!dirty || saving}
                            >
                                <RotateCcw className="h-3.5 w-3.5 mr-1.5" />重置
                            </Button>
                            <Button
                                size="sm"
                                onClick={save}
                                disabled={!dirty || saving}
                            >
                                {saving
                                    ? <Loader2 className="h-3.5 w-3.5 animate-spin mr-1.5" />
                                    : <Save className="h-3.5 w-3.5 mr-1.5" />}
                                {saving ? '保存中...' : '保存调研模型'}
                            </Button>
                        </div>
                    </>
                )}
            </CardContent>
        </Card>
    );
}
