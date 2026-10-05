# -*- coding: utf-8 -*-
"""证据包**只读**验收器(V7-B 起 · V8-B 封闭世界重做)。

V7-B 关掉的四条(仍然有效):先 unlink 再重生成 SUMS / roster 读交付包外 /
「junit 实收」拿同一份 JSON 的两个字段互证 / 最后一份 SUMS 不在循环里。

**V8-B 关掉的六条**(Codex fof6 P1-2 —— 他用它们协同伪造出 12 个 rc=0):

======================================  ==========================================
病                                      这一版怎么关
======================================  ==========================================
`if roster and …` 空 roster 短路         所有分母**显式**判空:空 ⇒ 红。而且分母不来自
                                        证据目录,来自**树**(load_v2 / QUICK_SCOPE /
                                        FULL)—— 证据里的 roster_*.json 降为
                                        **交叉核对**,不再是源
`0 targets == 0 XML == 0 tests` 当成功    权威合同钉死:18 包 / 27 发 / 33 份 junit /
                                        9 指纹,任何一项对不上就红
`parse_junit` 按 header 属性累加          以 **testcase 实体**为 SSOT:逐 case 恰一个
                                        结局,四数从实体**重算**,再与每层 suite
                                        header **和** run_meta 严格相等(header 撒谎 ⇒ 红)
`quick_red ⊆ XML 红集`(子集)            **精确相等**:用冻结的快集基线 junit 做真差集,
                                        并**验**基线红集为空(不是假设它零红)
无外部根锚                               根 `MANIFEST.json` 覆盖每份 SUMS 的 sha + tip/tree;
                                        重冻只证自洽,原真由根锚 + 台账 sha 双记
根目录不封闭 / junction                  恰 6 scope + 一份根锚,多一个文件/目录就红;
                                        任何读取**之前**对 root 与全部后代做
                                        no-follow / reparse 预检(Windows junction ⇒ 拒)
======================================  ==========================================

用法::

    python scripts/verify_fof_evidence.py <证据目录> --tip <本轮尖 sha>

`rc=0` 才叫过。
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import stat
import sys
import xml.etree.ElementTree as ET

sys.dont_write_bytecode = True          # 只读:连 __pycache__ 都不许写
with contextlib.suppress(AttributeError):   # 调用方可能把 stdout 换成
    sys.stdout.reconfigure(encoding="utf-8")   # StringIO(判据加载本模块时)

SUMS_NAME = "SHA256SUMS.txt"
MANIFEST_NAME = "MANIFEST.json"
# 🔴 [V9-B] 六个 scope:运输分裂之后基线分两段落盘
#    ( = 容器段 17 包 ·  = 宿主段 gate8)。
EXPECT_SCOPES = ("arms", "baseline17", "baseline_host", "findings",
                 "harness", "shots27")
#: 🔴 [2026-09-04 · #40] `packages` **18 → 23** —— 重锚,**只动这一根轴**。
#:    原因:这不是缺陷,是本文件第 0 项那条**绊线按设计响了** —— 它拿自己钉死的数
#:    和 `tree_contract()` 从树里现算的数比,不同即 fail(「别拿旧合同核新证据」)。
#:    树里的权威分母涨到 23 之后,绊线一直红,而 6 条阴性对照(`test_ev_00` 等
#:    「干净包必须 rc=0」)全部随之红 —— 那 6 条一红,同文件里所有「伪造必须被抓」
#:    的红就都证明不了东西了(阴性对照倒了,阳性结果悬空)。
#:
#:    **23 从哪来**:段①容器 22 包 + 段②宿主 1 包(gate8)。权威源是
#:    `scripts/gate9_full_denominator_baseline.py` 的 `GATE9_DENOMINATOR` /
#:    `GATE9_DENOMINATOR_SIZE`。**下次分母变时改这里**,并同批更新那份 SSOT。
#:    `ids` / `junit` / `fps` 三项**未动** —— 本文件第 6、7 项自己报了
#:    「OK 27 发」「33 份全对」,没漂。
CONTRACT = {"packages": 23, "ids": 27, "junit": 33, "fps": 9}

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mutation_replay_common as _RC        # noqa: E402  (红集 nodeid 单点)

_bad: list[str] = []


def fail(msg: str) -> None:
    _bad.append(msg)
    print(f"  XX {msg}")


def ok(msg: str) -> None:
    print(f"  OK {msg}")


class Section:
    """段内红计数 —— 各段的 OK 行只看**本段**。

    🔴 上一版用全局 `if not _bad`:前面一段红了,后面所有 OK 行全被抑制,
       交付日志里看起来像「后面根本没跑」。读数抑制本身就是一种误导。
    """
    def __init__(self):
        self.n = len(_bad)

    @property
    def clean(self) -> bool:
        return len(_bad) == self.n


def sha256_of(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ══ 0 树内权威合同 —— **不来自证据目录** ═══════════════════════════════════
def tree_contract() -> dict:
    """本轮该有的东西,从**树**里现算:18 包 / 27 发 / 33 份 junit / 9 指纹。

    🔴 为什么不能从证据目录取:那等于让被核物自己说自己该有多少 ——
       Codex 的 12 个伪造里「roster_pairs 空」与「0/0 分母」两个正是打这一格。
    """
    spec = importlib.util.spec_from_file_location(
        "_vfe_v2", HERE / "mutation_runner_extsel_v2_2026_08_27.py")
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    roster = m.load_v2()
    ids = sorted(x["id"] for x in roster)
    if len(ids) != len(set(ids)):
        raise SystemExit("🔴 树里的 roster 自己就有重复 id —— 权威分母不可用")
    scope = {i: tuple(m.QUICK_SCOPE[i]) for i in ids}
    return {"mod": m,
            "packages": tuple(m.FULL),
            "ids": ids,
            "scope": scope,
            "pairs": {x["id"]: x["pairs"] for x in roster},
            "junit_count": sum(len(v) for v in scope.values()),
            "fps": {m.criteria_fp_of(scope[i]) for i in ids}}


def contract_mismatches(got: dict, want: dict) -> list[str]:
    """树内合同 vs 钉死值 —— **纯裁决**,四路各自可单独驱动。

    🔴 [Review 亲毒 2026-08-30] 上一版这一格是内联的 for 循环 + fail,
       中和掉它**一条判据都不红** —— 门在、线接了、没人来咬一口试试。
       与 `gate_rc` 的 `dup` 路同病(那是第一例,这是第二例)。
       抽成纯函数才有地方打逐路正样本 + 数据流锁。
    """
    return [f"{k} {got.get(k)} != 钉死的 {want[k]}"
            for k in sorted(want) if got.get(k) != want[k]]


def record_set_mismatch(ids, roster) -> list[str]:
    """产物 id 集 vs **权威 roster** —— 纯裁决:重复 / 缺 / 多 / 空 各一路。

    🔴 [我自打的 S7 存活] 中和掉这一格,`r09` 照样绿 —— 因为 r09 是靠
       **另一条**带「树内」字样的检查(冻结 roster 交叉核对)过的。
       两把锁叠在同一条路上,存活还是被杀都分不出是哪一把。
       行为臂在这里做不到隔离:少一条记录必然连带 junit 认领数也不对。
       所以改成纯函数 + 逐路正样本 + 数据流锁。
    """
    out = []
    if not ids:
        out.append("产物 id 集为空 —— 空分母不是通过")
    dup = sorted({i for i in ids if list(ids).count(i) > 1})
    if dup:
        out.append(f"产物里有重复 id:{dup}")
    miss, extra = sorted(set(roster) - set(ids)), sorted(set(ids) - set(roster))
    if miss or extra:
        out.append(f"与**树内** roster 两向不空:缺 {miss[:5]} · 多 {extra[:5]}")
    return out


# ══ 1 reparse / junction 预检(任何读取之前)══════════════════════════════
def assert_no_reparse(root: pathlib.Path) -> bool:
    """root 与**全部后代**都不许是符号链接 / junction / 重解析点。

    🔴 内容对得上也不算:它指向的东西不在这份证据里,下一秒可以变。
       Windows 上 `is_symlink()` 认不出 junction,要看 `FILE_ATTRIBUTE_REPARSE_POINT`。
    """
    RP = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    hits: list[str] = []
    stack = [root]
    while stack:
        p = stack.pop()
        try:
            st = os.lstat(p)
        except OSError as e:
            fail(f"lstat 失败 {p}:{e}")
            return False
        if p.is_symlink() or (getattr(st, "st_file_attributes", 0) & RP):
            hits.append(str(p))
            continue                      # 不跟进去
        if stat.S_ISDIR(st.st_mode):
            stack.extend(p.iterdir())
    if hits:
        fail(f"证据里有符号链接/junction/重解析点({len(hits)} 个):{hits[:4]}")
        return False
    ok("no-follow 预检:root 与全部后代无重解析点")
    return True


# ══ 2 封闭世界:根下恰 6 scope + 一份根锚 ═════════════════════════════════
def assert_closed_root(root: pathlib.Path) -> None:
    _s = Section()
    entries = sorted(root.iterdir())
    dirs = tuple(sorted(p.name for p in entries if p.is_dir()))
    files = sorted(p.name for p in entries if p.is_file())
    if dirs != EXPECT_SCOPES:
        fail(f"根下目录不是恰好那 {len(EXPECT_SCOPES)} 个:{list(dirs)}"
             f"(应 {list(EXPECT_SCOPES)})")
    if files != [MANIFEST_NAME]:
        fail(f"根下文件不是恰好一份 {MANIFEST_NAME}:{files}")
    for name in dirs:
        stray = [p.relative_to(root).as_posix()
                 for p in (root / name).rglob(SUMS_NAME)
                 if p.parent != root / name]
        if stray:
            fail(f"{name}: 有嵌套的 {SUMS_NAME}:{stray}")
    if _s.clean:
        ok(f"封闭世界:根下恰 {len(dirs)} 个 scope + 1 份根锚,无嵌套 SUMS")


def tree_three_way(cli, manifest, baseline) -> list[str]:
    """三向树一致 —— **纯判定**,不读盘不打印,好单独喂反例。

    🔴 [V9-B · Codex fof7 P1-5] 修之前 `MANIFEST.json` 里的 `tree` 是个
       **只写不读**的字段:`freeze_sums.py --tree` 老老实实写进去,验收器
       一眼都没看过。只写不读的字段最坏的地方不是「没用」,是它**看起来
       像证据**:发车台账上「产物绑树」那一格,靠的正是这个从没被核过的值。

    缺失一律算不符:「没有树就不比」等于把这道闸对最需要它的那种产物关掉。
    """
    vals = {"CLI --tree": cli, "根锚 MANIFEST.tree": manifest,
            "baseline.identity.tree": baseline}
    bad = [f"{k} 缺失或不是字符串" for k, v in vals.items()
           if not isinstance(v, str) or not v.strip()]
    if bad:
        return bad
    if len({v for v in vals.values()}) != 1:
        return [f"三向树对不上:" + " · ".join(f"{k}={v[:12]}"
                                            for k, v in vals.items())]
    return []


def check_manifest(root: pathlib.Path, tip: str, tree: str, sums_sha: dict,
                   want_sha: str | None = None) -> str | None:
    """核根锚;返回根锚里记的 tree(供第 8 段做三向)。"""
    _s = Section()
    mf = root / MANIFEST_NAME
    if not mf.is_file():
        fail(f"没有根锚 {MANIFEST_NAME} —— 重冻只证自洽,证不了原真")
        return None
    # 🔴 先比根锚**自身**的 sha,再读它的内容。顺序不能反:
    #    台账上双记的那一格记的就是这个 sha,读完内容再比等于
    #    「先信了它说的话,再问它是不是本人」。
    self_sha = sha256_of(mf)
    if want_sha is not None and self_sha != want_sha:
        fail(f"根锚自身 sha256={self_sha[:16]} != 台账登记的 {want_sha[:16]} "
             "—— 这份根锚不是台账里那一份,后面全部读数都不作数")
        return None
    try:
        man = json.loads(mf.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        fail(f"根锚解析不了:{e}")
        return None
    if man.get("tip") != tip:
        fail(f"根锚 tip={man.get('tip')} != 本轮尖")
    if man.get("tree") != tree:
        fail(f"根锚 tree={str(man.get('tree'))[:12]} != 本轮树 {tree[:12]}")
    scopes = man.get("scopes") or {}
    if set(scopes) != set(EXPECT_SCOPES):
        fail(f"根锚覆盖的 scope 不对:{sorted(scopes)}")
        return man.get("tree")
    for name, spec in sorted(scopes.items()):
        want, got = spec.get("sums_sha256"), sums_sha.get(name)
        if want != got:
            fail(f"根锚里 {name} 的 SUMS sha={str(want)[:16]} != 实测 {str(got)[:16]}")
    if _s.clean:
        ok(f"根锚:tip + tree 都命中 · 每份 SUMS sha 逐个相符 · "
           f"自身 sha256 = {self_sha}"
           + ("(与台账登记值相符)" if want_sha else "(台账值未传,未比)"))
    return man.get("tree")


# ══ 3 每份 SUMS ═══════════════════════════════════════════════════════════
def check_sums(sub: pathlib.Path) -> str | None:
    name = sub.name
    sums = sub / SUMS_NAME
    if not sums.is_file():
        fail(f"{name}: 没有 {SUMS_NAME}")
        return None
    listed: dict[str, str] = {}
    dup: list[str] = []
    for i, line in enumerate(sums.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            digest, rel = line.split("  ", 1)
        except ValueError:
            fail(f"{name}:{i} 行格式坏:{line[:60]!r}")
            continue
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            fail(f"{name}:{i} 不是 sha256:{digest[:20]!r}")
            continue
        pp = pathlib.PurePosixPath(rel)
        if pp.is_absolute() or ".." in pp.parts or (len(rel) > 1 and rel[1] == ":"):
            fail(f"{name}: 越界路径 {rel!r}")
            continue
        if rel in listed:
            dup.append(rel)
        listed[rel] = digest
    if not listed:
        fail(f"{name}: SUMS 一条都没有 —— 空分母不是通过")
        return None
    if dup:
        fail(f"{name}: SUMS 里有重复路径 {sorted(set(dup))}")
    on_disk = {p.relative_to(sub).as_posix() for p in sub.rglob("*")
               if p.is_file() and p.name != SUMS_NAME}
    missing = sorted(set(listed) - on_disk)
    extra = sorted(on_disk - set(listed))
    if missing:
        fail(f"{name}: SUMS 列了但盘上没有 {missing[:5]}({len(missing)} 个)")
    if extra:
        fail(f"{name}: 盘上有但 SUMS 没列 {extra[:5]}({len(extra)} 个)")
    mism = [rel for rel in sorted(set(listed) & on_disk)
            if sha256_of(sub / rel) != listed[rel]]
    if mism:
        fail(f"{name}: 内容对不上 {mism[:5]}({len(mism)} 个)")
    if not (dup or missing or extra or mism):
        ok(f"{name}: {len(listed)} 份逐字节相符 · SUMS 自身 sha256 = {sha256_of(sums)}")
    return sha256_of(sums)


# ══ 4 JUnit:以 testcase **实体**为 SSOT ═══════════════════════════════════
_JCACHE: dict[str, dict | None] = {}


def parse_junit(path: pathlib.Path) -> dict | None:
    """四数从 `<testcase>` **实体重算**,再与每层 suite header 严格相等。

    🔴 [V8-B] 上一版按 `<testsuite>` 的 header 属性累加 —— header 是**可以随便写的**。
       Codex 的「header + JSON 协同伪造」正是打这一格:header 说 7 个 pass,
       实体里根本没有,而上一版照单全收。
       实体才是 SSOT,header 只是它的**声明**;两者不等 ⇒ 这份 XML 在撒谎。
    """
    key = str(path)
    if key in _JCACHE:
        return _JCACHE[key]
    _JCACHE[key] = None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        fail(f"junit 解析不了 {path.name}:{e}")
        return None
    if root.tag not in ("testsuite", "testsuites"):
        fail(f"junit 根不是 testsuite/testsuites:{path.name} → {root.tag}")
        return None
    if [e for e in root.iter("testsuites") if e is not root]:
        fail(f"junit 里有**非根** testsuites:{path.name} —— 嵌套聚合会让计数重复累加")
        return None
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    ent = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    reds: set[str] = set()
    skips: set[str] = set()
    idents: list[tuple] = []
    ident_skips: list[tuple] = []
    ident_reds: list[tuple] = []
    for case in root.iter("testcase"):
        ent["tests"] += 1
        idents.append((case.get("classname"), case.get("name")))
        kinds = [k for k in ("failure", "error", "skipped")
                 if case.find(k) is not None]
        if len(kinds) > 1:
            fail(f"{path.name}: 一个 testcase 同时有 {kinds} —— 结局不唯一,不予采信")
            return None
        if kinds:
            ent["skipped" if kinds[0] == "skipped" else kinds[0] + "s"] += 1
            nid = _RC.red_nodeid(case.get("classname"), case.get("name"))
            if kinds[0] == "skipped":
                skips.add(nid)
                ident_skips.append((case.get("classname"), case.get("name")))
            else:
                reds.add(nid)
                ident_reds.append((case.get("classname"), case.get("name")))
    hdr = {k: sum(int(s.get(k) or 0) for s in suites) for k in ent}
    if hdr != ent:
        fail(f"{path.name}: header 与实体重算不符 header={hdr} 实体={ent}")
        return None
    # 🔴 [V9-B · Codex fof7 P2-6] **逐 suite** 再核一遍,并核闭合。
    #    上面那一层只比「所有 suite 的 header 之和 == 全局实体重算」——
    #    两个 sibling suite,一个 header 说 5 条却一个 testcase 都没有、
    #    另一个有 5 条却 header 写 0,总和照样相等。空 suite 顶 header 的形。
    direct_total = 0
    for s in suites:
        d = s.findall("testcase")
        direct_total += len(d)
        de = {"tests": len(d),
              "failures": sum(1 for c in d if c.find("failure") is not None),
              "errors": sum(1 for c in d if c.find("error") is not None),
              "skipped": sum(1 for c in d if c.find("skipped") is not None)}
        dh = {k: int(s.get(k) or 0) for k in de}
        if dh != de:
            fail(f"{path.name}: suite {s.get('name')!r} 的 header {dh} "
                 f"与它**直属**实体 {de} 不符 —— 兄弟 suite 之间可以互相顶数")
            return None
    if direct_total != ent["tests"]:
        fail(f"{path.name}: 直属 testcase 合计 {direct_total} != 全局实体 "
             f"{ent['tests']} —— 有 testcase 不在任何 suite 的直属层(逐 suite 核不到它)")
        return None
    # 🔴 身份唯一:同一个 (classname, name) 出现两次,任何计数式检查都看不见,
    #    而「基线与变异跑的是同一批用例」这句话正是靠身份集说的。
    if len(set(idents)) != len(idents):
        dup = sorted({i for i in idents if idents.count(i) > 1})
        fail(f"{path.name}: 有重复的 (classname, name):{dup[:3]}")
        return None
    ent["red_nodes"] = reds
    ent["skipped_nodes"] = skips
    # 🔴 [fof8 P1-2] 两个 id 空间**各留各的**,不许互相顶替:
    #    · 短式(`red_nodeid`)—— 与产物里已用短式记录的红集/skip 集比对;
    #    · 无损身份(`testcase_identity`)—— **跨趟**比「跑的是不是同一批用例」。
    #    上一版 `node_ids` 用的是短式,于是同尾名异模块被压成同一身份
    #    (Codex 端到端反例:改一份 XML 的模块前缀 + 完整重冻 ⇒ 三参验收仍 rc=0)。
    ent["node_ids"] = {_RC.testcase_identity(c, n) for c, n in idents}
    ent["skipped_ids"] = {_RC.testcase_identity(c, n) for c, n in ident_skips}
    ent["red_ids"] = {_RC.testcase_identity(c, n) for c, n in ident_reds}
    _JCACHE[key] = ent
    return ent


def _skip_whitelist() -> frozenset:
    """skip 白名单**借** gate9 那一份 —— 验收器不抄第二张表。

    抄一份的代价在本轮已经付过一次:`gate9_report` 手写的「算杀」清单
    在新增一种杀之后当场过期,而过期的表现是**那一发静静地漏掉**。
    """
    spec = importlib.util.spec_from_file_location(
        "_vfe_g9", HERE / "gate9_full_denominator_baseline.py")
    m = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()):
        spec.loader.exec_module(m)
    return frozenset(m.EXPECTED_SKIP_NODEIDS)


SKIP_WHITELIST = _skip_whitelist()


def norm_pairs(pairs) -> list:
    """把一发的 pairs 标准化成可比较的形状:`[[from, to], ...]`,顺序保留。

    顺序保留是故意的:替换是**逐对按序**应用的,换个顺序就是另一份变异定义
    (前一对的替换结果可能正是后一对的锚)。
    """
    if not isinstance(pairs, list):
        return []
    out = []
    for x in pairs:
        if isinstance(x, dict):
            out.append([x.get("from"), x.get("to")])
        elif isinstance(x, (list, tuple)) and len(x) == 2:
            out.append([x[0], x[1]])
        else:
            out.append(["<坏形状>", repr(x)[:40]])
    return out


def pairs_mismatches(frozen: dict, tree_pairs: dict) -> list[str]:
    """[V9-B · Codex fof7 P1-8] 冻结的 roster_pairs 与树内**内容级**相等。

    🔴 修之前只比 `sorted(frozen) == ids` —— 只比了**键**。
       键对上而值被换掉(锚改了、替换文本改了、某发的 pairs 被清空成 `[]`)
       是一份完全合法的 JSON,而它描述的是另一套变异。
       「27 个 id 都在」这句话,对「这 27 发到底是什么」一个字都没说。
    """
    out = []
    if set(frozen) != set(tree_pairs):
        return [f"roster_pairs 的 id 集与树内不符("
                f"多 {sorted(set(frozen) - set(tree_pairs))[:3]} · "
                f"少 {sorted(set(tree_pairs) - set(frozen))[:3]})"]
    for mid in sorted(tree_pairs):
        want, got = norm_pairs(tree_pairs[mid]), norm_pairs(frozen[mid])
        if not want:
            out.append(f"{mid}: 树内 pairs 为空 —— 权威分母本身不可用")
        elif got != want:
            out.append(f"{mid}: 冻结的 pairs 与树内不符(冻结 {len(got)} 对 · "
                       f"树内 {len(want)} 对;首个差异 "
                       f"{next((f'{g!r} != {w!r}' for g, w in zip(got, want) if g != w), '长度不同')})")
    return out


def short_nodeid(nid: str) -> str:
    """把完整 pytest nodeid 归一到 XML 侧那个空间(模块名 :: 用例名)。

    🔴 记录里存的是 `tests/x/test_y.py::test_z`,而 XML 侧由
    `_RC.red_nodeid(classname, name)` 产出 `test_y::test_z` —— **两个 id 空间**。
    直接对比时这次是红(好),但同一个错误反过来就是最危险的形态:
    两边都空 ⇒ 集合相等 ⇒ 一路绿。所以归一化要写成一个有名字的函数,
    而不是在比较点顺手切一刀。
    """
    if "::" not in nid:
        return nid
    head, rest = nid.split("::", 1)
    if head.endswith(".py"):
        head = head.rsplit("/", 1)[-1][:-3]
    return f"{head}::{rest}"


def conservation_mismatches(rec: dict, j: dict | None, whitelist) -> list[str]:
    """[V9-B · Codex fof7 P1-6] 一个包的**守恒**:纯判定,逐条点名。

    「基线全绿」这句话过去只由 `totals.red == 0` 一个数支撑。而 totals 是
    **加出来的**:某个包 collected 0(整包没收集到)、或者 skipped 掉一半,
    都不会让那个数变。零红在一个悄悄变小的分母上永远成立。

    六条各管一件,分开写好让反例点名:
      · XML 缺失        —— 这个包根本没有原始产物
      · tests > 0       —— 收集到 0 条不是「全绿」,是没跑
      · collected==tests—— 收集数与执行数对不上 ⇒ 中途没跑完
      · red==f+e        —— 红的口径自洽
      · passed 守恒     —— passed == tests - red - skipped
      · skip 合同       —— 条数与 nodeid 集**双向**都要与白名单对上
    """
    tgt = rec.get("target")
    out: list[str] = []
    if j is None:
        return [f"{tgt}: 没有可用的原始 junit —— 这个包的读数没有产物支撑"]
    if j["tests"] <= 0:
        out.append(f"{tgt}: XML 实体 tests={j['tests']} —— 收集到 0 条不算全绿")
    for k in ("collected", "tests", "passed", "red", "skipped"):
        if type(rec.get(k)) is not int:
            out.append(f"{tgt}: {k}={rec.get(k)!r} 不是 int")
            return out
    if rec["collected"] != rec["tests"]:
        out.append(f"{tgt}: collected={rec['collected']} != tests={rec['tests']}")
    if rec["tests"] != j["tests"]:
        out.append(f"{tgt}: 记录 tests={rec['tests']} != XML 实体 {j['tests']}")
    if rec["red"] != j["failures"] + j["errors"]:
        out.append(f"{tgt}: 记录 red={rec['red']} != XML 实体红 "
                   f"{j['failures'] + j['errors']}")
    if rec["skipped"] != j["skipped"]:
        out.append(f"{tgt}: 记录 skipped={rec['skipped']} != XML 实体 {j['skipped']}")
    if rec["passed"] != rec["tests"] - rec["red"] - rec["skipped"]:
        out.append(f"{tgt}: passed={rec['passed']} != tests - red - skipped = "
                   f"{rec['tests'] - rec['red'] - rec['skipped']}")
    sk = set(rec.get("skipped_nodes") or ())
    if len(sk) != rec["skipped"]:
        out.append(f"{tgt}: skipped_nodes {len(sk)} 条 != skipped={rec['skipped']} "
                   "—— 只记数字的话白名单无从比对")
    shortsk = {short_nodeid(x) for x in sk}
    if len(shortsk) != len(sk):
        out.append(f"{tgt}: skip nodeid 归一后出现碰撞 —— 这一比不可用")
    elif shortsk != j["skipped_nodes"]:
        out.append(f"{tgt}: 记录的 skip nodeid 集与 XML 不符("
                   f"多 {sorted(shortsk - j['skipped_nodes'])[:2]} · "
                   f"少 {sorted(j['skipped_nodes'] - shortsk)[:2]})")
    off = sorted(sk - set(whitelist))
    if off:
        out.append(f"{tgt}: 白名单之外的 skip:{off[:3]}")
    return out


def identity_mismatches(rid: str, tgt: str, j: dict, b: dict) -> list[str]:
    """[V9-B · Codex fof7 P1-7] 基线与变异那两趟**跑的是同一批用例**吗。

    差集 `xml_red - base_red` 的全部意义建立在这句话上。两趟收集到的用例集
    不同(变异让某个模块 import 失败 ⇒ 整文件没被收集)时,差集算出来照样
    是个「合法的」小集合,而它描述的事情根本不是「这条判据红了」。
    身份集比数字硬:少收集 3 条、多收集 3 条,`tests` 可以纹丝不动。
    """
    out = []
    if not b["node_ids"]:
        return [f"{rid}/{tgt}: 基线 junit 里一个 testcase 都没有 —— 空分母不是通过"]
    # 🔴 [fof8 P1-2] 下面两比一律吃**无损身份**(`node_ids`/`skipped_ids`),
    #    不吃短式 —— 短式会把同尾名异模块压成同一条。
    miss = sorted(b["node_ids"] - j["node_ids"])
    extra = sorted(j["node_ids"] - b["node_ids"])
    if miss or extra:
        out.append(f"{rid}/{tgt}: 变异那趟与基线的用例身份集不同("
                   f"没跑到 {miss[:3]} · 多出 {extra[:3]})—— 差集口径不成立")
    if b["skipped_ids"] != j["skipped_ids"]:
        out.append(f"{rid}/{tgt}: 两趟的 skip 集不同("
                   f"基线多 {sorted(b['skipped_ids'] - j['skipped_ids'])[:2]} · "
                   f"变异多 {sorted(j['skipped_ids'] - b['skipped_ids'])[:2]})"
                   " —— 变异把判据 skip 掉会被算成「没红」")
    return out


def base_full_junit_name(target: str) -> str:
    return f"junit_{target.replace('/', '_')}.xml"


def junit_name(rid: str, target: str) -> str:
    return f"junit_{rid[4:]}_{target.replace('/', '_')}.xml"


def base_junit_name(target: str) -> str:
    return f"junit_v2base_{target.replace('/', '_')}.xml"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("evidence_dir")
    ap.add_argument("--tip", required=True)
    ap.add_argument("--tree", required=True,
                    help="本轮树(`git rev-parse HEAD^{tree}`)。"
                         "显式传参:验收器常跑在证据目录侧,那里没有仓可推。")
    ap.add_argument("--manifest-sha256", default=None,
                    help="台账登记的根锚自身 sha256;传了就在读 payload **之前**比对")
    a = ap.parse_args()
    root = pathlib.Path(a.evidence_dir)
    if not root.is_dir():
        print(f"🔴 证据目录不存在:{root}")
        return 1

    print("=== 0 树内权威合同(**不来自证据目录**)===")
    C = tree_contract()
    print(f"  包 {len(C['packages'])} · 发 {len(C['ids'])} · "
          f"应有 mutation junit {C['junit_count']} 份 · 指纹 {len(C['fps'])} 种")
    for d in contract_mismatches({"packages": len(C["packages"]),
                                  "ids": len(C["ids"]),
                                  "junit": C["junit_count"],
                                  "fps": len(C["fps"])}, CONTRACT):
        fail(f"树里的权威合同变了:{d}")

    print("=== 1 no-follow 预检 ===")
    if not assert_no_reparse(root):
        print()
        print("XX 预检不过,后面一律不读")
        return 1

    print("=== 2 封闭世界 ===")
    assert_closed_root(root)

    def snapshot():
        return {p.relative_to(root).as_posix():
                (p.stat().st_size, p.stat().st_mtime_ns, sha256_of(p))
                for p in sorted(root.rglob("*")) if p.is_file()}

    print("=== 3 零写入自证:跑前快照 ===")
    before = snapshot()
    print(f"  {len(before)} 份文件")

    print(f"=== 4 {len(EXPECT_SCOPES)} 份 SUMS ===")
    sums_sha: dict[str, str | None] = {}
    for name in EXPECT_SCOPES:
        sub = root / name
        if not sub.is_dir():
            fail(f"缺 scope {name}")
            continue
        sums_sha[name] = check_sums(sub)

    print("=== 5 根锚 ===")
    man_tree = check_manifest(root, a.tip, a.tree, sums_sha, a.manifest_sha256)

    shots = root / "shots27"
    print("=== 6 27 发产物 vs 树内合同 ===")
    _s6 = Section()
    res = shots / "extsel_v2_results_final.json"
    recs: list[dict] = []
    if not res.is_file():
        fail("shots27/extsel_v2_results_final.json 不在")
    else:
        recs = json.loads(res.read_text(encoding="utf-8"))
        for d in record_set_mismatch([r.get("id") for r in recs], C["ids"]):
            fail(d)
        for fn in ("roster_ids.json", "roster_pairs.json"):
            fp = shots / fn
            if not fp.is_file():
                fail(f"shots27/{fn} 不在")
                continue
            frozen = json.loads(fp.read_text(encoding="utf-8"))
            if not frozen:
                fail(f"shots27/{fn} 是空的 —— 空 roster 不是通过")
            elif sorted(frozen) != C["ids"]:
                fail(f"冻结的 {fn} 的 id 集与**树内** roster 不符")
            elif fn == "roster_pairs.json":
                for d in pairs_mismatches(frozen, C["pairs"]):
                    fail(d)
        offtip = [r.get("id") for r in recs if r.get("tip") != a.tip]
        if offtip:
            fail(f"这些发没绑本轮尖 {a.tip[:12]}:{offtip[:5]}")
        notk = [r.get("id") for r in recs if r.get("verdict") != "KILLED"]
        if notk:
            fail(f"非 KILLED:{notk}")
        # 逐发判据指纹必须等于**树内**按它的作用域算出来的那一个
        fpbad = [r.get("id") for r in recs
                 if r.get("id") in C["scope"]
                 and r.get("criteria_fp") != C["mod"].criteria_fp_of(C["scope"][r["id"]])]
        if fpbad:
            fail(f"这些发的 criteria_fp 与树内算出的不符:{fpbad[:5]}")
        seenfp = {r.get("criteria_fp") for r in recs}
        if seenfp != C["fps"]:
            fail(f"产物里的指纹集与树内 {len(C['fps'])} 种不符(产物 {len(seenfp)} 种)")
        # 🔴 [V9-B · P1-8] 逐发的 quick_scope 也要与树内一致 ——
        #    作用域决定「这一发在哪些包上被判死活」。产物里把它写窄一格,
        #    junit 份数、红集、指纹会跟着一起自洽,整条链看不出问题。
        for r in recs:
            rid = r.get("id")
            if rid in C["scope"]:
                gots = tuple(r.get("quick_scope") or ())
                if gots != C["scope"][rid]:
                    fail(f"{rid}: 产物 quick_scope={list(gots)} != "
                         f"树内 {list(C['scope'][rid])}")
        # 🔴 结构 + 语义都用 runner 里那**同一个**谓词,不在这里再写一份
        for r in recs:
            why = C["mod"].validate_record_run_meta(
                r, C["scope"].get(r.get("id")), full_targets=C["packages"])
            if why:
                fail(f"记录不可信:{why}")
        if _s6.clean:
            ok(f"27 发:{len(recs)} 条 · 全 KILLED · 全绑 {a.tip[:12]} · "
               f"结构+语义过树内唯一谓词 · 冻结 roster 与树内一致")

    print("=== 7 原始 JUnit 实体重算 + quick_red 精确差集 ===")
    _s7 = Section()
    jdir, jbase = shots / "junit", shots / "junit_base"
    if not jdir.is_dir() or not jbase.is_dir():
        fail("shots27/junit 或 junit_base 不在 —— 没有原始 XML 就只能拿 JSON 自证")
    elif recs:
        seen: set[str] = set()
        for r in recs:
            rid = r.get("id")
            if rid not in C["scope"]:
                continue
            xml_red: set[str] = set()
            base_red: set[str] = set()
            xml_red_ids: set[str] = set()
            base_red_ids: set[str] = set()
            for tgt in C["scope"][rid]:
                fp, bp = jdir / junit_name(rid, tgt), jbase / base_junit_name(tgt)
                if not fp.is_file():
                    fail(f"{rid}/{tgt}: 缺原始 junit {fp.name}")
                    continue
                if not bp.is_file():
                    fail(f"{rid}/{tgt}: 缺快集基线 junit {bp.name} —— 无从做精确差集")
                    continue
                seen.add(fp.name)
                j, b = parse_junit(fp), parse_junit(bp)
                if j is None or b is None:
                    continue
                m = (r.get("run_meta") or {}).get(tgt) or {}
                if j["tests"] != m.get("collected"):
                    fail(f"{rid}/{tgt}: XML 实体 tests={j['tests']} "
                         f"!= run_meta.collected={m.get('collected')}")
                if j["skipped"] != m.get("skipped"):
                    fail(f"{rid}/{tgt}: XML 实体 skipped={j['skipped']} "
                         f"!= run_meta.skipped={m.get('skipped')}")
                if j["failures"] + j["errors"] != m.get("red"):
                    fail(f"{rid}/{tgt}: XML 实体红={j['failures'] + j['errors']} "
                         f"!= run_meta.red={m.get('red')}")
                if b["failures"] + b["errors"]:
                    fail(f"{tgt}: 快集**基线**带红 {b['failures'] + b['errors']} 条 —— "
                         "基线带红时「新增红」的口径整个不成立")
                for d in identity_mismatches(rid, tgt, j, b):
                    fail(d)
                xml_red |= j["red_nodes"]
                base_red |= b["red_nodes"]
                xml_red_ids |= j["red_ids"]
                base_red_ids |= b["red_ids"]
            new_red = xml_red - base_red
            # 🔴 [fof8 P1-2 顺带] 这个差集是**跨趟**的,却跑在短式(展示)空间 ——
            #    产物里的 `quick_red` 就是短式记的,所以比较必须在短式空间做。
            #    它现在安全**只因为基线被强制零红**(上面那条 fail 就在同一个循环里),
            #    也就是「靠别处的不变量才安全」。就地上一道闸:同一批红在无损空间
            #    与短式空间算出的差集**势必须相等** —— 一旦发生同尾名压缩,两边就不等。
            if len(xml_red_ids - base_red_ids) != len(new_red):
                fail(f"{rid}: 新增红在无损身份空间有 "
                     f"{len(xml_red_ids - base_red_ids)} 条、短式空间只有 {len(new_red)} 条"
                     " —— 同尾名异模块被压掉了,这个差集不可用")
            qr = set(r.get("quick_red") or [])
            if qr != new_red:
                fail(f"{rid}: quick_red 与 XML 新增红集**不精确相等**:"
                     f"多 {sorted(qr - new_red)[:3]} · 少 {sorted(new_red - qr)[:3]}")
        if len(seen) != C["junit_count"]:
            fail(f"认领到的 mutation junit {len(seen)} 份 != 树内合同 {C['junit_count']} 份")
        stray = sorted({p.name for p in jdir.glob("junit_*.xml")} - seen)
        if stray:
            fail(f"shots27/junit 有 {len(stray)} 份没有任何记录认领:{stray[:4]}")
        if _s7.clean:
            ok(f"三向对账 XML 实体 ↔ run_meta ↔ verdict:{len(seen)} 份全对;"
               "quick_red 与新增红集**精确相等**;快集基线零红")

    print("=== 8 基线全分母(两段运输合起来覆盖满)===")
    _s8 = Section()
    # 🔴 [V9-B · Review 裁定 2026-08-31] 运输分裂:
    #    段①(容器)跑 17 包 · 段②(宿主)跑 gate8。合起来必须**恰好**是 18,
    #    不重不漏。两段各带 identity,且 tip/tree 必须**逐字相等** ——
    #    「两个尖各跑一段」正是本仓踩过的老坑(两尖跑出逐字节相同的 baseline.json)。
    segs = [("baseline17", "container"), ("baseline_host", "host")]
    seen_targets: dict[str, str] = {}
    seg_ident: dict[str, dict] = {}
    grand = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for scope, want_transport in segs:
        bj = root / scope / "baseline.json"
        if not bj.is_file():
            fail(f"{scope}/baseline.json 不在 —— 两段缺一段,18 包覆盖不满")
            continue
        b = json.loads(bj.read_text(encoding="utf-8"))
        ident = b.get("identity") or {}
        seg_ident[scope] = ident
        need = ("tip", "tree", "started_at", "transport", "python", "platform",
                "runner")
        for k in need:
            if not ident.get(k):
                fail(f"{scope}: identity 缺 {k}")
        # 🔴 [fof8] runner 版本:两段的**值可以不同**(容器 3.12 / 宿主 venv),
        #    所以不比相等,只要求「记全了」——记不全 = 说不出这批读数是谁跑的。
        rv = ident.get("runner")
        if not isinstance(rv, dict) or not rv:
            fail(f"{scope}: identity.runner 不是非空 dict —— 产物没绑 runner 版本")
        else:
            thin = sorted(k for k, v in rv.items()
                          if not isinstance(v, str) or not v.strip())
            if thin:
                fail(f"{scope}: identity.runner 里这些没有版本值:{thin}")
            for m in ("pytest", "pytest_asyncio"):
                if m not in rv:
                    fail(f"{scope}: identity.runner 缺 {m}")
        if ident.get("transport") != want_transport:
            fail(f"{scope}: identity.transport={ident.get('transport')!r} "
                 f"!= 本段应有的 {want_transport!r}")
        # 容器段必须有镜像身份;宿主段没有镜像可绑,环境身份由 python+platform 承担。
        if want_transport == "container" and not ident.get("image_id"):
            fail(f"{scope}: 容器段没有 image_id —— 这份证据没有环境身份")
        if ident.get("tip") != a.tip:
            fail(f"{scope}: identity.tip={ident.get('tip')} != 本轮尖")
        for d in tree_three_way(a.tree, man_tree, ident.get("tree")):
            fail(f"{scope}: {d}")
        if sorted(b.get("targets") or []) != sorted(C["packages"]):
            fail(f"{scope}: targets(冻结分母全集)与树内包集不符")
        # 这一段**覆盖了谁** —— 与 targets 是两件事
        segt = list(b.get("segment_targets") or [])
        if not segt:
            fail(f"{scope}: 没有 segment_targets —— 说不出这一段覆盖了哪些包")
            continue
        if len(set(segt)) != len(segt):
            fail(f"{scope}: segment_targets 里有重复")
        for tg in segt:
            if tg in seen_targets:
                fail(f"{tg} 被两段都跑了({seen_targets[tg]} 与 {scope})—— "
                     "同包两种运输 = 同包两个真相")
            seen_targets[tg] = scope
        t = b.get("totals") or {}
        if t.get("red"):
            fail(f"{scope}: 基线带红 {t.get('red')} 条")
        jd = root / scope / "junit"
        xs = sorted(jd.glob("junit_*.xml")) if jd.is_dir() else []
        brecs = b.get("records") or []
        btg = [r.get("target") for r in brecs]
        if len(btg) != len(set(btg)):
            fail(f"{scope}: records 里有重复 target:"
                 f"{sorted({x for x in btg if btg.count(x) > 1})[:3]}")
        if sorted(x for x in btg if x) != sorted(segt):
            fail(f"{scope}: records 的 target 集与 segment_targets 不是一一对应"
                 f"(记录 {len(btg)} 条 · 声称 {len(segt)} 包)")
        claimed = set()
        for br in brecs:
            tgt = br.get("target")
            bx = jd / base_full_junit_name(tgt or "")
            if not bx.is_file():
                fail(f"{scope}/{tgt}: 缺基线 junit {bx.name} —— 逐包守恒无从核")
                continue
            claimed.add(bx.name)
            for d in conservation_mismatches(br, parse_junit(bx), SKIP_WHITELIST):
                fail(f"{scope}: {d}")
        stray_b = sorted({x.name for x in xs} - claimed)
        if stray_b:
            fail(f"{scope}/junit 有 {len(stray_b)} 份没有任何包认领:{stray_b[:4]}")
        tot = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
        for f in xs:
            j = parse_junit(f)
            if j:
                for k in tot:
                    tot[k] += j[k]
        if tot["tests"] != t.get("tests"):
            fail(f"{scope}: junit 实体总数 {tot['tests']} != totals.tests {t.get('tests')}")
        if tot["failures"] + tot["errors"] != (t.get("red") or 0):
            fail(f"{scope}: junit 实体红 {tot['failures'] + tot['errors']} != totals.red")
        if tot["skipped"] != t.get("skipped"):
            fail(f"{scope}: junit 实体 skipped {tot['skipped']} != totals.skipped")
        for k in grand:
            grand[k] += tot[k]

    # ── 两段合起来:覆盖 == 冻结分母,且两段身份同尖同树 ──────────────
    missing = sorted(set(C["packages"]) - set(seen_targets))
    extra = sorted(set(seen_targets) - set(C["packages"]))
    if missing or extra:
        fail(f"两段合起来没覆盖满冻结分母(漏 {missing[:3]} · 多 {extra[:3]})")
    # 🔴 树内 TRANSPORT 是**权威**:哪些包该走宿主段由树说了算,不由产物说了算。
    want_host = set(C["mod"]._B.host_targets())        # noqa: SLF001
    got_host = {t for t, s in seen_targets.items() if s == "baseline_host"}
    if want_host != got_host:
        fail(f"宿主段覆盖的包 {sorted(got_host)} != 树内标注的 {sorted(want_host)}")
    if len(seg_ident) == 2:
        i1, i2 = seg_ident["baseline17"], seg_ident["baseline_host"]
        for k in ("tip", "tree"):
            if i1.get(k) != i2.get(k):
                fail(f"两段的 identity.{k} 不同({str(i1.get(k))[:12]} vs "
                     f"{str(i2.get(k))[:12]})—— 两个尖各跑一段,拼起来不是一份证据")
        if i1.get("started_at") == i2.get("started_at"):
            fail("两段的 started_at 逐字相同 —— 两段是分别跑的,不该撞上同一秒")
    if _s8.clean:
        ok(f"{len(C['packages'])} 包 = 段①容器 {len(C['packages']) - len(want_host)} + "
           f"段②宿主 {len(want_host)} · 合计 {grand['tests']} 跑 / 红 "
           f"{grand['failures'] + grand['errors']} / skip {grand['skipped']} · "
           "两段同尖同树 · 逐包守恒逐项对得上")

    print("=== 9 零写入自证:跑后快照 ===")
    after = snapshot()
    if after != before:
        diff = sorted(set(before) ^ set(after)) or \
            [k for k in before if before[k] != after.get(k)]
        fail(f"本工具动了证据!{diff[:5]}")
    else:
        ok(f"跑前跑后 {len(after)} 份文件的 大小/mtime_ns/sha256 **逐项相同** —— 零写入")

    print()
    print(("XX 未过 " + str(len(_bad)) + " 项") if _bad else "OK 全项通过")
    return 1 if _bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
