# -*- coding: utf-8 -*-
"""WO_204 c1 邻包归属:把本单改过的文件按字节退回基线,比两边的失败集。

只有这样才分得清「我弄红的」与「本来就红的」——
直接报一个失败集等于把存量红算到自己头上,或者把自己的缺陷算成存量。
"""
import hashlib
import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_REV = "4152dfe16"
FILES = [
    "api/geo_douyin_api.py",
    "db/geo_douyin_db.py",
    "db/migration_manifest.py",
    "services/geo_douyin/distill_task.py",
    "services/geo_douyin/production_task.py",
]

# 本单改过的文件的**消费方**(全仓 grep 得到,不是手挑)
PKGS = [
    "tests/article_self_report_2026_08_19",
    "tests/geo_douyin_publish_converge_2026_09_08",
    "tests/geo_image_note_2026_08_17",
    "tests/geo_imgnote_d1_2026_09_13",
    "tests/geo_imgnote_d1b_2026_09_13",
    "tests/geo_imgnote_d2_2026_09_13",
    "tests/hotfix_delivery_plan_2026_08_20",
    "tests/hotfix_to_thread_2026_08_20",
    "tests/inventory_distribution_chain_2026_08_12",
    "tests/orphanmon_2026_08_10",
    "tests/post_quote_binding_2026_09_08",
    "tests/production_quote_2026_09_08",
    "tests/publish_dispatch_industry_2026_08_17",
]

# 🔴 没有 conftest 的包用**专用**兜底库,不用本单自己的那个:
#    灌自己的库既污染本单判据的世界,又让邻包跑在一个不属于它的库上,
#    而两种坏法都只表现为"偶尔红一条"。
FALLBACK_DB = "geo_c14_nb_test"


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def dsn_for(pkg):
    """取这个包**自己**的库。

    🔴 不能给所有包灌同一个 DSN:包 conftest 里多是 `setdefault`,
       外面设了就把人家的库劫持了 —— #196 那次就这么造出 7 个假 ERROR。
    """
    cf = os.path.join(ROOT, pkg, "conftest.py")
    if os.path.exists(cf):
        text = io.open(cf, encoding="utf-8").read()
        m = re.search(r'DB_NAME\s*=\s*"([^"]+)"', text)
        if m:
            return "postgresql://geo_admin:testpw@localhost:55492/%s" % m.group(1)
        for cand in re.findall(r'postgresql://[^"\s]+', text):
            if not cand.endswith("/postgres") and "{" not in cand:
                return cand
    return "postgresql://geo_admin:testpw@localhost:55492/%s" % FALLBACK_DB


def run_all():
    failed = set()
    for pkg in PKGS:
        env = dict(os.environ)
        env["TEST_DATABASE_URL"] = dsn_for(pkg)
        env["PYTHONIOENCODING"] = "utf-8"
        p = subprocess.run(
            [sys.executable, "-m", "pytest", pkg, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=ROOT, env=env, capture_output=True)
        out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
        for line in out.splitlines():
            if line.startswith("FAILED ") or line.startswith("ERROR "):
                failed.add(line.split(" ")[1].split(" - ")[0])
        if "INTERNALERROR" in out:
            failed.add(pkg + "::<INTERNALERROR>")
    return failed


def main():
    saved = {f: io.open(os.path.join(ROOT, f), "rb").read() for f in FILES}
    saved_sha = {f: sha(f) for f in FILES}

    after = run_all()

    for f in FILES:
        blob = subprocess.run(["git", "show", "%s:%s" % (BASE_REV, f)],
                              cwd=ROOT, capture_output=True)
        assert blob.returncode == 0, blob.stderr.decode("utf-8", "replace")
        io.open(os.path.join(ROOT, f), "wb").write(blob.stdout)
    moved = [f for f in FILES if sha(f) != saved_sha[f]]
    print("退回生效:", len(moved), "/", len(FILES), "个文件")
    if len(moved) != len(FILES):
        print("  !! 有文件退回后 sha 没变 —— 基线没量成:",
              [f for f in FILES if f not in moved])

    before = run_all()

    for f in FILES:
        io.open(os.path.join(ROOT, f), "wb").write(saved[f])
    assert all(sha(f) == saved_sha[f] for f in FILES), "还原没回到原 sha!"

    print()
    print("基线(%s)失败集: %d 条" % (BASE_REV, len(before)))
    print("本单之后失败集    : %d 条" % len(after))
    new = sorted(after - before)
    gone = sorted(before - after)
    print()
    print("== 本单引入的新红 %d 条 ==" % len(new))
    for t in new:
        print("   " + t)
    if gone:
        print("== 本单之后反而绿了的 %d 条(要解释)==" % len(gone))
        for t in gone:
            print("   " + t)
    print()
    print("== 存量红(与本单无关)%d 条 ==" % len(before))
    for t in sorted(before)[:40]:
        print("   " + t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
