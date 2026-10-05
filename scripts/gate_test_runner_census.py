#!/usr/bin/env python3
"""WO_263 ③ · 运行者普查门:`tests/` 顶层每一项都必须指得出谁在跑它(三态)。

## 为什么要这一把
2026-09-22 实测:全仓 pytest **没有任何常驻运行者**。
  · `.github/workflows/` 三个 workflow 里只有两个跑 pytest,选择器各只选
    **一个文件**(`tests/test_keyword_quality_regression.py`)和
    **一个子目录 + 一条通配**(`tests/research_monitor/`、`tests/api/test_research_monitor_*.py`);
  · `.deploy_toolkit/` 的 preflight / prep / bake **一个都不跑 pytest**;
  · `preflight.sh` 里那句「`tests/response_model_contract`(G2 · MUST_RUN 必跑集)兜底」
    **只是注释**,靠 Review 签字时手工挑包。

🔴 最扎眼的一处:那句注释的**上一行**写的是
   「**注释传不出去,只有会自己报错的东西才拦得住** —— 所以进 preflight」。
   写下这句话的人,紧接着就把「必跑集」这件事留在了注释里。
   ⇒ 「存在 ≠ 有人跑」。`tests/test_geo_douyin_detail_ui.py` 7 格红就是这么来的:
     它从来没有被任何自动化跑过。

## 四类(每一项必须落在且只落在一类)
  ① MUST_RUN   —— 登记在 `tests/MUST_RUN.txt`,preflight G2b 每班真跑
  ② WORKFLOW   —— 被某 workflow 的 pytest 选择器**机械解析**命中
  ③ ONESHOT    —— 随单登记(路径 · 守什么 · 出处)。~~跑过一次,不再常驻~~ ——
                  **WO_266 起作废**:G2b 的 ONESHOT 段每班全跑本表全集,受时长预算约束
  ④ RETIRED    —— 已退役,登记**接替者**与原因(WO_266 起接替者必须指得出运行者)
  四类之外 = **无运行者**,冻进 `UNASSIGNED_FROZEN`(**只许减不许增**)。

## WO_266 加的八格(与上面的既有判据分开打印、分开计数)
  · 随单登记格式:每条必须三栏 `路径<TAB>守什么<TAB>出处`,没写「守什么」⇒ 红
  · 时长预算行:登记表头恰好一行 `# ONESHOT_BUDGET_SECONDS=<正整数>` ⇒ 否则红
  · 预算棘轮:与**基准**(待发树与 `github/main` 的 merge-base)上的同一行比,改大 ⇒ 红
  · 冻结名单相对基准只许减:多出一项 ⇒ 红
    (🔴 WO_263 的 PASS 文案一直写着「冻结名单未变大」,而那时**没有任何一格在验它**:
       新包直接塞进冻结名单,旧判据照样绿。本格是把那句话兑现。)
  · 运行者守恒:基准上登记在 MUST_RUN / ONESHOT 的每一项,待发树里要么仍有运行者,
    要么在 RETIRED 里写明接替者 ⇒ 否则红(挡「直接删登记行」「挪进冻结名单」)
  · 退役接替:RETIRED 每条三栏 `路径<TAB>接替者<TAB>原因`,接替者必须存在;
    在 tests/ 下的接替者自己必须有运行者 ⇒ 否则红(挡「退役等于删判据」);
    接替者可写 `文件::格`(WO_297,与第 1 栏对称):文件须存在、格须在文件里有定义;
    tests/ 外的接替者(WO_289)逐字出现在 `frontend/package.json` build 链某一步 ⇒
    由 bake 跑,✅ 并写明第几步;不在链上 ⇒ ⚠️;build 链读不到 ⇒ rc=3
  · 删除引用者(Review 09-23 补):本班删掉/改名掉的每个文件,按文件名在全仓(不含 docs/·
    *.md·*.txt)找引用者,本班没动过的引用者 ⇒ 红;本班动过的,**提到它的那几行本身**
    也必须都变了(裁定 ②:文件别处改一行不算看过)⇒ 否则红。文件名太泛的不搜、
    只搬目录的不判,两类都**逐个点名自曝**,不装作搜过。
  · 必跑集预算(Review 09-23 裁定 ①):`tests/MUST_RUN.txt` 表头恰好一行
    `# MUST_RUN_BUDGET_SECONDS=<正整数>`;G2b 超了同样红。可以上调,但只能是
    「单独一笔、只改这一行」的提交 ⇒ 打 ⚠️ 并要求签字条点名;夹在别的改动里上调 ⇒ 红。
  随单超预算的处置只有三条合法出口:提进 MUST_RUN / 肯定式退役 / 拆包;
  提进 MUST_RUN 之后同样受必跑集预算约束(不再是只黄不红的出口)。
  上面几格把**其余出口**(改大预算 / 挪进冻结名单 / 删登记行 / 空口退役)都堵成红。

## 退出码(三态)
  0 = 没有未登记的新项,冻结名单没变大,WO_266 八格全绿
  1 = 有新项没登记 / 登记了却不存在的路径 / WO_266 任一格红
  3 = 没跑成(取不到 tests/ 或 workflow 目录 / 自校准不过 / 基准 ref 解析不了)
"""

from __future__ import annotations

import ast
import fnmatch
import json
import re
import shlex
import subprocess
import sys
from collections import Counter
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
SELF_REL = "scripts/gate_test_runner_census.py"
MUST_RUN_FILE = "tests/MUST_RUN.txt"
FROZEN_FILE = "tests/UNASSIGNED_FROZEN.txt"
ONESHOT_FILE = "tests/ONESHOT_RUNS.txt"
RETIRED_FILE = "tests/RETIRED_TESTS.txt"
#: [Review 09-28 · E3a] ⑦「删除引用者」的注释行白名单:三栏 `文件 \t 该行原文(去首尾空白) \t 理由`。
#:   只收两种行:十个保护文件里的 `#` 注释行、`.sql` 文件里的 `--` 注释行 —— 保护文件不许为改一行注释去动,
#:   迁移 .sql 改了会被 preflight 第 2 关判红。门会核:文件属于这两类、该行在待发一侧仍原样存在、仍是纯注释;
#:   非注释的引用照样红。
COMMENT_ALLOW_FILE = "tests/REFERENCER_COMMENT_ALLOW.txt"
#: 保护文件十项(签字条点名口径,与 Review 签字条一致;preflight P7 仪器权威那份是其中 8 项)
PROTECTED_FILES = (
    "middleware/billing.py", "db/connection.py", "auth/middleware.py", "auth/jwt_utils.py",
    "config/pricing_config.py", "tools/transparent_pricing.py", "db/wallet_db.py",
    "config/settings_manager.py", "services/payment_routing.py", "tools/pricing_bands.py",
)
BUILD_PKG_FILE = "frontend/package.json"
BUILD_PKG_DIR = "frontend/"

_GIT_REPO = ""
_GIT_REF = ""


# [AE · 自测提速] 单次运行内缓存「读对象」的 git 调用(show / ls-tree / cat-file):
#   同一张登记表在一次普查里要按 ref、基准各读 2–4 次,git 对象又不可变 ⇒ 缓存不改变任何读数。
#   🔴 缓存只活在一次 main() 里(main 开头清空):进程内连续调用时,`HEAD` 这类可变名字不许串到下一次。
_GIT_CACHE: dict[tuple[str, ...], tuple[int, bytes]] = {}
_CACHEABLE = ("show", "ls-tree", "cat-file")


# [ONESHOT 提速] `git show <ref>:<path>` 走一个常驻 `git cat-file --batch`(每次 main() 一个,main 结束关掉):
#   原先每读一个文件起一次 git 进程(Windows 上 ~20ms/次,WO_266 锁包 70 格 × 8 次)。
#   只接管「对象是 blob」这一种 —— 与 `git show` 对 blob 的输出逐字节相同(原样内容,不过滤器);
#   missing / ambiguous / 非 blob(目录等)一律回落原来的 `git show` 子进程,退出码与输出都照旧。
_BATCH: dict[str, subprocess.Popen] = {}


