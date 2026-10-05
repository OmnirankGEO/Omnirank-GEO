"""工单 B(飞轮真接管 + 开闸 + 血缘回填)的判别测试。

每条都按"若我的结论错了,这里会看到什么"来写 —— 恒真断言在这批里没有意义:
B5 的三条边界、B4 的"不传 = 零变化"、回填的幂等与不猜,每一条都能被一次变异打红。
"""
from __future__ import annotations

import asyncio

import pytest

from services.flywheel_writing_strategy_choice import apply_writing_style_choice

# 六文体家族里对任何行业都合法的一个(不是榜单类,不触发医疗/法律 hard rule)。
SAFE_FAMILY = "implementation_guide"
# `resolve_user_choice` 明确拒绝的选项(停用的 company / 契约外的家族),
# 用它们验"飞轮让路"。
#
# 口径事实(读过 writing/style_registry.resolve_user_choice 才敢这么写):
# 六家族经 FAMILY_TO_GENERATION_STYLE 全部落在非榜单 style 上,
# **当前没有任何家族码能触发医疗/法律榜单 hard rule**。所以边界③在今天是
# 纵深防御(防映射表日后变动),真正会被拒的是下面这两类。不装作它今天就在拦榜单。
REJECTED_CHOICES = ("company", "not_a_real_family")


def _llm_choice(family=SAFE_FAMILY, **extra):
    return {"style_family": family, "source": "llm", "angle": "", "reason": "", **extra}


def _topics(*choices):
    return [{"id": i, "user_choice": c, "title": f"t{i}"} for i, c in enumerate(choices, 1)]


# ============================================================
# B5 真接管:三条边界
# ============================================================

def test_b5_takes_over_auto_topics():
    """auto 档 + source=llm → 飞轮的家族真的落到 topic 上(这就是"真接管"本身)。"""
    topics = _topics("auto", "auto")
    out = apply_writing_style_choice(topics, _llm_choice(), "家居建材")

    assert out["applied_topics"] == 2
    assert out["family"] == SAFE_FAMILY
    assert out["skip_reason"] is None
    assert [t["user_choice"] for t in topics] == [SAFE_FAMILY, SAFE_FAMILY]
    assert all(t["_style_from_flywheel"] for t in topics)


def test_b5_never_overrides_explicit_user_choice():
    """边界①:用户 > AI。前端/DB 显式选过的 topic 一个字都不许改。"""
    topics = _topics("auto", "evidence_qa", "company_facts")
    out = apply_writing_style_choice(topics, _llm_choice(), "家居建材")

    assert out["applied_topics"] == 1
    assert [t["user_choice"] for t in topics] == [SAFE_FAMILY, "evidence_qa", "company_facts"]
    # 没被接管的 topic 不许被打飞轮标记,否则效果归因会把用户选的算到飞轮头上。
    assert "_style_from_flywheel" not in topics[1]
    assert "_style_from_flywheel" not in topics[2]


@pytest.mark.parametrize("choice", [
    None,                                          # 判断层整个不可用
    {"style_family": SAFE_FAMILY, "source": "rule"},  # 判断点关闸 / 模型全挂 → 规则兜底
    {"style_family": "", "source": "llm"},         # 模型没选出家族
])
def test_b5_does_not_touch_topics_without_llm_choice(choice):
    """边界②:只认 source=llm。关闸/兜底时 topics 与改前逐字节一致 —— 这就是零风险回退。"""
    topics = _topics("auto", "auto")
    before = [dict(t) for t in topics]

    out = apply_writing_style_choice(topics, choice, "家居建材")

    assert out["applied_topics"] == 0
    assert out["family"] == ""
    assert out["skip_reason"]  # 必须说明为什么没接管,不能静默跳过
    assert topics == before


