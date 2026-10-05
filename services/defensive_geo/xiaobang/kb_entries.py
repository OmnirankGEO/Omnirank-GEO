"""WP8 新增 KB 条目 —— **零社媒**,并把两条 Owner 铁律写成可执行的锁。

🔴 先说清楚"零社媒"该怎么锁,否则会锁错人
------------------------------------------
census 实测:``knowledge/system_kb/pages/feature-pricing.md`` 第 15/62 行
出现「社媒IP」——但那是在**如实描述价目页上的分类 Tab 名**
(全部 / GEO / 社媒IP / AI员工 / 媒体发布 / 免费)。页面上确实有那个 Tab。

如果把负向锁写成裸子串 ``"社媒" in text``,它会把这两行判红,
于是下一个人为了让锁变绿去**删掉一句真话** —— 那是合规表演,
而且让 KB 与真实界面对不上,用户按 KB 说的去找 Tab 会找不到。
本仓已经在 ``_segments``(``persona`` 撞 ``personal``)上栽过同一种跟头。

所以这里的锁是**结构性**的:判的是"这条 KB 条目**是不是**在教用户
使用社媒能力"(topic 路由前缀 / capability 命名空间 / 归属模块),
复用现役四轴锁 ``xiaobang_command_contract.social_domain_hits``,
**不判措辞**。如实描述别处存在的 Tab 名 ⇒ 放行且**登记在案**
(:data:`INCIDENTAL_SOCIAL_MENTIONS`),不是偷偷放过。

🔴 两条 Owner 铁律(本模块把它们从"文档里的话"变成"会红的锁")
---------------------------------------------------------------
* **ORG-SEAT-NO-SOCIAL**(Owner 2026-08-17):员工席位永久剔除社媒。
* **XIAOBANG-GEO-ONLY**(Owner 2026-08-18):小榜只负责 GEO 板块。

census 实测:这两条在**注册表侧**已有锁
(``assert_no_social_domain`` 打在 ``gap_operation_map`` 与
``services/organization_route_contract.MEMBER_GEO_ROUTE_POLICIES``),
但在 **KB / 索引链侧零判据** —— 也就是说"小榜的知识库里没有社媒条目"
这句话此前**没有任何东西在守**。本模块补的正是这一侧。
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, NamedTuple, Sequence

from services.xiaobang_command_contract import social_domain_hits

KB_ENTRIES_VERSION = "defgeo-xiaobang-kb-v1"

#: 两条 Owner 铁律的机读形式。判据遍历它,而不是在测试里重打一遍中文。
IRON_RULES: tuple[Mapping[str, str], ...] = (
    {
        "rule_id": "ORG-SEAT-NO-SOCIAL",
        "signed_by": "Owner",
        "signed_at": "2026-08-17",
        "statement": "员工席位永久剔除社媒;塞社媒路由一律 NO-GO。",
        "enforced_by": "services.xiaobang_command_contract.assert_no_social_domain",
    },
    {
        "rule_id": "XIAOBANG-GEO-ONLY",
        "signed_by": "Owner",
        "signed_at": "2026-08-18",
        "statement": "小榜只负责 GEO 板块;KB / 意图注册零社媒条目。",
        "enforced_by": "services.defensive_geo.xiaobang.kb_entries.assert_entry_is_geo_only",
    },
)

#: 本包新增的 KB 页(全部 GEO 防御侧)。路径必须落在**已进索引**的目录下 ——
#: 放在别处等于写了没人读,而 KB 判据的分母是从索引构建链解析的,
#: 不是从这张表(见 tests/xiaobang_vnext_2026_08_18/kb_index_sources.py)。
NEW_KB_PAGES: tuple[str, ...] = (
    "knowledge/system_kb/pages/defensive-geo.md",
)

#: 如实描述别处 UI 的偶发社媒字样 —— **登记在案的放行**,不是漏网。
#: 每一条都要写清楚"为什么删掉它反而更糟"。
INCIDENTAL_SOCIAL_MENTIONS: tuple[Mapping[str, str], ...] = (
    {
        "path": "knowledge/system_kb/pages/feature-pricing.md",
        "what": "分类 Tab 名列表里的「社媒IP」",
        "why_allowed": (
            "价目页上确实有这个 Tab。删掉它,KB 与真实界面对不上,"
            "用户照 KB 找 Tab 会找不到 —— 这是把真话删成假话,"
            "不降低任何风险(开发原则第 1/3 条)。"
            "结构性锁判的是「这条目在不在教用户用社媒能力」,不判措辞。"
        ),
    },
)


class KbEntryError(ValueError):
    """KB 条目不合法。**不入索引**,而不是入了再说。"""


class KbEntry(NamedTuple):
    entry_id: str
    title: str
    #: 这条 KB 教用户去哪个页面。用于结构性社媒判定。
    route: str
    #: 关联的能力命名空间(如 ``diagnosis.``)。同上。
    capability: str
    body: str


def assert_entry_is_geo_only(entry: KbEntry) -> None:
    """XIAOBANG-GEO-ONLY 的**结构性**锁。

    复用现役四轴锁(operation_id 词干 / 路由前缀 / capability 命名空间 /
    模块归属)。**不做裸子串词表匹配** —— 见模块 docstring 里
    feature-pricing.md 那个反例。
    """
    hits = social_domain_hits(
        identifier=entry.entry_id, route=entry.route, capability=entry.capability,
    )
    if hits:
        raise KbEntryError(
            f"KB 条目 {entry.entry_id!r} 命中社媒域 {hits}。"
            "小榜只负责 GEO 板块(Owner 2026-08-18 · XIAOBANG-GEO-ONLY);"
            "社媒能力不在本产品内,不进小榜知识库。"
        )


def assert_pages_are_indexed(
    declared_pages: Sequence[str], indexed_globs: Iterable[str]
) -> None:
    """新增 KB 页必须落在**已进索引**的路径下。

    🔴 这条拦的是本仓记过的那个假绿形态:页写了、锁扫了、线上答不出来 ——
       因为那个文件根本不进索引。分母是**索引构建链**,不是文件清单。
    """
    import fnmatch

    for page in declared_pages:
        normalized = page.replace("\\", "/")
        if not any(fnmatch.fnmatch(normalized, g.replace("\\", "/"))
                   for g in indexed_globs):
            raise KbEntryError(
                f"KB 页 {page} 不在任何索引 glob 下({list(indexed_globs)})。"
                "写了不进索引 = 线上问不出来,而术语锁照样全绿 —— "
                "这正是 34 班那次假绿的形态。"
            )


def assert_declared_pages_are_indexed(indexed_globs: Iterable[str]) -> None:
    """给**索引构建链**用的入口:本包声明的新 KB 页必须落在它的 glob 下。

    🔴 为什么包一层,而不是让构建链直接 import :data:`NEW_KB_PAGES`
    ----------------------------------------------------------------
    KB 索引分母的对账闸(``tests/xiaobang_vnext_2026_08_18/kb_index_sources.py``)
    会把 builder 模块里出现的**路径形状数据常量**当成"又一个索引源"要求登记。
    而 :data:`NEW_KB_PAGES` **不是**索引源 —— 它只是断言输入,这些页的内容
    仍然全部经由那同一个 glob 进库。把它登记成索引源会**重复计数**,
    让分母虚高一格,而虚高的分母比缺一格更难发现。

    所以构建链只 import 这个**函数**;数据常量留在本模块里。
    """
    assert_pages_are_indexed(NEW_KB_PAGES, indexed_globs)


def assert_no_unregistered_social_mention(
    path: str, text: str, *, social_markers: Sequence[str] = ("社媒", "社交媒体", "SocialStudio"),
) -> None:
    """措辞层的**兜底**普查:出现社媒字样时,必须已登记在
    :data:`INCIDENTAL_SOCIAL_MENTIONS` 里。

    🔴 这条与 :func:`assert_entry_is_geo_only` 是**方向相反**的一对:
       前者判"这条目在不在教用户用社媒"(结构),
       这一条判"有没有没人过目就混进来的社媒字样"(措辞)。
       只有结构锁 ⇒ 一段介绍社媒套餐的散文不会被拦;
       只有措辞锁 ⇒ 会去删真话。两条一起用才既不漏也不误伤。
    """
    hit = next((m for m in social_markers if m in text), None)
    if hit is None:
        return
    registered = {m["path"].replace("\\", "/") for m in INCIDENTAL_SOCIAL_MENTIONS}
    if path.replace("\\", "/") not in registered:
        raise KbEntryError(
            f"{path} 出现社媒字样「{hit}」但未登记。"
            "如果它是在如实描述别处 UI(例如价目页上的分类 Tab 名),"
            "请登记进 INCIDENTAL_SOCIAL_MENTIONS 并写明为什么删掉反而更糟;"
            "如果它是在教用户使用社媒能力,请删除整条 —— "
            "小榜只负责 GEO 板块(XIAOBANG-GEO-ONLY)。"
        )


def census() -> dict[str, Any]:
    return {
        "kbEntriesVersion": KB_ENTRIES_VERSION,
        "ironRules": [dict(r) for r in IRON_RULES],
        "newKbPages": list(NEW_KB_PAGES),
        "incidentalSocialMentions": [dict(m) for m in INCIDENTAL_SOCIAL_MENTIONS],
    }
