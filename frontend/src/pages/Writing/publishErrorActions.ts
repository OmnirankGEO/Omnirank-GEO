/**
 * 错误合同里的 `detail.actions[]` → **界面上真的能点的东西**(#192 a3)。
 *
 * 现场(0913a):发布链返回 403 `ABILITY_NOT_GRANTED`,合同里带了
 * `actions: [{id:'handoff',label:'交给团队负责人',type:'nav'}, {id:'contact_admin',…,type:'contact'}]`,
 * 而面板只渲染了那句 message —— 用户看到「你还没有这个操作权限」,
 * 却拿不到后端**已经给出**的两条出路。禁猜清单第 4 条:断头必须有出口。
 *
 * 🔴 合同里的 action **只有 `{id,label,type}`,没有 href/endpoint**。
 *    所以"去哪儿"这件事必须由前端按 **id** 决定 —— 而这正是危险的地方:
 *    照着 label 渲染一颗按钮、点了却什么都不发生,就是又造一颗死按钮
 *    (本程序一路在修的就是这个)。
 *    ⇒ 规则:**只有认识的 id 才渲染成按钮**;不认识的**降级成纯文字**,
 *    它仍然把后端的建议告诉用户,但不假装可点。
 *
 * 🔴 路由必须是**真实存在**的(App.tsx 里有 `/team`、`/help/home`、`/publish`)。
 *    编一个不存在的路径 = 点了掉进 404,比不给按钮更糟。
 *
 * 本模块**零 import**:判据用 data: URL 直接加载它来真调。
 */

export interface ContractAction {
    id: string;
    label: string;
    type: string;
}

export type ResolvedAction =
    /** 渲染成按钮,点了走这个路由 */
    | { kind: 'nav'; label: string; to: string; id: string }
    /** 渲染成按钮,点了重试刚才那次操作 */
    | { kind: 'retry'; label: string; id: string }
    /** 只渲染文字 —— 认不出该去哪儿,不假装可点 */
    | { kind: 'text'; label: string; id: string };

/**
 * 已知 id → 去哪儿。
 * 🔴 每一条都要指向 App.tsx 里**真的有**的路由。加新条目时先确认路由存在。
 */
const NAV_TARGET: Record<string, string> = {
    handoff: '/organization/team', // 交给团队负责人 → 团队管理(在那里请负责人操作/授权)[WO_260:原 /team 不存在]
    contact_admin: '/help/home', // 联系管理员 → 帮助中心(里面有联系方式)
    change_account: '/publish',  // 换账号 → 发布中心
    back: '/publish',
    back_to_select: '/publish',
    open_post: '/writing',
};

/**
 * 把合同里的 actions 解析成界面能渲染的东西。
 *
 * 🔴 `dismiss` 一律丢掉:关闭弹窗这件事界面自己有(右上角/点外部),
 *    再多一颗「知道了」是 Owner 说的非必要按钮。
 */
export function resolveActions(actions: unknown): ResolvedAction[] {
    const list = Array.isArray(actions) ? actions : [];
    const out: ResolvedAction[] = [];
    for (const raw of list) {
        if (!raw || typeof raw !== 'object') continue;
        const a = raw as Partial<ContractAction>;
        const id = typeof a.id === 'string' ? a.id : '';
        const label = typeof a.label === 'string' ? a.label.trim() : '';
        const type = typeof a.type === 'string' ? a.type : '';
        if (!label) continue;                 // 没有文字的按钮没法渲染
        if (type === 'dismiss') continue;
        if (type === 'retry') { out.push({ kind: 'retry', label, id }); continue; }
        const to = NAV_TARGET[id];
        if (to) { out.push({ kind: 'nav', label, to, id }); continue; }
        /* 认不出去处 ⇒ 只给文字。**不给一颗点了没反应的按钮。** */
        out.push({ kind: 'text', label, id });
    }
    return out;
}

/** 界面上要不要显示这一组 —— 一条可渲染的都没有就别占地方。 */
export function hasActions(actions: unknown): boolean {
    return resolveActions(actions).length > 0;
}
