# -*- coding: utf-8 -*-
"""变异 runner 的**两道机制闸**(Review 机制令 2026-08-26)。

这两条不是「更严格一点」的优化,是把**口头纪律**换成**执行时会自己报错的东西**。
本仓 2026-08-26 一天之内被同一件事咬了两次:

  · A 的**缓存重放事故** —— A 补完判据后重跑,runner 见 id 已在
    ``extsel_results.json`` 里就跳过,于是**新判据从没面对过那些变异**,
    而结果文件长得跟跑过一模一样。
  · **三伤**(窗口 A/C/B 同树)—— 派单写「串行」,三个窗口照样同时在改同一棵树:
    我的基线读到别人写了一半的判据文件,报 5 红;单跑一条都复现不了。
    撕锁 runner 是**就地改源文件**的,并发下谁的毒落在谁的基线上根本无法归属。

口头串行守不住(实测第二次)。所以:

  ① :func:`tree_lock` —— **树级**排他文件锁 ``<repo>/.mutlock``。
     起跑抢锁,存在即**拒跑**并打印持锁者现场;崩溃残锁**只许带人工确认强解**
     (``MUTLOCK_BREAK`` 必须等于锁里那个 token —— 强解的人必须真的读过锁文件,
     ``MUTLOCK_BREAK=1`` 这种瞎设的一律拒)。
  ② :func:`criteria_fingerprint` —— 结果缓存键**并入判据文件集指纹**。
     判据变了 ⇒ 指纹变 ⇒ 缓存条目失效 ⇒ 必须重跑。
     指纹的文件集是从 **pytest 目标路径机械枚举**出来的,不是手抄清单 ——
     手抄的分母漏掉的那一项不会让任何判据变红(本仓记过)。

两条都放在**同一个模块**里给三个 runner 共用:同一个谓词写两处,必有一处没人验。
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = ROOT / ".mutlock"


# ══════════════════════════════════════════════════════════════════════════
# ① 树级排他锁
# ══════════════════════════════════════════════════════════════════════════
def _read_lock() -> dict:
    try:
        return json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 锁文件损坏/半写:当成「有人持锁」处理 —— 拒跑方向永远是安全的那一侧。
        return {"token": "<unreadable>", "raw": "锁文件不可解析"}


def _age(held: dict) -> str:
    try:
        return f"{(time.time() - float(held.get('started_at', 0))) / 60:.1f} 分钟前"
    except (TypeError, ValueError):
        return "未知时间"


def acquire(runner: str) -> str:
    """抢锁;抢不到就 :class:`SystemExit`。返回本次持有的 token。"""
    token = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
    payload = {
        "token": token,
        "runner": runner,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": time.time(),
        "argv": sys.argv[1:],
        "cwd": str(Path.cwd()),
    }
    blob = json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")

    for attempt in (1, 2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            held = _read_lock()
            want = os.environ.get("MUTLOCK_BREAK", "").strip()
            if attempt == 1 and want and want == held.get("token"):
                # 🔴 强解**只**认锁里那个 token:能填对 = 真的读过锁文件、
                #    真的确认过那个 pid 已经不在了。MUTLOCK_BREAK=1 一律拒。
                print(f"⚠️  人工强解残锁:token={want} runner={held.get('runner')} "
                      f"pid={held.get('pid')} 起于 {_age(held)}", flush=True)
                LOCK_PATH.unlink(missing_ok=True)
                continue
            raise SystemExit(
                "🔴 **树已被另一个变异 runner 锁住** —— 拒跑。\n"
                f"    锁文件 : {LOCK_PATH}\n"
                f"    持有者 : runner={held.get('runner')} pid={held.get('pid')} "
                f"host={held.get('host')}\n"
                f"    起跑于 : {_age(held)}\n"
                f"    token  : {held.get('token')}\n"
                "    撕锁 runner 就地改源文件,并发下毒落在谁的基线上无法归属\n"
                "    (2026-08-26 三伤:口头串行守不住,实测第二次)。\n"
                "    确认那个进程真的死了之后,带 token 强解:\n"
                f"        MUTLOCK_BREAK={held.get('token')} python <runner>")
        else:
            os.write(fd, blob)
            os.close(fd)
            return token
    raise SystemExit("🔴 强解后仍抢不到锁 —— 停下人工看。")


def release(token: str) -> None:
    """只释放**自己**那把 —— 强解之后别人可能已经重新持锁。"""
    if not LOCK_PATH.exists():
        return
    if _read_lock().get("token") != token:
        print(f"⚠️  锁已不是我的(token {token} 不匹配)—— 不动它。", flush=True)
        return
    LOCK_PATH.unlink(missing_ok=True)


@contextmanager
def tree_lock(runner: str):
    token = acquire(runner)
    print(f"🔒 树锁已持有 · runner={runner} token={token}", flush=True)
    try:
        yield token
    finally:
        release(token)
        print(f"🔓 树锁已释放 · token={token}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
# ② 判据文件集指纹(缓存键的一部分)
# ══════════════════════════════════════════════════════════════════════════
def _test_support_imports(path: Path) -> set[Path]:
    """这个判据文件 import 了哪些 **tests/ 下的支撑模块**。

    🔴 [E1-3② = Codex 二审 §8] 只跟 ``tests.*``,**绝不跟生产模块**。
       生产代码是**被测对象**,不是判据:把它算进指纹,变异 runner 每改一次
       源文件指纹就变一次,缓存永远失效 —— 那不是收紧,是把机制搞坏。

    两种形态都要认(第一版只认了前一种,于是 ``_seed`` / ``_chain`` 这类
    最重要的夹具全漏在外面)::

        from tests.pkg.helper import x      # 模块在 n.module 上
        from tests.pkg import _seed         # 模块是 module + "." + alias
    """
    out: set[Path] = set()

    def resolve(mod: str) -> Path | None:
        if not (mod == "tests" or mod.startswith("tests.")):
            return None
        p = ROOT.joinpath(*mod.split("."))
        for cand in (p.with_suffix(".py"), p / "__init__.py"):
            if cand.is_file():
                return cand.resolve()
        return None

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for cand in (node.module,
                         *(f"{node.module}.{a.name}" for a in node.names)):
                q = resolve(cand)
                if q:
                    out.add(q)
        elif isinstance(node, ast.Import):
            for a in node.names:
                q = resolve(a.name)
                if q:
                    out.add(q)
    return out


def _criteria_files(targets: Iterable[str]) -> list[Path]:
    """从 pytest 目标**机械枚举**「判据可达文件集」。

    收三类:
      · ``test_*.py``  —— 判据本体;
      · ``conftest.py`` —— 单文件目标要一路上溯到仓根,conftest 支配判据行为;
      · **判据可达的 tests/ 支撑模块**(``_seed`` / ``_chain`` / ``_world`` /
        ``_census`` / 各级 ``__init__``),按 import 做**传递闭包**。

    🔴 第三类是 [E1-3②] 补的。原口径只有前两类,实测**漏 14 个**
       (机械枚举见交付文;Codex 报的是 15,我按自己工具的数报,差的一位如实标出)。
       夹具变了而指纹不变 = 缓存该失效的时候没失效 —— 这正是这道闸要挡的事,
       所以名字也从「判据文件集指纹」改成「**判据可达**文件集指纹」。
    """
    found: set[Path] = set()
    for raw in targets:
        p = (ROOT / raw.split("::")[0]).resolve()
        if p.is_dir():
            for q in p.rglob("*.py"):
                if q.name.startswith("test_") or q.name == "conftest.py":
                    found.add(q)
            base = p
        elif p.is_file():
            found.add(p)
            base = p.parent
        else:
            # 目标不存在 = 分母出了问题,不许静默当 0 条过去。
            raise SystemExit(f"🔴 判据目标不存在:{raw} —— 停下报 Review")
        for d in [base, *base.parents]:
            c = d / "conftest.py"
            if c.exists():
                found.add(c)
            if d == ROOT:
                break
    # 判据可达的 tests/ 支撑模块 —— 传递闭包(helper 还会 import helper)。
    queue, seen = list(found), set()
    while queue:
        f = queue.pop()
        if f in seen:
            continue
        seen.add(f)
        for q in _test_support_imports(f):
            if q not in found:
                found.add(q)
                queue.append(q)
    return sorted(found)


def criteria_fingerprint(targets: Iterable[str]) -> tuple[str, dict]:
    """返回 ``(指纹, 元数据)``。判据文件**任意**一字节变动都会换指纹。"""
    files = _criteria_files(targets)
    if not files:
        raise SystemExit(f"🔴 判据文件集为空:{list(targets)} —— 空分母不算通过")
    h = hashlib.sha256()
    for f in files:
        rel = f.relative_to(ROOT).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(hashlib.sha256(f.read_bytes()).digest())
    return h.hexdigest()[:16], {
        "n_files": len(files),
        "targets": list(targets),
    }


def cache_is_fresh(record: dict, fp: str) -> bool:
    """缓存条目还能不能用。

    🔴 **没有** ``criteria_fp`` 字段的旧条目一律判**陈旧** —— 它是在这道闸
       之前写的,谁也不知道当时的判据长什么样。方向永远选重跑那一侧。
    """
    return record.get("criteria_fp") == fp
