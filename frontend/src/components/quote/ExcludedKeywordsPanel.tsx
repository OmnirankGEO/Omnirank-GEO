/**
 * 报价意图闸 · 被排除关键词只读区(WO-QUOTE-INTENT-GATE-2026-08-04)
 *
 * Owner 2026-08-04 拍板:「全禁进计价明细 + 单列只读展示区」——
 *   被排除的词绝不出现在计价明细里(后端就没放进 pricing_data.keywords),
 *   但也不静默消失:这里单列出来 + 给原因,代理才知道系统砍了什么、为什么。
 *
 * 提示铁律(2026-08-04):要么帮人解决,要么不显示。所以本组件不止说"排除了 N 个",
 * 还把候选池里现成可用的商业词直接列出来(后端 suggested_keywords,零成本零等待)。
 *
 * 代理端(OnlineQuoteFlow)与客户端(SelectionPage)共用同一个组件 —— 元指令 9:
 * C 端与代理端同源,不许一边改一边不改。
 */

export interface ExcludedKeyword {
  keyword: string;
  intent_type?: string;
  reason_text?: string;
  needs_clarification?: boolean;
}

export interface SuggestedKeyword {
  id?: number;
  keyword: string;
  category_label?: string;
  intent_type?: string;
}

interface Props {
  excluded?: ExcludedKeyword[];
  suggested?: SuggestedKeyword[];
  /** "unavailable" = 本次核验服务不可用,必须明示未核验(不许假装核过) */
  gateStatus?: string;
  className?: string;
}

export function ExcludedKeywordsPanel({
  excluded = [],
  suggested = [],
  gateStatus = 'active',
  className = '',
}: Props) {
  // 降级态:核验没跑成,照实说。这条优先于"没排除词就不显示"——
  // "没排除"和"没核验"是两回事,混为一谈就是骗人。
  if (gateStatus === 'unavailable') {
    return (
      <div className={`bg-amber-50 border border-amber-200 rounded-2xl px-5 py-3 ${className}`}>
        <p className="text-sm font-medium text-amber-700">
          ⚠️ 本次未能完成关键词投放价值核验
        </p>
        <p className="text-xs text-amber-600 mt-1">
          核验服务暂时不可用，以上关键词均按原样计价。建议稍后重新生成报价。
        </p>
      </div>
    );
  }

  if (!excluded.length) return null;

  return (
    <div className={`bg-slate-50 border border-slate-200 rounded-2xl px-5 py-4 ${className}`}>
      <p className="text-sm font-semibold text-slate-700">
        已排除 {excluded.length} 个不建议投放的词（不计费）
      </p>
      <p className="text-xs text-slate-500 mt-1">
        用户问 AI 这类问题时，AI 的回答里不会推荐服务商——投了也拿不到推荐位。
      </p>

      <ul className="mt-3 space-y-1.5">
        {excluded.map(item => (
          <li key={item.keyword} className="text-xs">
            <span className="text-slate-600 line-through">{item.keyword}</span>
            {item.reason_text && (
              <span className="text-slate-400 ml-2">· {item.reason_text}</span>
            )}
          </li>
        ))}
      </ul>

      {suggested.length > 0 && (
        <div className="mt-4 pt-3 border-t border-slate-200">
          <p className="text-xs font-medium text-emerald-700">
            这些词客户会真的拿来挑服务商，可以直接加进报价：
          </p>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {suggested.map(s => (
              <span
                key={s.keyword}
                className="inline-flex items-center rounded-full bg-emerald-50 border border-emerald-200 px-2.5 py-1 text-xs text-emerald-700"
              >
                {s.keyword}
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export default ExcludedKeywordsPanel;
