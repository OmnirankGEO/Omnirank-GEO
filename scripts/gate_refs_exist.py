#!/usr/bin/env python3
"""WO_261 ③ · 元锁:门禁脚本引用的仓内路径必须存在(三态)。

## 为什么要这一把
门禁自己会烂。它引用的文件被改名/删掉之后,多数门禁**不报错**,只是那一条
永远不命中 —— 与 OSS_12 §2.8 那条社媒死锁同病。E3 要删一大片域,
这类「守着已不存在之物」的门会成批出现,而它们**看起来全绿**。

## 🔴 天真实现会打错人(这是本门最要紧的一段)
「所有被引用的路径都必须存在」听着对,实测会把**正确的退役写法判红**:

    ok(!existsSync(join(ROOT, 'src/pages/Diagnosis/launch/QuestionExampleCard.tsx')),
       'F1 🔴 示例卡与其模块已删');

这是**肯定式退役** —— 它断言那个路径**不该存在**。要求它存在,等于逼着人
把这种断言删掉,而那正是让退役可验证的东西。实测基线里 17 条「引用了但不存在」
**一条真陈旧引用都没有**:全是否定断言、人造探针、或注释里的历史记录。

⇒ 本门只对「**断言存在**」的引用要求存在。判法:
   ① 先去注释(注释里的路径是历史记录,不是引用);
   ② 同一条语句里出现否定形(`!existsSync` / `not ... exists` / `assert not`)
      ⇒ 这是断言不存在,跳过;
   ③ 登记在 SYNTHETIC_PROBES 里的人造探针名 ⇒ 跳过(逐条带理由)。

## 退出码(三态)
  0 = 所有「断言存在」的引用都存在
  1 = 有引用指向不存在的路径
  3 = 没跑成(取不到 package.json / 门禁面为空 / 自校准不过)
      🔴 3 不是 0:「没扫」与「扫了没发现」必须长得不一样。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

# 🔴 [2026-09-22 判红后加] 强制 UTF-8 输出。
#    实测:GBK 控制台下 `_die3` 里的 emoji 让 print 抛 UnicodeEncodeError ⇒
#    进程异常退出 rc=1 ⇒ 调用方把「**没跑成**」读成「有门禁引用了不存在的路径」——
#    一个没跑成的门被报成红,而且红在**完全错误的处置**上。
#    「门要能说出自己没跑成」的前提是:它印得出那句话。
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parents[1]
FE = ROOT / "frontend"

#: 本脚本在仓里的路径。自排除用它,**不用 `Path(__file__).name`** ——
#: preflight 会把它存成 `g.py` 再跑,按名字排除在那里失效。
SELF_REL = "scripts/gate_refs_exist.py"

#: 按 git ref 读时的来源。空 = 读文件系统(本地/pytest 用)。
#: 🔴 preflight 必须传 --repo/--ref:它的第一条设计纪律就是
#:    「全程 git show <ref>:<path>,绝不读工作树」——主仓工作树长期停在老分支。
_GIT_REPO: str = ""
_GIT_REF: str = ""


def _git(*args: str) -> tuple[int, bytes]:
    r = subprocess.run(["git", "-C", _GIT_REPO, *args], capture_output=True)
    return r.returncode, r.stdout


def _read_source(rel: str) -> str | None:
    if not _GIT_REF:
        p = ROOT / rel
        try:
            return p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return None
    rc, out = _git("show", f"{_GIT_REF}:{rel}")
    if rc != 0:
        return None
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _path_exists(rel: str) -> bool:
    if not _GIT_REF:
        return (ROOT / rel).exists()
    # 目录在 ls-tree 里不是条目,用前缀判
    return rel in tracked_files() or any(
        t.startswith(rel.rstrip("/") + "/") for t in tracked_files()
    )

PATH_LITERAL = re.compile(r"""['"`]([A-Za-z0-9_@./-]*?/[A-Za-z0-9_@./-]+)['"`]""")
FILE_LIKE = re.compile(r"\.(?:tsx?|jsx?|mjs|cjs|py|json|css|md|sql|sh)$")
_SKIP_PREFIX = ("http://", "https://", "//", "node_modules/")
_SPECIFIER = re.compile(r"^(?:@[\w.-]+/|react|react-dom|vite|node:)")

