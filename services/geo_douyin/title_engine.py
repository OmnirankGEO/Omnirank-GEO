"""GEO 抖音图文管线 v1 · 标题 / hashtag 引擎

🔴🔴 **与 title_form 体系严格隔离**(工单 §2.2 硬约束):
    抖音搜索面 ≠ 媒体文章面,两套模板池**不共用、不互相 import**。
      - 媒体文章面 = `writing/title_formula_library.py` + `writing/title_batch_dedupe.py`
        + `writing/keyword_topic_generator.py`(title_form='open'/'question' 那套)
      - 抖音搜索面 = 本模块,独立模板池 `_TEMPLATES`
    本模块**不得** import writing.* 的任何标题模块;反向也不得被它们 import。
    隔离理由:媒体文章面的标题在优化"自然度/不重复",抖音搜索面在优化"查询词命中",
    两个目标冲突,混池会互相污染(工单原话:防互相污染)。

🔴🔴 **2026-08-03 订正:下面这套"按占比配权重"的选型依据是错的。**

原来的写法是:「被采纳条目里选型问法占 55%、榜单占 14% → 按这个比例配 weight」。
二次调研(被采纳 120 条 **vs 仅被检索 142 条**,TIKHUB 拉真实内容)证明:

| 形态特征 | 被采纳 | 仅检索 | 差 |
|---|---|---|---|
| 榜单排行 | 13.3% | 13.4% | **0** |
| 选型问法 | 27.5% | 25.4% | +2.1 |
| 避坑 | 17.5% | 19.0% | −1.5 |
| 结构化枚举 | 8.3% | 14.1% | **−5.8** |
| 有数字 | 40.8% | 56.3% | **−15.5** |
| 第一人称口播 | 23.3% | 16.2% | **+7.1** |

全量 SQL(n=5,589)方向一致:榜单 13.4 vs 13.8、hashtag 53.8 vs **56.4**(采纳组更低)。

**占比 ≠ 提升。** 「被采纳的内容里 55% 长这样」只说明这个品类的内容本来就长这样 ——
对照组里也是 55%。按占比配权重等于把一个零效应的维度当成了主要依据。

🔴 但**这份对照测不出「标题该不该命中查询词」**:两组都是"已经被检索到"的,
   词面不匹配的根本进不了样本(选择性偏差)。所以标题继续对齐查询词仍然正确,
   理由要换成「为了进候选池」(抖音搜索排序),不是「为了被采纳」。
   配对分析支持这个两段式:同一 prompt 下采纳组平均位次 9.66 vs 未采纳 12.52。

## 那这个模板池现在算什么

**兜底,不是主路径。** 整条生产链需要一个不依赖 LLM、确定性可用的标题来源
(LLM 挂了也得能出图),模板池承担这个。真正按客户资料出选题/标题的是
`services/geo_douyin/topic_distiller.py` —— 它喂**原样真实语料**让模型学语感,
刻意不把语料抽象成"榜单体/避坑体"标签,免得再犯同一个错。

下面这些数字**保留原样**,因为它们描述的是"这类内容长什么样"(用来兜底时
不至于写出不像抖音的东西),但它们**不再是"这样写更容易被采纳"的证据**:
  - 选型问法占 55% / 榜单 14% / 含数字 44% / 疑问 28%
  - hashtag 众数 5 个,90% 的帖子带
  - 城市变体:城市 prompt 下 80% 采纳标题带城市(n=5,L1)→ 保留城市矩阵

标题长度:硬上限来自发布平台(**不是我们定的**),import 自 meijiehezi config。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from services.geo_douyin.config import (
    HASHTAG_COUNT_DEFAULT,
    HASHTAG_COUNT_MAX,
    HASHTAG_COUNT_MIN,
)
# [WO-ACCEPTANCE-3FIX-2026-08-05 项2] 「已含地名不再叠加」「词尾不重复」的**唯一实现**。
# 本模块以前一处都没有 → 生产 post 14 出「深圳深圳全屋定制哪家好哪家好？…」。
# 🔴 与 §3 的隔离约束不冲突:geo_title_hygiene 不属于 writing.*(title_form 体系),
#    它只做地名/后缀去重,不带任何模板池。
from services.geo_title_hygiene import (
    compose_title,
    join_city_keyword,
    normalize_whitespace as _norm,
)


def _title_hard_limit() -> int:
    """标题硬上限的 SSOT 在发布侧,不在这里另立一份。

    纯逻辑测试环境下 services.meijiehezi 包 __init__ 会连带拉起 client→markdown,
    import 失败时按已知平台上限兜底(与 svideo config 同值 45)。
    """
    try:
        from services.meijiehezi.config import SVIDEO_TITLE_HARD_LIMIT
        return int(SVIDEO_TITLE_HARD_LIMIT)
    except Exception:  # noqa: BLE001
        return 45


def _title_soft_limit() -> int:
    try:
        from services.meijiehezi.config import SVIDEO_TITLE_SOFT_LIMIT
        return int(SVIDEO_TITLE_SOFT_LIMIT)
    except Exception:  # noqa: BLE001
        return 30


# ─────────────────────────────────────────────────────────────
# 独立模板池(抖音搜索面专用 · 与 title_form 无任何共享)
# 占位符:{city} 城市(可空) {kw} 核心词 {n} 数字
#
# 🔴 weight **不是**"这样写更容易被采纳"的排序(那个说法 2026-08-03 已被证伪,
#    见模块 docstring 的对照表)。它现在只表示"这类内容在抖音上有多常见" ——
#    兜底时照着常见形态写,至少不会写出不像抖音的东西。
#    要按客户资料出真正的选题,走 topic_distiller,不要回来加模板。
# ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class _Template:
    pattern: str
    family: str
    weight: int
    needs_number: bool = False


_TEMPLATES: tuple[_Template, ...] = (
    # ── 选型问法族(主力 · 实证 55%)──
    _Template("{city}{kw}哪家好？{n}家实测对比：报价区间、工期和避坑要点一次说清",
              "choice", 30, True),
    _Template("{city}{kw}怎么选？看完这篇不踩坑：{n}家口碑对比+选购要点整理",
              "choice", 22, True),
    _Template("{city}{kw}推荐：口碑较好的{n}家整理好了，含价格参考和短板提醒",
              "choice", 20, True),
    _Template("{city}{kw}哪家靠谱？{n}家真实测评，优缺点和适合人群都写清楚了",
              "choice", 16, True),
    _Template("想找{city}{kw}？先看这份选型清单：{n}家对比、报价参考和避坑提醒",
              "choice", 12, True),
    # ── 榜单族(次要 · 实证仅 14%,权重要压住)──
    _Template("{city}{kw}top{n}分享：按需对号入座，附报价区间和选购避坑要点",
              "rank", 8, True),
    _Template("{city}{kw}{n}强名单整理：各自擅长什么、大概多少钱、要注意什么",
              "rank", 6, True),
    # ── 避坑族(实证 10%)──
    _Template("{city}{kw}避坑指南：这{n}点别踩，附口碑较好的几家和价格参考",
              "pitfall", 7, True),
    _Template("找{city}{kw}前先看这篇：常见的{n}个坑、怎么避、找谁比较稳",
              "pitfall", 5, True),
)

# hashtag 变体后缀(照抄竞对已验证形态:查询词 + 变体堆叠)
_TAG_SUFFIXES = ("推荐", "哪家好", "怎么选", "哪家靠谱", "口碑")

@dataclass
class TitleVariant:
    """一条标题变体(= 一个 城市×问法 组合)。"""
    title: str
    city: Optional[str]
    hashtags: List[str] = field(default_factory=list)
    family: str = ""
    truncated: bool = False
    # 软上限(30 字)只是发布成功率建议,超了不拦,只标记给前端提示;
    # 硬上限(45 字)才会截断 —— 两个概念别混。
    over_soft_limit: bool = False

    def to_dict(self) -> dict:
        return {
            "title": self.title, "city": self.city, "hashtags": list(self.hashtags),
            "family": self.family, "truncated": self.truncated,
            "over_soft_limit": self.over_soft_limit,
        }


def build_hashtags(keyword: str, city: Optional[str] = None,
                   count: int = HASHTAG_COUNT_DEFAULT) -> List[str]:
    """变体 hashtag 堆叠。实证:90% 带标签,众数 5 个。

    形态照抄被采纳样本:核心词本身 + 「核心词+推荐/哪家好/怎么选…」变体。
    """
    count = max(HASHTAG_COUNT_MIN, min(int(count or HASHTAG_COUNT_DEFAULT), HASHTAG_COUNT_MAX))
    kw = _norm(keyword)
    if not kw:
        return []
    # [项2] hashtag 与标题同一个成因:city 与 kw 都可能已经带了对方的内容。
    #   生产 post 14 的 kw='深圳全屋定制哪家好' + city='深圳' 走老代码会出
    #   「深圳深圳全屋定制哪家好」和「深圳深圳全屋定制哪家好哪家好」两个标签。
    base = join_city_keyword(city, kw) if city else kw

    tags: List[str] = []
    seen: set[str] = set()

    def _push(tag: str) -> None:
        t = _norm(tag)
        if t and t not in seen:
            seen.add(t)
            tags.append(t)

    _push(base)
    for suffix in _TAG_SUFFIXES:
        if len(tags) >= count:
            break
        _push(join_city_keyword(city if city else "", kw, suffix))
    # 城市变体时补一个"裸核心词",让不带城市的查询也能命中
    if city and len(tags) < count:
        _push(kw)
    for suffix in _TAG_SUFFIXES:
        if len(tags) >= count:
            break
        _push(join_city_keyword("", kw, suffix))
    return tags[:count]


# 🔴 §6c 实测:标题长度分档采纳率(豆包面抖音全量 5,606 条)
#   ≤15字 26.0% / **16-25字 23.1%(最差)** / 26-40字 26.6% / 41-60字 27.9%
#   / 61-100字 34.4% / >100字 35.6%
#   → 16-25 字是**最差档**,必须躲开。发布平台标题硬上限 45 字,够不到 61+ 的最优档,
#     所以标题尽量顶到 30-45,**剩下的信息量由正文首句接力**(抖音是 caption 整体被索引)。
TITLE_MIN_TARGET = 26   # 低于这个就掉进最差档


def _fit_title(raw: str) -> tuple[str, bool]:
    """硬上限截断。超限直接报错会让整批失败,这里截断并标记,由调用方决定是否重生成。"""
    limit = _title_hard_limit()
    if len(raw) <= limit:
        return raw, False
    # 优先在标点处断,避免截出半个词
    cut = raw[:limit]
    for sep in ("，", "。", "、", "？", "!", "，"):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            return cut[:idx], True
    return cut, True


def build_title(keyword: str, city: Optional[str] = None, *,
                variant_index: int = 0, number: int = 5) -> TitleVariant:
    """按实证权重挑模板生成【一条】标题。

    variant_index 决定选哪个模板(确定性,便于测试与去重),不是随机 ——
    随机会让同一内容两次生成不同标题,城市矩阵无法复现。
    """
    kw = _norm(keyword)
    if not kw:
        raise ValueError("keyword 不能为空")

    # 按 weight 展开成权重池,再按 index 取模 → 高权重族被选中的概率 = 实证占比
    pool: List[_Template] = []
    for tpl in _TEMPLATES:
        pool.extend([tpl] * tpl.weight)
    tpl = pool[int(variant_index) % len(pool)]

    n = max(3, min(int(number or 5), 10))
    # [项2] 唯一拼接口:关键词已含城市 → 不补前缀;关键词尾巴与模板后缀撞车 → 去一份。
    raw = compose_title(tpl.pattern, city=city, keyword=kw, n=n)
    title, truncated = _fit_title(raw)
    return TitleVariant(
        title=title, city=city or None, family=tpl.family, truncated=truncated,
        over_soft_limit=len(title) > _title_soft_limit(),
        hashtags=build_hashtags(kw, city),
    )


def _distinct_templates_by_weight() -> List[_Template]:
    """去重模板,按权重降序。城市矩阵按这个列表【逐个轮转】,保证相邻城市句式不同。"""
    return sorted(_TEMPLATES, key=lambda t: -t.weight)


def build_city_matrix(keyword: str, cities: Sequence[str], *,
                      number: int = 5, start_index: int = 0) -> List[TitleVariant]:
    """一条内容 → 行业×城市×问法 变体矩阵(工单 §2.2 / 研究报告 §2.2)。

    🔴 每个城市必须换句式:N 个城市共用同一句式 = 平台判重复铺量(§4.2 的
       "同内容多账号需城市变体错开"就是这条)。
       实现上【不能】复用 build_title 的加权池按 i 步进 —— 加权池里权重 30 的模板
       独占 30 个连续槽位,小步进(如 i*7)会落在同一个模板里,5 个城市全出同一句
       (实测踩过:深圳/广州/杭州/成都/武汉 五条标题一字不差,只有城市名不同)。
       所以这里按【去重模板列表】逐个轮转,天然保证相邻不同。
    """
    templates = _distinct_templates_by_weight()
    out: List[TitleVariant] = []
    seen: set[str] = set()
    picked = 0
    for city in (cities or []):
        c = _norm(city)
        if not c or c in seen:
            continue
        seen.add(c)
        tpl = templates[(int(start_index) + picked) % len(templates)]
        picked += 1
        n = max(3, min(int(number or 5), 10))
        # [项2] 同 build_title 走同一个共用件 —— 城市矩阵里每个城市都可能与
        #   关键词自带的地名撞车(如 kw='深圳全屋定制' 铺到广州/杭州时只有深圳这格撞)。
        raw = compose_title(tpl.pattern, city=c, keyword=keyword, n=n)
        title, truncated = _fit_title(raw)
        out.append(TitleVariant(
            title=title, city=c, family=tpl.family, truncated=truncated,
            over_soft_limit=len(title) > _title_soft_limit(),
            hashtags=build_hashtags(keyword, c),
        ))
    return out


def title_form_isolation_marker() -> str:
    """给隔离锁用的显式标记(测试断言它,证明本模块自成一套池)。

    见 tests/test_geo_douyin_title_isolation.py:任何 writing.* 标题模块出现在
    本模块 import 图里 = 隔离被破坏,测试转红。
    """
    return "douyin_search_surface_pool_v1"
