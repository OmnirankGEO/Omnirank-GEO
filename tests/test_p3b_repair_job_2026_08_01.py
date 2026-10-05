"""P3b · SSE 可恢复批任务(一键修复全部系统问题)的锁 —— 工单 §6 后半。

锁 1  九种事件齐全 + 每条八个字段(契约)
锁 2  真实 done/total,禁伪造百分比
锁 3  🔴 幂等:同一 finding 不重复修复、不重复调 provider
锁 4  重连:同 job_id 拿回同一个任务,events_since 严格续传
锁 5  🔴 归属:别人的 job_id 重连必须被拒
锁 6  逐篇隔离 + done 绝不超过 total("文章不存在"那条曾经数了两次)
锁 7  未登记的 kind 发不出去
锁 8  locating 只挑可 AI 修的 span,finding_id 就是既有的稳定指纹
锁 9  零扣费 + 零新建表 + 零迁移
锁 10 SSE 端点复用**既有单篇处理函数**,不复制第二份实现
锁 11-13 前端:只画服务端给的 done/total、九种 kind 标签一一对应、重连带游标

⚠️ 覆盖边界(如实交代):
  · 锁 1-8 是行为锁,直调真函数(依赖用替身注入,不起 FastAPI、不调模型);
  · 锁 9/10 是 AST 锁;锁 11-13 是前端静态结构锁(本仓前端无 vitest/jsdom)。
  · **没有**真跑一次端到端 SSE(要起 uvicorn + 真模型 + 真库)。
    "流真的能推出去"这一条由上线后人工验收覆盖,不在这里假装已验。
"""
from __future__ import annotations

import ast
import asyncio
import re
from pathlib import Path

import pytest

from services.article_repair_job import (
    JOB_EVENT_FIELDS,
    JOB_EVENT_KINDS,
    JobOwnershipError,
    attach_or_create,
    locate_repairable_findings,
    reset_registry_for_tests,
    run_repair_job,
)

ROOT = Path(__file__).resolve().parents[1]
WRITING_HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
SERVER = ROOT / "server.py"


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


# --------------------------------------------------------------- 替身工厂

def _article(quality_warning=None, title="本地装修怎么选", industry="建材家居"):
    return {"title": title, "industry": industry, "quality_warning": quality_warning or {}}


def _qw_with_hard(matched: str = "行业第一", count: int = 1):
    return {
        "evidence_legal": {
            "hard": [
                {"code": "absolute_first_claim", "message": "绝对化用语",
                 "matched_text": f"{matched}{i}" if count > 1 else matched}
                for i in range(count)
            ]
        }
    }


def _drive(article_ids, *, articles, repair_result=None, repair_calls=None,
           validate_result=None, load_raises=None, owner=7):
    job, _ = attach_or_create(job_id=None, article_ids=article_ids, owner_user_id=owner)

    async def load_article(article_id):
        if load_raises and article_id in load_raises:
            raise load_raises[article_id]
        return articles.get(article_id)

    async def repair_one(article_id, code, matched):
        if repair_calls is not None:
            repair_calls.append((article_id, code, matched))
        return repair_result if repair_result is not None else {"success": True}

    async def validate_article(article_id):
        return validate_result or {"article_review_status": "approved", "remaining_hard": False}

    asyncio.run(run_repair_job(
        job, load_article=load_article, repair_one=repair_one, validate_article=validate_article,
    ))
    return job


# ===========================================================================
# 锁 1 · 事件契约
# ===========================================================================
def test_lock1_event_kinds_are_exactly_the_nine_the_workorder_names():
    assert JOB_EVENT_KINDS == {
        "queued", "reviewing", "locating", "repairing", "validating",
        "partial", "completed", "failed", "heartbeat",
    }


def test_lock1b_every_emitted_event_carries_all_eight_required_fields():
    """工单 §6:每条带 job_id/article_id/finding_id/done/total/success_count/
    failed_count/message。少一个前端就要到处判 undefined。"""
    job = _drive([11], articles={11: _article(_qw_with_hard())})
    assert job.events, "一条事件都没发 —— 提取口径失效了,这条锁等于没跑"
    for event in job.events:
        missing = [f for f in JOB_EVENT_FIELDS if f not in event]
        assert not missing, f"{event['kind']} 缺字段: {missing}"
        assert "seq" in event and "kind" in event