#: 断言**不存在**的写法。命中即跳过这条引用。
NEGATION = re.compile(
    r"(?:!\s*existsSync|!\s*fs\.existsSync|not\s+\w*\.?exists|assert\s+not|"
    r"should\s+not\s+exist|不存在|已删|不得|never_exists)",
    re.IGNORECASE,
)

#: 人造探针:门禁自带的反向对照夹具,按定义就不该存在。逐条带理由。
SYNTHETIC_PROBES: dict[str, str] = {
    "frontend/src/pages/__never_exists_zzz__.ts": "check_anchors 的「锚指向不存在文件」反臂",
    "pages/X.tsx": "WO_267-A 行业大类闸(test-industry-taxonomy.mjs)C 段反臂喂给 leaksIn 的内存夹具文件名,不落盘",
    "api/reader_api.py": "gate_binding_write_paths 的人造越权样本",
    "api/rogue_api.py": "同上",
    "api/rogue_update.py": "同上",
    "api/sneaky_api.py": "同上",
    "api/switch_api.py": "同上",
    "svc/target.py": "mutation replayer 硬闸契约的运行期临时树",
    "fixtures/longContent.ts": "原生弹窗扫描器的运行期临时夹具",
    "scratchpad/v7b/poison_probe.py": "变异运行器毒样本(运行期生成)",
    "./_verify_exit.mjs": "verify 退出码契约的运行期临时脚本",
    # [WO_276 · 2026-09-23] frontend/scripts/test-chunk-heal.mjs 喂给假 fetch 的站点产物名(assets/… 清单项与块间 ./… 引用)
    "assets/Page-r1.js": "WO_276 单元闸的内存夹具(运行期合成产物名,按定义不存在)",
    "assets/Page-r1.css": "同上",
    "assets/dep-shared.js": "同上",
    "./Lazy-noDeps.js": "同上",
    "./Page-r1.js": "同上",
    "./dep-deep.js": "同上",
}

BASES = (FE / "src", FE, ROOT)

#: 门禁**自己创建**的输出文件(不是引用,是写入目标)。逐条带理由。
GENERATED_OUTPUTS: dict[str, str] = {
    "docs/AI-CONTEXT/_v36_branding_scan/leak_baseline_phase0.json":
        "whitelabel_leak_scan --mode baseline 的产物;同一行就 mkdir(parents=True) 建它",
}

_TRACKED: set[str] | None = None


def tracked_files() -> set[str]:
    """全仓 tracked 路径集,用来做**后缀匹配**解析。

    🔴 不再靠猜基点:门禁里常用「相对某个 feature 目录」的写法当 dict 键
    (`components/sections/KeyFindings.tsx` 真身在
     `frontend/src/features/publicReportPremium/components/sections/`),
    猜基点必然漏。改成「有没有任何 tracked 文件以它结尾」。
    """
    global _TRACKED
    if _TRACKED is None:
        if _GIT_REF:
            # 按 ref 读:preflight 把本脚本抽到临时目录跑,ROOT 在那里
            # **不是仓** —— 用 `git -C <repo> ls-tree <ref>` 才取得到待发树。
            rc, out = _git("ls-tree", "-r", "--name-only", "-z", _GIT_REF)
            if rc != 0:
                out = b""
        else:
            out = subprocess.run(
                ["git", "-C", str(ROOT), "ls-files", "-z"],
                capture_output=True, check=True,
            ).stdout
        _TRACKED = {b.decode("utf-8") for b in out.split(b"\0") if b}
    return _TRACKED


def _die3(msg: str) -> int:
    print(f"  🔴 本门【没跑成·rc=3】:{msg} —— 没跑 != 没违规")
    return 3


