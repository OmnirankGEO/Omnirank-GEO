"""判别测试 · 项3 ``list_recent_conversations`` 对所有人恒返空(WO-ACCEPTANCE-3FIX 2026-08-05)。

坐实的事故(2026-08-05 生产只读 dry-run,不是推断):

    旧 SQL 逐字跑在生产 schema 上 → ``ERROR: column c.session_id does not exist``
    ``advisor_conversations`` 真实列 = id / conversation_id / advisor_id / title /
    message_count / created_at / updated_at / owner_user_id(共 8 列,没有 session_id)
    新 SQL 同一条件跑 → **10 行**(admin 口径),非 admin(112)口径 → 0 行(存量 owner 全 NULL)

    UndefinedColumn 被外层 ``except Exception`` 吞成 ``{"success": True, "conversations": []}``
    → 对所有人恒返空,admin 也一样。上一版与本版各 1 处,五包列车既没引入也没修。

🔴 本单最值钱的不是那个列名,是那个吞异常的 except:
   上一版作者已经看出它危险(把 401 守卫挪到 try 外),但**同一个 except 的另一半仍在吞** ——
   鉴权拒绝测得出来了,查询失败还是伪装成"成功返回空"。

命名 ``*__must_hit`` / ``*__must_not_hit``。变异见 ``tests/mutation_acc3fix_advisor_recent.py``。
"""
from __future__ import annotations



import api.advisor_api as mod

# ---------------------------------------------------------------------------
# 生产实证快照(2026-08-05 现取 · information_schema.columns)。
# 用它做"列名是否真实存在"的判据,比只黑名单一个 session_id 强:
# 任何**新写错的**列名都会转红,而不是等下一次事故再来加一条黑名单。
# 前提变了(表加列)→ 本快照要同步更新,那正是提醒人"改了 schema 记得看这里"。
# ---------------------------------------------------------------------------
ADVISOR_CONVERSATIONS_COLUMNS = frozenset({
    "id", "conversation_id", "advisor_id", "title",
    "message_count", "created_at", "updated_at", "owner_user_id",
})


# ===========================================================================
# 夹具:假连接 —— 记录执行过的 SQL 与参数,按需抛错或回放行
# ===========================================================================
class FakeCursor:
    def __init__(self, rows, raise_on_select=None):
        self._rows = rows
        self._raise = raise_on_select
        self.executed: list[tuple[str, tuple]] = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        # DDL(ALTER/CREATE INDEX)照常放行,只让业务 SELECT 炸
        if self._raise is not None and sql.strip().upper().startswith("SELECT"):
            raise self._raise

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeConn:
    def __init__(self, rows=(), raise_on_select=None):
        self._cursor = FakeCursor(list(rows), raise_on_select)
        self.closed = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        self.closed += 1


def test_column_checker_would_catch_session_id__must_not_hit():
    """成对反向:上面那条不是恒真 —— 事故里那个列名确实不在真实列里。"""
    assert "session_id" not in ADVISOR_CONVERSATIONS_COLUMNS


# ===========================================================================
# E. 同文件其余 except 的盘点结论(工单 §3.3 第 3 条)
#    —— 结论写进交付说明,这里只钉住"没有第二处把失败伪装成成功返回空"。
# ===========================================================================
def test_no_other_handler_returns_success_true_on_failure__must_hit():
    """全文件扫:``except Exception`` 块里不许再出现 ``"success": True``。

    盘点结论(修复后全文件 **29** 个 ``except Exception``,AST 分类实测,不是估的)
    见交付说明 §项3-盘点:
      · RAISE 6 处 —— 正确(含本次改的这一处)
      · PASS 11 处 —— 全是 ``conn.close()`` / 可选字段读取的收尾
      · LOG_ONLY 5 处 —— 只记日志不改返回值
      · RETURN_DEGRADED 5 + RETURN_DICT 1 —— 内部 helper 的降级返回
        (``False`` / ``None`` / ``""`` / ``{}``),不是端点返回体
      · RETURN_SUCCESS_FALSE 1 处(chat_with_advisor)—— 正确
      · **RETURN_SUCCESS_TRUE 0 处** —— 本次修完后一处不剩,就是这条断言钉的
    """
    import pathlib

    src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
    offenders = _handlers_returning_success_true(src)
    assert not offenders, f"仍有 except 把失败伪装成成功返回:行 {offenders}"


def _handlers_returning_success_true(src: str) -> list[int]:
    """``except Exception`` 块里 ``return {"success": True, ...}`` 的行号。

    🔴 用 AST 判 ``Return -> Dict -> key/value``,**不做源码串匹配**:
       第一版写成 ``'"success": True' in body`` 时,被本次修复自己写的
       那句注释(引用了事故原文)命中 → 假红。判据打在源码串上就会这样。
    """
    import ast

    out: list[int] = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not (isinstance(node.type, ast.Name) and node.type.id == "Exception"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict)):
                continue
            for key, val in zip(sub.value.keys, sub.value.values):
                if (isinstance(key, ast.Constant) and key.value == "success"
                        and isinstance(val, ast.Constant) and val.value is True):
                    out.append(sub.lineno)
    return out


def test_scanner_would_have_caught_the_original_bug__must_not_hit():
    """成对反向:上面那条扫描器对**事故原文**必须报警(否则它恒绿)。"""
    buggy = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        '        return {"success": True, "conversations": []}\n'
    )
    assert _handlers_returning_success_true(buggy) == [5]


def test_scanner_does_not_flag_success_false__must_not_hit():
    """第二条反向:返回 success=False 的失败处理是**对的**,不许误报。

    ``chat_with_advisor`` 那处就是这个形态。
    """
    ok = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        '        return {"success": False, "error": str(e)}\n'
    )
    assert _handlers_returning_success_true(ok) == []
