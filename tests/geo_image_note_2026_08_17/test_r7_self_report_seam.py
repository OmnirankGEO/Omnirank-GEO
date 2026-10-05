# -*- coding: utf-8 -*-
"""第 7 棒 · R7 (5) · 浏览器自报两处谓词的**单一出处**与跨包并车耦合。

Review 裁定第 ⑤ 条要求采纳文章自报包导出的一行改法:
  · occurrence 臂加 `AND verification_state = 'verified'`;
  · body proof 弃**来源位** `public_url_reported_explicitly`,改**核实位**。

🔴 本包能做的与不能做的,分得很清楚:

  **能做**:把这两段谓词从内联 SQL 里抽成单一出处的可导出常量
  (`SELF_REPORT_OCCURRENCE_SQL` / `SELF_REPORT_BODY_PROOF_SQL`),
  这正是"一行改法"成立的前提 —— 谓词散在两个 SQL 字面量里时,
  文章包那一行改不动,而且改一处漏一处没有判据看得见。

  **不能做**:现在就把核实位写进去。**这不是推断,是实跑出来的**(2026-08-19):
  我真的改了一版(occurrence 臂加 `AND rx.public_url_verification_state='verified'`、
  body proof 换核实位),然后跑图文全量 —— **20 条既有判据当场红**
  (`test_wp7_projection_pg16.py` 整片),因为该列在生产 schema
  `_geoimg_prodschema_20260817.sql` 与本树 `db/*.sql` 里**都还不存在**;
  它由文章自报包的 `scripts/migration_publish_records_url_verification_2026_08_19.sql`
  引入。改回来之后 22 passed。

  🔴 所以这一行改法**既不属于本包、也不属于文章包,它属于两包的并车提交**:
  文章包里没有 `publication_stage_sources.py`(那是本包的文件),
  本包里没有那条迁移。次序 = 并车时一起落(交付单 ④)。

  [并车 2026-08-20 · 36 班订正] 上面「不能做」的前提已随并车消失:迁移 039
  已进树,两条谓词已按逐字改法换成核实位。原「自激活」判据群随之翻转/退役,
  现役判据断的是**已并车的现状**(臂带核实位、来源位不得回潮、不加 COALESCE)。
"""
from __future__ import annotations

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SOURCES = REPO / "services" / "publication_stage_sources.py"

#: 核实位列名(2026-08-20 · 36 班并车后:该列已随文章包迁移 039 进树,
#: 「自激活开关」使命结束;常量保留只作下方断言的锚)。
VERIFIED_BIT = "public_url_verification_state"


def _sources_text() -> str:
    return SOURCES.read_text(encoding="utf-8", errors="ignore")


def _strip_comments(text: str) -> str:
    """去掉 `#` 注释行 —— 结构锚不能被**自己的病历**判红。

    🔴 本仓一天内因为这件事红过三次:锁里引用裁决原文/写耦合声明,
       裸串匹配把说明文字当成了真代码。下面两处都必须先剥注释再数。
    """
    out = []
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        out.append(line)
    return chr(10).join(out)


# [并车 2026-08-20 · 36 班 · 清单件 3] `_schema_has_verified_bit` 死码已删:
# R8 ④ 撤掉自激活锁后它就没有调用方了,留着只会冒充防线。
# 列已随文章包迁移 039 真实进树,存在性由该迁移的自身判据守。


def test_self_report_predicates_have_exactly_one_source():
    """两段谓词各自**只写一处**,两个消费点都引用常量。

    🔴 这是"一行改法"的前提:谓词内联在两个 SQL 字面量里时,文章包那一行
       根本改不动 —— 只能两处各改一遍,而改一处漏一处**没有任何判据看得见**。
       本仓已经为"同一谓词写两处"付过学费(第 6 棒变异 Q7)。
    """
    from services.publication_stage_sources import (
        SELF_REPORT_BODY_PROOF_SQL,
        SELF_REPORT_OCCURRENCE_SQL,
    )

    text = _sources_text()
    # ① 常量被两个 SQL 真的引用(而不是定义完没人用 —— 那是死常量)
    assert text.count("+ SELF_REPORT_BODY_PROOF_SQL +") == 1, (
        "body proof 常量没有被 SQL 引用 —— 定义了没人用等于没抽")
    assert text.count("+ SELF_REPORT_OCCURRENCE_SQL +") == 1, (
        "occurrence 常量没有被 SQL 引用")

    # ② 在**本坐标模块内**各只出现一次(常量本身),不许再内联第二份
    body = _strip_comments(text)
    # [并车 2026-08-20 · 36 班] 锚随核实位翻转:body proof 现在的谓词是
    # `r.public_url_verification_state = 'verified'`(来源位已整体退场,见下一条 census)。
    assert body.count("r.public_url_verification_state = 'verified'") == 1, (
        "body proof 在 publication_stage_sources 里出现了不止一次 —— 单一出处被破坏")
    assert body.count("rx.status = 'success'") == 1, (
        "occurrence 臂在 publication_stage_sources 里出现了不止一次")
    assert body.count("rx.public_url_verification_state = 'verified'") == 1, (
        "occurrence 臂的核实位谓词出现次数异常 —— 单一出处被破坏")


    # ③ 常量本身不是空壳(零分母的"单一出处"毫无意义)
    assert "submitted_content_snapshot_hash" in SELF_REPORT_BODY_PROOF_SQL
    assert "publish_records" in SELF_REPORT_OCCURRENCE_SQL
    assert "{q}" in SELF_REPORT_OCCURRENCE_SQL, (
        "occurrence 常量丢了 quote 占位符 —— 绑定会静默变成全表存在性")


