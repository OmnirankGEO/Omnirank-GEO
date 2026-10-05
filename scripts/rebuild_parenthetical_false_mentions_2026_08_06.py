"""受污染诊断重判 · 括号地名假提及 P0(536 / 541 / 563)· 2026-08-06。

返工单 `WO_DIAGNOSIS_PARENTHETICAL_REWORK_2026-08-06.md` §3(已批准,随发车执行)。

## 做什么

用**现存的原始回答**(``full_response``)按修复后的身份重判每一格,
再按评分 SSOT 重算 dimension_stats → funnel → 总分/等级,回写报告与 `brands.latest_score`。

  · **零模型调用** —— 只走 ``BrandIdentityResolver.resolve_local``(纯本地确定性层);
  · **零扣费** —— 不碰任何计费表;
  · **不覆盖原始回答** —— ``full_response`` 一个字节都不改。

## 🔴 为什么必须重写单元格,而不是只重算聚合

``classify_cell_state`` 见到落库的 ``brand_verdict == "YES"`` 就**直接返回 YES**
(那是设计:人工确认过的格不能被后续重判推翻)。所以只调聚合函数,
536/541/563 里那些靠 ``matched_text="深圳"`` 判 YES 的格**一格都不会变** ——
分数照旧虚高。必须逐格 `resolve_local` 重判并改写 verdict。

## 🔴 绝不动的两类格

1. **人工已决**(``identity_review_state`` ∈ {confirmed, rejected}):机器不许推翻人;
2. **没有原始回答**的格(``full_response`` 空):无据可判,原样保留。

## 静默执行(Owner 铁律,资金无涉)

不通知用户、不设对外暂停、界面不留"已修正"痕迹。本脚本只写数据,不发任何通知。

## 跑法

    # 先看效果,不写库(默认)
    python scripts/rebuild_parenthetical_false_mentions_2026_08_06.py
    # 确认后写库
    python scripts/rebuild_parenthetical_false_mentions_2026_08_06.py --apply
    # 指定别的诊断
    python scripts/rebuild_parenthetical_false_mentions_2026_08_06.py --ids 536,541,563 --apply
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys

DEFAULT_IDS = (536, 541, 563)

HUMAN_DECIDED = {"confirmed", "rejected"}


# ══════════════════════════════════════════════════════════════════════════
# [返工单 2026-08-08 §2 / §4.2] `--apply` 自己把自己锁死 —— 结构性根治
# ══════════════════════════════════════════════════════════════════════════
#
# ## 病是什么(生产 `pg_blocking_pids` 实测 + 本地真库复现)
#
#   488727 idle in transaction  ← 主事务,已 `UPDATE diagnosis_records`(持 ROW EXCLUSIVE)
#   488726 active / Lock        ← `CREATE INDEX IF NOT EXISTS idx_diag_records_run_token
#                                  ON diagnosis_records`,blocked_by {488727}
#
# 两个 pid 都是它自己:主事务在等报告装配返回,装配深处**开了第二条连接**,
# 建连时触发 `db/diagnosis_db.py:8467` 的**模块级** `init_db()` → 自愈 DDL 要
# SHARE 锁 → 与主事务的 ROW EXCLUSIVE 冲突 → 死等。`dry-run` 不写库、不持锁,
# 所以永远碰不到 —— 这就是 dry-run 一路绿却 `--apply` 22 分钟零进展的原因。
#
# ## 为什么不按"把 conn 灌进整条装配链"来修
#
# 我按 §2.3 要求**实测扫**了(探针包住连接池 `getconn`,跑未修版 `--apply`):
# 第一条写语句之后仍有 **10 次**取连接,来自 **7 处**不同调用点 ——
#   `profile_db.get_effective_brief_by_brand` / `report_evidence.extract_report_evidence`
#   / `brand_directed_question.load_confirmed_aliases` / `source_authority_analyzer`
#   `._fetch_monitoring_citations` / `report_writer_v2.build_module_4_competitors`
#   / `industry_median.get_industry_median` / `public_whitelabel`,
# 最后一条就是 `db/diagnosis_db.py:8467` 的模块级 `init_db()`。
# 给这 7 个**共享生产模块**(全在实时诊断主链上)加 `conn=` 参数,blast radius
# 远超本单;而最后那一条是 **import 副作用**,签名怎么改都灌不进去。
#
# ## 所以改的是前提本身:**让"持锁期间"这段时间不存在**
#
# 不变式(三条一起才成立,缺一条就会复发):
#   ① `_preload_report_chain()` —— 装配链上所有模块在**开事务之前**导入完,
#      模块级 `init_db()` 的自愈 DDL 在任何事务之外跑掉;
#   ② `rebuild_one` 里**只读 + 装配全部前移到第一条写语句之前**,写只剩一条短尾巴;
#   ③ `main()` **逐份提交** —— 第 N 份的锁在第 N+1 份开始装配前就已释放。
# 再加一道**会自己报错**的守卫 `_forbid_new_connections`:写窗口内谁再开连接,
# 立刻抛 `ConnectionInsideLockWindow`。把 22 分钟的静默死等换成一行指名道姓的报错。
#
# 🔴 §4.2 明令不许用的三种糊法(超时 / 重试 / NOWAIT)一个都没用:
#    它们只是把死锁变成随机失败,这里是让死锁**不可能发生**。


class ConnectionInsideLockWindow(RuntimeError):
    """写窗口内又去开新连接 —— 这正是 `--apply` 自锁死的形态,当场炸,不许死等。"""


@contextlib.contextmanager
def _forbid_new_connections(what: str):
    """写窗口守卫:期间任何取连接都立刻失败。

    🔴 打在**连接池对象**上,不打在 `db.connection.get_connection` 这个名字上 ——
    装配链里大量模块是 `from db.connection import get_connection`(import 期绑定),
    patch 模块属性对它们**一个都拦不到**(判别力=0)。所有路径最终都要过
    `_get_pool().getconn()`,拦这里才是真拦。
    """
    from db import connection as _connection

    pool = _connection._get_pool()
    original = pool.getconn

    def _deny(*args, **kwargs):
        raise ConnectionInsideLockWindow(
            f"{what}:持锁期间试图开新连接 —— 会与主事务抢 diagnosis_records 的锁"
        )

    pool.getconn = _deny
    try:
        yield
    finally:
        pool.getconn = original


def _preload_report_chain() -> None:
    """把报告装配链的模块在**开事务之前**导入完。

    🔴 `db/diagnosis_db.py:8467` 是**模块级** `init_db()`:第一次 import 它 =
    开一条新连接跑整条建表/自愈 DDL 链。它在装配链深处被懒加载,于是这条 DDL
    正好落在主事务持锁期间。这里主动把它逼到事务之前跑掉。

    这不是新增 DDL:同一段 `init_db()` 每次应用启动都会跑一遍,本函数只是
    **改变它发生的时刻**。导入失败不致命(装配阶段会各自再 import 并按既有
    路径报 `report_v2_error`),所以逐个 try —— 但绝不吞掉"没预加载"这件事本身。
    """
    modules = (
        "db.diagnosis_db",              # 🔴 模块级 init_db() —— 死锁真凶,必须最先
        "db.profile_db",
        "db.distillation_db",
        "services.diagnosis_report_v2",
        "services.report_writer_v2",
        "services.report_evidence",
        "services.report_metrics",
        "services.public_whitelabel",
        "services.source_authority_analyzer",
        "tools.industry_median",
        "services.brand_identity_resolver",
        "services.brand_directed_question",
        "services.diagnosis_identity_decision",
        "tools.scoring.funnel_score",
    )
    import importlib

    failed: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{name}({type(exc).__name__})")
    if failed:
        print(f"[preload] 未能预加载:{', '.join(failed)}", file=sys.stderr)


def _load(conn, diagnosis_id: int) -> dict | None:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, brand_id, brand_name, raw_data_json, total_score, level
          FROM diagnosis_records
         WHERE id = %s
         FOR UPDATE
        """,
        (int(diagnosis_id),),
    )
    row = cur.fetchone()
    return dict(row) if row else None


