/**
 * 公开创作者画像卡 — 不需要登录
 * 路由: /public/creator/:shareCode
 */
import { useState, useEffect } from 'react';
import { useParams, Link } from 'react-router-dom';
import { Loader2, Sparkles, Users } from 'lucide-react';

const DNA_LABELS = [
    { key: 'camera_comfort', left: '幕后型', right: '出镜型', icon: '📷' },
    { key: 'update_pace', left: '精品型', right: '量产型', icon: '⚡' },
    { key: 'interaction_style', left: '内容型', right: '社交型', icon: '💬' },
    { key: 'content_depth', left: '轻量型', right: '深度型', icon: '📚' },
];

const RADAR_LABELS: Record<string, string> = {
    professional: '专业性', expression: '表达力', storytelling: '故事力',
    empathy: '共情力', business: '商业思维', creativity: '创造力',
};

export default function SharedProfile() {
    const { shareCode } = useParams<{ shareCode: string }>();
    const [card, setCard] = useState<any>(null);
    const [loading, setLoading] = useState(true);

    useEffect(() => {
        if (!shareCode) return;
        fetch(`/api/public/creator-card/${shareCode}`)
            .then(r => r.json())
            .then(data => { if (data.status === 'success') setCard(data.card); })
            .finally(() => setLoading(false));
    }, [shareCode]);

    if (loading) return <div className="min-h-screen bg-background flex justify-center items-center"><Loader2 className="h-8 w-8 animate-spin text-muted-foreground" /></div>;
    if (!card) return <div className="min-h-screen bg-background flex justify-center items-center text-muted-foreground">画像不存在</div>;

    return (
        <div className="min-h-screen bg-background">
            <div className="max-w-md mx-auto p-6 space-y-6">
                <div className="text-center space-y-2">
                    <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-primary to-purple-500 flex items-center justify-center text-white text-2xl font-bold mx-auto">
                        {card.name?.charAt(0) || '?'}
                    </div>
                    <h1 className="text-xl font-bold text-foreground">{card.name}</h1>
                    <p className="text-sm text-primary font-medium">{card.creator_type || '创作者'}</p>
                    {card.industry && <p className="text-xs text-muted-foreground">{card.industry}</p>}
                </div>

                {card.soul_tags?.length > 0 && (
                    <div className="flex flex-wrap justify-center gap-2">
                        {card.soul_tags.map((tag: string, i: number) => (
                            <span key={i} className="px-3 py-1 rounded-full text-xs font-medium bg-muted text-foreground">{tag}</span>
                        ))}
                    </div>
                )}

                {card.ip_declaration && (
                    <p className="text-center text-sm text-muted-foreground italic">"{card.ip_declaration}"</p>
                )}

                {card.creator_dna && (
                    <div className="rounded-xl border border-border bg-card p-4 space-y-3">
                        <h3 className="text-sm font-semibold text-foreground">创作者 DNA</h3>
                        {DNA_LABELS.map(dim => {
                            const val = card.creator_dna[dim.key]?.value || 50;
                            return (
                                <div key={dim.key} className="space-y-1">
                                    <div className="flex justify-between text-xs text-muted-foreground">
                                        <span>{dim.icon} {dim.left}</span><span>{dim.right}</span>
                                    </div>
                                    <div className="h-2 bg-muted rounded-full overflow-hidden">
                                        <div className="h-full bg-primary rounded-full" style={{ width: `${val}%` }} />
                                    </div>
                                </div>
                            );
                        })}
                    </div>
                )}

                {card.radar && (
                    <div className="rounded-xl border border-border bg-card p-4 space-y-2">
                        <h3 className="text-sm font-semibold text-foreground">能力雷达</h3>
                        {Object.entries(RADAR_LABELS).map(([dim, label]) => {
                            const score = typeof card.radar[dim] === 'object' ? card.radar[dim]?.score : (card.radar[dim] || 0);
                            return (
                                <div key={dim} className="flex items-center gap-2 text-xs">
                                    <span className="w-16 text-muted-foreground shrink-0">{label}</span>
                                    <div className="flex-1 h-1.5 bg-muted rounded-full overflow-hidden">
                                        <div className="h-full bg-primary rounded-full" style={{ width: `${score}%` }} />
                                    </div>
                                    <span className="w-8 text-right font-medium text-foreground">{score}</span>
                                </div>
                            );
                        })}
                    </div>
                )}

                <div className="space-y-3 pt-4">
                    <Link to="/register" className="flex items-center justify-center gap-2 w-full rounded-xl bg-foreground text-background py-3.5 text-sm font-medium hover:opacity-90">
                        <Sparkles size={16} /> 测测你是什么类型的创作者
                    </Link>
                    <Link to="/login" className="flex items-center justify-center gap-2 w-full rounded-xl border border-border py-3.5 text-sm font-medium text-foreground hover:bg-muted">
                        <Users size={16} /> 已有账号？匹配我们的默契度
                    </Link>
                </div>

                <p className="text-center text-[10px] text-muted-foreground pt-4">由 AI 分析生成</p>
            </div>
        </div>
    );
}
