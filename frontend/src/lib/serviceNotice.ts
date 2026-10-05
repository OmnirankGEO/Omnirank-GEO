/**
 * 确认链的**服务期提示**:出声 + 出口,不当错误 · WO_253 ②(P0)
 *
 * 缺陷原状:`handleGenerateLink` 把后端带回的服务期信息当异常 toast 掉,
 * 于是**链接明明生成了、用户却只看到一句红色报错**,既不知道怎么续费,
 * 也拿不到那条链接。
 *
 * 🔴 「有话要说」不等于「这次失败了」。把提示塞进 catch,代价是**把出口一起吞掉** ——
 *    用户看到的是死路,而系统其实已经把事情做完了。
 *
 * 契约字段(WO_252,C 做中):`service_notice { state, paid_at, service_end_date, renew_url }`,
 * `state ∈ active | never_paid | expired`。
 * 🔴 **本模块按契约字段名先行实现**(Review 2026-09-20 授权),C 的 sha 到了再对接;
 *    形状解析收在 `parseServiceNotice` 一处,字段名若有出入只改这一处。
 */

export type ServiceNoticeState = 'active' | 'never_paid' | 'expired';

export interface ServiceNotice {
    state: ServiceNoticeState;
    paid_at?: string | null;
    service_end_date?: string | null;
    renew_url?: string | null;
}

/** 给界面用的一条提示;`null` = 这次不用提示(`active`,或后端没给)。 */
export interface NoticeView {
    tone: 'warning' | 'info';
    text: string;
    /** 续费出口;`null` = 没有可点的出口(但**链接照样要展示**) */
    renewUrl: string | null;
}

const str = (v: unknown): string | null => (typeof v === 'string' && v.trim() !== '' ? v.trim() : null);

/**
 * 把回包里的 `service_notice` 收成本模块认的形状。
 * 🔴 认不出来一律 `null` = **不提示**,绝不猜一个状态 ——
 *    猜错会对客户说一句不成立的服务期结论。
 */
export function parseServiceNotice(raw: unknown): ServiceNotice | null {
    if (!raw || typeof raw !== 'object') return null;
    const o = raw as Record<string, unknown>;
    const state = o.state;
    if (state !== 'active' && state !== 'never_paid' && state !== 'expired') return null;
    return {
        state,
        paid_at: str(o.paid_at),
        service_end_date: str(o.service_end_date),
        renew_url: str(o.renew_url),
    };
}

/**
 * 这次要不要提示、提示什么、出口在哪。
 *
 * 🔴 **任何状态都不影响"链接要展示出来"** —— 这一条不在本函数里,
 *    而在调用方:本函数只回提示,回 `null` 也只是"不用提示",不是"别展示链接"。
 *    把两件事绑在一起,正是原缺陷(提示走了 catch,链接跟着没了)。
 */
export function noticeView(notice: ServiceNotice | null): NoticeView | null {
    if (!notice) return null;
    if (notice.state === 'active') return null;
    if (notice.state === 'never_paid') {
        return { tone: 'info', text: '尚未付款,链接已生成', renewUrl: notice.renew_url ?? null };
    }
    /* expired */
    const when = notice.service_end_date;
    return {
        tone: 'warning',
        /* 🔴 有日期就说日期 —— 「已到期」不带日期时,客户无法判断是昨天还是半年前。 */
        text: when
            ? `服务期已于 ${when} 到期,确认链已生成`
            : '服务期已到期,确认链已生成',
        renewUrl: notice.renew_url ?? null,
    };
}