def _batch_blob(spec: str) -> bytes | None:
    repo = _GIT_REPO or str(ROOT)
    p = _BATCH.get(repo)
    if p is None:
        p = subprocess.Popen(["git", "-C", repo, "cat-file", "--batch"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        _BATCH[repo] = p
    p.stdin.write(spec.encode("utf-8") + b"\n")
    p.stdin.flush()
    header = p.stdout.readline().rstrip(b"\n")
    if header.endswith((b" missing", b" ambiguous")):
        return None
    _oid, typ, size = header.split(b" ")
    data = p.stdout.read(int(size))
    p.stdout.read(1)  # 内容后的 LF
    return data if typ == b"blob" else None


def _close_batch() -> None:
    for p in _BATCH.values():
        try:
            p.stdin.close()
            p.wait(timeout=10)
        except Exception:
            p.kill()
    _BATCH.clear()


def _git(*args: str) -> tuple[int, bytes]:
    key = (_GIT_REPO or str(ROOT), *args)
    if args and args[0] in _CACHEABLE and key in _GIT_CACHE:
        return _GIT_CACHE[key]
    if len(args) == 2 and args[0] == "show" and ":" in args[1] and "\n" not in args[1]:
        data = _batch_blob(args[1])
        if data is not None:
            _GIT_CACHE[key] = (0, data)
            return 0, data
    r = subprocess.run(["git", "-C", _GIT_REPO or str(ROOT), *args],
                       capture_output=True)
    if args and args[0] in _CACHEABLE:
        _GIT_CACHE[key] = (r.returncode, r.stdout)
    return r.returncode, r.stdout


def read_file(rel: str) -> str | None:
    if _GIT_REF:
        rc, out = _git("show", f"{_GIT_REF}:{rel}")
        if rc != 0:
            return None
        try:
            return out.decode("utf-8")
        except UnicodeDecodeError:
            return None
    p = ROOT / rel
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def tests_top_level() -> list[str]:
    """`tests/` 顶层的包与文件(不递归)。

    🔴 两种模式读**不同的东西**,这是刻意的:
      · `--ref` 模式(preflight 用)读那个 ref 的树 —— 验的是**待发包**;
      · 无 ref 模式(本地/判据用)读**工作树** —— 开发者新建一个测试包、
        还没 commit 的时候就该看见红。第一版两边都读 HEAD,于是
        「新建 tests/zz_probe 不登记必红」这条反臂**根本不可能响**:
        探针不在 HEAD 里,门当然看不见它。反臂自己先是假的。
    """
    if _GIT_REF:
        rc, out = _git("ls-tree", "--name-only", "-z", _GIT_REF, "tests/")
        if rc != 0:
            return []
        items = [b.decode("utf-8") for b in out.split(b"\0") if b]
    else:
        # 🔴 **按文件枚举,不读裸目录**。
        #    第一版 `base.iterdir()` 把「只剩 __pycache__ 的空壳目录」也算成一项 ——
        #    切分支后的残留目录会让门报「新增测试包没登记」,那是**假红**:
        #    那里根本没有判据,只有 .pyc。
        #    改成:tracked 文件 ∪ untracked 且不被 .gitignore 忽略的文件,
        #    取它们在 tests/ 下的第一段。目录里没有一个这样的文件 ⇒ 它不存在。
        rc1, tracked = _git("ls-files", "-z", "tests/")
        rc2, untracked = _git("ls-files", "--others", "--exclude-standard",
                              "-z", "tests/")
        if rc1 != 0:
            return []
        paths = [b.decode("utf-8") for b in (tracked + untracked).split(b"\0") if b]
        seen: set[str] = set()
        for p in paths:
            rest = p[len("tests/"):]
            if not rest or "__pycache__" in rest:
                continue
            seen.add("tests/" + rest.split("/", 1)[0])
        items = sorted(seen)
    # 只留可能含判据的:子目录,或 .py 文件
    return sorted(
        i for i in items
        if not i.endswith((".txt", ".md", ".json", ".cfg", ".ini"))
    )


def _registry(rel: str) -> dict[str, str]:
    """登记表:`路径<空白>说明`,`#` 开头是注释。返回 {路径: 说明}。"""
    text = read_file(rel)
    if text is None:
        return {}
    return _parse_registry(text)


def _parse_registry(text: str) -> dict[str, str]:
    # 🔴 [WO_266] 从 `_registry` 原样拆出来,好让基准 ref 上的登记表走**同一个**解析器。
    #    既有判据的每一处读取都还经过这里,逐字未改(见交付单差分表)。
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        out[parts[0].rstrip("/")] = parts[1].strip() if len(parts) > 1 else ""
    return out


# ============================================================ WO_266
# 🔴 预算行的形状**同时被 preflight G2b 用 bash 读**(`grep -E` 同一个正则)。
#    两边必须逐字同形,所以这里故意写得死板:整行、无多余空白、正整数、**无前导零**。
#    前导零不是洁癖:bash 算术把 `0120` 当八进制 = 80,两边会读出两个不同的预算。
#    形状不对 ⇒ 两边都读成「没有预算行」⇒ 两边都红 —— 分歧只许以**响亮**的方式出现。
BUDGET_LINE_RE = re.compile(r"^# ONESHOT_BUDGET_SECONDS=([1-9][0-9]*)$")
# [WO_266 · Review 09-23 裁定 ①] 必跑集同样有独立预算行,超了同样红。
#   与随单预算的**区别**:随单预算只许降;必跑集预算可以上调,但只能是
#   「**单独一笔、只改这一行**」的提交(由 ⑧ 机器核),并由签字条点名。
MUST_RUN_BUDGET_LINE_RE = re.compile(r"^# MUST_RUN_BUDGET_SECONDS=([1-9][0-9]*)$")
GUARD_MIN_CHARS = 8          # 「守什么」至少一句话,不是一个词
_WO_TAG = re.compile(r"WO_\d+")
# 「栏位串了」= 守什么那一栏长成了**出处的形状**(`WO_号 · …`)。
# 🔴 第一版写成「以 WO_数字 开头就红」—— 臂 B1 当场误伤一句正常的守什么
#    (「WO_266 臂:故意红的随单包…」)。守什么里提到工单号是正常写法,
#    要抓的是**整栏就是出处**,所以只认出处的分隔形状。
_ORIGIN_SHAPE = re.compile(r"WO_\d+\s*·")
DEFAULT_BASE_REF = "github/main"
# 有运行者的类(退役接替者、运行者守恒都认这几类)
RUNNER_CLASSES = ("MUST_RUN", "WORKFLOW", "WORKFLOW_PARTIAL", "ONESHOT")


def _read_at(ref: str, rel: str) -> str | None:
    """**永远按 git ref 读**(基准一侧)。取不到 ⇒ None,由调用方判灰还是红。"""
    rc, out = _git("show", f"{ref}:{rel}")
    if rc != 0:
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _exists_now(rel: str) -> bool:
    """待发一侧:`--ref` 模式问那个 ref,工作树模式问磁盘(与 read_file 同一口径)。"""
    if _GIT_REF:
        return _git("cat-file", "-e", f"{_GIT_REF}:{rel}")[0] == 0
    return (ROOT / rel).exists()


def _budget_values(text: str, rx: re.Pattern[str] = BUDGET_LINE_RE) -> list[int]:
    return [int(m.group(1)) for line in text.splitlines()
            if (m := rx.match(line))]


def _raising_commits(base: str, rel: str,
                     rx: re.Pattern[str]) -> tuple[list[str], list[str]] | None:
    """base..待发 之间**非合并**提交里,把 rel 的预算**调大**的那几笔:(纯的, 不纯的)。取不到 ⇒ None。

    「纯」= 这一笔只动了 rel 这一个文件、只换了预算这一行(numstat 1 增 1 删,两行都是预算行)。
    只看「调大」:下调永远合法;首次引入预算行(之前没有)不算调大。
    🔴 为什么跳过合并提交:班次用 --no-ff 合 WO 分支,合并提交相对第一亲本的差分
       会带上整条分支的改动 —— 拿它判「纯不纯」必然误判;那一笔纯提交本身就在范围里。
    """
    tip = _GIT_REF or "HEAD"
    rc, out = _git("rev-list", "--no-merges", f"{base}..{tip}", "--", rel)
    if rc != 0:
        return None
    pure: list[str] = []
    impure: list[str] = []
    for c in out.decode().split():
        b = _budget_values(_read_at(f"{c}^", rel) or "", rx)
        a = _budget_values(_read_at(c, rel) or "", rx)
        if not (len(a) == 1 and len(b) == 1 and a[0] > b[0]):
            continue
        rc2, ns = _git("diff", "--numstat", f"{c}^", c)
        files = [ln.split("\t") for ln in ns.decode().splitlines() if ln.strip()]
        rc3, patch = _git("diff", "-U0", f"{c}^", c, "--", rel)
        changed = [ln[1:] for ln in patch.decode("utf-8", "replace").splitlines()
                   if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
        ok = (rc2 == 0 and rc3 == 0 and len(files) == 1 and files[0][2] == rel
              and files[0][:2] == ["1", "1"] and len(changed) == 2
              and all(rx.match(x) for x in changed))
        (pure if ok else impure).append(c)
    return pure, impure


def _entry_lines(text: str) -> list[str]:
    """与 `_parse_registry` **同一个**过滤口径:strip 后非空、非 `#`。"""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s)
    return out


def _oneshot_entry_errors(text: str) -> list[str]:
    """随单登记每条必须三栏:`路径<TAB>守什么<TAB>出处`。

    🔴 为什么必须**按栏数**判,而不是「第二栏非空就算写了守什么」:
       存量五条是两栏(`路径<TAB>WO_255 · 签字条…`)。只看「第二栏非空」,
       出处那一栏就会被当成「守什么」,**五条老格式全部原样过关** ——
       那等于这一格对它要管的存量恒绿。
    """
    errs: list[str] = []
    for s in _entry_lines(text):
        cols = s.split("\t")
        path = s.split(None, 1)[0].rstrip("/")
        if len(cols) != 3:
            errs.append(f"随单登记 `{path}` 是 {len(cols)} 栏,要 3 栏"
                        "(`路径<TAB>守什么<TAB>出处`)—— 老格式缺「守什么」")
            continue
        guard, origin = cols[1].strip(), cols[2].strip()
        if len(guard) < GUARD_MIN_CHARS:
            errs.append(f"随单登记 `{path}` 的「守什么」不到 {GUARD_MIN_CHARS} 个字:"
                        f"`{guard}` —— 要一句话:它红了说明什么坏了")
        elif _ORIGIN_SHAPE.match(guard):
            errs.append(f"随单登记 `{path}` 的「守什么」一栏长成了出处的形状 `{guard[:24]}…` "
                        "—— 栏位串了(`WO_号 · 签字条` 属于第三栏「出处」)")
        if not _WO_TAG.search(origin):
            errs.append(f"随单登记 `{path}` 的「出处」没有 `WO_数字`:`{origin[:40]}`")
    return errs


def _defined_nodes(src: str | None) -> set[str] | None:
    """[WO_297] 一个测试文件里能被 `文件::格` 指到的名字:顶层 (async) def、class、`类::方法`。解析不了 ⇒ None。"""
    if src is None:
        return None
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    names: set[str] = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(n.name)
        elif isinstance(n, ast.ClassDef):
            names.add(n.name)
            names.update(f"{n.name}::{m.name}" for m in n.body
                         if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)))
    return names


