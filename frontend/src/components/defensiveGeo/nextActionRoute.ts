/**
 * 服务端 `nextAction` 的**三档处置** —— 纯逻辑,刻意与 TSX 分家。
 *
 * 仓库没有前端单测框架(无 vitest/jest),所以把"这个动作点了会去哪"这条判断
 * 从组件里拆出来,`frontend/scripts/*.mjs` 就能**真的执行**它并断言行为,
 * 而不是只对 TSX 做静态字符串匹配。做法沿用现役
 * `@/components/publishing/pendingUserActionsLogic`,不另起一套。
 *
 * ## 为什么需要它([工单 V5-B · Codex fix-of-fix3 P2-NEW-5])
 *
 * 上一轮(V5-A)把「按 `error.code` 猜动作」改成了「按 `nextAction.kind` 分发」,
 * 但只给 `new_preview` 画了可点按钮,**其余一律降级成纯文字**。
 * Codex 指出这里还差一半:服务端下发 `top_up` + `{page: "wallet"}` 时,
 * 站内**真的有** `/wallet` 这一页 —— 只显示"去充值算力"五个字而不给按钮,
 * 等于把她推回去自己找路。
 *
 * 但也不能反过来对所有 kind 都造一颗按钮:`request_approval` 的落点
 * (defgeo 侧的审批页)现役**不存在**,给一颗点了 404 的按钮比不给更糟。
 *
 * 所以分三档,每一档都由**站内真实路由**决定,而不是由"服务端说了什么"决定:
 *
 *   ① `in_panel`  —— 这一屏自己就能做完(重新发起体检)。
 *   ② `navigate`  —— 站内有真实落点,给按钮并真的跳过去。
 *   ③ `text`      —— 没有落点。把服务端那句话原样显示,**不给按钮**。
 *
 * 🔴 第③档不是"放弃",而是**如实**:她仍然知道下一步是什么(那句话在),
 *    只是我们不假装这一屏能替她做。判据侧有一条 census 钉住第③档是个
 *    **冻结集**,新增一个 kind 落进去必须由人确认它真的没有落点。
 */

import { CONTACT_ACTION } from '@/components/publishing/pendingUserActionsLogic';

/** 这一屏自己能做完的动作。 */
export const IN_PANEL_KINDS: readonly string[] = ['new_preview'];

/**
 * 有**站内真实落点**的动作 → 路由。
 *
 * 🔴 只放确认存在的路由。逐条对过 `frontend/src/App.tsx`:
 *   · `top_up`         → `/wallet`   —— `<Route path="wallet" element={<WalletPage />} />` 真实存在。
 *   · `contact_support` → 复用现役 `CONTACT_ACTION.target`(`/feedback`)。
 *      **不在这里再写一遍字面量**:全站"联系客服去哪"只有一处定义,
 *      那一处哪天改了,这里跟着变;写第二份迟早两边漂。
 *
 * 🔴 **刻意不放** `request_budget_approval`,尽管服务端给它的 target 也是
 *    `{page: "wallet"}`(`api/defensive_geo_api.py` 的资金矩阵分支)。
 *    组织预算不足时把成员导到**个人钱包**,等于让成员自己掏钱替组织付 ——
 *    那正是资金矩阵注释里写着「绝不给个人钱包充值」要防的事。
 *    在站内出现真正的"申请追加团队预算"入口之前,它留在第③档。
 */
export const KIND_ROUTES: Readonly<Record<string, string>> = {
    top_up: '/wallet',
    contact_support: CONTACT_ACTION.target as string,
    // `NOT_FOUND` 的默认动作(题单/预览已经不在了)。`/history` = 诊断记录列表,
    // `<Route path="history" element={<HistoryList />} />` 真实存在。
    back_to_list: '/history',
};

export interface NextActionLike {
    kind?: string;
    label?: string;
    target?: unknown;
}

export type NextActionPlan =
    | { tier: 'in_panel'; kind: string }
    | { tier: 'navigate'; kind: string; href: string }
    | { tier: 'text'; kind: string };

/**
 * 一个 nextAction 该怎么呈现。`null` = 服务端没给动作(那一屏本来就没有下一步)。
 *
 * 顺序有意:先问"这一屏能不能自己做完",再问"站内有没有落点",最后才降级成文字。
 * 反过来的话,`new_preview` 只要哪天被塞进 `KIND_ROUTES` 就会变成跳走 ——
 * 而她要的是留在这一屏重新算一次。
 */
export function planNextAction(next: NextActionLike | null | undefined): NextActionPlan | null {
    const kind = next?.kind;
    if (!kind) return null;
    if (IN_PANEL_KINDS.includes(kind)) return { tier: 'in_panel', kind };
    const href = KIND_ROUTES[kind];
    if (href) return { tier: 'navigate', kind, href };
    return { tier: 'text', kind };
}
