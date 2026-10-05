# -*- coding: utf-8 -*-
"""P0-2 · 主优势结构化 + 形态闸 + relevance 门槛 + 实体绑定三态 · 判别锁(2026-08-14)。

变异点:
  M1 拆形态闸(is_malformed_advantage 恒 False)→ test_repr_shape_rejected 红;
  M2 拆结构化取值(dict 走 str())→ test_dict_value_never_reprs 红;
  M3 拆域名/名称判别(entity_binding_state 恒 confirmed)→ 三态测试全红;
  M4 拆渲染隔离(render 不过滤 different_entity)→ test_writer_render_isolates 红;
  M5 拆计分隔离(_evidence_refs 不过滤)→ test_refs_skip_different_entity 红。
"""
from __future__ import annotations

from writing.evidence_pack import normalize_evidence_pack, render_evidence_pack_for_writer
from writing.evidence_research import (
    BINDING_CONFIRMED,
    BINDING_DIFFERENT,
    BINDING_UNVERIFIED,
    ENTITY_BINDING_RULE_ID,
    ENTITY_BINDING_RULE_VERSION,
    brand_core_term,
    entity_binding_state,
)
from writing.primary_advantage import (
    build_primary_advantage_block,
    candidate_advantages,
    is_malformed_advantage,
    rank_candidates,
)

# ------------------------------------------------------------------ 三态判别
_KZ_ITEM = {"title": "KZ Woodcrafts custom cabinets", "excerpt": "Handmade wood furniture",
            "claim": "x", "url": "https://kzwoodcrafts.com/about", "publisher": "kzwoodcrafts.com"}


def test_known_wrong_entity_judged_different() -> None:
    """研究定稿方法学负结果的确定性替代:kzwoodcrafts 42 条形态必须判 different。"""
    assert entity_binding_state(_KZ_ITEM, "QZQZ") == BINDING_DIFFERENT


def test_same_name_different_company_judged_different() -> None:
    """工单判别测试原文:「作者张三」类同名异企域名 → binding=different。"""
    item = {"title": "作者张三的博客", "excerpt": "个人随笔", "claim": "x",
            "url": "https://zhangsan-blog.com", "publisher": "zhangsan-blog.com"}
    assert entity_binding_state(item, "张三科技") == BINDING_DIFFERENT


def test_full_name_in_content_confirmed() -> None:
    # 反向对照:真实同主体页面绝不能被判 different(第三方关系事实不受影响)
    item = {"title": "岱林生物获省级认证", "excerpt": "浙江岱林生物是一家…", "claim": "x",
            "url": "https://news.example.com/1", "publisher": "新华网"}
    assert entity_binding_state(item, "浙江岱林生物") == BINDING_CONFIRMED


def test_partial_token_unverified_not_isolated() -> None:
    # 中间态:≥3 字子串命中 → unverified(A1 三出口),不隔离
    item = {"title": "岱林生物产品介绍", "excerpt": "", "claim": "x",
            "url": "https://a.com", "publisher": ""}
    assert entity_binding_state(item, "浙江岱林生物技术股份") == BINDING_UNVERIFIED


def test_scope_field_never_consulted() -> None:
    """反向锁:scope 含检索 query(query 必含实体名)—— 判定若读 scope 恒 confirmed。"""
    item = dict(_KZ_ITEM, scope="搜索问题:QZQZ 官网 简介 资质")
    assert entity_binding_state(item, "QZQZ") == BINDING_DIFFERENT


# ------------------------------------------------------------------ R2-4 反例矩阵
def test_brand_core_term_strips_weak_parts() -> None:
    """元判据:核心词提取真的剥掉行政区划与组织后缀。"""
    assert brand_core_term("深圳栖舍设计有限公司") == "栖舍设计"
    assert brand_core_term("浙江岱林生物技术股份有限公司") == "岱林生物技术"
    assert brand_core_term("晨光富士电梯有限公司") == "晨光富士电梯"
    # 反向:剥空保护 —— 名字全是弱词也剩 ≥2 字,不产生空核心词
    assert len(brand_core_term("深圳有限公司")) >= 2


def _item_about(text: str) -> dict:
    return {"title": text, "excerpt": "", "claim": "", "url": "https://x.com", "publisher": ""}


def test_r2_4_same_city_different_company() -> None:
    """🔴 R2-4 反例矩阵①(返修单原例):同城不同企 —— 只共享「深圳/有限公司」
    必须判 different,不许被弱词降成 unverified。"""
    item = _item_about("深圳栖舍设计有限公司 全屋整装案例")
    assert entity_binding_state(item, "深圳饰界品牌管理有限公司") == BINDING_DIFFERENT


