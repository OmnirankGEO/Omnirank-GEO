# -*- coding: utf-8 -*-
"""两个复放器(V3-C / V4-C)的**公共硬门谓词** —— 一条谓词只许有一处实现。

═══ 为什么要有这个模块 ═══

Codex fix-of-fix2 的 P1-7 与 P1-8 是**同一个病的两面**:V4-C 有基线硬门、
有逐变量 DSN 机械枚举,V3-C 没有;而两支脚本的 ``_run`` / ``_apply`` /
``_restore`` / ``_assert_tree_is_unmutated`` / 主循环是**逐行抄过去**的。
抄的时候把闸落下了 —— 这正是本仓记过的

    「同一谓词写两处 ⇒ 必有一处没人验」

所以本单不是"把 V4-C 的闸复制到 V3-C",那只会让下一支复放器再漏一次。
闸搬到这里,两支都 import;判据 ``scripts/test_mutation_replayer_hardgate_contract.py``
用 AST 机械枚举 ``scripts/mutation_replay_*.py``,**谁自己再写一份就红**。

本模块只放**纯谓词**(不起容器、不跑 pytest),所以它的每一条都能在
没有 Docker 的机器上被判据直接打。
"""
from __future__ import annotations

import ast
import hashlib
import re
import subprocess
from pathlib import Path

# ── 起跑前:残留 / 锚点过期 / 干净,三态分开 ───────────────────────────────
RESIDUE = "RESIDUE"
EXPIRED = "EXPIRED"
CLEAN = "CLEAN"


def classify_target_state(*, differs_from_head: bool, anchor_hits: int) -> str:
    """靶文件的三态 —— **纯函数**,好让这道闸自己被判据直接打。

    🔴 上一版用「替换文本在不在树上」判残留,两个洞:

      · **anchor-preserving 替换**(V4-C 的 MUT-V4C-C1b 把锚行重新插在末尾)——
        崩溃残留下来时锚点**照样恰好命中 1 次**,旧闸判"干净"直接放行。
        实测:`当前 V4-C 判定:干净,放行`。后果是拿污染树当基线,
        再把污染版 ``copy2`` 存成 ``.mutbak`` —— 备份本身就脏了。
      · **空串替换**(MUT-V4C-C2a 是删除型)—— ``repl and repl in raw``
        永远为假,于是残留被扔进"锚点过期"栏。实测:
        `含【变异残留】=False · 含【锚点过期】=True`。
        它会安全停机,但给出的修复动作是**错的**:把人引去找不存在的过期。

    换成「与 HEAD 逐字节是否相同」之后,两个洞由**同一个信号**关掉:

      · 与 HEAD 不同           ⇒ 树被改过 ⇒ 残留(替换长什么样都无所谓);
      · 与 HEAD 相同、锚点不在 ⇒ 被测代码真的变了 ⇒ 锚点过期;
      · 与 HEAD 相同、锚点恰 1 ⇒ 干净。

    (本仓判例:存量红先分三种 —— 真缺陷 / 锚点过期 / 尺子坏。这里是同一手法。)
    """
    if differs_from_head:
        return RESIDUE
    return CLEAN if anchor_hits == 1 else EXPIRED


