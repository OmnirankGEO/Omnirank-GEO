"""§3 挪家 · 体检发起页「提交字段集 ⊆ DiagnosisRequest」红线
(原 frontend/scripts/test-diagnosis-launch-ui.mjs §1)

## 为什么挪

原锁挂在 `frontend/package.json` 的 `build` 链上,§1 要读 `server.py`。
Docker 的 frontend-builder 阶段只 `COPY frontend/ ./` —— 后端源码不在那一层,
于是 §1 每次镜像构建都走 SKIP 分支、打印「🔴 这不是通过,是没跑」然后继续。
**它从没在镜像构建里跑过**;本地跑得通只是因为本地 worktree 有 server.py ——
这正是「本机跑通 ≠ 目标运行时」。

## 挪家原则:断言语义逐字保留

原 §1 五条断言在下面一一对应,含两条反向对照:
  1. server.py 里找得到 DiagnosisRequest
  2. 模型字段集非空(否则下面的 ⊆ 恒真)
  3. 提交的每个字段都在请求模型里          ← 主判据
  4. 反向对照:虚构字段必须被判为缺失(证明不是恒真)
  5. 反向对照:brand_name / keywords 确实在提交集里(防抽取器抽了个空壳)

## 抽取口径不许退化成 grep

原判据明写「Python 侧走 ast.parse,不 grep server.py」,前端侧走 TypeScript AST。
两边都保住:
  · server.py  → 本文件用 `ast.parse`(与原内联 python 逐字同逻辑);
  · NewDiagnosis.tsx → 起子进程调 `frontend/scripts/dump-diagnosis-submit-fields.mjs`
    (TS AST,与原 collectSubmitFields 同源)。
node/typescript 够不到时退到 Python 抽取器,并且**两者都在时必须给出完全相同的集合**
—— 那条交叉判据就是防「退化的那条路悄悄换了口径」。
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import subprocess

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_PY = os.path.join(REPO_ROOT, "server.py")
FRONTEND = os.path.join(REPO_ROOT, "frontend")
PAGE_TSX = os.path.join(FRONTEND, "src", "pages", "Diagnosis", "NewDiagnosis.tsx")
DUMPER = os.path.join(FRONTEND, "scripts", "dump-diagnosis-submit-fields.mjs")


def _read(path):
    with io.open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        return fh.read()


def diagnosis_request_fields():
    """server.py → DiagnosisRequest 的注解字段名(原内联 python 逐字同逻辑)。"""
    tree = ast.parse(_read(SERVER_PY))
    found, fields = False, []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DiagnosisRequest":
            found = True
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields.append(stmt.target.id)
    return found, fields


def submit_fields_via_node():
    """TS AST 抽取(首选)。node/typescript 够不到返回 None。"""
    if not shutil.which("node") or not os.path.exists(DUMPER):
        return None
    if not os.path.isdir(os.path.join(FRONTEND, "node_modules", "typescript")):
        return None
    try:
        out = subprocess.run(
            ["node", os.path.basename(DUMPER)],
            cwd=os.path.join(FRONTEND, "scripts", ".."),
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    try:
        return set(json.loads(out.stdout.strip())["fields"])
    except Exception:
        return None


def submit_fields_via_python():
    """兜底抽取:`payload.X = ` 与 `const payload ... = { X: ... }`,与 TS AST 同口径。"""
    src = _read(PAGE_TSX)
    fields = set(re.findall(r"\bpayload\.([A-Za-z_$][\w$]*)\s*=(?!=)", src))
    m = re.search(r"\bconst\s+payload\b[^=]*=\s*\{", src)
    if m:
        i, depth = m.end() - 1, 0
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = src[m.end(): i]
        # 只收**本层**的键,不收嵌套对象里的键(与 TS AST 的 properties 同层级)
        depth, buf, top = 0, [], []
        for ch in body:
            if ch in "{[(":
                depth += 1
            elif ch in "}])":
                depth -= 1
            if depth == 0 and ch == ",":
                top.append("".join(buf)); buf = []
            else:
                buf.append(ch)
        top.append("".join(buf))
        for seg in top:
            km = re.match(r"\s*(?:'([^']+)'|\"([^\"]+)\"|([A-Za-z_$][\w$]*))\s*:", seg)
            if km:
                fields.add(km.group(1) or km.group(2) or km.group(3))
            else:  # 简写 { brand_name, keywords }
                sm = re.match(r"\s*([A-Za-z_$][\w$]*)\s*$", seg)
                if sm:
                    fields.add(sm.group(1))
    return fields


@pytest.fixture(scope="module")
def submit_fields():
    node_set = submit_fields_via_node()
    py_set = submit_fields_via_python()
    if node_set is not None:
        # 交叉判据:两条路必须给出完全相同的集合,否则兜底那条已经悄悄换了口径
        assert node_set == py_set, (
            "TS AST 抽取与 Python 兜底抽取结果不一致 —— 兜底口径漂了,不许用它当判据。\n"
            "  只在 TS 里: %s\n  只在 Python 里: %s" % (sorted(node_set - py_set), sorted(py_set - node_set))
        )
        return node_set
    return py_set


# ------------------------------------------------------------ 0. 判据可用性
def test_0_criteria_are_reachable():
    """挪家的全部意义:这两个文件在这里必须都够得到,没有 SKIP 分支可走。"""
    assert os.path.exists(SERVER_PY), "够不到 server.py —— 原锁在 builder 里正是死在这一步"
    assert os.path.exists(PAGE_TSX), "够不到 NewDiagnosis.tsx"


# ------------------------------------------------------------ 1. 原 §1 五条
def test_1_diagnosis_request_class_exists():
    found, _fields = diagnosis_request_fields()
    assert found is True, "server.py 里找不到 DiagnosisRequest"


def test_2_model_field_set_is_not_empty():
    """否则下面的 ⊆ 恒真。"""
    _found, fields = diagnosis_request_fields()
    assert len(fields) >= 10, "DiagnosisRequest 只有 %d 个注解字段,⊆ 判据近乎恒真" % len(fields)


def test_3_every_submitted_field_exists_in_request_model(submit_fields):
    """🔴 主判据:表单提交字段集 ⊆ DiagnosisRequest 字段集(原工单 §0 红线)。"""
    assert len(submit_fields) >= 5, "抽出的提交字段只有 %d 个 —— 抽取器抽了个空壳" % len(submit_fields)
    _found, fields = diagnosis_request_fields()
    missing = sorted(f for f in submit_fields if f not in set(fields))
    assert not missing, (
        "请求模型里没有:%s —— 按原工单 §0 必须停下来报,不许静默砍功能" % ", ".join(missing)
    )


def test_4_reverse_control_fabricated_field_is_flagged():
    """必须命中:虚构字段必须被判为缺失 —— 证明 ⊆ 判据不是恒真。"""
    _found, fields = diagnosis_request_fields()
    assert "__field_that_must_not_exist__" not in set(fields)


def test_5_reverse_control_core_fields_present(submit_fields):
    """必须不命中:核心字段确实在提交集里 —— 防 collectSubmitFields 抽了个空壳。"""
    assert "brand_name" in submit_fields and "keywords" in submit_fields, sorted(submit_fields)


# ------------------------------------------------------------ 2. 挪家自证
def test_6_lock_no_longer_reads_backend_from_frontend_build():
    """原 .mjs 不许再引后端文件 —— 引了就说明 §1 又被搬回前端构建层了。"""
    mjs = _read(os.path.join(FRONTEND, "scripts", "test-diagnosis-launch-ui.mjs"))
    # 判「有没有真去读后端文件」,不判「文中有没有出现 server.py 这几个字」——
    # 那条挪家说明的注释本身就会提到它,按字面判会命中解释它的注释(2026-08 反复踩过的形态)。
    assert "'../server.py'" not in mjs and '"../server.py"' not in mjs, \
        "test-diagnosis-launch-ui.mjs 仍在解析后端路径(挪家没做干净)"
    assert "SERVER_PY" not in mjs, "仍留着 SERVER_PY 常量"
    assert "本轮 SKIP" not in mjs, "仍留着 SKIP 分支 —— 那正是要根除的形态"
    # 反向对照:文件本身仍有实质判据(否则上面两条是"文件被清空"式的空即通过)
    assert mjs.count("check(") >= 20, "test-diagnosis-launch-ui.mjs 判据数塌了,上面两条成了空即通过"