@pytest.mark.parametrize("bad_family", REJECTED_CHOICES)
def test_b5_yields_when_style_registry_rejects_the_family(bad_family):
    """边界③:`resolve_user_choice` 拒绝的家族一律不接管,退回 ratio 抽签。

    改前 auto 档走 ratio 抽签永远不会 raise;如果把一个被拒绝的值硬塞进 user_choice,
    `_allocate_style_from_ratios` 会按既定口径上抛 ValueError,**整批文章 fail**。
    所以这里必须是"不接管",不是"接管了再祈祷下游宽容"。
    """
    from writing.style_registry import resolve_user_choice

    with pytest.raises(ValueError):
        resolve_user_choice(bad_family, "医疗健康")  # 前提成立才谈得上让路

    topics = _topics("auto", "auto")
    out = apply_writing_style_choice(topics, _llm_choice(bad_family), "医疗健康")

    assert out["applied_topics"] == 0
    assert out["skip_reason"].startswith("rejected:")
    assert [t["user_choice"] for t in topics] == ["auto", "auto"]


def test_b5_applies_every_contract_family_without_blowing_up_generation():
    """六家族逐一验:接管后的 user_choice 必须能被下游 resolve 成合法 style_code。

    这是"飞轮不许把一批文章判死"的正面证明 —— 只测被拒的那条路等于只测了一半。
    """
    from writing.article_style_contract import USER_CHOICE_FAMILY_CODES
    from writing.style_registry import resolve_user_choice

    for family in USER_CHOICE_FAMILY_CODES:
        topics = _topics("auto")
        out = apply_writing_style_choice(topics, _llm_choice(family), "家居建材")
        assert out["applied_topics"] == 1, f"{family} 未被接管"
        assert resolve_user_choice(topics[0]["user_choice"], "家居建材")


def test_b5_reports_no_auto_topic_instead_of_pretending_applied():
    """全批都是用户显式选择时,applied 必须是 0 且有理由 —— 不许报成"已生效"。"""
    topics = _topics("evidence_qa", "trend_policy_risk")
    out = apply_writing_style_choice(topics, _llm_choice(), "家居建材")

    assert out["applied_topics"] == 0
    assert out["skip_reason"] == "no_auto_topic"


def _function_source(rel_path: str, func_name: str) -> str:
    """按源码文本取某个函数体。

    刻意**不 import** server / diagnosis_workflow:这两个模块导入期就连库、起调度,
    在纯静态检查里把 DB 拖进来只会让这条断言变成"环境测试"。
    """
    import ast
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[2] / rel_path
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            lines = text.splitlines()
            return "\n".join(lines[node.lineno - 1:node.end_lineno])
    raise AssertionError(f"{rel_path} 里找不到 {func_name}")


def test_server_records_applied_flag_in_assignment_metadata():
    """§2 验收 2:落账必须带 applied —— 否则"选了但没生效"和"选了并写出来了"分不开。"""
    import pathlib

    source = (
        pathlib.Path(__file__).resolve().parents[2] / "server.py"
    ).read_text(encoding="utf-8")

    assert "style_choice_applied" in source
    assert "apply_writing_style_choice" in source, "B5 接管没接进 server 的选题解析链"


# ============================================================
# B4 接主链:不传 = 零变化
# ============================================================

def test_b4_material_block_empty_without_material():
    from tools.keyword_generator import _render_flywheel_material_block

    assert _render_flywheel_material_block(None) == ""
    assert _render_flywheel_material_block({}) == ""
    # 有 key 但全是空值也算没素材(否则 prompt 里会出现一个空标题块)
    assert _render_flywheel_material_block(
        {"question_seeds": ["", "  "], "competitor_seeds": [None]}
    ) == ""


def test_b4_material_block_renders_seeds_as_reference_only():
    from tools.keyword_generator import _render_flywheel_material_block

    block = _render_flywheel_material_block({
        "competitor_seeds": ["甲公司", "乙公司"],
        "question_seeds": ["装修全包一般多少钱", "旧房翻新要注意什么"],
    })

    assert "甲公司" in block and "装修全包一般多少钱" in block
    # 素材不是结论:必须显式写"可以不用" + 禁止原样拼名录(那正是被修掉的 bug 形状)
    assert "仅供参考" in block
    assert "不是结论" in block
    assert "原样拼进问题里" in block


