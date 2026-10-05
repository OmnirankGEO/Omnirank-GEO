/**
 * 制作 GEO 图文 · 内容详情页(三栏)
 *
 * 左 = 大图 + 缩略条 + 城市版本 + 文案编辑区
 * 中 = 手机预览 + 「去发布投放」跳转(+ 下载全部在下方,次要位)
 *      🔴 本页**没有任何直接下单入口**:账号选择/筛选/价格/频控/下单
 *      全部在发布中心(2026-08-03 Owner 裁定)。
 * 右 = 知识库资料卡 + 发布前确认卡
 *
 * 🔴 预览真实性(工单两条 P1),实现方式而不只是"做了":
 *   1. 手机预览与大图**共用同一个 activeIdx**,默认 0(封面),左右可滑整组,
 *      角标显示 `当前/总数`;点缩略图就是改这一个 state,所以三处天然联动。
 *   2. 预览的标题/正文/标签**直接渲染编辑区的 state**,不另存一份 ——
 *      "禁两套文案"不是靠同步逻辑维持的,是**结构上只有一份**。
 *
 * 🔴 价格一律从 /api/geo-douyin/pricing 读实价,**不在本文件写任何数字**
 *    (价目表 SSOT;Owner 调价不该要改前端代码)。
 *
 * 🔴 黄点只在后端 `checked=true` 时才渲染。没有比对基准就一个点都不给 ——
 *    工单:禁止摆假数据。
 *
 * 🔴 供应商零暴露:本页不出现任何供应商名,统一「外部发布通道」;
 *    错误一律取后端已脱敏的 detail。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ImageNoteTopicPanel } from './ImageNoteTopicPanel';
import { selectionView, userError, userFacingError, type TopicRow } from './imageNoteTopics';
import { authFetch } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { Switch } from '@/components/ui/switch';
import {
    Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog';
import {
    AlertCircle, ChevronLeft, ChevronRight, Download, Heart, Link2,
    Loader2, MessageCircle, Phone, RefreshCw, Save, Send,
    Share2, Sparkles, Star, CheckCircle2, FileText, Image as ImageIcon,
    Plus, Music2, User,
} from 'lucide-react';
import { useDouyinPostTask, formatEta } from '@/hooks/useDouyinPostTask';
// 风格选择器 2026-08-04 抽成共用组件 —— 创作页也要用同一份四款样例。
// 留在本文件里的话，创作页只能复制一份，加第五款风格时必然漏改一处。
import { CardStylePicker, STYLE_SAMPLES, type StyleOption }
    from '@/components/writing/CardStylePicker';
import { cn } from '@/lib/utils';
import { useUnsavedWarning } from '@/hooks/useUnsavedWarning';
import { useAuth } from '@/context/AuthContext';
import { imageNotePublishHref, sameCopy, matchesSavedRevision } from './imageNoteFlow';

// ─────────────────────────────────────────────────────────────
// 类型
// ─────────────────────────────────────────────────────────────
interface CardMeta {
    idx: number;
    kind: string;            // cover | content | closing
    headline: string;
    status: string;
    /** 这张卡在组里承担什么(封面问题/比较口径/成本结构…)。
     *  由 series_plan.ROLE_SPEC 回填,是缩略条标签的正解 ——
     *  比把正文标题切五个字强得多。 */
    role_label?: string | null;
}

export interface DouyinPostFull {
    id: number;
    brand_id: number | null;
    active_revision_id?: number | null;
    title: string | null;
    body_text: string | null;
    hashtags: string[] | null;
    cards: CardMeta[] | null;
    oss_keys: string[] | null;
    status: string;
    publish_status?: string | null;
    city: string | null;
    keyword: string | null;
    redraw_count: number;
    style_key: string | null;
    contact_enabled: boolean;
    closing_stale: boolean;
    /** 画幅(规范 §8.8)。预览框按它取比例 —— 9:16 的内容用 3:4 的框预览
     *  会把用户的判断带偏(他看到的留白和真机上不一样)。 */
    aspect_ratio?: string | null;
}

interface Sibling { id: number; city: string | null; status: string; title: string | null }
interface PriceRow { feature_code: string; cost_points: number; feature_name: string }
interface Pricing {
    first_generation: PriceRow | null;
    regenerate: PriceRow | null;
    /** 🔴 2026-08-03 起重抽收费,这一档才是重抽的实价。原来的 `redraw_free`
     *  字段**已删除**(不是置 false)—— 留一个恒 false 的 free 字段,
     *  下一个读代码的人第一眼看到的仍会是"有免费这回事"。 */
    redraw: PriceRow | null;
    extra_card: PriceRow | null;
    included_cards: number;
    redraw_limit: number;
    /** 榜单母版可选项。后端是 SSOT,本文件不写死一份(与画幅同一个道理)。 */
    ranking_templates?: Array<{ key: string; label: string; when: string }>;
}
interface KnowledgeCard {
    has_brand: boolean;
    /** 🔴 与"没资料"不是一回事:取失败要单独显示,不能表现成客户没填。 */
    load_failed: boolean;
    materials: {
        filled: number; total: number;
        items: Array<{ key: string; label: string; present: boolean }>;
    };
    images: { count: number; thumbs: Array<{ thumb: string; alt: string }> };
    /** 这条内容【实际用到】的来源(生产时留痕),不是"这个客户有什么"。 */
    sources_used: string[];
}

interface ConsistencyReport {
    checked: boolean;
    reason: string;
    flagged_count: number;
    cards: Array<{ card_index: number; ok: boolean; mismatches: string[] }>;
    /** 规范 §8.6-10:卡面写的和文案说的对不对得上。与上面 cards 是**不同的轴** ——
     *  cards 核的是"资料库里有没有这个数",alignment 核的是"图上和文案自不自相矛盾"。 */
    alignment?: {
        checked: boolean;
        reason: string;
        ok: boolean;
        issues: Array<{ kind: string; where: string; detail: string }>;
    };
}
/** 规范 §8.6-11 的 OCR 逐字核验结果。
 *  🔴 `checked` 与 `ok` 是两件事:checked=false 表示**没核成**(服务不可用/没出图),
 *     绝不能渲染成绿灯。每张卡各有自己的 checked。 */
interface OcrReport {
    checked: boolean;
    reason: string;
    flagged_count: number;
    checked_count: number;
    cards: Array<{
        card_index: number; checked: boolean; ok: boolean;
        reason: string; missing: string[];
    }>;
}

/** 榜单窄 DTO(后端 `ranking_summary`)。
 *  🔴 前端**不读原始 generation_meta** —— 那一块里有 contract_hash / 内部 ID /
 *     供应商模型名。窄 DTO 让"前端能拿到什么"变成一份可被锁住的清单。 */
interface RankingSummary {
    content_form: string;
    template: string;
    template_label: string;
    entity_count: number;
    entity_count_actual: number;
    degraded: boolean;
    engine_label: string;
    fallback_notice: { reason: string; message: string;
                       actions: Array<{ id: string; label: string; type: string; href?: string }> } | null;
    gates: Array<{ gate: string; message: string; card_indices: number[] }>;
}

interface DetailPayload {
    post: DouyinPostFull;
    preview_urls: string[];
    siblings: Sibling[];
    style: { key: string; label: string };
    /** null = 这条不是榜单作品(卡组型)。 */
    ranking: RankingSummary | null;
    redraw: { used: number; limit: number; remaining: number };
    contact: { configured: boolean; display: string; enabled: boolean; closing_stale: boolean };
    task?: { status?: string } | null;
}
interface Props {
    postId: number;
    /** 同一批作品的 id 顺序,用于「上一条/下一条 (N/M)」。 */
    siblingIds?: number[];
    /**
     * 把本篇所在的同城市/同批顺序**回传**给父层(#203)。
     *
     * 🔴 为什么不是"详情页自己兜底用 detail.siblings":
     *    那样父层忘了接线也照样能翻页 ⇒「没接线」这件事**永远测不出来**。
     *    顺序的唯一来源仍是这一次 `/posts/{id}` 的回包,不多拉一次。
     */
    onSiblings?: (ids: number[]) => void;
    onNavigate?: (postId: number) => void;
    onBack?: () => void;
    onChanged?: () => void;
    /** The step-based route owns topic selection; legacy embedding can retain the side list. */
    showTopics?: boolean;
    onBrandResolved?: (brandId: number) => void;
    workspaceStep?: number;
    onStageChange?: (step: number) => void;
}

/** 取后端【已脱敏】的人话,绝不回退到原始异常串。 */
async function readError(res: Response, fallback: string): Promise<string> {
    try {
        const data = await res.json();
        const d = data?.detail;
        if (typeof d === 'string') return d;
        if (d && typeof d === 'object' && typeof d.message === 'string') return d.message;
        if (typeof data?.message === 'string') return data.message;
    } catch {
        /* 非 JSON 响应 → 兜底文案,不把原始 body 抛给用户 */
    }
    return fallback;
}

/** 手机状态栏那几个小图标。
 *  🔴 原来这里写的是 `▮▮▮` 三个方块字符 —— 那是**占位符没删**,
 *     截图里一眼就看得出"没做完"。真画出来才三十行 SVG。 */
function PhoneStatusIcons() {
    return (
        <span className="flex items-center gap-1 text-white/90">
            <svg viewBox="0 0 18 12" className="h-2.5 w-4" fill="currentColor" aria-hidden>
                <rect x="0" y="8" width="3" height="4" rx="0.6" />
                <rect x="5" y="5.5" width="3" height="6.5" rx="0.6" />
                <rect x="10" y="3" width="3" height="9" rx="0.6" />
                <rect x="15" y="0" width="3" height="12" rx="0.6" opacity="0.45" />
            </svg>
            <svg viewBox="0 0 16 12" className="h-2.5 w-3.5" fill="none"
                 stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" aria-hidden>
                <path d="M1 4.2a10 10 0 0 1 14 0" />
                <path d="M3.6 7a6.4 6.4 0 0 1 8.8 0" />
                <circle cx="8" cy="10" r="0.9" fill="currentColor" stroke="none" />
            </svg>
            <svg viewBox="0 0 26 12" className="h-2.5 w-5" aria-hidden>
                <rect x="0.6" y="0.6" width="21" height="10.8" rx="2.6"
                      fill="none" stroke="currentColor" strokeWidth="1.2" opacity="0.6" />
                <rect x="2.2" y="2.2" width="14" height="7.6" rx="1.4" fill="currentColor" />
                <path d="M23.4 4.2v3.6a2 2 0 0 0 0-3.6z" fill="currentColor" opacity="0.6" />
            </svg>
        </span>
    );
}

/** 抖音右侧互动栏。
 *  🔴 数字一律 `—`:我们**拿不到**真实互动数,编一个是假数据。
 *     但栏目本身要画出来 —— 它是"这条在抖音上长什么样"的一部分。 */
