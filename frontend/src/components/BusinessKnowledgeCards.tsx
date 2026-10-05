/**
 * 业务知识卡片组件 - 结构化展示5大维度
 * 📦 我们卖什么 | 😣 解决什么问题 | 👥 客户是谁 | ⚔️ 为什么选我们 | 📣 成功案例
 *
 * 功能：
 * - 5维度卡片展示
 * - 完成度指示器
 * - 意见框输入 + AI更新
 * - 单字段编辑
 */

import React, { useState } from 'react';
import { cn } from '@/lib/utils';

// 维度配置 - 使用Tailwind类名代替硬编码颜色
const DIMENSIONS = [
    {
        key: 'products', icon: '📦', title: '我们卖什么',
        fields: ['name', 'features', 'pricing', 'scenarios', 'metrics'],
        classes: { bg: 'bg-blue-500', border: 'border-blue-500', barBg: 'bg-blue-500', leftBorder: 'border-l-blue-500' },
    },
    {
        key: 'painPoints', icon: '😣', title: '解决什么问题',
        fields: ['scenario', 'emotions', 'consequences', 'triggers'],
        classes: { bg: 'bg-orange-500', border: 'border-orange-500', barBg: 'bg-orange-500', leftBorder: 'border-l-orange-500' },
    },
    {
        key: 'customers', icon: '👥', title: '客户是谁',
        fields: ['segments', 'needs', 'barriers', 'concerns'],
        classes: { bg: 'bg-violet-500', border: 'border-violet-500', barBg: 'bg-violet-500', leftBorder: 'border-l-violet-500' },
    },
    {
        key: 'differentiation', icon: '⚔️', title: '为什么选我们',
        fields: ['competitors', 'advantages', 'usp', 'killerData'],
        classes: { bg: 'bg-green-500', border: 'border-green-500', barBg: 'bg-green-500', leftBorder: 'border-l-green-500' },
    },
    {
        key: 'cases', icon: '📣', title: '成功案例',
        fields: ['client', 'background', 'solution', 'results', 'quote'],
        classes: { bg: 'bg-pink-500', border: 'border-pink-500', barBg: 'bg-pink-500', leftBorder: 'border-l-pink-500' },
    },
];

// 字段标签映射
const FIELD_LABELS: Record<string, string> = {
    name: '产品/服务名称', features: '核心功能', pricing: '定价区间', scenarios: '使用场景', metrics: '效果数据',
    scenario: '痛点场景', emotions: '情绪关键词', consequences: '不解决的后果', triggers: '购买触发时刻',
    segments: '客户细分', needs: '核心诉求', barriers: '决策障碍', concerns: '最关心的问题',
    competitors: '竞品名单', advantages: 'vs竞品优势', usp: '独特卖点(USP)', killerData: '杀手级数据',
    client: '客户名称/行业', background: '问题背景', solution: '解决方案', results: '效果数据', quote: '客户评价原话',
};

interface BusinessKnowledge {
    products?: { name?: string; features?: string[]; pricing?: string; scenarios?: string[]; metrics?: string };
    painPoints?: { scenario?: string; emotions?: string[]; consequences?: string; triggers?: string };
    customers?: { segments?: string[]; needs?: string[]; barriers?: string[]; concerns?: string[] };
    differentiation?: { competitors?: string[]; advantages?: string[]; usp?: string; killerData?: string[] };
    cases?: { client?: string; background?: string; solution?: string; results?: string; quote?: string }[];
}

interface Props {
    data: BusinessKnowledge;
    onUpdate?: (newData: BusinessKnowledge) => void;
    onFeedbackUpdate?: (feedback: string, currentData: BusinessKnowledge) => Promise<BusinessKnowledge | null>;
    editable?: boolean;
    advisorId?: string;
}

