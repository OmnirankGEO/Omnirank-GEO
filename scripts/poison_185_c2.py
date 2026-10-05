# -*- coding: utf-8 -*-
"""#185 c2 注毒闸。

纪律(每条都对应一次踩过的坑):
  · 基线失败集必须为空,且条数等于预期 —— 否则"每发毒都像命中";
  · 每发毒自证**字节变了**(sha 前后不同),否则"毒没下成"与"锁没牙"同形;
  · 还原用**字节拷回**,不用 `git checkout --`(那回的是 HEAD);还原后核 sha;
  · 判读比**失败集差**,不比 rc;
  · 仪器自己不许带控制字符(0x08 会让否定断言恒真恒绿)。
"""
import glob
import hashlib
import io
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = "tests/defensive_direction_2026_09_13"
EXPECTED_N = 36
WHITESPACE = chr(10) + chr(13) + chr(9)

DQ = "writing/defensive_questions.py"
ASC = "writing/article_style_contract.py"
PS = "services/placement_service.py"
SRV = "server.py"

ENV = dict(os.environ)
ENV["TEST_DATABASE_URL"] = "postgresql://geo_admin:testpw@localhost:55492/geo_c14_185_test"
ENV["PYTHONIOENCODING"] = "utf-8"


def sha(path):
    return hashlib.sha256(io.open(os.path.join(ROOT, path), "rb").read()).hexdigest()[:12]


def read(path):
    return io.open(os.path.join(ROOT, path), encoding="utf-8").read()


def write(path, text):
    io.open(os.path.join(ROOT, path), "w", encoding="utf-8", newline="\n").write(text)


def run():
    p = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--no-header",
         "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    out = p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")
    failed = set()
    for line in out.splitlines():
        if line.startswith("FAILED ") or line.startswith("ERROR "):
            failed.add(line.split(" ")[1].split(" - ")[0])
    c = subprocess.run(
        [sys.executable, "-m", "pytest", PKG, "-q", "--collect-only",
         "--no-header", "-p", "no:cacheprovider"],
        cwd=ROOT, env=ENV, capture_output=True)
    total = len([l for l in c.stdout.decode("utf-8", "replace").splitlines() if "::" in l])
    return failed, total


