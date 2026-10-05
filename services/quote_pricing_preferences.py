"""Private quote pricing preferences for agents.

This layer only controls the agent-facing quote calculation multiplier. It must
never be shown on customer-facing quote or portal pages.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("QuotePricingPreferences")

DEFAULT_ARTICLE_COST_YUAN = 60.0
DEFAULT_QUOTE_MARKUP_RATIO = 1.0  # [§4.5/决策5 2026-06-06] 默认回成本·operator 自设利润
MIN_QUOTE_MARKUP_RATIO = 1.0
MAX_QUOTE_MARKUP_RATIO = 5.0


def normalize_quote_markup_ratio(value: object) -> float:
    """Normalize user input to a sane private markup multiplier."""
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return DEFAULT_QUOTE_MARKUP_RATIO

    if ratio < MIN_QUOTE_MARKUP_RATIO:
        return MIN_QUOTE_MARKUP_RATIO
    if ratio > MAX_QUOTE_MARKUP_RATIO:
        return MAX_QUOTE_MARKUP_RATIO
    # [v12 item6] 系数量化统一走 canonical(与 SKU 链 resolve_canonical_markup 同一口径 Decimal ROUND_HALF_UP)·
    #   禁 round(ratio,2)(float/banker 误差 · 与 SKU 链量化不一致致同系数两套价)。
    from services.agent_pricing import resolve_canonical_markup
    return float(resolve_canonical_markup(ratio)["ratio"])


def get_user_quote_markup_ratio(user_id: Optional[int]) -> Optional[float]:
    """Return a user's private quote multiplier, or None when unavailable."""
    if not user_id:
        return None

    try:
        from db.auth_db import get_user

        user = get_user(int(user_id))
        if not user:
            return None
        value = user.get("quote_markup_ratio")
        if value is None:
            return None
        return normalize_quote_markup_ratio(value)
    except Exception as exc:
        logger.warning("读取用户报价系数失败 user_id=%s: %s", user_id, exc)
        return None


def get_system_quote_markup_ratio() -> float:
    """Return the system default quote multiplier."""
    try:
        from config.settings_manager import get_current_settings

        return normalize_quote_markup_ratio(get_current_settings().quote_markup_ratio)
    except Exception:
        return DEFAULT_QUOTE_MARKUP_RATIO


def get_quote_markup_for_user(user_id: Optional[int]) -> float:
    """Resolve quote multiplier: admin override -> user private preference -> system setting -> default.

    [D3] 优先级: 平台后台 admin 强制 override > 服务商自设 quote_markup_ratio > 全局默认。
    传入的 user_id 应为【服务商本人】;被邀请客户的继承由 D4 在调用前先 resolve 上级 agent_user_id。
    """
    # [D3] admin 强制 override 优先(平台给单个服务商设的系数·丰俭由人)
    try:
        from services.agent_pricing_overrides import get_agent_quote_markup_override
        admin_ov = get_agent_quote_markup_override(user_id)
        if admin_ov is not None:
            return normalize_quote_markup_ratio(admin_ov)
    except Exception as exc:
        logger.warning("读取 admin 报价系数 override 失败 user_id=%s(回落自设/默认): %s", user_id, exc)
    user_ratio = get_user_quote_markup_ratio(user_id)
    if user_ratio is not None:
        return user_ratio
    return get_system_quote_markup_ratio()


def get_quote_markup_override_for_user(user_id: Optional[int]) -> Optional[float]:
    """Return an override only when it differs from the system default.

    [D3] 解析最终有效系数(admin override > 自设 > 默认),仅当 != 系统默认时返回 override →
    触发 generate_batch_quote(markup_override=) 重算 · 绕过全局共享价格锁缓存写入。

    [B2 缓存防污染铁律] 全局共享 keyword_price_cache 只存【系统默认系数】下的基线价;
    per-agent / 被邀请继承系数一律走此 override 在【读取/渲染时】套用,绝不写回共享缓存
    (否则 A 服务商系数会泄漏污染 B 服务商及所有客户 + 破坏 7 天价格锁全局一致铁律)。
    """
    effective = get_quote_markup_for_user(user_id)
    system_ratio = get_system_quote_markup_ratio()
    if abs(effective - system_ratio) < 0.001:
        return None
    return effective


