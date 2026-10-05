"""[误报治理工单 2026-07-30 · §7.2] 变异验证:每个变异注入后必须有锁转红。

跑法:
    python scripts/mutate_self_invented_scoring_negation_2026_07_30.py

做法(不用 git checkout 还原 —— 那会冲掉未提交改动,本仓踩过):
每个变异在内存里替换目标文件正文、写盘、跑锁、**用备份原文写回**,
finally 再校验**字节**与备份一致,不一致立刻停,绝不留残留。

🔴 读写一律 read_bytes/write_bytes:Windows 上 Path.write_text 会把 \\n 翻成
\\r\\n,一次"还原"就把整个文件变成 CRLF,diff 成"全文重写"。

🔴 顶部必须 reconfigure(stdout, utf-8):上一包(短视频)漏了这条,重定向到文件时
GBK 崩、exit=1、输出只剩崩溃栈,**那份输出完全像"变异脚本挂了"** ——
跑不起来的交付物等于下任无法复验。

统计口径:FAILED 与 ERROR **都算转红**(ERROR 不是 PASS,但也不是绿);
全绿 = SURVIVED = 该锁没有判别力,必须如实打印,**不许把"N 转红 + M 存活"
写成"全转红"**。
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

try:  # Windows 控制台默认 GBK,打印 ✅/🔴 会 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "writing" / "evidence_first_policy.py"
ENGINE = ROOT / "services" / "span_level_repair.py"
ALERTS = ROOT / "services" / "governance_alerts.py"
HALL = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"

PYTEST_TARGET = "tests/test_self_invented_scoring_negation_2026_07_30.py"
NODE_TARGET = "scripts/verify-soft-findings-presentation.mjs"

# (编号, 说明, 目标文件, 原串, 变异串, 期望转红的锁, 跑法)
MUTATIONS: list[tuple[str, str, Path, str, str, str, str]] = [
    (
        "①",
        "整支「自建|自创|独家 + 评分体系」正则删掉(= 把否定排除做成全放行)",
        POLICY,
        r'    r"|(?:自建|自创|独家)(?:的)?(?:评分|评级|打分)(?:体系|模型|标准|权重)",',
        r'    r"",',
        "锁 2 / 2b / 3 / 4 / 5",
        "pytest",
    ),
    (
        "②",
        "否定排除不限同句(跨句也救):分句回退改成扫到全文开头",
        POLICY,
        "    lo = start\n    while lo > 0 and text[lo - 1] not in _CLAUSE_BOUNDARY_CHARS:\n        lo -= 1",
        "    lo = 0",
        "锁 4(跨句不救)",
        "pytest",
    ),
    (
        "③",
        "否定标记不再排除「不仅/不但/不只」(递进结构被当成否定)",
        POLICY,
        r'    r"(?:不(?!仅|但|只|光|单)|未(?!来)|没有|无法|绝不|从不|决不)"',
        r'    r"(?:不|未(?!来)|没有|无法|绝不|从不|决不)"',
        "锁 3(递进不算否定)",
        "pytest",
    ),
    (
        "④",
        "否定词允许出现在命中串**之后**(prefix 改成整个分句)",
        POLICY,
        "    prefix = text[lo:start]",
        "    prefix = text[lo:]",
        "锁 4b(否定必须在命中之前)",
        "pytest",
    ),
    (
        "⑤",
        "CANNOT_FIX 仍给 retry 按钮",
        ALERTS,
        '            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},\n'
        '            {"id": "request_human_review", "label": "提交人工签发", "type": "contact"},\n'
        '        ],\n'
        '        rule_version="span-repair-v1",\n'
        '    )\n'
        '    # [误报治理 2026-07-30 · T2] 这是**按设计的正确行为**,不是失败。',
        '            {"id": "edit_manually", "label": "自己手动改", "type": "nav"},\n'
        '            {"id": "request_human_review", "label": "提交人工签发", "type": "contact"},\n'
        '            {"id": "ai_fix_this_span", "label": "再试一次", "type": "retry"},\n'
        '        ],\n'
        '        rule_version="span-repair-v1",\n'
        '    )\n'
        '    # [误报治理 2026-07-30 · T2] 这是**按设计的正确行为**,不是失败。',
        "锁 6a(需人工不给重试)",
        "pytest",
    ),
    (
        "⑥",
        "基础设施错误也计额度(退还规则里去掉 INFRA 分支)",
        ENGINE,
        "    if not llm_invoked:\n        return True\n    return str(reason or \"\").strip() in INFRA_FAILURE_REASONS",
        "    if not llm_invoked:\n        return True\n    return False",
        "锁 6c(基础设施错误退还额度)",
        "pytest",
    ),
    (
        "⑦",
        "砍 soft 区按钮时把 hard 区按钮一起砍了",
        HALL,
        '                        data-testid="auto-repair-btn"',
        '                        data-testid="auto-repair-btn-removed"',
        "锁 7-②(hard 面板出口仍在)",
        "node",
    ),
]

#: 如实记录的预期存活(本轮为空;若出现存活必须在此登记并说明为什么)
EXPECTED_SURVIVORS: dict[str, str] = {}


def run_locks(kind: str) -> tuple[bool, str]:
    """返回 (是否全绿, 摘要)。FAILED 与 ERROR 都算转红。"""
    if kind == "pytest":
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", PYTEST_TARGET, "-q", "-p", "no:cacheprovider"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    else:
        proc = subprocess.run(
            ["node", NODE_TARGET],
            cwd=str(ROOT / "frontend"), capture_output=True, text=True,
            encoding="utf-8", errors="replace", shell=(sys.platform == "win32"),
        )
    tail = (proc.stdout or "").strip().splitlines()
    summary = tail[-1] if tail else (proc.stderr or "").strip().splitlines()[-1:] or ""
    return proc.returncode == 0, (summary if isinstance(summary, str) else str(summary))


def main() -> int:
    print("=" * 78)
    print("变异验证 · 误报治理工单 2026-07-30 §7.2")
    print("=" * 78)

    # 前置:未变异时锁必须全绿,否则后面的"转红"没有意义
    for kind, label in (("pytest", "判定/出口锁"), ("node", "前端呈现锁")):
        ok, summary = run_locks(kind)
        print(f"[基线] {label}: {'✅ 全绿' if ok else '🔴 未变异就不绿'} · {summary}")
        if not ok:
            print("基线不绿,变异结果不可信,中止。")
            return 1
    print()

    survived: list[str] = []
    for num, desc, target, old, new, expect, kind in MUTATIONS:
        backup = target.read_bytes()
        text = backup.decode("utf-8")
        if old not in text:
            print(f"{num} 🔴 锚点串没找到(文件已变?),中止 · {target.name}")
            return 1
        try:
            target.write_bytes(text.replace(old, new, 1).encode("utf-8"))
            ok, summary = run_locks(kind)
            if ok:
                survived.append(num)
                print(f"{num} ⚠️ SURVIVED(锁没转红) · {desc}")
                print(f"     期望转红: {expect} · 实际: {summary}")
            else:
                print(f"{num} ✅ 转红 · {desc}")
                print(f"     期望转红: {expect} · 实际: {summary}")
        finally:
            target.write_bytes(backup)
            restored = target.read_bytes() == backup
        # 还原校验放在 finally 之外:`return` 写在 finally 里会吞掉正在传播的异常。
        if not restored:
            print(f"{num} 🔴 还原失败!{target} 与备份不一致,立刻停。")
            return 2

    print()
    print("=" * 78)
    total = len(MUTATIONS)
    unexpected = [n for n in survived if n not in EXPECTED_SURVIVORS]
    print(f"合计 {total} 个变异:{total - len(survived)} 个转红 · {len(survived)} 个存活")
    for n in survived:
        note = EXPECTED_SURVIVORS.get(n, "🔴 未登记的存活 = 该锁没有判别力,必须查")
        print(f"  存活 {n}: {note}")
    print("=" * 78)
    return 1 if unexpected else 0


if __name__ == "__main__":
    raise SystemExit(main())