def test_r2_4_same_industry_different_company() -> None:
    """R2-4 反例矩阵②:同行业不同企(共享「生物技术/股份有限公司」)。"""
    item = _item_about("浙江岱林生物技术股份有限公司 实验设备介绍")
    assert entity_binding_state(item, "江苏赛康生物技术股份有限公司") == BINDING_DIFFERENT


def test_r2_4_same_suffix_different_company() -> None:
    """R2-4 反例矩阵③:同后缀不同企(共享「电梯有限公司」)。"""
    item = _item_about("晨光富士电梯有限公司 参数手册")
    assert entity_binding_state(item, "苏州迅达电梯有限公司") == BINDING_DIFFERENT


def test_core_term_hit_confirms_without_full_legal_name() -> None:
    """反向对照:页面只写核心词(不带行政区划/后缀)必须 confirmed ——
    去弱词不能把真主体也误杀。"""
    item = _item_about("栖舍设计获 2025 年度设计大奖")
    assert entity_binding_state(item, "深圳栖舍设计有限公司") == BINDING_CONFIRMED


def test_alias_supported() -> None:
    """标准名 + 别名(R2-4 改法原文):别名命中同样 confirmed。"""
    item = _item_about("TL-Bio 岱林生物 发布新品")
    assert entity_binding_state(
        item, "浙江岱林生物技术股份有限公司", aliases=("TL-Bio",),
    ) == BINDING_CONFIRMED


def test_h0_registration_fields_exist() -> None:
    """手册 §1.4「新增 H0 必须写清」:rule_id / rule_version 是可导入常量。"""
    assert ENTITY_BINDING_RULE_ID == "H0-ENTITY-BINDING"
    assert ENTITY_BINDING_RULE_VERSION == 1


# ------------------------------------------------------------------ normalize 保鲜
def test_normalize_keeps_binding_state_only_when_present() -> None:
    pack = normalize_evidence_pack({"items": [
        {"evidence_id": "EV-001", "claim": "c", "url": "https://a.com",
         "binding_state": "different_entity"},
        {"evidence_id": "EV-002", "claim": "c", "url": "https://b.com"},
    ]}, request_id="t")
    assert pack["items"][0]["binding_state"] == "different_entity"
    # 反向(manifest 纪律):没有值的条目不许凭空长键
    assert "binding_state" not in pack["items"][1]


def test_legacy_pack_manifest_hash_stable() -> None:
    """历史 pack(无 binding_state)重过 normalize,manifest_hash 不得漂移。"""
    raw = {"request_id": "t", "created_at": "2026-01-01T00:00:00+00:00", "items": [
        {"evidence_id": "EV-001", "claim": "c", "url": "https://a.com"},
    ]}
    h1 = normalize_evidence_pack(dict(raw), request_id="t")["manifest_hash"]
    h2 = normalize_evidence_pack(dict(raw), request_id="t")["manifest_hash"]
    assert h1 == h2


# ------------------------------------------------------------------ H0 隔离面
def _pack_with_binding() -> dict:
    return normalize_evidence_pack({"items": [
        {"evidence_id": "EV-001", "relationship": "support", "claim": "别家公司的素材",
         "title": "KZ Woodcrafts", "url": "https://kzwoodcrafts.com",
         "binding_state": "different_entity"},
        {"evidence_id": "EV-002", "relationship": "support", "claim": "交付周期 45 天 调研",
         "title": "本品牌交付调研", "url": "https://ok.com", "publisher": "新华网",
         "binding_state": "entity_confirmed"},
    ]}, request_id="t")


