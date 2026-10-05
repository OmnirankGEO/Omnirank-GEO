"""B 类判据 · **不可达行的调用点普查锁**
(工单 WO_PLAN_SIDE_MISMATCH_UPSTREAM_2026-09-03 §7)。

## 为什么这一族不是"注入判据"

`question_plan.py` 里 14 处 `raise PlanIdentityError`,工单原稿要求"每行一条注入判据"。
普查后发现其中一部分**任何 HTTP 路径都到不了**:

    apply_edit_revision / remove_question  → 全生产树**零调用者**(:329 :346 :348)
    run_request_hash                       → 唯一调用点在 create_run_preview 内(:246),
                                             而黄框只有一个 setter,来源是 question-plan preview

给这些行写注入判据 = **造一批永远不会红的判据**:它们不会被调用,注入什么都不发生,
判据恒绿,而恒绿的判据不制造任何问题,会被当成"覆盖到了"。

所以这一族锁的**不是"现在不会发生"**,而是 **"不许在没人注意时变成会发生"**:
将来有人把 `remove_question` 接到某个端点上,本文件当场红,那时才需要上游预防。

## 为什么分母必须机械枚举

调用点用 `ast` 走,不 grep:
  · grep 会把注释、docstring、字符串里的同名词算进来(散文提及触发裸串锁,栽过多次);
  · 也会漏掉 `getattr(mod, "remove_question")` 这类间接调用 —— 所以本文件**同时**
    用 AST 查直呼、用字符串查间接引用,两条都必须为空。

🔴 每条锁都配**正样本**:同一条提取器必须能在 `validate_plan` 上查出真实调用点。
   没有正样本时,"查出 0 个"与"提取器坏了"读数完全相同。
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
QP = ROOT / "services" / "defensive_geo" / "question_plan.py"
API = ROOT / "api" / "defensive_geo_api.py"

#: 生产代码目录。**机械枚举**:凡是 import 得到 question_plan 的地方都在这里面。
#: 🔴 不含 tests/ —— 判据自己调这些函数是正常的,把它算进"生产调用点"会让锁恒红。
PROD_DIRS = ("api", "services", "workflows", "agents", "db", "middleware",
             "tools", "utils", "config", "auth", "advisors", "writing")


def _prod_py_files() -> list[Path]:
    out: list[Path] = []
    for d in PROD_DIRS:
        base = ROOT / d
        if base.is_dir():
            out.extend(p for p in base.rglob("*.py"))
    for name in ("server.py", "main.py"):
        p = ROOT / name
        if p.is_file():
            out.append(p)
    assert out, "分母为空 —— 提取器坏了,不是'生产代码里没有 py 文件'"
    return out


def _read_text_or_none(p: Path) -> str | None:
    """读不出 UTF-8 文本就返回 None —— 由调用方退到**字节级**判断。

    🔴 生产树里确实有一个:`tools/scoring/geo_scorer_backup.py`(90KB,已被 git 跟踪,
       内容是二进制而扩展名是 .py)。**不给它开豁免名单** ——
       豁免会随时间长出第二个、第三个,而每一个都是分母上的洞。
       改成:文本读不出来时按**字节**找函数名。找不到 ⇒ 它确实不含调用者(结论成立);
       找得到 ⇒ 照样红(结论不被绕过)。这样分母是完整的,不靠"相信它没事"。
    """
    try:
        return io.open(p, encoding="utf-8").read()
    except UnicodeDecodeError:
        return None


def _name_in_bytes(p: Path, func_name: str) -> bool:
    return func_name.encode("ascii") in p.read_bytes()


def _call_sites(func_name: str, *, exclude: Path) -> list[str]:
    """`func_name(...)` 形式的**直呼**调用点(AST,不 grep)。"""
    hits: list[str] = []
    for p in _prod_py_files():
        if p.resolve() == exclude.resolve():
            continue
        src = _read_text_or_none(p)
        if src is None:
            if _name_in_bytes(p, func_name):
                hits.append(f"{p.relative_to(ROOT).as_posix()}: 非文本文件里出现了这个名字")
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:                     # 生产树里不该有,有就让它红
            hits.append(f"{p}: SyntaxError")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = (f.id if isinstance(f, ast.Name)
                    else f.attr if isinstance(f, ast.Attribute) else None)
            if name == func_name:
                hits.append(f"{p.relative_to(ROOT).as_posix()}:{node.lineno}")
    return hits


def _textual_refs(func_name: str, *, exclude: Path) -> list[str]:
    """间接引用:赋给别名、装进 dispatch 表、`getattr(m, "remove_question")`。

    🔴 直呼锁单独用是不够的:把函数塞进一张表再调,AST 里看不到 `Call`。

    🔴 **但不能用裸串包含判定。** 第一版就是那样写的,当场被
       `services/defensive_geo/plan_store.py:187` 的一句**错误信息文案**
       (「改题请调用 apply_edit_revision 生成 superseding revision」)判红 ——
       散文提及一个名字,不等于用了它。裸串锁分不开"引用"和"提到"。

    所以这里查两样,都走 AST:
      ① `ast.Name` / `ast.Attribute` 的标识符 —— 覆盖赋值、传参、装表;
      ② `ast.Constant` 里**与函数名全等**的字符串 —— 覆盖 getattr / 字符串派发,
         而散文是一整句话、不会与函数名全等,所以不会误伤。
    """
    hits: list[str] = []
    for p in _prod_py_files():
        if p.resolve() == exclude.resolve():
            continue
        src = _read_text_or_none(p)
        if src is None:
            if _name_in_bytes(p, func_name):
                hits.append(f"{p.relative_to(ROOT).as_posix()}(非文本文件)")
            continue
        try:
            tree = ast.parse(src)
        except SyntaxError:
            hits.append(f"{p.relative_to(ROOT).as_posix()}(SyntaxError)")
            continue
        for node in ast.walk(tree):
            ident = (node.id if isinstance(node, ast.Name)
                     else node.attr if isinstance(node, ast.Attribute)
                     else node.value if isinstance(node, ast.Constant)
                     and isinstance(node.value, str) else None)
            if ident == func_name:
                hits.append(f"{p.relative_to(ROOT).as_posix()}:{getattr(node, 'lineno', '?')}")
                break
    return hits


# ── 正样本:提取器必须能查出真实调用点 ─────────────────────────────
def test_positive_sample_extractor_finds_a_real_call_site():
    """🔴 没有这条,下面三条的"0 个"与"提取器坏了"完全同形。"""
    sites = _call_sites("validate_plan", exclude=QP)
    assert any(s.startswith("api/defensive_geo_api.py") for s in sites), (
        f"正样本失败:AST 提取器在生产树里找不到 validate_plan 的调用点 "
        f"(实得 {sites}) —— 提取器坏了,下面的锁全部无效")


def test_positive_sample_textual_extractor_finds_a_real_reference():
    refs = _textual_refs("validate_plan", exclude=QP)
    assert any(r.startswith("api/") for r in refs), (
        f"正样本失败:字符串提取器找不到 validate_plan 的引用(实得 {refs})")


# ── B 类锁 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("func, raise_lines", [
    ("apply_edit_revision", "329"),
    ("remove_question", "346 / 348"),
])
def test_dead_helper_has_no_production_caller(func: str, raise_lines: str):
    """`question_plan.py` 里这两个 helper 至今**没有任何生产调用者**。

    ⇒ 它们内部的 `raise PlanIdentityError`(第 {raise_lines} 行)到不了前端,
      黄框的分母里没有它们。

    🔴 **这条红了不代表出 bug**,代表"有人把它接上了" ——
      那时要做的是回到工单 §5 给它定处置(上游预防 or 自动收敛),而不是删掉这条锁。
    """
    calls = _call_sites(func, exclude=QP)
    refs = _textual_refs(func, exclude=QP)
    assert calls == [] and refs == [], (
        f"{func} 已被接线(直呼 {calls} · 引用 {refs})。"
        f"它内部 raise 的第 {raise_lines} 行现在**可达**了,"
        f"需要按工单 §5 给它定处置;不要直接放宽本锁。")


def test_run_request_hash_only_reachable_from_run_preview():
    """`:246` 会抛错,但**不产出黄框** —— 它只在 run-preview 那条路上。

    黄框全页只有一个 setter(`NewDiagnosis.tsx` 的 `setPlanError`),
    来源是 question-plan preview 端点;`run_request_hash` 不在那个端点里。

    🔴 锁的是"它没有溜进 question-plan preview",不是"它没被调用"。
    """
    sites = _call_sites("run_request_hash", exclude=QP)
    assert sites, "run_request_hash 一个调用点都没有 —— 与已知事实不符,提取器可疑"
    src = io.open(API, encoding="utf-8").read()
    tree = ast.parse(src)
    preview_fn = next(
        (n for n in ast.walk(tree)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
         and n.name == "create_question_plan_preview"), None)
    assert preview_fn is not None, "找不到 create_question_plan_preview —— 端点改名了,锁要跟着改"
    inside = [n for n in ast.walk(preview_fn)
              if isinstance(n, ast.Call)
              and getattr(n.func, "id", getattr(n.func, "attr", None)) == "run_request_hash"]
    assert inside == [], (
        "run_request_hash 出现在 question-plan preview 里 ⇒ `:246` 现在能产出黄框,"
        "需要按工单 §5 给它定处置")


def test_offensive_mode_never_reaches_the_defgeo_preview():
    """`:282`(offensive 计划混入 defensive)在 UI 上不可达。

    `runDefensivePlanPreview` 只在 `if (isDefensiveFlow)` 里被调,
    而 `isDefensiveFlow === (campaignMode !== 'offensive')`
    ⇒ 送到后端的 mode ∈ {defensive, hybrid},**永远不是 offensive**。

    🔴 这条锁读的是**分流键的定义**与**调用点的守卫**两处,不是读一句注释。
    """
    tsx = io.open(ROOT / "frontend" / "src" / "pages" / "Diagnosis" / "NewDiagnosis.tsx",
                  encoding="utf-8").read()
    assert "const isDefensiveFlow = campaignMode !== 'offensive';" in tsx, (
        "isDefensiveFlow 的定义变了 —— `:282` 的不可达性建立在它之上,重新证明再改锁")
    guarded = "if (isDefensiveFlow) {" in tsx and "await runDefensivePlanPreview();" in tsx
    assert guarded, (
        "runDefensivePlanPreview 不再被 isDefensiveFlow 守卫 ⇒ offensive 模式可能送到后端,"
        "`:282` 变为可达,需要按工单 §5 给它定处置(对称的自动收敛)")
