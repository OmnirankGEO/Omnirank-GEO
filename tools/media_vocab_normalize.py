"""
媒体词表规范化(写入侧唯一入口)· WO_VOCAB_CONVERGENCE_2026-08-09 §3-L2 / §3-L5

存在的理由:媒介盒子 / 快易播两套上游把**同一个概念写成不同字符串**,并且把
**不是行业的东西塞进行业列**。存量回填只能治已经进来的那批,治不了下一次同步 ——
所以规范化必须钉在写入侧(所有 INSERT/UPDATE mhz_* 的入口),回填才有意义。
顺序不能反:先落闸,再回填;反过来回填完下一次同步立刻写回来。

两件事:

1) **地域同义归一**(Owner 2026-08-09 批两组)
   - 「综合全国」→「全国」
   - 「海外」→「全球」
   只做**同义合并**,不做地名解析、不做省市推断 —— 那是另一件事(§3-L2 未批部分)。

2) **价格档从体裁列剥离**(Owner 2026-08-09 选 (c) 叠加标记)
   「套餐系列 / 十元专区 / 最新秒杀」是**上架档位**不是内容体裁,却和「新闻资讯」
   「汽车网站」并排躺在 mhz_media.resource_type_name / category 里。
   (c) 方案 = **原值一个字不动**,额外落 listing_slot 标记 → 零信息损失、零回归;
   消费侧凭 listing_slot 判断"这行的类目其实不是类目"。

🔴 本模块**只做纯字符串映射**,不查库、不调 LLM、不抛异常(上游脏值一律原样返回)。
   写入侧接线出错的代价是全量同步挂掉,所以这里绝不引入任何可能失败的依赖。
"""
from __future__ import annotations

from typing import Optional

# ============ 1. 地域同义归一(Owner 2026-08-09 批)============
# key = 被合并掉的写法 · value = 保留的写法
AREA_SYNONYMS: dict[str, str] = {
    "综合全国": "全国",
    "海外": "全球",
}

# ============ 2. 价格档(上架档位 · 不是内容体裁)============
# 2026-08-09 生产实测(mhz_media 全表 52166 行):
#   resource_type_name 侧 套餐系列 664 / 十元专区 383 / 最新秒杀 182 = 1229 行
#   category           侧 套餐系列 330 / 十元专区 168 / 最新秒杀  50 =  548 行(1229 的子集)
LISTING_SLOTS: frozenset[str] = frozenset({"套餐系列", "十元专区", "最新秒杀"})


def canonical_area(value: Optional[str]) -> str:
    """地域值归一。非同义词原样返回(只 strip),空值返空串。

    >>> canonical_area("综合全国")
    '全国'
    >>> canonical_area("海外")
    '全球'
    >>> canonical_area("广东")     # 反向:不在同义表里的一律不动
    '广东'
    """
    if value is None:
        return ""
    trimmed = str(value).strip()
    if not trimmed:
        return ""
    return AREA_SYNONYMS.get(trimmed, trimmed)


def is_listing_slot(value: Optional[str]) -> bool:
    """这个值是不是"上架档位"(而非内容体裁)。"""
    if value is None:
        return False
    return str(value).strip() in LISTING_SLOTS


def detect_listing_slot(*values: Optional[str]) -> Optional[str]:
    """从若干个候选类目值里认出价格档 · 认不出返 None。

    按传入顺序取第一个命中(调用方按权威度排:resource_type_name 优先于 category)。

    >>> detect_listing_slot("十元专区", "新闻资讯")
    '十元专区'
    >>> detect_listing_slot("新闻资讯", "套餐系列")
    '套餐系列'
    >>> detect_listing_slot("新闻资讯", "汽车网站")   # 反向:没有档位就是 None
    """
    for value in values:
        if is_listing_slot(value):
            return str(value).strip()
    return None


def normalize_media_row(media: dict) -> dict:
    """mhz_media 写入前规范化(就地改 · 同时返回同一个 dict 方便链式调用)。

    - area 归一
    - 落 listing_slot(resource_type_name 优先,其次 category)
    - 🔴 遵 Owner (c):resource_type_name / category **原值不动**

    对不含相应键的 dict 是 no-op(两套上游写不同列,不能假设键都在)。
    """
    if not isinstance(media, dict):
        return media
    if "area" in media:
        media["area"] = canonical_area(media.get("area"))
    media["listing_slot"] = detect_listing_slot(
        media.get("resource_type_name"), media.get("category")
    )
    return media


def normalize_wemedia_row(media: dict) -> dict:
    """mhz_wemedia 写入前规范化 · 自媒体侧地域列叫 province(无 area / 无价格档)。"""
    if not isinstance(media, dict):
        return media
    if "province" in media:
        media["province"] = canonical_area(media.get("province"))
    return media
