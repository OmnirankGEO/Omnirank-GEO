"""[ADVISOR-OWNERSHIP] 行为测试盖不到的结构不变量。

每条都配反向对照:判据必须能在"被改坏"的源码上转红,否则就是恒真锁。
"""
import ast
import io
import os

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ADVISOR = os.path.join(_ROOT, "api", "advisor_api.py")
_SERVER = os.path.join(_ROOT, "server.py")


def _src(path):
    return io.open(path, encoding="utf-8", newline="").read()


def _tree(path):
    return ast.parse(_src(path))


# ============ 对话写入 ============
# [开源 E3 · B3c · 2026-09-28] advisor_api 的对话端点(列表 / 读 / 改标题 / 删 / 续聊 / 聊天)全部随社媒顾问下线删除,
#   写对话的 INSERT 一并没了 ⇒ 原「每条 INSERT 都要带 owner_user_id」在 0 条 INSERT 上恒绿(没有分母)。
#   改成更强的形状:advisor_api 里**不许再有**写 advisor_conversations / advisor_messages 的 SQL;
#   谁要把对话写入加回来,必须先回来重建归属校验(旧 decide_conversation_access 那一套)。

_CONV_WRITES = ("insert into advisor_conversations", "insert into advisor_messages",
                "update advisor_conversations", "delete from advisor_conversations")


def _sql_hits(src: str, needles) -> list:
    return [n.lineno for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and any(k in n.value.lower() for k in needles)]


def test_advisor_api_no_longer_writes_conversations():
    src = _src(_ADVISOR)
    # 分母活性:在役的 GET /api/advisors 与默认写手配置仍在读 SQL —— 扫描器必须看得见它们
    assert _sql_hits(src, ("from advisors", "system_config")), "advisor_api 里一条 SQL 字面量都扫不到 —— 尺子坏了"
    assert _sql_hits(src, _CONV_WRITES) == [], "advisor_api 又开始写对话表了 —— 先重建归属校验"


def test_conversation_write_detector_has_teeth():
    """反向对照:人造一条写对话的 SQL,判据必须抓到。"""
    fake = 'x = """INSERT INTO advisor_conversations (conversation_id, advisor_id) VALUES (%s,%s)"""'
    assert _sql_hits(fake, _CONV_WRITES) == [1]


# ============ server.py 三条影子路由 ============
# [开源 E3 · B3a · 2026-09-28] 三条影子路由(及其 fail-closed 闸)已随 server.py 内联顾问端点整体删除。
# 原来的「挂了闸 / include_router 排在前面」两条锁的前提不存在了,改成更强的形状:
# server.py 里不许再有任何 /api/advisors 前缀的 @app 路由 —— 没有影子,就没有「import 失败时顶上来的零认证口」。

_SHADOW_NAMES = ("api_recent_conversations", "api_get_advisor_conversations", "api_get_conversation_messages")


def _app_route_paths(tree):
    out = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                        and getattr(d.func.value, "id", "") == "app"
                        and d.args and isinstance(d.args[0], ast.Constant)):
                    out.append((n.name, d.args[0].value))
    return out


def test_server_has_no_advisor_shadow_route():
    tree = _tree(_SERVER)
    routes = _app_route_paths(tree)
    assert routes, "server.py 一条 @app 路由都扫不到 —— 尺子坏了,不是没有影子"
    shadows = [(name, path) for name, path in routes if path.startswith("/api/advisors")]
    assert shadows == [], f"server.py 又长出了顾问路由影子:{shadows}"
    back = [n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in _SHADOW_NAMES]
    assert back == [], f"已删的影子处理函数回来了:{back}"


def test_include_router_registered_for_advisor_router():
    """顾问路由只剩 router 这一处 —— include_router 这条真调用没了,整个顾问板块就 404。

    🔴 判据必须走 AST 找**真的调用节点**,不能 `src.index("include_router(...)")`:
    把调用注释掉后字串仍在,判据恒真(第一版就是这么写的,被变异 M17 抓了个正着)。
    """
    tree = _tree(_SERVER)
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", "") == "include_router"
             and any(getattr(a, "id", "") == "advisor_router" for a in n.args)]
    assert calls, "server.py 里找不到真正的 app.include_router(advisor_router) 调用"
