#!/usr/bin/env python
"""[E1-3⑥ / 门9③] 迁移号段 **冷建 + 同库二跑** 证据(默认 042–054)。

Codex 二审门 5 要 "042–052 冷建、同库二跑" 的证据。冷建这一半其实每次跑包
都在做(``tests/defgeo_wob_publish_funding_2026_08_25/conftest.py`` 从生产
schema 快照 + manifest 顺序的 042–052 建一次性库),但**同库二跑**没有单独产物。

这个脚本把两半都跑成可复核的产物:

  ① 一次性库(库名安全栓:必须含 ``test``)上冷建 —— 生产 schema 快照
     + 042–052(**清单从 conftest 现取,不手抄**);
  ② 取 schema 指纹 A;
  ③ **同一个库上再跑一遍同一批迁移**;
  ④ 取指纹 B。要求:第二轮零报错 **且 A == B**。

🔴 清单为什么必须从 conftest import 而不是抄一份:同一个谓词写两处必有一处
   没人验。抄一份就意味着"冷建证据用的迁移集"和"判据真正用的迁移集"可以
   悄悄漂开,而漂开之后这份证据说明不了任何事。

🔴 [门9 · 2026-08-27] 号段从写死的 042–052 改成**传参**,默认 042–054 ——
   E3 落了 054(监测格冻结租户归属)。**没有再写一份 042–054 的脚本**:
   同一个谓词写两处必有一处没人验,两份会各自漂移,"哪一份是证据"从此说不清。

🔴 053 **不在本仓**(manifest 里写明归工单D)。号段有洞是正常的,
   但"洞"与"漏登记"必须分得开,所以加了一条方向正确的交叉核:
   **盘上有、清单里没有** ⇒ 停机。反方向(清单里有盘上没有)会在读文件时当场炸,
   响亮,不需要判据;而盘上有清单没有的那个文件**上线后永远不会跑**,静悄悄。

用法::

    python scripts/defgeo_migration_band_cold_and_replay.py \\
        postgresql://geo_admin:testpw@localhost:55488/geo_defgeo_cold_g9_test \\
        --band 042-054
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import psycopg2
import psycopg2.extras

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _assert_throwaway(dsn: str) -> str:
    db = dsn.rsplit("/", 1)[-1].split("?")[0]
    if "test" not in db.lower():
        raise SystemExit(f"安全栓:库名 {db!r} 不含 'test' —— 拒绝在可能是真库的地方跑")
    return db


def _conn(dsn: str):
    c = psycopg2.connect(dsn)
    c.autocommit = True
    c.cursor_factory = psycopg2.extras.RealDictCursor
    with c.cursor() as cur:
        cur.execute("SET search_path TO public")
    return c


def _snapshot(dsn: str) -> dict:
    """schema 指纹 —— 复用既有工具,索引按 (表, 索引名) 键。"""
    out = ROOT / ".gate9" / "_snap_tmp.json"
    out.parent.mkdir(exist_ok=True)
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "defgeo_manifest_replay.py"),
         "snapshot", dsn, "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"snapshot 失败:\n{r.stdout}\n{r.stderr}")
    return json.loads(out.read_text(encoding="utf-8"))


def _apply(conn, path: Path) -> None:
    """复刻 prestart 的 ``_apply`` 语义:整文件一次 execute,报错向上抛。"""
    sql = path.read_text(encoding="utf-8", errors="replace")
    with conn.cursor() as cur:
        cur.execute(sql)


def _band_regex(band: str):
    """``042-054`` → 匹配 db/migration_0NN_ 的正则(NN 在闭区间内)。"""
    import re as _re

    m = _re.fullmatch(r"(\d{3})-(\d{3})", band)
    if not m:
        raise SystemExit(f"号段写法应为 042-054,实得 {band!r}")
    lo, hi = int(m.group(1)), int(m.group(2))
    if lo > hi:
        raise SystemExit(f"号段反了:{band}")
    return lo, hi, _re.compile(r"^db/migration_(\d{3})_")


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("dsn")
    ap.add_argument("--band", default="042-054")
    args = ap.parse_args()
    dsn = args.dsn
    db = _assert_throwaway(dsn)
    lo, hi, band_re = _band_regex(args.band)

    # 🔴 迁移清单 + 生产 schema 装载器**都从判据包现取**,不抄。
    sys.path.insert(0, str(ROOT))
    # 窗B 的 conftest 在**导入期**就跑库名安全栓(必须同时含 defgeo/wob/test)。
    # 只为过这道栓临时喂一个合规名,用完还原 —— 真正连的库始终是 --dsn 传进来的
    # 那个(``_load_prod_schema(conn)`` 收显式 conn,不读环境)。
    # 不给冷库改名去"迎合"栓:那会让库名撒谎(它不是 wob 包的库)。
    import os as _os

    _saved = _os.environ.get("TEST_DATABASE_URL")
    _base = dsn.rsplit("/", 1)[0]
    _os.environ["TEST_DATABASE_URL"] = f"{_base}/geo_defgeo_wob_latch_only_test"
    try:
        from tests.defgeo_wob_publish_funding_2026_08_25 import conftest as _wob
    finally:
        if _saved is None:
            _os.environ.pop("TEST_DATABASE_URL", None)
        else:
            _os.environ["TEST_DATABASE_URL"] = _saved

    # 🔴 门里写的是 "042–052 整段",所以清单**按号段从 manifest 机械枚举**,
    #    而不是只取判据包用到的那几个 —— 手挑子集会让证据的作用域小于结论。
    import re as _re
    from db.migration_manifest import MIGRATIONS

    def _in_band(rel: str) -> bool:
        m = band_re.match(rel)
        return bool(m) and lo <= int(m.group(1)) <= hi

    migrations = [ROOT / rel for rel in MIGRATIONS if _in_band(rel)]

    # 🔴 方向正确的交叉核:**盘上有、清单里没有** = 上线后永远不会跑,静悄悄。
    #    (反方向会在读文件时当场炸,不需要判据。)
    on_disk = sorted(
        f"db/{f.name}" for f in (ROOT / "db").glob("migration_*.sql")
        if _in_band(f"db/{f.name}"))
    listed = {p_.name for p_ in migrations}
    orphan = [rel for rel in on_disk if rel.rsplit("/", 1)[-1] not in listed]
    if orphan:
        raise SystemExit(
            f"🔴 {args.band} 号段里有**盘上存在但 manifest 未登记**的迁移:{orphan} —— "
            "它上线后永远不会跑。停下报 Review")

    # 号段里缺的号(如 053 归工单D)如实打印,不静默跳过。
    present = sorted(int(band_re.match(f"db/{p_.name}").group(1)) for p_ in migrations)
    gaps = [n for n in range(lo, hi + 1) if n not in present]

    # 交叉核:判据包用的那一份必须是本段的**子集**。两边漂开的话,
    # 这份冷建证据说明不了判据跑在什么 schema 上。
    pkg = {p.name for p in _wob.package_migrations()}
    band_names = {p.name for p in migrations}
    drifted = sorted(pkg - band_names)
    if drifted:
        raise SystemExit(
            f"🔴 判据包用了 {args.band} 号段之外的迁移:{drifted} —— "
            "冷建证据的作用域覆盖不了判据,停下报 Review")

    print(f"库 = {db}")
    if gaps:
        print(f"号段内空号:{gaps} —— 本仓 manifest 无此号(053 归工单D,见 manifest 注释);"
              "已核『盘上有清单没有』为空")
    print(f"迁移清单({args.band} 号段,从 manifest 机械枚举){len(migrations)} 个;"
          f"其中判据包用到 {len(pkg)} 个:")
    for m in migrations:
        mark = "★" if m.name in pkg else " "
        print(f"   {mark} {m.relative_to(ROOT).as_posix()}")

    conn = _conn(dsn)
    try:
        print("\n① 冷建:装生产 schema 快照 …")
        _wob._load_prod_schema(conn)                      # noqa: SLF001
        print("   ✅ 生产 schema 已装")

        print(f"② 冷建:按 manifest 顺序应用 {args.band} …")
        for m in migrations:
            _apply(conn, m)
            print(f"   ✅ {m.name}")
        snap_a = _snapshot(dsn)
        print(f"   指纹A:表 {len(snap_a.get('tables', {}))} · "
              f"索引 {len(snap_a.get('indexes', {}))} · 约束 {len(snap_a.get('constraints', {}))}")

        print("\n③ **同库二跑**:同一批迁移再来一遍 …")
        errors = []
        for m in migrations:
            try:
                _apply(conn, m)
                print(f"   ✅ {m.name}")
            except Exception as exc:                      # noqa: BLE001
                errors.append({"file": m.name, "error": f"{type(exc).__name__}: {exc}"})
                print(f"   🔴 {m.name}:{type(exc).__name__}: {exc}")
        snap_b = _snapshot(dsn)
    finally:
        conn.close()

    print("\n④ 判定")
    same = snap_a == snap_b
    diffs = []
    if not same:
        for key in sorted(set(snap_a) | set(snap_b)):
            a, b = snap_a.get(key), snap_b.get(key)
            if a != b:
                only_a = sorted(set(a or {}) - set(b or {}))[:8]
                only_b = sorted(set(b or {}) - set(a or {}))[:8]
                diffs.append({"section": key, "only_round1": only_a, "only_round2": only_b})
    verdict = {
        "db": db, "band": args.band, "gaps": gaps,
        "migrations": [m.name for m in migrations],
        "round2_errors": errors, "schema_identical": same, "diffs": diffs,
    }
    (ROOT / ".gate9" / "coldbuild_replay.json").write_text(
        json.dumps(verdict, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"   第二轮报错 {len(errors)} · schema 指纹一致 {same}")
    if errors or not same:
        print("🔴 同库二跑不幂等 —— 停下报 Review")
        print(json.dumps(verdict, ensure_ascii=False, indent=1)[:2000])
        return 1
    print(f"✅ {args.band} 冷建通过,且同库二跑幂等(零报错 + schema 指纹逐字相同)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