def test_writer_render_four_state_gate() -> None:
    """R2-1 四态行为锁(写作渲染门):confirmed 下发;unverified/different/
    缺绑定的实体条目 = 候选不下发;主题 lane(无实体轴)照常下发。"""
    pack = normalize_evidence_pack({"items": [
        {"evidence_id": "EV-001", "relationship": "support", "claim": "c",
         "title": "t1", "url": "https://c.com", "entity": "甲公司",
         "binding_state": "entity_confirmed"},
        {"evidence_id": "EV-002", "relationship": "support", "claim": "c",
         "title": "t2", "url": "https://u.com", "entity": "甲公司",
         "binding_state": "entity_unverified"},
        {"evidence_id": "EV-003", "relationship": "support", "claim": "c",
         "title": "t3", "url": "https://d.com", "entity": "甲公司",
         "binding_state": "different_entity"},
        {"evidence_id": "EV-004", "relationship": "support", "claim": "c",
         "title": "t4", "url": "https://m.com", "entity": "甲公司"},
        {"evidence_id": "EV-005", "relationship": "support", "claim": "c",
         "title": "t5", "url": "https://topic.com"},
    ]}, request_id="t")
    text = render_evidence_pack_for_writer(pack)
    assert "EV-001" in text, "confirmed 条目被误拦(门过严)"
    assert "EV-002" not in text, "unverified 实体条目进了正文链(R2-1)"
    assert "EV-003" not in text, "different_entity 条目仍被下发(H0 隔离失效)"
    assert "EV-004" not in text, "缺 binding 的实体条目进了正文链 —— 生产 1,097 条同形态"
    # [R5.1 §9.1] 无轴条目零降级零标记(「无 lane=候选」方案已撤回:拿元数据
    # 不完整代替真实性判断违反开发原则第 1/3 条);错实体归属由修复链主体核对管
    assert "EV-005" in text, "无实体轴条目被误拦(批量降级网络素材,原则第 1/3 条)"


def test_by_entity_block_four_state_gate() -> None:
    """🔴 R3-1 四态行为锁(第 4 出口:逐家分组块)。该块自己就是写作 prompt 的
    输入(逐条带「归属句照抄」)—— 缺 binding 的实体条目(生产 1,097 条形态)
    曾从这里漏进 prompt(Review 构造实证)。与 for_writer 同门:只有
    confirmed 进分组;缺 binding/unverified/different 一律不进。"""
    from writing.evidence_pack import render_evidence_pack_by_entity

    pack = normalize_evidence_pack({"items": [
        {"evidence_id": "EV-001", "relationship": "support", "claim": "c",
         "title": "t1", "url": "https://c.com", "entity": "甲公司",
         "binding_state": "entity_confirmed", "publisher": "新华网"},
        {"evidence_id": "EV-002", "relationship": "support", "claim": "c",
         "title": "t2", "url": "https://u.com", "entity": "甲公司",
         "binding_state": "entity_unverified", "publisher": "新华网"},
        {"evidence_id": "EV-003", "relationship": "support", "claim": "c",
         "title": "t3", "url": "https://d.com", "entity": "甲公司",
         "binding_state": "different_entity", "publisher": "新华网"},
        {"evidence_id": "EV-004", "relationship": "support", "claim": "c",
         "title": "t4", "url": "https://m.com", "entity": "甲公司", "publisher": "新华网"},
    ]}, request_id="t")
    block = render_evidence_pack_by_entity(pack, ["甲公司"])
    assert "EV-001" in block, "confirmed 条目被误拦(门过严)"
    assert "EV-002" not in block, "unverified 实体条目进了分组块"
    assert "EV-003" not in block, "different_entity 条目进了分组块"
    assert "EV-004" not in block, (
        "缺 binding 的实体条目带着「归属句照抄」进了分组块 —— R3-1 漏缝复发"
    )


def test_by_entity_scope_fallback_kept_for_no_axis_items() -> None:
    """反向对照(裁定 §4-2 / R5.1):**无实体轴**条目(无 entity 无 binding)
    的 scope/标题回退匹配 = C-2 T2 既有设计,统一门不误伤(零降级)。"""
    from writing.evidence_pack import render_evidence_pack_by_entity

    pack = normalize_evidence_pack({"items": [
        {"evidence_id": "EV-009", "relationship": "support", "claim": "c",
         "title": "含甲公司字样的旧条目", "url": "https://x.com", "publisher": "新华网"},
    ]}, request_id="t")
    assert "EV-009" in render_evidence_pack_by_entity(pack, ["甲公司"])


