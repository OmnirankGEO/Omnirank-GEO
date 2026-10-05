/**
 * 「同行对比」三档的**文案与可用性**(#189)。
 *
 * Owner 2026-09-13 看着截图问:「这三个标签不是找同行的吗?现在的表达是不是需要换一下?」
 * 原来三枚 chip 是「已核验 / 待核验 / 仅写标准」—— 写的是**证据状态**,没有主语:
 * 用户看不出这是在决定「文章里要不要点名同行」。
 *
 * 🔴 三件事一起改,缺一件都不成立:
 *   ① **加主语**:控件前置「同行对比」,三档改成"我在做的选择 + 后果";
 *   ② **去告警色**:「不点名 · 只写怎么选」是一个**合法且常用**的选择,
 *      原来配红点红字,读起来像出错 —— 用红色劝退一个正当选项,是在误导;
 *   ③ **动作与状态分开**:原来「已核验」既是状态又是动作(点它会联网检索),
 *      一个按钮两种含义。检索拆成独立按钮。
 *
 * 🔴 本模块**零 import**:判据用 data: URL 直接加载它来真调 ——
 *    文案锁只查源码字符串的话,证明的是"写了这么一句",不是"屏幕上是这句"。
 */

export type PeerMode = 'real' | 'semi' | 'evidence_only';

/** 控件前置标签。没有它,三档就是三个没有主语的形容词。 */
export const PEER_GROUP_LABEL = '同行对比';

export interface PeerModeOption {
    value: PeerMode;
    /** 定稿措辞(Review 2026-09-13 定,Owner 要结果不要选项)。N 为实时数。 */
    label: string;
    disabled: boolean;
    /**
     * 🔴 不可用**必须同屏给原因**(禁猜清单第 4 条)。
     *    只置灰不说话 = 用户反复点一个永远不动的按钮。
     */
    disabledReason: string;
}

export interface PeerCounts {
    /** 已核实(名称有可追溯来源)的同行家数,不含已排除。 */
    verified: number;
    /** 还在核实的家数,不含已排除。 */
    pending: number;
}

/**
 * 三档的文字与可用性。
 *
 * 🔴 「点名对比」在**一家都没核实**时不可选:选了它正文也点不了名,
 *    后端那道证据硬门会把它打回来(前端不能自行升级)。
 *    与其让用户点一次再被弹回,不如当场说清楚该先做什么。
 */
export function peerModeOptions(counts: PeerCounts | null | undefined): PeerModeOption[] {
    const verified = Math.max(0, Number(counts?.verified) || 0);
    const pending = Math.max(0, Number(counts?.pending) || 0);
    return [
        {
            value: 'real',
            label: `点名对比 · 只写已核实的 ${verified} 家`,
            disabled: verified === 0,
            disabledReason: verified === 0 ? '还没有核实过的同行,先联网核实' : '',
        },
        {
            value: 'semi',
            label: `暂不点名 · ${pending} 家还在核实`,
            disabled: false,
            disabledReason: '',
        },
        {
            value: 'evidence_only',
            label: '不点名 · 只写怎么选',
            disabled: false,
            disabledReason: '',
        },
    ];
}

/** 独立按钮的文字。动作与状态分开之后,它是**唯一**触发联网检索的地方。 */
export const PEER_RESEARCH_LABEL = '联网找同行并核实';

/** 列表标题。原来是「已核验竞品 / 待核验候选」—— 「候选」也是工程词。 */
export function peerListTitle(mode: PeerMode | string, activeCount: number): string {
    const n = Math.max(0, Number(activeCount) || 0);
    return mode === 'real' ? `已核实的同行 ${n} 家` : `还在核实的同行 ${n} 家`;
}

/**
 * `evidence_only` 那一行提示。
 * 🔴 **中性色**,不是红的:它描述的是一个正当选择的后果,不是错误。
 *    原文「正文不使用未核验竞品名称 · 当前仅写选型标准」既是工程口吻又配红字。
 */
export const EVIDENCE_ONLY_NOTE = '文章不点名同行,只写怎么选;想点名就先联网核实';

/** 手动添加那一格的占位。原来是「输入竞争对手名称...」。 */
export const ADD_PEER_PLACEHOLDER = '加一个同行,系统会联网核实';

/**
 * 从竞品列表算出两个数。
 *
 * 🔴 「已核实」= 名称有可追溯来源(`name_verified`)**或**人工确认过
 *    (`human_verified_name`)。两个位任一为真都算 —— 人工确认过的名字
 *    不该因为机器没查到就被算成"还在核实"。
 * 🔴 已排除的不算进任何一边:用户把它划掉了,再报进数里只会让两个数对不上眼前的列表。
 */
export function peerCounts(rows: unknown): PeerCounts {
    const list = Array.isArray(rows) ? rows : [];
    let verified = 0;
    let pending = 0;
    for (const raw of list) {
        if (!raw || typeof raw !== 'object') continue;
        const o = raw as { excluded?: unknown; name_verified?: unknown; human_verified_name?: unknown };
        if (o.excluded === true) continue;
        if (o.name_verified === true || o.human_verified_name === true) verified += 1;
        else pending += 1;
    }
    return { verified, pending };
}
