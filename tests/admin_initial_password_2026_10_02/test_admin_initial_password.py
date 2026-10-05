# -*- coding: utf-8 -*-
"""WO_327 · 初始管理员口令不再写死(Review 10-02 · 开源前必修)。

病:db/auth_db._seed_admin_user 在空库上写死 admin / admin123;「首登强制改密」只在前端,后端只透传
must_change_password、不拦请求 ⇒ 开源后任何人拿这组口令直接调登录接口就是管理员。
修:读 ADMIN_INITIAL_PASSWORD(≥ 12 位,短了直接报错);没设 ⇒ 随机 24 位,落库后只打印一次;must_change_password 照旧 1。

种子函数用假连接驱动(只认它发出的 SQL 与参数),口令校验用 auth_db 自己的 verify_password:
  A1 没设 ⇒ 随机口令 ≥ 20 位、能登录;admin123 不能登录;WARNING 恰好一条且含口令;提交在打印之前;must_change_password = 1
  A2 设了 ⇒ 用它;admin123 不能登录;日志里不出现口令
  A3 设了但太短 ⇒ 报错,一条 INSERT 都没发
  A4 admin 已存在(生产库) ⇒ 不建、不打印
  A5 两次空库初始化的随机口令不同;A6 .env 里留空(= 模板)⇒ 按没设处理
  L1 冷启动链路(db/ auth/ server.py scripts/prestart.py)里没有 admin123 字面量;种子函数里 hash_password 不许传字符串常量(带牙证)
"""
from __future__ import annotations

import ast
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from db import auth_db  # noqa: E402


class _Cur:
    def __init__(self, log, admin_exists=False):
        self.log, self.admin_exists, self._next = log, admin_exists, None

    def execute(self, sql, params=None):
        self.log.append(("sql", " ".join(sql.split()), params))
        if "FROM users WHERE username = 'admin'" in sql:
            self._next = {"id": 1} if self.admin_exists else None
        elif sql.strip().upper().startswith("INSERT INTO USERS"):
            self._next = {"id": 7}
        elif "FROM roles WHERE name = 'admin'" in sql:
            self._next = {"id": 3}
        else:
            self._next = None

    def fetchone(self):
        return self._next


class _Conn:
    def __init__(self, admin_exists=False):
        self.log = []
        self.cur = _Cur(self.log, admin_exists)

    def cursor(self):
        return self.cur

    def commit(self):
        self.log.append(("commit", None, None))


class _Recorder(logging.Handler):
    def __init__(self, log):
        super().__init__(logging.DEBUG)
        self.log = log

    def emit(self, record):
        self.log.append(("log", record.levelname, record.getMessage()))


def _seed(monkeypatch, env=None, admin_exists=False):
    if env is None:
        monkeypatch.delenv("ADMIN_INITIAL_PASSWORD", raising=False)
    else:
        monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", env)
    conn = _Conn(admin_exists)
    h = _Recorder(conn.log)
    auth_db.logger.addHandler(h)
    old = auth_db.logger.level
    auth_db.logger.setLevel(logging.DEBUG)
    try:
        auth_db._seed_admin_user(conn)
    finally:
        auth_db.logger.removeHandler(h)
        auth_db.logger.setLevel(old)
    inserts = [p for kind, sql, p in conn.log if kind == "sql" and sql.upper().startswith("INSERT INTO USERS")]
    logs = [(lvl, msg) for kind, lvl, msg in conn.log if kind == "log"]
    return conn.log, inserts, logs


def test_a1_unset_generates_a_random_password_printed_exactly_once(monkeypatch):
    log, inserts, logs = _seed(monkeypatch)
    assert len(inserts) == 1
    username, pw_hash, _display, must_change = inserts[0]
    assert username == "admin" and must_change == 1
    assert not auth_db.verify_password("admin123", pw_hash)                 # 🔴 老口令登录不了
    warns = [m for lvl, m in logs if lvl == "WARNING"]
    assert len(warns) == 1, logs                                            # 恰好打印一次
    m = re.search(r"随机初始口令:(\S+) ——", warns[0])
    assert m, warns[0]
    pw = m.group(1)
    assert len(pw) >= 20 and auth_db.verify_password(pw, pw_hash)           # 打印的就是写进库的那个
    assert sum(pw in msg for _lvl, msg in logs) == 1
    order = [k for k, *_ in log]
    assert order.index("commit") < order.index("log")                       # 落库之后才打印


