#!/usr/bin/env python
"""外选变异终单 V2(E2 12 + E3 15 = **27 发**)· 合流尖 f5417c561 · 窗口B 执行。

复用了什么、**没有**复用什么(2026-08-27 对抗审计订正)
------------------------------------------------------
**复用**:落盘/还原/在盘三字段/语法核/树锁 import 自
``mutation_runner_extsel_2026_08_26``;分母与库计划 import 自
``gate9_full_denominator_baseline``。

🔴 这里原来写的是「一个引擎,不是第三个……一行都不抄」——**那是超额声明**。
   我复用的是**机械**,没有复用**裁定闸**:引擎里专门用来区分「没跑」与
   「跑了没红」的两道闸(绿数守恒 / 钝杀分类)v2 一道都没接,
   ``base_green`` / ``green`` / ``brk`` 三个量算了、写进 JSON、**没有一个参与裁定**。
   对抗审计挖出来后已补齐,见 ``_verdict``。

三条纪律(Review 终单点名)
--------------------------
① **全分母必须带 pkgF / w4** —— EXTE3-04/14 的「恰 1 红」长在那两个包里,
   缺包会把两发**误记存活**。机械反查还发现 **w2** 同样必须在
   (MUT-EXTE3-11 的那条红长在 w2),已一并入分母。
   🔴 2026-08-27 Review 令①再收编 p03_settlement / p03c(MUT-EXTE2-04 的
   杀手判据长在前者)⇒ 分母 11 -> **13 包**。历史读数里的「11 包」一律指旧分母。
② **054 那发(MUT-EXTE3-10)文件 + 真库双打**:还原文件之后还要
   DROP 冷库模板 + 重装 + ``pg_get_constraintdef`` 核定义 ——
   本仓 2026-08-26 实测过"文件还原了、库里弱定义还留着"。
③ **SURVIVES 一律以全分母为准**:包作用域的快速红集只用来分流,
   任何一发要落 "存活",必须在**当轮全分母**(现 18 包)上再跑一遍仍然零红。
   (EXTE2-05 若 woc 红 = 杀 —— 终单亲裁。)

报数三栏:E2 / E3 / 汇总。每发带**在盘三字段**(anchor_hits · sha 前后 · 读回)。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ROOT))


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_E = _load("_v2_engine", "scripts/mutation_runner_extsel_2026_08_26.py")
_B = _load("_v2_base", "scripts/gate9_full_denominator_baseline.py")
from mutation_tree_lock import tree_lock  # noqa: E402
import mutation_replay_common as _RC      # noqa: E402  (内容级残留自证单点)

OUT = ROOT / ".gate9"
EXTRACT = ROOT / ".tiprun" / "extsel_v2.json"

# ══════════════════════════════════════════════════════════════════════════
# 显式 amendment —— 草单里写成人话的那 5 处
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 抽取器对这 5 发标了 NEEDS_AMENDMENT(「整行删除」/ 带省略号的替换 /
#:    锚与替换数配不上)。**抽不出来不许猜**,只能在这里显式补,并写明理由 +
#:    补出来的逐字内容。每一条都在树上复核 count == 1 之后才允许跑。
V2_AMENDMENTS: dict[str, dict] = {
    "MUT-EXTE2-05": {
        "why": "草单写「锚同 MUT-EXTE2-02 那一行」(散文引用),抽取器拿不到锚。"
               "逐字取自 services/diagnosis_sample_contract.py:314。",
        "file": "services/diagnosis_sample_contract.py",
        "pairs": [{
            "from": "    return max(1, min(total, (total * succeeded) // planned))",
            "to": "    return max(1, min(total, -((-total * succeeded) // planned)))",
        }],
    },
    "MUT-EXTE2-09": {
        "why": "草单替换写作「整行删除(替换为空)」—— 是指令不是代码。"
               "落成 to='' (删掉整行内容,留空行;list 字面量内合法)。",
        "file": "services/defensive_geo/activation_materializer.py",
        "pairs": [{
            "from": '                ("funding_policy", draft.funding_policy),',
            "to": "",
        }],
    },
    "MUT-EXTE3-07": {
        "why": "两处配对替换,其中锚 b 的替换是 fenced 块(不是 `→ 反引号` 形态),"
               "抽取器只配到 1 个替换。两处同上同还,视为一发。",
        "file": "api/defensive_geo_assist_api.py",
        "pairs": [
            {"from": '            "VALIDATION_FAILED",',
             "to": '            "POLICY_UNAVAILABLE",'},
            {"from": '        ) from exc\n    except LegalRepairNotApplied as exc:',
             "to": '        ) from exc\n        _safe_error("VALIDATION_FAILED")\n'
                   '    except LegalRepairNotApplied as exc:'},
        ],
    },
    "MUT-EXTE3-08": {
        "why": "草单替换写作「删除该行(PACKAGE_MIGRATIONS 三条 → 两条)」——"
               "是指令不是代码。落成 to=''。本发按工单点名打**判据底座**。",
        "file": "tests/defgeo_e3_2026_08_26/conftest.py",
        "pairs": [{
            "from": '    "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",',
            "to": "",
        }],
    },
    "MUT-EXTE3-10": {
        "why": "锚 a 的替换草单写作 `... tenant_owner_user_id >= 0);`(带省略号,"
               "不是逐字)。按锚 a 原文逐字展开;锚 b 抽取器已给出逐字对。"
               "两处配对 = 一发,还原两处同还。",
        "file": "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",
        "pairs": [
            {"from": "            CHECK (tenant_owner_user_id IS NULL "
                     "OR tenant_owner_user_id > 0);",
             "to": "            CHECK (tenant_owner_user_id IS NULL "
                   "OR tenant_owner_user_id >= 0);"},
            {"from": "(tenant_owner_user_id > 0)))",
             "to": "(tenant_owner_user_id >= 0)))"},
        ],
    },
}

# ══════════════════════════════════════════════════════════════════════════
# 每发的**快速**作用域(只用来分流,不用来定存活)
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 来源 = 草单「- 预测:」点名的判据 → ``grep -rl "def <名>" tests/`` 实测所在包。
#:    快集只为省时间;**任何一发要落存活,都必须再过当轮全分母**(现 18 包 · 终单裁定)。
QUICK_SCOPE: dict[str, tuple[str, ...]] = {
    "MUT-EXTE2-01": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-02": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-03": ("tests/defgeo_funding_p0_2026_08_25",),
    # 🔴 收编后:杀手判据在 p03_settlement,快集必须带上它,否则快集仍报存活。
    "MUT-EXTE2-04": ("tests/defgeo_funding_p0_2026_08_25",
                     "tests/p03_settlement_2026_08_24"),
    "MUT-EXTE2-05": ("tests/defgeo_funding_p0_2026_08_25",
                     "tests/defgeo_woc_closure_2026_08_25"),
    "MUT-EXTE2-06": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-06b": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-07": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-07b": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-08": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-09": ("tests/defensive_geo_pkge_2026_08_24",),
    "MUT-EXTE2-10": ("tests/defensive_geo_pkge_2026_08_24",),
    "MUT-EXTE2-11": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE2-12": ("tests/defgeo_funding_p0_2026_08_25",),
    "MUT-EXTE3-01": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-02": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-03": ("tests/defgeo_e3_2026_08_26",
                     "tests/defensive_geo_pkgf_2026_08_23"),
    "MUT-EXTE3-04": ("tests/defensive_geo_pkgf_2026_08_23",
                     "tests/defgeo_e3_2026_08_26"),
    "MUT-EXTE3-05": ("tests/defgeo_e3_2026_08_26",),
    # 重锚后的新 id 沿用同一作用域 —— 重锚只动锚,不动"谁该守它"。
    "MUT-EXTE3-05b": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-06": ("tests/defgeo_woc_closure_2026_08_25",),
    "MUT-EXTE3-07": ("tests/defgeo_woc_closure_2026_08_25",),
    "MUT-EXTE3-07b": ("tests/defgeo_woc_closure_2026_08_25",),
    "MUT-EXTE3-08": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-09": ("tests/defgeo_e3_2026_08_26",),
    #: [#91 · 2026-09-06 重锚] 新旧两把键都要在 —— QUICK_SCOPE 归 post-rename,
    #: 而重锚在**尚未生效的尖**上会回落旧 id(见 _apply_reanchors 的三态)。
    "MUT-EXTE3-09b": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-10": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-10b": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-11": ("tests/defensive_geo_w2_2026_08_21",
                     "tests/defgeo_e3_2026_08_26"),
    "MUT-EXTE3-12": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-13": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-14": ("tests/defensive_geo_w4_2026_08_22",
                     "tests/defgeo_e3_2026_08_26"),
    "MUT-EXTE3-15": ("tests/defgeo_e3_2026_08_26",),
    "MUT-EXTE3-15b": ("tests/defgeo_e3_2026_08_26",),
}

#: 全分母 = 基线脚本那张清单(同一张表,不另抄一份;现 18 包)。
#: 🔴 读**钉了大小的清单**,不再读 ``ENV_PLAN`` 的键。
#:    两者本来就应该相等,而 ``assert_denominator_frozen`` 每轮开跑会核这件事
#:    ——「相等」要有人验,不能靠"反正是同一张表"。
FULL = tuple(sorted(_B.GATE9_DENOMINATOR))

#: 🔴 [终单令②] 打迁移文件的发,还原之后必须**真库双打**:
#:    DROP 冷库/模板 + 让下一发重装 + 核定义。
#:    只 diff 文件不够 —— 弱定义会留在库里,而库比文件更晚被人看见。
DB_DOUBLE_HIT: dict[str, dict] = {
    "MUT-EXTE3-10": {
        "dbs": ("geo_defgeo_e3_tpl_test", "geo_defgeo_e3_cold_test"),
        "verify_db": "geo_defgeo_e3_cold_test",
        "verify": ("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                   "WHERE conname = 'chk_monitoring_run_cells_tenant_owner_positive'"),
        "must_contain": ("> 0",),
        "must_not_contain": (">= 0",),
    },
    "MUT-EXTE3-09": {
        "dbs": ("geo_defgeo_e3_tpl_test", "geo_defgeo_e3_cold_test"),
        "verify_db": None,          # 051 是索引,冷库由 e3 包自己重建后核
    },
    #: 🔴 [#91 · 2026-09-06 重锚] 本表归 post-rename,而它唯一的读点是
    #:    `DB_DOUBLE_HIT.get(mid)` —— **漏键静默返回 None**,那一发从此不打真库
    #:    而产物看起来一切正常(本文件 ID_TABLE_RENAME_PHASE 里已写明这一点)。
    #:    所以重锚必须**同笔**补新 id,不能等它报错(它不会报)。
    "MUT-EXTE3-09b": {
        "dbs": ("geo_defgeo_e3_tpl_test", "geo_defgeo_e3_cold_test"),
        "verify_db": None,
    },
    "MUT-EXTE3-10b": {
        "dbs": ("geo_defgeo_e3_tpl_test", "geo_defgeo_e3_cold_test"),
        "verify_db": "geo_defgeo_e3_cold_test",
        "verify": ("SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                   "WHERE conname = 'chk_monitoring_run_cells_tenant_owner_positive'"),
        "must_contain": ("> 0",),
        "must_not_contain": (">= 0",),
    },
}


#: 🔴 [V9-B · Codex fof7 P1-2] `FLAKE_ADJUDICATIONS` **已删**,两条理由都是机械查出来的:
#:    ① **只声明不消费** —— 全仓 grep 只有那一处赋值,没有任何读点。
#:       一张自称「带证据才允许改终态」的表,如果没人读它,它改不了任何终态,
#:       却让读代码的人以为终态可以被它改。这是仪器在说谎。
#:    ② **而且已经过期** —— 它唯一那条 entry 的键是 `MUT-EXTE3-15`,
#:       而该发 2026-08-28 已重锚为 `MUT-EXTE3-15b`,**不在本轮清单里**。
#:       接线它等于把一次改判应用到一个不存在的发上。
#:    「带证据改终态」这件事本身仍然成立,但它的正确形态是**走 Review 裁定 + 台账**,
#:    不是在 runner 里放一张没人读的字典。真要回来,必须同批带上消费点与判据。
#:
#: 🔴 **零判别力**定性(存活的一种,但性质不同:草单预测它会被杀)。
#: 🔴 [V9-B] 下面这张同样**未被消费**(全仓只有赋值)。与上面那张的区别是:
#:    它的键仍在本轮清单里,而且它记的是一次**已完成的定性**(留档价值真实)。
#:    所以不删,改为**显式登记**在 `DOC_ONLY_TABLES` 里并配判据:
#:      · 未消费的裁定类表必须在名单里且写理由(不许无声躺着冒充可执行);
#:      · 它的键必须仍在本轮清单里(不许像上面那张一样悄悄过期)。
DOC_ONLY_TABLES: dict[str, str] = {
    "ZERO_DISCRIMINATION":
        "留档一次已完成的定性(草单预测被杀 / 实跑证明该判据对本形态零判别力),"
        "不参与任何终态计算;键必须仍在清单内,由判据守着不许过期",
    #: 🔴 [2026-09-04 · #53] 补登记。它零读点已久,却一直**不在本名单里** ——
    #:    正是上面 218 行写着「不许无声躺着冒充可执行」的那件事。
    #:    为什么以前没人发现:这条规矩**承诺过配判据,而判据从没写** ——
    #:    `DOC_ONLY_TABLES` 自己也是零读点。治「只声明不消费」的那张表,
    #:    自己就是只声明不消费;而它的存在会让人**停止检查**
    #:    (读到「凡未消费的表必须登记在此」,自然默认「没登记的就是没有」)。
    #:    判据已补:`tests/wob39_collection_scope_2026_09_03/test_doc_only_tables_contract.py`,
    #:    **分母是 AST 机械枚举出的零读点表全集,不是本名单自身**。
    "DENOMINATOR_ADJUDICATIONS":
        "留档一次已完成的**分母洞**定性(MUT-EXTE2-04:杀手判据仓里早有,"
        "只是当时不在 11 包分母内;2026-08-27 Review 令①收编 p03_settlement / p03c 之后,"
        "该发已走正常裁定路)。留档价值在于**谱系**:把「分母洞」与「判据洞」分开记,"
        "否则下次会派人去写一条**已经存在**的判据。不参与任何终态计算。",
}

ZERO_DISCRIMINATION: dict[str, dict] = {
    "MUT-EXTE3-11": {
        "criterion": ("tests/defensive_geo_w2_2026_08_21/test_wp4_confirm_quote_wiring_pg.py"
                      "::test_e4_confirm_lands_every_irreproducible_acceptance_fact"),
        "why": (
            "草单预测「被杀,恰 1 条」就是这一条。w2 已在分母里且**真的跑了**"
            "(绿数守恒:268/268),那条判据也跑了、也过了。"
            "定向双臂:带/不带变异**全绿** ⇒ 它对这条轴**零判别力**,"
            "是判据洞,不是「我没跑到它」。"),
    },
}

#: 🔴 **分母洞**定性:在**当时的 11 包**分母下存活,但仓里已有杀手判据、只是不在分母里。
#: ⚠️ 2026-08-27 Review 令①**已收编** p03_settlement / p03c ⇒ 这张表的唯一条目
#:    MUT-EXTE2-04 从此走正常裁定路(它的杀手包已在分母内),本表保留只为留档谱系。
#:
#: 这与「判据洞」必须分开报:把分母洞报成判据洞,会派人去写一条**已经存在**的判据。
#: 进这张表的门槛 = 决定性双臂实证(不带变异该包全绿 / 带变异该包红),
#: 且臂A 必须看到 passed 数 > 0(防「它其实是 skipped」那一脚)。
DENOMINATOR_ADJUDICATIONS: dict[str, dict] = {
    "MUT-EXTE2-04": {
        "killer_package": "tests/p03_settlement_2026_08_24",
        "killer_nodes": [
            "test_closeout_p03b.py::test_multi_pool_with_snapshot_settles_per_pool_in_the_frozen_order",
            "test_closeout_p03b.py::test_reversed_order_in_snapshot_moves_the_money_differently(判别力自证)",
        ],
        "why": (
            "那条判据**逐池**断言钱包增量(bonus 退 138 / paid 整池退 350),"
            "docstring 里逐字写着「如果 order 被反过来(paid 先),数字会是 paid 退 188 / bonus 退 300 ——"
            "只断言总额对是分不出这两种的」。而分母内的 a4/a1 都是**池间求和**后才断言,"
            "两道形态守卫判的又都是 sorted(order),反转不改 sorted ⇒ 全都够不到。"),
        "evidence": (
            "决定性双臂(P03_TEST_DSN 指向自有冷建库 55488/p03_g9_test):"
            "臂A 不带变异 **32 passed / rc=0**(证明包真跑了、非 skip);"
            "臂B 带变异 **2 failed / 30 passed / rc=1**,失败项含该逐池判据。"
            "在盘实证 sha 22a0a7c2de0c ≠ fd2924eafe08,还原逐字节一致。"),
        "verdict": "SURVIVED_IN_11PKG_BUT_KILLED_OUTSIDE",
    },
}

# ══════════════════════════════════════════════════════════════════════════
# 终账(ledger) —— 每发接下来归谁   [Review 令 · 2026-08-27]
# ══════════════════════════════════════════════════════════════════════════
#: 🔴 **报数 ≠ 终账**。
#:    报数回答"这一轮**测出了什么**"(杀 / 存活 / 分母洞);
#:    终账回答"这一发**接下来归谁**"(结了 / 等复放 / 已闭合)。
#:
#: MUT-EXTE2-04 是这条区分的靶子:它在**当时的 11 包**分母里确实存活(报数为真),
#: 但它**不是待办** —— 杀手判据仓里已经有
#: (``tests/p03_settlement_2026_08_24/test_closeout_p03b.py`` 逐池断言 + 判别力自证),
#: 决定性双臂坐实(臂A 32 passed 证明真跑非 skip / 臂B 2 failed)。
#: 它的处方是**补分母**,不是补判据。留在存活栏里,下一步就会有人被派去
#: 写一条**已经存在**的判据 —— 那正是「同一个谓词写两处,必有一处没人验」的入口。
LEDGER_KILLED = "杀 · 已结"
LEDGER_REPLAY = "待复放(A/C 合流后收账)"
LEDGER_CLOSED_OUTSIDE = "已闭合 · 分母外已有杀手判据(处方=补分母)"

_VERDICT_TO_LEDGER: dict[str, str] = {
    "KILLED": LEDGER_KILLED,
    "KILLED_BLUNT": LEDGER_KILLED,
    "KILLED_BY_WIDER_DENOMINATOR": LEDGER_KILLED,
    "KILLED_BY_WIDER_DENOMINATOR_BLUNT": LEDGER_KILLED,
    "SURVIVED_FULL": LEDGER_REPLAY,
    "SURVIVED_IN_11PKG_BUT_KILLED_OUTSIDE": LEDGER_CLOSED_OUTSIDE,
}

#: 复放清单钉大小。A/C 合流后要复放的发数 —— 变了就停机:
#: 要么某发定性变了(该有人知道),要么我记错了(更该有人知道)。
REPLAY_LIST_SIZE = 11


def ledger_of(verdict: str) -> str:
    """裁定 → 终账。**未知裁定一律停机**,不许落进"其它"。"""
    if verdict not in _VERDICT_TO_LEDGER:
        raise SystemExit(
            f"🔴 裁定 {verdict!r} 没有对应的终账栏 —— 新裁定必须同批决定它归谁,"
            "不许悄悄落进兜底")
    return _VERDICT_TO_LEDGER[verdict]


def build_replay_list(recs: list[dict]) -> list[dict]:
    """机械导出「A/C 合流后要复放的发」+ 钉大小 + 闭合自证。

    复放清单必须是**导出的**不是抄的:抄一次少一项,而少的那一项不会让
    任何判据变红 —— 它只会安静地不复放。
    """
    replay = [r for r in recs if ledger_of(r["verdict"]) == LEDGER_REPLAY]
    if len(replay) != REPLAY_LIST_SIZE:
        raise SystemExit(
            f"🔴 复放清单 {len(replay)} 发 ≠ 钉死的 {REPLAY_LIST_SIZE} 发。"
            "要么某发定性变了,要么钉值该改而没改 —— 两种都必须有人看一眼。")
    buckets = {LEDGER_KILLED: 0, LEDGER_REPLAY: 0, LEDGER_CLOSED_OUTSIDE: 0}
    for r in recs:
        buckets[ledger_of(r["verdict"])] += 1
    if sum(buckets.values()) != len(recs):
        raise SystemExit(f"🔴 终账不闭合:{buckets} 合计 != {len(recs)}")
    return [{"id": r["id"], "family": r["family"], "file": r["file"],
             "quick_scope": r.get("quick_scope"),
             "why_replay": "旧 11 包全分母下存活 —— 待 A/C 合流后在新分母上复放收账"}
            for r in sorted(replay, key=lambda x: x["id"])]


def _write_results(res_path: Path, results: list[dict]) -> None:
    """产物**机械 LF** 写出(B-3 = Codex P2-3)。

    🔴 `Path.write_text` 在 Windows 上按平台默认换行 ⇒ 写出 CRLF,
       `git diff --check` 1299 条警告。产物是**跨机器被读的证据**,
       行尾跟着写它的那台机器走,等于给"两台机器读出不同 diff"留口子。
       (本仓已经因为行尾吃过两次亏:预检绿实跑红 / 两轮 after-sha 对不上。)
    """
    res_path.write_text(json.dumps(results, ensure_ascii=False, indent=1),
                        encoding="utf-8", newline="\n")


def _results_path() -> Path:
    """产物落哪儿。**部分跑必须换文件** —— runner 是整份覆盖写的。

    🔴 复放只跑 12 发时若仍写回 `extsel_v2_results.json`,那 27 条记录会被
       12 条**整份替换**:终账立刻不闭合,而"另外 15 发不见了"与"它们没跑过"
       在产物里长得一模一样。上一轮的定性还得留着做「复放前 / 复放后」两态。
    """
    name = os.environ.get("V2_RESULTS_NAME", "extsel_v2_results.json")
    # 🔴 [B-2② ] 只跑一部分却写进**默认**结果文件 ⇒ 下一轮续跑读到它,
    #    半份冒充全份。2026-08-28 那次 18/27 的来路就是半份产物。
    partial = os.environ.get("V2_ONLY", "").strip() or os.environ.get("V2_FAMILY", "").strip()
    if partial and name == "extsel_v2_results.json":
        raise SystemExit(
            "🔴 V2_ONLY/V2_FAMILY 只跑一部分,却要写默认结果文件 —— "
            "半份产物会被下一轮当成全份。请显式设 V2_RESULTS_NAME 另存。")
    if "/" in name or "\\" in name:
        raise SystemExit(f"🔴 V2_RESULTS_NAME 只收文件名,不收路径:{name!r}")
    return ROOT / ".tiprun" / name


def _current_tip() -> str:
    """本轮工作树的尖。读数必须挂在尖上,否则"哪棵树跑出来的"没人守。"""
    import subprocess

    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"🔴 取不到 HEAD:{r.stderr.strip()}")
    return r.stdout.strip()


def resume_done_ids(results: list[dict], tip: str, res_name: str,
                    fp_by_id: dict[str, str], *,
                    targets_by_id: dict[str, tuple],
                    full_targets=FULL, mut_fp_by_id=None) -> set[str]:
    """断点续跑能认哪些旧记录 —— **尖对不上就停机**。

    🔴 [2026-08-28 实测被咬] 原来这里是 ``{r["id"] for r in results}``,
       只认 id。底从 dd440a6e6 换到 b1fa4f88b 之后重跑,产物文件还是上一轮那份,
       于是 12 发**全部跳过**,而"复放前/复放后"表照常打印 9 杀 3 存活 ——
       那 9 个"杀"是**上一个尖**的。坏消息形状的假数最难看出来:
       它不报错、不空、格式全对。

       静默重跑也不行:那会把"这份文件是别人的"这件事藏起来。
       所以是**停机**,由人显式挪走或改名。
    """
    if not results:
        return set()
    alien = sorted({(r.get("tip") or "(无尖)")[:12] for r in results
                    if r.get("tip") != tip})
    if alien:
        raise SystemExit(
            f"🔴 {res_name} 里的记录来自别的尖:{alien}(本轮尖 {tip[:12]})。\n"
            "   续跑缓存只认**同一个尖**的读数 —— 换尖之后旧读数会冒充本轮结果\n"
            "   (2026-08-28 实测:12 发全跳过,报表照样打印 9 杀 3 存活)。\n"
            "   请显式挪走或改名后重跑,不要让它被静默复用。")
    # 🔴 [门③ · B-1] 尖对上还不够,**判据指纹**也要对上。
    #    尖只说「哪棵树」,指纹才说「哪套判据」。criteria-only 的改动落进
    #    工作区而还没提交时,尖没变、判据已经变了 —— 旧读数照样会冒充本轮结果。
    # 🔴 [V4-B · Codex P1-4] 指纹按 **mutation id 逐发**比,不是单一全局指纹。
    #    27 发分属 9 个不同作用域,各有各的判据集合;拿一个指纹套所有发,
    #    等于再造一次"适用域 < 使用域":某个包的判据变了而别的没变时,
    #    单一指纹要么全拒(过严、把能续的也毙了)、要么全放(过松、正是这次的洞)。
    stale = []
    for r in results:
        # 🔴 用 `not in` 判归属,不用 `.get() is None` ——
        #    「键不存在」(不在本轮清单)与「值是 None」(清单里但指纹算不出)
        #    是两件事,混成一格就会把后者误报成前者。
        if r["id"] not in fp_by_id:
            raise SystemExit(
                f"🔴 {res_name} 里有本轮清单外的发 {r['id']!r} —— "
                "它的作用域指纹算不出来,不许当已跑过")
        want = fp_by_id[r["id"]]
        if r.get("criteria_fp") != want:
            stale.append((r["id"], (r.get("criteria_fp") or "(未绑)")[:12],
                          (want or "(未算出)")[:12]))
    if stale:
        _nl = chr(10)
        lines = "".join(f"{_nl}     {i}:记录 {got} ≠ 本轮 {exp}"
                        for i, got, exp in stale)
        raise SystemExit(
            f"🔴 {res_name} 里这些记录的**判据指纹**与本轮不符(或根本没绑):"
            f"{lines}{_nl}   判据变了,旧读数不作数,重跑。")
    # 🔴 [V7-B · Codex fof5 P1-1] 尖对上、指纹对上,还差最后一关:
    #    **这条缓存记录本身可不可信**。实测(纯机制反例,修前 10/10 放行):
    #    同尖同指纹的 KILLED 缓存里注入 null / "12" / 12.0 / True / 负数 /
    #    加法不平 / 空 map / 缺 target / 多 target / 缺字段,这里一律照收
    #    ⇒ 那一发被跳过重跑,坏读数直接进终局,而且它长着「已跑过」的样子。
    #    **「能续跑」不等于「这条记录可信」** —— 两件事,过去只查了前一件。
    meta_bad = [w for w in
                (validate_record_run_meta(r, targets_by_id.get(r["id"]),
                                          full_targets=full_targets,
                                          mut_fp_by_id=mut_fp_by_id)
                 for r in results) if w]
    if meta_bad:
        _nl2 = chr(10)
        raise SystemExit(
            f"🔴 {res_name} 里这些记录的 run_meta 不可信,不许当已跑过:"
            + "".join(f"{_nl2}     {w}" for w in meta_bad)
            + f"{_nl2}   停机 —— 不静默重跑(那会把「这份缓存是坏的」藏起来)。")
    return {r["id"] for r in results}


#: 🔴 **重锚**(Review 裁定照准,执行方不自行改锚)。承 EXTC-04→04b 判例:
#:    重锚**只动锚一条轴**,替换语义一字不动,并换一个新 id 留谱系。
#:
#: MUT-EXTE3-05 的旧锚 `                WHERE tenant_owner_user_id <= 0)`
#: 在 b1fa4f88b 上命中 **0** 次 —— 窗口C 的 89a514e36 把那行改写成 query_to_xml
#: 动态执行(修 to_regclass 挡不住计划期解析、-1 哨兵永远拿不到那个坑)。
#: 谓词仍在,只是挪进了字符串字面量。
#:
#: 台账:「2026-08-28 · B 尾单三件验收合流 @ fb0f07299」· 收口单①
#: 🔴 [2026-09-04 · #51] **按 id 索引的表,相对重锚的消费时机分类。**
#:
#:    `_apply_reanchors()`(本文件 567)把 `m["id"]` 从旧 id 换成新 id,
#:    唯一调用点在 `load_v2()` 里(959)。于是每张按 id 索引的表分两类:
#:      · **pre-rename**  —— 读点在 959 **之前**,拿到旧 id ⇒ 表按**旧** id 索引
#:      · **post-rename** —— 读点在 959 **之后** ⇒ 见下面那条铁律
#:
#:    🔴 **post-rename 的铁律不是「补新 id」,是「新旧两把键都要在」。**
#:       因为重锚是**三态**的(578-590):新锚在本尖靶文件里不唯一而旧锚唯一时,
#:       它打印「尚未生效」并 `continue` —— **id 保持旧的**跑完本轮。
#:       所以 959 返回后,同一发的 id 既可能是新的也可能仍是旧的,取决于本尖合没合过
#:       改靶文件的那笔。谁要是拿「它是 post-rename」当理由去**删旧 id**,
#:       会在重锚尚未生效的尖上当场炸 964 行。
#:
#:    分类由 `tests/wob39_collection_scope_2026_09_03/test_id_table_rename_phase_contract.py`
#:    守着,**分母是 AST 机械枚举出的全部顶层 id 索引表,不是本登记表自身** ——
#:    拿登记表当分母的话,没登记的那张永远在分母之外(`DOC_ONLY_TABLES` 现在就是这个处境,
#:    见 #53)。新增一张表而不分类 ⇒ 判据红,逼出一次决定。
ID_TABLE_RENAME_PHASE: dict[str, str] = {
    "V2_AMENDMENTS": (
        "pre-rename|读点 943 `V2_AMENDMENTS.get(m[\"id\"])` 在 959 之前 ⇒ 拿到的是旧 id。"
        "所以它**必须**按旧 id 索引;要求它补新 id 是错的。"
        "现状 `MUT-EXTE3-07` 有、`07b` 没有 —— **这是对的,别修**。"),
    "QUICK_SCOPE": (
        "post-rename|24 个读点全部在 959 之后(964/968 及各主循环),新旧两把键都要在。"
        "注意主读点 964 是 `not in` + SystemExit(**响的**);"
        "只有 690 与 1405 用 `.get(默认值)` 会静默,目前被 964 那道闸挡在前面 —— "
        "**顺序依赖,不是它们自己有牙**。"),
    "DB_DOUBLE_HIT": (
        "post-rename|全仓唯一读点 1733 `DB_DOUBLE_HIT.get(mid)`,mid 出自 load_v2() 之后的 muts。"
        "🔴 它用 `.get()` ⇒ 漏键**静默返回 None**,那一发从此不打真库而产物看起来一切正常。"
        "2026-09-04 我是被 QUICK_SCOPE 那道**吼**的闸拦住,才去机械枚举,才发现这张**不吭声**的。"),
    "ZERO_DISCRIMINATION": (
        "zero-read|全仓零读点,不参与重锚同步。已登记在 `DOC_ONLY_TABLES` 里(留档用)。"),
    "DENOMINATOR_ADJUDICATIONS": (
        "zero-read|全仓零读点,不参与重锚同步。🔴 **未**登记在 `DOC_ONLY_TABLES` —— "
        "正是本文件 218 行写着「不许无声躺着冒充可执行」的那件事。归 #53 处理,本单不动。"),
    "V2_REANCHORS": (
        "rule-source|它就是重锚规则本身,是同步的**来源**不是**对象**,不参与。"),
}


V2_REANCHORS: dict[str, dict] = {
    "MUT-EXTE3-09": {
        "new_id": "MUT-EXTE3-09b",
        "ledger": "2026-09-06 · Review 裁定照准(继任 2104672c0 / 29214ec48)",
        "why": (
            "旧锚 `usingbtree(provider_order_ref)where` 在本尖命中 0 ——"
            "它钉的是 readiness 对索引的**渲染文本**比对,而 `2104672c0`"
            "「迁移 readiness 改按**语义身份**核,不再比 pg_get_*def 的渲染文本」"
            "把那一层整个换掉了(`29214ec48` 又把身份里的列**序号**换成列**名**)。"
            "⇒ 被测代码合法变了,锚随之作废,不是残留、也不是尺子坏。"
            "重锚到同一层的**新表达**:生成块里那条身份串的 `cols=`。"
            "🔴 语义一字不改 —— 变异意图仍是「把期望里的索引键改成错的那一列」"
            "(旧:`provider_order_ref` → `publish_command_id`;新:同一对列名),"
            "所以 test_e3_01 那条「预置错键索引 ⇒ 期望==实际 ⇒ 不 RAISE」照旧成立。"
            "锚点普查限靶文件:`cols=provider_order_ref|am=btree` 在 051 里命中 1。"),
        "file": "db/migration_051_defgeo_publish_settlement_guards_2026_08_25.sql",
        "pairs": [{
            "from": "cols=provider_order_ref|am=btree",
            "to": "cols=publish_command_id|am=btree",
        }],
        "expect_red_superset": (
            "test_e3_01_051_refuses_a_same_named_index_on_the_wrong_key",
            "test_e3_02_051_accepts_the_exact_index",
            "test_e3_03_051_from_scratch_still_builds_the_index",
        ),
    },
    "MUT-EXTE3-10": {
        "new_id": "MUT-EXTE3-10b",
        "ledger": "2026-09-06 · Review 裁定照准(继任 2104672c0 / 29214ec48)",
        "why": (
            "两处配对 = 一发(弱化 DDL **同时**弱化期望,使二者仍然一致 ⇒"
            "结构层不该 RAISE,必须由**行为层** test_e3_20 / test_e3_22 来杀)。"
            "锚 a(真 DDL 的 `CHECK ... > 0;`)本尖仍命中 1,**一字未动**;"
            "锚 b 旧写法 `(tenant_owner_user_id > 0)))` 命中 0 —— 它钉的是"
            "`pg_get_constraintdef` 的渲染文本,而 `2104672c0` 把期望改成了"
            "结构化身份串(`c|cols=...|lits=0|ops=>:1,is:1,null:1,or:1`)。"
            "重锚到该串里的 `ops=` 段:`>` → `>=`,与锚 a 的弱化**同向**,"
            "保持「DDL 与期望一致地被弱化」这个前提不变。"
            "🔴 编码位已核:仓内既有生成物有 `ops=<>:1,>=:1,and:1,...` 先例,"
            "`>=` 与 `>` 同位排序,所以弱化后的期望仍能与弱化后的真库对上。"
            "锚点普查限靶文件:两条新锚在 054 里各命中 1,替换串均不存在(不会 no-op)。"),
        "file": "db/migration_054_monitoring_cell_tenant_owner_2026_08_26.sql",
        "pairs": [
            {
                "from": "            CHECK (tenant_owner_user_id IS NULL OR tenant_owner_user_id > 0);",
                "to": "            CHECK (tenant_owner_user_id IS NULL OR tenant_owner_user_id >= 0);",
            },
            {
                "from": "ops=>:1,is:1,null:1,or:1",
                "to": "ops=>=:1,is:1,null:1,or:1",
            },
        ],
        "expect_red_superset": (
            "test_e3_20_054_refuses_a_same_named_weak_check",
            "test_e3_22_054_from_scratch_and_replay",
        ),
    },
    "MUT-EXTE3-05": {
        "new_id": "MUT-EXTE3-05b",
        "ledger": "2026-08-28 · B 尾单三件验收合流 @ fb0f07299(收口单①)",
        "why": (
            "旧锚在 fb0f07299 上命中 0 次(窗口C 89a514e36 改写成 query_to_xml 动态执行);"
            "谓词 <= 0 仍在,重锚候选在**靶文件**里唯一。"
            "🔴 锚点普查只限靶文件:同字面串在 scripts/mutation_replay_e3tail_2026_08_27.py"
            "的 _NEW_SENTINEL 里自指在场,那是工具在断言这个文件,不是第二个变异点。"),
        "file": "scripts/backfill_monitoring_cell_tenant_owner_2026_08_26.sql",
        "pairs": [{
            "from": "' WHERE tenant_owner_user_id <= 0', false, true, ''",
            "to": "' WHERE tenant_owner_user_id < 0', false, true, ''",
        }],
        #: 判别力**两向**:这两条必红,而"健康账本"那条必须保持绿 ——
        #: 只验"变红了"验不出判据是不是把所有账本都当成脏的。
        "expect_red_superset": (
            "test_e5_10_the_zero_census_sees_every_fabricated_tenant",
            "test_e5_11_a_bare_zero_alone_is_enough_to_stop_the_line",
        ),
        "must_stay_green": ("test_e5_12_a_healthy_ledger_reports_zero",),
    },
    "MUT-EXTE3-15": {
        "new_id": "MUT-EXTE3-15b",
        "ledger": "2026-08-28 · Review 裁定照准(V3-C 合流后 C-4 改写靶文件)",
        "why": (
            "V3-C 的 C-4(98ba7f156「attempt 绑 cutoff」)给取格的参数三元组"
            "加了 cutoff_at,原锚那两行的尾部形态变了,在 583b6cf7e 上命中 0。"
            "语义(sampling_window start/end 互换)一字不改,只动锚。"
            "锚点普查限靶文件。"),
        "file": "api/defensive_geo_report_api.py",
        "pairs": [{
            "from": """                         _window["sampling_window_start"],
                         _window["sampling_window_end"],""",
            "to": """                         _window["sampling_window_end"],
                         _window["sampling_window_start"],""",
        }],
        #: 预期红集**至少**含上一轮复放实测的那两条(精确 id,取自 replay 产物)。
        #: V3-C 若新增 e6 正样本臂/迟到 retry 臂而多红,照实记超集。
        "expect_red_superset": (
            "test_e6_02_the_window_is_bound_lower_bound_first",
            "test_e6_10_a_cell_inside_the_sampling_window_is_really_picked_up",
        ),
    },
    "MUT-EXTE3-07": {
        "new_id": "MUT-EXTE3-07b",
        "ledger": "2026-08-28 · Review 裁定照准(V3-C P2-2 新增同形臂 ⇒ 原锚命中 2)",
        "why": (
            "V3-C 的 P2-2 在 api/defensive_geo_assist_api.py 新增了一处 "
            "_safe_error(\"VALIDATION_FAILED\", reason_key=\"legal_repair_input_rejected\") ,"
            "与歧义臂(reason_key=\"legal_repair_ambiguous\")几乎同形,"
            "原锚从唯一变成命中 2。重锚 = 在原锚上多带一行 reason_key 消歧,"
            "语义一字不改(仍翻歧义臂那一位)。锚点普查限靶文件。"
            "锚1 未受影响(本尖实测仍命中 1、替换串不存在),但重锚整套替换 pairs,"
            "所以两条都在这里重新给 —— 不许只换出问题那条、假设另一条还活着。"),
        "file": "api/defensive_geo_assist_api.py",
        "pairs": [
            {"from": """            "VALIDATION_FAILED",
            reason_key="legal_repair_ambiguous",""",
             "to": """            "POLICY_UNAVAILABLE",
            reason_key="legal_repair_ambiguous","""},
            {"from": """        ) from exc
    except LegalRepairNotApplied as exc:""",
             "to": """        ) from exc
        _safe_error("VALIDATION_FAILED")
    except LegalRepairNotApplied as exc:"""},
        ],
        #: 预期红集 ⊇ 上一轮实测的三条(e2_22 取值规则 / e2_24 不可达语句 / e2_26 真 HTTP 422)。
        "expect_red_superset": (
            "test_e2_22_the_handler_really_maps_each_outcome_to_the_right_code",
            "test_e2_24_the_repair_handler_has_no_unreachable_statements",
            "test_e2_26_the_ambiguous_arm_is_a_non_retryable_422_over_real_http",
        ),
    },
    "MUT-EXTE2-06": {
        "new_id": "MUT-EXTE2-06b",
        "ledger": "08-28 B-4 起跑前锚检 · Review 裁定照准(A 的 70b2769ea 加 AND is_active)",
        "why": (
            "A 的 V4-A 给该 SQL 加了 AND is_active,原锚命中 0。"
            "语义一字不改:仍是摘掉 FOR SHARE(锁没了)。锚点普查限靶文件。"
            "🔴 必红集按判断**收窄**:上轮实测两条红,其中 "
            "test_e2_flipping_the_pool_policy_mid_confirm_cannot_change_which_pool_pays "
            "是 08-27 记过的并发**计时**判据、已知间歇报「没验到」。"
            "expect_red_superset 现在是硬门,把已知会抖的判据写进必红 = "
            "把抖动升级成硬失败,所以它不进门 —— 但在此写明,不当它不存在。"),
        "file": "api/defensive_geo_api.py",
        "pairs": [{
            "from": """        "FROM feature_pricing WHERE feature_code=%s AND is_active FOR SHARE",""",
            "to": """        "FROM feature_pricing WHERE feature_code=%s AND is_active",""",
        }],
        "expect_red_superset": (
            "test_e2_the_pricing_lock_locks_the_row_of_the_feature_it_was_asked_for",
        ),
    },
    "MUT-EXTE2-07": {
        "new_id": "MUT-EXTE2-07b",
        "ledger": "08-28 B-4 起跑前锚检 · Review 裁定照准(A 的 70b2769ea 加 AND is_active)",
        "why": (
            "同一处 SQL 加了 AND is_active,原锚(两行块)命中 0。"
            "语义一字不改:仍是把锁行的 feature_code 写死(跨 SKU 锁错行)。"),
        "file": "api/defensive_geo_api.py",
        "pairs": [{
            "from": """        "FROM feature_pricing WHERE feature_code=%s AND is_active FOR SHARE",
        (str(feature_code),),""",
            "to": """        "FROM feature_pricing WHERE feature_code=%s AND is_active FOR SHARE",
        ("geo_diagnosis",),""",
        }],
        "expect_red_superset": (
            "test_e2_the_pricing_lock_locks_the_row_of_the_feature_it_was_asked_for",
            "test_e2_the_pricing_lock_reads_the_row_of_the_feature_it_was_asked_for",
        ),
    },
}


def _apply_reanchors(muts: list[dict]) -> list[dict]:
    """按裁定表换锚 + 换 id。**不改条数**(27 还是 27),只改那一发的身份。"""
    for m in muts:
        ra = V2_REANCHORS.get(m["id"])
        if not ra:
            continue
        tgt = ROOT / ra["file"]
        if not tgt.exists():
            raise SystemExit(f"🔴 重锚靶文件不存在:{ra['file']}")
        text = tgt.read_text(encoding="utf-8")
        # 🔴 三态,且每一态都**响**:
        #   ①新锚唯一 ⇒ 重锚生效;②新锚不在但旧锚唯一 ⇒ 大声说「尚未生效」并按旧锚跑;
        #   ③两个都不唯一 ⇒ 停机。
        #   重锚常常是为**别的尖**裁定的(C 改了文件、我的树还没合过来)。
        #   无条件套新锚会把能跑的弄成不能跑;静默回落旧锚会把漂移藏起来。
        new_ok = all(text.count(pr["from"]) == 1 for pr in ra["pairs"])
        old_ok = all(text.count(pr["from"]) == 1 for pr in m["pairs"])
        if not new_ok:
            if old_ok:
                print(f"⚠️  重锚 {m['id']} → {ra['new_id']} **尚未生效**:"
                      "新锚在本尖靶文件里不存在,旧锚仍唯一 ⇒ 本轮按**旧锚**跑。"
                      f"(裁定台账:{ra['ledger']};到 integration 尖上会自动切过去)")
                continue
            raise SystemExit(
                f"🔴 {ra['new_id']} 新锚与旧锚在本尖**都不唯一命中** —— 两头落空,停机报 Review")
        for pr in ra["pairs"]:
            if text.count(pr["to"]):
                raise SystemExit(
                    f"🔴 {ra['new_id']} 的替换串已在靶文件里存在 —— 变异会变成 no-op")
        m["reanchored_from"] = m["id"]
        m["id"] = ra["new_id"]
        m["file"] = ra["file"]
        m["pairs"] = [dict(p) for p in ra["pairs"]]
        m["reanchor_ledger"] = ra["ledger"]
        m["reanchor_why"] = ra["why"]
        m["expect_red_superset"] = list(ra.get("expect_red_superset", ()))
        m["must_stay_green"] = list(ra.get("must_stay_green", ()))
        print(f"🔁 重锚 {m['reanchored_from']} → {m['id']}(台账:{ra['ledger']})")
    return muts


#: 🔴 [B-1 · Codex P1-7] **裁定 → 是否算"已解决"**。
#:    只有杀是解决;存活 / 语法崩 / 包跑不起来 / 绿数没守恒 / 未知,一律**未解决**。
#:    未解决还给 rc=0,等于"有洞"和"全杀"在 CI 眼里长得一模一样。
RESOLVED_VERDICTS = frozenset({
    "KILLED", "KILLED_BLUNT", "KILLED_BY_WIDER_DENOMINATOR",
    "KILLED_BY_WIDER_DENOMINATOR_BLUNT",
})
#: 已知但**未解决**的裁定 —— 认识它、但它必须让退出码非零。
UNRESOLVED_VERDICTS = frozenset({
    "SURVIVED_QUICK", "SURVIVED_FULL", "SURVIVED_IN_11PKG_BUT_KILLED_OUTSIDE",
    "INVALID_SYNTAX", "PACKAGE_BROKEN_NEEDS_REVIEW", "GREEN_NOT_CONSERVED",
    "REANCHOR_EXPECT_VIOLATED",
    "EXPECTED_KILLER_NOT_RUN",
    "RUN_META_UNUSABLE",
    # 🔴 [V9-B · P1-2] 快集判存活、全分母判红,而新红**全在 QUICK 内** ——
    #    同一批判据两次结果不一致。它是「未解决」,不是「被更宽分母杀了」。
    "REPLAY_DISAGREES_WITH_QUICK",
})


#: 🔴 [fof8 P1-1 · 2026-08-31] 指纹**版本**。改它 ⇒ 全部旧缓存作废(这正是要的)。
#:    版本单独入 payload 且单独落进记录:只靠「指纹不等」也能拒,但报文会说不清
#:    到底是定义变了还是口径变了 —— 而这两件事的处置不同(前者重跑那一发,
#:    后者重跑全部)。
FP_VERSION = 2

#: **进指纹**的 roster 键 —— 「这一发是什么变异」+「用什么标准验收它」。
#: 🔴 分母不是我手写的一句「影响裁定的字段」:它与 `FP_EXEMPT_KEYS` 的并集
#:    必须**逐字等于树内 roster 真正带的键全集**(判据 `test_fp_13` 机械核)。
#:    新加一个 roster 字段而没决定它进不进指纹 ⇒ 那条判据先红,逼人做一次决定。
FP_KEYS = ("family", "file", "pairs", "expect_red_superset",
           "must_stay_green", "needs_amendment")

#: **不进指纹**的 roster 键,每条带理由。判据钉大小,防它变成垃圾桶。
#: 🔴 判准只有一条:**改了它,这一发已有的读数还作不作数?**
#:    作数 ⇒ 豁免(否则改一句散文就要全量重跑,过严的闸迟早被人关掉);
#:    不作数 ⇒ 必须进指纹。
FP_EXEMPT_KEYS: dict[str, str] = {
    "id": "指纹表的**键**本身。把它塞进 payload 会让「两发定义完全相同必须撞指纹」"
          "这条判据恒真(id 天然不同),等于把那道闸关掉。",
    "predict": "草单**先验**,只参与「预测 vs 实得」对账报表,不参与任何裁定;"
               "改它不影响这一发的读数还作不作数。",
    "amended": "amendment 的散文说明。真正影响跑什么的是 pairs,它已进指纹。",
    "reanchor_ledger": "重锚台账出处(散文)。锚本身在 pairs 里,已进指纹。",
    "reanchor_why": "重锚理由(散文)。同上。",
    "reanchored_from": "重锚前的旧 id(溯源)。改它不改跑什么、也不改怎么验收。",
}
FP_EXEMPT_SIZE = 6

#: 每个进指纹的键**怎么标准化** —— 显式一张表,不靠 isinstance 猜。
#: `pairs` 保序(替换逐对按序应用,换序就是另一份定义);
#: 集合语义的三个排序(顺序不是定义的一部分)。
_FP_NORM = {
    "family": lambda v: v,
    "file": lambda v: v,
    "pairs": lambda v: [[p.get("from"), p.get("to")] for p in (v or [])],
    "expect_red_superset": lambda v: sorted(v or ()),
    "must_stay_green": lambda v: sorted(v or ()),
    "needs_amendment": lambda v: sorted(v or ()),
}


def mutation_fp_of(mut: dict) -> str:
    """一发变异的**定义指纹** —— 标准化后取 sha256。

    🔴 [V9-B · Codex fof7 P1-4] 修之前,缓存只绑 tip + 判据指纹 + 结构 + 语义,
       **没有一样东西绑「这一发到底是什么变异」**。

    🔴🔴 [fof8 P1-1] 上一版 payload 只有 file/pairs/scope/expect ——
       `must_stay_green` **参与裁定**(`check_reanchor_expectations` 读它:
       must_stay_green ∩ 实际红 必须为空)却**不在指纹里**。
       Codex 反例:只改 `must_stay_green`,新旧指纹同为 `130c11e…`,
       新验收定义已经会判 `REANCHOR_EXPECT_VIOLATED`,而旧记录照样进
       `resume_done`,validator 返 null,终门 rc=0 ——
       **旧证据证明了一份它从没跑过的验收定义**。

    修法不是「再补一个字段」(下一个字段还会漏),而是把**分母机械化**:
    payload 由 `FP_KEYS` 逐键生成,而 `FP_KEYS ∪ FP_EXEMPT_KEYS` 必须
    逐字等于树内 roster 的键全集 —— 新加字段没决定归属,判据先红。
    """
    payload = {k: _FP_NORM[k](mut.get(k)) for k in FP_KEYS}
    #: 作用域不是 roster 字段(来自 QUICK_SCOPE),单列进 payload。
    payload["scope"] = sorted(QUICK_SCOPE.get(mut.get("id"), ()))
    payload["fp_version"] = FP_VERSION
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
_MUT_FP_CACHE: dict[str, str] | None = None


def mutation_fp_by_id() -> dict[str, str]:
    """本轮清单的 id → 定义指纹。惰性算一次(`load_v2()` 会打重锚台账,不宜反复调)。"""
    global _MUT_FP_CACHE
    if _MUT_FP_CACHE is None:
        # 🔴 [#91 · 2026-09-06] 这张表**必须保持单射**:`test_fp_02_every_shot_in_the_
        #    roster_has_a_distinct_definition` 要求 27 发的定义指纹两两不同。
        #    我第一版在这里给重锚的旧 id 加了别名(旧→新同值),rmg_21 的 KeyError 是好了,
        #    fp_02 当场红 —— 别名让表不再单射。
        #    ⇒ 旧 id 的解析属于**消费方**的事(读历史产物的那一个),不是这张表的事。
        #    用 `reanchor_alias()` 在调用点转。
        _MUT_FP_CACHE = {m["id"]: mutation_fp_of(m) for m in load_v2()}
    return _MUT_FP_CACHE


def reanchor_alias() -> dict[str, str]:
    """**旧 id → 新 id**。历史冻结产物用旧 id,当前名册用新 id。

    只给读历史产物的消费方用。不要把它并进 `mutation_fp_by_id()` ——
    那张表要保持单射(fp_02 要求 27 发定义两两不同),掺别名当场红。
    """
    return {old: ra["new_id"] for old, ra in V2_REANCHORS.items()}


def criteria_fp_of(targets) -> str:
    """本轮这组包的**判据指纹** —— 结果绑它,才知道这条读数是在哪套判据下拿到的。

    🔴 指纹机制本仓 2026-08-26 就有(mutation_tree_lock.criteria_fingerprint),
       但 V2 一直没接:于是"判据变了"这件事在结果里**没有任何痕迹**,
       换一套判据重跑出来的读数和旧读数在产物里长得一模一样。
    """
    from mutation_tree_lock import criteria_fingerprint

    fp, _meta = criteria_fingerprint(list(targets))
    return fp


def assert_baseline_clean(base_red, where: str, res_path=None) -> None:
    """门① 基线有红 ⇒ **立即非零退出,且零落盘**。

    🔴 为什么必须零落盘:半份结果比没有更毒 —— 它带着正确的格式、正确的字段,
       只是每一条的 `new_red = red - base_red` 都被基线红**减掉过**。
       实测(2026-08-28):把两条杀手判据在基线里打红,
       同一发确认的杀当场变成 SURVIVED_QUICK 且 rc=0。
    """
    if not base_red:
        return
    if res_path is not None and res_path.exists():
        raise SystemExit(
            f"🔴 {where} 基线有红 {sorted(base_red)},而结果文件 {res_path.name} 已存在 ——"
            "先挪走它,再修基线。半份结果比没有更毒。")
    raise SystemExit(
        f"🔴 {where} 基线有红({len(base_red)} 条):{sorted(base_red)}\n"
        "   红基线下 `new_red = red - base_red` 会把**本该被杀**的发减成零红存活"
        "(2026-08-28 实测一发即中),所以这里不再只打印 —— 停机,零落盘。")


def assert_full_fp_current(results, full_fp: str, res_name: str) -> None:
    """[V5-B · B-1-1] 走过**全分母臂**的记录,必须绑当轮的全分母判据指纹。

    🔴 实测的洞:全分母段续跑只把记录喂给 ``resume_done_ids``,而那道门核的是
       **quick** 指纹。于是改掉一个「只在全分母里、不在该发快集作用域里」的包的
       判据之后:尖没变、quick 指纹没变 ⇒ 旧记录全部放行 ⇒ 上一轮的
       ``KILLED_BY_WIDER_DENOMINATOR`` 原样复用 ⇒ ``surv`` 为空 ⇒
       零存活早退 ⇒ rc 0。**过期的杀证冒充本轮杀证**,且四个信号全部正常。

    「走过全分母臂」按**字段前缀**机械判(任一 ``full_`` 开头的键),
    不按裁定名的手写清单 —— 手写清单漏掉的那一项不会让任何判据变红,
    而全分母臂将来多写一个字段是很自然的事。
    """
    stale = []
    for r in results:
        if not any(k.startswith("full_") for k in r):
            continue                       # 只跑过快集的记录,还没轮到这道门
        got = r.get("full_criteria_fp")
        if got != full_fp:
            stale.append((r.get("id"), (got or "(未绑)")[:12], full_fp[:12]))
    if not stale:
        return
    _nl = chr(10)
    lines = "".join(f"{_nl}     {i}:记录 {g} ≠ 本轮 {w}" for i, g, w in stale)
    raise SystemExit(
        f"🔴 {res_name} 里这些记录走过**全分母臂**,但绑的**全分母判据指纹**"
        f"与本轮不符(或根本没绑):{lines}{_nl}"
        "   全分母判据变了,旧的 KILLED_BY_WIDER_DENOMINATOR / SURVIVED_FULL "
        "不作数 —— 否则它们会让 surv 变空、从零存活早退口拿走 rc 0。")


def assert_unique_roster(muts, where: str) -> None:
    """[V5-B · B-1-2] 应跑清单必须 **id 唯一** —— 计数对不代表清单对。

    🔴 实测的洞(纯机制反例已复现):清单里一条重复 + 一条缺失时,
       ``len(muts)==27`` 过关;``expected = {m["id"] for m in muts}`` 把重复
       折成 26;续跑让重复那发只产出一条记录 ⇒ 26 条结果对 26 个应跑 ⇒
       集合比、计数比、重复比**全过**,rc 0。
       而真正少跑的那一发,在任何一处都不出现。
    """
    seen, dup = set(), []
    for m in muts:
        rid = m.get("id")
        (dup.append(rid) if rid in seen else seen.add(rid))
    if dup:
        raise SystemExit(
            f"🔴 {where}应跑清单里有**重复 id**:{sorted(set(dup))}"
            f"(共 {len(muts)} 条 / 唯一 {len(seen)} 个)——\n"
            "   重复会把应跑集合折小,于是「一条重复配一条缺」在集合比、计数比、"
            "重复比里**全部看不见**,而少跑的那一发不会让任何判据变红。")


def assert_selector_hits(only, fam, roster, matched) -> None:
    """[V5-B · B-1-4] ``V2_ONLY`` / ``V2_FAMILY`` 的**请求集合 vs 命中集合**对账。

    🔴 实测的洞:拼错一个 id ⇒ 过滤后 ``muts`` 为空 ⇒ ``expected=set()`` ⇒
       硬门里 ``if expected`` 为假、完整性检查整段跳过 ⇒
       打印「0 发全部为「杀」(⚠️ 未核完整性)」并返回 0。
       **分母为 0 不是通过,是探针没打着** —— 本仓老规矩,这次长在自己身上。
    """
    ids = {m["id"] for m in roster}
    fams = {m["family"] for m in roster}
    bad_ids = sorted(set(only) - ids)
    bad_fams = sorted(set(fam) - fams)
    if bad_ids or bad_fams:
        raise SystemExit(
            f"🔴 部分跑点名了清单里没有的东西:id={bad_ids} 家族={bad_fams} ——\n"
            f"   清单里现有 {len(ids)} 个 id / 家族 {sorted(fams)}。"
            "拼错的名字会静默变成「跑 0 发」,而 0 发在旧硬门下拿的是退出码 0。")
    if not matched:
        raise SystemExit(
            f"🔴 部分跑选择器命中 **0 发**(only={sorted(only)} family={sorted(fam)})"
            " —— 名字都对但交集为空,同样不许当成一轮跑过。")
    print(f"部分跑对账:请求 only={sorted(only) or '(全部)'} "
          f"family={sorted(fam) or '(全部)'} ⇒ 命中 {len(matched)} 发")


def gate_rc(*, empty_expected, unresolved, unknown, missing, extra, dup,
            meta_bad) -> int:
    """门② 的**纯裁决** —— 七路输入任一非空/为真 ⇒ 1。

    🔴 [V7-B · Codex fof5 P1-1] 抽出来的理由与 V6-B 的 `baseline_rc` 同:
       上一版 `hard_gate_rc` 里是 `return 0` / `return 1` 两个**字面量**,
       于是「某一路没进退出码」这种毒改不掉任何字符串、也不动任何位置,
       判据全绿而闸已经不接线。裁决抽成纯函数之后,三把锁才有地方打:
       ① 每一路单独驱动 rc→1 的正样本;② 数据流锁(实参回溯到真正的计算);
       ③ 入口禁字面量 return。
    """
    return 1 if (empty_expected or unresolved or unknown or missing or extra
                 or dup or meta_bad) else 0


def hard_gate_rc(results, where: str, *, expected, targets_by_id,
                 full_targets=FULL, mut_fp_by_id=None) -> int:
    """门② 任一未解决裁定 ⇒ 非零退出;**且该跑的发必须都在、读数必须可信**。

    🔴 [2026-08-28 终放当场暴露] 原来只查"有没有坏裁定"。
       MUT-EXTE3-07 锚命中 2 次 ⇒ 快集相按纪律停机(rc=1,对的);
       可第二趟对着**只有 18 条**的半份产物打印
       「18 发全部为「杀」,退出码 0」—— 18 条确实全是杀,应有 **27**。
       **「全是杀」不等于「该跑的都跑了」**:分母没人守时,
       「全绿」在一个悄悄变小的分母上永远成立。

    🔴 [V7-B · Codex fof5 P1-1] 这一版再补一格:**「都跑了」也不等于「读数可信」**。
       终门过去只看 verdict / 缺发 / 多发 / 重复,**一次都没读过 run_meta**。
       实测:27 条全 KILLED、集合两向都对,而每条 run_meta 是 null / 空 map /
       加法不平,终门照样 rc=0。三层是三件事,缺一层就有一整类假绿:
       裁定对了吗 → 该跑的都在吗 → **这些读数本身可信吗**。
    """
    empty_expected = not expected
    if empty_expected:
        # 🔴 [B-1-4] 空的应跑集合:它会让下面每一项完整性检查都恒真。
        #    「0 发全部为「杀」」是这条洞过去的原话。
        print(f"🔴 硬门({where}):应跑集合为**空** —— "
              "「0 发全部为「杀」」不是通过,是分母没了(选择器拼错 / 清单被过滤空)")
    unresolved, unknown, dup, missing, extra, meta_bad = [], [], [], [], [], []
    if not empty_expected:
        for r in results:
            v = r.get("verdict")
            if v in RESOLVED_VERDICTS:
                continue
            (unresolved if v in UNRESOLVED_VERDICTS else unknown).append(
                (r.get("id"), v))
        # 🔴 [B-2①] 重复 ID:集合比与计数比**都看不出**「一条重复 + 一条缺」
        #    —— 集合相等、数目相等,而实际少跑了一发。
        seen = set()
        for r in results:
            rid = r.get("id")
            (dup.append(rid) if rid in seen else seen.add(rid))
        missing = sorted(set(expected) - {r.get("id") for r in results})
        extra = sorted({r.get("id") for r in results} - set(expected))
        # 🔴 兜底覆盖四路(快集 / 全分母 / 零存活早退 / 续跑),一条都不许绕过。
        meta_bad = [w for w in
                    (validate_record_run_meta(r, targets_by_id.get(r.get("id")),
                                              full_targets=full_targets,
                                              mut_fp_by_id=mut_fp_by_id)
                     for r in results) if w]
    rc = gate_rc(empty_expected=empty_expected, unresolved=unresolved,
                 unknown=unknown, missing=missing, extra=extra, dup=dup,
                 meta_bad=meta_bad)
    if rc == 0:
        print(f"✅ 硬门({where}):{len(results)} 发全部为「杀」"
              f"(覆盖应跑 {len(expected)} 发,run_meta 逐条可信),退出码 0")
    else:
        for i, v in unresolved:
            print(f"🔴 未解决:{i} → {v}")
        for i, v in unknown:
            print(f"🔴 未知裁定:{i} → {v!r} —— 新裁定必须同批决定它算不算解决")
        if missing:
            print(f"🔴 这些发**根本没跑到**({len(missing)}/{len(expected)}):{missing}")
            print("   「全是杀」不等于「该跑的都跑了」—— 截断的一轮不许拿 0")
        if extra:
            print(f"🔴 产物里有不该出现的发:{extra}")
        if dup:
            print(f"🔴 产物里有**重复 ID**:{sorted(set(dup))} —— "
                  "一条重复配一条缺,集合与计数都看不出来")
        for w in meta_bad:
            print(f"🔴 run_meta 不可信:{w}")
        print(f"🔴 硬门({where})不通过:未解决 {len(unresolved)} · 未知 {len(unknown)}"
              f" · 缺发 {len(missing)} · 多发 {len(extra)} · 重复 {len(dup)}"
              f" · run_meta 坏 {len(meta_bad)}"
              " —— 退出码非零(「有洞」不许和「全杀」共用退出码 0)")
    return rc


def _admin_dsn() -> str:
    return f"{_B.BASE}/postgres"


def _drop_dbs(names) -> list[str]:
    import psycopg2
    done = []
    conn = psycopg2.connect(_admin_dsn())
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            for n in names:
                cur.execute("SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                            "WHERE datname=%s AND pid<>pg_backend_pid()", (n,))
                cur.execute(f'DROP DATABASE IF EXISTS "{n}" WITH (FORCE)')
                done.append(n)
    finally:
        conn.close()
    return done


def _constraint_def(db: str, sql: str) -> list[str]:
    import psycopg2
    conn = psycopg2.connect(f"{_B.BASE}/{db}")
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def load_v2() -> list[dict]:
    """机械枚举 27 发 —— 抽取产物 + 显式 amendment 合并。"""
    if not EXTRACT.exists():
        raise SystemExit(f"🔴 缺 {EXTRACT} —— 先跑 scripts/extsel_v2_extract.py")
    muts = json.loads(EXTRACT.read_text(encoding="utf-8"))
    for m in muts:
        am = V2_AMENDMENTS.get(m["id"])
        if am:
            m["file"] = am.get("file", m["file"])
            m["pairs"] = [dict(p) for p in am["pairs"]]
            m["amended"] = am["why"]
            m["needs_amendment"] = []
    still = [m["id"] for m in muts if m["needs_amendment"]]
    if still:
        raise SystemExit(f"🔴 这些发抽不出来又没有 amendment:{still} —— 停下,不猜")

    n_e2 = sum(1 for m in muts if m["family"] == "E2")
    n_e3 = sum(1 for m in muts if m["family"] == "E3")
    print(f"机械枚举对账:E2={n_e2} · E3={n_e3} · 合计={len(muts)}")
    if (n_e2, n_e3, len(muts)) != (12, 15, 27):
        raise SystemExit(f"🔴 对账不上:E2={n_e2} E3={n_e3} 合计={len(muts)},终单是 12/15/27")

    muts = _apply_reanchors(muts)
    # 🔴 [B-1-2] 唯一性必须核在**重锚之后** —— 重锚会改 id(06 → 06b),
    #    只在重锚前核只证明抽取产物干净,证不了重锚没跟既有 id 撞车。
    assert_unique_roster(muts, "27 发")

    missing_scope = [m["id"] for m in muts if m["id"] not in QUICK_SCOPE]
    if missing_scope:
        raise SystemExit(f"🔴 这些发没有快速作用域:{missing_scope}")
    # 快集里出现的包必须都在全分母里 —— 否则那一发的红根本没机会出现。
    bad = sorted({t for m in muts for t in QUICK_SCOPE[m["id"]] if t not in FULL})
    if bad:
        raise SystemExit(
            f"🔴 快集点名了不在全分母里的包:{bad} —— "
            "缺包会把该发**误记存活**(终单执行令①正是这一条)")
    return muts


#: 钝杀阈:红占**本作用域**基线绿数的比例超过它 ⇒ 杀成立,但红集不可用于定位。
BLUNT_KILL_RED_FRACTION = 0.5


def _run_targets(targets, tag: str) -> tuple[set[str], dict[str, int], list[str]]:
    """跑一组包,返回(红节点名集合, **逐包**绿数, 出问题的包)。

    🔴 绿数按包返回,不返回总数:绿数守恒必须**按作用域**比。
       实测快集基线绿 = 986(7 包),而一发的作用域绿 = 86 或 170 ——
       拿总数比会把每一发都判成"没守恒"。
       分母对不齐的判据比没有判据更坏:它会把 27 发全染红,然后被当成"太严"关掉。
    ``tag`` 让每一发的 junit/log 各自落盘,不覆盖基线那一份。
    """
    red: set[str] = set()
    green: dict[str, int] = {}
    broken: list[str] = []
    meta: dict[str, dict] = {}
    status: dict[str, str] = {}
    # 🔴 [V9-B · Codex fof7 P1-2] 红集**按包**留一份。
    #    原来只并成一个扁平集合 ⇒ 「这条红是哪个包出的」这件事当场丢掉,
    #    而 KBWD 的全部正当性正是「杀来自 QUICK 之外的包」——
    #    没有归属就没法验它,只能靠「全分母比快集宽」这句话本身。
    red_by_target: dict[str, list[str]] = {}
    for t in targets:
        rec = _B.run_target(t, tag=tag, collect=False)
        if "error" in rec:
            broken.append(f"{t}:{rec['error']}")
            continue
        _rn = sorted(set(rec.get("red_nodes") or []))
        red |= set(_rn)
        red_by_target[t] = _rn
        green[t] = rec["passed"]
        # 🔴 [V6-B · B-4] 原来这里把 collected / skipped **当场丢掉**,
        #    于是「判据被 skip 了」和「判据跑了但没红」在上层长得一模一样 ——
        #    Codex 反例:缺 P03 schema 时 MUT-EXTE2-04 的两条杀手被 skip,
        #    该发零新增红 ⇒ 记 SURVIVED_QUICK,而整轮 rc=0。
        # 🔴 [Codex fof4 P1] 这里原来写 `rec.get("collected")` —— 而变异臂调的是
        #    `run_target(..., collect=False)`,那一路**根本不跑 --collect-only**,
        #    `collected` 恒 None。于是产物里 33 条 run_meta 的 collected 全是 null,
        #    而我的判据只查了「字典里有这个键名」⇒ **只写不读的字段,锁住的是个空值**。
        #    改成从**已解析的 junit** 派生(零额外成本):`tests` 属性 = 该包这一轮
        #    真正出现的 testcase 数。同批把三分量也记下,好让「加起来对不对」可核。
        _tests = int(rec.get("tests") or 0)
        _passed, _red = int(rec.get("passed") or 0), int(rec.get("red") or 0)
        _skipped = int(rec.get("skipped") or 0)
        meta[t] = {"collected": _tests,          # = junit 里实际出现的 testcase 数
                   "passed": _passed, "red": _red, "skipped": _skipped,
                   "skipped_nodes": list(rec.get("skipped_nodes") or ())}
        # 🔴 [V6-B-final 实跑咬到] 用例名要**剥掉参数后缀**再归并 ——
        #    junit 里是 `…_it_was_asked_for[social_diagnosis]`,
        #    而 expect_red_superset 登记的是裸名(老门 check_reanchor_expectations
        #    一直是 `.split("[")[0]`)。不剥的话「跑了而且红了」会被读成「没跑」,
        #    实跑里 MUT-EXTE2-07b 就是这么被我自己的新门误判的。
        #    一个裸名可能对应多个参数实例,按**优先级**归并:
        #    只要有一例 failed/error 就算「跑了且抓住」,全是 skipped 才算「没跑」。
        def _bare(nid: str) -> str:
            return nid.split("::")[-1].split("[")[0]

        for nid in (rec.get("skipped_nodes") or ()):
            status.setdefault(_bare(nid), "skipped")
        for nid in (rec.get("red_nodes") or ()):
            status[_bare(nid)] = "failed"
    return red, green, broken, {"per_target": meta, "status_by_name": status,
                                "red_by_target": red_by_target}


RUN_META_FIELDS = ("collected", "passed", "red", "skipped")


#: 「杀」类裁定 —— 它们的成立**必须**有红集支撑。
KILL_VERDICTS = frozenset({"KILLED", "KILLED_BLUNT"})
#: 「更宽分母下被杀」的两种终态 —— **钝与不钝各占一格**。
#: 🔴 [V9-B · Codex fof7 P2-2] 修之前只有不钝那一格:全分母那一轮若是钝杀,
#:    `_verdict` 返 KILLED_BLUNT,于是 `if v == "KILLED"` 整段被跳过 ——
#:    ① KBWD 的正当性检查(新红须来自 QUICK 之外)对钝杀从不执行;
#:    ② 记录顶着 KILLED_BLUNT 落盘,而它 quick 侧本就是存活形状(quick_red 空),
#:       quick 语义分支按 KILL_VERDICTS 要求它有红 ⇒ **合法的宽分母钝杀被误红**。
#:    两个毛病同一个根:钝这条轴没有自己的终态,被迫挤进快集那一格。
KBWD_VERDICTS = frozenset({"KILLED_BY_WIDER_DENOMINATOR",
                           "KILLED_BY_WIDER_DENOMINATOR_BLUNT"})
#: 终态 → 它是不是「钝」。分类要能**两向重算**,所以正反两张表都由这一张推。
BLUNT_VERDICTS = frozenset({"KILLED_BLUNT",
                            "KILLED_BY_WIDER_DENOMINATOR_BLUNT"})
#: 「这一发算被杀了吗」的**唯一**答案源。渲染层、报表层一律借这张,不各抄一份:
#: 手写 `("KILLED", "KILLED_BY_WIDER_DENOMINATOR")` 这种元组,新增一种杀之后
#: 不会有任何判据变红 —— 那一发只会静静地从「杀」那一栏里漏出去。
KILLED_FAMILY = KILL_VERDICTS | KBWD_VERDICTS


def is_blunt(red_total: int, base_green) -> bool | None:
    """按**这一侧的**作用域基线绿数重算钝杀 —— 唯一一处判据。

    返回 None 表示「算不出来」(基线绿缺失或坏型),调用方必须当拒,
    不许把「算不出」和「不钝」合成一格。
    """
    if type(base_green) is not int or base_green < 0:
        return None
    if not base_green:
        return False
    return red_total >= BLUNT_KILL_RED_FRACTION * base_green
#: 「存活」类裁定 —— 它们的成立**必须**没有新增红。
SURVIVE_VERDICTS = frozenset({"SURVIVED_QUICK", "SURVIVED_FULL"})


def _semantic_mismatch(rec: dict, red_total: int, passed_total: int,
                       label: str = "quick") -> str | None:
    """verdict ↔ 红集的**语义**一致性 —— 结构对不代表这条记录说得通。

    🔴 [V8-B · Codex fof6 P1-1] 上一版只验结构/类型/加法,从不问
       「这个裁定与它的红集配不配」。于是这种形状能一路过关:

           verdict=KILLED · quick_red=[] · run_meta 全 0(或 12/12/0/0 全 pass)

       而它是**语义不可能**的:`_verdict` 里 `if not new_red: return SURVIVED_*`,
       KILLED / KILLED_BLUNT 只在 `new_red` 非空时可达。
       Codex 反例实测:同尖同指纹的这种缓存,`resume_done_ids` 收下、
       `hard_gate_rc` 返 0,还打印「run_meta 逐条可信」。

    🔴 严格相等的依据(不是拍脑袋):快集基线由 `assert_baseline_clean` 强制零红,
       所以 `new_red = red - base_red` 里 `base_red` 为空 ⇒
       **`sum(run_meta.*.red) == len(quick_red)`** 是那道闸的推论。
       27 发真产物逐条实测:27/27 精确相等,`sum(passed) == quick_green` 同样 27/27。
       —— 这条等式一旦不成立,要么基线带红开跑了,要么这条记录是编的。
    """
    mid, v = rec.get("id"), rec.get("verdict")
    # 🔴 没有裁定 ⇒ 语义面无从谈起,直接放过。
    #    这不是放水:`assert_run_meta_sane` 那条路手上是「刚跑出来的 meta」,
    #    裁定要等 `_verdict` 算完才有 —— 在那里要求 quick_red 等于要求
    #    「还没发生的事已经写下来了」。结构面在上面已经验完,不受影响。
    if not v:
        return None
    rk, gk = ("quick_red", "quick_green") if label == "quick" else ("full_red", "full_green")
    # 🔴 这一侧「该不该有红」完全由 verdict 决定,两侧规则不同,分开写清楚。
    #    尤其 KILLED_BY_WIDER_DENOMINATOR:它在 **quick 侧是存活形状**
    #    (所以才轮到全分母复核),杀证在 full 侧 —— 第一版把这一格漏了。
    if label == "quick":
        needs_red = v in KILL_VERDICTS
        needs_none = (v in SURVIVE_VERDICTS) or (v in KBWD_VERDICTS)
    else:
        needs_red = v in KBWD_VERDICTS
        needs_none = v == "SURVIVED_FULL"
    if not (needs_red or needs_none):
        return None                      # 未解决类裁定:硬门自会拒,语义面不重复判
    reds = rec.get(rk)
    if not isinstance(reds, list):
        return f"{mid}: verdict={v} 却 {rk} 不是 list(是 {type(reds).__name__})"
    # 🔴 [V9-B · P2-3] 红集元素本身也要立得住:非 str 或有重复 ⇒ 后面「红数 == len」
    #    这条等式就可以被灌水灌平(塞两个相同 nodeid、或塞一个 None 凑数)。
    badel = [x for x in reds if type(x) is not str]
    if badel:
        return (f"{mid}: {rk} 里有非 str 元素 {badel[:3]} "
                f"(type={[type(x).__name__ for x in badel[:3]]})")
    if len(set(reds)) != len(reds):
        dupes = sorted({x for x in reds if reds.count(x) > 1})
        return f"{mid}: {rk} 里有重复 nodeid {dupes[:3]} —— 重复能把红数凑平"
    # 🔴 [V9-B · P1-1] green 字段:**按 verdict 强制存在**,不是「是 int 才比」。
    #    修之前 `if type(g) is int and g != passed_total` —— 缺失 / 字符串 / bool /
    #    浮点四种形态**全部跳过检查**(实测 4/4 放行),只有「错的整数」才被抓。
    #    「坏型就不检查」是这一族最阴的写法:它让守卫在**最该说话的时候**闭嘴。
    if needs_red or needs_none:
        g = rec.get(gk)
        if gk not in rec:
            return f"{mid}: verdict={v} 却没有 {gk} —— 这一侧的绿数没人记"
        if type(g) is not int:
            return (f"{mid}: {gk}={g!r} 不是 int(type={type(g).__name__})"
                    " —— bool / 字符串 / 浮点一律拒,不许「坏型就不比」")
        if g < 0:
            return f"{mid}: {gk}={g} 是负数"
        if g != passed_total:
            return f"{mid}: {gk}={g} != {label} passed 总数 {passed_total}"
    if needs_red:
        if not reds:
            return (f"{mid}: verdict={v} 却 {rk} 为空 —— "
                    "杀只在新增红非空时可达,这形状语义不可能")
        if red_total != len(reds):
            return (f"{mid}: {label} 红总数 {red_total} != len({rk}) {len(reds)} "
                    "—— 基线零红时两者必须精确相等")
        # ── [V9-B · Codex fof7 P2-1] 钝 / 不钝**按数重算**,不信记录上的标签 ──
        #    钝杀是「杀成立但红集不能用来定位」,它决定这一发进哪一栏、
        #    Review 要不要另眼看它。而修之前它只是一个**跑那一刻算出来的标签**:
        #    缓存里把 KILLED_BLUNT 改成 KILLED(或反过来),没有任何一道闸会看一眼。
        #    重算所需的两样数就在记录里(这一侧的基线绿 + 这一侧的红数),
        #    所以「信标签」纯粹是没去算,不是算不了。
        bk = "scope_base_green" if label == "quick" else "full_scope_base_green"
        blunt = is_blunt(red_total, rec.get(bk))
        if blunt is None:
            return (f"{mid}: {bk}={rec.get(bk)!r} 缺失或坏型 —— 钝杀分类无从重算,"
                    "「算不出」不许和「不钝」合成一格")
        want_v = (("KILLED_BLUNT" if blunt else "KILLED") if label == "quick"
                  else ("KILLED_BY_WIDER_DENOMINATOR_BLUNT" if blunt
                        else "KILLED_BY_WIDER_DENOMINATOR"))
        if v != want_v:
            return (f"{mid}: verdict={v} 但按 {label} 侧读数重算应是 {want_v}"
                    f"(红 {red_total} · 基线绿 {rec.get(bk)} · 阈 "
                    f"{BLUNT_KILL_RED_FRACTION})")
        fk = "blunt" if label == "quick" else "full_blunt"
        if bool(rec.get(fk)) != blunt:
            return (f"{mid}: {fk}={rec.get(fk)!r} 与重算的钝杀结论 {blunt} 不符 "
                    "—— 标志位与终态必须同源")
    elif needs_none:
        if reds:
            return f"{mid}: verdict={v} 却 {rk} 非空 {reds[:3]} —— 有新增红就不是这个裁定"
        if red_total != 0:
            return f"{mid}: verdict={v} 却 {label} 红总数 {red_total}"
    return None


def validate_record_run_meta(rec: dict, expected_targets,
                             full_targets=None, mut_fp_by_id=None,
                             *, stage: str = "record") -> str | None:
    """🔴 [V7-B · Codex fof5 P1-1] run_meta 的**唯一**校验谓词。

    好 ⇒ None;坏 ⇒ 一句说明。三条路(新产物 / 续跑缓存 / 终门)都必须过它,
    **不许有第二份谓词** —— 同一谓词写两处,必有一处没人验。

    上一版 `assert_run_meta_sane` 的四个洞,逐个在这里关掉:

    1. **空 map 恒过** —— `for t, m in (meta or {}).items()` 在空 dict 上是空循环,
       `bad` 为空 ⇒ 返 None。分母为 0 时任何检查都恒真,这是本仓的老病。
       现在先比 target 集(两向),空集直接拒。
    2. **`isinstance(c, int)` 放过 `bool`** —— Python 里 `isinstance(True, int)` 为真,
       于是 `collected=True, passed=True, red=False, skipped=False` 能算出 1 == 1+0+0
       **加法都成立**。改用 `type(v) is int`。
    3. **`int(m.get(k) or 0)` 静默吞类型** —— `int("12")` / `int(12.0)` 都成功,
       于是字符串和浮点混进来不留痕。现在不做任何转换:先验类型,再直接做算术。
    4. **缺字段被当 0** —— `m.get(k) or 0` 把「没有这个键」和「值是 0」合成一格。
       现在 `k not in m` 单独拒。

    另加 ≥0:负数在四则里能凑平(-12 == -10 + -2 + 0),不查就永远发现不了。
    """
    mid = rec.get("id")
    meta = rec.get("run_meta")
    if not isinstance(meta, dict):
        return f"{mid}: run_meta 不是 dict(是 {type(meta).__name__})"
    want = set(expected_targets or ())
    if not want:
        return f"{mid}: 应跑 target 集为空 —— 空分母上任何检查都恒真,不许当通过"
    if not meta:
        # 🔴 单列一条(不与「缺 target」合并):空 map 是**分母整个没了**,
        #    而缺 target 是「跑了别的包」。两件事合成一格,报文就说不清是哪种,
        #    而空分母恰恰是本仓最常见的假绿源头 —— 它值得自己的一句话和自己的判据。
        return f"{mid}: run_meta 是**空 map** —— 空分母上任何逐项检查都恒真"
    got = set(meta)
    if want != got:
        return (f"{mid}: run_meta 的 target 集与应跑集对不上("
                f"缺 {sorted(want - got)} · 多 {sorted(got - want)})")
    for tgt in sorted(want):
        m = meta[tgt]
        if not isinstance(m, dict):
            return f"{mid}/{tgt}: run_meta 条目不是 dict(是 {type(m).__name__})"
        for k in RUN_META_FIELDS:
            if k not in m:
                return f"{mid}/{tgt}: 缺字段 {k}"
            v = m[k]
            # 🔴 `type(v) is int` 不是 `isinstance` —— bool 是 int 的子类。
            if type(v) is not int:
                return (f"{mid}/{tgt}: {k}={v!r} 不是 int"
                        f"(type={type(v).__name__})")
            if v < 0:
                return f"{mid}/{tgt}: {k}={v} 是负数"
        # 不做任何转换:上面已逐个验过是 int,这里直接算。
        parts = m["passed"] + m["red"] + m["skipped"]
        if m["collected"] != parts:
            return (f"{mid}/{tgt}: collected={m['collected']} != "
                    f"passed+red+skipped={parts}")
    #    🔴 [V9-B 修正] 适用门必须**显式说自己是哪一路**,不许拿替身信号。
    #    上一版写的是 `if not rec.get("verdict"): return None` —— 用「没有裁定」
    #    当「这是新产物那一路」的代理。它同时放过了另一种东西:**一条真缓存记录
    #    恰好没写 verdict**。那种记录 `resume_done_ids` 照样当「已跑过」收下,
    #    而 fp 校验被整条跳过。代理信号一向如此:它替你回答了一个你没问的问题。
    if stage not in ("record", "fresh"):
        return f"{mid}: stage={stage!r} 不是已知两态 —— 调用点没说清自己哪一路"
    if stage == "fresh":
        # `assert_run_meta_sane` 那条路手上是「刚跑出来的 meta」,
        # 裁定与 mutation_fp 都要等这一发跑完才写;在这里要求它们,
        # 等于要求「还没发生的事已经写下来了」。
        return None
    table = mutation_fp_by_id() if mut_fp_by_id is None else mut_fp_by_id
    if "mutation_fp" not in rec:
        return (f"{mid}: 没有 mutation_fp —— 旧格式缓存没绑变异定义,"
                "换掉锚/靶文件也看不出来,不许当已跑过")
    # 🔴 [fof8 P1-1] 版本单独核,排在指纹之前:版本变了 ⇒ **口径**变了
    #    (处置 = 全部重跑);指纹不等 ⇒ **这一发的定义**变了(处置 = 重跑这一发)。
    #    只靠「指纹不等」两种都能拒,但报文说不清是哪一种,而两者处置不同。
    got_ver = rec.get("mutation_fp_version")
    if "mutation_fp_version" not in rec:
        return (f"{mid}: 没有 mutation_fp_version —— 指纹口径未知的旧缓存,"
                f"不许当已跑过(本轮 v{FP_VERSION})")
    if type(got_ver) is not int or got_ver != FP_VERSION:
        return (f"{mid}: mutation_fp_version={got_ver!r} != 本轮 v{FP_VERSION} "
                "—— 指纹口径变了,**全部**旧读数作废")
    got_fp = rec.get("mutation_fp")
    # 🔴 连 hex 一起验:只验长度的话,`"g"*64` 这种也算「形状对」,
    #    最后被「不等于本轮」拒掉 —— 拒是拒了,但报的理由是错的,
    #    而且形状那一条从此没有任何输入能让它单独响(判据跟着变成恒绿)。
    if (type(got_fp) is not str or len(got_fp) != 64
            or any(c not in "0123456789abcdef" for c in got_fp)):
        return f"{mid}: mutation_fp={got_fp!r} 不是 64 位小写 sha256 十六进制串"
    want_fp = table.get(mid)
    if want_fp is None:
        return f"{mid}: 本轮清单里查不到它的变异定义指纹 —— 不许当已跑过"
    if got_fp != want_fp:
        return (f"{mid}: mutation_fp 记录 {got_fp[:12]} != 本轮 {want_fp[:12]} "
                "—— 变异定义变了(file/pairs/scope/expect 任一),旧读数不作数")
    # ── [V9-B · P1-3] 带 full 派生字段的缓存,**全分母判据指纹**也在这里核 ──
    #    原来只有 `run_full_survivors` 里 `assert_full_fp_current` 核一次,
    #    默认入口(快集/终门)对这类缓存不核 ⇒ 全分母判据变了它照样过。
    if any(k.startswith("full_") for k in rec):
        if full_targets:
            want_full = criteria_fp_of(tuple(full_targets))
            got_full = rec.get("full_criteria_fp")
            if got_full != want_full:
                return (f"{mid}: full_criteria_fp 记录 "
                        f"{(got_full or '(未绑)')[:12]} != 本轮 {want_full[:12]} "
                        "—— 全分母判据变了,旧的 full 读数不作数")
    # ── 结构过了,再问语义:这个裁定与它的红集配不配 ──────────────────
    why = _semantic_mismatch(rec, sum(meta[t]["red"] for t in want),
                             sum(meta[t]["passed"] for t in want), "quick")
    if why:
        return why
    # ── [V8-B · P2-1] 带 full_* 的记录,全分母那一侧同样两向都要过 ────
    has_full = any(k.startswith("full_") for k in rec)
    if rec.get("verdict") in KBWD_VERDICTS or has_full:
        fm = rec.get("full_run_meta")
        if not isinstance(fm, dict) or not fm:
            return (f"{mid}: 带 full_* 却没有 full_run_meta —— "
                    "「更宽分母下被杀」的证据面是空的")
        fwant = set(full_targets or ())
        if not fwant:
            return f"{mid}: 没给 full 应跑集,无从核 full_run_meta(空分母不算通过)"
        if set(fm) != fwant:
            return (f"{mid}: full_run_meta 的 target 集对不上("
                    f"缺 {sorted(fwant - set(fm))} · 多 {sorted(set(fm) - fwant)})")
        for tgt in sorted(fwant):
            fmm = fm[tgt]
            if not isinstance(fmm, dict):
                return f"{mid}/{tgt}: full_run_meta 条目不是 dict"
            for k in RUN_META_FIELDS:
                if k not in fmm:
                    return f"{mid}/{tgt}: full_run_meta 缺字段 {k}"
                fv = fmm[k]
                if type(fv) is not int:
                    return (f"{mid}/{tgt}: full_run_meta {k}={fv!r} 不是 int"
                            f"(type={type(fv).__name__})")
                if fv < 0:
                    return f"{mid}/{tgt}: full_run_meta {k}={fv} 是负数"
            if fmm["collected"] != fmm["passed"] + fmm["red"] + fmm["skipped"]:
                return f"{mid}/{tgt}: full_run_meta collected 加不平"
        why = _semantic_mismatch(rec, sum(fm[t]["red"] for t in fwant),
                                 sum(fm[t]["passed"] for t in fwant), "full")
        if why:
            return why
    return None


def assert_run_meta_sane(mid: str, meta: dict, expected_targets) -> str | None:
    """[Codex fof4 P1 / V7-B P1-1] 新产物那一路的入口 —— **只做适配,不做判断**。

    判断整个在 `validate_record_run_meta` 里。这里存在的唯一理由是:
    这条路手上是「刚跑出来的 meta」而不是「已经写好的 rec」,
    包一层比把 rec 提前造出来更小的改动面。
    """
    why = validate_record_run_meta({"id": mid, "run_meta": meta}, expected_targets,
                                   stage="fresh")
    if why:
        print(f"   ⛔ run_meta 对不上 junit:{why}")
        return "RUN_META_UNUSABLE"
    return None


def assert_expected_killers_ran(mid: str, mut: dict, status_by_name: dict) -> str | None:
    """[V6-B · B-4] 登记的预期杀手,**必须真的跑过**(collected 且未 skip)。

    🔴 与 ``check_reanchor_expectations`` 分工严格分开,两道门各管一件:
       · 这一道只问「**跑没跑**」—— skipped / 压根不在 junit 里 ⇒ 该发作废;
       · 那一道问「**红没红**」—— 跑了却没红是判别力问题,不归这里。
       合成一道的话,「没跑」会被写成「没抓住」,再被写成「变异存活」。
       Codex 反例就是这条链:缺 schema ⇒ 杀手 skip ⇒ 零红 ⇒ SURVIVED_QUICK ⇒ rc 0。
    """
    want = tuple(mut.get("expect_red_superset") or ())
    if not want:
        return None
    not_run = [w for w in want if status_by_name.get(w) not in ("failed", "error",
                                                                "passed")]
    if not_run:
        for w in not_run:
            st = status_by_name.get(w) or "(不在 junit 里)"
            print(f"   ⛔ {mid} 的预期杀手 {w} 状态={st} —— **没跑**,不是「没抓住」")
        return "EXPECTED_KILLER_NOT_RUN"
    return None


def check_reanchor_expectations(mid: str, mut: dict, red_nodes) -> str | None:
    """[B-2③] 重锚登记的预期红集 / 必绿集,**机械执行**。

    🔴 这两个字段本来只是注释:05b/07b/15b 三发的"预期对不对"全靠人肉核。
       **配置里写了却不执行 = 「登记过」冒充「守住了」** ——
       人肉核会随交接消失,而配置会一直在那儿装作有人守。

    两向都查(只验"变红了"验不出判据是不是把所有情形都当脏的):
      · expect_red_superset ⊆ 实际红;
      · must_stay_green ∩ 实际红 == ∅。
    """
    want_red = tuple(mut.get("expect_red_superset") or ())
    keep_green = tuple(mut.get("must_stay_green") or ())
    if not want_red and not keep_green:
        return None
    names = {n.split("::")[-1].split("[")[0] for n in red_nodes}
    missing = [w for w in want_red if w not in names]
    broke = [g for g in keep_green if g in names]
    if not missing and not broke:
        print(f"   ✅ 重锚预期机械核过:必红 {len(want_red)} 条全在"
              f"{f' · 必绿 {len(keep_green)} 条未被打红' if keep_green else ''}")
        return None
    if missing:
        print(f"   🔴 {mid} 登记的**必红**判据没红:{missing}")
    if broke:
        print(f"   🔴 {mid} 登记的**必绿**判据被打红了:{broke}"
              "(判别力另一向塌了 —— 变异把不该染的也染了)")
    return "REANCHOR_EXPECT_VIOLATED"


def _kbwd_outside_quick(mid: str, new_red, red_by_target: dict) -> set[str]:
    """新增红里,**落在 QUICK 作用域之外**的那些包。

    🔴 [V9-B · P1-2] 空集 ⇒ 这一发不配 KBWD。
       判定只用**本轮实测**的 `red_by_target`(哪个包出的哪条红),
       不用「FULL 比 QUICK 宽」这句话本身 —— 后者是定义,不是证据。
    """
    quick = set(QUICK_SCOPE.get(mid, ()))
    want = set(new_red)
    return {t for t, nodes in (red_by_target or {}).items()
            if t not in quick and (set(nodes) & want)}


def _verdict(rec: dict, new_red: list[str], green: dict[str, int],
             base_green: dict[str, int], scope, brk: list[str], *, quick: bool) -> str:
    """[对抗审计 C] 裁定闸 —— 不再只看「红集空不空」。

    四种「零红」长得一模一样,必须分开:
      · 包**根本没出数**(junit 缺 / rc 不是 0|1)⇒ 既不是杀也不是存活;
      · 语法不合法(出题/抽取错)⇒ INVALID,不是杀;
      · 红 0 但**绿数没守恒** ⇒ 有判据没跑,零红不成立;
      · 真的零红 ⇒ 才是存活。
    """
    want = sum(base_green.get(t, 0) for t in scope)
    got = sum(green.get(t, 0) for t in scope)
    # 🔴 [V9-B · P2-1] 两侧各写各的键。修之前两侧共用 `scope_*`,
    #    于是一条走过全分母的记录里,`scope_base_green` 是**全分母**的基线 ——
    #    而它名字看着像快集那一侧的。想按「这一侧的基线」重算钝杀分类时,
    #    快集那一侧的数已经被覆盖掉,重算无从谈起(共用一个格子 = 后写的赢)。
    pre = "" if quick else "full_"
    rec[pre + "scope_green"] = got
    rec[pre + "scope_base_green"] = want
    if brk:
        # 🔴 整包起不来 ≠ 存活:没有 junit 的包不贡献红,而零新增红正是存活的观测形态。
        #    也不直接判杀(可能是环境问题)。第三种结论,交 Review 按失败签名三件核。
        rec["broken_detail"] = brk
        print(f"   ⛔ 有包跑不起来:{brk} —— 记「需裁定」,**不记存活**"
              "(整包 error 常常正是最响亮的杀)")
        return "PACKAGE_BROKEN_NEEDS_REVIEW"
    if any(x.startswith("__SYNTAX__") for x in new_red):
        print(f"   ⛔ 变异后语法不合法 —— 记 INVALID(出题/抽取错),**不记杀**:{new_red}")
        return "INVALID_SYNTAX"
    if not new_red:
        if got < want:
            print(f"   ⛔ 零红但**绿数没守恒**(作用域绿 {got} < 基线 {want},少 "
                  f"{want - got} 条)—— 有判据没跑,零红不成立")
            return "GREEN_NOT_CONSERVED"
        print(f"   🟠 {'快集' if quick else '全分母'}存活(绿 {got})"
              + ("—— 按终单必须过全分母才算存活" if quick else ""))
        return "SURVIVED_QUICK" if quick else "SURVIVED_FULL"
    if is_blunt(len(new_red), want):
        rec[pre + "blunt"] = True
        print(f"   🟡 钝杀:红 {len(new_red)} 条 ≥ 作用域基线绿 {want} 的一半 —— "
              "杀成立,但**红集不可用于定位**,单独成栏")
        return "KILLED_BLUNT"     # 全分母那一侧由调用方改判 KBWD_BLUNT
    print(f"   ✅ 杀 · 红 {len(new_red)} 条:{new_red[:8]}")
    return "KILLED"


def _apply_pairs(path: Path, pairs: list[dict], mid: str) -> tuple[bytes, list[dict]]:
    """逐字节应用所有配对替换,每对各出一份在盘三字段。"""
    original = path.read_bytes()
    cur = original
    proofs = []
    for k, pr in enumerate(pairs):
        needle, nl = _E._fit_newlines_bytes(cur, pr["from"])       # noqa: SLF001
        repl = _E._fit_repl_bytes(cur, needle, pr["to"])           # noqa: SLF001
        n = cur.count(needle)
        if n != 1:
            path.write_bytes(original)
            raise SystemExit(
                f"🔴 {mid} 锚{k} 命中 {n} 次(要求 1)—— 停下报 Review,不自行改锚。\n"
                f"    file={path.relative_to(ROOT).as_posix()} 换行形态={nl}\n"
                f"    anchor={pr['from'][:200]!r}")
        before = cur
        cur = cur.replace(needle, repl, 1)
        if cur == before:
            path.write_bytes(original)
            raise SystemExit(f"🔴 {mid} 锚{k} 替换后零字节变化 —— 这是 no-op 不是变异")
        path.write_bytes(cur)
        # expected = 我刚刚写下去的那份字节。传了它,ok 就按**逐字节等价**判,
        # 不再依赖「替换不含锚」这个隐含前提(MUT-EXTE2-10 正是插入型)。
        pf = _E._on_disk_proof(path, anchor_hits=n, before=before,   # noqa: SLF001
                               needle=needle, repl=repl, expected=cur)
        pf["pair"] = k
        proofs.append(pf)
        if not pf["ok"]:
            path.write_bytes(original)
            raise SystemExit(f"🔴 {mid} 锚{k} 变异没有真的落盘:{pf}")
    return original, proofs


def run_full_survivors() -> int:
    """[终单裁定③] 快集存活的每一发,在**当轮全分母**(现 18 包)上重跑一遍。

    零红才落 SURVIVED_FULL;有红则改判 KILLED_BY_WIDER_DENOMINATOR
    并把红集记下来(EXTE2-05 若 woc 红 = 杀,终单亲裁)。
    """
    res_path = _results_path()
    if not res_path.exists():
        raise SystemExit("🔴 还没有快集结果 —— 先跑 27 发")
    results = json.loads(res_path.read_text(encoding="utf-8"))
    # 🔴 第二趟是**独立进程**,同样要先自证树干净:上一趟若死在还原上,
    #    这一趟会在残留之上再打一层变异,而两层叠加的读数没有任何东西在证。
    _assert_no_mutation_residue(load_v2())
    # 第二趟同样只认本尖的读数(否则会拿别的尖的快集结论去跑全分母)。
    resume_done_ids(results, _current_tip(), res_path.name,
                    {m["id"]: criteria_fp_of(QUICK_SCOPE[m["id"]])
                     for m in load_v2()},
                    targets_by_id={m["id"]: QUICK_SCOPE[m["id"]]
                                   for m in load_v2()})
    # 🔴 [B-1-1] 全分母指纹要在**这里**算、**这里**核:排在 `surv` 之后就没用了,
    #    因为过期杀证的出口正是「surv 为空 ⇒ 零存活早退 ⇒ rc 0」。
    full_fp = criteria_fp_of(FULL)
    assert_full_fp_current(results, full_fp, res_path.name)
    surv = [r for r in results if r["verdict"] == "SURVIVED_QUICK"]
    if not surv:
        print("快集零存活 —— 全分母复核无对象")
        # 🔴 [门② 旁路 · 我自己复核时挖到的] 这里原来是无条件 `return 0`。
        #    `surv` 只筛 SURVIVED_QUICK —— 产物里若有 INVALID_SYNTAX /
        #    PACKAGE_BROKEN_NEEDS_REVIEW / GREEN_NOT_CONSERVED,它们都不是
        #    SURVIVED_QUICK,于是 surv 为空、直接返 0,**门②整个被绕过**。
        #    「零存活」不等于「全杀」:前者只说没人待复核。
        return hard_gate_rc(results, "全分母复核(零存活早退)",
                            expected={m["id"] for m in load_v2()},
                            targets_by_id={m["id"]: QUICK_SCOPE[m["id"]]
                                           for m in load_v2()})
    muts = {m["id"]: m for m in load_v2()}

    print("═" * 74)
    print(f"全分母基线({len(FULL)} 包)")
    print("═" * 74)
    base_red, base_green, broken, _bmeta = _run_targets(FULL, "fullbase")
    print(f"全分母基线:绿 {sum(base_green.values())} · 红 {len(base_red)}")
    if broken:
        raise SystemExit(f"🔴 全分母基线有包跑不起来:{broken}")
    # 🔴 [门①] 全分母基线同样不许带红开跑。
    assert_baseline_clean(base_red, "全分母", None)

    print(f"全分母判据指纹 = {full_fp[:16]}")
    for r in surv:
        mid = r["id"]
        m = muts[mid]
        path = ROOT / m["file"]
        print("\n" + "─" * 74)
        print(f"{mid} 全分母复核({len(FULL)} 包)")
        original, proofs = None, []
        try:
            original, proofs = _apply_pairs(path, m["pairs"], mid)
            ok, err = _E._syntax_ok(path)                           # noqa: SLF001
            if not ok:
                red, green, brk, _rmeta = {f"__SYNTAX__::{err[:60]}"}, -1, [], {"status_by_name": {}}
            else:
                red, green, brk, _rmeta = _run_targets(FULL, mid.replace("MUT-", "") + "_full")
        finally:
            if original is not None:
                _E._restore(path, original)                         # noqa: SLF001
        new_red = sorted(red - base_red)
        r["full_on_disk_proof"] = proofs
        # 🔴 [V8-B · Codex fof6 P2-1] 全分母那一轮的 run_meta 原来**只在瞬时检查一次
        #    就丢掉**,不落盘。后果:一条带 `KILLED_BY_WIDER_DENOMINATOR + full_*`
        #    却**没有** full_run_meta 的缓存记录,续跑与终门都无从复核它的全分母读数
        #    —— 「更宽分母下被杀」这句话的证据面是空的。本轮 quick 零存活所以不阻断,
        #    但不留升级伏笔:现在就落。
        r["full_run_meta"] = _rmeta.get("per_target", {})
        r["full_red_nodes_by_target"] = _rmeta.get("red_by_target", {})
        r["full_red"] = new_red
        r["full_green"] = sum(green.values()) if isinstance(green, dict) else green
        r["full_broken"] = brk
        v = _verdict(r, new_red, green, base_green, FULL, brk, quick=False)
        # 🔴 [V9-B · Codex fof7 P2-2] 这里原来只接 `== "KILLED"`。
        #    全分母那一轮若判**钝杀**,`_verdict` 返 KILLED_BLUNT ⇒ 整段被跳过:
        #    ① KBWD 的正当性(新红须来自 QUICK 之外)对钝杀从不检查;
        #    ② 记录顶着快集那一族的终态落盘,而它 quick 侧 red 是空的
        #       ⇒ quick 语义分支把这条**合法的宽分母钝杀**误判成「杀却没红」。
        if v in KILL_VERDICTS:
            # 🔴 [V9-B · Codex fof7 P1-2] 「更宽分母下被杀」这句话的**全部正当性**是:
            #    杀来自 QUICK 之外的包。若新增红**全部**落在 QUICK 作用域内 ——
            #    那这一发在快集就该被杀,而快集判它存活 ⇒ 两次读数自相矛盾。
            #    那不是「分母更宽所以抓到了」,是**同一批判据两次跑出不同结果**(抖动/复放),
            #    必须当未解决处理,不许戴上 KBWD 的帽子混成「已解决」。
            outside = _kbwd_outside_quick(mid, new_red,
                                         _rmeta.get("red_by_target", {}))
            if outside:
                blunt = v == "KILLED_BLUNT"
                v = ("KILLED_BY_WIDER_DENOMINATOR_BLUNT" if blunt
                     else "KILLED_BY_WIDER_DENOMINATOR")
                print(f"   ✅ 更宽分母下被杀{' · 钝' if blunt else ''} · "
                      f"新红来自 QUICK 之外的包:{sorted(outside)[:3]}")
            else:
                v = "REPLAY_DISAGREES_WITH_QUICK"
                print("   ⛔ 新增红**全部**落在 QUICK 作用域内 —— 快集判它存活,"
                      "全分母又判它红:同一批判据两次结果不一致,记未解决,不记 KBWD")
        # 🔴 [B-1-3] 重锚期望必须**贯穿两段**。只在快集段核时:一发在快集存活、
        #    到全分母被某条**无关**的红"误杀",照样记 KILLED_BY_WIDER_DENOMINATOR。
        #    而重锚的全部正当性就是「只动锚一条轴、语义一字不改」——
        #    无关的红冒充重锚命中,等于把那条正当性悄悄取消了。
        if v in RESOLVED_VERDICTS:
            bad = (assert_run_meta_sane(mid, _rmeta["per_target"], FULL)
                   or assert_expected_killers_ran(mid, m, _rmeta["status_by_name"])
                   or check_reanchor_expectations(mid, m, new_red))
            if bad:
                v = bad
        r["verdict"] = v
        r["full_criteria_fp"] = full_fp
        _write_results(res_path, results)

    print("\n" + "═" * 74)
    sf = [r["id"] for r in results if r["verdict"] == "SURVIVED_FULL"]
    kw = [r["id"] for r in results if r["verdict"] in KBWD_VERDICTS]
    print(f"全分母复核:更宽分母杀掉 {len(kw)} 发 {kw}")
    print(f"全分母仍存活 {len(sf)} 发 {sf} —— 交 Review 定洞/冗余")
    return hard_gate_rc(results, "全分母复核", expected=set(muts),
                        targets_by_id={i: QUICK_SCOPE[i] for i in muts})


def main() -> int:
    # 🔴 机制令 ①:就地改源 ⇒ 抢树级锁。
    with tree_lock("mutation_runner_extsel_v2_2026_08_27"):
        if os.environ.get("V2_MODE") == "full_survivors":
            return run_full_survivors()
        return _main_locked()


def _assert_no_mutation_residue(roster) -> None:
    """🔴 [V8-B 补] 起跑前**内容级**残留自证 —— 与标记法并列,不是替代。

    起因:Deploy 建议「收尾无条件核树 == HEAD」,我去查主 runner 有没有这道闸,
    核出的是一个**真缺口**:

      · 现有 `_assert_no_concurrent_runner()` 扫的是 `*.mutbak` / `*.mutrestore`
        —— **标记法**;
      · 内容法的谓词 `assert_tree_is_unmutated` 就在 `mutation_replay_common`,
        V3-C 与 V4-C 两个 replayer 都调它,**主 runner 不调**。

    两者不等价,差的正是这一格:**还原被 ENOSPC 截断时,标记文件已删、树是脏的**
    (本仓记过那一形)。标记法看不见,内容法看得见。
    同族还有:进程被 kill ⇒ `finally` 根本不执行 ⇒ 变异留在树里
    —— 那一发我今天亲身撞过,而它用的是自己的备份不是 `.mutbak`,
    所以就算跑 runner,标记法也发现不了。

    ⇒ 「清理必须可被**独立复验**,而不是依赖 `finally`/`trap` 真的跑到」。
    """
    _RC.assert_tree_is_unmutated(
        ROOT, [{"id": m["id"], "file": m["file"],
                "edits": [(pr["from"], pr["to"]) for pr in m["pairs"]]}
               for m in roster])


def _main_locked() -> int:
    _E._assert_disk_headroom()                                     # noqa: SLF001
    _E._assert_no_concurrent_runner()                              # noqa: SLF001
    # 🔴 分母锁也要护**变异跑**,不能只护基线:27 发的裁定("杀"还是"存活")
    #    整个建立在分母上,分母悄悄少一个包 ⇒ 某发从杀翻成存活 ⇒ 被当判据洞上报。
    _B.selftest_denominator_lock()
    _B.assert_denominator_frozen()
    roster = load_v2()
    _assert_no_mutation_residue(roster)
    only = {x.strip() for x in os.environ.get("V2_ONLY", "").split(",") if x.strip()}
    fam = {x.strip() for x in os.environ.get("V2_FAMILY", "").split(",") if x.strip()}
    muts = [m for m in roster if (not only or m["id"] in only)
            and (not fam or m["family"] in fam)]
    # 🔴 [B-1-4] 请求集合 vs 命中集合,**在烧掉一分钟基线之前**对账。
    #    不加 `if only or fam` 的条件:全跑那一路也要过这道门,
    #    「命中 0 发」在任何模式下都不许当成一轮跑过。
    assert_selector_hits(only, fam, roster, muts)
    muts.sort(key=lambda m: (m["family"], m["id"]))

    OUT.mkdir(exist_ok=True)
    res_path = _results_path()
    results: list[dict] = []
    if res_path.exists():
        results = json.loads(res_path.read_text(encoding="utf-8"))
    tip = _current_tip()
    done = resume_done_ids(results, tip, res_path.name,
                          {m["id"]: criteria_fp_of(QUICK_SCOPE[m["id"]])
                           for m in muts},
                          targets_by_id={m["id"]: QUICK_SCOPE[m["id"]]
                                         for m in muts})

    print("═" * 74)
    print("基线(快集所涉包的并集)")
    print("═" * 74)
    quick_pkgs = sorted({t for m in muts for t in QUICK_SCOPE[m["id"]]})
    base_red, base_green, broken, _bmeta = _run_targets(quick_pkgs, "v2base")
    print(f"基线:绿 {sum(base_green.values())} · 红 {len(base_red)} · 包 {len(quick_pkgs)}")
    print(f"       逐包绿数(绿数守恒按包比,不按总数比):{base_green}")
    if broken:
        raise SystemExit(f"🔴 基线里有包跑不起来:{broken} —— 后面全部作废")
    # 🔴 [门①] 基线有红:停机、零落盘。原来这里只 print —— 见函数 docstring 的实测。
    assert_baseline_clean(base_red, "快集", res_path)

    for m in muts:
        mid = m["id"]
        if mid in done:
            print(f"⏭  {mid} 已有结果,跳过(删 {res_path.name} 可重跑)")
            continue
        path = ROOT / m["file"]
        if not path.exists():
            raise SystemExit(f"🔴 {mid} 文件不在:{m['file']}")
        print("\n" + "─" * 74)
        print(f"{mid} [{m['family']}] {m['file']}")
        print(f"   草单预测:{m['predict']['verdict']}"
              + (f"(恰 {m['predict']['exact_n']} 条)" if m["predict"]["exact_n"] else ""))
        if m.get("amended"):
            print(f"   ⚠️ amendment:{m['amended'][:110]}")

        backup = path.with_suffix(path.suffix + ".mutbak")
        original, proofs = None, []
        try:
            original, proofs = _apply_pairs(path, m["pairs"], mid)
            backup.write_bytes(original)
            ok, err = _E._syntax_ok(path)                           # noqa: SLF001
            if not ok:
                red, green, brk, _rmeta = {f"__SYNTAX__::{err[:60]}"}, -1, [], {"status_by_name": {}}
            else:
                red, green, brk, _rmeta = _run_targets(QUICK_SCOPE[mid], mid.replace("MUT-", ""))
        finally:
            if original is not None:
                _E._restore(path, original)                         # noqa: SLF001
            backup.unlink(missing_ok=True)

        new_red = sorted(red - base_red)
        rec = {"id": mid, "family": m["family"], "file": m["file"],
               "predict": m["predict"], "amended": m.get("amended"),
               "on_disk_proof": proofs, "quick_scope": list(QUICK_SCOPE[mid]),
               "quick_red": new_red, "quick_green": sum(green.values()),
               "quick_green_by_pkg": green, "broken": brk,
               # 🔴 重锚谱系必须落进产物:裁定是"只动锚一条轴",
               #    而"哪一发承自哪一发、依据哪条台账"只活在 runner 内存里的话,
               #    产物就说不清 27 发里为什么有三个带 b 的 id。
               "reanchored_from": m.get("reanchored_from"),
               "reanchor_ledger": m.get("reanchor_ledger"),
               "expect_red_superset": m.get("expect_red_superset"),}

        hit = DB_DOUBLE_HIT.get(mid)
        if hit:
            # [终单令②] 文件还原了**不等于**库还原了:054 的守卫按 conname 判存,
            # 库里留一条同名弱定义,重放会直接跳过 —— 文件干净、库里是脏的。
            dropped = _drop_dbs(hit["dbs"])
            note = {"dropped": dropped}
            print(f"   🔁 真库双打 ①DROP:{dropped}")
            if hit.get("verify_db"):
                # ②重装:让判据包按**还原后**的迁移把模板/冷库重新冷建一遍。
                _r, _g, _b, _ = _run_targets(("tests/defgeo_e3_2026_08_26",),
                                          mid.replace("MUT-", "") + "_dbrebuild")
                note["rebuild_green"] = _g
                note["rebuild_broken"] = _b
                print(f"   🔁 真库双打 ②重装:e3 包重跑 绿 {_g}"
                      + (f" · 出问题 {_b}" if _b else ""))
                # ③核定义:核的是**定义**不是数量 —— 弱化版下数量同样是 1。
                defs = _constraint_def(hit["verify_db"], hit["verify"])
                note["constraint_defs"] = defs
                joined = " | ".join(defs)
                bad = [x for x in hit.get("must_not_contain", ()) if x in joined]
                miss = [x for x in hit.get("must_contain", ()) if x not in joined]
                print(f"   🔁 真库双打 ③核定义:{defs or '(空)'}")
                if bad or miss:
                    raise SystemExit(
                        f"🔴 {mid} 真库里的定义不对:含禁词 {bad} · 缺 {miss} —— "
                        f"弱定义留在库里了(文件已还原)。defs={defs}。停下报 Review。")
                note["verified"] = True
                print("   ✅ 真库双打:定义逐字回到 `> 0`(不是只看数量)")
            rec["db_double_hit"] = note

        rec["verdict"] = _verdict(rec, new_red, green, base_green,
                                  QUICK_SCOPE[mid], brk, quick=True)
        # [B-2③] 重锚登记的预期,机械执行(不再靠人肉核)。
        if rec["verdict"] in RESOLVED_VERDICTS:
            # 🔴 顺序有意义:先问「杀手跑没跑」,再问「红没红」。
            bad = (assert_run_meta_sane(mid, _rmeta["per_target"],
                                        QUICK_SCOPE[mid])
                   or assert_expected_killers_ran(mid, m, _rmeta["status_by_name"])
                   or check_reanchor_expectations(mid, m, new_red))
            if bad:
                rec["verdict"] = bad
        rec["run_meta"] = _rmeta["per_target"]
        # 🔴 [V9-B · P1-2] 红集**按包**落盘 —— 「这条红是哪个包出的」是 KBWD 正当性的全部依据。
        rec["red_nodes_by_target"] = _rmeta.get("red_by_target", {})
        rec["mutation_fp"] = mutation_fp_of(m)
        rec["mutation_fp_version"] = FP_VERSION
        rec["tip"] = tip
        rec["criteria_fp"] = criteria_fp_of(QUICK_SCOPE[mid])
        results.append(rec)
        _write_results(res_path, results)

    print("\n" + "═" * 74)
    print("快集分流小结(**不是**终局:存活要过全分母)")
    for famname in ("E2", "E3"):
        g = [r for r in results if r["family"] == famname]
        k = sum(1 for r in g if r["verdict"] == "KILLED")
        print(f"   {famname}:{len(g)} 发 · 杀 {k} · 快集存活 {len(g) - k}")
    surv = [r["id"] for r in results if r["verdict"] == "SURVIVED_QUICK"]
    print(f"   快集存活待过全分母:{surv}")
    for v in ("PACKAGE_BROKEN_NEEDS_REVIEW", "INVALID_SYNTAX",
              "GREEN_NOT_CONSERVED", "KILLED_BLUNT"):
        ids = [r["id"] for r in results if r["verdict"] == v]
        if ids:
            print(f"   ⛔ {v}:{ids}")
    return hard_gate_rc(results, "快集分流", expected={m["id"] for m in muts},
                        targets_by_id={m["id"]: QUICK_SCOPE[m["id"]]
                                       for m in muts})


if __name__ == "__main__":
    raise SystemExit(main())
