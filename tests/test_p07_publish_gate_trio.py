"""[WP9-P0-7 · D8]发布口判别三连(Owner 点名):

1. 违法绝对化**草稿能存**(文章层零阻断)—— 见 test_p07_d8_zero_blocking(源锁);
2. **对外发布被拦且带一键修复**(本文件);
3. **修完放行**(本文件)。

用 TEMP 表影子 articles,直接驱动 evaluate_publication_eligibility,不拉起生成栈。
"""
import os

import psycopg2
import psycopg2.extras
import pytest

from services.article_review_gate import evaluate_publication_eligibility

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
  article_review JSONB,
  evidence_manifest_hash CHAR(64),
  publication_profile VARCHAR(60),
  platform_review JSONB,
  -- [标准类证据 lane 2026-07-29] 影子表必须跟住门真读的列:门加读 evidence_pack
  -- 之后这里不同步 = `column "evidence_pack" does not exist`,六条锁全 ERROR。
  -- (生产 articles.evidence_pack 是 JSONB,已只读核对。)
  evidence_pack JSONB,
  -- [span 级 AI 免费修复 2026-07-30] 同一条教训第二次:门现在还读 title 与
  -- quotes.industry(判"是不是医疗/法律/金融高风险" → 决定 ai_fix_this_span
  -- 渲不渲染)。列名/类型对生产 pg_dump 核对过:articles.title text /
  -- articles.quote_id integer / quotes.industry text。
  title TEXT,
  quote_id INTEGER,
  -- [发布门三态拆分 2026-07-31] **同一条教训第三次**:门现在还读 quality_warning
  -- (advisory_state 的唯一来源)。影子表不同步 = `column "quality_warning" does
  -- not exist`,本文件七条锁全 ERROR(而 ERROR 不是 PASS,也不是"锁在拦")。
  -- 生产 articles.quality_warning 是 JSONB,已只读核对 information_schema。
  -- 🔴 只动 DDL,下面 106-132 三条法律硬门/放行/平台档断言一字未动。
  quality_warning JSONB
) ON COMMIT DROP;

