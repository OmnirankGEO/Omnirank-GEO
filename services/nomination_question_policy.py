"""提名型问题闸 —— 诊断 SOV 题的**额外**资格,比「商业意图」更严的子集。

[WO_DIAGNOSIS_MENTION_VARIANT_AND_QUESTION_FITNESS 2026-08-06 §2]

## 🔴 先说清楚这不是什么(防修错方向)

**商业意图闸没有坏。** 「贵州贵阳贵州劳务派遣服务商怎么选才不踩坑？」被生产同版
``commercial_query_policy.evaluate()`` 判 ``commercial · GATE:PROVIDER_OBJECT ·
eligible=True`` —— 07-23 SSOT 就是这么裁的(提问对象 = 服务商 → 买家意图成立),
**判得对**。报价/监测/扩词侧继续按它走,本模块**一个字都不改它**。

真问题是另一件事:**商业意图 ≠ 品牌拉出力**。

  「劳务派遣服务商怎么选才不踩坑」的 AI 答案是一张方法论清单(看证照、看资质、
  看社保户、签合同注意什么)—— 结构上**不可能点名任何厂商**。这道题放进诊断:
    · 永远测不出提及(4 个引擎 × 1 道题 = 4 格必然 0);
    · 稀释出现率分母 —— 客户的可见度被一道注定测不出的题拉低;
    · 花的是真钱(4 引擎 ¥0.29/次)买回一格噪声。

诊断 SOV 题需要的资格是**提名型**:这道题问出去,AI 会**点名具体厂商**。
提名型 ⊂ 商业意图 —— 所有提名型题都是商业题,反之不成立。

## 结构上如何保证 ⊂(不靠人记得)

``is_nomination_question`` 的**第一步**就是委托 ``evaluate()``:商业闸判不合格
直接返回 False。所以"提名型放行 → 商业闸也放行"是**代码结构保证**的,不是靠
两张清单人工对齐。这条由 ``test_nomination_is_subset_of_commercial`` 拿 SSOT
四个已裁例 + 模板池全量反向对照钉住。

## 判据 = 单一正向清单(fail-closed)

只有一张 ``_NOMINATION_SIGNALS`` 正向清单,**没有反向清单**。理由是反向清单会
在"混合问法"上和正向清单打架,而打架的裁决顺序没有客观依据:

  · 「{品类}一般怎么收费？哪家性价比高」—— 前半句是方法论,后半句**必然点名**。
    有反向清单时这道题的死活取决于"先查哪张表",纯属人定;单一正向清单下它
    命中「哪家」→ 放行,**符合事实**(AI 真的会列商家)。
  · 「{品类}怎么选才不踩坑」—— 一个正向信号都不含 → 判废。不需要任何反向规则。
  · 「{品类}口碑怎么判断」—— 清单里是「口碑好」不是裸「口碑」→ 判废(答案是
    方法论)。**信号的粒度就是判据本身**,这是清单里每条都写全的原因。
  · 「哪种方案适合我」—— 清单里是「哪家/哪几家/哪个+机构名词」,不含裸「哪种」
    → 判废(答案讲方案类型,不点名)。

🔴 判废 **不等于**删题。调用方(``_enforce_commercial_questions``)走**等槽替换**:
题数不变(SSOT §9.6 不静默缩减 · 扣费 N 题 = 实跑 N 题的对账不许破),
只把这一槽换成提名型的题。

## 挂载点只有一个

**只挂诊断出题**。报价侧 ``selection_api`` / 扩词 ``batch_pricing`` / 监测侧
一律不接 —— 报价卖的是"这个词值不值得买",一道方法论词照样可以有搜索量和
商业价值;诊断测的是"AI 提不提我们",测不出名字的题在这里就是废票。
两件事的资格标准本来就不同,别用一把尺子。
"""
from __future__ import annotations

import re

#: 一次判定的返回原因码前缀,便于日志/快照追溯。
POLICY_VERSION = "diagnosis-nomination-gate-v1"

REASON_NOT_COMMERCIAL = "NOT_COMMERCIAL"
REASON_BRAND_DIRECT = "BRAND_DIRECT"
REASON_NO_NOMINATION_SIGNAL = "NO_NOMINATION_SIGNAL"
REASON_EMPTY = "EMPTY_QUERY"

_PROVIDER_NOUN_GROUP = "品牌|厂家|公司|机构|平台|服务商|供应商|门店|商家|店"

