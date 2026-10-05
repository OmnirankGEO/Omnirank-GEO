"""标题生成静默失败 · 判别锁(T5 · 2026-07-28 P0)

工单:docs/AI-CONTEXT/WORKORDER_ATTRIBUTION_FIX_2026-07-28.md §5.5

事故:QZQZ(quote 386)写 12 篇 → `topics` 表从未有过这批行,整批无痕蒸发。
本组锁钉死"先落库再生成 + 失败就地留痕",并结构性钉死调用顺序。

变异验证(每条都实跑过,详见交付说明):
  T5-M1 去掉 reserve_title_slots(异常时不落库)        → 锁 3 转红
  T5-M2 except 里不调 fail_title_reservations(不更新状态)→ 锁 4 转红
  T5-M3 把 reserve 挪到 generate() 之后(先生成再落库)  → 锁 5 转红
  T5-M4 失败标记漏写 generation_error_code             → 锁 2 转红
  T5-M5 失败标记连别人的 topic 一起改                   → 锁 6 转红
  T5-M6 清理凭据时把带标题的正式行也删                  → 锁 7 转红
  T5-M7 模块里引入扣费/退费调用                         → 锁 8 转红
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from services import topic_generation_reservation as tgr

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server.py"


# ============================================================
# 假连接:记录真实执行的 SQL 与绑定参数
# ============================================================

class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        self.store["calls"].append((text, params))
        self._result = []
        self.rowcount = 0

        if text.startswith("SELECT id FROM topics") and "generation_request_id" in text:
            quote_id, request_id = int(params[0]), str(params[1])
            rows = [
                r for r in self.store["topics"]
                if r["quote_id"] == quote_id and r["generation_request_id"] == request_id
            ]
            self._result = [{"id": r["id"]} for r in rows]
            return

        if text.startswith("INSERT INTO topics"):
            self.store["seq"] += 1
            row = {
                "id": self.store["seq"],
                "keyword_id": params[0],
                "quote_id": int(params[1]),
                "original_keyword": params[2],
                "optimized_title": None,
                "status": "pending",
                "generation_request_id": str(params[3]),
                "generation_operation": params[4],
                "article_id": None,
                "generation_error_code": None,
                "generation_error_message": None,
                "generation_retryable": None,
                "generation_failure_phase": None,
                "fail_reason": None,
            }
            self.store["topics"].append(row)
            self._result = [{"id": row["id"]}]
            return

        if text.startswith("UPDATE topics SET"):
            # 🔴 按**语句里真实写着的**列与谓词执行,不在这里重写一份语义。
            #    否则改 SQL 的变异打不红锁(fake 用自己那套硬编码照样过)。
            set_part, where_part = text.split(" WHERE ", 1)
            set_part = set_part.split("SET", 1)[1]
            rest = list(params)
            assigns = {}
            for col, val in re.findall(r"(\w+)\s*=\s*(%s|NULL|'[^']*')", set_part):
                if val == "%s":
                    assigns[col] = rest.pop(0)
                elif val == "NULL":
                    assigns[col] = None
                else:
                    assigns[col] = val.strip("'")
            preds = self._predicates(where_part, rest)
            hit = 0
            for row in self.store["topics"]:
                if self._matches(row, preds):
                    row.update(assigns)
                    hit += 1
            self.rowcount = hit
            return

        if text.startswith("DELETE FROM topics"):
            where_part = text.split(" WHERE ", 1)[1]
            preds = self._predicates(where_part, list(params))
            keep, removed = [], 0
            for row in self.store["topics"]:
                if self._matches(row, preds):
                    removed += 1
                    continue
                keep.append(row)
            self.store["topics"][:] = keep
            self.rowcount = removed
            return

        raise AssertionError(f"假 cursor 收到未预期的 SQL: {text[:200]}")

    @staticmethod
    def _predicates(where_sql, rest):
        """把 WHERE 子句解析成 (column, op, value) 列表。

        只认本模块真实用到的四种形态:col=%s / col='字面量' / col IS NULL /
        col IS NOT NULL。多一个谓词就多一层过滤,少一个就少一层 —— 变异改了
        WHERE,这里立刻跟着变松/变紧,锁才有意义。
        """
        preds = []
        for token in re.split(r"\s+AND\s+", where_sql.strip().rstrip('"')):
            token = token.strip()
            m = re.fullmatch(r"(\w+)\s*=\s*%s", token)
            if m:
                preds.append((m.group(1), "eq", rest.pop(0)))
                continue
            m = re.fullmatch(r"(\w+)\s*<>\s*%s", token)
            if m:
                preds.append((m.group(1), "ne", rest.pop(0)))
                continue
            m = re.fullmatch(r"(\w+)\s*=\s*'([^']*)'", token)
            if m:
                preds.append((m.group(1), "eq", m.group(2)))
                continue
            m = re.fullmatch(r"(\w+)\s+IS\s+NULL", token)
            if m:
                preds.append((m.group(1), "isnull", None))
                continue
            m = re.fullmatch(r"(\w+)\s+IS\s+NOT\s+NULL", token)
            if m:
                preds.append((m.group(1), "notnull", None))
                continue
            raise AssertionError(f"假 cursor 无法解析谓词: {token!r}")
        return preds

    @staticmethod
    def _matches(row, preds):
        for col, op, value in preds:
            actual = row.get(col)
            if op == "eq":
                if isinstance(value, int) and not isinstance(actual, int):
                    return False
                if str(actual) != str(value):
                    return False
            elif op == "ne":
                if str(actual) == str(value):
                    return False
            elif op == "isnull" and actual is not None:
                return False
            elif op == "notnull" and actual is None:
                return False
        return True

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result)


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.committed = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.committed += 1

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def store(monkeypatch):
    """喂一个只暴露 get_connection 的 db.diagnosis_db 替身。

    真模块在 import 期就跑 init_db() 建全库,本机没有 PG 会直接连不上;
    而受理凭据模块对它的全部依赖就是 get_connection 这一个符号。
    替身不改写任何 SQL —— 下面的假 cursor 收到的是模块里逐字的语句。
    """
    import sys
    import types

    data = {"topics": [], "seq": 6000, "calls": []}
    stub = types.ModuleType("db.diagnosis_db")
    stub.get_connection = lambda: FakeConn(data)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "db.diagnosis_db", stub)
    return data


KEYWORDS = [
    {"id": 901, "keyword": "净水器十大品牌"},
    {"id": 902, "keyword": "商用净水设备"},
]


# ============================================================
# 锁 1-2 · 先落库 · 失败就地留痕(错误码 + 失败阶段非空)
# ============================================================

def test_lock1_reserve_creates_pending_rows_before_generation(store):
    ids = tgr.reserve_title_slots(386, KEYWORDS, "titles-abc12345")
    assert len(ids) == 2
    rows = store["topics"]
    assert [r["status"] for r in rows] == ["pending", "pending"]
    assert [r["quote_id"] for r in rows] == [386, 386]
    assert [r["original_keyword"] for r in rows] == ["净水器十大品牌", "商用净水设备"]
    assert all(r["generation_request_id"] == "titles-abc12345" for r in rows)
    assert all(r["generation_operation"] == tgr.TITLE_RESERVATION_OPERATION for r in rows)
    # 凭据不是成品:标题必须为空,不能被当交付计数
    assert all(r["optimized_title"] is None for r in rows)


def test_lock2_failure_marks_rows_failed_with_error_code(store):
    """🔴 核心锁:标题生成抛异常 → topics 行**存在**且为 failed、错误码非空。

    变异 T5-M4(漏写 generation_error_code)→ 本锁转红。
    """
    tgr.reserve_title_slots(386, KEYWORDS, "titles-abc12345")

    failure = tgr.failure_from_exception(
        RuntimeError("provider blew up"), phase=tgr.TITLE_PHASE
    )
    changed = tgr.fail_title_reservations(
        386, "titles-abc12345",
        error_code=failure.code, error_message=failure.message,
        phase=failure.phase, retryable=failure.retryable,
    )
    assert changed == 2
    rows = store["topics"]
    assert len(rows) == 2, "整批不许蒸发:失败后行必须还在"
    for row in rows:
        assert row["status"] == "failed"
        assert row["generation_error_code"], "错误码不得为空"
        assert row["generation_failure_phase"] == tgr.TITLE_PHASE
        assert row["generation_error_message"]
        assert row["fail_reason"]


def test_lock2b_failure_phase_is_pinned_to_title_stage(store):
    """标题阶段失败不得被分类器改写成正文阶段(否则前端显示成"正文保存失败")。"""
    from writing.article_generation_failure import ArticleSaveFailed

    failure = tgr.failure_from_exception(ArticleSaveFailed(), phase=tgr.TITLE_PERSIST_PHASE)
    assert failure.phase == tgr.TITLE_PERSIST_PHASE
    assert failure.phase.startswith("title_"), "前端按 title_ 前缀区分重试入口"


def test_lock2c_reserve_is_idempotent_per_request(store):
    first = tgr.reserve_title_slots(386, KEYWORDS, "titles-abc12345")
    second = tgr.reserve_title_slots(386, KEYWORDS, "titles-abc12345")
    assert first == second
    assert len(store["topics"]) == 2, "同一 request 重放不得重复落行"


# ============================================================
# 锁 3-5 · 端点结构:先落库 → 再生成 → 失败必标记
# ============================================================

def _generate_titles_fn():
    tree = ast.parse(SERVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "api_generate_titles":
            return node
    raise AssertionError("server.py 里找不到 api_generate_titles")


def _call_lines(node, name):
    lines = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            fn = child.func
            got = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
            if got == name:
                lines.append(child.lineno)
    return sorted(lines)


def test_lock3_endpoint_reserves_before_llm(store):
    """🔴 先落库再生成:reserve 的调用行必须早于 generator.generate()。

    变异 T5-M1(删掉 reserve)/ T5-M3(挪到 generate 之后)→ 本锁转红。
    """
    fn = _generate_titles_fn()
    reserve_lines = _call_lines(fn, "reserve_title_slots")
    generate_lines = _call_lines(fn, "generate")
    assert reserve_lines, "端点必须调用 reserve_title_slots"
    assert generate_lines, "端点必须调用 generator.generate()"
    assert min(reserve_lines) < min(generate_lines), (
        f"受理凭据必须落在 LLM 之前:reserve@{reserve_lines} vs generate@{generate_lines}"
    )


def test_lock4_endpoint_marks_failure_in_except(store):
    """🔴 异常路径必须更新状态:except Exception 分支内要有 fail_title_reservations。

    变异 T5-M2(except 里不调)→ 本锁转红。
    """
    fn = _generate_titles_fn()
    handlers = [n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)]
    generic = [
        h for h in handlers
        if h.type is None or (isinstance(h.type, ast.Name) and h.type.id == "Exception")
    ]
    assert generic, "端点必须有兜底 except"
    marked = any(_call_lines(h, "fail_title_reservations") for h in generic)
    assert marked, "兜底 except 必须把已受理的选题标记为 failed"


def test_lock5_every_early_return_path_is_covered(store):
    """LLM 零产出 / 写库失败 两条早退路径也必须留痕,不能只有 except 覆盖。"""
    fn = _generate_titles_fn()
    fail_calls = _call_lines(fn, "fail_title_reservations")
    # 零产出、写库失败、被拒、兜底异常 —— 四条
    assert len(fail_calls) >= 4, (
        f"早退路径覆盖不全,fail_title_reservations 仅出现 {len(fail_calls)} 次"
    )
    clear_calls = _call_lines(fn, "clear_title_reservations")
    assert clear_calls, "成功路径必须清理残留空壳凭据"


# ============================================================
# 锁 6-7 · 只动自己的行
# ============================================================

def test_lock6_failure_marking_never_touches_other_rows(store):
    """标记失败只动本 request 的 pending 行;别人的 topic 一个字节都不许改。

    变异 T5-M5 → 本锁转红。
    """
    tgr.reserve_title_slots(386, KEYWORDS, "titles-mine0001")
    tgr.reserve_title_slots(386, [{"id": 999, "keyword": "别人的词"}], "titles-other001")
    # 一条已成稿的行(不同 request)
    store["topics"].append({
        "id": 7777, "keyword_id": 500, "quote_id": 386,
        "original_keyword": "已完成", "optimized_title": "已有标题",
        "status": "pending", "generation_request_id": "titles-mine0001",
        "generation_operation": tgr.TITLE_RESERVATION_OPERATION, "article_id": 4242,
        "generation_error_code": None, "generation_error_message": None,
        "generation_retryable": None, "generation_failure_phase": None,
        "fail_reason": None,
    })

    changed = tgr.fail_title_reservations(
        386, "titles-mine0001", error_code="TITLE_GENERATION_FAILED",
        error_message="boom", phase=tgr.TITLE_PHASE,
    )
    assert changed == 2, "只应标记本 request 的两条凭据"
    by_id = {r["id"]: r for r in store["topics"]}
    other = [r for r in store["topics"] if r["generation_request_id"] == "titles-other001"]
    assert all(r["status"] == "pending" for r in other), "别的请求的行不得被改"
    assert by_id[7777]["status"] == "pending", "已挂成稿(article_id)的行不得被改"


def test_lock7_cleanup_only_removes_empty_shells(store):
    """清理只删"无标题无成稿"的空壳凭据;带标题的正式行必须留下。

    变异 T5-M6 → 本锁转红。
    """
    tgr.reserve_title_slots(386, KEYWORDS, "titles-clean001")
    store["topics"].append({
        "id": 8888, "keyword_id": 903, "quote_id": 386,
        "original_keyword": "正式行", "optimized_title": "真标题",
        "status": "pending", "generation_request_id": "titles-clean001",
        "generation_operation": tgr.TITLE_RESERVATION_OPERATION, "article_id": None,
        "generation_error_code": None, "generation_error_message": None,
        "generation_retryable": None, "generation_failure_phase": None,
        "fail_reason": None,
    })
    # 上一次失败留下的空壳(别的 request)· 本次成功应把它顶掉
    store["topics"].append({
        "id": 8889, "keyword_id": 904, "quote_id": 386,
        "original_keyword": "上次失败的", "optimized_title": None,
        "status": "failed", "generation_request_id": "titles-old00001",
        "generation_operation": tgr.TITLE_RESERVATION_OPERATION, "article_id": None,
        "generation_error_code": "TITLE_OUTPUT_EMPTY", "generation_error_message": "x",
        "generation_retryable": True, "generation_failure_phase": "title_output",
        "fail_reason": "x",
    })
    # 同一 quote 上另一批**正在跑**的任务(别的 request 的 pending)· 绝不能删
    store["topics"].append({
        "id": 8890, "keyword_id": 905, "quote_id": 386,
        "original_keyword": "并发在跑的", "optimized_title": None,
        "status": "pending", "generation_request_id": "titles-concur01",
        "generation_operation": tgr.TITLE_RESERVATION_OPERATION, "article_id": None,
        "generation_error_code": None, "generation_error_message": None,
        "generation_retryable": None, "generation_failure_phase": None,
        "fail_reason": None,
    })

    removed = tgr.clear_title_reservations(386, "titles-clean001")
    assert removed == 3, "本次 2 条 pending + 上次 1 条 failed 空壳"
    remaining = sorted(r["id"] for r in store["topics"])
    assert remaining == [8888, 8890], (
        "带标题的正式行(8888)与并发批次的 pending(8890)都必须留下"
    )


# ============================================================
# 锁 8 · 三条不许碰
# ============================================================

def test_lock8_reservation_module_touches_no_billing():
    """🔴 扣费/退费口径一条不动:本模块不得引入任何计费调用。

    变异 T5-M7(引入 refund_points / deduct_points)→ 本锁转红。
    """
    tree = ast.parse(
        (ROOT / "services" / "topic_generation_reservation.py").read_text(encoding="utf-8")
    )
    forbidden = ("billing", "refund", "deduct", "charge", "points",
                 "wallet", "freeze", "agent_level", "is_admin")
    hits = []
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Call):
            fn = node.func
            names.append(fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "") or "")
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
            names.extend(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.extend(a.name for a in node.names)
        for name in names:
            if any(bad in str(name).lower() for bad in forbidden):
                hits.append(name)
    assert hits == [], f"受理凭据模块不得触碰计费/身份:{hits}"


def test_lock9_reservation_sql_only_touches_topics():
    """凭据模块的 SQL 只许碰 topics 表。"""
    tree = ast.parse(
        (ROOT / "services" / "topic_generation_reservation.py").read_text(encoding="utf-8")
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            upper = " ".join(node.value.upper().split())
            for verb in ("INSERT INTO ", "UPDATE ", "DELETE FROM "):
                if verb in upper:
                    tail = upper.split(verb, 1)[1].strip()
                    table = tail.split()[0].strip("(")
                    assert table == "TOPICS", f"凭据模块触碰了 {table} 表"


def test_lock10_frontend_separates_title_retry_from_article_retry():
    """前端必须按 title_ 阶段前缀区分重试入口:标题失败不走退款闸门。"""
    src = (ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx").read_text(
        encoding="utf-8"
    )
    assert "function isTitlePhaseFailure" in src
    assert "generation_failure_phase || ''" in src and "startsWith('title_')" in src
    assert 'data-testid="retry-title-generation"' in src
    # 标题失败分支不得被退款状态闸门挡住
    assert "!isTitlePhaseFailure(topic) && topic.generation_retryable !== false" in src


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-q"]))
