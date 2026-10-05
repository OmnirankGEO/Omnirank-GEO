/**
 * 「这次到底跑哪些题」——订正十二 Owner 拍板的三分支,写成一处。
 *
 * 原话:客户不主动出题 ⇒ 跑 AI 出的;主动出题 ⇒ **只跑客户自己出的**;
 * 开关开 ⇒ 双轨(AI + 客户)。**计价 = 实际要跑的题数**,不是题单显示的总数。
 *
 * 🔴 为什么单独成模块:这条规则同时决定**三件事** ——
 *    ①题单区怎么显示(AI 那批要不要折叠置灰)②按钮上的价 ③最终真跑什么。
 *    三处各写一份的话,必有一处漂;而漂开那天,屏幕上三处**各自看起来都对**。
 *    (这正是订正十二自己写的「UI 必须把这条规则做成看得见的,否则又是『看到的 ≠ 跑的』」。)
 *
 * 🔴 本模块**不算钱**。它只回答"跑几道题";价一律由服务端
 *    `/api/pricing/diagnosis-preview` 按同一个题数算。前端算价的镜子要删(阶段⑤)。
 */

export type QuestionRunMode =
    /** 她没出题 ⇒ 跑 AI 出的那批 */
    | 'ai_only'
    /** 她出了题 ⇒ **只**跑她的,AI 那批不跑 */
    | 'custom_only'
    /** 她出了题 + 开了「也跑 AI 出的题」⇒ 两边都跑 */
    | 'dual';

/** 三分支判定。`alsoRunAi` = 订正十二里改名后的那个开关(原 `aiOptimizeCustom`),默认关。 */
export function questionRunMode(customCount: number, alsoRunAi: boolean): QuestionRunMode {
    if (customCount <= 0) return 'ai_only';
    return alsoRunAi ? 'dual' : 'custom_only';
}

/**
 * **实际要跑的题数** —— 计价基数就是它(订正十二:「计价 = 实际要跑的题数」)。
 * 例:她出了 3 道 ⇒ 跑 3 道;出了 9 道 ⇒ 跑 9 道;开了双轨 ⇒ 按两边合计。
 */
export function effectiveQuestionCount(
    aiCount: number, customCount: number, alsoRunAi: boolean,
): number {
    const ai = Math.max(0, aiCount | 0);
    const custom = Math.max(0, customCount | 0);
    switch (questionRunMode(custom, alsoRunAi)) {
        case 'ai_only': return ai;
        case 'custom_only': return custom;
        case 'dual': return ai + custom;
    }
}

/** AI 那批是否该折叠置灰(她出了自己的题、且没开双轨)。 */
export function aiQuestionsMuted(customCount: number, alsoRunAi: boolean): boolean {
    return questionRunMode(customCount, alsoRunAi) === 'custom_only';
}

/**
 * 折叠态那一行人话。订正十二逐字要求写清「这次只跑你出的 N 道;AI 出的题不再跑」——
 * 数字取**她的题数**,不取手边恰好有的别的数(它描述的就是那个东西)。
 */
export function mutedNotice(customCount: number): string {
    return `你出了自己的题,这次只跑你出的 ${Math.max(0, customCount | 0)} 道;AI 出的题不再跑。`;
}
