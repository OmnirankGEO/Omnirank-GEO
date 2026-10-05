/**
 * 「客户可能会这样问」的**候选题**来源(#144 · 2026-09-07)。
 *
 * 🔴 为什么要有这个模块 —— 后端 `POST /api/diagnosis/suggest-questions`
 *    (`server.py:4231`)从 2026-09-05 起就在,**全仓零调用方**:
 *    新页只给本地模板,于是 Owner 的原话是「老版本能蒸馏,新版不会」。
 *    生产者是 `tools.keyword_generator.analyze_client_business` 的 `real_user_questions`,
 *    与真正跑体检时用的是同一个 —— 所以候选与实跑同源,不是另编一套。
 *
 * 🔴 两条**契约事实**,想当然会接错:
 *    ① `source = "ai" if questions else "fallback"` ⇒ **`fallback` 时 `questions` 必为空**。
 *       后端**不提供**兜底题面。
 *       🔴 [#149 订正] 这里原来写「兜底示例只能前端出(`buildQuestionExamples`)」——
 *       那个函数与整个示例卡已随 #149 删除。现在取不到候选就是**题单为空**,
 *       屏幕上给原因提示 + 那句真实说明(「题可以不填,系统会按你的行业自动出题(最多 8 道)」),
 *       后端 `mode_default` 会自己出题。**不再有前端兜底题面这回事。**
 *    ② 请求**必须带 `brand_id`(int)**。增长模式下她可能还没有 brand_id ——
 *       那不是失败,是**第三种状态**,判据单列一格,界面上也不该说"取失败了"。
 *
 * 🔴 本模块**不扣算力**(端点自己也不扣)。别在调用点加任何扣费确认。
 */

/** 后端认的三种模式。前端的 `campaignMode` 用的是另一套词,由调用点映射。 */
export type SuggestMode = 'growth' | 'defensive' | 'full';

/**
 * 🔴 [#149] 侧别有**两套词**:后端 `growth | defensive`,前端 `offensive | defensive`。
 *
 *    同一根轴两套词,映射就**只准写一处** —— 散在各处的三元表达式必有一处漏改,
 *    而漏改的那处会把增长题标成防守题,**屏幕上完全正常**(#125 那类静默错)。
 *    两个方向都从这里出:候选进来用 `sideFromBackend`,提交体出去用 `sideToBackend`。
 */
export type BackendSide = 'growth' | 'defensive';
export type FrontSide = 'offensive' | 'defensive';
export function sideFromBackend(side: BackendSide | string | null | undefined): FrontSide | null {
    if (side === 'growth') return 'offensive';
    if (side === 'defensive') return 'defensive';
    return null;
}
export function sideToBackend(side: FrontSide | string | null | undefined): BackendSide | null {
    if (side === 'offensive') return 'growth';
    if (side === 'defensive') return 'defensive';
    return null;
}

export interface SuggestCandidate {
    /** 🔴 后端字段名是 `question`,**不是** `text`(提交体那侧才叫 `text`)。 */
    text: string;
    /** 已映射到**前端**词表;后端没给或给了不认识的值 ⇒ null,调用点按当前模式给默认侧。 */
    side: FrontSide | null;
    /**
     * 🔴 漏斗层。**自由字符串,不是枚举** —— 取值域归生成器所有
     *    (`super_tier1` / `brand_awareness` / …)。前端写死枚举去校验它,
     *    会在生成器新增一层时把整页打成 500(C 明确警告过,#139 那种炸法)。
     *    所以这里只做 `String(...)` + 去空,不做任何取值判断。
     */
    layer: string | null;
}

export interface SuggestQuestionsResult {
    /** 🔴 归一化后的候选。**优先读 `candidates`(C #145 起带 side),回落 `questions`。**
     *  回落不是可选的:本笔上线时线上后端还只返 `questions`,只读 candidates 会当场变成"永远没候选"。 */
    candidates: SuggestCandidate[];
    questions: string[];
    /** `ai` = 真出了题;`fallback` = 没出(此时 questions 必空)。 */
    source: 'ai' | 'fallback';
    /** `exception`(生成器抛了)/ `empty`(生成器返空)—— 两种运维处置不同,别压成一个。 */
    fallbackReason: 'exception' | 'empty' | null;
}

