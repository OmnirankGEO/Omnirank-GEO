# -*- coding: utf-8 -*-
"""WO_209 c1 注毒闸 —— 打这一单新加/改掉的那几把锁。

工单点名的三发:
  C1 把内层键从 `SuggestedQuestion` 删掉 ⇒ 必须红(证明"跟进元素模型"之后仍有牙)
  C2 把「没跑成」的退出码 3 改成 1   ⇒ 必须红(假红与真红不许同形)
  C3 0 条路由按通过处理             ⇒ 必须红(dark 不许当合格)
外加两发打机制本身,和一发阴性对照。

🔴 毒仍绿有四解:锁没牙 / 毒没下成 / 毒够不着 / 目标冗余。
   前两解本脚本自带检查(锚恰好 1 次、sha 必变),后两解要人读「抓住它的」那一栏。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/g1_contract_gate_2026_09_15"
EXPECTED_N = 23
GATE = "scripts/response_model_contract_gate.py"
SRV = "server.py"

_DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_g1_gate_test"
ENV = dict(os.environ)
ENV["TEST_DATABASE_URL"] = _DSN
ENV["DATABASE_URL"] = _DSN
ENV["PYTHONIOENCODING"] = "utf-8"
ENV["PYTHONUTF8"] = "1"

_SUMMARY = re.compile(r"\b\d+ (passed|failed|error|errors|skipped)\b")


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def read(path):
    return io.open(os.path.join(ROOT, path), encoding="utf-8").read()


def write(path, text):
    io.open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="\n").write(text)


def run():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
         "-p", "no:cacheprovider", "-W", "ignore::DeprecationWarning"],
        cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    # 仪器坏了要出声:没有摘要行就不是「0 失败」,是没跑成。
    if not [l for l in out.splitlines() if _SUMMARY.search(l) and " in " in l]:
        raise RuntimeError("pytest 没给摘要行 —— 仪器坏了:\n" + out[-1500:])
    failed = set()
    for line in out.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            parts = line.split()
            if len(parts) > 1 and "::" in parts[1]:
                failed.add(parts[1].split(" - ")[0])
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines()
                 if "::" in l])
    return failed, total


POISONS = [
    ("NC-阴性对照:门禁里加一句无害注释(应当**绿**)", GATE,
     "def _tree_depth(",
     "# 阴性对照注入的一行,无行为影响\ndef _tree_depth(",
     "字节变了、行为没变 —— 绿才对。红了说明有判据在钉形状不钉行为。"),

    ("C1-从 SuggestedQuestion 删掉内层键 side", SRV,
     '    side: Literal["growth", "defensive"]\n', "",
     "工单点名的那一发:证明「跟进到元素模型」之后**仍然有牙**,"
     "而不是靠「干脆不看 candidates」把假阳性消掉。"),

    ("C2-把「没跑成」的退出码 3 改成 1", GATE,
     "EXIT_DID_NOT_RUN = 3", "EXIT_DID_NOT_RUN = 1",
     "假红与真红同形正是本单的起因:2026-08-14 门禁压根没跑,"
     "preflight 却打出「上线即 500」。"),

    ("C3-0 条路由按通过处理", GATE,
     '    if total_routes == 0:\n'
     '        _verdict(EXIT_DID_NOT_RUN, "收集到 0 条路由 —— server 没起来或 router 一个都没注册")',
     '    if False:\n'
     '        _verdict(EXIT_DID_NOT_RUN, "收集到 0 条路由 —— server 没起来或 router 一个都没注册")',
     "0 条路由时门禁什么都没检查;算成通过就是把 dark 当合格。"),

    ("M1-收集器又钻进嵌套函数(三条假阳性原样回来)", GATE,
     "    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:\n"
     "        self.nested_funcs[node.name] = node",
     "    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:\n"
     "        self.nested_funcs[node.name] = node\n"
     "        self.generic_visit(node)",
     "内层 helper 的返回 dict 又被当成端点自己的返回 —— 比错对象。"),

    ("M2-不再展开 `{**本地 dict}`(candidates 重新没人看)", GATE,
     "            if (isinstance(value, ast.Name) and value.id in assigned" + chr(10) +
     "                    and value.id not in mutated):",
     "            if False:",
     "不展开的话 `candidates` 这个键从头到尾没人看过 —— "
     "门禁从「误报」变成「瞎」,而瞎比误报更难发现。"),

    # ── c1':门禁**自述「看没看过」**的三个维度 ──────────────────
    ("PG-展开本地 dict 时不管它后来被改写(过期快照当可信)", GATE,
     "            if (isinstance(value, ast.Name) and value.id in assigned" + chr(10) +
     "                    and value.id not in mutated):",
     "            if (isinstance(value, ast.Name) and value.id in assigned):",
     "展开的是赋值那一刻的键集合;之后 payload['x']=… 塞进来的键看不见,"
     "却因为展开成功而记成**可信** —— 从「我不知道」退化成「我看过了」。"),

    ("PH-跟不动元素时返空列表而不是 None(谎报覆盖)", GATE,
     "        return None                 # 跟不动:这个字段里到底放了什么键,我们不知道",
     "        return []                   # 跟不动:这个字段里到底放了什么键,我们不知道",
     "docstring 早写着「跟不动返空、调用方记 False」,而调用方没做 —— "
     "注释说了什么不算数,只有代码算数。"),

    ("PI-「真跑了 N 条路由」只在通过路径打", GATE,
     '    print("真跑了 %d 条路由(其中 %d 条声明了 response_model)"' + chr(10) +
     "          % (total_routes, len(routes)))",
     '    if not findings:' + chr(10) +
     '        print("真跑了 %d 条路由(其中 %d 条声明了 response_model)"' + chr(10) +
     "              % (total_routes, len(routes)))",
     "§1.3 要求每次都打。只在通过路径打的话,真出现违规那天 "
     "落盘那条判据会自己红,而红的原因跟那个违规无关。"),

    # ── c1'':两种「元素零比较却记可信」的形状 ────────────────────
    ("PJ-内层函数跟不到返回值时不置不可信", GATE,
     "                unresolved = [nm for nm in inner.returned_names" + chr(10) +
     "                              if nm not in inner.assigned]" + chr(10) +
     "                if (inner.returned_calls or inner.has_unanalyzable_return" + chr(10) +
     "                        or unresolved or not inner.returned_dicts):",
     "                if (inner.returned_calls or inner.has_unanalyzable_return):",
     "`def _c(x): return x` / `d = build(x); return d` —— 元素一条都没收到,"
     "却因为没触发任何一条「跟不动」而记成可信。"),

    ("PK-本地 dict 被当参数传出去后仍按可信展开", GATE,
     "        for arg in list(node.args) + [kw.value for kw in node.keywords]:" + chr(10) +
     "            if isinstance(arg, ast.Name):" + chr(10) +
     "                self.mutated.add(arg.id)",
     "        pass",
     "`enrich(payload)` 之后 payload 里有什么静态看不见;被调方塞一个键,"
     "门禁照样声称「我展开看过了」。"),

    ("M3-时间戳不落盘(dark 检测失去依据)", GATE,
     "    where = _write_last_ok(stamp)",
     "    where = None",
     "落不了盘 ⇒ 没有「上次成功运行」⇒ dark 超 48h 这件事永远发现不了。"),
]


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    selected = [p for p in POISONS
                if not only or any(p[0].startswith(k) for k in only)]
    if only:
        print("子集模式:%d/%d 发" % (len(selected), len(POISONS)))

    paths = sorted({p for _, p, _, _, _ in selected})
    base_sha = {p: sha(p) for p in paths}
    base_src = {p: read(p) for p in paths}

    failed, total = run()
    print("基线: 收集 %d 条 / 失败集 %d 条" % (total, len(failed)))
    if failed:
        print("  !! 基线失败集非空 —— 红基线会让每发毒都像命中")
        for f in sorted(failed):
            print("     " + f)
        return 2
    if not only and total != EXPECTED_N:
        print("  !! 收集到 %d 条,预期 %d 条" % (total, EXPECTED_N))
        return 2

    rows = []
    for name, path, old, new, why in selected:
        src = base_src[path]
        cnt = src.count(old)
        if cnt != 1:
            rows.append((name, "毒没下成", "锚命中 %d 次(要 1 次)" % cnt))
            continue
        write(path, src.replace(old, new, 1))
        psha = sha(path)
        if psha == base_sha[path]:
            write(path, base_src[path])
            rows.append((name, "毒没下成", "sha 没变"))
            continue
        try:
            pf, ptotal = run()
        finally:
            write(path, base_src[path])
            assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        if name.startswith("NC-"):
            verdict = "绿(阴性对照 OK)" if not pf else "红(!! 阴性对照不该红)"
        else:
            verdict = "红(锁有牙)" if pf else "绿(没牙/够不着/冗余)"
        rows.append((name, verdict, "sha %s->%s · 收集 %d · 抓住它的: %s" % (
            base_sha[path], psha, ptotal,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:110] or "无")))

    print("")
    for name, verdict, detail in rows:
        print("%-46s %-22s %s" % (name, verdict, detail))
    f2, t2 = run()
    print("")
    print("还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