class UnexpectedChange(RuntimeError):
    """清单外的格子会被改动 —— 立即中止,不写库。"""


#: 由 `resolve_local`(纯本地确定性层)**自己**产出的 detection_method。
#:
#: 🔴🔴 [返工 2026-08-07] 这张表是本次返工的核心。第一版把两种完全不同的东西
#: 混成一类 surprise:
#:   (a) **真的意外** —— 这一格本来能被本地层忠实重放,重放后却变了;
#:   (b) **本来就不可重放** —— 落库 verdict 是**完整判定链**(含 LLM 复核层)
#:       产生的,本地层根本无从复现。
#: 生产实测:落库 NO(复核层判的)那类格,`resolve_local` 一律给 UNKNOWN ——
#: 于是「清单外的格重判后应稳定」这个前提**对任何诊断都不成立**。
#: 后果:536/541/563 三份全部整份中止,全量 330 份里 316 份中止,
#: 清单外「会变」的格 5183 个(其中 99% 的 old_matched_text 为空)。
#: **`--apply` 一格也写不进去 —— 功能是惰性的。**
#:
#: 判据必须**显式、可枚举、写死**(不许用"看着像本地"这种推断):
#: 下面每一个都是 `BrandIdentityResolver.resolve_local` 里 return 的字面量。
#: `deepseek_v4_flash_structured`(复核层)与 `human_review`(人工)**不在表里**。
_LOCAL_DECISION_METHODS = frozenset({
    "deterministic",              # load_error / 空答案 / 身份冲突 / 无候选
    "deterministic_local",        # 本地有证据窗口,待复核
    "trusted_exact",
    "trusted_legal_suffix_exact",
    "human_negative_exact",
    "derived_storefront_exact",
    "derived_legal_exact",
})