# ============================================================
# D4: 被邀请客户随【第一层服务商】系数(归属解析 + viewer 解析)
# ============================================================

def resolve_owning_agent(customer_user_id: Optional[int]) -> Optional[int]:
    """Resolve the internal commercial service principal.

    Promotion and invitation relationships never participate. No binding uses
    the controlled platform-direct account; dirty state fails closed.
    """
    if not customer_user_id:
        return None
    try:
        from db.connection import get_db
        from services.commercial_service_routing import resolve_commercial_relationship
        with get_db() as conn:
            return resolve_commercial_relationship(
                conn.cursor(), int(customer_user_id)
            ).service_user_id
    except Exception as exc:
        logger.warning("commercial pricing resolution failed type=%s", type(exc).__name__)
        return None


def _get_agent_level(user_id: Optional[int]) -> int:
    """读 user_wallets.agent_level(0=客户 / >=1=服务商)。异常 → 0。"""
    if not user_id:
        return 0
    try:
        from db.connection import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT agent_level FROM user_wallets WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            if not row:
                return 0
            v = row["agent_level"] if isinstance(row, dict) else row[0]
            return int(v or 0)
    except Exception:
        return 0


def is_l0_quote_markup_self_edit_enabled() -> bool:
    """[报价系数第二步·灰度] 无 owner 的 L0 普通用户是否可自设报价覆盖系数。

    默认 False:无 owner 的孤立 L0 报价回落系统默认(防历史残留 quote_markup_ratio 脏数据生效)。
    True(admin 后台 system_settings / env 开):无 owner L0 自设系数生效。
    与 V3.3.1 总开关解耦(走 get_flag · 不走 is_enabled 的 V3_3_1_ 前缀/总开关门控)。
    env 名 = L0_QUOTE_MARKUP_SELF_EDIT_ENABLED。任何异常一律 False(失败安全)。
    """
    try:
        from config.v3_3_1_flags import get_flag
        # [对抗审计 P1] is True 而非 bool():防 system_settings.value_type 误存 'string' 时
        #   _coerce 返字符串 'false' → bool('false')==True 误开 flag。只认真 bool True(失败安全)。
        return get_flag("L0_QUOTE_MARKUP_SELF_EDIT_ENABLED") is True
    except Exception:
        return False


def resolve_pricing_user_id(current_user_id: Optional[int]) -> Optional[int]:
    """[历史/继承语义 · 当前 dormant] 被邀请客户随上级解析(06-04 口径)。

    ⚠️ 2026-06-08 老板拍板:**报价中心(给终端客户报价)绝不继承上级**,已改走
       resolve_quote_pricing_user_id(永远本人)。此函数保留原始"有上级就继承"语义,
       仅供算力/充值/进货等"上级深度绑定"链路或将来需要时使用,**不要再用于报价中心**。
      - 服务商本人(agent_level>=1) → 本人
      - 客户(agent_level=0) 有归属 → 邀请他的【第一层服务商】(继承)
      - 客户(agent_level=0) 无归属 → flag 关(默认)None 回落系统默认 / flag 开 本人
    """
    if not current_user_id:
        return current_user_id
    if _get_agent_level(current_user_id) >= 1:
        return current_user_id
    owner = resolve_owning_agent(current_user_id)
    if owner:
        return owner
    if is_l0_quote_markup_self_edit_enabled():
        return current_user_id
    return None


