# -*- coding: utf-8 -*-
"""定点 A/B:d1 包在「我的改动」与「退回基线」下各跑 N 遍。

归属那一轮报了 1 条新红(W2),但单跑复现不了 —— 必须先让解释唯一:
  ① 真是我的缺陷(改动侧稳定红)
  ② 本来就是不稳定的(两侧都偶尔红)
  ③ 别的窗口在同一个库上并发(两侧都偶尔红,且与改动无关)
不做这一步就报"我没弄红",等于拿一次通过掩盖一次失败。
"""
import hashlib
import io
import os
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
PKG = "tests/geo_imgnote_d1_2026_09_13"
DSN = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_184_test"
ROUNDS = 3


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def run_once():
    env = dict(os.environ)
    env["TEST_DATABASE_URL"] = DSN
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=env, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    return sorted({l.split(" ")[1] for l in out.splitlines()
                   if l.startswith("FAILED ") or l.startswith("ERROR ")})


def arm(label):
    print("── %s ──" % label)
    for i in range(ROUNDS):
        f = run_once()
        print("   第 %d 遍: %s" % (i + 1, ("全绿" if not f else "红 " + ", ".join(
            x.split("::")[-1] for x in f))))


def main():
    saved = {f: io.open(os.path.join(ROOT, f), "rb").read() for f in FILES}
    saved_sha = {f: sha(f) for f in FILES}

    arm("A · 带本单改动")

    for f in FILES:
        blob = subprocess.run(["git", "show", "%s:%s" % (BASE_REV, f)],
                              cwd=ROOT, capture_output=True)
        assert blob.returncode == 0
        io.open(os.path.join(ROOT, f), "wb").write(blob.stdout)
    assert all(sha(f) != saved_sha[f] for f in FILES), "退回没生效,A/B 不成立"

    arm("B · 退回基线 %s" % BASE_REV)

    for f in FILES:
        io.open(os.path.join(ROOT, f), "wb").write(saved[f])
    assert all(sha(f) == saved_sha[f] for f in FILES), "还原没回到原 sha!"
    print("\n还原完成,sha 与开跑前一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
