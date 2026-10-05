"""[P0 代发卡单出口] awaiting_sync 死胡同的判别测试。

不打补丁必须全红。每条对应工单验收标准里的一条：

  1. 反查命中 → 回填 mhz_order_id 并回到 submitted，随后能被 get_pending_items() 捞到；
  2. 未命中且超阈值 → awaiting_action + 七字段合同 + actions≥2 + submit_attempts 顶到上限；
  3. 未到阈值 → 保持 awaiting_sync 不动（防刚提交就被判死）；
  4. 终态不被捡起 → 出口扫描的 WHERE 只认 awaiting_sync；
  5. 退款幂等键 → 全链路只认 item:{id}，禁止 charge_tx_id / refund_request 键；
  6. **不静默退款** → 出口路径（sweep + 两个用户端点）不得出现任何退款调用。

纯静态 + 假游标，可 `pytest tests/test_mhz_awaiting_sync_exit.py --noconftest` 直接跑，
不需要 TEST_DATABASE_URL。
"""
import re
from pathlib import Path

import pytest

from services import publication_awaiting_sync_exit as exit_mod

ROOT = Path(__file__).parent.parent
DB_SRC = (ROOT / "db" / "meijiehezi_db.py").read_text(encoding="utf-8")
SCHED_SRC = (ROOT / "api" / "scheduler.py").read_text(encoding="utf-8")
API_SRC = (ROOT / "api" / "meijiehezi_api.py").read_text(encoding="utf-8")


def _func_body(src: str, name: str) -> str:
    """按缩进截出一个 def / async def 的函数体（支持嵌套在别的函数里的 def）。

    ⚠️ 必须按缩进切，不能用"下一个顶层 def"：`_mhz_awaiting_sync_resolver` 是嵌套在
    `_setup_mhz_jobs` 里的 async def，切错会把半个文件都算进函数体，
    让"这个函数里没有退款调用"这类断言变成永远为假的空断言。
    """
    lines = src.splitlines(keepends=True)
    pat = re.compile(rf"^(\s*)(?:async\s+)?def\s+{re.escape(name)}\s*\(")
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        indent = len(m.group(1))
        out = [line]
        # 签名可能跨多行（`):` 那一行缩进为 0），括号闭合前不能套用缩进判据
        depth = line.count("(") - line.count(")")
        for nxt in lines[i + 1:]:
            if depth > 0:
                out.append(nxt)
                depth += nxt.count("(") - nxt.count(")")
                continue
            if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= indent:
                break
            out.append(nxt)
        return "".join(out)
    raise AssertionError(f"源码里找不到 def {name}(")


def test_func_body_slicer_is_indentation_aware():
    """守住上面那个坑：切错了，本文件所有"没有退款调用"的断言都会变成空断言。"""
    body = _func_body(SCHED_SRC, "_mhz_awaiting_sync_resolver")
    assert "find_awaiting_sync_items_for_exit" in body, "切早了"
    assert "_mhz_call_log_cleanup" not in body, "切晚了，串到下一个 job 了"
    api_body = _func_body(API_SRC, "api_exit_report_not_published")
    assert "USER_CLAIM_NOT_PUBLISHED" in api_body
    assert "record-publish-url" not in api_body, "切晚了，串到下一个端点了"


# ---------------------------------------------------------------- 阈值判据

def test_below_threshold_keeps_item_untouched():
    """未到阈值 → 不开出口。刚提交几个小时就被判死是最贵的误伤。"""
    # 次数够但时间不够
    assert exit_mod.should_open_user_exit(probe_attempts=9, awaiting_hours=2) is False
    # 时间够但次数不够（可能只是 mhz 接口本轮抖动）
    assert exit_mod.should_open_user_exit(probe_attempts=1, awaiting_hours=200) is False
    # 判据缺失一律按"还不够"
    assert exit_mod.should_open_user_exit(probe_attempts=None, awaiting_hours=200) is False
    assert exit_mod.should_open_user_exit(probe_attempts=5, awaiting_hours=None) is False


def test_at_threshold_opens_exit():
    assert exit_mod.should_open_user_exit(probe_attempts=3, awaiting_hours=72) is True
    assert exit_mod.should_open_user_exit(probe_attempts=58, awaiting_hours=58 * 24) is True


def test_thresholds_match_ticket():
    assert exit_mod.EXIT_MIN_PROBE_ATTEMPTS == 3
    assert exit_mod.EXIT_MIN_AGE_HOURS == 72


# ---------------------------------------------------------------- §13 合同

