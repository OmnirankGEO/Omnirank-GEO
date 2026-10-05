"""P4 缺口作战计划 · 变异 runner(判别力自证)

跑法(Git Bash / PowerShell 都行):
    TEST_DATABASE_URL=postgresql://geo_admin:gaptest@127.0.0.1:55610/geo_gapplan_test \
    python -X utf8 tests/gap_plan_2026_08_08/mutation_runner.py

每条变异:改一处实现 → 断言 `expect_red` 里**每一条**测试都真的转红 → 还原 → 逐字节核对。
「有一条红就算过」是自欺:那只证明某条锁碰巧覆盖到,不证明我点名的那条锁在守它。

🔴 三个 Windows 坑(2026-08-08 记忆,已在本 runner 里堵上):
  1. 行尾:全程二进制读写(Buffer 语义),不让 Python 的文本模式把 LF 翻成 CRLF ——
     翻了会导致"还原后逐字节不一致"这条自检恒红,然后人就会去关掉那条自检。
  2. `__pycache__`:等字节替换(改一个字符不改长度)时 mtime 粒度可能骗过缓存,
     每次变异后主动清 __pycache__。
  3. 存活先分诊:变异没杀掉,先问"是锁弱"还是"这个变异根本是空操作"。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = "tests/gap_plan_2026_08_08"

MUTATIONS = [
    {
        "id": "M1",
        "file": "services/gap_operation_plan.py",
        "desc": "容量守卫恒 True(Capacity.executable 不再看容量)",
        "edits": [(
            "        return self.available > 0",
            "        return True  # MUTATION M1",
        )],
        "expect_red": [
            "test_capacity_zero_never_emits_write_action",
            "test_capacity_guard_lives_on_the_server_not_the_client",
        ],
    },
    {
        "id": "M2",
        "file": "services/gap_operation_plan.py",
        "desc": "拆掉 _actions_for_item 唯一那道写动作剔除",
        "edits": [(
            "    if not capacity.executable:\n        ids = [a for a in ids if a not in _WRITE_ACTIONS]\n    return [translate_action(a) for a in ids]",
            "    return [translate_action(a) for a in ids]  # MUTATION M2",
        )],
        # 🔴 第一版这里写的是 test_capacity_zero_never_emits_write_action —— 变异存活。
        #    分诊结论:**不是锁弱,是这个变异对 372 那条路径是空操作**。
        #    _allocation_code_for 在容量不足时早把状态降级成 capacity_zero,
        #    ids 里压根没有写动作可剔,端到端那条锁自然照绿。
        #    真正守着这道的是直打函数、喂"上游产生不出来的组合"的那条单元锁。
        "expect_red": ["test_actions_guard_strips_write_actions_even_if_state_says_ready"],
    },
    {
        "id": "M3",
        "file": "api/gap_plan_api.py",
        "desc": "删掉 item 端点的代际校验(旧链接照样预填)",
        "edits": [(
            "    if (expected_authority_generation is not None\n"
            "            and int(expected_authority_generation) != int(snapshot[\"authority_generation\"])):\n"
            "        raise _error(\"plan_item_generation_stale\", primary=\"back_to_plan\",\n"
            "                     secondary=\"retry_snapshot\", status_code=409)",
            "    pass  # MUTATION M3",
        )],
        "expect_red": ["test_stale_generation_is_rejected"],
    },
    {
        "id": "M4",
        "file": "services/gap_operation_labels.py",
        "desc": "术语泄漏闸清空供应商名单(闸还在,但什么都不查)",
        "edits": [(
            '_SUPPLIER_TERMS = (\n    "deepseek",',
            '_SUPPLIER_TERMS = (  # MUTATION M4\n    "zzz_never_matches",',
        )],
        # 🔴 第一版把 test_evidence_platforms_are_anonymized 也列进来,它没转红。
        #    分诊:那条锁自己直接断言 body 里不含供应商名,**不经过扫描器** ——
        #    即测试用例打偏了,不是实现好。扫描器的判别力由下面这条独扛。
        "expect_red": ["test_leak_scanner_actually_catches_a_planted_term"],
    },
    {
        "id": "M5",
        "file": "services/gap_operation_map.py",
        "desc": "从操作地图里删掉「交付计划」入口(判据 #9 的正主)",
        "edits": [(
            '        operation_id="gap_plan_block",',
            '        operation_id="gap_plan_block_REMOVED",  # MUTATION M5',
        )],
        "expect_red": [
            "test_gap_plan_entry_resolves_to_pricing",
            "test_match_finds_the_entry_by_human_phrasing",
        ],
    },
    {
        "id": "M6",
        "file": "services/gap_operation_plan.py",
        "desc": "推荐机器原始项整包透传 + frozen_spec 整包展开(内部采购字段直达浏览器)",
        # 🔴 这条改了两版才有判别力,两次都是"变异是空操作",不是锁弱:
        #    v1 只改 _media_candidates 的白名单转录 → cand 变肥了,但 cand 不进出参;
        #    v2 只改 frozen_spec 展开 cand → cand 已被上一层洗过,展开出来还是那四个键。
        #    真正的泄漏要**两层同时破**才发生 —— 所以这条变异必须是两处一起改。
        #    这也正说明那两层不是冗余:任何一层还在,内部采购口径就出不去。
        "edits": [
            (
                "            out.append({\n",
                "            out.append({**entry,  # MUTATION M6\n",
            ),
            (
                '            "frozen_spec_jsonb": {\n                "target_question": question,',
                '            "frozen_spec_jsonb": {**cand,  # MUTATION M6\n                "target_question": question,',
            ),
        ],
        "expect_red": ["test_no_cost_fields_leak_from_the_recommender"],
    },
    {
        "id": "M7",
        "file": "db/gap_plan_db.py",
        "desc": "填链接时顺手点亮「已收录」(跳级)",
        "edits": [(
            "                publication_domain, published_at, evidence_published,\n"
            "                submitted_by, idempotency_key\n"
            "            ) VALUES (%s,%s,%s,%s,%s,%s, TRUE, %s,%s)",
            "                publication_domain, published_at, evidence_published,\n"
            "                evidence_indexed, submitted_by, idempotency_key\n"
            "            ) VALUES (%s,%s,%s,%s,%s,%s, TRUE, TRUE, %s,%s)  -- MUTATION M7",
        )],
        "expect_red": ["test_publication_link_lights_only_published_and_queues_7_14_30"],
    },
    {
        "id": "M8",
        "file": "services/gap_operation_plan.py",
        "desc": "把可及性核实结论踢出事实集(不参与 data_version)",
        "edits": [(
            '        "assessments": {\n'
            '            k: {"entry_assessment": v.get("entry_assessment")}\n'
            '            for k, v in sorted(assessments.items())\n'
            '        },',
            '        "assessments": {},  # MUTATION M8',
        )],
        "expect_red": ["test_marking_domain_unreachable_writes_back_and_recomputes"],
    },
    {
        "id": "M10",
        "file": "services/gap_operation_plan.py",
        "desc": "容量退回 P4 自算(不再读 P1 合同,改回 quotes.total_articles)",
        # 🔴 这条守的是**并轨本身**。没有它,哪天有人把 compute_capacity 改回自算,
        #    所有锁照绿(数字碰巧一样),而口径已经悄悄分叉成两套。
        "edits": [(
            "        view = capacity_contract.resolve_quote_capacity(cur, int(quote[\"id\"]))",
            "        view = {\"display_status\": \"capacity_zero\", \"authorized_articles\":"
            " int(quote.get(\"total_articles\") or 0), \"consumed_articles\": 0,"
            " \"available_articles\": int(quote.get(\"total_articles\") or 0),"
            " \"reserved_articles\": 0, \"semantics\": \"x\"}  # MUTATION M10",
        )],
        "expect_red": [
            "test_capacity_zero_never_emits_write_action",
            "test_snapshot_records_which_capacity_source_it_used",
        ],
    },
    {
        "id": "M11",
        "file": "services/gap_operation_plan.py",
        "desc": "不足额原因不再回流(说不出为什么也当成正常完成)",
        "edits": [(
            "        code = _SHORTFALL_REASON_BY_ALLOCATION.get(item.get(\"allocation_code\"))",
            "        code = None  # MUTATION M11",
        )],
        "expect_red": ["test_shortfall_reasons_flow_back_to_the_contract"],
    },
    {
        "id": "M12",
        "file": "services/gap_operation_plan.py",
        "desc": "合同返回未知三态时不再 fail-closed(静默当可写)",
        "edits": [(
            "        if status not in capacity_contract.CAPACITY_STATUSES:",
            "        if False:  # MUTATION M12",
        )],
        "expect_red": ["test_unknown_contract_status_is_fail_closed"],
    },
    {
        "id": "M9",
        "file": "api/gap_plan_api.py",
        "desc": "归属校验换成「只要登录就放行」(A5 拆门)",
        "edits": [(
            '    quote = require_quote_access(request, int(quote_id), allow_null=False)\n'
            '    try:\n'
            '        publications = gap_plan_db.get_publications(int(quote_id))',
            '    from db.diagnosis_db import get_quote as _gq  # MUTATION M9\n'
            '    quote = _gq(int(quote_id))\n'
            '    try:\n'
            '        publications = gap_plan_db.get_publications(int(quote_id))',
        )],
        "expect_red": ["test_other_tenant_cannot_read_the_plan"],
    },
    # ── 2026-08-08 追加:动作落点(小榜运营回答原来一个跳转按钮都出不来)──
    {
        "id": "M13",
        "file": "services/gap_assistant.py",
        "desc": "不再给动作补落点(运营回答退回「只有文字没有按钮」)",
        "edits": [(
            "        actions=_with_routes(actions),",
            "        actions=actions,  # MUTATION M13",
        )],
        "expect_red": [
            "test_every_operations_answer_has_at_least_one_place_to_go",
            "test_mapped_actions_do_fire_when_the_focus_card_carries_them",
        ],
    },
    {
        "id": "M14",
        "file": "services/gap_assistant.py",
        "desc": "不追加兜底落点(真实报价 372/900 又变成没有可点按钮)",
        "edits": [(
            "        actions = actions + [translate_action(_PLAN_LANDING_ACTION)]",
            "        actions = actions  # MUTATION M14",
        )],
        "expect_red": ["test_every_operations_answer_has_at_least_one_place_to_go"],
    },
    {
        "id": "M15",
        "file": "services/gap_operation_map.py",
        "desc": "查不到映射就回落到默认页(把「宁可没按钮」改成「给个八成不对的落点」)",
        "edits": [(
            "    operation_id = _ACTION_TO_OPERATION.get(action_id)",
            '    operation_id = _ACTION_TO_OPERATION.get(action_id, "quote_center")  # MUTATION M15',
        )],
        "expect_red": ["test_unmapped_action_gets_no_route_at_all"],
    },
    {
        "id": "M16",
        "file": "services/gap_assistant.py",
        "desc": "按动作类型查表的落点覆盖导航回答按问题匹配出来的精确落点",
        "edits": [(
            '        if action.get("target_route"):',
            "        if False:  # MUTATION M16",
        )],
        # 🔴 真链路那条锁对这个变异是**零判别力**(带落点的 go_to_signed_route 不在
        #    映射表里,拆了守卫也没东西能覆盖它)。判据必须打在构造用例上。
        "expect_red": ["test_a_preset_landing_is_never_overwritten_by_the_action_table"],
    },
    {
        "id": "M17",
        "file": "services/gap_operation_map.py",
        "desc": "把写类动作也塞进落点表(小榜可直接把人送到执行面前)",
        "edits": [(
            '    "open_delivery_plan": "gap_plan_block",',
            '    "open_delivery_plan": "gap_plan_block",\n'
            '    "open_writing_task": "writing_center",  # MUTATION M17',
        )],
        "expect_red": ["test_write_actions_are_never_mapped"],
    },
    {
        "id": "M18",
        "file": "services/gap_operation_map.py",
        "desc": "落点指向 App.tsx 里不存在的路由",
        "edits": [(
            '        route="/publish",\n'
            '        breadcrumb=("发布投放", "媒体建议"),',
            '        route="/media",  # MUTATION M18\n'
            '        breadcrumb=("发布投放", "媒体建议"),',
        )],
        "expect_red": ["test_every_mapped_action_lands_on_a_route_that_really_exists"],
    },

    # ══════════════════════════════════════════════════════════════
    # WO_GAPPLAN_RELOCATION_A 2026-08-10 追加(阶段 1 执行闸 + 阶段 2 受众裁剪)
    # ══════════════════════════════════════════════════════════════
    {
        "id": "M19",
        "file": "services/gap_operation_plan.py",
        "desc": "阶段 1:执行闸恒开 → 未付款报价在报价页展开一个点不动的执行面",
        "edits": [(
            "    is_open = bool(capacity.executable)",
            "    is_open = True  # MUTATION M19",
        )],
        "expect_red": [
            "test_execution_gate_is_closed_when_capacity_is_zero",
            "test_execution_gate_never_diverges_from_the_capacity_guard",
        ],
    },
    {
        "id": "M20",
        "file": "services/gap_operation_plan.py",
        "desc": "阶段 2:受众判定恒 agent(customer 分支形同虚设)",
        "edits": [(
            "    for_customer = audience == AUDIENCE_CUSTOMER",
            "    for_customer = False  # MUTATION M20",
        )],
        "expect_red": [
            "test_customer_audience_emits_no_action_even_when_agent_side_can_write",
            "test_customer_audience_drops_operator_only_blocks",
            "test_selection_page_serves_the_customer_cropped_plan",
        ],
    },
    {
        "id": "M21",
        "file": "services/gap_operation_plan.py",
        "desc": "阶段 2:客户版「怎么发」不再替换 → access 话术从 rationale 漏给客户",
        "edits": [(
            '        rationale["how"] = translate_status("access_arranged_by_team")["explanation"]',
            "        pass  # MUTATION M21",
        )],
        "expect_red": ["test_customer_audience_has_no_channel_access_wording"],
    },
    {
        "id": "M22",
        "file": "services/gap_operation_plan.py",
        "desc": "阶段 2:客户版不再过滤已合并项 → 交付量被说大",
        "edits": [(
            '        if item.get("allocation_code") == "rejected_duplicate_coverage":\n'
            "            continue",
            "        if False:  # MUTATION M22\n"
            "            continue",
        )],
        "expect_red": ["test_customer_preview_drops_merged_items_and_renumbers"],
    },
    {
        "id": "M23",
        "file": "api/selection_api.py",
        "desc": "阶段 2:客户页误用 agent 受众 → 执行按钮直接发到客户浏览器",
        "edits": [(
            "        bundle, capacity=capacity, audience=plan_service.AUDIENCE_CUSTOMER",
            "        bundle, capacity=capacity, audience=plan_service.AUDIENCE_AGENT  # MUTATION M23",
        )],
        "expect_red": ["test_selection_page_serves_the_customer_cropped_plan"],
    },
    {
        "id": "M24",
        "file": "api/selection_api.py",
        "desc": "阶段 2:售前计划算炸时不再兜底 → 收款链路上那一屏跟着 500",
        "edits": [(
            "    except Exception as exc:                        # noqa: BLE001\n"
            '        logger.debug(f"[s/token] 售前交付计划跳过 quote_id={quote_id}: {exc}")\n'
            "        return None",
            "    except ValueError as exc:  # MUTATION M24:只兜住一种,别的往上抛\n"
            '        logger.debug(f"[s/token] 售前交付计划跳过 quote_id={quote_id}: {exc}")\n'
            "        return None",
        )],
        "expect_red": ["test_selection_page_survives_a_broken_plan"],
    },
]


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _write(path: Path, data: bytes) -> None:
    path.write_bytes(data)


# ══════════════════════════════════════════════════════════════════════
# 🔴 中断安全(Review 2026-08-08 指出)
#
# `try/finally` 只挡得住正常异常。真正的风险是**外部把进程杀掉**:
#   · 我自己的工具链有 600s 超时,基线 + 12 条变异跑满会顶到
#   · Ctrl-C / CI 取消 / OOM
# 那时 finally 不执行,**变异后的代码就留在工作树里**,而它长得跟正常代码一样
# (只多一行 `# MUTATION Mx`)。下一个人拿到的是一棵被改过的树,
# 甚至可能被当成"实现"提交上去 —— 这是本 runner 最坏的失败模式。
#
# 三层兜底(按能挡住的范围从窄到宽):
#   1. finally        —— 正常异常
#   2. SIGINT/SIGTERM —— Ctrl-C 与常规 kill(Windows 上 SIGTERM 有限,尽力而为)
#   3. atexit         —— 解释器正常退出的兜底
# SIGKILL / taskkill /F 谁也挡不住,所以再加第 4 层:
#   4. 每次施加变异前把原文备份到 .mutation_backup/,启动时若发现残留就**拒跑并提示还原**。
#      这一层不依赖进程活着 —— 它是唯一能挡住硬杀的。
# ══════════════════════════════════════════════════════════════════════

BACKUP_DIR = ROOT / ".mutation_backup_gap_plan"

# 当前已施加变异、尚未还原的文件:{绝对路径: 原始字节}
_IN_FLIGHT: dict[Path, bytes] = {}


def _rel(path: Path) -> str:
    """展示用相对路径。🔴 仓外路径(自测的 tmp 目录)不能让它抛 —— 
    还原流程里任何一步抛异常都会把"还原"本身搞砸,而那正是这套机制存在的意义。"""
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _restore_all(reason: str = "") -> None:
    for path, original in list(_IN_FLIGHT.items()):
        try:
            path.write_bytes(original)
            print(f"   ↩️  已还原 {_rel(path)}{(' · ' + reason) if reason else ''}")
        except Exception as exc:                      # noqa: BLE001
            print(f"   🔴 还原失败 {path}:{exc} —— 请手工 `git checkout -- {path}`")
        finally:
            _IN_FLIGHT.pop(path, None)
            _drop_backup(path)


def _keep_backup(path: Path, original: bytes) -> None:
    BACKUP_DIR.mkdir(exist_ok=True)
    (BACKUP_DIR / path.name).write_bytes(original)
    (BACKUP_DIR / (path.name + ".path")).write_text(_rel(path), encoding="utf-8")


def _drop_backup(path: Path) -> None:
    for suffix in ("", ".path"):
        try:
            (BACKUP_DIR / (path.name + suffix)).unlink()
        except FileNotFoundError:
            pass
    try:
        BACKUP_DIR.rmdir()
    except OSError:
        pass


def _abort_if_stale_backup() -> bool:
    """启动时检查上一轮是否被硬杀在半路。发现残留就**拒跑** —— 不自动还原。

    🔴 不自动还原的理由:残留意味着上一轮的状态不明(可能人已经手工改过了)。
       静默覆盖别人的工作树比多问一句糟得多。
    """
    if not BACKUP_DIR.exists():
        return False
    leftovers = sorted(p for p in BACKUP_DIR.iterdir() if p.suffix == ".path")
    if not leftovers:
        return False
    print("🔴 检测到上一轮变异没有正常收尾(可能被外部超时/强杀打断):")
    for meta in leftovers:
        target = meta.read_text(encoding="utf-8").strip()
        print(f"     {target}  ← 备份在 {_rel(BACKUP_DIR / meta.name[:-5])}")
    print("   工作树里可能残留变异代码。请先确认并还原(`git status` / `git checkout -- <文件>`),")
    print(f"   然后删掉 {_rel(BACKUP_DIR)} 再重跑。")
    return True


def _install_guards() -> None:
    import atexit
    import signal

    atexit.register(lambda: _restore_all("解释器退出兜底"))

    def _on_signal(signum, _frame):                   # noqa: ANN001
        print(f"\n🔴 收到信号 {signum},先还原变异再退出")
        _restore_all(f"signal {signum}")
        raise SystemExit(130)

    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is not None:
            try:
                signal.signal(sig, _on_signal)
            except (ValueError, OSError):
                pass                                   # 非主线程 / 平台不支持,已有另外三层


def _clear_pycache() -> None:
    for d in ROOT.rglob("__pycache__"):
        shutil.rmtree(d, ignore_errors=True)


def _run(test_names: list[str]) -> dict[str, bool]:
    """跑指定测试,返回 {测试名: 是否通过}。"""
    expr = " or ".join(test_names)
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", SUITE,
         "-q", "-p", "no:cacheprovider", "-k", expr, "--no-header", "-rN"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    # 逐条判定:pytest -q 不逐条打名字,所以再跑一次带 -v 拿逐条结果
    proc_v = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", SUITE,
         "-v", "-p", "no:cacheprovider", "-k", expr, "--no-header", "-rN"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    vout = (proc_v.stdout or "") + (proc_v.stderr or "")
    result: dict[str, bool] = {}
    for name in test_names:
        passed = None
        for line in vout.splitlines():
            if name in line and ("PASSED" in line or "FAILED" in line or "ERROR" in line):
                passed = "PASSED" in line
                break
        if passed is None:
            # 收集不到 = 当作没跑到,视为未转红(保守方向:宁可报"变异存活")
            print(f"      ⚠️  收集不到 {name}(-k 没匹配上?)")
            passed = True
        result[name] = passed
    if "no tests ran" in out:
        print("      ⚠️  本轮一个测试都没跑到 —— 判据不可用")
    return result


def main() -> int:
    if not os.environ.get("TEST_DATABASE_URL"):
        print("🔴 需要 TEST_DATABASE_URL(见文件头 docstring)")
        return 2

    # 🔴 第 -1 关:上一轮若被硬杀在半路,工作树里可能还留着变异代码。
    #    先拒跑 —— 在一棵被改过的树上跑变异,结论全是假的。
    if _abort_if_stale_backup():
        return 1
    _install_guards()

    print("=" * 72)
    print("第 0 关 · 基线必须全绿(基线就红的话,后面每条变异都是假阳性)")
    print("=" * 72)
    base = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest", SUITE, "-q",
         "-p", "no:cacheprovider", "--no-header"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if base.returncode != 0:
        print((base.stdout or "")[-3000:])
        print("🔴 基线不绿,拒绝继续")
        return 1
    print("✅ 基线全绿\n")

    failures = 0
    for mut in MUTATIONS:
        path = ROOT / mut["file"]
        original = _read(path)
        text = original.decode("utf-8")

        # 🔴 行尾免疫:仓库约定 LF,但 Windows 上任何一次 io.open(...,"w") 写回都会
        #    把整个文件翻成 CRLF,于是**多行锚点全部失配**,报成"实现改过了"。
        #    2026-08-08 实测踩到(M7)。这里按文件实际行尾把锚点归一,不靠人记得。
        eol = "\r\n" if "\r\n" in text else "\n"
        applied = text
        missed = []
        for old, new in mut["edits"]:
            old_n = old.replace("\n", eol)
            new_n = new.replace("\n", eol)
            if old_n not in applied:
                missed.append(old[:70])
            else:
                applied = applied.replace(old_n, new_n, 1)
        if missed:
            print(f"🔴 {mut['id']} 锚点没命中(实现改过了?),变异未施加:")
            for m in missed:
                print(f"     {m!r}")
            failures += 1
            continue

        print(f"── {mut['id']} · {mut['desc']}")
        # 🔴 先登记 + 落盘备份,再改文件。顺序不能反:反了就有个窗口期
        #    "文件已改但没人知道要还原",硬杀正好落在那里。
        _IN_FLIGHT[path] = original
        _keep_backup(path, original)
        _write(path, applied.encode("utf-8"))
        _clear_pycache()
        try:
            results = _run(mut["expect_red"])
        finally:
            _restore_all()
            _clear_pycache()

        assert _read(path) == original, f"{mut['id']} 还原后逐字节不一致"

        survivors = [n for n, passed in results.items() if passed]
        if survivors:
            print(f"   🔴 变异存活 —— 下列锁没转红:{survivors}")
            print("      先分诊:①锁弱(判据没打在这条接线上)②这个变异是空操作")
            failures += 1
        else:
            print(f"   ✅ {len(results)}/{len(results)} 条锁全部转红")

    print("\n" + "=" * 72)
    if failures:
        print(f"🔴 {failures} 条变异未被杀死 —— 判别力不足")
        return 1
    print(f"✅ {len(MUTATIONS)}/{len(MUTATIONS)} 条变异全部被杀死")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