#: 出现任一即认为「AI 的回答会点名具体厂商」。
#:
#: 每条都写到**能决定答案形态的那个粒度**:是「口碑好」不是「口碑」,
#: 是「哪个+机构名词」不是「哪个」—— 粒度写粗一格,方法论题就会漏进来。
_NOMINATION_SIGNALS: tuple[str, ...] = (
    # —— 直接点名类 ——
    r"哪家",
    r"哪几家",
    r"哪一家",
    r"哪个好",
    r"哪个更好",
    rf"哪个(?:{_PROVIDER_NOUN_GROUP})",
    rf"哪些(?:{_PROVIDER_NOUN_GROUP})",
    r"哪个更适合",
    r"哪家更适合",
    r"选哪家",
    r"选哪个",
    r"找谁",
    r"去哪找",
    r"去哪买",
    r"哪里买",
    r"哪里有",
    r"去哪里",
    # —— 列举/榜单类 ——
    r"推荐",
    r"排名",
    r"排行",
    r"前十",
    r"前五",
    r"十佳",
    r"榜单",
    r"top\s*\d+",
    r"前\d+",
    # 🔴 「有哪些」**不许裸用**:"怎么选？有哪些注意点" 会整条漏进来(实测)。
    #   必须锚在机构名词上 —— 与 SSOT 自己的 PROVIDER_LIST_QUERY 同形。
    rf"(?:{_PROVIDER_NOUN_GROUP}).{{0,4}}有哪些",
    rf"有哪些.{{0,10}}(?:{_PROVIDER_NOUN_GROUP})",
    r"名单",
    r"名录",
    r"清单",
    # —— 声誉/择优类(必须带能落到主体上的后缀) ——
    r"口碑好",
    r"口碑怎么样",
    r"评价高",
    r"人气高",
    r"最专业",
    r"更值得合作",
    r"值得合作",
    r"性价比高",
    r"服务响应快",
    r"做过类似案例",
    r"比较好的",
    r"比较有名",
    r"比较成熟",
    # —— 供应商尽调类(与 SSOT PROVIDER_TRUST_QUERY 同形) ——
    rf"(?:{_PROVIDER_NOUN_GROUP}).{{0,12}}(?:靠谱|正规|专业|可靠)吗",
    r"哪家靠谱",
    r"哪家正规",
    # —— 对比类(SSOT §3.1 明列合法商业方向) ——
    r"对比下来",
    r"哪个值得买",
)

_NOMINATION_RE = re.compile("|".join(_NOMINATION_SIGNALS), re.IGNORECASE)


def has_nomination_signal(text: str) -> bool:
    """纯文本层:题面里有没有「会让 AI 点名」的信号。

    单独暴露出来是为了让锁能分别钉住两层(文本信号 / ⊂ 商业),
    某一层被改坏时红的是那一层,不用猜。
    """
    cleaned = re.sub(r"\s+", "", str(text or ""))
    if not cleaned:
        return False
    return bool(_NOMINATION_RE.search(cleaned))


def is_nomination_question(
    text: str,
    *,
    brand_name: str | None = None,
) -> tuple[bool, str]:
    """这道题够不够格进诊断 SOV 测试;返回 ``(eligible, reason_code)``。

    🔴 第一步必须是商业闸委托 —— 这是 ⊂ 关系的**结构保证**,不许改成并列判断。
    """
    normalized = str(text or "").strip()
    if not normalized:
        return False, REASON_EMPTY

    try:
        from services.commercial_query_policy import INTENT_BRAND_DIRECT, evaluate
    except Exception:  # pragma: no cover - 独立脚本兜底:拿不到引擎不擅自判废
        return True, "POLICY_UNAVAILABLE"

    decision = evaluate(normalized, brand_name=brand_name)
    if not decision.commercial_delivery_eligible:
        return False, REASON_NOT_COMMERCIAL
    # 品牌直问(品牌认知层)按定义就点名本品牌,不需要再找提名信号。
    if decision.intent_type == INTENT_BRAND_DIRECT:
        return True, REASON_BRAND_DIRECT

    if has_nomination_signal(normalized):
        return True, "NOMINATION_SIGNAL"
    return False, REASON_NO_NOMINATION_SIGNAL


def nomination_eligible(text: str, *, brand_name: str | None = None) -> bool:
    """布尔入口(与 ``commercial_query_policy.buyer_intent_eligible`` 同形)。"""
    ok, _ = is_nomination_question(text, brand_name=brand_name)
    return ok