def _organization_principal_for(current_user_id: Optional[int]) -> Optional[int]:
    """当前用户若是**组织员工席位**,返回其所属经营主体(团队长)的 user_id;否则 None。

    任何异常一律 None(fail-closed:退回原有"本人"语义,不会把定价权错给别人)。
    """
    if not current_user_id:
        return None
    try:
        from db.organization_db import resolve_identity

        identity = resolve_identity(int(current_user_id))
        if identity is None or not identity.is_member:
            return None
        if identity.membership_status != "active" or identity.organization_status != "active":
            return None
        principal = int(identity.principal_user_id)
        return principal if principal and principal != int(current_user_id) else None
    except Exception:
        return None


def resolve_quote_pricing_user_id(current_user_id: Optional[int]) -> Optional[int]:
    """[报价中心 · 2026-06-08 老板拍板] 给终端客户报价永远用【本人】· 与上级完全无关。

    报价中心是用户给自己终端客户做代运营报价的工具;他想赚多少是自己的经营决策。
    上级只在算力/充值/进货/返利链路赚钱,**不干预**他对终端客户怎么报价。
    因此报价系数 / 单篇成本一律按当前登录用户本人解析,不查 resolve_owning_agent、不继承上级。

    [2026-07-25 组织席位例外]「永远本人」针对的是**邀请人 → 被邀请人**这条分销链:
    下游是独立经营者,他怎么报价是他自己的事。**组织员工不是下游经营者,而是同一家
    公司的手** —— 团队长与员工共用一个经营主体、一个钱包、一份客户。

    若这里仍按"本人"解析,员工的 agent_level 恒为 0、也没有自设系数,报价会静默
    回落到平台底价(markup=1.0、无单篇成本覆盖)—— **老板配置的加价被丢掉,员工
    发出去的每一单都不赚钱**;更糟的是员工自己签一份《报价定价免责协议》就能让
    **他自己**的系数去定客户价。两者都不是老板要的。

    因此:员工席位 → 解析为所属团队长;其余一切情况 → 维持"本人"语义不变。
    注意这里只是**用**老板的系数算价,员工并不因此获得查看成本/毛利的权限
    (那仍是 owner-only,见 selection_api 的内部字段脱敏)。
    """
    principal = _organization_principal_for(current_user_id)
    if principal:
        return principal
    return current_user_id


def get_quote_markup_for_viewer(current_user_id: Optional[int]) -> float:
    """[D4] 报价入口用:resolve 系数归属(被邀请客户→上级)后取有效 markup。"""
    return get_quote_markup_for_user(resolve_pricing_user_id(current_user_id))


def get_quote_markup_override_for_viewer(current_user_id: Optional[int]) -> Optional[float]:
    """[D4] 报价入口用:resolve 归属后取 markup override(!=系统默认才返 · B2 不写共享缓存)。"""
    return get_quote_markup_override_for_user(resolve_pricing_user_id(current_user_id))


# ============================================================
# M2 2026-06-07: 服务商自设单篇成本 cost_per_article(A 完全覆盖 · 镜像 markup 的 viewer 归属解析)
# ============================================================

def get_user_cost_per_article(user_id: Optional[int]) -> Optional[float]:
    """读 user.cost_per_article(服务商自设单篇内容成本)· 未设(NULL)返 None(算价走系统动态成本)。"""
    if not user_id:
        return None
    try:
        from db.auth_db import get_user
        user = get_user(user_id)
        if not user:
            return None
        v = user.get("cost_per_article")
        if v is None:
            return None
        c = float(v)
        return c if c > 0 else None
    except Exception as exc:
        logger.warning("get_user_cost_per_article 失败 user=%s: %s", user_id, exc)
        return None


def get_cost_per_article_for_viewer(current_user_id: Optional[int]) -> Optional[float]:
    """[M2 A完全覆盖] 报价入口用:resolve 归属(被邀请客户→第一层服务商·同 markup 口径)后取服务商自设单篇成本。
    返回 None = 服务商未设 → 算价走系统动态成本(竞品来源加权 35-350 / 兜底 ¥60)。
    非 None = 服务商自设值 → 完全覆盖系统动态(与 M1 成本地板联动防亏)。
    """
    return get_user_cost_per_article(resolve_pricing_user_id(current_user_id))


