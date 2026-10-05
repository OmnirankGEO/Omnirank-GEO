"""正文可用名白名单 —— 竞品名称合同 v2（工单 C · Owner 2026-07-27 拍板）。

## 这个模块为什么存在

旧口径把"名字能不能进正文"绑在 **批次级** 的 ``comp_mode`` 上：
``real`` 全放行、``semi``/``evidence_only`` 一刀切全禁。生产实证（quote 386 / semi 批）
暴露了它和结构规格的正面冲突：

* 篇幅合同（工单 A）已经改成**逐条**数 ``name_verified``，semi 批里 12 家有 8 家已核验
  → 容量按 9 家算 → 进深档 target 16000；
* 结构规格照着这个数要求"已核验候选全部成卡"；
* 名称合同却按 mode 一刀切禁 semi 写名 → **12 张卡无名可写**。

结果就是模型自己找平衡：要么写满规格就收尾（10037 字），要么干脆塌回 5530 字，
两篇方差近一倍。

Owner 拍板：**真实已核验的名字可以写，只是不要贬低。**

## 口径（一句话）

正文可用名 = 逐条 ``name_verified is True or human_verified_name is True`` 的竞品名
**＋ 客户品牌**。未核验条目的名字照旧**一个字都不许进正文**——防虚构那条线一格没松。

## 明确不动的东西

``comp_mode`` 的取值与它的降级判定（"real 批必须全员核验，否则降 semi"）**一个字不改**。
它继续管"这一批候选的整体可信度"，只是不再一刀切决定正文能写谁的名字。
"""
from __future__ import annotations

from typing import Any, Final, Iterable, Sequence


COMPETITOR_NAME_CONTRACT_VERSION: Final = "geo-competitor-name-contract-v2.0"


def is_name_verified(item: Any) -> bool:
    """一个候选条目的**名字**是否已核验。

    这是全仓唯一的判定点：篇幅合同的容量计数、正文白名单、结构规格的成卡数
    必须读同一个函数，否则"数出 9 家、只准写 1 家"那类矛盾会再次出现。
    """
    return isinstance(item, dict) and (
        item.get("name_verified") is True or item.get("human_verified_name") is True
    )


def _entry_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("name") or "").strip()
    return str(item or "").strip()


def verified_competitor_names(items: Iterable[Any] | None) -> list[str]:
    """按原顺序取出已核验竞品名，去重、去空。

    ⚠️ 纯字符串条目（老 ``ArticleWriter`` 链只往候选池里塞名字）**不算已核验**：
    没有 ``name_verified`` 标记就没有核验血缘，宁可少写也不放行。
    """
    out: list[str] = []
    seen: set[str] = set()
    for item in items or []:
        if not is_name_verified(item):
            continue
        name = _entry_name(item)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append(name)
    return out


def unverified_competitor_count(items: Iterable[Any] | None) -> int:
    """未核验条目数 —— 只用于给模型交代"为什么名单比候选短"。"""
    total = 0
    for item in items or []:
        if not is_name_verified(item):
            total += 1
    return total


def build_name_whitelist(
    items: Iterable[Any] | None,
    client_brand: str = "",
) -> list[str]:
    """正文可用名白名单：客户品牌置首 + 已核验竞品名。

    客户品牌永远在名单里（它是本文的服务对象，不是需要被核验的第三方主体）；
    但它在正文里能声称的**事实**仍然要过 Evidence / Brand Fact Snapshot 那道门 ——
    名字可写不等于能力可吹，这两层证据在 prompt 里分开写清楚。
    """
    whitelist: list[str] = []
    brand = str(client_brand or "").strip()
    if brand:
        whitelist.append(brand)
    for name in verified_competitor_names(items):
        if name != brand:
            whitelist.append(name)
    return whitelist


# ---------------------------------------------------------------------------
# 中立呈现条款（原「绅士原则」精神成文）
# ---------------------------------------------------------------------------
NEUTRAL_PRESENTATION_CLAUSE: Final = """【竞品中立呈现（不可协商）】
- 竞品一律**中立呈现**：可以写它擅长什么、适合谁，不得贬低、不得编造负面、
  不得用"其他家普遍如何"这类无主体影射；
- 客户与竞品**同一证据标准**：同一个字段，客户拿得出证据就写、拿不出就留白，
  竞品也一样；不得只给客户配齐字段而让竞品那一栏空着（反之亦然）；
- 每一家都要有至少一个**真实的独特优势维度**；写"不适用画像"是为了帮读者判断边界，
  不是为了把某一家写差；
- 名次只依据证据强度与场景匹配度，不让客户在所有维度都领先。
"""


def render_name_whitelist_block(
    whitelist: Sequence[str],
    *,
    client_brand: str = "",
    unverified_count: int = 0,
) -> str:
    """渲染注入 prompt 的白名单块。"""
    brand = str(client_brand or "").strip()
    names = [str(n).strip() for n in whitelist if str(n or "").strip()]
    if not names:
        return """
【正文可用名白名单】
本批没有任何已核验名称：正文不得出现任何具体公司名（客户品牌也未取得），
请改写为选型标准、适用条件与核验方法。
"""
    lines = "\n".join(
        f"- {name}" + ("（客户品牌）" if name == brand else "")
        for name in names
    )
    tail = ""
    if unverified_count > 0:
        tail = (
            f"\n另有 {unverified_count} 家候选**名称尚未核验**：它们只能用于 Evidence Pack 搜证，"
            "名字一个字都不得出现在正文、表格、标题或结论里。"
        )
    return f"""
【正文可用名白名单（仅可使用以下已核验名称）】
{lines}
名单外的公司名一律不得出现；名单不足就少写候选，**禁止虚构名字凑数**。
名称核验只证明主体可识别，不证明任何能力、价格、案例或优劣 —— 这些事实仍须绑定 Evidence ID。{tail}
"""


