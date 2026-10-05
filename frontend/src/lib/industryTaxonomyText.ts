/**
 * [WO_267] 行业大类的**纯函数**部分 —— 零 import,判据可以直接转译后真调
 * (`scripts/test-industry-taxonomy.mjs`)。取数与 React 钩子在 `industryTaxonomy.ts`,
 * 业务代码一律从那里 import(它原样转出这些)。
 */

export interface IndustryTaxonomyCategory {
    key: string;
    name: string;
    subcategories: string[];
}

export interface IndustryTaxonomy {
    version: string;
    categories: IndustryTaxonomyCategory[];
}

/** 长得像字典 key 的串:小写 ASCII 开头,只含小写字母、数字、下划线。 */
export function looksLikeCategoryKey(value: string): boolean {
    return /^[a-z][a-z0-9_]*$/.test(value);
}

/**
 * 页面上显示的行业大类名。四种输入,各有去处(判据逐种钉):
 *   ① 新写入的字典 key            ⇒ 字典里的中文名(优先用后端附的 `name`);
 *   ② 存量旧中文名                ⇒ 原样(它本来就是给人看的字);
 *   ③ 认不出的英文 key / 字典未到 ⇒ ''(**宁可不显示,也不把 key 露给用户**);
 *   ④ 空                          ⇒ ''。
 * 后端附的 `name` 在认不出时会回原值 —— 所以它自己长得像 key 时同样按 ③ 处理。
 */
export function industryCategoryText(
    raw: string | null | undefined,
    name: string | null | undefined,
    nameOf: (key: string) => string,
): string {
    const given = String(name ?? '').trim();
    if (given && !looksLikeCategoryKey(given)) return given;
    const value = String(raw ?? '').trim();
    if (!value) return '';
    if (looksLikeCategoryKey(value)) return nameOf(value);
    return value;
}

/**
 * 原值 → 字典 key(下拉的选中项 / 按大类筛选用)。新写入的原值本身就是 key(须在字典里);
 * 存量旧中文名只在它恰好等于某个大类名时能对上;其余一律 ''(= 未选定,不猜)。
 */
export function categoryKeyFromRaw(raw: string | null | undefined, taxonomy: IndustryTaxonomy | null): string {
    const value = String(raw ?? '').trim();
    if (!value || !taxonomy) return '';
    if (looksLikeCategoryKey(value)) return taxonomy.categories.some((c) => c.key === value) ? value : '';
    return taxonomy.categories.find((c) => c.name === value)?.key || '';
}

export function nameOfFrom(taxonomy: IndustryTaxonomy | null): (key: string) => string {
    return (key: string) => taxonomy?.categories.find((c) => c.key === key)?.name || '';
}

/** `GET /api/industry-taxonomy` 回包 → 字典;形状不对就是 null(不拼一个半截字典)。 */
export function parseTaxonomy(data: unknown): IndustryTaxonomy | null {
    const d = data as { version?: unknown; categories?: unknown } | null;
    if (!d || !Array.isArray(d.categories)) return null;
    const categories = d.categories
        .map((c) => c as { key?: unknown; name?: unknown; subcategories?: unknown })
        .filter((c) => typeof c.key === 'string' && typeof c.name === 'string' && c.key && c.name)
        .map((c) => ({
            key: String(c.key),
            name: String(c.name),
            subcategories: Array.isArray(c.subcategories) ? c.subcategories.map(String) : [],
        }));
    return categories.length ? { version: String(d.version ?? ''), categories } : null;
}
