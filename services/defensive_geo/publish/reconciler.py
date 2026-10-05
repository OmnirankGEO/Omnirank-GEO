"""settlement reconciler —— §12.3 六项覆盖 + kill window ④ 的收敛器。

判据:FIN-08 / FIN-13 / FIN-10 / MED-20 / §19 变异 23。

═══════════════════════════════════════════════════════════════════════
🔴 §12.3 要求覆盖的六项,逐项对应本文件一个函数
═══════════════════════════════════════════════════════════════════════
  1. 已收款但 activation 未物化        → ``activation_materializer.materialize_pending``
                                          (包E 起真的在 cron 里;此前那条 coveredBy
                                           指向的函数零调用点 = 声称覆盖、没人跑)
  2. 已冻结但 worker 未开始            → :func:`_frozen_never_dispatched`
  3. provider 已接收但本地 ack 缺失    → :func:`_external_started_without_outcome`
  4. canonical outcome 已落但 settlement 未完成 → :func:`_outcome_without_settlement`(窗口 ④)
  5. 发布镜像与 canonical item 矛盾    → :func:`_quarantine_mirror_conflicts`(转隔离进 Z-1)
  6. artifact/文章/媒体 revision 失配  → :func:`_report_revision_mismatch`(只报不修)

🔴 六项**全部**由 :func:`reconcile_once` 真的跑到 —— 包E 之前只跑 ②③④,
   ⑤⑥ 两个函数存在但零调用点。「有这个函数」不等于「这一项被覆盖了」。

🔴 「未知态**有人工核验与 owner/admin 处置入口**,不得用自动 release 掩盖接线故障」
   —— 本模块**没有任何一条路径会 release 一个未知态**。
   未知只做一件事:转 ``quarantined`` 并进 Z-1 队列(``settlement_review``)。
   判据 ``test_reconciler_never_releases_any_hold_or_quarantine_state`` 打的就是这一条。
"""

from __future__ import annotations

import logging
from typing import Any, Mapping, NamedTuple

from services.defensive_geo.publish import publish_funding as _funding
from services.defensive_geo.publish import publish_settlement as _settle
from services.defensive_geo.publish import store as _store

logger = logging.getLogger("GEO-DefGeoPublishReconciler")

RECONCILER_VERSION = "defgeo-publish-reconciler-v1"

#: 冻结多久没派发就算「已冻结但 worker 未开始」。
STUCK_FROZEN_SECONDS = 900
#: 外调后多久没有 canonical outcome 就转未知核验。
STUCK_EXTERNAL_SECONDS = 1800
#: Z-1 逐字:pending 超 **7 天** 自动告警。
PENDING_ALERT_SECONDS = 7 * 24 * 3600


class ReconcileAction(NamedTuple):
    command_id: str
    kind: str
    before: str
    after: str
    note: str


async def reconcile_once(cur, *, limit: int = 50) -> list[ReconcileAction]:
    """跑一轮收敛。**调用方持有事务**;每条 action 都是 CAS,重放安全。

    🔴 [包E 2026-08-24] 六项**全部**在这里跑,不是三项。
       第一版只跑 ②③④,而 ⑤⑥ 两个函数写好了却零调用点 ——
       ``coverage()`` 里那两行写着 ``inThisModule: True``,读起来像"覆盖了",
       实际是"有这个函数"。本仓记过同一形态:声称被覆盖、没有人跑。
       ①(已收款但 activation 未物化)由
       ``services/defensive_geo/activation_materializer`` 承担,它现在**真的**
       在 cron 里(包E 之前那个函数同样零调用点)。
    """
    actions: list[ReconcileAction] = []
    actions += _frozen_never_dispatched(cur, limit=limit)
    actions += _external_started_without_outcome(cur, limit=limit)
    actions += await _outcome_without_settlement(cur, limit=limit)
    actions += _quarantine_mirror_conflicts(cur, limit=limit)
    actions += _report_revision_mismatch(cur, limit=limit)
    actions += await _release_never_dispatched(cur, limit=limit)
    return actions


