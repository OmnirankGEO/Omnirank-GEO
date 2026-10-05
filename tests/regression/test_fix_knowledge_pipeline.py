"""
判别性回归锁 — services/knowledge_pipeline.py

覆盖 findings:
- GEO-R1-CAN-129: process_role_document 路径逃逸写入(basename + resolve/containment 兜底)
- GEO-R4-CAN-020: _store_role_vectors LanceDB 谓词注入(转义 filename)

主形态: source-inspection(读源码断言修复标志)。回退修复则断言失败。
另加纯字符串行为单测验证转义/basename 逻辑(不依赖 DB/app)。
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SRC_PATH = ROOT / "services" / "knowledge_pipeline.py"
SRC = SRC_PATH.read_text(encoding="utf-8")


def _slice_func(src: str, def_name: str) -> str:
    """截取某 async/def 函数体到下一个同缩进 def(粗粒度，够断言标志用)。"""
    idx = src.find(def_name)
    assert idx != -1, f"未找到 {def_name}"
    tail = src[idx:]
    m = re.search(r"\n    (?:async )?def ", tail[1:])
    return tail[: m.start() + 1] if m else tail


# ---------- GEO-R1-CAN-129: 路径逃逸兜底 ----------

def test_r1_process_role_document_basename_guard():
    body = _slice_func(SRC, "async def process_role_document")
    # basename 兜底: 只保留 Path(...).name
    assert "Path(filename" in body and ".name" in body, \
        "process_role_document 缺少 basename 兜底(Path(filename).name)"
    # 标记注释存在
    assert "GEO-R1-CAN-129" in body, "缺少 GEO-R1-CAN-129 修复标记"


def test_r1_containment_resolve_check():
    body = _slice_func(SRC, "async def process_role_document")
    # 二次容器化: resolve() + parents/parent 校验
    assert ".resolve()" in body, "缺少 resolve() 容器化校验"
    assert "parents" in body, "缺少 relative-to / parents 逃逸判定"
    # 逃逸时必须 raise 不能静默
    assert "raise" in body, "路径逃逸必须 raise 而非静默降级"


# ---------- GEO-R4-CAN-020: LanceDB 谓词注入 ----------

def test_r4_role_vectors_predicate_escaped():
    body = _slice_func(SRC, "async def _store_role_vectors")
    assert "GEO-R4-CAN-020" in body, "_store_role_vectors 缺少 GEO-R4-CAN-020 修复标记"
    # 必须先转义再进谓词
    assert "escaped_filename" in body, "缺少 escaped_filename 转义变量"
    assert 'replace(\'"\'' in body or 'replace("\\"' in body or '\\\\"' in body, \
        "缺少对双引号/反斜杠的转义"
    # 不应再有裸插值 f'filename = "{filename}"'
    assert 'f\'filename = "{filename}"\'' not in body, \
        "仍存在未转义的裸 filename 谓词插值"


def test_r4_no_raw_filename_predicate_anywhere():
    # 全文兜底: 两处 store 函数都不得残留裸插值
    assert 'f\'filename = "{filename}"\'' not in SRC, \
        "文件内仍残留未转义的 filename 谓词"


# ---------- 纯行为单测(不依赖 DB) ----------

def test_behavior_basename_strips_traversal():
    # 复刻源码 basename 逻辑
    def basename(filename):
        return Path(filename or "未命名资料.txt").name.strip() or "未命名资料.txt"

    assert basename("../../../etc/evil.py") == "evil.py"
    assert basename("../../config/settings.json") == "settings.json"
    # basename 后不含分隔符, 无法逃逸
    assert "/" not in basename("a/b/c.txt")
    assert "\\" not in basename("a\\b\\c.txt")


def test_behavior_predicate_escape():
    def esc(filename):
        return filename.replace("\\", "\\\\").replace('"', '\\"')

    # 注入尝试: 含双引号
    evil = 'x" OR "1"="1'
    out = esc(evil)
    assert '\\"' in out
    # 转义后原始未转义双引号数量减少(每个真引号都带反斜杠前缀)
    assert out.count('\\"') == evil.count('"')


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"PASS {name}")
    print("ALL PASS")
