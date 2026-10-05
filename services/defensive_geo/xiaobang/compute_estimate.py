"""小榜「先示预估、确认后开始」—— Owner 2026-08-24 拍板的两段式落地。

Owner 原话:**小榜可真执行、必收费,但执行前必须示算力预估、用户确认后才开始。**

落法与诊断链同构,**不另设第二套执行链**(§0.5.1 第 2 条):

  ① **估价段** —— 免费、零资金副作用、不需要确认回执。
     现役 ``prepare`` 就是这一段:它调 ``_resolve_compute_quote`` /
     ``_channel_compute_quote`` 拿价,那两个函数读的是
     ``db.wallet_db.get_feature_pricing`` —— 与 ``middleware/billing.py`` 扣费时
     **同一个** resolver。本模块因此**自己一个字不算价**:只读那一段的结果,
     翻成用户看得懂的一句话。(结构锁:见 ``tests`` 里的"本模块不 import 定价"锚。)

  ② **确认段** —— 用户在页面上真点一次 ⇒ 签发回执 ⇒ 回执即幂等键 ⇒
     external 档执行。这一段现役已经完整,本模块只往它前面加一道
     「没有预估就不许确认」。

🔴 本模块解的那个洞(亲手实测,不是推测)
========================================
基线 ``fa8aecc50`` 上,一个**报不出价**的 external 意图可以一路走到确认:

* ``services/xiaobang_intent.quote_expired`` 在 ``compute_quote_expires_at IS NULL``
  时恒返 ``False`` —— 没报过价 ⇒ 永远"没过期";
* ``services/xiaobang_intent.drift_reason`` 里那段
  ``existing_quote = row.get("compute_quote_hash"); if existing_quote and ...``
  在没报过价时**整段跳过** —— 没报过价 ⇒ 永远"没漂移";
* ``api/xiaobang_operations_api.operation_confirm`` 全程**没有任何一行**
  要求意图上存在报价。

于是 ``issue_confirmation_receipt`` 照签,``bound_compute_quote_hash`` 写 NULL。
用户在**一个数字都没看见**的情况下按下了确认 —— 而确认在本系统里就是授权。

现役确实有一道 ``COMPUTE_QUOTE_MISSING``,但它长在
``services/xiaobang_publish_execute.execute_publish_image_note`` **里面** ——
单个领域 adapter 的内部,位于确认**之后**。也就是说:

* 用户已经付出了确认这个动作(回执已签、``user_action_challenge`` 已烧),
  才被告知"还没算出要用多少算力";
* 第二个 execute adapter 一旦接进来,它**不会**继承这道门 ——
  这正是本仓记过的「校验器留在下游拦不住上游」形态。

本模块把这条谓词搬到**共用咽喉**(confirm),并让下游那道门**调用同一个函数**,
于是全仓只有一处实现:改坏它,confirm 与 execute 两侧判据一起红。

🔴 这道门只补现役没有的那一格
----------------------------
过期由 ``quote_expired`` 判、渠道资格由领域 adapter 判 —— 都不在这里重判。
两把锁叠在同一条路径上,其中一把的变异会被另一把吞掉(本仓记过的形态),
于是「相关判据全绿」证明不了任何一行被验过。

confirm 侧因此**只**硬拦 ``unavailable``(价格系统本身取不到,没有下游能说得更准);
execute 侧则一格都不让 —— 那是真花钱的一跳,见 ``frozen_estimate_amount`` 的调用方。
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

from services.defensive_geo.copy_registry import assert_public_copy_clean

ESTIMATE_VERSION = "defgeo-xiaobang-compute-estimate-v1"

# ── 报价态 ────────────────────────────────────────────────────────────────
# 🔴 **逐值复用现役 ``quote_state`` 的五个取值**,一个新枚举都不造。
#    理由是硬的:``quote_state`` 已经登记在
#    ``services.xiaobang_facade_dlp.FIVE_PHASE_MACHINE_KEYS`` 里(当年正因为
#    裸枚举上屏把 ``POST /prepare`` 打成 500 才补的)。造一个新名字 = 那条
#    豁免登记对不上 = 新的 500,而且是只在"报不出价"那几档才触发的 500。
STATE_QUOTED = "quoted"
STATE_PENDING_DOMAIN_ADAPTER = "pending_domain_adapter"
STATE_UNAVAILABLE = "unavailable"
STATE_NOT_APPLICABLE = "not_applicable"
STATE_EXPIRED = "expired"

#: 全集。判据拿它当分母,不手抄。
ESTIMATE_STATES: tuple[str, ...] = (
    STATE_QUOTED, STATE_PENDING_DOMAIN_ADAPTER, STATE_UNAVAILABLE,
    STATE_NOT_APPLICABLE, STATE_EXPIRED,
)

#: 能进入「确认」的报价态**只有一个**。写成集合而不是 ``== "quoted"``,
#: 是为了让"以后又多一档能确认的态"这件事必须显式改这一行,而不是
#: 在别处加个 or 就悄悄放宽。这是**界面**口径:主按钮亮不亮。
CONFIRMABLE_STATES: frozenset[str] = frozenset({STATE_QUOTED})

#: prepare 冻结的这一位回答:**这次估不出价,领域是不是已经知道原因了?**
#:
#: 🔴 为什么不能用报价档(``quote_state``)来判(实跑三条判据红出来的订正):
#:    实测该档在"渠道不合格"与"目录价取不到"两种完全不同的情形下**都可能是**
#:    ``unavailable`` —— 因为 ``_resolve_compute_quote`` 先查目录价,查不到就直接
#:    落 ``unavailable``,根本走不到 ``pending_domain_adapter`` 那一支。
#:    拿它当判别依据,就会把「这个账号发不了图文」讲成「算不出要用多少算力」。
#:    本仓对这个形态已有签发过的裁定:
#:
#:        「必须是渠道资格那条码,不是"还没算出算力"。
#:          后者是答非所问:他选的号发不了图文,该听见的是这件事。」
#:        (tests/xiaobang_execute_2026_08_20/test_five_phase_execute_pg16.py · 资格翻转矩阵)
#:
#: 所以判别依据改成 prepare 冻结的这一位:领域已经知道原因(账号发不了图文 /
#: 今天额度用完 / 还没选账号)⇒ **这里不拦**,让她走到领域侧听那句准话;
#: 领域也说不出原因(价格系统自己取不到)⇒ 没有任何下游能解释得更好,
#: 在她按下确认**之前**就拦住。
#:
#: 钱的安全不靠这道门:execute 侧那道同源门(``frozen_estimate_amount`` 为空 ⇒
#: 零冻结)才是资金保证。这道门保证的是**她不会在一无所知的情况下按下确认**。
DOMAIN_BLOCKER_KEY = "estimate_domain_blocked"


class EstimateNotShown(ValueError):
    """还没算出这次要用多少算力。**不签回执** —— 没有数字的确认不是确认。"""

    def __init__(self, message: str, *, state: str) -> None:
        super().__init__(message)
        self.state = state
        self.public_message = message


def raw_estimate_amount(intent_row: Mapping[str, Any]) -> Optional[int]:
    """库里那一列**原样**的值(含 ``0``)。只给审计/判据用,**不做门控依据**。

    存在的理由是可判性:判据要能证明"这一行上确实冻了个 0,而我们拒了它",
    而不是只能证明"读出来是 None"—— 后者与"这一列本来就是 NULL"分不开。
    """
    amount = intent_row.get("compute_quote_amount")
    if amount is None:
        return None
    try:
        return int(amount)
    except (TypeError, ValueError):
        return None


def frozen_estimate_amount(intent_row: Mapping[str, Any]) -> Optional[int]:
    """取**冻结在意图上**的那个预估数字;估不出有效价时返回 ``None``。

    🔴 全仓**唯一**的「这次估了多少」取数口。confirm 的门与
       ``execute_publish_image_note`` 的门都调它 —— 同一条谓词只有一处实现。
       (本仓铁律:同一谓词写两处 ⇒ 必有一处没人验。)

    🔴 只读 ``compute_quote_amount`` 这一列,**不回落**去读 preview 里的
       ``execution_binding.price_points``。两个数字在正常情况下相等,
       但相等是巧合不是约束:回落会让"意图上没有报价"这件事被一个
       **另一条路写进来**的数字掩盖过去,而冻结、扣费、判据读的都是前者。

    ═══════════════════════════════════════════════════════════════════
    🔴 [工单 C-6 · Owner 2026-08-25 拍板] ``0`` 与 ``None`` **不再同义**,
       但两者都不是"可以确认的价"
    ═══════════════════════════════════════════════════════════════════
    改动前这里只挡 ``None``,``0`` 原样返回。后果是一条**稳定 500**:

      ``estimate_state`` 见"有数字" ⇒ ``quoted`` ⇒ ``confirmable=True``,
      屏幕上写「本次预计消耗你的算力 **0**,确认后开始」;
      她确认 ⇒ 回执签发 ⇒ execute ⇒ ``materialize_publish_batch`` ⇒
      ``freeze_one_item(expected_points=0)`` ⇒ ``middleware/billing.py:1364``
      逐字 ``if total_cost == 0: return {"freeze_id": None, ...}`` ⇒
      ``contract_freeze`` 那道「不接受无句柄的成功」守卫抛
      ``FreezeProducedNoHandle`` ⇒ 对外 500 ``SETTLEMENT_HANDLE_INVALID``。

    🔴 为什么 ``0`` 的语义是"估不出价"而不是"这家免费"(Owner 2026-08-25 亲裁,
       我按代码复核过):``services/media_price_projection.resolve_price_points``
       的三档取价(``our_price_points`` → ``our_price_yuan`` →
       ``price × markup × 130``)**每一档都只在 > 0 时返回**,兜底
       ``yuan_to_points`` 在 ``v <= 0`` 时返 0。也就是说现役目录里
       **没有"免费媒体"这个商业档**;能落到 0 的只有三种情形:
       这一行没有进货价、markup 被配成 0、价格字段脏。
       那三种都该说"这次估不出要用多少算力",不该说"免费,确认后开始"。

    所以门控口径统一为「**正整数才算估出了价**」。走到这里返回 ``None``
    的两条路(没报过价 / 报出 0)在**下一步动作**上完全一致 ——
    都是"回去重新准备",所以合并成一档是对的;需要区分时读
    :func:`raw_estimate_amount`。

    🔴 这不是给 0 价新开一条免冻路径。Owner 选项 A 逐字:0 价不可确认。
       真要支持免费媒体,是去把那一行的目录价配成非 0,而不是在资金链上
       加一条"零句柄也算成功"的分支 —— 那与 P0-6「不接受零记账的成功」
       正面冲突。
    """
    amount = raw_estimate_amount(intent_row)
    if amount is None or amount <= 0:
        return None
    return amount


def estimate_state(intent_row: Mapping[str, Any], *, expired: bool = False) -> str:
    """这次估价处在哪一档。

    ``expired`` 由调用方用**与 confirm 同一只钟**算好传进来
    (``services.xiaobang_intent.quote_expired``)—— 本模块不自己取时间,
    否则这条谓词的可判性会依赖跑的时刻。

    优先用 prepare 时**冻结**下来的那个 ``quote_state``(意图不可变 ⇒ 刷新一次
    不能显示成另一档);冻结里没有时,只按"有没有数字"给一个保守答案 ——
    **不猜**是"账号还没选"还是"价格取不到",那两句话的下一步动作不一样,
    猜错等于把用户支到错的地方去。
    """
    if frozen_estimate_amount(intent_row) is not None:
        # 🔴 [对抗复审自查] 有数字但**已过期** ⇒ 不能再说「确认后开始」。
        #    第一版漏了这一档,后果是 GET 恢复时同一个响应里
        #    quote_state="expired" 与 headline「确认后开始」直接打架,
        #    而 registry 里那两条 expired 文案永远取不到(死文案)。
        return STATE_EXPIRED if expired else STATE_QUOTED
    preview = dict(intent_row.get("preview") or {})
    frozen = str(preview.get("quote_state") or "")
    if frozen in ESTIMATE_STATES and frozen != STATE_QUOTED:
        # 冻结说"报不出价",而现在也确实没数字 —— 采信冻结那一档的具体原因。
        return frozen
    return STATE_UNAVAILABLE


def estimate_headline(state: str, amount: Optional[int]) -> str:
    """上屏那一句。**文案取自 copy registry**,本函数只负责把数字填进去。

    🔴 单位在文案里写死「算力」而不是模板变量:Owner 已定全站统一「算力」,
       禁「积分 / 额度」。做成变量等于给"哪天换个叫法"留一个不会有人守的口子。
    """
    from services.defensive_geo.copy_registry import user_label

    template = user_label("compute_estimate", state)
    text = template.replace("{amount}", str(amount)) if amount is not None else template
    return assert_public_copy_clean(text, field=f"compute_estimate.{state}")


def estimate_next_action(state: str) -> str:
    """报不出价那几档的「下一步点哪」。

    ``quoted`` 没有下一步文案 —— 那一档的下一步就是页面上那个确认按钮,
    再给一句会变成两个按钮打架(§0.5.5 U-3「每屏恰一个主行动」)。
    """
    from services.defensive_geo.copy_registry import user_label

    return assert_public_copy_clean(
        user_label("compute_estimate_next_action", state),
        field=f"compute_estimate.next_action.{state}")


def build_public_estimate(intent_row: Mapping[str, Any], *,
                          expired: bool = False) -> dict[str, Any]:
    """估价段的**用户面**投影。

    只出「数字 + 单位 + 一句人话 + 下一步人话」。档位枚举本身走同级的
    ``quote_state``(已登记机读键),**不在这里再露一次** ——
    露了就要再开一条 DLP 豁免,而豁免面每宽一格都要有人守。
    """
    state = estimate_state(intent_row, expired=expired)
    amount = frozen_estimate_amount(intent_row)
    out: dict[str, Any] = {
        "headline": estimate_headline(state, amount),
        "confirmable": state in CONFIRMABLE_STATES,
    }
    if amount is not None and state in CONFIRMABLE_STATES:
        # 过期档**刻意不出数字**:出了她会以为按下去就是这个价,
        # 而重新核对之后价可能已经变了。
        out["amount"] = amount
        out["unit"] = str(intent_row.get("compute_quote_unit") or "算力")
    if state not in CONFIRMABLE_STATES:
        out["next_action"] = estimate_next_action(state)
    return out


def assert_estimate_shown(intent_row: Mapping[str, Any]) -> Optional[int]:
    """🔴 **Owner 2026-08-24 铁律在 confirm 那一侧的形式**。

    返回冻结的预估数字;数字不在、且这一档**没有下游能说得更准**时抛
    :class:`EstimateNotShown`。返回 ``None`` = 「这里不拦,让领域去说」
    (见 :data:`DOMAIN_BLOCKER_KEY` 上那段裁定)。

    调用方**必须**把异常翻成自己那一层的 typed error —— 本模块不认识
    ``IntentError``,也不该认识:它是纯谓词,不绑任何一层的错误体系。
    """
    amount = frozen_estimate_amount(intent_row)
    if amount is not None:
        return amount
    preview = dict(intent_row.get("preview") or {})
    if bool(preview.get(DOMAIN_BLOCKER_KEY)):
        return None
    state = estimate_state(intent_row)
    from services.defensive_geo.copy_registry import user_label

    raise EstimateNotShown(
        assert_public_copy_clean(user_label("compute_estimate", state),
                                 field=f"compute_estimate.{state}"),
        state=state,
    )


def census() -> dict[str, Any]:
    return {
        "estimateVersion": ESTIMATE_VERSION,
        "states": list(ESTIMATE_STATES),
        "confirmableStates": sorted(CONFIRMABLE_STATES),
        "domainBlockerKey": DOMAIN_BLOCKER_KEY,
        "singleAmountReader": "services.defensive_geo.xiaobang.compute_estimate.frozen_estimate_amount",
        # [工单 C-6 · Owner 2026-08-25 选项 A] 可机读断言:0 不是可确认的价。
        "zeroAmountIsConfirmable": False,
        "rawAmountReader": "services.defensive_geo.xiaobang.compute_estimate.raw_estimate_amount",
    }


__all__ = [
    "CONFIRMABLE_STATES",
    "raw_estimate_amount",
    "ESTIMATE_STATES",
    "ESTIMATE_VERSION",
    "EstimateNotShown",
    "DOMAIN_BLOCKER_KEY",
    "assert_estimate_shown",
    "build_public_estimate",
    "census",
    "estimate_headline",
    "estimate_next_action",
    "estimate_state",
    "frozen_estimate_amount",
]