async def _release_never_dispatched(cur, *, limit: int) -> list[ReconcileAction]:
    """派发通道跑不通、而且**一次都没外调过** ⇒ release 一次 + needs_action。

    ═══════════════════════════════════════════════════════════════════
    🔴 这不是"自动退款掩盖接线故障",恰恰相反
    ═══════════════════════════════════════════════════════════════════
    §12.1 明令禁止的是「已 external-start / 结果未知」时自动退款。
    这里的条件里有 ``external_start_at IS NULL`` —— 那是 §12.3 的 canonical
    external-start marker,**它没有就是权威的零接单、零副作用证据**。
    §12.1 表格第 4 行逐字:「``rejected/failed`` 且有权威证据确认零接单、
    零外部副作用 → release 一次」。这一格正好落在那一行上。

    触发条件写成「outbox 已经 ``needs_review``」而不是一个时间阈值,
    是因为时间阈值回答不了"到底试过没有";队列耗尽重试才是真的试过了。
    (判据里那条「判据里不许有『今天』」的教训:阈值型条件容易变成
     与被测行为无关的红/绿。)

    她看到的是:这一篇没发出去、钱退回来了、可以再试一次 ——
    而不是钱冻着、没人说话、12 小时后莫名其妙退回。

    ═══════════════════════════════════════════════════════════════════
    🔴 [R1 返修] 候选集必须**包含平台腿**,否则下面那个分支是死的
    ═══════════════════════════════════════════════════════════════════
    Review 要我给平台成本腿补行为判据。补的时候才发现:这个查询原来写的是
    ``funding_state = 'frozen'``,而迁移 044 的 ``chk_defgeo_pcmd_platform_state``
    逐字规定

        principal_kind <> 'platform_cost_center' OR funding_state = 'exempt_recorded'

    —— 也就是说**数据库自己保证**平台腿永远不是 ``frozen``。于是
    ``if is_platform_cost(...)`` 这个分支一行都执行不到:守卫说"我管平台腿",
    查询却把平台腿全滤掉了。**谓词与候选集打架,守卫就是装饰**。

    所以候选集加上 ``exempt_recorded`` 这一格,并在非平台腿那一侧补一道
    「不是 frozen 就不动」的闸 —— 加宽候选集不许顺手加宽退款面
    (判据 ``test_24b`` 就是这一闸的反向对照)。

    🟡 **诚实标注**:现役签发链里 ``activation_materializer`` 调
       ``execution_budget_policy.derive()`` 没传 ``funding_policy``,吃默认值
       ``personal_wallet`` —— 所以生产当前的平台腿发布命令**分母为 0**。
       这条腿是**纵深防御,非承重**。要让它承重,得先由 Owner 拍板
       「谁发布时由平台承担成本」,那是商业语义,不在本包裁定范围。
    """
    cur.execute(
        f"""
        SELECT {', '.join('c.' + col for col in _store.COMMAND_COLUMNS)}
          FROM {_store.COMMAND_TABLE} c
          JOIN {_store.OUTBOX_TABLE} o
            ON o.publish_command_id = c.publish_command_id
         WHERE c.funding_state IN ('frozen', 'exempt_recorded')
           AND c.settled_at IS NULL
           AND c.external_start_at IS NULL
           AND c.provider_call_count = 0
           AND c.command_state NOT IN ('completed','failed','cancelled','quarantined')
           AND o.status = 'needs_review'
         LIMIT %s
        """,
        (int(limit),),
    )
    out: list[ReconcileAction] = []
    for row in _store._rows(cur):                      # noqa: SLF001
        # ══════════════════════════════════════════════════════════════════
        # 🔴 [R3 · Owner 批口径①] 平台成本腿与钱包腿**同构** —— 不再另走一条
        # ══════════════════════════════════════════════════════════════════
        # R2 时这里有一个平台早退分支:只 bump ``command_state``、不碰钱。
        # 那条分支存在的唯一理由是"平台腿的 fundingState 不能动";
        # 而 R2 已经把这条规则收口进 ``store.bump_status`` 的 CASE 了 ——
        # 于是**平台腿可以走完全同一条路**:同一次 ``release_exact``、
        # 同一个真值表、同一句 ``_assert_direction``。
        #
        # 同构比"各写一份"强的地方很具体:平台腿不再有一份**自己的**结算逻辑
        # 需要单独验(「同一谓词写两处 ⇒ 必有一处没人验」)。
        # 代价是这条路必须真的对两种腿都成立 —— 判据就打在这一点上。
        is_platform = _funding.is_platform_cost(str(row["funding_policy"]))
        if not is_platform and str(row["funding_state"]) != "frozen":
            # 🔴 加宽候选集**不许**顺手加宽退款面:非平台腿却不在 ``frozen``,
            #    这个形状本不该存在。不猜 —— 既不退款也不改状态,留给 Z-1
            #    人工队列。(反向对照判据:``test_24b``。)
            logger.warning(
                "[defgeo-reconcile] %s 非平台腿却是 %s,跳过退款",
                row["publish_command_id"], row["funding_state"])
            continue
        # 🔴 顺序不可交换:**先立事实,再让钱跟着事实走**。
        #    ``release_exact`` 会自己重读 canonical state 再验一次钱向
        #    (它不信调用方说的方向 —— 那道自检是对的)。所以必须先把
        #    「零接单」这个 canonical 事实写下来,否则它看到的还是 not_started,
        #    钱向是 ``none``,当场拒绝。
        fresh = _store.bump_status(
            cur, publish_command_id=row["publish_command_id"],
            canonical_publication_state="rejected_no_effect",
            command_state="needs_action",
            status_reason="发布通道暂时不可用，这一篇没有发出去；算力已全额退回，可以稍后再试一次",
        )
        # 🔴 [P0-4] 钱**先真的退成了**,``release_exact`` 才在同一事务里把
        #    fundingState 落成 released 并盖 ``settled_at``(终态写在原语内)。
        #    这里不再有第二句 ``bump_status(funding_state="released")`` ——
        #    原来那一句是无条件的:billing 返 success=false / ambiguous /
        #    相反幂等时,command 照样被写成"已退款",而钱还冻着。
        #    ``commandState`` 上面已经摆成 needs_action,所以不传 terminal。
        outcome = await _funding.release_exact(
            cur, fresh or row, reason="发布通道不可用，一次都没有对外发起，全额退回")
        if not outcome.settled:
            _funding.apply_unsettled(cur, fresh or row, outcome)
            out.append(ReconcileAction(
                row["publish_command_id"], f"settlement_{outcome.verdict}",
                str(row["funding_state"]), _funding_state_now(cur, row["publish_command_id"]),
                f"退款未完成({outcome.reason})—— 资金保持冻结,不写终态",
            ))
            continue
        after = str((_store.get_command_any_tenant(
            cur, publish_command_id=row["publish_command_id"]) or {}).get("funding_state") or "")
        out.append(ReconcileAction(
            row["publish_command_id"], "release_never_dispatched",
            str(row["funding_state"]), after,
            "零 external-start marker = 权威零接单证据 —— §12.1 表格第 4 行",
        ))
    return out