def test_contract_has_seven_fields_and_two_actions():
    c = exit_mod.build_awaiting_sync_exit_contract(
        item_id=364, media_name="咸宁新闻网", awaiting_hours=58 * 24, probe_attempts=3,
    )
    for field in ("code", "message", "reason", "impact", "repair_hint", "actions", "rule_version"):
        assert c.get(field), f"§13 七字段缺 {field}"
    assert len(c["actions"]) >= 2, "红码必须有下一步，且工单要求 ≥2 个"


def test_contract_contains_refund_action():
    c = exit_mod.build_awaiting_sync_exit_contract(item_id=364)
    labels = [a["label"] for a in c["actions"]]
    assert "确认未发布 · 退还算力" in labels, "工单点名必须有这个动作"
    executable = [a for a in c["actions"] if a.get("type") == "api"]
    assert len(executable) >= 2, "至少两个动作必须真能调用，不能全是 nav 装饰"
    for a in executable:
        assert a["target"].startswith("/api/meijiehezi/items/364/"), a
        assert a.get("method") == "POST"


def test_contract_does_not_promise_instant_refund():
    """mhz 已回执"已接收"，稿件可能真发出去了 —— 不得承诺立即退款，也不得断言未发布。"""
    c = exit_mod.build_awaiting_sync_exit_contract(item_id=1, media_name="某媒体")
    blob = c["message"] + c["reason"] + c["impact"] + c["repair_hint"]
    assert "已退" not in blob and "立即退" not in blob
    assert "人工核实" in blob or "核实" in blob


def test_contract_actions_match_real_endpoints():
    """合同里的 target 必须真有对应路由，否则按钮点下去 404 = 又一个死胡同。"""
    c = exit_mod.build_awaiting_sync_exit_contract(item_id=7)
    for a in c["actions"]:
        if a.get("type") != "api":
            continue
        route = a["target"].replace("/api/meijiehezi", "").replace("/7/", "/{item_id}/")
        assert f'@router.post("{route}")' in API_SRC, f"合同动作 {a['id']} 没有对应端点 {route}"


# ---------------------------------------------------- 出口扫描：谁会被捡起来

def test_exit_scan_only_picks_awaiting_sync():
    """终态不被捡起：出口扫描的 WHERE 只认 awaiting_sync。"""
    body = _func_body(DB_SRC, "find_awaiting_sync_items_for_exit")
    assert "i.status = 'awaiting_sync'" in body
    for terminal in ("published", "cancelled", "withdrawn", "rejected", "failed", "success"):
        assert f"'{terminal}'" not in body, f"出口扫描不该出现终态 {terminal}（只认 awaiting_sync 即可）"


def test_exit_scan_ignores_manual_review_flag():
    """根因：既有两个扫描器都带 manual_review_required=FALSE，

    打完标记那一刻 item 从所有自动扫描里消失（线上最老一条因此滞留 58 天）。
    出口扫描若沿用同一过滤，等于新建一个死胡同，历史卡单永远进不来。
    """
    body = _func_body(DB_SRC, "find_awaiting_sync_items_for_exit")
    assert "manual_review_required = FALSE" not in body
    # 对照组：既有 12 小时扫描器确实带这个过滤（证明上面那条不是空断言）
    assert "manual_review_required = FALSE" in _func_body(DB_SRC, "find_awaiting_sync_items_overdue")


def test_exit_scan_covers_null_awaiting_since():
    """awaiting_sync_since 是后加列，早期行可能为空；

    既有两个扫描器都要求它非空，那些行对自动化完全不可见 —— 另一类永久卡死。
    """
    body = _func_body(DB_SRC, "find_awaiting_sync_items_for_exit")
    assert "COALESCE(i.awaiting_sync_since, i.created_at)" in body


# ---------------------------------------------- 自愈优先：命中就回填，不开出口

def test_sweep_relinks_before_opening_exit():
    """反查命中 → link_awaiting_sync_to_order_sn 回填 + continue，绝不继续走到出口。"""
    body = _func_body(SCHED_SRC, "_mhz_awaiting_sync_resolver")
    step3 = body[body.index("find_awaiting_sync_items_for_exit(min_age_hours"):]
    assert "link_awaiting_sync_to_order_sn(_iid, sn)" in step3
    assert step3.index("link_awaiting_sync_to_order_sn") < step3.index("open_awaiting_sync_user_exit")
    assert "continue" in step3.split("open_awaiting_sync_user_exit")[0]


def test_relink_returns_item_to_submitted_pipeline():
    """回填后必须回到 submitted —— get_pending_items() 只扫 submitted，

    落在别的状态就等于换了个地方继续卡死。
    """
    link = _func_body(DB_SRC, "link_awaiting_sync_to_order_sn")
    assert "status = 'submitted'" in link
    assert "mhz_order_id = %s" in link
    assert "WHERE i.status = 'submitted'" in _func_body(DB_SRC, "get_pending_items")