def test_b4_prompt_is_byte_identical_without_material(monkeypatch):
    """真正跑一遍 analyze_client_business,比对两次 user_prompt。

    不传素材时必须与改前完全一致 —— 关闸回归靠的是这条,不是靠"应该没影响"。
    """
    import tools.keyword_generator as kg

    captured: list[str] = []

    class _FakeResponse:
        status_code = 500
        text = "stub"

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured.append(json["messages"][1]["content"])
            return _FakeResponse()

    monkeypatch.setenv("DASHSCOPE_API_KEY", "stub-key")
    monkeypatch.setattr(kg.httpx, "AsyncClient", _FakeClient)

    async def _run(material):
        return await kg.analyze_client_business(
            brand_name="某品牌", industry="装修", keywords=["装修"],
            client_location="深圳", business_scope="regional",
            flywheel_material=material,
        )

    asyncio.run(_run(None))
    asyncio.run(_run({"competitor_seeds": ["甲公司"], "question_seeds": ["装修多少钱"]}))

    assert len(captured) == 2
    without, with_material = captured
    assert "【本行业已有沉淀" not in without
    assert "甲公司" in with_material
    # 素材块之外的部分必须一字不改
    assert with_material.replace(
        kg._render_flywheel_material_block(
            {"competitor_seeds": ["甲公司"], "question_seeds": ["装修多少钱"]}
        ), ""
    ) == without


def test_b4_is_wired_into_diagnosis_main_chain():
    """B4 必须真接在诊断主链上,并且判断点关闸时根本不调用(连库都不查)。

    🔴 [#147-B · 2026-09-07] 飞轮取素材搬进了共享的
       `services/diagnosis_generator_inputs.brand_side_generator_inputs`
       (预览端与实跑同源)。本条钉的**三条性质一条没变**,只是散在两个文件里,
       所以判据必须**沿着链走完**:

           run_diagnosis_workflow → brand_side_generator_inputs → suggest_reusable_material

       少走一段就成空壳:只查 helper 里有那串,helper 没人调也照样绿。
    """
    source = _function_source("workflows/diagnosis_workflow.py", "run_diagnosis_workflow")
    helper = _function_source("services/diagnosis_generator_inputs.py",
                              "brand_side_generator_inputs")
    gate = _function_source("services/diagnosis_generator_inputs.py",
                            "_flywheel_material")

    # ① 主链真的调了那一份(链的第一段)——这段断了,下面两条就都是空壳
    assert "brand_side_generator_inputs" in source, (
        "诊断主链没调共享的品牌侧输入 → B4 又只剩 health API 那条路")
    # ② 素材真的被取(链的第二段),且判断点关闸时根本不调用
    assert "suggest_reusable_material" in gate, "B4 没进取素材那一步"
    assert "judgment_enabled" in gate, "B4 调用没有被判断点开关守住 → 关闸时行为不一致"
    # ③ 素材真的喂给出题步骤:helper 产出它,主链原样展开给生成器
    assert '"flywheel_material"' in helper, "共享那份没产出飞轮素材"
    assert "**_brand_side" in source, "素材没真喂给出题步骤"


# ============================================================
# §4 血缘回填:幂等 + 不猜
# ============================================================

def test_backfill_only_writes_null_rows():
    """幂等的来源:两档 SQL 都必须带 article_id IS NULL —— 少一处就会重写已有血缘。"""
    from services import geo_source_signal_lineage_backfill as bf

    for sql in (bf._SQL_EXACT, bf._SQL_URL_ONLY):
        assert sql.count("article_id IS NULL") >= 2, "选行与写回都要挡,防并发下重写"


def test_backfill_exact_phase_uses_production_unique_key():
    """生产真实唯一键是 (url_hash, primary_industry),不是建表语句写的 url_hash 单键。

    只按 url_hash 盲 JOIN 会在同 hash 多行的组上随机挑一篇,把血缘地基浇歪。
    """
    from services import geo_source_signal_lineage_backfill as bf

    assert "a.primary_industry = s.industry_key" in bf._SQL_EXACT


def test_backfill_url_only_phase_refuses_ambiguous_hash():
    """行业对不上时只认"全库该 hash 唯一一篇";一 hash 多篇宁可留空也不猜。"""
    from services import geo_source_signal_lineage_backfill as bf

    assert "HAVING COUNT(*) = 1" in bf._SQL_URL_ONLY


