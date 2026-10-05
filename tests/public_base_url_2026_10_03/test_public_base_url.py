"""WO_331 · 站点对外地址收成一个出处:services.owned_image_policy.public_base_origin()。

病:server.py 五处把诊断报告分享链接写死成 http://127.0.0.1:8001,不读任何配置 —— 开源版自建后分享链接跳到我们的生产站。
修:五处 + 图片绝对地址 + 注册短链 + 两家支付回调缺省值都走唯一出处(读 PUBLIC_BASE_URL;没设 / 空串 ⇒ 仍是 127.0.0.1:8001)。

锁 1 线上等价:PUBLIC_BASE_URL 不设 ⇒ server.py 五处生成的字符串与改前逐字节相同。
      取的是 server.py 里**那五个 f-string 的 AST 节点本身**,代入固定 id / token 求值 —— 量的是真代码,不是另写一份。
锁 2 配了就用:设成 https://example.test/ ⇒ 五处 / 图片 / 注册短链 / 支付缺省都用 https://example.test;
      支付变量显式设了仍用显式值;PUBLIC_BASE_URL 或支付变量是空串(dotenv 会把 KEY= 注入成空串)⇒ 走缺省。
锁 3 字面量普查:运行时 Python(不含 tests/ scripts/ 与只读历史证据 agent-test-artifacts*/;注释、docstring 不算)
      里 127.0.0.1:8001 字面量**恰好 1 处**,就在唯一出处。🔴 认 f-string:3.12 的 tokenize 把 f-string 拆成
      FSTRING_START / FSTRING_MIDDLE / FSTRING_END,只数 STRING 会漏掉 server.py 那五处。
      牙证(往 server.py 文本塞回一处 f-string ⇒ 必红)与对照臂(只加一行带域名的注释 ⇒ 必绿)走同一个普查函数。
"""
from __future__ import annotations

import ast
import functools
import importlib
import io
import subprocess
import sys
import tokenize
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DOMAIN = "127.0.0.1:8001"
ORIGIN_FN = "public_base_origin"
FIXED = {"diagnosis_id": 4242, "id": 4242, "tk": "TOKabc", "token": "TOKabc", "code": "CODE99"}
#: 改前(main 153d2b686)五处在上面固定值下生成的完整字符串
EXPECTED_DEFAULT = sorted([
    "http://127.0.0.1:8001/public/report/4242",
    "http://127.0.0.1:8001/public/report/4242?st=TOKabc",
    "http://127.0.0.1:8001/api/sl/TOKabc",
    "http://127.0.0.1:8001/public/report/4242",
    "http://127.0.0.1:8001/api/sl/CODE99",
])


# ───────────────────────────────────────────────────────────── 锁 1 / 2:server.py 五个 f-string
def _origin_fstrings() -> list[ast.JoinedStr]:
    """server.py 里以「唯一出处」开头的 f-string:首段是 public_base_origin() 或绑定了它的 _origin。"""
    tree = ast.parse((REPO / "server.py").read_text(encoding="utf-8"))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.JoinedStr) and n.values and isinstance(n.values[0], ast.FormattedValue):
            v = n.values[0].value
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == ORIGIN_FN) or \
               (isinstance(v, ast.Name) and v.id == "_origin"):
                out.append(n)
    return out


def _render_all(origin_value: str) -> list[str]:
    ns = dict(FIXED, _origin=origin_value, **{ORIGIN_FN: lambda: origin_value})
    return sorted(eval(compile(ast.Expression(body=n), "server.py", "eval"), ns) for n in _origin_fstrings())


def _origin_now() -> str:
    from services.owned_image_policy import public_base_origin
    return public_base_origin()