/** 取不到候选的四种原因。`no_brand` 与失败**不是一回事**,文案与判据都要分开。 */
export type SuggestFailure = 'no_brand' | 'empty' | 'exception' | 'network' | 'unauthorized';

export interface SuggestQuestionsFailure {
    kind: SuggestFailure;
    /** 给用户看的话。`no_brand` 一格**不是错误**,说的是"还差一步"。 */
    message: string;
}

/** 前端模式 → 后端模式。`hybrid` 即"全面",防守模式**不调用本接口**(见调用点)。 */
export function toSuggestMode(campaignMode: string): SuggestMode | null {
    if (campaignMode === 'offensive') return 'growth';
    if (campaignMode === 'hybrid') return 'full';
    // defensive:保留品牌名模板,不调接口(工单明确要求)。
    return null;
}

/**
 * 取候选题。**任何失败都不抛** —— 调用点只需要区分「有候选」与「没有,原因是这个」。
 *
 * @param authFetch 由调用点注入,免得本模块依赖 `@/lib/api` 从而把判据拖进整棵依赖树。
 */
export async function fetchSuggestedQuestions(
    authFetch: (url: string, init?: RequestInit) => Promise<Response>,
    input: {
        brandId: number | null | undefined;
        mode: SuggestMode;
        businessScope?: string;
        signal?: AbortSignal;
    },
): Promise<{ ok: true; data: SuggestQuestionsResult } | { ok: false; error: SuggestQuestionsFailure }> {
    const brandId = Number(input.brandId);
    if (!input.brandId || !Number.isFinite(brandId) || brandId <= 0) {
        // 🔴 不是失败:接口按品牌出题,还没有品牌就没得出。文案说"还差一步"。
        return { ok: false, error: { kind: 'no_brand', message: '先填好客户品牌,我再给你候选问题。' } };
    }
    try {
        const res = await authFetch('/api/diagnosis/suggest-questions', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                brand_id: brandId,
                mode: input.mode,
                business_scope: input.businessScope || '',
            }),
            signal: input.signal,
        });
        if (res.status === 401) {
            return { ok: false, error: { kind: 'unauthorized', message: '登录状态过期了,刷新一下再试。' } };
        }
        if (!res.ok) {
            return { ok: false, error: { kind: 'network', message: '这次没能给出候选问题,下面有示例可以照着写。' } };
        }
        const body = await res.json();
        const d = body?.data ?? body;
        // 🔴 先 candidates 后 questions。两者都可能不在(老后端 / 异常),各自独立判空。
        const rawCands: unknown[] = Array.isArray(d?.candidates) ? d.candidates : [];
        const cands: SuggestCandidate[] = rawCands
            .map((c) => {
                const o = (c || {}) as { question?: unknown; text?: unknown; side?: unknown; layer?: unknown };
                // 🔴 后端给的是 `question`;`text` 只作老形状兜底,别指望它。
                const text = String(o.question ?? o.text ?? '').trim();
                const layerRaw = String(o.layer ?? '').trim();
                return { text, side: sideFromBackend(o.side as string), layer: layerRaw || null };
            })
            .filter((c) => c.text);
        const qs: string[] = cands.length > 0
            ? cands.map((c) => c.text)
            : (Array.isArray(d?.questions)
                ? d.questions.map((q: unknown) => String(q ?? '').trim()).filter(Boolean)
                : []);
        if (qs.length === 0) {
            // 契约事实 ①:fallback 时 questions 必空。reason 用后端给的,分不出就记 empty。
            const reason = d?.fallbackReason === 'exception' ? 'exception' : 'empty';
            return {
                ok: false,
                error: {
                    kind: reason,
                    message: reason === 'exception'
                        ? '出题服务这次没响应,下面有示例可以照着写。'
                        : '这个品牌暂时没生成出候选问题,下面有示例可以照着写。',
                },
            };
        }
        return {
            ok: true,
            data: {
                candidates: cands.length > 0 ? cands : qs.map((text) => ({ text, side: null, layer: null })),
                questions: qs,
                source: d?.source === 'ai' ? 'ai' : 'fallback',
                fallbackReason: d?.fallbackReason === 'exception' ? 'exception'
                    : d?.fallbackReason === 'empty' ? 'empty' : null,
            },
        };
    } catch (e) {
        if ((e as { name?: string })?.name === 'AbortError') {
            return { ok: false, error: { kind: 'network', message: '' } };
        }
        return { ok: false, error: { kind: 'network', message: '这次没能给出候选问题,下面有示例可以照着写。' } };
    }
}