def _quarantine_mirror_conflicts(cur, *, limit: int) -> list[ReconcileAction]:
    """⑤ 镜像与 canonical item 矛盾 → **隔离**,进 Z-1 队列。一行钱都不翻。

    §12.1 末行逐字:「canonical item 优先,资金不随镜像翻转;写 critical alert
    与可操作核验入口」。可操作入口 = ``funding_state='quarantined'`` 之后
    ``store.review_queue`` 能看见它 —— 只 log 不改状态的话,那条"入口"
    在队列里根本不出现,等于没有。
    """
    out: list[ReconcileAction] = []
    for row in _mirror_conflicts(cur, limit=limit):
        before = str(row.get("funding_state") or "")
        if before in ("committed", "released", "quarantined"):
            continue                       # 已终局 / 已隔离:不重复动
        _store.bump_status(
            cur, publish_command_id=row["publish_command_id"],
            funding_state="quarantined", command_state="quarantined",
            status_reason="结果待平台核实，费用已冻结、不会多扣（无需操作）",
        )
        out.append(ReconcileAction(
            row["publish_command_id"], "quarantine_mirror_conflict", before, "quarantined",
            "镜像与 canonical 矛盾 —— canonical 优先,钱不翻转,转人工核验",
        ))
    return out


def _report_revision_mismatch(cur, *, limit: int) -> list[ReconcileAction]:
    """⑥ artifact/文章/媒体 revision 失配 —— **只报不修**,但必须真的跑。

    不自动修的理由见 :func:`_revision_mismatch`(自动修会让漂移变得无痕)。
    "只报"也必须留下痕迹:这里把它记成一条 action,调用方(worker)会把它
    写进日志;它同时是 ``coverage()`` 第 ⑥ 项唯一的真实执行点。
    """
    out: list[ReconcileAction] = []
    for row in _revision_mismatch(cur, limit=limit):
        logger.error(
            "[defgeo-reconcile] %s 绑定漂移:command rev=%s / snapshot rev=%s;"
            " command media=%s / snapshot media=%s",
            row["publish_command_id"], row.get("command_revision"),
            row.get("snapshot_revision"), row.get("command_media_key"),
            row.get("snapshot_media_key"),
        )
        out.append(ReconcileAction(
            row["publish_command_id"], "report_revision_mismatch", "", "",
            "confirm 之后有人改过绑定关系 —— 只报不自动修(自动修会让漂移无痕)",
        ))
    return out


