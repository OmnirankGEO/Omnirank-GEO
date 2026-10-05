"""WP3 · 冻结实体 DTO —— 图片上的品牌名由**结构化实体**确定性写入(规格 02 §10)。

## 这是对象身份 BUG,不是文风问题

规格 README 第 8 条合同原文:「品牌占位符是对象身份 BUG,不是文风选择」。
根因(现尖复核实证,2026-08-17):

  ① `content_generator.py:807-813` 榜单路径**只**把 `entity_ref` 写死,
     可见的 `entity` / `headline` 仍是 `clip_text(模型给的 entity, 12)`;
  ② `image_pipeline.py:160,164` 烘进图片的正是那个**可见** `entity`;
  ③ 非榜单的 `per_entity` 骨架**根本没有**冻结候选件 —— 模型自由生成品牌名。

所以「`entity_ref` 对了」不等于「图片上的名字对了」:引用字段与可见字段是两条路,
只堵了一条。本模块让两者**同源**:代码从同一份 `FrozenEntity` 同时写引用与可见名。

## 没有真实候选时怎么办

**不生成虚拟品牌实体**(规格 §10.2)。改用 `per_question` / `per_feature` 骨架 ——
讲问题、讲卖点,不点名不存在的企业。这不是降级,是唯一诚实的出口:
凭空造一个"XX品牌"印在图上,既是编造第三方身份(开发原则第 2 条硬禁),
也会被 AI 判成软文指纹。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, Iterable, Mapping, Optional, Sequence

from services.article_closed_loop_contract import snapshot_hash

ENTITY_SNAPSHOT_SCHEMA: Final = "geo-image-note-frozen-entity-v1"

#: 可见实体名的显示上限。与 content_generator 既有 `clip_text(entity, 12)` 同值 ——
#: 改这个数会让卡面排版和既有成品不一致,不是本模块该动的东西。
DISPLAY_NAME_MAX: Final = 12

#: 有冻结候选时的骨架;没有时的两个诚实出口。
SKELETON_PER_ENTITY: Final = "per_entity"
SKELETON_PER_QUESTION: Final = "per_question"
SKELETON_PER_FEATURE: Final = "per_feature"


class EntityIdentityMismatch(ValueError):
    """卡面可见实体与冻结 DTO 不一致。H0 对象身份。

    🔴 只标**这一张**卡 stale/needs_redraw,不阻断整篇保存,也不丢其他成功卡
       (规格 §10.2 末条 + 01 §4.4)。
    """

    def __init__(self, card_index: int, expected: str, actual: str):
        super().__init__(
            f"第 {card_index + 1} 张卡的品牌名与冻结实体不一致:"
            f"应为 {expected!r},实际 {actual!r}"
        )
        self.card_index = card_index
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True)
class FrozenEntity:
    """一个**已确权**的候选。三件套缺一不可:身份、可见名、来源。"""

    entity_id: str
    display_name: str
    source: str                 # 来源:榜单冻结候选 / 客户资料 / 竞品发现…
    source_snapshot_hash: str

    def to_dto(self) -> dict[str, Any]:
        return {
            "entity_id": self.entity_id,
            "display_name": self.display_name,
            "source": self.source,
            "source_snapshot_hash": self.source_snapshot_hash,
        }


def freeze_entities(candidates: Sequence[Mapping[str, Any]], *, source: str) -> list[FrozenEntity]:
    """把候选清单冻结成不可变 DTO。

    🔴 `display_name` 在这里**就地定稿**并求哈希 —— 之后任何环节(包括模型)
       都不得再改它。哈希覆盖 id + 名字 + 来源,改任一项都会让下游身份校验红。
    """
    frozen: list[FrozenEntity] = []
    for index, raw in enumerate(candidates or []):
        name = str(raw.get("display_name") or raw.get("entity_name") or raw.get("name") or "").strip()
        if not name:
            # 没有名字的候选不是候选。**丢弃而不是编一个** —— 编名字正是本模块要消灭的事。
            continue
        entity_id = str(raw.get("entity_id") or raw.get("id") or f"{source}:{index}")
        digest = snapshot_hash({
            "schema": ENTITY_SNAPSHOT_SCHEMA,
            "entity_id": entity_id,
            "display_name": name,
            "source": source,
        })
        frozen.append(FrozenEntity(entity_id=entity_id, display_name=name,
                                   source=source, source_snapshot_hash=digest))
    return frozen


def choose_skeleton(frozen: Sequence[FrozenEntity], *, requested: Optional[str] = None) -> str:
    """有真实候选才允许 `per_entity`;没有就走问题卡 / 卖点卡。

    🔴 这条是**结构性**的:不是"提醒模型别编",是让 per_entity 这条路在没有候选时
       根本走不到。提醒模型别编在实测里挡不住(规格 §10.1 记的就是这件事)。
    """
    if frozen:
        return requested if requested in (SKELETON_PER_ENTITY, SKELETON_PER_FEATURE) else SKELETON_PER_ENTITY
    if requested == SKELETON_PER_FEATURE:
        return SKELETON_PER_FEATURE
    return SKELETON_PER_QUESTION


def apply_frozen_entities(cards: list[dict], frozen: Sequence[FrozenEntity]) -> list[dict]:
    """把冻结实体**确定性**写进卡片:引用字段与可见字段**同源同值**。

    覆盖模型给的任何 `entity` / `headline` —— 模型对 display name 没有话语权
    (规格 §10.2:「代码从同一 DTO 同时写 entity_ref 和可见 entity/headline;
    模型不得改 display name」)。

    只作用于 `role == 'entity'` 的卡:封面卡与收尾卡没有实体主体,
    强写会把封面标题也覆盖掉。
    """
    if not frozen:
        return cards
    cursor = 0
    for card in cards:
        if not isinstance(card, dict):
            continue
        if str(card.get("role") or "") != "entity":
            continue
        if cursor >= len(frozen):
            break
        entity = frozen[cursor]
        cursor += 1
        # 三处同源:引用 / 可见名 / 生图模板取的 headline
        card["entity_ref"] = entity.display_name
        card["entity"] = entity.display_name[:DISPLAY_NAME_MAX]
        card["headline"] = entity.display_name[:DISPLAY_NAME_MAX]
        card["entity_index"] = cursor - 1
        card["frozen_entity"] = entity.to_dto()
    return cards


def assert_card_entity_identity(cards: Sequence[Mapping[str, Any]],
                                frozen: Sequence[FrozenEntity]) -> list[EntityIdentityMismatch]:
    """渲染**前**的结构化身份校验(规格 §10.2)。

    返回不一致清单而不是抛第一个 —— 因为处置是「只标那一张卡」,
    调用方需要知道**全部**坏卡才能把好卡照常渲染出来。
    """
    problems: list[EntityIdentityMismatch] = []
    by_name = {e.display_name for e in frozen}
    for index, card in enumerate(cards or []):
        if not isinstance(card, Mapping):
            continue
        if str(card.get("role") or "") != "entity":
            continue
        dto = card.get("frozen_entity")
        if not isinstance(dto, Mapping) or not dto.get("display_name"):
            problems.append(EntityIdentityMismatch(index, "<冻结实体>", "<缺失>"))
            continue
        expected = str(dto.get("display_name") or "")
        # 可见名必须是冻结名的前缀截断(排版会截到 12 字),不能是别的字符串
        visible = str(card.get("entity") or card.get("headline") or "")
        if not expected.startswith(visible) or not visible:
            problems.append(EntityIdentityMismatch(index, expected, visible))
            continue
        if str(card.get("entity_ref") or "") != expected:
            problems.append(EntityIdentityMismatch(index, expected, str(card.get("entity_ref") or "")))
            continue
        if expected not in by_name:
            # DTO 自称的名字不在冻结名单里 —— 说明 DTO 被改过
            problems.append(EntityIdentityMismatch(index, "<冻结名单内>", expected))
    return problems


def mark_stale_cards(cards: list[dict], problems: Iterable[EntityIdentityMismatch]) -> list[int]:
    """把坏卡标 stale/needs_redraw,**其他卡继续可用**。返回被标记的下标。"""
    marked: list[int] = []
    for problem in problems:
        index = problem.card_index
        if 0 <= index < len(cards) and isinstance(cards[index], dict):
            cards[index]["entity_identity_stale"] = True
            cards[index]["needs_redraw_reason"] = "entity_identity_mismatch"
            cards[index]["user_message"] = "这张图的品牌名称需要重做"
            marked.append(index)
    return marked