def test_lock1c_real_run_walks_the_real_phase_order():
    """queued → reviewing → locating → repairing → validating → completed。

    阶段名必须对应**真的有那个动作**:locating 是真去聚合器定位,repairing 是真调
    修复入口。围着一个不透明的大调用摆三个阶段名就是伪造进度(工单同段刚禁过)。
    """
    job = _drive([11], articles={11: _article(_qw_with_hard())})
    kinds = [e["kind"] for e in job.events if e["article_id"] == 11]
    assert kinds[:5] == ["queued", "reviewing", "locating", "repairing", "validating"], kinds
    assert kinds[-1] == "completed"
    # 最后一条是任务级 completed(article_id 为 None)
    assert job.events[-1]["kind"] == "completed" and job.events[-1]["article_id"] is None


def test_lock1d_partial_when_some_findings_fail():
    """修不完必须出 `partial`,不许统一报 completed 骗人。"""
    job = _drive([11], articles={11: _article(_qw_with_hard())},
                 repair_result={"success": False})
    kinds = [e["kind"] for e in job.events if e["article_id"] == 11]
    assert "partial" in kinds and "completed" not in kinds


# ===========================================================================
# 锁 2 · 真实 done/total,禁伪造百分比
# ===========================================================================
def test_lock2_done_total_are_real_and_self_consistent():
    job = _drive([11, 12, 13], articles={
        11: _article(_qw_with_hard()), 12: _article(), 13: _article(_qw_with_hard()),
    })
    assert job.total == 3
    assert job.done == 3
    assert job.success_count + job.failed_count == job.done
    # total 全程不变;done 单调不减
    totals = {e["total"] for e in job.events}
    assert totals == {3}, f"total 中途变了: {totals}"
    dones = [e["done"] for e in job.events]
    assert dones == sorted(dones), "done 往回走了"
    assert dones[-1] == 3


def test_lock2b_no_forged_progress_fields_anywhere():
    """工单 §6:"用真实 done/total,禁伪造百分比"。事件里不许出现任何
    由 done/total 推出来、只为"看着在动"的字段。"""
    job = _drive([11], articles={11: _article(_qw_with_hard())})
    for event in job.events:
        for forged in ("percent", "percentage", "progress", "eta", "estimated_remaining"):
            assert forged not in event, f"{event['kind']} 出现了伪造进度字段 {forged}"


# ===========================================================================
# 锁 3 · 🔴 幂等
# ===========================================================================
def test_lock3_same_finding_is_never_repaired_twice_in_one_job():
    """同一条 finding 在一个 job 里只动一次 —— 不重复修复、不重复调 provider、
    因而也不重复消耗那条 finding 的免费额度。

    构造:同一个 (code, matched_text) 在 findings 里重复出现。
    变异「去掉 attempted_keys 去重」→ 调用次数翻倍 → 本条红。
    """
    dup = {"evidence_legal": {"hard": [
        {"code": "absolute_first_claim", "message": "绝对化用语", "matched_text": "行业第一"},
        {"code": "absolute_first_claim", "message": "绝对化用语", "matched_text": "行业第一"},
    ]}}
    calls: list = []
    _drive([11], articles={11: _article(dup)}, repair_calls=calls)
    assert len(calls) == 1, f"同一条 finding 被调了 {len(calls)} 次: {calls}"


def test_lock3b_distinct_findings_are_each_repaired_once():
    """反向面:去重不能把不同的 finding 也一起吞掉(那是把功能做没了)。"""
    two = {"evidence_legal": {"hard": [
        {"code": "absolute_first_claim", "message": "m", "matched_text": "行业第一"},
        {"code": "absolute_first_claim", "message": "m", "matched_text": "本地最好"},
    ]}}
    calls: list = []
    _drive([11], articles={11: _article(two)}, repair_calls=calls)
    assert len(calls) == 2, f"不同 finding 被误当成同一条: {calls}"


# ===========================================================================
# 锁 4 · 重连
# ===========================================================================
def test_lock4_same_job_id_reattaches_to_the_same_job():
    job, created = attach_or_create(job_id=None, article_ids=[1, 2], owner_user_id=7)
    assert created is True
    again, created2 = attach_or_create(job_id=job.job_id, article_ids=[1, 2], owner_user_id=7)
    assert created2 is False
    assert again is job, "重连拿到的不是同一个任务对象 —— 事件流接不回去"