def test_lock1_unset_is_byte_identical_to_before(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert len(_origin_fstrings()) == 5, "分母自证:server.py 里走唯一出处的 f-string 应恰好 5 处"
    assert _origin_now() == "http://127.0.0.1:8001"
    assert _render_all(_origin_now()) == EXPECTED_DEFAULT


@pytest.mark.parametrize("env,expect", [
    ("https://example.test/", "https://example.test"),
    ("  https://example.test/  ", "https://example.test"),
    ("", "http://127.0.0.1:8001"),          # 空串当没设
])
def test_lock2_configured_origin_is_used(monkeypatch, env, expect):
    monkeypatch.setenv("PUBLIC_BASE_URL", env)
    assert _origin_now() == expect
    assert _render_all(_origin_now()) == sorted(s.replace("http://127.0.0.1:8001", expect) for s in EXPECTED_DEFAULT)
    from services.image_placeholder import _public_base_url
    assert _public_base_url() == expect


def test_lock2_agent_workbench_register_link_uses_single_source():
    src = (REPO / "api" / "agent_workbench_api.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "public_origin" for t in n.targets)]
    assert len(hits) == 1
    v = hits[0].value
    assert isinstance(v, ast.Call) and isinstance(v.func, ast.Name) and v.func.id == ORIGIN_FN


def _fresh(mod: str):
    sys.modules.pop(mod, None)
    return importlib.import_module(mod)


PAY = [
    ("services.wechat_pay", "NOTIFY_URL", "WX_NOTIFY_URL", "/api/wallet/wechat-callback"),
    ("services.wechat_pay", "REFUND_NOTIFY_URL", "WX_REFUND_NOTIFY_URL", "/api/wallet/wechat-refund-callback"),
    ("services.xunhupay", "XUNHUPAY_NOTIFY_URL", "XUNHUPAY_NOTIFY_URL", "/api/wallet/xunhupay-callback"),
    ("services.xunhupay", "XUNHUPAY_RETURN_URL", "XUNHUPAY_RETURN_URL", "/wallet?payment=success"),
]


@pytest.mark.parametrize("mod,const,env,path", PAY)
def test_lock2_payment_defaults(monkeypatch, mod, const, env, path):
    # 线上形状:支付变量显式设了 ⇒ 用显式值(PUBLIC_BASE_URL 设什么都不影响)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test/")
    monkeypatch.setenv(env, "https://explicit.example/cb")
    assert getattr(_fresh(mod), const) == "https://explicit.example/cb"
    # 支付变量没设 ⇒ 唯一出处 + 原路径
    monkeypatch.delenv(env, raising=False)
    assert getattr(_fresh(mod), const) == "https://example.test" + path
    # 支付变量是空串 ⇒ 当没设
    monkeypatch.setenv(env, "")
    assert getattr(_fresh(mod), const) == "https://example.test" + path
    # 都没设 ⇒ 与改前缺省逐字节相同
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv(env, raising=False)
    assert getattr(_fresh(mod), const) == "http://127.0.0.1:8001" + path
    _fresh(mod)


# ───────────────────────────────────────────────────────────── 锁 3:字面量普查(认 f-string)
_KINDS = {tokenize.STRING} | ({tokenize.FSTRING_MIDDLE} if hasattr(tokenize, "FSTRING_MIDDLE") else set())
_STMT_START = {tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING}


def domain_literals(path: str, text: str) -> list[tuple[str, int]]:
    """一个文件里真会生效的 127.0.0.1:8001 字面量:STRING 与 FSTRING_MIDDLE;注释、docstring(语句级裸字符串)不算。"""
    hits, prev = [], None
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError):
        # 分不了词的(如 tools/scoring/geo_scorer_backup.py,开头就是控制字符,不是有效源码):
        #   保守 —— 全文出现域名就逐行算命中,宁可红也不跳过不数
        return [(path, i) for i, ln in enumerate(text.splitlines(), 1) if DOMAIN in ln]
    for t in toks:
        if t.type in _KINDS and DOMAIN in t.string:
            is_doc = t.type == tokenize.STRING and (prev is None or prev.type in _STMT_START)
            if not is_doc:
                hits.append((path, t.start[0]))
        if t.type not in (tokenize.NL, tokenize.COMMENT):
            prev = t
    return hits


@functools.lru_cache(maxsize=1)
def _runtime_files_cached() -> tuple[tuple[str, str], ...]:
    out = subprocess.run(["git", "ls-files", "-z", "--", "*.py"], cwd=str(REPO), capture_output=True, check=True).stdout
    files = {}
    for f in (x for x in out.decode("utf-8").split("\0") if x):
        if f.startswith(("tests/", "scripts/")) or f.split("/")[0].startswith("agent-test-artifacts"):
            continue
        raw = (REPO / f).read_bytes()
        try:
            files[f] = raw.decode("utf-8")
        except UnicodeDecodeError:
            files[f] = raw.decode("latin-1")    # 域名是 ASCII:按字节解出来照样认得
    return tuple(files.items())


def _runtime_files() -> dict[str, str]:
    return dict(_runtime_files_cached())


@functools.lru_cache(maxsize=None)
def _hits_of(path: str, text: str) -> tuple[tuple[str, int], ...]:
    return tuple(domain_literals(path, text))


def census(files: dict[str, str]) -> list[tuple[str, int]]:
    hits = []
    for f, text in files.items():
        hits += _hits_of(f, text)   # 同一份文本只分一次词(三格共用;牙证 / 对照改过的 server.py 文本会重新分)
    return hits


def test_lock3_exactly_one_literal_in_the_single_source():
    files = _runtime_files()
    assert len(files) > 500, len(files)    # 分母自证
    hits = census(files)
    assert [h[0] for h in hits] == ["services/owned_image_policy.py"], hits


def test_lock3_tooth_an_fstring_put_back_into_server_is_red():
    files = _runtime_files()
    base = len(census(files))
    files["server.py"] += '\n_e10 = f"http://127.0.0.1:8001/api/sl/{code}"\n'
    assert len(census(files)) == base + 1, "塞回一处 f-string 普查必须多 1(只数 STRING 会漏)"


def test_lock3_control_a_comment_with_the_domain_stays_green():
    files = _runtime_files()
    base = len(census(files))
    files["server.py"] += "\n# 分享链接以前写死成 http://127.0.0.1:8001\n"
    assert len(census(files)) == base
