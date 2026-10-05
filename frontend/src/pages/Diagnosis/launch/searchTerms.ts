/**
 * 「搜索词」的唯一归一化处(订正二十九 · 乙案)。
 *
 * 🔴 它**不是题**。题是要拿去问 AI 的整句话(上限 100 字,走 `custom_questions`);
 *    搜索词是喂 `batch_collect_all` → 抖音/小红书搜索框的短词(上限 50 字,走 `keywords`)。
 *    两个概念、两个输入位、两套上限 —— 订正二十五 消灭的正是"用同一个框装两种东西":
 *    她在一个叫「问题」的编辑器里打字,却收到一条说「关键词」的 422。
 *    所以这个输入位在**高级**里、默认折叠、默认空、**不预填**,和题单编辑器不共用任何状态。
 *
 * 🔴 为什么又把它加回来(订正二十五 一度整个删掉):
 *    Deploy 的正确分母显示首诊 keywords 非空 207/207、≥2 次诊断的品牌上次非空 61/61 ——
 *    删框会让「她填的业务词」这条输入整体消失,而 Owner 铁律是「影响诊断质量的不能丢」。
 *    (⑤b 原来那版删得掉是因为我以为服务端能从品牌档案派生 ——
 *     后来查清:预填从来读的是**上一次已发布诊断的入参**,不是档案,
 *     `brands.seed_keywords` 是只写不读的死列。见订正二十八。)
 *
 * 🔴 上限与服务端 `DiagnosisRequest.validate_keywords` **同解**,判据配跨层锁:
 *    条数 > 20 拒 · 单条 > 50 字拒 · 空串拒(这里先 trim 去空,免得提交必 422)。
 *    ⚠️ 锚打 **validator 函数名**,不打行号。
 *
 * 🔴 超限**不静默丢弃**:留在状态里、界面上标出来、并把提交拦住。
 *    静默截断会变成"她填了 25 条,只跑了 20 条",而屏幕上没有任何字解释那 5 条去哪了。
 */

/** ≡ `server.py` `DiagnosisRequest.validate_keywords`:单条字数上限。 */
export const SEARCH_TERM_MAX_CHARS = 50;
/** ≡ 同一个 validator:条数上限。 */
export const SEARCH_TERMS_MAX_COUNT = 20;

export interface SearchTermsResult {
    /** 归一化后的词 —— **提交体用的就是这一份**(非空时才带上)。 */
    terms: string[];
    /** 超过单条字数上限的原文(要在界面上指出来,不许静默)。 */
    tooLong: string[];
    /** 条数是否超过上限。 */
    overflow: boolean;
    /** 合法即可提交:没有超长、也没有超条数。**空是合法的**(服务端派生)。 */
    valid: boolean;
}

export function collectSearchTerms(raw: string | null | undefined): SearchTermsResult {
    const terms: string[] = [];
    const tooLong: string[] = [];
    for (const line of (raw || '').split(String.fromCharCode(10))) {
        const t = line.trim();
        if (!t) continue;                                    // 空串服务端会拒,这里先去掉
        if (t.length > SEARCH_TERM_MAX_CHARS) { tooLong.push(t); continue; }
        terms.push(t);
    }
    const overflow = terms.length > SEARCH_TERMS_MAX_COUNT;
    return { terms, tooLong, overflow, valid: tooLong.length === 0 && !overflow };
}

/** 这一行是不是超长(逐行标红用,别让她写完才知道)。 */
export function isSearchTermTooLong(line: string | null | undefined): boolean {
    return ((line || '').trim().length) > SEARCH_TERM_MAX_CHARS;
}