# ============================================================
# [报价中心 · 2026-06-08 老板拍板] 给终端客户报价专用 viewer:永远本人 · 绝不继承上级 · 平台默认固定 1.0。
#   报价中心 = 代运营报价工具,只服务用户给自己终端客户报价;上级只赚算力/充值/进货/返利链路,不干预客户报价。
#   [返修点2] 平台默认报价系数固定 = DEFAULT_QUOTE_MARKUP_RATIO(1.0)· users.quote_markup_ratio 列默认 1.0
#     即"本人按成本基线",不区分"未设 vs 自设 1.0"(都 = 1.0 = 按成本不加价)· 不宣称未设跟随平台默认漂移。
#   [返修点3] 签约闸"也管生效":未签普通用户 → 忽略本人自设系数/成本、用平台默认/动态(不止管保存)。
#   server.py / selection_api 报价生成已切到这组(原 *_for_viewer 历史继承语义 dormant)。
# ============================================================

def _can_use_self_pricing(user_id: Optional[int]) -> bool:
    """[报价中心·签约闸也管生效] 本人自设报价系数/成本是否生效:
      服务商(agent_level>=1)/ admin → True;普通用户须已签《报价定价免责协议》;未签普通用户 → False(用平台默认/动态)。
    [P1-2] admin(可能 agent_level=0 且未签)也豁免 · 与 auth_api GET 把 admin 当可编辑一致。
    任何异常一律 False(失败安全·防未签历史/脏值漏用)。
    """
    if not user_id:
        return False
    try:
        if _get_agent_level(user_id) >= 1:
            return True
        from services.agent_agreement import is_pricing_disclaimer_signed
        if is_pricing_disclaimer_signed(user_id):
            return True
        # [P1-2] admin 豁免(非服务商等级/未签也算)· 与 auth_api GET is_provider 含 admin 一致
        from db.auth_db import get_user
        u = get_user(user_id)
        return bool(u and u.get("is_admin"))
    except Exception:
        return False


def get_quote_markup_for_quote_viewer(current_user_id: Optional[int]) -> float:
    """[报价中心] 客户售价系数:平台 admin override > 本人自设(仅服务商/已签生效)> 平台默认(固定 1.0)· 绝不继承上级。"""
    uid = resolve_quote_pricing_user_id(current_user_id)
    try:
        from services.agent_pricing_overrides import get_agent_quote_markup_override
        ov = get_agent_quote_markup_override(uid)
        if ov is not None:
            return normalize_quote_markup_ratio(ov)
    except Exception as exc:
        logger.warning("get_quote_markup_for_quote_viewer override 读取失败 user=%s: %s", uid, exc)
    if _can_use_self_pricing(uid):
        own = get_user_quote_markup_ratio(uid)
        if own is not None:
            return own
    return DEFAULT_QUOTE_MARKUP_RATIO


def get_quote_markup_override_for_quote_viewer(current_user_id: Optional[int]) -> Optional[float]:
    """[报价中心] 本人有效系数 != 平台默认(固定 1.0)时返 override(触发 per-agent 重算 · 不写共享缓存)· 绝不继承上级。"""
    effective = get_quote_markup_for_quote_viewer(current_user_id)
    if abs(effective - DEFAULT_QUOTE_MARKUP_RATIO) < 0.001:
        return None
    return effective


def get_cost_per_article_for_quote_viewer(current_user_id: Optional[int]) -> Optional[float]:
    """[报价中心] 单篇成本:本人自设(仅服务商/已签生效)· 否则 None(系统动态/平台默认 35-350 / 兜底 ¥60)· 绝不继承上级。"""
    uid = resolve_quote_pricing_user_id(current_user_id)
    if _can_use_self_pricing(uid):
        return get_user_cost_per_article(uid)
    return None


