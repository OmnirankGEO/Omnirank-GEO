/**
 * [WO_267] 行业大类下拉:选项来自后端字典(`GET /api/industry-taxonomy`),**只提交字典 key**。
 *
 * 🔴 父层只在用户**真的动过**它之后,才把 `industry_category` 放进保存请求:
 *    后端把空串当「清空、回到按行业文字自动判」,把任何 key 当「用户选定、此后以它为准」——
 *    每次保存都带上它,等于替一个根本没碰下拉的用户清掉了自动判断,或者把它锁成了人工选定。
 * 字典没到就不渲染(不给一个只有「自动」一项的假控件)。
 */
import { categoryKeyFromRaw, useIndustryTaxonomy } from '@/lib/industryTaxonomy';

export function IndustryCategorySelect({
    raw,
    value,
    touched,
    onPick,
}: {
    /** 品牌当前存的原值(存量旧中文名 / 新写入的 key) */
    raw?: string | null;
    /** 用户选的 key;`touched` 为真时才生效 */
    value: string;
    touched: boolean;
    onPick: (key: string) => void;
}) {
    const { taxonomy } = useIndustryTaxonomy();
    if (!taxonomy) return null;
    const current = touched ? value : categoryKeyFromRaw(raw, taxonomy);
    return (
        <select
            aria-label="行业大类"
            data-testid="industry-category-select"
            className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
            value={current}
            onChange={(e) => onPick(e.target.value)}
        >
            <option value="">按行业文字自动判断</option>
            {taxonomy.categories.map((c) => (
                <option key={c.key} value={c.key}>{c.name}</option>
            ))}
        </select>
    );
}
