/**
 * 保存反馈:**按落库事实说话**,不按"请求没抛错"说话 · WO_253 ①(P0)
 *
 * 缺陷原状:`BrandDetailPage` 保存时 `await authApi.put(...)` 把响应**丢掉**,
 * 然后无条件 `toast.success('已保存')` —— 提交了联系方式而后端没落库时,
 * 用户看到的是**绿色的「已保存」**。Owner 三图里那一张就是它。
 *
 * 🔴 「请求成功」与「字段落库」是两件事。200 只说明服务端受理了这次调用,
 *    没说它把哪几个字段写进去了。拿前者冒充后者,是本仓反复出现的
 *    「信号由做事方发出」——真判据要取自**被服务方**(这里是 `persisted[]`)。
 *
 * ## 三态,不是两态(与 WO_251 §4 同一课)
 *
 * · `persisted` **键不存在** = 这个后端还不报告落库明细(WO_252 未上线)
 *   ⇒ **保持原行为**(绿),否则本笔一上线,每一次保存都会变成红字;
 * · `persisted` 在、但缺了某个已提交的联系字段 ⇒ **红**,点名说没保存哪几个;
 * · 都在 ⇒ 绿,并由调用方**重新拉详情**回显落库值(不拿本地表单值假装)。
 */

/** 联系方式四字段 —— 本单只对它们判(工单边界)。 */
export const CONTACT_FIELDS = [
    'contact_phone', 'contact_wechat', 'contact_website', 'contact_address',
] as const;
export type ContactField = (typeof CONTACT_FIELDS)[number];

const CONTACT_LABEL: Record<ContactField, string> = {
    contact_phone: '电话',
    contact_wechat: '微信',
    contact_website: '官网',
    contact_address: '地址',
};

export interface SaveFeedback {
    /** `ok` 绿 · `missing` 红 · `unreported` = 后端没报告明细,按原行为绿 */
    kind: 'ok' | 'missing' | 'unreported';
    message: string;
    /** 提交了却没落库的字段(仅 `missing` 时非空) */
    missing: ContactField[];
    /** 是否应当重新拉详情回显落库值 */
    shouldReload: boolean;
}

const filled = (v: unknown): boolean => typeof v === 'string' && v.trim() !== '';
const hasKey = (o: object, k: string): boolean => Object.prototype.hasOwnProperty.call(o, k);

/**
 * @param payload  这次 PUT 提交的内容(用来判"提交了哪些联系字段")
 * @param response PUT 的响应体
 *
 * 🔴 判 `persisted` 用**键在不在**,不用 `?? / ||`:
 *    `[]` 是「后端说一个都没落」—— 必须红;
 *    **键不存在**是「这个后端不报告」—— 不能红。
 *    用 `||` 判会把前者当成后者(空数组为真值,但 `response.persisted || []` 让两者同形),
 *    于是**最该红的那一种反而永远绿**。
 */
export function saveFeedback(
    payload: Record<string, unknown> | null | undefined,
    response: Record<string, unknown> | null | undefined,
): SaveFeedback {
    const submitted = CONTACT_FIELDS.filter((f) => filled(payload?.[f]));

    if (!response || !hasKey(response, 'persisted')) {
        return { kind: 'unreported', message: '已保存', missing: [], shouldReload: false };
    }
    const raw = response.persisted;
    const persisted = new Set(Array.isArray(raw) ? raw.filter((x): x is string => typeof x === 'string') : []);

    const missing = submitted.filter((f) => !persisted.has(f));
    if (missing.length > 0) {
        return {
            kind: 'missing',
            /* 🔴 点名说没保存哪几个 —— 只说"保存失败"会让人以为整单都没存。 */
            message: `联系方式未保存:${missing.map((f) => CONTACT_LABEL[f]).join('、')} · 请重试或联系管理员`,
            missing,
            shouldReload: true,
        };
    }
    /* 落库了 ⇒ 重新拉详情回显**落库值**,不拿本地表单值假装。 */
    return { kind: 'ok', message: '已保存', missing: [], shouldReload: true };
}
