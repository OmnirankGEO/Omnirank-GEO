/**
 * #233-a1 · 建议题单的**纯逻辑**。从 QuestionPlanEditor.tsx 抽出来单独一个模块。
 *
 * 🔴 为什么要抽:工单说「它是纯函数,判据可以直接钉它,不必起浏览器」——
 *    可它原来和 React / lucide / UI 组件住在同一个文件里,判据一 import 就得连整棵 UI 依赖树一起解析,
 *    于是只能退回文本匹配。文本匹配对「插值被删掉」这种改动天生没分辨力。
 *    这里只留 `import type`(转译后被完全擦除)⇒ 本模块**零运行时依赖**,判据能真调它。
 *
 * 🔴 `QuestionPlanEditor` 继续 re-export 这两个符号,既有 import 路径一律不动。
 */
import type { CampaignMode } from './ModeRadioCards';
import type { DraftQuestion } from '@/lib/defensiveGeoApi';

/**
 * 「这道题被用户划掉了」的键。**单一归属在这里**,四个调用点都用它。
 *
 * 🔴 [#233-a1] 原来四处各自内联 `${familyKey}::${text}`。增长题的题面现在随
 *    行业词/城市变化,于是:用户在还没填行业时划掉「这一类服务哪家做得好?」,
 *    填完行业后题面变成「深圳宠物医院哪家做得好?」—— **键对不上,划掉的题复活**。
 *    系统题在 (modeSide, familyKey) 上本来就唯一,所以系统题按 family 记,题面不进键。
 *    AI 出的题不能这么记:同一个 family 下可能有好几道,必须连题面一起。
 */
export function removalKey(q: { familyKey: string; text: string; modeSide?: string; source?: string }): string {
    if (q.source === 'system') return `sys::${q.modeSide ?? ''}::${q.familyKey}`;
    return `${q.familyKey}::${q.text}`;
}

/** 增长侧题面要用到的上下文。都可缺,缺了的后果各自写在下面。 */
export interface SuggestContext {
    /** 行业词。**取不到就不发增长题** —— 见下方红字。 */
    industry?: string;
    /** 客户所在地。只有区域生意才进题面。 */
    location?: string;
    /** 'regional'(默认)| 'national'。全国生意出行业大词,不带城市。 */
    businessScope?: string;
}

export function suggestedQuestions(
    mode: CampaignMode,
    brandName: string,
    ctx: SuggestContext = {},
): DraftQuestion[] {
    const name = brandName?.trim() || '这家公司';
    const defensive: DraftQuestion[] = [
        { text: `${name}靠谱吗?`, modeSide: 'defensive', familyKey: 'trust_reliability', brandExposure: 'named', source: 'system' },
        { text: `${name}是做什么的?`, modeSide: 'defensive', familyKey: 'identity_scope', brandExposure: 'named', source: 'system' },
        { text: `${name}更适合哪些客户?`, modeSide: 'defensive', familyKey: 'fit_for_whom', brandExposure: 'named', source: 'system' },
    ];

    /*
     * 🔴 [#233-a1 · P0 2026-09-17] 增长侧两题原来是**写死的空字符串**:
     *    「这一类服务哪家做得好?」「有什么推荐的吗?」—— 不含行业词、不含地域词、
     *    一个变量都没有。生产实证:AI 对第二题回答的是**王者荣耀游戏推荐**,
     *    而系统据此记「该品牌未被提及」并按 40% 权重扣分 ——
     *    我们还为这道没有主语的题向 4 个付费引擎各调了一次。
     *    不是模板变量没填进去,是**模板本来就没有变量**。
     *
     * 🔴 取不到行业词就**一道都不发**。宁可少发,不发空题:
     *    空题既花钱又扣分,是两头亏。
     *    (提交闸 `isFormComplete` 本来就要求行业词,所以「没有行业词」只发生在
     *     用户还没填完的**预览时刻**;而这份题单是 render 期现算的,
     *     行业词一填就会重算 —— 不会带着 0 道增长题走到提交。)
     *
     * 🔴 区域 / 全国分开,沿用本文件外既有口径(NewDiagnosis.tsx:403-406:
     *    「区域生意出『城市+行业』题、全国出行业大词」)—— 不另立一套。
     */
    const industry = ctx.industry?.trim() || '';
    const location = ctx.location?.trim() || '';
    const national = ctx.businessScope === 'national';
    const prefix = national || !location ? '' : location;
    const offensive: DraftQuestion[] = industry
        ? [
            { text: `${prefix}${industry}哪家做得好?`, modeSide: 'offensive', familyKey: 'category_choice', brandExposure: 'unnamed', source: 'system' },
            { text: `${prefix}${industry}有什么推荐的吗?`, modeSide: 'offensive', familyKey: 'recommendation_ask', brandExposure: 'unnamed', source: 'system' },
        ]
        : [];

    if (mode === 'defensive') return defensive;
    if (mode === 'offensive') return offensive;
    return [...defensive, ...offensive];
}