export const BusinessKnowledgeCards: React.FC<Props> = ({ data, onUpdate, onFeedbackUpdate, editable = false }) => {
    const [expandedCard, setExpandedCard] = useState<string | null>(null);
    const [editingField, setEditingField] = useState<{ dim: string; field: string; value: string } | null>(null);
    const [feedback, setFeedback] = useState('');
    const [isUpdating, setIsUpdating] = useState(false);

    const renderFieldValue = (value: unknown): string => {
        if (!value) return '—';
        if (Array.isArray(value)) return value.join('、');
        return String(value);
    };

    const getCompleteness = (dimKey: string): number => {
        const dimData = data[dimKey as keyof BusinessKnowledge];
        if (!dimData) return 0;
        if (Array.isArray(dimData)) return dimData.length > 0 ? 100 : 0;
        const dim = DIMENSIONS.find(d => d.key === dimKey);
        if (!dim) return 0;
        const filledFields = dim.fields.filter(f => {
            const val = (dimData as Record<string, unknown>)[f];
            return val && (Array.isArray(val) ? val.length > 0 : String(val).trim() !== '');
        });
        return Math.round((filledFields.length / dim.fields.length) * 100);
    };

    // 处理意见更新
    const handleFeedbackUpdate = async () => {
        if (!feedback.trim() || !onFeedbackUpdate) return;
        setIsUpdating(true);
        try {
            const result = await onFeedbackUpdate(feedback, data);
            if (result) {
                setFeedback('');
            }
        } finally {
            setIsUpdating(false);
        }
    };

    // 保存单字段编辑
    const handleSaveField = () => {
        if (!editingField || !onUpdate) return;
        const { dim, field, value } = editingField;
        const newData = { ...data };

        if (dim === 'cases') {
            const cases = [...(data.cases || [])];
            if (cases[0]) {
                cases[0] = { ...cases[0], [field]: value };
            }
            newData.cases = cases;
        } else {
            const dimData = data[dim as keyof BusinessKnowledge] || {};
            const isArrayField = ['features', 'scenarios', 'emotions', 'segments', 'needs', 'barriers', 'concerns', 'competitors', 'advantages', 'killerData'].includes(field);
            (newData as Record<string, unknown>)[dim] = {
                ...dimData,
                [field]: isArrayField ? value.split(/[,;，；、]/).map(s => s.trim()).filter(Boolean) : value,
            };
        }

        onUpdate(newData);
        setEditingField(null);
    };

    return (
        <div className="flex flex-col gap-3">
            {/* 标题 + 提示 */}
            <div className="flex justify-between items-center">
                <span className="font-semibold text-[15px] text-foreground">📚 业务知识库</span>
                <span className="text-xs text-muted-foreground">点击卡片展开详情</span>
            </div>

            {/* 意见框输入区域 */}
            {editable && onFeedbackUpdate && (
                <div className="flex gap-2 p-3 bg-amber-50 rounded-xl border border-amber-200">
                    <input
                        type="text"
                        value={feedback}
                        onChange={(e) => setFeedback(e.target.value)}
                        placeholder="输入修改意见，如：定价改为6000-20000元，添加教育行业案例..."
                        className="flex-1 px-3 py-2 border border-border rounded-md text-[13px] bg-background focus:outline-hidden focus:ring-2 focus:ring-ring"
                        onKeyDown={(e) => e.key === 'Enter' && handleFeedbackUpdate()}
                    />
                    <button
                        onClick={handleFeedbackUpdate}
                        disabled={isUpdating || !feedback.trim()}
                        className={cn(
                            "px-4 py-2 text-white border-none rounded-md text-[13px] whitespace-nowrap transition-colors",
                            isUpdating
                                ? "bg-muted-foreground/40 cursor-wait"
                                : "bg-brand hover:bg-brand-hover cursor-pointer"
                        )}
                    >
                        {isUpdating ? '🔄 更新中...' : '✨ AI更新'}
                    </button>
                </div>
            )}

            {/* 5维度卡片网格 */}
            <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-2.5">
                {DIMENSIONS.map(dim => {
                    const completeness = getCompleteness(dim.key);
                    const isExpanded = expandedCard === dim.key;
                    return (
                        <div
                            key={dim.key}
                            onClick={() => setExpandedCard(isExpanded ? null : dim.key)}
                            className={cn(
                                "px-3 py-3.5 rounded-xl border-2 cursor-pointer transition-all duration-200 text-center",
                                isExpanded
                                    ? `${dim.classes.bg} ${dim.classes.border} text-white`
                                    : `bg-card ${dim.classes.border.replace(dim.classes.border, 'border-border')} hover:border-foreground/20`
                            )}
                        >
                            <div className="text-2xl mb-1.5">{dim.icon}</div>
                            <div className={cn(
                                "text-[13px] font-semibold mb-2",
                                isExpanded ? "text-white" : "text-foreground"
                            )}>{dim.title}</div>
                            <div className={cn(
                                "h-1 rounded-full overflow-hidden",
                                isExpanded ? "bg-white/30" : "bg-border"
                            )}>
                                <div
                                    className={cn(
                                        "h-full transition-[width] duration-300",
                                        isExpanded ? "bg-white" : dim.classes.barBg
                                    )}
                                    style={{ width: `${completeness}%` }}
                                />
                            </div>
                            <div className={cn(
                                "text-[11px] mt-1",
                                isExpanded ? "text-white/80" : "text-muted-foreground"
                            )}>{completeness}% 已填写</div>
                        </div>
                    );
                })}
            </div>

            {/* 展开的详情区域 */}
            {expandedCard && (
                <div className="bg-secondary rounded-xl p-5 border border-border">
                    {(() => {
                        const dim = DIMENSIONS.find(d => d.key === expandedCard);
                        if (!dim) return null;
                        const dimData = data[expandedCard as keyof BusinessKnowledge];

                        // 特殊处理cases
                        if (expandedCard === 'cases') {
                            const cases = dimData as BusinessKnowledge['cases'];
                            if (!cases || cases.length === 0) {
                                return <div className="text-center text-muted-foreground py-5">暂无成功案例数据</div>;
                            }
                            return (
                                <div className="flex flex-col gap-4">
                                    {cases.map((caseItem, idx) => (
                                        <div key={idx} className={cn("bg-card p-4 rounded-xl border-l-4", dim.classes.leftBorder)}>
                                            <div className="font-semibold text-foreground mb-2">📌 {caseItem.client || `案例 ${idx + 1}`}</div>
                                            {dim.fields.filter(f => f !== 'client').map(field => (
                                                <div key={field} className="mb-1.5 flex items-center gap-2">
                                                    <span className="text-xs text-muted-foreground min-w-[80px]">{FIELD_LABELS[field]}：</span>
                                                    <span className="text-[13px] text-foreground flex-1">{renderFieldValue(caseItem[field as keyof typeof caseItem])}</span>
                                                    {editable && (
                                                        <button
                                                            onClick={(e) => { e.stopPropagation(); setEditingField({ dim: 'cases', field, value: String(caseItem[field as keyof typeof caseItem] || '') }); }}
                                                            className="text-[11px] text-brand hover:text-brand-hover bg-transparent border-none cursor-pointer px-1.5 py-0.5"
                                                        >✏️</button>
                                                    )}
                                                </div>
                                            ))}
                                        </div>
                                    ))}
                                </div>
                            );
                        }

                        // 常规维度
                        return (
                            <div className="grid grid-cols-2 gap-3">
                                {dim.fields.map(field => {
                                    const value = dimData ? (dimData as Record<string, unknown>)[field] : undefined;
                                    const displayValue = renderFieldValue(value);
                                    return (
                                        <div key={field} className="bg-card px-3.5 py-3 rounded-lg border border-border">
                                            <div className="flex justify-between items-center">
                                                <div className="text-xs text-muted-foreground">{FIELD_LABELS[field]}</div>
                                                {editable && (
                                                    <button
                                                        onClick={(e) => {
                                                            e.stopPropagation();
                                                            const strValue = Array.isArray(value) ? value.join('、') : String(value || '');
                                                            setEditingField({ dim: expandedCard, field, value: strValue });
                                                        }}
                                                        className="text-[11px] text-brand hover:text-brand-hover bg-transparent border-none cursor-pointer px-1.5 py-0.5"
                                                    >✏️</button>
                                                )}
                                            </div>
                                            <div className={cn(
                                                "text-sm mt-1",
                                                value ? "text-foreground font-medium" : "text-muted-foreground/40"
                                            )}>{displayValue}</div>
                                        </div>
                                    );
                                })}
                            </div>
                        );
                    })()}
                </div>
            )}

            {/* 编辑弹窗 */}
            {editingField && (
                <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-1000" onClick={() => setEditingField(null)}>
                    <div className="bg-card p-6 rounded-2xl w-[400px] max-w-[90%]" onClick={e => e.stopPropagation()}>
                        <div className="font-semibold text-base text-foreground mb-4">
                            ✏️ 编辑 - {FIELD_LABELS[editingField.field] || editingField.field}
                        </div>
                        <textarea
                            value={editingField.value}
                            onChange={(e) => setEditingField({ ...editingField, value: e.target.value })}
                            className="w-full min-h-[100px] p-3 border border-border rounded-lg text-sm resize-y bg-background text-foreground focus:outline-hidden focus:ring-2 focus:ring-ring"
                            placeholder="输入内容..."
                        />
                        <div className="text-xs text-muted-foreground mt-2">
                            💡 多个值用顿号（、）或逗号分隔
                        </div>
                        <div className="flex gap-3 mt-4 justify-end">
                            <button onClick={() => setEditingField(null)} className="px-4 py-2 bg-secondary border-none rounded-md cursor-pointer text-foreground hover:bg-secondary/80 transition-colors">取消</button>
                            <button onClick={handleSaveField} className="px-4 py-2 bg-brand text-white border-none rounded-md cursor-pointer hover:bg-brand-hover transition-colors">保存</button>
                        </div>
                    </div>
                </div>
            )}
        </div>
    );
};

export default BusinessKnowledgeCards;