def git_differs_from_head(root: Path):
    """返回 ``rel -> bool``:该文件在盘上与 HEAD 是否不同。

    🔴 用 ``git diff --quiet HEAD -- <rel>`` 而不是「blob 的 sha256 对比」:
       前者是 git 自己的比较,**过滤器感知**(``.gitattributes`` 的 eol / clean
       filter)。本仓 ``core.autocrlf=false`` 时两者一致,但把正确性押在
       "配置现在恰好是这个值"上,就是又一条会烂的前提。
    """

    def _differs(rel: str) -> bool:
        tracked = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                                 cwd=str(root), capture_output=True)
        if tracked.returncode != 0:
            raise SystemExit(
                f"停:靶文件 {rel} **不在 git 索引里** —— "
                "复放证据必须出自最终 commit 的树,未跟踪的文件比不出「有没有残留」。")
        r = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", rel],
                           cwd=str(root), capture_output=True)
        if r.returncode not in (0, 1):
            raise SystemExit(
                f"停:比不出 {rel} 与 HEAD 的差异(git rc={r.returncode}):"
                f"{r.stderr.decode('utf-8', 'replace')[:400]}")
        return r.returncode == 1

    return _differs


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def assert_tree_is_unmutated(root: Path, muts, *, differs_from_head=None) -> None:
    """起跑前自证:每一发的每一个锚,都必须落在 ``CLEAN`` 这一格。

    ``differs_from_head`` 可注入(判据用假的,真跑用 git)—— 抽出这个口子的
    唯一目的就是让这道闸**在没有 git / 没有 Docker 的环境里也能被判据打**。
    """
    differs = differs_from_head or git_differs_from_head(root)
    files = sorted({m["file"] for m in muts})
    dirty = {f: differs(f) for f in files}
    raw_of = {f: (root / f).read_text(encoding="utf-8-sig") for f in files}

    residue, expired = [], []
    for mut in muts:
        rel = mut["file"]
        for i, (anchor, _repl) in enumerate(mut["edits"]):
            hits = raw_of[rel].count(anchor)
            state = classify_target_state(differs_from_head=dirty[rel],
                                          anchor_hits=hits)
            if state == CLEAN:
                continue
            where = f"{mut['id']} 锚{i} @ {rel}(命中 {hits} 次)"
            (residue if state == RESIDUE else expired).append(where)
    if not residue and not expired:
        return
    msg = ["停:起跑前自证未过 —— 一发都不跑(零 .mutbak、零产物)。"]
    if residue:
        msg.append(
            "【变异残留】这些靶文件与 HEAD **逐字节不同**:"
            + "; ".join(sorted(set(residue)))
            + " —— 要么上一次跑被中途杀掉没还原,要么树上有未提交的改动;"
              "两者都必须先处理(报的绿必须出自最终 commit 的树)。"
              "🔴 注意:锚点命中 1 次**不代表**干净 —— anchor-preserving 的"
              "替换会把锚原样留在树上。")
    if expired:
        msg.append(
            "【锚点过期】这些靶文件与 HEAD 完全一致、锚却不在:"
            + "; ".join(sorted(set(expired)))
            + " —— 这不是残留,是被测代码变了,该退役或改锚并写明继任者。"
              "🔴 只动锚一条轴,不自行改语义。")
    raise SystemExit(" ".join(msg))


# ── 基线:红 / 非零 rc 都算脏,第一发之前停 ────────────────────────────────
def baseline_offenders(observed: dict) -> list[str]:
    """基线里"不干净"的包 —— **纯函数**,好让这道闸自己也能被判据打。

    判脏的两个信号缺一不可:
      · ``red`` 非空 —— 有判据在基线上就是红的;
      · ``rc != 0`` —— rc=2(collect 错)/ rc=5(一条都没收)时 junit 里
        可能一条红都没有,而那正是"整包起不来"的样子。
    """
    out = []
    for pkg, (rc, red) in sorted(observed.items()):
        if red or rc != 0:
            out.append(f"{pkg}(rc={rc} 红={sorted(red) or '无'})")
    return out


def assert_baseline_clean(observed: dict) -> None:
    """基线脏 ⇒ **在第一发变异之前**非零退出,零落盘。

    脏基线不是"结果差一点":每一发的"新增红"是 ``red - baseline``,
    基线里那几条红会被**当成正常值扣掉** —— 一个本该被抓住的变异,
    只要它打红的恰好是基线里已经红的那几条,就显示成"存活/红集精确"。
    **结论方向可能整个反过来。**
    """
    dirty = baseline_offenders(observed)
    if dirty:
        raise SystemExit(
            "停:基线不干净,**一发变异都不跑**(零落盘)。"
            "脏基线会被当成正常值从每一发的新增红里扣掉,"
            "把该抓住的变异显示成存活。实况:" + " | ".join(dirty))