def _frozen_never_dispatched(cur, *, limit: int) -> list[ReconcileAction]:
    """② 已冻结但 worker 未开始 —— 补一条 outbox,**不动钱**。

    🔴 [R2] 候选集同样补 ``exempt_recorded``。本项没有 ``is_platform_cost`` 分支,
       所以不在 Owner 点名的那个类里 —— 但平台腿启用后它一样漏:平台单丢了
       outbox 就**永远**补不回来,而这一项从头到尾一分钱都不碰(只补队列),
       加宽零资金面。判据 ``test_r2_13``。
    """
    cur.execute(
        f"""
        SELECT c.publish_command_id, c.publish_slot_id, c.funding_state
          FROM {_store.COMMAND_TABLE} c
          LEFT JOIN {_store.OUTBOX_TABLE} o
                 ON o.publish_command_id = c.publish_command_id
         WHERE c.funding_state IN ('frozen', 'exempt_recorded')
           AND c.canonical_publication_state = 'not_started'
           AND c.external_start_at IS NULL
           AND o.id IS NULL
           AND c.created_at < NOW() - (%s || ' seconds')::INTERVAL
         LIMIT %s
        """,
        (STUCK_FROZEN_SECONDS, int(limit)),
    )
    out: list[ReconcileAction] = []
    for row in _store._rows(cur):                      # noqa: SLF001 —— 同包读工具
        _store.enqueue_outbox(
            cur, publish_command_id=row["publish_command_id"],
            publish_slot_id=row["publish_slot_id"],
            event_kind="publish_command_created",
        )
        out.append(ReconcileAction(
            row["publish_command_id"], "requeue_outbox", "frozen", "frozen",
            "已冻结但没有 outbox —— 补入队,钱不动",
        ))
    return out


def _external_started_without_outcome(cur, *, limit: int) -> list[ReconcileAction]:
    """③ provider 已接收但本地 ack 缺失 —— 转 ``unknown`` + ``pending_reconciliation``。

    🔴 **不重传、不 release**。重传要赌对方幂等,release 要赌对方没收到 ——
       两个都是赌。转核验是唯一不赌的选项。
    """
    cur.execute(
        f"""
        SELECT publish_command_id, funding_state, canonical_publication_state
          FROM {_store.COMMAND_TABLE}
         WHERE external_start_at IS NOT NULL
           AND canonical_publication_state IN ('not_started','queued','submitting')
           AND external_start_at < NOW() - (%s || ' seconds')::INTERVAL
         LIMIT %s
        """,
        (STUCK_EXTERNAL_SECONDS, int(limit)),
    )
    out: list[ReconcileAction] = []
    for row in _store._rows(cur):                      # noqa: SLF001
        _store.bump_status(
            cur, publish_command_id=row["publish_command_id"],
            canonical_publication_state="unknown",
            funding_state="pending_reconciliation",
            command_state="settlement_pending",
            status_reason="正在向平台核实结果，费用已冻结",
        )
        out.append(ReconcileAction(
            row["publish_command_id"], "hold_unknown",
            str(row["canonical_publication_state"]), "unknown",
            "外调后久无回执 —— 转核验,零重传零退款",
        ))
    return out