def _retired_entries(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """退役登记每条三栏:`路径<TAB>接替者<TAB>原因`。返回 ([(路径, 接替者)], 格式错误)。"""
    ok_rows: list[tuple[str, str]] = []
    errs: list[str] = []
    for s in _entry_lines(text):
        cols = s.split("\t")
        path = s.split(None, 1)[0].rstrip("/")
        if len(cols) != 3 or not cols[1].strip() or not cols[2].strip():
            errs.append(f"退役登记 `{path}` 不是 3 栏(`路径<TAB>接替者<TAB>原因`)"
                        " —— 没写接替者的退役,对「正确替换了」和「整个功能被删了」都成立")
            continue
        ok_rows.append((path, cols[1].strip().rstrip("/")))
    return ok_rows, errs


def _build_chain_steps() -> tuple[list[str] | None, str]:
    """[WO_289] `frontend/package.json` 的 `scripts.build` 按 `&&` 切成步(第 1 步起数)。

    读不到 / 不是 JSON / 没有 build 串 ⇒ (None, 原因),调用方记 rc=3:判不了 ≠ 不在链上。
    """
    text = read_file(BUILD_PKG_FILE)
    if text is None:
        return None, f"`{BUILD_PKG_FILE}` 读不到"
    try:
        build = json.loads(text)["scripts"]["build"]
    except (ValueError, KeyError, TypeError):
        return None, f"`{BUILD_PKG_FILE}` 里取不到 scripts.build"
    if not isinstance(build, str) or not build.strip():
        return None, f"`{BUILD_PKG_FILE}` 的 scripts.build 不是非空字符串"
    return [s.strip() for s in build.split("&&")], ""


def _build_step_of(succ: str, steps: list[str]) -> int | None:
    """接替者路径在 build 链第几步**按名**出现(整词相等,不是子串);不在 ⇒ None。

    build 串的路径相对 `frontend/`,所以只有 `frontend/` 下的接替者可能在链上。
    🔴 只认直接写在 build 串里的那一步;经 `npm run xxx` 间接调用的不算(按不在链上 ⚠️)。
    """
    if not succ.startswith(BUILD_PKG_DIR):
        return None
    rel = succ[len(BUILD_PKG_DIR):]
    for i, step in enumerate(steps, 1):
        try:
            toks = shlex.split(step)
        except ValueError:
            toks = step.split()
        if rel in toks or f"./{rel}" in toks:
            return i
    return None


def _resolve_base(base_ref: str) -> tuple[str | None, str]:
    """基准 = 待发一侧与 `base_ref` 的 **merge-base**,不是 `base_ref` 本身。

    🔴 为什么不直接拿主干比:班次若分叉在主干更早的点、而主干之后又降过预算 /
       缩过冻结名单,直接比会把「主干后来的进步」算成「本包的倒退」⇒ 假红。
       merge-base 才是「本包是从哪个状态改出来的」。
    """
    rc, out = _git("rev-parse", "--verify", "-q", f"{base_ref}^{{commit}}")
    if rc != 0:
        return None, f"基准 ref `{base_ref}` 解析不了"
    tip = _GIT_REF or "HEAD"
    rc, mb = _git("merge-base", tip, out.decode().strip())
    if rc != 0:
        return None, f"`{tip}` 与 `{base_ref}` 没有共同祖先"
    return mb.decode().strip(), ""


# ---------------------------------------------------------------- ⑦ 删除的引用者
# 🔴 [WO_266 补 · Review 09-23] 「退役某文件时,分母 = 全仓引用者(含 tests/)」。
#    来由:09-08 6b491ab23 删了 DouyinImagePost.tsx 等 5 个创作页文件,那一笔**零改 tests/**;
#    当时的「退役引用者 k=0」只扫了 frontend/scripts。结果 tests/ 里 18 个文件还指着已删的页面,
#    全部变红 —— 而它们都在冻结名单里没人跑,于是**没有任何东西喊**。
#    ⇒ 本格:本班(merge-base..待发)每删掉 / 改名掉一个文件,就按文件名在全仓找引用者;
#      **本班没动过的引用者** ⇒ 红(没动 = 没人看过它还指不指着一个不存在的东西)。
#    按差分判:只管本班删的,不追溯历史(历史那 18 个另表处置)。
_GENERIC_STEMS = {"index", "__init__", "main", "mod", "types", "utils", "util", "api",
                  "config", "constants", "helpers", "styles", "test", "conftest", "app"}
_REF_SCOPE_EXCLUDES = (":(exclude)docs", ":(exclude)*.md", ":(exclude)*.txt")


def _is_distinctive(stem: str) -> bool:
    """够独特才按文件名搜。🔴 不够独特的**不装作搜过**:逐个点名,读数里写明没搜。"""
    return (len(stem) >= 8 and stem.lower() not in _GENERIC_STEMS
            and bool(re.search(r"[a-z][A-Z]|[_\-]|\d", stem)))


def _removals(base: str) -> tuple[list[str], list[tuple[str, str]], set[str]] | None:
    """返回 (本班删掉的路径, 同名搬家 [(旧, 新)], 本班动过的全部路径);取不到 ⇒ None。

    改名且改了文件名 ⇒ 旧名算删除(引用旧名的地方都得跟着改);
    只搬目录不改名 ⇒ 按文件名分不出新旧引用,**单列自曝**,不判。
    """
    args = ["diff", "--name-status", "-M", "-z", base] + ([_GIT_REF] if _GIT_REF else [])
    rc, out = _git(*args)
    if rc != 0:
        return None
    toks = out.decode("utf-8", errors="replace").split("\0")
    removed: list[str] = []
    moved: list[tuple[str, str]] = []
    changed: set[str] = set()
    i = 0
    while i < len(toks) and toks[i]:
        st = toks[i]
        if st[:1] in ("R", "C"):
            old, new = toks[i + 1], toks[i + 2]
            changed.update((old, new))
            if st[:1] == "R":
                if Path(old).stem == Path(new).stem:
                    moved.append((old, new))
                else:
                    removed.append(old)
            i += 3
        else:
            p = toks[i + 1]
            changed.add(p)
            if st[:1] == "D":
                removed.append(p)
            i += 2
    return removed, moved, changed


def _removal_key(path: str) -> str | None:
    p = Path(path)
    stem = p.stem
    if stem.lower() in ("index", "__init__", "main", "mod"):
        stem = p.parent.name          # 包/目录的入口文件:引用者引的是目录名
    return stem if _is_distinctive(stem) else None


def _tracked_files() -> list[str]:
    """待发一侧已跟踪的全部文件(ref 模式读 ls-tree,工作树模式读 ls-files)。"""
    rc, out = _git("ls-tree", "-r", "--name-only", _GIT_REF) if _GIT_REF else _git("ls-files")
    if rc != 0:
        raise RuntimeError(f"列不出待发树的文件 rc={rc}")
    return [x for x in out.decode("utf-8", errors="replace").splitlines() if x]


def _collision_keys(path: str, key: str, tracked: list[str],
                    removed: set[str]) -> tuple[list[str], list[str], str | None] | None:
    """[Review 09-28 · B2 前置] 删除键撞名:待发树里还有同名(同 stem)的在役文件 ⇒ 按文件名搜会误伤
    **正确引用新件**的在役文件(典型:E0 上提时新建同名文件、旧件留在包里,git 认不出 rename)。
    对这类键改用被删文件的**完整路径**判:点分 + 斜杠两种写法;所在包若在待发树里已整包没了,
    再加包路径的两种写法(旧包路径的任何写法都算残留引用)。照样判红,只是不再误伤。
    防逃生:同名在役文件必须真实存在且非空(tests/、docs/ 不算在役),否则不算撞名、照旧按文件名判。
    [Review 09-28 二审] 包没整包删(只删了包里一个文件)时,同目录(含子目录)在役兄弟文件仍按**裸名**判 ——
    相对导入 `from .advisor_llm import X` / `./advisor_llm` 只写裸名,只剩完整路径键会把它放过去。
    返回 (改用的路径键, 同名在役文件, 裸名仍适用的包目录 或 None);不撞名 ⇒ None。"""
    live_same = [f for f in tracked
                 if f not in removed and f != path and not f.startswith(("tests/", "docs/"))
                 and Path(f).stem == key and (read_file(f) or "").strip()]
    if not live_same:
        return None
    mod = str(Path(path).with_suffix("")).replace("\\", "/")
    if Path(path).stem.lower() in ("index", "__init__", "main", "mod"):
        mod = str(Path(path).parent).replace("\\", "/")
    keys = [mod, mod.replace("/", ".")]
    pkg = str(Path(path).parent).replace("\\", "/")
    sibling_dir = None
    if pkg not in ("", "."):
        if not any(f.startswith(pkg + "/") for f in tracked):
            keys += [pkg, pkg.replace("/", ".")]
        else:
            sibling_dir = pkg
    return sorted(set(keys)), sorted(live_same), sibling_dir


def _generic_path_keys(path: str, tracked: list[str], removed: set[str]) -> tuple[list[str], str | None, list[str]] | None:
    """[Review 09-28 · 门五] 文件名太泛(trace / chat_db / document …)的删除:不再只「没搜、请人工看」,
    与撞名改判同一套 —— 按被删文件的**完整路径**两种写法(点分 / 斜杠)全仓判;所在包在待发树里还有文件时,
    包目录(含子目录)下的兄弟文件再按**相对导入形的裸名**判:`.x` / `./x` / `import x`
    (泛名本身多是普通英文词,整词裸搜会满屏误报;相对导入只写裸名,只剩完整路径键会把它放过去)。
    来由:B2 删 agent_loop 包里那个泛名的 trace 模块,门只报「没搜」,人工按路径搜才找到 tests 里一个真引用者。
    完整路径只剩一段(仓根的 app.py、顶层包 meeting/)⇒ 点分 / 斜杠形都是普通词,仍列「没搜」。
    返回 (路径键, 相对导入键仍适用的包目录 或 None, 相对导入形的裸名键);不适用 ⇒ None。"""
    p = Path(path)
    entry = p.stem.lower() in ("index", "__init__", "main", "mod")
    mod = str(p.parent if entry else p.with_suffix("")).replace("\\", "/")
    if "/" not in mod:
        return None
    bare = Path(mod).name
    keys = [mod, mod.replace("/", ".")]
    pkg = str(Path(mod).parent).replace("\\", "/")
    sibling_dir = pkg if (pkg not in ("", ".")
                          and any(f.startswith(pkg + "/") and f not in removed for f in tracked)) else None
    return sorted(set(keys)), sibling_dir, [f".{bare}", f"./{bare}", f"import {bare}"]


#: [Review 09-28 · B2 前置 三] 引用者分母排除 `agent-test-artifacts*/`(和 docs 同待遇):那是只读历史 QA 证据,
#:   一字不改、不删(不修历史记录;开源导出按 E4 本来就排除)。
#:   🔴 保险:只要任一**运行者**引用了这些目录下的路径,就不许排除、判红 —— 运行者 = MUST_RUN / ONESHOT 登记、
#:   `.github/workflows/`、frontend build 链(package.json)、api/scheduler.py、仓根 *.sh / docker-compose*.yml / Makefile
#:   (Dockerfile / .dockerignore 不算:它们提这些目录是为了排除)。里面有 4 月的 _runner.py,要证明没人跑它们。
ARTIFACT_DIR_RE = re.compile(r"^agent-test-artifacts[^/]*/")
_ARTIFACT_DIR_WORD = "agent-test-artifacts"


def _artifact_runner_hits(tracked: list[str]) -> list[str]:
    """运行者面里提到 agent-test-artifacts 的地方(文件:行号);空 = 没人跑这些目录下的东西。"""
    surfaces = [MUST_RUN_FILE, ONESHOT_FILE, BUILD_PKG_FILE, "api/scheduler.py"]
    surfaces += [f for f in tracked if f.startswith(".github/workflows/")]
    surfaces += [f for f in tracked if "/" not in f and (f.endswith(".sh") or f == "Makefile"
                                                          or re.match(r"docker-compose[^/]*\.ya?ml$", f))]
    hits = []
    for f in sorted(set(surfaces)):
        text = read_file(f)
        if not text:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if _ARTIFACT_DIR_WORD in line:
                hits.append(f"{f}:{n}")
    return hits


def _referencers(keys: list[str]) -> dict[str, set[str]]:
    """一次 git grep 搜多个键(`-o` 取回命中的是哪个键),按键归并引用者。"""
    hits: dict[str, set[str]] = {k: set() for k in keys}
    prefix = f"{_GIT_REF}:" if _GIT_REF else ""
    for j in range(0, len(keys), 40):
        chunk = keys[j:j + 40]
        args = ["grep", "-I", "-o", "-w", "-F", "-z"]
        for k in chunk:
            args += ["-e", k]
        if _GIT_REF:
            args.append(_GIT_REF)
        args += ["--", ".", *_REF_SCOPE_EXCLUDES]
        rc, out = _git(*args)
        if rc not in (0, 1):          # 1 = 一个都没命中,不是出错
            raise RuntimeError(f"git grep rc={rc}")
        for line in out.decode("utf-8", errors="replace").splitlines():
            if "\0" not in line:
                continue
            name, rest = line.split("\0", 1)
            # -z 下是「文件名\0行号?\0命中」或「文件名\0命中」,取最后一段
            match = rest.split("\0")[-1]
            if name.startswith(prefix):
                name = name[len(prefix):]
            if match in hits:
                hits[match].add(name)
    return hits


def _is_pure_comment(path: str, text: str) -> bool:
    t = text.strip()
    if path.endswith(".sql"):
        return t.startswith("--")
    return path.endswith(".py") and t.startswith("#")


#: [Review 09-28 · B1b-2b] 白名单第二类 `e2_token_spec`:E2 社媒模块状态表 `_SOCIAL_MODULE_TOKEN_SPECS` 是拦运行期
#:   模块串的拒绝名单,表里指向已删路径是本意(不是陈引用)。只收这一个文件、这一张表之内的行;行形状恰为两个字符串常量的元组;
#:   元组里的路径必须已登进 E2 那格的 RETIRED_PATHS(两边对账)。表外(同文件或别的文件)出现同形状的行照样红。
E2_CONTRACT_FILE = "services/xiaobang_command_contract.py"
E2_TABLE_NAME = "_SOCIAL_MODULE_TOKEN_SPECS"
E2_RETIRED_TEST = "tests/oss_e2_dead_guard_2026_09_22/test_social_module_axis_is_live_or_retired.py"
E2_OK: set[tuple[str, str]] = set()


def _e2_table_range(src: str) -> tuple[int, int] | None:
    for node in ast.parse(src).body:
        tgt = (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else
               node.target if isinstance(node, ast.AnnAssign) else None)
        if isinstance(tgt, ast.Name) and tgt.id == E2_TABLE_NAME:
            return node.lineno, node.end_lineno
    return None


def _e2_retired_paths() -> set[str] | None:
    src = read_file(E2_RETIRED_TEST)
    if src is None:
        return None
    for node in ast.parse(src).body:
        tgt = (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else
               node.target if isinstance(node, ast.AnnAssign) else None)
        if isinstance(tgt, ast.Name) and tgt.id == "RETIRED_PATHS":
            try:
                return set(ast.literal_eval(node.value))
            except ValueError:
                return None
    return None


def _e2_entry_error(path: str, line: str) -> str | None:
    """e2_token_spec 条目的校验;合规返回 None。"""
    if path != E2_CONTRACT_FILE:
        return f"`{path}` 不是 {E2_CONTRACT_FILE} —— e2_token_spec 只收那一张表"
    try:
        val = ast.literal_eval(line.rstrip(",").strip())
    except (ValueError, SyntaxError):
        val = None
    if not (isinstance(val, tuple) and len(val) == 2 and all(isinstance(x, str) for x in val)):
        return "行形状不是「两个字符串常量的元组」"
    body = read_file(path)
    rng = _e2_table_range(body) if body is not None else None
    if rng is None:
        return f"{path} 里找不到 {E2_TABLE_NAME} 这张表"
    lines = body.splitlines()
    inside = [i for i in range(rng[0], rng[1] + 1) if lines[i - 1].strip() == line]
    outside = [i for i, x in enumerate(lines, 1) if x.strip() == line and not (rng[0] <= i <= rng[1])]
    if not inside:
        return f"这行不在 {E2_TABLE_NAME} 表内(或已改掉:陈条目)"
    if outside:
        return f"同一行在 {E2_TABLE_NAME} 表外也出现了(第 {outside[0]} 行)—— 表外同形状行不放行"
    retired = _e2_retired_paths()
    if retired is None:
        return f"读不出 {E2_RETIRED_TEST} 的 RETIRED_PATHS —— 无从对账"
    if val[1] not in retired:
        return f"表内路径 `{val[1]}` 没登进 E2 的 RETIRED_PATHS(两边对账不上)"
    return None


#: [Review 09-28 · B2 前置] 白名单第三类 `homonym`(同词不同物):门按词边界取删除键,在役代码里**同一个词指另一件事**
#:   (别的类的字段、配置键、能力 id、请求字段名)也会被判成残留引用。条件:文件 + 行原文钉死(行一改就不再放行);
#:   理由写明这个词在该行实际指什么;该行不得含本班任一被删文件的完整路径或所在包路径(点分 / 斜杠;包路径只认两段及以上,
#:   单段顶层目录名如 `tools` 太泛不作判据);不得是 import / from 行;读数单列。
HOMONYM_OK: set[tuple[str, str]] = set()
_IMPORT_LINE = re.compile(r"^\s*(from|import)\s")


def _path_forms(removed: list[str]) -> set[str]:
    forms: set[str] = set()
    for p in removed:
        mod = str(Path(p).with_suffix("")).replace("\\", "/")
        pkg = str(Path(p).parent).replace("\\", "/")
        forms |= {mod, mod.replace("/", ".")}
        if pkg.count("/") >= 1:
            forms |= {pkg, pkg.replace("/", ".")}
    return {f for f in forms if f and f != "."}


#: [Review 10-02 签 · WO_322] import 行收 homonym 的唯一条件:导入**来源**既不以被删键结尾、也不含任何被删路径形
#:   (`@/` 别名先归一成 `frontend/src/` 再比)。来由:营销顾问后台从同目录 `./ui` 导入同名空态组件的单行 import
#:   引的是同目录在役模块,与被删的同名社媒文件无关,旧规则一律拒 import 行 ⇒ 只能拆行躲闸。
#:   这条放宽依赖构建链兜底:来源若实际解析到被删模块,tsc / vite 构建失败。白名单仍逐行登记。
_IMPORT_SOURCE = re.compile(r"""\bfrom\s+['"]([^'"]+)['"]|\bimport\s*\(\s*['"]([^'"]+)['"]|^\s*import\s+['"]([^'"]+)['"]"""
                            r"""|^\s*from\s+([\w.]+)\s+import\b|^\s*import\s+([\w.]+)""")


def _import_source_error(line: str, forms: set[str], keys: set[str]) -> str | None:
    m = _IMPORT_SOURCE.search(line)
    src = next((g for g in m.groups() if g), None) if m else None
    if not src:
        return "import / from 行不收 homonym —— 读不出导入来源"
    norm = ("frontend/src/" + src[2:]) if src.startswith("@/") else src
    last = re.split(r"[/.]", norm.rstrip("/"))[-1] if "/" in norm else norm.rsplit(".", 1)[-1]
    last = re.sub(r"\.(tsx?|jsx?|mjs|cjs|py)$", "", last)
    if last.lower() in {k.lower() for k in keys}:
        return f"import / from 行不收 homonym —— 导入来源 `{src}` 以被删键 `{last}` 结尾,就是在引那个模块"
    for f in sorted(forms):
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(f) + r"(?![A-Za-z0-9_])", norm):
            return f"import / from 行不收 homonym —— 导入来源 `{src}` 含被删路径 `{f}`,就是在引那个模块"
    return None


