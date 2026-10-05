/**
 * 报价「单据日期」口径 —— 单点(SSOT)。
 *
 * ## 为什么需要这个文件
 *
 * [WO_QUOTE_LIST_DATE 2026-08-05] 报价列表原来把 `keyword_selection_sessions.updated_at`
 * 当单据日期渲染。`updated_at` 是**行最后一次被写的时间**,不是业务时间:
 *
 * 生产实证(2026-08-08 只读取证):
 *   · 会话 194 —— 建单 07-23 15:08 / 提交词 07-23 15:19 / 定价生成 07-23 15:21,
 *     而 `updated_at` = **08-04 16:08**。代理看到的是「2026/8/4」,
 *     一张 12 天前的老单长得像今天刚出的单(Owner 因此误判"今天还在出信息词")。
 *   · 全库 168 个会话里 **104 个** `updated_at` 与 `created_at` 不在同一天 = **62%**
 *     的列表行日期是错的。其中 2026-07-22 单日就有 **54 个**会话的 `updated_at`
 *     被同一次批量写顶到同一天。
 *
 * ## 口径(Owner 2026-08-08 拍板)
 *
 *   提交关键词时间 → 建单时间,**两级回落,到此为止**。
 *
 * 🔴 **绝不回落到 `updated_at`** —— 那正是要修掉的东西。两个业务时间都没有时
 * 宁可**不显示日期**,也不显示一个会自己变的日期(显示一个错日期比不显示更坏:
 * 前者代理会当真)。
 *
 * 可用率(同一次只读取证):`created_at` 168/168 · `keywords_submitted_at` 131/168 ·
 * 定价生成时间 120/168。所以两级回落对会话行实际上恒有值;
 * "取不到"这一支留给 quotes 兜底占位行(`is_quote_placeholder`)。
 *
 * ## 为什么不用「定价生成时间」做主显
 *
 * 它最贴近"这张单什么时候出的价",但它躺在 `pricing_data` 这个 TEXT 列的 JSON 里,
 * **列表端点没 SELECT 它**,要接线就得同时改 `api/selection_api.py` 与
 * `db/diagnosis_db.py`。生产样本里它与提交词时间几乎总在同一天(见 §E 取证),
 * 收益极小而撞车面从 1 个文件扩到 3 个。Owner 拍板走提交词时间。
 */

export interface QuoteOrderDateSource {
  /** 客户提交关键词的时间 —— 主显。 */
  keywords_submitted_at?: string | null;
  /** 建单(生成选词链接)时间 —— 回落。 */
  created_at?: string | null;
}

/**
 * 返回该单的**业务时间** ISO 串;两个业务时间都没有时返回 `null`。
 *
 * 🔴 入参类型刻意**不含** `updated_at` —— 让"手滑把 updated_at 传进来当单据日期"
 * 在类型层就不成立,而不是靠注释提醒。
 */
export function quoteOrderDateIso(session: QuoteOrderDateSource | null | undefined): string | null {
  if (!session) return null;
  const submitted = (session.keywords_submitted_at || '').trim();
  if (submitted) return submitted;
  const created = (session.created_at || '').trim();
  if (created) return created;
  return null;
}

/** 渲染用:`zh-CN` 日期串;取不到业务时间时返回空串(不显示,不编一个)。 */
export function formatQuoteOrderDate(session: QuoteOrderDateSource | null | undefined): string {
  const iso = quoteOrderDateIso(session);
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleDateString('zh-CN');
}

/**
 * 排序键:与**显示**同源。
 *
 * 排序若还按 `updated_at`,列表会出现"上面那行写着 07-23、下面那行写着 08-06"
 * 这种看不懂的顺序 —— 日期列与排序必须是同一个口径。
 * 取不到业务时间的行排最后(返回 0)。
 */
export function quoteOrderDateSortKey(session: QuoteOrderDateSource | null | undefined): number {
  const iso = quoteOrderDateIso(session);
  if (!iso) return 0;
  const t = new Date(iso).getTime();
  return Number.isNaN(t) ? 0 : t;
}
