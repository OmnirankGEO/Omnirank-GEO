# -*- coding: utf-8 -*-
"""写作大厅「补全知识库」基础资料表 ←→ `client_profiles` 的**单点映射**。

═══════════════════════════════════════════════════════════════════
🔴 为什么要一个模块,而不是在 PUT 和 GET 里各写一份

读侧和写侧必须用**同一张表**。各写一份的话,加一个字段时漏改一边,
症状是「保存成功,刷新回来还是空」—— 与本单要修的缺陷一模一样,
而且不会有任何东西报错。同一谓词两处,必有一处没验。

🔴 六个字段的去处不一样,这不是随意的

逐个**真写真读**验过(写一段带顿号和逗号的自由文本,再 SELECT 回来逐字比对):

  target_customers   -> target_users        ✅ 逐字相同
  key_selling_points -> selling_points      ✅ 逐字相同
  forbidden_notes    -> brand_constraints   ✅ 逐字相同
  business_summary   -> business_summary    ✅ 列一直在,只是从来不在写白名单里
  products_services  -> products            🔴 被按顿号/逗号**切碎成数组**
  proof_cases        -> success_cases       🔴 同上

切碎发生在 `db/profile_db.py` 的 `array_fields` 分支:纯字符串会
「按顿号/逗号拆分为数组」。所以后两个字段**不能**复用那两列,
落 `client_profiles.basic_info_fields`(JSONB,不进 array_fields)。

🔴 不去改 `products` 的数组语义:那是**有意的** ——
   客户档案页按数组用它(`MarketingTab.tsx:193`),后端 44 份、前端 26 份在读。
🔴 也不把六个**全部**塞进 JSONB:`target_users` / `selling_points` /
   `brand_constraints` 今天就被客户档案页的自由文本框编辑着。
   两个面共享**同一份事实**正是本单要的;全塞进来等于给同一个谓词造两个住处。
═══════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

#: 表单上的六个字段,**顺序即表单顺序**(交付单与前端按这个顺序列)。
BASIC_FIELDS: tuple[str, ...] = (
    "business_summary",
    "target_customers",
    "products_services",
    "key_selling_points",
    "proof_cases",
    "forbidden_notes",
)

#: 有同语义真列的 —— 表单字段 -> `client_profiles` 的列名。
COLUMN_FOR: Dict[str, str] = {
    "business_summary": "business_summary",
    "target_customers": "target_users",
    "key_selling_points": "selling_points",
    "forbidden_notes": "brand_constraints",
}

#: 没有去处的 —— 住 JSONB,键名 = 表单字段名。
JSONB_COLUMN = "basic_info_fields"
JSONB_FIELDS: tuple[str, ...] = ("products_services", "proof_cases")

#: 🔴 明确写下**不许碰**的列,并配反臂判据。
#:  · negative_feedback:array_fields,且有专门的 `add_negative_feedback` 追加口
#:    走飞轮语义(content_api「我有意见」的负反馈)。把「不能说的话」写进去会污染飞轮。
#:    前端旧读链把它当 forbidden_notes 的第三优先 —— 那是**读**,不代表可以往里写。
#:  · products / success_cases:见抬头,**经 `update_profile` 这条路**写进去会被切碎。
#:    🔴 别把它们说成「是数组」:形态**取决于写入方** —— `_json_text_or_none`
#:    只在调用方传 list/dict 时才 dumps,传 str 原样存,所以生产上两种都有。
#:    禁写的理由是「形态不定 + 这条路会切碎」,不是「它是个数组」。
FORBIDDEN_COLUMNS = frozenset({"negative_feedback", "products", "success_cases"})

# 分母自证:每个表单字段都必须**恰好**有一个去处,不能漏也不能两处都占。
assert set(COLUMN_FOR) | set(JSONB_FIELDS) == set(BASIC_FIELDS), (
    "字段去处不全:%s" % (set(BASIC_FIELDS) ^ (set(COLUMN_FOR) | set(JSONB_FIELDS))))
assert not (set(COLUMN_FOR) & set(JSONB_FIELDS)), "有字段同时占了真列和 JSONB 两个住处"
assert not (set(COLUMN_FOR.values()) & FORBIDDEN_COLUMNS), (
    "映射把某个字段指向了禁写列:%s" % (set(COLUMN_FOR.values()) & FORBIDDEN_COLUMNS))


def _as_dict(value: Any) -> Dict[str, Any]:
    """JSONB 列读出来可能是 dict,也可能还是字符串(取决于取数路径)。"""
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def from_profile(profile: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """从一行档案里读出六个表单字段。

    🔴 老档案读出 `None` **不抛**,返回空串 —— 建于本迁移之前的档案里
       这些位置本来就没有东西,那不是错误。
    """
    if not profile:
        return {f: "" for f in BASIC_FIELDS}
    blob = _as_dict(profile.get(JSONB_COLUMN))
    out: Dict[str, str] = {}
    for field in BASIC_FIELDS:
        if field in COLUMN_FOR:
            raw = profile.get(COLUMN_FOR[field])
        else:
            raw = blob.get(field)
        out[field] = "" if raw is None else str(raw)
    return out


def to_update_kwargs(
    values: Mapping[str, Any],
    existing_profile: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """把表单提交的字段翻成 `update_profile(**kwargs)`。

    语义(与判据一一对应):
      · `values` 里**没有的键** = 没传 ⇒ 不出现在返回值里 ⇒ 不覆盖已有值;
      · 显式传 `""` = 用户清空了这一栏 ⇒ 会写进去(不是"没传");
      · 显式传 `None` 一律当**没传**(FastAPI 把缺省字段填成 None)。

    🔴 JSONB 那两个字段是**合并写**,不是整块替换:
       只传 `products_services` 时,`proof_cases` 必须原样留着。
       整块替换会让「改一栏」变成「清掉另一栏」,而且不会有任何东西报错。
    """
    kwargs: Dict[str, Any] = {}
    blob_updates: Dict[str, str] = {}

    for field in BASIC_FIELDS:
        if field not in values:
            continue
        raw = values.get(field)
        if raw is None:
            continue
        text = str(raw)
        if field in COLUMN_FOR:
            kwargs[COLUMN_FOR[field]] = text
        else:
            blob_updates[field] = text

    if blob_updates:
        merged = _as_dict((existing_profile or {}).get(JSONB_COLUMN))
        merged.update(blob_updates)
        kwargs[JSONB_COLUMN] = merged

    # 🔴 出门前自检:绝不能有禁写列混进去。这条不是装饰 ——
    #    `forbidden_notes` 的前端读链第三优先就是 `negative_feedback`,
    #    照着读链写回是个很容易犯的错。
    bad = set(kwargs) & FORBIDDEN_COLUMNS
    if bad:
        raise AssertionError(
            "基础资料表试图写入禁写列 %s —— 见 writing_basics_contract 抬头" % sorted(bad))
    return kwargs
