/**
 * 「这道题太长了」的判定与文案格式化(#153-A · 2026-09-08)。
 *
 * 🔴 **为什么要有这个模块** —— 超长题在防守线的真实后果是 C 取证出来的:
 *    它**整单进不去**,而且**不在提交时拦**,是后台派发时静默重试到退款。
 *    也就是说用户点了确认、算力被冻、然后什么都没发生、最后退款,
 *    全程**屏幕上没有一个字**说明发生了什么。
 *
 * 🔴 而前端本来就有那句「超 100 字 · 这道不会跑」的提示 —— 它写着
 *    `{isManual && …}`,**把自己挡在系统/AI 题之外**:
 *    她自己写超长会标红;系统按超长品牌名拼出来的题超长则一个字都不说。
 *    (`ownQuestions.ts` 自己的注释就写着「静默丢弃**必须**配可见提示」——
 *     这条规矩在系统题那一半上没有被执行。)
 *
 * 🔴 **不截断、不加品牌名长度闸**(Review 裁定):
 *    品牌名本来就该**原样**出现在防守题里(「XX 靠谱吗?」),截断它会去问一个
 *    **不存在的公司**。与 R6 的行业登记名性质相反 —— 那里整串进题面是无意义的。
 *
 * 🔴 文案**取自跨层注册表**(`DEFGEO_COPY.questionTooLong`,后端 registry v11 生成),
 *    本模块只做 format,**不自带任何句子** —— 前后端两份文案会漂,
 *    而漂开那天两边各自看起来都对。
 *
 * 本模块**零 import**:判据用 data: URL 加载它,带相对 import 会解析不了;
 * 字数上限由**调用方传入**(来自 `ownQuestions.CUSTOM_QUESTION_MAX_CHARS` 单源),
 * 不在这里再抄一个数。
 */

/** 一道超长题的定位信息。`ordinal` 是**给人看的序号**(从 1 起)。 */
export interface TooLongHit {
    ordinal: number;
    chars: number;
    excerpt: string;
    text: string;
}

/**
 * 把 `{key}` 占位符换成实际值。
 *
 * 🔴 **少给一个占位符 = 用户看到字面的 `{excerpt}`。** 所以这里不做"尽力而为":
 *    没被替换掉的占位符**原样留着**,由判据 `hasUnresolvedPlaceholder` 抓 ——
 *    悄悄吞掉未知占位符会让"文案模板加了新字段"这件事无声无息。
 */
export function formatCopyTemplate(
    template: string,
    vars: Readonly<Record<string, string | number>>,
): string {
    let out = String(template || '');
    for (const [k, v] of Object.entries(vars || {})) {
        out = out.split('{' + k + '}').join(String(v));
    }
    return out;
}

/** 还有没有没被填上的占位符。判据用它钉「模板新增字段时必须有人补」。 */
export function hasUnresolvedPlaceholder(s: string): boolean {
    return /\{[A-Za-z_][A-Za-z0-9_]*\}/.test(String(s || ''));
}

/**
 * 找出**第一道**超长题。返回 null = 没有超长题。
 *
 * 只报第一道:一次让她改一处,报文里那个序号才有意义;
 * 列全部反而让「第 N 题」这个定位失去作用。
 */
export function findTooLongQuestion(
    questions: readonly { text?: string | null }[] | null | undefined,
    limit: number,
): TooLongHit | null {
    const lim = Math.max(1, Number(limit) || 0);
    const list = questions || [];
    for (let i = 0; i < list.length; i += 1) {
        const t = String(list[i]?.text || '');
        if (t.length > lim) {
            return { ordinal: i + 1, chars: t.length, excerpt: t.slice(0, 20), text: t };
        }
    }
    return null;
}

/**
 * 组装那句话。`template` 必须是注册表里的 `questionTooLong`,
 * 本模块**不提供**任何兜底句子 —— 兜底句就是第二份文案,它一定会漂。
 */
export function questionTooLongMessage(
    template: string, hit: TooLongHit, limit: number,
): string {
    return formatCopyTemplate(template, {
        ordinal: hit.ordinal, chars: hit.chars, limit: Math.max(1, Number(limit) || 0),
        excerpt: hit.excerpt,
    });
}
