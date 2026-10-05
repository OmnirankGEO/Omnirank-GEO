/**
 * [WO_267] 行业大类(`industry_category`)在前端的唯一读法。
 *
 * 字典本身只在后端(`config/industry_taxonomy.json`),前端经 `GET /api/industry-taxonomy` 读,
 * **不另写一份** —— 两份各自演化,正是这张字典要治的病(五处各一套行业名)。
 *
 * 🔴 与 `lib/industries.ts` 不是一回事:那份是**自由文本 `industry`** 的归类器,
 *    报价流程靠它推断行业、挑兜底扩词种子(WO_267 边界「不动计价」,本单不碰它)。
 *    这份只管大类 key 的**显示**与**选择**。
 *
 * 🔴 存量与新写入两种形状并存(Owner 09-19:存量不回填):
 *    `brands.industry_category` 旧行是中文名(「房产家居」…),新写入是英文 key(`new_energy`…)。
 *    后端只给部分响应附了 `industry_category_name`(诊断历史等没有),
 *    所以**所有**显示点都走 `industryCategoryText`,英文 key 一个都不许漏到页面上。
 *
 * 纯函数在 `industryTaxonomyText.ts`(零 import,判据直接真调),这里原样转出。
 */
import { useEffect, useMemo, useState } from 'react';
import { authFetch } from '@/lib/api';
import { nameOfFrom, parseTaxonomy, type IndustryTaxonomy } from '@/lib/industryTaxonomyText';

export {
    categoryKeyFromRaw,
    industryCategoryText,
    looksLikeCategoryKey,
    nameOfFrom,
    parseTaxonomy,
    type IndustryTaxonomy,
    type IndustryTaxonomyCategory,
} from '@/lib/industryTaxonomyText';

let pending: Promise<IndustryTaxonomy | null> | null = null;

/** 读一次、全页共用;失败不缓存,下一次调用会重试。 */
export function loadIndustryTaxonomy(): Promise<IndustryTaxonomy | null> {
    if (!pending) {
        pending = authFetch('/api/industry-taxonomy')
            .then((res) => (res.ok ? res.json() : null))
            .then(parseTaxonomy)
            .catch(() => null)
            .then((t) => {
                if (!t) pending = null;
                return t;
            });
    }
    return pending;
}

/** 组件里用:字典到之前 `nameOf` 一律回 '',于是新 key 暂不显示,而不是闪一下英文。 */
export function useIndustryTaxonomy(): {
    taxonomy: IndustryTaxonomy | null;
    nameOf: (key: string) => string;
} {
    const [taxonomy, setTaxonomy] = useState<IndustryTaxonomy | null>(null);
    useEffect(() => {
        let alive = true;
        void loadIndustryTaxonomy().then((t) => { if (alive) setTaxonomy(t); });
        return () => { alive = false; };
    }, []);
    /* 按字典记住:否则每次渲染都是新函数,谁把它放进 effect 依赖就会每次渲染都重跑 */
    const nameOf = useMemo(() => nameOfFrom(taxonomy), [taxonomy]);
    return { taxonomy, nameOf };
}