def _is_locally_replayable(cell: dict, *, in_scope: bool) -> bool:
    """这一格的落库判定,本地层复现得了吗。

    🔴 **清单内的格永远算可重放**(返工单 §4① 明令):否则 §1.3 那 50 格
    会被新判据自己吃掉,整件事白做。这一条是 `or in_scope` **显式写死**的,
    不依赖"污染格恰好都是 trusted_exact"这种巧合 —— 巧合会随数据变。

    其余按 `detection_method` 白名单判。没有 `detection_method` 的历史格
    → 不可重放(fail-closed:不评判、不改写、也不中止)。
    """
    if in_scope:
        return True
    method = str(cell.get("detection_method") or "").strip()
    return method in _LOCAL_DECISION_METHODS


def _pollution_signature(cell: dict, identity) -> str | None:
    """这一格是不是**本次污染**命中的那一类;是则返回旧的命中文本。

    🔴🔴 [复审 P0 · 2026-08-06] 第一版我遍历**所有**非人工裁决格重判,
    允许 `NO→YES` 也允许 `YES→PENDING`,没有污染清单、没有旧值哈希、
    没有预期变更集 —— 那等于拿一个"修括号地名"的包去把三份报告的分数
    整体重算一遍。resolver 里任何一处别的改动都会顺带改分,而且没人会发现。
    **数据完整性 H0,复审判得对。**

    污染的精确指纹(两条同时成立):
      ① 该格当前判 **YES**(假提及只会是 YES —— 假阴性不在本包范围);
      ② 它当时命中的 ``matched_text`` 在**修复后**已经不是可信形态了
         —— 也就是说,这次命中**完全依赖**那个被拆出来的括号地名。
    只有同时满足两条的格才进清单。别的格一律不碰。
    """
    if str(cell.get("brand_verdict") or "").upper() != "YES":
        return None
    matched = str(cell.get("matched_text") or "").strip()
    if not matched:
        return None
    from services.brand_identity_resolver import normalize_brand_name

    trusted_now = {
        normalize_brand_name(name)
        for name in (getattr(identity, "all_trusted_names", ()) or ())
        if normalize_brand_name(name)
    }
    return matched if normalize_brand_name(matched) not in trusted_now else None