# ── 变异臂:rc 白名单 + junit 用例数守恒 ──────────────────────────────────
#: pytest 的可信退出码:0 = 全绿,1 = 有失败。其余(2 中断 / 3 内部错 /
#: 4 用法错 / 5 一条都没收集到)意味着**这一轮根本没跑完**,
#: 而"没跑完"与"跑完了只红了预期那几条"在红集里长得一模一样。
TRUSTWORTHY_PYTEST_RCS = frozenset({0, 1})


def assert_mutation_arm_trustworthy(mid: str, pkg: str, *, rc: int,
                                    total: int, baseline_total: int) -> None:
    """[V5-B · B-3①] 变异臂的读数可不可信 —— **在红集裁定之前**判。

    🔴 Codex 的反例:``rc=2`` 配一份"只含预期红集"的 partial junit,
       旧代码只把 rc 打印出来,照样算出「红集精确」并返回 0。
       两个信号都要:rc 在白名单里 **且** 收集到的用例数与基线一致。
       只看 rc 挡不住"跑了一半就崩";只看条数挡不住 rc=5 的空轮。
    """
    if rc not in TRUSTWORTHY_PYTEST_RCS:
        raise SystemExit(
            f"停:{mid} 的 {pkg} 变异臂 pytest rc={rc},不在可信白名单 "
            f"{sorted(TRUSTWORTHY_PYTEST_RCS)} —— 这一轮没跑完,"
            "而「没跑完」与「只红了预期那几条」在红集里长得一模一样,"
            "不许拿它做红集裁定。")
    if total != baseline_total:
        raise SystemExit(
            f"停:{mid} 的 {pkg} 变异臂收集到 {total} 条用例,基线是 "
            f"{baseline_total} 条 —— 分母变了,红集差值不成立"
            "(少收的那些判据不会红,而不红正是「存活」的观测形态)。")


# ── DSN:逐包机械枚举 + 逐变量喂全 ────────────────────────────────────────
def package_dsn_vars(root: Path, path: str) -> set[str]:
    """一个包**真正读**的 DSN 环境变量 —— AST 机械枚举,不靠惯例名。

    🔴 读文件用 ``utf-8-sig``:带 BOM 的文件用 ``utf-8`` 读会 ``ast.parse``
       失败,而失败在旧版里是 ``continue`` —— **静默跳过 = 分母洞**,
       而分母洞不会让任何判据变红。所以这里额外把解析失败**数出来**并停机。
    """
    found: set[str] = set()
    broken: list[str] = []
    for f in sorted((root / path).rglob("*.py")):
        if f.stat().st_size > 2_000_000:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8-sig"))
        except (SyntaxError, ValueError) as e:
            broken.append(f"{f.relative_to(root).as_posix()}: {e}")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            fname = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if fname not in ("getenv", "get"):
                continue
            a0 = node.args[0]
            if not (isinstance(a0, ast.Constant) and isinstance(a0.value, str)):
                continue
            if re.search(r"(DSN|DB_URL|DATABASE_URL)$", a0.value):
                found.add(a0.value)
    if broken:
        raise SystemExit(
            f"停:{path} 下有文件解析不了,DSN 枚举的**分母有洞**:"
            + "; ".join(broken[:5]))
    return found


def env_vars_of(spec: dict) -> set[str]:
    """一个包声明喂了哪些 DSN 变量(``env`` 既可以是元组也可以是 var→db 映射)。"""
    env = spec["env"]
    return set(env.keys()) if isinstance(env, dict) else set(env)


def env_partition_of(spec: dict) -> frozenset:
    """DSN 变量按"共用哪一个库"分组后的**结构** —— 库名不参与比较。

    用来跨复放器对账:同一个包在两支复放器里必须是同一个分法。
    (V3-C 把 w4 的两个变量指到同一个库、V4-C 分成两个库,就是被这条抓住的。)
    """
    env = spec["env"]
    if not isinstance(env, dict):
        return frozenset({frozenset(env)})
    groups: dict[str, set[str]] = {}
    for var, db in env.items():
        groups.setdefault(db, set()).add(var)
    return frozenset(frozenset(v) for v in groups.values())