def strip_comments(src: str, py: bool) -> str:
    """把注释内容换成等长空格,**保留行数与列位置**。

    注释里的路径是历史记录不是引用 —— 本仓 `verify-silent-reload-outlets.mjs`
    的注释里就躺着一条已删条目的路径,不去注释就会误报。
    """
    out = list(src)
    i, n = 0, len(src)
    state = None  # None | 'line' | 'block' | quote char
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state is None:
            if not py and c == "/" and nxt == "/":
                state = "line"; out[i] = out[i + 1] = " "; i += 2; continue
            if not py and c == "/" and nxt == "*":
                state = "block"; out[i] = out[i + 1] = " "; i += 2; continue
            if py and c == "#":
                state = "line"; out[i] = " "; i += 1; continue
            if c in "'\"`":
                state = c
            i += 1; continue
        if state == "line":
            if c == "\n":
                state = None
            else:
                out[i] = " "
            i += 1; continue
        if state == "block":
            if c == "*" and nxt == "/":
                out[i] = out[i + 1] = " "; state = None; i += 2; continue
            if c != "\n":
                out[i] = " "
            i += 1; continue
        # 字符串态
        if c == "\\":
            i += 2; continue
        if c == state:
            state = None
        i += 1
    return "".join(out)


_PY_DOCSTRING = re.compile(r"(\"\"\"|''')(?:.|\n)*?\1")


def blank_py_docstrings(src: str) -> str:
    """把 Python 三引号串的**内容**换成等长空白,保留行数。

    🔴 docstring 是字符串不是注释,去注释器碰不到它 —— 而门禁的模块 docstring
    里常写「原脚本 xxx.py 里的 FG-2 …」这类**历史说明**,里面的路径
    是记录不是引用。不处理它就会误报(实测误报 1 条)。
    """
    def _blank(m: re.Match[str]) -> str:
        return "".join(ch if ch == "\n" else " " for ch in m.group(0))

    return _PY_DOCSTRING.sub(_blank, src)


def gate_surfaces() -> tuple[list[Path], list[Path], int]:
    """分两个面返回,外加 build 串里**声明**的脚本数。

    🔴 分面返回是承重的。第一版把两个面合成一个 list、只判总数 `< 20`,
       于是我把 build 链那 66 个全砍掉去毒它时 —— **它没响**:
       还剩 42 个 python 门,总数照样过线。
       「整个扫描面消失」与「扫了没发现」在总数上长得一模一样。
       现在:两个面各自核对,且 build 面要拿**声明数**与**解析数**对齐。
    """
    raw_pkg = _read_source("frontend/package.json")
    if raw_pkg is None:
        return [], [], 0
    pkg = json.loads(raw_pkg)
    names = sorted(set(re.findall(r"[\w./-]+\.(?:mjs|js|cjs|ts)", pkg["scripts"]["build"])))
    declared = len(names)
    chain = [f"frontend/{n}" for n in names if _path_exists(f"frontend/{n}")]
    keys = ("gate", "verify", "check", "scan")
    # 🔴 自排除按**仓内路径**判,不按 `__file__` 的文件名。
    #    preflight 会把本脚本 `git show` 出来存成 `g.py` 再跑 —— 按 `Path(__file__).name`
    #    排除在那里**当场失效**,于是它扫到自己的自校准夹具(`a/b.tsx` /
    #    `src/nope_zzz.tsx`),报 3 条假阳。
    #    这与 WO_245 给密钥扫描器修过的洞是**同一个**:
    #    「谁决定这个文件叫什么」不是我,就不能拿名字当依据。
    pys = sorted(
        t for t in tracked_files()
        if t.startswith("scripts/") and t.endswith(".py")
        and "/" not in t[len("scripts/"):]
        and any(k in t.lower() for k in keys)
        and t != SELF_REL
    )
    return chain, pys, declared


def gate_files() -> list[str]:
    chain, pys, _ = gate_surfaces()
    return chain + pys


def resolves(raw: str, owner_rel: str) -> bool:
    """能不能在仓内找到它。

    先按几个常见基点直判,再退到**全仓 tracked 后缀匹配** ——
    门禁里常用「相对某个 feature 目录」的写法当 dict 键,猜基点必然漏。
    """
    owner_dir = owner_rel.rsplit("/", 1)[0] if "/" in owner_rel else ""
    for base in (owner_dir, "frontend/src", "frontend", ""):
        cand = f"{base}/{raw}" if base else raw
        if _path_exists(cand.replace("//", "/")):
            return True
    needle = raw.lstrip("./")
    return any(t == needle or t.endswith("/" + needle) for t in tracked_files())