def _homonym_entry_error(path: str, line: str, forms: set[str], keys: set[str] | None = None) -> str | None:
    """homonym 条目的校验;合规返回 None。"""
    if _IMPORT_LINE.match(line):
        err = _import_source_error(line, forms, keys or set())
        if err:
            return err
    body = read_file(path)
    if body is None or line not in {x.strip() for x in body.splitlines()}:
        return f"`{path}` 里已找不到这行原文(陈条目:该行改了就删条目)"
    for f in sorted(forms):
        if re.search(r"(?<![A-Za-z0-9_])" + re.escape(f) + r"(?![A-Za-z0-9_])", line):
            return f"这行含本班被删文件或其包的路径 `{f}` —— 那是真引用,不是同词不同物"
    return None


#: [Review 09-28 · 门五补] 白名单第四类 `forbid_list`(禁用名单):锁用来禁止已删模块回流的名单行
#:   (如上提锁里的 `SOCIAL = ("<已删包>", ...)`)点名已删路径是本意,属真引用、有用途。条件:文件在 tests/ 下;
#:   行原文钉死(改了就不再放行);行恰为「单个名字 = 字符串常量的元组 / 列表」(ast 判,行尾注释允许;import 行天然进不来);
#:   至少一个元素是本班被删文件或其包的路径(点分 / 斜杠);任何元素都不许指向待发树里仍在的模块 / 目录(禁在役模块不是禁用名单)。
FORBID_OK: set[tuple[str, str]] = set()


