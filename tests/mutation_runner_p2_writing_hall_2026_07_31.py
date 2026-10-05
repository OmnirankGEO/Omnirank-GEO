"""P2 变异 runner —— 证明锁有判别力,不是恒真断言。

三层分流(工单 §8):
  仍绿                        → 存活(锁没抓到)
  退出码非 0 但零断言失败      → **假红**,同样计存活(退出码可能来自 collection error /
                               无库 ERROR,不是锁抓到了变异)
  判别力断言不成立            → 存活(变异根本没落到被断言的位置上)

用法:python tests/mutation_runner_p2_writing_hall_2026_07_31.py
每个变异跑完**字节级还原**并校验 sha256 与变异前一致。
"""
import hashlib
import io
import os
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
TSX = ROOT / "frontend" / "src" / "pages" / "Writing" / "WritingHall.tsx"
LOCKS = "tests/test_p2_writing_hall_advisory_2026_07_31.py"


def read(path: Path) -> str:
    # newline='' —— 不让 Python 把 LF 换成 CRLF(否则"还原"会改掉整个文件)
    with io.open(path, "r", encoding="utf-8", newline="") as f:
        return f.read()


def write(path: Path, text: str) -> None:
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def run_locks():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault(
        "TEST_DATABASE_URL",
        "postgresql://nobody:nobody@127.0.0.1:5432/geo_agentscope_test",
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LOCKS, "-q", "--no-header", "-p", "no:cacheprovider"],
        cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"(\d+) failed", out)
    failed = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) passed", out)
    passed = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) skipped", out)
    skipped = int(m.group(1)) if m else 0
    errors = len(re.findall(r"^ERROR ", out, flags=re.M))
    return {
        "rc": proc.returncode, "failed": failed, "passed": passed,
        "skipped": skipped, "errors": errors, "names": set(re.findall(r"FAILED \S+::(\w+)", out)),
        "out": out,
    }


# (名字, 说明, 变异函数, 期望被打红的锁名前缀)
def mut_restore_navigate(src: str) -> str:
    """变异 ①:恢复确认后的强制跳转。"""
    anchor = "            if (selectedProject) await loadProjectDetail(selectedProject.id);\n        } catch {"
    assert src.count(anchor) == 1, "变异 ① 锚点不唯一"
    injected = (
        "            if (selectedProject) await loadProjectDetail(selectedProject.id);\n"
        "            if (\n"
        "                (payload.article_human_review_status === 'approved' || topic.publication_eligible)\n"
        "                && topic.article_id\n"
        "            ) {\n"
        "                navigate(`/publish?article_id=${topic.article_id}&quote_id=${selectedProject?.id || ''}`);\n"
        "            }\n"
        "        } catch {"
    )
    return src.replace(anchor, injected)


def mut_modal_back_to_details(src: str) -> str:
    """变异 ②:把 Modal 换回内联折叠元素(只需在组件体内重新出现该标签即可)。"""
    anchor = '            <Dialog open={panelOpen} onOpenChange={setPanelOpen}>'
    assert src.count(anchor) == 1, "变异 ② 锚点不唯一"
    tag = "<" + "details"
    return src.replace(
        anchor,
        f'            {tag} open={{panelOpen}}><summary>质量参考</summary></{"details"}>\n' + anchor,
    )


def mut_switch_back_to_rewrite(src: str) -> str:
    """变异 ③:面板里的段级免费修复切回收费的整篇重写端点。"""
    anchor = "authFetch(`/api/articles/${topic.article_id}/repair-finding`"
    # 该串在 ArticleLegalFindings(2 处)与 ArticleEvidenceAdvisory(1 处)都有;
    # 只改 advisory 组件里那一处 —— 精确对应"P2 把修复接线切回去"。
    start = src.index("function ArticleEvidenceAdvisory(props: {")
    end = src.index("// 写作进度类型", start)
    body = src[start:end]
    assert body.count(anchor) == 1, "变异 ③ 锚点不唯一"
    return src[:start] + body.replace(anchor, "authFetch(`/api/writing/rewrite/${topic.id}`") + src[end:]


def mut_drop_paid_labelling(src: str) -> str:
    """变异 ④(自加 · 反向):删掉付费入口的「计费」标注块。"""
    anchor = 'data-testid="advisory-whole-rewrite-section"'
    assert src.count(anchor) == 1, "变异 ④ 锚点不唯一"
    return src.replace(anchor, 'data-testid="advisory-extra-section"')


MUTATIONS = [
    ("① 恢复 navigate(...)", mut_restore_navigate, "lock1"),
    ("② Modal 改回折叠元素", mut_modal_back_to_details, "lock2"),
    ("③ 切回 rewrite", mut_switch_back_to_rewrite, "lock3/lock4"),
    ("④ 删掉付费入口计费标注(反向)", mut_drop_paid_labelling, "lock5"),
]


def main() -> int:
    original = read(TSX)
    base_sha = sha(original)

    baseline = run_locks()
    print(f"[baseline] rc={baseline['rc']} passed={baseline['passed']} "
          f"failed={baseline['failed']} errors={baseline['errors']} skipped={baseline['skipped']}")
    if baseline["failed"] or baseline["errors"] or baseline["rc"] != 0:
        print("!! 基线就不是全绿,变异结果不可解读")
        return 2
    if baseline["skipped"]:
        print("!! 基线有 skipped,整块被跳过等于没跑")
        return 2
    total_assertions = baseline["passed"]

    survivors = []
    for name, fn, expect in MUTATIONS:
        write(TSX, fn(original))
        try:
            res = run_locks()
        finally:
            write(TSX, original)
            assert sha(read(TSX)) == base_sha, f"{name}:字节级还原失败!"

        if res["failed"] == 0:
            verdict = "存活(仍绿)" if res["rc"] == 0 else "存活(**假红**:退出码非 0 但零断言失败)"
            survivors.append(name)
        elif res["skipped"]:
            verdict = f"存活(有 {res['skipped']} 个 skipped,统计不可信)"
            survivors.append(name)
        else:
            verdict = f"已杀 · 打红 {res['failed']} 条: {sorted(res['names'])}"
        print(f"[变异 {name}] 期望打红={expect} · rc={res['rc']} "
              f"failed={res['failed']} passed={res['passed']} skipped={res['skipped']} → {verdict}")

    print(f"\n锁断言总数={total_assertions} · skipped=0(基线实测)· 存活变异={len(survivors)}")
    if survivors:
        print("存活清单:", survivors)
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