# ============================================================
# [v2.1 2026-06-11 老板拍] 下级扫码用户的进货成本倍率(发布投放媒体资源分级卖价)
#   平台卖 ¥100 · 上级服务商 SKU 系数 2 → 下级真实进货成本 ¥200。
#   下级用报价工具"系统自动估"单篇成本时必须 × 上级系数,否则按平台价算报价 = 低于真实成本 = 亏。
#   ⚠️ 跟报价系数(quote_markup_ratio · 卖客户加价)是两个系数:这个是【进货成本】层。
# ============================================================

def get_procurement_cost_multiplier_for_quote_viewer(current_user_id: Optional[int]) -> float:
    """报价工具"系统自动估"的单篇成本进货倍率。

    解析:
      - 服务商本人(agent_level >= 1 · 直充平台进货)→ 1.0(进货价 = 平台价)
      - 扫码下级(L0 有归属上级)→ 上级的 SKU 加价倍数:
          admin 强制 override(agent_pricing_overrides)> 上级自设 users.agent_sku_markup_ratio > 1.0(未设=没加价)
      - 无归属普通用户 / 异常 → 1.0(fail-safe · 按平台价)

    不作用于自设单篇成本(cost_per_article_override):老板口径"填了数报价就按你的真实成本算" —
    用户自己填的就是含进货倍率的真实成本,再乘 = 双重。
    口径限制(知情):上级只逐包设 retail_cents 没设全局倍数时,本倍率取 1.0(近似 · 全局系数是老板拍的口径)。
    """
    if not current_user_id:
        return 1.0
    try:
        if _get_agent_level(current_user_id) >= 1:
            return 1.0
        owner = resolve_owning_agent(current_user_id)
        if not owner:
            return 1.0
        # admin 给上级的 SKU 系数强制 override 优先(平台限定该服务商零售系数)
        try:
            from services.agent_pricing_overrides import get_agent_sku_markup_override
            ov = get_agent_sku_markup_override(owner)
            if ov is not None and float(ov) > 0:
                return round(float(ov), 2)
        except Exception as exc:
            logger.warning("procurement_multiplier: admin override 读取失败 owner=%s: %s", owner, exc)
        from db.auth_db import get_user
        u = get_user(int(owner))
        if u and u.get("agent_sku_markup_ratio") is not None:
            r = float(u["agent_sku_markup_ratio"])
            if r > 0:
                return round(r, 2)
        return 1.0
    except Exception as exc:
        logger.warning("procurement_multiplier 解析失败 user=%s(回落 1.0): %s", current_user_id, exc)
        return 1.0


def _sku_markup_of_agent(agent_user_id: Optional[int]) -> float:
    """某服务商【卖给其下级】的媒体 SKU 加价倍率:admin override > users.agent_sku_markup_ratio > 1.0。
    与 get_procurement_cost_multiplier_for_quote_viewer 内联逻辑同源,抽出供累计解析复用。"""
    if not agent_user_id:
        return 1.0
    try:
        from services.agent_pricing_overrides import get_agent_sku_markup_override
        ov = get_agent_sku_markup_override(agent_user_id)
        if ov is not None and float(ov) > 0:
            return round(float(ov), 2)
    except Exception as exc:
        logger.warning("_sku_markup_of_agent admin override 读取失败 owner=%s: %s", agent_user_id, exc)
    try:
        from db.auth_db import get_user
        u = get_user(int(agent_user_id))
        if u and u.get("agent_sku_markup_ratio") is not None:
            r = float(u["agent_sku_markup_ratio"])
            if r > 0:
                return round(r, 2)
    except Exception as exc:
        logger.warning("_sku_markup_of_agent 读取 agent_sku_markup_ratio 失败 owner=%s: %s", agent_user_id, exc)
    return 1.0