def _forbid_entry_error(path: str, line: str, forms: set[str]) -> str | None:
    """forbid_list 条目的校验;合规返回 None。"""
    if not path.startswith("tests/"):
        return f"`{path}` 不在 tests/ 下 —— 禁用名单只收锁里的名单行"
    try:
        tree = ast.parse(line)
    except SyntaxError:
        tree = None
    node = tree.body[0] if tree is not None and len(tree.body) == 1 else None
    if not (isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, (ast.Tuple, ast.List)) and node.value.elts
            and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in node.value.elts)):
        return "行形状不是「单个名字 = 字符串常量的元组 / 列表」"
    body = read_file(path)
    if body is None or line not in {x.strip() for x in body.splitlines()}:
        return f"`{path}` 里已找不到这行原文(陈条目:该行改了就删条目)"
    vals = [e.value for e in node.value.elts]
    if not any(v.rstrip("/.") in forms for v in vals):
        return "名单里没有一个元素是本班被删文件或其包的路径 —— 不需要放行"
    for v in vals:
        rel = v.rstrip("/.").replace(".", "/") if "/" not in v else v.rstrip("/")
        if read_file(rel + ".py") is not None or _git("cat-file", "-e", f"{_GIT_REF or 'HEAD'}:{rel}")[0] == 0:
            return f"名单元素 `{v}` 在待发树里仍在 —— 禁用在役模块不是禁用名单"
    return None