/** 提交体里的一条题面元数据。字段名与 `server.py` `QuestionMeta` 逐字对齐。 */
export interface QuestionMetaItem {
    text: string;
    origin: 'ai_suggested' | 'customer';
    side?: 'growth' | 'defensive';
    layer?: string;
}

/**
 * 🔴 [#149] 由题单行构造 `question_meta`。
 *
 * 后端硬约束(C 明确):`question_meta[].text` 必须 **⊆ `custom_questions`**
 * (归一化后:strip → 去空 → 精确去重 → 保序),**多一条少一条都 422**。
 * 所以这里**不自己再归一化一遍** —— 传进来的 `unique` 就是提交体那一份,
 * 逐条按它取,天然 ⊆ 且一一对应。自己再算一次的话,两处归一化口径漂一个字符
 * 就是每次提交必 422,而错误看起来像"服务端抽风"。
 *
 * 🔴 参数用**结构化类型**、不 import `DraftQuestion`:本模块被判据用
 *    data: URL 加载,带相对 import 会解析不了(见 verify-p0 的 loadTs)。
 *
 * origin 映射:`source === 'user'` ⇒ `customer`,其余(`ai` / `system`)⇒ `ai_suggested`。
 * 这条谓词也只写这一处 —— 后端据它决定品牌定向豁免与报告文案,写两处漂了会
 * 把 AI 出的品牌题豁免掉,顶高提及率(工作流 §2.3 的承重墙)。
 */
export function buildQuestionMeta(
    rows: readonly {
        text?: string | null; source?: string | null;
        modeSide?: string | null; layer?: string | null; draft?: boolean;
    }[] | null | undefined,
    unique: readonly string[] | null | undefined,
): QuestionMetaItem[] {
    const byText = new Map<string, { source?: string | null; modeSide?: string | null; layer?: string | null }>();
    for (const r of rows || []) {
        if (r?.draft) continue;                       // 未提交的草稿不进提交体,也就不该有元数据
        const t = (r?.text || '').trim();
        if (!t || byText.has(t)) continue;            // 保序 + 首次出现优先,与去重口径一致
        byText.set(t, r);
    }
    const out: QuestionMetaItem[] = [];
    for (const text of unique || []) {
        const r = byText.get(text);
        const item: QuestionMetaItem = {
            text,
            origin: r?.source === 'user' ? 'customer' : 'ai_suggested',
        };
        const side = sideToBackend(r?.modeSide);
        if (side) item.side = side;
        const layer = (r?.layer || '').trim();
        if (layer) item.layer = layer;
        out.push(item);
    }
    return out;
}

/**
 * 🔴 [#149 返修] 增长线**这次要跑的题** = AI 出的那半 + 她自己写的那半,**按这个顺序**。
 *
 * 为什么要一个这么小的函数:Review 注毒 Pd 把 `planQuestions` 的增长分支改成
 * `[...manualQuestions]`(丢掉 AI 那半)——**我全部判据照绿**。
 * 那时编辑器仍按 `suggested` 显示 AI 那半,而计价/计数/提交体都不含它
 * ⇒ **屏幕上 N 道、按 M 道收钱**。我在注释里点名要防这一格,却没给它上锁:
 * 我锁的是「`planQuestions` 长这个样子」,不是「显示的那份 == 计价的那份」。
 *
 * ⇒ 把「跑哪些」收成**一个可调用的构造器**,显示侧与计价侧都从它出,
 *    判据就能拿真数字对:N + M 条进去,出来必须是 N + M 条且顺序固定。
 *    形状锁挡不住 Pd,数量/顺序锁挡得住。
 */
export function growthRunSet<T>(
    aiSuggested: readonly T[] | null | undefined,
    manual: readonly T[] | null | undefined,
): T[] {
    return [...(aiSuggested || []), ...(manual || [])];
}