def test_prompt_outlet_enumeration_lock() -> None:
    """🔴 R5 裸触 items 白名单计数锁(设计反转后的降维锁)。

    语法污点扫描在这根轴上连输两回(R3 单文件锁漏跨文件出口;R4 全仓污点锁
    被 下标/quote字段/f-string/helper拆分 四形态全逃逸)—— 不再赌第三回。
    R5 起:统一门长在唯一消费 API(iter_entity_admissible_items)上,业务代码
    禁止裸触 pack["items"];本锁只做一件没有逃逸面的事 ——
    **全仓每个 .py 的裸触 items 文本计数必须逐文件等于冻结快照**:
    - 新文件出现任何 `["items"]` / `.get("items")`(单双引号皆算)→ 红;
    - 既有文件计数上涨 → 红(evidence_pack.py 内部也一样,rogue helper 藏
      不进白名单文件);计数下降(迁移到访问器)→ 更新快照即可;
    - 快照里绝大多数条目是**非 pack 的 items 键**(菜单/API payload 等),
      一并冻结:预算制,涨一处就得过门审 —— 宁可误拦,不留逃逸面。
    """
    import re
    import subprocess
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    files = subprocess.run(
        ["git", "ls-files", "*.py"], capture_output=True, text=True, cwd=repo,
    ).stdout.split()
    assert len(files) > 100, "git ls-files 分母异常(空分母 = 假绿)"
    pattern = re.compile(r"""\[\s*["']items["']\s*\]|\.get\(\s*["']items["']""")
    FROZEN = {
        "agent-test-artifacts-round2/_runner.py": 5,
        "agent-test-artifacts-round2/_runner_final.py": 3,
        "agent-test-artifacts-round2/_runner_retry.py": 2,
        "agent-test-artifacts-round2/_runner_retry2.py": 1,
        "agent-test-artifacts-round3/retest_v21_hotfix.py": 4,
        "agent-test-artifacts-round3/sD2_redo_21_27.py": 1,
        "agent-test-artifacts-round3/sF_rbac.py": 1,
        "agent-test-artifacts-round3/sI_admin.py": 2,
        "api/agent_workbench_api.py": 4,
        "api/content_api.py": 1,
        "api/dashboard_api.py": 1,
        "api/finance_api.py": 1,
        # [merge 2026-08-17 门审] 监测大车 8f8bd101 带入:飞轮绑定候选扫描
        # 结果字典的 items 键(非 evidence pack),生产已跑,按预算冻结。
        "api/media_entity_flywheel_api.py": 2,
        "db/media_entity_flywheel_db.py": 2,
        "api/gap_plan_api.py": 7,
        "api/m3_material_confirm_api.py": 2,
        "api/marketing_confirm_api.py": 1,
        "api/pricing_ssot_api.py": 2,
        "db/brand_image_assets_db.py": 6,
        "db/diagnosis_db.py": 1,
        "db/meijiehezi_db.py": 1,
        "docs/AI-CONTEXT/INDUSTRY_REPLAY_FINAL_2026-08-08.py": 3,
        "scripts/ab_realsource_arm_2026_08_11.py": 1,
        "scripts/ab_recommendability_local_2026_08_10.py": 1,
        "scripts/generate_pptx_demo.py": 1,
        "scripts/product/social_advisor_import_readiness_probe.py": 1,
        "scripts/qa_backend_2026_04_29.py": 2,
        "scripts/sanitize_legacy_article_bodies.py": 4,
        "scripts/validate_geo_article_v14.py": 1,
        "server.py": 7,
        "services/billing_debt_offset_outbox.py": 1,
        "services/bonus_grants.py": 2,
        "services/client_knowledge.py": 1,
        "services/customer_operation_plan.py": 2,
        "services/dealer_inventory_resale.py": 1,
        "services/demo_access.py": 1,
        "services/gap_assistant.py": 2,
        "services/gap_operation_plan.py": 1,
        "services/geo_douyin/knowledge_context.py": 1,
        "services/geo_douyin/ranking_router.py": 1,
        "services/media_domain_directory.py": 1,
        "services/multi_ai_voter.py": 2,
        "tools/agent_loop/adapters/search.py": 1,
        "tools/agent_loop/adapters/tikhub.py": 2,
        "tools/industry_knowledge_collector.py": 5,
        "tools/scoring/geo_scorer_backup.py": 2,
        "tools/tikhub/tikhub_tools.py": 2,
        "writing/evidence_pack.py": 6,
    }
    observed: dict[str, int] = {}
    for rel in files:
        if rel.startswith(("tests/", "frontend/")):
            continue
        n = len(pattern.findall((repo / rel).read_text(encoding="utf-8", errors="replace")))
        if n:
            observed[rel] = n
    grew = {k: (FROZEN.get(k, 0), v) for k, v in observed.items() if v > FROZEN.get(k, 0)}
    assert not grew, (
        f"裸触 items 超出冻结快照(文件: 冻结→观测){grew} —— "
        "消费必须走 iter_entity_admissible_items(),结构用途走 raw_pack_items();"
        "确属合法新增(非 pack 的 items 键)需门审后更新快照"
    )
    shrunk = {k: (v, observed.get(k, 0)) for k, v in FROZEN.items() if observed.get(k, 0) < v}
    assert not shrunk, (
        f"快照过期(计数下降,请把快照改小,别留水位给未来涨回):{shrunk}"
    )
    # ── [R5.2-2] raw 访问器收口:调用面同样冻结 ──
    # raw_pack_items 是「能拿到候选条目」的口子,新调用方 = 潜在新出口。
    # 🔴 按**裸词**计数不按调用形态:别名导入(`raw_pack_items as _x`)后
    #    全文只剩别名,`raw_pack_items(` 形态锁当场漏空 —— 词计数把 import/
    #    def/模块属性访问全兜住,别名必经 import,无逃逸面。
    raw_pat = re.compile(r"raw_pack_items")
    RAW_FROZEN = {
        "scripts/rebind_assessment_2026_08_15.py": 2,
        "services/span_level_repair.py": 2,
        "services/standard_citation_guard.py": 2,
        "writing/article_generator_service.py": 5,
        "writing/body_internal_marker_sanitizer.py": 2,
        "writing/brand_evidence_asset.py": 2,
        "writing/evidence_pack.py": 3,
        "writing/evidence_precision_policy.py": 2,
        "writing/evidence_research.py": 1,
    }
    raw_obs: dict[str, int] = {}
    for rel in files:
        if rel.startswith(("tests/", "frontend/")):
            continue
        n = len(raw_pat.findall((repo / rel).read_text(encoding="utf-8", errors="replace")))
        if n:
            raw_obs[rel] = n
    raw_grew = {k: (RAW_FROZEN.get(k, 0), v) for k, v in raw_obs.items()
                if v > RAW_FROZEN.get(k, 0)}
    assert not raw_grew, (
        f"raw_pack_items 调用面扩张(文件: 冻结→观测){raw_grew} —— "
        "新结构用途需门审;消费用途一律走 iter_entity_admissible_items()"
    )
    raw_shrunk = {k: (v, raw_obs.get(k, 0)) for k, v in RAW_FROZEN.items()
                  if raw_obs.get(k, 0) < v}
    assert not raw_shrunk, f"raw 快照过期(计数下降请收水位):{raw_shrunk}"


