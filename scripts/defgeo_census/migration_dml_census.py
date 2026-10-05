"""防御型 GEO · WP0 census ③:迁移体内 DML 扫描器。

为什么要一把**正经**的尺子
--------------------------
本仓 prestart **每次部署无条件重放全部迁移**(无追踪表),所以迁移体内的 DML
是常驻地雷:一条回填会在每次部署重跑一遍。Review-CTO 2026-08-21 把
「迁移体零 DML」列为 040/041 的附带条件。

第一版尺子是一行 grep::

    grep -icE "\\b(INSERT +INTO|UPDATE +[a-z_]+ +SET|DELETE +FROM)\\b"

它对 040/041 报 0 —— 但对**已知含一条回填 UPDATE** 的 039 也报 0。
反向对照为 0 ⇒ **尺子坏了,那个 0 不算数**(本仓「全阴性先怀疑尺子」)。

两个真因:
  ① DML 常常跨行:``UPDATE publish_records`` 独占一行,``SET`` 在下一行。
     逐行正则永远拼不到一起。
  ② 反过来,``BEFORE UPDATE ON t`` / ``FOR EACH ROW`` 里的 UPDATE 是
     **触发器事件**,不是 DML。只按关键字数会把 040/041 自己的不可变 trigger
     误报成 DML —— 那种误报同样致命,它会逼人去加白名单,而白名单迟早会
     把真 DML 一起放过。

所以这把尺子:先剥注释与字符串 → 再按**语句**切分 → 再判语句首词,
并显式排除触发器事件上下文。且自带 selftest:拿 039 当正样本、
拿 040/041 当负样本,两边都必须如预期,否则 abort。

用法::

    python scripts/defgeo_census/migration_dml_census.py            # 扫 manifest 全量
    python scripts/defgeo_census/migration_dml_census.py --selftest # 验尺子有判别力
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: 反向对照锚:**已知含 DML** 的迁移。尺子对它必须报非 0,否则尺子是坏的。
KNOWN_DML_MIGRATION = "scripts/migration_publish_records_url_verification_2026_08_19.sql"

#: 本包新增、要求**零 DML** 的迁移。
DEFGEO_MIGRATIONS = (
    "db/migration_040_defgeo_question_plans_2026_08_21.sql",
    "db/migration_041_defgeo_run_previews_2026_08_21.sql",
)

_LINE_COMMENT = re.compile(r"--[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_SINGLE_QUOTED = re.compile(r"'(?:[^']|'')*'", re.DOTALL)
_DOLLAR_QUOTED = re.compile(r"\$(\w*)\$.*?\$\1\$", re.DOTALL)

#: 触发器事件上下文里的 UPDATE/INSERT/DELETE 是**事件名**,不是语句。
_TRIGGER_EVENT = re.compile(
    r"\b(?:BEFORE|AFTER|INSTEAD\s+OF)\s+(?:INSERT|UPDATE|DELETE)"
    r"(?:\s+OR\s+(?:INSERT|UPDATE|DELETE))*",
    re.IGNORECASE,
)
#: ``ON UPDATE CASCADE`` / ``ON DELETE SET NULL`` 里的同样不是语句。
_REFERENTIAL_ACTION = re.compile(r"\bON\s+(?:UPDATE|DELETE)\s+(?:CASCADE|RESTRICT|SET|NO)\b", re.IGNORECASE)

_DML_HEAD = re.compile(r"^(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|COPY)\b", re.IGNORECASE)


def _strip_noise(sql: str) -> str:
    """剥注释、字符串字面量与 $$ 块。

    🔴 $$ 块要整块剥掉:040/041 的不可变 trigger 函数体里有 ``RAISE EXCEPTION``
       和列名比较,里面出现 UPDATE 字样纯属文案。**但**这也意味着本尺子
       看不见函数体内的 DML —— 那是刻意的取舍:函数体在 CREATE FUNCTION 时
       不执行,prestart 重放它不会写数据;真正每次重放都跑的是顶层语句。
       这条局限写在这里,不藏着。
    """
    sql = _BLOCK_COMMENT.sub(" ", sql)
    sql = _LINE_COMMENT.sub(" ", sql)
    sql = _DOLLAR_QUOTED.sub(" $$BODY$$ ", sql)
    sql = _SINGLE_QUOTED.sub(" 'S' ", sql)
    sql = _TRIGGER_EVENT.sub(" TRIGGER_EVENT ", sql)
    sql = _REFERENTIAL_ACTION.sub(" REFACTION ", sql)
    return sql


def scan(path: Path) -> list[str]:
    """返回该迁移顶层 DML 语句的首 60 字符列表。空列表 = 零 DML。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    cleaned = _strip_noise(raw)
    hits: list[str] = []
    for stmt in cleaned.split(";"):
        s = stmt.strip()
        if not s:
            continue
        # 语句可能以 WITH ... 开头再接 DML;把前导 CTE 掐掉再看首词。
        s_head = re.sub(r"^WITH\b.*?\)\s*", "", s, flags=re.IGNORECASE | re.DOTALL)
        if _DML_HEAD.match(s_head):
            hits.append(re.sub(r"\s+", " ", s_head)[:60])
    return hits


