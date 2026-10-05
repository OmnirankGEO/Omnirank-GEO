# -*- coding: utf-8 -*-
"""#185 c1 归属:把 c1 改过的两个文件临时退回 PROD_TIP,量基线失败集。

纪律:字节拷回而不是 `git checkout --`(后者回的是 HEAD);还原后核 sha;
比**失败集差**而不是数量差 —— 数量相同也可能是换了一批。
"""
import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_REV = "5784baa9e"
FILES = ["server.py", "writing/article_style_contract.py",
         "writing/direction_distribution.py", "writing/style_registry.py",
         "services/placement_service.py"]

TESTS = [
    "tests/flywheel_close_loop/test_flywheel_b1_b5.py",
    "tests/flywheel_close_loop/test_flywheel_golive_2026_07_27.py",
    "tests/flywheel_integration/test_w1_query_intent_map.py",
    "tests/test_article_length_contract.py",
    "tests/test_article_type_spec_cards_2026_08_08.py",
    "tests/test_c5_title_year_rules_2026_07_28.py",
    "tests/test_canonical_family_extractability.py",
    "tests/test_crosstalk_sweep_2026_08_11.py",
    "tests/test_geo_article_column_alignment_v14.py",
    "tests/test_geo_v2_writing_hall.py",
    "tests/test_prompt_contract_consistency_2026_08_10.py",
    "tests/test_rework_r4_owner_locks_2026_08_11.py",
    "tests/test_title_keyword_semantic_binding.py",
    "tests/test_title_naturalness_2026_08_01.py",
    "tests/test_topic_style_default_2026_08_09.py",
    "tests/test_v210_direction_distribution.py",
    "tests/test_v210_endpoints_and_integration.py",
    "tests/test_wp11_evidence_reinforcement.py",
    "tests/test_writing_quality_full_audit_2026_07_29.py",
]

ENV = dict(os.environ)
ENV["TEST_DATABASE_URL"] = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_196_test"
ENV["PYTHONIOENCODING"] = "utf-8"


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def run():
    p = subprocess.run([sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header",
                        "-p", "no:cacheprovider"],
                       cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed = set()
    for line in out.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            failed.add(line.split(" ")[1].split(" - ")[0])
    return failed


def main():
    saved = {f: io.open(os.path.join(ROOT, f), "rb").read() for f in FILES}
    saved_sha = {f: sha(f) for f in FILES}

    after = run()

    # 退回 PROD_TIP 版本
    for f in FILES:
        blob = subprocess.run(["git", "show", "%s:%s" % (BASE_REV, f)],
                              cwd=ROOT, capture_output=True)
        assert blob.returncode == 0, blob.stderr.decode("utf-8", "replace")
        io.open(os.path.join(ROOT, f), "wb").write(blob.stdout)
    moved = [f for f in FILES if sha(f) != saved_sha[f]]
    print("退回生效的文件:", moved or "!! 一个都没变 —— 基线没量成")
    if len(moved) != len(FILES):
        for f in FILES:
            io.open(os.path.join(ROOT, f), "wb").write(saved[f])
        return 2

    before = run()

    for f in FILES:
        io.open(os.path.join(ROOT, f), "wb").write(saved[f])
    assert all(sha(f) == saved_sha[f] for f in FILES), "还原没回到原 sha!"

    print()
    print("基线(PROD_TIP %s)失败集 : %d 条" % (BASE_REV, len(before)))
    print("c1 之后失败集              : %d 条" % len(after))
    new = sorted(after - before)
    fixed = sorted(before - after)
    print()
    print("== c1 引入的新红 %d 条 ==" % len(new))
    for t in new:
        print("   " + t)
    if fixed:
        print("== c1 之后反而绿了的 %d 条(要解释)==" % len(fixed))
        for t in fixed:
            print("   " + t)
    print()
    print("== 存量红(与我无关,但要写进交付物)%d 条 ==" % len(before))
    for t in sorted(before)[:60]:
        print("   " + t)
    return 0


if __name__ == "__main__":
    sys.exit(main())