def missing_refs() -> list[tuple[str, str, int]]:
    out: list[tuple[str, str, int]] = []
    for rel_gate in gate_files():
        src = _read_source(rel_gate)
        if src is None:
            continue
        is_py = rel_gate.endswith(".py")
        visible = strip_comments(src, py=is_py)
        if is_py:
            visible = blank_py_docstrings(visible)
        for line_no, line in enumerate(visible.splitlines(), 1):
            if NEGATION.search(line):
                continue                      # 断言不存在 —— 正确的退役写法
            for m in PATH_LITERAL.finditer(line):
                raw = m.group(1)
                if (raw.startswith(_SKIP_PREFIX) or raw.startswith("/")
                        or ".." in raw or _SPECIFIER.match(raw)):
                    continue
                if not FILE_LIKE.search(raw):
                    continue
                if raw in SYNTHETIC_PROBES or raw in GENERATED_OUTPUTS:
                    continue
                if not resolves(raw, rel_gate):
                    out.append((rel_gate, raw, line_no))
    return sorted(set(out))


def _selftest() -> bool:
    """尺子自校准:先证它会响,再说话。"""
    js = "const a = 'src/nope_zzz.tsx'  // 'src/in_comment_zzz.tsx'\n"
    v = strip_comments(js, py=False)
    if "in_comment_zzz" in v or "nope_zzz" not in v:
        print("  🔴 自校准失败:去注释器把该留的删了或该删的留了")
        return False
    if not NEGATION.search("ok(!existsSync(join(ROOT, 'a/b.tsx')))"):
        print("  🔴 自校准失败:否定形识别不出 !existsSync")
        return False
    if NEGATION.search("const p = 'a/b.tsx'"):
        print("  🔴 自校准失败:把普通引用误判成否定形")
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    global _GIT_REPO, _GIT_REF
    ap = __import__("argparse").ArgumentParser()
    ap.add_argument("--repo", default="", help="仓路径;与 --ref 一起用则按 git ref 读")
    ap.add_argument("--ref", default="", help="待发包尖 SHA —— preflight 必须传它")
    a = ap.parse_args(argv)
    if bool(a.repo) != bool(a.ref):
        return _die3("--repo 与 --ref 必须成对出现")
    _GIT_REPO, _GIT_REF = a.repo, a.ref

    if _read_source("frontend/package.json") is None:
        return _die3(
            f"取不到 frontend/package.json"
            + (f"(ref={_GIT_REF})" if _GIT_REF else "(工作树)")
        )
    chain, pys, declared = gate_surfaces()
    gates = chain + pys
    # 🔴 两个面**各自**核对,再对齐 build 面的声明数 vs 解析数。
    if declared and len(chain) != declared:
        return _die3(
            f"build 串声明 {declared} 个脚本,只解析到 {len(chain)} 个 —— "
            "有一批门根本没进扫描面,读数不可信")
    if len(chain) < 50:
        return _die3(f"build 链面只有 {len(chain)} 个(常态 66)—— 分母塌了")
    if len(pys) < 20:
        return _die3(f"scripts/*.py 面只有 {len(pys)} 个(常态 41)—— 分母塌了")
    if not _selftest():
        return _die3("尺子自校准不过 ⇒ 本门结论作废")

    missing = missing_refs()
    print(f"  分母:build链 {len(chain)} + scripts/*.py {len(pys)} = {len(gates)} 个 · 人造探针 {len(SYNTHETIC_PROBES)} 条 · 生成产物 {len(GENERATED_OUTPUTS)} 条")
    if missing:
        for g, raw, ln in missing:
            print(f"  🔴 {g}:{ln} 引用了不存在的路径:{raw}")
        print(f"  🔴 共 {len(missing)} 条 —— 门禁引用了仓里没有的东西,"
              f"那几条判据已**恒不命中**")
        return 1
    print(f"  ✅ 所有「断言存在」的引用都存在(0 条缺失)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
