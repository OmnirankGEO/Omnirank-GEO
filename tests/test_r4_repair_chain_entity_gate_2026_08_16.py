# -*- coding: utf-8 -*-
"""R4/R5.1 · 修复链实体轴门 + 主体核对(第 3 个 prompt 出口,2026-08-16)。

R4:build_source_hint / evidence_number_pool 没过实体轴门 → 错实体条目进修复
LLM prompt → UPDATE articles 写回正文。R5.1(裁定 §9.1)收窄药方:
存量无轴条目**零降级零标记**;错实体归属由**修复链主体核对**在消费端管 ——
修目标品牌 X 的 span 时,条目须 entity_confirmed 于 X,或无 entity 且
标题/claim 不含本文实体白名单(pack 自带 entity 名 = 客户+竞品)里 X 以外
的名字。单品牌 pack 的无主体素材照常可用。

本文件 = 修复链四态行为锁 + 成对端到端反向锁(拒错家 / 本家可用防过杀)。
裸触 items 白名单计数锁在 tests/test_p0_2。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.span_level_repair import (  # noqa: E402
    CATEGORY_FABRICATED_NUMBER,
    CATEGORY_FAKE_AUTHORITY,
    build_source_hint,
    evidence_number_pool,
    pack_entity_whitelist,
    repair_subject_admits,
)


def _hv_item(evidence_id: str, *, title: str, claim: str,
             entity: str | None = None, binding: str | None = None) -> dict:
    """human_verified + 溯源三件套齐(Review §7 复现形态:核验/溯源全过,
    只有实体轴/主体归属不该放行)。"""
    item = {
        "evidence_id": evidence_id,
        "verification_status": "human_verified",
        "human_reviewed_by": "qa-reviewer",
        "human_reviewed_at": "2026-08-01T00:00:00Z",
        "human_review_reason": "人工复核通过",
        "url": f"https://example.com/{evidence_id}",
        "title": title,
        "publisher": "某某网",
        "published_at": "2026-08-01",
        "claim": claim,
    }
    if entity is not None:
        item["entity"] = entity
    if binding is not None:
        item["binding_state"] = binding
    return item


def _four_state_pack() -> dict:
    return {"items": [
        _hv_item("EV-C", title="甲公司获省级认证", claim="甲公司通过认证,指标 73 项",
                 entity="甲公司", binding="entity_confirmed"),
        # 缺 binding + 带 entity = 生产 1,097 条 verified 的形态(Review 复现态)
        _hv_item("EV-M", title="错公司年度报告", claim="错公司营收 81 亿",
                 entity="错公司"),
        _hv_item("EV-U", title="乙公司白皮书", claim="乙公司份额 92%",
                 entity="乙公司", binding="entity_unverified"),
        _hv_item("EV-D", title="丙公司排行榜", claim="丙公司位列 64 名",
                 entity="丙公司", binding="different_entity"),
    ]}


# ------------------------------------------------------------------ R4 四态(统一门层)
def test_source_hint_four_state_gate():
    """[R4 · 第 3 出口] 修复提示词的[可用来源]只认 entity_confirmed。

    变异 R4/Mc(绕过消费 API)→ 本测试转红。
    """
    hint = build_source_hint(_four_state_pack(), "该产品获权威认证", CATEGORY_FAKE_AUTHORITY)
    assert "甲公司获省级认证" in hint, "confirmed 条目必须仍可作来源(门不许误伤)"
    assert "错公司年度报告" not in hint, "缺 binding 条目进了修复 prompt(R4 复现形态)"
    assert "乙公司白皮书" not in hint
    assert "丙公司排行榜" not in hint


def test_source_hint_number_route_same_gate():
    """数字类路由(§3.3 同数字匹配)同样过门:错实体条目里出现同一数字也不算来源。"""
    hint = build_source_hint(_four_state_pack(), "指标 81 项", CATEGORY_FABRICATED_NUMBER)
    assert "错公司年度报告" not in hint, "数字对上了但实体轴是候选 —— 不得给修复模型当来源"


def test_number_pool_four_state_gate():
    """[R4] 数字保留资格 = 特权:只有过统一门的条目的数字进池。

    变异 R4/Md(绕过消费 API)→ 本测试转红。
    """
    pool = evidence_number_pool(_four_state_pack())
    assert "73" in pool, "confirmed 条目的数字必须仍在池内(门不许误伤)"
    assert "81" not in pool, "缺 binding 条目的数字获得了保留资格(可反写进正文)"
    assert "92" not in pool
    assert "64" not in pool


# ------------------------------------------------------------------ R5.1 主体核对(成对)
def _multi_brand_pack() -> dict:
    """深档多品牌 pack 的真实形态:客户「甲公司」+ 竞品「乙竞品」各有 confirmed
    条目,另有一条**无 entity** 但标题/claim 点名乙竞品的检索素材,和一条
    无主体的行业素材。"""
    return {"items": [
        _hv_item("EV-X", title="甲公司获省级认证", claim="甲公司通过认证,指标 73 项",
                 entity="甲公司", binding="entity_confirmed"),
        _hv_item("EV-Y", title="乙竞品产能报告", claim="乙竞品年产能 58 万台",
                 entity="乙竞品", binding="entity_confirmed"),
        _hv_item("EV-N", title="乙竞品渠道调研纪要", claim="乙竞品经销网点 240 家"),
        _hv_item("EV-T", title="行业白皮书", claim="行业均值 55%"),
    ]}


def test_repair_subject_pair_wrong_home_rejected_own_home_usable():
    """[R5.1 §9.1 成对端到端锁 · 正反两向]

    修「甲公司」的 span:乙竞品的 confirmed 条目、以及无 entity 但点名乙竞品
    的条目都不可用(把 A 的事实写成 B 的事实 = 三硬禁之三);
    同样的 pack 修「乙竞品」的 span:那两条**照常可用**(防过杀)。

    变异 R5.1/Mi(拆主体核对)→ 本测试转红。
    """
    pack = _multi_brand_pack()
    # 修甲公司:别家素材(实体条目 + 点名条目)全拒;本家/无主体素材可用
    hint_x = build_source_hint(pack, "产能与网点覆盖领先", CATEGORY_FAKE_AUTHORITY,
                               target_entity="甲公司")
    assert "甲公司获省级认证" in hint_x
    assert "行业白皮书" in hint_x, "无主体行业素材被误拦(过杀 = 批量降级)"
    assert "乙竞品产能报告" not in hint_x, "竞品 confirmed 条目修了客户的 span"
    assert "乙竞品渠道调研纪要" not in hint_x, (
        "无 entity 但点名竞品的条目修了客户的 span —— §9.1 端到端复现形态"
    )
    pool_x = evidence_number_pool(pack, target_entity="甲公司")
    assert "73" in pool_x and "55" in pool_x
    assert "58" not in pool_x and "240" not in pool_x, "竞品数字获得保留资格"
    # 同一条目在它自己家的语境:照常可用(防过杀)
    hint_y = build_source_hint(pack, "产能与网点覆盖领先", CATEGORY_FAKE_AUTHORITY,
                               target_entity="乙竞品")
    assert "乙竞品产能报告" in hint_y
    assert "乙竞品渠道调研纪要" in hint_y
    assert "甲公司获省级认证" not in hint_y
    pool_y = evidence_number_pool(pack, target_entity="乙竞品")
    assert "58" in pool_y and "240" in pool_y and "73" not in pool_y


def test_single_brand_pack_no_subject_items_usable():
    """[R5.1 改写 R4 反向锁] 单品牌 pack(白名单只有本家/为空)的无主体素材
    照常可用 —— 零降级(旧「无轴不受门约束」锁按 §9.1 语义重写于此)。"""
    pack = {"items": [
        _hv_item("EV-T", title="行业统计", claim="行业均值 55%"),
    ]}
    assert "55" in evidence_number_pool(pack, target_entity="甲公司")
    hint = build_source_hint(pack, "行业表现领先", CATEGORY_FAKE_AUTHORITY,
                             target_entity="甲公司")
    assert "行业统计" in hint
    # 未接线旧调用(target 空)= 现行为,不静默变语义
    assert "55" in evidence_number_pool(pack)


def test_pack_entity_whitelist_full_roster():
    """[R5.2-1 元判据] 白名单 = 完整 roster(queries/entities_covered/extra/
    items 补充)+ 确定性别名展开(核心词简称)。"""
    wl = pack_entity_whitelist(_multi_brand_pack())
    assert "甲公司" in wl and "乙竞品" in wl
    assert pack_entity_whitelist({"items": []}) == ()
    # queries 里的零结果 lane 实体必须进白名单(items 派生法在此漏空)
    pack = {
        "items": [],
        "queries": [{"entity": "丙零果竞品科技有限公司", "query": "丙零果竞品 案例"}],
        "evidence_supply": {"entities_covered": ["丁覆盖竞品"]},
    }
    wl2 = pack_entity_whitelist(pack, extra_names=("戊名单竞品",))
    assert "丙零果竞品科技有限公司" in wl2
    assert "丁覆盖竞品" in wl2 and "戊名单竞品" in wl2
    # 别名展开:全称的核心词简称也在(brand_core_term 剥行政区划/后缀)
    from writing.evidence_research import brand_core_term

    assert brand_core_term("丙零果竞品科技有限公司") in wl2


def test_zero_result_lane_competitor_still_blocks():
    """[R5.2-1 端到端锁] 竞品 lane 零结果(不在 items 里)+ 主题素材点名该
    竞品 → 修客户的 span 仍拒(items 派生白名单在此漏防,roster 版抓住)。"""
    pack = {
        "items": [
            _hv_item("EV-X", title="甲公司获省级认证", claim="甲公司指标 73 项",
                     entity="甲公司", binding="entity_confirmed"),
            # 零结果竞品未出现在任何 item.entity,但主题素材点名了它
            _hv_item("EV-Z", title="丙零果竞品渠道扩张观察", claim="丙零果竞品网点 310 家"),
        ],
        "queries": [
            {"entity": "甲公司", "query": "甲公司 资质"},
            {"entity": "丙零果竞品", "query": "丙零果竞品 案例"},  # 一条没搜回来
        ],
    }
    hint = build_source_hint(pack, "网点覆盖领先", CATEGORY_FAKE_AUTHORITY,
                             target_entity="甲公司")
    assert "甲公司获省级认证" in hint
    assert "丙零果竞品渠道扩张观察" not in hint, (
        "零结果 lane 竞品点名条目修了客户的 span —— R5.2-1 复现形态"
    )
    assert "310" not in evidence_number_pool(pack, target_entity="甲公司")


def test_alias_short_full_pair_locks():
    """[R5.2-1 简称/全称正反锁]
    反:白名单只有全称,素材点名**简称** → 仍拒(别名展开);
    正:target 自己的简称出现在自家素材里 → 不拒(同企对齐防过杀)。"""
    pack = {
        "items": [
            # 无 entity 素材点名竞品简称「栖舍设计」;白名单只有全称(来自 queries)
            _hv_item("EV-S", title="栖舍设计中标某项目", claim="栖舍设计签约金额 66 万"),
            # target 自家的无 entity 素材点名自家简称
            _hv_item("EV-Me", title="饰界品牌完成品牌升级", claim="饰界品牌门店 12 家"),
        ],
        "queries": [
            {"entity": "深圳饰界品牌管理有限公司", "query": "饰界 案例"},
            {"entity": "深圳栖舍设计有限公司", "query": "栖舍 案例"},
        ],
    }
    target = "深圳饰界品牌管理有限公司"
    hint = build_source_hint(pack, "品牌实力领先", CATEGORY_FAKE_AUTHORITY,
                             target_entity=target)
    assert "栖舍设计中标某项目" not in hint, "竞品简称点名条目漏进(别名展开失效)"
    assert "饰界品牌完成品牌升级" in hint, "target 自家简称被当竞品拒了(过杀)"
    pool = evidence_number_pool(pack, target_entity=target)
    assert "66" not in pool and "12" in pool


def test_repair_subject_admits_unit_matrix():
    """主体核对谓词的判定矩阵(确定性字符串判别,不做开放式内容检测)。"""
    wl = ("甲公司", "乙竞品")
    ok = dict(title="甲公司认证", claim="", entity="甲公司")
    assert repair_subject_admits(ok, target_entity="甲公司", entity_whitelist=wl)
    other = dict(title="乙竞品产能", claim="", entity="乙竞品")
    assert not repair_subject_admits(other, target_entity="甲公司", entity_whitelist=wl)
    named = dict(title="乙竞品渠道调研", claim="")
    assert not repair_subject_admits(named, target_entity="甲公司", entity_whitelist=wl)
    assert repair_subject_admits(named, target_entity="乙竞品", entity_whitelist=wl)
    neutral = dict(title="行业白皮书", claim="行业均值")
    assert repair_subject_admits(neutral, target_entity="甲公司", entity_whitelist=wl)
    # target 空 = 未接线旧调用,放行(不静默变语义)
    assert repair_subject_admits(named, target_entity="", entity_whitelist=wl)
