"""[ADVISOR-OWNERSHIP] 变异自检 —— 严格全红。

每条变异期望转红的用例**必须全部转红**,少一条即判该变异未被有效杀死。
(松口径"有一条红就算杀掉"会让锁的覆盖被高估。)

三关自检:锚点恰好命中 1 次 / 文件字节真变了 / 期望的每一条都转红。

    python -X utf8 tests/advisor_ownership_2026_08_04/mutation_runner.py
"""
import io
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))

ADV = os.path.join("api", "advisor_api.py")
SRV = "server.py"

MUTATIONS = [
    # ---------- 判定核心 ----------
    # [开源 E3 · B3c · 2026-09-28] M1–M15 守的是 advisor_api 的对话端点与归属守卫(decide_conversation_access /
    #   _guard_conversation / _current_user_or_401 / _get_or_create_conversation),这些随社媒顾问对话一起删了,
    #   锚点全部不在 ⇒ 退役。换成一条守新形状的:advisor_api 不许再写对话表。
    dict(id="M18", file=ADV, desc="advisor_api 重新出现写对话表的 SQL(归属校验已随对话端点删除)",
         edits=['_DEFAULT_WRITER_KEY = "default_writer_id"\n',
                '_DEFAULT_WRITER_KEY = "default_writer_id"\n'
                '_M18 = "INSERT INTO advisor_conversations (advisor_id, conversation_id) VALUES (%s, %s)"\n'],
         expect_red=["test_advisor_api_no_longer_writes_conversations"]),

    # ---------- server.py 影子路由 ----------
    dict(id="M16", file=SRV, desc="server.py 重新长出一条 /api/advisors 影子路由",
         edits=["# 复杂任务 API (Project Director Handoffs)\n# ============================================\n",
                "# 复杂任务 API (Project Director Handoffs)\n# ============================================\n\n@app.get(\"/api/advisors/recent-conversations\")\n"
                "def api_recent_conversations(request: Request, limit: int = 10):\n    return {\"conversations\": []}\n"],
         expect_red=["test_server_has_no_advisor_shadow_route"]),
    dict(id="M17", file=SRV, desc="include_router(advisor_router) 被注释掉(顾问板块整体 404)",
         edits=["    from api.advisor_api import router as advisor_router\n    app.include_router(advisor_router)",
                "    from api.advisor_api import router as advisor_router\n    pass  # include_router(advisor_router)"],
         expect_red=["test_include_router_registered_for_advisor_router"]),
]


def _run_tests():
    r = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "pytest",
         os.path.join("tests", "advisor_ownership_2026_08_04"), "-q", "-rfE"],
        cwd=_ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ,
             "TEST_DATABASE_URL": os.environ.get(
                 "TEST_DATABASE_URL", "postgresql://u:p@127.0.0.1:5432/omnirank_test"),
             "PYTHONIOENCODING": "utf-8"},
    )
    red = set()
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        # 单个 -rfE:FAILED 与 ERROR 都会打。两个 -r 会互相覆盖 —— 别再写成 "-rf","-rE"。
        if s.startswith("FAILED ") or s.startswith("ERROR "):
            tail = s.split("::")[-1]
            red.add(tail.split()[0].split("[")[0])
    return red, r.returncode


def main() -> int:
    files = sorted({m["file"] for m in MUTATIONS})
    backup = {f: io.open(os.path.join(_ROOT, f), encoding="utf-8", newline="").read()
              for f in files}

    base_red, base_rc = _run_tests()
    if base_rc != 0:
        print(f"🔴 基线就不绿,先修基线。红:{sorted(base_red)}")
        return 1
    print(f"基线:✅ 绿({len(MUTATIONS)} 条变异待跑)\n")

    survivors = []
    for mut in MUTATIONS:
        path = os.path.join(_ROOT, mut["file"])
        orig = backup[mut["file"]]
        src = orig
        ok = True
        for anchor, repl in [mut["edits"]] if isinstance(mut["edits"][0], str) else mut["edits"]:
            n = src.count(anchor)
            if n != 1:                       # 关 1:锚点必须恰好命中 1 次
                print(f"{mut['id']} 🔴 锚点命中 {n} 次(应=1),变异作废 —— {mut['desc']}")
                ok = False
                break
            src = src.replace(anchor, repl, 1)
        if not ok:
            survivors.append(mut["id"])
            continue
        if src == orig:                      # 关 2:文件字节必须真变了
            print(f"{mut['id']} 🔴 文件没变,变异无效")
            survivors.append(mut["id"])
            continue

        io.open(path, "w", encoding="utf-8", newline="").write(src)
        try:
            red, _ = _run_tests()
        finally:
            io.open(path, "w", encoding="utf-8", newline="").write(orig)

        want = set(mut["expect_red"])
        missing = want - red                 # 关 3:严格全红
        if missing:
            print(f"{mut['id']} 🔴 没杀 —— {mut['desc']}\n"
                  f"      期望转红但没红:{sorted(missing)}")
            survivors.append(mut["id"])
        else:
            print(f"{mut['id']} ✅ 转红 {sorted(want)}  —— {mut['desc']}")

    # 还原后必须回到绿
    for f, s in backup.items():
        io.open(os.path.join(_ROOT, f), "w", encoding="utf-8", newline="").write(s)
    _, rc = _run_tests()
    print(f"\n还原后基线:{'✅ 绿' if rc == 0 else '🔴 红(还原有问题)'}")
    print(f"变异 {len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} 被杀")
    return 0 if (not survivors and rc == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