class _FakeCursor:
    """按批返回 rowcount 的假游标,用来验分批循环真的会收敛。"""

    def __init__(self, affected_per_batch):
        self._plan = list(affected_per_batch)
        self.calls = 0
        self.rowcount = 0

    def execute(self, sql, params=None):
        self.calls += 1
        self.rowcount = self._plan.pop(0) if self._plan else 0


def test_backfill_phase_stops_on_partial_batch():
    """一批没吃满 = 已经没得写了,必须立刻停,不能空转到 max_batches。"""
    from services.geo_source_signal_lineage_backfill import _run_phase

    cur = _FakeCursor([100, 100, 37])
    written, batches = _run_phase(cur, "SQL", batch_size=100, max_batches=50)

    assert (written, batches) == (237, 3)
    assert cur.calls == 3


def test_backfill_phase_respects_max_batches():
    """批次上限是保险丝:用满就停,交给明晚那一趟继续(不许无限循环)。"""
    from services.geo_source_signal_lineage_backfill import _run_phase

    cur = _FakeCursor([100] * 20)
    written, batches = _run_phase(cur, "SQL", batch_size=100, max_batches=4)

    assert batches == 4
    assert written == 400


# ============================================================
# B5 返工(Review-CTO 2026-07-27):接管落"家族方向",家族内形态按 ratio 抽
# 根因:resolve_user_choice 把 multi_brand_comparison 恒落单一 comparison_review,
# 接管批次永远不出榜单文(ranking_v2),与工单 A 的榜单深档修复正面对冲。
# ============================================================

def _service():
    from writing.article_generator_service import ArticleGeneratorService

    return ArticleGeneratorService(quote_id=0, brand_name="测试品牌", industry="家居建材")


def _flywheel_topic(user_choice="multi_brand_comparison"):
    return {"id": 1, "user_choice": user_choice, "_style_from_flywheel": True, "title": "t"}


@pytest.fixture()
def _fixed_ratios(monkeypatch):
    """固定配比,让抽签断言确定化:比较家族内 ranking_v2/comparison_review 都有权重。"""
    import config.settings_manager as sm

    monkeypatch.setattr(
        sm, "get_effective_style_ratios",
        lambda industry, unit="fraction": {
            "ranking_v2": 0.2, "comparison_review": 0.32, "qa_recommendation": 0.14,
        },
    )


def test_flywheel_takeover_batch_can_still_produce_ranking_style(_fixed_ratios):
    """🔴 返工判别锁:接管批次必须仍能产出榜单形态,且是分布不是单点。

    变异区分度:删掉 _allocate 里的 flywheel-family 分支 → 恒落 comparison_review,
    seen 里永远不会出现 ranking_v2 → 本测试转红。
    """
    svc = _service()
    seen = {
        svc._allocate_style_from_ratios(_flywheel_topic(), "家居建材")
        for _ in range(300)
    }
    assert "ranking_v2" in seen, "接管批次抽不出榜单文 —— 家族内分布抽签没生效"
    assert "comparison_review" in seen, "家族内应是分布抽签,不是把单点从一个换成另一个"
    assert "qa_recommendation" not in seen, "抽签越出了所选家族的边界"


def test_flywheel_takeover_respects_medical_legal_ranking_ban(_fixed_ratios):
    """医疗/法律 hard rule 在飞轮路径同样生效:家族内抽签排除榜单类。"""
    svc = _service()
    seen = {
        svc._allocate_style_from_ratios(_flywheel_topic(), "医疗健康")
        for _ in range(300)
    }
    assert "ranking_v2" not in seen and "authority_ranking" not in seen, \
        "飞轮路径放宽了医疗/法律禁榜单红线"


def test_explicit_user_family_choice_behavior_is_untouched(_fixed_ratios):
    """无 _style_from_flywheel 标记(用户显式选家族)→ 既有单一映射行为一个字不变。"""
    svc = _service()
    topic = {"id": 1, "user_choice": "multi_brand_comparison", "title": "t"}
    seen = {svc._allocate_style_from_ratios(topic, "家居建材") for _ in range(50)}
    assert seen == {"comparison_review"}, "用户显式选择的既有行为被返工波及了"
