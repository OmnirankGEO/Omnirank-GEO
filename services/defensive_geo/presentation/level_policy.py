"""``defensive_presentation_level_policy_v1`` —— Z-6 签发的对客等级口径。

Owner 2026-08-24 签发(Z-6):按**出现率**分三档。

🔴 阈值是**动态系数**,不是写死的常量
------------------------------------
本仓铁律:「比例/倍率(佣金率、返利率、markup、折扣系数等)都是系统系数,
后台动态可调 —— 文档不背具体数字,以生产配置为准」。等级阈值属于同一类:
它是商业口径的一个旋钮,Owner 随时可能调。

所以这里的形态是:

* :data:`_DEFAULT_BANDS` 只是**出厂默认**,不是"规格里的数字";
* :func:`bands` 每次现取运行配置(``system_settings`` 里那一行),
  取不到才回落默认 —— 于是后台调完立刻生效,不用发版;
* 判据**不钉具体数字**,钉的是**结构性质**(单调、无缝、无重叠、全覆盖)
  与"改配置能改结果"这件事本身。钉数字的判据会在 Owner 调旋钮的那天
  变成一条假红,然后逼人去改判据 —— 那正是"文档背数字"的坏处。

🔴 为什么等级必须有 policy 签发才给
----------------------------------
MET-36 逐字「未签 policy 不得 enrollment 对客 v2 terminal Presentation」。
对客等级一旦发出去就成了承诺:说"优"就是说"你在 AI 里被推荐得不错"。
执行方自己定一套阈值 = 用一个没人签过的标准给客户发承诺。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, NamedTuple, Sequence

_LOGGER = logging.getLogger("GEO-DefGeo-LevelPolicy")

POLICY_ID = "defensive_presentation_level_policy_v1"

#: Owner 签发元数据。ACT-12 要求 H0 门能解析它。
SIGNED_BY = "owner"
SIGNED_AT = "2026-08-24"
SIGNED_REF = "Z-6"

#: 运行配置键。后台改这一行即生效(无需发版)。
SETTINGS_KEY = "defgeo_presentation_level_bands"


class Band(NamedTuple):
    """一档。``min_rate`` 是**下界(含)**,上界由下一档的下界给出。"""

    level_key: str
    min_rate: float


#: 出厂默认三档(Z-6)。**这不是"规格数字"**,是可调系数的初值。
#: 顺序:从高到低。判据校验单调性,不校验这几个数。
_DEFAULT_BANDS: tuple[Band, ...] = (
    Band("guarded", 75.0),
    Band("needs_strengthening", 50.0),
    Band("priority_fix", 0.0),
)


class LevelPolicyError(ValueError):
    """阈值表形态不合法。**不发等级**,而不是发一个用坏阈值算出来的档。"""


def _validate(bands: Sequence[Band]) -> tuple[Band, ...]:
    """结构性校验 —— 这些性质与具体数字无关,所以调旋钮不会让它们失效。

    · 严格单调递减(否则"更高的出现率"可能落到更低的档);
    · 最低一档必须从 0 起(否则某段出现率**无档可落**,而
      "算不出等级"与"等级是待提升"是两件事);
    · 档位 key 必须都在 registry 的等级表里,且不含 ``unknown``
      (``unknown`` 是"没测到",不是一个可以由阈值算出来的档)。
    """
    if not bands:
        raise LevelPolicyError("阈值表为空 —— 不发等级")
    mins = [b.min_rate for b in bands]
    if mins != sorted(mins, reverse=True) or len(set(mins)) != len(mins):
        raise LevelPolicyError(
            f"阈值下界不是严格单调递减:{mins} —— "
            "非单调会让更高的出现率落进更低的档")
    if float(bands[-1].min_rate) != 0.0:
        raise LevelPolicyError(
            f"最低一档从 {bands[-1].min_rate} 起,不是 0 —— "
            "0 到它之间那段出现率无档可落,而「算不出」与「待提升」是两件事")
    for b in bands:
        if not (0.0 <= float(b.min_rate) <= 100.0):
            raise LevelPolicyError(f"下界 {b.min_rate} 不在 [0,100]")
        if b.level_key == "unknown":
            raise LevelPolicyError(
                "unknown 不许出现在阈值表里 —— 它是「没测到」,"
                "不是一个能由阈值算出来的档")
    return tuple(bands)


def _from_settings(cur) -> tuple[Band, ...] | None:
    """用**调用方的** cursor 现取运行配置(``system_settings`` key/value 表)。

    形态:``value`` 是 JSON 数组 ``[{"levelKey": ..., "minRate": ...}, ...]``,
    ``value_type='json'``。与现役 ``pricing_config`` 同一套路
    (``SELECT value FROM system_settings WHERE key=...``),不另造读法。

    🔴🔴 **绝不自己开连接**。第二版写的是 ``from db.connection import
       get_connection`` 然后自己 ``get_connection()`` —— 窗D 那条既有锁
       ``test_bridge_import_closure_touches_no_db_module`` 当场判红,而它是对的:

       · import ``db.connection`` 会触发 ``init_db``,而 ``init_db`` 要抢
         ACCESS EXCLUSIVE —— 2026-08-10 那次**把生产打成 503 十六分钟**
         的自死锁就是这么来的;
       · 更直接的:调用方(``produce_cards``)此刻**正在另一个连接的事务里**,
         在事务中间再开一个连接去读配置,是标准的连接放大 + 死锁面。

       所以配置读取走调用方已经持有的那个 cursor,并包在 SAVEPOINT 里
       (表缺失/权限问题不许打废调用方事务)。

    🔴 **也不**从一个不存在的模块 import。第一版写的是
       ``from db.system_settings_db import get_setting`` —— 那个模块全仓
       不存在,于是 import 永远失败、动态路径**恒空转**:
       后台怎么调都不生效,而没有任何东西会报错。
       本仓管这个叫「接了线但接线是坏的」。判据
       ``test_the_dynamic_band_path_actually_works`` 往真库插一行、
       断言 ``bands(cur)`` 真的变了 —— 那条判据是这段代码存在的唯一证明。

    🔴 取不到 / 形态不合法一律回落默认,**并记 warning**:
       静默回落会让"后台已经调过了"和"配置写错了"长得一样。
    """
    import json

    if cur is None:
        return None

    # 与账本桥共用同一个 SAVEPOINT 出口 —— 同一个谓词不写两处。
    from services.defensive_geo.monitoring.run_ledger_bridge import guarded

    def _read():
        cur.execute(
            "SELECT value FROM system_settings WHERE key=%s", (SETTINGS_KEY,))
        return cur.fetchone()

    row = guarded(cur, "level bands", _read)
    if not row:
        return None
    raw = row["value"] if isinstance(row, Mapping) else row[0]
    if not raw:
        return None
    try:
        rows = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        return _validate([Band(str(r["levelKey"]), float(r["minRate"]))
                          for r in rows])
    except Exception as exc:
        _LOGGER.warning(
            "等级阈值配置形态不合法(%s),回落出厂默认 —— 请修配置", exc)
        return None


def bands(cur=None) -> tuple[Band, ...]:
    """当前生效的阈值表。**每次现取**,不缓存。

    ``cur`` 省略时只返回出厂默认 —— 纯函数路径(判据、离线计算)不需要库。
    需要动态系数生效的调用方(``card_producer.derive_level``)必须把自己
    手里那个 cursor 传进来。

    不缓存是有意的:缓存会让"后台调完立刻生效"变成"重启后生效",
    而那正是把动态系数退化成硬编码。
    """
    return _from_settings(cur) or _validate(_DEFAULT_BANDS)


def level_for_rate(rate: float | None, cur=None) -> str:
    """出现率 → 等级。``None`` ⇒ ``unknown``(没测到,不是"差")。

    🔴 ``None`` 与 0 是不同事实(§15.6 逐字)。没有有效样本时返回
       ``unknown``;真的算出 0% 才落最低档。把 ``None`` 当 0 处理会让
       "这一项没测到"在客户眼里变成"你一次都没被提到"。
    """
    if rate is None:
        return "unknown"
    r = float(rate)
    if not (0.0 <= r <= 100.0):
        raise LevelPolicyError(f"出现率 {r} 不在 [0,100] —— 不发等级")
    for band in bands(cur):
        if r >= float(band.min_rate):
            return band.level_key
    # _validate 保证最低一档从 0 起 ⇒ 不可达。留着是为了让"有人绕过校验
    # 直接改 _DEFAULT_BANDS"时炸在这里,而不是静默返回一个错的档。
    raise LevelPolicyError(       # pragma: no cover - 结构性保证
        f"出现率 {r} 无档可落 —— 阈值表校验被绕过了")


def census(cur=None) -> dict[str, Any]:
    """POR-20 用的机械分母 + 签发留痕。

    🔴 **不**把阈值数字放进 census 的"契约"部分:census 是给判据当分母用的,
       而阈值是可调系数。数字放在 ``activeBands`` 里作**现状快照**,
       判据不拿它做相等断言。
    """
    active = bands(cur)
    return {
        "policyId": POLICY_ID,
        "signedBy": SIGNED_BY,
        "signedAt": SIGNED_AT,
        "signedRef": SIGNED_REF,
        "settingsKey": SETTINGS_KEY,
        "bandCount": len(active),
        "levelKeys": [b.level_key for b in active],
        "isDefault": active == _validate(_DEFAULT_BANDS),
        # 现状快照(会随后台调整变化)—— 判据只用它验结构,不验数值
        "activeBands": [{"levelKey": b.level_key, "minRate": b.min_rate}
                        for b in active],
    }