def schema_rebuilder_files(root: Path, path: str) -> list[str]:
    """包内**重建 schema** 的文件清单(含 ``DROP SCHEMA`` 的 ``.py``)。

    两个以上的文件各自 ``DROP SCHEMA public CASCADE`` 时,它们必须落在
    **不同的库**上,否则两边互相拆台 —— 而拆台的表现是间歇性的结构竞态,
    四个信号(全绿 / 条数 / 两臂差值 / rc)全部正常。
    """
    out = []
    for f in sorted((root / path).rglob("*.py")):
        if f.stat().st_size > 2_000_000:
            continue
        if "DROP SCHEMA" in f.read_text(encoding="utf-8-sig", errors="replace"):
            out.append(f.relative_to(root).as_posix())
    return out


def assert_every_dsn_var_is_declared(root: Path, packages: dict, port: int,
                                     *, label: str = "") -> None:
    """机械证明:每个包读的**每一个** DSN 变量都被本复放器喂了,且不共库。

    这条闸的由来是一次真事故:w4 的 HTTP 判据只认 ``DEFGEO_W4_HTTP_DB_URL``,
    按惯例只喂 ``TEST_DATABASE_URL`` / ``DEFGEO_W4_TEST_DB_URL`` 时,
    那 9 条判据整轮都打在**别窗**的 55475 容器上 —— 而全绿、条数正常、
    两臂差值自洽,四个信号全部正常(因为两臂打在同一个错库上)。
    """
    holes, shared = [], []
    for pkg, spec in packages.items():
        need = package_dsn_vars(root, spec["path"])
        # DATABASE_URL 由 _run 无条件设置(root conftest 用它),不必逐包声明
        need.discard("DATABASE_URL")
        missing = sorted(need - env_vars_of(spec))
        if missing:
            holes.append(f"{pkg} 漏喂 {missing}(该包实际读:{sorted(need)})")
        rebuilders = schema_rebuilder_files(root, spec["path"])
        if len(rebuilders) > 1 and len(env_partition_of(spec)) < 2:
            shared.append(
                f"{pkg} 有 {len(rebuilders)} 个文件各自 DROP SCHEMA"
                f"({rebuilders}),却把 {sorted(env_vars_of(spec))} 全指到同一个库")
    if holes or shared:
        parts = ["停:DSN 声明不合格" + (f"({label})" if label else "") + " ——"]
        if holes:
            parts.append("【漏喂】那些判据会落到各自的**默认**库(往往是别窗容器),"
                         "而结果看起来完全正常:" + " | ".join(holes))
        if shared:
            parts.append("【共库】两个 schema 重建者指到同一个库会互相拆台,"
                         "表现是间歇性结构竞态:" + " | ".join(shared))
        raise SystemExit(" ".join(parts))
    print(f"DSN 自证{('(' + label + ')') if label else ''}:"
          f"{len(packages)} 个包读到的 DSN 变量全部指向 localhost:{port} 的本轮临时库")


def dsn_env(base_env: dict, spec: dict, port: int, *,
            host: str = "localhost", user: str = "geo_admin",
            pw: str = "testpw") -> tuple[dict, str]:
    """按 ``spec['env']`` 逐变量喂 DSN,返回 ``(env, 主库 DSN)``。

    🔴 这里是 V3-C 那条 P1-7 的原址:旧代码是 ``for key in spec["env"]:
       env[key] = dsn`` —— **一个 DSN 灌所有 key**。
       元组形态照样是"全部指主库",但 var→db 映射形态必须逐个查,
       不能再退回统一赋值。写成公共谓词,两支复放器就不可能再各写一遍。
    """
    def _dsn(db: str) -> str:
        return f"postgresql://{user}:{pw}@{host}:{port}/{db}"

    env = dict(base_env)
    e = spec["env"]
    if isinstance(e, dict):
        for key, db in e.items():
            env[key] = _dsn(db)
    else:
        for key in e:
            env[key] = _dsn(spec["db"])
    env["DATABASE_URL"] = _dsn(spec["db"])
    return env, _dsn(spec["db"])