def _comment_allow(removed: list[str] | None = None) -> tuple[dict[str, set[str]], list[str]]:
    """读引用行白名单(待发一侧);返回 ({文件: {行原文}}, [违规说明])。违规条目不生效且判红。
    四栏:文件 · 类型(comment / e2_token_spec / homonym / forbid_list)· 行原文(去首尾空白)· 理由。"""
    allowed: dict[str, set[str]] = {}
    errs: list[str] = []
    E2_OK.clear()
    HOMONYM_OK.clear()
    FORBID_OK.clear()
    forms = _path_forms(removed or [])
    del_keys = {(Path(x).parent.name if Path(x).stem.lower() in ("index", "__init__", "main", "mod") else Path(x).stem)
                for x in (removed or [])}
    text = read_file(COMMENT_ALLOW_FILE)
    if text is None:
        return allowed, errs
    for n, raw in enumerate(text.splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        cols = raw.split("\t")
        if len(cols) != 4:
            errs.append(f"{COMMENT_ALLOW_FILE}:{n} 不是四栏(文件 / 类型 / 行原文 / 理由)")
            continue
        path, kind, line, why = (c.strip() for c in cols)
        if len(why) < 8:
            errs.append(f"{COMMENT_ALLOW_FILE}:{n} 理由太短")
            continue
        if kind == "e2_token_spec":
            err = _e2_entry_error(path, line)
            if err:
                errs.append(f"{COMMENT_ALLOW_FILE}:{n} {err}")
                continue
            E2_OK.add((path, line))
            allowed.setdefault(path, set()).add(line)
            continue
        if kind == "homonym":
            err = _homonym_entry_error(path, line, forms, del_keys)
            if err:
                errs.append(f"{COMMENT_ALLOW_FILE}:{n} {err}")
                continue
            HOMONYM_OK.add((path, line))
            allowed.setdefault(path, set()).add(line)
            continue
        if kind == "forbid_list":
            err = _forbid_entry_error(path, line, forms)
            if err:
                errs.append(f"{COMMENT_ALLOW_FILE}:{n} {err}")
                continue
            FORBID_OK.add((path, line))
            allowed.setdefault(path, set()).add(line)
            continue
        if kind != "comment":
            errs.append(f"{COMMENT_ALLOW_FILE}:{n} 类型 `{kind}` 不认识(只有 comment / e2_token_spec / homonym / forbid_list)")
            continue
        if not (path in PROTECTED_FILES or path.endswith(".sql")):
            errs.append(f"{COMMENT_ALLOW_FILE}:{n} `{path}` 不是保护文件也不是 .sql —— 白名单只收这两类的注释行")
            continue
        if not _is_pure_comment(path, line):
            errs.append(f"{COMMENT_ALLOW_FILE}:{n} `{path}` 那行不是纯注释(.py 须以 # 开头、.sql 须以 -- 开头)")
            continue
        body = read_file(path)
        if body is None or line not in {x.strip() for x in body.splitlines()}:
            errs.append(f"{COMMENT_ALLOW_FILE}:{n} `{path}` 里已找不到这行原文(陈条目:该行改了就删条目)")
            continue
        allowed.setdefault(path, set()).add(line)
    return allowed, errs


def _key_lines(ref: str | None, key: str, files: list[str]) -> dict[str, list[str]]:
    """每个文件里**提到 key 的那几行**原文(ref=None ⇒ 读工作树)。

    [WO_266 · Review 09-23 裁定 ②] ⑦ 的放行条件原是「本班动过这个文件」——
    文件别处改一行也算看过,太宽。现在「动过」要落在**引用行本身**:
    待发一侧每一行引用,都不许在基准一侧原样存在(按行内容多重集比)。
    """
    out: dict[str, list[str]] = {}
    for j in range(0, len(files), 50):
        chunk = files[j:j + 50]
        args = ["grep", "-I", "-w", "-F", "-z", "-e", key]
        if ref:
            args.append(ref)
        args += ["--", *chunk]
        rc, raw = _git(*args)
        if rc not in (0, 1):
            raise RuntimeError(f"git grep(引用行)rc={rc}")
        prefix = f"{ref}:" if ref else ""
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if "\0" not in line:
                continue
            name, content = line.split("\0", 1)
            if name.startswith(prefix):
                name = name[len(prefix):]
            out.setdefault(name, []).append(content.rstrip())
    return out


def _wo266_cells(base_ref: str, items: list[str], cls: dict[str, tuple[str, str]],
                 sels: list[str], must: dict[str, str], oneshot: dict[str, str],
                 retired: dict[str, str], frozen: dict[str, str]) -> int:
    """WO_266 八格。返回 0 / 1 / 3(3 = 需要基准的那几格没跑成)。"""
    print("  —— WO_266:随单登记格式 · 预算棘轮 · 冻结名单与运行者守恒 · 退役接替 · 删除引用者 · 必跑集预算 ——")
    red: list[str] = []
    cant: list[str] = []

    # ① 随单登记格式(不需要基准)
    os_text = read_file(ONESHOT_FILE)
    if os_text is None:
        red.append(f"读不到 {ONESHOT_FILE} —— 随单登记表缺失")
        os_text = ""
    else:
        fmt = _oneshot_entry_errors(os_text)
        red.extend(fmt)
        if not fmt:
            print(f"  ✅ 随单登记 {len(_entry_lines(os_text))} 条,每条都写了「守什么」")

    # ② 预算行(不需要基准)
    bud = _budget_values(os_text)
    budget = bud[0] if len(bud) == 1 else None
    if not bud:
        red.append(f"{ONESHOT_FILE} 没有预算行(要恰好一行 `# ONESHOT_BUDGET_SECONDS=<正整数>`,"
                   "整行、无空白、无前导零)—— 没有预算,G2b 无从判超时")
    elif len(bud) > 1:
        red.append(f"{ONESHOT_FILE} 有 {len(bud)} 行预算({bud})—— 歧义,只许一行")

    # ⑧ 必跑集预算行(Review 09-23 裁定 ①;形状这一半不需要基准)
    mr_text = read_file(MUST_RUN_FILE) or ""
    mbud = _budget_values(mr_text, MUST_RUN_BUDGET_LINE_RE)
    mr_budget = mbud[0] if len(mbud) == 1 else None
    if not mbud:
        red.append(f"{MUST_RUN_FILE} 没有预算行(要恰好一行 `# MUST_RUN_BUDGET_SECONDS=<正整数>`,"
                   "整行、无空白、无前导零)—— 没有预算,G2b 无从判必跑集超时")
    elif len(mbud) > 1:
        red.append(f"{MUST_RUN_FILE} 有 {len(mbud)} 行预算({mbud})—— 歧义,只许一行")

    # ③④⑤⑦⑧ 需要基准
    base, why = _resolve_base(base_ref)
    if base is None:
        cant.append(f"{why} ⇒ 预算棘轮 / 冻结名单增量 / 运行者守恒三格无从比较")
    else:
        tag = f"基准 {base[:12]}(= 待发一侧与 {base_ref} 的 merge-base)"
        b_os = _read_at(base, ONESHOT_FILE)
        b_bud = _budget_values(b_os) if b_os is not None else []
        if budget is not None:
            if len(b_bud) != 1:
                print(f"  ⚪ 预算棘轮无基准:{tag} 的随单登记表还没有预算行"
                      f"(本班引入预算 {budget}s;上主干后本格转真比)")
            elif budget > b_bud[0]:
                red.append(
                    f"时长预算被改大:{b_bud[0]}s → {budget}s({tag})。预算**只许降**。"
                    "超预算的处置只能三选一并写回登记表:① 提进 tests/MUST_RUN.txt "
                    "② 肯定式退役(tests/RETIRED_TESTS.txt 写明接替者)③ 拆包")
            else:
                note = "未改" if budget == b_bud[0] else f"下调 {b_bud[0]}s → {budget}s"
                print(f"  ✅ 时长预算 {budget}s({note};{tag})")

        # ⑧ 必跑集预算:可上调,但只能是「单独一笔、只改这一行」的提交,且签字条点名
        b_mbud = _budget_values(_read_at(base, MUST_RUN_FILE) or "", MUST_RUN_BUDGET_LINE_RE)
        if mr_budget is not None:
            if len(b_mbud) != 1:
                print(f"  ⚪ 必跑集预算无基准:{tag} 的 {MUST_RUN_FILE} 还没有预算行"
                      f"(本班引入 {mr_budget}s;上主干后本格转真比)")
            elif mr_budget > b_mbud[0]:
                rcs = _raising_commits(base, MUST_RUN_FILE, MUST_RUN_BUDGET_LINE_RE)
                head_mbud = (None if _GIT_REF else
                             _budget_values(_read_at("HEAD", MUST_RUN_FILE) or "",
                                            MUST_RUN_BUDGET_LINE_RE))
                if rcs is None:
                    cant.append(f"取不到 {tag} 以来改过 {MUST_RUN_FILE} 的提交 ⇒ 必跑集预算上调无从核")
                elif head_mbud is not None and head_mbud != [mr_budget]:
                    red.append(f"必跑集预算上调 {b_mbud[0]}s → {mr_budget}s 是**未提交**的改动 —— "
                               "调预算只能是单独一笔、只改这一行的提交(Review 裁定 ①)")
                elif rcs[1] or not rcs[0]:
                    bad = ", ".join(c[:12] for c in rcs[1]) or "(找不到做这次上调的单独一笔)"
                    red.append(f"必跑集预算上调 {b_mbud[0]}s → {mr_budget}s 没有走「单独一笔、只改这一行」:"
                               f"{bad} —— 超预算的处置:拆包 / 肯定式退役 / 单独一笔调预算并由签字条点名")
                else:
                    print(f"  ⚠️  必跑集预算上调 {b_mbud[0]}s → {mr_budget}s:单独一笔 "
                          + ", ".join(c[:12] for c in rcs[0])
                          + " ✅ —— 签字条必须点名这一笔(Review 裁定 ①;本行是绿路径上的提醒,不用红标)")
            else:
                note = "未改" if mr_budget == b_mbud[0] else f"下调 {b_mbud[0]}s → {mr_budget}s"
                print(f"  ✅ 必跑集预算 {mr_budget}s({note};{tag})")

        b_fz_text = _read_at(base, FROZEN_FILE)
        if b_fz_text is None:
            print(f"  ⚪ 冻结名单增量无基准:{tag} 没有 {FROZEN_FILE}")
        else:
            b_fz = _parse_registry(b_fz_text)
            grown = sorted(p for p in frozen if p not in b_fz)
            if grown:
                red.append(
                    f"冻结名单比基准多了 {len(grown)} 项:{', '.join(grown[:6])}"
                    f"{'…' if len(grown) > 6 else ''} —— 冻结名单只许减。"
                    "没有运行者的包要登记(MUST_RUN / 随单 / 肯定式退役),不能塞进冻结名单")
            else:
                print(f"  ✅ 冻结名单相对基准新增 0 项({len(b_fz)} → {len(frozen)})")

        b_reg: dict[str, str] = {}
        for rel, label in ((MUST_RUN_FILE, "MUST_RUN"), (ONESHOT_FILE, "ONESHOT")):
            t = _read_at(base, rel)
            for p in (_parse_registry(t) if t is not None else {}):
                b_reg.setdefault(p, label)
        lost = []
        for p, label in sorted(b_reg.items()):
            if p in must or p in oneshot or p in retired:
                continue
            if workflow_covered(p, sels):
                continue
            lost.append(f"`{p}`(基准登记于 {label})")
        if lost:
            for x in lost:
                red.append(f"运行者守恒:{x} 在待发树里既没有运行者也没有退役登记 —— "
                           "删登记行 / 挪进冻结名单都不是合法处置,唯一出口是肯定式退役")
        else:
            print(f"  ✅ 运行者守恒:基准登记 {len(b_reg)} 项,待发树里都仍有运行者或已肯定式退役")

        # ⑦ 本班删掉 / 改名掉的文件:全仓引用者(含 tests/)必须本班都看过
        rem = _removals(base)
        if rem is None:
            cant.append(f"取不到 {tag} 到待发一侧的差分 ⇒ 删除引用者一格无从判断")
        else:
            removed, moved, changed = rem
            keyed = {p: _removal_key(p) for p in removed}
            weak = sorted(p for p, k in keyed.items() if k is None)
            #: [门五] 泛名键改按完整路径判:(被删路径, 路径键, 裸名适用的包目录, 裸名)
            generic: list[tuple[str, list[str], str | None, list[str]]] = []
            # [Review 09-28 · B2 前置] 撞名的键换成完整路径键(见 _collision_keys);其余照旧一文件一键
            pairs: list[tuple[str, str]] = []
            collided: list[tuple[str, str, list[str], list[str], str | None]] = []
            #: 键 → 只在这些目录下判(撞名但包还在时的裸名兄弟规则);None = 全仓判
            key_scope: dict[str, set[str] | None] = {}
            try:
                tracked = _tracked_files() if keyed else []
            except RuntimeError as e:
                tracked = None
                cant.append(f"删除引用者·撞名判定:{e}")
            for p, k in keyed.items():
                if not k:
                    gk = _generic_path_keys(p, tracked, set(removed)) if tracked is not None else None
                    if gk:
                        weak.remove(p)
                        generic.append((p, gk[0], gk[1], gk[2]))
                        for kk in gk[0]:
                            pairs.append((p, kk))
                            key_scope[kk] = None
                        if gk[1]:
                            for rk in gk[2]:
                                pairs.append((p, rk))
                                if key_scope.get(rk, ()) is not None:
                                    key_scope.setdefault(rk, set()).add(gk[1])
                    continue
                ck = _collision_keys(p, k, tracked, set(removed)) if tracked is not None else None
                if ck:
                    collided.append((p, k, ck[0], ck[1], ck[2]))
                    for kk in ck[0]:
                        pairs.append((p, kk))
                        key_scope[kk] = None
                    if ck[2]:
                        pairs.append((p, k))
                        sc = key_scope.setdefault(k, set())
                        if sc is not None:
                            sc.add(ck[2])
                else:
                    pairs.append((p, k))
                    key_scope[k] = None
            keys = sorted({k for _, k in pairs})

            def _in_scope(k: str, f: str) -> bool:
                sc = key_scope.get(k)
                return sc is None or any(f.startswith(d + "/") for d in sc)
            try:
                refs = _referencers(keys) if keys else {}
            except RuntimeError as e:
                refs = None
                cant.append(f"删除引用者一格:{e}")
            art_note = ""
            if refs:
                # [Review 09-28 · B2 前置 三] 历史证据目录不进分母 —— 先过保险:有运行者引用这些目录就不排除、判红
                art_runners = _artifact_runner_hits(tracked if tracked is not None else _tracked_files())
                art_refs = {k: {f for f in fs if ARTIFACT_DIR_RE.match(f)} for k, fs in refs.items()}
                n_art_pairs = sum(len(v) for v in art_refs.values())
                if art_runners:
                    red.append("删除引用者·历史证据目录:运行者引用了 agent-test-artifacts* 下的路径("
                               + ", ".join(art_runners[:4]) + ("…" if len(art_runners) > 4 else "")
                               + ")⇒ 这些目录不再是只读证据,不排除;先让运行者别再跑它们")
                elif n_art_pairs:
                    for k in refs:
                        refs[k] = refs[k] - art_refs[k]
                    art_files = sorted(set().union(*art_refs.values()))
                    art_note = (f"  ⚪ 删除引用者·历史证据不判:agent-test-artifacts* 下 {len(art_files)} 个文件"
                                f"({n_art_pairs} 处 文件×键)提到本班被删键,按只读历史证据不计(E4 导出排除;运行者 0 引用)")
            if refs is not None:
                n_bad = 0
                if art_note:
                    print(art_note)
                allow, allow_errs = _comment_allow(removed)
                for x in allow_errs:
                    n_bad += 1
                    red.append(f"删除引用者·注释行白名单:{x}")
                allowed_hits: list[str] = []
                homonym_hits: set[str] = set()
                flagged: dict[str, set[str]] = {}

                def _all_allowed(fname: str, lines: list[str]) -> bool:
                    ok = allow.get(fname, set())
                    res = bool(lines) and all(
                        ln.strip() in ok and (_is_pure_comment(fname, ln) or (fname, ln.strip()) in E2_OK
                                              or (fname, ln.strip()) in HOMONYM_OK
                                              or (fname, ln.strip()) in FORBID_OK)
                        for ln in lines)
                    if res:
                        homonym_hits.update(f"{fname}:「{ln.strip()[:40]}」" for ln in lines
                                            if (fname, ln.strip()) in HOMONYM_OK)
                    return res

                for p, k in sorted(pairs):
                    # 不给本脚本开后门:它的注释里若还点着一个被删的文件,同样该在本班改掉
                    stale = sorted(f for f in refs.get(k, set()) if f not in changed and _in_scope(k, f))
                    if stale and allow:
                        cand = [f for f in stale if f in allow]
                        if cand:
                            try:
                                cl = _key_lines(_GIT_REF or None, k, cand)
                            except RuntimeError as ex:
                                cant.append(f"删除引用者·白名单比对:{ex}")
                                cl = {}
                            for f in cand:
                                if _all_allowed(f, cl.get(f, [])):
                                    stale.remove(f)
                                    allowed_hits.append(f"{f}(`{k}`)")
                    if stale:
                        n_bad += 1
                        flagged.setdefault(k, set()).update(stale)
                        red.append(
                            f"删了 `{p}`,还有 {len(stale)} 个文件提到 `{k}` 而本班没动它们:"
                            + ", ".join(stale[:8]) + ("…" if len(stale) > 8 else "")
                            + " —— 退役一个文件,分母是全仓引用者(含 tests/):"
                              "逐个改指向、换成肯定式退役锁,或连同删掉")
                    # [Review 09-23 裁定 ②] 本班动过的引用者:「动过」要落在引用行本身。
                    #   文件别处改一行不算 —— 提到 k 的每一行都不许在基准一侧原样存在。
                    touched = sorted(f for f in refs.get(k, set()) if f in changed and _in_scope(k, f))
                    if touched:
                        try:
                            now_l = _key_lines(_GIT_REF or None, k, touched)
                            old_l = _key_lines(base, k, touched)
                        except RuntimeError as e:
                            cant.append(f"删除引用者·引用行比对:{e}")
                            continue
                        for f in touched:
                            same = Counter(now_l.get(f, [])) & Counter(old_l.get(f, []))
                            if same and f in allow:
                                same = Counter({ln: c for ln, c in same.items()
                                                if not _all_allowed(f, [ln])})
                                if not same:
                                    allowed_hits.append(f"{f}(`{k}`)")
                            if same:
                                n_bad += 1
                                flagged.setdefault(k, set()).add(f)
                                first = next(iter(same)).strip()
                                red.append(
                                    f"删了 `{p}`,`{f}` 本班动过,但提到 `{k}` 的 {sum(same.values())} 行"
                                    f"原样没动(例:「{first[:60]}」)—— 「动过」要落在引用行本身:"
                                    "改指向、换成肯定式退役锁,或删掉这几行")
                # [Review 09-28 二次 · E3a r3] 上面只核「基准里原有的引用行变没变」,**本班新冒出来的引用**
                #   (同文件新加一行、别的文件新加)看不见。补一道全树扫描:待发树里(不含 tests/;docs/、*.md、*.txt
                #   与 ⑦ 同口径不进分母)本班每个删除键的每一处命中,都必须恰是白名单里的纯注释行,否则红。
                #   tests/ 不查:同班写的肯定式退役锁本来就要点名被删文件。
                for k in keys:
                    hit_files = sorted(x for x in refs.get(k, set())
                                       if not x.startswith("tests/") and x not in flagged.get(k, set())
                                       and _in_scope(k, x))
                    if not hit_files:
                        continue
                    try:
                        hl = _key_lines(_GIT_REF or None, k, hit_files)
                    except RuntimeError as ex:
                        cant.append(f"删除引用者·全树扫描:{ex}")
                        continue
                    for x in hit_files:
                        bad = [ln.strip() for ln in hl.get(x, []) if not _all_allowed(x, [ln])]
                        if bad:
                            n_bad += 1
                            red.append(
                                f"删了 `{k}` 那个文件,待发树里 `{x}` 仍有 {len(bad)} 处提到它且不是白名单注释行"
                                f"(例:「{bad[0][:60]}」)—— 新写的引用同样要改掉;只有保护文件 # 注释 / .sql -- 注释"
                                f"可进 {COMMENT_ALLOW_FILE}")
                if allowed_hits:
                    print(f"  ⚠️  删除引用者·注释行白名单放行 {len(allowed_hits)} 处:"
                          + ", ".join(allowed_hits[:6]) + ("…" if len(allowed_hits) > 6 else "")
                          + f"(见 {COMMENT_ALLOW_FILE};签字条点名)")
                if n_bad == 0:
                    if not removed and not moved:
                        print("  ⚪ 删除引用者:本班没有删除或改名的文件(无对象)")
                    elif not keys:
                        # 🔴 一个键都没搜 ≠ 干净。不许打 ✅。
                        print(f"  ⚪ 删除引用者:本班删/改名 {len(removed)} 个、同名搬家 "
                              f"{len(moved)} 个,一个键都没搜(见下方点名)—— 这不是「干净」")
                    else:
                        print(f"  ✅ 删除引用者:本班删/改名 {len(removed)} 个文件,"
                              f"按文件名搜了 {len(keys)} 个键,引用者都动过、引用行本身都变了")
                for p, k, ck, live, sib in collided:
                    print(f"  ⚠️  删除引用者·撞名改判:`{p}` 的文件名键 `{k}` 在待发树里还有同名在役文件 "
                          + ", ".join(live[:3]) + ("…" if len(live) > 3 else "")
                          + " —— 改按完整路径键判:" + " / ".join(f"`{x}`" for x in ck)
                          + (f";包还在,`{sib}/` 下的兄弟文件仍按裸名 `{k}` 判(相对导入)" if sib else ""))
                for p, gks, sib, bare in generic:
                    print(f"  ⚠️  删除引用者·泛名改判:`{p}` 的文件名太泛,不按名搜 —— 改按完整路径键判:"
                          + " / ".join(f"`{x}`" for x in gks)
                          + (f";包还在,`{sib}/` 下的兄弟文件再按相对导入形 " + " / ".join(f"`{x}`" for x in bare) + " 判"
                             if sib else ""))
                if homonym_hits:
                    print(f"  ⚠️  删除引用者·同词不同物放行 {len(homonym_hits)} 处:"
                          + ", ".join(sorted(homonym_hits)[:6]) + ("…" if len(homonym_hits) > 6 else "")
                          + f"(见 {COMMENT_ALLOW_FILE} homonym;签字条点名)")
                if weak:
                    print(f"  ⚠️  删除引用者·没搜(文件名太泛,按名搜只会满屏误报):{len(weak)} 个 —— "
                          + ", ".join(weak[:6]) + ("…" if len(weak) > 6 else "")
                          + "(这几个的引用者要人工看)")
                if moved:
                    print(f"  ⚠️  删除引用者·没判(只搬目录不改名,按文件名分不出新旧引用):"
                          f"{len(moved)} 个 —— " + ", ".join(f"{a}→{b}" for a, b in moved[:4])
                          + ("…" if len(moved) > 4 else ""))

    # ⑥ 退役接替(不需要基准)
    rt_text = read_file(RETIRED_FILE) or ""
    rows, rerrs = _retired_entries(rt_text)
    red.extend(rerrs)
    n_red_before = len(red)
    steps: list[str] | None = None
    for path, succ_full in rows:
        if succ_full == path:
            red.append(f"退役 `{path}` 的接替者写的是它自己")
            continue
        # [WO_297] 接替者与第 1 栏对称,支持 `文件::格`(含 `文件::类::方法`、参数化后缀 `[…]`):
        #   文件必须存在;格必须在该文件里有定义;运行者按文件所属顶层项判。
        #   之前整串当路径找 ⇒ 格级接替者一律「不存在」(AD 预览尖上 C 的 brand_row 那条就是这么红的)。
        succ, _, node = succ_full.partition("::")
        if not _exists_now(succ):
            red.append(f"退役 `{path}` 的接替者 `{succ_full}` 在待发树里不存在"
                       + ("(文件本身就不在)" if node else ""))
            continue
        if node:
            defined = _defined_nodes(read_file(succ))
            want = re.sub(r"\[.*\]$", "", node)
            if defined is None:
                red.append(f"退役 `{path}` 的接替者文件 `{succ}` 解析不了,判不了格 `{want}` 在不在")
                continue
            if want not in defined:
                red.append(f"退役 `{path}` 的接替者格 `{want}` 在 `{succ}` 里不存在"
                           "(没有这个顶层 def,也没有这个 类::方法)")
                continue
        if succ.startswith("tests/"):
            top = "tests/" + succ[len("tests/"):].split("/", 1)[0]
            c = cls.get(top, ("(不在普查面内)", ""))[0]
            if c not in RUNNER_CLASSES:
                red.append(f"退役 `{path}` 的接替者 `{succ}` 自己没有运行者({top} 属 {c})"
                           " ⇒ 这次退役等于删判据")
            elif c == "WORKFLOW_PARTIAL" and not workflow_covered(succ, sels):
                red.append(f"退役 `{path}` 的接替者 `{succ}` 落在只被部分覆盖的 {top} 里,"
                           "且它自己没被任何选择器命中 ⇒ 没有运行者")
        else:
            # [WO_289] tests/ 外的接替者:逐字出现在 build 链某一步 ⇒ 每次 bake 都跑它
            if steps is None:
                steps, why = _build_chain_steps()
                if steps is None:
                    cant.append(f"退役接替者在 tests/ 之外,要查 build 链,但 {why}")
                    steps = []
            n = _build_step_of(succ, steps)
            if n:
                print(f"  ✅ 退役 `{path}` 的接替者 `{succ}` 由 build 链第 {n} 步跑(bake)")
                continue
            print(f"  ⚠️  退役 `{path}` 的接替者 `{succ}` 在 tests/ 之外、也不在 build 链上 "
                  "—— 它的运行者不在本普查面内,请在原因栏写明哪一关跑它")
    if not rows and not rerrs:
        print("  ⚪ 退役登记 0 条(无对象)")
    elif rows and not rerrs and len(red) == n_red_before:
        print(f"  ✅ 退役登记 {len(rows)} 条,接替者都指得出")

    for x in red:
        print(f"  🔴 {x}")
    for x in cant:
        print(f"  🔴 本门【没跑成·rc=3】:{x} —— 没跑 != 没违规")
    if red:
        print(f"  🔴 WO_266 格共 {len(red)} 条")
    if cant:
        return 3
    return 1 if red else 0


def workflow_selectors() -> list[str]:
    """机械解析 `.github/workflows/*.yml` 里 `pytest` 后面的路径参数。

    🔴 只认**路径样**的参数:`-v` `--no-header` `-m "not live"` 这些不是选择器。
    解析不到任何选择器 ⇒ 视为解析失败(rc=3),不是「没有 workflow 跑测试」。
    """
    if _GIT_REF:
        rc, out = _git("ls-tree", "-r", "--name-only", "-z", _GIT_REF,
                       ".github/workflows/")
    else:
        rc, out = _git("ls-tree", "-r", "--name-only", "-z", "HEAD",
                       ".github/workflows/")
    if rc != 0:
        return []
    sels: list[str] = []
    for b in out.split(b"\0"):
        if not b:
            continue
        rel = b.decode("utf-8")
        text = read_file(rel)
        if not text:
            continue
        for m in re.finditer(r"^\s*(?:-\s*)?pytest\s+(.+)$", text, re.MULTILINE):
            for tok in m.group(1).split():
                if tok.startswith("-"):
                    continue
                if "/" in tok or tok.endswith(".py"):
                    sels.append(tok)
    return sels


def workflow_covered(item: str, sels: list[str]) -> str | None:
    """item 形如 `tests/foo` 或 `tests/foo.py`。返回命中的选择器或 None。"""
    for s in sels:
        s_norm = s.rstrip("/")
        if item == s_norm:
            return s
        # 目录选择器覆盖其下所有内容;item 是顶层项,所以只有相等或前缀两种
        if item.startswith(s_norm + "/"):
            return s
        if "*" in s_norm and fnmatch.fnmatch(item, s_norm):
            return s
        # `tests/api/test_research_monitor_*.py` 覆盖的是 tests/api 里的文件,
        # 顶层项 `tests/api` 因此**部分**被覆盖 —— 记为命中,并在报表里标注。
        if s_norm.startswith(item + "/"):
            return s + "(部分)"
    return None


def _selftest(sels: list[str]) -> bool:
    if not sels:
        print("  🔴 自校准失败:一个 workflow 选择器都解析不到")
        return False
    if workflow_covered("tests/__nope_zzz__", sels) is not None:
        print("  🔴 自校准失败:不存在的项也被判成被 workflow 覆盖")
        return False
    probe = ["tests/foo/"]
    if workflow_covered("tests/foo", probe) is None:
        print("  🔴 自校准失败:目录选择器匹配不上同名顶层项")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_body(argv)
    finally:
        _close_batch()


def _main_body(argv: list[str] | None = None) -> int:
    global _GIT_REPO, _GIT_REF
    _GIT_CACHE.clear()
    _close_batch()
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--repo", default="")
    ap.add_argument("--ref", default="")
    ap.add_argument("--print-unassigned", action="store_true",
                    help="打印当前无运行者清单(用于首次冻结)")
    # 🔴 [WO_266] preflight 5-d **不传**这个参数(装机版 5-d 的调用一字未改),
    #    所以默认值必须就是 preflight 要的那个:github/main。
    ap.add_argument("--base", default=DEFAULT_BASE_REF,
                    help="棘轮基准 ref(实际比的是它与待发一侧的 merge-base)")
    a = ap.parse_args(argv)
    if bool(a.repo) != bool(a.ref):
        print("  🔴 本门【没跑成·rc=3】:--repo 与 --ref 必须成对 —— 没跑 != 没违规")
        return 3
    _GIT_REPO, _GIT_REF = a.repo, a.ref

    items = tests_top_level()
    if len(items) < 50:
        print(f"  🔴 本门【没跑成·rc=3】:tests/ 顶层只枚举到 {len(items)} 项 "
              "—— 分母塌了,读数不可信")
        return 3

    sels = workflow_selectors()
    if not _selftest(sels):
        print("  🔴 本门【没跑成·rc=3】:尺子自校准不过 ⇒ 结论作废")
        return 3

    must = _registry(MUST_RUN_FILE)
    oneshot = _registry(ONESHOT_FILE)
    retired = _registry(RETIRED_FILE)
    frozen = _registry(FROZEN_FILE)

    cls: dict[str, tuple[str, str]] = {}
    for it in items:
        if it in must:
            cls[it] = ("MUST_RUN", must[it])
            continue
        hit = workflow_covered(it, sels)
        if hit:
            # 🔴 「部分覆盖」不许读成「覆盖」。`tests/api` 17 个 .py 里只有 11 个
            #    被 `test_research_monitor_*.py` 命中,另外 6 个没人跑 ——
            #    把整个目录记成 WORKFLOW,等于把那 6 个藏进绿里。
            cls[it] = ("WORKFLOW_PARTIAL" if hit.endswith("(部分)")
                       else "WORKFLOW", hit)
            continue
        if it in oneshot:
            cls[it] = ("ONESHOT", oneshot[it])
            continue
        if it in retired:
            cls[it] = ("RETIRED", retired[it])
            continue
        cls[it] = ("UNASSIGNED", "")

    unassigned = sorted(k for k, (c, _) in cls.items() if c == "UNASSIGNED")

    if a.print_unassigned:
        for u in unassigned:
            print(u)
        return 0

    counts = {c: sum(1 for v in cls.values() if v[0] == c)
              for c in ("MUST_RUN", "WORKFLOW", "WORKFLOW_PARTIAL",
                        "ONESHOT", "RETIRED", "UNASSIGNED")}
    print(f"  tests/ 顶层 {len(items)} 项 · "
          + " · ".join(f"{k} {v}" for k, v in counts.items()))

    # 🔴 部分覆盖的目录:把**没被选择器命中**的文件逐个点名,别让它们混进绿里。
    # [WO_296] 这些文件可以按 WO_263 四类**逐文件**登记(必跑 / 随单 / 退役 / 冻结写原因);
    #   登记了的按类点名,只有四类都不在的才留在 ⚠️ 里。文件级登记只认部分覆盖目录里的文件
    #   (下面「陈账」格同口径),别处的子路径登记仍按陈账红。
    partial_files: set[str] = set()
    for it, (c, why) in sorted(cls.items()):
        if c != "WORKFLOW_PARTIAL":
            continue
        rc, out = (_git("ls-tree", "-r", "--name-only", "-z",
                        _GIT_REF or "HEAD", it + "/"))
        inner = [b.decode("utf-8") for b in out.split(b"\0")
                 if b and b.decode("utf-8").endswith(".py")]
        uncovered = [f for f in inner if workflow_covered(f, sels) is None]
        partial_files.update(uncovered)
        placed = {f: lab for f in uncovered for lab, tbl in
                  (("MUST_RUN", must), ("ONESHOT", oneshot), ("RETIRED", retired), ("FROZEN", frozen))
                  if f in tbl}
        orphan = [f for f in uncovered if f not in placed]
        tally = Counter(placed.values())
        head = (f"{it} 只被**部分**覆盖({why}):{len(inner)} 个 .py 里 {len(inner) - len(uncovered)} 个被 workflow 跑,"
                f"{len(uncovered)} 个不在选择器里 —— 逐文件登记 "
                + (" · ".join(f"{k} {v}" for k, v in sorted(tally.items())) or "0"))
        if orphan:
            print(f"  ⚠️  {head} · **{len(orphan)} 个四类都不在**")
            for f in orphan:
                print(f"        · {f}")
        else:
            print(f"  ✅ {head} · 没有落空的")
        for f, lab in sorted(placed.items()):
            print(f"        ✓ {f} → {lab}")

    fails: list[str] = []

    # 登记了却不存在的路径(陈账)—— 三张表都查
    for label, table in (("MUST_RUN", must), ("ONESHOT", oneshot),
                         ("RETIRED", retired), ("FROZEN", frozen)):
        for p in table:
            if p not in items and label != "RETIRED" and p not in partial_files:
                fails.append(f"{label} 登记了 `{p}`,但 tests/ 顶层没有这一项(陈账)"
                             + ("" if "/" not in p[len("tests/"):] else
                                " —— 文件级登记只认部分覆盖目录里、不在 workflow 选择器中的文件"))

    # 🔴 冻结名单只许减不许增
    new_unassigned = [u for u in unassigned if u not in frozen]
    if new_unassigned:
        for u in new_unassigned:
            fails.append(f"新增测试包**没登记运行者**:{u}")
    # 部分覆盖目录里按文件登记的冻结项(WO_296)本来就不在顶层 unassigned 里 —— 仍未被选择器命中就不算「该缩」
    gone = [f for f in frozen if f not in unassigned and f not in partial_files]

    if gone:
        # [Review 09-28 · 门四] 原来这里只打印一行 🔴、不计红 ⇒ 退出码 0:单看退出码的人被放过(A 24f121aa6 就是这么漏的)。
        #   该缩没缩是真红:进 fails。门的全部输出里「有 🔴 ⇒ 退出码非 0」由门自测的合成仓 helper 在每一次调用上核。
        fails.append(f"冻结名单该缩没缩:{FROZEN_FILE} 里还登记着 {len(gone)} 项已有运行者或已删的("
                     + ", ".join(gone[:5]) + ("…" if len(gone) > 5 else "")
                     + ")—— 请同步删掉这些行,名单只许减,减了要落到文件里")

    # 🔴 [WO_266] 既有判据的打印与计数**逐字不动**;唯一的改变是红了之后
    #    不再当场 return,而是接着跑 WO_266 八格 —— 否则「既有格一红」就会把
    #    新格整段藏起来,两类问题要分两班才看得全。
    if fails:
        for f in fails:
            print(f"  🔴 {f}")
        print(f"  🔴 共 {len(fails)} 条")
        rc_old = 1
    else:
        print(f"  ✅ 无未登记的新项;冻结名单 {len(frozen)} 项(无运行者,只许减)")
        rc_old = 0

    rc_new = _wo266_cells(a.base, items, cls, sels, must, oneshot, retired, frozen)
    # 3 压过 1:需要基准的格没跑成时,整份读数不完整,不许以「红/绿」的面目出现
    if rc_new == 3:
        return 3
    return max(rc_old, rc_new)


if __name__ == "__main__":
    raise SystemExit(main())
