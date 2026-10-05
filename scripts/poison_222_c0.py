# -*- coding: utf-8 -*-
"""WO_222-c0 注毒台:证明这 8 条判据**有牙**。

🔴 最忠实的毒不是我现编一段「像额度的代码」,而是**把删掉的那两段从基线
   `e7669e699` 原样恢复回去**。现编的毒只能证明「判据认得我编的那个形状」;
   原样恢复才能证明「判据认得**真正被删掉的那个东西**」——
   本仓 `a-structural-lock-needs-a-structural-poison` 的同一件事。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PKG = "tests/l0_client_quota_lifted_2026_09_15"
EXPECTED_N = 8
BASE = "e7669e699"
DSN = "postgresql://geo_admin:testpw@127.0.0.1:55492/geo_c14_222_test"


def _at_base(rel):
    out = subprocess.run(["git", "show", "%s:%s" % (BASE, rel)],
                         cwd=ROOT, capture_output=True)
    assert out.returncode == 0, "取不到基线版本 %s:%s" % (BASE, rel)
    return out.stdout.decode("utf-8")


def _restored_block(rel, start_mark, end_mark):
    """从基线版本里切出被删掉的那一段(含两端锚之间的全部内容)。"""
    src = _at_base(rel)
    s = src.index(start_mark)
    e = src.index(end_mark, s)
    return src[s:e]


#: (名字, 文件, 现在树上的锚, 换成什么(可调用,返回字符串), 说明)
def _brand_quota():
    return _restored_block(
        "api/brand_api.py", "    # v1_4: L0 额度检测", "    with get_db() as conn:")


def _profile_quota():
    return _restored_block(
        "api/profile_api.py",
        '    if not existing and user and not user.get("is_admin"):',
        "    if existing:")


POISONS = [
    #: 🔴 NC 第一版钉 `user_id = user["user_id"]` —— 那行在 brand_api 里有 **10 处**。
    #:   注毒台拒判(「毒没下成」)而不是报假绿。负样本的锚也得唯一:
    #:   它要证明的是「改一处无关的地方不会红」,改十处证明的是别的事。
    ("NC 无害注释", "api/brand_api.py",
     lambda: "    # [P0-1] 品牌名入库校验必须在同名查询之前：脏名字会污染",
     lambda: "    # [P0-1] 品牌名入库校验必须在同名查询之前：脏名字会污染(无害注释)",
     "负样本 —— 必须读绿", 1),

    ("N1 恢复 L0 品牌额度(原样)", "api/brand_api.py",
     lambda: "    # 🔴 [WO_222-c0 · Owner 直令 2026-09-15] **L0 品牌额度已撤**。",
     lambda: _brand_quota() + "    # 🔴 [WO_222-c0 · Owner 直令 2026-09-15] **L0 品牌额度已撤**。",
     "秦老师被卡住的那一道重新装回去 ⇒ 建第 2 个客户必红", 1),

    ("N2 恢复 L0 档案额度与静默覆盖(原样)", "api/profile_api.py",
     lambda: "    # 🔴 [WO_222-c0 · Owner 直令 2026-09-15] **L0 档案额度已撤**(与 add_client 同族)。",
     lambda: _profile_quota() + "    # 🔴 [WO_222-c0 · Owner 直令 2026-09-15] **L0 档案额度已撤**(与 add_client 同族)。",
     "静默覆盖路径装回去 ⇒ 甲的档案被乙冲掉,必红", 1),

    ("N3 归属改按操作者(不按商业主体)", "api/brand_api.py",
     lambda: "    principal_user_id = int(organization_identity.principal_user_id) if organization_member else user_id",
     lambda: "    principal_user_id = user_id",
     "撤额度时顺手带走归属那一半 ⇒ 员工建的品牌变孤儿,必红", 1),

    #: 🔴 这个 WHERE 在 `list_my_clients` 里有**两条查询路径**各一份(:842 / :898)。
    #:   只毒一条 = 留了一条活路,判据可能靠另一条读绿 —— 那是「毒够不着」的变种。
    #:   所以期望命中 2 次并**两处同时**下毒。
    ("N4 列表按身份过滤(两条路径同时)", "api/brand_api.py",
     lambda: "                WHERE b.owner_user_id = %s AND b.brand_type = 'client'",
     lambda: "                WHERE b.owner_user_id = %s AND b.brand_type = 'client' AND FALSE",
     "建成功但列表吞掉 —— 对用户是同一件事", 2),
]


def sha(rel):
    return hashlib.sha256(io.open(os.path.join(ROOT, rel), "rb").read()).hexdigest()[:12]


def run():
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = DSN
    p = subprocess.run([sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
                        "-p", "no:cacheprovider", "-p", "no:warnings"],
                       cwd=ROOT, capture_output=True, env=env)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed = set(re.findall(r"^FAILED [^:]+::(\S+)", out, re.M))
    collected = sum(int(m.group(1))
                    for m in re.finditer(r"(\d+) (?:passed|failed|error)", out))
    return collected, failed, out


def main():
    print("=" * 78)
    base_c, base_f, base_out = run()
    print("基线: collected=%d  failed=%s" % (base_c, sorted(base_f) or "空"))
    if base_c != EXPECTED_N:
        print("🔴 基线条数 %d != EXPECTED_N %d —— 仪器没跑满,后面全部作废" % (base_c, EXPECTED_N))
        print(base_out[-1800:])
        return 2
    if base_f:
        print("🔴 基线不是全绿,红基线会让每一发毒都像命中")
        return 2

    results = []
    for name, rel, anchor_fn, repl_fn, why, want in POISONS:
        full = os.path.join(ROOT, rel)
        raw = io.open(full, "rb").read()
        src = raw.decode("utf-8")
        anchor = anchor_fn()
        hits = src.count(anchor)
        before = sha(rel)
        if hits != want:
            print("  %-34s 🔴 锚命中 %d 次(要 %d)—— 毒没下成,不判" % (name, hits, want))
            results.append((name, "ANCHOR_MISS", set()))
            continue
        io.open(full, "wb").write(src.replace(anchor, repl_fn()).encode("utf-8"))
        assert sha(rel) != before, "下毒后 sha 没变"
        try:
            c, f, _ = run()
        finally:
            io.open(full, "wb").write(raw)
        assert sha(rel) == before, "还原后 sha 对不上基线"
        new = f - base_f
        is_nc = name.startswith("NC")
        ok = (not new) if is_nc else bool(new)
        verdict = (("绿 ✓" if is_nc else "红 ✓") if ok
                   else ("🔴 负样本读红" if is_nc else "🔴 仍绿"))
        print("  %-34s %-8s 新红 %d 条  %s"
              % (name, verdict, len(new), ",".join(sorted(new))[:60]))
        results.append((name, verdict, new))

    print("-" * 78)
    bad = [r for r in results if "✓" not in r[1]]
    if bad:
        print("🔴 这些没过:%s" % [r[0] for r in bad])
        return 1
    print("✅ %d 发毒全部被抓 · 负样本绿"
          % len([r for r in results if not r[0].startswith("NC")]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
