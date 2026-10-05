/**
 * 客户知识库卡 · 写文章与做 GEO 图文**共用同一个**
 *
 * 2026-08-03 · Owner:「图2图3这里,知识库应该是同一个地方,外框应该是差不多的」。
 *
 * 🔴 为什么必须共用:改之前同一个客户在两个页面显示的**分数不一样** ——
 *    写文章那边 7/8(m3 的 8 字段口径),图文这边 x/10(另一份 10 项口径)。
 *    不是"外框长得不一样",是数字对不上。现在两边同一个组件、同一个端点、
 *    同一份计算(`services/client_knowledge.build_client_knowledge`)。
 *
 * 🔴 各页**不同**的东西走 props 传进来,不塞进组件里做分支:
 *      statusBadge —— 写文章侧才有的"待客户确认"(那是 quote 级状态,图文没有)
 *      actions     —— 各自的按钮(补全资料 / 确认链接 / AI 补全)
 *    组件只负责"这个客户有什么",这一件事两边完全一致。
 */
import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { authFetch } from '@/lib/api';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { BookOpen, Loader2 } from 'lucide-react';

export interface ClientKnowledge {
    has_brand: boolean;
    /** 🔴 与"没资料"不是一回事:取失败要单独显示,不能表现成客户没填。 */
    load_failed: boolean;
    materials: {
        filled: number; total: number;
        items: Array<{ key: string; label: string; present: boolean }>;
    };
    images: { count: number; thumbs: Array<{ thumb: string; alt: string }> };
    contact_ready: boolean;
    summary: string;
    intro_excerpt?: string;
    usp_excerpt?: string;
}

interface Props {
    brandId: number | null;
    /** quote 级状态徽章(写文章侧独有) */
    statusBadge?: ReactNode;
    /** 右下动作区 */
    actions?: ReactNode;
    /** 父组件想拿到数据时用(例如判断"资料够不够开写") */
    onLoaded?: (kb: ClientKnowledge | null) => void;
    /** 外部触发重取(补全资料之后) */
    reloadToken?: number;
    /**
     * [#188] 把**缺哪几项**按名列出来。
     * 🔴 只给「5/8」是让人猜:她得点进档案页逐项比对才知道差什么。
     *    Owner 09-13:「任何这种不直观、需要人去猜的…全部不允许」。
     *    默认关(写文章那张卡维持原样),图文制作台打开。
     */
    showMissing?: boolean;
    /** [#188] 卡底那句"这些资料会被用到哪里"。不传就不显示。 */
    note?: ReactNode;
    className?: string;
}