def test_the_remaining_source_bit_readers_are_declared():
    """🔴 **census**:还有谁在拿**来源位**当"已核实"用。

    Review 第 ⑤ 条把改动圈在两个坐标上。实扫之后发现:来源位
    `public_url_reported_explicitly` 在 `services/` 里**还有另外四个消费方**
    (数据健康 / 实验登记 / 严格产出 / 投放计划)。它们都在**文章 lane**、
    不在图文包 territory —— 本包不擅自改,但**必须把它们点名**:
    文章包只翻 `publication_stage_sources` 这一处的话,那四处仍然把
    "URL 是显式报的"当成"我们核实过",核实位就只补上了五分之一。

    🔴 写成**集合**不是计数:计数会被正常业务写过期而无声,集合里多一个
       文件就必须有人来这里声明一次(本仓已为"计数式判据"付过学费)。
    """
    declared = {
        # [并车 2026-08-20 · 36 班重锚] 文章包 R2~R6 已把 data_health /
        # experiment_registry / strict_outcomes / delivery_plan 与本包
        # publication_stage_sources 的谓词全部翻成核实位 —— 来源位读者只剩两个,
        # 且都**合法**:
        # ① 探针队列选择器:「显式报过 URL」正是"该去核实谁"的入队条件,
        #    它读来源位是本职,不是把它当核实位用;
        "services/publication_url_verifier.py",
        # ② 列契约声明(列在不在、类型对不对),不是消费方。
        "services/geo_article_v14_schema_contract.py",
    }
    found = set()
    for f in sorted((REPO / "services").rglob("*.py")):
        if "__pycache__" in str(f):
            continue
        if "public_url_reported_explicitly" in _strip_comments(
                f.read_text(encoding="utf-8", errors="ignore")):
            found.add(str(f.relative_to(REPO)).replace("\\", "/"))
    assert found == declared, (
        "来源位消费方的名单变了,必须有人重新判一遍谁该改成核实位。 "
        "多出来:" + str(sorted(found - declared))
        + " / 少掉了:" + str(sorted(declared - found)))


# ═══════════════════════════════════════════════════════════════════
# 【R8 ④ · 降级说明】原 `test_the_verified_bit_flip_cannot_be_forgotten`
#   (按 schema/迁移存在性自激活的强制锁)**已撤下**,改为提示注释。
#
# 🔴 为什么撤:强制执行应该由**扫得到那条臂的人**来做。本包的自激活锁只能看见
#    "列进树了没有",看不见"臂改对了没有";而并树之后,真正有判别力的是文章包
#    那道跨包完备性闸 —— **前提是它先把臂形状的括号洞修掉**(见下面那条判据:
#    它今天扫不到本包的 occurrence 臂)。
#
# 🔴 所以现在的分工是:
#      · 本包:把谓词收成单一出处 + 把改法逐字记档(下面两条判据);
#      · 文章包:修好扫描器形状 → 并树时由它强制;
#      · 交接信号:`test_the_cross_package_gate_does_not_actually_cover_our_occurrence_arm`
#        会在上游修好形状的那一刻**转红** —— 那就是"该并车翻这两行"的提醒。
#
# ⚠️ 提示(给并车那一棒):核实位列名是 `public_url_verification_state`;
#    两处各加/换一行,改法逐字写在 `test_the_exact_one_line_fix_is_recorded_verbatim`。
#    别在本包单独翻 —— 实跑过,列不在时 `test_wp7_projection_pg16` 整片 20 条红。


def test_extracting_the_constants_changed_no_sql():
    """重构对照:抽常量之后,两个 SQL 拼出来的**文本逐字节不变**。

    没有这一条,"只是抽了个常量"就只是一句自述 —— 拼接顺序/缩进错一格,
    生成的 SQL 就变了,而上面两条锁都看不见。
    """
    from services.publication_stage_sources import (
        _ATTEMPTS_SQL,
        published_occurrence_predicate,
    )

    # [并车 2026-08-20 · 36 班] 期望文本随核实位翻转同步更新 —— 本判据守的是
    # 「常量拼进 SQL 逐字节一致」这个组合不变式,不是守旧谓词本身。
    expected_body = ("""       r.public_url,
       (r.submitted_content_snapshot_hash IS NOT NULL
        AND r.public_url_verification_state = 'verified')
  FROM publish_records r""")
    assert expected_body in _ATTEMPTS_SQL, (
        "抽常量把 body proof 那一段的文本改了(缩进/换行/顺序):\\n"
        + _ATTEMPTS_SQL[_ATTEMPTS_SQL.find("publish_records") - 400:][:800])

    got = published_occurrence_predicate("q.id")
    expected_arm = ("""    ) OR EXISTS (
        SELECT 1 FROM publish_records rx
          LEFT JOIN articles rax ON rax.id = rx.article_id
          LEFT JOIN topics rtx   ON rtx.id = rax.topic_id
         WHERE COALESCE(rax.quote_id, rtx.quote_id) = q.id
           AND rx.status = 'success'
           AND rx.public_url_verification_state = 'verified'
    )""")
    assert expected_arm in got, (
        "抽常量把 occurrence 臂的文本改了:\\n" + got[-600:])
    # 绑定仍然是真的绑上了(不是留着占位符去打库)
    assert "{q}" not in got and re.search(r"\bq\.id\b", got)


