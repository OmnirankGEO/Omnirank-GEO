/**
 * 客户线上购买门控 · 客户侧文案与判定消费层(2026-07-29)
 *
 * 工单:docs/AI-CONTEXT/WORKORDER_CLIENT_PURCHASE_GATE_2026-07-27.md
 *
 * 白标铁律(硬约束,改这里前先读工单 §0):
 *   客户面前**不得出现**服务商名称/公司名/账号名,也不得出现
 *   "服务商 / 代理 / 主账号 / 销售"等内部角色词。统一称呼定死为「推荐人」。
 *   本文件是客户侧拦截文案的**唯一**来源,tests/ 有全文扫描锁盯着它。
 *
 * 判定单一源:
 *   能否购买只看后端下发的 wallet.canPurchaseOnline(来自 GET /api/wallet 的
 *   can_purchase_online)。前端**绝不**自己拼"客户级覆盖 + 主账号默认"两级逻辑。
 */

/** 拦截提示主文案 · 与后端 services/client_purchase_gate.BLOCKED_MESSAGE 逐字一致。 */
export const ONLINE_PURCHASE_BLOCKED_MESSAGE = '如需充值或购买，请联系您的推荐人办理';

/** 弹窗标题 · 同样零内部术语。 */
export const ONLINE_PURCHASE_BLOCKED_TITLE = '当前账户暂不支持线上购买';

/** 弹窗补充说明 · 只说"怎么办",不解释是谁关的、也不提任何账号。 */
export const ONLINE_PURCHASE_BLOCKED_HINT =
  '你的算力与服务由推荐人统一安排，购买和续费请直接联系推荐人办理。';

/** 后端拦截时返回的 error code(HTTP 403 detail.code)。 */
export const ONLINE_PURCHASE_BLOCKED_CODE = 'ONLINE_PURCHASE_BY_REFERRER_ONLY';