export function ClientKnowledgeCard({
    brandId, statusBadge, actions, onLoaded, reloadToken, showMissing, note, className,
}: Props) {
    const [kb, setKb] = useState<ClientKnowledge | null>(null);
    const [loading, setLoading] = useState(false);

    const load = useCallback(async () => {
        if (!brandId) { setKb(null); onLoaded?.(null); return; }
        setLoading(true);
        try {
            const res = await authFetch(`/api/geo-douyin/clients/${brandId}/knowledge`);
            if (!res.ok) { setKb(null); onLoaded?.(null); return; }
            const d = await res.json();
            const next: ClientKnowledge = {
                has_brand: !!d.has_brand,
                load_failed: !!d.load_failed,
                materials: {
                    filled: Number(d?.materials?.filled ?? 0),
                    total: Number(d?.materials?.total ?? 0),
                    items: Array.isArray(d?.materials?.items) ? d.materials.items : [],
                },
                images: {
                    count: Number(d?.images?.count ?? 0),
                    thumbs: Array.isArray(d?.images?.thumbs) ? d.images.thumbs : [],
                },
                contact_ready: !!d.contact_ready,
                summary: String(d.summary || ''),
                intro_excerpt: d.intro_excerpt || '',
                usp_excerpt: d.usp_excerpt || '',
            };
            setKb(next);
            onLoaded?.(next);
        } catch {
            setKb(null);
            onLoaded?.(null);
        } finally {
            setLoading(false);
        }
        // onLoaded 故意不进依赖:父组件多半传的是内联函数,进依赖会每帧重取
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [brandId]);

    useEffect(() => { void load(); }, [load, reloadToken]);

    const filled = kb?.materials.filled ?? 0;
    const total = kb?.materials.total ?? 0;
    const imageCount = kb?.images.count ?? 0;
    const excerpt = kb?.intro_excerpt || kb?.usp_excerpt || kb?.summary || '';

    return (
        <Card className={`border border-emerald-500/25 bg-card/80 ${className || ''}`}
              data-testid="client-knowledge-card">
            <CardContent className="space-y-3 p-4">
                <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                        <div className="flex items-center gap-2">
                            <BookOpen className="h-4 w-4 text-emerald-500" />
                            <h3 className="text-sm font-semibold text-foreground">知识库 / 写作资料</h3>
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                            客户基础资料、图片和可选联系方式都会同步到客户档案。写文章和做图文用的是同一份。
                        </p>
                    </div>
                    {loading && <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />}
                </div>

                {kb?.load_failed ? (
                    // 🔴 取失败必须与"客户没填"分开显示 —— 混成一句,
                    //    一个查询异常能安静躺一整个版本。
                    <p className="text-xs text-destructive">资料暂时读不出来，稍后再看（不是客户没填）</p>
                ) : (
                    <>
                        <div className="flex flex-wrap gap-1.5">
                            <Badge variant="outline"
                                   className="border-emerald-500/30 text-[11px] text-emerald-600 dark:text-emerald-300"
                                   data-testid="kb-filled">
                                资料 {filled}/{total}
                            </Badge>
                            <Badge variant="outline"
                                   className={`text-[11px] ${kb?.contact_ready
                                       ? 'border-emerald-500/30 text-emerald-600'
                                       : 'border-border/60 text-muted-foreground'}`}>
                                {kb?.contact_ready ? '可选联系方式已填' : '可选联系方式未填'}
                            </Badge>
                            <Badge variant="outline"
                                   className={`text-[11px] ${imageCount > 0
                                       ? 'border-emerald-500/30 text-emerald-600'
                                       : 'border-amber-500/30 text-amber-600'}`}>
                                {/*
                                    🔴 [#188] 原文「图片 N 张」有歧义:这个数**不是**客户上传了几张,
                                    是「已授权且已确权、可以对外用」的几张
                                    (services/geo_douyin/knowledge_context.count_authorized_images:
                                     publish_allowed=1 AND rights_confirmed=1 —— 生产实测两位的与不冗余,
                                     232 行里只有 33 行两位都为 1)。
                                    区 C「实拍叠字」能不能选读的是**同一个谓词**
                                    (load_authorized_images,同表同条件),所以两处不会互相打架;
                                    但叫「图片」会让人以为"我明明传了图,怎么说我没有"。照实说。
                                */}
                                可用实拍图 {imageCount} 张
                            </Badge>
                            {statusBadge}
                        </div>

                        <div className="rounded-lg border border-border/50 bg-background/40 p-2.5 text-xs">
                            <div className="text-muted-foreground">资料摘要</div>
                            <div className="mt-1 line-clamp-2 text-foreground">
                                {excerpt || '还没有整理出客户资料摘要'}
                            </div>
                        </div>

                        {/* [#188] 缺项按名列出 —— 「5/8」只说了有多少,没说差什么。 */}
                        {showMissing && (() => {
                            const missing = (kb?.materials.items || []).filter(i => !i.present);
                            if (missing.length === 0) {
                                return (
                                    /*
                                     * 🔴 原文写死「八项」,而 total 是服务端给的。
                                     *    截图里撞见 `资料 0/0` 配「八项资料都齐了」——
                                     *    一句自信的假话。项数用真值;真的一项都没有时
                                     *    (total=0)那不是"齐了",是"还没有资料项"。
                                     */
                                    total > 0 ? (
                                        <p className="text-xs text-emerald-600 dark:text-emerald-300"
                                            data-testid="kb-missing-none">{total} 项资料都齐了</p>
                                    ) : (
                                        <p className="text-xs text-muted-foreground"
                                            data-testid="kb-missing-none">还没有资料项</p>
                                    )
                                );
                            }
                            return (
                                <p className="text-xs text-amber-600 dark:text-amber-300"
                                    data-testid="kb-missing-list">
                                    还缺:{missing.map(i => i.label).join('、')}
                                </p>
                            );
                        })()}

                        {(kb?.images.thumbs?.length ?? 0) > 0 && (
                            <div className="flex gap-2">
                                {/* 🔴 [#188 · 设计 §2 A] ≤4 张(原来 3 张)。禁猜第 5 条:
                                    「看得见吃了什么」—— 缩略图是唯一能让人确认
                                    "系统要拿去用的就是这几张"的东西。 */}
                                {kb!.images.thumbs.slice(0, 4).map((t, i) => (
                                    t.thumb
                                        ? <img key={i} src={t.thumb} alt={t.alt || ''} loading="lazy"
                                               className="h-12 w-12 rounded-md border border-border/50 object-cover" />
                                        : <div key={i} className="h-12 w-12 rounded-md border border-border/50 bg-muted" />
                                ))}
                            </div>
                        )}
                    </>
                )}

                {note && (
                    <p className="text-xs text-muted-foreground" data-testid="kb-note">{note}</p>
                )}
                {actions && <div className="flex flex-wrap gap-2">{actions}</div>}
            </CardContent>
        </Card>
    );
}

export default ClientKnowledgeCard;
