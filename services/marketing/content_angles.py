"""内容角度池(Owner 2026-07-22:用户只给主题种子,成品围绕它展开不同角度,天天发不重样)。

五类角度,每类带中文名 + prompt 指导语(该角度怎么讲、第一人称、
舒老师「先把术语翻成人话」文风)。角度是**表达观点的自由发挥**,不视为编造;
事实锚点(金额/行业/客户原话)仍以冻结证据与确认单为准。

分配与轮换:
- ``assign_angles`` 按渠道基础偏好分配,同一 job 内角度不重复(渠道数不超过
  角度池时严格不重复;超过时在池内顺位兜底,尽力不重复)。
- 重复生成按 ``rotation`` 轮换:新包取 request hash、局部重试取 job 血缘
  (父 rotation + 1),保证同主题多次生成角度不同。角度分配冻结进
  geo_snapshot(angles + angle_rotation),job 可溯。
"""
from __future__ import annotations

from typing import Optional

ANGLE_POOL: dict[str, dict] = {
    "deal_fact": {
        "name": "成交事实",
        "guidance": (
            "围绕一笔已经发生的真实成交讲:发生了什么、客户是什么行业、结果如何。"
            "第一人称复盘口吻,有一说一;金额、行业、客户原话只用冻结确认单里的,"
            "不拔高、不编故事、不补细节。"
        ),
        "visual": "画面围绕成交事实本身:喜报/事实陈述感,突出确认单上的真实要素,不虚构场景。",
    },
    "business_logic": {
        "name": "商业逻辑",
        "guidance": (
            "讲这门生意背后的账:客户为什么愿意买、钱花在哪值、双方各自算的是什么账。"
            "第一人称算账口吻,先把行业术语翻成人话,让外行也听懂这门生意为什么成立。"
        ),
        "visual": "画面围绕算账与逻辑:干净的事实/步骤编排感,像给客户算一笔明白账。",
    },
    "philosophy": {
        "name": "经营理念",
        "guidance": (
            "讲你做生意长期坚持的原则:什么事坚决不做、什么钱不赚、怎么对待客户。"
            "第一人称价值观口吻,用一件具体小事把理念讲出来,不喊口号、不说教。"
        ),
        "visual": "画面围绕人的原则与态度:克制、可信的长期主义气质,不堆砌营销元素。",
    },
    "trust_persona": {
        "name": "靠谱人设",
        "guidance": (
            "让受众觉得你这个人靠谱:讲你怎么干活、怎么交付、出了岔子怎么处理。"
            "第一人称日常口吻,讲具体动作和习惯,不自我吹嘘。"
        ),
        "visual": "画面围绕真实干活的人:工作场景纪实感,朴素可信,不摆拍不夸张。",
    },
    "industry_observation": {
        "name": "行业观察",
        "guidance": (
            "讲你在这个行业里看到的真实变化:客户在变什么、同行在干什么、接下来可能怎样。"
            "第一人称观察口吻,把行业术语翻成人话;看到的事实和你的判断分开说。"
        ),
        "visual": "画面围绕行业趋势观察:信息图式的清晰层次,事实与判断分区呈现。",
    },
}

ANGLE_KEYS: tuple = tuple(ANGLE_POOL)

# 渠道基础偏好(rotation=0 时的角度分配;示例口径:海报=成交事实、朋友圈=经营理念、
# 小红书=商业逻辑、抖音=靠谱人设、信息图=行业观察)。冲突时按池序顺位找未用角度。
CHANNEL_ANGLE_PREFERENCE: dict[str, str] = {
    "professional_poster": "deal_fact",
    "moments": "philosophy",
    "xiaohongshu": "business_logic",
    "douyin": "trust_persona",
    "private_chat": "trust_persona",
    "infographic": "industry_observation",
    "diagnosis_case": "industry_observation",
    "deal_poster": "deal_fact",
    "deal_chat": "deal_fact",
    "deal_data_card": "deal_fact",
    "deal_story": "business_logic",
    "deal_feedback_card": "deal_fact",
}


def normalize_angle(value: Optional[str]) -> Optional[str]:
    """冻结快照之外的角度值一律 fail-closed 为 None(无角度,按原口径生成)。"""
    key = str(value or "").strip().lower()
    return key if key in ANGLE_POOL else None


def rotation_from_request_hash(request_hash: str) -> int:
    """新包轮换位:request hash 决定,确定性(同一请求永远同一轮换)。"""
    try:
        return int(str(request_hash or "")[:12], 16) % len(ANGLE_POOL)
    except (TypeError, ValueError):
        return 0


def next_rotation(rotation) -> int:
    """局部重试轮换位:job 血缘父 rotation + 1,保证与上一版角度不同。"""
    try:
        base = int(rotation or 0)
    except (TypeError, ValueError):
        base = 0
    return (base + 1) % len(ANGLE_POOL)


def assign_angles(channels, *, rotation: int = 0) -> dict:
    """为每个渠道确定性地分配一个角度;同一 job 内不重复(池容量内)。

    ``rotation`` 整体平移各渠道的起始角度:同一组渠道、不同 rotation 得到
    不同分配,同主题多次生成角度因此不同。渠道数超过角度池时,超出部分
    按平移后的起始位兜底(重复不可避免,但仍确定性)。
    """
    keys = list(ANGLE_POOL)
    size = len(keys)
    try:
        shift = int(rotation or 0) % size
    except (TypeError, ValueError):
        shift = 0
    used: set[str] = set()
    assigned: dict[str, str] = {}
    for channel in [str(item) for item in (channels or [])]:
        if channel in assigned:
            continue
        base = CHANNEL_ANGLE_PREFERENCE.get(channel, keys[0])
        start = (keys.index(base) + shift) % size
        angle = keys[start]
        for step in range(size):
            candidate = keys[(start + step) % size]
            if candidate not in used:
                angle = candidate
                break
        assigned[channel] = angle
        used.add(angle)
    return assigned