def _funding_state_now(cur, publish_command_id: str) -> str:
    """现读一次资金态。**不猜** —— 平台腿的 fundingState 是常量,写没写进去
    只有库知道(``bump_status`` 的 CASE 会静默挡掉平台腿的资金方向)。"""
    row = _store.get_command_any_tenant(cur, publish_command_id=publish_command_id)
    return str((row or {}).get("funding_state") or "")


def _record_unsettled(
    cur, row: dict[str, Any], outcome: Any,
    out: list[ReconcileAction], before: str,
) -> bool:
    """[P0-4] 裁决不是 ``settled`` 时的**共用**处置。返回 True = 可以继续落 action。

    收敛器三条钱向各写一遍"要不要停手"就是三次写错的机会 ——
    「同一谓词写两处 ⇒ 必有一处没人验」。这里收成一处,
    调用点只剩一句 ``if not _record_unsettled(...): continue``。
    """
    if outcome.settled:
        return True
    _funding.apply_unsettled(cur, row, outcome)
    out.append(ReconcileAction(
        row["publish_command_id"], f"settlement_{outcome.verdict}", before,
        _funding_state_now(cur, row["publish_command_id"]),
        f"物理结算未完成({outcome.reason})—— 资金保持冻结,不写终态",
    ))
    return False