def selftest() -> int:
    """🔴 用之前先证明尺子有判别力。正负样本都必须如预期,否则 abort。"""
    ok = True
    anchor = ROOT / KNOWN_DML_MIGRATION
    if not anchor.exists():
        print(f"[selftest] 反向对照锚不存在:{KNOWN_DML_MIGRATION} —— 请更新锚,不要跳过自检")
        return 3
    pos = scan(anchor)
    if pos:
        print(f"[selftest] ✅ 正样本 {anchor.name}: 命中 {len(pos)} 条 DML")
        for h in pos:
            print(f"            · {h}")
    else:
        print(f"[selftest] 🔴 正样本 {anchor.name} 命中 0 条 —— **尺子坏了**,任何 0 都不算数")
        ok = False
    for rel in DEFGEO_MIGRATIONS:
        p = ROOT / rel
        if not p.exists():
            print(f"[selftest] 🔴 负样本缺失:{rel}")
            ok = False
            continue
        neg = scan(p)
        if neg:
            print(f"[selftest] 🔴 负样本 {p.name} 意外命中 {len(neg)} 条:{neg}")
            ok = False
        else:
            print(f"[selftest] ✅ 负样本 {p.name}: 0 条 DML")
    return 0 if ok else 1


def build() -> dict:
    from db.migration_manifest import MIGRATIONS

    out = {}
    for rel in MIGRATIONS:
        p = ROOT / rel
        if not p.exists():
            continue
        hits = scan(p)
        if hits:
            out[rel] = hits
    return {
        "manifest_count": len(MIGRATIONS),
        "migrations_with_dml": out,
        "defgeo_migrations": {rel: scan(ROOT / rel) for rel in DEFGEO_MIGRATIONS
                              if (ROOT / rel).exists()},
    }


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass
    if "--selftest" in argv:
        return selftest()
    sys.path.insert(0, str(ROOT))
    data = build()
    if "--json" in argv:
        json.dump(data, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
        sys.stdout.write("\n")
        return 0
    print(f"manifest 共 {data['manifest_count']} 条迁移;含顶层 DML 的 {len(data['migrations_with_dml'])} 条:")
    for rel, hits in sorted(data["migrations_with_dml"].items()):
        print(f"  · {rel}  ({len(hits)} 条)")
    print("\n本包新增迁移:")
    for rel, hits in sorted(data["defgeo_migrations"].items()):
        mark = "✅ 零 DML" if not hits else f"🔴 {len(hits)} 条 DML: {hits}"
        print(f"  · {rel}  {mark}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
