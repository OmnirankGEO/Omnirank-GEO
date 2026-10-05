"""[工单 span 级 AI 免费修复 2026-07-30] §7 八条判别锁 · **全部行为级**。

判别原则(本仓踩过多次"源码串断言换皮即绕"/"锁绿而实际未达成"):
- 不做任何源码串断言。每条锁都**真跑修复链**:锁 1/2/3/5/8 直接调
  `services.writing_span_repair.repair_finding_span`(它是生产端点用的同一个入口,
  底线类 code 由它路由到 span 级引擎);锁 6/7 调**真端点函数**
  `server.api_repair_article_finding`,连真库(生产 pg_dump --schema-only 还原的表)。
- 每条 finding 的 matched_text 都由**真判定函数** `evaluate_content_trust` 产出,
  不是手写字符串 —— 判定改了这里会跟着红。
- LLM 全部替身,但替身的**返回值形态取自真实失败模式**(顺手扩写 / 补新数字 /
  塞结构 / 换行 / 诚实拒绝),不是"随便造一个字符串"。

跑法(需要一个**一次性空库**,不能用共享测试库:
`CREATE TABLE IF NOT EXISTS` 在非空库上会静默拿到别人的表形状):
    docker run -d --name omnirank-spanfix-pg -e POSTGRES_PASSWORD=... \
        -e POSTGRES_DB=geo_spanfix_test -p 15439:5432 pgvector/pgvector:pg16
    docker exec -i omnirank-spanfix-pg psql -U postgres -d geo_spanfix_test \
        -v ON_ERROR_STOP=1 < tests/fixtures/span_repair_prod_schema_2026_07_30.sql
    # .env 里 TEST_DATABASE_URL 指向它,然后:
    python -m pytest tests/test_span_level_ai_repair_2026_07_30.py -q
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import types

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from services import span_level_repair as engine  # noqa: E402
from services.writing_span_repair import repair_finding_span  # noqa: E402
from writing.evidence_first_policy import evaluate_content_trust  # noqa: E402


# ══════════════════════════════════════════════════════════════════════════
# 夹具:正文 + 由真判定产出的 finding
# ══════════════════════════════════════════════════════════════════════════
BODY_NUMBER = (
    "## 客户反馈\n\n"
    "内部口径下客户满意度提升 12%，团队据此调整了服务流程。\n\n"
    "另有一段与本处无关的正文，用于验证隔离是否真的成立。\n"
)
BODY_SCORE = (
    "## 评价方式\n\n"
    "我们在综合评分维度上给出结论，供采购参考。\n\n"
    "另一段与本处无关的正文。\n"
)
BODY_ABSOLUTE = (
    "## 说明\n\n"
    "它是行业第一的板材品牌，价格处于中等区间。\n\n"
    "另一段与本处无关的正文。\n"
)
BODY_MEDICAL = (
    "## 结论\n\n"
    "该疗法对慢性疾病的疗效行业公认，可以放心长期使用。\n\n"
    "另一段与本处无关的正文。\n"
)


def real_finding(body: str, code: str) -> dict:
    """从**真判定函数**里取这条 finding(含 matched_text),不手写字符串。"""
    trust = evaluate_content_trust("", body, evidence_mode="unknown")
    for item in tuple(trust.hard) + tuple(trust.soft):
        if item.code == code:
            assert item.matched_text, f"{code} 缺 matched_text → span 级永远点不出来"
            return {
                "code": item.code,
                "message": item.message,
                "matched_text": item.matched_text,
            }
    raise AssertionError(f"夹具没有触发 {code}(判定口径变了,夹具必须跟着改)")


def run_repair(body: str, finding: dict, reply: str, **kwargs) -> dict:
    calls: list[str] = []

    async def _llm(prompt: str) -> str:
        calls.append(prompt)
        return reply

    out = asyncio.run(repair_finding_span(body, finding, _llm, **kwargs))
    out["_prompts"] = calls
    return out


# ══════════════════════════════════════════════════════════════════════════
# 锁 1 · span 级隔离:修复后正文除该 span 外逐字节相同
# ══════════════════════════════════════════════════════════════════════════
def test_lock1_only_that_span_changes_byte_for_byte():
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    located = engine.locate_span(BODY_NUMBER, finding["matched_text"])
    assert located is not None
    # span 必须是"那一句",不是整段(整段级是本单要替换掉的旧行为)
    assert located["span"] == "内部口径下客户满意度提升 12%，团队据此调整了服务流程。"

    out = run_repair(
        BODY_NUMBER, finding,
        "内部口径下客户满意度有明显改善，团队据此调整了服务流程。",
    )
    assert out["ok"], out["reason"]
    new = out["content"]
    start, end = located["start"], located["end"]
    after = out["span_after"]
    # 逐字节:左段 / 右段与原文完全一致,中间正好是新片段
    assert new.encode("utf-8")[:len(BODY_NUMBER[:start].encode("utf-8"))] == BODY_NUMBER[:start].encode("utf-8")
    assert new[start:start + len(after)] == after
    assert new[start + len(after):].encode("utf-8") == BODY_NUMBER[end:].encode("utf-8")
    # 无关那一段一字未动
    assert "另有一段与本处无关的正文，用于验证隔离是否真的成立。" in new


def test_lock1_isolation_check_has_discriminating_power():
    """校验器本身必须能抓住"动了 span 以外"——否则锁 1 是假绿。

    刻意构造一个**长度相同、结构相同**的 candidate,只有"span 外字节被改"这一项
    不同:除了 verify_span_isolation,没有别的守卫拦得住它。
    """
    original = "甲。乙。丙。"
    located = engine.locate_span(original, "乙")
    assert located is not None
    good = engine.splice_span(original, located["start"], located["end"], "戊。")
    tampered = good.replace("丙。", "丁。")
    assert engine.verify_span_isolation(
        original, good, located["start"], located["end"], "戊。",
    ) is True
    assert engine.verify_span_isolation(
        original, tampered, located["start"], located["end"], "戊。",
    ) is False
    # 长度/结构两道守卫对 tampered 都是绿的 → 只有锁 1 拦得住
    assert engine.check_length(located["span"], "戊。") is True
    assert engine.structure_fingerprint(original) == engine.structure_fingerprint(tampered)


# ══════════════════════════════════════════════════════════════════════════
# 锁 2 · 结构不变(标题/列表/表格/段落 修复前后一致)
# ══════════════════════════════════════════════════════════════════════════
def test_lock2_structure_change_is_rejected():
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    # 单行、长度合规、命中串已去掉、无新数字 —— 唯一问题是塞进了表格管道符
    out = run_repair(BODY_NUMBER, finding, "内部口径下客户满意度改善|团队据此调整流程。")
    assert out["ok"] is False
    assert out["reason"] == "repair_structure_changed"
    assert out["content"] == BODY_NUMBER  # 正文一字未动

    # 换行型(把一句拆成新的标题行)同样过不去
    out2 = run_repair(BODY_NUMBER, finding, "内部口径下满意度改善。\n## 新标题")
    assert out2["ok"] is False
    assert out2["reason"] == "repair_span_multiline"
    assert out2["content"] == BODY_NUMBER


# ══════════════════════════════════════════════════════════════════════════
# 锁 3 · 长度约束
# ══════════════════════════════════════════════════════════════════════════
def test_lock3_length_band_rejects_padding():
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    out = run_repair(
        BODY_NUMBER, finding,
        "内部口径下客户满意度出现了较为明显的改善，团队据此重新梳理并调整了整个服务"
        "流程与回访机制，后续还会持续跟进并定期复盘整体表现。",
    )
    assert out["ok"] is False
    assert out["reason"] == "repair_span_length_out_of_band"
    assert out["content"] == BODY_NUMBER


def test_lock3_band_is_the_corrected_one_not_plus_minus_20pct():
    """[执行方订正 2026-07-30] 工单 §4 的 ±20% 与 §3.4 自己的示例互相矛盾:
    短 span 换条件化表述必然超 20%。订正后的带宽必须**同时**满足:
    §3.4 示例过得去、明显扩写过不去。"""
    before = "综合评分 92 分，位列第一梯队。"
    after = "在预算充足且需要深度定制的场景下更为匹配。"
    assert engine.check_length(before, after) is True, "工单 §3.4 自己的示例必须能过"
    assert engine.check_length(before, after + "此外我们也建议同时比较三家以上供应商的报价与工期。") is False
    assert engine.check_length(before, "更匹配。") is False, "缩到不成句也不该放行"


# ══════════════════════════════════════════════════════════════════════════
# 锁 4 · 重判放行:修完重跑**同一判定函数**,仍红不落库
# ══════════════════════════════════════════════════════════════════════════
def test_lock4_rejudge_blocks_cosmetic_rewrite():
    finding = real_finding(BODY_SCORE, "manufactured_score")
    assert finding["matched_text"] == "综合评分"
    # "综合得分" 换皮:命中串没了、长度结构数字全过 —— 只有重判拦得住
    out = run_repair(BODY_SCORE, finding, "我们在综合得分维度上给出结论，供采购参考。")
    assert out["ok"] is False
    assert out["reason"] == "still_violating"
    assert out["content"] == BODY_SCORE
    # 反向:真改掉了评分体系 → 重判转绿 → 放行
    ok = run_repair(
        BODY_SCORE, finding,
        "我们按预算与工期两个条件给出结论，供采购参考。",
    )
    assert ok["ok"] is True, ok["reason"]
    assert "综合评分" not in ok["content"]


def test_lock4_rejudge_uses_the_same_judge_function():
    """重判不是第二份口径:它就是判定链自己的函数。
    把判定函数替身成"永远仍违规",修复必须转红(证明真的走了它)。"""
    finding = real_finding(BODY_SCORE, "manufactured_score")
    import services.writing_span_repair as wsr

    original = wsr.still_violates
    try:
        wsr.still_violates = lambda *a, **k: True
        out = run_repair(
            BODY_SCORE, finding,
            "我们按预算与工期两个条件给出结论，供采购参考。",
        )
        assert out["ok"] is False and out["reason"] == "still_violating"
    finally:
        wsr.still_violates = original


# ══════════════════════════════════════════════════════════════════════════
# 锁 5 · CANNOT_FIX_WITHOUT_FABRICATION → 零改动转人工
# ══════════════════════════════════════════════════════════════════════════
def test_lock5_cannot_fix_lands_nothing():
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    out = run_repair(BODY_NUMBER, finding, engine.CANNOT_FIX_MARKER)
    assert out["ok"] is False
    assert out["reason"] == "cannot_fix_without_fabrication"
    assert out["content"] == BODY_NUMBER
    # 即便模型在标记前后加了废话,也照样零改动(不许被"顺带修一点"绕过)
    out2 = run_repair(BODY_NUMBER, finding, f"抱歉。{engine.CANNOT_FIX_MARKER}")
    assert out2["ok"] is False and out2["content"] == BODY_NUMBER


def test_lock5_prompt_actually_offers_the_honest_exit():
    """不给诚实出口,模型会为了完成任务补一个新编造 —— 提示词里必须真有这句。"""
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    out = run_repair(BODY_NUMBER, finding, "内部口径下客户满意度有所改善，团队据此调整了流程。")
    prompt = out["_prompts"][0]
    assert engine.CANNOT_FIX_MARKER in prompt
    # 形状约束:只给 span + 只读上下文,不给整篇
    assert "[待替换片段]" in prompt and "[只读上文]" in prompt
    assert "另有一段与本处无关的正文" not in prompt, "上下文窗口外的正文不许进提示词"


# ══════════════════════════════════════════════════════════════════════════
# 锁 8 · 无来源时不得出现新数字(有来源那一路才准留数字)
# ══════════════════════════════════════════════════════════════════════════
def test_lock8_no_new_number_without_evidence():
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    out = run_repair(BODY_NUMBER, finding, "内部口径下客户满意度提升约 9%，团队据此调整流程。")
    assert out["ok"] is False
    assert out["reason"] == "repair_introduced_number"
    assert out["content"] == BODY_NUMBER


def test_lock8_number_must_disappear_when_no_source():
    """§3.2:无来源 = 删掉数字。原样留着数字(只改措辞)必须被拒。"""
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    out = run_repair(BODY_NUMBER, finding, "内部统计里客户满意度提升 12%，服务流程已调整。")
    assert out["ok"] is False
    assert out["reason"] == "repair_violation_text_remains"
    assert out["content"] == BODY_NUMBER


def test_lock8_with_real_source_keeps_the_number():
    """§3.3:证据包里能对上**同一个数字** → 走"补出处"那一路,数字准留。
    对不上的证据不许进提示词(否则模型张冠李戴,比不修更糟)。"""
    pack = {"items": [{
        "evidence_id": "E1",
        "url": "https://example.gov.cn/report",
        "claim": "抽样回访中客户满意度提升 12%",
        "excerpt": "抽样回访中客户满意度提升 12%",
        "title": "行业服务质量年度报告",
        "publisher": "中国某行业协会",
        "published_at": "2026-01-15",
        "verification_status": "official_record",
        # 🔴 provenance 必须备齐:official_record 少了 official_record_id 会被
        # _verified_provenance_complete 静默判成"未核验",于是整条锁假绿
        # (本仓踩过同型:VERIFIED 态 provenance 不全被悄悄降级)。
        "official_record_id": "GOV-2026-0115-001",
        "relationship": "support",
    }]}
    finding = {**real_finding(BODY_NUMBER, "unsourced_outcome_number"), "evidence_pack": pack}
    hint = engine.build_source_hint(pack, "客户满意度提升 12%", engine.CATEGORY_FABRICATED_NUMBER)
    assert "行业服务质量年度报告" in hint
    # 数字对不上的证据 → 不给来源(不许诱导模型硬挂一个来源)
    assert engine.build_source_hint(pack, "客户满意度提升 30%", engine.CATEGORY_FABRICATED_NUMBER) == ""

    out = run_repair(
        BODY_NUMBER, finding,
        "据中国某行业协会 2026 年报告，客户满意度提升 12%。",
    )
    assert out["ok"] is True, out["reason"]
    assert "12%" in out["span_after"], "有来源那一路必须保留原数字"
    assert "行业服务质量年度报告" in out["_prompts"][0]

    # 🔴 "有来源可以多出数字"不是开口子:多出来的每个数字仍必须在证据里对得上。
    faked = run_repair(
        BODY_NUMBER, finding,
        "据中国某行业协会 2030 年报告，客户满意度提升 12%。",
    )
    assert faked["ok"] is False
    assert faked["reason"] == "repair_introduced_number"
    assert faked["content"] == BODY_NUMBER


# ══════════════════════════════════════════════════════════════════════════
# 锁 6 · 高风险类(医疗/法律/金融)不给 AI 修复出口
# ══════════════════════════════════════════════════════════════════════════
def test_lock6_high_risk_never_calls_the_model():
    finding = real_finding(BODY_MEDICAL, "anonymous_authority")
    out = run_repair(
        BODY_MEDICAL, finding, "不应该被调用",
        industry="医疗健康", title="慢性病疗法怎么选",
    )
    assert out["ok"] is False
    assert out["reason"] in ("high_risk_article", "high_risk_span")
    assert out["content"] == BODY_MEDICAL
    assert out["_prompts"] == [], "高风险类连模型都不该调(调了就等于给它软化措辞的机会)"


def test_lock6_route_and_findings_annotation_agree():
    """前端渲染依据(findings_aggregate 的 ai_repairable)与端点拒绝依据是同一个
    repair_route —— 两份口径就会出现"按钮点得到但端点拒"的假出口。"""
    from services.article_findings_aggregate import aggregate_article_findings

    qw = {"evidence": {"soft": [{
        "code": "anonymous_authority",
        "severity": "soft",
        "message": "借匿名权威背书",
        "matched_text": "行业公认",
    }]}}
    normal = aggregate_article_findings(qw, industry="建材家居", title="板材怎么选")
    assert normal[0]["spans"][0]["ai_repairable"] is True
    assert normal[0]["ai_repairable_count"] == 1

    risky = aggregate_article_findings(qw, industry="医疗健康", title="慢性病疗法怎么选")
    assert risky[0]["spans"][0]["ai_repairable"] is False
    assert risky[0]["spans"][0]["ai_repair_block_reason"] == "high_risk_article"
    assert risky[0]["ai_repairable_count"] == 0, "高风险类的「一键修复本类」也不许出现"
    # 条数一条不少(只改出口,不改判定/不藏 finding)
    assert risky[0]["count"] == normal[0]["count"] == 1


def test_lock6_high_risk_vocabulary_is_the_single_ssot():
    """高风险词表只有一份:改 geo_article_expert 的词表,修复门必须跟着变。"""
    import writing.geo_article_expert as expert

    assert engine.article_ai_repair_blocked("医疗健康", "x") is True
    assert engine.article_ai_repair_blocked("建材家居", "板材怎么选") is False
    original = expert._HIGH_RISK_RE
    try:
        import re as _re
        expert._HIGH_RISK_RE = _re.compile("板材")
        assert engine.article_ai_repair_blocked("建材家居", "板材怎么选") is True
    finally:
        expert._HIGH_RISK_RE = original


# ══════════════════════════════════════════════════════════════════════════
# 锁 6/7 端点层 · 真库 + 真 handler
# ══════════════════════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def srv():
    import server as srv_mod

    srv_mod._require_article_access = lambda *a, **k: None  # RBAC 不在本单范围
    return srv_mod


@pytest.fixture
def make_article(srv):
    from db.diagnosis_db import get_connection

    created: list[int] = []

    def _make(body: str, *, industry: str = "建材家居", title: str = "板材怎么选") -> int:
        conn = get_connection()
        try:
            c = conn.cursor()
            c.execute(
                "INSERT INTO quotes (brand_name, industry, status) VALUES (%s,%s,'draft') RETURNING id",
                ("测试品牌", industry),
            )
            quote_id = c.fetchone()["id"]
            c.execute(
                "INSERT INTO topics (quote_id, optimized_title, status) VALUES (%s,%s,'completed') RETURNING id",
                (quote_id, title),
            )
            topic_id = c.fetchone()["id"]
            c.execute(
                "INSERT INTO articles (topic_id, quote_id, title, content, publication_profile) "
                "VALUES (%s,%s,%s,%s,'standard') RETURNING id",
                (topic_id, quote_id, title, body),
            )
            article_id = c.fetchone()["id"]
            c.execute("UPDATE topics SET article_id=%s WHERE id=%s", (article_id, topic_id))
            conn.commit()
            created.append(article_id)
            return article_id
        finally:
            conn.close()

    yield _make


def _fake_llm(srv, reply: str) -> list[str]:
    """把端点内部真正会用的 MultiLLMCaller 替身掉,并记录调用次数。"""
    calls: list[str] = []

    class _Caller:
        def __init__(self, *a, **k):
            pass

        async def call(self, prompt, verbose=False):
            calls.append(prompt)
            return reply, "fake"

    module = sys.modules.get("tools.multi_llm_caller")
    if module is None:
        import tools.multi_llm_caller as module  # noqa: F401
        module = sys.modules["tools.multi_llm_caller"]
    module.MultiLLMCaller = _Caller
    return calls


def _read_content(article_id: int) -> tuple[str, dict]:
    from db.diagnosis_db import get_connection

    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT content, quality_warning FROM articles WHERE id=%s", (article_id,))
        row = c.fetchone()
        return row["content"], (row["quality_warning"] or {})
    finally:
        conn.close()


def _post(srv, article_id: int, code: str, matched_text: str):
    payload = srv.ArticleFindingRepairRequest(finding_code=code, matched_text=matched_text)
    request = types.SimpleNamespace(state=types.SimpleNamespace(user={"id": 1}))
    return asyncio.run(srv.api_repair_article_finding(article_id, payload, request))


def test_lock6_endpoint_refuses_high_risk_article(srv, make_article):
    article_id = make_article(BODY_MEDICAL, industry="医疗健康", title="慢性病疗法怎么选")
    calls = _fake_llm(srv, "不应该被调用")
    finding = real_finding(BODY_MEDICAL, "anonymous_authority")

    res = _post(srv, article_id, finding["code"], finding["matched_text"])
    assert res["success"] is False
    assert res["code"] == "HIGH_RISK_NO_AI_REPAIR"
    action_ids = {a["id"] for a in res["actions"]}
    assert "ai_fix_this_span" not in action_ids, "锁 6:高风险类不许再给 AI 修复出口"
    assert action_ids == {"request_human_review", "edit_manually"}
    assert calls == [], "高风险类不许调模型"
    content, _ = _read_content(article_id)
    assert content == BODY_MEDICAL


BODY_MEDICAL_RANKING = (
    "## 慢性疾病疗法怎么选\n\n"
    "下面按疗效梳理三家机构。\n\n"
    "1. 甲康医疗科技\n"
    "2. 乙安健康服务\n"
    "3. 丙泰医疗集团\n\n"
    "以上排序仅供参考。\n"
)


def test_lock6_endpoint_is_the_only_guard_for_legacy_path(srv, make_article):
    """🔴 端点层高风险拒绝**不是冗余**:非底线类 code(这里 ordered_brand_candidates)
    走的是既有**整段级**旧路径,那条路径里没有 span 引擎的 route 检查 ——
    端点这一道是唯一拦得住"让 AI 重写一整段医疗正文"的地方。
    (本条是变异 ⑤c 从 SURVIVED 变红的用例:没有它,端点这层删掉也全绿。)"""
    article_id = make_article(
        BODY_MEDICAL_RANKING, industry="医疗健康", title="慢性病疗法怎么选",
    )
    calls = _fake_llm(srv, "1. 甲康医疗科技(按公开资质排序)")
    finding = real_finding(BODY_MEDICAL_RANKING, "ordered_brand_candidates")
    assert finding["code"] not in engine.BOTTOMLINE_CATEGORIES, "夹具必须是走旧整段路径的 code"

    res = _post(srv, article_id, finding["code"], finding["matched_text"])
    assert res["success"] is False
    assert res["code"] == "HIGH_RISK_NO_AI_REPAIR"
    assert calls == [], "高风险文章连整段级旧路径也不许调模型"
    content, _ = _read_content(article_id)
    assert content == BODY_MEDICAL_RANKING


def test_lock7_free_quota_two_then_refuse_with_zero_charge(srv, make_article):
    article_id = make_article(BODY_NUMBER)
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    # 每次都让模型返回一个"仍留着数字"的版本 → 必然失败,但**额度照扣**
    # (一次尝试就是一次平台成本;§5"再修 1 次"就是这个语义)
    calls = _fake_llm(srv, "内部统计里客户满意度提升 12%，服务流程已调整。")

    first = _post(srv, article_id, finding["code"], finding["matched_text"])
    assert first["success"] is False and first["code"] == "SPAN_REPAIR_FAILED"
    second = _post(srv, article_id, finding["code"], finding["matched_text"])
    assert second["success"] is False and second["code"] == "SPAN_REPAIR_FAILED"
    assert len(calls) == 2

    third = _post(srv, article_id, finding["code"], finding["matched_text"])
    assert third["success"] is False
    assert third["code"] == "SPAN_REPAIR_QUOTA_EXHAUSTED", "第 3 次必须被拒"
    assert third["free_repairs_limit"] == engine.FREE_REPAIRS_PER_FINDING == 2
    assert len(calls) == 2, "被拒的那次不许再烧一次模型"
    action_ids = {a["id"] for a in third["actions"]}
    assert "ai_fix_this_span" not in action_ids and "request_human_review" in action_ids

    content, qw = _read_content(article_id)
    assert content == BODY_NUMBER, "三次全失败 → 正文一字未动"
    quota = qw.get("span_repair_quota") or {}
    fingerprint = engine.finding_fingerprint(finding["code"], finding["matched_text"])
    assert quota[fingerprint]["used"] == 2


def test_lock7_zero_billing_on_the_whole_repair_path(srv, make_article):
    """免费口径不是靠"我们没写扣费代码"这句话保证的:把扣费入口全部替身成
    "一被调用就炸",修复照样成功 → 证明这条路径真的零计费。"""
    article_id = make_article(BODY_NUMBER)
    finding = real_finding(BODY_NUMBER, "unsourced_outcome_number")
    _fake_llm(srv, "内部口径下客户满意度有明显改善，团队据此调整了服务流程。")

    import middleware.billing as billing

    exploded: list[str] = []
    patched: dict[str, object] = {}

    def _boom(name):
        def _inner(*a, **k):
            exploded.append(name)
            raise AssertionError(f"span 级免费修复不许调用扣费函数:{name}")
        return _inner

    for name in dir(billing):
        if name.startswith("_"):
            continue
        attr = getattr(billing, name)
        if isinstance(attr, types.FunctionType) and (
            "charge" in name or "freeze" in name or "deduct" in name or "bill" in name
        ):
            patched[name] = attr
            setattr(billing, name, _boom(name))
    assert patched, "没抓到任何扣费函数名 → 这条锁会假绿"
    try:
        res = _post(srv, article_id, finding["code"], finding["matched_text"])
    finally:
        for name, attr in patched.items():
            setattr(billing, name, attr)
    assert res["success"] is True, res
    assert res["charged"] is False
    assert exploded == []
    content, _ = _read_content(article_id)
    assert content != BODY_NUMBER and "另有一段与本处无关的正文" in content


def test_lock6_gate_actions_drop_ai_repair_for_high_risk(srv, make_article):
    """发布门 blocked 分支的出口按钮:高风险类不给 ai_fix_this_span。
    (工单锚定的 `article_review_gate.py:112-116` 就是这三颗按钮。)"""
    from db.diagnosis_db import get_connection
    from services.article_review_gate import evaluate_publication_eligibility

    normal_id = make_article(BODY_ABSOLUTE)
    risky_id = make_article(BODY_MEDICAL, industry="医疗健康", title="慢性病疗法怎么选")
    conn = get_connection()
    try:
        c = conn.cursor()
        import hashlib

        for article_id, body in ((normal_id, BODY_ABSOLUTE), (risky_id, BODY_MEDICAL)):
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            c.execute(
                "UPDATE articles SET article_review_status='blocked', "
                "article_review=%s::jsonb, evidence_manifest_hash=%s WHERE id=%s",
                (
                    json.dumps({
                        "reviewed_content_hash": digest,
                        "reviewed_evidence_manifest_hash": "e" * 64,
                    }),
                    "e" * 64,
                    article_id,
                ),
            )
        conn.commit()
    finally:
        conn.close()

    normal = evaluate_publication_eligibility(normal_id)
    assert normal["reason_class"] == "legal_hard"
    assert {a["id"] for a in normal["actions"]} == {
        "ai_fix_this_span", "view_findings", "edit_manually",
    }
    assert normal["ai_repair_available"] is True

    risky = evaluate_publication_eligibility(risky_id)
    assert risky["reason_class"] == "legal_hard"
    # [核验流融合包① §3A · 2026-08-01] 原断言 overridable is False("法律硬门可覆盖性
    # 不许被本单动到" —— 指的是 span 级修复那一单)。Owner 08-01 拍板取消发布侧硬拦后,
    # legal_hard 已是提示级,overridable 必然为 True。
    # 🔴 本锁真正守的东西**一个字没变**:医疗/法律/金融高风险不给 AI 修复出口
    # (它缺的是人工签发,不是措辞;让 AI 软化措辞会让判定转绿而风险一点没减)。
    # 下面两条才是本锁的实质,原样保留。
    assert risky["overridable"] is True, "内容类已降为提示级"
    assert "ai_fix_this_span" not in {a["id"] for a in risky["actions"]}
    assert risky["ai_repair_available"] is False