# ═══════════════════════════════════════════════════════════════════
# 跨包闸的**判别力**:它对本包这两条臂到底管不管用
# ═══════════════════════════════════════════════════════════════════

#: 文章自报包 `tests/article_self_report_2026_08_19/…::_scan_unguarded_arms`
#: 用的臂形状(逐字照抄,便于对照)。
_UPSTREAM_ARM_SHAPE = re.compile(
    r"FROM\s+publish_records\s+(?:AS\s+)?(\w+)(?P<body>.{0,900}?)(?:\)|\Z)",
    re.IGNORECASE | re.DOTALL)
_UPSTREAM_RAW_SUCCESS = re.compile(r"\.status\s*=\s*'success'", re.IGNORECASE)
_UPSTREAM_VERIFIED = re.compile(
    r"public_url_verification_state\s*=\s*'verified'", re.IGNORECASE)


def test_our_occurrence_arm_carries_the_verified_bit():
    """[并车 2026-08-20 · 36 班 · 清单件 2 翻转] 臂已带核实位,且要一直带着。

    并车前这条判据的名字是
    ``test_the_cross_package_gate_does_not_actually_cover_our_occurrence_arm`` ——
    它断言"上游闸的旧形状抓不到我们的臂"(R4 时的非贪婪截断洞)。
    文章包 R5 已把闸形修好(括号平衡 + 无别名 + JOIN + 派生表),
    R6 又补了六种等价谓词;36 班并车把本包两条谓词换成核实位。
    于是它按自己的遗言翻转成现在这样:**断言臂上真的带着核实位**,
    与上游闸(它现在扫得到本文件引用的常量的真实出处)双向夹住。
    """
    from services.publication_stage_sources import (
        SELF_REPORT_BODY_PROOF_SQL,
        SELF_REPORT_OCCURRENCE_SQL,
    )

    arms = list(_UPSTREAM_ARM_SHAPE.finditer(SELF_REPORT_OCCURRENCE_SQL))
    assert arms, "臂形状匹配不到 FROM publish_records —— 结构锚对不上了,重看"
    naked = [m.group(0) for m in arms
             if _UPSTREAM_RAW_SUCCESS.search(m.group(0))
             and not _UPSTREAM_VERIFIED.search(m.group(0))]
    assert not naked, f"occurrence 臂丢了核实位(裸 success):{naked}"
    assert _UPSTREAM_VERIFIED.search(SELF_REPORT_OCCURRENCE_SQL), (
        "occurrence 臂里没有核实位谓词 —— 并车件 1 被回退了")
    # body proof:核实位取代来源位,且不许回退
    assert "public_url_verification_state = 'verified'" in SELF_REPORT_BODY_PROOF_SQL
    assert "public_url_reported_explicitly" not in SELF_REPORT_BODY_PROOF_SQL, (
        "body proof 回退成来源位了 —— 并车件 1 被回退了")


# [并车 2026-08-20 · 36 班] `test_the_exact_one_line_fix_is_recorded_verbatim` 退役:
# 它记录的「并车那天要做的逐字改法」已在 36 班并车提交里真实落地
# (occurrence 臂 + 核实位一行;body proof 来源位→核实位)。
# 它的两个 replace 锚点(尤其 `public_url_reported_explicitly IS TRUE`)
# 已随改法消失,继续保留只会恒红。其全部实质断言由上面翻转后的
# `test_our_occurrence_arm_carries_the_verified_bit` 以"断现状"的形式接管,
# 含「不加 COALESCE」——该形状仍由文章包跨包闸的结构锚约束。
def test_no_coalesce_wrapper_snuck_onto_the_verified_bit():
    """「不加 COALESCE」单独钉住:两处都是 FROM 表、列 NOT NULL DEFAULT,
    包一层 COALESCE 既无意义又会脱离上游闸的结构锚。"""
    from services.publication_stage_sources import (
        SELF_REPORT_BODY_PROOF_SQL,
        SELF_REPORT_OCCURRENCE_SQL,
    )
    assert "COALESCE(r.public_url_verification_state" not in SELF_REPORT_BODY_PROOF_SQL
    assert "COALESCE(rx.public_url_verification_state" not in SELF_REPORT_OCCURRENCE_SQL
