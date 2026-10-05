#!/usr/bin/env python
"""变异 runner:证明 tests/admin_user_governance/test_provider_downgrade_blockers.py
每条判据都真能抓到「服务商降级 UX / 治理绕过」这一类退化。

用法:
    TEST_DATABASE_URL=... python scripts/mutation_provider_downgrade_2026_08_06.py

🔴 三条防自欺:
  1. 子进程跑 pytest 且 PYTHONDONTWRITEBYTECODE=1 —— .pyc 缓存会把「已杀死」误报成「存活」。
  2. 锚点改不上去一律记 ERROR,不算 SKIP 放过(改不上去 = 那条判据本轮压根没被验证)。
  3. 读写都用 newline="" —— 否则 Windows 上会把整份源文件的行尾改掉,变成一次假变异。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/admin_user_governance/test_provider_downgrade_blockers.py"

GOVERNANCE = ROOT / "services" / "admin_user_governance.py"
PARTNER = ROOT / "api" / "partner_api.py"
API = ROOT / "api" / "admin_user_governance_api.py"

# (名字, 文件, 锚点, 替换)
MUTATIONS = [
    (
        "B01 拒绝话术退回「12 个计数器一股脑」",
        GOVERNANCE,
        'summary = "、".join(item["label"] for item in items)',
        'summary = f"{bound_customers} 个商业绑定客户、{active_channel_relations} 条现役渠道关系"',
    ),
    (
        "B02 零值项也一起列出来",
        GOVERNANCE,
        "        if value <= 0:\n            continue",
        "        if value < 0:\n            continue",
    ),
    (
        "B03 降级守卫少掉「现役渠道关系」这一项",
        GOVERNANCE,
        "is_platform_direct or bound_customers or active_channel_relations",
        "is_platform_direct or bound_customers",
    ),
    (
        "B04 自助降级退回裸 UPDATE",
        PARTNER,
        '        result = change_business_identity(',
        '        cursor.execute("UPDATE user_wallets SET agent_level = 0 WHERE user_id=%s", (user_id,))\n'
        '        result = _legacy_noop(',
    ),
    (
        "B05 自助降级不再走治理原语",
        PARTNER,
        "result = change_business_identity(",
        "result = _legacy_direct_write(",
    ),
    (
        "B06 某一项丢了「去哪里处理」",
        GOVERNANCE,
        '"逐个把客户转交给其他服务商或转为平台直营，再回到本向导", "commercial_binding"),',
        '"", "commercial_binding"),',
    ),
    (
        "B07 结构化明细不再透到 HTTP 层",
        API,
        'detail.update(getattr(exc, "details", None) or {})',
        "pass",
    ),
    (
        "B08 渠道关系那条不再讲顺序陷阱",
        GOVERNANCE,
        "\"先在本向导终结该服务商的现役上下游渠道关系（降级后就再也改不了，必须先做）\"",
        "\"处理一下渠道关系即可\"",
    ),
    (
        "B09 渠道关系那条不再指向可操作入口",
        GOVERNANCE,
        '"先在本向导终结该服务商的现役上下游渠道关系（降级后就再也改不了，必须先做）",\n     "channel_relationship"),',
        '"先在本向导终结该服务商的现役上下游渠道关系（降级后就再也改不了，必须先做）",\n     None),',
    ),
]


def run_tests() -> int:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", TESTS, "-q", "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.returncode


def main() -> int:
    if not os.getenv("TEST_DATABASE_URL"):
        print("🔴 需要 TEST_DATABASE_URL(仓库 conftest 强制),否则整轮恒红=零判别力")
        return 1

    originals = {path: path.read_text(encoding="utf-8", newline="") for path in (GOVERNANCE, PARTNER, API)}

    if run_tests() != 0:
        print("🔴 基线就是红的 —— 变异结果证明不了任何事,先修")
        return 1
    print("✅ 基线绿 · 开始变异\n")

    killed = survived = errored = 0
    try:
        for name, path, anchor, replacement in MUTATIONS:
            source = originals[path]
            if anchor not in source:
                print(f"  ⚠️  ERROR    {name} — 锚点未命中(判别力本轮未验证)")
                errored += 1
                continue
            path.write_text(source.replace(anchor, replacement, 1), encoding="utf-8", newline="")
            if run_tests() != 0:
                print(f"  ✅ KILLED   {name}")
                killed += 1
            else:
                print(f"  ❌ SURVIVED {name}")
                survived += 1
            path.write_text(source, encoding="utf-8", newline="")
    finally:
        for path, source in originals.items():
            path.write_text(source, encoding="utf-8", newline="")

    for path, source in originals.items():
        if path.read_text(encoding="utf-8", newline="") != source:
            print(f"\n🔴 还原失败:{path}")
            return 1

    print(f"\n变异 {len(MUTATIONS)}:KILLED {killed} · SURVIVED {survived} · ERROR {errored}")
    bad = survived + errored
    print("🔴 变异未全红" if bad else "✅ 变异全红 · 判据有判别力")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
