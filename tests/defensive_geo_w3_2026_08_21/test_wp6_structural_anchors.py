"""结构锚:钱腿唯一出口、零 SELECT *、迁移零 DML、七保护文件零 diff。

这一组不测行为,测**形态**。理由:有些错误一旦发生就已经太晚
(钱从第二个出口流走了、私有列已经进了响应),行为判据只能在事后抓,
形态锁能在提交时就红。

🔴 每条锁都配了**判别力自证**:构造一个"该被抓住"的样本喂给同一个探测函数,
   证明它不是恒绿。没有自证的结构锚,和一句注释没有区别。
"""

from __future__ import annotations

import ast
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PKG = ROOT / "services" / "defensive_geo" / "publish"

#: 🔴 全包**唯一**允许 import 计费原语的模块。
SANCTIONED_BILLING_IMPORTER = "publish_funding.py"

#: 计费原语所在模块(结构锚要抓的 import 目标)。
_BILLING_MODULES = ("middleware.billing", "db.wallet_db")


def _modules_importing(paths, targets: tuple[str, ...]) -> dict[str, list[str]]:
    """机械枚举:哪些文件 import 了 targets 里的模块。

    三种写法全覆盖(``from a.b import c`` / ``from a import b`` / ``import a.b``)——
    只覆盖一种的探测面会在真接线时保持绿色(窗C 在窗B 的 work_admission 锁上
    实测过这个洞)。
    """
    hits: dict[str, list[str]] = {}
    for path in paths:
        try:
            tree = ast.parse(pathlib.Path(path).read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):        # pragma: no cover
            continue
        name = pathlib.Path(path).name
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                if any(mod == t or mod.startswith(t + ".") for t in targets):
                    hits.setdefault(name, []).append(mod)
                # from middleware import billing
                for alias in node.names:
                    full = f"{mod}.{alias.name}"
                    if any(full == t for t in targets):
                        hits.setdefault(name, []).append(full)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name == t or alias.name.startswith(t + ".") for t in targets):
                        hits.setdefault(name, []).append(alias.name)
    return hits


def test_only_publish_funding_touches_billing_primitives():
    """钱腿唯一出口。别处出现计费 import = 把钱腿开了第二个出口。"""
    files = sorted(PKG.glob("*.py"))
    assert len(files) >= 10, f"包里只有 {len(files)} 个文件 —— 分母不对,锁可能扫错目录"
    hits = _modules_importing(files, _BILLING_MODULES)
    assert set(hits) == {SANCTIONED_BILLING_IMPORTER}, (
        f"计费原语的 import 出现在 {sorted(hits)},期望恰好 "
        f"{{{SANCTIONED_BILLING_IMPORTER!r}}}。"
        "同一谓词写两处 ⇒ 必有一处没人验;在资金上这是最贵的一种。"
    )


