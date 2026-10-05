"""[发布门三态拆分 P1 · 2026-07-31] 工单 §4.4 八锁 · 行为级,禁源码串断言。

工单:docs/AI-CONTEXT/WORKORDER_REVIEW_GATE_AND_BATCH_AUDIT_2026-07-30.md §4

锁 1 legacy 不再是 H0            锁 5 修完即放行
锁 2 🔴反向:法律硬门仍拦          锁 6 人审可跳过 + 五字段审计
锁 3 🔴反向:平台档仍拦但只拦该档   锁 7 🔴反向:资金/租户/lineage 仍是 H0
锁 4 A1 不阻断                    锁 8 分布断言(True 与 False 必须同时出现)

🔴 为什么必须有反向锁与分布锁:只有正向锁的话,把 eligible 写死成 True 照样全绿。
锁 8 直接对样本集的 eligible 取 set,少一边就红。

用 TEMP 表影子 articles(与 test_p07_publish_gate_trio 同款),ON COMMIT DROP + rollback,
不拉起生成栈、不碰真实表。
"""
import hashlib
import json
import os

import psycopg2
import psycopg2.extras
import pytest

from services.article_review_gate import (
    evaluate_publication_eligibility,
    set_human_review,
)

_DDL = """
CREATE TEMP TABLE articles (
  id BIGINT PRIMARY KEY,
  content TEXT,
  current_content_hash CHAR(64),
  style_code VARCHAR(60),
  style VARCHAR(60),
  style_family VARCHAR(60),
  article_review_status VARCHAR(40),
  article_human_review_status VARCHAR(40),
  article_human_reviewed_by INTEGER,
  article_human_reviewed_at TIMESTAMPTZ,
  article_human_review_reason TEXT,
  article_review JSONB,
  evidence_manifest_hash CHAR(64),
  publication_profile VARCHAR(60),
  platform_review JSONB,
  evidence_pack JSONB,
  -- [三态拆分] 门现在还读 quality_warning(advisory_state 的唯一来源)。
  -- 影子表不同步这一列 = `column "quality_warning" does not exist`,八锁全 ERROR。
  -- 生产 articles.quality_warning 是 JSONB,已只读核对(information_schema)。
  quality_warning JSONB,
  title TEXT,
  quote_id INTEGER
) ON COMMIT DROP;

CREATE TEMP TABLE quotes (
  id INTEGER PRIMARY KEY,
  industry TEXT
) ON COMMIT DROP;

CREATE TEMP TABLE geo_article_review_events (
  id BIGSERIAL PRIMARY KEY,
  article_id BIGINT NOT NULL,
  actor_user_id INTEGER NOT NULL,
  decision VARCHAR(40) NOT NULL CHECK (decision IN ('approved','rejected','skipped')),
  reason TEXT NOT NULL,
  machine_review_status VARCHAR(40),
  machine_review_version VARCHAR(100),
  reviewed_content_hash CHAR(64),
  evidence_manifest_hash CHAR(64),
  -- 本单唯一 schema 变更(§4.3 五字段的 before)。影子表必须带,否则锁 6 拿不到证据。
  prior_human_review_status VARCHAR(40),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
) ON COMMIT DROP;
"""

_EV = "e" * 64