def _rejudge_cells(ai: dict, identity) -> dict:
    """只重判**污染清单内**的格;清单外任何变化立即中止。零 provider 调用。"""
    from services.brand_identity_resolver import BrandIdentityResolver, BrandVerdict

    resolver = BrandIdentityResolver(identity)
    stats = {"total": 0, "in_scope": 0, "rewritten": 0,
             "human_skipped": 0, "no_answer_skipped": 0, "out_of_scope_stable": 0,
             # [返工 2026-08-07 §4①] 第四个数:本地层无从复现的格。
             # 它既不是"稳定",也不是"意外" —— 是**本函数无权评判**。
             # 不单独打出来的话,"316 份中止"这种事只会表现为一句 aborted=3。
             "unreplayable": 0}
    changes: list[dict] = []
    surprises: list[dict] = []

    for item in ai.get("detail_table") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "")
        for engine, cell in (item.get("results") or {}).items():
            if not isinstance(cell, dict):
                continue
            stats["total"] += 1
            if str(cell.get("identity_review_state") or "") in HUMAN_DECIDED:
                stats["human_skipped"] += 1
                continue
            answer = str(cell.get("full_response") or "")
            if not answer.strip():
                stats["no_answer_skipped"] += 1
                continue

            before = str(cell.get("brand_verdict") or ("YES" if cell.get("brand_detected") else "NO")).upper()
            polluted_text = _pollution_signature(cell, identity)

            # 🔴 [返工 2026-08-07 §4①] **先判可重放性,再判 surprise**。顺序不可换:
            # 反过来的话,不可重放的格会被当成"意外变化"整份中止 —— 那正是
            # `--apply` 一格也写不进去的原因。
            if not _is_locally_replayable(cell, in_scope=polluted_text is not None):
                stats["unreplayable"] += 1
                continue

            decision = resolver.resolve_local(answer)
            after = decision.verdict.value

            if polluted_text is None:
                # 清单外:**只观察不改写**。若它也会变,说明本包的影响面超出
                # "括号地名污染"—— 立刻中止,交人看,绝不静默顺带改分。
                if after != before:
                    surprises.append({
                        "question": question[:40], "engine": engine,
                        "before": before, "after": after,
                        "old_matched_text": cell.get("matched_text"),
                    })
                else:
                    stats["out_of_scope_stable"] += 1
                continue

            stats["in_scope"] += 1
            if after == "YES":
                # 修复后仍然判命中 = 它本来就有别的可信证据,不是污染。不动。
                continue
            # 🔴 清单内只允许 YES → 非 YES。方向反了立即中止。
            if before != "YES":
                surprises.append({
                    "question": question[:40], "engine": engine,
                    "before": before, "after": after, "reason": "非 YES 起点进了清单",
                })
                continue

            changes.append({
                "question": question[:40], "engine": engine,
                "before": before, "after": after,
                "old_matched_text": polluted_text,
                # 旧值血缘:原始回答指纹 + 旧判定,便于事后核对/回溯
                "response_sha256": hashlib.sha256(answer.encode("utf-8")).hexdigest()[:16],
                "old_detection_reason": cell.get("detection_reason"),
            })
            stats["rewritten"] += 1

            cell["brand_verdict"] = after
            cell["brand_detected"] = decision.verdict is BrandVerdict.YES
            cell["detection_reason"] = decision.reason
            cell["detection_method"] = decision.method
            # matched_text 只在真命中时才有意义;判否要清掉旧的
            # (536/541/563 里它就是 "深圳" —— 留着等于把真凶继续挂在报告上)
            cell["matched_text"] = decision.matched_alias or None
            cell["identity_candidates"] = (
                [decision.matched_alias] if decision.matched_alias else []
            )
            # verdict 与人工复核态必须一致:机器重判的格子不许残留 confirmed/rejected
            # (人工已决格在上面就 skip 了,这里只是把机器格的态显式写干净)
            cell["identity_review_state"] = "not_required"
            if decision.evidence_snippet:
                cell["identity_evidence_snippet"] = decision.evidence_snippet

    if surprises:
        raise UnexpectedChange(json.dumps(surprises, ensure_ascii=False))
    stats["changes"] = changes
    return stats


