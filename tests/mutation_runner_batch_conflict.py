"""[WO-BATCH-CONFLICT-2026-08-04] 变异自检。

三关缺一即判无效变异:
  1. 锚点唯一命中(命中 0 或 >1 = 变异没打在预期位置);
  2. 落盘后文件真变了;
  3. **至少一个预期用例转红**。零转红 = 这条锁在这个 fixture 下抓不到本 bug。

🔴 文件读写一律走 bytes:`Path.read_text`/`write_text` 在 Windows 上会翻换行
   (读 CRLF→LF、写 LF→CRLF),"读原文再写回"这个本该恒等的还原操作会把整个文件
   从 LF 翻成 CRLF —— 2026-08-04 已经实打实栽过一次。

跑法: python tests/mutation_runner_batch_conflict.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

API = "api/meijiehezi_api.py"
CONTRACT = "frontend/src/contracts/duplicateOrderConflict.ts"
PANEL = "frontend/src/components/publishing/DuplicateConflictPanel.tsx"

PY_TESTS = "tests/test_batch_conflict_precheck_2026_08_04.py"
JS_LOCKS = "scripts/test-batch-conflict-window.mjs"

# (编号, 说明, 文件, 原文, 替换, 预期转红的用例/锁名片段)
MUTATIONS: list[tuple[str, str, str, str, str, list[str]]] = [
    # ---------------- 后端 ----------------
    # 锚点必须唯一:`"article_id": article_id,` 在本文件另有一处(行 2695,别的端点),
    # 带上前一行 `return {` 才切得准。
    ("B1", "冲突条目不带 article_id(前端就回不到具体组合)",
     API,
     '    return {\n        "article_id": article_id,\n',
     '    return {\n        "article_id": None,\n',
     ["test_conflict_entry_carries_ids"]),

    ("B2", "media_id 也丢掉",
     API,
     '        "media_id": media_id,\n',
     '        "media_id": None,\n',
     ["test_conflict_entry_carries_ids"]),

    ("B3", "取不到媒体名时**编**一个(而不是退回 id)",
     API,
     '        return str(media_id)\n    return media_names[idx] if idx < len(media_names) else str(media_id)\n',
     '        return "未知媒体"\n    return media_names[idx] if idx < len(media_names) else "未知媒体"\n',
     ["test_media_name_lookup_cells"]),

    ("B4", "🔴 撞到第一个冲突就抛(回到整批卡死那个形态)",
     API,
     "                    all_dups.append(build_conflict_entry(\n",
     "                    if all_dups:\n                        raise HTTPException(status_code=409, detail={})\n"
     "                    all_dups.append(build_conflict_entry(\n",
     ["test_batch_precheck_collects_all_before_raising"]),

    ("B5", "单发链路丢掉旧键 duplicate_media_ids(老前端当场瞎)",
     API,
     '                "duplicate_media_ids": [d["media_id"] for d in dups],\n',
     "",
     ["test_response_shape_cells"]),

    # ---------------- 前端契约 ----------------
    ("F1", "缺 id 的条目补 0 混进清单(剔除会去摘不存在的组合)",
     CONTRACT,
     "    if (articleId === null || mediaId === null) continue;\n",
     "    if (false) continue;\n",
     ["锁2"]),

    ("F2", "🔴 already_published 又说成「进行中」",
     CONTRACT,
     "  already_published: '已在该媒体发布过',\n",
     "  already_published: '有进行中的订单',\n",
     ["锁3"]),

    ("F3", "剔除改成只按 mediaId(误伤同媒体别的文章)",
     CONTRACT,
     "  const drop = new Set((conflicts || []).map(c => `${c.articleId}:${c.mediaId}`));\n",
     "  const drop = new Set((conflicts || []).map(c => `${c.mediaId}`));\n",
     ["锁4"]),

    ("F3b", "剔除时不摘、原样返回(等于没有剔除功能)",
     CONTRACT,
     "  return (cart || [])\n    .map(entry => ({\n",
     "  return (cart || []);\n  return (cart || [])\n    .map(entry => ({\n",
     ["锁4", "锁5"]),

    ("F4", "剩余数改成减法(冲突条目重复时会算错)",
     CONTRACT,
     "  return countCombinations(applyConflictRemoval(cart, conflicts));\n",
     "  return countCombinations(cart) - (conflicts || []).length;\n",
     ["锁5"]),

    ("F5", "🔴 有未处理冲突时也放行提交",
     CONTRACT,
     "  return !!block && block.conflicts.length > 0;\n",
     "  return false;\n",
     ["锁6", "锁7"]),

    ("F6", "解析器不认领 DUPLICATE_ORDER(直接回落成一个 toast)",
     CONTRACT,
     "  if (candidate.code !== DUPLICATE_ORDER_CODE) return null;\n",
     "  return null;\n  if (candidate.code !== DUPLICATE_ORDER_CODE) return null;\n",
     ["锁1", "锁2"]),

    ("F7", "大写理由码不做规范化(全落 other,分类文案失效)",
     CONTRACT,
     "  const s = String(raw ?? '').trim().toLowerCase();\n",
     "  const s = String(raw ?? '').trim();\n",
     ["锁1"]),

    # ---------------- 前端面板 ----------------
    ("P1", "剩 0 时仍给剔除按钮(点了就是提交空单)",
     PANEL,
     "  const wouldBeEmpty = remainingAfterRemoval <= 0;\n",
     "  const wouldBeEmpty = false;\n",
     ["锁7"]),

    ("P2", "不再明示「未产生任何费用」(用户以为钱已经花了)",
     PANEL,
     "        这批里有 {conflicts.length} 个组合没法提交，本次未产生任何费用。\n",
     "        这批里有 {conflicts.length} 个组合没法提交。\n",
     ["锁6"]),

    ("P3", "不按类型分组,全部混成一坨(回到「一句话」那个形态)",
     PANEL,
     "      {groupConflictsByReason(conflicts).map(group => (\n",
     "      {[].map(group => (\n",
     ["锁6"]),

    ("P4", "冲突为空时也把面板渲染出来(空面板挡路)",
     PANEL,
     "  if (conflicts.length === 0) return null;\n",
     "  if (false) return null;\n",
     ["锁6"]),
]


def read_src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_src(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def run_locks() -> tuple[int, str]:
    """跑后端 pytest + 前端锁套件,任一红即红。"""
    out = []
    rc = 0
    p1 = subprocess.run([sys.executable, "-m", "pytest", PY_TESTS, "-q", "--no-header",
                         "-p", "no:cacheprovider"],
                        cwd=REPO, capture_output=True, text=True,
                        encoding="utf-8", errors="replace")
    out.append(p1.stdout or "")
    out.append(p1.stderr or "")
    rc |= (1 if p1.returncode else 0)

    p2 = subprocess.run(["node", JS_LOCKS], cwd=REPO / "frontend",
                        capture_output=True, text=True, encoding="utf-8",
                        errors="replace", shell=(sys.platform == "win32"))
    out.append(p2.stdout or "")
    out.append(p2.stderr or "")
    rc |= (2 if p2.returncode else 0)
    return rc, "\n".join(out)


def red_markers(output: str) -> set[str]:
    """收集转红标记:pytest 的 FAILED 用例名 + 前端锁的 ❌ 锁名。"""
    marks: set[str] = set()
    for line in output.splitlines():
        if line.startswith("FAILED ") and "::" in line:
            marks.add(line.split("::")[-1].split()[0])
        if line.startswith("❌"):
            for i in range(1, 10):
                if f"锁{i}" in line:
                    marks.add(f"锁{i}")
    return marks


def main() -> int:
    rc, out = run_locks()
    if rc != 0:
        print("基线不绿,先修基线:\n" + out[-3000:])
        return 2
    print("基线 GREEN\n" + "=" * 72)

    killed, survived = 0, []
    for mid, desc, rel, old, new, expect in MUTATIONS:
        path = REPO / rel
        original = read_src(path)
        hits = original.count(old)
        if hits != 1:
            print(f"[{mid}] ❌ 锚点命中 {hits} 次(需恰好 1 次): {desc}")
            survived.append((mid, desc, f"锚点命中 {hits} 次"))
            continue
        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"[{mid}] ❌ 落盘后文件没变(no-op): {desc}")
            survived.append((mid, desc, "文件未改变"))
            continue

        write_src(path, mutated)
        try:
            m_rc, m_out = run_locks()
        finally:
            write_src(path, original)

        reds = red_markers(m_out)
        hit = sorted(reds & set(expect))
        if m_rc == 0:
            print(f"[{mid}] ❌ 存活(零转红): {desc}")
            survived.append((mid, desc, "零转红"))
        elif not hit:
            print(f"[{mid}] ⚠️ 转红但不是预期那些: {desc}\n"
                  f"        预期 {expect} / 实际 {sorted(reds)}")
            survived.append((mid, desc, f"红的是 {sorted(reds)}"))
        else:
            killed += 1
            print(f"[{mid}] ✅ 被杀 (含预期 {hit}): {desc}")

    print("=" * 72)
    print(f"变异结果: {killed}/{len(MUTATIONS)} 被杀")
    if survived:
        print("存活/无效变异:")
        for mid, desc, why in survived:
            print(f"  - {mid} [{why}] {desc}")
        return 1
    rc2, _ = run_locks()
    print("还原后复跑基线:", "GREEN" if rc2 == 0 else "RED(文件未正确还原!)")
    return 0 if rc2 == 0 else 3


if __name__ == "__main__":
    raise SystemExit(main())
