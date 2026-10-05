"""诊断报告专线 12 项修复的判别测试（2026-07-26）。

每个用例都对应 EXIT 里的一条生产实证，并且**删掉守卫就会转红**：
断言的是行为差异（口径/分母/替换/拒绝/映射），不是恒真的源码字符串存在性。

对应关系：
  P0-1 畸形品牌名        → test_p0_1_*
  P0-2 引擎清单单源      → test_p0_2_*
  P0-3 推荐率恒 0        → test_p0_3_*
  P0-4 选词质量          → test_p0_4_*
  P0-5 满分+数据不足     → test_p0_5_*
  P1-6 定向题分母        → test_p1_6_*
  P1-7 竞品消失          → test_p1_7_*
  P1-8 待确认卡片        → test_p1_8_*
  P1-9 报告按钮          → test_p1_9_*
  P1-10 0 分叙事         → test_p1_10_*
  P1-11 重复品牌         → test_p1_11_*
  P2-12 优先行动         → test_p2_12_*
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# 生产实证锚点：brands.id=278 的真实 name（含换行 + "城市:" 标签串）
PROD_MALFORMED_BRAND_NAME = "深圳驰鲸科技\n\n城市:深圳"
PROD_CLEAN_BRAND_NAME = "深圳市驰鲸科技有限公司"


# ---------------------------------------------------------------------------
# P0-1 · 畸形品牌名导致 0 分废报告仍扣费
# ---------------------------------------------------------------------------

def test_p0_1_production_malformed_brand_name_is_rejected_with_repair_exit():
    """brand 278 的真实名字必须被拒，且必须带建议名（有出口，不是光丢错误码）。"""
    from utils.brand_name_hygiene import BrandNameHygieneError, validate_brand_name

    with pytest.raises(BrandNameHygieneError) as exc:
        validate_brand_name(PROD_MALFORMED_BRAND_NAME)
    error = exc.value
    assert error.code == "BRAND_NAME_HAS_CONTROL_CHARS"
    # §13：告警必须有下一步 —— 建议名 + 修复提示 + 至少一个动作
    assert error.suggestion == "深圳驰鲸科技"
    assert error.repair_hint
    detail = error.to_detail()
    assert detail["actions"]
    assert detail["repair_hint"]


def test_p0_1_field_label_string_in_name_is_rejected():
    """「城市:深圳」这类字段标签串单独出现也要拒（不只是靠换行才拦得住）。"""
    from utils.brand_name_hygiene import BrandNameHygieneError, validate_brand_name

    with pytest.raises(BrandNameHygieneError) as exc:
        validate_brand_name("深圳驰鲸科技 城市:深圳")
    assert exc.value.code == "BRAND_NAME_HAS_FIELD_LABEL"


def test_p0_1_clean_names_still_pass():
    """反向判别：正常品牌名（含全角括号）不得被新校验误杀。"""
    from utils.brand_name_hygiene import validate_brand_name

    for name in (
        PROD_CLEAN_BRAND_NAME,
        "全域上榜（深圳）科技有限公司",
        "OmniRank AI",
        "A&B 传媒·广州",
    ):
        assert validate_brand_name(name)


def test_p0_1_all_layer_zero_with_signal_is_not_delivered_as_zero_score():
    """全层 0 命中 + 识别可疑信号 → 判疑似识别失败，而不是"分数=0"。"""
    from services.diagnosis_identity_suspicion import (
        VERDICT_SUSPECTED_IDENTITY_FAILURE,
        assess_identity_suspicion,
    )

    dimension_stats = {
        "brand_awareness": {"total": 4, "detected": 0},
        "regional_industry": {"total": 7, "detected": 0},
        "super_tier1": {"total": 20, "detected": 0},
    }
    assessment = assess_identity_suspicion(
        brand_name=PROD_MALFORMED_BRAND_NAME,
        dimension_stats=dimension_stats,
        detail_table=[],
    )
    assert assessment["verdict"] == VERDICT_SUSPECTED_IDENTITY_FAILURE
    assert assessment["suspected"] is True
    assert "brand_name_malformed" in assessment["signals"]
    assert assessment["suggested_brand_name"] == "深圳驰鲸科技"
    assert assessment["actions"]


def test_p0_1_all_layer_zero_without_signal_stays_a_real_zero():
    """反向判别：名字干净、原文里也没出现品牌名 → 是真·未被收录，不得误报识别失败。

    删掉 signals 判定（改成"只要全 0 就报识别失败"）本用例立刻转红。
    """
    from services.diagnosis_identity_suspicion import (
        VERDICT_ZERO_NO_SIGNAL,
        assess_identity_suspicion,
    )

    assessment = assess_identity_suspicion(
        brand_name=PROD_CLEAN_BRAND_NAME,
        dimension_stats={
            "brand_awareness": {"total": 5, "detected": 0},
            "regional_industry": {"total": 10, "detected": 0},
            "super_tier1": {"total": 15, "detected": 0},
        },
        detail_table=[
            {"question": "深圳TikTok代运营哪家好", "results": {
                "dashscope": {"full_response": "推荐 A 公司、B 公司。"},
            }}
        ],
    )
    assert assessment["verdict"] == VERDICT_ZERO_NO_SIGNAL
    assert assessment["suspected"] is False


def test_p0_1_brand_name_present_in_answer_but_missed_is_a_signal():
    """品牌名明明出现在回答原文里却判未命中 = 识别器与原文打架 → 必须是信号。"""
    from services.diagnosis_identity_suspicion import assess_identity_suspicion

    assessment = assess_identity_suspicion(
        brand_name=PROD_CLEAN_BRAND_NAME,
        dimension_stats={
            "brand_awareness": {"total": 5, "detected": 0},
            "regional_industry": {"total": 10, "detected": 0},
        },
        detail_table=[
            {"question": "深圳TikTok代运营哪家好", "results": {
                "dashscope": {"full_response": f"可以看看{PROD_CLEAN_BRAND_NAME}。"},
            }}
        ],
    )
    assert assessment["suspected"] is True
    assert "brand_name_present_in_answers" in assessment["signals"]


def test_p0_1_suspected_run_does_not_auto_commit_full_charge():
    """疑似识别失败的 run 不得自动全额扣费 → 转人工结算（复用既有原语，不自造资金路径）。"""
    from services import diagnosis_runs

    run = {"freeze_id": 1, "freeze_backend": "tool_freezes", "final_snapshot_jsonb": None}
    snapshot = {
        "data": {"ai_visibility": {"identity_suspicion": {"suspected": True}}},
    }
    actual, error = diagnosis_runs._partial_commit_points(run, snapshot)
    assert actual is None
    assert error == "suspected_identity_failure_manual_review"


def test_p0_1_healthy_run_keeps_original_full_charge_path():
    """反向判别：没有 suspicion 标记时行为不变（None, None = 走原全额 commit）。"""
    from services import diagnosis_runs

    run = {"freeze_id": 1, "freeze_backend": "tool_freezes", "final_snapshot_jsonb": None}
    actual, error = diagnosis_runs._partial_commit_points(run, {"data": {"ai_visibility": {}}})
    assert (actual, error) == (None, None)


# ---------------------------------------------------------------------------
# P0-2 · 引擎清单双源打架
# ---------------------------------------------------------------------------

def test_p0_2_diagnosis_and_monitoring_share_one_engine_source():
    """改常量两边同步变：诊断与监测的默认引擎清单必须来自同一个源且相等。"""
    from config import ai_engines
    from db import monitoring_db

    assert ai_engines.DIAGNOSIS_ENGINES == ai_engines.MONITORING_ENGINES
    assert monitoring_db.DEFAULT_MONITORING_PLATFORMS == ai_engines.MONITORING_PLATFORMS_CSV
    assert tuple(monitoring_db.DEFAULT_MONITORING_PLATFORMS.split(",")) == ai_engines.MONITORING_ENGINES


def test_p0_2_unified_five_engines_owner_decision():
    """Owner 裁决：统一为五引擎 dashscope/deepseek/doubao/kimi/yuanbao。"""
    from config.ai_engines import UNIFIED_ENGINES

    assert set(UNIFIED_ENGINES) == {"dashscope", "deepseek", "doubao", "kimi", "yuanbao"}
    assert len(UNIFIED_ENGINES) == 5


def test_p0_2_no_hardcoded_engine_list_left_in_diagnosis_collector():
    """诊断采集器不得再硬编码引擎清单（否则改常量只有一边生效 = 缺陷复发）。"""
    source = (ROOT / "tools" / "ai_visibility" / "ai_tester.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    hardcoded = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List):
            continue
        values = [
            elt.value for elt in node.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        ]
        if len(values) >= 3 and {"dashscope", "deepseek"} <= set(values):
            hardcoded.append(values)
    assert hardcoded == [], f"ai_tester 仍有硬编码引擎清单: {hardcoded}"


def test_p0_2_every_default_monitoring_platform_is_actually_collectable():
    """默认监测矩阵必须全部有采集实现，且实跑面 = 账本可执行面。

    [2026-08-04 订正] 原断言是 ``eligible(MONITORING_ENGINES) == MONITORING_ENGINES``。
    它在 unified5 上线后**一直是绿的，但绿错了原因**：它只比对了常量与
    ``SUPPORTED_PLATFORMS``（"有没有采集实现"），完全没碰"账本存不存得下"。
    而 ``monitoring_run_cells`` 的 CHECK 只认 classic4 四路 —— 于是任何
    entitlement 含元宝的词，跑到 ``create_monitoring_run_cells`` 直接炸掉**整批**
    任务（生产 monitoring_tasks 1543/1544 双 failed）。测试全绿，功能全炸。

    Owner 2026-08-04 裁决：账本可执行面先取 classic4 四路（低风险·可逆）。
    所以实跑面的锚点从 ``MONITORING_ENGINES``（卖了什么）换成
    ``MONITORING_RUN_CELL_PLATFORMS``（账本能跑什么）。

    两条断言各管一件事，缺一不可：
      1. 卖出去的每个引擎都得有采集实现 —— 原 P0-2 要防的"静默剔除"，原样保留；
      2. 裁出来的清单必须**逐字**等于账本可执行面 —— 防"两层各读各的白名单"复发。
    """
    from config.ai_engines import MONITORING_ENGINES, MONITORING_RUN_CELL_PLATFORMS
    from tools.monitoring.batch_monitor import PlatformAdapter

    missing = [p for p in MONITORING_ENGINES if p not in PlatformAdapter.SUPPORTED_PLATFORMS]
    assert missing == [], f"默认监测平台没有采集实现: {missing}"

    eligible = PlatformAdapter.eligible_monitoring_platforms(list(MONITORING_ENGINES))
    assert eligible == [
        p for p in MONITORING_ENGINES if p in MONITORING_RUN_CELL_PLATFORMS
    ], f"实跑面与账本可执行面不一致: eligible={eligible}"
    assert set(eligible) == set(MONITORING_RUN_CELL_PLATFORMS)


def test_sold_matrix_minus_executable_surface_is_declared_not_silent():
    """"卖了五路、只跑四路"这个缺口必须是**显式登记**的，不许静默存在。

    Owner 2026-08-04 选了"先取 classic4 四路"，代价明写在案：生产已有 35 条
    ``monitoring-unified5-v1`` 合同词（quote 409/411/421/422，其中 411/421/422
    已付款且 service active），它们买的是五引擎、实际会按四引擎跑。

    本锁的作用不是"祝福"这个差集，而是**不让它无声无息**：哪天有人把账本扩到
    五路（改 CHECK + 改启动守卫期望值 + 改常量，三处同批），差集变空，本测试
    转红，逼着把这条过期的锁和它记录的业务代价一起删掉。

    🔴 反向对照在下面第二段：差集不许**反向**（账本能跑的必须都是卖过的），
    否则就是在给客户跑他没买的引擎 —— 那是另一个方向的资金问题。
    """
    from config.ai_engines import MONITORING_ENGINES, MONITORING_RUN_CELL_PLATFORMS

    sold_not_executable = [
        p for p in MONITORING_ENGINES if p not in MONITORING_RUN_CELL_PLATFORMS
    ]
    assert sold_not_executable == ["yuanbao"], (
        "已售但账本跑不了的引擎集合变了。若是把账本扩到五路(差集变空)，"
        "请连同本测试和它记录的业务代价一起删除；若是多了别的引擎，"
        "说明又有人只推授权侧、没推执行侧 —— 正是 1543/1544 的复发。"
        f" 实际={sold_not_executable}"
    )

    executable_not_sold = [
        p for p in MONITORING_RUN_CELL_PLATFORMS if p not in MONITORING_ENGINES
    ]
    assert executable_not_sold == [], (
        f"账本会跑客户没买的引擎: {executable_not_sold}"
    )


def test_p0_2_every_default_platform_has_registered_runtime_lineage():
    """默认矩阵的每个平台都必须在**运行时血缘表**里登记。

    实测教训：血缘表原本在 PlatformAdapter.query 里抄了两份，统一五引擎时
    只改一处 → 元宝那一格 KeyError 被当成 provider 失败，客户看到
    "该平台本次未返回可用结果"，真实原因却是我们没登记血缘。
    """
    from config.ai_engines import MONITORING_ENGINES
    from tools.monitoring.batch_monitor import (
        MonitoringLineageUnregistered,
        _resolve_runtime_lineage,
    )

    for platform in MONITORING_ENGINES:
        provider, model, surface, mode = _resolve_runtime_lineage(platform, "standard")
        assert provider and model and surface and mode, platform

    # 未登记平台必须 fail-loud，不得静默编一个 provider/model 写进观测账本
    with pytest.raises(MonitoringLineageUnregistered):
        _resolve_runtime_lineage("definitely_not_a_platform", "standard")


def test_p0_2_platform_weights_cover_all_engines_and_sum_to_one():
    """权重表必须覆盖全部五个平台（漏一个 → 该平台权重被别人顶替）。"""
    from db.monitoring_db import (
        DEFAULT_PLATFORM_WEIGHTS,
        PLATFORM_CANONICAL_ORDER,
        normalize_active_platform_weights,
    )
    from config.ai_engines import MONITORING_ENGINES

    assert set(PLATFORM_CANONICAL_ORDER) == set(MONITORING_ENGINES)
    assert set(DEFAULT_PLATFORM_WEIGHTS) == set(MONITORING_ENGINES)
    weights = normalize_active_platform_weights({})
    assert set(weights) == set(MONITORING_ENGINES)
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_p0_2_kimi_and_yuanbao_weights_are_independent():
    """删掉旧的 kimi←yuanbao 别名兜底：两个真实平台不得共用一份权重。"""
    from db.monitoring_db import normalize_active_platform_weights

    weights = normalize_active_platform_weights({"yuanbao": 0.6, "doubao": 0.4})
    # kimi 缺失 → 按 DEFAULT 补齐后重归一，而不是等于 yuanbao 的值
    assert weights["kimi"] != pytest.approx(weights["yuanbao"])


def test_p0_2_unified5_migration_is_manifested_and_preserves_history():
    """迁移必须登记 manifest，且**不得** UPDATE 存量 platforms（已售监测按下单快照履约）。"""
    from db.migration_manifest import MIGRATIONS

    migration = "scripts/migration_monitoring_unified5_2026_07_26.sql"
    assert migration in MIGRATIONS
    sql = (ROOT / migration).read_text(encoding="utf-8")
    assert "monitoring-unified5-v1" in sql
    assert "dashscope,deepseek,doubao,kimi,yuanbao" in sql
    # 历史矩阵行必须保留（老词外键指向它）
    assert "monitoring-classic4-v1" in sql
    # 存量履约口径不得被批量改写
    assert "UPDATE public.client_keywords" not in sql
    assert "UPDATE public.extra_keywords" not in sql
    assert "UPDATE public.confirmed_keywords" not in sql
    assert "UPDATE monitoring_results" not in sql


def test_p0_2_yuanbao_monitoring_never_borrows_paid_diagnosis_exemption():
    """监测语境的元宝必须走 collect_observation；付费诊断的 ingest 豁免不外借。"""
    source = (ROOT / "tools" / "ai_visibility" / "ai_tester.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "query_yuanbao"
    )
    segment = ast.get_source_segment(source, target) or ""
    assert 'source_kind == "paid_diagnosis"' in segment
    assert "collect_paid_delivery" in segment
    assert "collect_observation" in segment


# ---------------------------------------------------------------------------
# P0-3 · 推荐率恒 0 + 「仅提到/推荐」假标签
# ---------------------------------------------------------------------------

def test_p0_3_recommendation_bucket_is_widened_per_owner_decision():
    """Owner 放宽口径：进候选（candidate）也算被推荐；推荐 ⊂ 提及。"""
    from services import public_report_presentation as prp

    assert "candidate" in prp._RECOMMENDATION_OUTCOMES
    assert prp._RECOMMENDATION_OUTCOMES < prp._MENTION_OUTCOMES
    assert "mentioned" in prp._MENTION_OUTCOMES
    # mentioned_only 不得并进推荐，否则两个指标失去区分度
    assert "mentioned" not in prp._RECOMMENDATION_OUTCOMES


def test_p0_3_legacy_direct_maps_to_mentioned_never_recommended():
    """存量 direct 只能映射成 mentioned；升成 recommended 就是伪造业绩。"""
    from services.mention_vocabulary import (
        MENTION_MENTIONED,
        MENTION_PENDING_IDENTITY,
        normalize_mention_type,
    )

    assert normalize_mention_type("direct") == MENTION_MENTIONED
    assert normalize_mention_type("llm_verified_fallback") == MENTION_MENTIONED
    assert normalize_mention_type("pending_identity") == MENTION_PENDING_IDENTITY
    # 未知值不当 0（PENDING/UNKNOWN ≠ 未提到，SSOT §10.2）
    assert normalize_mention_type("some_future_value") == MENTION_PENDING_IDENTITY


def test_p0_3_outcome_to_mention_never_invents_recommendation_from_detection():
    """只知道 brand_detected=True 时最多给 mentioned，不得凭空给 recommended。"""
    from services.mention_vocabulary import (
        MENTION_MENTIONED,
        MENTION_RECOMMENDED,
        mention_type_from_outcome,
    )

    assert mention_type_from_outcome(None, is_detected=True) == MENTION_MENTIONED
    assert mention_type_from_outcome("candidate_only") == MENTION_RECOMMENDED
    assert mention_type_from_outcome("recommended") == MENTION_RECOMMENDED
    assert mention_type_from_outcome("mentioned_only") == MENTION_MENTIONED


def test_p0_3_recommend_rate_is_not_stuck_at_zero():
    """有 target_outcome 的实测 → 推荐率非 0 且 ≤ 提及率。

    这是 P0-3 的核心行为断言：删掉 target_outcome 落盘或口径放宽都会转红。
    """
    from services.public_report_presentation import _evidence_and_platforms

    def _cell(outcome: str) -> dict:
        return {
            "engine": "dashscope",
            "platform_key": "dashscope",
            "full_response": "本地几家里可以优先考虑该品牌。",
            "brand_detected": outcome != "not_mentioned",
            "target_outcome": outcome,
            "search_citations": [{"url": "https://example.com"}],
        }

    raw_module = {
        "tests": [
            {"question": "深圳TikTok代运营哪家好", "layer_key": "regional_industry",
             "results": [_cell("recommended")]},
            {"question": "深圳TikTok代运营怎么选", "layer_key": "super_tier1",
             "results": [_cell("candidate_only")]},
            {"question": "深圳TikTok代运营哪家靠谱", "layer_key": "regional_industry",
             "results": [_cell("mentioned_only")]},
            {"question": "深圳TikTok代运营有推荐吗", "layer_key": "super_tier1",
             "results": [_cell("not_mentioned")]},
        ]
    }
    _evidence, platforms, _summary = _evidence_and_platforms(
        {"3_raw": raw_module}, "2026-07-26T00:00:00", brand_name="驰鲸"
    )
    assert platforms["status"] == "ready"
    row = platforms["data"][0]
    # 4 条非定向题：recommended + candidate = 2 推荐；再加 mentioned = 3 提及
    assert row["recommendRatePct"] == pytest.approx(50.0)
    assert row["mentionRatePct"] == pytest.approx(75.0)
    assert row["recommendRatePct"] < row["mentionRatePct"]


def test_p0_3_no_search_platform_citation_is_not_applicable_not_zero():
    """元宝是无联网检索表面 → 引用数「不适用」(null + 原因)，不得渲染成 0。"""
    from services.public_report_presentation import _evidence_and_platforms

    raw_module = {
        "tests": [
            {"question": "深圳TikTok代运营哪家好", "layer_key": "regional_industry",
             "results": [{
                 "engine": "yuanbao", "platform_key": "yuanbao",
                 "full_response": "可以看看这几家。",
                 "brand_detected": False, "target_outcome": "not_mentioned",
             }]},
        ]
    }
    _evidence, platforms, _summary = _evidence_and_platforms(
        {"3_raw": raw_module}, "2026-07-26T00:00:00", brand_name="驰鲸"
    )
    row = platforms["data"][0]
    assert row["platformName"] == "元宝"
    assert row["citationCount"] is None
    assert "不适用" in (row["citationStatus"] or "")


def test_p0_3_mention_vocabulary_migration_is_manifested_and_never_fabricates():
    from db.migration_manifest import MIGRATIONS

    migration = "scripts/migration_mention_vocabulary_2026_07_26.sql"
    assert migration in MIGRATIONS
    sql = (ROOT / migration).read_text(encoding="utf-8")
    assert "SET mention_type = 'mentioned'" in sql
    assert "'direct'" in sql
    # 绝不把存量升成 recommended
    assert "SET mention_type = 'recommended'" not in sql
    # 判定与原文一字不改
    assert "SET is_detected" not in sql
    assert "SET full_response" not in sql


# ---------------------------------------------------------------------------
# P0-4 · 选词质量：自造术语 + 无区域适配
# ---------------------------------------------------------------------------

def test_p0_4_production_service_name_keywords_are_replaced_with_real_questions():
    """驰鲸的 8 题服务名称必须被换成真实问法（before/after 行为断言）。"""
    from services.diagnosis_question_quality import (
        enforce_question_quality,
        looks_like_real_question,
    )

    production_keywords = [
        "TikTok工厂出海获客服务",
        "TikTok外贸精准询盘",
        "TikTok工厂全案代运营",
    ]
    # before：这三条都不是真实问法
    assert all(not looks_like_real_question(q) for q in production_keywords)

    result = enforce_question_quality(
        production_keywords,
        {q: "super_tier1" for q in production_keywords},
        brand_name=PROD_CLEAN_BRAND_NAME,
        industry="TikTok代运营",
        city="深圳",
        business_scope="regional",
        engine_count=5,
    )
    # after：全部是真实问法，且题量不缩减（§9.6 不静默丢词）
    assert len(result["questions"]) >= len(production_keywords)
    assert all(looks_like_real_question(q) for q in result["questions"])
    assert all(q not in production_keywords for q in result["questions"])

    # 隔离判别：**已经带了城市**的服务名称短语只能被"真实问法"这一条守卫抓住
    #   （地域守卫在这里是满足的）。删掉真实问法判定，本断言立刻转红。
    geo_ok_service_name = "深圳TikTok工厂全案代运营"
    isolated = enforce_question_quality(
        [geo_ok_service_name],
        {geo_ok_service_name: "regional_industry"},
        brand_name=PROD_CLEAN_BRAND_NAME,
        industry="TikTok代运营",
        city="深圳",
        business_scope="regional",
        engine_count=5,
    )
    assert geo_ok_service_name not in isolated["questions"]
    assert isolated["repairs"]
    assert isolated["repairs"][0]["problems"] == "service_name_phrase"


def test_p0_4_regional_client_questions_all_carry_geo_qualifier():
    """区域客户：除品牌题外每一道题都必须带地域限定。"""
    from services.diagnosis_question_quality import (
        LAYER_BRAND,
        enforce_question_quality,
        has_geo_qualifier,
    )

    questions = ["TikTok代运营哪家好？", "TikTok代运营怎么选？", f"{PROD_CLEAN_BRAND_NAME}是什么公司？"]
    types = {questions[0]: "regional_industry", questions[1]: "super_tier1",
             questions[2]: LAYER_BRAND}
    result = enforce_question_quality(
        questions, types,
        brand_name=PROD_CLEAN_BRAND_NAME, industry="TikTok代运营",
        city="深圳", business_scope="regional", engine_count=5,
    )
    for question in result["questions"]:
        if result["question_types"][question] == LAYER_BRAND:
            continue
        assert has_geo_qualifier(question, "深圳"), question


def test_p0_4_national_client_may_use_geo_free_big_words():
    """反向判别：全国客户的场景层允许无地域大词（否则把全国生意锁死在一个城市）。"""
    from services.diagnosis_question_quality import (
        LAYER_LOCAL,
        SCOPE_NATIONAL,
        enforce_question_quality,
        has_geo_qualifier,
        required_geo_layers,
    )

    assert required_geo_layers(SCOPE_NATIONAL) == (LAYER_LOCAL,)
    questions = ["TikTok代运营哪家好？推荐几家靠谱的"]
    result = enforce_question_quality(
        questions, {questions[0]: "super_tier1"},
        brand_name=PROD_CLEAN_BRAND_NAME, industry="TikTok代运营",
        city="深圳", business_scope="national", engine_count=5,
    )
    scenario = [q for q in result["questions"] if result["question_types"][q] == "super_tier1"]
    assert scenario
    assert any(not has_geo_qualifier(q, "深圳") for q in scenario)


def test_p0_4_business_scope_defaults_to_regional():
    """业务范围缺省/非法值一律按区域（默认值错了会让区域客户拿到全国大词）。"""
    from services.diagnosis_question_quality import SCOPE_REGIONAL, normalize_business_scope

    for value in (None, "", "  ", "unknown", "hybrid", "区域"):
        assert normalize_business_scope(value) == SCOPE_REGIONAL
    assert normalize_business_scope("全国") == "national"
    assert normalize_business_scope("national") == "national"


def test_p0_4_layer_sample_floor_scales_with_engine_count():
    """每层样本 ≥5：五引擎时 1 题够，四引擎时必须 2 题（删掉下限则转红）。"""
    from config.ai_engines import MIN_SAMPLES_PER_FUNNEL_LAYER, min_questions_per_layer

    assert MIN_SAMPLES_PER_FUNNEL_LAYER == 5
    assert min_questions_per_layer(5) == 1
    assert min_questions_per_layer(4) == 2
    assert min_questions_per_layer(2) == 3

    from services.diagnosis_question_quality import FUNNEL_LAYERS, enforce_question_quality

    result = enforce_question_quality(
        [f"{PROD_CLEAN_BRAND_NAME}是什么公司？"],
        {f"{PROD_CLEAN_BRAND_NAME}是什么公司？": "brand_awareness"},
        brand_name=PROD_CLEAN_BRAND_NAME, industry="TikTok代运营",
        city="深圳", business_scope="regional", engine_count=4,
    )
    floor = min_questions_per_layer(4)
    for layer in FUNNEL_LAYERS:
        assert result["layer_counts"][layer] >= floor, (layer, result["layer_counts"])


def test_p0_4_city_and_scope_reach_the_question_generator():
    """城市/业务范围必须真的传进选词链（旧版根本没传，prompt 只能瞎猜）。"""
    import inspect

    from tools.keyword_generator import _fallback_business_context, analyze_client_business

    params = inspect.signature(analyze_client_business).parameters
    assert "client_location" in params
    assert "business_scope" in params

    # 用户填的城市优先于品牌名里的城市
    parsed = _fallback_business_context(
        "深圳市驰鲸科技有限公司", "TikTok代运营", ["TikTok代运营"],
        client_location="杭州", business_scope="regional",
    )
    regional = [
        q for q, t in (parsed.get("question_types") or {}).items()
        if t == "regional_industry"
    ]
    assert regional
    assert all("杭州" in q for q in regional), regional
    assert all("深圳" not in q for q in regional), regional


def test_p0_4_workflow_and_api_thread_business_scope():
    """接线判别：server 请求模型与 workflow 形参都必须有 business_scope。

    用 AST 读形参而不是 import —— 导入 workflows.diagnosis_workflow 会触发一串
    DB 初始化，在空库里结果依赖 import 顺序（会造出假红）。
    """
    workflow_source = (ROOT / "workflows" / "diagnosis_workflow.py").read_text(encoding="utf-8")
    tree = ast.parse(workflow_source)
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
        and node.name == "run_diagnosis_workflow"
    )
    params = {
        arg.arg for arg in list(target.args.args) + list(target.args.kwonlyargs)
    }
    assert "business_scope" in params
    assert "client_location" in params

    server_source = (ROOT / "server.py").read_text(encoding="utf-8")
    assert "business_scope: Optional[str]" in server_source
    assert "business_scope=request.business_scope" in server_source

    # 选词链真的收到这两个参数（不是只在 workflow 签名上挂着好看）
    generator_source = (ROOT / "tools" / "keyword_generator.py").read_text(encoding="utf-8")
    assert "client_location=client_location" in generator_source
    assert "business_scope=business_scope" in generator_source


# ---------------------------------------------------------------------------
# P0-5 · 「100% + 数据不足」左右脑互搏
# ---------------------------------------------------------------------------

def test_p0_5_full_rate_with_small_sample_gets_merged_phrasing():
    """4/4 命中 + 样本 4（<5）→ 合并成一句，并给「初步达标 · 待扩测」。"""
    from tools.scoring.funnel_score import calculate_funnel_score

    result = calculate_funnel_score(
        brand_detected=4, brand_total=4,
        local_detected=0, local_total=7,
        scenario_detected=0, scenario_total=20,
    )
    brand_layer = next(layer for layer in result["layers"] if layer["key"] == "brand")
    assert brand_layer["rate_pct"] == 100.0
    assert brand_layer["data_sufficient"] is False        # 底层判定不变
    assert brand_layer["provisional"] is True
    assert brand_layer["headline_label"] == "初步达标 · 待扩测"
    # 合并表述里同时含命中数与扩测建议，不再是"满分"与"数据不足"两块并列
    assert "4/4 命中" in brand_layer["sample_note"]
    assert "扩测" in brand_layer["sample_note"]


def test_p0_5_sufficient_layer_has_no_provisional_label():
    """反向判别：样本充足层不得被打上待扩测标签。"""
    from tools.scoring.funnel_score import calculate_funnel_score

    result = calculate_funnel_score(
        brand_detected=15, brand_total=15,
        local_detected=8, local_total=10,
        scenario_detected=5, scenario_total=10,
    )
    for layer in result["layers"]:
        assert layer["provisional"] is False
        assert layer["headline_label"] is None


def test_p0_5_frontend_never_pairs_a_rate_with_a_standalone_insufficient_badge():
    """前端：有 sampleNote 时不得再单独渲染"样本不足"角标（否则又是并排互搏）。"""
    source = (
        ROOT / "frontend" / "src" / "features" / "publicReportPremium"
        / "components" / "sections" / "DecisionFunnel.tsx"
    ).read_text(encoding="utf-8")
    assert "!layer.dataSufficient && !layer.sampleNote" in source
    assert "layer.provisional && layer.headlineLabel" in source
    # 旧角标文案不得再被**渲染**（注释里作为病历描述出现是允许的，
    # 所以断言的是 JSX 渲染位置，不是整文件里有没有这串字）
    rendered_lines = [
        line for line in source.splitlines()
        if "样本不足" in line and not line.lstrip().startswith(("*", "//", "/*"))
    ]
    assert rendered_lines == [], rendered_lines


def test_p0_5_markdown_layer_text_uses_merged_phrasing():
    from services.report_writer_v2 import _rw_layer_hit_text

    layer = {
        "detected": 4, "total": 4, "data_sufficient": False, "provisional": True,
        "sample_note": "4/4 命中（样本较少，建议扩测到 8 题以上确认）",
    }
    text = _rw_layer_hit_text(layer)
    assert "初步达标" in text
    assert "扩测" in text


# ---------------------------------------------------------------------------
# P1-6 · 品牌认知题不得计入竞争格局分母
# ---------------------------------------------------------------------------

def test_p1_6_brand_directed_questions_excluded_from_mention_denominator():
    """品牌定向题必然命中，进分母会把提及率顶高 → 必须排除。"""
    from services.public_report_presentation import _evidence_and_platforms

    def _cell(outcome: str, detected: bool) -> dict:
        return {
            "engine": "dashscope", "platform_key": "dashscope",
            "full_response": "回答正文。", "brand_detected": detected,
            "target_outcome": outcome,
        }

    raw_module = {
        "tests": [
            # 品牌定向题：必然命中，不进竞争口径
            {"question": f"{PROD_CLEAN_BRAND_NAME}是什么公司？", "layer_key": "brand_awareness",
             "results": [_cell("mentioned_only", True)]},
            # 真实竞争题：1 命中 / 2 有效
            {"question": "深圳TikTok代运营哪家好", "layer_key": "regional_industry",
             "results": [_cell("recommended", True)]},
            {"question": "深圳TikTok代运营怎么选", "layer_key": "super_tier1",
             "results": [_cell("not_mentioned", False)]},
        ]
    }
    _evidence, platforms, _summary = _evidence_and_platforms(
        {"3_raw": raw_module}, "2026-07-26T00:00:00", brand_name=PROD_CLEAN_BRAND_NAME
    )
    row = platforms["data"][0]
    assert row["validSamples"] == 3                # 识别率仍看全部题
    assert row["competitiveSamples"] == 2          # 竞争口径只看非定向题
    assert row["brandDirectedSamples"] == 1
    assert row["recommendRatePct"] == pytest.approx(50.0)   # 1/2，不是 2/3
    assert row["detectionRatePct"] == pytest.approx(66.7)   # 识别率含定向题


def test_p1_6_competition_module_denominator_excludes_brand_questions():
    from services.report_writer_v2 import build_module_3_competition

    report_data = {
        "brand_name": PROD_CLEAN_BRAND_NAME,
        "diagnosis_data": {"ai_visibility_data": {
            "question_types": {
                f"{PROD_CLEAN_BRAND_NAME}是什么公司？": "brand_awareness",
                "深圳TikTok代运营哪家好": "regional_industry",
            },
            "detail_table": [
                {"question": f"{PROD_CLEAN_BRAND_NAME}是什么公司？", "results": {
                    "dashscope": {"brand_detected": True, "answer_summary": "是一家公司。"},
                }},
                {"question": "深圳TikTok代运营哪家好", "results": {
                    "dashscope": {"brand_detected": False, "answer_summary": "推荐 A 和 B。",
                                  "mentioned_brands": ["A公司", "B公司"]},
                }},
            ],
        }},
    }
    module = build_module_3_competition(report_data, brand_id=0)
    assert module["denominator_scope"] == "excludes_brand_directed_questions"
    assert module["valid_total"] == 1              # 只有那道竞争题
    assert module["client_detected_count"] == 0    # 竞争题里没命中
    assert module["brand_directed_valid"] == 1
    assert module["brand_directed_detected"] == 1


# ---------------------------------------------------------------------------
# P1-7 · 竞争格局竞品消失
# ---------------------------------------------------------------------------

def test_p1_7_extraction_failure_is_distinguishable_from_no_competitors():
    """抽取失败 ≠ 这个行业没有同行；报告必须能区分并说明原因。"""
    from services.report_writer_v2 import build_module_3_competition

    def _module(status: str) -> dict:
        return build_module_3_competition({
            "brand_name": PROD_CLEAN_BRAND_NAME,
            "diagnosis_data": {"ai_visibility_data": {
                "question_types": {"深圳TikTok代运营哪家好": "regional_industry"},
                "detail_table": [
                    {"question": "深圳TikTok代运营哪家好", "results": {
                        "dashscope": {
                            "brand_detected": False, "answer_summary": "泛化回答。",
                            "mentioned_brands": [], "mentioned_brands_status": status,
                        },
                    }},
                ],
            }},
        }, brand_id=0)

    failed = _module("extractor_error")
    assert failed["competitor_extraction_failed"] is True
    assert "未采集成功" in failed["rendered_md"]

    genuinely_empty = _module("no_brands_in_answer")
    assert genuinely_empty["competitor_extraction_failed"] is False
    assert "没有点名任何具体同行" in genuinely_empty["rendered_md"]


def test_p1_7_competitor_extractor_records_failure_reason():
    """抽取器的每条失败路径都必须留原因（旧版全是静默 return []）。"""
    import asyncio

    from tools.ai_visibility import ai_tester

    sink: dict = {}
    asyncio.run(ai_tester._extract_mentioned_brands_llm("太短", status_sink=sink))
    assert sink["mentioned_brands_status"] == "answer_too_short"

    sink2: dict = {}
    original = ai_tester.DEEPSEEK_CONFIG.get("api_key")
    try:
        ai_tester.DEEPSEEK_CONFIG["api_key"] = ""
        asyncio.run(ai_tester._extract_mentioned_brands_llm("x" * 200, status_sink=sink2))
    finally:
        if original is None:
            ai_tester.DEEPSEEK_CONFIG.pop("api_key", None)
        else:
            ai_tester.DEEPSEEK_CONFIG["api_key"] = original
    assert sink2["mentioned_brands_status"] == "extractor_not_configured"


def test_p1_7_flywheel_fallback_never_invents_competitors():
    """飞轮回落取不到数据时必须返回空，不得编造名单。"""
    from services.report_writer_v2 import _competitor_fallback_from_flywheel

    rows, source = _competitor_fallback_from_flywheel({}, "chijing", lambda x: str(x))
    assert rows == []
    assert source is None


# ---------------------------------------------------------------------------
# P1-8 · 待确认卡片显示不全
# ---------------------------------------------------------------------------

def test_p1_8_brand_cell_dto_exposes_full_evidence_and_reason():
    """SSOT §10.3 要求"查看原文和判定依据"：DTO 必须带全文与判定理由。"""
    from services.diagnosis_identity_decision import _cell_dto

    cell = {
        "brand_verdict": "UNKNOWN",
        "brand_detected": False,
        "full_response": "很长的原文" * 100,
        "answer_summary": "很长的原文…",
        "detection_reason": "alias_partial_match",
        "detection_method": "local_resolver",
        "identity_review_state": "pending",
    }
    dto = _cell_dto(
        diagnosis_id=1, brand_id=1, question="深圳TikTok代运营哪家好",
        engine="dashscope", cell=cell, identity=None,
    )
    assert dto["answer_full"]
    assert dto["detection_reason"] == "alias_partial_match"
    assert dto["detection_method"] == "local_resolver"


def test_p1_8_frontend_card_can_expand_and_keeps_buttons_outside_the_clamp():
    source = (
        ROOT / "frontend" / "src" / "pages" / "Diagnosis" / "components" / "BrandVerdictCell.tsx"
    ).read_text(encoding="utf-8")
    assert "看全文和判定依据" in source
    assert "answer_full" in source
    # 决策按钮不得依赖展开态
    assert "expanded && (" not in source.split("确认提到")[0].split("flex flex-wrap gap-1.5")[-1]
    # 移动端单列，按钮不被挤压
    assert "grid-cols-1 gap-2 sm:grid-cols-2" in source


# ---------------------------------------------------------------------------
# P1-9 · 报告按钮主次重排
# ---------------------------------------------------------------------------

def test_p1_9_report_actions_are_reordered_and_dead_path_removed():
    source = (
        ROOT / "frontend" / "src" / "pages" / "Diagnosis" / "DiagnosisReport.tsx"
    ).read_text(encoding="utf-8")
    # 死路彻底删除（含「更多操作」里指向同一路径的入口）。
    # 只看**可执行代码**，注释里描述"删掉了什么"是允许的。
    code_lines = [
        line for line in source.splitlines()
        if not line.lstrip().startswith(("*", "//", "/*"))
    ]
    code = chr(10).join(code_lines)
    assert "用报告写第一篇文章" not in code
    assert "/articles?diagnosis_id=" not in code
    # 两个主按钮
    assert "复制报告链接" in source
    assert "打开报告" in source
    assert "handleOpenReport" in source
    # 报价单降次要：它前面最近的 Button 声明必须是 ghost（主按钮不带 variant）
    quote_block = code.rsplit("生成客户报价单", 1)[0]
    assert 'variant="ghost"' in quote_block[-500:], quote_block[-500:]


# ---------------------------------------------------------------------------
# P1-10 · 0 分报告的兜底叙事
# ---------------------------------------------------------------------------

def test_p1_10_all_zero_report_gets_baseline_narrative_not_a_verdict():
    """0 分要讲成"起点基线 + 首月动作"，不是把客户判死。"""
    from services.report_writer_v2 import build_module_1_interpretation

    module = build_module_1_interpretation({
        "funnel_score": {"layers": [
            {"key": "brand", "label": "品牌认知层", "detected": 0, "total": 5, "rate_pct": 0.0},
            {"key": "local", "label": "决策获客层", "detected": 0, "total": 7, "rate_pct": 0.0},
            {"key": "scenario", "label": "场景转化层", "detected": 0, "total": 20, "rate_pct": 0.0},
        ]},
        "diagnosis_data": {"ai_visibility_data": {}},
    })
    assert "起点基线" in module["headline"]
    translation = module["business_translation"]
    assert "起点" in translation
    assert "首月" in translation
    # 必须有具体动作，不只是一串 0
    assert "官网" in translation or "发布" in translation
    assert "复测" in translation


def test_p1_10_identity_suspected_zero_is_not_called_zero_at_all():
    """疑似识别失败时连"未被收录"都不能说 —— 那是我们没认出来。"""
    from services.report_writer_v2 import build_module_1_interpretation

    module = build_module_1_interpretation({
        "funnel_score": {"layers": [
            {"key": "brand", "label": "品牌认知层", "detected": 0, "total": 5, "rate_pct": 0.0},
            {"key": "local", "label": "决策获客层", "detected": 0, "total": 7, "rate_pct": 0.0},
        ]},
        "diagnosis_data": {"ai_visibility_data": {"identity_suspicion": {
            "suspected": True,
            "signals": ["brand_name_malformed"],
            "suggested_brand_name": "深圳驰鲸科技",
        }}},
    })
    assert "没有认出" in module["headline"]
    assert "深圳驰鲸科技" in module["business_translation"]
    assert "重测" in module["business_translation"]


# ---------------------------------------------------------------------------
# P1-11 · 重复品牌提示
# ---------------------------------------------------------------------------

def test_p1_11_production_duplicate_pair_normalizes_to_the_same_key():
    """brand 278「深圳驰鲸科技」与 712「深圳市驰鲸科技有限公司」必须归一到同一键。"""
    from utils.brand_name_hygiene import brand_dedupe_key

    assert brand_dedupe_key("深圳驰鲸科技") == brand_dedupe_key("深圳市驰鲸科技有限公司")


def test_p1_11_unrelated_brands_do_not_collide():
    """反向判别：不同公司不得被归一成同一键（否则提示变噪音）。"""
    from utils.brand_name_hygiene import brand_dedupe_key

    assert brand_dedupe_key("深圳驰鲸科技") != brand_dedupe_key("深圳鲸鱼传媒")
    assert brand_dedupe_key("全域上榜") != brand_dedupe_key("全域科技")


def test_p1_11_duplicate_endpoint_only_hints_and_never_writes():
    """重复检测只提示：接口里不得出现任何写库语句。"""
    source = (ROOT / "api" / "brand_api.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "check_duplicate_client"
    )
    segment = ast.get_source_segment(source, target) or ""
    assert "similar" in segment
    assert "brand_dedupe_key" in segment
    for forbidden in ("UPDATE ", "INSERT ", "DELETE "):
        assert forbidden not in segment.upper(), forbidden


# ---------------------------------------------------------------------------
# P2-12 · 优先行动可执行化
# ---------------------------------------------------------------------------

def test_p2_12_actions_carry_concrete_volume_and_reference_data():
    """行动必须带具体动作 + 量 + 参照数据，且不得绝对化承诺收益。"""
    from services.report_action_recommendations import derive_funnel_todos

    funnel = {"layers": [
        {"key": "local", "label": "决策获客层", "detected": 0, "total": 10,
         "rate": 0.0, "data_sufficient": True},
    ]}
    context = {"local": {
        "missed_questions": ["深圳TikTok外贸代运营哪家靠谱", "深圳TikTok代运营怎么选", "深圳TikTok代运营哪家好"],
        "peer_citation_count": 12,
    }}
    todos = derive_funnel_todos(funnel, layer_context=context)
    action = todos[0]["action"]
    assert "深圳TikTok外贸代运营哪家靠谱" in action
    assert "3 个 0 命中问题" in action
    assert "篇" in action                    # 有量
    assert "约 12 条" in action              # 有参照数据
    assert "有望" in action                  # 非绝对化
    for forbidden in ("必然", "保证提升", "一定能", "100%"):
        assert forbidden not in action


def test_p2_12_missing_context_falls_back_without_inventing_numbers():
    """反向判别：没有上下文时退回原文案，不编造数字。"""
    from services.report_action_recommendations import derive_funnel_todos

    funnel = {"layers": [
        {"key": "local", "label": "决策获客层", "detected": 0, "total": 10,
         "rate": 0.0, "data_sufficient": True},
    ]}
    action = derive_funnel_todos(funnel)[0]["action"]
    assert "0 命中问题" not in action
    assert "约" not in action


def test_p2_12_small_sample_layer_asks_for_more_testing_first():
    """样本不足的层先说"扩测到 N 题再定策略"，不硬给基于 2 条样本的策略。"""
    from services.report_action_recommendations import derive_funnel_todos

    funnel = {"layers": [
        {"key": "brand", "label": "品牌认知层", "detected": 4, "total": 4,
         "rate": 1.0, "data_sufficient": False},
    ]}
    todo = derive_funnel_todos(funnel)[0]
    assert "扩测" in todo["action"]
    assert "8 题以上" in todo["action"]


def test_p2_12_layer_context_is_derived_from_real_measurements_only():
    """量化上下文必须来自本次真实实测（0 命中词 + 真实引用域名），取不到就空。"""
    from services.report_action_recommendations import build_layer_context

    assert build_layer_context(None) == {}
    assert build_layer_context({"detail_table": [], "question_types": {}}) == {}

    context = build_layer_context({
        "question_types": {"深圳TikTok代运营哪家好": "regional_industry"},
        "detail_table": [
            {"question": "深圳TikTok代运营哪家好", "results": {
                "dashscope": {"brand_detected": False, "search_citations": [
                    {"url": "https://a.com/x"}, {"url": "https://b.com/y"}, {"url": "https://a.com/z"},
                ]},
            }},
        ],
    })
    assert context["local"]["missed_questions"] == ["深圳TikTok代运营哪家好"]
    assert context["local"]["peer_citation_count"] == 2      # 去重后的域名数


# ---------------------------------------------------------------------------
# 红线：五保护文件零 diff
# ---------------------------------------------------------------------------

PROTECTED_FILES = (
    "middleware/billing.py",
    "db/wallet_db.py",
    "db/connection.py",
    "auth/middleware.py",
    "auth/jwt_utils.py",
)


def test_protected_files_are_untouched_by_this_batch():
    """五保护文件相对 base 必须零 diff（SSOT §16）。"""
    import subprocess

    base = "3a2650e8"
    result = subprocess.run(
        ["git", "diff", "--name-only", base, "--", *PROTECTED_FILES],
        cwd=ROOT, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "", f"保护文件被改动: {result.stdout}"


def test_report_evidence_platform_labels_cover_all_unified_engines():
    """[Review-CTO 2026-07-26 补锁] P0-2 元宝证据整段丢失的反向锁。

    report_evidence 消费侧按 ``engine not in _PLATFORM_LABEL: continue`` 丢证据,
    映射表少任何一个统一引擎,该平台全部实测就静默消失(客户只看到证据变少,
    看不出为什么)。执行包修了映射改单源,但没配反向锁——补上:任何人从
    _build_platform_labels 里排除引擎,本测试必须转红。
    """
    from config.ai_engines import UNIFIED_ENGINES
    from services.report_evidence import _PLATFORM_LABEL, _build_platform_labels

    live = _build_platform_labels()
    for engine in UNIFIED_ENGINES:
        assert engine in live, f"引擎 {engine} 不在平台映射里,其证据会被整段丢弃"
        assert engine in _PLATFORM_LABEL, f"模块级映射缺 {engine}(import 时构建)"
