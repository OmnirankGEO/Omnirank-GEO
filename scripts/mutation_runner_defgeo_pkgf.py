"""包F 撕锁自证 —— 每把锁亲手注毒一次,不红就是没在守。

本仓记过:「引用锁当裁定依据前必须亲手注毒」(0x08 假 \\b 负向锁恒绿顶了两轮)、
「变异存活是唯一信号」。所以本包每一条承重判据都在这里配一发。

用法::

    python scripts/mutation_runner_defgeo_pkgf.py            # 全跑
    python scripts/mutation_runner_defgeo_pkgf.py MUT-F03    # 单跑

🔴 变异**不用 git checkout 撤销**(本仓记过:那会连带撤掉工作树里别的改动)。
   这里读原文进内存、写回原文,失败也在 finally 里写回。

🔴 每发变异都必须**语法合法、语义精确**:整段替成废 SQL 只是 blunt kill,
   证明不了那一行判据在守什么(本仓记过)。
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 🔴 [R2] Windows 控制台默认 gbk,``✓`` / 中文 desc 一 print 就
#    ``UnicodeEncodeError``。而它抛在 ``finally`` **之前**的 print 上 ——
#    文件确实被还原了(finally 跑了),但整轮变异**从第一发就断**,
#    只是断得像"跑完了"。本仓记过同形的:退出码/输出不是判据,
#    "工具压根没跑"和"跑完没事"长得一样。
#    子进程那边早就设了 PYTHONIOENCODING,父进程自己反而漏了。
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):       # pragma: no cover - 已重定向等
            pass

BRIDGE = "services/defensive_geo/monitoring/run_ledger_bridge.py"
LEDGER = "services/defensive_geo/monitoring/attempt_ledger.py"
LEGACY = "services/defensive_geo/monitoring/legacy_bridge.py"
MDB = "db/monitoring_db.py"
MON_API = "api/defensive_monitoring_api.py"
LINEAGE = "services/engine_contract.py"   # [包F ⑥] 合同已搬到零依赖模块
TESTER = "tools/ai_visibility/ai_tester.py"

W = "tests/defensive_geo_pkgf_2026_08_23/test_run_ledger_wiring.py"
R = "tests/defensive_geo_pkgf_2026_08_23/test_run_ledger_realchain_pg.py"
F = "tests/defensive_geo_pkgf_2026_08_23/test_engine_fidelity.py"
W3 = "tests/defensive_geo_w3_2026_08_21/test_fixoffix_p1abc_pg.py"
#: [R2] 夹具本身也要能被注毒 —— 保险丝判据打的就是它。
CONFTEST = "tests/defensive_geo_pkgf_2026_08_23/conftest.py"

MUTATIONS: list[dict] = [
    # ── ① 接线(每个 chokepoint 一发)────────────────────────────────
    {
        "id": "MUT-F01", "file": MDB,
        "desc": "①摘掉 claim 时的 open_for_claim —— 账本又变成死函数",
        "from": "        open_for_claim(cur, dict(row))",
        "to": "        pass  # open_for_claim(cur, dict(row))",
        "expect": [f"{W}::test_each_chokepoint_calls_its_hook",
                   f"{R}::test_progress_walks_zero_to_running_to_hundred"],
    },
    {
        "id": "MUT-F02", "file": MDB,
        "desc": "③摘掉失败收口 —— error 格永远收不了口,progress 到不了 100",
        "from": "        close_for_error(\n            cur,",
        "to": "        _ = close_for_error and (\n            cur,",
        "expect": [f"{W}::test_each_chokepoint_calls_its_hook",
                   f"{R}::test_progress_walks_zero_to_running_to_hundred",
                   f"{R}::test_error_is_engine_error_never_absent"],
    },
    {
        "id": "MUT-F03", "file": MDB,
        "desc": "④摘掉 policy skip —— terminal 永远到不了 planned",
        "from": "                record_skip_for_plan(cur, dict(row))",
        "to": "                pass  # record_skip_for_plan(cur, dict(row))",
        "expect": [f"{W}::test_each_chokepoint_calls_its_hook"],
    },
    {
        "id": "MUT-F04", "file": MDB,
        "desc": "⑤摘掉人工确认那一跳 —— 客户确认过了卡上仍写「身份待确认」",
        "from": "                    close_for_human_resolution(",
        "to": "                    _unused_close_for_human_resolution(",
        "expect": [f"{W}::test_each_chokepoint_calls_its_hook"],
    },
    {
        "id": "MUT-F05", "file": BRIDGE,
        "desc": "把崩溃占位的唯一防线摘掉(open 前不再收口旧在飞行)",
        "from": "    reclaim_superseded(cur, plan_cell_id=plan_cell_id)",
        "to": "    pass  # reclaim_superseded(cur, plan_cell_id=plan_cell_id)",
        "expect": [f"{W}::test_internal_recovery_helper_is_called_from_open",
                   f"{R}::test_crashed_inflight_attempt_does_not_block_the_cell_forever"],
    },
    {
        "id": "MUT-F06", "file": BRIDGE,
        "desc": "把 reclaim_superseded 换成恒空转的 reclaim_orphans(假防护)",
        "from": "    reclaim_superseded(cur, plan_cell_id=plan_cell_id)",
        "to": "    reclaim_orphans(cur, plan_cell_ids=(plan_cell_id,))",
        "expect": [f"{W}::test_internal_recovery_helper_is_called_from_open",
                   f"{R}::test_crashed_inflight_attempt_does_not_block_the_cell_forever"],
    },
    # ── fail-soft 两层各一发 ────────────────────────────────────────
    {
        "id": "MUT-F07", "file": BRIDGE,
        "desc": "把 SAVEPOINT 那一层摘掉(改成裸 try)—— 一期那个真 bug 的形态",
        "from": '        cur.execute(f"SAVEPOINT {_SAVEPOINT}")\n    except Exception as exc:  # 调用方事务已废 —— 不是我们造成的,也不去碰',
        "to": '        pass\n    except Exception as exc:  # 调用方事务已废 —— 不是我们造成的,也不去碰',
        "expect": [f"{R}::test_ledger_sql_failure_never_blocks_the_live_chain",
                   f"{W}::test_savepoint_name_is_a_constant_not_interpolated"],
    },
    {
        "id": "MUT-F08", "file": BRIDGE,
        "desc": "把 never_raises 那一层摘掉 —— 账本的 Python 异常逃到现役链",
        "from": "        except Exception as exc:\n            _LOGGER.warning(\n                \"defgeo 账本 %s 整体异常(**不阻断**现役监测):%r\",",
        "to": "        except ZeroDivisionError as exc:\n            _LOGGER.warning(\n                \"defgeo 账本 %s 整体异常(**不阻断**现役监测):%r\",",
        "expect": [f"{R}::test_ledger_python_failure_never_blocks_the_live_chain"],
        # 说明:换成只捕 ZeroDivisionError 是**精确**变异 —— 它保留了
        # "有个 except 在" 的形状,只把作用面收窄。blunt kill(整段删)
        # 证明不了判据在守的是"任何异常"这一位。
        "note": "精确变异:收窄捕获面而不是删掉 except",
    },
    {
        "id": "MUT-F09", "file": LEGACY,
        "desc": "把 legacy 桥改回裸 try/except(一期原样)",
        "from": "    return bool(guarded(cur, \"capture_before_retry_overwrite\", _write))",
        "to": "    try:\n        return _write()\n    except Exception:\n        return False",
        "expect": [f"{W}::test_legacy_bridge_no_longer_uses_a_bare_try_except_around_sql"],
    },
    {
        "id": "MUT-F10", "file": LEGACY,
        "desc": "把 backfill 守卫摘掉 —— 同一次失败在账本里留两行(双算)",
        "from": "    if _cell_is_already_ledgered(cur, plan_cell_id):\n        return False",
        "to": "    if False:\n        return False",
        "expect": [f"{R}::test_backfill_adds_nothing_when_the_cell_is_already_ledgered"],
    },
    # ── 语义位 ──────────────────────────────────────────────────────
    {
        "id": "MUT-F11", "file": BRIDGE,
        "desc": "把 pending_identity 抬成 answered —— 「AI 说的可能不是你」在卡上消失",
        "from": '    state_map = {"succeeded": "answered", "pending_identity": "entity_ambiguous"}',
        "to": '    state_map = {"succeeded": "answered", "pending_identity": "answered"}',
        "expect": [f"{R}::test_human_identity_resolution_appends_answered"],
    },
    {
        "id": "MUT-F12", "file": BRIDGE,
        "desc": "policy_skip 记成 provider 调用过 —— attemptRecords 虚高",
        "from": '    if bool(cell.get("is_planned")):\n        return None',
        "to": '    if False:\n        return None',
        "expect": [f"{R}::test_policy_skip_counts_terminal_but_not_attempted"],
    },
    {
        "id": "MUT-F13", "file": BRIDGE,
        "desc": "账本租约缩到不大于现役上限 —— 会在现役还认为在跑时先收口",
        "from": "ATTEMPT_LEASE_SECONDS = LIVE_CELL_MAX_LEASE_SECONDS + 600",
        "to": "ATTEMPT_LEASE_SECONDS = LIVE_CELL_MAX_LEASE_SECONDS",
        "expect": [f"{W}::test_attempt_lease_is_strictly_longer_than_the_live_cell_lease"],
    },
    {
        "id": "MUT-F14", "file": BRIDGE,
        "desc": "收口码改成自造词 —— 账本与现役 cell 讲两个故事",
        "from": 'RECLAIM_CODE_NOT_DISPATCHED = "worker_lost_before_dispatch"',
        "to": 'RECLAIM_CODE_NOT_DISPATCHED = "ledger_says_lost"',
        "expect": [f"{W}::test_reclaim_codes_are_the_live_chains_own_words"],
    },
    # ── ② 重开 ──────────────────────────────────────────────────────
    {
        "id": "MUT-F15", "file": MON_API,
        "desc": "②把存在性闸摘掉 —— 不存在的 task 又开始编造零进度",
        "from": '            if cur.fetchone() is None:\n                raise _safe_error("NOT_FOUND")',
        "to": '            if False:\n                raise _safe_error("NOT_FOUND")',
        "expect": [f"{W3}::test_p1b_existence_gate_is_permanent",
                   f"{W3}::test_p1b_progress_is_open_and_typed"],
    },
    {
        "id": "MUT-F16", "file": MON_API,
        "desc": "②偷偷加回一道静默关闸",
        "from": "    _require_user(request)\n    _require_brand(request, brand_id)\n\n    conn = _db()",
        "to": '    raise _safe_error("FORBIDDEN")\n    _require_user(request)\n    _require_brand(request, brand_id)\n\n    conn = _db()',
        "expect": [f"{W3}::test_p1b_progress_is_open_and_typed"],
    },
    # ── ⑥ 保真锁 ────────────────────────────────────────────────────
    {
        "id": "MUT-F17", "file": LINEAGE,
        "desc": "⑥模型名漂移回 qwen3-max",
        "from": '    "model": "qwen3.7-plus",',
        "to": '    "model": "qwen3-max",',
        "expect": [f"{F}::test_qwen_model_is_the_owner_ruled_one",
                   f"{F}::test_cost_accounting_model_equals_the_called_model"],
    },
    {
        "id": "MUT-F18", "file": LINEAGE,
        "desc": "⑥端点漂移回 text-generation(那条路 3.7-plus 必 400)",
        "from": '        "multimodal-generation/generation"',
        "to": '        "text-generation/generation"',
        "expect": [f"{F}::test_qwen_endpoint_is_the_multimodal_one"],
    },
    {
        "id": "MUT-F19", "file": LINEAGE,
        "desc": "⑥把角标参数摘掉 —— 正文角标静默消失",
        "from": '        "enable_citation": True,',
        "to": '        "enable_citation": False,',
        "expect": [f"{F}::test_search_options_are_exactly_the_probed_set"],
    },
    {
        "id": "MUT-F20", "file": TESTER,
        "desc": "⑥请求体绕过 SSOT,手写模型名(分叉复发)",
        "from": '                        "model": _QWEN["model"],',
        "to": '                        "model": "qwen3-max",',
        "expect": [f"{F}::test_request_body_takes_the_model_from_the_ssot"],
    },
    {
        "id": "MUT-F21", "file": TESTER,
        "desc": "⑥content 退回字符串形状 —— 多模态端点会 400",
        "from": '                            {"role": "user", "content": [{"text": query}]},',
        "to": '                            {"role": "user", "content": query},',
        "expect": [f"{F}::test_multimodal_content_is_an_array"],
    },
    {
        "id": "MUT-F22", "file": TESTER,
        "desc": "⑥提取器不折平数组 —— 换代后监测全线静默失败",
        "from": "    if isinstance(content, list):",
        "to": "    if False:",
        "expect": [f"{F}::test_extractor_flattens_the_multimodal_array"],
    },
    # ══════════════════════════════════════════════════════════════════
    # [R2] 返修新增判据的撕锁 —— F-2 / F-1 / F-3
    # ══════════════════════════════════════════════════════════════════
    # ── F-2:enable_thinking(资金 × 质量)────────────────────────────
    {
        "id": "MUT-F23", "file": LINEAGE,
        "desc": "F-2 把 enable_thinking 从 SSOT 里删掉 —— 回到 R1 那个每次流血的形状",
        "from": "        \"enable_thinking\": False,\n        \"enable_search\": True,",
        "to": "        \"enable_search\": True,",
        "expect": [f"{F}::test_base_parameters_are_exactly_the_probed_set",
                   f"{F}::test_thinking_is_off_by_identity_not_by_falsiness",
                   f"{F}::test_assembled_request_body_carries_thinking_off"],
    },
    {
        "id": "MUT-F24", "file": LINEAGE,
        "desc": "F-2 enable_thinking 写成 0(falsy 但不是 False)—— dict 相等照样过",
        "from": "        \"enable_thinking\": False,",
        "to": "        \"enable_thinking\": 0,",
        "expect": [f"{F}::test_thinking_is_off_by_identity_not_by_falsiness"],
    },
    {
        "id": "MUT-F25", "file": TESTER,
        "desc": "F-2 调用点退回手写参数 —— 参数漂出 SSOT 的那个形态",
        "from": "                            **dict(_QWEN[\"base_parameters\"]),\n"
                "                            \"max_tokens\": max_tokens,",
        "to": "                            \"max_tokens\": max_tokens,\n"
              "                            \"enable_search\": True,",
        "expect": [f"{F}::test_request_parameters_all_come_from_the_ssot",
                   f"{F}::test_assembled_request_body_carries_thinking_off"],
    },
    {
        "id": "MUT-F26", "file": TESTER,
        "desc": "R2 实修:第二处消费者退回裸取 content —— 200 却判 engine_error",
        "from": "    ai_response = _dashscope_message_text(output)",
        "to": "    ai_response = output.get(\"choices\", [{}])[0]"
              ".get(\"message\", {}).get(\"content\", \"\")",
        "expect": [f"{F}::test_no_second_unflattened_reader_of_the_native_content",
                   f"{F}::test_native_citation_extractor_eats_the_multimodal_array",
                   f"{F}::test_assembled_request_body_carries_thinking_off"],
    },
    # ── F-1:成功路径 + 三条补写的判据 ───────────────────────────────
    {
        "id": "MUT-F27", "file": MDB,
        "desc": "F-1 Review 毒①:close_for_result 改绑 no-op(调用行还在,AST 锁照绿)",
        "from": "                close_for_result,\n            )\n            cursor.execute(",
        "to": "                close_for_result,\n            )\n"
              "            close_for_result = lambda *a, **k: None\n"
              "            cursor.execute(",
        "expect": [f"{R}::test_success_path_writes_an_answered_row_through_the_live_chain",
                   f"{R}::test_pending_identity_closes_as_entity_ambiguous_through_the_live_chain"],
    },
    {
        "id": "MUT-F28", "file": MDB,
        "desc": "F-1 收口不带实际模型 —— actual_* 里留着计划值,漂移永远发现不了",
        "from": "                observed_model=lineage_payload[\"model\"],",
        "to": "                observed_model=None,",
        "expect": [f"{R}::test_success_path_overwrites_the_planned_lineage_with_the_observed_one"],
    },
    {
        "id": "MUT-F29", "file": BRIDGE,
        "desc": "F-1 摘掉 reclaim 的权威分支① —— 只剩超时猜,收敛既慢又漏",
        "from": "                    EXISTS (SELECT 1 FROM public.monitoring_run_cells c\n"
                "                             WHERE c.id = a.monitoring_cell_id\n"
                "                               AND c.state <> 'running')",
        "to": "                    FALSE",
        "expect": [f"{R}::test_reclaim_prefers_cell_state_over_timeout"],
    },
    {
        "id": "MUT-F30", "file": BRIDGE,
        "desc": "F-1 摘掉 reclaim 的回落分支② —— cell 行没了的那批永久悬挂",
        "from": "                    OR (NOT EXISTS (SELECT 1 FROM public.monitoring_run_cells c",
        "to": "                    OR (FALSE AND NOT EXISTS "
              "(SELECT 1 FROM public.monitoring_run_cells c",
        "expect": [f"{R}::test_reclaim_falls_back_to_the_lease_only_when_no_cell_row_exists"],
    },
    {
        "id": "MUT-F31", "file": BRIDGE,
        "desc": "F-1 _planned_lineage 手写模型名 —— 合同一改账本就记着旧名",
        "from": "        \"actual_model\": str(contract.get(\"model\") or \"unknown\"),",
        "to": "        \"actual_model\": \"qwen3.7-plus\",",
        "expect": [f"{R}::test_planned_model_tracks_the_platform_contract"],
    },
    {
        "id": "MUT-F32", "file": CONFTEST,
        "desc": "F-1 保险丝:cur 回到 autocommit —— SAVEPOINT 静默失效,整片判据测空气",
        "from": "    conn.autocommit = False          # ← 与生产同构,SAVEPOINT 才真的存在",
        "to": "    conn.autocommit = True",
        "expect": [f"{R}::test_savepoint_guard_is_actually_active_in_this_fixture"],
    },
    # ── F-3:三条绕过路径 ────────────────────────────────────────────
    {
        "id": "MUT-F33", "file": MDB,
        "desc": "F-3a 从 sweeper 收割里摘掉 reclaim —— 每小时留下永久悬挂的在飞行",
        "from": "            reclaim_orphans(cur, monitoring_cell_ids=reaped)",
        "to": "            pass  # reclaim_orphans(cur, monitoring_cell_ids=reaped)",
        "expect": [f"{R}::test_sweeper_reaping_an_abandoned_task_also_closes_the_ledger",
                   f"{R}::test_sweeper_reaping_an_undispatched_cell_says_so"],
    },
    {
        "id": "MUT-F34", "file": MDB,
        "desc": "F-3b 从退款释放里摘掉 reclaim —— 同一 cron 链的同一后果",
        "from": "            reclaim_orphans(cur, monitoring_cell_ids=released)",
        "to": "            pass  # reclaim_orphans(cur, monitoring_cell_ids=released)",
        "expect": [f"{R}::test_refund_release_also_closes_the_ledger"],
    },
    {
        "id": "MUT-F35", "file": MDB,
        "desc": "F-3c 新增第四条产出路径(不接账本)—— census 必须当场发现",
        "from": "def list_monitoring_run_cells(task_id: int, *, brand_id: int = None)",
        "to": "def _mutation_fourth_producer(cur, cell_id):\n"
              "    cur.execute(\"UPDATE public.monitoring_run_cells SET "
              "state='pending_provider_confirmation' WHERE id=%s\", (cell_id,))\n\n\n"
              "def list_monitoring_run_cells(task_id: int, *, brand_id: int = None)",
        "expect": [f"{R}::test_every_producer_of_pending_provider_confirmation_closes_the_ledger"],
    },
    {
        "id": "MUT-F36", "file": MDB,
        "desc": "⑥.2 计价行换回旧模型 —— 按一个模型收钱、用另一个模型干活",
        "from": "        \"dashscope\": (\"dashscope\", _QWEN_MODEL),",
        "to": "        \"dashscope\": (\"dashscope\", \"qwen3-max\"),",
        "expect": [f"{R}::test_cost_row_records_the_called_model",
                   f"{F}::test_cost_accounting_model_equals_the_called_model"],
    },
    # ══════════════════════════════════════════════════════════════════
    # [R3] 「改绑不删行」形变异 —— 结构锁照绿,只有行为判据能抓
    # ══════════════════════════════════════════════════════════════════
    # 这是 Review 2026-08-24 亲手用来打穿 ④⑤ 的那个毒形。
    # 调用行**保留**,所以 AST 接线锁一条都不会红。
    {
        "id": "MUT-F37", "file": MDB,
        "desc": "R3 ④ record_skip_for_plan 改绑 no-op(调用行保留)",
        "from": "                )\n                record_skip_for_plan(cur, dict(row))",
        "to": "                )\n"
              "                record_skip_for_plan = lambda *a, **k: None\n"
              "                record_skip_for_plan(cur, dict(row))",
        "expect": [f"{R}::test_create_run_cells_records_policy_skip_through_the_live_chain"],
    },
    {
        "id": "MUT-F38", "file": MDB,
        "desc": "R3 ⑤ close_for_human_resolution 改绑 no-op(调用行保留)",
        "from": "                    close_for_human_resolution,\n                )\n                cur.execute(",
        "to": "                    close_for_human_resolution,\n                )\n"
              "                close_for_human_resolution = lambda *a, **k: None\n"
              "                cur.execute(",
        "expect": [f"{R}::test_identity_review_appends_answered_through_the_live_chain"],
    },
    {
        "id": "MUT-F39", "file": MDB,
        "desc": "R3 ①重试臂 open_for_claim 改绑 no-op —— 重试那次调用不进账本",
        "from": "        from services.defensive_geo.monitoring.run_ledger_bridge import open_for_claim\n"
                "        open_for_claim(cur, dict(claimed))",
        "to": "        from services.defensive_geo.monitoring.run_ledger_bridge import open_for_claim\n"
              "        open_for_claim = lambda *a, **k: None\n"
              "        open_for_claim(cur, dict(claimed))",
        "expect": [f"{R}::test_retry_reservation_opens_a_new_attempt_through_the_live_chain"],
    },
    {
        "id": "MUT-F40", "file": MDB,
        "desc": "R3 租约恢复 reclaim_orphans 改绑 no-op —— 在飞行永久占位",
        "from": "    from services.defensive_geo.monitoring.run_ledger_bridge import reclaim_orphans\n",
        "to": "    from services.defensive_geo.monitoring.run_ledger_bridge import reclaim_orphans\n"
              "    reclaim_orphans = lambda *a, **k: None\n",
        "expect": [f"{R}::test_expired_lease_recovery_closes_the_ledger_through_the_live_chain"],
    },
    # ── meta 锁自毒族 ───────────────────────────────────────────────
    # 🔴 上面那些毒的是**被测代码**;这一族毒的是**锁自己**。
    #    一把 meta 锁如果没人毒过它,它保证的是"我在跑",不是"我在守"。
    #    F41 拆它守的对象(行为判据),F42 拆它的判别力(尺子退化),
    #    R4 的 F43/F44/F45 同族(拆键域分母 + 拆键域尺子)。
    {
        "id": "MUT-F41", "file": R,
        "desc": "meta 锁自毒:把 ④ 的行为判据摘掉 —— 接线点退回「只有结构锁」",
        "from": "def test_create_run_cells_records_policy_skip_through_the_live_chain(",
        "to": "def _disabled_create_run_cells_records_policy_skip_through_the_live_chain(",
        "expect": [f"{W}::test_every_wired_chokepoint_has_a_behavioural_criterion"],
    },
    {
        "id": "MUT-F42", "file": W,
        "desc": "meta 锁自毒:退化成只看 import(不看调用)—— 空判据会蒙混过关",
        "from": "            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \\\n"
                "                    and n.func.id in imported:\n"
                "                driven.setdefault(n.func.id, set()).add(fn.name)\n"
                "    return driven",
        "to": "            pass\n"
              "        for _name in imported:\n"
              "            driven.setdefault(_name, set()).add(fn.name)\n"
              "    return driven",
        "expect": [f"{W}::test_the_behavioural_census_can_tell_import_from_call"],
    },
    # ══════════════════════════════════════════════════════════════════
    # [R4] 键域机械分母 —— Review 毒C 与它的镜像(F43/F44 拆分母,F45 拆尺子;
    #      三发同属「meta 锁自毒族」)
    # ══════════════════════════════════════════════════════════════════
    {
        "id": "MUT-F43", "file": W,
        "desc": "R4 毒C:从 EXPECTED_HOOKS 删掉 retry 键 —— R3 时 22 条全绿存活",
        "from": '    "reserve_monitoring_cell_retry": ("open_for_claim",),\n',
        "to": "",
        "expect": [f"{W}::test_expected_hooks_key_domain_equals_the_production_census"],
    },
    {
        "id": "MUT-F44", "file": W,
        "desc": "R4 幽灵键:登记一个生产侧根本不接线的 chokepoint",
        "from": '    "claim_monitoring_run_cell": ("open_for_claim",),\n',
        "to": '    "claim_monitoring_run_cell": ("open_for_claim",),\n'
              '    "list_monitoring_run_cells": ("open_for_claim",),\n',
        "expect": [f"{W}::test_expected_hooks_key_domain_equals_the_production_census"],
    },
    {
        "id": "MUT-F45", "file": W,
        "desc": "R4 键域尺子退化成恒空 —— 双向相等会因双空而恒真",
        "from": "        if node.func.id not in hooks:\n            continue",
        "to": "        if node.func.id not in hooks:\n            continue\n"
              "        if True:\n            continue",
        "expect": [f"{W}::test_the_production_hook_census_has_discriminating_power"],
    },
    {
        "id": "MUT-F46", "file": MDB,
        "desc": "R4 ⑤ admin 授权臂:把 admin 判定写死为 False —— 非 owner 的 admin 被误拒",
        "from": "        live_is_admin = bool(cur.fetchone())",
        "to": "        live_is_admin = False",
        "expect": [f"{R}::test_identity_review_by_a_non_owner_admin_also_appends_answered"],
    },
]


def _run_pytest(node_ids: list[str]) -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_pkgf_test")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-x", *node_ids],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


#: pytest 退出码。**只有 1 才是"判据把变异打红了"**。
#:
#: 🔴 [R2] R1 这里写的是 ``if rc != 0: KILLED`` —— 那是一个假绿源:
#:    · rc=4 用法错误(node id 拼错)· rc=5 一条都没收集到
#:    · rc=2/3 中断 / 内部错误
#:    这几种都是"**判据压根没跑**",而 R1 会把它们全记成 KILLED。
#:    也就是说:``expect`` 里但凡有一个 node id 拼错,那一发变异不用跑
#:    就自动"通过"。本仓记过一模一样的:我把 rc=4 读成了 kill。
_PYTEST_FAILED = 1


def _preflight(picked: list[dict]) -> list[str]:
    """开跑前证明每个 ``expect`` node id **真的能被收集到**。

    这是本轮的分母自证。没有它,拼错的 node id 会让那一发变异
    "零成本通过";而拼错在 14 条手写 node id 里几乎必然发生。
    """
    node_ids = sorted({n for m in picked for n in m["expect"]})
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_admin:testpw@localhost:55475/geo_defgeo_pkgf_test")
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "--collect-only", *node_ids],
        cwd=str(ROOT), env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace")
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        return []
    missing = [n for n in node_ids if n.rsplit("::", 1)[-1] not in out]
    return missing or [f"(收集失败但说不清哪一条)rc={proc.returncode}\n{out[-1500:]}"]


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else None
    picked = [m for m in MUTATIONS if not only or m["id"] == only]
    if not picked:
        print(f"no mutation matches {only!r}")
        return 2

    print(f"preflight: 核对 {len(picked)} 发变异引用的 node id 都收集得到 …")
    missing = _preflight(picked)
    if missing:
        print("  ! 以下 node id 收集不到 —— 先修它们,否则那几发变异是零成本通过:")
        for n in missing:
            print(f"      {n}")
        return 2
    print("  ✓ 全部可收集\n")

    killed, survived, broken = [], [], []
    for m in picked:
        path = ROOT / m["file"]
        original = io.open(path, encoding="utf-8", newline="").read()
        if m["from"] not in original:
            msg = f"{m['id']}: `from` 在源码里找不到 —— 变异本身没打上"
            if m.get("optional"):
                print(f"  ~ SKIP {msg}")
                continue
            print(f"  ! BROKEN {msg}")
            broken.append(m["id"])
            continue
        mutated = original.replace(m["from"], m["to"], 1)
        try:
            io.open(path, "w", encoding="utf-8", newline="").write(mutated)
            rc, out = _run_pytest(m["expect"])
            if rc == _PYTEST_FAILED:
                print(f"  ✓ KILLED  {m['id']}  {m['desc']}")
                killed.append(m["id"])
            elif rc == 0:
                print(f"  ✗ SURVIVED {m['id']}  {m['desc']}")
                print("      ↑ 判据没在守这一位。存活是唯一信号。")
                survived.append(m["id"])
            else:
                # 🔴 不是红、也不是绿 —— 是**没跑**。记 BROKEN,不许当 kill。
                print(f"  ! BROKEN {m['id']} 判据没跑起来(pytest rc={rc},"
                      f"不是 1)—— 这不算把变异打红")
                print("      " + out.strip().splitlines()[-1][:200] if out.strip() else "")
                broken.append(m["id"])
        except Exception as exc:               # noqa: BLE001 - 一发炸不许带走整轮
            print(f"  ! BROKEN {m['id']} 跑变异时自己抛了:{exc!r}")
            broken.append(m["id"])
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(original)

    print()
    accounted = len(killed) + len(survived) + len(broken)
    skipped = len(picked) - accounted
    print(f"发数={len(picked)}  killed={len(killed)}  survived={len(survived)}  "
          f"broken={len(broken)}  skipped={skipped}")
    if survived:
        print("SURVIVED:", ", ".join(survived))
    if broken:
        print("BROKEN  :", ", ".join(broken))
    # 🔴 完整性自证:每一发都必须有归宿。中途崩掉会让报告"看起来跑完了"。
    if accounted + max(skipped, 0) != len(picked) or skipped < 0:
        print(f"! 账对不上:{len(picked)} 发只归了 {accounted} 发 —— 这一轮不算数")
        return 2
    return 1 if (survived or broken) else 0


if __name__ == "__main__":
    raise SystemExit(main())
