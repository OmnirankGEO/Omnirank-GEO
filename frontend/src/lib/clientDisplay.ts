/**
 * 监测客户行的**显示口径** · WO_251 §4(2026-09-20)
 *
 * 契约原件:`db/monitoring_db.py::get_paid_clients` 的 docstring
 * (sha `31f905276`;以那份为准,不以任何消息为准)。
 *
 * ## 两组字段是两件事
 *
 * · `brand_current_name` / `brand_current_industry` —— **品牌现值**,
 *   后端已经做完「现值 → 报价快照」的回落,都没有时返 `null`。
 * · `brand_name` / `industry` —— **下单那一刻的快照**,对账用,本单一个字没动。
 *   客户改了名、或当初分类选错,快照都不会跟着变。
 *
 * 🔴 **前端不再写一遍「现值→快照」的回落**:后端已经做了。
 *    同一条规则写两处,迟早有一处跟不上,而两边各自看都"对",
 *    只有客户看到的那个名字是旧的。前端只做**显示默认值**。
 *
 * ## 🔴 为什么还要区分「字段缺失」与「字段是 null」
 *
 * 契约说这两列「对**所有**消费方一律附带」—— 那句话对真实端点成立,
 * 但**演示态不走那个查询**:`services/demo_access.py::_demo_clients`
 * 自己拼行,只有 `brand_name` / `industry`,**根本没有这两列**。
 * 🔴 自拼客户条的地方 **一共三处**(C 2026-09-20 点名):
 *    ① `db/monitoring_db.get_paid_clients`(实时,带两列)
 *    ② `services/demo_access.py::_demo_clients`(兜底)
 *    ③ `services/admin_cross_tenant_governance.py::_monitoring_snapshot`
 *       —— **落盘快照**,存量里这两个键补不回来。
 *    所以「键不存在」在演示态是**结构性不可消除**的,不是待修的临时状态。
 * 于是:
 *   · **键不存在** = 这条链不提供现值 ⇒ **回落到快照**(保持原行为);
 *   · 键在、值为 `null` = 后端明说"两级都没有" ⇒ 用**显示默认值**。
 * 🔴 判的是**键在不在**(`hasOwnProperty`),不是值等不等于 `undefined` ——
 *    见下面 `hasKey` 的注释。
 * 把两者压成一种,演示态会从「显示旧名」变成「显示 品牌 #id」——
 * 那是把一处没人要求改的行为顺手改掉。
 */

export interface ClientRowForDisplay {
    brand_id?: number | string | null;
    /** 品牌现值(后端已回落过快照);`null` = 两级都空;**缺失** = 这条链不提供 */
    brand_current_name?: string | null;
    brand_current_industry?: string | null;
    /** 下单那一刻的快照,对账用 */
    brand_name?: string | null;
    industry?: string | null;
}

/** 「空」= null / undefined / 空串 / 仅空白 —— 与后端 `NULLIF(TRIM(...),'')` 同口径。 */
const blank = (v: unknown): boolean => typeof v !== 'string' || v.trim() === '';

/*
 * 🔴 **按「键在不在」分支,不按值等不等于 `undefined`。**
 *    契约(C 2026-09-20 `9c6878450` 订正为三态)把「键不存在」定义成一个**独立状态**。
 *    用 `?? / || / === undefined` 判,会把「键存在但值是 undefined」和「键不存在」
 *    混成一件事 —— 而前者是**某个映射层顺手塞进来的**,后者是**那条链根本不提供**。
 *    两者要的行为相反:前者该按后端的"没有值"走默认值,后者该回落快照。
 */
const hasKey = (row: object, key: string): boolean => Object.prototype.hasOwnProperty.call(row, key);

/**
 * 客户显示名。
 * 优先现值 → (字段缺失时)快照 → `品牌 #<brand_id>` → `未命名客户`。
 */
export function clientDisplayName(row: ClientRowForDisplay | null | undefined): string {
    if (!row) return '未命名客户';
    if (!blank(row.brand_current_name)) return (row.brand_current_name as string).trim();
    /* 🔴 只有**字段缺失**时才回落快照(演示态那条链);后端明说 `null` 时不回落 —— 那是"确实没有"。 */
    if (!hasKey(row, 'brand_current_name') && !blank(row.brand_name)) return (row.brand_name as string).trim();
    const id = row.brand_id;
    if (id !== null && id !== undefined && String(id).trim() !== '') return `品牌 #${String(id).trim()}`;
    return '未命名客户';
}

/** 客户显示行业。优先现值 → (字段缺失时)快照 → `未填写`。 */
export function clientDisplayIndustry(row: ClientRowForDisplay | null | undefined): string {
    if (!row) return '未填写';
    if (!blank(row.brand_current_industry)) return (row.brand_current_industry as string).trim();
    if (!hasKey(row, 'brand_current_industry') && !blank(row.industry)) return (row.industry as string).trim();
    return '未填写';
}