def test_lock4b_events_since_is_a_strict_cursor():
    """回放用 seq 游标而不是时间戳:时间戳会撞(同毫秒两条),seq 单调唯一。"""
    job = _drive([11, 12], articles={11: _article(_qw_with_hard()), 12: _article()})
    assert job.events_since(0) == job.events
    cut = job.events[2]["seq"]
    tail = job.events_since(cut)
    assert all(e["seq"] > cut for e in tail)
    assert len(tail) == len(job.events) - 3
    seqs = [e["seq"] for e in job.events]
    assert seqs == list(range(1, len(job.events) + 1)), "seq 不是从 1 开始的连续单调序列"


# ===========================================================================
# 锁 5 · 🔴 归属
# ===========================================================================
def test_lock5_reconnecting_with_someone_elses_job_id_is_refused():
    """job_id 是可猜的串。拿到别人的 job_id 就能看别人文章的修复流 ——
    message 里带标题和命中的违规表述。所以重连必须核 owner。"""
    job, _ = attach_or_create(job_id=None, article_ids=[1], owner_user_id=7)
    with pytest.raises(JobOwnershipError):
        attach_or_create(job_id=job.job_id, article_ids=[1], owner_user_id=8)
    # 反向面:本人重连正常(否则"全拒"也能让上面那条绿)
    same, created = attach_or_create(job_id=job.job_id, article_ids=[1], owner_user_id=7)
    assert created is False and same is job


# ===========================================================================
# 锁 6 · 逐篇隔离 + done 不超 total
# ===========================================================================
def test_lock6_one_article_failing_does_not_stop_the_rest():
    job = _drive([11, 12, 13],
                 articles={11: _article(_qw_with_hard()), 13: _article(_qw_with_hard())},
                 load_raises={12: RuntimeError("boom")})
    assert job.total == 3 and job.done == 3
    assert job.failed_count == 1 and job.success_count == 2
    assert job.events[-1]["kind"] == "completed"


def test_lock6b_missing_article_is_counted_exactly_once():
    """🔴 曾经的真 bug:"文章不存在"分支里手动 +1,`continue` 又走了 finally 再 +1
    → done 变成 2 而 total 只有 1,进度条冲过头。

    判据:done 恰好等于 total,且 failed_count 恰好 1。
    """
    job = _drive([11], articles={})     # 11 不存在 → load_article 返回 None
    assert job.total == 1
    assert job.done == 1, f"done={job.done},被数了不止一次"
    assert job.failed_count == 1 and job.success_count == 0
    kinds = [e["kind"] for e in job.events if e["article_id"] == 11]
    assert "failed" in kinds


# ===========================================================================
# 锁 7 · 未登记的 kind 发不出去
# ===========================================================================
def test_lock7_unregistered_event_kind_is_rejected():
    """前端按 kind 分支。悄悄多出一种它不认识的,表现是"卡住不动"。"""
    job, _ = attach_or_create(job_id=None, article_ids=[1], owner_user_id=7)
    with pytest.raises(ValueError):
        job.emit("almost_done", message="编的")
    job.emit("queued", message="正常的")     # 反向面:登记过的照常


# ===========================================================================
# 锁 8 · locating 的口径
# ===========================================================================
def test_lock8_locating_skips_spans_that_ai_must_not_repair():
    """医疗/法律/金融高风险题材:`ai_repairable=False`。端点也会拒,
    但让它先在 locating 阶段消失,用户才不会看到一排必然失败的 repairing。"""
    qw = _qw_with_hard()
    normal = locate_repairable_findings(qw, industry="建材家居", title="本地装修怎么选")
    high_risk = locate_repairable_findings(qw, industry="医疗健康", title="某癌症治疗方案怎么选")
    assert normal, "普通题材应当定位到可修项 —— 一个都没有说明口径失效了"
    assert not high_risk, "高风险题材不该给出 AI 可修项"


def test_lock8b_finding_id_is_the_existing_stable_fingerprint():
    """幂等键复用既有 `finding_fingerprint`(code + 命中串,刻意不含字符偏移 ——
    偏移每修一次就漂,含了它额度会被"改一处就重置"绕开)。自造一套 = 与
    span_repair_quota 里的键对不上,库级幂等当场失效。"""
    from services.span_level_repair import finding_fingerprint

    located = locate_repairable_findings(_qw_with_hard(), industry="建材家居")
    assert located
    for item in located:
        assert item["finding_id"] == finding_fingerprint(item["code"], item["matched_text"])