CREATE TEMP TABLE quotes (
  id INTEGER PRIMARY KEY,
  industry TEXT
) ON COMMIT DROP;
"""


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


def _review_payload(content: str, evidence_hash: str) -> str:
    """审核快照与正文/证据哈希绑定(门的完整性前置条件,测试须尊重)。"""
    import hashlib
    import json
    return json.dumps({
        "reviewed_content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "reviewed_evidence_manifest_hash": evidence_hash,
        "review_version": "v14-test",
    })


def _seed(cur, *, article_id, review_status, style="buying_guide", content="正文",
          industry="建材家居", title="本地装修怎么选"):
    """[span 级 2026-07-30] industry/title 默认给**非高风险**值:本文件锁的是
    "拦的同时给一键修复出口",而医疗/法律/金融高风险类按 §2.1 刻意没有那颗按钮
    (那条另有 test_span_level_ai_repair_2026_07_30 的锁 6 覆盖)。"""
    evidence_hash = "e" * 64
    cur.execute(
        "INSERT INTO quotes(id, industry) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
        (article_id, industry),
    )
    cur.execute(
        "INSERT INTO articles(id, content, style_code, article_review_status, "
        "article_review, evidence_manifest_hash, title, quote_id) "
        "VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s)",
        (article_id, content, style, review_status,
         _review_payload(content, evidence_hash), evidence_hash, title, article_id),
    )


def _rereview(cur, *, article_id, content, review_status):
    """模拟"修完重审":正文变更后重新执行审核 → 快照重新绑定新正文哈希。"""
    evidence_hash = "e" * 64
    cur.execute(
        "UPDATE articles SET content=%s, article_review_status=%s, article_review=%s::jsonb "
        "WHERE id=%s",
        (content, review_status, _review_payload(content, evidence_hash), article_id),
    )


def test_outbound_publish_notifies_but_does_not_block_with_repair_action(cur):
    """[§3A 降级 2026-08-01] 原断言"对外发布被拦"。Owner 拍板广告法防线前移写作侧,
    发布侧硬拦取消 → 翻的只有"拦不拦";提示与一键修复出口一字不减。"""
    _seed(cur, article_id=101, review_status="blocked", content="我们是行业第一的服务商。")
    result = evaluate_publication_eligibility(101, cursor=cur)
    assert result["eligible"] is True                     # [§3A] 不再阻塞
    assert result["content_notice_state"] == "open"       # 但必须报出来
    assert result["reason"] == "blocked"
    assert result["reason_class"] == "legal_hard"
    actions = result.get("actions") or []
    assert any(a["id"] == "ai_fix_this_span" for a in actions), "提示必须带一键修复动作"
    assert all(a.get("id") and a.get("label") for a in actions)


def test_notice_clears_after_the_violation_is_repaired(cur):
    """[§3A] 原断言"修完才放行"。降级后修前修后都能发 → 该断言已无判别力。
    改锁真正还有意义的:**修完提示要真的消失**(否则一键修复在 UI 上永远显示未处理)。"""
    _seed(cur, article_id=102, review_status="blocked", content="我们是行业第一的服务商。")
    before = evaluate_publication_eligibility(102, cursor=cur)
    assert before["eligible"] is True and before["content_notice_state"] == "open"
    # span 级修复 + 重审后的状态(快照重新绑定修复后的正文)
    _rereview(cur, article_id=102, content="我们在公开口径下表现较好。", review_status="approved")
    after = evaluate_publication_eligibility(102, cursor=cur)
    assert after["eligible"] is True, "修完必须放行"
    assert after["reason"] == "machine_rules_approved"
    assert after["content_notice_state"] == "none", "修完提示必须消失"


def test_platform_profile_notice_stays_channel_level_and_repairable(cur):
    # ⑤ 平台档保持渠道级(不拆·不与 legal_hard 混同);[§3A] 降为提示级
    _seed(cur, article_id=103, review_status="rewrite_required")
    result = evaluate_publication_eligibility(103, cursor=cur)
    assert result["eligible"] is True
    assert result["content_notice_classes"] == ["platform_profile_hard"]
    assert result["reason_class"] == "platform_profile_hard"
    assert result["reason"] != "blocked"  # 与法律硬门分层清晰


# ===== [返修 P1-3] 兜底分支必须给出口:存量未审文章不能被堵死 =====

def test_legacy_unreviewed_is_not_a_publication_hard_gate(cur):
    """🔴 [发布门三态拆分 2026-07-31 · 工单 §4.1/§1.1] 本条断言**语义被订正**。

    旧名 `test_legacy_unreviewed_is_blocked_but_with_a_runnable_exit` 锁的是
    "legacy 被 blocked(只是给了出口)" —— 那正是工单点名要改掉的**错误语义**:
    "还没跑过机审"既不属法律硬门、也不属平台档,不该阻断对外发布。
    改后锁的是:出口仍在(actions/repair_hint 一条不少),但**不再 eligible=False**。
    """
    _seed(cur, article_id=201, review_status="legacy_unreviewed")
    r = evaluate_publication_eligibility(201, cursor=cur)
    assert r["eligible"] is True, "还没跑过机审不是对外发布硬门"
    assert r["publication_h0_state"] == "clear"
    assert r["reason_class"] == "review_not_run"
    assert r["overridable"] is True          # 不是永久硬门
    actions = r.get("actions") or []
    assert any(a["id"] == "run_article_review" for a in actions), "存量未审仍要给一键执行审核"
    assert all(a.get("id") and a.get("label") for a in actions)
    assert r.get("repair_hint")


def test_machine_not_approved_also_gets_repair_and_human_exits(cur):
    """机审未过同样不再是发布硬门,但三个出口一个不能少(工单 §4.1)。"""
    _seed(cur, article_id=202, review_status="needs_fix")
    r = evaluate_publication_eligibility(202, cursor=cur)
    assert r["eligible"] is True and r["publication_h0_state"] == "clear"
    assert r["reason_class"] == "machine_not_approved"
    ids = {a["id"] for a in (r.get("actions") or [])}
    assert {"ai_fix_this_span", "run_article_review", "request_human_review"} <= ids


def test_standard_citation_content_claim_blocks_through_the_real_sql_path(cur):
    """[标准类证据 lane 2026-07-29 · §5.6] 标记必须**真能被门读到**。

    这一条刻意走真游标真 SQL(不是假 row):§3.3 实测 `source_tier` 全仓零读取,
    不接线的话锁拿不到字段会永远不触发而全绿 —— 那正是本仓今日踩过两次的
    "看着在跑、实际空转"。
    """
    import json

    pack = json.dumps({"items": [{
        "evidence_id": "EV-001",
        "title": "GB 50210-2018 建筑装饰装修工程质量验收标准",
        "url": "https://www.mohurd.gov.cn/a.pdf",
        "source_tier": "standard_citation",
        "verification_status": "search_result_only",
    }]}, ensure_ascii=False)
    body = (
        "依据 GB 50210-2018《建筑装饰装修工程质量验收标准》，验收有据可依。"
        "该标准要求甲醛释放量不超过 0.03mg/m³。"
    )
    _seed(cur, article_id=301, review_status="approved", content=body)
    cur.execute("UPDATE articles SET evidence_pack=%s::jsonb WHERE id=%s", (pack, 301))
    r = evaluate_publication_eligibility(301, cursor=cur)
    # [§3A] 编造型证据同属内容类 → 降为提示级。**判定本身一字未改**:
    # 标记仍必须真能被门读到,否则本条(以及它防的"看着在跑、实际空转")就废了。
    assert r["eligible"] is True
    assert r["reason"] == "standard_citation_content_claim"
    assert r["content_notice_classes"] == ["evidence_fabrication_hard"]

    # 只做存在性引用 → 同一条证据下必须**无提示**(别把锁写成"有标准就报")。
    clean = "依据 GB 50210-2018《建筑装饰装修工程质量验收标准》，装修验收有国家标准可依。"
    _rereview(cur, article_id=301, content=clean, review_status="approved")
    after = evaluate_publication_eligibility(301, cursor=cur)
    assert after["eligible"] is True
    assert after["content_notice_state"] == "none", (
        "存在性引用不该报提示 —— 否则本锁退化成'有标准就报',判别力归零"
    )


def test_content_classes_are_overridable_but_operator_hard_is_not(cur):
    """🔴 [§3A 同步改 2026-08-01] 原名 test_legal_hard_remains_the_only_unoverridable_hard_gate,
    断言"法律硬门仍 overridable=False"。Owner 拍板取消发布侧硬拦后该断言与新口径打架。

    改成**成对断言**,判别力比原来强:三类内容提示全部 overridable=True(降级真生效),
    而 operator_hard 类必须仍 overridable=False(降级没过界)。
    单说任一边都证明不了 —— 全放软和全不放软各自都会红。"""
    # 正向:内容类已放软
    _seed(cur, article_id=203, review_status="blocked")
    r = evaluate_publication_eligibility(203, cursor=cur)
    assert r["reason_class"] == "legal_hard" and r["overridable"] is True
    _seed(cur, article_id=204, review_status="rewrite_required")
    r2 = evaluate_publication_eligibility(204, cursor=cur)
    assert r2["reason_class"] == "platform_profile_hard" and r2["overridable"] is True

    # 🔴 反向:lineage 不一致仍是不可覆盖的 H0
    _seed(cur, article_id=205, review_status="approved")
    cur.execute("UPDATE articles SET content=%s WHERE id=%s", ("审核后被改过的正文", 205))
    r3 = evaluate_publication_eligibility(205, cursor=cur)
    assert r3["eligible"] is False
    assert r3["publication_h0_state"] == "operator_hard"
    assert r3["overridable"] is False, "对象完整性绝不允许被降级顺手放软"