def red_nodeid(classname: str | None, name: str | None) -> str:
    """junit `<testcase>` → **红集展示**用的短 nodeid(模块尾名 :: 用例名)。

    🔴 [V7-B] 抽出来的理由:`gate9_full_denominator_baseline.run_target` 里内联着
       这个拼法,而只读验收器要解析**同一批 XML** 做三向对账。
       两处各写一份 ⇒ 哪天有人改了其中一处,验收器会在生产上报一片假红,
       而那片红长得跟"证据被篡改"一模一样。

    🔴🔴 [fof8 P1-2 · 2026-08-31 订正] 上一版这句 docstring 写的是
       「**一条谓词只许有一处实现**」—— 规矩没错,**粒度错了**。
       「红集怎么显示」与「两趟跑的是不是同一条用例」是**两条不同的谓词**,
       它们碰巧可以用同一个拼法实现,于是被合成了一处。后果:
       `classname.split('.')[-1]` 丢掉整个模块路径,**同尾名异模块被压成同一身份**
       (Codex 端到端反例:把一份 mutation XML 的 classname 从
        `tests.defgeo_funding_p0_2026_08_25.test_a1_platform_leg_pg` 改成
        `codex_shadow.test_a1_platform_leg_pg`,保留尾名与用例名,完整重冻之后
        三参验收**仍 rc=0**)。
       身份比较一律用 `testcase_identity()`,**不许**复用本函数。

    「同一谓词写两处必有一处没人验」的**反面**同样是病:
    **两条不同的谓词合成一处**,弱的那条会把强的那条悄悄降级。
    """
    return f"{(classname or '').split('.')[-1]}::{name}"


def testcase_identity(classname: str | None, name: str | None) -> tuple[str, str]:
    """junit `<testcase>` → **无损身份**,返回二元组 `(classname, name)`。

    用途只有一个:回答「基线那一趟与变异那一趟跑的是**同一条**用例吗」。
    差集 `xml_red - base_red` 的全部意义建立在这句话上,所以这里**一个字符都不许丢,
    也不许粘连**。

    🔴🔴 [fof9 P1 · 2026-09-01] 上一版返回 `f"{classname}::{name}"` —— 分隔符**裸拼**。
       后果是同一族病的**第二形**:
         · fof8 那一形是**截断**(取模块尾名)⇒ 丢掉模块前缀;
         · 这一形是**粘连**(拼接不转义)⇒ 丢掉**字段边界**。
       Codex 反例:`("qa::boundary", "same_case")` 与 `("qa", "boundary::same_case")`
       序列化成同一个 `qa::boundary::same_case`;在完整证据副本上同步改基线与
       10 份 mutation junit 并**完整重冻**(六份 SUMS + 根锚)之后,
       三参验收**仍 rc=0**。

       教训不是「换个更罕见的分隔符」——那只是把碰撞概率调小,病还在。
       **有结构的东西就别拍平成字符串**:集合、差集、去重全部在元组空间里做。

    🔴 若将来真需要一个字符串形态(落盘 / 打日志),**不许**在这里加分隔符拼接:
       用 JSON 数组(`json.dumps([classname, name])`)或长度前缀。
       本仓当前**不需要** —— 这些身份集合全部是进程内的,从不落盘
       (判据 `test_ev_j04` 钉住「没有任何消费方把它拼成字符串」)。

    与 `red_nodeid()` 的分工是硬的:
      · `red_nodeid`        —— 展示 / 与产物里已用短式记录的红集比对(有损,可接受);
      · `testcase_identity` —— 跨趟身份(无损,不许有损)。
    合成一条就是 fof8 P1-2;把它拍平成字符串就是 fof9 P1。
    """
    return (classname or "", name or "")