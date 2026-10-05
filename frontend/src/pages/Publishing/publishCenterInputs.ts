/**
 * 发布中心两个纯输入逻辑 —— 抽出来是为了能拿真数据逐条核，而不是靠源码串断言。
 *
 * ① `filterPublishProjects`  客户反馈①「品牌选择器加搜索」的过滤口径
 * ② `mediaNeedsRegionRemark` 客户反馈②「发布带地区备注」的**媒体信号**判定
 *
 * ────────────────────────────────────────────────────────────────────────
 * 🔴 ② 的信号为什么跟工单写的不一样(2026-08-09 replica 生产快照实测,52166 行 mhz_media)
 * ────────────────────────────────────────────────────────────────────────
 * 工单原文信号 = `area 为空 AND remark ILIKE '%指定地区%'`。实测:
 *   - `area 为空` 这一条**做了零功: `area 非空 AND remark 含'指定地区'` = **0 行**,
 *     也就是说它一行都没排除掉,是个恒真的合取项;
 *   - 而它漏掉了 `车主之家随机(可指定地区)`(id=100585768,area='全国',
 *     remark='所有的地方站都可以指定发')—— 名字里明写"可指定地区",
 *     "可指定地区"这四个字长在 **media_name** 上而不是 remark 上。
 * 于是这里取:`media_name 含'可指定地区'` **或** `remark 含'指定地区'`。
 * 实测命中 2 行(列举网 + 车主之家),原口径 1 行。两条都在 `is_active` 池里。
 *
 * 🔴 刻意**不**把 `remark 含'备注'` 收进来:那会命中 748 行,绝大多数是
 *   "有要求的提前备注清楚"这类通用套话,跟"指定地区"无关 —— 收进来等于对
 *   1.4% 的媒体都弹一个输入框,是噪音不是提示。
 * 🔴 自媒体侧(mhz_wemedia)同口径实测 **0 命中**,所以自媒体面不出这个输入框;
 *   后端字段照样接(同一个请求模型),不构成死字段——它由本函数控制何时被填。
 */

export interface ProjectLike {
  id: number;
  brand_name: string;
  industry?: string;
  keyword_count?: number;
}

/** ① 按名称 / 行业即输即滤。空查询 = 原样返回(不改变默认行为)。 */
export function filterPublishProjects<T extends ProjectLike>(projects: T[], query: string): T[] {
  const q = (query || '').trim().toLowerCase();
  if (!q) return projects;
  return projects.filter(p =>
    (p.brand_name || '').toLowerCase().includes(q) ||
    (p.industry || '').toLowerCase().includes(q)
  );
}

export interface MediaLike {
  media_name?: string;
  remark?: string;
}

/**
 * ② 单个媒体是否需要「地区备注」。
 *
 * 🔴 [2026-08-14 收紧] 必须**同时**含「列举网」——原口径只看「可指定地区/指定地区」，
 *   会命中 `车主之家随机(可指定地区)`(id 100585768)。但拿到供应商文档 2.0 后确认：
 *   `admin_remark` 是**三选一枚举**，地区那条**只有**「列举网指定地区xx」，
 *   **没有车主之家**。给车主之家弹输入框 = 让用户白填一个发不出去的值
 *   (会被 `services/kuaiyibo/order_remark.is_valid_admin_remark` 拦下)。
 *   —— 提示只该出现在真能生效的地方，否则是骗用户。
 *
 * 同时排除 `北京列举网` / `成都列举网` 这类**名字里已带城市**的地方站：
 *   它们的 remark 不含「指定地区」，本来就不走备注这条路。
 */
export function mediaNeedsRegionRemark(media: MediaLike | null | undefined): boolean {
  if (!media) return false;
  const name = String(media.media_name || '');
  const remark = String(media.remark || '');
  if (!name.includes('列举网')) return false;
  return name.includes('可指定地区') || remark.includes('指定地区');
}

/** ② 一批媒体里需要地区备注的那些(给提示文案点名用)。 */
export function mediaNeedingRegionRemark<T extends MediaLike>(list: T[]): T[] {
  return (list || []).filter(mediaNeedsRegionRemark);
}

export interface CartMediaLike {
  name?: string;
  needsRegionRemark?: boolean;
}