def rebuild_one(conn, diagnosis_id: int, *, apply: bool) -> dict:
    from services.diagnosis_identity_decision import (
        _aggregates,
        _funnel_from_dimension_stats,
        _load_identity_for_review,
        _parse_raw_data,
        _ai_visibility,
        _rate_or_none,
        apply_recomputed_totals,
    )

    record = _load(conn, diagnosis_id)
    if not record:
        return {"diagnosis_id": diagnosis_id, "status": "not_found"}
    brand_id = int(record.get("brand_id") or 0)
    if not brand_id:
        return {"diagnosis_id": diagnosis_id, "status": "no_brand_id"}

    raw = _parse_raw_data(record.get("raw_data_json"))
    data = raw.get("data") if isinstance(raw.get("data"), dict) else {}
    ai = _ai_visibility(raw)
    if not ai.get("detail_table"):
        return {"diagnosis_id": diagnosis_id, "status": "no_detail_table"}

    identity = _load_identity_for_review(
        brand_id, fallback_name=record.get("brand_name") or ""
    )
    if getattr(identity, "load_error", False):
        # fail-closed:身份读不到就绝不重判(否则会把全部格判成未提到)
        return {"diagnosis_id": diagnosis_id, "status": "identity_load_failed"}

    stats = _rejudge_cells(ai, identity)
    aggregates = _aggregates(ai, identity)
    # [WO_MENTION_COUNT 2026-08-08 §4.1] 与人工确认链走同一个派生点。
    # 此前本脚本把 YES 改少了却不刷汇总,541/563 因此写着「被提及 17 次」
    # 而等级已是隐形级 20 分 —— 同一份报告自相矛盾。
    totals_delta = apply_recomputed_totals(ai, aggregates)
    funnel = _funnel_from_dimension_stats(aggregates["dimension_stats"])
    data["ai_visibility"] = ai
    raw["data"] = data

    result = {
        "diagnosis_id": diagnosis_id,
        "brand_id": brand_id,
        "brand_name": record.get("brand_name"),
        "status": "rebuilt" if apply else "dry_run",
        "score_before": record.get("total_score"),
        "score_after": funnel.get("total_score"),
        "level_before": record.get("level"),
        "level_after": funnel.get("level"),
        # dry-run 也要把这两个数印出来:回填前逐份过目靠的就是它(§4.2)
        "detected_before": totals_delta["detected_count"]["before"],
        "detected_after": totals_delta["detected_count"]["after"],
        "rate_before": totals_delta["overall_mention_rate"]["before"],
        "rate_after": totals_delta["overall_mention_rate"]["after"],
        **stats,
    }
    if not apply:
        return result

    cur = conn.cursor()

    # ── 阶段 1:只读 + 报告装配(**此时一条写语句都还没发**)────────────────
    # 🔴 [返工单 2026-08-08 §4.2] 顺序是修复本身,不是风格。原版先 `UPDATE
    #   diagnosis_records` 再装配 —— 装配深处开的第二条连接会触发模块级
    #   `init_db()` 的 `CREATE INDEX ... ON diagnosis_records`,去等主事务
    #   自己刚拿的锁 → 自锁死。装配挪到写之前,那 10 次取连接就全落在
    #   "没有任何写锁"的时间窗里,与谁都不冲突。
    #   (`_load` 的 `SELECT ... FOR UPDATE` 拿的是 ROW SHARE 表锁,按 PG 锁
    #    冲突矩阵与 `CREATE INDEX` 的 SHARE **不冲突**,所以它留在原位无害。)
    report_error = None
    v2 = None
    try:
        cur.execute("SELECT * FROM brands WHERE id = %s", (brand_id,))
        brand_row = cur.fetchone()
        cur.execute("SELECT * FROM client_profiles WHERE brand_id = %s LIMIT 1", (brand_id,))
        profile_row = cur.fetchone()
        cur.execute(
            "SELECT * FROM quotes WHERE brand_id = %s ORDER BY created_at DESC LIMIT 1",
            (brand_id,),
        )
        quote_row = cur.fetchone()

        from services.diagnosis_report_v2 import assemble_diagnosis_report_v2

        v2 = assemble_diagnosis_report_v2(
            diagnosis_results=raw,
            brand=dict(brand_row) if brand_row else {},
            profile=dict(profile_row) if profile_row else {},
            quote=dict(quote_row) if quote_row else {},
            brand_id=brand_id,
            score_data=raw.get("score_data") or raw.get("scores") or {},
            # 🔴 [#54/#55] 本脚本没有可透传的游标(装配深处自开连接会重放 08-10 自锁死),
            #    所以**显式**传不可用,并写明 reason —— 不是省略、也不是默认值。
            #    后果:回填/重生成产物在这一格上退出分母,与主链路口径不同,已在交付里点名。
            published={"count": None, "available": False, "reason": "rebuild_no_cursor"},
        )
        report_error = v2.get("error")
        v2["funnel_score"] = funnel  # 以本次重算为准,防装配层读陈旧 dimension_stats
    except Exception as exc:  # noqa: BLE001
        report_error = f"v2 报告重生异常: {exc}"
        v2 = None

    # ── 阶段 2:写窗口(到 commit 为止,**禁止**再开新连接)──────────────────
    # fail-closed 降级不变:报告重生失败也必须把评分 SSOT 列落下去,绝不让
    # 代理端/公开端读到与重判结果不一致的分数(与 decide_brand_cell 第 11 步同口径)。
    report_rebuilt = False
    with _forbid_new_connections("rebuild_one 写窗口"):
        cur.execute(
            "UPDATE diagnosis_records SET raw_data_json = %s WHERE id = %s",
            (json.dumps(raw, ensure_ascii=False, default=str), diagnosis_id),
        )

        if v2 is not None and not report_error:
            from services.diagnosis_report_v2 import update_diagnosis_v2_in_db

            cur.execute("SAVEPOINT sp_v2")
            try:
                if update_diagnosis_v2_in_db(diagnosis_id, v2, conn=conn):
                    report_rebuilt = True
                else:
                    report_error = "v2 报告回写失败"
            except Exception as exc:  # noqa: BLE001
                report_error = f"v2 报告回写异常: {exc}"
            if not report_rebuilt:
                cur.execute("ROLLBACK TO SAVEPOINT sp_v2")
            cur.execute("RELEASE SAVEPOINT sp_v2")

        # [WO_MENTION_COUNT 2026-08-08 §4.1] 列镜像与 raw JSON 同事务刷新。
        # 与 decide_brand_cell 同口径:放在成功/降级分支之外,两条路径都覆盖。
        cur.execute(
            "UPDATE diagnosis_records SET ai_detected_count=%s,"
            " ai_mention_rate=COALESCE(%s, ai_mention_rate) WHERE id=%s",
            (
                int(totals_delta["detected_count"]["after"]),
                _rate_or_none(totals_delta),  # 无分母 → None → 保留原列值,绝不写 0
                diagnosis_id,
            ),
        )

        if not report_rebuilt:
            cur.execute(
                "UPDATE diagnosis_records SET total_score=%s, level=%s, report_v2_error=%s "
                "WHERE id=%s",
                (int(funnel.get("total_score") or 0), funnel.get("level"),
                 report_error, diagnosis_id),
            )

        # 🔴 latest_score 只在该诊断确实是该品牌最新那条时才同步 —— 返工单点名
        # `latest_diagnosis_id` 错绑另立单,这里不趁机"顺手修",只做不会加重错绑的写法。
        cur.execute(
            """
            UPDATE brands
               SET latest_score = %s, updated_at = CURRENT_TIMESTAMP
             WHERE id = %s AND latest_diagnosis_id = %s
            """,
            (int(funnel.get("total_score") or 0), brand_id, diagnosis_id),
        )
        result["latest_score_synced"] = cur.rowcount == 1

    result["report_rebuilt"] = report_rebuilt
    result["report_error"] = report_error
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ids", default=",".join(str(i) for i in DEFAULT_IDS))
    parser.add_argument("--apply", action="store_true", help="真写库(默认只 dry-run)")
    args = parser.parse_args()
    ids = [int(x) for x in str(args.ids).split(",") if str(x).strip()]

    # 🔴 [返工单 2026-08-08 §4.2 · 不变式①] 装配链**在开事务之前**导入完 ——
    #    把 `db/diagnosis_db.py` 模块级 `init_db()` 的自愈 DDL 逼到任何事务之外跑。
    _preload_report_chain()

    from db.connection import get_connection

    conn = get_connection()
    results: list[dict] = []
    applied = planned = skipped = aborted = failed = 0
    try:
        for diagnosis_id in ids:
            # 🔴 [复审 P2 · 2026-08-06] 逐份独立事务边界。第一版三份共用一个事务:
            #   任一份异常整批回滚,而 --apply 最后照样打印「已写库」——
            #   那是**报告一件没发生的事**。每份独立回滚,并分别计数。
            # 🔴 [返工单 2026-08-08 · 不变式③] 由 SAVEPOINT 改成**逐份提交/回滚**:
            #   原版把三份包在一个事务里,第 1 份的写锁一直持到最后 ——
            #   第 2、3 份装配时开的那 10 次连接仍落在持锁窗口内。逐份提交后,
            #   第 N 份的锁在第 N+1 份开始装配之前就已经释放,窗口真正消失。
            #   (逐份独立的语义本来就是上一版 SAVEPOINT 想要的,这里只是把它做实。)
            try:
                result = rebuild_one(conn, diagnosis_id, apply=args.apply)
            except UnexpectedChange as exc:
                conn.rollback()
                result = {
                    "diagnosis_id": diagnosis_id,
                    "status": "aborted_unexpected_change",
                    "surprises": json.loads(str(exc)),
                }
                aborted += 1
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                result = {
                    "diagnosis_id": diagnosis_id,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                failed += 1
            else:
                if result.get("status") == "rebuilt":
                    conn.commit()
                    applied += 1
                elif result.get("status") == "dry_run":
                    conn.rollback()   # dry-run 一个字都不写,读事务照样收干净
                    planned += 1
                else:
                    conn.rollback()
                    skipped += 1
            results.append(result)
        # 兜底:上面每条分支都已 commit/rollback,这里只收残留的只读事务。
        conn.rollback()
    finally:
        conn.close()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    # 🔴 [返工 2026-08-07 §4①] 四个数**分别**打出来。
    #    只打 aborted=3 的话,"316 份中止、5183 个格" 这种事看不出来 ——
    #    第一版就是这么把一个惰性功能报成"已交付"的。
    totals = {k: 0 for k in ("in_scope", "rewritten", "out_of_scope_stable",
                             "unreplayable", "human_skipped", "no_answer_skipped")}
    for item in results:
        for key in totals:
            totals[key] += int(item.get(key) or 0)
    print(
        "\n[CELLS] in_scope={in_scope} rewritten={rewritten} "
        "out_of_scope_stable={out_of_scope_stable} unreplayable={unreplayable} "
        "human_skipped={human_skipped} no_answer_skipped={no_answer_skipped}".format(**totals)
    )
    # 🔴 诚实统计:说的必须是**真发生的**。dry-run 不许说"已写库";
    #    有中止/失败时也不许把它藏在一句总括里。
    if args.apply:
        print(f"\n[APPLIED] committed={applied} skipped={skipped} "
              f"aborted={aborted} failed={failed}")
    else:
        print(f"\n[DRY-RUN] no writes · planned={planned} skipped={skipped} "
              f"aborted={aborted} failed={failed}")
    print("zero-model / zero-billing / full_response unchanged")
    return 1 if (aborted or failed) else 0


if __name__ == "__main__":
    sys.exit(main())