def test_consumption_api_wiring_lock() -> None:
    """R5 接线锁:四出口 + 评分/修复链全部真走唯一消费 API(源码级断言)。"""
    import inspect

    import services.span_level_repair as slr
    import writing.evidence_pack as ep
    import writing.primary_advantage as pa

    for fn in (ep.render_evidence_pack_for_writer, ep.render_evidence_pack_by_entity,
               slr.build_source_hint, slr.evidence_number_pool):
        assert "iter_entity_admissible_items" in inspect.getsource(fn), (
            f"{fn.__qualname__} 没走唯一消费 API"
        )
    assert "iter_entity_admissible_items" in inspect.getsource(pa)


def test_writer_render_keeps_topic_lane_legacy_items() -> None:
    """[R5.1 §9.1] 历史无轴条目照常下发 —— 零降级零标记(「无标记=候选」
    方案已撤回;847 条存量不因元数据不完整降级)。"""
    pack = normalize_evidence_pack({"items": [
        {"evidence_id": "EV-009", "relationship": "support", "claim": "c",
         "title": "t", "url": "https://x.com"},
    ]}, request_id="t")
    assert "EV-009" in render_evidence_pack_for_writer(pack)


def test_refs_four_state_gate() -> None:
    """R2-1 四态行为锁(优势计分门):带实体轴的条目只有 confirmed 计分。"""
    cands = [{"advantage": "交付周期 45 天,支持定制", "field": "delivery_capability",
              "fact_ref": "BF-001"}]

    def _rank_with(extra: dict) -> list:
        item = {"evidence_id": "EV-001", "relationship": "support",
                "title": "交付周期 45 天 调研", "claim": "交付周期 45 天",
                "excerpt": "", **extra}
        return rank_candidates(cands, question="交付周期哪家快",
                               evidence_pack={"items": [item]})

    assert _rank_with({"binding_state": "different_entity"})[0]["evidence_refs"] == []
    assert _rank_with({"entity": "甲公司", "binding_state": "entity_unverified"})[0]["evidence_refs"] == [], (
        "unverified 实体条目给候选抬了分(R2-1)"
    )
    assert _rank_with({"entity": "甲公司"})[0]["evidence_refs"] == [], (
        "缺 binding 的实体条目给候选抬了分 —— 生产 1,097 条同形态"
    )
    assert "EV-001" in _rank_with({"entity": "甲公司", "binding_state": "entity_confirmed"})[0]["evidence_refs"]
    # 反向对照:主题 lane 条目(无实体轴)照常计分(R5.1:零降级)
    assert "EV-001" in _rank_with({})[0]["evidence_refs"]


