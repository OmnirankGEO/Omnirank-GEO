/**
 * 「她自己出的题」的**唯一**归一化处 —— 取价 / 提交体 / 三分支判定都读它(订正二十四)。
 *
 * 🔴 为什么必须唯一:后端拿 `custom_questions` 去比 `pricePreviewId` 的哈希。
 *    取价用一份、提交用另一份 ⇒ 要么每次 409,要么(更糟)按 A 算价、按 B 扣钱。
 *    所以本模块的返回值在页面里只允许有**一个**变量持有,三处都读它,
 *    而不是三处各调一次"应该等价"的函数。
 *
 * 🔴 归一化规则与服务端 `DiagnosisRequest.validate_custom_questions` 同解:
 *    trim → 去空 → 超长丢弃 → 去重(保序)。判据配跨层锁:本文件的
 *    `CUSTOM_QUESTION_MAX_CHARS` 必须等于那个 validator 里的上限。
 *    ⚠️ 锚打 **validator 函数名**,不打行号 —— 行号会漂,今天两边都被咬过。
 *
 * 🔴 超长是**静默丢弃**的(与服务端一致:它 raise,这里先丢以免提交必 422)。
 *    静默丢弃必须配可见提示 —— 原先那句提示挂在高级选项的 Textarea 旁,
 *    而那个 Textarea 在本笔被删 ⇒ 提示必须跟着搬进题单编辑器,否则
 *    「她写了 12 行、只跑 10 行」而屏幕上没有任何字解释那 2 行去哪了。
 */

/** ≡ `server.py` `DiagnosisRequest.validate_custom_questions` 的单条字数上限。 */
export const CUSTOM_QUESTION_MAX_CHARS = 100;

/**
 * 🔴 [#143③] 一道题最少几个字才**允许提交**。
 *
 * 主闸是「提交才计数」(见 `DraftQuestion.draft`),这条只是拦住**误提交**:
 * 焦点一移走就把刚敲进去的单个字符变成一道题。
 * 取 2 而不是更大,是因为中文短问句真的存在(「贵吗」「靠谱吗」)——
 * 阈值定高会拦住真实用法,而那种伤害没人会来报。
 * 它是**可调参数**,不是不变式;改它只影响能不能提交,不影响已提交的题。
 */
export const CUSTOM_QUESTION_MIN_CHARS = 2;

/** 这一行够不够格提交(空行与单字符不算)。 */
export function isQuestionCommittable(text: string | null | undefined): boolean {
    const t = (text || '').trim();
    return t.length >= CUSTOM_QUESTION_MIN_CHARS && t.length <= CUSTOM_QUESTION_MAX_CHARS;
}

export interface OwnQuestionsResult {
    /** 归一化后的题集 —— **提交体、取价、三分支判定都用这一份**。 */
    unique: string[];
    /** 超长被丢掉的原文(要在界面上指出来,不许静默)。 */
    tooLong: string[];
    /** 与前文重复被合并的条数。 */
    dupCount: number;
}

export function collectOwnQuestions(
    questions: readonly { text?: string | null; draft?: boolean }[] | null | undefined,
): OwnQuestionsResult {
    const unique: string[] = [];
    const tooLong: string[] = [];
    const seen = new Set<string>();
    let dupCount = 0;
    for (const q of questions || []) {
        // 🔴 [#143③] 还在打字的行**不计数、不进提交体、不触发算价**。
        //    这是本模块唯一的入口,所以「输入中不计价」在三处消费点上是结构性的,
        //    不靠三处各自记得跳过。
        if (q?.draft) continue;
        const t = (q?.text || '').trim();
        if (!t) continue;
        if (t.length > CUSTOM_QUESTION_MAX_CHARS) { tooLong.push(t); continue; }
        if (seen.has(t)) { dupCount += 1; continue; }
        seen.add(t);
        unique.push(t);
    }
    return { unique, tooLong, dupCount };
}

/** 这一条是不是超长(编辑器逐题标红用,别让她写完才知道)。 */
export function isQuestionTooLong(text: string | null | undefined): boolean {
    return ((text || '').trim().length) > CUSTOM_QUESTION_MAX_CHARS;
}
