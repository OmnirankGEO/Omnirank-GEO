/**
 * 题单「侧别」守卫的**纯逻辑**(工单 WO_QUESTION_PLAN_PROMPT_2026-09-02 §3.1.4/3.1.5)。
 *
 * 为什么单独一个无 React 依赖的模块 —— 与同目录 `defensiveLaunchGate.ts` 同一理由:
 * 塞在 1900 行的 .tsx 里,判据要么去 bundle 整棵 React 依赖树,要么只能读源码串,
 * 而读串证明不了分支真的会那样走。
 *
 * 🔴 这里**不重复后端的规则本体**(`services/defensive_geo/question_plan.py:230-262`
 * 是身份/计价安全线,不能有第二份)。这里只做两件前端该做的事:
 *   ① 上游预防:删到某一侧为空之前就拦住,让那条通用错误**根本没机会发生**(DEV_PRINCIPLES :76);
 *   ② 收到后端错误时,把 `nextAction.target` 翻成"该聚焦/该补哪一侧",给出一个能点的修复动作(:83)。
 */

export type ModeSide = 'defensive' | 'offensive';
export type PlanMode = 'defensive' | 'offensive' | 'hybrid';

export interface SideBearing {
    modeSide: ModeSide | string;
    text?: string;
}

/** 各侧**非空题**的条数。空白题不算数 —— 后端也不认(`plan_question_blank`)。 */
export function sideCounts(questions: readonly SideBearing[]): Record<ModeSide, number> {
    const out: Record<ModeSide, number> = { defensive: 0, offensive: 0 };
    for (const q of questions) {
        if (!q || typeof q.text !== 'string' || q.text.trim().length === 0) continue;
        if (q.modeSide === 'defensive' || q.modeSide === 'offensive') out[q.modeSide] += 1;
    }
    return out;
}

/**
 * hybrid 下删掉 `removing` 之后,是否会让某一侧变空?会 ⇒ 返回那一侧,否则 null。
 *
 * 🔴 只在 `hybrid` 生效:单模式下另一侧本来就该是空的,拦它会把正常操作judged成错误
 *    —— 那正是「隔离措施自己制造假象」那一类。
 */
export function wouldEmptySide(
    mode: PlanMode,
    all: readonly SideBearing[],
    removing: SideBearing | null | undefined,
): ModeSide | null {
    if (mode !== 'hybrid' || !removing) return null;
    const side = removing.modeSide;
    if (side !== 'defensive' && side !== 'offensive') return null;
    const before = sideCounts(all)[side];
    const removingCounts = typeof removing.text === 'string' && removing.text.trim().length > 0 ? 1 : 0;
    return before - removingCounts <= 0 ? side : null;
}

/**
 * 把后端 `nextAction.target` 翻成"要补哪一侧"。
 *
 * 🔴 **不猜**:target 里没有可识别的侧别就返回 null,由调用方退到"聚焦题单"这个
 *    永远成立的兜底动作。猜错一侧会让按钮把用户带到错的地方,比没有按钮更糟。
 */
export function sideFromTarget(target: unknown): ModeSide | null {
    // 🔴 只读 `target.side` 这一个字段。上面注释里提到的 `plan_question_blank`
    //    是**后端 code**,不进错误信封,**不许按它分支** —— 前端只拿
    //    `publicExplanation`(服务端那句话)与 `nextAction.target.side`。
    //    按 code 分支 = 前端第二次实现后端规则,两份会漂。
    if (!target || typeof target !== 'object') return null;
    const raw = (target as Record<string, unknown>).side
        ?? (target as Record<string, unknown>).mode_side
        ?? (target as Record<string, unknown>).modeSide;
    return raw === 'defensive' || raw === 'offensive' ? raw : null;
}

/** 该侧在 UI 上的人话名字。按钮文案优先用服务端给的 label,这个只作兜底。 */
export function sideLabel(side: ModeSide): string {
    return side === 'defensive' ? '客户会问的' : '客户会搜的';
}

/**
 * 题集签名(WO §3.2 第三态)—— 用来判「上次算的价还对不对得上现在的题单」。
 *
 * 🔴 **顺序无关**:她把两道题上下调换不算"题单变了",价也不会变;
 *    若签名带顺序,面板会在她只是拖动排序时也说"题单变了",那是假过期。
 * 🔴 只取「非空题文 + 侧别」:这两样才是计价与身份看的东西;
 *    空白题不计(后端也不认),否则一个空行就能制造一次假过期。
 */
export function questionSetSignature(qs: readonly SideBearing[]): string {
    return qs
        .map((q) => `${q.modeSide}:${(q.text || '').trim()}`)
        .filter((x) => !x.endsWith(':'))
        .sort()
        .join('|');
}

/**
 * 这份 plan 是不是**按这份 draft** 建出来的(题文+侧别逐条对上,顺序无关、空白不计)。
 *
 * 🔴 为什么不用 `planRevision` 递增来判:revision 是否递增取决于后端的幂等口径,
 *    换了新 plan 时它可能重新从 1 起。**"绑的是不是当前题单"才是要保的那件事**,
 *    而 preview 返回体里就带着 `questions` —— 直接比,不必推断。
 */
export function planMatchesDraft(
    planQuestions: readonly SideBearing[],
    draft: readonly SideBearing[],
): boolean {
    return questionSetSignature(planQuestions) === questionSetSignature(draft);
}

/**
 * 题单内容键(给 `clientRequestId` 用)。
 *
 * 🔴 原先键里只放**题数**(`usable.length`)⇒ 只改题文不改条数时键不变,
 *    幂等会把**改之前**那份 plan 原样取回来,于是"重新算一下"给旧 plan 算了一次价:
 *    请求确实又发了一次(读数好看),钱仍然错。改成按内容取键:
 *    同一份题单 ⇒ 同一个键(幂等性保住);题文一变 ⇒ 键就变。
 */
export function questionSetKey(qs: readonly SideBearing[]): string {
    const sig = questionSetSignature(qs);
    let h = 5381;
    for (let i = 0; i < sig.length; i += 1) h = (((h << 5) + h) ^ sig.charCodeAt(i)) >>> 0;
    return h.toString(36);
}

/**
 * 侧别决定的两个附属字段(阶段④ · 订正八①)。
 *
 * 🔴 单源的理由:新建题(`addManual`)与**改侧别**(题单里的侧别选择器)都要设这两个值。
 *    各写一份的话,用选择器改过侧的题会带着**上一侧的** familyKey / brandExposure ——
 *    后端按 family 分组、按 exposure 判"点没点名",于是那道题会被归错组、
 *    而屏幕上它看起来完全正常(侧别标签已经变了)。
 */
export function sideDefaults(side: ModeSide): { familyKey: string; brandExposure: 'named' | 'unnamed' } {
    return side === 'defensive'
        ? { familyKey: 'trust_reliability', brandExposure: 'named' }
        : { familyKey: 'category_choice', brandExposure: 'unnamed' };
}