# ------------------------------------------------------------------ 形态闸 + 结构化取值
def test_dict_value_never_reprs() -> None:
    snap = {"claims": [{
        "claim_id": "BF-001", "field": "core_selling_points",
        "value": [{"name": "6 米整板电视柜工艺特色", "desc": "无拼缝"}, "交付周期 30 天以内"],
        "provenance": "customer_provided",
    }]}
    texts = [c["advantage"] for c in candidate_advantages(snap)]
    assert texts, "结构化取值一条都没取出来"
    assert not any("{" in t or "'" in t for t in texts), f"repr 泄漏进候选: {texts}"
    assert any("电视柜工艺" in t for t in texts), "dict 的 name 文本键没被结构化提取"


def test_repr_shape_rejected() -> None:
    # 元判据:闸函数对 repr 形态真的响
    assert is_malformed_advantage("{'name': '工艺', 'desc': 'x'}")
    assert is_malformed_advantage('{"claim": "交付周期 45 天"}')
    # 反向:正常中文短语(含冒号的自然句)不误伤
    assert not is_malformed_advantage("交付周期 30 天以内,支持定制")
    assert not is_malformed_advantage("服务范围:深圳全市 2 小时响应")
    # 闸接进 candidate_advantages(坏数据直接给 repr 字符串也进不来)
    snap = {"claims": [{"claim_id": "BF-001", "field": "service_scope",
                        "value": "{'a': '坏数据形态串进来了没'}", "provenance": "x"}]}
    assert candidate_advantages(snap) == []


def test_atom_carries_structured_fields() -> None:
    snap = {"claims": [{"claim_id": "BF-001", "field": "after_sales",
                        "value": "深圳本地 2 小时响应,质保 24 个月",
                        "provenance": "customer_provided"}]}
    atom = candidate_advantages(snap)[0]
    assert atom["fact_ref"] == "BF-001"
    assert atom["provenance"] == "customer_provided"
    assert atom["why"] == "售后"


# ------------------------------------------------------------------ relevance 门槛(机制常开·数值走配置)
def test_relevance_threshold_default_inert(monkeypatch) -> None:
    monkeypatch.delenv("GEO_ADVANTAGE_MIN_RELEVANCE", raising=False)
    ranked = rank_candidates(
        [{"advantage": "完全无关的另一件事情说明", "field": "", "fact_ref": ""}],
        question="观光电梯交付周期",
    )
    assert ranked[0]["low_relevance"] is False, "默认阈值必须 0(不比现状激进 —— 工单 §四)"


def test_relevance_threshold_marks_and_demotes(monkeypatch) -> None:
    monkeypatch.setenv("GEO_ADVANTAGE_MIN_RELEVANCE", "0.9")
    ranked = rank_candidates(
        [{"advantage": "完全无关的另一件事情说明", "field": "", "fact_ref": ""},
         ],
        question="观光电梯交付周期",
    )
    assert ranked[0]["low_relevance"] is True
    # 全军低相关时保底放行(门槛不许把主优势块饿死 —— D8 零阻断)
    block = build_primary_advantage_block("甲品牌", "观光电梯交付周期", ranked)
    assert "完全无关的另一件事情说明" in block
    # 高低混合时:低相关降为辅证段,不占主候选位
    # (keyword 必须传:相关性 = 0.5×关键词通道 + 0.5×问题通道,漏传会把
    #  真相关候选也压到 0.5 以下 —— 本测试第一版就栽在这里)
    mixed = rank_candidates(
        [{"advantage": "观光电梯交付周期 45 天承诺", "field": "", "fact_ref": "BF-1"},
         {"advantage": "完全无关的另一件事情说明", "field": "", "fact_ref": ""}],
        question="观光电梯交付周期", keyword="观光电梯交付周期",
    )
    block2 = build_primary_advantage_block("甲品牌", "观光电梯交付周期", mixed)
    assert "不作主优势" in block2 and "完全无关的另一件事情说明" in block2