function PhoneActionRail() {
    return (
        <div className="absolute bottom-16 right-1.5 flex flex-col items-center gap-3 text-white">
            <span className="relative mb-1">
                <span className="flex h-7 w-7 items-center justify-center rounded-full
                                 border border-white/60 bg-white/10">
                    <User className="h-3.5 w-3.5 text-white/70" />
                </span>
                <span className="absolute -bottom-1.5 left-1/2 flex h-3.5 w-3.5 -translate-x-1/2
                                 items-center justify-center rounded-full bg-rose-500">
                    <Plus className="h-2.5 w-2.5" strokeWidth={3} />
                </span>
            </span>
            {[Heart, MessageCircle, Star, Share2].map((Icon, i) => (
                <span key={i} className="flex flex-col items-center gap-0.5">
                    <Icon className="h-[18px] w-[18px]" />
                    <span className="text-[9px] leading-none text-white/75">—</span>
                </span>
            ))}
        </div>
    );
}

/** 抖音底部 tab 栏。参考图里有,而它恰恰是"这是抖音不是随便一个黑框"的关键。 */
function PhoneTabBar() {
    return (
        <div className="flex items-center justify-between border-t border-white/10
                        bg-black px-3 py-1.5 text-[9px] text-white/45">
            <span className="text-white">首页</span>
            <span>朋友</span>
            <span className="flex h-4 w-7 items-center justify-center rounded bg-white text-black">
                <Plus className="h-3 w-3" strokeWidth={3} />
            </span>
            <span>消息</span>
            <span>我</span>
        </div>
    );
}

const KIND_LABEL: Record<string, string> = {
    cover: '封面', content: '内容', closing: '收尾',
};

// ─────────────────────────────────────────────────────────────
// 排版层级(2026-08-03 · Owner 拿参考图打回后补)
// ─────────────────────────────────────────────────────────────
// 🔴 打回的**根因不是某个组件写错了**,是这页从头到尾只有数据系统、没有视觉系统:
//    每一块都是 `<Card><CardContent className="space-y-3 p-4">` + `text-sm font-medium`
//    的小标题。同样的内边距、同样的行距、同样的字号 —— 于是一个分区标题、
//    一条警告、一个输入框、一排状态 chip **视觉权重完全相同**,右栏八个分区一起喊。
//    参考图赢在"决定了什么该响、什么该轻",不在于它用了什么特效。
//
// 所以先把层级定下来,再让所有分区引用它 —— 这样"改一处=全页一致",
// 而不是每个分区各写各的 className(那正是漂成现在这样的原因)。

/** 分区标题:小、稳、不抢内容。整页只有这一种写法。 */
function SectionLabel({ children, right }: {
    children: React.ReactNode; right?: React.ReactNode;
}) {
    return (
        <div className="flex items-baseline justify-between gap-2">
            <p className="text-[13px] font-semibold tracking-wide text-foreground/90">
                {children}
            </p>
            {right && <span className="text-[11px] text-muted-foreground">{right}</span>}
        </div>
    );
}

/** 字段小标题(分区内的二级),比 SectionLabel 轻一档。 */
function FieldLabel({ children, right }: {
    children: React.ReactNode; right?: React.ReactNode;
}) {
    return (
        <div className="flex items-baseline justify-between gap-2">
            <span className="text-[11px] font-medium text-muted-foreground">{children}</span>
            {right && (
                <span className="text-[11px] tabular-nums text-muted-foreground/80">{right}</span>
            )}
        </div>
    );
}

/** 只读状态 chip(资料 7/8 这类)。**不是按钮** —— 原来用带边框的方块格子做,
 *  和旁边真正可点的按钮长得一样,是误导。 */
function StatChip({ icon: Icon, children, tone = 'muted' }: {
    icon: React.ElementType; children: React.ReactNode;
    tone?: 'muted' | 'ok' | 'warn';
}) {
    const toneCls = tone === 'ok'
        ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
        : tone === 'warn'
            ? 'border-amber-500/30 bg-amber-500/10 text-amber-600 dark:text-amber-400'
            : 'border-border bg-muted/40 text-muted-foreground';
    return (
        <span className={`inline-flex items-center gap-1 rounded-md border px-2 py-1
                          text-[11px] leading-none ${toneCls}`}>
            <Icon className="h-3 w-3 shrink-0" />
            {children}
        </span>
    );
}

// 标题长度建议档:§6c 实测采纳率 26-45 字优于 16-25 字(那是最差档)
const TITLE_MIN_SUGGEST = 26;
const TITLE_HARD_LIMIT = 45;