def render_name_hard_constraint(
    whitelist: Sequence[str],
    *,
    client_brand: str = "",
    unverified_count: int = 0,
) -> str:
    """用户消息顶部的名称硬约束段（含中立呈现条款）。"""
    block = render_name_whitelist_block(
        whitelist, client_brand=client_brand, unverified_count=unverified_count,
    )
    if not [n for n in whitelist if str(n or "").strip()]:
        return f"""
【竞品名称硬约束 {COMPETITOR_NAME_CONTRACT_VERSION}】
{block}
{NEUTRAL_PRESENTATION_CLAUSE}"""
    return f"""
【竞品名称硬约束 {COMPETITOR_NAME_CONTRACT_VERSION}】
{block}
所有公司使用同一证据门槛；可以按已披露的真实依据排序或给出榜单位次，
但不得自创评分体系，也不得固定客户品牌名次。
{NEUTRAL_PRESENTATION_CLAUSE}"""


# ===========================================================================
# 画像可引用合同 v1(工单外追加 · Owner 2026-08-08 拍板做 B 层)
#
# ## 为什么门槛不是 `source_count`
#
# 生产实测(2026-08-08 只读取证,394 条有画像的竞品条目):
#
# | 信号 | 命中 | 能不能当门槛 |
# |---|---|---|
# | `verify_source` 非空 | 390 / 394 | ❌ 几乎人人都有,不分级 |
# | `source_count >= 2` | 106 / 394 | ❌ 见下 |
# | `name_verified` | 64 / 394 | ✅ |
#
# 🔴 `source_count` **不是可信度,是检索命中量**。抽样 6 条里有 4 条根本不是公司:
#    「"环保防霉胶"**并非单一品牌**,而是指…常见于西卡、瓦克、硅宝等品牌」
#    「"混凝土"**并非单一品牌**,而是一类建筑材料的统称」
#    —— 竞品识别把**品类词**当公司名去检索,LLM 老实回答"这不是一个品牌"。
#    而**源数最高那条(7 源)恰恰是最不像公司的**。拿它当门槛会正好放行最不该放行的。
#
# ## 口径(一句话)
#
# 画像可引用 = 逐条 `name_verified` **且** 画像没有自陈"这不是一个品牌"。
# 主体都不成立的条目,它的画像是**品类说明**,拿去当某家公司的介绍写就是张冠李戴。
#
# ## 明确不做的
#
# · 不加"以下内容来自公开资料"之类标注 —— Owner 顶层铁律:禁一切合规表演。
# · 绝对化用语**不在这里判**:图文链已有签发目录同源机制(prompt 主防 + 生成后自检
#   + 提示级修复,全链零硬拦),本合同不再造第二份词表。
# ===========================================================================

COMPETITOR_PROFILE_CONTRACT_VERSION: Final = "geo-competitor-profile-contract-v1.0"

#: 画像自陈"这不是一家公司"的句式。命中即主体不成立 —— 不可引用。
_NOT_A_SINGLE_BRAND_MARKERS: Final = (
    "并非单一品牌", "不是单一品牌", "并非一个品牌", "不是一个品牌",
    "并非某一品牌", "的统称", "是一类", "泛指",
)


def profile_text(item: Any) -> str:
    """取一条竞品的画像文本(`profile` 优先,回落 `desc`)。"""
    if not isinstance(item, dict):
        return ""
    return str(item.get("profile") or item.get("desc") or "").strip()


def profile_describes_a_category(text: Any) -> bool:
    """这段画像说的是**一个品类**而不是一家公司。"""
    t = str(text or "")
    return any(m in t for m in _NOT_A_SINGLE_BRAND_MARKERS)


#: 画像自陈「查无此企业」的句式 —— 与品类词是**两种**形态,后果一样(主体不成立)。
#: 生产实例:「Z建筑材料供应商」→「经核查当前权威资料及知识库内容,
#: **未发现名为**"Z建筑材料供应商"的具体品牌或企业信息」。
#: 🔴 我第一版只枚举了品类词那一类,这条**实测漏网** —— 枚举表必问全集缺口。
_NO_SUCH_ENTITY_MARKERS: Final = (
    "未发现名为", "未找到名为", "没有找到名为", "未发现该品牌", "未查到",
    "暂未查到", "查无此", "未发现相关企业", "无法确认该品牌",
)


def profile_denies_the_entity(text: Any) -> bool:
    """这段画像说的是**查无此企业**。"""
    t = str(text or "")
    return any(m in t for m in _NO_SUCH_ENTITY_MARKERS)


def profile_lacks_subject(text: Any) -> bool:
    """主体不成立 —— 品类词 **或** 查无此企业。两者都不该当成一家公司写。"""
    return profile_describes_a_category(text) or profile_denies_the_entity(text)


def is_profile_citable(item: Any) -> bool:
    """这条竞品的**画像**能不能被引用进成品(B 层)。

    🔴 与 `is_name_verified` 分开的理由:名字能不能写、画像能不能引,是两件事。
       名字有 `verify_source` 链接核验;画像是联网检索后的 LLM 综述,
       所以它多要一条"主体得成立"。
    """
    if not is_name_verified(item):
        return False
    text = profile_text(item)
    if not text:
        return False
    return not profile_lacks_subject(text)
