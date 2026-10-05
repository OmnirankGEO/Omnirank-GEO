#!/usr/bin/env python3
"""门禁 · 禁止绕过服务函数裸写 `customer_agent_bindings`(工单 §P1-6 方案 b)。

存在的理由(2026-08-12 实测):
    全站 11 条 `binding_source='admin_manual'` 归属里有 4 条**没有配对审计**,
    其中 1 条(binding 67)就是应急处置时直接 INSERT 出来的 ——
    当时没有任何合法入口能写审计,于是绕过端点手写库。
    绕过一次,「旧人工归属缺少直接审计」这个治理告警就多一条永久噪音。

    工单给了两个选项:(a) 生产表触发器 (b) 门禁脚本。取 **(b)** ——
    动生产表触发器属高风险(要单独评估回滚方案),而这个洞的形态是
    **代码里再写一条写路径**,门禁在代码进仓前就能拦住,拦得更早、代价更低。
    触发器该不该加是独立议题,不在本包。

允许的写者(白名单,每条都要有理由):
  - `services/customer_binding.py`          业务侧唯一 upsert(邀请码/推荐链)
  - `services/admin_user_governance.py`     治理侧唯一写路径(CAS + 审计 + 版本位)
  - `services/commercial_binding_history.py` 历史版本投影
  - `api/admin_w4_api.py`                   W4 治理路径(既有,产出了 7 条有审计的绑定)
  - 迁移 SQL / 测试 / 文档                    结构与夹具

用法:
    python scripts/gate_binding_write_paths.py          # 全仓扫描
    python scripts/gate_binding_write_paths.py --selftest  # 证明判据有判别力
退出码:0 通过 · 1 发现违规 · 2 自检失败
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 🔴 Windows 控制台默认 GBK,输出里的 ✅/🔴 会直接 UnicodeEncodeError ——
#    门禁自己崩掉时退出码是 1,与"发现违规"同码,等于把通过/失败混成一个信号。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):  # 非 TTY / 已被重定向
        pass

# 要守的不变式是「**归属**不许绕过服务函数被改」,不是「这张表一个字都不许写」。
#
# 🔴 这条口径是被实测校正过的:第一版按"任何 INSERT/UPDATE/DELETE"扫,
#    当场命中 `api/agent_workbench_api.py` 里那条只改 `online_purchase_override`
#    的 UPDATE —— 那是每客户的线上购买开关,既不改承接方也不改来源,
#    改它不会产生任何"没有审计凭证的归属"。
#    把它算违规,门禁就会因为噪音被人关掉;把整个文件加白名单,
#    又等于放行该文件里**将来**真正改归属的写入。所以口径下沉到列。
OWNERSHIP_COLUMNS = (
    "customer_user_id", "agent_user_id", "binding_source", "source_token", "bound_at",
    "admin_override_user_id", "admin_override_at", "dispute_status", "dispute_note",
)

# INSERT / DELETE 天然改变归属的存在性,一律算写。
INSERT_DELETE_PATTERN = re.compile(
    r"\b(INSERT\s+INTO|DELETE\s+FROM)\s+(?:public\.)?customer_agent_bindings\b",
    re.IGNORECASE,
)
# UPDATE 要看 SET 了什么 —— 判据打在 SET 子句上,不打在整条语句上
# (WHERE 里出现 customer_user_id 是**读**条件,不是写)。
UPDATE_PATTERN = re.compile(
    r"\bUPDATE\s+(?:public\.)?customer_agent_bindings\b(?P<tail>.{0,2000})",
    re.IGNORECASE | re.DOTALL,
)
SET_CLAUSE_PATTERN = re.compile(
    r"\bSET\b(?P<body>.*?)(?=\bWHERE\b|\bRETURNING\b|\bFROM\b|;|\"\"\"|'''|$)",
    re.IGNORECASE | re.DOTALL,
)

ALLOWED_FILES = {
    "services/customer_binding.py",
    "services/admin_user_governance.py",
    "services/commercial_binding_history.py",
    "api/admin_w4_api.py",
}

# 目录级豁免:结构定义与验证夹具本来就要写这张表。
ALLOWED_PREFIXES = ("tests/", "scripts/", "db/", "docs/")

SCAN_SUFFIXES = (".py", ".sql")


def iter_candidate_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in SCAN_SUFFIXES:
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", "node_modules/", "frontend/node_modules/", "venv/", ".venv/")):
            continue
        yield rel, path


def _lineno_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _sets_ownership_column(set_body: str) -> bool:
    """SET 子句里是否出现归属列。

    只看等号左侧的赋值目标:`SET online_purchase_override = %(x)s` 不算,
    但 `SET agent_user_id = ...` / `SET binding_source='admin_manual'` 算。
    """
    for assignment in set_body.split(","):
        target = assignment.split("=", 1)[0].strip().strip('"').lower()
        # 去掉可能的表别名前缀
        target = target.rsplit(".", 1)[-1]
        if target in OWNERSHIP_COLUMNS:
            return True
    return False


def scan(root: Path) -> list[tuple[str, int, str]]:
    violations: list[tuple[str, int, str]] = []
    for rel, path in iter_candidate_files(root):
        if rel in ALLOWED_FILES or rel.startswith(ALLOWED_PREFIXES):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if "customer_agent_bindings" not in text:
            continue
        for match in INSERT_DELETE_PATTERN.finditer(text):
            violations.append((
                rel, _lineno_of(text, match.start()), match.group(0).strip()[:160],
            ))
        for match in UPDATE_PATTERN.finditer(text):
            set_match = SET_CLAUSE_PATTERN.search(match.group("tail"))
            if not set_match:
                # 没找到 SET —— 语句被截断或形态未知。fail-closed 报出来让人看一眼,
                # 静默放行才是真的危险(那正是"以为拦住了、其实没拦"的形态)。
                violations.append((
                    rel, _lineno_of(text, match.start()),
                    "UPDATE customer_agent_bindings(未能解析 SET 子句,请人工确认)",
                ))
                continue
            if _sets_ownership_column(set_match.group("body")):
                violations.append((
                    rel, _lineno_of(text, match.start()),
                    "UPDATE customer_agent_bindings SET "
                    + " ".join(set_match.group("body").split())[:120],
                ))
    return violations


def selftest() -> int:
    """证明判据有判别力 —— 正反两侧都要动。

    🔴 只验"当前仓库通过"是**零判别力**的:一个恒返回空列表的扫描也能通过。
       必须造出违规样本让它转红,再确认合法写者不会被误报。
    """
    import tempfile

    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        # ① 正面:非白名单文件里的裸写 → 必须命中
        (root / "api").mkdir()
        (root / "api" / "rogue_api.py").write_text(
            "cur.execute(\"INSERT INTO customer_agent_bindings(customer_user_id) VALUES (1)\")\n",
            encoding="utf-8",
        )
        hits = scan(root)
        if len(hits) != 1 or hits[0][0] != "api/rogue_api.py":
            print(f"🔴 自检①失败:裸写没被抓到 · hits={hits}")
            ok = False
        else:
            print("✅ 自检① 非白名单裸写 → 命中")

        # ② 反面:白名单文件里的同一句 → 必须不命中
        (root / "services").mkdir()
        (root / "services" / "customer_binding.py").write_text(
            "cur.execute(\"INSERT INTO customer_agent_bindings(customer_user_id) VALUES (1)\")\n",
            encoding="utf-8",
        )
        hits = scan(root)
        if any(rel == "services/customer_binding.py" for rel, _, _ in hits):
            print("🔴 自检②失败:白名单写者被误报")
            ok = False
        else:
            print("✅ 自检② 白名单写者 → 不误报")

        # ③ 反面:只读该表 → 必须不命中(否则门禁会变成噪音)
        (root / "api" / "reader_api.py").write_text(
            "cur.execute(\"SELECT * FROM customer_agent_bindings WHERE customer_user_id=%s\")\n",
            encoding="utf-8",
        )
        hits = scan(root)
        if any(rel == "api/reader_api.py" for rel, _, _ in hits):
            print("🔴 自检③失败:纯读被当成写")
            ok = False
        else:
            print("✅ 自检③ 纯读 → 不命中")

        # ④ 正面:改归属列的 UPDATE / DELETE 也算写
        (root / "api" / "rogue_update.py").write_text(
            "cur.execute(\"UPDATE customer_agent_bindings SET agent_user_id=2 WHERE id=1\")\n"
            "cur.execute(\"DELETE FROM customer_agent_bindings WHERE id=1\")\n",
            encoding="utf-8",
        )
        hits = [h for h in scan(root) if h[0] == "api/rogue_update.py"]
        if len(hits) != 2:
            print(f"🔴 自检④失败:改归属列的 UPDATE/DELETE 没被当成写 · hits={hits}")
            ok = False
        else:
            print("✅ 自检④ 改归属列的 UPDATE / DELETE → 命中")

        # ⑤ 反面:只改非归属列(线上购买开关)→ 必须不命中。
        #    这一条是被生产代码校正出来的:api/agent_workbench_api.py 就是这个形态,
        #    第一版按"任何 UPDATE"扫会把它误报,门禁随即会被人关掉。
        (root / "api" / "switch_api.py").write_text(
            "cur.execute(\"\"\"UPDATE customer_agent_bindings\n"
            "   SET online_purchase_override = %(override)s\n"
            "   WHERE customer_user_id = %(cid)s AND agent_user_id = %(aid)s\"\"\")\n",
            encoding="utf-8",
        )
        hits = [h for h in scan(root) if h[0] == "api/switch_api.py"]
        if hits:
            print(f"🔴 自检⑤失败:只改非归属列被误报 · hits={hits}")
            ok = False
        else:
            print("✅ 自检⑤ 只改非归属列 → 不误报")

        # ⑥ 正面:同一条 UPDATE 里混进归属列 → 必须命中(防"夹带")
        (root / "api" / "sneaky_api.py").write_text(
            "cur.execute(\"\"\"UPDATE customer_agent_bindings\n"
            "   SET online_purchase_override = %(o)s, agent_user_id = %(a)s\n"
            "   WHERE customer_user_id = %(c)s\"\"\")\n",
            encoding="utf-8",
        )
        hits = [h for h in scan(root) if h[0] == "api/sneaky_api.py"]
        if len(hits) != 1:
            print(f"🔴 自检⑥失败:夹带归属列没被抓到 · hits={hits}")
            ok = False
        else:
            print("✅ 自检⑥ 非归属列里夹带归属列 → 命中")
    return 0 if ok else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selftest", action="store_true")
    args = parser.parse_args()
    if args.selftest:
        return selftest()

    violations = scan(REPO_ROOT)
    if violations:
        print("🔴 发现绕过服务函数直接写 customer_agent_bindings 的代码:")
        for rel, lineno, line in violations:
            print(f"   {rel}:{lineno}  {line}")
        print()
        print("   商业服务归属只允许走以下写者(它们负责审计 / CAS / 版本位):")
        for allowed in sorted(ALLOWED_FILES):
            print(f"     · {allowed}")
        print("   绕过写入的归属会永久亮「旧人工归属缺少直接审计」,且无人知道当时为什么这么定。")
        return 1
    print("✅ 未发现绕过服务函数的 customer_agent_bindings 写入")
    return 0


if __name__ == "__main__":
    sys.exit(main())