async def _outcome_without_settlement(cur, *, limit: int) -> list[ReconcileAction]:
    """④ canonical outcome 已落但 settlement 未完成 —— **原子**完成剩余状态。

    这是 kill window ④ 的收敛器:worker 已经把 canonical state 写进库然后被杀,
    钱还停在 frozen。这里按真值表把它推到终点。

    ═══════════════════════════════════════════════════════════════════
    🔴 [R2] ⑦ 那个洞的**同构姊妹**:候选集不含 ``exempt_recorded``
    ═══════════════════════════════════════════════════════════════════
    候选集原来是 ``funding_state IN ('frozen','pending_reconciliation')``,
    而迁移 044 的 ``chk_defgeo_pcmd_platform_state`` 保证平台腿恒
    ``exempt_recorded`` ⇒ 下面那个 ``is_platform_cost`` 分支**永远够不到候选集**,
    与 ⑦ 同形。后果(平台腿启用后):平台单的 canonical 已经落地,
    ``command_state`` 却永卡非终态 —— 零资金风险(双重保护都在),
    但客户面永远停在"处理中"。

    同款修法:候选集补 ``exempt_recorded`` + 非平台腿补一道「不是这两格就不动」的闸。
    🔴 全类核对(Owner 点名):``reconciler.py`` 里 ``is_platform_cost`` 只有
       **两处**(⑦ 与本函数),没有第三个姊妹 —— 判据
       ``test_r2_40_every_platform_branch_has_exempt_recorded_in_its_candidate_set``
       机械枚举源码,新增第三处却忘了补候选集会当场红。
    """
    cur.execute(
        f"""
        SELECT {', '.join(_store.COMMAND_COLUMNS)}
          FROM {_store.COMMAND_TABLE}
         WHERE funding_state IN ('frozen','pending_reconciliation','exempt_recorded')
           AND settled_at IS NULL
           AND command_state <> 'quarantined'
           AND canonical_publication_state IN
               ('verified_published','rejected_no_effect','failed_no_effect','retracted')
         LIMIT %s
        """,
        (int(limit),),
    )
    rows = _store._rows(cur)                           # noqa: SLF001
    out: list[ReconcileAction] = []
    for row in rows:
        state = str(row["canonical_publication_state"])
        direction = _settle.settlement_direction(state)
        before = str(row["funding_state"])
        # 🔴 [R3 · Owner 批口径①] 与 ⑦ 同一件事:平台腿不再另走一条,
        #    同一个 ``direction``、同一次 ``commit_exact`` / ``release_exact``、
        #    同一句 ``_assert_direction``(平台腿**不豁免真值表**)。
        #    ``funding_state`` 的不变性由 ``bump_status`` 的 CASE 保证,
        #    所以这里不需要、也不该再有一份平台专用的收敛逻辑。
        is_platform = _funding.is_platform_cost(str(row["funding_policy"]))
        if not is_platform and before not in ("frozen", "pending_reconciliation"):
            # 🔴 [R2] 加宽候选集**不许**顺手加宽结算面:非平台腿却停在
            #    ``exempt_recorded``,这个形状本不该存在。不猜 —— 留给 Z-1。
            #    (与 ⑦ 那道闸同形;反向对照判据 ``test_r2_12``。)
            logger.warning(
                "[defgeo-reconcile] %s 非平台腿却是 %s,跳过结算",
                row["publish_command_id"], before)
            continue
        # 🔴 [P0-4] 三条钱向共用同一条纪律:**物理结算成功且方向一致**才落终态。
        #    终态(fundingState + commandState)写在 ``commit_exact``/``release_exact``
        #    内部、与 billing 同一事务、带 statusVersion CAS —— 调用点只负责
        #    说清"成功后该落哪一格 commandState",以及裁决不是 settled 时**停手**。
        if direction == "commit":
            outcome = await _funding.commit_exact(
                cur, row, reason="发布已核实，结算", terminal_command_state="completed")
            if not _record_unsettled(cur, row, outcome, out, before):
                continue
            out.append(ReconcileAction(row["publish_command_id"], "commit", before, "committed",
                                       "canonical verified_published"))
        elif direction == "release":
            outcome = await _funding.release_exact(
                cur, row, reason="确认零接单，退回", terminal_command_state="failed")
            if not _record_unsettled(cur, row, outcome, out, before):
                continue
            out.append(ReconcileAction(row["publish_command_id"], "release", before, "released",
                                       "canonical no-effect,权威零接单"))
        elif direction == "preserve_historical_commit":
            # 下架:历史 committed **保留**,不自动退款、不改写历史 settlement。
            if before != "committed":
                outcome = await _funding.commit_exact(
                    cur, row, reason="下架前已核实发布，历史结算保留",
                    terminal_command_state="completed")
                if not _record_unsettled(cur, row, outcome, out, before):
                    continue
            out.append(ReconcileAction(row["publish_command_id"], "preserve", before, "committed",
                                       "retracted 保留历史 commit"))
    return out


def _mirror_conflicts(cur, *, limit: int = 50) -> list[dict[str, Any]]:
    """⑤ 发布镜像与 canonical item 矛盾 —— **只报警,不翻转资金**(FIN-10)。

    §12.1 末行逐字:「canonical item 优先,资金不随镜像翻转;写 critical alert
    与可操作核验入口」。所以本函数返回**待告警清单**,一行钱都不动。
    """
    cur.execute(
        f"""
        SELECT publish_command_id, canonical_publication_state, funding_state,
               raw_state_source_table, raw_state_value
          FROM {_store.COMMAND_TABLE}
         WHERE canonical_publication_state = 'conflict'
         LIMIT %s
        """,
        (int(limit),),
    )
    return _store._rows(cur)                           # noqa: SLF001