def test_a2_env_password_is_used_and_never_logged(monkeypatch):
    env = "Env-Initial-Pw-2026!"
    _log, inserts, logs = _seed(monkeypatch, env=env)
    pw_hash = inserts[0][1]
    assert auth_db.verify_password(env, pw_hash) and not auth_db.verify_password("admin123", pw_hash)
    assert inserts[0][3] == 1
    assert not any(env in msg for _lvl, msg in logs)
    assert not any(lvl == "WARNING" for lvl, _m in logs)


def test_a3_short_env_password_fails_loud_before_any_insert(monkeypatch):
    with pytest.raises(RuntimeError, match="ADMIN_INITIAL_PASSWORD"):
        _seed(monkeypatch, env="short1234")
    # 「至少 12 位」的边界:12 位放行
    _log, inserts, _logs = _seed(monkeypatch, env="x" * 12)
    assert len(inserts) == 1


def test_a3b_short_env_sends_no_insert(monkeypatch):
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "admin123")
    conn = _Conn()
    with pytest.raises(RuntimeError):
        auth_db._seed_admin_user(conn)
    assert not any(kind == "sql" and sql.upper().startswith("INSERT") for kind, sql, _p in conn.log)


def test_a4_existing_admin_is_left_alone(monkeypatch):
    log, inserts, logs = _seed(monkeypatch, admin_exists=True)
    assert inserts == [] and logs == [] and not any(k == "commit" for k, *_ in log)


def test_a5_a6_random_each_time_and_empty_env_means_unset(monkeypatch):
    pws = []
    for env in (None, ""):
        _log, _ins, logs = _seed(monkeypatch, env=env)
        warns = [m for lvl, m in logs if lvl == "WARNING"]
        assert len(warns) == 1
        pws.append(re.search(r"随机初始口令:(\S+) ——", warns[0]).group(1))
    assert pws[0] != pws[1]


# ---------------------------------------------------------------- 静态锁

_COLD_PATHS = ["db", "auth", "server.py", "scripts/prestart.py"]


def _literal_hits(regex: str, cwd: Path, paths: list, no_index: bool = False) -> list:
    args = ["git", "grep"] + (["--no-index"] if no_index else []) + ["-l", "-P", regex, "--"] + paths
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode not in (0, 1):
        raise RuntimeError(f"git grep 判不了 rc={r.returncode}: {r.stderr[-200:]}")
    return sorted(x.strip().replace("\\", "/") for x in r.stdout.splitlines() if x.strip())


_ADMIN123 = r"admin" + r"123"


def _hash_literal_calls(src: str) -> list:
    """_seed_admin_user 里 hash_password(<字符串常量>) 的调用。"""
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "_seed_admin_user")
    return [c for c in ast.walk(fn) if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "hash_password"
            and c.args and isinstance(c.args[0], ast.Constant)]


def test_l1_no_hardcoded_initial_password(tmp_path):
    assert _literal_hits(_ADMIN123, ROOT, _COLD_PATHS) == []
    src = (ROOT / "db" / "auth_db.py").read_text(encoding="utf-8")
    assert _hash_literal_calls(src) == []
    assert "hash_password(initial_password)" in src
    # 牙证:同一个 git grep 引擎扫临时目录;同一个 AST 判据扫改回去的源码
    (tmp_path / "db").mkdir()
    (tmp_path / "db" / "x.py").write_text('admin_hash = hash_password("' + "admin" + '123")\n', encoding="utf-8")
    (tmp_path / "db" / "ok.py").write_text("admin_hash = hash_password(initial_password)\n", encoding="utf-8")
    assert _literal_hits(_ADMIN123, tmp_path, ["db"], no_index=True) == ["db/x.py"]
    poisoned = src.replace("hash_password(initial_password)", 'hash_password("' + "admin" + '123")')
    assert len(_hash_literal_calls(poisoned)) == 1