export function DouyinPostDetail({
    postId, siblingIds = [], onSiblings, onNavigate, onBack, onChanged, showTopics = true, onBrandResolved,
    workspaceStep, onStageChange,
}: Props) {
    const navigate = useNavigate();
    const { user } = useAuth();
    const [detail, setDetail] = useState<DetailPayload | null>(null);
    /*
     * 🔴 [#204 a1] 左栏选中的那条**选题**。
     *    「已完成」的选题点了直接换 URL(那一屏就是它的作品);
     *    「待做 / 制作中 / 没做成」还没有作品,就留在这里 ——
     *    中栏显示那一档风格的样图与「还没做」,右栏编辑区禁用**并写清原因**。
     *    不写原因的禁用正是第 11 条要清的那种猜(#199 刚清过一批)。
     */
    const [pickedTopic, setPickedTopic] = useState<TopicRow | null>(null);
    const [loading, setLoading] = useState(true);
    const [error, setError] = useState('');
    const [notice, setNotice] = useState('');
    const [saveConflict, setSaveConflict] = useState(false);

    // ── 文案编辑区 = 唯一一份文案 state(手机预览直接读它)──
    const [title, setTitle] = useState('');
    const [bodyText, setBodyText] = useState('');
    const [tags, setTags] = useState<string[]>([]);
    const [newTag, setNewTag] = useState('');
    const [dirty, setDirty] = useState(false);
    const [saving, setSaving] = useState(false);
    useUnsavedWarning(dirty);
    const copyRef = useRef({ title, body: bodyText, tags });
    copyRef.current = { title, body: bodyText, tags };
    const dirtyRef = useRef(dirty);
    dirtyRef.current = dirty;
    const loadEpoch = useRef(0);
    const detailController = useRef<AbortController | null>(null);
    const draftKey = `omnirank-image-note-draft:${user?.id ?? 'none'}:${postId}`;
    const draftBase = useRef('');
    // Drafts are tab-local and account/post-scoped. Never treat them as saved server content.
    useEffect(() => {
        if (!dirty || !detail || !user) return;
        try { sessionStorage.setItem(draftKey, JSON.stringify({ ...copyRef.current, base: draftBase.current })); }
        catch { /* The unload warning still offers save/return when browser storage is unavailable. */ }
    }, [dirty, title, bodyText, tags, detail, user, draftKey]);

    // ── 卡片浏览:大图 / 缩略条 / 手机预览 共用这一个下标 ──
    const [activeIdx, setActiveIdx] = useState(0);

    const [pricing, setPricing] = useState<Pricing | null>(null);
    const [styles, setStyles] = useState<StyleOption[]>([]);
    const [consistency, setConsistency] = useState<ConsistencyReport | null>(null);
    const [knowledge, setKnowledge] = useState<KnowledgeCard | null>(null);
    // 规范 §8.6-11:卡面文字 OCR 逐字核验。按需触发(不自动跑、不收费)。
    const [ocr, setOcr] = useState<OcrReport | null>(null);
    /**
     * 🔴 [WO_262] 弹窗顶部那段原因,与 OCR 面板读**同一份 `ocr`、同一组过滤条件**
     *    (`checked && !ok`),只是按 `activeIdx` 取其中一张。
     *    两处各算一遍的话,会出现「面板说第 4 张缺 2 条、弹窗里一条都不显示」——
     *    而那种不一致**两边单独看都对**,正是本仓反复出现的那类缺陷。
     */
    const activeMissing = useMemo<string[]>(() => {
        const card = ocr?.cards?.find(c => c.card_index === activeIdx && c.checked && !c.ok);
        return Array.isArray(card?.missing) ? card.missing : [];
    }, [ocr, activeIdx]);
    const [ocrRunning, setOcrRunning] = useState(false);

    const [redrawOpen, setRedrawOpen] = useState(false);
    const [redrawHint, setRedrawHint] = useState('');
    const [redrawing, setRedrawing] = useState(false);

    const [regenOpen, setRegenOpen] = useState(false);
    const [regenHint, setRegenHint] = useState('');
    const [regenStyle, setRegenStyle] = useState('');
    const [regenerating, setRegenerating] = useState(false);
    // 🔴 P2-2:榜单母版的默认值必须**来自这条作品的快照**,不是一个空串。
    //    `null` = 用户没在弹窗里改过 → 请求里不发这个字段 → 服务端继承快照。
    //    这个"没改"与"改成自动"的区别必须一路保到后端(见 RegenerateRequest 三态)。
    const [regenTemplate, setRegenTemplate] = useState<string | null>(null);

    const productionView = workspaceStep === 2;
    // 当前有没有在等一个后台长活;'redraw' | 'regen' | null
    const [busyKind, setBusyKind] = useState<'redraw' | 'regen' | null>(null);
    const [canPublish, setCanPublish] = useState(false);
    const [gateReason, setGateReason] = useState('');

    const post = detail?.post ?? null;
    const previews = detail?.preview_urls ?? [];
    const cardsMeta = post?.cards ?? [];
    const total = previews.length;

    // ── 载入详情 ──
    const loadDetail = useCallback(async (keepIdx = false) => {
        detailController.current?.abort();
        const controller = new AbortController();
        detailController.current = controller;
        const epoch = ++loadEpoch.current;
        setLoading(true);
        setError('');
        try {
            const res = await authFetch(`/api/geo-douyin/posts/${postId}`, { signal: controller.signal });
            if (!res.ok) throw userError(await readError(res, '这条内容没打开'));
            const data: DetailPayload = await res.json();
            if (controller.signal.aborted || epoch !== loadEpoch.current) return;
            setDetail(data);
            const serverCopy = { title: data.post.title || '', body: data.post.body_text || '',
                tags: (data.post.hashtags || []).map(t => String(t).replace(/^#/, '')) };
            draftBase.current = JSON.stringify(serverCopy);
            if (!dirtyRef.current) {
                let next = serverCopy;
                try {
                    const stored = JSON.parse(sessionStorage.getItem(draftKey) || 'null');
                    if (stored && typeof stored.title === 'string' && typeof stored.body === 'string'
                        && Array.isArray(stored.tags) && stored.tags.every((t: unknown) => typeof t === 'string')) {
                        next = stored;
                        setNotice(stored.base === draftBase.current
                            ? '已找回这台浏览器里未保存的修改，请保存后再发布。'
                            : '已找回未保存的修改；服务器内容也有变化，请核对后保存。');
                    }
                } catch { /* Invalid or unavailable draft storage never blocks server reads. */ }
                setTitle(next.title); setBodyText(next.body); setTags(next.tags);
                setDirty(!sameCopy(next, serverCopy));
            }
            if (data.post.brand_id) onBrandResolved?.(data.post.brand_id);
            setRegenStyle(data.style?.key || '');
            if (data.task && ['pending', 'running'].includes(data.task.status || '')) setBusyKind('regen');
            if (!keepIdx) setActiveIdx(0);   // P1:默认从封面(第 1 张)开始
        } catch (e) {
            if (controller.signal.aborted || epoch !== loadEpoch.current) return;
            setError(userFacingError(e, '这条内容没打开'));
        } finally {
            if (!controller.signal.aborted && epoch === loadEpoch.current) setLoading(false);
        }
    }, [postId, draftKey, onBrandResolved]);

    useEffect(() => { void loadDetail(); return () => { detailController.current?.abort(); ++loadEpoch.current; }; }, [loadDetail]);

    /* 回包一到就把顺序回传父层。回调放 ref:父层每次渲染换引用会把这里变成死循环。 */
    const onSiblingsRef = useRef(onSiblings);
    onSiblingsRef.current = onSiblings;
    useEffect(() => {
        if (!detail) return;
        onSiblingsRef.current?.((detail.siblings || []).map(x => x.id));
    }, [detail]);

    // ── 后台任务进度(重抽 / 再次创作 都异步了)──
    // busyKind 非空 = 我们刚发起过一次长活,开轮询;后端说不 active 了就停并刷新。
    // 🔴 复用同一个 hook 与同一套后端进度语义 —— 前端不自己判"算不算做完"。
    const { progress: taskProgress } = useDouyinPostTask(postId, busyKind !== null);
    useEffect(() => {
        if (!busyKind || !taskProgress) return;
        if (taskProgress.active) return;
        // 到终态:失败就把人话原因显出来,成功就重载(留在当前张,让用户直接看到新图)
        if (taskProgress.state === 'succeeded') {
            setNotice(busyKind === 'redraw' ? '这张已经重做好了' : '已经重新做了一版');
        } else {
            setError(taskProgress.failure_reason || '这次没做成，费用已自动退回');
        }
        setBusyKind(null);
        void loadDetail(busyKind === 'redraw');
        onChanged?.();
        // onChanged 是父层传进来的回调,放进依赖会因为父层每次渲染换引用而反复触发
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [busyKind, taskProgress, loadDetail]);

    // ── 价目实价 + 风格选项 + 闸门(与详情并行,互不阻塞)──
    useEffect(() => {
        void (async () => {
            try {
                const res = await authFetch('/api/geo-douyin/pricing');
                if (res.ok) setPricing(await res.json());
            } catch { /* 价目读不到就不显示数字,不猜 */ }
        })();
        void (async () => {
            try {
                const res = await authFetch('/api/geo-douyin/status');
                if (!res.ok) return;
                const d = await res.json();
                setCanPublish(Boolean(d.can_publish));
                setGateReason(d.publish_disabled_reason || '');
            } catch {
                setCanPublish(false);
                setGateReason('暂时无法发布，请稍后再试');
            }
        })();
    }, []);

    useEffect(() => {
        if (!post) return;
        const ac = new AbortController();
        setStyles([]);
        void (async () => {
            try {
                const q = post.brand_id ? `?brand_id=${post.brand_id}` : '';
                const res = await authFetch(`/api/geo-douyin/styles${q}`, { signal: ac.signal });
                const d = await res.json();
                if (res.ok && !ac.signal.aborted) setStyles(d.styles || []);
            } catch { /* 风格取不到时弹窗内不展示选择器 */ }
        })();
        return () => ac.abort();
    }, [post?.brand_id]);

    // ── 知识库核对(黄点)。单独拉,不拖住详情 ──
    useEffect(() => {
        if (!postId) return;
        const ac = new AbortController();
        setConsistency(null);
        void (async () => {
            try {
                const res = await authFetch(`/api/geo-douyin/posts/${postId}/consistency`, { signal: ac.signal });
                const d = await res.json();
                if (ac.signal.aborted) return;
                setConsistency(res.ok ? d : null);
            } catch {
                if (!ac.signal.aborted) setConsistency(null);
            }
        })();
        return () => ac.abort();
    }, [postId]);

    // ── 右栏知识库卡明细。同样单独拉,不拖住左中两栏 ──
    useEffect(() => {
        if (!postId) return;
        const ac = new AbortController();
        setKnowledge(null);
        void (async () => {
            try {
                const res = await authFetch(`/api/geo-douyin/posts/${postId}/knowledge`, { signal: ac.signal });
                const d = await res.json();
                if (ac.signal.aborted) return;
                setKnowledge(res.ok ? d : null);
            } catch {
                if (!ac.signal.aborted) setKnowledge(null);
            }
        })();
        return () => ac.abort();
    }, [postId]);

    // 🔴 账号列表与频控预检已整体删除(2026-08-03):
    //    它们服务的是本页那个内嵌选择器,而选择器已交给发布中心。
    //    留着 = 每开一次详情页就白打两个请求,而且早晚有人拿它复活内嵌下单。

    /** 规范 §8.6-11:核一次卡面文字。不收费,所以按钮上不标价;
     *  但它要打视觉服务,后端有 30s 节流,这里靠 disabled 挡连点。 */
    const runOcrCheck = async () => {
        if (!postId || ocrRunning) return;
        setOcrRunning(true);
        try {
            const res = await authFetch(`/api/geo-douyin/posts/${postId}/ocr-check`,
                { method: 'POST' });
            const d = await res.json();
            if (!res.ok) {
                // 🔴 失败**不写进 ocr 状态** —— 写进去会让"没核成"长得像"核过了"。
                setOcr(null);
                setError('这次卡面文字没核对成功，可以稍后重试；已写好的内容还在。');
                return;
            }
            setOcr(d);
        } catch {
            setOcr(null);
            setError('这次卡面文字没核对成功，可以稍后重试；已写好的内容还在。');
        } finally {
            setOcrRunning(false);
        }
    };

    // ── 黄点:后端说 checked 才给 ──
    const flagMap = useMemo(() => {
        const m = new Map<number, string[]>();
        if (!consistency?.checked) return m;      // 没有比对基准 → 一个点都不给
        for (const c of consistency.cards) {
            if (!c.ok) m.set(c.card_index, c.mismatches);
        }
        return m;
    }, [consistency]);

    const activeFlags = flagMap.get(activeIdx) ?? null;

    // ── 上一条/下一条 ──
    const orderIds = siblingIds.length ? siblingIds : [postId];
    const orderPos = Math.max(0, orderIds.indexOf(postId));
    const gotoOffset = (delta: number) => {
        const next = orderIds[orderPos + delta];
        if (next && onNavigate) onNavigate(next);
    };

    const step = (delta: number) => {
        if (total === 0) return;
        setActiveIdx(i => (i + delta + total) % total);
    };

    // 键盘左右键翻卡(手机预览是主动线,这里给个快捷通道)
    const stageRef = useRef<HTMLDivElement | null>(null);
    useEffect(() => {
        const onKey = (e: KeyboardEvent) => {
            if (redrawOpen || regenOpen) return;
            const el = document.activeElement;
            if (el && ['INPUT', 'TEXTAREA'].includes(el.tagName)) return;
            if (e.key === 'ArrowLeft') step(-1);
            if (e.key === 'ArrowRight') step(1);
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [total, redrawOpen, regenOpen]);

    // ── 保存文案 ──
    const save = async () => {
        if (saving) return false;
        const submitted = { ...copyRef.current, tags: [...copyRef.current.tags] };
        setSaving(true);
        setError('');
        try {
            const res = await authFetch(`/api/geo-douyin/posts/${postId}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ title: submitted.title, body_text: submitted.body, hashtags: submitted.tags,
                    expected_revision_id: detail?.post.active_revision_id ?? null }),
            });
            if (res.status === 409) setSaveConflict(true);
            if (!res.ok) throw userError(await readError(res, '没保存上'));
            const receipt = await res.json();
            const readBack = await authFetch(`/api/geo-douyin/posts/${postId}`);
            if (!readBack.ok) throw new Error('修改已提交，但重新读取没成功。请保留当前内容，重试保存后再发布。');
            const saved: DetailPayload = await readBack.json();
            setDetail(saved);
            if (!matchesSavedRevision(receipt, saved.post)) {
                setSaveConflict(true);
                throw new Error('保存后作品又有更新。你的输入已保留，请对比最新版本后再保存或发布。');
            }
            setSaveConflict(false);
            const savedCopy = { title: saved.post.title || '', body: saved.post.body_text || '',
                tags: (saved.post.hashtags || []).map(t => String(t).replace(/^#/, '')) };
            draftBase.current = JSON.stringify(savedCopy);
            if (sameCopy(submitted, copyRef.current)) {
                setTitle(savedCopy.title); setBodyText(savedCopy.body); setTags(savedCopy.tags);
                dirtyRef.current = false; setDirty(false);
                try { sessionStorage.removeItem(draftKey); } catch { /* optional browser storage */ }
            }
            setNotice('改动已保存');
            onChanged?.();
            return sameCopy(submitted, copyRef.current);
        } catch (e) {
            setError(userFacingError(e, '没保存上'));
            return false;
        } finally {
            setSaving(false);
        }
    };

    // ── 插入联系方式开关 ──
    const toggleContact = async (enabled: boolean) => {
        setError('');
        try {
            const res = await authFetch(`/api/geo-douyin/posts/${postId}/contact`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled }),
            });
            const data = await res.json().catch(() => null);
            if (!res.ok) throw userError(await readError(res, '开关没生效'));
            setNotice(data.message || '');
            await loadDetail(true);
        } catch (e) {
            setError(userFacingError(e, '开关没生效'));
        }
    };

    // ── 重抽这张 ──
    // 🔴 已异步化:端点立刻返回,真活在后台。原因是单张实测 49-73s,
    //    而生产 nginx 对本前缀是 60s 超时 —— 同步会出现"前端报失败、
    //    图其实出了、额度也扣了"这种最难跟用户解释的状态。
    const doRedraw = async () => {
        setRedrawing(true);
        setError('');
        try {
            const res = await authFetch(
                `/api/geo-douyin/posts/${postId}/cards/${activeIdx}/redraw`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ hint: redrawHint }),
            });
            const data = await res.json().catch(() => null);
            if (data?.status === 'coming_soon') { setError(data.message); return; }
            if (!res.ok) throw userError(await readError(res, '这张没重做出来'));
            setRedrawOpen(false);
            setRedrawHint('');
            setNotice(data?.message || '正在重画这一张');
            setBusyKind('redraw');          // → 轮询接管,好了自动刷新
        } catch (e) {
            setError(userFacingError(e, '这张没重做出来'));
        } finally {
            setRedrawing(false);
        }
    };

    // ── 再次创作(整条重做,走 regen 档)──
    // 🔴 同样异步:整组重做 ≈137s,同步必然被 nginx 60s 掐断。
    const doRegenerate = async () => {
        setRegenerating(true);
        setError('');
        try {
            const res = await authFetch(`/api/geo-douyin/posts/${postId}/regenerate`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                // 🔴 P1-3:榜单参数**一个都不发** —— 服务端从这条作品的冻结快照继承。
                //    只有用户在弹窗里显式改过母版时才发 `ranking_template`。
                //    上一版这里就是只发 style/hint,而服务端也没回取,于是一条付费
                //    重做把榜单作品做成了普通图文。
                body: JSON.stringify({
                    style_key: regenStyle, extra_hint: regenHint,
                    ...(regenTemplate !== null ? { ranking_template: regenTemplate } : {}),
                }),
            });
            const data = await res.json().catch(() => null);
            if (data?.status === 'coming_soon') { setError(data.message); return; }
            if (!res.ok) throw userError(await readError(res, '没做出来'));
            setRegenOpen(false);
            setRegenHint('');
            setRegenTemplate(null);      // 下次打开仍从快照取默认值
            setNotice(data?.message || '开始重新创作了');
            setBusyKind('regen');
        } catch (e) {
            setError(userFacingError(e, '没做出来'));
        } finally {
            setRegenerating(false);
        }
    };

    // Save and confirm the immutable revision before handing off the current work.
    const goPublish = async () => {
        if (dirtyRef.current && !(await save())) return;
        if (post) navigate(imageNotePublishHref(post.id, post.brand_id));
    };

    if (loading && !detail) {
        return (
            <div className="flex items-center justify-center py-24 text-muted-foreground">
                <Loader2 className="mr-2 h-5 w-5 animate-spin" />正在打开
            </div>
        );
    }
    if (!post) {
        return (
            <div className="space-y-4 py-12 text-center">
                <p role="alert" className="text-sm text-muted-foreground">{error || '这条内容打不开'}</p>
                {onBack && <Button variant="outline" onClick={onBack}>回写作中心</Button>}
            </div>
        );
    }

    const notReady = post.status !== 'ready';
    // 跳转前门:闸关 / 还没做好 / §15 补齐中,都不放行去投放
    const completing = post?.status === 'completing';
    const pendingCards = (post?.cards || []).filter(
        c => !(String(c.status) === 'ready')).length;
    const receiptAvailable = ['publishing', 'published'].includes(post.status)
        || ['submitted', 'publishing', 'published'].includes(post.publish_status || '');
    const jumpBlocked = !post.active_revision_id || !post.brand_id || (notReady && !receiptAvailable);
    /*
     * 🔴 [#203] `gateReason` **可以为空**(闸还没拉回来 / 服务端没给理由)——
     *    空的时候原来这里也返回空串,于是屏幕上是一颗**灰着的主按钮,一个字都没有**:
     *    用户知道有事,但不知道是什么事,也不知道该做什么。
     *    (2026-08-04 那次决定的是"没理由就不显示那个黄框",说的是**警告框**;
     *     按钮下面这一行是另一个元件,不能跟着一起沉默。)
     *    ⇒ 没有服务端原话时给一句**说明现在是什么状态**的兜底,不编原因。
     */
    /*
     * 🔴 [#204 a1 · A6/A7] 左栏选中「待做 / 制作中 / 没做成」的选题时:
     *    中栏显示那一档风格的**样图**并明说「还没做」,右栏编辑区**禁用并写清原因**。
     *    判断落在零 import 的 `selectionView` 里 —— 界面按它渲染,判据也调它,
     *    在 JSX 里再写一遍条件就会有一天两边不是同一句话。
     */
    const topicView = selectionView(pickedTopic);
    const showStyleSample = topicView.preview === 'style_sample';

    const jumpHint = !canPublish ? (gateReason || '正在确认这条能不能去投放，稍等一下')
        : completing ? `还有 ${pendingCards} 张没出来，补齐了再发`
            : notReady ? '这条内容还没做好' : '';

    const redrawUsed = detail?.redraw.used ?? 0;
    const rankingTemplates = pricing?.ranking_templates ?? [];
    const redrawLimit = detail?.redraw.limit ?? (pricing?.redraw_limit ?? 0);
    const redrawExhausted = redrawLimit > 0 && redrawUsed >= redrawLimit;
    const regenPoints = pricing?.regenerate?.cost_points ?? null;
    // 🔴 重抽 2026-08-03 起收费。价从 /pricing 读,本文件不写数字。
    const redrawPoints = pricing?.redraw?.cost_points ?? null;

    const titleLen = title.length;
    const titleHintOk = titleLen >= TITLE_MIN_SUGGEST && titleLen <= TITLE_HARD_LIMIT;

    return (
        <div className="space-y-4">
            {/* ── 顶栏:风格 + 逐条导航 + 两个动作 ── */}
            <div className="flex flex-wrap items-center justify-between gap-3">
                <div className="flex min-w-0 flex-wrap items-center gap-3">
                    {/* 🔴 [#204 a2 · 条件③] 原来这里写「返回列表」,跳 `/writing?tab=douyin`。
                        那个 tab 本单撤了,**列表这层也不存在了**(左栏就是列表)——
                        按钮留着就是一条死路。改成回写作中心:一个不会落空的去处。 */}
                    {onBack && (
                        <Button variant="ghost" size="sm" onClick={onBack}
                            data-testid="detail-back-writing">回写作中心</Button>
                    )}
                    <span className="max-w-full shrink-0 text-sm text-muted-foreground">
                        当前风格：<span className="text-foreground">{detail?.style.label}</span>
                    </span>
                    {orderIds.length > 1 && (
                        <div className="flex shrink-0 items-center gap-1">
                            <Button
                                variant="outline" size="sm" className="h-8 px-2"
                                disabled={orderPos <= 0}
                                onClick={() => gotoOffset(-1)}
                                data-testid="post-prev"
                            >
                                <ChevronLeft className="h-4 w-4" />上一条
                            </Button>
                            <span className="px-1 text-xs tabular-nums text-muted-foreground">
                                {orderPos + 1}/{orderIds.length}
                            </span>
                            <Button
                                variant="outline" size="sm" className="h-8 px-2"
                                disabled={orderPos >= orderIds.length - 1}
                                onClick={() => gotoOffset(1)}
                                data-testid="post-next"
                            >
                                下一条<ChevronRight className="h-4 w-4" />
                            </Button>
                        </div>
                    )}
                </div>

                {/* 动作区:图标进圆角方块 + 主副两行。三个动作视觉规格一致,
                    只有「再次创作」是实心主按钮(它是这一栏里唯一的主动作)。
                    🔴 参考图上「重抽这张」写的是**免费 · 已用 3/10** —— 那是示意图,
                       我们 2026-08-03 起重抽**收费**(Owner 拍板)。价照旧从
                       /pricing 读实价,照抄参考图的"免费"就是把价目讲错。 */}
                {!productionView && <div className="flex flex-wrap items-center gap-2">
                    <Button
                        variant="outline"
                        className="h-11 gap-2.5 px-3"
                        disabled={redrawExhausted || total === 0}
                        onClick={() => setRedrawOpen(true)}
                        data-testid="redraw-open"
                    >
                        <span className="flex h-6 w-6 shrink-0 items-center justify-center
                                         rounded-md border bg-muted/50">
                            <RefreshCw className="h-3.5 w-3.5" />
                        </span>
                        <span className="flex flex-col items-start leading-tight">
                            <span className="text-[13px]">重抽这张</span>
                            <span className="text-[11px] font-normal tabular-nums text-muted-foreground">
                                {redrawPoints !== null ? `${redrawPoints} 算力 · ` : ''}
                                已用 {redrawUsed}/{redrawLimit || '—'}
                            </span>
                        </span>
                    </Button>
                    <Button
                        className="h-11 gap-2.5 px-3"
                        onClick={() => setRegenOpen(true)} data-testid="regen-open"
                    >
                        <span className="flex h-6 w-6 shrink-0 items-center justify-center
                                         rounded-md bg-primary-foreground/15">
                            <Sparkles className="h-3.5 w-3.5" />
                        </span>
                        <span className="flex flex-col items-start leading-tight">
                            <span className="text-[13px]">再次创作</span>
                            {regenPoints !== null && (
                                <span className="text-[11px] font-normal tabular-nums opacity-80">
                                    {regenPoints} 算力
                                </span>
                            )}
                        </span>
                    </Button>
                </div>}
            </div>

            {(error || saveConflict) && (
                /* [WO_283-F8] 动作失败要播报给读屏(校样页原来的 aria-live 并回详情页时丢了) */
                <div role="alert" data-testid="detail-action-error"
                    className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
                    {error}
                    {saveConflict && <div className="mt-2 space-y-2">
                        <Button variant="outline" onClick={() => { void loadDetail(true); }}>读取最新版本，保留我的输入</Button>
                        <p>先读取最新版本，再与下方已存文案对比。你正在编辑的输入不会被替换，对比后可继续修改并保存。</p>
                        <details className="whitespace-pre-wrap"><summary className="cursor-pointer">对比当前读取到的已存版本</summary>
                            <h3 className="mt-2 font-medium">{detail?.post.title}</h3><p>{detail?.post.body_text}</p>
                            <p>{detail?.post.hashtags?.map(t => `#${t}`).join(' ')}</p></details>
                    </div>}
                </div>
            )}
            {notice && (
                <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3 text-sm text-emerald-700">
                    {notice}
                </div>
            )}
            {/* [WO_283-F5] 榜单质量闸的发现(如「标题说 5 家，实测 3 家」)要画出来:后端序列化了 gate / message /
                card_indices 三个键,原来一个都没人读。只提示、不拦(闸本来就是 A1 级,不阻断) */}
            {(detail?.ranking?.gates?.length ?? 0) > 0 && (
                <div className="space-y-1 rounded-md border border-amber-500/30 bg-amber-500/5 p-3 text-sm"
                    data-testid="ranking-gates">
                    <p className="font-medium text-amber-700">这条榜单有几处建议你核对一下</p>
                    <ul className="space-y-1 text-amber-800">
                        {(detail?.ranking?.gates || []).map(f => (
                            <li key={f.gate} data-testid="ranking-gate">
                                {f.message}
                                {(f.card_indices || []).map(i => (
                                    <button key={i} type="button" className="ml-2 underline"
                                        data-testid="ranking-gate-card" onClick={() => setActiveIdx(i)}>
                                        看第 {i + 1} 张
                                    </button>
                                ))}
                            </li>
                        ))}
                    </ul>
                </div>
            )}

            {productionView && <section className="space-y-5 rounded-xl border bg-card p-4 sm:p-6" data-testid="detail-production-summary">
                <div className="flex flex-wrap items-center justify-between gap-3">
                    <div><h2 className="text-lg font-semibold">这条图文的制作情况</h2><p className="mt-1 text-sm text-muted-foreground">这是已有作品的制作记录，查看不会重新生成或收费。</p></div>
                    <Button className="min-h-11" onClick={() => onStageChange?.(3)}>继续检查与修改</Button>
                </div>
                <div><p className="font-medium">{post.title || post.keyword}</p><p className="mt-2 text-sm text-muted-foreground">围绕关键词：{post.keyword || '未记录'}</p></div>
                <dl className="grid gap-4 rounded-lg bg-muted/30 p-4 text-sm sm:grid-cols-3">
                    <div><dt className="text-muted-foreground">制作状态</dt><dd className="mt-1 font-medium">{busyKind ? '正在制作' : ['ready', 'publishing', 'published'].includes(post.status) ? '已制作' : post.status === 'failed' ? '制作未完成，请返回检查重试' : '尚未完成，请返回检查'}</dd></div>
                    <div><dt className="text-muted-foreground">图片与画幅</dt><dd className="mt-1 font-medium">{(post.oss_keys || []).filter(Boolean).length} 张 · {post.aspect_ratio || '未记录画幅'}</dd></div>
                    <div><dt className="text-muted-foreground">制作风格</dt><dd className="mt-1 font-medium">{detail?.style.label || '未记录'}</dd></div>
                </dl>
                <div className="flex gap-3 overflow-x-auto">{previews.filter(Boolean).map((url, i) => <img key={i} src={url} alt={`已制作图片 ${i + 1}`} className="w-28 shrink-0 rounded-lg border object-cover" />)}</div>
                <Button variant="outline" onClick={() => { void loadDetail(true); }}>刷新制作情况</Button>
            </section>}

            {/* 后台长活进度条:真数据由后端下发(阶段/第几张/预计剩余),不模拟 */}
            {busyKind && taskProgress?.active && (
                <div className="space-y-1.5 rounded-md border bg-muted/40 p-3"
                     data-testid="detail-task-progress">
                    <div className="flex items-center justify-between text-xs">
                        <span className="flex items-center gap-1.5 text-foreground">
                            <Loader2 className="h-3.5 w-3.5 animate-spin" />
                            {taskProgress.stage_label}
                            {taskProgress.total > 0 && taskProgress.done > 0
                                ? ` · 第 ${taskProgress.done}/${taskProgress.total} 张`
                                : ''}
                        </span>
                        <span className="tabular-nums text-muted-foreground">
                            {formatEta(taskProgress.eta_seconds)}
                        </span>
                    </div>
                    <div className="h-1 w-full overflow-hidden rounded-full bg-muted">
                        <div className="h-full rounded-full bg-primary transition-all"
                             style={{ width: `${Math.max(2, taskProgress.percent)}%` }} />
                    </div>
                </div>
            )}

            {/* 🔴 2026-08-03 由**视口断点**改成**容器查询**。
                原来是 `xl:`(视口 ≥1280px)。实测:视口 1249px 时这一栏的可用宽度是
                **935px** —— 三栏该排得下,却因为视口没到 1280 而整个塌成一栏,
                大图两侧于是空出两大片死白(Owner 截图里就是这个)。
                更根本的问题:左侧导航栏可以折叠,**视口宽度代表不了可用宽度**,
                拿视口做判据从一开始就是错的量。容器查询量的才是这一栏自己有多宽。
                🔴 [#203] 断点从 896px 抬到 **1024px**(@5xl):
                   896 那档三栏各自只有 220 / 355 / 320px —— 中栏要装 262px 的手机预览、
                   右栏要装文案编辑器,挤到那个宽度就不是"三栏可用",是"三栏都不好用"。
                   工单 D7 要的也是「<1024 顺序式」。
                   sticky 那几个类跟着同一个断点走 —— 不同步的话会出现
                   "已经堆成一列了、中栏却还粘着"。 */}
            <div className={cn('@container', productionView && 'hidden')}>
            <div className={cn('grid gap-5', showTopics
                ? '@5xl:grid-cols-[minmax(0,0.62fr)_minmax(0,1fr)_minmax(0,0.9fr)]'
                : '@3xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]')}
                data-testid="detail-three-cols">
                {/* ═══════════ 左栏 = 设置 ═══════════
                    #203 §4(Owner 09-13 夜):三栏分工固定为 设置 / 预览 / 重要内容。
                    本栏现在放**作品列表**(这一批还有哪几篇,点了换 URL);
                    WO_204 会把「客户一句话 → 生成选题 → 选题列表」填进来,位置不变。
                    🔴 放的是真列表,不是一句"敬请期待" —— 占位不该占掉一整栏的用处。 */}
                {showTopics && <div className="space-y-3" data-testid="detail-col-settings">
                    {/*
                      * 🔴 [#204 a1] 左栏 = 设置。WO_203 时这里放的是"这一批的作品"占位,
                      *    现在换成**选题**那一套(生成选题 → 可改标题 → 按标题制作)。
                      *    这不是新交互:逐项对照写文章大厅,见 ImageNoteTopicPanel 抬头的对照表。
                      */}
                    <Card>
                        <CardContent className="space-y-3 p-4">
                            <SectionLabel>这个客户的图文</SectionLabel>
                            <ImageNoteTopicPanel
                                styles={styles}
                                brandId={post?.brand_id ?? null}
                                selectedTopicId={pickedTopic?.id ?? null}
                                onSelectTopic={(row) => {
                                    /* 🔴 已完成的选题**就是**某一篇作品 ⇒ 换 URL,整屏跟着走;
                                       其余三态还没有作品,留在本地状态里驱动中栏/右栏。 */
                                    if (row.postId && row.postId !== postId) {
                                        setPickedTopic(null);
                                        onNavigate?.(row.postId);
                                        return;
                                    }
                                    setPickedTopic(row.status === 'done' ? null : row);
                                }}
                                onChanged={() => { void loadDetail(true); }}
                            />
                            {/* 城市版本(措辞:不叫"兄弟版本")—— 同一条选题的多城市版本 */}
                            {(detail?.siblings.length ?? 0) > 1 && (
                                <div className="flex flex-wrap items-center gap-2 border-t border-border pt-2">
                                    <span className="text-[11px] font-medium text-muted-foreground">城市版本</span>
                                    {detail!.siblings.map(s => (
                                        <Button
                                            key={s.id}
                                            size="sm"
                                            variant={s.id === postId ? 'default' : 'outline'}
                                            className="h-7 px-2.5 text-xs"
                                            onClick={() => s.id !== postId && onNavigate?.(s.id)}
                                            data-testid={`city-${s.id}`}
                                        >
                                            {s.city || '不限城市'}
                                        </Button>
                                    ))}
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    {/* 卡面风格 —— Owner 2026-08-04:「右边这一栏下半部分是空的，
                        可以把风格预览放在右边知识库上面？」
                        🔴 摆在这里不只是填空:在此之前,这条内容用的是哪一款风格
                           只在「发布预览」右上角有四个字,而**四款长什么样、
                           能不能换**用户完全看不到 —— 换风格的唯一入口埋在
                           「再次创作」弹窗里。
                        🔴 点非当前款 = 打开再次创作并预选它,**不静默重做**:
                           重做要花钱,一次点击直接扣费是不能接受的。 */}
                    {styles.length > 0 && (
                        <Card>
                            <CardContent className="space-y-2.5 p-4">
                                <SectionLabel right={detail?.style.label}>卡面风格</SectionLabel>
                                <CardStylePicker
                                    styles={styles}
                                    value={detail?.style.key || ''}
                                    onChange={key => {
                                        // 点当前这一款 = 没有要改的意思,不弹重做
                                        if (key === detail?.style.key) return;
                                        setRegenStyle(key);
                                        setRegenOpen(true);
                                    }}
                                    columns={2}
                                    testIdPrefix="side-style"
                                    footnote={regenPoints !== null
                                        ? `换风格要整条重做一版，${regenPoints} 算力。点一下先看确认框，不会直接扣。`
                                        : '换风格要整条重做一版。点一下先看确认框，不会直接扣。'}
                                />
                            </CardContent>
                        </Card>
                    )}
                </div>}

                {/* ═══════════ 中栏 = 预览 ═══════════
                    大图 + 缩略条 + 手机预览。
                    🔴 sticky 留着,而且理由变了:原来是怕窗口矮时把中栏底部的
                       「去发布投放」顶出可视区;现在那颗按钮搬去了右栏,sticky 的用处
                       变成**右栏改文案时预览不跑掉** —— 改哪一句、手机上长什么样,同屏可见。 */}
                <div className="space-y-3 @5xl:sticky @5xl:top-4 @5xl:self-start
                                @5xl:max-h-[calc(100vh-2rem)] @5xl:overflow-y-auto"
                    data-testid="detail-col-preview">
                    {/*
                      * 🔴 [A6] 选中的是还没做出来的选题 ⇒ 这里**不显示当前这篇的图**。
                      *    显示它会让人以为"这就是我选的那条做出来的样子" —— 那是张冠李戴。
                      *    改成显示那一档风格的**样图**,并明说还没做。
                      */}
                    {showStyleSample && (
                        <Card data-testid="preview-not-made">
                            <CardContent className="space-y-3 p-4">
                                <SectionLabel>{pickedTopic?.title || '这一条'}</SectionLabel>
                                <p className="text-xs text-muted-foreground" data-testid="preview-not-made-note">
                                    {topicView.note}
                                </p>
                                {STYLE_SAMPLES[pickedTopic?.styleKey || ''] ? (
                                    <img src={STYLE_SAMPLES[pickedTopic?.styleKey || '']} alt=""
                                        data-testid="preview-style-sample"
                                        className="block w-full max-w-[300px] rounded-lg border border-border object-cover" />
                                ) : (
                                    <p className="text-xs text-muted-foreground" data-testid="preview-style-sample-none">
                                        这一条还没选卡面风格,系统会按客户资料自动挑一款。
                                    </p>
                                )}
                            </CardContent>
                        </Card>
                    )}
                    {!showStyleSample && (<>
                    <Card>
                        <CardContent className="space-y-3 p-4">
                            {/* 大图 */}
                            <div ref={stageRef} className="relative overflow-hidden rounded-xl border bg-muted/30">
                                {previews[activeIdx] ? (
                                    <img
                                        src={previews[activeIdx]}
                                        alt={`第 ${activeIdx + 1} 张`}
                                        className="mx-auto block max-h-[420px] w-auto object-contain"
                                    />
                                ) : (
                                    <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
                                        这条内容还没有图
                                    </div>
                                )}
                                {total > 1 && (
                                    <>
                                        <button
                                            type="button" onClick={() => step(-1)}
                                            aria-label="上一张"
                                            className="absolute left-2 top-1/2 -translate-y-1/2 rounded-full bg-background/80 p-1.5 shadow hover:bg-background"
                                        >
                                            <ChevronLeft className="h-4 w-4" />
                                        </button>
                                        <button
                                            type="button" onClick={() => step(1)}
                                            aria-label="下一张"
                                            className="absolute right-2 top-1/2 -translate-y-1/2 rounded-full bg-background/80 p-1.5 shadow hover:bg-background"
                                        >
                                            <ChevronRight className="h-4 w-4" />
                                        </button>
                                    </>
                                )}
                                {total > 0 && (
                                    <span
                                        className="absolute right-2 top-2 rounded-full bg-background/85 px-2 py-0.5 text-xs tabular-nums"
                                        data-testid="stage-counter"
                                    >
                                        {activeIdx + 1}/{total}
                                    </span>
                                )}
                            </div>

                            {/* 黄点提示:只有后端 checked 才会出现 */}
                            {activeFlags && activeFlags.length > 0 && (
                                <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-2.5 text-sm">
                                    <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-amber-500" />
                                    <div className="min-w-0 flex-1">
                                        <p className="text-foreground">
                                            这条参数与资料库不一致，建议改文字
                                        </p>
                                        <p className="mt-0.5 truncate text-xs text-muted-foreground">
                                            对不上的是：{activeFlags.join('、')}
                                        </p>
                                        <button
                                            type="button"
                                            className="mt-1 text-xs text-primary underline underline-offset-2"
                                            onClick={() => {
                                                document.getElementById('douyin-copy-editor')
                                                    ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
                                                document.getElementById('douyin-body-input')?.focus();
                                            }}
                                            data-testid="goto-copy-editor"
                                        >
                                            去修改正文 →
                                        </button>
                                    </div>
                                </div>
                            )}

                            {/* 缩略条:点一下 = 大图与手机预览同时跳张
                                🔴 2026-08-03 修:原来是 `h-20 w-16`(4:5)+ `object-cover`,
                                   而卡片实际是 **3:4** —— 于是每一张缩略图的上下都被裁掉,
                                   用户看到的排版和真图不一样。改成按画幅给比例 + object-cover
                                   只在等比时才不裁。
                                🔴 标签原来是 `headline.slice(0, 5)`,把正文标题硬切五个字,
                                   出来的是半截词。改成**职责名**(封面/参数对比/收尾…) ——
                                   series_plan 已经把 role_label 回填在卡上了,那才是这张卡是什么。 */}
                            {total > 0 && (
                                <div className="flex gap-2 overflow-x-auto pb-1">
                                    {previews.map((u, i) => {
                                        const meta = cardsMeta[i];
                                        const flagged = flagMap.has(i);
                                        const label = meta?.role_label
                                            || KIND_LABEL[meta?.kind || '']
                                            || `第 ${i + 1} 张`;
                                        const active = i === activeIdx;
                                        return (
                                            <button
                                                key={i} type="button"
                                                onClick={() => setActiveIdx(i)}
                                                data-testid={`thumb-${i}`}
                                                title={meta?.headline || label}
                                                className="group shrink-0 cursor-pointer"
                                            >
                                                <span className={`relative block overflow-hidden rounded-lg border-2
                                                                  transition ${active
                                                        ? 'border-primary ring-2 ring-primary/25'
                                                        : 'border-transparent group-hover:border-muted-foreground/40'}`}>
                                                    <img
                                                        src={u} alt=""
                                                        className="block h-[74px] w-auto object-contain"
                                                    />
                                                    {flagged && (
                                                        <span className="absolute right-1 top-1 h-2 w-2 rounded-full
                                                                         bg-amber-500 ring-2 ring-background" />
                                                    )}
                                                </span>
                                                <span className={`mt-1 block max-w-[76px] truncate text-center text-[11px]
                                                                  ${active ? 'text-foreground' : 'text-muted-foreground'}`}>
                                                    {label}
                                                </span>
                                            </button>
                                        );
                                    })}
                                </div>
                            )}
                        </CardContent>
                    </Card>

                    <Card>
                        <CardContent className="space-y-3 p-4">
                            {/* 风格名 2026-08-04 从这里去掉:右栏「卡面风格」那张卡
                                已经把当前款高亮出来了,顶栏也写着「当前风格：X」——
                                同一件事在一屏里出现三遍,第三遍不提供任何新信息。 */}
                            <SectionLabel>发布预览</SectionLabel>

                            {/* 🔴 内嵌账号选择器与频控预检已整块删除（2026-08-03）。
                                根因不是“选错池”也不是“漏筛选”：platform='抖音' + can_tuwen=1
                                这两刀是对的（池里 12 个平台、13,702 个号发不了图文），
                                真凶是**漏分页** —— 合格账号 2,561 个，而端点 limit=50、
                                前端拉一次就不再拉。发布中心本来就有完整的筛选/分页/价格/
                                频控/下单链，在它前面套一个窄窗口本身就是错的。 */}

                            {/* 手机预览:与大图共用 activeIdx;文案直接读编辑区 state
                                🔴 2026-08-03 重做。原来它只是"一个黑色圆角盒子里塞了张图" ——
                                   状态栏是 `▮▮▮` 三个占位方块,没有顶部导航、没有底部 tab、
                                   互动栏浮在图上。用户看的是"这条发出去长什么样",
                                   而那个盒子给不出这个判断。现在按真机结构补齐:
                                   状态栏 → 关注/推荐 → 图 + 右侧互动栏 → 文案 → 底部 tab。
                                🔴 但**一个数字都不编**:互动数恒 `—`(我们拿不到真实数据)。 */}
                            {/* 🔴 2026-08-04 修比例。Owner:「这里的比例不像手机」——
                                量了一下确实:机身宽 286，里面 3:4 的图按原比例撑到 381 高，
                                加状态栏/导航/tab 一共约 455，机身就成了 **1:1.59**；
                                真机是 1:2.16（9:19.5）。所以看着又矮又胖。
                                改成机身**固定 9:19.5**、图在屏幕区里等比居中。
                                这同时也更真实:3:4 的图发到抖音上本来就是上下留黑，
                                原来那种"图撑满整个机身"是现实里不存在的样子。 */}
                            <div className="mx-auto w-full max-w-[262px]">
                                <div className="flex aspect-[9/19.5] flex-col overflow-hidden
                                                rounded-[30px] border-[5px] border-foreground/80
                                                bg-black shadow-xl shadow-black/40"
                                     data-testid="phone-frame">
                                    {/* 状态栏 */}
                                    <div className="flex items-center justify-between bg-black px-4 pb-1 pt-2">
                                        <span className="text-[10px] font-medium tabular-nums text-white">9:41</span>
                                        <PhoneStatusIcons />
                                    </div>
                                    {/* 顶部导航:抖音的「关注 / 推荐」 */}
                                    <div className="flex items-center justify-center gap-5 bg-black pb-1.5 pt-0.5">
                                        <span className="text-[11px] text-white/45">关注</span>
                                        <span className="relative text-[11px] font-medium text-white">
                                            推荐
                                            <span className="absolute -bottom-1 left-1/2 h-[2px] w-4 -translate-x-1/2
                                                             rounded-full bg-white" />
                                        </span>
                                    </div>

                                    {/* 屏幕区:占满机身剩下的高度,图在里面等比居中(上下留黑)。
                                        min-h-0 不能省 —— flex 子项默认 min-height:auto,
                                        不置 0 的话图会把机身撑破,aspect 就白设了。 */}
                                    <div className="relative flex min-h-0 flex-1 items-center
                                                    justify-center bg-black">
                                        {previews[activeIdx] ? (
                                            <img
                                                src={previews[activeIdx]}
                                                alt=""
                                                className="block max-h-full max-w-full object-contain"
                                            />
                                        ) : (
                                            <div className="flex items-center justify-center text-xs text-white/50">
                                                还没有图
                                            </div>
                                        )}
                                        {total > 1 && (
                                            <>
                                                <button
                                                    type="button" onClick={() => step(-1)}
                                                    aria-label="上一张"
                                                    className="absolute left-1 top-1/2 -translate-y-1/2 rounded-full
                                                               bg-black/45 p-1 text-white transition hover:bg-black/70"
                                                >
                                                    <ChevronLeft className="h-3.5 w-3.5" />
                                                </button>
                                                <button
                                                    type="button" onClick={() => step(1)}
                                                    aria-label="下一张"
                                                    className="absolute right-1 top-1/2 -translate-y-1/2 rounded-full
                                                               bg-black/45 p-1 text-white transition hover:bg-black/70"
                                                >
                                                    <ChevronRight className="h-3.5 w-3.5" />
                                                </button>
                                            </>
                                        )}
                                        {total > 0 && (
                                            <span
                                                className="absolute left-2 top-2 rounded-full bg-black/60 px-2 py-0.5
                                                           text-[10px] tabular-nums text-white"
                                                data-testid="phone-counter"
                                            >
                                                {activeIdx + 1}/{total}
                                            </span>
                                        )}
                                        <PhoneActionRail />

                                        {/* 文案压在图下沿(真机就是这样),不是另起一块白底 */}
                                        {/* pr-12 给右侧互动栏让位 —— 不留的话文案会压到图标底下 */}
                                        <div className="absolute inset-x-0 bottom-0 space-y-1
                                                        bg-gradient-to-t from-black via-black/85 to-transparent
                                                        px-3 pb-3 pr-12 pt-8 text-white">
                                            {/* 🔴 账号是在**发布中心**选的,这一步我们还不知道会发哪个号。
                                                参考图里写的 `@秦林生物` 是示意图的编造值 ——
                                                照抄就是假数据。写成待选,顺带把这条流程讲清楚。 */}
                                            <p className="text-[11px] font-medium text-white/60">
                                                @待选账号
                                            </p>
                                            <p className="line-clamp-2 text-[11px] font-medium leading-snug"
                                               data-testid="phone-title">
                                                {title || '（还没写标题）'}
                                            </p>
                                            <p className="line-clamp-2 text-[10px] leading-relaxed text-white/70"
                                               data-testid="phone-body">
                                                {bodyText || '（还没写正文）'}
                                            </p>
                                            {tags.length > 0 && (
                                                <p className="line-clamp-1 text-[10px] text-sky-300/90">
                                                    {tags.map(t => `#${t}`).join(' ')}
                                                </p>
                                            )}
                                            <p className="flex items-center gap-1 pt-0.5 text-[9px] text-white/50">
                                                <Music2 className="h-2.5 w-2.5" />原声
                                            </p>
                                        </div>
                                    </div>
                                    <PhoneTabBar />
                                </div>
                            </div>
                        </CardContent>
                    </Card>
                    </>)}
                </div>

                {/* ═══════════ 右栏 = 重要内容 ═══════════
                    标题 / 正文 / 标签 / 联系方式(从原左栏搬来)+ 去发布投放 + 资料卡 + 发布前确认。
                    🔴 文案仍然**只有一份 state**:预览直接读它,这里只是换了摆放的位置。 */}
                <div className="space-y-3" data-testid="detail-col-content">
                    {/*
                      * 🔴 [A6] 选中的选题还没做出来 ⇒ 编辑区没有东西可编,
                      *    但**必须说清是为什么**。一个灰着且一个字都不说的编辑区,
                      *    和坏了长得一模一样(#203 那颗灰按钮刚栽过同一件事)。
                      */}
                    {!topicView.canEdit && pickedTopic && (
                        <Card data-testid="content-locked">
                            <CardContent className="space-y-2 p-4">
                                <SectionLabel>文案</SectionLabel>
                                <p className="text-xs text-muted-foreground" data-testid="content-locked-reason">
                                    {topicView.reason}
                                </p>
                            </CardContent>
                        </Card>
                    )}
                    {/* 文案编辑区 */}
                    <Card id="douyin-copy-editor"
                        className={cn(!topicView.canEdit && pickedTopic && 'pointer-events-none opacity-50')}
                        data-testid="copy-editor-card"
                        data-editable={!pickedTopic || topicView.canEdit ? 'true' : 'false'}>
                        <CardContent className="space-y-3 p-4">
                            <div className="space-y-1.5">
                                <FieldLabel
                                    right={
                                        <span className={titleHintOk ? 'text-emerald-600' : 'text-amber-600'}>
                                            {titleLen} 字 · 建议 {TITLE_MIN_SUGGEST}-{TITLE_HARD_LIMIT} 字
                                        </span>
                                    }
                                >
                                    标题
                                </FieldLabel>
                                <Input
                                    value={title}
                                    maxLength={TITLE_HARD_LIMIT}
                                    onChange={e => { setTitle(e.target.value); setDirty(true); }}
                                    data-testid="title-input"
                                />
                            </div>

                            <div className="space-y-1.5">
                                <FieldLabel right={`${bodyText.length} 字`}>正文</FieldLabel>
                                <Textarea
                                    id="douyin-body-input"
                                    value={bodyText}
                                    rows={6}
                                    onChange={e => { setBodyText(e.target.value); setDirty(true); }}
                                    data-testid="body-input"
                                />
                            </div>

                            <div className="space-y-1.5">
                                <FieldLabel right={tags.length ? `${tags.length} 个` : undefined}>
                                    话题标签
                                </FieldLabel>
                                <div className="flex flex-wrap items-center gap-2">
                                    {tags.map((t, i) => (
                                        <Badge
                                            key={`${t}-${i}`} variant="secondary"
                                            className="cursor-pointer"
                                            onClick={() => {
                                                setTags(prev => prev.filter((_, j) => j !== i));
                                                setDirty(true);
                                            }}
                                            title="点一下去掉"
                                        >
                                            #{t}
                                        </Badge>
                                    ))}
                                    <Input
                                        value={newTag}
                                        placeholder="加标签后回车"
                                        className="h-7 w-32 text-xs"
                                        onChange={e => setNewTag(e.target.value)}
                                        onKeyDown={e => {
                                            if (e.key !== 'Enter') return;
                                            e.preventDefault();
                                            const v = newTag.replace(/^#/, '').trim();
                                            if (!v) return;
                                            setTags(prev => prev.includes(v) ? prev : [...prev, v]);
                                            setNewTag('');
                                            setDirty(true);
                                        }}
                                    />
                                </div>
                            </div>

                            <div className="flex items-center justify-between rounded-lg border p-2.5">
                                <div className="min-w-0">
                                    <p className="text-sm">插入联系方式</p>
                                    <p className="text-xs text-muted-foreground">
                                        {detail?.contact.configured
                                            ? '开启后联系方式会排在收尾卡上'
                                            : '这个客户还没填联系方式，先去客户资料里补上'}
                                    </p>
                                </div>
                                <Switch
                                    checked={Boolean(post.contact_enabled)}
                                    disabled={!detail?.contact.configured}
                                    onCheckedChange={v => void toggleContact(v)}
                                    data-testid="contact-switch"
                                />
                            </div>
                            {post.closing_stale && (
                                <div className="flex items-start gap-2 rounded-md border border-amber-500/40 bg-amber-500/5 p-2.5 text-xs">
                                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
                                    <span className="text-muted-foreground">
                                        收尾卡还是改开关之前那张。翻到收尾卡「重抽这张」一次，画面才会跟着变。
                                    </span>
                                </div>
                            )}

                            <Button onClick={save} disabled={!dirty || saving} data-testid="save-copy">
                                {saving
                                    ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                                    : <Save className="mr-2 h-4 w-4" />}
                                保存修改
                            </Button>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardContent className="space-y-3 p-4">
                            {/* 🔴 2026-08-04 本机实测抓到:原判据只有 `!canPublish`,
                                而 `gateReason` 是可以为空的(闸没给理由 / 状态还没拉回来)——
                                于是页面上出现一个**只有感叹号、一个字都没有**的黄框。
                                空提示比不提示更糟:用户知道有事,但不知道是什么事,
                                也不知道该做什么(feedback_hint_must_help_or_hide)。
                                没有理由可说就整块不显示。 */}
                            {!canPublish && gateReason && (
                                <div className="flex items-start gap-2 rounded-md border border-amber-500/30 bg-amber-500/5 p-2.5 text-xs">
                                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" />
                                    <span className="text-muted-foreground">{gateReason}</span>
                                </div>
                            )}

                            {/* Shared publisher has the complete searchable, paged channel pool. */}
                            <Button
                                className="w-full" onClick={goPublish}
                                disabled={jumpBlocked || saving || Boolean(pickedTopic)} data-testid="douyin-goto-publish"
                            >
                                <Send className="mr-2 h-4 w-4" />
                                {saving ? '正在保存…' : dirty ? '保存并去发布投放' : receiptAvailable ? '查看发布进度' : '去发布投放'}
                            </Button>
                            {jumpBlocked && jumpHint && (
                                <p className="text-center text-xs text-muted-foreground">{jumpHint}</p>
                            )}
                            {!jumpBlocked && (
                                <p className="text-center text-[11px] text-muted-foreground">
                                    {receiptAvailable ? '发布状态和失败处理统一在发布投放查看' : '自动带入这条图文，在发布投放选择抖音账号，确认费用后才会发布'}
                                </p>
                            )}

                            {/* 下载全部:**次要位**,在发布按钮下方。
                                🔴 参考图把它画在顶栏 —— 但我们 2026-08-02 专门把它从顶栏
                                   挪下来过,并且有锁钉着
                                   (test_download_all_moved_out_of_top_bar)。
                                   参考图是示意图,不知道这条决定;照抄就是把一条已决事项
                                   悄悄推翻。取参考图的**版式语言**,不取它的信息架构结论。 */}
                            <div className="pt-1 text-center">
                                <a
                                    href={`/api/geo-douyin/posts/${postId}/export`}
                                    className="inline-flex items-center gap-1.5 text-xs text-muted-foreground
                                               underline underline-offset-2 transition hover:text-foreground"
                                    data-testid="download-all"
                                >
                                    <Download className="h-3.5 w-3.5" />下载全部
                                </a>
                            </div>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardContent className="space-y-3 p-4">
                            <SectionLabel>知识库 / 写作资料</SectionLabel>

                            {/* 🔴 取失败 ≠ 没资料。两者必须分开显示,否则一个 SQL 错
                                会长期表现成"这个客户没填资料"(smallint 事故的同型教训)。 */}
                            {knowledge?.load_failed && (
                                <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-2 text-xs">
                                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-destructive" />
                                    <span className="text-muted-foreground">
                                        资料没读出来（不是没填），刷新看看，还不行就找我们
                                    </span>
                                </div>
                            )}

                            {/* 🔴 原来这三格是**带边框的方块格子**,和右下角真正可点的
                                「补全资料 / 确认链接」长得一模一样 —— 用户会去点它们。
                                它们是只读状态,就该长成 chip。 */}
                            <div className="flex flex-wrap gap-1.5">
                                <StatChip
                                    icon={FileText}
                                    tone={knowledge && knowledge.materials.total > 0
                                        && knowledge.materials.filled >= knowledge.materials.total
                                        ? 'ok' : 'muted'}
                                >
                                    <span className="tabular-nums" data-testid="kb-materials">
                                        {knowledge && knowledge.materials.total > 0
                                            ? `资料 ${knowledge.materials.filled}/${knowledge.materials.total}`
                                            : '资料 —'}
                                    </span>
                                </StatChip>
                                <StatChip
                                    icon={Phone}
                                    tone={detail?.contact.configured ? 'ok' : 'muted'}
                                >
                                    {detail?.contact.configured ? '联系方式已配置' : '联系方式待填'}
                                </StatChip>
                                <StatChip icon={ImageIcon}>
                                    <span className="tabular-nums" data-testid="kb-images">
                                        {knowledge ? `图片 ${knowledge.images.count} 张` : '图片 —'}
                                    </span>
                                </StatChip>
                            </div>

                            {/* 这条内容【实际用到】了什么 —— 取生产时的真实留痕,
                                不是"这个客户有什么"。有资料 ≠ 这条用上了。 */}
                            {knowledge && knowledge.sources_used.length > 0 && (
                                <div className="space-y-1">
                                    <FieldLabel>这条内容用到了</FieldLabel>
                                    <div className="flex flex-wrap gap-1">
                                        {knowledge.sources_used.map(s => (
                                            <Badge key={s} variant="secondary" className="text-[10px]">
                                                {s}
                                            </Badge>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {/* 资料填充明细:缺哪几项一眼看到,才知道该去补什么 */}
                            {knowledge && knowledge.materials.items.length > 0 && (
                                <div className="space-y-1">
                                    <FieldLabel>资料清单</FieldLabel>
                                    <div className="flex flex-wrap gap-1">
                                        {knowledge.materials.items.map(it => (
                                            /* 🔴 主次原来是反的:已填的用绿色(最响),没填的用灰色划掉(最轻)。
                                               可要用户去补的**是没填的那几项** —— 该响的是它们。
                                               现在:已填 = 安静的中性色(它只是背景事实),
                                                     没填 = 琥珀色(这是待办)。 */
                                            <span
                                                key={it.key}
                                                className={`rounded px-1.5 py-0.5 text-[10px] ${it.present
                                                    ? 'bg-muted/60 text-muted-foreground'
                                                    : 'bg-amber-500/10 text-amber-600 dark:text-amber-400'
                                                    }`}
                                                title={it.present ? '已填' : '还没填'}
                                            >
                                                {it.label}
                                            </span>
                                        ))}
                                    </div>
                                </div>
                            )}

                            {detail?.contact.display && (
                                <div className="space-y-1">
                                    <FieldLabel>联系方式</FieldLabel>
                                    <p className="text-sm">{detail.contact.display}</p>
                                </div>
                            )}

                            {/* 参考图片缩略图(只列已授权且已确权的) */}
                            {knowledge && knowledge.images.thumbs.length > 0 && (
                                <div className="space-y-1">
                                    <FieldLabel right={`${knowledge.images.count} 张`}>
                                        参考图片
                                    </FieldLabel>
                                    <div className="flex gap-1.5 overflow-x-auto pb-1">
                                        {knowledge.images.thumbs.map((im, i) => (
                                            <img
                                                key={i} src={im.thumb} alt={im.alt}
                                                className="h-12 w-12 shrink-0 rounded border object-cover"
                                                data-testid={`kb-thumb-${i}`}
                                            />
                                        ))}
                                    </div>
                                </div>
                            )}

                            {consistency && !consistency.checked && (
                                <p className="rounded-md bg-muted/50 p-2 text-xs text-muted-foreground">
                                    {consistency.reason}
                                </p>
                            )}

                            {/* 规范 §8.6-10:卡面与文案对不上。**提示级**,不拦发布 ——
                                与广告法扫描同一档(Owner 08-03:发不发由客户)。
                                🔴 只在 checked && 有问题 时出现:没核过就什么都不显示,
                                   绝不把"没核"渲染成"核过没问题"。 */}
                            {consistency?.alignment?.checked
                                && consistency.alignment.issues.length > 0 && (
                                <div className="space-y-1 rounded-md border border-amber-500/40 bg-amber-500/5 p-2"
                                     data-testid="alignment-issues">
                                    <p className="text-xs font-medium text-amber-600">
                                        卡面和文案有 {consistency.alignment.issues.length} 处对不上
                                    </p>
                                    {consistency.alignment.issues.map((it, i) => (
                                        <p key={i} className="text-[11px] leading-relaxed text-muted-foreground">
                                            {it.detail}
                                        </p>
                                    ))}
                                </div>
                            )}

                            {/* 规范 §8.6-11:卡面文字逐字核验。按需触发、不收费。
                                🔴 三态严格分开:没核过(ocr===null)什么都不显示;
                                   核成了且有缺失 → 列出来;核成了且没缺失 → 说"都对上了"。
                                   `checked=false` 归到第一类,绝不显示成绿的。 */}
                            <div className="space-y-1.5">
                                <Button
                                    variant="outline" size="sm"
                                    className="w-full text-xs"
                                    disabled={ocrRunning}
                                    onClick={() => { void runOcrCheck(); }}
                                    data-testid="ocr-check-run"
                                >
                                    {ocrRunning ? '正在核对卡面文字…' : '核对卡面文字'}
                                </Button>
                                {ocr && !ocr.checked && (
                                    <p className="rounded-md bg-muted/50 p-2 text-xs text-muted-foreground"
                                       data-testid="ocr-not-checked">
                                        {ocr.reason}
                                    </p>
                                )}
                                {ocr?.checked && ocr.flagged_count === 0 && (
                                    <p className="text-[11px] text-muted-foreground"
                                       data-testid="ocr-all-good">
                                        核了 {ocr.checked_count} 张，文字都排上了
                                    </p>
                                )}
                                {ocr?.checked && ocr.flagged_count > 0 && (
                                    <div className="space-y-1 rounded-md border border-amber-500/40 bg-amber-500/5 p-2"
                                         data-testid="ocr-missing">
                                        <p className="text-xs font-medium text-amber-600">
                                            有 {ocr.flagged_count} 张的文字没排全，建议重抽这几张
                                        </p>
                                        {/*
                                          * 🔴 [WO_262] 客户原话:「系统让我重抽,但没告诉我要重抽什么?重抽哪一部分」。
                                          *    原来这里是 `missing.join('、')` —— 多条缺失被拼成一行,
                                          *    而缺失项本身常常就带顿号/标点,拼完根本分不出边界;
                                          *    再加上「建议重抽这几张」只是一句话,用户得自己去上面找第 N 张。
                                          *    ⇒ ① 逐条一行(带引号,标出原文边界)② 每张给一颗直达按钮。
                                          * 🔴 条目里若出现 `['…','…']` 这种列表 repr,**照原样当普通文本显示**:
                                          *    那是后端归一化漏了(WO_262 C 侧在修),前端不解析 ——
                                          *    前端一解析,后端那个缺陷就永远没人看见。
                                          */}
                                        {ocr.cards.filter(c => c.checked && !c.ok).map(c => (
                                            <div key={c.card_index} className="space-y-1"
                                                 data-testid={`ocr-missing-card-${c.card_index}`}>
                                                <div className="flex items-start justify-between gap-2">
                                                    <p className="text-[11px] font-medium leading-relaxed text-muted-foreground">
                                                        第 {c.card_index + 1} 张图上没排上这些文字：
                                                    </p>
                                                    <Button
                                                        size="sm" variant="outline"
                                                        className="h-6 shrink-0 px-2 text-[11px]"
                                                        data-testid={`ocr-redraw-card-${c.card_index}`}
                                                        onClick={() => { setActiveIdx(c.card_index); setRedrawOpen(true); }}>
                                                        重抽这一张
                                                    </Button>
                                                </div>
                                                <ul className="list-disc space-y-0.5 pl-4">
                                                    {c.missing.map((m, i) => (
                                                        <li key={i} data-testid="ocr-missing-item"
                                                            className="text-[11px] leading-relaxed text-muted-foreground">
                                                            「{m}」
                                                        </li>
                                                    ))}
                                                </ul>
                                            </div>
                                        ))}
                                    </div>
                                )}
                            </div>

                            <div className="flex gap-2">
                                <Button variant="outline" size="sm" className="flex-1 text-xs" asChild>
                                    <a href={post.brand_id ? `/my-clients/${post.brand_id}` : '/my-clients'}>
                                        补全资料
                                    </a>
                                </Button>
                                <Button variant="outline" size="sm" className="flex-1 text-xs" asChild>
                                    <a href={post.brand_id ? `/my-clients/${post.brand_id}` : '/my-clients'}>
                                        <Link2 className="mr-1 h-3 w-3" />确认链接
                                    </a>
                                </Button>
                            </div>
                        </CardContent>
                    </Card>

                    <Card>
                        <CardContent className="space-y-2 p-4">
                            <SectionLabel>发布前确认</SectionLabel>
                            {[
                                { ok: total > 0, text: total > 0 ? '图片已生成，请检查顺序和内容' : '还没有卡片' },
                                { ok: Boolean(title && bodyText), text: dirty ? '文案有未保存修改' : '文案已载入，请检查内容' },
                                // 账号与频次改到发布中心选/校验了,这里只确认"这条能不能去投放"。
                                // §15:补齐中的组不放行 —— 缺图发出去就是残组,而且钱还没结算。
                                {
                                    ok: !completing && total > 0,
                                    text: completing
                                        ? `还有 ${pendingCards} 张没出来，补齐了再发`
                                        : '图片齐了，可以去投放',
                                },
                                {
                                    ok: Boolean(post.contact_enabled) && !post.closing_stale,
                                    text: post.contact_enabled
                                        ? (post.closing_stale ? '联系方式：待重抽收尾卡' : '联系方式：已插入')
                                        : '联系方式：未插入',
                                },
                            ].map((row, i) => (
                                <div key={i} className="flex items-center gap-2 text-sm">
                                    <CheckCircle2
                                        className={`h-4 w-4 shrink-0 ${row.ok ? 'text-emerald-600' : 'text-muted-foreground/40'}`}
                                    />
                                    <span className={row.ok ? '' : 'text-muted-foreground'}>{row.text}</span>
                                </div>
                            ))}
                            <p className="pt-1 text-xs text-muted-foreground">
                                确认无误后即可发布，这里不阻断编辑。
                            </p>
                        </CardContent>
                    </Card>
                </div>
            </div>
            </div>

            {/* ═══════════ 重抽弹窗 ═══════════ */}
            <Dialog open={redrawOpen} onOpenChange={setRedrawOpen}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>重抽第 {activeIdx + 1} 张</DialogTitle>
                    </DialogHeader>
                    <div className="space-y-3">
                        {/*
                          * 🔴 [WO_262] 弹窗顶部先说**为什么让你重抽这一张**。
                          *    原来面板说「建议重抽这几张」、弹窗只说「只换这一张,消耗 N 算力,想怎么改?」——
                          *    两处不通,用户在弹窗里看不到任何原因,于是问「重抽什么?重抽哪一部分」。
                          * 🔴 `redrawHint` 仍然**默认留空**:缺了哪几行是事实,而「想怎么改」是客户的意思,
                          *    替他预填等于替他做了决定,而且他多半不会去改。
                          */}
                        {activeMissing.length > 0 && (
                            <div className="space-y-1 rounded-md border border-amber-500/40 bg-amber-500/5 p-2"
                                 data-testid="redraw-reason">
                                <p className="text-xs font-medium text-amber-600">
                                    第 {activeIdx + 1} 张图上没排上这些文字：
                                </p>
                                <ul className="list-disc space-y-0.5 pl-4">
                                    {activeMissing.map((m, i) => (
                                        <li key={i} data-testid="redraw-reason-item"
                                            className="text-[11px] leading-relaxed text-muted-foreground">
                                            「{m}」
                                        </li>
                                    ))}
                                </ul>
                                <p className="text-[11px] leading-relaxed text-muted-foreground">
                                    重抽会把这张重新排版，想改内容再填，不改就留空。
                                </p>
                            </div>
                        )}
                        {/* 🔴 静默扣费(2026-06-03 拍板废除按钮级前置确认),但**价格必须看得见**。
                            这里不是确认框,是把价钱摆在动手之前的最后一眼。 */}
                        <p className="text-sm text-muted-foreground">
                            只换这一张，其他卡不动。
                            {redrawPoints !== null
                                ? <>本次消耗 <span className="font-medium text-foreground">{redrawPoints} 算力</span>。</>
                                : '（价格暂时读不到，稍后再试）'}
                            {' '}这条内容还能重抽 {Math.max(0, redrawLimit - redrawUsed)} 次。
                        </p>
                        <Textarea
                            value={redrawHint}
                            rows={3}
                            placeholder="想怎么改？例：去掉价格，换成工期说明（留空就随机换一版）"
                            onChange={e => setRedrawHint(e.target.value)}
                            data-testid="redraw-hint"
                        />
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setRedrawOpen(false)}>取消</Button>
                        <Button onClick={doRedraw} disabled={redrawing} data-testid="redraw-submit">
                            {redrawing && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                            开始重抽
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>

            {/* ═══════════ 再次创作弹窗 ═══════════ */}
            <Dialog open={regenOpen} onOpenChange={setRegenOpen}>
                <DialogContent>
                    <DialogHeader>
                        <DialogTitle>再次创作</DialogTitle>
                    </DialogHeader>
                    <div className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                            整条重新做一版（文案和全部卡片都会换）。
                            {regenPoints !== null
                                ? <>本次消耗 <span className="font-medium text-foreground">{regenPoints} 算力</span>。</>
                                : '（价格暂时读不到，稍后再试）'}
                        </p>

                        {styles.length > 0 && (
                            <div className="space-y-2">
                                <p className="text-sm font-medium">卡面风格</p>
                                <CardStylePicker
                                    styles={styles}
                                    value={regenStyle}
                                    onChange={setRegenStyle}
                                    columns={4}
                                />
                            </div>
                        )}

                        {/* 🔴 P2-2:榜单作品的默认值**来自这条作品的快照**。
                            不显示这一块的话,用户在弹窗里看到的是"什么都没选",
                            于是他合理地以为重做出来是普通图文 —— 而实际会继承榜单。
                            界面必须和后端的继承行为长得一样。 */}
                        {detail?.ranking?.content_form === 'ranking' && (
                            <div className="space-y-2" data-testid="regen-ranking">
                                <p className="text-sm font-medium">榜单设置</p>
                                <p className="text-[11px] leading-relaxed text-muted-foreground"
                                   data-testid="regen-ranking-inherit">
                                    沿用这条原来的设置：点名 {detail.ranking.entity_count} 家
                                    {detail.ranking.entity_count_actual > 0
                                        && detail.ranking.entity_count_actual !== detail.ranking.entity_count
                                        && `（上次实际做出 ${detail.ranking.entity_count_actual} 家）`}
                                    ，面向 {detail.ranking.engine_label}
                                </p>
                                <select
                                    value={regenTemplate ?? detail.ranking.template}
                                    onChange={e => setRegenTemplate(e.target.value)}
                                    data-testid="regen-ranking-template"
                                    className="h-9 w-full rounded-md border border-input
                                               bg-background px-2 text-sm"
                                >
                                    <option value="">自动（推荐）</option>
                                    {rankingTemplates.map(t => (
                                        <option key={t.key} value={t.key}>{t.label}</option>
                                    ))}
                                </select>
                                <p className="text-[11px] leading-relaxed text-muted-foreground">
                                    不动它就沿用原来的版式
                                    {detail.ranking.template_label
                                        && `（${detail.ranking.template_label}）`}
                                </p>
                            </div>
                        )}

                        <div className="space-y-1.5">
                            <p className="text-sm font-medium">补充要求（选填）</p>
                            <Textarea
                                value={regenHint}
                                rows={3}
                                placeholder="例：多讲工期和售后，少讲价格"
                                onChange={e => setRegenHint(e.target.value)}
                                data-testid="regen-hint"
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setRegenOpen(false)}>取消</Button>
                        <Button onClick={doRegenerate} disabled={regenerating} data-testid="regen-submit">
                            {regenerating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                            确认重做{regenPoints !== null ? ` · ${regenPoints} 算力` : ''}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </div>
    );
}

export default DouyinPostDetail;