POISONS = [
    ("D1-标题不拼品牌名(WO C1' 点名的毒)", DQ,
     '''        out.append({"question": question,
                    "title": "%s%s" % (brand, _TITLE_SUFFIX[question])})''',
     '''        out.append({"question": question,
                    "title": _TITLE_SUFFIX[question]})''',
     "去掉品牌名强制 —— 题就不是公司词了。"),

    ("D2-超容量补普通题(凑数)", DQ,
     """    unconverted.extend({"topic_id": t.get("id"), "reason": "capacity_exceeded"}
                       for t in eligible[len(titles):])""",
     """    for i, t in enumerate(eligible[len(titles):]):
        q = DEFENSIVE_QUESTIONS[i % len(DEFENSIVE_QUESTIONS)]
        applied.append({"topic_id": t.get("id"), "question": q,
                        "title": "%s%s" % (brand, _TITLE_SUFFIX[q])})""",
     "静默凑满 —— 客户会看到两条几乎一样的标题,而列表上看不出来。"),

    ("D3-落库值折成 company_facts(Review 毒1)", ASC,
     "    if raw in STYLE_FAMILIES or raw in OUTWARD_CHOICE_TO_FAMILY:\n        return raw",
     "    if raw in STYLE_FAMILIES:\n        return raw\n"
     "    if raw in OUTWARD_CHOICE_TO_FAMILY:\n        return OUTWARD_CHOICE_TO_FAMILY[raw]",
     "折叠后两类篇的两列都相同,「哪些是防御型」永久不可恢复。"),

    ("D4-方向改按 style_code 派生(Review 毒2)", PS,
     '''                row["is_defensive"] = (
                    str(row.get("user_choice") or "") == "defensive_company")''',
     '''                row["is_defensive"] = (
                    str(row.get("style_code") or "") == "brand_softarticle")''',
     "两者 style_code 逐字相同 ⇒ 普通 company_facts 篇会被当成防御型。"),

    ("D5-把防御型塞进推荐配比域(Review 判据3)", ASC,
     'USER_CHOICE_OPTIONS: Final[frozenset[str]] = frozenset(("auto", *USER_CHOICE_FAMILY_CODES))',
     'USER_CHOICE_OPTIONS: Final[frozenset[str]] = frozenset('
     '("auto", *USER_CHOICE_FAMILY_CODES, "defensive_company"))',
     "每个存量项目的推荐篇数都会变,而且不报错。"),

    ("D6-老调用默认走防御(WO C2 毒)", SRV,
     '    _req_uc = _norm_uc(request.user_choice, allow_auto=False) if request.user_choice else None',
     '    _req_uc = _norm_uc(request.user_choice, allow_auto=False) '
     'if request.user_choice else "defensive_company"',
     "所有重写标题的老入口一起被改掉。"),

    ("D7-固定槽也转", DQ,
     '''        if t.get("is_fixed"):
            unconverted.append({"topic_id": t.get("id"), "reason": "fixed_slot"})
        else:
            eligible.append(t)''',
     """        eligible.append(t)""",
     "系统固定槽被用户配比动掉(Review 09-13 裁定不动它)。"),

    ("D8-容量自称比实际多一条", DQ,
     "    return len(DEFENSIVE_QUESTIONS) + len(_VARIANTS)",
     "    return len(DEFENSIVE_QUESTIONS) + len(_VARIANTS) + 1",
     "前端按 N 封顶、后端只出得了 N-1 ⇒ "
     "「配比里能选 15、实际出 14」,没有任何东西会红。"),

    ("D9-缺事实提示直接吐列名", DQ,
     '    missing = [_FIELD_LABELS.get(f, f) for f in fields]',
     '    missing = [f for f in fields]',
     "工程词出现在用户面前(元指令第 11 条)。"),

    ("D10-防御篇不从 LLM 批里摘掉", SRV,
     "        topics_to_regen = [t for t in topics_to_regen if t[\"id\"] not in _touched]",
     "        topics_to_regen = list(topics_to_regen)",
     "已经出好题的篇又被送进 LLM 重写一遍(还要收一次费)。"),
]


def _no_control_chars():
    bad = []
    for f in [__file__] + glob.glob(os.path.join(ROOT, PKG, "*.py")):
        t = io.open(f, encoding="utf-8").read()
        hits = [hex(ord(c)) for c in t if ord(c) < 32 and c not in WHITESPACE]
        if hits:
            bad.append((f, sorted(set(hits))))
    return bad


def main():
    bad = _no_control_chars()
    if bad:
        print("  !! 仪器自己带控制字符,先修仪器:")
        for f, hits in bad:
            print("     %s %s" % (f, hits))
        return 2

    paths = sorted({p for _, p, _, _, _ in POISONS})
    base_sha = {p: sha(p) for p in paths}
    base_src = {p: read(p) for p in paths}

    failed, total = run()
    print("基线: 收集 %d 条 / 失败集 %d 条" % (total, len(failed)))
    if failed:
        print("  !! 基线失败集非空,先修基线")
        for f in sorted(failed):
            print("     " + f)
        return 2
    if total != EXPECTED_N:
        print("  !! 收集到 %d 条,预期 %d 条 —— 仪器没跑全" % (total, EXPECTED_N))
        return 2

    rows = []
    for name, path, old, new, why in POISONS:
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
        pf, ptotal = run()
        write(path, base_src[path])
        assert sha(path) == base_sha[path], "还原没回到基线 sha!"
        verdict = "红(锁有牙)" if pf else "绿(没牙/够不着/冗余)"
        rows.append((name, verdict, "sha %s->%s · 收集 %d · 抓住它的: %s" % (
            base_sha[path], psha, ptotal,
            ", ".join(sorted(x.split("::")[-1] for x in pf))[:150] or "无")))

    print()
    for name, verdict, detail in rows:
        print("%-42s %-22s %s" % (name, verdict, detail))
    f2, t2 = run()
    print("\n还原后复跑: 收集 %d / 失败 %d" % (t2, len(f2)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
