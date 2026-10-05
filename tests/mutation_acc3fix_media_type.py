"""[WO-ACCEPTANCE-3FIX-2026-08-05 项1] 单发 media_type 落库 · 变异自检。

三关缺一即判无效变异:
  1. 锚点唯一命中(命中 0 或 >1 = 变异没打在预期位置,**记 SKIP 不记 SURVIVED** ——
     把"没打上"混进"存活"里会得出"锁抓不到"的假结论);
  2. 落盘后文件真变了;
  3. **至少一个预期用例转红**。零转红 = 这条锁在这个 fixture 下抓不到本 bug。

🔴 文件读写一律走 bytes:`Path.read_text`/`write_text` 在 Windows 上会翻换行,
   "读原文再写回"这个本该恒等的还原操作会把整个文件从 LF 翻成 CRLF。

跑法: TEST_DATABASE_URL=... python tests/mutation_acc3fix_media_type.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

API = "api/meijiehezi_api.py"
DB = "db/meijiehezi_db.py"

PY_TESTS = "tests/test_acc3fix_media_type_2026_08_05.py"

# (编号, 说明, 文件, 原文, 替换, 预期转红的用例名片段)
MUTATIONS: list[tuple[str, str, str, str, str, list[str]]] = [
    ("A1", "🔴 把修复直接删掉(退回本 bug 的形态:单发 items 里没有 media_type 键)",
     API,
     '            "media_type": _item_media_type,\n',
     '',
     ["test_single_publish_wemedia_lands_wemedia__must_hit",
      "test_single_publish_wemedia_is_never_null_or_mhz__must_not_hit",
      "test_single_publish_softtext_lands_mhz__must_hit"]),

    ("A2", "不归一,原样落 req.media_type('article' 漏进 DB → 筛选下拉多一个重复选项)",
     API,
     '    _item_media_type = _canonical_media_type(req.media_type)\n',
     '    _item_media_type = req.media_type\n',
     ["test_single_publish_softtext_lands_mhz__must_hit"]),

    ("A3", "🔴 写死成 'mhz'(自媒体单被当软文 —— 比 NULL 更难查的那种错)",
     API,
     '    _item_media_type = _canonical_media_type(req.media_type)\n',
     '    _item_media_type = "mhz"\n',
     ["test_single_publish_wemedia_lands_wemedia__must_hit",
      "test_single_publish_wemedia_is_never_null_or_mhz__must_not_hit",
      "test_single_publish_softtext_and_wemedia_differ__must_not_hit"]),

    ("A4", "🔴 归一函数把 None 兜底成 'mhz'(工单 §1.4 明令禁止的那一条)",
     DB,
     '    if value is None:\n        return None\n    raw = str(value).strip().lower()\n',
     '    raw = str(value or "").strip().lower()\n',
     ["test_canonical_none_stays_none__must_hit",
      "test_canonical_none_is_not_mhz__must_not_hit"]),

    ("A5", "🔴 归一函数把 wemedia 也归成软文(等于这个字段白补)",
     DB,
     '    if raw == MEDIA_TYPE_WEMEDIA:\n        return MEDIA_TYPE_WEMEDIA\n',
     '    if False:\n        return MEDIA_TYPE_WEMEDIA\n',
     ["test_canonical_wemedia_stays_wemedia__must_hit",
      "test_canonical_wemedia_is_not_softtext__must_not_hit",
      "test_single_publish_wemedia_lands_wemedia__must_hit",
      "test_single_publish_softtext_and_wemedia_differ__must_not_hit"]),

    ("A6", "归一函数把 svideo 也归成软文(工单 §1.5 第 4 行:svideo 独立路径不受影响)",
     DB,
     '    if raw == MEDIA_TYPE_SVIDEO:\n        return MEDIA_TYPE_SVIDEO\n',
     '    if False:\n        return MEDIA_TYPE_SVIDEO\n',
     ["test_canonical_svideo_untouched__must_hit",
      "test_canonical_svideo_not_normalized_to_mhz__must_not_hit"]),

    ("A7", "🔴 在 create_order 里加默认值(工单 §1.4 红线 · 把错误固化成正确行为)",
     DB,
     '                  it.get("media_type")))\n',
     '                  it.get("media_type") or "mhz"))\n',
     ["test_create_order_still_reads_raw_key__must_hit"]),

    ("A8", "顺手\"统一\"两支:批量端点也改走单发的归一(工单 §1.4 禁做第 2 条)",
     API,
     '                    "media_type": item.media_types[i] if i < len(item.media_types) else None,\n',
     '                    "media_type": _canonical_media_type(item.media_types[i] if i < len(item.media_types) else None),\n',
     ["test_batch_endpoint_still_builds_its_own_media_type__must_hit",
      "test_batch_endpoint_does_not_call_canonical__must_not_hit"]),

    ("A9", "🔴 归一结果不再受 VARCHAR(10) 约束(远端 'short_video' 原样落库 → INSERT 报错)",
     DB,
     '    return MEDIA_TYPE_MHZ\n\n\ndef _get_conn():\n',
     '    return raw\n\n\ndef _get_conn():\n',
     ["test_canonical_output_fits_varchar10__must_hit",
      "test_canonical_article_and_empty_become_mhz__must_hit",
      "test_canonical_article_does_not_leak_raw_word__must_not_hit"]),
]


def read_src(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def write_src(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


def run_locks() -> tuple[int, str]:
    env = dict(os.environ)
    env.setdefault("TEST_DATABASE_URL",
                   "postgresql://geo_test:geo_test@127.0.0.1:5432/test_geo_agentscope")
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PY_TESTS, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=REPO, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env,
    )
    return (1 if p.returncode else 0), (p.stdout or "") + (p.stderr or "")


def red_markers(output: str) -> set[str]:
    marks: set[str] = set()
    for line in output.splitlines():
        if line.startswith("FAILED ") and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
        if line.startswith("ERROR ") and "::" in line:
            marks.add(line.split("::")[-1].split()[0].split("[")[0])
    return marks


def main() -> int:
    rc, out = run_locks()
    if rc != 0:
        print("基线不绿,先修基线:\n" + out[-3000:])
        return 2
    print("基线 GREEN(反向对照:未变异的树必须全绿,否则后面每条'转红'都可能是基线自己红的)")
    print("=" * 72)

    killed, survived, skipped = 0, [], []
    for mid, desc, rel, old, new, expect in MUTATIONS:
        path = REPO / rel
        original = read_src(path)
        hits = original.count(old)
        if hits != 1:
            # 🔴 命中 != 1 记 SKIP 不记 SURVIVED:变异根本没打上,
            #    与"打上了但没被抓到"是两回事,混在一起会得出假的"锁无效"结论。
            print(f"[{mid}] ⏭️ SKIP · 锚点命中 {hits} 次(需恰好 1 次): {desc}")
            skipped.append((mid, desc, f"锚点命中 {hits} 次"))
            continue
        mutated = original.replace(old, new, 1)
        if mutated == original:
            print(f"[{mid}] ⏭️ SKIP · 落盘后文件没变(no-op): {desc}")
            skipped.append((mid, desc, "文件未改变"))
            continue

        write_src(path, mutated)
        try:
            m_rc, m_out = run_locks()
        finally:
            write_src(path, original)
        assert read_src(path) == original, f"[{mid}] 还原失败,树被污染了"

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
    print(f"变异结果: {killed} 杀 / {len(survived)} 存活 / {len(skipped)} 跳过 "
          f"(共 {len(MUTATIONS)})")
    for tag, rows in (("存活", survived), ("跳过(变异没打上,不算证据)", skipped)):
        if rows:
            print(f"{tag}:")
            for mid, desc, why in rows:
                print(f"  - {mid} [{why}] {desc}")
    return 1 if (survived or skipped) else 0


if __name__ == "__main__":
    raise SystemExit(main())