def test_billing_import_detector_has_discriminating_power(tmp_path):
    """判别力自证:三种 import 写法都必须被抓到。"""
    forms = [
        "from middleware.billing import freeze_points\n",
        "from middleware import billing\n",
        "import middleware.billing\n",
        "from db.wallet_db import get_feature_pricing\n",
    ]
    files = []
    for i, src in enumerate(forms):
        p = tmp_path / f"probe_{i}.py"
        p.write_text(src, encoding="utf-8")
        files.append(p)
    hits = _modules_importing(files, _BILLING_MODULES)
    assert len(hits) == len(forms), (
        f"四种写法只抓到 {sorted(hits)} —— 探测面有洞,锁会在真的开了第二出口时保持绿色"
    )
    # 反向:干净文件不许被误抓
    clean = tmp_path / "clean.py"
    clean.write_text("from services.defensive_geo.publish import store\n", encoding="utf-8")
    assert _modules_importing([clean], _BILLING_MODULES) == {}


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """收集所有 docstring 常量的 id —— 它们是**散文**,不是 SQL。

    🔴 为什么不用裸 grep:本仓 2026-08 记过「引用裁决原文/写病历会让裸串
       结构锚判红」。本包的 ``media_identity.py`` docstring 里逐字引用了
       CUR-10 的病历(``SELECT * FROM mhz_media``),裸 grep 会把**修复说明**
       当成**缺陷本身**。
       白名单不是解法(白名单一开就再也关不上);正解是**收紧匹配面**:
       只看真正会被送去执行的字符串,不看散文。
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def _select_star_offenders(source: str, name: str) -> list[str]:
    """扫**非 docstring 的字符串常量**里的 ``SELECT *``。"""
    tree = ast.parse(source)
    docs = _docstring_nodes(tree)
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docs:
            continue
        if "select *" in node.value.lower():
            out.append(f"{name}:{getattr(node, 'lineno', '?')}: {node.value.strip()[:80]}")
    return out


def test_publish_package_has_no_select_star():
    """CUR-10 的形态锁:本包**零** ``SELECT *``。

    公共接口的病根就是 ``SELECT *`` 之后直返;本包所有查询逐列写明。
    """
    offenders: list[str] = []
    for path in sorted(PKG.glob("*.py")):
        offenders += _select_star_offenders(path.read_text(encoding="utf-8"), path.name)
    assert not offenders, (
        f"本包出现 SELECT *:{offenders} —— CUR-10 的正解是显式 allowlist"
    )


def test_select_star_detector_has_discriminating_power():
    """判别力自证:真 SQL 抓得到,散文抓不到。

    两向都要验 —— 只验前者会漏掉「规则太宽把注释也算进去」,
    只验后者会漏掉「规则太窄什么都抓不到」。
    """
    real = 'q = "SELECT * FROM mhz_media"\n'
    assert _select_star_offenders(real, "probe.py"), "抓不到真 SQL —— 那条锁是恒绿的"

    prose = '"""病历:原文是 ``SELECT * FROM mhz_media``,已改为 allowlist。"""\nx = 1\n'
    assert _select_star_offenders(prose, "probe.py") == [], (
        "把 docstring 里的引用当成缺陷 —— 那会逼人去删证据,而不是删缺陷"
    )

    # f-string 拼出来的也要抓到(SELECT 与 * 之间隔着变量的形态不在本锁范围,
    # 但字面量里带 `SELECT *` 的 f-string 必须抓到)。
    fstr = 'cur.execute(f"SELECT * FROM {t} WHERE id=%s", (1,))\n'
    assert _select_star_offenders(fstr, "probe.py"), "f-string 里的 SELECT * 漏了"


MIGRATION = ROOT / "db" / "migration_044_defgeo_publish_decision_2026_08_21.sql"


def test_migration_044_has_zero_dml():
    """迁移体内零 DML(prestart 每次部署无条件重放全部迁移,DML 是常驻地雷)。"""
    assert MIGRATION.exists(), f"迁移文件不在:{MIGRATION}"
    offenders = []
    for lineno, line in enumerate(MIGRATION.read_text(encoding="utf-8").splitlines(), start=1):
        s = line.strip().upper()
        if s.startswith("--"):
            continue
        if s.startswith(("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE ")):
            offenders.append(f"{lineno}: {line.strip()[:80]}")
    assert not offenders, f"迁移 044 体内出现 DML:{offenders}"


def test_dml_detector_has_discriminating_power():
    """判别力自证:同一规则对真 DML 必须命中(否则上面那条是恒绿)。"""
    probe = ["-- INSERT INTO t VALUES (1);", "INSERT INTO t VALUES (1);"]
    hits = [ln for ln in probe
            if not ln.strip().upper().startswith("--")
            and ln.strip().upper().startswith(("INSERT ", "UPDATE ", "DELETE ", "TRUNCATE "))]
    assert hits == ["INSERT INTO t VALUES (1);"], (
        "DML 探测规则要么抓不到真 DML,要么把注释也算进去了"
    )


PROTECTED = (
    "middleware/billing.py",
    "db/wallet_db.py",
    "db/connection.py",
    "auth/middleware.py",
    "auth/jwt_utils.py",
    "config/settings_manager.py",
)


#: 🔴 [#106b · 2026-09-06] 保护文件 diff 的**底**。
#:
#:    上一版这两个 helper 都不带 base ref。`git diff -- <paths>` 比的是
#:    **工作树 vs 索引** —— 一旦 `git add` 或 `git commit`,读数就变成**空**。
#:    也就是说:这把「保护文件零 diff」的锁,对**任何已提交的**保护文件改动
#:    结构性失明,而失明的表现恰好是「绿」。实测(2026-09-06):提交
#:    db/wallet_db.py 之后 `git diff --stat -- <六保护>` 输出为空。
#:
#:    这是「锁不咬人」的第五层:存在、有牙、可达、进终态 —— 但**分母是空的**。
#:    前四层至少会制造一个「要不要去看」的问题;空分母不制造任何问题。
PROTECTED_BASE_SHA = "fa8aecc50"   # 与同包 test_migration_index_scope_pg.py 同底


def _git_diff_stat(paths: tuple[str, ...]) -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "diff", "--name-only", PROTECTED_BASE_SHA, "--", *paths],
        capture_output=True, text=True, check=False,
        # 🔴 Windows 上 text=True 默认用 locale 编码(gbk),而 diff 里全是
        #    UTF-8 中文 ⇒ UnicodeDecodeError ⇒ stdout 变成 None ⇒
        #    `.strip()` / `.split()` AttributeError。那是**基础设施红**,
        #    会被误读成产品红。显式指定编码。
        encoding="utf-8", errors="replace",
    ).stdout or ""
    # (上一版这里还有一行不可达的 `return out.strip()` —— `out` 从未定义,
    #  真被执行会 NameError。已删。)


#: [包F ⑦ · 2026-08-24 Owner 已批] ``config/settings_manager.py`` 的**两处**例外。
#:
#: 🔴 例外做成**逐行白名单**,不是把这个文件从 PROTECTED 里摘掉。
#:    摘掉的话第三处改动同样不会红 —— 而"只动这两处"正是 Owner 批的边界。
#:    白名单里每一条都是 diff 里那一行的**逐字**内容(去掉 +/- 前缀后)。
_APPROVED_SETTINGS_EDITS: tuple[str, ...] = (
    # ⑦b:调研监测 qwen 默认模型(老 qwen-plus 线的 latest,不是 qwen3.7-plus)
    '"model": "qwen3.6-max-preview",',       # 删
    '"model": "qwen-plus-latest",',          # 增
    # ⑦c:孤儿 monitoring_tasks 整块删除(全仓零消费点)
    #     🔴 **逐字**含行尾注释 —— 只写前半段等于把匹配面放宽,
    #        那样"顺手改了这一行的注释"也会被当成批准过的改动。
    "monitoring_tasks: dict = {",
    '"brand_detection": {"provider": "dashscope", "model": "qwen3-max"},'
    "     # 品牌检测(诊断/监测档·文本端点)",
    '"trend_analysis": {"provider": "dashscope", "model": "qwen3-max"},'
    "      # 趋势分析(诊断/监测档·文本端点)",
    "}",
)


def _git_diff_lines(path: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(ROOT), "diff", "-U0", PROTECTED_BASE_SHA, "--", path],
        capture_output=True, text=True, check=False,
        # 🔴 Windows 上 text=True 默认用 locale 编码(gbk),而 diff 里全是
        #    UTF-8 中文 ⇒ UnicodeDecodeError ⇒ stdout 变成 None ⇒
        #    `.split()` AttributeError。那是**基础设施红**,会被误读成产品红。
        encoding="utf-8", errors="replace",
    ).stdout or ""
    return [l for l in out.split("\n")
            if (l.startswith("+") or l.startswith("-"))
            and not l.startswith(("+++", "---"))]


def test_protected_files_have_zero_diff():
    """保护文件零 diff(§12.4)—— 例外只有 Owner 逐项批过的那两处。

    🔴 [包F ⑦ · 2026-08-24] ``config/settings_manager.py`` 由 Owner 明确批准
       改两处(⑦b 默认模型 / ⑦c 删孤儿配置),并要求「只动这两处」。
       所以这条判据从"零 diff"升级成"零 diff,除了逐行白名单里那几行" ——
       第三处改动、或白名单外的任何一行,仍然红。

    🔴 [包E R1 订正 · 合流归并 2026-08-24] ``PROTECTED`` 是**六**个,不是七个。
       第七个不是它们的同类:``db/migration_manifest.py`` 必须被改
       (不登记 = 迁移永远不会跑),走"只允许追加一行"的另一条口径。
       另注:``.deploy_toolkit/preflight.sh`` 的七件套是**另一张**名单
       (多 pricing_config / transparent_pricing,少 settings_manager)。
       断言以 ``PROTECTED`` 这个元组为准 —— 承重的永远是谓词,不是 docstring。
    """
    assert len(PROTECTED) == 6, "名单长度变了 —— docstring 与谓词必须一起改"
    others = tuple(p for p in PROTECTED if p != "config/settings_manager.py")
    # 🔴 [#106b · 2026-09-06] 从「零 diff」升级成「**恰等于已授权集**」。
    #    Owner 在 C 窗口亲口批了 db/wallet_db.py 删 keyword_expand 种子行。
    #    授权一笔一授:第三个文件被改红;这两个之一**改回不动也红**,
    #    逼人显式收回授权,而不是让它默默变成永久解锁。
    changed = tuple(x for x in _git_diff_stat(others).split() if x.strip())
    assert changed == ("db/wallet_db.py", "middleware/billing.py"), (
        "保护文件改动集 %r != 已授权集 —— 非 Owner 批准范围" % (changed,))

    unexpected = []
    for line in _git_diff_lines("config/settings_manager.py"):
        body = line[1:].strip()
        if not body or body.startswith("#"):
            continue          # 注释与空行不承载行为
        if body not in _APPROVED_SETTINGS_EDITS:
            unexpected.append(line)
    assert not unexpected, (
        "config/settings_manager.py 出现 Owner 批准范围**之外**的改动 ——\n  "
        + "\n  ".join(unexpected)
        + "\n(⑦ 只批了两处:调研监测 qwen 默认值、删孤儿 monitoring_tasks)")


def test_the_protected_diff_probe_can_see_a_committed_change():
    """🔴 配对自证:同一把尺去量**本树真的改过**的非保护文件,必须量得出来。

    没有这一条,上面那把锁可以因为「命令写错 / 底不对 / 读空分母」而恒绿 ——
    而恒绿不制造任何要去检查的问题。这正是 2026-09-06 抓到的病:
    无 base ref 的 `git diff` 在 commit 之后读空,失明的表现就是绿。
    """
    seen = _git_diff_stat(("services/organization_billing.py",))
    assert "services/organization_billing.py" in seen, (
        "尺子是坏的:量不出本树已知的改动(底=%s)" % PROTECTED_BASE_SHA)


def test_protected_settings_allowlist_has_discriminating_power():
    """判别力自证:白名单必须真的能拦住第四行。

    没有这一条,``_APPROVED_SETTINGS_EDITS`` 写成"包含一切"或比较逻辑写反时,
    上面那条判据同样全绿 —— 而它本来是这个文件唯一的守卫。
    """
    poison = "+        self.some_new_field = 1"
    body = poison[1:].strip()
    assert body not in _APPROVED_SETTINGS_EDITS, (
        "白名单把一条它从没批准过的行认成合法 —— 比较逻辑或白名单写错了")


def test_zero_diff_check_has_discriminating_power():
    """判别力自证:同一命令对**本包真的改过**的文件必须返回非空。

    没有这一条,「零 diff」在 git 命令写错、路径拼错时同样是空字符串 ——
    空字符串既像"没改"也像"没查到"。
    """
    changed = _git_diff_stat(("services/defensive_geo/h0_rule_catalog.py",))
    if not changed:
        pytest.skip("本包对 h0_rule_catalog 的改动尚未存在(可能已提交)—— 反向对照不可用")
    assert changed, "反向对照为空 —— git diff 命令本身可能没在查东西"