def test_sweep_counts_failed_probe_before_deciding():
    """没有计次就无法表达"失败 ≥3 次"，旧代码正是无限重试且不计次。"""
    step3 = _func_body(SCHED_SRC, "_mhz_awaiting_sync_resolver")
    step3 = step3[step3.index("find_awaiting_sync_items_for_exit(min_age_hours"):]
    assert "bump_awaiting_sync_probe_attempt(_iid)" in step3
    assert "should_open_user_exit(" in step3
    assert step3.index("bump_awaiting_sync_probe_attempt") < step3.index("should_open_user_exit")


# ------------------------------------------------------------ 挂起的落库形状

def test_open_exit_suspends_and_caps_retries():
    body = _func_body(DB_SRC, "open_awaiting_sync_user_exit")
    assert "suspend_item_for_user_action" in body, "复用既有挂起 helper，别自己写一套状态机"
    # helper 自己负责顶满 submit_attempts + 重算订单状态
    suspend = _func_body(DB_SRC, "suspend_item_for_user_action")
    assert "DEFAULT_MAX_SUBMIT_ATTEMPTS" in suspend
    assert "recompute_order_status" in suspend
    assert "ITEM_STATUS_AWAITING_ACTION" in suspend


def test_open_exit_rechecks_status_under_row_lock():
    """反查刚好命中和出口挂起会抢同一行，必须行锁内复核状态。"""
    body = _func_body(DB_SRC, "open_awaiting_sync_user_exit")
    assert "FOR UPDATE" in body
    assert 'row["status"] != "awaiting_sync"' in body


def test_open_exit_keeps_item_in_admin_queue():
    """用户和管理员任一侧都能结案，不依赖单一方出现。"""
    body = _func_body(DB_SRC, "open_awaiting_sync_user_exit")
    assert "manual_review_required = TRUE" in body


# ------------------------------------------------ 最贵的一条：绝不静默退款

_REFUND_CALLS = ("refund_for_publish_order", "refund_points", "create_refund_request",
                 "review_refund_request", "cancel_awaiting_item", "deduct_points")


@pytest.mark.parametrize("func", [
    "find_awaiting_sync_items_for_exit",
    "bump_awaiting_sync_probe_attempt",
    "open_awaiting_sync_user_exit",
    "record_awaiting_sync_user_claim",
    "list_user_pending_action_items",
])
def test_exit_path_never_moves_money(func):
    """mhz 已回执"成功提交，正在执行发布" —— 稿件可能真发出去了。

    自动退款 = 既退钱又发稿，双重损失且不可追回。整条出口路径必须零退款调用。
    """
    body = _func_body(DB_SRC, func)
    for call in _REFUND_CALLS:
        assert f"{call}(" not in body, f"{func} 出现了动钱调用 {call} —— 这是静默退款"


def test_sweep_never_moves_money():
    body = _func_body(SCHED_SRC, "_mhz_awaiting_sync_resolver")
    for call in _REFUND_CALLS:
        assert f"{call}(" not in body, f"sweep 出现了动钱调用 {call}"
    # 对照组：短视频兜底 sweep 确实会退款，证明上面不是空断言
    assert "refund_for_publish_order(" in _func_body(SCHED_SRC, "_mhz_svideo_stuck_refund_sweep")


def test_user_endpoints_never_move_money():
    for name in ("api_exit_report_not_published", "api_exit_record_publish_url"):
        body = _func_body(API_SRC, name)
        for call in _REFUND_CALLS:
            assert f"{call}(" not in body, f"{name} 出现了动钱调用 {call}"


def test_cancel_awaiting_item_is_never_reused():
    """cancel_awaiting_item 自带退款且 WHERE 只认 awaiting_confirmation ——

    拿它做状态收口要么无声无效，要么多退一次。
    """
    cancel = _func_body(DB_SRC, "cancel_awaiting_item")
    assert "status = 'awaiting_confirmation'" in cancel
    assert "refund_for_publish_order(" in cancel
    assert "cancel_awaiting_item(" not in _func_body(SCHED_SRC, "_mhz_awaiting_sync_resolver")


# ------------------------------------------------------ 退款幂等键唯一 item:{id}

def test_only_admin_path_refunds_and_uses_item_key():
    """真正动钱的仍是既有管理员出口，且幂等键必须是 item:{id}。

    另一套键（charge_tx_id / refund_request:{id}）与它互不相认 → 同一笔退两次。
    """
    body = _func_body(DB_SRC, "admin_manual_review_resolve")
    assert 'refund_key=f"item:{item_id}"' in body
    assert "charge_tx_id" not in body


