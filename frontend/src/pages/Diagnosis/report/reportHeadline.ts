/**
 * 报告页顶部的**一句结论**(包三 §G · 报告页「3 秒不知该点哪」)。
 *
 * 🔴 页面顶部原来只有分数和等级徽标 —— 那是**读数**,不是结论。
 *    她要的是"这意味着什么、接下来做哪一步";没有这句,她就得自己把
 *    62 分 + 「成长」翻译成一个动作,而这正是"3 秒不知该点哪"的来源。
 *
 * 🔴 映射必须**总覆盖**:等级是服务端下发的字符串,可能出现我没见过的档。
 *    未知档给**笼统但为真**的一句,绝不给一个具体但可能错的判断 ——
 *    错的具体会让她停止寻找并走错方向,笼统只是让她继续看下去。
 *
 * 🔴 禁绝对化承诺(元指令:禁「保排名 / 几乎每次 / 90%+」)。这里只描述现状与下一步,
 *    不预测结果。
 */

export interface ReportHeadline {
    /** 一句结论:这份报告说明了什么。 */
    conclusion: string;
    /** 下一步该做的那**一件**事(不是列三个选项 —— 列三个等于没说)。 */
    nextStep: string;
    /** 语气档,决定顶部那条的配色。 */
    tone: 'urgent' | 'attention' | 'steady' | 'neutral';
}

/**
 * `level` 是服务端下发的中文档位(领先 / 成熟 / 成长 / 起步 / 待提升 / 空白 …)。
 * 用**包含**匹配而不是相等:服务端历史上出现过「成长期」「待提升(起步)」这类变体,
 * 相等匹配会把它们全甩进未知档。
 */
export function reportHeadline(
    level: string | null | undefined,
    score: number | null | undefined,
): ReportHeadline {
    const s = (level || '').trim();
    const hasScore = typeof score === 'number' && Number.isFinite(score);
    const scoreText = hasScore ? `${score} 分` : '这次的结果';

    if (s.includes('领先') || s.includes('成熟')) {
        return {
            conclusion: `${scoreText}:AI 已经比较稳定地提到这个品牌。`,
            nextStep: '把稳住现状的内容排进下个月,别让它掉下来。',
            tone: 'steady',
        };
    }
    if (s.includes('成长')) {
        return {
            conclusion: `${scoreText}:AI 有时会提到这个品牌,但还不稳。`,
            nextStep: '先补上报告里标红的那几类问题,它们最影响被提到的次数。',
            tone: 'attention',
        };
    }
    if (s.includes('起步') || s.includes('待提升')) {
        return {
            conclusion: `${scoreText}:AI 目前很少提到这个品牌。`,
            nextStep: '从报告里挑 3-5 个问题先做,一次做太多反而看不出效果。',
            tone: 'urgent',
        };
    }
    if (s.includes('空白')) {
        return {
            conclusion: `${scoreText}:这一轮里 AI 基本没有提到这个品牌。`,
            nextStep: '先确认品牌名和行业填得对不对,再决定要不要重测。',
            tone: 'urgent',
        };
    }
    // 未知档:笼统但为真。**不猜**它属于哪一档。
    return {
        conclusion: hasScore
            ? `${scoreText}:下面是这次在 AI 搜索里的完整结果。`
            : '下面是这次在 AI 搜索里的完整结果。',
        nextStep: '按报告里标出的问题逐条看,先处理排在最前面的。',
        tone: 'neutral',
    };
}