/**
 * ② 购物车条目是否需要地区备注。
 *
 * 🔴 为什么不是简单读 `needsRegionRemark`:购物车整车落 localStorage,
 *   本次改动之前存下来的车**没有这个字段**。旧车里只剩 `name`,
 *   所以按名称再兜一次 —— 当前两个命中媒体("列举网(可指定地区)" /
 *   "车主之家随机(可指定地区)")名字里都带这四个字,兜得住。
 *   兜底只放宽不收紧:字段为 true 时直接 true。
 */
export function cartMediaNeedsRegionRemark(item: CartMediaLike | null | undefined): boolean {
  if (!item) return false;
  if (item.needsRegionRemark === true) return true;
  return mediaNeedsRegionRemark({ media_name: item.name });
}

/** ② 备注长度上限 —— 上游下单接口的 order_remark 是自由文本，我们自己收口防误粘长文。 */
export const REGION_REMARK_MAX_LEN = 200;

/**
 * 🔴 [2026-08-14 开闸] 地区备注恢复启用 —— 契约已拿到。
 *
 * 停用原委(2026-08-10):生产真单 A/B,同文章 / 同媒体 / 同账号,只差备注字段
 *   订单 479 `QA-BACKENDTEST-20260810-NEG 投放地区:深圳`(35字) → failed
 *   订单 480 `投放地区深圳`(6字纯中文)                        → failed
 *   订单 481 不传该字段                                        → completed ✅
 * 回包逐字相同:「备注字段内容不合法」。当时结论只到「一填就死」。
 *
 * 真根因(2026-08-14 在 `docs/AI-CONTEXT/快易播/` 找到供应商文档 2.0):
 *   `admin_remark` **不是自由文本,是三选一的枚举**,地区那条格式固定为
 *   `列举网指定地区xx`。479/480 两条都不在枚举里,所以必被拒 ——
 *   **不是格式/编码问题,是值不在允许集合里**。
 *
 * 开闸的两个前提都已具备:
 *   ① 用户不再手打备注 —— 只填**地区名**,由 `buildRegionRemark()` 拼契约格式;
 *   ② 后端两道闸:`order_remark.is_valid_admin_remark()` 白名单 fail-fast +
 *      `remark_applies_to_media()` **按媒体逐条判定**(479 的失败媒体
 *      100511350 根本不是列举网 —— 备注被全批同传,拖死了不相干的媒体)。
 *
 * ⚠️ 前端这一处只是不让用户白填;真正兜住的是后端。
 * 两侧同时翻开才算恢复:另一侧是 `MHZ_ORDER_REMARK_ENABLED=1`
 * (`services/meijiehezi/config.py`)。
 */
export const REGION_REMARK_ENABLED = true;

/** ② 契约前缀 —— 与 `services/kuaiyibo/order_remark.REGION_REMARK_PREFIX` 必须一致。 */
export const REGION_REMARK_PREFIX = '列举网指定地区';

/** ② 地区名长度上限(文档未规定,我方自设防误粘)。 */
export const REGION_NAME_MAX_LEN = 20;

/**
 * ② 把用户填的**地区名**拼成供应商契约格式。
 * 🔴 用户永远不该看见、更不该手打这个前缀 —— 格式是我们的事。
 * 空地区返回空串 = 不下发备注(该字段非必填)。
 */
export function buildRegionRemark(region: string | null | undefined): string {
  const r = String(region || '').trim().slice(0, REGION_NAME_MAX_LEN);
  return r ? `${REGION_REMARK_PREFIX}${r}` : '';
}

/** ② 从契约格式取回地区名(回显 / 二次编辑用)。非该格式返回空串。 */
export function extractRegionName(remark: string | null | undefined): string {
  const s = String(remark || '').trim();
  return s.startsWith(REGION_REMARK_PREFIX) ? s.slice(REGION_REMARK_PREFIX.length) : '';
}

/** ② 提交前把备注收敛成上送值:去首尾空白 + 截断;空串 = 不上送(非必选)。 */
export function normalizeRegionRemark(raw: string | null | undefined): string {
  return String(raw || '').trim().slice(0, REGION_REMARK_MAX_LEN);
}
