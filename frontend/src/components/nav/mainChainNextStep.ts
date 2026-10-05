/**
 * GEO 主链「这一页做完了,接下来做什么」的**唯一**映射(包三 §G)。
 *
 * 🔴 与 `defensiveGeo/nextActionRoute.ts` 是两回事,别混:
 *    那个是**服务端按错误码下发**的补救动作(充值 / 重新预览 / 联系客服),
 *    这个是**页面之间的固定流程**(创作 → 发布 → 监测)。
 *    混成一个会让"她这次失败了该干嘛"和"她做完了该干嘛"共用一套文案。
 *
 * 🔴 沿用 `nextActionRoute.ts` 立下的那条纪律:**只放确认存在的站内路由**。
 *    逐条对过 `frontend/src/App.tsx`:
 *      · `/articles`   → `<Route path="articles" …>`  真实存在
 *      · `/publish`    → `<Route path="publish" …>`   真实存在
 *      · `/monitoring` → `<Route path="monitoring" …>` 真实存在
 *      · `/pricing`    → `<Route path="pricing" …>`   真实存在
 *    给一颗点了 404 的按钮,比不给按钮更糟。
 *
 * 🔴 **未知页返回 null**(不渲染那一条),不给"猜一个"的下一步 ——
 *    错的具体会把她带去一个跟她无关的页面,而她会以为那是流程要求的。
 */

export interface MainChainStep {
    /** 这一页刚做完的是什么(用她的话,不用模块名)。 */
    doneLabel: string;
    /** 下一步那**一件**事。 */
    nextLabel: string;
    /** 站内真实路由。 */
    to: string;
    /** 为什么是这一步 —— 一句,给"我为什么要去那儿"一个答案。 */
    why: string;
}

export type MainChainPage = 'writing' | 'publish' | 'monitoring';

const STEPS: Record<MainChainPage, MainChainStep> = {
    writing: {
        doneLabel: '文章写完了',
        nextLabel: '去发布',
        to: '/publish',
        why: '写好的文章要发出去,AI 才可能读到它。',
    },
    publish: {
        doneLabel: '发布提交了',
        nextLabel: '去看监测',
        to: '/monitoring',
        why: '发出去之后看 AI 有没有开始提到这个品牌,通常要几天。',
    },
    monitoring: {
        doneLabel: '看完这一轮监测',
        nextLabel: '去报价',
        to: '/pricing',
        why: '哪些词还没上榜,就把它们做成下一单的报价。',
    },
};

export function mainChainNextStep(page: string | null | undefined): MainChainStep | null {
    const p = (page || '').trim();
    return (p in STEPS) ? STEPS[p as MainChainPage] : null;
}

/** 判据用:全部页 key(机械枚举,避免手写清单漏项)。 */
export const MAIN_CHAIN_PAGES = Object.keys(STEPS) as MainChainPage[];