def test_exit_items_are_reachable_by_admin_resolve():
    """出口把 status 改成 awaiting_action，管理员出口的 WHERE 必须仍能命中它。"""
    body = _func_body(DB_SRC, "admin_manual_review_resolve")
    assert "manual_review_required = TRUE" in body
    assert "status = 'awaiting_sync'" not in body, "若按 status 过滤，挂起后管理员就够不着了"


def _assigns_refund_request_key(src: str, func: str) -> bool:
    """函数的**代码**里(AST:不看注释、不看 docstring)有没有把 refund_key 赋成 f"refund_request:…"。"""
    import ast

    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            for n in ast.walk(node):
                if (isinstance(n, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "refund_key" for t in n.targets)
                        and isinstance(n.value, ast.JoinedStr) and n.value.values
                        and isinstance(n.value.values[0], ast.Constant)
                        and str(n.value.values[0].value).startswith("refund_request:")):
                    return True
            return False
    raise AssertionError(f"找不到函数 {func}")


def test_user_claim_does_not_route_through_refund_requests():
    """mhz_refund_requests 那条路原来在查不到 order_sn 时会回落到 refund_request:{id} 键(与 item:{id} 互不相认);

    WO_310 起那条路不再回落:查不到本平台下单条目就转人工、不批准。卡单恰恰没有 order_sn ——
    认领路径仍然不许走 mhz_refund_requests(主断言不变)。
    """
    # 对照(WO_310 起改写):原来断言源码文本里「有」refund_request:{request_id},修法落地后
    #   只剩 docstring 旧句在保绿 ⇒ 改成断言**代码**里不再有兜底键的赋值(注释 / docstring 不算)
    assert not _assigns_refund_request_key(DB_SRC, "review_refund_request"), \
        "review_refund_request 又给 refund_key 赋了 refund_request:… 兜底键(WO_310 已禁用于批准)"
    claim = _func_body(DB_SRC, "record_awaiting_sync_user_claim")
    assert "create_refund_request(" not in claim


# ------------------------------------------------------------ 出口必须可达

def test_contract_has_a_user_facing_read_surface():
    """合同只写不读 = 用户看不到任何下一步，出口等于不存在。"""
    assert "reject_contract" in _func_body(DB_SRC, "list_user_pending_action_items")
    assert '@router.get("/pending-user-actions")' in API_SRC


def test_user_claim_guards_status_and_code():
    """否则任何人都能拿这个端点把别的状态的单子改成已发布。"""
    body = _func_body(DB_SRC, "record_awaiting_sync_user_claim")
    assert "ITEM_STATUS_AWAITING_ACTION" in body
    assert "EXIT_CODE" in body
    assert "FOR UPDATE" in body


def test_record_published_requires_url():
    body = _func_body(DB_SRC, "record_awaiting_sync_user_claim")
    assert "补录已发布必须提供稿件链接" in body
    api = _func_body(API_SRC, "api_exit_record_publish_url")
    assert 'url.startswith("https://")' in api


def test_endpoints_check_ownership():
    """防枚举：不是自己的一律按不存在处理。"""
    body = _func_body(API_SRC, "_load_exit_item_for_user")
    assert 'int(row["user_id"]) != int(user["user_id"])' in body
    assert "status_code=404" in body
    for name in ("api_exit_report_not_published", "api_exit_record_publish_url"):
        assert "_load_exit_item_for_user(item_id, user)" in _func_body(API_SRC, name)


# ------------------------------------------------------------ 部署完整性

def test_migration_registered_in_manifest():
    """「写了迁移」≠「生产会跑迁移」：漏登记 manifest，出口扫描会 UndefinedColumn

    并被 job 的 except 静默吞掉 —— 表现成"任务在跑但永远不出口"，正是本次故障形态。
    """
    manifest = (ROOT / "db" / "migration_manifest.py").read_text(encoding="utf-8")
    assert '"scripts/migration_mhz_awaiting_sync_exit_2026_07_26.sql"' in manifest
    sql = (ROOT / "scripts" / "migration_mhz_awaiting_sync_exit_2026_07_26.sql").read_text(encoding="utf-8")
    assert "awaiting_sync_probe_attempts" in sql
    assert sql.count("IF NOT EXISTS") >= 4, "必须幂等可连跑"


def test_runtime_fallback_alter_exists():
    """迁移漏跑时的第二层保险 —— 两层都要有。"""
    assert "ADD COLUMN IF NOT EXISTS awaiting_sync_probe_attempts" in DB_SRC


def test_sweep_job_has_tick_claim():
    """本 job 现在会改状态到 awaiting_action 并写合同（下一步就是管理员按合同退款），

    双跑会重复计次/重复告警，必须和其他动状态的 mhz job 一样上 fail-closed claim。
    """
    assert 'sched_claim("mhz_awaiting_sync_resolver", 300)' in SCHED_SRC