def _revision_mismatch(cur, *, limit: int = 50) -> list[dict[str, Any]]:
    """⑥ artifact/文章/媒体 revision 失配 —— command 冻的与 snapshot 冻的对不上。

    对不上意味着有人在 confirm 之后改过绑定关系。**不自动修复**:
    自动修复等于让漂移变得无痕。报出来进 Z-1 队列。
    """
    cur.execute(
        f"""
        SELECT c.publish_command_id,
               c.article_revision_id AS command_revision,
               s.frozen_payload ->> 'articleRevisionId' AS snapshot_revision,
               c.public_media_key   AS command_media_key,
               s.frozen_payload -> 'decision' ->> 'publicMediaKey' AS snapshot_media_key
          FROM {_store.COMMAND_TABLE} c
          JOIN {_store.SNAPSHOT_TABLE} s
            ON s.decision_snapshot_id = c.decision_snapshot_id
         WHERE c.article_revision_id IS DISTINCT FROM (s.frozen_payload ->> 'articleRevisionId')
            OR c.public_media_key IS DISTINCT FROM
               (s.frozen_payload -> 'decision' ->> 'publicMediaKey')
         LIMIT %s
        """,
        (int(limit),),
    )
    return _store._rows(cur)                           # noqa: SLF001


def stale_pending(cur, *, older_than_seconds: int = PENDING_ALERT_SECONDS) -> list[dict[str, Any]]:
    """Z-1:pending 超 7 天的核验条目。调度任务拿它发 critical alert。"""
    cur.execute(
        f"""
        SELECT publish_command_id, tenant_owner_id, funding_state,
               exact_settlement_points, updated_at,
               EXTRACT(EPOCH FROM (NOW() - updated_at))::BIGINT AS pending_seconds
          FROM {_store.COMMAND_TABLE}
         WHERE funding_state IN ('pending_reconciliation','quarantined')
           AND updated_at < NOW() - (%s || ' seconds')::INTERVAL
         ORDER BY updated_at ASC
        """,
        (int(older_than_seconds),),
    )
    return _store._rows(cur)                           # noqa: SLF001


def census() -> dict[str, Any]:
    """与全包 census 汇总口径一致的别名 —— 汇总器按 ``census`` 收集。

    (第一版只叫 ``coverage``,于是它**不在**汇总里 ——
     而汇总正是判据的分母。少一个模块的分母不会让任何判据变红。)
    """
    return coverage()


def coverage() -> dict[str, Any]:
    """§12.3 六项覆盖的机械声明。判据拿它对账,不看 docstring。"""
    return {
        "reconcilerVersion": RECONCILER_VERSION,
        "items": [
            # 🔴 [包E] 这一行原本指向 ``activation_outbox.orphaned_activation_roots``,
            #    而那个函数当时**零生产调用点** —— 声称被别处覆盖,别处没人跑。
            #    现在指向真的在 cron 里的物化器。
            {"id": 1, "case": "已收款但 activation 未物化",
             "coveredBy": "services/defensive_geo/activation_materializer.materialize_pending",
             "inThisModule": False},
            {"id": 2, "case": "已冻结但 worker 未开始",
             "coveredBy": "_frozen_never_dispatched", "inThisModule": True},
            {"id": 3, "case": "provider 已接收但本地 ack 缺失",
             "coveredBy": "_external_started_without_outcome", "inThisModule": True},
            {"id": 4, "case": "canonical outcome 已落但 settlement 未完成",
             "coveredBy": "_outcome_without_settlement", "inThisModule": True},
            {"id": 5, "case": "发布镜像与 canonical item 矛盾",
             "coveredBy": "_quarantine_mirror_conflicts(转隔离进 Z-1 队列,钱不翻转)",
             "inThisModule": True},
            {"id": 6, "case": "artifact/文章/媒体 revision 失配",
             "coveredBy": "_report_revision_mismatch(只报,不自动修)", "inThisModule": True},
            # 🔴 第七项不在 §12.3 的六项清单里,是包E 接执行器时补的:
            #    「派发通道跑不通、而且一次都没外调过」。没有它,通道故障期间
            #    确认的每一篇都会无限期冻着 —— 那正是 P0-1 症状的复发形态。
            {"id": 7, "case": "队列判定跑不通且零 external-start ⇒ release 一次",
             "coveredBy": "_release_never_dispatched", "inThisModule": True},
        ],
        "pendingAlertSeconds": PENDING_ALERT_SECONDS,
        "neverReleasesStates": sorted(
            s for s in _settle.CANONICAL_STATES
            if _settle.settlement_direction(s) == "hold_or_quarantine"
        ),
    }