def get_cumulative_media_procurement_multiplier(current_user_id: Optional[int],
                                                max_depth: Optional[int] = None) -> float:
    """[correction 3 · 2026-06-13 老板审核口径] 累计可见媒体成本倍率(多级邀请链已加价媒体成本逐层传递)。

    与 get_procurement_cost_multiplier_for_quote_viewer(只返【直属上级】一层)的区别:
      沿 current → owner → owner's owner ... 逐层累乘每级【上级卖给下级】的 SKU 加价,
      直到无上级 / 环 / 超 depth limit。**1 级链时退化 == 直属上级倍率(与旧行为等价 · 安全)。**

    语义假设(铁律口径):每级下级看到的媒体成本 = 其上级看到的媒体成本 × 该上级给它的进货系数;
      若某级实为直充平台(不向上进货),其 agent_sku_markup 未设=1.0,自然不累加(无副作用)。
    仅作用于"系统自动估"的【媒体成本层】· 不碰报价中心(本人)· 不作用于自设 cost_override(防双算)。
    服务商本人(agent_level>=1)且无上级 → 1.0(直充平台)。带 cycle guard(visited)+ depth limit。
    """
    if not current_user_id:
        return 1.0
    if max_depth is None:
        try:
            from config.pricing_config import get_cost_procurement_max_depth
            max_depth = get_cost_procurement_max_depth()
        except Exception:
            max_depth = 8
    mult = 1.0
    visited = {int(current_user_id)}
    node = current_user_id
    depth = 0
    try:
        while depth < max_depth:
            owner = resolve_owning_agent(node)
            if not owner or int(owner) in visited:
                break
            visited.add(int(owner))
            mult *= _sku_markup_of_agent(owner)   # owner 卖给 node 的加价
            node = owner
            depth += 1
    except Exception as exc:
        logger.warning("cumulative_media_procurement 解析失败 user=%s(回落已累计 %.4f): %s",
                       current_user_id, mult, exc)
    return round(mult, 4)


def is_default_quote_pricing(current_user_id: Optional[int]) -> bool:
    """[报价中心·价格锁 2026-06-10 audit P1] 本人报价口径是否=平台默认(系数==默认 1.0 且未自设单篇成本)。
    仅默认口径 True 才允许把新词底盘写入【全局共享】7 天价格锁缓存(keyword_price_cache):
      默认口径写入的底盘是平台默认值,任何 per-agent 命中后从底盘末位重算客户价,零污染;
      per-agent 自设系数/成本者返 False(其成品价快照/自设成本会污染共享底盘),命中他人默认底盘时仍按本人系数重算。
    解耦「算价用实际值(防回退 settings)」与「写缓存资格(仅默认口径)」—— 修 P1-1 把 markup_override 恒传实际值后
    引擎 `markup_override is None` 写判据恒 False、共享 7 天价格锁全面停写的回归。
    [v2.1 2026-06-11] 进货倍率 ≠1(扫码下级)同属非默认口径:其底盘 cost 含上级 SKU 系数 · 写入会污染共享缓存。"""
    return (get_quote_markup_override_for_quote_viewer(current_user_id) is None
            and get_cost_per_article_for_quote_viewer(current_user_id) is None
            and abs(get_procurement_cost_multiplier_for_quote_viewer(current_user_id) - 1.0) < 0.001)


def _resolve_fallback_unit_cost(cost_override, cached_cost=None, default: float = 60.0) -> float:
    """[报价兜底成本地板 2026-06-08] 被过滤词兜底时单篇成本解析(纯函数·便于值级测试):
      本人自设(M2·cost_override is not None·最高优先)> 缓存 per-keyword 真实成本(>0·如央媒 350)> 默认 60。
    防硬编码 60 把高权威词(真实成本远高)严重低估;价格仍由调用方 × 本人系数(永不继承上级)。
    放此模块(而非 selection_api)因 selection_api 模块级触发 DB 连接不可在 mock 环境导入。
    """
    if cost_override is not None:
        try:
            return float(cost_override)
        except (TypeError, ValueError):
            pass
    if cached_cost:
        try:
            cc = float(cached_cost)
            if cc > 0:
                return cc
        except (TypeError, ValueError):
            pass
    return float(default)