@pytest.fixture()
def cur(monkeypatch):
    monkeypatch.setenv("GEO_ARTICLE_PUBLICATION_REVIEW_GATE_ENABLED", "true")
    conn = psycopg2.connect(os.environ["TEST_DATABASE_URL"],
                            cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        c = conn.cursor()
        c.execute(_DDL)
        yield c
    finally:
        conn.rollback()
        conn.close()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _seed(cur, *, article_id, review_status, content="正文", style="buying_guide",
          style_family=None, human=None, reviewed=True, quality_warning=None,
          evidence_hash=_EV, reviewed_content=None, industry="建材家居",
          title="本地装修怎么选"):
    """reviewed=False 复刻**生产 1110 篇 legacy 的真实形态**:article_review IS NULL
    且 evidence_manifest_hash IS NULL(2026-07-31 只读实测,见交付说明 §2)。"""
    review_json = None
    if reviewed:
        review_json = json.dumps({
            "reviewed_content_hash": _sha(reviewed_content if reviewed_content is not None else content),
            "reviewed_evidence_manifest_hash": evidence_hash,
            "review_version": "v14-test",
        })
    cur.execute(
        "INSERT INTO quotes(id, industry) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
        (article_id, industry),
    )
    cur.execute(
        "INSERT INTO articles(id, content, style_code, style_family, article_review_status, "
        "article_human_review_status, article_review, evidence_manifest_hash, "
        "quality_warning, title, quote_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s)",
        (article_id, content, style, style_family, review_status, human, review_json,
         evidence_hash if reviewed else None,
         json.dumps(quality_warning) if quality_warning is not None else None,
         title, article_id),
    )


def _advisory(n: int = 3, acknowledged: bool = False) -> dict:
    payload = {"evidence": {"soft": [{"code": f"soft_{i}"} for i in range(n)]}}
    if acknowledged:
        payload["human_continue"] = {"acknowledged": True, "actor_user_id": 9}
    return payload


# ===========================================================================
# 锁 1 · legacy 不再是 H0
# ===========================================================================
def test_lock1_legacy_unreviewed_is_clear_and_not_run(cur):
    """🔴 生产形态:article_review IS NULL。旧代码在这里返回
    content_changed_after_review(实测 1110 篇全中),而不是走兜底分支 —— 所以
    "只改兜底分支"一篇都放不出来。本锁钉的正是这条真实路径。"""
    _seed(cur, article_id=1001, review_status="legacy_unreviewed", reviewed=False)
    r = evaluate_publication_eligibility(1001, cursor=cur)
    assert r["eligible"] is True
    assert r["publication_h0_state"] == "clear"
    assert r["review_state"] == "not_run"
    assert r["reason"] != "content_changed_after_review", (
        "快照缺失 = 从没审过,不是 lineage 不一致;把两者混为一谈正是本单要修的病"
    )


def test_lock1b_pending_human_review_is_clear(cur):
    """生产第二大群(136 篇)同样不该被非 H0 理由锁死。"""
    _seed(cur, article_id=1002, review_status="pending_human_review")
    r = evaluate_publication_eligibility(1002, cursor=cur)
    assert r["eligible"] is True
    assert r["publication_h0_state"] == "clear"
    assert r["review_state"] == "computed"


# ===========================================================================
# 锁 2 · 🔴 反向:法律硬门仍拦
# ===========================================================================
def test_lock2_legal_notice_is_advisory_but_conclusion_intact(cur):
    """[§3A 降级 2026-08-01] 原断言"法律硬门仍拦"。Owner 拍板广告法防线前移写作侧,
    发布侧硬拦取消 → **只翻"拦不拦"这一面**;机审结论、类别、定位与一键修复出口
    必须一字不减,否则就是借降级把功能做没了。"""
    _seed(cur, article_id=1003, review_status="blocked",
          content="我们是行业第一的服务商。")
    r = evaluate_publication_eligibility(1003, cursor=cur)
    # 翻面:不再阻塞发布
    assert r["eligible"] is True
    assert r["publication_h0_state"] == "clear"
    # 🔴 实质一律不减:结论仍在、类别仍是 legal_hard、修复出口仍在
    assert r["content_notice_state"] == "open"
    assert r["content_notice_classes"] == ["legal_hard"]
    notice = r["content_notices"][0]
    assert notice["reason"] == "blocked"
    assert notice["reason_class"] == "legal_hard"
    assert any(a["id"] == "ai_fix_this_span" for a in (notice.get("actions") or []))
    # 提示文案必须明示"由你决定",不能还写着"不可覆盖"
    assert "不阻止你发布" in notice["message"]


def test_lock2b_downgrade_must_not_bypass_operator_hard(cur):
    """🔴🔴 本锁是 P1 那条"最危险绕过路径"锁的**继承版**,守的是同一个结构风险。

    P1 时的风险:快照缺失若提前 return clear → 绕过法律硬门。
    本单的风险:法律提示若在原地 return clear → **绕过后面的人工拒稿等 operator_hard**。
    两者是同一个病。实现刻意把提示累积后继续往下走,本锁就是那句话的行为证据:
    一篇既命中广告法、又被人工明确拒稿的文章,**必须仍然拦**,且提示照样报出来。
    """
    _seed(cur, article_id=1004, review_status="blocked", reviewed=False,
          human="rejected", content="我们是行业第一的服务商。")
    r = evaluate_publication_eligibility(1004, cursor=cur)
    assert r["review_state"] == "not_run"
    assert r["eligible"] is False, "内容类降级不得顺手把人工拒稿也放行"
    assert r["publication_h0_state"] == "operator_hard"
    assert r["reason"] == "human_rejected"
    # 拦归拦,提示不许被吞掉(证明是"走完全程"而不是"提前 return")
    assert "legal_hard" in r["content_notice_classes"]


def test_lock2c_legal_notice_survives_without_review_snapshot(cur):
    """P1 锁 2b 的另一半:快照缺失(生产 1110 篇 legacy 的真实形态)时,
    blocked 检查仍要跑到 —— 降级后表现为"提示仍然产出",而不是静默消失。"""
    _seed(cur, article_id=1014, review_status="blocked", reviewed=False,
          content="我们是行业第一的服务商。")
    r = evaluate_publication_eligibility(1014, cursor=cur)
    assert r["review_state"] == "not_run"
    assert r["eligible"] is True
    assert r["content_notice_classes"] == ["legal_hard"], (
        "未审快照不得让法律提示静默消失(那等于机审白跑)"
    )


# ===========================================================================
# 锁 3 · 🔴 反向:平台档仍拦,但只拦该档
# ===========================================================================
def test_lock3_platform_profile_is_advisory_and_still_layered(cur):
    """[§3A 降级 2026-08-01] 原断言"平台档仍拦但只拦该档"。平台档属内容类 → 降为提示级。
    **分层必须保住**:平台档与法律硬门仍是两个类别,不许降级时糊成一类。"""
    _seed(cur, article_id=1005, review_status="rewrite_required")
    r = evaluate_publication_eligibility(1005, cursor=cur)
    assert r["eligible"] is True
    assert r["publication_h0_state"] == "clear"
    assert r["content_notice_classes"] == ["platform_profile_hard"]
    assert "legal_hard" not in r["content_notice_classes"], "平台档必须与法律硬门分层"

    # 换到非严审档重评(refresh 后 machine 不再是 rewrite_required)→ 可发
    cur.execute(
        "UPDATE articles SET article_review_status='approved' WHERE id=%s", (1005,),
    )
    after = evaluate_publication_eligibility(1005, cursor=cur)
    assert after["eligible"] is True
    assert after["publication_h0_state"] == "clear", "平台档不得全局封死文章"


# ===========================================================================
# 锁 4 · A1 不阻断
# ===========================================================================
def test_lock4_open_advisory_findings_do_not_block(cur):
    """N 条 A1 提示且**未确认** → 仍 eligible True、advisory_state=='open'。
    这条同时替代被删掉的 "我确认过了,继续" 源码串断言:锁的是
    "确认动作可有可无"这件事本身,而不是某个按钮的字样。"""
    _seed(cur, article_id=1006, review_status="approved", quality_warning=_advisory(5))
    r = evaluate_publication_eligibility(1006, cursor=cur)
    assert r["eligible"] is True
    assert r["advisory_state"] == "open"
    assert r["advisory_open_count"] == 5
    assert r["publication_h0_state"] == "clear"


def test_lock4b_acknowledged_advisory_reports_acknowledged(cur):
    _seed(cur, article_id=1007, review_status="approved",
          quality_warning=_advisory(2, acknowledged=True))
    r = evaluate_publication_eligibility(1007, cursor=cur)
    assert r["eligible"] is True
    assert r["advisory_state"] == "acknowledged"


def test_lock4c_no_advisory_reports_none(cur):
    _seed(cur, article_id=1008, review_status="approved", quality_warning={})
    r = evaluate_publication_eligibility(1008, cursor=cur)
    assert r["advisory_state"] == "none"
    assert r["advisory_open_count"] == 0


# ===========================================================================
# 锁 5 · 修完即放行
# ===========================================================================
def test_lock5_blocked_then_repaired_and_rereviewed_passes(cur):
    _seed(cur, article_id=1009, review_status="blocked",
          content="我们是行业第一的服务商。")
    # [§3A 降级] 修复前不再是"拦",而是"带提示可发";修完提示必须**清空**——
    # 这一条才是本锁的实质(修复真的生效了),降级后反而更该锁住。
    before = evaluate_publication_eligibility(1009, cursor=cur)
    assert before["eligible"] is True
    assert before["content_notice_state"] == "open"
    fixed = "我们在公开口径下表现较好。"
    cur.execute(
        "UPDATE articles SET content=%s, article_review_status='approved', "
        "article_review=%s::jsonb WHERE id=%s",
        (fixed, json.dumps({
            "reviewed_content_hash": _sha(fixed),
            "reviewed_evidence_manifest_hash": _EV,
            "review_version": "v14-test",
        }), 1009),
    )
    after = evaluate_publication_eligibility(1009, cursor=cur)
    assert after["eligible"] is True
    assert after["publication_h0_state"] == "clear"
    assert after["review_state"] == "computed"
    # 🔴 修完提示必须真的消失。若这里仍是 open,说明提示是"贴上去就摘不掉"的死标签,
    # 一键修复在 UI 上永远显示未处理 —— 降级把拦截去掉之后,这是唯一还能证明
    # "修复确实生效"的断言,比原来的 eligible False→True 更要紧。
    assert after["content_notice_state"] == "none"
    assert after["content_notices"] == []


# ===========================================================================
# 锁 6 · 人审可跳过 + 五字段审计
# ===========================================================================
def test_lock6_recommended_human_review_is_skippable_with_five_field_audit(cur):
    _seed(cur, article_id=1010, review_status="pending_human_review",
          style="brand_softarticle", style_family="company_facts")
    before = evaluate_publication_eligibility(1010, cursor=cur)
    # §4.3:推荐人审**本来就不该阻断**(不得成为全局发布死锁)
    assert before["eligible"] is True
    assert before["reason"] == "brand_story_review_recommended"
    assert before["publication_h0_state"] == "clear"

    set_human_review(1010, reviewer_user_id=77, decision="skipped",
                     reason="企业档案已由客户确认无误", cursor=cur)

    after = evaluate_publication_eligibility(1010, cursor=cur)
    assert after["eligible"] is True

    cur.execute(
        "SELECT actor_user_id, created_at, reason, prior_human_review_status, decision "
        "FROM geo_article_review_events WHERE article_id=%s ORDER BY id DESC LIMIT 1",
        (1010,),
    )
    row = cur.fetchone()
    assert row is not None, "跳过必须留痕"
    # 五字段:operator / time / reason / before / after
    assert row["actor_user_id"] == 77                      # operator
    assert row["created_at"] is not None                   # time
    assert row["reason"] == "企业档案已由客户确认无误"        # reason
    assert row["prior_human_review_status"] is None        # before(跳过前无人审状态)
    assert row["decision"] == "skipped"                    # after


def test_lock6b_audit_before_field_captures_real_prior_state(cur):
    """before 必须是**决策前**的值,不是决策后的回读(否则 before==after 恒等,
    这一列等于没写)。先 rejected 再 approved,before 必须是 rejected。"""
    _seed(cur, article_id=1011, review_status="approved", human="rejected")
    set_human_review(1011, reviewer_user_id=88, decision="approved",
                     reason="复核后确认可以发布", cursor=cur)
    cur.execute(
        "SELECT prior_human_review_status, decision FROM geo_article_review_events "
        "WHERE article_id=%s ORDER BY id DESC LIMIT 1", (1011,),
    )
    row = cur.fetchone()
    assert row["prior_human_review_status"] == "rejected"
    assert row["decision"] == "approved"


def test_lock6c_skip_does_not_erase_content_notice(cur):
    """[§3A 降级 2026-08-01] 原断言"跳过绝不越过法律硬门"。降级后法律门已不拦,
    **没有东西可越** → 该锁若原样保留就成了恒真的空壳。改成守真正还有意义的那条:
    跳过推荐人审 **不得顺手把内容提示也抹掉**(否则"跳过"就变成了消音按钮)。"""
    _seed(cur, article_id=1012, review_status="blocked",
          style="brand_softarticle", style_family="company_facts",
          content="我们是行业第一的服务商。")
    set_human_review(1012, reviewer_user_id=77, decision="skipped",
                     reason="企业档案已自行核对", cursor=cur)
    r = evaluate_publication_eligibility(1012, cursor=cur)
    assert r["eligible"] is True
    assert r["content_notice_classes"] == ["legal_hard"], (
        "跳过人审不是消音按钮:广告法提示必须仍然报出来"
    )


def test_lock6d_skip_still_cannot_bypass_operator_hard(cur):
    """🔴 反向(锁 6c 让出的那个位置由本锁接管):跳过仍然绕不过 operator_hard。
    lineage 不一致的文章,reason 不是 brand_story_review_recommended,skip 必须报错。"""
    _seed(cur, article_id=1013, review_status="approved",
          style="brand_softarticle", style_family="company_facts",
          content="改过的正文", reviewed_content="审核时的正文")
    with pytest.raises(ValueError):
        set_human_review(1013, reviewer_user_id=77, decision="skipped",
                         reason="想跳过 lineage 硬门", cursor=cur)
    assert evaluate_publication_eligibility(1013, cursor=cur)["eligible"] is False


# ===========================================================================
# 锁 7 · 🔴 反向:资金 / 租户 / lineage 仍是 H0
# ===========================================================================
def test_lock7_content_lineage_mismatch_still_h0(cur):
    """审完又改正文 = 真的 lineage 不一致 → 仍拦(这条若放开,"审完再改"
    就成了绕过审核的后门)。"""
    _seed(cur, article_id=1013, review_status="approved",
          content="改过的正文", reviewed_content="审核时的正文")
    r = evaluate_publication_eligibility(1013, cursor=cur)
    assert r["eligible"] is False
    assert r["publication_h0_state"] == "operator_hard"
    assert r["reason"] == "content_changed_after_review"


def test_lock7b_evidence_lineage_mismatch_still_h0(cur):
    _seed(cur, article_id=1014, review_status="approved")
    cur.execute("UPDATE articles SET evidence_manifest_hash=%s WHERE id=%s",
                ("f" * 64, 1014))
    r = evaluate_publication_eligibility(1014, cursor=cur)
    assert r["eligible"] is False
    assert r["publication_h0_state"] == "operator_hard"
    assert r["reason"] == "evidence_changed_after_review"


def test_lock7c_missing_object_still_h0(cur):
    """对象不存在(跨租户探测/越权取号的典型表现)→ 仍 H0,不得 fail-open。"""
    r = evaluate_publication_eligibility(999999, cursor=cur)
    assert r["eligible"] is False
    assert r["publication_h0_state"] == "operator_hard"
    assert r["reason"] == "article_not_found"


def test_lock7d_human_rejected_still_blocks(cur):
    """人工**明确拒稿**必须继续拦 —— 否则审核员按下的"拒绝"当场失效。"""
    _seed(cur, article_id=1015, review_status="approved", human="rejected")
    r = evaluate_publication_eligibility(1015, cursor=cur)
    assert r["eligible"] is False
    assert r["reason"] == "human_rejected"
    # [P3 并入项 1 · 2026-08-01] 复审 §3.2 点名的正是这一条:人工拒稿归到了名叫
    # `tenant_or_funds` 的桶里,读起来像"越权/资金守恒失败"。改名后把档名钉死在这里
    # —— 改名若漏了任何一处成员(或有人把它单独挪走改了行为),本条即红。
    assert r["publication_h0_state"] == "operator_hard"


def test_lock7e_human_approved_snapshot_mismatch_still_h0(cur):
    """签发后正文被改 → 签发失效,仍拦。"""
    _seed(cur, article_id=1016, review_status="approved", human="approved")
    cur.execute(
        "INSERT INTO geo_article_review_events(article_id, actor_user_id, decision, "
        "reason, reviewed_content_hash, evidence_manifest_hash) "
        "VALUES (%s,%s,'approved','签发', %s, %s)",
        (1016, 5, _sha("别的正文"), _EV),
    )
    r = evaluate_publication_eligibility(1016, cursor=cur)
    assert r["eligible"] is False
    assert r["publication_h0_state"] == "operator_hard"
    assert r["reason"] == "human_approval_content_changed"


# ===========================================================================
# 锁 8 · 分布断言
# ===========================================================================
def test_lock8_sample_set_contains_both_eligible_true_and_false(cur):
    """🔴 只有正向锁的话,`eligible` 写死成 True 照样全绿。本锁对同一批样本
    取 eligible 的取值集合,必须**同时**出现 True 与 False,写死任一侧即红。"""
    _seed(cur, article_id=2001, review_status="legacy_unreviewed", reviewed=False)
    _seed(cur, article_id=2002, review_status="pending_human_review")
    _seed(cur, article_id=2003, review_status="approved")
    _seed(cur, article_id=2004, review_status="blocked",
          content="我们是行业第一的服务商。")
    _seed(cur, article_id=2005, review_status="rewrite_required")
    _seed(cur, article_id=2006, review_status="approved",
          content="改过的正文", reviewed_content="审核时的正文")
    _seed(cur, article_id=2007, review_status="approved", human="rejected")

    verdicts = {
        aid: evaluate_publication_eligibility(aid, cursor=cur)
        for aid in (2001, 2002, 2003, 2004, 2005, 2006, 2007)
    }
    values = {v["eligible"] for v in verdicts.values()}
    assert values == {True, False}, f"eligible 必须两侧都有,实际={values}"

    # 分布必须落在正确的一边,而不只是"两种都有"
    assert verdicts[2001]["eligible"] is True   # 未审 → 放行
    assert verdicts[2002]["eligible"] is True   # 待人审 → 放行
    assert verdicts[2003]["eligible"] is True   # 机审通过 → 放行
    assert verdicts[2004]["eligible"] is True   # [§3A] 广告法 → 降为提示,放行
    assert verdicts[2005]["eligible"] is True   # [§3A] 平台档 → 降为提示,放行
    assert verdicts[2006]["eligible"] is False  # lineage 不一致 → 仍拦
    assert verdicts[2007]["eligible"] is False  # 人工明确拒稿 → 仍拦

    h0 = {v["publication_h0_state"] for v in verdicts.values()}
    assert h0 == {"clear", "operator_hard"}, (
        f"降级后 h0 只应剩 clear / operator_hard 两个取值,实际={h0}"
    )

    # 🔴 上一行丢掉了原锁"四个 h0 取值全覆盖"的判别力(legal_hard / platform_profile_hard
    # 不再是 h0 取值了)。判别力必须由**提示类别的分布**接管,否则把 content_notices
    # 一律写成空数组照样全绿 —— 那正是本次降级最容易做废的地方。
    notice_classes = {c for v in verdicts.values() for c in v["content_notice_classes"]}
    assert notice_classes == {"legal_hard", "platform_profile_hard"}, (
        f"两类内容提示必须都被样本覆盖(不许被降级顺手清空),实际={notice_classes}"
    )
    notice_states = {v["content_notice_state"] for v in verdicts.values()}
    assert notice_states == {"none", "open"}, (
        f"content_notice_state 必须两侧都有,写死任一侧即红,实际={notice_states}"
    )


def test_lock8b_eligible_is_derived_only_from_h0_state(cur):
    """契约锁:eligible 恒等于 (publication_h0_state == 'clear')。
    任何让两者脱钩的改动(例如把 review_state 重新塞回 eligible)即红。"""
    _seed(cur, article_id=2101, review_status="legacy_unreviewed", reviewed=False)
    _seed(cur, article_id=2102, review_status="blocked",
          content="我们是行业第一的服务商。")
    _seed(cur, article_id=2103, review_status="rewrite_required")
    _seed(cur, article_id=2104, review_status="approved", quality_warning=_advisory(4))
    for aid in (2101, 2102, 2103, 2104):
        r = evaluate_publication_eligibility(aid, cursor=cur)
        assert r["eligible"] is (r["publication_h0_state"] == "clear"), (
            f"article {aid}: eligible={r['eligible']} 与 h0={r['publication_h0_state']} 脱钩"
        )