# ===========================================================================
# 锁 9 · 零扣费 / 零新建表 / 零迁移
# ===========================================================================
BILLING_CALL_NAMES = {
    "deduct_points", "deduct_points_with_preference", "charge_on_success",
    "freeze_points", "commit_freeze", "consume_points", "charge_points",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _called_names(node: ast.AST) -> set:
    names = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def _endpoint_ast(func_name: str):
    tree = ast.parse(_read(SERVER))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
            return node
    raise AssertionError(f"server.py 里找不到函数 {func_name}")


def test_lock9_repair_job_never_calls_any_billing_function():
    leaked = _called_names(ast.parse(_read(ROOT / "services" / "article_repair_job.py"))) & BILLING_CALL_NAMES
    assert not leaked, f"article_repair_job 调用了扣费函数: {sorted(leaked)}"
    leaked = _called_names(_endpoint_ast("api_batch_repair_stream")) & BILLING_CALL_NAMES
    assert not leaked, f"SSE 端点调用了扣费函数: {sorted(leaked)}"


def test_lock9b_p3b_adds_no_table_and_no_migration():
    """零新建表 / 零迁移。

    判据两条:
      · 服务模块里不存在 CREATE TABLE / ALTER TABLE(AST 只认调用节点,建表是
        字符串 SQL,所以这里只能扫文本 —— 扫的是**本包自己新增的那个文件**,
        范围小到不会被别处的同名串喂假);
      · migration_manifest 里没有 2026-08 的新条目。
    """
    src = _read(ROOT / "services" / "article_repair_job.py").upper()
    for ddl in ("CREATE TABLE", "ALTER TABLE", "DROP TABLE"):
        assert ddl not in src, f"P3b 的服务模块里出现了 DDL: {ddl}"
    manifest = _read(ROOT / "db" / "migration_manifest.py")
    assert "2026_08" not in manifest, "P3b 往 migration_manifest 里加了条目 —— 本包应当零迁移"


def test_lock10_sse_endpoint_reuses_the_existing_single_finding_handler():
    """🔴 复用既有单篇处理函数,不复制第二份实现。

    复制第二份 = 额度闸 / 高风险排除 / 鉴权 会各有一套,慢慢漂开,
    最后一定演化成"批量比单篇宽松"。判据用 AST:端点函数体里真的调了它。
    """
    called = _called_names(_endpoint_ast("api_batch_repair_stream"))
    assert "api_repair_article_finding" in called, "SSE 端点没有复用既有的单篇修复处理函数"
    assert "refresh_article_review" in called, "SSE 端点没有复用既有的机审刷新"


# ===========================================================================
# 锁 11-13 · 前端(静态结构锁)
# ===========================================================================
def _strip_ts_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"^\s*//.*$", "", text, flags=re.M)
    return text


def _batch_panel_block(source: str) -> str:
    start = source.find("function BatchReviewPanel(props: {")
    assert start >= 0
    end = source.find("// [P3a] 批量面板到此为止", start)
    assert end > start
    return source[start:end]


def test_lock11_frontend_draws_only_the_server_supplied_counts():
    """前端只画服务端给的 done/total,**不插值、不换算百分比**。

    判据:进度块读的是事件字段;面板里不出现 `* 100`(百分比换算)这类写法。
    """
    panel = _strip_ts_comments(_batch_panel_block(_read(WRITING_HALL)))
    assert 'data-testid="repair-stream-progress"' in panel
    progress = panel[panel.index('data-testid="repair-stream-progress"'):]
    progress = progress[:progress.index("</div>")]
    assert "repairProgress.done" in progress and "repairProgress.total" in progress
    assert "* 100" not in panel and "*100" not in panel, "面板里出现了百分比换算"


def test_lock12_every_event_kind_has_a_human_label():
    """九种 kind 一一对应人话标签。少一种就会显示 "当前:undefined"。"""
    source = _read(WRITING_HALL)
    block = source[source.index("const REPAIR_STAGE_LABELS"):source.index("interface BatchRunSummary")]
    for kind in sorted(JOB_EVENT_KINDS):
        assert f"{kind}:" in block, f"缺少 kind 的标签: {kind}"


def test_lock13_reconnect_sends_both_job_id_and_cursor():
    """重连必须同时带 job_id 与 last_event_id;只带 job_id 会把整段事件重放一遍。"""
    panel = _strip_ts_comments(_batch_panel_block(_read(WRITING_HALL)))
    body = panel[panel.index("batch-repair-stream"):]
    body = body[:body.index("});")]
    assert "job_id:" in body and "last_event_id:" in body
    assert 'data-testid="batch-repair-resume"' in panel, "没有给出「继续上次修复」的入口"
